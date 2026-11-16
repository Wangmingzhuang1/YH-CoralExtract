"""生成 ESSD 九幅候选主图与附录 A1–A6，默认读取同一个增量归档。"""

import argparse
import html
import json
import logging
from pathlib import Path
import shutil
import site
import sys
import textwrap
from tempfile import TemporaryDirectory

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.transforms import Bbox
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

from app.analysis import (coral_comparison, figures_dataset, figures_structure,
                          figures_validation, figures_workflow, loader, style)
from app.analysis.figures_spatial import aggregate_sites, plot_figure_1_optimized, plot_figure_2_advanced
from plots import (accounting_panel, composition, early_records,
                   environmental_consistency, rescale_for_width, source_tables, variance)
from series_analysis import EARLY_YEAR, environmental_results, pair_results, variance_results
from source_audit import audit_source_tables


MOA = ROOT / "output" / "moa_output"
DEFAULT_RUN = MOA / "run_20261114_223923_498530"
BASELINE_DIR = MOA / "run_20261111_052203_466656"
# 外部格点归档曾位于 moa_output 内，清理后可能移到盘根；按候选顺序探测。
_EXTERNAL_CANDIDATES = [MOA / loader.FALLBACK_EXTERNAL_DIRNAME,
                        ROOT.parent / loader.FALLBACK_EXTERNAL_DIRNAME]
EXTERNAL = next((p for p in _EXTERNAL_CANDIDATES if p.is_dir()), _EXTERNAL_CANDIDATES[0])
BASELINE_REFERENCE = ROOT / "output" / "coral_comparison" / "CoralHydro2k1_0_1.zip"

STEMS = {
    1: "Figure1_Extraction_and_QC", 2: "Figure2_Coverage", 3: "Figure3_Archive_Composition",
    4: "Figure4_CoralHydro2k", 5: "Figure5_Source_Table_Audit", 6: "Figure6_Submonthly",
    7: "Figure7_Environmental_Consistency", 8: "Figure8_Variance_and_Sensitivity",
    9: "Figure9_Local_Agreement",
}
APPENDIX = "Appendix_A1_Instrumental_Correlations"
EARLY_APPENDIX = "Appendix_A2_Early_Records"
D13C_APPENDIX = "Appendix_A3_d13C_Coverage"
METHODS_APPENDIX = "Appendix_A4_Analytical_Methods"
METADATA_APPENDIX = "Appendix_A5_Metadata_Disclosure"
CONSISTENCY_APPENDIX = "Appendix_A6_Internal_Consistency"
NETWORK_APPENDIX = "Appendix_A7_Network_Potential"
DURATION_BINS = ["<20", "20-50", "50-100", "100-200", ">200"]
UNIT_COLUMNS = {"Sr/Ca": "Sr/Ca (mmol/mol)", "d18O": "δ18O (‰ VPDB)",
                "d13C": "δ13C (‰)", "Ba/Ca": "Ba/Ca (μmol/mol)"}
METHOD_COLORS = {
    "LA-ICP-MS": "#426A91", "IRMS": "#478982", "ICP-MS": "#C19C58", "ICP-AES": "#B96E59",
    "Mixed": "#8B7AA2", "Other": "#71818B", "Unknown": "#BFC5CA",
}
METADATA_COLORS = ("#3E7576", "#AD6C50")


def save_figure(fig, number, output):
    if fig is None:
        raise RuntimeError(f"Figure {number} could not be plotted")
    stem = STEMS.get(number, number)
    for ext, dpi in (("pdf", 300), ("png", 360)):
        fig.savefig(output / f"{stem}.{ext}", dpi=dpi, facecolor="white")
    plt.close(fig)
    print(f"Figure {number} ready", flush=True)


def pdf_part(fig, path, bounds=None):
    if bounds:
        bounds = Bbox.from_bounds(*bounds)
    fig.savefig(path, dpi=300, bbox_inches=bounds, pad_inches=0, facecolor="white")
    plt.close(fig)
    return path


def combine_panels(parts, number, output, gap=9):
    """Preserve existing vector panels while arranging them on one page."""
    import pypdfium2
    try:
        from pypdf import PdfReader, PdfWriter, Transformation
    except ImportError:
        sys.path.append(site.getusersitepackages())
        from pypdf import PdfReader, PdfWriter, Transformation
    sources = [PdfReader(part).pages[0] for part in parts]
    width = 7.35 * 72
    scales = [width / float(p.mediabox.width) for p in sources]
    heights = [float(p.mediabox.height) * s for p, s in zip(sources, scales)]
    writer = PdfWriter()
    y = sum(heights) + (len(sources) - 1) * gap
    page = writer.add_blank_page(width=width, height=y)
    for source, scale, height in zip(sources, scales, heights):
        y -= height
        page.merge_transformed_page(source, Transformation().scale(scale).translate(0, y))
        y -= gap
    page.compress_content_streams(level=9)
    writer.compress_identical_objects()
    stem = output / STEMS.get(number, number)
    writer.write(stem.with_suffix(".pdf"))
    with pypdfium2.PdfDocument(stem.with_suffix(".pdf")) as pdf:
        pdf[0].render(scale=5, fill_color=(255, 255, 255, 255)).to_pil().save(
            stem.with_suffix(".png"), dpi=(360, 360))
    print(f"Figure {number} ready", flush=True)


def original_structure(run, frame, output, temp):
    processed = loader.process_data(run)
    sites = aggregate_sites(processed)
    length = (sites["Max_Year"] - sites["Min_Year"]).clip(lower=1)
    durations = pd.cut(length, [0, 20, 50, 100, 200, np.inf], labels=DURATION_BINS)
    durations = durations.value_counts().reindex(DURATION_BINS).fillna(0).astype(int)
    original = plot_figure_2_advanced(processed, split_other=True)
    # Keep the original end-year plots and the map. The new a–c row replaces the old overview.
    for ax in original.axes[:2]:
        original.delaxes(ax)
    for ax, old, new in zip(original.axes, ("c", "d", "e"), ("d", "e", "f")):
        for text in ax.texts:
            if text.get_text() == f"({old})":
                text.set_text(f"({new})")
    top = pdf_part(composition(frame, output, durations), temp / "composition.pdf", (0, 0, 7.55, 4.15))
    bottom = pdf_part(original, temp / "end_years.pdf", (0, 0, 7.2, 7.12))
    combine_panels([top, bottom], 3, output, gap=6)
    return durations


def original_workflow(counts, row_count, qc, queue, output, temp):
    # Keep the existing workflow graphic (body type compensated for the reduced page) plus accounting.
    try:
        figures_workflow.render_workflow_svg(temp / "workflow_compensated", body_font_scale=1.5)
        original = temp / "workflow_compensated.pdf"
    except Exception:
        original = BASELINE_DIR / "figures" / "Figure7_Workflow.pdf"
    ledger = accounting_panel(counts, row_count, qc, queue)
    ledger.axes[0].set_position([.14, .17, .21, .42])
    ledger.axes[1].set_position([.47, .17, .21, .42])
    ledger.axes[2].set_position([.80, .17, .18, .42])
    part = pdf_part(ledger, temp / "accounting.pdf", (0, .35, 7.25, 2.85))
    combine_panels([original, part], 1, output, gap=6)


