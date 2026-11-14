"""早期记录准入、频带敏感性、配对一致性与正文器测说明。"""

import numpy as np
import pandas as pd

from app.analysis import figures_validation as validation, loader


PROXIES = ("Sr/Ca", "d18O", "d13C")
POLICIES = (("Baseline", .80, 24), ("Short gaps", .90, 6), ("Complete", 1.0, 0))
EARLY_YEAR = 1850


def variance_results(monthly):
    outputs = []
    for name, coverage, gap in POLICIES:
        result = validation.compute_variance_decomposition(
            {"monthly": monthly}, min_coverage=coverage, max_gap=gap)
        if not result.empty:
            outputs.append(result.assign(policy=name))
    if outputs:
        return pd.concat(outputs, ignore_index=True)
    return pd.DataFrame(columns=["entity_id", "proxy", "policy"] +
                        [f"{band}_pct" for band, _, _ in validation.VARIANCE_BANDS])


def pair_results(frames):
    _, pairs = validation.compute_local_reproducibility(frames)
    if pairs.empty:
        return pairs.reindex(columns=["entity_a", "entity_b", "proxy", "resolution", "r",
                                      "duplicate_version", "near_identical", "status"])
    identity = pd.concat([part for part in frames.values() if not part.empty], ignore_index=True)
    site = "geo_siteName" if "geo_siteName" in identity else "Site"
    metadata = identity.groupby("entity_id")[["source_file", "source_record_id", "paper_hash", site]].first()
    left = metadata.reindex(pairs["entity_a"]).reset_index(drop=True)
    right = metadata.reindex(pairs["entity_b"]).reset_index(drop=True)
    documented_id = left["source_record_id"].fillna("").astype(str).str.strip().ne("")
    related = left["paper_hash"].fillna("").ne("") & left["paper_hash"].eq(right["paper_hash"])
    same_core = (documented_id & left["source_record_id"].eq(right["source_record_id"])
                 & ((related & left[site].eq(right[site])) | left["source_file"].eq(right["source_file"])))
    pairs["status"] = np.select(
        [same_core, pairs["duplicate_version"], pairs["near_identical"]],
        ["Shared source record ID", "Exact matched values", "Near-identical; identity unresolved"],
        default="No shared ID or exact values detected")
    return pairs


def environmental_results(run, external, summaries, submonthly, output):
    sst_path = loader.resolve_grid_file(run, loader.SST_NC_NAME, external)
    sss_path = loader.resolve_grid_file(run, loader.SSS_NC_NAME, external)
    if not sst_path:
        raise FileNotFoundError("ERSST grid is needed for the text diagnostics")
    sst = loader.open_grid_dataset(sst_path)
    sss = loader.open_grid_dataset(sss_path)
    try:
        return compute_environment(sst.load(), sss.load() if sss is not None else None,
                                   summaries, submonthly, output)
    finally:
        sst.close()
        if sss is not None:
            sss.close()


def compute_environment(sst, sss, summaries, submonthly, output):
    # Cache the 30-year normal as a 12-month grid before iterating over sites.
    normals = (sst["sst"].sel(time=slice("1981-01-01", "2010-12-31"))
               .groupby("time.month").mean("time").load())
    sst_first = int(sst["time"].dt.year.min())
    sst_last = int(sst["time"].dt.year.max())
    monthly = summaries["monthly"]
    results = []
    seasonal_examples = {}
    for name, frame in (("Monthly", monthly), ("Submonthly", submonthly)):
        frame = frame.loc[frame["qc_location_status"].eq("pass")
                          & frame["lat"].between(-90, 90) & frame["lon"].between(-180, 180)]
        for entity, series in frame.groupby("entity_id", sort=True):
            lat, lon = series.iloc[0][["lat", "lon"]]
            grid_lon = lon % 360 if float(normals["lon"].min()) >= 0 else lon
            normal = normals.sel(lat=lat, lon=grid_lon, method="nearest").to_series()
            instrumental = validation.extract_grid_val(sst, lat, lon)
            for proxy in PROXIES[:2]:
                # First average within each year-month, to avoid weighting densely sampled months.
                calendar = series[["Year_int", "Month", proxy]].dropna()
                calendar = calendar.groupby(["Year_int", "Month"], as_index=False)[proxy].mean()
                if calendar.empty:
                    continue
                calendar["Year"] = calendar["Year_int"] + (calendar["Month"] - 1) / 12
                dates = pd.DatetimeIndex(instrumental.index) if instrumental is not None else pd.DatetimeIndex([])
                available = instrumental if len(dates) else None
                paired = validation._paired_environment(calendar, proxy, available)
                contemporaneous = (len(paired) >= 12 and paired.index.month.nunique() >= 6
                                   and paired.index.year.nunique() >= 3)
                if contemporaneous:
                    _, _, r = validation._seasonal_statistics(paired)
                    if np.isfinite(r):
                        results.append({"entity_id": entity, "resolution": name, "proxy": proxy,
                                        "comparison": "Common-period seasonal climatology",
                                        "n_calendar_months": paired.index.month.nunique(),
                                        "n_year_months": len(paired), "r": r})
                        if name == "Monthly" and (
                            proxy not in seasonal_examples
                            or len(paired) > len(seasonal_examples[proxy]["pairs"])
                        ):
                            seasonal_examples[proxy] = {"site": series["Site"].iloc[0], "pairs": paired}
                # A separate modern analogue for portions outside instrumental coverage.
                outside = calendar.loc[~calendar["Year_int"].between(sst_first, sst_last)]
                by_month = outside.groupby("Month")[proxy].agg(["mean", "size"])
                common = by_month.loc[by_month["size"].ge(3), "mean"].to_frame("proxy").join(
                    normal.rename("sst")).dropna()
                if len(common) >= 8 and common.nunique().min() >= 2:
                    results.append({"entity_id": entity, "resolution": name, "proxy": proxy,
                                    "comparison": "Non-overlap vs 1981–2010 SST climatology",
                                    "n_calendar_months": len(common),
                                    "n_year_months": int(by_month.loc[common.index, "size"].sum()),
                                    "r": common["proxy"].corr(common["sst"])})
    seasonality = pd.DataFrame(results)
    seasonality.to_csv(output / "seasonal_climatology.csv", index=False, encoding="utf-8-sig")
    if seasonal_examples:
        pd.concat([example["pairs"].assign(proxy=proxy)
                   for proxy, example in seasonal_examples.items()]).to_csv(
            output / "seasonal_example_pairs.csv", index_label="year_month", encoding="utf-8-sig")
    simultaneous = validation.calculate_correlations(summaries, sst, sss)
    simultaneous.to_csv(output / "instrumental_correlations.csv", index=False, encoding="utf-8-sig")

    nino = validation.compute_nino34(sst)
    entity, enso = validation.select_enso_record(monthly, nino)
    enso_pairs = None
    if enso:
        record = monthly.loc[monthly["entity_id"].eq(entity)]
        enso.update(entity_id=entity, site=record["Site"].iloc[0])
        anomaly = validation._monthly_anomaly(record, "d18O")
        enso_pairs = anomaly.to_frame("coral_d18O_anomaly").join(nino, how="inner").dropna()
        enso_pairs.to_csv(output / "enso_paired_anomalies.csv", index_label="year_month", encoding="utf-8-sig")
    return seasonality, simultaneous, seasonal_examples, enso, enso_pairs, (sst_first, sst_last)
