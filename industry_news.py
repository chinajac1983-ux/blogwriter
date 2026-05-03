"""
Runify · BlogWriter — Industry News Fetcher 4.0（自研轨道）

信息源（按优先级）：
1. Google News RSS      → 免费无限，主力
2. Bing News RSS        → 免费无限，补充
3. The Guardian API     → 500次/天，高质量英文
4. NewsAPI              → 100次/天，结构化好
5. GDELT DOC 2.0        → 完全免费，100+语言，全球覆盖
6. Hacker News          → 完全免费，科技类客户专用
7. Reddit               → 免费，买家真实声音，需要subreddit过滤
8. Google Trends        → 非官方，热度趋势，不稳定
9. Tavily               → 兜底，1000次/月

职责：
- 按客户的 product_desc + markets 构建搜索词
- 同行业客户共享搜索结果，减少API调用
- 规则清洗（基础过滤 + 相关性过滤 + 质量评分）
- 写入 IndustryNews 表，source="self"

不负责：
- Hermes轨道（见 hermes_bridge.py）
- 文章生成（见 article_generator.py）
"""

import feedparser
import requests
import hashlib
import json
import logging
import time
import re
from datetime import datetime, timedelta
from urllib.parse import quote_plus

from extensions import db
from models import IndustryNews, Site
from config import (
    GUARDIAN_API_KEY,
    NEWSAPI_KEY,
    TAVILY_API_KEY,
    REDDIT_CLIENT_ID,
    REDDIT_CLIENT_SECRET,
)

log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════
# 常量
# ══════════════════════════════════════════════════════════════════

SPAM_KEYWORDS = ["sponsored", "advertisement", "promoted", "advertorial", "press release"]

# Reddit：B2B外贸相关的优质 subreddit 白名单
REDDIT_B2B_SUBREDDITS = [
    "supplychain", "manufacturing", "entrepreneur", "smallbusiness",
    "importing", "exporting", "logistics", "ecommerce", "Alibaba",
    "procurement", "b2b", "industrialengineering", "mechanical",
    "chemistry", "electronics", "textiles",
]

# GDELT：支持的语言代码映射
GDELT_LANGUAGE_MAP = {
    "english": "eng",
    "chinese": "chi",
    "spanish": "spa",
    "french": "fra",
    "german": "deu",
    "japanese": "jpn",
    "korean": "kor",
    "arabic": "ara",
    "portuguese": "por",
    "russian": "rus",
}


# ══════════════════════════════════════════════════════════════════
# 关键词构建
# ══════════════════════════════════════════════════════════════════

def build_keywords_with_ai(site):
    """
    用AI推断完整的关键词库，涵盖7个维度。
    结果缓存在 site.ai_search_keywords，每30天刷新一次。
    触发条件：首次生成、product_desc/markets变化、超过30天。
    """
    from ai_router import call_ai_text

    prompt = f"""
You are a B2B market research expert. Based on the following company information, generate comprehensive search keywords across 7 dimensions for industry news monitoring.

Company Product/Service: {site.product_desc}
Target Markets: {site.markets}
User's Primary Industries: {site.user_industries or "not specified"}
User's Primary Buyer Types: {site.user_buyer_types or "not specified"}

Generate keywords for each dimension. Return ONLY this JSON:
{{
  "product": ["core product term", "synonym", "technical name"],
  "industry_backup": ["other potential industry 1", "industry 2", "industry 3"],
  "buyer_backup": ["other potential buyer type 1", "buyer type 2"],
  "application": ["use case 1", "application area 2"],
  "pain_points": ["common buyer pain 1", "pain 2", "pain 3"],
  "regulations": ["relevant standard 1", "certification 2", "regulation 3"],
  "market_events": ["market trend 1", "supply chain topic 2", "trade topic 3"]
}}

Rules:
- All keywords must be in English regardless of input language
- Keep each keyword concise (2-5 words)
- Focus on what B2B buyers would search for, not what suppliers would say
- industry_backup should be different from user's primary industries
- buyer_backup should be different from user's primary buyer types
"""

    try:
        raw = call_ai_text([{"role": "user", "content": prompt}], temperature=0.3)
        raw = raw.strip()
        if raw.startswith("```"):
            import re
            raw = re.sub(r"^```[a-zA-Z]*", "", raw).rstrip("`").strip()
        import re
        match = re.search(r"\{.*\}", raw, re.S)
        if match:
            data = json.loads(match.group(0))
            # 存入数据库
            try:
                site.ai_search_keywords = json.dumps(data, ensure_ascii=False)
                site.ai_keywords_generated_at = datetime.utcnow()
                db.session.commit()
                log.info(f"[IndustryNews] AI关键词生成成功 client_id={site.client_id}")
            except Exception as db_err:
                log.warning(f"[IndustryNews] AI关键词存库失败（不影响本次使用）：{db_err}")
            return data
    except Exception as e:
        log.warning(f"[IndustryNews] AI关键词生成失败，使用基础关键词：{e}")

    return None


