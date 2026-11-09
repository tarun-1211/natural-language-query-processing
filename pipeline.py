"""Climate data pipeline, following the workflow on the course slides:

  1. Data acquisition   -> raw_climate.csv (real, from fetch_climate.py) or generated sample data
  2. Quality control    -> find and fix missing / impossible values, log everything
  3. Pre-processing     -> daily table + monthly summary table
  4. Analysis           -> disaster events + district risk index
  5. Validation         -> RMSE, bias, correlation of the gap-filling method
  (Visualisation is the dashboard in app.py)

Run automatically by setup_db.py, or on its own:  python pipeline.py
"""
import math
import os
import random
import sqlite3

import numpy as np
import pandas as pd

DB_PATH = "public_services.db"
RAW_CSV = "raw_climate.csv"

# --- thresholds (IMD rainfall classes; heat threshold simplified) -------------------------
HEAVY_RAIN_MM = 64.5     # IMD "heavy rain" starts at 64.5 mm in 24 h
VERY_HEAVY_MM = 115.6    # IMD "very heavy rain" starts at 115.6 mm
RAINY_DAY_MM = 2.5       # IMD counts a day as rainy at >= 2.5 mm
HOT_DAY_C = 40.0         # simplified: IMD's official heatwave rule also needs a departure from normal
MAX_GAP_DAYS = 3         # temperature gaps up to this long are filled by linear interpolation

SCHEMA = """
DROP TABLE IF EXISTS climate_daily;
DROP TABLE IF EXISTS climate_monthly;
DROP TABLE IF EXISTS disaster_events;
DROP TABLE IF EXISTS district_risk;
DROP TABLE IF EXISTS qc_log;
DROP TABLE IF EXISTS validation_metrics;
DROP TABLE IF EXISTS pipeline_meta;

CREATE TABLE climate_daily (
    id          INTEGER PRIMARY KEY,
    district_id INTEGER NOT NULL REFERENCES districts(district_id),
    date        TEXT NOT NULL,             -- ISO text 'YYYY-MM-DD'
    rainfall_mm REAL,                      -- rain in 24 h; NULL = missing
    tmax_c      REAL,                      -- daily maximum temperature, degrees Celsius
    tmin_c      REAL,                      -- daily minimum temperature, degrees Celsius
    qc_flag     TEXT NOT NULL              -- ok / interpolated / corrected / missing_rain / missing_temp
);
CREATE TABLE climate_monthly (
    district_id      INTEGER NOT NULL REFERENCES districts(district_id),
    year             INTEGER NOT NULL,
    month            INTEGER NOT NULL,     -- 1 to 12
    total_rain_mm    REAL,
    rainy_days       INTEGER,              -- days with >= 2.5 mm
    max_daily_rain_mm REAL,
    heavy_rain_days  INTEGER,              -- days with >= 64.5 mm (IMD heavy rain)
    mean_tmax_c      REAL,
    hot_days         INTEGER,              -- days with maximum temperature >= 40 C
    PRIMARY KEY (district_id, year, month)
);
CREATE TABLE disaster_events (
    event_id         INTEGER PRIMARY KEY,
    district_id      INTEGER NOT NULL REFERENCES districts(district_id),
    event_type       TEXT NOT NULL CHECK (event_type IN ('Flood','Cyclone','Heatwave')),
    year             INTEGER NOT NULL,
    month            INTEGER NOT NULL,
    people_affected  INTEGER NOT NULL,
    deaths           INTEGER NOT NULL,
    damage_crore_inr REAL NOT NULL         -- estimated damage in crore rupees
);
CREATE TABLE district_risk (
    district_id   INTEGER PRIMARY KEY REFERENCES districts(district_id),
    flood_score   REAL NOT NULL,           -- 0-100, from heavy-rain days and highest daily rainfall
    heat_score    REAL NOT NULL,           -- 0-100, from number of hot days
    history_score REAL NOT NULL,           -- 0-100, from people affected per 1000 residents
    overall_risk  REAL NOT NULL,           -- 0-100, weighted: 40% flood, 30% heat, 30% history
    risk_category TEXT NOT NULL CHECK (risk_category IN ('High','Medium','Low'))  -- relative: top, middle, bottom third
);
CREATE TABLE qc_log (
    step TEXT, rows_affected INTEGER, action TEXT
);
CREATE TABLE validation_metrics (
    variable TEXT, gap_days INTEGER, n_points INTEGER, rmse REAL, bias REAL, correlation REAL
);
CREATE TABLE pipeline_meta (
    key TEXT PRIMARY KEY, value TEXT
);
"""

