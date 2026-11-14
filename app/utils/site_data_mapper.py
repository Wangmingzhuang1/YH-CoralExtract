#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
研究地点与数据映射模块
利用大模型分析文献全文和表格结构，建立"样品/站点 - 研究地点 - 数据列"的映射关系
实现高效的数据点与研究地点的匹配
"""

import logging
import json
from typing import Dict, List, Any, Optional, Tuple

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def build_site_data_mapping(
    pdf_text: str,
    table_headers: List[str],
    research_sites: Dict[str, Dict[str, float]],
    sample_data_preview: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    使用大模型分析文献全文和表格结构，建立"样品/站点 - 研究地点 - 数据列"的映射关系
    
    Args:
        pdf_text: PDF/MD文本内容（已阅读全文）
        table_headers: 表格列名列表
        research_sites: 研究地点信息字典，格式：{"站点ID": {"latitude": 纬度, "longitude": 经度}, ...}
        sample_data_preview: 数据预览（可选，包含前几行数据用于上下文分析）
        
    Returns:
        Dict[str, Any]: 映射关系字典，包含：
            - column_site_mapping: 列名到站点ID的映射 {"列名": "站点ID"}
            - sample_site_mapping: 样品标识到站点ID的映射 {"样品ID": "站点ID"}
            - year_range_mapping: 年份区间到站点ID的映射 {"年份区间": "站点ID"}（可选）
            - default_site: 默认站点ID（如果只有一个站点）
    """
    # 如果只有一个研究地点，直接返回单地点映射
    if len(research_sites) == 1:
        site_id = list(research_sites.keys())[0]
        return {
            "default_site": site_id,
            "column_site_mapping": {},
            "sample_site_mapping": {},
            "mapping_strategy": "single_site"
        }
    
    # 如果有多个研究地点，使用大模型分析
    try:
        from app.utils.llm_api import call_llm
        
        # 【优化】构建更详细的数据预览文本（增加行数和字段数）
        data_preview_text = ""
        if sample_data_preview:
            preview_items = []
            # 增加字段数量，显示所有关键字段
            for key, value in list(sample_data_preview.items())[:20]:  # 从10个增加到20个字段
                # 限制值长度，避免过长
                value_str = str(value)
                if len(value_str) > 50:
                    value_str = value_str[:50] + "..."
                preview_items.append(f"  {key}: {value_str}")
            data_preview_text = "\n".join(preview_items)
        
        # 【优化】构建更详细的研究地点信息文本（包含更多上下文）
        sites_info_text = "\n".join([
            f"  - 站点ID: {site_id}, 纬度: {info['latitude']}, 经度: {info['longitude']}"
            for site_id, info in research_sites.items()
        ])
        
        # 【优化】构建更详细的列名列表文本（包含列名索引和分组信息）
        headers_text = ""
        if table_headers:
            # 按类型分组列名（包含站点标识的、包含指标的、其他）
            site_related_headers = []
            indicator_headers = []
            other_headers = []
            
            for idx, header in enumerate(table_headers[:50]):  # 最多显示50个列名
                header_str = str(header)
                # 检查是否包含站点标识
                has_site_id = any(site_id in header_str for site_id in research_sites.keys())
                # 检查是否包含指标关键词
                has_indicator = any(keyword in header_str.lower() for keyword in ['sr/ca', 'δ18o', 'δ13c', 'sst', 'temperature'])
                
                if has_site_id:
                    site_related_headers.append(f"    [{idx}] {header_str}")
                elif has_indicator:
                    indicator_headers.append(f"    [{idx}] {header_str}")
                else:
                    other_headers.append(f"    [{idx}] {header_str}")
            
            headers_text = "列名列表（按类型分组）：\n"
            if site_related_headers:
                headers_text += "  【包含站点标识的列名】：\n" + "\n".join(site_related_headers[:20]) + "\n"
            if indicator_headers:
                headers_text += "  【包含指标关键词的列名】：\n" + "\n".join(indicator_headers[:20]) + "\n"
            if other_headers:
                headers_text += "  【其他列名】：\n" + "\n".join(other_headers[:10]) + "\n"
        else:
            headers_text = "  无列名信息"
        
        # 【修复】不需要传入PDF全文，站点映射只需要列名和数据预览
        # 站点ID已经在research_sites中，只需要匹配列名中是否包含这些ID
        
        prompt = f"""
你是一个专业的古气候数据分析专家。请分析表格列名和数据预览，建立"数据列/样品标识 - 研究地点"的映射关系。

【任务目标】
识别表格列名中是否包含站点标识，建立列名与研究地点的对应关系。

【已知信息】

1. 研究地点列表（每个地点有唯一标识和经纬度）：
{sites_info_text}

2. 表格列名列表：
{headers_text}

3. 数据预览（前几行数据的关键字段）：
{data_preview_text}

【分析要求】

请仔细分析以下内容，建立准确的映射关系：

1. **列名分析**：
   - 识别哪些列名包含样品/站点标识（如"15WC24_Sr/Ca"、"SiteA_δ18O"、"PM1 Sr/Ca (mmol/mol)"等）
   - 注意列名可能包含样品编号前缀（如"PM"、"E17"、"11LW2"等）
   - 注意列名可能包含单位信息（如"(mmol/mol)"、"(‰ VPDB)"等），需要区分站点标识和单位
   - 对于包含站点标识的列名，提取站点标识并匹配到对应的研究地点

2. **数据值分析**：
   - 识别哪些数据值包含样品/站点标识（如数据行中的"15WC24"、"11LW2"、"现代珊瑚"等）
   - 注意数据值可能出现在任意列中（如"Sample"列、"Site"列、"ID"列等）
   - 注意数据值可能包含部分站点标识（如"15WC"匹配"15WC24"）

3. **年份区间分析**：
   - 识别是否有年份区间对应不同站点的情况（如"1900-1950"对应站点A，"1950-2000"对应站点B）
   - 注意年份区间可能以不同格式出现（如"1900-1950"、"1900 to 1950"、"1900~1950"等）

4. **列名与站点的对应关系**：
   - 仔细比对列名中的站点标识与已知研究地点的标识
   - 注意站点标识可能有多种变体（如大小写、缩写、全称等）
   - 如果列名中的标识与已知站点标识不完全一致，尝试模糊匹配

【输出要求】

请以严格的JSON格式返回结果，必须包含以下字段：

{{
  "column_site_mapping": {{
    "完整列名1": "站点ID1",
    "完整列名2": "站点ID2",
    ...
  }},
  "sample_site_mapping": {{
    "样品标识1": "站点ID1",
    "样品标识2": "站点ID2",
    ...
  }},
  "year_range_mapping": {{
    "年份区间1": "站点ID1",
    "年份区间2": "站点ID2",
    ...
  }},
  "mapping_strategy": "映射策略",
  "confidence": "置信度",
  "mapping_details": {{
    "column_matches": [
      {{"column": "列名", "site_id": "站点ID", "match_type": "匹配类型", "confidence": "置信度"}}
    ],
    "sample_matches": [
      {{"sample_id": "样品标识", "site_id": "站点ID", "match_type": "匹配类型", "confidence": "置信度"}}
    ],
    "year_range_matches": [
      {{"range": "年份区间", "site_id": "站点ID", "match_type": "匹配类型", "confidence": "置信度"}}
    ]
  }}
}}

【字段说明】

1. **column_site_mapping**: 列名中包含站点标识的情况
   - 键：完整的列名（如"15WC24_Sr/Ca (mmol/mol)"）
   - 值：对应的站点ID（必须与已知研究地点列表中的ID完全一致）

2. **sample_site_mapping**: 数据值中包含站点标识的情况
   - 键：样品标识（如"15WC24"、"11LW2"、"现代珊瑚"等）
   - 值：对应的站点ID

3. **year_range_mapping**: 年份区间对应不同站点的情况（可选）
   - 键：年份区间字符串（如"1900-1950"）
   - 值：对应的站点ID

4. **mapping_strategy**: 映射策略
   - "column_based": 主要基于列名匹配
   - "sample_based": 主要基于样品标识匹配
   - "year_range_based": 主要基于年份区间匹配
   - "mixed": 混合策略

5. **confidence**: 整体置信度
   - "high": 高置信度（映射关系明确，有充分依据）
   - "medium": 中等置信度（映射关系较明确，但可能有部分不确定）
   - "low": 低置信度（映射关系不明确，需要人工审查）

6. **mapping_details**: 详细的匹配信息（新增）
   - 记录每个匹配的详细信息，包括匹配类型和置信度
   - 便于后续验证和调试

【重要提示】

1. **准确性优先**：如果无法确定映射关系，请将对应字段设为空对象 {{}}，不要猜测
2. **站点ID必须精确匹配**：返回的站点ID必须与已知研究地点列表中的ID完全一致
3. **多种匹配策略**：可以同时使用多种匹配策略（列名、样品标识、年份区间）
4. **置信度评估**：根据匹配依据的充分程度评估置信度
5. **只返回JSON**：只返回JSON格式的结果，不要包含其他文本或说明

请开始分析：
"""
        
        result = call_llm("max", prompt)
        if result:
            # 清理可能的Markdown代码块标记
            result = result.strip()
            if result.startswith("```json"):
                result = result[8:]
            elif result.startswith("```"):
                result = result.split("```", 1)[1] if "```" in result else result
            if result.endswith("```"):
                result = result[:-3]
            result = result.strip()
            
            # 解析JSON结果
            try:
                mapping_result = json.loads(result)
                logger.info(f"成功建立站点-数据映射关系，策略: {mapping_result.get('mapping_strategy', 'unknown')}")
                return mapping_result
            except json.JSONDecodeError as e:
                logger.warning(f"大模型返回的JSON格式错误: {str(e)}")
                logger.debug(f"原始返回内容: {result[:500]}")
    except Exception as e:
        logger.warning(f"使用大模型建立映射关系失败: {str(e)}")
    
    # 如果大模型分析失败，回退到基于规则的匹配
    return _build_mapping_with_rules(table_headers, research_sites, sample_data_preview)


