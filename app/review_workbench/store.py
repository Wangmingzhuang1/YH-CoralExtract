"""Index the queue by source and keep review notes and pipeline decisions in the run."""

import csv
import hashlib
import json
import os
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

from app.core.moa.unit_converter import BACA_UNIT_REVIEW, UnitConversionModule, finite_number
from app.utils.review_queue import JUDGE_HANDOFF_ACTIONS, is_paper_body_pdf, mapping_review_key
from .sources import read_source, source_path

CATEGORIES = {
    "unit": "单位与标尺", "structure": "表格与列", "time": "年代",
    "location": "坐标", "source": "源文件", "other": "其他",
    "history": "已应用与已排除",
}
UNIT_REASONS = ("没有已验证的单位或标尺转换规则", "源单位或同位素参考标尺缺失",
                "不同来源的单位声明冲突", BACA_UNIT_REVIEW)
TIME_FIELDS = ("year", "age_bp", "age_ka_bp", "age_bce")
OPEN_STATES = {"pending", "partial", "annotated"}
DECISION_FIELDS = (
    "review_id", "file", "sheet", "column", "source_sha256", "decision",
    "review_decision", "confirmed_unit", "selected_column", "coord_lat", "coord_lon",
    "review_note", "evidence_status", "decision_origin", "evidence_file", "evidence_sha256",
    "cluster_id", "llm_model", "llm_confidence", "decided_at", "original_unit", "layout_blocks",
)
ANNOTATION_FIELDS = ("item_key", "file", "sheet", "column", "action", "reason", "note",
                     "suggested_action", "reviewed", "decided_at")
ITEM_FIELDS = ("item_key", "file", "sheet", "column", "action", "reason", "decision",
               "source_row", "source_line", "source_value", "source_unit", "sample_id",
               "source_sha256", "dataset_doi", "confidence", "review_decision")


def _read_csv(path):
    if path.is_file():
        with path.open(encoding="utf-8-sig", newline="") as stream:
            yield from csv.DictReader(stream)


def _save_csv(path, fields, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, encoding="utf-8-sig",
                                         newline="", delete=False) as stream:
            temporary = Path(stream.name)
            writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(records)
        os.replace(temporary, path)
    finally:
        if temporary and temporary.is_file():
            temporary.unlink()


