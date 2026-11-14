"""数据集特色块与完整性图（Figure 11-15）。

相对 CoralHydro2k（月/双月 + Sr/Ca/δ18O）的取景框，本模块补齐本库自有特色块：
- Figure 11 亚月高分辨率（LA-ICP-MS）验证/展示；
- Figure 12 Ba/Ca 覆盖/质量对等面板；
- Figure 13 分析方法组成（testing_method）；
- Figure 14 前器测（pre-1850）覆盖/内部质量；
- Figure 15 元数据完整性/披露（年代轴填充率、分辨率→图件映射）。
覆盖/质量面板与 figures_validation 共用同一实现（plot_proxy_coverage_quality 等），不重复造轮子。
"""
import logging
import os

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.colors import ListedColormap

from .style import save_figure
from .loader import (SST_NC_NAME, load_resolution_csv, load_all_data, load_consolidated_raw,
                     filter_analysis_data, add_series_identity, _add_calendar_month,
                     resolve_grid_file, open_grid_dataset)
from .figures_validation import (compute_site_seasonal_correlation, find_best_site,
                                 plot_proxy_lat_time_density, plot_proxy_resolution_composition,
                                 PROXY_LABEL, PROXY_UNIT)

logger = logging.getLogger(__name__)

PRE_INSTRUMENTAL_YEAR = 1850   # 器测 SST 场（ERSSTv5）起点，之前为前器测段
METHOD_ORDER = ["LA-ICP-MS", "IRMS", "ICP-MS", "ICP-AES", "Mixed", "Other", "Unknown"]
RESOLUTION_ORDER = ["submonthly", "monthly", "bimonthly", "seasonal", "biannual",
                    "annual", "irregular", "discrete_samples", "unknown"]


def _load_prepared(run_dir, resolution):
    """加载某分辨率汇总并做值级质控 + 序列标识 + 日历月还原。"""
    raw = load_resolution_csv(run_dir, resolution)
    if raw is None:
        return pd.DataFrame()
    frame = filter_analysis_data(raw, context=f'（数据集图 {resolution}）')
    if frame is None or frame.empty:
        return pd.DataFrame()
    add_series_identity(frame)
    if 'Year' in frame:
        _add_calendar_month(frame)
    return frame


def _method_short(value):
    """把 testing_method 长串归并为可读的分析方法类别。"""
    s = str(value)
    if not s or s == 'nan':
        return 'Unknown'
    has_la = 'laser ablation' in s.lower()
    has_irms = 'isotope ratio mass spectrometry' in s.lower()
    has_icpms = 'inductively-coupled plasma mass spectrometry' in s.lower()
    has_aes = 'atomic emission' in s.lower()
    if has_la:
        return 'LA-ICP-MS'
    if '|' in s or (has_irms and (has_icpms or has_aes)) or (has_icpms and has_aes):
        return 'Mixed'
    if has_irms:
        return 'IRMS'
    if has_icpms:
        return 'ICP-MS'
    if has_aes:
        return 'ICP-AES'
    return 'Other'


def _proxy_counts(frame, proxies=('Sr/Ca', 'd18O', 'd13C', 'Ba/Ca')):
    return {p: int(frame[p].notna().sum()) for p in proxies if p in frame}


def _short_site(name, limit=42):
    """站点标签去掉 URI/括号尾巴并截断，保证图题可读。"""
    s = str(name).split(' (URI')[0].split(' (http')[0].strip()
    return s if len(s) <= limit else s[:limit - 1] + '…'


