from sqlalchemy import text
import logging

from extensions import db

log = logging.getLogger(__name__)


def ensure_runtime_columns():
    """轻量自动补列/补索引，避免从旧版本升级时因为缺字段或缺约束导致运行风险。"""
    try:
        result = db.session.execute(text("SHOW COLUMNS FROM clients"))
        existing = {row[0] for row in result}
        alters = []
        if "publishing_locked_until" not in existing:
            alters.append("ADD COLUMN publishing_locked_until DATETIME NULL")
        if "circuit_opened_at" not in existing:
            alters.append("ADD COLUMN circuit_opened_at DATETIME NULL")
        # 3.5：密码哈希从 64 位 SHA256 升级为 werkzeug PBKDF2，字段长度必须放大。
        alters.append("MODIFY COLUMN password_hash VARCHAR(255) NOT NULL")
        for alter in alters:
            db.session.execute(text(f"ALTER TABLE clients {alter}"))
        if alters:
            db.session.commit()
            log.info(f"[Startup] 已自动补齐 clients 表字段：{', '.join(alters)}")
    except Exception as e:
        db.session.rollback()
        log.warning(f"[Startup] 自动补列跳过或失败：{e}")


    try:
        db.session.execute(text("ALTER TABLE admins MODIFY COLUMN password_hash VARCHAR(255) NOT NULL"))
        db.session.commit()
        log.info("[Startup] 已确认 admins.password_hash 字段长度兼容新版密码哈希")
    except Exception as e:
        db.session.rollback()
        log.debug(f"[Startup] admins.password_hash 字段长度检查跳过或已兼容：{e}")

    # 3.3：为 subscription_cycles.order_id 补唯一索引，防止并发支付回调重复生成周期。
    # 注意：如果旧数据里已经存在重复 order_id，索引创建会失败，需要先人工清理重复数据。
    try:
        db.session.execute(text(
            "ALTER TABLE subscription_cycles "
            "ADD UNIQUE INDEX uq_cycle_order_id (order_id)"
        ))
        db.session.commit()
        log.info("[Startup] 已为 subscription_cycles.order_id 增加唯一索引 uq_cycle_order_id")
    except Exception as e:
        db.session.rollback()
        err = str(e)
        if "Duplicate key name" in err or "already exists" in err or "1061" in err:
            log.debug("[Startup] subscription_cycles.order_id 唯一索引 uq_cycle_order_id 已存在，跳过")
        else:
            log.warning(f"[Startup] subscription_cycles.order_id 唯一索引暂未创建，请检查是否存在重复 order_id：{e}")

    # 3.6.1：payment_orders 补 trade_no（第三方交易流水号）和 refund_note（退款备注）字段。
    try:
        result = db.session.execute(text("SHOW COLUMNS FROM payment_orders"))
        existing = {row[0] for row in result}
        alters = []
        if "trade_no" not in existing:
            alters.append("ADD COLUMN trade_no VARCHAR(64) NULL DEFAULT ''")
        if "refund_note" not in existing:
            alters.append("ADD COLUMN refund_note TEXT NULL")
        for alter in alters:
            db.session.execute(text(f"ALTER TABLE payment_orders {alter}"))
        if alters:
            db.session.commit()
            log.info(f"[Startup] 已自动补齐 payment_orders 表字段：{', '.join(alters)}")
    except Exception as e:
        db.session.rollback()
        log.warning(f"[Startup] payment_orders 补列跳过或失败：{e}")

    # 3.6.2：payment_configs 补 cert_key（微信支付 API 证书私钥）字段。
    try:
        result = db.session.execute(text("SHOW COLUMNS FROM payment_configs"))
        existing = {row[0] for row in result}
        if "cert_key" not in existing:
            db.session.execute(text("ALTER TABLE payment_configs ADD COLUMN cert_key TEXT NULL"))
            db.session.commit()
            log.info("[Startup] 已自动补齐 payment_configs.cert_key 字段")
    except Exception as e:
        db.session.rollback()
        log.warning(f"[Startup] payment_configs 补列跳过或失败：{e}")

    # ── 4.0：articles 表新增质检、AB测试、SEO字段 ────────────────────────────
    try:
        result = db.session.execute(text("SHOW COLUMNS FROM articles"))
        existing = {row[0] for row in result}
        alters = []
        if "quality_score" not in existing:
            alters.append("ADD COLUMN quality_score INT NULL")
        if "quality_notes" not in existing:
            alters.append("ADD COLUMN quality_notes TEXT NULL")
        if "quality_rewrite_count" not in existing:
            alters.append("ADD COLUMN quality_rewrite_count INT NOT NULL DEFAULT 0")
        if "info_source" not in existing:
            alters.append("ADD COLUMN info_source VARCHAR(20) NOT NULL DEFAULT 'none'")
        if "seo_description" not in existing:
            alters.append("ADD COLUMN seo_description VARCHAR(300) NOT NULL DEFAULT ''")
        if "seo_slug" not in existing:
            alters.append("ADD COLUMN seo_slug VARCHAR(200) NOT NULL DEFAULT ''")
        if "seo_focus_keyword" not in existing:
            alters.append("ADD COLUMN seo_focus_keyword VARCHAR(100) NOT NULL DEFAULT ''")
        for alter in alters:
            db.session.execute(text(f"ALTER TABLE articles {alter}"))
        if alters:
            db.session.commit()
            log.info(f"[Startup] 已自动补齐 articles 表字段：{', '.join(alters)}")
    except Exception as e:
        db.session.rollback()
        log.warning(f"[Startup] articles 补列跳过或失败：{e}")

    # ── 4.0：sites 表新增角度多样性字段 ─────────────────────────────────────
    try:
        result = db.session.execute(text("SHOW COLUMNS FROM sites"))
        existing = {row[0] for row in result}
        alters = []
        if "recent_angles" not in existing:
            alters.append("ADD COLUMN recent_angles TEXT NOT NULL DEFAULT ''")
        if "recent_topics" not in existing:
            alters.append("ADD COLUMN recent_topics TEXT NOT NULL DEFAULT ''")
        for alter in alters:
            db.session.execute(text(f"ALTER TABLE sites {alter}"))
        if alters:
            db.session.commit()
            log.info(f"[Startup] 已自动补齐 sites 表字段：{', '.join(alters)}")
    except Exception as e:
        db.session.rollback()
        log.warning(f"[Startup] sites 补列跳过或失败：{e}")

    # ── 4.0：新建 industry_news 表（如果不存在）────────────────────────────
    try:
        db.session.execute(text("SELECT 1 FROM industry_news LIMIT 1"))
        log.debug("[Startup] industry_news 表已存在，跳过建表")
    except Exception:
        db.session.rollback()
        try:
            db.session.execute(text("""
                CREATE TABLE IF NOT EXISTS industry_news (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    client_id INT NOT NULL,
                    source VARCHAR(20) NOT NULL DEFAULT 'self',
                    source_api VARCHAR(30) NOT NULL DEFAULT '',
                    title VARCHAR(500) NOT NULL,
                    summary TEXT,
                    full_text LONGTEXT,
                    url VARCHAR(500) NOT NULL DEFAULT '',
                    published_at DATETIME NULL,
                    language VARCHAR(20) NOT NULL DEFAULT 'en',
                    keywords_matched TEXT NOT NULL DEFAULT '',
                    has_full_text TINYINT(1) NOT NULL DEFAULT 0,
                    quality_score INT NOT NULL DEFAULT 0,
                    used_count INT NOT NULL DEFAULT 0,
                    last_used_at DATETIME NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    expires_at DATETIME NULL,
                    INDEX idx_industry_news_client (client_id),
                    INDEX idx_industry_news_source (source),
                    INDEX idx_industry_news_expires (expires_at),
                    FOREIGN KEY (client_id) REFERENCES clients(id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """))
            db.session.commit()
            log.info("[Startup] 已创建 industry_news 表")
        except Exception as e:
            db.session.rollback()
            log.warning(f"[Startup] industry_news 建表失败：{e}")

    # ── 4.0：清理过期 industry_news 记录 ────────────────────────────────────
    try:
        result = db.session.execute(text(
            "DELETE FROM industry_news WHERE expires_at IS NOT NULL AND expires_at < NOW()"
        ))
        db.session.commit()
        if result.rowcount:
            log.info(f"[Startup] 已清理 {result.rowcount} 条过期 industry_news 记录")
    except Exception as e:
        db.session.rollback()
        log.debug(f"[Startup] industry_news 过期清理跳过：{e}")

    # ── 4.1 新增：sites 表关键词智能扩展字段 ────────────────────────────────
    try:
        result = db.session.execute(text("SHOW COLUMNS FROM sites"))
        existing = {row[0] for row in result}
        alters = []
        if "user_industries" not in existing:
            alters.append("ADD COLUMN user_industries TEXT NULL")
        if "user_buyer_types" not in existing:
            alters.append("ADD COLUMN user_buyer_types TEXT NULL")
        if "ai_search_keywords" not in existing:
            alters.append("ADD COLUMN ai_search_keywords TEXT NULL")
        if "ai_keywords_generated_at" not in existing:
            alters.append("ADD COLUMN ai_keywords_generated_at DATETIME NULL")
        if "search_keyword_history" not in existing:
            alters.append("ADD COLUMN search_keyword_history TEXT NULL")
        for alter in alters:
            db.session.execute(text(f"ALTER TABLE sites {alter}"))
        if alters:
            db.session.commit()
            log.info(f"[Startup] 已自动补齐 sites 关键词扩展字段：{', '.join(alters)}")
    except Exception as e:
        db.session.rollback()
        log.warning(f"[Startup] sites 关键词扩展字段补列失败：{e}")
