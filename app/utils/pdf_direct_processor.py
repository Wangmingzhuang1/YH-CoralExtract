#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
直接PDF处理工具（跳过MD转换，直接使用LLM分析PDF）
"""

import os
import json
import logging
from pathlib import Path
from urllib.parse import urlparse
import hashlib
import datetime
import re
import time

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# 动态导入必要的模块
try:
    import pdfplumber
except ImportError:
    pdfplumber = None
    logging.warning("pdfplumber未安装，部分功能可能受限")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    logging.warning("python-dotenv未安装，将无法加载.env文件")

# 导入附录处理函数
try:
    from app.utils.appendix_processor import download_appendix_file
except ImportError as e:
    logging.error(f"无法导入download_appendix_file函数: {str(e)}")
    download_appendix_file = None


def extract_text_from_pdf(pdf_path):
    """从PDF中提取文本内容，优先使用对应的MD文件"""
    # 首先检查是否存在对应的MD文件
    from pathlib import Path
    pdf_path_obj = Path(pdf_path)
    md_path = pdf_path_obj.parent / (pdf_path_obj.stem + ".md")
    
    # 如果MD文件存在，优先读取MD文件内容
    if md_path.exists():
        try:
            with open(md_path, 'r', encoding='utf-8') as f:
                md_content = f.read()
            if md_content and md_content.strip():
                logging.info(f"优先使用MD文件内容: {md_path.name} (来源: MD文件)")
                # 在文本内容前添加来源标识
                return f"[SOURCE:MD_FILE]{md_content}"
            else:
                logging.warning(f"MD文件读取失败或内容无效，回退到PDF直接提取: {md_path.name}")
        except Exception as e:
            logging.warning(f"读取MD文件时出错，回退到PDF直接提取: {str(e)}")
    
    # 如果没有MD文件或MD文件读取失败，则直接从PDF提取
    if not pdfplumber:
        logging.error("pdfplumber未安装，无法提取PDF文本")
        return ""
    
    try:
        text_content = []
        with pdfplumber.open(pdf_path) as pdf:
            for page_num, page in enumerate(pdf.pages):
                try:
                    # 提取文本
                    text = page.extract_text() or ""
                    text_content.append(f"=== 第{page_num + 1}页 ===\n{text}\n")
                except Exception as page_error:
                    logging.warning(f"提取PDF第{page_num + 1}页文本失败: {str(page_error)}")
                    # 继续处理下一页
                    continue
        logging.info(f"从PDF文件提取文本内容: {pdf_path} (来源: PDF文件)")
        # 在文本内容前添加来源标识
        return "[SOURCE:PDF_FILE]" + "\n".join(text_content)
    except Exception as e:
        logging.error(f"提取PDF文本失败: {str(e)}")
        return ""


def find_skeletal_isotope_scale(pdf_path, body):
    """定位明确同时说明骨骼碳、氧同位素标尺的分析方法原文。"""
    if not pdfplumber or Path(pdf_path).suffix.lower() != ".pdf" or not re.search(r"\b(?:VPDB|VSMOW)\b", body, re.I):
        return None
    methods = re.compile(r"\b(?:analys[ei]s|measurements?)\s+of\s+(?:the\s+)?skeletal\s+"
                         r"(?:δ\s*13\s*C\s+and\s+δ\s*18\s*O|δ\s*18\s*O\s+and\s+δ\s*13\s*C)"
                         r".{0,620}?\b(?:the\s+)?isotope data (?:were|are) "
                         r"(?:normali[sz]ed|reported) (?:to|on|against) .{0,70}?\b(VPDB|VSMOW)\b", re.I)
    try:
        with pdfplumber.open(pdf_path) as document:
            matches = {}
            for page in document.pages[:12]:
                for start, end in ((0, page.width / 2), (page.width / 2, page.width), (0, page.width)):
                    text = page.crop((start, 0, end, page.height)).extract_text() or ""
                    match = methods.search(" ".join(text.split()))
                    if match:
                        matches.setdefault(match.group(1).upper(), (page.page_number, match.group()))
            if len(matches) == 1:
                scale, (page_number, quote) = next(iter(matches.items()))
                return scale, page_number, quote
    except (OSError, ValueError) as exc:
        logging.warning("核验同位素标尺原文失败 %s: %s", pdf_path, exc)
    return None

def extract_doi_from_pdf(pdf_text):
    """从PDF文本中提取DOI号"""
    # DOI通常在文章首页或参考文献部分
    # 提取前几页的文本进行分析
    first_pages_text = pdf_text[:5000]  # 前5000字符通常包含DOI信息
    
    # DOI的正则表达式模式
    doi_patterns = [
        r'\b(10\.\d{4,9}/[-._;()/:A-Z0-9]+)\b',
        r'DOI:\s*(10\.\d{4,9}/[-._;()/:A-Z0-9]+)',
        r'doi:\s*(10\.\d{4,9}/[-._;()/:A-Z0-9]+)',
        r'https?://doi\.org/(10\.\d{4,9}/[-._;()/:A-Z0-9]+)'
    ]
    
    for pattern in doi_patterns:
        matches = re.findall(pattern, first_pages_text, re.IGNORECASE)
        if matches:
            # 返回第一个匹配的DOI
            doi = matches[0] if isinstance(matches[0], str) else matches[0][0]
            # 清理DOI，确保格式正确
            doi = re.sub(r'https?://doi\.org/', '', doi)
            return doi
    
    return None

def is_pdf_relevant_and_has_appendix_cot(pdf_text):
    """使用具备思维链能力的大模型判断PDF是否与珊瑚古气候主题相关并提取附录链接"""
    try:
        # 导入LLM API
        from app.utils.llm_api import call_llm
        
        # 提取摘要部分（通常是文档的前几页）
        # 我们只使用前2000个字符来判断主题相关性，节约token
        abstract_text = pdf_text[:2000]
        
        # 附录通常在结论之后参考文献之前，我们提取文档的后部分进行分析
        # 同时也保留一些前面的内容作为上下文
        text_length = len(pdf_text)
        # 提取文档的后1/2部分，但不超过15000字符
        start_pos = max(0, text_length * 1 // 2)
        end_pos = min(text_length, start_pos + 15000)
        appendix_context = pdf_text[start_pos:end_pos]
        
        # 构造提示词，引导大模型进行逐步思考
        prompt = f"""
