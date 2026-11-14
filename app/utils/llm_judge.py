#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Jev 判卷层：独立于生成式 LLM 的高频原子判断通道

借鉴"生成器 + 判卷器 + 置信度门禁"架构（MiniCPM-OCR/Jev 流程的文本域移植）：
- 生成式模型（qwen4-max-preview / qwen3.8-flash）负责提取与映射；
- 判别式 Jev 模型对每个原子判断返回校准概率（0-1）；
- 门禁策略由调用方按 JUDGE_PASS_THRESHOLD / JUDGE_FAIL_THRESHOLD 分流：
  高置信放行 / 中置信保留并进人工复核队列 / 低置信撤销并进复核队列。

后端（.env 的 JUDGE_BACKEND 切换，默认 off 即完全不启用、零行为变化）：
- "typesafe": TypeSafe Jev 官方 API（POST https://api.typesafe.ai/v1/systemone，
              模型 jev-latest，输入 $0.042/百万token、输出免费，延迟 70-500ms）
- "ark":      火山方舟"决策评估"beta（国内端点，Jev 同类 schema，
              JUDGE_MODEL 需设为方舟的决策模型ID）

失败语义（fail-open）：未配置密钥、网络失败、返回异常时一律返回空 dict，
调用方视为"无判卷"全部放行——判卷层只做质量增强，绝不因自身故障阻塞流水线。
"""

import json
import logging
import re

import requests

from app.core.config import settings

DEFAULT_ENDPOINTS = {
    "typesafe": "https://api.typesafe.ai/v1/systemone",
    "ark": "https://ark.cn-beijing.volces.com/api/v1/systemone",
}


def judge_enabled() -> bool:
    """判卷层是否可用（后端已选择且密钥已配置）"""
    return settings.JUDGE_BACKEND in DEFAULT_ENDPOINTS and bool(settings.JUDGE_API_KEY)


def judge_noul_batch(state, instructions: dict, timeout: int = None) -> dict:
    """批量是非判断（noul）：一次请求并行评估多个命题

    Args:
        state: 共享上下文，str 或可 JSON 序列化对象（表头、样例行等）
        instructions: {问题ID: 命题文本}，命题按"成立"评估
        timeout: 请求超时秒数，默认 settings.JUDGE_TIMEOUT

    Returns:
        {问题ID: 命题成立概率(0-1)}；判卷不可用或失败时返回 {}（fail-open）
    """
    if not judge_enabled():
        return {}

    backend = settings.JUDGE_BACKEND
    url = settings.JUDGE_BASE_URL or DEFAULT_ENDPOINTS[backend]

    questions = {}
    for qid, text in instructions.items():
        if backend == "ark":
            questions[qid] = {
                "type": "noul",
                "instructions": text,
                "criteria": {"true": "命题成立", "false": "命题不成立或证据不足"},
            }
        else:
            questions[qid] = {"type": "noul", "instructions": text}

    headers = {
        "Authorization": f"Bearer {settings.JUDGE_API_KEY}",
        "Content-Type": "application/json",
    }
    if backend == "ark":
        headers["ark-beta-systemone-api"] = "true"

    body = {"model": settings.JUDGE_MODEL, "state": state, "questions": questions}

    try:
        resp = requests.post(url, headers=headers, json=body,
                             timeout=timeout or settings.JUDGE_TIMEOUT)
        if resp.status_code == 200:
            data = resp.json()
            answers = data.get("answers") if isinstance(data, dict) else None
            if not isinstance(answers, dict):
                return {}
            result = {}
            for qid, ans in answers.items():
                if not isinstance(ans, dict):
                    continue
                # noul 概率字段：TypeSafe 为 "noul"，兼容其他命名
                p = ans.get("noul")
                if p is None:
                    p = ans.get("probability", ans.get("value"))
                if qid in instructions and type(p) in (int, float) and 0 <= p <= 1:
                    result[qid] = float(p)
            if result:
                return result
            logging.warning("Jev判卷返回中无可解析概率: %s", str(answers)[:200])
        elif resp.status_code in (401, 403):
            logging.error("Jev判卷鉴权失败(HTTP %d)，请检查 JUDGE_API_KEY: %s",
                          resp.status_code, resp.text[:200])
        else:
            logging.warning("Jev判卷调用失败 HTTP %d: %s", resp.status_code, resp.text[:300])
    except (requests.RequestException, ValueError) as e:
        logging.warning("Jev判卷请求异常: %s", e)
    return {}


def escalate_remap(state, col, unit, orig_field, jev_score, pass_th, valid_fields,
                   model=None, timeout=None):
    """低置信映射升级：交生成式强模型独立【重映射】（默认关闭，由 JUDGE_ESCALATE 开启）

    与"自报verdict"不同：本函数让强模型基于完整证据重新给出该列应映射的标准字段（或skip），
    再由调用方用 Jev 复判该提议——同一独立门禁验证重试结果，防止强模型自评引入错误。
    只喂被判列自身的定义，避免跨列串扰（实测全量dump定义会导致抓错相邻列定义）。

    Args:
        state: 判卷上下文，含档案、正文、表注、目标列的声明与样值
        col: 被判列名；unit: 单位标注；orig_field: 生成式模型原提议字段
        jev_score: Jev 对原映射的置信度；pass_th: 放行线（写入提示作为"拒绝理由"）
        valid_fields: 合法标准字段集合（校验重映射结果）
        model: 升级模型ID，默认 settings.JUDGE_ESCALATE_MODEL
    Returns:
        (proposed_field, reason)；proposed_field ∈ valid_fields ∪ {"skip"}；
        未启用、调用失败或无有效提议时返回 None，由调用方决定是否暂缓原映射。
    """
    if not settings.JUDGE_ESCALATE:
        return None
    from app.utils.llm_api import call_llm

    model = model or settings.JUDGE_ESCALATE_MODEL
    column_evidence = (state.get("column_evidence") or {}).get(col)
    if column_evidence is None:
        column_evidence = {
            "archive_unit": (state.get("unit_row") or {}).get(col),
            "definition": (state.get("column_definitions") or {}).get(col),
        }
    fields = sorted(f for f in (valid_fields or set()) if f and f != "skip")
    prompt = f"""你是珊瑚古气候数据表格的列映射专家。生成式模型把某列映射到 '{orig_field}'，但独立判卷器置信度仅 {jev_score:.2f}（低于放行线 {pass_th:.2f}），认为该映射存疑。请基于完整证据独立重新判断该列应映射到哪个标准字段。

