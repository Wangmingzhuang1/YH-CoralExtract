#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
大模型解析层模块
负责语义解析、字段映射、元数据补全和数据合理性校验
处理dataset子文件夹中的PDF正文和对应附件（Excel/Word），确保Excel转换为CSV
当附录中没有地点名称、经纬度或单位时，从PDF正文中获取
"""

import logging
import json
import os
import re
import unicodedata
from collections import Counter
from typing import Dict, List, Any, Optional
import numpy as np
from pandas import isna
from app.core.config import settings
from app.core.moa.context_extractor import build_table_evidence, composite_evidence, quote_names_column, sample_identity_header
from app.utils.llm_api import call_llm
from app.core.moa.llm_parser_validation import ParsingResultValidator
from app.core.moa.spatiotemporal_standard import time_basis
from app.core.moa.unit_converter import UnitConversionModule, canonical_unit, resolve_unit, source_unit
from app.utils.llm_cache import LLMCache, LLMBatchProcessor
from app.utils.llm_judge import judge_enabled, judge_noul_batch, escalate_remap
from app.utils.review_queue import add_review_item, mapping_review_key

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# 新增：核心指标正则匹配模式（覆盖带样品编号和附加修饰的列名）
PATTERNS = {
    "Sr/Ca_value": re.compile(r'.*(Sr/Ca|锶钙比).*\((\w+/\w+)\)'),  # 匹配含Sr/Ca及单位的列名
    "Ba/Ca_value": re.compile(r'.*(Ba/Ca|钡钙比).*\((\w+/\w+)\)'),  # 匹配含Ba/Ca及单位的列名
    "δ18O_value": re.compile(r'.*(δ18O|氧同位素|d18O).*\((‰\s*[A-Za-z]+)\)'),  # 匹配含δ18O及单位的列名
    "δ18O_difference": re.compile(r'.*(Δδ18O|Δ18O|氧同位素差值|d18O\s+difference|delta18O\s+difference).*\((‰\s*[A-Za-z]+)\)'),  # 匹配含Δδ18O及单位的列名
    "δ13C_value": re.compile(r'.*(δ13C|碳同位素|d13C).*\((‰\s*[A-Za-z]+)\)'),   # 匹配含δ13C及单位的列名
    "sea_surface_temperature_value": re.compile(r".*(?:\bSST\b|sea[\s_-]+surface[\s_-]+temperature|海表温度)", re.I),
    "hydrological_signal": re.compile(r'.*(δ¹⁸Osw-wet|δ18Osw-dry).*\((‰\s*[A-Za-z]+)\)')  # 匹配水文信号列名
}

# 需要排除的二次处理数据关键词（全局）
EXCLUDE_KEYWORDS = [
    "offset", "diff", "anom", "residual",  # 偏移/差异/距平/残差
    "centered", "centred", "demean", "de-mean", "scaled",  # 中心化/去均值/缩放序列
    "spliced", "splice", "composite", "stack", "average", "mean",  # 拼接/组合/平均
    "interpolated", "interp", "grid",  # 插值（如果没有原始数据可考虑保留，但此处作为通用排除）
    "trend", "detrend", "smooth", "filter",  # 趋势/去趋势/平滑/滤波
    "relative", "relative age", "相对年龄"  # 相对年龄（需要绝对年龄，不要相对年龄）
]

def is_derived_observation_column(header):
    """过滤非骨骼观测列，包括海水氧同位素。"""
    name = re.sub(r"[_-]", " ", unicodedata.normalize("NFKC", str(header))).casefold()
    return bool(re.search(r"anom|residual|detrend|std\s*dev|filtered|smoothed|\boffset\b|\buncertainty\b|"
                          r"(?:δ|d(?:elta)?)\s*18\s*o\s*(?:sw\b|sea\s*water\b)", name))


# 核心指标列名 token（不要求括号单位，比 PATTERNS 更宽松；比值类兼容 / _ 空格 分隔）：用于判卷"确定性置信地板"
CORE_PROXY_TOKENS = {
    "Sr/Ca_value": re.compile(r"Sr[\s/_]*Ca|锶钙比", re.I),
    "Ba/Ca_value": re.compile(r"Ba[\s/_]*Ca|钡钙比", re.I),
    "Mg/Ca_value": re.compile(r"Mg[\s/_]*Ca|镁钙比", re.I),
    "δ18O_value": re.compile(r"δ\s*18\s*O|d\s*18\s*O|delta\s*18\s*O|氧同位素", re.I),
    "δ13C_value": re.compile(r"δ\s*13\s*C|d\s*13\s*C|delta\s*13\s*C|碳同位素", re.I),
    "sea_surface_temperature_value": re.compile(r"\bSST\b|sea[\s_-]*surface[\s_-]*temp|海表温度", re.I),
}

# 误差/离散度列标识：即便含核心指标 token 也不放行（应交 Jev/规则按 skip 处理），
# 补足 EXCLUDE_KEYWORDS 未覆盖的 error/±/Nsd/se/range 等（在归一化小写串上匹配；is_derived 已覆盖 uncertainty/std dev）
ERROR_COLUMN_RE = re.compile(r"error|\berr\b|sigma|±|\d*\s*s\.?d\b|\d*\s*s\.?e\b|stdev|\bstd\b|\brange\b")


def is_confident_core_proxy(header, field):
    """确定性置信地板：列名清晰指示其所映射的核心指标、且非派生/排除列时返回 True。

    用途：避免 Jev 对"平滑但合法"的核心指标列（如 Sr/Ca-a.sam-4pt、Sr/Ca[µmol/mol]）
    系统性打低分（~0.56）而误送人工复核。
    安全边界（缺一不可）：
      1. token 必须与所映射字段一致 → 拦截 Sr/Ca→δ13C、SST→Sr/Ca 类错映；
      2. is_derived_observation_column 排除海水δ18Osw/距平/残差/平滑/滤波等；
      3. EXCLUDE_KEYWORDS 排除显式派生列（average/mean/anom/interp/splice 等）。
    模糊列名（如 slope-in-T 不含 SST 字样）不满足条件1，仍走 Jev+升级，不会被地板放行。
    """
    if field not in CORE_PROXY_TOKENS:
        return False
    if not CORE_PROXY_TOKENS[field].search(str(header)):
        return False
    if is_derived_observation_column(header):
        return False
    norm = re.sub(r"[_-]", " ", unicodedata.normalize("NFKC", str(header))).casefold()
    if ERROR_COLUMN_RE.search(norm):
        return False
    if any(kw.lower() in norm for kw in EXCLUDE_KEYWORDS):
        return False
    return True


class LLMParsingModule:
    """大模型解析层模块"""
    
    def __init__(self, enable_cache: bool = True, cache_dir: str = "cache"):
        # 初始化缓存
        self.enable_cache = enable_cache
        self.cache = LLMCache(cache_dir=cache_dir) if enable_cache else None
        self.batch_processor = LLMBatchProcessor(self.cache) if enable_cache else None
        
        # 标准字段映射字典
        self.field_mapping = {
            # 锶钙比相关字段
            "Sr/Ca": "Sr/Ca_value",
            "锶钙比": "Sr/Ca_value",
            "Sr_Ca": "Sr/Ca_value",
            "sr/ca": "Sr/Ca_value",
            # 带样品编号和单位的锶钙比字段
            "PM Sr/Ca (mmol/mol)": "Sr/Ca_value",
            "PM1 Sr/Ca (mmol/mol)": "Sr/Ca_value",
            "PM5 Sr/Ca (mmol/mol)": "Sr/Ca_value",
            "P13 Sr/Ca (mmol/mol)": "Sr/Ca_value",
            "PAL2 Sr/Ca (mmol/mol)": "Sr/Ca_value",
            "E17 Sr/Ca (mmol/mol)": "Sr/Ca_value",
            "E18 Sr/Ca (mmol/mol)": "Sr/Ca_value",
            "E11 Sr/Ca (mmol/mol)": "Sr/Ca_value",
            "E26 Sr/Ca (mmol/mol)": "Sr/Ca_value",
            "E9 Sr/Ca (mmol/mol)": "Sr/Ca_value",
            "E14 Sr/Ca (mmol/mol)": "Sr/Ca_value",
            "E19 Sr/Ca (mmol/mol)": "Sr/Ca_value",
            "E24 Sr/Ca (mmol/mol)": "Sr/Ca_value",
            "E2-100 Sr/Ca (mmol/mol)": "Sr/Ca_value",
            "E12 Sr/Ca (mmol/mol)": "Sr/Ca_value",
            "E22 Sr/Ca (mmol/mol)": "Sr/Ca_value",
            "E15 Sr/Ca (mmol/mol)": "Sr/Ca_value",
            "E5 Sr/Ca (mmol/mol)": "Sr/Ca_value",
            "E2 Sr/Ca (mmol/mol)": "Sr/Ca_value",
            "E33 Sr/Ca (mmol/mol)": "Sr/Ca_value",
            "E30 Sr/Ca (mmol/mol)": "Sr/Ca_value",
            "E3 Sr/Ca (mmol/mol)": "Sr/Ca_value",
            "E28 Sr/Ca (mmol/mol)": "Sr/Ca_value",
            "E31 Sr/Ca (mmol/mol)": "Sr/Ca_value",
            "EL Sr/Ca (mmol/mol)": "Sr/Ca_value",
            "LY1 Sr/Ca (mmol/mol)": "Sr/Ca_value",
            "E33L Sr/Ca (mmol/mol)": "Sr/Ca_value",
            "LY2 Sr/Ca (mmol/mol)": "Sr/Ca_value",
            
            # 钡钙比相关字段
            "Ba/Ca": "Ba/Ca_value",
            "钡钙比": "Ba/Ca_value",
            "Ba_Ca": "Ba/Ca_value",
            "ba/ca": "Ba/Ca_value",
            "Ba/Ca (μmol/mol)": "Ba/Ca_value",
            "Ba/Ca [μmol/mol]": "Ba/Ca_value",
            
            # 氧同位素相关字段
            "δ18O": "δ18O_value",
            "氧同位素": "δ18O_value",
            "d18O": "δ18O_value",
            "delta18O": "δ18O_value",
            # 带样品编号和单位的氧同位素字段
            "PM δ18O (‰ VPDB)": "δ18O_value",
            "PM1 δ18O (‰ VPDB)": "δ18O_value",
            "PM5 δ18O (‰ VPDB)": "δ18O_value",
            "P13 δ18O (‰ VPDB)": "δ18O_value",
            "PAL2 δ18O (‰ VPDB)": "δ18O_value",
            "E17 δ18O (‰ VPDB)": "δ18O_value",
            "E18 δ18O (‰ VPDB)": "δ18O_value",
            "E11 δ18O (‰ VPDB)": "δ18O_value",
            "E26 δ18O (‰ VPDB)": "δ18O_value",
            "E9 δ18O (‰ VPDB)": "δ18O_value",
            "E14 δ18O (‰ VPDB)": "δ18O_value",
            "E19 δ18O (‰ VPDB)": "δ18O_value",
            "E24 δ18O (‰ VPDB)": "δ18O_value",
            "E2-100 δ18O (‰ VPDB)": "δ18O_value",
            "E12 δ18O (‰ VPDB)": "δ18O_value",
            "E22 δ18O (‰ VPDB)": "δ18O_value",
            "E15 δ18O (‰ VPDB)": "δ18O_value",
            "E5 δ18O (‰ VPDB)": "δ18O_value",
            "E2 δ18O (‰ VPDB)": "δ18O_value",
            "E33 δ18O (‰ VPDB)": "δ18O_value",
            "E30 δ18O (‰ VPDB)": "δ18O_value",
            "E3 δ18O (‰ VPDB)": "δ18O_value",
            "E28 δ18O (‰ VPDB)": "δ18O_value",
            "E31 δ18O (‰ VPDB)": "δ18O_value",
            "EL δ18O (‰ VPDB)": "δ18O_value",
            "LY1 δ18O (‰ VPDB)": "δ18O_value",
            "E33L δ18O (‰ VPDB)": "δ18O_value",
            "LY2 δ18O (‰ VPDB)": "δ18O_value",
            
            # 氧同位素差值相关字段（Δδ18O）
            "Δδ18O": "δ18O_difference",
            "Δ18O": "δ18O_difference",
            "氧同位素差值": "δ18O_difference",
            "d18O difference": "δ18O_difference",
            "delta18O difference": "δ18O_difference",
            # 带样品编号和单位的氧同位素差值字段
            "PM Δδ18O (‰ VPDB)": "δ18O_difference",
            "PM1 Δδ18O (‰ VPDB)": "δ18O_difference",
            "PM5 Δδ18O (‰ VPDB)": "δ18O_difference",
            "P13 Δδ18O (‰ VPDB)": "δ18O_difference",
            "PAL2 Δδ18O (‰ VPDB)": "δ18O_difference",
            "E17 Δδ18O (‰ VPDB)": "δ18O_difference",
            "E18 Δδ18O (‰ VPDB)": "δ18O_difference",
            "E11 Δδ18O (‰ VPDB)": "δ18O_difference",
            "E26 Δδ18O (‰ VPDB)": "δ18O_difference",
            "E9 Δδ18O (‰ VPDB)": "δ18O_difference",
            "E14 Δδ18O (‰ VPDB)": "δ18O_difference",
            "E19 Δδ18O (‰ VPDB)": "δ18O_difference",
            "E24 Δδ18O (‰ VPDB)": "δ18O_difference",
            "E2-100 Δδ18O (‰ VPDB)": "δ18O_difference",
            "E12 Δδ18O (‰ VPDB)": "δ18O_difference",
            "E22 Δδ18O (‰ VPDB)": "δ18O_difference",
            "E15 Δδ18O (‰ VPDB)": "δ18O_difference",
            "E5 Δδ18O (‰ VPDB)": "δ18O_difference",
            "E2 Δδ18O (‰ VPDB)": "δ18O_difference",
            "E33 Δδ18O (‰ VPDB)": "δ18O_difference",
            "E30 Δδ18O (‰ VPDB)": "δ18O_difference",
            "E3 Δδ18O (‰ VPDB)": "δ18O_difference",
            "E28 Δδ18O (‰ VPDB)": "δ18O_difference",
            "E31 Δδ18O (‰ VPDB)": "δ18O_difference",
            "EL Δδ18O (‰ VPDB)": "δ18O_difference",
            "LY1 Δδ18O (‰ VPDB)": "δ18O_difference",
            "E33L Δδ18O (‰ VPDB)": "δ18O_difference",
            "LY2 Δδ18O (‰ VPDB)": "δ18O_difference",
            
            # 碳同位素相关字段
            "δ13C": "δ13C_value",
            "碳同位素": "δ13C_value",
            "d13C": "δ13C_value",
            "delta13C": "δ13C_value",
            
            # 海表温度相关字段
            "SST": "sea_surface_temperature_value",
            "海表温度": "sea_surface_temperature_value",
            
            # 时间相关字段
            "Year": "year",
            "Date": "year",
            "年份": "year",
            "年代": "year",
            "采样年份": "year",
            "Yr": "year",
            "AGE": "year",
            "Year (CE)": "year",
            "Year BP": "year",
            "Years CE": "year",
            "年代(CE)": "year",
            "采样年": "year",
            "Estimated date using satellite T": "year",
            "Estimated date using in situ T": "year",
            "Year (C.E.)": "year",
            
            # 其他常用字段
            "latitude": "geo_latitude",
            "纬度": "geo_latitude",
            "longitude": "geo_longitude",
            "经度": "geo_longitude",
            "site": "geo_siteName",
            "采样点": "geo_siteName",
            "地点": "geo_siteName",
            
            # 新增的符合数据同化标准的字段
            # 移除非必要字段，仅保留必要字段
            "paleoData_dating_method": "paleoData_dating_method",
            "paleoData_dating_error": "paleoData_dating_error",
            "paleoData_holocene_stage": "paleoData_holocene_stage",
            "testing_method": "testing_method"
        }
        
        # 全新世时段映射
        self.holocene_stage_mapping = {
            "早全新世": "早全新世",
            "中全新世": "中全新世",
            "晚全新世": "晚全新世",
            "CE": "CE",
            "Common Era": "CE",
            "现代": "modern",
            "中世纪气候异常期": "Medieval Climate Anomaly",
            "小冰期": "LIA"
        }
    
    def parse_table_structure(self, table_data: Dict[str, Any], context: str = "", pdf_context: Optional[Dict[str, Any]] = None, known_variables: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
        """
        解析表格结构，识别表名含义和行列名语义
        
        Args:
            table_data (Dict[str, Any]): 表格数据
            context (str): 上下文信息（如文献标题、摘要等）
            pdf_context (Dict[str, Any], optional): 从PDF正文中提取的上下文信息，包括地理位置和单位信息
            known_variables (List[Dict[str, str]], optional): 已知的变量定义列表（从文件头解析）
            
        Returns:
            Dict[str, Any]: 解析结果，包含标准化字段映射、元数据补全等
        """
        headers = table_data.get('headers', [])
        
        # 【新增】检测是否为“作者+年份”格式的引用表，如果是则跳过
        if self._is_author_year_citation_table(headers):
            logging.info(f"检测到引用表（列名包含作者+年份格式），跳过解析")
            return {
                "mapped_data": [],
                "table_metadata": {"skip_reason": "author_year_citation_table"},
                "complemented_metadata": {}
            }
        
        evidence = build_table_evidence(table_data, pdf_context, known_variables)
        reviewed = self._verified_mappings(table_data, evidence)
        try:
            parsed_result = self._map_with_model(table_data, context, pdf_context, evidence)
            if parsed_result is None:
                logging.warning("列映射模型无可用响应，使用默认解析")
                result = self.apply_source_units(self._default_parsing(table_data, pdf_context), table_data, pdf_context, evidence)
                return self._merge_verified_mappings(result, reviewed)
            for section in ("table_metadata", "complemented_metadata"):
                if not isinstance(parsed_result.get(section), dict):
                    parsed_result[section] = {}

            # 【修复】补充缺失的单位信息（对大模型解析结果）
            parsed_result = self.apply_source_units(parsed_result, table_data, pdf_context, evidence)
            
            # 【日志】批量汇总列名→核心指标与单位映射（避免刷屏）
            try:
                if isinstance(parsed_result, dict) and "mapped_data" in parsed_result:
                    mapped_items = parsed_result["mapped_data"]
                    if mapped_items:
                        # 统计映射结果
                        total_columns = len(mapped_items)
                        valid_mappings = [item for item in mapped_items if isinstance(item, dict) and item.get("standard_field") not in [None, "skip"]]
                        skip_mappings = [item for item in mapped_items if isinstance(item, dict) and item.get("standard_field") == "skip"]
                        
                        logging.info(f"\n{'='*60}")
                        logging.info(f"【附件数据-LLM解析】列名映射汇总 - 表格: {table_data.get('sheet_name', 'unknown')}")
                        logging.info(f"总列数: {total_columns}, 有效映射: {len(valid_mappings)}, 跳过: {len(skip_mappings)}")
                        logging.info(f"{'-'*60}")
                        
                        # 输出有效映射
                        for item in valid_mappings:
                            original_header = item.get('original_header', 'unknown')
                            standard_field = item.get('standard_field', 'unknown')
                            unit = item.get('unit', 'unknown')
                            logging.info(f"  '{original_header}' -> {standard_field} ({unit})")
                        
                        # 输出被跳过的列
                        if skip_mappings:
                            logging.info(f"{'-'*60}")
                            logging.info(f"跳过的列 ({len(skip_mappings)}):")
                            for item in skip_mappings:
                                logging.info(f"  '{item.get('original_header')}' (原因: 非主时间列/无效字段)")
                        
                        logging.info(f"{'='*60}\n")
            except Exception as log_err:
                logging.debug(f"记录列名映射日志失败: {log_err}")
            
            # 如果提供了PDF上下文信息，则补充元数据
            if pdf_context:
                parsed_result = self._apply_pdf_context(parsed_result, pdf_context)
            
            # 【后处理修正】强制修复已知的常见映射错误（即使是从缓存读取的）
            # 1. 修复 Ba/Ca 被错误映射为 Sr/Ca 的问题
            # 2. 修复 Year (CE/AD) 单位显示为 'year' 的问题
            # 3. 过滤相对年龄列（需要绝对年龄，不要相对年龄）
            if isinstance(parsed_result, dict) and "mapped_data" in parsed_result:
                mapped_data = parsed_result["mapped_data"]
                if isinstance(mapped_data, list):
                    # 先过滤掉相对年龄列
                    filtered_mapped_data = []
                    for item in mapped_data:
                        if not isinstance(item, dict):
                            filtered_mapped_data.append(item)
                            continue
                        
                        original_header = item.get("original_header", "")
                        standard_field = item.get("standard_field", "")
                        
                        # 【新增】过滤相对年龄列
                        header_lower = original_header.lower()
                        if any(kw in header_lower for kw in ["relative", "相对年龄", "relative age"]):
                            # 如果是时间相关字段且包含相对年龄关键词，标记为skip
                            if standard_field in ["year", "age_ka_bp"]:
                                logging.info(f"【后处理过滤】跳过相对年龄列: {original_header} (需要绝对年龄)")
                                item["standard_field"] = "skip"
                        
                        filtered_mapped_data.append(item)
                    
                    # 更新mapped_data
                    parsed_result["mapped_data"] = filtered_mapped_data
                    mapped_data = filtered_mapped_data
                    
                    for item in mapped_data:
                        if not isinstance(item, dict):
                            continue
                        
                        original_header = item.get("original_header", "")
                        standard_field = item.get("standard_field", "")
                        
                        # 跳过已标记为skip的项
                        if standard_field == "skip":
                            continue
                        
                        # 1. 修正 Ba/Ca 错误
                        if "Ba/Ca" in original_header or "Ba_Ca" in original_header:
                            if standard_field == "Sr/Ca_value":
                                item["standard_field"] = "Ba/Ca_value"
                                logging.info(f"【后处理修正】将列 '{original_header}' 从 Sr/Ca_value 修正为 Ba/Ca_value")
                        
                        # 2. 修正 Year 单位
                        if standard_field == "year":
                            header_upper = original_header.upper()
                            if "AD" in header_upper or "CE" in header_upper or "COMMON ERA" in header_upper:
                                if item.get("unit") == "year":
                                    item["unit"] = "CE"
                                    logging.info(f"【后处理修正】将列 '{original_header}' 的单位修正为 CE")

        except Exception as e:
            logging.error(f"大模型解析失败: {str(e)}")
            # 返回默认解析结果
            result = self.apply_source_units(self._default_parsing(table_data, pdf_context), table_data, pdf_context, evidence)
            return self._merge_verified_mappings(result, reviewed)

        result = self._judge_mapped_columns(parsed_result, table_data, pdf_context, known_variables, evidence,
                                           reviewed_columns=reviewed)
        return self._merge_verified_mappings(result, reviewed)

    def _verified_mappings(self, table, evidence):
        file, sheet = table.get("file_path"), table.get("sheet_name")
        if not file or not sheet or os.path.normcase(os.path.abspath(file)) not in getattr(self, "mapping_files", ()):
            return {}
        digest = self.mapping_digest(file)
        reviewed = {}
        for column in table["headers"]:
            decision = self.mapping_decisions.get(mapping_review_key(file, sheet, column, digest))
            if not decision or decision["evidence_status"] != "verified":
                continue
            target, unit = decision["target"], decision["unit"]
            if target not in UnitConversionModule.default_units and (target not in {"year", "age_bp", "age_ka_bp", "age_bce"}
                    or time_basis(unit) != target):
                continue
            item = {"original_header": column, "standard_field": target, "unit": unit}
            if target in UnitConversionModule.default_units:
                info = evidence["column_evidence"].get(column, {})
                direct = resolve_unit(target, column, (("unit_row", info.get("unit_row")),
                                                       ("source_definition", info.get("archive_unit"))))
                if unit == "unknown":
                    item.update(unit=direct["unit"] or "unknown", unit_resolution=direct)
                    reviewed[column] = item
                    continue
                compatible = {None, unit} | ({"‰"} if target in {"δ18O_value", "δ13C_value", "δ18O_difference"} else set())
                if (direct["status"] == "unit_conflict" or direct["unit"] not in compatible
                        or UnitConversionModule().convert_value(target, 1, unit) is None):
                    continue
                item["unit_resolution"] = {**direct, "unit": unit, "status": "resolved",
                    "evidence": [*direct["evidence"], {"source": "paper_model_review" if decision["origin"] == "paper_model"
                                                       else "source_rule_review", "evidence_status": "verified",
                                                       "unit": unit, "note": decision["note"]}]}
            reviewed[column] = item
        return reviewed

    @staticmethod
    def _merge_verified_mappings(result, reviewed):
        if reviewed and result.get("table_metadata", {}).get("is_coral_data") is not False:
            result["mapped_data"] = [item for item in result.get("mapped_data", [])
                                     if not isinstance(item, dict) or item.get("original_header") not in reviewed]
            result["mapped_data"].extend(reviewed.values())
        return result

    @staticmethod
    def _restrict_evidence(evidence, columns):
        allowed = set(columns)
        scoped = {**evidence, "table_headers": [name for name in evidence["table_headers"] if name in allowed],
                  "column_evidence": {name: item for name, item in evidence["column_evidence"].items() if name in allowed}}
        if "sample_rows" in evidence:
            scoped["sample_rows"] = [{name: value for name, value in row.items() if name in allowed}
                                     for row in evidence["sample_rows"]]
        return scoped

    @staticmethod
    def _context_axes(headers):
        return [name for name in headers if re.search(r"\b(?:date|time|year|age|CE|AD|BP|BCE)\b|年代|日期|年份",
                                                     name.replace("_", " "), re.I)][:4]

    def _request_mapping(self, headers, prompt):
        cache_context = "table-map-v8\n" + prompt
        cached = self.cache.get(headers, cache_context) if self.enable_cache and self.cache else None
        try:
            result = cached["response"] if cached else call_llm("max", prompt, temperature=0)
            if not isinstance(result, str) or "{" not in result or "}" not in result:
                return None
            parsed = json.loads(result[result.index("{"):result.rindex("}") + 1])
            valid, _ = ParsingResultValidator().validate_parsing_result(parsed)
        except (AttributeError, TypeError, ValueError, RuntimeError, KeyError) as exc:
            logging.warning("列映射响应不可用: %s", exc)
            return None
        if not valid:
            return None
        if not cached and self.enable_cache and self.cache:
            self.cache.set(headers, {"response": result}, cache_context)
        return parsed

    def _map_with_model(self, table, context, pdf_context, evidence):
        headers = evidence["table_headers"]
        prompt = self._generate_parsing_prompt(table, context, pdf_context, evidence=evidence)
        if len(headers) <= 32 and len(prompt) <= 25000:
            response = self._request_mapping(headers, prompt)
            if response is None:
                return None
            mapped = self._mapped_source_columns(table, headers, response)
            metadata = response.get("table_metadata")
            if (isinstance(metadata, dict) and len(mapped) != len(headers)
                    and metadata.get("is_coral_data") is False):
                metadata = {key: value for key, value in metadata.items() if key != "is_coral_data"}
            return {**response, "mapped_data": mapped, "table_metadata": metadata}

        axes = self._context_axes(headers)
        parts = []
        pending = [headers]
        while pending:
            columns = pending.pop()
            scoped = self._restrict_evidence(evidence, [*axes, *columns])
            scoped["target_columns"] = columns
            prompt = self._generate_parsing_prompt(table, context, pdf_context, evidence=scoped)
            if len(columns) > 1 and (len(columns) > 32 or len(prompt) > 25000):
                midpoint = len(columns) // 2
                pending.extend((columns[midpoint:], columns[:midpoint]))
                continue
            parts.append((columns, self._request_mapping(columns, prompt) if len(prompt) <= 25000 else None))

        mapped = []
        metadata = []
        for columns, response in parts:
            mapped.extend(self._mapped_source_columns(table, columns, response))
            if response and isinstance(response.get("table_metadata"), dict):
                metadata.append(response["table_metadata"])
        common = {key: value for key, value in metadata[0].items()
                  if all(part.get(key) == value for part in metadata[1:])} if len(metadata) == len(parts) else {}
        if len(mapped) != len(headers) and common.get("is_coral_data") is False:
            common.pop("is_coral_data")
        logging.info("宽表列映射: %s，%d 列分为 %d 批，返回 %d 列", table.get("sheet_name"),
                     len(headers), len(parts), len(mapped))
        return {"mapped_data": mapped, "table_metadata": common}

    @staticmethod
    def _mapped_source_columns(table, columns, response):
        """以原列为准，隔离漏列和相互矛盾的映射。"""
        exact = set(columns)
        aliases = {}
        for column in columns:
            aliases.setdefault(column.strip().casefold(), []).append(column)
        identifiers = {column for column in columns if sample_identity_header(column)}
        returned = {column: {"original_header": column, "standard_field": "skip"} for column in columns if column in identifiers}
        conflicts = set()
        for item in (response or {}).get("mapped_data", []):
            label, field = item.get("original_header"), item.get("standard_field")
            if not isinstance(label, str) or not isinstance(field, str) or not field.strip():
                continue
            candidates = aliases.get(label.strip().casefold(), ())
            name = label if label in exact else candidates[0] if len(candidates) == 1 else None
            if name is None or name in identifiers:
                continue
            previous = returned.setdefault(name, {**item, "original_header": name})
            if (previous["standard_field"], previous.get("unit")) != (field, item.get("unit")):
                conflicts.add(name)
        missed = [name for name in columns if name not in returned or name in conflicts]
        if missed:
            add_review_item({"file": table.get("file_path"), "sheet": table.get("sheet_name"),
                             "column": " | ".join(missed)[:1200], "action": "暂缓列映射",
                             "reason": f"模型未返回 {len(missed)} 个源列的唯一有效映射"})
        return [item for name, item in returned.items() if name not in conflicts]

    def _judge_column_batches(self, state, instructions, valid_items):
        if len(instructions) <= 24 and len(json.dumps([state, instructions], ensure_ascii=False, default=str)) <= 16000:
            return judge_noul_batch(state, instructions)
        axes = self._context_axes(state["table_headers"])
        pending = [list(instructions)]
        probabilities = {}
        while pending:
            questions = pending.pop()
            columns = [valid_items[int(key.split("_")[1])]["original_header"] for key in questions]
            scoped = self._restrict_evidence(state, [*axes, *columns])
            batch = {key: instructions[key] for key in questions}
            size = len(json.dumps([scoped, batch], ensure_ascii=False, default=str))
            if len(questions) > 1 and (len(questions) > 24 or size > 16000):
                middle = len(questions) // 2
                pending.extend((questions[middle:], questions[:middle]))
            elif size <= 16000:
                probabilities.update(judge_noul_batch(scoped, batch))
        return probabilities
    
    def _judge_mapped_columns(self, parsed_result: Dict[str, Any], table_data: Dict[str, Any],
                              pdf_context: Optional[Dict[str, Any]] = None,
                              known_variables: Optional[List[Dict[str, str]]] = None,
                              evidence=None, *, reviewed_columns=()) -> Dict[str, Any]:
        """Jev判卷门禁：对列名映射结果做批量置信度校验
        
        开启升级且关闭只复核时，低分映射先撤销，只有复判通过的提议可以入表。
        无法判卷、无法重映射或复判未通过的列暂缓输出，并记录供论文级复核的定位。
        关闭升级时保留原来的阈值分流和判卷不可用时的回退行为。
        """
        escalate = settings.JUDGE_ESCALATE and not settings.JUDGE_REVIEW_ONLY
        judge_available = judge_enabled()
        mapped = parsed_result.get("mapped_data") if isinstance(parsed_result, dict) else None
        if not isinstance(mapped, list):
            return parsed_result

        # 人工批准的 exclude 裁决在映射门生效：整列移除，不发表也不产生复核项
        # （与 workflow 单位路径的 COLUMN 键同构；置于判卷开关早退之前，judge off 也生效）
        decisions = getattr(self, "mapping_decisions", None) or {}
        file_path = table_data.get("file_path", "")
        sheet = table_data.get("sheet_name", "")
        if decisions and file_path and getattr(self, "mapping_digest", None):
            digest = self.mapping_digest(file_path)
            base = ("COLUMN", os.path.normcase(os.path.abspath(file_path)))
            kept = []
            for it in mapped:
                col = it.get("original_header", "") if isinstance(it, dict) else ""
                key = (*base, str(col).strip().casefold(), str(sheet).strip().casefold(), digest)
                d = decisions.get(key)
                if d and d.get("action") == "exclude" and d.get("evidence_status") == "verified":
                    applied = getattr(self, "applied_mapping_excludes", None)
                    if applied is not None:
                        applied.add(key)
                    logging.info("映射门[人工批准舍弃] 列'%s' 按 exclude 裁决移除", col)
                    continue
                kept.append(it)
            if len(kept) != len(mapped):
                parsed_result["mapped_data"] = kept
                mapped = kept
        if not judge_available and not escalate:
            return parsed_result
        
        valid_items = [it for it in mapped
                       if isinstance(it, dict) and it.get("standard_field") not in (None, "", "skip")
                       and it.get("original_header") not in reviewed_columns]
        if not valid_items:
            return parsed_result
        
        evidence = evidence if evidence is not None else build_table_evidence(table_data, pdf_context, known_variables)
        sample_rows = []
        for row in (table_data.get("data") or [])[:5]:
            if isinstance(row, dict):
                sample_rows.append({key: None if isna(row[key]) else row[key] for key in list(row)[:15]})

        state = {
            **evidence,
            "task": "珊瑚古气候数据表格的列名→标准字段映射校验",
            "sample_rows": sample_rows,
            "column_evidence": {col: evidence["column_evidence"][col]
                                for col in dict.fromkeys(item.get("original_header") for item in valid_items)
                                if col in evidence["column_evidence"]},
        }
        
        instructions = {}
        for i, item in enumerate(valid_items):
            instructions[f"col_{i}"] = (
                f"在珊瑚古气候数据表格中，把列 '{item.get('original_header')}'"
                f"（单位标注：{item.get('unit') or '无'}）映射到标准字段 "
                f"'{item.get('standard_field')}' 在语义上是正确的。"
            )
        
        probs = self._judge_column_batches(state, instructions, valid_items) if judge_available else {}
        if not probs and not escalate:
            return parsed_result
        
        pass_th = settings.JUDGE_PASS_THRESHOLD
        fail_th = settings.JUDGE_FAIL_THRESHOLD
        file_path = table_data.get("file_path", "")
        sheet = table_data.get("sheet_name", "")
        # 合法标准字段集合（校验升级重映射）：field_mapping值集 + 时间/特殊字段
        valid_fields = set(self.field_mapping.values()) | {
            "year", "age_bp", "age_ka_bp", "age_bce", "internal_chronology",
            "paleoData_dating_error", "skip"}

        # 第一遍：Jev 放行高置信映射，收集低分待处置项
        # 确定性置信地板：列名清晰指示所映射核心指标且非派生列时，不因 Jev 低分送复核
        # （解决"平滑但合法"列如 Sr/Ca-a.sam-4pt 被系统性误判 ~0.56 送人工的问题）
        n_pass = 0
        n_floor = 0
        pending = []  # (索引, item, jev概率)
        for i, item in enumerate(valid_items):
            p = probs.get(f"col_{i}")
            if p is None:
                if escalate:
                    pending.append((i, item, p))
            elif p >= pass_th:
                n_pass += 1
            elif is_confident_core_proxy(item.get("original_header", ""), item.get("standard_field")):
                n_floor += 1
                logging.info("判卷置信地板[放行] 列'%s'→%s jev=%.2f（清晰核心指标，豁免低分复核）",
                             item.get("original_header"), item.get("standard_field"), p)
            else:
                pending.append((i, item, p))

        # 升级前撤销待处理的原映射。只有复判通过的候选映射可以恢复输出。
        remaps = {}
        if escalate:
            for i, item, p in pending:
                item["_original_standard_field"] = item["standard_field"]
                item["standard_field"] = "skip"
                if p is None:
                    continue
                r = escalate_remap(state, item.get("original_header", ""), item.get("unit") or "",
                                   item["_original_standard_field"], p, pass_th, valid_fields)
                if r:
                    remaps[i] = r

        # 也复判 DeepSeek 的排除提议，确保这类列进入完整的论文复核路径。
        reinstructions = {}
        proposals = {}
        for i, (proposed, _reason) in remaps.items():
            if proposed == "skip":
                column = valid_items[i].get("original_header", "")
                reinstructions[f"col_{i}"] = f"根据原表和文献证据，列 '{column}' 不应映射到受支持的观测或绝对年代字段。"
                continue
            if proposed and proposed != "skip":
                it = valid_items[i]
                candidate = {**it, "standard_field": proposed}
                candidate.pop("unit", None)
                candidate.pop("unit_resolution", None)
                candidates = [candidate if item is it else dict(item) for item in mapped]
                self.apply_source_units({"mapped_data": candidates}, table_data, pdf_context, evidence)
                proposals[i] = candidate
                reinstructions[f"col_{i}"] = (
                    f"在珊瑚古气候数据表格中，把列 '{it.get('original_header')}'"
                    f"（单位标注：{candidate.get('unit') or '无'}）映射到标准字段 '{proposed}' 在语义上是正确的。")
        reprob = self._judge_column_batches(state, reinstructions, valid_items) if reinstructions else {}

        n_review = n_withheld = n_esc_ok = 0
        for i, item, p in pending:
            col = item.get("original_header", "")
            if escalate:
                orig_field = item["_original_standard_field"]
                proposed, reason = remaps.get(i, (None, ""))
                rp = reprob.get(f"col_{i}")
                if i in proposals and rp is not None and rp >= pass_th:
                    # 采用新字段的证据判定，不能继承原字段的单位或比例因子。
                    item.pop("unit", None)
                    item.pop("unit_resolution", None)
                    item.update(proposals[i])
                    n_esc_ok += 1
                    logging.info("判卷升级[重映射通过] 列'%s' %s→%s 复判=%.2f：%s",
                                 col, orig_field, proposed, rp, reason)
                    continue
                if p is None:
                    action, detail = "暂缓映射，初判缺失", "Jev 未返回此列初判分数"
                elif proposed is None:
                    action, detail = "暂缓映射，升级失败", "DeepSeek 未返回有效映射"
                elif proposed == "skip":
                    action, detail = "暂缓映射，升级建议排除", "DeepSeek 建议排除此列"
                else:
                    action = "暂缓映射，复判未通过"
                    detail = "Jev 未返回此列复判分数" if rp is None else "复判分数低于放行阈值"
                decision = f"qwen:{orig_field} / deepseek:{proposed or 'NA'} / 复判:{'%.3f' % rp if rp is not None else 'NA'}"
                self._add_judge_review(file_path, sheet, col, decision, p, action,
                                       f"{detail}。{reason}" if reason else detail,
                                       judge_original_field=orig_field, judge_proposed_field=proposed,
                                       judge_recheck_score=rp, source_header_line=table_data.get("source_header_line"),
                                       publication_doi=(table_data.get("archive_metadata") or {}).get("pub1_doi")
                                       or (pdf_context or {}).get("pub1_doi"))
                n_review += 1
                n_withheld += 1
                logging.info("判卷升级[%s] 列'%s' %s", action, col, decision)
                continue

            # 未启用升级时保留原有判卷模式。
            if p <= fail_th and not settings.JUDGE_REVIEW_ONLY:
                item["_original_standard_field"] = item["standard_field"]
                item["standard_field"] = "skip"
                n_withheld += 1
                action = "撤销映射"
            else:
                action = "判卷提示，保留映射" if settings.JUDGE_REVIEW_ONLY else "保留待复核"
            n_review += 1
            self._add_judge_review(file_path, sheet, col,
                                   f"-> {item.get('_original_standard_field') or item.get('standard_field')}",
                                   p, action, "列映射判卷置信度低于放行阈值")
            logging.info("判卷门禁[%s] 列'%s' 置信度%.2f", action, col, p)

        logging.info("判卷门禁汇总: 放行%d(含置信地板%d) / 升级接受%d / 送后续复核%d(其中暂缓%d)（共%d列）",
                     n_pass + n_floor, n_floor, n_esc_ok, n_review, n_withheld, len(valid_items))
        if n_esc_ok:
            self.apply_source_units(parsed_result, table_data, pdf_context, evidence)
        return parsed_result

    def _add_judge_review(self, file_path, sheet, column, decision, confidence, action, reason, **evidence):
        """统一写入判卷/升级复核队列项"""
        if evidence and file_path and os.path.isfile(file_path) and hasattr(self, "mapping_digest"):
            evidence["source_sha256"] = self.mapping_digest(file_path)
        add_review_item({
            "file": file_path,
            "sheet": sheet,
            "column": column,
            "decision": decision,
            "confidence": round(confidence, 3) if confidence is not None else None,
            "judge_model": settings.JUDGE_MODEL,
            "action": action,
            "reason": reason,
            **evidence,
        })

    def apply_source_units(self, parsed_result, table_data, pdf_context, evidence=None):
        """模型给出候选字段，单位由共同的证据规则判定。"""
        from .unit_converter import UnitConversionModule, resolve_unit

        body_fields = {
            "Sr/Ca_value": "sr_ca_unit",
            "δ18O_value": "d18o_unit",
            "δ13C_value": "d13c_unit",
            "sea_surface_temperature_value": "sst_unit",
        }
        context = pdf_context or {}
        unit_rows = {col.strip(): value for col, value in table_data.get("units", {}).items()}
        definitions = table_data.get("defined_units", {})
        variable_definitions = table_data.get("variable_definitions", {})
        fields = Counter(item.get("standard_field") for item in parsed_result.get("mapped_data", [])
                         if isinstance(item, dict))
        for item in parsed_result.get("mapped_data", []):
            if not isinstance(item, dict):
                continue
            field = item.get("standard_field")
            header = str(item.get("original_header") or "").strip()
            column = (evidence or {}).get("column_evidence", {}).get(header, {})
            if field == "year" and column.get("date_order"):
                archive_unit = (evidence or {}).get("archive_context", {}).get("source_time_unit")
                bases = {time_basis(label) for label in (header, column.get("unit_row"), column.get("archive_unit"), archive_unit)}
                if bases <= {None, "year"}:
                    item["unit"] = "CE"
            if field not in UnitConversionModule.default_units:
                continue
            key = body_fields.get(field)
            source = column.get("definition_source") or variable_definitions.get(header, {}).get("source", "database_definition")
            quote = context.get("metadata_evidence", {}).get(key)
            use_body = bool(key and (fields[field] == 1 or quote_names_column(quote, header)))
            decision = resolve_unit(
                field, header,
                (("unit_row", column.get("unit_row", unit_rows.get(header))),
                 (source, column.get("archive_unit", definitions.get(header)))),
                context.get(f"default_{key}") if use_body else None,
                quote if use_body else None,
                table_note=table_data.get("table_caption", "") if fields[field] == 1 else "",
            )
            item["unit"] = decision["unit"] or "unknown"
            item["unit_resolution"] = decision
        return parsed_result

    def _is_author_year_citation_table(self, headers: List[str]) -> bool:
        """
        检测是否为“作者+年份”格式的引用表
        这种表通常是文献引用汇总，不包含实际的气候数据
        
        参数:
            headers: 表格的列名列表
            
        返回:
            True 表示是引用表，False 表示不是
        """
        import re
        
        # 检测模式：作者名 + 年份，如 "Smith et al. 2020"、"Jones (1998)"、"Li and Wang 2015"
        author_year_patterns = [
            r'\w+\s+et\s+al\.?\s*[\(\[]?\d{4}[\)\]]?',  # Smith et al. 2020
            r'\w+\s*[\(\[]\d{4}[\)\]]',  # Smith (2020)
            r'\w+\s+and\s+\w+\s*[\(\[]?\d{4}[\)\]]?',  # Smith and Jones 2020
            r'\w+\s*,\s*\d{4}',  # Smith, 2020
            r'[A-Z][a-z]+\s+\d{4}',  # Smith 2020
        ]
        
        # 统计匹配的列数
        matched_count = 0
        for header in headers:
            header_str = str(header).strip()
            for pattern in author_year_patterns:
                if re.search(pattern, header_str, re.IGNORECASE):
                    matched_count += 1
                    break
        
        # 如果超过50%的列匹配了作者+年份格式，则认为是引用表
        if len(headers) > 0 and matched_count / len(headers) > 0.5:
            logging.debug(f"检测到 {matched_count}/{len(headers)} 列匹配作者+年份格式，判定为引用表")
            return True
        
        return False
    
    def _generate_parsing_prompt(self, table_data: Dict[str, Any], context: str = "", pdf_context: Optional[Dict[str, Any]] = None, known_variables: Optional[List[Dict[str, str]]] = None, evidence=None) -> str:
        """生成大模型解析提示词"""
        evidence = evidence if evidence is not None else build_table_evidence(table_data, pdf_context, known_variables)
        # 【优化】使用更多样本（20行）提高时间分辨率判断准确性
        # 【修复】限制数据量：只取前20行，且每行只保留前10列的数据
        raw_sample_data = table_data.get("data", [])[:20]  # 取前20行作为示例
        
        # 【修复】简化样本数据，只保留前10列，避免Token超限
        MAX_COLS_IN_SAMPLE = 10
        sample_data = []
        for row in raw_sample_data:
            if isinstance(row, dict):
                # 如果是字典，只取前10个键值对
                simplified_row = {}
                local = ((k, v) for k, v in row.items() if k in evidence["column_evidence"])
                for i, (k, v) in enumerate(local):
                    if i >= MAX_COLS_IN_SAMPLE:
                        break
                    # 对于数值，保留4位小数以减少长度
                    if isinstance(v, float):
                        simplified_row[k] = round(v, 4)
                    else:
                        simplified_row[k] = v
                sample_data.append(simplified_row)
            elif isinstance(row, (list, tuple)):
                # 如果是列表，只取前10个元素
                simplified_row = list(row[:MAX_COLS_IN_SAMPLE])
                sample_data.append(simplified_row)
            else:
                sample_data.append(row)
        
        # 【优化】过滤pdf_context，只保留关键元数据，避免Token超限
        filtered_pdf_context = {}
        if pdf_context:
            # 关键字段白名单
            keep_keys = [
                'geo_siteName', 'geo_latitude', 'geo_longitude', 'geo_ocean', 'geo_reefType',
                'base_year', 'measurement_year', 'paleoData_dating_method',
                'default_age_unit', 'default_sr_ca_unit', 'default_d18o_unit', 'default_sst_unit',
                'pub1_doi', 'pub1_authors', 'pub1_year'
            ]
            for key in keep_keys:
                if key in pdf_context:
                    filtered_pdf_context[key] = pdf_context[key]
            
            # 保留其他非长文本字段
            for k, v in pdf_context.items():
                if k not in keep_keys and isinstance(v, (str, int, float, bool)) and len(str(v)) < 100:
                     filtered_pdf_context[k] = v

        prompt = f"""
