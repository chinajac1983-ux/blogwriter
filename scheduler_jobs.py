"""
Runify · BlogWriter — Scheduler Jobs

职责：只负责文章发布调度相关逻辑。

包含：
- 发布锁管理
- 调度任务注册/恢复/自愈
- 主发布任务 run_publish_job

不包含（已拆分到独立文件）：
- Snapshot 爬取 → snapshot_scanner.py
- 文章前台回查 → article_verifier.py
- 行业新闻抓取 → industry_news.py
- Hermes 数据接收 → hermes_bridge.py
- 文章生成 → article_generator.py
- 质检逻辑 → quality_checker.py
"""

import random
import json
import threading
import logging
from datetime import datetime, timedelta

from apscheduler.triggers.date import DateTrigger
from sqlalchemy import or_

from article_generator import generate_article
from config import FRONTEND_BASE_URL, OPS_EMAIL
from extensions import db, scheduler
from models import Client, Article, Schedule, WebsiteSnapshot, Signal, IndustryNews
from crypto_utils import decrypt
from cycle_utils import (
    published_in_cycle, get_active_cycle, get_next_pending_cycle,
    sync_client_legacy_cycle_fields, billing_days, cycle_is_fulfilled
)
from mail_utils import send_email
from site_utils import site_wp_test_is_current
from time_utils import utc_to_cst_dt, utc_to_cst_str, calculate_next_run_at, get_interval_hours, estimate_word_count
from wp_utils import publish_to_wordpress, verify_wordpress_post_rest
from article_verifier import schedule_publish_visibility_check

log = logging.getLogger(__name__)

_app = None


def init_scheduler_app(app):
    global _app
    _app = app
    # 同步初始化子模块
    from article_verifier import init_verifier_app
    from snapshot_scanner import init_scanner_app
    init_verifier_app(app)
    init_scanner_app(app)


def _app_context():
    if _app is None:
        raise RuntimeError('Scheduler app 未初始化')
    return _app.app_context()


# ══════════════════════════════════════════════════════════════════
# 发布锁
# ══════════════════════════════════════════════════════════════════

def scheduler_job_exists(client_id):
    return scheduler.get_job(f"publish_{client_id}") is not None


def publish_lock_is_active(client, now=None):
    if not client or not client.publishing_locked_until:
        return False
    now = now or datetime.utcnow()
    return client.publishing_locked_until > now


def acquire_publish_lock(client_id, lock_minutes=60):
    now = datetime.utcnow()
    locked_until = now + timedelta(minutes=lock_minutes)
    updated = Client.query.filter(
        Client.id == client_id,
        or_(
            Client.publishing_locked_until == None,
            Client.publishing_locked_until <= now
        )
    ).update({"publishing_locked_until": locked_until}, synchronize_session=False)
    db.session.commit()
    return updated > 0, locked_until


def release_publish_lock(client_id):
    Client.query.filter_by(id=client_id).update(
        {"publishing_locked_until": None}, synchronize_session=False
    )
    db.session.commit()


# ══════════════════════════════════════════════════════════════════
# 调度注册
# ══════════════════════════════════════════════════════════════════

def register_schedule(client_id, interval_hours, run_at):
    """注册一次性 DateTrigger。每次执行完成后再计算下一次发布时间。"""
    job_id = f"publish_{client_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    scheduler.add_job(
        run_publish_job,
        trigger=DateTrigger(run_date=utc_to_cst_dt(run_at)),
        id=job_id,
        args=[client_id],
        replace_existing=True,
        max_instances=1,
        coalesce=True
    )
    log.info(f"[Scheduler] 注册 {job_id}：下一次 {utc_to_cst_str(run_at)} 北京时间尝试发布")


def schedule_next_publish(client):
    """根据当前客户状态，计算并注册下一次发布时间。"""
    if not client or not client.is_active or client.is_circuit_open or not client.site:
        return
    cycle = get_active_cycle(client.id) or get_next_pending_cycle(client.id)
    if not cycle:
        return
    if cycle.status == "active" and published_in_cycle(cycle.id) >= int(cycle.quota or 0):
        return
    sched = client.schedule or Schedule(client_id=client.id)
    interval_hours = sched.interval_hours or get_interval_hours(cycle.plan)
    next_run_at = calculate_next_run_at(interval_hours)
    sched.interval_hours = interval_hours
    sched.next_run_at = next_run_at
    sched.is_active = True
    db.session.add(sched)
    db.session.commit()
    register_schedule(client.id, interval_hours, next_run_at)