def original_source_plot(analysis, sub, audit, output, temp):
    frames = {scale: analysis.loc[analysis["time_resolution"].eq(scale)]
              for scale in ("monthly", "annual")}
    frames["submonthly"] = sub
    spatial = analysis.loc[analysis["qc_location_status"].eq("pass")
                           & analysis["lat"].between(-90, 90) & analysis["lon"].between(-180, 180)]
    original = figures_dataset.plot_figure12(frames, spatial)
    original.texts[-1].set_text("Panels c–f show eligible observations. See the source data for all archived high values.")
    legacy_titles = {
        "(a)": "(c) Ba/Ca latitude–time density",
        "(b)": "(d) Ba/Ca by resolution",
        "(d)": "(f) Havannah Island: submonthly Ba/Ca",
    }
    for ax in original.axes:
        current = ax.get_title(loc="left")
        if current.startswith("(c)"):
            ax.set_title(current.replace("(c)", "(e)", 1), loc="left")
            for note in ax.texts:
                note.set_text(note.get_text().replace("source check pending", "see panel b for sampled records"))
        elif current[:3] in legacy_titles:
            ax.set_title(legacy_titles[current[:3]], loc="left")
    # The reused 14-inch layout is reduced in the composite. Match its lettering at final width.
    rescale_for_width(original, 14)
    comparison = source_tables(audit)
    combine_panels([
        pdf_part(comparison, temp / "source_comparison.pdf", (0, 0, 7.4, 2.95)),
        pdf_part(original, temp / "baca_overview.pdf", (0, 0, 14, 10)),
    ], 5, output, gap=5)


def original_submonthly_plot(sub, run, external, output):
    sst_path = loader.resolve_grid_file(run, loader.SST_NC_NAME, external)
    sst = loader.open_grid_dataset(sst_path) if sst_path else None
    try:
        fig = figures_dataset.plot_figure11(sub, sst)
    finally:
        if sst is not None:
            sst.close()
    fig.set_size_inches(7.35, 4.95)
    intervals, composition_ax, example, seasonal = fig.axes
    fig.delaxes(composition_ax)
    example.set_position([.10, .54, .85, .38])
    intervals.set_position([.10, .16, .42, .24])
    seasonal.set_position([.56, .16, .39, .24])
    example.set_title(example.get_title(loc="left").replace("(c)", "(a)", 1), loc="left")
    intervals.set_title(intervals.get_title(loc="left").replace("(a)", "(b)", 1), loc="left")
    seasonal.set_title(seasonal.get_title(loc="left").replace("(d)", "(c)", 1), loc="left")
    intervals.set_xlabel("Sampling interval (days)", fontsize=8)
    intervals.set_ylabel("Observations", fontsize=8)
    intervals.yaxis.set_major_locator(plt.MaxNLocator(4))
    intervals.tick_params(labelsize=7)
    seasonal.tick_params(labelsize=7)
    seasonal.set_xlabel("Seasonal-cycle r vs SST", fontsize=8)
    seasonal.set_ylabel("Series", fontsize=8)
    n_laser = int(sub["testing_method"].map(figures_dataset._method_short).eq("LA-ICP-MS").sum())
    fig.texts[-1].set_text(f"LA-ICP-MS reported for {n_laser:,}/{len(sub):,} submonthly rows. Submonthly records are "
                           "excluded from the monthly/annual Figs 7–9; density, composition and physical realism "
                           "are validated here.")
    fig.texts[-1].set_fontsize(7.5)
    save_figure(fig, 6, output)


def original_early_plot(analysis, intervals, output, temp):
    fields = {"Year": "year_CE", "lat": "geo_latitude", "lon": "geo_longitude", **UNIT_COLUMNS}
    original = figures_dataset.plot_figure14(analysis.rename(columns=fields))
    axes = list(original.axes)
    # Keep the existing resolution and span panels below the dated intervals.
    for ax in [axes[0], *axes[3:]]:
        original.delaxes(ax)
    axes[1].set_position([.07, .43, .38, .45])
    axes[2].set_position([.56, .43, .38, .45])
    axes[1].set_title(axes[1].get_title(loc="left").replace("(b)", "(c)", 1), loc="left")
    axes[2].set_title(axes[2].get_title(loc="left").replace("(c)", "(d)", 1), loc="left")
    rescale_for_width(original, 14)
    original.texts[-1].remove()
    combine_panels([
        pdf_part(intervals, temp / "dating_intervals.pdf", (0, 0, 7.35, 6.65)),
        pdf_part(original, temp / "early_overview.pdf", (0, 2.85, 14, 6.70)),
    ], EARLY_APPENDIX, output, gap=3)


def original_variance_plot(spread, early, output, temp):
    maps = figures_validation.plot_variance_decomposition(
        spread.loc[spread["policy"].eq("Baseline")], compact_titles=True)
    rescale_for_width(maps, 18)
    maps.canvas.draw()
    map_columns = [(ax.get_position().x0, ax.get_position().width) for ax in maps.axes[:3]]
    panel = variance(spread, early, output, map_columns)
    combine_panels([
        pdf_part(maps, temp / "variance_maps.pdf", (0, 0, 18, 10)),
        pdf_part(panel, temp / "variance_sensitivity.pdf", (0, 0, 7.4, 3.1)),
    ], 8, output, gap=4)


def local_sites(pairs):
    clean = pairs.loc[pairs["status"].eq("No shared ID or exact values detected")]
    common = ["proxy", "resolution"]
    pair_sites = pd.concat([
        clean[[*common, "entity_a", "r"]].rename(columns={"entity_a": "entity_id"}),
        clean[[*common, "entity_b", "r"]].rename(columns={"entity_b": "entity_id"}),
    ])
    sites = pair_sites.groupby(["entity_id", *common])["r"].agg(
        median_abs_r=lambda r: r.abs().median(), n_partners="size").reset_index()
    identities = sites["entity_id"].str.rsplit(" | ", n=2, expand=True)
    sites["lat"] = pd.to_numeric(identities[1], errors="coerce")
    sites["lon"] = pd.to_numeric(identities[2], errors="coerce")
    return clean, sites.dropna(subset=["lat", "lon"])


