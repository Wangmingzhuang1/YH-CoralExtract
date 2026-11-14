"""以实际源文件的物理行号、原始列名和单位核对新增指标。"""

import json
from pathlib import Path

import numpy as np
import pandas as pd
from app.core.moa.spatiotemporal_standard import year_ce


FIELDS = {"d13C": "δ13C_value", "Ba/Ca": "Ba/Ca_value"}
SOURCES = {
    "d13C": (
        "Dassié2013/Dassié2013.txt", "Zinke2004/Zinke2004.txt",
        "Osborne2014/Osborne2014a2.txt", "Wang2023/Wang2023.txt",
        "Linsley1994/Linsley1994.txt",
    ),
    "Ba/Ca": (
        "Walid Naciri2023/Walid Naciri2023a5.txt", "Reed2021/Reed2021a4.txt",
        "Reed2021/Reed2021a5.txt", "Shen1992/Shen1992.txt",
        "Swart1999/Swart1999a2.txt", "Lea1989/Lea1989.txt",
        "Alibert2003/Alibert2003a2.txt",
    ),
}
SAMPLE_SIZE = 24
SCREENING_VALUE = 20
PAPER_EVIDENCE = {
    "Reed2021a4.txt": "Reed et al. (2021), DOI 10.1029/2020PA004051, pp. 6 and 9, Fig. 3b: publication uses μmol/mol; archive definition uses mmol/mol. Figure 3b is the WLF04 core from the same study.",
    "Reed2021a5.txt": "Reed et al. (2021), DOI 10.1029/2020PA004051, p. 9, Fig. 3b: WLF04 plotted in μmol/mol; archive definition for WLF04 annual values uses mmol/mol.",
    "Shen1992.txt": "Shen et al. (1992), local PDF pp. 20–21: printed data table also labels Ba/Ca mmol/mol.",
}


def read_archive(path, column, time_column):
    lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    marker = next(i for i, line in enumerate(lines) if line.strip() == "# Data:")
    header = next(line.split("\t") for line in lines[marker + 1:]
                  if line.strip() and not line.lstrip().startswith("#"))
    value_index = header.index(column)
    time_index = header.index(time_column)
    definition = next(line for line in lines[:marker] if line.startswith(f"## {column}\t"))
    unit = definition.split("\t", 1)[1].split(",")[3].strip()
    return lines, value_index, time_index, unit, definition


def source_scale(proxy, unit):
    unit = unit.casefold().replace("µ", "μ")
    if proxy == "Ba/Ca":
        return {
            "micromole per mole": 1, "μmol/mol": 1,
            "millimole per mole": 1000, "mmol/mol": 1000,
            "nanomole per mole": 0.001, "nmol/mol": 0.001,
            "mole per mole": 1_000_000, "mol/mol": 1_000_000,
        }.get(unit, np.nan)
    # PDB is the source's reported label. This checks numeric copying, not scale identity.
    return 1 if "per mil" in unit else np.nan


def stratified_sample(frame, proxy):
    # Ba/Ca: give each present magnitude band a quota. δ13C: evenly span ranked values.
    ordered = frame.sort_values([proxy, "source_line", "observation_id"])
    strata = [ordered]
    if proxy == "Ba/Ca":
        bands = pd.cut(ordered[proxy], [-np.inf, 20, 100, 1000, np.inf])
        strata = [group for _, group in ordered.groupby(bands, observed=True)]
    quota, extra = divmod(SAMPLE_SIZE, len(strata))
    return pd.concat([group.iloc[np.linspace(0, len(group) - 1, min(len(group), quota + (i < extra)), dtype=int)]
                      for i, group in enumerate(strata)])


