#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按研究目录的提取覆盖审计 + 定向回收。

文件夹=一篇论文+附件。本模块对每个研究目录做"源证据 vs 实际产出"对账，把
"人工下载却未被充分利用"的文献逐目录 accounted，并定向驱动现有解决机制：

1. 本地规则（确定性、零 LLM）枚举源附件的目标指标列/年代列/岩芯标识，与该目录
   实际产出的列对账。
2. 三分支：
   * covered —— 源有目标线索且产出已覆盖，无动作。
   * irrelevant —— 任何附件都无目标指标线索（生长率/超 schema/非珊瑚），证据化
     排除，不调 Flash/Max（廉价挡掉不相干文章）。
   * gap —— 源有目标线索但产出缺失/暂缓，按缺口类型分级解决。
3. 缺口分级路由：确定性源证据修复（月份格式/并列岩芯/作者合成列，复用现有机制）
   → 仍缺解释才 Flash 定位具体文件/工作表/页码+支持列义和年代的原文 → 复杂跨表
   交现有 Max 论文级复核 → verified 后 patch 定向重提取。

账本与现有 FILE_EXCLUDE / FILE_RECOVER 裁决兼容（verified+sha256、可逆、跨批次
复用）。源文件一律不动。普适化：按目录+列/年代/岩芯模式判定，非单篇特判。
"""

import csv
import hashlib
import json
import logging
import os
import re
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.core.moa.llm_parser import CORE_PROXY_TOKENS
from app.utils.llm_api import call_llm
from app.utils.llm_cache import LLMCache

logger = logging.getLogger(__name__)

AUDIT_VERSION = "coverage-audit-v2"
AUDIT_LEDGER = "coverage_audit.csv"
DECISIONS = "scope_decisions.csv"
SUMMARY = "coverage_audit_summary.json"
MAX_HANDOFF = "max_handoff.csv"
MAX_ROUTES = {"flash_then_max", "deterministic_mapping_then_max"}
KEYWORD_GATE_REASON = "档案文件头未发现受支持指标线索"
FINAL_EXCLUSION_MARKERS = ("同目录内容完全相同的副本", "GBRCD", "整研究排除")
ATTACHMENT_SUFFIXES = {".xlsx", ".xlsm", ".xls", ".ods", ".docx", ".csv", ".tsv", ".txt", ".tab", ".md"}

# 发表指标（consolidated 观测列）：源线索对账的目标。Mg/Ca 被管线识别但未发表，
# 单独标 unpublished_proxy 缺口交用户决策，不静默丢弃。
PUBLISHED_PROXIES = {
    "Sr/Ca_value": ("Sr/Ca", "Sr/Ca (mmol/mol)"),
    "Ba/Ca_value": ("Ba/Ca", "Ba/Ca (μmol/mol)"),
    "δ18O_value": ("δ18O", "δ18O (‰ VPDB)"),
    "δ13C_value": ("δ13C", "δ13C (‰)"),
    "sea_surface_temperature_value": ("SST", "SST (°C)"),
}
PUBLISHED_NAMES = {name for name, _ in PUBLISHED_PROXIES.values()}
PROXY_BY_FIELD = {field: name for field, (name, _) in PUBLISHED_PROXIES.items()}

# 年代列与岩芯标识的本地规则（对账源是否具备可提取的年代/岩芯结构）。
CHRONOLOGY_RE = re.compile(
    r"\byear\b|\bage\b|\bdate\b|year\s*ce|common\s*era|\bce\b|\bbp\b|\bad\b|"
    r"calendar|年代|年份|日历年|年龄", re.I)
CORE_ID_RE = re.compile(
    r"\bcore\b|\bcolony\b|\bsample\b|\bcore\s*id\b|岩芯|样品|珊瑚编号|core\s*name", re.I)

# ---- scope 分类（irrelevant 分支用，沿用已验证的精度守卫）----
OUT_OF_SCHEMA = {
    "δ11B": re.compile(r"d\s*11\s*b|delta\s*11\s*b|boron\s+isotop|硼同位素", re.I),
    "Δ14C": re.compile(r"d\s*14\s*c|delta\s*14\s*c|radiocarbon|放射性碳", re.I),
    "δ66Zn": re.compile(r"d\s*66\s*zn|delta\s*66\s*zn|zinc\s+isotop", re.I),
    "δ65Cu": re.compile(r"d\s*65\s*cu|delta\s*65\s*cu|copper\s+isotop", re.I),
    "δ15N": re.compile(r"d\s*15\s*n|delta\s*15\s*n|nitrogen\s+isotop", re.I),
    "δ47": re.compile(r"d\s*47|delta\s*47|clumped", re.I),
    "P/Ca": re.compile(r"\bp\s*/\s*ca\b|phosphorus\s*/\s*calcium|磷钙", re.I),
    "Li/Mg": re.compile(r"\bli\s*/\s*mg\b|lithium\s*/\s*magnesium", re.I),
    "B/Ca": re.compile(r"\bb\s*/\s*ca\b|boron\s*/\s*calcium", re.I),
    "U/Ca": re.compile(r"\bu\s*/\s*ca\b|uranium\s*/\s*calcium", re.I),
    "luminescence": re.compile(r"luminescen|fluorescen|发光|荧光", re.I),
    "trace_metal": re.compile(r"\bzn\b|\bcu\b|zinc|copper|trace\s+metal|微量金属", re.I),
}
OUT_OF_SCOPE = {
    "growth": re.compile(r"growth\s+rate|calcification|linear\s+extension|extension\s+rate|"
                         r"density\s+band|band\s+width|skeletal\s+density|生长率|钙化速率|伸展速率", re.I),
    "sediment": re.compile(r"sediment\s+core|XRF|clastic|carbonate\s+mound|沉积", re.I),
    "non_coral": re.compile(r"speleothem|stalagmite|flowstone|sclerosponge|ceratoporella|astrosclera|"
                            r"foraminifer|fish\s+otolith|diet|tissue|symbiont|algae|seawater|"
                            r"石笋|硬海绵|有孔虫|耳石|共生藻", re.I),
}
HARD_NON_CORAL = re.compile(
    r"foraminifer|uvigerina|oyster|isognomon|bivalve|mollusk|mussel|clam|gastropod|pecten|"
    r"speleothem|stalagmite|flowstone|sclerosponge|ceratoporella|astrosclera|fish|otolith|"
    r"有孔虫|耳石|石笋|硬海绵|牡蛎|双壳|软体|腹足", re.I)


def _norm(text):
    return unicodedata.normalize("NFKC", text).casefold().replace("δ", "d").replace("Δ", "d")


def _sha256(path):
    try:
        with open(path, "rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError:
        return ""


def _read_header_blob(path, chars=40000):
    suffix = os.path.splitext(path)[1].lower()
    try:
        if suffix in {".xlsx", ".xlsm", ".xls"}:
            import openpyxl
            book = openpyxl.load_workbook(path, read_only=True, data_only=True)
            parts = list(book.sheetnames)
            for sheet in book.worksheets:  # 全部工作表（漏读第4+表会误判可排除）
                for row in sheet.iter_rows(min_row=1, max_row=12, values_only=True):
                    parts.append(" ".join(str(c) for c in row if c is not None))
            book.close()
            return "\n".join(parts)
        with open(path, "r", encoding="utf-8", errors="ignore") as stream:
            return stream.read(chars)
    except Exception as exc:
        logger.debug("审计读取失败 %s: %s", path, exc)
        return ""


def _extract_quantities(blob):
    """提取实测变量标识，避开 citation/方法/校正注记中的 incidental 词。"""
    noaa = re.findall(r"^##\s*([^\t\n]+)\t([^,\n]+),([^,\n]*)", blob, re.M)
    if noaa:
        return " ".join(f"{name} {what} {material}" for name, what, material in noaa)
    param = re.search(r"Parameter\(s\):(.*?)(?:\nLicense:|\nSize:|\n\*/|\Z)", blob, re.S)
    if param:
        names = []
        for line in param.group(1).splitlines():
            head = re.split(r"\s\*\s", line.strip())[0]
            name = re.sub(r"\[[^\]]*\]|\([^)]*\)", "", head).strip()
            if name and not name.startswith(("PI:", "METHOD", "COMMENT")):
                names.append(name)
        if names:
            return " ".join(names)
    return ""


def classify_scope(blob):
    """irrelevant 分支的 scope 分类（硬非珊瑚优先→指标→超 schema→scope 外）。"""
    text = _norm(blob)
    if not text.strip():
        return "ambiguous", [], "无法读取头部，需 flash 略读论文"
    if HARD_NON_CORAL.search(text):
        return "exclude_non_coral", ["non_coral"], "非珊瑚材料/生物（有孔虫/双壳/石笋/硬海绵等），属 scope 外"
    in_schema = [PROXY_BY_FIELD.get(f, f) for f, token in CORE_PROXY_TOKENS.items() if token.search(blob)]
    if in_schema:
        return "in_schema", in_schema, "含目标指标线索"
    out_schema = [name for name, pat in OUT_OF_SCHEMA.items() if pat.search(text)]
    if out_schema:
        return "exclude_out_of_schema", out_schema, "仅含发表 schema 之外的代用指标，证据化排除"
    out_scope = [name for name, pat in OUT_OF_SCOPE.items() if pat.search(text)]
    if out_scope:
        disp = "exclude_non_coral" if "non_coral" in out_scope else "exclude_out_of_scope"
        return disp, out_scope, "生长/钙化、沉积或非珊瑚材料，属 scope 外，证据化排除"
    return "ambiguous", [], "头部未识别出指标或材料线索，需 flash 略读论文"


def detect_source_clues(path):
    """本地规则：从单个附件检测目标指标列/年代列/岩芯标识 + 硬非珊瑚材料标志。"""
    blob = _read_header_blob(path)
    text = _extract_quantities(blob) or blob
    proxies = [PROXY_BY_FIELD.get(field, field) for field, token in CORE_PROXY_TOKENS.items()
               if token.search(text)]
    return {"proxies": proxies, "chronology": bool(CHRONOLOGY_RE.search(text)),
            "core_id": bool(CORE_ID_RE.search(text)),
            "non_coral": bool(HARD_NON_CORAL.search(_norm(text))), "blob": text}


# ---- 列级/源行级对账（第二轮：发现部分漏提）----
_PROXY_QUANTITY = [("strontium/calcium", "Sr/Ca"), ("barium/calcium", "Ba/Ca"),
                   ("magnesium/calcium", "Mg/Ca"), ("delta18o", "δ18O"), ("d18o", "δ18O"),
                   ("delta13c", "δ13C"), ("d13c", "δ13C"), ("seasurfacetemperature", "SST")]
_NOAA_VAR = re.compile(r"^##\s*([^\t\n]+)\t(.+)$", re.M)


def _quantity_to_proxy(quantity):
    q = re.sub(r"\s+", "", _norm(quantity))  # _norm 含 δ→d/Δ→d 归一化，否则漏 δ18O/δ13C
    for key, name in _PROXY_QUANTITY:
        if key in q:
            return name
    return None


def _header_to_proxy(header):
    for field, token in CORE_PROXY_TOKENS.items():
        if token.search(str(header)):
            return PROXY_BY_FIELD.get(field, field)
    return _quantity_to_proxy(header)


def _count_data_rows(blob, fmt):
    """统计源表数据行数（NOAA 在 # Data: 后，PANGAEA 在 */ 后），供行级对账。"""
    marker = "# Data:" if fmt == "NOAA" else "*/"
    index = blob.find(marker)
    if index < 0:
        return 0
    body = blob[index + len(marker):]
    return max(0, sum(1 for line in body.splitlines() if line.strip() and not line.lstrip().startswith("#")) - 1)