# ------------------------------------------------------------------------------------------
# 1. DATA ACQUISITION
# ------------------------------------------------------------------------------------------
# name: (north-east monsoon rain factor, south-west monsoon rain factor, temperature offset C)
PROFILE = {
    "Chennai": (1.6, 0.7, 0.0), "Coimbatore": (0.7, 0.9, -3.0), "Madurai": (1.0, 0.4, 0.5),
    "Tiruchirappalli": (1.0, 0.5, 1.2), "Salem": (0.8, 0.8, -0.5), "Tirunelveli": (1.2, 0.4, -0.3),
    "Thanjavur": (1.4, 0.5, 0.3), "Vellore": (1.0, 0.8, 1.5), "Erode": (0.8, 0.7, 1.0),
    "Thoothukudi": (1.2, 0.3, -0.5), "Dindigul": (0.9, 0.6, -1.0), "Villupuram": (1.4, 0.7, 0.0),
}
RAIN_DAY_PROB = [.06, .04, .05, .10, .15, .18, .20, .22, .25, .38, .38, .22]   # per month
TMAX_BASE = [31, 33, 35.5, 37.5, 38.5, 36.5, 35.5, 35, 34.5, 32.5, 30.5, 30]   # per month, plains


def synthetic_raw(start="2019-01-01", end="2025-12-31", seed=2026):
    """Generated sample data with a Tamil Nadu style monsoon pattern, plus deliberately
    injected errors (gaps, negative rainfall, spikes, swapped columns) so the quality-control
    step has something real to find. Used only when raw_climate.csv does not exist."""
    rng = np.random.default_rng(seed)
    dates = pd.date_range(start, end)
    n = len(dates)
    m = dates.month.values
    ne = (m >= 10) & (m <= 12)
    sw = (m >= 6) & (m <= 9)
    rows = []
    for name, (ne_f, sw_f, off) in PROFILE.items():
        factor = np.where(ne, ne_f, np.where(sw, sw_f, 1.0))
        p = np.clip(np.array(RAIN_DAY_PROB)[m - 1] * factor, 0, 0.85)
        rainy = rng.random(n) < p
        scale = np.where(ne, 24 * ne_f, np.where(sw, 16 * sw_f, 14.0))
        rain = np.where(rainy, rng.gamma(0.55, scale), 0.0).round(1)
        tmax = np.array(TMAX_BASE)[m - 1] + off + rng.normal(0, 1.3, n) - np.minimum(rain, 60) / 20
        tmin = tmax - rng.uniform(7, 11, n)
        tmax, tmin = tmax.round(1), tmin.round(1)

        # inject errors
        tmax[rng.choice(n, int(n * 0.012), replace=False)] = np.nan
        tmin[rng.choice(n, int(n * 0.012), replace=False)] = np.nan
        for start_i in rng.choice(n - 5, 4, replace=False):          # a few 2-3 day gaps
            tmax[start_i:start_i + rng.integers(2, 4)] = np.nan
        for start_i in rng.choice(n - 12, 2, replace=False):         # and a long gap (cannot be filled)
            tmax[start_i:start_i + 8] = np.nan
        rain[rng.choice(n, int(n * 0.004), replace=False)] = np.nan
        rain[rng.choice(n, max(2, int(n * 0.001)), replace=False)] = -9999.0   # sentinel value
        swap = rng.choice(n, max(2, int(n * 0.001)), replace=False)
        tmax[swap], tmin[swap] = tmin[swap].copy(), tmax[swap].copy()
        spike = rng.choice(n, max(2, int(n * 0.0005)), replace=False)
        tmax[spike] = 99.9

        rows.append(pd.DataFrame({
            "district": name, "date": dates.strftime("%Y-%m-%d"), "rainfall_mm": rain,
            "tmax_c": tmax, "tmin_c": tmin, "source": "SYNTHETIC sample data (generated for this project)",
        }))
    return pd.concat(rows, ignore_index=True)


def acquire(raw_csv=RAW_CSV):
    if os.path.exists(raw_csv):
        raw = pd.read_csv(raw_csv)
        print(f"[1/5] Data acquisition: loaded REAL data from {raw_csv} ({len(raw):,} rows)")
    else:
        raw = synthetic_raw()
        print(f"[1/5] Data acquisition: {raw_csv} not found, generated SAMPLE data ({len(raw):,} rows). "
              "Run fetch_climate.py first for real data.")
    return raw