def build_search_keywords(site):
    """
    构建本次搜索的关键词列表（最多8个），覆盖不同维度，按周轮换。

    优先级：
    1. 用户填写的行业+买家（主力，每次必有）
    2. AI推断的备用维度（按周轮换，保证角度多样）
    3. 防止8周内重复使用相同关键词

    如果AI关键词不存在或超过30天，先调用AI生成。
    """
    # 检查AI关键词是否需要生成/刷新
    need_regenerate = False
    if not site.ai_search_keywords:
        need_regenerate = True
    elif site.ai_keywords_generated_at:
        days_since = (datetime.utcnow() - site.ai_keywords_generated_at).days
        if days_since >= 30:
            need_regenerate = True

    if need_regenerate:
        ai_data = build_keywords_with_ai(site)
    else:
        try:
            ai_data = json.loads(site.ai_search_keywords or "{}")
        except Exception:
            ai_data = {}

    # 兜底：ai_data 为 None 时用空字典
    if not ai_data:
        ai_data = {}

    # 解析用户填写的维度
    try:
        user_industries = json.loads(site.user_industries or "[]")
    except Exception:
        user_industries = []

    try:
        user_buyer_types = json.loads(site.user_buyer_types or "[]")
    except Exception:
        user_buyer_types = []

    # 读取搜索历史（8周内用过的关键词）
    try:
        history = json.loads(site.search_keyword_history or "[]")
    except Exception:
        history = []

    # 产品核心词
    product_core = " ".join((site.product_desc or "").split()[:3])
    markets = (site.markets or "").strip()

    # 按周轮换的维度索引
    week_num = datetime.utcnow().isocalendar()[1]

    candidates = []

    # ── 必有：用户主攻行业 + 产品词 ──────────────────────────────────────
    for industry in user_industries[:2]:
        candidates.append(f"{product_core} {industry}")

    # ── 必有：用户主要买家 + 产品词 ──────────────────────────────────────
    for buyer in user_buyer_types[:1]:
        candidates.append(f"{buyer} {product_core}")

    # ── 必有：产品 + 市场（基础兜底）────────────────────────────────────
    if not candidates:
        if product_core and markets:
            candidates.append(f"{product_core} {markets}")
        if product_core:
            candidates.append(f"{product_core} industry trends")

    # ── 轮换：AI推断的各维度 ─────────────────────────────────────────────
    def pick_rotating(lst, offset=0):
        """从列表中按周轮换取一个"""
        if not lst:
            return None
        return lst[(week_num + offset) % len(lst)]

    pain = pick_rotating(ai_data.get("pain_points", []), 0)
    if pain:
        candidates.append(f"{pain} {markets}" if markets else pain)

    regulation = pick_rotating(ai_data.get("regulations", []), 1)
    if regulation:
        candidates.append(regulation)

    market_event = pick_rotating(ai_data.get("market_events", []), 2)
    if market_event:
        candidates.append(market_event)

    backup_industry = pick_rotating(ai_data.get("industry_backup", []), 3)
    if backup_industry:
        candidates.append(f"{product_core} {backup_industry}")

    backup_buyer = pick_rotating(ai_data.get("buyer_backup", []), 4)
    if backup_buyer:
        candidates.append(f"{backup_buyer} procurement")

    application = pick_rotating(ai_data.get("application", []), 5)
    if application:
        candidates.append(f"{application} {markets}" if markets else application)

    # ── 过滤8周内用过的关键词 ────────────────────────────────────────────
    history_set = set(h.lower() for h in history)
    filtered = [k for k in candidates if k.lower() not in history_set]

    # 如果过滤后太少，放开限制用原始candidates
    final = filtered[:8] if len(filtered) >= 3 else candidates[:8]

    # ── 更新搜索历史 ─────────────────────────────────────────────────────
    new_history = history + final
    # 保留8周，每周最多8个词，最多保留64条
    new_history = new_history[-64:]
    try:
        site.search_keyword_history = json.dumps(new_history, ensure_ascii=False)
        db.session.commit()
    except Exception as e:
        log.warning(f"[IndustryNews] 搜索历史更新失败：{e}")

    log.info(f"[IndustryNews] 本次搜索词（{len(final)}个）：{final}")
    return [k for k in final if k.strip()]