def _detect_xlsx_columns(path):
    """逐工作表检测目标列（读全部表+深表头，含第4+表/第8行表头），保留表名与行数。"""
    import openpyxl
    columns, chronology, profile = [], {"type": "none", "column": None}, None
    sheet_rows = {}
    try:
        book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:
        logger.debug("xlsx 读取失败 %s: %s", path, exc)
        return {"columns": [], "chronology": chronology, "profile": None, "format": "xlsx", "sheet_rows": {}}
    for sheet in book.worksheets:
        sheet_rows[sheet.title] = max(0, (sheet.max_row or 1) - 1)
        for row in sheet.iter_rows(min_row=1, max_row=20, values_only=True):
            for cell in row:
                if cell is None:
                    continue
                text = str(cell).strip()
                proxy = _header_to_proxy(text)
                if proxy:
                    columns.append({"name": text, "proxy": proxy, "seasonality": "", "sheet": sheet.title})
                if re.search(r"\bage\b|\byear\b|\bdate\b", text, re.I):
                    if re.search(r"relative|internal|growth|elapsed", text, re.I):
                        chronology = {"type": "relative", "column": text}
                    elif re.search(r"\bCE\b|\bAD\b|\bBP\b|\bBCE\b|calendar|common\s*era", text, re.I) \
                            and chronology["type"] != "relative":
                        chronology = {"type": "absolute", "column": text}
                if re.search(r"\bsample\b|\bcore\b", text, re.I):
                    profile = profile or text
    book.close()
    return {"columns": columns, "chronology": chronology, "profile": profile,
            "format": "xlsx", "sheet_rows": sheet_rows}