# ------------------------------------------------------------------------------------------
# 2. QUALITY CONTROL
# ------------------------------------------------------------------------------------------
def fill_short_gaps(values, max_gap=MAX_GAP_DAYS):
    """Linear interpolation, but ONLY across gaps of <= max_gap days that have valid values on
    both sides. Longer gaps are left missing on purpose (guessing there would invent data)."""
    v = values.astype(float).copy()
    filled = np.zeros(len(v), dtype=bool)
    i = 0
    while i < len(v):
        if np.isnan(v[i]):
            j = i
            while j < len(v) and np.isnan(v[j]):
                j += 1
            if i > 0 and j < len(v) and (j - i) <= max_gap:
                v[i:j] = np.interp(np.arange(i, j), [i - 1, j], [v[i - 1], v[j]])
                filled[i:j] = True
            i = j
        else:
            i += 1
    return v, filled


def quality_control(raw):
    raw = raw.copy()
    raw["date"] = pd.to_datetime(raw["date"])
    n_dup = int(raw.duplicated(["district", "date"]).sum())
    raw = raw.drop_duplicates(["district", "date"])
    all_dates = pd.date_range(raw["date"].min(), raw["date"].max())

    c = dict(added=0, bad_rain=0, bad_temp=0, swapped=0, miss_rain=0, miss_temp=0, filled=0, left=0)
    cleaned = []
    for name, g in raw.groupby("district"):
        g = g.set_index("date")
        c["added"] += len(all_dates) - len(g)
        g = g.reindex(all_dates)
        rain = g["rainfall_mm"].to_numpy(float).copy()
        tmax = g["tmax_c"].to_numpy(float).copy()
        tmin = g["tmin_c"].to_numpy(float).copy()

        bad_rain = (rain < 0) | (rain > 1000)
        bad_tx = (tmax < -10) | (tmax > 55)
        bad_tn = (tmin < -10) | (tmin > 55)
        rain[bad_rain], tmax[bad_tx], tmin[bad_tn] = np.nan, np.nan, np.nan
        c["bad_rain"] += int(bad_rain.sum())
        c["bad_temp"] += int(bad_tx.sum() + bad_tn.sum())

        swap = tmax < tmin
        tmp = tmax[swap].copy()
        tmax[swap] = tmin[swap]
        tmin[swap] = tmp
        c["swapped"] += int(swap.sum())

        c["miss_rain"] += int(np.isnan(rain).sum())
        c["miss_temp"] += int(np.isnan(tmax).sum() + np.isnan(tmin).sum())
        tmax, f1 = fill_short_gaps(tmax)
        tmin, f2 = fill_short_gaps(tmin)
        c["filled"] += int(f1.sum() + f2.sum())
        c["left"] += int(np.isnan(tmax).sum() + np.isnan(tmin).sum())

        corrected = bad_rain | bad_tx | bad_tn | swap
        flag = np.select(
            [corrected, f1 | f2, np.isnan(tmax) | np.isnan(tmin), np.isnan(rain)],
            ["corrected", "interpolated", "missing_temp", "missing_rain"], default="ok")
        cleaned.append(pd.DataFrame({
            "district": name, "date": all_dates.strftime("%Y-%m-%d"),
            "rainfall_mm": rain, "tmax_c": tmax, "tmin_c": tmin, "qc_flag": flag}))

    log = [
        ("Duplicate rows", n_dup, "Removed"),
        ("Missing days (no row at all)", c["added"], "Added as empty rows"),
        ("Impossible rainfall (< 0 or > 1000 mm)", c["bad_rain"], "Set to missing"),
        ("Impossible temperature (outside -10 to 55 C)", c["bad_temp"], "Set to missing"),
        ("Tmax lower than Tmin", c["swapped"], "Values swapped"),
        ("Missing temperature values (before filling)", c["miss_temp"], "Counted"),
        (f"Temperature gaps of <= {MAX_GAP_DAYS} days", c["filled"], "Filled by linear interpolation"),
        (f"Temperature gaps longer than {MAX_GAP_DAYS} days", c["left"], "Left missing (not guessed)"),
        ("Missing rainfall values", c["miss_rain"], "Left as NULL (rain is intermittent, so not interpolated)"),
    ]
    print("[2/5] Quality control:")
    for step, n, action in log:
        print(f"        {step:50s} {n:>6}  -> {action}")
    return pd.concat(cleaned, ignore_index=True), log