请逐步分析以下学术论文的内容，完成两个任务：

任务1：判断该论文是否与珊瑚古气候主题相关
判断标准：
1. 是否包含以下关键词：'珊瑚', '锶钙比', '海表温度', '碳同位素', '氧同位素'
2. 是否与珊瑚、古气候、古海洋学、ENSO等主题相关

任务2：找出其中所有附录相关的链接或文件引用
查找重点：
1. 只查找附录、补充材料、数据可用性等章节中的链接
2. 忽略参考文献部分的DOI链接
3. 忽略任何在References、参考文献、Bibliography等章节中的链接
4. 重点关注以下关键词附近的链接：
- Appendix
- Supplementary data
- Supplemental information
- Supporting information
- Data availability
- 数据可用性
- 附录
- 补充材料
- 附加文件

分析步骤：
1. 首先分析论文摘要部分，判断是否与珊瑚古气候主题相关
2. 然后分析论文后半部分，定位包含附录关键词的段落
3. 在有效段落中识别URL、DOI或文件路径
4. 验证链接是否与数据附件相关

请以JSON格式返回分析结果，格式如下：
{{
    "relevant": true/false,
    "reason": "判断理由",
    "has_target_data": true/false,
    "target_data_types": ["锶钙比", "海表温度", "碳同位素", "氧同位素"]中的匹配项,
    "links": [
        {{
            "url": "链接地址",
            "context": "上下文内容（关键词附近的一小段文本）",
            "page": "页码（如果能确定的话）"
        }}
    ]
}}

论文摘要部分：
{abstract_text}

