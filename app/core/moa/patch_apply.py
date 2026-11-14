"""Replay changed sources against a published batch and publish coherent CSV views."""

import csv
import hashlib
import json
import logging
import os
import tempfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from app.core.moa.unit_converter import canonical_unit, finite_number

logger = logging.getLogger(__name__)
COMBINED = "consolidated/all_data_combined.csv"
TIME_CONFLICT = "同一观测的绝对年代列不一致"
UNIT_REASONS = ("没有已验证的单位或标尺转换规则", "源单位或同位素参考标尺缺失")
OBSERVATIONS = ("Sr/Ca (mmol/mol)", "Ba/Ca (μmol/mol)", "δ18O (‰ VPDB)",
                "δ13C (‰)", "Δδ18O (‰ VPDB)", "SST (°C)")
TIME_TARGETS = {"year", "age_bp", "age_ka_bp", "age_bce"}
COORDINATES = ("geo_latitude", "geo_longitude", "geo_rep_latitude", "geo_rep_longitude")


def _path(value):
    return os.path.normcase(os.path.abspath(value)) if value else ""


def _rows(path):
    if path.is_file():
        with path.open(encoding="utf-8-sig", newline="") as stream:
            yield from csv.DictReader(stream)


def _headers(path):
    if path.is_file():
        with path.open(encoding="utf-8-sig", newline="") as stream:
            return next(csv.reader(stream), [])
    return []


def affected_files(run_dir, extra_files=None, decisions=None):
    """Find files that can benefit from the precision or explicit PDB rules or a decision."""
    decisions = decisions or {}
    files = {
        key[1] for key, decision in decisions.items()
        if isinstance(key, tuple) and decision["evidence_status"] != "provisional"
    }
    for row in _rows(Path(run_dir) / "review_queue.csv"):
        reason, unit = row.get("reason", ""), row.get("source_unit", "")
        decision = decisions.get(row.get("review_id"))
        if decision and decision["evidence_status"] != "provisional":
            files.add(_path(row.get("file")))
        elif not decisions and not extra_files and (reason.startswith(TIME_CONFLICT)
                or (reason.startswith(UNIT_REASONS) and canonical_unit(unit) in {"‰ VPDB", "nmol/mol"})):
            files.add(_path(row.get("file")))
    files.update(_path(file) for file in extra_files or ())
    files.discard("")
    return sorted(files)


def _observation_source(row):
    return _path(row.get("source_attachment") or row.get("source_file"))


def authorized_metadata_changes(decisions):
    changes = defaultdict(set)
    for decision in decisions.values():
        if decision["evidence_status"] != "verified":
            continue
        source = _path(decision.get("source_file"))
        if not source:
            continue
        if (decision["action"] == "select_time_column"
                or (decision["action"] == "confirm_mapping" and decision["target"] in TIME_TARGETS)):
            changes[source].add("time")
        elif decision["action"] == "confirm_coordinates":
            changes[source].add("coordinates")
    return changes


def preserved_metadata(row, year_key, allowed_changes):
    allowed = allowed_changes.get(_observation_source(row), ())
    year = None if "time" in allowed else finite_number(row.get(year_key))
    coordinates = tuple(None if "coordinates" in allowed else finite_number(row.get(field)) for field in COORDINATES)
    return year, *coordinates


def _safe_replacements(old, delta, inventory, decisions=None):
    """Keep the previous source when replay fails or would discard a published value."""
    allowed = authorized_metadata_changes(decisions or {})

    def signature(row):
        values = tuple(finite_number(row.get(field)) for field in OBSERVATIONS)
        # 用稳定源字段身份核对旧记录，不依赖 observation_id 公式——观测 ID 含处理版本
        # 后仍可增量回灌；source_processing 区分同岩芯 raw/interpolated 版本。
        identity = (_path(row.get("source_file")), row.get("sheet_name"), row.get("source_row"),
                    row.get("source_record_id"), row.get("sample_id"), row.get("source_processing"))
        return identity, values + preserved_metadata(row, "year_CE", allowed)

    return safe_replacement_sources(_rows(old / COMBINED), _rows(delta / COMBINED), inventory, signature)


