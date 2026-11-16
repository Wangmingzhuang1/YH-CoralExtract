"""以实际源文件的物理行号、原始列名和单位核对归档指标。

d13C 与 Ba/Ca 沿用固定来源抽核（每份源表每项指标至多 24 行）；
Sr/Ca 与 δ18O 按来源类型×映射方式分层抽样，层内跨源文件均匀布点，
单独报告模型辅助映射层的独立转录一致率。全部核对为确定性源文件重读，
不调用模型。
"""

import argparse
import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.moa.spatiotemporal_standard import year_ce


FIELDS = {"d13C": "δ13C_value", "Ba/Ca": "Ba/Ca_value",
          "Sr/Ca": "Sr/Ca_value", "d18O": "δ18O_value"}
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
STRATUM_TOTAL = 300      # Sr/Ca、δ18O 每指标抽核目标值数
STRATUM_MIN = 30         # 每个来源类型×映射方式层的最少值数
PER_FILE_CAP = 24        # 单份源表单指标至多抽核值数
PAPER_EVIDENCE = {
    "Reed2021a4.txt": "Reed et al. (2021), DOI 10.1029/2020PA004051, pp. 6 and 9, Fig. 3b: publication uses μmol/mol; archive definition uses mmol/mol. Figure 3b is the WLF04 core from the same study.",
    "Reed2021a5.txt": "Reed et al. (2021), DOI 10.1029/2020PA004051, p. 9, Fig. 3b: WLF04 plotted in μmol/mol; archive definition for WLF04 annual values uses mmol/mol.",
    "Shen1992.txt": "Shen et al. (1992), local PDF pp. 20–21: printed data table also labels Ba/Ca mmol/mol.",
}
# 列义语义核对：源列定义或表头须点名目标指标
PROXY_DEFINITION_PATTERN = {
    "Sr/Ca": r"strontium|sr\s*/\s*ca",
    "d18O": r"delta\s*18|δ\s*18|d18o|18\s*o\s*/\s*16",
    "d13C": r"delta\s*13|δ\s*13|d13c|13\s*c\s*/\s*12",
    "Ba/Ca": r"barium|ba\s*/\s*ca",
}
_WORKBOOK_CACHE = {}


def read_archive(path, column, time_column):
    lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    marker = next(i for i, line in enumerate(lines) if line.strip() == "# Data:")
    header = next(line.split("\t") for line in lines[marker + 1:]
                  if line.strip() and not line.lstrip().startswith("#"))
    value_index = header.index(column)
    time_index = header.index(time_column)
    definition = next((line for line in lines[:marker]
                       if line.startswith((f"## {column}\t", f"##{column}\t"))), "")
    unit = definition.split("\t", 1)[1].split(",")[3].strip() if definition else ""
    return lines, value_index, time_index, unit, definition


def read_pangaea(path, column, time_column):
    lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    end = next(i for i, line in enumerate(lines) if line.strip() == "*/")
    header = lines[end + 1].split("\t")
    value_index = header.index(column)
    time_index = header.index(time_column)
    bracket = re.search(r"\[([^\]]+)\]", column)
    unit = bracket.group(1).strip() if bracket else ""
    short = column.split(" [")[0].strip()
    definition = next((line.strip() for line in lines[:end]
                       if re.search(r"\(" + re.escape(short) + r"\)", line)), "")
    if not unit and definition:
        bracket = re.search(r"\[([^\]]+)\]", definition)
        unit = bracket.group(1).strip() if bracket else ""
    return lines, value_index, time_index, unit, definition


