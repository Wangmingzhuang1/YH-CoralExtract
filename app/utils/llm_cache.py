#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
LLM 缓存和批处理模块
用于减少 API 调用，提升性能
"""

import hashlib
import json
import logging
import sqlite3
import os
from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta
from pathlib import Path

from app.core.config import settings

# 缓存键包含模型标识：切换模型后旧缓存自动失效，避免旧模型结果污染
_CACHE_MODEL_TAG = f"{settings.LLM_MODEL_MAX}|{settings.LLM_MODEL_FLASH}"

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class LLMCache:
    """LLM 缓存管理器 - 使用 SQLite 存储缓存结果"""
    
    def __init__(self, cache_dir: str = "cache", ttl_hours: int = 24 * 7):
        """
        初始化缓存管理器
        
        Args:
            cache_dir: 缓存目录
            ttl_hours: 缓存过期时间（小时），默认7天
        """
        self.cache_dir = cache_dir
        self.ttl_hours = ttl_hours
        self.db_path = os.path.join(cache_dir, "llm_cache.db")
        
        # 创建缓存目录
        os.makedirs(cache_dir, exist_ok=True)
        
        # 初始化数据库
        self._init_db()
    
    def _init_db(self):
        """初始化 SQLite 数据库"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS llm_cache (
                cache_key TEXT PRIMARY KEY,
                input_hash TEXT NOT NULL,
                result TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                hit_count INTEGER DEFAULT 0
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS cache_stats (
                metric TEXT PRIMARY KEY,
                value INTEGER DEFAULT 0,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # 初始化统计字段
        cursor.execute("INSERT OR IGNORE INTO cache_stats (metric, value) VALUES ('total_hits', 0)")
        cursor.execute("INSERT OR IGNORE INTO cache_stats (metric, value) VALUES ('total_misses', 0)")
        cursor.execute("INSERT OR IGNORE INTO cache_stats (metric, value) VALUES ('api_calls_saved', 0)")
        
        conn.commit()
        conn.close()
    
    def _generate_cache_key(self, table_headers: List[str], context: str = "") -> str:
        """
        生成缓存键：基于表格头部和上下文的哈希值
        
        Args:
            table_headers: 表格列名列表
            context: 上下文信息（如文献标题）
            
        Returns:
            缓存键（MD5 哈希）
        """
        # 排序列名以确保同样的列顺序不同不会产生不同的键
        headers_str = "|".join(sorted(table_headers))
        key_input = f"{_CACHE_MODEL_TAG}:{headers_str}:{context}"
        
        # 生成 MD5 哈希
        cache_key = hashlib.md5(key_input.encode()).hexdigest()
        return cache_key
    
    def get(self, table_headers: List[str], context: str = "") -> Optional[Dict[str, Any]]:
        """
        从缓存中获取结果
        
        Args:
            table_headers: 表格列名列表
            context: 上下文信息
            
        Returns:
            缓存的结果，如果缓存不存在或已过期则返回 None
        """
        cache_key = self._generate_cache_key(table_headers, context)
        
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # 查询缓存
            cursor.execute("""
                SELECT result, timestamp, hit_count 
                FROM llm_cache 
                WHERE cache_key = ?
            """, (cache_key,))
            
            row = cursor.fetchone()
            
            if row is None:
                self._update_stat('total_misses', 1)
                logger.debug(f"缓存未命中: {cache_key}")
                conn.close()
                return None
            
            result_json, timestamp_str, hit_count = row
            
            # 检查缓存是否过期
            timestamp = datetime.fromisoformat(timestamp_str)
            if datetime.now() - timestamp > timedelta(hours=self.ttl_hours):
                logger.debug(f"缓存已过期: {cache_key}")
                cursor.execute("DELETE FROM llm_cache WHERE cache_key = ?", (cache_key,))
                conn.commit()
                self._update_stat('total_misses', 1)
                conn.close()
                return None
            
            # 更新缓存命中计数
            cursor.execute(
                "UPDATE llm_cache SET hit_count = ? WHERE cache_key = ?",
                (hit_count + 1, cache_key)
            )
            conn.commit()
            conn.close()
            
            # 反序列化结果
            result = json.loads(result_json)
            self._update_stat('total_hits', 1)
            logger.info(f"缓存命中: {cache_key}，节省 API 调用")
            
            return result
            
        except Exception as e:
            logger.error(f"缓存读取错误: {str(e)}")
            return None
    
    def set(self, table_headers: List[str], result: Dict[str, Any], context: str = ""):
        """
        保存结果到缓存
        
        Args:
            table_headers: 表格列名列表
            result: LLM 解析结果
            context: 上下文信息
        """
        cache_key = self._generate_cache_key(table_headers, context)
        
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # 生成输入哈希
            headers_str = "|".join(sorted(table_headers))
            input_hash = hashlib.md5(headers_str.encode()).hexdigest()
            
            # 序列化结果
            result_json = json.dumps(result, ensure_ascii=False)
            
            # 插入或更新缓存
            cursor.execute("""
                INSERT OR REPLACE INTO llm_cache (cache_key, input_hash, result, timestamp, hit_count)
                VALUES (?, ?, ?, datetime('now'), 0)
            """, (cache_key, input_hash, result_json))
            
            conn.commit()
            conn.close()
            
            logger.debug(f"缓存保存: {cache_key}")
            
        except Exception as e:
            logger.error(f"缓存保存错误: {str(e)}")
    
    def clear(self):
        """清空所有缓存"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM llm_cache")
            cursor.execute("UPDATE cache_stats SET value = 0")
            conn.commit()
            conn.close()
            logger.info("缓存已清空")
        except Exception as e:
            logger.error(f"缓存清空错误: {str(e)}")
    
    def get_stats(self) -> Dict[str, Any]:
        """获取缓存统计信息"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            
            # 获取统计数据
            cursor.execute("SELECT metric, value FROM cache_stats")
            stats = {row[0]: row[1] for row in cursor.fetchall()}
            
            # 计算缓存命中率
            total_requests = stats.get('total_hits', 0) + stats.get('total_misses', 0)
            hit_rate = (stats.get('total_hits', 0) / total_requests * 100) if total_requests > 0 else 0
            
            # 获取缓存大小
            cursor.execute("SELECT COUNT(*) FROM llm_cache")
            cache_size = cursor.fetchone()[0]
            
            conn.close()
            
            return {
                'total_hits': stats.get('total_hits', 0),
                'total_misses': stats.get('total_misses', 0),
                'total_requests': total_requests,
                'hit_rate': f"{hit_rate:.2f}%",
                'api_calls_saved': stats.get('api_calls_saved', 0),
                'cache_size': cache_size
            }
        except Exception as e:
            logger.error(f"统计信息获取错误: {str(e)}")
            return {}
    
    def _update_stat(self, metric: str, value: int):
        """更新统计数据"""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE cache_stats SET value = value + ?, updated_at = datetime('now') WHERE metric = ?",
                (value, metric)
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"统计更新错误: {str(e)}")


