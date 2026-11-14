#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time : 2025/11/12 10:00
# @Author : 桐
# @QQ:1041264242
# 注意事项：

# 文献标准元数据字段字典
LITERATURE_METADATA_STANDARD = {
    # 实体元数据（文献表2）
    "paleoData_coralHydro2kGroup": {
        "type": "integer",
        "range": [1, 7],
        "description": "珊瑚Hydro2k分组（1-7组）",
        "required": True
    },
    "geo_latitude": {
        "type": "float",
        "range": [-90.0, 90.0],
        "description": "纬度（N正/S负）",
        "required": True
    },
    "geo_longitude": {
        "type": "float",
        "range": [-180.0, 180.0],
        "description": "经度（E正/W负）",
        "required": True
    },
    "geo_siteName": {
        "type": "string",
        "format": "[岛屿/城市]-[国家]",
        "description": "站点名（如“Mentawai Islands, Indonesia”）",
        "required": True
    },

    "hasResolution_nominal": {
        "type": "string",
        "values": ["monthly", "bimonthly", "quarterly", "biannual", "annual", ">annual"],
        "description": "名义分辨率（可加_uneven标记变量分辨率）",
        "required": True
    },
    "time_oldestYear": {
        "type": "integer",
        "description": "最早年份（CE年份）",
        "required": True
    },
    "time_youngestYear": {
        "type": "integer",
        "description": "最晚年份（CE年份）",
        "required": True
    },
    
    # 出版元数据（文献表4）
    "pub1_doi": {
        "type": "string",
        "format": "DOI格式",
        "description": "第一作者DOI",
        "required": True
    },
    "pub1_authors": {
        "type": "string",
        "description": "作者标准化引用",
        "required": False
    },
    
    # 全新世专属字段
    "paleoData_holocene_stage": {
        "type": "string",
        "values": ["early", "middle", "late", "ce"],
        "description": "全新世时段（early: 12000-8000 BP, middle: 8000-4000 BP, late: 4000 BP-0 CE, ce: 0 CE-2025 CE）",
        "required": False
    },
    "paleoData_dating_method": {
        "type": "string",
        "description": "测年方法（如AMS ¹⁴C dating, U-Th dating）",
        "required": False
    },
    "paleoData_dating_error": {
        "type": "string",
        "description": "年代误差（如±100 yr）",
        "required": False
    }
}

# 分组规则映射（文献2.3节，表1）
GROUP_CLASSIFICATION_RULES = {
    1: {
        "name": "配对高分辨率20世纪高覆盖组",
        "proxy_pairing": "δ¹⁸O+Sr/Ca",
        "resolution": ["monthly", "bimonthly"],
        "twentieth_century_coverage": "≥80%"
    },
    2: {
        "name": "配对高分辨率20世纪中覆盖组",
        "proxy_pairing": "δ¹⁸O+Sr/Ca",
        "resolution": ["monthly", "bimonthly"],
        "twentieth_century_coverage": "50-80%"
    },
    3: {
        "name": "配对高分辨率20世纪低覆盖组",
        "proxy_pairing": "δ¹⁸O+Sr/Ca",
        "resolution": ["monthly", "bimonthly"],
        "twentieth_century_coverage": "<50%"
    },
    4: {
        "name": "配对低分辨率组",
        "proxy_pairing": "δ¹⁸O+Sr/Ca",
        "resolution": ["quarterly", "biannual", "annual"],
        "twentieth_century_coverage": "any"
    },
    5: {
        "name": "δ¹⁸O单指标高分辨率组",
        "proxy_pairing": "δ¹⁸O-only",
        "resolution": ["monthly", "bimonthly"],
        "twentieth_century_coverage": "any"
    },
    6: {
        "name": "δ¹⁸O单指标低分辨率组",
        "proxy_pairing": "δ¹⁸O-only",
        "resolution": ["quarterly", "biannual", "annual"],
        "twentieth_century_coverage": "any"
    },
    7: {
        "name": "Sr/Ca单指标组",
        "proxy_pairing": "Sr/Ca-only",
        "resolution": "any",
        "twentieth_century_coverage": "any"
    }
}

# 相关性阈值标准（文献3.2节）
CORRELATION_THRESHOLDS = {
    "bimonthly": {
        "Sr/Ca-SST": 0.23,
        "δ¹⁸O-SST": 0.13
    },
    "annual": {
        "Sr/Ca-SST": 0.26,
        "δ¹⁸O-SST": 0.26
    },
    "reproducibility": {
        "Sr/Ca": 0.11,
        "δ¹⁸O": 0.24
    }
}

# 偏差标记规则
BIAS_FLAG_RULES = {
    "low_extension_rate": {
        "field": "paleoData_coralExtensionRate",
        "threshold": 5.0,
        "unit": "mm/year",
        "description": "低生长率可能影响Sr/Ca"
    },
    "no_jcp_standard": {
        "field": "paleoData_jcpUsed",
        "value": "No",
        "description": "未用JCp-1标准，Sr/Ca数据需谨慎使用"
    }
}