from datetime import datetime

from extensions import db


class Client(db.Model):
    __tablename__ = "clients"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    plan = db.Column(db.String(20), default="")
    billing = db.Column(db.String(20), default="")
    articles_per_month = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=False)
    publishing_locked_until = db.Column(db.DateTime, nullable=True)
    consecutive_failures = db.Column(db.Integer, default=0)
    is_circuit_open = db.Column(db.Boolean, default=False)
    circuit_opened_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    articles_quota = db.Column(db.Integer, default=0)
    expires_at = db.Column(db.DateTime, nullable=True)
    cycle_started_at = db.Column(db.DateTime, nullable=True)

    site = db.relationship("Site", uselist=False, back_populates="client")
    articles = db.relationship("Article", back_populates="client")
    schedule = db.relationship("Schedule", uselist=False, back_populates="client")
    cycles = db.relationship("SubscriptionCycle", back_populates="client")


class Site(db.Model):
    __tablename__ = "sites"

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), unique=True)
    wp_url = db.Column(db.String(255), nullable=False)
    wp_username = db.Column(db.String(100), nullable=False)
    wp_app_password = db.Column(db.Text, nullable=False)
    topic_keywords = db.Column(db.Text, default="")
    article_style = db.Column(db.String(50), default="professional")
    article_length = db.Column(db.Integer, default=1500)

    brand_name = db.Column(db.String(255), default="")
    site_url = db.Column(db.String(255), default="")
    markets = db.Column(db.Text, default="")
    language = db.Column(db.String(50), default="English")
    product_desc = db.Column(db.Text, default="")
    target_customer = db.Column(db.Text, default="")
    customer_pain = db.Column(db.Text, default="")
    win_reason = db.Column(db.Text, default="")
    competitors = db.Column(db.Text, default="")
    references = db.Column(db.Text, default="")
    publish_mode = db.Column(db.String(20), default="draft")
    wp_test_passed_at = db.Column(db.DateTime, nullable=True)
    wp_test_post_id = db.Column(db.Integer, nullable=True)
    wp_test_signature = db.Column(db.String(64), default="")
    configured_at = db.Column(db.DateTime, default=datetime.utcnow)

    snapshot_scanned_at = db.Column(db.DateTime, nullable=True)
    snapshot_locked_until = db.Column(db.DateTime, nullable=True)

    # ── 4.0 新增：角度多样性控制 ─────────────────────────────────────────────
    # recent_angles: 最近10篇文章使用的角度，JSON数组，防止重复
    # 例：["cost reduction angle", "compliance angle", "supplier selection angle"]
    recent_angles = db.Column(db.Text, default="")
    # recent_topics: 最近10篇文章使用的话题，JSON数组
    recent_topics = db.Column(db.Text, default="")

    # ── 4.1 新增：关键词智能扩展 ─────────────────────────────────────────────
    # 用户填写（主力，准确）
    user_industries = db.Column(db.Text, nullable=True)
    # 例：["oil and gas", "chemical processing", "food industry"]
    user_buyer_types = db.Column(db.Text, nullable=True)
    # 例：["procurement managers", "plant engineers", "EPC contractors"]

    # AI推断（补充，扩展，每30天刷新）
    ai_search_keywords = db.Column(db.Text, nullable=True)
    # JSON结构存储所有维度的关键词：
    # {"product":[], "industry_backup":[], "buyer_backup":[],
    #  "application":[], "pain_points":[], "regulations":[], "market_events":[]}
    ai_keywords_generated_at = db.Column(db.DateTime, nullable=True)
    # AI关键词上次生成时间，用于判断是否需要刷新（超30天重新生成）

    # 搜索历史（防止8周内重复搜索相同关键词）
    search_keyword_history = db.Column(db.Text, nullable=True)
    # JSON数组，存最近8周用过的搜索词

    client = db.relationship("Client", back_populates="site")

class Schedule(db.Model):
    __tablename__ = "schedules"

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), unique=True)
    interval_hours = db.Column(db.Float, default=56.0)
    start_date = db.Column(db.DateTime, nullable=True)
    next_run_at = db.Column(db.DateTime, nullable=True)
    is_active = db.Column(db.Boolean, default=True)

    day_of_week = db.Column(db.String(20), default="")
    hour = db.Column(db.Integer, default=10)
    minute = db.Column(db.Integer, default=0)

    client = db.relationship("Client", back_populates="schedule")


