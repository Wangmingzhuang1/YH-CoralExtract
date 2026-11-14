import json
import math
import re
from typing import Dict, List, Tuple, Union, Optional
import numpy as np
from scipy.spatial.distance import pdist, squareform
from scipy.stats import pearsonr
import logging

# 配置日志
logger = logging.getLogger(__name__)

def load_spatio_temporal_config(config_path: str = "config/spatio_temporal_config.json") -> Dict:
    """加载时空对齐配置文件"""
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning(f"配置文件 {config_path} 未找到，使用默认配置")
        return get_default_spatio_temporal_config()
    except json.JSONDecodeError as e:
        logger.error(f"配置文件 {config_path} 格式错误: {e}")
        return get_default_spatio_temporal_config()

def get_default_spatio_temporal_config() -> Dict:
    """获取默认时空对齐配置"""
    return {
        "time_resolution_thresholds": {
            "monthly": {"min_data_points_per_year": 10, "max_data_points_per_year": 13},
            "bimonthly": {"min_data_points_per_year": 5, "max_data_points_per_year": 7},
            "quarterly": {"min_data_points_per_year": 3, "max_data_points_per_year": 5},
            "biannual": {"min_data_points_per_year": 1.5, "max_data_points_per_year": 2.5},
            "annual": {"min_data_points_per_year": 0.8, "max_data_points_per_year": 1.2},
            "greater_than_annual": {"min_data_points_per_year": 0, "max_data_points_per_year": 0.8}
        },
        "spatial_distance_thresholds": {
            "site_deduplication": 0.1,
            "local_reproducibility": 50,
            "regional_analysis": 3000
        },
        "reproducibility_thresholds": {
            "Sr_Ca_bimonthly": 0.11,
            "delta18O_bimonthly": 0.24
        },
        "coordinate_ranges": {
            "latitude_min": -90,
            "latitude_max": 90,
            "longitude_min": -180,
            "longitude_max": 180
        },
        "temporal_consistency": {
            "max_year_difference": 1
        },
        "ocean_basins": {
            "Pacific Ocean": "太平洋",
            "Atlantic Ocean": "大西洋",
            "Indian Ocean": "印度洋",
            "Arctic Ocean": "北冰洋",
            "Southern Ocean": "南大洋",
            "Mediterranean Sea": "地中海",
            "Caribbean Sea": "加勒比海",
            "South China Sea": "南海",
            "Red Sea": "红海",
            "Coral Sea": "珊瑚海"
        },
        "holocene_stages": {
            "early": {
                "min_year": -10050,
                "max_year": -6050
            },
            "middle": {
                "min_year": -6050,
                "max_year": -2050
            },
            "late": {
                "min_year": -2050,
                "max_year": 0
            },
            "ce": {
                "min_year": 0,
                "max_year": 2025
            }
        },
        "key_events": {
            "8.2ka": -6250,
            "4.2ka": -2250
        }
    }

def convert_coordinate_format(coord_str: str) -> float:
    """
    转换坐标格式：将度分秒格式转换为十进制度数
    
    Args:
        coord_str: 坐标字符串，如 "23°30′N" 或 "110.5" 或 "19.29N" 或 "0.017000N"
        
    Returns:
        float: 十进制度数
    """
    # 清理字符串
    coord_str = coord_str.strip()
    
    # 如果已经是十进制格式，直接返回
    if re.match(r"^[-+]?\d+\.?\d*$", coord_str):
        return float(coord_str)
    
    # 处理带方向的十进制格式，如 "19.29N" 或 "110.66E" 或 "0.017000N"
    dir_pattern = r"^([+-]?\d+\.?\d*)\s*([NSEW])$"
    dir_match = re.match(dir_pattern, coord_str, re.IGNORECASE)
    if dir_match:
        degrees = float(dir_match.group(1))
        direction = dir_match.group(2).upper()
        
        # 南纬和西经为负值
        if direction in ['S', 'W']:
            degrees = -degrees
            
        return degrees
    
    # 处理度分秒格式
    dms_pattern = r"(\d+)°(\d+)[′']?(\d*)[″\"]?\s*([NSEW])"
    match = re.match(dms_pattern, coord_str.strip())
    
    if match:
        degrees = int(match.group(1))
        minutes = int(match.group(2)) if match.group(2) else 0
        seconds = int(match.group(3)) if match.group(3) else 0
        direction = match.group(4)
        
        decimal_degrees = degrees + minutes/60 + seconds/3600
        
        # 南纬和西经为负值
        if direction in ['S', 'W']:
            decimal_degrees = -decimal_degrees
            
        return round(decimal_degrees, 6)  # 保留6位小数
    
    logger.warning(f"无法解析坐标格式: {coord_str}")
    return 0.0

