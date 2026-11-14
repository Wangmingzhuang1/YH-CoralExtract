"""大模型辅助复核建议：复用统一调用层 call_llm，输出结构化裁决建议供人工确认。

安全约定（宁放勿杀）：模型只给建议，不自动入库；证据不足时返回 needs_human，
绝不静默猜测单位/标尺。人工在工作台确认后才写入 --review-decisions 回填表。
"""

import json
import logging
import re

from app.utils.llm_api import call_llm
from app.core.moa.unit_converter import (
    UnitConversionModule, canonical_unit, finite_number, requires_baca_unit_check, resolve_unit,
)

from .sources import read_source

logger = logging.getLogger(__name__)

_SYSTEM = (
    "你是珊瑚古气候数据库的单位/标尺复核专家。根据提供的源文件证据，判断待复核观测的单位或同位素参考标尺，"
    "或判断该列是否应排除。只输出一个 JSON 对象，不要输出其它文字。字段："
    "action(confirm_unit|exclude|needs_human)、confirmed_unit(确认后的标准单位字符串，如 '‰ VPDB'、'mmol/mol'、'µmol/mol')、"
    "note(裁决依据说明，引用证据来源)、evidence_status(verified|provisional)、confidence(0到1)、rationale(简短推理)。"
    "只有原始档案定义、注解、表头或正文引句明确声明该列的单位和所需参考标尺，才可 confirm_unit 且 evidence_status=verified；"
    "正文引句只按字面内容核对。若同位素源资料仅写 '‰' 或 'per mil'，返回 needs_human，不能用领域惯例补写标尺；"
    "confirmed_unit 指源文件实际声明的单位，不是目标单位。若需要新增换算规则，返回 needs_human 并说明。"
    "中心化/距平/缩放等二次处理序列或测量误差列应 exclude；证据不足一律 needs_human。"
)


def _source_snippet(source_root, item, context=2):
    try:
        src = read_source(source_root, item.get("file", ""), item.get("sheet"),
                          item.get("source_row"), item.get("source_line"),
                          item.get("column"), context=context)
    except Exception:
        return ""
    if src.get("kind") not in {"table", "pdf", "docx"}:
        return f"[源不可读: {src.get('note', '')}]"
    parts = []
    if src.get("header"):
        parts.append("表头: " + " | ".join(c for c in src["header"] if c))
    for declaration in src.get("declarations", [])[:6]:
        parts.append(f"源文件第 {declaration['line']} 行: {declaration['text']}")
    for r in src.get("rows", [])[:6]:
        parts.append(f"行{r['line']}: " + " | ".join(c for c in r["cells"] if c))
    return "\n".join(parts)[:1500]


def _unit_evidence(item):
    raw = item.get("unit_evidence")
    try:
        entries = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return []
    return [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []


def _evidence_text(item, evidence):
    lines = [
        f"源文件: {item.get('file', '')}",
        f"工作表: {item.get('sheet', '')}  列: {item.get('column', '')}",
        f"目标指标字段: {item.get('decision', '')}",
        f"源单位(原始): {item.get('source_unit', '') or '(缺失)'}",
        f"源值样例: {item.get('source_value', '')}",
        f"暂缓原因: {item.get('reason', '')}",
    ]
    if evidence:
        # 历史批次的 body.label 是提取模型提出的候选，原文可能从未出现该单位。
        source_evidence = [{key: value for key, value in entry.items() if key != "label"}
                           if entry.get("source") == "body" else entry for entry in evidence]
        lines.append("已有单位证据: " + json.dumps(source_evidence, ensure_ascii=False))
    return "\n".join(lines)


def _supports_unit(item, evidence, unit):
    field = item.get("decision", "")
    unit = canonical_unit(unit)
    if (not unit or requires_baca_unit_check(field, unit)
            or UnitConversionModule().convert_value(field, 1, unit) is None):
        return False

    column = item.get("column", "")
    declarations = [(entry.get("source"), entry.get("label") or entry.get("unit")) for entry in evidence
                    if entry.get("source") in {"header", "unit_row", "archive_definition", "database_definition",
                                               "workbook_definition", "workbook_definition_alias", "table_note"}]
    results = [resolve_unit(field, column, declarations)]
    for entry in evidence:
        quote = entry.get("quote")
        if entry.get("source") in {"body", "reviewed_body"} and isinstance(quote, str) and quote:
            results.append(resolve_unit(field, column, declarations, quote=quote))
    return (all(result["status"] != "unit_conflict" for result in results)
            and {result["unit"] for result in results if result["status"] == "resolved"} == {unit})


def _parse(text):
    if not text:
        return None
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    match = re.search(r"\{.*\}", cleaned, re.S)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _fail_safe(reason):
    return {"action": "needs_human", "confirmed_unit": "", "note": "",
            "evidence_status": "", "confidence": 0.0, "rationale": reason}


def _suggest(question, model):
    try:
        text = call_llm(model=model, question=question, system_prompt=_SYSTEM,
                        temperature=0.0, enable_thinking=False)
    except RuntimeError as exc:
        logger.warning("复核建议调用失败: %s", exc)
        return _fail_safe(f"调用失败: {exc}"), model
    obj = _parse(text)
    if obj is None:
        return _fail_safe("模型未返回有效 JSON 建议"), model
    action = obj.get("action", "needs_human")
    if action not in {"confirm_unit", "exclude", "needs_human"}:
        action = "needs_human"
    return {
        "action": action,
        "confirmed_unit": str(obj.get("confirmed_unit", "") or ""),
        "note": str(obj.get("note", "") or ""),
        "evidence_status": obj.get("evidence_status", "") if obj.get("evidence_status") in {"verified", "provisional"} else "",
        "confidence": min(1, max(0, finite_number(obj.get("confidence")) or 0)),
        "rationale": str(obj.get("rationale", "") or ""),
    }, model


def suggest_item(source_root, item, model="flash"):
    evidence = _unit_evidence(item)
    question = (_evidence_text(item, evidence) + "\n\n源文件上下文:\n" + _source_snippet(source_root, item)
                + "\n\n请给出复核建议 JSON。")
    suggestion, used = _suggest(question, model)
    if suggestion["action"] == "confirm_unit" and (
            suggestion["evidence_status"] != "verified"
            or not _supports_unit(item, evidence, suggestion["confirmed_unit"])):
        suggestion = _fail_safe("现有可核对的源声明和正文引句不足以确认建议的单位或参考标尺。")
    elif suggestion["action"] == "needs_human":
        suggestion.update(confirmed_unit="", note="", evidence_status="")
    suggestion["model"] = used
    suggestion["scope"] = "item"
    return suggestion