def detect_source_columns(path):
    """复用 NOAA/PANGAEA 变量声明与 xlsx 表头，逐列检测目标指标+年代类型+剖面标识。

    返回 {columns:[{name,proxy,seasonality,sheet?}], chronology:{type,column}, profile, format}。
    年代 type=absolute（CE/BP/BCE）/relative（relative/internal/growth）/none。
    """
    suffix = os.path.splitext(path)[1].lower()
    if suffix in {".xlsx", ".xlsm", ".xls"}:
        return _detect_xlsx_columns(path)
    blob = _read_header_blob(path, chars=60000)
    columns, chronology, profile = [], {"type": "none", "column": None}, None

    def note_chronology(name, text):
        nonlocal chronology
        if re.search(r"relative|internal|growth|elapsed", text, re.I):
            chronology = {"type": "relative", "column": name}
        elif re.search(r"common\s*era|\bCE\b|\bAD\b|\bBP\b|\bBCE\b|calendar", text, re.I):
            if chronology["type"] != "relative":
                chronology = {"type": "absolute", "column": name}

    noaa = _NOAA_VAR.findall(blob)
    if noaa:
        for name, definition in noaa:
            try:
                parts = next(csv.reader([definition]))
            except csv.Error:
                continue
            name = name.strip()
            quantity = parts[0].strip() if parts else ""
            unit = parts[3].strip() if len(parts) > 3 else ""
            seasonality = parts[4].strip() if len(parts) > 4 else ""
            proxy = _quantity_to_proxy(quantity)
            if proxy:
                columns.append({"name": name, "proxy": proxy, "seasonality": seasonality})
            if re.search(r"\bage\b|\byear\b|\bdate\b|\btime\b", name, re.I) or "age" in quantity.casefold():
                note_chronology(name, f"{quantity} {unit}")
            if "sample identification" in quantity.casefold() or re.search(r"\bsample\b|\bcore\b", name, re.I):
                profile = profile or name
        return {"columns": columns, "chronology": chronology, "profile": profile,
                "format": "NOAA", "rows": _count_data_rows(blob, "NOAA")}

    param = re.search(r"Parameter\(s\):(.*?)(?:\nLicense:|\nSize:|\n\*/|\Z)", blob, re.S)
    if param:
        for line in param.group(1).splitlines():
            head = re.split(r"\s\*\s", line.strip())[0]
            label = re.sub(r"\[[^\]]*\]", "", head)
            label = re.sub(r"\([^)]*\)", "", label).strip()
            if not label:
                continue
            proxy = _quantity_to_proxy(label)
            if proxy:
                columns.append({"name": label, "proxy": proxy, "seasonality": ""})
            if re.search(r"\bage\b|\byear\b|\bdate\b", label, re.I):
                note_chronology(label, label)
            if re.search(r"sample|core|label", label, re.I):
                profile = profile or label
        return {"columns": columns, "chronology": chronology, "profile": profile,
                "format": "PANGAEA", "rows": _count_data_rows(blob, "PANGAEA")}
    return {"columns": columns, "chronology": chronology, "profile": profile, "format": "unknown", "rows": 0}