def validate_coordinates(latitude: float, longitude: float, config: Optional[Dict] = None, geo_siteName: str = "") -> bool:
    """
    校验坐标范围和区域合理性
    
    Args:
        latitude: 纬度
        longitude: 经度
        config: 配置字典
        geo_siteName: 站点名称，用于区域合理性检查
        
    Returns:
        bool: 坐标是否有效
    """
    if config is None:
        config = load_spatio_temporal_config()
    
    coord_ranges = config.get("coordinate_ranges", {})
    lat_min = coord_ranges.get("latitude_min", -90)
    lat_max = coord_ranges.get("latitude_max", 90)
    lon_min = coord_ranges.get("longitude_min", -180)
    lon_max = coord_ranges.get("longitude_max", 180)
    
    # 确保坐标是数值类型
    try:
        lat_val = float(latitude)
        lon_val = float(longitude)
        
        # 基础范围校验
        if not (lat_min <= lat_val <= lat_max and lon_min <= lon_val <= lon_max):
            return False
        
        # 针对特定研究区域的常识校验（如Palmyra Atoll大致范围）
        if "Palmyra Atoll" in geo_siteName or "palmyra" in geo_siteName.lower():
            # 若提取的纬度异常，检查是否在合理范围
            if not (4 <= lat_val <= 6):
                logger.warning(f"Palmyra Atoll纬度异常：{lat_val}")
                return False
            # 若提取的经度异常，检查是否在合理范围
            if not (-163 <= lon_val <= -161):
                logger.warning(f"Palmyra Atoll经度异常：{lon_val}")
                return False
        
        return True
    except (ValueError, TypeError):
        return False

