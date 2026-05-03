import logging

import resend
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import create_engine
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from cryptography.fernet import Fernet

from config import DATABASE_URL, ENCRYPTION_KEY, RESEND_API_KEY, SHANGHAI_TZ

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("blogwriter.log"), logging.StreamHandler()]
)
log = logging.getLogger(__name__)

db = SQLAlchemy()

try:
    fernet = Fernet(ENCRYPTION_KEY.encode() if isinstance(ENCRYPTION_KEY, str) else ENCRYPTION_KEY)
except Exception as e:
    raise RuntimeError(f"ENCRYPTION_KEY 格式不合法，必须是有效的 Fernet key：{e}")

resend.api_key = RESEND_API_KEY

jobstore_engine = create_engine(
    DATABASE_URL,
    pool_size=3,
    max_overflow=5,
    pool_pre_ping=True,
    pool_recycle=1800,
    echo=False,
)

scheduler = BackgroundScheduler(
    jobstores={
        "default": SQLAlchemyJobStore(engine=jobstore_engine)
    },
    timezone=SHANGHAI_TZ
)
