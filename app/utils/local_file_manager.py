#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time : 2025/11/10 15:30

import os
from tqdm import tqdm
import hashlib
import json
from app.core.config import PARSED_TEXT_PATH

def get_local_pdf_paths(root_dir):
    """递归读取根目录下所有PDF文件路径"""
    pdf_paths = []
    for root, dirs, files in os.walk(root_dir):
        for file in files:
            if file.lower().endswith(".pdf"):
                pdf_paths.append(os.path.join(root, file))
    return pdf_paths

def generate_file_hash(file_path):
    """生成文件唯一哈希（避免重复解析）"""
    with open(file_path, "rb") as f:
        md5_hash = hashlib.md5(f.read()).hexdigest()
    return md5_hash

def init_process_record(parsed_text_path=PARSED_TEXT_PATH, target_data=None):
    """初始化处理记录（新增数据类型字段）"""
    record_path = os.path.join(parsed_text_path, "process_record.json")
    if not os.path.exists(record_path):
        # 记录字段：文件哈希、是否解析、提取到的数据类型列表
        init_record = {"records": []}
        with open(record_path, "w", encoding="utf-8") as f:
            json.dump(init_record, f, ensure_ascii=False)
    return record_path

def update_process_record(record_path, file_hash, extracted_types):
    """更新记录：标记该文件提取到的数据类型"""
    with open(record_path, "r", encoding="utf-8") as f:
        record = json.load(f)
    # 查找该文件哈希的记录并更新
    for item in record["records"]:
        if item["file_hash"] == file_hash:
            item["extracted_types"] = extracted_types
            break
    with open(record_path, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)

def batch_read_local_pdf(root_dir, parsed_text_path):
    """批量读取PDF路径，返回（文件路径+文件哈希+存储路径）列表"""
    pdf_paths = get_local_pdf_paths(root_dir)
    processed_list = []
    
    os.makedirs(parsed_text_path, exist_ok=True)
    
    for pdf_path in tqdm(pdf_paths, desc="读取本地PDF"):
        file_hash = generate_file_hash(pdf_path)
        # 解析后文本存储路径（用哈希命名，避免重名）
        text_save_path = os.path.join(parsed_text_path, f"{file_hash}.md")
        processed_list.append({
            "pdf_path": pdf_path,
            "file_hash": file_hash,
            "text_save_path": text_save_path,
            "is_parsed": os.path.exists(text_save_path)  # 是否已解析
        })
    
    return processed_list