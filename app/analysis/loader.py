"""数据加载层：INDEX.csv 解析、珊瑚数据合并、分辨率汇总表、netCDF 格点数据路径解析

合并自 comprehensive_data_analysis.py 的 process_data/assign_basin/assign_resolution
与 FIG4.py 的 load_all_data、Figure5 的 load_user_summaries、Figure6 的 load_monthly/annual_data。
所有路径均由 run_dir 参数给出，不依赖脚本所在目录。
"""
import json
import logging
import os

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

try:
    import xarray as xr
    XARRAY_AVAILABLE = True
except ImportError:
    XARRAY_AVAILABLE = False
    logger.warning("未安装 xarray，依赖 netCDF 格点数据的图将被跳过。")

# 外部格点数据文件名（原脚本硬编码于 run 目录内）
SST_NC_NAME = 'sst.mnmean.nc'                            # NOAA ERSST v5
SSS_NC_NAME = 'Hadley_EN4_SSS_Surface_Merged.nc'         # Hadley EN4 SSS

# 分辨率标签映射（中文 / 英文）
RESOLUTION_ZH = {'monthly': '月尺度', 'annual': '年尺度',
                 'bimonthly': '双月尺度', 'biannual': '半年尺度',
                 'seasonal': '季度尺度', 'discrete_samples': '独立样品',
                 'submonthly': '月内高频观测', 'irregular': '不规则采样', 'unknown': '分辨率未确定'}
RESOLUTION_EN = {'月尺度': 'Monthly', '双月尺度': 'Bimonthly', '年尺度': 'Annual',
                 '半年尺度': 'Biannual', '季度尺度': 'Seasonal', '不均匀': 'Uneven',
                 '独立样品': 'Discrete samples', '月内高频观测': 'Submonthly',
                 '分辨率未确定': 'Unknown', '不规则采样': 'Irregular'}
RESOLUTION_EN.update({key: RESOLUTION_EN[label] for key, label in RESOLUTION_ZH.items()})

# 统一列名映射
COL_MAP = {
    'geo_latitude': 'Lat', 'geo_longitude': 'Lon', 'year_CE': 'Year',
    'Sr/Ca (mmol/mol)': 'Sr/Ca', 'δ18O (‰ VPDB)': 'd18O', 'δ13C (‰)': 'd13C',
    'geo_siteName': 'Site', 'resolution': 'Resolution', 'frequency': 'Frequency',
    'geo_basin': 'Basin'
}

# 分辨率汇总表列名映射（Figure5/6 使用的短名）
SUMMARY_COL_MAP = {
    'Sr/Ca (mmol/mol)': 'Sr/Ca', 'δ18O (‰ VPDB)': 'd18O', 'δ13C (‰)': 'd13C',
    'Ba/Ca (μmol/mol)': 'Ba/Ca', 'SST (°C)': 'SST', 'year_CE': 'Year', 'geo_siteName': 'Site',
    'geo_latitude': 'lat', 'geo_longitude': 'lon'
}

# 新 run 目录内缺少 netCDF 格点数据时，回退探测的同级旧 run 目录名
FALLBACK_EXTERNAL_DIRNAME = 'run_20251224_161554'