你负责识别珊瑚骨骼古气候表格的列含义。保留原始列名，提供映射候选和表中能找到的元数据。

- 先分辨观测表、元数据页、引用表、实验表和模型结果。只有证据明确显示硬海绵、泥炭、海水、组织或共生藻等材料时，才将整个非骨骼表标为 is_coral_data=false。表中同时存在不同材料时，逐列标记 skip。
- SST 仅指明确标为海表温度的观测或重建。实验室、培养、仪器、空气温度及含义未明的温度列标记为 skip。
- 测量误差、距平、平滑值、残差、海水同位素及模型拟合值用 skip。测年误差单列映射为 paleoData_dating_error。作者明确发布的多岩芯复合序列与组成岩芯分别映射，保留原始列名。单一岩芯同时有观测值与插值或汇总值时，采用原始观测；档案仅发布经处理序列时保留发布列。
- 从原表列名、单位行或能准确对应的变量定义转录单位。正文只在明确说明同一指标时可作补充。单位缺失写 unknown，保留 VPDB、VSMOW 等标尺。请保留原始单位，由程序换算并处理冲突。
- 绝对年代依据源表声明识别。CE/AD 映射为 year，年 BP 为 age_bp，千年 BP 为 age_ka_bp，BCE/BC 为 age_bce。标出全部明确候选，排除相对年代、年代上下界与实验日期。内部年表按源文说明标记为 internal_chronology。数字的大小与是否带小数都不能判定年代基准、单位或采样间隔。
- 表注如果明确写出年度均值、季节、拼接或插值，保留其原义。采样间隔由程序根据完整时间轴判定。
- 地点、物种、测年方法和分析方法仅从明确给出的信息转录。证据不足时保留空值。

