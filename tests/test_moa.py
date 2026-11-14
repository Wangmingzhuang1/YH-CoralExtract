"""用原表和离线模型响应复核 MOA，所有产物写入临时目录。"""

import csv
import importlib.util
import json
import os
import re
import shutil
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "eval_sample"


class MOARegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.addClassCleanup(temporary.cleanup)
        cls.addClassCleanup(os.chdir, Path.cwd())
        os.chdir(temporary.name)

        from app.core.moa.workflow import MOAWorkflow
        from app.core.moa.unit_converter import UnitConversionModule, finite_number
        from app.core.moa.llm_parser import LLMParsingModule
        from app.core.moa.context_extractor import ContextExtractor
        from app.utils.review_queue import clear_review_items
        from app.utils.calibration_checker import check_sr_ca_calibration
        from app.core.moa.unit_converter import resolve_unit

        cls.Workflow = MOAWorkflow
        cls.UnitConverter = UnitConversionModule
        cls.Parser = LLMParsingModule
        cls.Context = ContextExtractor
        cls.calibration = staticmethod(check_sr_ca_calibration)
        cls.resolve_unit = staticmethod(resolve_unit)
        cls.number = staticmethod(finite_number)
        cls.clear_review = staticmethod(clear_review_items)

    def setUp(self):
        self.clear_review()
        self.workflow = self.Workflow(output_dir=self._testMethodName)
        layout = patch("app.core.moa.table_layout.call_llm", return_value="")
        layout.start()
        self.addCleanup(layout.stop)
        request = patch("requests.post", side_effect=AssertionError("禁止网络请求"))
        self.post = request.start()
        self.addCleanup(request.stop)
        self.addCleanup(self.post.assert_not_called)

    def fixture(self, filename, directory=SAMPLES):
        path = next(directory.rglob(filename), None)
        if path is None:
            self.skipTest(f"缺少本地研究附件样本 {filename}")
        return path

    def local_fixture(self, path):
        if not path.is_file():
            self.skipTest(f"缺少本地研究输入 {path.name}")
        return path

    def dataset_fixture(self, relative_path):
        path = Path(os.environ.get("MOA_TEST_DATASET", ROOT / "dataset")) / relative_path
        if not path.is_file():
            self.skipTest(f"缺少原始附件样本 {relative_path}，可通过 MOA_TEST_DATASET 指定样本目录")
        return path

    def database_rows(self, path, fields):
        decisions = {"mapped_data": [
            {"original_header": header, "standard_field": field}
            for header, field in fields.items()
        ], "table_metadata": {"resolution": "monthly"}}
        parser = self.workflow.database_parser.llm_parser
        with patch.object(parser, "parse_table_structure", return_value=decisions):
            if path.suffix == ".txt":
                return self.workflow.process_noaa_file(str(path), path.stem)
            return self.workflow.process_pangaea_file(str(path), path.stem)

    def test_abram_uses_observation_and_primary_publication(self):
        path = self.fixture("Abram2008a1.txt")
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        first = next(i for i, line in enumerate(lines) if line.startswith("age\td18O\t"))
        source = csv.DictReader(lines[first:], delimiter="\t")
        expected = Counter((float(row["age"]), value) for row in source
                           if (value := self.number(row["d18O"])) is not None)
        with patch.object(self.workflow.database_parser.llm_parser, "parse_table_structure") as model:
            rows = self.workflow.process_noaa_file(str(path), path.stem)
            model.assert_not_called()
        self.assertEqual(Counter((row["year"], row["δ18O_value"]) for row in rows), expected)
        self.assertEqual(len(rows), 1670)
        first_row = rows[0]
        self.assertEqual(first_row["δ18O_value"], -5.50)
        self.assertEqual(first_row["source_columns"]["δ18O_value"], "d18O")
        self.assertEqual(first_row["source_line"], first + 2)
        self.assertEqual(first_row["pub1_doi"], "10.1038/ngeo357")
        self.assertEqual(first_row["dataset_doi"], "10.25921/j078-dh26")
        self.assertTrue(first_row["pub1_authors"].startswith("Abram,"))
        self.assertEqual(first_row["testing_method"], "isotope ratio mass spectrometry")
        self.assertIsNone(first_row["geo_latitude"])
        self.assertAlmostEqual(first_row["geo_rep_latitude"], -0.265)
        self.assertAlmostEqual(first_row["geo_rep_longitude"], 98.535)
        self.assertAlmostEqual(first_row["geo_lat_span"], 0.27)
        self.assertEqual(first_row["geo_rep_status"], "reviewed_regional_composite")
        self.assertIn("composited", first_row["source_processing"])
        self.assertEqual(first_row["mapping_route"], "archive_metadata")

    def test_parallel_archives_match_serial_and_include_attachment_sources(self):
        directories = [Path(self._testMethodName) / name for name in ("dataset", "noaa", "pangaea", "zenodo")]
        dataset, noaa, pangaea, zenodo = directories
        paper = zenodo / "srca" / "Reed"
        for directory in (dataset, noaa, pangaea, paper):
            directory.mkdir(parents=True)
        for name, directory in (("Abram2008a1.txt", noaa), ("D'Olivo2022c.tab", pangaea)):
            shutil.copy2(self.fixture(name), directory / name)
        toth = self.local_fixture(ROOT / "noaa" / "Toth2015" / "Toth2015a1.txt")
        shutil.copy2(toth, noaa / toth.name)
        reed = self.fixture("Reed2021b1.xlsm", ROOT / "zenodo")
        shutil.copy2(reed, paper / reed.name)
        kwargs = {"noaa_dir": str(noaa), "pangaea_dir": str(pangaea), "zenodo_dir": str(zenodo)}
        serial = self.workflow.process_directory(str(dataset), **kwargs)
        concurrent = self.Workflow(output_dir=self._testMethodName, max_workers=2)
        parallel = concurrent.process_directory(str(dataset), use_parallel=True, **kwargs)
        self.assertEqual(Counter(row["paper_hash"] for row in parallel),
                         {"noaa_Abram2008a1.txt": 1670, "noaa_Toth2015a1.txt": 150, "pangaea_D'Olivo2022c.tab": 11204,
                          "zenodo_srca_Reed": 794})
        self.assertEqual(serial, parallel)

    def test_noaa_uncertain_bounds_do_not_become_map_or_grid_coordinates(self):
        from app.utils.review_queue import get_review_items

        parser = self.workflow.database_parser
        metadata = {"geo_bounds": {"Southernmost_Latitude": 8, "Northernmost_Latitude": 9,
                                   "Westernmost_Longitude": 170, "Easternmost_Longitude": -175}}
        parser._resolve_noaa_location(metadata, "reversed.txt")
        self.assertNotIn("geo_rep_latitude", metadata)
        self.assertEqual(get_review_items()[0]["action"], "复核空间范围")
        metadata["geo_bounds"]["Easternmost_Longitude"] = 171
        parser._resolve_noaa_location(metadata, "extent.txt")
        self.assertEqual(metadata["geo_rep_latitude"], 8.5)
        self.assertEqual(metadata["geo_rep_status"], "pending_review")
        self.assertNotIn("geo_latitude", metadata)

    def test_dolivo_matches_source_and_experiment_is_excluded(self):
        path = self.fixture("D'Olivo2022c.tab")
        column = "Porites spp. 86Sr/43Ca [mmol/mol]"
        contents = path.read_text(encoding="utf-8-sig")
        source = csv.DictReader(contents.split("*/", 1)[1].strip().splitlines(), delimiter="\t")
        expected = Counter((float(row["Age [a AD/CE]"]), value) for row in source
                           if (value := self.number(row[column])) is not None)
        with patch.object(self.workflow.database_parser.llm_parser, "parse_table_structure") as model:
            rows = self.workflow.process_pangaea_file(str(path), path.stem)
            model.assert_not_called()
        self.assertEqual(Counter((row["year"], row["Sr/Ca_value"]) for row in rows), expected)
        self.assertEqual(len(rows), 11204)
        self.assertEqual(rows[0]["source_record_id"], "FZ04B-1")
        self.assertEqual(rows[0]["geo_latitude"], -16.93)
        self.assertEqual(rows[0]["dataset_doi"], "10.1594/PANGAEA.944980")
        self.assertEqual(rows[0]["hasResolution_nominal"], "submonthly")
        self.assertEqual(rows[0]["mapping_route"], "archive_metadata")
        experiment = self.fixture("Allison2011.tab")
        with patch.object(self.workflow.database_parser.llm_parser, "parse_table_structure") as model:
            parsed = self.workflow.database_parser.parse_file(str(experiment))
            self.assertEqual(parsed["skip_reason"], "experimental_table_without_chronology")
            model.assert_not_called()

    def test_noaa_preserves_bp_axis_and_excludes_smoothed_and_seawater_columns(self):
        path = self.local_fixture(ROOT / "noaa" / "Toth2015" / "Toth2015a1.txt")
        with patch.object(self.workflow.database_parser.llm_parser, "parse_table_structure") as model:
            rows = self.workflow.process_noaa_file(str(path), path.stem)
            model.assert_not_called()
        self.assertTrue(rows)
        first = next(row for row in rows if "Sr/Ca_value" in row["source_columns"])
        self.assertEqual(first["year"], 1950 - float(first["source_time_value"]))
        self.assertEqual(first["source_time_column"], "Age_BP")
        self.assertEqual(first["source_columns"]["Sr/Ca_value"], "Sr/Ca")
        self.assertEqual(first["source_columns"]["δ18O_value"], "d18O")
        self.assertNotIn("sea_surface_temperature_value", first["source_columns"])

    def test_pangaea_uses_standalone_unit_comments_for_the_named_ratio(self):
        variables = self.workflow.database_parser._parse_pangaea_variables("\n".join([
            "Parameter(s): Strontium/Calcium ratio (Sr/Ca) * COMMENT: mmol/mol",
            "Barium/Calcium ratio (Ba/Ca) * COMMENT: JCp-1 standard 7 μmol/mol",
            "Magnesium/Calcium ratio [mmol/mol] (Mg/Ca) * COMMENT: μmol/mol",
            "License: CC-BY",
        ]))
        self.assertEqual({row["name"]: row["unit"] for row in variables},
                         {"Sr/Ca": "mmol/mol", "Ba/Ca": "", "Mg/Ca": "mmol/mol"})

    def test_pangaea_events_bind_by_label_and_age_bounds_are_not_observations(self):
        path = Path("events.tab")
        path.write_text("\n".join([
            "/* DATA DESCRIPTION:",
            "Citation:\tAuthors (2022): Study. https://doi.org/10.1594/PANGAEA.12345",
            "Event(s):\tA * LATITUDE: 10 * LONGITUDE: 100",
            "\tB * LATITUDE: 20 * LONGITUDE: 110",
            "*/",
            "Event\tAge, minimum [a AD/CE]\tAge [ka BP]\tAge, maximum [a AD/CE]\tSr/Ca [mmol/mol]",
            "B\t1300\t0.50\t1600\t9.0",
            "A\t1300\t0.55\t1600\t9.1",
            "Unmatched\t1300\t0.60\t1600\t9.2",
        ]), encoding="utf-8")
        rows = self.database_rows(path, {"Age [ka BP]": "age_ka_bp", "Sr/Ca [mmol/mol]": "Sr/Ca_value"})
        self.assertEqual([row["year"] for row in rows], [1450.0, 1400.0, 1350.0])
        self.assertEqual([row["source_record_id"] for row in rows], ["B", "A", None])
        self.assertEqual([row["geo_latitude"] for row in rows], [20.0, 10.0, None])

    def test_workbook_intake_keeps_sheet_rows_and_defers_unconfirmed_csv_variants(self):
        from openpyxl import Workbook
        from app.utils.review_queue import get_review_items

        path = Path("book.xlsm")
        workbook = Workbook()
        workbook.active.title = "Metadata"
        workbook.active.append(["Year (AD)", "Sr/Ca [mmol/mol]"])
        workbook.active.append([1900, 9.5])
        for title in ("Data", "Data (Annual)"):
            sheet = workbook.create_sheet(title)
            sheet.append(["Monthly or annual mean as labelled by the author"])
            sheet.append(["Year (AD)", "Sr/Ca [mmol/mol]"])
            sheet.append([2000, 9.1])
            sheet.append([2001, 9.2])
        workbook.save(path)

        decisions = {"mapped_data": [
            {"original_header": "Year (AD)", "standard_field": "year"},
            {"original_header": "Sr/Ca [mmol/mol]", "standard_field": "Sr/Ca_value"},
        ]}
        cache = {}
        with patch("app.core.moa.llm_parser.call_llm", return_value=json.dumps(decisions)) as model:
            rows = self.workflow.process_file(str(path), native_tables=cache)
            self.assertEqual(model.call_count, 2)
            self.assertIn("annual mean", model.call_args.args[1])
            self.assertEqual({(r["source_sheet_name"], r["source_line"]) for r in rows},
                             {(sheet, line) for sheet in ("Data", "Data (Annual)") for line in (3, 4)})
            self.assertFalse(list(Path.cwd().glob("book_*.csv")))
            copied = Path("book_Data.csv")
            copied.write_text("Year (AD),Sr/Ca [mmol/mol]\n2000,9.1\n2001,9.2\n", encoding="utf-8")
            self.assertEqual(self.workflow.process_file(str(copied), native_tables=cache), [])
            self.assertEqual(model.call_count, 2)
            copied.write_text("Year (AD),Sr/Ca [mmol/mol]\n2000,9.3\n2001,9.2\n", encoding="utf-8")
            self.assertEqual(self.workflow.process_file(str(copied), native_tables=cache), [])
            self.assertEqual(model.call_count, 2)
            self.assertIn("2000,9.3", copied.read_text(encoding="utf-8"))
        self.assertTrue(any(item["action"] == "暂缓差异表" for item in get_review_items()))

    def test_delimited_overflow_is_held_without_mapping_shifted_cells(self):
        from app.utils.review_queue import get_review_items

        path = Path("shifted.txt")
        path.write_text("Year (AD)\tSr/Ca [mmol/mol]\nSample-A\t2000\t9.1\n", encoding="utf-8")
        with patch.object(self.workflow.llm_parser, "parse_table_structure") as model:
            self.assertEqual(self.workflow.process_file(str(path)), [])
            model.assert_not_called()
        self.assertTrue(any("第 2 行" in item["reason"] and "错位" in item["reason"] for item in get_review_items()))

    def test_past_workbook_uses_declared_units_and_does_not_extract_template_examples(self):
        reed = self.fixture("Reed2021b1.xlsm", ROOT / "zenodo")
        peat = self.fixture("BKB-2_Yamamoto.xlsm", ROOT / "pangaea")
        tables = self.workflow.data_ingestion.read_file(str(reed))
        self.assertEqual({table["sheet_name"] for table in tables}, {"Data", "Data (Annual)"})
        with patch.object(self.workflow.llm_parser, "parse_table_structure") as model:
            rows = self.workflow.process_file(str(reed), paper_hash="Reed")
            self.assertEqual(model.call_count, 0)
        first = rows[0]
        self.assertAlmostEqual(first["year"], 2010.04166)
        self.assertAlmostEqual(first["Sr/Ca_value"], 9.196899661998287)
        self.assertEqual(first["source_line"], 15)
        self.assertEqual(first["source_time_unit"], "CE")
        self.assertEqual(first["mapping_route"], "workbook_definition")
        self.assertIn("workbook_definition", {item["source"] for item in first["Sr/Ca_value_unit_resolution"]["evidence"]})
        self.assertEqual({row["hasResolution_nominal"] for row in rows}, {"monthly", "annual"})
        tables = self.workflow.data_ingestion.read_file(str(peat))
        self.assertFalse(any(table["sheet_name"].startswith("Example") for table in tables))
        cellulose = next(table for table in tables if table["sheet_name"] == "Data cellulose d18O")
        plan = self.workflow.database_parser.plan_workbook_columns(cellulose)
        self.assertIs(plan["table_metadata"]["is_coral_data"], False)

    def test_resolution_comes_from_observed_spacing(self):
        from app.core.moa.spatiotemporal_standard import sampling_profile

        self.assertEqual(sampling_profile([2000, 2001])[0], "unknown")
        self.assertEqual(sampling_profile([2000 + index / 12 for index in range(20)])[0], "monthly")
        self.assertEqual(sampling_profile([2000, 2000.01, 2000.02, 2001, 2002, 2010])[0], "irregular")

    def test_chen_discrete_samples_and_roundtrip(self):
        path = self.fixture("Chen2022_Sheet2.csv")
        text = path.read_text(encoding="utf-8-sig").splitlines()
        expected = {row["Sample name"]: row for row in csv.DictReader([text[0], *text[2:]])}
        fields = {"Year (AD)": "year", "±2σ": "paleoData_dating_error", "Sr/Ca": "Sr/Ca_value",
                  "δ18O": "δ18O_value", "δ13C": "δ13C_value"}
        response = json.dumps({"mapped_data": [
            {"original_header": header, "standard_field": field}
            for header, field in fields.items()
        ], "table_metadata": {"resolution": "monthly"}})
        context = {"geo_siteName": "Luhuitou", "default_d18o_unit": "‰ VPDB",
                   "default_d13c_unit": "‰ VPDB", "paleoData_dating_method": "U-Th",
                   "paleoData_dating_error": "文献级描述",
                   "metadata_evidence": {"d18o_unit": "δ18O was reported on the VPDB scale.",
                                         "d13c_unit": "δ13C was reported on the VPDB scale."}}
        with patch("app.core.moa.llm_parser.call_llm", return_value=response):
            rows = self.workflow.process_file(str(path), paper_hash="Chen2022", pdf_context=context)
        self.assertEqual({row["sample_id"] for row in rows}, expected.keys())
        self.assertEqual(len(rows), 170)
        for row in rows:
            source = expected[row["sample_id"]]
            self.assertEqual(row["hasResolution_nominal"], "discrete_samples")
            self.assertEqual(row["year"], self.number(source["Year (AD)"]))
            self.assertEqual(self.number(row["paleoData_dating_error"]), self.number(source["±2σ"]))
            for source_field, field in (("Sr/Ca", "Sr/Ca_value"), ("δ18O ", "δ18O_value"), ("δ13C", "δ13C_value")):
                self.assertEqual(row.get(field), self.number(source[source_field]))

        self.workflow.processed_data = rows
        run = Path(self.workflow.generate_final_output())
        groups = run / "discrete_samples"
        with (groups / "G1_SrCa_O18_C13.csv").open(encoding="utf-8-sig", newline="") as file:
            self.assertEqual(len(list(csv.DictReader(file))), 150)
        with (groups / "G4_SrCa.csv").open(encoding="utf-8-sig", newline="") as file:
            self.assertEqual(len(list(csv.DictReader(file))), 20)
        with (run / "consolidated/all_data_combined.csv").open(encoding="utf-8-sig", newline="") as file:
            combined = list(csv.DictReader(file))
        self.assertEqual(len(combined), 170)
        self.assertEqual({row["time_resolution"] for row in combined}, {"discrete_samples"})
        self.assertEqual({row["sample_id"] for row in combined}, expected.keys())
        self.assertEqual([float(row["year_CE"]) for row in combined], [row["year"] for row in rows])
        self.assertEqual(int(combined[0]["source_line"]), 3)
        self.assertEqual(combined[0]["source_sheet_name"], "default")

        if importlib.util.find_spec("xarray") is not None:
            import xarray as xr
            with xr.open_dataset(run / "paleoclimate_grid.nc") as dataset:
                self.assertEqual(dataset.sizes["observation"], 170)
                self.assertEqual(dataset["time"].values.tolist(), [row["year"] for row in rows])
                self.assertEqual(dataset["sample_id"].values.tolist(), [row["sample_id"] for row in rows])
                self.assertEqual(dataset["qc_flag"].values.tolist(), [row["qc_flag"] for row in rows])
                self.assertEqual(dataset["paleoData_dating_error"].values.tolist(),
                                 [self.number(row["paleoData_dating_error"]) for row in rows])
                self.assertEqual(dataset["observation_id"].values.tolist(), [row["observation_id"] for row in combined])
                self.assertEqual(dataset["source_line"].values.tolist(), [row["source_line"] for row in rows])
                self.assertLess(len(set(dataset["time"].values)), 170)

        from app.analysis.loader import load_all_data, load_resolution_csv
        self.assertEqual(set(load_all_data(str(run))["Resolution"]), {"Discrete samples"})
        self.assertIsNone(load_resolution_csv(str(run), "monthly"))

    def test_chen_pdf_methods_confirm_both_isotope_scales(self):
        from app.utils.pdf_direct_processor import extract_text_from_pdf

        pdf = self.fixture("Geochemical significance*.pdf")
        text = extract_text_from_pdf(str(pdf))
        with patch("app.core.moa.context_extractor.call_llm", return_value=""):
            context = self.Context().extract_from_text(text, source_name=str(pdf))
        self.assertEqual(context["isotope_scale_evidence_page"], 4)
        for field, key in (("δ18O_value", "d18o_unit"), ("δ13C_value", "d13c_unit")):
            decision = self.resolve_unit(field, "", [("unit_row", "‰")],
                                         context[f"default_{key}"], context["metadata_evidence"][key])
            self.assertEqual(decision["unit"], "‰ VPDB")
            self.assertEqual(decision["status"], "resolved")

    def test_bp_reference_and_unknown_units(self):
        for name, value in (("Age [ka BP]", "0.5"), ("Age [a BP]", "500")):
            with self.subTest(name=name):
                path = Path(f"bp_{value}.tab")
                path.write_text(f"/* DATA DESCRIPTION:\n*/\n{name}\tSr/Ca [µmol/mol]\n{value}\t9000\n",
                                encoding="utf-8")
                rows = self.database_rows(path, {})
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["year"], 1450)
                self.assertEqual(rows[0]["Sr/Ca_value"], 9)

        converter = self.UnitConverter()
        for field, unit, value, expected in (
            ("Sr/Ca_value", "μmol/mol", 9000, 9), ("Sr/Ca_value", "µmol/mol", 9000, 9),
            ("δ18O_value", "‰ VSMOW", 25, None), ("δ18O_value", "‰", -4, None),
            ("Sr/Ca_value", "", 9, None), ("sea_surface_temperature_value", "°F", 77, 25),
        ):
            with self.subTest(field=field, unit=unit):
                point = converter.convert_units({field: value, f"{field}_source_unit": unit}, {})
                self.assertEqual(point[field], expected)
                self.assertEqual(point[f"{field}_original_value"], value)
                self.assertEqual(converter.verify_unit_conversion(point, field), expected is not None)

    def test_noaa_bp_reference_year_in_notes_is_not_a_ce_observation(self):
        path = self.local_fixture(ROOT / "noaa" / "DeLong2010" / "DeLong2010a3.txt")
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        header = next(i for i, line in enumerate(lines) if line.startswith("age_calBP2008\t"))
        source = list(csv.DictReader(lines[header:], delimiter="\t"))
        with patch.object(self.workflow.database_parser.llm_parser, "parse_table_structure") as model:
            rows = self.workflow.process_noaa_file(str(path), path.stem)
            model.assert_not_called()
        self.assertEqual(len(rows), 105)
        self.assertEqual(len(source), len(rows))
        self.assertEqual({row["source_time_column"] for row in rows}, {"age_calBP"})
        self.assertEqual({row["source_time_unit"] for row in rows}, {"yr BP"})
        self.assertEqual({row["qc_time_status"] for row in rows}, {"pass"})
        for row, original in zip(rows, source):
            self.assertAlmostEqual(row["year"], 1950 - float(original["age_calBP"]))
            self.assertAlmostEqual(row["year"], 2008 - float(original["age_calBP2008"]))
            self.assertEqual(row["Sr/Ca_value"], float(original["Sr_Ca_interp"]))

    def test_golden_date_diagnostic_preserves_values_and_observation_counts(self):
        from app.analysis.coral_comparison import count_close_dates, dates_by_value

        reference = dates_by_value(Counter({(1998.29, 8.95): 1, (2000.04, 9.0): 2, (2001.0, 9.1): 1}))
        candidate = dates_by_value(Counter({(1998.29, 8.96): 1, (2000.042, 9.0): 1, (2001.01, 9.1): 1}))
        self.assertEqual(count_close_dates(reference, candidate, 0.005), 1)

    def test_pdb_convention_keeps_original_label_and_verifies(self):
        field = "δ18O_value"
        point = {
            field: -4.6, "pub1_doi": "10.1016/j.palaeo.2014.07.039",
            "dataset_doi": "10.1594/PANGAEA.887712",
            "source_columns": {field: "P. lobata δ18O [‰ PDB]"},
            f"{field}_unit_resolution": self.resolve_unit(field, "P. lobata δ18O [‰ PDB]"),
        }
        # PDB≡VPDB 社区惯例（1993重定义）：表头声明 ‰ PDB 即按 VPDB 转换，
        # 由通用 resolve_unit/canonical_unit 处理，无需逐篇正文引文或硬编码特判；
        # 溯源保留档案原始声明标签 "‰ PDB"
        self.assertEqual(point[f"{field}_unit_resolution"]["unit"], "‰ VPDB")
        result = self.UnitConverter().convert_units(point, {})
        self.assertEqual(result[field], -4.6)
        self.assertEqual(result[f"{field}_original_unit"], "‰ PDB")
        self.assertEqual(result[f"{field}_unit"], "‰ VPDB")
        self.assertEqual(result[f"{field}_unit_status"], "verified")
        self.assertEqual(result[f"{field}_unit_resolution"]["evidence"][-1]["source"], "header")

    def test_clipperton_workbook_keeps_observations_without_confirmed_reference_material(self):
        from app.analysis.coral_comparison import baseline_series
        from app.utils.review_queue import get_review_items

        source = self.dataset_fixture("8/8.xlsx")
        article = next(source.parent.glob("*.md")).read_text(encoding="utf-8")
        field_map = [
            {"original_header": "Age [a AD/CE]", "standard_field": "year"},
            {"original_header": "P. lobata Sr/Ca [mmol/mol]", "standard_field": "Sr/Ca_value"},
            {"original_header": "P. lobata Sr/Ca anom [mmol/mol]", "standard_field": "Sr/Ca_value"},
        ]
        context = {"pub1_doi": "10.1016/j.palaeo.2014.07.039", "geo_siteName": "Clipperton Atoll",
                   "geo_latitude": 10.3, "geo_longitude": -109.21666,
                   "sr_ca_calibration_info": self.calibration(article)}

        def mapped_columns(table, _, pdf_context, **kwargs):
            return self.workflow.llm_parser.apply_source_units({"mapped_data": field_map}, table, pdf_context)

        with patch.object(self.workflow.llm_parser, "parse_table_structure", side_effect=mapped_columns):
            points = self.workflow.process_file(str(source), paper_hash=source.stem, pdf_context=context)
        baseline = ROOT / "output" / "coral_comparison" / "CoralHydro2k1_0_1.zip"
        expected = next(item["pairs"] for item in baseline_series(self.local_fixture(baseline))
                        if (item["entity"], item["proxy"]) == ("WU14CLI01", "Sr/Ca"))
        self.assertEqual(len(points), 1439)
        pairs = Counter((row["year"], row["Sr/Ca_value"]) for row in points)
        self.assertEqual(Counter((round(year, 2), value) for year, value in pairs.elements()), expected)
        self.assertEqual({row["sr_ca_reference_status"] for row in points}, {"unconfirmed"})
        self.assertEqual({row["source_columns"]["Sr/Ca_value"] for row in points},
                         {"P. lobata Sr/Ca [mmol/mol]"})
        pending = [row for row in get_review_items() if row["action"] == "复核参考材料"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["sheet"], "Sheet1")
        self.workflow.processed_data = points
        batch = Path(self.workflow.generate_final_output())
        with (batch / "consolidated" / "all_data_combined.csv").open(encoding="utf-8-sig", newline="") as stream:
            archived = list(csv.DictReader(stream))
        self.assertEqual(Counter((float(row["year_CE"]), float(row["Sr/Ca (mmol/mol)"]))
                                 for row in archived), pairs)
        self.assertEqual({row["sr_ca_reference_status"] for row in archived}, {"unconfirmed"})

    def test_attachment_metadata_and_word_notes_remain_scoped_to_the_source(self):
        from docx import Document

        workbook = self.dataset_fixture("3/1.xlsx")
        blocks = self.workflow.data_ingestion.read_file(str(workbook))
        block = next(table for table in blocks if table["sheet_name"] == "uneven data#block_2")
        self.assertEqual(block["sample_id"], "PM1")
        self.assertIn("2019GC008420", block["table_description"])
        self.assertIn("monthly d18O: contains monthly interpolated", block["table_description"])

        path = Path("word_proxy.docx")
        document = Document()
        caption = "Table S1. Subseasonal observations from the coral chronology, reported without a unit column."
        note = "Note: The skeletal Sr/Ca values are reported in µmol/mol."
        document.add_paragraph(caption)
        table = document.add_table(rows=2, cols=2)
        table.cell(0, 0).text, table.cell(0, 1).text = "Year (CE)", "Sr/Ca"
        table.cell(1, 0).text, table.cell(1, 1).text = "1990", "9000"
        document.add_paragraph(note)
        document.save(path)
        parsed = self.workflow.data_ingestion.read_file(str(path))[0]
        self.assertEqual(parsed["table_caption"], f"{caption}\n{note}")
        self.assertEqual(parsed["source_rows"], [2])

        def mapped_columns(table, _, pdf_context, **kwargs):
            return self.workflow.llm_parser.apply_source_units({"mapped_data": [
                {"original_header": "Year (CE)", "standard_field": "year"},
                {"original_header": "Sr/Ca", "standard_field": "Sr/Ca_value"},
            ]}, table, pdf_context)

        with patch.object(self.workflow.llm_parser, "parse_table_structure", side_effect=mapped_columns):
            points = self.workflow.process_file(str(path), paper_hash=path.stem)
        self.assertEqual(len(points), 1)
        self.assertEqual((points[0]["year"], points[0]["Sr/Ca_value"]), (1990, 9))
        self.assertEqual(points[0]["Sr/Ca_value_original_unit"], "μmol/mol")
        self.assertEqual(points[0]["Sr/Ca_value_unit_resolution"]["evidence"][0]["quote"], note)

    def test_pangaea_indented_supplement_keeps_paper_and_dataset_dois_separate(self):
        source = self.local_fixture(ROOT / "pangaea" / "2" / "ClippertonComposite.tab")
        parser = self.workflow.database_parser
        metadata, _ = parser._split_pangaea_content(source.read_text(encoding="utf-8-sig"))
        record = parser._parse_pangaea_metadata(metadata)
        self.assertEqual(record["pub1_doi"], "10.1016/j.palaeo.2014.07.039")
        self.assertEqual(record["dataset_doi"].casefold(), "10.1594/pangaea.887712")

    def test_golden_workbook_versions_are_compared_at_their_documented_precision(self):
        from decimal import Decimal, ROUND_HALF_UP

        from app.analysis.coral_comparison import baseline_series

        baseline = ROOT / "output" / "coral_comparison" / "CoralHydro2k1_0_1.zip"
        reference = {(item["entity"], item["proxy"]): item["pairs"]
                     for item in baseline_series(self.local_fixture(baseline))}
        for workbook, cases in (
            ("3/1.xlsx", [
                ("monthly d18O#block_1", "AGE", "PM1 δ18O  (‰ VPDB)", "SA19PAL01", "δ18O"),
                ("monthly d18O#block_1", "AGE", "PM5 δ18O  (‰ VPDB)", "SA19PAL02", "δ18O"),
                ("monthly SrCa#block_1", "Age", "PM1 Sr/Ca (mmol/mol)", "SA19PAL01", "Sr/Ca"),
                ("monthly SrCa#block_1", "Age", "PM5  Sr/Ca (mmol/mol)", "SA19PAL02", "Sr/Ca"),
            ]),
            ("5/5.xlsx", [
                ("Modern#block_2", "Year (CE) [column 5]", "LY1", "CH18YOA01", "Sr/Ca"),
                ("Modern#block_4", "Year (CE) [column 11]", "LY2", "CH18YOA02", "Sr/Ca"),
            ]),
        ):
            tables = {table["sheet_name"]: table for table in
                      self.workflow.data_ingestion.read_file(str(self.dataset_fixture(workbook)))}
            for sheet, age, column, entity, proxy in cases:
                with self.subTest(entity=entity, proxy=proxy):
                    if workbook == "5/5.xlsx":
                        self.assertEqual(tables[sheet]["sample_id"], column)
                    pairs = Counter()
                    for row in tables[sheet]["data"]:
                        if self.number(row[age]) is None or self.number(row[column]) is None:
                            continue
                        year = Decimal(str(row[age]))
                        value = Decimal(str(row[column]))
                        if workbook == "3/1.xlsx":
                            year = year.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                            value = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                        else:
                            value = value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
                        pairs[float(year), float(value)] += 1
                    gold = reference[entity, proxy]
                    if entity == "CH18YOA02":
                        # 同一岩芯的工作簿与参考库并非逐值相同。核对已知重合数和记录总数。
                        self.assertEqual((sum(pairs.values()), sum(gold.values()), sum((pairs & gold).values())),
                                         (154, 154, 103))
                    else:
                        self.assertEqual(pairs, gold)

    def test_opaque_coral_columns_keep_body_units_and_core_identity_in_the_archive(self):
        source = self.dataset_fixture("5/5.xlsx")
        article = source.with_suffix(".md").read_text(encoding="utf-8")
        quote = "Precision for the coral Sr/Ca analyses was 0.014 mmol/mol (1σ), as determined by analyses of repeat standards."
        self.assertIn(quote, article)
        choices = {"Modern#block_2": ("Year (CE) [column 5]", "LY1"),
                   "Modern#block_4": ("Year (CE) [column 11]", "LY2")}

        def reviewed_mapping(table, _, context, **kwargs):
            choice = choices.get(table["sheet_name"])
            fields = [] if choice is None else [
                {"original_header": choice[0], "standard_field": "year"},
                {"original_header": choice[1], "standard_field": "Sr/Ca_value"},
            ]
            return self.workflow.llm_parser.apply_source_units({"mapped_data": fields}, table, context)

        context = {"pub1_doi": "10.1002/2018gl077619", "geo_siteName": "Yongle Atoll",
                   "default_sr_ca_unit": "mmol/mol", "metadata_evidence": {"sr_ca_unit": quote},
                   "sr_ca_calibration_info": self.calibration(article)}
        with patch.object(self.workflow.llm_parser, "parse_table_structure", side_effect=reviewed_mapping):
            self.workflow.processed_data = self.workflow.process_file(str(source), paper_hash="Chen2018", pdf_context=context)
        batch = Path(self.workflow.generate_final_output())
        with (batch / "consolidated" / "all_data_combined.csv").open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 270)
        source_tables = {table["sheet_name"]: table for table in self.workflow.data_ingestion.read_file(str(source))}
        for core, entity in (("LY1", "CH18YOA01"), ("LY2", "CH18YOA02")):
            subset = [row for row in rows if row["source_record_id"] == core]
            self.assertTrue(all(quote in row["Sr/Ca_value_unit_evidence"] for row in subset))
            self.assertEqual({row["Sr/Ca_value_original_unit"] for row in subset}, {"mmol/mol"})
            sheet = next(name for name, (_, sample) in choices.items() if sample == core)
            age = choices[sheet][0]
            expected = Counter((float(row[age]), value) for row in source_tables[sheet]["data"]
                               if (value := self.number(row[core])) is not None)
            pairs = Counter((float(row["year_CE"]), float(row["Sr/Ca (mmol/mol)"])) for row in subset)
            self.assertEqual(pairs, expected, entity)

    def test_judge_review_only_sends_missing_cells_as_null_and_keeps_mapping(self):
        from app.core.config import settings
        from app.utils.review_queue import get_review_items

        parser = self.Parser(enable_cache=False)
        table = {"headers": ["±2σ", "δ18O"], "data": [{"±2σ": "2", "δ18O": float("nan")}]}
        result = {"mapped_data": [{"original_header": "±2σ", "standard_field": "paleoData_dating_error"}]}
        with (patch.object(settings, "JUDGE_REVIEW_ONLY", True),
              patch("app.core.moa.llm_parser.judge_enabled", return_value=True),
              patch("app.core.moa.llm_parser.judge_noul_batch", return_value={"col_0": 0.46}) as judge):
            parser._judge_mapped_columns(result, table)
        self.assertEqual(judge.call_args.args[0]["sample_rows"], [{"±2σ": "2", "δ18O": None}])
        self.assertEqual(result["mapped_data"][0]["standard_field"], "paleoData_dating_error")
        self.assertEqual(get_review_items()[0]["action"], "判卷提示，保留映射")

    def test_cache_uses_table_evidence_and_same_postprocessing(self):
        parser = self.Parser(cache_dir="test_cache")
        response = json.dumps({"mapped_data": [
            {"original_header": "Ba/Ca", "standard_field": "Sr/Ca_value"},
            {"original_header": "Year", "standard_field": "year"},
        ]})
        table = {"headers": ["Year", "Ba/Ca"], "data": [{"Year": 2000, "Ba/Ca": 2}]}
        with patch("app.core.moa.llm_parser.call_llm", return_value=response) as model:
            cold = parser.parse_table_structure(table, context="A")
            warm = parser.parse_table_structure(table, context="A")
            other = {**table, "data": [{"Year": 1900, "Ba/Ca": 3}]}
            parser.parse_table_structure(other, context="B")
        self.assertEqual(cold, warm)
        self.assertEqual(cold["mapped_data"][0]["standard_field"], "Ba/Ca_value")
        self.assertEqual(model.call_count, 2)

    def test_inferred_isotope_scale_requires_a_source_quote(self):
        quote = "Coral δ18O was measured on the VPDB scale."
        body = "Methods. " + quote + " Sampling sites and age models are described in Supplementary Table 1."
        response = json.dumps({"d18o_unit": "‰ VPDB", "d13c_unit": "‰ VPDB",
                               "evidence": {"d18o_unit": quote, "d13c_unit": "fabricated evidence"}})
        with patch("app.core.moa.context_extractor.call_llm", return_value=response):
            context = self.Context().extract_from_text(body, source_name="sample.pdf")
        table = {"units": {"δ18O": "‰", "δ13C": "‰"}}
        mapping = {"mapped_data": [
            {"original_header": "δ18O", "standard_field": "δ18O_value"},
            {"original_header": "δ13C", "standard_field": "δ13C_value"},
        ]}
        result = self.workflow.llm_parser.apply_source_units(mapping, table, context)
        self.assertEqual(context["metadata_evidence"], {"d18o_unit": quote})
        self.assertEqual([item["unit"] for item in result["mapped_data"]], ["‰ VPDB", "‰"])

    def test_model_quota_skips_requests_but_temporary_rate_limit_retries(self):
        import requests
        from app.core.config import settings
        from app.utils import llm_api

        def response(status, body):
            result = requests.Response()
            result.status_code = status
            result._content = json.dumps(body).encode("utf-8")
            return result

        quota = response(429, {"error": {"code": "insufficient_quota"}})
        rate_limit = response(429, {"error": {"code": "rate_limit_exceeded"}})
        success = response(200, {"choices": [{"message": {"content": "OK"}}]})
        with (patch.object(settings, "LLM_API_KEY", "fixture-quota"),
              patch.object(llm_api, "_exhausted_quotas", set()),
              patch.object(llm_api.requests, "post", side_effect=[quota, rate_limit, success, success]) as post,
              patch.object(llm_api.time, "sleep") as sleep):
            self.assertEqual(llm_api.call_llm("max", "fixture"), "")
            self.assertEqual(llm_api.call_llm("max", "fixture again"), "")
            self.assertEqual(post.call_count, 1)
            sleep.assert_not_called()
            self.assertEqual(llm_api.call_llm("flash", "fixture"), "OK")
            sleep.assert_called_once_with(1)
            with patch.object(settings, "LLM_API_KEY", "other-fixture"):
                self.assertEqual(llm_api.call_llm("max", "fixture"), "OK")
            self.assertEqual(post.call_count, 4)

    def test_paper_context_cache_rechecks_evidence_and_retries_empty_reply(self):
        quote = "Coral δ18O was measured on the VPDB scale."
        body = "Methods. " + quote + " Sampling sites and age models are described in Supplementary Table 1."
        response = json.dumps({"d18o_unit": "‰ VPDB", "d13c_unit": "‰ VPDB",
                               "evidence": {"d18o_unit": quote, "d13c_unit": "fabricated evidence"}})
        with (patch("app.utils.pdf_direct_processor.pdfplumber.open", side_effect=OSError),
              patch("app.core.moa.context_extractor.call_llm",
                    side_effect=["", response, '{"authors":"Truncated', response]) as model):
            first = self.workflow.context_extractor
            self.assertEqual(first.extract_from_text(body, "a.pdf"), {})
            cold = first.extract_from_text(body, "a.pdf")
            replay = self.Workflow(output_dir=self._testMethodName).context_extractor
            warm = replay.extract_from_text(body, "b.pdf")
            self.assertEqual(warm, {**cold, "metadata_source": "b.pdf"})
            self.assertEqual(warm["metadata_evidence"], {"d18o_unit": quote})
            self.assertEqual(model.call_count, 2)
            changed_body = body + " Additional measurements are described in the archive."
            self.assertEqual(replay.extract_from_text(changed_body, "a.pdf"), {})
            self.assertEqual(replay.extract_from_text(changed_body, "a.pdf"), cold)
            self.assertEqual(model.call_count, 4)

    def test_model_failure_uses_explicit_source_unit_and_keeps_unknown_isotope_out(self):
        path = Path("explicit_units.csv")
        path.write_text("Year (CE),Sr/Ca [μmol/mol],δ18O [‰]\n2000,9100,-4.2\n", encoding="utf-8")
        with patch("app.core.moa.llm_parser.call_llm", return_value=""):
            rows = self.workflow.process_file(str(path))
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["Sr/Ca_value"], 9.1)
        self.assertIsNone(rows[0]["δ18O_value"])
        self.assertEqual(rows[0]["δ18O_value_unit_status"], "unknown_unit")

    def test_seawater_isotope_cannot_replace_skeletal_observation(self):
        path = Path("mixed_isotope.csv")
        fields = {"Year (CE)": "year", "δ18O [‰ VPDB]": "δ18O_value", "δ¹⁸O_sw [‰ VSMOW]": "δ18O_value"}
        path.write_text(",".join(fields) + "\n2000,-4.2,0.42\n", encoding="utf-8")
        response = {"mapped_data": [{"original_header": key, "standard_field": field}
                                    for key, field in fields.items()]}
        with patch("app.core.moa.llm_parser.call_llm", return_value=json.dumps(response)):
            rows = self.workflow.process_file(str(path))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["δ18O_value"], -4.2)
        self.assertEqual(rows[0]["source_columns"]["δ18O_value"], "δ18O [‰ VPDB]")

    def test_unresolved_unit_is_reviewable_with_no_publishable_rows(self):
        path = Path("unknown_unit.csv")
        path.write_text("Year (CE),Sample name,Sr/Ca\n2000,001,9\n", encoding="utf-8")
        response = json.dumps({"mapped_data": [
            {"original_header": "Year (CE)", "standard_field": "year", "unit": "CE"},
            {"original_header": "Sr/Ca", "standard_field": "Sr/Ca_value", "unit": "mmol/mol"},
        ]})
        with patch("app.core.moa.llm_parser.call_llm", return_value=response):
            self.assertEqual(self.workflow.process_file(str(path), paper_hash="unit_test"), [])
        run = Path(self.workflow.generate_final_output())
        with (run / "review_queue.csv").open(encoding="utf-8-sig", newline="") as file:
            pending = list(csv.DictReader(file))
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["sample_id"], "001")
        self.assertEqual(pending[0]["source_value"], "9")
        self.assertFalse((run / "consolidated/all_data_combined.csv").exists())

    def test_reference_material_for_another_proxy_is_not_srca_evidence(self):
        result = self.calibration("Sr/Ca was measured by ICP-MS. JCp-1 was used to calibrate δ11B.")
        self.assertFalse(result["reference_reported"])
        self.assertTrue(result["reference_unconfirmed"])
        result = self.calibration("Sr/Ca measurement precision was monitored with JCp-1.")
        self.assertTrue(result["reference_reported"])
        self.assertFalse(result["is_calibrated"])
        self.assertFalse(result["reference_unconfirmed"])

    def test_parallel_sample_columns_keep_each_identity_and_unit(self):
        path = Path("wide.csv")
        fields = {"Year (CE)": "year", "P1 Sr/Ca [µmol/mol]": "Sr/Ca_value",
                  "P10 Sr/Ca [mmol/mol]": "Sr/Ca_value", "P10 δ18O [‰ VPDB]": "δ18O_value",
                  "P2 δ13C [‰ VPDB]": "δ13C_value", "P1 d18O_anom [‰ VPDB]": "δ18O_value"}
        with path.open("w", encoding="utf-8", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(fields)
            writer.writerow([2000, 9000, 9.1, -4, -2, 0.5])
        response = json.dumps({"mapped_data": [
            {"original_header": header, "standard_field": field}
            for header, field in fields.items()
        ]})
        with patch("app.core.moa.llm_parser.call_llm", return_value=response):
            result = self.workflow.process_file(str(path), paper_hash="wide_test")
        rows = {row["sample_id"]: row for row in result}
        self.assertEqual(set(rows), {"P1", "P10", "P2"})
        self.assertEqual(rows["P1"]["Sr/Ca_value"], 9)
        self.assertIsNone(rows["P1"].get("δ18O_value"))
        self.assertEqual(rows["P10"]["Sr/Ca_value"], 9.1)
        self.assertEqual(rows["P10"]["δ18O_value"], -4)
        self.assertEqual(rows["P2"]["δ13C_value"], -2)

    def test_single_proxy_sheet_uses_original_core_columns_as_series_ids(self):
        from app.utils.review_queue import get_review_items

        header = ["AD", "PSYL-3-2", "15XS1", "Ramos-Porites", "Error1", "Unnamed"]
        table = {"sheet_name": "annual-δ18O", "file_path": "supplement.xlsx"}
        paper = {"_pdf_text": "Coral core Ramos-Porites was sampled at the reef."}
        response = [{"original_header": name, "standard_field": "year" if name == "AD" else "δ18O_value",
                     "unit": "‰ VPDB"} for name in header]
        mapping = self.workflow._map_csv_columns(header, response, table, paper)
        base = {"source_file": "supplement.xlsx", "source_row": 1, "source_line": 2, "sheet_name": table["sheet_name"]}
        points = self.workflow._process_multi_sample_row(dict(zip(header, [2000, -4, -5, -6, 0.2, -7])), mapping, base)
        self.assertEqual({(p["source_record_id"], p["δ18O_value"]) for p in points},
                         {("PSYL-3-2", -4), ("15XS1", -5), ("Ramos-Porites", -6)})
        self.assertEqual({(r["column"], r["sheet"], r["source_line"]) for r in get_review_items()},
                         {("Error1", "annual-δ18O", 2), ("Unnamed", "annual-δ18O", 2)})
        for label in ("data", "δ18O and Sr/Ca", "mean coral δ18O"):
            alternative = self.workflow._map_csv_columns(header, response, {**table, "sheet_name": label}, paper)
            self.assertFalse(alternative["PSYL-3-2"]["sample_id"])

    def test_unit_conflicts_are_held_and_aliases_agree(self):
        for header, declarations, model_unit, quote in (
            ("δ18O", [("unit_row", "‰")], "‰ VPDB", "Coral δ18O was measured on the VSMOW scale."),
            ("δ18O [‰ VPDB]", [("database_definition", "per mil VSMOW")], None, None),
            ("δ18O [‰ VSMOW]", [("unit_row", "‰ VPDB")], None, None),
        ):
            with self.subTest(header=header, quote=quote):
                resolved = self.resolve_unit("δ18O_value", header, declarations, model_unit, quote)
                self.assertEqual(resolved["status"], "unit_conflict")
                self.assertIsNone(resolved["unit"])
        conflict = self.resolve_unit("Sr/Ca_value", "Sr/Ca [µmol/mol]", [("unit_row", "mmol/mol")])
        self.assertEqual(conflict["status"], "unit_conflict")
        agree = self.resolve_unit("Sr/Ca_value", "Sr/Ca [µmol mol−1]", [("unit_row", "μmol/mol")])
        self.assertEqual(agree["unit"], "μmol/mol")
        self.assertEqual(agree["status"], "resolved")
        unresolved = self.resolve_unit("δ18O_value", "δ18O", [("unit_row", "‰")], "‰ VPDB",
                                       "δ13C was measured on the VPDB scale.")
        self.assertEqual(unresolved["status"], "unknown_unit")

    def test_ambiguous_age_cannot_become_ce(self):
        from app.utils.review_queue import get_review_items
        path = Path("relative_age.tab")
        path.write_text("/* DATA DESCRIPTION:\n*/\nAge [a]\tSr/Ca [mmol/mol]\n1\t9\n2\t9\n",
                        encoding="utf-8")
        rows = self.database_rows(path, {"Age [a]": "year"})
        self.assertEqual(rows, [])
        self.assertTrue(any(item.get("column") == "Age [a]" for item in get_review_items()))

    def test_manual_unit_review_roundtrip_and_source_change(self):
        path = Path("review_source.csv")
        path.write_text("Year (CE),Sample ID,Sr/Ca\n2000,001,9000\n", encoding="utf-8")
        response = json.dumps({"mapped_data": [
            {"original_header": "Year (CE)", "standard_field": "year"},
            {"original_header": "Sr/Ca", "standard_field": "Sr/Ca_value"},
        ]})

        def process(workflow):
            with patch("app.core.moa.llm_parser.call_llm", return_value=response):
                return workflow.process_file(str(path), paper_hash="unit_review")

        self.assertEqual(process(self.workflow), [])
        first_run = Path(self.workflow.generate_final_output())
        with (first_run / "review_queue.csv").open(encoding="utf-8-sig", newline="") as file:
            decision = next(csv.DictReader(file))
        decision.update(review_decision="confirm_unit", confirmed_unit="µmol/mol", review_note="按源附录说明确认")
        decision_path = Path("reviewed_units.csv")
        with decision_path.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=list(decision))
            writer.writeheader()
            writer.writerow(decision)

        second = self.Workflow(output_dir="second", review_decisions=decision_path)
        rows = process(second)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Sr/Ca_value"], 9)
        self.assertEqual(rows[0]["Sr/Ca_value_unit_resolution"]["evidence"][-1]["source"], "manual_review")
        second.processed_data = rows
        second_run = Path(second.generate_final_output())
        with (second_run / "review_queue.csv").open(encoding="utf-8-sig", newline="") as file:
            self.assertEqual(next(csv.DictReader(file))["review_decision"], "confirm_unit")

        path.write_text("Year (CE),Sample ID,Sr/Ca\n2000,001,9100\n", encoding="utf-8")
        changed = self.Workflow(output_dir="changed", review_decisions=decision_path)
        self.assertEqual(process(changed), [])
        self.assertFalse(changed.applied_unit_reviews)
        changed_run = Path(changed.generate_final_output())
        with (changed_run / "review_queue.csv").open(encoding="utf-8-sig", newline="") as file:
            self.assertIn("人工裁决未应用", {row["action"] for row in csv.DictReader(file)})

    def test_analysis_uses_per_proxy_and_coordinate_quality(self):
        from app.analysis.loader import filter_analysis_data
        import pandas as pd
        frame = pd.DataFrame([
            {"Year": 2000, "lat": 18, "lon": 109, "Sr/Ca": 9, "d18O": -5, "qc_flag": 4,
             "qc_time_status": "pass", "qc_location_status": "pass",
             "Sr/Ca_value_qc_status": "pass", "δ18O_value_qc_status": "unit_conflict"},
            {"Year": 2001, "Sr/Ca": 9.1, "qc_flag": 4, "qc_time_status": "pass",
             "qc_location_status": "missing", "Sr/Ca_value_qc_status": "pass"},
            {"Year": 2002, "lat": 18, "lon": 109, "Sr/Ca": 90, "qc_flag": 3,
             "qc_time_status": "pass", "qc_location_status": "pass", "Sr/Ca_value_qc_status": "outside_range"},
        ])
        grid = filter_analysis_data(frame, require_coordinates=True)
        self.assertEqual(grid["Year"].tolist(), [2000])
        self.assertEqual(grid["Sr/Ca"].tolist(), [9])
        self.assertTrue(grid["d18O"].isna().all())
        self.assertEqual(filter_analysis_data(frame)["Year"].tolist(), [2000, 2001])

    def test_export_failure_does_not_publish_partial_batch(self):
        import pandas as pd
        records = [{"year": 2000, "Sr/Ca_value": 9, "hasResolution_nominal": "annual"}]
        self.workflow.processed_data = records
        actual_to_csv = pd.DataFrame.to_csv

        def failed_csv(dataframe, path, *args, **kwargs):
            if Path(path).name == "all_data_combined.csv":
                raise PermissionError("模拟汇总文件写入失败")
            return actual_to_csv(dataframe, path, *args, **kwargs)

        with patch.object(pd.DataFrame, "to_csv", failed_csv):
            with self.assertRaises(PermissionError):
                self.workflow.generate_final_output()
        self.assertEqual(list(Path(self._testMethodName).iterdir()), [])

    def test_unit_row_conflict_is_reviewed_without_losing_first_observation(self):
        path = Path("conflicting_unit_row.csv")
        path.write_text(
            "Year (CE),Sample ID,Sr/Ca [µmol/mol],δ18O [‰ VPDB]\n"
            "years CE,,mmol/mol,‰ VPDB\n2000,PM1,9000,-5\n", encoding="utf-8",
        )
        fields = {"Year (CE)": "year", "Sr/Ca [µmol/mol]": "Sr/Ca_value", "δ18O [‰ VPDB]": "δ18O_value"}
        response = json.dumps({"mapped_data": [
            {"original_header": header, "standard_field": field} for header, field in fields.items()
        ]})
        with patch("app.core.moa.llm_parser.call_llm", return_value=response):
            rows = self.workflow.process_file(str(path), paper_hash="unit_conflict")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sample_id"], "PM1")
        self.assertEqual(rows[0]["δ18O_value"], -5)
        self.assertIsNone(rows[0]["Sr/Ca_value"])
        self.assertEqual(rows[0]["Sr/Ca_value_qc_status"], "unit_conflict")
        self.assertEqual(rows[0]["Sr/Ca_value_original_value"], "9000")

    def test_cli_entry_propagates_publish_failure(self):
        import main
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path.cwd()
            os.chdir(directory)
            try:
                Path("pangaea").mkdir()
                with patch("app.core.moa.workflow.MOAWorkflow") as workflow:
                    workflow.return_value.process_directory.return_value = []
                    workflow.return_value.generate_final_output.side_effect = PermissionError("模拟发布失败")
                    with self.assertRaises(PermissionError):
                        main.run_moa_pipeline()
                    self.assertEqual(workflow.return_value.process_directory.call_args.kwargs["pangaea_dir"], "pangaea")
            finally:
                os.chdir(cwd)

    def test_local_delimited_formats_preserve_raw_cells(self):
        path = Path("locale.csv")
        path.write_bytes("Year CE;Sr/Ca (mmol/mol);样品编号\n2000;9,1;珊瑚A\n2001;9,25;珊瑚A\n".encode("gb18030"))
        with patch("app.core.moa.llm_parser.call_llm", return_value=""):
            rows = self.workflow.process_file(path)
        self.assertEqual([(row["year"], row["Sr/Ca_value"], row["Sr/Ca_value_original_value"], row["source_line"])
                          for row in rows], [(2000, 9.1, "9,1", 2), (2001, 9.25, "9,25", 3)])

    def test_stacked_sections_keep_local_units_samples_and_missing_values(self):
        path = Path("stacked.csv")
        path.write_text("Core A\nMissing value,-999\nYear CE,Sr/Ca (mmol/mol)\n2000,9.1\n2001,-999\n"
                        "\nCore B\nYear CE,Sr/Ca (μmol/mol)\n2002,9300\n", encoding="utf-8")
        with patch("app.core.moa.llm_parser.call_llm", return_value=""):
            rows = self.workflow.process_file(path)
        self.assertEqual([(row["sample_id"], row["year"], row["Sr/Ca_value"], row["source_line"])
                          for row in rows], [("A", 2000, 9.1, 4), ("B", 2002, 9.3, 9)])
        self.assertEqual([row["source_header_line"] for row in rows], [3, 8])

    def test_parallel_groups_split_samples_even_with_different_proxies(self):
        for name, table in (
            ("different_proxies", "Year CE,A1 Sr/Ca (mmol/mol),B1 δ18O (‰ VPDB)\n2000,9.1,-4.3\n"),
            ("group_headers", ",Core A1,,Core B1\nYear CE,Sr/Ca (mmol/mol),,δ18O (‰ VPDB)\n2000,9.1,,-4.3\n"),
        ):
            with self.subTest(layout=name), patch("app.core.moa.llm_parser.call_llm", return_value=""):
                path = Path(f"{name}.csv")
                path.write_text(table, encoding="utf-8")
                rows = self.workflow.process_file(path)
                self.assertEqual({(row["sample_id"], row.get("Sr/Ca_value"), row.get("δ18O_value"))
                                  for row in rows}, {("A1", 9.1, None), ("B1", None, -4.3)})

    def test_grouped_core_columns_keep_independent_absolute_chronologies(self):
        from docx import Document

        file = Path("parallel.docx")
        doc = Document()
        table = doc.add_table(rows=3, cols=4)
        for left, right, label in ((0, 1, "岩芯：A1"), (2, 3, "Core B1")):
            table.cell(0, left).merge(table.cell(0, right)).text = label
        for row, values in enumerate((("AD", "Sr/Ca (mmol/mol)", "AD", "δ18O (‰ VPDB)"),
                                      ("1971", "8.7", "2000", "-4.3")), 1):
            for column, value in enumerate(values):
                table.cell(row, column).text = value
        doc.save(file)
        with patch("app.core.moa.llm_parser.call_llm", return_value=""):
            points = self.workflow.process_file(file)
        self.assertEqual({(p["sample_id"], p["year"], p["source_time_column"]) for p in points},
                         {("A1", 1971, "AD"), ("B1", 2000, "AD [column 3]")})

    def test_published_composite_keeps_provenance_and_avoids_only_proven_overlap(self):
        import pandas as pd
        from app.analysis.loader import filter_analysis_data

        file = Path("published.csv")
        file.write_text("AD,Composite δ18O (‰ VPDB),Core A1 δ18O (‰ VPDB)\n2000,-4.1,-4.3\n", encoding="utf-8")
        with patch("app.core.moa.llm_parser.call_llm", return_value=""):
            points = self.workflow.process_file(file)
        records = {p["source_record_id"]: p for p in points}
        self.assertEqual(records["Composite δ18O (‰ VPDB)"]["record_type"], "published_composite")
        self.assertEqual(records["A1"]["δ18O_value"], -4.3)

        core = {"pub1_doi": "10.1234/article", "source_record_id": "A1", "time_resolution": "annual", "d18O": -4.3}
        composite = {**core, "Year": 2000, "source_record_id": "region", "d18O": -4.1,
                     "composite_evidence": json.dumps({"δ18O_value": "Composite of cores A1 (2m), and B1 (4m)"})}
        frame = pd.DataFrame([composite, {**core, "Year": 2000}, {**core, "Year": 2001},
                              {**core, "Year": 2000, "pub1_doi": "10.1234/other"},
                              {**core, "Year": 2000, "time_resolution": "monthly"},
                              {**core, "Year": 2000, "source_record_id": "A10"}])
        self.assertEqual(set(filter_analysis_data(frame).index), {0, 2, 3, 4, 5})
        self.assertEqual(frame.at[1, "d18O"], -4.3)

    def test_markdown_preserves_empty_cells_and_source_line(self):
        path = Path("table.md")
        path.write_text("# Supplement\n\n| Year CE | Sample ID | Sr/Ca (mmol/mol) |\n| --- | --- | --- |\n"
                        "| 2000 | | 9.1 |\n", encoding="utf-8")
        with patch("app.core.moa.llm_parser.call_llm", return_value=""):
            point, = self.workflow.process_file(path)
        self.assertEqual((point["source_header_line"], point["source_line"], point["Sr/Ca_value"]), (3, 5, 9.1))

    def test_explicit_symmetric_uncertainty_survives_measurement_and_csv_export(self):
        from app.utils.review_queue import get_review_items

        source = Path("measurement.csv")
        source.write_text("AD;Sr/Ca (µmol/mol)\n2000;9290,0 ± 80,0\n2001;<9000\n", encoding="utf-8")
        with patch("app.core.moa.llm_parser.call_llm", return_value=""):
            point, = self.workflow.process_file(source)
        self.assertEqual(point["Sr/Ca_value"], 9.29)
        self.assertEqual(point["Sr/Ca_value_uncertainty"], 0.08)
        self.assertEqual(point["Sr/Ca_value_original_value"], "9290,0 ± 80,0")
        record = self.workflow.data_output._csv_record(point, "annual_0001", "annual")
        self.assertEqual(record["Sr/Ca_value_original_uncertainty"], 80.0)
        self.assertEqual(record["Sr/Ca_value_uncertainty"], 0.08)
        self.assertTrue(any(row.get("source_value") == "<9000" for row in get_review_items()))

    def test_iso_dates_and_explicit_bp_reference_are_preserved(self):
        cases = (("Date", "2000-02-01", 2000 + 31 / 366, None),
                 ('"Age (yr BP, reference year 2000)"', "50", 1950, 2000))
        with patch("app.core.moa.llm_parser.call_llm", return_value=""):
            for index, (column, value, year, epoch) in enumerate(cases):
                path = Path(f"time_{index}.csv")
                path.write_text(f"{column},Sr/Ca (mmol/mol)\n{value},9.1\n", encoding="utf-8")
                point, = self.workflow.process_file(path)
                self.assertAlmostEqual(point["year"], year)
                self.assertEqual(point["source_time_value"], value)
                self.assertEqual(point["source_time_reference_year"], epoch)
            path = Path("time_conflict.csv")
            path.write_text("Year CE,Age (yr BP),Sr/Ca (mmol/mol)\n2000,100,9.1\n", encoding="utf-8")
            self.assertEqual(self.workflow.process_file(path), [])
            self.assertTrue(any("年代列不一致" in row["reason"] for row in self.workflow.source_inventory))

    def test_attachment_ages_use_source_precision_and_hold_conflicts(self):
        path = Path("precision.csv")
        path.write_text(
            "Year CE,Age (ka BP),Sr/Ca (mmol/mol)\n"
            "1751.417,0.199,9.1\n"
            "1752.4,0.1976005,9.2\n"
            "1753.205,0.1968,9.3\n"
            "1754.6250,0.1953700,9.4\n"
            "1755.20,0.198,9.5\n", encoding="utf-8",
        )
        with patch("app.core.moa.llm_parser.call_llm", return_value=""):
            rows = self.workflow.process_file(path)
        self.assertEqual([row["source_time_column"] for row in rows],
                         ["Year CE", "Age (ka BP)", "Year CE"])
        self.assertEqual(rows[0]["year"], 1751.417)
        self.assertAlmostEqual(rows[1]["year"], 1752.3995)
        self.assertEqual(rows[1]["source_time_value"], "0.1976005")
        self.assertEqual(rows[2]["year"], 1753.205)
        self.assertEqual([row["source_row"] for row in self.workflow.pending_reviews()
                          if "年代列不一致" in row["reason"]], [4, 5])

    def test_archive_ages_use_the_same_precision_rule(self):
        import pandas as pd

        result = self.workflow.database_parser.extract_standardized_data({
            "file_path": "archive.tab",
            "data": pd.DataFrame(
                [("1751.417", "0.199", "9.1"), ("1752.4", "0.1976005", "9.2"),
                 ("1754.6250", "0.1953700", "9.4")],
                columns=["Age CE", "Age ka BP", "Sr/Ca [mmol/mol]"], index=[11, 12, 13],
            ),
            "column_mapping": {"Age CE": "year", "Age ka BP": "age_ka_bp", "Sr/Ca [mmol/mol]": "Sr/Ca_value"},
        })
        self.assertEqual([row["source_line"] for row in result], [11, 12])
        self.assertEqual([row["source_time_column"] for row in result], ["Age CE", "Age ka BP"])
        self.assertEqual(result[1]["source_time_reference_year"], 1950)

    def test_zero_padding_requires_a_whole_column_match(self):
        from functools import partial
        from app.core.moa.spatiotemporal_standard import source_year_axis, verified_padded_year_step

        ce = ["1990.292", "1990.375", "1990.458", "1990.542"]
        bp = ["-0.040290", "-0.040380", "-0.040460", "-0.040540"]
        read_ce = partial(source_year_axis, basis="year")
        read_bp = partial(source_year_axis, basis="age_ka_bp", reference_year=1950)
        self.assertAlmostEqual(verified_padded_year_step(ce, bp, read_ce, read_bp), 0.01)
        self.assertIsNone(verified_padded_year_step(ce, [*bp[:3], "-0.040640"], read_ce, read_bp))

    def test_workbench_exports_source_scoped_decisions(self):
        import hashlib
        from app.review_workbench.sources import read_source
        from app.review_workbench.store import ReviewStore
        from app.utils.review_queue import load_unit_review_decisions, save_review_queue

        root = Path(self._testMethodName).resolve()
        archive, run = root / "noaa", root / "run"
        archive.mkdir(parents=True)
        files = [archive / "first.txt", archive / "second.txt"]
        text = "# NOAA Paleoclimatology\n" + "# Metadata\n" * 90 + (
            "## δ18O\tdelta 18O,coral,,per mil\n# Data:\n"
            "Date A\tDate B\tδ18O\n2000\t1990\t-4.1\n2001\t1991\t-4.2\n"
        )
        for file in files:
            file.write_text(text, encoding="utf-8")
        digest = hashlib.sha256(files[0].read_bytes()).hexdigest()
        first, second = ("noaa/" + file.name for file in files)
        unit = {"sheet": "first.txt", "column": "δ18O", "decision": "δ18O_value",
                "action": "暂缓该值", "reason": "源单位或同位素参考标尺缺失", "source_sha256": digest}
        save_review_queue(str(run), [
            {**unit, "file": first, "review_id": "r1", "source_line": "95"},
            {**unit, "file": first, "review_id": "r2", "source_line": "96"},
            {**unit, "file": second, "sheet": "second.txt", "review_id": "r3"},
            {"file": first, "column": "Date A | Date B", "decision": "year",
             "action": "暂缓表格", "reason": "存在多个无法区分的主年代列"},
            {"file": first, "action": "复核空间范围", "reason": "档案给出空间范围"},
        ])
        store = ReviewStore(run, root)
        cases = store.query("unit")["cases"]
        self.assertEqual([case["count"] for case in cases], [2, 1])
        raw = read_source(root, first, line="96", column="δ18O")
        self.assertEqual(raw["header_line"], 94)
        self.assertEqual(raw["highlight_line"], 96)
        self.assertEqual(raw["highlight_cols"], [2])
        body = {"id": cases[0]["id"], "scope": "group", "action": "confirm_unit", "unit": "‰ VPDB",
                "note": "核对源文件单位", "source_verified": True}
        self.assertEqual(store.save_decision(body)["saved"], 2)
        self.assertEqual(store.save_decision(body)["saved"], 0)
        self.assertEqual(store.query("unit")["total"], 1)
        time, = store.query("time")["cases"]
        coordinates, = store.query("location")["cases"]
        store.save_decision({**body, "id": time["id"], "scope": "item",
                             "action": "select_time_column", "selected_column": "Date A"})
        store.save_decision({**body, "id": coordinates["id"], "scope": "item",
                             "action": "confirm_coordinates", "lat": 0, "lon": 120})
        decisions = load_unit_review_decisions(store.decisions_path)
        self.assertEqual({entry["action"] for entry in decisions.values()},
                         {"confirm_unit", "select_time_column", "confirm_coordinates"})
        self.assertNotIn("r3", decisions)
        files[0].write_text(text + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "源文件版本已变化"):
            store.save_decision(body)

    def test_paper_review_requires_source_bound_evidence(self):
        import hashlib
        from app.review_workbench.paper_review import PaperReview, _paper_link
        from app.utils.review_queue import load_unit_review_decisions, save_review_queue

        root = Path(self._testMethodName).resolve()
        root.mkdir(exist_ok=True)
        raw = root / "observations.csv"
        raw.write_text("Year CE,d18O-HYD3\n2000,-4.2\n", encoding="utf-8")
        sha = hashlib.sha256(raw.read_bytes()).hexdigest()
        reason = "源单位或同位素参考标尺缺失"
        quote = "Porites coral d18O in HYD3 was measured and calibrated relative to VPDB."
        paper = root / "paper.pdf"
        paper.write_text(quote, encoding="utf-8")
        save_review_queue(str(root), [
            {"file": raw.name, "sheet": "default", "column": "d18O-HYD3", "action": "暂缓该值",
             "reason": reason, "decision": "δ18O_value", "review_id": "r1", "source_sha256": sha,
             "source_unit": "per mil"},
            {"file": raw.name, "sheet": "default", "column": "Year CE", "action": "暂缓该行",
             "reason": "同一观测的绝对年代列不一致", "decision": "Year CE=2005 | Age BP=-57", "source_row": "1"},
        ])
        review = PaperReview(root, root, rules_only=True)
        case, = (case for case in review.store.order if case["category"] == "unit")
        entry = review.prepare(case, raw)
        self.assertEqual(_paper_link(entry, "Coral δ18O–HYD3. 10.1234/example", True), "正文首页指标及岩芯编号")
        self.assertFalse(_paper_link(entry, "Coral δ18O–HYD4. 10.1234/example", True))
        source = {"id": "p1", "cases": [case["id"]], "file": "paper.pdf", "at": "PDF 第 3 页",
                  "text": quote, "kind": "paper", "bound": {}, "sha256": hashlib.sha256(paper.read_bytes()).hexdigest()}
        answer = {"action": "confirm_unit", "unit": "‰ VPDB", "evidence": [{"ref": "p1", "quote": quote}]}
        self.assertFalse(review._verify(entry, answer, {"p1": source}))
        source["bound"][case["id"]] = "DOI"
        # 绑定论文真实含样品+VPDB 声明时，确定性 _confirm_bound_isotope 不依赖模型引文逐字，直接接受
        self.assertTrue(review._verify(entry, {**answer, "evidence": [{"ref": "p1", "quote": quote + " extra"}]},
                                       {"p1": source}))
        self.assertTrue(review._verify(entry, answer, {"p1": source}))
        time, = (case for case in review.store.order if case["category"] == "time")
        self.assertFalse(review._verify(review.prepare(time, raw),
                                        {"action": "select_time_column", "column": "Year CE"}, {}))
        review.publish(1)
        saved = load_unit_review_decisions(root / "review_workbench" / "max_decisions.csv")
        self.assertEqual(saved["r1"]["unit"], "‰ VPDB")
        resumed = PaperReview(root, root, rules_only=True)
        resumed._restore()
        self.assertEqual(len(resumed.decisions), 1)
        paper.write_text(quote + " revised", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "引用的正文已变化"):
            load_unit_review_decisions(root / "review_workbench" / "max_decisions.csv")
        self.assertEqual(load_unit_review_decisions(root / "review_workbench" / "max_decisions.csv",
                                                    reuse_verified=True), {})

    def test_paper_review_accepts_bound_column_and_time_choices(self):
        import hashlib
        from app.review_workbench.paper_review import PaperReview
        from app.utils.review_queue import load_unit_review_decisions, save_review_queue

        root = Path(self._testMethodName).resolve()
        root.mkdir(exist_ok=True)
        source = root / "observations.csv"
        source.write_text("Year CE,Bulk_d13C,d13C_Ala\n2000,-4.2,-20.1\n", encoding="utf-8")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        save_review_queue(str(root), [{"file": source.name, "sheet": "default",
                                       "column": "Bulk_d13C | d13C_Ala", "decision": "δ13C_value",
                                       "action": "暂缓表格", "reason": "多个候选列无法确定唯一来源",
                                       "source_sha256": digest}])
        review = PaperReview(root, root, rules_only=True)
        case, = review.store.order
        self.assertEqual(case["action"], "select_column")
        entry = review.prepare(case, source)
        quote = "The column Bulk_d13C represents bulk skeletal carbon isotope δ13C values."
        paper_file = root / "paper.pdf"
        paper_file.write_text(quote, encoding="utf-8")
        paper = {"id": "p1", "cases": [case["id"]], "file": "paper.pdf", "at": "PDF 第 2 页",
                 "text": quote, "kind": "paper", "bound": {case["id"]: "同一论文目录"},
                 "sha256": hashlib.sha256(paper_file.read_bytes()).hexdigest()}
        answer = {"action": "select_column", "target": "δ13C_value", "column": "Bulk_d13C",
                  "evidence": [{"ref": "p1", "quote": quote}]}
        self.assertFalse(review._verify(entry, {**answer, "column": "NotACandidate"}, {"p1": paper}))
        self.assertFalse(review._verify(entry, answer, {"p1": {**paper, "bound": {}}}))
        self.assertTrue(review._verify(entry, answer, {"p1": paper}))
        review.publish(1)
        saved = load_unit_review_decisions(root / "review_workbench" / "max_decisions.csv")
        decision, = saved.values()
        self.assertEqual((decision["action"], decision["selected"], decision["target"]),
                         ("select_column", "Bulk_d13C", "δ13C_value"))

        time_source = root / "chronology.csv"
        time_source.write_text("Age (AD),Top age (AD),d18O\n1850,1832,-4.2\n", encoding="utf-8")
        time_digest = hashlib.sha256(time_source.read_bytes()).hexdigest()
        save_review_queue(str(root), [{"file": time_source.name, "sheet": "default",
                                       "column": "Age (AD) | Top age (AD)", "decision": "year",
                                       "action": "暂缓表格", "reason": "存在多个无法区分的主年代列",
                                       "source_sha256": time_digest}])
        review = PaperReview(root, root, rules_only=True)
        case, = (case for case in review.store.order if case["file"] == time_source.name)
        self.assertEqual(case["action"], "select_time_column")
        entry = review.prepare(case, time_source)
        quote = "The Top age (AD) column gives the calendar year of storm deposition for each coral block."
        paper_file = root / "paper2.pdf"
        paper_file.write_text(quote, encoding="utf-8")
        paper = {"id": "p1", "cases": [case["id"]], "file": "paper2.pdf", "at": "PDF 第 4 页",
                 "text": quote, "kind": "paper", "bound": {case["id"]: "同一论文目录"},
                 "sha256": hashlib.sha256(paper_file.read_bytes()).hexdigest()}
        self.assertTrue(review._verify(entry, {"action": "select_time_column", "target": "year",
                                               "column": "Top age (AD)",
                                               "evidence": [{"ref": "p1", "quote": quote}]}, {"p1": paper}))

    def test_paper_review_accepts_verbatim_site_coordinates_within_bounds(self):
        import hashlib
        import json as jsonlib
        from app.review_workbench.paper_review import PaperReview
        from app.utils.review_queue import load_unit_review_decisions, save_review_queue

        root = Path(self._testMethodName).resolve()
        archive = root / "noaa"
        archive.mkdir(parents=True)
        source = archive / "site.txt"
        source.write_text("# NOAA Paleoclimatology\n"
                          "# Northernmost_Latitude: 18.4\n# Southernmost_Latitude: 18.3\n"
                          "# Easternmost_Longitude: -64.8\n# Westernmost_Longitude: -64.9\n"
                          "# Data:\nYear CE\td18O\n2000\t-4.2\n", encoding="utf-8")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        bounds = {"Northernmost_Latitude": 18.4, "Southernmost_Latitude": 18.3,
                  "Easternmost_Longitude": -64.8, "Westernmost_Longitude": -64.9}
        save_review_queue(str(root), [{"file": str(source), "column": "", "action": "复核空间范围",
                                       "reason": "档案给出空间范围，代表点待核对，精确坐标留空",
                                       "source_sha256": digest, "geo_bounds": jsonlib.dumps(bounds)}])
        review = PaperReview(root, root, rules_only=True)
        case, = review.store.order
        self.assertEqual(case["action"], "confirm_coordinates")
        entry = review.prepare(case, source)
        quote = "The coral colony was collected at 18.35 N, -64.82 W on the reef flat."
        paper_file = archive / "paper.pdf"
        paper_file.write_text(quote, encoding="utf-8")
        paper = {"id": "p1", "cases": [case["id"]], "file": str(paper_file), "at": "PDF 第 3 页",
                 "text": quote, "kind": "paper", "bound": {case["id"]: "同一论文目录"},
                 "sha256": hashlib.sha256(paper_file.read_bytes()).hexdigest()}
        answer = {"action": "confirm_coordinates", "lat": 18.35, "lon": -64.82,
                  "evidence": [{"ref": "p1", "quote": quote}]}
        self.assertFalse(review._verify(entry, {**answer, "lat": 19.0}, {"p1": paper}))
        self.assertFalse(review._verify(entry, answer, {"p1": {**paper, "bound": {}}}))
        self.assertFalse(review._verify(entry, {**answer, "lat": None}, {"p1": paper}))
        self.assertTrue(review._verify(entry, answer, {"p1": paper}))
        review.publish(1)
        saved = load_unit_review_decisions(root / "review_workbench" / "max_decisions.csv")
        decision, = saved.values()
        self.assertEqual((decision["action"], float(decision["lat"]), float(decision["lon"])),
                         ("confirm_coordinates", 18.35, -64.82))

    def test_paper_review_unit_scale_accepts_unit_row_and_generic_method(self):
        import hashlib
        import json as jsonlib
        from app.review_workbench.paper_review import PaperReview
        from app.utils.review_queue import save_review_queue

        root = Path(tempfile.mkdtemp(prefix="unit-relax-"))
        # 源列名不带单位，‰ 声明来自单位行证据；论文通用方法声明不点名样品也不含 coral 字样
        evidence = jsonlib.dumps([{"source": "unit_row", "label": "per mil", "unit": "‰"}])
        method = ("Oxygen isotope ratios were measured by isotope ratio mass spectrometry. "
                  "The δ18O values are reported in per mil relative to VPDB.")
        cases = {
            "generic_method": ((method,), "verified"),
            "seawater_guard": (("Seawater δ18O was measured and reported in per mil relative to VPDB.",), "pending"),
            "no_scale": (("The δ18O values are reported in per mil.",), "pending"),
        }
        for name, (pages, state) in cases.items():
            with self.subTest(name=name):
                study = root / name
                study.mkdir(exist_ok=True)
                source = study / "observations.csv"
                source.write_text("Year CE,d18O\n2000,-4.2\n2001,-4.5\n", encoding="utf-8")
                digest = hashlib.sha256(source.read_bytes()).hexdigest()
                rows = [{"file": str(source), "sheet": "default", "column": "d18O", "decision": "δ18O_value",
                         "source_unit": "", "unit_evidence": evidence, "source_sha256": digest,
                         "action": "暂缓该值", "reason": "源单位或同位素参考标尺缺失",
                         "review_id": core, "sample_id": core} for core in ("A01", "B02")]
                save_review_queue(str(study), rows)
                (study / "paper.pdf").write_text(pages[0], encoding="utf-8")
                review = PaperReview(study, root, rules_only=True)
                with (patch("app.review_workbench.paper_review._pdf_pages",
                            return_value=tuple(enumerate(pages, 1))),
                      patch.object(review, "_ask") as model):
                    summary = review.run(all_rules=True)
                model.assert_not_called()
                self.assertEqual(summary["cases"], {state: 1})

        # NOAA 档案定义缺少 material/data_type 字段时，‰ 声明+通用方法同样可确认
        archive = root / "noaa_archive"
        archive.mkdir()
        noaa = archive / "observations.txt"
        noaa.write_text("# NOAA Paleoclimatology\n# Variables\n"
                        "## d18O\tdelta 18O,,,per mil,,,,,N,\n"
                        "# Data:\nYear CE\td18O\n2000\t-4.2\n", encoding="utf-8")
        noaa_digest = hashlib.sha256(noaa.read_bytes()).hexdigest()
        (archive / "paper.pdf").write_text(method, encoding="utf-8")
        run = root / "noaa_run"
        save_review_queue(str(run), [{"file": str(noaa), "sheet": noaa.name, "column": "d18O",
                                      "decision": "δ18O_value", "source_unit": "per mil",
                                      "source_sha256": noaa_digest, "action": "暂缓该值",
                                      "reason": "源单位或同位素参考标尺缺失", "review_id": "r1"}])
        review = PaperReview(run, root, rules_only=True)
        with (patch("app.review_workbench.paper_review._pdf_pages", return_value=((1, method),)),
              patch.object(review, "_ask") as model):
            summary = review.run(all_rules=True)
        model.assert_not_called()
        self.assertEqual(summary["cases"], {"verified": 1})

    def test_reviewed_representatives_resolve_anomalous_bounds(self):
        from app.core.moa.database_parser import UnifiedDatabaseParser
        from app.utils.review_queue import capture_review_items

        parser = UnifiedDatabaseParser()
        swapped = {"Southernmost_Latitude": 24.6, "Northernmost_Latitude": 24.7,
                   "Westernmost_Longitude": -82.7, "Easternmost_Longitude": -82.9}
        crossing = {"Southernmost_Latitude": -10.0, "Northernmost_Latitude": 10.0,
                    "Westernmost_Longitude": 175.0, "Easternmost_Longitude": -85.0}
        for name, bounds, lon, span in (("swap", swapped, -82.8, 0.2), ("cross", crossing, -135.0, 100.0)):
            with self.subTest(name=name):
                parser.noaa_reviewed_representatives = {
                    "10.1234/test|rec1": {
                        "bounds_south_north_west_east": [bounds["Southernmost_Latitude"],
                                                         bounds["Northernmost_Latitude"],
                                                         bounds["Westernmost_Longitude"],
                                                         bounds["Easternmost_Longitude"]],
                        "evidence": "核验：区域复合，边界顺序异常已确认"}}
                meta = {"geo_bounds": bounds, "dataset_doi": "10.1234/test", "source_record_id": "rec1"}
                with capture_review_items() as items:
                    parser._resolve_noaa_location(meta, f"noaa/{name}.txt")
                self.assertEqual(meta["geo_rep_status"], "reviewed_regional_composite")
                self.assertAlmostEqual(meta["geo_rep_longitude"], lon, places=6)
                self.assertAlmostEqual(meta["geo_lon_span"], span, places=6)
                self.assertFalse([i for i in items if i.get("action") == "复核空间范围"])
        parser.noaa_reviewed_representatives = {}
        meta = {"geo_bounds": swapped, "dataset_doi": "10.1234/test", "source_record_id": "rec1"}
        with capture_review_items() as items:
            parser._resolve_noaa_location(meta, "noaa/none.txt")
        self.assertTrue(any(i.get("action") == "复核空间范围" for i in items))

    def test_coordinate_gate_accepts_hemisphere_and_integer_notation(self):
        import hashlib
        from app.review_workbench.paper_review import PaperReview
        from app.utils.review_queue import save_review_queue

        root = Path(tempfile.mkdtemp(prefix="coord-hemi-"))
        archive = root / "noaa"
        archive.mkdir()
        source = archive / "site.txt"
        source.write_text("# NOAA Paleoclimatology\n# Data:\nYear CE\td18O\n2000\t-4.2\n", encoding="utf-8")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        save_review_queue(str(root), [{"file": str(source), "column": "", "action": "复核空间范围",
                                       "reason": "档案给出空间范围，代表点待核对，精确坐标留空",
                                       "source_sha256": digest}])
        review = PaperReview(root, root, rules_only=True)
        case, = review.store.order
        entry = review.prepare(case, source)
        for quote, lat, lon, expected in (
                ("The colony was collected at 36.697°N, 122.378°W on the reef flat.", 36.697, -122.378, True),
                ("The colony was collected at 36.697°N, 122.378°W on the reef flat.", 36.697, 122.378, False),
                ("Samples were collected at 18 N, 120 E.", 18, 120, True),
                ("Samples were collected at 18 S, 120 W.", -18, -120, True),
                ("Sinking particles were collected at station M2 (36.697(cid:1)N, 122.378(cid:1)W; Fig. 1).",
                 36.697, -122.378, True)):
            with self.subTest(quote=quote[:30], lat=lat, lon=lon):
                paper_file = archive / "paper.pdf"
                paper_file.write_text(quote, encoding="utf-8")
                paper = {"id": "p1", "cases": [case["id"]], "file": str(paper_file), "at": "PDF 第 3 页",
                         "text": quote, "kind": "paper", "bound": {case["id"]: "同一论文目录"},
                         "sha256": hashlib.sha256(paper_file.read_bytes()).hexdigest()}
                review.decisions = {}
                self.assertEqual(review._verify(entry, {"action": "confirm_coordinates", "lat": lat, "lon": lon,
                                                        "evidence": [{"ref": "p1", "quote": quote}]},
                                                {"p1": paper}), expected)

    def test_publish_keeps_history_decisions_when_findings_go_stale(self):
        import hashlib
        from app.review_workbench.paper_review import PaperReview
        from app.review_workbench.store import DECISION_FIELDS, _save_csv
        from app.utils.review_queue import load_unit_review_decisions, save_review_queue

        root = Path(tempfile.mkdtemp(prefix="publish-merge-"))
        source = root / "observations.csv"
        source.write_text("Year CE,d18O-HYD3\n2000,-4.2\n", encoding="utf-8")
        sha = hashlib.sha256(source.read_bytes()).hexdigest()
        save_review_queue(str(root), [{"file": source.name, "sheet": "default", "column": "d18O-HYD3",
                                       "action": "暂缓该值", "reason": "源单位或同位素参考标尺缺失",
                                       "decision": "δ18O_value", "review_id": "r1", "source_sha256": sha,
                                       "source_unit": "per mil"}])
        # 模拟历史 verified 裁决表（无同目录论文，本轮无法重新核验）
        history = {"review_id": "r1", "file": str(source.resolve()), "sheet": "default", "column": "",
                   "source_sha256": sha, "decision": "δ18O_value", "review_decision": "confirm_unit",
                   "confirmed_unit": "‰ VPDB", "review_note": "历史论文证据已核验", "evidence_status": "verified",
                   "decision_origin": ""}
        _save_csv(root / "review_workbench" / "max_decisions.csv", DECISION_FIELDS, [history])
        review = PaperReview(root, root, rules_only=True)
        summary = review.run(all_rules=True)
        self.assertEqual(summary["cases"], {"pending": 1})
        # 版本失效/无法复核不得清空历史裁决
        self.assertEqual(summary["decision_rows"], 1)
        saved = load_unit_review_decisions(root / "review_workbench" / "max_decisions.csv",
                                           reuse_verified=True, source_root=root)
        self.assertEqual(saved["r1"]["unit"], "‰ VPDB")

    def test_pangaea_speleothem_source_is_excluded_but_reference_mention_is_not(self):
        from app.core.moa.database_parser import UnifiedDatabaseParser

        parser = UnifiedDatabaseParser()
        base = ["/* DATA DESCRIPTION:",
                "Citation:\tAuthors (2025): {title}. https://doi.org/10.1594/PANGAEA.99999",
                "Keyword(s):\tmagnesium; SST",
                "Event(s):\tA * LATITUDE: 32 * LONGITUDE: -64",
                "*/",
                "Age [a AD/CE]\tSST [°C]",
                "2000\t24.9",
                "2001\t25.1"]
        stalagmite = Path("stalagmite.tab")
        stalagmite.write_text("\n".join(base).format(title="Annual stalagmite-derived SSTs"), encoding="utf-8")
        self.assertEqual(parser.parse_file(str(stalagmite))["skip_reason"], "not_coral_data")
        coral = Path("coral.tab")
        text = "\n".join(base).format(title="Coral Sr/Ca record").replace(
            "Keyword(s):\tmagnesium; SST",
            "Keyword(s):\tcoral; SST\nReferences:\tSmith (2020): Stalagmite dating methods.")
        coral.write_text(text, encoding="utf-8")
        with patch("app.core.moa.llm_parser.call_llm", return_value=""):
            self.assertNotEqual(parser.parse_file(str(coral)).get("skip_reason"), "not_coral_data")

    def test_excluded_table_leaves_queue_trace_and_excluded_status(self):
        from app.core.moa.workflow import MOAWorkflow
        from app.utils.review_queue import clear_review_items, get_review_items

        root = Path(self._testMethodName).resolve()
        root.mkdir(exist_ok=True)
        source = root / "stalagmite.tab"
        source.write_text("\n".join([
            "/* DATA DESCRIPTION:",
            "Citation:\tAuthors (2025): Annual stalagmite-derived SSTs. https://doi.org/10.1594/PANGAEA.99999",
            "Keyword(s):\tstalagmite; SST",
            "Event(s):\tA * LATITUDE: 32 * LONGITUDE: -64",
            "*/",
            "Age [a AD/CE]\tSST [°C]",
            "2000\t24.9",
            "2001\t25.1"]), encoding="utf-8")
        workflow = MOAWorkflow(output_dir=str(root))
        clear_review_items()
        self.assertEqual(workflow.process_file(str(source)), [])
        trace = [it for it in get_review_items() if it.get("action") == "已排除非观测片段"]
        self.assertEqual(len(trace), 1)
        self.assertIn("not_coral_data", trace[0]["reason"])
        self.assertIn("stalagmite", trace[0]["reason"])  # 排除证据（skip_evidence）随留痕
        file_rows = [row for row in workflow.source_inventory if not row["sheet"]]
        self.assertEqual(file_rows[0]["status"], "excluded")
        clear_review_items()

    def test_file_level_exclude_removes_source_via_patch(self):
        import hashlib
        import main
        from app.core.config import settings
        from app.core.moa.patch_apply import apply_patch
        from app.review_workbench.store import DECISION_FIELDS, _save_csv
        from app.utils.review_queue import load_unit_review_decisions

        cwd = Path.cwd()
        with tempfile.TemporaryDirectory(prefix="file-exclude-") as staging:
            try:
                os.chdir(staging)
                for name, value in (("keep", "8.9"), ("drop", "7.7")):
                    source = Path(f"dataset/{name}/data.csv")
                    source.parent.mkdir(parents=True)
                    source.write_text(f"Year CE,Sr/Ca (mmol/mol)\n2000,{value}\n2001,9.1\n", encoding="utf-8")
                with (patch.object(settings, "LLM_API_KEY", "fixture"),
                      patch.object(settings, "JUDGE_BACKEND", "off"),
                      patch("app.core.moa.llm_parser.call_llm", return_value="")):
                    batch = Path(main.run_moa_pipeline())
                with (batch / "consolidated/all_data_combined.csv").open(encoding="utf-8-sig", newline="") as stream:
                    self.assertEqual(len(list(csv.DictReader(stream))), 4)
                drop = Path("dataset/drop/data.csv").resolve()
                row = {"file": str(drop), "sheet": "", "column": "", "decision": "",
                       "source_sha256": hashlib.sha256(drop.read_bytes()).hexdigest(),
                       "review_decision": "exclude", "confirmed_unit": "",
                       "review_note": "整文件非珊瑚观测（测试）", "evidence_status": "verified",
                       "decision_origin": "manual"}
                table = Path("file_excludes.csv")
                _save_csv(table, DECISION_FIELDS, [row])
                bad = Path("bad_excludes.csv")
                _save_csv(bad, DECISION_FIELDS, [{**row, "source_sha256": ""}])
                with self.assertRaisesRegex(ValueError, "文件级排除需要源文件 SHA-256"):
                    load_unit_review_decisions(bad)
                new_batch = Path(apply_patch(str(batch), review_decisions=str(table)))
                with (new_batch / "consolidated/all_data_combined.csv").open(encoding="utf-8-sig", newline="") as stream:
                    rows = list(csv.DictReader(stream))
                self.assertEqual(len(rows), 2)
                self.assertTrue(all("keep" in r["source_file"] for r in rows))
                summary = json.loads((new_batch / "incremental_summary.json").read_text(encoding="utf-8"))
                self.assertIn("drop", " ".join(summary["updated_sources"]))
            finally:
                os.chdir(cwd)

    def test_coverage_audit_scope_classification_guards(self):
        from app.core.moa.coverage_audit import classify_scope, _extract_quantities

        self.assertEqual(classify_scope("Sr/Ca (mmol/mol) year")[0], "in_schema")
        self.assertEqual(classify_scope("Sr Ca year")[0], "in_schema")  # 关键词门漏判的空间变体
        self.assertEqual(classify_scope("Delta 14C Porites")[0], "exclude_out_of_schema")
        self.assertEqual(classify_scope("boron isotopes d11B")[0], "exclude_out_of_schema")
        self.assertEqual(classify_scope("linear extension rate growth")[0], "exclude_out_of_scope")
        self.assertEqual(classify_scope("speleothem stalagmite")[0], "exclude_non_coral")
        self.assertEqual(classify_scope("User_Signal_Intensity Battery_Voltage")[0], "ambiguous")
        self.assertEqual(classify_scope("Sr/Ca and d11B")[0], "in_schema")  # in-schema 优先
        # 精度守卫：只匹配实测变量标识，避开校正注记/论文标题中的 incidental 词
        druffel = ("# Variables\n## D14C\tDelta 14C,Porites,,per mil,,corals,corrected,AMS,N,"
                   "corrected for a delta 13C of -25 per mil PDB\n")
        self.assertEqual(classify_scope(_extract_quantities(druffel))[0], "exclude_out_of_schema")
        kuhnert = ("Citation: Sea-surface temperature variability inferred from coral records\n"
                   "Parameter(s):\tDISTANCE [cm] (Distance) * Age model [ka] (Age model) * Age [a AD/CE] (Age)\n")
        self.assertNotEqual(classify_scope(_extract_quantities(kuhnert))[0], "in_schema")
        # 硬非珊瑚材料优先于指标：有孔虫 Mg/Ca、牡蛎壳即便含指标也排除
        self.assertEqual(classify_scope("Mg/Ca Uvigerina foraminifer")[0], "exclude_non_coral")
        self.assertEqual(classify_scope("Sr/Ca oyster Isognomon shell")[0], "exclude_non_coral")
        foram_noaa = "# Variables\n## Mg/Ca\tmagnesium calcium,Uvigerina spp.,,mmol/mol\n"
        self.assertEqual(classify_scope(_extract_quantities(foram_noaa))[0], "exclude_non_coral")

    def test_coverage_audit_reconciles_folders(self):
        from app.core.moa.coverage_audit import audit_folder

        root = Path(tempfile.mkdtemp(prefix="coverage-audit-"))

        def mk(folder, name, content):
            path = root / folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return {"file": f"{folder}/{name}", "status": "excluded", "reason": ""}

        srca = "## Sr/Ca\tstrontium calcium,Porites,,mmol/mol\n## year_CE\tage,,,year Common Era\n"
        # 已覆盖：源有 Sr/Ca+年代，产出已覆盖
        f1 = mk("noaa/covered", "obs.txt", srca)
        r1 = audit_folder("noaa/covered", [f1], root,
                          {"noaa/covered": {"rows": 10, "proxies": {"Sr/Ca"}, "year": 10, "core": 0}})
        self.assertEqual(r1["disposition"], "covered")
        # 不相干：生长率（无目标指标）
        f2 = mk("noaa/growth", "data.tab", "growth rate\tlinear extension\n2000\t1.2\n")
        r2 = audit_folder("noaa/growth", [f2], root, {})
        self.assertEqual(r2["disposition"], "irrelevant")
        self.assertEqual(r2["scope"], "exclude_out_of_scope")
        # 不相干：有孔虫 Mg/Ca（硬非珊瑚优先，含指标也排除）
        f3 = mk("noaa/foram", "obs.txt", "## Mg/Ca\tmagnesium calcium,Uvigerina spp.,,mmol/mol\n")
        r3 = audit_folder("noaa/foram", [f3], root, {})
        self.assertEqual(r3["disposition"], "irrelevant")
        self.assertEqual(r3["scope"], "exclude_non_coral")
        # 缺口：源有 Sr/Ca 但零产出
        f4 = mk("noaa/gap", "obs.txt", srca)
        r4 = audit_folder("noaa/gap", [f4], root, {})
        self.assertEqual(r4["disposition"], "gap")
        self.assertTrue(any(g["type"] == "zero_output" for g in r4["gaps"]))
        # 缺口：产出有 Sr/Ca 但缺年代（月份格式/年代基准类）
        r5 = audit_folder("noaa/gap", [f4], root,
                          {"noaa/gap": {"rows": 10, "proxies": {"Sr/Ca"}, "year": 0, "core": 0}})
        self.assertTrue(any(g["type"] == "chronology" for g in r5["gaps"]))
        # 缺口：源有岩芯标识但产出缺 core_id（并列岩芯类）
        f6 = mk("noaa/coregap", "obs.txt",
                "## Sr/Ca\tstrontium calcium,Porites,,mmol/mol\n## core_id\tcore identification,,,,,corals,,,\n"
                "## year_CE\tage,,,year Common Era\n")
        r6 = audit_folder("noaa/coregap", [f6], root,
                          {"noaa/coregap": {"rows": 10, "proxies": {"Sr/Ca"}, "year": 10, "core": 0}})
        self.assertTrue(any(g["type"] == "core_id" for g in r6["gaps"]))
        # 混合：生长率文件夹里另有 Sr/Ca 附件 → 按该表证据处理，不整目录排除
        f7a = mk("zenodo/mixed", "growth.csv", "growth rate,extension\n1.2,3\n")
        f7b = mk("zenodo/mixed", "srca.csv", "year,Sr/Ca\n2000,8.9\n")
        r7 = audit_folder("zenodo/mixed", [f7a, f7b], root, {})
        self.assertEqual(r7["disposition"], "gap")
        self.assertIn("Sr/Ca", r7["source_proxies"])
        # 产出已有目标指标但源表头扫描漏检 → 仍判 covered，不误判 irrelevant
        f8 = mk("noaa/scanmiss", "data.tab", "opaque header without proxy token\n1\t2\n")
        r8 = audit_folder("noaa/scanmiss", [f8], root,
                          {"noaa/scanmiss": {"rows": 5, "proxies": {"Sr/Ca"}, "year": 5, "core": 0}})
        self.assertEqual(r8["disposition"], "covered")

    def test_coverage_audit_run_emits_ledger_and_excludes(self):
        from app.core.moa.coverage_audit import audit_run
        from app.utils.review_queue import load_unit_review_decisions

        root = Path(tempfile.mkdtemp(prefix="audit-run-"))
        srca = "## Sr/Ca\tstrontium calcium,Porites,,mmol/mol\n## year_CE\tage,,,year Common Era\n"
        sources = {"noaa/covered/obs.txt": srca,
                   "noaa/growth/data.tab": "growth rate\tlinear extension\n2000\t1.2\n"}
        manifest = []
        for rel, content in sources.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            manifest.append({"file": rel, "sheet": "", "status": "excluded", "input_rows": "1",
                             "source_rows_with_output": "0", "output_rows": "0", "review_items": "0",
                             "reason": "档案文件头未发现受支持指标线索"})
        run = root / "run"
        (run / "consolidated").mkdir(parents=True)
        with (run / "input_manifest.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(manifest[0]))
            writer.writeheader()
            writer.writerows(manifest)
        with (run / "consolidated/all_data_combined.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["source_file", "year_CE", "Sr/Ca (mmol/mol)"])
            writer.writeheader()
            writer.writerow({"source_file": "noaa/covered/obs.txt", "year_CE": "2000", "Sr/Ca (mmol/mol)": "8.9"})
        summary = audit_run(str(run), source_root=str(root),
                            folders=["noaa/covered", "noaa/growth"], use_flash=False)
        self.assertEqual(summary["dispositions"]["covered"], 1)
        self.assertEqual(summary["dispositions"]["irrelevant"], 1)
        self.assertTrue(Path(summary["ledger_csv"]).is_file())
        decisions = load_unit_review_decisions(summary["decisions_csv"])
        self.assertTrue(all(k[0] == "FILE_EXCLUDE" for k in decisions))
        self.assertEqual(len(decisions), 1)  # 生长率附件被证据化排除
        reused = load_unit_review_decisions(summary["decisions_csv"], reuse_verified=True, source_root=str(root))
        self.assertEqual(len(reused), 1)

    def test_coverage_audit_recover_bypasses_keyword_gate(self):
        import hashlib
        from app.core.moa.workflow import MOAWorkflow
        from app.utils.review_queue import load_unit_review_decisions

        root = Path(tempfile.mkdtemp(prefix="recover-gate-"))
        archive = root / "noaa" / "srca"
        archive.mkdir(parents=True)
        source = archive / "obs.txt"
        # "Sr Ca" 空间变体：CORE_PROXY_TOKENS 认得，但 PROXY_KEYWORDS 漏判 → 关键词门排除
        source.write_text("# NOAA Paleoclimatology\n# Variables\n"
                          "## Sr Ca\tstrontium calcium,Porites sp.,,mmol/mol,,corals,,ICP-MS,N,\n"
                          "# Data:\nYear CE\tSr Ca\n2000\t8.9\n2001\t9.1\n", encoding="utf-8")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        cwd = Path.cwd()
        from app.core.config import settings
        try:
            os.chdir(root)
            (root / "dataset").mkdir()
            with (patch.object(settings, "LLM_API_KEY", "fixture"),
                  patch.object(settings, "JUDGE_BACKEND", "off"),
                  patch("app.core.moa.llm_parser.call_llm", return_value="")):
                workflow = MOAWorkflow(output_dir=str(root / "out"), enable_calibration_check=False)
                self.assertFalse(workflow._is_relevant_proxy_file(str(source)))  # 关键词门确实漏判
                points = workflow.process_directory("dataset", noaa_dir="noaa")
                self.assertEqual(points, [])  # 无裁决 → 被关键词门排除
                row = {"file": str(source), "sheet": "", "column": "", "decision": "",
                       "source_sha256": digest, "review_decision": "recover", "confirmed_unit": "",
                       "review_note": "含 Sr/Ca（关键词预筛漏判），回收", "evidence_status": "verified",
                       "decision_origin": "scope_triage"}
                table = root / "recover.csv"
                from app.review_workbench.store import DECISION_FIELDS, _save_csv
                _save_csv(table, DECISION_FIELDS, [row])
                bad = root / "bad.csv"
                _save_csv(bad, DECISION_FIELDS, [{**row, "source_sha256": ""}])
                with self.assertRaisesRegex(ValueError, "文件级回收需要源文件 SHA-256"):
                    load_unit_review_decisions(bad)
                decisions = load_unit_review_decisions(table)
                self.assertIn(("FILE_RECOVER", os.path.normcase(str(source))), decisions)
                workflow2 = MOAWorkflow(output_dir=str(root / "out2"), enable_calibration_check=False,
                                        review_decisions=decisions)
                points2 = workflow2.process_directory("dataset", noaa_dir="noaa")
                self.assertTrue(points2)  # 回收裁决绕过关键词门 → 成功提取
                self.assertTrue(any(p.get("Sr/Ca_value") is not None for p in points2))
        finally:
            os.chdir(cwd)

    def test_month_year_parsing_unique_constraint_and_pending(self):
        import glob
        from datetime import date
        from app.core.moa.spatiotemporal_standard import (
            month_year_date, resolve_two_digit_year, calendar_date_order, year_ce)
        from app.core.moa.workflow import MOAWorkflow
        from app.core.config import settings

        # 两位年份由档案起止年唯一约束
        self.assertEqual(month_year_date("Aug-07", (2007, 2013)), date(2007, 8, 1))
        self.assertEqual(month_year_date("Aug-87", (1987, 2012)), date(1987, 8, 1))
        self.assertAlmostEqual(year_ce("Aug-07", "year", year_range=(2007, 2013)), 2007 + 212 / 365, places=3)
        # 证据不足/多候选→待核（None），不猜
        self.assertIsNone(month_year_date("Aug-07", (1900, 2010)))  # 1907 与 2007 均在区间
        self.assertIsNone(month_year_date("Aug-07", None))
        self.assertIsNone(resolve_two_digit_year(7, (None, 2013)))
        self.assertEqual(calendar_date_order(["Aug-07", "Oct-07"], (2007, 2013)), "month_year")

        # 集成：NOAA Mon-YY 表按起止年唯一解析入库
        root = Path(tempfile.mkdtemp(prefix="month-year-"))
        archive = root / "noaa" / "mon"
        archive.mkdir(parents=True)
        source = archive / "obs.txt"
        source.write_text(
            "# NOAA Paleoclimatology\n# First_Year: 2007\n# Last_Year: 2013\n# Variables\n"
            "## age_CE\tage,,,year Common Era,,corals and sclerosponges,,,C,\n"
            "## Sr/Ca\tstrontium/calcium,Porites sp.,,millimole per mole,,corals and sclerosponges,,ICP-MS,N,\n"
            "# Data:\nage_CE\tSr/Ca\nAug-07\t9.0\nOct-07\t8.9\n", encoding="utf-8")
        cwd = Path.cwd()
        try:
            os.chdir(root)
            (root / "dataset").mkdir()
            with (patch.object(settings, "LLM_API_KEY", "fixture"),
                  patch.object(settings, "JUDGE_BACKEND", "off"),
                  patch("app.core.moa.llm_parser.call_llm", return_value="")):
                workflow = MOAWorkflow(output_dir=str(root / "out"), enable_calibration_check=False)
                points = workflow.process_directory("dataset", noaa_dir="noaa")
                self.assertEqual(len(points), 2)
                self.assertEqual(points[0]["source_time_value"], "Aug-07")
                self.assertAlmostEqual(points[0]["year"], 2007 + 212 / 365, places=3)
                self.assertEqual(points[0]["Sr/Ca_value"], 9.0)
                self.assertEqual(points[0]["Sr/Ca_value_unit"], "mmol/mol")
                self.assertEqual(points[0]["source_time_date_order"], "month_year")
        finally:
            os.chdir(cwd)

    def test_parallel_core_split_composite_and_multi_version(self):
        import json as _json
        from app.core.moa.workflow import MOAWorkflow
        from app.core.moa.context_extractor import named_composite_cores
        from app.core.config import settings

        # 合成声明无 cores 关键词也能解析组成岩芯
        self.assertEqual(named_composite_cores("composite of STM4 and STM2; interpolated"), ("stm4", "stm2"))
        self.assertEqual(named_composite_cores("composite of the records"), ())  # 散文防伪匹配

        root = Path(tempfile.mkdtemp(prefix="parallel-core-"))
        archive = root / "noaa" / "grove"
        archive.mkdir(parents=True)
        header = ("# NOAA Paleoclimatology\n# First_Year: 1963\n# Last_Year: 1965\n# Variables\n"
                  "## age\tage,,,year Common Era,,corals and sclerosponges,,,N,\n")
        stm4 = ("## Sr/Ca-STM4\tstrontium/calcium,Porites sp.,,millimole per mole,,corals and sclerosponges,"
                "interpolated,ICP-AES,N,from core STM4; interpolated\n")
        stm2 = ("## Sr/Ca-STM2\tstrontium/calcium,Porites sp.,,millimole per mole,,corals and sclerosponges,"
                "interpolated,ICP-AES,N,from core STM2; interpolated\n")
        comp = ("## Sr/Ca-comp\tstrontium/calcium,Porites sp.,,millimole per mole,,corals and sclerosponges,"
                "interpolated;composited,ICP-AES,N,composite of STM4 and STM2; interpolated\n")
        (archive / "parallel.txt").write_text(
            header + stm4 + stm2 + comp + "# Data:\nage\tSr/Ca-STM4\tSr/Ca-STM2\tSr/Ca-comp\n"
            "1963.5\t9.0\t8.9\t9.0\n1963.6\t9.1\t8.8\t9.0\n", encoding="utf-8")
        # 同岩芯多处理版本：raw 与 interpolated 均声明 from core STM4
        raw = ("## Sr/Ca-STM4-raw\tstrontium/calcium,Porites sp.,,millimole per mole,,corals and sclerosponges,"
               "raw,ICP-AES,N,from core STM4\n")
        interp = ("## Sr/Ca-STM4-interp\tstrontium/calcium,Porites sp.,,millimole per mole,,corals and sclerosponges,"
                  "interpolated,ICP-AES,N,from core STM4; interpolated\n")
        (archive / "versions.txt").write_text(
            header + raw + interp + "# Data:\nage\tSr/Ca-STM4-raw\tSr/Ca-STM4-interp\n"
            "1963.5\t9.0\t9.05\n1963.6\t9.1\t9.15\n", encoding="utf-8")
        cwd = Path.cwd()
        try:
            os.chdir(root)
            (root / "dataset").mkdir()
            with (patch.object(settings, "LLM_API_KEY", "fixture"),
                  patch.object(settings, "JUDGE_BACKEND", "off"),
                  patch("app.core.moa.llm_parser.call_llm", return_value="")):
                workflow = MOAWorkflow(output_dir=str(root / "out"), enable_calibration_check=False)
                points = workflow.process_file(str(archive / "parallel.txt"))
                self.assertEqual(len(points), 6)  # 2 行 × 3 序列
                cores = Counter(p.get("source_core_id") for p in points)
                self.assertEqual(cores["STM4"], 2)
                self.assertEqual(cores["STM2"], 2)
                composite = [p for p in points if p.get("record_type") == "published_composite"]
                self.assertEqual(len(composite), 2)
                self.assertEqual(composite[0]["source_columns"]["Sr/Ca_value"], "Sr/Ca-comp")
                self.assertIn("STM4", composite[0]["composite_evidence"]["Sr/Ca_value"])
                # 同岩芯多版本：归档按 source_processing/source_columns 区分
                versions = workflow.process_file(str(archive / "versions.txt"))
                self.assertEqual(len(versions), 4)  # 2 行 × 2 版本
                self.assertTrue(all(p.get("source_core_id") == "STM4" for p in versions))
                processings = {p.get("source_processing") for p in versions}
                self.assertEqual(len(processings), 2)  # raw 与 interpolated 两版本可区分
                self.assertTrue(any("raw" in s for s in processings))
                self.assertTrue(any("interpolated" in s for s in processings))
        finally:
            os.chdir(cwd)

        # 分析视图：合成列与组成岩芯重叠观测只计一次
        import numpy as np
        import pandas as pd
        from app.analysis.loader import _mask_overlapping_composite_members
        col = "Sr/Ca (mmol/mol)"
        frame = pd.DataFrame({
            "pub1_doi": ["10.x", "10.x", "10.x"],
            "source_core_id": ["", "STM4", "STM2"],
            "source_record_id": ["comp", "STM4", "STM2"],
            "time_resolution": ["monthly", "monthly", "monthly"],
            "composite_evidence": [_json.dumps({"Sr/Ca_value": "composite of STM4 and STM2"}), np.nan, np.nan],
            "Year": [1963.5, 1963.5, 1963.5],
            col: [9.0, 9.0, 8.9],
        })
        _mask_overlapping_composite_members(frame, [col], "Year")
        self.assertEqual(frame[col].notna().sum(), 1)  # 仅合成列计一次，成员重叠被掩码
        self.assertTrue(pd.notna(frame.loc[0, col]))

    def test_auto_discard_respects_per_item_review_and_retry(self):
        from app.core.moa.workflow import MOAWorkflow
        from app.core.config import settings

        with patch.object(settings, "LLM_API_KEY", "fixture"):
            workflow = MOAWorkflow(output_dir="output/_r11_check", enable_calibration_check=False)
        file = os.path.abspath("noaa/x/obs.txt")
        from app.review_workbench.store import review_case_id
        items = [
            {"file": file, "sheet": "obs.txt", "column": "d18O", "action": "暂缓该值", "reason": "源单位缺失"},
            {"file": file, "sheet": "obs.txt", "column": "Sr/Ca", "action": "暂缓该值", "reason": "源单位缺失"},
            {"file": file, "sheet": "obs.txt", "column": "Ba/Ca", "action": "暂缓该值", "reason": "源单位缺失"},
        ]
        workflow._review_items = [dict(item) for item in items]
        ids = [review_case_id(item, os.getcwd()) for item in items]
        findings = {
            ids[0]: {"state": "pending", "reason": "本批 Max 论文额度已用完，仍待核对"},
            ids[1]: {"state": "manual", "reviewed_files": [], "unreadable_files": ["big.pdf"], "reason": "材料未读完"},
            ids[2]: {"state": "manual", "reviewed_files": ["obs.txt"], "unreadable_files": [], "reason": "无证据"},
        }
        discarded = workflow.discard_unrecovered_reviews({"findings": findings})
        self.assertEqual(discarded, 1)  # 仅 Ba/Ca 复核完成且确无证据才舍弃
        self.assertEqual(workflow.incomplete_review_count, 2)  # 配额失败 + 材料未读完保留
        by_column = {item["column"]: item for item in workflow._review_items}
        self.assertEqual(by_column["d18O"]["review_decision"], "retry_pending")
        self.assertEqual(by_column["d18O"]["reason"], items[0]["reason"])
        self.assertIn("额度", by_column["d18O"]["retry_reason"])
        self.assertEqual(by_column["d18O"]["action"], "暂缓该值")  # 原项保留
        self.assertEqual(by_column["Sr/Ca"]["review_decision"], "retry_pending")
        self.assertEqual(by_column["Ba/Ca"]["action"], "已三轮复核舍弃")
        # 模拟按原项重试成功：d18O 复核完成并应用 → 不再暂缓、不再舍弃
        by_column["d18O"]["action"] = "已应用证据复核"
        findings[ids[0]]["state"] = "verified"
        discarded_again = workflow.discard_unrecovered_reviews({"findings": findings})
        self.assertEqual(discarded_again, 0)
        self.assertEqual(workflow.incomplete_review_count, 1)  # 仅 Sr/Ca 仍未读完
        # 无 finding 的项保守保留（未复核不能断言证据不存在）
        workflow._review_items = [{"file": file, "sheet": "obs.txt", "column": "Mg/Ca",
                                   "action": "暂缓该值", "reason": "源单位缺失"}]
        self.assertEqual(workflow.discard_unrecovered_reviews({"findings": {}}), 0)
        self.assertEqual(workflow.incomplete_review_count, 1)

    def test_auto_discard_no_cross_sheet_contamination(self):
        from app.core.moa.workflow import MOAWorkflow
        from app.core.config import settings
        from app.review_workbench.store import review_case_id

        with patch.object(settings, "LLM_API_KEY", "fixture"):
            workflow = MOAWorkflow(output_dir="output/_f1_check", enable_calibration_check=False)
        file = os.path.abspath("book.xlsx")
        items = [
            {"file": file, "sheet": "Sheet1", "column": "Sr/Ca", "action": "暂缓该值", "reason": "单位缺失"},
            {"file": file, "sheet": "Sheet2", "column": "Sr/Ca", "action": "暂缓该值", "reason": "单位缺失"},
            # 同表同列但不同复核事项（映射 vs 单位）：结论不得串用
            {"file": file, "sheet": "Sheet1", "column": "Sr/Ca", "action": "暂缓映射，复判未通过", "reason": "复判分数低于放行阈值"},
        ]
        workflow._review_items = [dict(item) for item in items]
        findings = {
            review_case_id(items[0], os.getcwd()): {"state": "manual", "reviewed_files": ["book.xlsx"], "unreadable_files": []},
            review_case_id(items[1], os.getcwd()): {"state": "pending", "reason": "Max 超时"},
            review_case_id(items[2], os.getcwd()): {"state": "pending", "reason": "Max 超时"},
        }
        discarded = workflow.discard_unrecovered_reviews({"findings": findings})
        self.assertEqual(discarded, 1)  # 仅 Sheet1 单位事项（已审查确无证据）舍弃
        result = workflow._review_items
        self.assertEqual(result[0]["action"], "已三轮复核舍弃")
        self.assertEqual(result[1]["review_decision"], "retry_pending")  # Sheet2 超时保留
        self.assertEqual(result[1]["action"], "暂缓该值")
        self.assertEqual(result[1]["reason"], items[1]["reason"])
        self.assertIn("超时", result[1]["retry_reason"])
        # 同表同列的映射事项超时保留，不被单位事项的舍弃结论串用
        self.assertEqual(result[2]["review_decision"], "retry_pending")
        self.assertEqual(result[2]["action"], "暂缓映射，复判未通过")
        self.assertEqual(workflow.incomplete_review_count, 2)

    def test_review_failure_retry_and_decision_reinjection(self):
        from app.review_workbench.paper_review import PaperReview
        from app.utils.review_queue import save_review_queue, load_unit_review_decisions
        from app.core.moa.workflow import MOAWorkflow
        from app.core.config import settings

        root = Path(tempfile.mkdtemp(prefix="retry-reinject-"))
        archive = root / "noaa"
        archive.mkdir(parents=True)
        doi = "10.1234/retry"
        source = archive / "obs.txt"
        source.write_text(
            "# NOAA Paleoclimatology\n# DOI: " + doi + "\n# Variables\n"
            "## d18O\tdelta 18O,Porites sp.,,per mil,,corals and sclerosponges,,IRMS,N,\n"
            "# Data:\nYear CE\td18O\n2000\t-4.2\n2001\t-4.5\n", encoding="utf-8")
        (archive / "paper.pdf").write_bytes(b"fixture")
        run = root / "run"
        method = "Coral δ18O was analyzed by IRMS. Measurements were calibrated relative to PDB."
        cwd = Path.cwd()
        try:
            os.chdir(root)
            with (patch.object(settings, "LLM_API_KEY", "fixture"),
                  patch.object(settings, "JUDGE_BACKEND", "off"),
                  patch("app.core.moa.llm_parser.call_llm", return_value="")):
                # 由真实提取生成复核队列行（review_id 为工作流哈希，回灌才能匹配）
                wf0 = MOAWorkflow(output_dir=str(root / "out0"), enable_calibration_check=False)
                wf0.process_file(str(source))
                row = wf0.pending_reviews()[0]
                save_review_queue(str(run), [row])
                # 失败：原件不可读 + 模型空响应 + 惯例不介入 → pending
                review = PaperReview(run, root, rules_only=False)
                with (patch("app.review_workbench.paper_review._pdf_pages", return_value=()),
                      patch.object(review, "_ask", return_value={}),
                      patch.object(review, "_confirm_carbonate_convention", return_value=False)):
                    summary = review.run(all_rules=True)
                finding = next(iter(summary["findings"].values()))
                self.assertEqual(finding["state"], "pending")
                # 自动舍弃依据逐项结果：pending 保留待重试，不舍弃
                workflow = MOAWorkflow(output_dir=str(root / "out"), enable_calibration_check=False)
                workflow._review_items = [dict(row)]
                self.assertEqual(workflow.discard_unrecovered_reviews(summary), 0)
                self.assertEqual(workflow._review_items[0]["review_decision"], "retry_pending")
                self.assertEqual(workflow._review_items[0]["reason"], row["reason"])
                self.assertEqual(workflow._review_items[0]["retry_reason"], finding["reason"])
                save_review_queue(str(run), workflow.pending_reviews())
                review2 = PaperReview(run, root, rules_only=True)
                case, = review2.store.order
                self.assertEqual(case["id"], next(iter(summary["findings"])))
                self.assertEqual(case["action"], "confirm_unit")
                self.assertTrue(review2.eligible(case, (case["id"],)))
                with patch("app.review_workbench.paper_review._pdf_pages", return_value=((1, method),)):
                    summary2 = review2.run(filters=(case["id"],), all_rules=True)
                finding2 = next(iter(summary2["findings"].values()))
                self.assertEqual(finding2["state"], "verified")
                decisions = load_unit_review_decisions(run / "review_workbench" / "max_decisions.csv")
                self.assertIn(row["review_id"], decisions)
                # 裁决回灌：加载 verified 裁决重提取 → δ18O 值发表（单位已确认）
                workflow2 = MOAWorkflow(output_dir=str(root / "out2"), enable_calibration_check=False,
                                        review_decisions=decisions)
                points = workflow2.process_file(str(source))
                self.assertTrue(any(p.get("δ18O_value") is not None for p in points))
        finally:
            os.chdir(cwd)

    def test_coverage_audit_detects_partial_column_gap_and_relative_age(self):
        from app.core.moa.coverage_audit import detect_source_columns, audit_file_columns, flash_locate_evidence

        root = Path(tempfile.mkdtemp(prefix="r2-audit-"))
        # NOAA 季节表：并列干季/湿季 δ18O 列（月表已产出时仍须发现这些列漏提）
        season = root / "season.txt"
        season.write_text(
            "# NOAA Paleoclimatology\n# First_Year: 1788\n# Last_Year: 2000\n# Variables\n"
            "## age_CE\tage,,,year Common Era,,corals and sclerosponges,,,N,\n"
            "## d18O-Porites-dry\tdelta 18O,Porites sp.,,per mil VPDB,Dec-Mar,corals and sclerosponges,,IRMS,N,\n"
            "## d18O-Porites-wet\tdelta 18O,Porites sp.,,per mil VPDB,Jun-Sep,corals and sclerosponges,,IRMS,N,\n"
            "# Data:\nage_CE\td18O-Porites-dry\td18O-Porites-wet\n1788\t-5.4\t-4.4\n", encoding="utf-8")
        source = detect_source_columns(str(season))
        self.assertEqual({c["name"] for c in source["columns"]}, {"d18O-Porites-dry", "d18O-Porites-wet"})
        self.assertEqual(source["chronology"]["type"], "absolute")
        self.assertEqual({c["seasonality"] for c in source["columns"]}, {"Dec-Mar", "Jun-Sep"})
        # 该季节文件无产出 → 列级缺口列出两列（即便同目录月表另有产出）
        result = audit_file_columns(str(season), {})
        gap = next(g for g in result["gaps"] if g["type"] == "zero_output")
        self.assertEqual(set(gap["columns"]), {"d18O-Porites-dry", "d18O-Porites-wet"})

        # PANGAEA 相对年代目标表：保留剖面与待核定年锚点
        relative = root / "relative.tab"
        relative.write_text(
            "/* DATA DESCRIPTION:\nCitation:\tTest (2024): Monthly interpolated geochemistry\n"
            "Parameter(s):\tSample code/label (Sample label)\n"
            "\tAge, relative, number of years [a] (Age)\n"
            "\tPorites sp., Strontium/Calcium ratio [mmol/mol] (Porites sp. Sr/Ca)\n"
            "License:\tCC-BY\nSize:\t10 data points\n*/\n"
            "Sample code/label\tAge, relative, number of years [a]\tPorites sp., Strontium/Calcium ratio [mmol/mol]\n"
            "CORE-1\t0\t9.0\n", encoding="utf-8")
        rsource = detect_source_columns(str(relative))
        self.assertEqual(rsource["chronology"]["type"], "relative")
        self.assertEqual(rsource["profile"], "Sample code/label")
        self.assertIn("Sr/Ca", {c["proxy"] for c in rsource["columns"]})
        rresult = audit_file_columns(str(relative), {})
        anchor = next(g for g in rresult["gaps"] if g["type"] == "relative_age_needs_anchor")
        self.assertEqual(anchor["profile"], "Sample code/label")
        self.assertEqual(anchor["chronology_column"], "Age, relative, number of years")

        # Flash 证据定位器调用状态写入账本（空响应/成功）
        with patch("app.core.moa.coverage_audit.call_llm", return_value=""):
            empty = flash_locate_evidence(str(root), [{"type": "zero_output"}], cache=None)
        self.assertEqual(empty["call_status"], "empty_response")
        answer = '{"file":"season.txt","sheet":null,"page":"3","column_meaning":"dry season","chronology":"CE","resolution":"needs_max","reason":"x"}'
        with patch("app.core.moa.coverage_audit.call_llm", return_value=answer):
            ok = flash_locate_evidence(str(root), [{"type": "zero_output"}], cache=None)
        self.assertEqual(ok["call_status"], "success")
        self.assertEqual(ok["file"], "season.txt")
        self.assertEqual(ok["resolution"], "needs_max")

    def test_coverage_audit_column_level_xlsx_and_handoff(self):
        import openpyxl
        from app.core.moa.coverage_audit import (detect_source_columns, audit_file_columns,
                                                 build_max_handoff)
        from app.review_workbench.store import ReviewStore
        from app.utils.review_queue import save_review_queue
        root = Path(tempfile.mkdtemp(prefix="f2-audit-"))
        # PANGAEA δ18O/δ13C 检出（δ→d 归一化修复）
        pangaea = root / "knebel.tab"
        pangaea.write_text(
            "/* DATA DESCRIPTION:\nCitation:\tX\nParameter(s):\tSample code/label (Sample label)\n"
            "\tAge, relative, number of years [a] (Age)\n"
            "\tPorites sp., Strontium/Calcium ratio [mmol/mol] (Sr/Ca)\n"
            "\tPorites sp., δ18O [‰ PDB] (δ18O)\n"
            "\tPorites sp., δ13C [‰ PDB] (δ13C)\nLicense:\tCC\nSize:\t1\n*/\n", encoding="utf-8")
        source = detect_source_columns(str(pangaea))
        self.assertEqual({c["proxy"] for c in source["columns"]}, {"Sr/Ca", "δ18O", "δ13C"})
        # Ramos 逐列对账：干季 δ18O 已产出，湿季列仍须报漏（非仅指标存在）
        season = root / "season.txt"
        season.write_text(
            "# NOAA Paleoclimatology\n# Variables\n"
            "## age_CE\tage,,,year Common Era,,corals and sclerosponges,,,N,\n"
            "## d18O-Porites-dry\tdelta 18O,Porites sp.,,per mil VPDB,Dec-Mar,corals and sclerosponges,,IRMS,N,\n"
            "## d18O-Porites-wet\tdelta 18O,Porites sp.,,per mil VPDB,Jun-Sep,corals and sclerosponges,,IRMS,N,\n"
            "# Data:\nage_CE\td18O-Porites-dry\td18O-Porites-wet\n1788\t-5\t-4\n", encoding="utf-8")
        file_out = {os.path.normcase(os.path.abspath(str(season))):
                    {"rows": 100, "proxies": {"δ18O"}, "columns": {"d18O-Porites-dry"}}}
        result = audit_file_columns(str(season), file_out)
        missing = [g for g in result["gaps"] if g["type"] == "column_missing"]
        self.assertEqual(missing[0]["columns"], ["d18O-Porites-wet"])
        # xlsx 第4工作表 Sr/Ca 样例须检出（读全部表）
        book = openpyxl.Workbook()
        for index in range(3):
            book.create_sheet(f"S{index + 1}")
        fourth = book.create_sheet("S4")
        fourth.append(["Year", "Sr/Ca (mmol/mol)"])
        fourth.append([2000, 8.9])
        del book[book.sheetnames[0]]
        xlsx = root / "book.xlsx"
        book.save(str(xlsx))
        xsource = detect_source_columns(str(xlsx))
        self.assertTrue(any(c["proxy"] == "Sr/Ca" and c.get("sheet") == "S4" for c in xsource["columns"]))
        # Flash 证据账本 → Max 交接
        results = [{"disposition": "gap", "route": "flash_then_max", "folder": "noaa/x", "gaps": [],
                    "file_gaps": [{"file": "season.txt",
                                   "gaps": [{"type": "column_missing", "columns": ["d18O-Porites-wet"], "sheets": [""]}]}],
                    "flash_evidence": {"file": "season.txt", "sheet": "S4", "page": "3",
                                       "column_meaning": "wet season", "chronology": "CE",
                                       "resolution": "needs_max", "call_status": "success"}}]
        items = build_max_handoff(results, root)
        self.assertEqual(len(items), 1)
        self.assertIn("Flash定位", items[0]["reason"])
        self.assertIn("needs_max", items[0]["reason"])
        run = root / "run"
        (run / "review_workbench").mkdir(parents=True)
        fields = ["file", "sheet", "column", "action", "reason", "decision", "source_sha256", "review_decision"]
        with (run / "review_workbench" / "max_handoff.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(items)
        save_review_queue(str(run), [{"file": "season.txt", "action": "复核输入"}])
        original_queue = (run / "review_queue.csv").read_bytes()
        cases = [case for case in ReviewStore(run, root).order if case["action"] == "confirm_mapping"]
        self.assertEqual([(case["column"], case["target"]) for case in cases], [("d18O-Porites-wet", "δ18O_value")])
        self.assertEqual((run / "review_queue.csv").read_bytes(), original_queue)
        save_review_queue(str(run), [{**items[0], "file": str(season)}])
        self.assertEqual(len(ReviewStore(run, root).order), 1)  # 绝对与相对路径为同一源列

    def test_parallel_cores_separate_age_axes_and_version_identity(self):
        from app.core.moa.workflow import MOAWorkflow
        from app.core.moa.data_output import DataOutputModule
        from app.core.config import settings

        root = Path(tempfile.mkdtemp(prefix="f3-cores-"))
        archive = root / "noaa" / "cores"
        archive.mkdir(parents=True)
        source = archive / "obs.txt"
        source.write_text(
            "# NOAA Paleoclimatology\n# First_Year: 1963\n# Last_Year: 1965\n# Variables\n"
            "## age-STM4\tage,,,year Common Era,,corals and sclerosponges,,,N,\n"
            "## Sr/Ca-STM4\tstrontium/calcium,Porites sp.,,millimole per mole,,corals and sclerosponges,interpolated,ICP-AES,N,from core STM4\n"
            "## age-STM2\tage,,,year Common Era,,corals and sclerosponges,,,N,\n"
            "## Sr/Ca-STM2\tstrontium/calcium,Porites sp.,,millimole per mole,,corals and sclerosponges,interpolated,ICP-AES,N,from core STM2\n"
            "# Data:\nage-STM4\tSr/Ca-STM4\tage-STM2\tSr/Ca-STM2\n1963.5\t9.0\t1963.6\t8.9\n1964.5\t9.1\t1964.6\t8.8\n",
            encoding="utf-8")
        cwd = Path.cwd()
        try:
            os.chdir(root)
            (root / "dataset").mkdir()
            with (patch.object(settings, "LLM_API_KEY", "fixture"),
                  patch.object(settings, "JUDGE_BACKEND", "off"),
                  patch("app.core.moa.llm_parser.call_llm", return_value="")):
                workflow = MOAWorkflow(output_dir=str(root / "out"), enable_calibration_check=False)
                points = workflow.process_file(str(source))
                self.assertEqual(len(points), 4)  # 2 行 × 2 岩芯，各自年代轴不整表暂缓
                stm4 = [p for p in points if p.get("source_core_id") == "STM4"]
                stm2 = [p for p in points if p.get("source_core_id") == "STM2"]
                self.assertEqual(sorted(round(p["year"], 3) for p in stm4), [1963.5, 1964.5])
                self.assertEqual(sorted(round(p["year"], 3) for p in stm2), [1963.6, 1964.6])
            # 观测 ID 区分同岩芯处理版本（raw/interpolated 不再同 ID）
            base = {"source_file": "x.txt", "sheet_name": "s", "source_row": 1,
                    "source_record_id": "STM4", "sample_id": "STM4"}
            raw = {**base, "source_processing": "raw", "source_columns": {"Sr/Ca_value": "Sr/Ca-raw"}}
            interp = {**base, "source_processing": "interpolated", "source_columns": {"Sr/Ca_value": "Sr/Ca-interp"}}
            self.assertNotEqual(DataOutputModule._observation_id(raw), DataOutputModule._observation_id(interp))
        finally:
            os.chdir(cwd)

    def test_calendar_month_mar_not_february_and_version_overlap(self):
        import numpy as np
        import pandas as pd
        from app.analysis.loader import _add_calendar_month, _mask_processing_version_overlaps

        # Mar-YY 须归 3 月（分数年公式会误归 2 月），Aug-YY 归 8 月
        frame = pd.DataFrame({"Year": [1987 + 59 / 365, 1987 + 212 / 365, 2007 + 212 / 365],
                              "source_time_value": ["Mar-87", "Aug-87", "Aug-07"]})
        _add_calendar_month(frame)
        self.assertEqual(list(frame["Month"]), [3, 8, 8])
        # 同岩芯同年代 raw/interpolated 重叠只计一次（优先原始版）
        col = "Sr/Ca (mmol/mol)"
        versioned = pd.DataFrame({
            "source_record_id": ["STM4", "STM4", "STM2"], "source_core_id": ["STM4", "STM4", "STM2"],
            "source_processing": ["raw", "interpolated", "raw"],
            "Year": [1963.5, 1963.5, 1963.6], col: [9.0, 9.05, 8.9]})
        _mask_processing_version_overlaps(versioned, [col], "Year")
        self.assertTrue(pd.notna(versioned.loc[0, col]))   # STM4 raw 保留
        self.assertTrue(pd.isna(versioned.loc[1, col]))    # STM4 interpolated 掩码
        self.assertTrue(pd.notna(versioned.loc[2, col]))   # STM2 保留

    def test_parallel_core_missing_own_age_not_borrowed(self):
        from app.core.moa.workflow import MOAWorkflow
        from app.core.config import settings

        root = Path(tempfile.mkdtemp(prefix="p1-age-"))
        archive = root / "noaa" / "cores"
        archive.mkdir(parents=True)
        source = archive / "obs.txt"
        source.write_text(
            "# NOAA Paleoclimatology\n# First_Year: 1963\n# Last_Year: 1965\n# Variables\n"
            "## age-STM4\tage,,,year Common Era,,corals and sclerosponges,,,N,\n"
            "## Sr/Ca-STM4\tstrontium/calcium,Porites sp.,,millimole per mole,,corals and sclerosponges,interpolated,ICP-AES,N,from core STM4\n"
            "## age-STM2\tage,,,year Common Era,,corals and sclerosponges,,,N,\n"
            "## Sr/Ca-STM2\tstrontium/calcium,Porites sp.,,millimole per mole,,corals and sclerosponges,interpolated,ICP-AES,N,from core STM2\n"
            "# Data:\nage-STM4\tSr/Ca-STM4\tage-STM2\tSr/Ca-STM2\n1963.5\t9.0\t1963.6\t8.9\n1964.5\t9.1\t\t8.8\n",
            encoding="utf-8")
        cwd = Path.cwd()
        try:
            os.chdir(root)
            (root / "dataset").mkdir()
            with (patch.object(settings, "LLM_API_KEY", "fixture"),
                  patch.object(settings, "JUDGE_BACKEND", "off"),
                  patch("app.core.moa.llm_parser.call_llm", return_value="")):
                workflow = MOAWorkflow(output_dir=str(root / "out"), enable_calibration_check=False)
                points = workflow.process_file(str(source))
                stm2 = [p for p in points if p.get("source_core_id") == "STM2"]
                # STM2 第二行自身年代缺失→不借用 STM4 的 1964.5，仅发表第一行
                self.assertEqual([round(p["year"], 3) for p in stm2], [1963.6])
                self.assertFalse(any(p.get("source_core_id") == "STM2" and abs(p["year"] - 1964.5) < 1e-6 for p in points))
                # 缺失年代项进入复核队列（不静默通过时间质控）
                reviews = workflow.pending_reviews()
                self.assertTrue(any("STM2" in str(r.get("reason", "")) and "年代" in str(r.get("reason", "")) for r in reviews))
        finally:
            os.chdir(cwd)

    def test_version_dedup_isolates_different_papers(self):
        import pandas as pd
        from app.analysis.loader import _mask_processing_version_overlaps

        col = "Sr/Ca (mmol/mol)"
        # 两篇不同论文恰好同名同年岩芯：不是同一岩芯的处理版本，两个有效值都保留
        frame = pd.DataFrame({
            "source_file": ["paperA.csv", "paperB.csv"], "pub1_doi": ["10.A", "10.B"],
            "source_record_id": ["STM4", "STM4"], "source_core_id": ["STM4", "STM4"],
            "source_processing": ["raw", "raw"], "Year": [1963.5, 1963.5], col: [9.0, 8.5]})
        _mask_processing_version_overlaps(frame, [col], "Year")
        self.assertTrue(pd.notna(frame.loc[0, col]))
        self.assertTrue(pd.notna(frame.loc[1, col]))  # 不同论文不被误删
        # 同论文同岩芯 raw/interpolated 重叠→插值版掩码
        same = pd.DataFrame({
            "source_file": ["paperA.csv", "paperA.csv"], "pub1_doi": ["10.A", "10.A"],
            "source_record_id": ["STM4", "STM4"], "source_core_id": ["STM4", "STM4"],
            "source_processing": ["raw", "interpolated"], "Year": [1963.5, 1963.5], col: [9.0, 9.05]})
        _mask_processing_version_overlaps(same, [col], "Year")
        self.assertTrue(pd.notna(same.loc[0, col]))
        self.assertTrue(pd.isna(same.loc[1, col]))

    def test_coverage_audit_sheet_row_level_and_executable_handoff(self):
        import openpyxl
        from app.core.moa.coverage_audit import detect_source_columns, audit_file_columns, build_max_handoff

        root = Path(tempfile.mkdtemp(prefix="p4-audit-"))
        # 同工作簿两表同名 Sr/Ca：A 表已产出不掩盖 B 表漏提
        book = openpyxl.Workbook()
        sheet_a = book.active
        sheet_a.title = "SheetA"
        sheet_a.append(["Year", "Sr/Ca (mmol/mol)"])
        sheet_a.append([2000, 8.9])
        sheet_b = book.create_sheet("SheetB")
        sheet_b.append(["Year", "Sr/Ca (mmol/mol)"])
        sheet_b.append([2000, 7.7])
        xlsx = root / "book.xlsx"
        book.save(str(xlsx))
        source = detect_source_columns(str(xlsx))
        self.assertEqual({c["sheet"] for c in source["columns"] if c["proxy"] == "Sr/Ca"}, {"SheetA", "SheetB"})
        file_out = {os.path.normcase(os.path.abspath(str(xlsx))):
                    {"rows": 1, "proxies": {"Sr/Ca"}, "columns": {"Sr/Ca (mmol/mol)"},
                     "sheets": {"SheetA": {"rows": 1, "columns": {"Sr/Ca (mmol/mol)"}}}}}
        result = audit_file_columns(str(xlsx), file_out)
        missing = [g for g in result["gaps"] if g["type"] == "column_missing"]
        self.assertTrue(missing and "SheetB" in missing[0]["sheets"])
        # 深表头：第 8 行 Sr/Ca 样例须检出（不再误判可排除）
        book2 = openpyxl.Workbook()
        deep = book2.active
        for index in range(7):
            deep.append([f"note{index}"])
        deep.append(["Year", "Sr/Ca (mmol/mol)"])
        deep.append([2000, 8.9])
        xlsx2 = root / "deep.xlsx"
        book2.save(str(xlsx2))
        self.assertTrue(any(c["proxy"] == "Sr/Ca" for c in detect_source_columns(str(xlsx2))["columns"]))
        # 行级缺口：源 10 行产出 5 行
        noaa = root / "rows.txt"
        noaa.write_text("# NOAA Paleoclimatology\n# Variables\n"
                        "## age_CE\tage,,,year Common Era,,corals and sclerosponges,,,N,\n"
                        "## Sr/Ca\tstrontium/calcium,Porites sp.,,mmol/mol,,corals and sclerosponges,,ICP-MS,N,\n"
                        "# Data:\nage_CE\tSr/Ca\n" + "".join(f"{2000 + i}\t{8.9 + i * 0.1:.1f}\n" for i in range(10)),
                        encoding="utf-8")
        file_out2 = {os.path.normcase(os.path.abspath(str(noaa))):
                     {"rows": 5, "proxies": {"Sr/Ca"}, "columns": {"Sr/Ca"}, "sheets": {}}}
        result2 = audit_file_columns(str(noaa), file_out2)
        self.assertTrue(any(g["type"] == "row_missing" for g in result2["gaps"]))
        # Max 交接项可执行（confirm_mapping 动作 + 目标字段 + 表名）
        results = [{"disposition": "gap", "route": "flash_then_max", "folder": "x", "gaps": [],
                    "file_gaps": [{"file": "book.xlsx", "gaps": [
                        {"type": "column_missing", "columns": ["Sr/Ca (mmol/mol)"], "proxies": ["Sr/Ca"],
                         "column_proxies": {"Sr/Ca (mmol/mol)": "Sr/Ca"}, "sheets": ["SheetB"]}]}],
                    "flash_evidence": {"resolution": "needs_max", "call_status": "success", "file": "book.xlsx"}}]
        items = build_max_handoff(results, root)
        self.assertEqual(items[0]["action"], "暂缓映射，复判未通过")
        self.assertEqual(items[0]["decision"], "Sr/Ca_value")
        self.assertEqual(items[0]["sheet"], "SheetB")

    def test_cross_folder_copy_remains_available_after_first_copy_fails(self):
        from app.core.moa.workflow import MOAWorkflow
        from app.core.config import settings

        root = Path(tempfile.mkdtemp(prefix="p4-dedup-"))
        for folder in ("paperA", "paperB"):
            directory = root / "noaa" / folder
            directory.mkdir(parents=True)
            (directory / "paper.pdf").write_bytes(b"same paper content")
            (directory / "data.csv").write_text("Year CE,Sr/Ca (mmol/mol)\n2000,8.9\n2001,9.1\n", encoding="utf-8")
        duplicate = root / "noaa/paperB/duplicate.csv"
        duplicate.write_bytes((duplicate.parent / "data.csv").read_bytes())
        cwd = Path.cwd()
        try:
            os.chdir(root)
            (root / "dataset").mkdir()
            with (patch.object(settings, "LLM_API_KEY", "fixture"),
                  patch.object(settings, "JUDGE_BACKEND", "off"),
                  patch("app.core.moa.llm_parser.call_llm", return_value="")):
                workflow = MOAWorkflow(output_dir=str(root / "out"), enable_calibration_check=False)
                process_file = workflow.process_file

                def process(file, *args, **kwargs):
                    if Path(file) == Path("noaa/paperA/data.csv"):
                        workflow._record_input(file, status="failed", reason="模拟读取失败")
                        return []
                    return process_file(file, *args, **kwargs)

                with patch.object(workflow, "process_file", side_effect=process):
                    points = workflow.process_directory("dataset", noaa_dir="noaa")
                self.assertEqual([point["Sr/Ca_value"] for point in points], [8.9, 9.1])
                self.assertEqual({Path(point["source_file"]).parent.name for point in points}, {"paperB"})
                inventory = {Path(row["file"]): row for row in workflow.source_inventory if not row["sheet"]}
                self.assertEqual(inventory[Path("noaa/paperA/data.csv")]["status"], "failed")
                self.assertEqual(inventory[Path("noaa/paperB/data.csv")]["status"], "extracted")
                self.assertIn("同目录内容完全相同的副本", inventory[Path("noaa/paperB/duplicate.csv")]["reason"])
        finally:
            os.chdir(cwd)

    def test_row_missing_filters_legitimate_exclusions(self):
        from app.core.moa.coverage_audit import (audit_file_columns, _count_extractable_rows,
                                                 detect_source_columns)
        root = Path(tempfile.mkdtemp(prefix="row-missing-"))
        noaa = root / "obs.txt"
        # 5 行：3 行有效（指标+年代），1 行缺年代、1 行缺指标值（合理排除）
        noaa.write_text("# NOAA Paleoclimatology\n# Variables\n"
                        "## age_CE\tage,,,year Common Era,,corals and sclerosponges,,,N,\n"
                        "## Sr/Ca\tstrontium/calcium,Porites sp.,,mmol/mol,,corals and sclerosponges,,ICP-MS,N,\n"
                        "# Data:\nage_CE\tSr/Ca\n2000\t8.9\n2001\t9.0\n2002\t9.1\n\t9.2\n2004\t\n",
                        encoding="utf-8")
        source = detect_source_columns(str(noaa))
        self.assertEqual(_count_extractable_rows(str(noaa), source), 3)  # 缺年代/缺值 2 行被过滤
        key = os.path.normcase(os.path.abspath(str(noaa)))
        # 产出 3 行=可提取 3 行 → 无缺行（合理排除不算漏提）
        full = {key: {"rows": 3, "proxies": {"Sr/Ca"}, "columns": {"Sr/Ca"}, "sheets": {}}}
        self.assertFalse(any(g["type"] == "row_missing" for g in audit_file_columns(str(noaa), full)["gaps"]))
        # 产出 2 行 < 可提取 3 行 → 真缺行
        partial = {key: {"rows": 2, "proxies": {"Sr/Ca"}, "columns": {"Sr/Ca"}, "sheets": {}}}
        missing = [g for g in audit_file_columns(str(noaa), partial)["gaps"] if g["type"] == "row_missing"]
        self.assertTrue(missing and missing[0]["source_rows"] == 3)

    def test_proxy_identity_confirmed_uses_sheet_and_definition(self):
        from app.review_workbench.paper_review import proxy_identity_confirmed

        case = {"target": "δ18O_value", "column": "PSYL-3-2"}
        self.assertTrue(proxy_identity_confirmed(case, {"item": {"sheet": "monthly-δ18O"}, "variables": {}}))
        self.assertTrue(proxy_identity_confirmed(case, {"item": {"sheet": "Sheet1"},
                                                        "variables": {"PSYL-3-2": {"quantity": "delta 18O"}}}))
        self.assertFalse(proxy_identity_confirmed(case, {"item": {"sheet": "Sheet1"}, "variables": {}}))
        self.assertFalse(proxy_identity_confirmed({"target": "δ18O_value", "column": "d18O_error"},
                                                  {"item": {"sheet": "monthly-δ18O"}, "variables": {}}))
        self.assertFalse(proxy_identity_confirmed({"target": "δ18O_value", "column": "mean d18O"},
                                                  {"item": {"sheet": "monthly-δ18O"}, "variables": {}}))

    def test_carbonate_convention_recovers_coral_permil_and_guards(self):
        import hashlib
        from app.review_workbench.paper_review import PaperReview, _carbonate_convention_candidate
        from app.utils.review_queue import load_unit_review_decisions, save_review_queue

        root = Path(tempfile.mkdtemp(prefix="carbonate-conv-"))
        archive = root / "noaa"
        archive.mkdir()
        source = archive / "obs.txt"
        source.write_text("# NOAA Paleoclimatology\n# Variables\n"
                          "## d18O\tdelta 18O,Porites sp.,,per mil,,corals and sclerosponges,,mass spectrometry,N,\n"
                          "# Data:\nYear CE\td18O\n2000\t-4.2\n", encoding="utf-8")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        save_review_queue(str(root), [{"file": str(source), "sheet": source.name, "column": "d18O",
                                       "decision": "δ18O_value", "source_unit": "per mil",
                                       "source_sha256": digest, "action": "暂缓该值",
                                       "reason": "源单位或同位素参考标尺缺失", "review_id": "r1"}])
        review = PaperReview(root, root, rules_only=True)
        summary = review.run(all_rules=True)
        self.assertEqual(summary["cases"], {"verified": 1})
        saved = load_unit_review_decisions(root / "review_workbench" / "max_decisions.csv")
        self.assertEqual((saved["r1"]["unit"], saved["r1"]["origin"]), ("‰ VPDB", "carbonate_convention"))
        # 海水材料守卫：同结构但 material=seawater 不得认定
        sw = archive / "sw.txt"
        sw.write_text("# NOAA Paleoclimatology\n# Variables\n"
                      "## d18O\tdelta 18O,seawater,,per mil,,corals and sclerosponges,,mass spectrometry,N,\n"
                      "# Data:\nYear CE\td18O\n2000\t0.5\n", encoding="utf-8")
        sw_digest = hashlib.sha256(sw.read_bytes()).hexdigest()
        sw_root = root / "sw_run"
        save_review_queue(str(sw_root), [{"file": str(sw), "sheet": sw.name, "column": "d18O",
                                          "decision": "δ18O_value", "source_unit": "per mil",
                                          "source_sha256": sw_digest, "action": "暂缓该值",
                                          "reason": "源单位或同位素参考标尺缺失", "review_id": "s1"}])
        sw_review = PaperReview(sw_root, root, rules_only=True)
        sw_case, = sw_review.store.order
        self.assertFalse(_carbonate_convention_candidate(sw_review.prepare(sw_case, sw)))

    def test_discard_unrecovered_reviews_marks_held_items_only(self):
        from app.review_workbench.store import review_case_id
        items = [
            {"file": "a.csv", "sheet": "", "column": "d18O", "action": "暂缓该值", "reason": "源单位缺失"},
            {"file": "b.csv", "sheet": "", "column": "", "action": "复核空间范围", "reason": "范围"},
            {"file": "c.csv", "action": "已应用证据复核", "reason": "ok"},
            {"file": "d.csv", "action": "已排除非观测片段", "reason": "excluded"},
        ]
        self.workflow._review_items = items
        # a.csv 复核完成且确无证据→舍弃；b.csv 无 finding→保守保留待重试
        findings = {review_case_id(items[0], os.getcwd()): {"state": "manual",
                    "reviewed_files": ["a.csv"], "unreadable_files": []}}
        self.assertEqual(self.workflow.discard_unrecovered_reviews({"findings": findings}), 1)
        actions = [item["action"] for item in self.workflow._review_items]
        self.assertEqual(actions, ["已三轮复核舍弃", "复核空间范围", "已应用证据复核", "已排除非观测片段"])
        self.assertIn("源数据保留", self.workflow._review_items[0]["reason"])
        self.assertEqual(self.workflow._review_items[1]["review_decision"], "retry_pending")
        self.assertEqual(self.workflow.incomplete_review_count, 1)

    def test_full_auto_mode_preserves_failed_reviews_for_retry(self):
        import main
        from app.core.config import settings

        cwd = Path.cwd()
        with tempfile.TemporaryDirectory(prefix="full-auto-") as staging:
            try:
                os.chdir(staging)
                source = Path("dataset/case/data.csv")
                source.parent.mkdir(parents=True)
                source.write_text("Year CE,Sr/Ca (mmol/mol),d18O-HYD3\n2000,8.9,-4.2\n2001,9.1,-4.5\n",
                                  encoding="utf-8")
                with (patch.object(settings, "LLM_API_KEY", "fixture"),
                      patch.object(settings, "JUDGE_BACKEND", "off"),
                      patch("app.core.moa.llm_parser.call_llm", return_value=""),
                      patch("app.review_workbench.paper_review.PaperReview._ask", return_value={})):
                    batch = Path(main.run_moa_pipeline(auto_discard=True))
                summary = json.loads((batch / "review_workbench/max_review.json").read_text(encoding="utf-8"))
                self.assertEqual(summary.get("model_review_scope"), "full_auto_recover_then_discard")
                # 空模型响应=复核失败：未完成项如实报告（不强制归零），失败项保留待重试
                incomplete = summary.get("incomplete_reviews", 0)
                self.assertEqual(summary.get("manual_remaining"), incomplete)
                with (batch / "review_queue.csv").open(encoding="utf-8-sig", newline="") as stream:
                    rows = list(csv.DictReader(stream))
                if incomplete:
                    self.assertTrue(any(r.get("review_decision") == "retry_pending" for r in rows))
                    self.assertEqual(summary.get("status"), "completed_with_retry_pending")
            finally:
                os.chdir(cwd)

    def test_full_auto_mode_retries_unapplied_verified_decision(self):
        from types import SimpleNamespace

        from app.core.moa.full_review import _replay
        from app.review_workbench.store import review_case_id

        item = {"file": "data.csv", "sheet": "default", "column": "ProxyZ",
                "action": "暂缓映射，复判未通过", "reason": "复判未通过"}
        self.workflow._review_items = [item]
        case_id = review_case_id(item, os.getcwd())
        summary = {"findings": {case_id: {
            "state": "verified", "reason": "原件已确认列定义",
            "reviewed_files": ["data.csv"], "unreadable_files": [],
        }}}
        review = SimpleNamespace(
            store=SimpleNamespace(work_dir=Path.cwd()),
            decisions={"mapping": {"cluster_id": case_id, "file": "data.csv"}},
        )
        with (patch("app.core.moa.full_review.load_unit_review_decisions", return_value={"mapping": {}}),
              patch("app.core.moa.workflow.MOAWorkflow") as replay):
            replay.return_value.applied_unit_reviews = set()
            replay.return_value.processed_data = []
            replay.return_value.source_inventory = []
            decisions, summary = _replay(self.workflow, review, summary, None, None, {}, False)

        self.assertEqual(decisions, [])
        self.assertEqual(summary["cases"], {"pending": 1})
        self.assertEqual(self.workflow.discard_unrecovered_reviews(summary), 0)
        self.assertEqual(item["review_decision"], "retry_pending")

    def test_workbench_unit_suggestion_requires_scale_in_original_quote(self):
        from app.review_workbench.assist import suggest_item

        model_answer = json.dumps({"action": "confirm_unit", "confirmed_unit": "‰ VPDB",
                                   "evidence_status": "verified", "note": "确认参考标尺"})
        archive = {"source": "archive_definition", "label": "per mil", "unit": "‰"}
        body = {"source": "body", "label": "‰ VPDB", "quoted_units": [],
                "quote": "The δ 18Oc values of DS86 average −4.54‰"}
        item = {"column": "d18O", "decision": "δ18O_value", "source_unit": "per mil",
                "unit_evidence": json.dumps([archive, body])}
        with (patch("app.review_workbench.assist._source_snippet", return_value="d18O | per mil"),
              patch("app.review_workbench.assist.call_llm", return_value=model_answer) as model):
            pending = suggest_item(Path.cwd(), item)
            self.assertEqual((pending["action"], pending["confirmed_unit"], pending["note"]), ("needs_human", "", ""))
            self.assertNotIn("VPDB", model.call_args.kwargs["question"])
            body["quote"] = "DS86 coral δ18O values were reported in ‰ relative to VPDB."
            item["unit_evidence"] = json.dumps([archive, body])
            self.assertEqual(suggest_item(Path.cwd(), item)["action"], "confirm_unit")
            item["unit_evidence"] = [archive, body, {**body, "quote": body["quote"].replace("VPDB", "VSMOW")}]
            self.assertEqual(suggest_item(Path.cwd(), item)["action"], "needs_human")

    def test_paper_review_uses_bound_isotope_methods_before_calling_the_model(self):
        import hashlib
        from app.review_workbench.paper_review import PaperReview
        from app.utils.review_queue import load_unit_review_decisions, save_review_queue

        root = Path(self._testMethodName).resolve()
        archive = root / "noaa"
        archive.mkdir(parents=True)
        doi = "10.1234/isotope-method"
        source = archive / "observations.txt"
        source.write_text(
            "# NOAA Paleoclimatology\n" + f"# DOI: {doi}\n" + "# Variables\n"
            + "## d18O\tdelta 18O,Porites sp.,,per mil,,corals and sclerosponges,,mass spectrometry,N,\n"
            + "# Data:\nYear CE\td18O\n2000\t-4.2\n2001\t-4.5\n", encoding="utf-8",
        )
        paper = archive / "paper.pdf"
        paper.write_bytes(b"paper fixture")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        row = {"file": str(source), "sheet": source.name, "column": "d18O", "decision": "δ18O_value",
               "source_unit": "per mil", "source_sha256": digest, "action": "暂缓该值",
               "reason": "源单位或同位素参考标尺缺失"}
        rows = [{**row, "review_id": core, "sample_id": core} for core in ("A01", "B02")]
        method = ("Coral δ18O was analyzed using isotope ratio mass spectrometry. "
                  "Measurements were calibrated relative to PDB.")
        cases = {
            "global": ((doi, method), "verified", "paper_source"),
            "bare": ((doi, "Coral δ18O values were reported in per mil."), "verified", "carbonate_convention"),
            "publication_version": (("10.1234/other", method), "verified", "paper_source"),
            "wrong_proxy": ((doi, "Coral δ18O was analyzed, while δ13C was reported relative to VPDB."),
                            "verified", "carbonate_convention"),
            "standard": ((doi, "Coral δ18O was analyzed. The standard was calibrated relative to VPDB."),
                         "verified", "carbonate_convention"),
            "subset": ((doi, "Coral δ18O for core A01 was normalized to VPDB."), "verified", "carbonate_convention"),
            "sample_list": ((doi, "For the two (A-01 and B-02) coral samples we measured δ18O. " + method),
                            "verified", "paper_source"),
            "conflicting_pages": ((doi, method, "Coral δ18O measurements were expressed on the VSMOW scale."),
                                  "manual", None),
            "conflicting_subset": ((doi, method, "Coral δ18O for core B-02 was normalized to VSMOW."), "manual", None),
            # 仅部分岩芯声明非 VPDB 标尺（无全局方法声明）：不得回退碳酸盐惯例误放整组
            "subset_vsmow_only": ((doi, "Coral δ18O for core A01 was normalized to VSMOW."), "manual", None),
        }
        for name, (pages, state, origin) in cases.items():
            with self.subTest(name=name):
                run = root / name
                save_review_queue(str(run), rows)
                review = PaperReview(run, root, rules_only=True)
                with (patch("app.review_workbench.paper_review._pdf_pages", return_value=tuple(enumerate(pages, 1))),
                      patch.object(review, "_ask") as model):
                    summary = review.run(all_rules=True)
                model.assert_not_called()
                self.assertEqual(summary["cases"], {state: 1})
                decisions = load_unit_review_decisions(run / "review_workbench" / "max_decisions.csv")
                self.assertEqual(set(decisions), {"A01", "B02"} if state == "verified" else set())
                for decision in decisions.values():
                    self.assertEqual((decision["unit"], decision["origin"], decision["original_unit"]),
                                     ("‰ VPDB", origin, "per mil"))
                if origin == "paper_source":
                    finding, = summary["findings"].values()
                    self.assertIn(finding["evidence"][0]["quote"], pages[1])

    def test_paper_review_reads_linked_word_supplement_for_missing_scale(self):
        from docx import Document
        from app.review_workbench.paper_review import PaperReview
        from app.utils.review_queue import save_review_queue

        root = Path(self._testMethodName).resolve()
        root.mkdir(exist_ok=True)
        source = root / "observations.csv"
        source.write_text("Year CE,d18O-HYD3\n2000,-4.2\n", encoding="utf-8")
        save_review_queue(str(root), [{"file": source.name, "sheet": "default", "column": "d18O-HYD3",
                                       "action": "暂缓该值", "reason": "源单位或同位素参考标尺缺失",
                                       "decision": "δ18O_value", "review_id": "r1", "source_unit": "per mil",
                                       "publication_doi": "10.1234/example"}])
        review = PaperReview(root, root, rules_only=True)
        case, = review.store.order
        before = review.prepare(case, source)
        quote = "Coral δ18O values of HYD3 were reported in ‰ relative to VPDB."
        doc = Document()
        doc.add_paragraph(quote)
        supplement = root / "definitions.docx"
        doc.save(supplement)
        review = PaperReview(root, root, rules_only=True)
        entry = review.prepare(case, source)
        self.assertNotEqual(before["version"], entry["version"])

        def answer(entries, documents):
            material, = (material for material in documents if material["file"] == supplement.name)
            self.assertEqual(material["file"], supplement.name)
            self.assertIn(quote, material["text"])
            return {case["id"]: {"action": "confirm_unit", "unit": "‰ VPDB",
                                 "evidence": [{"ref": material["id"], "quote": quote}]}}

        with patch.object(review, "_ask", side_effect=answer):
            review._review_with_model([entry])
        self.assertEqual(review.findings[case["id"]]["state"], "verified")
        decision, = review.decisions.values()
        self.assertEqual(decision["evidence_file"], str(supplement))

    def test_full_review_uses_pdf_body_and_unnamed_attachment_for_unit_case(self):
        from docx import Document
        from app.review_workbench.paper_review import PaperReview
        from app.utils.review_queue import save_review_queue

        root = Path(self._testMethodName).resolve()
        root.mkdir(exist_ok=True)
        source = root / "observations.csv"
        source.write_text("Year CE,Ba/Ca-HYD3\n2000,4500\n", encoding="utf-8")
        (root / "paper.pdf").write_bytes(b"paper fixture")
        quote = "Coral Ba/Ca for HYD3 was measured and reported in nmol/mol."
        notes = Document()
        notes.add_paragraph(quote)
        notes.save(root / "column_notes.docx")
        save_review_queue(root, [{"file": source.name, "sheet": "default", "column": "Ba/Ca-HYD3",
                                  "action": "暂缓该值", "reason": "Ba/Ca 的 nmol/mol 档案声明需与论文或补充数据交叉核对",
                                  "decision": "Ba/Ca_value", "review_id": "r1", "source_unit": "nmol/mol"}])
        review = PaperReview(root, root)

        def answer(entries, documents):
            self.assertTrue(any(doc["kind"] == "paper" and "Measurement methods" in doc["text"] for doc in documents))
            self.assertTrue(any(doc["file"] == source.name and doc["kind"] == "attachment" for doc in documents))
            note, = (doc for doc in documents if doc["file"] == "column_notes.docx")
            return {entries[0]["case"]["id"]: {"action": "confirm_unit", "unit": "nmol/mol",
                                                "evidence": [{"ref": note["id"], "quote": quote}]}}

        with (patch("app.review_workbench.paper_review._pdf_pages", return_value=((1, "Measurement methods"),)),
              patch.object(review, "_ask", side_effect=answer) as model):
            summary = review.run(all_rules=True, jev_recheck_only=True)
        model.assert_called_once()
        self.assertEqual((summary["cases"], summary["decision_rows"]), ({"verified": 1}, 1))

    def test_full_export_skips_unit_model_when_no_other_study_material_is_available(self):
        import main
        from app.core.config import settings

        current = Path.cwd()
        with tempfile.TemporaryDirectory(prefix="full-review-") as staging:
            try:
                os.chdir(staging)
                source = Path("dataset/case/data.csv")
                source.parent.mkdir(parents=True)
                source.write_text("Year CE,Sr/Ca (mmol/mol),d18O-HYD3,d18Osw-HYD3,d18O-HYD3-anom\n"
                                  "2000,8.9,-4.2,0.1,0.1\n2001,9.1,-4.5,0.2,0.2\n", encoding="utf-8")
                with (patch.object(settings, "LLM_API_KEY", "fixture"), patch.object(settings, "JUDGE_BACKEND", "off"),
                      patch("app.core.moa.llm_parser.call_llm", return_value=""),
                      patch("app.review_workbench.paper_review.PaperReview._ask") as model):
                    batch = Path(main.run_moa_pipeline())
                    model.assert_not_called()
                summary = json.loads((batch / "review_workbench/max_review.json").read_text(encoding="utf-8"))
                self.assertEqual((summary["model_review_scope"], summary["decision_rows"], summary["model_calls"]),
                                 ("jev_failed_recheck_and_study_units", 0, 0))
                with (batch / "review_queue.csv").open(encoding="utf-8-sig", newline="") as stream:
                    self.assertTrue(any(row["column"] == "d18O-HYD3" and row["action"] == "暂缓该值"
                                        for row in csv.DictReader(stream)))
                with (batch / "consolidated/all_data_combined.csv").open(encoding="utf-8-sig", newline="") as stream:
                    observations = list(csv.DictReader(stream))
                self.assertEqual([float(row["Sr/Ca (mmol/mol)"]) for row in observations], [8.9, 9.1])
                self.assertTrue(all(not row.get("δ18O (‰ VPDB)") for row in observations))
                lipd = json.loads((batch / "paleoclimate_data.lpd").read_text(encoding="utf-8"))
                self.assertEqual(len(lipd["paleoData"]), 2)
                qc = json.loads((batch / "quality_reports/qc_log.json").read_text(encoding="utf-8"))
                if qc["export_status"]["netcdf"] == "written":
                    import xarray as xr
                    with xr.open_dataset(batch / "paleoclimate_grid.nc", decode_times=False) as grid:
                        self.assertEqual(set(grid.observation_id.values), {row["observation_id"] for row in observations})
            finally:
                os.chdir(current)

    def test_failed_jev_recheck_reads_originals_and_replays_verified_mapping(self):
        import main
        from app.core.config import settings
        from app.core.moa.workflow import MOAWorkflow
        from app.review_workbench.paper_review import PaperReview

        cwd = Path.cwd()
        doi = "10.1234/paper"
        quote = "Column ProxyZ contains skeletal oxygen isotope δ18O values reported in ‰ VPDB."
        with tempfile.TemporaryDirectory(prefix="jev-paper-review-") as staging:
            try:
                os.chdir(staging)
                source = Path("dataset/paper/data.csv")
                source.parent.mkdir(parents=True)
                source.write_text("Year CE,Sr/Ca (mmol/mol),ProxyZ\n2000,8.9,-4.2\n2001,9.1,-4.5\n", encoding="utf-8")
                source.with_name("paper.pdf").write_bytes(b"local paper fixture")
                source.with_name("table_notes.txt").write_text("Supplemental column definitions\n" +
                    "".join(f"{i},{i * 10}\n" for i in range(1, 1902)) + quote + "\n", encoding="utf-8")

                def initial(table, *_):
                    targets = {"Year CE": "year", "Sr/Ca (mmol/mol)": "Sr/Ca_value", "ProxyZ": "δ13C_value"}
                    return {"mapped_data": [{"original_header": name, "standard_field": targets[name]}
                                            for name in table["headers"] if name in targets]}

                def judge(_, instructions):
                    return {key: 0.35 if "ProxyZ" in statement else 0.95 for key, statement in instructions.items()}

                def max_review(_, entries, docs):
                    answers = {}
                    for entry in entries:
                        case_id = entry["case"]["id"]
                        self.assertEqual((entry["item"]["judge_original_field"], entry["item"]["judge_proposed_field"]),
                                         ("δ13C_value", "δ18O_value"))
                        proof = next((doc for doc in docs if quote in doc["text"] and doc["kind"] == "attachment"), None)
                        if proof and "1900,19000" in proof["text"]:
                            answers[case_id] = {"action": "confirm_mapping", "column": "ProxyZ", "target": "δ18O_value",
                                                "unit": "‰ VPDB", "evidence": [{"ref": proof["id"], "quote": quote}]}
                        elif proof:
                            answers[case_id] = {"action": "request_evidence", "request": {"file": proof["file"],
                                                "first_row": 1900, "last_row": 1903}}
                        else:
                            answers[case_id] = {"action": "needs_human"}
                    return answers

                with (patch.object(settings, "LLM_API_KEY", "fixture"), patch.object(settings, "JUDGE_ESCALATE", True),
                      patch.object(settings, "JUDGE_REVIEW_ONLY", False), patch.object(settings, "JUDGE_PASS_THRESHOLD", .6),
                      patch.object(MOAWorkflow, "_paper_context", return_value={"pub1_doi": doi}),
                      patch("app.core.moa.llm_parser.LLMParsingModule._map_with_model", side_effect=initial),
                      patch("app.core.moa.llm_parser.judge_enabled", return_value=True),
                      patch("app.core.moa.llm_parser.judge_noul_batch", side_effect=judge),
                      patch("app.core.moa.llm_parser.escalate_remap", return_value=("δ18O_value", "重新映射")) as remap,
                      patch("app.review_workbench.paper_review._pdf_pages", return_value=((1, doi), (2, "全文后页"))),
                      patch.object(PaperReview, "_ask", autospec=True, side_effect=max_review) as model):
                    first = Path(main.run_moa_pipeline())
                    self.assertGreater(model.call_count, 1)
                    self.assertTrue(any("全文后页" in doc["text"] for call in model.call_args_list for doc in call.args[-1]))
                    remap.assert_called_once()
                    model.reset_mock()
                    second = Path(main.run_moa_pipeline())
                    model.assert_not_called()
                for batch, applied, reused in ((first, 1, 0), (second, 0, 1)):
                    summary = json.loads((batch / "review_workbench/max_review.json").read_text(encoding="utf-8"))
                    self.assertEqual((summary["decision_rows"], summary["reused_decisions"]), (applied, reused), summary)
                    with (batch / "consolidated/all_data_combined.csv").open(encoding="utf-8-sig", newline="") as f:
                        rows = list(csv.DictReader(f))
                    self.assertEqual([float(row["δ18O (‰ VPDB)"]) for row in rows], [-4.2, -4.5])
                    self.assertEqual([float(row["Sr/Ca (mmol/mol)"]) for row in rows], [8.9, 9.1])
            finally:
                os.chdir(cwd)

    def test_paper_review_prioritizes_linked_definition_over_wide_sibling_table(self):
        from app.review_workbench.paper_review import PaperReview
        from app.utils.review_queue import save_review_queue

        quote = "Column ProxyZ contains skeletal oxygen isotope δ18O values reported in ‰ VPDB."
        with tempfile.TemporaryDirectory() as staging:
            root = Path(staging)
            source = root / "study" / "b_observations.csv"
            source.parent.mkdir()
            source.write_text("Year CE,ProxyZ\n2000,-4.2\n2001,-4.5\n", encoding="utf-8")
            source.with_name("a_wide_table.txt").write_text(
                "".join(f"Unrelated group description {row} " + "x" * 720 + "\n" for row in range(200)),
                encoding="utf-8",
            )
            source.with_name("z_definitions.txt").write_text(quote + "\n", encoding="utf-8")
            save_review_queue(str(root / "run"), [{
                "file": str(source.relative_to(root)), "sheet": "default", "column": "ProxyZ",
                "action": "暂缓映射，复判未通过", "reason": "复判未通过", "confidence": 0.35,
                "decision": "qwen:δ13C_value / deepseek:δ18O_value / 复判:0.400",
                "judge_original_field": "δ13C_value", "judge_proposed_field": "δ18O_value",
                "judge_recheck_score": 0.4,
            }])

            def ask(entries, docs):
                self.assertTrue(any(doc["kind"] == "source" and "2000 | -4.2" in doc["text"] for doc in docs))
                proof = next(doc for doc in docs if quote in doc["text"])
                noise = next((index for index, doc in enumerate(docs)
                              if "a_wide_table.txt" in doc["file"]), len(docs))
                self.assertLess(docs.index(proof), noise)
                return {entries[0]["case"]["id"]: {
                    "action": "confirm_mapping", "column": "ProxyZ", "target": "δ18O_value",
                    "evidence": [{"ref": proof["id"], "quote": quote}],
                }}

            review = PaperReview(root / "run", root)
            with (patch("app.review_workbench.paper_review.MAX_ORIGINAL_REVIEW_CHARS", 80000),
                  patch.object(review, "_ask", side_effect=ask) as model):
                result = review.run(jev_recheck_only=True)
            model.assert_called_once()
            self.assertEqual(result["cases"], {"verified": 1})
            decision, = review.decisions.values()
            self.assertEqual((decision["decision"], decision["confirmed_unit"]), ("δ18O_value", "‰ VPDB"))
            self.assertTrue(decision["evidence_file"].endswith("z_definitions.txt"))

    def test_discarded_mapping_decision_persists_and_is_reused(self):
        import main
        from app.core.config import settings
        from app.core.moa.workflow import MOAWorkflow
        from app.review_workbench.paper_review import DISCARD_LEDGER, PaperReview
        from app.utils.review_queue import load_unit_review_decisions

        cwd = Path.cwd()
        with tempfile.TemporaryDirectory(prefix="jev-discard-") as staging:
            try:
                os.chdir(staging)
                source = Path("dataset/paper/data.csv")
                source.parent.mkdir(parents=True)
                source.write_text("Year CE,Sr/Ca (mmol/mol),ProxyZ\n2000,8.9,-4.2\n2001,9.1,-4.5\n", encoding="utf-8")
                source.with_name("paper.pdf").write_bytes(b"local paper fixture")
                source.with_name("table_notes.txt").write_text("Supplemental column definitions\nProxyZ opaque\n",
                                                               encoding="utf-8")

                def initial(table, *_):
                    targets = {"Year CE": "year", "Sr/Ca (mmol/mol)": "Sr/Ca_value", "ProxyZ": "δ13C_value"}
                    return {"mapped_data": [{"original_header": name, "standard_field": targets[name]}
                                            for name in table["headers"] if name in targets]}

                def judge(_, instructions):
                    return {key: 0.35 if "ProxyZ" in statement else 0.95 for key, statement in instructions.items()}

                def max_review(_, entries, docs):
                    # Max 读完原件仍无法证实→needs_human（带理由）→证据化舍弃
                    return {e["case"]["id"]: {"action": "needs_human", "reason": "原件无法证实该列指标"}
                            for e in entries}

                with (patch.object(settings, "LLM_API_KEY", "fixture"), patch.object(settings, "JUDGE_ESCALATE", True),
                      patch.object(settings, "JUDGE_REVIEW_ONLY", False), patch.object(settings, "JUDGE_PASS_THRESHOLD", .6),
                      patch.object(MOAWorkflow, "_paper_context", return_value={"pub1_doi": "10.1234/paper"}),
                      patch("app.core.moa.llm_parser.LLMParsingModule._map_with_model", side_effect=initial),
                      patch("app.core.moa.llm_parser.judge_enabled", return_value=True),
                      patch("app.core.moa.llm_parser.judge_noul_batch", side_effect=judge),
                      patch("app.core.moa.llm_parser.escalate_remap", return_value=("δ18O_value", "重新映射")),
                      patch("app.review_workbench.paper_review._pdf_pages", return_value=((1, "正文"),)),
                      patch.object(PaperReview, "_ask", autospec=True, side_effect=max_review) as model):
                    first = Path(main.run_moa_pipeline())
                    self.assertGreater(model.call_count, 0)
                    model.reset_mock()
                    second = Path(main.run_moa_pipeline())
                    model.assert_not_called()  # 舍弃裁决跨批次复用，不再重审
                # 首批：舍弃裁决落盘 + finding 安全回灌(applied) + 舍弃账本
                first_decisions = load_unit_review_decisions(first / "review_workbench" / "max_decisions.csv")
                self.assertEqual(len(first_decisions), 1)
                dec, = first_decisions.values()
                self.assertEqual((dec["action"], dec["origin"]), ("exclude", "evidence_discard"))
                first_summary = json.loads((first / "review_workbench/max_review.json").read_text(encoding="utf-8"))
                self.assertEqual(first_summary["reused_decisions"], 0)
                finding, = first_summary["findings"].values()
                self.assertEqual(finding["state"], "applied")
                self.assertIn(str(source), finding["reviewed_files"])
                self.assertEqual(finding["unreadable_files"], [])
                self.assertTrue((first / "review_workbench" / DISCARD_LEDGER).is_file())
                # 次批：复用既有裁决（不重复写盘），舍弃持续生效
                second_summary = json.loads((second / "review_workbench/max_review.json").read_text(encoding="utf-8"))
                self.assertEqual(second_summary["reused_decisions"], 1)
                for batch in (first, second):
                    with (batch / "consolidated/all_data_combined.csv").open(encoding="utf-8-sig", newline="") as f:
                        rows = list(csv.DictReader(f))
                    self.assertEqual([float(row["Sr/Ca (mmol/mol)"]) for row in rows], [8.9, 9.1])
                    self.assertTrue(all(not row.get("δ18O (‰ VPDB)") and not row.get("δ13C (‰)") for row in rows))
            finally:
                os.chdir(cwd)

    def test_oversize_originals_do_not_block_other_paper_reviews(self):
        from app.review_workbench.paper_review import PaperReview
        from app.utils.review_queue import save_review_queue

        root = Path(self._testMethodName).resolve()
        root.mkdir(exist_ok=True)
        # 单文件上限只针对论文 PDF；超大 CSV 源表不再被跳过
        too_large = root / "first" / "supplementary_source.pdf"
        usable = root / "second" / "source.csv"
        for path in (too_large, usable):
            path.parent.mkdir()
        too_large.write_bytes(b"%PDF-1.4\n" + b"oversize paper fixture\n" * 20)
        usable.write_text("Year CE,ProxyZ\n2000,-4.2\n", encoding="utf-8")
        # 附件不受单文件上限，但超过语料总预算仍会被跳过
        usable.with_name("large-appendix.txt").write_text("attachment\n" * 3_200_000, encoding="utf-8")
        usable.with_name("small-appendix.txt").write_text("Small attachment for original evidence.\n", encoding="utf-8")
        save_review_queue(str(root), [{"file": str(path), "sheet": "default", "column": "ProxyZ",
                                       "action": "暂缓映射，复判未通过", "reason": "复判分数低于放行阈值"}
                                      for path in (too_large, usable)])

        def model(_, entries, docs):
            self.assertEqual([entry["path"] for entry in entries], [usable])
            self.assertTrue(any(doc["file"].endswith("small-appendix.txt") for doc in docs))
            self.assertFalse(any(doc["file"].endswith("large-appendix.txt") for doc in docs))
            return {entries[0]["case"]["id"]: {"action": "needs_human"}}

        with (patch("app.review_workbench.paper_review.MAX_ORIGINAL_FILE_BYTES", 128),
              patch.object(PaperReview, "_ask", autospec=True, side_effect=model) as ask):
            report = PaperReview(root, root).run(max_papers=0, workers=2)
        self.assertEqual(ask.call_count, 1)
        self.assertEqual(report["cases"], {"pending": 2})
        findings = {row["file"]: row for row in report["findings"].values()}
        self.assertIn("已跳过", findings[str(too_large)]["reason"])
        self.assertTrue(any("large-appendix.txt" in file for file in findings[str(usable)]["unreadable_files"]))

    def test_large_attachment_source_is_not_skipped_by_file_limit(self):
        import hashlib
        from app.review_workbench.paper_review import PaperReview
        from app.utils.review_queue import save_review_queue

        root = Path(self._testMethodName).resolve()
        root.mkdir(exist_ok=True)
        source = root / "source.csv"
        source.write_text("Year CE,d18O-HYD3\n" + "2000,-4.2\n" * 40, encoding="utf-8")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        save_review_queue(str(root), [{"file": source.name, "sheet": "default", "column": "d18O-HYD3",
                                       "action": "暂缓该值", "reason": "源单位或同位素参考标尺缺失",
                                       "decision": "δ18O_value", "review_id": "r1", "source_sha256": digest,
                                       "source_unit": "per mil"}])
        review = PaperReview(root, root, rules_only=True)
        with patch("app.review_workbench.paper_review.MAX_ORIGINAL_FILE_BYTES", 128):
            entry = review.prepare(review.store.order[0], source)
        self.assertNotIn("missing", entry)

    def test_paper_review_only_reissues_missing_cases_after_a_valid_response(self):
        from app.review_workbench.paper_review import PaperReview
        from app.utils.review_queue import save_review_queue

        source = Path("source.csv")
        source.write_text("Year CE,OpaqueA,OpaqueB\n2000,1,2\n", encoding="utf-8")
        for name, partial in (("no-response", False), ("partial-response", True)):
            with self.subTest(name=name):
                run = Path(name)
                save_review_queue(str(run), [
                    {"file": source.name, "sheet": "default", "column": column,
                     "decision": "δ18O_value", "action": "暂缓映射，复判未通过",
                     "reason": "复判分数低于放行阈值"} for column in ("OpaqueA", "OpaqueB")
                ])
                asked = []

                def answer(_, question, **kwargs):
                    cases = json.loads(question)["cases"]
                    asked.append(len(cases))
                    if not partial:
                        return ""
                    return json.dumps({"results": [{"id": cases[0]["id"], "action": "needs_human",
                                                     "reason": "原表无法确认指标"}]})

                with patch("app.review_workbench.paper_review.call_llm", side_effect=answer):
                    summary = PaperReview(run, Path.cwd()).run()
                self.assertEqual(asked, [2, 1] if partial else [2])
                self.assertEqual(summary["cases"], {"discarded": 2} if partial else {"pending": 2})

    def test_complete_unresolved_mapping_is_discarded_and_incomplete_reply_can_retry(self):
        import hashlib
        from app.review_workbench.paper_review import DISCARD_LEDGER, PaperReview
        from app.review_workbench.store import ReviewStore
        from app.utils.review_queue import load_unit_review_decisions, save_review_queue

        root = Path(self._testMethodName).resolve()
        original = root / "old"
        source = root / "source.csv"
        root.mkdir(exist_ok=True)
        source.write_text("Year CE,OpaqueA,OpaqueB\n2000,1,2\n", encoding="utf-8")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        pending = [{"file": str(source), "sheet": "default", "column": column, "source_sha256": digest,
                    "decision": "qwen:δ18O_value / deepseek:δ13C_value / 复判:0.300",
                    "confidence": .2, "judge_original_field": "δ18O_value", "judge_proposed_field": "δ13C_value",
                    "judge_recheck_score": .3, "action": "暂缓映射，复判未通过", "reason": "复判分数低于放行阈值"}
                   for column in ("OpaqueA", "OpaqueB")]
        save_review_queue(str(original), pending)

        def answers(_, entries, docs):
            self.assertTrue(any(doc["kind"] == "attachment" and doc["file"] == source.name for doc in docs))
            return {e["case"]["id"]: {"action": "needs_human", "reason": "原件无法证实该列指标"}
                    if e["case"]["column"] == "OpaqueA" else {"action": "needs_human"} for e in entries}

        with patch.object(PaperReview, "_ask", autospec=True, side_effect=answers):
            report = PaperReview(original, root).run(max_papers=0, jev_recheck_only=True)
        # OpaqueA：Max 读完原件仍无法证实→证据化舍弃（可逆排除）；OpaqueB：答复缺理由→pending 可重试
        self.assertEqual(report["cases"], {"discarded": 1, "pending": 1})
        decisions = load_unit_review_decisions(original / "review_workbench" / "max_decisions.csv")
        self.assertEqual(len(decisions), 1)
        discard, = decisions.values()
        self.assertEqual((discard["action"], discard["origin"], discard["source_sha256"]),
                         ("exclude", "evidence_discard", digest))
        self.assertTrue((original / "review_workbench" / DISCARD_LEDGER).is_file())
        store = ReviewStore(original, root)
        # 舍弃组退出"待人工"视图（finding=discarded），仅 pending 组留待重试
        self.assertEqual(store.overview()["paper_cases"], 0)
        states = {row["column"]: row["state"] for row in report["findings"].values()}
        self.assertEqual(states, {"OpaqueA": "discarded", "OpaqueB": "pending"})

    def test_evidence_discard_unit_case_and_unreadable_material_guard(self):
        from app.review_workbench.paper_review import PaperReview
        from app.utils.review_queue import load_unit_review_decisions, save_review_queue

        root = Path(self._testMethodName).resolve()
        root.mkdir(exist_ok=True)
        queue = [{"file": "observations.csv", "sheet": "default", "column": "Ba/Ca-HYD3",
                  "action": "暂缓该值", "reason": "Ba/Ca 的 nmol/mol 档案声明需与论文或补充数据交叉核对",
                  "decision": "Ba/Ca_value", "review_id": "r1", "source_unit": "nmol/mol"}]

        def answer(entries, documents):
            return {e["case"]["id"]: {"action": "needs_human", "reason": "原件未给出可核实的单位声明"}
                    for e in entries}

        # 场景1：Max 读完正文与附件仍无法核实→证据化舍弃（可逆 exclude）
        readable = root / "readable"
        readable.mkdir(parents=True)
        (readable / "observations.csv").write_text("Year CE,Ba/Ca-HYD3\n2000,4500\n", encoding="utf-8")
        (readable / "paper.pdf").write_bytes(b"paper fixture")
        save_review_queue(str(readable), queue)
        review = PaperReview(readable, readable)
        with (patch("app.review_workbench.paper_review._pdf_pages", return_value=((1, "Measurement methods"),)),
              patch.object(review, "_ask", side_effect=answer)):
            summary = review.run(all_rules=True, jev_recheck_only=True)
        self.assertEqual(summary["cases"], {"discarded": 1})
        decisions = load_unit_review_decisions(readable / "review_workbench" / "max_decisions.csv")
        dec, = decisions.values()
        self.assertEqual((dec["action"], dec["origin"]), ("exclude", "evidence_discard"))

        # 场景2：正文不可读（扫描/超限）→pending 待重试，绝不舍弃（读不出≠不该存在）
        unreadable = root / "unreadable"
        unreadable.mkdir(parents=True)
        (unreadable / "observations.csv").write_text("Year CE,Ba/Ca-HYD3\n2000,4500\n", encoding="utf-8")
        (unreadable / "paper.pdf").write_bytes(b"paper fixture")
        save_review_queue(str(unreadable), queue)
        review = PaperReview(unreadable, unreadable)
        with (patch("app.review_workbench.paper_review._pdf_pages", return_value=((1, ""),)),
              patch.object(review, "_ask", side_effect=answer)):
            summary = review.run(all_rules=True, jev_recheck_only=True)
        self.assertEqual(summary["cases"], {"pending": 1})
        self.assertEqual(load_unit_review_decisions(unreadable / "review_workbench" / "max_decisions.csv"), {})

        # 场景3：正文可读，但其中一页是扫描表，仍不能作为已读完原件。
        mixed = root / "mixed"
        mixed.mkdir(parents=True)
        (mixed / "observations.csv").write_text("Year CE,Ba/Ca-HYD3\n2000,4500\n", encoding="utf-8")
        (mixed / "paper.pdf").write_bytes(b"paper fixture")
        save_review_queue(str(mixed), queue)
        review = PaperReview(mixed, mixed)
        with (patch("app.review_workbench.paper_review._pdf_pages",
                    return_value=((1, "Measurement methods"), (2, None))),
              patch.object(review, "_ask", side_effect=answer)):
            summary = review.run(all_rules=True, jev_recheck_only=True)
        self.assertEqual(summary["cases"], {"pending": 1})
        finding, = summary["findings"].values()
        self.assertEqual(finding["unreadable_files"], ["paper.pdf（第 2 页仅有图像）"])

    def test_paper_mapping_can_verify_metric_while_leaving_the_scale_pending(self):
        from app.review_workbench.paper_review import PaperReview
        from app.utils.review_queue import get_review_items, load_unit_review_decisions, save_review_queue

        root = Path(self._testMethodName).resolve()
        source = root / "source.csv"
        source.write_text("AD,d18O\n2000,-4.2\n", encoding="utf-8")
        save_review_queue(str(root), [{"file": str(source), "sheet": "default", "column": "d18O",
                                     "action": "暂缓映射，复判未通过", "reason": "复判分数低于放行阈值"}])
        review = PaperReview(root, root, rules_only=True)
        entry = review.prepare(review.store.order[0], source)
        self.assertTrue(review._verify(entry, {"action": "confirm_mapping", "target": "δ18O_value",
                                              "column": "d18O", "unit": "unknown"}, {}))
        review.publish(1)
        decisions = load_unit_review_decisions(root / "review_workbench" / "max_decisions.csv")
        workflow = type(self.workflow)(output_dir=root, review_decisions=decisions)
        with patch("app.core.moa.llm_parser.call_llm", return_value=""):
            self.assertEqual(workflow.process_file(source), [])
        self.assertTrue(any(row["reason"] == "源单位或同位素参考标尺缺失" for row in get_review_items()))
        self.assertEqual(workflow.applied_unit_reviews, set(decisions))

    def test_paper_review_keeps_later_groups_after_a_second_evidence_pass(self):
        from app.review_workbench.paper_review import PaperReview

        entries = [{"case": {"id": str(index), "action": "select_time_column", "category": "time"}}
                   for index in range(17)]
        review = PaperReview.__new__(PaperReview)

        with (patch.object(review, "_materials", return_value=[]),
              patch.object(review, "_review_originals") as originals):
            review._review_with_model(entries)
        self.assertEqual([len(call.args[0]) for call in originals.call_args_list], [8, 8, 1])
        self.assertEqual([entry for call in originals.call_args_list for entry in call.args[0]], entries)

    def test_full_review_default_covers_more_than_twelve_papers(self):
        from app.core.config import settings
        from app.core.moa.full_review import review_before_export
        from app.review_workbench.paper_review import PaperReview

        root = Path(self._testMethodName).resolve()
        root.mkdir(exist_ok=True)
        workflow = type(self.workflow)(output_dir=str(root), max_workers=3)
        for index in range(13):
            source = root / f"paper{index}" / "data.csv"
            source.parent.mkdir()
            source.write_text("Year,d18O\n2000,-4.2\n", encoding="utf-8")
            workflow._review_items.append({"file": str(source), "sheet": "default", "column": "d18O",
                                           "reason": "复判分数低于放行阈值", "decision": "δ18O_value",
                                           "confidence": .4, "judge_recheck_score": .5,
                                           "judge_original_field": "δ18O_value", "judge_proposed_field": "δ18O_value",
                                           "action": "暂缓映射，复判未通过"})
        # 无复判分数和达到阈值的记录都不进入自动 Max。
        for index, score in enumerate((None, .6), 13):
            source = root / f"paper{index}" / "data.csv"
            source.parent.mkdir()
            source.write_text("Year,d18O\n2000,-4.2\n", encoding="utf-8")
            workflow._review_items.append({**workflow._review_items[0], "file": str(source), "judge_recheck_score": score})

        def no_result(review, entries):
            for entry in entries:
                review._report(entry, "manual", "证据不足")

        with patch.object(settings, "LLM_API_KEY", "fixture"), patch.object(
            PaperReview, "_review_with_model", autospec=True, side_effect=no_result
        ) as model:
            summary, _ = review_before_export(workflow, "dataset", {}, {}, source_root=root, use_parallel=True)
            self.assertEqual(model.call_count, 13)
            self.assertEqual(summary["cases"], {"manual": 13})
            summary, _ = review_before_export(workflow, "dataset", {}, {}, source_root=root, max_papers=1)
            self.assertEqual(summary["status"], "partial")
            self.assertEqual(summary["cases"], {"manual": 1, "pending": 12})

        def fail_one(review, entries):
            if Path(entries[0]["item"]["file"]).parent.name == "paper0":
                raise TypeError("fixture: unexpected model-review worker error")
            no_result(review, entries)

        with patch.object(settings, "LLM_API_KEY", "fixture"), patch.object(
            PaperReview, "_review_with_model", autospec=True, side_effect=fail_one
        ):
            summary, _ = review_before_export(workflow, "dataset", {}, {}, source_root=root, use_parallel=True)
        self.assertEqual(summary["status"], "partial")
        self.assertEqual(summary["cases"], {"manual": 12, "pending": 1})

    def test_workbench_paper_handoff_is_an_open_queue_subset(self):
        from app.review_workbench.paper_review import _write_report
        from app.review_workbench.store import ReviewStore
        from app.utils.review_queue import save_review_queue

        root = Path(self._testMethodName).resolve()
        root.mkdir(exist_ok=True)
        save_review_queue(str(root), [
            {"file": "a.txt", "action": "暂缓该值", "reason": "源单位或同位素参考标尺缺失"},
            {"file": "b.txt", "action": "暂缓该值", "reason": "源单位或同位素参考标尺缺失"},
            {"file": "paper.pdf", "action": "已排除非观测片段", "reason": "PDF 线框没有数据"},
            {"file": "other.xlsx", "action": "暂缓表格", "reason": "尚需检查原表"},
        ])
        store = ReviewStore(root, root)
        keys = {case["file"]: case["id"] for case in store.order}
        _write_report(store.work_dir, {"batch": root.name, "findings": {
            keys["a.txt"]: {"state": "manual"}, keys["b.txt"]: {"state": "pending"},
        }})
        overview = store.overview()
        self.assertEqual((overview["open_cases"], overview["open_records"]), (3, 3))
        self.assertEqual((overview["paper_cases"], overview["paper_records"]), (1, 1))
        self.assertEqual([case["file"] for case in store.query(status="paper")["cases"]], ["a.txt"])

    def test_pdf_context_filter_preserves_candidate_measurements(self):
        only_context = self.workflow.data_ingestion._pdf_context_only
        self.assertTrue(only_context([[None, ""], [None, ""]]))
        self.assertTrue(only_context([["Funded by grant ABC-123", None], ["Authors' acknowledgements", None]]))
        self.assertFalse(only_context([["Date", "Sr/Ca"], ["1999.5", "9.02"]]))
        self.assertFalse(only_context([["Sample X", "Ba/Ca μmol/mol"], ["core A", "Result: 2.0"]]))
        self.assertFalse(only_context([["core A", "−5.1 ± 0.2"]]))

    def test_archive_review_separates_raw_series_from_same_core_annual_mean(self):
        from app.review_workbench.paper_review import _archive_choice

        raw, mean = "d18O-HYD3-monthly", "d18O-HYD3-MeanAnnual"
        variables = {
            name: {"name": name, "quantity": "delta 18O", "material": "Porites",
                   "definition_valid": True, "seasonality": frequency, "full_definition": name}
            for name, frequency in ((raw, "monthly"), (mean, "annual"))
        }
        case = {"action": "select_column", "target": "δ18O_value", "candidates": [raw, mean]}
        self.assertEqual(_archive_choice(case, variables, [raw, mean])[0], raw)
        different_core = "d18O-HYD4-MeanAnnual"
        variables[different_core] = {**variables[mean], "name": different_core}
        self.assertIsNone(_archive_choice({**case, "candidates": [raw, different_core]},
                                          variables, [raw, different_core]))

    def test_nmolar_archive_units_keep_original_label(self):
        field = "Ba/Ca_value"
        result = self.resolve_unit(field, "Ba/Ca", (("archive_definition", "nanomole per mole"),))
        self.assertEqual(result["unit"], "nmol/mol")
        point = self.UnitConverter().convert_units({field: 3500, f"{field}_unit_resolution": result}, {})
        self.assertEqual(point[field], 3.5)
        self.assertEqual(point[f"{field}_original_unit"], "nanomole per mole")
        raw = Path("baca.txt")
        raw.write_text("源文件", encoding="utf-8")
        pending = self.workflow._finalize_measurements(
            {field: 3500, f"{field}_unit_resolution": result, "source_file": str(raw), "year": 2000})
        self.assertIsNone(pending[field])
        self.assertEqual(pending[f"{field}_unit_status"], "unit_conflict")

    def test_incremental_batch_retains_old_values_and_review_queue(self):
        from app.core.moa.patch_apply import COMBINED, _merge_batch, _path, _rows, _safe_replacements, _write_rows

        base = Path(self._testMethodName)
        old, delta, merged = [base / name for name in ("old", "delta", "merged")]
        fields = ["data_id", "observation_id", "source_file", "Sr/Ca (mmol/mol)", "δ18O (‰ VPDB)",
                  "year_CE", "geo_latitude"]
        original = [
            dict(zip(fields, ("combined_00001", "a", "noaa/a.txt", 9.1, "", 1900, 10))),
            dict(zip(fields, ("combined_00002", "b", "noaa/b.txt", 9.2, "", 1900, 10))),
            dict(zip(fields, ("combined_00003", "c", "noaa/c.txt", 9.3, "", 1900, 10))),
        ]
        updated = [{**original[0], "δ18O (‰ VPDB)": -4.5},
                   {**original[1], "Sr/Ca (mmol/mol)": 9.7}]
        for root, rows in ((old, original), (delta, updated)):
            _write_rows(root / COMBINED, fields, rows)
            _write_rows(root / "INDEX.csv", ["path", "record_count"],
                        [{"path": COMBINED, "record_count": len(rows)}])
            _write_rows(root / "review_queue.csv", ["file", "reason"],
                        [{"file": row["source_file"], "reason": root.name} for row in rows
                         if root == old or row["source_file"] != "noaa/a.txt"])
        inventory = [{"file": row["source_file"], "sheet": "", "status": "extracted"} for row in updated]
        sources, rejected = _safe_replacements(old, delta, inventory)
        self.assertEqual(sources, {_path("noaa/a.txt")})
        self.assertEqual(rejected, [_path("noaa/b.txt")])
        self.assertEqual(_merge_batch(old, delta, merged, sources), 3)
        result = {row["observation_id"]: row for row in _rows(merged / COMBINED)}
        self.assertEqual(result["a"]["δ18O (‰ VPDB)"], "-4.5")
        self.assertEqual(result["b"]["Sr/Ca (mmol/mol)"], "9.2")
        self.assertEqual({row["file"] for row in _rows(merged / "review_queue.csv")}, {"noaa/b.txt", "noaa/c.txt"})
        _write_rows(delta / COMBINED, fields, [{**updated[0], "year_CE": 1901, "geo_latitude": 11}, updated[1]])
        self.assertEqual(_safe_replacements(old, delta, inventory)[0], set())
        approved = {action: {"action": action, "source_file": "noaa/a.txt", "evidence_status": "verified"}
                    for action in ("select_time_column", "confirm_coordinates")}
        self.assertEqual(_safe_replacements(old, delta, inventory, approved)[0], {_path("noaa/a.txt")})

    def test_sst_and_baca_only_export_and_generic_temperature_is_held(self):
        with patch("app.core.moa.llm_parser.call_llm", return_value=""):
            for header, value, expected in (("SST (°C)", 27, "sea_surface_temperature_value"),
                                            ("Ba/Ca (μmol/mol)", 2, "Ba/Ca_value")):
                path = Path(f"{expected.replace('/', '_')}.csv")
                path.write_text(f"Year CE,{header}\n2000,{value}\n", encoding="utf-8")
                point, = self.workflow.process_file(path)
                self.assertEqual(point[expected], value)
                self.workflow.processed_data.append(point)
        path = Path("lab.csv")
        path.write_text("Year CE,Sr/Ca (mmol/mol),Lab Temperature (°C)\n2001,9.1,25\n", encoding="utf-8")
        response = json.dumps({"mapped_data": [
            {"original_header": header, "standard_field": field} for header, field in
            (("Year CE", "year"), ("Sr/Ca (mmol/mol)", "Sr/Ca_value"),
             ("Lab Temperature (°C)", "sea_surface_temperature_value"))
        ]})
        with patch("app.core.moa.llm_parser.call_llm", return_value=response):
            point, = self.workflow.process_file(path)
        self.assertIsNone(point.get("sea_surface_temperature_value"))
        run = Path(self.workflow.generate_final_output())
        with (run / "consolidated" / "all_data_combined.csv").open(encoding="utf-8-sig", newline="") as file:
            rows = list(csv.DictReader(file))
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["SST (°C)"] for row in rows], ["27.0", ""])
        self.assertEqual([row["Ba/Ca (μmol/mol)"] for row in rows], ["", "2.0"])

    def test_directory_accounts_for_each_input_and_table_without_pdf(self):
        directory = Path("manifest_input/study")
        directory.mkdir(parents=True)
        good = "Year CE,Sr/Ca (mmol/mol)\n2000,9.1\n"
        (directory / "reference_measurements.csv").write_text(good, encoding="utf-8")
        (directory / "z_copy.csv").write_text(good, encoding="utf-8")
        (directory / "unknown.csv").write_text("T,A01\n2000,9.1\n", encoding="utf-8")
        (directory / "damaged.xlsx").write_bytes(b"not a workbook")
        (directory / "unknown.json").write_text("{}", encoding="utf-8")
        with patch("app.core.moa.llm_parser.call_llm", return_value="") as model:
            rows = self.workflow.process_directory(str(directory.parent), use_parallel=True)
        self.assertEqual(len(rows), 1)
        self.assertTrue(any("A01" in args[0][1] for args in model.call_args_list))
        run = Path(self.workflow.generate_final_output())
        with (run / "input_manifest.csv").open(encoding="utf-8-sig", newline="") as file:
            manifest = list(csv.DictReader(file))
        self.assertEqual({Path(row["file"]).name: row["status"] for row in manifest if not row["sheet"]},
                         {"reference_measurements.csv": "extracted", "z_copy.csv": "excluded",
                          "unknown.csv": "review", "damaged.xlsx": "failed", "unknown.json": "unsupported"})
        self.assertTrue(any(row["sheet"] == "default" and row["output_rows"] == "1" for row in manifest))
        self.assertIn("damaged.xlsx", (run / "review_queue.csv").read_text(encoding="utf-8-sig"))

    def test_pdf_table_coordinates_and_scanned_page_review(self):
        from types import SimpleNamespace
        from PIL import Image
        from app.utils.review_queue import get_review_items

        document = Path("supplementary_tables.pdf")
        table = SimpleNamespace(bbox=(10, 20, 100, 120),
                                extract=lambda: [["Year CE", "Sr/Ca (mmol/mol)"], ["2000", "9.1"]])
        page = SimpleNamespace(page_number=3, chars=[{}], images=[], find_tables=lambda: [table])
        with patch("pdfplumber.open") as pdf, patch("app.core.moa.llm_parser.call_llm", return_value=""):
            pdf.return_value.__enter__.return_value.pages = [page]
            point, = self.workflow.process_file(document)
        self.assertEqual((point["source_page"], point["source_table_bbox"], point["source_line"]),
                         (3, (10, 20, 100, 120), 2))
        with patch("pdfplumber.open") as pdf:
            self.assertEqual(self.workflow.process_file(Path("paper.pdf")), [])
            pdf.assert_not_called()
        self.assertEqual(self.workflow.source_inventory[-1]["status"], "context")
        scan = Path("supplementary_scan.pdf")
        Image.new("RGB", (40, 40), color="white").save(scan)
        self.assertEqual(self.workflow.process_file(scan), [])
        self.assertTrue(any(item["action"] == "暂缓读取" and item["sheet"] == "page_1" for item in get_review_items()))

    def test_default_and_legacy_cli_use_same_workflow(self):
        import main
        import sys

        with patch.object(main, "run_moa_pipeline") as moa:
            for args in (["main.py"], ["main.py", "--mode", "complete"]):
                with patch.object(sys, "argv", args):
                    main.main()
            self.assertEqual(moa.call_count, 2)

    def test_mixed_decisions_publish_with_legacy_archive_identity(self):
        from app.utils.review_queue import load_unit_review_decisions

        source = Path("archive.txt")
        source.write_text("fixture", encoding="utf-8")
        point = {"source_file": str(source), "source_type": "NOAA", "sheet_name": source.name,
                 "source_row": 1, "source_columns": {"δ18O_value": "d18O"}, "year": 2000,
                 "δ18O_value": -4.5, "δ18O_value_source_unit": "‰"}
        legacy_id = self.workflow._unit_review_id(point, "δ18O_value", legacy_archive=True)
        row_file, column_file = Path("mixed_row.csv"), Path("mixed_col.csv")
        row_file.write_text("review_id,review_decision,confirmed_unit,review_note\n"
                            f"{legacy_id},confirm_unit,‰ VPDB,附录明确声明标尺\n"
                            "missing-row,exclude,,核对原表\n", encoding="utf-8")
        column_file.write_text("file,column,review_decision,confirmed_unit,review_note\n"
                               "missing-file.csv,Sr/Ca,exclude,,核对原表\n", encoding="utf-8")
        self.workflow.unit_review_decisions = load_unit_review_decisions([row_file, column_file])
        result = self.workflow._finalize_measurements(point)
        self.assertEqual(result["δ18O_value"], -4.5)
        self.assertEqual(self.workflow.applied_unit_reviews, {legacy_id})
        self.workflow.processed_data = [result]
        run = Path(self.workflow.generate_final_output())
        with (run / "review_queue.csv").open(encoding="utf-8-sig", newline="") as file:
            rows = list(csv.DictReader(file))
        self.assertEqual(len(rows), 2)
        self.assertTrue(any(row["column"] == "sr/ca" for row in rows))

    def test_judge_review_only_never_escalates_and_remap_rechecks_units(self):
        from app.core.config import settings

        table = {"headers": ["X"], "data": [{"X": 8.5}]}
        item = {"original_header": "X", "standard_field": "Sr/Ca_value", "unit": "mmol/mol",
                "unit_resolution": {"unit": "mmol/mol", "status": "resolved"}}
        parser = self.Parser(enable_cache=False)
        with (patch.object(settings, "JUDGE_ESCALATE", True),
              patch.object(settings, "JUDGE_REVIEW_ONLY", True),
              patch("app.core.moa.llm_parser.judge_enabled", return_value=True),
              patch("app.core.moa.llm_parser.judge_noul_batch", return_value={"col_0": 0.1}),
              patch("app.core.moa.llm_parser.escalate_remap") as escalate):
            result = parser._judge_mapped_columns({"mapped_data": [item.copy()]}, table)
            self.assertEqual(result["mapped_data"], [item])
            escalate.assert_not_called()
        with (patch.object(settings, "JUDGE_ESCALATE", True),
              patch.object(settings, "JUDGE_REVIEW_ONLY", False),
              patch("app.core.moa.llm_parser.judge_enabled", return_value=True),
              patch("app.core.moa.llm_parser.judge_noul_batch", side_effect=[{"col_0": 0.1}, {"col_0": 1.0}]),
              patch("app.core.moa.llm_parser.escalate_remap", return_value=("Ba/Ca_value", "重映射"))):
            result = parser._judge_mapped_columns({"mapped_data": [item.copy()]}, table)
        self.assertEqual(result["mapped_data"][0]["standard_field"], "Ba/Ca_value")
        self.assertEqual(result["mapped_data"][0]["unit_resolution"]["status"], "unknown_unit")

    def test_verified_exclude_decision_drops_column_at_mapping_gate(self):
        import hashlib

        from app.utils.review_queue import load_unit_review_decisions
        source = Path("exclude_gate.csv")
        source.write_text("Year,d14C_1sd\n1998,0.1\n", encoding="utf-8")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        table = {"headers": ["Year", "d14C_1sd"], "data": [{"Year": 1998, "d14C_1sd": 0.1}],
                 "file_path": str(source), "sheet_name": source.name}
        item = {"original_header": "d14C_1sd", "standard_field": "δ13C_value", "unit": "‰"}
        keep = {"original_header": "Year", "standard_field": "year", "unit": "CE"}
        decision_file = Path("exclude_gate_decisions.csv")
        decision_file.write_text(
            "file,sheet,column,source_sha256,review_decision,confirmed_unit,review_note,evidence_status\n"
            f"{source},{source.name},d14C_1sd,{digest},exclude,,多信号收敛舍弃,verified\n",
            encoding="utf-8")
        parser = self.Parser(enable_cache=False)
        parser.mapping_decisions = load_unit_review_decisions(decision_file)
        parser.mapping_digest = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
        with patch("app.core.moa.llm_parser.judge_enabled", return_value=False):
            result = parser._judge_mapped_columns({"mapped_data": [keep.copy(), item.copy()]}, table)
        self.assertEqual([it["original_header"] for it in result["mapped_data"]], ["Year"])

    def test_escalation_withholds_unaccepted_columns_from_batch(self):
        from app.core.config import settings

        source = Path("escalation.csv")
        headers = ["Year (CE)", "Sr/Ca (mmol/mol)"] + [f"{label} (‰ VPDB)" for label in "ABCDE"]
        with source.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(headers)
            writer.writerows([(1998, 8.9, -4.4, -4.1, -4.2, -4.3, -4.5),
                              (1999, 9.0, -4.3, -4.0, -4.1, -4.2, -4.4)])
        fields = ["year", "Sr/Ca_value"] + ["δ13C_value"] * 5
        generator = json.dumps({"mapped_data": [
            {"original_header": header, "standard_field": field, "unit": "unknown"}
            for header, field in zip(headers, fields)
        ]})
        with (patch.object(settings, "JUDGE_ESCALATE", True),
              patch.object(settings, "JUDGE_REVIEW_ONLY", False),
              patch.object(settings, "JUDGE_PASS_THRESHOLD", .60),
              patch("app.core.moa.llm_parser.judge_enabled", return_value=True),
              patch("app.core.moa.llm_parser.call_llm", return_value=generator),
              patch("app.core.moa.llm_parser.judge_noul_batch", side_effect=[
                  {"col_0": .60, "col_1": .1, "col_2": .45, "col_3": .2, "col_4": .59, "col_5": .01},
                  {"col_2": .60, "col_3": .59, "col_5": .85},
              ]) as judge,
              patch("app.core.moa.llm_parser.escalate_remap", side_effect=[
                  ("δ18O_value", "重映射"), ("δ18O_value", "提议未通过"), None, ("skip", "建议排除"),
              ]) as remap):
            self.workflow.processed_data = self.workflow.process_file(source)
            run = Path(self.workflow.generate_final_output())

        self.assertEqual([call.args[1] for call in remap.call_args_list], headers[2:6])
        self.assertEqual(set(judge.call_args.args[1]), {"col_2", "col_3", "col_5"})
        with (run / "consolidated" / "all_data_combined.csv").open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual([float(row["δ18O (‰ VPDB)"]) for row in rows], [-4.4, -4.3])
        self.assertEqual([float(row["Sr/Ca (mmol/mol)"]) for row in rows], [8.9, 9.0])
        self.assertTrue(all(not row.get("δ13C (‰)") for row in rows))
        with (run / "review_queue.csv").open(encoding="utf-8-sig", newline="") as stream:
            reviews = {row["column"]: row["action"] for row in csv.DictReader(stream)}
        self.assertEqual(reviews, dict(zip(headers[3:], ["暂缓映射，复判未通过", "暂缓映射，升级失败",
                                                      "暂缓映射，升级建议排除", "暂缓映射，初判缺失"])))
        with (run / "input_manifest.csv").open(encoding="utf-8-sig", newline="") as stream:
            self.assertTrue(all(row["status"] == "partial" for row in csv.DictReader(stream)))

    def test_year_last_dates_require_a_consistent_column_order(self):
        cases = [
            (["1/2/1994", "7/26/1994"], "mdy", 1994 + 1 / 365),
            (["1/2/1994", "26/7/1994"], "dmy", 1994 + 31 / 365),
            (["1/2/1994", "7/8/1994"], None, None),
            (["7/26/1994", "26/7/1994"], None, None),
        ]
        with patch("app.core.moa.llm_parser.call_llm", return_value=""):
            for index, (dates, order, expected) in enumerate(cases):
                with self.subTest(dates=dates):
                    source = Path(f"dates_{index}.csv")
                    source.write_text("Date,Ba/Ca (mol/mol)\n" + "".join(f"{date},0.0000038773\n" for date in dates),
                                      encoding="utf-8")
                    rows = self.workflow.process_file(source)
                    if order is None:
                        self.assertEqual(rows, [])
                    else:
                        self.assertEqual(len(rows), 2)
                        self.assertEqual(rows[0]["source_time_date_order"], order)
                        self.assertEqual(rows[0]["source_time_value"], dates[0])
                        self.assertAlmostEqual(rows[0]["year"], expected)
                        self.assertAlmostEqual(rows[0]["Ba/Ca_value"], 3.8773)

    def test_shared_table_evidence_reaches_mapping_and_rejudge(self):
        from app.core.config import settings

        target = "A01"
        headers = ["Year (CE)", "unrelated", *(f"empty{i}" for i in range(14)), target]
        quote = "A01 skeletal δ18O was measured on the VPDB scale."
        context = {"metadata_evidence": {"d18o_unit": quote}, "default_d18o_unit": "‰ VPDB",
                   "metadata_source": "source.pdf", "paper_link": "publication_doi"}
        table = {"headers": headers, "file_path": "source.csv", "sheet_name": "Sheet 2",
                 "units": {target: "‰ VPDB"}, "defined_units": {target: "‰ VPDB"},
                 "archive_metadata": {"source_time_unit": "year Common Era"},
                 "variable_definitions": {target: {"description": "skeletal oxygen isotope"},
                                          "unrelated": {"description": "Unrelated measurement signature"}},
                 "data": [{"Year (CE)": 1990 + i, target: None if i < 6 else -4.3} for i in range(7)]}
        model_response = json.dumps({"mapped_data": [{"original_header": target, "standard_field": "δ13C_value"}]})
        with (patch.object(settings, "JUDGE_ESCALATE", True), patch.object(settings, "JUDGE_REVIEW_ONLY", False),
              patch.object(settings, "JUDGE_PASS_THRESHOLD", .60),
              patch("app.core.moa.llm_parser.judge_enabled", return_value=True),
              patch("app.core.moa.llm_parser.call_llm", return_value=model_response) as qwen,
              patch("app.core.moa.llm_parser.judge_noul_batch", side_effect=[{"col_0": .45}, {"col_0": .6}]) as judge,
              patch("app.utils.llm_api.call_llm", return_value='{"standard_field":"δ18O_value"}') as remap):
            result = self.Parser(enable_cache=False).parse_table_structure(table, "Table S2", context)

        qwen_prompt = qwen.call_args.args[1]
        remap_prompt = remap.call_args.args[1]
        for prompt in (qwen_prompt, remap_prompt):
            self.assertIn("year Common Era", prompt)
            self.assertIn(quote, prompt)
            self.assertIn('"samples": ["-4.3"]', prompt)
        self.assertIn("Table S2", qwen_prompt)
        self.assertNotIn("Unrelated measurement signature", remap_prompt)
        initial, rejudge = [call.args[0] for call in judge.call_args_list]
        self.assertEqual(initial, rejudge)
        self.assertEqual(rejudge["table_source"]["sheet"], "Sheet 2")
        self.assertEqual(rejudge["column_evidence"][target]["paper_quote_keys"], ["d18o_unit"])
        self.assertEqual(result["mapped_data"][0]["standard_field"], "δ18O_value")

    def test_attachment_publication_doi_prevents_wrong_paper_context(self):
        from contextlib import nullcontext
        from types import SimpleNamespace

        directory = Path("paper_matching")
        directory.mkdir()
        for name in ("correct.pdf", "wrong.pdf"):
            (directory / name).touch()
        for name, doi in (("a_match.csv", "10.1234/correct"), ("b_unmatched.csv", "10.1234/absent")):
            (directory / name).write_text(f"# Publication DOI: {doi}\nYear CE,Sr/Ca (mmol/mol)\n2000,9.1\n", encoding="utf-8")

        def document(path):
            page = SimpleNamespace(extract_text=lambda: f"https://doi.org/10.1234/{Path(path).stem}")
            return SimpleNamespace(pages=[page])

        with (patch("pdfplumber.open") as pdf,
              patch.object(self.workflow, "_paper_context", return_value={"metadata_source": "correct.pdf"}) as paper,
              patch("app.core.moa.llm_parser.call_llm", return_value="")):
            pdf.side_effect = lambda path: nullcontext(document(path))
            rows = self.workflow.process_directory(None, noaa_dir=directory)
        paper.assert_called_once_with(str(directory / "correct.pdf"))
        records = {Path(row["source_file"]).name: row for row in rows}
        self.assertEqual(records["a_match.csv"]["pub1_doi"], "10.1234/correct")
        self.assertIsNone(records["b_unmatched.csv"]["pub1_doi"])
        self.assertTrue(any(item.get("reason") == "附件明确声明的出版物 DOI 未与正文核实"
                            for item in self.workflow._review_items))

    def test_word_layout_uses_raw_cells_and_physical_coordinates(self):
        from docx import Document
        from app.core.config import settings

        path = Path("merged_word.docx")
        document = Document()
        table = document.add_table(rows=5, cols=4)
        table.cell(0, 1).merge(table.cell(0, 3)).text = "Core A"
        table.cell(1, 1).text, table.cell(1, 3).text = "Date", "Sr/Ca"
        table.cell(2, 1).text, table.cell(2, 3).text = "CE", "mmol/mol"
        table.cell(3, 1).text, table.cell(3, 3).text = "1990", "8.9"
        table.cell(4, 1).text, table.cell(4, 3).text = "1991", "9.0"
        document.save(path)
        layout = {"blocks": [{"header_rows": [1, 2], "first_data_row": 4, "last_data_row": 5,
                              "columns": [2, 4], "unit_row": 3}]}
        mapping = {"mapped_data": [{"original_header": "Core A | Date", "standard_field": "year"},
                                   {"original_header": "Core A | Sr/Ca", "standard_field": "Sr/Ca_value"}]}
        with (patch("app.core.moa.table_layout.call_llm", return_value=json.dumps(layout)) as structure,
              patch("app.core.moa.llm_parser.call_llm", return_value=json.dumps(mapping)),
              patch.object(settings, "JUDGE_ESCALATE", False),
              patch("app.core.moa.llm_parser.judge_enabled", return_value=False)):
            result = self.workflow.process_file(path)
        raw = json.loads(structure.call_args.args[1].split("原始网格：", 1)[1])
        self.assertIn([1, 1, 2, 4], raw["merged_ranges"])
        self.assertEqual([p["Sr/Ca_value"] for p in result], [8.9, 9.0])
        self.assertEqual([p["source_line"] for p in result], [4, 5])
        self.assertEqual(result[0]["source_column_positions"], {"Core A | Date": 2, "Core A | Sr/Ca": 4})
        self.assertEqual(result[0]["source_header_rows"], [1, 2])
        self.assertEqual(result[0]["layout_route"], "model_coordinates")

    def test_invalid_layout_cannot_read_or_duplicate_source_cells(self):
        import pandas as pd
        from app.core.moa.data_ingestion import DataIngestionModule

        reader = DataIngestionModule()
        grid = pd.DataFrame([["Date CE", "Sr/Ca (mmol/mol)"], ["1990", "9.0"], ["1991", "9.1"]])
        block = {"header_rows": [1], "first_data_row": 2, "last_data_row": 3, "columns": [1, 2]}
        for plan in ([{**block, "last_data_row": 40}], [block, block], [{**block, "unit_row": 2}]):
            self.assertEqual(reader._apply_layout(grid, plan, "source.xlsx", "sheet", "", True, "", []), [])

    def test_partial_layout_keeps_valid_main_block_and_flags_overlap(self):
        import pandas as pd
        from app.core.moa.data_ingestion import DataIngestionModule
        from app.utils.review_queue import get_review_items

        reader = DataIngestionModule()
        grid = pd.DataFrame([
            ["Year CE", "Sr/Ca", "", "Slope", "Intercept"],
            ["1990", "9.0", "", "0.5", "1.2"],
            ["1991", "9.1", "", "0.6", "1.3"],
            ["1992", "9.2", "", "Slope", "Intercept"],
            ["1993", "9.3", "", "0.7", "1.4"],
        ])
        main = {"header_rows": [1], "first_data_row": 2, "last_data_row": 5, "columns": [1, 2]}
        sub_a = {"header_rows": [1], "first_data_row": 2, "last_data_row": 5, "columns": [4, 5]}
        sub_b = {"header_rows": [4], "first_data_row": 5, "last_data_row": 5, "columns": [4, 5]}
        result = reader._apply_layout(grid, [main, sub_a, sub_b], "source.xlsx", "sheet", "", True, "", [])
        # 主表独立无重叠，照常从原表提取；两个共享单元格的重叠子表单独跳过并留痕。
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["layout_route"], "model_coordinates")
        self.assertEqual(result[0]["headers"], ["Year CE", "Sr/Ca"])
        self.assertEqual([row["Sr/Ca"] for row in result[0]["data"]], ["9.0", "9.1", "9.2", "9.3"])
        flagged = [it for it in get_review_items() if it.get("action") == "暂缓模型结构块"]
        self.assertEqual(len(flagged), 2)
        self.assertTrue(all("重叠" in it["reason"] for it in flagged))

    def test_layout_accepts_valid_blocks_and_tracks_omitted_calendar_rows(self):
        import pandas as pd
        from app.core.moa.data_ingestion import DataIngestionModule
        from app.utils.review_queue import get_review_items

        reader = DataIngestionModule(enable_layout_model=True)
        grid = pd.DataFrame([["Date", "Sr/Ca (mmol/mol)"], ["2000-01-01", "9.0"], ["2000-02-01", "9.1"]])
        main = {"header_rows": [1], "first_data_row": 2, "last_data_row": 2, "columns": [1, 2]}
        plan = json.dumps({"blocks": [main, {**main, "first_data_row": 3, "last_data_row": 20}]})
        with (patch.object(reader, "_needs_layout_model", return_value=True),
              patch("app.core.moa.table_layout.call_llm", return_value=plan)):
            rows = reader._inspect_attachment_table(grid, "source.xlsx", "sheet", workbook=True)
        self.assertEqual([(table["layout_route"], table["source_rows"]) for table in rows], [("model_coordinates", [2])])
        issues = get_review_items()
        self.assertTrue(any(issue["action"] == "暂缓模型结构块" for issue in issues))
        self.assertTrue(any(issue["action"] == "暂缓未覆盖区域" and issue["source_line"] == 3 for issue in issues))

    def test_small_mapping_reports_missing_and_conflicting_original_columns(self):
        from app.core.config import settings
        from app.utils.review_queue import get_review_items

        source = Path("small.csv")
        source.write_text("AD,Sr/Ca (mmol/mol),d18O (VPDB),d13C (VPDB)\n2000,9.1,-4.2,-2\n", encoding="utf-8")
        mappings = [("AD", "year"), ("Sr/Ca (mmol/mol)", "Sr/Ca_value"),
                    ("d18O (VPDB)", "δ18O_value"), ("d18O (VPDB)", "δ13C_value")]
        response = json.dumps({"mapped_data": [{"original_header": name, "standard_field": field}
                                               for name, field in mappings]})
        with (patch.object(settings, "JUDGE_ESCALATE", False),
              patch("app.core.moa.llm_parser.call_llm", return_value=response),
              patch("app.core.moa.llm_parser.judge_enabled", return_value=False)):
            point, = self.workflow.process_file(source)
        self.assertEqual(point["Sr/Ca_value"], 9.1)
        self.assertIsNone(point.get("δ18O_value"))
        issue, = (row for row in get_review_items() if row["action"] == "暂缓列映射")
        self.assertEqual(issue["column"], "d18O (VPDB) | d13C (VPDB)")

    def test_wide_mapping_and_jev_keep_every_target_column(self):
        from app.core.config import settings

        headers = ["Year CE", *(f"field_{i:03}" for i in range(71))]
        table = {"headers": headers, "sheet_name": "Wide", "data": [{name: 1 for name in headers}],
                 "variable_definitions": {name: {"full_definition": "long declaration " * 40} for name in headers}}
        sent, judged = [], []

        def map_batch(model, prompt, **kwargs):
            self.assertLessEqual(len(prompt), 25000)
            wire = prompt.split("按表和列整理的原始证据: ", 1)[1]
            evidence, _ = json.JSONDecoder().raw_decode(wire)
            self.assertIn("Year CE", evidence["table_headers"])
            names = evidence["target_columns"]
            sent.extend(names)
            return json.dumps({"mapped_data": [{"original_header": name,
                                "standard_field": "year" if name == "Year CE" else "paleoData_dating_error"}
                               for name in names]})

        def judge_batch(state, questions):
            self.assertLessEqual(len(json.dumps([state, questions], ensure_ascii=False)), 16000)
            judged.extend(questions)
            return {key: .95 for key in questions}

        with (patch.object(settings, "JUDGE_ESCALATE", False),
              patch("app.core.moa.llm_parser.judge_enabled", return_value=True),
              patch("app.core.moa.llm_parser.call_llm", side_effect=map_batch),
              patch("app.core.moa.llm_parser.judge_noul_batch", side_effect=judge_batch)):
            result = self.Parser(enable_cache=False).parse_table_structure(table)
        self.assertEqual(sent, headers)
        self.assertEqual(judged, [f"col_{i}" for i in range(len(headers))])
        self.assertEqual([item["original_header"] for item in result["mapped_data"]], headers)

    def test_column_decision_is_scoped_to_table_and_source_version(self):
        from app.utils.review_queue import load_unit_review_decisions

        source = Path("blocks.csv")
        source.write_text("source version 1", encoding="utf-8")
        digest = self.workflow._source_digest(source)
        decisions = Path("scoped.csv")
        decisions.write_text("file,sheet,column,source_sha256,review_decision,confirmed_unit,review_note\n"
                             f"{source},default#part_1,d18O,{digest},confirm_unit,‰ VPDB,正文明确声明标尺\n",
                             encoding="utf-8")
        point = {"source_file": str(source), "source_sheet_name": "default",
                 "sheet_name": "default#part_1", "source_row": 1, "year": 2000,
                 "source_columns": {"δ18O_value": "d18O"}, "δ18O_value": -4.5}
        self.workflow.unit_review_decisions = load_unit_review_decisions(decisions)
        self.assertEqual(self.workflow._finalize_measurements(point.copy())["δ18O_value"], -4.5)
        self.assertIsNone(self.workflow._finalize_measurements({**point, "sheet_name": "default#part_2"})["δ18O_value"])
        source.write_text("source version 2", encoding="utf-8")
        changed = self.Workflow(output_dir="changed_scope", review_decisions=decisions)
        self.assertIsNone(changed._finalize_measurements(point.copy())["δ18O_value"])

    def test_plot_groups_and_correlations_use_values_that_pass_qc(self):
        import numpy as np
        import pandas as pd
        from app.analysis.figures_validation import _paired_environment, _seasonal_statistics
        from app.analysis.loader import _add_calendar_month, _classify_indicators, filter_analysis_data

        frame = pd.DataFrame({"Year": [2000.125], "G_Grade": ["G2"], "Sr/Ca": [9], "d18O": [-4],
                              "Sr/Ca_value_qc_status": ["pass"], "δ18O_value_qc_status": ["unit_conflict"],
                              "source_time_value": [None]})
        _add_calendar_month(frame)
        self.assertEqual(frame["Month"].tolist(), [2])
        self.assertEqual(_classify_indicators(filter_analysis_data(frame))["G_Grade"].tolist(), ["G4"])
        times = pd.date_range("2000-01-01", periods=12, freq="MS")
        values = np.arange(12, dtype=float)
        grid = pd.Series(values, index=times)
        grid.iloc[:3] = np.nan
        paired = _paired_environment(pd.DataFrame({"Year": 2000, "Month": times.month, "Sr/Ca": values}),
                                     "Sr/Ca", grid)
        self.assertEqual(len(paired), 9)
        means, _, r = _seasonal_statistics(paired)
        self.assertTrue(means.iloc[:3].isna().all().all())
        self.assertAlmostEqual(r, 1)
        year = pd.DataFrame({"Year": [2000, 2001], "Sr/Ca": [8.9, 9.0]})
        complete = pd.Series(25.0, index=pd.date_range("2000-01-01", periods=23, freq="MS"))
        annual = _paired_environment(year, "Sr/Ca", complete, annual=True)
        self.assertEqual(annual.index.year.tolist(), [2000])


    def test_confirm_layout_decision_roundtrip_and_reinjection(self):
        import hashlib
        from openpyxl import Workbook
        from app.review_workbench.paper_review import PaperReview
        from app.utils.review_queue import load_unit_review_decisions, save_review_queue
        from app.core.config import settings

        root = Path(self._testMethodName).resolve()
        root.mkdir(exist_ok=True)
        book = root / "book.xlsx"
        wb = Workbook()
        ws = wb.active
        ws.title = "Data"
        ws.append(["Table S1. Coral geochemistry"])
        ws.append(["Year", "Sr/Ca", "d18O"])
        ws.append(["CE", "mmol/mol", "permil VPDB"])
        ws.append([1990, 9.0, -4.0])
        ws.append([1991, 9.1, -4.1])
        wb.save(book)
        sha = hashlib.sha256(book.read_bytes()).hexdigest()
        save_review_queue(str(root), [{"file": book.name, "sheet": "Data", "column": "",
                                       "action": "暂缓表格", "reason": "无法确定表头与观测区",
                                       "source_sha256": sha}])
        review = PaperReview(root, root, rules_only=True)
        case, = review.store.order
        self.assertEqual(case["action"], "confirm_layout")
        self.assertTrue(review.eligible(case, ()))
        entry = review.prepare(case, book)
        quote = "Table S1 lists monthly coral Sr/Ca and d18O with calendar year headings."
        paper = root / "paper.pdf"
        paper.write_text(quote, encoding="utf-8")
        doc = {"id": "p1", "cases": [case["id"]], "file": "paper.pdf", "at": "PDF 第 1 页",
               "text": quote, "kind": "paper", "bound": {case["id"]: "同一论文目录"},
               "sha256": hashlib.sha256(paper.read_bytes()).hexdigest()}
        blocks = [{"header_rows": [2], "first_data_row": 4, "last_data_row": 5,
                   "columns": [1, 2, 3], "unit_row": 3}]
        answer = {"action": "confirm_layout", "blocks": blocks, "evidence": [{"ref": "p1", "quote": quote}]}
        # 坐标越界/缺指标列的结构不得通过核验
        bad = [{"header_rows": [2], "first_data_row": 4, "last_data_row": 99, "columns": [1, 2, 3]}]
        self.assertFalse(review._verify(entry, {**answer, "blocks": bad}, {"p1": doc}))
        self.assertTrue(review._verify(entry, answer, {"p1": doc}))
        review.publish(1)
        decisions = load_unit_review_decisions(root / "review_workbench" / "max_decisions.csv")
        layout = next(d for d in decisions.values() if d["action"] == "confirm_layout")
        self.assertEqual(layout["blocks"], blocks)
        # 回灌：按裁决坐标从原表重提取，产出观测并标记已应用
        with (patch.object(settings, "LLM_API_KEY", "fixture"),
              patch.object(settings, "JUDGE_BACKEND", "off"),
              patch("app.core.moa.llm_parser.call_llm", return_value="")):
            workflow = self.Workflow(output_dir=str(root / "out"), enable_calibration_check=False,
                                     review_decisions=decisions)
            points = workflow.process_file(str(book))
        self.assertEqual(len(points), 2)
        self.assertEqual([p["year"] for p in points], [1990.0, 1991.0])
        self.assertTrue(all("Sr/Ca_value" in p["source_columns"] for p in points))
        self.assertTrue(any(k[0] == "LAYOUT" for k in workflow.applied_unit_reviews))
        self.assertTrue(any(r.get("review_decision") == "confirm_layout" for r in workflow._review_items))

    def test_confirm_layout_csv_requires_blocks_and_version(self):
        import hashlib
        from app.utils.review_queue import load_unit_review_decisions
        root = Path(self._testMethodName).resolve()
        root.mkdir(exist_ok=True)
        source = root / "book.xlsx"
        source.write_bytes(b"fixture")
        sha = hashlib.sha256(source.read_bytes()).hexdigest()
        table = root / "decisions.csv"
        fields = ["review_decision", "confirmed_unit", "review_note", "file", "sheet", "source_sha256", "layout_blocks"]
        row = {"review_decision": "confirm_layout", "confirmed_unit": "", "review_note": "结构核验",
               "file": str(source), "sheet": "Data", "source_sha256": sha, "layout_blocks": ""}
        with table.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerow(row)
        with self.assertRaisesRegex(ValueError, "layout_blocks"):
            load_unit_review_decisions(table)
        blocks = [{"header_rows": [2], "first_data_row": 4, "last_data_row": 5, "columns": [1, 2, 3]}]
        row["layout_blocks"] = json.dumps(blocks)
        with table.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerow(row)
        decisions = load_unit_review_decisions(table)
        key = next(iter(decisions))
        self.assertEqual(key[0], "LAYOUT")
        self.assertEqual(decisions[key]["blocks"][0]["first_data_row"], 4)

    def test_auto_discard_keeps_unreviewed_structure_and_separates_technical(self):
        from app.review_workbench.store import review_case_id
        file = os.path.abspath("noaa/x/struct.txt")
        items = [
            # 结构项无 finding：无法证明已经完成第三轮。
            {"file": file, "sheet": "default", "column": "", "action": "暂缓表格",
             "reason": "无法确定表头与观测区"},
            # 技术失败：超时 → 保留待程序重试并分类
            {"file": file, "sheet": "default", "column": "d18O", "action": "暂缓该值", "reason": "源单位缺失"},
            # 技术失败：原件不可读 → 保留
            {"file": file, "sheet": "default", "column": "Sr/Ca", "action": "暂缓该值", "reason": "源单位缺失"},
        ]
        self.workflow._review_items = [dict(i) for i in items]
        ids = [review_case_id(i, os.getcwd()) for i in items]
        findings = {
            ids[1]: {"state": "pending", "reason": "Max 超时，待重试"},
            ids[2]: {"state": "manual", "reason": "材料未读完", "reviewed_files": [], "unreadable_files": ["big.pdf"]},
        }
        discarded = self.workflow.discard_unrecovered_reviews({"findings": findings})
        self.assertEqual(discarded, 0)
        by_col = {i["column"]: i for i in self.workflow._review_items}
        self.assertEqual(by_col[""]["action"], "暂缓表格")
        self.assertEqual(by_col[""]["review_decision"], "retry_pending")
        self.assertEqual(by_col[""]["retry_category"], "未复核")
        self.assertEqual(by_col["d18O"]["review_decision"], "retry_pending")
        self.assertEqual(by_col["d18O"]["retry_category"], "模型超时或配额")
        self.assertEqual(by_col["Sr/Ca"]["retry_category"], "原件不可读")
        self.assertEqual(self.workflow.incomplete_review_count, 3)
        self.assertEqual(self.workflow.retry_pending_categories,
                         {"未复核": 1, "模型超时或配额": 1, "原件不可读": 1})

    def test_auto_discard_excludes_secondary_processing_and_pdf_structure(self):
        from app.review_workbench.store import _category
        file = os.path.abspath("noaa/x/obs.txt")
        pdf = os.path.abspath("zenodo/study/paper.pdf")
        items = [
            # 二次处理序列（history 归类）：直接排除，不进入第三轮。
            {"file": file, "sheet": "obs.txt", "column": "d18O_anom", "action": "暂缓该列",
             "reason": "档案注解标明中心化/缩放/距平二次处理序列，绝对值不可入数据库"},
            # 历史队列里的正文条目仅保留为上下文。
            {"file": pdf, "sheet": "page_1#table_2", "column": "", "action": "暂缓表格",
             "reason": "无法确定表头与观测区"},
            # 源文件读取失败且没有复核结论。
            {"file": file, "sheet": "", "column": "", "action": "暂缓读取",
             "reason": "无法读取原件"},
            # 非结构项 finding=None：仍保守保留待复核。
            {"file": file, "sheet": "obs.txt", "column": "Sr/Ca", "action": "暂缓该值",
             "reason": "源单位缺失"},
        ]
        self.workflow._review_items = [dict(i) for i in items]
        discarded = self.workflow.discard_unrecovered_reviews({"findings": {}})
        self.assertEqual(discarded, 1)
        excluded, pdf_struct, source_item, held = self.workflow._review_items
        self.assertEqual(excluded["action"], "已排除非观测片段")
        self.assertEqual(excluded["review_decision"], "exclude")
        self.assertIn("二次处理序列", excluded["reason"])
        self.assertEqual(_category(pdf_struct), "history")
        self.assertNotIn("review_decision", pdf_struct)
        self.assertEqual(source_item["review_decision"], "retry_pending")
        self.assertEqual(held["review_decision"], "retry_pending")
        self.assertEqual(held["retry_category"], "未复核")
        self.assertEqual(self.workflow.incomplete_review_count, 2)
        # 幂等：再次结案不再重复计数，已排除/已舍弃项保持原状。
        self.assertEqual(self.workflow.discard_unrecovered_reviews({"findings": {}}), 0)
        self.assertEqual(self.workflow.incomplete_review_count, 2)
        self.assertEqual(excluded["review_decision"], "exclude")

    def test_parallel_cores_bare_core_notes_and_composites_preserved(self):
        from app.core.config import settings
        path = ROOT / "noaa" / "3" / "3.txt"
        if not path.is_file():
            self.skipTest("缺少真实样本 noaa/3/3.txt")
        with (patch.object(settings, "LLM_API_KEY", "fixture"),
              patch.object(settings, "JUDGE_BACKEND", "off"),
              patch("app.core.moa.llm_parser.call_llm", return_value="")):
            workflow = self.Workflow(output_dir=self._testMethodName, enable_calibration_check=False)
            points = workflow.process_file(str(path))
        cores = Counter(p.get("source_core_id") for p in points)
        # 四个岩芯独立序列均保留，不再被选列歧义丢弃
        for core in ("92PAC", "92PAD", "99PAA", "92PAA"):
            self.assertGreater(cores[core], 0)
        # 作者合成序列（3core/4core）保留原单位绝对值，标为 published_composite
        composites = [p for p in points if p.get("record_type") == "published_composite"]
        self.assertGreater(len(composites), 0)
        proxies = {"Sr/Ca_value", "δ18O_value", "δ13C_value", "Ba/Ca_value"}
        self.assertTrue(all(proxies & set(p["source_columns"]) for p in composites))
        self.assertFalse(any(r.get("action") == "暂缓该指标" for r in workflow._review_items))

    def test_smoothed_only_observation_kept_but_raw_preferred(self):
        from app.core.moa.database_parser import UnifiedDatabaseParser
        parser = UnifiedDatabaseParser()
        # 仅平滑、保留原单位绝对值的直接骨骼观测 → 不再一概排除
        smoothed = {"processing": "smoothed", "material": "Acropora humilis", "error": "",
                    "data_type": "corals and sclerosponges", "quantity": "strontium/calcium"}
        self.assertTrue(parser.is_non_observation("Sr/Ca-a.hum-4pt", smoothed))
        self.assertTrue(parser._smoothed_only_observation("Sr/Ca-a.hum-4pt", smoothed))
        # 气候重建（Sr/Ca→SST）即便平滑也排除
        recon = {**smoothed, "quantity": "sea surface temperature",
                 "data_type": "climate reconstructions;corals and sclerosponges"}
        self.assertFalse(parser._smoothed_only_observation("Sr/Ca_T", recon))
        # 带误差/距平的平滑列不 rescue
        self.assertFalse(parser._smoothed_only_observation("Sr/Ca_SE", {**smoothed, "error": "one standard error"}))
        self.assertFalse(parser._smoothed_only_observation("Sr/Ca_anom", {**smoothed, "processing": "smoothed; anomaly"}))
        # 真实样本：Sadler2015 平滑 Sr/Ca 不再被静默丢弃，成为可复核候选
        path = ROOT / "noaa" / "10" / "sadler2015-heronreef-noaa.txt"
        if path.is_file():
            from app.utils.review_queue import get_review_items
            self.clear_review()
            parser.parse_file(str(path))
            issues = get_review_items()
            # 两个平滑 Sr/Ca 列保留为选列复核候选（而非被守卫排除）
            srca = [r for r in issues if r.get("decision") == "Sr/Ca_value"]
            self.assertTrue(srca)
            self.assertIn("Sr/Ca-a.hum-4pt", srca[0]["column"])
            self.assertIn("Sr/Ca-a.sam-4pt", srca[0]["column"])


    def test_incremental_archive_export_preserves_identity_and_values(self):
        from app.core.moa.data_output import DataOutputModule
        root = Path(self._testMethodName).resolve()
        root.mkdir(exist_ok=True)
        out = DataOutputModule(str(root))
        record = {"paper_hash": "p1", "source_file": str(root / "obs.txt"),
                  "source_attachment": str(root / "obs.txt"), "sheet_name": "default",
                  "source_row": 1, "source_line": 10, "year": 2000.0,
                  "geo_latitude": 18.5, "geo_longitude": 109.2, "geo_siteName": "南海",
                  "Sr/Ca_value": 8.9, "δ18O_value": -4.2,
                  "source_columns": {"Sr/Ca_value": "Sr/Ca", "year": "Year"},
                  "sample_id": "C1", "source_record_id": "C1",
                  "hasResolution_nominal": "monthly", "qc_flag": 0}
        out.write_observation_tables(str(root), [record])
        combined = root / "consolidated" / "all_data_combined.csv"
        with combined.open(encoding="utf-8-sig", newline="") as stream:
            original_id = next(csv.DictReader(stream))["observation_id"]
        status = out.export_archive_formats(str(root), str(combined))
        self.assertEqual(status["lipd"], "written")
        self.assertTrue((root / "paleoclimate_data.lpd").is_file())
        with combined.open(encoding="utf-8-sig", newline="") as stream:
            reversed_records = out._records_from_combined(list(csv.DictReader(stream)))
        self.assertEqual(out._observation_id(reversed_records[0]), original_id)
        self.assertEqual(reversed_records[0]["year"], 2000.0)
        self.assertEqual(reversed_records[0]["source_columns"]["Sr/Ca_value"], "Sr/Ca")
        if status["netcdf"] == "written":
            import xarray as xr
            with xr.open_dataset(root / "paleoclimate_grid.nc") as ds:
                self.assertEqual(str(ds["observation_id"].values[0]), original_id)
                self.assertAlmostEqual(float(ds["Sr_Ca"].values[0]), 8.9)


if __name__ == "__main__":
    unittest.main()