class LLMBatchProcessor:
    """LLM 批处理器 - 分组处理相同结构的表格"""
    
    def __init__(self, cache: LLMCache):
        """
        初始化批处理器
        
        Args:
            cache: LLM 缓存实例
        """
        self.cache = cache
        self.table_groups = {}  # 按表格结构分组
    
    def group_tables(self, tables: List[Dict[str, Any]]) -> Dict[str, List[int]]:
        """
        按表格结构分组表格
        
        Args:
            tables: 表格列表
            
        Returns:
            分组结果：{结构特征 -> 表格索引列表}
        """
        groups = {}
        
        for idx, table in enumerate(tables):
            headers = tuple(sorted(table.get('headers', [])))
            header_key = hashlib.md5(str(headers).encode()).hexdigest()
            
            if header_key not in groups:
                groups[header_key] = []
            groups[header_key].append(idx)
        
        logger.info(f"将 {len(tables)} 个表格分为 {len(groups)} 组")
        return groups
    
    def process_batch(self, 
                     tables: List[Dict[str, Any]], 
                     parse_func,
                     context: str = "") -> List[Dict[str, Any]]:
        """
        批量处理表格
        
        Args:
            tables: 表格列表
            parse_func: 解析函数（接收表格和上下文）
            context: 上下文信息
            
        Returns:
            解析结果列表
        """
        results = [None] * len(tables)
        
        # 分组
        groups = self.group_tables(tables)
        
        api_calls_total = 0
        api_calls_saved = 0
        
        # 逐组处理
        for group_key, indices in groups.items():
            logger.info(f"处理表格组: {len(indices)} 个表格")
            
            # 先检查所有表格的缓存
            cached_indices = []
            uncached_indices = []
            
            for idx in indices:
                table = tables[idx]
                headers = table.get('headers', [])
                
                # 尝试从缓存获取
                cached_result = self.cache.get(headers, context)
                if cached_result is not None:
                    results[idx] = cached_result
                    cached_indices.append(idx)
                    api_calls_saved += 1
                else:
                    uncached_indices.append(idx)
            
            # 批量处理未缓存的表格
            if uncached_indices:
                logger.info(f"缓存命中: {len(cached_indices)}/{len(indices)}，处理未缓存表格: {len(uncached_indices)}")
                
                for idx in uncached_indices:
                    table = tables[idx]
                    headers = table.get('headers', [])
                    
                    # 调用解析函数
                    result = parse_func(table, context)
                    results[idx] = result
                    api_calls_total += 1
                    
                    # 保存到缓存
                    self.cache.set(headers, result, context)
            else:
                logger.info(f"该组所有表格已命中缓存，无需 API 调用")
        
        logger.info(f"批处理完成：API 调用 {api_calls_total} 次，缓存命中 {api_calls_saved} 次")
        
        return results
    
    def get_savings_report(self) -> Dict[str, Any]:
        """获取节省成本报告"""
        stats = self.cache.get_stats()
        
        # 假设每次 API 调用成本为 0.01 元
        COST_PER_API_CALL = 0.01
        
        total_api_calls = stats.get('total_hits', 0) + stats.get('total_misses', 0)
        calls_without_cache = total_api_calls
        calls_with_cache = stats.get('total_misses', 0)
        calls_saved = stats.get('total_hits', 0)
        
        cost_without_cache = calls_without_cache * COST_PER_API_CALL
        cost_with_cache = calls_with_cache * COST_PER_API_CALL
        cost_saved = calls_saved * COST_PER_API_CALL
        
        return {
            '总请求次数': total_api_calls,
            '无缓存情况下的 API 调用': calls_without_cache,
            '有缓存情况下的 API 调用': calls_with_cache,
            '节省的 API 调用次数': calls_saved,
            '节省百分比': f"{(calls_saved / calls_without_cache * 100):.2f}%" if calls_without_cache > 0 else "0%",
            '无缓存成本': f"¥{cost_without_cache:.2f}",
            '有缓存成本': f"¥{cost_with_cache:.2f}",
            '节省成本': f"¥{cost_saved:.2f}",
            '缓存命中率': stats.get('hit_rate', '0%'),
            '缓存大小': f"{stats.get('cache_size', 0)} 条记录"
        }


