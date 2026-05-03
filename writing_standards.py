"""
Runify · BlogWriter — Writing Standards 4.1

统一写作标准和质检标准的单一真相来源（Single Source of Truth）。

写作 prompt 和质检 prompt 必须引用相同的标准，确保两者说同一套语言。
改动写作标准时，质检标准自动同步，永远不会出现不一致。
"""

# ══════════════════════════════════════════════════════════════════
# 核心原则：允许 vs 禁止
# ══════════════════════════════════════════════════════════════════

# 写作时允许的内容（质检不扣分）
ALLOWED_CONTENT = """
ALLOWED (do NOT penalize in quality check):
- Logical inference from provided context:
  e.g. "years of export experience" inferred from product_desc containing "export" → ALLOWED
  e.g. "serving European buyers" inferred from markets containing "Europe" → ALLOWED
  e.g. "specializing in industrial applications" inferred from product type → ALLOWED
- Explicit hypothetical scenarios clearly framed as examples:
  "For example, imagine a procurement manager in Germany who..."
  "Consider a scenario where a factory owner needs to..."
  "A typical buyer in this segment might ask..."
- General industry knowledge that is widely accepted and does not require a specific source
- How-to guidance and buyer evaluation frameworks
- Professional analytical reasoning without specific factual claims
"""

# 写作时禁止的内容（质检必须拦截）
FORBIDDEN_CONTENT = """
FORBIDDEN (hard fail in quality check — triggers mandatory rewrite):
- Specific certifications with numbers, dates, or bodies NOT provided in Company Profile or Industry News:
  e.g. "ISO 9001 certified since 2015" → FORBIDDEN if not in context
  e.g. "CE certified by TÜV Rheinland" → FORBIDDEN if not in context
- Specific 3rd-party statistics or data points with attributed sources NOT provided in context:
  e.g. "According to McKinsey, 73% of buyers..." → FORBIDDEN if not in context
  e.g. "Industry reports show a 45% increase..." → FORBIDDEN without provided source
- Fabricated customer names, project references, or case study details:
  e.g. "Our client BMW reported..." → FORBIDDEN if not in context
  e.g. "A project for a German auto parts manufacturer..." → FORBIDDEN if not in context
- Specific company addresses, phone numbers, emails, or office locations not provided:
  e.g. "Our office in Frankfurt..." → FORBIDDEN if not in context
- Named competitor accusations that could create legal risk
- Vague attribution that sounds authoritative but has no basis:
  e.g. "Industry experts agree that..." → FORBIDDEN (who? where?)
  e.g. "Studies show that buyers prefer..." → FORBIDDEN (which studies?)
"""

# 核心原则声明
CORE_PRINCIPLE = """
CORE PRINCIPLE:
We prefer professional "how-to" logic over fabricated "proof-of-capability".
An article that teaches buyers how to evaluate suppliers is BETTER than one that invents credentials.
Support logical inference from provided context.
Penalize factual fabrication of specific verifiable claims.
"""

# ══════════════════════════════════════════════════════════════════
# 质检阈值
# ══════════════════════════════════════════════════════════════════

QUALITY_EXCELLENT = 80     # ≥80分：「优质」，直接发布
QUALITY_GOOD = 70          # ≥70分：「良好」，发布
TRUST_HARD_FAIL = 10       # Trustworthiness < 10：硬性失败，必须重写

# ══════════════════════════════════════════════════════════════════
# 写作规则（注入 article_generator.py 的 prompt）
# ══════════════════════════════════════════════════════════════════

