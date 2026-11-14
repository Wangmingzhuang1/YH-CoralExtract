"""按记录生成标准 LiPD（BagIt 包 + metadata.jsonld + 测量表 CSV）。

结构对齐 CoralHydro2k v1.0.1：每个 .lpd 是一个 zip，内含
bag/bagit.txt、bag/bag-info.txt、bag/manifest-md5.txt、bag/tagmanifest-md5.txt、
bag/data/metadata.jsonld 与 bag/data/<code>.paleo1measurementN.csv。
一条记录按分辨率拆测量表（paleo1measurement1..N），列序 = year + 各代理，
CSV 无表头、逐值加引号、缺测写 NA（LiPD 惯例），列语义全部在 metadata.jsonld。
"""
import hashlib
import io
import json
import logging
import zipfile
from datetime import date

import numpy as np
import pandas as pd

from .schema import PROXIES, PROXY_LABELS, slugify_site

logger = logging.getLogger(__name__)

LIPD_CONTEXT = ["https://linked.earth/lipd/context.jsonld"]


def assign_record_codes(frame: pd.DataFrame) -> dict:
    """record_id -> 文件系统安全的记录短码（站点短码 + 两位序号）。"""
    sites = frame.groupby("source_record_id")["geo_siteName"].agg(
        lambda s: s.dropna().iloc[0] if s.notna().any() else "Unknown")
    codes, per_site = {}, {}
    for record_id in sorted(sites.index, key=str):
        slug = slugify_site(sites[record_id])
        idx = per_site.get(slug, 0) + 1
        per_site[slug] = idx
        codes[record_id] = f"{slug}{idx:02d}"
    return codes


def _measurement_columns(code: str, resolution: str, proxies):
    """jsonld 列定义：year 为主年代列 + 各代理列。"""
    columns = [{
        "variableName": "year", "units": "CE", "description": "Year CE",
        "dataType": "float", "inferredVariableType": "year",
        "TSid": f"{code}_year", "isPrimary": True, "primaryAgeColumn": True,
        "hasResolution": {"nominal": resolution}, "number": 1,
    }]
    for number, name in enumerate(proxies, start=2):
        spec = PROXIES[name]
        columns.append({
            "variableName": name, "units": spec[2],
            "description": PROXY_LABELS[name], "dataType": "float",
            "TSid": f"{code}_{name}", "isPrimary": False, "primaryAgeColumn": False,
            "hasResolution": {"nominal": resolution}, "number": number,
        })
    return columns


def _csv_bytes(sub: pd.DataFrame, proxies) -> bytes:
    """无表头测量 CSV：year + 代理列，缺测 NA，全部加引号。"""
    lines = []
    years = pd.to_numeric(sub["year_CE"], errors="coerce").to_numpy()
    values = [pd.to_numeric(sub[PROXIES[name][0]], errors="coerce").to_numpy()
              for name in proxies]
    for i in range(len(sub)):
        row = [years[i]] + [v[i] for v in values]
        cells = []
        for value in row:
            if value is None or (isinstance(value, float) and not np.isfinite(value)):
                cells.append('"NA"')
            else:
                number = float(value)
                text = str(int(number)) if number.is_integer() else repr(number)
                cells.append(f'"{text}"')
        lines.append(",".join(cells))
    return ("\n".join(lines) + "\n").encode("utf-8")


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _clean(value):
    """NaN/NaT -> None，保证 JSON-LD 严格合法。"""
    if isinstance(value, float) and not np.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def write_record_lpd(path, code: str, record_frame: pd.DataFrame, meta: dict,
                     reprint_of_code: str | None = None):
    """把一条记录写成 .lpd。meta 提供站点/文献/转载等记录级字段。"""
    payload = {}
    tables = []
    resolutions = sorted(record_frame["time_resolution"].fillna("unknown").unique(), key=str)
    for number, resolution in enumerate(resolutions, start=1):
        sub = record_frame[record_frame["time_resolution"].fillna("unknown") == resolution]
        sub = sub.sort_values("year_CE", kind="stable")
        proxies = [name for name in PROXIES
                   if PROXIES[name][0] in sub and pd.to_numeric(
                       sub[PROXIES[name][0]], errors="coerce").notna().any()]
        if not proxies:
            continue
        table_name = f"paleo1measurement{number}"
        filename = f"{code}.{table_name}.csv"
        payload[f"data/{filename}"] = _csv_bytes(sub, proxies)
        tables.append({"tableName": table_name, "filename": filename,
                       "columns": _measurement_columns(code, str(resolution), proxies)})

    years = pd.to_numeric(record_frame["year_CE"], errors="coerce")
    site = meta.get("site_name") or "Unknown"
    lat, lon = meta.get("latitude"), meta.get("longitude")
    geometry = ({"type": "Point", "coordinates": [float(lon), float(lat)]}
                if pd.notna(lat) and pd.notna(lon) else None)
    pub = {"author": meta.get("pub_authors"), "doi": meta.get("pub_doi")}
    jsonld = {
        "@context": LIPD_CONTEXT,
        "lipdVersion": "1.0",
        "archiveType": "coral",
        "datasetId": code,
        "dataSetName": str(meta.get("record_name") or site),
        "createdBy": "GeoDataExtractor",
        "geo": {"siteName": site, "geometry": geometry},
        "pub": [pub],
        "paleoData": [{"measurementTable": tables}],
        "minYear": float(np.nanmin(years)) if years.notna().any() else None,
        "maxYear": float(np.nanmax(years)) if years.notna().any() else None,
        "originalDataUrl": (f"https://doi.org/{meta['dataset_doi']}"
                            if pd.notna(meta.get("dataset_doi")) else None),
        "additionalDataUrl": None,
        "changelog": [],
        "geoDataExtractor": {
            "sourceRecordId": meta.get("source_record_id"),
            "sourceFiles": meta.get("source_files", []),
            "reprintOf": reprint_of_code,
            "datingMethod": meta.get("dating_method"),
            "testingMethod": meta.get("testing_method"),
            "observationCount": int(len(record_frame)),
        },
    }
    payload["data/metadata.jsonld"] = json.dumps(_clean(jsonld), ensure_ascii=False, indent=1).encode("utf-8")

    bagging_date = date.today().isoformat()
    octets = sum(len(v) for v in payload.values())
    bag_info = (f"Bag-Software-Agent: GeoDataExtractor release packager\n"
                f"Bagging-Date: {bagging_date}\n"
                f"Payload-Oxum: {octets}.{len(payload)}\n").encode("utf-8")
    bagit = b"BagIt-Version: 0.97\nTag-File-Character-Encoding: UTF-8\n"
    manifest = "".join(f"{_md5(data)}\t{name}\n" for name, data in sorted(payload.items())).encode("utf-8")
    tags = {"bag-info.txt": bag_info, "bagit.txt": bagit, "manifest-md5.txt": manifest}
    tagmanifest = "".join(f"{_md5(data)}\t{name}\n" for name, data in sorted(tags.items())).encode("utf-8")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bag/bagit.txt", bagit)
        zf.writestr("bag/bag-info.txt", bag_info)
        zf.writestr("bag/manifest-md5.txt", manifest)
        zf.writestr("bag/tagmanifest-md5.txt", tagmanifest)
        for name, data in sorted(payload.items()):
            zf.writestr(f"bag/{name}", data)
    path.write_bytes(buffer.getvalue())
    return len(tables)