# ------------------------------------------------------------------------------------------
# 3. PRE-PROCESSING (daily -> monthly)
# ------------------------------------------------------------------------------------------
def preprocess(daily):
    d = daily.copy()
    dt = pd.to_datetime(d["date"])
    d["year"], d["month"] = dt.dt.year, dt.dt.month
    g = d.groupby(["district_id", "year", "month"])
    monthly = pd.DataFrame({
        "total_rain_mm": g["rainfall_mm"].sum(min_count=1).round(1),
        "rainy_days": g["rainfall_mm"].apply(lambda s: int((s >= RAINY_DAY_MM).sum())),
        "max_daily_rain_mm": g["rainfall_mm"].max().round(1),
        "heavy_rain_days": g["rainfall_mm"].apply(lambda s: int((s >= HEAVY_RAIN_MM).sum())),
        "mean_tmax_c": g["tmax_c"].mean().round(1),
        "hot_days": g["tmax_c"].apply(lambda s: int((s >= HOT_DAY_C).sum())),
    }).reset_index()
    print(f"[3/5] Pre-processing: {len(daily):,} daily rows -> {len(monthly):,} monthly summaries")
    return monthly


# ------------------------------------------------------------------------------------------
# 4. ANALYSIS (events + risk index)
# ------------------------------------------------------------------------------------------
def generate_events(monthly, districts):
    """Disaster events are SYNTHETIC but rule-based, so they are consistent with the climate
    data: floods follow heavy-rain months, heatwaves follow months with many hot days, and
    cyclones hit coastal districts in Oct-Dec. Replace with real records (e.g. from the state
    disaster management authority) if you have them."""
    rng = random.Random(7)
    pop = dict(zip(districts["district_id"], districts["population"]))
    coastal = dict(zip(districts["district_id"], districts["coastal"]))
    events = []
    for r in monthly.sort_values(["district_id", "year", "month"]).itertuples():
        p = pop[r.district_id]
        if r.max_daily_rain_mm >= VERY_HEAVY_MM or r.heavy_rain_days >= 2:
            sev = 2 if r.max_daily_rain_mm >= VERY_HEAVY_MM else 1
            events.append((r.district_id, "Flood", r.year, r.month, int(p * rng.uniform(0.0005, 0.003) * sev),
                           rng.randint(0, 3) * sev, round(rng.uniform(5, 40) * sev, 1)))
        if r.hot_days >= 12:
            events.append((r.district_id, "Heatwave", r.year, r.month, int(p * rng.uniform(0.0002, 0.001)),
                           rng.randint(0, 5), round(rng.uniform(1, 10), 1)))
    for did in sorted(pop):
        if coastal[did]:
            for year in sorted(monthly["year"].unique()):
                if rng.random() < 0.12:
                    events.append((did, "Cyclone", int(year), rng.choices([10, 11, 12], [3, 5, 2])[0],
                                   int(pop[did] * rng.uniform(0.01, 0.04)), rng.randint(0, 15),
                                   round(rng.uniform(100, 800), 1)))
    return events


def minmax(s):
    return (s - s.min()) / (s.max() - s.min()) if s.max() > s.min() else s * 0


def risk_index(monthly, events, districts):
    n_years = monthly["year"].nunique()
    yearly_max = monthly.groupby(["district_id", "year"])["max_daily_rain_mm"].max().groupby("district_id").mean()
    heavy = monthly.groupby("district_id")["heavy_rain_days"].sum() / n_years
    hot = monthly.groupby("district_id")["hot_days"].sum() / n_years
    ev = pd.DataFrame(events, columns=["district_id", "type", "year", "month", "affected", "deaths", "damage"])
    affected = ev.groupby("district_id")["affected"].sum().reindex(districts["district_id"]).fillna(0)
    per_1000 = affected / (districts.set_index("district_id")["population"] / 1000)

    flood = 100 * (0.5 * minmax(heavy) + 0.5 * minmax(yearly_max))
    heat = 100 * minmax(hot)
    hist = 100 * minmax(per_1000)
    overall = 0.4 * flood + 0.3 * heat + 0.3 * hist
    order = overall.sort_values(ascending=False).index.tolist()
    k = math.ceil(len(order) / 3)
    category = {d: ("High" if i < k else "Medium" if i < 2 * k else "Low") for i, d in enumerate(order)}
    rows = [(int(d), round(float(flood[d]), 1), round(float(heat[d]), 1), round(float(hist[d]), 1),
             round(float(overall[d]), 1), category[d]) for d in districts["district_id"]]
    print(f"[4/5] Analysis: {len(events)} events generated, risk index computed for {len(rows)} districts")
    return rows


