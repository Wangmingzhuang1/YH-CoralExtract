"""论文分析阶段统一绘图风格与全局常量（由原 run 目录散落脚本合并而来）"""
import warnings

import matplotlib
matplotlib.use("Agg")  # 无界面后端，必须在 pyplot 导入前设置
import matplotlib.pyplot as plt

warnings.filterwarnings('ignore')

# Cartopy 导入（可选依赖，缺失时地图类子图自动跳过）
try:
    import cartopy.crs as ccrs
    import cartopy.feature as cfeature
    HAS_CARTOPY = True
except ImportError:
    ccrs = None
    cfeature = None
    HAS_CARTOPY = False

# ======================== 全局范围常量（数据自适应 + 可显式覆盖） ========================
# 标准数据范围：项目数据库标准 "12000 年以来"（约 -10050 CE 起）至 2025 CE。
# 年份 < STD_YEAR_MIN（深海珊瑚等）或 > STD_YEAR_MAX（异常未来年份）的记录不进入图的
# 统计分箱，并在加载时按条数写日志（见 loader.filter_by_qc_and_year），不静默丢弃。
STD_YEAR_MIN = -10050
STD_YEAR_MAX = 2025
# 主图范围：CE 数据密集段（默认 1600~2025，可据数据分位数微调或用 set_ranges 覆盖）
YEAR_MIN = 1600
YEAR_MAX = 2025
# 长尺度面板/插图范围：全新世（-10050~1600），替代旧的 0~1600
LONG_YEAR_MIN = -10050
LONG_YEAR_MAX = 1600
# Figure2 岩芯顶年代窗口（近年段），上界随主数据延伸到 2025，避免丢弃近年岩芯顶
CORETOP_YEAR_MIN = 1900
CORETOP_YEAR_MAX = 2025
LAT_RANGE = (-40, 40)
CORE_LAT_RANGE = (-30, 30)
# 图统计默认保留的行级质控标记：0=正常, 2=统计异常保留；排除 3(超范围)/4(待复核)
QC_KEEP_FLAGS = (0, 2)


def set_ranges(year_min=None, year_max=None, long_year_min=None, long_year_max=None,
               std_year_min=None, std_year_max=None, coretop_year_min=None,
               coretop_year_max=None, lat_range=None, core_lat_range=None,
               qc_keep_flags=None):
    """覆盖模块级范围常量；任一参数为 None 时保持当前值。

    由 run_analysis 在生成图之前调用，实现"数据自适应 + 可显式覆盖"。
    返回覆盖后的当前范围字典（见 get_ranges）。
    """
    global YEAR_MIN, YEAR_MAX, LONG_YEAR_MIN, LONG_YEAR_MAX
    global STD_YEAR_MIN, STD_YEAR_MAX, CORETOP_YEAR_MIN, CORETOP_YEAR_MAX
    global LAT_RANGE, CORE_LAT_RANGE, QC_KEEP_FLAGS
    if year_min is not None:
        YEAR_MIN = year_min
    if year_max is not None:
        YEAR_MAX = year_max
    if long_year_min is not None:
        LONG_YEAR_MIN = long_year_min
    if long_year_max is not None:
        LONG_YEAR_MAX = long_year_max
    if std_year_min is not None:
        STD_YEAR_MIN = std_year_min
    if std_year_max is not None:
        STD_YEAR_MAX = std_year_max
    if coretop_year_min is not None:
        CORETOP_YEAR_MIN = coretop_year_min
    if coretop_year_max is not None:
        CORETOP_YEAR_MAX = coretop_year_max
    if lat_range is not None:
        LAT_RANGE = tuple(lat_range)
    if core_lat_range is not None:
        CORE_LAT_RANGE = tuple(core_lat_range)
    if qc_keep_flags is not None:
        QC_KEEP_FLAGS = tuple(qc_keep_flags)
    return get_ranges()


def get_ranges():
    """返回当前生效的范围常量快照（dict）。"""
    return {
        'year_min': YEAR_MIN, 'year_max': YEAR_MAX,
        'long_year_min': LONG_YEAR_MIN, 'long_year_max': LONG_YEAR_MAX,
        'std_year_min': STD_YEAR_MIN, 'std_year_max': STD_YEAR_MAX,
        'coretop_year_min': CORETOP_YEAR_MIN, 'coretop_year_max': CORETOP_YEAR_MAX,
        'lat_range': LAT_RANGE, 'core_lat_range': CORE_LAT_RANGE,
        'qc_keep_flags': QC_KEEP_FLAGS,
    }

# 顶刊配色 (Nature/Science 常用色板)
PALETTE = {
    'primary': ['#4E79A7', '#F28E2B', '#E15759', '#76B7B2', '#59A14F', '#EDC948', '#B07AA1'],
    'gray': '#4A4A4A',
    'light_gray': '#E0E0E0',
    'land': '#F0F0F0',
    'water': '#FFFFFF',
    'border': '#444444'
}