def load_existing_schedules():
    """启动时恢复数据库中已有的调度任务，并清理过期发布锁。"""
    with _app_context():
        expired_lock_count = Client.query.filter(
            Client.publishing_locked_until != None,
            Client.publishing_locked_until <= datetime.utcnow()
        ).update({"publishing_locked_until": None}, synchronize_session=False)
        if expired_lock_count:
            log.warning(f"[Startup] 已清理 {expired_lock_count} 个过期发布锁")
        db.session.commit()

        schedules = Schedule.query.filter_by(is_active=True).all()
        count = 0
        for s in schedules:
            run_at = s.next_run_at or s.start_date
            if s.client and s.client.is_active and run_at:
                if run_at <= datetime.utcnow():
                    run_at = datetime.utcnow() + timedelta(minutes=random.randint(10, 120))
                    s.next_run_at = run_at
                    db.session.add(s)
                    db.session.commit()
                register_schedule(s.client_id, s.interval_hours, run_at)
                count += 1
        log.info(f"[Startup] 已阶梯式恢复 {count} 个调度任务")


def repair_publish_schedules():
    """调度心跳自愈：防止 DateTrigger 未成功续约导致客户永久停更。"""
    with _app_context():
        now = datetime.utcnow()
        repaired = 0
        clients = Client.query.filter_by(is_active=True).all()
        for client in clients:
            if not client.site or client.is_circuit_open or publish_lock_is_active(client) or not site_wp_test_is_current(client.site):
                continue
            cycle = get_active_cycle(client.id) or get_next_pending_cycle(client.id)
            if not cycle or cycle_is_fulfilled(cycle):
                if cycle and cycle.status == "active":
                    cycle.status = "completed"
                    cycle.completed_at = cycle.completed_at or now
                    sync_client_legacy_cycle_fields(client, cycle)
                continue

            sched = client.schedule or Schedule(client_id=client.id)
            run_at = sched.next_run_at or sched.start_date
            job_missing = not scheduler_job_exists(client.id)
            run_missing = run_at is None
            run_stale = bool(run_at and run_at <= now - timedelta(minutes=30))

            if job_missing or run_missing or run_stale:
                interval_hours = sched.interval_hours or get_interval_hours(cycle.plan)
                next_run_at = now + timedelta(minutes=random.randint(10, 120))
                sched.interval_hours = interval_hours
                sched.next_run_at = next_run_at
                sched.is_active = True
                db.session.add(sched)
                db.session.commit()
                register_schedule(client.id, interval_hours, next_run_at)
                repaired += 1

        db.session.commit()
        if repaired:
            log.warning(f"[SchedulerRepair] 已自愈恢复 {repaired} 个发布任务")


# ══════════════════════════════════════════════════════════════════
# 主发布任务
# ══════════════════════════════════════════════════════════════════

