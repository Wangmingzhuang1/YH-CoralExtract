#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
时空标准化层模块
负责时间维度和空间维度的标准化处理
处理dataset子文件夹中的PDF正文和对应附件（Excel/Word），确保Excel转换为CSV
当附录中没有地点名称、经纬度或单位时，从PDF正文中获取
"""

import logging
from decimal import Decimal
from math import ulp
from statistics import median
from app.core.moa.unit_converter import finite_number, source_number
from datetime import date
import re
from typing import Dict, Any, Optional
from app.utils.spatio_temporal_utils import convert_coordinate_format, validate_coordinates

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def time_basis(label):
    """从源文声明识别绝对年代口径。单独的 a、yr、ka 只表示时间长度。"""
    text = str(label or "").replace("_", " ")
    if re.search(r"\b(?:relative|internal|growth)\b", text, re.I):
        return None
    if re.search(r"\b(?:BCE|BC)\b|公元前", text, re.I):
        return "age_bce"
    if re.search(r"\b(?:BP|before present)\b", text, re.I):
        return "age_ka_bp" if re.search(r"\b(?:ka|kyr)\b", text, re.I) else "age_bp"
    if re.search(r"\b(?:CE|AD|Common Era)\b|公元", text, re.I):
        return "year"
    return None


def bp_reference_year(*labels):
    """读取明确绑定到 BP 零点的年份，冲突时不换算。"""
    pattern = r"(?:reference\s+year\s*(?:is|=|:)?|present\s+(?:is\s+)?defined\s+as)\s*(?:CE|AD)?\s*(\d{4})\b"
    years = {int(year) for label in labels for year in re.findall(pattern, str(label or ""), re.I)}
    return next(iter(years), 1950) if len(years) <= 1 else None


def calendar_date(value, order=None, year_range=None):
    """解析 ISO 日期、年在后的月日日期，或档案起止年约束下的 Mon-YY 两位年份。"""
    label = str(value).strip()
    try:
        if re.fullmatch(r"\d{4}([-\/])\d{2}\1\d{2}(?:[ T]00:00:00)?", label):
            return date.fromisoformat(label[:10].replace("/", "-"))
        match = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", label)
        if order in {"mdy", "dmy"} and match:
            first, second, year = map(int, match.groups())
            month, day = (first, second) if order == "mdy" else (second, first)
            return date(year, month, day)
        if year_range is not None:
            day = month_year_date(label, year_range)
            if day:
                return day
    except ValueError:
        return None
    return None


def calendar_date_order(values, year_range=None):
    """只在非空日期整列一致且月日顺序唯一时确认年在后的格式；整列 Mon-YY 记 month_year。"""
    values = list(values)
    if values and all(_MONTH_YEAR.fullmatch(str(value).strip()) for value in values):
        return "month_year"
    orders = {"mdy", "dmy"}
    saw_year_last = saw_date = False
    for value in values:
        saw_date = True
        if calendar_date(value, year_range=year_range):
            continue
        saw_year_last = True
        orders = {order for order in orders if calendar_date(value, order)}
        if not orders:
            return None
    if saw_year_last:
        return next(iter(orders)) if len(orders) == 1 else None
    return "iso" if saw_date else None


_CE_LABEL = re.compile(r"^(?:AD|CE)\s+|\s+(?:AD|CE)$", re.I)
_NUMERIC_AGE = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
_MONTH_NAMES = {name: index for index, name in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MONTH_YEAR = re.compile(r"^([A-Za-z]{3,9})[-\s/](\d{2})$")


def resolve_two_digit_year(two_digit, year_range):
    """两位年份由源档案起止年唯一约束；区间缺失或多候选→None（保留待核，不猜）。"""
    if not year_range:
        return None
    start, end = year_range
    if start is None or end is None:
        return None
    candidates = [century + two_digit for century in (1900, 2000)
                  if start <= century + two_digit <= end]
    return candidates[0] if len(candidates) == 1 else None


def month_year_date(value, year_range):
    """解析 Mon-YY（如 Aug-07→2007-08、Aug-87→1987-08）为月首日。

    两位年份须由明确的档案起止年唯一确定；证据不足或跨世纪多候选时返回 None，
    由调用方保留原值待核。月份名须合法。
    """
    match = _MONTH_YEAR.fullmatch(str(value).strip())
    if not match:
        return None
    month = _MONTH_NAMES.get(match.group(1)[:3].casefold())
    if month is None:
        return None
    year = resolve_two_digit_year(int(match.group(2)), year_range)
    if year is None:
        return None
    try:
        return date(year, month, 1)
    except ValueError:
        return None


def year_ce(value, basis, reference_year=1950, decimal_comma=False, date_order=None, year_range=None):
    label = str(value).strip()
    if basis == "year":
        label = _CE_LABEL.sub("", label)
        day = calendar_date(label, date_order, year_range=year_range)
        if day:
            start = date(day.year, 1, 1)
            days = 365 + int(day.year % 4 == 0 and (day.year % 100 != 0 or day.year % 400 == 0))
            return day.year + (day - start).days / days
    number = source_number(label, decimal_comma)
    if number is None:
        return None
    if basis == "year":
        return number
    if basis == "age_bce":
        return 1 - number
    if basis in {"age_bp", "age_ka_bp"} and reference_year is not None:
        return reference_year - number * (1000 if basis == "age_ka_bp" else 1)
    return None


def source_year_axis(value, basis, reference_year=None, decimal_comma=False, date_order=None, year_range=None):
    """Return the converted year and the last reported digit's step in years."""
    year = year_ce(value, basis, reference_year, decimal_comma, date_order, year_range=year_range)
    if year is None:
        return None, None
    label = str(value).strip()
    if basis == "year":
        label = _CE_LABEL.sub("", label)
        if calendar_date(label, date_order, year_range=year_range):
            return year, 0.0
    if decimal_comma:
        label = label.replace(",", ".")
    if not _NUMERIC_AGE.fullmatch(label):
        return year, None
    exponent = Decimal(label).as_tuple().exponent + (3 if basis == "age_ka_bp" else 0)
    return year, 10.0 ** min(308, max(-323, exponent))


