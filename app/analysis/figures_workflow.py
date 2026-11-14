"""Figure 7-8：工作流架构示意与数据账目（统一 GeoDataExtractor 视觉语言）。

Figure 7 由项目根 GeoDataExtractor_workflow.svg 经 headless Chrome/Edge 渲染：
矢量 PDF + 600 dpi PNG（零新依赖；SVG 为唯一可编辑源）。
Figure 8 以同一风格（实心圆角框+阴影、同调色板、侧栏层标签、LEGEND BOX）
用 matplotlib 数据驱动重绘，账目从 run 目录现算，口径与 Figure 1-3 加载器一致。
所有标签英文、新罗马（style.set_pub_style）。
"""
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

from .style import set_pub_style, save_figure

logger = logging.getLogger(__name__)

# 与 GeoDataExtractor_workflow.svg 一致的调色板 (fill, edge)
WF_BLUE = ('#4A7EBB', '#2E5A8F')
WF_GREEN = ('#70AD47', '#4E7A27')
WF_GOLD = ('#BF9000', '#8F6B00')
WF_ORANGE = ('#DD7E36', '#A85A20')
WF_RED = ('#B03A2E', '#7E2A20')
WF_TEAL = ('#1F7A72', '#14554E')
WF_GRAY = ('#8494A7', '#5F6E80')
WF_BG = '#FAFAF8'
WF_LEGEND = ('#F2F2F2', '#999999')
WF_INK = '#111111'


# ======================== Figure 7: SVG 架构示意渲染 ========================
def _browser_binary():
    for path in (r'C:\Program Files\Google\Chrome\Application\chrome.exe',
                 r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
                 shutil.which('chrome'), shutil.which('msedge')):
        if path and os.path.isfile(path):
            return path
    return None


def render_workflow_svg(stem, body_font_scale=1.0):
    """把唯一矢量源 SVG 渲染为 stem.pdf（矢量）+ stem.png（约600dpi）。

    body_font_scale 仅放大 <15px 的正文字号（标题/框体不变），用于整页缩排后的印刷补偿。
    """
    svg_path = Path(__file__).resolve().parents[2] / 'GeoDataExtractor_workflow.svg'
    svg = svg_path.read_text(encoding='utf-8')
    if body_font_scale != 1.0:
        svg = re.sub(r'font-size="([\d.]+)"',
                     lambda m: f'font-size="{float(m.group(1)) * body_font_scale:.2f}"'
                     if float(m.group(1)) < 15 else m.group(0), svg)
    browser = _browser_binary()
    if browser is None:
        raise FileNotFoundError('渲染 Figure 7 需要 Chrome 或 Edge（headless）')
    wrapper = ('<!DOCTYPE html><html><head><meta charset="utf-8"><style>'
               '@page{size:1700px 2120px;margin:0}'
               'html,body{margin:0;padding:0;background:#FAFAF8}'
               'svg{display:block;width:1700px;height:2120px}'
               '</style></head><body>' + svg + '</body></html>')
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / 'workflow_print.html'
        page.write_text(wrapper, encoding='utf-8')
        url = page.as_uri()
        common = [browser, '--headless=new', '--disable-gpu', '--no-sandbox',
                  '--hide-scrollbars', f'--user-data-dir={tmp}/profile',
                  '--virtual-time-budget=3000']
        subprocess.run(common + [f'--print-to-pdf={stem}.pdf', '--no-pdf-header-footer', url],
                       check=True, timeout=180, capture_output=True)
        subprocess.run(common + [f'--screenshot={stem}.png', '--window-size=1700,2120',
                                 '--force-device-scale-factor=2.5', url],
                       check=True, timeout=180, capture_output=True)
    for ext in ('pdf', 'png'):
        if not os.path.isfile(f'{stem}.{ext}') or os.path.getsize(f'{stem}.{ext}') == 0:
            raise RuntimeError(f'Figure 7 渲染失败：{stem}.{ext}')
    logger.info('Figure 7 由 SVG 渲染: %s.pdf/png', stem)


