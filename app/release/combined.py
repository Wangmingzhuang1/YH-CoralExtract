"""合并发布包：YH-CoralGeo 为主体，补入 CoralHydro2k 独有记录（并集去重）。

去重依据金标准 crosswalk（sequence_crosswalk.csv）：entity 的任一系列
有 local_series_id 或状态非"待核，无来源对应候选"即视为已被 YH 覆盖，
保留 YH 提取版；仅全部系列均无来源对应候选的 entity 才是 CoralHydro2k
独有，从基线 zip 原样取出其 LiPD 并入 01_records，观测解析后对齐 YH
observations.csv 的同名 schema（含 source_time_* 原始年代列）并入合并表。

CLI: python -m app.release.combined --base <YH发布目录> --baseline <CoralHydro2k.zip>
     --crosswalk <sequence_crosswalk.csv> --out <合并发布目录> [--version 1.1.0_combined]
"""
import argparse
import hashlib
import io
import json
import logging
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import docs
from .schema import PROXIES, proxy_group_of

logger = logging.getLogger(__name__)

NO_CANDIDATE = "待核，无来源对应候选"
BASELINE_LABEL = "CoralHydro2k v1.0.1"
PROXY_NAMES = list(PROXIES)

# LiPD variableName（小写）-> 发布 schema 代理名；未映射变量（如海水 d18O_sw）
# 不属于珊瑚骨骼代理 schema，剔除并在合并账本中披露。
VARIABLE_TO_PROXY = {
    "d18o": "d18O", "srca": "sr_ca", "sr/ca": "sr_ca",
    "d13c": "d13C", "baca": "ba_ca", "ba/ca": "ba_ca",
    "sst": "sst", "seasurfacetemperature": "sst",
}
YEAR_UNITS = {"ad", "ce", "year ce"}

# 与 app/core/moa/quality_control.py 晚全新世/CE 段一致的物理范围；
# 导入值按同一范围复检给 qc_status（ba_ca 项目未定义范围，有限值即 pass）。
VALUE_RANGES = {"d18O": (-6.0, 3.0), "sr_ca": (8.0, 10.0),
                "d13C": (-15.0, 5.0), "sst": (-2.0, 35.0), "ba_ca": None}

PROVENANCE_COLUMNS = [
    "observation_id", "record_id", "proxy", "released_value", "original_value",
    "original_unit", "unit_status", "qc_status", "unit_evidence",
    "source_file", "source_attachment", "sheet_name", "source_row", "source_line",
    "source_page", "source_core_id", "source_processing", "composite_evidence"]


def unique_entities(crosswalk: pd.DataFrame) -> list:
    """返回 CoralHydro2k 独有 entity（全部系列均无 YH 来源对应候选）。"""
    has_local = crosswalk["local_series_id"].fillna("").astype(str).str.strip().ne("")
    covered = crosswalk["status"].ne(NO_CANDIDATE) | has_local
    by_entity = covered.groupby(crosswalk["entity"]).any()
    return sorted(by_entity.index[~by_entity].tolist())


def _observation_id(entity: str, year: float) -> str:
    key = f"CoralHydro2k|{entity}|{float(year)!r}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _qc_status(name: str, value: float, centered: bool = False) -> str:
    if value is None or not np.isfinite(value):
        return "missing_or_invalid_value"
    if centered:
        return "suspect_centered_series"
    bounds = VALUE_RANGES[name]
    if bounds and not bounds[0] <= value <= bounds[1]:
        return "outside_range"
    return "pass"


def _centered_proxies(merged: pd.DataFrame) -> dict:
    """识别中心化/距平序列：Sr/Ca 绝对值物理上约 8-10 mmol/mol，
    中位数 < 1 即判定为距平序列（与 YH 提取的 suspect_centered_series 口径一致）。"""
    centered = {}
    for name in PROXY_NAMES:
        if name not in merged:
            continue
        values = pd.to_numeric(merged[name], errors="coerce").dropna()
        if name == "sr_ca" and len(values) and float(values.median()) < 1.0:
            centered[name] = True
    return centered