def original_local_plot(pair, early, output, temp):
    clean, sites = local_sites(pair)
    clean_early, early_sites = local_sites(early)
    fig = figures_validation.plot_local_reproducibility(sites, early=early_sites)
    rescale_for_width(fig, 16)
    fig.texts[-1].set_text(f"Within 50 km and ≥20 years of shared observations; {len(clean)} pairs "
                           f"({len(clean_early)} pre-1850 pairs re-paired on <1850 CE observations, g–i; "
                           "circle = monthly, diamond = annual).\nExact repeats and unresolved near-identical "
                           "pairs are listed in the data. Pair-level signed r in local_pairs_details.csv; "
                           "map colour = site median |r|.")
    combine_panels([pdf_part(fig, temp / "local.pdf", (0, 0, 16, 11.2))], 9, output)
    return len(clean), len(clean_early), len(early_sites)


def d13c_coverage_plot(data, output):
    frames = {"monthly": data["monthly"], "annual": data["annual"]}
    spatial = pd.concat(frames.values()).loc[lambda d: d["d13C"].notna()]
    fig = plt.figure(figsize=(7.35, 5.6))
    cell = fig.add_gridspec(1, 1, left=.09, right=.95, top=.93, bottom=.10)[0, 0]
    figures_validation.plot_proxy_coverage_quality(fig, cell, frames, "d13C", spatial, prefix="a")
    for ax in fig.axes:
        label = ax.get_title(loc="left")
        for i, letter in enumerate("abcd"):
            label = label.replace(f"(a{i + 1})", f"({letter})")
        ax.set_title(label, loc="left")
    fig.text(.5, .02, "δ¹³C coverage and continuity (monthly + annual, value-level QC); density requires verified "
             "coordinates. Parity panel for the added proxy: gridded correlation excludes δ¹³C (Figs 7/A1); "
             "internal quality in Figs 8–9.", ha="center", fontsize=7)
    save_figure(fig, D13C_APPENDIX, output)
    segments = figures_validation._proxy_segments(frames, "d13C")
    return len(spatial), len(segments)


def methods_plot(analysis, output, temp):
    fig = figures_dataset.plot_figure13(analysis.rename(columns=UNIT_COLUMNS),
                                        method_colors=METHOD_COLORS, drop_share=True)
    rescale_for_width(fig, 14)
    combine_panels([pdf_part(fig, temp / "methods.pdf", (0, 0, 14, 4.9))], METHODS_APPENDIX, output)
    return float(analysis["testing_method"].notna().mean())


def metadata_plot(run, output, temp):
    raw = loader.load_consolidated_raw(run)
    eligibility = (
        ["Fig6", "Fig7–9", "Fig5c–f", "FigA2", "FigA3", "Fig2–3"],
        {"submonthly": [1, 0, 1, 1, 0, 1], "monthly": [0, 1, 1, 1, 1, 1],
         "bimonthly": [0, 0, 0, 1, 0, 1], "annual": [0, 1, 1, 1, 1, 1],
         "seasonal": [0, 0, 0, 1, 0, 1], "biannual": [0, 0, 0, 1, 0, 1],
         "irregular": [0, 0, 0, 1, 0, 1], "discrete_samples": [0, 0, 0, 1, 0, 1],
         "unknown": [0, 0, 0, 1, 0, 1]})
    dm_fill = 100 * raw["paleoData_dating_method"].notna().mean()
    bullets = [
        f"• Chronological control is sparse: dating method filled for only {dm_fill:.1f}% of raw rows; "
        "age-model uncertainty is a dataset limitation and is NOT figured.",
        "• Figs 7–9 (gridded correlation, band-pass variance, local agreement) use monthly/annual only: "
        "the band-pass design needs a regular monthly grid; bimonthly/irregular/seasonal series would "
        "alias the seasonal band.",
        "• Submonthly is validated in Fig6 and enters the Fig7 ensemble seasonal check; Ba/Ca coverage "
        "and audit are in Fig5 (excluded from Figs 7–9 for proxy-physics reasons); δ¹³C coverage and "
        "continuity are in A3.",
        "• Pre-1850 quality is carried by internal analyses (Figs 8–9, A2), never by instrumental "
        "correlation.",
    ]
    disclosure = "Disclosure / limitations\n\n" + "\n\n".join(
        textwrap.fill(b, width=64, subsequent_indent="  ") for b in bullets)
    fig = figures_dataset.plot_figure15(
        raw, eligibility=eligibility, disclosure=disclosure, metadata_colors=METADATA_COLORS)
    fig.axes[3].texts[0].set_fontsize(6.5)
    rescale_for_width(fig, 14)
    combine_panels([pdf_part(fig, temp / "metadata.pdf", (0, 0, 14, 10))], METADATA_APPENDIX, output)
    return dm_fill


def consistency_plot(run, output, temp):
    prep = figures_structure.load_and_prep_data(run)
    hexfig = figures_structure.plot_stats_proxy_correlation(prep)
    paired = prep.dropna(subset=["Sr/Ca", "d18O"])
    domain = paired[["Sr/Ca", "d18O"]].quantile([.05, .95])
    hexfig.suptitle("(a) Paired Sr/Ca–δ¹⁸O joint distribution", fontweight="bold", y=1.02)
    hexfig.text(.5, .01, f"n={len(paired):,} paired observations; 5–95% domain Sr/Ca "
                f"{domain.loc[.05, 'Sr/Ca']:.2f}–{domain.loc[.95, 'Sr/Ca']:.2f} mmol/mol, "
                f"δ¹⁸O {domain.loc[.05, 'd18O']:.2f}–{domain.loc[.95, 'd18O']:.2f} ‰ VPDB.",
                ha="center", fontsize=8)
    rescale_for_width(hexfig, hexfig.get_size_inches()[0])
    repfig = figures_structure.plot_stats_representative_timeseries(prep)
    repfig.axes[0].set_title(repfig.axes[0].get_title(loc="left").replace(
        "Representative Time-Series:", "(b) Representative paired series:"), loc="left")
    rescale_for_width(repfig, 10)
    combine_panels([pdf_part(hexfig, temp / "hex.pdf"), pdf_part(repfig, temp / "rep.pdf")],
                   CONSISTENCY_APPENDIX, output, gap=6)
    return len(paired)


def load_archive(run):
    src = run / "consolidated" / "all_data_combined.csv"
    fields = {
        *loader.SUMMARY_COL_MAP, "time_resolution", "paper_hash", "source_file", "sheet_name",
        "source_record_id", "data_source", "source_time_value", "source_time_column", "source_time_unit",
        "source_line", "source_columns", "source_time_reference_year", "source_time_date_order",
        "source_sheet_name", "source_header_line", "mapping_route",
        "observation_id", "record_type", "qc_flag",
        "qc_time_status", "qc_location_status", "paleoData_dating_method", "testing_method",
        "native_interval_years",
    }
    prefixes = ("Ba/Ca_value", "δ13C_value", "δ18O_value", "Sr/Ca_value", "sea_surface_temperature_value")
    frame = pd.read_csv(src, usecols=lambda col: col in fields or col.startswith(prefixes),
                        dtype={"source_time_value": "string"}, low_memory=False)
    frame = frame.rename(columns=loader.SUMMARY_COL_MAP)
    loader.add_series_identity(frame)
    return loader._classify_indicators(frame)