# ======================== Figure 11: 亚月高分辨率 ========================
def plot_figure11(sub_df, sst_ds):
    """亚月（LA-ICP-MS）采样间隔、指标构成、示例剖面与聚合后季节循环验证。"""
    fig = plt.figure(figsize=(14, 10))
    gs = fig.add_gridspec(2, 2, hspace=0.38, wspace=0.26,
                          left=0.07, right=0.95, top=0.93, bottom=0.09)

    # (a) 原生采样间隔分布（天，对数轴）
    ax = fig.add_subplot(gs[0, 0])
    ni = pd.to_numeric(sub_df.get('native_interval_years'), errors='coerce') * 365.25
    ni = ni.dropna()
    ni = ni[ni > 0]
    if not ni.empty:
        ax.hist(ni, bins=40, color='#4E79A7', edgecolor='white', linewidth=0.4)
        ax.set_xscale('log')
        ax.axvline(ni.median(), color='#cb181d', linestyle='--', linewidth=1)
        ax.set_xlabel('Native sampling interval (days, log scale)', fontsize=9)
        ax.set_ylabel('Observations', fontsize=9)
        ax.set_title(f'(a) Submonthly interval (median {ni.median():.1f} d, n={len(ni):,})',
                     loc='left', fontsize=10)
    else:
        ax.text(0.5, 0.5, 'No native_interval_years', transform=ax.transAxes, ha='center')

    # (b) 亚月指标构成
    ax = fig.add_subplot(gs[0, 1])
    counts = _proxy_counts(sub_df)
    names = [PROXY_LABEL.get(p, p) for p in counts]
    vals = list(counts.values())
    ax.bar(names, vals, color='#4E79A7', edgecolor='black', linewidth=0.4)
    for i, v in enumerate(vals):
        ax.text(i, v, f'{v:,}', ha='center', va='bottom', fontsize=8)
    any_proxy = sub_df[list(counts)].notna().any(axis=1) if counts else pd.Series(False, index=sub_df.index)
    n_ent = sub_df.loc[any_proxy, 'entity_id'].nunique()
    n_site = sub_df.loc[any_proxy, 'Site'].nunique() if 'Site' in sub_df else 0
    ax.set_ylabel('Observations', fontsize=9)
    ax.set_title(f'(b) Submonthly proxy composition ({n_ent} series / {n_site} sites)',
                 loc='left', fontsize=10)

    # (c) 示例亚月 Sr/Ca 剖面 + 月均值叠加（展示分辨季节/亚季节结构）
    ax = fig.add_subplot(gs[1, 0])
    work = sub_df.rename(columns={'Sr/Ca': 'SrCa'})
    site = find_best_site(work, 'SrCa', min_years=3)
    if site is not None:
        series = work.loc[work['entity_id'] == site].dropna(subset=['SrCa']).sort_values('Year')
        end = series['Year'].max()
        window = series[series['Year'] >= end - 3]
        ax.plot(window['Year'], window['SrCa'], color='#9ecae1', linewidth=0.6,
                label='Submonthly (raw)')
        monthly = window.groupby(['Year_int', 'Month'])['SrCa'].mean().reset_index()
        monthly['dec'] = monthly['Year_int'] + (monthly['Month'] - 1) / 12
        ax.plot(monthly['dec'], monthly['SrCa'], color='#08519c', linewidth=1.4,
                marker='o', markersize=3, label='Monthly mean')
        site_name = _short_site(series['Site'].iloc[0]) if 'Site' in series else str(site)
        ax.set_xlabel('Year (CE)', fontsize=9)
        ax.set_ylabel('Sr/Ca (mmol/mol)', fontsize=9)
        ax.set_title(f'(c) {site_name}: submonthly vs monthly (last 3 yr)', loc='left', fontsize=10)
        ax.legend(fontsize=8, frameon=False)
    else:
        ax.text(0.5, 0.5, 'No submonthly Sr/Ca', transform=ax.transAxes, ha='center')

    # (d) 亚月聚合到月后与 SST 季节循环相关（物理验证）
    ax = fig.add_subplot(gs[1, 1])
    if sst_ds is not None and 'Sr/Ca' in sub_df:
        agg = (sub_df.dropna(subset=['Sr/Ca'])
               .groupby(['entity_id', 'Year_int', 'Month'])
               .agg(SrCa=('Sr/Ca', 'mean'), lat=('lat', 'first'), lon=('lon', 'first'))
               .reset_index())
        agg['Year'] = agg['Year_int'] + (agg['Month'] - 1) / 12
        corr = compute_site_seasonal_correlation(agg, 'SrCa', sst_ds)
        if not corr.empty:
            ax.hist(corr['r'], bins=20, color='#4E79A7', edgecolor='white', linewidth=0.4)
            ax.axvline(corr['r'].mean(), color='#cb181d', linestyle='--', linewidth=1)
            ax.set_xlabel('Seasonal-cycle r vs SST', fontsize=9)
            ax.set_ylabel('Series', fontsize=9)
            ax.set_title(f'(d) Submonthly→monthly seasonal r (n={len(corr)}, mean {corr["r"].mean():.2f})',
                         loc='left', fontsize=10)
        else:
            ax.text(0.5, 0.5, 'No eligible aggregated series', transform=ax.transAxes, ha='center')
    else:
        ax.text(0.5, 0.5, 'SST grid unavailable', transform=ax.transAxes, ha='center')

    fig.text(0.5, 0.02, 'Submonthly (LA-ICP-MS) records are excluded from the monthly/annual Figures 5, 6, 9 and 10; '
             'their sampling density, composition and physical realism are validated here.',
             ha='center', fontsize=8)
    return fig


