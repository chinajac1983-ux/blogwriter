import json
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify

from auth_utils import require_admin, generate_admin_token, verify_and_upgrade_password
from extensions import db
from models import Client, Article, PaymentOrder, PlanPrice, SubscriptionCycle, Admin, PaymentConfig, IndustryNews
from rate_limit import check_login_rate_limit
from payment_utils import activate_paid_order
from cycle_utils import get_active_cycle, get_next_pending_cycle, get_latest_completed_cycle, published_in_cycle, sync_client_legacy_cycle_fields
from site_utils import site_wp_test_is_current, circuit_requires_new_test
from time_utils import utc_to_cst_str
from scheduler_jobs import publish_lock_is_active, run_publish_job, register_schedule
from config import DELIVERED_ARTICLE_STATUSES
from crypto_utils import encrypt, decrypt

admin_bp = Blueprint('admin', __name__)


# ══════════════════════════════════════════════════════════════════
# 登录
# ══════════════════════════════════════════════════════════════════

@admin_bp.route("/admin/login", methods=["POST"])
def admin_login():
    data = request.get_json() or {}
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")
    if not email or not password:
        return jsonify({"error": "邮箱和密码不能为空"}), 400
    allowed, limit_err = check_login_rate_limit(f"admin:{email}")
    if not allowed:
        return jsonify({"error": limit_err, "error_code": "RATE_LIMITED"}), 429
    admin = Admin.query.filter_by(email=email).first()
    if not admin or not verify_and_upgrade_password(admin, password):
        return jsonify({"error": "邮箱或密码错误"}), 401
    return jsonify({"token": generate_admin_token(admin.id, admin.email, admin.password_hash), "email": admin.email})


# ══════════════════════════════════════════════════════════════════
# 客户管理
# ══════════════════════════════════════════════════════════════════

@admin_bp.route("/admin/clients", methods=["GET"])
@require_admin
def admin_clients():
    clients = Client.query.order_by(Client.created_at.desc()).all()
    rows = []
    for c in clients:
        active_cycle = get_active_cycle(c.id)
        pending_cycle = get_next_pending_cycle(c.id)
        completed_cycle = get_latest_completed_cycle(c.id)
        display_cycle = active_cycle or pending_cycle or completed_cycle
        latest = Article.query.filter(Article.client_id == c.id, Article.status.in_(DELIVERED_ARTICLE_STATUSES)).order_by(Article.published_at.desc()).first()
        quota = int(display_cycle.quota or 0) if display_cycle else 0
        cycle_published = published_in_cycle(display_cycle.id) if display_cycle else 0
        cycle_remaining = max(quota - cycle_published, 0)
        rows.append({
            "id": c.id,
            "email": c.email,
            "plan": display_cycle.plan if display_cycle else c.plan,
            "billing": display_cycle.billing if display_cycle else c.billing,
            "is_active": c.is_active,
            "is_publishing": publish_lock_is_active(c),
            "publishing_locked_until": utc_to_cst_str(c.publishing_locked_until) if c.publishing_locked_until else None,
            "is_circuit_open": c.is_circuit_open,
            "circuit_opened_at": utc_to_cst_str(c.circuit_opened_at) if c.circuit_opened_at else None,
            "is_expired": False,
            "has_paid": c.is_active,
            "created_at": utc_to_cst_str(c.created_at),
            "cycle_id": display_cycle.id if display_cycle else None,
            "cycle_status": display_cycle.status if display_cycle else None,
            "cycle_started_at": utc_to_cst_str(display_cycle.started_at) if display_cycle and display_cycle.started_at else None,
            "expires_at": utc_to_cst_str(display_cycle.expires_at) if display_cycle and display_cycle.expires_at else None,
            "brand_name": c.site.brand_name if c.site else "",
            "site_url": c.site.wp_url if c.site else None,
            "site_configured": c.site is not None,
            "wp_username": c.site.wp_username if c.site else "",
            "wp_app_password_set": bool(c.site and c.site.wp_app_password),
            "wp_app_password_masked": "********" if c.site and c.site.wp_app_password else "",
            "publish_mode": c.site.publish_mode if c.site else None,
            "wp_test_passed": site_wp_test_is_current(c.site) if c.site else False,
            "wp_test_passed_at": utc_to_cst_str(c.site.wp_test_passed_at) if c.site and c.site.wp_test_passed_at else None,
            "articles_published": Article.query.filter(Article.client_id == c.id, Article.status.in_(DELIVERED_ARTICLE_STATUSES)).count(),
            "articles_failed": Article.query.filter_by(client_id=c.id, status="failed").count(),
            "last_published_at": utc_to_cst_str(latest.published_at) if latest and latest.published_at else None,
            "articles_quota": quota,
            "published_in_cycle": cycle_published,
            "remaining_articles": cycle_remaining,
            "pending_cycles_count": SubscriptionCycle.query.filter_by(client_id=c.id, status="pending").count(),
            "schedule": {
                "interval_hours": c.schedule.interval_hours if c.schedule else None,
                "start_date": utc_to_cst_str(c.schedule.start_date) if c.schedule and c.schedule.start_date else None,
                "next_run_at": utc_to_cst_str(c.schedule.next_run_at) if c.schedule and c.schedule.next_run_at else None,
                "is_active": c.schedule.is_active if c.schedule else False
            }
        })
    return jsonify(rows)