def age_summary(frame):
    has_method = frame["paleoData_dating_method"].fillna("").str.strip().ne("")
    return {
        "early_rows": len(frame), "rows_with_method": int(has_method.sum()),
        "early_records": frame["entity_id"].nunique(),
        "records_with_method": frame.loc[has_method, "entity_id"].nunique(),
    }


def archive_accounting(frame, run, output):
    files = pd.read_csv(run / "input_manifest.csv").drop_duplicates("file")
    status = files["status"].map({
        "review": "Held for review", "excluded": "Excluded", "context": "Context only",
        "unsupported": "Unsupported / failed", "failed": "Unsupported / failed",
    }).fillna("Other disposition")
    status = status.mask(files["output_rows"].gt(0), "Contributed rows")
    order = ["Contributed rows", "Context only", "Held for review", "Excluded",
             "Unsupported / failed", "Other disposition"]
    file_counts = status.value_counts().reindex(order, fill_value=0)
    labels = {0: "Clean", 2: "Flagged outlier", 3: "Outside year range", 4: "Pending review",
              5: "Dropped", 6: "Context only"}
    row_counts = frame["qc_flag"].map(labels).fillna("Other QC flag").value_counts()
    for counts, name, total in ((file_counts, "workflow_files", len(files)), (row_counts, "workflow_rows", len(frame))):
        assert counts.sum() == total
        counts.rename("count").to_csv(output / f"{name}.csv", index_label="disposition", encoding="utf-8-sig")
    return file_counts, row_counts


def crosswalk_for(run, output):
    # Cache is accepted only for the same consolidated file and pinned CoralHydro2k archive.
    source = run / "consolidated" / "all_data_combined.csv"
    digest = coral_comparison.digest(source)
    baseline_digest = coral_comparison.digest(BASELINE_REFERENCE)
    if baseline_digest != coral_comparison.BASELINE_SHA256:
        raise ValueError("CoralHydro2k v1.0.1 校验值不符")
    cache = BASELINE_DIR / "figures"
    manifest = cache / "comparison_summary.json"
    summary = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else {}
    cached = (summary.get("source_csv_sha256") == digest
              and (cache / "sequence_crosswalk.csv").is_file()
              and all((cache / f"Figure4_GoldenSet_Validation.{ext}").is_file() for ext in ("pdf", "png"))
              and summary.get("baseline_sha256") == baseline_digest)
    if cached:
        shutil.copyfile(cache / "sequence_crosswalk.csv", output / "sequence_crosswalk.csv")
    else:
        reference = coral_comparison.baseline_series(BASELINE_REFERENCE)
        local = coral_comparison.local_series(source, ROOT)
        rows, _, _ = coral_comparison.compare(reference, local)
        pd.DataFrame(rows).to_csv(output / "sequence_crosswalk.csv", index=False, encoding="utf-8-sig")
    frame = pd.read_csv(output / "sequence_crosswalk.csv")
    assert len(frame) == 233 and not frame.duplicated(["entity", "proxy"]).any()
    exact = frame.loc[frame["status"].eq("双方一致")]
    exact.groupby("proxy")[["matched_year_value_pairs", "matched_after_proxy_qc"]].sum().to_csv(
        output / "benchmark_exact_pair_counts.csv", encoding="utf-8-sig")
    return frame, digest, cached


def scope_text(seasonal, concurrent, enso, sst_years):
    text = [
        "Instrumental comparisons and seasonal consistency",
        "",
        f"同月或同年的器测对照只对有共同观测的序列计算。ERSSTv5 当前本地格点覆盖 {sst_years[0]}–{sst_years[1]} 年。"
        "Sr/Ca 和 δ¹⁸O 与最近格点的 SST 对照，δ¹⁸O 另与 SSS 对照。相关结果描述共同变动。"
        "月尺度原序列含共同季节循环。每条序列的重叠数和相关系数见 instrumental_correlations.csv。",
        "",
        "季节气候态使用每个年月的观测均值，以免亚月高密度月份获得额外权重。共同期检验要求至少 "
        "12 个共同年月、覆盖至少 6 个日历月和 3 个年份。对器测覆盖之外的时段，"
        "使用 1981–2010 年同地格点气候态作现代类比，要求至少 8 个日历月各有至少 3 年的数据。"
        "现代类比检验季节变化形状和相位的一致性，证据范围限于现代参照。",
        "",
    ]
    if not concurrent.empty:
        key = ["resolution", "proxy", "env_target"]
        corr = "r" if "r" in concurrent else "R"
        if key and corr in concurrent:
            for names, group in concurrent.groupby(key):
                names = names if isinstance(names, tuple) else (names,)
                text.append(f"同期格点 · {' / '.join(map(str, names))}，n={len(group)}，r 中位数 {group[corr].median():.2f}。")
    for (kind, resolution, proxy), group in seasonal.groupby(["comparison", "resolution", "proxy"]):
        p = {"d18O": "δ¹⁸O"}.get(proxy, proxy)
        text.append(f"{kind} · {resolution} {p}，n={len(group)}，r 中位数 {group['r'].median():.2f}。")
    text.extend(["", "ENSO 使用 Niño 3.4 区域面积加权 SST 距平（1981–2010 气候态），"
                 "与珊瑚 δ¹⁸O 逐月去季节化后的距平配对。沿用 ≥60 月、|r|≥0.5、名义 p<0.001 的候选规则，"
                 "取满足条件且共同月份最多的序列。该 p 值用于筛选示例，时间自相关与筛选应在推断时另行处理。"])
    if enso:
        text.append(f"代表站 {enso['site']}，{enso['start']} 至 {enso['end']}，"
                    f"{enso['n']:,} 个共同月，Pearson r={enso['r']:.3f}，名义 p={enso['p']:.3g}。")
    else:
        text.append("该批次没有通过上述筛选的 ENSO 代表序列。")
    return "\n".join(text)


def write_index(output, run, digest, descriptions, text8):
    sections = []
    for n, stem in STEMS.items():
        notes = (f'<details><summary>器测、季节气候态与 ENSO 的正文说明</summary><pre>{html.escape(text8)}</pre>'
                 f'<a href="Figure7_text.txt">正文说明</a> · <a href="{APPENDIX}.pdf">完整六联相关图</a></details>'
                 if n == 7 else "")
        sections.append(f'<section><h2>图 {n} · {stem.split("_", 1)[1].replace("_", " ")}</h2>'
                        f'<p>{html.escape(descriptions[n])}</p><a href="{stem}.pdf">PDF</a> · '
                        f'<a href="{stem}.png">PNG</a><img src="{stem}.png" alt="Figure {n}" loading="lazy">'
                        f'{notes}</section>')
    appendix_titles = {
        APPENDIX: ("A1", "器测格点相关全图"),
        EARLY_APPENDIX: ("A2", "早期记录与定年信息"),
        D13C_APPENDIX: ("A3", "δ13C 覆盖与连续性"),
        METHODS_APPENDIX: ("A4", "分析方法组成"),
        METADATA_APPENDIX: ("A5", "元数据完整性与披露"),
        CONSISTENCY_APPENDIX: ("A6", "内部一致性检查"),
        NETWORK_APPENDIX: ("A7", "网络尺度重建潜力"),
    }
    for stem, (letter, title) in appendix_titles.items():
        sections.append(f'<section><h2>附图 {letter} · {title}</h2><p>{html.escape(descriptions[letter])}</p>'
                        f'<a href="{stem}.pdf">PDF</a> · <a href="{stem}.png">PNG</a>'
                        f'<img src="{stem}.png" alt="Figure {letter}" loading="lazy"></section>')
    files = " · ".join(f'<a href="{p.name}">{p.name}</a>' for p in sorted(output.glob("*.csv")))
    page = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>ESSD 候选主图</title>
