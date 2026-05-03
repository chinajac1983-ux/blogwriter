import os
from datetime import timezone
from zoneinfo import ZoneInfo

DATABASE_URL = os.environ.get("DATABASE_URL", "mysql+pymysql://runify:YOUR_DB_PASSWORD@localhost/blogwriter")

ENCRYPTION_KEY = os.environ.get("ENCRYPTION_KEY")
if not ENCRYPTION_KEY:
    raise RuntimeError("ENCRYPTION_KEY 未配置，拒绝启动。请在 .env 中配置固定 Fernet key。")
if len(ENCRYPTION_KEY) < 32:
    raise RuntimeError("ENCRYPTION_KEY 长度不足 32 位，拒绝启动。")

JWT_SECRET = os.environ.get("JWT_SECRET")
if not JWT_SECRET:
    raise RuntimeError("JWT_SECRET 未配置，拒绝启动。")
if len(JWT_SECRET) < 32:
    raise RuntimeError("JWT_SECRET 长度不足 32 位，拒绝启动。")
if ENCRYPTION_KEY == JWT_SECRET:
    raise RuntimeError("ENCRYPTION_KEY 和 JWT_SECRET 不能相同，存在安全风险。")

JWT_EXPIRE_DAYS = int(os.environ.get("JWT_EXPIRE_DAYS", "7"))

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
FROM_EMAIL = os.environ.get("FROM_EMAIL", "noreply@runify.xiaoheili.com")
FRONTEND_BASE_URL = os.environ.get("FRONTEND_BASE_URL", "https://runify.xiaoheili.com")
OPS_EMAIL = os.environ.get("OPS_EMAIL", "")

PAYMENT_NOTIFY_SECRET = os.environ.get("PAYMENT_NOTIFY_SECRET", "")
PAYMENT_NOTIFY_DEV_MODE = os.environ.get("PAYMENT_NOTIFY_DEV_MODE", "false").lower() == "true"

REGISTER_RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("REGISTER_RATE_LIMIT_WINDOW_SECONDS", "3600"))
REGISTER_RATE_LIMIT_MAX_PER_IP = int(os.environ.get("REGISTER_RATE_LIMIT_MAX_PER_IP", "20"))
REGISTER_RATE_LIMIT_MAX_PER_EMAIL = int(os.environ.get("REGISTER_RATE_LIMIT_MAX_PER_EMAIL", "5"))
LOGIN_RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("LOGIN_RATE_LIMIT_WINDOW_SECONDS", "3600"))
LOGIN_RATE_LIMIT_MAX_PER_IP = int(os.environ.get("LOGIN_RATE_LIMIT_MAX_PER_IP", "30"))
LOGIN_RATE_LIMIT_MAX_PER_EMAIL = int(os.environ.get("LOGIN_RATE_LIMIT_MAX_PER_EMAIL", "10"))

RESET_TOKEN_EXPIRE_MINUTES = 30
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
UTC_TZ = timezone.utc
DELIVERED_ARTICLE_STATUSES = ("draft", "published")
JITTER_MIN_HOURS = 3.0
JITTER_MAX_HOURS = 5.0

# app.py 通过 SQLAlchemy 配置需要这两个名字
SQLALCHEMY_DATABASE_URI = DATABASE_URL
SQLALCHEMY_TRACK_MODIFICATIONS = False

# ── 4.0：AI多平台轮询配置 ────────────────────────────────────────────────────
# 主力平台（硅基流动）
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.siliconflow.cn/v1")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "deepseek-ai/DeepSeek-V3")

# 备用平台：钱多多
QIANDUODUO_API_KEY = os.environ.get("QIANDUODUO_API_KEY", "")
QIANDUODUO_BASE_URL = os.environ.get("QIANDUODUO_BASE_URL", "https://api2.aigcbest.top/v1")
QIANDUODUO_MODEL = os.environ.get("QIANDUODUO_MODEL", "DeepSeek-V3")

# 兜底平台：OpenRouter
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "deepseek/deepseek-chat")

# ── 4.0：行业新闻抓取 API Keys ────────────────────────────────────────────────
# 以下全部可选，未配置则跳过对应数据源

# The Guardian（500次/天，免费非商业）
GUARDIAN_API_KEY = os.environ.get("GUARDIAN_API_KEY", "")

# NewsAPI（100次/天，免费）
NEWSAPI_KEY = os.environ.get("NEWSAPI_KEY", "")

# Tavily（1000次/月，兜底用）
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")

# Reddit（需要在 reddit.com/prefs/apps 注册应用获取）
REDDIT_CLIENT_ID = os.environ.get("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET", "")

# ── 4.0：Hermes集成 ──────────────────────────────────────────────────────────
# Hermes把行业信息写成JSON文件的目录，BlogWriter定时读取并导入
HERMES_OUTPUT_DIR = os.environ.get("HERMES_OUTPUT_DIR", "/tmp/hermes_news")

# ── 4.0：EEAT质检阈值 ────────────────────────────────────────────────────────
# 分数 >= EEAT_PASS_SCORE：直接发布
# 分数在 EEAT_REVIEW_SCORE 和 EEAT_PASS_SCORE 之间：重写一次
# 分数 < EEAT_REVIEW_SCORE 且重写后仍不达标：标记needs_review，发邮件通知
EEAT_PASS_SCORE = int(os.environ.get("EEAT_PASS_SCORE", "85"))
EEAT_REVIEW_SCORE = int(os.environ.get("EEAT_REVIEW_SCORE", "70"))
