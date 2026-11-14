"""ESSD 图件，沿用项目原有字体、指标配色、轴线与面板编号。"""

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib import ticker
from matplotlib.collections import LineCollection, PathCollection
from matplotlib.lines import Line2D
from matplotlib.text import Text

from app.analysis.style import G_COLORS, add_panel_label
from app.analysis import figures_validation as validation
from app.analysis.figures_validation import VARIANCE_BANDS
from series_analysis import POLICIES, PROXIES


LABELS = {"Sr/Ca": "Sr/Ca", "d18O": "δ¹⁸O", "d13C": "δ¹³C", "Ba/Ca": "Ba/Ca"}
INK = "#4A4A4A"
PALETTE = {"Sr/Ca": "#4E79A7", "d18O": "#E15759", "d13C": "#76B7B2", "Ba/Ca": "#59A14F"}
BLUE, ORANGE, GREY = "#4E79A7", "#F28E2B", "#9D9D9D"
RESOLUTIONS = {
    "submonthly": "Submonthly", "monthly": "Monthly", "bimonthly": "Bimonthly",
    "seasonal": "Seasonal", "biannual": "Biannual", "annual": "Annual",
    "irregular": "Irregular", "discrete_samples": "Discrete samples", "unknown": "Unknown",
}
GROUP_LABELS = {
    "G1": "G1  All three", "G2": "G2  Sr/Ca + δ¹⁸O", "G3": "G3  δ¹⁸O + δ¹³C",
    "G4": "G4  Sr/Ca", "G5": "G5  δ¹⁸O", "G6": "G6  δ¹³C", "G7": "G7  Sr/Ca + δ¹³C",
    "Ba/Ca only": "Ba/Ca only", "SST only": "SST only",
    "Ba/Ca + SST": "Ba/Ca + SST", "Remaining": "Remaining (no proxy)",
}
GROUP_EXTRA_COLORS = {"Ba/Ca only": "#59A14F", "SST only": "#B07AA1", "Ba/Ca + SST": "#8C6D46"}


def title(ax, letter, label):
    add_panel_label(ax, f"({letter})", x=0, y=1.015)
    ax.set_title(label, loc="left", pad=6, x=.055)


def clean(ax):
    ax.spines[["top", "right"]].set_visible(False)


def count_axis(ax, axis="x"):
    getattr(ax, f"{axis}axis").set_major_formatter(ticker.StrMethodFormatter("{x:,.0f}"))


def rescale_for_width(fig, native_width, target_width=7.35, font_gain=.85, line_gain=.7, marker_gain=.85):
    """整页按宽度缩排（combine_panels）前补偿字号/线宽/标记面积，使最终印刷尺寸接近设计值。"""
    scale = target_width / native_width
    fig.canvas.draw()
    for text in fig.findobj(Text):
        text.set_fontsize(text.get_fontsize() * font_gain / scale)
    for line in fig.findobj(Line2D):
        line.set_linewidth(line.get_linewidth() * line_gain / scale)
    for coll in fig.findobj(PathCollection):
        coll.set_sizes(np.asarray(coll.get_sizes()) * (marker_gain / scale) ** 2)
        coll.set_linewidths(np.asarray(coll.get_linewidths()) * line_gain / scale)
    for ax in fig.axes:
        for spine in ax.spines.values():
            spine.set_linewidth(spine.get_linewidth() * line_gain / scale)
    return fig


