#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
MOA 主工作流，统一处理目录发现、结构化提取、时空与单位标准化、质控和发布。
"""

import os
import re
import logging
import hashlib
import json
import unicodedata
import pandas as pd
from typing import Dict, List, Any, Optional
from threading import Lock
from functools import partial
from app.core.moa.data_ingestion import DataIngestionModule
from app.core.moa.llm_parser import CORE_PROXY_TOKENS, LLMParsingModule, is_confident_core_proxy
from app.core.moa.database_parser import DatabaseFormat, UnifiedDatabaseParser
from app.core.moa.context_extractor import ContextExtractor, composite_evidence, publication_doi, sample_identity_header
from app.core.moa.spatiotemporal_standard import (SpatiotemporalStandardizationModule, bp_reference_year,
                                                   calendar_date_order, sampling_profile, select_year_axis,
                                                   source_year_axis, time_basis, verified_padded_year_step)
from app.core.moa.unit_converter import (BACA_UNIT_REVIEW, UnitConversionModule,
                                         finite_number, measurement_parts, requires_baca_unit_check)
from app.core.moa.quality_control import QualityControlModule
from app.core.moa.data_output import DataOutputModule
from app.utils.pdf_direct_processor import extract_text_from_pdf
from app.utils.data_trace_logger import get_tracer
from app.utils.calibration_checker import check_sr_ca_calibration
from app.utils.spatio_temporal_utils import convert_coordinate_format, validate_coordinates
from app.utils.parallel_processor import DirectoryParallelProcessor
from app.utils.site_data_mapper import build_site_data_mapping
from app.utils.review_queue import (add_review_item, capture_review_items, clear_review_items,
                                    is_paper_body_pdf, load_unit_review_decisions, mapping_review_key, measurement_review_id,
                                    structural_review_decision)
from app.utils.toolkit import filter_empty_key_fields

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# 珊瑚古气候目标代用指标关键词（相关性预筛用，全部小写；δ/Δ在读入时归一化为d）
# 注意：不放裸"temperature"/"isotope"等宽泛词，避免碳酸化学实验类文件误通过
PROXY_KEYWORDS = [
    "sr/ca", "srca", "strontium/calcium", "ba/ca", "baca", "barium/calcium", "mg/ca", "li/mg", "b/ca", "u/ca",
    "d18o", "d13c", "d15n", "d47",
    "oxygen isotop", "carbon isotop",
    "sea surface temperature", "sst", "paleotemperature", "palaeotemperature",
    "dendrochronolog",
]

def match_data_to_research_site(data_point: Dict[str, Any], column_names: List[str], research_sites: Dict[str, Dict[str, float]]) -> Optional[str]:
    """
    根据数据点和列名匹配最可能的研究地点
    
    Args:
        data_point: 数据点字典
        column_names: 表格列名列表
        research_sites: 研究地点信息字典
        
    Returns:
        匹配的研究地点标识，如果无法匹配则返回None
    """
    # 收集所有可能的站点标识
    possible_site_ids = set(research_sites.keys())
    
    # 1. 检查数据点中的值是否包含站点标识
    for key, value in data_point.items():
        if isinstance(value, str):
            # 直接匹配
            if value in possible_site_ids:
                return value
            # 模糊匹配
            for site_id in possible_site_ids:
                if site_id in value:
                    return site_id
    
    # 2. 检查列名是否包含站点标识
    for col_name in column_names:
        for site_id in possible_site_ids:
            if site_id in col_name:
                return site_id
    
    # 3. 如果没有找到明确的匹配，返回None
    return None

class MOAWorkflow:
    """MOA主工作流"""
    
    def __init__(self, output_dir: str = "output", enable_calibration_check: bool = True,
                 max_workers: int = 5, review_decisions=None):
        # 初始化各个模块
        self.llm_parser = LLMParsingModule(enable_cache=True, cache_dir="cache")
        self.data_ingestion = DataIngestionModule(enable_layout_model=True, layout_cache=self.llm_parser.cache)
        # 统一数据库解析器（支持NOAA和PANGAEA），共享LLM缓存
        self.database_parser = UnifiedDatabaseParser(llm_cache=self.llm_parser.cache if self.llm_parser.enable_cache else None)
        self.spatiotemporal_standard = SpatiotemporalStandardizationModule()
        self.unit_converter = UnitConversionModule()
        self.unit_review_decisions = (dict(review_decisions) if isinstance(review_decisions, dict)
                                      else load_unit_review_decisions(review_decisions))
        self.applied_unit_reviews = set()
        self.database_parser.review_decisions = self.unit_review_decisions
        self.database_parser.applied_structural_reviews = self.applied_unit_reviews
        # 表结构裁决回灌：数据接入层按 confirm_layout 坐标从原表重提取。
        self.data_ingestion.review_decisions = self.unit_review_decisions
        self.data_ingestion.source_digest = self._source_digest
        self.data_ingestion.applied_structural_reviews = self.applied_unit_reviews
        self.deferred_unit_reviews = {}
        self._source_digests = {}
        self._mapping_files = {key[1] for key in self.unit_review_decisions
                               if isinstance(key, tuple) and key[0] == "MAP"}
        for parser in (self.llm_parser, self.database_parser.llm_parser):
            parser.mapping_files = self._mapping_files
            parser.mapping_decisions = self.unit_review_decisions
            parser.mapping_digest = self._source_digest
            parser.applied_mapping_excludes = self.applied_unit_reviews
        self.quality_control = QualityControlModule()
        self.data_output = DataOutputModule(output_dir)
        
        # 【新增】上下文提取器：从MD正文提取元数据
        self.context_extractor = ContextExtractor(cache=self.llm_parser.cache)
        
        # 是否启用校准检查
        self.enable_calibration_check = enable_calibration_check
        
        # 并发处理器
        self.max_workers = max_workers
        self.parallel_processor = DirectoryParallelProcessor(max_workers=max_workers)
        
        self.processed_data = []
        self.quality_reports = []
        self.source_inventory = []
        self._review_items = []
        self.incomplete_review_count = 0
        self.retry_pending_categories = {}
        self._inventory_lock = Lock()
        
        # 初始化追踪器
        self.tracer = get_tracer()
    
    def _is_relevant_proxy_file(self, file_path: str, header_chars: int = 30000) -> bool:
        """相关性预筛：检查文本类数据文件（NOAA txt/PANGAEA tab）头部是否含目标代用指标关键词。
        
        这两类文件的元数据、参数定义和列名都集中在文件头部，无关键词即可判定不含
        珊瑚古气候代用数据，直接跳过以节省LLM调用（如海藻密度/碳酸化学实验类文件）。
        读取失败时返回True（宁可放行交给后续流程，避免静默丢数据）。
        """
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                head = unicodedata.normalize("NFKC", f.read(header_chars)).casefold().replace("δ", "d")
            return any(kw in head for kw in PROXY_KEYWORDS)
        except OSError:
            return True
    
    def process_file(self, file_path: str, context: str = "", paper_hash: str = "",
                     pdf_context: Optional[Dict[str, Any]] = None, native_tables=None,
                     paper_context_loader=None) -> List[Dict[str, Any]]:
        """处理单个输入，同时记录观测和逐表处理结果。"""
        file_path = os.fspath(file_path)
        if is_paper_body_pdf(file_path):
            self._record_input(file_path, status="context", reason="论文正文仅用于核对附件证据")
            return []
        exclude_key = ("FILE_EXCLUDE", os.path.normcase(os.path.abspath(file_path)))
        exclusion = self.unit_review_decisions.get(exclude_key)
        if (exclusion and exclusion["evidence_status"] == "verified"
                and exclusion.get("source_sha256") == self._source_digest(file_path)):
            self.applied_unit_reviews.add(exclude_key)
            self._record_input(file_path, None, 0, 0, (), "excluded", exclusion["note"])
            return []
        tables = []
        with capture_review_items() as reviews:
            points = self._process_file(file_path, context, paper_hash, pdf_context, native_tables, tables,
                                        paper_context_loader)
            if os.path.normcase(os.path.abspath(file_path)) in self._mapping_files:
                self._record_verified_mappings(file_path, points)
            points = filter_empty_key_fields(points)
            if not points and not reviews and not (tables and all(table.get("status") == "excluded" for table in tables)):
                add_review_item({"file": file_path, "action": "复核输入", "reason": "没有可确认的观测，需检查表格结构与字段含义"})
        self._remember_reviews(reviews)
        counts = {}
        source_rows = {}
        for point in points:
            sheet = point.get("sheet_name")
            counts[sheet] = counts.get(sheet, 0) + 1
            source_rows.setdefault(sheet, set()).add(point.get("source_row"))
        for table in tables:
            sheet = table.get("sheet_name")
            self._record_input(file_path, sheet, table.get("row_count"), counts.get(sheet, 0),
                               [item for item in reviews if item.get("sheet") == sheet],
                               table.get("status"), table.get("reason"),
                               covered_rows=len(source_rows.get(sheet, ())))
        reported = {table.get("sheet_name") for table in tables}
        for sheet in sorted({item["sheet"] for item in reviews if item.get("sheet") and item["sheet"] not in reported}):
            self._record_input(file_path, sheet, None, 0, [item for item in reviews if item.get("sheet") == sheet])
        status = reason = None
        if tables and not points and all(table.get("status") == "excluded" for table in tables) \
                and all(item.get("action") == "已排除非观测片段" for item in reviews):
            status, reason = "excluded", "所有表块均被明确排除，详见表块清单"
        self._record_input(file_path, None, sum(table.get("row_count", 0) for table in tables),
                           len(points), reviews, status, reason,
                           covered_rows=sum(len(rows) for rows in source_rows.values()))
        return points

    def _record_verified_mappings(self, file, points):
        digest = self._source_digest(file)
        applied = {}
        for point in points:
            sheet = point.get("sheet_name")
            if not sheet:
                continue
            columns = {field: col for field, col in point.get("source_columns", {}).items() if col}
            if point.get("source_time_column") and finite_number(point.get("year")) is not None:
                columns[time_basis(point.get("source_time_unit"))] = point["source_time_column"]
            for field, column in columns.items():
                key = mapping_review_key(file, sheet, column, digest)
                decision = self.unit_review_decisions.get(key)
                if (decision and decision["evidence_status"] == "verified" and decision["target"] == field
                        and (finite_number(point.get(field)) is not None
                             or (decision["unit"] == "unknown" and field in self.unit_converter.default_units
                                 and measurement_parts(point.get(f"{field}_original_value"),
                                                       column in point.get("decimal_columns", ()))[0] is not None))):
                    applied[key] = (sheet, column, field, decision)
        for key, (sheet, column, field, decision) in applied.items():
            self.applied_unit_reviews.add(key)
            add_review_item({"file": file, "sheet": sheet, "column": column, "decision": field,
                             "action": "已应用证据复核", "review_decision": "confirm_mapping",
                             "reason": decision["note"], "source_sha256": digest})

    def _remember_reviews(self, reviews):
        with self._inventory_lock:
            self._review_items.extend(reviews)

    def _record_input(self, file_path, sheet=None, rows=None, count=0, reviews=(), status=None, reason=None, covered_rows=0):
        if not status:
            if count:
                withheld = any(item.get("status") == "failed" or item.get("action", "").startswith(("暂缓", "撤销"))
                               for item in reviews)
                status = "partial" if withheld or (rows and covered_rows < rows) else "extracted"
            elif any(item.get("status") == "failed" for item in reviews):
                status = "failed"
            else:
                status = "review"
        reasons = dict.fromkeys(item.get("reason", "") for item in reviews)
        with self._inventory_lock:
            self.source_inventory.append({
                "file": file_path, "sheet": sheet, "status": status, "input_rows": rows,
                "source_rows_with_output": covered_rows, "output_rows": count, "review_items": len(reviews),
                "reason": reason or " | ".join(filter(None, reasons)) or (
                    "没有可确认的观测" if not count and status == "review" else ""
                ),
            })

    def _process_file(self, file_path, context, paper_hash, pdf_context, native_tables, tables, paper_context_loader=None):
        """
        处理单个文件
        
        Args:
            file_path (str): 文件路径
            context (str): 上下文信息（如文献标题、摘要等）
            paper_hash (str): 论文哈希值
            pdf_context (Dict[str, Any], optional): 从PDF正文中提取的上下文信息
            
        Returns:
            List[Dict[str, Any]]: 处理后的数据点列表
        """
        try:
            logging.info(f"开始处理文件: {file_path}")
            
            suffix = os.path.splitext(file_path)[1].lower()
            if suffix in {".txt", ".tab"}:
                native_format = self.database_parser.detect_format(file_path)
                if native_format != DatabaseFormat.UNKNOWN:
                    return self._process_database_file(file_path, paper_hash, native_format.value.upper(), tables,
                                                       paper_context_loader)
            tables_data = self.data_ingestion.read_file(file_path, inventory=tables)
            tables.extend(tables_data)
            if not tables_data:
                logging.warning(f"文件读取失败或无数据: {file_path}")
                return []
            
            processed_points = []
            
            # 2. 对每个表格进行处理
            for table_data in tables_data:
                try:
                    if native_tables is not None and self._defer_csv_variant(file_path, table_data, native_tables):
                        continue
                    table_points = []
                    logging.info(f"处理表格: {table_data.get('sheet_name', 'unknown')}")

                    declared_paper = re.search(r"(?mi)^[\s#]*(?:publication[ _]doi|paper[ _]doi|pub1_doi|论文 DOI)\s*[:：]\s*([^\n]+)",
                                               table_data.get("table_description") or "")
                    expected_doi = publication_doi(declared_paper.group(1)) if declared_paper else None
                    table_context = pdf_context if pdf_context is not None else (
                        paper_context_loader(expected_doi) if paper_context_loader else {}
                    )
                    if expected_doi and publication_doi(table_context.get("pub1_doi")) != expected_doi:
                        add_review_item({"file": file_path, "sheet": table_data.get("sheet_name"),
                                         "action": "暂缓关联正文", "decision": expected_doi,
                                         "reason": "附件明确声明的出版物 DOI 未与正文核实"})
                        table_context = {}
                    research_sites = table_context.get("research_sites") or {}
                    pdf_text = table_context.get("_pdf_text", "")

                    table_data["date_orders"] = {
                        column: order for column in table_data.get("headers", [])
                        if re.search(r"\b(?:date|year|age|time)\b|日期", column.replace("_", " "), re.I)
                        if (order := calendar_date_order(
                            row.get(column) for row in table_data.get("data", [])
                            if isinstance(row, dict) and pd.notna(row.get(column)) and str(row[column]).strip()
                        ))
                    }
                    
                    parsing_result = self.database_parser.plan_workbook_columns(table_data)
                    mapping_route = "workbook_definition"
                    if parsing_result is None:
                        mapping_route = "model_assisted"
                        parsing_result = self.llm_parser.parse_table_structure(
                            table_data, context, table_context, known_variables=table_data.get("variables")
                        )
                    else:
                        parsing_result = self.llm_parser.apply_source_units(parsing_result, table_data, table_context)
                    if parsing_result.get("table_metadata", {}).get("is_coral_data") is False:
                        table_data.update(status="excluded", reason="已判定为非珊瑚观测表")
                        add_review_item({"file": file_path, "sheet": table_data.get("sheet_name"),
                                         "action": "已排除非观测片段",
                                         "reason": "模型判定非珊瑚观测表（is_coral_data=false），表块级留痕备查"})
                        continue
                    
                    # 【优化】提前定义公共变量，避免重复调用
                    table_headers = table_data.get("headers", [])
                    mapped_data = parsing_result.get("mapped_data", [])
                    column_map = self._map_csv_columns(table_headers, mapped_data, table_data, table_context)
                    
                    if not any(info["standard_field"] in {"year", "age_bp", "age_ka_bp", "age_bce"}
                               for info in column_map.values()):
                        add_review_item({"file": file_path, "sheet": table_data.get("sheet_name"),
                                         "action": "暂缓表格", "reason": "未找到可确认的绝对年代列"})
                        continue
                    if not any(info["standard_field"] in self.unit_converter.default_units for info in column_map.values()):
                        add_review_item({"file": file_path, "sheet": table_data.get("sheet_name"),
                                         "action": "暂缓表格", "reason": "没有可确认的受支持观测指标"})
                        continue

                    sample_column = next((col for col in table_headers if sample_identity_header(col)), None)
                    if sample_column:
                        values = [str(row.get(sample_column)) for row in table_data.get("data", [])
                                  if pd.notna(row.get(sample_column))]
                        age_column = next((col for col, info in column_map.items()
                                           if info["standard_field"] in {"year", "age_bp", "age_ka_bp", "age_bce"}), None)
                        ages = sorted({value for row in table_data.get("data", [])
                                       if (value := finite_number(row.get(age_column))) is not None})
                        intervals = [b - a for a, b in zip(ages, ages[1:])]
                        age_error_column = any(info["standard_field"] == "paleoData_dating_error"
                                               for info in column_map.values())
                        if (age_error_column and len(values) >= 3 and len(set(values)) == len(values)
                                and intervals and pd.Series(intervals).median() > 1.5):
                            parsing_result.setdefault("table_metadata", {})["resolution"] = "discrete_samples"

                    site_mapping_info = None
                    if len(research_sites) > 1 and pdf_text:
                        first_row = next(iter(table_data.get("data", [])), {})
                        site_mapping_info = build_site_data_mapping(
                            pdf_text=pdf_text, table_headers=table_headers,
                            research_sites=research_sites,
                            sample_data_preview=self._ensure_row_dict(first_row, table_headers),
                        )

                    # ===== 优化的批量站点匹配策略 =====
                    site_assignment_plan = self._prepare_site_assignment_plan(
                        pdf_context=table_context,
                        site_mapping_info=site_mapping_info,
                        table_headers=table_headers
                    )
                    
                    # 记录字段映射过程（只记录一次）
                    self.tracer.log_field_mapping(column_map, mapped_data)
                    
                    # 【优化】检测多样品列：只在循环前检测一次
                    multi_sample_columns = self._detect_multi_sample_columns(column_map)
                    
                    definitions = table_data.get("variable_definitions", {})
                    time_axes = {column: info for column, info in column_map.items()
                                 if info["standard_field"] in {"year", "age_bp", "age_ka_bp", "age_bce"}}
                    if len(time_axes) > 1:
                        self._verify_padded_time_columns(time_axes, table_data)
                    import psutil
                    for row_idx, row_data in enumerate(table_data.get("data", [])):
                        try:
                            normalized_row = self._ensure_row_dict(row_data, table_headers)
                            if normalized_row is None:
                                logging.warning(f"无法解析的行数据格式，跳过第 {row_idx} 行")
                                continue
                            if normalized_row is not row_data:
                                table_data["data"][row_idx] = normalized_row
                            row_data = normalized_row
                            
                            # 检查内存使用（当内存占用超过80%时，短暂休眠释放资源）
                            if psutil.virtual_memory().percent > 80:
                                import time
                                time.sleep(0.1)  # 休眠100ms，避免内存溢出
                            data_point = {
                                "paper_hash": paper_hash,
                                "source_file": table_data.get("file_path", file_path),
                                "source_attachment": file_path,
                                "sheet_name": table_data.get("sheet_name", "unknown"),
                                "source_row": row_idx + 1,
                                "source_line": table_data["source_rows"][row_idx] if "source_rows" in table_data else None,
                                "source_sheet_name": table_data.get("source_sheet_name"),
                                "source_page": table_data.get("source_page"),
                                "source_table_bbox": table_data.get("source_table_bbox"),
                                "source_header_line": table_data.get("source_header_line"),
                                "source_header_rows": table_data.get("source_header_rows"),
                                "source_column_positions": table_data.get("source_column_positions"),
                                "layout_route": table_data.get("layout_route"),
                                "source_type": "attachment",
                                "mapping_route": mapping_route,
                                "sample_id": row_data.get(sample_column) if sample_column else table_data.get("sample_id"),
                                "source_columns": {},
                                "decimal_columns": table_data.get("decimal_columns", ()),
                                "time_axes": time_axes,
                            }
                            if sample_column and re.match(r"core\b", sample_column, re.I):
                                data_point["source_record_id"] = data_point["sample_id"]
                            elif table_data.get("sample_id"):
                                data_point["source_record_id"] = table_data["sample_id"]
                            if multi_sample_columns:
                                points = self._process_multi_sample_row(
                                    row_data, column_map, data_point
                                )
                            else:
                                for column, info in column_map.items():
                                    field = info["standard_field"]
                                    data_point[field] = row_data.get(column)
                                    data_point["source_columns"][field] = column
                                    data_point[f"{field}_source_unit"] = info.get("unit")
                                    if info.get("unit_resolution") is not None:
                                        data_point[f"{field}_unit_resolution"] = info["unit_resolution"]
                                points = [data_point]

                            for point in points:
                                composite = {field: column_map[column]["composite_evidence"]
                                             for field, column in point["source_columns"].items()
                                             if column_map[column].get("composite_evidence")}
                                if composite:
                                    point["composite_evidence"] = composite
                                    if all(field in composite for field in point["source_columns"]
                                           if field in self.unit_converter.default_units):
                                        point["record_type"] = "published_composite"
                                        point.setdefault("source_record_id", " | ".join(point["source_columns"][field] for field in composite))
                                if definitions:
                                    specs = [(column, definitions.get(column, {}))
                                             for field, column in point["source_columns"].items()
                                             if field in self.unit_converter.default_units]
                                    methods = sorted({spec["method"] for _, spec in specs if spec.get("method")})
                                    processing = [(column, ", ".join(spec[key] for key in ("processing", "seasonality")
                                                                      if spec.get(key))) for column, spec in specs]
                                    if methods:
                                        point["testing_method"] = " | ".join(methods)
                                    point["source_processing"] = " | ".join(f"{column}: {detail}" for column, detail in processing if detail)
                                checked = self._process_single_data_point(
                                    point, parsing_result, table_context, site_assignment_plan, row_data
                                )
                                if checked:
                                    table_points.append(checked)
                        except Exception as row_error:
                            add_review_item({"file": file_path, "sheet": table_data.get("sheet_name"),
                                             "source_row": row_idx + 1, "status": "failed",
                                             "action": "暂缓该行", "reason": str(row_error)})
                            logging.exception("处理行数据时出错")
                            # 继续处理下一行，而不是中断整个过程
                            continue
                        
                        # 移除原有的行数限制代码，处理所有数据
                        # if row_idx >= 1000:
                        #     logging.info(f"为避免程序卡死，仅处理前1000条数据")
                        #     break
                    self._apply_sampling_profiles(table_points)
                    self._flag_series_outliers(table_points)
                    pending_reference = {point["source_columns"]["Sr/Ca_value"] for point in table_points
                                         if point.get("sr_ca_reference_status") == "unconfirmed"}
                    for column in sorted(pending_reference):
                        add_review_item({"file": file_path, "sheet": table_data.get("sheet_name"), "column": column,
                                         "decision": "Sr/Ca_value", "action": "复核参考材料",
                                         "reason": "正文未确认参考材料，保留观测并单列参考材料状态"})
                    processed_points.extend(table_points)
                    self.quality_reports.extend({
                        "paper_hash": paper_hash,
                        "data_point": point.copy(),
                        "qc_flag": point["qc_flag"],
                        "qc_log": point["qc_log"],
                    } for point in table_points)

                except Exception as table_error:
                    add_review_item({"file": file_path, "sheet": table_data.get("sheet_name"), "status": "failed",
                                     "action": "暂缓表格", "reason": str(table_error)})
                    # 【增强】记录详细的错误信息和堆栈跟踪
                    import traceback
                    logging.error(f"处理表格时出错 {table_data.get('sheet_name', 'unknown')}: {str(table_error)}")
                    logging.error(f"错误类型: {type(table_error).__name__}")
                    logging.debug(f"堆栈跟踪:\n{traceback.format_exc()}")
                    # 继续处理下一个表格，而不是中断整个过程
                    continue
            
            logging.info(f"文件处理完成: {file_path}，共处理 {len(processed_points)} 条数据")
            return processed_points
        except Exception as e:
            add_review_item({"file": file_path, "status": "failed", "action": "暂缓读取", "reason": str(e)})
            logging.exception("处理文件时出错 %s", file_path)
            return []

    @staticmethod
    def _primary_pdf(directory, files):
        if len(files) == 1:
            return os.path.join(directory, files[0])
        if files:
            label = os.path.basename(os.path.normpath(directory)).casefold()
            matches = [name for name in files if os.path.splitext(name)[0].casefold() == label]
            if len(matches) == 1:
                return os.path.join(directory, matches[0])
        return None

    def _defer_csv_variant(self, file_path, table, native_tables):
        """排除已确认的副本，暂缓疑似转换版本的差异表。"""
        stem, extension = os.path.splitext(os.path.basename(file_path))
        extension = extension.lower()
        raw_formats = self.data_ingestion.WORKBOOK_EXTENSIONS | {".docx"}
        if extension not in raw_formats and extension != ".csv":
            return False
        candidate = stem.casefold()
        if extension == ".csv" and candidate not in native_tables:
            word_key = re.match(r"^(.+_table_\d+)_", candidate)
            if word_key:
                candidate = word_key.group(1)
            if candidate not in native_tables:
                return False

        headers = table["headers"]
        payload = [
            headers, table.get("units", {}),
            [[None if pd.isna(row.get(column)) else str(row[column]).strip() for column in headers]
             for row in table["data"]],
        ]
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        if extension in raw_formats:
            sheet = table.get("source_sheet_name", table["sheet_name"])
            if extension == ".docx":
                # 原工具按从零开始的表号命名 Word 转换副本。
                number = int(sheet.removeprefix("table_")) - 1
                native_tables.setdefault(f"{stem}_table_{number}".casefold(), set()).add(digest)
            else:
                safe_sheet = re.sub(r'[\\/*?:"<>|]', "_", sheet)
                native_tables.setdefault(f"{stem}_{safe_sheet}".casefold(), set()).add(digest)
            return False
        if digest in native_tables[candidate]:
            logging.info("跳过与原始表内容一致的 CSV: %s", file_path)
            table.update(status="excluded", reason="与原始工作表内容一致的 CSV 副本")
            return True
        add_review_item({"file": file_path, "sheet": table["sheet_name"], "action": "暂缓差异表",
                         "reason": "CSV 名称对应原始工作表，但单元格内容不同，需确认版本关系"})
        return True

    def _detect_multi_sample_columns(self, column_map):
        measures = {}
        identities = set()
        composite_states = set()
        for column, info in column_map.items():
            field = info["standard_field"]
            if field in self.unit_converter.default_units:
                measures.setdefault(field, []).append(column)
                composite_states.add(bool(info.get("composite_evidence")))
                sample = info.get("sample_id")
                if sample:
                    identities.add(sample.casefold())
        return (len(identities) > 1 or len(composite_states) > 1 or any(len(columns) > 1 for columns in measures.values())
                or any(info.get("sample_id") and info["standard_field"] in {"year", "age_bp", "age_ka_bp", "age_bce"}
                       for info in column_map.values()))

    def _process_multi_sample_row(self, row_data, column_map, base_data_point):
        """只将带有同一样品标识的代理指标写入同一行。"""
        by_sample = {}
        common = {}
        for column, info in column_map.items():
            field = info["standard_field"]
            if field not in self.unit_converter.default_units:
                common[column] = info
                continue
            sample = info.get("sample_id") or self._extract_sample_id_from_column(column, field, "", "", allow_prefix=True)
            composite = bool(info.get("composite_evidence"))
            if not sample and not composite:
                if pd.notna(row_data.get(column)):
                    add_review_item({"file": base_data_point["source_file"],
                                     "sheet": base_data_point["sheet_name"],
                                     "source_row": base_data_point["source_row"], "column": column,
                                     "source_line": base_data_point.get("source_line"), "decision": field,
                                     "source_value": row_data[column], "action": "暂缓该值",
                                     "reason": "并列观测列缺少可辨认的样品标识"})
                continue
            by_sample.setdefault((sample, column if composite else ""), {}).setdefault(field, []).append(column)

        points = []
        axes = base_data_point.get("time_axes", {})
        shared_axes = {column: axis for column, axis in axes.items() if not axis.get("sample_id")}
        for (sample, composite_column), fields in by_sample.items():
            local_axes = {column: axis for column, axis in axes.items() if axis.get("sample_id") == sample}
            point = {**base_data_point, "sample_id": None if composite_column else sample,
                     "source_record_id": composite_column or sample, "source_columns": {},
                     "time_axes": local_axes or shared_axes}
            selected = {column: info for column, info in common.items() if info.get("sample_id") in {None, "", sample}}
            for field, columns in fields.items():
                if len(columns) == 1:
                    selected[columns[0]] = column_map[columns[0]]
                else:
                    add_review_item({"file": point["source_file"], "source_row": point["source_row"],
                                     "sample_id": sample, "column": " | ".join(columns),
                                     "decision": field, "action": "暂缓该指标",
                                     "reason": "同一样品有多个候选列"})
            for column, info in selected.items():
                field = info["standard_field"]
                point[field] = row_data.get(column)
                point["source_columns"][field] = column
                point[f"{field}_source_unit"] = info.get("unit")
                if info.get("unit_resolution") is not None:
                    point[f"{field}_unit_resolution"] = info["unit_resolution"]
            points.append(point)
        return points

    def _extract_sample_id_from_column(self, column_name, field, table_label, paper_text, *, allow_prefix=False):
        """识别带指标的样品表头，以及单指标表中仅写岩芯编号的表头。"""
        match = re.search(r"(?:Sr|Ba)\s*/\s*Ca|δ[¹1][⁸8]O|δ[¹1][³3]C|d18O|d13C|\bSST\b", column_name, re.I)
        if not match:
            name = column_name.strip()
            if (not is_confident_core_proxy(table_label, field)
                    or sum(bool(token.search(table_label)) for token in CORE_PROXY_TOKENS.values()) != 1
                    or not is_confident_core_proxy(f"{name} {table_label}", field)):
                return ""
            if re.fullmatch(r"(?=[A-Za-z0-9._-]*[A-Za-z])(?=[A-Za-z0-9._-]*\d)[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*", name):
                return name
            if re.fullmatch(r"[A-Za-z]+(?:[-_.][A-Za-z]+)+", name) and re.search(
                    rf"(?<![\w.-]){re.escape(name)}(?![\w.-])", paper_text, re.I):
                return name
            return ""
        prefix = column_name[:match.start()].strip(" _-:()[]")
        tagged = self.data_ingestion.SAMPLE_LABEL.fullmatch(prefix)
        if tagged:
            return tagged.group(1)
        if re.fullmatch(r"[A-Za-z]+\d[\w.-]*|[A-Z]", prefix):
            return prefix
        suffix = re.match(r"\s*[_-]?\s*[\[(]([A-Za-z]+\d[\w.-]*)[\])]", column_name[match.end():])
        if suffix:
            return suffix.group(1)
        return prefix if allow_prefix else ""

    def _process_single_data_point(self, data_point: Dict[str, Any], parsing_result: Dict[str, Any],
                                    pdf_context: Optional[Dict[str, Any]], site_assignment_plan: Dict[str, Any],
                                    row_data: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """在单点和并列样品路径中使用相同的时间、单位和质控规则。"""
        context = pdf_context or {}
        metadata = parsing_result.get("complemented_metadata", {})
        axes = data_point.pop("time_axes", {})
        candidates = []
        for column, axis in axes.items():
            value = (row_data or {}).get(column)
            if pd.isna(value) or not str(value).strip():
                continue
            basis = axis["standard_field"]
            year, precision = source_year_axis(value, basis, axis.get("reference_year"),
                                               column in data_point.get("decimal_columns", ()), axis.get("date_order"))
            candidates.append((basis, column, value, axis, year, axis.get("padded_precision", precision)))
        candidates.sort(key=lambda item: ("year", "age_bp", "age_ka_bp", "age_bce").index(item[0]))
        time_field = None
        data_point["year"] = None
        if candidates:
            selected = select_year_axis([(item[4], item[5]) for item in candidates])
            time_field, column, value, axis, year, _ = candidates[selected if selected is not None else 0]
            data_point["source_time_value"] = value
            data_point["source_time_column"] = column
            data_point["source_time_unit"] = axis["unit"]
            data_point["source_time_reference_year"] = axis.get("reference_year")
            if axis.get("date_order"):
                data_point["source_time_date_order"] = axis["date_order"]
            if selected is None and any(item[4] is not None for item in candidates):
                self._review_measurement(data_point, time_field, "同一观测的绝对年代列不一致: " +
                                         " | ".join(f"{name}={raw}" for _, name, raw, _, _, _ in candidates))
                return None
            data_point["year"] = year

        for field in ("pub1_doi", "pub1_authors", "paleoData_dating_method",
                      "paleoData_dating_error", "testing_method"):
            if pd.isna(data_point.get(field)) or data_point.get(field) == "":
                data_point[field] = context.get(field) or metadata.get(field)

        if measurement_parts(data_point.get("Sr/Ca_value"),
                             data_point.get("source_columns", {}).get("Sr/Ca_value") in data_point.get("decimal_columns", ()))[0] is not None:
            reference = context.get("sr_ca_calibration_info", {})
            if reference.get("reference_reported"):
                data_point["sr_ca_reference_status"] = (
                    "correction_reported" if reference["is_calibrated"] else "reference_reported"
                )
                data_point["sr_ca_reference_evidence"] = reference.get("related_text")
            elif reference.get("reference_unconfirmed"):
                data_point["sr_ca_reference_status"] = "unconfirmed"

        data_point = self.spatiotemporal_standard.standardize_time_dimension(
            data_point, parsing_result.get("table_metadata", {})
        )
        if finite_number(data_point.get("year")) is None:
            self._review_measurement(data_point, time_field or "year", "该行缺少可解析的绝对年代")
        if data_point.get("hasResolution_nominal") == "discrete_samples" and not data_point.get("record_type"):
            data_point["record_type"] = "discrete_sample"

        if context.get("metadata_source"):
            data_point["metadata_source"] = context["metadata_source"]
            data_point["metadata_evidence"] = context.get("metadata_evidence", {})
        spatial_metadata = {**context, **metadata}
        if site_assignment_plan.get("multiple_sites"):
            for key in ("geo_siteName", "geo_latitude", "geo_longitude"):
                spatial_metadata.pop(key, None)
        data_point = self.spatiotemporal_standard.standardize_spatial_dimension(data_point, spatial_metadata)
        if row_data is not None:
            self._apply_site_assignment(data_point, row_data, site_assignment_plan, context)
        return self._finalize_measurements(data_point)

    @staticmethod
    def _verify_padded_time_columns(axes, table):
        columns = {col: [row.get(col) for row in table["data"] if isinstance(row, dict)] for col in axes}
        readers = {
            col: partial(source_year_axis, basis=axis["standard_field"], reference_year=axis["reference_year"],
                         decimal_comma=col in table.get("decimal_columns", ()), date_order=axis["date_order"])
            for col, axis in axes.items()
        }
        for col, axis in axes.items():
            steps = [
                step for peer, other in axes.items()
                if axis["standard_field"] != other["standard_field"] and axis.get("sample_id") == other.get("sample_id")
                and (step := verified_padded_year_step(columns[peer], columns[col], readers[peer], readers[col]))
                is not None
            ]
            if steps:
                axis["padded_precision"] = min(steps)

    def _source_digest(self, source):
        source = os.path.abspath(source)
        if source not in self._source_digests:
            with open(source, "rb") as file:
                self._source_digests[source] = hashlib.file_digest(file, "sha256").hexdigest()
        return self._source_digests[source]

    def _unit_review_id(self, point, field, legacy_archive=False):
        digests = []
        for key in ("source_file", "source_attachment"):
            source = point.get(key)
            if source:
                digests.append(self._source_digest(source))
        if legacy_archive:
            point = {**point, "sheet_name": None}
        return measurement_review_id(point, field, digests)

    def _review_measurement(self, point, field, reason, unit_issue=False):
        resolution = point.get(f"{field}_unit_resolution", {})
        row = {
            "file": point.get("source_file"), "sheet": point.get("sheet_name"),
            "source_row": point.get("source_row"), "source_line": point.get("source_line"),
            "sample_id": point.get("sample_id"),
            "source_record_id": point.get("source_record_id"),
            "column": point.get("source_columns", {}).get(field), "decision": field,
            "source_value": point.get(f"{field}_original_value", point.get(field)),
            "source_unit": point.get(f"{field}_original_unit", point.get(f"{field}_source_unit")),
            "action": "暂缓该值", "reason": reason,
        }
        if field in {"year", "age_bp", "age_ka_bp", "age_bce"}:
            row["source_value"] = point.get("source_time_value")
            row["source_unit"] = point.get("source_time_unit")
        if unit_issue:
            row["publication_doi"] = point.get("pub1_doi")
            row["review_id"] = self._unit_review_id(point, field)
            if point.get("source_file"):
                row["source_sha256"] = self._source_digest(point["source_file"])
            row["unit_evidence"] = json.dumps(resolution.get("evidence", []), ensure_ascii=False)
        add_review_item(row)

    def _finalize_measurements(self, point):
        if self.unit_review_decisions:
            for field in self.unit_converter.default_units:
                column = point.get("source_columns", {}).get(field)
                if measurement_parts(point.get(field), column in point.get("decimal_columns", ()))[0] is None:
                    continue
                review_id = self._unit_review_id(point, field)
                decision = self.unit_review_decisions.get(review_id)
                if not decision and point.get("source_type") in {"NOAA", "PANGAEA"}:
                    legacy_id = self._unit_review_id(point, field, legacy_archive=True)
                    decision = self.unit_review_decisions.get(legacy_id)
                    if decision:
                        review_id = legacy_id
                if not decision and column:
                    file = point["source_file"]
                    base = ("COLUMN", os.path.normcase(os.path.abspath(file)), str(column).strip().casefold())
                    sheet = str(point.get("sheet_name") or point.get("source_sheet_name") or "").strip().casefold()
                    review_id = (*base, sheet, self._source_digest(file))
                    decision = self.unit_review_decisions.get(review_id)
                    if not decision and base in self.unit_review_decisions:
                        self.deferred_unit_reviews[base] = "列级裁决缺少工作表和源文件校验值"
                if not decision:
                    continue
                if decision["evidence_status"] == "provisional":
                    self.deferred_unit_reviews[review_id] = "现有说明仅作线索，仍需核对源文件或正文中的单位声明"
                    continue
                excluded = decision["action"] == "exclude"
                unit = decision["unit"]
                if not excluded and self.unit_converter.convert_value(field, 1, unit) is None:
                    self._review_measurement(point, field, "人工确认的单位不支持该字段", unit_issue=True)
                    point[f"{field}_unit_resolution"] = {
                        "unit": None, "status": "unit_conflict",
                        "evidence": point.get(f"{field}_unit_resolution", {}).get("evidence", []),
                    }
                    continue
                prior = point.get(f"{field}_unit_resolution", {})
                origin = decision.get("origin")
                evidence_source = {
                    "paper_model": "paper_model_review", "paper_source": "paper_source_review",
                    "source_rule": "source_rule_review", "evidence_discard": "evidence_discard_review",
                }.get(origin, "manual_review")
                point[f"{field}_unit_resolution"] = {
                    "unit": None if excluded else unit,
                    "original_unit": (decision.get("original_unit") or
                                      prior.get("original_unit", prior.get("unit") or (None if origin else unit))),
                    "status": "manual_excluded" if excluded else "resolved",
                    "evidence": [*prior.get("evidence", []), {"source": evidence_source, "review_id": review_id, **decision}],
                }
                self.applied_unit_reviews.add(review_id)
                location = {"review_id": review_id}
                if isinstance(review_id, tuple):
                    location = {"column": column, "source_sha256": review_id[4]}
                add_review_item({
                    **location, "file": point.get("source_file"),
                    "sheet": point.get("sheet_name") or point.get("source_sheet_name"),
                    "source_row": point.get("source_row"), "decision": field,
                    "action": "已应用证据复核" if origin else "已应用人工裁决", "reason": decision["note"],
                    "review_decision": decision["action"], "confirmed_unit": unit,
                    "review_note": decision["note"], "evidence_status": decision["evidence_status"],
                })

        field = "Ba/Ca_value"
        resolution = point.get(f"{field}_unit_resolution") or {}
        direct = resolution.get("unit") or point.get(f"{field}_source_unit")
        requires_check = requires_baca_unit_check(field, direct) and not any(
            item.get("source") in {"manual_review", "paper_model_review", "paper_source_review"}
            and item.get("evidence_status") == "verified" for item in resolution.get("evidence", ())
        )
        if requires_check and resolution.get("status") not in {"manual_excluded", "unit_conflict"}:
            point[f"{field}_unit_resolution"] = {
                **resolution, "unit": None, "status": "unit_conflict",
                "original_unit": resolution.get("original_unit") or direct,
            }
        point = self.unit_converter.convert_units(point, {})
        reasons = {
            "unsupported_unit": "没有已验证的单位或标尺转换规则",
            "unknown_unit": "源单位或同位素参考标尺缺失",
            "unit_conflict": "不同来源的单位声明冲突，或正文证据不支持模型填写的单位",
        }
        for field in self.unit_converter.default_units:
            status = point.get(f"{field}_unit_status")
            if status in reasons:
                reason = BACA_UNIT_REVIEW if field == "Ba/Ca_value" and requires_check else reasons[status]
                self._review_measurement(point, field, reason, unit_issue=True)
            elif status == "missing_or_invalid_value":
                raw = str(point.get(f"{field}_original_value")).strip().casefold()
                if raw not in {"", "-", "--", "none", "na", "n/a", "nan", "null", "#n/a", "<na>"}:
                    self._review_measurement(point, field, "源单元格不是可无歧义转换的数值")
        return self.quality_control.check_data_quality(point)

    def _apply_sampling_profiles(self, points):
        series = {}
        for point in points:
            if point.get("record_type") != "discrete_sample":
                series.setdefault(point.get("source_record_id"), []).append(point)
        for group in series.values():
            resolution, interval = sampling_profile(point.get("year") for point in group)
            for point in group:
                point["hasResolution_nominal"] = resolution
                point["native_interval_years"] = interval

    def _flag_series_outliers(self, points):
        series = {}
        for point in points:
            if point.get("hasResolution_nominal") in {"discrete_samples", "unknown", None}:
                continue
            sample = point.get("sample_id")
            if pd.isna(sample):
                sample = point.get("source_record_id")
            series.setdefault(sample, []).append(point)
        for group in series.values():
            for point in self.llm_parser.detect_outliers_in_table(group, {}):
                if point.get("qc_outlier_flag") == 1:
                    for field in point.get("qc_outlier_fields", []):
                        if point.get(f"{field}_qc_status") == "pass":
                            point[f"{field}_qc_status"] = "outlier_flagged"
                    reason = point["qc_outlier_reason"]
                    point["qc_log"] = (point["qc_log"] + "；" + reason
                                       if point["qc_flag"] else reason)
                    point["qc_flag"] = max(point["qc_flag"], 2)
                    point["qc_description"] = self.quality_control.qc_flags[point["qc_flag"]]

    def _map_csv_columns(self, df_columns, mapped_data, table_data=None, pdf_context=None):
        """
        建立CSV列名与标准化字段的映射（容错匹配）
        
        【重要】对于被标记为skip的字段，不要加入映射，以避免处理非主时间列
        """
        column_map = {}
        source = table_data or {}
        table_label = str(source.get("source_sheet_name") or source.get("table_caption") or source.get("sheet_name") or "")
        paper_text = (pdf_context or {}).get("_pdf_text") or ""
        for item in mapped_data:
            original_header = item.get("original_header", "")
            standard_field = item.get("standard_field")
            unit = item.get("unit", "未明确")
            
            # 确保standard_field不为None
            if standard_field is None:
                continue
            
            # 【关键修复】跳过被标记为skip的字段（这些通常是非主时间列或无效字段）
            if standard_field == "skip":
                logging.debug(f"跳过列: {original_header} (被标记为skip)")
                continue
                
            # 增强列名匹配的容错性，处理希腊字母变体
            matched_column = None
            for col in df_columns:
                # 直接匹配
                if col == original_header:
                    matched_column = col
                    break
                # 模糊匹配（忽略大小写和空格）
                elif col.strip().lower() == original_header.strip().lower():
                    matched_column = col
                    break
                # 处理希腊字母变体（如δ和Δ）
                elif original_header.replace('δ', 'delta').replace('Δ', 'delta') == col.replace('δ', 'delta').replace('Δ', 'delta'):
                    matched_column = col
                    break
            
            if matched_column:
                reference_year = None
                composite = ""
                date_order = (table_data or {}).get("date_orders", {}).get(matched_column)
                if standard_field in self.unit_converter.default_units:
                    variable = (table_data or {}).get("variable_definitions", {}).get(matched_column, {})
                    if (self.database_parser.is_non_observation(matched_column, variable)
                            and not self.database_parser._smoothed_only_observation(matched_column, variable)):
                        continue
                    composite = composite_evidence(matched_column, " ".join(str(variable.get(key) or "")
                                                   for key in ("method", "processing", "notes")),
                                                   paper_text, source.get("table_caption") or "")
                    if standard_field == "sea_surface_temperature_value" and not self.database_parser.is_sst(matched_column, variable):
                        add_review_item({"file": (table_data or {}).get("file_path"), "sheet": (table_data or {}).get("sheet_name"),
                                         "column": matched_column, "action": "暂缓该指标", "reason": "没有证据确认该温度为海表温度"})
                        continue
                if standard_field in {"year", "age_bp", "age_ka_bp", "age_bce"}:
                    context = pdf_context or {}
                    source = table_data or {}
                    if re.search(r"\b(?:min(?:imum)?|max(?:imum)?|error|age e|limit|relative|internal|elapsed)\b",
                                 matched_column.replace("_", " "), re.I):
                        continue
                    unit_rows = {col.strip(): value for col, value in source.get("units", {}).items()}
                    header_basis = time_basis(matched_column)
                    row_basis = time_basis(unit_rows.get(matched_column.strip()))
                    definition_basis = time_basis(source.get("defined_units", {}).get(matched_column))
                    quote = context.get("metadata_evidence", {}).get("age_unit")
                    body_basis = time_basis(quote)
                    if body_basis != time_basis(context.get("default_age_unit")):
                        body_basis = None
                    direct = {basis for basis in (header_basis, row_basis, definition_basis) if basis}
                    basis = next(iter(direct), body_basis) if len(direct) <= 1 else None
                    if not direct and not body_basis and date_order:
                        basis = "year"
                    if not basis:
                        add_review_item({"file": source.get("file_path"), "sheet": source.get("sheet_name"),
                                         "column": matched_column, "action": "暂缓时间列",
                                         "reason": "源年代基准未明确或相互冲突"})
                        continue
                    standard_field = basis
                    unit = {"year": "CE", "age_bp": "yr BP", "age_ka_bp": "ka BP", "age_bce": "BCE"}[basis]
                    if basis in {"age_bp", "age_ka_bp"}:
                        reference_year = bp_reference_year(matched_column, unit_rows.get(matched_column.strip()),
                                                           source.get("defined_units", {}).get(matched_column),
                                                           quote if body_basis == basis else None)
                        if reference_year is None:
                            add_review_item({"file": source.get("file_path"), "sheet": source.get("sheet_name"),
                                             "column": matched_column, "action": "暂缓时间列", "reason": "BP 参考年份冲突"})
                            continue
                column_map[matched_column] = {
                    "standard_field": standard_field,
                    "unit": unit,
                    "description": item.get("description", ""),
                    "unit_resolution": item.get("unit_resolution"),
                    "reference_year": reference_year,
                    "date_order": date_order,
                    "composite_evidence": composite,
                    "sample_id": source.get("sample_by_column", {}).get(matched_column)
                    or self._extract_sample_id_from_column(matched_column, standard_field, table_label, paper_text),
                }

        source = table_data or {}
        file, sheet = source.get("file_path"), source.get("sheet_name")
        time_fields = ("year", "age_bp", "age_ka_bp", "age_bce")
        present = {item["standard_field"] for item in column_map.values()}
        primary = next((field for field in time_fields if field in present), None)
        for field in (*time_fields, *self.unit_converter.default_units):
            candidates = [column for column, item in column_map.items() if item["standard_field"] == field]
            if len(candidates) > 1:
                time_field = field not in self.unit_converter.default_units
                if time_field:
                    counts = {}
                    for column in candidates:
                        scope = column_map[column].get("sample_id") or ""
                        counts.setdefault(scope, []).append(column)
                    if all(len(columns) == 1 for columns in counts.values()):
                        continue
                    if len(counts) > 1:
                        add_review_item({"file": file, "sheet": sheet, "decision": field,
                                         "column": " | ".join(candidates), "action": "暂缓表格",
                                         "reason": "同一岩芯或共享年代存在多个无法区分的主年代列"})
                        return {}
                action = "select_time_column" if time_field else "select_column"
                key, dec = structural_review_decision(self.unit_review_decisions, file, sheet, action,
                                                      field, self._source_digest)
                if dec and dec["selected"] in candidates:
                    for column in candidates:
                        if column != dec["selected"]:
                            del column_map[column]
                    self.applied_unit_reviews.add(key)
                    add_review_item({"file": file, "sheet": sheet,
                                     "column": dec["selected"], "action": "已应用人工裁决",
                                     "decision": field, "review_decision": action, "reason": dec["note"],
                                     "source_sha256": dec["source_sha256"]})
                elif field == primary:
                    add_review_item({"file": file, "sheet": sheet, "decision": field,
                                     "column": " | ".join(candidates), "action": "暂缓表格",
                                     "reason": "存在多个无法区分的主年代列"})
                    return {}
        
        # 【日志】输出CSV列名到标准字段的映射结果（简洁版）
        if column_map:
            logging.debug(f"【字段映射】成功匹配 {len(column_map)} 个字段")
            for col, map_info in column_map.items():
                logging.debug(f"  '{col}' -> {map_info['standard_field']} ({map_info['unit']})")
        
        return column_map
    
    def _prepare_site_assignment_plan(
        self,
        pdf_context: Optional[Dict[str, Any]],
        site_mapping_info: Optional[Dict[str, Any]],
        table_headers: Optional[List[str]]
    ) -> Dict[str, Any]:
        """根据PDF上下文和映射信息构建批量站点匹配方案"""
        plan: Dict[str, Any] = {
            "mode": "",
            "single_site": None,
            "column_sites": {},
            "sample_sites": {},
            "default_site": None,
            "site_ids": [],
            "research_sites": {},
            "multiple_sites": False,
            "pdf_site_name": pdf_context.get("geo_siteName") if pdf_context else "",
        }
        
        if not pdf_context:
            return plan
        
        raw_research_sites = pdf_context.get("research_sites") if isinstance(pdf_context.get("research_sites"), dict) else {}
        plan["multiple_sites"] = len(raw_research_sites) > 1
        normalized_sites = {}
        for site_id, info in raw_research_sites.items():
            standardized = self._standardize_site_info(site_id, info, plan["pdf_site_name"])
            if standardized:
                normalized_sites[site_id] = standardized
        plan["research_sites"] = normalized_sites
        plan["site_ids"] = list(normalized_sites.keys())
        
        # 默认站点信息（主研究地点）
        default_site_candidate = {
            "latitude": pdf_context.get("geo_latitude"),
            "longitude": pdf_context.get("geo_longitude"),
            "label": pdf_context.get("geo_siteName")
        }
        plan["default_site"] = self._standardize_site_info("default_site", default_site_candidate, plan["pdf_site_name"])
        
        # 单地点场景
        if len(normalized_sites) == 1 and not plan["multiple_sites"]:
            plan["single_site"] = next(iter(normalized_sites.values()))
            plan["mode"] = "single_site"
            return plan
        
        column_sites: Dict[str, Dict[str, Any]] = {}
        sample_sites: Dict[str, Dict[str, Any]] = {}
        
        if site_mapping_info:
            for column_name, site_id in site_mapping_info.get("column_site_mapping", {}).items():
                site_info = normalized_sites.get(site_id)
                if site_info:
                    column_sites[column_name] = site_info
            
            for sample_id, site_id in site_mapping_info.get("sample_site_mapping", {}).items():
                site_info = normalized_sites.get(site_id)
                if site_info:
                    sample_sites[sample_id] = site_info
            
        # 回退：从列名中直接匹配站点标识
        if not column_sites and plan["site_ids"] and table_headers:
            for header in table_headers:
                if not isinstance(header, str):
                    continue
                matching = [sid for sid in plan["site_ids"] if re.search(
                    rf"(?<![A-Za-z0-9]){re.escape(sid)}(?![A-Za-z0-9])", header, re.I
                )]
                if len(matching) == 1:
                    column_sites[header] = normalized_sites[matching[0]]
        
        plan["column_sites"] = column_sites
        plan["sample_sites"] = sample_sites
        
        strategies = []
        if column_sites:
            strategies.append("column")
        if sample_sites:
            strategies.append("sample")
        if plan["default_site"]:
            strategies.append("default")
        
        if strategies:
            plan["mode"] = "+".join(strategies)
        else:
            plan["mode"] = "fallback" if plan["default_site"] else ""
        
        return plan
    
    def _apply_site_assignment(
        self,
        data_point: Dict[str, Any],
        row_data: Dict[str, Any],
        assignment_plan: Dict[str, Any],
        pdf_context: Optional[Dict[str, Any]]
    ) -> None:
        """按照批量策略为数据点赋予研究地点坐标"""
        if not assignment_plan:
            return
        
        pdf_site_name = assignment_plan.get("pdf_site_name") or (pdf_context.get("geo_siteName") if pdf_context else "")
        research_sites = assignment_plan.get("research_sites", {})
        
        lat = finite_number(data_point.get("geo_latitude"))
        lon = finite_number(data_point.get("geo_longitude"))
        if lat is not None and lon is not None and validate_coordinates(lat, lon):
            return

        # 单地点场景：直接返回
        if assignment_plan.get("single_site"):
            self._set_site_coordinates(data_point, assignment_plan["single_site"], pdf_site_name)
            return
        
        column_sites = assignment_plan.get("column_sites", {})
        assigned = {col for key, col in data_point.get("source_columns", {}).items()
                    if key in self.unit_converter.default_units}
        matched = [site for col, site in column_sites.items() if col in assigned]
        matched_ids = {site.get("id") for site in matched}
        if len(matched_ids) == 1 and self._set_site_coordinates(data_point, matched[0], pdf_site_name):
            return

        sample_id = str(data_point.get("sample_id") or "").strip().casefold()
        sample_sites = {key.casefold(): site for key, site in assignment_plan.get("sample_sites", {}).items()}
        site = sample_sites.get(sample_id)
        if site and self._set_site_coordinates(data_point, site, pdf_site_name):
            return
        if sample_id:
            explicit_sites = [info for key, info in research_sites.items()
                              if str(key).strip().casefold() == sample_id]
            if len(explicit_sites) == 1 and self._set_site_coordinates(data_point, explicit_sites[0], pdf_site_name):
                return

        if assignment_plan.get("multiple_sites"):
            return

        # 默认站点策略
        if assignment_plan.get("default_site"):
            if self._set_site_coordinates(data_point, assignment_plan["default_site"], pdf_site_name):
                return
        
        # 最终兜底：尝试直接使用PDF上下文中的经纬度
        if pdf_context and pdf_context.get("geo_latitude") is not None and pdf_context.get("geo_longitude") is not None:
            fallback_site = self._standardize_site_info(
                "pdf_context",
                {
                    "latitude": pdf_context.get("geo_latitude"),
                    "longitude": pdf_context.get("geo_longitude"),
                    "label": pdf_context.get("geo_siteName")
                },
                pdf_context.get("geo_siteName")
            )
            if fallback_site:
                self._set_site_coordinates(data_point, fallback_site, pdf_site_name)
    
    def _set_site_coordinates(self, data_point: Dict[str, Any], site_info: Optional[Dict[str, Any]], pdf_site_name: Optional[str]) -> bool:
        """将指定站点信息写入数据点，并确保日志只打印一次"""
        if not site_info:
            return False
        
        latitude = site_info.get("latitude")
        longitude = site_info.get("longitude")
        if latitude is None or longitude is None:
            return False
        
        data_point["geo_latitude"] = latitude
        data_point["geo_longitude"] = longitude
        
        site_label = site_info.get("label") or site_info.get("id") or pdf_site_name
        if site_label:
            data_point["geo_siteName"] = site_label
        if site_info.get("id"):
            data_point["geo_site_id"] = site_info["id"]
        
        # 只在首次匹配到新站点时记录日志
        if site_info.get("id"):
            if not hasattr(self, "_logged_sites"):
                self._logged_sites = set()
            site_key = f"{site_info['id']}_{latitude}_{longitude}"
            if site_key not in self._logged_sites:
                self._logged_sites.add(site_key)
                logging.info(f"匹配到研究地点 {site_info['id']}: ({latitude}, {longitude})")
        
        return True
    
    def _ensure_row_dict(self, row_data: Any, headers: Optional[List[str]]) -> Optional[Dict[str, Any]]:
        """确保行数据为字典格式，便于后续处理"""
        if isinstance(row_data, dict):
            return row_data
        if isinstance(row_data, (list, tuple)) and headers:
            return dict(zip(headers, row_data))
        if isinstance(row_data, (list, tuple)):
            return {str(idx): value for idx, value in enumerate(row_data)}
        return None
    
    def _standardize_site_info(self, site_id: str, site_data: Optional[Dict[str, Any]], site_name_hint: Optional[str]) -> Optional[Dict[str, Any]]:
        """标准化站点信息，确保经纬度为浮点并在合理范围内"""
        if not site_data:
            return None
        
        latitude = self._safe_convert_coordinate(site_data.get("latitude"))
        longitude = self._safe_convert_coordinate(site_data.get("longitude"))
        if latitude is None or longitude is None:
            return None
        
        site_name = site_data.get("label") or site_name_hint or site_id
        if not validate_coordinates(latitude, longitude, geo_siteName=site_name or ""):
            return None
        
        standardized = {
            "id": site_id,
            "latitude": latitude,
            "longitude": longitude
        }
        if site_data.get("label"):
            standardized["label"] = site_data["label"]
        return standardized
    
    def _safe_convert_coordinate(self, value: Any) -> Optional[float]:
        """容错处理的坐标转换方法"""
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        
        try:
            converted = convert_coordinate_format(str(value))
            if isinstance(converted, (int, float)):
                return float(converted)
            return float(str(value))
        except Exception:
            return None
    
    def process_noaa_file(self, file_path: str, paper_hash: str = "") -> List[Dict[str, Any]]:
        return self.process_file(file_path, paper_hash=paper_hash)

    def process_pangaea_file(self, file_path: str, paper_hash: str = "") -> List[Dict[str, Any]]:
        return self.process_file(file_path, paper_hash=paper_hash)

    def _process_database_file(self, file_path, paper_hash, source_type, tables, paper_context_loader=None):
        try:
            parsed = self.database_parser.parse_file(file_path, paper_context_loader=paper_context_loader)
            sheet = os.path.basename(file_path)
            table = {"sheet_name": sheet, "row_count": len(parsed.get("data", []))}
            tables.append(table)
            if parsed.get("skip_reason"):
                table.update(status="excluded", reason=parsed["skip_reason"])
                evidence = parsed.get("skip_evidence")
                add_review_item({"file": file_path, "sheet": sheet, "action": "已排除非观测片段",
                                 "reason": f"解析前排除（{parsed['skip_reason']}）"
                                           + (f"：{evidence}" if evidence else "") + "，表块级留痕备查"})
                return []
            rows = self.database_parser.extract_standardized_data(parsed)
            points = []
            for point in rows:
                point.update(paper_hash=paper_hash or f"{source_type.lower()}_data",
                             source_type=source_type, sheet_name=sheet)
                point = self.spatiotemporal_standard.standardize_time_dimension(
                    point, {"resolution": point.get("hasResolution_nominal", "unknown")}
                )
                points.append(self._finalize_measurements(point))
            self._apply_sampling_profiles(points)
            self._flag_series_outliers(points)
            return points
        except Exception as exc:
            add_review_item({"file": file_path, "status": "failed", "action": "暂缓读取", "reason": str(exc)})
            logging.exception("处理 %s 数据失败: %s", source_type, file_path)
            return []

    def process_directory_parallel(self, directory_path, context_info=None):
        return self.process_directory(directory_path, context_info, use_parallel=True)

    def _paper_context(self, pdf_path):
        if not pdf_path:
            return {}
        text = extract_text_from_pdf(pdf_path)
        if not text or not re.sub(r"\[SOURCE:[^\]]+\]|=== 第\d+页 ===", "", text).strip():
            return {}
        context = {"_pdf_text": text}
        if self.enable_calibration_check:
            context["sr_ca_calibration_info"] = check_sr_ca_calibration(text)
        context.update(self.context_extractor.extract_from_text(text, source_name=pdf_path) or {})
        if "[SOURCE:MD_FILE]" in text:
            needs_doi = not context.get("pub1_doi")
            authors = context.get("pub1_authors")
            needs_year = bool(authors and not re.search(r"\d{4}", str(authors)))
            if needs_doi or not authors or needs_year:
                import pdfplumber
                from app.utils.metadata_extractor import _extract_doi_and_authors_with_regex

                try:
                    with pdfplumber.open(pdf_path) as pdf:
                        original_text = "\n".join(page.extract_text() or "" for page in pdf.pages[:3])
                    metadata = _extract_doi_and_authors_with_regex(original_text)
                    if needs_doi and metadata.get("pub1_doi"):
                        context["pub1_doi"] = metadata["pub1_doi"]
                    if not authors and metadata.get("pub1_authors"):
                        context["pub1_authors"] = metadata["pub1_authors"]
                    if needs_year and metadata.get("pub1_year"):
                        author_label = authors if "et al" in authors.lower() else f"{authors} et al."
                        context["pub1_authors"] = f"{author_label} {metadata['pub1_year']}"
                except Exception:
                    logging.warning("无法从 PDF 补齐正文元数据 %s", pdf_path, exc_info=True)
        return context

    def _process_paper_directory(self, directory, label, source, files, context_info, selected=None):
        native_tables = {}
        points = []
        seen = {}
        pdfs = [name for name in files if name.lower().endswith(".pdf")]
        body_files = [name for name in pdfs if is_paper_body_pdf(name)]
        primary = self._primary_pdf(directory, body_files)
        paper_contexts = {}
        body_dois = None
        checked_dois = set()

        def paper_context_for(expected_doi=None):
            nonlocal body_dois
            paper = primary
            link = "same_directory"
            if expected_doi:
                if body_dois is None:
                    import pdfplumber

                    body_dois = {}
                    for name in body_files:
                        path = os.path.join(directory, name)
                        try:
                            with pdfplumber.open(path) as document:
                                first_page = document.pages[0].extract_text() if document.pages else ""
                            body_dois[path] = publication_doi(first_page)
                        except Exception:
                            logging.warning("无法核对正文首页 DOI %s", path, exc_info=True)
                doi = publication_doi(expected_doi)
                matches = [path for path, candidate in body_dois.items() if doi and candidate == doi]
                if len(matches) == 1:
                    paper, = matches
                    link = "publication_doi"
                elif len(body_files) != 1 or not primary:
                    if body_files and doi not in checked_dois:
                        checked_dois.add(doi)
                        add_review_item({"file": directory, "action": "暂缓关联正文", "decision": expected_doi,
                                         "reason": "同一研究目录存在多份正文版本，留待论文级复核结合原件判断"})
                    return {}
            if not paper:
                if len(body_files) > 1 and None not in checked_dois:
                    checked_dois.add(None)
                    add_review_item({"file": directory, "action": "暂缓关联正文",
                                     "reason": "存在多份 PDF，无法唯一确定正文",
                                     "decision": " | ".join(sorted(body_files))})
                return {}
            if paper not in paper_contexts:
                try:
                    paper_contexts[paper] = self._paper_context(paper)
                except Exception as exc:
                    paper_contexts[paper] = {}
                    add_review_item({"file": paper, "action": "暂缓正文元数据", "reason": str(exc)})
                    logging.exception("读取正文上下文失败 %s", paper)
            context = paper_contexts[paper]
            if not context:
                return {}
            if expected_doi:
                return {**context, "pub1_doi": expected_doi, "paper_link": link}
            return {**context, "paper_link": link}

        pdf_stems = {os.path.splitext(name)[0].casefold() for name in pdfs}
        order = self.data_ingestion.WORKBOOK_EXTENSIONS | {".docx"}
        def source_order(name):
            suffix = os.path.splitext(name)[1].lower()
            return (suffix == ".pdf", suffix not in order, name.casefold())

        for name in sorted(files, key=source_order):
            path = os.path.join(directory, name)
            stem, suffix = os.path.splitext(name)
            suffix = suffix.lower()
            include = selected is None or name in selected
            if not include and suffix not in {".csv", ".tsv", ".txt", ".tab"}:
                continue
            if suffix == ".md" and stem.casefold() in pdf_stems:
                self._record_input(path, status="context", reason="对应 PDF 的正文文本")
                continue
            if suffix not in self.data_ingestion.ATTACHMENT_EXTENSIONS | {".pdf"}:
                self._record_input(path, status="unsupported", reason="未注册该文件格式的读取器")
                continue
            if suffix in {".csv", ".tsv", ".txt", ".tab"}:
                try:
                    with open(path, "rb") as stream:
                        digest = hashlib.file_digest(stream, "sha256").hexdigest()
                except OSError:
                    digest = None  # 由读取器报告可定位的错误。
                if digest in seen:
                    if include:
                        self._record_input(path, status="excluded", reason=f"同目录内容完全相同的副本: {seen[digest]}")
                    continue
                if digest:
                    seen[digest] = name
            if not include:
                continue
            if suffix in {".txt", ".tab"} and self.database_parser.detect_format(path) != DatabaseFormat.UNKNOWN:
                if not self._is_relevant_proxy_file(path):
                    norm = os.path.normcase(os.path.abspath(path))
                    digest = self._source_digest(path)
                    recover = self.unit_review_decisions.get(("FILE_RECOVER", norm))
                    recovered = (recover and recover["evidence_status"] == "verified"
                                 and recover.get("source_sha256") == digest)
                    if not recovered:
                        exclude = self.unit_review_decisions.get(("FILE_EXCLUDE", norm))
                        reason = "档案文件头未发现受支持指标线索"
                        if exclude and exclude["evidence_status"] == "verified" \
                                and exclude.get("source_sha256") == digest:
                            reason = exclude["note"]
                        self._record_input(path, status="excluded", reason=reason)
                        continue
                relative = label.replace(os.sep, "_")
                identity = f"{source}_{name}" if relative == "." else f"{source}_{relative}_{name}"
                points.extend(self.process_file(path, paper_hash=identity, paper_context_loader=paper_context_for))
                continue
            prefix = f"{source}_attachment" if source in {"pangaea", "noaa"} else source
            context = context_info.get(name, f"文献 {label} 的附件")
            points.extend(self.process_file(path, context, f"{prefix}_{label}", native_tables=native_tables,
                                            paper_context_loader=paper_context_for))
        return points

    def process_directory(self, directory_path, context_info=None, use_parallel=False,
                          noaa_dir=None, pangaea_dir=None, zenodo_dir=None, selected_files=None):
        """串行和并行使用相同的发现规则和按目录处理逻辑。"""
        excluded = {"__pycache__", "node_modules", "cache", "output"}
        selected_by_dir = None
        if selected_files is not None:
            selected_by_dir = {}
            for path in selected_files:
                parent, name = os.path.split(os.path.abspath(path))
                selected_by_dir.setdefault(os.path.normcase(parent), set()).add(os.path.normcase(name))
        tasks = []
        for source, directory in (("noaa", noaa_dir), ("pangaea", pangaea_dir),
                                  ("dataset", directory_path), ("zenodo", zenodo_dir)):
            if not directory or not os.path.isdir(directory):
                continue
            def walk_error(exc):
                path = exc.filename or directory
                item = {"file": path, "status": "failed", "action": "暂缓读取", "reason": str(exc)}
                add_review_item(item)
                self._remember_reviews([item])
                self._record_input(path, status="failed", reason=str(exc))

            for root, dirs, names in os.walk(directory, onerror=walk_error):
                dirs[:] = sorted(name for name in dirs if not name.startswith(".") and name not in excluded)
                files = sorted(name for name in names if not name.startswith((".", "~$")))
                if files:
                    selected = None
                    if selected_by_dir is not None:
                        wanted = selected_by_dir.get(os.path.normcase(os.path.abspath(root)), set())
                        selected = {name for name in files if os.path.normcase(name) in wanted}
                        if not selected:
                            continue
                        if any(os.path.splitext(name)[1].lower() not in {".txt", ".tab"}
                               or self.database_parser.detect_format(os.path.join(root, name)) == DatabaseFormat.UNKNOWN
                               for name in selected):
                            selected = None
                    relative = os.path.relpath(root, directory)
                    label = relative if source in {"noaa", "pangaea"} else relative.replace(os.sep, "_")
                    tasks.append({"path": root, "name": label, "source": source, "files": files, "selected": selected})

        def worker(task):
            with capture_review_items() as reviews:
                try:
                    return self._process_paper_directory(task["path"], task["name"], task["source"],
                                                         task["files"], context_info or {}, task["selected"])
                except Exception as exc:
                    logging.exception("处理输入目录失败 %s", task["path"])
                    add_review_item({"file": task["path"], "status": "failed", "action": "暂缓读取", "reason": str(exc)})
                    self._record_input(task["path"], status="failed", reason=str(exc))
                    return []
                finally:
                    self._remember_reviews(reviews)

        if use_parallel and tasks:
            results, errors = self.parallel_processor.processor.process_items(tasks, worker, show_progress=False)
            for index, error in errors.items():
                self._record_input(tasks[index]["path"], status="failed", reason=error)
        else:
            results = [worker(task) for task in tasks]
        points = [point for group in results for point in group]
        self.processed_data.extend(points)
        return points

    def pending_reviews(self):
        pending = list(self._review_items)
        for review_id in sorted(self.unit_review_decisions.keys() - self.applied_unit_reviews, key=str):
            decision = self.unit_review_decisions[review_id]
            location = {"review_id": review_id}
            reason = self.deferred_unit_reviews.get(review_id, "未匹配源观测或确认单位不可用")
            if isinstance(review_id, tuple):
                column = review_id[2] if review_id[0] == "COLUMN" else decision.get("selected")
                location = {"file": review_id[1], "column": column}
                if decision.get("sheet"):
                    location.update(sheet=decision["sheet"], source_sha256=decision["source_sha256"])
                if not os.path.isfile(review_id[1]):
                    reason = "裁决对应的源文件当前不存在"
                elif decision["evidence_status"] == "provisional":
                    reason = "候选裁决仍需核对源证据"
            pending.append({**location, "action": "人工裁决未应用",
                            "reason": reason, "proposed_decision": decision["action"],
                            "proposed_unit": decision["unit"], "review_note": decision["note"]})
        return pending

    # 仅这些技术原因保留待程序定向重试；其余复核完成未挽回者一律证据不足结案。
    _TECHNICAL_RETRY_PATTERNS = (
        ("原件不可读", re.compile(r"无法定位|无法读取|读不到|版本已变化|超限|容器无法")),
        ("裁决回灌失败", re.compile(r"回灌|安全回灌|未应用")),
        ("分批漏答", re.compile(r"分批|漏答|没有返回该复核组")),
        ("模型超时或配额", re.compile(r"超时|额度|配额|未完成|待重试|空响应|没有成功复核")),
    )

    @classmethod
    def _technical_retry_category(cls, finding):
        """区分技术失败（可程序定向重试）与证据不足（应结案）。返回原因类别或 None。"""
        if not finding:
            return None
        if finding.get("unreadable_files"):
            return "原件不可读"
        reason = str(finding.get("reason") or "")
        state = finding.get("state")
        if state == "pending":
            for label, pattern in cls._TECHNICAL_RETRY_PATTERNS:
                if pattern.search(reason):
                    return label
            return "复核未完成"
        if state in {"manual", "discarded"}:
            # 复核完成但原件本身读不到/版本变化属技术失败，而非证据不存在。
            for label, pattern in cls._TECHNICAL_RETRY_PATTERNS[:1]:
                if pattern.search(reason):
                    return label
        if state in {"verified", "applied"}:
            return "裁决已通过但未应用"
        return None

    def discard_unrecovered_reviews(self, review_summary=None):
        """仅将有完成证据的第三轮否定结论结案。未审或执行失败保留原始定位。"""
        from app.review_workbench.store import _category, review_case_id
        findings = (review_summary or {}).get("findings", {})
        source_root = os.getcwd()
        discarded = incomplete = 0
        categories = {}
        # 保留观测、仅提示性复核的项不属于"未挽回"，不得自动结案。
        informational = {"复核参考材料"}
        for item in self._review_items:
            action = item.get("action", "")
            if not action.startswith(("暂缓", "复核")) or action in informational:
                continue
            original = str(item.get("reason", ""))
            # 距平/缩放/中心化二次处理序列：绝对值不可入库，直接排除，
            # 不进入第三轮复核（即使被归为 history 也须结案）。
            if "中心化/缩放/距平二次处理序列" in original:
                item["action"] = "已排除非观测片段"
                item["review_decision"] = "exclude"
                item["reason"] = (f"二次处理序列（中心化/缩放/距平）绝对值不可入数据库，"
                                  f"直接排除、不进入第三轮复核。原由：{original}")
                item.pop("retry_reason", None)
                item.pop("retry_category", None)
                discarded += 1
                continue
            if _category(item) == "history":
                continue
            # 按完整复核组身份匹配（含 action/reason/decision/source_unit）：
            # 同表同列但不同复核事项属不同组，结论不得串用。先算身份再改 item。
            finding = findings.get(review_case_id(item, source_root))
            technical = self._technical_retry_category(finding)
            if technical:
                item["review_decision"] = "retry_pending"
                item["retry_reason"] = (finding.get("reason") or "论文级复核未完成")
                item["retry_category"] = technical
                categories[technical] = categories.get(technical, 0) + 1
                incomplete += 1
            elif finding is None:
                item["review_decision"] = "retry_pending"
                item["retry_reason"] = "未进入论文级复核"
                item["retry_category"] = "未复核"
                categories["未复核"] = categories.get("未复核", 0) + 1
                incomplete += 1
            elif (finding.get("state") in {"manual", "discarded"}
                  and (finding.get("review_completed") or finding.get("reviewed_files"))):
                item["action"] = "已三轮复核舍弃"
                item["review_decision"] = "auto_discard"
                item["reason"] = (f"本批证据不足，未纳入：三轮复核未挽回（证据不存在或不充分），"
                                  f"自动结案；源数据保留、可逆。原由：{original}")
                item.pop("retry_reason", None)
                item.pop("retry_category", None)
                discarded += 1
            else:
                item["review_decision"] = "retry_pending"
                item["retry_reason"] = finding.get("reason") or "缺少可确认复核已完成的证据"
                item["retry_category"] = "复核状态未知"
                categories["复核状态未知"] = categories.get("复核状态未知", 0) + 1
                incomplete += 1
        self.incomplete_review_count = incomplete
        self.retry_pending_categories = categories
        return discarded

    def generate_final_output(self, paper_review=None) -> str:
        """
        生成最终输出
        
        Returns:
            str: 输出目录路径
        """
        # 最终过滤
        before_count = len(self.processed_data)
        self.processed_data = filter_empty_key_fields(self.processed_data)
        after_count = len(self.processed_data)
        
        if before_count > 0:
            logging.info(f"数据过滤完成: {before_count} 条 -> {after_count} 条 (丢失 {before_count - after_count} 条)")
        
        pending = self.pending_reviews()
        if not self.processed_data and not pending and not self.source_inventory:
            logging.warning("没有可输出的观测或复核项")
            return ""

        quality_report = self.quality_control.generate_quality_report(self.processed_data)
        metadata_summary = self.data_output.generate_metadata_summary(self.processed_data)
        output_path = self.data_output.integrate_and_output(
            self.processed_data, quality_report, metadata_summary, review_items=pending,
            source_inventory=sorted(self.source_inventory, key=lambda row: (row["file"], row["sheet"] or "")),
            paper_review=paper_review,
        )
        clear_review_items()
        self._review_items.clear()
        return output_path
    
    def get_processed_data(self) -> List[Dict[str, Any]]:
        """获取处理后的数据"""
        return self.processed_data
    
    def get_quality_reports(self) -> List[Dict[str, Any]]:
        """获取质量报告"""
        return self.quality_reports

# 示例使用
if __name__ == "__main__":
    # 创建MOA工作流实例
    workflow = MOAWorkflow()
    
    # 示例：处理单个文件
    # processed_data = workflow.process_file("example.xlsx", "Palmyra Atoll珊瑚古气候数据", "paper_hash_123")
    # print(f"处理完成，共 {len(processed_data)} 条数据")
    
    # 示例：处理目录
    # context_info = {
    #     "Supplementary_Dataset.xlsx": "Palmyra Atoll珊瑚古气候数据，包含月分辨率锶钙比和氧同位素记录"
    # }
    # all_data = workflow.process_directory("dataset/", context_info)
    # print(f"目录处理完成，共 {len(all_data)} 条数据")
    
    # 生成最终输出
    # output_path = workflow.generate_final_output()
    # print(f"数据已输出到: {output_path}")
    pass
