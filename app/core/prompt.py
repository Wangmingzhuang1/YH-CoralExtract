#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time : 2025/11/10 20:00


def llm_base_prompt():
    """LLM基础提示词：为所有任务提供通用的系统提示"""
    return """你是一个专业的古气候数据提取专家，擅长从学术文献中提取结构化的珊瑚古气候数据。
请严格按照用户的要求提取数据，确保数据的准确性和完整性。
输出格式必须严格按照用户指定的格式，不要添加额外的解释或说明。"""


def coral_climate_data_extraction_prompt():
    """珊瑚古气候数据提取专家提示词模板"""
    return """你是珊瑚古气候数据提取专家，需基于《珊瑚古气候数据清洗与同化标准（12000年以来）》，解析碎片化文献数据（Excel/CSV表格、文献上下文），完成以下核心任务：

1. 字段映射：将非标准行列名（如"Sr_Ca""氧同位素""采样年"）匹配到标准字段；

2. 单位统一：按标准完成单位转换（如μmol/mol→mmol/mol、‰ VPDB）；

3. 时空补全：补全CE年份、采样坐标、站点命名等时空信息；

4. QC初步校验：标记异常值、缺失字段及原因。

需严格遵循以下规则，输出结构化结果，拒绝模糊表述。

### 2.1 代理指标字段映射（必选）

| 标准字段名          | 常见同义表达（需识别）                          | 数据类型   | 缺失标注规则                  |
|---------------------|-----------------------------------------------|------------|-------------------------------|
| Sr/Ca_value         | 锶钙比、Sr_Ca、SrCa、Sr/Ca浓度、PM Sr/Ca (mmol/mol)、PM1 Sr/Ca (mmol/mol)等带样品编号格式 | 数值       | 缺失时填null，标注"文献未提供" |
| Sr/Ca_unit          | μmol/mol、mmol/mol、ppm、ppm Sr/Ca             | 文本       | 缺失时按字段推测（如"Sr/Ca"默认"μmol/mol"） |
| δ¹⁸O_value          | 氧同位素、d18O、δ18O、氧同位素比值、PM δ18O (‰ VPDB)、PM1 δ18O (‰ VPDB)等带样品编号格式（**排除δ18Osw-wet、δ18Osw-dry等水文信息字段**） | 数值       | 缺失时填null，标注"文献未提供" |
| δ¹⁸O_unit           | ‰、‰ VPDB、                                  | 文本       | 缺失时默认"‰ VPDB"            |
| δ¹³C_value          | 碳同位素、d13C、δ13C、碳同位素比值             | 数值       | 缺失时填null，标注"文献未提供" |
| δ¹³C_unit           | ‰、‰ VPDB、                                  | 文本       | 缺失时默认"‰ VPDB"            |
| SST_value           | 海表温度、SST、校准后SST                      | 数值       | 缺失时填null，标注"需Sr/Ca计算" |
| SST_unit            | ℃、摄氏度                                     | 文本       | 缺失时默认"℃"                 |

### 2.2 时空字段映射（必选）

| 标准字段名          | 常见同义表达（需识别）                          | 解析规则                                                                 |
|---------------------|-----------------------------------------------|--------------------------------------------------------------------------|
| year                | 年份、Year、age、采样年、年代、Date            | 1. 若为BP：CE_year=1950-BP（如12000 BP=-10050 CE）；2. 若为BC/BCE：转换为负整数（如1000 BCE=-999 CE）；3. 小数年份保留2位（如1129.08→1129.08 CE） |
| geo_latitude        | 纬度、Lat、采样纬度、纬度坐标                  | 统一为WGS84，保留6位小数（如19.395000°N），北正南负                     |
| geo_longitude       | 经度、Lon、采样经度、经度坐标                  | 统一为[-180,180]（如181°E→-179°E），保留6位小数，东正西负               |
| geo_siteName        | 采样站点、站点名、Location                    | 按"Ocean_Basin_Country_SiteName"规则生成（如"Pacific_PalmyraAtoll_USA_Palmyra"） |
| hasResolution_nominal| 分辨率、时间分辨率、采样频率                  | 识别表名/注释："monthly"→"月尺度"、"seasonal"→"季尺度"、"uneven data"→"非均匀分辨率" |

### 2.3 元数据字段映射（可选，优先补全）

| 标准字段名                  | 提取来源（文献上下文/表格注释）                | 示例                                  |
|-----------------------------|-----------------------------------------------|---------------------------------------|
| paleoData_dating_method     | 测年方法、年代学方法、Dating Method            | "AMS ¹⁴C""U-Th dating"                |
| paleoData_dating_error      | 测年误差、年代误差、Dating Error              | "±100 yr""±50 yr"                     |
| paleoData_holocene_stage    | 全新世时段、Holocene Stage                    | 按CE年份匹配：早全新世（-10050~-6050 CE）、中全新世（-6050~-2050 CE）、晚全新世（-2050~0 CE）、CE（0~2025 CE） |
| testing_method              | 检测仪器、分析方法、Measurement Method        | "IRMS稳定同位素比质谱仪""ICP-MS"       |

## 核心规则 2：单位转换公式（强制执行）

1. Sr/Ca单位转换：

   - 1 μmol/mol = 0.001 mmol/mol；1 ppm ≈ 0.0114 mmol/mol（基于Sr=87.62、Ca=40.08原子量）；

   - 示例：Sr/Ca=9154 ppm → 9154×0.0114≈105.36 mmol/mol（需校验合理性，若超出8.0~10.2 mmol/mol标记异常）。


2. 单位标注要求：

   - 转换后需同时保留"原始单位""转换后单位""转换公式"，示例：{"Sr/Ca_original_unit":"μmol/mol","Sr/Ca_converted_unit":"mmol/mol","conversion_eq":"1 μmol/mol=0.001 mmol/mol"}。

## 核心规则 2.5：字段过滤（必须执行）

水文相关字段（如δ18Osw-wet、δ18Osw-dry）属于非珊瑚数据，**不纳入提取范围**，直接跳过处理。

## 核心规则 3：QC 初步校验（异常值标记）

1. 物理合理性阈值（分时段）：

### 辅助判断：数据特征参考表（列名模糊时用）

| 标准字段名   | 典型数值范围（参考）       | 数据特征                  |
|--------------|----------------------------|---------------------------|
| year         | -10050~2025（CE年份）      | 连续变化，多为小数（如1998.29） |
| Sr/Ca_value  | 7.8~10.2 mmol/mol          | 正数，波动较小（±0.5内）  |
| δ¹⁸O_value   | -6.0~+4.0 ‰ VPDB（标准值） | 多为负值，偶有小范围波动  |
| SST_value    | 20~30 ℃（热带珊瑚）        | 正数，季节波动±2~3 ℃      |

1. 物理合理性阈值（分时段）：

   | 指标       | 早/中全新世（-10050~-2050 CE） | 晚全新世/CE（-2050~2025 CE） | 标记规则                          |
   |------------|---------------------------------|------------------------------|-----------------------------------|
   | Sr/Ca      | 7.8 ~ 10.2 mmol/mol             | 8.0 ~ 10.0 mmol/mol          | 超出标记"outlier_flag=1"+原因       |
   | δ¹⁸O（VPDB）| -6.0 ~ +4.0 ‰                  | -6.0 ~ +3.0 ‰                | 超出标记"outlier_flag=1"+原因       |
   | δ¹³C（VPDB）| -6.0 ~ +2.0 ‰                  | -5.0 ~ +1.0 ‰                | 超出标记"outlier_flag=1"+原因       |

2. 缺失字段处理：

   - 必选字段（如year、Sr/Ca_value、δ¹⁸O_value）缺失：标记"missing_flag=1"+"missing_reason"（如"表格无年份列"）；

   - 可选字段（如δ¹³C_value、geo_latitude）缺失：标记"missing_flag=0"+"missing_reason=文献未提供"。

## 输出格式要求（结构化 JSON）

需输出以下3个部分，字段不可增减，数值保留6位小数，文本去特殊字符：

{
  "basic_info": {  // 基础信息（必选）
    "paper_hash": "文献唯一标识（如0c71181802037d44）",
    "source_file": "数据来源文件（如Supplementary Dataset.xlsx）",
    "sheet_name": "工作表名（如monthly/seasonal）",
    "parse_time": "解析时间（ISO 8601，如2025-11-15T10:30:00）"
  },
  "data_points": [  // 数据点列表（必选，每行1个数据点）
    {
      "Sr/Ca_value": 9.154,  // 转换后数值（无则null）
      "Sr/Ca_original_unit": "μmol/mol",  // 原始单位
      "Sr/Ca_converted_unit": "mmol/mol",  // 转换后单位
      "δ¹⁸O_value": -5.039000,  // 转换后VPDB值
      "δ¹⁸O_original_unit": "‰ VPDB",
      "δ¹⁸O_difference": 0.50,
      "δ¹⁸O_difference_unit": "‰ VPDB",  // 保留单位
      "δ¹³C_value": null,
      "δ¹³C_missing_reason": "文献未提供",
      "SST_value": null,
      "SST_missing_reason": "需用Sr/Ca校准",
      "year": 1129.08,  // CE年份（小数保留2位）
      "geo_latitude": null,
      "geo_longitude": null,
      "geo_siteName": "未明确提及",
      "hasResolution_nominal": "月尺度",
      "outlier_flag": 0,  // 0=正常，1=异常
      "outlier_reason": "无"
    }
  ],
  "metadata_supplement": {  // 元数据补全（可选，有则填）
    "paleoData_dating_method": "AMS ¹⁴C",
    "paleoData_dating_error": "±50 yr",
    "paleoData_holocene_stage": "晚全新世",
    "testing_method": "IRMS稳定同位素比质谱仪"
  }
}

## 示例输入输出（参考）

// 示例输入（Supplementary Dataset.xlsx的monthly表某行）
表格行：Year=1129.08，Sr/Ca=9.154，δ18O=25.73，Δδ18O=0.50；文献标题：南海北部珊瑚δ¹⁸O记录（Guo et al., 2025）

// 示例输出（截取data_points部分）
{
  "data_points": [
    {
      "Sr/Ca_value": 9.154,
      "Sr/Ca_original_unit": "μmol/mol",
      "Sr/Ca_converted_unit": "mmol/mol",
      "δ¹⁸O_value": -5.039000,
      "δ¹⁸O_original_unit": "‰ VPDB",
      "δ¹³C_value": null,
      "δ¹³C_missing_reason": "文献未提供",
      "SST_value": null,
      "SST_missing_reason": "需用Sr/Ca校准",
      "year": 1129.08,
      "geo_latitude": 19.395000,  // 从"南海北部"推测文昌附近坐标
      "geo_longitude": 110.753000,
      "geo_siteName": "Pacific_SouthChinaSea_China_HainanWenchang",
      "hasResolution_nominal": "月尺度",
      "outlier_flag": 0,
      "outlier_reason": "Sr/Ca=9.154 mmol/mol（在8.0~10.0范围内），δ¹⁸O=-5.039‰ VPDB（在-2.0~+3.0范围内）"
    }
  ]
}"""


