"""Figure 5 的同期配对相关、Figure 6 的季节循环诊断、Figure 9 方差分解与 Figure 10 局地可复现性。"""
import logging
import os

import numpy as np
import pandas as pd
import xarray as xr
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.lines import Line2D
from mpl_toolkits.axes_grid1.inset_locator import inset_axes
from scipy.stats import pearsonr

from .style import HAS_CARTOPY, ccrs, cfeature, save_figure
from .loader import (SST_NC_NAME, SSS_NC_NAME,
                     load_user_summaries, load_monthly_data, load_annual_data,
                     resolve_grid_file, open_grid_dataset, filter_analysis_data)

logger = logging.getLogger(__name__)

# 设置相关性计算的阈值（最少需要多少个重叠数据点才计算）
MIN_OVERLAP_POINTS = 10
# 显著性水平：对齐 Walter et al. (2023) Fig 4 的 90% 置信（双侧 p < 0.10，名义显著性）
SIGNIFICANCE_ALPHA = 0.10

MONTH_LABELS = ['J', 'F', 'M', 'A', 'M', 'J', 'J', 'A', 'S', 'O', 'N', 'D']
DIVERGING_CMAP = LinearSegmentedColormap.from_list(
    'div_corr', ['#2c7bb6', '#abd9e9', '#ffffbf', '#fdae61', '#d7191c']
)


# ======================== Figure 5 核心计算逻辑 ========================
def extract_grid_val(ds, lat, lon, var_name='sst'):
    """从xarray数据集提取指定经纬度的时间序列"""
    try:
        # 使用 sel 和 nearest 方法查找最近网格点
        # 注意：不同nc文件的经度可能是0-360或-180-180，需自动适配
        if ds.coords['lon'].min() >= 0 and lon < 0:
            lon_query = lon + 360
        else:
            lon_query = lon

        point_data = ds.sel(lat=lat, lon=lon_query, method='nearest')

        # 转换为pandas Series, 索引为时间
        ts = point_data[var_name].to_series()
        return ts
    except Exception:
        return None


def _paired_environment(frame, proxy, grid_series, annual=False):
    """按年或月一对一配对，保留真实共同观测和相同值的重复副本。"""
    if grid_series is None or proxy not in frame:
        return pd.DataFrame(columns=["proxy", "env"])
    frequency = "Y" if annual else "M"
    years = np.floor(frame["Year"]).to_numpy(dtype=int)
    months = np.ones(len(frame), dtype=int) if annual else frame["Month"].to_numpy(dtype=int)
    time = pd.PeriodIndex.from_fields(year=years, month=months, freq="M").asfreq(frequency)
    coral = pd.DataFrame({"time": time, "proxy": pd.to_numeric(frame[proxy], errors="coerce")})
    coral = coral.loc[np.isfinite(coral["proxy"])].drop_duplicates()
    coral = coral.drop_duplicates("time", keep=False).set_index("time")
    environment = grid_series.loc[np.isfinite(grid_series)]
    environment = environment.groupby(environment.index.to_period("M")).mean()
    if annual:
        by_year = environment.groupby(environment.index.asfreq("Y"))
        environment = by_year.mean().loc[by_year.size().eq(12)]
    return coral.join(environment.rename("env"), how="inner").dropna().sort_index()


def calculate_correlations(user_dfs, sst_ds, sss_ds):
    """按独立来源序列计算有效配对上的描述性 Pearson r。"""
    tasks = (("Sr/Ca", "SST", sst_ds, "sst"), ("d18O", "SST", sst_ds, "sst"),
             ("d18O", "SSS", sss_ds, "salinity"))
    results, grid_cache = [], {}
    for resolution, frame in user_dfs.items():
        if frame is None or frame.empty:
            continue
        annual = resolution.casefold() == "annual"
        keys = "entity_id" if "entity_id" in frame else ["lat", "lon"]
        for identity, series in frame.groupby(keys):
            lat, lon = series.iloc[0][["lat", "lon"]]
            for proxy, target, grid, variable in tasks:
                if grid is None or proxy not in series or series[proxy].count() < MIN_OVERLAP_POINTS:
                    continue
                key = (target, lat, lon)
                if key not in grid_cache:
                    grid_cache[key] = extract_grid_val(grid, lat, lon, variable)
                paired = _paired_environment(series, proxy, grid_cache[key], annual)
                if len(paired) < MIN_OVERLAP_POINTS or paired.nunique().min() < 2:
                    continue
                r_value, p_value = pearsonr(paired["proxy"], paired["env"])
                results.append({"entity_id": identity, "lat": lat, "lon": lon, "proxy": proxy,
                                "env_target": target, "r": float(r_value), "p": float(p_value),
                                "resolution": "Annual" if annual else "Monthly", "n": len(paired),
                                "first_period": str(paired.index[0]), "last_period": str(paired.index[-1])})
    return pd.DataFrame(results)


def plot_correlation_maps(corr_df):
    """展示带符号的相关性；圆圈=90% 置信显著，菱形=不显著（对齐 Walter2023 Fig 4）。"""
    if corr_df.empty:
        return None
    fig, axes = plt.subplots(3, 2, figsize=(14, 10),
                             subplot_kw={"projection": ccrs.Robinson(central_longitude=180)})
    pairs = (("Sr/Ca", "SST", "Sr/Ca–SST"), ("d18O", "SST", "δ¹⁸O–SST"), ("d18O", "SSS", "δ¹⁸O–SSS"))
    for i, (proxy, env, label) in enumerate(pairs):
        for j, resolution in enumerate(("Monthly", "Annual")):
            ax = axes[i, j]
            subset = corr_df.loc[corr_df["proxy"].eq(proxy) & corr_df["env_target"].eq(env)
                                 & corr_df["resolution"].eq(resolution)]
            draw_instrumental_map(ax, subset)
            ax.set_title(f"({chr(97 + i * 2 + j)}) {resolution} {label} ({len(subset)} series)", loc="left")
    fig.subplots_adjust(bottom=0.16, hspace=0.30, wspace=0.15)
    cax = fig.add_axes([0.27, 0.105, 0.46, 0.014])
    sm = plt.cm.ScalarMappable(cmap=DIVERGING_CMAP, norm=plt.Normalize(-1, 1))
    fig.colorbar(sm, cax=cax, orientation="horizontal", label="Pearson r")
    handles = [Line2D([], [], marker="o", linestyle="", markerfacecolor="#bdbdbd",
                      markeredgecolor="black", markersize=6, label=f"Significant (p < {SIGNIFICANCE_ALPHA:.2f})"),
               Line2D([], [], marker="D", linestyle="", markerfacecolor="#bdbdbd",
                      markeredgecolor="black", markersize=6, label="Not significant")]
    fig.legend(handles=handles, loc="lower center", bbox_to_anchor=(0.5, 0.035), ncol=2, frameon=False)
    fig.text(0.5, 0.008, "Pearson r of paired observations (two-sided nominal significance, 90% confidence level). "
             "Monthly series retain seasonality. δ¹³C is deliberately excluded here: coral δ¹³C is not a clean "
             "hydroclimate proxy (metabolic and Suess-effect influences); its coverage and internal quality are "
             "shown in Figs. 6f, 9 and 10 instead.", ha="center", fontsize=7)
    return fig


