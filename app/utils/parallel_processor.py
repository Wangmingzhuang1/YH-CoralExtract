#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
并发处理模块 - 用于加速多文件处理
支持多线程和多进程处理，提供进度条和错误处理
"""

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed
from typing import Dict, List, Any, Callable, Optional, Tuple
from pathlib import Path
from datetime import datetime
import time

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# 线程安全的锁，保护日志输出
_log_lock = threading.Lock()


class ParallelProcessor:
    """并发处理器 - 支持多线程处理多个任务"""
    
    def __init__(self, max_workers: int = 3, use_threading: bool = True):
        """
        初始化并发处理器
        
        Args:
            max_workers: 最大并发数（默认5个线程）
            use_threading: 是否使用线程（True）还是进程（False）
        """
        self.max_workers = max_workers
        self.use_threading = use_threading
        self.total_tasks = 0
        self.completed_tasks = 0
        self.failed_tasks = 0
        self.results = {}
        self.errors = {}
        self._lock = threading.Lock()
        self._start_time = None
    
    def process_items(self,
                     items: List[Dict[str, Any]],
                     worker_func: Callable,
                     show_progress: bool = True) -> Tuple[List[Any], Dict[str, str]]:
        """
        并发处理项目列表
        
        Args:
            items: 要处理的项目列表，每个项目是字典，包含处理所需的信息
            worker_func: 工作函数，接收一个项目参数
            show_progress: 是否显示进度条
            
        Returns:
            (结果列表, 错误字典)
        """
        self.total_tasks = len(items)
        self.completed_tasks = 0
        self.failed_tasks = 0
        self.results = {}
        self.errors = {}
        self._start_time = time.time()
        
        logger.info(f"开始并发处理 {self.total_tasks} 个任务（最大并发数: {self.max_workers}）")
        
        # 选择 Executor 类型
        executor_class = ThreadPoolExecutor if self.use_threading else ProcessPoolExecutor
        
        try:
            with executor_class(max_workers=self.max_workers) as executor:
                # 提交所有任务
                futures = {
                    executor.submit(self._safe_execute, worker_func, item, idx): idx
                    for idx, item in enumerate(items)
                }
                
                # 收集结果
                results = []
                for future in as_completed(futures):
                    idx = futures[future]
                    try:
                        result = future.result()
                        results.append((idx, result))
                        self._update_progress("completed")
                        
                    except Exception as e:
                        logger.error(f"任务 {idx} 处理失败: {str(e)}")
                        self._update_progress("failed")
                        self.errors[idx] = str(e)
                
                # 按原始顺序排序结果
                results.sort(key=lambda x: x[0])
                final_results = [r[1] for r in results]
                
        except Exception as e:
            logger.error(f"并发处理出错: {str(e)}")
            raise
        
        # 显示统计信息
        self._print_summary()
        
        return final_results, self.errors
    
    def _safe_execute(self, worker_func: Callable, item: Dict[str, Any], idx: int) -> Any:
        """安全执行工作函数，包含错误处理"""
        try:
            logger.info(f"[{idx+1}/{self.total_tasks}] 处理任务: {item.get('name', f'task_{idx}')}")
            result = worker_func(item)
            logger.info(f"[{idx+1}/{self.total_tasks}] 任务完成: {item.get('name', f'task_{idx}')}")
            return result
        except Exception as e:
            logger.error(f"[{idx+1}/{self.total_tasks}] 任务失败: {str(e)}")
            raise
    
    def _update_progress(self, status: str):
        """更新进度（线程安全）"""
        with self._lock:
            if status == "completed":
                self.completed_tasks += 1
            elif status == "failed":
                self.failed_tasks += 1
    
    def _print_summary(self):
        """打印处理统计摘要"""
        elapsed_time = time.time() - self._start_time
        success_rate = ((self.total_tasks - self.failed_tasks) / self.total_tasks * 100) if self.total_tasks > 0 else 0
        
        logger.info("\n" + "="*60)
        logger.info("并发处理统计摘要")
        logger.info("="*60)
        logger.info(f"总任务数：{self.total_tasks}")
        logger.info(f"成功：{self.total_tasks - self.failed_tasks}")
        logger.info(f"失败：{self.failed_tasks}")
        logger.info(f"成功率：{success_rate:.2f}%")
        logger.info(f"耗时：{elapsed_time:.2f} 秒")
        logger.info(f"平均速度：{self.total_tasks / elapsed_time:.2f} 任务/秒")
        logger.info("="*60 + "\n")


class DirectoryParallelProcessor:
    """目录并发处理器 - 专门用于处理 dataset 目录中的多个子目录"""
    
    def __init__(self, max_workers: int = 5):
        """
        初始化目录处理器
        
        Args:
            max_workers: 最大并发数（默认5）
        """
        self.max_workers = max_workers
        self.processor = ParallelProcessor(max_workers=max_workers, use_threading=True)
        self._lock = threading.Lock()
    
    def process_directories(self,
                           base_dir: str,
                           process_func: Callable,
                           context_info: Optional[Dict[str, str]] = None) -> Tuple[List[Any], Dict[str, str]]:
        """
        并发处理多个子目录
        
        Args:
            base_dir: 基础目录路径
            process_func: 处理函数，接收 (subdir_path, context_info) 参数
            context_info: 上下文信息字典
            
        Returns:
            (处理结果列表, 错误字典)
        """
        # 收集所有子目录
        subdirs = []
        base_path = Path(base_dir)
        
        if not base_path.exists():
            logger.error(f"目录不存在: {base_dir}")
            return [], {}
        
        for subdir in base_path.iterdir():
            if subdir.is_dir():
                subdirs.append({
                    'name': subdir.name,
                    'path': str(subdir),
                    'context': context_info.get(subdir.name, "") if context_info else ""
                })
        
        if not subdirs:
            logger.warning(f"未找到子目录: {base_dir}")
            return [], {}
        
        logger.info(f"发现 {len(subdirs)} 个子目录，开始并发处理...")
        
        # 定义工作函数（包装原始处理函数）
        def wrapped_worker(item):
            return process_func(item['path'], item['context'])
        
        # 使用并发处理器处理
        return self.processor.process_items(subdirs, wrapped_worker, show_progress=True)


class DataProcessingBatcher:
    """数据处理批处理器 - 在处理完成后合并结果"""
    
    def __init__(self, max_workers: int = 5):
        """
        初始化数据批处理器
        
        Args:
            max_workers: 最大并发数
        """
        self.max_workers = max_workers
        self.parallel_processor = ParallelProcessor(max_workers=max_workers)
    
    def batch_process_and_merge(self,
                               items: List[Dict[str, Any]],
                               process_func: Callable,
                               merge_results: bool = True) -> Tuple[Any, Dict[str, str]]:
        """
        批处理多个项目并合并结果
        
        Args:
            items: 要处理的项目列表
            process_func: 处理函数
            merge_results: 是否合并结果（将列表结果展平）
            
        Returns:
            (合并后的结果, 错误字典)
        """
        logger.info(f"开始批处理 {len(items)} 个项目...")
        
        results, errors = self.parallel_processor.process_items(
            items,
            process_func,
            show_progress=True
        )
        
        # 合并结果
        if merge_results:
            merged_result = []
            for result in results:
                if isinstance(result, list):
                    merged_result.extend(result)
                else:
                    merged_result.append(result)
            return merged_result, errors
        
        return results, errors


# 示例使用
if __name__ == "__main__":
    import time
    
    # 示例1：并发处理简单任务
    def example_task(item):
        """示例工作函数"""
        name = item.get('name', 'unknown')
        duration = item.get('duration', 1)
        time.sleep(duration)  # 模拟耗时操作
        return f"完成: {name}"
    
    items = [
        {'name': 'task_1', 'duration': 2},
        {'name': 'task_2', 'duration': 2},
        {'name': 'task_3', 'duration': 2},
        {'name': 'task_4', 'duration': 2},
        {'name': 'task_5', 'duration': 2},
    ]
    
    print("\n演示1：并发处理 5 个任务（2秒/任务）")
    print("不使用并发：需要 10 秒")
    print("使用 5 个线程：需要 2 秒（加速 5 倍）\n")
    
    processor = ParallelProcessor(max_workers=5)
    results, errors = processor.process_items(items, example_task)
    
    print(f"\n结果示例：")
    for i, result in enumerate(results[:3]):
        print(f"  {i+1}. {result}")