def get_shared_cache_key(site):
    """
    同行业客户共享缓存key。
    product_desc前3个词 + markets → MD5 hash
    相同hash的客户共享搜索结果，节省API调用次数。
    """
    product_words = (site.product_desc or "").split()[:3]
    markets = (site.markets or "").strip().lower()
    raw = " ".join(product_words).lower() + "|" + markets
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def get_filter_keywords(site):
    """提取用于相关性过滤的关键词列表"""
    product_keywords = (site.product_desc or "").lower().split()[:5]
    market_keywords = (site.markets or "").lower().split()[:3]
    return list(set(product_keywords + market_keywords))


# ══════════════════════════════════════════════════════════════════
# 信息源：RSS类（免费无限）
# ══════════════════════════════════════════════════════════════════

def fetch_google_rss(keyword, language="english"):
    """Google News RSS，按关键词动态生成URL"""
    try:
        lang_lower = (language or "english").lower()
        if lang_lower.startswith("zh") or lang_lower.startswith("chinese"):
            hl, gl, ceid = "zh-CN", "CN", "CN:zh-Hans"
        elif lang_lower.startswith("fr") or lang_lower.startswith("french"):
            hl, gl, ceid = "fr", "FR", "FR:fr"
        elif lang_lower.startswith("de") or lang_lower.startswith("german"):
            hl, gl, ceid = "de", "DE", "DE:de"
        elif lang_lower.startswith("es") or lang_lower.startswith("spanish"):
            hl, gl, ceid = "es", "ES", "ES:es"
        else:
            hl, gl, ceid = "en-US", "US", "US:en"

        url = f"https://news.google.com/rss/search?q={quote_plus(keyword)}&hl={hl}&gl={gl}&ceid={ceid}"
        feed = feedparser.parse(url)
        results = []
        for entry in feed.entries[:10]:
            results.append({
                "title": entry.get("title", ""),
                "summary": entry.get("summary", ""),
                "url": entry.get("link", ""),
                "published_at": entry.get("published", ""),
                "source_api": "google_rss",
            })
        log.info(f"[IndustryNews] Google RSS 返回 {len(results)} 条：{keyword[:30]}")
        return results
    except Exception as e:
        log.warning(f"[IndustryNews] Google RSS 失败：{e}")
        return []


def fetch_bing_rss(keyword):
    """Bing News RSS"""
    try:
        url = f"https://www.bing.com/news/search?q={quote_plus(keyword)}&format=RSS"
        feed = feedparser.parse(url)
        results = []
        for entry in feed.entries[:10]:
            results.append({
                "title": entry.get("title", ""),
                "summary": entry.get("summary", ""),
                "url": entry.get("link", ""),
                "published_at": entry.get("published", ""),
                "source_api": "bing_rss",
            })
        log.info(f"[IndustryNews] Bing RSS 返回 {len(results)} 条：{keyword[:30]}")
        return results
    except Exception as e:
        log.warning(f"[IndustryNews] Bing RSS 失败：{e}")
        return []


# ══════════════════════════════════════════════════════════════════
# 信息源：API类（有额度限制）
# ══════════════════════════════════════════════════════════════════