@admin_bp.route("/admin/stats", methods=["GET"])
@require_admin
def admin_stats():
    under_delivered = 0
    for c in Client.query.all():
        cycle = get_active_cycle(c.id)
        if cycle and published_in_cycle(cycle.id) < int(cycle.quota or 0):
            under_delivered += 1
    return jsonify({
        "total_clients": Client.query.count(),
        "active_clients": Client.query.filter_by(is_active=True).count(),
        "unpaid_clients": Client.query.filter_by(is_active=False).count(),
        "pending_cycles": SubscriptionCycle.query.filter_by(status="pending").count(),
        "active_cycles": SubscriptionCycle.query.filter_by(status="active").count(),
        "completed_cycles": SubscriptionCycle.query.filter_by(status="completed").count(),
        "expired_clients": 0,
        "total_articles": Article.query.filter(Article.status.in_(DELIVERED_ARTICLE_STATUSES)).count(),
        "total_words": db.session.query(db.func.coalesce(db.func.sum(Article.word_count), 0)).filter(Article.status.in_(DELIVERED_ARTICLE_STATUSES)).scalar(),
        "failed_articles": Article.query.filter_by(status="failed").count(),
        "error_clients": db.session.query(Article.client_id).filter_by(status="failed").distinct().count(),
        "expiring_clients": 0,
        "under_delivered_clients": under_delivered,
        "total_orders": PaymentOrder.query.count(),
        "paid_orders": PaymentOrder.query.filter_by(status="paid").count(),
        "pending_orders": PaymentOrder.query.filter_by(status="pending").count(),
    })


@admin_bp.route("/admin/logs", methods=["GET"])
@require_admin
def admin_logs():
    client_id = request.args.get("client_id", type=int)
    limit = request.args.get("limit", 50, type=int)
    q = Article.query.order_by(Article.created_at.desc())
    if client_id:
        q = q.filter_by(client_id=client_id)
    articles = q.limit(limit).all()
    return jsonify([{
        "id": a.id,
        "client_id": a.client_id,
        "cycle_id": a.cycle_id,
        "client_email": a.client.email if a.client else "",
        "client_name": a.client.site.brand_name if a.client and a.client.site else "",
        "title": a.title,
        "status": a.status,
        "error_msg": a.error_msg,
        "created_at": utc_to_cst_str(a.created_at),
        "published_at": utc_to_cst_str(a.published_at) if a.published_at else None,
        "wp_post_id": a.wp_post_id,
        "url": a.wp_url,
        "word_count": a.word_count
    } for a in articles])


@admin_bp.route("/admin/trigger-publish", methods=["POST"])
@require_admin
def admin_trigger_publish():
    data = request.get_json() or {}
    client_id = data.get("client_id")
    if not client_id:
        return jsonify({"error": "需要 client_id"}), 400
    result = run_publish_job(int(client_id))
    return jsonify(result), (200 if result.get("success") else 400)


