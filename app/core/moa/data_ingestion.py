#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
数据接入模块（Data Ingestion Module）

功能：
1. 读取多种格式的数据文件（Excel、CSV、Markdown、NOAA txt等）
2. 对数据进行初步清洗和标准化
3. 记录数据接入的完整过程

技术栈：
- pandas: 数据处理
- openpyxl: Excel文件读取
- logging: 日志记录
"""

import logging
import csv
import io
import re
from zipfile import BadZipFile
import pandas as pd
import numpy as np
import os
from typing import List, Dict, Any, Optional
from pathlib import Path
from app.utils.appendix_processor import get_table_caption_for_word_table
from app.utils.data_trace_logger import get_tracer
from app.utils.review_queue import add_review_item, capture_review_items, layout_review_decision
from docx import Document
from docx.opc.exceptions import PackageNotFoundError
from app.core.moa.unit_converter import canonical_unit, finite_number, measurement_parts
from app.core.moa.spatiotemporal_standard import calendar_date, calendar_date_order, time_basis
from app.core.moa.table_layout import propose_layout, valid_layout

# 获取logger和tracer
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


class DataIngestionModule:
    """数据接入模块"""
    
    def __init__(self, enable_layout_model=False, layout_cache=None):
        """初始化数据接入模块"""
        self.tracer = get_tracer()
        self.enable_layout_model = enable_layout_model
        self.layout_cache = layout_cache
        # 表结构裁决（confirm_layout）回灌：由工作流注入版本绑定的裁决与摘要函数。
        self.review_decisions = {}
        self.source_digest = None
        self.applied_structural_reviews = None
    
    WORKBOOK_EXTENSIONS = {".xlsx", ".xlsm", ".xls", ".ods"}
    ATTACHMENT_EXTENSIONS = WORKBOOK_EXTENSIONS | {".docx", ".csv", ".tsv", ".txt", ".tab", ".md"}
    SAMPLE_LABEL = re.compile(r"(?:core|sample|岩芯|样品)(?:\s*[:#：]\s*|\s+)([\w.-]+)", re.I)

    def read_file(self, file_path, inventory=None):
        """读原始工作表或表格，保留工作表名和源行，不写入同目录转换副本。"""
        try:
            suffix = Path(file_path).suffix.lower()
            if suffix in self.WORKBOOK_EXTENSIONS:
                return self._read_excel(file_path, inventory)
            if suffix == ".docx":
                document = Document(file_path)
                return [item for number, table in enumerate(document.tables, 1)
                        for item in self._read_word_table(document, table, number, file_path)]
            if suffix in {".csv", ".tsv", ".txt", ".tab"}:
                return self._read_delimited(file_path, suffix)
            if suffix == ".md":
                return self._read_markdown(file_path)
            if suffix == ".pdf":
                return self._read_pdf_tables(file_path, inventory)
            return []
        except (ImportError, OSError, ValueError, csv.Error, BadZipFile, PackageNotFoundError) as exc:
            add_review_item({"file": file_path, "status": "failed", "action": "暂缓读取", "reason": f"附件读取失败: {exc}"})
            logging.warning("附件读取失败 %s: %s", file_path, exc)
            return []

    def _read_excel(self, file_path, inventory=None):
        suffix = Path(file_path).suffix.lower()
        engine = {".xlsx": "openpyxl", ".xlsm": "openpyxl", ".xls": "xlrd", ".ods": "odf"}[suffix]
        tables = []
        with pd.ExcelFile(file_path, engine=engine) as workbook:
            names = {name.strip().casefold() for name in workbook.sheet_names}
            template = "proxyandreconstructioninfo" in names or {"about", "example-metadata"} <= names
            metadata_sheet = next((sheet for sheet in workbook.sheet_names
                                   if sheet.strip().casefold() == "metadata"), None)
            workbook_notes = ""
            if metadata_sheet and not template:
                metadata = workbook.parse(metadata_sheet, header=None, dtype=str, nrows=60).fillna("")
                workbook_notes = "\n".join(" | ".join(row) for row in metadata.values.tolist()
                                           if any(row))
            for sheet in workbook.sheet_names:
                name = sheet.strip().casefold()
                if re.fullmatch(r"cite(?:d)? other data(?: in figure .+)?", name):
                    if inventory is not None:
                        inventory.append({"sheet_name": sheet, "row_count": 0, "status": "excluded",
                                          "reason": "作者将工作表标为引用其他研究的数据"})
                    continue
                if (name in {"about", "metadata", "example-metadata", "proxyandreconstructioninfo"}
                        or template and (name == "chronology" or re.match(r"example[ -]", name))):
                    if inventory is not None:
                        inventory.append({"sheet_name": sheet, "row_count": 0, "status": "context",
                                          "reason": "已识别为元数据、说明或模板工作表"})
                    continue
                try:
                    grid = workbook.parse(sheet, header=None, dtype=str, keep_default_na=False)
                    blocks = self._inspect_attachment_table(grid, file_path, sheet, workbook=True,
                                                            workbook_notes=workbook_notes,
                                                            merge_loader=lambda: self._excel_merges(workbook, sheet))
                    if not blocks and inventory is not None:
                        inventory.append({"sheet_name": sheet, "row_count": 0, "status": "review",
                                          "reason": "工作表为空或未识别到观测块"})
                    tables.extend(blocks)
                except Exception as exc:
                    add_review_item({"file": file_path, "sheet": sheet, "status": "failed",
                                     "action": "暂缓读取", "reason": f"工作表读取失败: {exc}"})
        return tables

    @staticmethod
    def _excel_merges(workbook, sheet):
        """读取 xlsx/xlsm 的合并范围，旧格式保留原始单元格网格。"""
        from xml.etree.ElementTree import iterparse
        from openpyxl.utils.cell import range_boundaries

        book = workbook.book
        archive = getattr(book, "_archive", None)
        if archive is None:
            return []
        path = getattr(book[sheet], "_worksheet_path", None)
        if not path:
            return []
        ranges = []
        with archive.open(path) as stream:
            for _, element in iterparse(stream, events=("end",)):
                if element.tag.rsplit("}", 1)[-1] == "mergeCell":
                    left, top, right, bottom = range_boundaries(element.attrib["ref"])
                    ranges.append([top, bottom, left, right])
                element.clear()
        return ranges

    def _read_word_table(self, document, table, number, file_path):
        positions = {}
        grid = []
        for row_number, row in enumerate(table.rows, 1):
            before = getattr(row, "grid_cols_before", 0)
            values = [""] * before
            for column, cell in enumerate(row.cells, before + 1):
                values.append(cell.text.strip())
                positions.setdefault(cell._tc, []).append((row_number, column))
            grid.append(values)
        merges = [[min(r for r, _ in cells), max(r for r, _ in cells),
                   min(c for _, c in cells), max(c for _, c in cells)]
                  for cells in positions.values() if len(cells) > 1]
        caption = get_table_caption_for_word_table(document, table, number - 1, for_filename=False)
        return self._inspect_attachment_table(pd.DataFrame(grid), file_path, f"table_{number}", caption, merges=merges)

    def _confirmed_layout(self, file_path, sheet):
        """查找已核验、版本绑定的表结构裁决（confirm_layout），无则返回 (None, None)。"""
        if not self.review_decisions or not self.source_digest:
            return None, None
        try:
            key, decision = layout_review_decision(self.review_decisions, file_path, sheet, self.source_digest)
        except OSError:
            return None, None
        return key, decision

    def _inspect_attachment_table(self, grid, file_path, sheet, caption="", workbook=False,
                                  workbook_notes="", merges=(), merge_loader=None):
        # 回灌优先：已有核验通过的表结构裁决时，直接按裁决坐标从原表读取，不再调用模型。
        layout_key, confirmed = self._confirmed_layout(file_path, sheet)
        if confirmed is not None:
            try:
                resolved_merges = merge_loader() if merge_loader else merges
            except (OSError, KeyError, ValueError):
                resolved_merges = []
            planned = self._apply_layout(grid, confirmed["blocks"], file_path, sheet, caption,
                                         workbook, workbook_notes, resolved_merges)
            if planned:
                if self.applied_structural_reviews is not None:
                    self.applied_structural_reviews.add(layout_key)
                add_review_item({"file": file_path, "sheet": sheet, "action": "已应用人工裁决",
                                 "review_decision": "confirm_layout", "reason": confirmed["note"],
                                 "source_sha256": confirmed["source_sha256"]})
                return planned
            add_review_item({"file": file_path, "sheet": sheet, "action": "暂缓表格",
                             "reason": "已核验表结构裁决回灌后仍无可用观测块，保留待重试"})
        with capture_review_items() as rule_reviews:
            try:
                tables = self._prepare_table(grid, file_path, sheet, caption, workbook=workbook, workbook_notes=workbook_notes)
            except (IndexError, KeyError, ValueError) as exc:
                tables = []
                add_review_item({"file": file_path, "sheet": sheet, "action": "暂缓表格", "reason": str(exc)})
        if self.enable_layout_model and len(grid) >= 2 and self._needs_layout_model(grid, tables, merges):
            try:
                merges = merge_loader() if merge_loader else merges
            except (OSError, KeyError, ValueError) as exc:
                logging.warning("无法读取合并范围 %s: %s", sheet, exc)
                merges = []
            hints = sorted({table["source_header_line"] for table in tables})
            plan = propose_layout(grid, sheet, caption or "", merges, hints, self.layout_cache)
            planned = self._apply_layout(grid, plan, file_path, sheet, caption, workbook, workbook_notes, merges)
            if planned:
                self._review_layout_coverage(file_path, sheet, tables, planned)
                return planned
            if tables:
                add_review_item({"file": file_path, "sheet": sheet, "action": "复核表结构",
                                 "reason": "模型未给出可在原表核验的结构，保留规则识别结果"})
        for item in rule_reviews:
            add_review_item(item)
        return tables

    def _review_layout_coverage(self, file_path, sheet, rule_tables, planned_tables):
        """根据实际接纳的原表坐标核验覆盖，未通过校验的块不计入覆盖。"""
        accepted = [(set(table["source_rows"]), set(table["source_column_positions"].values()))
                    for table in planned_tables]
        missing = set()
        for table in rule_tables:
            positions = table["source_column_positions"]
            date = [col for col in positions if self._is_time_header(col) or re.search(r"\btime\b|时间", col, re.I)]
            proxy = [col for col in positions if re.search(r"sr\s*[/_]?\s*ca|ba\s*[/_]?\s*ca|(?:δ|d)(?:18o|13c)|\bsst\b|锶钙比|钡钙比", col, re.I)]
            if not date or not proxy:
                continue
            date_orders = {col: calendar_date_order(row.get(col) for row in table["data"] if pd.notna(row.get(col)))
                           for col in date}
            for line, row in zip(table["source_rows"], table["data"]):
                if not any(finite_number(row.get(col)) is not None or calendar_date(row.get(col), date_orders[col])
                           for col in date):
                    continue
                if any(measurement_parts(row.get(col))[0] is not None and not any(
                    line in lines and positions[col] in columns for lines, columns in accepted
                ) for col in proxy):
                    missing.add(line)
        if missing:
            add_review_item({"file": file_path, "sheet": sheet, "source_line": min(missing),
                             "action": "暂缓未覆盖区域", "reason": f"模型结构未覆盖规则识别的 {len(missing)} 个可能观测行"})

    def _needs_layout_model(self, grid, tables, merges):
        if len(tables) != 1 or merges:
            return True
        table, = tables
        headers = table["headers"]
        header_line = table["source_header_line"]
        # 只有单层表头、已定位的时间与指标，且没有未命名的观测列，才直接使用规则结构。
        time = r"\b(?:year|age|date|time|CE|AD|BP|BCE)\b|年代|日期|年份"
        proxy = r"sr\s*[/_ ]?\s*ca|ba\s*[/_ ]?\s*ca|(?:δ|d(?:elta)?)\s*(?:18o|13c)|\bsst\b|锶钙比|海表温度"
        return (any(header.startswith("Unnamed:") for header in headers)
                or not any(re.search(time, header.replace("_", " "), re.I) for header in headers)
                or not any(re.search(proxy, header, re.I) for header in headers)
                or (header_line > 1 and len([label for label in self._labels(grid.iloc[header_line - 2]) if label]) > 1)
                or any(item.get("name", "").casefold() not in {h.casefold() for h in headers}
                       for item in table.get("variables", []) if re.search(time, item.get("name", "").replace("_", " "), re.I)))

    def _apply_layout(self, grid, plan, file_path, sheet, caption, workbook, workbook_notes, merges):
        if not plan:
            return []
        row_count, column_count = grid.shape
        # 逐块独立核验：坐标合法、单元格归属无歧义且通过表头/观测/单位校验的块照常从原表提取；
        # 非法、重叠或校验失败的块单独跳过并留痕，不连累有效主表（宁放勿杀，不静默丢行）。
        valid = [valid_layout(block, row_count, column_count) for block in plan]
        conflicted = [False] * len(plan)
        for i in range(len(plan)):
            if not valid[i]:
                continue
            for j in range(i + 1, len(plan)):
                if not valid[j]:
                    continue
                a, b = plan[i], plan[j]
                if (a["first_data_row"] <= b["last_data_row"] and b["first_data_row"] <= a["last_data_row"]
                        and not set(a["columns"]).isdisjoint(b["columns"])):
                    conflicted[i] = conflicted[j] = True
        tables = []
        for number, block in enumerate(plan, 1):
            block_label = sheet if len(plan) == 1 else f"{sheet}#part_{number}"
            if not valid[number - 1]:
                self._flag_layout_block(file_path, block_label, block, "模型坐标越界或非法")
                continue
            if conflicted[number - 1]:
                self._flag_layout_block(file_path, block_label, block, "模型结构块与其他块重叠，单元格归属不明")
                continue
            start, end = block["first_data_row"], block["last_data_row"]
            columns = block["columns"]
            offsets = [column - 1 for column in columns]
            header_rows = block["header_rows"]
            header = []
            for column in columns:
                labels = []
                for row in header_rows:
                    value = grid.iat[row - 1, column - 1]
                    if pd.isna(value) or not str(value).strip():
                        for top, bottom, left, right in merges:
                            if top <= row <= bottom and left <= column <= right and top in header_rows:
                                value = grid.iat[top - 1, left - 1]
                                break
                    if pd.notna(value) and str(value).strip():
                        labels.append(str(value).strip())
                header.append(" | ".join(dict.fromkeys(labels)))
            if sum(bool(name) and finite_number(name) is None for name in header) < 2:
                self._flag_layout_block(file_path, block_label, block, "模型表头不足两个非数值列名")
                continue
            data = grid.iloc[start - 1:end, offsets].copy()
            for top, bottom, left, right in merges:
                for row in range(max(start, top), min(end, bottom) + 1):
                    for column in columns:
                        if left <= column <= right and (row, column) != (top, left):
                            data.loc[row - 1, column - 1] = None
            if not any(self._observation_row(self._labels(row)) for row in data.head(8).itertuples(index=False, name=None)):
                self._flag_layout_block(file_path, block_label, block, "模型数据区前8行无观测值")
                continue
            fragments = [pd.DataFrame([header], index=[header_rows[-1] - 1], columns=offsets)]
            unit_row = block.get("unit_row")
            if unit_row is not None:
                unit_grid = grid.iloc[unit_row - 1:unit_row, offsets]
                if not self._is_unit_row(unit_grid.iloc[0].tolist()):
                    self._flag_layout_block(file_path, block_label, block, "模型单位行未被识别")
                    continue
                fragments.append(unit_grid)
            fragments.append(data)
            rows = self._prepare_block(pd.concat(fragments), file_path, sheet, block_label, 0, caption or "",
                                       False, workbook_notes, False, None, None, {}, layout_confirmed=True)
            if not rows:
                self._flag_layout_block(file_path, block_label, block, "模型结构块清洗后无数据")
                continue
            table, = rows
            table["source_header_rows"] = header_rows
            table["layout_route"] = "model_coordinates"
            for name, position in table["source_column_positions"].items():
                candidates = name.split(" | ") + [str(grid.iat[top - 1, left - 1]).strip()
                    for top, bottom, left, right in merges
                    if header_rows[0] - 4 <= top <= header_rows[-1] and bottom < start and left <= position <= right]
                samples = {match.group(1) for part in candidates if (match := self.SAMPLE_LABEL.fullmatch(part))}
                if len(samples) == 1:
                    table["sample_by_column"][name] = samples.pop()
            assigned = set(table["sample_by_column"].values())
            if len(assigned) == 1:
                table["sample_id"], = assigned
            table["table_description"] = "\n".join(filter(None, (table["table_description"],
                "表头上方原文:\n" + "\n".join(" | ".join(self._labels(row)) for row in
                    grid.iloc[max(0, header_rows[0] - 9):header_rows[0] - 1].itertuples(index=False, name=None)))))
            if workbook:
                definition_index, marker, schema = self._workbook_definitions(grid)
                definitions = self._declared_variables(grid, definition_index, marker, schema, header_rows[0] - 1)
                table["variables"] = list(definitions.values())
                table["layout_definitions"] = {name: definitions[row] for name, position in table["source_column_positions"].items()
                                               if (row := block.get("definition_rows", {}).get(str(position))) in definitions}
            tables.append(table)
        return tables

    @staticmethod
    def _flag_layout_block(file_path, block_label, block, reason):
        """跳过的模型结构块单独留痕，附原表坐标供人工恢复，不静默丢数据。"""
        block = block if isinstance(block, dict) else {}
        start = block.get("first_data_row")
        add_review_item({
            "file": file_path, "sheet": block_label,
            "source_line": start if type(start) is int else None,
            "action": "暂缓模型结构块",
            "reason": f"{reason}（原表 行{start}-{block.get('last_data_row')} 列{block.get('columns')}）",
        })

    def _read_delimited(self, file_path, suffix):
        with open(file_path, "rb") as source:
            bom = source.read(2)
        encoding = "utf-16" if bom in {b"\xff\xfe", b"\xfe\xff"} else "utf-8-sig"
        try:
            return self._read_delimited_text(file_path, suffix, encoding)
        except UnicodeDecodeError:
            if encoding != "utf-8-sig":
                raise
            return self._read_delimited_text(file_path, suffix, "gb18030")

    def _read_delimited_text(self, file_path, suffix, encoding):
        with open(file_path, encoding=encoding, newline="") as stream:
            probe = stream.read(8192)
            stream.seek(0)
            if suffix in {".tsv", ".tab"}:
                delimiter = "\t"
            else:
                delimiter = self._detect_delimiter(probe)
            rows = []
            source_rows = []
            reader = csv.reader(stream, delimiter=delimiter, strict=True)
            while True:
                line = reader.line_num + 1
                row = next(reader, None)
                if row is None:
                    break
                rows.append(row)
                source_rows.append(line)
        if not rows:
            return []
        # 允许短标题或注释行，但保留每个数据单元格的原始位置。
        return self._prepare_table(pd.DataFrame(rows, index=source_rows), file_path, "default",
                                   delimited=True, decimal_comma=delimiter == ";")

    @staticmethod
    def _detect_delimiter(probe):
        excel = re.match(r"\s*sep=([,;\t])\s*(?:\r?\n|$)", probe, re.I)
        if excel:
            return excel.group(1)
        try:
            return csv.Sniffer().sniff(probe, delimiters="\t,;").delimiter
        except csv.Error:
            # 带标题或表注的 CSV 往往行宽不齐，使用第一条实际分隔的行。
            for line in probe.splitlines():
                widths = {sep: len(next(csv.reader([line], delimiter=sep))) for sep in (",", "\t", ";")}
                if max(widths.values()) > 1:
                    return max(widths, key=widths.get)
            return ","

    @staticmethod
    def _workbook_definitions(grid):
        keys = {"short_name": "name", "what": "quantity", "material": "material", "error": "error",
                "units": "unit", "seasonality": "seasonality", "data_type": "data_type",
                "detail": "processing", "method": "method", "data_format": "data_format",
                "additional_information": "notes"}
        for index, row in enumerate(grid.head(30).fillna("").values.tolist()):
            labels = [str(value).strip().casefold() for value in row]
            if not {"short_name", "what", "material", "units"} <= set(labels):
                continue
            # 标准模板有 Data 分隔行，也有删去该行但保留定义区的版本。
            marker = next((i for i in range(index + 1, len(grid))
                           if str(grid.iat[i, 0]).strip().casefold() == "data"
                           and "paste data table" in str(grid.iat[i, 1]).casefold()), None)
            return index, marker, {keys[label]: position for position, label in enumerate(labels) if label in keys}
        return None, None, {}

    @staticmethod
    def _declared_variables(grid, definition_index, marker, schema, header_index):
        variables = {}
        if not schema or definition_index >= header_index:
            return variables
        end = marker if marker is not None else header_index
        for position, row in enumerate(grid.iloc[definition_index + 1:end].fillna("").values.tolist(),
                                       definition_index + 2):
            item = {key: str(row[column]).strip() for key, column in schema.items()}
            if item["name"] and item.get("quantity"):
                item.update(definition_valid=True, description=item["quantity"], source="workbook_definition",
                            source_definition_row=position)
                variables[position] = item
        return variables

    def _prepare_table(self, grid, file_path, sheet_name, caption="", delimited=False, workbook=False,
                       workbook_notes="", decimal_comma=False):
        if grid.empty or len(grid) < 2:
            return []
        definition_index, marker, schema = self._workbook_definitions(grid) if workbook else (None, None, {})
        start = marker + 1 if marker is not None else 0
        header_index = start + self._detect_header_row(grid.iloc[start:])
        if header_index >= len(grid):
            return []
        # 先定位纵向排列的表块，再为每块识别各自的单位行和样品说明。
        sections = []
        begin, header, last_data = 0, header_index, header_index
        active_labels = self._labels(grid.iloc[header_index])
        for position, row in enumerate(grid.iloc[header_index + 1:].itertuples(index=False, name=None), header_index + 1):
            labels = self._labels(row)
            if self._observation_row(labels):
                last_data = position
            elif (last_data > header and (labels == active_labels or self._looks_like_header(labels))
                  and self._is_header_candidate(grid, position)):
                sections.append((begin, header, last_data + 1))
                begin, header, last_data = last_data + 1, position, position
                active_labels = labels
        sections.append((begin, header, len(grid)))
        tables = []
        for number, (begin, header, end) in enumerate(sections, 1):
            name = sheet_name if len(sections) == 1 else f"{sheet_name}#part_{number}"
            tables.extend(self._prepare_block(
                grid.iloc[begin:end], file_path, sheet_name, name, header - begin, caption,
                delimited, workbook_notes, decimal_comma,
                definition_index if begin == 0 else None, marker if begin == 0 else None,
                schema if begin == 0 else {},
            ))
        return tables

    def _prepare_block(self, grid, file_path, sheet_name, block_label, header_index, caption,
                       delimited, workbook_notes, decimal_comma, definition_index, marker, schema,
                       layout_confirmed=False):
        labels = self._labels(grid.iloc[header_index])
        if not layout_confirmed and not self._looks_like_header(labels) and not self._is_header_candidate(grid, header_index):
            add_review_item({"file": file_path, "sheet": block_label, "action": "暂缓表格",
                             "reason": "无法确定表头与观测区"})
            return []

        variables = list(self._declared_variables(grid, definition_index, marker, schema, header_index).values())
        missing = set()
        for row in grid.iloc[:header_index].fillna("").values.tolist():
            if len(row) > 1 and re.fullmatch(r"missing values?\s*:?|缺失值\s*[:：]?", str(row[0]).strip(), re.I):
                missing.update(str(value).strip() for value in row[1:] if str(value).strip())

        data = grid.iloc[header_index + 1:].copy()
        if delimited:
            width = max(position for position, label in enumerate(labels, 1) if label)
            overflow = data.iloc[:, width:].fillna("").apply(lambda column: column.str.strip().ne("")).any(axis=1)
            if overflow.any():
                line = overflow.index[overflow][0]
                add_review_item({"file": file_path, "sheet": sheet_name, "action": "暂缓表格",
                                 "reason": f"第 {line} 行的非空单元格超出表头宽度，需确认列是否错位"})
                return []
            labels = labels[:width]
            data = data.iloc[:, :width]
            short = [line for line, row in zip(data.index, data.itertuples(index=False, name=None))
                     if any(pd.isna(value) for value in row) and self._observation_row(self._labels(row))]
            for line in short:
                add_review_item({"file": file_path, "sheet": block_label, "source_line": line,
                                 "action": "暂缓该行", "reason": "数据行字段少于表头，缺少明确的空单元格分隔符"})
            data = data.drop(index=short)
        headers = []
        for position, label in enumerate(labels, 1):
            name = re.sub(r"[\r\n\t]+", " ", label.lstrip("\ufeff")).strip() or f"Unnamed: {position}"
            headers.append(f"{name} [column {position}]" if name in headers else name)
        data.columns = headers
        units = {}
        if not data.empty and self._is_unit_row(data.iloc[0].tolist()):
            units = {column: str(value).strip() for column, value in data.iloc[0].items()
                     if pd.notna(value) and str(value).strip()}
            data = data.iloc[1:]
        data = data.mask(data.apply(lambda column: column.astype("string").str.fullmatch(r"\s*", na=False))).dropna(how="all")
        if missing:
            data = data.mask(data.isin(missing)).dropna(how="all")
        if data.empty:
            return []
        last_column = max(position for position, label in enumerate(labels)
                          if label or data.iloc[:, position].notna().any())
        data = data.iloc[:, :last_column + 1]
        headers = headers[:last_column + 1]
        excluded = [column for column in headers if re.search(
            r"(?:δ(?:18|¹⁸)O|d18o)\s*sw[-_ ]*(?:wet|dry)", column, re.I
        )]
        data = data.drop(columns=excluded)
        source_labels = grid.iloc[:header_index, :len(headers)].fillna("")
        grouped = self._column_sample_groups(source_labels, headers)
        time_columns = [col for col in data if self._is_time_header(col)]
        grouped_axes = {grouped.get(col) for col in time_columns}
        grouped_table = grouped and (len(time_columns) == 1 or len(grouped_axes - {None}) > 1)
        blocks = [data] if layout_confirmed or grouped_table else self._detect_multi_sample_blocks(data)
        tables = []
        for index, block in enumerate(blocks, 1):
            block_name = block_label if len(blocks) == 1 else f"{block_label}#block_{index}"
            block_labels = grid.iloc[:header_index, [headers.index(col) for col in block.columns]].fillna("")
            block_ids = {match.group(1) for value in block_labels.to_numpy().flat
                         if (match := self.SAMPLE_LABEL.fullmatch(str(value).strip()))}
            sample_id = (next(iter(block_ids)) if len(block_ids) == 1 else
                         self._extract_sample_id_from_block(block) if len(blocks) > 1 else None)
            sample_by_column = self._column_sample_groups(block_labels, block.columns)
            description = [" | ".join(str(value) for value in row if value)
                           for row in block_labels.values.tolist()]
            if workbook_notes:
                description.append("Workbook metadata:\n" + workbook_notes)
            tables.append({
                "file_path": file_path, "sheet_name": block_name, "source_sheet_name": sheet_name,
                "source_rows": [int(row) if delimited else int(row) + 1 for row in block.index],
                "source_header_line": int(grid.index[header_index]) + (0 if delimited else 1),
                "source_column_positions": {col: int(grid.columns[headers.index(col)]) + 1 for col in block},
                "decimal_columns": tuple(col for col in block if decimal_comma and self._uses_decimal_comma(block[col])),
                "headers": list(block.columns),
                "units": {column: unit for column, unit in units.items() if column in block},
                "table_description": "\n".join([caption, *description]).strip(),
                "table_caption": caption,
                "data": block.to_dict("records"), "row_count": len(block),
                "format": Path(file_path).suffix.lower().lstrip("."),
                "sample_id": sample_id,
                "sample_by_column": sample_by_column,
                "variables": variables,
            })
        return tables

    def _column_sample_groups(self, labels, columns):
        for row in reversed(labels.values.tolist()):
            matches = [self.SAMPLE_LABEL.fullmatch(str(label).strip()) for label in row]
            if len({match.group(1) for match in matches if match}) < 2:
                continue
            groups = {}
            sample = None
            for column, label, match in zip(columns, row, matches):
                if str(label).strip():
                    sample = match.group(1) if match else None
                if sample:
                    groups[column] = sample
            return groups
        return {}

    @staticmethod
    def _is_time_header(column):
        return bool(time_basis(column) or re.match(r"^(?:age|year|date)\b|^(?:年代|年份|日期)", column, re.I))

    @staticmethod
    def _labels(row):
        return [str(value).strip() if pd.notna(value) else "" for value in row]

    @staticmethod
    def _observation_row(labels):
        first = next((label for label in labels if label), "")
        if re.match(r"^(?:missing values?|notes?|footnotes?|缺失值|说明|注)\s*(?:[:：.]|$)", first, re.I):
            return False
        return any(measurement_parts(label)[0] is not None or re.fullmatch(
            r"[+-]?\d+,\d+|\d{4}[-/]\d\d?[-/]\d\d?(?:[ T].*)?", label) for label in labels)

    @staticmethod
    def _uses_decimal_comma(column):
        values = column.dropna().astype(str).str.split("±").explode().str.strip().str.replace("−", "-", regex=False)
        comma = values.str.extract(r"^[+-]?\d+,(\d+)(?:[eE][+-]?\d+)?$")[0].dropna()
        return bool(comma.str.len().ne(3).any() and not values.str.fullmatch(r"[+-]?\d+\.\d+").any())

    def _is_header_candidate(self, grid, index):
        labels = [label for label in self._labels(grid.iloc[index]) if label]
        if len(labels) < 2 or self._observation_row(labels) or self._is_unit_row(labels):
            return False
        following = [self._labels(row) for _, row in grid.iloc[index + 1:index + 5].iterrows()]
        return any(self._observation_row(row) for row in following)

    @staticmethod
    def _looks_like_header(labels):
        pattern = r"\b(?:year|age|date|sr\s*/?\s*ca|d(?:elta\s*)?(?:18o|13c)|sst)\b|δ(?:18|13|¹⁸|¹³)|样品|年代|日期|年份|锶钙比|海表温度"
        return any(re.search(pattern, label.replace("_", " "), re.I) for label in labels)

    def _detect_header_row(self, df: pd.DataFrame) -> int:
        """
        智能定位表头行（通用策略，适用于作者自定义格式的附件）
        
        核心思路：找到数据开始的位置，它上面那行就是列名行
        
        返回表头行在当前表内的零基位置。
        """
        header_keywords = [
            "year", "date", "age", "sr/ca", "mg/ca", "d18o", "δ18o", "d13c", 
            "sst", "temperature", "depth", "sample", "lat", "lon", "site",
            "serial", "drilling", "total", "extension", "calcification", "density",
            "transect", "quality"
        ]
        
        # 策略1：从上往下找第一个"数据行"，它上面那行就是列名行
        # 数据行的特征：第一列是数值（年份如2009.75）或日期格式
        first_data_row_idx = None
        for i in range(min(30, len(df))):
            row_values = df.iloc[i].astype(str).tolist()
            first_value = row_values[0].strip() if row_values else ""
            
            # 跳过空行
            if not first_value or first_value.lower() == 'nan':
                continue
            
            # 检查第一列是否是数值（可能是年份）
            # 年份格式：1999.75, 2009, 1850.5 等
            if re.match(r'^-?\d+\.?\d*$', first_value):
                try:
                    num_val = float(first_value)
                    # 合理的年份范围或小数（可能是年份的小数表示）
                    if 1000 <= num_val <= 3000 or 0 < num_val < 1000:
                        first_data_row_idx = i
                        break
                except ValueError:
                    pass
        
        # 如果找到了数据行，列名行就是它上面第一个非空行
        if first_data_row_idx is not None and first_data_row_idx > 0:
            # 从数据行往上找最近的非空行作为列名行
            fallback_header = 0
            for j in range(first_data_row_idx - 1, -1, -1):
                row_values = df.iloc[j].astype(str).tolist()
                valid_values = [v for v in row_values if v.lower() != 'nan' and v.strip() != '']
                if valid_values and not self._is_unit_row(valid_values):
                    if not fallback_header:
                        fallback_header = j
                    # 检查是否包含表头关键词
                    match_count = sum(1 for val in valid_values if any(kw in val.lower() for kw in header_keywords))
                    if match_count >= 1:
                        logging.debug(f"通过数据行定位到表头行: 第 {j} 行")
                        return j
            # 如果上面没找到匹配关键词的行，直接返回数据行上一行
            return fallback_header
        
        # 策略2：如果策略1失败，使用关键词匹配
        candidate_header_rows = []
        
        for i in range(min(30, len(df))):
            row_values = df.iloc[i].astype(str).tolist()
            valid_values = [v for v in row_values if v.lower() != 'nan' and v.strip() != '']
            
            if not valid_values or self._is_unit_row(valid_values):
                continue
            
            # 计算匹配关键词的数量
            match_count = sum(1 for val in valid_values if any(kw in val.lower() for kw in header_keywords))
            
            if match_count >= 1:
                # 检查这行下面是否紧跟数据行（验证这确实是列名行）
                is_before_data = False
                if i + 1 < len(df):
                    next_row = df.iloc[i + 1].astype(str).tolist()
                    next_first = next_row[0].strip() if next_row else ""
                    if next_first and re.match(r'^-?\d+\.?\d*$', next_first):
                        is_before_data = True
                
                candidate_header_rows.append({
                    'index': i,
                    'match_count': match_count,
                    'valid_count': len(valid_values),
                    'is_before_data': is_before_data
                })
        
        # 选择最佳表头行：优先选择紧跟数据行的
        if candidate_header_rows:
            best_candidate = max(
                candidate_header_rows,
                key=lambda x: (x['is_before_data'], x['match_count'])
            )
            header_index = best_candidate['index']
            return header_index
        
        # 如果都没找到，返回第0行
        return 0
    
    def _is_unit_row(self, row_values):
        """单位行由单位词元组成，不能把样品编号中的字母当作单位。"""
        import re

        labels = [str(value).strip() for value in row_values
                  if pd.notna(value) and str(value).strip().lower() not in {"", "nan", "-", "n/a"}]
        if not labels or any(finite_number(label) is not None for label in labels):
            return False
        known = {"mol/mol", "mmol/mol", "μmol/mol", "‰", "‰ VPDB", "‰ VSMOW", "‰ PDB", "‰ SMOW",
                 "°C", "°F", "K", "ppm", "ppb", "%", "g/cm3", "g/cm³", "cm", "mm", "m", "gram", "units", "unit", "单位"}
        time_units = r"(?:(?:a|yr|years?|ka|kyr)\s*)?(?:BP|BCE|BC|AD|CE|before present|common era)|a|yr|years?|ka|kyr"
        return all(canonical_unit(label) in known or re.fullmatch(time_units, label, re.I)
                   for label in labels)

    def _detect_multi_sample_blocks(self, df: pd.DataFrame) -> List[pd.DataFrame]:
        """
        检测多样品并列数据块（通过空列分隔）
        
        Args:
            df: DataFrame
            
        Returns:
            List[pd.DataFrame]: 数据块列表
        """
        # 只有没有表头且整列为空的列可以作为分隔。具名的空字段仍属于表格。
        empty_col_indices = []
        for i, col in enumerate(df.columns):
            if not col.startswith("Unnamed: "):
                continue
            # 【修复】分开判断，避免 Series 的 or 操作
            is_empty_data = False
            try:
                col_series = df[col]
                if col_series.isna().all():
                    is_empty_data = True
                elif (col_series.astype(str).str.strip() == '').all():
                    is_empty_data = True
            except Exception:
                pass
            
            if is_empty_data:
                empty_col_indices.append(i)
        
        if not empty_col_indices:
            # 连续并列的块可以没有空列，但各块必须有明确的独立年代列。
            starts = [i for i, col in enumerate(df.columns) if self._is_time_header(col)]
            if len(starts) > 1 and starts[0] == 0:
                blocks = [df.iloc[:, a:b] for a, b in zip(starts, [*starts[1:], len(df.columns)])]
                proxy = r"sr\s*/?\s*ca|δ(?:18|13|¹⁸|¹³)|d18o|d13c|\bsst\b"
                if all(any(re.search(proxy, col, re.I) for col in block) for block in blocks):
                    return [block.dropna(how="all") for block in blocks if not block.dropna(how="all").empty]
            return [df]
        
        # 按空列分割数据块
        blocks = []
        start_idx = 0
        
        for empty_idx in empty_col_indices:
            if empty_idx > start_idx:
                # 提取从start_idx到empty_idx的列
                block_cols = df.columns[start_idx:empty_idx].tolist()
                block = df[block_cols].copy()
                # 去除全为NaN的行
                block = block.dropna(how='all')
                if not block.empty and len(block.columns) > 0:
                    blocks.append(block)
            start_idx = empty_idx + 1
        
        # 处理最后一个块
        if start_idx < len(df.columns):
            block_cols = df.columns[start_idx:].tolist()
            block = df[block_cols].copy()
            block = block.dropna(how='all')
            if not block.empty and len(block.columns) > 0:
                blocks.append(block)
        
        # 如果分割后只有一个块或没有块，返回原始DataFrame
        if len(blocks) <= 1:
            return [df]
        
        return blocks
    
    def _extract_sample_id_from_block(self, block: pd.DataFrame) -> str:
        """
        从数据块的列名中提取样品标识
        
        Args:
            block: 数据块DataFrame
            
        Returns:
            str: 样品标识
        """
        import re
        
        for col in block.columns:
            col_str = str(col)
            # 尝试匹配常见的样品标识模式
            # 模式1: MCA, Modern, LIA 等时期标识
            period_match = re.search(r'\b(MCA|Modern|LIA|Medieval|Holocene|Pleistocene)\b', col_str, re.IGNORECASE)
            if period_match:
                return period_match.group(1)
            
            # 模式2: 字母+数字的组合（如 GQ2, YXN1, PM1, E33L）
            sample_match = re.match(r'^([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*)(?=\s|[:(\[]|$)', col_str)
            if sample_match:
                potential_id = sample_match.group(1)
                # 排除常见的非样品标识
                if potential_id.lower() not in ['year', 'date', 'age', 'serial', 'sr', 'ba', 'mg', 'ca', 'sst']:
                    return potential_id
        
        return ""
    
    def _read_markdown(self, file_path: str) -> List[Dict[str, Any]]:
        lines = Path(file_path).read_text(encoding="utf-8-sig").splitlines()
        tables = []
        number = 0
        index = 0
        while index + 1 < len(lines):
            if not re.fullmatch(r"\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*", lines[index + 1]):
                index += 1
                continue
            positions = [index + 1]
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                positions.append(index + 1)
                index += 1
            if len(positions) > 1:
                number += 1
                rows = [[cell.replace(r"\|", "|").strip() for cell in re.split(
                    r"(?<!\\)\|", re.sub(r"^\||(?<!\\)\|$", "", lines[i - 1].strip()))]
                        for i in positions]
                tables.extend(self._prepare_table(pd.DataFrame(rows, index=positions), file_path,
                                                  f"table_{number}", delimited=True))
        return tables

    PDF_CONTEXT_REASON = "PDF 线框内没有可定位的数值单元格或观测列线索"

    @classmethod
    def _pdf_context_only(cls, rows):
        labels = [str(cell).strip() for row in rows for cell in row if cell and str(cell).strip()]
        if not any(re.search(r"\d", label) for label in labels):
            return True
        # A decimal in prose or a citation alone is not a table value. Keep possible data
        # cells and recognizable column labels, including unusual layouts for review.
        if cls._looks_like_header(labels):
            return False
        return not any(re.search(
            r"(?:^|\n)\s*[<>≤≥≈~+−–-]*\s*\(?\s*(?:\d|[.,]\d)|"
            r"ba\s*/\s*ca|barium.{0,4}calcium|oxygen isotope|carbon isotope|钡钙比",
            label, re.I,
        ) for label in labels)

    def _read_pdf_tables(self, file_path, inventory=None):
        import pdfplumber

        tables = []
        with pdfplumber.open(file_path) as document:
            for page in document.pages:
                page_name = f"page_{page.page_number}"
                if not page.chars and page.images:
                    add_review_item({"file": file_path, "sheet": page_name, "action": "暂缓读取",
                                     "reason": "图像页没有可提取的文字层，需提供带文字层或已核对的表格"})
                    continue
                try:
                    for number, table in enumerate(page.find_tables(), 1):
                        sheet = f"{page_name}#table_{number}"
                        # 只有几何上能定位为单元格的内容才进入结构化流程。
                        rows = table.extract()
                        if self._pdf_context_only(rows):
                            if inventory is not None:
                                inventory.append({"sheet_name": sheet, "row_count": 0, "status": "excluded",
                                                  "reason": self.PDF_CONTEXT_REASON})
                            continue
                        for item in self._prepare_table(pd.DataFrame(rows), file_path, sheet):
                            item["source_page"] = page.page_number
                            item["source_table_bbox"] = table.bbox
                            tables.append(item)
                except Exception as exc:
                    add_review_item({"file": file_path, "sheet": page_name, "status": "failed",
                                     "action": "暂缓该页", "reason": f"PDF 表格读取失败: {exc}"})
        return tables


if __name__ == "__main__":
    pass
