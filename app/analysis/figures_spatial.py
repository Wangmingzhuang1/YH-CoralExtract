"""Figure 1-3：空间分布/垂直剖面、数据结构、验证图
移植自 comprehensive_data_analysis.py，绘图逻辑保持不变。
"""
import logging
import os

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import matplotlib.ticker as ticker
from matplotlib.colors import LogNorm

from . import style
from .style import (HAS_CARTOPY, ccrs, cfeature, PALETTE, GROUP_ORDER, GROUP_COLORS, GROUP_DISPLAY,
                    G_COLORS, BASIN_COLORS, PROXY_COLORS,
                    add_panel_label, save_figure)
from .loader import add_series_identity, load_figure1_data, process_data
from .figures_structure import draw_sunburst

logger = logging.getLogger(__name__)


def aggregate_sites(df, split_other=False):
    """以来源序列为单位统计跨度与末次观测年代。

    split_other=True 时把无三核心指标的序列按 Ba/Ca / SST 显式归类，不再汇总为 Other。
    """
    records = df.loc[df.get("record_type", pd.Series("", index=df.index)).ne("discrete_sample")].copy()
    add_series_identity(records)
    # 兼容原始列名与短名，确保 Ba/Ca / SST 显式归类不被空列掩盖。
    records = records.rename(columns={"Ba/Ca (μmol/mol)": "Ba/Ca", "SST (°C)": "SST"})
    for column in ("Sr/Ca", "d18O", "d13C", "Ba/Ca", "SST"):
        if column not in records:
            records[column] = np.nan
    records["has_srca"] = records["Sr/Ca"].notna()
    records["has_o18"] = records["d18O"].notna()
    records["has_c13"] = records["d13C"].notna()
    records["has_ba"] = records["Ba/Ca"].notna()
    records["has_sst"] = records["SST"].notna()
    grouped = records.groupby("entity_id").agg(
        Lat=("Lat", "mean"), Lon=("Lon", "mean"), Detailed_Basin=("Basin", "first"),
        Min_Year=("Year", "min"), Max_Year=("Year", "max"), Years=("Year", "nunique"),
        has_srca=("has_srca", "any"), has_o18=("has_o18", "any"), has_c13=("has_c13", "any"),
        has_ba=("has_ba", "any"), has_sst=("has_sst", "any"),
    ).reset_index()
    labels = {
        (True, False, False): "Sr/Ca-only", (False, True, False): "δ18O-only",
        (False, False, True): "δ13C-only", (True, True, False): "Sr/Ca + δ18O",
        (True, False, True): "Sr/Ca + δ13C", (False, True, True): "δ18O + δ13C",
        (True, True, True): "Three indicators",
    }

    def proxy_type(values):
        key = values[:3]
        if key in labels:
            return labels[key]
        if not split_other:
            return "Other"
        ba, sst = values[3], values[4]
        if ba and sst:
            return "Ba/Ca + SST"
        if ba:
            return "Ba/Ca"
        if sst:
            return "SST"
        return "Other"

    indicators = grouped[["has_srca", "has_o18", "has_c13", "has_ba", "has_sst"]]
    grouped["Proxy_Type"] = [proxy_type(values) for values in indicators.itertuples(index=False, name=None)]
    return grouped.loc[grouped["Years"].ge(2)].copy()


