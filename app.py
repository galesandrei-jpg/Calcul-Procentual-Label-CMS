import datetime as dt
from typing import Dict, List, Tuple

import streamlit as st

from src.youtube import (
    YoutubeConfig,
    build_yta_service,
    list_groups,
    query_monthly_estimated_revenue,
)
from src.sheets import (
    SheetConfig,
    open_sheet,
    build_month_row_index,
    find_header_columns,
    batch_write_values,
    ensure_month_rows,
)

st.set_page_config(page_title="YouTube CMS Revenue → Google Sheets", layout="wide")


# -----------------------------
# Helpers
# -----------------------------
def yyyymm_first_day(yyyymm: str) -> str:
    """Return the first day of a month as YYYY-MM-DD."""
    y, m = map(int, yyyymm.split("-"))
    return dt.date(y, m, 1).isoformat()


def months_between(start_yyyymm: str, end_yyyymm: str) -> List[str]:
    """Inclusive list of YYYY-MM between start and end."""
    sy, sm = map(int, start_yyyymm.split("-"))
    ey, em = map(int, end_yyyymm.split("-"))

    out = []
    y, m = sy, sm
    while (y < ey) or (y == ey and m <= em):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            m = 1
            y += 1
    return out


def month_range_min_max_for_month_dimension(selected: List[str]) -> Tuple[str, str]:
    """
    For YouTube Analytics reports.query with dimensions=month:
    - startDate must be first day of a month
    - endDate must ALSO be first day of a month
    API returns monthly rows for each month between start and end (inclusive).
    """
    selected_sorted = sorted(selected)
    startDate = yyyymm_first_day(selected_sorted[0])
    endDate = yyyymm_first_day(selected_sorted[-1])
    return startDate, endDate


# -----------------------------
# UI
# -----------------------------
st.title("YouTube CMS Revenue → Google Sheets")
st.caption(
    "Pulls monthly **estimatedRevenue** for 3 CMS groups (Total + US) and writes into a Google Sheet. "
    "Supports any year/month range and can auto-create missing month rows chronologically."
)

