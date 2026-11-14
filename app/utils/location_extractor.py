#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
地理位置信息提取工具模块
从PDF文本中提取研究地点的地理位置信息，支持多个研究地点
"""

import re
import logging
from typing import Dict, Any, Optional

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def extract_location_from_pdf_text(text: str) -> Dict[str, Any]:
    """
    从PDF文本中提取地理位置信息，支持多个研究地点
    
    优先使用大模型提取，失败时回退到正则表达式方法。
    
    Args:
        text: PDF文本内容
        
    Returns:
        Dict[str, Any]: 包含以下字段的字典：
            - geo_siteName: 研究地点名称
            - geo_latitude: 纬度（十进制）
            - geo_longitude: 经度（十进制）
            - research_sites: 多个研究地点的坐标信息（可选）
    """
    # 清理 MinerU 源标记
    text = re.sub(r'^\[SOURCE:[A-Z_]+\]', '', text, count=1)
    location_info = {}
    
    # 首先尝试使用大模型提取地理位置信息
    try:
        from app.utils.llm_api import call_llm
        import json
        
        prompt = f"""
        请从以下学术论文文本中提取研究地点的地理位置信息：
        
        要求提取的信息包括：
        1. 研究地点名称 (geo_siteName)
        2. 纬度 (geo_latitude)，需识别"°N"（北纬，转换为正数）或"°S"（南纬，转换为负数）
        3. 经度 (geo_longitude)，需识别"°E"（东经，转换为正数）或"°W"（西经，转换为负数）
        
        如果文本中包含多个研究地点，请分别列出每个地点的完整信息和对应的经纬度。
        特别注意：如果文献中提到了多个珊瑚岩心采样点，请详细列出每个采样点的标识和坐标经纬度，每个年份段对应一个珊瑚样本（即一个研究地点）。
        
        文本内容：
        {text[:8000]}
        
        请以严格的JSON格式返回结果，示例格式：
        {{
          "geo_siteName": "Palmyra Atoll",
          "geo_latitude": 5.88,
          "geo_longitude": -162.08,
          "research_sites": {{
            "15WC24": {{
              "latitude": 19.39,
              "longitude": 110.76
            }},
            "11LW2": {{
              "latitude": 19.29,
              "longitude": 110.66
            }}
          }}
        }}
        
        注意：
        1. 确保经纬度为数值类型（带符号）
        2. 如果某些字段无法提取，请将其值设为null
        3. 只返回JSON格式的结果，不要包含其他文本
        4. 如果研究地点经纬度不是唯一一个，请仔细阅读整篇文献，确保不遗漏任何研究地点信息
        """
        
        result = call_llm("max", prompt)
        if result:
            # 清理可能的Markdown代码块标记
            result = result.strip()
            if result.startswith("```json"):
                result = result[8:]
            if result.endswith("```"):
                result = result[:-3]
            
            # 解析JSON结果
            llm_result = json.loads(result)
            location_info.update(llm_result)
            return location_info
    except Exception as e:
        logger.warning(f"使用大模型提取地理位置信息失败: {str(e)}")
    
    # 如果大模型提取失败，回退到正则表达式方法
    return _extract_location_with_regex(text)


def _extract_location_with_regex(text: str) -> Dict[str, Any]:
    """
    使用正则表达式提取地理位置信息（回退方法）
    
    Args:
        text: PDF文本内容
        
    Returns:
        Dict[str, Any]: 地理位置信息字典
    """
    location_info = {}
    
    # 提取站点名称
    site_patterns = [
        r'([A-Za-z\s\-]+(?:Islands?|Island|Atoll|Atolls|Reef|Reefs|Lagoon|Lagoons)[,\s]+[A-Za-z\s\-]+)',
        r'([A-Za-z\s\-]+(?:Coral\s+Reef|National\s+Park|Marine\s+Park)[,\s]+[A-Za-z\s\-]+)',
        r'([A-Za-z\s\-]+[,\s]+[A-Za-z\s\-]+(?:Ocean|Sea|Bay|Gulf))'
    ]
    
    text_sample = text[:3000]
    for pattern in site_patterns:
        site_matches = re.findall(pattern, text_sample, re.IGNORECASE)
        if site_matches:
            site_name = max(site_matches, key=len).strip()
            site_name = re.sub(r'[\n\r\t]+', ' ', site_name)
            site_name = re.sub(r'\s+', ' ', site_name)
            if len(site_name) > 10:
                location_info["geo_siteName"] = site_name
                break
    
    # 提取多个研究地点的经纬度信息
    research_sites = _extract_research_sites(text)
    if research_sites:
        location_info["research_sites"] = research_sites
    
    # 提取默认的经纬度信息（用于向后兼容）
    valid_coords = _extract_coordinates(text)
    if valid_coords:
        location_info["geo_latitude"] = valid_coords[0][0]
        location_info["geo_longitude"] = valid_coords[0][1]
    
    return location_info


def _extract_research_sites(text: str) -> Dict[str, Dict[str, float]]:
    """
    提取多个研究地点的坐标信息
    
    Args:
        text: PDF文本内容
        
    Returns:
        Dict[str, Dict[str, float]]: 研究地点坐标字典
    """
    research_sites = {}
    
    # 定义研究地点模式
    research_site_patterns = [
        r'((?:15WC24|11LW2|11QG1|13OC4)[\s\S]*?(?:19\.\d+°?[NS]|\d+\.\d+°?[NS])[\s\S]*?(?:110\.\d+°?[EW]|\d+\.\d+°?[EW]))',
        r'((?:现代珊瑚|LIA|MCA)[\s\S]*?(?:19\.\d+°?[NS]|\d+\.\d+°?[NS])[\s\S]*?(?:110\.\d+°?[EW]|\d+\.\d+°?[EW]))'
    ]
    
    for pattern in research_site_patterns:
        site_matches = re.findall(pattern, text, re.IGNORECASE | re.DOTALL)
        for match in site_matches:
            site_text = match
            site_id_match = re.search(r'(15WC24|11LW2|11QG1|13OC4|现代珊瑚|LIA|MCA)', site_text, re.IGNORECASE)
            if site_id_match:
                site_id = site_id_match.group(1)
                coord = _parse_coordinate_from_text(site_text)
                if coord:
                    research_sites[site_id] = coord
    
    return research_sites


def _extract_coordinates(text: str) -> list:
    """
    提取坐标信息
    
    Args:
        text: PDF文本内容
        
    Returns:
        list: 有效坐标列表 [(latitude, longitude), ...]
    """
    coord_patterns = [
        r'(\d+\.\d+)°?\s*([NS])\s*,?\s*(\d+\.\d+)°?\s*([EW])',  # 标准格式：数字°方向
        r'([NS])\s*(\d+\.\d+)°?\s*,?\s*([EW])\s*(\d+\.\d+)°?',  # 方向在前的格式
        r'([+-]?\d+\.\d+)\s*[°]?\s*,?\s*([+-]?\d+\.\d+)\s*[°]?'  # 简单十进制格式
    ]
    
    valid_coords = []
    
    for i, pattern in enumerate(coord_patterns):
        coord_matches = re.findall(pattern, text, re.IGNORECASE)
        for match in coord_matches:
            try:
                if i == 0:  # 标准格式
                    lat_str, lat_dir, lon_str, lon_dir = match
                    latitude = float(lat_str)
                    longitude = float(lon_str)
                    if lat_dir.upper() == 'S':
                        latitude = -latitude
                    if lon_dir.upper() == 'W':
                        longitude = -longitude
                elif i == 1:  # 方向在前的格式
                    lat_dir, lat_str, lon_dir, lon_str = match
                    latitude = float(lat_str)
                    longitude = float(lon_str)
                    if lat_dir.upper() == 'S':
                        latitude = -latitude
                    if lon_dir.upper() == 'W':
                        longitude = -longitude
                else:  # 简单十进制格式
                    lat_str, lon_str = match
                    latitude = float(lat_str)
                    longitude = float(lon_str)
                
                # 验证坐标范围
                if -90 <= latitude <= 90 and -180 <= longitude <= 180:
                    valid_coords.append((latitude, longitude))
                    break
            except ValueError:
                pass
        
        if valid_coords:
            break
    
    return valid_coords


def _parse_coordinate_from_text(text: str) -> Optional[Dict[str, float]]:
    """
    从文本中解析单个坐标
    
    Args:
        text: 包含坐标的文本
        
    Returns:
        Optional[Dict[str, float]]: 坐标字典或None
    """
    coord_patterns = [
        r'(\d+\.\d+)°?\s*([NS])\s*,?\s*(\d+\.\d+)°?\s*([EW])',
        r'([NS])\s*(\d+\.\d+)°?\s*,?\s*([EW])\s*(\d+\.\d+)°?',
    ]
    
    for coord_pattern in coord_patterns:
        coord_match = re.search(coord_pattern, text, re.IGNORECASE)
        if coord_match:
            try:
                if coord_pattern == coord_patterns[0]:
                    lat_str, lat_dir, lon_str, lon_dir = coord_match.groups()
                    latitude = float(lat_str)
                    longitude = float(lon_str)
                    if lat_dir.upper() == 'S':
                        latitude = -latitude
                    if lon_dir.upper() == 'W':
                        longitude = -longitude
                else:
                    lat_dir, lat_str, lon_dir, lon_str = coord_match.groups()
                    latitude = float(lat_str)
                    longitude = float(lon_str)
                    if lat_dir.upper() == 'S':
                        latitude = -latitude
                    if lon_dir.upper() == 'W':
                        longitude = -longitude
                
                if -90 <= latitude <= 90 and -180 <= longitude <= 180:
                    return {"latitude": latitude, "longitude": longitude}
            except ValueError:
                continue
    
    return None
