def safe_replacement_sources(before, after, inventory, signature):
    sources = {_path(row["file"]) for row in inventory if not row["sheet"]}
    failed = {_path(row["file"]) for row in inventory if row["status"] == "failed"}
    replacements = sources - failed
    new_values = defaultdict(dict)
    for row in after:
        source = _observation_source(row)
        if source not in replacements:
            continue
        identity, values = signature(row)
        if identity in new_values[source]:
            failed.add(source)
        new_values[source][identity] = values
    rejected = Counter()
    for row in before:
        source = _observation_source(row)
        if source not in replacements:
            continue
        identity, previous = signature(row)
        values = new_values[source].get(identity)
        if values is None or any(old is not None and old != new for old, new in zip(previous, values)):
            rejected[source] += 1
    return replacements - failed - rejected.keys(), sorted(failed | rejected.keys())


def _write_rows(destination, fields, rows, data_id_width=None):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        count = 0
        for count, row in enumerate(rows, 1):
            if data_id_width:
                prefix = row["data_id"].rsplit("_", 1)[0]
                row["data_id"] = f"{prefix}_{count:0{data_id_width}d}"
            writer.writerow(row)
    return count


def _merge_csv(old, delta, destination, select, replacements, data_id_width=None):
    fields = list(dict.fromkeys([*_headers(old), *_headers(delta)]))
    if not fields:
        return 0

    def retained_rows():
        yield from (row for row in _rows(old) if select(row) not in replacements)
        yield from (row for row in _rows(delta) if select(row) in replacements)

    return _write_rows(destination, fields, retained_rows(), data_id_width)


def _relative_table(root, name):
    path = root / name
    path.resolve().relative_to(root.resolve())
    return path


def _merge_batch(old, delta, staged, replacements):
    old_index, new_index = old / "INDEX.csv", delta / "INDEX.csv"
    index = {row["path"]: row for path in (old_index, new_index) for row in _rows(path)}
    if COMBINED not in index:
        raise ValueError("原批次缺少汇总表索引")
    merged = []
    for name, row in sorted(index.items()):
        target = _relative_table(staged, name)
        count = _merge_csv(_relative_table(old, name), _relative_table(delta, name), target,
                           _observation_source, replacements, 5 if name == COMBINED else 4)
        if count:
            merged.append({**row, "record_count": count})
        elif target.exists():
            target.unlink()
    _write_rows(staged / "INDEX.csv", list(dict.fromkeys([*_headers(old_index), *_headers(new_index)])), merged)
    for name in ("input_manifest.csv", "review_queue.csv"):
        _merge_csv(old / name, delta / name, staged / name, lambda row: _path(row.get("file")), replacements)
    return next(row["record_count"] for row in merged if row["path"] == COMBINED)