with st.expander("1) Configuration (from secrets)", expanded=True):
    st.write("This app reads defaults from **Streamlit secrets**. You can override below.")

    # YouTube / OAuth
    default_owner = st.secrets.get("youtube", {}).get("content_owner", "")
    default_on_behalf = st.secrets.get("youtube", {}).get("on_behalf_of_content_owner", "")

    col1, col2 = st.columns(2)
    with col1:
        content_owner = st.text_input(
            "YouTube CMS Content Owner ID (from /owner/<ID>)",
            value=default_owner,
            help="Used in ids=contentOwner==<ID>",
        )
    with col2:
        on_behalf = st.text_input(
            "onBehalfOfContentOwner (kept for config)",
            value=default_on_behalf,
            help="Current YouTube client build does NOT pass onBehalf* into reports.query().",
        )

    # Google Sheet
    default_sheet_id = st.secrets.get("sheets", {}).get("sheet_id", "")
    default_worksheet = st.secrets.get("sheets", {}).get(
        "worksheet_name", "Calcul Procentual Venituri Luna"
    )
    sheet_id = st.text_input("Google Sheet ID", value=default_sheet_id)
    worksheet_name = st.text_input("Worksheet name", value=default_worksheet)

    # Group mapping
    st.subheader("Group config")
    st.write("Paste 3 CMS group IDs from URLs like: studio.youtube.com/group/<GROUP_ID>/analytics")

    default_groups = st.secrets.get("groups", {})
    g1_name = st.text_input(
        "Group 1 name (Sheet header)", value=default_groups.get("group1_name", "HaHaHa Channels")
    )
    g1_id = st.text_input("Group 1 ID (YouTube group id)", value=default_groups.get("group1_id", ""))

    g2_name = st.text_input(
        "Group 2 name (Sheet header)", value=default_groups.get("group2_name", "HaHaHa Content ID")
    )
    g2_id = st.text_input("Group 2 ID", value=default_groups.get("group2_id", ""))

    g3_name = st.text_input(
        "Group 3 name (Sheet header)", value=default_groups.get("group3_name", "HaHaha Art Tracks")
    )
    g3_id = st.text_input("Group 3 ID", value=default_groups.get("group3_id", ""))

    # Optional discovery (not required)
    use_discovery = st.checkbox("Load groups from YouTube (discovery)", value=False)
    if use_discovery:
        if st.button("Load groups"):
            try:
                cfg = YoutubeConfig(
                    content_owner=content_owner.strip(),
                    on_behalf_of_content_owner=on_behalf.strip() or None,
                )
                yta = build_yta_service(cfg)
                groups = list_groups(yta, cfg)
                st.session_state["groups_list"] = groups
                st.success(f"Loaded {len(groups)} groups.")
            except Exception as e:
                st.error(f"Failed to load groups: {e}")

        groups = st.session_state.get("groups_list", [])
        if groups:
            name_to_id = {g["title"]: g["id"] for g in groups}
            titles = sorted(name_to_id.keys())

            st.write("Pick three groups (these set the ID fields above):")
            c1, c2, c3 = st.columns(3)
            with c1:
                pick1 = st.selectbox("Pick Group 1", options=[""] + titles, index=0)
            with c2:
                pick2 = st.selectbox("Pick Group 2", options=[""] + titles, index=0)
            with c3:
                pick3 = st.selectbox("Pick Group 3", options=[""] + titles, index=0)

            if st.button("Apply selected group IDs"):
                if pick1:
                    st.session_state["g1_id"] = name_to_id[pick1]
                if pick2:
                    st.session_state["g2_id"] = name_to_id[pick2]
                if pick3:
                    st.session_state["g3_id"] = name_to_id[pick3]
                st.success("Applied. Scroll up and copy the IDs into the text fields if needed.")

    if "g1_id" in st.session_state and not g1_id:
        g1_id = st.session_state["g1_id"]
    if "g2_id" in st.session_state and not g2_id:
        g2_id = st.session_state["g2_id"]
    if "g3_id" in st.session_state and not g3_id:
        g3_id = st.session_state["g3_id"]


with st.expander("2) Select months (any year) + auto-create missing rows", expanded=True):
    auto_create = st.checkbox(
        "Auto-create missing month rows in the Sheet (chronological insertion)",
        value=True,
        help="Creates missing months by inserting new rows in the correct chronological position based on column A.",
    )

    now = dt.date.today()
    years = list(range(2010, now.year + 11))  # extend as needed

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        start_year = st.selectbox("Start year", years, index=years.index(now.year))
    with c2:
        start_month = st.selectbox("Start month", list(range(1, 13)), index=now.month - 1)
    with c3:
        end_year = st.selectbox("End year", years, index=years.index(now.year))
    with c4:
        end_month = st.selectbox("End month", list(range(1, 13)), index=now.month - 1)

    start_yyyymm = f"{start_year:04d}-{start_month:02d}"
    end_yyyymm = f"{end_year:04d}-{end_month:02d}"

    if (end_year, end_month) < (start_year, start_month):
        st.error("End month must be after (or equal to) start month.")
        selected_months = []
    else:
        selected_months = months_between(start_yyyymm, end_yyyymm)
        st.write(f"Selected **{len(selected_months)}** months: {selected_months[0]} → {selected_months[-1]}")

    # Optional: show sheet month coverage
    if sheet_id.strip() and worksheet_name.strip():
        try:
            sheet_cfg = SheetConfig(sheet_id=sheet_id.strip(), worksheet_name=worksheet_name.strip())
            ws_preview = open_sheet(sheet_cfg)
            month_map = build_month_row_index(ws_preview)
            st.caption(f"Sheet currently has **{len(month_map)}** month rows detected in column A.")
        except Exception:
            pass