class SubscriptionCycle(db.Model):
    __tablename__ = "subscription_cycles"

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False)
    order_id = db.Column(db.Integer, db.ForeignKey("payment_orders.id"), nullable=True, unique=True)

    plan = db.Column(db.String(20), nullable=False)
    billing = db.Column(db.String(20), nullable=False)
    weekly_articles = db.Column(db.Integer, nullable=False)
    quota = db.Column(db.Integer, nullable=False)

    status = db.Column(db.String(20), default="pending", index=True)
    started_at = db.Column(db.DateTime, nullable=True)
    expires_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    reminder_sent_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    client = db.relationship("Client", back_populates="cycles")
    order = db.relationship("PaymentOrder", back_populates="cycle")
    articles = db.relationship("Article", back_populates="cycle")


class Article(db.Model):
    __tablename__ = "articles"

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"))
    cycle_id = db.Column(db.Integer, db.ForeignKey("subscription_cycles.id"), nullable=True)

    title = db.Column(db.String(500), default="")
    wp_post_id = db.Column(db.Integer, nullable=True)
    wp_url = db.Column(db.String(500), default="")
    status = db.Column(db.String(20), default="pending")
    error_msg = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    published_at = db.Column(db.DateTime, nullable=True)
    word_count = db.Column(db.Integer, default=0)
    draft_reminder_sent_at = db.Column(db.DateTime, nullable=True)

    signal_ids_used = db.Column(db.Text, default="")
    snapshot_ids_used = db.Column(db.Text, default="")
    topic_used = db.Column(db.String(255), default="")
    angle_used = db.Column(db.String(255), default="")

    # ── 4.0 新增：EEAT质检 ───────────────────────────────────────────────────
    # quality_score: 0-100整数，NULL表示未质检
    quality_score = db.Column(db.Integer, nullable=True)
    # quality_notes: 各维度详情，JSON格式，含weakest_dimension和improvement_note
    quality_notes = db.Column(db.Text, nullable=True)
    # quality_rewrite_count: 重写次数，最多1次
    quality_rewrite_count = db.Column(db.Integer, default=0)

    # ── 4.0 新增：AB测试 ─────────────────────────────────────────────────────
    # info_source: 本篇文章使用的行业信息来源
    # "self"=自研轨道 / "hermes"=Hermes轨道 / "none"=无行业信息
    info_source = db.Column(db.String(20), default="none")

    # ── 4.0 新增：SEO字段 ────────────────────────────────────────────────────
    # seo_description: meta description，建议150-160字符
    seo_description = db.Column(db.String(300), default="")
    # seo_slug: WordPress URL slug，英文连字符格式，max 60字符
    seo_slug = db.Column(db.String(200), default="")
    # seo_focus_keyword: Yoast/RankMath的focus keyword，2-4个词
    seo_focus_keyword = db.Column(db.String(100), default="")

    client = db.relationship("Client", back_populates="articles")
    cycle = db.relationship("SubscriptionCycle", back_populates="articles")