def composition(frame, output, durations=None):
    proxies = list(LABELS)
    resolution = frame["time_resolution"].where(frame["time_resolution"].isin(RESOLUTIONS), "unknown")
    counts, series = [], []
    for key, label in RESOLUTIONS.items():
        subset = frame.loc[resolution.eq(key)]
        counts.append([subset[proxy].notna().sum() for proxy in proxies])
        series.append([subset.loc[subset[proxy].notna(), "entity_id"].nunique() for proxy in proxies])
    matrix = pd.DataFrame(counts, index=RESOLUTIONS.values(), columns=proxies)
    series = pd.DataFrame(series, index=matrix.index, columns=proxies)
    matrix.to_csv(output / "proxy_resolution_values.csv", index_label="resolution", encoding="utf-8-sig")
    series.to_csv(output / "proxy_resolution_series.csv", index_label="resolution", encoding="utf-8-sig")
    # G1–G7 use the original three-variable grouping; break out the Other rows.
    group = frame["G_Grade"].copy()
    other = group.eq("Other")
    ba, sst = frame["Ba/Ca"].notna(), frame["SST"].notna()
    group.loc[other] = np.select(
        [(ba & ~sst)[other], (sst & ~ba)[other], (ba & sst)[other]],
        ["Ba/Ca only", "SST only", "Ba/Ca + SST"], default="Remaining")
    totals = group.value_counts().reindex(GROUP_LABELS, fill_value=0)
    assert int(totals.sum()) == len(frame)
    assert int(matrix.to_numpy().sum()) == int(frame[proxies].notna().sum().sum())
    totals.rename("rows").to_csv(output / "row_accounting.csv", index_label="group", encoding="utf-8-sig")
    ratios = [1.06, 1, .62] if durations is not None else [1.06, 1]
    fig, axes = plt.subplots(1, len(ratios), figsize=(7.55, 4.15),
                             gridspec_kw={"width_ratios": ratios}, layout="constrained")
    ax, ledger = axes[0], axes[1]
    image = ax.imshow(np.log10(matrix + 1), cmap="Blues", aspect="auto", vmin=0)
    ax.set_xticks(range(len(proxies)), [LABELS[p] for p in proxies])
    ax.set_yticks(range(len(matrix)), matrix.index)
    for (i, j), n in np.ndenumerate(matrix.to_numpy()):
        color = "white" if image.norm(np.log10(n + 1)) > .59 else "#182532"
        ax.text(j, i, f"{n:,}\n({series.iat[i, j]:,})", va="center", ha="center", color=color, fontsize=8)
    title(ax, "a", "Values and series by resolution")
    ax.set_xlabel("Non-exclusive values (series in parentheses)")
    y = np.arange(len(totals))
    ledger.barh(y, totals, color=[{**G_COLORS, **GROUP_EXTRA_COLORS}.get(g, GREY) for g in totals.index], height=.66)
    ledger.set_yticks(y, [GROUP_LABELS[g] for g in totals.index])
    ledger.invert_yaxis()
    ledger.set_xlim(0, totals.max() * 1.23)
    for i, n in enumerate(totals):
        ledger.text(n + totals.max() * .015, i, f"{n:,}", va="center", fontsize=7)
    count_axis(ledger)
    clean(ledger)
    ledger.set_xlabel(f"Mutually exclusive rows (total {len(frame):,})")
    title(ledger, "b", "Row-level closure")
    if durations is not None:
        span = axes[2]
        durations.rename("series").to_csv(output / "record_duration.csv", index_label="span_bin",
                                          encoding="utf-8-sig")
        span.bar(durations.index, durations.to_numpy(), color=BLUE, alpha=.85,
                 edgecolor="black", linewidth=.5)
        long = int(durations.iloc[3:].sum())
        span.text(.97, .95, f"n={int(durations.sum()):,}\n>100 yr: {long} ({long / durations.sum():.1%})",
                  transform=span.transAxes, ha="right", va="top", fontsize=7)
        span.set_ylabel("Number of series", fontsize=8)
        span.set_xlabel("Record span (yr)", fontsize=8)
        span.tick_params(labelsize=6.5)
        span.tick_params(axis="x", rotation=45)
        clean(span)
        title(span, "c", "Record duration")
    return fig


def accounting_panel(manifest, rows, accounting, queue=None):
    panels = [(manifest, "b", "Source file disposition", f"Unique files ({manifest.sum():,})"),
              (accounting, "c", "Archive row disposition", f"Archive rows ({rows:,})")]
    if queue is not None:
        panels.append((queue, "d", "Review queue", f"Queue items ({queue.sum():,})"))
    fig, axes = plt.subplots(1, len(panels), figsize=(7.25, 4.85))
    labelsize = 7.5 if queue is not None else 10
    factors = [1.5, 1.6, 1.35] if queue is not None else [1.35, 1.35]
    for i, (data, letter, caption, denominator) in enumerate(panels):
        a = axes[i]
        y = np.arange(len(data))
        a.barh(y, data, color=[BLUE, ORANGE, "#76B7B2", GREY, "#59A14F", "#B07AA1"][:len(y)], height=.60)
        a.set_yticks(y, data.index)
        a.invert_yaxis()
        a.set_xlim(0, max(1, data.max() * factors[i]))
        for j, n in enumerate(data):
            a.text(n + data.max() * .025, j, f"{n:,}", va="center", fontsize=labelsize)
        count_axis(a)
        a.xaxis.set_major_locator(ticker.MaxNLocator(4))
        a.tick_params(labelsize=labelsize)
        clean(a)
        title(a, letter, caption)
        a.title.set_fontsize(8.5 if queue is not None else 10)
        a.set_xlabel(denominator, fontsize=labelsize)
    return fig