# 示例使用
if __name__ == "__main__":
    # 创建缓存实例
    cache = LLMCache(cache_dir="cache")
    
    # 模拟表格数据
    table1 = {
        'headers': ['Sr/Ca', 'δ18O', 'Year'],
        'data': [[8.5, -5.2, 2020]]
    }
    
    table2 = {
        'headers': ['Sr/Ca', 'δ18O', 'Year'],  # 相同结构
        'data': [[8.6, -5.3, 2021]]
    }
    
    table3 = {
        'headers': ['SST', 'Mg/Ca'],  # 不同结构
        'data': [[25.3, 4.5]]
    }
    
    # 模拟解析函数
    def mock_parse_func(table, context):
        return {
            "mapped_data": [{"col": h, "standard_field": f"field_{i}"} for i, h in enumerate(table['headers'])],
            "table_metadata": {"structure": "standard"}
        }
    
    # 创建批处理器
    batch_processor = LLMBatchProcessor(cache)
    tables = [table1, table2, table3]
    
    # 第一次批处理（所有表格都会调用 API）
    print("第一次批处理...")
    results1 = batch_processor.process_batch(tables, mock_parse_func, context="test")
    
    # 第二次批处理（相同结构的表格会从缓存读取）
    print("\n第二次批处理...")
    results2 = batch_processor.process_batch(tables, mock_parse_func, context="test")
    
    # 显示节省报告
    print("\n成本节省报告:")
    report = batch_processor.get_savings_report()
    for key, value in report.items():
        print(f"  {key}: {value}")