def filter_by_qc_and_year(df, year_col='Year', context='', log=True, check_row_flag=True):
    """图统计默认的行级质控 + 标准年份范围过滤（不静默丢弃，按条数写日志）。

    - 若存在 qc_flag，仅保留 qc_flag ∈ style.QC_KEEP_FLAGS（默认 {0,2}：0=正常、
      2=统计异常保留），排除 3(超范围)/4(待复核)；
    - 年份 < style.STD_YEAR_MIN（如 -10050 CE，深海珊瑚等）或 > style.STD_YEAR_MAX
      （如 2025 CE，异常未来年份）的记录不进入统计分箱；
    - 缺失年份的记录不因范围过滤被丢弃（交由下游按需处理）。

    Args:
        df: 输入 DataFrame
        year_col: 年份列名（process_data 用 'Year'，汇总表用 'year_CE'）
        context: 日志中标注来源的短语，如 "（图1-3）"
        log: 是否写日志（同一数据多次过滤时可关闭避免重复）
    Returns:
        过滤后的新 DataFrame
    """
    from . import style
    if df is None or df.empty:
        return df

    keep = pd.Series(True, index=df.index)

    # 1) 标准年份范围（仅排除真实超范围记录，NaN 年份不在此丢弃）
    if year_col in df.columns:
        year = pd.to_numeric(df[year_col], errors='coerce')
        below = int((year < style.STD_YEAR_MIN).sum())
        above = int((year > style.STD_YEAR_MAX).sum())
        if log and (below or above):
            logger.info("排除超标准范围记录 %d 条%s：< %d CE 的 %d 条（深海珊瑚等），> %d CE 的 %d 条",
                        below + above, context, style.STD_YEAR_MIN, below,
                        style.STD_YEAR_MAX, above)
        keep &= year.isna() | year.between(style.STD_YEAR_MIN, style.STD_YEAR_MAX)

    # 2) 行级质控标记
    if check_row_flag and 'qc_flag' in df.columns:
        qc = pd.to_numeric(df['qc_flag'], errors='coerce')
        keep_qc = qc.isin(style.QC_KEEP_FLAGS)
        if log:
            excluded = qc.loc[~keep_qc]
            detail = '、'.join(f'qc_flag={int(k)}：{int(v)} 条'
                               for k, v in excluded.value_counts().sort_index().items())
            logger.info("QC 过滤%s：保留 qc_flag∈%s 共 %d 条，排除 %d 条（%s）",
                        context, tuple(style.QC_KEEP_FLAGS), int(keep_qc.sum()),
                        int((~keep_qc).sum()), detail or '无')
        keep &= keep_qc

    return df.loc[keep]


def _read_all_years(run_dir):
    """读取 run 数据的 year_CE（供自适应范围计算）。优先合并汇总 CSV，其次指标组文件。

    仅读取 year_CE 单列，41 万行也只需数秒。
    """
    consolidated = os.path.join(run_dir, 'consolidated', 'all_data_combined.csv')
    if os.path.exists(consolidated):
        candidates = [consolidated]
    else:
        candidates = [f for f in get_indicator_files(run_dir) if os.path.exists(f)]
    series = []
    for path in candidates:
        try:
            col = pd.read_csv(path, usecols=['year_CE'], encoding='utf-8-sig',
                              low_memory=False)['year_CE']
            series.append(pd.to_numeric(col, errors='coerce'))
        except Exception as e:
            logger.warning("读取 year_CE 失败（%s）: %s", path, e)
    if not series:
        return None
    return pd.concat(series, ignore_index=True).dropna()


def compute_adaptive_ranges(run_dir):
    """基于数据 year_CE 分位数推荐主图/长尺度范围（数据自适应默认值）。

    主图下界取标准范围内 P10 就近取整到百年（本数据集 P10≈1588 → 1600），
    上界取 style.STD_YEAR_MAX；长尺度面板为 [STD_YEAR_MIN, 主图下界]。
    无法读取数据时返回 None（回退到 style 默认常量）。返回 dict 可直接喂给
    style.set_ranges。
    """
    import math
    from . import style
    years = _read_all_years(run_dir)
    if years is None or years.empty:
        return None
    y = years.loc[(years >= style.STD_YEAR_MIN) & (years <= style.STD_YEAR_MAX)]
    if y.empty:
        return None
    p10 = float(y.quantile(0.10))
    year_min = int(round(p10 / 100.0) * 100)
    year_min = max(style.STD_YEAR_MIN, min(year_min, style.STD_YEAR_MAX - 50))
    return {
        'year_min': year_min,
        'year_max': style.STD_YEAR_MAX,
        'long_year_min': style.STD_YEAR_MIN,
        'long_year_max': year_min,
    }


def read_index(run_dir):
    """读取 run_dir/INDEX.csv，返回 DataFrame；不存在时返回 None"""
    index_path = os.path.join(run_dir, 'INDEX.csv')
    if not os.path.exists(index_path):
        return None
    return pd.read_csv(index_path, encoding='utf-8-sig')


