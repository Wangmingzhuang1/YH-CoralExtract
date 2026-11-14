"""Figure 4 旭日图 + 分辨率统计分布图
移植自 FIG4.py（旭日图）与 reulst.py（统计分布四联图），绘图逻辑保持不变。
"""
import logging
import os

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import seaborn as sns

from . import style
from .style import G_COLORS, RES_COLORS, save_figure
from .loader import add_series_identity, filter_analysis_data, load_all_data, load_resolution_csv

logger = logging.getLogger(__name__)


# ======================== Figure 4: 嵌套饼图法旭日图 ========================
def draw_sunburst(ax, df, compact=False):
    """按时间分辨率和质控后的指标组合绘制同一套组成环。"""
    counts = df.groupby(["Resolution", "G_Grade"]).size().sort_index()
    inner = counts.groupby(level=0).sum()
    width = 0.32
    ax.pie(counts.values, radius=1, startangle=90, counterclock=False,
           colors=[G_COLORS.get(grade, "#CCCCCC") for _, grade in counts.index],
           wedgeprops={"width": width, "edgecolor": "white"})
    ax.pie(inner.values, radius=1 - width, startangle=90, counterclock=False,
           colors=[RES_COLORS.get(res, "#CCCCCC") for res in inner.index],
           wedgeprops={"width": width, "edgecolor": "white"})
    ax.text(0, 0, f"{len(df):,}\nobservations", ha="center", va="center",
            fontsize=6 if compact else 10)
    ax.set_title("Resolution and Indicator Composition", fontsize=9 if compact else 14)
    resolutions = [plt.Line2D([], [], marker="s", linestyle="none",
                              color=RES_COLORS.get(res, "#CCCCCC"), label=f"{res} ({count:,})")
                   for res, count in inner.items()]
    grades = [plt.Line2D([], [], marker="s", linestyle="none",
                        color=G_COLORS.get(grade, "#CCCCCC"),
                        label=f"{'Other (Ba/Ca/SST)' if grade == 'Other' else grade} ({count:,})")
              for grade, count in counts.groupby(level=1).sum().items()]
    ax.legend(handles=[*resolutions, *grades], title="Inner: resolution / Outer: group",
              loc="center left", bbox_to_anchor=(1.0, 0.5), fontsize=5 if compact else 8,
              title_fontsize=5 if compact else 8, frameon=False)


def plot_sunburst_chart(df):
    if df.empty:
        return None
    fig, ax = plt.subplots(figsize=(9, 7))
    draw_sunburst(ax, df)
    return fig


# ======================== 统计分布图（原 reulst.py） ========================
def load_and_prep_data(run_dir):
    """加载四个分辨率汇总 CSV 并统一列名/分辨率标签（原 reulst.load_and_prep_data）"""
    res_map = {
        '月尺度': 'Monthly',
        '年尺度': 'Annual',
        '半年尺度': 'Biannual',
        '双月尺度': 'Bimonthly',
        '不均匀': 'Uneven'
    }

    df_list = []
    for resolution in ('monthly', 'annual', 'biannual', 'bimonthly'):
        df = load_resolution_csv(run_dir, resolution, rename=False, year_range=None)
        if df is None:
            logger.warning(f"Error loading {resolution}_data.csv")
            continue
        try:
            # 映射分辨率
            if 'time_resolution' in df.columns:
                df['Resolution'] = df['time_resolution'].map(res_map).fillna(df['time_resolution'])

            # 确保年份是数值
            df['year_CE'] = pd.to_numeric(df['year_CE'], errors='coerce')

            # 确保经纬度是数值
            df['geo_latitude'] = pd.to_numeric(df['geo_latitude'], errors='coerce')
            df['geo_longitude'] = pd.to_numeric(df['geo_longitude'], errors='coerce')

            # 统一指标列名为标准格式
            rename_dict = {
                'Sr/Ca (mmol/mol)': 'Sr/Ca',
                'δ18O (‰ VPDB)': 'd18O',
                'δ13C (‰)': 'd13C',
                'SST (°C)': 'SST'
            }
            for col in list(df.columns):
                for key in rename_dict:
                    if key in col:
                        df.rename(columns={col: rename_dict[key]}, inplace=True)

            df_list.append(df)
        except Exception as e:
            logger.warning(f"Error loading {resolution}_data.csv: {e}")

    if not df_list:
        return pd.DataFrame()
    combined = pd.concat(df_list, ignore_index=True)
    # 与主图共用逐指标质控和年代范围。
    return filter_analysis_data(combined, year_col='year_CE', context='（统计分布图）')