def source_tables(audit):
    fig = plt.figure(figsize=(7.4, 2.95), layout="constrained")
    gs = fig.add_gridspec(1, 2)
    ax = fig.add_subplot(gs[0, 0])
    c = audit.loc[audit["proxy"].eq("d13C")]
    valid = c.dropna(subset=["source_label_standardized"])
    lo, hi = valid["source_label_standardized"].min(), valid["source_label_standardized"].max()
    ax.plot([lo, hi], [lo, hi], "--", color=GREY, lw=.8, label="1:1")
    ax.scatter(valid["source_label_standardized"], valid["archived_numeric"], s=13, color=PALETTE["d13C"],
               edgecolors="white", linewidths=.25, zorder=3)
    ax.set(xlabel="Source-table δ¹³C (‰)", ylabel="Archived δ¹³C (‰)",
           xlim=(lo - .35, hi + .35), ylim=(lo - .35, hi + .35))
    if not valid.empty:
        n_same = int((valid["raw_value_match"] & valid["year_match"] & valid["difference"].abs().lt(1e-7)).sum())
        ax.text(.04, .94, f"{n_same}/{len(c)} agree in age and value\n{c.source_file.nunique()} source files",
                transform=ax.transAxes, va="top")
    title(ax, "a", "δ¹³C source-table fidelity")
    clean(ax)

    ax = fig.add_subplot(gs[0, 1])
    b = audit.loc[audit["proxy"].eq("Ba/Ca")].dropna(subset=["source_numeric"])
    units = {"millimole per mole": ("mmol/mol", ORANGE, "^"),
             "micromole per mole": ("μmol/mol", BLUE, "o"),
             "mole per mole": ("mol/mol", "#76B7B2", "s")}
    for unit, subset in b.groupby("source_label_unit"):
        name, marker_color, marker = units.get(unit, (unit, GREY, "D"))
        ax.scatter(subset["source_numeric"], subset["archived_numeric"], s=15, color=marker_color,
                   marker=marker, edgecolors="white", linewidths=.25, label=name)
    conflict = b.loc[b["verdict"].eq("Publication and archive unit conflict")]
    ax.scatter(conflict["source_numeric"], conflict["archived_numeric"], s=25, facecolors="none",
               edgecolors=INK, linewidths=.55, label="Paper–archive conflict")
    ax.set(xscale="log", yscale="log", xlabel="Numeric value in source table (as printed)",
           ylabel="Archived Ba/Ca (μmol/mol)")
    ax.axhline(20, color=GREY, ls=":", lw=.8)
    ax.legend(loc="upper left", fontsize=7)
    title(ax, "b", "Ba/Ca value and unit audit")
    clean(ax)

    return fig


