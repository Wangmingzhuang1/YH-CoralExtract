"""发布包 schema：源批次列 -> 精简 ASCII 列的映射与代理定义。

主表 observations.csv 只保留分析必需列；逐值溯源（原值/原单位/证据）
拆到 04_provenance_qc/value_provenance.csv 长表，按 observation_id 关联。
"""
import re

# 代理定义：规范名 -> (源值列, 源QC状态列, 单位, 原值列, 原单位列, 单位状态列, 证据列)
PROXIES = {
    "d18O": ("δ18O (‰ VPDB)", "δ18O_value_qc_status", "permil VPDB",
             "δ18O_value_original_value", "δ18O_value_original_unit",
             "δ18O_value_unit_status", "δ18O_value_unit_evidence"),
    "d13C": ("δ13C (‰)", "δ13C_value_qc_status", "permil VPDB",
             "δ13C_value_original_value", "δ13C_value_original_unit",
             "δ13C_value_unit_status", "δ13C_value_unit_evidence"),
    "sr_ca": ("Sr/Ca (mmol/mol)", "Sr/Ca_value_qc_status", "mmol/mol",
              "Sr/Ca_value_original_value", "Sr/Ca_value_original_unit",
              "Sr/Ca_value_unit_status", "Sr/Ca_value_unit_evidence"),
    "ba_ca": ("Ba/Ca (μmol/mol)", "Ba/Ca_value_qc_status", "umol/mol",
              "Ba/Ca_value_original_value", "Ba/Ca_value_original_unit",
              "Ba/Ca_value_unit_status", "Ba/Ca_value_unit_evidence"),
    "sst": ("SST (°C)", "sea_surface_temperature_value_qc_status", "degree Celsius",
            "sea_surface_temperature_value_original_value",
            "sea_surface_temperature_value_original_unit",
            "sea_surface_temperature_value_unit_status",
            "sea_surface_temperature_value_unit_evidence"),
}

# LiPD/CSV 列名 -> 展示名（数据字典用）
PROXY_LABELS = {
    "d18O": "Coral skeletal oxygen isotope ratio (delta 18O)",
    "d13C": "Coral skeletal carbon isotope ratio (delta 13C)",
    "sr_ca": "Coral skeletal strontium-to-calcium ratio (Sr/Ca)",
    "ba_ca": "Coral skeletal barium-to-calcium ratio (Ba/Ca)",
    "sst": "Sea surface temperature (SST)",
}

# 代理组合分组（与提取批次 G 编号一致）
PROXY_GROUPS = {
    "G1_SrCa_O18_C13": ("sr_ca", "d18O", "d13C"),
    "G2_SrCa_O18": ("sr_ca", "d18O"),
    "G3_O18_C13": ("d18O", "d13C"),
    "G4_SrCa": ("sr_ca",),
    "G5_O18": ("d18O",),
    "G6_C13": ("d13C",),
    "G7_SrCa_C13": ("sr_ca", "d13C"),
    "G8_SST": ("sst",),
    "G10_BaCa": ("ba_ca",),
}

# observations.csv 精简 schema：目标列 -> 源列
OBSERVATION_COLUMNS = {
    "observation_id": "observation_id",
    "record_id": "source_record_id",
    "site_name": "geo_siteName",
    "latitude": "geo_latitude",
    "longitude": "geo_longitude",
    "year_ce": "year_CE",
    "time_resolution": "time_resolution",
    "native_interval_years": "native_interval_years",
    "source_time_value": "source_time_value",
    "source_time_unit": "source_time_unit",
    "source_time_reference_year": "source_time_reference_year",
    "source_time_column": "source_time_column",
    "qc_flag": "qc_flag",
}

# 记录级元数据：目标列 -> 源列（每条记录取首条非空）
RECORD_METADATA_COLUMNS = {
    "pub_doi": "pub1_doi",
    "pub_authors": "pub1_authors",
    "dataset_doi": "dataset_doi",
    "dataset_investigators": "dataset_investigators",
    "dating_method": "paleoData_dating_method",
    "dating_error": "paleoData_dating_error",
    "testing_method": "testing_method",
}

QC_STATUS_VOCAB = {
    "pass": "Value passed range, unit and outlier checks.",
    "missing_or_invalid_value": "No valid value reported for this proxy at this observation.",
    "outside_range": "Value outside the proxy-specific physical range; retained but flagged.",
    "outlier_flagged": "Value flagged as an IQR outlier within its record; retained but flagged.",
    "suspect_centered_series": "Series appears centered/transformed; calibration to absolute scale unverified.",
    "unknown_unit": "Original unit could not be confirmed; value withheld from the released value column.",
}


def slugify_site(name: str) -> str:
    """站点名 -> ASCII 短码（记录命名用）。"""
    text = re.sub(r"\(.*?\)", "", str(name or ""))
    text = re.sub(r"[^A-Za-z0-9]+", "", text.title())
    return (text[:12] or "Site").upper()


def proxy_group_of(present) -> str:
    """按行内非空代理集合返回 G 组名；未收录组合返回 OTHER。"""
    present = frozenset(present)
    for group, members in PROXY_GROUPS.items():
        if present == frozenset(members):
            return group
    return "OTHER"