@admin_bp.route("/admin/update-expiry", methods=["POST"])
@require_admin
def admin_update_expiry():
    data = request.get_json() or {}
    client_id = data.get("client_id")
    days = int(data.get("days", 30))
    client = Client.query.get(client_id)
    if not client:
        return jsonify({"error": "客户不存在"}), 404
    cycle = get_active_cycle(client.id)
    if not cycle:
        return jsonify({"error": "当前没有 active 周期，不能修正预计周期时间"}), 400
    cycle.expires_at = datetime.utcnow() + timedelta(days=days)
    sync_client_legacy_cycle_fields(client, cycle)
    db.session.commit()
    return jsonify({"success": True, "expires_at": utc_to_cst_str(cycle.expires_at)})


@admin_bp.route("/admin/reset-circuit", methods=["POST"])
@require_admin
def admin_reset_circuit():
    data = request.get_json() or {}
    client_id = data.get("client_id")
    client = Client.query.get(client_id)
    if not client:
        return jsonify({"error": "客户不存在"}), 404
    if circuit_requires_new_test(client):
        return jsonify({"error": "请先通过 WordPress 连接测试以证明配置已修复"}), 400
    client.consecutive_failures = 0
    client.is_circuit_open = False
    client.circuit_opened_at = None
    client.publishing_locked_until = None
    db.session.commit()
    if client.schedule and client.schedule.is_active and (client.schedule.next_run_at or client.schedule.start_date):
        register_schedule(client.id, client.schedule.interval_hours, client.schedule.next_run_at or client.schedule.start_date)
    return jsonify({"success": True, "message": "熔断状态已手动解除"})


@admin_bp.route("/admin/set-client-active", methods=["POST"])
@require_admin
def admin_set_client_active():
    """手动激活或停用客户。停用不影响已有周期数据，只是停止服务。"""
    data = request.get_json() or {}
    client_id = data.get("client_id")
    is_active = data.get("is_active")
    if client_id is None or is_active is None:
        return jsonify({"error": "缺少 client_id 或 is_active"}), 400
    client = Client.query.get(client_id)
    if not client:
        return jsonify({"error": "客户不存在"}), 404
    client.is_active = bool(is_active)
    db.session.commit()
    return jsonify({"success": True, "is_active": client.is_active})


@admin_bp.route("/admin/add-quota", methods=["POST"])
@require_admin
def admin_add_quota():
    """给当前 active 周期手动补发 quota，用于退款补偿或人工补发场景。"""
    data = request.get_json() or {}
    client_id = data.get("client_id")
    add_count = int(data.get("add_count", 0))
    if not client_id or add_count <= 0:
        return jsonify({"error": "缺少 client_id 或 add_count 必须大于0"}), 400
    client = Client.query.get(client_id)
    if not client:
        return jsonify({"error": "客户不存在"}), 404
    cycle = get_active_cycle(client.id) or get_next_pending_cycle(client.id)
    if not cycle:
        return jsonify({"error": "该客户没有可用周期"}), 400
    cycle.quota = int(cycle.quota or 0) + add_count
    sync_client_legacy_cycle_fields(client, cycle)
    db.session.commit()
    return jsonify({
        "success": True,
        "new_quota": cycle.quota,
        "published": published_in_cycle(cycle.id),
        "remaining": max(cycle.quota - published_in_cycle(cycle.id), 0)
    })


# ══════════════════════════════════════════════════════════════════
# 价格管理
# ══════════════════════════════════════════════════════════════════

@admin_bp.route("/admin/prices", methods=["GET"])
@require_admin
def admin_prices():
    rows = PlanPrice.query.order_by(PlanPrice.plan, PlanPrice.billing).all()
    return jsonify([{
        "id": r.id,
        "plan": r.plan,
        "billing": r.billing,
        "amount": r.amount,
        "price_yuan": r.amount / 100,
        "weekly_articles": r.weekly_articles,
        "articles_quota": r.articles_quota,
        "is_active": r.is_active,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None
    } for r in rows])