【表格上下文】
表格来源: {json.dumps(state.get("table_source", {}), ensure_ascii=False)}
表头: {json.dumps(state.get("table_headers", []), ensure_ascii=False)}
表注: {state.get("table_description", "") or "无"}
档案元数据: {json.dumps(state.get("archive_context", {}), ensure_ascii=False)}
文档元数据(PDF抽取): {json.dumps(state.get("document_context", {}), ensure_ascii=False)}
本列源证据: {json.dumps(column_evidence, ensure_ascii=False)}
前几行样例: {json.dumps(state.get("sample_rows", []), ensure_ascii=False)}

【可选标准字段】{fields}
若该列应排除（非观测值/海水同位素/实验处理/平滑距平残差/相对年代/无绝对年代/纯元数据列等），standard_field 填 "skip"。

【待重映射的列】'{col}'（单位标注: {unit or '无'}）

独立给出该列正确的标准字段。严格只返回JSON: {{"standard_field":"...","reason":"简短理由"}}"""

    try:
        text = call_llm(model, prompt, temperature=0,
                        timeout=timeout or settings.JUDGE_TIMEOUT, max_retries=2)
    except Exception as e:
        logging.warning("升级重映射调用异常: %s", e)
        return None
    if not text or not text.strip():
        return None

    cleaned = re.sub(r"^```json\s*|\s*```$", "", text.strip(), flags=re.MULTILINE).strip()
    s, e = cleaned.find("{"), cleaned.rfind("}")
    if s != -1 and e > s:
        cleaned = cleaned[s:e + 1]
    try:
        d = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        logging.warning("升级重映射返回非JSON: %s", text[:120])
        return None
    if not isinstance(d, dict):
        return None
    proposed = d.get("standard_field")
    if not isinstance(proposed, str) or not proposed.strip():
        return None
    proposed = proposed.strip()
    if proposed != "skip" and proposed not in (valid_fields or set()):
        return None
    return proposed, str(d.get("reason", ""))[:200]