def draw_instrumental_map(ax, data):
    _configure_correlation_map(ax)
    if data.empty:
        ax.text(.5, .5, "No eligible series", transform=ax.transAxes, ha="center")
        return
    significant = (data["p"].lt(SIGNIFICANCE_ALPHA) if "p" in data
                   else pd.Series(True, index=data.index))
    for mask, marker in ((significant, "o"), (~significant, "D")):
        part = data.loc[mask]
        if not part.empty:
            ax.scatter(part["lon"], part["lat"], c=part["r"], cmap=DIVERGING_CMAP,
                       vmin=-1, vmax=1, s=32, marker=marker, edgecolors="black", linewidth=0.4,
                       transform=ccrs.PlateCarree())


def plot_data_distribution_overview(user_data):
    """缺少网格数据时的回退图：按代理类型的数据分布概览"""
    overview_data = []
    for key, df in user_data.items():
        if df is not None and not df.empty and 'lat' in df.columns and 'lon' in df.columns:
            for idx, row in df.iterrows():
                if pd.notna(row.get('lat')) and pd.notna(row.get('lon')):
                    if 'Sr/Ca' in row and pd.notna(row['Sr/Ca']):
                        overview_data.append({
                            'lat': row['lat'],
                            'lon': row['lon'],
                            'proxy': 'Sr/Ca',
                            'resolution': row.get('Resolution', 'Unknown'),
                            'G_Grade': row.get('G_Grade', 'Unknown')
                        })
                    if 'd18O' in row and pd.notna(row['d18O']):
                        overview_data.append({
                            'lat': row['lat'],
                            'lon': row['lon'],
                            'proxy': 'd18O',
                            'resolution': row.get('Resolution', 'Unknown'),
                            'G_Grade': row.get('G_Grade', 'Unknown')
                        })
                    if 'd13C' in row and pd.notna(row['d13C']):
                        overview_data.append({
                            'lat': row['lat'],
                            'lon': row['lon'],
                            'proxy': 'd13C',
                            'resolution': row.get('Resolution', 'Unknown'),
                            'G_Grade': row.get('G_Grade', 'Unknown')
                        })

    if not overview_data:
        logger.warning("没有找到包含坐标和指标值的数据点")
        return None

    overview_df = pd.DataFrame(overview_data)
    logger.info(f"发现 {len(overview_df)} 个有坐标和指标值的数据点")
    logger.info("\n%s", overview_df.groupby(['proxy', 'resolution']).size())

    fig, ax = plt.subplots(figsize=(10, 6))

    proxies = overview_df['proxy'].unique()
    colors = ['red', 'blue', 'green', 'orange', 'purple', 'brown']

    for i, proxy in enumerate(proxies):
        proxy_data = overview_df[overview_df['proxy'] == proxy]
        if not proxy_data.empty:
            ax.scatter(proxy_data['lon'], proxy_data['lat'],
                       label=proxy, alpha=0.6, c=colors[i % len(colors)], s=30)

    ax.set_xlabel('Longitude')
    ax.set_ylabel('Latitude')
    ax.set_title('Data Distribution by Proxy Type')
    ax.legend()
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    return fig


def run_figure_5(run_dir, out_dir, external_data_dir=None):
    """Figure 5：代理-环境相关性地图；格点数据缺失时回退为数据分布概览图"""
    produced = []
    user_data = load_user_summaries(run_dir)
    if not user_data:
        logger.warning("Figure 5：未读取到 monthly/annual 汇总表，跳过。")
        return produced

    sst_path = resolve_grid_file(run_dir, SST_NC_NAME, external_data_dir)
    sss_path = resolve_grid_file(run_dir, SSS_NC_NAME, external_data_dir)
    if sst_path is None:
        logger.warning("找不到 %s，无法计算 SST 相关性（可经 external_data_dir 提供）。", SST_NC_NAME)
    if sss_path is None:
        logger.warning("找不到 %s，无法计算 SSS 相关性（可经 external_data_dir 提供）。", SSS_NC_NAME)

    sst_ds = open_grid_dataset(sst_path)
    sss_ds = open_grid_dataset(sss_path)

    if not HAS_CARTOPY:
        logger.warning("未检测到 Cartopy，Figure 5 相关性地图无法绘制，跳过。")
        return produced

    if sst_ds is not None or sss_ds is not None:
        corr_results = calculate_correlations(user_data, sst_ds, sss_ds)
        if corr_results.empty:
            logger.warning("没有足够的数据进行相关性计算，Figure 5 跳过。")
            return produced
        logger.info("\n%s", corr_results.groupby(['proxy', 'env_target'])['r'].mean())
        if "p" in corr_results:
            sig_rate = (corr_results.assign(significant=corr_results["p"].lt(SIGNIFICANCE_ALPHA))
                        .groupby(["proxy", "env_target", "resolution"])["significant"].mean())
            logger.info("显著比例 (p < %.2f):\n%s", SIGNIFICANCE_ALPHA, sig_rate)
        corr_results.to_csv(os.path.join(out_dir, 'correlation_results.csv'),
                            index=False, encoding='utf-8-sig')
        fig = plot_correlation_maps(corr_results)
        if fig is not None:
            save_figure(fig, os.path.join(out_dir, 'Figure5_Correlation_Analysis'))
            logger.info("绘图完成: Figure5_Correlation_Analysis.png/pdf/eps")
            produced.append('Figure5_Correlation_Analysis')
    else:
        # 如果没有网格数据，生成一个基于实际数据的可视化
        logger.warning("生成数据集质量概览图 (因为缺少网格数据)...")
        fig = plot_data_distribution_overview(user_data)
        if fig is not None:
            save_figure(fig, os.path.join(out_dir, 'Figure5_Data_Distribution_Overview'))
            logger.info("数据分布概览图已保存为 Figure5_Data_Distribution_Overview.png/pdf/eps")
            produced.append('Figure5_Data_Distribution_Overview')
    return produced


# ======================== Figure 6: SST提取与计算 ========================
def _seasonal_pairs(frame, proxy, sst_ds):
    if frame.empty or sst_ds is None:
        return None
    lat, lon = frame.iloc[0][["lat", "lon"]]
    paired = _paired_environment(frame, proxy, extract_grid_val(sst_ds, lat, lon))
    if len(paired) < 12 or paired.index.month.nunique() < 6 or paired.index.year.nunique() < 3:
        return None
    return paired


def _seasonal_statistics(paired):
    monthly = paired.groupby(paired.index.month)
    means = monthly.mean()
    error = monthly.sem()
    r = means["proxy"].corr(means["env"]) if means.nunique().min() >= 2 else np.nan
    return means.reindex(range(1, 13)), error.reindex(range(1, 13)), r


def compute_site_seasonal_correlation(df, var_name, sst_ds, min_records=12):
    """按共同观测期的月份气候态计算描述性相关性。"""
    if var_name not in df.columns:
        return pd.DataFrame()
    results = []
    group_key = "entity_id" if "entity_id" in df else "Site"
    for identity, series in df.groupby(group_key):
        if series[var_name].count() < min_records:
            continue
        paired = _seasonal_pairs(series, var_name, sst_ds)
        if paired is None:
            continue
        _, _, r = _seasonal_statistics(paired)
        if np.isfinite(r):
            lat, lon = series.iloc[0][["lat", "lon"]]
            results.append({"entity_id": identity, "lat": lat, "lon": lon, "r": r,
                            "n": len(paired), "months": paired.index.month.nunique()})
    return pd.DataFrame(results)


