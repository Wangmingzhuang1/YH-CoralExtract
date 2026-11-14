"""发布打包器测试：schema 映射、转载归并、LiPD 合规、端到端行数闭合。"""
import hashlib
import json
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import pandas as pd

from app.release.lipd import assign_record_codes, write_record_lpd
from app.release.packager import build_observations, build_release
from app.release.reprints import detect_reprints
from app.release.schema import PROXIES, proxy_group_of, slugify_site


def _synthetic_frame(n_years=30):
    """两条转载记录（同站点同序列）+ 一条独立记录。"""
    rows = []
    years = np.arange(1900, 1900 + n_years)
    base = np.sin(years / 3.0)
    for record_id, site, values, doi in (
            ("recA", "Alpha Reef", base, "10.1/aaa"),
            ("recB", "Alpha Reef", base + 1e-12, "10.1/bbb"),   # 重复版本
            ("recC", "Beta Reef", np.cos(years / 3.0), "10.1/ccc")):
        for i, year in enumerate(years):
            rows.append({
                "observation_id": f"{record_id}_{i}", "source_record_id": record_id,
                "geo_siteName": site, "geo_latitude": -10.0, "geo_longitude": 150.0,
                "year_CE": float(year), "time_resolution": "annual",
                "native_interval_years": 1.0, "qc_flag": 0,
                "pub1_doi": doi, "pub1_authors": f"Author, A. {1990 + ord(record_id[-1]) - 65}",
                "dataset_doi": None, "dataset_investigators": None,
                "paleoData_dating_method": "U/Th", "paleoData_dating_error": None,
                "testing_method": "IRMS", "source_file": f"{record_id}.csv",
                "sheet_name": "s1", "source_row": i + 2, "source_page": None,
                "δ18O (‰ VPDB)": float(values[i]),
                "δ18O_value_qc_status": "pass",
                "δ18O_value_original_value": float(values[i]),
                "δ18O_value_original_unit": "per mil PDB",
                "δ18O_value_unit_status": "verified",
                "δ18O_value_unit_evidence": "[]",
                "Sr/Ca (mmol/mol)": np.nan, "Sr/Ca_value_qc_status": "missing_or_invalid_value",
            })
    return pd.DataFrame(rows)


class SchemaTest(unittest.TestCase):
    def test_slugify_and_groups(self):
        self.assertEqual(slugify_site("Houbihu, southern Taiwan"), "HOUBIHUSOUTH")
        self.assertEqual(proxy_group_of(["sr_ca", "d18O"]), "G2_SrCa_O18")
        self.assertEqual(proxy_group_of(["d18O", "sst"]), "OTHER")

    def test_observations_ascii_and_values(self):
        frame = _synthetic_frame()
        codes = assign_record_codes(frame)
        obs = build_observations(frame, codes, {})
        self.assertTrue(all(c.isascii() for c in obs.columns))
        self.assertIn("d18O", obs.columns)
        self.assertEqual(obs["d18O"].notna().sum(), len(frame))
        self.assertEqual(len(obs), len(frame))


class ReprintTest(unittest.TestCase):
    def test_duplicate_and_canonical(self):
        frame = _synthetic_frame()
        reprint_of, pairs = detect_reprints(frame)
        self.assertEqual(len(reprint_of), 1)
        canonical = next(iter(reprint_of.values()))
        self.assertIn(canonical, ("recA", "recB"))
        self.assertTrue((pairs["method"] == "duplicate_version").any())
        # 独立记录不被误并
        self.assertNotIn("recC", reprint_of)
        self.assertNotIn("recC", set(pairs["record_a"]) | set(pairs["record_b"]))

    def test_short_overlap_not_reprint(self):
        frame = _synthetic_frame(n_years=10)   # < 20 共同年
        reprint_of, pairs = detect_reprints(frame)
        self.assertEqual(reprint_of, {})
        self.assertTrue(pairs.empty)


