#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
附录文件处理工具
根据附录文件类型（Excel/Word/PDF）选择解析工具，输出结构化数据或文本
"""

import os
import time
import pandas as pd
import pdfplumber
import json
import logging
import hashlib
import re
from docx import Document
from urllib.parse import urlparse
# 已移除pdf_to_md模块，不再需要复用PDF转MD逻辑
from app.core.config import PARSED_TEXT_PATH
from app.utils.toolkit import extract_tables_from_md  # 复用表格提取逻辑

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def calculate_file_md5(file_path):
    """计算文件的MD5值用于完整性校验"""
    hash_md5 = hashlib.md5()
    try:
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()
    except Exception as e:
        logging.error(f"计算文件MD5失败: {str(e)}")
        return None

# 项目已改为处理本地已下载的附件文件，不再需要下载功能
# 为了兼容性，提供一个简化版本的函数，直接返回本地文件路径
def download_appendix_file(url, save_dir):
    """处理本地附录文件并保存到指定目录（简化版本）"""
    try:
        import os
        import shutil
        from urllib.parse import urlparse
        
        # 确保保存目录存在
        os.makedirs(save_dir, exist_ok=True)
        
        # 解析URL获取文件名
        parsed_url = urlparse(url)
        filename = os.path.basename(parsed_url.path)
        
        # 如果没有文件名，使用默认名称
        if not filename:
            filename = "appendix_file"
        
        # 完整保存路径
        file_path = os.path.join(save_dir, filename)
        
        # 如果URL指向本地文件，则复制该文件
        if os.path.exists(url):
            shutil.copy2(url, file_path)
            logging.info(f"本地附录文件处理成功: {file_path}")
            return file_path
        else:
            # 如果是网络URL，记录日志但不实际下载
            logging.warning(f"项目已改为处理本地文件，跳过网络下载: {url}")
            # 创建一个空文件作为占位符
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(f"Placeholder for: {url}")
            return file_path
        
    except Exception as e:
        logging.error(f"处理本地附录文件失败: {str(e)}")
        return None

def convert_excel_to_csv(file_path, output_dir):
    """将Excel文件转换为CSV格式（支持.xlsx, .xls, .xlsm格式）
    自动跳过表格前的介绍行，定位真实数据开始行
    """
    try:
        # 为.xlsm文件指定使用openpyxl引擎以支持宏文件
        file_ext = os.path.splitext(file_path)[1].lower()
        if file_ext in ['.xlsx', '.xlsm']:
            engine = 'openpyxl'
        elif file_ext == '.xls':
            engine = 'xlrd'
        else:
            logging.error(f"不支持的Excel文件格式: {file_ext}")
            return []
        
        # 读取Excel文件
        excel_file = pd.ExcelFile(file_path, engine=engine)
        
        # 为每个工作表创建CSV文件
        csv_files = []
        for sheet_name in excel_file.sheet_names:
            # 跳过介绍表（如metadata、info等）
            if any(keyword in sheet_name.lower() for keyword in ["metadata", "info", "介绍", "说明", "description"]):
                logging.info(f"跳过介绍表: {sheet_name}")
                continue
            
            # 读取工作表数据（不指定header，先读全部数据）
            df = pd.read_excel(file_path, sheet_name=sheet_name, engine=engine, header=None)
            
            # 检查是否为介绍表（通过数据特征判断）
            if df.empty or len(df) < 5:
                logging.info(f"跳过数据量过少的工作表: {sheet_name}")
                continue
            
            # 自动定位真实数据开始行
            # 通过查找数据列的关键字（如Date、Sr/Ca、Mg/Ca等）
            header_row_idx = None
            # 匹配前对文本做归一化：希腊字母δ/Δ统一替换为拉丁字母d，
            # 兼容 "δ18O"/"d18O"/"Δδ18O" 等写法
            def _norm_kw(text: str) -> str:
                return text.replace("δ", "d").replace("Δ", "d").replace("δ", "d")

            data_keywords = ["date", "year", "age", "sr/ca", "mg/ca", "d13c", "d18o", "sst", "depth"]
            
            # 从前三行查找，否则从最后5行向上查找
            found = False
            for idx in range(min(15, len(df))):
                row_str = _norm_kw(" ".join([str(cell).lower().strip() for cell in df.iloc[idx] if pd.notna(cell)]))
                if any(keyword in row_str for keyword in data_keywords):
                    header_row_idx = idx
                    found = True
                    break
            
            # 如果前三行没有找到，从最后5行向上查找
            if not found:
                for idx in range(len(df) - 1, max(0, len(df) - 20), -1):
                    row_str = _norm_kw(" ".join([str(cell).lower().strip() for cell in df.iloc[idx] if pd.notna(cell)]))
                    if any(keyword in row_str for keyword in data_keywords):
                        header_row_idx = idx
                        break
            
            # 如果找不到header行，跳过此表
            if header_row_idx is None:
                logging.warning(f"不能指定数据表的列名行，跳过: {sheet_name}")
                continue
            
            # 提取header行之后的数据
            df = df.iloc[header_row_idx:].reset_index(drop=True)
            # 使用第一行作为header
            df.columns = df.iloc[0].values
            df = df.iloc[1:].reset_index(drop=True)
            
            # 检查是否有有效数据行
            if df.empty or len(df) < 1:
                logging.info(f"整理后数据为空，跳过: {sheet_name}")
                continue
            
            # 检查列名是否包含数据特征
            data_columns = [col for col in df.columns if any(keyword in _norm_kw(str(col).lower()) for keyword in ["year", "age", "sr/ca", "mg/ca", "date", "d18o", "d13c", "sst", "depth", "温度", "同位素"])]
            if not data_columns and len(df.columns) < 3:
                logging.info(f"整理后不是数据表，跳过: {sheet_name}")
                continue
            
            # 生成CSV文件名，包含原始文件名前缀以便后续正确匹配
            # 例如：Supplementary Dataset.xlsx 中的 monthly 转换为 Supplementary Dataset_monthly.csv
            file_prefix = os.path.splitext(os.path.basename(file_path))[0]
            # 清洗工作表名，移除非法文件名字符
            safe_sheet_name = re.sub(r'[\\/*?:"<>|]', '_', sheet_name)
            csv_filename = f"{file_prefix}_{safe_sheet_name}.csv"
            csv_path = os.path.join(output_dir, csv_filename)
            
            # 保存为CSV，使用utf-8-sig编码以避免Excel中的中文乱码
            df.to_csv(csv_path, index=False, encoding='utf-8-sig')
            csv_files.append(csv_path)
            logging.info(f"Excel工作表 {sheet_name} 已转换为CSV: {csv_path}")
        
        return csv_files
    except Exception as e:
        logging.error(f"Excel转CSV失败: {str(e)}")
        return []

def convert_word_to_csv(file_path, output_dir):
    """
    将Word文档中的表格转换为CSV文件（仅提取包含目标指标的表格）
    Args:
        file_path: Word文件路径
        output_dir: 输出CSV的目录
    Returns: 生成的CSV文件路径列表
    """
    try:
        # 定义匹配模式（与clean_integrated_extractor.py中一致）
        pattern = re.compile(
            r"""
            (?:\w+\d*[\s\-_]*)?
            Sr\s*/?\s*Ca
            (?:\s*\(?\w+/?\w+\)?|\s*µmol/mol)?
            """, 
            re.VERBOSE | re.IGNORECASE | re.DOTALL
        )
        
        doc = Document(file_path)
        csv_files = []
        table_idx = 0
        
        # 创建输出目录
        os.makedirs(output_dir, exist_ok=True)
        
        for table in doc.tables:
            # 检查表格是否包含目标指标
            table_text = ""
            for row in table.rows[:5]:  # 只检查前5行
                for cell in row.cells:
                    table_text += cell.text.strip() + " "
            
            # 如果不匹配目标指标，则跳过该表格
            if not pattern.search(table_text):
                table_idx += 1
                continue
            
            # 提取表格标题
            caption = get_table_caption_for_word_table(doc, table, table_idx)
            
            # 提取表格数据
            table_data = []
            for row in table.rows:
                row_data = [cell.text.strip() for cell in row.cells]
                table_data.append(row_data)
            
            if not table_data:
                table_idx += 1
                continue
            
            # 转换为DataFrame
            df = pd.DataFrame(table_data[1:], columns=table_data[0] if len(table_data) > 1 else None)
            
            # 处理时间列，将时间格式转换为数值
            df = convert_time_columns_to_numeric(df)
            
            # 【修复】生成CSV文件名，包含原始文件名前缀以便后续正确匹配
            file_prefix = os.path.splitext(os.path.basename(file_path))[0]
            filename = f"{file_prefix}_table_{table_idx}_{caption}.csv" if caption else f"{file_prefix}_table_{table_idx}.csv"
            # 清洗文件名
            filename = re.sub(r'[\\/*?:"<>|]', "_", filename)
            csv_path = os.path.join(output_dir, filename)
            
            # 保存为CSV
            df.to_csv(csv_path, index=False, encoding="utf-8-sig")
            csv_files.append(csv_path)
            
            table_idx += 1
        
        return csv_files
    except Exception as e:
        logging.error(f"Word转CSV失败: {str(e)}")
        return []


def get_table_caption_for_word_table(doc, table, table_idx, *, for_filename=True):
    """读取 Word 表格标题。提取数据时同时保留相邻表注。"""
    try:
        from docx.text.paragraph import Paragraph

        table_element = table._element
        prev_elem = table_element.getprevious()
        caption = ""
        while prev_elem is not None and (for_filename or not prev_elem.tag.endswith("}tbl")):
            if prev_elem.tag.endswith('p'):  # 段落元素
                caption = Paragraph(prev_elem, doc).text.strip()
                if caption:
                    break
            prev_elem = prev_elem.getprevious()
        if for_filename:
            return re.sub(r'[\\/*?:"<>|]', "_", caption)[:50] or f"table_{table_idx}"

        context = [caption] if caption else []
        following = table_element.getnext()
        while following is not None and following.tag.endswith("}p"):
            note = Paragraph(following, doc).text.strip()
            if note and not re.match(r"^(?:notes?|footnotes?|注|说明)\s*[.:：]", note, re.I):
                break
            if note:
                context.append(note)
            following = following.getnext()
        return "\n".join(context)
    except (AttributeError, TypeError):
        return f"table_{table_idx}" if for_filename else ""


def convert_time_columns_to_numeric(df):
    """将DataFrame中的时间列转换为数值格式"""
    try:
        # 复制DataFrame以避免修改原始数据
        df_copy = df.copy()
        
        # 查找可能的时间列（包含日期特征的列）
        for col in df_copy.columns:
            # 检查列名是否包含时间相关关键词
            if any(keyword in str(col).lower() for keyword in ['year', 'date', 'time', '年', '时间']):
                # 对该列应用时间转换
                df_copy[col] = df_copy[col].apply(lambda x: date_to_numeric(x) if pd.notna(x) and x != '' else x)
            # 特别检查列名是否为"Date"
            elif str(col).strip().lower() == 'date':
                df_copy[col] = df_copy[col].apply(lambda x: date_to_numeric(x) if pd.notna(x) and x != '' else x)
        
        return df_copy
    except Exception as e:
        logging.warning(f"时间列转换失败: {str(e)}")
        return df


def date_to_numeric(time_str):
    """
    时间转数值格式（整数+小数）：年 + 月小数 + 日小数，保留小数精度
    
    支持的格式：
    - 小数年份：1961.25, 2009.5 → 直接保留
    - 整数年份：2016 → 2016.0
    - 日期格式：2016/8/1, 2016-08-01 → 转换为小数年份
    - 日期时间字符串：2019-12-11 00:00:00 → 转换为小数年份
    - 英文日期：1-Aug-16, 1-August-2016 → 转换为小数年份
    
    示例：
    - 2016/8/1 → 2016.5833（2016 + 7/12 ≈ 2016.5833）
    - 2019-12-11 00:00:00 → 2019.9425
    - 1961.25 → 1961.25（保留原始精度）
    """
    if not time_str or str(time_str).lower() in ['nan', 'none', 'null', '', 'nat']:
        return None
    
    time_str = str(time_str).strip()
    
    # 处理已为小数格式的年份（如1961.25）
    if re.match(r"^-?\d+\.\d+$", time_str):
        return float(time_str)  # 保留原始精度，不做round
    
    # 处理整数年份
    if re.match(r"^-?\d+$", time_str):
        return float(time_str)
    
    # 处理日期时间字符串（如 2019-12-11 00:00:00）
    datetime_pattern = r"(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2}):(\d{2})"
    dt_match = re.match(datetime_pattern, time_str)
    if dt_match:
        year = int(dt_match.group(1))
        month = int(dt_match.group(2))
        day = int(dt_match.group(3))
        hour = int(dt_match.group(4))
        minute = int(dt_match.group(5))
        
        # 计算精确的小数年份
        # 月份小数 + 日小数 + 小时小数
        month_decimal = (month - 1) / 12
        day_decimal = (day - 1) / 365.25
        hour_decimal = hour / (365.25 * 24)
        minute_decimal = minute / (365.25 * 24 * 60)
        
        numeric_date = year + month_decimal + day_decimal + hour_decimal + minute_decimal
        return numeric_date  # 保留完整精度
    
    # 定义可能的日期格式（扩展版）
    date_formats = [
        # 带时间的格式
        "%Y-%m-%d %H:%M:%S",    # 2019-12-11 00:00:00
        "%Y/%m/%d %H:%M:%S",    # 2019/12/11 00:00:00
        "%d-%m-%Y %H:%M:%S",    # 11-12-2019 00:00:00
        
        # 标准日期格式
        "%Y-%m-%d",             # 2016-08-01
        "%Y/%m/%d",             # 2016/8/1
        "%d/%m/%Y",             # 01/08/2016
        "%m/%d/%Y",             # 08/01/2016
        
        # 英文日期格式
        "%d-%b-%y",             # 1-Aug-16
        "%d-%B-%y",             # 1-August-16
        "%d-%b-%Y",             # 1-Aug-2016
        "%d-%B-%Y",             # 1-August-2016
        "%b-%d-%Y",             # Aug-01-2016
        "%B-%d-%Y",             # August-01-2016
        
        # ISO 格式
        "%Y%m%d",               # 20160801
    ]
    
    # 尝试匹配日期格式并转换
    from datetime import datetime
    for fmt in date_formats:
        try:
            dt = datetime.strptime(time_str, fmt)
            year = dt.year
            month = dt.month
            day = dt.day
            hour = getattr(dt, 'hour', 0)
            minute = getattr(dt, 'minute', 0)
            
            # 计算精确的小数年份
            month_decimal = (month - 1) / 12
            day_decimal = (day - 1) / 365.25
            hour_decimal = hour / (365.25 * 24)
            minute_decimal = minute / (365.25 * 24 * 60)
            
            numeric_date = year + month_decimal + day_decimal + hour_decimal + minute_decimal
            return numeric_date  # 保留完整精度
        except:
            continue
    
    # 尝试使用pandas解析（作为最后的降级方案）
    try:
        import pandas as pd
        dt = pd.to_datetime(time_str)
        if pd.notna(dt):
            year = dt.year
            month = dt.month
            day = dt.day
            hour = dt.hour
            minute = dt.minute
            
            month_decimal = (month - 1) / 12
            day_decimal = (day - 1) / 365.25
            hour_decimal = hour / (365.25 * 24)
            minute_decimal = minute / (365.25 * 24 * 60)
            
            return year + month_decimal + day_decimal + hour_decimal + minute_decimal
    except:
        pass
    
    # 无法匹配格式时返回None（后续转为NaN）
    return None


def convert_date_column_to_ce(df, column_name):
    """
    将日期列统一转换为CE小数格式
    
    Args:
        df: DataFrame
        column_name: 日期列名
        
    Returns:
        DataFrame: 转换后的DataFrame
    """
    if column_name not in df.columns:
        return df
    
    df[column_name] = df[column_name].apply(date_to_numeric)
    return df


def parse_appendix_file(file_path, file_hash):
    """根据文件类型解析附录，只提取表格数据（强化Excel单位和多表处理）
    不保存中间文件，仅返回解析结果供后续处理
    """
    # 文件完整性检查
    if not os.path.exists(file_path):
        logging.error(f"附录文件不存在: {file_path}")
        return {"type": "error", "content": "文件不存在"}
    
    # 计算文件MD5用于校验
    file_md5 = calculate_file_md5(file_path)
    logging.info(f"解析附录文件: {file_path}, MD5: {file_md5}")
    
    try:
        if file_path.endswith(('.xlsx', '.xls', '.xlsm')):
            excel_data = []
            excel_file = pd.ExcelFile(file_path)
            for sheet_name in excel_file.sheet_names:
                # 处理可能的合并单元格（根据文件类型选择合适的引擎）
                # 根据文件扩展名选择合适的引擎
                file_ext = os.path.splitext(file_path)[1].lower()
                engine = "openpyxl" if file_ext in ['.xlsx', '.xlsm'] else "xlrd"
                df = pd.read_excel(
                    file_path, 
                    sheet_name=sheet_name,
                    engine=engine,
                    header=0,  # 显式指定表头行
                    skiprows=None,  # 自动检测空行
                    parse_dates=False  # 先不解析日期，避免格式错误
                )
                # 提取单位行（常见于表头下方一行）
                units = None
                if not df.empty and df.shape[0] > 1:
                    # 检查第二行是否为单位（含°、%、mol等单位特征）
                    potential_units = df.iloc[0].astype(str).tolist()
                    if any(re.search(r'[°%μmol/‰]', u) for u in potential_units):
                        units = potential_units
                        df = df.iloc[1:].reset_index(drop=True)  # 跳过单位行
                
                # 记录表格元数据（含单位）
                excel_data.append({
                    "sheet_name": sheet_name,
                    "headers": df.columns.tolist(),
                    "units": units,
                    "data": df.to_dict('records')[:500]  # 限制最大行数，避免内存溢出
                })
            
            return {"type": "excel", "content": excel_data}
        
        elif file_path.endswith(".docx"):
            # Word处理：结合文本与表格提取
            doc = Document(file_path)
            word_data = []
            table_idx = 0
            # 先提取所有段落文本（用于上下文辅助解析）
            full_text = "\n".join([para.text.strip() for para in doc.paragraphs if para.text.strip()])
            
            for table in doc.tables:
                # 提取表格数据
                data = []
                for row in table.rows:
                    row_data = [cell.text.strip() for cell in row.cells]
                    data.append(row_data)
                if not data:
                    continue
                
                # 提取表格前后的文本上下文（辅助判断表格含义）
                table_start_para = None
                for i, para in enumerate(doc.paragraphs):
                    if para._element in table._element.getprevious():
                        table_start_para = i
                        break
                context = ""
                if table_start_para is not None:
                    # 取表格前2段、后1段文本作为上下文
                    start = max(0, table_start_para - 2)
                    end = min(len(doc.paragraphs), table_start_para + 2)
                    context = "\n".join([doc.paragraphs[j].text.strip() for j in range(start, end)])
                
                # 记录表格元数据（含上下文）
                word_data.append({
                    "table_index": table_idx,
                    "headers": data[0] if len(data) > 0 else [],
                    "data": data[1:] if len(data) > 1 else [],
                    "context": context  # 新增上下文，辅助LLM理解表格含义
                })
                table_idx += 1
            return {"type": "word", "content": word_data}
        
        elif file_path.endswith(".pdf"):
            # PDF处理：不处理PDF内嵌表格，仅处理独立附件文件
            logging.info(f"PDF文件 {file_path} 跳过处理（仅处理独立附件文件）")
            return {"type": "pdf", "content": []}
        
        elif file_path.endswith(".csv"):
            # CSV处理：提取数据并尝试识别表头和单位
            df = pd.read_csv(file_path, header=None)
            # 尝试识别表头和单位行（假设表头在第0行，单位在第1行）
            header_row = 0
            unit_row = 1 if len(df) > 1 else None
            
            # 提取表头
            headers = df.iloc[header_row].fillna("").tolist() if len(df) > 0 else []
            # 提取单位（若存在）
            units = df.iloc[unit_row].fillna("").tolist() if unit_row is not None else [""]*len(headers)
            
            # 提取数据行（跳过表头和单位行）
            data_start_row = unit_row + 1 if unit_row is not None else header_row + 1
            data_rows = df.iloc[data_start_row:].reset_index(drop=True)
            
            # 构造带单位的结构化数据
            structured_data = {
                "headers": headers,
                "units": units,  # 新增单位信息
                "data": data_rows.to_dict(orient="records")
            }
            
            return {"type": "csv", "content": structured_data}
        
        else:
            # 其他类型文件默认返回空（不处理）
            logging.warning(f"不支持的文件类型: {file_path}")
            return {"type": "unknown", "content": None}
            
    except Exception as e:
        logging.error(f"附录解析错误（{file_path}）：{str(e)}")
        return {"type": "error", "content": str(e)}
