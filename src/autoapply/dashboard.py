"""Streamlit dashboard: run with `autoapply dashboard`."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autoapply.config import load_settings  # noqa: E402
from autoapply.db import STATUSES, Database  # noqa: E402

st.set_page_config(page_title="autoapply", page_icon="📨", layout="wide")
st.title("autoapply — applications")

settings = load_settings()
db = Database(settings.database_path)
records = db.query()
df = pd.DataFrame([r.to_dict() for r in records])

counts = db.counts_by_status()
cols = st.columns(len(STATUSES) + 1)
cols[0].metric("Total", sum(counts.values()))
for i, s in enumerate(STATUSES, start=1):
    cols[i].metric(s, counts.get(s, 0))

if df.empty:
    st.info("No applications recorded yet. Run `autoapply run` first.")
    st.stop()

with st.sidebar:
    st.header("Filters")
    status_sel = st.multiselect("Status", STATUSES, default=[])
    company_sel = st.text_input("Company contains")
    search = st.text_input("Search role / location")
    dates = pd.to_datetime(df["timestamp"])
    dmin, dmax = dates.min().date(), dates.max().date()
    date_range = st.date_input("Date range", (dmin, dmax))
    ats_sel = st.multiselect("ATS", sorted(df["ats"].dropna().unique().tolist()))

f = df.copy()
if status_sel:
    f = f[f["status"].isin(status_sel)]
if company_sel:
    f = f[f["company"].str.contains(company_sel, case=False, na=False)]
if search:
    m = f["role"].str.contains(search, case=False, na=False) | f["location"].str.contains(search, case=False, na=False)
    f = f[m]
if ats_sel:
    f = f[f["ats"].isin(ats_sel)]
if isinstance(date_range, tuple) and len(date_range) == 2:
    ts = pd.to_datetime(f["timestamp"]).dt.date
    f = f[(ts >= date_range[0]) & (ts <= date_range[1])]

st.subheader(f"{len(f)} application(s)")
show = f[["timestamp", "company", "role", "location", "ats", "status", "url", "error"]].copy()
show["timestamp"] = show["timestamp"].str.replace("T", " ")
st.dataframe(
    show,
    use_container_width=True,
    hide_index=True,
    column_config={"url": st.column_config.LinkColumn("Job link", display_text="open")},
)

st.subheader("Details")
options = {f"{r['timestamp'][:16]} · {r['company']} · {r['role']} · {r['status']}": i for i, r in f.iterrows()}
if options:
    pick = st.selectbox("Select an application", list(options))
    row = f.loc[options[pick]]
    left, right = st.columns([1, 1])
    with left:
        st.markdown(f"**{row['company']}** — {row['role']}  \n{row['location']}  \n[{row['url']}]({row['url']})")
        st.markdown(f"Status: `{row['status']}` · ATS: `{row['ats']}` · run `{row['run_id']}` · mode `{row['mode']}`")
        if row["error"]:
            st.warning(row["error"])
        uq = row["unanswered_questions"]
        if isinstance(uq, str):
            uq = json.loads(uq or "[]")
        if uq:
            st.markdown("**Unanswered questions:**")
            for q in uq:
                st.markdown(f"- {q}")
        ans = row["answers"]
        if isinstance(ans, str):
            ans = json.loads(ans or "[]")
        if ans:
            st.markdown("**Answers filled:**")
            st.dataframe(pd.DataFrame(ans), use_container_width=True, hide_index=True)
    with right:
        sp = row["screenshot_path"]
        if sp and Path(sp).exists():
            st.image(sp, caption=Path(sp).name, use_container_width=True)
        else:
            st.caption("No screenshot for this record.")

with st.expander("Runs"):
    st.dataframe(pd.DataFrame(db.runs()), use_container_width=True, hide_index=True)

st.download_button("Download CSV", f.to_csv(index=False).encode("utf-8"), "applications.csv", "text/csv")