def fetch_guardian(keyword):
    """The Guardian API，500次/天免费"""
    if not GUARDIAN_API_KEY:
        return []
    try:
        url = (
            f"https://content.guardianapis.com/search"
            f"?q={quote_plus(keyword)}"
            f"&api-key={GUARDIAN_API_KEY}"
            f"&show-fields=trailText"
            f"&page-size=5"
            f"&order-by=newest"
        )
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        results = []
        for item in data.get("response", {}).get("results", []):
            results.append({
                "title": item.get("webTitle", ""),
                "summary": item.get("fields", {}).get("trailText", ""),
                "url": item.get("webUrl", ""),
                "published_at": item.get("webPublicationDate", ""),
                "source_api": "guardian",
            })
        log.info(f"[IndustryNews] Guardian 返回 {len(results)} 条：{keyword[:30]}")
        time.sleep(1)  # Guardian 严格限制每秒1次
        return results
    except Exception as e:
        log.warning(f"[IndustryNews] Guardian API 失败：{e}")
        return []


def fetch_newsapi(keyword):
    """NewsAPI，100次/天免费"""
    if not NEWSAPI_KEY:
        return []
    try:
        url = (
            f"https://newsapi.org/v2/everything"
            f"?q={quote_plus(keyword)}"
            f"&sortBy=publishedAt"
            f"&pageSize=5"
            f"&apiKey={NEWSAPI_KEY}"
        )
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        results = []
        for item in data.get("articles", []):
            # 过滤掉 [Removed] 的文章
            if item.get("title") == "[Removed]":
                continue
            results.append({
                "title": item.get("title", ""),
                "summary": item.get("description", ""),
                "url": item.get("url", ""),
                "published_at": item.get("publishedAt", ""),
                "source_api": "newsapi",
            })
        log.info(f"[IndustryNews] NewsAPI 返回 {len(results)} 条：{keyword[:30]}")
        time.sleep(1)
        return results
    except Exception as e:
        log.warning(f"[IndustryNews] NewsAPI 失败：{e}")
        return []


def fetch_tavily(keyword):
    """Tavily，1000次/月，最终兜底"""
    if not TAVILY_API_KEY:
        return []
    try:
        resp = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": TAVILY_API_KEY,
                "query": keyword,
                "max_results": 5,
                "search_depth": "basic",
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        results = []
        for item in data.get("results", []):
            results.append({
                "title": item.get("title", ""),
                "summary": item.get("content", ""),
                "url": item.get("url", ""),
                "published_at": None,
                "source_api": "tavily",
            })
        log.info(f"[IndustryNews] Tavily 返回 {len(results)} 条：{keyword[:30]}")
        return results
    except Exception as e:
        log.warning(f"[IndustryNews] Tavily 失败：{e}")
        return []


# ══════════════════════════════════════════════════════════════════
# 信息源：GDELT（完全免费，全球新闻）
# ══════════════════════════════════════════════════════════════════

