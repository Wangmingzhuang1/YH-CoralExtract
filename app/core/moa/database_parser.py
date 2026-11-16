#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
统一古气候数据库解析模块 (Unified Paleoclimate Database Parser)

支持的数据库格式：
- NOAA Paleoclimatology (.txt)
- PANGAEA Data Publisher (.tab)

功能：
1. 自动检测数据格式
2. 解析元数据（DOI、作者、坐标、时间范围等）
3. 解析变量定义（列名、单位、方法）
4. 提取数据表并标准化
5. LLM辅助的复杂列名映射
"""

import logging
import csv
import re
import os
import json
import hashlib
from itertools import islice
from functools import partial
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple
from enum import Enum
import pandas as pd
from app.core.moa.context_extractor import composite_evidence, correct_archive_doi
from app.core.moa.llm_parser import LLMParsingModule, is_derived_observation_column
from app.core.moa.unit_converter import UnitConversionModule, canonical_unit, finite_number, measurement_parts, resolve_unit
from app.core.moa.spatiotemporal_standard import (bp_reference_year, calendar_date_order, sampling_profile,
                                                   select_year_axis, source_year_axis, time_basis,
                                                   verified_padded_year_step, year_ce)
from app.utils.review_queue import add_review_item, structural_review_decision

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

SPELEOTHEM_SOURCE = re.compile(r"\b(?:stalagmites?|speleothems?|flowstones?)\b", re.I)


def speleothem_declaration(metadata_block):
    """标题或关键词明示石笋/洞穴沉积来源时返回证据行；参考文献提及不算。"""
    for line in metadata_block.splitlines():
        if re.match(r"^\s*(?:Citation|Keyword\(s\))\s*:", line, re.I) and SPELEOTHEM_SOURCE.search(line):
            return line.strip()
    return None


CORE_FIELDS = {"Sr/Ca_value", "δ18O_value", "δ13C_value"}
TIME_FIELDS = ("year", "age_bp", "age_ka_bp", "age_bce", "internal_chronology")
CORAL_MATERIAL = re.compile(
    r"\b(?:porites|acropora|orbicella|montastrea|montastraea|diploria|pocillopora|"
    r"siderastrea|pavona|favia|favites|goniopora|coral skeleton|coral aragonite|coral)\b", re.I
)


class DatabaseFormat(Enum):
    """数据库格式枚举"""
    NOAA = "noaa"
    PANGAEA = "pangaea"
    UNKNOWN = "unknown"


class UnifiedDatabaseParser:
    """统一的古气候数据库解析器"""
    
    def __init__(self, llm_cache=None):
        """
        初始化解析器
        
        Args:
            llm_cache: LLM缓存对象（可选，用于减少重复调用）
        """
        self.llm_cache = llm_cache
        config_path = Path(__file__).resolve().parents[3] / "config" / "spatio_temporal_config.json"
        with config_path.open(encoding="utf-8") as config_file:
            self.noaa_reviewed_representatives = json.load(config_file).get("noaa_reviewed_representatives", {})
        # 初始化LLM解析器用于时间分辨率推断
        self.llm_parser = LLMParsingModule(enable_cache=True if llm_cache else False, cache_dir="cache")
        if llm_cache:
            self.llm_parser.cache = llm_cache
            
        # 标准字段映射规则
        self.field_mapping = {
            # 年龄/时间字段
            "age": "year",
            "age_ce": "year",
            "age_ad": "year",
            "age [a ad/ce]": "year",
            "age [a ad]": "year",
            "age [ka bp]": "age_ka_bp",
            "int chron [a]": "internal_chronology",  # 【优化】映射到独立字段，避免与标准BP混淆
            
            # Sr/Ca 字段
            "sr/ca": "Sr/Ca_value",
            "sr_ca": "Sr/Ca_value",
            "srca": "Sr/Ca_value",
            "sr/ca [mmol/mol]": "Sr/Ca_value",
            "strontium/calcium ratio [mmol/mol]": "Sr/Ca_value",
            
            # δ18O 字段
            "d18o": "δ18O_value",
            "d18o_raw": "δ18O_value",
            "delta18o": "δ18O_value",
            "δ18o": "δ18O_value",
            "δ18o [‰ pdb]": "δ18O_value",
            "δ18o [‰ vpdb]": "δ18O_value",
            
            # δ13C 字段
            "d13c": "δ13C_value",
            "d13c_raw": "δ13C_value",
            "delta13c": "δ13C_value",
            "δ13c": "δ13C_value",
            "δ13c [‰ pdb]": "δ13C_value",
            "δ13c [‰ vpdb]": "δ13C_value",
            
            # Ba/Ca 字段
            "ba/ca": "Ba/Ca_value",
            "ba/ca [µmol/mol]": "Ba/Ca_value",
            
            # SST 字段
            "sst": "sea_surface_temperature_value",
        }
        
        # 需要过滤的水文信号列（与NOAA解析器保持一致）
        self.hydro_signal_patterns = [
            r'δ18osw', r'd18osw', r'δ¹⁸osw',
            r'δ18o.*sw', r'd18o.*sw',
            r'δ18osw.*wet', r'δ18osw.*dry',
        ]
    
    def detect_format(self, file_path: str) -> DatabaseFormat:
        """
        自动检测数据文件格式
        
        Args:
            file_path: 文件路径
            
        Returns:
            DatabaseFormat: 检测到的格式类型
        """
        try:
            with open(file_path, encoding="utf-8-sig", errors="replace") as source:
                head = source.read(16000).lstrip()
        except OSError:
            return DatabaseFormat.UNKNOWN
        if re.match(r"/\*\s*DATA DESCRIPTION:", head, re.I):
            return DatabaseFormat.PANGAEA
        if head.startswith("#") and (
            re.search(r"^#\s*(?:Dataset_DOI|Site_Information|Data_Collection|Variables)\b", head, re.M)
            or "World Data Center for Paleoclimatology" in head
        ):
            return DatabaseFormat.NOAA
        return DatabaseFormat.UNKNOWN

    def parse_file(self, file_path: str, paper_context_loader=None) -> Dict[str, Any]:
        """
        解析数据文件（主入口）
        
        Args:
            file_path: 文件路径
            
        Returns:
            Dict: 包含元数据、变量定义和数据的字典
        """
        format_type = self.detect_format(file_path)
        logger.info(f"检测到文件格式: {format_type.value}, 文件: {os.path.basename(file_path)}")
        
        if format_type == DatabaseFormat.NOAA:
            return self._parse_noaa(file_path, paper_context_loader)
        elif format_type == DatabaseFormat.PANGAEA:
            return self._parse_pangaea(file_path, paper_context_loader)
        else:
            logger.warning(f"未知文件格式，尝试通用解析: {file_path}")
            return self._parse_generic(file_path)
    
    def _parse_noaa(self, file_path: str, paper_context_loader=None) -> Dict[str, Any]:
        with open(file_path, encoding="utf-8-sig", errors="replace") as source:
            lines = source.read().splitlines()
        metadata = self._parse_noaa_metadata(lines)
        self._resolve_noaa_location(metadata, file_path)
        variables = self._parse_noaa_variables(lines)
        data = self._parse_noaa_data(lines, file_path)
        return self._resolve_database_table("NOAA", file_path, data, metadata, variables, paper_context_loader)

    def _parse_pangaea(self, file_path: str, paper_context_loader=None) -> Dict[str, Any]:
        with open(file_path, encoding="utf-8-sig", errors="replace") as source:
            content = source.read()
        metadata_block, data_block = self._split_pangaea_content(content)
        if declaration := speleothem_declaration(metadata_block):
            return {"format": "PANGAEA", "file_path": file_path, "metadata": {}, "variables": [],
                    "data": pd.DataFrame(), "column_mapping": {}, "record_count": 0, "mapped_data": [],
                    "table_metadata": {}, "skip_reason": "not_coral_data", "skip_evidence": declaration}
        metadata = self._parse_pangaea_metadata(metadata_block)
        variables = self._parse_pangaea_variables(metadata_block)
        first_line = content.count("\n", 0, content.find(data_block, len(metadata_block))) + 1
        data = self._parse_pangaea_data(data_block, first_line, file_path)
        return self._resolve_database_table("PANGAEA", file_path, data, metadata, variables, paper_context_loader)

    def _resolve_database_table(self, format_name, file_path, data, metadata, variables, paper_context_loader=None):
        result = {
            "format": format_name, "file_path": file_path, "metadata": metadata,
            "variables": variables, "data": data, "column_mapping": {},
            "record_count": 0, "mapped_data": [], "table_metadata": {},
        }
        if data.empty:
            return result
        if format_name == "NOAA" and not self._is_coral_data(variables):
            result["skip_reason"] = "sclerosponge_data"
            return result
        if format_name == "PANGAEA" and any(
            str(column).casefold().startswith("binary") for column in data.columns
        ) and not any(re.search(r"age|year|δ18o|sr/ca", str(column), re.I) for column in data.columns):
            result["skip_reason"] = "binary_attachment_index"
            return result
        time_candidate = any(re.search(r"\b(?:age|year|date|time|chron\w*|yr|ce|ad)\b",
                                       col.replace("_", " "), re.I) for col in data.columns)
        experiment = any(re.search(r"\b(?:exp(?:erimental)?\s+treat|treatment)\b", col, re.I)
                         for col in data.columns)
        if not time_candidate and experiment:
            result["skip_reason"] = "experimental_table_without_chronology"
            return result
        definitions = {col: self._variable_for_column(col, variables) for col in data.columns}
        missing = {str(value).casefold() for value in metadata.get("source_missing_values", [])} | {""}
        core_columns = [column for column, variable in definitions.items()
                        if self._is_declared_core_identifier(column, variable)]
        if len(core_columns) == 1:
            result["core_column"] = core_columns[0]

        def values_for(column):
            return (value for value in data[column] if pd.notna(value) and str(value).strip().casefold() not in missing)

        year_range = (metadata.get("time_coverage_start"), metadata.get("time_coverage_end"))
        date_orders = {column: order for column in data.columns
                       if re.search(r"\b(?:date|year|age|time)\b", column.replace("_", " "), re.I)
                       if (order := calendar_date_order(values_for(column), year_range=year_range))}
        result["date_orders"] = date_orders
        parallel_cores = {}
        trial_issues = []
        trial, proposed = self._standardize_columns(
            data, variables, metadata, file_path=file_path, issues=trial_issues, definitions=definitions,
            parallel_cores=parallel_cores
        )
        plan = self._build_column_plan(proposed, definitions)
        if self._can_route_natively(format_name, metadata, definitions, plan, trial_issues):
            for issue in trial_issues:
                add_review_item(issue)
            result.update(data=trial, column_mapping=proposed, column_plan=plan,
                          record_count=len(trial), mapping_route="archive_metadata",
                          parallel_cores=parallel_cores)
            logger.info("源档案定义直通: %s", os.path.basename(file_path))
            return result

        model_data = data.drop(columns=core_columns)
        table = {
            "headers": list(model_data.columns),
            "variable_definitions": definitions,
            "defined_units": {col: item.get("unit") for col, item in definitions.items()},
            "data": model_data.head(20).to_dict("records"),
            "archive_metadata": metadata,
            "date_orders": date_orders,
            "column_samples": {col: [str(value)[:100] for value in islice(values_for(col), 3)]
                               for col in model_data.columns},
            "file_path": file_path,
            "sheet_name": os.path.basename(file_path),
        }
        paper_context = paper_context_loader(metadata.get("pub1_doi")) if paper_context_loader and metadata.get("pub1_doi") else None
        decision = self.llm_parser.parse_table_structure(table, pdf_context=paper_context, known_variables=variables)
        table_metadata = decision.get("table_metadata", {})
        if table_metadata.get("is_coral_data") is False:
            result["skip_reason"] = "not_coral_data"
            return result
        data, mapping = self._standardize_columns(
            data, variables, metadata, decision.get("mapped_data", []), file_path,
            definitions=definitions, parallel_cores=parallel_cores
        )
        plan = self._build_column_plan(mapping, definitions)
        for item in decision.get("mapped_data", []):
            column = item.get("original_header")
            if column in plan and mapping[column] == item.get("standard_field") and item.get("unit_resolution"):
                plan[column]["unit_resolution"] = item["unit_resolution"]
        result.update(
            data=data, column_mapping=mapping, column_plan=plan,
            record_count=len(data), mapped_data=decision.get("mapped_data", []),
            table_metadata=table_metadata, mapping_route="model_assisted",
            parallel_cores=parallel_cores
        )
        if paper_context and paper_context.get("metadata_source"):
            result["paper_evidence"] = {key: paper_context[key] for key in ("metadata_source", "metadata_evidence")
                                        if key in paper_context}
        return result

    @staticmethod
    def _build_column_plan(mapping, definitions, unit_row=None):
        plan = {}
        for column, field in mapping.items():
            definition = definitions.get(column, {})
            plan[column] = {
                "standard_field": field,
                "unit_resolution": resolve_unit(field, column, (
                    (definition.get("source", "database_definition"), definition.get("unit")),
                    ("unit_row", (unit_row or {}).get(column)),
                )),
                "testing_method": definition.get("method"),
                "source_processing": definition.get("processing"),
                "composite_evidence": composite_evidence(column, " ".join(str(definition.get(key) or "")
                                                         for key in ("method", "processing", "notes"))),
                "definition": definition.get("full_definition"),
            }
        return plan

    def plan_workbook_columns(self, table):
        """复用 PaST 变量语义。工作簿可来自任意存档平台。"""
        variables = table.get("variables", [])
        if not variables:
            return None
        data = pd.DataFrame(columns=table["headers"])
        definitions = {column: self._variable_for_column(column, variables) for column in data.columns}
        self._bind_workbook_time_definition(table, variables, definitions)
        table["variable_definitions"] = definitions
        table["defined_units"] = {column: variable.get("unit") for column, variable in definitions.items() if variable}
        measures = [variable for variable in variables if self._field_from_noaa_definition(variable) in CORE_FIELDS]
        if measures and all(re.search(r"\bpeat core\b", variable.get("data_type", ""), re.I)
                            for variable in measures):
            return {"table_metadata": {"is_coral_data": False}}
        issues = []
        _, mapping = self._standardize_columns(data, variables, {}, file_path=table["file_path"],
                                                issues=issues, definitions=definitions)
        plan = self._build_column_plan(mapping, definitions, table.get("units"))
        if not self._can_route_natively("WORKBOOK", {}, definitions, plan, issues):
            return None
        for issue in issues:
            add_review_item(issue)
        return {"mapped_data": [{"original_header": column, **item} for column, item in plan.items()]}

    @staticmethod
    def _bind_workbook_time_definition(table, variables, definitions):
        """同一块只有一个年代列和一个明确的年代定义时，核对原值再接受别名。"""
        time_headers = [column for column in definitions if re.search(
            r"\b(?:year|age|date|time|chronology)\b|年代|日期|年份", column.replace("_", " "), re.I)]
        time_definitions = [item for item in variables if time_basis(item.get("unit"))]
        if len(time_headers) != 1 or len(time_definitions) != 1:
            return
        column, = time_headers
        definition, = time_definitions
        if definitions[column] or re.search(r"\b(?:min|max|error|relative|internal|elapsed)\b", column, re.I):
            return
        proposed = (table.get("layout_definitions") or {}).get(column)
        if proposed and proposed != definition:
            return
        basis = time_basis(definition["unit"])
        local_bases = {time_basis(label) for label in (column, (table.get("units") or {}).get(column))} - {None}
        if local_bases and local_bases != {basis}:
            return
        order = (table.get("date_orders") or {}).get(column)
        values = [row.get(column) for row in table.get("data", []) if pd.notna(row.get(column)) and str(row[column]).strip()]
        years = [year_ce(value, basis, date_order=order) for value in values]
        if len(years) < 2 or any(year is None for year in years):
            return
        span = re.search(r"\b(\d{4})\s*[-–—]\s*(\d{4})\b", definition.get("description", ""))
        if not local_bases and not proposed and (basis != "year" or not span):
            return
        if basis == "year" and span:
            start, end = sorted(map(int, span.groups()))
            if min(years) < start or max(years) >= end + 1:
                return
        definitions[column] = {**definition, "source": "workbook_definition_alias"}

    def _can_route_natively(self, format_name, metadata, definitions, plan, issues):
        """只有每个源列的定义可定位、目标指标和年代都明确时跳过模型。"""
        # 已确认排除的二次处理列和歧义钡钙通道不妨碍其余观测走档案直通。
        if (any(issue.get("action") != "已应用人工裁决"
                and issue.get("decision") not in {"二次处理序列", "Ba/Ca_value"} for issue in issues)
                or not all(definitions.values())
                or (format_name != "WORKBOOK" and not metadata.get("source_record_id"))):
            return False
        if len(metadata.get("source_events", {})) > 1:
            return False
        if format_name == "NOAA" and not all(item.get("definition_valid") for item in definitions.values()):
            return False
        if format_name == "PANGAEA" and not metadata.get("dataset_doi"):
            return False
        counts = {field: sum(item["standard_field"] == field for item in plan.values()) for field in TIME_FIELDS}
        primary = next((count for count in counts.values() if count), 0)
        if primary != 1 or not any(counts[field] for field in TIME_FIELDS[:-1]):
            return False
        converter = UnitConversionModule()
        measures = [(column, item) for column, item in plan.items()
                     if item["standard_field"] in UnitConversionModule.default_units]
        if not measures:
            return False
        for column, item in measures:
            definition = definitions[column]
            material = definition.get("material") or definition.get("description", "")
            if (item["standard_field"] != "sea_surface_temperature_value"
                    and (not CORAL_MATERIAL.search(material) or "sclerosponge" in material.casefold())):
                return False
            unit = item["unit_resolution"]
            if unit["status"] != "resolved" or converter.convert_value(item["standard_field"], 1, unit["unit"]) is None:
                return False
        # 关键指标的未知缩写或材质不能被其余已识别的列掩盖。
        return all(column in plan or self.is_non_observation(column, item)
                   or not re.search(r"Sr\s*/?\s*Ca|strontium|d(?:elta\s*)?(?:18O|13C)|δ\s*(?:18O|13C)",
                                    column + " " + item.get("description", ""), re.I)
                   for column, item in definitions.items())

    @staticmethod
    def is_sst(column, definition):
        label = str(column).replace("_", " ")
        if re.search(r"\b(?:lab(?:oratory)?|instrument|air|tank|culture|incubat\w*)\b|实验室|仪器|培养|气温", label, re.I):
            return False
        return bool(re.search(r"\bSST\b|\bsea[\s-]+surface[\s-]+temperature\b|海表温度",
                              " ".join((label, str(definition.get("quantity", "")))), re.I))

    @staticmethod
    def _is_declared_core_identifier(column, definition):
        return (definition.get("definition_valid") and str(definition.get("data_format", "")).upper() == "C"
                and str(definition.get("quantity", "")).strip().casefold() == "sample identification"
                and bool(re.search(r"\b(?:core|colony)\b|岩芯", f"{column} {definition.get('notes', '')}", re.I)))

    @staticmethod
    def _declared_parallel_core(column, definition):
        """从变量声明提取并列岩芯身份：notes 的 "from core X" 给岩芯名，
        processing/notes 的 composite 标记作者合成列。无声明返回 None。"""
        if not definition or not definition.get("definition_valid"):
            return None
        notes = str(definition.get("notes", ""))
        processing = str(definition.get("processing", ""))
        is_composite = bool(re.search(r"composit", f"{processing} {notes}", re.I))
        match = re.search(r"\bfrom core\s+([\w.-]+)", notes, re.I)
        if match:
            return {"core_id": match.group(1), "is_composite": is_composite}
        # 许多档案以裸 "core X" 标注岩芯身份（如 Amédée 3.txt：notes="core 92PAC"），
        # 与 "from core X" 同等确证。仅匹配单数 core 且编号位于末尾，避免误抓
        # "stack of cores A+B+C" 这类合成声明（后者按 is_composite 保留为合成序列）。
        match = re.search(r"\bcore\s+([\w.-]+)\s*$", notes, re.I)
        if match and not is_composite:
            return {"core_id": match.group(1), "is_composite": False}
        match = re.search(r"\b(?:geochemistry|measurements?|records?|data)\s+for\s+(?:core\s+)?([\w.-]+)\s*[.;]?\s*$",
                          notes, re.I)
        if match and re.search(rf"[-_ ]{re.escape(match[1])}$", column, re.I):
            return {"core_id": match[1], "is_composite": is_composite}
        if is_composite:
            return {"core_id": None, "is_composite": True}
        return None

    @classmethod
    def declared_parallel_series(cls, columns, definitions):
        declared = {col: cls._declared_parallel_core(col, definitions.get(col, {})) for col in columns}
        if all(declared.values()) and any(info["core_id"] for info in declared.values()):
            return declared
        months = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
        season_pattern = rf"(?:{months}\s*[-–]\s*{months}|(?:dry|wet|summer|winter|spring|autumn|fall)\s+season)"
        seasons = {col: str(definitions.get(col, {}).get("seasonality") or "").strip() for col in columns}
        if (len({season.casefold() for season in seasons.values()}) == len(columns)
                and all(definitions.get(col, {}).get("definition_valid")
                        and re.fullmatch(season_pattern, season, re.I) for col, season in seasons.items())
                and len({re.sub(r"[-_ ](?:dry|wet|summer|winter|spring|autumn|fall)$", "", col, flags=re.I).casefold()
                         for col in columns}) == 1):
            return {col: {"season": season} for col, season in seasons.items()}
        return {}

    @staticmethod
    def _pair_core_age_columns(parallel_cores, year_columns):
        """为并列岩芯的指标列按岩芯身份配对各自年代列（age-STM4↔Sr/Ca-STM4）。
        返回 {proxy_col: age_col}；仅唯一匹配时配对，避免误配。"""
        core_age = {}
        for field, cols in (parallel_cores or {}).items():
            for col, info in cols.items():
                core_id = info.get("core_id")
                if not core_id:
                    continue
                matches = [yc for yc in year_columns if core_id.casefold() in yc.casefold()]
                if len(matches) == 1:
                    core_age[col] = matches[0]
        return core_age

    @staticmethod
    def is_non_observation(column, definition):
        processing = str(definition.get("processing") or "")
        data_type = str(definition.get("data_type") or "")
        material = str(definition.get("material") or "")
        return (
            UnifiedDatabaseParser._is_declared_core_identifier(column, definition)
            or definition.get("secondary_processing")
            or is_derived_observation_column(column)
            or str(definition.get("error") or "").casefold() not in {"", "none", "n/a", "na", "-"}
            or bool(re.search(r"anomal|smooth|detrend|residual|normaliz|filter", processing, re.I))
            or ("climate reconstructions" in data_type.casefold()
                and definition.get("quantity", "").casefold() != "sea surface temperature")
            or bool(re.search(r"\b(?:seawater|water|standard gas|coralline alga(?:e)?|sclerosponge(?:s)?|"
                              r"ceratoporella|astrosclera|stromatospongia|goreauiella|hispidopetra|"
                              r"coral tissue|symbiont(?:s)?)\b", material, re.I))
        )

    @staticmethod
    def is_smoothed_derivative(column, definition):
        """平滑/滤波等保留绝对值的衍生序列（用于"有原始对照才剔除冗余"的选列判断）。"""
        processing = str(definition.get("processing") or "")
        return bool(re.search(r"smooth|filter", processing, re.I)) or is_derived_observation_column(column)

    @staticmethod
    def _smoothed_only_observation(column, definition):
        """仅因平滑/滤波被排除、且保留原单位绝对值的直接骨骼观测（非气候重建）。

        这类列在无原始对照列时应保留（如 Sadler2015 仅 4 点滑动平均 Sr/Ca）；
        有原始对照时由选列阶段按"优先原始序列"剔除。气候重建（如 Sr/Ca→SST 转换）
        或带误差/距平/海水等其它排除因素者不在此列。
        """
        processing = str(definition.get("processing") or "")
        if not re.search(r"smooth|filter", processing, re.I):
            return False
        if "climate reconstructions" in str(definition.get("data_type") or "").casefold():
            return False
        stripped = re.sub(r"smooth\w*|filter\w*", "", processing, flags=re.I)
        return not UnifiedDatabaseParser.is_non_observation(column, {**definition, "processing": stripped})

    @staticmethod
    def _field_from_noaa_definition(variable):
        quantity = re.sub(r"\s+", "", variable.get("quantity", "").casefold())
        return {
            "strontium/calcium": "Sr/Ca_value",
            "barium/calcium": "Ba/Ca_value",
            "delta18o": "δ18O_value",
            "delta13c": "δ13C_value",
            "seasurfacetemperature": "sea_surface_temperature_value",
        }.get(quantity)

    def _parse_noaa_metadata(self, lines: List[str]) -> Dict[str, Any]:
        """解析NOAA元数据"""
        metadata = {
            "geo_siteName": None,
            "geo_latitude": None,
            "geo_longitude": None,
            "pub1_doi": None,
            "pub1_authors": None,
            "time_coverage_start": None,
            "time_coverage_end": None,
            "data_type": None,
            "species": None,
            "location": None,
        }
        
        current_section = None
        
        for line in lines:
            line = line.strip()
            if not line.startswith('#'):
                continue
            
            line_content = line[1:].strip()  # 去除#
            
            # 检测章节
            if 'Site_Information' in line_content:
                current_section = 'site'
            elif 'Data_Collection' in line_content:
                current_section = 'collection'
            elif 'Publication' in line_content:
                current_section = 'publication'
            
            # 提取具体字段
            if 'Dataset_DOI:' in line_content:
                doi = line_content.split('Dataset_DOI:')[1].strip()
                if doi:
                    metadata['dataset_doi'] = doi
            
            elif 'Site_Name:' in line_content:
                metadata['geo_siteName'] = line_content.split('Site_Name:')[1].strip()
            
            elif 'Location:' in line_content and current_section == 'site':
                metadata['location'] = line_content.split('Location:')[1].strip()
            
            elif line_content.startswith(("Northernmost_Latitude:", "Southernmost_Latitude:",
                                          "Easternmost_Longitude:", "Westernmost_Longitude:")):
                label, value = line_content.split(":", 1)
                metadata.setdefault("geo_bounds", {})[label] = finite_number(value)

            elif line_content.startswith("Missing_Values:"):
                metadata["source_missing_values"] = line_content.split(":", 1)[1].strip().split()

            elif line_content.startswith("Collection_Name:"):
                metadata["source_record_id"] = line_content.split(":", 1)[1].strip()

            elif line_content.startswith("Time_Unit:") and current_section == "collection":
                metadata["source_time_unit"] = line_content.split(":", 1)[1].strip()

            elif line_content.startswith("Description:") and not metadata.get("archive_description"):
                metadata["archive_description"] = line_content.split(":", 1)[1].strip()

            elif line_content.startswith("Investigators:"):
                authors = line_content.split(":", 1)[1].strip()
                metadata["dataset_investigators"] = re.sub(r"\(https?://[^)]+\)", "", authors).strip()

            elif line_content.startswith("Authors:") and current_section == "publication":
                if not metadata.get("pub1_authors"):
                    metadata["pub1_authors"] = line_content.split(":", 1)[1].strip()

            elif line_content.startswith("DOI:") and current_section == "publication":
                if not metadata.get("pub1_doi"):
                    raw_doi = line_content.split(":", 1)[1].strip()
                    # 修复档案自身的DOI笔误：缺首1（如 0.1016/... 应为 10.1016/...）
                    if re.match(r"^0[.]\d{4,9}/", raw_doi):
                        logger.warning("修复缺首1的DOI: %s -> %s", raw_doi, "1" + raw_doi)
                        raw_doi = "1" + raw_doi
                    metadata["pub1_doi"] = correct_archive_doi(raw_doi)

            elif 'First_Year:' in line_content:
                try:
                    metadata['time_coverage_start'] = int(line_content.split(':')[1].strip())
                except (ValueError, IndexError) as e:
                    logger.debug(f"解析起始年份失败: {e}")
                    pass
            
            elif 'Last_Year:' in line_content:
                try:
                    metadata['time_coverage_end'] = int(line_content.split(':')[1].strip())
                except (ValueError, IndexError) as e:
                    logger.debug(f"解析结束年份失败: {e}")
                    pass
            
            elif 'Data_Type:' in line_content:
                metadata['data_type'] = line_content.split('Data_Type:')[1].strip()
            
            # 【新增】提取基点U/Th年龄（用于BP转换，中优先级改进）
            if 'U/Th' in line_content or 'U-Th' in line_content or 'U-Th Age' in line_content:
                uth_age_match = re.search(r'U[/-]Th\s+Age[:\s]+([\d.]+)\s*[±\s]*[\d.]*\s*(?:kyr|ka)', line_content, re.IGNORECASE)
                if not uth_age_match:
                    # 尝试其他格式
                    uth_age_match = re.search(r'([\d.]+)\s*[±\s]*[\d.]*\s*(?:kyr|ka)\s*(?:BP|before present)', line_content, re.IGNORECASE)
                if uth_age_match:
                    try:
                        metadata['base_uth_age_ka'] = float(uth_age_match.group(1))
                        logger.debug(f"从NOAA元数据提取基点年龄: {metadata['base_uth_age_ka']} ka")
                    except (ValueError, AttributeError) as e:
                        logger.debug(f"解析基点年龄失败: {e}")
                        pass
            
            # 从Description中提取物种信息
            elif 'Description:' in line_content:
                desc = line_content.split('Description:')[1].strip()
                # 尝试提取物种名
                species_match = re.search(r'(Porites\s+\w+|Diploria\s+\w+|Ceratoporella\s+\w+)', desc, re.IGNORECASE)
                if species_match:
                    metadata['species'] = species_match.group(1)
                # 【新增】从Description中提取基点U/Th年龄
                if 'U/Th' in desc or 'U-Th' in desc:
                    uth_age_match = re.search(r'U[/-]Th\s+Age[:\s]+([\d.]+)\s*[±\s]*[\d.]*\s*(?:kyr|ka)', desc, re.IGNORECASE)
                    if not uth_age_match:
                        uth_age_match = re.search(r'([\d.]+)\s*[±\s]*[\d.]*\s*(?:kyr|ka)\s*(?:BP|before present)', desc, re.IGNORECASE)
                    if uth_age_match:
                        try:
                            metadata['base_uth_age_ka'] = float(uth_age_match.group(1))
                            logger.debug(f"从NOAA Description提取基点年龄: {metadata['base_uth_age_ka']} ka")
                        except (ValueError, AttributeError) as e:
                            logger.debug(f"从Description解析基点年龄失败: {e}")
                            pass
        
        return metadata

    def _structural_decision(self, file_path, action, field=None):
        def digest(path):
            with open(path, "rb") as stream:
                return hashlib.file_digest(stream, "sha256").hexdigest()

        return structural_review_decision(getattr(self, "review_decisions", {}), file_path,
                                          os.path.basename(file_path or ""), action, field, digest)

    def _record_structural_application(self, key, file, decision, field=None, record=add_review_item):
        getattr(self, "applied_structural_reviews", set()).add(key)
        record({"file": file, "sheet": os.path.basename(file), "column": decision["selected"],
                "decision": field, "action": "已应用人工裁决", "review_decision": decision["action"],
                "reason": decision["note"], "source_sha256": decision["source_sha256"]})

    def _resolve_noaa_location(self, metadata, file_path):
        bounds = metadata.get("geo_bounds", {})
        key, dec = self._structural_decision(file_path, "confirm_coordinates")
        if dec:
            lat, lon = finite_number(dec["lat"]), finite_number(dec["lon"])
            if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
                metadata.update(geo_latitude=lat, geo_longitude=lon, geo_location_source="manual_review",
                                geo_rep_status="reviewed", geo_location_evidence=dec["note"])
                self._record_structural_application(key, file_path, dec)
                return
        if not bounds:
            return
        limits = [bounds.get(key) for key in ("Southernmost_Latitude", "Northernmost_Latitude",
                                              "Westernmost_Longitude", "Easternmost_Longitude")]
        south, north, west, east = limits
        if all(value is not None for value in limits) and -90 <= south <= north <= 90 and -180 <= west <= east <= 180:
            if south == north and west == east:
                metadata.update(geo_latitude=south, geo_longitude=west, geo_location_source="bounds_point")
                return
            metadata.update(geo_rep_latitude=(south + north) / 2, geo_rep_longitude=(west + east) / 2,
                            geo_lat_span=north - south, geo_lon_span=east - west,
                            geo_location_source="bounds_midpoint", geo_rep_status="pending_review")
            key = f"{metadata.get('dataset_doi')}|{metadata.get('source_record_id')}"
            decision = self.noaa_reviewed_representatives.get(key, {})
            if limits == decision.get("bounds_south_north_west_east") and decision.get("evidence"):
                metadata.update(geo_rep_status="reviewed_regional_composite", geo_location_evidence=decision["evidence"])
                return
            reason = "档案给出空间范围，代表点待核对，精确坐标留空"
        else:
            key = f"{metadata.get('dataset_doi')}|{metadata.get('source_record_id')}"
            decision = self.noaa_reviewed_representatives.get(key, {})
            if (all(value is not None for value in limits)
                    and limits == decision.get("bounds_south_north_west_east") and decision.get("evidence")):
                # 已核验区域复合：档案边界顺序异常（E/W标签互换或跨180°）时取小跨度解释计算代表中点
                span_wrap = (east - west) % 360
                span_swap = west - east
                if span_wrap <= span_swap:
                    lon, span_lon = (west + span_wrap / 2 + 180) % 360 - 180, span_wrap
                else:
                    lon, span_lon = (west + east) / 2, span_swap
                metadata.update(geo_rep_latitude=(min(south, north) + max(south, north)) / 2,
                                geo_rep_longitude=lon, geo_lat_span=abs(north - south), geo_lon_span=span_lon,
                                geo_location_source="reviewed_bounds_midpoint",
                                geo_rep_status="reviewed_regional_composite",
                                geo_location_evidence=decision["evidence"])
                return
            reason = "边界不完整、越界或顺序异常，需确认坐标及是否跨越 180°"
        add_review_item({"file": file_path, "source_record_id": metadata.get("source_record_id"),
                         "dataset_doi": metadata.get("dataset_doi"), "action": "复核空间范围", "reason": reason,
                         "geo_bounds": json.dumps(bounds), "geo_rep_latitude": metadata.get("geo_rep_latitude"),
                         "geo_rep_longitude": metadata.get("geo_rep_longitude"), "geo_lat_span": metadata.get("geo_lat_span"),
                         "geo_lon_span": metadata.get("geo_lon_span")})
    
    def _parse_noaa_variables(self, lines):
        """读取 PaST 变量声明，按 CSV 语法保留带引号的逗号。"""
        variables = []
        keys = ("quantity", "material", "error", "unit", "seasonality", "data_type",
                "processing", "method", "data_format", "notes")
        for line in lines:
            entry = line.lstrip()
            if not entry.startswith("##") or "\t" not in entry:
                continue
            name, definition = entry[2:].strip().split("\t", 1)
            try:
                values = next(csv.reader([definition], strict=True))
            except csv.Error:
                values = []
            variable = dict(zip(keys, (value.strip() for value in values)))
            variable.update(name=name.strip(), description=definition, full_definition=entry,
                            definition_valid=len(values) in {9, 10} and variable.get("data_format", "").upper() in {"N", "C"})
            variable.update(self._classify_time_column(
                variable["name"], variable.get("quantity", ""), variable.get("unit", "")
            ))
            variables.append(variable)
        return variables

    def _is_coral_data(self, variables: List[Dict[str, str]]) -> bool:
        """
        判断NOAA数据文件是否包含珊瑚相关数据
        
        Args:
            variables: 从# Variables部分解析出的变量列表
            
        Returns:
            bool: True表示是珊瑚数据，False表示是硬海绵或其他非珊瑚数据
        """
        if not variables:
            # 如果没有变量定义，默认认为是珊瑚数据（保守策略）
            return True
        
        # 珊瑚属名关键词（常见珊瑚属）
        coral_genera = [
            'porites', 'acropora', 'orbicella', 'montastrea', 'montastraea',
            'diploria', 'colpophyllia', 'siderastrea', 'pavona', 'pocillopora',
            'favia', 'favites', 'platygyra', 'galaxea', 'turbinaria',
            'fungia', 'lobophyllia', 'euphyllia', 'coral', 'corals'
        ]
        
        # 硬海绵关键词
        sclerosponge_keywords = [
            'sclerosponge', 'sclerosponges', 'ceratoporella', 'astrosclera',
            'stromatospongia', 'goreauiella', 'hispidopetra'
        ]
        
        # 检查所有变量的material字段
        has_coral = False
        has_sclerosponge = False
        
        for var in variables:
            material = var.get('material', '').lower()
            if not material:
                continue
            
            # 检查是否包含硬海绵关键词
            if any(keyword in material for keyword in sclerosponge_keywords):
                has_sclerosponge = True
                logger.debug(f"检测到硬海绵材料: {material} (变量: {var.get('name')})")
            
            # 检查是否包含珊瑚关键词
            if any(genus in material for genus in coral_genera):
                has_coral = True
                logger.debug(f"检测到珊瑚材料: {material} (变量: {var.get('name')})")
        
        # 判断逻辑：
        # 1. 如果明确包含硬海绵且不包含珊瑚，则不是珊瑚数据
        # 2. 如果包含珊瑚，则是珊瑚数据
        # 3. 如果都不包含，默认认为是珊瑚数据（保守策略，因为可能material字段为空）
        if has_sclerosponge and not has_coral:
            logger.info(f"检测到硬海绵数据，将跳过此文件（material字段: {[v.get('material') for v in variables if v.get('material')]}）")
            return False
        
        if has_coral:
            logger.debug(f"确认是珊瑚数据（检测到珊瑚属名）")
            return True
        
        # 默认认为是珊瑚数据（保守策略）
        logger.debug(f"未明确检测到珊瑚或硬海绵关键词，默认认为是珊瑚数据")
        return True
    
    def _parse_noaa_data(self, lines: List[str], file_path) -> pd.DataFrame:
        """保留制表符表示的空单元格和列位置。"""
        return self._read_tabular_lines(((number, line) for number, line in enumerate(lines, 1)
                                         if not line.lstrip().startswith("#")), file_path)

    @staticmethod
    def _read_tabular_lines(lines, file_path):
        """保留制表符的空单元格和档案物理行号。"""
        indexed = [(number, line) for number, line in lines if line.strip()]
        reader = csv.reader((line for _, line in indexed), delimiter="\t", strict=True)
        headers = [header.strip() for header in next(reader, [])]
        if not headers:
            return pd.DataFrame()
        if len(headers) != len(set(headers)):
            add_review_item({"file": file_path, "sheet": os.path.basename(file_path),
                             "source_line": indexed[0][0], "action": "暂缓表格", "reason": "档案存在重复列名"})
            return pd.DataFrame()
        rows = []
        source_lines = []
        offset = reader.line_num
        for values in reader:
            line = indexed[offset][0]
            offset = reader.line_num
            if len(values) > len(headers):
                add_review_item({"file": file_path, "sheet": os.path.basename(file_path),
                                 "source_line": line, "action": "暂缓表格", "reason": "档案数据行列数多于表头"})
                return pd.DataFrame()
            rows.append(values + [""] * (len(headers) - len(values)))
            source_lines.append(line)
        return pd.DataFrame(rows, columns=headers, index=source_lines)

    def _split_pangaea_content(self, content: str) -> Tuple[str, str]:
        """分离PANGAEA的元数据块和数据区"""
        # 查找 */ 结束标记
        end_marker = '*/'
        end_pos = content.find(end_marker)
        
        if end_pos != -1:
            metadata_block = content[:end_pos + len(end_marker)]
            data_block = content[end_pos + len(end_marker):].strip("\r\n")
        else:
            # 没有找到标记，尝试按行分割
            lines = content.split('\n')
            metadata_lines = []
            data_start_idx = 0
            
            for i, line in enumerate(lines):
                if line.startswith('/*') or line.startswith('Citation:') or line.startswith('Abstract:'):
                    metadata_lines.append(line)
                elif not line.startswith(('#', '/*', 'Citation', 'Abstract', 'Project', 'Coverage', 'Event', 'Comment', 'Parameter', 'License', 'Size')):
                    if line.strip() and '\t' in line:
                        data_start_idx = i
                        break
            
            metadata_block = '\n'.join(lines[:data_start_idx])
            data_block = '\n'.join(lines[data_start_idx:])
        
        return metadata_block, data_block
    
    def _parse_pangaea_metadata(self, metadata_block: str) -> Dict[str, Any]:
        """解析PANGAEA元数据"""
        metadata = {
            "geo_siteName": None,
            "geo_latitude": None,
            "geo_longitude": None,
            "pub1_doi": None,
            "pub1_authors": None,
            "time_coverage_start": None,
            "time_coverage_end": None,
            "data_type": "Coral",
            "species": None,
            "location": None,
        }
        
        citation = re.search(r"(?m)^Citation:[ \t]*([^\n]+)", metadata_block)
        if citation:
            doi = re.search(r"10\.1594/PANGAEA\.\d+", citation.group(1), re.I)
            if doi:
                metadata["dataset_doi"] = doi.group()
        description = re.search(r"(?mi)^[ \t]*(?:Abstract|Comment):[ \t]*([^\n]+)", metadata_block)
        if description:
            metadata["archive_description"] = description.group(1).strip()
        supplement = re.search(r"(?mi)^[ \t]*Supplement to:[ \t]*([^\n]+)", metadata_block)
        if supplement:
            doi = re.search(r"(?:doi\.org/|doi:\s*)(10\.\d+/[^\s,;]+)", supplement.group(1), re.I)
            if doi and not doi.group(1).upper().startswith("10.1594/PANGAEA"):
                metadata["pub1_doi"] = doi.group(1).rstrip(".")
                metadata["pub1_authors"] = supplement.group(1).split(" (", 1)[0].strip()

        # 提取坐标
        lat_match = re.search(r'(?:^|[\t*])\s*LATITUDE:\s*([-\d.]+)', metadata_block)
        lon_match = re.search(r'(?:^|[\t*])\s*LONGITUDE:\s*([-\d.]+)', metadata_block)
        if lat_match:
            try:
                metadata['geo_latitude'] = float(lat_match.group(1))
            except (ValueError, AttributeError) as e:
                logger.debug(f"解析PANGAEA纬度失败: {e}")
                pass
        if lon_match:
            try:
                metadata['geo_longitude'] = float(lon_match.group(1))
            except (ValueError, AttributeError) as e:
                logger.debug(f"解析PANGAEA经度失败: {e}")
                pass
        
        # 提取地点名称 (从LOCATION或Event行)
        location_match = re.search(r'LOCATION:\s*([^\n*]+)', metadata_block)
        if location_match:
            metadata['geo_siteName'] = location_match.group(1).strip()
            metadata['location'] = location_match.group(1).strip()
        
        events = {}
        event_block = re.search(r"(?m)^Event\(s\):[^\S\n]*([^\n]+(?:\n[ \t]+[^\n]+)*)", metadata_block)
        if event_block:
            for line in event_block.group(1).splitlines():
                # 跳过事件行COMMENT的换行续行（无*分隔符），避免幻影事件触发多事件清空规则
                if "*" not in line:
                    continue
                label = line.split("*", 1)[0].strip()
                event_id = label.split(" (", 1)[0]
                if not event_id:
                    continue
                # 兼容钻探轨迹格式 LATITUDE START / LONGITUDE START（END为0或缺失时取START为代表点）
                lat = re.search(r"\*\s*LATITUDE(?:\s+START)?:\s*([-\d.]+)", line)
                lon = re.search(r"\*\s*LONGITUDE(?:\s+START)?:\s*([-\d.]+)", line)
                location = re.search(r"\*\s*LOCATION:\s*([^*]+)", line)
                events[event_id] = {
                    "geo_siteName": location.group(1).strip() if location else label,
                    "geo_latitude": finite_number(lat.group(1)) if lat else None,
                    "geo_longitude": finite_number(lon.group(1)) if lon else None,
                    "source_record_id": event_id,
                }
            metadata["source_events"] = events
        if len(events) == 1:
            event = next(iter(events.values()))
            metadata["source_record_id"] = event["source_record_id"]
            for field in ("geo_latitude", "geo_longitude", "geo_siteName"):
                metadata[field] = event[field] if event[field] is not None else metadata.get(field)
        elif len(events) > 1:
            # 覆盖范围与第一事件都不能代表所有观测。
            for field in ("geo_latitude", "geo_longitude", "geo_siteName", "source_record_id"):
                metadata[field] = None

        # 提取作者
        citation_match = re.search(r'Citation:\s*([^(]+)', metadata_block)
        if citation_match:
            authors_text = citation_match.group(1).strip()
            # 清理年份
            authors_text = re.sub(r'\(\d{4}\)', '', authors_text)
            # 取第一个冒号前的部分（作者列表）
            if ':' in authors_text:
                authors_text = authors_text.split(':')[0]
            metadata['dataset_investigators'] = authors_text.strip().rstrip(';').strip()
        
        # 提取物种
        species_match = re.search(r'(Porites\s+\w+|Diploria\s+\w+|Ceratoporella\s+\w+)', metadata_block, re.IGNORECASE)
        if species_match:
            metadata['species'] = species_match.group(1)
        
        # 提取时间范围（从Comment或文件名）
        time_match = re.search(r'(\d{4})\s*[-–to]\s*(\d{4})', metadata_block)
        if time_match:
            try:
                metadata['time_coverage_start'] = int(time_match.group(1))
                metadata['time_coverage_end'] = int(time_match.group(2))
            except (ValueError, AttributeError) as e:
                logger.debug(f"解析PANGAEA时间范围失败: {e}")
                pass
        
        # 【新增】提取测量年份（用于ka BP到CE转换）
        # 从Comment中查找"AD 2014"或"year of measurement (which is AD 2014)"等格式
        measurement_year_match = re.search(r'(?:year of measurement|measured|sampled).*?AD\s*(\d{4})', metadata_block, re.IGNORECASE)
        if measurement_year_match:
            try:
                metadata['measurement_year'] = int(measurement_year_match.group(1))
            except (ValueError, AttributeError) as e:
                logger.debug(f"解析PANGAEA测量年份失败: {e}")
                pass
        
        # 【新增】提取基部U/Th年龄（用于Int chron [a]转换）
        # 从Comment中查找"U/Th Age: 123.9 ± 1.3 kyr"等格式
        uth_age_match = re.search(r'U[/-]Th\s+Age[:\s]+([\d.]+)\s*[±\s]*[\d.]*\s*(?:kyr|ka)', metadata_block, re.IGNORECASE)
        if not uth_age_match:
            # 尝试其他格式
            uth_age_match = re.search(r'([\d.]+)\s*[±\s]*[\d.]*\s*(?:kyr|ka)\s*(?:BP|before present)', metadata_block, re.IGNORECASE)
        if uth_age_match:
            try:
                metadata['base_uth_age_ka'] = float(uth_age_match.group(1))
                logger.info(f"【成功】从PANGAEA元数据提取基点年龄: {metadata['base_uth_age_ka']} ka")
            except (ValueError, AttributeError) as e:
                logger.warning(f"解析PANGAEA基点年龄失败: {e}")
                pass
        else:
            # 【优化】U/Th Age 仅针对化石珊瑚的相对年代学是必须的，对于现代珊瑚或已有CE年代的数据是非必须的
            # 因此降级为 DEBUG 日志，避免误报警告
            logger.debug(f"未在PANGAEA元数据中找到U/Th Age信息（对于现代珊瑚数据正常）")
        
        # 如果没有找到，使用默认值2014（常见于古气候数据）
        return metadata
    
    def _parse_pangaea_variables(self, metadata_block: str) -> List[Dict[str, str]]:
        """解析PANGAEA变量定义"""
        variables = []
        
        # 查找Parameter(s)段落
        param_match = re.search(r'Parameter\(s\):(.*?)(?=License:|Size:|$)', metadata_block, re.DOTALL)
        if param_match:
            param_text = param_match.group(1)
            
            # 按行解析
            for line in param_text.split('\n'):
                line = line.strip()
                if not line:
                    continue
                
                # 解析格式: Variable [unit] (short_name) * PI: ...
                # 或: Species, Variable [unit] (short_name) * PI: ...
                
                # 提取单位
                unit_match = re.search(r'\[([^\]]+)\]', line)
                unit = unit_match.group(1) if unit_match else ""
                if not unit and (comment := re.search(r"\*\s*COMMENT:\s*([^*]+)", line, re.I)):
                    label = comment.group(1).strip()
                    if canonical_unit(label) in {"mol/mol", "mmol/mol", "μmol/mol", "nmol/mol"}:
                        unit = label
                
                # 提取短名
                short_match = re.search(r'\(([^)]+)\)', line)
                short_name = short_match.group(1) if short_match else ""
                
                # 提取变量描述（[单位]之前的部分）
                desc_match = re.search(r'^([^[]+)', line)
                description = desc_match.group(1).strip() if desc_match else line
                
                if short_name or description:
                    var_info = {
                        "name": short_name or description.split(',')[0].strip(),
                        "description": description,
                        "unit": unit,
                        "method": (method.group(1).strip() if (method := re.search(
                            r"\*\s*METHOD(?:/DEVICE)?:\s*([^*]+)", line, re.I)) else ""),
                        "full_definition": line
                    }
                    # 【新增】对时间列进行类型分类
                    time_info = self._classify_time_column(var_info["name"], description, unit)
                    var_info.update(time_info)
                    variables.append(var_info)
        
        return variables
    
    def _classify_time_column(self, col_name: str, description: str, unit: str) -> Dict[str, Any]:
        """
        【新增】判断时间列类型并分类
        
        时间列类型：
        - absolute_ce: 绝对CE年龄（公元纪年），如 "year Common Era", "a AD/CE"
        - absolute_bp: BP年龄，如 "ka BP", "year BP"
        - internal_chronology: 内部年表（需用U/Th年龄转换为CE）
        - relative_continuous: 相对连续年数（珊瑚生长年数，需排除）
        
        Args:
            col_name: 列名
            description: 描述文本
            unit: 单位
            
        Returns:
            Dict: 时间列分类信息
        """
        col_lower = col_name.lower() if col_name else ""
        desc_lower = description.lower() if description else ""
        unit_lower = unit.lower() if unit else ""
        
        result = {
            "is_time_column": False,
            "time_column_type": None,
            "is_relative_age": False,
            "requires_conversion": False
        }
        
        # 检查是否为时间相关列
        time_keywords = ["age", "year", "time", "chronology", "date", "chron"]
        is_time_related = any(kw in col_lower or kw in desc_lower for kw in time_keywords)
        
        if not is_time_related:
            return result
        
        result["is_time_column"] = True
        
        # 1. 检查是否为内部年表（Internal coral chronology）
        internal_chron_keywords = [
            "internal chronology", "internal coral chronology", 
            "int chron", "coral chronology", "internal coral"
        ]
        if any(kw in desc_lower or kw in col_lower for kw in internal_chron_keywords):
            result["time_column_type"] = "internal_chronology"
            result["requires_conversion"] = True
            result["is_relative_age"] = True  # 内部年表实际上是相对年龄，但可以转换
            logger.debug(f"列 '{col_name}' 识别为内部年表（需转换）")
            return result
        
        # 2. 检查是否为相对连续年数（珊瑚生长的相对年数）- 需要排除
        relative_keywords = [
            "relative", "number of years", "relative age", 
            "continuous year", "growth year", "relative number"
        ]
        if any(kw in desc_lower for kw in relative_keywords):
            result["time_column_type"] = "relative_continuous"
            result["is_relative_age"] = True
            result["requires_conversion"] = False  # 不转换，直接排除
            logger.info(f"列 '{col_name}' 识别为相对连续年数（将排除）")
            return result
        
        # 3. 检查是否为绝对CE年龄
        ce_keywords = ["common era", "ad/ce", "a ad", "a ce", "year ad", "year ce"]
        if any(kw in unit_lower or kw in desc_lower for kw in ce_keywords):
            result["time_column_type"] = "absolute_ce"
            result["is_relative_age"] = False
            logger.debug(f"列 '{col_name}' 识别为绝对CE年龄")
            return result
        
        # 4. 检查是否为BP年龄
        bp_keywords = ["bp", "before present", "ka bp", "kyr bp"]
        if any(kw in unit_lower or kw in desc_lower for kw in bp_keywords):
            result["time_column_type"] = "absolute_bp"
            result["requires_conversion"] = True
            result["is_relative_age"] = False
            logger.debug(f"列 '{col_name}' 识别为BP年龄（需转换）")
            return result
        
        # 5. 单位仅为 [a] 且没有其他明确标识，可能是相对年数，需要数据判断
        if unit_lower == "a" and not any(kw in desc_lower for kw in ["ad", "ce", "bp"]):
            result["time_column_type"] = "unknown"
            result["needs_data_check"] = True
            logger.debug(f"列 '{col_name}' 时间类型未知，需结合数据判断")
            return result
        
        # 默认为CE
        result["time_column_type"] = "absolute_ce"
        return result
    
    def _check_relative_age_by_data(self, col_name: str, data_series: pd.Series) -> bool:
        """
        【新增】通过数据特征判断时间列是否为相对连续年数
        
        相对连续年数的特征：
        1. 第一个值接近0（<10）
        2. 数值较小（最大值<100或200）
        3. 数据递增，间隔通常小于2年
        
        Args:
            col_name: 列名
            data_series: 数据序列
            
        Returns:
            bool: True表示是相对连续年数，应该排除
        """
        try:
            # 转换为数值
            values = pd.to_numeric(data_series, errors='coerce').dropna()
            if len(values) < 3:
                return False
            
            first_val = values.iloc[0]
            max_val = values.max()
            
            # 特征1：第一个值接近0
            if first_val > 10:
                return False
            
            # 特征2：最大值较小（一般珊瑚生长年数不会超过100-200年）
            if max_val > 200:
                return False
            
            # 特征3：检查是否大致递增
            diffs = values.diff().dropna()
            if len(diffs) == 0:
                return False
            
            # 大部分差值应该是正的（递增）
            positive_ratio = (diffs > 0).sum() / len(diffs)
            if positive_ratio < 0.7:
                return False
            
            # 特征4：间隔较小（通常<2年）
            median_interval = diffs.median()
            if median_interval > 2:
                return False
            
            logger.info(f"列 '{col_name}' 通过数据特征识别为相对连续年数（首值={first_val:.2f}, 最大值={max_val:.2f}, 中位间隔={median_interval:.3f}）")
            return True
            
        except Exception as e:
            logger.debug(f"数据特征判断失败: {e}")
            return False
    
    def _filter_relative_age_columns(self, df: pd.DataFrame, variables: List[Dict], 
                                      column_mapping: Dict[str, str]) -> Tuple[Dict[str, str], List[str]]:
        """
        【新增】过滤相对年龄列
        
        Args:
            df: 数据DataFrame
            variables: 变量定义列表（包含时间列分类信息）
            column_mapping: 当前列映射
            
        Returns:
            Tuple[Dict, List]: (过滤后的列映射, 被排除的列名列表)
        """
        excluded_cols = []
        
        # 构建变量名到变量信息的映射
        var_info_map = {}
        for var in variables:
            var_name = var.get("name", "")
            var_info_map[var_name.lower()] = var
            # 也添加全名匹配（如 "Age [a]"）
            if var.get("unit"):
                full_name = f"{var_name} [{var.get('unit')}]"
                var_info_map[full_name.lower()] = var
        
        for col, std_field in list(column_mapping.items()):
            # 只处理时间相关列
            if std_field not in ['year', 'age_ka_bp', 'age_bp', 'internal_chronology']:
                continue
            
            col_lower = col.lower()
            var_info = var_info_map.get(col_lower)
            
            # 1. 通过变量定义检查
            if var_info:
                if var_info.get("time_column_type") == "relative_continuous":
                    logger.info(f"根据变量定义排除相对连续年数列: {col}")
                    excluded_cols.append(col)
                    del column_mapping[col]
                    continue
                
                # 如果类型未知，需要数据判断
                if var_info.get("needs_data_check") and col in df.columns:
                    if self._check_relative_age_by_data(col, df[col]):
                        logger.info(f"根据数据特征排除相对连续年数列: {col}")
                        excluded_cols.append(col)
                        del column_mapping[col]
                        continue
            
            # 2. 如果没有变量信息，也尝试数据特征判断
            elif col in df.columns:
                # 检查列名是否暗示相对年龄
                relative_hints = ["relative", "internal", "int chron", "growth"]
                if any(hint in col_lower for hint in relative_hints):
                    if self._check_relative_age_by_data(col, df[col]):
                        logger.info(f"根据数据特征排除相对连续年数列: {col}")
                        excluded_cols.append(col)
                        del column_mapping[col]
                        continue
        
        return column_mapping, excluded_cols
    
    def _parse_pangaea_data(self, data_block: str, first_line, file_path) -> pd.DataFrame:
        """读取 PANGAEA 制表符数据块。"""
        return self._read_tabular_lines(enumerate(data_block.splitlines(), first_line), file_path)

    @staticmethod
    def _variable_for_column(column, variables):
        key = column.strip().casefold()
        matches = [
            item for item in variables
            if key in {
                str(item.get("name", "")).casefold(),
                f"{item.get('name', '')} [{item.get('unit', '')}]".casefold(),
                f"{item.get('description', '')} [{item.get('unit', '')}]".casefold(),
            }
        ]
        if not matches:
            base = re.sub(r"\s*\[[^\]]+\]\s*$", "", key).strip()
            matches = [item for item in variables if base in {
                str(item.get("name", "")).strip().casefold(),
                str(item.get("description", "")).strip().casefold(),
            }]
        return matches[0] if len(matches) == 1 else {}

    def _standardize_columns(self, df: pd.DataFrame, variables: List[Dict], 
                            metadata: Dict[str, Any], decisions=(), file_path="", issues=None, definitions=None,
                            parallel_cores=None) -> Tuple[pd.DataFrame, Dict[str, str]]:
        """
        标准化列名
        
        Args:
            df: 数据DataFrame
            variables: 变量定义列表
            metadata: 元数据
            
        Returns:
            Tuple[DataFrame, Dict]: 标准化后的DataFrame和列映射
        """
        record_issue = issues.append if issues is not None else add_review_item
        # 二次处理序列拦截：档案注解（PANGAEA COMMENT / NOAA notes/processing）标明
        # 已明确为 centered/anomaly 的列不作为绝对值提取，记录排除原因。
        # （典型案例：Todorovic2025 "centered by removing the 1850-1995 mean"、
        #   Sanchez2020 "d18O_scaled"，其值围绕0，物理范围检查无法捕获）
        for col, defn in (definitions or {}).items():
            if not defn:
                continue
            text = " ".join(str(defn.get(k, "")) for k in ("description", "notes", "processing"))
            if re.search(r"centered|centred|anomal|de-?mean|removing the\s+\S+\s+mean", text, re.I):
                defn["secondary_processing"] = True
                record_issue({
                    "file": file_path, "sheet": os.path.basename(file_path or ""), "column": col,
                    "decision": "二次处理序列", "confidence": "",
                    "action": "已排除非观测片段", "reason": "档案注解标明中心化/缩放/距平二次处理序列，绝对值不可入数据库",
                })
                logger.info("排除二次处理序列列: %s (%s)", col, os.path.basename(file_path or ""))
        column_mapping = {}
        columns_to_drop = []
        custom_datum_columns = []
        model_columns = {str(item.get("original_header", "")).strip().casefold(): item
                         for item in decisions if isinstance(item, dict)}
        recognized = {*self.field_mapping.values(), "age_bp", "age_bce", "δ18O_difference"}

        for col in df.columns:
            col_lower = col.lower().strip()
            
            # 检查是否是水文信号列（需要过滤）
            is_hydro = False
            for pattern in self.hydro_signal_patterns:
                if re.search(pattern, col_lower, re.IGNORECASE):
                    is_hydro = True
                    columns_to_drop.append(col)
                    logger.info(f"过滤水文信号列: {col}")
                    break
            
            if is_hydro:
                continue
            
            model = model_columns.get(col_lower, {})
            candidate = model.get("standard_field")
            variable = definitions.get(col, {}) if definitions is not None else self._variable_for_column(col, variables)
            if candidate == "skip" or is_derived_observation_column(col):
                continue
            if (re.search(r"\b(?:age|year|yr|date|time|chron\w*)\b", col_lower.replace("_", " "))
                    or candidate in {"year", "age_bp", "age_ka_bp", "age_bce", "internal_chronology"}):
                if (re.search(r"\b(?:min(?:imum)?|max(?:imum)?|error|age e|limit)\b", col_lower.replace("_", " "))
                        or str(variable.get("error") or "").casefold() not in {"", "none", "n/a", "na", "-"}):
                    continue
                if re.search(r"\b(?:int(?:ernal)? chron\w*)\b", col_lower):
                    column_mapping[col] = "internal_chronology"
                    continue
                if re.search(r"\b(?:relative|growth|elapsed)\b", col_lower):
                    continue
                if "before other datum" in str(variable.get("unit") or "").casefold():
                    # 备注中的“present defined as ... CE”描述零点，列值是相对该零点的年龄。
                    custom_datum_columns.append(col)
                    continue
                header_basis = time_basis(col)
                variable_basis = time_basis(variable.get("unit")) or time_basis(variable.get("description"))
                basis = header_basis or variable_basis or time_basis(metadata.get("source_time_unit"))
                if header_basis and variable_basis and header_basis != variable_basis:
                    basis = None
                if basis:
                    column_mapping[col] = basis
                else:
                    record_issue({"file": file_path, "column": col, "action": "暂缓时间列",
                                     "reason": "源年代基准未明确或相互冲突"})
                continue

            if self.is_non_observation(col, variable) and not self._smoothed_only_observation(col, variable):
                continue

            definition_field = self._field_from_noaa_definition(variable)
            if definition_field and variable.get("definition_valid"):
                if definition_field != "sea_surface_temperature_value" or self.is_sst(col, variable):
                    column_mapping[col] = definition_field
                continue

            # 尝试直接映射（优先匹配更精确/更长的键）
            mapped = False
            # 按键长度降序排序，确保更精确的匹配优先（如 'age [ka bp]' 优先于 'age'）
            sorted_keys = sorted(self.field_mapping.keys(), key=len, reverse=True)
            for key in sorted_keys:
                std_field = self.field_mapping[key]
                if col_lower == key or (key in col_lower and len(key) > 3):
                    # 对于短键（如'age'），要求完全匹配以避免误匹配
                    column_mapping[col] = std_field
                    mapped = True
                    break
            
            # 如果没有直接映射，尝试模糊匹配
            if not mapped:
                # 检查是否包含关键指标名
                if 'sr' in col_lower and 'ca' in col_lower:
                    column_mapping[col] = 'Sr/Ca_value'
                elif 'd18o' in col_lower or 'δ18o' in col_lower or 'delta 18' in col_lower:
                    # 排除anomaly和seawater
                    if 'anom' not in col_lower and 'sw' not in col_lower:
                        column_mapping[col] = 'δ18O_value'
                elif 'd13c' in col_lower or 'δ13c' in col_lower or 'delta 13' in col_lower:
                    if 'anom' not in col_lower:
                        column_mapping[col] = 'δ13C_value'
                elif 'ba' in col_lower and 'ca' in col_lower:
                    column_mapping[col] = 'Ba/Ca_value'
            if col not in column_mapping and candidate in recognized:
                column_mapping[col] = candidate
            if column_mapping.get(col) == "sea_surface_temperature_value" and not self.is_sst(col, variable):
                del column_mapping[col]
                record_issue({"file": file_path, "column": col, "action": "暂缓该指标",
                              "reason": "没有证据确认该温度为海表温度"})

        column_mapping, excluded_cols = self._filter_relative_age_columns(df, variables, column_mapping)
        columns_to_drop.extend(excluded_cols)
        if custom_datum_columns and not set(column_mapping.values()).intersection(TIME_FIELDS):
            record_issue({"file": file_path, "column": " | ".join(custom_datum_columns),
                          "action": "暂缓时间列", "reason": "仅有自定义零点的年龄列，缺少可用的标准年代列"})

        # 每个测量字段最多对应一列。无法确认归属时，保留候选给人工复核。
        for field in set(column_mapping.values()) & UnitConversionModule.default_units.keys():
            columns = [col for col, value in column_mapping.items() if value == field]
            if len(columns) < 2:
                continue
            # 并列岩芯序列：各列声明了岩芯身份（from core X）或作者合成→拆分为独立
            # 序列而非当歧义候选暂缓（Grove2013b STM4/STM2/comp）。
            declared = self.declared_parallel_series(columns, definitions or {})
            if declared:
                if parallel_cores is not None:
                    parallel_cores[field] = declared
                continue
            key, dec = self._structural_decision(file_path, "select_column", field)
            if dec and dec["selected"] in columns:
                for col in columns:
                    if col != dec["selected"]:
                        del column_mapping[col]
                self._record_structural_application(key, file_path, dec, field, record_issue)
                continue
            # 优先原始序列：存在非衍生对照列时，剔除平滑/滤波等冗余衍生列；
            # 若全部为保留绝对值的衍生列（无原始对照），则不剔除，保留为候选交复核，
            # 避免一概丢弃作者平滑序列（如 Sadler2015 仅 4 点滑动平均 Sr/Ca）。
            preferred = [col for col in columns
                         if not self.is_smoothed_derivative(col, (definitions or {}).get(col, {}))]
            raw_columns = [col for col in columns if re.search(r"\braw\b", col.replace("_", " "), re.I)]
            keep = preferred if len(preferred) == 1 else (raw_columns if len(raw_columns) == 1 else [])
            if len(keep) != 1:
                record_issue({"file": file_path, "column": " | ".join(columns),
                                 "decision": field, "action": "暂缓该指标",
                                 "reason": "多个候选列无法确定唯一来源"})
            for col in columns:
                if len(keep) != 1 or col != keep[0]:
                    del column_mapping[col]

        for field in TIME_FIELDS[:-1]:
            columns = [col for col, target in column_mapping.items() if target == field]
            if len(columns) > 1:
                key, dec = self._structural_decision(file_path, "select_time_column", field)
                if dec and dec["selected"] in columns:
                    for col in columns:
                        if col != dec["selected"]:
                            del column_mapping[col]
                    self._record_structural_application(key, file_path, dec, field, record_issue)

        # 过滤水文信号列
        if columns_to_drop:
            df = df.drop(columns=columns_to_drop, errors='ignore')
        
        return df, column_mapping
    
    def _convert_chron_to_ce(self, value: float, bp_base_age_ka: float, reference_year: int) -> Optional[float]:
        """
        【新增】将内部年表值转换为CE年份
        
        Args:
            value: 内部年表值（年）
            bp_base_age_ka: 基点年龄（ka）
            reference_year: 参考年份（如测量年份）
            
        Returns:
            float: CE年份
        """
        try:
            if bp_base_age_ka is None or reference_year is None:
                return None
                
            # 珊瑚BP年龄 = 基点年龄（ka转换为年）
            coral_bp_age_years = bp_base_age_ka * 1000
            
            # 实际年龄（年）= 珊瑚BP年龄（年）- 内部年表值（年）
            # 假设内部年表值是从基点（老）向现代（新）累加
            actual_age_years = coral_bp_age_years - float(value)
            
            # CE年份 = 参考年份 - 实际年龄（BP/Before Ref）
            ce_year = reference_year - actual_age_years
            return ce_year
        except Exception as e:
            logger.debug(f"内部年表转换计算错误: {e}")
            return None

    def extract_standardized_data(self, parsed_result: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        从解析结果中提取标准化数据
        
        Args:
            parsed_result: parse_file的返回结果
            
        Returns:
            List[Dict]: 标准化数据点列表
        """
        df = parsed_result.get("data", pd.DataFrame())
        metadata = parsed_result.get("metadata", {})
        mapping = parsed_result.get("column_mapping", {})
        variables = parsed_result.get("variables", [])
        candidates = {
            field: [col for col, target in mapping.items() if target == field]
            for field in ("year", "age_bp", "age_ka_bp", "age_bce", "internal_chronology")
        }
        year_col = time_field = None
        for field, columns in candidates.items():
            if columns:
                if len(columns) > 1:
                    # 并列岩芯带各自年代轴（age-STM4/age-STM2）：按岩芯身份配对，
                    # 配对成功则不整表暂缓，记录逐岩芯年代轴供提取覆盖。
                    parallel_cores = parsed_result.get("parallel_cores", {})
                    core_age = self._pair_core_age_columns(parallel_cores, columns)
                    core_proxy_cols = {col for cols in parallel_cores.values()
                                       for col, info in cols.items() if info.get("core_id")}
                    if core_proxy_cols and core_proxy_cols <= set(core_age):
                        parsed_result["core_age_map"] = core_age
                        year_col, time_field = columns[0], field
                        break
                    explicit_ce = [col for col in columns if re.search(r"\b(?:AD|CE)\b", col, re.I)]
                    if len(explicit_ce) != 1:
                        add_review_item({"file": parsed_result.get("file_path"), "column": " | ".join(columns),
                                         "decision": field, "action": "暂缓表格", "reason": "存在多个无法区分的主年代列"})
                        return []
                    columns = explicit_ce
                year_col, time_field = columns[0], field
                break
        if df.empty or year_col is None:
            return []

        events = metadata.get("source_events", {})
        if time_field == "internal_chronology" and len(events) > 1:
            add_review_item({"file": parsed_result.get("file_path"), "column": year_col,
                             "action": "暂缓表格", "reason": "多个事件的内部年表缺少逐事件定年锚点"})
            return []

        missing = {str(value).casefold() for value in metadata.get("source_missing_values", [])}
        date_orders = parsed_result.get("date_orders", {})
        bp_epochs = {
            col: bp_reference_year(col, self._variable_for_column(col, variables).get("unit"),
                                   self._variable_for_column(col, variables).get("notes"),
                                   metadata.get("source_time_unit"))
            for basis in ("age_bp", "age_ka_bp") for col in candidates[basis]
        }
        if year_col in bp_epochs and bp_epochs[year_col] is None:
            add_review_item({"file": parsed_result.get("file_path"), "column": year_col,
                             "action": "暂缓时间列", "reason": "BP 参考年份冲突"})
            return []
        time_values = []
        alternate_columns = [(col, basis) for basis in TIME_FIELDS[:-1] if basis != time_field
                             for col in candidates[basis]]
        year_axes = [(year_col, time_field), *alternate_columns]
        year_range = (metadata.get("time_coverage_start"), metadata.get("time_coverage_end"))
        def read_axis(col, basis):
            return partial(source_year_axis, basis=basis, reference_year=bp_epochs.get(col),
                           date_order=date_orders.get(col), year_range=year_range)

        readers = {col: read_axis(col, basis) for col, basis in year_axes}
        # 并列岩芯各自年代轴：为配对的年代列补充 reader（主轴 reader 不含它们）
        for age_col in (parsed_result.get("core_age_map") or {}).values():
            if age_col not in readers:
                readers[age_col] = read_axis(age_col, time_field)
        padded_steps = {
            col: step for col, _ in alternate_columns
            if (step := verified_padded_year_step(df[year_col], df[col], readers[year_col], readers[col])) is not None
        }
        primary_steps = [
            step for col, _ in alternate_columns
            if (step := verified_padded_year_step(df[col], df[year_col], readers[col], readers[year_col])) is not None
        ]
        if primary_steps:
            padded_steps[year_col] = min(primary_steps)
        for row_number, (_, row) in enumerate(df.iterrows(), 1):
            value = row[year_col]
            if str(value).strip().casefold() in missing:
                time_values.append(None)
                continue
            if time_field == "internal_chronology":
                number = self._convert_chron_to_ce(
                    finite_number(value), metadata.get("base_uth_age_ka"), metadata.get("measurement_year")
                )
                time_values.append((number, year_col, time_field) if number is not None else None)
                continue
            axes = []
            for col, _ in year_axes:
                year, step = readers[col](row[col]) if str(row[col]).strip().casefold() not in missing else (None, None)
                axes.append((year, padded_steps.get(col, step)))
            if axes[0][0] is None:
                time_values.append(None)
                continue
            choice = select_year_axis(axes)
            if choice is None:
                add_review_item({"file": parsed_result.get("file_path"), "source_row": row_number,
                                 "source_line": int(row.name), "source_value": value,
                                 "column": year_col, "action": "暂缓该行", "reason": "同一观测的绝对年代列不一致",
                                 "decision": " | ".join(f"{col}={row[col]}" for col, _ in year_axes)})
            time_values.append((axes[choice][0], *year_axes[choice]) if choice is not None else None)
        if all(value is None for value in time_values):
            add_review_item({"file": parsed_result.get("file_path"), "sheet": os.path.basename(parsed_result.get("file_path") or ""),
                             "column": year_col, "action": "暂缓时间列", "reason": "未找到可按源声明转换的绝对年代"})
            return []

        measures = UnitConversionModule.default_units
        selected = {col: field for col, field in mapping.items() if field in measures}
        if not selected:
            return []
        plan = parsed_result.get("column_plan")
        if plan is None:
            plan = self._build_column_plan(mapping, {
                col: self._variable_for_column(col, variables) for col in mapping
            })
        resolution, interval = sampling_profile(value[0] for value in time_values if value is not None)
        event_column = next((col for col in df.columns if col.strip().casefold() in {"event", "event label", "event id"}), None)
        if len(events) > 1:
            unknown = sorted({str(value).strip() for value in df[event_column]} - set(events)) if event_column else []
            if not event_column or unknown:
                add_review_item({"file": parsed_result.get("file_path"), "column": event_column,
                                 "action": "暂缓关联地点", "reason": "多事件表缺少事件列或存在未匹配的事件标签",
                                 "decision": " | ".join(label or "[空]" for label in unknown)})
        core_column = parsed_result.get("core_column")
        core_labels = ({str(value).strip().casefold() for value in df[core_column] if pd.notna(value)} - (missing | {""})
                       if core_column else set())
        core_age_map = parsed_result.get("core_age_map")
        records = []
        for source_row, (time_value, (source_line, row)) in enumerate(zip(time_values, df.iterrows()), start=1):
            if time_value is None:
                # 并列岩芯各自年代轴：主轴缺年代但任一岩芯年代存在则保留，逐岩芯覆盖年份
                if not core_age_map or not any(
                        str(row[age_col]).strip().casefold() not in missing for age_col in core_age_map.values()):
                    continue
                year, time_column, basis = None, year_col, time_field
            else:
                year, time_column, basis = time_value
            record = {key: metadata.get(key) for key in (
                "geo_siteName", "geo_latitude", "geo_longitude", "geo_bounds",
                "geo_rep_latitude", "geo_rep_longitude", "geo_lat_span", "geo_lon_span",
                "geo_rep_status", "geo_location_source", "geo_location_evidence",
                "pub1_doi", "pub1_authors", "dataset_doi", "dataset_investigators",
                "species", "source_record_id",
            )}
            if len(events) > 1 and event_column:
                event = events.get(str(row[event_column]).strip())
                if event:
                    record.update(event)
            if core_column:
                label = str(row[core_column]).strip()
                if label.casefold() in core_labels:
                    record["source_core_id"] = label
                    if len(core_labels) > 1:
                        record.update(sample_id=label, source_record_id=label)
            record.update(
                year=year, source_time_value=row[time_column],
                source_time_unit={"year": "CE", "age_bp": "yr BP", "age_ka_bp": "ka BP",
                                  "age_bce": "BCE", "internal_chronology": "internal chronology"}[basis],
                source_time_column=time_column,
                source_time_reference_year=bp_epochs.get(time_column),
                source_format=parsed_result.get("format"),
                source_file=parsed_result.get("file_path", ""),
                source_row=source_row, source_line=int(source_line), hasResolution_nominal=resolution,
                native_interval_years=interval, mapping_route=parsed_result.get("mapping_route"),
            )
            if date_orders.get(time_column):
                record["source_time_date_order"] = date_orders[time_column]
            record.update(parsed_result.get("paper_evidence", {}))
            records.extend(self._emit_proxy_records(
                record, row, selected, plan, missing, measures, parsed_result.get("parallel_cores"),
                parsed_result.get("core_age_map"), readers))
        return records

    def _emit_proxy_records(self, base, row, selected, plan, missing, measures, parallel_cores,
                            core_age_map=None, readers=None):
        """为一行构建指标记录。并列岩芯列各成独立记录（保留岩芯身份/原列/合成关系），
        其余指标按常规合并为单记录。core_age_map 提供时按岩芯各自年代轴覆盖年份。"""
        parallel_fields = {field: cols for field, cols in (parallel_cores or {}).items()
                           if field in set(selected.values())}
        normal = {col: field for col, field in selected.items() if field not in parallel_fields}
        emitted = []
        for field, cols in parallel_fields.items():
            for col, info in cols.items():
                if str(row[col]).strip().casefold() in missing:
                    continue
                value, spread = measurement_parts(row[col])
                if value is None:
                    continue
                spec = plan[col]
                record = {**base}
                record[field] = row[col] if spread is not None else value
                record[f"{field}_unit_resolution"] = spec["unit_resolution"]
                record["source_columns"] = {field: col}
                if info.get("core_id"):
                    record["source_core_id"] = info["core_id"]
                    record["sample_id"] = info["core_id"]
                    record["source_record_id"] = info["core_id"]
                # 并列岩芯各自年代轴：必须用本岩芯年代列；缺失/不可解析则入复核，
                # 绝不借用其他岩芯年份（避免错误年份通过时间质控静默发表）。
                if core_age_map and col in core_age_map:
                    age_col = core_age_map[col]
                    age_value = row[age_col]
                    core_year = None
                    if str(age_value).strip().casefold() not in missing and readers and age_col in readers:
                        core_year, _step = readers[age_col](age_value)
                    if core_year is None:
                        add_review_item({"file": base.get("source_file"), "sheet": base.get("sheet_name"),
                                         "column": col, "source_row": base.get("source_row"),
                                         "source_value": age_value, "action": "暂缓该行",
                                         "reason": f"并列岩芯 {info.get('core_id') or col} 自身年代列 {age_col} "
                                                   f"缺失或不可解析，不借用其他岩芯年份"})
                        continue
                    record["year"] = core_year
                    record["source_time_value"] = age_value
                    record["source_time_column"] = age_col
                comp = spec.get("composite_evidence")
                if info.get("is_composite") or comp:
                    record["composite_evidence"] = {field: comp or "author-declared composite"}
                    record["record_type"] = "published_composite"
                if spec["testing_method"]:
                    record["testing_method"] = spec["testing_method"]
                processing = "; ".join(filter(None, (spec["source_processing"], info.get("season"))))
                if processing:
                    record["source_processing"] = f"{col}: {processing}"
                emitted.append(record)
        if normal:
            record = {**base}
            methods, processing, composite, source_columns = set(), [], {}, {}
            for col, field in normal.items():
                if str(row[col]).strip().casefold() in missing:
                    continue
                value, spread = measurement_parts(row[col])
                if value is None:
                    continue
                spec = plan[col]
                record[field] = row[col] if spread is not None else value
                record[f"{field}_unit_resolution"] = spec["unit_resolution"]
                source_columns[field] = col
                if spec.get("composite_evidence"):
                    composite[field] = spec["composite_evidence"]
                if spec["testing_method"]:
                    methods.add(spec["testing_method"])
                if spec["source_processing"]:
                    processing.append(f"{col}: {spec['source_processing']}")
            if any(field in record for field in measures):
                record["source_columns"] = source_columns
                if composite:
                    record["composite_evidence"] = composite
                    if composite.keys() == source_columns.keys():
                        record["record_type"] = "published_composite"
                if methods:
                    record["testing_method"] = " | ".join(sorted(methods))
                if processing:
                    record["source_processing"] = " | ".join(processing)
                emitted.append(record)
        return emitted

    def process_directory(self, dir_path: str, recursive: bool = True) -> List[Dict[str, Any]]:
        """
        处理目录中的所有数据库文件
        
        Args:
            dir_path: 目录路径
            recursive: 是否递归处理子目录
            
        Returns:
            List[Dict]: 所有标准化数据
        """
        all_data = []
        supported_extensions = ['.txt', '.tab']
        
        if recursive:
            for root, dirs, files in os.walk(dir_path):
                for file in files:
                    ext = os.path.splitext(file)[1].lower()
                    if ext in supported_extensions:
                        file_path = os.path.join(root, file)
                        try:
                            result = self.parse_file(file_path)
                            data = self.extract_standardized_data(result)
                            all_data.extend(data)
                            logger.info(f"处理完成: {file}, 提取 {len(data)} 条记录")
                        except Exception as e:
                            logger.error(f"处理文件失败: {file}, 错误: {e}")
        else:
            for file in os.listdir(dir_path):
                ext = os.path.splitext(file)[1].lower()
                if ext in supported_extensions:
                    file_path = os.path.join(dir_path, file)
                    try:
                        result = self.parse_file(file_path)
                        data = self.extract_standardized_data(result)
                        all_data.extend(data)
                        logger.info(f"处理完成: {file}, 提取 {len(data)} 条记录")
                    except Exception as e:
                        logger.error(f"处理文件失败: {file}, 错误: {e}")
        
        return all_data
    
    def _parse_generic(self, file_path: str) -> Dict[str, Any]:
        """通用解析（用于未知格式）"""
        logger.warning(f"使用通用解析器处理: {file_path}")
        
        try:
            # 尝试读取为制表符分隔文件
            df = pd.read_csv(file_path, sep='\t', comment='#', encoding='utf-8-sig')
            
            return {
                "format": "GENERIC",
                "file_path": file_path,
                "metadata": {},
                "variables": [],
                "column_mapping": {},
                "data": df,
                "record_count": len(df)
            }
        except Exception as e:
            logger.error(f"通用解析失败: {file_path}, 错误: {e}")
            return {
                "format": "UNKNOWN",
                "file_path": file_path,
                "metadata": {},
                "variables": [],
                "column_mapping": {},
                "data": pd.DataFrame(),
                "record_count": 0
            }


