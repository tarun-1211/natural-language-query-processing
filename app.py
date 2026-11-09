import sqlite3

import pandas as pd
import streamlit as st

from core import DB_PATH, answer_question, district_facts, generate_advisory, get_schema

st.set_page_config(page_title="Disaster Risk Assistant", page_icon="🌧️", layout="wide")


@st.cache_data
def query_df(sql: str, params: tuple = ()) -> pd.DataFrame:
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        return pd.read_sql(sql, con, params=params)
    finally:
        con.close()


st.title("🌧️ Disaster Risk Assistant")
st.caption("Climate data → flood and heat risk → answers in English, Tamil or Hindi. "
           "12 districts of Tamil Nadu, 2019 to 2025.")

meta = dict(query_df("SELECT key, value FROM pipeline_meta").values)
if meta.get("climate_is_real") == "no":
    st.warning("See the 'Data pipeline' tab.")

with st.sidebar:
    st.header("Settings")
    validate = st.toggle("Validation + self-repair", value=True,
                         help="OFF = plain LLM→SQL baseline, useful for comparing in your report")
    show_sql = st.toggle("Show generated SQL", value=True)
    with st.expander("Database schema"):
        st.code(get_schema(), language="sql")

tab_ask, tab_risk, tab_pipe = st.tabs(["💬 Ask a question", "📊 Risk dashboard", "🔬 Data pipeline"])

# ------------------------------------------------------------------ tab 1: text-to-SQL
with tab_ask:
    EXAMPLES = [
        "Which district has the highest overall disaster risk?",
        "How many flood events were recorded in Thanjavur?",
        "What was the highest single-day rainfall recorded in Chennai?",
        "How many hot days did Vellore have in 2024?",
        "How many relief shelters are there in Chennai?",
        "சென்னையில் எத்தனை நிவாரண முகாம்கள் உள்ளன?",
        "सबसे ज़्यादा बाढ़ की घटनाएँ किस जिले में हुईं?",
        "Which districts are in the High risk category?",
    ]
    example = st.selectbox("Try an example", [""] + EXAMPLES)
    question = st.text_input("Your question", value=example)

    if st.button("Ask", type="primary") and question.strip():
        with st.spinner("Thinking..."):
            try:
                res = answer_question(question, validate=validate, summarize=True)
            except Exception as e:
                st.error(f"Error: {e}")
                st.stop()

        if res["error"]:
            st.error(f"Could not answer: {res['error']}")
        else:
            if res["summary"]:
                st.success(res["summary"])
            if res["rows"]:
                st.dataframe(pd.DataFrame(res["rows"], columns=res["columns"]))
            else:
                st.info("The query ran but returned no rows.")

        if show_sql:
            st.subheader("Generated SQL")
            st.code(res["sql"], language="sql")
        st.caption(f"Attempts: {res['attempts']}  |  Validation + repair: {'ON' if validate else 'OFF'}")
        if res["repair_log"]:
            with st.expander("Self-repair log (failed attempts)"):
                for item in res["repair_log"]:
                    st.code(item["sql"], language="sql")
                    st.write(f"❌ {item['error']}")

# ------------------------------------------------------------------ tab 2: risk dashboard
with tab_risk:
    risk = query_df("""SELECT d.name, d.latitude, d.longitude, d.population, r.flood_score, r.heat_score,
                              r.history_score, r.overall_risk, r.risk_category
                       FROM district_risk r JOIN districts d USING(district_id)
                       ORDER BY r.overall_risk DESC""")
    c1, c2 = st.columns([3, 2])
    with c1:
        st.subheader("Overall risk by district (0 to 100)")
        st.bar_chart(risk.set_index("name")["overall_risk"])
    with c2:
        st.subheader("Map (bigger = higher risk)")
        map_df = risk.assign(size=risk["overall_risk"] * 600 + 4000)
        st.map(map_df, latitude="latitude", longitude="longitude", size="size")

    st.dataframe(risk[["name", "flood_score", "heat_score", "history_score", "overall_risk", "risk_category"]], hide_index=True)
    st.caption("Overall = 40% flood + 30% heat + 30% past impact. Category is relative: top, middle and "
               "bottom third of the 12 districts.")

    st.divider()
    district = st.selectbox("Look at one district", risk["name"].tolist())
    monthly = query_df("""SELECT year, month, total_rain_mm, max_daily_rain_mm, heavy_rain_days, mean_tmax_c, hot_days
                          FROM climate_monthly m JOIN districts d USING(district_id)
                          WHERE d.name = ? ORDER BY year, month""", (district,))
    monthly["period"] = monthly["year"].astype(str) + "-" + monthly["month"].astype(str).str.zfill(2)
    a, b = st.columns(2)
    with a:
        st.markdown("**Monthly rainfall (mm)**")
        st.bar_chart(monthly.set_index("period")["total_rain_mm"])
    with b:
        st.markdown("**Average maximum temperature (°C)**")
        st.line_chart(monthly.set_index("period")["mean_tmax_c"])

    events = query_df("""SELECT e.event_type, e.year, e.month, e.people_affected, e.deaths, e.damage_crore_inr
                         FROM disaster_events e JOIN districts d USING(district_id)
                         WHERE d.name = ? ORDER BY e.year, e.month""", (district,))
    st.markdown("**Recorded events (synthetic)**")
    st.dataframe(events, hide_index=True)

    st.subheader("Public advisory")
    lang = st.selectbox("Language", ["English", "Tamil", "Hindi"])
    if st.button("Generate advisory"):
        with st.spinner("Writing advisory..."):
            try:
                st.info(generate_advisory(district, lang))
                st.caption("Generated from the project database only. Always check official sources "
                           "(IMD, state disaster management authority) before acting.")
            except Exception as e:
                st.error(f"Error: {e}")

# ------------------------------------------------------------------ tab 3: pipeline
with tab_pipe:
    st.subheader("Climate data workflow")
    st.markdown(f"""
| Step | What this project does |
|---|---|
| 1. Data acquisition | Daily rainfall and temperature. Source: **{meta.get('climate_source')}**, period {meta.get('period')} |
| 2. Quality control | Removes impossible values, swaps reversed min/max, fills short temperature gaps (table below) |
| 3. Pre-processing | Daily table summarised into a monthly table (rain totals, heavy-rain days, hot days) |
| 4. Analysis | Flood, heat and past-impact scores combined into a district risk index |
| 5. Visualisation | The risk dashboard tab |
| 6. Validation | Hold-out test of the gap-filling method: RMSE, bias, correlation (table below) |
""")
    st.markdown("**Quality-control log**")
    st.dataframe(query_df("SELECT step, rows_affected, action FROM qc_log"), hide_index=True)
    st.markdown("**Validation of temperature gap filling** (hidden values re-estimated and compared with the truth)")
    st.dataframe(query_df("SELECT variable, gap_days, n_points, rmse AS rmse_celsius, bias AS bias_celsius, correlation "
                          "FROM validation_metrics"), hide_index=True)
    st.markdown("**Rows flagged by quality control**")
    st.dataframe(query_df("SELECT qc_flag, COUNT(*) AS days FROM climate_daily GROUP BY qc_flag ORDER BY days DESC"), hide_index=True)
    st.caption(meta.get("events_note", ""))