<style>
body{{max-width:1000px;margin:35px auto;padding:0 20px;color:#172737;background:#fafbfc;
font:16px/1.6 "Microsoft YaHei",sans-serif}} section{{background:white;border:1px solid #dbe3ea;
padding:18px 24px;margin:20px 0}} img{{max-width:100%;display:block;margin:12px auto}}
h2{{font-size:21px}} a{{color:#245c8c}} pre{{white-space:pre-wrap;font:inherit}}
code{{overflow-wrap:anywhere}}
</style><h1>ESSD 候选主图</h1><p>数据批次 <code>{run.name}</code><br>
合并表 SHA-256 <code>{digest}</code></p><p><a href="generate.py">生成入口</a> ·
<a href="source_audit.py">源表核验</a> · <a href="plots.py">绘图</a> ·
<a href="series_analysis.py">序列分析</a> · <a href="summary.json">结果统计</a></p>
{"".join(sections)}
<section><h2>配套数据</h2><p>{files}</p></section></html>'''
    (output / "index.html").write_text(page, encoding="utf-8")


def schema_table(frame, output):
    """字段级 schema 表：字段名/类型/非空数/填充率/示例值（补充材料 A8 的数据底表）。"""
    rows = []
    for col in frame.columns:
        s = frame[col]
        rows.append({"field": col, "dtype": str(s.dtype), "non_null": int(s.notna().sum()),
                     "fill_rate": round(float(s.notna().mean()), 4),
                     "example": str(s.dropna().iloc[0])[:60] if s.notna().any() else ""})
    pd.DataFrame(rows).to_csv(output / "schema_fields.csv", index=False, encoding="utf-8-sig")


def provenance_table(frame, output):
    """逐记录溯源表：来源文件/站点/坐标/指标/行数/首末年/分辨率/DOI（补充材料 A9 的数据底表）。"""
    rows = []
    for rid, sub in frame.groupby("source_record_id", sort=True):
        proxies = [p for p in ("Sr/Ca", "d18O", "d13C", "Ba/Ca", "SST") if p in sub and sub[p].notna().any()]
        rows.append({"record_id": rid,
                     "source_files": "|".join(sorted(sub["source_file"].unique())),
                     "site": sub["Site"].iloc[0] if "Site" in sub else "",
                     "lat": round(float(sub["lat"].mean()), 3) if sub["lat"].notna().any() else "",
                     "lon": round(float(sub["lon"].mean()), 3) if sub["lon"].notna().any() else "",
                     "proxies": "|".join(proxies), "n_rows": len(sub),
                     "first_year": float(sub["Year"].min()), "last_year": float(sub["Year"].max()),
                     "resolutions": "|".join(sorted(sub["time_resolution"].dropna().unique())),
                     "doi": sub["pub1_doi"].iloc[0] if "pub1_doi" in sub and sub["pub1_doi"].notna().any() else ""})
    pd.DataFrame(rows).to_csv(output / "record_provenance.csv", index=False, encoding="utf-8-sig")


def network_potential(correlations, output):
    """网络尺度重建潜力：把记录–最近格点相关按 2° 格点×3000 km 半径聚合（覆盖度代理，方法披露）。

    每格点取半径内记录的中位 |r| 与名义显著占比；显著占比<0.5 的格点灰显。
    此为聚合代理而非逐格点直接相关，局限在图注与正文披露。
    """
    lat_c = np.arange(-39, 40, 2)
    lon_c = np.arange(-179, 180, 2)
    rows = []
    for res in ("Monthly", "Annual"):
        for proxy in ("Sr/Ca", "d18O"):
            sub = correlations[(correlations["resolution"] == res) & (correlations["proxy"] == proxy)
                               & (correlations["env_target"] == "SST")]
            if sub.empty:
                continue
            slat = np.radians(sub["lat"].to_numpy())
            slon = np.radians(sub["lon"].to_numpy())
            r = sub["r"].to_numpy()
            p = sub["p"].to_numpy()
            for la in lat_c:
                dla = slat - np.radians(la)
                cosla = np.cos(slat) * np.cos(np.radians(la))
                for lo in lon_c:
                    dlo = slon - np.radians(lo)
                    a = np.sin(dla / 2) ** 2 + cosla * np.sin(dlo / 2) ** 2
                    dist = 6371 * 2 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))
                    m = dist <= 3000
                    n = int(m.sum())
                    if n == 0:
                        continue
                    rows.append({"resolution": res, "proxy": proxy, "lat": la, "lon": lo, "n": n,
                                 "median_abs_r": float(np.median(np.abs(r[m]))),
                                 "frac_sig": float((p[m] < 0.10).mean())})
    net = pd.DataFrame(rows)
    net.to_csv(output / "network_potential.csv", index=False, encoding="utf-8-sig")
    fig, axes = plt.subplots(2, 2, figsize=(14, 9),
                             subplot_kw={"projection": figures_validation.ccrs.Robinson(central_longitude=180)})
    panels = [("Monthly", "Sr/Ca", "(a) Monthly Sr/Ca"), ("Monthly", "d18O", "(b) Monthly δ¹⁸O"),
              ("Annual", "Sr/Ca", "(c) Annual Sr/Ca"), ("Annual", "d18O", "(d) Annual δ¹⁸O")]
    from scipy.spatial import cKDTree
    from scipy.interpolate import griddata
    from scipy.ndimage import gaussian_filter
    import matplotlib.path as mpath
    # 陆地严格遮罩：细网格点落在陆地多边形内者置 NaN，热力图不盖陆地。
    land_path = mpath.Path.make_compound_path(*[
        mpath.Path(np.asarray(g.exterior.coords)) for g in figures_validation.cfeature.LAND.geometries()])
    fl = np.arange(-39.5, 40, 1.0)
    fo = np.arange(-179.5, 180, 1.0)
    FL, FO = np.meshgrid(fl, fo, indexing="ij")
    fine_lonlat = np.column_stack([FO.ravel(), FL.ravel()])
    ocean = ~land_path.contains_points(fine_lonlat)

    def xyz(lat, lon):
        la, lo = np.radians(lat), np.radians(lon)
        return np.column_stack([np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)])

    fine_xyz = xyz(FL.ravel(), FO.ravel())
    lat_edges_f = np.arange(-40, 41, 1.0)
    lon_edges_f = np.arange(-180, 181, 1.0)
    mesh = None
    for ax, (res, proxy, title) in zip(axes.flat, panels):
        g = net[(net["resolution"] == res) & (net["proxy"] == proxy)]
        sup = g[g["frac_sig"] >= 0.5]
        sub = correlations[(correlations["resolution"] == res) & (correlations["proxy"] == proxy)
                           & (correlations["env_target"] == "SST")]
        tree = cKDTree(xyz(sub["lat"].to_numpy(), sub["lon"].to_numpy()))
        d = tree.query(fine_xyz)[0]
        km = 2 * 6371 * np.arcsin(np.clip(d / 2, 0, 1))
        covered = (km <= 3000) & ocean
        src = np.column_stack([sup["lon"], sup["lat"]])
        vals = griddata(src, sup["median_abs_r"].to_numpy(), fine_lonlat[covered], method="linear")
        nn = griddata(src, sup["median_abs_r"].to_numpy(), fine_lonlat[covered], method="nearest")
        vals = np.where(np.isfinite(vals), vals, nn)
        field = np.full(FL.size, np.nan)
        field[covered] = vals
        field = field.reshape(FL.shape)
        mask = np.isfinite(field).astype(float)
        sm = gaussian_filter(np.nan_to_num(field) * mask, 1.2) / np.maximum(gaussian_filter(mask, 1.2), 1e-6)
        sm = np.where(mask > 0, sm, np.nan)
        figures_validation._configure_correlation_map(ax)
        mesh = ax.pcolormesh(lon_edges_f, lat_edges_f, np.ma.masked_invalid(sm), cmap="YlOrRd", vmin=0, vmax=1,
                             transform=figures_validation.ccrs.PlateCarree(), zorder=0.5)
        ax.set_title(f"{title} ({len(sup)} supported boxes)", loc="left")
    fig.subplots_adjust(bottom=0.10, hspace=0.25, wspace=0.15)
    cax = fig.add_axes([0.27, 0.055, 0.46, 0.014])
    fig.colorbar(mesh, cax=cax, orientation="horizontal",
                 label="Median |r| (smoothed over record-covered ocean, ±40°)")
    fig.text(0.5, 0.015, "Smoothed ocean heatmap (±40°): median |r| of supported 2° boxes interpolated over record-covered "
             "ocean (≤3000 km) and gaussian-smoothed; land masked; white = no record coverage. Aggregation + interpolation "
             "proxy, not per-box direct correlation; see text.", ha="center", fontsize=7)
    save_figure(fig, NETWORK_APPENDIX, output)
    return net


def export_index_pdf(output):
    """index.html → index.pdf（A4、每图一页、图片按页自适应），供对外分享。"""
    import subprocess
    from app.analysis.figures_workflow import _browser_binary
    browser = _browser_binary()
    if browser is None:
        raise FileNotFoundError("导出 index.pdf 需要 Chrome 或 Edge（headless）")
    page = (output / "index.html").read_text(encoding="utf-8")
    page = page.replace("<details>", "<details open>")
    print_css = """<style>
@page{size:A4;margin:12mm}
body{max-width:none;margin:0;padding:0;background:#fff;font:10pt/1.5 "Microsoft YaHei",sans-serif}
h1{font-size:16pt} h2{font-size:12.5pt}
section{border:none;padding:0;margin:0;break-before:page}
img{max-width:100%;max-height:225mm;width:auto;height:auto;display:block;margin:4mm auto}
pre{font:8.5pt/1.45 "Microsoft YaHei",sans-serif;white-space:pre-wrap;break-inside:avoid}
a{color:#245c8c;text-decoration:none}
</style>"""
    page = page.replace("<h1>", print_css + "<h1>", 1)
    html_path = output / "_index_print.html"
    html_path.write_text(page, encoding="utf-8")
    pdf_path = output / "index.pdf"
    with TemporaryDirectory(prefix="essd_pdf_") as tmp:
        subprocess.run([browser, "--headless=new", "--disable-gpu", "--no-sandbox",
                        "--hide-scrollbars", f"--user-data-dir={tmp}/profile",
                        "--virtual-time-budget=8000", f"--print-to-pdf={pdf_path}",
                        "--no-pdf-header-footer", html_path.as_uri()],
                       check=True, timeout=300, capture_output=True)
    html_path.unlink()
    if not pdf_path.is_file() or pdf_path.stat().st_size == 0:
        raise RuntimeError("index.pdf 导出失败")
    print(f"index.pdf ready ({pdf_path.stat().st_size / 1e6:.1f} MB)", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, default=DEFAULT_RUN)
    parser.add_argument("--external-data", type=Path, default=EXTERNAL)
    parser.add_argument("--output", type=Path, default=HERE)
    args = parser.parse_args()
    run, output = args.run.resolve(), args.output.resolve()
    if output == (run / "figures").resolve():
        parser.error("选择独立输出目录，保留归档的 figures 目录")
    output.mkdir(parents=True, exist_ok=True)
    logging.getLogger("fontTools").setLevel(logging.WARNING)
    style.set_pub_style()
    style.plt.rcParams.update({"savefig.dpi": 300, "pdf.fonttype": 42})
    with TemporaryDirectory(prefix="essd_panels_") as tmp:
        build(args, run, output, Path(tmp))


def build(args, run, output, temp):
    frame = load_archive(run)
    coverage = loader.load_figure1_data(run)
    # load_figure1_data 保留原始指标列名，这里归一到短名供显式归类使用。
    coverage = coverage.rename(columns={"Ba/Ca (μmol/mol)": "Ba/Ca", "SST (°C)": "SST"})
    # Other 不再作为汇总类：无三核心指标的观测按 Ba/Ca / SST 显式归类。
    other = coverage["Group"].eq("Other")
    ba, sst = coverage["Ba/Ca"].notna(), coverage["SST"].notna()
    explicit = np.select([ba & ~sst, sst & ~ba, ba & sst],
                         ["Ba/Ca only", "SST only", "Ba/Ca + SST"], default="Other")
    coverage["Group"] = coverage["Group"].where(~other, explicit)
    # 入图行（Dated）必含至少一个合格指标值，故必可显式归类；未入图的 Other 行不参与绘图。
    assert not coverage.loc[coverage["Dated"], "Group"].eq("Other").any(), "存在无法显式归类的入图 Other 行"
    save_figure(plot_figure_1_optimized(coverage, split_other=True), 2, output)
    durations = original_structure(run, frame, output, temp)
    counts, qc = archive_accounting(frame, run, output)
    accounting = figures_workflow.load_accounting(run)
    assert accounting["rows_total"] == len(frame)
    queue = pd.Series({"Held values": accounting["review_values"], "Held rows": accounting["review_rows"],
                       "Held tables": accounting["review_tables"],
                       "Other pending": accounting["review_other_pending"],
                       "Applied / excluded": accounting["review_applied"]})
    assert int(queue.sum()) == accounting["review_total"]
    original_workflow(counts, len(frame), qc, queue, output, temp)
    crosswalk, digest, cached = crosswalk_for(run, output)
    if cached:
        original = BASELINE_DIR / "figures" / "Figure4_GoldenSet_Validation"
        for suffix in (".pdf", ".png"):
            shutil.copyfile(original.with_suffix(suffix), (output / STEMS[4]).with_suffix(suffix))
    else:
        # 非缓存分支与缓存分支同族：六联全版图，避免换批次时图件降级。
        coral_comparison.run_comparison(run, BASELINE_REFERENCE, output, figure_stem=STEMS[4], source_root=ROOT)
    print("Figure 4 ready", flush=True)
    audit, tail = audit_source_tables(frame, ROOT, output)

    # Use consolidated rows for all remaining figures, including the verified age and value filters.
    analysis = loader.filter_analysis_data(frame, context="（ESSD）")
    sub = analysis.loc[analysis["time_resolution"].eq("submonthly")].copy()
    loader._add_calendar_month(sub)
    original_source_plot(analysis, sub, audit, output, temp)
    geo = analysis.loc[analysis["qc_location_status"].eq("pass")
                       & analysis["lat"].between(-90, 90) & analysis["lon"].between(-180, 180)]
    data = {}
    for scale in ("monthly", "annual"):
        scale_frame = geo.loc[geo["time_resolution"].eq(scale)].copy()
        loader._add_calendar_month(scale_frame)
        data[scale] = scale_frame
    early = analysis.loc[analysis["Year"].lt(EARLY_YEAR)]
    early_data = {scale: f.loc[f["Year"].lt(EARLY_YEAR)] for scale, f in data.items()}
    spread, spread_early = variance_results(data["monthly"]), variance_results(early_data["monthly"])
    pair, pair_early = pair_results(data), pair_results(early_data)
    for name, full, old in (("variance", spread, spread_early), ("local_pairs", pair, pair_early)):
        pd.concat([full.assign(scope="All eligible"), old.assign(scope="Pre-1850 only")]).to_csv(
            output / f"{name}_details.csv", index=False, encoding="utf-8-sig")
    fig, early_table = early_records(early, spread_early, pair_early, output)
    original_early_plot(analysis, fig, output, temp)
    original_variance_plot(spread, spread_early, output, temp)
    n_pairs, n_early_pairs, n_early_sites = original_local_plot(pair, pair_early, output, temp)
    seasonal, concurrent, examples, enso, enso_pairs, years = environmental_results(
        run, args.external_data, data, sub, output)
    original_submonthly_plot(sub, run, args.external_data, output)
    save_figure(environmental_consistency(concurrent, examples, enso, enso_pairs, seasonal), 7, output)
    appendix = figures_validation.plot_correlation_maps(concurrent)
    appendix.texts[-1].set_text("Pearson r of paired observations. Monthly records retain the seasonal cycle. "
                                "Two-sided nominal p values do not account for temporal autocorrelation.\n"
                                "δ¹³C is excluded here (proxy physics); its coverage and internal quality "
                                "are in Figs 8–9 and Appendix A3.")
    rescale_for_width(appendix, 14)
    combine_panels([pdf_part(appendix, temp / "corr_maps.pdf", (0, 0, 14, 10))], APPENDIX, output)
    text8 = scope_text(seasonal, concurrent, enso, years)
    (output / "Figure7_text.txt").write_text(text8 + "\n", encoding="utf-8")
    d13c_obs, d13c_segments = d13c_coverage_plot(data, output)
    methods_fill = methods_plot(analysis, output, temp)
    dating_fill = metadata_plot(run, output, temp)
    n_paired_values = consistency_plot(run, output, temp)
    net = network_potential(concurrent, output)
    schema_table(frame, output)
    provenance_table(frame, output)

    dates = age_summary(early)
    common = seasonal.loc[seasonal["comparison"].eq("Common-period seasonal climatology")]
    ensemble = {f"{resolution} {proxy}": {"n": int(len(group)), "median_r": float(group["r"].median())}
                for (resolution, proxy), group in common.groupby(["resolution", "proxy"])}
    main_stats = {
        "batch": run.name, "sha256": digest, "archive_rows": len(frame),
        "sst_values": int(frame["SST"].notna().sum()),
        "sst_only_rows": int((frame["SST"].notna() &
                              ~frame[["Sr/Ca", "d18O", "d13C", "Ba/Ca"]].notna().any(axis=1)).sum()),
        "dated_rows": int(coverage["Dated"].sum()), "early": dates,
        "early_max_observed_span_years": float((early_table["last"] - early_table["first"]).max()),
        "early_records_eligible_for_variance": int(early_table["variance_eligible"].sum()),
        "early_records_in_local_pairs": int(early_table["local_pair_eligible"].sum()),
        "record_duration_series": durations.to_dict(),
        "record_duration_gt100yr": int(durations.iloc[3:].sum()),
        "review_queue": queue.to_dict(),
        "local_pairs": n_pairs, "early_local_pairs": n_early_pairs, "early_local_sites": n_early_sites,
        "seasonal_ensemble": ensemble,
        "d13c_coverage_obs": d13c_obs, "d13c_segments": d13c_segments,
        "methods_fill_rate": methods_fill, "dating_fill_rate_raw": dating_fill,
        "paired_srca_d18o_values": n_paired_values,
        "baca_high_count": len(tail), "baca_high_max": float(tail["Ba/Ca"].max()),
        "baca_high_pass_verified": int((tail["Ba/Ca_value_qc_status"].eq("pass") &
                                       tail["Ba/Ca_value_unit_status"].eq("verified")).sum()),
        "source_audit_samples": audit["proxy"].value_counts().to_dict(),
        "source_audit_verdicts": audit["verdict"].value_counts().to_dict(),
        "enso": enso,
    }
    (output / "summary.json").write_text(json.dumps(main_stats, ensure_ascii=False, indent=2), encoding="utf-8")
    descriptions = {
        2: ("保留原三联图结构。地图与纬度密度使用各自坐标口径。低于标准年代范围的待核行单独在插图披露。"
            "地图与时间堆叠不再设 Other 汇总类：无三核心指标的观测以 Ba/Ca-only（绿×）与 SST-only（紫六边形）"
            "显式展示，G1–G7 不变。"),
        3: ("a 为归档非空标准化值，未再按值级质控过滤。共测同一行可以贡献多个指标值。"
            "序列定义为 entity × 指标，entity 由来源标识及站点坐标构成。分辨率分箱内去重，同一序列可跨分箱。"
            f"b 的 G1–G7 与 Ba/Ca-only、SST-only 在行级互斥（不设 Other 汇总类；Ba/Ca+SST 与无指标剩余桶均为 0），"
            f"含 SST 的 {main_stats['sst_values']:,} 行中，"
            f"{main_stats['sst_only_rows']:,} 行为 SST-only。c 为值级质控+验证坐标子集的序列时长分布"
            f"（>100 年 {main_stats['record_duration_gt100yr']} 条）。d–f 沿用原图，在坐标和年代符合入图条件的子集上描述记录末年，"
            "d 按指标独立堆叠 Ba/Ca 与 SST。"),
        1: ("a 为模块化工作流（正文字号已按整页缩排补偿）。b、c 源文件数和归档观测行数各自闭合；"
            "d 为复核队列分桶（暂缓值/行/表、其他暂缓、已裁决应用或排除；暂缓为可恢复而非损失）。"
            "行级标记用于描述归档状态；逐指标分析另用相应的值级质控状态。"),
        4: "CoralHydro2k v1.0.1 独立编库对照。匹配状态分母是 233 个参考指标序列。保留率分母是身份明确且全序列一致的已匹配年代–数值对。",
        5: "首行 a、b 核对实际源行及列，c–f 沿用原 Ba/Ca 覆盖、分布和时序面板。每份源表每项指标最多 24 行，δ¹³C 覆盖值域，Ba/Ca 分层覆盖高值。b 的横轴按源表打印数字，保留原单位标签。全批次 >20 μmol/mol 单列清单，20 是排查阈值。Reed 论文图表与档案单位冲突按来源标记，抽查结果见明细。δ¹³C 的对等覆盖/连续性面板见附图 A3。",
        6: ("a 对比同一 Sr/Ca 序列的亚月观测与日历月均值，显示末 3 年。b 为亚月归档行的实际采样间隔。"
            "c 为亚月聚合到月后与局地 SST 季节气候态的逐序列相关直方图（物理验证）。"
            "指标构成见图 3a，季节气候态汇总见图 7e 与正文说明。"),
        7: ("a、b 为有共同观测的月尺度 Sr/Ca–SST 与 δ¹⁸O–SST 格点相关（圆圈/菱形为名义 90% 显著性）。"
            "c、d 为符合条件且共同月份最多的 Sr/Ca 与 δ¹⁸O 序列季节循环，误差棒为同月跨年均值标准误，"
            "同时列出气候态相关和原始配对月相关。e 为逐序列季节气候态相关的集合分布（月 Sr/Ca、月 δ¹⁸O、亚月 Sr/Ca）。"
            "f 为代表序列逐月 δ¹⁸O 与 Niño 3.4 距平对照，红、蓝分别表示负、正 δ¹⁸O 距平。"
            "完整六联格点相关图见附图 A1，现代气候态类比见正文说明。"),
        8: ("Sr/Ca、δ¹⁸O、δ¹³C 用 13 月高通、2–7 年带通和 10 年低通。各带方差除以全序列方差。"
            "月序列跨度 ≥30 年。j 为三套预设（80%/24 月、90%/6 月、完整月覆盖）与 pre-1850 基线的敏感性，"
            "合格缺口采用线性内插，不同策略样本组成随准入变化。Ba/Ca 的事件型尖峰适合保留原始事件尺度，未纳入这项规则月频带分析。"),
        9: ("用有共同观测的序列对计算带符号 Pearson r（逐对值见 local_pairs_details.csv），地图色标为站点中位 |r|。"
            "共享有据可查的源记录 ID 或逐值相同的对从正文统计剔除，极高相关但岩芯身份待核的对以空心符号列出。"
            "g–i 为双方 <1850 CE 子集重新配对的早期局地一致性（圆=月、菱形=年），未用现代重叠替代。"),
        "A1": "六联格点相关全图（月/年 × Sr/Ca–SST、δ¹⁸O–SST、δ¹⁸O–SSS），与图 7a、b 同源同口径；显著性为名义双侧 90%，未做序列相关校正。",
        "A2": (f"早期为通过年代和值级筛选的 <1850 CE 记录。定年方法有报 {dates['rows_with_method']:,}/"
               f"{dates['early_rows']:,} 行，{dates['records_with_method']}/{dates['early_records']} 个来源记录。"
               f"最长观测跨度 {main_stats['early_max_observed_span_years']:,.0f} 年。"
               "两轴段比例不同，观测缺口以分段表示。早期内部检验见图 8、9，"
               f"早期月度方差基线要求 ≥30 年、覆盖 ≥80%、最长缺测 ≤24 月，涉及 "
               f"{main_stats['early_records_eligible_for_variance']} 个来源记录。局地配对要求同指标、≤50 km、"
               f"≥20 年的共同观测，明确重复排除、近似重复身份待核，涉及 "
               f"{main_stats['early_records_in_local_pairs']} 个来源记录。"),
        "A3": (f"δ¹³C 覆盖/连续性对等面板（月+年、值级质控；密度另要求验证坐标 {d13c_obs:,} 值，"
               f"连续段 ≥30 观测共 {d13c_segments} 段）。与图 5c–f 的 Ba/Ca 面板同一实现；"
               "δ¹³C 因代用指标物理原因不进图 7/A1 格点相关，内部质量由图 8、9 承担。"),
        "A4": (f"分析方法组成（testing_method 填充率 {methods_fill:.1%}，值级质控后）：a 方法×分辨率、b 方法×指标、"
               "c 亚月采样间隔按方法。方法份额为 a 的行边际、不再单列以免重复。LA-ICP-MS 主导亚月块，与图 6 互证；"
               "CoralHydro2k 未展示该质量轴。"),
        "A5": (f"元数据完整性与披露（未质控原始全表 n={len(frame):,}）：关键字段填充率（定年方法 {dating_fill:.1f}%）、"
               "年代方法构成、分辨率→图件准入矩阵、限制披露。年代学控制稀疏是数据缺口而非绘图选择。"),
        "A6": (f"内部一致性检查：a 为 Sr/Ca–δ¹⁸O 配对联合分布（{n_paired_values:,} 对，5–95% 物理域面板内披露）；"
               "b 为配对最丰序列的 Sr/Ca+δ¹⁸O 双轴原始时序（末 100 个配对点），供目视数据形态。"),
        "A7": ("网络尺度重建潜力（南北纬40°平滑海洋热力图，覆盖度代理）：以获支持 2° 格点（成员序列多数名义显著）的中位 |r| 为源，"
               "在记录覆盖海洋（≤3000 km）上插值并平滑，颜色连续过渡；空白=无记录覆盖，陆地遮罩。月/年 × Sr/Ca/δ¹⁸O 四面板。"
               "此为聚合+插值代理而非逐格点直接相关，东部太平洋/大西洋因记录稀疏受限；局限见第 4.5 节。"),
    }
    write_index(output, run, digest, descriptions, text8)
    export_index_pdf(output)
    print(f"Deliverables in {output}", flush=True)


if __name__ == "__main__":
    main()
