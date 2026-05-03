"""
Runify · BlogWriter — Article Generator 4.1

职责边界：
- 只负责根据站点配置生成文章标题与 HTML 正文。
- 不做质检和重写决策（见 quality_checker.py）。
- 不访问数据库。
- 不发布 WordPress。
- 不处理 quota / cycle / scheduler / 熔断 / 发布锁。

4.1 新增：
- 写作标准统一引用 writing_standards.py
- 拆分出 generate_article_raw()（纯生成，无质检）
- generate_article() 保持向后兼容，内部调用质检
- 支持 safe_mode 参数（极高限制模式）
"""

import json
import re
import logging

from ai_router import call_ai_text
from writing_standards import WRITING_RULES

log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════
# JSON 工具
# ══════════════════════════════════════════════════════════════════

def clean_json_response(text):
    """清理 AI 返回的 JSON，去除 markdown 包裹、前后杂质"""
    if not text:
        return ""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*", "", text)
        text = text.rstrip("`")
    match = re.search(r"\{.*\}", text, re.S)
    if match:
        return match.group(0)
    return text


def safe_parse_json(text):
    """多重兜底 JSON 解析"""
    try:
        return json.loads(text)
    except Exception:
        pass
    try:
        fixed = text.replace("\n", " ")
        fixed = re.sub(r",\s*}", "}", fixed)
        fixed = re.sub(r",\s*]", "]", fixed)
        return json.loads(fixed)
    except Exception:
        pass
    return None


# ══════════════════════════════════════════════════════════════════
# 信息格式化工具
# ══════════════════════════════════════════════════════════════════

def _safe_attr(obj, name, default=""):
    return getattr(obj, name, default) if obj is not None else default


def _format_signals(signals):
    if not signals:
        return "NONE - No company signals this cycle. Write an evergreen buyer-oriented article."
    lines = []
    for s in signals:
        sid = _safe_attr(s, "id", "")
        stype = _safe_attr(s, "type", "")
        weight = _safe_attr(s, "weight", "")
        content = _safe_attr(s, "content", "")
        if content:
            lines.append(f"- [ID: {sid}] [Type: {stype}] [Weight: {weight}] {content}")
    return "\n".join(lines) if lines else "NONE - No company signals this cycle."


def _format_snapshots(snapshots):
    """格式化竞品和参考网站 snapshot（不含 own_site，own_site单独处理）"""
    if not snapshots:
        return "No external snapshot available."
    lines = []
    for ss in snapshots:
        ssid = _safe_attr(ss, "id", "")
        source_type = _safe_attr(ss, "source_type", "")
        if source_type == "own_site":
            continue  # own_site 单独注入，不和竞品混在一起
        summary = _safe_attr(ss, "summary", "")
        keywords = _safe_attr(ss, "keywords", "")
        topics = _safe_attr(ss, "topics", "")
        tone = _safe_attr(ss, "tone", "")
        recent_changes = _safe_attr(ss, "recent_changes", "")
        parts = []
        if summary:
            parts.append(f"Summary: {summary}")
        if keywords:
            parts.append(f"Keywords: {keywords}")
        if topics:
            parts.append(f"Topics: {topics}")
        if tone:
            parts.append(f"Tone: {tone}")
        if recent_changes:
            parts.append(f"Recent Changes: {recent_changes}")
        if parts:
            lines.append(f"- [ID: {ssid}] [Source: {source_type}] " + " | ".join(parts))
    return "\n".join(lines) if lines else "No external snapshot available."


def _format_own_site(snapshots):
    """
    提取客户自己公司网站的 snapshot（source_type=own_site）。
    返回格式化的公司画像字符串，单独注入 prompt。
    包含：公司概述、认证、历史项目、市场活动等真实信息。
    """
    if not snapshots:
        return ""
    for ss in snapshots:
        source_type = _safe_attr(ss, "source_type", "")
        if source_type != "own_site":
            continue
        summary = _safe_attr(ss, "summary", "")
        keywords = _safe_attr(ss, "keywords", "")
        # recent_changes 字段被 own_site 复用来存 company_context
        company_context = _safe_attr(ss, "recent_changes", "")
        parts = []
        if summary:
            parts.append(f"Company Overview: {summary}")
        if company_context:
            parts.append(f"Additional Context (certifications, projects, history, activities): {company_context}")
        if keywords:
            parts.append(f"Product/Service Keywords from Website: {keywords}")
        if parts:
            return "\n".join(parts)
    return ""