# ------------------------------------------------------------------------------------------
# 5. VALIDATION (RMSE, bias, correlation, as on the slides)
# ------------------------------------------------------------------------------------------
def validate_gap_filling(daily, seed=11, samples=300):
    """Hold-out test: hide known good temperature values, re-estimate them with the same
    interpolation used in quality control, then compare estimate vs truth."""
    rng = np.random.default_rng(seed)
    out = []
    series = [g["tmax_c"].to_numpy(float) for _, g in daily.groupby("district_id")]
    for gap in range(1, MAX_GAP_DAYS + 1):
        est, true = [], []
        for _ in range(samples):
            v = series[rng.integers(len(series))]
            i = int(rng.integers(1, len(v) - gap - 1))
            window = v[i - 1:i + gap + 1]
            if np.isnan(window).any():
                continue
            est += list(np.interp(np.arange(i, i + gap), [i - 1, i + gap], [v[i - 1], v[i + gap]]))
            true += list(v[i:i + gap])
        est, true = np.array(est), np.array(true)
        out.append(("tmax_c", gap, len(est), round(float(np.sqrt(np.mean((est - true) ** 2))), 2),
                    round(float(np.mean(est - true)), 2), round(float(np.corrcoef(est, true)[0, 1]), 3)))
    print("[5/5] Validation (hold-out test of temperature gap filling):")
    for _, gap, n, rmse, bias, corr in out:
        print(f"        gap {gap} day(s): n={n:4d}  RMSE={rmse} C  bias={bias} C  correlation={corr}")
    return out


# ------------------------------------------------------------------------------------------
def run(db_path=DB_PATH, raw_csv=RAW_CSV):
    con = sqlite3.connect(db_path)
    districts = pd.read_sql("SELECT district_id, name, population, coastal FROM districts", con)
    con.executescript(SCHEMA)

    raw = acquire(raw_csv)
    source = str(raw["source"].iloc[0]) if "source" in raw.columns else "unknown"
    raw = raw[raw["district"].isin(districts["name"])]
    daily, qc = quality_control(raw)
    daily = daily.merge(districts[["district_id", "name"]], left_on="district", right_on="name")
    daily = daily.sort_values(["district_id", "date"]).reset_index(drop=True)

    monthly = preprocess(daily)
    events = generate_events(monthly, districts)
    risk = risk_index(monthly, events, districts)
    validation = validate_gap_filling(daily)

    def clean(x):
        return None if pd.isna(x) else (x.item() if hasattr(x, "item") else x)

    con.executemany("INSERT INTO climate_daily (district_id,date,rainfall_mm,tmax_c,tmin_c,qc_flag) VALUES (?,?,?,?,?,?)",
                    [tuple(clean(v) for v in r) for r in
                     daily[["district_id", "date", "rainfall_mm", "tmax_c", "tmin_c", "qc_flag"]].itertuples(index=False)])
    con.executemany("INSERT INTO climate_monthly VALUES (?,?,?,?,?,?,?,?,?)",
                    [tuple(clean(v) for v in r) for r in
                     monthly[["district_id", "year", "month", "total_rain_mm", "rainy_days", "max_daily_rain_mm",
                              "heavy_rain_days", "mean_tmax_c", "hot_days"]].itertuples(index=False)])
    con.executemany("INSERT INTO disaster_events (district_id,event_type,year,month,people_affected,deaths,damage_crore_inr) "
                    "VALUES (?,?,?,?,?,?,?)", events)
    con.executemany("INSERT INTO district_risk VALUES (?,?,?,?,?,?)", risk)
    con.executemany("INSERT INTO qc_log VALUES (?,?,?)", qc)
    con.executemany("INSERT INTO validation_metrics VALUES (?,?,?,?,?,?)", validation)
    real = "SYNTHETIC" not in source
    con.executemany("INSERT INTO pipeline_meta VALUES (?,?)", [
        ("climate_source", source),
        ("climate_is_real", "yes" if real else "no"),
        ("period", f"{daily['date'].min()} to {daily['date'].max()}"),
        ("events_note", "Disaster events are SYNTHETIC, generated by rules from the climate data."),
    ])
    con.commit()
    con.close()


if __name__ == "__main__":
    run()
