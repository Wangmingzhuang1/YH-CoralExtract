"""Reproduce two CoralHydro2k Sr/Ca matches from the public NOAA attachment."""

import argparse
import csv
from collections import Counter
from hashlib import sha256
import os
import logging
from pathlib import Path
import subprocess
import sys

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.analysis.coral_comparison import BASELINE_SHA256, BASELINE_URL, run_comparison
from app.core.moa.coverage_audit import audit_run
from app.release.packager import build_release

SOURCE_URL = (
    "https://www.ncei.noaa.gov/pub/data/paleo/coral/indian_ocean/"
    "grove2013/grove2013-srca-noaa.txt"
)
SOURCE_SHA256 = "cf8d45be05caf9956274eacbc060c38f60975f624473567367f9229619dd4cc0"


def download(url, path, expected_sha256):
    response = requests.get(url, timeout=60)
    response.raise_for_status()
    if sha256(response.content).hexdigest() != expected_sha256:
        raise ValueError(f"Source checksum changed: {url}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(response.content)


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="A new output directory")
    args = parser.parse_args()
    work = args.output.resolve()
    work.mkdir(parents=True, exist_ok=False)
    source = work / "noaa/Grove2013/grove2013-srca-noaa.txt"
    baseline = work / "reference/CoralHydro2k1_0_1.zip"
    download(SOURCE_URL, source, SOURCE_SHA256)
    download(BASELINE_URL, baseline, BASELINE_SHA256)

    # Isolate the example from any local model settings and previous extraction runs.
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith(("LLM_", "JUDGE_", "REVIEW_"))}
    env["PYTHON_DOTENV_DISABLED"] = "1"
    subprocess.run(
        [sys.executable, str(ROOT / "main.py"), "--mode", "moa", "--auto-discard", "--review-rules-only"],
        cwd=work, env=env, check=True,
    )
    batch, = (work / "output/moa_output").glob("run_*")
    audit_run(batch, source_root=work, use_flash=False)

    compare_dir = work / "comparison"
    logging.getLogger("fontTools").setLevel(logging.WARNING)
    run_comparison(batch, baseline, compare_dir, source_root=work)
    crosswalk = {row["entity"]: row for row in read_csv(compare_dir / "sequence_crosswalk.csv")
                 if row["proxy"] == "Sr/Ca"}
    for entity, core in (("GR13MAD01", "STM2"), ("GR13MAD02", "STM4")):
        row = crosswalk[entity]
        if (row["core"], row["status"], row["matched_year_value_pairs"]) != (core, "双方一致", "517"):
            raise ValueError(f"Unexpected result for {entity}: {row}")

    combined = read_csv(batch / "consolidated/all_data_combined.csv")
    counts = Counter(row["source_core_id"] or "author composite" for row in combined)
    expected = {"STM2": 517, "STM4": 517, "author composite": 517}
    if counts != expected or sum(bool(row.get("composite_evidence")) for row in combined) != 517:
        raise ValueError(f"Unexpected source identities or observation counts: {counts}")
    build_release(batch, work / "release")
    print("Verified: 1,551 Sr/Ca rows, including 517 per core and 517 for the author composite.")
    print("CoralHydro2k: GR13MAD01 and GR13MAD02 match all 517 age–value pairs each.")
    print(f"Batch: {batch}\nCrosswalk: {compare_dir / 'sequence_crosswalk.csv'}")


if __name__ == "__main__":
    main()
