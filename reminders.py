from datetime import datetime, timedelta

from config import FRONTEND_BASE_URL
from extensions import db
from models import Client, PaymentOrder, Article
from mail_utils import send_email
from site_utils import normalize_publish_mode

_app = None

def init_reminder_app(app):
    global _app
    _app = app

def _app_context():
    if _app is None:
        raise RuntimeError('Reminder app 未初始化')
    return _app.app_context()


def daily_reminder_check():
    with _app_context():
        now = datetime.utcnow()

        # 付费后未配置：按第一笔已支付订单 paid_at 起算，只在第 3 / 7 天提醒。
        for c in Client.query.filter_by(is_active=True).all():
            if not c.site:
                first_order = PaymentOrder.query.filter(
                    PaymentOrder.client_id == c.id,
                    PaymentOrder.status == "paid",
                    PaymentOrder.paid_at != None
                ).order_by(PaymentOrder.paid_at.asc()).first()
                if first_order and first_order.paid_at:
                    days = (now - first_order.paid_at).days

                    if days >= 7 and not first_order.config_reminder_7_sent_at:
                        send_email(
                            c.email,
                            "BlogWriter 网站配置提醒",
                            f"<p>你的 BlogWriter 服务已开通 {days} 天，但仍未完成网站配置。</p>"
                            f"<p>服务周期不会在配置前开始；完成配置并到达首次发布时间后，系统才会开始自动发布。</p>"
                            f"<p><a href='{FRONTEND_BASE_URL}/setup'>去完成配置</a></p>"
                        )
                        first_order.config_reminder_7_sent_at = now
                    elif days >= 3 and not first_order.config_reminder_3_sent_at:
                        send_email(
                            c.email,
                            "请完成 BlogWriter 网站配置",
                            f"<p>你的 BlogWriter 服务已开通 {days} 天，但尚未完成网站配置。</p>"
                            f"<p>完成配置并到达首次发布时间后，系统才会开始自动发布并激活服务周期。</p>"
                            f"<p><a href='{FRONTEND_BASE_URL}/setup'>去完成配置</a></p>"
                        )
                        first_order.config_reminder_3_sent_at = now

        # 2.8：服务周期必须发满 quota 才完成，不再发送“按时间到期”的提醒。

        # 草稿模式沉默提醒：累计未提醒草稿 >=5，或存在 14 天以上未处理草稿时，提醒客户。
        for c in Client.query.filter_by(is_active=True).all():
            if not c.site or normalize_publish_mode(c.site.publish_mode) != "draft":
                continue
            drafts = Article.query.filter_by(
                client_id=c.id,
                status="draft",
                draft_reminder_sent_at=None
            ).order_by(Article.created_at.asc()).all()
            old_drafts = [a for a in drafts if a.created_at and a.created_at <= now - timedelta(days=14)]
            if len(drafts) >= 5 or old_drafts:
                # 3.5：先标记并提交，再发送邮件；避免邮件已发但进程在 commit 前崩溃导致下次重复提醒。
                for a in drafts:
                    a.draft_reminder_sent_at = now
                db.session.commit()
                send_email(
                    c.email,
                    "你有 BlogWriter 草稿待处理",
                    f"<p>BlogWriter 已为你生成 {len(drafts)} 篇尚未提醒过的草稿，其中 {len(old_drafts)} 篇已超过 14 天。</p>"
                    f"<p>这些草稿需要你登录 WordPress 后台审核并发布；如果长期不处理，网站前台不会出现新内容，也就无法产生持续更新效果。</p>"
                    f"<p>请进入 WordPress 后台 → 文章 → 草稿，检查并发布合适的内容。</p>"
                )

        db.session.commit()
