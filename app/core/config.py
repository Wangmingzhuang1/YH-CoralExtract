#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time : 2025/8/23 10:33
# @Author : 桐
# @QQ:1041264242
# 注意事项：
from pydantic_settings import BaseSettings, SettingsConfigDict
import os

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Use top level .env file (one level above ./backend/)
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
    )

    # 本项目配置
    PAPER_ASSISTANT_OUTPUT_PATH: str = "./output"  # 修改为output文件夹
    PARSED_TEXT_PATH: str = "./parsed_texts"  # 解析后文本存储目录
    DATA_CONFIG_PATH: str = "./data_config.yaml"  # 数据配置文件路径
    LOCAL_PDF_PATH: str = "./pdfs"  # 本地PDF存储根目录

    # 大模型调用配置（OpenAI兼容端点）
    # 默认指向阿里云百炼 Token Plan；切换按量付费只需在 .env 中改为
    # LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1 并替换 LLM_API_KEY
    LLM_BASE_URL: str = "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    LLM_API_KEY: str = ""
    LLM_MODEL_MAX: str = "qwen3.8-max"    # 复杂推理任务（提取保持3.8-max复用缓存；复核档见REVIEW_MODEL）
    LLM_MODEL_FLASH: str = "qwen3.8-flash"  # 轻量任务
    LLM_TIMEOUT: int = 300  # 单次请求超时（秒）
    LLM_ENABLE_THINKING: bool = False  # 默认关闭思考模式（结构化判断任务无需长思考）
    LLM_THINKING_BUDGET: int = 0  # 思考token预算，0=不限制（仅开启思考时生效）

    # 论文级复核（PaperReview）模型档位：默认 max 关思考，与历史行为一致。
    # A/B 对比时可在 .env 或评测脚本中覆盖，如 REVIEW_MODEL=flash + REVIEW_ENABLE_THINKING=true
    REVIEW_MODEL: str = "max"
    REVIEW_ENABLE_THINKING: bool = False
    REVIEW_THINKING_BUDGET: int = 0  # 0=不限制；中等思考可设 4096

    # Jev判卷层配置（置信度门禁，默认off=不启用，行为与无判卷层完全一致）
    # typesafe: TypeSafe Jev官方API；ark: 火山方舟决策评估beta（国内端点）
    JUDGE_BACKEND: str = "off"
    JUDGE_API_KEY: str = ""
    JUDGE_MODEL: str = "jev-latest"  # ark后端需改为方舟的决策模型ID
    JUDGE_BASE_URL: str = ""  # 留空使用后端默认端点
    JUDGE_PASS_THRESHOLD: float = 0.60  # ≥此概率直接放行（tev1:4b+上下文增强24命题校准：正确min0.466/错误max0.139，零翻转）
    JUDGE_FAIL_THRESHOLD: float = 0.40  # ≤此概率撤销映射并进复核队列；两者之间保留并进复核队列（.env 实配0.30更保守，等效）
    JUDGE_TIMEOUT: int = 60
    JUDGE_REVIEW_ONLY: bool = True  # 低分仅进入复核队列，原映射照常接受源证据检查
    # 低置信映射升级复核：Jev判为低分的列交生成式强模型独立重判（默认off=不升级，行为与纯Jev门禁一致）
    # 实测(deepseek-v4-pro,24命题)：错误映射挽救12/12、漏放0；复核带并入升级后人工2→1、漏检0
    JUDGE_ESCALATE: bool = False
    JUDGE_ESCALATE_MODEL: str = "deepseek-v4-pro"  # 走 LLM_BASE_URL（Token Plan），call_llm 直接传模型ID

    # 代理配置
    HTTP_PROXY: str = ""
    HTTPS_PROXY: str = ""

    # 数据库配置（已停用）
    # DB_SERVER: str = "127.0.0.1"
    # DB_USER: str = ""
    # DB_PORT: int = 3306
    # DB_PASSWORD: str = ""
    # DB_DBNAME: str = ""

    # redis配置（已停用）
    # REDIS_SERVER: str = "127.0.0.1"
    # REDIS_PORT: int = 6380
    # REDIS_PASSWORD: str = ""
    # REDIS_DB_INDEX: int = 0

settings = Settings()

OUTPUT_PATH=settings.PAPER_ASSISTANT_OUTPUT_PATH #这里为结果文件输出路径
LOCAL_PDF_PATH=settings.LOCAL_PDF_PATH  # 本地PDF存储根目录
PARSED_TEXT_PATH=settings.PARSED_TEXT_PATH  # 解析后文本存储目录
DATA_CONFIG_PATH=settings.DATA_CONFIG_PATH  # 数据配置文件路径

# MinerU Token配置（已停用，改用大模型API直接解析）

# 代理地址配置（仅用于requests库，不用于agentscope）
Proxies = {}
if settings.HTTP_PROXY:
    Proxies['http'] = settings.HTTP_PROXY
if settings.HTTPS_PROXY:
    Proxies['https'] = settings.HTTPS_PROXY