def _format_history(history):
    if not history:
        return "No history."
    lines = []
    for h in history:
        title = _safe_attr(h, "title", "")
        topic = _safe_attr(h, "topic_used", "")
        angle = _safe_attr(h, "angle_used", "")
        item = []
        if title:
            item.append(f"Title: {title}")
        if topic:
            item.append(f"Topic: {topic}")
        if angle:
            item.append(f"Angle: {angle}")
        if item:
            lines.append("- " + " | ".join(item))
    return "\n".join(lines) if lines else "No history."


def _format_industry_news(news_items):
    """
    格式化行业动态注入 prompt。
    有全文的条目可以引用具体信息；只有标题的条目只能用作话题方向。
    """
    if not news_items:
        return "No current industry news available."
    lines = []
    for item in news_items:
        has_full = _safe_attr(item, "has_full_text", False)
        title = _safe_attr(item, "title", "")
        summary = _safe_attr(item, "summary", "")
        source_api = _safe_attr(item, "source_api", "")
        if has_full and _safe_attr(item, "full_text", ""):
            lines.append(
                f"- [Source: {source_api}] [Full Content Available — may reference specific trends]\n"
                f"  Title: {title}\n"
                f"  Summary: {summary[:300]}"
            )
        else:
            lines.append(
                f"- [Source: {source_api}] [Headline Only — use as topic direction ONLY, do NOT cite specific figures]\n"
                f"  Title: {title}"
            )
    return "\n".join(lines) if lines else "No current industry news available."


def _ids_from(items):
    if not items:
        return []
    ids = []
    for item in items:
        item_id = _safe_attr(item, "id", None)
        if item_id is not None:
            ids.append(item_id)
    return ids


# ══════════════════════════════════════════════════════════════════
# Prompt 构建
# ══════════════════════════════════════════════════════════════════