def find_best_site(df, var_name, min_years=5):
    """找到最适合展示的站点"""
    if var_name not in df.columns:
        return None
    valid_df = df[df[var_name].notna()]
    group_key = 'entity_id' if 'entity_id' in df else 'Site'
    site_stats = valid_df.groupby(group_key).agg({
        var_name: 'count', 'Year_int': ['nunique'], 'Month': 'nunique'
    })
    site_stats.columns = ['count', 'n_years', 'n_months']
    good = site_stats[(site_stats['n_years'] >= min_years) & (site_stats['n_months'] >= 10)]
    if len(good) == 0:
        good = site_stats[site_stats['count'] >= 36]
    return good['count'].idxmax() if len(good) > 0 else (site_stats['count'].idxmax() if len(site_stats) > 0 else None)


def calculate_anomaly(df, var_name, site_filter=None):
    """计算去季节化异常值"""
    if not site_filter or var_name not in df:
        return None
    work_df = df.copy()
    if site_filter:
        group_key = 'entity_id' if 'entity_id' in work_df else 'Site'
        work_df = work_df[work_df[group_key] == site_filter]
    work_df = work_df[work_df[var_name].notna()]
    if len(work_df) < 24:
        return None
    climatology = work_df.groupby('Month')[var_name].mean()
    work_df['Anomaly'] = work_df[var_name] - work_df['Month'].map(climatology)
    return work_df[['Year', 'Year_int', 'Month', var_name, 'Anomaly', 'Site']].copy()


# ======================== Figure 6(e): Niño3.4 定量验证 ========================
ENSO_MIN_N = 60          # 最少配对月数
ENSO_MIN_ABS_R = 0.5     # |r| 下限
ENSO_MAX_P = 1e-3        # p 上限（名义显著性）
NINO34_LAT = (-5.0, 5.0)
NINO34_LON = (-170.0, -120.0)
NINO34_BASE = (1981, 2010)


def compute_nino34(sst_ds):
    """ERSSTv5 在 5°S–5°N、170°W–120°W 的面积加权平均，减 1981–2010 月气候态。

    返回以 Period('M') 为索引的异常序列；无格点数据时返回 None。
    """
    if sst_ds is None or "sst" not in sst_ds:
        return None
    lon = ((sst_ds["lon"].to_numpy() + 180) % 360) - 180
    lat = sst_ds["lat"].to_numpy()
    region = sst_ds["sst"].isel(
        lat=np.where((lat >= NINO34_LAT[0]) & (lat <= NINO34_LAT[1]))[0],
        lon=np.where((lon >= NINO34_LON[0]) & (lon <= NINO34_LON[1]))[0])
    if region.sizes["lat"] == 0 or region.sizes["lon"] == 0:
        return None
    weights = xr.DataArray(np.cos(np.deg2rad(region["lat"].to_numpy())),
                           dims="lat", coords={"lat": region["lat"]})
    ts = ((region * weights).sum(("lat", "lon"))
          / (weights.sum() * region.sizes["lon"])).to_series()
    ts = ts.loc[np.isfinite(ts)]
    monthly = ts.groupby(ts.index.to_period("M")).mean()
    base = monthly.loc[(monthly.index.year >= NINO34_BASE[0]) & (monthly.index.year <= NINO34_BASE[1])]
    climatology = base.groupby(base.index.month).mean()
    anomaly = monthly - climatology.reindex(monthly.index.month).to_numpy()
    return anomaly.rename("nino34")


def _monthly_anomaly(series, proxy):
    """单条来源序列按全记录月气候态去季节化，返回按日历月去重平均的月异常序列。"""
    frame = series.loc[np.isfinite(series[proxy]), ["Year", "Month", proxy]]
    if frame.empty:
        return None
    periods = pd.PeriodIndex.from_fields(year=np.floor(frame["Year"]).astype(int),
                                         month=frame["Month"].astype(int), freq="M")
    values = pd.Series(frame[proxy].to_numpy(dtype=float), index=periods)
    values = values.groupby(level=0).mean().sort_index()
    climatology = values.groupby(values.index.month).mean()
    return values - climatology.reindex(values.index.month).to_numpy()


def select_enso_record(monthly_df, nino):
    """在 Niño3.4 区域内确定性挑选通过定量门槛的最长 δ18O 序列。

    候选池：|lat|≤5 且经度落在 170°W–120°W；门槛 n≥60、|r|≥0.5、p<1e-3；
    通过者取配对月数最多（而非 r 最大），避免按相关强度挑序列。
    返回 (entity_id, stats)；无合格序列时返回 (None, None)。
    """
    if nino is None or monthly_df is None or monthly_df.empty or "d18O" not in monthly_df:
        return None, None
    lon_norm = ((monthly_df["lon"] + 180) % 360) - 180
    pool = monthly_df.loc[monthly_df["lat"].abs().le(5) & lon_norm.between(*NINO34_LON)]
    keys = "entity_id" if "entity_id" in pool else ["lat", "lon"]
    candidates = []
    for identity, series in pool.groupby(keys):
        anomaly = _monthly_anomaly(series, "d18O")
        if anomaly is None or len(anomaly) < ENSO_MIN_N:
            continue
        paired = anomaly.to_frame("proxy").join(nino.to_frame("nino"), how="inner").dropna()
        if len(paired) < ENSO_MIN_N or paired.nunique().min() < 2:
            continue
        r_value, p_value = pearsonr(paired["proxy"], paired["nino"])
        if abs(r_value) >= ENSO_MIN_ABS_R and p_value < ENSO_MAX_P:
            candidates.append((len(paired), identity, r_value, p_value,
                               paired.index[0], paired.index[-1]))
    if not candidates:
        return None, None
    candidates.sort(key=lambda item: (-item[0], str(item[1])))
    n, identity, r_value, p_value, start, end = candidates[0]
    return identity, {"r": float(r_value), "p": float(p_value), "n": int(n),
                      "start": str(start), "end": str(end)}


# ======================== Figure 6 子图绑制 ========================
def _configure_correlation_map(ax):
    ax.set_global()
    ax.add_feature(cfeature.LAND, facecolor="#F5F5F5", edgecolor="gray", linewidth=0.3)
    ax.add_feature(cfeature.COASTLINE, linewidth=0.3, color="gray")
    gl = ax.gridlines(draw_labels=True, linewidth=0.3, color="gray", alpha=0.5, x_inline=False, y_inline=False)
    gl.top_labels = gl.right_labels = False
    gl.xlocator = mticker.FixedLocator([-120, -60, 0, 60, 120, 180])
    gl.ylocator = mticker.FixedLocator([-40, -20, 0, 20, 40])
    gl.xlabel_style = gl.ylabel_style = {"size": 6}


def plot_correlation_map(ax, corr_df, title, cmap):
    _configure_correlation_map(ax)
    ax.set_title(title, loc="left")
    if corr_df.empty:
        ax.text(0.5, 0.5, "No eligible series", transform=ax.transAxes, ha="center")
        return
    scatter = ax.scatter(corr_df["lon"], corr_df["lat"], c=corr_df["r"], cmap=cmap,
                         s=28, edgecolors="black", linewidth=0.4, vmin=-1, vmax=1,
                         transform=ccrs.PlateCarree())
    cax = inset_axes(ax, width="3%", height="60%", loc="center right",
                     bbox_to_anchor=(0.12, 0, 1, 1), bbox_transform=ax.transAxes)
    plt.colorbar(scatter, cax=cax, label="r")
    ax.text(0.02, 0.98, f'{len(corr_df)} series\nmean r={corr_df["r"].mean():.2f}',
            transform=ax.transAxes, va="top", fontsize=7)