def early_records(early, baseline, pairs, output):
    early = early.copy()
    early["dating_reported"] = early["paleoData_dating_method"].fillna("").str.strip().ne("")
    summaries = early.groupby("entity_id").agg(
        site=("Site", "first"), first=("Year", "min"), last=("Year", "max"),
        n_rows=("Year", "size"), n_dating=("dating_reported", "sum"),
        dating_reported=("dating_reported", "any"))
    methods = early.loc[early["dating_reported"]].groupby("entity_id")["paleoData_dating_method"].agg(
        lambda values: " | ".join(sorted(set(values))))
    summaries["methods"] = methods
    baseline = baseline.loc[baseline["policy"].eq("Baseline")]
    summaries["variance_eligible"] = summaries.index.isin(baseline["entity_id"])
    definite = pairs.loc[pairs["status"].eq("No shared ID or exact values detected")]
    summaries["local_pair_eligible"] = summaries.index.isin(pd.concat([definite["entity_a"], definite["entity_b"]]))
    summaries = summaries.sort_values(["site", "last", "first"], na_position="last")
    summaries.to_csv(output / "early_records.csv", encoding="utf-8-sig")
    count = len(summaries)
    reported = int(summaries["dating_reported"].sum())
    colors_by_status = {True: BLUE, False: "#B9B9B9"}
    fig = plt.figure(figsize=(7.35, 6.65), layout="constrained")
    gs = fig.add_gridspec(2, 2, height_ratios=[.55, 4], width_ratios=[1, 1.55], wspace=.04)
    head = fig.add_subplot(gs[0, :])
    head.barh(0, reported, color=BLUE, height=.48)
    head.barh(0, count - reported, left=reported, color=colors_by_status[False], height=.48)
    head.set(xlim=(0, count), ylim=(-.72, .72), yticks=[], xticks=[])
    head.text(reported + (count - reported) / 2, 0,
              f"Method absent from archive metadata: {count - reported}/{count} ({1 - reported / count:.1%})",
              ha="center", va="center")
    head.text(.005, .84, f"Method reported: {reported}/{count} ({reported / count:.1%})", transform=head.transAxes)
    title(head, "a", "Dating-method metadata by record")
    head.spines[:].set_visible(False)
    first = min(-100, int(np.floor(summaries["first"].min() / 500) * 500))
    left = fig.add_subplot(gs[1, 0])
    right = fig.add_subplot(gs[1, 1], sharey=left)
    years_by_entity = early.groupby("entity_id")["Year"]
    for ax, (xmin, xmax) in zip((left, right), ((first, 1600), (1600, 1850))):
        for status, color in colors_by_status.items():
            segments, xy = [], []
            for i, (entity, row) in enumerate(summaries.iterrows(), 1):
                if row["dating_reported"] != status or row["last"] < xmin or row["first"] > xmax:
                    continue
                years = np.unique(years_by_entity.get_group(entity).to_numpy())
                jumps = np.diff(years)
                # Draw sampled sections, leaving multi-year gaps visible.
                limit = min(20, max(2, 4 * np.median(jumps))) if len(jumps) else 2
                for block in np.split(years, np.flatnonzero(jumps > limit) + 1):
                    a, b = max(block[0], xmin), min(block[-1], xmax)
                    if b > a:
                        segments.append(((a, i), (b, i)))
                    elif a == b:
                        xy.append((a, i))
            ax.add_collection(LineCollection(segments, colors=color, lw=.8))
            if xy:
                ax.scatter(*zip(*xy), color=color, s=3, linewidth=0)
        ax.set(xlim=(xmin, xmax), ylim=(count + 2, -1), xlabel="Year (CE)")
        ax.xaxis.set_major_locator(ticker.MaxNLocator(5, integer=True))
        clean(ax)
    left.set_ylabel("Records ordered by site")
    left.yaxis.set_major_locator(ticker.MaxNLocator(6, integer=True))
    right.tick_params(labelleft=False)
    right.spines["left"].set_visible(False)
    title(left, "b", "Early records on two time scales")
    return fig, summaries