def fetch_gdelt(keyword, language="english"):
    """
    GDELT DOC 2.0 API，完全免费，100+语言，全球新闻覆盖。
    返回最近3个月内的相关文章列表。
    """
    try:
        lang_code = GDELT_LANGUAGE_MAP.get((language or "english").lower(), "eng")

        params = {
            "query": keyword,
            "mode": "artlist",
            "maxrecords": 10,
            "timespan": "1month",
            "format": "json",
        }
        # 非英语时加上语言过滤
        if lang_code != "eng":
            params["query"] = f"{keyword} sourcelang:{lang_code}"

        resp = requests.get(
            "https://api.gdeltproject.org/api/v2/doc/doc",
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        results = []
        for item in (data.get("articles") or []):
            title = item.get("title", "")
            if not title:
                continue
            results.append({
                "title": title,
                "summary": item.get("seendatetime", ""),  # GDELT不返回摘要，用日期占位
                "url": item.get("url", ""),
                "published_at": _parse_gdelt_date(item.get("seendatetime", "")),
                "source_api": "gdelt",
            })

        log.info(f"[IndustryNews] GDELT 返回 {len(results)} 条：{keyword[:30]}")
        time.sleep(1)
        return results
    except Exception as e:
        log.warning(f"[IndustryNews] GDELT 失败：{e}")
        return []


def _parse_gdelt_date(dt_str):
    """解析GDELT的时间格式 20240101120000 → datetime"""
    try:
        if dt_str and len(dt_str) >= 14:
            return datetime.strptime(dt_str[:14], "%Y%m%d%H%M%S")
    except Exception:
        pass
    return None


# ══════════════════════════════════════════════════════════════════
# 信息源：Hacker News（免费，科技类客户专用）
# ══════════════════════════════════════════════════════════════════

def fetch_hacker_news(keyword):
    """
    Hacker News 官方 Firebase API，完全免费无限制。
    先用 Algolia HN Search API 搜索相关帖子，再取详情。
    主要适合科技类B2B客户（SaaS、硬件、半导体等）。
    """
    try:
        # Algolia HN Search（非官方但稳定）
        url = f"https://hn.algolia.com/api/v1/search?query={quote_plus(keyword)}&tags=story&hitsPerPage=5"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        results = []
        for item in data.get("hits", []):
            title = item.get("title", "")
            story_url = item.get("url", "")
            hn_url = f"https://news.ycombinator.com/item?id={item.get('objectID', '')}"

            if not title:
                continue

            results.append({
                "title": title,
                "summary": f"HN discussion with {item.get('num_comments', 0)} comments. Points: {item.get('points', 0)}",
                "url": story_url or hn_url,
                "published_at": datetime.fromtimestamp(item["created_at_i"]) if item.get("created_at_i") else None,
                "source_api": "hackernews",
            })

        log.info(f"[IndustryNews] HackerNews 返回 {len(results)} 条：{keyword[:30]}")
        time.sleep(1)
        return results
    except Exception as e:
        log.warning(f"[IndustryNews] HackerNews 失败：{e}")
        return []


# ══════════════════════════════════════════════════════════════════
# 信息源：Reddit（买家真实声音）
# ══════════════════════════════════════════════════════════════════

def _get_reddit_token():
    """获取Reddit OAuth token"""
    if not REDDIT_CLIENT_ID or not REDDIT_CLIENT_SECRET:
        return None
    try:
        resp = requests.post(
            "https://www.reddit.com/api/v1/access_token",
            auth=(REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET),
            data={"grant_type": "client_credentials"},
            headers={"User-Agent": "BlogWriter/4.0 (industry research)"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json().get("access_token")
    except Exception as e:
        log.warning(f"[IndustryNews] Reddit token获取失败：{e}")
        return None


def _find_relevant_subreddits(keyword):
    """
    根据关键词从白名单里挑选最相关的subreddit（最多3个）。
    简单关键词匹配，不做复杂计算。
    """
    keyword_lower = keyword.lower()
    matched = []

    keyword_subreddit_map = {
        "supply": ["supplychain", "logistics", "procurement"],
        "manufactur": ["manufacturing", "industrialengineering"],
        "import": ["importing", "Alibaba", "b2b"],
        "export": ["exporting", "b2b", "importing"],
        "logistic": ["logistics", "supplychain"],
        "electronic": ["electronics", "manufacturing"],
        "chemical": ["chemistry", "manufacturing"],
        "textile": ["textiles", "manufacturing"],
        "software": ["entrepreneur", "smallbusiness"],
        "tech": ["entrepreneur", "smallbusiness"],
        "mechanic": ["mechanical", "manufacturing", "industrialengineering"],
    }

    for kw, subs in keyword_subreddit_map.items():
        if kw in keyword_lower:
            for sub in subs:
                if sub not in matched:
                    matched.append(sub)

    # 兜底：如果没有匹配，用通用B2B subreddit
    if not matched:
        matched = ["entrepreneur", "smallbusiness", "b2b"]

    return matched[:3]


def fetch_reddit(keyword):
    """
    Reddit搜索，返回相关讨论帖。
    注意：这是买家真实声音，在prompt里标注不能直接引用。
    """
    if not REDDIT_CLIENT_ID or not REDDIT_CLIENT_SECRET:
        return []

    token = _get_reddit_token()
    if not token:
        return []

    try:
        subreddits = _find_relevant_subreddits(keyword)
        subreddit_str = "+".join(subreddits)

        url = f"https://oauth.reddit.com/r/{subreddit_str}/search"
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": "BlogWriter/4.0 (industry research)",
        }
        params = {
            "q": keyword,
            "sort": "relevance",
            "t": "month",
            "limit": 5,
            "restrict_sr": "true",
        }

        resp = requests.get(url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        results = []
        for post in data.get("data", {}).get("children", []):
            p = post.get("data", {})
            title = p.get("title", "")
            if not title or p.get("score", 0) < 5:  # 过滤低质量帖子
                continue

            # 取帖子内容作为摘要
            selftext = (p.get("selftext", "") or "")[:300]
            summary = selftext if selftext else f"Reddit discussion in r/{p.get('subreddit', '')} with {p.get('num_comments', 0)} comments"

            results.append({
                "title": title,
                "summary": summary,
                "url": f"https://reddit.com{p.get('permalink', '')}",
                "published_at": datetime.fromtimestamp(p.get("created_utc", 0)) if p.get("created_utc") else None,
                "source_api": "reddit",
                "_subreddit": p.get("subreddit", ""),
                "_score": p.get("score", 0),
                "_is_community_voice": True,  # 标记为社区声音，prompt里特殊处理
            })

        log.info(f"[IndustryNews] Reddit 返回 {len(results)} 条：{keyword[:30]} subs={subreddits}")
        time.sleep(1)
        return results
    except Exception as e:
        log.warning(f"[IndustryNews] Reddit 失败：{e}")
        return []


# ══════════════════════════════════════════════════════════════════
# 信息源：Google Trends（非官方，热度趋势）
# ══════════════════════════════════════════════════════════════════

def fetch_google_trends(keyword, markets=""):
    """
    Google Trends，用 pytrends 获取关键词热度趋势。
    非官方API，不稳定，失败直接跳过不影响主流程。
    返回的是趋势数据，不是新闻，用作话题热度参考。
    """
    try:
        from pytrends.request import TrendReq
        import pandas as pd

        # 根据markets推断地区代码
        geo = ""
        markets_lower = (markets or "").lower()
        if "china" in markets_lower or "中国" in markets_lower:
            geo = "CN"
        elif "europe" in markets_lower or "欧洲" in markets_lower:
            geo = "GB"  # 用英国作为欧洲代表
        elif "us" in markets_lower or "america" in markets_lower or "美国" in markets_lower:
            geo = "US"
        elif "japan" in markets_lower or "日本" in markets_lower:
            geo = "JP"
        elif "korea" in markets_lower or "韩国" in markets_lower:
            geo = "KR"

        pytrends = TrendReq(hl="en-US", tz=360, timeout=(10, 25))
        # 取关键词的前2个词避免过长
        trend_keyword = " ".join(keyword.split()[:2])
        pytrends.build_payload([trend_keyword], cat=0, timeframe="today 3-m", geo=geo)

        interest_df = pytrends.interest_over_time()

        if interest_df is None or interest_df.empty:
            return []

        # 计算最近一个月的平均热度
        recent = interest_df.tail(4)
        avg_interest = int(recent[trend_keyword].mean()) if trend_keyword in recent.columns else 0

        if avg_interest < 10:
            # 热度太低，不值得记录
            return []

        # 获取相关话题
        related_topics = pytrends.related_topics()
        rising_topics = []
        if trend_keyword in related_topics:
            rising = related_topics[trend_keyword].get("rising")
            if rising is not None and not rising.empty:
                rising_topics = rising["topic_title"].head(3).tolist()

        summary = f"Search interest index: {avg_interest}/100 over the past 3 months."
        if rising_topics:
            summary += f" Rising related topics: {', '.join(rising_topics)}."

        log.info(f"[IndustryNews] Google Trends 返回热度数据：{trend_keyword} interest={avg_interest}")
        time.sleep(2)  # Trends 需要更长的间隔

        return [{
            "title": f"Google Trends: '{trend_keyword}' search interest — {avg_interest}/100",
            "summary": summary,
            "url": f"https://trends.google.com/trends/explore?q={quote_plus(trend_keyword)}&geo={geo}",
            "published_at": datetime.utcnow(),
            "source_api": "google_trends",
        }]

    except ImportError:
        log.debug("[IndustryNews] pytrends 未安装，跳过 Google Trends")
        return []
    except Exception as e:
        log.warning(f"[IndustryNews] Google Trends 失败（非致命）：{e}")
        return []


# ══════════════════════════════════════════════════════════════════
# 全文抓取
# ══════════════════════════════════════════════════════════════════

def try_fetch_full_text(url):
    """
    尝试抓取文章全文，失败返回None。
    只对高质量来源（guardian/newsapi/gdelt）尝试抓全文。
    """
    if not url or url.startswith("https://reddit.com") or url.startswith("https://news.ycombinator.com"):
        return None
    try:
        resp = requests.get(
            url,
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (compatible; BlogWriter/4.0)"},
            allow_redirects=True,
        )
        resp.raise_for_status()
        # 简单去除HTML标签
        text = re.sub(r"<[^>]+>", " ", resp.text[:10000])
        text = re.sub(r"\s+", " ", text).strip()
        # 至少200字才认为是有效全文
        return text if len(text) > 200 else None
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════
# 规则清洗
# ══════════════════════════════════════════════════════════════════

def is_valid_item(item):
    """基础过滤：标题长度、垃圾词、URL"""
    title = (item.get("title") or "").strip()
    if len(title) < 10:
        return False
    title_lower = title.lower()
    if any(kw in title_lower for kw in SPAM_KEYWORDS):
        return False
    if not item.get("url"):
        return False
    return True


def is_recent(item, days=30):
    """时效性过滤：超过N天的内容丢弃"""
    pub = item.get("published_at")
    if not pub:
        return True  # 无日期不过滤，交给质量评分降权
    try:
        if isinstance(pub, datetime):
            return pub >= datetime.utcnow() - timedelta(days=days)
        from dateutil import parser as dateparser
        dt = dateparser.parse(str(pub))
        if dt:
            dt = dt.replace(tzinfo=None)
            return dt >= datetime.utcnow() - timedelta(days=days)
    except Exception:
        pass
    return True


def is_relevant(item, filter_keywords):
    """
    相关性过滤：标题或摘要包含至少1个关键词才保留。
    同时记录匹配到的关键词，用于质量评分。
    """
    text = ((item.get("title") or "") + " " + (item.get("summary") or "")).lower()
    matched = [kw for kw in filter_keywords if kw and kw.lower() in text]
    item["_matched_keywords"] = matched
    return len(matched) > 0


def score_item(item):
    """
    质量评分，0-10分，决定文章生成时的取用优先级。
    """
    score = 0
    source_api = item.get("source_api", "")

    # 来源质量权重
    source_scores = {
        "guardian": 4,
        "newsapi": 3,
        "gdelt": 3,
        "tavily": 3,
        "hackernews": 2,
        "google_rss": 2,
        "bing_rss": 2,
        "reddit": 1,      # 噪音多，基础分低
        "google_trends": 1,  # 趋势数据，参考价值
    }
    score += source_scores.get(source_api, 1)

    # 有全文加分
    if item.get("full_text"):
        score += 2

    # 有发布时间加分
    if item.get("published_at"):
        score += 1

    # 关键词匹配数加分
    matched = len(item.get("_matched_keywords", []))
    score += min(matched, 2)

    # Reddit高分帖子加分
    if source_api == "reddit" and item.get("_score", 0) > 100:
        score += 1

    return min(score, 10)


def _safe_parse_datetime(value):
    """安全解析日期字符串"""
    if not value:
        return None
    try:
        if isinstance(value, datetime):
            return value.replace(tzinfo=None) if value.tzinfo else value
        from dateutil import parser as dateparser
        dt = dateparser.parse(str(value))
        return dt.replace(tzinfo=None) if dt else None
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════════

def fetch_industry_news_for_client(client_id):
    """
    为单个客户抓取行业动态，写入 IndustryNews 表，source="self"。

    执行顺序：
    1. Google RSS + Bing RSS（免费主力）
    2. GDELT（免费，全球覆盖）
    3. Guardian + NewsAPI（有额度，高质量）
    4. Hacker News（科技类客户）
    5. Reddit（买家声音）
    6. Google Trends（热度参考）
    7. Tavily（兜底，仅在前面来源不足时调用）
    """
    site = Site.query.filter_by(client_id=client_id).first()
    if not site:
        log.warning(f"[IndustryNews] client={client_id} 无站点配置，跳过")
        return

    keywords = build_search_keywords(site)
    if not keywords:
        log.warning(f"[IndustryNews] client={client_id} 无法构建关键词，使用基础词")
        product_core = " ".join((site.product_desc or "").split()[:3])
        markets = (site.markets or "").strip()
        if product_core:
            keywords = [f"{product_core} {markets}".strip(), f"{product_core} industry"]
        else:
            log.warning(f"[IndustryNews] client={client_id} product_desc为空，跳过")
            return

    filter_keywords = get_filter_keywords(site)
    language = site.language or "english"

    log.info(f"[IndustryNews] client={client_id} 开始抓取，关键词：{keywords}")

    # 已存在的URL（去重用）
    existing_urls = {
        row.url for row in IndustryNews.query.filter_by(
            client_id=client_id, source="self"
        ).with_entities(IndustryNews.url).all()
    }

    all_items = []
    seen_urls = set()

    for keyword in keywords:
        # Step 1: RSS（免费无限，先跑）
        for item in fetch_google_rss(keyword, language):
            all_items.append(item)
        time.sleep(1)

        for item in fetch_bing_rss(keyword):
            all_items.append(item)
        time.sleep(1)

        # Step 2: GDELT（免费，全球覆盖）
        for item in fetch_gdelt(keyword, language):
            all_items.append(item)

        # Step 3: Guardian + NewsAPI（有额度，按需调用）
        for item in fetch_guardian(keyword):
            all_items.append(item)

        for item in fetch_newsapi(keyword):
            all_items.append(item)

        # Step 4: Hacker News（科技类）
        for item in fetch_hacker_news(keyword):
            all_items.append(item)

        # Step 5: Reddit（买家声音）
        for item in fetch_reddit(keyword):
            all_items.append(item)

        # Step 6: Google Trends（热度趋势）
        for item in fetch_google_trends(keyword, site.markets):
            all_items.append(item)

    # 检查是否需要Tavily兜底
    # 清洗后有效内容少于5条时才调用Tavily
    preliminary_valid = [
        i for i in all_items
        if is_valid_item(i) and is_recent(i) and is_relevant(i, filter_keywords)
    ]

    if len(preliminary_valid) < 5:
        log.info(f"[IndustryNews] client={client_id} 有效内容不足({len(preliminary_valid)}条)，调用Tavily兜底")
        for keyword in keywords[:1]:  # Tavily只用第一个关键词节省额度
            for item in fetch_tavily(keyword):
                all_items.append(item)

    # 清洗和写入
    saved = 0
    for item in all_items:
        url = (item.get("url") or "").strip()
        if not url or url in seen_urls or url in existing_urls:
            continue
        seen_urls.add(url)

        if not is_valid_item(item):
            continue
        if not is_recent(item):
            continue
        if not is_relevant(item, filter_keywords):
            continue

        # 尝试抓全文（只对新闻类来源）
        full_text = None
        source_api = item.get("source_api", "")
        if source_api in ("guardian", "newsapi", "gdelt", "tavily"):
            full_text = try_fetch_full_text(url)
            if full_text:
                time.sleep(1)

        item["full_text"] = full_text
        score = score_item(item)

        try:
            news = IndustryNews(
                client_id=client_id,
                source="self",
                source_api=source_api,
                title=(item.get("title") or "")[:500],
                summary=(item.get("summary") or "")[:2000],
                full_text=full_text,
                url=url[:500],
                published_at=_safe_parse_datetime(item.get("published_at")),
                language=(language or "en")[:20],
                keywords_matched=json.dumps(
                    item.get("_matched_keywords", []), ensure_ascii=False
                ),
                has_full_text=bool(full_text),
                quality_score=score,
                created_at=datetime.utcnow(),
                expires_at=datetime.utcnow() + timedelta(days=60),
            )
            db.session.add(news)
            existing_urls.add(url)
            saved += 1
        except Exception as e:
            log.warning(f"[IndustryNews] 写入失败：{e}")

    try:
        db.session.commit()
        log.info(f"[IndustryNews] client={client_id} 完成，保存 {saved} 条")
    except Exception as e:
        db.session.rollback()
        log.error(f"[IndustryNews] client={client_id} 提交失败：{e}")


def run_weekly_industry_news_scan():
    """
    每周定时任务入口。
    为所有 active 客户抓取行业动态，错峰执行（每客户间隔30秒）。
    """
    from models import Client
    from extensions import db

    clients = Client.query.filter_by(is_active=True).all()
    count = 0

    for client in clients:
        if not client.site:
            continue
        try:
            fetch_industry_news_for_client(client.id)
        except Exception as e:
            log.error(f"[IndustryNews] client={client.id} 周扫描失败：{e}")
        time.sleep(30)  # 错峰，避免API请求集中
        count += 1

    log.info(f"[IndustryNews] 周扫描完成，共处理 {count} 个客户")