# ======================== Figure 12: Ba/Ca 覆盖/质量 ========================
def plot_figure12(frames, spatial_frame):
    """Ba/Ca 全时空覆盖/质量面板：密度、分辨率构成、值分布、亚月示例剖面。"""
    fig = plt.figure(figsize=(14, 10))
    gs = fig.add_gridspec(2, 2, hspace=0.40, wspace=0.26,
                          left=0.07, right=0.95, top=0.93, bottom=0.09)

    ax = fig.add_subplot(gs[0, 0])
    plot_proxy_lat_time_density(fig, ax, spatial_frame, 'Ba/Ca', '(a) Ba/Ca lat–time density')

    ax = fig.add_subplot(gs[0, 1])
    plot_proxy_resolution_composition(ax, frames, 'Ba/Ca', '(b) Resolution',
                                      identity_frame=frames.get('submonthly'))

    # (c) Ba/Ca 值分布（物理合理域）
    ax = fig.add_subplot(gs[1, 0])
    vals = pd.concat([f['Ba/Ca'].dropna() for f in frames.values() if 'Ba/Ca' in f])
    if not vals.empty:
        cap = 20.0  # 物理合理域上界；超出者为离群/单位待确认值，单独披露不压扁主分布
        bulk, tail = vals[vals <= cap], vals[vals > cap]
        ax.hist(bulk, bins=40, range=(0, cap), color='#4E79A7', edgecolor='white', linewidth=0.4)
        ax.axvline(vals.median(), color='#cb181d', linestyle='--', linewidth=1)
        ax.set_xlim(0, cap)
        ax.set_xlabel('Ba/Ca (μmol/mol)', fontsize=9)
        ax.set_ylabel('Observations', fontsize=9)
        ax.set_title(f'(c) Ba/Ca distribution (median {vals.median():.2f}, p95 {vals.quantile(0.95):.2f})',
                     loc='left', fontsize=10)
        ax.text(0.98, 0.95, f'{len(tail):,} obs ({100 * len(tail) / len(vals):.1f}%) > {cap:.0f}\n'
                f'(max {vals.max():,.0f}; pass QC + unit-verified,\nsource check pending)', transform=ax.transAxes,
                ha='right', va='top', fontsize=8, color='#555555')
    else:
        ax.text(0.5, 0.5, 'No Ba/Ca values', transform=ax.transAxes, ha='center')

    # (d) 亚月 Ba/Ca 示例剖面（事件尖峰 = 生产力/上升流/径流）
    ax = fig.add_subplot(gs[1, 1])
    sub = frames.get('submonthly')
    if sub is not None and 'Ba/Ca' in sub and sub['Ba/Ca'].notna().any():
        counts = sub.dropna(subset=['Ba/Ca']).groupby('entity_id').size()
        site = counts.idxmax()
        series = sub.loc[sub['entity_id'] == site].dropna(subset=['Ba/Ca']).sort_values('Year')
        end = series['Year'].max()
        window = series[series['Year'] >= end - 3]
        ax.plot(window['Year'], window['Ba/Ca'], color='#6a51a3', linewidth=0.7)
        site_name = _short_site(series['Site'].iloc[0]) if 'Site' in series else str(site)
        ax.set_xlabel('Year (CE)', fontsize=9)
        ax.set_ylabel('Ba/Ca (μmol/mol)', fontsize=9)
        ax.set_title(f'(d) {site_name}: submonthly Ba/Ca spikes (last 3 yr)', loc='left', fontsize=10)
    else:
        ax.text(0.5, 0.5, 'No submonthly Ba/Ca', transform=ax.transAxes, ha='center')

    fig.text(0.5, 0.02, 'Ba/Ca is a productivity/upwelling proxy, not a clean SST/SSS proxy: it is deliberately excluded '
             'from Figure 5 (no global gridded Ba/Ca field) and from the band-pass variance of Figure 9 (event-driven '
             'spikes); its coverage and internal quality are shown here.', ha='center', fontsize=8)
    return fig