论文后半部分（用于查找附录链接）：
{appendix_context}
"""
        
        # 调用具备思维链能力的大模型
        result = call_llm("max", prompt)
        
        # 解析结果
        if result:
            # 清理可能的Markdown代码块标记
            result = result.strip()
            if result.startswith("```json"):
                result = result[7:]
            if result.endswith("```"):
                result = result[:-3]
            
            parsed_result = json.loads(result)
            return parsed_result
        return {"relevant": False, "reason": "无法获取分析结果", "links": []}
    except Exception as e:
        logging.error(f"调用思维链大模型判断PDF相关性和提取附录链接失败: {str(e)}")
        return {"relevant": False, "reason": f"分析过程出错: {str(e)}", "links": []}

def extract_appendix_links_directly(pdf_text):
    """直接从PDF文本中提取附录链接（使用正则表达式）"""
    links = []
    
    # 定义附录相关关键词
    appendix_keywords = [
        r'Appendix', r'Supplementary\s+data', r'Supplemental\s+information', 
        r'Supporting\s+information', r'Data\s+availability', r'数据可用性', 
        r'附录', r'补充材料', r'附加文件'
    ]
    
    # 定义参考文献相关关键词（用于排除）
    reference_keywords = [
        r'References', r'参考文献', r'Bibliography', r'Works\s+Cited'
    ]
    
    # 定义DOI模式
    doi_pattern = r'10\.\d{4,9}/[-._;()/:A-Z0-9]+'
    
    # 定义URL模式
    url_pattern = r'https?://[^\s\)]+'
    
    # 在整个文本中查找
    for keyword in appendix_keywords:
        # 查找关键词附近的内容
        pattern = rf'({keyword}.*?)(https?://[^\s\)]+|10\.\d{4,9}/[-._;()/:A-Z0-9]+)'
        matches = re.finditer(pattern, pdf_text, re.IGNORECASE | re.DOTALL)
        
        for match in matches:
            context = match.group(1).strip()
            url = match.group(2).strip()
            
            # 清理URL（移除可能的标点符号）
            url = re.sub(r'[.,;:]+$', '', url)
            
            # 检查上下文是否包含参考文献关键词，如果包含则跳过
            is_reference_context = False
            for ref_keyword in reference_keywords:
                if re.search(ref_keyword, context, re.IGNORECASE):
                    is_reference_context = True
                    break
            
            # 额外检查：如果URL是DOI且上下文包含"doi.org"或类似格式，则很可能是参考文献
            if url.startswith("10.") and re.search(r'doi\.org|DOI|doi', context, re.IGNORECASE):
                is_reference_context = True
            
            if not is_reference_context:
                links.append({
                    "url": url,
                    "context": context,
                    "page": "unknown"
                })
    
    # 特别查找DOI模式，但只在非参考文献上下文中
    doi_matches = re.finditer(doi_pattern, pdf_text, re.IGNORECASE)
    for match in doi_matches:
        doi = match.group(0)
        # 获取DOI周围的上下文
        start = max(0, match.start() - 100)
        end = min(len(pdf_text), match.end() + 100)
        context = pdf_text[start:end]
        
        # 检查上下文是否包含参考文献关键词，如果包含则跳过
        is_reference_context = False
        for ref_keyword in reference_keywords:
            if re.search(ref_keyword, context, re.IGNORECASE):
                is_reference_context = True
                break
        
        # 额外检查：如果上下文包含"doi.org"或类似格式，则很可能是参考文献
        if re.search(r'doi\.org|DOI|doi', context, re.IGNORECASE):
            is_reference_context = True
        
        if not is_reference_context:
            links.append({
                "url": doi,
                "context": context,
                "page": "unknown"
            })
    
    # 去重
    unique_links = []
    seen_urls = set()
    for link in links:
        url = link["url"]
        if url not in seen_urls:
            seen_urls.add(url)
            unique_links.append(link)
    
    return unique_links

def is_valid_attachment_link(url):
    """检查是否为有效的附件链接"""
    # 定义附件链接白名单后缀
    ATTACHMENT_DOMAIN_WHITELIST = [
        '.pdf', '.xlsx', '.xls', '.docx', '.doc', '.csv', '.txt'
    ]
    
    # 定义需要排除的域名（这些通常是公共数据仓库，需要特殊处理）
    EXCLUDED_DOMAINS = [
        'ncbi.nlm.nih.gov', 'dryad.org', 'figshare.com', 'zenodo.org'
    ]
    
    parsed_url = urlparse(url)
    
    # 检查是否在排除域名列表中（这些需要特殊处理）
    for domain in EXCLUDED_DOMAINS:
        if domain in parsed_url.netloc:
            # 对于公共数据仓库，我们仍然认为是有效的，但需要特殊处理
            logging.info(f"检测到公共数据仓库链接: {url}")
            return True
    
    # 对于DOI链接，我们总是认为是有效的
    if url.startswith("10."):
        logging.info(f"检测到DOI链接: {url}")
        return True
    
    # 检查是否在白名单后缀中
    for suffix in ATTACHMENT_DOMAIN_WHITELIST:
        if url.lower().endswith(suffix):
            return True
    
    # 如果没有明确的文件后缀，但我们有HTTP/HTTPS协议，也认为可能是有效的
    # 因为有些链接可能需要跳转到下载页面
    if parsed_url.scheme in ['http', 'https']:
        logging.info(f"检测到可能需要跳转的链接: {url}")
        return False

def is_need_manual_intervention(url):
    """判断链接是否需要手动干预"""
    parsed_url = urlparse(url)
    # 1. DOI链接（通常需要跳转）
    if url.startswith("10.") or parsed_url.netloc.endswith("doi.org"):
        return True
    # 2. 已知难以自动爬取的官方数据集域名
    official_domains = ["noaa.gov", "nasa.gov", "usgs.gov", "sciencedb.cn"]
    if any(domain in parsed_url.netloc for domain in official_domains):
        return True
    # 3. 纯HTML页面（无直接下载链接）
    if parsed_url.path.endswith(".html") or parsed_url.path.endswith(".htm"):
        return True
    return False

def is_official_website(url):
    """判断是否为官方数据网站，需要跳过打开"""
    parsed_url = urlparse(url)
    # 定义官方数据网站域名
    official_websites = [
        "ncei.noaa.gov", "noaa.gov", "nasa.gov", "usgs.gov", 
        "sciencedb.cn", "dryad.org", "figshare.com", "zenodo.org"
    ]
    return any(website in parsed_url.netloc for website in official_websites)

def process_pdf_directly(pdf_path, output_dir, use_cot=False):
    """直接处理PDF文件，提取附录链接并下载
    
    Args:
        pdf_path (str): PDF文件路径
        output_dir (str): 输出目录
        use_cot (bool): 是否使用思维链大模型，默认为False
    """
    try:
        # 1. 提取PDF文本
        logging.info(f"开始提取PDF文本: {pdf_path}")
        pdf_text = extract_text_from_pdf(pdf_path)
        if not pdf_text:
            logging.error(f"无法提取PDF文本: {pdf_path}")
            return []
        
        # 2. 判断PDF是否与主题相关并提取附录链接（合并为一个步骤）
        logging.info("判断PDF是否与珊瑚古气候主题相关并提取附录链接...")
        relevance_and_appendix_result = is_pdf_relevant_and_has_appendix_cot(pdf_text)
        logging.info(f"相关性和附录链接判断结果: {relevance_and_appendix_result}")
        
        # 如果PDF不相关，直接返回
        if not relevance_and_appendix_result.get("relevant", False):
            logging.info(f"PDF {pdf_path} 与主题不相关，跳过处理")
            return []
        
        # 3. 检查是否有目标数据类型
        target_data_types = relevance_and_appendix_result.get("target_data_types", [])
        if not target_data_types:
            logging.info(f"PDF {pdf_path} 未找到目标数据类型，跳过处理")
            return []
        
        # 4. 获取附录链接
        appendix_links = relevance_and_appendix_result.get("links", [])
        logging.info(f"找到 {len(appendix_links)} 个附录链接")
        
        # 5. 验证链接有效性并分类
        valid_links = []
        reference_links = []
        for link_info in appendix_links:
            url = link_info.get("url", "")
            context = link_info.get("context", "").lower()
            
            # 检查是否为参考文献链接
            if any(keyword in context for keyword in ['reference', '参考文献', 'bibliography', 'works cited']):
                reference_links.append(link_info)
                logging.info(f"跳过参考文献链接: {url}")
                continue
            
            # 检查是否为官方数据网站链接，如果是则跳过
            if is_official_website(url):
                logging.info(f"检测到官方数据网站链接，跳过处理: {url}")
                continue
            
            if is_valid_attachment_link(url):
                valid_links.append(link_info)
            else:
                logging.info(f"跳过无效链接: {url}")
        
        logging.info(f"找到 {len(appendix_links)} 个链接，其中 {len(reference_links)} 个为参考文献链接，{len(valid_links)} 个为有效附录链接")
        
        # 6. 创建唯一的输出目录（避免覆盖）
        pdf_name = os.path.splitext(os.path.basename(pdf_path))[0]
        # 使用时间戳确保唯一性
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        unique_output_dir = os.path.join(output_dir, f"{pdf_name}_{timestamp}_{hashlib.md5(pdf_path.encode()).hexdigest()[:8]}")
        os.makedirs(unique_output_dir, exist_ok=True)
        
        # 7. 下载附录文件
        downloaded_files = []
        if download_appendix_file is None:
            logging.error("download_appendix_file函数未正确导入，无法下载附录文件")
        else:
            for link_info in valid_links:
                url = link_info["url"]
                file_path = download_appendix_file(url, unique_output_dir)
                if file_path:
                    link_info["downloaded_path"] = file_path
                    downloaded_files.append(link_info)
        
        # 8. 保存附录信息到JSON文件
        appendix_info_file = os.path.join(unique_output_dir, "appendix_info.json")
        with open(appendix_info_file, "w", encoding="utf-8") as f:
            json.dump(downloaded_files, f, ensure_ascii=False, indent=2)
        
        # 9. 保存相关性分析结果
        relevance_info_file = os.path.join(unique_output_dir, "relevance_info.json")
        with open(relevance_info_file, "w", encoding="utf-8") as f:
            json.dump(relevance_and_appendix_result, f, ensure_ascii=False, indent=2)
        
        logging.info(f"附录处理完成，信息保存到: {unique_output_dir}")
        return downloaded_files
        
    except Exception as e:
        logging.error(f"处理PDF时出错: {str(e)}")
        return []

if __name__ == "__main__":
    # 测试代码
    import sys
    if len(sys.argv) > 2:
        pdf_path = sys.argv[1]
        output_dir = sys.argv[2]
        # 检查是否使用思维链大模型
        use_cot = len(sys.argv) > 3 and sys.argv[3].lower() == 'true'
        result = process_pdf_directly(pdf_path, output_dir, use_cot)
        print(f"处理完成，找到 {len(result)} 个附录文件")
    else:
        print("用法: python pdf_direct_processor.py <pdf_path> <output_dir> [use_cot]")
        print("  use_cot: 是否使用思维链大模型(true/false)，默认为false")
