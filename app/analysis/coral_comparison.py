"""用固定版本的 CoralHydro2k 对照已发布观测，生成可复算的清单和覆盖图。"""

import argparse
from collections import Counter, defaultdict
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import zipfile

import numpy as np
import pandas as pd

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.core.moa.unit_converter import canonical_unit


BASELINE_URL = "https://lipdverse.org/CoralHydro2k/current_version/CoralHydro2k1_0_1.zip"
BASELINE_SHA256 = "64b99b81203a4bda8bd736ebd6a37e442a7252762dafb96603140039e274602d"
PROXIES = {"Sr/Ca": ("Sr/Ca (mmol/mol)", "Sr/Ca_value"),
           "δ18O": ("δ18O (‰ VPDB)", "δ18O_value"), "δ13C": ("δ13C (‰)", "δ13C_value")}
SHARED = {"Sr/Ca", "δ18O"}
IDENTITY = ["source_file", "sheet_name", "source_record_id", "geo_siteName"]
BODY_SCALE = {
    ("10.25921/amc5-m917", "d18o_mont"): "10.1029/2005PA001187 §2, PDF p.5",
    ("10.25921/amc5-m917", "d13c_mont"): "10.1029/2005PA001187 §2, PDF p.5",
    ("10.1594/pangaea.965772", "porites sp. δ18o"): "10.1029/2024PA004843 §2.2",
}


def doi(value):
    cleaned = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", str(value).strip().casefold())
    # 修复缺首1笔误（如 0.1016/... 应为 10.1016/...）
    return ("1" + cleaned) if re.match(r"^0[.]\d{4,9}/", cleaned) else cleaned


def column_label(value):
    return re.sub(r"\s*\[[^\]]+\]\s*$", "", value).strip().casefold()


def study_id(value):
    match = re.search(r"(?:/study/|noaa-coral-)(\d+)", value, re.I)
    return match.group(1) if match else ""


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def baseline_series(path):
    result = []
    with zipfile.ZipFile(path) as release:
        for name in sorted(n for n in release.namelist() if n.endswith(".lpd") and not n.startswith("__MACOSX/")):
            with zipfile.ZipFile(io.BytesIO(release.read(name))) as entity:
                meta_path = next(n for n in entity.namelist() if n.endswith("/metadata.jsonld"))
                metadata = json.loads(entity.read(meta_path))
                folder = str(Path(meta_path).parent).replace("\\", "/")
                primary = doi((metadata.get("pub") or [{}])[0].get("doi") or "")
                for paleo in metadata["paleoData"]:
                    for table in paleo.get("measurementTable", []):
                        columns = {str(col["variableName"]).casefold(): col for col in table["columns"]}
                        year = columns.get("year")
                        if not year:
                            continue
                        if str(year["units"]).casefold() not in {"ad", "ce", "year ce"}:
                            raise ValueError(f"对照年代基准未处理: {metadata['dataSetName']} {year['units']}")
                        source = pd.read_csv(entity.open(f"{folder}/{table['filename']}"), header=None,
                                             encoding="utf-8-sig", float_precision="round_trip")
                        years = pd.to_numeric(source.iloc[:, int(year["number"]) - 1], errors="coerce")
                        for key, col in columns.items():
                            proxy = {"srca": "Sr/Ca", "sr/ca": "Sr/Ca", "d18o": "δ18O"}.get(key)
                            if proxy is None:
                                continue
                            vals = pd.to_numeric(source.iloc[:, int(col["number"]) - 1], errors="coerce")
                            if proxy == "Sr/Ca" and str(col["units"]).casefold() != "mmol/mol":
                                raise ValueError(f"对照 Sr/Ca 单位未处理: {col['units']}")
                            good = np.isfinite(years) & np.isfinite(vals)
                            result.append({"entity": metadata["dataSetName"], "proxy": proxy, "pub1_doi": primary,
                                           "source_url": metadata.get("originalDataUrl", ""),
                                           "core": col.get("coreName", ""),
                                           "pairs": Counter(zip(years[good].round(2), vals[good]))})  # 年份统一到2位小数，与发布库对齐
    return result


