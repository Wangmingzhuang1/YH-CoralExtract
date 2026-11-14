#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
信号分离模块
负责将古气候数据中的不同信号进行分离
处理dataset子文件夹中的PDF正文和对应附件（Excel/Word），确保Excel转换为CSV
"""

import logging
import numpy as np
from typing import Dict, List, Any, Optional, Tuple

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class SignalSeparator:
    """信号分离模块"""
    
    def __init__(self):
        """初始化信号分离模块"""
        pass
    
    def separate_seasonal_and_non_seasonal(self, data: List[Dict[str, Any]], field_name: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        分离季节性和非季节性信号
        
        Args:
            data (List[Dict[str, Any]]): 数据列表
            field_name (str): 要分离的字段名
            
        Returns:
            Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]: (季节性信号, 非季节性信号)
        """
        # TODO: 实现季节性和非季节性信号分离的逻辑
        # 这里只是一个简单的示例，实际应用中可能需要使用更复杂的信号处理技术
        seasonal_data = []
        non_seasonal_data = []
        
        # 简单按月份分离（示例）
        for record in data:
            if "month" in record and record["month"] in [12, 1, 2]:  # 冬季
                seasonal_data.append(record)
            elif "month" in record and record["month"] in [6, 7, 8]:  # 夏季
                seasonal_data.append(record)
            else:
                non_seasonal_data.append(record)
                
        logging.info(f"信号分离完成: 季节性信号 {len(seasonal_data)} 条，非季节性信号 {len(non_seasonal_data)} 条")
        return seasonal_data, non_seasonal_data
    
    def separate_trend_and_anomaly(self, data: List[Dict[str, Any]], field_name: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        分离趋势和异常信号
        
        Args:
            data (List[Dict[str, Any]]): 数据列表
            field_name (str): 要分离的字段名
            
        Returns:
            Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]: (趋势信号, 异常信号)
        """
        # TODO: 实现趋势和异常信号分离的逻辑
        # 这里只是一个简单的示例，实际应用中可能需要使用移动平均等方法
        trend_data = []
        anomaly_data = []
        
        # 计算字段的平均值作为基准
        values = [record[field_name] for record in data if field_name in record and record[field_name] is not None]
        if values:
            mean_value = np.mean(values)
            
            # 简单按与均值的偏差分离
            for record in data:
                if field_name in record and record[field_name] is not None:
                    deviation = abs(record[field_name] - mean_value)
                    if deviation > 0.1 * abs(mean_value):  # 偏差大于10%认为是异常
                        anomaly_data.append(record)
                    else:
                        trend_data.append(record)
                        
        logging.info(f"趋势和异常信号分离完成: 趋势信号 {len(trend_data)} 条，异常信号 {len(anomaly_data)} 条")
        return trend_data, anomaly_data

# 导出所有类和函数
__all__ = [
    "SignalSeparator"
]