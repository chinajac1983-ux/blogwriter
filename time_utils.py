import re
import random
from datetime import datetime, timedelta

from config import SHANGHAI_TZ, UTC_TZ, JITTER_MIN_HOURS, JITTER_MAX_HOURS


def cst_to_utc(dt_str):
    """
    前端传入的时间按北京时间理解，存库统一用 UTC naive datetime。
    例如：2026-05-05T10:00:00 表示北京时间 10:00。
    """
    if not dt_str:
        return None
    dt = datetime.fromisoformat(str(dt_str).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SHANGHAI_TZ)
    return dt.astimezone(UTC_TZ).replace(tzinfo=None)

def utc_to_cst_dt(dt):
    """数据库 UTC naive datetime → 北京时间 aware datetime，用于调度。"""
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC_TZ)
    return dt.astimezone(SHANGHAI_TZ)

def utc_to_cst_str(dt):
    """数据库 UTC naive datetime → 北京时间字符串，返回给前端。"""
    if not dt:
        return None
    return utc_to_cst_dt(dt).isoformat(timespec="seconds")

def get_interval_hours(plan):
    if plan in ("trial", "standard"):
        return 56.0
    if plan == "pro":
        return 33.6
    return 56.0

def calculate_next_run_at(interval_hours):
    """按基础间隔加入 3–5 小时随机浮动，并限制边界，返回 UTC naive datetime。"""
    base = float(interval_hours or 56.0)
    direction = random.choice([-1, 1])
    jitter = random.uniform(JITTER_MIN_HOURS, JITTER_MAX_HOURS) * direction
    actual_hours = base + jitter

    # 防止连续极值导致频率失控：standard/trial 控制在 52-60 小时，pro 控制在 30-38 小时。
    if base >= 50:
        actual_hours = max(52.0, min(60.0, actual_hours))
    else:
        actual_hours = max(30.0, min(38.0, actual_hours))

    return datetime.utcnow() + timedelta(hours=actual_hours)

def estimate_word_count(html_content, language="English"):
    """粗略统计文章字数/词数，用于用户后台展示交付体感。"""
    text = re.sub(r"<[^>]+>", " ", html_content or "")
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return 0
    if (language or "").lower().startswith("english"):
        return len(re.findall(r"\b[\w'-]+\b", text))
    # 中文等语言粗略按非空字符计数
    return len(re.sub(r"\s+", "", text))
