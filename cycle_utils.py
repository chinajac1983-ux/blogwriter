from extensions import db
from config import DELIVERED_ARTICLE_STATUSES
from models import Article, SubscriptionCycle


def billing_days(billing):
    return {"monthly": 30, "quarterly": 90, "yearly": 365}.get(billing, 30)

def published_in_cycle(cycle_id):
    if not cycle_id:
        return 0
    return Article.query.filter(
        Article.cycle_id == cycle_id,
        Article.status.in_(DELIVERED_ARTICLE_STATUSES)
    ).count()

def get_active_cycle(client_id):
    return SubscriptionCycle.query.filter_by(client_id=client_id, status="active").order_by(SubscriptionCycle.started_at.asc()).first()

def get_next_pending_cycle(client_id):
    return SubscriptionCycle.query.filter_by(client_id=client_id, status="pending").order_by(SubscriptionCycle.created_at.asc(), SubscriptionCycle.id.asc()).first()

def get_latest_completed_cycle(client_id):
    return SubscriptionCycle.query.filter_by(client_id=client_id, status="completed").order_by(SubscriptionCycle.completed_at.desc(), SubscriptionCycle.id.desc()).first()

def get_current_or_next_cycle(client_id):
    return get_active_cycle(client_id) or get_next_pending_cycle(client_id)

def sync_client_legacy_cycle_fields(client, cycle):
    if not cycle:
        client.articles_quota = 0
        client.cycle_started_at = None
        client.expires_at = None
        return
    client.plan = cycle.plan
    client.billing = cycle.billing
    client.articles_per_month = cycle.weekly_articles
    client.articles_quota = cycle.quota
    client.cycle_started_at = cycle.started_at
    client.expires_at = cycle.expires_at

def cycle_is_fulfilled(cycle):
    """服务周期唯一完成标准：已交付文章数 >= quota。

    注意：expires_at 仅作为预计周期展示/提醒参考，不再作为停更或结束依据。
    """
    if not cycle:
        return False
    return published_in_cycle(cycle.id) >= int(cycle.quota or 0)
