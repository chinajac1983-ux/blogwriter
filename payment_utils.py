"""
Runify · BlogWriter — Payment Utils 3.6.2

支持：
- 微信支付 v3 Native 扫码支付
- 支付宝电脑网站支付 / 手机网站支付
- 所有密钥从数据库读取，Fernet 解密后传给 SDK
- 验签失败绝不激活订单
"""

import json
import secrets
import logging
from datetime import datetime

from flask import request

from config import PAYMENT_NOTIFY_DEV_MODE, PAYMENT_NOTIFY_SECRET, FRONTEND_BASE_URL
from extensions import db

log = logging.getLogger(__name__)

from models import PaymentOrder, PaymentConfig, Client, SubscriptionCycle
from price_utils import plan_config
from cycle_utils import get_current_or_next_cycle, sync_client_legacy_cycle_fields
from crypto_utils import decrypt

WECHAT_NOTIFY_URL = "https://api.runify.xiaoheili.com/wechat-notify"
ALIPAY_NOTIFY_URL = "https://api.runify.xiaoheili.com/alipay-notify"
ALIPAY_RETURN_URL = f"{FRONTEND_BASE_URL}/dashboard"


# ══════════════════════════════════════════════════════════════════
# 工具函数：从库读取支付配置
# ══════════════════════════════════════════════════════════════════

def get_payment_config(provider):
    """从数据库读取指定渠道的支付配置，解密返回 dict。"""
    try:
        row = PaymentConfig.query.filter_by(provider=provider, is_active=True).first()
        if not row or not row.config_json:
            return None
        return json.loads(decrypt(row.config_json))
    except Exception as e:
        log.error(f"[PaymentConfig] 读取 {provider} 配置失败：{e}")
        return None


def get_payment_cert(provider):
    """从数据库读取证书文件内容（apiclient_cert.pem），解密返回字符串。"""
    try:
        row = PaymentConfig.query.filter_by(provider=provider, is_active=True).first()
        if not row or not row.cert_file:
            return None
        return decrypt(row.cert_file)
    except Exception as e:
        log.error(f"[PaymentConfig] 读取 {provider} 证书失败：{e}")
        return None


def get_payment_cert_key(provider):
    """从数据库读取私钥文件内容（apiclient_key.pem），解密返回字符串。"""
    try:
        row = PaymentConfig.query.filter_by(provider=provider, is_active=True).first()
        if not row or not row.cert_key:
            return None
        return decrypt(row.cert_key)
    except Exception as e:
        log.error(f"[PaymentConfig] 读取 {provider} 私钥失败：{e}")
        return None


# ══════════════════════════════════════════════════════════════════
# 微信支付 v3
# ══════════════════════════════════════════════════════════════════

def _get_wechat_pay():
    """初始化微信支付 SDK 实例，从库读取配置。"""
    try:
        from wechatpayv3 import WeChatPay, WeChatPayType
    except ImportError:
        log.error("[WeChat] wechatpayv3 未安装，请检查 requirements.txt")
        return None, None

    cfg = get_payment_config("wechat")
    if not cfg:
        log.warning("[WeChat] 未找到启用的微信支付配置")
        return None, None

    private_key = get_payment_cert_key("wechat")
    cert_content = get_payment_cert("wechat")

    if not private_key:
        log.warning("[WeChat] 商户私钥未上传")
        return None, None
    if not cert_content:
        log.warning("[WeChat] 商户证书未上传")
        return None, None

    try:
        from cryptography import x509
        from cryptography.hazmat.backends import default_backend
        cert_obj = x509.load_pem_x509_certificate(cert_content.encode(), default_backend())
        cert_serial_no = format(cert_obj.serial_number, 'X')
        
        wxpay = WeChatPay(
            wechatpay_type=WeChatPayType.NATIVE,
            mchid=cfg.get("mch_id", ""),
            private_key=private_key,
            cert_serial_no=cert_serial_no,
            appid=cfg.get("appid", ""),
            apiv3_key=cfg.get("api_v3_key", ""),
            notify_url=WECHAT_NOTIFY_URL,
            cert_dir=None,
            public_key=cfg.get("public_key", ""),
            public_key_id=cfg.get("public_key_id", ""),
        )
        return wxpay, cfg
    except Exception as e:
        log.error(f"[WeChat] SDK 初始化失败：{e}")
        return None, None