def _build_mapping_with_rules(
    table_headers: List[str],
    research_sites: Dict[str, Dict[str, float]],
    sample_data_preview: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    使用基于规则的匹配方法建立映射关系（回退方法）
    【优化】增强匹配能力：支持部分匹配、正则表达式匹配、大小写不敏感匹配
    
    Args:
        table_headers: 表格列名列表
        research_sites: 研究地点信息字典
        sample_data_preview: 数据预览
        
    Returns:
        Dict[str, Any]: 映射关系字典
    """
    import re
    
    column_site_mapping = {}
    sample_site_mapping = {}
    site_ids = list(research_sites.keys())
    
    # 【优化】1. 从列名中提取站点标识（增强匹配能力）
    for header in table_headers:
        if not isinstance(header, str):
            continue
        
        header_lower = header.lower()
        matched_site_id = None
        match_type = None
        
        # 策略1：精确匹配（最高优先级）
        for site_id in site_ids:
            if site_id in header:
                matched_site_id = site_id
                match_type = "exact"
                break
        
        # 策略2：大小写不敏感匹配
        if not matched_site_id:
            for site_id in site_ids:
                if site_id.lower() in header_lower:
                    matched_site_id = site_id
                    match_type = "case_insensitive"
                    break
        
        # 策略3：部分匹配（站点ID是列名的子串，且长度>=3）
        if not matched_site_id:
            for site_id in site_ids:
                if len(site_id) >= 3 and site_id in header:
                    # 检查是否是完整的单词或标识符（避免误匹配）
                    # 使用正则表达式匹配，确保站点ID前后是单词边界或非字母数字字符
                    pattern = re.compile(r'[^a-zA-Z0-9]' + re.escape(site_id) + r'[^a-zA-Z0-9]|^' + re.escape(site_id) + r'[^a-zA-Z0-9]|[^a-zA-Z0-9]' + re.escape(site_id) + r'$|^' + re.escape(site_id) + r'$', re.IGNORECASE)
                    if pattern.search(header):
                        matched_site_id = site_id
                        match_type = "partial"
                        break
        
        # 策略4：正则表达式匹配（支持站点ID的变体）
        if not matched_site_id:
            for site_id in site_ids:
                # 构建正则表达式，支持站点ID的常见变体
                # 例如："15WC24" 可以匹配 "15WC24"、"15-WC-24"、"15_WC_24" 等
                site_id_escaped = re.escape(site_id)
                # 允许在数字和字母之间插入连字符、下划线等
                pattern_str = site_id_escaped.replace(r'(\d)', r'[\s\-_]?\d').replace(r'([A-Z])', r'[\s\-_]?[A-Z]')
                pattern = re.compile(pattern_str, re.IGNORECASE)
                if pattern.search(header):
                    matched_site_id = site_id
                    match_type = "regex"
                    break
        
        if matched_site_id:
            column_site_mapping[header] = matched_site_id
            logger.debug(f"列名匹配成功: {header} -> {matched_site_id} (匹配类型: {match_type})")
    
    # 【优化】2. 从数据预览中提取站点标识（增强提取能力）
    if sample_data_preview:
        for key, value in sample_data_preview.items():
            if not isinstance(value, str):
                continue
            
            value_str = str(value).strip()
            if not value_str:
                continue
            
            # 策略1：精确匹配
            for site_id in site_ids:
                if site_id in value_str:
                    sample_site_mapping[value_str] = site_id
                    logger.debug(f"样品标识匹配成功: {value_str} -> {site_id} (来源字段: {key})")
                    break
            
            # 策略2：大小写不敏感匹配
            if value_str not in sample_site_mapping:
                value_lower = value_str.lower()
                for site_id in site_ids:
                    if site_id.lower() in value_lower:
                        sample_site_mapping[value_str] = site_id
                        logger.debug(f"样品标识匹配成功（大小写不敏感）: {value_str} -> {site_id} (来源字段: {key})")
                        break
            
            # 策略3：部分匹配（从数据值中提取站点标识）
            if value_str not in sample_site_mapping:
                for site_id in site_ids:
                    if len(site_id) >= 3:
                        # 使用正则表达式匹配，确保站点ID是完整的标识符
                        pattern = re.compile(r'\b' + re.escape(site_id) + r'\b', re.IGNORECASE)
                        if pattern.search(value_str):
                            sample_site_mapping[value_str] = site_id
                            logger.debug(f"样品标识匹配成功（部分匹配）: {value_str} -> {site_id} (来源字段: {key})")
                            break
    
    # 【优化】3. 基于模式的匹配（时间序列模式和数值范围模式）
    year_range_mapping = {}
    
    # 3.1 基于时间序列模式的匹配
    # 如果数据预览中包含年份信息，尝试根据年份区间匹配站点
    if sample_data_preview:
        year_values = []
        # 从数据预览中提取年份值
        for key, value in sample_data_preview.items():
            if "year" in key.lower() or "年" in key or "age" in key.lower():
                try:
                    if isinstance(value, (int, float)):
                        year_values.append(float(value))
                    elif isinstance(value, str):
                        # 尝试从字符串中提取年份
                        year_match = re.search(r'(\d{4})', str(value))
                        if year_match:
                            year_values.append(float(year_match.group(1)))
                except (ValueError, TypeError):
                    continue
        
        # 如果有多个年份值且多个站点，尝试根据年份区间分配
        if len(year_values) >= 2 and len(site_ids) > 1:
            year_values_sorted = sorted(set(year_values))
            if len(year_values_sorted) >= 2:
                # 将年份范围分成与站点数量相等的区间
                year_range_size = (max(year_values_sorted) - min(year_values_sorted)) / len(site_ids)
                for idx, site_id in enumerate(site_ids):
                    start_year = min(year_values_sorted) + idx * year_range_size
                    end_year = min(year_values_sorted) + (idx + 1) * year_range_size
                    if idx == len(site_ids) - 1:
                        end_year = max(year_values_sorted)  # 最后一个区间包含最大值
                    year_range_key = f"{int(start_year)}-{int(end_year)}"
                    year_range_mapping[year_range_key] = site_id
                    logger.debug(f"时间序列模式匹配: {year_range_key} -> {site_id}")
    
    # 3.2 基于数值范围模式的匹配
    # 如果数据预览中包含指标数值，尝试根据数值范围匹配站点
    # 注意：这个策略的置信度较低，仅作为辅助手段
    value_range_mapping = {}
    if sample_data_preview and len(site_ids) > 1:
        # 提取数值字段（Sr/Ca、δ18O等）
        numeric_fields = {}
        for key, value in sample_data_preview.items():
            if any(indicator in key.lower() for indicator in ['sr/ca', 'δ18o', 'δ13c', 'sst']):
                try:
                    if isinstance(value, (int, float)):
                        numeric_fields[key] = float(value)
                    elif isinstance(value, str):
                        # 尝试从字符串中提取数值
                        num_match = re.search(r'([+-]?\d+\.?\d*)', str(value))
                        if num_match:
                            numeric_fields[key] = float(num_match.group(1))
                except (ValueError, TypeError):
                    continue
        
        # 如果找到数值字段，尝试根据数值范围分配站点
        if numeric_fields:
            for field_name, field_values in numeric_fields.items():
                if isinstance(field_values, list) and len(field_values) >= 2:
                    values_sorted = sorted(set(field_values))
                    if len(values_sorted) >= 2 and len(site_ids) > 1:
                        # 将数值范围分成与站点数量相等的区间
                        value_range_size = (max(values_sorted) - min(values_sorted)) / len(site_ids)
                        for idx, site_id in enumerate(site_ids):
                            start_value = min(values_sorted) + idx * value_range_size
                            end_value = min(values_sorted) + (idx + 1) * value_range_size
                            if idx == len(site_ids) - 1:
                                end_value = max(values_sorted)
                            range_key = f"{field_name}_{start_value:.2f}-{end_value:.2f}"
                            value_range_mapping[range_key] = site_id
                            logger.debug(f"数值范围模式匹配: {range_key} -> {site_id}")
    
    # 【优化】4. 统计匹配结果
    match_count = len(column_site_mapping) + len(sample_site_mapping) + len(year_range_mapping)
    confidence = "high" if match_count >= len(site_ids) else ("medium" if match_count > 0 else "low")
    
    # 确定映射策略
    strategy_parts = []
    if column_site_mapping:
        strategy_parts.append("column")
    if sample_site_mapping:
        strategy_parts.append("sample")
    if year_range_mapping:
        strategy_parts.append("year_range")
    if value_range_mapping:
        strategy_parts.append("value_range")
    
    mapping_strategy = "+".join(strategy_parts) if strategy_parts else "rule_based"
    
    logger.info(f"规则匹配完成: 列名匹配 {len(column_site_mapping)} 个, 样品标识匹配 {len(sample_site_mapping)} 个, "
                f"年份区间匹配 {len(year_range_mapping)} 个, 置信度: {confidence}")
    
    return {
        "column_site_mapping": column_site_mapping,
        "sample_site_mapping": sample_site_mapping,
        "year_range_mapping": year_range_mapping,
        "mapping_strategy": mapping_strategy,
        "confidence": confidence,
        "match_count": match_count
    }


def match_data_point_to_site(
    data_point: Dict[str, Any],
    row_data: Dict[str, Any],
    mapping_info: Dict[str, Any],
    research_sites: Dict[str, Dict[str, float]],
    default_site_info: Optional[Dict[str, float]] = None
) -> Tuple[Optional[str], Optional[Dict[str, float]]]:
    """
    将数据点匹配到对应的研究地点
    
    匹配优先级：
    1. 单地点场景：如果只有一个研究地点，直接返回
    2. 列名关联：如果列名中包含站点标识，使用列名映射
    3. 样品标识：如果数据值中包含站点标识，使用样品映射
    4. 年份区间：如果定义了年份区间映射，使用年份区间
    5. 默认站点：使用默认站点信息
    
    Args:
        data_point: 数据点字典
        row_data: 原始行数据字典
        mapping_info: 映射关系信息（由build_site_data_mapping生成）
        research_sites: 研究地点信息字典
        default_site_info: 默认站点信息（可选）
        
    Returns:
        Tuple[Optional[str], Optional[Dict[str, float]]]: (站点ID, 站点坐标信息)
    """
    # 1. 单地点场景
    if mapping_info.get("mapping_strategy") == "single_site":
        site_id = mapping_info.get("default_site")
        if site_id and site_id in research_sites:
            return site_id, research_sites[site_id]
    
    # 2. 列名关联场景
    column_mapping = mapping_info.get("column_site_mapping", {})
    for col_name, site_id in column_mapping.items():
        if col_name in row_data:
            if site_id in research_sites:
                return site_id, research_sites[site_id]
    
    # 3. 样品标识场景
    sample_mapping = mapping_info.get("sample_site_mapping", {})
    for key, value in data_point.items():
        if isinstance(value, str) and value in sample_mapping:
            site_id = sample_mapping[value]
            if site_id in research_sites:
                return site_id, research_sites[site_id]
    
    # 4. 年份区间场景
    year_range_mapping = mapping_info.get("year_range_mapping", {})
    if year_range_mapping and "year" in data_point:
        try:
            year = float(data_point["year"])
            for year_range, site_id in year_range_mapping.items():
                # 解析年份区间，如 "1900-1950"
                if "-" in year_range:
                    start, end = map(float, year_range.split("-"))
                    if start <= year <= end:
                        if site_id in research_sites:
                            return site_id, research_sites[site_id]
        except (ValueError, TypeError):
            pass
    
    # 5. 默认站点
    if default_site_info:
        return None, default_site_info
    
    # 6. 如果只有一个研究地点，使用它
    if len(research_sites) == 1:
        site_id = list(research_sites.keys())[0]
        return site_id, research_sites[site_id]
    
    return None, None