def archive_scale_comments(root, source_files):
    result = {}
    for file in source_files:
        path = root / file
        if path.suffix.casefold() != ".tab" or not path.is_file():
            continue
        with path.open(encoding="utf-8-sig", errors="replace") as stream:
            for line_no, line in enumerate(stream, 1):
                if line.startswith("*/"):
                    break
                declaration, sep, comment = line.partition(" * COMMENT:")
                if not sep or not re.search(r"\b(?:vs\.?|per mil|relative to)\s+(?:the\s+)?V[- ]?PDB\b", comment, re.I):
                    continue
                parameter = re.fullmatch(r"\s*(.+?)\s*\[‰ PDB\]\s*\(([^)]+)\)", declaration.split(" * ")[0])
                if parameter:
                    evidence = f"{file}:{line_no} {comment.strip()}"
                    for label in parameter.groups():
                        result.setdefault((file, column_label(label)), set()).add(evidence)
    return {key: next(iter(refs)) for key, refs in result.items() if len(refs) == 1}


def reviewed_scale_locator(serialized):
    try:
        evidence = json.loads(serialized)
    except (TypeError, ValueError):
        return ""
    if not isinstance(evidence, list):
        return ""
    for item in evidence:
        if isinstance(item, dict) and item.get("source") == "reviewed_body" and item.get("unit") == "‰ VPDB":
            return item.get("locator", "")
        if (isinstance(item, dict) and item.get("source") == "paper_model_review"
                and item.get("unit") == "‰ VPDB" and item.get("evidence_status") == "verified"):
            return item.get("note", "").split("原文：", 1)[0]
    return ""