def audit_source_tables(frame, root, output):
    normalized = frame["source_file"].fillna("").str.replace("\\", "/", regex=False)
    records = []
    for proxy, files in SOURCES.items():
        field = FIELDS[proxy]
        for file in files:
            source = f"noaa/{file}"
            subset = frame.loc[normalized.eq(source) & frame[proxy].notna()]
            if subset.empty:
                continue
            for _, row in stratified_sample(subset, proxy).iterrows():
                records.append(check_row(row, proxy, field, source, root))
    audit = pd.DataFrame(records)
    if audit.empty:
        raise ValueError("No configured source tables could be sampled")
    audit.to_csv(output / "source_table_audit.csv", index=False, encoding="utf-8-sig")
    tail = frame.loc[frame["Ba/Ca"].gt(SCREENING_VALUE), [
        "observation_id", "Year", "source_file", "source_line", "source_time_value",
        "Ba/Ca", "Ba/Ca_value_original_unit", "Ba/Ca_value_qc_status", "Ba/Ca_value_unit_status",
    ]].copy()
    tail["paper_unit_evidence"] = tail["source_file"].map(
        lambda name: PAPER_EVIDENCE.get(Path(name).name, ""))
    sampled = audit[audit["proxy"].eq("Ba/Ca")].set_index("observation_id")["verdict"]
    tail["sample_verdict"] = tail["observation_id"].map(sampled).fillna("Not sampled")
    tail.to_csv(output / "baca_high_value_inventory.csv", index=False, encoding="utf-8-sig")
    return audit, tail


def check_row(row, proxy, field, source, root):
    result = {
        "proxy": proxy, "observation_id": row["observation_id"], "source_file": source,
        "source_line": row["source_line"], "source_year": np.nan, "archived_year": row["Year"],
        "source_numeric": np.nan, "archived_numeric": row[proxy], "source_label_unit": "",
        "source_label_standardized": np.nan, "difference": np.nan, "source_definition": "",
        "raw_value_match": False, "time_token_match": False, "year_match": False,
        "paper_evidence": PAPER_EVIDENCE.get(Path(source).name, ""),
    }
    try:
        column = json.loads(row["source_columns"])[field]
        lines, val_idx, time_idx, unit, definition = read_archive(
            root / source, column, row["source_time_column"])
        source_cells = lines[int(row["source_line"]) - 1].split("\t")
        source_value = float(source_cells[val_idx])
        source_time = source_cells[time_idx].strip()
        result.update(source_numeric=source_value, source_label_unit=unit, source_definition=definition)
    except (OSError, ValueError, TypeError, KeyError, IndexError, StopIteration) as exc:
        return {**result, "verdict": "Unresolved source locator", "detail": str(exc)}

    expected = source_value * source_scale(proxy, unit)
    delta = float(row[proxy]) - expected
    time_basis = {"CE": "year", "yr BP": "age_bp", "ka BP": "age_ka_bp", "BCE": "age_bce"}
    basis = time_basis.get(row["source_time_unit"])
    reference = row["source_time_reference_year"]
    order = row["source_time_date_order"]
    source_year = year_ce(source_time, basis, reference_year=reference if pd.notna(reference) else None,
                          date_order=order if pd.notna(order) else None) if basis else None
    result.update(
        source_year=source_year, source_label_standardized=expected, difference=delta,
        raw_value_match=bool(np.isclose(source_value, float(row[f"{field}_original_value"]), atol=1e-9, rtol=0)),
        time_token_match=source_time == str(row["source_time_value"]).strip(),
        year_match=source_year is not None and bool(np.isclose(source_year, row["Year"], atol=1e-6, rtol=0)),
    )
    if not result["raw_value_match"] or not (result["time_token_match"] or result["year_match"]):
        verdict = "Source locator or transcription mismatch"
    elif not np.isfinite(delta):
        verdict = "Unresolved source unit"
    elif not np.isclose(delta, 0, atol=1e-7, rtol=0):
        verdict = "Archive differs from source-label conversion"
    elif Path(source).name.startswith("Reed2021"):
        verdict = "Publication and archive unit conflict"
    elif proxy == "Ba/Ca" and row[proxy] > SCREENING_VALUE and source_scale(proxy, unit) in (1, 1_000_000):
        verdict = "High value follows reported source unit"
    elif proxy == "Ba/Ca" and row[proxy] > SCREENING_VALUE:
        verdict = "High after source-label conversion; unit needs adjudication"
    else:
        verdict = "Agrees with source label"
    return {**result, "verdict": verdict, "detail": ""}