# ======================== Figure 13: 分析方法组成 ========================
def plot_figure13(all_df, *, method_colors=None, drop_share=False):
    """testing_method × 分辨率 / 指标 组成 + 亚月采样间隔按方法。

    drop_share=True 时省略"方法份额"面板（其为方法×分辨率的行边际，重复展示），
    间隔面板顺位为 (c)，整图改 1×3 布局。
    """
    df = all_df.copy()
    df['method'] = df['testing_method'].map(_method_short)
    res_col = 'Resolution' if 'Resolution' in df else 'time_resolution'
    df['res'] = df[res_col].astype(str).str.lower().str.replace(' ', '_', regex=False)
    if drop_share:
        fig = plt.figure(figsize=(14, 4.9))
        gs = fig.add_gridspec(1, 3, wspace=0.30, width_ratios=[1, 1, 1.2],
                              left=0.07, right=0.95, top=0.88, bottom=0.24)
        cell_a, cell_b, cell_interval = gs[0, 0], gs[0, 1], gs[0, 2]
        interval_letter = 'c'
    else:
        fig = plt.figure(figsize=(14, 10))
        gs = fig.add_gridspec(2, 2, hspace=0.40, wspace=0.26,
                              left=0.08, right=0.95, top=0.93, bottom=0.09)
        cell_a, cell_b, cell_interval = gs[0, 0], gs[0, 1], gs[1, 1]
        interval_letter = 'd'
    methods = [m for m in METHOD_ORDER if m in set(df['method'])]
    cmap = plt.cm.tab10(np.linspace(0, 1, max(1, len(methods))))
    mcolor = dict(zip(methods, cmap)) if method_colors is None else method_colors

    # (a) method × resolution 堆叠
    ax = fig.add_subplot(cell_a)
    res_order = [r for r in RESOLUTION_ORDER if r in set(df['res'])]
    ct = pd.crosstab(df['res'], df['method']).reindex(index=res_order, columns=methods, fill_value=0)
    bottom = np.zeros(len(ct))
    for m in methods:
        ax.bar(ct.index, ct[m], bottom=bottom, label=m, color=mcolor[m], edgecolor='white', linewidth=0.3)
        bottom += ct[m].to_numpy()
    ax.set_ylabel('Observations', fontsize=9)
    ax.set_title('(a) Analytical method × resolution', loc='left', fontsize=10)
    ax.tick_params(axis='x', rotation=45, labelsize=8)
    ax.legend(fontsize=7, frameon=False, ncol=2)

    # (b) method × proxy 堆叠
    ax = fig.add_subplot(cell_b)
    proxies = [('Sr/Ca (mmol/mol)', 'Sr/Ca'), ('δ18O (‰ VPDB)', 'δ¹⁸O'),
               ('δ13C (‰)', 'δ¹³C'), ('Ba/Ca (μmol/mol)', 'Ba/Ca')]
    proxies = [(c, l) for c, l in proxies if c in df]
    bottom = np.zeros(len(proxies))
    for m in methods:
        vals = [int(df.loc[df['method'] == m, c].notna().sum()) for c, _ in proxies]
        ax.bar([l for _, l in proxies], vals, bottom=bottom, label=m, color=mcolor[m],
               edgecolor='white', linewidth=0.3)
        bottom += np.array(vals)
    ax.set_ylabel('Observations', fontsize=9)
    ax.set_title('(b) Analytical method × proxy', loc='left', fontsize=10)
    ax.legend(fontsize=7, frameon=False, ncol=2)

    # (c) 方法份额（方法×分辨率的行边际；drop_share 时省略）
    if not drop_share:
        ax = fig.add_subplot(gs[1, 0])
        share = df['method'].value_counts().reindex(methods).dropna()
        pct = 100 * share / share.sum()
        ax.barh(share.index[::-1], share.to_numpy()[::-1],
                color=[mcolor[m] for m in share.index[::-1]], edgecolor='black', linewidth=0.4)
        ax.set_xlim(0, share.max() * 1.35)
        for i, (m, v) in enumerate(zip(share.index[::-1], share.to_numpy()[::-1])):
            ax.text(v, i, f' {v:,} ({pct[m]:.1f}%)', va='center', fontsize=8)
        ax.set_xlabel('Observations', fontsize=9)
        ax.set_title('(c) Method share of dated observations', loc='left', fontsize=10)

    # 亚月采样间隔按方法（箱线，对数轴）
    ax = fig.add_subplot(cell_interval)
    sub = df[df['res'] == 'submonthly'].copy()
    sub['days'] = pd.to_numeric(sub['native_interval_years'], errors='coerce') * 365.25
    groups = [sub.loc[sub['method'] == m, 'days'].dropna() for m in methods]
    groups = [(m, g) for m, g in zip(methods, groups) if len(g) > 0]
    if groups:
        bp = ax.boxplot([g.to_numpy() for _, g in groups], labels=[m for m, _ in groups],
                        showfliers=False, patch_artist=True)
        for patch, (m, _) in zip(bp['boxes'], groups):
            patch.set_facecolor(mcolor[m])
        if method_colors is not None:
            for median in bp['medians']:
                median.set_color('#28373E')
        ax.set_yscale('log')
        ax.set_ylabel('Native interval (days, log)', fontsize=9)
        ax.set_title(f'({interval_letter}) Submonthly sampling interval by method', loc='left',
                     fontsize=9 if drop_share else 10)
        ax.tick_params(axis='x', rotation=30, labelsize=8)
    else:
        ax.text(0.5, 0.5, 'No submonthly interval data', transform=ax.transAxes, ha='center')

    fill = 100 * df['testing_method'].notna().mean()
    sub_la = int(((df['res'] == 'submonthly') & (df['method'] == 'LA-ICP-MS')).sum())
    sub_n = int((df['res'] == 'submonthly').sum())
    fig.text(0.5, 0.02, f'testing_method filled for {fill:.1f}% of quality-controlled observations; '
             f'LA-ICP-MS dominates the submonthly block ({sub_la:,}/{sub_n:,} = {100*sub_la/max(1,sub_n):.1f}%), '
             'IRMS the monthly δ18O/δ13C block.', ha='center', fontsize=8)
    return fig