def local_series(csv_path, root):
    columns = {*IDENTITY, "dataset_doi", "pub1_doi", "year_CE", "source_time_unit", "source_columns", "record_type",
               "geo_latitude", "geo_longitude", "geo_rep_latitude", "geo_rep_longitude", "geo_rep_status",
               "qc_location_status", "qc_time_status"}
    for column, field in PROXIES.values():
        columns.update((column, f"{field}_qc_status", f"{field}_original_unit",
                        f"{field}_unit_status", f"{field}_unit_evidence"))
    data = pd.read_csv(csv_path, encoding="utf-8-sig", usecols=lambda name: name in columns,
                       low_memory=False, float_precision="round_trip")
    comments = archive_scale_comments(root, data["source_file"].dropna().unique())
    files = {}
    records = []
    for key, frame in data.groupby(IDENTITY, sort=True, dropna=False):
        file, sheet, source_id, site = ("" if pd.isna(value) else str(value) for value in key)
        if file not in files:
            path = root / file
            noaa_study = ""
            if path.is_file() and file.casefold().startswith(("noaa\\", "noaa/")):
                with path.open(encoding="utf-8-sig", errors="replace") as stream:
                    noaa_study = study_id(stream.read(5000))
            files[file] = (digest(path) if path.is_file() else "", noaa_study)
        dates = (frame["qc_time_status"].eq("pass")
                 & frame["source_time_unit"].isin({"CE", "yr BP", "ka BP", "BCE"}))
        exact = frame.loc[frame["qc_location_status"].eq("pass"), ["geo_latitude", "geo_longitude"]].dropna().drop_duplicates()
        region = frame.loc[frame["geo_rep_status"].eq("reviewed_regional_composite"),
                           ["geo_rep_latitude", "geo_rep_longitude"]].dropna().drop_duplicates()
        coordinates = tuple(exact.iloc[0]) if len(exact) == 1 else ()
        representative = tuple(region.iloc[0]) if len(region) == 1 else ()
        primary = {doi(v) for v in frame["pub1_doi"].dropna()}
        archive = {doi(v) for v in frame["dataset_doi"].dropna()}
        primary = next(iter(primary)) if len(primary) == 1 else ""
        archive = next(iter(archive)) if len(archive) == 1 else ""
        for proxy, (column, field) in PROXIES.items():
            if column not in frame:
                continue
            valid = dates & frame[column].notna()
            candidates = frame.loc[valid]
            source_headers = {mapping: json.loads(mapping).get(field, "")
                              for mapping in candidates["source_columns"].dropna().unique()}
            for original, subset in candidates.groupby(candidates["source_columns"].map(source_headers), sort=True):
                evidence = ""
                selected = subset
                if proxy != "Sr/Ca":
                    label = column_label(original)
                    evidence = comments.get((file, label)) or BODY_SCALE.get((archive, label), "")
                    units = subset[f"{field}_original_unit"]
                    standard = units.map({unit: canonical_unit(unit) for unit in units.dropna().unique()})
                    accepted = standard.eq("‰ VPDB")
                    status_col = f"{field}_unit_status"
                    if status_col in subset:
                        accepted &= subset[status_col].eq("verified")
                    if accepted.any() and (accepted & units.ne("‰ VPDB")).any() and not evidence:
                        evidence = "Explicit PDB label harmonized to VPDB"
                    evidence_column = f"{field}_unit_evidence"
                    if evidence_column in subset:
                        stored = subset[evidence_column].dropna()
                        locators = stored.map({item: reviewed_scale_locator(item) for item in stored.unique()})
                        reviewed = locators.reindex(subset.index, fill_value="").ne("") & (
                            units.isna() | units.isin({"‰", "‰ PDB"})
                        )
                        accepted |= reviewed
                        if reviewed.any():
                            references = [evidence, *locators[reviewed.loc[stored.index]].unique()]
                            evidence = " | ".join(dict.fromkeys(item for item in references if item))
                    selected = subset.loc[accepted]
                # 年份统一到2位小数：发布库(CoralHydro2k)按2位小数报告年代，
                # 本地保留全精度会导致精确键交集为空（BO14/CO03类假阴性）
                years_rounded = selected["year_CE"].round(2)
                before_proxy_qc = Counter(zip(years_rounded, selected[column]))
                screened = selected.loc[selected[f"{field}_qc_status"].eq("pass")]
                pairs = Counter(zip(years_rounded.loc[screened.index], screened[column]))
                identity = [file, sheet, source_id, site, original, proxy]
                key_hash = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode("utf-8")).hexdigest()[:20]
                records.append(dict(series_id=key_hash, source_file=file, source_sha256=files[file][0], sheet=sheet,
                                    noaa_study_id=files[file][1],
                                    source_record_id=source_id, site=site, source_column=original, proxy=proxy,
                                    pub1_doi=primary, dataset_doi=archive, pairs=pairs, comparison_pairs=before_proxy_qc,
                                    coordinates=coordinates,
                                    representative=representative, scale_evidence=evidence,
                                    scale_confirmed_values=int(selected[f"{field}_original_unit"].isin({"‰ PDB", "‰"}).sum()),
                                    excluded_scale_values=len(subset)-len(selected),
                                    discrete=bool(subset["record_type"].eq("discrete_sample").all())))
    return records


VALUE_TOLERANCE = 0.01  # 发布库舍入位差容忍（‰/mmol/mol 保留2-3位小数的最大舍入差<0.005）


def overlap_within_tolerance(ref_pairs, local_pairs, tol=VALUE_TOLERANCE):
    """按(年份2位小数, 值容差)统计匹配观测数，替代精确键交集。"""
    lmap = {}
    for (y, v), c in local_pairs.items():
        lmap.setdefault(round(y, 2), []).append([v, c, False])
    matched = 0
    for (y, v), c in ref_pairs.items():
        remaining = c
        for slot in lmap.get(round(y, 2), ()):
            if slot[2] or abs(slot[0] - v) > tol:
                continue
            take = min(remaining, slot[1])
            matched += take
            slot[1] -= take
            remaining -= take
            if remaining == 0:
                break
    return matched


