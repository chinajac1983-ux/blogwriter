"""
Runify · BlogWriter — Snapshot Scanner

职责：
- 抓取客户的自有网站（own_site）、竞品、参考网站
- 生成 WebsiteSnapshot 记录
- 每周定时扫描所有 active 客户
- 异步触发首次扫描（不阻塞请求）
- 首次/每月刷新 AI 关键词

不负责：
- 行业新闻抓取（见 industry_news.py）
- 文章生成（见 article_generator.py）
- 发布（见 scheduler_jobs.py）
"""

import re
import json
import hashlib
import threading
import time
import logging
from datetime import datetime, timedelta

import requests

from extensions import db
from models import WebsiteSnapshot, Client

log = logging.getLogger(__name__)

_app = None

SNAPSHOT_MAX_CONCURRENCY = 2
SNAPSHOT_TIMEOUT = 15
SNAPSHOT_MAX_RETRY = 2
SNAPSHOT_SEMAPHORE = threading.Semaphore(SNAPSHOT_MAX_CONCURRENCY)


def init_scanner_app(app):
    global _app
    _app = app


def _app_context():
    if _app is None:
        raise RuntimeError('Scanner app 未初始化')
    return _app.app_context()


# ══════════════════════════════════════════════════════════════════
# 网页抓取
# ══════════════════════════════════════════════════════════════════

