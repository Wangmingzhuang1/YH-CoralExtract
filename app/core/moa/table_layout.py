"""让模型定位附件结构。所有坐标都指向原表，观测值由读取器从原表取得。"""

import json
import logging
import re

from pandas import isna

from app.utils.llm_api import call_llm


def propose_layout(grid, sheet, caption, merges, header_hints, cache=None):
    row_count, column_count = grid.shape
    edges = set(range(1, min(row_count, 10) + 1)) | set(range(max(1, row_count - 2), row_count + 1))
    edges.update(row for hint in header_hints for row in range(max(1, hint - 2), min(row_count, hint + 3) + 1))
    edges.update({1 + row_count // 3, 1 + row_count * 2 // 3})
    rows = {}
    for index in sorted(edges):
        cells = [[column, str(value).strip()[:160]] for column, value in enumerate(grid.iloc[index - 1], 1)
                 if not isna(value) and str(value).strip()]
        if cells:
            rows[index] = cells

    payload = {"sheet": sheet, "caption": caption[:2000], "shape": [row_count, column_count],
               "merged_ranges": merges, "rule_header_hints": header_hints, "rows": rows}
    # 缩减取样内容时保持坐标和 JSON 完整。
    for cell_limit in (160, 60, 24, 10):
        payload["rows"] = {row: [[column, value[:cell_limit]] for column, value in cells] for row, cells in rows.items()}
        wire = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(wire) <= 22000:
            break
    else:
        # 密集宽表改用按物理列顺序排列的少量完整行。保留所有列，避免裁掉右侧子表。
        priority = dict.fromkeys([*header_hints, *(row + 1 for row in header_hints), 1, 2, row_count, *rows])
        payload["rows"] = {}
        payload["row_format"] = "dense_from_column_1"
        for row in priority:
            if row not in rows:
                continue
            values = [""] * column_count
            for column, value in rows[row]:
                values[column - 1] = value[:24]
            payload["rows"][row] = values
            if len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"))) > 22000:
                del payload["rows"][row]
        if len(payload["rows"]) < 2 or not all(hint in payload["rows"] for hint in header_hints if hint in rows):
            logging.warning("附件结构样例超过预算，需复核: %s", sheet)
            return []
        wire = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    prompt = f"""定位作者自定义的 Excel 或 Word 原表结构。只提交能从原表证实的坐标。
输入行号、列号从 1 开始。rows 只展示若干原始行，未展示的行仍存在。合并范围为 [起始行,终止行,起始列,终止列]。
rows 的值通常是 [列号,值] 列表。若 row_format 为 dense_from_column_1，则数组位置对应第 1 列起的物理列号，空字符串保留空列。
区分定义/单位/表头/观测/注释，不把元数据、引用表、实验说明、汇总或模拟结果当成珊瑚原始观测。
可以有多行表头、上下表块或左右独立子表。每块给原始数据起止行、参与的列号和表头行，单位行可为空。
若定义区和数据区命名不一致，只有证据明确时才给出列号和同表定义区行号的对应关系。
不得重写数值、发明表头或单位。坐标不确定时返回空 blocks。只输出 JSON：
{{"blocks":[{{"header_rows":[3,4],"first_data_row":6,"last_data_row":20,"columns":[1,3,4],
"unit_row":5,"definition_rows":{{"3":2}}}}]}}
原始网格：{wire}"""
    cache_key = "attachment-layout-v1\n" + prompt
    try:
        saved = cache.get([], cache_key) if cache else None
        response = saved["response"] if saved else call_llm("max", prompt, temperature=0)
        if not response:
            return []
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", response.strip(), flags=re.I)
        result = json.loads(cleaned)
        blocks = result.get("blocks") if isinstance(result, dict) else None
        if not isinstance(blocks, list):
            return []
        if cache and not saved and blocks and all(valid_layout(block, row_count, column_count) for block in blocks):
            cache.set([], {"response": response}, cache_key)
        return blocks
    except (ValueError, RuntimeError, OSError, KeyError, TypeError) as exc:
        logging.warning("附件结构建议不可用 %s: %s", sheet, exc)
        return []


def valid_layout(block, row_count, column_count):
    if not isinstance(block, dict):
        return False
    headers, columns = block.get("header_rows"), block.get("columns")
    start, end, unit = block.get("first_data_row"), block.get("last_data_row"), block.get("unit_row")
    if not (type(start) is int and type(end) is int and 1 < start <= end <= row_count):
        return False
    if not isinstance(headers, list) or not 1 <= len(headers) <= 8:
        return False
    if not all(type(row) is int and 1 <= row < start for row in headers) or headers != sorted(set(headers)):
        return False
    if not isinstance(columns, list) or len(columns) < 2:
        return False
    if not all(type(column) is int and 1 <= column <= column_count for column in columns) or columns != sorted(set(columns)):
        return False
    if unit is not None and not (type(unit) is int and headers[-1] < unit < start):
        return False
    bindings = block.get("definition_rows", {})
    return isinstance(bindings, dict) and all(
        str(column) in {str(value) for value in columns} and type(row) is int and 1 <= row < start
        for column, row in bindings.items()
    )
