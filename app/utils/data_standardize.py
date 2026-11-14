import logging
from typing import Dict, Any, List, Tuple


def literature_based_standardize(extracted_data: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    对提取的古气候数据进行标准化处理
    输入：提取的原始数据列表
    输出：(标准化数据列表, 质量报告字典)
    """
    try:
        standardized_data = []
        quality_reports = {
            "total_records": len(extracted_data),
            "valid_records": 0,
            "invalid_records": 0,
            "issues": []
        }
        
        for idx, data_point in enumerate(extracted_data):
            try:
                # 验证必要字段
                required_fields = ["paper_hash", "sampling_location", "testing_method"]
                if not all(field in data_point for field in required_fields):
                    quality_reports["invalid_records"] += 1
                    quality_reports["issues"].append(f"记录{idx}: 缺少必要字段")
                    continue
                
                # 数据验证与清洗
                standardized_point = data_point.copy()
                
                # 验证年份信息
                if "year" in standardized_point:
                    try:
                        year = float(standardized_point["year"])
                        if not (1000 <= year <= 2100):
                            quality_reports["issues"].append(f"记录{idx}: 年份值异常 {year}")
                    except ValueError:
                        pass
                
                # 验证地理坐标
                if "geo_latitude" in standardized_point:
                    try:
                        lat = float(standardized_point["geo_latitude"])
                        if not (-90 <= lat <= 90):
                            quality_reports["issues"].append(f"记录{idx}: 纬度值超出范围 {lat}")
                    except ValueError:
                        pass
                
                if "geo_longitude" in standardized_point:
                    try:
                        lon = float(standardized_point["geo_longitude"])
                        if not (-180 <= lon <= 180):
                            quality_reports["issues"].append(f"记录{idx}: 经度值超出范围 {lon}")
                    except ValueError:
                        pass
                
                standardized_data.append(standardized_point)
                quality_reports["valid_records"] += 1
                
            except Exception as e:
                quality_reports["invalid_records"] += 1
                quality_reports["issues"].append(f"记录{idx}: 处理失败 - {str(e)}")
                continue
        
        logging.info(f"数据标准化完成: {quality_reports['valid_records']}/{quality_reports['total_records']} 条有效记录")
        return standardized_data, quality_reports
        
    except Exception as e:
        logging.error(f"数据标准化失败: {str(e)}")
        return [], {"error": str(e), "total_records": len(extracted_data)}