# ======================== Figure 14: 前器测覆盖/内部质量 ========================
def plot_figure14(all_df):
    """pre-1850 覆盖与内部质量：时间-指标堆叠、分辨率、跨度分布、站点空间reach。"""
    year = pd.to_numeric(all_df['year_CE'], errors='coerce')
    pre = all_df.loc[year < PRE_INSTRUMENTAL_YEAR].copy()
    pre['year'] = year.loc[pre.index]
    add_series_identity(pre)
    res_col = 'Resolution' if 'Resolution' in pre else 'time_resolution'
    pre['res'] = pre[res_col].astype(str).str.lower().str.replace(' ', '_', regex=False)
    fig = plt.figure(figsize=(14, 10))
    gs = fig.add_gridspec(2, 2, hspace=0.40, wspace=0.26,
                          left=0.07, right=0.95, top=0.93, bottom=0.09)

    # (a) 时间 × 指标 堆叠（250 年箱）
    ax = fig.add_subplot(gs[0, 0])
    bins = np.arange(np.floor(pre['year'].min() / 250) * 250, 1851, 250)
    proxies = [('Sr/Ca (mmol/mol)', 'Sr/Ca', '#4E79A7'), ('δ18O (‰ VPDB)', 'δ¹⁸O', '#F28E2B'),
               ('δ13C (‰)', 'δ¹³C', '#6a51a3'), ('Ba/Ca (μmol/mol)', 'Ba/Ca', '#59A14F')]
    proxies = [(c, l, col) for c, l, col in proxies if c in pre]
    bottom = np.zeros(len(bins) - 1)
    centers = (bins[:-1] + bins[1:]) / 2
    for c, l, col in proxies:
        hist, _ = np.histogram(pre.loc[pre[c].notna(), 'year'], bins=bins)
        ax.bar(centers, hist, width=np.diff(bins) * 0.9, bottom=bottom, label=l, color=col,
               edgecolor='white', linewidth=0.3)
        bottom += hist
    ax.set_xlabel('Year (CE)', fontsize=9)
    ax.set_ylabel('Observations / 250 yr', fontsize=9)
    ax.set_title(f'(a) Pre-1850 observations by proxy (n={len(pre):,})', loc='left', fontsize=10)
    ax.legend(fontsize=8, frameon=False)

    # (b) 分辨率构成
    ax = fig.add_subplot(gs[0, 1])
    rc = pre['res'].value_counts()
    rc = rc.reindex([r for r in RESOLUTION_ORDER if r in rc.index]).dropna()
    ax.bar(rc.index, rc.to_numpy(), color='#4E79A7', edgecolor='black', linewidth=0.4)
    for i, v in enumerate(rc.to_numpy()):
        ax.text(i, v, f'{v:,}', ha='center', va='bottom', fontsize=7)
    ax.set_ylabel('Observations', fontsize=9)
    ax.set_title('(b) Pre-1850 resolution composition', loc='left', fontsize=10)
    ax.tick_params(axis='x', rotation=45, labelsize=8)

    # (c) 序列跨度分布
    ax = fig.add_subplot(gs[1, 0])
    span = pre.groupby('entity_id')['year'].agg(lambda s: s.max() - s.min())
    span = span[span > 0]
    if not span.empty:
        cap = 300.0  # 主体跨度域；超长序列（化石/拼接）单独披露，避免压扁主分布
        bulk, tail = span[span <= cap], span[span > cap]
        ax.hist(bulk, bins=30, range=(0, cap), color='#4E79A7', edgecolor='white', linewidth=0.4)
        ax.axvline(30, color='#cb181d', linestyle='--', linewidth=1)
        ax.axvline(100, color='#cb181d', linestyle=':', linewidth=1)
        ax.set_xlim(0, cap)
        ax.set_xlabel('Record span (yr)', fontsize=9)
        ax.set_ylabel('Series', fontsize=9)
        ax.set_title(f'(c) Pre-1850 span (≥30 yr: {(span >= 30).sum()}, ≥100 yr: {(span >= 100).sum()})',
                     loc='left', fontsize=10)
        ax.text(0.98, 0.95, f'{len(tail)} series > {cap:.0f} yr\n(max {span.max():,.0f} yr)',
                transform=ax.transAxes, ha='right', va='top', fontsize=8, color='#555555')
    else:
        ax.text(0.5, 0.5, 'No pre-1850 series', transform=ax.transAxes, ha='center')

    # (d) 站点空间 reach（点大小=跨度）
    ax = fig.add_subplot(gs[1, 1])
    sites = pre.groupby('entity_id').agg(lat=('geo_latitude', 'first'), lon=('geo_longitude', 'first'),
                                         span=('year', lambda s: s.max() - s.min()))
    sites = sites.dropna(subset=['lat', 'lon'])
    if not sites.empty:
        sc = ax.scatter(sites['lon'], sites['lat'], s=20 + 2 * sites['span'].clip(0, 200),
                        c=sites['span'], cmap='YlOrRd', edgecolors='black', linewidth=0.4)
        cb = fig.colorbar(sc, ax=ax, pad=0.02)
        cb.set_label('Span (yr)', fontsize=8)
        ax.set_xlabel('Longitude (°)', fontsize=9)
        ax.set_ylabel('Latitude (°)', fontsize=9)
        ax.set_xlim(-180, 180)
        ax.set_ylim(-45, 45)
        ax.grid(alpha=0.3)
        ax.set_title(f'(d) Pre-1850 sites ({len(sites)} series)', loc='left', fontsize=10)
    else:
        ax.text(0.5, 0.5, 'No pre-1850 coordinates', transform=ax.transAxes, ha='center')

    frac = 100 * len(pre) / max(1, len(all_df))
    fig.text(0.5, 0.02, f'Pre-instrumental (<1850 CE) observations: {len(pre):,} ({frac:.1f}% of the quality-controlled '
             'archive). No instrumental counterpart exists for this window; its quality is carried by the internal '
             'variance (Fig. 9) and local reproducibility (Fig. 10) analyses, not by Fig. 5/6 correlations.',
             ha='center', fontsize=8)
    return fig


