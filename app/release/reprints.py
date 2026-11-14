"""跨库转载归并：同站点、同代理的序列比对识别重复版本与近完美转载。

口径与 figures_validation.compute_local_reproducibility 一致：
共同观测值完全一致（重复版本），或 |r| >= 0.999（跨文献转载）。
聚类后每簇选一条规范记录（发表年最早 -> 观测数最多 -> record_id 字典序），
其余记录打 reprint_of 标记，全部保留可溯源，不静默删除。
"""
import logging
import re

import numpy as np
import pandas as pd

from .schema import PROXIES

logger = logging.getLogger(__name__)

MIN_COMMON = 20          # 最少共同观测年数（与 Fig10 的 >=20 年一致）
NEAR_IDENTICAL_R = 0.999


def _pub_year(authors) -> int:
    """从作者串里取首个合理年份；取不到返回 9999（排序时靠后）。"""
    match = re.search(r"(1[5-9]\d{2}|20[0-2]\d)", str(authors or ""))
    return int(match.group(1)) if match else 9999


def _record_series(frame: pd.DataFrame):
    """record_id -> proxy -> {year: value}（同年取均值，与 Fig10 口径一致）。"""
    series = {}
    value_cols = {name: spec[0] for name, spec in PROXIES.items() if spec[0] in frame}
    for record_id, sub in frame.groupby("source_record_id", sort=False):
        per_proxy = {}
        years = pd.to_numeric(sub["year_CE"], errors="coerce")
        for name, col in value_cols.items():
            values = pd.to_numeric(sub[col], errors="coerce")
            ok = years.notna() & values.notna()
            if not ok.any():
                continue
            grouped = values[ok].groupby(years[ok].astype(int)).mean()
            per_proxy[name] = grouped.to_dict()
        series[record_id] = per_proxy
    return series


def _compare(a: dict, b: dict):
    """返回 (method, n_common, r)；不构成转载返回 None。"""
    common = sorted(set(a) & set(b))
    if len(common) < MIN_COMMON:
        return None
    xa = np.array([a[y] for y in common], dtype=float)
    xb = np.array([b[y] for y in common], dtype=float)
    if np.allclose(xa, xb, rtol=0, atol=1e-9):
        return "duplicate_version", len(common), 1.0
    if np.ptp(xa) == 0 or np.ptp(xb) == 0:
        return None
    r = float(np.corrcoef(xa, xb)[0, 1])
    if abs(r) >= NEAR_IDENTICAL_R:
        return "near_identical", len(common), r
    return None


def detect_reprints(frame: pd.DataFrame):
    """返回 (reprint_of 映射, 转载对审计表 DataFrame)。

    reprint_of: record_id -> 规范 record_id（仅非规范记录有键）。
    """
    sites = frame.groupby("source_record_id")["geo_siteName"].agg(
        lambda s: s.dropna().iloc[0] if s.notna().any() else "")
    series = _record_series(frame)
    by_site = {}
    for record_id, site in sites.items():
        by_site.setdefault(str(site), []).append(record_id)

    parent = {r: r for r in series}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    pair_rows = []
    for site, records in by_site.items():
        records = sorted(records)
        for i in range(len(records)):
            for j in range(i + 1, len(records)):
                ra, rb = records[i], records[j]
                for proxy in series[ra]:
                    if proxy not in series[rb]:
                        continue
                    verdict = _compare(series[ra][proxy], series[rb][proxy])
                    if verdict is None:
                        continue
                    method, n_common, r = verdict
                    union(ra, rb)
                    pair_rows.append({"site_name": site, "record_a": ra, "record_b": rb,
                                      "proxy": proxy, "method": method,
                                      "n_common_years": n_common, "r": r})

    clusters = {}
    for record_id in series:
        clusters.setdefault(find(record_id), []).append(record_id)

    counts = frame.groupby("source_record_id").size()
    authors = frame.groupby("source_record_id")["pub1_authors"].agg(
        lambda s: s.dropna().iloc[0] if s.notna().any() else "")
    reprint_of = {}
    for members in clusters.values():
        if len(members) < 2:
            continue
        canonical = sorted(members, key=lambda r: (_pub_year(authors.get(r)),
                                                   -int(counts.get(r, 0)), str(r)))[0]
        for record_id in members:
            if record_id != canonical:
                reprint_of[record_id] = canonical
    logger.info("转载归并：%d 条记录中识别 %d 条非规范转载（%d 个证据对）",
                len(series), len(reprint_of), len(pair_rows))
    return reprint_of, pd.DataFrame(pair_rows)