# ======================== Figure 8: 数据账目 ========================
def load_accounting(run_dir):
    """从 run 目录现算文件桶、行级 QC 桶、复核队列桶与入图口径。"""
    from . import loader

    manifest = pd.read_csv(os.path.join(run_dir, 'input_manifest.csv'))
    files = manifest.drop_duplicates('file')
    with_output = int((files['output_rows'] > 0).sum())
    zero = files.loc[files['output_rows'] == 0, 'status'].value_counts()
    acc = {
        'files_total': len(files),
        'files_contributing': with_output,
        'files_review_hold': int(zero.get('review', 0)),
        'files_excluded': int(zero.get('excluded', 0)),
        'files_context': int(zero.get('context', 0)),
        'files_unsupported_failed': int(zero.get('unsupported', 0)) + int(zero.get('failed', 0)),
    }

    combined = pd.read_csv(os.path.join(run_dir, 'consolidated', 'all_data_combined.csv'),
                           usecols=['qc_flag'], low_memory=False)
    qc = pd.to_numeric(combined['qc_flag'], errors='coerce').value_counts()
    acc['rows_total'] = len(combined)
    acc['qc_clean'] = int(qc.get(0, 0))
    acc['qc_outlier_kept'] = int(qc.get(2, 0))
    acc['qc_out_of_range'] = int(qc.get(3, 0))
    acc['qc_pending'] = int(qc.get(4, 0))

    queue = pd.read_csv(os.path.join(run_dir, 'review_queue.csv'),
                        usecols=['action'], low_memory=False)
    action = queue['action'].fillna('')
    acc['review_total'] = len(queue)
    acc['review_values'] = int(action.str.startswith('暂缓该值').sum())
    acc['review_rows'] = int(action.str.startswith('暂缓该行').sum())
    acc['review_tables'] = int(action.str.startswith('暂缓表格').sum())
    acc['review_other'] = acc['review_total'] - acc['review_values'] - acc['review_rows'] - acc['review_tables']
    acc['review_applied'] = int(action.str.startswith(('已应用', '已排除')).sum())
    acc['review_other_pending'] = acc['review_other'] - acc['review_applied']

    fig1 = loader.load_figure1_data(run_dir)
    dated = fig1.loc[fig1['Dated']]
    older = fig1.loc[~fig1['Dated'] & fig1['Year'].lt(-10050)]
    acc['fig1_dated'] = len(dated)
    acc['fig1_older'] = len(older)
    acc['fig1_other'] = len(fig1) - len(dated) - len(older)
    acc['fig1_exact'] = int(dated['Exact location'].sum())
    acc['value_eligible'] = len(loader.process_data(run_dir))
    return acc


def _solid_box(ax, x, y, w, h, title, lines, color, title_fs=9.0, body_fs=7.3,
               text_color='white', rounding=1.2):
    """新图7风格：实心圆角框 + 偏移阴影 + 白色衬线字。"""
    fill, edge = color
    ax.add_patch(FancyBboxPatch((x + 0.55, y - 0.75), w, h,
                                boxstyle=f'round,pad=0,rounding_size={rounding}',
                                facecolor='#000000', alpha=0.22, linewidth=0, zorder=1))
    ax.add_patch(FancyBboxPatch((x, y), w, h,
                                boxstyle=f'round,pad=0,rounding_size={rounding}',
                                facecolor=fill, edgecolor=edge, linewidth=1.1, zorder=2))
    ax.text(x + w / 2, y + h - 2.5, title, ha='center', va='center',
            fontsize=title_fs, fontweight='bold', color=text_color, zorder=3)
    for i, line in enumerate(lines):
        ax.text(x + w / 2, y + h - 5.4 - i * 2.7, line, ha='center', va='center',
                fontsize=body_fs, color=text_color, zorder=3)


def _down_arrow(ax, x, y_from, y_to):
    ax.add_patch(FancyArrowPatch((x, y_from), (x, y_to), arrowstyle='-|>',
                                 mutation_scale=10, linewidth=1.2, color=WF_INK, zorder=3))