def _file_output(run_dir):
    """产出侧按文件+工作表聚合：实际产出的指标列/原列/行数（复用 source_file/source_columns/
    source_sheet_name）。表级追踪使 A 表产出不掩盖 B 表同名列漏提。"""
    output = defaultdict(lambda: {"rows": 0, "proxies": set(), "columns": set(), "sheets": {}})
    combined = Path(run_dir) / "consolidated/all_data_combined.csv"
    columns = {name: column for name, column in PUBLISHED_PROXIES.values()}
    if not combined.is_file():
        return output
    with combined.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            file = row.get("source_file", "")
            if not file:
                continue
            entry = output[os.path.normcase(os.path.abspath(file))]
            entry["rows"] += 1
            sheet = (row.get("source_sheet_name") or row.get("sheet_name") or "").strip()
            sheet_entry = entry["sheets"].setdefault(sheet, {"rows": 0, "columns": set()})
            sheet_entry["rows"] += 1
            for name, column in columns.items():
                if row.get(column) not in (None, "", "NA"):
                    entry["proxies"].add(name)
            try:
                source_columns = json.loads(row.get("source_columns") or "{}")
            except (TypeError, ValueError):
                source_columns = {}
            entry["columns"].update(source_columns.values())
            sheet_entry["columns"].update(source_columns.values())
    return output


def _norm_col(name):
    return re.sub(r"\s+", " ", re.sub(r"\[[^\]]*\]", "", str(name))).strip().casefold()


def _read_data_table(path, fmt):
    """读源数据表（NOAA 在 # Data: 后、PANGAEA 在 */ 后、xlsx 逐表），失败返回 None。"""
    import pandas as pd
    from io import StringIO
    suffix = os.path.splitext(path)[1].lower()
    try:
        if suffix in {".xlsx", ".xlsm", ".xls"}:
            return pd.read_excel(path, sheet_name=None)
        if suffix in {".csv", ".tsv"}:
            return pd.read_csv(path, sep=None, engine="python")
        text = open(path, encoding="utf-8", errors="ignore").read()
        marker = "# Data:" if fmt == "NOAA" else "*/"
        index = text.find(marker)
        if index < 0:
            return None
        return pd.read_csv(StringIO(text[index + len(marker):]), sep="\t")
    except Exception as exc:
        logger.debug("源数据表读取失败 %s: %s", path, exc)
        return None


def _count_extractable_rows(path, source):
    """统计源里真正可提取的行：至少一个目标指标列有有限数值，且年代列非空可解析。

    过滤合理排除（缺指标值/缺年代的行），避免 row_missing 把合理排除当漏提。
    相对/无年代→返回 None（行不可提取，由 relative_age 缺口处理）；逐表返回 dict。
    """
    if source["chronology"]["type"] != "absolute":
        return None
    proxy_cols = [c["name"] for c in source["columns"] if c["proxy"] in PUBLISHED_NAMES]
    if not proxy_cols:
        return None
    table = _read_data_table(path, source["format"])
    if table is None:
        return None
    from app.core.moa.unit_converter import finite_number
    chrono_col = source["chronology"].get("column")

    def count_df(df):
        cols = {_norm_col(str(c)): c for c in df.columns}
        pcols = [cols[_norm_col(p)] for p in proxy_cols if _norm_col(p) in cols]
        if not pcols:
            return 0
        valid = df[pcols].apply(lambda s: s.map(lambda v: finite_number(v) is not None)).any(axis=1)
        ccol = cols.get(_norm_col(chrono_col)) if chrono_col else None
        if ccol is not None:
            year_ok = df[ccol].map(lambda v: finite_number(v) is not None or bool(re.search(r"\d", str(v))))
            valid = valid & year_ok
        return int(valid.sum())

    if isinstance(table, dict):
        return {sheet: count_df(df) for sheet, df in table.items()}
    return count_df(table)