WRITING_RULES = f"""
### Content Integrity Rules (MUST FOLLOW) ###
{ALLOWED_CONTENT}
{FORBIDDEN_CONTENT}
{CORE_PRINCIPLE}

### Mandatory Writing Rules ###

RULE 1 — HOOK (first 100 words must contain ALL of):
  ✅ A specific buyer role (e.g., procurement manager, sourcing director, plant manager)
  ✅ A specific geographic market or industry context
  ✅ One concrete problem or pressure the buyer is facing RIGHT NOW
  ❌ FORBIDDEN openers: "In today's global business environment..." / "As the world becomes more connected..." / "In recent years..."

RULE 2 — EVERY CLAIM needs grounding:
  ✅ If provided data exists → reference it with source context
  ✅ If no data → use an explicit hypothetical: "For example, imagine a buyer in Germany sourcing..."
  ❌ FORBIDDEN: "Many companies report..." / "Industry experts say..." / "Studies show..." (without a provided source)

RULE 3 — INSIGHT section:
  ✅ Must offer ONE non-obvious professional interpretation
  ✅ Must be something a buyer couldn't easily find with a Google search
  ❌ FORBIDDEN: Restating the problem as the insight. Generic advice that applies to any supplier.

RULE 4 — SOLUTION section:
  ✅ Must connect to the company's specific product and market context
  ✅ Must give the buyer a practical framework, checklist, or set of questions they can actually use
  ❌ FORBIDDEN: "Contact us for more information" / "Our team is ready to help" / "Reach out today"

RULE 5 — CLOSING:
  ✅ Must give the buyer a clear evaluation criterion or decision-making standard
  ❌ FORBIDDEN: "We look forward to working with you" / "Feel free to reach out" / any sales pitch

RULE 6 — BANNED PHRASES (never use these, not even paraphrased versions):
  "high quality", "leading supplier", "best-in-class", "world-class",
  "cutting-edge", "innovative solutions", "one-stop solution",
  "many years of experience", "trusted partner", "comprehensive service",
  "state-of-the-art", "industry-leading"

RULE 7 — GEO/SGE STRUCTURE (mandatory for search engine visibility):
  ✅ Must include at least ONE <ul> or <table> for comparison, checklist, or key points
  ✅ Must end with a <blockquote> containing 3-5 key takeaways for buyers
  ✅ Use <h2> and <h3> to organize content into clear sections
  ❌ FORBIDDEN: Wall of unbroken paragraphs with no lists or structured elements
"""

# ══════════════════════════════════════════════════════════════════
# 质检标准（注入 quality_checker.py 的 prompt）
# ══════════════════════════════════════════════════════════════════

QUALITY_CHECK_DIMENSIONS = f"""
Score each dimension 0-20. Use the SAME standards as the writing rules.

1. TRUSTWORTHINESS (0-20) — MOST CRITICAL
   Apply the Content Integrity Rules strictly:
   
   {FORBIDDEN_CONTENT}
   {ALLOWED_CONTENT}
   
   Hard fail (score BELOW {TRUST_HARD_FAIL} immediately) if ANY forbidden item is present.
   Soft inference from context (see ALLOWED above) should NOT be penalized.
   
   15-20 = No fabrication detected, logically sound
   10-14 = Borderline but no clear hard fail
   0-9   = Contains forbidden fabrication → triggers mandatory rewrite

2. EXPERTISE (0-20) — Information Gain
   Does the article demonstrate genuine industry knowledge AND add unique value?
   - Merely restates industry news/context without company perspective → max 10
   - Combines industry context with company's specific product angle → 15
   - Provides unique buyer guidance combining trends + company USP,
     creating content not easily found elsewhere → 20
   
   20 = unique expert insight with information gain
   0  = pure generic filler or news restatement

3. BUYER_RELEVANCE (0-20)
   Would a real B2B buyer in this market find this genuinely useful for decision-making?
   20 = directly actionable with clear takeaways
   0  = useless or too generic to help any specific buyer

4. SPECIFICITY (0-20)
   Does every claim have a specific scenario, example, or explicit hypothetical?
   Note: Explicit hypotheticals ("imagine a buyer who...") count as specific. 
   Vague assertions ("many companies...") do not.
   20 = every claim grounded
   0  = everything is vague generalization

5. GEO_STRUCTURE (0-20)
   Is the article structured for AI/SGE extraction and B2B readability?
   - Has at least one <ul> or <table> → +8
   - Ends with a <blockquote> key takeaway → +6
   - Uses <h2>/<h3> to organize sections → +6
   20 = excellent structure
   0  = wall of text, no structure elements
"""