def parse_entity(lpd_bytes: bytes, entity: str, source_file: str) -> dict:
    """解析一个 CoralHydro2k LiPD，产出对齐 YH schema 的观测/溯源/记录元数据。

    同 entity 的多张测量表按 year 外连接合并为一行多代理；表内年份重复
    或跨表同代理同年冲突直接报错，不做静默聚合。
    """
    with zipfile.ZipFile(io.BytesIO(lpd_bytes)) as bag:
        meta_name = next(n for n in bag.namelist() if n.endswith("/metadata.jsonld"))
        metadata = json.loads(bag.read(meta_name))
        folder = str(Path(meta_name).parent).replace("\\", "/")
        if metadata.get("dataSetName") != entity:
            raise ValueError(f"LiPD 身份与文件名不符: {metadata.get('dataSetName')} != {entity}")
        tables = []
        for paleo in metadata.get("paleoData", []):
            tables.extend(paleo.get("measurementTable", []))
        if not tables:
            raise ValueError(f"{entity}: 无测量表")

        excluded, merged, resolutions, cores = [], None, [], {}
        proxy_table, proxy_units = {}, {}
        for table_id, table in enumerate(tables):
            columns = {str(c["variableName"]).casefold(): c for c in table["columns"]}
            year_col = columns.get("year")
            if year_col is None:
                raise ValueError(f"{entity}/{table['filename']}: 缺 year 列")
            if str(year_col.get("units", "")).casefold() not in YEAR_UNITS:
                raise ValueError(f"{entity}: 未处理的年代单位 {year_col.get('units')}")
            raw = pd.read_csv(bag.open(f"{folder}/{table['filename']}"), header=None,
                              encoding="utf-8-sig", float_precision="round_trip")
            years = pd.to_numeric(raw.iloc[:, int(year_col["number"]) - 1], errors="coerce")
            if years.isna().any():
                raise ValueError(f"{entity}/{table['filename']}: 存在缺年代行")
            if years.duplicated().any():
                raise ValueError(f"{entity}/{table['filename']}: 表内年份重复，需人工裁决")
            frame = pd.DataFrame({"year_ce": years.to_numpy()})
            frame[f"__row_{table_id}"] = np.arange(1, len(frame) + 1)  # 原始CSV行号（溯源用）
            table_resolution = None
            has_proxy = False
            for key, col in columns.items():
                if key == "year":
                    continue
                proxy = VARIABLE_TO_PROXY.get(key)
                if proxy is None:
                    excluded.append({"table": table["filename"], "variableName": col["variableName"],
                                     "units": col.get("units"), "reason": "not_a_skeletal_proxy_in_schema"})
                    continue
                values = pd.to_numeric(raw.iloc[:, int(col["number"]) - 1], errors="coerce")
                if proxy in proxy_table:
                    raise ValueError(f"{entity}: 代理 {proxy} 跨表重复，需人工裁决")
                frame[proxy] = values.to_numpy()
                has_proxy = True
                units = str(col.get("units", "")).casefold()
                if proxy == "sr_ca" and units != "mmol/mol":
                    raise ValueError(f"{entity}: Sr/Ca 单位未处理 {col.get('units')}")
                if proxy in ("d18O", "d13C") and units not in {"permil", "permil vpdb", "‰ vpdb"}:
                    raise ValueError(f"{entity}: {proxy} 单位未处理 {col.get('units')}")
                nominal = (col.get("hasResolution") or {}).get("nominal")
                if nominal and table_resolution is None:
                    table_resolution = str(nominal)
                core = col.get("coreName")
                if core:
                    cores.setdefault(proxy, str(core))
                proxy_table[proxy] = table_id
                proxy_units[proxy] = str(col.get("units", ""))
            if not has_proxy:
                continue  # 整表均为非 schema 变量（如海水 d18O_sw）
            resolutions.append(table_resolution or "unknown")
            merged = frame if merged is None else merged.merge(frame, on="year_ce", how="outer")
        if merged is None:
            raise ValueError(f"{entity}: 无可并入 schema 的测量表")

    merged = merged.sort_values("year_ce", kind="stable").reset_index(drop=True)
    resolution = next((r for r in resolutions if r != "unknown"), "unknown")
    geo = metadata.get("geo") or {}
    coords = (geo.get("geometry") or {}).get("coordinates") or []
    lon = float(coords[0]) if len(coords) > 0 and str(coords[0]) != "NA" else None
    lat = float(coords[1]) if len(coords) > 1 and str(coords[1]) != "NA" else None
    pub = (metadata.get("pub") or [{}])[0]

    obs = pd.DataFrame(index=merged.index)
    obs["observation_id"] = [_observation_id(entity, y) for y in merged["year_ce"]]
    obs["record_id"] = entity
    obs["site_name"] = geo.get("siteName")
    obs["latitude"] = lat
    obs["longitude"] = lon
    obs["year_ce"] = merged["year_ce"]
    obs["time_resolution"] = resolution
    obs["native_interval_years"] = np.nan
    obs["source_time_value"] = merged["year_ce"]
    obs["source_time_unit"] = "CE"
    obs["source_time_reference_year"] = np.nan
    obs["source_time_column"] = "year"
    obs["qc_flag"] = 0
    obs["record_code"] = entity
    obs["reprint_of"] = ""
    obs["source_core_id"] = ";".join(dict.fromkeys(cores.values()))
    obs["source_processing"] = ""
    obs["composite_evidence"] = ""
    provenance = []
    centered = _centered_proxies(merged)
    for name in PROXY_NAMES:
        values = merged[name] if name in merged else pd.Series(np.nan, index=merged.index)
        obs[name] = values
        obs[f"{name}_qc_status"] = [_qc_status(name, v, centered.get(name, False)) for v in values]
        if name not in proxy_table:
            continue
        source_rows = merged[f"__row_{proxy_table[name]}"]
        for i, value in enumerate(values):
            if pd.isna(value):
                continue
            provenance.append({
                "observation_id": obs["observation_id"].iloc[i], "record_id": entity,
                "proxy": name, "released_value": float(value), "original_value": float(value),
                "original_unit": proxy_units[name],
                "unit_status": "curated_baseline", "qc_status": obs[f"{name}_qc_status"].iloc[i],
                "unit_evidence": json.dumps({"source": BASELINE_LABEL, "import": "as_is_curated"}),
                "source_file": source_file, "source_attachment": "",
                "sheet_name": f"{entity}.lpd", "source_row": int(source_rows.iloc[i]),
                "source_line": "", "source_page": "", "source_core_id": cores.get(name, ""),
                "source_processing": "", "composite_evidence": ""})
    present = [name for name in PROXY_NAMES
               if name in merged and pd.to_numeric(merged[name], errors="coerce").notna().any()]
    years = merged["year_ce"]
    record = {
        "record_id": entity, "record_code": entity, "site_name": geo.get("siteName"),
        "latitude": lat, "longitude": lon,
        "year_min_ce": float(years.min()), "year_max_ce": float(years.max()),
        "n_observations": int(len(merged)), "time_resolutions": resolution,
        "proxies": ";".join(present), "source_files": source_file,
        "is_reprint": False, "reprint_of": "",
        "pub_doi": pub.get("doi"), "pub_authors": pub.get("author"),
        "dataset_doi": metadata.get("originalDataUrl"), "dataset_investigators": None,
        "dating_method": None, "dating_error": None, "testing_method": None,
        "dating_method_reported": False,
        "source": "CoralHydro2k", "source_version": str(metadata.get("datasetVersion", "")),
    }
    return {"observations": obs, "provenance": pd.DataFrame(provenance, columns=PROVENANCE_COLUMNS),
            "record": record, "excluded": excluded, "metadata": metadata,
            "centered": sorted(centered),
            "n_values": int(sum(obs[name].notna().sum() for name in PROXY_NAMES))}


