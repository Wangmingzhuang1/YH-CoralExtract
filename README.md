# YH-CoralExtract

YH-CoralExtract is the extraction architecture behind **YH-CoralGeo**, a global compilation of coral skeletal geochemistry (Sr/Ca, Ba/Ca, δ¹⁸O, δ¹³C, plus author-published SST reconstructions). This repository contains the **processing code only**; the observations, record metadata, provenance tables and gridded products are distributed separately as the YH-CoralGeo dataset on Zenodo (see the paper, §6).

The pipeline reads heterogeneous public archives (NOAA, PANGAEA) and author deposits (Zenodo, paper attachments), identifies cores / proxies / chronologies with a generator–judge separation, standardizes ages and units against source evidence, and emits value-level traceable observations with two-level quality flags.

## Repository layout

| Path | Role |
|---|---|
| `main.py` | CLI entry: modes `moa` (extract), `audit`, `review`, `patch`, `analyze`, `complete`, `demo` |
| `app/core/config.py`, `.env.example`, `.env.production` | Settings; example config; **frozen batch configuration snapshot** (model IDs, thresholds, endpoints; no secrets) |
| `app/core/moa/` | Extraction pipeline: ingestion, database parsers (NOAA/PANGAEA), LLM table/column mapping, spatiotemporal standardization, unit conversion, quality control, coverage audit, patch replay, export |
| `app/core/prompt.py`, `app/utils/llm_api.py` | Prompts and OpenAI-compatible API client with response cache |
| `app/utils/llm_judge.py` | Jev discriminative confidence gate (batch true/false propositions → calibrated probabilities) |
| `app/review_workbench/` | Three-round review: deterministic evidence rules, paper-level review, decision store |
| `app/release/` | Release packager (LiPD + CSV layers), Zenodo uploader, data dictionary generator |
| `app/analysis/`, `paper/essd_figures/` | **Optional** paper-figure and supplementary-table reproduction, incl. the independent source-table audit (`source_audit.py`). Not required for extraction |
| `config/` | Unit conversion, outlier thresholds, spatio-temporal rules, logging |
| `examples/` | Grove et al. (2013) end-to-end reproduction (no API key needed) |
| `tests/` | Regression suite (controlled model responses; some fixtures optional) |
| `GeoDataExtractor_workflow.svg` | Workflow diagram asset rendered into paper Figure 1a |

`paper/` is bundled so reviewers can regenerate every paper figure and supplementary table from a frozen batch; it is deliberately separate from the extraction path and can be ignored when only running extraction.

## Installation

Python **3.12**, from the repository root:

```sh
python -m pip install -r requirements.txt
```

Dependency groups in `requirements.txt`:

- **Core parsing**: pandas, numpy, openpyxl/xlrd/odfpy (workbooks), python-docx, pdfplumber (PDF evidence), PyYAML.
- **Model API**: requests, python-dotenv (OpenAI-compatible endpoints; no vendor SDK).
- **Statistics / utils**: scipy, tqdm, psutil.
- **Figures & grids (only for `app/analysis` / `paper/`)**: matplotlib, seaborn, Cartopy (maps; needs GEOS/PROJ, provided by wheels), xarray, netCDF4.
- **Config**: pydantic, pydantic-settings.

Paper-figure rendering additionally needs headless **Chrome or Edge** on PATH (workflow SVG only). Extraction itself needs neither Cartopy nor a browser.

## Step-by-step usage