# ======================== Figure 1: 垂直布局 (一列三行) ========================
def plot_figure_1_optimized(df, split_other=False):
    """三联覆盖图。时间轴保留无坐标观测，纬度密度只使用已通过检查的点坐标。

    split_other=True 时无三核心指标的观测按 Ba/Ca-only / SST-only 显式着色与标记，
    不再使用 Other 汇总类。
    """
    if not HAS_CARTOPY or df.empty:
        return None
    dated = df.loc[df['Dated']]
    if dated.empty:
        return None
    exact = dated.loc[dated['Exact location']]
    regional = dated.loc[dated['Regional location']]
    flagged = df.loc[~df['Dated']]
    older = flagged.loc[flagged['Year'].lt(style.STD_YEAR_MIN), 'Year']

    fig = plt.figure(figsize=(7.2, 9.4))
    gs = fig.add_gridspec(4, 2, width_ratios=[1, 1.5], height_ratios=[1.45, 0.87, 1, 0.045],
                          hspace=0.4, wspace=0.13, left=0.11, right=0.97, top=0.97, bottom=0.115)

    # (a) 全球分布，区域代表点单独编码。
    ax_map = fig.add_subplot(gs[0, :], projection=ccrs.Robinson(central_longitude=180))
    ax_map.set_global()
    ax_map.add_feature(cfeature.LAND, facecolor=PALETTE['land'], edgecolor='none', zorder=0)
    ax_map.coastlines(linewidth=0.4, color=PALETTE['border'], zorder=1)
    gl = ax_map.gridlines(draw_labels=True, linewidth=0.3, color='gray', alpha=0.4, linestyle=':')
    gl.top_labels = gl.right_labels = False
    gl.xlabel_style = gl.ylabel_style = {'size': 7, 'color': 'black'}
    gl.xlocator = ticker.FixedLocator(np.arange(-180, 181, 60))
    gl.ylocator = ticker.FixedLocator(np.arange(-60, 61, 30))

    if split_other:
        groups = [*GROUP_ORDER, 'Ba/Ca only', 'SST only', 'Ba/Ca + SST']
        colors = {**GROUP_COLORS, 'Ba/Ca only': '#59A14F', 'SST only': '#B07AA1',
                  'Ba/Ca + SST': '#8C6D46'}
        markers = ['o', 's', '^', 'D', 'v', '<', '>', 'P', 'X', 'h']
    else:
        groups = [*GROUP_ORDER, 'Other']
        colors = {**GROUP_COLORS, 'Other': '#999999'}
        markers = ['o', 's', '^', 'D', 'v', '<', '>', 'P']
    sites = exact[['Lon', 'Lat', 'Group']].drop_duplicates()
    for group, marker in zip(groups, markers):
        subset = sites.loc[sites['Group'].eq(group)]
        if dated['Group'].eq(group).any():
            ax_map.scatter(subset['Lon'], subset['Lat'], color=colors[group], marker=marker,
                           s=24, edgecolor='white', linewidth=0.35, transform=ccrs.PlateCarree(),
                           label=GROUP_DISPLAY.get(group, group), zorder=3)
    if not regional.empty:
        points = regional[['geo_rep_longitude', 'geo_rep_latitude']].drop_duplicates()
        ax_map.scatter(points['geo_rep_longitude'], points['geo_rep_latitude'],
                       facecolors='none', edgecolors='black', marker='D', s=30,
                       transform=ccrs.PlateCarree(), label='Regional composite', zorder=4)
    ax_map.legend(loc='upper center', bbox_to_anchor=(0.5, -0.061),
                  ncol=10 if split_other else 8,
                  fontsize=6 if split_other else 7, handletextpad=0.05, columnspacing=0.65)
    add_panel_label(ax_map, '(a)', x=-0.03, y=1.02)

    # (b, c) 分界年（默认 1600 CE = style.YEAR_MIN）只进入右侧。两个时窗独立分箱，密度按同一时间和纬度单位显示。
    split = style.YEAR_MIN
    early_step, recent_step = 250, 10
    early_start = min(split - early_step, split - early_step * int(np.ceil((split - dated['Year'].min()) / early_step)))
    recent_end = max(split + recent_step, recent_step * int(np.ceil(dated['Year'].max() / recent_step)))
    year_edges = [np.arange(early_start, split + 1, early_step),
                  np.arange(split, recent_end + 1, recent_step)]
    before = dated['Year'].lt(split)
    windows = [dated.loc[before], dated.loc[~before]]
    lat_min = min(-30, 10 * np.floor(exact['Lat'].min() / 10)) if not exact.empty else -30
    lat_max = max(30, 10 * np.ceil(exact['Lat'].max() / 10)) if not exact.empty else 30
    lat_edges = np.arange(lat_min, lat_max + 1, 2)
    age_format = ticker.FuncFormatter(lambda year, _: f'{(1950 - year) / 1000:g}')
    densities = []
    shown_in_time = shown_in_space = 0
    time_axes = []
    density_ax = None
    for i, (rows, edges) in enumerate(zip(windows, year_edges)):
        ax_time = fig.add_subplot(gs[1, i])
        ax_dens = fig.add_subplot(gs[2, i], sharex=ax_time, sharey=density_ax)
        density_ax = ax_dens
        time_axes.append(ax_time)
        widths = np.diff(edges)
        bottom = np.zeros(len(widths), dtype=int)
        for group in groups:
            counts = np.histogram(rows.loc[rows['Group'].eq(group), 'Year'], bins=edges)[0]
            ax_time.bar(edges[:-1], counts, width=widths, bottom=bottom, align='edge',
                        color=colors[group], linewidth=0)
            bottom += counts
        shown_in_time += bottom.sum()
        ax_time.set_ylim(0, max(1, bottom.max() * 1.18))
        ax_time.yaxis.set_major_locator(ticker.MaxNLocator(nbins=4, integer=True))
        ax_time.yaxis.set_major_formatter(ticker.StrMethodFormatter('{x:,.0f}'))
        ax_time.set_title(f'{widths[0]}-year bins', fontweight='normal', fontsize=8)
        ax_time.tick_params(labelbottom=False, bottom=False, labelsize=7)
        ax_time.spines[['top', 'right']].set_visible(False)
        if i == 0:
            ax_time.set_ylabel('Number of observations', loc='center')
            ax_dens.set_ylabel('Latitude (°N)')
            ticks = 1950 - 2000 * np.arange(1, (1950 - early_start) // 2000 + 1)
            ax_dens.set_xticks([*sorted(ticks), split])
            ax_dens.xaxis.set_major_formatter(age_format)
            ax_dens.set_xlabel('Age (ka BP)')
            add_panel_label(ax_time, '(b)', x=-0.18, y=1.04)
            add_panel_label(ax_dens, '(c)', x=-0.18, y=1.04)
        else:
            ax_time.yaxis.tick_right()
            ax_time.spines['right'].set_visible(True)
            ax_time.spines['left'].set_visible(False)
            ax_dens.tick_params(labelleft=False)
            ax_dens.set_xticks([*np.arange(split, recent_end - 35, 100), recent_end])
            ax_dens.set_xlabel('Year (CE)')
        ax_dens.set_xlim(edges[0], edges[-1])
        ax_dens.set_ylim(lat_min, lat_max)
        ax_dens.yaxis.set_major_locator(ticker.MultipleLocator(15))
        ax_dens.tick_params(labelsize=7)
        located = rows.loc[rows['Exact location']]
        counts, _, _ = np.histogram2d(located['Year'], located['Lat'], bins=[edges, lat_edges])
        shown_in_space += int(counts.sum())
        densities.append((ax_dens, edges, np.ma.masked_equal((counts * (10 / widths[:, None])).T, 0)))

    if shown_in_time != len(dated) or shown_in_space != len(exact):
        raise ValueError("Figure 1 分箱前后观测数不一致")

    if not older.empty:
        # 旧年代保留归档分布，和通过年代检查的柱状图分开。
        ages = (1950 - older) / 1000
        first = np.floor(ages.min())
        bounds = np.arange(first, max(first + 1, np.ceil(ages.max())) + 1)
        ax_old = time_axes[0].inset_axes([0.14, 0.48, 0.57, 0.41])
        ax_old.hist(ages, bins=bounds, color='#949494', linewidth=0)
        ax_old.set_xlim(bounds[-1], bounds[0])
        ax_old.set_title('Older ages under review (1 kyr bins)', fontweight='normal', fontsize=7, pad=3)
        ax_old.set_xlabel('Age (ka BP)', fontsize=7, labelpad=1)
        ax_old.xaxis.set_major_locator(ticker.MaxNLocator(nbins=3, integer=True))
        ax_old.yaxis.set_major_locator(ticker.MaxNLocator(nbins=2, integer=True))
        ax_old.tick_params(labelsize=7, length=2)
        ax_old.spines[['top', 'right']].set_visible(False)

    norm = LogNorm(vmin=10 / early_step, vmax=max([10, *(float(d.max()) for _, _, d in densities if d.count())]))
    for ax_dens, edges, density in densities:
        mesh = ax_dens.pcolormesh(edges, lat_edges, density, cmap='Spectral_r',
                                  norm=norm, shading='flat', rasterized=True)
    ax_cbar = fig.add_subplot(gs[3, :])
    color_ticks = [t for t in (0.1, 1, 10, 100, 1000, 10000) if t <= norm.vmax]
    cbar = fig.colorbar(mesh, cax=ax_cbar, orientation='horizontal', ticks=color_ticks,
                        format=ticker.StrMethodFormatter('{x:g}'))
    cbar.set_label('Observations per 10 years per 2° latitude', labelpad=3)
    cbar.ax.tick_params(labelsize=7, length=2)

    no_location = len(dated) - len(exact) - len(regional)
    fig.text(0.11, 0.054, f'Archive rows: {len(df):,}. Shown in (b): {len(dated):,} dated rows, '
             f'plus {len(older):,} older rows in the inset. Other rows outside the analysis: {len(flagged) - len(older):,}.',
             fontsize=7)
    fig.text(0.11, 0.036, f'Dated rows in (c): {len(exact):,} with point locations. '
             f'Regional only: {len(regional):,}. No verified location: {no_location:,}.', fontsize=7)
    if split_other:
        fig.text(0.11, 0.018, 'Ba/Ca-only, SST-only and Ba/Ca + SST markers: rows without Sr/Ca, δ¹⁸O or δ¹³C '
                 'after value-level QC, shown as separate classes rather than pooled.', fontsize=7)
    else:
        fig.text(0.11, 0.018, 'Group "Other": rows without Sr/Ca, δ¹⁸O or δ¹³C after value-level QC '
                 '(Ba/Ca- or SST-only records).', fontsize=7)
    return fig


# ======================== Figure 2: 数据结构 ========================
def plot_figure_2_advanced(df, split_other=False):
    """
    Figure 2: Data Structure & Composition
    (a) Record Length Histogram
    (b) Sunburst Chart (Resolution -> G_Grade) - Based on FIG4.py
    (c) Basin-wise Core Top Dates (core-top window, default 1900-2025)
    (d) Proxy-wise Core Top Dates (core-top window, default 1900-2025)
    (e) Top Age Spatial Distribution
    split_other=True 时 (d) 以 Ba/Ca / SST 显式堆叠，不再使用 Other 汇总类。
    """
    if df.empty or not HAS_CARTOPY:
        return None

    # 聚合站点数据
    sites = aggregate_sites(df, split_other=split_other)

    # 设定画布：宽度7.2英寸，高度10英寸
    fig = plt.figure(figsize=(7.2, 10))

    # 使用更加细致的 GridSpec - 减少间距，地图往上移
    gs = gridspec.GridSpec(3, 2, height_ratios=[1, 1, 1.3],
                           left=0.08, right=0.92, top=0.96, bottom=0.10,
                           hspace=0.30, wspace=0.28)

    # --- (a) Record Length Histogram ---
    ax_len = fig.add_subplot(gs[0, 0])
    sites['Length'] = sites['Max_Year'] - sites['Min_Year']
    sites['Length'] = sites['Length'].clip(lower=1)

    bins = [0, 20, 50, 100, 200, np.inf]
    labels = ['<20', '20-50', '50-100', '100-200', '>200']
    sites['Len_Bin'] = pd.cut(sites['Length'], bins=bins, labels=labels)
    counts = sites['Len_Bin'].value_counts().sort_index()

    ax_len.bar(labels, counts, color='#4E79A7', alpha=0.85, edgecolor='black', linewidth=0.5)
    ax_len.set_ylabel('Number of Series', fontsize=8)
    ax_len.set_title('Record Duration Distribution', fontsize=9, pad=8)
    ax_len.tick_params(axis='x', rotation=0, labelsize=7)
    ax_len.spines['top'].set_visible(False)
    ax_len.spines['right'].set_visible(False)
    add_panel_label(ax_len, '(a)')

    # (b) 与补充图使用同一组成统计。
    ax_sun = fig.add_subplot(gs[0, 1])
    draw_sunburst(ax_sun, df, compact=True)
    add_panel_label(ax_sun, '(b)')

    # --- (c) Basin Core Top Dates ---
    ax_basin = fig.add_subplot(gs[1, 0])
    core_top_sites = sites[(sites['Max_Year'] >= style.CORETOP_YEAR_MIN) & (sites['Max_Year'] <= style.CORETOP_YEAR_MAX)]
    year_bins = np.arange(style.CORETOP_YEAR_MIN, style.CORETOP_YEAR_MAX + 1, 5)

    bottom = np.zeros(len(year_bins) - 1)
    for basin, color in BASIN_COLORS.items():
        subset = core_top_sites[core_top_sites['Detailed_Basin'] == basin]
        counts, _ = np.histogram(subset['Max_Year'], bins=year_bins)
        ax_basin.bar(year_bins[:-1], counts, width=4.5, bottom=bottom, align='edge',
                     color=color, alpha=0.9, linewidth=0.3, label=basin)
        bottom += counts

    ax_basin.set_xlim(style.CORETOP_YEAR_MIN, style.CORETOP_YEAR_MAX)
    ax_basin.set_ylabel('Number of Series', fontsize=8)
    ax_basin.set_title('Record End Years by Ocean Sector', fontsize=9, pad=8)
    ax_basin.tick_params(axis='x', rotation=45, labelsize=7)
    ax_basin.spines['top'].set_visible(False)
    ax_basin.spines['right'].set_visible(False)
    ax_basin.legend(loc='upper left', ncol=2, frameon=False, fontsize=6)
    add_panel_label(ax_basin, '(c)')

    # --- (d) Proxy Core Top Dates ---
    ax_proxy = fig.add_subplot(gs[1, 1])
    bottom = np.zeros(len(year_bins) - 1)
    for p_type, color in PROXY_COLORS.items():
        subset = core_top_sites[core_top_sites['Proxy_Type'] == p_type]
        if split_other and subset.empty:
            continue
        counts, _ = np.histogram(subset['Max_Year'], bins=year_bins)
        ax_proxy.bar(year_bins[:-1], counts, width=4.5, bottom=bottom, align='edge',
                     color=color, alpha=0.9, linewidth=0.3,
                     label='Other (Ba/Ca/SST)' if p_type == 'Other' else p_type)
        bottom += counts

    ax_proxy.set_xlim(style.CORETOP_YEAR_MIN, style.CORETOP_YEAR_MAX)
    ax_proxy.set_ylabel('Number of Series', fontsize=8)
    ax_proxy.set_title('Record End Years by Indicator', fontsize=9, pad=8)
    ax_proxy.tick_params(axis='x', rotation=45, labelsize=7)
    ax_proxy.spines['top'].set_visible(False)
    ax_proxy.spines['right'].set_visible(False)
    ax_proxy.legend(loc='upper left', ncol=1, frameon=False, fontsize=6)
    add_panel_label(ax_proxy, '(d)')

    # --- (e) Spatial Distribution of Top Age ---
    ax_map = fig.add_subplot(gs[2, :], projection=ccrs.Robinson(central_longitude=180))
    ax_map.set_global()
    ax_map.add_feature(cfeature.LAND, facecolor=PALETTE['land'], edgecolor='none', zorder=0)
    ax_map.coastlines(linewidth=0.3, color=PALETTE['border'], zorder=1)

    gl = ax_map.gridlines(draw_labels=True, linewidth=0.2, color='gray', alpha=0.3, linestyle=':')
    gl.top_labels = False
    gl.right_labels = False
    gl.xlabel_style = {'size': 6}
    gl.ylabel_style = {'size': 6}
    gl.xlocator = ticker.FixedLocator(np.arange(-180, 181, 60))
    gl.ylocator = ticker.FixedLocator(np.arange(-90, 91, 30))

    sc = ax_map.scatter(sites['Lon'], sites['Lat'], c=sites['Max_Year'], cmap='plasma',
                        s=25, edgecolor='black', linewidth=0.3, alpha=0.9,
                        transform=ccrs.PlateCarree(), vmin=style.CORETOP_YEAR_MIN, vmax=style.CORETOP_YEAR_MAX, zorder=3)

    ax_map.set_title('Global Distribution of Record End Years', fontsize=9, pad=10)

    # Colorbar 放置在地图下方（水平），不遮挡地图
    cbar_ax = fig.add_axes([0.25, 0.04, 0.5, 0.012])
    cbar = plt.colorbar(sc, cax=cbar_ax, orientation='horizontal')
    cbar.set_label('Record End Year (CE)', fontsize=7)
    cbar.ax.tick_params(labelsize=6)

    add_panel_label(ax_map, '(e)', x=0.0, y=1.03)

    return fig


# ======================== Figure 3: 验证图 ========================
def plot_figure_3_optimized(df):
    if df.empty:
        return None

    fig = plt.figure(figsize=(3.5, 7))
    gs = gridspec.GridSpec(3, 1, height_ratios=[1, 1, 0.8], hspace=0.4)

    # --- (a) ---
    ax1 = fig.add_subplot(gs[0])
    grade_counts = df['G_Grade'].value_counts()
    grades = [grade for grade in G_COLORS if grade_counts.get(grade, 0) or grade in ('G1', 'G2', 'G3', 'G4', 'G5', 'G6')]
    counts = [grade_counts.get(g, 0) for g in grades]

    # 使用统一的G_COLORS进行颜色映射；Other 加星号并在图底注明含义
    display = ['Other*' if g == 'Other' else g for g in grades]
    bars = ax1.bar(display, counts, color=[G_COLORS[g] for g in grades],
                   edgecolor='black', linewidth=0.4, alpha=0.9, width=0.6)

    ax1.set_ylabel('Count')
    ax1.spines['top'].set_visible(False)
    ax1.spines['right'].set_visible(False)

    for bar in bars:
        height = bar.get_height()
        ax1.text(bar.get_x() + bar.get_width() / 2., height + max(counts) * 0.02,
                 f'{height}', ha='center', va='bottom', fontsize=6)
    add_panel_label(ax1, '(a)')
    # --- (b) ---
    ax2 = fig.add_subplot(gs[1])
    vp = df.dropna(subset=['d18O', 'Sr/Ca'])
    if not vp.empty:
        # 使用统一的G_COLORS进行颜色映射
        ax2.scatter(vp['d18O'], vp['Sr/Ca'], c=vp['G_Grade'].map(G_COLORS),
                    s=10, alpha=0.6, edgecolors='none')

    paired_grades = sorted(set(vp["G_Grade"]))
    if paired_grades:
        handles = [plt.Line2D([], [], marker="o", linestyle="none", color=G_COLORS[grade], label=grade)
                   for grade in paired_grades]
        ax2.legend(handles=handles, loc="upper left", title="Indicator group")
    ax2.set_xlabel('δ¹⁸O (‰ VPDB)')
    ax2.set_ylabel('Sr/Ca (mmol/mol)')
    ax2.spines['top'].set_visible(False)
    ax2.spines['right'].set_visible(False)
    add_panel_label(ax2, '(b)')
    # --- (c) ---
    ax3 = fig.add_subplot(gs[2])
    yearly = df.groupby(np.floor(df["Year"]).astype(int)).size()
    yearly = yearly.reindex(range(int(yearly.index.min()), int(yearly.index.max()) + 1), fill_value=0)

    ax3.fill_between(yearly.index, 0, yearly.values, color='gray', alpha=0.2)
    ax3.plot(yearly.index, yearly.values, color='black', linewidth=0.8, alpha=0.8)

    if len(yearly) > 10:
        ma = yearly.rolling(window=10, center=True).mean()
        ax3.plot(ma.index, ma.values, color='#E15759', linewidth=1.2, label='10-yr MA')
        ax3.legend(loc='lower right')

    ax3.set_xlim(style.YEAR_MIN, style.YEAR_MAX)
    ax3.set_xlabel('Year (CE)')
    ax3.set_ylabel('# Records')
    ax3.spines['top'].set_visible(False)
    ax3.spines['right'].set_visible(False)

    # 全范围插图：主图聚焦 1600 CE 之后，插图以对数轴展示完整年代跨度
    ax_in = ax3.inset_axes([0.04, 0.50, 0.44, 0.44])
    ax_in.plot(yearly.index, yearly.clip(lower=0.5), color='black', linewidth=0.5)
    ax_in.set_yscale('log')
    ax_in.set_xlim(style.STD_YEAR_MIN, style.YEAR_MAX)
    ax_in.set_xticks([-10000, -5000, 0, 1600], ['-10k', '-5k', '0', '1600'])
    ax_in.tick_params(labelsize=5, length=2)
    ax_in.set_title('Full range (log)', fontsize=6, pad=2)
    ax_in.spines[['top', 'right']].set_visible(False)

    add_panel_label(ax3, '(c)')

    fig.text(0.5, 0.005, '*Other: no core proxy after QC (Ba/Ca- or SST-only rows)',
             ha='center', fontsize=5.5)
    return fig


def run_figures_1_3(run_dir, out_dir, figures):
    """生成 Figure1-3，保存到 out_dir"""
    produced = []
    df = process_data(run_dir) if any(fig in figures for fig in ('2', '3')) else None

    jobs = [
        ('1', 'Figure1_SpatialTemporal_Vertical', plot_figure_1_optimized),
        ('2', 'Figure2_DataStructure_Advanced', plot_figure_2_advanced),
        ('3', 'Figure3_Validation_Optimized', plot_figure_3_optimized),
    ]
    for fig_id, name, func in jobs:
        if fig_id not in figures:
            continue
        data = load_figure1_data(run_dir) if fig_id == '1' else df
        if data.empty:
            logger.warning("Figure %s 没有可绘制的数据", fig_id)
            continue
        if not HAS_CARTOPY and fig_id in ('1', '2'):
            logger.warning("未检测到 Cartopy，Figure %s 地图类图无法绘制，已跳过。", fig_id)
            continue
        fig = func(data)
        if fig is not None:
            save_figure(fig, os.path.join(out_dir, name))
            logger.info("Figure %s saved.", fig_id)
            produced.append(name)
    return produced