def wechat_create_native_order(order_no, amount_fen, description="BlogWriter 订阅服务"):
    """创建微信 Native 扫码支付订单，返回 code_url（二维码链接）。"""
    wxpay, cfg = _get_wechat_pay()
    if not wxpay:
        raise ValueError("微信支付未配置或初始化失败")

    try:
        code, message = wxpay.pay(
            description=description,
            out_trade_no=order_no,
            amount={"total": int(amount_fen), "currency": "CNY"},
        )
        if code not in (200, 201):
            raise ValueError(f"微信下单失败：{message}")

        data = json.loads(message)
        code_url = data.get("code_url")
        if not code_url:
            raise ValueError(f"微信下单返回缺少 code_url：{message}")

        log.info(f"[WeChat] Native 订单创建成功：{order_no}")
        return code_url

    except Exception as e:
        log.error(f"[WeChat] 创建 Native 订单失败：{e}")
        raise


def wechat_verify_notify():
    """微信支付回调验签，返回 (verified: bool, data: dict)。"""
    wxpay, cfg = _get_wechat_pay()
    if not wxpay:
        log.warning("[WeChat] 验签失败：SDK 未初始化")
        return False, {}

    try:
        headers = {
            "Wechatpay-Timestamp": request.headers.get("Wechatpay-Timestamp", ""),
            "Wechatpay-Nonce": request.headers.get("Wechatpay-Nonce", ""),
            "Wechatpay-Signature": request.headers.get("Wechatpay-Signature", ""),
            "Wechatpay-Serial": request.headers.get("Wechatpay-Serial", ""),
            "Wechatpay-Signature-Type": request.headers.get(
                     "Wechatpay-Signature-Type",
                     "WECHATPAY2-SHA256-RSA2048"
            ),
        }
        body = request.data.decode("utf-8")

        result = wxpay.callback(headers=headers, body=body)
        if not result:
            log.warning("[WeChat] 回调验签失败")
            return False, {}

        event_type = result.get("event_type", "")
        if event_type != "TRANSACTION.SUCCESS":
            log.info(f"[WeChat] 非支付成功事件，忽略：{event_type}")
            return False, {}

        resource = result.get("resource", {})
        trade_info = resource if isinstance(resource, dict) else {}

        return True, {
            "order_no": trade_info.get("out_trade_no", ""),
            "trade_no": trade_info.get("transaction_id", ""),
            "amount": trade_info.get("amount", {}).get("total", 0),
        }

    except Exception as e:
        log.error(f"[WeChat] 回调处理异常：{e}")
        return False, {}


# ══════════════════════════════════════════════════════════════════
# 支付宝
# ══════════════════════════════════════════════════════════════════

def _get_alipay():
    """初始化支付宝 SDK 实例，从库读取配置。"""
    try:
        from alipay import AliPay, AliPayConfig
    except ImportError:
        log.error("[Alipay] alipay-sdk-python 未安装，请检查 requirements.txt")
        return None, None

    cfg = get_payment_config("alipay")
    if not cfg:
        log.warning("[Alipay] 未找到启用的支付宝配置")
        return None, None

    private_key = cfg.get("private_key", "")
    alipay_public_key = cfg.get("alipay_public_key", "")

    if not private_key or not alipay_public_key:
        log.warning("[Alipay] 私钥或支付宝公钥未配置")
        return None, None

    try:
        def wrap_key(key, key_type="PUBLIC KEY"):
            key = key.strip()
            if "-----" not in key:
                key = f"-----BEGIN {key_type}-----\n{key}\n-----END {key_type}-----"
            return key

        def wrap_private_key(key):
            key = key.strip()
            if "-----" not in key:
                key = f"-----BEGIN RSA PRIVATE KEY-----\n{key}\n-----END RSA PRIVATE KEY-----"
            return key

        alipay = AliPay(
            appid=cfg.get("app_id", ""),
            app_notify_url=ALIPAY_NOTIFY_URL,
            app_private_key_string=wrap_private_key(private_key),
            alipay_public_key_string=wrap_key(alipay_public_key),
            sign_type="RSA2",
            debug=False,
            verbose=False,
            config=AliPayConfig(timeout=15),
        )
        return alipay, cfg

    except Exception as e:
        log.error(f"[Alipay] SDK 初始化失败：{e}")
        return None, None


def alipay_create_page_order(order_no, amount_yuan, subject="BlogWriter 订阅服务", is_mobile=False):
    """创建支付宝电脑/手机网站支付订单，返回支付跳转 URL。"""
    alipay, cfg = _get_alipay()
    if not alipay:
        raise ValueError("支付宝未配置或初始化失败")

    try:
        gateway = "https://openapi.alipay.com/gateway.do"

        if is_mobile:
            order_string = alipay.api_alipay_trade_wap_pay(
                out_trade_no=order_no,
                total_amount=str(amount_yuan),
                subject=subject,
                return_url=ALIPAY_RETURN_URL,
                notify_url=ALIPAY_NOTIFY_URL,
            )
        else:
            order_string = alipay.api_alipay_trade_page_pay(
                out_trade_no=order_no,
                total_amount=str(amount_yuan),
                subject=subject,
                return_url=ALIPAY_RETURN_URL,
                notify_url=ALIPAY_NOTIFY_URL,
            )

        pay_url = f"{gateway}?{order_string}"
        log.info(f"[Alipay] 订单创建成功：{order_no} is_mobile={is_mobile}")
        return pay_url

    except Exception as e:
        log.error(f"[Alipay] 创建订单失败：{e}")
        raise


