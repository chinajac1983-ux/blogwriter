"""
Runify · BlogWriter — Article Verifier

职责：
- 文章发布后的前台可见性回查
- publish 模式下检查文章 URL 是否可访问
- 回查失败时发送邮件通知

不负责：
- 文章生成（见 article_generator.py）
- 文章发布（见 scheduler_jobs.py）
- 质检（见 quality_checker.py）
"""

import logging
from datetime import datetime, timedelta

from config import OPS_EMAIL
from mail_utils import send_email
from site_utils import normalize_publish_mode
from time_utils import utc_to_cst_dt, utc_to_cst_str
from wp_utils import verify_wordpress_public_url

log = logging.getLogger(__name__)

_app = None


def init_verifier_app(app):
    global _app
    _app = app


def _app_context():
    if _app is None:
        raise RuntimeError('Verifier app 未初始化')
    return _app.app_context()


def schedule_publish_visibility_check(article_id, delay_seconds=60):
    """注册文章前台回查任务，发布后延迟执行。"""
    from extensions import scheduler
    from apscheduler.triggers.date import DateTrigger

    run_at = datetime.utcnow() + timedelta(seconds=delay_seconds)
    scheduler.add_job(
        verify_published_article_visibility,
        trigger=DateTrigger(run_date=utc_to_cst_dt(run_at)),
        id=f"verify_article_{article_id}",
        args=[article_id],
        replace_existing=True,
        max_instances=1,
        coalesce=True
    )
    log.info(f"[WPVerify] 已注册文章前台回查任务 article={article_id} run_at={utc_to_cst_str(run_at)}")


def verify_published_article_visibility(article_id):
    """
    检查已发布文章的前台 URL 是否可访问。
    只对 publish 模式的文章执行，草稿不检查。
    回查失败时发邮件通知运营和客户，但不改变文章状态。
    """
    from models import Article

    with _app_context():
        article = Article.query.get(article_id)
        if not article or article.status != "published":
            return

        client = article.client
        site = client.site if client else None
        if not site or normalize_publish_mode(site.publish_mode) != "publish":
            return

        ok, err = verify_wordpress_public_url(article.wp_url)
        if ok:
            log.info(f"[WPVerify] article={article.id} 前台回查通过")
            return

        log.warning(f"[WPVerify] article={article.id} 前台回查失败，保留 published 状态：{err}")

        if OPS_EMAIL:
            send_email(
                OPS_EMAIL,
                f"[WPVerify警告] 文章前台回查失败：{client.email if client else article.client_id}",
                f"<p>文章 {article.id}《{article.title}》发布后前台回查失败，文章状态已保留为 published，请人工确认。</p>"
                f"<p>失败原因：{err}</p>"
                f"<p>WordPress 文章链接：{article.wp_url}</p>"
            )
        if client:
            send_email(
                client.email,
                "BlogWriter 文章发布提醒",
                f"<p>你的文章《{article.title}》已发布到 WordPress，但系统自动检查时暂时无法访问前台链接。</p>"
                f"<p>这通常是 WordPress 缓存延迟导致，请稍后手动确认文章是否正常展示。</p>"
                f"<p>文章链接：<a href='{article.wp_url}'>{article.wp_url}</a></p>"
            )