def _view_filename(label: str) -> str:
    """分辨率标签 -> 文件系统安全名（'>annual' -> 'gt_annual'）。"""
    return str(label).replace(">", "gt_").replace("<", "lt_").replace("/", "_")


def _regenerate_derived(out_dir: Path, observations: pd.DataFrame) -> None:
    for layer in ("by_resolution", "by_proxy_group"):
        target = out_dir / "03_derived" / layer
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
    for resolution, sub in observations.groupby("time_resolution", sort=True):
        sub.to_csv(out_dir / "03_derived" / "by_resolution" / f"{_view_filename(resolution)}.csv",
                   index=False, encoding="utf-8")
    presence = observations[PROXY_NAMES].notna()
    groups = presence.apply(lambda row: proxy_group_of(p for p in PROXY_NAMES if row[p]), axis=1)
    for group, sub in observations.groupby(groups, sort=True):
        sub.to_csv(out_dir / "03_derived" / "by_proxy_group" / f"{group}.csv",
                   index=False, encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _append_combined_docs(out_dir: Path, version: str, summary: dict, ledger: pd.DataFrame) -> None:
    accounting = summary["accounting"]
    merge = accounting["combined"]
    excluded = "; ".join(f"{row['entity']}:{row['excluded_variables']}"
                         for _, row in ledger.iterrows() if row["excluded_variables"]) or "none"
    section = f"""
## Combined release: YH-CoralGeo + CoralHydro2k (union, YH-primary)

This package is the union of **YH-CoralGeo v{merge['base_version']}** (primary) and
**{BASELINE_LABEL}** records not covered by YH-CoralGeo. Deduplication follows the
golden-set sequence crosswalk (`04_provenance_qc/coralhydro2k_unique_crosswalk.csv`
and the full comparison crosswalk): a CoralHydro2k entity whose series has any YH
source-linked or value-matched candidate is considered covered and keeps the YH
extracted version; only entities whose every series has status
`{NO_CANDIDATE}` (no source-linked candidate) are imported from CoralHydro2k.

- Imported CoralHydro2k-only entities: **{merge['unique_entities_count']}**
  ({', '.join(merge['unique_entities'])})
- Records: {accounting['records']} = {merge['base_records']} YH-CoralGeo +
  {merge['added_records']} CoralHydro2k (original curated LiPD preserved byte-for-byte
  in `01_records`, datasetVersion retained in `records_metadata.source_version`)
- Observations: {accounting['observation_rows']:,} = {merge['base_rows']:,} YH-CoralGeo +
  {merge['added_rows']:,} CoralHydro2k
- `records_metadata.csv` adds `source` (YH-CoralGeo | CoralHydro2k) and `source_version`;
  `observations.csv` keeps the identical schema to the YH-CoralGeo base, including
  the `source_time_*` original-age columns (join on `record_id`).
- CoralHydro2k values are imported as-is from the curated baseline and re-checked
  against the project physical ranges for `*_qc_status`; original resolution labels
  are preserved (including `>annual`, meaning coarser than annual). Centered/anomaly
  series (e.g. HE02GBR01 Sr/Ca) are flagged `suspect_centered_series` and must not be
  used as absolute values without recalibration (see merge ledger).
- Excluded non-schema variables (disclosed in the merge ledger): {excluded}.
  Seawater d18O (`d18O_sw`) is not a coral skeletal proxy and is not part of the
  compilation schema; the original LiPD in `01_records` still contains it.
- `03_derived/gridded/paleoclimate_grid.nc` is inherited from the YH-CoralGeo
  v{merge['base_version']} layer and does not include the CoralHydro2k-only records.
- Per-value provenance for imported rows points to
  `CoralHydro2k1_0_1.zip!<entity>.lpd` (table row level); see
  `04_provenance_qc/coralhydro2k_merge_ledger.csv`.
"""
    readme = out_dir / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8") + section, encoding="utf-8")
    dictionary = out_dir / "DATA_DICTIONARY.md"
    dictionary.write_text(dictionary.read_text(encoding="utf-8") + """
## Combined-release additions

- `records_metadata.csv` gains `source` (YH-CoralGeo | CoralHydro2k) and
  `source_version` (release version for YH rows; curated datasetVersion for
  CoralHydro2k rows). CoralHydro2k `record_id`/`record_code` equal the LiPD
  `dataSetName`; `source_files` is `CoralHydro2k1_0_1.zip!<entity>.lpd`.
- `time_resolution` may carry the CoralHydro2k label `>annual` (coarser than
  annual) in addition to the YH vocabulary; its derived view file is named
  `by_resolution/gt_annual.csv` (filesystem-safe spelling).
- CoralHydro2k `*_qc_status` reflects a physical-range re-check of the curated
  baseline values (import as-is; no re-extraction); `unit_status` in
  value_provenance is `curated_baseline` for these rows.
""", encoding="utf-8")
    changelog = f"""# Changelog

## v{version} ({summary['generated_at'][:10]})

- Combined release: YH-CoralGeo v{merge['base_version']} (primary) union
  {BASELINE_LABEL} entities without any YH source-linked candidate.
- {merge['added_records']} CoralHydro2k-only entities imported as original curated
  LiPD; {merge['added_rows']:,} observations appended with schema-aligned columns.
- Total: {accounting['observation_rows']:,} observations, {accounting['records']} records.
- Merge strategy, source composition and exclusions documented in README and
  04_provenance_qc/coralhydro2k_merge_ledger.csv.

## v{merge['base_version']} (base layer)

- See the YH-CoralGeo v{merge['base_version']} release changelog (01_records and
  02_compilation YH rows are unchanged from that release).
"""
    (out_dir / "CHANGELOG.md").write_text(changelog, encoding="utf-8")


def build_combined(base_dir, baseline_zip, crosswalk_path, out_dir,
                   version: str = "1.1.0_combined") -> dict:
    base_dir, out_dir = Path(base_dir), Path(out_dir)
    if out_dir.exists():
        raise FileExistsError(f"输出目录已存在（不覆盖）: {out_dir}")
    crosswalk = pd.read_csv(crosswalk_path)
    entities = unique_entities(crosswalk)
    logger.info("CoralHydro2k 独有 entity: %d 个 %s", len(entities), entities)

    base_summary = json.loads((base_dir / "02_compilation" / "compilation_summary.json")
                              .read_text(encoding="utf-8"))
    base_version = base_summary.get("version", "unknown")
    shutil.copytree(base_dir, out_dir)

    base_obs = pd.read_csv(out_dir / "02_compilation" / "observations.csv", low_memory=False)
    base_rec = pd.read_csv(out_dir / "02_compilation" / "records_metadata.csv", low_memory=False)
    if set(entities) & (set(base_rec["record_code"]) | set(base_rec["record_id"])):
        raise ValueError("CoralHydro2k entity 与 YH 记录标识冲突")

    obs_parts, prov_parts, rec_rows, ledger_rows = [], [], [], []
    baseline_name = Path(baseline_zip).name
    with zipfile.ZipFile(baseline_zip) as release:
        for entity in entities:
            source_file = f"{baseline_name}!{entity}.lpd"
            payload = release.read(f"{entity}.lpd")
            (out_dir / "01_records" / f"{entity}.lpd").write_bytes(payload)
            parsed = parse_entity(payload, entity, source_file)
            obs_parts.append(parsed["observations"])
            prov_parts.append(parsed["provenance"])
            rec_rows.append(parsed["record"])
            status = ";".join(sorted(crosswalk.loc[crosswalk["entity"].eq(entity), "status"].unique()))
            ledger_rows.append({
                "entity": entity, "crosswalk_status": status,
                "pub1_doi": parsed["record"]["pub_doi"],
                "dataset_version": parsed["record"]["source_version"],
                "rows_imported": parsed["record"]["n_observations"],
                "values_imported": parsed["n_values"],
                "proxies": parsed["record"]["proxies"],
                "centered_series": ";".join(parsed["centered"]),
                "excluded_variables": ";".join(sorted({e["variableName"] for e in parsed["excluded"]})),
                "source_file": source_file})
            logger.info("并入 %s: %d 行 / %d 值", entity,
                        parsed["record"]["n_observations"], parsed["n_values"])

    ch2k_obs = pd.concat(obs_parts, ignore_index=True)[base_obs.columns]
    observations = pd.concat([base_obs, ch2k_obs], ignore_index=True)
    base_rec["source"] = "YH-CoralGeo"
    base_rec["source_version"] = base_version
    ch2k_rec = pd.DataFrame(rec_rows)
    records = pd.concat([base_rec, ch2k_rec], ignore_index=True)
    if observations["observation_id"].duplicated().any():
        raise ValueError("合并后 observation_id 冲突")
    observations.to_csv(out_dir / "02_compilation" / "observations.csv", index=False, encoding="utf-8")
    records.to_csv(out_dir / "02_compilation" / "records_metadata.csv", index=False, encoding="utf-8")

    _regenerate_derived(out_dir, observations)

    base_prov = pd.read_csv(out_dir / "04_provenance_qc" / "value_provenance.csv", low_memory=False)
    provenance = pd.concat([base_prov, *prov_parts], ignore_index=True)
    provenance.to_csv(out_dir / "04_provenance_qc" / "value_provenance.csv", index=False, encoding="utf-8")
    ledger = pd.DataFrame(ledger_rows)
    ledger.to_csv(out_dir / "04_provenance_qc" / "coralhydro2k_merge_ledger.csv",
                  index=False, encoding="utf-8")
    crosswalk[crosswalk["entity"].isin(entities)].to_csv(
        out_dir / "04_provenance_qc" / "coralhydro2k_unique_crosswalk.csv",
        index=False, encoding="utf-8")

    # 质控与统计指纹：在基线报告上重算合并口径
    report = json.loads((out_dir / "04_provenance_qc" / "qc_report.json").read_text(encoding="utf-8"))
    years = pd.to_numeric(observations["year_ce"], errors="coerce")
    in_core = years.between(report["scope"]["common_era_core_range_ce"][0],
                            report["scope"]["common_era_core_range_ce"][1])
    report.update({
        "observation_rows": int(len(observations)),
        "records": int(len(records)),
        "canonical_records": int((~records["is_reprint"].astype(bool)).sum()),
        "reprint_records": int(records["is_reprint"].astype(bool).sum()),
        "sites": int(observations["site_name"].nunique()),
        "year_range_ce": [float(years.min()), float(years.max())],
        "row_qc_flag_counts": {str(k): int(v) for k, v in
                               observations["qc_flag"].value_counts(dropna=False).items()},
        "value_qc_status_counts": {
            name: {str(k): int(v) for k, v in
                   observations[f"{name}_qc_status"].value_counts(dropna=False).items()}
            for name in PROXY_NAMES},
        "proxy_non_null_values": {name: int(observations[name].notna().sum()) for name in PROXY_NAMES},
        "resolution_counts": {str(k): int(v) for k, v in
                              observations["time_resolution"].value_counts(dropna=False).items()},
    })
    report["scope"]["rows_within_core_range"] = int(in_core.sum())
    report["scope"]["rows_outside_core_range"] = int((~in_core).sum())
    report["combined"] = {
        "policy": "union_deduplicated_YH_primary",
        "base_version": base_version,
        "coralhydro2k_baseline": BASELINE_LABEL,
        "crosswalk": str(Path(crosswalk_path).name),
        "unique_entities": entities,
        "unique_entities_count": len(entities),
        "base_records": int(len(base_rec)),
        "base_rows": int(len(base_obs)),
        "added_records": int(len(ch2k_rec)),
        "added_rows": int(len(ch2k_obs)),
        "source_records": {"YH-CoralGeo": int(len(base_rec)), "CoralHydro2k": int(len(ch2k_rec))},
        "gridded_note": "gridded netCDF inherited from YH-CoralGeo layer; excludes CoralHydro2k-only records",
    }
    (out_dir / "04_provenance_qc" / "qc_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    summary = {
        "dataset": "YH-CoralGeo + CoralHydro2k Combined Coral Proxy Compilation",
        "version": version,
        "source_batch": base_summary.get("source_batch"),
        "combined_from": {"base": str(base_dir), "base_version": base_version,
                          "baseline": baseline_name, "crosswalk": str(crosswalk_path)},
        "source_csv_sha256": base_summary.get("source_csv_sha256"),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "accounting": report,
        "sha256": {
            "observations.csv": _sha256(out_dir / "02_compilation" / "observations.csv"),
            "records_metadata.csv": _sha256(out_dir / "02_compilation" / "records_metadata.csv"),
        },
    }
    (out_dir / "02_compilation" / "compilation_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    docs.write_docs(out_dir, version, summary, records)
    _append_combined_docs(out_dir, version, summary, ledger)
    logger.info("合并发布包完成: %s（%d 记录 / %d 行）", out_dir, len(records), len(observations))
    return summary


def main():
    parser = argparse.ArgumentParser(description="YH-CoralGeo + CoralHydro2k 独有记录合并发布包")
    parser.add_argument("--base", required=True, help="YH-CoralGeo 发布目录（四层结构）")
    parser.add_argument("--baseline", required=True, help="CoralHydro2k 基线 zip")
    parser.add_argument("--crosswalk", required=True, help="金标准 sequence_crosswalk.csv")
    parser.add_argument("--out", required=True, help="合并发布包输出目录（须不存在）")
    parser.add_argument("--version", default="1.1.0_combined", help="合并数据集版本号")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    build_combined(args.base, args.baseline, args.crosswalk, args.out, args.version)


if __name__ == "__main__":
    main()