标准字段映射字典：
{json.dumps(self.field_mapping, ensure_ascii=False, indent=2)}

全新世时段映射：
{json.dumps(self.holocene_stage_mapping, ensure_ascii=False, indent=2)}

请严格按照以下JSON格式返回结果：
{{
    "mapped_data": [
        {{
            "original_header": "原始列名",
            "standard_field": "标准字段名",
            "unit": "单位（如果列名中没有单位，请尝试从PDF上下文获取，否则返回'unknown'）",
            "description": "字段描述",
            "is_bp_format": "如果是BP格式时间列，返回true；否则返回false",
            "is_internal_chronology": "如果是内部年表值（Internal Chronology），返回true；否则返回false。内部年表值表示相对于基点的年数，需要与基点年龄结合计算",
            "bp_base_age": "如果是BP格式，返回基点年龄（珊瑚BP年龄，如果有，如123.9表示123.9 ka BP）；否则返回null",
            "bp_unit": "如果是BP格式，返回单位类型：'year'（年）或'ka'（千年）；否则返回null"
        }}
    ],
    "table_metadata": {{
        "resolution": "源材料明确声明的分辨率，未知则为 unknown",
        "is_interpolated": "是否为插值数据（true/false）",
        "measurement_year": "测量年份（如果有，如2014表示AD 2014）；否则返回null",
        "data_type": "数据类型描述",
        "is_coral_data": "是否可能包含珊瑚骨骼记录（true/false）。仅有明确的非骨骼材料证据时返回 false"
    }},
    "complemented_metadata": {{
        "geo_siteName": "采样地点名称",
        "geo_latitude": "纬度",
        "geo_longitude": "经度",
        "paleoData_dating_method": "测年方法",
        "paleoData_dating_error": "测年误差",
        "paleoData_holocene_stage": "全新世时段",
        "testing_method": "检测方法",
        "missing_units": ["如果某些列名没有单位且PDF上下文中也无法获取，请在这里列出对应的列名"],
        "missing_fields": ["缺失的字段列表"]
    }},
    "validation_flags": {{
        "outlier_flag": 0,
        "outlier_reason": "异常原因说明"
    }}
}}

