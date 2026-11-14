"""Show raw source cells and the declarations around an archived observation."""

import csv
import re
from pathlib import Path

from app.core.moa.unit_converter import finite_number

_TABLE_EXT = {".txt", ".tab", ".csv", ".tsv", ".md"}
_EXCEL_EXT = {".xlsx", ".xlsm", ".xls", ".ods"}
ATTACHMENT_TEXT_EXTENSIONS = _TABLE_EXT | _EXCEL_EXT | {".docx"}


def source_path(root, file):
    if not file:
        return None
    try:
        root = Path(root).resolve()
        path = (root / file).resolve()
        return path if path.is_relative_to(root) else None
    except (OSError, ValueError):
        return None


def _read_lines(path):
    for encoding in ("utf-8-sig", "gb18030", "latin-1"):
        try:
            return path.read_text(encoding=encoding).splitlines(), encoding
        except UnicodeDecodeError:
            continue


def attachment_text_lines(path):
    """Read original attachment content with locations, without a preview row limit."""
    ext = path.suffix.casefold()
    if ext in _TABLE_EXT:
        lines, _ = _read_lines(path)
        for number, line in enumerate(lines, 1):
            yield f"第 {number} 行", line
    elif ext in _EXCEL_EXT:
        import pandas as pd

        with pd.ExcelFile(path) as book:
            for sheet in book.sheet_names:
                grid = book.parse(sheet, header=None, dtype=str, keep_default_na=False)
                for number, row in enumerate(grid.itertuples(index=False, name=None), 1):
                    yield f"工作表 {sheet} 第 {number} 行", " | ".join(map(str, row))
    elif ext == ".docx":
        from docx import Document

        document = Document(path)
        for number, paragraph in enumerate(document.paragraphs, 1):
            yield f"第 {number} 段", paragraph.text
        for number, table in enumerate(document.tables, 1):
            for row_number, row in enumerate(table.rows, 1):
                yield f"表 {number} 第 {row_number} 行", " | ".join(cell.text for cell in row.cells)


def _number(value):
    number = finite_number(value)
    return int(number) if number is not None and number.is_integer() and number > 0 else None


def _targets(column):
    return {part.strip().casefold() for part in (column or "").split(" | ") if part.strip()}


def _find_header(rows, targets, center=None):
    if not targets:
        return None
    search = range(min(center + 1, len(rows)) - 1, -1, -1) if center is not None else range(len(rows))
    return next((i for i in search if targets.intersection(str(cell).strip().casefold() for cell in rows[i])), None)


def _preview(rows, targets, row, line, context, *, header=None, indices=None, notes=(), label=""):
    indices = list(range(1, len(rows) + 1)) if indices is None else indices
    if header is None:
        header = _find_header(rows, targets, (_number(line) - 1) if _number(line) else None)
    physical = _number(line)
    logical = _number(row)
    if physical:
        center = next((i for i, n in enumerate(indices) if n >= physical), len(rows) - 1)
        highlight = physical if physical in indices else None
    elif logical and header is not None:
        center = header + logical
        highlight = None
    else:
        center = header + 1 if header is not None else 0
        highlight = None
    center = min(max(0, center), max(0, len(rows) - 1))
    first = max(header + 1 if header is not None else 0, center - context)
    last = min(len(rows), max(first, center + context + 1))
    headers = rows[header] if header is not None else []
    highlights = [i for i, value in enumerate(headers) if str(value).strip().casefold() in targets]
    return {"kind": "table", "header": headers, "header_line": indices[header] if header is not None else None,
            "rows": [{"line": indices[i], "cells": rows[i]} for i in range(first, last)],
            "highlight_line": highlight, "highlight_cols": highlights,
            "total_lines": len(rows), "note": label, "declarations": list(notes)}


def _read_delimited(path, row, line, column, context):
    lines, encoding = _read_lines(path)
    targets = _targets(column)
    marker = next((i for i, text in enumerate(lines[:2000])
                   if re.fullmatch(r"\s*#\s*Data:\s*", text, re.I) or text.strip() == "*/"), None)
    metadata = list(enumerate(lines[:marker + 1], 1)) if marker is not None else []
    entries = [(i, text) for i, text in enumerate(lines, 1)
               if (marker is None or i > marker + 1) and text.strip()
               and (marker is None or not text.lstrip().startswith("#"))]
    if not entries:
        return {"kind": "text", "note": "源文件没有可预览的数据行", "rows": [], "header": []}
    dialect_sample = [text for _, text in entries[:15]]
    delimiter = next((separator for separator in ("\t", ";", ",", "|")
                      if sum(separator in text for text in dialect_sample) >= min(2, len(dialect_sample))), None)
    rows = []
    indices = []
    if delimiter:
        reader = csv.reader([text for _, text in entries], delimiter=delimiter)
        last = 0
        for values in reader:
            rows.append(values)
            indices.append(entries[last][0])
            last = reader.line_num
    else:
        indices = [i for i, _ in entries]
        rows = [[text] for _, text in entries]
    notes = []
    for number, text in metadata:
        clean = text.strip().lstrip("#").strip()
        label = re.sub(r"^(?:Parameter\(s\):\s*)", "", clean, flags=re.I).split("\t", 1)[0]
        if any(label.casefold().startswith(target) for target in targets) or re.search(
                r"Latitude:|Longitude:|Site_Name:|Time_Unit:", clean, re.I):
            notes.append({"line": number, "text": clean})
    if line is None and marker is not None and _number(row):
        physical = _number(row)
        line = indices[physical] if physical < len(indices) else None
    return _preview(rows, targets, row, line, context, header=0 if marker is not None else None,
                    indices=indices, notes=notes[:12], label=f"原始文本 · {encoding}")


