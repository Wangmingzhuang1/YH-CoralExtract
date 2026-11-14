#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
质量控制层模块
负责数据质量检查和异常值标记
处理dataset子文件夹中的PDF正文和对应附件（Excel/Word），确保Excel转换为CSV
当附录中没有地点名称、经纬度或单位时，从PDF正文中获取
"""

import logging
from typing import Dict, Any, List
import pandas as pd
from datetime import date
from app.core.moa.unit_converter import UnitConversionModule, finite_number

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class QualityControlModule:
    """质量控制层模块"""
    
    def __init__(self):
        # 数据合理性范围定义（按全新世时段分段）
        # 早/中全新世范围
        self.valid_ranges_early_mid = {
            "Sr/Ca_value": (7.8, 10.2),      # 锶钙比合理范围 (mmol/mol)
            "δ18O_value": (-6.0, 4.0),       # 氧同位素合理范围 (‰ VPDB)
            "δ13C_value": (-15.0, 5.0),      # 碳同位素合理范围 (‰)
            "sea_surface_temperature_value": (-2.0, 35.0)  # 海表温度合理范围 (°C)
        }
        
        # 晚全新世/CE范围
        self.valid_ranges_late = {
            "Sr/Ca_value": (8.0, 10.0),      # 锶钙比合理范围 (mmol/mol)
            "δ18O_value": (-6.0, 3.0),       # 氧同位素合理范围 (‰ VPDB)
            "δ13C_value": (-15.0, 5.0),      # 碳同位素合理范围 (‰)
            "sea_surface_temperature_value": (-2.0, 35.0)  # 海表温度合理范围 (°C)
        }
        
        # 质量控制标志定义
        self.qc_flags = {
            0: "已检查，未发现规则定义的问题",
            1: "提示",
            2: "统计异常或时段不一致，保留观测值",
            3: "超出检查范围或格式异常",
            4: "单位、来源或关键元数据待复核",
            5: "严重错误（次生方解石 / 伪造）"
        }
        
        # 全新世时段定义
        self.holocene_stages = ["早全新世", "中全新世", "晚全新世", "CE", "现代", "modern", "Late Holocene", "中世纪气候异常期", "小冰期", "LIA", "Medieval Climate Anomaly"]
        # 全新世时段的别名映射
        self.holocene_stage_aliases = {
            "中世纪气候异常期（Medieval Climate Anomaly）": "Medieval Climate Anomaly",
            "中世纪气候异常期": "Medieval Climate Anomaly",
            "小冰期（LIA）": "LIA",
            "晚全新世": "晚全新世",
            "现代": "modern",
            "全新世晚期": "晚全新世",
            "Late Holocene": "晚全新世",
            "Early Holocene": "早全新世",
            "Middle Holocene": "中全新世",
            "Mid-Holocene": "中全新世"
        }
        
        # 全新世时段时间范围定义（CE年份）
        self.holocene_stage_ranges = {
            "早全新世": (-10050, -6050),
            "中全新世": (-6050, -2050),
            "晚全新世": (-2050, 0),
            "CE": (0, date.today().year),
            "modern": (0, date.today().year)
        }
    
    def check_data_quality(self, data_point: Dict[str, Any]) -> Dict[str, Any]:
        """
        数据质量检查
        
        Args:
            data_point (Dict[str, Any]): 数据点
            
        Returns:
            Dict[str, Any]: 包含质量控制信息的数据点
        """
        qc_point = data_point.copy()
        previous_log = data_point.get("qc_log")
        qc_logs = [previous_log] if previous_log and previous_log != "数据质量正常" else []
        max_qc_flag = data_point.get("qc_flag", 0)
        for key, status in data_point.items():
            if key.endswith("_unit_status") and status in {"unknown_unit", "unsupported_unit", "unit_conflict"}:
                qc_logs.append(f"{key.removesuffix('_unit_status')} 的源单位待复核")
                max_qc_flag = max(max_qc_flag, 4)
        
        missing_fields = [
            field for field in ("paper_hash", "geo_siteName")
            if str(data_point.get(field) or "").strip().lower() in {"", "-", "unknown", "未明确"}
        ]
        if finite_number(data_point.get("year")) is None:
            missing_fields.append("year")
        for field in ("geo_latitude", "geo_longitude"):
            if finite_number(data_point.get(field)) is None:
                missing_fields.append(field)
        if missing_fields:
            qc_logs.append(f"缺少关键字段: {missing_fields}")
            max_qc_flag = max(max_qc_flag, 4)

        holocene_stage = self.holocene_stage_aliases.get(
            data_point.get("paleoData_holocene_stage"), data_point.get("paleoData_holocene_stage")
        )
        valid_ranges = (self.valid_ranges_early_mid if holocene_stage in {"早全新世", "中全新世"}
                        else self.valid_ranges_late)

        for field in UnitConversionModule.default_units:
            value = finite_number(data_point.get(field))
            unit_status = data_point.get(f"{field}_unit_status")
            bounds = valid_ranges.get(field)
            if unit_status not in {None, "verified"}:
                field_status = unit_status
            elif value is None:
                field_status = "missing_or_invalid_value"
            elif unit_status is None:
                field_status = "unit_not_checked"
            elif bounds and not bounds[0] <= value <= bounds[1]:
                field_status = "outside_range"
                qc_logs.append(f"{field}值 {value} 超出合理范围 {bounds}")
                max_qc_flag = max(max_qc_flag, 3)
            elif field == "δ18O_value" and abs(value) < 0.5:
                # 珊瑚骨骼 δ18O (VPDB) 物理上不可能落在 ±0.5‰ 内；近零值通常是
                # 中心化/距平序列（如 "centered by removing the 1850-1995 mean"）
                # 或占位零，物理范围检查(-6~+3)无法捕获，单独拦截送复核
                field_status = "suspect_centered_series"
                qc_logs.append(f"δ18O_value值 {value} 近零（疑似中心化/距平序列或占位零，待复核）")
                max_qc_flag = max(max_qc_flag, 4)
            else:
                field_status = "pass"
            qc_point[f"{field}_qc_status"] = field_status

        year = finite_number(data_point.get("year"))
        qc_point["qc_time_status"] = "pass" if year is not None and -10050 <= year < date.today().year + 1 else "invalid"
        if year is not None and qc_point["qc_time_status"] == "invalid":
            qc_logs.append(f"年份 {year} 超出检查范围")
            max_qc_flag = max(max_qc_flag, 3)

        latitude = finite_number(data_point.get("geo_latitude"))
        longitude = finite_number(data_point.get("geo_longitude"))
        if latitude is None or longitude is None:
            qc_point["qc_location_status"] = "missing"
        elif -90 <= latitude <= 90 and -180 <= longitude <= 180:
            qc_point["qc_location_status"] = "pass"
        else:
            qc_point["qc_location_status"] = "invalid"
            qc_logs.append(f"经纬度 ({latitude}, {longitude}) 超出有效范围")
            max_qc_flag = max(max_qc_flag, 3)

        # 检查全新世时段合理性
        holocene_stage = data_point.get("paleoData_holocene_stage")
        if holocene_stage:
            # 标准化全新世时段描述
            normalized_stage = self.holocene_stage_aliases.get(holocene_stage, holocene_stage)
            # 更新数据点中的全新世时段
            qc_point["paleoData_holocene_stage"] = normalized_stage
            
            # 检查是否在有效范围内
            if normalized_stage not in self.holocene_stages:
                # 对于现代数据，降低QC标志级别
                if normalized_stage and ("现代" in normalized_stage or "modern" in normalized_stage.lower()):
                    max_qc_flag = max(max_qc_flag, 1)  # 轻微异常
                else:
                    qc_logs.append(f"全新世时段 {holocene_stage} 不在有效范围内 {self.holocene_stages}")
                    max_qc_flag = max(max_qc_flag, 2)
        
        # 验证年份与全新世时段的一致性
        year = data_point.get("year")
        holocene_stage = data_point.get("paleoData_holocene_stage")
        if year is not None and holocene_stage:
            try:
                # 【修复】使用float而非int，保留小数部分
                year_float = float(year)
                # 获取时段范围
                stage_range = self.holocene_stage_ranges.get(self.holocene_stage_aliases.get(holocene_stage, holocene_stage))
                if stage_range and not (stage_range[0] <= year_float <= stage_range[1]):
                    qc_logs.append(f"年份 {year_float} 与全新世时段 {holocene_stage} 不一致")
                    max_qc_flag = max(max_qc_flag, 2)
            except:
                pass
        
        # 添加质量控制信息
        qc_point["qc_flag"] = max_qc_flag
        qc_point["qc_description"] = self.qc_flags.get(max_qc_flag, "未知")
        qc_point["qc_log"] = "; ".join(qc_logs) if qc_logs else "数据质量正常"
        
        return qc_point
    
    def generate_quality_report(self, data_points: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        生成数据质量报告
        
        Args:
            data_points (List[Dict[str, Any]]): 数据点列表
            
        Returns:
            Dict[str, Any]: 质量报告
        """
        report = {
            "total_count": len(data_points),
            "qc_distribution": {},
            "issues_summary": [],
            "statistics_by_stage": {}
        }
        
        # 统计QC标志分布
        qc_counts = {}
        issues = []
        
        # 按全新世时段分类统计
        stage_stats = {}
        
        for point in data_points:
            qc_flag = point.get("qc_flag", 0)
            qc_counts[qc_flag] = qc_counts.get(qc_flag, 0) + 1
            
            # 记录有问题的数据
            if qc_flag > 0:
                issues.append({
                    "paper_hash": point.get("paper_hash", "unknown"),
                    "source_file": point.get("source_file"),
                    "source_row": point.get("source_row"),
                    "sample_id": point.get("sample_id"),
                    "qc_flag": qc_flag,
                    "qc_description": point.get("qc_description", ""),
                    "qc_log": point.get("qc_log", "")
                })
            
            # 按时段统计
            stage = point.get("paleoData_holocene_stage", "unknown")
            if stage not in stage_stats:
                stage_stats[stage] = {"count": 0, "flags": {}}
            stage_stats[stage]["count"] += 1
            stage_stats[stage]["flags"][qc_flag] = stage_stats[stage]["flags"].get(qc_flag, 0) + 1
        
        # 构造QC分布统计
        for flag, count in qc_counts.items():
            report["qc_distribution"][flag] = {
                "count": count,
                "description": self.qc_flags.get(flag, "未知"),
                "percentage": round(count / len(data_points) * 100, 2) if data_points else 0
            }
        
        # 构造时段统计
        for stage, stats in stage_stats.items():
            flag_details = {}
            for flag, count in stats["flags"].items():
                flag_details[flag] = {
                    "count": count,
                    "description": self.qc_flags.get(flag, "未知"),
                    "percentage": round(count / stats["count"] * 100, 2) if stats["count"] > 0 else 0
                }
            report["statistics_by_stage"][stage] = {
                "count": stats["count"],
                "flag_distribution": flag_details
            }
        
        report["issues_summary"] = issues
        return report
