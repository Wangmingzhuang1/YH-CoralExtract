#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""解析源单位证据，并把已确认的量转换为统一单位。"""

import math
import re
import unicodedata


_UNKNOWN = {"", "-", "none", "unknown", "未明确"}
_ISOTOPES = {"δ18O_value", "δ13C_value", "δ18O_difference"}
_RATIO = r"(?:\b[mμun]?mol\s*(?:/\s*mol|[·⋅]?\s*mol\s*[-−]\s*1)|\b(?:micro|milli|nano)?moles?\s+per\s+mole)"
_SCALE = r"\b(?:VSMOW|SMOW|V[-\s]?PDB|PDB|Vienna\s+Pee\s*Dee\s+Belemnite)\b"
_TEMP = r"(?:°\s*[CF]|deg\s*[CFK]\b|(?:degrees?\s+)?(?:Celsius|Fahrenheit|Kelvin)\b)"
BACA_UNIT_REVIEW = "Ba/Ca 的 nmol/mol 档案声明需与论文或补充数据交叉核对"
_UNIT_PATTERN = re.compile(
    rf"{_RATIO}|(?:‰|per\s*mil)\s*(?:{_SCALE})?|{_SCALE}|{_TEMP}"
    r"|\b(?:pp[mb]|[mμu]g\s*/\s*(?:kg|g))\b|%",
    re.I,
)


def source_unit(header):
    """读取表头中的单位表达，保留无法换算的明确声明。"""
    text = unicodedata.normalize("NFKC", str(header))
    bracket = re.search(r"[\[(]([^\[\]()]+)[\])]\s*$", text)
    if bracket:
        label = bracket.group(1).strip()
        if _UNIT_PATTERN.search(label) or label.upper() in {"C", "F", "K"}:
            return label
    match = _UNIT_PATTERN.search(text)
    return match.group().strip() if match else ""


def canonical_unit(unit):
    """合并拼写变体，保留量纲、比例因子和同位素标尺的区别。"""
    label = unicodedata.normalize("NFKC", str(unit or "")).strip().casefold()
    if label in _UNKNOWN:
        return None
    label = label.replace("μ", "u").replace("−", "-").replace("·", "").replace("⋅", "")
    compact = re.sub(r"\s+", "", label).strip("[]()")
    compact = re.sub(r"v-pdb|viennapeedeebelemnite", "vpdb", compact)
    if compact in {"mmol/mol", "mmolmol-1", "millimolpermol", "millimolepermole", "millimolespermole"}:
        return "mmol/mol"
    if compact in {"umol/mol", "umolmol-1", "micromolpermol", "micromolepermole", "micromolespermole"}:
        return "μmol/mol"
    if compact in {"nmol/mol", "nmolmol-1", "nanomolpermol", "nanomolepermole", "nanomolespermole"}:
        return "nmol/mol"
    if compact in {"mol/mol", "molmol-1", "molpermol", "molepermole", "molespermole"}:
        return "mol/mol"
    for scale in ("VPDB", "PDB", "VSMOW", "SMOW"):
        if compact in {scale.lower(), f"‰{scale.lower()}", f"permil{scale.lower()}"}:
            # 显式 PDB 标签在本项目归一到 VPDB，原始标签保留在单位证据中。
            return "‰ VPDB" if scale in ("VPDB", "PDB") else f"‰ {scale}"
    if compact in {"‰", "permil", "permille", "permill"}:
        return "‰"
    for canonical, aliases in (
        ("°C", {"°c", "degc", "c", "celsius", "degreecelsius", "degreescelsius"}),
        ("°F", {"°f", "degf", "f", "fahrenheit", "degreefahrenheit", "degreesfahrenheit"}),
        ("K", {"k", "degk", "kelvin", "degreekelvin", "degreeskelvin"}),
    ):
        if compact in aliases:
            return canonical
    return label


def _quoted_units(field, quote):
    """只有正文同时指明目标变量和单位，才用它补充该变量。"""
    names = {
        "Sr/Ca_value": r"Sr\s*/\s*Ca|strontium.{0,4}calcium|锶钙比",
        "Ba/Ca_value": r"Ba\s*/\s*Ca|barium.{0,4}calcium|钡钙比",
        "δ18O_value": r"(?:δ\s*18\s*O|\bd18o\b|oxygen isotope|氧同位素)",
        "δ13C_value": r"(?:δ\s*13\s*C|\bd13c\b|carbon isotope|碳同位素)",
        "sea_surface_temperature_value": r"\bSST\b|sea surface temperature|海表温度",
    }
    text = unicodedata.normalize("NFKC", quote or "")
    if field not in names or not re.search(names[field], text, re.I):
        return set()
    if re.search(r"\b(?:not|never|rather than|instead of)\b|未使用|并非", text, re.I):
        return set()
    if field in _ISOTOPES:
        return {canonical_unit(match) for match in re.findall(_SCALE, text, re.I)}
    pattern = _RATIO if field in {"Sr/Ca_value", "Ba/Ca_value"} else _TEMP
    return {canonical_unit(match.group()) for match in re.finditer(pattern, text, re.I)}