def audit_file_columns(path, file_out):
    """列级/行级对账：源目标列 vs 该文件实际产出，发现部分漏提与相对年代目标表。

    按工作表逐列核对（A 表产出不掩盖 B 表同名列）；源行数>产出行数→缺行缺口。
    """
    source = detect_source_columns(path)
    produced = file_out.get(os.path.normcase(os.path.abspath(path)),
                            {"rows": 0, "proxies": set(), "columns": set(), "sheets": {}})
    seen, target_columns = set(), []
    for column in source["columns"]:
        if column["proxy"] not in PUBLISHED_NAMES:
            continue
        key = (_norm_col(column["name"]), column.get("sheet", ""))
        if key in seen:
            continue
        seen.add(key)
        target_columns.append(column)
    sheets = produced.get("sheets", {})

    def produced_cols_for(column):
        sheet = column.get("sheet")
        if sheet:
            return {_norm_col(c) for c in sheets.get(sheet, {}).get("columns", set())}
        return {_norm_col(c) for c in produced["columns"]}

    gaps = []
    missing_by_sheet = defaultdict(list)
    for column in target_columns:
        if _norm_col(column["name"]) not in produced_cols_for(column):
            missing_by_sheet[column.get("sheet", "")].append(column)
    for sheet, missing in missing_by_sheet.items():
        gaps.append({"type": "column_missing" if produced["rows"] else "zero_output",
                     "columns": [c["name"] for c in missing],
                     "proxies": sorted({c["proxy"] for c in missing}),
                     "column_proxies": {c["name"]: c["proxy"] for c in missing},
                     "seasonality": [c["seasonality"] for c in missing if c["seasonality"]],
                     "sheets": [sheet] if sheet else []})
    # 行级对账：仅统计可提取行（有效指标值+可解析年代），过滤合理排除（缺值/缺年代）
    extractable = _count_extractable_rows(path, source)
    if isinstance(extractable, dict):
        for sheet, nrows in extractable.items():
            if not any(c.get("sheet") == sheet for c in target_columns):
                continue
            out_rows = sheets.get(sheet, {}).get("rows", 0)
            if nrows > out_rows:
                gaps.append({"type": "row_missing", "sheet": sheet,
                             "source_rows": nrows, "output_rows": out_rows})
    elif extractable is not None and extractable > produced["rows"] and target_columns:
        gaps.append({"type": "row_missing", "source_rows": extractable, "output_rows": produced["rows"]})
    if target_columns and source["chronology"]["type"] == "relative":
        gaps.append({"type": "relative_age_needs_anchor", "profile": source["profile"],
                     "chronology_column": source["chronology"]["column"],
                     "columns": [c["name"] for c in target_columns],
                     "column_proxies": {c["name"]: c["proxy"] for c in target_columns},
                     "note": "相对年代目标表，保留剖面与待核定年锚点"})
    return {"file": path, "source": source, "output_rows": produced["rows"],
            "output_proxies": sorted(produced["proxies"]), "gaps": gaps}


def _folder_output(run_dir):
    """对账产出侧：按文件夹聚合 consolidated 的发表指标/年代/岩芯标识。"""
    output = defaultdict(lambda: {"rows": 0, "proxies": set(), "year": 0, "core": 0})
    combined = Path(run_dir) / "consolidated/all_data_combined.csv"
    columns = {name: column for name, column in PUBLISHED_PROXIES.values()}
    if not combined.is_file():
        return output
    with combined.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            folder = os.path.dirname(row.get("source_file", ""))
            if not folder:
                continue
            entry = output[folder]
            entry["rows"] += 1
            for name, column in columns.items():
                if row.get(column) not in (None, "", "NA"):
                    entry["proxies"].add(name)
            if row.get("year_CE") not in (None, "", "NA"):
                entry["year"] += 1
            if row.get("source_core_id") not in (None, "", "NA"):
                entry["core"] += 1
    return output


def _manifest_folders(run_dir):
    """从 manifest 取每个研究目录的文件清单（文件级行）。"""
    folders = defaultdict(list)
    manifest = Path(run_dir) / "input_manifest.csv"
    with manifest.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            if row.get("sheet"):
                continue
            folders[os.path.dirname(row["file"])].append(
                {"file": row["file"], "status": row.get("status", ""), "reason": row.get("reason", "")})
    return folders


def _route_gap(gaps):
    """缺口分级路由：确定性源证据修复优先，跨表/缺解释升级 Flash/Max。"""
    types = {g["type"] for g in gaps}
    if "unpublished_proxy" in types:
        return "schema_decision"  # Mg/Ca 等已识别未发表，交用户决策
    if "relative_age_needs_anchor" in types:
        return "flash_then_max"  # 相对年代定年锚点需跨附件判断
    if types <= {"chronology"}:
        return "deterministic_date_then_flash"  # 月份格式/年代基准
    if types <= {"core_id"}:
        return "deterministic_core_then_flash"  # 并列岩芯/样品标识
    if "zero_output" in types or "proxy_missing" in types or "column_missing" in types:
        return "deterministic_mapping_then_max"  # 列映射/作者合成列/并列季节列/跨表
    return "flash_then_max"


