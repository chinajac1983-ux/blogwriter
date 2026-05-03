from datetime import datetime

import logging

from extensions import db

log = logging.getLogger(__name__)
from models import PlanPrice


def plan_config(plan, billing):
    """产品定义中心。优先读取后台价格表；未配置时回退到内置默认价格。"""
    try:
        row = PlanPrice.query.filter_by(plan=plan, billing=billing, is_active=True).first()
        if row:
            return {
                "amount": row.amount,
                "weekly_articles": row.weekly_articles,
                "articles_per_month": row.weekly_articles,
                "articles_quota": row.articles_quota,
            }
    except Exception:
        pass

    table = {
        ("trial", "monthly"): (3900, 3, 12),
        ("standard", "monthly"): (7900, 3, 12),
        ("standard", "quarterly"): (19000, 3, 36),
        ("standard", "yearly"): (61700, 3, 144),
        ("pro", "monthly"): (9900, 5, 20),
        ("pro", "quarterly"): (23800, 5, 60),
        ("pro", "yearly"): (77200, 5, 240),
    }
    amount, weekly_articles, articles_quota = table.get((plan, billing), (0, 0, 0))
    return {
        "amount": amount,
        "weekly_articles": weekly_articles,
        "articles_per_month": weekly_articles,
        "articles_quota": articles_quota,
    }

def init_default_prices():
    """首次启动时初始化默认价格，避免管理后台价格表为空。"""
    if PlanPrice.query.count() > 0:
        return

    defaults = [
        ("trial", "monthly", 3900, 3, 12),
        ("standard", "monthly", 7900, 3, 12),
        ("standard", "quarterly", 19000, 3, 36),
        ("standard", "yearly", 61700, 3, 144),
        ("pro", "monthly", 9900, 5, 20),
        ("pro", "quarterly", 23800, 5, 60),
        ("pro", "yearly", 77200, 5, 240),
    ]
    for plan, billing, amount, weekly_articles, quota in defaults:
        db.session.add(PlanPrice(
            plan=plan,
            billing=billing,
            amount=amount,
            weekly_articles=weekly_articles,
            articles_quota=quota,
            is_active=True,
            updated_at=datetime.utcnow()
        ))
    db.session.commit()
    log.info("[Startup] 已初始化默认套餐价格")