def plot_stats_global_distribution(df):
    """(A) 全球分布散点 + (B) 纬度分布直方图"""
    fig3 = plt.figure(figsize=(12, 6))
    gs = gridspec.GridSpec(1, 2, width_ratios=[3, 1])

    ax_map = plt.subplot(gs[0])
    ax_map.set_title('(A) Global Distribution of Extracted Coral Records', loc='left', fontweight='bold')
    ax_map.set_xlabel('Longitude')
    ax_map.set_ylabel('Latitude')
    ax_map.set_xlim(-180, 180)
    ax_map.set_ylim(*style.LAT_RANGE)  # 聚焦热带
    ax_map.grid(True, linestyle='--', alpha=0.5)

    site_groups = df.groupby(['geo_siteName', 'Resolution']).agg({
        'geo_longitude': 'mean',
        'geo_latitude': 'mean',
        'year_CE': 'count'  # 用数据量表示大小
    }).reset_index()

    sns.scatterplot(
        data=site_groups,
        x='geo_longitude',
        y='geo_latitude',
        hue='Resolution',
        size='year_CE',
        sizes=(20, 200),
        alpha=0.7,
        palette='viridis',
        ax=ax_map
    )
    ax_map.legend(bbox_to_anchor=(0.5, -0.15), loc='upper center', ncol=4, frameon=False)

    # 纬度分布直方图
    ax_hist = plt.subplot(gs[1])
    sns.histplot(data=site_groups, y='geo_latitude', hue='Resolution', multiple="stack", palette='viridis', ax=ax_hist)
    ax_hist.set_title('(B) Latitudinal Distribution', loc='left', fontweight='bold')
    ax_hist.set_ylim(*style.LAT_RANGE)
    ax_hist.set_xlabel('Number of Sites')
    ax_hist.axhline(0, color='k', linestyle='--', alpha=0.3)  # 赤道

    plt.tight_layout()
    return fig3


def plot_stats_temporal_density(df):
    """时间可用性堆叠面积图（近年窗口：1900 - style.YEAR_MAX）"""
    fig, ax = plt.subplots(figsize=(10, 5))
    temporal_counts = df.groupby(['year_CE', 'Resolution']).size().unstack(fill_value=0)
    # 限制年份范围 (去掉异常值)；上界随主数据范围 style.YEAR_MAX 变化
    temporal_counts = temporal_counts.loc[1900:style.YEAR_MAX]

    # 显式传入 ax，确保面积图绘制在返回的 fig 上（此前 pandas 另建画布导致返回空白图）
    temporal_counts.plot(kind='area', stacked=True, colormap='viridis', alpha=0.8, ax=ax)
    ax.set_title(f'Temporal Density of Recovered Proxy Records (1900-{style.YEAR_MAX} CE)', loc='left', fontweight='bold')
    ax.set_xlabel('Year (CE)')
    ax.set_ylabel('Number of Data Points')
    ax.grid(True, linestyle='--', alpha=0.3)
    ax.legend(title='Resolution')
    fig.tight_layout()
    return fig