def compare(reference, local):
    duplicates = {}
    equivalent = {}
    main = []
    for item in local:
        pairs = item["pairs"]
        archive = item["dataset_doi"] or item["pub1_doi"]
        signature = (tuple(sorted(pairs.items())), tuple(sorted(item["comparison_pairs"].items())))
        archive_key = (item["proxy"], archive, item["coordinates"], signature) if archive and len(pairs) >= 10 else None
        file_key = (item["proxy"], item["source_sha256"], item["sheet"], item["source_record_id"],
                    item["source_column"], signature) if item["source_sha256"] and pairs else None
        match = equivalent.get(file_key) or equivalent.get(archive_key)
        if match:
            duplicates[item["series_id"]] = match
            continue
        for key in (file_key, archive_key):
            if key is not None:
                equivalent[key] = item["series_id"]
        main.append(item)

    by_proxy = defaultdict(list)
    for item in main:
        if item["comparison_pairs"]:
            by_proxy[item["proxy"]].append(item)
    match_rows = []
    local_status = defaultdict(set)
    for ref in reference:
        values = ref["pairs"]
        base_years = [y for y, _ in values]
        total = sum(values.values())
        pool = by_proxy[ref["proxy"]]
        same_paper = {s["series_id"] for s in pool if ref["pub1_doi"] and s["pub1_doi"] == ref["pub1_doi"]}
        ref_archive = doi(ref["source_url"]) if isinstance(ref["source_url"], str) else ""
        ref_study = study_id(ref["source_url"]) if isinstance(ref["source_url"], str) else ""
        same_archive = {s["series_id"] for s in pool if (s["dataset_doi"] and s["dataset_doi"] == ref_archive)
                        or (ref_study and s["noaa_study_id"] == ref_study)}
        linked = same_paper | same_archive
        candidates = [s for s in pool if s["series_id"] in linked or s["comparison_pairs"] == values]
        result = "待核，无来源对应候选"
        best = None
        local_years = []
        overlap = 0
        if candidates:
            scored = [(overlap_within_tolerance(values, s["comparison_pairs"]), s) for s in candidates]
            overlap, best = max(scored, key=lambda pair: (pair[0], pair[1]["comparison_pairs"] == values,
                                                         pair[1]["series_id"] in same_archive, pair[1]["series_id"] in same_paper))
            local_years = [y for y, _ in best["comparison_pairs"]]
            result = "待核，来源关联或部分数值重合"
            best_total = sum(best["comparison_pairs"].values())
            if overlap == total and total > 0 and best_total == total:
                result = "双方一致" if best["series_id"] in linked else "待核，逐值一致但身份需核"
            elif overlap == total and total >= 10:
                if min(local_years) < min(base_years) or max(local_years) > max(base_years):
                    result = "待核，原序列可能延长"
            local_status[best["series_id"]].add(result)
        row = {k: v for k, v in ref.items() if k != "pairs"}
        row.update(reference_values=total, matched_year_value_pairs=overlap, status=result,
                   matched_after_proxy_qc=overlap_within_tolerance(values, best["pairs"]) if best else 0,
                   reference_first_year_CE=min(base_years, default=None), reference_last_year_CE=max(base_years, default=None),
                   local_first_year_CE=min(local_years, default=None), local_last_year_CE=max(local_years, default=None),
                   local_series_id=best["series_id"] if best else "",
                   local_source_file=best["source_file"] if best else "",
                   same_paper_candidates=len(same_paper), same_archive_candidates=len(same_archive),
                   candidate_series_ids=json.dumps(sorted(s["series_id"] for s in candidates)))
        match_rows.append(row)
    ledger = []
    for item in local:
        pairs = item["pairs"]
        years = [y for y, _ in pairs]
        series_id = item["series_id"]
        status = "待核，新增候选"
        if not item["comparison_pairs"]:
            status = "待核，标尺证据缺失"
        elif not years:
            status = "待核，未通过指标质控"
        elif min(years) < 1:
            status = "待核，含公元纪年前观测"
        if series_id in local_status:
            status = "；".join(sorted(local_status[series_id]))
        if series_id in duplicates:
            status = "重复版本"
        ledger.append({**{k: v for k, v in item.items() if k not in {"pairs", "comparison_pairs"}}, "status": status,
                       "duplicate_of": duplicates.get(series_id, ""), "values": sum(pairs.values()),
                       "values_before_proxy_qc": sum(item["comparison_pairs"].values()),
                       "first_year_CE": min(years) if years else None, "last_year_CE": max(years) if years else None})
    return match_rows, ledger, main