def plot_figure_8_accounting(acc):
    """(a) 文件→观测漏斗；(b) 行级 QC 与复核队列组成 + LEGEND BOX（现算口径）。"""
    set_pub_style()
    fig = plt.figure(figsize=(7.2, 9.4))
    fig.patch.set_facecolor(WF_BG)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.set_facecolor(WF_BG)
    ax.axis('off')
    f = acc

    # 标题（与图7同款双行衬线大标题）
    ax.text(50, 98.0, 'GeoDataExtractor: Data Accounting from Archive Files',
            ha='center', va='center', fontsize=12.5, fontweight='bold', color=WF_INK)
    ax.text(50, 95.2, 'to Analysis-Ready Observations',
            ha='center', va='center', fontsize=12.5, fontweight='bold', color=WF_INK)
    # 侧栏层标签（图7同款旋转 === 标签）
    ax.text(1.8, 66, '=== FILE FUNNEL ===', ha='center', va='center', fontsize=7.5,
            fontweight='bold', color='#444444', rotation=90)
    ax.text(1.8, 24, '=== DISPOSITION ===', ha='center', va='center', fontsize=7.5,
            fontweight='bold', color='#444444', rotation=90)

    # ---- (a) 文件漏斗 ----
    ax.text(6, 91.5, '(a)', fontsize=9, fontweight='bold', color=WF_INK)
    excl = f['files_excluded'] + f['files_context'] + f['files_unsupported_failed']
    _solid_box(ax, 8, 80.5, 46, 9, 'Archive Files Screened',
               [f"{f['files_total']:,} files · NOAA / PANGAEA / Zenodo"], WF_BLUE)
    _down_arrow(ax, 31, 80.2, 77.8)
    _solid_box(ax, 8, 68.5, 46, 9, 'Files Held for Review',
               [f"{f['files_review_hold']:,} files · recoverable via adjudication"], WF_ORANGE)
    _down_arrow(ax, 31, 68.2, 65.8)
    _solid_box(ax, 8, 56.5, 46, 9, 'Files Contributing Observations',
               [f"{f['files_contributing']:,} files"], WF_GREEN)
    _down_arrow(ax, 31, 56.2, 53.8)
    _solid_box(ax, 8, 44.5, 46, 9, 'Standardized Observations',
               [f"{f['rows_total']:,} rows"], WF_TEAL)
    # 提取前排除侧支
    ax.add_patch(FancyArrowPatch((54.2, 85), (59.8, 85), arrowstyle='-|>',
                                 mutation_scale=10, linewidth=1.2, color=WF_INK, zorder=3))
    _solid_box(ax, 60, 79, 37, 11.5, 'Excluded before Extraction',
               [f"{excl:,} files",
                f"out of scope {f['files_excluded']:,} · context-only {f['files_context']:,}",
                f"unsupported / failed {f['files_unsupported_failed']:,}"],
               WF_RED, title_fs=8.6, body_fs=6.8)
    ax.text(60, 71.5, 'Held items are recoverable through the\n'
                      'incremental patch loop (Figure 7):\n'
                      'adjudications re-extract affected sources.',
            ha='left', va='center', fontsize=6.5, color='#666666')

    # ---- (b) 行级 QC 与复核队列 ----
    ax.text(6, 40.5, '(b)', fontsize=9, fontweight='bold', color=WF_INK)
    rows = [
        ('Row QC flags', 35.5, [
            (f"clean (flag 0): {f['qc_clean']:,}", f['qc_clean'], WF_GREEN[0]),
            (f"outlier kept (2): {f['qc_outlier_kept']:,}", f['qc_outlier_kept'], WF_GOLD[0]),
            (f"out of range (3): {f['qc_out_of_range']:,}", f['qc_out_of_range'], WF_ORANGE[0]),
            (f"unit / scale pending (4): {f['qc_pending']:,}", f['qc_pending'], WF_RED[0]),
        ], f['rows_total']),
        ('Review queue items', 29.5, [
            (f"held values: {f['review_values']:,}", f['review_values'], WF_RED[0]),
            (f"held rows: {f['review_rows']:,}", f['review_rows'], WF_ORANGE[0]),
            (f"held tables: {f['review_tables']:,}", f['review_tables'], WF_GOLD[0]),
            (f"other: {f['review_other']:,}", f['review_other'], WF_GRAY[0]),
        ], f['review_total']),
    ]
    for label, ytop, parts, total in rows:
        left = 24.0
        for _, value, color in parts:
            width = 64.0 * value / total
            ax.barh(ytop, width, left=left, height=3.4, color=color,
                    edgecolor='white', linewidth=0.6, zorder=2)
            left += width
        ax.text(22.5, ytop + 1.7, label, ha='right', va='center', fontsize=8,
                fontweight='bold', color=WF_INK)
        ax.text(89, ytop + 1.7, f'n = {total:,}', ha='left', va='center', fontsize=6.5,
                color='#666666')

    # LEGEND BOX（图7同款灰底圆角图例框）
    fill, edge = WF_LEGEND
    ax.add_patch(FancyBboxPatch((6, 6.5), 88, 17, boxstyle='round,pad=0,rounding_size=1.0',
                                facecolor=fill, edgecolor=edge, linewidth=0.8, zorder=1))
    ax.text(50, 21.6, 'LEGEND BOX', ha='center', va='center', fontsize=9.5,
            fontweight='bold', color=WF_INK, zorder=2)
    for x0, (label, _, parts, _) in zip((10, 52), rows):
        ax.text(x0, 19.0, label, ha='left', va='center', fontsize=8,
                fontweight='bold', color=WF_INK, zorder=2)
        for i, (text, _, color) in enumerate(parts):
            y = 16.4 - i * 2.5
            ax.add_patch(FancyBboxPatch((x0, y - 0.8), 3.2, 1.6,
                                        boxstyle='round,pad=0,rounding_size=0.4',
                                        facecolor=color, edgecolor='white', linewidth=0.4, zorder=2))
            ax.text(x0 + 4.5, y, text, ha='left', va='center', fontsize=7,
                    color='#222222', zorder=2)
    ax.text(6, 3.9, f"Figure-eligible sets: value-level QC with verified locations {f['value_eligible']:,} rows "
                    f"(Figures 2-3); dated rows {f['fig1_dated']:,} plus {f['fig1_older']:,} older rows",
            ha='left', va='center', fontsize=6.5, color='#666666')
    ax.text(6, 1.9, f"in the Figure 1 inset; {f['fig1_other']:,} rows outside the analysis.",
            ha='left', va='center', fontsize=6.5, color='#666666')
    return fig


def run_figures_7_8(run_dir, out_dir, figures):
    produced = []
    if '7' in figures:
        render_workflow_svg(os.path.join(out_dir, 'Figure7_Workflow'))
        produced.append('Figure7_Workflow')
    if '8' in figures:
        acc = load_accounting(run_dir)
        logger.info("账目现算: %s", {k: v for k, v in acc.items()})
        save_figure(plot_figure_8_accounting(acc), os.path.join(out_dir, 'Figure8_Accounting'))
        produced.append('Figure8_Accounting')
    return produced