def _build_prompt(site, signals, snapshots, history, industry_news, rewrite_note="", safe_mode=False):
    """
    构建完整的文章生成 prompt。
    rewrite_note: 重写时传入上次质检的改进建议。
    """
    has_signals = bool(signals)
    has_industry_news = bool(industry_news)

    # 信息驱动模式判断
    if has_signals and has_industry_news:
        article_driver = (
            "SIGNAL + INDUSTRY NEWS MODE: "
            "Use Signal to define the company angle and opening hook. "
            "Use Industry News to provide the broader market context and timing relevance."
        )
    elif has_signals and not has_industry_news:
        article_driver = (
            "SIGNAL ONLY MODE: "
            "The article must be driven by the Signal. "
            "No current industry news available — draw context from general industry knowledge."
        )
    elif not has_signals and has_industry_news:
        article_driver = (
            "INDUSTRY NEWS MODE: "
            "Use Industry News to define the topic and angle. "
            "The company provides its perspective through the Company Profile."
        )
    else:
        article_driver = (
            "EVERGREEN MODE: "
            "No signals or industry news. "
            "Write a timeless buyer-oriented article based on Company Profile alone."
        )

    # 选填字段：有值才注入，空值不占位
    optional_parts = []
    if site.target_customer:
        optional_parts.append(f"Target Customer Profile: {site.target_customer}")
    if site.customer_pain:
        optional_parts.append(f"Known Customer Pains: {site.customer_pain}")
    if site.win_reason:
        optional_parts.append(f"Company Differentiators: {site.win_reason}")
    if site.topic_keywords:
        optional_parts.append(f"Preferred Topic Keywords: {site.topic_keywords}")
    optional_context = "\n".join(optional_parts) if optional_parts else "Not provided — infer from industry context and company profile."

    # own_site 公司画像（从 snapshot 里提取，独立注入）
    own_site_context = _format_own_site(snapshots)

    # 角度轮换
    try:
        recent_angles = json.loads(site.recent_angles or "[]")
    except Exception:
        recent_angles = []
    recent_angles_text = ", ".join(recent_angles[-10:]) if recent_angles else "None yet"

    # 重写指令
    rewrite_section = ""
    if rewrite_note:
        rewrite_section = f"""
### Rewrite Directive ###
{rewrite_note}
"""

    # Safe Mode 指令
    safe_mode_section = ""
    if safe_mode:
        safe_mode_section = """
### SAFE MODE ACTIVE ###
This is the final attempt. Apply MAXIMUM restriction:
- Use ONLY facts explicitly stated in Company Profile and Industry News
- Do NOT invent ANY specific claims, numbers, certifications, or cases
- Keep the article concise (800-1000 words) and extremely professional
- Focus entirely on buyer guidance: "how to evaluate", "what to look for", "questions to ask"
- Every sentence must be defensible without any external source
"""

    prompt = f"""
{safe_mode_section}### Role ###
You are the Lead Content Strategist for {site.brand_name or "this company"}, writing B2B articles on their behalf.

You do NOT write for search engines. You write for real buyers — procurement managers, sourcing directors, factory owners — who are evaluating suppliers or solving operational problems.

### Article Driver ###
{article_driver}
{rewrite_section}
### Company Identity (Always Apply) ###
Brand: {site.brand_name}
Product / Service: {site.product_desc}
Target Markets: {site.markets}
Language: {site.language or "English"}
Writing Style: {site.article_style or "professional"}
Target Length: approximately {site.article_length or 1500} words

### Company Website Profile (Extracted from Official Website) ###
{own_site_context if own_site_context else "Not available — rely on Company Identity above."}
Use this to enrich articles with authentic company context: history, certifications, past projects, real differentiators.
Do NOT invent details not present here. If this section is empty, ignore it entirely.

### Additional Company Context (Apply Only If Provided) ###
{optional_context}

### Current Industry News ###
IMPORTANT RULES FOR USING INDUSTRY NEWS:
- Items marked [Full Content Available]: you MAY reference specific trends or developments.
- Items marked [Headline Only]: use ONLY as topic direction. Do NOT invent or cite specific figures from these.

{_format_industry_news(industry_news)}

### Business Signals (Company's Own Events) ###
{_format_signals(signals)}

### Competitive & Reference Snapshots ###
Use for industry language and tone calibration ONLY.
Do NOT present competitor information as facts about this company.
{_format_snapshots(snapshots)}

### Recent Article History — Do Not Repeat ###
{_format_history(history)}

### Angle Diversity Control ###
Recently used angles (DO NOT repeat these): {recent_angles_text}

Pick ONE angle from this list that has NOT been used recently:
- Buyer decision criteria (how to evaluate and select suppliers)
- Market trend (what is changing in the industry right now)
- Risk management (what can go wrong, how to avoid it)
- Cost and efficiency (how to reduce procurement cost or improve ROI)
- Compliance and regulation (standards, certifications, requirements)
- Hypothetical buyer scenario (a specific realistic buyer situation)
- Supplier differentiation (what separates good suppliers from average ones)

{WRITING_RULES}

### Writing Structure ###
Follow this structure exactly:
1. Hook — specific buyer situation (RULE 1 required)
2. Industry Context — why this matters now (use Industry News if available)
3. Problem Depth — the real buyer pain, not a generic version
4. Insight — one non-obvious professional interpretation (RULE 3 required)
5. Solution / Guidance — practical framework tied to company context (RULE 4 required)
6. Practical Details — grounded specifics, explicit hypotheticals if needed (RULE 2 throughout)
7. Closing — evaluation standard for the buyer (RULE 5 required)

### SEO Requirements ###
Along with the article, generate:
- seo_description: 150-160 character meta description. Factual, specific, no clickbait.
- seo_slug: URL-friendly slug. Lowercase, hyphens only, max 60 characters.
- seo_focus_keyword: The single most important 2-4 word keyword phrase for this article.

### Output Format ###
Return STRICT JSON only. No markdown fences, no preamble, no explanation outside the JSON:
{{
  "title": "Article title",
  "content": "Full HTML body (no html/head/body tags)",
  "topic": "Brief topic tag (3-5 words)",
  "angle": "The specific angle used in this article (will be recorded to prevent repetition)",
  "seo_description": "150-160 char meta description",
  "seo_slug": "url-friendly-slug-here",
  "seo_focus_keyword": "focus keyword phrase"
}}
"""
    return prompt


# ══════════════════════════════════════════════════════════════════
# 原始生成函数（无质检，供 quality_checker.py 调用）
# ══════════════════════════════════════════════════════════════════