def dates_by_value(pairs):
    """Index the multiset of observation dates by exact proxy value."""
    grouped = defaultdict(list)
    for (year, value), count in sorted(pairs.items()):
        grouped[value].extend([year] * count)
    return grouped


def count_close_dates(reference_dates, candidate_dates, tolerance):
    """Match each observation at most once within the declared date tolerance."""
    matched = 0
    for value, reference_years in reference_dates.items():
        candidate_years = candidate_dates.get(value, [])
        left = right = 0
        while left < len(reference_years) and right < len(candidate_years):
            delta = reference_years[left] - candidate_years[right]
            if abs(delta) <= tolerance + 1e-10:
                matched += 1
                left += 1
                right += 1
            elif delta < 0:
                left += 1
            else:
                right += 1
    return matched


def sequence_diagnostics(reference, local, crosswalk):
    """Find candidates for review without promoting any strict comparison status."""
    tolerance = 0.005  # Half the last place of a calendar year reported to two decimals.
    references = {(row["entity"], row["proxy"]): row["pairs"] for row in reference}
    candidates = {row["series_id"]: row for row in local}
    candidate_dates = {}
    rows = []
    for row in crosswalk:
        if row["status"] != "待核，来源关联或部分数值重合":
            continue
        pairs = references[row["entity"], row["proxy"]]
        reference_dates = dates_by_value(pairs)
        scores = []
        for series_id in json.loads(row["candidate_series_ids"]):
            candidate = candidates[series_id]["comparison_pairs"]
            if series_id not in candidate_dates:
                candidate_dates[series_id] = dates_by_value(candidate)
            matches = count_close_dates(reference_dates, candidate_dates[series_id], tolerance)
            scores.append((matches, sum((pairs & candidate).values()), series_id))
        near, exact = max((near, exact) for near, exact, _ in scores)
        best = [series_id for n, e, series_id in scores if (n, e) == (near, exact)] if near else []
        identified = candidates[best[0]] if len(best) == 1 else {}
        rows.append(dict(entity=row["entity"], proxy=row["proxy"], reference_core=row["core"],
                         reference_values=row["reference_values"], strict_matched_pairs=row["matched_year_value_pairs"],
                         tolerance_years=tolerance, diagnostic_matched_pairs=near, diagnostic_exact_pairs=exact,
                         source_file=identified.get("source_file", ""),
                         source_record_id=identified.get("source_record_id", ""),
                         source_column=identified.get("source_column", ""),
                         candidate_series_ids=json.dumps(best)))
    return rows


