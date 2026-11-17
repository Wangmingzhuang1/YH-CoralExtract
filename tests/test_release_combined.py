"""合并发布包测试：crosswalk 去重口径、LiPD 解析对齐、端到端行数闭合。"""
import io
import json
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import pandas as pd

from app.release.combined import (NO_CANDIDATE, build_combined, parse_entity,
                                  unique_entities)
from app.release.packager import build_release


def _mini_frame(n=12):
    years = np.arange(1950, 1950 + n)
    rows = []
    for i, year in enumerate(years):
        rows.append({
            "observation_id": f"obs_{i}", "source_record_id": "recYH",
            "geo_siteName": "Alpha Reef", "geo_latitude": -10.0, "geo_longitude": 150.0,
            "year_CE": float(year), "time_resolution": "annual",
            "native_interval_years": 1.0, "qc_flag": 0,
            "pub1_doi": "10.1/aaa", "pub1_authors": "Author, A.",
            "source_file": "recYH.csv", "sheet_name": "s1", "source_row": i + 2,
            "δ18O (‰ VPDB)": -4.0 + 0.01 * i, "δ18O_value_qc_status": "pass",
            "δ18O_value_original_value": -4.0 + 0.01 * i,
            "δ18O_value_original_unit": "per mil PDB", "δ18O_value_unit_status": "verified",
            "δ18O_value_unit_evidence": "[]",
        })
    return pd.DataFrame(rows)


def _make_lpd(entity, tables):
    """tables: [(filename, [(variableName, units, resolution)], rows)]，rows 为年+值。"""
    measurement, payload = [], {}
    for number, (filename, columns, rows) in enumerate(tables, start=1):
        spec = [{"variableName": name, "units": units, "number": i + 1,
                 "hasResolution": {"nominal": res}, "coreName": "CORE-1"}
                for i, (name, units, res) in enumerate(columns)]
        measurement.append({"tableName": f"paleo1measurement{number}",
                            "filename": filename, "columns": spec})
        lines = [",".join(f'"{v}"' for v in row) for row in rows]
        payload[f"bag/data/{filename}"] = ("\n".join(lines) + "\n").encode()
    meta = {"dataSetName": entity, "datasetVersion": "1.0.2",
            "geo": {"siteName": "Beta Reef",
                    "geometry": {"type": "Point", "coordinates": [151.0, -11.0, "NA"]}},
            "pub": [{"doi": "10.1/bbb", "author": "Author, B."}],
            "originalDataUrl": "https://www.ncdc.noaa.gov/paleo/study/1",
            "paleoData": [{"measurementTable": measurement}]}
    payload["bag/data/metadata.jsonld"] = json.dumps(meta).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, data in payload.items():
            zf.writestr(name, data)
    return buffer.getvalue()


def _crosswalk(rows):
    return pd.DataFrame(rows, columns=["entity", "proxy", "status", "local_series_id"])


class UniqueEntitiesTest(unittest.TestCase):
    def test_entity_level_coverage(self):
        cw = _crosswalk([
            # 同一 entity：一条有对应、一条无候选 -> 整体视为已覆盖
            {"entity": "COVERED", "proxy": "δ18O", "status": "双方一致", "local_series_id": "abc"},
            {"entity": "COVERED", "proxy": "Sr/Ca", "status": NO_CANDIDATE, "local_series_id": ""},
            {"entity": "UNIQUE", "proxy": "δ18O", "status": NO_CANDIDATE, "local_series_id": ""},
            # 无候选但有 local_series_id 也算覆盖
            {"entity": "LINKED", "proxy": "δ18O", "status": NO_CANDIDATE, "local_series_id": "def"},
        ])
        self.assertEqual(unique_entities(cw), ["UNIQUE"])