def get_indicator_files(run_dir):
    """从 INDEX.csv 提取 indicator_group 类型的数据文件绝对路径列表；
    INDEX 缺失时回退到四个分辨率汇总 CSV"""
    index_df = read_index(run_dir)
    if index_df is not None:
        return [os.path.join(run_dir, row['path']) for _, row in index_df.iterrows()
                if row['type'] == 'indicator_group']
    return [os.path.join(run_dir, sub, f'{sub}_data.csv')
            for sub in ('monthly', 'annual', 'biannual', 'bimonthly')]


def _resolution_mapping(run_dir):
    """INDEX.csv 中 文件绝对路径 -> resolution 的映射"""
    mapping = {}
    index_df = read_index(run_dir)
    if index_df is not None:
        for _, row in index_df.iterrows():
            if pd.notna(row['resolution']) and row['resolution']:
                mapping[os.path.join(run_dir, row['path'])] = row['resolution']
    return mapping


def _grade_from_filename(filepath):
    """根据文件名中的 G1-G6 标记确定质量等级"""
    filename = os.path.basename(filepath).upper()
    for g_num in range(1, 7):
        if filename.startswith(f'G{g_num}_') or filename == f'G{g_num}.CSV':
            return f'G{g_num}'
    return 'Unclassified'


def assign_basin(lat, lon):
    """按热带与亚热带坐标分配宽尺度海域，边界不明的点归为 Other。"""
    lat = pd.to_numeric(pd.Series(np.ravel(lat)), errors='coerce')
    lon = pd.to_numeric(pd.Series(np.ravel(lon)), errors='coerce')
    lon = (lon + 180) % 360 - 180
    sectors = [
        lon.between(32, 44) & lat.between(12, 31),
        lon.between(-180, -100) | (lon.between(-100, -70) & lat.between(-40, 8)),
        lon.between(120, 180),
        lon.between(20, 110),
        lon.between(-100, 20),
    ]
    return np.select(sectors, ['Red Sea', 'East Pacific', 'West Pacific', 'Indian', 'Atlantic'], default='Other')


def assign_resolution(freq):
    """根据频率分配分辨率类型（中文标签，默认年尺度）"""
    freq_str = str(freq).lower()
    if "monthly" in freq_str:
        return "月尺度"
    elif "annual" in freq_str:
        return "年尺度"
    elif "bimonthly" in freq_str:
        return "双月尺度"
    elif "biannual" in freq_str:
        return "半年尺度"
    else:
        return "年尺度"  # 默认


def _resolution_from_path(filepath):
    """从文件所在目录读取分辨率标签。"""
    folder = os.path.basename(os.path.dirname(filepath)).lower()
    return folder if folder in RESOLUTION_ZH else "unknown"


def _load_frames(run_dir, file_list=None, resolution_lang='zh'):
    """公共读取逻辑：遍历指标组 CSV，附加 G_Grade 与 Resolution 标签

    resolution_lang: 'zh' -> 月尺度/年尺度...（comprehensive 版），
                     'en' -> Monthly/Annual...（FIG4 版）
    """
    if file_list is None:
        file_list = get_indicator_files(run_dir)
    res_mapping = _resolution_mapping(run_dir)

    df_list = []
    for f in file_list:
        if not os.path.exists(f):
            continue
        try:
            d = pd.read_csv(f, encoding='utf-8-sig')
            d.columns = d.columns.str.strip()
            d['G_Grade'] = _grade_from_filename(f)

            res_key = res_mapping.get(f) or _resolution_from_path(f)
            res_key = str(res_key).strip()
            if resolution_lang == 'en':
                # 与 FIG4.load_all_data 一致：中文标签映射为英文，其余原样保留
                d['Resolution'] = RESOLUTION_EN.get(res_key, res_key)
            else:
                d['Resolution'] = RESOLUTION_ZH.get(res_key.lower(), '分辨率未确定')
            df_list.append(d)
        except Exception as e:
            logger.warning(f"Skipping {f}: {e}")

    if not df_list:
        return pd.DataFrame()
    return pd.concat(df_list, ignore_index=True)


