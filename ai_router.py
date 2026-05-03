"""
Runify · BlogWriter — AI Router 4.0

职责：
- 多平台轮询调用AI，收到429或超时自动切换下一个平台
- 指数退避重试
- 所有AI调用统一走此模块，不在其他文件直接初始化OpenAI client

平台优先级：
1. 硅基流动（主力，价格便宜）
2. 钱多多（备用）
3. OpenRouter（兜底）

注意：
- 限速是账户级别，不是key级别，多个key无法绕过限速
- 多平台轮询才是正确的绕过限速方式
- 没有配置API key的平台自动跳过
"""

import os
import time
import random
import logging

from openai import OpenAI

from config import (
    OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL,
    QIANDUODUO_API_KEY, QIANDUODUO_BASE_URL, QIANDUODUO_MODEL,
    OPENROUTER_API_KEY, OPENROUTER_BASE_URL, OPENROUTER_MODEL,
)

log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════
# 平台配置
# ══════════════════════════════════════════════════════════════════

AI_PROVIDERS = [
    {
        "name": "siliconflow",
        "base_url": OPENAI_BASE_URL,
        "api_key": OPENAI_API_KEY,
        "model": OPENAI_MODEL,
        "priority": 1,
    },
    {
        "name": "qianduoduo",
        "base_url": QIANDUODUO_BASE_URL,
        "api_key": QIANDUODUO_API_KEY,
        "model": QIANDUODUO_MODEL,
        "priority": 2,
    },
    {
        "name": "openrouter",
        "base_url": OPENROUTER_BASE_URL,
        "api_key": OPENROUTER_API_KEY,
        "model": OPENROUTER_MODEL,
        "priority": 3,
    },
]


def _get_active_providers():
    """只返回已配置了API key的平台，按优先级排序"""
    return [p for p in sorted(AI_PROVIDERS, key=lambda x: x["priority"]) if p.get("api_key")]


# ══════════════════════════════════════════════════════════════════
# 错误类型判断
# ══════════════════════════════════════════════════════════════════

def _is_rate_limit_error(err_str):
    return "429" in err_str or "rate limit" in err_str.lower() or "too many requests" in err_str.lower()

def _is_timeout_error(err_str):
    return "timeout" in err_str.lower() or "timed out" in err_str.lower()

def _is_auth_error(err_str):
    return "401" in err_str or "403" in err_str or "invalid api key" in err_str.lower() or "authentication" in err_str.lower()


# ══════════════════════════════════════════════════════════════════
# 核心调用函数
# ══════════════════════════════════════════════════════════════════

def call_ai(messages, temperature=0.7, max_retries_per_provider=2):
    """
    多平台轮询调用AI，返回 completion response 对象。

    流程：
    1. 按优先级尝试各平台
    2. 遇到限速（429）→ 指数退避重试，超过max_retries切换下一平台
    3. 遇到超时或其他错误 → 直接切换下一平台
    4. 遇到认证错误 → 直接切换（key无效，重试没用）
    5. 全部平台失败 → 抛出RuntimeError

    调用方不需要处理平台切换逻辑，只需要处理最终失败的情况。
    """
    providers = _get_active_providers()
    if not providers:
        raise RuntimeError("[AIRouter] 没有可用的AI平台，请检查 .env 中的API key配置")

    last_error = None

    for provider in providers:
        log.info(f"[AIRouter] 尝试平台：{provider['name']}")

        for attempt in range(max_retries_per_provider):
            try:
                client = OpenAI(
                    api_key=provider["api_key"],
                    base_url=provider["base_url"],
                    timeout=60.0,
                )
                resp = client.chat.completions.create(
                    model=provider["model"],
                    messages=messages,
                    temperature=temperature,
                )
                log.info(f"[AIRouter] 调用成功：platform={provider['name']}")
                return resp

            except Exception as e:
                err_str = str(e)
                last_error = e

                if _is_auth_error(err_str):
                    # 认证失败，重试没用，直接切换平台
                    log.warning(f"[AIRouter] {provider['name']} 认证失败，切换下一平台：{e}")
                    break

                elif _is_rate_limit_error(err_str):
                    if attempt < max_retries_per_provider - 1:
                        # 指数退避：第1次等1-2秒，第2次等2-3秒
                        wait = (2 ** attempt) + random.uniform(0, 1)
                        log.warning(f"[AIRouter] {provider['name']} 触发限速，{wait:.1f}s后重试（attempt={attempt+1}）")
                        time.sleep(wait)
                    else:
                        log.warning(f"[AIRouter] {provider['name']} 限速重试耗尽，切换下一平台")
                        break

                elif _is_timeout_error(err_str):
                    # 超时直接切换，不重试
                    log.warning(f"[AIRouter] {provider['name']} 请求超时，切换下一平台")
                    break

                else:
                    # 其他未知错误，记录后切换
                    log.warning(f"[AIRouter] {provider['name']} 未知错误，切换下一平台：{e}")
                    break

    raise RuntimeError(f"[AIRouter] 所有AI平台均不可用。最后错误：{last_error}")


def call_ai_text(messages, temperature=0.7):
    """
    调用AI并直接返回文本内容字符串。
    是 call_ai() 的便捷封装，适合不需要完整response对象的场景。
    """
    resp = call_ai(messages, temperature=temperature)
    return resp.choices[0].message.content