@admin_bp.route("/admin/update-price", methods=["POST"])
@require_admin
def admin_update_price():
    data = request.get_json() or {}
    plan = data.get("plan")
    billing = data.get("billing")
    amount = int(data.get("amount", 0))
    weekly_articles = int(data.get("weekly_articles", 0))
    articles_quota = int(data.get("articles_quota", 0))
    if not plan or not billing or amount <= 0 or weekly_articles <= 0 or articles_quota <= 0:
        return jsonify({"error": "参数不完整"}), 400
    row = PlanPrice.query.filter_by(plan=plan, billing=billing).first() or PlanPrice(plan=plan, billing=billing)
    row.amount = amount
    row.weekly_articles = weekly_articles
    row.articles_quota = articles_quota
    row.is_active = True
    row.updated_at = datetime.utcnow()
    db.session.add(row)
    db.session.commit()
    return jsonify({"success": True})


@admin_bp.route("/admin/toggle-price", methods=["POST"])
@require_admin
def admin_toggle_price():
    """启用或禁用某个套餐，禁用后前端 /plans 接口不再返回该套餐。"""
    data = request.get_json() or {}
    price_id = data.get("id")
    is_active = data.get("is_active")
    if price_id is None or is_active is None:
        return jsonify({"error": "缺少 id 或 is_active"}), 400
    row = PlanPrice.query.get(price_id)
    if not row:
        return jsonify({"error": "套餐不存在"}), 404
    row.is_active = bool(is_active)
    row.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"success": True, "is_active": row.is_active})


# ══════════════════════════════════════════════════════════════════
# 订单管理
# ══════════════════════════════════════════════════════════════════

@admin_bp.route("/admin/orders", methods=["GET"])
@require_admin
def admin_orders():
    """订单列表，支持按状态、渠道、客户筛选。"""
    status = request.args.get("status")
    pay_method = request.args.get("pay_method")
    client_id = request.args.get("client_id", type=int)
    limit = request.args.get("limit", 100, type=int)

    q = PaymentOrder.query.order_by(PaymentOrder.created_at.desc())
    if status:
        q = q.filter_by(status=status)
    if pay_method:
        q = q.filter_by(pay_method=pay_method)
    if client_id:
        q = q.filter_by(client_id=client_id)

    orders = q.limit(limit).all()
    # PaymentOrder 没有 client relationship，用 client_id 查询
    client_ids = list({o.client_id for o in orders if o.client_id})
    clients_map = {c.id: c for c in Client.query.filter(Client.id.in_(client_ids)).all()}
    return jsonify([{
        "id": o.id,
        "order_no": o.order_no,
        "client_id": o.client_id,
        "client_email": clients_map.get(o.client_id).email if clients_map.get(o.client_id) else "",
        "plan": o.plan,
        "billing": o.billing,
        "amount": o.amount,
        "price_yuan": round(o.amount / 100, 2) if o.amount else 0,
        "pay_method": o.pay_method,
        "status": o.status,
        "trade_no": o.trade_no or "",
        "refund_note": o.refund_note or "",
        "created_at": utc_to_cst_str(o.created_at),
        "paid_at": utc_to_cst_str(o.paid_at) if o.paid_at else None,
    } for o in orders])


@admin_bp.route("/admin/mark-order-paid", methods=["POST"])
@require_admin
def admin_mark_order_paid():
    data = request.get_json() or {}
    order_no = data.get("order_no")
    order = PaymentOrder.query.filter_by(order_no=order_no).first()
    if not order:
        return jsonify({"error": "订单不存在"}), 404
    try:
        activate_paid_order(order)
        return jsonify({"success": True})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": str(e)}), 400


@admin_bp.route("/admin/update-refund-note", methods=["POST"])
@require_admin
def admin_update_refund_note():
    """给订单添加退款备注，仅内部记录，不影响订单状态。"""
    data = request.get_json() or {}
    order_no = data.get("order_no")
    note = (data.get("note") or "").strip()
    if not order_no:
        return jsonify({"error": "缺少 order_no"}), 400
    order = PaymentOrder.query.filter_by(order_no=order_no).first()
    if not order:
        return jsonify({"error": "订单不存在"}), 404
    order.refund_note = note
    db.session.commit()
    return jsonify({"success": True})


# ══════════════════════════════════════════════════════════════════
# 支付配置管理
# ══════════════════════════════════════════════════════════════════

