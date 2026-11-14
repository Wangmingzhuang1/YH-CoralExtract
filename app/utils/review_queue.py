#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""导出人工复核项，读取绑定到原始观测的单位裁决。"""

import csv
import hashlib
import json
import logging
import os
import re
import threading
from contextlib import contextmanager
from contextvars import ContextVar

_lock = threading.Lock()
_items = []
_capture = ContextVar("review_capture", default=None)
JUDGE_HANDOFF_ACTIONS = frozenset({"暂缓映射，复判未通过", "暂缓映射，初判缺失",
                                   "暂缓映射，升级失败", "暂缓映射，升级建议排除"})

_PDF_ATTACHMENT_NAME = re.compile(
    r"(?:\bsupp(?:lement(?:ary|al)?)?\b|\bappendix\b|esm[_ .-]*\d|"
    r"table[_ .-]*s\d|附录|补充材料)", re.I,
)


def is_paper_body_pdf(file):
    """正文 PDF 只供证据复核，明确命名的 PDF 附件仍可提取。"""
    name = os.path.basename(os.fspath(file))
    return name.lower().endswith(".pdf") and not _PDF_ATTACHMENT_NAME.search(name.replace("_", " "))

# 输出列顺序（新增字段自动追加到末尾）
_BASE_FIELDS = ["file", "sheet", "column", "decision",
                "confidence", "action", "reason", "review_id",
                "review_decision", "confirmed_unit", "review_note"]