def process_data(run_dir, file_list=None, include_reviewed_representatives=False):
    """加载并合并所有指标组数据（comprehensive_data_analysis.process_data 移植版）

    - 列名标准化为 Lat/Lon/Year/Sr/Ca/d18O/d13C/Site
    - Resolution 使用英文标签（项目规则：图标签一律英文），Group 由 G_Grade 映射
    - 默认行级质控（qc_flag∈{0,2}）+ 标准年份范围 [-10050, 2025] 过滤并记日志，
      再按纬度范围过滤
    """
    from . import style
    df = _load_frames(run_dir, file_list, resolution_lang='en')
    if df.empty:
        return df

    df = df.rename(columns={k: v for k, v in COL_MAP.items() if k in df.columns})

    for c in ['Lat', 'Lon', 'Year', 'Sr/Ca', 'd18O', 'd13C']:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors='coerce')

    # 分配Basin（向量化）
    if 'Basin' not in df.columns:
        if 'Lat' in df.columns and 'Lon' in df.columns:
            df['Basin'] = assign_basin(df['Lat'], df['Lon'])
        else:
            df['Basin'] = 'Pacific'

    # 如果还没有Resolution列，使用原始逻辑
    if 'Resolution' not in df.columns:
        if 'Frequency' in df.columns:
            df['Resolution'] = df['Frequency'].apply(assign_resolution)
        else:
            df['Resolution'] = assign_resolution('annual')

    # 行级质控 + 标准年份范围过滤（排除超范围/待复核并记日志）
    df = filter_analysis_data(df, context='（图1-3）')
    if df.empty:
        return df
    df = _classify_indicators(df)

    # 纬度范围过滤（保留 reviewed 区域合成点逻辑）
    required_cols = ['Lat', 'Lon', 'Year']
    available_required_cols = [col for col in required_cols if col in df.columns]
    if available_required_cols:
        if {"Lat", "Lon", "Year"}.issubset(df.columns):
            exact = df['Lat'].between(*style.LAT_RANGE) & df['Lon'].between(-180, 180)
            if 'qc_location_status' in df:
                exact &= df['qc_location_status'].eq('pass') | df['qc_location_status'].isna()
            if include_reviewed_representatives:
                exact |= reviewed_regional_points(df)
            return df.loc[exact]
        df = df.dropna(subset=available_required_cols)
        if 'Lat' in df.columns:
            df = df[df['Lat'].between(*style.LAT_RANGE)]
    return df


def load_figure1_data(run_dir):
    """从合并表读取覆盖统计所需字段，保留无坐标及未归入六组的观测。"""
    path = os.path.join(run_dir, 'consolidated', 'all_data_combined.csv')
    columns = {
        'year_CE', 'geo_latitude', 'geo_longitude', 'qc_flag', 'qc_time_status',
        'qc_location_status', 'geo_rep_status', 'geo_rep_latitude', 'geo_rep_longitude',
        'composite_evidence', 'time_resolution', 'pub1_doi', 'source_file', 'sample_id', 'source_record_id', 'source_core_id',
        'source_processing', 'source_time_value',
        *_PROXY_FIELDS, *(f"{field}_qc_status" for field in _PROXY_FIELDS.values()),
    }
    if os.path.isfile(path):
        df = pd.read_csv(path, usecols=lambda name: name in columns, low_memory=False).rename(columns=COL_MAP)
        df, available = _mask_proxy_values(df)
        _mask_overlapping_composite_members(df, available, 'Year')
        _mask_processing_version_overlaps(df, available, 'Year')
        df = _classify_indicators(df)
    else:
        df = process_data(run_dir)
        if df.empty:
            return df
        df['Group'] = df['Group'].replace('Unclassified', 'Other')
        available = [column for column in _PROXY_FIELDS if column in df]
        logger.warning("历史批次没有合并表，Figure 1 仅展示可读取的分组文件")

    for field in ('Year', 'Lat', 'Lon'):
        df[field] = pd.to_numeric(df.get(field), errors='coerce')

    from . import style
    # 超标准范围年份：不进入主分箱，按条数汇总（Figure 1 另以插图/脚注透明呈现，不静默丢弃）
    below = int((df['Year'] < style.STD_YEAR_MIN).sum())
    above = int((df['Year'] > style.STD_YEAR_MAX).sum())
    if below or above:
        logger.info("排除超标准范围记录 %d 条（图1，不进入主分箱）：< %d CE 的 %d 条（深海珊瑚等，转插图/脚注），> %d CE 的 %d 条",
                    below + above, style.STD_YEAR_MIN, below, style.STD_YEAR_MAX, above)

    qc_ok = df[available].notna().any(axis=1)

    if 'qc_time_status' in df:
        df['Dated'] = np.isfinite(df['Year']) & df['qc_time_status'].eq('pass') & qc_ok
    else:
        df['Dated'] = df['Year'].between(style.STD_YEAR_MIN, style.STD_YEAR_MAX) & qc_ok
        logger.warning("历史批次没有逐字段年代质控，Figure 1 使用标准年代范围")
    exact = df['Lat'].between(-90, 90) & df['Lon'].between(-180, 180)
    if 'qc_location_status' in df:
        exact &= df['qc_location_status'].eq('pass')
    df['Exact location'] = exact
    df['Regional location'] = ~exact & reviewed_regional_points(df, lat_range=(-90, 90))
    return df


