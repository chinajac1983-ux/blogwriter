"""
Runify · BlogWriter — Routes Client 4.1 (Decoupled Version)

职责：
- 处理客户侧的所有 API 请求（注册、登录、订单、配置、状态、Signals）
- 维护配置变更时的逻辑联动（如重置 AI 关键词）
- 对接拆分后的 snapshot_scanner 模块
"""

import secrets
from datetime import datetime, timedelta
import math
import logging
from flask import Blueprint, request, jsonify, g

from extensions import db
from auth_utils import require_client, generate_token, hash_password, verify_and_upgrade_password
from config import RESET_TOKEN_EXPIRE_MINUTES, FRONTEND_BASE_URL, SHANGHAI_TZ, UTC_TZ

from models import Client, Site, Schedule, Article, PaymentOrder, PasswordReset, SubscriptionCycle, Signal
from rate_limit import check_register_rate_limit, check_login_rate_limit
from price_utils import plan_config
from payment_utils import generate_order_no, handle_verified_payment
from cycle_utils import get_current_or_next_cycle, get_active_cycle, get_next_pending_cycle, get_latest_completed_cycle, sync_client_legacy_cycle_fields, published_in_cycle
from site_utils import site_public_payload, site_wp_test_matches_input, wp_config_signature, normalize_publish_mode, circuit_requires_new_test
from crypto_utils import encrypt, decrypt
from time_utils import cst_to_utc, utc_to_cst_dt, utc_to_cst_str, get_interval_hours
from wp_utils import validate_wordpress, create_wordpress_test_draft
from mail_utils import send_email

# ── 关键对接：从拆分后的模块引入 ──────────────────────────────────────────
from scheduler_jobs import register_schedule, publish_lock_is_active
from snapshot_scanner import trigger_snapshot_scan_async 

log = logging.getLogger(__name__)
client_bp = Blueprint('client', __name__)

# =========================
# 基础信息与方案
# =========================

@client_bp.route("/plans", methods=["GET"])
def public_plans():
    plans = []
    keys = [
        ("trial", "monthly"), ("standard", "monthly"), ("standard", "quarterly"), ("standard", "yearly"),
        ("pro", "monthly"), ("pro", "quarterly"), ("pro", "yearly"),
    ]
    for plan, billing in keys:
        cfg = plan_config(plan, billing)
        if cfg["amount"] > 0:
            plans.append({
                "plan": plan,
                "billing": billing,
                "amount": cfg["amount"],
                "price_yuan": cfg["amount"] / 100,
                "weekly_articles": cfg["weekly_articles"],
                "articles_quota": cfg["articles_quota"],
            })
    return jsonify(plans)

@client_bp.route("/payment-methods", methods=["GET"])
def public_payment_methods():
    from models import PaymentConfig
    rows = PaymentConfig.query.filter_by(is_active=True).all()
    return jsonify([r.provider for r in rows])

# =========================
# 注册与登录
# =========================

@client_bp.route("/client/register", methods=["POST"])
def client_register():
    data = request.get_json() or {}
    email = data.get("email", "").strip().lower()
    pwd = data.get("password", "")
    if not email or not pwd:
        return jsonify({"error": "邮箱和密码不能为空"}), 400
    allowed, limit_err = check_register_rate_limit(email)
    if not allowed:
        return jsonify({"error": limit_err, "error_code": "RATE_LIMITED"}), 429
    if len(pwd) < 8:
        return jsonify({"error": "密码至少需要8位字符"}), 400
    if Client.query.filter_by(email=email).first():
        return jsonify({"error": "该邮箱已注册，请直接登录", "error_code": "ALREADY_REGISTERED"}), 409
    
    client = Client(email=email, password_hash=hash_password(pwd), is_active=False)
    db.session.add(client)
    db.session.commit()
    return jsonify({
        "token": generate_token(client.id, client.email, client.password_hash), 
        "email": client.email, 
        "plan": "", 
        "is_active": False, 
        "has_paid": False, 
        "expires_at": None
    })

@client_bp.route("/client/login", methods=["POST"])
def client_login():
    data = request.get_json() or {}
    email = data.get("email", "").strip().lower()
    pwd = data.get("password", "")
    if not email or not pwd:
        return jsonify({"error": "邮箱和密码不能为空"}), 400
    allowed, limit_err = check_login_rate_limit(email)
    if not allowed:
        return jsonify({"error": limit_err, "error_code": "RATE_LIMITED"}), 429
    
    client = Client.query.filter_by(email=email).first()
    if not client:
        return jsonify({"error": "该邮箱尚未注册，请先开通账号", "error_code": "NOT_REGISTERED"}), 404
    if not verify_and_upgrade_password(client, pwd):
        return jsonify({"error": "密码错误，请重试或点击忘记密码找回", "error_code": "WRONG_PASSWORD"}), 401
    
    cycle = get_current_or_next_cycle(client.id)
    if cycle:
        sync_client_legacy_cycle_fields(client, cycle)
        db.session.commit()
    
    return jsonify({
        "token": generate_token(client.id, client.email, client.password_hash),
        "email": client.email,
        "plan": cycle.plan if cycle else client.plan,
        "billing": cycle.billing if cycle else client.billing,
        "is_active": client.is_active,
        "is_expired": False,
        "has_paid": client.is_active,
        "site_configured": client.site is not None,
        "expires_at": utc_to_cst_str(cycle.expires_at) if cycle and cycle.expires_at else None,
    })