def read_workbook(path, sheet, column, time_column, header_line):
    key = (str(path), sheet)
    if key not in _WORKBOOK_CACHE:
        from openpyxl import load_workbook
        book = load_workbook(path, read_only=True, data_only=True)
        name = sheet if sheet in book.sheetnames else book.sheetnames[0]
        _WORKBOOK_CACHE[key] = [tuple(row) for row in book[name].iter_rows(values_only=True)]
        book.close()
    rows = _WORKBOOK_CACHE[key]
    header, header_at = None, None
    if header_line is not None and 0 < int(header_line) <= len(rows):
        candidate = [str(cell).strip() if cell is not None else "" for cell in rows[int(header_line) - 1]]
        if column.strip() in candidate:
            header, header_at = candidate, int(header_line) - 1
    if header is None:
        for at, candidate in enumerate(rows[:30]):
            texts = [str(cell).strip() if cell is not None else "" for cell in candidate]
            if column.strip() in texts:
                header, header_at = texts, at
                break
    if header is None:
        raise ValueError(f"工作表未找到列头: {column}")
    value_index = header.index(column.strip())
    time_index = header.index(time_column.strip())
    bracket = re.search(r"\[([^\]]+)\]|\(([^)]*(?:mol|‰|per mil|deg)[^)]*)\)", column)
    unit = next((g for g in (bracket.groups() if bracket else ()) if g), "")
    if not unit:
        # 单位常单独占一行（表头行的上一行或下一行）
        for at in (header_at + 1, header_at - 1):
            if 0 <= at < len(rows) and value_index < len(rows[at]):
                text = str(rows[at][value_index] or "").strip()
                if re.search(r"mol|‰|per mil|deg|°C", text, re.I) and len(text) <= 24:
                    unit = text
                    break
    definition = header[value_index]
    if not unit:
        # NOAA 贡献模板：数据块上方有 Variables 表（Short_name/What/Units）
        for at, candidate in enumerate(rows[:header_at]):
            texts = [str(cell).strip() if cell is not None else "" for cell in candidate]
            if "Short_name" in texts and "Units" in texts:
                short_at, unit_at = texts.index("Short_name"), texts.index("Units")
                what_at = texts.index("What") if "What" in texts else None
                for var_row in rows[at + 1:header_at]:
                    name = str(var_row[short_at] or "").strip() if short_at < len(var_row) else ""
                    if name.casefold() == column.strip().casefold():
                        unit = str(var_row[unit_at] or "").strip() if unit_at < len(var_row) else ""
                        if what_at is not None and what_at < len(var_row) and var_row[what_at]:
                            definition = f"{var_row[what_at]} ({name})"
                        break
                break
    return rows, value_index, time_index, unit.strip(), definition


def source_scale(proxy, unit):
    unit = unit.casefold().replace("µ", "μ")
    if proxy == "Ba/Ca":
        return {
            "micromole per mole": 1, "μmol/mol": 1,
            "millimole per mole": 1000, "mmol/mol": 1000,
            "nanomole per mole": 0.001, "nmol/mol": 0.001,
            "mole per mole": 1_000_000, "mol/mol": 1_000_000,
        }.get(unit, np.nan)
    if proxy == "Sr/Ca":
        return {
            "millimole per mole": 1, "mmol/mol": 1,
            "micromole per mole": 0.001, "μmol/mol": 0.001,
            "mole per mole": 1000, "mol/mol": 1000,
        }.get(unit, np.nan)
    # PDB is the source's reported label. This checks numeric copying, not scale identity.
    return 1 if ("per mil" in unit or "permil" in unit or "‰" in unit) else np.nan


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


def spread_across_files(group, quota):
    """层内跨源文件均匀布点：先均匀选文件，再在文件内均匀选行，单表不超过 PER_FILE_CAP。"""
    files = sorted(group["source_file"].unique())
    n_files = min(len(files), max(1, int(np.ceil(quota / PER_FILE_CAP))))
    picked = [files[i] for i in np.linspace(0, len(files) - 1, n_files, dtype=int)]
    per, extra = divmod(quota, n_files)
    pieces = []
    for i, name in enumerate(picked):
        rows = group.loc[group["source_file"].eq(name)].sort_values(["source_line", "observation_id"])
        take = min(len(rows), per + (i < extra), PER_FILE_CAP)
        pieces.append(rows.iloc[np.linspace(0, len(rows) - 1, take, dtype=int)])
    return pd.concat(pieces)


def route_stratified_sample(frame, proxy):
    """Sr/Ca、δ18O：按来源类型×映射方式分层，比例分配且每层不少于 STRATUM_MIN。"""
    subset = frame.loc[frame[proxy].notna() & frame["source_file"].notna()].copy()
    subset["source_type"] = (subset["source_file"].str.replace("\\", "/", regex=False)
                             .str.split("/").str[0])
    groups = dict(tuple(subset.groupby(["source_type", "mapping_route"], sort=True)))
    sizes = pd.Series({key: len(group) for key, group in groups.items()})
    floor = sizes.clip(upper=STRATUM_MIN)
    remainder = STRATUM_TOTAL - int(floor.sum())
    if remainder < 0:
        raise ValueError("分层最小额度超出抽核总量")
    weights = (sizes - floor).clip(lower=0)
    share = (weights / weights.sum() * remainder).round().astype(int) if weights.sum() else weights.astype(int)
    quota = (floor + share).to_dict()
    return pd.concat([spread_across_files(group, quota[key]) for key, group in groups.items()])


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
    for proxy in ("Sr/Ca", "d18O"):
        for _, row in route_stratified_sample(frame, proxy).iterrows():
            source = str(row["source_file"]).replace("\\", "/")
            records.append(check_row(row, proxy, FIELDS[proxy], source, root))
    audit = pd.DataFrame(records)
    if audit.empty:
        raise ValueError("No configured source tables could be sampled")
    audit.to_csv(output / "source_table_audit.csv", index=False, encoding="utf-8-sig")
    summarize_audit(audit).to_csv(output / "source_table_audit_summary.csv",
                                  index=False, encoding="utf-8-sig")
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