def reviewed_regional_points(frame, lat_range=None):
    fields = {"geo_rep_status", "geo_rep_latitude", "geo_rep_longitude"}
    if not fields.issubset(frame.columns):
        return pd.Series(False, index=frame.index)
    from .style import LAT_RANGE
    return (frame["geo_rep_status"].eq("reviewed_regional_composite")
            & pd.to_numeric(frame["geo_rep_latitude"], errors="coerce").between(*(lat_range or LAT_RANGE))
            & pd.to_numeric(frame["geo_rep_longitude"], errors="coerce").between(-180, 180))


def load_all_data(run_dir):
    """FIG4.load_all_data 移植版：保留原始列名，Resolution 使用英文标签。

    使用逐指标质控，兼容只含行级标记的历史批次。
    """
    df = _load_frames(run_dir, resolution_lang='en')
    return _classify_indicators(filter_analysis_data(df, context='（图S1旭日图）', year_col='year_CE'))


def load_consolidated_raw(run_dir):
    """读取 consolidated/all_data_combined.csv 原始行（不做质控掩膜）。

    供元数据完整性/披露类图（Figure15）统计提取阶段各字段的填充率，
    与 load_all_data（质控后分析集）口径区分。
    """
    path = os.path.join(run_dir, 'consolidated', 'all_data_combined.csv')
    if not os.path.exists(path):
        logger.warning(f"Consolidated file not found: {path}")
        return pd.DataFrame()
    df = pd.read_csv(path, encoding='utf-8-sig', low_memory=False)
    df.columns = df.columns.str.strip()
    return df


def load_resolution_csv(run_dir, resolution, rename=True, year_range=None):
    """加载分辨率汇总 CSV（如 monthly/monthly_data.csv）

    rename=True 时将列名映射为短名（Sr/Ca、d18O、Year、lat、lon 等）并转数值；
    year_range 非 None 时按 year_CE 过滤（缺省 None，年份范围统一由
    filter_by_qc_and_year / filter_analysis_data 按 style 标准范围处理）。
    """
    path = os.path.join(run_dir, resolution, f'{resolution}_data.csv')
    if not os.path.exists(path):
        logger.warning(f"Summary file not found: {path}")
        return None
    df = pd.read_csv(path, encoding='utf-8-sig')
    df.columns = df.columns.str.strip()
    if rename:
        df.rename(columns=SUMMARY_COL_MAP, inplace=True)
        for col in ['Sr/Ca', 'd18O', 'd13C', 'Ba/Ca', 'SST', 'Year', 'lat', 'lon',
                    'SrCa', 'geo_latitude', 'geo_longitude', 'year_CE']:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')
    if year_range and 'Year' in df.columns:
        df = df[(df['Year'] >= year_range[0]) & (df['Year'] <= year_range[1])]
    return df