def _sheet_name(sheet):
    return re.split(r"#(?:part|block)_\d+", sheet or "", maxsplit=1)[0]


def _read_excel(path, sheet, row, line, column, context):
    import pandas as pd

    with pd.ExcelFile(path) as book:
        name = _sheet_name(sheet)
        if name and name not in book.sheet_names:
            return {"kind": "error", "note": f"源工作簿中没有工作表 {name}", "rows": [], "header": []}
        target = name or book.sheet_names[0]
        grid = pd.read_excel(book, sheet_name=target, header=None, dtype=str, keep_default_na=False)
    rows = grid.values.tolist()
    result = _preview(rows, _targets(column), row, line, context, label=f"工作表 {target}")
    if (header := result["header_line"]) is not None:
        for index in range(max(0, header - 4), header - 1):
            cells = [cell.strip() for cell in rows[index] if cell.strip()]
            if len(cells) == 1 and re.match(r"^\s*Table\s+[A-Z]?\d+", cells[0], re.I):
                result["declarations"].append({"line": index + 1, "text": cells[0]})
    return result


def _read_pdf(path, sheet, row, line, column, context):
    import pdfplumber

    page_no = int(match.group(1)) if (match := re.search(r"page_(\d+)", sheet or "")) else 1
    table_number = int(match.group(1)) if (match := re.search(r"#table_(\d+)", sheet or "")) else None
    with pdfplumber.open(path) as doc:
        if not 1 <= page_no <= len(doc.pages):
            return {"kind": "error", "note": f"源 PDF 没有第 {page_no} 页", "rows": [], "header": []}
        page = doc.pages[page_no - 1]
        text = page.extract_text() or ""
        if table_number:
            tables = page.find_tables()
            if 1 <= table_number <= len(tables):
                table = [[cell or "" for cell in values] for values in tables[table_number - 1].extract()]
                if any(cell.strip() for values in table for cell in values):
                    return _preview(table, _targets(column), row, line, context,
                                    label=f"PDF 第 {page_no} 页 · 表 {table_number}")
            return {"kind": "error", "note": f"第 {page_no} 页无法定位表 {table_number}", "rows": [], "header": []}
    if not text.strip():
        return {"kind": "pdf", "page": page_no, "note": f"第 {page_no} 页为空白页或扫描版无文本层",
                "rows": [], "header": []}
    return _preview([[text] for text in text.splitlines()], set(), row, line, context,
                    label=f"PDF 第 {page_no} 页的文字")


def _read_docx(path, sheet, row, line, column, context):
    import docx

    document = docx.Document(path)
    match = re.search(r"table_(\d+)", sheet or "")
    if match:
        number = int(match.group(1))
        if not 1 <= number <= len(document.tables):
            return {"kind": "error", "note": f"源文档没有表 {number}", "rows": [], "header": []}
        rows = [[""] * getattr(r, "grid_cols_before", 0) + [cell.text for cell in r.cells]
                for r in document.tables[number - 1].rows]
        return _preview(rows, _targets(column), row, line, context, label=f"Word 表 {number}")
    return _preview([[p.text] for p in document.paragraphs], set(), row, line, context, label="Word 正文")


def read_source(source_root, file, sheet=None, row=None, line=None, column=None, context=6):
    path = source_path(source_root, file)
    if path is None or not path.exists():
        return {"kind": "missing", "note": "源文件无法在当前项目中定位", "rows": [], "header": []}
    if path.is_dir():
        return {"kind": "dir", "note": "该记录指向目录", "rows": [], "header": [],
                "entries": sorted(p.name for p in path.iterdir())[:30]}
    ext = path.suffix.casefold()
    try:
        if ext in _TABLE_EXT:
            return _read_delimited(path, row, line, column, context)
        if ext in _EXCEL_EXT:
            return _read_excel(path, sheet, row, line, column, context)
        if ext == ".pdf":
            return _read_pdf(path, sheet, row, line, column, context)
        if ext == ".docx":
            return _read_docx(path, sheet, row, line, column, context)
        return {"kind": "file", "note": "请打开原件查看这种文件格式", "rows": [], "header": []}
    except (OSError, ImportError, ValueError, csv.Error) as exc:
        return {"kind": "error", "note": f"读取源文件失败：{exc}", "rows": [], "header": []}