def calculate_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    使用Haversine公式计算两点间距离（公里）
    
    Args:
        lat1, lon1: 第一个点的经纬度
        lat2, lon2: 第二个点的经纬度
        
    Returns:
        float: 两点间距离（公里）
    """
    # 地球半径（公里）
    R = 6371.0
    
    # 转换为弧度
    lat1_rad = math.radians(lat1)
    lon1_rad = math.radians(lon1)
    lat2_rad = math.radians(lat2)
    lon2_rad = math.radians(lon2)
    
    # Haversine公式
    dlat = lat2_rad - lat1_rad
    dlon = lon2_rad - lon1_rad
    
    a = math.sin(dlat/2)**2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    
    distance = R * c
    return distance

def match_ocean_basin(latitude: float, longitude: float) -> str:
    """
    根据经纬度匹配海洋盆地（简化实现）
    
    Args:
        latitude: 纬度
        longitude: 经度
        
    Returns:
        str: 海洋盆地名称
    """
    # 简化实现，实际应用中可以集成World Ocean Atlas工具
    if -30 <= latitude <= 30 and 100 <= longitude <= 150:
        return "Pacific Ocean"
    elif -30 <= latitude <= 30 and -100 <= longitude <= -50:
        return "Atlantic Ocean"
    elif -30 <= latitude <= 30 and 30 <= longitude <= 100:
        return "Indian Ocean"
    elif 30 <= latitude <= 90:
        return "Arctic Ocean"
    elif -90 <= latitude <= -30:
        return "Southern Ocean"
    elif 30 <= latitude <= 45 and -10 <= longitude <= 40:
        return "Mediterranean Sea"
    elif 10 <= latitude <= 30 and -90 <= longitude <= -60:
        return "Caribbean Sea"
    elif 0 <= latitude <= 25 and 100 <= longitude <= 125:
        return "South China Sea"
    elif 12 <= latitude <= 30 and 30 <= longitude <= 45:
        return "Red Sea"
    elif -30 <= latitude <= -5 and 140 <= longitude <= 160:
        return "Coral Sea"
    else:
        return "Other"

def calculate_data_points_per_year(data_points: List[Dict], min_year: int, max_year: int) -> float:
    """
    计算年均数据点数
    
    Args:
        data_points: 数据点列表
        min_year: 起始年份
        max_year: 结束年份
        
    Returns:
        float: 年均数据点数
    """
    if max_year <= min_year:
        return 0.0
    
    total_points = len(data_points)
    year_span = max_year - min_year + 1  # 包含起始和结束年份
    
    return total_points / year_span if year_span > 0 else 0.0

def validate_time_resolution(has_resolution: str, actual_points_per_year: float, config: Optional[Dict] = None) -> Tuple[bool, str]:
    """
    验证时间分辨率标签与实际数据点数是否匹配
    
    Args:
        has_resolution: 分辨率标签
        actual_points_per_year: 实际年均数据点数
        config: 配置字典
        
    Returns:
        Tuple[bool, str]: (是否匹配, 建议的分辨率标签)
    """
    if config is None:
        config = load_spatio_temporal_config()
    
    thresholds = config.get("time_resolution_thresholds", {})
    
    # 处理带_uneven后缀的标签
    base_resolution = has_resolution.replace("_uneven", "")
    
    if base_resolution in thresholds:
        threshold = thresholds[base_resolution]
        min_points = threshold.get("min_data_points_per_year", 0)
        max_points = threshold.get("max_data_points_per_year", float('inf'))
        
        if min_points <= actual_points_per_year <= max_points:
            return True, has_resolution
        else:
            # 根据实际数据点数推荐合适的分辨率标签
            for res, thresh in thresholds.items():
                min_p = thresh.get("min_data_points_per_year", 0)
                max_p = thresh.get("max_data_points_per_year", float('inf'))
                if min_p <= actual_points_per_year <= max_p:
                    # 如果原标签不含_uneven但需要添加，则添加
                    if "_uneven" not in has_resolution and abs(actual_points_per_year - (min_p + max_p)/2) > (max_p - min_p)/3:
                        return False, f"{res}_uneven"
                    return False, res
            return False, "unknown"
    
    return False, "unknown"

def detect_time_gaps(years: List[int], gap_threshold: int = 5) -> List[Tuple[int, int]]:
    """
    检测时间序列中的断档
    
    Args:
        years: 年份列表（已排序）
        gap_threshold: 断档阈值（年）
        
    Returns:
        List[Tuple[int, int]]: 断档时间段列表 [(start_year, end_year), ...]
    """
    if len(years) < 2:
        return []
    
    gaps = []
    for i in range(1, len(years)):
        gap = years[i] - years[i-1]
        if gap > gap_threshold:
            gaps.append((years[i-1], years[i]))
    
    return gaps

def calculate_correlation(series1: List[float], series2: List[float]) -> float:
    """
    计算两个时间序列的相关性
    
    Args:
        series1: 第一个时间序列
        series2: 第二个时间序列
        
    Returns:
        float: 相关性系数
    """
    if len(series1) != len(series2) or len(series1) < 2:
        return 0.0
    
    try:
        # pearsonr返回的是PearsonRResult对象或元组
        correlation_result = pearsonr(series1, series2)
        
        # 获取相关系数
        correlation = 0.0
        
        # 尝试多种方法提取相关系数
        try:
            # 方法1: 检查是否有statistic属性
            if hasattr(correlation_result, 'statistic'):
                correlation = float(correlation_result.statistic)  # type: ignore
            # 方法2: 检查是否为元组
            elif isinstance(correlation_result, tuple) and len(correlation_result) > 0:
                correlation = float(correlation_result[0])  # type: ignore
            # 方法3: 直接转换
            else:
                correlation = float(correlation_result)  # type: ignore
        except (ValueError, TypeError, AttributeError):
            # 如果以上方法都失败，尝试从字符串表示中提取
            try:
                result_str = str(correlation_result)
                # 使用正则表达式提取相关系数
                import re
                match = re.search(r'[\( ]([\-\+]?\d+\.\d+)', result_str)
                if match:
                    correlation = float(match.group(1))
                else:
                    correlation = 0.0
            except (ValueError, TypeError, AttributeError):
                correlation = 0.0
        
        # 确保返回的是float类型且不是NaN
        if not np.isnan(correlation):
            return correlation
        else:
            return 0.0
    except Exception as e:
        logger.warning(f"计算相关性时出错: {e}")
        return 0.0

def find_overlap_period(records: List[Dict]) -> Tuple[int, int]:
    """
    找到多条记录的重叠时间区间
    
    Args:
        records: 记录列表，每条记录包含minYear和maxYear字段
        
    Returns:
        Tuple[int, int]: 重叠时间区间 (start_year, end_year)
    """
    if not records:
        return 0, 0
    
    # 初始化为第一条记录的时间范围
    overlap_start = records[0].get("minYear", 0)
    overlap_end = records[0].get("maxYear", 0)
    
    # 逐条记录更新重叠区间
    for record in records[1:]:
        min_year = record.get("minYear", 0)
        max_year = record.get("maxYear", 0)
        
        # 更新重叠区间的起始年份（取最大值）
        overlap_start = max(overlap_start, min_year)
        # 更新重叠区间的结束年份（取最小值）
        overlap_end = min(overlap_end, max_year)
    
    # 如果没有重叠区间，返回(0, 0)
    if overlap_start > overlap_end:
        return 0, 0
    
    return overlap_start, overlap_end

def aggregate_to_annual(data_points: List[Dict], aggregation_method: str = "mean") -> List[Dict]:
    """
    将数据聚合为年度分辨率
    
    Args:
        data_points: 数据点列表
        aggregation_method: 聚合方法 ("mean", "median", "sum")
        
    Returns:
        List[Dict]: 聚合后的年度数据点列表
    """
    # 按年份分组数据
    yearly_data = {}
    for point in data_points:
        year = point.get("year")
        if year is not None:
            if year not in yearly_data:
                yearly_data[year] = []
            yearly_data[year].append(point)
    
    # 聚合每年的数据
    aggregated_data = []
    for year, points in yearly_data.items():
        if aggregation_method == "mean":
            # 计算数值字段的平均值
            avg_point = {"year": year}
            numeric_fields = [key for key in points[0].keys() if isinstance(points[0][key], (int, float)) and key != "year"]
            for field in numeric_fields:
                values = [point[field] for point in points if field in point]
                if values:
                    avg_point[field] = sum(values) / len(values)
            aggregated_data.append(avg_point)
        elif aggregation_method == "median":
            # 计算数值字段的中位数
            median_point = {"year": year}
            numeric_fields = [key for key in points[0].keys() if isinstance(points[0][key], (int, float)) and key != "year"]
            for field in numeric_fields:
                values = [point[field] for point in points if field in point]
                if values:
                    median_point[field] = sorted(values)[len(values)//2]
            aggregated_data.append(median_point)
        elif aggregation_method == "sum":
            # 计算数值字段的总和
            sum_point = {"year": year}
            numeric_fields = [key for key in points[0].keys() if isinstance(points[0][key], (int, float)) and key != "year"]
            for field in numeric_fields:
                values = [point[field] for point in points if field in point]
                if values:
                    sum_point[field] = sum(values)
            aggregated_data.append(sum_point)
    
    return sorted(aggregated_data, key=lambda x: x["year"])

# 导出所有函数
__all__ = [
    "load_spatio_temporal_config",
    "convert_coordinate_format",
    "validate_coordinates",
    "calculate_distance",
    "match_ocean_basin",
    "calculate_data_points_per_year",
    "validate_time_resolution",
    "detect_time_gaps",
    "calculate_correlation",
    "find_overlap_period",
    "aggregate_to_annual"
]