# ======================== Figure 15: 元数据完整性/披露 ========================
def plot_figure15(raw_df, eligibility=None, disclosure=None, *, metadata_colors=None):
    """关键元数据填充率 + 年代方法构成 + 分辨率→图件映射 + 披露文字。

    eligibility=(列标签列表, {分辨率: 0/1 列表}) 与 disclosure 文本可按目标图件集覆盖，
    默认保持原 Fig1–15 编号口径。
    """
    n = max(1, len(raw_df))
    fig = plt.figure(figsize=(14, 10))
    gs = fig.add_gridspec(2, 2, hspace=0.42, wspace=0.30,
                          left=0.16, right=0.95, top=0.93, bottom=0.09)

    # (a) 填充率
    ax = fig.add_subplot(gs[0, 0])
    fields = [('paleoData_dating_method', 'Dating method'),
              ('paleoData_dating_error', 'Dating error'),
              ('testing_method', 'Analytical method'),
              ('native_interval_years', 'Native interval'),
              ('geo_latitude', 'Coordinate'),
              ('Sr/Ca_value_unit_status', 'Sr/Ca unit status'),
              ('δ18O_value_unit_status', 'δ18O unit status')]
    labels, rates = [], []
    for col, lab in fields:
        if col in raw_df:
            labels.append(lab)
            rates.append(100 * raw_df[col].notna().mean())
    order = np.argsort(rates)
    fill_color, dating_color = metadata_colors or ('#4E79A7', '#F28E2B')
    bar_colors = fill_color
    if metadata_colors is not None:
        bar_colors = [dating_color if labels[i] in {'Dating method', 'Dating error'} else fill_color for i in order]
    ax.barh([labels[i] for i in order], [rates[i] for i in order],
            color=bar_colors, edgecolor='black', linewidth=0.4)
    for i, idx in enumerate(order):
        ax.text(rates[idx], i, f' {rates[idx]:.1f}%', va='center', fontsize=8)
    ax.set_xlim(0, 105)
    ax.set_xlabel('Fill rate (%)', fontsize=9)
    ax.set_title(f'(a) Metadata fill rate (raw archive, n={n:,})', loc='left', fontsize=10)

    # (b) 年代方法构成（仅有值者）
    ax = fig.add_subplot(gs[0, 1])
    dm = raw_df['paleoData_dating_method'].dropna().astype(str).str.strip()
    dm = dm[dm != '']
    dm = (dm.str.replace(' and annual band counting', ' + band', regex=False)
          .str.replace('U-Th dating + band', 'U-Th + band', regex=False))
    if not dm.empty:
        vc = dm.value_counts().head(6)
        ax.barh(vc.index[::-1], vc.to_numpy()[::-1], color=dating_color, edgecolor='black', linewidth=0.4)
        for i, v in enumerate(vc.to_numpy()[::-1]):
            ax.text(v, i, f' {v:,}', va='center', fontsize=8)
        ax.set_xlabel('Observations', fontsize=9)
        ax.set_title(f'(b) Dating method (filled {len(dm):,} = {100*len(dm)/n:.1f}%)', loc='left', fontsize=10)
        ax.tick_params(axis='y', labelsize=8)
    else:
        ax.text(0.5, 0.5, 'No dating metadata', transform=ax.transAxes, ha='center')

    # (c) 分辨率 → 图件 映射矩阵
    ax = fig.add_subplot(gs[1, 0])
    if eligibility is None:
        figs = ['Fig5/6', 'Fig9', 'Fig10', 'Fig11', 'Fig12', 'Fig14']
        use = {'submonthly': [0, 0, 0, 1, 1, 1], 'monthly': [1, 1, 1, 0, 1, 1],
               'bimonthly': [0, 0, 0, 0, 0, 1], 'annual': [1, 0, 1, 0, 1, 1],
               'seasonal': [0, 0, 0, 0, 0, 1], 'biannual': [0, 0, 0, 0, 0, 1],
               'irregular': [0, 0, 0, 0, 0, 1], 'discrete_samples': [0, 0, 0, 0, 0, 1],
               'unknown': [0, 0, 0, 0, 0, 1]}
    else:
        figs, use = eligibility
    res_order = [r for r in RESOLUTION_ORDER if r in use]
    mat = np.array([use[r] for r in res_order])
    matrix_palette = 'Blues' if metadata_colors is None else ListedColormap(['#F2F3F1', fill_color])
    ax.imshow(mat, cmap=matrix_palette, vmin=0, vmax=1, aspect='auto')
    ax.set_xticks(range(len(figs)))
    ax.set_xticklabels(figs, fontsize=8)
    ax.set_yticks(range(len(res_order)))
    ax.set_yticklabels(res_order, fontsize=8)
    for i in range(len(res_order)):
        for j in range(len(figs)):
            # 用字体安全的 Y/– 代替 ✓（Times New Roman 无 U+2713，会渲染成缺字方框）
            ax.text(j, i, 'Y' if mat[i, j] else '–', ha='center', va='center',
                    fontsize=9, color='white' if mat[i, j] else ('#7A8589' if metadata_colors else '#bbbbbb'))
    ax.set_title('(c) Which resolutions enter which figures', loc='left', fontsize=10)

    # (d) 披露文字
    ax = fig.add_subplot(gs[1, 1])
    ax.axis('off')
    dm_fill = 100 * raw_df['paleoData_dating_method'].notna().mean() if 'paleoData_dating_method' in raw_df else 0
    text = (
        'Disclosure / limitations\n\n'
        f'• Chronological control is sparse: dating method filled for only {dm_fill:.1f}% of rows '
        '(U/Th + annual band counting dominant). Age-model uncertainty is therefore NOT figured; '
        'it is a dataset limitation, not a plotting choice.\n\n'
        '• Figures 9/10 use monthly/annual only: the band-pass variance design needs a regular '
        'monthly grid, and bimonthly/irregular/seasonal series would require interpolation that '
        'aliases the seasonal band.\n\n'
        '• Submonthly data are excluded from Figs 5/6/9/10 but validated in Fig 11; Ba/Ca coverage '
        'and quality are in Fig 12 (excluded from Fig 5/9 for proxy-physics reasons).\n\n'
        '• Pre-1850 quality is carried by internal analyses (Figs 9/10/14), never by instrumental '
        'correlation (Figs 5/6).'
    )
    if disclosure is not None:
        text = disclosure
    ax.text(0.0, 0.98, text, transform=ax.transAxes, va='top', ha='left', fontsize=8.5,
            family='serif', linespacing=1.4)
    return fig