def plot_seasonal_cycle(ax, var_name, color, monthly_df, sst_ds, correlations):
    if correlations.empty:
        ax.text(0.5, 0.5, "No eligible series", transform=ax.transAxes, ha="center")
        return
    identity = correlations.sort_values(["n", "entity_id"], ascending=[False, True]).iloc[0]["entity_id"]
    series = monthly_df.loc[monthly_df["entity_id"].eq(identity)]
    paired = _seasonal_pairs(series, var_name, sst_ds)
    draw_seasonal_cycle(ax, paired, var_name, color, series["Site"].iloc[0])


def draw_seasonal_cycle(ax, paired, var_name, color, site):
    """Draw the mean monthly cycle and both correlations from the same paired observations."""
    means, errors, r = _seasonal_statistics(paired)
    months = np.arange(1, 13)
    twin = ax.twinx()
    label = "Sr/Ca" if var_name == "SrCa" else "δ¹⁸O"
    unit = "mmol/mol" if var_name == "SrCa" else "‰ VPDB"
    proxy = ax.errorbar(months, means["proxy"], yerr=errors["proxy"], color=color,
                        marker="o", linewidth=1.3, capsize=2)
    sst = twin.errorbar(months, means["env"], yerr=errors["env"], color="#cb181d",
                        marker="s", linewidth=1.3, capsize=2)
    ax.invert_yaxis()
    ax.set_ylabel(f"{label} ({unit})", color=color)
    twin.set_ylabel("SST (°C)", color="#cb181d")
    ax.legend([proxy, sst], [f"{label} (inverted)", "SST"], loc="upper right")
    ax.set_xticks(months, MONTH_LABELS)
    ax.set_xlabel("Month")
    r_raw = pearsonr(paired["proxy"], paired["env"])[0]
    ax.text(0.02, 0.98,
            f"r_cycle = {r:.2f} (12-month means)\n"
            f"r_monthly = {r_raw:.2f}, n = {len(paired):,} paired months",
            transform=ax.transAxes, va="top", fontsize=7)
    ax.set_title(f"{site} ({paired.index[0]} to {paired.index[-1]})", loc="left")


def plot_anomaly_series(ax, monthly_df, var_name, site_name, nino=None, enso_stats=None):
    """绘制月异常时间序列；给出 nino/enso_stats 时叠加 Niño3.4 并标注定量相关。"""
    anomaly_df = calculate_anomaly(monthly_df, var_name, site_name)
    if anomaly_df is None:
        ax.text(0.5, 0.5, 'Insufficient data', transform=ax.transAxes, ha='center', va='center')
        return

    anomaly_df = anomaly_df.sort_values('Year')
    years, anomalies = anomaly_df['Year'].values, anomaly_df['Anomaly'].values
    label = "Sr/Ca" if var_name == "SrCa" else "δ¹⁸O"
    unit = "mmol/mol" if var_name == "SrCa" else "‰ VPDB"
    # δ18O 负异常对应暖/淡（El Niño 态）着红色；Sr/Ca 保持正异常红色
    warm_mask = anomalies < 0 if var_name == "d18O" else anomalies >= 0
    colors = np.where(warm_mask, "#cb181d", "#2171b5")
    ax.vlines(years, 0, anomalies, color=colors, alpha=0.6, linewidth=0.6)
    ax.axhline(0, color="black", linewidth=0.8)
    ax.set_xlabel("Year (CE)")
    ax.set_ylabel(f"{label} anomaly ({unit})")
    site = anomaly_df["Site"].iloc[0]
    if enso_stats and nino is not None:
        ax.set_title(f'{label} anomalies vs Niño3.4 ({site})', loc="left")
        subset = nino.loc[(nino.index.year >= int(np.floor(years.min())))
                          & (nino.index.year <= int(np.ceil(years.max())))]
        twin = ax.twinx()
        twin.plot(subset.index.year + (subset.index.month - 1) / 12, subset.to_numpy(),
                  color="#7f7f7f", linewidth=0.7, alpha=0.85)
        twin.set_ylabel("Niño3.4 (°C)", color="#7f7f7f")
        twin.tick_params(axis="y", labelcolor="#7f7f7f", labelsize=7)
        handles = [Line2D([], [], color="#cb181d", linewidth=2, label=f"{label} warm/fresh"),
                   Line2D([], [], color="#2171b5", linewidth=2, label=f"{label} cool/salty"),
                   Line2D([], [], color="#7f7f7f", linewidth=1, label="Niño3.4")]
        ax.legend(handles=handles, loc="upper right", fontsize=6, frameon=False)
        ax.text(0.02, 0.98,
                f"r = {enso_stats['r']:.2f}, p = {enso_stats['p']:.1e}, n = {enso_stats['n']}\n"
                "vs Niño3.4 (ERSSTv5, 1981–2010 base)",
                transform=ax.transAxes, va="top", fontsize=7)
    else:
        ax.set_title(f'{label} anomalies ({site})', loc="left")
        ax.text(0.02, 0.98, "Descriptive; monthly climatology removed",
                transform=ax.transAxes, va="top", fontsize=7)


# ======================== Figure 6(f): δ¹³C 最长连续段 ========================
PROXY_SEGMENT_MIN_N = 30
PROXY_SEGMENT_GAP = {"monthly": 12, "annual": 24, "submonthly": 12, "bimonthly": 24,
                     "seasonal": 24, "biannual": 24, "irregular": 24}
PROXY_LABEL = {"Sr/Ca": "Sr/Ca", "d18O": "δ¹⁸O", "d13C": "δ¹³C", "Ba/Ca": "Ba/Ca", "SST": "SST"}
PROXY_UNIT = {"Sr/Ca": "mmol/mol", "d18O": "‰ VPDB", "d13C": "‰ VPDB", "Ba/Ca": "μmol/mol", "SST": "°C"}


def _segment_years(segment):
    """把连续段观测数换算为年（月/亚月按月计、双月按 2 个月计、其余按年计）。"""
    n, res = segment["n"], segment["resolution"]
    if res in ("monthly", "submonthly"):
        return n / 12.0
    if res == "bimonthly":
        return n / 6.0
    return float(n)


def _proxy_segments(frames, proxy, gap_map=None):
    """按缺测间隔把每条来源序列切成连续段，返回 n≥PROXY_SEGMENT_MIN_N 的段（proxy 通用）。"""
    gap_map = gap_map or PROXY_SEGMENT_GAP
    segments = []
    for resolution, frame in frames.items():
        if frame is None or frame.empty or not {proxy, "entity_id", "Year"} <= set(frame):
            continue
        gap = gap_map.get(resolution, 24)
        for identity, group in frame.dropna(subset=[proxy]).groupby("entity_id"):
            month = group["Month"].astype(int) if "Month" in group else pd.Series(1, index=group.index)
            periods = pd.PeriodIndex.from_fields(year=np.floor(group["Year"]).astype(int),
                                                 month=month.to_numpy(), freq="M")
            values = pd.Series(group[proxy].to_numpy(dtype=float), index=periods)
            values = values.groupby(level=0).mean().sort_index()
            breaks = np.flatnonzero(np.diff(values.index.asi8) > gap) + 1
            site = group["Site"].iloc[0] if "Site" in group else str(identity)
            for idx in np.split(np.arange(len(values)), breaks):
                if len(idx) < PROXY_SEGMENT_MIN_N:
                    continue
                segment = values.iloc[idx]
                segments.append({"entity_id": identity, "resolution": resolution, "site": site,
                                 "n": len(segment), "start": segment.index[0],
                                 "end": segment.index[-1], "values": segment})
    return segments