def alipay_verify_notify():
    """支付宝回调验签，返回 (verified: bool, data: dict)。"""
    alipay, cfg = _get_alipay()
    if not alipay:
        log.warning("[Alipay] 验签失败：SDK 未初始化")
        return False, {}

    try:
        data = request.form.to_dict()
        signature = data.pop("sign", None)
        data.pop("sign_type", None)

        if not signature:
            log.warning("[Alipay] 回调缺少签名")
            return False, {}

        verified = alipay.verify(data, signature)
        if not verified:
            log.warning("[Alipay] 回调验签失败")
            return False, {}

        trade_status = data.get("trade_status", "")
        if trade_status != "TRADE_SUCCESS":
            log.info(f"[Alipay] 非支付成功状态，忽略：{trade_status}")
            return False, {}

        amount_yuan = data.get("total_amount", "0")
        try:
            amount_fen = int(round(float(amount_yuan) * 100))
        except Exception:
            amount_fen = 0

        return True, {
            "order_no": data.get("out_trade_no", ""),
            "trade_no": data.get("trade_no", ""),
            "amount": amount_fen,
        }

    except Exception as e:
        log.error(f"[Alipay] 回调处理异常：{e}")
        return False, {}


# ══════════════════════════════════════════════════════════════════
# 统一验签入口（兼容旧接口）
# ══════════════════════════════════════════════════════════════════

def payment_notify_verified(pay_method):
    """支付回调验签入口，返回 bool。"""
    if PAYMENT_NOTIFY_DEV_MODE and PAYMENT_NOTIFY_SECRET:
        return request.headers.get("X-Payment-Notify-Secret") == PAYMENT_NOTIFY_SECRET

    if pay_method == "wechat":
        verified, _ = wechat_verify_notify()
        return verified

    if pay_method == "alipay":
        verified, _ = alipay_verify_notify()
        return verified

    log.warning(f"[Payment] 未知渠道 {pay_method}，拒绝回调")
    return False


# ══════════════════════════════════════════════════════════════════
# 订单工具函数
# ══════════════════════════════════════════════════════════════════

def generate_order_no():
    return "RUN" + datetime.utcnow().strftime("%Y%m%d%H%M%S") + secrets.token_hex(6).upper()


def activate_paid_order(order):
    if order.status == "paid":
        return

    cfg = plan_config(order.plan, order.billing)
    if cfg["amount"] <= 0:
        raise ValueError("无效套餐，不能激活订单")

    order.status = "paid"
    order.paid_at = datetime.utcnow()

    client = Client.query.get(order.client_id)
    if not client:
        raise ValueError("客户不存在")

    client.is_active = True

    cycle = SubscriptionCycle(
        client_id=client.id,
        order=order,
        plan=order.plan,
        billing=order.billing,
        weekly_articles=cfg["weekly_articles"],
        quota=cfg["articles_quota"],
        status="pending"
    )
    db.session.add(cycle)

    if not get_current_or_next_cycle(client.id):
        sync_client_legacy_cycle_fields(client, cycle)

    db.session.commit()
    log.info(f"[Payment] 订单 {order.order_no} 已支付，生成 pending cycle，client={client.email}")


def handle_verified_payment(order_no, amount, pay_method, trade_no=""):
    """验签通过后的统一支付成功处理，防重复、防金额篡改。"""
    if not order_no:
        raise ValueError("缺少订单号")

    order = PaymentOrder.query.filter_by(order_no=order_no).first()
    if not order:
        raise ValueError("订单不存在")

    if order.status == "paid":
        log.info(f"[Payment] 订单 {order.order_no} 已处理过，忽略重复回调")
        return order

    if int(amount or 0) != int(order.amount or 0):
        raise ValueError(f"订单金额不一致：notify={amount}, order={order.amount}")

    if pay_method and order.pay_method and pay_method != order.pay_method:
        raise ValueError(f"支付方式不一致：notify={pay_method}, order={order.pay_method}")

    if trade_no:
        order.trade_no = str(trade_no)

    activate_paid_order(order)
    log.info(f"[Payment] 回调验签通过，订单已激活：order={order.order_no}, trade_no={trade_no}")
    return order
