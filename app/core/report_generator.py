#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time : 2025/11/10 17:10
# @Author : 桐
# @QQ:1041264242
# 注意事项：

import os
import json
import matplotlib.pyplot as plt
import seaborn as sns
from datetime import datetime
from app.utils.config_loader import load_target_data_config
from app.utils.spatio_temporal_utils import load_spatio_temporal_config

def generate_data_summary(standardized_data, target_data):
    """生成数据汇总统计（按类型）"""
    summary = {}
    for data in target_data:
        data_name = data["name"]
        value_key = f"{data_name.lower().replace(' ', '_')}_value"
        # 统计该类型的有效数据点数量
        count = sum(1 for point in standardized_data if value_key in point)
        summary[data_name] = {
            "有效数据点": count,
            "单位": data["units"][0]
        }
    return summary

def generate_spatio_temporal_report(standardized_data, save_path):
    """生成时空对齐报告"""
    config = load_spatio_temporal_config()
    
    # 1. 时间覆盖热力图数据准备
    time_coverage = {}
    ocean_basins = {}
    
    for point in standardized_data:
        min_year = point.get("time_oldestYear", 0)
        max_year = point.get("time_youngestYear", 0)
        ocean = point.get("geo_ocean", "Unknown")
        
        if min_year and max_year:
            # 按世纪分组统计
            century_start = (min_year // 100) * 100
            century_end = (max_year // 100) * 100
            
            for century in range(century_start, century_end + 100, 100):
                if century not in time_coverage:
                    time_coverage[century] = {}
                if ocean not in time_coverage[century]:
                    time_coverage[century][ocean] = 0
                time_coverage[century][ocean] += 1
        
        # 统计海洋盆地分布
        if ocean not in ocean_basins:
            ocean_basins[ocean] = 0
        ocean_basins[ocean] += 1
    
    # 2. 空间分布数据准备
    site_distribution = []
    reproducibility_stats = {"合格": 0, "不合格": 0}
    
    for point in standardized_data:
        lat = point.get("geo_latitude", 0)
        lon = point.get("geo_longitude", 0)
        site_distribution.append((lat, lon))
        
        # 统计重现性合格情况（简化实现）
        if point.get("spatial_consistency"):
            if "合格" in point["spatial_consistency"]:
                reproducibility_stats["合格"] += 1
            else:
                reproducibility_stats["不合格"] += 1
    
    # 3. 时空对齐异常统计
    coordinate_errors = sum(1 for point in standardized_data if point.get("quality_flags", {}).get("metadata_errors") and 
                           any("坐标" in str(error) for error in point["quality_flags"]["metadata_errors"]))
    time_range_errors = sum(1 for point in standardized_data if point.get("quality_flags", {}).get("metadata_errors") and 
                           any("时间" in str(error) for error in point["quality_flags"]["metadata_errors"]))
    
    # 生成报告内容
    report_content = f"""# 时空对齐报告

## 时间覆盖统计
| 世纪 | """
    
    # 获取所有海洋盆地
    all_oceans = set()
    for century_data in time_coverage.values():
        all_oceans.update(century_data.keys())
    
    # 添加海洋盆地表头
    for ocean in sorted(all_oceans):
        report_content += f"{ocean} | "
    report_content += "\n|------|"
    for _ in all_oceans:
        report_content += "-----|"
    report_content += "\n"
    
    # 添加统计数据
    for century in sorted(time_coverage.keys()):
        report_content += f"| {century}s | "
        for ocean in sorted(all_oceans):
            count = time_coverage[century].get(ocean, 0)
            report_content += f"{count} | "
        report_content += "\n"
    
    report_content += f"""
## 空间分布统计
| 海洋盆地 | 记录数 |
|---------|-------|
"""
    for ocean, count in sorted(ocean_basins.items()):
        report_content += f"| {ocean} | {count} |\n"
    
    report_content += f"""
## 重现性统计
| 状态 | 记录数 |
|-----|-------|
| 合格 | {reproducibility_stats['合格']} |
| 不合格 | {reproducibility_stats['不合格']} |

## 时空对齐异常统计
| 异常类型 | 数量 |
|---------|-----|
| 坐标异常 | {coordinate_errors} |
| 时间范围不匹配 | {time_range_errors} |
"""
    
    with open(save_path, "w", encoding="utf-8") as f:
        f.write(report_content)
    print(f"\033[1;32m | INFO     | 时空对齐报告已保存到：{save_path} \033[0m")

def generate_verify_report(verification_results, hypothesis_data, save_path, standardized_data=None):
    """生成验证报告（扩展版本，支持数据统计）"""
    # 基本报告内容
    report_content = f"""# 假设验证报告
## 基本信息
- 验证主题：{hypothesis_data['keyword']}
- 假设来源文件：{os.path.basename(save_path).replace('verify_', '').replace('.md', '')}
- 假设生成时间：{hypothesis_data['generate_time']}
- 知识截止日期：{hypothesis_data['knowledge_cutoff_date']}
- 验证时间：{datetime.now().isoformat()}
- 搜索论文数量：{sum(len(res['parsed_papers']) for res in verification_results)}

## 验证结果详情
"""
    for res in verification_results:
        report_content += f"""
### 假设{res['hypothesis_id']}：{res['hypothesis_content']}
#### 验证结论
{res['verification_result']}

---
"""
    
    # 如果提供了标准化数据，则添加数据统计章节
    if standardized_data is not None:
        target_data = load_target_data_config()
        summary = generate_data_summary(standardized_data, target_data)
        
        report_content += "\n## 数据统计\n"
        report_content += "| 数据类型 | 有效数据点 | 单位 |\n"
        report_content += "|---------|-----------|------|\n"
        for data_name, stats in summary.items():
            report_content += f"| {data_name} | {stats['有效数据点']} | {stats['单位']} |\n"
        
        # 添加数据来源统计
        paper_count = len(set(point["paper_hash"] for point in standardized_data))
        report_content += f"\n数据来源统计：共处理 {paper_count} 篇论文，提取 {len(standardized_data)} 个数据点\n"
        
        # 生成时空对齐报告
        spatio_temporal_report_path = save_path.replace(".md", "_时空对齐报告.md")
        generate_spatio_temporal_report(standardized_data, spatio_temporal_report_path)
    
    with open(save_path, "w", encoding="utf-8") as f:
        f.write(report_content)
    print(f"\033[1;32m | INFO     | 验证报告已保存到：{save_path} \033[0m")
    return save_path