def plot_stats_representative_timeseries(df):
    """代表性站点 Sr/Ca 与 δ18O 双轴时间序列"""
    if not {"Sr/Ca", "d18O"} <= set(df.columns):
        return None
    paired = df.dropna(subset=["Sr/Ca", "d18O"]).copy()
    if paired.empty:
        return None
    add_series_identity(paired)
    series_key = paired["entity_id"].value_counts().idxmax()
    subset = paired.loc[paired["entity_id"].eq(series_key)].sort_values("year_CE").tail(100)
    top_site = subset["geo_siteName"].iloc[0]

    fig5, ax1 = plt.subplots(figsize=(10, 4))
    ax1.set_title(f'Representative Time-Series: {top_site} ({subset["Resolution"].iloc[0]})', loc='left', fontweight='bold')

    color1 = 'tab:red'
    ax1.set_xlabel('Year (CE)')
    ax1.set_ylabel('Sr/Ca (mmol/mol)', color=color1)
    ax1.plot(subset['year_CE'], subset['Sr/Ca'], color=color1, marker='o', markersize=3, linestyle='-', linewidth=1, label='Sr/Ca')
    ax1.tick_params(axis='y', labelcolor=color1)
    ax1.grid(True, linestyle='--', alpha=0.3)

    # 双Y轴展示 d18O
    ax2 = ax1.twinx()
    color2 = 'tab:blue'
    ax2.set_ylabel('δ18O (‰ VPDB)', color=color2)
    ax2.plot(subset['year_CE'], subset['d18O'], color=color2, marker='x', markersize=3, linestyle='--', linewidth=1, label='δ18O')
    ax2.tick_params(axis='y', labelcolor=color2)

    plt.tight_layout()
    return fig5


def plot_stats_proxy_correlation(df):
    """Sr/Ca 与 δ18O 全局相关性 hexbin 联合分布图"""
    corr_data = df.dropna(subset=['Sr/Ca', 'd18O'])
    if corr_data.empty:
        return None

    g = sns.jointplot(
        data=corr_data,
        x='Sr/Ca',
        y='d18O',
        kind="hex",
        color="#4CB391",
        height=6,
        ratio=5
    )
    g.fig.suptitle('Global Proxy Correlation: Sr/Ca vs δ18O', fontweight='bold', y=1.02)
    g.set_axis_labels('Sr/Ca (mmol/mol)', 'δ18O (‰ VPDB)')
    return g.fig


def run_figure_4(run_dir, out_dir):
    """生成补充图 Figure S1 旭日图（与 Figure 2b 同源，单图放大版供附件）"""
    data_df = load_all_data(run_dir)
    if data_df.empty:
        logger.warning("No data found or INDEX.csv missing, Figure 4 skipped.")
        return []
    logger.info(f"Loaded {len(data_df)} records for Figure 4.")
    fig = plot_sunburst_chart(data_df)
    if fig is None:
        return []
    save_figure(fig, os.path.join(out_dir, 'FigureS1_Sunburst_Corrected'))
    logger.info("Figure S1 (Sunburst) saved.")
    return ['FigureS1_Sunburst_Corrected']


def run_stats_figures(run_dir, out_dir):
    """生成原 reulst.py 的四张统计分布图"""
    produced = []
    df = load_and_prep_data(run_dir)
    if df.empty:
        logger.warning("统计分布图：无可用分辨率汇总数据，跳过。")
        return produced

    jobs = [
        ('Stats_Global_Distribution', plot_stats_global_distribution),
        ('Stats_Temporal_Density', plot_stats_temporal_density),
        ('Stats_Representative_Timeseries', plot_stats_representative_timeseries),
        ('Stats_Proxy_Correlation', plot_stats_proxy_correlation),
    ]
    for name, func in jobs:
        try:
            fig = func(df)
        except Exception as e:
            logger.warning(f"统计分布图 {name} 生成失败（源数据列缺失？）: {e}")
            plt.close('all')
            continue
        if fig is not None:
            save_figure(fig, os.path.join(out_dir, name))
            produced.append(name)
    return produced