_PROXY_FIELDS = {
    "Sr/Ca (mmol/mol)": "Sr/Ca_value", "Sr/Ca": "Sr/Ca_value", "SrCa": "Sr/Ca_value",
    "δ18O (‰ VPDB)": "δ18O_value", "d18O": "δ18O_value",
    "δ13C (‰)": "δ13C_value", "d13C": "δ13C_value",
    "SST (°C)": "sea_surface_temperature_value", "SST": "sea_surface_temperature_value",
    "Ba/Ca (μmol/mol)": "Ba/Ca_value", "Ba/Ca": "Ba/Ca_value", "Δδ18O (‰ VPDB)": "δ18O_difference",
}


def _classify_indicators(frame):
    from app.core.moa.data_output import INDICATOR_GROUPS

    if frame.empty:
        return frame
    pairs = (("Sr/Ca", "Sr/Ca (mmol/mol)"), ("d18O", "δ18O (‰ VPDB)"), ("d13C", "δ13C (‰)"))
    values = [pd.to_numeric(frame.get(alias, frame.get(raw, pd.Series(np.nan, index=frame.index))),
                            errors="coerce") for alias, raw in pairs]
    lookup = {pattern: name.split("_", 1)[0] for pattern, name in INDICATOR_GROUPS.items()}
    frame["G_Grade"] = [lookup.get(pattern, "Other") for pattern in zip(*(np.isfinite(v) for v in values))]
    frame["Group"] = frame["G_Grade"].str.replace(r"^G(\d+)$", r"Group \1", regex=True)
    return frame


def _mask_proxy_values(frame):
    from . import style

    selected = frame.copy()
    flag = pd.to_numeric(selected["qc_flag"], errors="coerce") if "qc_flag" in selected else None
    available = []
    for column, field in _PROXY_FIELDS.items():
        if column not in selected:
            continue
        status = selected.get(f"{field}_qc_status")
        legacy_ok = flag.isin(style.QC_KEEP_FLAGS) if flag is not None else pd.Series(True, index=selected.index)
        passed = {"pass", "outlier_flagged"} if 2 in style.QC_KEEP_FLAGS else {"pass"}
        eligible = legacy_ok if status is None else status.isin(passed).where(status.notna(), legacy_ok)
        if flag is not None:
            eligible &= ~flag.ge(5)
        selected[column] = pd.to_numeric(selected[column], errors="coerce").where(eligible)
        available.append(column)
    return selected, available


def _mask_overlapping_composite_members(frame, available, year_col):
    """同一论文的同指标、同分辨率、同年代仅采用一个明确已纳入的岩芯观测。"""
    required = {"composite_evidence", "time_resolution", "source_record_id", year_col}
    if not required.issubset(frame) or not frame["composite_evidence"].notna().any():
        return
    from app.core.moa.context_extractor import named_composite_cores

    def labels(column):
        return frame.get(column, pd.Series("", index=frame.index)).fillna("").astype(str).str.strip().str.casefold()

    doi = labels("pub1_doi")
    scope = doi.where(doi.ne(""), labels("source_file"))
    sample = labels("source_core_id")
    sample = sample.where(sample.ne(""), labels("sample_id"))
    sample = sample.where(sample.ne(""), labels("source_record_id"))
    cadence = labels("time_resolution")
    years = pd.to_numeric(frame[year_col], errors="coerce").round(8)
    timed = years.notna() & scope.ne("") & ~cadence.isin({"", "unknown"})
    if "qc_time_status" in frame:
        timed &= frame["qc_time_status"].eq("pass") | frame["qc_time_status"].isna()
    evidence = frame.loc[timed & frame["composite_evidence"].notna(), "composite_evidence"]
    for text in evidence.unique():
        try:
            declared = json.loads(text)
        except (TypeError, ValueError):
            continue
        if not isinstance(declared, dict):
            continue
        written = evidence.index[evidence.eq(text)]
        for column in available:
            field = _PROXY_FIELDS[column]
            cores = named_composite_cores(declared.get(field))
            if not cores:
                continue
            retained = written[frame.loc[written, column].notna()]
            keys = pd.MultiIndex.from_arrays([scope.loc[retained], cadence.loc[retained], years.loc[retained]])
            member = timed & sample.isin(cores) & ~frame.index.isin(written)
            overlap = pd.MultiIndex.from_arrays([scope[member], cadence[member], years[member]]).isin(keys)
            frame.loc[frame.index[member][overlap], column] = np.nan