class LipdTest(unittest.TestCase):
    def test_bagit_structure_and_checksums(self):
        frame = _synthetic_frame()
        codes = assign_record_codes(frame)
        sub = frame[frame["source_record_id"] == "recA"]
        meta = {"site_name": "Alpha Reef", "latitude": -10.0, "longitude": 150.0,
                "pub_authors": "Author, A. 1990", "pub_doi": "10.1/aaa",
                "source_record_id": "recA", "source_files": ["recA.csv"]}
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "ALPHAREEF01.lpd"
            n_tables = write_record_lpd(path, "ALPHAREEF01", sub, meta)
            self.assertEqual(n_tables, 1)
            with zipfile.ZipFile(path) as zf:
                names = set(zf.namelist())
                for required in ("bag/bagit.txt", "bag/bag-info.txt",
                                 "bag/manifest-md5.txt", "bag/tagmanifest-md5.txt",
                                 "bag/data/metadata.jsonld"):
                    self.assertIn(required, names)
                manifest = zf.read("bag/manifest-md5.txt").decode()
                for line in manifest.strip().splitlines():
                    md5, name = line.split("\t")
                    self.assertEqual(hashlib.md5(zf.read(f"bag/{name}")).hexdigest(), md5)
                jsonld = json.loads(zf.read("bag/data/metadata.jsonld"))
                self.assertEqual(jsonld["archiveType"], "coral")
                self.assertEqual(jsonld["geo"]["geometry"]["coordinates"], [150.0, -10.0])
                columns = jsonld["paleoData"][0]["measurementTable"][0]["columns"]
                self.assertTrue(columns[0]["primaryAgeColumn"])
                csv_name = jsonld["paleoData"][0]["measurementTable"][0]["filename"]
                csv_text = zf.read(f"bag/data/{csv_name}").decode()
                self.assertEqual(len(csv_text.strip().splitlines()), len(sub))


class EndToEndTest(unittest.TestCase):
    def test_unlabelled_observations_are_included_in_the_archive(self):
        frame = _synthetic_frame().iloc[:2].copy()
        frame["source_record_id"] = None
        frame["sheet_name"] = ["First", "Second"]
        with TemporaryDirectory() as tmp:
            run = Path(tmp) / "run"
            (run / "consolidated").mkdir(parents=True)
            frame.to_csv(run / "consolidated" / "all_data_combined.csv", index=False)
            out = Path(tmp) / "release"
            build_release(run, out, include_grid=False)
            obs = pd.read_csv(out / "02_compilation" / "observations.csv")
            records = pd.read_csv(out / "02_compilation" / "records_metadata.csv")
            self.assertEqual(list(obs["observation_id"]), list(frame["observation_id"]))
            self.assertTrue(obs["record_id"].str.startswith("unlabelled-").all())
            self.assertEqual(obs["record_id"].nunique(), 2)
            self.assertEqual(set(obs["record_id"]), set(records["record_id"]))
            self.assertEqual(len(list((out / "01_records").glob("*.lpd"))), 2)

    def test_build_release_accounting(self):
        frame = _synthetic_frame()
        with TemporaryDirectory() as tmp:
            run = Path(tmp) / "run_test"
            (run / "consolidated").mkdir(parents=True)
            frame.to_csv(run / "consolidated" / "all_data_combined.csv", index=False)
            queue = pd.DataFrame([
                {"file": "x.csv", "sheet": "s1", "column": "d18O", "action": "暂缓该值",
                 "reason": "unit unconfirmed", "source_value": "1.0", "source_unit": "?",
                 "source_record_id": "recA", "decision": "δ18O_value"},
                {"file": "x.csv", "sheet": "s1", "column": "d18O", "action": "已应用证据复核",
                 "reason": "", "source_value": "2.0", "source_unit": "permil",
                 "source_record_id": "recA", "decision": "δ18O_value"}])
            queue.to_csv(run / "review_queue.csv", index=False)
            out = Path(tmp) / "release"
            summary = build_release(run, out, version="0.0.1", include_grid=False)

            obs = pd.read_csv(out / "02_compilation" / "observations.csv")
            records = pd.read_csv(out / "02_compilation" / "records_metadata.csv")
            prov = pd.read_csv(out / "04_provenance_qc" / "value_provenance.csv")
            held = pd.read_csv(out / "04_provenance_qc" / "held_values.csv")
            self.assertEqual(len(obs), len(frame))                    # 行数闭合
            self.assertEqual(len(records), 3)
            self.assertEqual(len(prov), frame["δ18O (‰ VPDB)"].notna().sum())
            self.assertEqual(len(held), 1)                            # 暂缓披露
            self.assertEqual(summary["accounting"]["reprint_records"], 1)
            self.assertEqual(len(list((out / "01_records").glob("*.lpd"))), 3)
            self.assertTrue((out / "README.md").exists())
            self.assertTrue((out / "DATA_DICTIONARY.md").exists())
            # 派生视图行数闭合
            derived = pd.read_csv(out / "03_derived" / "by_resolution" / "annual.csv")
            self.assertEqual(len(derived), len(frame))
            # 主表转载标记与记录表一致
            flagged = obs.loc[obs["reprint_of"].notna() & (obs["reprint_of"] != ""), "record_code"]
            self.assertEqual(flagged.nunique(), 1)


if __name__ == "__main__":
    unittest.main()
