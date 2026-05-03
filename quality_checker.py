"""
Runify · BlogWriter — Quality Checker 4.1

职责边界：
- 只负责文章质检和重写决策
- 不生成文章（见 article_generator.py）
- 不发布 WordPress（见 scheduler_jobs.py）
- 不访问数据库

质检流程（最多3次，保底发布）：
  第1次生成后质检：
    ≥ 80分           → 「优质」，直接发布
    Trustworthiness < 10 → Trust硬性失败，发Trust警告重写
    总分 < 80         → 针对最弱维度重写

  第2次生成后质检：
    ≥ 70分           → 「良好」，发布
    Trustworthiness < 10 → 切换Safe Mode第三次
    总分 < 70         → 切换Safe Mode第三次

  第3次生成（Safe Mode）：
    无论分数 → 「良好」，强制发布
"""

import time
import logging

from ai_router import call_ai_text
from writing_standards import (
    QUALITY_CHECK_DIMENSIONS,
    QUALITY_EXCELLENT,
    QUALITY_GOOD,
    TRUST_HARD_FAIL,
)
from article_generator import generate_article_raw, clean_json_response, safe_parse_json

log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════
# 质检核心函数
# ══════════════════════════════════════════════════════════════════

def _run_eeat_check(title, content, site):
    """
    EEAT 质检，5个维度各20分，满分100。
    返回 (score: int | None, notes: dict)
    score 为 None 表示质检调用失败，不阻断发布流程。
    """
    check_prompt = f"""
You are a strict B2B content quality auditor.

Article Title: {title}
Article Content (first 2000 chars): {content[:2000]}

Company Context provided to the AI writer:
- Product: {site.product_desc}
- Markets: {site.markets}
- Language: {site.language}

{QUALITY_CHECK_DIMENSIONS}

Return ONLY this JSON, nothing else:
{{
  "trustworthiness": <0-20>,
  "expertise": <0-20>,
  "buyer_relevance": <0-20>,
  "specificity": <0-20>,
  "geo_structure": <0-20>,
  "total": <0-100>,
  "trust_hard_fail": <true or false>,
  "weakest_dimension": "<dimension name>",
  "improvement_note": "<one specific actionable sentence on what to improve>"
}}
"""
    try:
        time.sleep(2)  # 避免短时间连续调用触发限速
        raw = call_ai_text(
            [{"role": "user", "content": check_prompt}],
            temperature=0.1,
        )
        cleaned = clean_json_response(raw)
        data = safe_parse_json(cleaned)
        if data and "total" in data:
            score = int(data["total"])
            trust_score = int(data.get("trustworthiness", 20))
            # 双重判断trust硬性失败：AI标记 或 trust分数低于阈值
            if trust_score < TRUST_HARD_FAIL:
                data["trust_hard_fail"] = True
            log.info(
                f"[EEAT] 质检完成 score={score} "
                f"trust={trust_score} "
                f"trust_hard_fail={data.get('trust_hard_fail', False)} "
                f"weakest={data.get('weakest_dimension')}"
            )
            return score, data
    except Exception as e:
        log.warning(f"[EEAT] 质检调用失败，跳过质检直接发布：{e}")
    return None, {}


# ══════════════════════════════════════════════════════════════════
# 重写指令构建
# ══════════════════════════════════════════════════════════════════

def _build_trust_warning():
    """Trust硬性失败时的专项重写指令"""
    return (
        "TRUST FAILURE — MANDATORY REWRITE: "
        "The previous version invented specific unverified facts "
        "(certifications, addresses, customer names, or statistics not provided in context). "
        "Rewrite focus: "
        "1. Remove ALL specific unverified claims immediately. "
        "2. Replace with professional analytical logic and buyer guidance. "
        "3. Use 'how-to evaluate' framing instead of 'we have proved' framing. "
        "4. Every factual claim must be traceable to provided Company Profile or Industry News."
    )


def _build_quality_warning(notes):
    """普通质量不足时的针对性重写指令"""
    weakest = notes.get("weakest_dimension", "")
    improvement = notes.get("improvement_note", "Improve specificity and buyer relevance.")
    return (
        f"QUALITY IMPROVEMENT REQUIRED — REWRITE: "
        f"Weakest dimension: {weakest}. "
        f"Required improvement: {improvement} "
        f"Choose a DIFFERENT angle from the previous attempt."
    )


def _build_safe_mode_instruction():
    """Safe Mode（第三次）的极高限制指令"""
    return (
        "SAFE MODE — FINAL ATTEMPT WITH MAXIMUM RESTRICTION: "
        "Use ONLY facts explicitly stated in Company Profile and Industry News. "
        "Do NOT invent ANY specific claims, numbers, certifications, or cases. "
        "Keep the article concise (800-1000 words) and extremely professional. "
        "Focus entirely on buyer guidance: 'how to evaluate', 'what to look for', 'questions to ask suppliers'. "
        "Every sentence must be defensible without any external source. "
        "Structure: short intro → buyer checklist → key questions → brief closing."
    )


