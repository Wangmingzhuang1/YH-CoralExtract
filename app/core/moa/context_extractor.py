#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
上下文提取模块 (Context Extractor)

从正文MD文件中提取辅助解析的元数据，弥补附件中元数据缺失的问题。
使用 Flash 模型提取正文元数据，并为表格映射整理源证据。

功能：
1. 从MD文件提取地理位置信息（站点名、经纬度）
2. 提取年代学信息（测年方法、基准年份、测量年份）
3. 提取缺省单位信息（age、Sr/Ca、SST等指标的默认单位）
"""

import logging
import os
import re
import json
from hashlib import sha256
from typing import Dict, Any, Optional, List
from pandas import isna
from app.core.config import settings
from app.core.prompt import llm_base_prompt
from app.core.moa.unit_converter import source_unit
from app.utils.llm_api import MODEL_ROLES, call_llm
from app.utils.pdf_direct_processor import find_skeletal_isotope_scale

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def publication_doi(value):
    """规范化文献 DOI，供比较附件与正文的出版物标识。"""
    match = re.search(r"\b10\.\d{4,9}/[^\s<>\"']+", str(value or ""), re.I)
    if not match:
        return None
    doi = match.group().rstrip(".,;]").casefold()
    while doi.endswith(")") and doi.count(")") > doi.count("("):
        doi = doi[:-1]
    return doi


# 档案 DOI 勘误表：键为来源原文笔误（小写），值为经 Crossref/DataCite 解析确认的注册 DOI。
# 新增条目须附解析证据，随代码版本控制；原始档案文件不改写，仅在读取时规范化。
DOI_ERRATA = {
    # NOAA Pfeiffer2019 档案前缀笔误：10.1002 应为 10.1029（AGU），Crossref 2026-11-16 解析确认
    "10.1002/2019pa003770": "10.1029/2019PA003770",
}


def correct_archive_doi(raw_doi):
    """应用已验证的档案 DOI 勘误；未命中勘误表时原样返回。"""
    corrected = DOI_ERRATA.get(str(raw_doi).casefold())
    if corrected:
        logging.warning("应用档案DOI勘误: %s -> %s", raw_doi, corrected)
        return corrected
    return raw_doi


def quote_names_column(quote, column):
    """检查正文原句是否直接点名该列，避免套用到同指标的其他样品列。"""
    if not isinstance(quote, str) or not quote:
        return False
    name = str(column).strip()
    suffix = re.search(r"\s*[\[(]([^\[\]()]+)[\])]$", name)
    if suffix and source_unit(suffix.group(1)):
        name = name[:suffix.start()].rstrip()
    return bool(name and re.search(rf"(?<!\w){re.escape(name)}(?!\w)", quote, re.I))


def sample_identity_header(column):
    return bool(re.fullmatch(r"(?:(?:coral|core|sample)[ _]+)?(?:id|name)|样品(?:编号|名称)", column.strip(), re.I))


_COMPOSITE = re.compile(r"\b(?:composite|stacked|spliced)\b|(?:多岩芯|多样品)(?:复合|拼接)|复合序列", re.I)
_MULTIPLE_CORES = re.compile(r"\b(?:cores|colonies|corals|coral core records)\b|多(?:根|个)?(?:岩芯|珊瑚)", re.I)
_MULTICORE_MEAN = re.compile(r"\barithmetic mean of (?:all )?(?:\d+|two|three|four|five|six|seven|eight|nine|ten) coral\b", re.I)


def composite_evidence(column, declaration="", paper="", caption=""):
    """保留作者对复合序列的明确标记，区分同一岩芯的时段均值。"""
    for text in (declaration, column):
        if _COMPOSITE.search(text or ""):
            return str(text)
    if caption and _COMPOSITE.search(caption) and quote_names_column(caption, column):
        return caption
    if re.search(r"\b(?:mean|average)\b|均值|平均", column, re.I):
        for pattern in (_MULTICORE_MEAN, _COMPOSITE):
            for match in pattern.finditer(paper):
                excerpt = " ".join(paper[max(0, match.start() - 120):match.end() + 310].split())
                if (pattern is _MULTICORE_MEAN or _MULTIPLE_CORES.search(excerpt)) and quote_names_column(excerpt, column):
                    return excerpt
    return ""


def named_composite_cores(evidence):
    """读取原件明确列出的组成岩芯，接受源中的编号和短名称。"""
    if not isinstance(evidence, str):
        return ()
    match = re.search(r"\b(?:composite|stack|splice)\s+(?:of|from)\s+(?:cores|colonies|corals)?\s*([^;\n]+)",
                      evidence, re.I)
    if not match:
        return ()
    labels = re.sub(r"\([^()]*\)", "", match[1]).strip().rstrip(".")
    parts = [part.strip() for part in re.split(r"[,、]|\band\b|&", labels, flags=re.I) if part.strip()]
    cores = tuple(part.casefold() for part in parts)
    # 岩芯编号须含数字或大写字母，避免 "composite of the records" 类散文误匹配
    identifier_like = all(re.search(r"\d|[A-Z]", part) for part in parts)
    return cores if len(set(cores)) >= 2 and identifier_like and all(
        re.fullmatch(r"[\w-]+(?:\.[\w-]+)*", core) for core in cores) else ()


def build_table_evidence(table, pdf_context=None, known_variables=None):
    """按原表列收集独立证据，保留原表单位与档案声明各自的来源。"""
    headers = table.get("headers", [])
    units = table.get("units") or {}
    defined_units = table.get("defined_units") or {}
    definitions = table.get("variable_definitions") or {}
    variables = {str(item["name"]).strip().casefold(): item
                 for item in known_variables or table.get("variables", [])
                 if isinstance(item, dict) and item.get("name")}
    archive = table.get("archive_metadata") or {}
    missing = {str(value).casefold() for value in archive.get("source_missing_values", [])} | {""}
    samples = {col: [] for col in headers}
    pending = set(headers)
    for row in table.get("data") or []:
        if not pending:
            break
        if not isinstance(row, dict):
            continue
        for col in tuple(pending):
            value = row.get(col)
            if not isna(value) and str(value).strip().casefold() not in missing:
                samples[col].append(str(value)[:100])
                if len(samples[col]) == 3:
                    pending.remove(col)

    context = pdf_context or {}
    quotes = {key: quote for key, quote in (context.get("metadata_evidence") or {}).items()
              if key in {"age_unit", "sr_ca_unit", "d18o_unit", "d13c_unit", "sst_unit"} and isinstance(quote, str)}
    columns = {}
    for col in headers:
        definition = definitions.get(col) or variables.get(str(col).strip().casefold(), {})
        proposed = (table.get("layout_definitions") or {}).get(col)
        snippet = definition.get("full_definition") or "; ".join(
            f"{key}={definition[key]}" for key in
            ("description", "unit", "material", "data_type", "method", "processing", "seasonality")
            if definition.get(key)
        )
        values = {
            "sample_id": (table.get("sample_by_column") or {}).get(col),
            "unit_row": units.get(col),
            "archive_unit": defined_units.get(col) or definition.get("unit"),
            "definition": str(snippet)[:400],
            "definition_source": (definition.get("source") or "archive_definition") if snippet else None,
            "definition_row": definition.get("source_definition_row"),
            "layout_definition_candidate": {key: proposed[key] for key in ("name", "description", "unit", "source_definition_row")
                                            if key in proposed} if proposed and not definition else None,
            "date_order": (table.get("date_orders") or {}).get(col),
            "samples": (table.get("column_samples") or {}).get(col, samples[col]),
            "paper_quote_keys": [key for key, quote in quotes.items() if quote_names_column(quote, col)],
        }
        columns[col] = {key: value for key, value in values.items() if value}

    paper = {key: str(context[key])[:200] for key in ("pub1_doi", "geo_siteName", "geo_ocean", "geo_reefType",
                                                       "paleoData_dating_method", "sr_ca_calibration_info")
             if context.get(key)}
    paper.update({f"{key}_quote": quote[:1000] for key, quote in quotes.items()})
    if context.get("metadata_source"):
        paper["source"] = os.path.basename(context["metadata_source"])
    if context.get("paper_link"):
        paper["paper_link"] = context["paper_link"]
    if context.get("isotope_scale_evidence_page"):
        paper["isotope_scale_page"] = context["isotope_scale_evidence_page"]
    return {
        "table_source": {key: str(value) for key, value in
                         (("file", os.path.basename(table.get("file_path") or "")), ("sheet", table.get("sheet_name")),
                          ("page", table.get("source_page")), ("header_line", table.get("source_header_line"))) if value},
        "table_headers": headers,
        "table_description": str(table.get("table_description") or "")[:3000],
        "archive_context": {key: str(archive[key])[:1200 if key == "archive_description" else 300] for key in
                            ("source_time_unit", "source_record_id", "pub1_doi", "dataset_doi", "geo_siteName", "archive_description")
                            if archive.get(key)},
        "document_context": paper,
        "column_evidence": columns,
    }


class ContextExtractor:
    """上下文提取器：从MD正文中提取辅助元数据"""
    
    def __init__(self, cache=None):
        """初始化上下文提取器"""
        self.cache = cache
        # 参考文献关键词，用于截断正文
        self.reference_keywords = [
            r'^\s*references?\s*$',
            r'^\s*bibliography\s*$',
            r'^\s*literature\s+cited\s*$',
            r'^\s*参考文献\s*$',
            r'^\s*引用文献\s*$',
            r'^\#\#?\s*references?',
            r'^\#\#?\s*bibliography',
        ]
        
        # 编译参考文献正则表达式
        self.ref_patterns = [re.compile(p, re.IGNORECASE | re.MULTILINE) for p in self.reference_keywords]
    
    def extract_from_md(self, md_path: str) -> Dict[str, Any]:
        """
        从MD文件提取上下文元数据
        
        Args:
            md_path: MD文件路径
            
        Returns:
            Dict: 提取的元数据字典
        """
        try:
            with open(md_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            return self.extract_from_text(content, source_name=md_path)
        except Exception as e:
            logger.error(f"读取MD文件失败: {md_path}, 错误: {e}")
            return {}
    
    def extract_from_text(self, content: str, source_name: str = "unknown") -> Dict[str, Any]:
        """
        从文本内容提取元数据
        
        Args:
            content: 文本内容
            source_name: 来源名称（用于日志）
            
        Returns:
            Dict: 提取的元数据字典
        """
        try:
            # 截断参考文献之前的内容
            truncated_content = self._truncate_before_references(content)
            logger.info(f"正文内容截断至 {len(truncated_content)} 字符（原始 {len(content)} 字符）")
            
            # 如果内容太短，可能没有有效信息
            if len(truncated_content) < 100:
                logger.warning("截断后内容过短，可能缺少有效信息")
                return {}
            
            # 限制最大长度（qwen系列支持1M上下文，按1字符≈1token保守估计留足余量）
            MAX_CONTENT_LENGTH = 600000
            if len(truncated_content) > MAX_CONTENT_LENGTH:
                truncated_content = truncated_content[:MAX_CONTENT_LENGTH]
                logger.info(f"内容超出限制，截断至 {MAX_CONTENT_LENGTH} 字符")
            
            # 构建提示词
            prompt = self._build_extraction_prompt(truncated_content)
            
            system_prompt = llm_base_prompt()
            cache_key = ""
            cached = None
            if self.cache is not None:
                request_digest = sha256(f"{system_prompt}\n{prompt}".encode("utf-8")).hexdigest()
                cache_key = (f"paper-context-v1|{settings.LLM_BASE_URL.rstrip('/')}|"
                             f"{MODEL_ROLES['flash']}|think=0|temperature=0|{request_digest}")
                cached = self.cache.get([], cache_key)
            result = cached.get("response") if cached else None
            if not result:
                result = call_llm("flash", prompt, system_prompt=system_prompt, enable_thinking=False, temperature=0)
            extracted, cacheable = self._parse_llm_result(result) if result else ({}, False)
            if cacheable and extracted and not cached and self.cache is not None:
                self.cache.set([], {"response": result}, cache_key)
            # 命中缓存时也用当前正文验证引文，并按当前源路径补查标尺。
            body = " ".join(truncated_content.split())
            evidence = {key: quote for key, quote in extracted.get("metadata_evidence", {}).items()
                        if isinstance(quote, str) and 8 <= len(quote.strip()) <= 1000
                        and " ".join(quote.split()) in body}
            isotope_keys = ("d18o_unit", "d13c_unit")
            if not all(evidence.get(key) for key in isotope_keys):
                match = find_skeletal_isotope_scale(source_name, body)
                if match:
                    scale, page, quote = match
                    for key in isotope_keys:
                        evidence.setdefault(key, quote)
                        if not extracted.get(f"default_{key}") or extracted[f"default_{key}"] == "unknown":
                            extracted[f"default_{key}"] = f"‰ {scale}"
                    extracted["isotope_scale_evidence_page"] = page
            if extracted:
                extracted["metadata_evidence"] = evidence
                extracted["metadata_source"] = source_name
                logger.info(f"从正文提取到元数据: {list(extracted.keys())}")
            
            return extracted
            
        except Exception as e:
            logger.error(f"提取上下文元数据失败: {e}")
            return {}
    
    def _truncate_before_references(self, content: str) -> str:
        """
        截断参考文献之前的内容
        
        Args:
            content: 原始文本内容
            
        Returns:
            str: 截断后的内容
        """
        # 查找参考文献开始位置
        earliest_pos = len(content)
        
        for pattern in self.ref_patterns:
            match = pattern.search(content)
            if match and match.start() < earliest_pos:
                earliest_pos = match.start()
        
        # 如果找到了参考文献，截断到该位置
        if earliest_pos < len(content):
            return content[:earliest_pos]
        
        return content
    
    def _build_extraction_prompt(self, content: str) -> str:
        """
        构建元数据提取的Prompt
        
        Args:
            content: 正文内容
            
        Returns:
            str: 提取Prompt
        """
        prompt = f"""你是一个古气候数据提取专家。请从以下珊瑚古气候研究论文正文中提取关键元数据信息。

