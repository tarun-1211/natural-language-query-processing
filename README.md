# Disaster Risk Assistant

A climate data pipeline and multilingual text-to-SQL assistant for flood and heat risk in Tamil Nadu.

Built for Database Systems, Digital Assignment 2 (LLM projects).

Daily rainfall and temperature for 12 districts (2019 to 2025) go through quality control, validation and
aggregation into a district risk index. Anyone can then ask questions, or get a public advisory, in
English, Tamil or Hindi. An LLM writes the SQL, but it can only read the database, never change it.

## Features

- **Climate data workflow** that follows the standard steps: acquisition, quality control, pre-processing,
  analysis, visualisation, validation. Every cleaning action is logged.
- **Validated gap filling**: short temperature gaps are interpolated, and the method is tested by hiding
  known values and measuring RMSE, bias and correlation.
- **District risk index** (flood, heat and past impact) using IMD rainfall thresholds.
- **Text-to-SQL with safety**: schema-aware and value-aware prompt, SELECT-only validator, read-only
  database connection, and execution-guided self-repair (the error is fed back to the model, up to 2 retries).
- **Multilingual**: questions and answers in English, Tamil and Hindi.
- **Public advisory**: a short advisory per district written from database facts only.
- **Streamlit app** with three tabs: Ask, Risk dashboard, Data pipeline.

## Architecture

```
Open-Meteo (ERA5-based) or generated sample data
        -> pipeline.py (quality control, aggregation, events, risk index, validation)
        -> SQLite database (9 main tables + 3 bookkeeping tables)
        <- core.py: prompt builder -> Gemini API -> SQL validator -> read-only executor -> self-repair
        <- app.py: Streamlit front end
```

## Setup

Requires Python 3.10+.

1. Install the libraries:
   ```
   pip install -r requirements.txt
   ```
2. Get a free API key at https://aistudio.google.com/apikey and set it in the same terminal you will run the app from:
   - Windows PowerShell: `$env:GEMINI_API_KEY="your_key_here"`
   - Windows cmd: `set GEMINI_API_KEY=your_key_here`
   - Mac/Linux: `export GEMINI_API_KEY=your_key_here`
3. Optional, for real climate data (needs internet, about 1 minute):
   ```
   python fetch_climate.py
   ```
   If you skip it, the project uses generated sample data and the app shows a warning.
4. Build the database and run the whole pipeline:
   ```
   python setup_db.py
   ```
5. Start the app:
   ```
   python -m streamlit run app.py
   ```
6. Optional: run the test questions (about 10 minutes, free-tier rate limits apply):
   ```
   python evaluate.py
   ```

Stop the app with Ctrl + C. The API key must be set again in each new terminal.
If you see a "model not found" error, set `GEMINI_MODEL` to a current Flash model name from AI Studio.

### No API key or internet?

```
python demo.py
```

This prints the data source, quality-control log, validation results, risk ranking, example queries
and a demonstration of the safety layer. It does not call any LLM.

## Project files

| File | Purpose |
|---|---|
| `fetch_climate.py` | Optional real-data download from Open-Meteo, writes `raw_climate.csv` |
| `setup_db.py` | Creates base tables, then runs the pipeline |
| `pipeline.py` | Acquisition, quality control, aggregation, events, risk index, validation |
| `core.py` | Prompting, SQL validation, safe execution, self-repair, advisory |
| `app.py` | Streamlit front end |
| `evaluate.py` | Test harness: 20 questions (16 English, 2 Tamil, 2 Hindi), baseline vs validation + repair |
| `demo.py` | Offline demonstration, no API needed |

## Database

Main tables: `districts`, `schools`, `hospitals`, `scheme_beneficiaries`, `relief_shelters`,
`climate_daily`, `climate_monthly`, `disaster_events`, `district_risk`.
Bookkeeping tables (hidden from the LLM): `qc_log`, `validation_metrics`, `pipeline_meta`.

`climate_monthly` and `district_risk` are derived tables. Every table links to `districts` through `district_id`.

## Methodology in brief

**Quality control**: duplicates removed, missing days added, rainfall below 0 or above 1000 mm and temperature
outside -10 to 55 C set to missing, reversed Tmax/Tmin swapped, temperature gaps of up to 3 days
interpolated. Rainfall gaps are never filled, because rain is intermittent and interpolation would
invent it.

**Thresholds** (IMD): heavy rain is 64.5 mm or more in a day, very heavy is 115.6 mm or more, a rainy day is
2.5 mm or more. A hot day is a maximum temperature of 40 C or more (simplified, IMD's official heatwave
rule also needs a departure from normal).

**Risk index**: flood score and heat score from yearly indicators, a past-impact score from the events
table, each scaled 0 to 100 across districts, combined as 40% flood, 30% heat, 30% past impact. The
category (High, Medium, Low) is relative: top, middle and bottom third of the districts.

**Text-to-SQL**: the prompt contains the table definitions, the real categorical values in the data and
the thresholds. The returned SQL must be a single SELECT, and runs on a read-only connection.
If it fails, the error is returned to the model for a corrected query (at most 2 retries).
A sidebar toggle turns validation and repair off to give a plain baseline.

## Data and limitations (please read)

- Climate values are real (ERA5-based, via Open-Meteo) only if you ran `fetch_climate.py`.
  Otherwise they are generated sample data with deliberately injected errors, and the app shows a warning.
- Disaster events, schools, hospitals, welfare schemes and relief shelters are **synthetic**.
- Events are generated by rules from the climate data, so the past-impact part of the risk index is
  partly circular. The ranking reflects those rules and is not a real risk assessment.
- Reanalysis values are gridded estimates for the district headquarters, not weather-station readings.
- Validation figures from the sample data show that the method works, not how it performs on real weather.
- The LLM can still write a wrong query that is safe but incorrect. Check important answers.
- Free-tier API limits apply. Do not use this for real emergency decisions. Check IMD and the state
  disaster management authority.

## Credits

If you use the download script: Weather data by [Open-Meteo.com](https://open-meteo.com/) (CC BY 4.0),
contains modified Copernicus Climate Change Service (ERA5) information.

## Author

Tarun Hrishikesh Venkatesh