1. **Prepare the corpus.** One directory per study under `noaa/`, `pangaea/`, `zenodo/` or `dataset/`, e.g. `noaa/DeLong2012/DeLong2012a2.txt` plus the study PDF beside it. Archive-only directories are accepted. PDFs supply evidence (core identity, units, scale, chronology); PDF attachments explicitly named as data files can also supply observations.
2. **Configure.** `cp .env.example .env`, fill `LLM_API_KEY` and endpoints. To reproduce the published batch exactly, copy the non-secret entries of `.env.production` into `.env` (model IDs, thinking switches, judge thresholds and local endpoint) and add your keys. With empty keys the pipeline runs rules-only.
3. **Smoke run (no model cost).** `python main.py --mode moa --auto-discard --review-rules-only`.
4. **Full extraction.** Omit `--review-rules-only`; add `--auto-discard` to close completed negative reviews evidence-based (reversible, ledgered). Outputs land in `output/moa_output/run_<timestamp>/`: `consolidated/all_data_combined.csv` (value-level provenance), `review_queue.csv`, `INDEX.csv`, per-resolution tables, LiPD and netCDF where supported.
5. **Coverage audit.** `python main.py --mode audit --run-dir <run> --no-flash` reconciles per-study expected proxies/chronologies/cores against actual output and routes gaps.
6. **Paper-level review.** `python main.py --mode review --run-dir <run> --review-file <study-or-file> [--review-apply]` verifies held candidates against the paper and attachments; `--review-apply` replays verified decisions onto affected sources.
7. **Incremental update.** `python main.py --mode patch --run-dir <run> --review-decisions <decisions.csv>` reprocesses only affected sources and publishes a validated full batch. Observation identity is stable across updates (source file, sheet, row, record, sample, processing version).
8. **Package a release.** `python -m app.release --run-dir <run> --out <dir> --no-unlabelled`. The published convention keeps 1,430 source records; the 12,188 observations without a declared record identity stay in the observation table with empty `record_code`. Without `--no-unlabelled` the packager instead creates 32 file/sheet-level `unlabelled-` records (1,462 records).
9. **Upload / update Zenodo.** `python -m app.release.zenodo --package <dir> --draft` (new draft + preview link), `--update-id <deposit>` (refresh an existing draft, ID unchanged), `--publish-id <deposit>` (publish, irreversible). Default is dry-run.

Unrecovered candidates after three review rounds, and manual decisions that cannot be adopted, are **evidence-based discarded**: reversible exclusion, source data preserved, reasons written to the discard/closure ledger. Nothing is silently dropped.

## Jev judge: deployment and usage advice

Jev is a **discriminative confidence gate**, separate from the generative extractor, to avoid self-evaluation hallucination: it scores atomic mapping propositions and returns calibrated probabilities.

- **Deploy locally** behind an OpenAI-compatible endpoint (e.g. Ollama serving the `tev1:4b` tag): set `JUDGE_BACKEND=typesafe`, `JUDGE_BASE_URL=http://localhost:11434/v1/systemone`, `JUDGE_MODEL=tev1:4b`. Local serving means no token cost for judging.
- **Thresholds**: accept ≥ `JUDGE_PASS_THRESHOLD` (0.60 in the published batch); revoke ≤ `JUDGE_FAIL_THRESHOLD` (0.30); scores in between enter the review queue. Calibration on 24 labelled propositions: correct propositions scored ≥ 0.466, incorrect ≤ 0.139 — zero flips at 0.60. **Recalibrate on your own labelled propositions before moving thresholds.**
- **Modes**: `JUDGE_REVIEW_ONLY=true` is the conservative cheap mode (low scores only queue). The published batch uses `JUDGE_REVIEW_ONLY=false` + `JUDGE_ESCALATE=true`: low-score candidates are independently re-mapped by an escalation model (`JUDGE_ESCALATE_MODEL`, deepseek-v4-pro in the batch) and re-judged; on the calibration set this rescued 12/12 known wrong mappings with 0 false passes.
- **Fail-open safety**: if the judge is unavailable it returns empty scores; candidates then still pass the deterministic source-evidence checks — never silently accepted on model authority alone.
- Keep the response cache enabled across runs; judge and generator responses are keyed by model ID and source content, so configuration changes invalidate only the affected entries.

## Reproduce a published example

```sh
python -m pip install -r requirements.txt
python examples/reproduce_grove2013.py --output output/grove2013_example
```