def plot_proxy_lat_time_density(fig, ax, spatial, proxy, title, fontsize=8):
    """lat–time 观测密度（2° 纬带 × 100 年箱，对数色标）；spatial 已质控+验证坐标+dropna(proxy)。"""
    from matplotlib.colors import LogNorm
    label = PROXY_LABEL.get(proxy, proxy)
    if spatial is not None and not spatial.empty:
        lat_edges = np.arange(-40, 42, 2)
        year_edges = np.arange(-10050, 2026, 100)
        counts, ye, le = np.histogram2d(spatial["Year"], spatial["lat"], bins=[year_edges, lat_edges])
        mesh = ax.pcolormesh(ye, le, np.where(counts > 0, counts, np.nan).T,
                             cmap="YlOrRd", norm=LogNorm(vmin=1, vmax=max(1, counts.max())))
        ax.set_xlabel("Year (CE)", fontsize=7)
        ax.set_ylabel("Latitude (°)", fontsize=7)
        cb = fig.colorbar(mesh, ax=ax, pad=0.02)
        cb.ax.tick_params(labelsize=6)
        cb.set_label(f"{label} obs / 100 yr / 2°", fontsize=6)
    else:
        ax.text(0.5, 0.5, f"No {label} with verified coords", transform=ax.transAxes, ha="center")
    ax.set_title(f"{title} ({0 if spatial is None else len(spatial):,} obs)", loc="left", fontsize=fontsize)
    ax.tick_params(labelsize=6)


def plot_proxy_resolution_composition(ax, frames, proxy, title, identity_frame=None, fontsize=8):
    """分辨率构成柱状（各分辨率观测数）+ 主分辨率序列/站点数。"""
    label = PROXY_LABEL.get(proxy, proxy)
    names, counts = [], []
    for resolution, frame in frames.items():
        if frame is None or proxy not in frame:
            continue
        names.append(resolution.capitalize())
        counts.append(int(frame[proxy].notna().sum()))
    colors = plt.cm.Blues(np.linspace(0.75, 0.35, max(1, len(names))))
    ax.bar(names, counts, color=colors, edgecolor="black", linewidth=0.4)
    for i, v in enumerate(counts):
        ax.text(i, v, f"{v:,}", ha="center", va="bottom", fontsize=6)
    base = identity_frame if identity_frame is not None else next(iter(frames.values()), None)
    n_ent = base.loc[base[proxy].notna(), "entity_id"].nunique() if base is not None and proxy in base else 0
    n_site = base.loc[base[proxy].notna(), "Site"].nunique() if base is not None and {proxy, "Site"} <= set(base) else 0
    ax.set_ylabel(f"{label} observations", fontsize=7)
    ax.set_title(f"{title} ({n_ent} series / {n_site} sites)", loc="left", fontsize=fontsize)
    ax.tick_params(labelsize=6)


def plot_proxy_coverage_quality(fig, cell, frames, proxy, spatial_frame, prefix="f",
                                gap_map=None, identity_frame=None):
    """proxy 通用全时空覆盖/质量面板（2×2）：lat–time 密度、连续段长度、分辨率构成、最长段示例。

    Fig6f(δ¹³C) 与 Fig12(Ba/Ca) 共用同一实现；既给整个时空范围的覆盖与连续性质量，也保留最长段作示例。
    """
    label = PROXY_LABEL.get(proxy, proxy)
    unit = PROXY_UNIT.get(proxy, "")
    sub = cell.subgridspec(2, 2, hspace=0.55, wspace=0.30)
    ax1 = fig.add_subplot(sub[0, 0])
    ax2 = fig.add_subplot(sub[0, 1])
    ax3 = fig.add_subplot(sub[1, 0])
    ax4 = fig.add_subplot(sub[1, 1])

    segments = _proxy_segments(frames, proxy, gap_map)
    segments.sort(key=lambda s: (-s["n"], str(s["entity_id"])))

    plot_proxy_lat_time_density(fig, ax1, spatial_frame, proxy, f"({prefix}1) {label} lat–time density")

    # (2) 连续段长度分布（年），标注最长 3 段
    if segments:
        lengths = np.array([_segment_years(s) for s in segments])
        ax2.hist(lengths, bins=20, color="#6a51a3", edgecolor="white", linewidth=0.4)
        for s in segments[:3]:
            ax2.axvline(_segment_years(s), color="#cb181d", linewidth=0.8, linestyle="--")
        ax2.set_xlabel("Continuous-segment length (yr)", fontsize=7)
        ax2.set_ylabel("Segments", fontsize=7)
    else:
        ax2.text(0.5, 0.5, f"No {label} segments", transform=ax2.transAxes, ha="center")
    ax2.set_title(f"({prefix}2) Continuity ({len(segments)} segments ≥{PROXY_SEGMENT_MIN_N} obs)",
                  loc="left", fontsize=8)
    ax2.tick_params(labelsize=6)

    plot_proxy_resolution_composition(ax3, frames, proxy, f"({prefix}3) Resolution", identity_frame)

    # (4) 最长连续段示例（保留 illustrative example）
    if segments:
        s = segments[0]
        values = s["values"]
        years = values.index.year + (values.index.month - 1) / 12
        ax4.plot(years, values.to_numpy(), color="#6a51a3", linewidth=0.7)
        ax4.set_xlabel("Year (CE)", fontsize=7)
        ax4.set_ylabel(f"{label} ({unit})", fontsize=7)
        ax4.set_title(f'({prefix}4) Longest: {s["site"]} (n={s["n"]:,})', loc="left", fontsize=8)
    else:
        ax4.text(0.5, 0.5, f"No {label} data", transform=ax4.transAxes, ha="center")
    ax4.tick_params(labelsize=6)
    for ax in (ax1, ax2, ax3, ax4):
        ax.spines[["top", "right"]].set_visible(False)



# ======================== Figure 6 主绑图函数 ========================
def plot_figure6(monthly_df, annual_df, sst_ds):
    """创建Figure 6: 3行2列布局"""
    fig = plt.figure(figsize=(14, 18))
    gs = fig.add_gridspec(3, 2, height_ratios=[1, 1.2, 1.6],
                          hspace=0.30, wspace=0.25, left=0.08, right=0.92, top=0.96, bottom=0.05)

    grid_monthly = filter_analysis_data(monthly_df, require_coordinates=True)

    # (a) Sr/Ca-SST 相关性地图
    ax_a = fig.add_subplot(gs[0, 0], projection=ccrs.Robinson(central_longitude=180))
    corr_srca = compute_site_seasonal_correlation(grid_monthly, 'SrCa', sst_ds)
    plot_correlation_map(ax_a, corr_srca, '(a) Sr/Ca-SST Seasonal Correlation', DIVERGING_CMAP)

    # (b) δ18O-SST 相关性地图
    ax_b = fig.add_subplot(gs[0, 1], projection=ccrs.Robinson(central_longitude=180))
    corr_d18o = compute_site_seasonal_correlation(grid_monthly, 'd18O', sst_ds)
    plot_correlation_map(ax_b, corr_d18o, '(b) δ18O-SST Seasonal Correlation', DIVERGING_CMAP)

    # (c) Sr/Ca 季节循环
    ax_c = fig.add_subplot(gs[1, 0])
    plot_seasonal_cycle(ax_c, 'SrCa', '#2171b5', grid_monthly, sst_ds, corr_srca)

    # (d) δ18O 季节循环
    ax_d = fig.add_subplot(gs[1, 1])
    plot_seasonal_cycle(ax_d, 'd18O', '#238b45', grid_monthly, sst_ds, corr_d18o)

    # (e) δ18O 异常 vs Niño3.4 定量验证；无合格序列时回退 Sr/Ca 描述性面板
    ax_e = fig.add_subplot(gs[2, 0])
    nino = compute_nino34(sst_ds)
    enso_id, enso_stats = select_enso_record(grid_monthly, nino)
    if enso_id is not None:
        logger.info("Figure 6(e) ENSO 定量验证选中 %s: r=%.3f, p=%.2e, n=%d (%s..%s)",
                    enso_id, enso_stats["r"], enso_stats["p"], enso_stats["n"],
                    enso_stats["start"], enso_stats["end"])
        plot_anomaly_series(ax_e, monthly_df, 'd18O', enso_id, nino=nino, enso_stats=enso_stats)
    else:
        logger.info("Figure 6(e) 无序列通过 Niño3.4 定量门槛（n>=%d, |r|>=%.1f, p<%g），回退 Sr/Ca 描述性面板。",
                    ENSO_MIN_N, ENSO_MIN_ABS_R, ENSO_MAX_P)
        best_site = find_best_site(monthly_df, 'SrCa')
        plot_anomaly_series(ax_e, monthly_df, 'SrCa', best_site)

    # (f) δ¹³C 全时空覆盖/质量面板（2×2：密度/连续性/分辨率/最长段示例）
    spatial_d13c = filter_analysis_data(monthly_df, require_coordinates=True)
    spatial_d13c = spatial_d13c.dropna(subset=["d13C"]) if spatial_d13c is not None else None
    plot_proxy_coverage_quality(fig, gs[2, 1], {"monthly": monthly_df, "annual": annual_df},
                                "d13C", spatial_d13c, prefix="f", identity_frame=monthly_df)

    return fig