@admin_bp.route("/admin/payment-configs", methods=["GET"])
@require_admin
def admin_payment_configs():
    """查看所有渠道支付配置，config_json 和 cert_file 脱敏返回。"""
    rows = PaymentConfig.query.order_by(PaymentConfig.provider).all()
    result = []
    for r in rows:
        # 解密后只返回字段名，不返回值，避免密钥泄露
        config_keys = []
        if r.config_json:
            try:
                cfg = json.loads(decrypt(r.config_json))
                config_keys = list(cfg.keys())
            except Exception:
                config_keys = ["[解密失败]"]
        result.append({
            "id": r.id,
            "provider": r.provider,
            "is_active": r.is_active,
            "has_cert": bool(r.cert_file),
            "has_cert_key": bool(r.cert_key),
            "config_keys": config_keys,   # 只返回字段名，不返回值
            "updated_at": utc_to_cst_str(r.updated_at) if r.updated_at else None,
            "updated_by": r.updated_by or "",
        })
    return jsonify(result)


@admin_bp.route("/admin/payment-configs", methods=["POST"])
@require_admin
def admin_upsert_payment_config():
    """新增或更新支付渠道配置，config_json 加密存库。
    
    请求体示例（微信）：
    {
        "provider": "wechat",
        "is_active": true,
        "config": {
            "appid": "wx...",
            "mch_id": "1234567890",
            "api_v3_key": "..."
        }
    }
    
    请求体示例（支付宝）：
    {
        "provider": "alipay",
        "is_active": true,
        "config": {
            "app_id": "2021...",
            "private_key": "MIIEow...",
            "alipay_public_key": "MIIBIj..."
        }
    }
    """
    data = request.get_json() or {}
    provider = (data.get("provider") or "").strip().lower()
    is_active = data.get("is_active", False)
    config = data.get("config") or {}

    if provider not in ("wechat", "alipay"):
        return jsonify({"error": "provider 只支持 wechat 或 alipay"}), 400
    if not config:
        return jsonify({"error": "config 不能为空"}), 400

    # 按渠道校验必填字段
    required_fields = {
        "wechat": ["appid", "mch_id", "api_v3_key", "public_key", "public_key_id"],
        "alipay": ["app_id", "private_key", "alipay_public_key"],
    }
    missing = [f for f in required_fields[provider] if not config.get(f)]
    if missing:
        return jsonify({"error": f"缺少必填字段：{', '.join(missing)}"}), 400

    try:
        encrypted_config = encrypt(json.dumps(config, ensure_ascii=False))
    except Exception as e:
        return jsonify({"error": f"配置加密失败：{e}"}), 500

    from flask import g
    admin_email = g.admin.email if hasattr(g, "admin") else ""

    row = PaymentConfig.query.filter_by(provider=provider).first()
    if not row:
        row = PaymentConfig(provider=provider)
        db.session.add(row)

    row.is_active = bool(is_active)
    row.config_json = encrypted_config
    row.updated_at = datetime.utcnow()
    row.updated_by = admin_email
    db.session.commit()

    return jsonify({"success": True, "provider": provider, "is_active": row.is_active})


@admin_bp.route("/admin/payment-configs/<provider>/toggle", methods=["POST"])
@require_admin
def admin_toggle_payment_config(provider):
    """单独启用或禁用某渠道，不改配置内容。"""
    data = request.get_json() or {}
    is_active = data.get("is_active")
    if is_active is None:
        return jsonify({"error": "缺少 is_active"}), 400
    row = PaymentConfig.query.filter_by(provider=provider).first()
    if not row:
        return jsonify({"error": f"渠道 {provider} 未配置"}), 404
    row.is_active = bool(is_active)
    row.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"success": True, "provider": provider, "is_active": row.is_active})