def environmental_consistency(correlations, examples, enso, enso_pairs, seasonality):
    fig = plt.figure(figsize=(7.35, 8.55))
    grid = fig.add_gridspec(5, 2, left=.08, right=.93, bottom=.06, top=.95,
                            height_ratios=[1.5, .16, 1.30, .95, 1.45], hspace=.50, wspace=.37)
    handles = [Line2D([], [], marker=marker, linestyle="", markerfacecolor="#bdbdbd",
                      markeredgecolor="black", markersize=4, label=label)
               for marker, label in [("o", "Nominal p < 0.10"), ("D", "Nominal p ≥ 0.10")]]
    for cell, (proxy, letter) in zip((grid[0, 0], grid[0, 1]), (("Sr/Ca", "a"), ("d18O", "b"))):
        ax = fig.add_subplot(cell, projection=validation.ccrs.Robinson(central_longitude=180))
        subset = correlations.loc[correlations["resolution"].eq("Monthly") &
                                  correlations["proxy"].eq(proxy) & correlations["env_target"].eq("SST")]
        validation.draw_instrumental_map(ax, subset)
        label = LABELS[proxy]
        ax.set_title(f"({letter}) Monthly {label}–SST (n = {len(subset)} series)", loc="left")
    cax = fig.add_subplot(grid[1, 0])
    mappable = plt.cm.ScalarMappable(cmap=validation.DIVERGING_CMAP, norm=plt.Normalize(-1, 1))
    colorbar = fig.colorbar(mappable, cax=cax, orientation="horizontal", ticks=[-1, -.5, 0, .5, 1])
    colorbar.set_label("Pearson r", fontsize=8)
    cax.tick_params(labelsize=7)
    lax = fig.add_subplot(grid[1, 1])
    lax.axis("off")
    lax.legend(handles=handles, loc="center", ncol=2, fontsize=6.8, columnspacing=.9, frameon=False)

    for cell, (proxy, letter, color) in zip((grid[2, 0], grid[2, 1]),
                                            (("Sr/Ca", "c", BLUE), ("d18O", "d", "#59A14F"))):
        cycle = fig.add_subplot(cell)
        example = examples.get(proxy)
        if example is None:
            cycle.set_title(f"({letter}) Monthly seasonal cycle", loc="left")
            cycle.text(.5, .5, f"No eligible {LABELS[proxy]} series", transform=cycle.transAxes, ha="center")
            continue
        var_name = "SrCa" if proxy == "Sr/Ca" else "d18O"
        validation.draw_seasonal_cycle(cycle, example["pairs"], var_name, color, example["site"])
        cycle.set_title(f"({letter}) Monthly seasonal cycle: {example['site']}", loc="left")
        dates = example["pairs"].index
        cycle.set_xlabel(f"Month ({dates[0].year}–{dates[-1].year} overlap)", fontsize=8)
        cycle.tick_params(labelsize=7)
        annotation = cycle.texts[-1]
        if proxy == "Sr/Ca":
            annotation.set(position=(.60, .075), ha="center", va="bottom", fontsize=6.8)
        else:
            annotation.set(position=(.98, .06), ha="right", va="bottom", fontsize=6.8)
        annotation.set_bbox({"facecolor": "white", "edgecolor": "none", "alpha": .85, "pad": 1.2})
        legend = cycle.get_legend()
        if proxy == "Sr/Ca":
            legend.set_loc("center")
            legend.set_bbox_to_anchor((.52, .30))
        else:
            legend.set_loc("lower left")
            legend.set_bbox_to_anchor((.02, .02))
        legend.set_frame_on(True)
        legend.get_frame().set_facecolor("white")
        legend.get_frame().set_alpha(.9)
        legend.get_frame().set_edgecolor("none")
        for text in legend.get_texts():
            text.set_fontsize(6.5)

    ensemble = fig.add_subplot(grid[3, :])
    common = seasonality.loc[seasonality["comparison"].eq("Common-period seasonal climatology")]
    for (resolution, proxy), color in [(("Monthly", "Sr/Ca"), BLUE), (("Monthly", "d18O"), "#59A14F"),
                                       (("Submonthly", "Sr/Ca"), "#76B7B2")]:
        group = common.loc[common["resolution"].eq(resolution) & common["proxy"].eq(proxy), "r"]
        if group.empty:
            continue
        ensemble.hist(group, bins=np.linspace(-1, 1, 25), color=color, alpha=.55,
                      edgecolor="white", linewidth=.3,
                      label=f"{resolution} {LABELS[proxy]} (n={len(group)}, median {group.median():.2f})")
        ensemble.axvline(group.median(), color=color, linestyle="--", linewidth=.9)
    ensemble.set_xlabel("Seasonal-cycle r vs local SST climatology", fontsize=8)
    ensemble.set_ylabel("Series", fontsize=8)
    ensemble.tick_params(labelsize=7)
    ensemble.legend(fontsize=6.8, frameon=False, loc="upper right")
    clean(ensemble)
    title(ensemble, "e", "Ensemble seasonal-cycle consistency")

    event = fig.add_subplot(grid[4, :])
    event.set(xlabel="Year (CE)", ylabel="δ¹⁸O anomaly (‰ VPDB)")
    if enso_pairs is None:
        event.set_title("(f) ENSO comparison", loc="left")
        event.text(.5, .5, "No eligible ENSO example", transform=event.transAxes, ha="center")
        return fig
    dates = enso_pairs.index
    years = dates.year + (dates.month - 1) / 12
    anomalies = enso_pairs["coral_d18O_anomaly"]
    event.vlines(years, 0, anomalies, color=np.where(anomalies.lt(0), "#cb181d", "#2171b5"),
                 alpha=.7, linewidth=.55)
    event.axhline(0, color=INK, linewidth=.6)
    nino = enso_pairs["nino34"].reindex(pd.period_range(dates.min(), dates.max(), freq="M"))
    twin = event.twinx()
    twin.plot(nino.index.year + (nino.index.month - 1) / 12, nino, color="#747474", linewidth=.65)
    twin.set_ylabel("Niño 3.4 anomaly (°C)", color="#606060")
    event.set_title(f"(f) {enso['site']}: monthly δ¹⁸O and Niño 3.4 anomalies", loc="left")
    event.text(.015, .96, f"r = {enso['r']:.3f}   n = {enso['n']:,} paired months", va="top",
               transform=event.transAxes, fontsize=7.5,
               bbox={"facecolor": "white", "edgecolor": "none", "pad": 1.5})
    event.margins(x=.005)
    return fig


