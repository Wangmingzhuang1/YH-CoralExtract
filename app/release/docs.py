"""发布包文档：README / DATA_DICTIONARY / CHANGELOG（英文，随版本自动生成）。"""
from pathlib import Path

from .schema import PROXIES, PROXY_LABELS, QC_STATUS_VOCAB

UNITS = {name: spec[2] for name, spec in PROXIES.items()}


def write_docs(out_dir: Path, version: str, summary: dict, records):
    accounting = summary["accounting"]
    tree = f"""GeoDataExtractor-Coral-v{version}/
├── README.md / DATA_DICTIONARY.md / CHANGELOG.md
├── 01_records/            one LiPD per source record (BagIt + JSON-LD + measurement CSVs)
├── 02_compilation/        observations.csv, records_metadata.csv, compilation_summary.json
├── 03_derived/            by_resolution/, by_proxy_group/, gridded/ (regenerable views)
└── 04_provenance_qc/      source_manifest, value_provenance, review_decisions,
                           held_values, reprint_pairs, qc_report.json
"""
    readme = f"""# GeoDataExtractor Coral Proxy Compilation v{version}

A value-level quality-controlled compilation of coral geochemical proxy records
(Sr/Ca, Ba/Ca, delta 18O, delta 13C, SST) extracted from {accounting['records']} source
records across {accounting['sites']} sites, spanning {accounting['year_range_ce'][0]:.0f} to
{accounting['year_range_ce'][1]:.0f} CE, with {accounting['observation_rows']:,} observations.
Every released value carries a QC status and a full provenance chain back to the
source file, sheet, row and page.

## Layers

1. **01_records** — archival unit: one standard LiPD file per source record
   (CoralHydro2k-compatible BagIt layout). Reprinted records are retained and
   linked to their canonical record via `geoDataExtractor.reprintOf`.
2. **02_compilation** — analysis-ready tables: `observations.csv` (one row per
   observation, ASCII headers) and `records_metadata.csv` (one row per record).
3. **03_derived** — convenience views by resolution and proxy combination, plus
   a gridded netCDF product. All regenerable from layer 2.
4. **04_provenance_qc** — audit trail: source-file manifest, per-value provenance
   (original value/unit/evidence), review decisions, withheld (held) values,
   reprint evidence pairs, and the QC accounting report.

## Key accounting

- Observations: {accounting['observation_rows']:,}
- Source records: {accounting['records']} ({accounting['canonical_records']} canonical,
  {accounting['reprint_records']} reprints flagged via `reprint_of`)
- Withheld review items (not released as values): {accounting['held_review_items']:,}
  — see `04_provenance_qc/held_values.csv`

## Scope declaration

Release policy: **full inclusion**. The dataset contains every extracted record,
including {accounting['scope']['rows_outside_core_range']:,} observations outside the
Common Era core range ({accounting['scope']['common_era_core_range_ce'][0]} to
{accounting['scope']['common_era_core_range_ce'][1]} CE; deep-time corals).
The accompanying data descriptor paper declares the analysis scope: figures and
compilation statistics restricted to the core range use
`year_ce` within [{accounting['scope']['common_era_core_range_ce'][0]},
{accounting['scope']['common_era_core_range_ce'][1]}]
({accounting['scope']['rows_within_core_range']:,} observations). No rows are
silently filtered at release time.

## Usage

Filter released values by the per-proxy `*_qc_status` columns (`pass` = fully
checked). Values flagged `outside_range` / `outlier_flagged` are retained but
marked. Join `value_provenance.csv` on `observation_id` + `proxy` for original
values, units and evidence. Select rows with an empty `reprint_of` to omit
identified reprints. The archive preserves author-processed series alongside the
original measurements. For analyses requiring independent observations, use
`source_core_id`, `source_processing` and `composite_evidence` to choose one
version per core and to avoid counting a composite together with its members.

## Citation

Cite this dataset via its Zenodo DOI (version {version}) and the accompanying
data descriptor paper. Source publications are listed per record in
`records_metadata.csv` (`pub_doi`, `dataset_doi`).

## Directory layout

```
{tree}```
"""
    (out_dir / "README.md").write_text(readme, encoding="utf-8")

    obs_columns = """| column | description |
|---|---|
| observation_id | unique observation identifier (stable across versions) |
| record_id | source record identifier (join to records_metadata.csv); unlabelled IDs identify source files and tables without a declared record ID |
| record_code | short record code, also the LiPD file name in 01_records |
| site_name | reef/site name as reported in the source |
| latitude / longitude | site coordinates in decimal degrees (WGS84) |
| year_ce | observation year in Common Era (fractional for sub-annual) |
| time_resolution | monthly / submonthly / annual / seasonal / bimonthly / biannual / irregular / discrete_samples / unknown |
| native_interval_years | native sampling interval in years where reported |
| qc_flag | row-level QC flag (0 = clean; see qc_report.json for the full ledger) |
| reprint_of | record_code of the canonical record if this record is a reprint; empty otherwise |
| source_core_id | coral core identifier when declared in the source |
| source_processing | source processing label, including interpolation or compositing when reported |
| composite_evidence | source evidence for the author-defined composite and its members when available |
"""
    for name in PROXIES:
        obs_columns += f"| {name} | {PROXY_LABELS[name]}, unit: {UNITS[name]}; empty = not released |\n"
        obs_columns += f"| {name}_qc_status | value-level QC status for {name} (vocabulary below) |\n"

    vocab = "\n".join(f"| {k} | {v} |" for k, v in QC_STATUS_VOCAB.items())
    dictionary = f"""# Data Dictionary — v{version}

All CSV files are UTF-8, comma-separated, with a header row. Missing values are
empty cells in CSV and `NA` inside LiPD measurement tables.

## 02_compilation/observations.csv

{obs_columns}
### QC status vocabulary

| status | meaning |
|---|---|
{vocab}

## 02_compilation/records_metadata.csv

One row per source record: record_id, record_code, site_name, latitude,
longitude, year_min_ce, year_max_ce, n_observations, time_resolutions,
proxies, source_files, is_reprint, reprint_of, pub_doi, pub_authors,
dataset_doi, dataset_investigators, dating_method, dating_error, testing_method.

## 04_provenance_qc/value_provenance.csv

Long table, one row per released value: observation_id, record_id, proxy,
released_value, original_value, original_unit, unit_status, qc_status,
unit_evidence (JSON), source_file, source_attachment, sheet_name, source_row,
source_line, source_page, source_core_id, source_processing, composite_evidence.

## 04_provenance_qc other files

- source_manifest.csv — per input file/sheet: status, input_rows,
  source_rows_with_output, output_rows, review_items, reason.
- review_decisions.csv — full review queue incl. applied evidence reviews and
  human/model adjudications (action, decision, evidence_status, judge fields).
- held_values.csv — subset of review items withheld from release (action starts
  with a hold category); nothing is silently dropped.
- reprint_pairs.csv — evidence for reprint detection: site, record pair, proxy,
  method (duplicate_version / near_identical), n_common_years, r.
- qc_report.json — value-level and row-level QC accounting, plus the release
  scope declaration (full inclusion; Common Era core range used by the paper).

## 01_records/*.lpd

Standard LiPD (BagIt) per record: bag/data/metadata.jsonld (JSON-LD: geo, pub,
paleoData measurement tables with per-column units and TSid) plus headerless
measurement CSVs (column order = metadata column order, year first).
`geoDataExtractor.reprintOf` links reprints to the canonical record code.

## 03_derived

by_resolution/<resolution>.csv and by_proxy_group/<G-group>.csv are row subsets
of observations.csv (identical schema). gridded/paleoclimate_grid.nc is the
gridded annual product; both are regenerable from layer 2.
"""
    (out_dir / "DATA_DICTIONARY.md").write_text(dictionary, encoding="utf-8")

    changelog = f"""# Changelog

## v{version} ({summary['generated_at'][:10]})

- Initial structured release from extraction batch `{summary['source_batch']}`.
- {accounting['observation_rows']:,} observations, {accounting['records']} source records
  ({accounting['reprint_records']} reprints flagged), {accounting['sites']} sites.
- Per-value QC statuses and full provenance chain; withheld items disclosed in
  04_provenance_qc/held_values.csv.
"""
    (out_dir / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
