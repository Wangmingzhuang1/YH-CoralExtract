#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
配置加载工具
用于加载JSON格式的配置文件
"""

import json
import os
import logging

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# 默认配置
DEFAULT_CONFIGS = {
    "unit_conversion.json": {
        "锶钙比": {
            "μmol/mol": {"target": "mmol/mol", "factor": 0.001},
            "mmol/mol": {"target": "mmol/mol", "factor": 1}
        },
        "碳同位素": {
            "‰": {"target": "‰", "factor": 1}
        },
        "氧同位素": {
            "‰": {"target": "‰", "factor": 1}
        },
        "海表温度": {
            "°C": {"target": "°C", "factor": 1},
            "℃": {"target": "°C", "factor": 1},
            "F": {"target": "°C", "factor": lambda x: (x - 32) * 5/9},
            "K": {"target": "°C", "factor": lambda x: x - 273.15}
        }
    },
    "outlier_thresholds.json": {
        "default": {"min": -999999, "max": 999999},
        "锶钙比": {
            "global": {"min": 6.0, "max": 12.0}
        },
        "海表温度": {
            "global": {"min": -2.0, "max": 35.0}
        },
        "碳同位素": {
            "global": {"min": -50.0, "max": 20.0}
        },
        "氧同位素": {
            "global": {"min": -20.0, "max": 10.0}
        }
    }
}

def load_config(config_name):
    """加载配置文件，带容错处理"""
    config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "config", config_name)
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
            logging.info(f"成功加载配置文件: {config_name}")
            return config
    except FileNotFoundError:
        logging.warning(f"配置文件未找到: {config_path}，使用默认配置")
        return DEFAULT_CONFIGS.get(config_name, {})
    except json.JSONDecodeError as e:
        logging.error(f"配置文件格式错误: {config_path}，错误: {str(e)}，使用默认配置")
        return DEFAULT_CONFIGS.get(config_name, {})
    except Exception as e:
        logging.error(f"加载配置文件失败: {config_path}，错误: {str(e)}，使用默认配置")
        return DEFAULT_CONFIGS.get(config_name, {})

# 保持原有的函数以确保向后兼容
def load_target_data_config():
    """加载目标数据类型配置（保持原函数）"""
    config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data_config.yaml")
    try:
        import yaml
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
            # 返回target_data列表而不是整个配置
            target_data = config.get("target_data", [])
            logging.info(f"成功加载目标数据配置，包含 {len(target_data)} 种数据类型")
            return target_data
    except FileNotFoundError:
        logging.error(f"数据配置文件未找到: {config_path}，请检查配置文件是否存在")
        # 返回默认配置
        default_target_data = [
            {
                "name": "锶钙比",
                "en_name": "Sr/Ca",
                "keywords": ["Sr/Ca", "锶钙比", "strontium/calcium"],
                "value_pattern": r"(\d+\.?\d*)\s*(mmol/mol|μmol/mol)",
                "units": ["mmol/mol", "μmol/mol"]
            },
            {
                "name": "氧同位素",
                "en_name": "δ18O",
                "keywords": ["δ18O", "氧同位素", "oxygen isotope"],
                "value_pattern": r"(-?\d+\.?\d*)\s*‰",
                "units": ["‰"]
            },
            {
                "name": "碳同位素",
                "en_name": "δ13C",
                "keywords": ["δ13C", "碳同位素", "carbon isotope"],
                "value_pattern": r"(-?\d+\.?\d*)\s*‰",
                "units": ["‰"]
            },
            {
                "name": "海表温度",
                "en_name": "SST",
                "keywords": ["SST", "海表温度", "sea surface temperature"],
                "value_pattern": r"(\d+\.?\d*)\s*(°C|℃|F|K)",
                "units": ["°C", "℃", "F", "K"]
            }
        ]
        logging.info("使用默认目标数据配置")
        return default_target_data
    except Exception as e:
        logging.error(f"加载数据配置文件失败 {config_path}: {str(e)}")
        return []

def load_spatio_temporal_config():
    """加载时空配置"""
    try:
        # 这里可以加载具体的时空配置文件
        # 目前返回默认配置
        config = {
            "valid_lat_range": (-90, 90),
            "valid_lon_range": (-180, 180),
            "valid_resolution_types": [
                "monthly", "bimonthly", "quarterly", "biannual", "annual", 
                "monthly_uneven", "bimonthly_uneven", "quarterly_uneven", 
                "biannual_uneven", "annual_uneven", "greater_than_annual",
                "greater_than_annual_uneven"
            ]
        }
        logging.info("成功加载时空配置")
        return config
    except Exception as e:
        logging.error(f"加载时空配置失败: {str(e)}")
        # 返回默认配置
        return {
            "valid_lat_range": (-90, 90),
            "valid_lon_range": (-180, 180),
            "valid_resolution_types": [
                "monthly", "bimonthly", "quarterly", "biannual", "annual"
            ]
        }