def select_year_axis(axes):
    """Select the finest compatible axis. Source order breaks precision ties."""
    valid = [(index, year, step) for index, (year, step) in enumerate(axes) if year is not None]
    if not valid:
        return None
    preferred, year, _ = min(valid, key=lambda axis: float("inf") if axis[2] is None else axis[2])
    known = [(other, step / 2) for _, other, step in valid if step is not None]
    if known:
        lower = max(other - radius for other, radius in known)
        upper = min(other + radius for other, radius in known)
        if lower - upper > max(ulp(lower), ulp(upper)) * 4:
            return None
    if any(step is None and abs(other - year) > 0.01 for _, other, step in valid):
        return None
    return preferred


def verified_padded_year_step(peer_values, padded_values, read_peer, read_padded):
    """Accept a coarser step only when the entire padded axis is reproduced by rounding."""
    samples = []
    for raw_peer, raw_padded in zip(peer_values, padded_values):
        peer, peer_step = read_peer(raw_peer)
        year, step = read_padded(raw_padded)
        if peer is None or year is None:
            continue
        digits = re.fullmatch(r"[+-]?\d*[.,](\d+)", str(raw_padded).strip())
        if not isinstance(raw_padded, str) or not digits or not step or peer_step is None:
            return None
        fraction = digits.group(1)
        zeros = len(fraction) - len(fraction.rstrip("0"))
        if not zeros:
            return None
        samples.append((peer, peer_step, year, step, zeros))
    if len({year for _, _, year, _, _ in samples}) < 4 or len({step for _, _, _, step, _ in samples}) != 1:
        return None
    step = samples[0][3] * (10.0 ** min(15, min(zeros for _, _, _, _, zeros in samples)))
    if any(peer_step >= step or abs(peer - year) > step / 2 + 4 * max(ulp(peer), ulp(year))
           for peer, peer_step, year, _, _ in samples):
        return None
    if all(select_year_axis(((peer, peer_step), (year, displayed))) is not None
           for peer, peer_step, year, displayed, _ in samples):
        return None
    return step