# =========================
# 状态查询 (核心面板)
# =========================

@client_bp.route("/client/status", methods=["GET"])
@require_client
def client_status():
    client = g.client
    active_cycle = get_active_cycle(client.id)
    pending_cycle = get_next_pending_cycle(client.id)
    completed_cycle = get_latest_completed_cycle(client.id)
    display_cycle = active_cycle or pending_cycle or completed_cycle
    
    if display_cycle:
        sync_client_legacy_cycle_fields(client, display_cycle)
        db.session.commit()
    
    quota = int(display_cycle.quota or 0) if display_cycle else 0
    published = published_in_cycle(display_cycle.id) if display_cycle else 0
    remaining = max(quota - published, 0)
    
    # 获取最近20篇文章
    articles = Article.query.filter_by(client_id=client.id).order_by(Article.created_at.desc()).limit(20).all()
    sched = client.schedule

    return jsonify({
        "email": client.email,
        "plan": display_cycle.plan if display_cycle else client.plan,
        "billing": display_cycle.billing if display_cycle else client.billing,
        "cycle_status": display_cycle.status if display_cycle else None,
        "articles_quota": quota,
        "published_in_cycle": published,
        "remaining_articles": remaining,
        "expires_at": utc_to_cst_str(display_cycle.expires_at) if display_cycle and display_cycle.expires_at else None,
        "has_pending_cycle": pending_cycle is not None,
        "pending_cycles_count": SubscriptionCycle.query.filter_by(client_id=client.id, status="pending").count(),
        
        # 4.1 新增：内容增强相关字段
        "user_industries": client.site.user_industries if client.site else None,
        "user_buyer_types": client.site.user_buyer_types if client.site else None,
        
        **site_public_payload(client.site),
        
        "is_active": client.is_active,
        "site_configured": client.site is not None,
        "is_publishing": publish_lock_is_active(client),
        "is_circuit_open": client.is_circuit_open,
        "schedule": {
            "interval_hours": sched.interval_hours if sched else None,
            "next_run_at": utc_to_cst_str(sched.next_run_at) if sched and sched.next_run_at else None,
            "is_active": sched.is_active if sched else False
        },
        "recent_articles": [{
            "id": a.id,
            "title": a.title,
            "status": a.status,
            "published_at": utc_to_cst_str(a.published_at) if a.published_at else None,
            "url": a.wp_url,
            "quality_score": a.quality_score,
            "quality_rewrite_count": a.quality_rewrite_count,
            "info_source": a.info_source,
            "seo_focus_keyword": a.seo_focus_keyword,
            "seo_slug": a.seo_slug,
        } for a in articles]
    })

# =========================
# 站点配置逻辑
# =========================

@client_bp.route("/setup-client", methods=["POST"])
@require_client
def setup_client():
    data = request.get_json() or {}
    client = g.client
    if not client.is_active:
        return jsonify({"error": "账号未激活，请先完成支付"}), 403
    
    wp_url = data.get("wp_url", "").rstrip("/")
    wp_username = data.get("wp_username", "")
    wp_app_pwd = data.get("wp_app_password", "")
    
    # 必须先通过测试
    existing_site = client.site
    expected_signature = wp_config_signature(wp_url, wp_username, wp_app_pwd)
    if not existing_site or not existing_site.wp_test_passed_at or existing_site.wp_test_signature != expected_signature:
        return jsonify({"error": "请先点击“测试 WordPress 发布连接”，测试通过后再保存配置"}), 400

    site = existing_site
    site.wp_url = wp_url
    site.wp_username = wp_username
    site.wp_app_password = encrypt(wp_app_pwd)
    
    # 4.1 补齐所有 4.1 新增字段
    fields = [
        "topic_keywords", "brand_name", "site_url", "markets", "language", 
        "product_desc", "target_customer", "customer_pain", "win_reason", 
        "competitors", "references", "publish_mode", "user_industries", "user_buyer_types"
    ]
    for field in fields:
        if field in data:
            setattr(site, field, data.get(field) or "")
            
    site.publish_mode = normalize_publish_mode(site.publish_mode)
    site.article_length = int(data.get("article_length", site.article_length or 1500))
    site.configured_at = datetime.utcnow()
    db.session.add(site)

    # 调度初始化
    cycle = get_current_or_next_cycle(client.id)
    interval_hours = get_interval_hours(cycle.plan if cycle else client.plan or "standard")
    start_date_raw = data.get("start_date")
    start_date = cst_to_utc(start_date_raw) if start_date_raw else datetime.utcnow() + timedelta(hours=1)

    if start_date <= datetime.utcnow():
        return jsonify({"error": "首次发布时间必须晚于当前时间"}), 400

    sched = client.schedule or Schedule(client_id=client.id)
    sched.interval_hours = interval_hours
    sched.start_date = start_date
    sched.next_run_at = start_date
    sched.is_active = True
    db.session.add(sched)
    
    client.consecutive_failures = 0
    client.is_circuit_open = False
    db.session.commit()
    
    register_schedule(client.id, interval_hours, start_date)
    # 异步触发首次扫描与关键词生成
    trigger_snapshot_scan_async(client.id)

    return jsonify({"success": True, "message": "配置保存成功，系统将在设定时间开始首次发布"})

