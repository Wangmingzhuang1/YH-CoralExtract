#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
PDF元数据提取模块 (Regex Only)
主要提供基于正则表达式的元数据提取功能，作为 Context Extractor (LLM) 的补充。
保留 DOI 和 作者提取功能供 workflow 使用。
"""

import re
import logging
from typing import Dict, Any

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def _extract_doi_and_authors_with_regex(text: str) -> Dict[str, Any]:
    """
    使用正则表达式提取DOI和作者信息（不调用大模型）
    
    【优化】改进提取逻辑：
    1. DOI通常以10.开头，优先匹配
    2. 从PDF前面提取作者和发表年份，然后组合
    3. 更准确地识别作者和年份的位置关系
    
    Args:
        text: PDF/MD文本内容
        
    Returns:
        Dict[str, Any]: 包含DOI和作者的字典
    """
    metadata = {}
    
    # 清理 MinerU 源标记
    clean_text = re.sub(r'^\[SOURCE:[A-Z_]+\]', '', text, count=1)
    
    # 1. 提取DOI号（通常以10.开头）
    # 【增强】扩大搜索范围，DOI可能在摘要、参考文献、脚注等位置
    # 优先搜索前20000字符（包含标题、摘要、脚注等），如果没找到再搜索全文
    search_texts = [
        clean_text[:20000],  # 优先搜索前20000字符
        clean_text  # 如果没找到，搜索全文
    ]
    
    doi_patterns = [
        r'\b(10\.\d{4,9}/[-._;()/:A-Z0-9]+)\b',  # 标准DOI格式：10.xxxx/xxxx
        r'DOI[:\s]+(10\.\d{4,9}/[-._;()/:A-Z0-9]+)',  # DOI: 10.xxxx/xxxx
        r'doi[:\s]+(10\.\d{4,9}/[-._;()/:A-Z0-9]+)',  # doi: 10.xxxx/xxxx
        r'https?://doi\.org/(10\.\d{4,9}/[-._;()/:A-Z0-9]+)',  # https://doi.org/10.xxxx/xxxx
        r'https?://dx\.doi\.org/(10\.\d{4,9}/[-._;()/:A-Z0-9]+)',  # https://dx.doi.org/10.xxxx/xxxx
    ]
    
    doi_found = False
    for search_text in search_texts:
        if doi_found:
            break
        for pattern in doi_patterns:
            matches = re.findall(pattern, search_text, re.IGNORECASE)
            if matches:
                doi = matches[0] if isinstance(matches[0], str) else matches[0][0]
                # 清理DOI，移除URL前缀和末尾标点
                doi = re.sub(r'https?://(doi|dx\.doi)\.org/', '', doi, flags=re.IGNORECASE)
                doi = doi.rstrip('.,;:)]}')  # 移除末尾标点
                # 确保DOI以10.开头
                if doi.startswith('10.'):
                    metadata["pub1_doi"] = doi.strip()
                    logger.debug(f"正则提取到DOI: {metadata['pub1_doi']}")
                    doi_found = True
                    break
    
    # 2. 提取作者和年份信息（从PDF前面，通常在标题附近）
    # 【增强】搜索PDF前8000字符，确保提取的是论文的第一作者，而不是引用信息中的作者
    # 参考文献通常在PDF后面，前8000字符通常只包含标题、作者、摘要等，不包含引用信息
    author_section = clean_text[:8000]
    
    # 2.1 先提取发表年份（通常在标题或作者附近）
    # 【增强】优先从"Received/Accepted"句子中提取年份，然后查找括号中的年份，最后是独立的年份
    extracted_year = None
    year_candidates = []
    
    # 优先模式：从"Received/Accepted"句子中提取年份（期刊通常都有）
    # 例如："Received 10 September 2024; Received in revised form 15 January 2025; Accepted 16 January 2025"
    received_accepted_patterns = [
        r'Accepted\s+\d+\s+\w+\s+(\d{4})',  # Accepted 16 January 2025
        r'Received\s+in\s+revised\s+form\s+\d+\s+\w+\s+(\d{4})',  # Received in revised form 15 January 2025
        r'Received\s+\d+\s+\w+\s+(\d{4})',  # Received 10 September 2024
        r'Published\s+(\d{4})',  # Published 2025
        r'Publication\s+date[:\s]+(\d{4})',  # Publication date: 2025
    ]
    
    for pattern in received_accepted_patterns:
        year_matches = re.findall(pattern, author_section, re.IGNORECASE)
        if year_matches:
            years = [int(y) for y in year_matches if 1900 <= int(y) <= 2025]
            if years:
                # 从Received/Accepted中提取的年份，取最大的（通常是Accepted年份，即发表年份）
                extracted_year = str(max(years))
                logger.debug(f"正则从Received/Accepted提取到年份: {extracted_year}")
                break
    
    # 如果从Received/Accepted中没有找到年份，尝试其他模式
    if not extracted_year:
        year_patterns = [
            r'\((\d{4})\)',  # (2021) - 括号中的年份通常是发表年份
            r'\b(19\d{2}|20\d{2})\b',  # 1900-2099 - 独立的年份
        ]
        
        for pattern in year_patterns:
            year_matches = re.findall(pattern, author_section)
            if year_matches:
                # 过滤合理的年份范围
                years = [int(y) for y in year_matches if 1900 <= int(y) <= 2025]
                if years:
                    year_candidates.extend(years)
        
        if year_candidates:
            # 取最大的年份（通常是发表年份）
            extracted_year = str(max(year_candidates))
            logger.debug(f"正则提取到年份: {extracted_year}")
    
    # 2.2 提取作者信息（通常在PDF前面，标题下方）
    # 【优化】改进作者提取模式，更准确地识别作者名和年份的组合
    # 优先匹配包含年份的模式，然后匹配不包含年份的模式
    
    # 模式1：作者名 et al. (年份) - 最常见的格式（优先匹配）
    author_patterns = [
        # 英文作者：First Author et al. (2021) - 括号中的年份
        r'^([A-Z][a-z]+(?:\s+[A-Z]\.?\s*[a-z]+)*)\s+et\s+al\.?\s*[,\s]*\((\d{4})\)',
        # 英文作者：First Author et al. 2021 - 无括号的年份
        r'^([A-Z][a-z]+(?:\s+[A-Z]\.?\s*[a-z]+)*)\s+et\s+al\.?\s+(\d{4})\b',
        # 英文作者：First Author, Second Author et al. (2021)
        r'^([A-Z][a-z]+(?:\s+[A-Z]\.?)*,\s+[A-Z][a-z]+(?:\s+[A-Z]\.?)*)\s+et\s+al\.?\s*[,\s]*\((\d{4})\)',
        # 英文作者：First Author (2021) - 没有et al.
        r'^([A-Z][a-z]+(?:\s+[A-Z]\.?\s*[a-z]+)+)\s*\((\d{4})\)',
        # 英文作者：First Author 2021 - 没有et al.和括号
        r'^([A-Z][a-z]+(?:\s+[A-Z]\.?\s*[a-z]+)+)\s+(\d{4})\b',
        # 中文作者：作者名 et al. (年份)
        r'^([\u4e00-\u9fa5]+(?:\s+[\u4e00-\u9fa5]+)*)\s+et\s+al\.?\s*[,\s]*\((\d{4})\)',
        # 中文作者：作者名 (年份)
        r'^([\u4e00-\u9fa5]+(?:\s+[\u4e00-\u9fa5]+)+)\s*\((\d{4})\)',
        # 中文作者：作者名 年份
        r'^([\u4e00-\u9fa5]+(?:\s+[\u4e00-\u9fa5]+)+)\s+(\d{4})\b',
    ]
    
    # 模式2：不包含年份的作者名（备用，如果模式1都失败）
    author_patterns_no_year = [
        # 英文作者：First Author et al.
        r'^([A-Z][a-z]+(?:\s+[A-Z]\.?\s*[a-z]+)*)\s+et\s+al\.',
        # 英文作者：First Author, Second Author et al.
        r'^([A-Z][a-z]+(?:\s+[A-Z]\.?)*,\s+[A-Z][a-z]+(?:\s+[A-Z]\.?)*)\s+et\s+al\.',
        # 英文作者：First Author Name（完整姓名，限制长度避免匹配段落）
        r'^([A-Z][a-z]{1,15}(?:\s+[A-Z]\.?\s*[a-z]{1,15}){1,3})',
        # 中文作者：作者名 et al.
        r'^([\u4e00-\u9fa5]{2,6}(?:\s+[\u4e00-\u9fa5]+)*)\s+et\s+al\.',
        # 中文作者：作者名
        r'^([\u4e00-\u9fa5]{2,6}(?:\s+[\u4e00-\u9fa5]+)+)',
    ]
    
    def _is_valid_author_name(name: str) -> bool:
        """验证是否是有效的作者名"""
        if not name or len(name) < 3:
            return False
        # 作者名不能太长（通常不超过40字符）
        if len(name) > 40:
            return False
        # 【关键】排除引用句子模式，如 "was shown by Smith", "according to Jones"
        citation_prefixes = [
            'was shown by', 'as shown by', 'shown by', 'according to',
            'reported by', 'described by', 'proposed by', 'suggested by',
            'demonstrated by', 'found by', 'observed by', 'noted by',
            'see', 'cf.', 'e.g.', 'i.e.'
        ]
        name_lower = name.lower()
        for prefix in citation_prefixes:
            if name_lower.startswith(prefix):
                return False
        # 作者名不能包含太多连续小写字母（这通常是句子或段落）
        # 例如 "Seasurfacetemperature" 有20+个连续小写字母
        lowercase_runs = re.findall(r'[a-z]{10,}', name)
        if lowercase_runs:
            return False
        # 作者名应该包含空格（姓和名分开）或是中文名
        has_space = ' ' in name
        is_chinese = re.search(r'[\u4e00-\u9fa5]', name) is not None
        if not has_space and not is_chinese and len(name) > 15:
            return False
        return True
    
    extracted_authors = None
    extracted_year_from_author = None
    
    # 首先尝试匹配包含年份的模式
    for pattern in author_patterns:
        matches = re.findall(pattern, author_section, re.MULTILINE | re.IGNORECASE)
        if matches:
            match = matches[0]
            if isinstance(match, tuple) and len(match) >= 2:
                author_name = match[0].strip()
                year_from_match = match[1].strip() if match[1] else None
                
                # 验证年份是否合理
                if year_from_match and 1900 <= int(year_from_match) <= 2025:
                    extracted_year_from_author = year_from_match
                
                # 【关键修复】提前检查是否是期刊名，避免后续处理
                # 期刊名通常包含多个期刊相关词汇，如"Global and Planetary Change"包含"global"和"change"
                journal_indicators = ['change', 'review', 'science', 'journal', 'letter', 
                                     'proceeding', 'geophysical', 'planetary', 'global', 'earth',
                                     'marine', 'ocean', 'climate', 'paleo', 'quaternary', 
                                     'geology', 'geoscience', 'nature', 'and']
                journal_word_count = sum(1 for word in journal_indicators if word in author_name.lower())
                # 如果包含2个或更多期刊相关词汇，很可能是期刊名（如"Global and Planetary Change"）
                if journal_word_count >= 2:
                    logger.debug(f"跳过可能的期刊名（含年份模式）: {author_name}")
                    continue
                
                if author_name and _is_valid_author_name(author_name):
                    # 【关键修复】过滤掉明显不是作者名的内容，包括引用信息中的作者和期刊名
                    exclude_keywords = [
                        'university', 'institute', 'department', 'college', 'school', 
                        'abstract', 'introduction', 'method', 'result', 'received', 'accepted',
                        'journal', 'volume', 'pages', 'doi', 'published', 'reference',
                        'cited', 'citation', 'bibliography', 'acknowledgment',
                        # 【新增】期刊名相关关键词
                        'change', 'review', 'science', 'nature', 'letter', 'proceeding',
                        'geophysical', 'planetary', 'global', 'earth', 'marine', 'ocean',
                        'climate', 'paleo', 'quaternary', 'geology', 'geoscience'
                    ]
                    
                    if not any(keyword in author_name.lower() for keyword in exclude_keywords):
                        # 【关键修复】如果作者名包含多个作者（逗号分隔），只提取第一作者
                        if ',' in author_name:
                            # 提取第一个作者（逗号前的部分）
                            first_author = author_name.split(',')[0].strip()
                            if first_author and _is_valid_author_name(first_author):
                                extracted_authors = first_author
                                logger.debug(f"从PDF提取到第一作者（含年份）: {first_author}, 年份: {extracted_year_from_author}")
                                break
                        else:
                            extracted_authors = author_name
                            logger.debug(f"从PDF提取到作者名（含年份）: {author_name}, 年份: {extracted_year_from_author}")
                            break
            elif isinstance(match, tuple) and len(match) >= 1:
                author_name = match[0].strip()
                if author_name and _is_valid_author_name(author_name):
                    exclude_keywords = [
                        'university', 'institute', 'department', 'college', 'school', 
                        'abstract', 'introduction', 'method', 'result', 'received', 'accepted',
                        'journal', 'volume', 'pages', 'doi', 'published', 'reference',
                        'cited', 'citation', 'bibliography', 'acknowledgment',
                        # 【新增】期刊名相关关键词
                        'change', 'review', 'science', 'nature', 'letter', 'proceeding',
                        'geophysical', 'planetary', 'global', 'earth', 'marine', 'ocean',
                        'climate', 'paleo', 'quaternary', 'geology', 'geoscience'
                    ]
                    
                    # 【关键修复】检查是否是期刊名（期刊名通常包含多个期刊相关词汇）
                    journal_indicators = ['change', 'review', 'science', 'journal', 'letter', 
                                         'proceeding', 'geophysical', 'planetary', 'global', 'earth',
                                         'marine', 'ocean', 'climate', 'paleo', 'quaternary', 
                                         'geology', 'geoscience', 'nature', 'and']
                    journal_word_count = sum(1 for word in journal_indicators if word in author_name.lower())
                    if journal_word_count >= 2:
                        logger.debug(f"跳过可能的期刊名（单元素模式）: {author_name}")
                        continue
                    
                    if not any(keyword in author_name.lower() for keyword in exclude_keywords):
                        # 【关键修复】如果作者名包含多个作者（逗号分隔），只提取第一作者
                        if ',' in author_name:
                            first_author = author_name.split(',')[0].strip()
                            if first_author and _is_valid_author_name(first_author):
                                extracted_authors = first_author
                                logger.debug(f"从PDF提取到第一作者: {first_author}")
                                break
                        else:
                            extracted_authors = author_name
                            logger.debug(f"从PDF提取到作者名: {author_name}")
                            break
    
    # 2.3 如果还没有找到作者，尝试不包含年份的模式
    if not extracted_authors:
        for pattern in author_patterns_no_year:
            matches = re.findall(pattern, author_section, re.MULTILINE | re.IGNORECASE)
            if matches:
                candidate = matches[0].strip()
                # 【关键修复】过滤掉明显不是作者名的内容，包括引用信息和期刊名
                exclude_keywords = [
                    'university', 'institute', 'department', 'college', 'school', 
                    'abstract', 'introduction', 'method', 'result', 'received', 'accepted',
                    'journal', 'volume', 'pages', 'doi', 'published', 'reference',
                    'cited', 'citation', 'bibliography', 'acknowledgment',
                    # 【新增】期刊名相关关键词
                    'change', 'review', 'science', 'nature', 'letter', 'proceeding',
                    'geophysical', 'planetary', 'global', 'earth', 'marine', 'ocean',
                    'climate', 'paleo', 'quaternary', 'geology', 'geoscience'
                ]
                
                # 【关键修复】检查是否是期刊名（期刊名通常包含多个期刊相关词汇）
                journal_indicators = ['change', 'review', 'science', 'journal', 'letter', 
                                     'proceeding', 'geophysical', 'planetary', 'global', 'earth',
                                     'marine', 'ocean', 'climate', 'paleo', 'quaternary', 
                                     'geology', 'geoscience', 'nature', 'and']
                journal_word_count = sum(1 for word in journal_indicators if word in candidate.lower())
                if journal_word_count >= 2:
                    logger.debug(f"跳过可能的期刊名（不含年份模式）: {candidate}")
                    continue
                
                if (_is_valid_author_name(candidate) and 
                    not any(keyword in candidate.lower() for keyword in exclude_keywords)):
                    # 【关键修复】如果作者名包含多个作者（逗号分隔），只提取第一作者
                    if ',' in candidate:
                        first_author = candidate.split(',')[0].strip()
                        if first_author and _is_valid_author_name(first_author):
                            extracted_authors = first_author
                            logger.debug(f"从PDF提取到第一作者（不含年份）: {first_author}")
                            break
                    else:
                        extracted_authors = candidate
                        logger.debug(f"从PDF提取到作者名（不含年份）: {candidate}")
                        break
    
    # 2.4 如果还没有找到作者，尝试更通用的模式（最后备用）
    if not extracted_authors:
        # 查找第一个看起来像作者名的模式（大写字母开头的单词序列，通常在PDF开头）
        generic_patterns = [
            r'^([A-Z][a-z]+(?:\s+[A-Z]\.?\s*[a-z]+)+)',  # First Author Name
            r'^([A-Z][a-z]+(?:\s+[A-Z]\.?)+)',  # First A. Last
        ]
        
        for pattern in generic_patterns:
            matches = re.findall(pattern, author_section, re.MULTILINE)
            if matches:
                candidate = matches[0].strip()
                # 【关键修复】过滤掉明显不是作者名的内容，包括引用信息和期刊名
                exclude_keywords = [
                    'university', 'institute', 'department', 'college', 'school', 
                    'abstract', 'introduction', 'method', 'result', 'received', 'accepted',
                    'journal', 'volume', 'pages', 'doi', 'published', 'reference',
                    'cited', 'citation', 'bibliography', 'acknowledgment',
                    # 【新增】期刊名相关关键词
                    'change', 'review', 'science', 'nature', 'letter', 'proceeding',
                    'geophysical', 'planetary', 'global', 'earth', 'marine', 'ocean',
                    'climate', 'paleo', 'quaternary', 'geology', 'geoscience'
                ]
                
                # 【关键修复】检查是否是期刊名（期刊名通常包含多个期刊相关词汇）
                journal_indicators = ['change', 'review', 'science', 'journal', 'letter', 
                                     'proceeding', 'geophysical', 'planetary', 'global', 'earth',
                                     'marine', 'ocean', 'climate', 'paleo', 'quaternary', 
                                     'geology', 'geoscience', 'nature', 'and']
                journal_word_count = sum(1 for word in journal_indicators if word in candidate.lower())
                if journal_word_count >= 2:
                    logger.debug(f"跳过可能的期刊名（不含年份模式）: {candidate}")
                    continue
                
                if (len(candidate) > 5 and len(candidate) < 100 and 
                    not any(keyword in candidate.lower() for keyword in exclude_keywords)):
                    # 【关键修复】如果作者名包含多个作者（逗号分隔），只提取第一作者
                    if ',' in candidate:
                        first_author = candidate.split(',')[0].strip()
                        if first_author and len(first_author) > 5:
                            extracted_authors = first_author
                            logger.debug(f"从PDF提取到第一作者（通用模式）: {first_author}")
                            break
                    else:
                        extracted_authors = candidate
                        logger.debug(f"从PDF提取到作者名（通用模式）: {candidate}")
                        break
    
    # 2.4 组合作者和年份（确保格式为"第一作者 et al. 年份"）
    if extracted_authors:
        # 【关键修复】确保只使用第一作者（如果包含多个作者，只取第一个）
        # 如果作者名包含逗号，只取第一个作者
        if ',' in extracted_authors:
            first_author = extracted_authors.split(',')[0].strip()
        else:
            first_author = extracted_authors.strip()
        
        # 如果第一作者已经包含"et al."，移除它（因为后面会统一添加）
        if 'et al' in first_author.lower():
            # 移除"et al."部分，只保留第一作者名
            first_author = re.sub(r'\s+et\s+al\.?', '', first_author, flags=re.IGNORECASE).strip()
        
        # 优先使用从作者模式中提取的年份，否则使用独立提取的年份
        final_year = extracted_year_from_author or extracted_year
        
        # 组合作者和年份（格式：第一作者 et al. 年份）
        if final_year:
            # 如果第一作者已经包含年份，不再重复添加
            if re.search(r'\d{4}', first_author):
                authors = first_author
            else:
                # 统一格式：第一作者 et al. 年份
                authors = f"{first_author} et al. {final_year}"
        else:
            # 没有年份，格式：第一作者 et al.
            authors = f"{first_author} et al."
        
        metadata["pub1_authors"] = authors
        logger.debug(f"正则提取到作者: {authors}")
    
    # 2.5 总是保存年份（用于后续补充），无论是否提取到作者
    # 优先使用从作者模式中提取的年份，否则使用独立提取的年份
    final_year = extracted_year_from_author or extracted_year
    if final_year:
        metadata["pub1_year"] = final_year
        logger.debug(f"从PDF提取到发表年份: {final_year}")
    
    return metadata
