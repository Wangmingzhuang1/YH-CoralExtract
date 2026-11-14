# YH-CoralExtract

YH-CoralExtract is the processing code for YH-CoralGeo, a compilation of coral skeletal geochemistry. It extracts Sr/Ca, Ba/Ca, δ¹⁸O and δ¹³C observations and separately identifies supported sea surface temperature records.

## Inputs and processing

Place the files for each study in a separate directory under `dataset/`, `noaa/`, `pangaea/` or `zenodo/`. The program also accepts archive-only directories. Observation values come from attachments and archive tables. Article PDFs supply evidence for sample identity, units, dates and other metadata. PDF attachments explicitly identified by filename can also supply observations.

1. Identify source tables and their physical rows and columns. Use archive and workbook definitions where available, and request model-assisted mapping only where needed.
2. Check mappings against the source, standardize ages and units, retain core and processing identities, and apply value-level quality flags. A configured judge can score candidates and escalate low-confidence mappings.
3. Review unresolved candidates against their original evidence. Replay verified decisions on the affected sources, while checking existing observations for unintended changes. With `--auto-discard`, completed negative reviews are closed; unreviewed candidates and technical failures remain distinguishable in the review queue.

Model configuration is optional for the local rules. Model-backed extraction and a fresh review are subject to model availability and may differ from the archived run. Decisions reused for extraction are scoped to the verified source version. The source corpus, model responses, study PDFs, and published observations are distributed separately from this code.

## Run

Use Python 3.12 from the repository root.

```sh
python -m pip install -r requirements.txt
python main.py --mode moa --auto-discard --review-rules-only
python -m unittest discover -s tests
```

The example runs the rules-only paper review. Leave the API keys empty for local processing or copy `.env.example` to `.env`, configure compatible model endpoints, and omit `--review-rules-only` for model-backed review. The default judge backend is disabled. Some regression tests use local research fixtures and are skipped when the fixtures are absent.

## Reproduce a published example

```sh
python -m pip install -r requirements.txt
python examples/reproduce_grove2013.py --output output/grove2013_example
```

Use a new output directory. The example downloads one [NOAA source attachment](https://www.ncei.noaa.gov/pub/data/paleo/coral/indian_ocean/grove2013/grove2013-srca-noaa.txt) and the pinned CoralHydro2k reference, about 1.5 MB in total. It uses no API key. It runs the extraction, local coverage audit, sequence comparison and release export using the project code. The source paper is [Grove et al. (2013)](https://doi.org/10.1002/ggge.20095), and the archive citation is [Grove and Zinke (2018), NOAA/WDS](https://doi.org/10.25921/bpem-9j52). Cite the original data and your access date when reusing the attachment.

The expected output is 1,551 Sr/Ca rows, with 517 each for STM2, STM4 and the author composite. The two cores each match all 517 age–value pairs in CoralHydro2k (GR13MAD01 and GR13MAD02). The composite remains separately identified. Check the source manifest and consolidated CSV in the example batch, `comparison/sequence_crosswalk.csv` for the matches, and `release/` for the packaged records. The benchmark denominator in this example still refers to the complete CoralHydro2k reference. This example verifies the archive-defined extraction path. The test suite uses controlled model responses to exercise evidence review, failures and incremental replay.

## Additional commands

```sh
python main.py --mode audit --run-dir PATH_TO_RUN --no-flash
python main.py --mode review --run-dir PATH_TO_RUN --review-file STUDY_OR_FILE --review-apply
python main.py --mode patch --run-dir PATH_TO_RUN --review-decisions PATH_TO_DECISIONS
python -m app.release --run-dir PATH_TO_RUN --out PATH_TO_RELEASE
```

Extraction writes a consolidated CSV, source inventory, review queue and QC report. Verified incremental runs also export LiPD and NetCDF where supported. A release package includes observation and record tables, value provenance and per-record LiPD files. Retained raw, interpolated, and author-composite versions are identified so they can be selected appropriately in downstream analysis.

## CoralHydro2k comparison

The independent dataset is compared with the [CoralHydro2k v1.0.1 reference archive](https://lipdverse.org/CoralHydro2k/current_version/CoralHydro2k1_0_1.zip). The comparison verifies the archive checksum. Reproducing the identity checks requires the source files at the relative paths recorded in the batch. Run from the source corpus root or make that corpus available at the repository root.

```sh
python -m app.analysis.coral_comparison --run-dir PATH_TO_RUN --baseline CoralHydro2k1_0_1.zip --out-dir PATH_TO_COMPARISON
```

Full manuscript figure generation is in `paper/essd_figures/generate.py`. Supply the CoralHydro2k ZIP at `output/coral_comparison/CoralHydro2k1_0_1.zip`, the environmental reference grids via `--external-data`, and Chrome or Edge on the path for rendering the workflow SVG. Run `python main.py --help` for command options.

The code is released under the [MIT License](LICENSE). Source articles and datasets retain their original terms.