with st.expander("3) Run", expanded=True):
    run = st.button("Fetch revenue and write to Google Sheet", type="primary")

    if run:
        missing = []
        if not content_owner.strip():
            missing.append("content_owner")
        if not sheet_id.strip():
            missing.append("sheet_id")
        if not worksheet_name.strip():
            missing.append("worksheet_name")
        if not (g1_id.strip() and g2_id.strip() and g3_id.strip()):
            missing.append("3 group IDs")
        if not selected_months:
            missing.append("selected months (start/end)")

        if missing:
            st.error("Missing: " + ", ".join(missing))
            st.stop()

        try:
            # Build services
            ycfg = YoutubeConfig(
                content_owner=content_owner.strip(),
                on_behalf_of_content_owner=on_behalf.strip() or None,
            )
            yta = build_yta_service(ycfg)

            sheet_cfg = SheetConfig(sheet_id=sheet_id.strip(), worksheet_name=worksheet_name.strip())
            ws = open_sheet(sheet_cfg)

            # Headers -> column indices
            headers = ws.row_values(1)
            header_to_col = find_header_columns(headers)

            groups = [
                (g1_name.strip(), g1_id.strip()),
                (g2_name.strip(), g2_id.strip()),
                (g3_name.strip(), g3_id.strip()),
            ]

            needed_headers = []
            for name, _ in groups:
                needed_headers.append(name)
                needed_headers.append(f"{name} US")

            missing_headers = [h for h in needed_headers if h not in header_to_col]
            if missing_headers:
                st.error("These headers are missing from row 1 in the sheet: " + ", ".join(missing_headers))
                st.stop()

            status = st.empty()

            # Ensure month rows exist (optional)
            if auto_create:
                status.info("Ensuring month rows exist in the sheet (auto-create enabled)…")
                month_to_row = ensure_month_rows(ws, selected_months)
            else:
                month_to_row = build_month_row_index(ws)

            # Month dimension requires start/end to be first day of month
            startDate, endDate = month_range_min_max_for_month_dimension(selected_months)

            results_total: Dict[str, Dict[str, float]] = {}
            results_us: Dict[str, Dict[str, float]] = {}

            progress = st.progress(0.0)
            steps = len(groups) * 2
            done = 0

            for group_name, group_id in groups:
                status.info(f"Querying TOTAL revenue for {group_name} …")
                total_map = query_monthly_estimated_revenue(
                    yta,
                    ycfg,
                    startDate=startDate,
                    endDate=endDate,
                    group_id=group_id,
                    country=None,
                )
                results_total[group_name] = total_map
                done += 1
                progress.progress(done / steps)

                status.info(f"Querying US-only revenue for {group_name} …")
                us_map = query_monthly_estimated_revenue(
                    yta,
                    ycfg,
                    startDate=startDate,
                    endDate=endDate,
                    group_id=group_id,
                    country="US",
                )
                results_us[group_name] = us_map
                done += 1
                progress.progress(done / steps)

            # Build updates list
            status.info("Preparing sheet updates…")
            updates = []

            missing_rows = [m for m in selected_months if m not in month_to_row]
            if missing_rows:
                st.warning(
                    "Some selected months still have no rows in the sheet (won't write): "
                    + ", ".join(missing_rows[:24])
                    + (" …" if len(missing_rows) > 24 else "")
                )

            for yyyymm in selected_months:
                row = month_to_row.get(yyyymm)
                if not row:
                    continue

                for group_name, _ in groups:
                    col_total = header_to_col[group_name]
                    value_total = results_total.get(group_name, {}).get(yyyymm, 0.0)
                    updates.append((row, col_total, value_total))

                    col_us = header_to_col[f"{group_name} US"]
                    value_us = results_us.get(group_name, {}).get(yyyymm, 0.0)
                    updates.append((row, col_us, value_us))

            status.info("Writing values into Google Sheet…")
            batch_write_values(ws, updates)
            status.success("Done ✅ Sheet updated.")
        except Exception as e:
            st.exception(e)
