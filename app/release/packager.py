"""发布打包主流程：run 批次 -> Zenodo 分层数据集。

层1 01_records      每条记录一个标准 LiPD（含转载记录，reprintOf 留痕）
层2 02_compilation  observations.csv 精简主表 + records_metadata.csv + 统计指纹
层3 03_derived      分辨率/代理组视图 + 格点 netCDF（可由层2再生）
层4 04_provenance_qc 源文件账本、逐值溯源长表、复核裁决、暂缓值、转载证据
"""
import hashlib
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import docs
from .lipd import assign_record_codes, write_record_lpd
from .reprints import detect_reprints
from .schema import (OBSERVATION_COLUMNS, PROXIES, RECORD_METADATA_COLUMNS,
                     proxy_group_of)

logger = logging.getLogger(__name__)

PROXY_NAMES = list(PROXIES)

# 论文分析的核心范围（Common Era 珊瑚）；发布全量收录，范围在文档中声明
CORE_RANGE_CE = (-10050, 2025)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _first_non_null(series):
    values = series.dropna()
    return values.iloc[0] if len(values) else None


def _ensure_record_ids(frame: pd.DataFrame) -> None:
    """给未声明记录身份的观测分配文件和表块级标识，不推断岩芯。"""
    ids = frame["source_record_id"].astype("string")
    missing = ids.isna() | ids.str.strip().eq("")
    if not missing.any():
        return
    files = frame.loc[missing, "source_file"].fillna("").astype(str)
    if "source_attachment" in frame:
        attachments = frame.loc[missing, "source_attachment"].fillna("").astype(str)
        files = attachments.where(attachments.str.strip().ne(""), files)
    sheets = frame.loc[missing, "sheet_name"].fillna("").astype(str)
    keys = files.str.replace("\\", "/", regex=False) + "\0" + sheets
    by_key = {key: "unlabelled-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
              for key in keys.unique()}
    if set(by_key.values()).intersection(ids.dropna()):
        raise ValueError("生成的无标签记录标识与已声明记录冲突")
    frame["source_record_id"] = ids
    frame.loc[missing, "source_record_id"] = keys.map(by_key)


def build_observations(frame: pd.DataFrame, codes: dict, reprint_of: dict) -> pd.DataFrame:
    """90 列源表 -> 精简主表（ASCII 表头，含原始年代列，单位进数据字典）。"""
    out = pd.DataFrame(index=frame.index)
    for target, source in OBSERVATION_COLUMNS.items():
        out[target] = frame[source] if source in frame else np.nan
    out["record_code"] = frame["source_record_id"].map(codes)
    out["reprint_of"] = frame["source_record_id"].map(reprint_of).map(codes).fillna("")
    for field in ("source_core_id", "source_processing", "composite_evidence"):
        if field in frame:
            out[field] = frame[field]
    for name in PROXY_NAMES:
        spec = PROXIES[name]
        out[name] = pd.to_numeric(frame[spec[0]], errors="coerce") if spec[0] in frame else np.nan
        out[f"{name}_qc_status"] = frame[spec[1]] if spec[1] in frame else "missing_or_invalid_value"
    if "paleoData_dating_method" in frame:
        dm = frame["paleoData_dating_method"]
        out["dating_method_reported"] = dm.notna() & dm.astype(str).str.strip().ne("")
    return out


def build_record_metadata(frame: pd.DataFrame, codes: dict, reprint_of: dict) -> pd.DataFrame:
    rows = []
    for record_id, sub in frame.groupby("source_record_id", sort=True):
        years = pd.to_numeric(sub["year_CE"], errors="coerce")
        row = {
            "record_id": record_id,
            "record_code": codes[record_id],
            "site_name": _first_non_null(sub["geo_siteName"]),
            "latitude": _first_non_null(sub["geo_latitude"]),
            "longitude": _first_non_null(sub["geo_longitude"]),
            "year_min_ce": float(np.nanmin(years)) if years.notna().any() else None,
            "year_max_ce": float(np.nanmax(years)) if years.notna().any() else None,
            "n_observations": int(len(sub)),
            "time_resolutions": ";".join(sorted(sub["time_resolution"].dropna().unique(), key=str)),
            "proxies": ";".join(name for name in PROXY_NAMES
                                if PROXIES[name][0] in sub
                                and pd.to_numeric(sub[PROXIES[name][0]], errors="coerce").notna().any()),
            "source_files": ";".join(sorted(sub["source_file"].dropna().unique(), key=str)),
            "is_reprint": record_id in reprint_of,
            "reprint_of": codes.get(reprint_of.get(record_id), ""),
        }
        for target, source in RECORD_METADATA_COLUMNS.items():
            row[target] = _first_non_null(sub[source]) if source in sub else None
        dating = sub.get("paleoData_dating_method")
        row["dating_method_reported"] = bool(
            dating is not None and (dating.notna() & dating.astype(str).str.strip().ne("")).any())
        rows.append(row)
    return pd.DataFrame(rows)