# ══════════════════════════════════════════════════════════════════
# 主入口：质检+重写编排
# ══════════════════════════════════════════════════════════════════

def check_and_rewrite(
    initial_result,
    site,
    signals,
    snapshots,
    history,
    industry_news,
):
    """
    质检和重写编排主入口。

    参数：
    - initial_result: article_generator.generate_article_raw() 的返回结果
    - site, signals, snapshots, history, industry_news: 传给重写的上下文

    返回：
    - result dict，包含 quality_score / quality_label / quality_rewrite_count
    """
    result = initial_result
    quality_score = None
    quality_notes = {}
    rewrite_count = 0
    quality_label = "good"  # 默认「良好」

    MAX_ATTEMPTS = 3  # 第1次已在外部生成，这里处理质检和最多2次重写

    for attempt in range(MAX_ATTEMPTS):

        # 第3次（Safe Mode）：直接发布，不再质检
        if attempt == 2:
            log.info("[EEAT] Safe Mode 第三次生成完成，强制发布，标记「良好」")
            quality_label = "good"
            break

        # 质检
        score, notes = _run_eeat_check(
            result.get("title", ""),
            result.get("content", ""),
            site,
        )
        quality_score = score
        quality_notes = notes

        # 质检API失败 → 直接发布
        if score is None:
            log.info("[EEAT] 质检失败，跳过重写直接发布")
            quality_label = "good"
            break

        # 「优质」：≥ 80分直接发布
        if score >= QUALITY_EXCELLENT:
            log.info(f"[EEAT] 质量优秀 score={score}，标记「优质」")
            quality_label = "excellent"
            break

        trust_hard_fail = notes.get("trust_hard_fail", False)
        trust_score = int(notes.get("trustworthiness", 20))

        # ── 第1次质检不达标 ──────────────────────────────────────────────────
        if attempt == 0:
            rewrite_count += 1
            if trust_hard_fail or trust_score < TRUST_HARD_FAIL:
                rewrite_note = _build_trust_warning()
                log.warning(
                    f"[EEAT] Trustworthiness硬性失败 trust_score={trust_score}，"
                    f"发Trust警告重写（第{rewrite_count}次）"
                )
            else:
                rewrite_note = _build_quality_warning(notes)
                log.info(
                    f"[EEAT] 质量不足 score={score}，"
                    f"触发针对性重写（第{rewrite_count}次）：{rewrite_note}"
                )

            # 重新生成
            new_result = generate_article_raw(
                site=site,
                signals=signals,
                snapshots=snapshots,
                history=history,
                industry_news=industry_news,
                rewrite_note=rewrite_note,
                safe_mode=False,
            )
            if new_result:
                result = new_result
            continue

        # ── 第2次质检 ────────────────────────────────────────────────────────
        if attempt == 1:
            if score >= QUALITY_GOOD and not trust_hard_fail:
                # ≥ 70分且无Trust硬失败 → 「良好」发布
                log.info(f"[EEAT] 重写后达到良好 score={score}，标记「良好」发布")
                quality_label = "good"
                break
            else:
                # 仍不达标 → 切换Safe Mode第三次
                rewrite_count += 1
                rewrite_note = _build_safe_mode_instruction()
                log.warning(
                    f"[EEAT] 第二次仍不达标 score={score} trust_fail={trust_hard_fail}，"
                    f"切换Safe Mode（第{rewrite_count}次）"
                )
                new_result = generate_article_raw(
                    site=site,
                    signals=signals,
                    snapshots=snapshots,
                    history=history,
                    industry_news=industry_news,
                    rewrite_note=rewrite_note,
                    safe_mode=True,
                )
                if new_result:
                    result = new_result
                continue

    # 保底：确保 result 有内容
    if not result:
        log.error("[EEAT] check_and_rewrite 没有有效结果，返回兜底内容")
        result = {
            "title": "AI Generation Failed (Fallback)",
            "content": "<p>Temporary content generation issue. Please retry.</p>",
            "topic": "generation_failed",
            "angle": "fallback",
            "seo_description": "",
            "seo_slug": "",
            "seo_focus_keyword": "",
        }
        quality_label = "good"

    # 注入质检结果到返回值
    result["quality_score"] = quality_score
    result["quality_notes"] = quality_notes
    result["quality_rewrite_count"] = rewrite_count
    result["quality_label"] = quality_label  # "excellent" / "good"

    return result
