#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
统一大模型调用层

通过 OpenAI 兼容协议调用阿里云百炼模型，端点与密钥由 .env 管理：
- 默认使用 Token Plan 端点；如需切换为按量付费，只需修改 .env 中的
  LLM_BASE_URL / LLM_API_KEY 两项，代码无需改动。
- 两档模型（模型 ID 可通过 .env 覆盖）：
    "max"   -> qwen4-max-preview    复杂推理任务：列名解析、站点/位置提取、异常值检测、站点-数据映射
    "flash" -> qwen3.8-flash  轻量任务：正文元数据提取等
- qwen 系列支持 1M 上下文，旧版针对 30K 字符的输入截断逻辑已移除，
  大表格不再被静默截断丢行。
- 临时失败按指数退避重试；鉴权错误（401/403）直接抛出异常；
  服务明确返回配额耗尽时，本进程暂停对应账户、端点及模型的网络调用，重启后可重试。
- Token 用量直接取自 API 响应的 usage 字段（不再用 tiktoken 估算），
  可通过 get_usage_summary() / log_usage_summary() 查看。
"""

import logging
import threading
import time

import requests

from app.core.config import settings
from app.core.prompt import llm_base_prompt

# 模型档位 -> 实际模型ID
MODEL_ROLES = {
    "max": settings.LLM_MODEL_MAX,
    "flash": settings.LLM_MODEL_FLASH,
}

# 用量统计（按模型ID聚合，线程安全）
_usage_lock = threading.Lock()
_usage_stats: dict = {}
_quota_lock = threading.Lock()
_exhausted_quotas = set()


def _is_quota_exhausted(response):
    if response.status_code != 429:
        return False
    try:
        error = response.json().get("error", {})
        return isinstance(error, dict) and error.get("code") == "insufficient_quota"
    except (AttributeError, ValueError):
        return False


def get_usage_summary() -> dict:
    """返回各模型的累计用量：{model_id: {calls, prompt_tokens, completion_tokens}}"""
    with _usage_lock:
        return {model: dict(stat) for model, stat in _usage_stats.items()}


def log_usage_summary() -> None:
    """将累计用量写入日志（可在工作流结束时调用）"""
    summary = get_usage_summary()
    if not summary:
        logging.info("LLM用量统计：本次无调用")
        return
    for model, stat in summary.items():
        logging.info(
            "LLM用量统计 [%s] 调用%d次, 输入%d tokens, 输出%d tokens",
            model, stat["calls"], stat["prompt_tokens"], stat["completion_tokens"],
        )


def _record_usage(model_id: str, usage: dict) -> None:
    if not usage:
        return
    with _usage_lock:
        stat = _usage_stats.setdefault(
            model_id, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
        )
        stat["calls"] += 1
        stat["prompt_tokens"] += usage.get("prompt_tokens") or 0
        stat["completion_tokens"] += usage.get("completion_tokens") or 0


def call_llm(
    model: str = "max",
    question: str = "",
    system_prompt: str = None,
    temperature: float = 0.7,
    top_p: float = 0.8,
    enable_thinking: bool = None,
    thinking_budget: int = None,
    max_retries: int = 3,
    timeout: int = None,
) -> str:
    """统一 LLM 调用入口

    Args:
        model: 模型档位，"max"（qwen4-max-preview）或 "flash"（qwen3.8-flash）；
               也可直接传入完整模型ID
        question: 用户消息内容
        system_prompt: 系统提示词，默认使用 llm_base_prompt()
        temperature: 采样温度
        top_p: 核采样
        enable_thinking: 是否开启思考模式；None 时取 settings.LLM_ENABLE_THINKING
                         （默认关闭——列名映射等结构化判断任务无需长思考，
                         单次调用可从分钟级降到秒级）
        thinking_budget: 思考token预算（低思考模式），仅在开启思考时生效；
                         None 时取 settings.LLM_THINKING_BUDGET（0表示不限制）
        max_retries: 最大重试次数（指数退避）
        timeout: 单次请求超时秒数，默认取 settings.LLM_TIMEOUT

    Returns:
        模型回复文本；重试耗尽后返回空字符串

    Raises:
        RuntimeError: 未配置 API Key，或鉴权失败（401/403）
    """
    system_prompt = system_prompt if system_prompt is not None else llm_base_prompt()
    model_id = MODEL_ROLES.get(model, model)

    if enable_thinking is None:
        enable_thinking = settings.LLM_ENABLE_THINKING
    if thinking_budget is None:
        thinking_budget = settings.LLM_THINKING_BUDGET

    api_key = settings.LLM_API_KEY
    if not api_key:
        raise RuntimeError("未配置 LLM_API_KEY，请在 .env 中设置")

    url = f"{settings.LLM_BASE_URL.rstrip('/')}/chat/completions"
    quota = (url, api_key, model_id)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question},
        ],
        "temperature": temperature,
        "top_p": top_p,
        "stream": False,
    }
    body["enable_thinking"] = enable_thinking
    if enable_thinking and thinking_budget and thinking_budget > 0:
        body["thinking_budget"] = thinking_budget

    request_timeout = timeout or settings.LLM_TIMEOUT

    for attempt in range(max_retries):
        with _quota_lock:
            if quota in _exhausted_quotas:
                return ""
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=request_timeout)

            if resp.status_code in (401, 403):
                raise RuntimeError(
                    f"LLM鉴权失败(HTTP {resp.status_code})，请检查 .env 中的 LLM_API_KEY: "
                    f"{resp.text[:200]}"
                )

            if _is_quota_exhausted(resp):
                with _quota_lock:
                    first = quota not in _exhausted_quotas
                    _exhausted_quotas.add(quota)
                if first:
                    logging.warning("[%s] 服务配额耗尽，本进程暂停该模型请求；未完成项可在配额恢复后重试", model_id)
                return ""

            if resp.status_code == 200:
                data = resp.json()
                choices = data.get("choices") or []
                message = (choices[0].get("message") if choices else {}) or {}
                content = message.get("content") or ""
                reasoning = message.get("reasoning_content")
                if reasoning:
                    logging.debug("[%s] 思考过程: %s", model_id, reasoning[:500])
                _record_usage(model_id, data.get("usage"))
                if isinstance(content, str) and content.strip():
                    return content
                logging.warning("[%s] 返回内容为空（第%d次尝试）", model_id, attempt + 1)
            else:
                logging.warning(
                    "[%s] 调用失败 HTTP %d: %s", model_id, resp.status_code, resp.text[:300]
                )
        except RuntimeError:
            raise
        except (requests.RequestException, ValueError) as e:
            logging.warning("[%s] 请求异常（第%d次尝试）: %s", model_id, attempt + 1, e)

        if attempt < max_retries - 1:
            time.sleep(2 ** attempt)

    logging.error("[%s] 重试%d次后仍失败，返回空结果", model_id, max_retries)
    return ""
