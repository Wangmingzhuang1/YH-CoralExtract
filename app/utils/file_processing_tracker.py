#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
文件处理状态跟踪模块
用于防止重复处理同一文件，支持大规模数据处理
"""

import os
import json
import hashlib
import logging
from typing import Dict, Set, Optional
from pathlib import Path

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class FileProcessingTracker:
    """文件处理状态跟踪器"""
    
    def __init__(self, state_file: str = "cache/file_processing_state.json"):
        """
        初始化文件处理跟踪器
        
        Args:
            state_file: 状态文件路径
        """
        self.state_file = state_file
        self.state_dir = os.path.dirname(state_file)
        if self.state_dir and not os.path.exists(self.state_dir):
            os.makedirs(self.state_dir, exist_ok=True)
        
        # 加载已有状态
        self.processed_files: Set[str] = set()
        self.file_hashes: Dict[str, str] = {}  # 文件路径 -> MD5哈希
        self.load_state()
    
    def load_state(self):
        """从文件加载处理状态"""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    state = json.load(f)
                    self.processed_files = set(state.get("processed_files", []))
                    self.file_hashes = state.get("file_hashes", {})
                logging.info(f"加载文件处理状态: {len(self.processed_files)} 个已处理文件")
            except Exception as e:
                logging.warning(f"加载文件处理状态失败: {str(e)}")
                self.processed_files = set()
                self.file_hashes = {}
    
    def save_state(self):
        """保存处理状态到文件"""
        try:
            state = {
                "processed_files": list(self.processed_files),
                "file_hashes": self.file_hashes
            }
            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.error(f"保存文件处理状态失败: {str(e)}")
    
    def calculate_file_hash(self, file_path: str) -> Optional[str]:
        """
        计算文件的MD5哈希值
        
        Args:
            file_path: 文件路径
            
        Returns:
            str: MD5哈希值，如果文件不存在则返回None
        """
        if not os.path.exists(file_path):
            return None
        
        try:
            hash_md5 = hashlib.md5()
            with open(file_path, "rb") as f:
                for chunk in iter(lambda: f.read(4096), b""):
                    hash_md5.update(chunk)
            return hash_md5.hexdigest()
        except Exception as e:
            logging.error(f"计算文件哈希失败 {file_path}: {str(e)}")
            return None
    
    def is_file_processed(self, file_path: str) -> bool:
        """
        检查文件是否已处理
        
        Args:
            file_path: 文件路径（绝对路径或相对路径）
            
        Returns:
            bool: 如果文件已处理则返回True
        """
        # 转换为绝对路径
        abs_path = os.path.abspath(file_path)
        
        # 检查是否在处理列表中
        if abs_path in self.processed_files:
            # 验证文件是否被修改（通过哈希值）
            current_hash = self.calculate_file_hash(abs_path)
            stored_hash = self.file_hashes.get(abs_path)
            
            if current_hash and stored_hash:
                if current_hash == stored_hash:
                    logging.debug(f"文件已处理且未修改: {file_path}")
                    return True
                else:
                    logging.info(f"文件已修改，需要重新处理: {file_path}")
                    # 文件已修改，移除旧记录
                    self.processed_files.discard(abs_path)
                    self.file_hashes.pop(abs_path, None)
                    return False
            else:
                # 无法计算哈希，假设已处理
                return True
        
        return False
    
    def mark_file_processed(self, file_path: str):
        """
        标记文件为已处理
        
        Args:
            file_path: 文件路径
        """
        abs_path = os.path.abspath(file_path)
        file_hash = self.calculate_file_hash(abs_path)
        
        if file_hash:
            self.processed_files.add(abs_path)
            self.file_hashes[abs_path] = file_hash
            self.save_state()
            logging.debug(f"标记文件为已处理: {file_path}")
        else:
            logging.warning(f"无法计算文件哈希，跳过标记: {file_path}")
    
    def mark_csv_files_processed(self, original_file: str, csv_files: list):
        """
        标记原始文件及其转换的CSV文件为已处理
        
        Args:
            original_file: 原始文件路径（Excel/Word）
            csv_files: 转换后的CSV文件路径列表
        """
        # 标记原始文件
        self.mark_file_processed(original_file)
        
        # 标记所有CSV文件
        for csv_file in csv_files:
            if os.path.exists(csv_file):
                self.mark_file_processed(csv_file)
    
    def clear_state(self):
        """清空处理状态（用于测试或重置）"""
        self.processed_files.clear()
        self.file_hashes.clear()
        self.save_state()
        logging.info("文件处理状态已清空")
    
    def get_statistics(self) -> Dict[str, int]:
        """
        获取处理统计信息
        
        Returns:
            Dict: 统计信息
        """
        return {
            "processed_files_count": len(self.processed_files),
            "tracked_hashes_count": len(self.file_hashes)
        }

# 全局单例
_tracker_instance: Optional[FileProcessingTracker] = None

def get_tracker() -> FileProcessingTracker:
    """获取全局文件处理跟踪器实例"""
    global _tracker_instance
    if _tracker_instance is None:
        _tracker_instance = FileProcessingTracker()
    return _tracker_instance

