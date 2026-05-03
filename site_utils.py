import hashlib

from crypto_utils import decrypt
from time_utils import utc_to_cst_str


def normalize_publish_mode(value):
    return "publish" if value == "publish" else "draft"


def mask_secret(value, keep_start=2, keep_end=2):
    """对敏感字段脱敏，避免接口误把应用密码、token、secret 暴露给前端。"""
    if not value:
        return ""
    value = str(value)
    if len(value) <= keep_start + keep_end:
        return "*" * len(value)
    return value[:keep_start] + "*" * (len(value) - keep_start - keep_end) + value[-keep_end:]


def wp_config_signature(wp_url, username, app_password):
    """根据 WordPress 连接信息生成签名；只要客户改了地址、用户名或应用密码，测试状态就失效。"""
    raw = f"{(wp_url or '').rstrip('/')}|{username or ''}|{app_password or ''}"
    return hashlib.sha256(raw.encode()).hexdigest()


def site_wp_test_is_current(site):
    """
    判断当前 WordPress 配置是否仍然有效。

    原逻辑问题：
    - 只要签名不一致 → 判定为未测试 → 停止发布（过于严格）

    新逻辑：
    1. 签名一致 → 直接通过（原逻辑）
    2. 签名不一致 → 尝试真实连接 WordPress：
       - 如果仍然能连接 → 视为有效（避免误停）
       - 如果连接失败 → 判定为无效
    """
    if not site or not site.wp_url or not site.wp_username or not site.wp_app_password:
        return False

    try:
        app_pwd = decrypt(site.wp_app_password)
    except Exception:
        return False

    # ✅ 优先走原签名机制（性能最好）
    if site.wp_test_passed_at and site.wp_test_signature:
        expected = wp_config_signature(site.wp_url, site.wp_username, app_pwd)
        if site.wp_test_signature == expected:
            return True

    # ⭐ 新增：签名不一致时，使用真实连接兜底
    try:
        from wp_utils import validate_wordpress
        ok, _ = validate_wordpress(site.wp_url, site.wp_username, app_pwd)
        if ok:
            return True
    except Exception:
        pass

    return False


def site_wp_test_matches_input(site, wp_url, username, app_password):
    """判断用户当前输入的 WordPress 配置是否已经测试通过。

    只要三项配置不变，就不再重复创建测试草稿；
    用户修改 URL / 用户名 / 应用密码后，签名变化，必须重新测试。
    """
    if not site or not site.wp_test_passed_at or not site.wp_test_signature:
        return False
    expected = wp_config_signature((wp_url or "").rstrip("/"), username or "", app_password or "")
    return site.wp_test_signature == expected


def circuit_requires_new_test(client):
    """熔断恢复前，必须确认 WP 测试时间晚于熔断发生时间。"""
    if not client or not client.is_circuit_open:
        return False
    if not client.circuit_opened_at:
        return False
    site = client.site
    if not site or not site.wp_test_passed_at:
        return True
    return site.wp_test_passed_at <= client.circuit_opened_at


def site_public_payload(site):
    """站点信息对前端输出的安全版本：永远不返回 WordPress 应用密码明文。"""
    if not site:
        return {
            "wp_url": None,
            "wp_username": "",
            "wp_app_password_set": False,
            "wp_app_password_masked": "",
            "publish_mode": None,
            "wp_test_passed": False,
            "wp_test_passed_at": None,
            "wp_test_post_id": None,
        }
    return {
        "wp_url": site.wp_url,
        "wp_username": site.wp_username or "",
        "wp_app_password_set": bool(site.wp_app_password),
        "wp_app_password_masked": "********" if site.wp_app_password else "",
        "publish_mode": site.publish_mode,
        "wp_test_passed": site_wp_test_is_current(site),
        "wp_test_passed_at": utc_to_cst_str(site.wp_test_passed_at) if site.wp_test_passed_at else None,
        "wp_test_post_id": site.wp_test_post_id,
    }