表格信息：
- 表名: {table_data.get('sheet_name', 'unknown')}
- 来源说明: {context[:500] if context else '无'}
- 如果证据含 target_columns，只映射这些列并逐列返回；其他列只用于理解。
- 按表和列整理的原始证据: {json.dumps(evidence, ensure_ascii=False, default=str)}
- 前{len(sample_data)}行示例数据: {json.dumps(sample_data, ensure_ascii=False)[:5000]}

PDF元数据（如果列名中没有单位，可从这里获取缺失单位）：
{json.dumps(filtered_pdf_context, ensure_ascii=False, indent=2) if filtered_pdf_context else "无"}

请开始分析：
"""
        return prompt
    
    def _default_parsing(self, table_data: Dict[str, Any], pdf_context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """默认解析方法（当大模型调用失败时使用）"""
        headers = table_data.get("headers", [])
        sample_data = table_data.get("data", [])[:5]  # 取前5行作为示例
        mapped_data = []
        
        # 定义需要排除的列名关键词（包含特殊处理含义）
        # 注意：这是默认解析使用的关键词，LLM解析使用Prompt中的指令
        exclude_keywords = [
            "offset",  # 偏移量处理
            "Spliced",   # 拼接数据
            "Composite",  # 组合数据
            "stack",      # 堆叠数据
            "average",    # 平均数据
            "mean",       # 平均数据
            "anom",       # 距平
            "residual",   # 残差
            "relative",   # 相对年龄（需要绝对年龄，不要相对年龄）
            "相对年龄"     # 相对年龄（中文）
        ]
        
        # 简单的字段映射
        for header in headers:
            # 【新增】第一步：检查是否包含需要排除的关键词
            # 忽略大小写比较
            header_lower = header.lower()
            
            # 【修复】Δδ18O (氧同位素差值) 是有意义的指标，不应被排除
            # 虽然包含 "diff" 或 "difference"，但它是原始测量数据，不是二次处理数据
            is_d18o_difference = (
                "δ18o" in header_lower and "difference" in header_lower or
                "δ18o" in header_lower and "diff" in header_lower or
                "Δδ18O" in header or "Δ18O" in header or
                "d18o" in header_lower and "difference" in header_lower
            )
            
            # 只有不是 δ18O_difference 的情况下才检查排除关键词
            should_skip = False
            if not is_d18o_difference:
                should_skip = any(keyword.lower() in header_lower for keyword in exclude_keywords)
            if (should_skip and not is_derived_observation_column(header)
                    and not ERROR_COLUMN_RE.search(header_lower)
                    and not re.search(r"\brelative\b|相对年龄", header_lower)
                    and composite_evidence(header, paper=(pdf_context or {}).get("_pdf_text") or "",
                                           caption=table_data.get("table_caption") or "")):
                should_skip = False
            
            if should_skip:
                logging.info(f"跳过列名: {header} (包含特殊处理含义)")
                continue  # 直接跳过该列，不进行任何处理
            
            # 先直接查找映射
            standard_field = self.field_mapping.get(header, None)
            if standard_field is None and re.fullmatch(r"(?:AD|CE|BC|BCE)(?: \[column \d+\])?", header, re.I):
                standard_field = time_basis(header)
            
            # 如果没有直接映射，尝试使用正则表达式模式匹配
            if standard_field is None:
                for field_type, pattern in PATTERNS.items():
                    # 跳过水文信号的正则匹配，因为它们需要特殊处理
                    if field_type == "hydrological_signal" and ("δ¹⁸Osw-wet" in header or "δ18Osw-dry" in header):
                        standard_field = "hydrological_signal"
                        # 对于水文信号，不提取年份和其他水文数据
                        break
                    elif pattern.match(header):
                        standard_field = field_type
                        break
            
            # 如果仍然没有匹配，尝试模糊匹配
            if standard_field is None:
                # 检查是否包含已知的字段名
                if "Sr/Ca" in header:
                    standard_field = "Sr/Ca_value"
                elif "Ba/Ca" in header:
                    standard_field = "Ba/Ca_value"
                elif "δ18O" in header and "Δδ18O" not in header and "δ¹⁸Osw-wet" not in header and "δ18Osw-dry" not in header:  # 确保不是Δδ18O且不是水文信号
                    standard_field = "δ18O_value"
                elif "Δδ18O" in header or "δ18O" in header and "difference" in header.lower():
                    standard_field = "δ18O_difference"
                elif "δ13C" in header:
                    standard_field = "δ13C_value"
                elif not is_d18o_difference and is_confident_core_proxy(header, "δ18O_value"):
                    standard_field = "δ18O_value"
                elif is_confident_core_proxy(header, "δ13C_value"):
                    standard_field = "δ13C_value"
                elif re.search(r"\b(?:year|age|date|yr)\b|年份|年代|日期", header, re.I):
                    # 【新增】检查是否为相对年龄，如果是则跳过
                    header_lower = header.lower()
                    if any(kw in header_lower for kw in ["relative", "相对年龄", "relative age"]):
                        logging.info(f"跳过相对年龄列: {header} (需要绝对年龄)")
                        continue  # 直接跳过相对年龄列
                    
                    # 【修复】检查是否为BP格式，如果是则标记为age_ka_bp
                    header_upper = header.upper()
                    if any(kw in header_upper for kw in ["BP", "BEFORE PRESENT", "KA BP"]):
                        standard_field = "age_ka_bp"  # BP格式标记为age_ka_bp，后续转换为CE
                    elif "CHRON" in header_upper and "[A]" in header_upper:
                        # Int chron [a] 格式：内部年表值，单位是年（[a]=annum），需配合基点年龄转换为CE
                        standard_field = "age_ka_bp"
                    else:
                        standard_field = "year"  # CE/AD格式
                elif "纬度" in header or "latitude" in header.lower():
                    standard_field = "geo_latitude"
                elif "经度" in header or "longitude" in header.lower():
                    standard_field = "geo_longitude"
                elif "site" in header.lower() or "采样点" in header or "地点" in header:
                    standard_field = "geo_siteName"
                # 水文信号特殊处理：δ¹⁸Osw-wet与δ18Osw-dry不提取时间和水文数据
                elif "δ¹⁸Osw-wet" in header or "δ18Osw-dry" in header:
                    standard_field = "hydrological_signal"
                    # 对于水文信号，不提取年份和其他水文数据
                    continue  # 跳过水文信号列的处理
                # 已移除非必要字段的模糊匹配逻辑
                elif "dating" in header.lower() or "测年" in header:
                    standard_field = "paleoData_dating_method"
                elif "holocene" in header.lower() or "全新世" in header:
                    standard_field = "paleoData_holocene_stage"
                else:
                    standard_field = header  # 如果没有映射则保持原名
            
            unit = canonical_unit(source_unit(header)) or "unknown"

            # 构建 mapped_data
            mapped_data.append({
                "original_header": header,
                "standard_field": standard_field,
                "unit": unit,
                "description": f"从列名 '{header}' 映射的字段"
            })
        
        # 【日志】批量汇总默认解析的映射结果
        if mapped_data:
            logging.info(f"\n{'='*60}")
            logging.info(f"【附件数据-默认解析】列名映射汇总")
            logging.info(f"总列数: {len(mapped_data)}")
            logging.info(f"{'-'*60}")
            for item in mapped_data:
                original_header = item.get('original_header', 'unknown')
                standard_field = item.get('standard_field', 'unknown')
                unit = item.get('unit', 'unknown')
                logging.info(f"  '{original_header}' -> {standard_field} ({unit})")
            logging.info(f"{'='*60}\n")
        
        # 构造默认的补充元数据
        complemented_metadata = {
            "geo_siteName": "",
            "geo_latitude": None,
            "geo_longitude": None,
            "paleoData_dating_method": None,
            "paleoData_dating_error": None,
            "paleoData_holocene_stage": None,
            "testing_method": "",
            "missing_fields": []
            # 已移除非必要字段
        }
        
        # 若单位仍为 unknown，则标记需要大模型从PDF提取的信息补充
        missing_units_headers = [item["original_header"] for item in mapped_data if item["unit"] == "unknown"]
        if missing_units_headers:
            complemented_metadata["missing_units"] = missing_units_headers
        
        # 如果提供了PDF上下文信息，则补充元数据
        if pdf_context:
            # 补充地理位置信息
            if pdf_context.get("geo_siteName"):
                complemented_metadata["geo_siteName"] = pdf_context["geo_siteName"]
            
            if pdf_context.get("geo_latitude") is not None:
                complemented_metadata["geo_latitude"] = pdf_context["geo_latitude"]
            
            if pdf_context.get("geo_longitude") is not None:
                complemented_metadata["geo_longitude"] = pdf_context["geo_longitude"]
            
            # 补充测年方法和误差信息
            if pdf_context.get("paleoData_dating_method"):
                complemented_metadata["paleoData_dating_method"] = pdf_context["paleoData_dating_method"]
            
            if pdf_context.get("paleoData_dating_error"):
                complemented_metadata["paleoData_dating_error"] = pdf_context["paleoData_dating_error"]
            
            # 补充全新世时段
            if pdf_context.get("paleoData_holocene_stage"):
                complemented_metadata["paleoData_holocene_stage"] = pdf_context["paleoData_holocene_stage"]
            
            # 补充检测方法
            if pdf_context.get("testing_method"):
                complemented_metadata["testing_method"] = pdf_context["testing_method"]
            
            # 补充DOI和作者信息
            if pdf_context.get("pub1_doi"):
                complemented_metadata["pub1_doi"] = pdf_context["pub1_doi"]
            
            if pdf_context.get("pub1_authors"):
                complemented_metadata["pub1_authors"] = pdf_context["pub1_authors"]
        
        return {
            "mapped_data": mapped_data,
            "table_metadata": {
                "resolution": "unknown",
                "is_interpolated": False,
                "data_type": "unknown"
            },
            "complemented_metadata": complemented_metadata,
            "validation_flags": {
                "outlier_flag": 0,
                "outlier_reason": ""
            }
        }
    
    def _apply_pdf_context(self, parsed_result: Dict[str, Any], pdf_context: Dict[str, Any]) -> Dict[str, Any]:
        """
        应用 PDF 上下文信息到解析结果中
        
        Args:
            parsed_result: LLM 解析结果
            pdf_context: PDF 上下文信息
            
        Returns:
            补充后的解析结果
        """
        if not pdf_context:
            return parsed_result
        
        complemented_metadata = parsed_result.get("complemented_metadata", {})
        
        # 补充地理位置信息
        if "geo_siteName" not in complemented_metadata or not complemented_metadata["geo_siteName"]:
            complemented_metadata["geo_siteName"] = pdf_context.get("geo_siteName", "")
        
        if "geo_latitude" not in complemented_metadata or not complemented_metadata["geo_latitude"]:
            complemented_metadata["geo_latitude"] = pdf_context.get("geo_latitude")
        
        if "geo_longitude" not in complemented_metadata or not complemented_metadata["geo_longitude"]:
            complemented_metadata["geo_longitude"] = pdf_context.get("geo_longitude")
        
        # 补充测年方法和误差信息
        if "paleoData_dating_method" not in complemented_metadata or not complemented_metadata["paleoData_dating_method"]:
            complemented_metadata["paleoData_dating_method"] = pdf_context.get("paleoData_dating_method")
        
        if "paleoData_dating_error" not in complemented_metadata or not complemented_metadata["paleoData_dating_error"]:
            complemented_metadata["paleoData_dating_error"] = pdf_context.get("paleoData_dating_error")
        
        # 补充全新世时段
        if "paleoData_holocene_stage" not in complemented_metadata or not complemented_metadata["paleoData_holocene_stage"]:
            complemented_metadata["paleoData_holocene_stage"] = pdf_context.get("paleoData_holocene_stage")
        
        # 补充检测方法
        if "testing_method" not in complemented_metadata or not complemented_metadata["testing_method"]:
            complemented_metadata["testing_method"] = pdf_context.get("testing_method")
        
        parsed_result["complemented_metadata"] = complemented_metadata
        return parsed_result
    
    def detect_outliers_in_table(self, table_data: List[Dict[str, Any]], 
                                  table_metadata: Dict[str, Any],
                                  holocene_stage: str = None) -> List[Dict[str, Any]]:
        """
        在单张表格内检测异常值（在解析后立即进行）
        这是"论文附件级别"的异常值检测，充分利用时间序列上下文
        
        Args:
            table_data: 已解析的表格数据（多行时间序列）
            table_metadata: 表格元数据，包含分辨率等信息
            holocene_stage: 全新世时段
            
        Returns:
            标记了异常值的表格数据
        """
        if not table_data or len(table_data) < 3:
            # 数据太少，无法进行统计检测
            return table_data
        
        # 按年份排序（保证时间顺序）
        try:
            sorted_table = sorted(table_data, key=lambda x: float(x.get('year', 0)) if x.get('year') else 0)
        except (ValueError, TypeError):
            sorted_table = table_data
        
        # 对每个指标字段进行检测
        indicator_fields = ['Sr/Ca_value', 'δ18O_value', 'δ13C_value']
        
        for field in indicator_fields:
            # 提取该字段的值
            values = []
            valid_indices = []
            for idx, row in enumerate(sorted_table):
                try:
                    val = row.get(field)
                    if val is not None:
                        val_float = float(val)
                        values.append(val_float)
                        valid_indices.append(idx)
                except (ValueError, TypeError):
                    pass
            
            if len(values) < 3:
                continue  # 数据太少，跳过
            
            # 【IQR 法检测】
            try:
                q1 = np.percentile(values, 25)
                q3 = np.percentile(values, 75)
                iqr = q3 - q1
                
                # 根据全新世时段设置不同的倍数
                multiplier = 2.0 if holocene_stage in ["早全新世", "中全新世"] else 1.5
                lower = q1 - multiplier * iqr
                upper = q3 + multiplier * iqr
                
                # 标记异常值
                for i, val in enumerate(values):
                    row_idx = valid_indices[i]
                    if val < lower or val > upper:
                        # 标记为异常
                        sorted_table[row_idx].setdefault('qc_outlier_fields', []).append(field)
                        sorted_table[row_idx]['qc_outlier_flag'] = 1
                        sorted_table[row_idx]['qc_outlier_reason'] = (
                            f"{field}为IQR法异常值: {val:.4f} "
                            f"(范围:[{lower:.4f}, {upper:.4f}], IQR={iqr:.4f})"
                        )
                        sorted_table[row_idx]['qc_outlier_method'] = 'IQR'
                        logging.debug(
                            f"检测到异常值 - {field}: {val:.4f}, "
                            f"范围: [{lower:.4f}, {upper:.4f}]"
                        )
                    else:
                        # 标记为正常（避免被后续流程误判）
                        if 'qc_outlier_flag' not in sorted_table[row_idx]:
                            sorted_table[row_idx]['qc_outlier_flag'] = 0
            except Exception as e:
                logging.warning(f"IQR检测异常: {str(e)}")
        
        return sorted_table