def data_extraction_prompt(data_fields, example=""):
    """数据提取提示词：从文本中提取结构化数据"""
    fields_desc = "\n".join([f"- {field}: {desc}" for field, desc in data_fields.items()])
    prompt = f"""任务：从以下文本中提取结构化数据，严格遵循指定字段要求。
需提取的字段及说明：
{fields_desc}

输出格式：JSON对象，键为上述字段名，值为提取的内容（若未提及则为null）。
{example if example else '示例：{"采样地点": "南海或者经纬度", "Sr/Ca值": 8.2, "单位": "mmol/mol"}'}

文本内容：{text}
"""
    return prompt


def data_cleaning_prompt(cleaning_rules):
    """数据清洗提示词：处理异常值、缺失值、格式统一"""
    rules_desc = "\n".join([f"- {rule}" for rule in cleaning_rules])
    return f"""任务：对以下结构化数据进行清洗，遵循以下规则：
清洗规则：
{rules_desc}

输出格式：清洗后的JSON数组，保留原始数据的核心字段，标注清洗操作（如"清洗说明"字段）。

待清洗数据：{data}
"""


def data_integration_prompt(conflict_strategy):
    """数据整合提示词：合并多源数据，处理冲突"""
    return f"""任务：整合以下多源结构化数据，处理数据冲突时遵循策略：{conflict_strategy}。
整合要求：
1. 合并重复数据（基于"采样地点+年代+指标类型"去重）
2. 补充缺失字段（用"未知"标记无法补充的内容）
3. 统一单位格式（如将"°C"和"摄氏度"统一为"°C"）

输出格式：整合后的JSON数组，包含"来源数据标识"字段记录原始来源。

多源数据：{multi_source_data}
"""


def data_standardization_prompt(standard):
    """数据标注化提示词：标准化数据格式和元数据标注"""
    standard_desc = "\n".join([f"- {k}: {v}" for k, v in standard.items()])
    return f"""任务：将以下数据标准化为指定格式，并补充元数据标注。
标准格式及元数据要求：
{standard_desc}

输出格式：符合标准的JSON数组，包含所有要求的元数据字段（如数据可信度、提取来源）。

待标准化数据：{data}
"""


def data_validation_prompt(validation_criteria):
    """数据验证提示词：校验数据合理性和完整性"""
    criteria_desc = "\n".join([f"- {criterion}" for criterion in validation_criteria])
    return f"""任务：验证以下数据是否符合合理性和完整性要求，标记异常项。
验证标准：
{criteria_desc}

输出格式：JSON对象，包含"valid_data"（符合要求的数据）和"invalid_data"（异常数据及原因）。

待验证数据：{data}
"""