@client_bp.route("/client/update-site", methods=["POST"])
@require_client
def update_site():
    data = request.get_json() or {}
    client = g.client
    site = client.site
    if not site:
        return jsonify({"error": "请先完成初始配置"}), 404

    # 4.1：核心逻辑 —— 检查产品/市场是否变化，若变化则重置 AI 关键词
    if "product_desc" in data or "markets" in data:
        if (data.get("product_desc") and data["product_desc"] != site.product_desc) or \
           (data.get("markets") and data["markets"] != site.markets):
            site.ai_search_keywords = None
            site.ai_keywords_generated_at = None
            log.info(f"[IndustryNews] 检测到核心配置变更，已重置 AI 关键词缓存 client={client.id}")

    # 字段同步
    fields = [
        "topic_keywords", "brand_name", "site_url", "markets", "language", 
        "product_desc", "target_customer", "customer_pain", "win_reason", 
        "competitors", "references", "publish_mode", "user_industries", "user_buyer_types"
    ]
    for field in fields:
        if field in data:
            setattr(site, field, data.get(field) or "")
    
    if "article_length" in data:
        site.article_length = int(data["article_length"])
    
    site.configured_at = datetime.utcnow()
    client.is_circuit_open = False # 更新配置通常意味着修复，解除熔断
    db.session.commit()

    return jsonify({"success": True, "message": "站点配置已更新"})

# =========================
# 其他模块 (支付/密码/Signals - 保持原有)
# =========================

@client_bp.route("/client/signals", methods=["GET"])
@require_client
def get_signals():
    client = g.client
    signals = Signal.query.filter_by(client_id=client.id, is_active=True).order_by(Signal.created_at.desc()).all()
    return jsonify([{
        "id": s.id, "type": s.type, "content": s.content, "weight": s.weight,
        "used_count": s.used_count, "last_used_at": utc_to_cst_str(s.last_used_at)
    } for s in signals])

@client_bp.route("/client/signals", methods=["POST"])
@require_client
def create_signal():
    data = request.get_json() or {}
    client = g.client
    signal = Signal(
        client_id=client.id, type=data.get("type"), content=data.get("content"),
        weight=int(data.get("weight", 2)), is_active=True
    )
    db.session.add(signal)
    db.session.commit()
    return jsonify({"success": True, "id": signal.id})

@client_bp.route("/client/signals/<int:signal_id>", methods=["DELETE"])
@require_client
def delete_signal(signal_id):
    sig = Signal.query.filter_by(id=signal_id, client_id=g.client.id).first()
    if sig:
        sig.is_active = False
        db.session.commit()
    return jsonify({"success": True})

@client_bp.route("/client/test-wordpress", methods=["POST"])
@require_client
def test_wordpress_connection():
    data = request.get_json() or {}
    client = g.client
    wp_url = (data.get("wp_url") or "").rstrip("/")
    wp_username = data.get("wp_username") or ""
    wp_app_pwd = data.get("wp_app_password") or ""
    
    ok, err = validate_wordpress(wp_url, wp_username, wp_app_pwd)
    if not ok: return jsonify({"error": err}), 400

    test_result = create_wordpress_test_draft(wp_url, wp_username, wp_app_pwd)
    site = client.site or Site(client_id=client.id)
    site.wp_url, site.wp_username = wp_url, wp_username
    site.wp_app_password = encrypt(wp_app_pwd)
    site.wp_test_passed_at = datetime.utcnow()
    site.wp_test_signature = wp_config_signature(wp_url, wp_username, wp_app_pwd)
    db.session.add(site)
    db.session.commit()

    return jsonify({"success": True, "message": "连接测试成功"})

@client_bp.route("/client/forgot-password", methods=["POST"])
def forgot_password():
    data = request.get_json() or {}
    email = data.get("email", "").strip().lower()
    client = Client.query.filter_by(email=email).first()
    if client:
        token = secrets.token_urlsafe(32)
        db.session.add(PasswordReset(email=email, token=token, expires_at=datetime.utcnow()+timedelta(minutes=30)))
        db.session.commit()
        send_email(email, "重置密码", f"Token: {token}")
    return jsonify({"success": True})