GROUP_ORDER = ['Group 1', 'Group 2', 'Group 3', 'Group 4', 'Group 5', 'Group 6', 'Group 7']
# 图例显示名：Group 编号对应互斥指标组合（与图 3b 的 G1–G7 定义一致）
GROUP_DISPLAY = {
    'Group 1': 'Sr/Ca + δ¹⁸O + δ¹³C', 'Group 2': 'Sr/Ca + δ¹⁸O', 'Group 3': 'δ¹⁸O + δ¹³C',
    'Group 4': 'Sr/Ca', 'Group 5': 'δ¹⁸O', 'Group 6': 'δ¹³C', 'Group 7': 'Sr/Ca + δ¹³C',
    'Ba/Ca only': 'Ba/Ca only', 'SST only': 'SST only', 'Ba/Ca + SST': 'Ba/Ca + SST',
    'Ba/Ca': 'Ba/Ca only', 'SST': 'SST only', 'Other': 'Other',
}
# 映射颜色到 Group
GROUP_COLORS = {g: c for g, c in zip(GROUP_ORDER, PALETTE['primary'])}
if len(GROUP_ORDER) > len(PALETTE['primary']):
    import itertools
    colors_cycle = itertools.cycle(PALETTE['primary'])
    GROUP_COLORS = {g: next(colors_cycle) for g in GROUP_ORDER}

G_COLORS = {
    'G1': '#4E79A7', 'G2': '#F28E2B', 'G3': '#E15759', 'G4': '#76B7B2',
    'G5': '#59A14F', 'G6': '#EDC948', 'G7': '#B07AA1', 'Other': '#9D9D9D'
}

# 盆地颜色映射（匹配论文图2a）
BASIN_COLORS = {
    'Red Sea': '#E15759',
    'East Pacific': '#F28E2B',
    'West Pacific': '#4E79A7',
    'Indian': '#76B7B2',
    'Atlantic': '#59A14F',
    'Other': '#B07AA1'
}

# 代理类型颜色映射（匹配论文图2b）
PROXY_COLORS = {
    'Sr/Ca-only': '#4E79A7',
    'Sr/Ca + δ18O': '#F28E2B',
    'δ18O-only': '#E15759',
    'δ13C-only': '#76B7B2',
    'δ18O + δ13C': '#59A14F',
    'Sr/Ca + δ13C': '#EDC948',
    'Three indicators': '#B07AA1',
    'Other': '#9D9D9D',
    'Ba/Ca': '#59A14F',
    'SST': '#B07AA1',
    'Ba/Ca + SST': '#8C6D46',
}

RES_COLORS = {
    '月尺度': '#4E79A7',
    '双月尺度': '#A0CBE8',
    '季度尺度': '#59A14F',
    '半年尺度': '#F28E2B',
    '年尺度': '#E15759',
    'Unknown': '#B07AA1'
}

# 英文键映射（原 FIG4 旭日图使用英文分辨率标签，颜色与中文键一一对应）
RES_COLORS_EN = {
    'Monthly': '#4E79A7', 'Bimonthly': '#A0CBE8', 'Seasonal': '#59A14F',
    'Biannual': '#F28E2B', 'Annual': '#E15759', 'Unknown': '#B07AA1',
    'Submonthly': '#76B7B2', 'Irregular': '#9D9D9D', 'Discrete samples': '#EDC948'
}
RES_COLORS.update(RES_COLORS_EN)


def set_pub_style():
    """配置 matplotlib 以符合顶刊排版标准"""
    plt.rcParams.update({
        'font.family': 'serif',
        'font.serif': ['Times New Roman', 'DejaVu Serif'],
        'font.size': 8,
        'axes.linewidth': 0.5,
        'axes.edgecolor': 'black',
        'xtick.major.width': 0.5,
        'ytick.major.width': 0.5,
        'xtick.direction': 'in',
        'ytick.direction': 'in',
        'xtick.major.size': 3,
        'ytick.major.size': 3,
        'figure.dpi': 300,
        'savefig.dpi': 600,
        'savefig.bbox': 'tight',
        'savefig.pad_inches': 0.05,
        'axes.labelsize': 8,
        'axes.titlesize': 9,
        'axes.titleweight': 'bold',
        'legend.fontsize': 7,
        'legend.frameon': False,
        'pdf.fonttype': 42,
        'mathtext.fontset': 'stix'
    })


def add_panel_label(ax, label, x=-0.06, y=1.02):
    """添加子图标签 (a, b, c)，位置固定在左上角外部"""
    ax.text(x, y, label, transform=ax.transAxes,
            fontsize=9, fontweight='bold', va='bottom', ha='right')


def save_figure(fig, stem):
    """统一保存 pdf + png(600dpi) + eps 三种格式，随后关闭画布"""
    fig.savefig(f'{stem}.pdf')
    fig.savefig(f'{stem}.png', dpi=600)
    fig.savefig(f'{stem}.eps', format='eps')
    plt.close(fig)