def audit_folder(folder, files, source_root, output, file_output=None):
    """对单个研究目录做源证据 vs 产出对账，返回处置 + 缺口 + 路由。

    按附件判材料：只把非硬非珊瑚附件的指标计入目标线索，故"生长率文件夹里另有
    Sr/Ca 附件"会按该 Sr/Ca 表的证据处理，而非整目录一刀切排除。
    file_output 提供时再做列级对账，发现文件夹有产出但个别列/文件漏提（部分漏提）。
    """
    target_proxies, source_chrono, source_core = set(), False, False
    blobs, scanned = [], 0
    for item in files:
        if os.path.splitext(item["file"])[1].lower() not in ATTACHMENT_SUFFIXES:
            continue
        clues = detect_source_clues(str(source_root / item["file"]))
        if clues["blob"].strip():
            blobs.append(clues["blob"])
        scanned += 1
        if clues["non_coral"]:
            continue  # 硬非珊瑚附件（有孔虫/牡蛎/石笋）不计入目标线索
        target_proxies.update(clues["proxies"])
        source_chrono = source_chrono or clues["chronology"]
        source_core = source_core or clues["core_id"]
    out = output.get(folder, {"rows": 0, "proxies": set(), "year": 0, "core": 0})
    base = {"folder": folder, "scanned_attachments": scanned,
            "source_proxies": sorted(target_proxies), "source_chronology": source_chrono,
            "source_core_id": source_core, "output_rows": out["rows"],
            "output_proxies": sorted(out["proxies"]), "output_year": out["year"],
            "output_core": out["core"]}

    # 不相干仅当"产出无目标指标 且 源无目标线索"。产出已发表目标指标即视为有数据，
    # 即便源表头扫描漏检也不误判不相干（对账以产出为准）。
    if not out["proxies"] and not target_proxies:
        scope, proxies, reason = classify_scope(" ".join(blobs))
        return {**base, "disposition": "irrelevant", "scope": scope,
                "scope_proxies": proxies, "reason": reason, "gaps": [], "route": "exclude"}

    gaps = []
    unpublished = target_proxies - PUBLISHED_NAMES
    publishable = target_proxies & PUBLISHED_NAMES
    if not out["proxies"]:
        if unpublished and not publishable:
            gaps.append({"type": "unpublished_proxy", "proxies": sorted(unpublished)})
        if publishable:
            gaps.append({"type": "zero_output", "proxies": sorted(publishable)})
    else:
        missing = publishable - out["proxies"]
        if missing:
            gaps.append({"type": "proxy_missing", "proxies": sorted(missing)})
        if source_chrono and out["year"] == 0:
            gaps.append({"type": "chronology"})
        if source_core and out["core"] == 0:
            gaps.append({"type": "core_id"})
    # 列级对账（第二轮）：文件夹即便有产出，仍逐文件发现漏提的目标列与相对年代目标表
    file_gaps = []
    if file_output is not None:
        for item in files:
            if os.path.splitext(item["file"])[1].lower() not in ATTACHMENT_SUFFIXES:
                continue
            result = audit_file_columns(str(source_root / item["file"]), file_output)
            if result["gaps"]:
                file_gaps.append(result)
    if not gaps and not file_gaps:
        return {**base, "disposition": "covered", "gaps": [], "file_gaps": [], "route": "none",
                "reason": "源目标线索已由产出覆盖"}
    all_gaps = gaps + [g for result in file_gaps for g in result["gaps"]]
    return {**base, "disposition": "gap", "gaps": gaps, "file_gaps": file_gaps,
            "route": _route_gap(all_gaps),
            "reason": "源有目标线索但产出缺失/暂缓：" + ", ".join(sorted({g["type"] for g in all_gaps}))}


def _find_pdf(folder):
    try:
        for name in sorted(os.listdir(folder)):
            if name.lower().endswith(".pdf"):
                return os.path.join(folder, name)
    except OSError:
        pass
    return None


def flash_locate_evidence(folder_abs, gap, cache=None):
    """Flash 证据定位器：有目标线索仍缺解释时，找出具体文件/工作表/页码+支持列义
    和年代的原文。仅在确定性修复不足时调用（不是逐篇略读判 scope）。"""
    from app.utils.pdf_direct_processor import extract_text_from_pdf
    pdf = _find_pdf(folder_abs)
    paper_text = ""
    if pdf:
        try:
            paper_text = extract_text_from_pdf(pdf)[:6000]
        except Exception as exc:
            logger.debug("Flash 定位 PDF 读取失败 %s: %s", pdf, exc)
    attachments = []
    for name in sorted(os.listdir(folder_abs)):
        if os.path.splitext(name)[1].lower() in ATTACHMENT_SUFFIXES:
            header = _read_header_blob(os.path.join(folder_abs, name), chars=1500)
            if header.strip():
                attachments.append(f"[{name}]\n{header}")
    prompt = (
        "你是珊瑚骨骼地球化学数据库的证据定位员。该研究目录含目标指标（Sr/Ca/Ba/Ca/δ18O/δ13C/SST）"
        "线索，但提取存在缺口。请定位支持列义与年代的具体证据。\n"
        f"缺口：{json.dumps(gap, ensure_ascii=False)}\n\n"
        f"【论文标题/摘要/方法（前6000字符）】\n{paper_text}\n\n"
        f"【各附件表头/变量定义】\n" + "\n\n".join(attachments)[:6000] + "\n\n"
        '只输出 JSON：{"file":"具体附件名","sheet":"工作表名或null","page":"PDF页码或null",'
        '"column_meaning":"支持列义的逐字原文","chronology":"支持年代的逐字原文",'
        '"resolution":"deterministic|needs_max","reason":"中文简述"}'
    )
    cache_headers = [AUDIT_VERSION, "locate", _sha256(pdf) if pdf else os.path.basename(folder_abs)]
    context = json.dumps(gap, ensure_ascii=False, sort_keys=True)
    cached = cache.get(cache_headers, context) if cache else None
    if cached and isinstance(cached.get("response"), dict):
        return {**cached["response"], "call_status": "cached"}
    try:
        raw = call_llm(model="flash", question=prompt, temperature=0.0, enable_thinking=False)
    except Exception as exc:
        logger.warning("Flash 证据定位调用失败 %s: %s", folder_abs, exc)
        return {"call_status": "error", "reason": str(exc)[:200]}
    if not raw:
        return {"call_status": "empty_response", "reason": "Flash 返回空（配额/超时），保留待重试"}
    match = re.search(r"\{.*\}", raw, re.S)
    result = {}
    if match:
        try:
            result = json.loads(match.group(0))
        except json.JSONDecodeError:
            result = {}
    if not result:
        return {"call_status": "parse_error", "reason": "Flash 返回无法解析，保留待重试"}
    if cache:
        cache.set(cache_headers, {"response": result}, context)
    return {**result, "call_status": "success"}