def sampling_profile(year_values):
    """按整条已换算的年代轴判断采样间隔，保留非规则和样本不足状态。"""
    years = sorted({number for value in year_values if (number := finite_number(value)) is not None})
    gaps = [end - start for start, end in zip(years, years[1:]) if end - start > 1e-8]
    interval = median(gaps) if gaps else None
    if len(gaps) < 3:
        return "unknown", interval
    bands = (
        ("submonthly", 0, 1 / 24),
        ("monthly", 1 / 24, 0.125), ("bimonthly", 0.125, 0.208),
        ("seasonal", 0.208, 0.375), ("biannual", 0.375, 0.75),
        ("annual", 0.75, 1.5),
    )
    for label, low, high in bands:
        if low <= interval < high and sum(low <= gap < high for gap in gaps) / len(gaps) >= 0.7:
            return label, interval
    return "irregular", interval


class SpatiotemporalStandardizationModule:
    """时空标准化层模块"""
    
    def __init__(self):
        # 时间分辨率映射
        self.resolution_mapping = {
            "monthly": "monthly",
            "月": "monthly",
            "monthly_uneven": "monthly_uneven",
            "月度非均匀": "monthly_uneven",
            "annual": "annual",
            "年": "annual",
            "yearly": "annual",
            "bimonthly": "bimonthly",
            "双月": "bimonthly",
            "seasonal": "seasonal",
            "季度": "seasonal",
            "biannual": "biannual",
            "semi-annual": "biannual",
            "半年": "biannual"
        }
        
        # 全新世时段映射
        self.holocene_stage_mapping = {
            "早全新世": "早全新世",
            "中全新世": "中全新世",
            "晚全新世": "晚全新世",
            "CE": "CE",
            "Common Era": "CE",
            "现代": "modern"
        }
        
        # 全新世时段时间范围定义（CE年份）
        self.holocene_stage_ranges = {
            "早全新世": (-10050, -6050),
            "中全新世": (-6050, -2050),
            "晚全新世": (-2050, 0),
            "CE": (0, date.today().year),
            "modern": (0, date.today().year)
        }
    
    def standardize_time_dimension(self, data_point: Dict[str, Any], table_metadata: Dict[str, Any]) -> Dict[str, Any]:
        """
        时间维度标准化
        
        Args:
            data_point (Dict[str, Any]): 数据点
            table_metadata (Dict[str, Any]): 表格元数据
            
        Returns:
            Dict[str, Any]: 时间维度标准化后的数据点
        """
        standardized_point = data_point.copy()
        
        # 处理年份字段
        if "year" in data_point:
            year = data_point["year"]
            # 如果是BP格式，转换为CE格式
            if isinstance(year, str) and "BP" in year.upper():
                try:
                    # 【修复】保留小数部分，改为float而非int
                    bp_year = float(year.upper().replace("BP", "").strip())
                    # 假设基准年为1950年
                    ce_year = 1950 - bp_year
                    standardized_point["year"] = ce_year
                    # 添加年代学误差标注字段
                    standardized_point["paleoData_age_units"] = "yr CE"
                except:
                    pass  # 保持原始值
            elif isinstance(year, str):
                # 处理BCE/BC格式
                # 【修复】修改正则表达式支持小数，改为float而非int
                bce_match = re.search(r'(\d+\.?\d*)\s*(BCE?|BC)', year.upper())
                if bce_match:
                    try:
                        bce_year = float(bce_match.group(1))
                        # BCE年份转换为负整数（无0 CE）
                        ce_year = -(bce_year - 1)
                        standardized_point["year"] = ce_year
                        standardized_point["paleoData_age_units"] = "yr CE"
                    except:
                        pass
                else:
                    # 处理其他字符串格式的年份
                    try:
                        # 移除常见的后缀
                        clean_year = year.replace("CE", "").replace("AD", "").strip()
                        year_val = float(clean_year)
                        # 确保年份在合理范围内
                        if -10050 <= year_val <= 2100:
                            # 保留原始年份的浮点数格式，不要转换为整数
                            standardized_point["year"] = year_val
                            standardized_point["paleoData_age_units"] = "yr CE"
                    except:
                        pass  # 保持原始值
            elif isinstance(year, (int, float)):
                # 确保年份在合理范围内
                if -10050 <= year <= 2100:
                    # 保留原始年份的浮点数格式，不要转换为整数
                    standardized_point["year"] = float(year)
                    standardized_point["paleoData_age_units"] = "yr CE"
        
        # 补全年份范围字段
        if "year" in standardized_point:
            year = standardized_point["year"]
            if "time_oldestYear" not in standardized_point:
                standardized_point["time_oldestYear"] = year
            if "time_youngestYear" not in standardized_point:
                standardized_point["time_youngestYear"] = year
        
        # 处理时间分辨率
        resolution = table_metadata.get("resolution", "unknown")
        standardized_resolution = self.resolution_mapping.get(resolution, resolution)
        standardized_point["hasResolution_nominal"] = standardized_resolution
        
        # 根据年份自动推断全新世时段
        if "year" in standardized_point and "paleoData_holocene_stage" not in standardized_point:
            year = standardized_point["year"]
            # 确保year是数值类型
            try:
                year_val = float(year)
                for stage, (start, end) in self.holocene_stage_ranges.items():
                    if start <= year_val <= end:
                        standardized_point["paleoData_holocene_stage"] = stage
                        break
            except (ValueError, TypeError):
                # 如果无法转换为数值，则跳过时段推断
                pass
        
        return standardized_point
    
    def standardize_spatial_dimension(self, data_point: Dict[str, Any], complemented_metadata: Dict[str, Any]) -> Dict[str, Any]:
        """
        空间维度标准化
        
        Args:
            data_point (Dict[str, Any]): 数据点
            complemented_metadata (Dict[str, Any]): 补充的元数据
            
        Returns:
            Dict[str, Any]: 空间维度标准化后的数据点
        """
        standardized_point = data_point.copy()
        
        # 从补充元数据中获取空间信息
        site_name = complemented_metadata.get("geo_siteName", "")
        latitude = complemented_metadata.get("geo_latitude")
        longitude = complemented_metadata.get("geo_longitude")
        
        # 如果数据点中没有空间信息，则使用补充的元数据
        if not standardized_point.get("geo_siteName") and site_name:
            standardized_point["geo_siteName"] = site_name
            
        if not standardized_point.get("geo_latitude") and latitude is not None:
            try:
                # 转换坐标格式
                lat = convert_coordinate_format(str(latitude))
                # 确保纬度在[-90, 90]范围内
                if -90 <= lat <= 90:
                    standardized_point["geo_latitude"] = round(float(lat), 6)  # 保留6位小数
                else:
                    logging.error(f"纬度超出范围: {latitude}")
                    standardized_point["geo_latitude"] = None  # 标记为无效而非默认值
            except Exception as e:
                logging.error(f"纬度转换失败: {latitude}, 错误: {e}")
                standardized_point["geo_latitude"] = None
                
        if not standardized_point.get("geo_longitude") and longitude is not None:
            try:
                # 转换坐标格式
                lon = convert_coordinate_format(str(longitude))
                lon = float(lon)
                # 确保经度在[-180, 180]范围内
                if -180 <= lon <= 180:
                    standardized_point["geo_longitude"] = round(lon, 6)  # 保留6位小数
                    # 转换为[-180, 180]范围（如181°E → -179）
                    if lon > 180:
                        standardized_point["geo_longitude"] = round(lon - 360, 6)
                    elif lon < -180:
                        standardized_point["geo_longitude"] = round(lon + 360, 6)
                else:
                    logging.error(f"经度超出范围: {longitude}")
                    standardized_point["geo_longitude"] = None  # 标记为无效而非默认值
            except Exception as e:
                logging.error(f"经度转换失败: {longitude}, 错误: {e}")
                standardized_point["geo_longitude"] = None
        
        # 验证坐标有效性
        lat = standardized_point.get("geo_latitude")
        lon = standardized_point.get("geo_longitude")
        if lat is not None and lon is not None:
            try:
                # 确保坐标是数值类型
                lat_val = float(lat)
                lon_val = float(lon)
                # 验证坐标范围
                if validate_coordinates(lat_val, lon_val):
                    standardized_point["geo_latitude"] = lat_val
                    standardized_point["geo_longitude"] = lon_val
                else:
                    logging.warning(f"坐标超出有效范围: ({lat_val}, {lon_val})")
                    # 移除无效坐标
                    standardized_point.pop("geo_latitude", None)
                    standardized_point.pop("geo_longitude", None)
            except Exception as e:
                logging.warning(f"坐标验证失败: {str(e)}")
                # 移除无效坐标
                standardized_point.pop("geo_latitude", None)
                standardized_point.pop("geo_longitude", None)
        
        # 处理补充的元数据字段
        # 处理测年方法
        dating_method = complemented_metadata.get("paleoData_dating_method")
        if dating_method and standardized_point.get("paleoData_dating_method") is None:
            standardized_point["paleoData_dating_method"] = dating_method
        
        # 处理测年误差
        dating_error = complemented_metadata.get("paleoData_dating_error")
        if dating_error and standardized_point.get("paleoData_dating_error") is None:
            standardized_point["paleoData_dating_error"] = dating_error
        
        # 处理全新世时段
        holocene_stage = complemented_metadata.get("paleoData_holocene_stage")
        if holocene_stage and standardized_point.get("paleoData_holocene_stage") is None:
            standardized_stage = self.holocene_stage_mapping.get(holocene_stage, holocene_stage)
            standardized_point["paleoData_holocene_stage"] = standardized_stage
        
        # 处理检测方法
        testing_method = complemented_metadata.get("testing_method")
        if testing_method and standardized_point.get("testing_method") is None:
            standardized_point["testing_method"] = testing_method
            
        # 添加海洋盆地字段（如果缺失）
        if "geo_ocean" not in standardized_point:
            standardized_point["geo_ocean"] = self._infer_ocean_basin(standardized_point)
            
        # 添加礁体类型字段（如果缺失）
        if "geo_reefType" not in standardized_point:
            standardized_point["geo_reefType"] = "unknown"
            
        # 添加是否近岸字段（如果缺失）
        if "geo_coastal" not in standardized_point:
            standardized_point["geo_coastal"] = "unknown"
            
        # 添加水深字段（如果缺失）
        if "geo_depth" not in standardized_point:
            standardized_point["geo_depth"] = "unknown"
        
        return standardized_point
    
    def _infer_ocean_basin(self, data_point: Dict[str, Any]) -> str:
        """
        根据经纬度推断海洋盆地
        
        Args:
            data_point (Dict[str, Any]): 数据点
            
        Returns:
            str: 海洋盆地名称
        """
        lat = data_point.get("geo_latitude")
        lon = data_point.get("geo_longitude")
        
        if lat is None or lon is None:
            return "unknown"
            
        try:
            lat = float(lat)
            lon = float(lon)
            
            # 简单的海洋盆地推断规则
            if -90 <= lat <= -60:
                return "Southern Ocean"
            elif -60 < lat < 60:
                if -180 <= lon < -80:
                    return "Pacific Ocean"
                elif -80 <= lon < 20:
                    return "Atlantic Ocean"
                elif 20 <= lon <= 180:
                    return "Indian Ocean"
            elif 60 <= lat <= 90:
                return "Arctic Ocean"
                
            return "unknown"
        except:
            return "unknown"