def generate_article_raw(
    site,
    signals=None,
    snapshots=None,
    history=None,
    industry_news=None,
    rewrite_note="",
    safe_mode=False,
):
    """
    纯文章生成函数，不包含质检逻辑。
    由 quality_checker.py 在需要重写时调用。

    返回 dict 或 None（生成失败时）。
    """
    signals = signals or []
    snapshots = snapshots or []
    history = history or []
    industry_news = industry_news or []

    prompt = _build_prompt(
        site=site,
        signals=signals,
        snapshots=snapshots,
        history=history,
        industry_news=industry_news,
        rewrite_note=rewrite_note,
        safe_mode=safe_mode,
    )

    try:
        temperature = 0.5 if safe_mode else 0.7
        raw = call_ai_text(
            [{"role": "user", "content": prompt}],
            temperature=temperature,
        )
    except Exception as e:
        log.error(f"[AI] generate_article_raw 生成失败：{e}")
        return None

    cleaned = clean_json_response(raw)
    data = safe_parse_json(cleaned)

    if not data or "title" not in data or "content" not in data:
        log.warning("[AI] JSON解析失败，启用兜底逻辑")
        title_match = re.search(r"^(.{10,80})", raw.strip())
        fallback_title = title_match.group(1) if title_match else "Generated Article"
        return {
            "title": fallback_title.strip(),
            "content": raw.strip(),
            "topic": "fallback",
            "angle": "fallback",
            "seo_description": "",
            "seo_slug": "",
            "seo_focus_keyword": "",
        }

    return data


# ══════════════════════════════════════════════════════════════════
# 主生成函数（向后兼容入口）
# ══════════════════════════════════════════════════════════════════

def generate_article(site, signals=None, snapshots=None, history=None, industry_news=None):
    """
    文章生成主入口（向后兼容）。
    内部调用 generate_article_raw() 生成，再交给 quality_checker.check_and_rewrite() 质检。

    返回 dict，包含所有字段供 scheduler_jobs.py 使用。
    """
    signals = signals or []
    snapshots = snapshots or []
    history = history or []
    industry_news = industry_news or []

    signals_used = _ids_from(signals)
    snapshots_used = _ids_from(snapshots)

    # 判断行业信息来源（用于AB测试记录）
    if industry_news:
        sources = [_safe_attr(n, "source", "") for n in industry_news]
        info_source = "hermes" if "hermes" in sources else "self"
    else:
        info_source = "none"

    # 第一次生成
    raw_result = generate_article_raw(
        site=site,
        signals=signals,
        snapshots=snapshots,
        history=history,
        industry_news=industry_news,
    )

    if not raw_result:
        log.error("[AI] 第一次生成失败，返回兜底内容")
        return {
            "title": "AI Generation Failed (Fallback)",
            "content": "<p>Temporary content generation issue. Please retry.</p>",
            "signals_used": signals_used,
            "snapshots_used": snapshots_used,
            "topic": "generation_failed",
            "angle": "fallback",
            "seo_description": "",
            "seo_slug": "",
            "seo_focus_keyword": "",
            "quality_score": None,
            "quality_notes": {},
            "quality_rewrite_count": 0,
            "quality_label": "good",
            "info_source": info_source,
            "needs_review": False,
        }

    # 质检和重写（交给 quality_checker）
    from quality_checker import check_and_rewrite
    final_result = check_and_rewrite(
        initial_result=raw_result,
        site=site,
        signals=signals,
        snapshots=snapshots,
        history=history,
        industry_news=industry_news,
    )

    return {
        "title": str(final_result.get("title", "")).strip(),
        "content": str(final_result.get("content", "")).strip(),
        "signals_used": signals_used,
        "snapshots_used": snapshots_used,
        "topic": str(final_result.get("topic", "")).strip(),
        "angle": str(final_result.get("angle", "")).strip(),
        "seo_description": str(final_result.get("seo_description", "")).strip()[:300],
        "seo_slug": str(final_result.get("seo_slug", "")).strip()[:200],
        "seo_focus_keyword": str(final_result.get("seo_focus_keyword", "")).strip()[:100],
        "quality_score": final_result.get("quality_score"),
        "quality_notes": final_result.get("quality_notes", {}),
        "quality_rewrite_count": final_result.get("quality_rewrite_count", 0),
        "quality_label": final_result.get("quality_label", "good"),
        "info_source": info_source,
        "needs_review": False,  # 4.1：保底发布，不再有needs_review
    }