def _decision_row(file, action, note, digest):
    return {"file": file, "sheet": "", "column": "", "decision": "",
            "review_decision": action, "confirmed_unit": "", "review_note": note[:1000],
            "review_id": "", "source_sha256": digest, "evidence_status": "verified",
            "decision_origin": "coverage_audit", "selected_column": "",
            "coord_lat": "", "coord_lon": ""}


PROXY_TO_FIELD = {name: field for field, (name, _) in PUBLISHED_PROXIES.items()}


def build_max_handoff(results, source_root):
    """把需跨附件判断的缺口（Flash resolution=needs_max 或 Max 路由）转为可执行复核队列项：
    action 用 JUDGE_HANDOFF_ACTIONS 使现有 review 入口归为 confirm_mapping 组（而非跳过），
    decision 给目标字段，携带 Flash 定位的文件/表名/页码/原文/调用状态。"""
    source_root = Path(source_root).resolve()
    items = {}
    for result in results:
        if result["disposition"] != "gap":
            continue
        flash = result.get("flash_evidence", {}) or {}
        if result["route"] not in MAX_ROUTES and flash.get("resolution") != "needs_max":
            continue
        gap_sources = result.get("file_gaps") or [{"file": "", "gaps": result["gaps"]}]
        for source in gap_sources:
            path = Path(source_root, source.get("file", "")).resolve()
            if not path.is_relative_to(source_root) or not path.is_file():
                continue
            file = str(path.relative_to(source_root))
            digest = _sha256(path)
            if not digest:
                continue
            for gap in source["gaps"]:
                if gap["type"] not in {"zero_output", "column_missing", "proxy_missing",
                                       "relative_age_needs_anchor"}:
                    continue
                sheets = gap.get("sheets") or [""]
                column_proxies = gap.get("column_proxies", {})
                for column in gap.get("columns", []):
                    proxy = column_proxies.get(column) or (gap.get("proxies") or [_header_to_proxy(column)])[0]
                    field = PROXY_TO_FIELD.get(proxy, "")
                    sheet = sheets[0] or os.path.basename(file)
                    reason = f"覆盖审计缺口[{gap['type']}]需 Max 跨附件复核。"
                    if flash:
                        reason += (f"Flash定位 file={flash.get('file')} sheet={flash.get('sheet')} "
                                   f"page={flash.get('page')} 列义={flash.get('column_meaning')} "
                                   f"年代={flash.get('chronology')} resolution={flash.get('resolution')} "
                                   f"调用={flash.get('call_status')}。")
                    if gap.get("note"):
                        reason += gap["note"]
                    if column and field:
                        items.setdefault((file, sheet, column), {
                            "file": file, "sheet": sheet, "column": column,
                            "action": "暂缓映射，复判未通过", "reason": reason[:1000],
                            "decision": field, "source_sha256": digest, "review_decision": "",
                        })
    return list(items.values())


def pending_handoff_rows(run_dir, source_root, existing):
    """只交接尚未入队且有完整源定位的覆盖候选。"""
    run_dir = Path(run_dir)
    source_root = Path(source_root).resolve()
    handoff = run_dir / "review_workbench" / MAX_HANDOFF
    if not handoff.is_file():
        return

    def location(row):
        return (os.path.normcase(str((source_root / row["file"]).resolve())),
                row.get("sheet", "").strip().casefold(), row.get("column", "").strip())

    keys = {location(row) for row in existing if row.get("file") and row.get("column")}
    with handoff.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            if not row.get("file") or not row.get("sheet") or not row.get("column") or not row.get("decision"):
                continue
            key = location(row)
            if key in keys:
                continue
            keys.add(key)
            yield row