def run_figure_6(run_dir, out_dir, external_data_dir=None):
    """Figure 6：季节性循环与气候信号验证"""
    produced = []
    monthly_df = load_monthly_data(run_dir)
    annual_df = load_annual_data(run_dir)
    if monthly_df.empty and annual_df.empty:
        logger.warning("Figure 6：未读取到 monthly/annual 数据，跳过。")
        return produced

    sst_path = resolve_grid_file(run_dir, SST_NC_NAME, external_data_dir)
    if sst_path is None:
        logger.warning("找不到 %s，Figure 6 的 SST 相关子图将显示 No Data。", SST_NC_NAME)
    sst_ds = open_grid_dataset(sst_path)

    logger.info(f"月度数据: {len(monthly_df)} 条, 年度数据: {len(annual_df)} 条")
    logger.info("SST网格数据: %s", "已加载" if sst_ds is not None else "未找到")

    if not HAS_CARTOPY:
        logger.warning("未检测到 Cartopy，Figure 6 地图类子图无法绘制，跳过。")
        return produced

    fig = plot_figure6(monthly_df, annual_df, sst_ds)
    save_figure(fig, os.path.join(out_dir, 'Figure6_Seasonality_Verification'))
    logger.info("图表已保存: Figure6_Seasonality_Verification.pdf/png/eps")
    produced.append('Figure6_Seasonality_Verification')
    return produced


# ======================== Figure 9: 方差分解（对齐 Walter2023 Fig 5） ========================
MIN_VARIANCE_SPAN_YEARS = 30      # 序列最短跨度（年）
MIN_MONTH_COVERAGE = 0.80         # 跨度内实测月份占比下限
MAX_MONTH_GAP = 24                # 允许线性内插的最长连续缺测（月）
FILTER_ORDER = 6                  # Butterworth 阶数（与 Walter2023 一致）
VARIANCE_BANDS = (
    ("seasonal", "Seasonal (13-month high-pass)", 60.0),
    ("interannual", "Interannual (2–7-year band-pass)", 60.0),
    ("decadal", "Decadal (10-year low-pass)", 80.0),
)


def _regular_monthly_series(series, proxy, min_coverage=MIN_MONTH_COVERAGE, max_gap=MAX_MONTH_GAP):
    """按日历月去重平均、检查跨度/覆盖率/缺测长度后线性内插为规则月序列。"""
    frame = series.loc[np.isfinite(series[proxy]), ["Year", "Month", proxy]]
    if frame.empty:
        return None, np.nan
    periods = pd.PeriodIndex.from_fields(year=np.floor(frame["Year"]).astype(int),
                                         month=frame["Month"].astype(int), freq="M")
    values = pd.Series(frame[proxy].to_numpy(dtype=float), index=periods)
    values = values.groupby(level=0).mean().sort_index()
    span_months = (values.index[-1] - values.index[0]).n + 1
    if span_months < MIN_VARIANCE_SPAN_YEARS * 12:
        return None, np.nan
    full = values.reindex(pd.period_range(values.index[0], values.index[-1], freq="M"))
    coverage = float(full.notna().mean())
    if coverage < min_coverage:
        return None, coverage
    missing = full.isna()
    if missing.any():
        runs = missing.ne(missing.shift()).cumsum()
        if int(missing.groupby(runs).sum().max()) > max_gap:
            return None, coverage
    return full.interpolate(method="linear", limit_area="inside"), coverage


def compute_variance_decomposition(user_dfs, *, min_coverage=MIN_MONTH_COVERAGE,
                                   max_gap=MAX_MONTH_GAP):
    """≥30 年月分辨率序列的三带方差占比（各带方差 / 全序列方差，零相位滤波）。"""
    from scipy.signal import butter, sosfiltfilt

    frame = user_dfs.get("monthly")
    if frame is None or frame.empty:
        return pd.DataFrame()
    nyquist = 6.0  # 月采样：每年 12 个样本，Nyquist = 6 周/年
    filters = {
        "seasonal": butter(FILTER_ORDER, (12.0 / 13.0) / nyquist, btype="highpass", output="sos"),
        "interannual": butter(FILTER_ORDER, [(1.0 / 7.0) / nyquist, (1.0 / 2.0) / nyquist],
                              btype="bandpass", output="sos"),
        "decadal": butter(FILTER_ORDER, (1.0 / 10.0) / nyquist, btype="lowpass", output="sos"),
    }
    results = []
    keys = "entity_id" if "entity_id" in frame else ["lat", "lon"]
    for identity, series in frame.groupby(keys):
        lat, lon = series.iloc[0][["lat", "lon"]]
        for proxy in ("Sr/Ca", "d18O", "d13C"):
            if proxy not in series:
                continue
            monthly, coverage = _regular_monthly_series(series, proxy, min_coverage, max_gap)
            if monthly is None or len(monthly) < MIN_VARIANCE_SPAN_YEARS * 12:
                continue
            values = monthly.to_numpy(dtype=float)
            total_var = float(np.var(values, ddof=1))
            if not np.isfinite(total_var) or total_var <= 0:
                continue
            row = {"entity_id": identity, "lat": lat, "lon": lon, "proxy": proxy,
                   "n_months": len(values), "span_years": len(values) / 12.0,
                   "month_coverage": coverage}
            for band, sos in filters.items():
                filtered = sosfiltfilt(sos, values)
                row[f"{band}_pct"] = 100.0 * float(np.var(filtered, ddof=1)) / total_var
            results.append(row)
    return pd.DataFrame(results)