def variance(frame, early, output, map_columns):
    labels = [title_text.split(" (")[0] for _, title_text, _ in VARIANCE_BANDS]
    columns = [f"{band}_pct" for band, _, _ in VARIANCE_BANDS]
    fig, axes = plt.subplots(1, 3, figsize=(7.4, 3.1), layout="constrained", sharex=True, sharey=True)
    summaries = []
    for proxy, ax in zip(PROXIES, axes):
        group = frame.loc[frame["proxy"].eq(proxy)]
        eligible_early = early.loc[early["policy"].eq("Baseline") & early["proxy"].eq(proxy)]
        policy_groups = [(policy, group.loc[group["policy"].eq(policy)]) for policy, _, _ in POLICIES]
        for i, band in enumerate(columns):
            for j, (policy, rows) in enumerate(policy_groups):
                if len(rows):
                    median = rows[band].median()
                    ax.plot(median, i + (j - 1) * .24, marker=["o", "s", "^"][j], color=PALETTE[proxy],
                            mfc=["none", PALETTE[proxy], PALETTE[proxy]][j], linestyle="none", ms=5.5)
                    summaries.append((proxy, band, policy, len(rows), median, len(eligible_early)))
            if len(eligible_early):
                ax.plot(eligible_early[band].median(), i + .35, "D", color=PALETTE[proxy], mfc="white", ms=4.5)
        sizes = "/".join(str(len(rows)) for _, rows in policy_groups)
        ax.set_title(f"{LABELS[proxy]}   (n = {sizes})", fontsize=9)
        ax.set(xlim=(0, 100), ylim=(2.60, -.50), xticks=[0, 25, 50, 75, 100])
        ax.tick_params(labelsize=8)
        clean(ax)
    pd.DataFrame(summaries, columns=["proxy", "band", "policy", "n_series", "median_pct",
                                    "early_baseline_n"]).to_csv(output / "variance_summary.csv", index=False)
    axes[0].set_yticks(range(len(labels)), labels)
    axes[0].tick_params(axis="y", labelsize=8.5)
    axes[1].set_xlabel("Median variance share (%)", fontsize=9)
    fig.suptitle("(j) Sensitivity to monthly gaps", x=.025, ha="left", fontsize=10, weight="bold")
    handles = [Line2D([], [], color=INK, marker=marker, mfc=face, lw=0, ms=5.5, label=label)
               for marker, face, label in zip(["o", "s", "^", "D"], ["none", INK, INK, "white"], [
                   "≥80%, gap ≤24 mo", "≥90%, gap ≤6 mo", "Complete coverage", "Pre-1850 baseline"])]
    fig.legend(handles=handles, loc="outside lower center", ncol=4, columnspacing=1, fontsize=8)
    fig.canvas.draw()
    fig.set_layout_engine("none")
    for ax, (left, width) in zip(axes, map_columns, strict=True):
        pos = ax.get_position()
        ax.set_position([left, pos.y0, width, pos.height])
    return fig