def audit_run(run_dir, source_root=".", folders=None, use_flash=False, max_workers=5):
    """按研究目录对账提取覆盖，写审计账本 + scope 裁决 + 汇总。返回汇总 dict。

    folders=None 审计全部目录；传入列表则只审计指定目录（小样验证）。
    use_flash=True 时对 gap 目录调用 Flash 证据定位（默认关，先验证对账）。
    """
    run_dir = Path(run_dir).resolve()
    source_root = Path(source_root).resolve()
    output = _folder_output(run_dir)
    file_output = _file_output(run_dir)
    all_folders = _manifest_folders(run_dir)
    selected = sorted(all_folders) if folders is None else [f for f in folders if f in all_folders]
    cache = LLMCache(cache_dir=str(source_root / "cache")) if use_flash else None
    logger.info("覆盖审计：%d 个研究目录（产出侧 %d 个），flash=%s", len(selected), len(output), use_flash)

    def work(folder):
        result = audit_folder(folder, all_folders[folder], source_root, output, file_output)
        if use_flash and result["disposition"] == "gap" and result["route"] != "schema_decision":
            all_gaps = result["gaps"] + [g for fg in result.get("file_gaps", []) for g in fg["gaps"]]
            evidence = flash_locate_evidence(str(source_root / folder), all_gaps, cache)
            result["flash_evidence"] = evidence
            result["flash_call_status"] = evidence.get("call_status", "not_called")
        return result

    if max_workers > 1 and len(selected) > 1:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            results = list(pool.map(work, selected))
    else:
        results = [work(folder) for folder in selected]

    counts = Counter(r["disposition"] for r in results)
    gap_routes = Counter(r["route"] for r in results if r["disposition"] == "gap")
    # irrelevant 目录 → 逐文件 FILE_EXCLUDE 证据裁决（跳过终局排除）
    decision_rows = []
    for result in results:
        if result["disposition"] != "irrelevant" or result["scope"] in {"ambiguous", "in_schema"}:
            continue
        for item in all_folders[result["folder"]]:
            if any(m in item["reason"] for m in FINAL_EXCLUSION_MARKERS):
                continue
            path = str(source_root / item["file"])
            digest = _sha256(path)
            if not digest or os.path.splitext(item["file"])[1].lower() not in ATTACHMENT_SUFFIXES:
                continue
            note = (f"[{result['scope']}] {result['reason']}｜指标/材料: "
                    f"{', '.join(result['scope_proxies']) or '无'}")
            decision_rows.append(_decision_row(item["file"], "exclude", note, digest))

    out_dir = run_dir / "review_workbench"
    out_dir.mkdir(parents=True, exist_ok=True)
    ledger_fields = ["folder", "disposition", "route", "source_proxies", "source_chronology",
                     "source_core_id", "output_rows", "output_proxies", "output_year",
                     "output_core", "gaps", "file_gaps", "flash_call_status", "flash_evidence",
                     "scope", "reason", "scanned_attachments"]
    with (out_dir / AUDIT_LEDGER).open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=ledger_fields, extrasaction="ignore")
        writer.writeheader()
        for result in results:
            row = {**result, "source_proxies": ";".join(result["source_proxies"]),
                   "output_proxies": ";".join(result["output_proxies"]),
                   "gaps": json.dumps(result["gaps"], ensure_ascii=False),
                   "file_gaps": json.dumps(result.get("file_gaps", []), ensure_ascii=False),
                   "flash_evidence": json.dumps(result.get("flash_evidence", {}), ensure_ascii=False),
                   "flash_call_status": result.get("flash_call_status", ""),
                   "scope": result.get("scope", "")}
            writer.writerow(row)
    decision_fields = ["file", "sheet", "column", "decision", "review_decision", "confirmed_unit",
                       "review_note", "review_id", "source_sha256", "evidence_status", "decision_origin",
                       "selected_column", "coord_lat", "coord_lon"]
    with (out_dir / DECISIONS).open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=decision_fields)
        writer.writeheader()
        writer.writerows(decision_rows)
    # Flash 证据账本 → Max 复核交接：需跨附件判断的缺口转复核队列项
    handoff_items = build_max_handoff(results, source_root)
    handoff_fields = ["file", "sheet", "column", "action", "reason", "decision",
                      "source_sha256", "review_decision"]
    with (out_dir / MAX_HANDOFF).open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=handoff_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(handoff_items)
    summary = {"run_dir": run_dir.name, "audit_version": AUDIT_VERSION,
               "audited_folders": len(selected), "dispositions": dict(counts),
               "gap_routes": dict(gap_routes), "exclude_decision_rows": len(decision_rows),
               "max_handoff_items": len(handoff_items),
               "gap_folders": [r["folder"] for r in results if r["disposition"] == "gap"],
               "irrelevant_folders": [r["folder"] for r in results if r["disposition"] == "irrelevant"],
               "ledger_csv": str(out_dir / AUDIT_LEDGER), "decisions_csv": str(out_dir / DECISIONS),
               "max_handoff_csv": str(out_dir / MAX_HANDOFF)}
    (out_dir / SUMMARY).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("覆盖审计完成：%s｜缺口路由 %s｜排除裁决 %d 行｜Max 交接 %d 项",
                dict(counts), dict(gap_routes), len(decision_rows), len(handoff_items))
    return summary
