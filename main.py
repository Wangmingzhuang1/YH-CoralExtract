#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
古气候数据提取项目主入口
支持多种运行模式：
1. MOA 提取（默认，complete 为兼容别名）
2. 演示模式（使用模拟数据）
3. 图表分析
"""

import os
import sys
import argparse
import logging
from pathlib import Path

# 配置日志###
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("project_run.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)

# 添加项目路径到Python路径
project_root = Path(__file__).parent.absolute()
sys.path.insert(0, str(project_root))

def setup_environment():
    """设置环境变量"""
    os.environ['PYTHONPATH'] = str(project_root)
    logging.info("环境设置完成")

def run_moa_pipeline(enable_calibration_check=True, use_parallel=False, max_workers=5,
                     include_noaa=True, include_pangaea=True, include_zenodo=True, review_decisions=None,
                     review_max_papers=None, review_rules_only=False, auto_discard=False):
    """运行 MOA，只有完整批次发布后才返回成功。"""
    from app.core.moa.workflow import MOAWorkflow
    from app.core.moa.full_review import DEFAULT_MAX_PAPERS, known_review_decisions, review_before_export

    sources = {
        "noaa_dir": "noaa" if include_noaa and os.path.isdir("noaa") else None,
        "pangaea_dir": "pangaea" if include_pangaea and os.path.isdir("pangaea") else None,
        "zenodo_dir": "zenodo" if include_zenodo and os.path.isdir("zenodo") else None,
    }
    if not os.path.isdir("dataset") and not any(sources.values()):
        raise FileNotFoundError("没有可用的 MOA 输入目录")
    context_info = {
        path.name: "古气候数据附录文件"
        for path in Path("dataset").rglob("*")
        if path.is_file() and path.suffix.lower() in {".xlsx", ".xls", ".csv"}
    }
    try:
        output_root = Path("output/moa_output")
        source_root = Path.cwd()
        decisions = known_review_decisions(source_root, output_root, review_decisions)
        logging.info("已加载 %d 条可用复核裁决", len(decisions))
        workflow = MOAWorkflow(
            output_dir=str(output_root), enable_calibration_check=enable_calibration_check,
            max_workers=max_workers, review_decisions=decisions,
        )
        all_data = workflow.process_directory("dataset", context_info, use_parallel=use_parallel, **sources)
        logging.info("MOA 处理完成，共 %d 条候选记录", len(all_data))
        paper_review = review_before_export(
            workflow, "dataset", context_info, sources, source_root=source_root, use_parallel=use_parallel,
            max_papers=DEFAULT_MAX_PAPERS if review_max_papers is None else review_max_papers,
            rules_only=review_rules_only, auto_discard=auto_discard,
        )
        review_summary = paper_review[0]
        logging.info("论文复核：%s，复用裁决 %d 条，本轮回灌 %d 条，复核组状态 %s", review_summary["status"],
                     review_summary["reused_decisions"], review_summary["decision_rows"], review_summary["cases"])
        output_path = workflow.generate_final_output(paper_review=paper_review)
        if not output_path:
            raise RuntimeError("批次没有可发布的观测或复核项")
        logging.info("MOA 批次已输出到 %s", output_path)
        return output_path
    except Exception:
        logging.exception("MOA 批次失败")
        raise
    finally:
        try:
            from app.utils.llm_api import log_usage_summary
            log_usage_summary()
        except Exception:
            logging.warning("无法汇总本批次模型用量", exc_info=True)


def run_demo_pipeline():
    """运行演示流水线（使用模拟数据）"""
    logging.info("=" * 60)
    logging.info("开始运行演示版古气候数据提取流水线")
    logging.info("=" * 60)
    
    try:
        # 动态导入模块
        from app.utils.data_standardize import literature_based_standardize
        from app.core.report_generator import generate_data_summary
        from app.utils.config_loader import load_target_data_config
        
        # 生成示例数据
        logging.info("生成示例数据...")
        
        # 模拟提取的数据（包含必要的元数据字段和年份信息，确保年份范围与元数据一致）
        # 为了通过时间一致性检查，我们需要确保数据点中的年份与元数据中的时间范围一致
        extracted_data = [
            {
                "paper_hash": "sample001",
                "sampling_location": "南海",
                "geological_age": "全新世",
                "testing_method": "ICP-MS",
                "锶钙比_value": 8.5,
                "锶钙比_unit": "mmol/mol",
                "氧同位素_value": -5.2,
                "氧同位素_unit": "‰",
                "year": 1900,  # 与time_oldestYear一致
                # 必需的元数据字段
                "paleoData_coralHydro2kGroup": 1,
                "geo_latitude": 18.5,
                "geo_longitude": 109.2,
                "geo_siteName": "南海-中国",
                "hasResolution_nominal": "monthly",
                "time_oldestYear": 1900,
                "time_youngestYear": 1900,  # 与year一致，确保通过时间一致性检查
                "pub1_doi": "10.1000/sample001"
            },
            {
                "paper_hash": "sample002",
                "sampling_location": "西太平洋",
                "geological_age": "全新世",
                "testing_method": "IRMS",
                "碳同位素_value": -2.1,
                "碳同位素_unit": "‰",
                "海表温度_value": 28.5,
                "海表温度_unit": "°C",
                "year": 1850,  # 与time_oldestYear一致
                # 必需的元数据字段
                "paleoData_coralHydro2kGroup": 3,
                "geo_latitude": -8.5,
                "geo_longitude": 120.3,
                "geo_siteName": "西太平洋-印尼",
                "hasResolution_nominal": "annual",
                "time_oldestYear": 1850,
                "time_youngestYear": 1850,  # 与year一致，确保通过时间一致性检查
                "pub1_doi": "10.1000/sample002"
            }
        ]
        
        logging.info(f"生成了 {len(extracted_data)} 条模拟数据")
        
        if not extracted_data:
            logging.error("未生成有效数据，流程终止")
            return
        
        # 标准化数据
        logging.info("开始数据标准化...")
        standardized_data, quality_reports = literature_based_standardize(extracted_data)
        logging.info(f"数据标准化完成，标准化后 {len(standardized_data)} 条数据")
        
        # 保存质量报告
        quality_report_path = project_root / "数据质量报告.json"
        with open(quality_report_path, "w", encoding="utf-8") as f:
            import json
            json.dump(quality_reports, f, ensure_ascii=False, indent=2)
        logging.info(f"数据质量报告已保存到 {quality_report_path}")
        
        if not standardized_data:
            logging.error("数据标准化后无有效数据，流程终止")
            return
        
        # 生成报告
        logging.info("开始生成报告...")
        target_data = load_target_data_config()
        summary = generate_data_summary(standardized_data, target_data)
        
        # 保存报告到唯一的输出目录
        import datetime
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        unique_output_dir = project_root / "output" / f"run_{timestamp}"
        unique_output_dir.mkdir(parents=True, exist_ok=True)
        
        report_path = unique_output_dir / "古气候数据提取报告.md"
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("# 古气候数据提取报告（演示版）\n\n")
            f.write("## 数据统计\n")
            f.write("| 数据类型 | 有效数据点 | 单位 |\n")
            f.write("|---------|-----------|------|\n")
            for data_name, stats in summary.items():
                f.write(f"| {data_name} | {stats['有效数据点']} | {stats['单位']} |\n")
            
            f.write(f"\n数据来源统计：共处理 {len(standardized_data)} 个数据点\n")
            f.write("\n## 数据示例\n")
            for i, point in enumerate(standardized_data[:5]):  # 显示前5个示例
                f.write(f"\n### 数据点 {i+1}\n")
                for key, value in point.items():
                    f.write(f"- {key}: {value}\n")
        
        logging.info(f"报告生成完成：{report_path}")
        
        # 保存质量报告到唯一输出目录
        quality_report_path = unique_output_dir / "数据质量报告.json"
        with open(quality_report_path, "w", encoding="utf-8") as f:
            import json
            json.dump(quality_reports, f, ensure_ascii=False, indent=2)
        logging.info(f"数据质量报告已保存到 {quality_report_path}")

        logging.info("\n" + "=" * 60)
        logging.info("演示版古气候数据提取流水线运行完成！")
        logging.info("=" * 60)
        
    except Exception as e:
        logging.error(f"运行演示流水线时发生错误: {str(e)}")
        raise

def run_analysis_pipeline(run_dir, figures=None, external_data_dir=None, ranges=None):
    """运行论文分析阶段：基于已有 run 目录生成 Figure1-6 等图表（不执行数据提取）"""
    logging.info("=" * 60)
    logging.info("开始运行论文分析阶段（run 目录: %s）", run_dir)
    logging.info("=" * 60)

    from app.analysis import run_analysis

    figure_list = None
    if figures:
        figure_list = [f.strip() for f in figures.split(',') if f.strip()]

    produced = run_analysis(run_dir, figures=figure_list,
                            external_data_dir=external_data_dir, ranges=ranges)
    logging.info("分析完成，生成图表: %s", produced)
    return produced

def main():
    """主函数"""
    parser = argparse.ArgumentParser(description="古气候数据提取项目主入口")
    parser.add_argument(
        "--mode", 
        choices=["complete", "demo", "moa", "analyze", "patch", "review", "audit"],
        default="moa",
        help="运行模式：moa（提取，默认）、review（按论文复核）、patch（增量更新）、"
             "audit（按研究目录覆盖对账+定向回收）、analyze（图表分析）"
    )
    parser.add_argument(
        "--run-dir",
        default=None,
        help="analyze、review 或 patch 模式使用的已有 run 目录"
    )
    parser.add_argument(
        "--figures",
        default=None,
        help="analyze 模式可选：逗号分隔的图号子集（如 1,4），缺省生成全部图"
    )
    parser.add_argument(
        "--external-data",
        default=None,
        help="analyze 模式可选：外部格点数据目录（含 sst.mnmean.nc / "
             "Hadley_EN4_SSS_Surface_Merged.nc）。缺省先在 run_dir 内探测，找不到再回退"
             "同级 run_20251224_161554，仍找不到则跳过 Figure5/6 并告警"
    )
    parser.add_argument("--year-min", type=int, default=None,
                        help="analyze 模式可选：显式覆盖主图年份下界（缺省按数据自适应）")
    parser.add_argument("--year-max", type=int, default=None,
                        help="analyze 模式可选：显式覆盖主图年份上界（缺省按数据自适应）")
    parser.add_argument("--long-year-min", type=int, default=None,
                        help="analyze 模式可选：显式覆盖长尺度面板下界（缺省 -10050）")
    parser.add_argument("--long-year-max", type=int, default=None,
                        help="analyze 模式可选：显式覆盖长尺度面板上界（缺省=主图下界）")
    parser.add_argument(
        "--disable-calibration-check", 
        action="store_true",
        help="禁用校准检查（仅在moa模式下有效）"
    )
    parser.add_argument("--review-decisions", action="append", default=None,
                        help="提取或增量更新时读取已填写的单位和结构裁决表，可重复传入")
    parser.add_argument("--review-file", action="append", default=[],
                        help="review 模式按文件名、列名或原因筛选复核组，可重复传入")
    parser.add_argument("--review-max-papers", type=int, default=None,
                        help="交给 Max 的论文数上限（0 表示全部，moa 默认全部，review 默认 8）")
    parser.add_argument("--review-rules-only", action="store_true", help="论文级复核只执行本地规则")
    parser.add_argument("--auto-discard", action="store_true",
                        help="全自动模式：复核受支持的暂缓项，已完成复核且证据不足时自动结案，未完成项留痕")
    parser.add_argument("--review-apply", action="store_true", help="review 模式增量回灌已通过核验的裁决")
    parser.add_argument("--no-flash", action="store_true",
                        help="audit 模式可选：只跑确定性对账，不调用 flash 证据定位")
    parser.add_argument("--folders", default=None,
                        help="audit 模式可选：逗号分隔的研究目录子集（小样验证），缺省审计全部")
    parser.add_argument("--parallel", action="store_true",
                        help="MOA 模式可选，启用多线程并行处理（实测与串行输出一致，共享态加锁+复核ContextVar隔离）")
    parser.add_argument("--max-workers", type=int, default=5,
                        help="MOA 并行最大线程数（默认5；判卷为本地单模型会排队，线程主要重叠Max网络调用与PDF解析）")
    # 项目已改为处理本地已下载的附件文件，不再需要这些参数
    args = parser.parse_args()
    if args.review_decisions and args.mode not in {"moa", "complete", "patch"}:
        parser.error("--review-decisions 仅用于提取模式")
    if args.review_apply and args.mode != "review":
        parser.error("--review-apply 仅用于 review 模式")
    if args.review_max_papers is not None and args.review_max_papers < 0:
        parser.error("--review-max-papers 不能为负数")
    if args.max_workers < 1:
        parser.error("--max-workers 必须大于 0")
    
    # 设置环境
    setup_environment()
    
    # 根据模式运行
    if args.mode == "analyze":
        if not args.run_dir:
            parser.error("analyze 模式必须通过 --run-dir 指定 run 目录")
        ranges = {}
        if args.year_min is not None:
            ranges['year_min'] = args.year_min
        if args.year_max is not None:
            ranges['year_max'] = args.year_max
        if args.long_year_min is not None:
            ranges['long_year_min'] = args.long_year_min
        if args.long_year_max is not None:
            ranges['long_year_max'] = args.long_year_max
        run_analysis_pipeline(args.run_dir, args.figures,
                              external_data_dir=args.external_data,
                              ranges=ranges or None)
    elif args.mode == "review":
        if not args.run_dir:
            parser.error("review 模式需要 --run-dir")
        from app.review_workbench.paper_review import DECISIONS, PaperReview
        review = PaperReview(args.run_dir, project_root, rules_only=args.review_rules_only)
        summary = review.run(args.review_file, 8 if args.review_max_papers is None else args.review_max_papers,
                             workers=max(1, args.max_workers) if args.parallel else 1)
        logging.info("论文复核完成：%s，裁决表 %d 行，Max 调用 %d 次", summary["cases"],
                     summary["decision_rows"], summary["model_calls"])
        if args.review_apply and summary["decision_rows"]:
            from app.core.moa.patch_apply import apply_patch
            new_run = apply_patch(args.run_dir, review_decisions=str(review.store.work_dir / DECISIONS),
                                  use_parallel=args.parallel, max_workers=args.max_workers,
                                  enable_calibration_check=not args.disable_calibration_check,
                                  review_summary=summary)
            logging.info("复核裁决已生成增量批次：%s", new_run)
    elif args.mode == "patch":
        if not args.run_dir:
            parser.error("patch 模式必须通过 --run-dir 指定 run 目录")
        from app.core.moa.patch_apply import apply_patch
        new_run = apply_patch(args.run_dir, review_decisions=args.review_decisions,
                              use_parallel=args.parallel, max_workers=args.max_workers,
                              enable_calibration_check=not args.disable_calibration_check)
        logging.info("补丁产出目录: %s", new_run)
    elif args.mode == "audit":
        if not args.run_dir:
            parser.error("audit 模式必须通过 --run-dir 指定 run 目录")
        from app.core.moa.coverage_audit import audit_run
        folders = [f.strip() for f in args.folders.split(",")] if args.folders else None
        summary = audit_run(args.run_dir, source_root=project_root, folders=folders,
                            use_flash=not args.no_flash, max_workers=args.max_workers)
        logging.info("覆盖审计处置：%s｜缺口路由 %s", summary["dispositions"], summary["gap_routes"])
        logging.info("账本 %s｜排除裁决 %s｜缺口目录 %d 个", summary["ledger_csv"],
                     summary["decisions_csv"], len(summary["gap_folders"]))
    elif args.mode == "demo":
        run_demo_pipeline()
    else:
        # 根据参数决定是否启用校准检查
        enable_calibration_check = not args.disable_calibration_check
        
        if enable_calibration_check:
            print("校准检查已启用（基于Sr/Ca的JCp-1标准）")
        else:
            print("校准检查已通过命令行参数禁用")
        
        run_moa_pipeline(enable_calibration_check=enable_calibration_check, review_decisions=args.review_decisions,
                         use_parallel=args.parallel, max_workers=args.max_workers,
                         review_max_papers=args.review_max_papers, review_rules_only=args.review_rules_only,
                         auto_discard=args.auto_discard)

if __name__ == "__main__":
    main()