def _json(value, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def _now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _category(item):
    reason, action = item.get("reason", ""), item.get("action", "")
    if (action in {"已应用人工裁决", "已应用证据复核", "已排除非观测片段", "已三轮复核舍弃"}
            or reason.startswith("档案注解标明中心化/缩放/距平")
            or is_paper_body_pdf(item.get("file") or "")):
        return "history"
    if reason.startswith(UNIT_REASONS):
        return "unit"
    if action in {"复核空间范围", "暂缓关联地点", "暂缓未覆盖区域"} or "边界不完整" in reason:
        return "location"
    if action == "暂缓时间列" or any(term in reason for term in ("绝对年代", "主年代列", "年代基准")):
        return "time"
    if action in {"暂缓读取", "暂缓关联正文"}:
        return "source"
    if action in {"暂缓表格", "暂缓该指标", "复核表结构", "复核输入"} or "样品标识" in reason or "映射" in action:
        return "structure"
    return "other"


def _location_number(value):
    number = finite_number(value)
    return str(int(number)) if number is not None and number.is_integer() and number >= 1 else ""


def review_case_id(item, source_root):
    """复核组身份：与 _load_queue 分组键一致的 case_id。同表同列但不同复核事项
    （action/reason/decision/source_unit 不同）属不同组，结论不得串用。"""
    item = {key: value or "" for key, value in item.items() if key}
    for key in (*ITEM_FIELDS, "review_id", "unit_evidence", "geo_bounds"):
        item.setdefault(key, "")
    category = _category(item)
    reasons = item["reason"].split(": ", 1)[0] if category == "time" else item["reason"]
    evidence = item["unit_evidence"] if category == "unit" else ""
    file = os.path.normcase(os.path.abspath(Path(source_root) / item["file"])) if item["file"] else ""
    group = (file, item["sheet"].casefold(), item["column"],
             item["decision"] if category != "time" or not item["source_row"] else "",
             category, item["action"], reasons, item["source_unit"], item["source_sha256"], evidence)
    return hashlib.sha256(json.dumps(group, ensure_ascii=False).encode()).hexdigest()[:20]


class ReviewStore:
    def __init__(self, run_dir, source_root):
        self.run_dir = Path(run_dir).resolve()
        self.source_root = Path(source_root).resolve()
        self.work_dir = self.run_dir / "review_workbench"
        self.decisions_path = self.work_dir / "decisions.csv"
        self.annotations_path = self.work_dir / "annotations.csv"
        self.lock = RLock()
        self._linked_lock = RLock()
        self._linked_index = None
        self._max_report_version = None
        self._max_findings = {}
        self._max_source_findings = {}
        self.by_key = {}
        self.cases = {}
        self._load_queue()
        self.decisions = {self._decision_key(row): row for row in _read_csv(self.decisions_path)}
        self.annotations = {row["item_key"]: row for row in _read_csv(self.annotations_path)}

    def _path_key(self, file):
        return os.path.normcase(os.path.abspath(self.source_root / file)) if file else ""

    def _decision_key(self, row):
        return self.decision_key(row, self.source_root)

    @staticmethod
    def decision_key(row, source_root):
        if row.get("review_id"):
            return row["review_id"]
        path = row.get("file") or ""
        file = os.path.normcase(os.path.abspath(Path(source_root) / path)) if path else ""
        action = row.get("review_decision")
        if action == "confirm_mapping":
            return mapping_review_key(file, row.get("sheet", ""), row.get("column", ""), row.get("source_sha256", ""))
        if action in {"select_column", "select_time_column"}:
            return ("SELECT", file, row.get("decision", "").casefold())
        if action == "confirm_coordinates":
            return ("COORDS", file)
        if action == "confirm_layout":
            return ("LAYOUT", file, row.get("sheet", "").casefold(), row.get("source_sha256", "").casefold())
        if action in {"exclude", "recover"} and file and not row.get("column"):
            return ("FILE_EXCLUDE" if action == "exclude" else "FILE_RECOVER", file)
        return ("COLUMN", file, row.get("column", "").casefold(), row.get("sheet", "").casefold(),
                row.get("source_sha256", "").casefold())

    def _load_queue(self):
        queue = self.run_dir / "review_queue.csv"
        if not queue.is_file():
            raise FileNotFoundError(f"批次缺少 review_queue.csv：{self.run_dir}")
        groups = defaultdict(list)
        from app.core.moa.coverage_audit import pending_handoff_rows
        rows = list(_read_csv(queue))
        rows.extend(pending_handoff_rows(self.run_dir, self.source_root, rows))
        for index, row in enumerate(rows):
            item = {key: value or "" for key, value in row.items() if key}
            for key in (*ITEM_FIELDS, "review_id", "unit_evidence", "geo_bounds"):
                item.setdefault(key, "")
            key = item["review_id"] or f"idx:{index}"
            item["item_key"] = key if key not in self.by_key else f"idx:{index}"
            self.by_key[item["item_key"]] = item
            category = _category(item)
            reasons = item["reason"].split(": ", 1)[0] if category == "time" else item["reason"]
            evidence = item["unit_evidence"] if category == "unit" else ""
            group = (self._path_key(item["file"]), item["sheet"].casefold(), item["column"],
                     item["decision"] if category != "time" or not item["source_row"] else "",
                     category, item["action"], reasons, item["source_unit"], item["source_sha256"], evidence)
            groups[group].append(item)
        for group, members in groups.items():
            item = members[0]
            case_id = review_case_id(item, self.source_root)
            candidates = [part.strip() for part in item["column"].split(" | ") if part.strip()]
            category = group[4]
            action = self._action(item, category, candidates)
            file = Path(item["file"]).name or "未定位源文件"
            search = " ".join((item["file"], item["sheet"], item["column"], item["reason"], item["action"],
                               item["decision"], item["source_unit"], item["dataset_doi"])).casefold()
            self.cases[case_id] = {
                "id": case_id, "members": members, "member_keys": {m["item_key"] for m in members},
                "category": category, "action": action, "candidates": candidates,
                "name": file, "file": item["file"], "sheet": item["sheet"], "column": item["column"],
                "reason": group[6], "target": group[3], "source_unit": item["source_unit"],
                "count": len(members), "search": search,
            }
        self.order = sorted(self.cases.values(), key=lambda c: (-c["count"], c["file"].casefold(), c["id"]))

    def _archive(self, item):
        suffix = Path(item["file"]).suffix.casefold()
        path = self._path_key(item["file"])
        return any(suffix in {".txt", ".tab"} and path.startswith(self._path_key(root) + os.sep)
                   for root in ("noaa", "pangaea"))

    def _action(self, item, category, candidates):
        if category == "history":
            return ""
        if item["action"] in JUDGE_HANDOFF_ACTIONS and item["sheet"] and item["column"]:
            return "confirm_mapping"
        if category == "unit" and item["decision"] in UnitConversionModule.default_units and item["column"]:
            if item["review_id"] or (item["sheet"] and item["source_sha256"]):
                return "confirm_unit"
        if (category == "structure" and item["reason"] == "多个候选列无法确定唯一来源"
                and len(candidates) > 1 and item["decision"] in UnitConversionModule.default_units):
            return "select_column"
        if category == "time" and len(candidates) > 1 and item["reason"] == "存在多个无法区分的主年代列":
            return "select_time_column"
        if (category == "location" and self._archive(item)
                and self._path_key(item["file"]).startswith(self._path_key("noaa") + os.sep)):
            return "confirm_coordinates"
        if self._layout_candidate(item, category):
            return "confirm_layout"
        return ""

    # 表结构裁决可应用的附件类型（data_ingestion._inspect_attachment_table 路径）。
    _LAYOUT_EXTENSIONS = {".xlsx", ".xlsm", ".xls", ".ods", ".docx"}
    _LAYOUT_REASONS = ("无法确定表头与观测区", "未找到可确认的绝对年代列",
                       "没有可确认的受支持观测指标", "模型未给出可在原表核验的结构")

    def _layout_candidate(self, item, category):
        """有目标指标线索、但卡在表头/观测区/年代的工作表/Word 表结构候选。"""
        if category not in {"structure", "source"}:
            return False
        if Path(item["file"]).suffix.casefold() not in self._LAYOUT_EXTENSIONS:
            return False
        if not item["sheet"]:
            return False
        reason = item["reason"]
        return (item["action"] in {"暂缓表格", "复核输入", "复核表结构"}
                or any(text in reason for text in self._LAYOUT_REASONS))

    def _record_for(self, item, action="", target=""):
        if action == "confirm_mapping":
            # 列级 exclude 裁决（证据化排除/舍弃）对映射组同样生效，使其计为已处理
            record = self.decisions.get(self._decision_key(item))
            return record if record and record.get("review_decision") == "exclude" else None
        if action in {"select_column", "select_time_column", "confirm_coordinates", "confirm_layout"}:
            sheet = item["sheet"] or (Path(item["file"]).name if self._archive(item) else "")
            row = {**item, "review_id": "", "review_decision": action, "decision": target}
            decision = self.decisions.get(self._decision_key(row))
            return decision if decision and decision.get("sheet", "").casefold() == sheet.casefold() else None
        return self.decisions.get(self._decision_key(item))

    def _status(self, case):
        if case["category"] == "history":
            return "history", 0
        action, members = case["action"], case["members"]
        if action and action != "confirm_unit":
            item = members[0]
            targets = TIME_FIELDS if action == "select_time_column" and not case["target"] else [case["target"]]
            records = [self._record_for(item, action, target) for target in targets]
            count = case["count"] if any(r and r.get("evidence_status") == "verified" for r in records) else 0
        else:
            count = sum(bool((r := self._record_for(item)) and r.get("evidence_status") != "provisional")
                        for item in members)
        if count == case["count"]:
            return "resolved", count
        if count:
            return "partial", count
        if case["id"] in self.annotations or any(m["item_key"] in self.annotations for m in members):
            return "annotated", 0
        return "pending", 0

    def _summary(self, case, status=None):
        status, completed = status or self._status(case)
        keys = ("id", "category", "action", "name", "file", "sheet", "column", "reason",
                "target", "source_unit", "count")
        return {**{key: case[key] for key in keys}, "status": status, "completed": completed}

    def overview(self):
        with self.lock:
            totals = Counter()
            open_cases = open_records = 0
            paper_cases = paper_records = 0
            self._refresh_max_report()
            for case in self.order:
                status, completed = self._status(case)
                totals["history" if status == "history" else case["category"]] += 1
                if status in OPEN_STATES:
                    open_cases += 1
                    open_records += case["count"] - completed
                    if self._is_paper_handoff(case):
                        paper_cases += 1
                        paper_records += case["count"] - completed
            return {"batch": self.run_dir.name, "records": len(self.by_key), "cases": len(self.order),
                    "open_cases": open_cases, "open_records": open_records,
                    "paper_cases": paper_cases, "paper_records": paper_records,
                    "decisions": len(self.decisions), "annotations": len(self.annotations),
                    "categories": [{"id": key, "label": label, "count": totals[key]}
                                   for key, label in CATEGORIES.items()]}

    def query(self, category="", status="open", search="", limit=24, offset=0):
        tokens = search.casefold().split()
        with self.lock:
            matches = []
            self._refresh_max_report()
            for case in self.order:
                current = self._status(case)
                case_category = "history" if current[0] == "history" else case["category"]
                if (category and case_category != category) or not all(t in case["search"] for t in tokens):
                    continue
                if status in {"open", "paper"}:
                    if current[0] not in OPEN_STATES or (status == "paper" and not self._is_paper_handoff(case)):
                        continue
                elif status != "all" and current[0] != status:
                    continue
                matches.append((case, current))
            return {"total": len(matches), "cases": [self._summary(case, current)
                                                    for case, current in matches[offset:offset + limit]]}

    def member(self, case_id, key=""):
        case = self.cases[case_id]
        if key and key not in case["member_keys"]:
            raise KeyError("该记录不属于当前复核组")
        return case, self.by_key[key] if key else case["members"][0]

    def detail(self, case_id, key="", offset=0, limit=20):
        case, item = self.member(case_id, key)
        action = case["action"]
        targets = TIME_FIELDS if action == "select_time_column" and not case["target"] else [case["target"]]
        with self.lock:
            self._refresh_max_report()
            decision = (next((record for target in targets if (record := self._record_for(item, action, target))), None)
                        if action and action != "confirm_unit" else self._record_for(item))
            records = [{**{field: m[field] for field in ITEM_FIELDS},
                        "saved": bool(self._record_for(m))} for m in case["members"][offset:offset + limit]]
            return {**self._summary(case), "candidates": case["candidates"], "records": records,
                    "item": {field: item[field] for field in ITEM_FIELDS},
                    "unit_evidence": _json(item["unit_evidence"], []),
                    "bounds": _json(item["geo_bounds"], {}),
                    "suggested_lat": item.get("geo_rep_latitude", ""),
                    "suggested_lon": item.get("geo_rep_longitude", ""),
                    "conversion_missing": bool(action == "confirm_unit" and item["source_unit"]
                                               and item["reason"].startswith(UNIT_REASONS[0])),
                    "decision_record": decision, "max_review": self.max_review(case_id),
                    "annotation": self.annotations.get(case_id) or self.annotations.get(item["item_key"])}

    def max_review(self, case_id):
        return self._max_review_for(self.cases[case_id])

    def _max_review_for(self, case):
        self._refresh_max_report()
        return self._matching_max_finding(case)

    def _max_source_key(self, row):
        return (self._path_key(row.get("file", "")), row.get("sheet", "").casefold(),
                row.get("column", "").casefold(), row.get("source_sha256", ""))

    def _refresh_max_report(self):
        report = self.work_dir / "max_review.json"
        try:
            version = report.stat().st_mtime_ns
        except FileNotFoundError:
            self._max_report_version, self._max_findings, self._max_source_findings = None, {}, {}
            return
        if version != self._max_report_version:
            payload = _json(report.read_text(encoding="utf-8"), {})
            self._max_findings = payload.get("findings", {}) if payload.get("batch") == self.run_dir.name else {}
            self._max_findings = {
                key: {**row, "state": "pending", "reason": "旧批次未证实该列属于排除范围，等待原件复核。"}
                if row.get("state") == "excluded" else row
                for key, row in self._max_findings.items()
            }
            groups = defaultdict(list)
            for row in self._max_findings.values():
                if row.get("source_sha256"):
                    groups[self._max_source_key(row)].append(row)
            self._max_source_findings = {key: rows[0] for key, rows in groups.items() if len(rows) == 1}
            self._max_report_version = version

    def _matching_max_finding(self, case):
        if case["category"] == "history":
            return None
        exact = self._max_findings.get(case["id"])
        if exact:
            return exact
        return self._max_source_findings.get(self._max_source_key(case["members"][0]))

    def _is_paper_handoff(self, case):
        finding = self._matching_max_finding(case)
        return bool(finding and finding.get("state") == "manual")

    def save_annotation(self, case_id, key, note):
        case, item = self.member(case_id, key)
        if not note.strip():
            raise ValueError("请填写复核批注")
        row = {field: item.get(field, "") for field in ("file", "sheet", "column", "action", "reason")}
        row.update(item_key=case["id"], note=note.strip(), reviewed="1", decided_at=_now())
        with self.lock:
            updated = {**self.annotations, case["id"]: row}
            _save_csv(self.annotations_path, ANNOTATION_FIELDS, updated.values())
            self.annotations = updated
        return row

    def save_decision(self, body):
        case, item = self.member(body.get("id", ""), body.get("item_key", ""))
        action = body.get("action", "")
        expected = case["action"]
        if not expected or expected == "confirm_mapping" or action not in ({expected, "exclude"} if expected == "confirm_unit" else {expected}):
            raise ValueError("该复核组无法导出此类裁决，可先保存批注")
        note = str(body.get("note") or "").strip()
        if not note or body.get("source_verified") is not True:
            raise ValueError("请填写源证据的位置与依据，并确认已核对")
        path = source_path(self.source_root, item["file"])
        if path is None or not path.is_file():
            raise ValueError("当前源文件不可读")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if item["source_sha256"] and item["source_sha256"].casefold() != digest:
            raise ValueError("源文件版本已变化，请从新提取批次复核")
        sheet = item["sheet"] or (path.name if self._archive(item) else "")
        target = case["target"]
        unit = str(body.get("unit") or "").strip()
        column = str(body.get("selected_column") or "").strip()
        lat, lon = (finite_number(body.get(field)) for field in ("lat", "lon"))
        if action == "confirm_unit" and UnitConversionModule().convert_value(target, 1, unit) is None:
            raise ValueError("当前转换规则不支持该指标的源单位或标尺，请核对单位或保存批注")
        if action in {"select_column", "select_time_column"}:
            if column not in case["candidates"]:
                raise ValueError("请选择源表中的候选列")
            preview = read_source(self.source_root, item["file"], sheet, column=item["column"])
            if column not in {str(value).strip() for value in preview.get("header", [])}:
                raise ValueError("在当前源表中找不到选定列，请核对源文件")
        if action == "select_time_column" and not target:
            target = body.get("target", "")
            if target not in TIME_FIELDS:
                raise ValueError("请选择年代基准")
        if action == "confirm_coordinates" and not (lat is not None and lon is not None
                                                     and -90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("请填写有效的十进制度经纬度")
        if not sheet:
            raise ValueError("当前复核项缺少可绑定的源表位置")
        group = body.get("scope") == "group"
        if group and expected != "confirm_unit":
            raise ValueError("该类裁决按源表保存")
        members = case["members"] if group else [item]
        with self.lock:
            updated = dict(self.decisions)
            saved = 0
            for member in members:
                row = {field: member.get(field, "") for field in ("file", "column", "review_id")}
                row.update(file=str(path), sheet=sheet, source_sha256=digest, decision=target,
                           review_decision=action, confirmed_unit=unit if action == "confirm_unit" else "",
                           selected_column=column if action in {"select_column", "select_time_column"} else "",
                           coord_lat=lat if action == "confirm_coordinates" else "",
                           coord_lon=lon if action == "confirm_coordinates" else "",
                           review_note=note, evidence_status="verified", decided_at=_now())
                if expected != "confirm_unit":
                    row["review_id"] = ""
                key = self._decision_key(row)
                existing = updated.get(key)
                if existing and existing.get("sheet", "").casefold() != sheet.casefold():
                    raise ValueError("该源文件已有同字段的另一表块裁决，请单独核对")
                if group and existing and existing.get("evidence_status") != "provisional":
                    continue
                updated[key] = row
                saved += 1
            if saved:
                _save_csv(self.decisions_path, DECISION_FIELDS, updated.values())
                self.decisions = updated
        return {"saved": saved}

    def linked_rows(self, case_id, key="", limit=6):
        _, item = self.member(case_id, key)
        with self._linked_lock:
            if self._linked_index is None:
                import pandas as pd
                path = self.run_dir / "consolidated" / "all_data_combined.csv"
                self._linked_index = {}
                if path.is_file():
                    columns = {"source_file", "sheet_name", "source_row", "source_line", "sample_id",
                               "year_CE", "qc_flag", "Sr/Ca (mmol/mol)", "δ18O (‰ VPDB)",
                               "δ13C (‰)", "Ba/Ca (μmol/mol)", "SST (°C)"}
                    frame = pd.read_csv(path, usecols=lambda name: name in columns, dtype=str,
                                        encoding="utf-8-sig", keep_default_na=False)
                    file_keys = {name: self._path_key(name) for name in frame["source_file"].unique()}
                    self._linked_index = dict(tuple(frame.groupby(frame["source_file"].map(file_keys), sort=False)))
            group = self._linked_index.get(self._path_key(item["file"]))
        exact = bool(item["source_line"] or item["source_row"])
        if group is None:
            return {"rows": [], "scope": "row" if exact else "file"}
        if item["sheet"] and "sheet_name" in group:
            group = group[group["sheet_name"].str.casefold().eq(item["sheet"].casefold())]
        for field in ("source_line", "source_row"):
            if item[field] and field in group:
                group = group[group[field].map(_location_number).eq(_location_number(item[field]))]
                break
        if item["sample_id"] and "sample_id" in group:
            group = group[group["sample_id"].eq(item["sample_id"])]
        return {"rows": group.head(limit).to_dict("records"), "scope": "row" if exact else "file"}