def apply_patch(run_dir, review_decisions=None, extra_files=None, use_parallel=False, max_workers=5,
                enable_calibration_check=True, review_summary=None):
    """Reprocess only selected sources. Publish a full tabular batch after validation."""
    from app.core.moa.workflow import MOAWorkflow
    from app.core.moa.full_review import known_review_decisions
    from app.utils.review_queue import is_paper_body_pdf, load_unit_review_decisions, save_review_queue
    from app.review_workbench.paper_review import DECISIONS, carry_review_findings
    from app.review_workbench.store import DECISION_FIELDS, ReviewStore

    run_dir = Path(run_dir).resolve()
    if not (run_dir / COMBINED).is_file() or not (run_dir / "INDEX.csv").is_file():
        raise ValueError("增量更新需要已有的汇总表和 INDEX.csv")
    if not (run_dir / "review_queue.csv").is_file() and not (review_decisions or extra_files):
        raise ValueError("找不到原批次复核队列")
    with tempfile.TemporaryDirectory(prefix=".moa-stage-", dir=run_dir.parent) as temporary:
        delta, staged = (Path(temporary) / name for name in ("delta", "batch"))
        delta.mkdir()
        staged.mkdir()
        requested = (review_decisions if isinstance(review_decisions, dict)
                     else load_unit_review_decisions(review_decisions))
        affected = [file for file in affected_files(run_dir, extra_files, requested)
                    if not is_paper_body_pdf(file)]
        if not affected:
            return str(run_dir)
        targets = set(affected)
        historical = {key: decision for key, decision in known_review_decisions(Path.cwd(), run_dir.parent).items()
                      if (key[1] if isinstance(key, tuple) else decision.get("source_file")) in targets}
        workflow = MOAWorkflow(output_dir=str(delta), max_workers=max_workers,
                               enable_calibration_check=enable_calibration_check,
                               review_decisions={**historical, **requested})
        logger.info("增量更新涉及 %d 个源文件", len(affected))
        points = workflow.process_directory("dataset", use_parallel=use_parallel, noaa_dir="noaa",
                                            pangaea_dir="pangaea", zenodo_dir="zenodo", selected_files=affected)
        if points:
            workflow.data_output.write_observation_tables(str(delta), points)
        inventory = workflow.source_inventory
        if inventory:
            workflow.data_output._output_csv(inventory, str(delta / "input_manifest.csv"))
        save_review_queue(str(delta), workflow.pending_reviews())
        accepted = {key: decision for key, decision in {**historical, **requested}.items()
                    if key in workflow.applied_unit_reviews}
        replacements, rejected = _safe_replacements(run_dir, delta, inventory, accepted)
        # 文件级排除裁决：重处理确认整文件排除后，授权移除其原有观测行
        file_excludes = {key[1] for key, decision in {**historical, **requested}.items()
                         if isinstance(key, tuple) and key[0] == "FILE_EXCLUDE"
                         and decision["evidence_status"] == "verified"}
        excluded_sources = {_path(row["file"]) for row in inventory
                            if not row["sheet"] and row.get("status") == "excluded"}
        authorized_removals = file_excludes & targets & excluded_sources
        replacements = replacements | authorized_removals
        rejected = sorted(set(rejected) - authorized_removals)
        unresolved = set(affected) - replacements
        rejected = sorted(set(rejected) | unresolved)
        if not replacements:
            raise RuntimeError("没有通过一致性校验的受影响源文件")
        count = _merge_batch(run_dir, delta, staged, replacements)
        # 增量批次与正式批次格式一致：从更新后的汇总表同步导出 LiPD/NetCDF。
        export_status = workflow.data_output.export_archive_formats(
            str(staged), str(staged / COMBINED))
        applied = sum(key in accepted and _path(decision.get("source_file")) in replacements
                      for key, decision in requested.items())
        if review_summary is not None:
            workflow._review_items = list(_rows(staged / "review_queue.csv"))
            finalized = workflow.discard_unrecovered_reviews(review_summary)
            save_review_queue(str(staged), workflow._review_items)
            review_counts = {
                "discarded_reviews": finalized, "incomplete_reviews": workflow.incomplete_review_count,
                "retry_pending_categories": workflow.retry_pending_categories,
            }
        else:
            review_counts = {}
        with (run_dir / COMBINED).open("rb") as source:
            parent_digest = hashlib.file_digest(source, "sha256").hexdigest()
        (staged / "incremental_summary.json").write_text(
            json.dumps({"source_batch": run_dir.name, "source_csv_sha256": parent_digest,
                        "tabular_observations": count, "requested_sources": len(affected),
                        "requested_decisions": len(requested), "applied_decisions": applied,
                        "updated_sources": sorted(os.path.relpath(file) for file in replacements),
                        "retained_previous_sources": [os.path.relpath(file) for file in rejected],
                        **review_counts,
                        "exports": ["CSV", *(["LiPD"] if export_status.get("lipd") == "written" else []),
                                    *(["NetCDF"] if export_status.get("netcdf") == "written" else [])],
                        "export_status": export_status}, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        destination = run_dir.parent / datetime.now().strftime("run_%Y%m%d_%H%M%S_%f")
        carry_review_findings(run_dir, staged, destination.name)
        if isinstance(review_decisions, (str, os.PathLike)):
            root = Path.cwd()
            retained = {ReviewStore.decision_key(row, root): row
                        for row in _rows(run_dir / "review_workbench" / DECISIONS)}
            for row in _rows(Path(review_decisions)):
                key = ReviewStore.decision_key(row, root)
                if key in accepted and _path(row["file"]) in replacements:
                    retained[key] = row
            if retained:
                _write_rows(staged / "review_workbench" / DECISIONS, DECISION_FIELDS, retained.values())
        staged.rename(destination)
    logger.info("增量批次已发布: %s，共 %d 条观测，保留 %d 个源文件原有结果",
                destination, count, len(rejected))
    return str(destination)
