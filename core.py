"""Core logic: schema-aware prompting, SQL validation, execution, self-repair, advisories."""
import os
import re
import sqlite3
import time

DB_PATH = "public_services.db"

MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")


HIDDEN_TABLES = ("qc_log", "validation_metrics", "pipeline_meta")

FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|vacuum|truncate)\b", re.I
)

_client = None


# ---------------------------------------------------------------- LLM
def ask_llm(prompt: str) -> str:
    global _client
    if _client is None:
        from google import genai  # imported lazily so the module loads without the package
        key = os.getenv("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("GEMINI_API_KEY is not set. See README step 3.")
        _client = genai.Client(api_key=key)
    for attempt in range(5):
            try:
                return _client.models.generate_content(model=MODEL, contents=prompt).text
            except Exception as e:
                raise RuntimeError(f"Gemini call failed: {e}")


# ---------------------------------------------------------------- schema-aware prompt
def get_schema() -> str:
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    con.close()
    return "\n".join(sql for name, sql in rows if name not in HIDDEN_TABLES)


def get_value_hints() -> str:
    """Tell the model which categorical values really exist in the data."""
    con = sqlite3.connect(DB_PATH)

    def q(sql):
        return ", ".join(str(r[0]) for r in con.execute(sql))

    y0, y1 = con.execute("SELECT MIN(year), MAX(year) FROM climate_monthly").fetchone()
    hints = (
        f"districts.name values: {q('SELECT name FROM districts')}\n"
        f"schools.type values: {q('SELECT DISTINCT type FROM schools')}\n"
        f"hospitals.type values: {q('SELECT DISTINCT type FROM hospitals')} "
        "(PHC = Primary Health Centre, CHC = Community Health Centre)\n"
        f"relief_shelters.shelter_type values: {q('SELECT DISTINCT shelter_type FROM relief_shelters')} "
        "(relief shelters = relief camps = evacuation centres)\n"
        f"disaster_events.event_type values: {q('SELECT DISTINCT event_type FROM disaster_events')}\n"
        f"district_risk.risk_category values: High, Medium, Low\n"
        f"scheme_beneficiaries.scheme_name values: {q('SELECT DISTINCT scheme_name FROM scheme_beneficiaries')}\n"
        f"scheme_beneficiaries.year values: {q('SELECT DISTINCT year FROM scheme_beneficiaries ORDER BY year')}\n"
        f"climate data covers years {y0} to {y1} (climate_monthly.year, disaster_events.year)"
    )
    con.close()
    return hints


def build_prompt(question, schema, bad_sql=None, error=None) -> str:
    prompt = f"""You are an expert SQLite analyst for a disaster-management database.
Convert the user's question into ONE SQLite SELECT query.

DATABASE SCHEMA:
{schema}

VALUES THAT EXIST IN THE DATA:
{get_value_hints()}

RULES:
- The question may be in English, Tamil or Hindi. All data values are stored in English,
  so translate/transliterate place names and terms to the English values listed above.
- Prefer climate_monthly over climate_daily unless the question needs individual days
  (for example the highest single-day rainfall). climate_daily.date is text 'YYYY-MM-DD';
  use strftime('%Y', date) for the year.
- "heavy rain" means 64.5 mm or more in a day; a "hot day" means a maximum temperature of 40 C or more.
- Join to districts to show district names instead of ids.
- Output ONLY the SQL query, no explanation, no markdown.
- Only SELECT statements. Never modify data.

QUESTION: {question}
"""
    if bad_sql:
        prompt += f"""
Your previous attempt failed.
PREVIOUS SQL: {bad_sql}
ERROR: {error}
Fix the query and output ONLY the corrected SQL.
"""
    return prompt


def extract_sql(text: str) -> str:
    text = text.strip()
    m = re.search(r"```(?:sql)?\s*(.*?)```", text, re.S | re.I)
    return (m.group(1) if m else text).strip()


# ---------------------------------------------------------------- validation layer
def validate_sql(sql: str):
    s = sql.strip().rstrip(";").strip()
    if not s:
        return False, "Empty query"
    if ";" in s:
        return False, "Only a single SQL statement is allowed"
    if not re.match(r"^(select|with)\b", s, re.I):
        return False, "Only SELECT queries are allowed"
    if FORBIDDEN.search(s):
        return False, "Query contains a forbidden keyword (database is read-only)"
    return True, "ok"


def run_sql(sql: str, limit: int = 200):
    """Executes on a READ-ONLY connection, so even a bad query cannot change data."""
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        cur = con.execute(sql.strip().rstrip(";"))
        cols = [c[0] for c in cur.description] if cur.description else []
        return cols, cur.fetchmany(limit)
    finally:
        con.close()


# ---------------------------------------------------------------- pipeline
def answer_question(question: str, validate: bool = True, max_repairs: int = 2,
                    summarize: bool = False) -> dict:
    """validate=True  -> validation layer + execution-guided self-repair.
    validate=False -> plain LLM -> SQL -> run (baseline for your evaluation)."""
    schema = get_schema()
    result = {"question": question, "sql": None, "columns": [], "rows": [],
              "error": None, "attempts": 0, "repair_log": [], "summary": None}

    sql = extract_sql(ask_llm(build_prompt(question, schema)))
    attempts = 0
    while True:
        attempts += 1
        result["sql"] = sql
        err = None
        if validate:
            ok, msg = validate_sql(sql)
            if not ok:
                err = msg
        if err is None:
            try:
                cols, rows = run_sql(sql)
                result.update(columns=cols, rows=rows, error=None)
                break
            except (sqlite3.Error, sqlite3.Warning) as e:
                err = str(e)
        result["error"] = err
        result["repair_log"].append({"sql": sql, "error": err})
        if not validate or attempts > max_repairs:
            break
        sql = extract_sql(ask_llm(build_prompt(question, schema, sql, err)))

    result["attempts"] = attempts

    if summarize and result["error"] is None and result["rows"]:
        preview = [list(r) for r in result["rows"][:20]]
        result["summary"] = ask_llm(
            f"Question: {question}\nSQL result columns: {result['columns']}\nRows: {preview}\n"
            "Answer the question in 1-2 short sentences, in the SAME language as the question."
        ).strip()
    return result


# ---------------------------------------------------------------- multilingual advisory
def district_facts(district: str):
    """Collects the database facts about one district (also shown in the app)."""
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    risk = con.execute(
        """SELECT d.population, d.coastal, r.flood_score, r.heat_score, r.history_score,
                  r.overall_risk, r.risk_category
           FROM districts d JOIN district_risk r USING(district_id) WHERE d.name = ?""",
        (district,)).fetchone()
    events = con.execute(
        """SELECT event_type, COUNT(*), SUM(people_affected), SUM(deaths)
           FROM disaster_events e JOIN districts d USING(district_id)
           WHERE d.name = ? GROUP BY event_type""", (district,)).fetchall()
    shelters = con.execute(
        """SELECT COUNT(*), COALESCE(SUM(capacity), 0), COALESCE(SUM(flood_safe), 0)
           FROM relief_shelters s JOIN districts d USING(district_id) WHERE d.name = ?""",
        (district,)).fetchone()
    beds = con.execute(
        """SELECT COALESCE(SUM(beds), 0) FROM hospitals h JOIN districts d USING(district_id)
           WHERE d.name = ?""", (district,)).fetchone()[0]
    con.close()
    return risk, events, shelters, beds


def generate_advisory(district: str, language: str = "English") -> str:
    """Asks the LLM to turn the district's database facts into a short public advisory."""
    risk, events, shelters, beds = district_facts(district)
    if risk is None:
        raise ValueError(f"No risk data for {district}")
    pop, coastal, flood, heat, hist, overall, cat = risk
    ev_text = "; ".join(f"{t}: {n} events, {a} people affected, {d} deaths" for t, n, a, d in events) or "none"
    facts = (
        f"District: {district} (population {pop:,}, {'coastal' if coastal else 'inland'})\n"
        f"Risk scores out of 100: flood {flood}, heat {heat}, past-impact {hist}; "
        f"overall {overall} ({cat} relative risk among the 12 districts)\n"
        f"Past events in the database: {ev_text}\n"
        f"Relief shelters: {shelters[0]} shelters, total capacity {shelters[1]:,}, "
        f"{shelters[2]} of them on safe ground\n"
        f"Hospital beds: {beds:,}"
    )
    prompt = f"""You write short public disaster-preparedness advisories for residents.
Write the advisory in {language}.

Use ONLY the facts below. Do not invent numbers, dates or phone numbers.
Format: one headline line, then 4 short bullet points (what the main risk is, who is most at
risk, what to prepare, where help is available). Keep it simple and calm.

FACTS:
{facts}
"""
    return ask_llm(prompt).strip()