def coverage_rows(reference, local):
    windows = {
        "pre_ce": np.arange(1.5, 10.01, 0.5),
        "common_era": np.r_[1, np.arange(100, 2100, 100), 2020],
        "recent": np.arange(1600, 2030, 10),
    }
    rows = []
    for panel, edges in windows.items():
        cohorts = (("Local screened candidates", local),) if panel == "pre_ce" else (("Local screened candidates", local), ("CoralHydro2k v1.0.1", reference))
        for name, series in cohorts:
            proxies = PROXIES if panel == "pre_ce" else {"Sr/Ca and δ18O": None}
            for proxy in proxies:
                members = [[] for _ in range(len(edges) - 1)]
                for item in series:
                    years = np.array([y for y, _ in item["pairs"]])
                    if not len(years) or item.get("discrete"):
                        continue
                    if panel == "pre_ce":
                        if item["proxy"] != proxy:
                            continue
                        years = (1950 - years[years < 1]) / 1000
                    else:
                        years = years[years >= 1]
                        if item["proxy"] not in SHARED or not len(years) or years.max() - years.min() < 10:
                            continue
                    identity = item["series_id"] if "series_id" in item else f"{item['entity']}|{item['proxy']}"
                    for i in np.flatnonzero(np.histogram(years, edges)[0]):
                        members[i].append(identity)
                rows.extend(dict(panel=panel, cohort=name, proxy=proxy, bin_start=edges[i], bin_end=edges[i+1],
                                 count=len(ids), series_ids=json.dumps(sorted(ids), ensure_ascii=False))
                            for i, ids in enumerate(members))
    return pd.DataFrame(rows)