# ======================== 入口 ========================
def run_figures_11_15(run_dir, out_dir, selected, external_data_dir=None):
    produced = []
    need_all = any(f in selected for f in ('13', '14'))
    all_df = load_all_data(run_dir) if need_all else None

    if '11' in selected:
        sub = _load_prepared(run_dir, 'submonthly')
        if sub.empty:
            logger.warning('Figure 11：未读取到 submonthly 数据，跳过。')
        else:
            sst_path = resolve_grid_file(run_dir, SST_NC_NAME, external_data_dir)
            sst_ds = open_grid_dataset(sst_path)
            fig = plot_figure11(sub, sst_ds)
            save_figure(fig, os.path.join(out_dir, 'Figure11_Submonthly_HighResolution'))
            produced.append('Figure11_Submonthly_HighResolution')

    if '12' in selected:
        frames, sparts = {}, []
        for res in ('submonthly', 'monthly', 'annual'):
            raw = load_resolution_csv(run_dir, res)
            if raw is None:
                continue
            qc = filter_analysis_data(raw, context=f'（图12 {res}）')
            if qc is not None and not qc.empty:
                add_series_identity(qc)
                if 'Year' in qc:
                    _add_calendar_month(qc)
                frames[res] = qc
            sp = filter_analysis_data(raw, require_coordinates=True, context=f'（图12 {res} 坐标）')
            if sp is not None and not sp.empty and 'Ba/Ca' in sp:
                sp = sp.dropna(subset=['Ba/Ca'])
                if not sp.empty:
                    sparts.append(sp)
        spatial = pd.concat(sparts, ignore_index=True) if sparts else pd.DataFrame()
        if frames:
            fig = plot_figure12(frames, spatial)
            save_figure(fig, os.path.join(out_dir, 'Figure12_BaCa_Coverage_Quality'))
            produced.append('Figure12_BaCa_Coverage_Quality')
        else:
            logger.warning('Figure 12：未读取到 Ba/Ca 数据，跳过。')

    if '13' in selected and all_df is not None and not all_df.empty:
        fig = plot_figure13(all_df)
        save_figure(fig, os.path.join(out_dir, 'Figure13_Analytical_Methods'))
        produced.append('Figure13_Analytical_Methods')

    if '14' in selected and all_df is not None and not all_df.empty:
        fig = plot_figure14(all_df)
        save_figure(fig, os.path.join(out_dir, 'Figure14_PreInstrumental'))
        produced.append('Figure14_PreInstrumental')

    if '15' in selected:
        raw = load_consolidated_raw(run_dir)
        if raw.empty:
            logger.warning('Figure 15：未读取到 consolidated 数据，跳过。')
        else:
            fig = plot_figure15(raw)
            save_figure(fig, os.path.join(out_dir, 'Figure15_Metadata_Completeness'))
            produced.append('Figure15_Metadata_Completeness')

    return produced
