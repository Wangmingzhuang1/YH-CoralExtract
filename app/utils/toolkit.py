#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
古气候数据提取工具包
功能：提供PDF处理、文件管理、表格提取、数据清洗等核心辅助功能
"""

import os
import re
import hashlib
import logging
from urllib.parse import urljoin
from tqdm import tqdm
import pandas as pd
from typing import List, Dict, Optional, Union
from docx import Document  # 处理Word
import openpyxl  # 处理Excel

from app.core.config import LOCAL_PDF_PATH, PARSED_TEXT_PATH  # 从配置文件导入路径常量

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# 添加字段映射表
FIELD_MAPPING = {
    # 统一中英文字段名
    "采样地点": "site_name",
    "站点名称": "site_name",
    "location": "site_name",
    "年代": "year",
    "年份": "year",
    "时间": "year",
    "海表温度": "sst",
    "temperature": "sst",
    "δ18O": "d18o",
    "氧同位素": "d18o",
    "Sr/Ca": "sr_ca",
    "锶钙比": "sr_ca"
}

def unify_fields(data):
    """统一不同附录的字段名"""
    unified = {}
    for raw_key, value in data.items():
        # 模糊匹配映射表（忽略大小写）
        matched_key = next(
            (mapped for raw, mapped in FIELD_MAPPING.items() 
             if raw.lower() in raw_key.lower()), 
            raw_key.lower()  # 未匹配则转为小写
        )
        unified[matched_key] = value
    return unified

def standardize_time(data):
    """将各种时间格式统一为年份（如处理'公元1900年'、'1950-1960'、'18世纪'），但保留小数部分"""
    if "year" not in data or not data["year"]:
        return data
    
    year_value = data["year"]
    standardized = None
    
    # 处理范围格式（如1900-2000）
    if isinstance(year_value, str) and "-" in year_value:
        parts = [p.strip() for p in year_value.split("-")[:2]]
        if all(part.replace('.', '', 1).isdigit() for part in parts):  # 允许小数
            standardized = {"start": float(parts[0]), "end": float(parts[1])}
    
    # 处理单一年份（如1950，公元1950年）
    if standardized is None:
        if isinstance(year_value, str):
            # 提取数字部分（包括小数）
            year_num = re.search(r'\b\d+\.?\d*\b', year_value)
            if year_num:
                standardized = {"start": float(year_num.group()), "end": float(year_num.group())}
        elif isinstance(year_value, (int, float)) and 1000 <= year_value <= 2100:
            standardized = {"start": float(year_value), "end": float(year_value)}
    
    # 处理世纪（如18世纪 -> 1701-1800）
    if standardized is None and isinstance(year_value, str):
        century_match = re.search(r'(\d+)\s*世纪', year_value)
        if century_match:
            century = int(century_match.group(1))
            standardized = {"start": float((century-1)*100 + 1), "end": float(century*100)}
    
    if standardized:
        data["year"] = standardized
    else:
        logging.warning(f"无法标准化时间格式: {year_value}")
        data["year_invalid"] = True
    return data

def clean_coral_data(data):
    """
    对提取的数据进行清洗（符合文献标准化要求）
    结合了tool.py和tool_simple.py中的清洗功能
    """
    # 1. 统一字段名（来自tool.py）
    data = unify_fields(data)
    
    # 2. 补充人工数据可能缺失的单位处理（来自tool.py）
    # 例如：若人工Excel中单位列缺失，添加默认单位推断
    if 'sst' in data and 'sst_unit' not in data:
        data['sst_unit'] = '°C'  # 假设默认温度单位
    if 'sr_ca' in data and 'sr_ca_unit' not in data:
        data['sr_ca_unit'] = 'mmol/mol'  # 假设默认锶钙比单位
    if 'd18o' in data and 'd18o_unit' not in data:
        data['d18o_unit'] = '‰'  # 假设默认氧同位素单位
    
    # 3. 增强异常值处理（来自tool.py）
    outlier_thresholds = {
        'sst': {'min': -2.0, 'max': 35.0},
        'sr_ca': {'min': 6.0, 'max': 12.0},
        'd18o': {'min': -10.0, 'max': 5.0}
        # ... 其他阈值
    }
    
    # 应用异常值处理
    for field, thresholds in outlier_thresholds.items():
        value_key = f"{field}_value"
        if value_key in data and data[value_key] is not None:
            try:
                value = float(data[value_key])
                if value < thresholds['min'] or value > thresholds['max']:
                    logging.warning(f"异常值检测: {field} 值 {value} 超出范围 [{thresholds['min']}, {thresholds['max']}]")
                    # 可以选择删除异常值或标记为需要审查
                    # 这里我们选择标记为需要审查
                    data[f"{field}_needs_review"] = True
            except (ValueError, TypeError):
                logging.warning(f"无法转换 {field} 值 {data[value_key]} 为浮点数")
    
    # 4. 时间标准化（来自tool.py）
    data = standardize_time(data)
    
    # 5. 跨附录去重（基于关键维度）（来自tool.py）
    # 生成唯一标识：站点+时间范围+数据类型
    if all(k in data for k in ["site_name", "year", "data_type"]):
        if isinstance(data["year"], dict) and "start" in data["year"] and "end" in data["year"]:
            time_key = f"{data['year']['start']}-{data['year']['end']}"
        else:
            time_key = str(data["year"])
        data["unique_id"] = hashlib.md5(
            f"{data['site_name']}_{time_key}_{data['data_type']}".encode()
        ).hexdigest()
    
    # 6. 补充元数据（来源附录、解析可信度）（来自tool.py）
    data["appendix_source"] = data.get("appendix_source", "unknown")  # 记录来自哪个附录
    data["confidence"] = data.get("confidence", 0.8)  # 可基于解析难度调整可信度
    
    # 7. 增加人工数据来源标记（来自tool.py）
    data["data_source"] = "manual_appendix"  # 标记数据来自人工附件
    
    # 8. 单位统一处理（来自tool_simple.py）
    # 处理温度数据
    if 'temperature' in data or '海表温度' in data:
        # 统一温度单位为℃
        temp_key = 'temperature' if 'temperature' in data else '海表温度'
        temp_value = data.get(temp_key)
        temp_unit = data.get(f'{temp_key}_unit', '')
        
        # 提取数值和单位
        if isinstance(temp_value, str):
            # 使用正则表达式提取数值和单位
            match = re.search(r'([+-]?\d*\.?\d+)\s*([°℃CcFf]*)', temp_value)
            if match:
                value = float(match.group(1))
                unit = match.group(2).lower() if match.group(2) else temp_unit.lower()
                
                # 单位转换
                if 'f' in unit:  # 华氏度转摄氏度
                    value = (value - 32) * 5/9
                    data[temp_key] = round(value, 2)
                    data[f'{temp_key}_unit'] = '°C'
                elif 'k' in unit:  # 开尔文转摄氏度
                    value = value - 273.15
                    data[temp_key] = round(value, 2)
                    data[f'{temp_key}_unit'] = '°C'
                else:  # 默认为摄氏度
                    data[temp_key] = value
                    data[f'{temp_key}_unit'] = '°C'
    
    # 处理δ18O数据
    if 'δ18O' in data or '氧同位素' in data:
        # 统一单位为‰
        delta_key = 'δ18O' if 'δ18O' in data else '氧同位素'
        delta_value = data.get(delta_key)
        delta_unit = data.get(f'{delta_key}_unit', '')
        
        # 提取数值
        if isinstance(delta_value, str):
            # 使用正则表达式提取数值
            match = re.search(r'([+-]?\d*\.?\d+)', delta_value)
            if match:
                value = float(match.group(1))
                data[delta_key] = value
                data[f'{delta_key}_unit'] = '‰'
    
    # 处理Sr/Ca数据
    if 'Sr/Ca' in data or '锶钙比' in data:
        # 统一单位为mmol/mol
        srca_key = 'Sr/Ca' if 'Sr/Ca' in data else '锶钙比'
        srca_value = data.get(srca_key)
        srca_unit = data.get(f'{srca_key}_unit', '')
        
        # 提取数值和单位
        if isinstance(srca_value, str):
            # 使用正则表达式提取数值和单位
            match = re.search(r'([+-]?\d*\.?\d+)\s*([μm]?mol/mol)', srca_value)
            if match:
                value = float(match.group(1))
                unit = match.group(2).lower()
                
                # 单位转换
                if 'μmol/mol' in unit:  # 微摩尔/摩尔转毫摩尔/摩尔
                    value = value * 0.001
                    data[srca_key] = value
                    data[f'{srca_key}_unit'] = 'mmol/mol'
                else:  # 默认为毫摩尔/摩尔
                    data[srca_key] = value
                    data[f'{srca_key}_unit'] = 'mmol/mol'
    
    return data

def search_keyword_score(keywords: List[Dict]) -> List[Dict]:
    """
    对关键词按重要性评分并降序排序（用于数据提取中的关键词权重排序）
    
    参数：
        keywords: 关键词列表，每个元素为字典，需包含 'importance_score' 字段（评分值）
    返回：
        按评分降序排序的关键词列表
    示例：
        input: [{'word': 'δ¹⁸O', 'importance_score': 0.9}, {'word': 'Sr/Ca', 'importance_score': 0.8}]
        output: [{'word': 'δ¹⁸O', 'importance_score': 0.9}, {'word': 'Sr/Ca', 'importance_score': 0.8}]
    """
    if not keywords:
        return []
    # 过滤缺少评分字段的关键词，默认评分为0
    valid_keywords = [
        kw for kw in keywords 
        if isinstance(kw.get('importance_score'), (int, float))
    ]
    # 按评分降序排序
    return sorted(valid_keywords, key=lambda x: x['importance_score'], reverse=True)

def get_local_pdf_paths(root_dir: str = LOCAL_PDF_PATH) -> List[str]:
    """
    递归遍历目录，获取所有PDF文件的绝对路径
    
    参数：
        root_dir: 根目录路径（默认使用配置文件中的LOCAL_PDF_PATH）
    返回：
        所有PDF文件路径的列表（路径不存在时返回空列表）
    异常：
        若root_dir不存在，返回空列表并打印警告
    """
    if not os.path.exists(root_dir):
        print(f"警告：PDF根目录不存在 - {root_dir}")
        return []
    
    pdf_paths = []
    for root, _, files in os.walk(root_dir):
        for file in files:
            if file.lower().endswith(".pdf"):
                pdf_paths.append(os.path.abspath(os.path.join(root, file)))
    return pdf_paths

def generate_file_hash(file_path: str) -> Optional[str]:
    """
    生成文件的MD5哈希值（用于校验文件唯一性，避免重复解析）
    
    参数：
        file_path: 文件绝对路径
    返回：
        32位MD5哈希字符串（文件不存在或读取失败时返回None）
    """
    if not os.path.isfile(file_path):
        print(f"错误：文件不存在 - {file_path}")
        return None
    
    try:
        with open(file_path, "rb") as f:
            # 分块读取大文件，避免内存占用过高
            md5_hash = hashlib.md5()
            chunk = f.read(4096)
            while chunk:
                md5_hash.update(chunk)
                chunk = f.read(4096)
            return md5_hash.hexdigest()
    except Exception as e:
        print(f"计算文件哈希失败 - {file_path}：{str(e)}")
        return None

def extract_attachment_links(text_save_path: str) -> List[str]:
    """
    从解析后的文本中提取附件链接
    
    参数：
        text_save_path: 解析后文本存储路径
    返回：
        附件链接列表
    """
    if not os.path.exists(text_save_path):
        return []
    
    try:
        with open(text_save_path, "r", encoding="utf-8") as f:
            content = f.read()
        
        # 使用正则表达式提取可能的附件链接
        # 匹配常见的附件文件扩展名
        attachment_pattern = r'(https?://[^\s]*?\.(?:xlsx|xls|docx|doc|pdf|csv|txt)(?:\?[^\s]*)?)'
        links = re.findall(attachment_pattern, content, re.IGNORECASE)
        return links
    except Exception as e:
        print(f"提取附件链接失败 - {text_save_path}：{str(e)}")
        return []

def parse_pdf(pdf_path: str) -> str:
    """基础PDF文本提取函数"""
    try:
        import pdfplumber
        text = ""
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text += page.extract_text() or ""
        return text
    except Exception as e:
        print(f"PDF解析失败 {pdf_path}：{str(e)}")
        return ""

def parse_attachments(attachment_paths: List[str]) -> str:
    """解析Excel/Word/PDF附件内容为文本，用于后续信息提取"""
    combined_text = ""
    for path in attachment_paths:
        try:
            if path.endswith((".xlsx", ".xls")):
                # 提取Excel表格内容
                wb = openpyxl.load_workbook(path, read_only=True)
                for sheet in wb.sheetnames:
                    ws = wb[sheet]
                    for row in ws.iter_rows(values_only=True):
                        combined_text += " ".join([str(cell) if cell else "" for cell in row]) + "\n"
                wb.close()
            
            elif path.endswith(".docx"):
                # 提取Word文本
                doc = Document(path)
                for para in doc.paragraphs:
                    combined_text += para.text + "\n"
            
            elif path.endswith(".pdf"):
                # 复用现有PDF解析逻辑
                combined_text += parse_pdf(path)
        except Exception as e:
            print(f"附件解析失败 {path}：{str(e)}")
    
    return combined_text

def batch_process_local_pdfs(
    root_dir: str = LOCAL_PDF_PATH,
    parsed_dir: str = PARSED_TEXT_PATH
) -> List[Dict]:
    """
    批量处理本地PDF文件，生成包含路径、哈希、解析状态的元数据列表
    
    参数：
        root_dir: PDF文件根目录（默认使用配置文件路径）
        parsed_dir: 解析后文本存储目录（默认使用配置文件路径）
    返回：
        每个元素为字典，包含：
            - pdf_path: PDF文件绝对路径
            - file_hash: 文件MD5哈希（唯一标识）
            - text_save_path: 解析后文本存储路径
            - is_parsed: 是否已解析（True/False）
    """
    # 确保解析目录存在
    os.makedirs(parsed_dir, exist_ok=True)
    
    # 获取所有PDF路径
    pdf_paths = get_local_pdf_paths(root_dir)
    if not pdf_paths:
        print("提示：未找到任何PDF文件")
        return []
    
    # 批量处理并生成元数据
    processed_metadata = []
    for pdf_path in tqdm(pdf_paths, desc="批量处理PDF文件"):
        file_hash = generate_file_hash(pdf_path)
        if not file_hash:
            continue  # 哈希生成失败则跳过
        
        text_save_path = os.path.join(parsed_dir, f"{file_hash}.md")
        
        # 即使已解析，仍检查外部附件
        is_parsed = os.path.exists(text_save_path)
        if is_parsed:
            # 读取已解析的文本
            with open(text_save_path, "r", encoding="utf-8") as f:
                pdf_text = f.read()
            
            # 附件处理逻辑已移除，因为使用人工准备的附件
        else:
            # 如果未解析，添加处理逻辑（可选）
            pass
        
        processed_metadata.append({
            "pdf_path": pdf_path,
            "file_hash": file_hash,
            "text_save_path": text_save_path,
            "is_parsed": is_parsed
        })
    
    return processed_metadata

def extract_tables_from_docx(docx_path):
    """从Word文档中提取表格数据（处理人工Word中的不规则表格）"""
    try:
        doc = Document(docx_path)
        table_data = []
        
        for table in doc.tables:
            # 提取表格数据
            data = []
            for row in table.rows:
                row_data = []
                for cell in row.cells:
                    row_data.append(cell.text.strip())
                data.append(row_data)
            
            # 处理人工Word可能的不规则表格
            if data:
                # 删除全空行
                data = [row for row in data if any(cell.strip() for cell in row)]
                
                # 如果表格不为空，转换为DataFrame
                if data:
                    df = pd.DataFrame(data[1:], columns=data[0])  # 第一行为列名
                    table_data.append(df)
        
        return table_data
    except Exception as e:
        logging.error(f"解析Word文档失败: {str(e)}")
        return []

def extract_tables_from_excel(excel_path):
    """从Excel文件中提取表格数据（处理人工Excel可能的合并单元格、空行）"""
    try:
        # 读取所有工作表
        excel_file = pd.ExcelFile(excel_path)
        table_data = []
        
        for sheet_name in excel_file.sheet_names:
            # 处理人工Excel可能的合并单元格、空行
            df = pd.read_excel(excel_path, sheet_name=sheet_name, engine="openpyxl")
            # 删除全空行
            df = df.dropna(how='all')
            # 删除全空列
            df = df.dropna(axis=1, how='all')
            table_data.append(df)
        
        return table_data
    except Exception as e:
        logging.error(f"解析Excel文件失败: {str(e)}")
        return []

def extract_tables_from_md(md_content):
    """
    从Markdown内容中提取表格数据
    结合了tool.py和tool_simple.py中的实现
    """
    try:
        # 首先尝试使用tool_simple.py中的方法
        lines = md_content.split('\n')
        tables = []
        current_table = []
        in_table = False
        
        for line in lines:
            # 检查是否为表格分隔行（如 |---|---|）
            if re.match(r'^\s*\|.*\|.*\|.*\|$', line) and re.match(r'^\s*\|(\s*[-:]+\s*\|)+', line):
                in_table = True
                continue
            
            # 检查是否为表格数据行
            if re.match(r'^\s*\|.*\|.*\|.*\|$', line):
                if not in_table:
                    # 开始新的表格
                    current_table = []
                    in_table = True
                current_table.append(line)
            else:
                # 表格结束
                if in_table and current_table:
                    # 处理当前表格
                    try:
                        # 转换为DataFrame
                        processed_lines = []
                        for table_line in current_table:
                            # 移除首尾的|和空格
                            cleaned_line = table_line.strip().strip('|')
                            # 分割为单元格
                            cells = [cell.strip() for cell in cleaned_line.split('|')]
                            processed_lines.append(cells)
                        
                        if processed_lines:
                            # 创建DataFrame
                            df = pd.DataFrame(processed_lines[1:], columns=processed_lines[0])
                            tables.append(df)
                    except Exception as e:
                        logging.warning(f"解析Markdown表格失败: {str(e)}")
                
                in_table = False
                current_table = []
        
        # 处理最后一个表格（如果有的话）
        if in_table and current_table:
            try:
                # 转换为DataFrame
                processed_lines = []
                for table_line in current_table:
                    # 移除首尾的|和空格
                    cleaned_line = table_line.strip().strip('|')
                    # 分割为单元格
                    cells = [cell.strip() for cell in cleaned_line.split('|')]
                    processed_lines.append(cells)
                
                if processed_lines:
                    # 创建DataFrame
                    df = pd.DataFrame(processed_lines[1:], columns=processed_lines[0])
                    tables.append(df)
            except Exception as e:
                logging.warning(f"解析Markdown表格失败: {str(e)}")
        
        # 如果没有提取到表格，尝试使用tool.py中的方法
        if not tables:
            # 正则匹配Markdown表格（表头+分隔线+内容行）
            table_pattern = re.compile(
                r'^\|.*\|.*$(?:\r?\n\|.*\|.*$){1,}',  # 表头行 + 至少一行内容行
                re.MULTILINE  # 多行匹配
            )
            table_matches = table_pattern.findall(md_content)
            
            tables = []
            for table_str in table_matches:
                try:
                    lines = [line.strip() for line in table_str.split('\n') if line.strip()]
                    if len(lines) < 2:
                        continue  # 至少需要表头+1行内容
                    
                    # 解析表头（去除首尾|，按|分割并去空格）
                    headers = [h.strip() for h in lines[0].strip('|').split('|') if h.strip()]
                    if not headers:
                        continue
                    
                    # 解析内容行（跳过分隔线行，如 |---|---|）
                    data_rows = []
                    for line in lines[1:]:
                        if re.fullmatch(r'\|(?:\s*[-:]+\s*\|)+', line):
                            continue  # 跳过分隔线
                        row = [cell.strip() for cell in line.strip('|').split('|') if cell.strip()]
                        # 确保行长度与表头一致
                        if len(row) == len(headers):
                            data_rows.append(row)
                    
                    if data_rows:
                        # 使用pandas的Index来避免类型错误
                        header_index = pd.Index(headers)
                        tables.append(pd.DataFrame(data_rows, columns=header_index))
                
                except Exception as e:
                    print(f"解析Markdown表格失败：{str(e)}")
                    continue
        
        return tables
    except Exception as e:
        logging.error(f"解析Markdown内容失败: {str(e)}")
        return []

def get_related_keywords(target_keyword: str) -> List[str]:
    """
    获取目标关键词的相关关键词（预留接口，可扩展为词向量匹配或规则匹配）
    
    参数：
        target_keyword: 目标关键词（如 'δ¹⁸O'）
    返回：
        相关关键词列表（默认返回空列表，需根据业务补充逻辑）
    示例扩展方向：
        - 规则匹配：若target_keyword为'δ¹⁸O'，返回['氧同位素', 'δ18O']
        - 词向量：通过预训练模型计算相似词
    """
    # 预留实现逻辑
    return []

def filter_empty_key_fields(data_list: List[Dict]) -> List[Dict]:
    """保留有有限 CE 年代及至少一个受支持观测指标的记录。"""
    from app.core.moa.unit_converter import UnitConversionModule, finite_number

    fields = UnitConversionModule.default_units
    return [
        point for point in (data_list or [])
        if finite_number(point.get("year")) is not None
        and any(finite_number(point.get(field)) is not None for field in fields)
    ]