def plot_variance_decomposition(var_df, compact_titles=False):
    """3 行（季节/年际/年代际）× 3 列（Sr/Ca、δ18O、δ13C）方差占比空间图。

    compact_titles 用于整页缩排场景：标题只留指标+频带+序列数，滤波定义移入脚注。
    """
    if var_df.empty:
        return None
    fig, axes = plt.subplots(3, 3, figsize=(18, 10),
                             subplot_kw={"projection": ccrs.Robinson(central_longitude=180)})
    # 先定布局再取坐标：固定 cax 替代 inset_axes（后者渐变网格在 pdfium 渲染下产生游离色条碎片）
    fig.subplots_adjust(bottom=0.09, hspace=0.30, wspace=0.15)
    cmap = plt.get_cmap("YlOrRd")
    proxies = (("Sr/Ca", "Sr/Ca"), ("d18O", "δ¹⁸O"), ("d13C", "δ¹³C"))
    band_short = {"seasonal": "seasonal", "interannual": "interannual", "decadal": "decadal"}
    for i, (band, label, vmax) in enumerate(VARIANCE_BANDS):
        for j, (proxy, plabel) in enumerate(proxies):
            ax = axes[i, j]
            _configure_correlation_map(ax)
            subset = var_df.loc[var_df["proxy"].eq(proxy)]
            column = f"{band}_pct"
            if subset.empty:
                ax.text(0.5, 0.5, "No eligible series", transform=ax.transAxes, ha="center")
            else:
                ax.scatter(subset["lon"], subset["lat"], c=subset[column].clip(upper=vmax),
                           cmap=cmap, vmin=0, vmax=vmax, s=32, edgecolors="black", linewidth=0.4,
                           transform=ccrs.PlateCarree())
                if j == 2:
                    pos = ax.get_position()
                    cax = fig.add_axes([pos.x1 + 0.012, pos.y0 + 0.2 * pos.height,
                                        0.012, 0.6 * pos.height])
                    sm = plt.cm.ScalarMappable(cmap=cmap, norm=plt.Normalize(0, vmax))
                    fig.colorbar(sm, cax=cax, label="% variance")
            name = band_short[band] if compact_titles else label
            ax.set_title(f"({chr(97 + i * 3 + j)}) {plabel} {name} ({len(subset)} series)", loc="left")
    footnote = ("Sixth-order zero-phase Butterworth filters; monthly records spanning ≥30 years "
                "(coverage ≥80%, gaps ≤24 months linearly interpolated). Band variances are fractions of total "
                "record variance and need not sum to 100%. δ¹³C included as an added proxy beyond CoralHydro2k.")
    if compact_titles:
        footnote = ("Seasonal = 13-month high-pass; interannual = 2–7-year band-pass; decadal = 10-year low-pass "
                    "(sixth-order zero-phase Butterworth).\nMonthly records spanning ≥30 years (coverage ≥80%, "
                    "gaps ≤24 months linearly interpolated).\nBand variances are fractions of total record variance "
                    "and need not sum to 100%. δ¹³C included as an added proxy beyond CoralHydro2k.")
    fig.text(0.5, 0.012 if compact_titles else 0.025, footnote, ha="center", fontsize=7)
    return fig


def run_figure_9(run_dir, out_dir, external_data_dir=None):
    """Figure 9：季节/年际/年代际方差分解（仅依赖本批次月分辨率汇总表）。"""
    produced = []
    user_data = load_user_summaries(run_dir)
    if not user_data:
        logger.warning("Figure 9：未读取到 monthly/annual 汇总表，跳过。")
        return produced
    if not HAS_CARTOPY:
        logger.warning("未检测到 Cartopy，Figure 9 无法绘制，跳过。")
        return produced
    var_df = compute_variance_decomposition(user_data)
    if var_df.empty:
        logger.warning("Figure 9：没有满足 ≥30 年条件的月分辨率序列，跳过。")
        return produced
    var_df.to_csv(os.path.join(out_dir, 'variance_decomposition.csv'), index=False, encoding='utf-8-sig')
    logger.info("方差分解：%d 条序列\n%s", len(var_df),
                var_df.groupby("proxy")[["seasonal_pct", "interannual_pct", "decadal_pct"]].median())
    fig = plot_variance_decomposition(var_df)
    if fig is not None:
        save_figure(fig, os.path.join(out_dir, 'Figure9_Variance_Decomposition'))
        logger.info("绘图完成: Figure9_Variance_Decomposition.png/pdf/eps")
        produced.append('Figure9_Variance_Decomposition')
    return produced


# ======================== Figure 10: 局地可复现性（对齐 Walter2023 Fig 7） ========================
REPRO_RADIUS_KM = 50.0            # 同站点判定半径
REPRO_MIN_OVERLAP_YEARS = 20      # 最少共同观测年数


def _haversine_km(lat1, lon1, lat2, lon2):
    """球面距离（km），输入为度。"""
    phi1, phi2 = np.radians(lat1), np.radians(lat2)
    dphi = np.radians(lat2 - lat1)
    dlambda = np.radians(lon2 - lon1)
    a = np.sin(dphi / 2) ** 2 + np.cos(phi1) * np.cos(phi2) * np.sin(dlambda / 2) ** 2
    return float(6371.0 * 2 * np.arctan2(np.sqrt(a), np.sqrt(1 - a)))


def _entity_period_series(frame, proxy, annual):
    """entity_id -> (lat, lon, 按周期索引的代用序列)。"""
    entities = {}
    keys = "entity_id" if "entity_id" in frame else ["lat", "lon"]
    for identity, series in frame.groupby(keys):
        if proxy not in series:
            continue
        sub = series.loc[np.isfinite(series[proxy])]
        if sub.empty:
            continue
        years = np.floor(sub["Year"]).astype(int)
        if annual:
            # pandas 2.3 的 from_fields 仅传 year 有缺陷；经月周期转年周期（兼容负年份）
            periods = pd.PeriodIndex.from_fields(year=years, month=np.ones(len(years), dtype=int),
                                                 freq="M").asfreq("Y")
        else:
            periods = pd.PeriodIndex.from_fields(year=years, month=sub["Month"].astype(int), freq="M")
        values = pd.Series(sub[proxy].to_numpy(dtype=float), index=periods)
        values = values.groupby(level=0).mean().sort_index()
        entities[identity] = (float(series.iloc[0]["lat"]), float(series.iloc[0]["lon"]), values)
    return entities


def compute_local_reproducibility(user_dfs):
    """同指标、50 km 内、≥20 年共同观测的序列对 Pearson r；逐站点取中位绝对相关。

    非独立序列对排除并留痕：共同观测值完全一致（重复版本），或 |r| ≥ 0.999
    （跨文献转载同一岩芯数据的近完美相关，如 Cobb2003→Dee2020）。
    返回 (站点统计表, 序列对明细表)。
    """
    site_rows, pair_rows = [], []
    for resolution, frame in user_dfs.items():
        if frame is None or frame.empty:
            continue
        annual = resolution.casefold() == "annual"
        periods_per_year = 1 if annual else 12
        min_overlap = REPRO_MIN_OVERLAP_YEARS * periods_per_year
        for proxy in ("Sr/Ca", "d18O", "d13C"):
            entities = _entity_period_series(frame, proxy, annual)
            ids = list(entities)
            partner_rs = {identity: [] for identity in ids}
            for a in range(len(ids)):
                lat_a, lon_a, va = entities[ids[a]]
                for b in range(a + 1, len(ids)):
                    lat_b, lon_b, vb = entities[ids[b]]
                    distance = _haversine_km(lat_a, lon_a, lat_b, lon_b)
                    if distance > REPRO_RADIUS_KM:
                        continue
                    common = va.index.intersection(vb.index)
                    if len(common) < min_overlap:
                        continue
                    xa = va.loc[common].to_numpy(dtype=float)
                    xb = vb.loc[common].to_numpy(dtype=float)
                    if np.ptp(xa) == 0 or np.ptp(xb) == 0:
                        continue
                    duplicate = bool(np.allclose(xa, xb, rtol=0, atol=1e-9))
                    r = 1.0 if duplicate else float(np.corrcoef(xa, xb)[0, 1])
                    near_identical = bool(abs(r) >= 0.999)
                    pair_rows.append({"entity_a": ids[a], "entity_b": ids[b], "proxy": proxy,
                                      "resolution": "Annual" if annual else "Monthly",
                                      "distance_km": distance, "overlap_years": len(common) / periods_per_year,
                                      "n_common": len(common), "r": r, "duplicate_version": duplicate,
                                      "near_identical": near_identical})
                    if not (duplicate or near_identical):
                        partner_rs[ids[a]].append(abs(r))
                        partner_rs[ids[b]].append(abs(r))
            for identity, rs in partner_rs.items():
                if not rs:
                    continue
                lat, lon, _ = entities[identity]
                site_rows.append({"entity_id": identity, "lat": lat, "lon": lon, "proxy": proxy,
                                  "resolution": "Annual" if annual else "Monthly",
                                  "median_abs_r": float(np.median(rs)), "n_partners": len(rs)})
    return pd.DataFrame(site_rows), pd.DataFrame(pair_rows)