def resolve_unit(field, header, declarations=(), reported_unit=None, quote=None, table_note=""):
    """将表头、单位行或数据库定义合并为一个可审查的决定。"""
    evidence = []
    for source, value in (("header", source_unit(header)), *declarations):
        if canonical_unit(value):
            evidence.append({"source": source, "label": str(value), "unit": canonical_unit(value)})

    for line in table_note.splitlines():
        if (re.match(r"^(?:notes?|footnotes?|注|说明)\s*[.:：]", line, re.I)
                and not re.search(r"\b(?:precision|uncertainty|error|slope|offset)\b|误差|精度|不确定度", line, re.I)):
            for unit in sorted(_quoted_units(field, line)):
                evidence.append({"source": "table_note", "label": unit, "unit": unit, "quote": line})

    reported = canonical_unit(reported_unit)
    quoted = _quoted_units(field, quote)
    if reported or quote:
        evidence.append({"source": "body", "label": reported_unit, "quote": quote,
                         "quoted_units": sorted(quoted)})
    direct = {item["unit"] for item in evidence if item["source"] != "body"}
    specific = direct - {"‰"} if field in _ISOTOPES else direct
    if len(specific) > 1 or len(quoted) > 1 or (quoted and reported and reported not in quoted):
        return {"unit": None, "status": "unit_conflict", "evidence": evidence}
    if specific and quoted and specific != quoted:
        return {"unit": None, "status": "unit_conflict", "evidence": evidence}
    unit = next(iter(specific or quoted or direct), None)
    # 保留档案原始声明标签（如 "‰ PDB"）用于溯源，转换决定用规范化单位
    declared = next((item["label"] for item in evidence if item.get("unit") == unit), None)
    result = {"unit": unit, "status": "resolved" if unit and unit != "‰" else "unknown_unit",
              "evidence": evidence}
    if declared:
        result["original_unit"] = declared
    return result


def finite_number(value):
    """读取有限标量，空值和非法数值统一返回 None。"""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def requires_baca_unit_check(field, unit):
    """Keep the archive nmol label reviewable where the corresponding publication may use μmol."""
    return field == "Ba/Ca_value" and canonical_unit(unit) == "nmol/mol"


def source_number(value, decimal_comma=False):
    label = str(value).strip()
    if decimal_comma and re.fullmatch(r"[+-]?\d+,\d+(?:[eE][+-]?\d+)?", label):
        return finite_number(label.replace(",", "."))
    return finite_number(value)


def measurement_parts(value, decimal_comma=False):
    """读取单值或明确的对称不确定度，保留范围、检出限等其他表达待核。"""
    text = value.strip().replace("−", "-") if isinstance(value, str) else value
    if isinstance(text, str) and text.count("±") == 1:
        center, spread = (source_number(part, decimal_comma) for part in text.split("±"))
        if center is not None and spread is not None and spread >= 0:
            return center, spread
    return source_number(text, decimal_comma), None


class UnitConversionModule:
    """只把已确认单位的有限数值写入标准字段。"""

    default_units = {
        "Sr/Ca_value": "mmol/mol",
        "Ba/Ca_value": "μmol/mol",
        "δ18O_value": "‰ VPDB",
        "δ13C_value": "‰ VPDB",
        "sea_surface_temperature_value": "°C",
        "δ18O_difference": "‰ VPDB",
    }

    def convert_units(self, data_point, mapped_data):
        point = data_point.copy()
        mapped_units = {item["standard_field"]: item.get("unit")
                        for item in mapped_data.get("mapped_data", [])}
        for field, target in self.default_units.items():
            if field not in point or f"{field}_unit_status" in point:
                continue
            original = point[field]
            resolution = point.get(f"{field}_unit_resolution")
            unit = (resolution["unit"] if resolution is not None else
                    canonical_unit(point.get(f"{field}_source_unit") or mapped_units.get(field)))
            column = point.get("source_columns", {}).get(field)
            value, spread = measurement_parts(original, column in point.get("decimal_columns", ()))
            converted = None if value is None else self.convert_value(field, value, unit)
            point[f"{field}_original_value"] = original
            point[f"{field}_original_unit"] = resolution.get("original_unit", unit) if resolution else unit
            point[f"{field}_converted"] = converted is not None and converted != value
            point[f"{field}_unit"] = target if converted is not None else None
            if value is None:
                status = "missing_or_invalid_value"
            elif resolution and resolution["status"] in {"unit_conflict", "manual_excluded"}:
                status = resolution["status"]
            elif converted is not None:
                status = "verified"
            else:
                status = "unsupported_unit" if unit and unit != "‰" else "unknown_unit"
            point[f"{field}_unit_status"] = status
            point[field] = converted
            if spread is not None:
                point[f"{field}_original_uncertainty"] = spread
                if status == "verified":
                    point[f"{field}_uncertainty"] = self.convert_value(field, spread, unit) - self.convert_value(field, 0, unit)
        return point

    def convert_value(self, field, value, unit):
        unit = canonical_unit(unit)
        if field == "Sr/Ca_value":
            if unit == "mmol/mol":
                return value
            if unit == "μmol/mol":
                return value / 1000
            if unit == "nmol/mol":
                return value / 1_000_000
            if unit == "mol/mol":
                return value * 1000
        elif field == "Ba/Ca_value":
            if unit == "μmol/mol":
                return value
            if unit == "mmol/mol":
                return value * 1000
            if unit == "nmol/mol":
                return value / 1000
            if unit == "mol/mol":
                return value * 1_000_000
        elif field == "sea_surface_temperature_value":
            if unit == "°C":
                return value
            if unit == "°F":
                return (value - 32) * 5 / 9
            if unit == "K":
                return value - 273.15
        elif field in _ISOTOPES and unit == "‰ VPDB":
            return value
        return None

    def infer_missing_units(self, data_point):
        return self.convert_units(data_point, {})

    def verify_unit_conversion(self, data_point, field):
        return data_point.get(f"{field}_unit_status") == "verified"

    def convert_units_batch(self, table_data, mapped_data):
        return [self.convert_units(point, mapped_data) for point in table_data]