# 便捷函数
def parse_database_file(file_path: str, llm_cache=None) -> Dict[str, Any]:
    """
    解析单个数据库文件
    
    Args:
        file_path: 文件路径
        llm_cache: LLM缓存对象
        
    Returns:
        Dict: 解析结果
    """
    parser = UnifiedDatabaseParser(llm_cache=llm_cache)
    return parser.parse_file(file_path)


def process_database_directory(dir_path: str, recursive: bool = True, llm_cache=None) -> List[Dict[str, Any]]:
    """
    处理目录中的所有数据库文件
    
    Args:
        dir_path: 目录路径
        recursive: 是否递归
        llm_cache: LLM缓存对象
        
    Returns:
        List[Dict]: 所有标准化数据
    """
    parser = UnifiedDatabaseParser(llm_cache=llm_cache)
    return parser.process_directory(dir_path, recursive)


if __name__ == "__main__":
    # 测试代码
    import sys
    
    if len(sys.argv) > 1:
        test_path = sys.argv[1]
        parser = UnifiedDatabaseParser()
        
        if os.path.isfile(test_path):
            result = parser.parse_file(test_path)
            print(f"\n格式: {result['format']}")
            print(f"记录数: {result['record_count']}")
            print(f"\n元数据:")
            for k, v in result['metadata'].items():
                if v:
                    print(f"  {k}: {v}")
            print(f"\n列映射:")
            for k, v in result['column_mapping'].items():
                print(f"  {k} -> {v}")
        else:
            data = parser.process_directory(test_path)
            print(f"\n共提取 {len(data)} 条记录")
    else:
        print("用法: python database_parser.py <文件或目录路径>")