def plot_local_reproducibility(repro_df, early=None):
    """2×3 地图：月/年 × Sr/Ca/δ18O/δ13C 的站点中位绝对相关，标记大小=伙伴序列数。

    early 给定时追加第三行（g–i）：双方 <1850 CE 子集重新配对的站点统计，
    标记形状区分分辨率（圆=月、菱形=年），色标与主行一致。
    """
    if repro_df.empty:
        return None
    rows = 3 if early is not None else 2
    fig, axes = plt.subplots(rows, 3, figsize=(16, 7.5 if rows == 2 else 11.2),
                             subplot_kw={"projection": ccrs.Robinson(central_longitude=180)})
    panels = (("Sr/Ca", "Monthly", "(a) Monthly Sr/Ca"), ("d18O", "Monthly", "(b) Monthly δ¹⁸O"),
              ("d13C", "Monthly", "(c) Monthly δ¹³C"), ("Sr/Ca", "Annual", "(d) Annual Sr/Ca"),
              ("d18O", "Annual", "(e) Annual δ¹⁸O"), ("d13C", "Annual", "(f) Annual δ¹³C"))
    cmap = plt.get_cmap("YlOrRd")
    for ax, (proxy, resolution, title) in zip(axes.flat, panels):
        _configure_correlation_map(ax)
        subset = repro_df.loc[repro_df["proxy"].eq(proxy) & repro_df["resolution"].eq(resolution)]
        if subset.empty:
            ax.text(0.5, 0.5, "No eligible sites", transform=ax.transAxes, ha="center")
        else:
            sizes = 18 + 20 * subset["n_partners"].clip(upper=6)
            ax.scatter(subset["lon"], subset["lat"], c=subset["median_abs_r"], cmap=cmap,
                       vmin=0, vmax=1, s=sizes, edgecolors="black", linewidth=0.4,
                       transform=ccrs.PlateCarree())
        ax.set_title(f"{title} ({len(subset)} sites)", loc="left")
    if early is not None:
        for ax, (proxy, letter) in zip(axes[2], (("Sr/Ca", "g"), ("d18O", "h"), ("d13C", "i"))):
            _configure_correlation_map(ax)
            subset = early.loc[early["proxy"].eq(proxy)]
            for resolution, marker in (("Monthly", "o"), ("Annual", "D")):
                part = subset.loc[subset["resolution"].eq(resolution)]
                if part.empty:
                    continue
                sizes = 18 + 20 * part["n_partners"].clip(upper=6)
                ax.scatter(part["lon"], part["lat"], c=part["median_abs_r"], cmap=cmap,
                           vmin=0, vmax=1, s=sizes, marker=marker, edgecolors="black",
                           linewidth=0.4, transform=ccrs.PlateCarree())
            if subset.empty:
                ax.text(0.5, 0.5, "No eligible pre-1850 sites", transform=ax.transAxes, ha="center")
            ax.set_title(f"({letter}) Pre-1850 {PROXY_LABEL.get(proxy, proxy)} ({len(subset)} sites)",
                         loc="left")
    fig.subplots_adjust(bottom=0.17 if rows == 2 else 0.145, hspace=0.22, wspace=0.12)
    cax = fig.add_axes([0.27, 0.125 if rows == 2 else 0.115, 0.46, 0.014])
    sm = plt.cm.ScalarMappable(cmap=cmap, norm=plt.Normalize(0, 1))
    fig.colorbar(sm, cax=cax, orientation="horizontal", label="Median absolute Pearson r")
    size_handles = [Line2D([], [], marker="o", linestyle="", markerfacecolor="#bdbdbd",
                           markeredgecolor="black", markersize=np.sqrt(18 + 20 * n),
                           label=label) for n, label in ((1, "N = 1"), (3, "N = 3"), (6, "N ≥ 6"))]
    if early is not None:
        size_handles += [Line2D([], [], marker=marker, linestyle="", markerfacecolor="#bdbdbd",
                                markeredgecolor="black", markersize=5, label=label)
                         for marker, label in (("o", "Monthly"), ("D", "Annual"))]
    fig.legend(handles=size_handles, loc="lower center",
               bbox_to_anchor=(0.5, 0.048 if rows == 2 else 0.030),
               ncol=3 if early is None else 5, frameon=False)
    fig.text(0.5, 0.012 if rows == 2 else 0.006,
             "Same-proxy series within 50 km sharing ≥20 years of observations; marker size = number of "
             "partner records (N). Near-identical pairs (|r| ≥ 0.999, likely shared source data) excluded. "
             "Monthly correlations retain the seasonal cycle. δ¹³C included as an added proxy beyond CoralHydro2k.",
             ha="center", fontsize=7)
    return fig


def run_figure_10(run_dir, out_dir, external_data_dir=None):
    """Figure 10：局地可复现性（仅依赖本批次 monthly/annual 汇总表）。"""
    produced = []
    user_data = load_user_summaries(run_dir)
    if not user_data:
        logger.warning("Figure 10：未读取到 monthly/annual 汇总表，跳过。")
        return produced
    if not HAS_CARTOPY:
        logger.warning("未检测到 Cartopy，Figure 10 无法绘制，跳过。")
        return produced
    site_df, pair_df = compute_local_reproducibility(user_data)
    if site_df.empty:
        logger.warning("Figure 10：没有满足 50 km + ≥20 年重叠的序列对，跳过。")
        return produced
    site_df.to_csv(os.path.join(out_dir, 'local_reproducibility_sites.csv'), index=False, encoding='utf-8-sig')
    pair_df.to_csv(os.path.join(out_dir, 'local_reproducibility_pairs.csv'), index=False, encoding='utf-8-sig')
    n_dup = int(pair_df["duplicate_version"].sum()) if not pair_df.empty else 0
    n_near = int((pair_df["near_identical"] & ~pair_df["duplicate_version"]).sum()) if not pair_df.empty else 0
    logger.info("局地可复现性：%d 个站点统计，%d 个序列对（重复版本 %d 对 + 近完美相关 %d 对已排除）\n%s",
                len(site_df), len(pair_df), n_dup, n_near,
                site_df.groupby(["proxy", "resolution"])["median_abs_r"].agg(["count", "median"]))
    fig = plot_local_reproducibility(site_df)
    if fig is not None:
        save_figure(fig, os.path.join(out_dir, 'Figure10_Local_Reproducibility'))
        logger.info("绘图完成: Figure10_Local_Reproducibility.png/pdf/eps")
        produced.append('Figure10_Local_Reproducibility')
    return produced