@admin_bp.route("/admin/payment-configs/<provider>/upload-cert", methods=["POST"])
@require_admin
def admin_upload_payment_cert(provider):
    """上传证书和私钥（PEM 文本），加密存库。
    
    请求体：
    {
        "cert_content": "-----BEGIN CERTIFICATE-----
...",
        "cert_key_content": "-----BEGIN PRIVATE KEY-----
..."
    }
    """
    if provider not in ("wechat", "alipay"):
        return jsonify({"error": "provider 只支持 wechat 或 alipay"}), 400

    data = request.get_json() or {}
    cert_content = (data.get("cert_content") or "").strip()
    cert_key_content = (data.get("cert_key_content") or "").strip()

    if not cert_content:
        return jsonify({"error": "cert_content 不能为空"}), 400
    if "BEGIN" not in cert_content:
        return jsonify({"error": "证书格式不正确，应为 PEM 格式"}), 400

    try:
        encrypted_cert = encrypt(cert_content)
    except Exception as e:
        return jsonify({"error": f"证书加密失败：{e}"}), 500

    row = PaymentConfig.query.filter_by(provider=provider).first()
    if not row:
        return jsonify({"error": f"渠道 {provider} 未配置，请先保存基本配置"}), 404

    from flask import g
    admin_email = g.admin.email if hasattr(g, "admin") else ""

    row.cert_file = encrypted_cert

    if cert_key_content:
        if "BEGIN" not in cert_key_content:
            return jsonify({"error": "私钥格式不正确，应为 PEM 格式"}), 400
        try:
            row.cert_key = encrypt(cert_key_content)
        except Exception as e:
            return jsonify({"error": f"私钥加密失败：{e}"}), 500

    row.updated_at = datetime.utcnow()
    row.updated_by = admin_email
    db.session.commit()

    return jsonify({
        "success": True,
        "provider": provider,
        "has_cert": True,
        "has_cert_key": bool(cert_key_content)
    })


# ══════════════════════════════════════════════════════════════════
# 4.0 新增：行业新闻管理
# ══════════════════════════════════════════════════════════════════

@admin_bp.route("/admin/industry-news", methods=["GET"])
@require_admin
def admin_industry_news():
    """查看行业新闻库，支持按客户、来源轨道筛选。"""
    client_id = request.args.get("client_id", type=int)
    source = request.args.get("source")        # "self" / "hermes"
    source_api = request.args.get("source_api")
    limit = request.args.get("limit", 50, type=int)

    q = IndustryNews.query.order_by(IndustryNews.created_at.desc())
    if client_id:
        q = q.filter_by(client_id=client_id)
    if source:
        q = q.filter_by(source=source)
    if source_api:
        q = q.filter_by(source_api=source_api)

    items = q.limit(limit).all()
    return jsonify([{
        "id": i.id,
        "client_id": i.client_id,
        "source": i.source,
        "source_api": i.source_api,
        "title": i.title,
        "summary": (i.summary or "")[:200],
        "url": i.url,
        "quality_score": i.quality_score,
        "has_full_text": i.has_full_text,
        "language": i.language,
        "keywords_matched": i.keywords_matched,
        "published_at": utc_to_cst_str(i.published_at) if i.published_at else None,
        "created_at": utc_to_cst_str(i.created_at),
        "expires_at": utc_to_cst_str(i.expires_at) if i.expires_at else None,
        "used_count": i.used_count,
        "last_used_at": utc_to_cst_str(i.last_used_at) if i.last_used_at else None,
    } for i in items])


@admin_bp.route("/admin/industry-news/stats", methods=["GET"])
@require_admin
def admin_industry_news_stats():
    """行业新闻库统计：各来源数量、过期情况。"""
    from sqlalchemy import func
    from datetime import datetime

    now = datetime.utcnow()
    total = IndustryNews.query.count()
    active = IndustryNews.query.filter(
        (IndustryNews.expires_at == None) | (IndustryNews.expires_at > now)
    ).count()
    expired = total - active

    by_source = db.session.query(
        IndustryNews.source,
        func.count(IndustryNews.id)
    ).group_by(IndustryNews.source).all()

    by_source_api = db.session.query(
        IndustryNews.source_api,
        func.count(IndustryNews.id)
    ).group_by(IndustryNews.source_api).all()

    return jsonify({
        "total": total,
        "active": active,
        "expired": expired,
        "by_source": {row[0]: row[1] for row in by_source},
        "by_source_api": {row[0]: row[1] for row in by_source_api},
    })


