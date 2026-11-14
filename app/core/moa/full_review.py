"""Run paper review before the one complete export of an extraction batch."""

import logging
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.config import settings
from app.core.moa.data_output import DataOutputModule
from app.core.moa.database_parser import DatabaseFormat
from app.core.moa.patch_apply import (_observation_source, _path, authorized_metadata_changes,
                                    preserved_metadata, safe_replacement_sources)
from app.core.moa.unit_converter import UnitConversionModule, canonical_unit, finite_number
from app.utils.review_queue import load_unit_review_decisions, save_review_queue
from app.review_workbench.paper_review import DECISIONS, PaperReview

logger = logging.getLogger(__name__)
DEFAULT_MAX_PAPERS = 0


def known_review_decisions(source_root, output_root, explicit=None):
    """Reuse only version-bound decisions, with an explicit table taking precedence."""
    source_root, output_root = Path(source_root), Path(output_root)
    by_key = {}
    conflicts = set()
    files = []
    for run in sorted(output_root.glob("run_*"), reverse=True):
        if (run / "INDEX.csv").is_file():
            files.extend(run / "review_workbench" / name for name in ("decisions.csv", DECISIONS))

    def signature(row):
        return (row["action"], canonical_unit(row["unit"]), row["selected"],
                row.get("target", "") if row["action"] == "confirm_mapping" else "",
                row.get("blocks"),
                finite_number(row["lat"]), finite_number(row["lon"]),
                row["sheet"].casefold(), row["source_sha256"])

    for file in files:
        if not file.is_file():
            continue
        try:
            rows = load_unit_review_decisions(file, reuse_verified=True, source_root=source_root)
        except (OSError, ValueError):
            logger.warning("跳过不可读取的历史裁决表：%s", file, exc_info=True)
            continue
        for key, row in rows.items():
            previous = by_key.get(key)
            if previous is not None and signature(previous) != signature(row):
                conflicts.add(key)
            by_key.setdefault(key, row)
    for key in conflicts:
        by_key.pop(key, None)
    if conflicts:
        logger.warning("历史裁决有 %d 组不一致，保留待核", len(conflicts))

    curated = source_root / "paper" / "structural_adjudication_sheet.csv"
    if curated.is_file():
        try:
            by_key.update(load_unit_review_decisions(curated, reuse_verified=True, source_root=source_root))
        except (OSError, ValueError):
            logger.warning("项目裁决表无法读取：%s", curated, exc_info=True)
    by_key.update(load_unit_review_decisions(explicit, source_root=source_root))
    return by_key


def _signature(record, allowed_changes=None):
    values = tuple(finite_number(record.get(field)) for field in UnitConversionModule.default_units)
    return DataOutputModule._observation_id(record), values + preserved_metadata(record, "year", allowed_changes or {})