def _snapshot_raw_hash(text):
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def fetch_url_raw_text(url):
    """抓取网页原文并返回 (raw_text, raw_hash)。只做网页抓取，不调用 AI。"""
    last_error = None
    for attempt in range(SNAPSHOT_MAX_RETRY + 1):
        try:
            resp = requests.get(url, timeout=SNAPSHOT_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            raw_text = resp.text[:8000]
            return raw_text, _snapshot_raw_hash(raw_text)
        except Exception as e:
            last_error = e
            if attempt < SNAPSHOT_MAX_RETRY:
                time.sleep(2)
    log.warning(f"[Snapshot] 网页抓取失败 {url}：{last_error}")
    return None, ""


# ══════════════════════════════════════════════════════════════════
# AI 解析
# ══════════════════════════════════════════════════════════════════

def parse_snapshot_from_text(url, raw_text):
    """用 AI 从网页原文中提取 summary/keywords/topics/tone/recent_changes。"""
    from ai_router import call_ai_text

    prompt = f"""
Analyze the following webpage content and extract key information.
Return ONLY a JSON object with these fields:
- summary: one paragraph summary of what this site/page is about (max 200 words)
- keywords: list of 5-10 important industry keywords (array of strings)
- topics: list of 3-5 main content topics covered (array of strings)
- tone: writing style description (e.g. "professional", "technical", "educational", "conversational")
- recent_changes: any notable recent content or announcements visible (string, can be empty)

Webpage content:
{raw_text}
"""
    last_error = None
    for attempt in range(SNAPSHOT_MAX_RETRY + 1):
        try:
            raw = call_ai_text([{"role": "user", "content": prompt}], temperature=0.3)
            raw = raw.strip()
            if raw.startswith("```"):
                raw = re.sub(r"^```[a-zA-Z]*", "", raw).rstrip("`").strip()
            match = re.search(r"\{.*\}", raw, re.S)
            if match:
                return json.loads(match.group(0))
            last_error = "AI 返回内容不是 JSON 对象"
        except Exception as e:
            last_error = e
            if attempt < SNAPSHOT_MAX_RETRY:
                time.sleep(2)
    log.warning(f"[Snapshot] AI 解析失败 {url}：{last_error}")
    return None


def parse_own_site_from_text(url, raw_text):
    """
    专门用于解析客户自己公司网站的 AI prompt。
    目标：提取公司画像信息（成立时间、案例、项目、认证、市场活动等）。
    和 parse_snapshot_from_text 不同，这里更关注公司自身信息而非行业语言。
    """
    from ai_router import call_ai_text

    prompt = f"""
Analyze the following company website content and extract key company profile information.
Return ONLY a JSON object with these fields:

- summary: A concise company profile paragraph (max 200 words). Focus on: what they do, who they serve, their positioning, and any notable differentiators visible on the site.
- keywords: List of 5-10 important product/service keywords from the site (array of strings).
- topics: List of 3-5 main content topics or product categories covered (array of strings).
- tone: Writing style description (e.g. "professional", "technical", "educational", "conversational").
- company_context: Additional structured context about the company. Include any of the following if visible on the site:
  * Founding year or years in business
  * Key certifications or standards (ISO, CE, FDA, etc.)
  * Notable past projects or case studies
  * Target markets or regions served
  * Key differentiators or unique selling points
  * Recent market activities, exhibitions, or announcements
  Leave as empty string if none of the above are found.

Website URL: {url}
Website content:
{raw_text}
"""
    last_error = None
    for attempt in range(SNAPSHOT_MAX_RETRY + 1):
        try:
            raw = call_ai_text([{"role": "user", "content": prompt}], temperature=0.3)
            raw = raw.strip()
            if raw.startswith("```"):
                raw = re.sub(r"^```[a-zA-Z]*", "", raw).rstrip("`").strip()
            match = re.search(r"\{.*\}", raw, re.S)
            if match:
                return json.loads(match.group(0))
            last_error = "AI 返回内容不是 JSON 对象"
        except Exception as e:
            last_error = e
            if attempt < SNAPSHOT_MAX_RETRY:
                time.sleep(2)
    log.warning(f"[Snapshot] own_site AI 解析失败 {url}：{last_error}")
    return None


def fetch_and_parse_url(url):
    """兼容旧调用：抓取网页并解析，返回 parsed（含 _raw_hash 字段）。"""
    raw_text, raw_hash = fetch_url_raw_text(url)
    if not raw_text:
        return None
    parsed = parse_snapshot_from_text(url, raw_text)
    if not parsed:
        return None
    parsed["_raw_hash"] = raw_hash
    return parsed


# ══════════════════════════════════════════════════════════════════
# 扫描主流程
# ══════════════════════════════════════════════════════════════════

def run_snapshot_scan(client_id):
    """
    抓取客户的自有网站、竞品和参考网站，生成 WebsiteSnapshot 记录，完成后锁定 14 天。

    扫描顺序：
    1. own_site（客户自己的公司网站，用专门prompt提取公司画像）
    2. competitors（竞品网站）
    3. references（参考网站）

    优化：
    - 全局最多 2 个并发（SNAPSHOT_SEMAPHORE）
    - 同一轮扫描内 URL 去重
    - 先用 raw_hash 判断网页是否变化，未变化则跳过 AI 调用
    """
    with SNAPSHOT_SEMAPHORE:
        with _app_context():
            from models import Site
            site = Site.query.filter_by(client_id=client_id).first()
            if not site:
                log.warning(f"[Snapshot] client={client_id} 无站点配置，跳过扫描")
                return

            urls_to_scan = []
            seen_urls = set()

            # 优先抓客户自己的公司网站
            if site.site_url:
                own_url = site.site_url.strip()
                if own_url:
                    if not own_url.startswith("http"):
                        own_url = "https://" + own_url
                    if own_url not in seen_urls:
                        urls_to_scan.append(("own_site", own_url))
                        seen_urls.add(own_url)

            if site.competitors:
                for u in site.competitors.split(","):
                    u = u.strip()
                    if u and u not in seen_urls:
                        urls_to_scan.append(("competitor", u))
                        seen_urls.add(u)

            if site.references:
                for u in site.references.split(","):
                    u = u.strip()
                    if u and u not in seen_urls:
                        urls_to_scan.append(("reference", u))
                        seen_urls.add(u)

            log.info(f"[Snapshot] client={client_id} 开始扫描 {len(urls_to_scan)} 个 URL")

            for source_type, url in urls_to_scan:
                raw_text, raw_hash = fetch_url_raw_text(url)
                if not raw_text or not raw_hash:
                    continue

                latest = WebsiteSnapshot.query.filter_by(
                    client_id=client_id,
                    source_url=url
                ).order_by(WebsiteSnapshot.scanned_at.desc()).first()

                if latest and (latest.raw_hash or "") == raw_hash:
                    log.info(f"[Snapshot] 网页原文未变化，跳过 AI 解析和写入：{url}")
                    continue

                # own_site 使用专门的 prompt
                if source_type == "own_site":
                    parsed = parse_own_site_from_text(url, raw_text)
                else:
                    parsed = parse_snapshot_from_text(url, raw_text)

                if not parsed:
                    continue

                snapshot = WebsiteSnapshot(
                    client_id=client_id,
                    source_type=source_type,
                    source_url=url,
                    summary=parsed.get("summary", ""),
                    keywords=json.dumps(parsed.get("keywords", []), ensure_ascii=False),
                    topics=json.dumps(parsed.get("topics", []), ensure_ascii=False),
                    tone=parsed.get("tone", ""),
                    # own_site 把 company_context 存入 recent_changes 字段复用
                    recent_changes=parsed.get("company_context", "") or parsed.get("recent_changes", ""),
                    scanned_at=datetime.utcnow(),
                    raw_hash=raw_hash,
                )
                db.session.add(snapshot)
                log.info(f"[Snapshot] 已保存 {source_type} snapshot：{url}")

            site.snapshot_scanned_at = datetime.utcnow()
            site.snapshot_locked_until = datetime.utcnow() + timedelta(days=14)
            db.session.commit()
            log.info(f"[Snapshot] client={client_id} 扫描完成，锁定至 {site.snapshot_locked_until}")


def trigger_snapshot_scan_async(client_id):
    """
    异步触发 snapshot 扫描，不阻塞当前请求，也不占用 scheduler 执行线程。
    延迟 30 秒启动，确保 setup-client 提交事务完全完成。
    同时触发首次 AI 关键词生成。
    """
    def _run():
        try:
            time.sleep(30)
            # 首次配置时触发 AI 关键词生成
            try:
                from industry_news import build_keywords_with_ai
                from models import Site
                with _app_context():
                    site = Site.query.filter_by(client_id=client_id).first()
                    if site and not site.ai_search_keywords:
                        build_keywords_with_ai(site)
                        log.info(f"[IndustryNews] AI关键词首次生成完成 client={client_id}")
            except Exception as e:
                log.warning(f"[IndustryNews] AI关键词生成失败（不影响主流程）：{e}")
            run_snapshot_scan(client_id)
        except Exception as e:
            log.error(f"[Snapshot] 异步扫描失败 client={client_id}：{e}")

    threading.Thread(target=_run, daemon=True).start()
    log.info(f"[Snapshot] 已启动异步扫描线程 client={client_id}，30 秒后执行")


def run_weekly_snapshot_scan():
    """
    每周定时任务入口：扫描所有 active 客户的网站。
    错峰执行，每个客户间隔 30 秒。
    同时检查 AI 关键词是否需要刷新（超 30 天）。
    """
    with _app_context():
        clients = Client.query.filter_by(is_active=True).all()
        count = 0
        for client in clients:
            if not client.site:
                continue
            if not client.site.competitors and not client.site.references and not client.site.site_url:
                continue

            def _run(client_id=client.id, delay_seconds=30 * count):
                try:
                    if delay_seconds > 0:
                        time.sleep(delay_seconds)
                    # 每周扫描前检查 AI 关键词是否需要刷新（超30天）
                    try:
                        from industry_news import build_keywords_with_ai
                        from models import Site
                        with _app_context():
                            site = Site.query.filter_by(client_id=client_id).first()
                            if site:
                                need_refresh = (
                                    not site.ai_search_keywords or
                                    not site.ai_keywords_generated_at or
                                    (datetime.utcnow() - site.ai_keywords_generated_at).days >= 30
                                )
                                if need_refresh:
                                    build_keywords_with_ai(site)
                                    log.info(f"[IndustryNews] AI关键词30天刷新完成 client={client_id}")
                    except Exception as e:
                        log.warning(f"[IndustryNews] AI关键词刷新失败（不影响主流程）：{e}")
                    run_snapshot_scan(client_id)
                except Exception as e:
                    log.error(f"[Snapshot] 每周扫描失败 client={client_id}：{e}")

            threading.Thread(target=_run, daemon=True).start()
            count += 1
        log.info(f"[Snapshot] 每周扫描已启动 {count} 个客户线程")