【提取目标】
1. **文献信息**：
   - doi: 文章的DOI号（如 10.1016/j.gloplacha.2025.104718）
   - authors: 作者信息，**必须只提取第一作者姓名加发表年份**，格式为"第一作者 et al. 发表年份"（如 "Chen et al. 2018"、"Guo et al. 2025"），不要返回完整作者列表

2. **地理位置信息**：
   - site_name: 研究站点名称
   - latitude: 纬度（十进制度数，南纬为负）
   - longitude: 经度（十进制度数，西经为负）
   - research_sites: 如果有多个研究站点，提供列表

3. **年代学信息**：
   - dating_method: 测年方法（如 U/Th, C14, annual band counting 等）
   - dating_error: 测年误差（如 "±2 years"）
   - base_year: 基准年份（用于 Internal Chronology 转 CE，如珊瑚采集年份）
   - measurement_year: 测量年份（用于 BP 转 CE，通常是文章发表年份或实验年份）
   - holocene_stage: 全新世时段（Early/Middle/Late Holocene）

4. **测试方法**：
   - testing_method: 测试方法（如 ICP-OES, ICP-MS, TIMS, LA-ICP-MS, IRMS 等）

5. **缺失单位信息**（从Methods章节或正文描述中提取）：
   - age_unit: 年龄单位（CE, BP, ka BP, AD 等）
   - sr_ca_unit: Sr/Ca 单位（mmol/mol, μmol/mol 等）
   - d18o_unit: δ18O 单位及明确的参考标尺（如 ‰ VPDB）
   - d13c_unit: δ13C 单位及明确的参考标尺（如 ‰ VPDB）
   - sst_unit: SST 单位（°C, ℃ 等）