def summarize_audit(audit):
    audit = audit.copy()
    audit["transcription_match"] = (audit["raw_value_match"]
                                    & (audit["time_token_match"] | audit["year_match"]))
    rows = []
    for (proxy, route), group in audit.groupby(["proxy", "mapping_route"], sort=True):
        known = group["source_label_standardized"].notna()
        rows.append({
            "proxy": proxy, "mapping_route": route, "n_sampled": len(group),
            "n_source_files": group["source_file"].nunique(),
            "raw_value_match": int(group["raw_value_match"].sum()),
            "time_or_year_match": int((group["time_token_match"] | group["year_match"]).sum()),
            "transcription_match": int(group["transcription_match"].sum()),
            "definition_matches_proxy": int(group["definition_matches_proxy"].sum()),
            "n_unit_scale_known": int(known.sum()),
            "unit_conversion_agrees": int((known & group["difference"].abs().le(1e-7)).sum()),
            "unresolved_locator": int(group["verdict"].eq("Unresolved source locator").sum()),
        })
    return pd.DataFrame(rows)


def check_row(row, proxy, field, source, root):
    result = {
        "proxy": proxy, "observation_id": row["observation_id"], "source_file": source,
        "source_type": source.split("/")[0],
        "mapping_route": row.get("mapping_route", ""),
        "source_line": row["source_line"], "source_year": np.nan, "archived_year": row["Year"],
        "source_numeric": np.nan, "archived_numeric": row[proxy], "source_label_unit": "",
        "source_label_standardized": np.nan, "difference": np.nan, "source_definition": "",
        "definition_matches_proxy": False,
        "raw_value_match": False, "time_token_match": False, "year_match": False,
        "paper_evidence": PAPER_EVIDENCE.get(Path(source).name, ""),
    }
    try:
        column = json.loads(row["source_columns"])[field]
        path = root / source
        suffix = path.suffix.lower()
        if suffix in (".xlsx", ".xls", ".xlsm"):
            lines, val_idx, time_idx, unit, definition = read_workbook(
                path, row.get("source_sheet_name"), column, row["source_time_column"],
                row.get("source_header_line"))
            cells = lines[int(row["source_line"]) - 1]
            source_value = float(cells[val_idx])
            source_time = str(cells[time_idx]).strip()
        else:
            head = path.read_text(encoding="utf-8-sig", errors="replace").lstrip()
            reader = read_pangaea if head.startswith("/*") else read_archive
            lines, val_idx, time_idx, unit, definition = reader(path, column, row["source_time_column"])
            source_cells = lines[int(row["source_line"]) - 1].split("\t")
            source_value = float(source_cells[val_idx])
            source_time = source_cells[time_idx].strip()
        semantic = f"{definition} {column}"
        matches = bool(re.search(PROXY_DEFINITION_PATTERN[proxy], semantic, re.I))
        result.update(source_numeric=source_value, source_label_unit=unit,
                      source_definition=definition, definition_matches_proxy=matches)
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
        verdict = ("Transcription matches; no unit label in source" if not unit
                   else "Unresolved source unit")
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


def main():
    from app.analysis import loader
    parser = argparse.ArgumentParser(description="确定性源表抽核（不调用模型）")
    parser.add_argument("--run", type=Path, required=True, help="提取批次目录")
    parser.add_argument("--output", type=Path, default=HERE)
    args = parser.parse_args()
    frame = pd.read_csv(args.run / "consolidated" / "all_data_combined.csv",
                        dtype={"source_time_value": "string"}, low_memory=False)
    frame = frame.rename(columns=loader.SUMMARY_COL_MAP)
    args.output.mkdir(parents=True, exist_ok=True)
    audit, _ = audit_source_tables(frame, ROOT, args.output)
    print(summarize_audit(audit).to_string(index=False))


if __name__ == "__main__":
    main()