def _replay(workflow, review, summary, directory, context, sources, use_parallel):
    path = review.store.work_dir / DECISIONS
    accepted = load_unit_review_decisions(path)
    accepted = {key: decision for key, decision in accepted.items() if key not in workflow.unit_review_decisions}
    targets = {_path(row["file"]) for key, row in review.decisions.items() if key in accepted}
    if not review.decisions:
        return [], summary
    if targets:
        from app.core.moa.workflow import MOAWorkflow

        replay = MOAWorkflow(output_dir=workflow.data_output.output_dir,
                             enable_calibration_check=workflow.enable_calibration_check,
                             max_workers=workflow.max_workers,
                             review_decisions={**workflow.unit_review_decisions, **accepted})
        replay.process_directory(directory, context, use_parallel=use_parallel, selected_files=targets, **sources)
        metadata_changes = authorized_metadata_changes(
            {key: decision for key, decision in accepted.items() if key in replay.applied_unit_reviews}
        )
        approved, rejected = safe_replacement_sources(workflow.processed_data, replay.processed_data,
                                                      replay.source_inventory,
                                                      lambda record: _signature(record, metadata_changes))
        workbook_dirs = {Path(file).parent for file in targets if Path(file).suffix.casefold() not in {".txt", ".tab"}
                         or workflow.database_parser.detect_format(file) == DatabaseFormat.UNKNOWN}
        unsafe_dirs = {Path(file).parent for file in set(rejected) | (targets - approved)} & workbook_dirs
        approved = {file for file in approved if Path(file).parent not in unsafe_dirs}
        used = {_path(row["file"]) for key, row in review.decisions.items()
                if key in accepted and key in replay.applied_unit_reviews and _path(row["file"]) in approved}
        replayed_dirs = {Path(file).parent for file in used} & workbook_dirs
        approved = {file for file in approved if file in used or Path(file).parent in replayed_dirs}
    else:
        replay, approved, rejected = None, set(), []
    selected = [
        (key, row) for key, row in review.decisions.items()
        if replay is not None and key in accepted and key in replay.applied_unit_reviews and _path(row["file"]) in approved
    ]
    cases = Counter(row["cluster_id"] for _, row in selected)
    proposed = Counter(row["cluster_id"] for row in review.decisions.values())
    for case_id, count in proposed.items():
        finding = summary["findings"][case_id]
        applied = cases[case_id]
        finding["applied_decision_rows"] = applied
        if applied == count:
            finding["state"] = "applied"
        else:
            finding["state"] = "pending"
            finding["reason"] = f"{applied}/{count} 条裁决完成安全回灌。其余保留首轮记录，等待定向重试。{finding['reason']}"

    if approved:
        workflow.processed_data = [row for row in workflow.processed_data if _observation_source(row) not in approved]
        workflow.processed_data.extend(row for row in replay.processed_data if _observation_source(row) in approved)
        for attr in ("source_inventory", "_review_items"):
            kept = [row for row in getattr(workflow, attr) if _path(row["file"]) not in approved]
            kept.extend(row for row in getattr(replay, attr) if _path(row["file"]) in approved)
            setattr(workflow, attr, kept)
    for key, row in selected:
        workflow.unit_review_decisions[key] = accepted[key]
        workflow.applied_unit_reviews.add(key)
    summary["decision_rows"] = len(selected)
    summary["cases"] = dict(Counter(row["state"] for row in summary["findings"].values()))
    summary["replayed_sources"] = len(approved)
    summary["retained_first_pass_sources"] = sorted(set(rejected) | (targets - approved))
    return [row for _, row in selected], summary


def review_before_export(workflow, directory, context, sources, *, source_root, use_parallel=False,
                         max_papers=DEFAULT_MAX_PAPERS, rules_only=False, auto_discard=False):
    """Keep first-pass output available even when the optional model stage fails.

    auto_discard=True 时启用全自动模式：第三轮扩 scope 尽力挽回所有暂缓类（结构/年代/
    映射/单位/坐标），挽回后仍无证据者证据化舍弃，人工复核归零。
    """
    pending = workflow.pending_reviews()
    reused = len(workflow.applied_unit_reviews)
    if not pending:
        return {"status": "no_queue", "reused_decisions": reused, "cases": {}, "findings": {}, "decision_rows": 0}, []
    try:
        with TemporaryDirectory(prefix=".moa-review-", dir=workflow.data_output.output_dir) as staging:
            save_review_queue(staging, pending)
            review = PaperReview(staging, source_root, rules_only=rules_only or not settings.LLM_API_KEY)
            summary = review.run(max_papers=max_papers, all_rules=True,
                                 workers=workflow.max_workers if use_parallel else 1,
                                 jev_recheck_only=not auto_discard)
            decisions, summary = _replay(workflow, review, summary, directory, context, sources, use_parallel)
            if auto_discard:
                summary["discarded_reviews"] = workflow.discard_unrecovered_reviews(summary)
                incomplete = getattr(workflow, "incomplete_review_count", 0)
                summary["incomplete_reviews"] = incomplete
                # 兼容旧报告中的计数，实际人工交接另用 human_handoff_remaining。
                summary["manual_remaining"] = incomplete
                summary["human_handoff_remaining"] = 0
                summary["retry_pending_categories"] = dict(getattr(workflow, "retry_pending_categories", {}))
                status = "completed" if not incomplete else "completed_with_retry_pending"
                scope = "full_auto_recover_then_discard"
            else:
                status = "partial" if any(summary["cases"].get(state) for state in ("pending", "manual")) else "completed"
                scope = "jev_failed_recheck_and_study_units"
            return {**summary, "status": status, "reused_decisions": reused,
                    "model_review_scope": scope}, decisions
    except (OSError, ValueError, RuntimeError):
        logger.exception("论文级复核未完成，保留首轮提取和待复核记录")
        return {"status": "failed", "reused_decisions": reused, "cases": {}, "findings": {}, "decision_rows": 0}, []
