#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
数据流转链路日志打印脚本
专门跟踪 CSV 读取→字段匹配→数值赋值→单位传递的全流程
用于快速定位字段不匹配或数据格式问题
"""
import logging
import pandas as pd
from typing import Dict, List, Any, Optional
import json

# 配置日志
logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('data_trace.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger('DataTraceLogger')

class DataTraceLogger:
    """数据流转链路日志跟踪器"""
    
    def __init__(self):
        self.trace_data = {
            "csv_reading": {},
            "field_mapping": {},
            "value_assignment": {},
            "unit_transfer": {},
            "final_output": {}
        }
    
    def log_csv_reading(self, file_path: str, df: pd.DataFrame):
        """记录CSV读取过程"""
        logger.debug("=" * 50)
        logger.debug("CSV读取阶段")
        logger.debug("=" * 50)
        logger.debug(f"文件路径: {file_path}")
        logger.debug(f"数据形状: {df.shape}")
        logger.debug(f"列名: {list(df.columns)}")
        logger.debug(f"前3行数据预览:")
        logger.debug(f"\n{df.head(3)}")
        
        # 记录到trace数据中
        self.trace_data["csv_reading"] = {
            "file_path": file_path,
            "shape": df.shape,
            "columns": list(df.columns),
            "sample_data": df.head(3).to_dict('records')
        }
    
    def log_column_cleaning(self, original_columns: List[str], cleaned_columns: List[str]):
        """记录列名清洗过程"""
        logger.debug("-" * 30)
        logger.debug("列名清洗过程")
        logger.debug("-" * 30)
        for orig, clean in zip(original_columns, cleaned_columns):
            if orig != clean:
                logger.debug(f"列名清洗: '{orig}' -> '{clean}'")
    
    def log_field_mapping(self, column_map: Dict[str, Dict[str, str]], mapped_data: List[Dict[str, Any]]):
        """记录字段映射过程"""
        logger.debug("=" * 50)
        logger.debug("字段映射阶段")
        logger.debug("=" * 50)
        logger.debug(f"共映射 {len(column_map)} 个字段")
        
        # 只在调试模式下输出详细映射信息
        if logging.getLogger().isEnabledFor(logging.DEBUG):
            logger.debug("列名映射关系:")
            for df_col, map_info in column_map.items():
                logger.debug(f"  {df_col} -> {map_info['standard_field']} (单位: {map_info.get('unit', '未明确')})")
            
            logger.debug("解析后的映射数据:")
            for item in mapped_data:
                logger.debug(f"  原始列名: {item['original_header']}")
                logger.debug(f"    标准字段: {item['standard_field']}")
                logger.debug(f"    单位: {item['unit']}")
                logger.debug(f"    描述: {item['description']}")
        
        # 记录到trace数据中
        self.trace_data["field_mapping"] = {
            "column_mapping": column_map,
            "mapped_data": mapped_data
        }
    
    def log_value_assignment(self, df: pd.DataFrame, column_map: Dict[str, Dict[str, str]]):
        """记录数值赋值过程"""
        logger.debug("=" * 50)
        logger.debug("数值赋值阶段")
        logger.debug("=" * 50)
        logger.debug(f"处理 {len(df)} 行数据")
        
        # 只在调试模式下检查前3行数据的赋值情况
        if logging.getLogger().isEnabledFor(logging.DEBUG):
            for idx in range(min(3, len(df))):
                logger.debug(f"第 {idx+1} 行数据:")
                row = df.iloc[idx]
                for df_col, map_info in column_map.items():
                    if df_col in df.columns:
                        raw_value = row[df_col]
                        logger.debug(f"  {df_col} ({map_info['standard_field']}): {raw_value} (原始值)")
        
        # 记录到trace数据中
        self.trace_data["value_assignment"] = {
            "row_count": len(df),
            "sample_assignments": []
        }
        
        # 记录前3行的赋值情况
        for idx in range(min(3, len(df))):
            row_assignments: Dict[str, Any] = {"row_index": idx}
            row = df.iloc[idx]
            for df_col, map_info in column_map.items():
                if df_col in df.columns:
                    row_assignments[df_col] = {
                        "raw_value": str(row[df_col]),
                        "standard_field": map_info['standard_field']
                    }
            self.trace_data["value_assignment"]["sample_assignments"].append(row_assignments)
    
    def log_numeric_conversion(self, field_name: str, raw_value: Any, converted_value: Any, success: bool):
        """记录数值转换过程"""
        # 只有在调试模式下才记录详细的数值转换信息
        if logging.getLogger().isEnabledFor(logging.DEBUG):
            if success:
                logger.info(f"数值转换成功: {field_name} '{raw_value}' -> '{converted_value}'")
            else:
                logger.warning(f"数值转换失败: {field_name} '{raw_value}' -> 转换失败")
        elif not success:
            # 在非调试模式下，仍然记录转换失败的警告信息
            logger.warning(f"数值转换失败: {field_name} '{raw_value}' -> 转换失败")
    
    def log_unit_transfer(self, field_name: str, unit: str, target_field: str):
        """记录单位传递过程"""
        logger.debug(f"单位传递: {field_name} 的单位 '{unit}' 已传递到 {target_field}")
        
        # 更新trace数据
        if "unit_transfers" not in self.trace_data["unit_transfer"]:
            self.trace_data["unit_transfer"]["unit_transfers"] = []
        
        self.trace_data["unit_transfer"]["unit_transfers"].append({
            "field_name": field_name,
            "unit": unit,
            "target_field": target_field
        })
    
    def log_final_output(self, final_data: List[Dict[str, Any]], output_fields: List[str]):
        """记录最终输出数据"""
        logger.debug("=" * 50)
        logger.debug("最终输出阶段")
        logger.debug("=" * 50)
        logger.debug(f"输出 {len(final_data)} 条记录")
        logger.debug(f"输出字段: {output_fields}")
        
        # 显示前3条记录的关键字段
        key_fields = [field for field in output_fields if any(kw in field.lower() for kw in ['value', 'unit', 'year'])]
        logger.debug("前3条记录的关键字段:")
        for i, record in enumerate(final_data[:3]):
            logger.debug(f"  记录 {i+1}:")
            for field in key_fields:
                if field in record:
                    logger.debug(f"    {field}: {record[field]}")
        
        # 记录到trace数据中
        self.trace_data["final_output"] = {
            "record_count": len(final_data),
            "output_fields": output_fields,
            "sample_records": final_data[:3]
        }
    
    def log_error(self, stage: str, error_msg: str, additional_info: Optional[Dict[str, Any]] = None):
        """记录错误信息"""
        logger.error(f"[{stage}] 错误: {error_msg}")
        if additional_info:
            logger.error(f"附加信息: {json.dumps(additional_info, ensure_ascii=False, indent=2)}")
    
    def save_trace_report(self, report_path: str = "data_trace_report.json"):
        """保存完整的追踪报告"""
        try:
            with open(report_path, 'w', encoding='utf-8') as f:
                json.dump(self.trace_data, f, ensure_ascii=False, indent=2)
            logger.info(f"追踪报告已保存到: {report_path}")
        except Exception as e:
            logger.error(f"保存追踪报告失败: {str(e)}")
    
    def print_summary(self):
        """打印追踪摘要"""
        logger.debug("=" * 60)
        logger.debug("数据流转链路追踪摘要")
        logger.debug("=" * 60)
        logger.debug(f"CSV读取: {self.trace_data['csv_reading'].get('file_path', '未执行')}")
        logger.debug(f"字段映射: {len(self.trace_data['field_mapping'].get('column_mapping', {}))} 个字段")
        logger.debug(f"数值赋值: {self.trace_data['value_assignment'].get('row_count', 0)} 行数据")
        logger.debug(f"最终输出: {self.trace_data['final_output'].get('record_count', 0)} 条记录")
        logger.debug("=" * 60)

# 全局实例
data_tracer = DataTraceLogger()

def get_tracer():
    """获取全局追踪器实例"""
    return data_tracer

if __name__ == "__main__":
    # 示例使用
    tracer = get_tracer()
    
    # 模拟CSV读取
    sample_data = {
        "Year": [2020.083333, 2020.166667, 2020.25],
        "Sr/Ca (mmol/mol)": [8.154, 8.231, 8.195],
        "δ18O (‰ VPDB)": [-2.35, -2.41, -2.38]
    }
    df = pd.DataFrame(sample_data)
    tracer.log_csv_reading("sample.csv", df)
    
    # 模拟字段映射
    column_map = {
        "Year": {"standard_field": "year", "unit": "CE"},
        "Sr/Ca (mmol/mol)": {"standard_field": "Sr/Ca_value", "unit": "mmol/mol"},
        "δ18O (‰ VPDB)": {"standard_field": "δ18O_value", "unit": "‰ VPDB"}
    }
    mapped_data = [
        {"original_header": "Year", "standard_field": "year", "unit": "CE", "description": "年份"},
        {"original_header": "Sr/Ca (mmol/mol)", "standard_field": "Sr/Ca_value", "unit": "mmol/mol", "description": "锶钙比"},
        {"original_header": "δ18O (‰ VPDB)", "standard_field": "δ18O_value", "unit": "‰ VPDB", "description": "氧同位素"}
    ]
    tracer.log_field_mapping(column_map, mapped_data)
    
    # 模拟数值赋值
    tracer.log_value_assignment(df, column_map)
    
    # 模拟单位传递
    tracer.log_unit_transfer("Sr/Ca_value", "mmol/mol", "Sr_Ca_original_unit")
    tracer.log_unit_transfer("δ18O_value", "‰ VPDB", "δ18O_original_unit")
    
    # 模拟最终输出
    final_data = [
        {
            "data_id": "monthly_0001",
            "year": 2020.083333,
            "Sr_Ca_value": 8.154,
            "Sr_Ca_original_unit": "mmol/mol",
            "δ18O_value": -2.35,
            "δ18O_original_unit": "‰ VPDB"
        }
    ]
    tracer.log_final_output(final_data, list(final_data[0].keys()))
    
    # 打印摘要并保存报告
    tracer.print_summary()
    tracer.save_trace_report()