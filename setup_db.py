"""Builds public_services.db (SQLite).

Step 1: base tables (districts, schools, hospitals, schemes, relief shelters).
Step 2: calls pipeline.run(), which builds the climate and disaster tables
        (acquisition -> quality control -> preprocessing -> analysis -> validation).

District names and populations are approximate (Census 2011 style); coordinates are
approximate district headquarters. Schools, hospitals, schemes and shelters are
SYNTHETIC sample data generated with a fixed seed, so results are identical each run.
"""
import os
import random
import sqlite3

random.seed(42)
DB_PATH = "public_services.db"

# name, population, latitude, longitude, coastal (1 = has a coastline)
DISTRICTS = [
    ("Chennai", 4646732, 13.08, 80.27, 1), ("Coimbatore", 3458045, 11.00, 76.96, 0),
    ("Madurai", 3038252, 9.92, 78.12, 0), ("Tiruchirappalli", 2722290, 10.79, 78.70, 0),
    ("Salem", 3482056, 11.66, 78.15, 0), ("Tirunelveli", 3077233, 8.73, 77.70, 1),
    ("Thanjavur", 2405890, 10.79, 79.14, 1), ("Vellore", 3936331, 12.92, 79.13, 0),
    ("Erode", 2251744, 11.34, 77.72, 0), ("Thoothukudi", 1750176, 8.76, 78.13, 1),
    ("Dindigul", 2159775, 10.37, 77.98, 0), ("Villupuram", 3458873, 11.94, 79.49, 1),
]

SCHEMES = ["Women Entitlement", "Student Breakfast", "Old Age Pension", "Free Ration"]

SCHEMA = """
CREATE TABLE districts (
    district_id   INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    population    INTEGER NOT NULL,
    literacy_rate REAL NOT NULL,           -- percent
    latitude      REAL NOT NULL,
    longitude     REAL NOT NULL,
    coastal       INTEGER NOT NULL         -- 1 = has a coastline (cyclone exposed), 0 = inland
);
CREATE TABLE schools (
    school_id   INTEGER PRIMARY KEY,
    district_id INTEGER NOT NULL REFERENCES districts(district_id),
    name        TEXT NOT NULL,
    type        TEXT NOT NULL CHECK (type IN ('Government','Aided','Private')),
    students    INTEGER NOT NULL,
    teachers    INTEGER NOT NULL
);
CREATE TABLE hospitals (
    hospital_id INTEGER PRIMARY KEY,
    district_id INTEGER NOT NULL REFERENCES districts(district_id),
    name        TEXT NOT NULL,
    type        TEXT NOT NULL CHECK (type IN ('PHC','CHC','District Hospital','Private')),
    beds        INTEGER NOT NULL
);
CREATE TABLE scheme_beneficiaries (
    id            INTEGER PRIMARY KEY,
    district_id   INTEGER NOT NULL REFERENCES districts(district_id),
    scheme_name   TEXT NOT NULL,
    year          INTEGER NOT NULL,
    beneficiaries INTEGER NOT NULL
);
CREATE TABLE relief_shelters (
    shelter_id   INTEGER PRIMARY KEY,
    district_id  INTEGER NOT NULL REFERENCES districts(district_id),
    name         TEXT NOT NULL,
    shelter_type TEXT NOT NULL CHECK (shelter_type IN ('School','Community Hall','Cyclone Shelter')),
    capacity     INTEGER NOT NULL,         -- people it can hold
    flood_safe   INTEGER NOT NULL          -- 1 = on high ground / not flood prone, 0 = can flood
);
"""

BED_RANGES = {"PHC": (6, 30), "CHC": (30, 100), "District Hospital": (200, 800), "Private": (10, 300)}


def build():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)

    for i, (name, pop, lat, lon, coastal) in enumerate(DISTRICTS, start=1):
        con.execute("INSERT INTO districts VALUES (?,?,?,?,?,?,?)",
                    (i, name, pop, round(random.uniform(68, 92), 1), lat, lon, coastal))

    school_id = hospital_id = row_id = shelter_id = 1
    for d, (name, pop, _lat, _lon, coastal) in enumerate(DISTRICTS, start=1):
        for n in range(1, 21):
            t = random.choices(["Government", "Aided", "Private"], weights=[50, 25, 25])[0]
            students = random.randint(80, 1500)
            teachers = max(3, students // random.randint(20, 40))
            con.execute("INSERT INTO schools VALUES (?,?,?,?,?,?)",
                        (school_id, d, f"{name} {t} School {n}", t, students, teachers))
            school_id += 1
        for n in range(1, 9):
            t = random.choices(["PHC", "CHC", "District Hospital", "Private"], weights=[40, 20, 5, 35])[0]
            lo, hi = BED_RANGES[t]
            con.execute("INSERT INTO hospitals VALUES (?,?,?,?,?)",
                        (hospital_id, d, f"{name} {t} {n}", t, random.randint(lo, hi)))
            hospital_id += 1
        for scheme in SCHEMES:
            for year in range(2022, 2026):
                con.execute("INSERT INTO scheme_beneficiaries VALUES (?,?,?,?,?)",
                            (row_id, d, scheme, year, int(pop * random.uniform(0.02, 0.25))))
                row_id += 1
        # relief shelters: 6-9 per district; cyclone shelters only exist in coastal districts
        types = ["School", "Community Hall"] + (["Cyclone Shelter"] if coastal else [])
        for n in range(1, random.randint(6, 9) + 1):
            t = random.choice(types)
            cap = {"School": (200, 900), "Community Hall": (100, 500), "Cyclone Shelter": (400, 1500)}[t]
            con.execute("INSERT INTO relief_shelters VALUES (?,?,?,?,?,?)",
                        (shelter_id, d, f"{name} {t} {n}", t, random.randint(*cap),
                         1 if random.random() < 0.7 else 0))
            shelter_id += 1

    con.commit()
    con.close()

    import pipeline  # climate + disaster tables, built in the same database
    pipeline.run(DB_PATH)

    con = sqlite3.connect(DB_PATH)
    print("\nTables in database:")
    for (table,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        print(f"  {table:22s} {con.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]:>7} rows")
    con.close()


if __name__ == "__main__":
    build()
