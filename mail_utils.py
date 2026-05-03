import resend

from config import RESEND_API_KEY, FROM_EMAIL
import logging

log = logging.getLogger(__name__)


def send_email(to, subject, html):
    if not RESEND_API_KEY:
        log.warning("[Email] RESEND_API_KEY 未配置，跳过发送")
        return
    try:
        resend.Emails.send({"from": FROM_EMAIL, "to": [to], "subject": subject, "html": html})
        log.info(f"[Email] 已发送至 {to}：{subject}")
    except Exception as e:
        log.error(f"[Email] 发送失败 {to}：{e}")