@admin_bp.route("/admin/trigger-industry-news-scan", methods=["POST"])
@require_admin
def admin_trigger_industry_news_scan():
    """手动触发指定客户的行业新闻扫描（自研轨道）。"""
    data = request.get_json() or {}
    client_id = data.get("client_id")
    if not client_id:
        return jsonify({"error": "需要 client_id"}), 400

    client = Client.query.get(client_id)
    if not client:
        return jsonify({"error": "客户不存在"}), 404

    try:
        from industry_news import fetch_industry_news_for_client
        fetch_industry_news_for_client(int(client_id))
        count = IndustryNews.query.filter_by(client_id=client_id, source="self").count()
        return jsonify({"success": True, "total_self": count})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@admin_bp.route("/admin/industry-news/<int:news_id>", methods=["DELETE"])
@require_admin
def admin_delete_industry_news(news_id):
    """删除单条行业新闻记录（用于清理低质量数据）。"""
    item = IndustryNews.query.get(news_id)
    if not item:
        return jsonify({"error": "记录不存在"}), 404
    db.session.delete(item)
    db.session.commit()
    return jsonify({"success": True})


# ══════════════════════════════════════════════════════════════════
# 4.0 新增：文章质检统计
# ══════════════════════════════════════════════════════════════════

@admin_bp.route("/admin/article-quality-stats", methods=["GET"])
@require_admin
def admin_article_quality_stats():
    """文章质检统计，用于AB测试对比和质量监控。"""
    from sqlalchemy import func

    # 总体质检覆盖
    total_checked = Article.query.filter(Article.quality_score.isnot(None)).count()
    total_unchecked = Article.query.filter(Article.quality_score.is_(None)).count()

    # 质量分布
    high = Article.query.filter(Article.quality_score >= 85).count()
    mid = Article.query.filter(
        Article.quality_score >= 70,
        Article.quality_score < 85
    ).count()
    low = Article.query.filter(
        Article.quality_score.isnot(None),
        Article.quality_score < 70
    ).count()

    # 平均分
    avg_score = db.session.query(
        func.avg(Article.quality_score)
    ).filter(Article.quality_score.isnot(None)).scalar()

    # 重写触发率
    rewritten = Article.query.filter(Article.quality_rewrite_count > 0).count()

    # needs_review 数量（质检低分且已发布）
    needs_review = Article.query.filter(
        Article.quality_score.isnot(None),
        Article.quality_score < 70,
        Article.status.in_(["draft", "published"])
    ).count()

    # AB测试：按info_source分组
    by_source = db.session.query(
        Article.info_source,
        func.count(Article.id),
        func.avg(Article.quality_score)
    ).filter(
        Article.quality_score.isnot(None)
    ).group_by(Article.info_source).all()

    # 按客户分组的质检情况（只返回有问题的）
    problem_clients = db.session.query(
        Article.client_id,
        func.count(Article.id).label("low_count"),
        func.avg(Article.quality_score).label("avg_score")
    ).filter(
        Article.quality_score.isnot(None),
        Article.quality_score < 70
    ).group_by(Article.client_id).having(
        func.count(Article.id) >= 2
    ).all()

    # 查询客户邮箱
    client_ids = [row[0] for row in problem_clients]
    clients_map = {c.id: c.email for c in Client.query.filter(Client.id.in_(client_ids)).all()}

    return jsonify({
        "coverage": {
            "total_checked": total_checked,
            "total_unchecked": total_unchecked,
        },
        "quality_distribution": {
            "high_85_plus": high,
            "mid_70_to_84": mid,
            "low_under_70": low,
            "avg_score": round(float(avg_score), 1) if avg_score else None,
        },
        "operations": {
            "rewritten_count": rewritten,
            "rewrite_rate": round(rewritten / total_checked * 100, 1) if total_checked else 0,
            "needs_review": needs_review,
        },
        "ab_test": [{
            "info_source": row[0] or "none",
            "article_count": row[1],
            "avg_quality_score": round(float(row[2]), 1) if row[2] else None,
        } for row in by_source],
        "problem_clients": [{
            "client_id": row[0],
            "client_email": clients_map.get(row[0], ""),
            "low_quality_count": row[1],
            "avg_score": round(float(row[2]), 1) if row[2] else None,
        } for row in problem_clients],
    })