def _mask_processing_version_overlaps(frame, available, year_col):
    """同岩芯同年代的不同处理版本（raw/interpolated）重叠值只计一次，优先原始版。

    与 _mask_overlapping_composite_members 互补：后者处理合成列与组成岩芯，
    本规则处理同一岩芯的原始版与插值版重叠。
    """
    if "source_processing" not in frame or not frame["source_processing"].notna().any():
        return
    identity = frame.get("source_record_id", pd.Series("", index=frame.index)).fillna("").astype(str).str.strip()
    core = frame.get("source_core_id", pd.Series("", index=frame.index)).fillna("").astype(str).str.strip()
    sample = identity.where(identity.ne(""), core)
    # 按论文/源文件隔离：不同论文中恰好同名同年的岩芯不是同一岩芯的处理版本
    doi = frame.get("pub1_doi", pd.Series("", index=frame.index)).fillna("").astype(str).str.strip()
    source = frame.get("source_file", pd.Series("", index=frame.index)).fillna("").astype(str).str.strip()
    scope = doi.where(doi.ne(""), source)
    entity = scope + "||" + sample
    years = pd.to_numeric(frame[year_col], errors="coerce").round(8)
    processing = frame["source_processing"].fillna("").astype(str).str.casefold()
    is_interp = processing.str.contains("interpolat|interp")
    for column in available:
        values = pd.to_numeric(frame[column], errors="coerce")
        present = values.notna() & entity.ne("") & years.notna()
        if not present.any():
            continue
        sub = pd.DataFrame({"entity": entity[present], "year": years[present], "interp": is_interp[present]},
                           index=frame.index[present])
        has_raw = sub.groupby(["entity", "year"])["interp"].transform(lambda s: (~s).any())
        mask = sub["interp"] & has_raw
        frame.loc[sub.index[mask], column] = np.nan


def filter_analysis_data(frame, require_coordinates=False, context='', year_col='Year'):
    """按年代、指标和使用场景分别筛选。历史批次缺少逐指标标记时使用行级标记。"""
    if frame is None or frame.empty:
        return frame
    if year_col not in frame:
        return frame.iloc[0:0].copy()

    frame = filter_by_qc_and_year(frame, year_col=year_col, context=context, check_row_flag=False)
    if frame.empty:
        return frame.iloc[0:0].copy()

    eligible = pd.to_numeric(frame[year_col], errors="coerce").notna()
    if "qc_time_status" in frame:
        eligible &= frame["qc_time_status"].eq("pass") | frame["qc_time_status"].isna()
    if require_coordinates:
        if not {"lat", "lon"}.issubset(frame.columns):
            return frame.iloc[0:0].copy()
        eligible &= (pd.to_numeric(frame["lat"], errors="coerce").between(-90, 90)
                     & pd.to_numeric(frame["lon"], errors="coerce").between(-180, 180))
        if "qc_location_status" in frame:
            eligible &= frame["qc_location_status"].eq("pass")

    selected, available = _mask_proxy_values(frame.loc[eligible])
    _mask_overlapping_composite_members(selected, available, year_col)
    _mask_processing_version_overlaps(selected, available, year_col)
    return selected.dropna(subset=available, how="all") if available else selected.iloc[0:0]


def add_series_identity(frame):
    """按文献、源文件、工作表和记录标识区分同一海域的不同岩芯。"""
    keys = ["paper_hash", "source_file", "sheet_name", "source_record_id", "data_source"]
    values = [frame[key].fillna("").astype(str) for key in keys if key in frame]
    if values:
        identity = values[0]
        for value in values[1:]:
            identity = identity.str.cat(value, sep=" | ")
        frame["entity_id"] = identity
    else:
        frame["entity_id"] = frame["Site"].fillna("") if "Site" in frame else ""
    # 历史文件缺少记录 ID 时保留站点区分，新文件始终保留来源标识。
    for key in ("Site", "Lat", "Lon", "lat", "lon"):
        if key in frame:
            frame["entity_id"] = frame["entity_id"].str.cat(frame[key].fillna("").astype(str), sep=" | ")