【输出格式】
请严格按照以下JSON格式输出，如果某项信息未找到则设为null：
```json
{{
    "doi": "DOI号或null",
    "authors": "作者信息或null",
    "site_name": "站点名称",
    "latitude": 数值或null,
    "longitude": 数值或null,
    "research_sites": [{{"id": "站点ID", "latitude": 数值, "longitude": 数值}}] 或 null,
    "dating_method": "测年方法",
    "dating_error": "测年误差或null",
    "base_year": 数值或null,
    "measurement_year": 数值或null,
    "holocene_stage": "全新世时段或null",
    "testing_method": "测试方法或null",
    "age_unit": "年龄单位",
    "sr_ca_unit": "Sr/Ca单位",
    "d18o_unit": "δ18O单位及参考标尺",
    "d13c_unit": "δ13C单位及参考标尺",
    "sst_unit": "SST单位",
    "evidence": {{"doi": "正文原句", "sr_ca_unit": "正文原句", "d18o_unit": "正文原句", "d13c_unit": "正文原句"}}
}}
```

【论文正文】
{content}

【重要提示】
- 只输出JSON，不要输出其他内容
- DOI通常在文章开头或脚注中，格式如 "doi: 10.xxxx/xxx" 或 "https://doi.org/10.xxxx"
- **authors字段必须是"第一作者 et al. 发表年份"格式**，仅使用刊物正式发表年份，不从 Received 或 Accepted 日期猜测
- 经纬度必须转换为十进制格式（如 5°52'N → 5.867）
- 如果有多个研究站点，在research_sites中列出所有站点
- base_year 通常是珊瑚采集的年份，如 "cored in 2007" → base_year=2007
- measurement_year 仅填写明确报告的测量年份，不能从发表年份推断
- evidence 可包含任一非空字段的原文支持片段，使用上述字段名作键
- 单位或参考标尺必须有原文证据，无证据时设为 null"""

        return prompt
    
    def _parse_llm_result(self, result: str) -> tuple[Dict[str, Any], bool]:
        """
        解析LLM返回的JSON结果
        
        Args:
            result: LLM返回的文本
            
        Returns:
            解析后的元数据，以及响应是否为可复用的完整 JSON 对象
        """
        try:
            # 尝试提取JSON块
            json_match = re.search(r'```json\s*(.*?)\s*```', result, re.DOTALL)
            if json_match:
                json_str = json_match.group(1)
            else:
                # 尝试直接解析整个结果
                json_str = result.strip()
                # 移除可能的前后缀
                if json_str.startswith('```'):
                    json_str = json_str[3:]
                if json_str.endswith('```'):
                    json_str = json_str[:-3]
            
            # 解析JSON
            data = json.loads(json_str)
            
            # 标准化结果
            extracted = {}
            
            # 文献信息
            if data.get('doi'):
                extracted['pub1_doi'] = correct_archive_doi(data['doi'])
            if data.get('authors'):
                extracted['pub1_authors'] = data['authors']
            
            # 地理位置
            if data.get('site_name'):
                extracted['geo_siteName'] = data['site_name']
            if data.get('latitude') is not None:
                try:
                    extracted['geo_latitude'] = float(data['latitude'])
                except (ValueError, TypeError):
                    pass
            if data.get('longitude') is not None:
                try:
                    extracted['geo_longitude'] = float(data['longitude'])
                except (ValueError, TypeError):
                    pass
            
            # 多个研究站点
            if data.get('research_sites') and isinstance(data['research_sites'], list):
                research_sites = {}
                for site in data['research_sites']:
                    if isinstance(site, dict) and site.get('id'):
                        site_id = site['id']
                        research_sites[site_id] = {
                            'latitude': site.get('latitude'),
                            'longitude': site.get('longitude')
                        }
                if research_sites:
                    extracted['research_sites'] = research_sites
            
            # 年代学信息
            if data.get('dating_method'):
                extracted['paleoData_dating_method'] = data['dating_method']
            if data.get('dating_error'):
                extracted['paleoData_dating_error'] = data['dating_error']
            if data.get('holocene_stage'):
                extracted['paleoData_holocene_stage'] = data['holocene_stage']
            if data.get('base_year') is not None:
                try:
                    extracted['base_year'] = int(data['base_year'])
                except (ValueError, TypeError):
                    pass
            if data.get('measurement_year') is not None:
                try:
                    extracted['measurement_year'] = int(data['measurement_year'])
                except (ValueError, TypeError):
                    pass
            
            # 测试方法
            if data.get('testing_method'):
                extracted['testing_method'] = data['testing_method']
            
            # 缺省单位
            if data.get('age_unit'):
                extracted['default_age_unit'] = data['age_unit']
            if data.get('sr_ca_unit'):
                extracted['default_sr_ca_unit'] = data['sr_ca_unit']
            if data.get('d18o_unit'):
                extracted['default_d18o_unit'] = data['d18o_unit']
            if data.get('d13c_unit'):
                extracted['default_d13c_unit'] = data['d13c_unit']
            if data.get('sst_unit'):
                extracted['default_sst_unit'] = data['sst_unit']
            
            if isinstance(data.get("evidence"), dict):
                extracted["metadata_evidence"] = data["evidence"]

            return extracted, True
            
        except json.JSONDecodeError as e:
            logger.warning(f"JSON解析失败: {e}")
            # 尝试正则表达式提取关键信息
            return self._fallback_extraction(result), False
        except Exception as e:
            logger.error(f"解析LLM结果失败: {e}")
            return {}, False
    
    def _fallback_extraction(self, text: str) -> Dict[str, Any]:
        """
        降级提取：当JSON解析失败时使用正则表达式提取
        
        Args:
            text: LLM返回的文本
            
        Returns:
            Dict: 提取的元数据
        """
        extracted = {}
        
        # 提取站点名称
        site_match = re.search(r'"site_name"\s*:\s*"([^"]+)"', text)
        if site_match:
            extracted['geo_siteName'] = site_match.group(1)
        
        # 提取经纬度
        lat_match = re.search(r'"latitude"\s*:\s*(-?\d+\.?\d*)', text)
        if lat_match:
            extracted['geo_latitude'] = float(lat_match.group(1))
        
        lon_match = re.search(r'"longitude"\s*:\s*(-?\d+\.?\d*)', text)
        if lon_match:
            extracted['geo_longitude'] = float(lon_match.group(1))
        
        # 提取测年方法
        dating_match = re.search(r'"dating_method"\s*:\s*"([^"]+)"', text)
        if dating_match:
            extracted['paleoData_dating_method'] = dating_match.group(1)
        
        # 提取基准年份
        base_match = re.search(r'"base_year"\s*:\s*(\d+)', text)
        if base_match:
            extracted['base_year'] = int(base_match.group(1))
        
        # 提取测量年份
        meas_match = re.search(r'"measurement_year"\s*:\s*(\d+)', text)
        if meas_match:
            extracted['measurement_year'] = int(meas_match.group(1))
        
        return extracted


# 便捷函数
def extract_context_from_md(md_path: str) -> Dict[str, Any]:
    """
    便捷函数：从MD文件提取上下文元数据
    
    Args:
        md_path: MD文件路径
        
    Returns:
        Dict: 提取的元数据字典
    """
    extractor = ContextExtractor()
    return extractor.extract_from_md(md_path)


def extract_context_from_text(content: str) -> Dict[str, Any]:
    """
    便捷函数：从文本内容提取上下文元数据
    
    Args:
        content: 文本内容
        
    Returns:
        Dict: 提取的元数据字典
    """
    extractor = ContextExtractor()
    return extractor.extract_from_text(content)