Use a new output directory. The example downloads one [NOAA source attachment](https://www.ncei.noaa.gov/pub/data/paleo/coral/indian_ocean/grove2013/grove2013-srca-noaa.txt) and the pinned CoralHydro2k reference, about 1.5 MB in total. It uses no API key. It runs the extraction, local coverage audit, sequence comparison and release export using the project code. The source paper is [Grove et al. (2013)](https://doi.org/10.1002/ggge.20095), and the archive citation is [Grove and Zinke (2018), NOAA/WDS](https://doi.org/10.25921/bpem-9j52). Cite the original data and your access date when reusing the attachment.

The expected output is 1,551 Sr/Ca rows, with 517 each for STM2, STM4 and the author composite. The two cores each match all 517 age–value pairs in CoralHydro2k (GR13MAD01 and GR13MAD02). The composite remains separately identified. Check the source manifest and consolidated CSV in the example batch, `comparison/sequence_crosswalk.csv` for the matches, and `release/` for the packaged records. The benchmark denominator in this example still refers to the complete CoralHydro2k reference. This example verifies the archive-defined extraction path. The test suite uses controlled model responses to exercise evidence review, failures and incremental replay.

## Optional: paper figures and independent audit

```sh
python paper/essd_figures/generate.py --run <batch-dir> --output <out-dir> [--external-data <grids-dir>]
python paper/essd_figures/source_audit.py --run <batch-dir> --output <out-dir>
```

`generate.py` regenerates all nine main figures, supplementary figures S1–S7 and the accompanying CSV tables from one frozen consolidated table; it needs the ERSSTv5/EN4 reference grids (`--external-data`) and Chrome/Edge for the workflow SVG. `source_audit.py` is the deterministic, model-free source-table audit (stratified by source type × mapping route for Sr/Ca/δ¹⁸O; fixed-source samples for δ¹³C/Ba/Ca) and can run standalone.

## Batch configuration snapshot and DOI errata

`.env.production` pins the model versions and call settings of the YH-CoralGeo v1.1.0 figure batch (consolidated-table SHA-256 `d2ad45f18b3b8bab60b6bfda181ca6ac2c01394e68b6e15ef27dca0ca1a695a8`): extraction `qwen3.8-max` (thinking disabled), lightweight metadata `qwen3.8-flash`, paper-level review `qwen4-max-preview`, judge `tev1:4b` on a local OpenAI-compatible endpoint (accept 0.60 / revoke 0.30), and low-score escalation `deepseek-v4-pro`. Copy its non-secret entries into `.env` and add your keys to reproduce the batch configuration. Verified archive-DOI errata are pinned in `DOI_ERRATA` (`app/core/moa/context_extractor.py`); original archive files are never rewritten.

## Additional commands

```sh
python main.py --mode audit --run-dir PATH_TO_RUN --no-flash
python main.py --mode review --run-dir PATH_TO_RUN --review-file STUDY_OR_FILE --review-apply
python main.py --mode patch --run-dir PATH_TO_RUN --review-decisions PATH_TO_DECISIONS
python -m app.release --run-dir PATH_TO_RUN --out PATH_TO_RELEASE --no-unlabelled
```

Extraction writes a consolidated CSV, source inventory, review queue and QC report. Verified incremental runs also export LiPD and NetCDF where supported. A release package includes observation and record tables, value provenance and per-record LiPD files. Retained raw, interpolated, and author-composite versions are identified so they can be selected appropriately in downstream analysis.

## CoralHydro2k comparison

The independent dataset is compared with the [CoralHydro2k v1.0.1 reference archive](https://lipdverse.org/CoralHydro2k/current_version/CoralHydro2k1_0_1.zip). The comparison verifies the archive checksum. Reproducing the identity checks requires the source files at the relative paths recorded in the batch. Run from the source corpus root or make that corpus available at the repository root.

```sh
python -m app.analysis.coral_comparison --run-dir PATH_TO_RUN --baseline CoralHydro2k1_0_1.zip --out-dir PATH_TO_COMPARISON
```

## Tests, license, citation

`python -m unittest discover -s tests` (fixture-dependent tests skip when fixtures are absent). Code: [MIT License](LICENSE). Source articles and datasets retain their original terms. Cite via `CITATION.cff` and the YH-CoralGeo dataset DOI on Zenodo.