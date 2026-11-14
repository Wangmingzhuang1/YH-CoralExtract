#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
大模型解析结果验证模块
用于验证指标和单位映射的正确性和合理性
"""

import logging
from typing import Dict, Any, List, Tuple

logger = logging.getLogger(__name__)

class ParsingResultValidator:
    """解析结果验证器"""
    
    # 标准字段与单位的合理匹配规则
    FIELD_UNIT_MAPPING = {
        "Sr/Ca_value": ["mmol/mol", "μmol/mol", "umol/mol", "ppm", "unknown"],
        "δ18O_value": ["‰ VPDB", "‰ PDB", "‰ VSMOW", "‰ SMOW", "‰", "per mil", "per mil VPDB", "per mil PDB", "unknown"],
        "δ18O_difference": ["‰ VPDB", "‰ PDB", "‰", "per mil", "per mil VPDB", "unknown"],
        "δ13C_value": ["‰ VPDB", "‰ PDB", "‰", "per mil", "per mil VPDB", "unknown"],
        "sea_surface_temperature_value": ["°C", "℃", "[C]", "degC", "unknown"],
        "year": ["CE", "BP", "BCE", "BC", "unknown"],
        "hydrological_signal": ["‰", "per mil", "unknown"]
    }
    
    # 必需的标准字段
    REQUIRED_FIELDS = ["year"]
    
    def __init__(self):
        pass
    
    def validate_parsing_result(self, parsed_result: Dict[str, Any]) -> Tuple[bool, List[str]]:
        """
        验证解析结果的完整性和正确性
        
        Args:
            parsed_result: 大模型返回的解析结果
            
        Returns:
            Tuple[bool, List[str]]: (是否有效, 错误/警告列表)
        """
        errors = []
        warnings = []
        
        # 1. 检查基本结构
        if not isinstance(parsed_result, dict):
            errors.append("解析结果不是字典类型")
            return False, errors
        
        # 2. 检查必需字段
        if "mapped_data" not in parsed_result:
            errors.append("缺少 mapped_data 字段")
            return False, errors
        
        mapped_data = parsed_result.get("mapped_data", [])
        if not isinstance(mapped_data, list):
            errors.append("mapped_data 不是列表类型")
            return False, errors
        
        # 3. 验证每个映射项
        for idx, item in enumerate(mapped_data):
            if not isinstance(item, dict):
                errors.append(f"mapped_data[{idx}] 不是字典类型")
                continue
            
            # 检查必需字段
            if "original_header" not in item:
                errors.append(f"mapped_data[{idx}] 缺少 original_header 字段")
            
            if "standard_field" not in item:
                errors.append(f"mapped_data[{idx}] 缺少 standard_field 字段")
                continue
            
            if "unit" not in item:
                warnings.append(f"mapped_data[{idx}] 缺少 unit 字段，等待源证据确认")
                item["unit"] = "unknown"
            
            standard_field = item.get("standard_field")
            unit = item.get("unit", "unknown")
            
            # 验证单位不为空
            if not unit or unit in [None, "", "null", "None"]:
                warnings.append(f"列 '{item.get('original_header', 'unknown')}' 的单位为空，设置为 'unknown'")
                item["unit"] = "unknown"
            
            # 验证指标和单位的匹配性
            if standard_field and unit != "unknown":
                is_valid, reason = self._validate_field_unit_match(standard_field, unit)
                if not is_valid:
                    warnings.append(f"列 '{item.get('original_header', 'unknown')}': {reason}")
        
        # 4. 检查是否至少有一个year字段
        has_year = any(
            item.get("standard_field") == "year" 
            for item in mapped_data 
            if isinstance(item, dict)
        )
        if not has_year:
            warnings.append("未找到年份字段，可能影响时间序列分析")
        
        # 5. 检查是否有数据字段
        data_fields = [
            "Sr/Ca_value", "Ba/Ca_value", "δ18O_value", "δ13C_value", 
            "sea_surface_temperature_value", "δ18O_difference"
        ]
        has_data_field = any(
            item.get("standard_field") in data_fields
            for item in mapped_data
            if isinstance(item, dict)
        )
        if not has_data_field:
            warnings.append("未找到任何数据字段（Sr/Ca、δ18O、δ13C、SST等），可能无法提取有效数据")
        
        all_issues = errors + warnings
        is_valid = len(errors) == 0
        
        if all_issues:
            logger.warning(f"解析结果验证发现问题: {len(errors)} 个错误, {len(warnings)} 个警告")
            for issue in all_issues:
                logger.warning(f"  - {issue}")
        
        return is_valid, all_issues
    
    def _validate_field_unit_match(self, standard_field: str, unit: str) -> Tuple[bool, str]:
        """
        验证字段和单位是否匹配
        
        Args:
            standard_field: 标准字段名
            unit: 单位
            
        Returns:
            Tuple[bool, str]: (是否匹配, 原因说明)
        """
        # 如果字段不在映射表中，允许任何单位
        if standard_field not in self.FIELD_UNIT_MAPPING:
            return True, ""
        
        # 获取该字段允许的单位列表
        allowed_units = self.FIELD_UNIT_MAPPING[standard_field]
        
        # 检查单位是否在允许列表中
        unit_normalized = unit.strip().lower()
        allowed_units_normalized = [u.lower() for u in allowed_units]
        
        # 模糊匹配：检查单位是否包含允许的单位关键词
        is_match = False
        for allowed_unit in allowed_units:
            if allowed_unit.lower() in unit_normalized or unit_normalized in allowed_unit.lower():
                is_match = True
                break
        
        if not is_match and unit != "unknown":
            return False, f"字段 '{standard_field}' 的单位 '{unit}' 可能不匹配（允许的单位: {', '.join(allowed_units)}）"
        
        return True, ""