class PaymentOrder(db.Model):
    __tablename__ = "payment_orders"

    id = db.Column(db.Integer, primary_key=True)
    order_no = db.Column(db.String(64), unique=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"))
    plan = db.Column(db.String(20))
    billing = db.Column(db.String(20))
    amount = db.Column(db.Integer)
    status = db.Column(db.String(20), default="pending")
    pay_method = db.Column(db.String(20))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    paid_at = db.Column(db.DateTime, nullable=True)

    config_reminder_3_sent_at = db.Column(db.DateTime, nullable=True)
    config_reminder_7_sent_at = db.Column(db.DateTime, nullable=True)

    # ── 3.6.1 新增：对账和退款字段 ──────────────────────────────────────────
    # trade_no：微信/支付宝那边的交易流水号，对账必须有
    trade_no = db.Column(db.String(64), nullable=True, default="")
    # refund_note：退款备注，管理员手动填写
    refund_note = db.Column(db.Text, nullable=True, default="")

    cycle = db.relationship("SubscriptionCycle", uselist=False, back_populates="order")


class PaymentConfig(db.Model):
    """支付渠道配置表。
    
    每个渠道（wechat / alipay）一条记录。
    config_json 和 cert_file 均用 Fernet 加密存储。
    换渠道或更新密钥只需在管理后台修改此表，不需要改代码或重启服务。

    config_json 解密后结构：
      微信: {"appid": "", "mch_id": "", "api_v3_key": ""}
      支付宝: {"app_id": "", "private_key": "", "alipay_public_key": ""}
    """
    __tablename__ = "payment_configs"

    id = db.Column(db.Integer, primary_key=True)

    # 渠道标识，目前支持 "wechat" / "alipay"，预留扩展
    provider = db.Column(db.String(30), nullable=False, unique=True)

    # 是否启用该渠道
    is_active = db.Column(db.Boolean, default=False)

    # 加密存储的 JSON 配置（appid、mch_id、api_key 等）
    config_json = db.Column(db.Text, nullable=True, default="")

    # 加密存储的证书文件内容（微信支付需要 apiclient_cert.pem）
    cert_file = db.Column(db.Text, nullable=True, default="")

    # 加密存储的证书私钥内容（微信支付需要 apiclient_key.pem）
    cert_key = db.Column(db.Text, nullable=True, default="")

    # 操作记录
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_by = db.Column(db.String(120), nullable=True, default="")  # 管理员邮箱


class PasswordReset(db.Model):
    __tablename__ = "password_resets"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), nullable=False, index=True)
    token = db.Column(db.String(64), unique=True, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Admin(db.Model):
    __tablename__ = "admins"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class RateLimitAttempt(db.Model):
    __tablename__ = "rate_limit_attempts"

    id = db.Column(db.Integer, primary_key=True)
    scope = db.Column(db.String(40), nullable=False, index=True)
    key = db.Column(db.String(255), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class PlanPrice(db.Model):
    __tablename__ = "plan_prices"

    id = db.Column(db.Integer, primary_key=True)
    plan = db.Column(db.String(20), nullable=False)
    billing = db.Column(db.String(20), nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    weekly_articles = db.Column(db.Integer, nullable=False)
    articles_quota = db.Column(db.Integer, nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)


class Signal(db.Model):
    __tablename__ = "signals"

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False, index=True)
    type = db.Column(db.String(50), nullable=False)
    content = db.Column(db.Text, nullable=False)
    weight = db.Column(db.Integer, default=2)
    is_active = db.Column(db.Boolean, default=True)
    used_count = db.Column(db.Integer, default=0)
    last_used_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    client = db.relationship("Client", backref="signals")


class WebsiteSnapshot(db.Model):
    __tablename__ = "website_snapshots"

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False, index=True)
    source_type = db.Column(db.String(30), default="")
    source_url = db.Column(db.String(500), default="")
    summary = db.Column(db.Text, default="")
    keywords = db.Column(db.Text, default="")
    topics = db.Column(db.Text, default="")
    tone = db.Column(db.String(100), default="")
    recent_changes = db.Column(db.Text, default="")
    scanned_at = db.Column(db.DateTime, default=datetime.utcnow)

    raw_hash = db.Column(db.String(64), default="")

    client = db.relationship("Client", backref="snapshots")


# ── 4.0 新增：行业动态表 ──────────────────────────────────────────────────────

class IndustryNews(db.Model):
    """行业动态表。

    双轨制信息获取的统一存储：
    - 自研轨道（Google RSS / Bing RSS / Guardian / NewsAPI / Tavily）→ source="self"
    - Hermes轨道（Felo Search / Blogwatcher）→ source="hermes"

    文章生成时优先取 hermes，其次取 self，两者都没有则走常青文章逻辑。
    记录60天后自动过期清理。
    """
    __tablename__ = "industry_news"

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False, index=True)

    # 来源轨道
    source = db.Column(db.String(20), default="self", index=True)
    # "self"=自研 / "hermes"=Hermes

    # 来源API
    source_api = db.Column(db.String(30), default="")
    # "google_rss" / "bing_rss" / "guardian" / "newsapi" / "tavily"
    # "hermes_felo" / "hermes_blogwatcher"

    # 内容
    title = db.Column(db.String(500), nullable=False)
    summary = db.Column(db.Text, default="")
    full_text = db.Column(db.Text, nullable=True)
    # full_text: requests抓取的全文，有则注入prompt时可引用具体信息
    url = db.Column(db.String(500), default="")

    # 元数据
    published_at = db.Column(db.DateTime, nullable=True)
    language = db.Column(db.String(20), default="en")
    keywords_matched = db.Column(db.Text, default="")
    # keywords_matched: JSON数组，记录匹配到的关键词
    has_full_text = db.Column(db.Boolean, default=False)
    quality_score = db.Column(db.Integer, default=0)
    # quality_score: 规则打分0-10，Guardian=高分，RSS=低分，有全文=加分

    # 使用记录
    used_count = db.Column(db.Integer, default=0)
    last_used_at = db.Column(db.DateTime, nullable=True)

    # 生命周期
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=True)
    # expires_at = created_at + 60天，启动时自动清理过期记录

    client = db.relationship("Client", backref="industry_news")


class RevokedToken(db.Model):
    __tablename__ = "revoked_tokens"
    id = db.Column(db.Integer, primary_key=True)
    jti = db.Column(db.String(64), unique=True, nullable=False, index=True)
    expires_at = db.Column(db.DateTime, nullable=False)
    revoked_at = db.Column(db.DateTime, default=datetime.utcnow)
