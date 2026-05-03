from datetime import datetime, timedelta
from flask import request

from config import REGISTER_RATE_LIMIT_WINDOW_SECONDS, REGISTER_RATE_LIMIT_MAX_PER_IP, REGISTER_RATE_LIMIT_MAX_PER_EMAIL, LOGIN_RATE_LIMIT_WINDOW_SECONDS, LOGIN_RATE_LIMIT_MAX_PER_IP, LOGIN_RATE_LIMIT_MAX_PER_EMAIL
from extensions import db
from models import RateLimitAttempt


def get_request_ip():
    """获取真实访问 IP；如果后面有反向代理，需要确保代理层正确传递 X-Forwarded-For。"""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"

def check_rate_limit(scope, key, window_seconds, max_attempts):
    """数据库级限流：重启、多 worker 下仍然有效。"""
    now = datetime.utcnow()
    cutoff = now - timedelta(seconds=window_seconds)
    RateLimitAttempt.query.filter(RateLimitAttempt.created_at < cutoff).delete(synchronize_session=False)
    count = RateLimitAttempt.query.filter(
        RateLimitAttempt.scope == scope,
        RateLimitAttempt.key == key,
        RateLimitAttempt.created_at >= cutoff
    ).count()
    if count >= max_attempts:
        db.session.commit()
        return False
    db.session.add(RateLimitAttempt(scope=scope, key=key, created_at=now))
    db.session.commit()
    return True

def check_register_rate_limit(email):
    """注册接口数据库级限流，防止机器人批量注册消耗资源。"""
    ip = get_request_ip()
    email_key = (email or '').lower()
    if not check_rate_limit("register_ip", ip, REGISTER_RATE_LIMIT_WINDOW_SECONDS, REGISTER_RATE_LIMIT_MAX_PER_IP):
        return False, "注册请求过于频繁，请稍后再试"
    if not check_rate_limit("register_email", email_key, REGISTER_RATE_LIMIT_WINDOW_SECONDS, REGISTER_RATE_LIMIT_MAX_PER_EMAIL):
        return False, "该邮箱注册尝试过于频繁，请稍后再试"
    return True, ""

def check_login_rate_limit(email):
    """登录接口数据库级限流，降低撞库和暴力破解风险。"""
    ip = get_request_ip()
    email_key = (email or '').lower()
    if not check_rate_limit("login_ip", ip, LOGIN_RATE_LIMIT_WINDOW_SECONDS, LOGIN_RATE_LIMIT_MAX_PER_IP):
        return False, "登录请求过于频繁，请稍后再试"
    if not check_rate_limit("login_email", email_key, LOGIN_RATE_LIMIT_WINDOW_SECONDS, LOGIN_RATE_LIMIT_MAX_PER_EMAIL):
        return False, "该邮箱登录尝试过于频繁，请稍后再试"
    return True, ""
