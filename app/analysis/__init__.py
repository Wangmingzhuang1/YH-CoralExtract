"""论文分析阶段模块：基于 run 目录生成 Figure1-15 等图表，与提取流水线解耦。

入口：run_analysis(run_dir, figures=None, external_data_dir=None, ranges=None)
figures 为 None 表示全部图；可传 ["1", "4"] 等子集（"stats" 为分辨率统计分布图，
"7"/"8" 为工作流示意与数据账目流程，"9"/"10" 为方差分解与局地可复现性，
"11"-"15" 为数据集特色块与完整性：亚月/Ba/Ca/分析方法/前器测/元数据披露）。
ranges 为 None 时按数据 year_CE 分位数自适应；也可传 dict 显式覆盖（见 style.set_ranges）。
"""
import logging
import os
from pathlib import Path

ROOT_PARENT = Path(__file__).resolve().parents[2]  # 项目根目录

from .style import set_pub_style

logger = logging.getLogger(__name__)

ALL_FIGURES = ['1', '2', '3', '4', '5', '6', '7', '8', '9', '10', '11', '12', '13', '14', '15', 'stats']

__all__ = ['run_analysis', 'ALL_FIGURES']


def _detect_external_data_dir(run_dir):
    """run_dir 内缺少 netCDF 格点数据时，回退探测同级旧 run 目录（含 sst.mnmean.nc 等）。

    返回可用于 external_data_dir 的目录；run_dir 内已有格点数据或无回退目录时返回 None。
    """
    from .loader import SST_NC_NAME, SSS_NC_NAME, FALLBACK_EXTERNAL_DIRNAME
    if (os.path.exists(os.path.join(run_dir, SST_NC_NAME))
            and os.path.exists(os.path.join(run_dir, SSS_NC_NAME))):
        return None
    fallback = os.path.join(os.path.dirname(run_dir), FALLBACK_EXTERNAL_DIRNAME)
    if fallback != run_dir and os.path.isdir(fallback):
        logger.info("run_dir 内无格点数据，external_data_dir 回退探测到: %s", fallback)
        return fallback
    return None


def run_analysis(run_dir, figures=None, external_data_dir=None, ranges=None):
    """在 run_dir 上运行论文分析，输出图保存到 run_dir/figures/（pdf+png+eps）

    Args:
        run_dir: 提取流水线产出的 run 目录（含 INDEX.csv、各分辨率子目录）
        figures: None=全部；或形如 ["1", "4"] 的图号子集，"stats" 代表统计分布图
        external_data_dir: 外部格点数据目录（sst.mnmean.nc 等）；None 时自动探测
            run_dir 内，找不到再回退到同级 run_20251224_161554，仍找不到则相关子图跳过
        ranges: None=按数据 year_CE 分位数自适应；或传 dict 显式覆盖 style.set_ranges
            支持的键（year_min/year_max/long_year_min/long_year_max/std_year_min/
            std_year_max/coretop_year_min/coretop_year_max/lat_range/core_lat_range/
            qc_keep_flags）
    Returns:
        生成的图名列表（不含扩展名）
    """
    run_dir = os.path.abspath(run_dir)
    if not os.path.isdir(run_dir):
        raise FileNotFoundError(f"run 目录不存在: {run_dir}")

    if figures is None:
        selected = list(ALL_FIGURES)
    else:
        selected = [str(f).strip() for f in figures if str(f).strip()]
        unknown = [f for f in selected if f not in ALL_FIGURES]
        if unknown:
            raise ValueError(f"未知图号: {unknown}，可选: {ALL_FIGURES}")

    from . import figures_spatial, figures_structure, figures_validation, style, loader

    # 范围参数：先按 year_CE 分位数设定数据自适应默认，再用显式 ranges 覆盖对应键
    adaptive = loader.compute_adaptive_ranges(run_dir)
    if adaptive:
        style.set_ranges(**adaptive)
        logger.info("数据自适应范围（基于 year_CE 分位数）: %s", adaptive)
    if ranges:
        style.set_ranges(**ranges)
        logger.info("应用显式指定范围覆盖: %s", ranges)
    logger.info("当前生效范围: %s", style.get_ranges())

    if external_data_dir is None:
        external_data_dir = _detect_external_data_dir(run_dir)

    out_dir = os.path.join(run_dir, 'figures')
    os.makedirs(out_dir, exist_ok=True)
    set_pub_style()

    produced = []

    # Figure 1-3：空间分布 / 数据结构 / 验证
    if any(f in selected for f in ('1', '2', '3')):
        produced += figures_spatial.run_figures_1_3(run_dir, out_dir, selected)

    # Figure 4 使用本批次汇总表，S1 保留组成旭日图。
    if '4' in selected:
        from .coral_comparison import run_comparison

        baseline = ROOT_PARENT / "output" / "coral_comparison" / "CoralHydro2k1_0_1.zip"
        name = "Figure4_GoldenSet_Validation"
        run_comparison(run_dir, baseline, out_dir, name, source_root=ROOT_PARENT)
        produced.append(name)
        produced += figures_structure.run_figure_4(run_dir, out_dir)

    # 统计分布图（原 reulst.py）
    if 'stats' in selected:
        produced += figures_structure.run_stats_figures(run_dir, out_dir)

    # Figure 5：代理-环境相关性
    if '5' in selected:
        produced += figures_validation.run_figure_5(run_dir, out_dir, external_data_dir)

    # Figure 6：季节性验证
    if '6' in selected:
        produced += figures_validation.run_figure_6(run_dir, out_dir, external_data_dir)

    # Figure 9-10：方差分解与局地可复现性（对齐 Walter2023 Fig 5/7，无需外部格点数据）
    if '9' in selected:
        produced += figures_validation.run_figure_9(run_dir, out_dir)
    if '10' in selected:
        produced += figures_validation.run_figure_10(run_dir, out_dir)

    # Figure 11-15：数据集特色块与完整性（亚月/Ba/Ca/方法/前器测/元数据披露）
    if any(f in selected for f in ('11', '12', '13', '14', '15')):
        from . import figures_dataset
        produced += figures_dataset.run_figures_11_15(run_dir, out_dir, selected, external_data_dir)

    # Figure 7-8：工作流示意与数据账目流程
    if any(f in selected for f in ('7', '8')):
        from . import figures_workflow
        produced += figures_workflow.run_figures_7_8(run_dir, out_dir, selected)

    logger.info("分析完成，共生成 %d 组图: %s", len(produced), produced)
    return produced