def run_publish_job(client_id):
    lock_acquired = False
    should_schedule_next = False

    with _app_context():
        client = Client.query.get(client_id)
        if not client:
            return {"success": False, "reason": "客户不存在"}

        now = datetime.utcnow()
        if publish_lock_is_active(client, now):
            return {"success": False, "reason": "已有发布任务正在执行"}
        if not client.is_active:
            return {"success": False, "reason": "客户未激活"}
        if not client.site:
            return {"success": False, "reason": "客户未配置站点"}
        if client.is_circuit_open:
            return {"success": False, "reason": "客户发布已熔断，请先通过 WordPress 连接测试以证明配置已修复"}
        if not site_wp_test_is_current(client.site):
            return {"success": False, "reason": "WordPress 测试发布未通过或配置已变更，请先在设置页完成测试发布"}

        cycle = get_active_cycle(client.id) or get_next_pending_cycle(client.id)
        if not cycle:
            return {"success": False, "reason": "无可用服务周期"}

        current_published = published_in_cycle(cycle.id)
        if current_published >= int(cycle.quota or 0):
            should_schedule_next = False
            if cycle.status == "active":
                cycle.status = "completed"
                cycle.completed_at = datetime.utcnow()
                sync_client_legacy_cycle_fields(client, cycle)
                db.session.commit()
            return {"success": False, "reason": "当前周期已完成"}

        lock_acquired, locked_until = acquire_publish_lock(client.id, lock_minutes=60)
        if not lock_acquired:
            return {"success": False, "reason": "已有发布任务正在执行"}
        client = Client.query.get(client_id)
        log.info(f"[Lock] client={client.id} 已获得发布锁，过期时间={utc_to_cst_str(locked_until)}")

        article = Article(client_id=client.id, cycle_id=cycle.id, status="pending")
        db.session.add(article)
        db.session.commit()

        try:
            # ── 取 Signal ────────────────────────────────────────────────────
            signals = (
                db.session.query(Signal)
                .filter_by(client_id=client.id, is_active=True)
                .order_by(Signal.weight.desc(), Signal.used_count.asc(), Signal.created_at.desc())
                .limit(2)
                .all()
            )

            # ── 取 Snapshot ──────────────────────────────────────────────────
            snapshots = (
                db.session.query(WebsiteSnapshot)
                .filter_by(client_id=client.id)
                .order_by(WebsiteSnapshot.scanned_at.desc())
                .limit(3)
                .all()
            )

            # ── 取历史文章（防角度重复）──────────────────────────────────────
            history = (
                db.session.query(Article)
                .filter_by(client_id=client.id)
                .order_by(Article.created_at.desc())
                .limit(8)
                .all()
            )

            # ── 取行业动态（优先 hermes，其次 self）──────────────────────────
            industry_news = (
                db.session.query(IndustryNews)
                .filter_by(client_id=client.id, source="hermes")
                .filter(IndustryNews.expires_at > datetime.utcnow())
                .order_by(IndustryNews.quality_score.desc(), IndustryNews.created_at.desc())
                .limit(3)
                .all()
            )
            if not industry_news:
                industry_news = (
                    db.session.query(IndustryNews)
                    .filter_by(client_id=client.id, source="self")
                    .filter(IndustryNews.expires_at > datetime.utcnow())
                    .order_by(IndustryNews.quality_score.desc(), IndustryNews.created_at.desc())
                    .limit(3)
                    .all()
                )

            # ── 生成文章（含质检和重写，最多3次）────────────────────────────
            result = generate_article(
                client.site,
                signals=signals,
                snapshots=snapshots,
                history=history,
                industry_news=industry_news,
            )

            # ── 发布到 WordPress ─────────────────────────────────────────────
            app_pwd = decrypt(client.site.wp_app_password)
            wp_result = publish_to_wordpress(
                client.site.wp_url,
                client.site.wp_username,
                app_pwd,
                result["title"],
                result["content"],
                client.site.publish_mode,
                seo_description=result.get("seo_description", ""),
                seo_slug=result.get("seo_slug", ""),
                seo_focus_keyword=result.get("seo_focus_keyword", ""),
            )

            verify_ok, verify_err = verify_wordpress_post_rest(
                client.site.wp_url,
                client.site.wp_username,
                app_pwd,
                wp_result["id"],
                expected_status=wp_result["mode"]
            )
            if not verify_ok:
                raise ValueError(verify_err)

            # ── 写入 Article 字段 ────────────────────────────────────────────
            article.title = result["title"]
            article.wp_post_id = wp_result["id"]
            article.wp_url = wp_result["link"]
            article.word_count = estimate_word_count(result["content"], client.site.language)
            article.status = "published" if wp_result["mode"] == "publish" else "draft"
            article.published_at = datetime.utcnow()
            article.signal_ids_used = json.dumps(result.get("signals_used", []), ensure_ascii=False)
            article.snapshot_ids_used = json.dumps(result.get("snapshots_used", []), ensure_ascii=False)
            article.topic_used = result.get("topic", "") or ""
            article.angle_used = result.get("angle", "") or ""
            article.quality_score = result.get("quality_score")
            article.quality_notes = json.dumps(result.get("quality_notes", {}), ensure_ascii=False)
            article.quality_rewrite_count = result.get("quality_rewrite_count", 0)
            article.info_source = result.get("info_source", "none")
            article.seo_description = result.get("seo_description", "")
            article.seo_slug = result.get("seo_slug", "")
            article.seo_focus_keyword = result.get("seo_focus_keyword", "")

            # ── 更新角度轮换记录 ─────────────────────────────────────────────
            try:
                recent_angles = json.loads(client.site.recent_angles or "[]")
                recent_angles.append(result.get("angle", ""))
                client.site.recent_angles = json.dumps(recent_angles[-10:], ensure_ascii=False)
                recent_topics = json.loads(client.site.recent_topics or "[]")
                recent_topics.append(result.get("topic", ""))
                client.site.recent_topics = json.dumps(recent_topics[-10:], ensure_ascii=False)
            except Exception as e:
                log.warning(f"[Job] 角度记录更新失败：{e}")

            # ── 更新 IndustryNews 使用记录 ───────────────────────────────────
            if industry_news:
                news_ids = [n.id for n in industry_news]
                IndustryNews.query.filter(IndustryNews.id.in_(news_ids)).update(
                    {"used_count": IndustryNews.used_count + 1, "last_used_at": article.published_at},
                    synchronize_session=False,
                )

            # ── 更新 Signal 使用记录 ─────────────────────────────────────────
            if result.get("signals_used"):
                Signal.query.filter(Signal.id.in_(result.get("signals_used", []))).update(
                    {"used_count": Signal.used_count + 1, "last_used_at": article.published_at},
                    synchronize_session=False
                )

            # ── 更新周期状态 ─────────────────────────────────────────────────
            if cycle.status == "pending":
                now = article.published_at
                cycle.status = "active"
                cycle.started_at = now
                cycle.expires_at = now + timedelta(days=billing_days(cycle.billing))

            new_count = current_published + 1
            if new_count >= int(cycle.quota or 0):
                cycle.status = "completed"
                cycle.completed_at = article.published_at

            sync_client_legacy_cycle_fields(client, cycle)
            client.consecutive_failures = 0
            client.is_circuit_open = False
            client.circuit_opened_at = None
            db.session.commit()

            # ── 前台可见性回查 ────────────────────────────────────────────────
            if article.status == "published":
                schedule_publish_visibility_check(article.id, delay_seconds=60)

            log.info(
                f"[Job] client={client.id} cycle={cycle.id} 发布成功：{article.title} "
                f"quality={result.get('quality_score')} "
                f"label={result.get('quality_label')} "
                f"source={result.get('info_source')}"
            )

            should_schedule_next = True

            return {
                "success": True,
                "article_id": article.id,
                "wp_post_id": article.wp_post_id,
                "publish_mode": article.status,
                "quality_score": result.get("quality_score"),
                "quality_label": result.get("quality_label"),
                "info_source": result.get("info_source"),
            }

        except Exception as e:
            article.status = "failed"
            article.error_msg = str(e)[:5000]
            client.consecutive_failures = int(client.consecutive_failures or 0) + 1

            if client.consecutive_failures >= 5:
                client.is_circuit_open = True
                client.circuit_opened_at = datetime.utcnow()
                log.error(f"[Circuit] client={client.id} 连续失败 {client.consecutive_failures} 次，已熔断")
                send_email(
                    client.email,
                    "BlogWriter 自动发布已暂停，请检查 WordPress 配置",
                    f"<p>你的 BlogWriter 自动发布连续失败 {client.consecutive_failures} 次，系统已暂时停止继续发布，避免重复失败。</p>"
                    f"<p>最近失败原因：{str(e)[:800]}</p>"
                    f"<p>请登录后台重新进行 WordPress 连接测试。测试通过后，系统才允许恢复发布。</p>"
                    f"<p><a href='{FRONTEND_BASE_URL}/setup'>去重新测试配置</a></p>"
                )
                if OPS_EMAIL:
                    send_email(
                        OPS_EMAIL,
                        f"BlogWriter 客户发布熔断：{client.email}",
                        f"<p>客户 {client.email} 连续发布失败 {client.consecutive_failures} 次，已熔断。</p>"
                        f"<p>错误：{str(e)[:1000]}</p>"
                    )

            db.session.commit()
            log.error(f"[Job] client={client.id} 发布失败：{e}")

            if not client.is_circuit_open:
                should_schedule_next = True

            return {"success": False, "reason": str(e)}

        finally:
            if lock_acquired:
                release_publish_lock(client_id)

            if should_schedule_next:
                try:
                    client = Client.query.get(client_id)
                    if client:
                        schedule_next_publish(client)
                except Exception as e:
                    log.error(f"[Scheduler] 调度下一次失败：{e}")