def plot_coverage(bins, local, crosswalk, output, figure_stem="Figure1"):
    import matplotlib.pyplot as plt
    import cartopy.crs as ccrs

    plt.rcParams.update({"font.family": "Times New Roman", "pdf.fonttype": 42, "font.size": 8,
                         "axes.titlesize": 10,
                         "axes.spines.top": False, "axes.spines.right": False, "axes.linewidth": 0.7})
    fig = plt.figure(figsize=(7.2, 7.65), layout="constrained")
    layout = fig.add_gridspec(3, 2, height_ratios=[1.05, 1, 0.65])
    axis = fig.add_subplot(layout[0, 0], projection=ccrs.Robinson(central_longitude=180))
    axis.set_global()
    axis.set_anchor("N")
    axis.coastlines(linewidth=0.45, color="#707680")
    maps = defaultdict(set)
    for s in local:
        if s["pairs"] and not s["discrete"]:
            if s["representative"]:
                maps["Regional composite"].add(s["representative"])
            if s["coordinates"]:
                for label, keep in (("Common Era", any(y >= 1 for y, _ in s["pairs"])),
                                    ("Before Common Era", any(y < 1 for y, _ in s["pairs"]))):
                    if keep:
                        maps[label].add(s["coordinates"])
    for label, color, marker in (("Common Era", "#267780", "o"), ("Before Common Era", "#B66233", "^"),
                                  ("Regional composite", "#252A31", "D")):
        if maps[label]:
            lat, lon = zip(*sorted(maps[label]))
            axis.scatter(lon, lat, color=color, s=10 if marker == "o" else 20, marker=marker,
                         transform=ccrs.PlateCarree(), label=label, zorder=3)
    axis.set_title("a   Screened record locations", loc="left", fontweight="bold")
    axis.legend(loc="lower center", bbox_to_anchor=(0.5, -0.19), frameon=False, ncol=3, fontsize=7)
    settings = (("pre_ce", "b   Earlier coverage", "Age (ka BP, 1950 reference)", layout[0, 1]),
                ("common_era", "c   Common Era", "Year (CE)", layout[1, 0]),
                ("recent", "d   Recent centuries", "Year (CE)", layout[1, 1]))
    colors = {"Sr/Ca": "#275E83", "δ18O": "#267780", "δ13C": "#B66233",
              "Local screened candidates": "#267780", "CoralHydro2k v1.0.1": "#343942"}
    for key, title, xlabel, cell in settings:
        ax = fig.add_subplot(cell)
        for (cohort, proxy), group in bins.loc[bins["panel"].eq(key)].groupby(["cohort", "proxy"], sort=False):
            label = {"δ18O": "δ¹⁸O", "δ13C": "δ¹³C"}.get(proxy, proxy) if key == "pre_ce" else cohort
            edges = np.r_[group["bin_start"], group["bin_end"].iloc[-1]]
            ax.stairs(group["count"], edges, baseline=None, label=label, color=colors[proxy if key == "pre_ce" else cohort], linewidth=1.5,
                      linestyle="--" if cohort.startswith("CoralHydro") else "-")
        if key == "pre_ce":
            ax.set_xlim(10, 1.5)
        elif key == "common_era":
            ax.set_xlim(1, 2020)
        else:
            ax.set_xlim(1600, 2020)
        ax.set_ylim(bottom=0)
        ax.set_title(title, loc="left", fontweight="bold")
        ax.set_xlabel(xlabel)
        ax.set_ylabel("Proxy series per bin")
        ax.grid(axis="y", color="#E3E7E9", linewidth=0.55)
        ax.legend(frameon=False, loc="upper left", fontsize=7)

    # The denominator for series reproduction is the full reference cohort.
    # QC retention uses only the pairs in the fully reproduced series.
    benchmark = pd.DataFrame(crosswalk)
    cohorts = [benchmark, benchmark.loc[benchmark["proxy"].eq("δ18O")],
               benchmark.loc[benchmark["proxy"].eq("Sr/Ca")]]
    statuses = ["Complete match within tolerance", "Other outcomes under review", "No comparable candidate"]
    exact = [rows.loc[rows["status"].eq("双方一致")] for rows in cohorts]
    no_candidate = [int(rows["status"].eq("待核，无来源对应候选").sum()) for rows in cohorts]
    totals = np.array([len(rows) for rows in cohorts])
    exact_counts = np.array([len(rows) for rows in exact])
    counts = [exact_counts, totals - exact_counts - no_candidate, no_candidate]
    y = np.arange(len(cohorts))
    proxy_labels = ["All", "δ¹⁸O", "Sr/Ca"]

    ax = fig.add_subplot(layout[2, 0])
    offset = np.zeros(len(cohorts))
    for label, values, fill, ink in zip(statuses, counts,
                                         ["#267780", "#C8DCE1", "#E4E7E9"],
                                         ["white", "#252A31", "#252A31"]):
        share = 100 * np.asarray(values) / totals
        ax.barh(y, share, left=offset, height=0.54, label=label, color=fill)
        for row, (start, width, count) in enumerate(zip(offset, share, values)):
            if width >= 5:
                text = f"{count} ({width:.1f}%)" if label == statuses[0] and width >= 25 else str(count)
                ax.text(start + width / 2, row, text, ha="center", va="center", color=ink)
        offset += share
    ax.set_title("e   Reference series reproduction", loc="left", fontweight="bold", pad=19)
    ax.text(0, 1.035, "Before proxy QC", transform=ax.transAxes)
    ax.set_yticks(y, [f"{label} ({total})" for label, total in zip(proxy_labels, totals)])
    ax.set_ylim(2.53, -0.53)
    ax.set_xlim(0, 100)
    ax.set_xticks([0, 25, 50, 75, 100])
    ax.set_xlabel("Share of reference series (%)")
    ax.legend(frameon=False, fontsize=7, loc="upper left", bbox_to_anchor=(0, -0.33),
              ncol=2, columnspacing=0.9, handlelength=1.2, borderaxespad=0)

    ax = fig.add_subplot(layout[2, 1])
    before = np.array([rows["matched_year_value_pairs"].sum() for rows in exact])
    after = np.array([rows["matched_after_proxy_qc"].sum() for rows in exact])
    retention = np.divide(100 * after, before, out=np.full(len(before), np.nan), where=before > 0)
    comparable = before > 0
    ax.axvline(100, linestyle="--", linewidth=0.8, color="#707680")
    ax.scatter(retention[comparable], y[comparable], color="#267780", s=24, zorder=3)
    for row, share in zip(y[comparable], retention[comparable]):
        ax.annotate(f"{share:.1f}%", (share, row), xytext=(6, 0), textcoords="offset points",
                    ha="left", va="center")
    ax.set_title("f   Matched pairs after proxy QC", loc="left", fontweight="bold", pad=19)
    ax.text(0, 1.035, "Among series matched within tolerance", transform=ax.transAxes)
    ax.set_yticks(y, [f"{label}  {n:,}/{d:,}" if d else f"{label}  No pairs"
                     for label, n, d in zip(proxy_labels, after, before)])
    ax.set_ylim(2.53, -0.53)
    start = min(98, np.floor(retention[comparable].min())) if comparable.any() else 98
    ax.set_xlim(start, 100.18)
    ax.set_xticks([start, 99, 100])
    ax.set_xlabel("Share of matched age–value pairs (%)")
    ax.grid(axis="y", color="#E3E7E9", linewidth=0.55)

    fig.savefig(output / f"{figure_stem}.pdf", facecolor="white")
    fig.savefig(output / f"{figure_stem}.png", dpi=300, facecolor="white")
    plt.close(fig)