class ParseEntityTest(unittest.TestCase):
    def test_multi_table_merge_and_exclusion(self):
        years = [1900.0 + i for i in range(5)]
        lpd = _make_lpd("XX90TST01", [
            ("t1.csv", [("d18O", "permil", "annual"), ("year", "AD", None)],
             [[-4.0 + 0.1 * i, y] for i, y in enumerate(years)]),
            ("t2.csv", [("SrCa", "mmol/mol", "annual"), ("year", "AD", None)],
             [[9.0 + 0.01 * i, y] for i, y in enumerate(years)]),
            ("t3.csv", [("d18O_sw", "permil", None), ("year", "AD", None)],
             [[0.5, y] for y in years]),
        ])
        parsed = parse_entity(lpd, "XX90TST01", "base.zip!XX90TST01.lpd")
        obs = parsed["observations"]
        self.assertEqual(len(obs), 5)                      # 按 year 合并为一行多代理
        self.assertEqual(obs["d18O"].notna().sum(), 5)
        self.assertEqual(obs["sr_ca"].notna().sum(), 5)
        self.assertEqual(parsed["record"]["proxies"], "d18O;sr_ca")
        self.assertEqual([e["variableName"] for e in parsed["excluded"]], ["d18O_sw"])
        self.assertEqual(len(parsed["provenance"]), 10)
        self.assertEqual(parsed["provenance"]["source_row"].iloc[0], 1)
        self.assertEqual(obs["latitude"].iloc[0], -11.0)

    def test_centered_srca_flagged(self):
        years = [1900.0 + i for i in range(5)]
        lpd = _make_lpd("XX90TST02", [
            ("t1.csv", [("SrCa", "mmol/mol", "annual"), ("year", "AD", None)],
             [[-0.02 + 0.01 * i, y] for i, y in enumerate(years)]),
        ])
        parsed = parse_entity(lpd, "XX90TST02", "base.zip!XX90TST02.lpd")
        self.assertEqual(parsed["centered"], ["sr_ca"])
        self.assertTrue(parsed["observations"]["sr_ca_qc_status"].eq("suspect_centered_series").all())


class CombinedEndToEndTest(unittest.TestCase):
    def test_union_dedup_and_accounting(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / "run"
            (run / "consolidated").mkdir(parents=True)
            frame = _mini_frame()
            frame.to_csv(run / "consolidated" / "all_data_combined.csv", index=False)
            base = root / "base"
            build_release(run, base, version="9.9.9", include_grid=False)

            covered = _make_lpd("COVERED01", [
                ("t.csv", [("d18O", "permil", "annual"), ("year", "AD", None)],
                 [[-4.5, 1960.0], [-4.4, 1961.0]])])
            unique = _make_lpd("UNIQUE01", [
                ("t.csv", [("d18O", "permil", "monthly"), ("year", "AD", None)],
                 [[-4.0 + 0.1 * i, 1960.0 + i / 12] for i in range(6)])])
            baseline = root / "baseline.zip"
            with zipfile.ZipFile(baseline, "w") as zf:
                zf.writestr("COVERED01.lpd", covered)
                zf.writestr("UNIQUE01.lpd", unique)
            crosswalk = root / "crosswalk.csv"
            _crosswalk([
                {"entity": "COVERED01", "proxy": "δ18O", "status": "双方一致", "local_series_id": "x"},
                {"entity": "UNIQUE01", "proxy": "δ18O", "status": NO_CANDIDATE, "local_series_id": ""},
            ]).to_csv(crosswalk, index=False)

            out = root / "combined"
            summary = build_combined(base, baseline, crosswalk, out, version="9.9.9_combined")

            obs = pd.read_csv(out / "02_compilation" / "observations.csv")
            rec = pd.read_csv(out / "02_compilation" / "records_metadata.csv")
            base_obs = pd.read_csv(base / "02_compilation" / "observations.csv")
            self.assertEqual(list(obs.columns), list(base_obs.columns))   # schema 对齐
            self.assertEqual(len(obs), len(frame) + 6)
            self.assertEqual(len(rec), 2)
            self.assertEqual(set(rec["source"]), {"YH-CoralGeo", "CoralHydro2k"})
            added = rec[rec["source"].eq("CoralHydro2k")].iloc[0]
            self.assertEqual(added["record_id"], "UNIQUE01")
            self.assertEqual(added["source_version"], "1.0.2")
            # 原始 LiPD 字节保留
            self.assertEqual((out / "01_records" / "UNIQUE01.lpd").read_bytes(), unique)
            self.assertFalse((out / "01_records" / "COVERED01.lpd").exists())
            self.assertEqual(len(list((out / "01_records").glob("*.lpd"))), 2)
            # 溯源与账本
            prov = pd.read_csv(out / "04_provenance_qc" / "value_provenance.csv")
            self.assertEqual(len(prov), len(frame) + 6)
            ledger = pd.read_csv(out / "04_provenance_qc" / "coralhydro2k_merge_ledger.csv")
            self.assertEqual(list(ledger["entity"]), ["UNIQUE01"])
            # 统计闭合
            self.assertEqual(summary["accounting"]["combined"]["added_rows"], 6)
            self.assertEqual(summary["accounting"]["observation_rows"], len(obs))
            derived = pd.read_csv(out / "03_derived" / "by_resolution" / "monthly.csv")
            self.assertEqual(len(derived), 6)
            self.assertTrue((out / "README.md").read_text(encoding="utf-8").find("UNIQUE01") > 0)


if __name__ == "__main__":
    unittest.main()