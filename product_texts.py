"""
Runify · Authorly — Product Texts

职责边界：
- 只负责统一产品名称与支付展示文案
- 不参与任何业务逻辑
- 不涉及跳转、回调、邮件发送等运行层
"""

# 品牌名称（统一源）
BRAND_NAME = "Runify · Authorly 内容营销系统"


# 套餐名称映射
PLAN_NAME_MAP = {
    "trial": "体验版",
    "standard": "标准版",
    "pro": "专业版",
}


# 计费周期映射
BILLING_NAME_MAP = {
    "monthly": "月付",
    "quarterly": "季付",
    "yearly": "年付",
}


def get_plan_name(plan: str) -> str:
    """返回套餐中文名称"""
    return PLAN_NAME_MAP.get(plan or "", plan or "")


def get_billing_name(billing: str) -> str:
    """返回计费周期中文名称"""
    return BILLING_NAME_MAP.get(billing or "", billing or "")


def get_payment_subject(plan: str, billing: str) -> str:
    """
    支付标题（用于支付宝 subject / 前端展示）
    """
    plan_name = get_plan_name(plan)
    billing_name = get_billing_name(billing)

    # 体验版不带周期
    if plan == "trial":
        return f"{BRAND_NAME}｜体验版"

    # 正常套餐
    if billing_name:
        return f"{BRAND_NAME}｜{plan_name}（{billing_name}）"

    return f"{BRAND_NAME}｜{plan_name}"


def get_payment_short_subject(plan: str) -> str:
    """
    简短标题（用于微信 description 等）
    """
    plan_name = get_plan_name(plan)

    if plan_name:
        return f"Runify · Authorly｜{plan_name}"

    return "Runify · Authorly"