def run_comparison(run_dir, baseline, out_dir, figure_stem="Figure1", source_root=None):
    run_dir, baseline, out_dir = map(Path, (run_dir, baseline, out_dir))
    if digest(baseline) != BASELINE_SHA256:
        raise ValueError("对照文件校验值与已核实的 CoralHydro2k v1.0.1 不符")
    source = run_dir / "consolidated/all_data_combined.csv"
    source_sha256 = digest(source)
    ref = baseline_series(baseline)
    local = local_series(source, source_root or Path.cwd())
    if digest(source) != source_sha256:
        raise RuntimeError("汇总数据在对照过程中发生变化")
    if len({(s["entity"], s["proxy"]) for s in ref}) != len(ref) or len(ref) != 233:
        raise ValueError("对照包没有解析出 233 个唯一的代理指标序列")
    if len({s["series_id"] for s in local}) != len(local):
        raise ValueError("项目序列 ID 不唯一")
    crosswalk, ledger, screened = compare(ref, local)
    bins = coverage_rows(ref, screened)
    out_dir.mkdir(parents=True, exist_ok=True)
    diagnostics = sequence_diagnostics(ref, local, crosswalk)
    for name, rows in (("sequence_crosswalk.csv", crosswalk), ("sequence_inventory.csv", ledger),
                       ("sequence_diagnostics.csv", diagnostics),
                       ("figure1_counts.csv", bins)):
        pd.DataFrame(rows).to_csv(out_dir / name, index=False, encoding="utf-8-sig")
    plot_coverage(bins, screened, crosswalk, out_dir, figure_stem)
    summary = {"baseline_url": BASELINE_URL, "baseline_sha256": BASELINE_SHA256,
               "source_batch": run_dir.name, "source_csv_sha256": source_sha256,
               "year_decimal_places": 2, "value_tolerance": VALUE_TOLERANCE,
               "reference_entities": len({s["entity"] for s in ref}), "reference_proxies": len(ref),
               "reference_status": dict(Counter(r["status"] for r in crosswalk)),
               "local_status": dict(Counter(r["status"] for r in ledger)),
               "scale_confirmed_values": sum(s["scale_confirmed_values"] for s in local),
               "excluded_scale_values": sum(s["excluded_scale_values"] for s in local),
               "scope": "逐值对照区分指标质控前与质控后。仅用原表绝对年代。显式 PDB 标尺按项目规则统一为 VPDB，原始标签保留。"
                        "图中每箱统计至少有一个通过指标质控的时间点的候选代理指标序列。CE 两组统一采用至少10年跨度。"
                        "仍待核的跨库版本可能影响候选序列数。"
                        "空间图仅绘通过坐标检查的点和已复核的区域代表点。"}
    (out_dir / "comparison_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description="珊瑚古气候序列对照与覆盖图")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    summary = run_comparison(args.run_dir, args.baseline, args.out_dir)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
