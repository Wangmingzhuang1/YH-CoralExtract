#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
输出整合层模块
负责将处理后的数据整合为统一格式并输出
处理dataset子文件夹中的PDF正文和对应附件（Excel/Word），确保Excel转换为CSV
当附录中没有地点名称、经纬度或单位时，从PDF正文中获取
"""
import os
import csv
import json
import hashlib
import tempfile
import pandas as pd
import logging
from typing import Dict, Any, List
from datetime import datetime
from pathlib import Path
from app.utils.data_trace_logger import get_tracer
from app.utils.toolkit import filter_empty_key_fields
from app.core.moa.unit_converter import finite_number
from app.utils.review_queue import save_review_queue

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

INDICATOR_GROUPS = {
    (True, True, True): "G1_SrCa_O18_C13",
    (True, True, False): "G2_SrCa_O18",
    (False, True, True): "G3_O18_C13",
    (True, False, False): "G4_SrCa",
    (False, True, False): "G5_O18",
    (False, False, True): "G6_C13",
    (True, False, True): "G7_SrCa_C13",
}


def _read_combined(path):
    if os.path.isfile(path):
        with open(path, encoding="utf-8-sig", newline="") as stream:
            yield from csv.DictReader(stream)

class DataOutputModule:
    """输出整合层模块"""
    
    def __init__(self, output_dir: str = "output"):
        self.output_dir = output_dir
        os.makedirs(self.output_dir, exist_ok=True)
        
        # 时间分辨率映射
        self.resolution_mapping = {
            "annual": "annual",
            "yearly": "annual",
            "monthly": "monthly",
            "seasonal": "seasonal",
            "monthly_uneven": "monthly",
            "bimonthly": "bimonthly",
            "biannual": "biannual",
            "semi-annual": "biannual",
            "半年": "biannual",
            "discrete_samples": "discrete_samples",
            "submonthly": "submonthly",
            "irregular": "irregular",
            "unknown": "unknown"
        }
        
        # 分辨率标识映射
        self.resolution_labels = {
            "annual": "年尺度",
            "monthly": "月尺度",
            "seasonal": "季度尺度",
            "bimonthly": "双月尺度",
            "biannual": "半年尺度",
            "discrete_samples": "独立样品",
            "submonthly": "月内高频观测",
            "irregular": "不规则采样",
            "unknown": "分辨率未确定"
        }
        
        # 全新世时段映射
        self.holocene_stage_mapping = {
            "早全新世": "Early Holocene",
            "中全新世": "Middle Holocene",
            "晚全新世": "Late Holocene",
            "CE": "Common Era",
            "modern": "Modern"
        }
        
        # 初始化追踪器
        self.tracer = get_tracer()
    
    def integrate_and_output(self, processed_data, quality_report, metadata_summary, review_items=None,
                             source_inventory=None, paper_review=None):
        """所有必需文件写完后，一次性发布批次目录。"""
        data = filter_empty_key_fields(processed_data)
        run = datetime.now().strftime("run_%Y%m%d_%H%M%S_%f")
        destination = os.path.join(self.output_dir, run)
        with tempfile.TemporaryDirectory(prefix=".moa-stage-", dir=self.output_dir) as temporary:
            staged = os.path.join(temporary, "batch")
            os.mkdir(staged)
            if data:
                self._write_batch(staged, data, quality_report, metadata_summary)
            if review_items:
                save_review_queue(staged, review_items)
            if source_inventory:
                self._output_csv(source_inventory, os.path.join(staged, "input_manifest.csv"))
            if paper_review is not None:
                from app.review_workbench.paper_review import write_batch_review

                summary, decisions = paper_review
                write_batch_review(Path(staged) / "review_workbench", {**summary, "batch": run}, decisions)
            os.rename(staged, destination)
        logging.info("批次已发布: %s", destination)
        return destination

    def _write_batch(self, output_path, processed_data, quality_report, metadata_summary):
        # 记录最终输出数据
        if processed_data:
            output_fields = list(processed_data[0].keys()) if processed_data else []
            self.tracer.log_final_output(processed_data, output_fields)
        
        indicator_groups = self.write_observation_tables(output_path, processed_data)
        qc_folder = os.path.join(output_path, "quality_reports")
        os.makedirs(qc_folder, exist_ok=True)
        self._output_group_summary(indicator_groups, os.path.join(qc_folder, "group_summary.json"))
        self._output_metadata_csv(metadata_summary, os.path.join(output_path, "metadata_summary.csv"))
        self._output_lipd_format(processed_data, os.path.join(output_path, "paleoclimate_data.lpd"))
        netcdf_status = self._output_netcdf_format(processed_data, os.path.join(output_path, "paleoclimate_grid.nc"))
        quality_report = {**quality_report, "export_status": {"netcdf": netcdf_status, "csv": "written"}}
        self._output_json(quality_report, os.path.join(qc_folder, "qc_log.json"))

    def write_observation_tables(self, output_path, processed_data):
        """Write the consolidated observations, resolution views, and index together."""
        # 按时间分辨率分组数据
        grouped_data = self._group_data_by_resolution(processed_data)
        
        # 按指标分组（用于生成指标分级表）
        indicator_groups = self._group_data_by_indicators(processed_data)
        resolutions = {id(record): name for name, records in grouped_data.items() for record in records}
        
        # 【新增】按分辨率创建子文件夹并输出数据
        all_resolution_index = []  # 用于生成索引
        for resolution, data in grouped_data.items():
            if data and resolution != "hydrological":
                # 创建分辨率子文件夹
                resolution_folder = os.path.join(output_path, resolution)
                os.makedirs(resolution_folder, exist_ok=True)
                
                # 输出该分辨率的完整数据
                csv_filename = f"{resolution}_data.csv"
                csv_path = os.path.join(resolution_folder, csv_filename)
                self._output_csv_by_resolution(data, csv_path, resolution)
                
                # 输出该分辨率对应的指标分级表
                indicator_group_indices = self._group_data_by_indicators(data)
                for group_name, indices in indicator_group_indices.items():
                    group_data = [data[i] for i in indices if i < len(data)]
                    if group_data:
                        # 【新增】执行二次验证：确保数据真正屬于该组，不承誓单指标機混入
                        validated_group_data = self._validate_indicator_group(group_data, group_name)
                        if validated_group_data:
                            group_csv_path = os.path.join(resolution_folder, f"{group_name}.csv")
                            self._output_indicator_group(validated_group_data, group_csv_path, resolution)
                            all_resolution_index.append({
                                "resolution": resolution,
                                "indicator_group": group_name,
                                "file_path": f"{resolution}/{group_name}.csv",
                                "record_count": len(validated_group_data)
                            })
        
        # 【新增】创建consolidated文件夹并输出汇总表
        consolidated_folder = os.path.join(output_path, "consolidated")
        os.makedirs(consolidated_folder, exist_ok=True)
        
        # 输出汇总表
        combined_csv_path = os.path.join(consolidated_folder, "all_data_combined.csv")
        self._output_combined_data(processed_data, combined_csv_path, resolutions)
        
        index_path = os.path.join(output_path, "INDEX.csv")
        self._generate_index_table(output_path, index_path, all_resolution_index, grouped_data)
        return indicator_groups

    def _group_data_by_resolution(self, data: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        """使用记录的已判定分辨率，保留独立样品及未知分辨率。"""
        grouped = {key: [] for key in self.resolution_labels}
        grouped["hydrological"] = []
        for record in data:
            if "hydrological_signal" in record:
                grouped["hydrological"].append(record)
                continue
            resolution = self.resolution_mapping.get(record.get("hasResolution_nominal"), "unknown")
            grouped[resolution].append(record)
        return grouped

    def _generate_overview_data(self, grouped_data: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
        """生成总览表数据"""
        overview_data = []
        
        for resolution, data in grouped_data.items():
            # 跳过水文信号数据，因为它们单独作为一个结果表
            if resolution == "hydrological":
                continue
            
            for i, record in enumerate(data):
                # 创建总览记录，将单位信息直接包含在指标列名中
                overview_record = {
                    "data_id": f"{resolution}_{i+1:04d}",
                    "table_name": f"{resolution}_data.csv",
                    "data_status": "可用" if record.get("qc_flag", 0) < 4 else "剔除",
                    "Sr/Ca (mmol/mol)": record.get("Sr/Ca_value", "-"),
                    "δ18O (‰ VPDB)": record.get("δ18O_value", "-"),
                    "δ13C (‰)": record.get("δ13C_value", "-"),
                    "Δδ18O (‰ VPDB)": record.get("δ18O_difference", "-"),
                    "SST (°C)": record.get("sea_surface_temperature_value", "待计算"),
                    "year_CE": record.get("year", "-"),
                    "time_resolution": self.resolution_labels.get(resolution, resolution),
                    # 【移除】不再输出holocene_stage，判断是数据提取后的事情
                    "geo_latitude": record.get("geo_latitude", "-"),
                    "geo_longitude": record.get("geo_longitude", "-"),
                    "geo_siteName": record.get("geo_siteName", "-"),
                    "pub1_doi": record.get("pub1_doi", "-"),
                    "pub1_authors": record.get("pub1_authors", "-"),
                    "paleoData_dating_method": record.get("paleoData_dating_method", "-"),
                    "paleoData_dating_error": record.get("paleoData_dating_error", "-"),
                    "testing_method": record.get("testing_method", "-"),
                    "qc_flag": record.get("qc_flag", 0),
                    "data_source": f"{os.path.basename(record.get('source_file', ''))}_{record.get('sheet_name', '')}",
                    "paper_hash": record.get("paper_hash", "-")
                }
                overview_data.append(overview_record)
        
        return overview_data
    
    def _output_csv_by_resolution(self, data, file_path, resolution):
        rows = [
            self._csv_record(record, f"{resolution}_{index:04d}", self.resolution_labels[resolution])
            for index, record in enumerate(filter_empty_key_fields(data), 1)
        ]
        self._output_csv(rows, file_path)

    @staticmethod
    def _observation_id(record):
        source = record.get("source_file")
        identity = [os.path.normcase(os.path.abspath(source)) if source else None]
        identity += [record.get(key) for key in ("paper_hash", "sheet_name", "source_row",
                                                "source_record_id", "sample_id")]
        # 同岩芯不同处理版本（raw/interpolated）须区分观测身份；仅当 source_processing
        # 非空时追加——无版本记录保持原身份不变（增量回灌依赖原 ID 核对旧记录）。
        # 不加 source_columns：它随列重映射变化，会破坏回灌时同一观测的身份稳定性。
        if record.get("source_processing"):
            identity += [record["source_processing"]]
        if record.get("source_row") is None:
            identity += [record.get(key) for key in ("year", "Sr/Ca_value", "δ18O_value", "δ13C_value")]
        payload = json.dumps(identity, ensure_ascii=False, default=str, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _csv_record(self, record, data_id, resolution):
        """同一记录在不同 CSV 视图中保留同样的值、身份和来源。"""
        row = {
            "data_id": data_id,
            "observation_id": self._observation_id(record),
            "year_CE": finite_number(record.get("year")),
            "time_resolution": resolution,
            "geo_latitude": finite_number(record.get("geo_latitude")),
            "geo_longitude": finite_number(record.get("geo_longitude")),
        }
        for key in (
            "geo_siteName", "pub1_doi", "pub1_authors", "dataset_doi", "dataset_investigators",
            "paleoData_dating_method", "paleoData_dating_error", "testing_method", "paper_hash",
            "sample_id", "source_record_id", "source_core_id", "source_attachment", "source_file", "sheet_name",
            "source_row", "source_line", "source_sheet_name", "source_time_value", "source_time_column", "source_time_unit",
            "source_header_line", "source_page", "source_time_reference_year", "source_time_date_order",
            "source_processing", "record_type", "native_interval_years", "mapping_route", "layout_route",
            "sr_ca_reference_status", "sr_ca_reference_evidence", "metadata_source",
            "qc_time_status", "qc_location_status", "qc_flag", "qc_log",
            "geo_rep_latitude", "geo_rep_longitude", "geo_lat_span", "geo_lon_span",
            "geo_rep_status", "geo_location_source", "geo_location_evidence",
        ):
            row[key] = record.get(key)
        row["data_source"] = f"{os.path.basename(record.get('source_file') or '')}_{record.get('sheet_name', '')}"
        row["qc_outlier_flag"] = record.get("qc_outlier_flag", 0)
        row["qc_outlier_reason"] = record.get("qc_outlier_reason")
        row["source_columns"] = json.dumps(record.get("source_columns", {}), ensure_ascii=False)
        for key in ("source_column_positions", "source_table_bbox", "source_header_rows", "composite_evidence"):
            if record.get(key) is not None:
                row[key] = json.dumps(record[key], ensure_ascii=False)
        if record.get("metadata_evidence"):
            row["metadata_evidence"] = json.dumps(record["metadata_evidence"], ensure_ascii=False)
        if record.get("geo_bounds"):
            row["geo_bounds"] = json.dumps(record["geo_bounds"], ensure_ascii=False)
        for field, header in (
            ("Sr/Ca_value", "Sr/Ca (mmol/mol)"),
            ("Ba/Ca_value", "Ba/Ca (μmol/mol)"),
            ("δ18O_value", "δ18O (‰ VPDB)"),
            ("δ13C_value", "δ13C (‰)"),
            ("δ18O_difference", "Δδ18O (‰ VPDB)"),
            ("sea_surface_temperature_value", "SST (°C)"),
        ):
            value = finite_number(record.get(field))
            if value is not None:
                row[header] = value
            for suffix in ("original_value", "original_unit", "unit_status", "qc_status", "original_uncertainty", "uncertainty"):
                key = f"{field}_{suffix}"
                if key in record:
                    row[key] = record[key]
            evidence = record.get(f"{field}_unit_resolution", {}).get("evidence")
            if evidence:
                row[f"{field}_unit_evidence"] = json.dumps(evidence, ensure_ascii=False)
        return row

    def _output_hydrological_csv(self, data: List[Dict[str, Any]], file_path: str):
        """输出水文信号CSV格式数据"""
        # 根据需求，不输出水文信号数据
        logging.info("根据需求，不输出水文信号数据")
        return
    
    def _output_csv(self, data: List[Dict[str, Any]], file_path: str):
        """输出CSV格式数据"""
        try:
            # 将数据转换为DataFrame
            df = pd.DataFrame(data)
            # 保存为CSV，使用utf-8-sig编码以避免Excel中的中文乱码
            df.to_csv(file_path, index=False, encoding='utf-8-sig')
            logging.info(f"CSV数据已保存到: {file_path}")
        except Exception as e:
            logging.error(f"保存CSV数据失败: {str(e)}")
            raise
    
    def _output_json(self, data: Any, file_path: str):
        """输出JSON格式数据"""
        try:
            # 处理可能包含Timestamp对象的数据
            def convert_timestamps(obj):
                if isinstance(obj, pd.Timestamp):
                    return obj.isoformat()
                elif isinstance(obj, dict):
                    return {key: convert_timestamps(value) for key, value in obj.items()}
                elif isinstance(obj, list):
                    return [convert_timestamps(item) for item in obj]
                return obj
            
            cleaned_data = convert_timestamps(data)
            
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(cleaned_data, f, ensure_ascii=False, indent=2)
            logging.info(f"JSON数据已保存到: {file_path}")
        except Exception as e:
            logging.error(f"保存JSON数据失败: {str(e)}")
            raise
    
    def _output_lipd_format(self, data: List[Dict[str, Any]], file_path: str):
        """输出LiPD格式数据"""
        try:
            # 构造LiPD格式数据
            lipd_data = {
                "paleoData": [],
                "geo": [],
                "chronData": [],
                "metadata": {
                    "title": "古气候珊瑚数据集",
                    "description": "标准化处理的珊瑚古气候数据，包含锶钙比、氧同位素等指标",
                    "version": "1.0",
                    "date": datetime.now().isoformat(),
                    "format_status": "项目 JSON 结构，尚未通过外部 LiPD 格式验证"
                }
            }
            
            # 转换数据为LiPD格式
            for record in data:
                # paleoData部分
                paleo_entry = {
                    "measurementTable": {
                        "columns": []
                    }
                }
                if record.get("composite_evidence"):
                    paleo_entry["compositeEvidence"] = record["composite_evidence"]
                if record.get("record_type") == "published_composite":
                    paleo_entry["recordType"] = record["record_type"]
                
                # 添加锶钙比数据
                if "Sr/Ca_value" in record:
                    paleo_entry["measurementTable"]["columns"].append({
                        "column_name": "Sr/Ca",
                        "values": [record["Sr/Ca_value"]],
                        "units": record.get("Sr/Ca_unit", "mmol/mol"),
                        "description": "锶钙比"
                    })
                
                # 添加氧同位素数据
                if "δ18O_value" in record:
                    paleo_entry["measurementTable"]["columns"].append({
                        "column_name": "δ18O",
                        "values": [record["δ18O_value"]],
                        "units": record.get("δ18O_unit", "‰ VPDB"),
                        "description": "氧同位素"
                    })
                
                # 添加碳同位素数据
                if "δ13C_value" in record:
                    paleo_entry["measurementTable"]["columns"].append({
                        "column_name": "δ13C",
                        "values": [record["δ13C_value"]],
                        "units": record.get("δ13C_unit", "‰"),
                        "description": "碳同位素"
                    })
                
                # 添加温度数据
                if "sea_surface_temperature_value" in record:
                    paleo_entry["measurementTable"]["columns"].append({
                        "column_name": "SST",
                        "values": [record["sea_surface_temperature_value"]],
                        "units": record.get("sea_surface_temperature_unit", "°C"),
                        "description": "海表温度"
                    })
                for field, name, units in (("Ba/Ca_value", "Ba/Ca", "μmol/mol"),
                                           ("δ18O_difference", "Δδ18O", "‰ VPDB")):
                    if record.get(field) is not None:
                        paleo_entry["measurementTable"]["columns"].append({
                            "column_name": name, "values": [record[field]], "units": units,
                        })
                for column in paleo_entry["measurementTable"]["columns"]:
                    name = column["column_name"]
                    field = {"SST": "sea_surface_temperature_value", "Δδ18O": "δ18O_difference"}.get(name, f"{name}_value")
                    if (spread := record.get(f"{field}_uncertainty")) is not None:
                        column["reportedUncertainty"] = [spread]
                
                lipd_data["paleoData"].append(paleo_entry)
                
                # geo部分
                geo_entry = {
                    "site": record.get("geo_siteName", ""),
                    "lat": record.get("geo_latitude"),
                    "lon": record.get("geo_longitude")
                    # 已移除非必要字段
                }
                lipd_data["geo"].append(geo_entry)
                
                # chronData部分
                chron_entry = {
                    "age": record.get("year"),
                    "ageUnits": record.get("paleoData_age_units", "yr CE"),
                    "datingMethod": record.get("paleoData_dating_method", "unknown"),
                    "datingError": record.get("paleoData_dating_error", "unknown")
                }
                lipd_data["chronData"].append(chron_entry)
            
            # 保存为LiPD格式（JSON）
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(lipd_data, f, ensure_ascii=False, indent=2)
            logging.info(f"LiPD格式数据已保存到: {file_path}")
        except Exception as e:
            logging.error(f"保存LiPD格式数据失败: {str(e)}")
            raise
    
    def _output_netcdf_format(self, data, file_path):
        """按独立观测保存，并携带 CSV 中的关键年代、单位与质控信息。"""
        try:
            import xarray as xr
        except ImportError:
            logging.warning("xarray 未安装，跳过 NetCDF 导出")
            return "unavailable"
        engines = xr.backends.list_engines()
        engine = next((name for name in ("netcdf4", "h5netcdf") if name in engines), None)
        if engine is None:
            logging.warning("缺少支持 Unicode 的 NetCDF 后端，跳过 NetCDF 导出")
            return "unavailable"

        variables = {}
        numeric = (
            ("year", "time", None), ("geo_latitude", "latitude", "degrees_north"),
            ("geo_longitude", "longitude", "degrees_east"), ("source_row", "source_row", None),
            ("source_line", "source_line", None),
            ("source_header_line", "source_header_line", None), ("source_page", "source_page", None),
            ("source_time_reference_year", "source_time_reference_year", None),
            ("geo_rep_latitude", "geo_rep_latitude", "degrees_north"),
            ("geo_rep_longitude", "geo_rep_longitude", "degrees_east"),
            ("geo_lat_span", "geo_lat_span", "degrees"), ("geo_lon_span", "geo_lon_span", "degrees"),
            ("paleoData_dating_error", "paleoData_dating_error", None),
            ("qc_flag", "qc_flag", None), ("qc_outlier_flag", "qc_outlier_flag", None),
        )
        measures = (
            ("Sr/Ca_value", "Sr_Ca", "mmol/mol"), ("δ18O_value", "δ18O", "‰ VPDB"),
            ("δ13C_value", "δ13C", "‰ VPDB"), ("δ18O_difference", "Δδ18O", "‰ VPDB"),
            ("sea_surface_temperature_value", "SST", "°C"),
            ("Ba/Ca_value", "Ba_Ca", "μmol/mol"),
        )
        for field, name, unit in (*numeric, *measures):
            values = [float("nan") if (value := finite_number(row.get(field))) is None else value for row in data]
            variables[name] = ("observation", values, {"units": unit} if unit else {})

        text_fields = (
            "paper_hash", "source_file", "source_attachment", "sheet_name", "source_sheet_name", "sample_id",
            "source_record_id", "source_core_id", "geo_siteName", "pub1_doi", "dataset_doi",
            "source_time_value", "source_time_column", "source_time_unit", "source_time_date_order", "paleoData_dating_method",
            "hasResolution_nominal", "record_type", "mapping_route", "qc_time_status", "qc_location_status",
            "qc_log", "qc_outlier_reason",
            "geo_rep_status", "geo_location_source", "geo_location_evidence",
        )
        for key in text_fields:
            variables[key] = ("observation", [str(row[key]) if row.get(key) is not None else "" for row in data])
        variables["paleoData_dating_error_raw"] = (
            "observation", [str(row["paleoData_dating_error"]) if row.get("paleoData_dating_error") is not None else "" for row in data]
        )
        for key in ("source_columns", "source_column_positions", "source_table_bbox", "metadata_evidence", "geo_bounds",
                    "composite_evidence"):
            variables[key] = ("observation", [json.dumps(row.get(key), ensure_ascii=False, default=str) for row in data])
        for field, name, unit in measures:
            for suffix in ("original_value", "original_unit", "unit_status", "qc_status", "unit_resolution"):
                key = f"{field}_{suffix}"
                values = [json.dumps(row.get(key), ensure_ascii=False, default=str) if suffix == "unit_resolution" else str(row[key])
                          if row.get(key) is not None else "" for row in data]
                variables[f"{name}_{suffix}"] = ("observation", values)
            for suffix in ("original_uncertainty", "uncertainty"):
                key = f"{field}_{suffix}"
                if any(key in row for row in data):
                    values = [float("nan") if (value := finite_number(row.get(key))) is None else value for row in data]
                    variables[f"{name}_{suffix}"] = ("observation", values, {"units": unit} if suffix == "uncertainty" else {})
        variables["observation_id"] = ("observation", [self._observation_id(row) for row in data])
        dataset = xr.Dataset(variables).set_coords(["time", "latitude", "longitude", "observation_id"])
        dataset["time"].attrs["long_name"] = "decimal calendar year CE"
        dataset.to_netcdf(file_path, engine=engine)
        logging.info("NetCDF 数据已保存到 %s", file_path)
        return "written"

    def _output_metadata_csv(self, metadata_summary: Dict[str, Any], file_path: str):
        """输出元数据汇总CSV"""
        try:
            # 将元数据转换为适合CSV的格式
            csv_data = []
            
            # 添加数据类型统计
            data_types = metadata_summary.get("data_types", [])
            for data_type in data_types:
                csv_data.append({
                    "category": "data_type",
                    "key": data_type,
                    "value": "1"
                })
            
            # 添加时间范围
            time_range = metadata_summary.get("time_range", {})
            if time_range:
                csv_data.append({
                    "category": "time_range",
                    "key": "earliest",
                    "value": str(time_range.get("earliest", ""))
                })
                csv_data.append({
                    "category": "time_range",
                    "key": "latest",
                    "value": str(time_range.get("latest", ""))
                })
            
            # 添加地理覆盖范围
            geo_coverage = metadata_summary.get("geographical_coverage", {})
            if geo_coverage:
                csv_data.append({
                    "category": "geographical_coverage",
                    "key": "lat_min",
                    "value": str(geo_coverage.get("lat_range", ["", ""])[0])
                })
                csv_data.append({
                    "category": "geographical_coverage",
                    "key": "lat_max",
                    "value": str(geo_coverage.get("lat_range", ["", ""])[1])
                })
                csv_data.append({
                    "category": "geographical_coverage",
                    "key": "lon_min",
                    "value": str(geo_coverage.get("lon_range", ["", ""])[0])
                })
                csv_data.append({
                    "category": "geographical_coverage",
                    "key": "lon_max",
                    "value": str(geo_coverage.get("lon_range", ["", ""])[1])
                })
            
            # 添加数据源统计
            sources = metadata_summary.get("sources", {})
            csv_data.append({
                "category": "sources",
                "key": "count",
                "value": str(sources.get("count", 0))
            })
            
            # 【移除】不再统计和输出holocene_stage，判断是数据提取后的事情
            
            # 添加测年方法统计
            dating_methods = metadata_summary.get("dating_methods", {})
            for method, count in dating_methods.items():
                csv_data.append({
                    "category": "dating_method",
                    "key": method,
                    "value": str(count)
                })
            
            # 添加检测方法统计
            testing_methods = metadata_summary.get("testing_methods", {})
            for method, count in testing_methods.items():
                csv_data.append({
                    "category": "testing_method",
                    "key": method,
                    "value": str(count)
                })
            
            # 保存为CSV
            df = pd.DataFrame(csv_data)
            df.to_csv(file_path, index=False, encoding='utf-8-sig')
            logging.info(f"元数据汇总已保存到: {file_path}")
        except Exception as e:
            logging.error(f"保存元数据汇总失败: {str(e)}")
            raise
    
    def generate_metadata_summary(self, data: List[Dict[str, Any]]) -> Dict[str, Any]:
        """生成元数据汇总"""
        summary = {
            "data_types": set(),
            "time_range": {},
            "geographical_coverage": {},
            "sources": set(),
            # 【移除】不再统计holocene_stages，判断是数据提取后的事情
            "dating_methods": {},
            "testing_methods": {}
        }
        
        years = []
        latitudes = []
        longitudes = []
        sources = set()
        # 【移除】不再统计holocene_stages，判断是数据提取后的事情
        dating_methods = {}
        testing_methods = {}
        
        for record in data:
            # 数据类型统计
            if record.get("Sr/Ca_value") is not None:
                summary["data_types"].add("Sr/Ca")
            if record.get("Ba/Ca_value") is not None:
                summary["data_types"].add("Ba/Ca")
            if record.get("δ18O_value") is not None:
                summary["data_types"].add("δ18O")
            if record.get("δ13C_value") is not None:
                summary["data_types"].add("δ13C")
            if record.get("δ18O_difference") is not None:
                summary["data_types"].add("Δδ18O")
            if record.get("sea_surface_temperature_value") is not None and record.get("sea_surface_temperature_value") not in ["-", "待计算"]:
                summary["data_types"].add("SST")
            
            # 时间范围统计
            year = record.get("year")
            if year is not None:
                try:
                    years.append(float(year))
                except:
                    pass
            
            # 地理覆盖范围统计
            lat = record.get("geo_latitude")
            lon = record.get("geo_longitude")
            if lat is not None:
                try:
                    latitudes.append(float(lat))
                except:
                    pass
            if lon is not None:
                try:
                    longitudes.append(float(lon))
                except:
                    pass
            
            # 数据源统计
            source = record.get("paper_hash")
            if source:
                sources.add(source)
            
            # 【移除】不再统计holocene_stage，判断是数据提取后的事情
            
            # 测年方法统计
            dating_method = record.get("paleoData_dating_method")
            if dating_method:
                dating_methods[dating_method] = dating_methods.get(dating_method, 0) + 1
            
            # 检测方法统计
            testing_method = record.get("testing_method")
            if testing_method:
                testing_methods[testing_method] = testing_methods.get(testing_method, 0) + 1
        
        # 构造汇总信息
        summary["data_types"] = list(summary["data_types"])
        
        if years:
            summary["time_range"] = {
                "earliest": min(years),
                "latest": max(years)
            }
        
        if latitudes and longitudes:
            summary["geographical_coverage"] = {
                "lat_range": [min(latitudes), max(latitudes)],
                "lon_range": [min(longitudes), max(longitudes)]
            }
        
        summary["sources"] = {
            "count": len(sources),
            "paper_hashes": list(sources)
        }
        
        # 【移除】不再统计holocene_stages，判断是数据提取后的事情
        summary["dating_methods"] = dating_methods
        summary["testing_methods"] = testing_methods
        
        return summary

    def _convert_numeric_value(self, value) -> str:
        """安全转换数值：处理空格、逗号等异常格式"""
        if pd.isna(value) or value in [None, "-", "待计算"]:
            return "-"
        # 转换为字符串处理
        value_str = str(value).strip().replace(',', '')  # 去除空格、逗号
        try:
            converted_value = str(round(float(value_str), 6))
            # 只有在调试模式下才记录数值转换过程
            if logging.getLogger().isEnabledFor(logging.DEBUG):
                self.tracer.log_numeric_conversion("unknown_field", value, converted_value, True)
            return converted_value
        except ValueError:
            # 只有在调试模式下才记录数值转换失败
            if logging.getLogger().isEnabledFor(logging.DEBUG):
                logging.warning(f"数值转换失败：{value}")
                self.tracer.log_numeric_conversion("unknown_field", value, "-", False)
            return "-"

    def _generate_readme(self, readme_path: str, data_count: int, quality_report: Dict[str, Any], grouped_data: Dict[str, List[Dict[str, Any]]]):
        """生成README文件"""
        try:
            with open(readme_path, 'w', encoding='utf-8') as f:
                f.write("# 古气候数据提取结果\n\n")
                f.write("## 数据统计\n")
                f.write(f"总计数据点数: {data_count}\n\n")
                
                # 分辨率统计
                f.write("## 按时间分辨率分组\n")
                for resolution, data in grouped_data.items():
                    f.write(f"- {self.resolution_labels.get(resolution, resolution)}: {len(data)} 条记录\n")
                f.write("\n")
                
                # 质量报告摘要
                f.write("## 数据质量报告摘要\n")
                qc_distribution = quality_report.get("qc_distribution", {})
                for flag, info in qc_distribution.items():
                    f.write(f"- QC标志 {flag} ({info.get('description', '')}): {info.get('count', 0)} 条记录 ({info.get('percentage', 0)}%)\n")
                f.write("\n")
                
                f.write("## 文件说明\n")
                f.write("- `*_data.csv`: 按时间分辨率分组的数据文件\n")
                f.write("- `data_overview.csv`: 数据总览表\n")
                f.write("- `paleoclimate_data.lpd`: LiPD格式数据文件\n")
                f.write("- `paleoclimate_grid.nc`: NetCDF 格式逐观测数据文件\n")
                f.write("- `qc_log.json`: 数据质量报告\n")
                f.write("- `metadata_summary.csv`: 元数据汇总\n")
                f.write("- `README.md`: 本说明文件\n")
                
            logging.info(f"README文件已生成: {readme_path}")
        except Exception as e:
            logging.error(f"生成README文件失败: {str(e)}")

    def save_final_results(self, all_data: List[Dict[str, Any]]):
        """保存最终汇总结果"""
        from app.utils.toolkit import filter_empty_key_fields
        
        # 最终过滤
        all_data = filter_empty_key_fields(all_data)
        
        # 输出为表格（如CSV/Excel）
        import pandas as pd
        df = pd.DataFrame(all_data)
        final_csv_path = os.path.join(self.output_dir, "final_aggregated_data.csv")
        df.to_csv(final_csv_path, index=False, encoding="utf-8-sig")
        logging.info(f"最终汇总结果已保存，共 {len(df)} 行有效数据")

    def _indicator_group(self, record):
        fields = ("Sr/Ca_value", "δ18O_value", "δ13C_value")
        pattern = tuple(finite_number(record.get(field)) is not None for field in fields)
        if any(pattern):
            return INDICATOR_GROUPS[pattern]
        for field, group in (("sea_surface_temperature_value", "G8_SST"),
                             ("δ18O_difference", "G9_O18Difference"), ("Ba/Ca_value", "G10_BaCa")):
            if finite_number(record.get(field)) is not None:
                return group
        return None

    def _group_data_by_indicators(self, data):
        groups = {}
        for index, record in enumerate(data):
            group = self._indicator_group(record)
            if group:
                groups.setdefault(group, []).append(index)
        return groups

    def _validate_indicator_group(self, data, group_name):
        return [record for record in data if self._indicator_group(record) == group_name]

    def _output_indicator_group(self, data, file_path, resolution):
        self._output_csv_by_resolution(data, file_path, resolution)

    def _output_combined_data(self, data, file_path, resolutions=None):
        resolutions = resolutions or {}
        rows = [
            self._csv_record(
                record, f"combined_{index:05d}",
                resolutions.get(id(record), record.get("hasResolution_nominal", "unknown")),
            )
            for index, record in enumerate(filter_empty_key_fields(data), 1)
        ]
        self._output_csv(rows, file_path)

    # 汇总表显示列名 → 内部字段名（_csv_record 的逆映射，用于增量批次同步导出 LiPD/NetCDF）。
    _COMBINED_MEASURES = (
        ("Sr/Ca (mmol/mol)", "Sr/Ca_value"),
        ("Ba/Ca (μmol/mol)", "Ba/Ca_value"),
        ("δ18O (‰ VPDB)", "δ18O_value"),
        ("δ13C (‰)", "δ13C_value"),
        ("Δδ18O (‰ VPDB)", "δ18O_difference"),
        ("SST (°C)", "sea_surface_temperature_value"),
    )
    _COMBINED_JSON = ("source_columns", "source_column_positions", "source_table_bbox",
                      "source_header_rows", "composite_evidence", "metadata_evidence", "geo_bounds")
    _COMBINED_INT = ("source_row", "source_line", "source_header_line", "source_page")

    @classmethod
    def _records_from_combined(cls, rows):
        """把汇总表 CSV 行还原为导出器所需的内部记录（增量批次无原始 processed_data）。"""
        records = []
        for row in rows:
            record = dict(row)
            record["year"] = finite_number(row.get("year_CE"))
            record["hasResolution_nominal"] = row.get("time_resolution")
            for header, field in cls._COMBINED_MEASURES:
                if header in row:
                    record[field] = finite_number(row.get(header))
            for key in cls._COMBINED_INT:
                value = finite_number(row.get(key))
                record[key] = int(value) if value is not None else None
            for key in cls._COMBINED_JSON:
                if row.get(key):
                    try:
                        record[key] = json.loads(row[key])
                    except (TypeError, ValueError):
                        record[key] = row[key]
            records.append(record)
        return records

    def export_archive_formats(self, output_path, combined_csv_path):
        """从更新后的汇总表同步导出 LiPD 与 NetCDF，使增量批次与正式批次格式一致。"""
        rows = list(_read_combined(combined_csv_path))
        if not rows:
            return {"netcdf": "empty", "lipd": "empty"}
        records = self._records_from_combined(rows)
        self._output_lipd_format(records, os.path.join(output_path, "paleoclimate_data.lpd"))
        netcdf_status = self._output_netcdf_format(records, os.path.join(output_path, "paleoclimate_grid.nc"))
        return {"netcdf": netcdf_status, "lipd": "written"}

    def _output_group_summary(self, indicator_groups: Dict[str, List[int]], file_path: str):
        """
        输出指标分组统计
        """
        try:
            summary = {}
            for group_name, indices in indicator_groups.items():
                summary[group_name] = {
                    "description": self._get_group_description(group_name),
                    "record_count": len(indices),
                    "indices": indices[:100]  # 仅保存前100个索引示例
                }
            
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(summary, f, ensure_ascii=False, indent=2)
            logging.info(f"指标分组统计已保存到: {file_path}")
        except Exception as e:
            logging.error(f"保存指标分组统计失败: {str(e)}")
            raise
    
    def _generate_index_table(self, output_path: str, index_path: str, 
                             resolution_index: List[Dict[str, Any]], 
                             grouped_data: Dict[str, List[Dict[str, Any]]]):
        """
        生成总索引表，便于快速定位各个数据文件
        """
        try:
            index_data = []
            
            # 添加分辨率级数据统计
            for resolution, data in grouped_data.items():
                if data and resolution != "hydrological":
                    index_data.append({
                        "type": "resolution_data",
                        "name": f"{resolution}_data.csv",
                        "path": f"{resolution}/{resolution}_data.csv",
                        "description": f"{self.resolution_labels.get(resolution, resolution)}数据汇总",
                        "record_count": len(data),
                        "content_type": "全字段数据"
                    })
            
            # 添加指标分级数据
            for item in resolution_index:
                index_data.append({
                    "type": "indicator_group",
                    "name": item["indicator_group"],
                    "path": item["file_path"],
                    "description": self._get_group_description(item["indicator_group"]),
                    "record_count": item["record_count"],
                    "resolution": item["resolution"],
                    "content_type": "指标分级数据"
                })
            
            # 添加汇总表
            index_data.append({
                "type": "consolidated",
                "name": "all_data_combined.csv",
                "path": "consolidated/all_data_combined.csv",
                "description": "所有分辨率数据的汇总表",
                "record_count": sum(len(d) for d in grouped_data.values() if d),
                "content_type": "全字段汇总数据"
            })
            
            # 创建DataFrame并保存
            df = pd.DataFrame(index_data)
            df.to_csv(index_path, index=False, encoding='utf-8-sig')
            logging.info(f"总索引表已生成: {index_path}")
        except Exception as e:
            logging.error(f"生成总索引表失败: {str(e)}")
            raise
    
    def _get_group_description(self, group_name: str) -> str:
        """
        获取指标分组的描述
        """
        descriptions = {
            "G1_SrCa_O18_C13": "三指标完整组（Sr/Ca + δ18O + δ13C）",
            "G2_SrCa_O18": "双指标组（Sr/Ca + δ18O）",
            "G3_O18_C13": "双指标组（δ18O + δ13C）",
            "G4_SrCa": "单指标组（仅Sr/Ca）",
            "G5_O18": "单指标组（仅δ18O）",
            "G6_C13": "单指标组（仅δ13C）",
            "G7_SrCa_C13": "双指标组（Sr/Ca + δ13C）",
            "G8_SST": "含海表温度的其他观测",
            "G9_O18Difference": "含氧同位素差值的其他观测",
            "G10_BaCa": "钡钙比观测",
        }
        return descriptions.get(group_name, group_name)
    
# 示例使用
if __name__ == "__main__":
    # 创建数据输出模块实例
    output_module = DataOutputModule()
    
    # 示例：生成元数据汇总
    # sample_data = [
    #     {
    #         "paper_hash": "sample001",
    #         "year": 1900,
    #         "geo_latitude": 18.5,
    #         "geo_longitude": 109.2,
    #         "geo_siteName": "南海-中国",
    #         "Sr/Ca_value": 8.5,
    #         "δ18O_value": -5.2,
    #         "paleoData_holocene_stage": "CE",
    #         "paleoData_dating_method": "U-Th dating",
    #         "testing_method": "ICP-MS"
    #     }
    # ]
    # metadata_summary = output_module.generate_metadata_summary(sample_data)
    # print(metadata_summary)
    pass