def _add_calendar_month(frame):
    """优先使用源日历日期，其他月度记录从十进制年份还原月份。"""
    years = np.floor(frame["Year"])
    month = np.floor((frame["Year"] - years) * 12 + 0.06).clip(0, 11) + 1
    if "source_time_value" in frame:
        values = frame["source_time_value"].astype("string")
        dates = values.str.extract(
            r"^(\d{4})[-/](\d{2})[-/]\d{2}(?:[ T].*)?$"
        ).apply(pd.to_numeric, errors="coerce")
        valid = (dates[0].eq(years) & dates[1].between(1, 12)).fillna(False)
        month = month.where(~valid, dates[1])
        # Mon-YY（如 Mar-87）直接取月份名，避免分数年公式把 Mar 误归 Feb
        mon_map = {name: index for index, name in enumerate(
            ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
        named = values.str.extract(r"^([A-Za-z]{3})[a-z]*[-\s/]\d{2,4}$", expand=False).str.casefold().map(mon_map)
        month = month.where(named.isna(), named)
    frame["Month"] = month.astype(int)
    frame["Year_int"] = years.astype(int)


def load_user_summaries(run_dir):
    """读取 monthly/annual 分辨率汇总表（Figure5 相关性分析输入）

    返回 {resolution: DataFrame}，包含坐标、年月和 Sr/Ca、d18O、d13C 等列。
    """
    data_map = {}
    for resolution in ('monthly', 'annual'):
        df_temp = load_resolution_csv(run_dir, resolution, rename=True, year_range=None)
        if df_temp is None:
            continue
        logger.info(f"Loading {resolution} summary data from: {run_dir}/{resolution}")
        df_temp = filter_analysis_data(df_temp, require_coordinates=True,
                                       context=f'（图5 {resolution}）')
        add_series_identity(df_temp)
        df_temp['Resolution'] = resolution

        # 从Year的小数部分提取月份信息
        if 'Month' not in df_temp.columns and 'Year' in df_temp.columns:
            _add_calendar_month(df_temp)

        data_map[resolution] = df_temp
        srca_count = df_temp['Sr/Ca'].notna().sum() if 'Sr/Ca' in df_temp.columns else 0
        d18o_count = df_temp['d18O'].notna().sum() if 'd18O' in df_temp.columns else 0
        logger.info(f"  -> Loaded {len(df_temp)} records (Sr/Ca: {srca_count}, d18O: {d18o_count})")
    return data_map


def load_monthly_data(run_dir):
    """加载月尺度珊瑚数据（Figure6 输入）"""
    df = load_resolution_csv(run_dir, 'monthly', rename=True, year_range=None)
    df = filter_analysis_data(df, context='（图6 monthly）')
    if df is None or df.empty or 'Year' not in df.columns:
        return pd.DataFrame()
    if 'Sr/Ca' in df.columns:
        df.rename(columns={'Sr/Ca': 'SrCa'}, inplace=True)
    add_series_identity(df)
    _add_calendar_month(df)
    return df


def load_annual_data(run_dir):
    """加载年尺度数据（Figure6 输入）"""
    df = load_resolution_csv(run_dir, 'annual', rename=True, year_range=None)
    df = filter_analysis_data(df, context='（图6 annual）')
    if df is None or df.empty or 'Year' not in df.columns:
        return pd.DataFrame()
    if 'Sr/Ca' in df.columns:
        df.rename(columns={'Sr/Ca': 'SrCa'}, inplace=True)
    add_series_identity(df)
    df['Year_int'] = np.floor(df['Year']).astype(int)
    return df


def resolve_grid_file(run_dir, filename, external_data_dir=None):
    """解析外部格点数据路径：优先 run_dir 内，找不到再看 external_data_dir"""
    candidate = os.path.join(run_dir, filename)
    if os.path.exists(candidate):
        return candidate
    if external_data_dir:
        candidate = os.path.join(external_data_dir, filename)
        if os.path.exists(candidate):
            return candidate
    return None


def open_grid_dataset(path):
    """安全打开 netCDF 数据集，失败返回 None"""
    if path is None or not XARRAY_AVAILABLE:
        return None
    try:
        return xr.open_dataset(path)
    except Exception as e:
        logger.warning(f"读取 NetCDF 出错 ({path}): {e}")
        return None