def build_value_provenance(frame: pd.DataFrame) -> pd.DataFrame:
    """逐值溯源长表：一行 = 一个观测 × 一个代理（仅非缺测值）。"""
    chunks = []
    base_cols = ["observation_id", "source_record_id", "source_file", "source_attachment", "sheet_name",
                 "source_row", "source_line", "source_page", "source_core_id", "source_processing",
                 "composite_evidence"]
    base = frame[[c for c in base_cols if c in frame]].copy()
    base = base.rename(columns={"source_record_id": "record_id"})
    for name in PROXY_NAMES:
        spec = PROXIES[name]
        if spec[0] not in frame:
            continue
        values = pd.to_numeric(frame[spec[0]], errors="coerce")
        sub = base.loc[values.notna()].copy()
        sub["proxy"] = name
        sub["released_value"] = values[values.notna()]
        for target, source in (("original_value", spec[3]), ("original_unit", spec[4]),
                               ("unit_status", spec[5]), ("qc_status", spec[1]),
                               ("unit_evidence", spec[6])):
            sub[target] = frame.loc[values.notna(), source] if source in frame else None
        chunks.append(sub)
    # 全 NA 列显式转 object，避免 concat 的 dtype 推断歧义（pandas FutureWarning）
    for chunk in chunks:
        for column in chunk.columns:
            if chunk[column].isna().all():
                chunk[column] = chunk[column].astype(object)
    provenance = pd.concat(chunks, ignore_index=True)
    columns = ["observation_id", "record_id", "proxy", "released_value", "original_value",
               "original_unit", "unit_status", "qc_status", "unit_evidence",
               "source_file", "source_attachment", "sheet_name", "source_row", "source_line",
               "source_page", "source_core_id", "source_processing", "composite_evidence"]
    return provenance[[c for c in columns if c in provenance]]


def build_qc_report(observations: pd.DataFrame, records: pd.DataFrame,
                    held: pd.DataFrame, run_dir: Path) -> dict:
    years = pd.to_numeric(observations["year_ce"], errors="coerce")
    in_core = years.between(CORE_RANGE_CE[0], CORE_RANGE_CE[1])
    report = {
        "observation_rows": int(len(observations)),
        "records": int(len(records)),
        "canonical_records": int((~records["is_reprint"]).sum()),
        "reprint_records": int(records["is_reprint"].sum()),
        "sites": int(observations["site_name"].nunique()),
        "year_range_ce": [float(years.min()), float(years.max())],
        "scope": {
            "policy": "full_inclusion",
            "common_era_core_range_ce": list(CORE_RANGE_CE),
            "rows_within_core_range": int(in_core.sum()),
            "rows_outside_core_range": int((~in_core).sum()),
            "note": ("All extracted records are released, including deep-time corals "
                     "outside the Common Era core range. The accompanying paper "
                     "declares the analysis scope; figures restrict to the core range."),
        },
        "row_qc_flag_counts": {str(k): int(v) for k, v in
                               observations["qc_flag"].value_counts(dropna=False).items()},
        "value_qc_status_counts": {
            name: {str(k): int(v) for k, v in
                   observations[f"{name}_qc_status"].value_counts(dropna=False).items()}
            for name in PROXY_NAMES},
        "proxy_non_null_values": {name: int(observations[name].notna().sum()) for name in PROXY_NAMES},
        "resolution_counts": {str(k): int(v) for k, v in
                              observations["time_resolution"].value_counts(dropna=False).items()},
        "held_review_items": int(len(held)),
    }
    source_report = run_dir / "quality_reports" / "group_summary.json"
    if source_report.exists():
        report["source_group_summary"] = json.loads(source_report.read_text(encoding="utf-8"))
    return report


