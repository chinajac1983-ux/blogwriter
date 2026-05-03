"""
Runify · BlogWriter — Flask 后端 4.1 (Decoupled Version)

本文件只负责：
- 创建 Flask app
- 初始化 db / CORS
- 注册 Blueprint
- 启动拆分后的各模块调度任务
"""

import os
import multiprocessing
import logging
from flask import Flask, jsonify
from flask_cors import CORS

from extensions import db, scheduler
from config import SQLALCHEMY_DATABASE_URI, SQLALCHEMY_TRACK_MODIFICATIONS

# 路由蓝图
from routes_client import client_bp
from routes_admin import admin_bp

# 调度任务 - 发布相关
from scheduler_jobs import (
    init_scheduler_app,
    load_existing_schedules,
    repair_publish_schedules,
)
# 调度任务 - 扫描相关 (4.1 拆分出的新文件)
from snapshot_scanner import (
    init_scanner_app,
    run_weekly_snapshot_scan,
)
# 调度任务 - 提醒与价格
from reminders import init_reminder_app, daily_reminder_check
from price_utils import init_default_prices
from db_maintenance import ensure_runtime_columns

log = logging.getLogger(__name__)

def create_app():
    app = Flask(__name__)

    # =========================
    # 基础配置
    # =========================
    app.config["SQLALCHEMY_DATABASE_URI"] = SQLALCHEMY_DATABASE_URI
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = SQLALCHEMY_TRACK_MODIFICATIONS

    db.init_app(app)
    CORS(app) # 确保处理跨域请求

    # =========================
    # 注册蓝图
    # =========================
    app.register_blueprint(client_bp)
    app.register_blueprint(admin_bp)

    # =========================
    # 健康检查
    # =========================
    @app.route("/health", methods=["GET"])
    def health():
        try:
            job_count = len(scheduler.get_jobs())
        except Exception:
            job_count = -1
        return jsonify({
            "status": "ok",
            "scheduler": "running" if scheduler.running else "stopped",
            "jobs_count": job_count
        })

    return app

def start_runtime(app):
    with app.app_context():
        # 1. 数据库维护与基础数据初始化
        db.create_all()
        ensure_runtime_columns()
        init_default_prices()

        # 2. 初始化各模块的 App Context (重要：确保各模块能正常访问数据库)
        init_scheduler_app(app)  # 发布逻辑模块
        init_scanner_app(app)    # 网页扫描模块 (拆分出的)
        init_reminder_app(app)   # 邮件提醒模块

        # 3. 启动调度器
        scheduler.start()

        # 4. 加载数据库中的现有发布计划
        load_existing_schedules()

        # ── 定时扫描任务 ──────────────────────────────────────────────────────
        
        # 修复发布计划 (每15分钟)
        scheduler.add_job(
            repair_publish_schedules,
            trigger="interval",
            minutes=15,
            id="repair_schedules",
            replace_existing=True,
        )

        # 每日提醒 (每天9点)
        scheduler.add_job(
            daily_reminder_check,
            trigger="cron",
            hour=9,
            minute=0,
            timezone="Asia/Shanghai",
            id="daily_reminder",
            replace_existing=True,
        )

        # 网页快照扫描 (每7天 - 从 snapshot_scanner 引入)
        scheduler.add_job(
            run_weekly_snapshot_scan,
            trigger="interval",
            hours=168,
            id="weekly_snapshot_scan",
            replace_existing=True,
        )

        # 行业新闻自研轨道扫描 (每7天)
        try:
            from industry_news import run_weekly_industry_news_scan
            scheduler.add_job(
                run_weekly_industry_news_scan,
                trigger="interval",
                hours=168,
                id="weekly_industry_news_scan",
                replace_existing=True,
            )
            log.info("[Startup] 已注册行业新闻自研轨道扫描任务（每7天）")
        except Exception as e:
            log.warning(f"[Startup] 行业新闻扫描任务注册失败：{e}")

        # Hermes 文件读取任务 (每6小时)
        try:
            from hermes_bridge import read_hermes_output_files
            scheduler.add_job(
                read_hermes_output_files,
                trigger="interval",
                hours=6,
                id="hermes_file_reader",
                replace_existing=True,
            )
            log.info("[Startup] 已注册 Hermes 文件读取任务（每6小时）")
        except Exception as e:
            log.warning(f"[Startup] Hermes 文件读取任务注册失败：{e}")

        log.info("[Startup] Scheduler 已启动并加载任务")


# =========================
# ⭐ Scheduler 启动控制
# =========================
START_SCHEDULER = os.environ.get("START_SCHEDULER", "true").lower() == "true"

# 防 gunicorn 多进程重复启动任务
if multiprocessing.current_process().name != "MainProcess":
    START_SCHEDULER = False

# 应用实例
app = create_app()

if START_SCHEDULER:
    start_runtime(app)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)