def measurement_review_id(point, field, source_digests):
    """同一数据源、观测位置、字段和原始值对应同一个复核项。"""
    source = os.path.normcase(os.path.abspath(point.get("source_file") or "."))
    identity = [source, source_digests, point.get("sheet_name"), point.get("source_row"),
                point.get("sample_id"), point.get("source_columns", {}).get(field),
                field, point.get(f"{field}_original_value", point.get(field))]
    raw = json.dumps(identity, ensure_ascii=False, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load_unit_review_decisions(path, *, reuse_verified=False, source_root=None):
    """读取人工填写的单位裁决，拒绝缺少依据或同 ID 重复的输入。"""
    if not path:
        return {}
    if isinstance(path, (list, tuple)):
        combined = {}
        for file in path:
            loaded = load_unit_review_decisions(file, reuse_verified=reuse_verified, source_root=source_root)
            if combined.keys() & loaded.keys():
                raise ValueError(f"裁决文件包含重复观测或列：{file}")
            combined.update(loaded)
        return combined
    decisions = {}
    checked_files = {}
    def matches_version(name, digest):
        if source_root and not os.path.isabs(name):
            name = os.path.join(source_root, name)
        path = os.path.normcase(os.path.abspath(name)) if name else ""
        if not path or not re.fullmatch(r"[a-f0-9]{64}", digest):
            return False
        if path not in checked_files:
            try:
                with open(path, "rb") as source:
                    checked_files[path] = hashlib.file_digest(source, "sha256").hexdigest()
            except OSError:
                checked_files[path] = None
        return checked_files[path] == digest

    with open(path, encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        required = {"review_decision", "confirmed_unit", "review_note"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError("人工裁决文件缺少裁决、单位或说明列")
        for row_number, row in enumerate(reader, 2):
            action = (row.get("review_decision") or "").strip()
            if not action:
                continue
            review_id = (row.get("review_id") or "").strip()
            unit = (row.get("confirmed_unit") or "").strip()
            note = (row.get("review_note") or "").strip()
            file_key = (row.get("file") or "").strip()
            column_key = (row.get("column") or "").strip()
            sheet = (row.get("sheet") or "").strip()
            digest = (row.get("source_sha256") or "").strip().casefold()
            evidence = (row.get("evidence_status") or "").strip().casefold()
            origin = (row.get("decision_origin") or "").strip()
            file_level_exclude = action == "exclude" and file_key and not column_key and not review_id
            file_level_recover = action == "recover" and file_key and not column_key and not review_id
            if reuse_verified:
                if evidence != "verified" or (not sheet and not file_level_exclude and not file_level_recover) \
                        or not matches_version(file_key, digest):
                    continue
                if not os.path.isabs(file_key) and source_root:
                    file_key = os.path.join(source_root, file_key)
            if origin in {"paper_model", "paper_source"}:
                evidence_file = (row.get("evidence_file") or "").strip()
                evidence_hash = (row.get("evidence_sha256") or "").strip().casefold()
                if not matches_version(evidence_file, evidence_hash):
                    if reuse_verified:
                        continue
                    raise ValueError(f"裁决表第 {row_number} 行引用的正文已变化，请重新复核")
            if action not in {"confirm_unit", "confirm_mapping", "exclude", "recover", "select_column",
                              "select_time_column", "confirm_coordinates", "confirm_layout"} or not note:
                raise ValueError(f"人工裁决表第 {row_number} 行无效，需要裁决和依据")
            selected = (row.get("selected_column") or "").strip()
            target = (row.get("decision") or "").strip()
            lat = (row.get("coord_lat") or "").strip()
            lon = (row.get("coord_lon") or "").strip()
            if action == "confirm_mapping":
                if not (file_key and sheet and column_key and target and unit and not review_id
                        and re.fullmatch(r"[a-f0-9]{64}", digest)) or (selected and selected != column_key):
                    raise ValueError(f"裁决表第 {row_number} 行需要精确绑定源列、表块、目标字段和单位")
                key = mapping_review_key(file_key, sheet, column_key, digest)
            elif review_id:
                key = review_id  # 行级裁决
            elif action in {"select_column", "select_time_column"}:
                # 结构性裁决：多候选列/多年代列时人工选定其一
                if not (file_key and column_key and target):
                    raise ValueError(f"人工裁决表第 {row_number} 行列选择裁决需要文件、选定列和目标字段")
                key = ("SELECT", os.path.normcase(os.path.abspath(file_key)), target.casefold())
            elif action == "confirm_coordinates":
                # 坐标裁决：边界不完整/多站点时人工确认代表点
                if not file_key or not lat or not lon:
                    raise ValueError(f"人工裁决表第 {row_number} 行坐标裁决需要文件和经纬度")
                key = ("COORDS", os.path.normcase(os.path.abspath(file_key)))
            elif action == "confirm_layout":
                # 表结构裁决：模型/人工给出原表表头与观测区坐标（blocks），版本绑定。
                if not (file_key and sheet and re.fullmatch(r"[a-f0-9]{64}", digest)):
                    raise ValueError(f"人工裁决表第 {row_number} 行表结构裁决需要文件、表块和源文件 SHA-256")
                try:
                    blocks = json.loads(row.get("layout_blocks") or "")
                except (TypeError, ValueError):
                    blocks = None
                if not isinstance(blocks, list) or not blocks:
                    raise ValueError(f"人工裁决表第 {row_number} 行表结构裁决需要非空 layout_blocks 坐标")
                key = ("LAYOUT", os.path.normcase(os.path.abspath(file_key)), sheet.casefold(), digest)
            elif file_level_exclude:
                # 文件级排除：整文件非目标观测（如石笋/非珊瑚），需版本绑定
                if not re.fullmatch(r"[a-f0-9]{64}", digest):
                    raise ValueError(f"裁决表第 {row_number} 行文件级排除需要源文件 SHA-256")
                key = ("FILE_EXCLUDE", os.path.normcase(os.path.abspath(file_key)))
            elif file_level_recover:
                # 文件级回收：scope 分诊判定含 in-schema 指标（关键词预筛漏判），绕过预筛重提取
                if not re.fullmatch(r"[a-f0-9]{64}", digest):
                    raise ValueError(f"裁决表第 {row_number} 行文件级回收需要源文件 SHA-256")
                key = ("FILE_RECOVER", os.path.normcase(os.path.abspath(file_key)))
            elif file_key and column_key:
                file_key = os.path.normcase(os.path.abspath(file_key))
                if sheet or digest:
                    if not sheet or not re.fullmatch(r"[a-f0-9]{64}", digest):
                        raise ValueError(f"人工裁决表第 {row_number} 行需要同时提供工作表与源文件 SHA-256")
                    key = ("COLUMN", file_key, column_key.casefold(), sheet.casefold(), digest)
                else:
                    key = ("COLUMN", file_key, column_key.casefold())
            else:
                raise ValueError(f"人工裁决表第 {row_number} 行缺少复核 ID 或 文件+列 定位")
            if key in decisions or (action == "confirm_unit" and not unit):
                raise ValueError(f"人工裁决表第 {row_number} 行重复或缺少确认单位")
            if evidence not in {"", "verified", "provisional"}:
                raise ValueError(f"人工裁决表第 {row_number} 行证据状态无效")
            control = re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", note)
            structural = action in {"confirm_mapping", "select_column", "select_time_column",
                                    "confirm_coordinates", "confirm_layout"}
            if structural and evidence == "verified" and (not sheet or not re.fullmatch(r"[a-f0-9]{64}", digest)):
                raise ValueError(f"人工裁决表第 {row_number} 行需提供表块和源文件 SHA-256")
            if not evidence and (structural or re.search(r"值域|值量级|碳酸盐惯例|数值范围|疑似|推测", note) or control):
                evidence = "provisional"
            if control:
                note = note[:control.start()].strip() or "原说明包含不可读内容，见源裁决表"
            decisions[key] = {"action": action, "unit": unit, "note": note, "target": target,
                              "source_file": os.path.normcase(os.path.abspath(file_key)) if file_key else "",
                              "evidence_status": evidence, "selected": selected or column_key,
                              "lat": lat, "lon": lon, "sheet": sheet, "source_sha256": digest, "origin": origin}
            if origin in {"paper_source", "carbonate_convention"} and row.get("original_unit"):
                decisions[key]["original_unit"] = row["original_unit"]
            if action == "confirm_layout":
                decisions[key]["blocks"] = blocks
    return decisions


def mapping_review_key(file, sheet, column, digest):
    return ("MAP", os.path.normcase(os.path.abspath(file)), column.casefold(), sheet.casefold(), digest)


def layout_review_decision(decisions, file, sheet, source_digest):
    """按文件+表块+版本查找已核验的表结构裁决（confirm_layout）。"""
    path = os.path.normcase(os.path.abspath(file or "."))
    key = ("LAYOUT", path, str(sheet or "").casefold(), source_digest(file) if source_digest else "")
    decision = decisions.get(key)
    if (decision and decision["action"] == "confirm_layout"
            and decision["evidence_status"] == "verified" and decision.get("blocks")):
        return key, decision
    return key, None


def structural_review_decision(decisions, file, sheet, action, field=None, source_digest=None):
    path = os.path.normcase(os.path.abspath(file or "."))
    key = ("COORDS", path) if action == "confirm_coordinates" else ("SELECT", path, field.casefold())
    decision = decisions.get(key)
    if (decision and decision["action"] == action and decision["evidence_status"] == "verified"
            and decision["sheet"].casefold() == str(sheet or "").casefold()
            and source_digest and decision["source_sha256"] == source_digest(file)):
        return key, decision
    return key, None


def add_review_item(item: dict) -> None:
    """添加一条复核项（线程安全）"""
    captured = _capture.get()
    if captured is not None:
        captured.append(dict(item))
    with _lock:
        _items.append(dict(item))


@contextmanager
def capture_review_items():
    """收集当前文件的复核项，不混入其他并行文件。"""
    items = []
    token = _capture.set(items)
    try:
        yield items
    finally:
        _capture.reset(token)


def get_review_items() -> list:
    with _lock:
        return [dict(it) for it in _items]


def clear_review_items() -> None:
    with _lock:
        _items.clear()


def save_review_queue(output_dir: str, items=None) -> str:
    """将复核队列写入 output_dir/review_queue.csv，返回文件路径（无复核项返回空串）"""
    items = get_review_items() if items is None else items
    if not items:
        return ""
    fields = list(_BASE_FIELDS)
    for it in items:
        for k in it:
            if k not in fields:
                fields.append(k)
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, "review_queue.csv")
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for it in items:
            writer.writerow(it)
    logging.info("人工复核队列已保存: %s（共 %d 条）", path, len(items))
    return path