def build_release(run_dir, out_dir, version: str = "1.0.0", include_grid: bool = True,
                  baca_inventory=None, assign_unlabelled: bool = True):
    run_dir, out_dir = Path(run_dir), Path(out_dir)
    combined_path = run_dir / "consolidated" / "all_data_combined.csv"
    if not combined_path.exists():
        raise FileNotFoundError(f"批次缺少汇总表: {combined_path}")
    logger.info("读取批次 %s", run_dir.name)
    frame = pd.read_csv(combined_path, low_memory=False)
    if frame.empty:
        raise ValueError("提取批次没有可发布的观测")
    if assign_unlabelled:
        _ensure_record_ids(frame)

    reprint_of, reprint_pairs = detect_reprints(frame)
    codes = assign_record_codes(frame)
    observations = build_observations(frame, codes, reprint_of)
    records = build_record_metadata(frame, codes, reprint_of)
    provenance = build_value_provenance(frame)

    queue_path = run_dir / "review_queue.csv"
    queue = pd.read_csv(queue_path, low_memory=False) if queue_path.exists() else pd.DataFrame()
    held = (queue[queue["action"].astype(str).str.startswith("暂缓")]
            if not queue.empty and "action" in queue else pd.DataFrame())
    qc_report = build_qc_report(observations, records, held, run_dir)

    for layer in ("01_records", "02_compilation", "03_derived/by_resolution",
                  "03_derived/by_proxy_group", "03_derived/gridded", "04_provenance_qc"):
        (out_dir / layer).mkdir(parents=True, exist_ok=True)

    # 层2：分析就绪主表
    observations.to_csv(out_dir / "02_compilation" / "observations.csv", index=False, encoding="utf-8")
    records.to_csv(out_dir / "02_compilation" / "records_metadata.csv", index=False, encoding="utf-8")

    # 层1：每条记录一个 LiPD
    record_meta = records.set_index("record_id")
    n_tables = 0
    for record_id, sub in frame.groupby("source_record_id", sort=True):
        meta = record_meta.loc[record_id].to_dict()
        meta["source_record_id"] = record_id
        meta["record_name"] = meta.get("site_name")
        meta["source_files"] = str(meta.get("source_files", "")).split(";")
        reprint_code = codes.get(reprint_of.get(record_id))
        n_tables += write_record_lpd(out_dir / "01_records" / f"{codes[record_id]}.lpd",
                                     codes[record_id], sub, meta, reprint_code)
    logger.info("LiPD：%d 条记录 / %d 张测量表", len(codes), n_tables)

    # 层3：派生视图
    for resolution, sub in observations.groupby("time_resolution", sort=True):
        sub.to_csv(out_dir / "03_derived" / "by_resolution" / f"{resolution}.csv",
                   index=False, encoding="utf-8")
    presence = observations[PROXY_NAMES].notna()
    groups = presence.apply(lambda row: proxy_group_of(p for p in PROXY_NAMES if row[p]), axis=1)
    for group, sub in observations.groupby(groups, sort=True):
        sub.to_csv(out_dir / "03_derived" / "by_proxy_group" / f"{group}.csv",
                   index=False, encoding="utf-8")
    grid_source = run_dir / "paleoclimate_grid.nc"
    if include_grid and grid_source.exists():
        shutil.copy2(grid_source, out_dir / "03_derived" / "gridded" / "paleoclimate_grid.nc")

    # 层4：溯源与质控审计
    provenance.to_csv(out_dir / "04_provenance_qc" / "value_provenance.csv", index=False, encoding="utf-8")
    manifest = run_dir / "input_manifest.csv"
    if manifest.exists():
        shutil.copy2(manifest, out_dir / "04_provenance_qc" / "source_manifest.csv")
    if not queue.empty:
        queue.to_csv(out_dir / "04_provenance_qc" / "review_decisions.csv", index=False, encoding="utf-8")
    if not held.empty:
        held.to_csv(out_dir / "04_provenance_qc" / "held_values.csv", index=False, encoding="utf-8")
    reprint_pairs.to_csv(out_dir / "04_provenance_qc" / "reprint_pairs.csv", index=False, encoding="utf-8")
    if baca_inventory and Path(baca_inventory).exists():
        shutil.copy2(baca_inventory, out_dir / "04_provenance_qc" / "baca_high_value_inventory.csv")
        logger.info("纳入 Ba/Ca 高值单位核验表: %s", Path(baca_inventory).name)
    (out_dir / "04_provenance_qc" / "qc_report.json").write_text(
        json.dumps(qc_report, ensure_ascii=False, indent=1), encoding="utf-8")

    # 统计指纹 + 文档
    summary = {
        "dataset": "GeoDataExtractor Coral Proxy Compilation",
        "version": version,
        "source_batch": run_dir.name,
        "source_csv_sha256": _sha256(combined_path),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "accounting": qc_report,
        "sha256": {
            "observations.csv": _sha256(out_dir / "02_compilation" / "observations.csv"),
            "records_metadata.csv": _sha256(out_dir / "02_compilation" / "records_metadata.csv"),
        },
    }
    (out_dir / "02_compilation" / "compilation_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    docs.write_docs(out_dir, version, summary, records)
    logger.info("发布包完成: %s", out_dir)
    return summary
