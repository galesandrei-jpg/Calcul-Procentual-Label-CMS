import datetime as dt
from typing import Dict, List, Tuple

import pandas as pd
import streamlit as st

from src.youtube import (
    YoutubeConfig,
    build_yta_service,
    list_groups,
    query_monthly_estimated_revenue,
    query_monthly_total_cms_revenue,
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
# Constants: CMS Deals groups
# -----------------------------
CMS_DEALS_GROUPS = [
    ("Unicorn", "WBbE5H4__OU"),
    ("BG Music", "aZ9OPuMpAD8"),
    ("Melosy", "Lx3MFo26GtM"),
    ("Huta Media", "SWO-QxXWTgU"),
    ("Magic", "Qv2RHPCUZOU"),
    ("Pine", "BXowAx3iy4k"),
    ("Malwin", "JGSzjsvdIDk"),
]

# Sheet header names for the new columns
COL_K_HEADER = "Total CMS Deals"
COL_L_HEADER = "CMS Deals (US tax)"
COL_M_HEADER = "CMS Deals Net"
COL_N_HEADER = "Total Distr"
COL_O_HEADER = "Distr (US tax)"
COL_P_HEADER = "Distr Net"
COL_Q_HEADER = "Total General (€)"

# Existing sheet columns referenced in formulas (fixed positions)
# H = "Total Label", I = "Label US (tax)", S = "General US Tax"


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


def col_index_to_letter(col_idx: int) -> str:
    """Convert a 1-based column index to a spreadsheet column letter (1='A', 2='B', ..., 27='AA')."""
    result = ""
    while col_idx > 0:
        col_idx, remainder = divmod(col_idx - 1, 26)
        result = chr(65 + remainder) + result
    return result


# -----------------------------
# UI
# -----------------------------
st.title("YouTube CMS Revenue → Google Sheets")
st.caption(
    "Pulls monthly **estimatedRevenue** for 3 CMS groups (Total + US), "
    "aggregates 7 CMS Deals groups, fetches total CMS revenue, computes formulas, "
    "and writes into a Google Sheet. "
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
    st.subheader("Group config (original 3 groups)")
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

    # CMS Deals groups (display only, hardcoded)
    st.subheader("CMS Deals groups (7 groups, aggregated into columns K & L)")
    for name, gid in CMS_DEALS_GROUPS:
        st.text(f"  • {name}: {gid}")

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

            # Original 3 groups
            groups = [
                (g1_name.strip(), g1_id.strip()),
                (g2_name.strip(), g2_id.strip()),
                (g3_name.strip(), g3_id.strip()),
            ]

            # Validate original group headers
            needed_headers = []
            for name, _ in groups:
                needed_headers.append(name)
                needed_headers.append(f"{name} US")

            # Validate new column headers (including Q)
            new_col_headers = [
                COL_K_HEADER, COL_L_HEADER, COL_M_HEADER,
                COL_N_HEADER, COL_O_HEADER, COL_P_HEADER,
                COL_Q_HEADER,
            ]
            all_needed = needed_headers + new_col_headers
            missing_headers = [h for h in all_needed if h not in header_to_col]
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

            # -------------------------------------------------------
            # Fetch revenue for original 3 groups (Total + US)
            # -------------------------------------------------------
            results_total: Dict[str, Dict[str, float]] = {}
            results_us: Dict[str, Dict[str, float]] = {}

            # Total steps: 3 original groups × 2 + 7 CMS deals groups × 2 + 1 total CMS = 21
            total_steps = len(groups) * 2 + len(CMS_DEALS_GROUPS) * 2 + 1
            progress = st.progress(0.0)
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
                progress.progress(done / total_steps)

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
                progress.progress(done / total_steps)

            # -------------------------------------------------------
            # Fetch revenue for 7 CMS Deals groups (Total + US)
            # -------------------------------------------------------
            cms_deals_total: Dict[str, Dict[str, float]] = {}  # group_name -> {yyyymm: revenue}
            cms_deals_us: Dict[str, Dict[str, float]] = {}

            for deal_name, deal_id in CMS_DEALS_GROUPS:
                status.info(f"Querying TOTAL revenue for CMS Deal: {deal_name} …")
                total_map = query_monthly_estimated_revenue(
                    yta,
                    ycfg,
                    startDate=startDate,
                    endDate=endDate,
                    group_id=deal_id,
                    country=None,
                )
                cms_deals_total[deal_name] = total_map
                done += 1
                progress.progress(done / total_steps)

                status.info(f"Querying US-only revenue for CMS Deal: {deal_name} …")
                us_map = query_monthly_estimated_revenue(
                    yta,
                    ycfg,
                    startDate=startDate,
                    endDate=endDate,
                    group_id=deal_id,
                    country="US",
                )
                cms_deals_us[deal_name] = us_map
                done += 1
                progress.progress(done / total_steps)

            # -------------------------------------------------------
            # Fetch total CMS revenue (no group filter) for column Q
            # -------------------------------------------------------
            status.info("Querying TOTAL CMS revenue (entire content owner) …")
            total_cms_per_month = query_monthly_total_cms_revenue(
                yta,
                ycfg,
                startDate=startDate,
                endDate=endDate,
            )
            done += 1
            progress.progress(done / total_steps)

            # -------------------------------------------------------
            # Aggregate CMS Deals: sum all 7 groups per month
            # -------------------------------------------------------
            # K values: sum of all 7 groups' total revenue per month
            agg_total_per_month: Dict[str, float] = {}
            # L values: sum of all 7 groups' US revenue × 0.1 per month
            agg_us_tax_per_month: Dict[str, float] = {}

            for yyyymm in selected_months:
                total_sum = 0.0
                us_sum = 0.0
                for deal_name, _ in CMS_DEALS_GROUPS:
                    total_sum += cms_deals_total.get(deal_name, {}).get(yyyymm, 0.0)
                    us_sum += cms_deals_us.get(deal_name, {}).get(yyyymm, 0.0)
                agg_total_per_month[yyyymm] = total_sum
                agg_us_tax_per_month[yyyymm] = us_sum * 0.1

            # -------------------------------------------------------
            # Build updates list
            # -------------------------------------------------------
            status.info("Preparing sheet updates…")
            updates = []

            missing_rows = [m for m in selected_months if m not in month_to_row]
            if missing_rows:
                st.warning(
                    "Some selected months still have no rows in the sheet (won't write): "
                    + ", ".join(missing_rows[:24])
                    + (" …" if len(missing_rows) > 24 else "")
                )

            # Get column indices for K-Q (looked up by header)
            col_k_idx = header_to_col[COL_K_HEADER]
            col_l_idx = header_to_col[COL_L_HEADER]
            col_m_idx = header_to_col[COL_M_HEADER]
            col_n_idx = header_to_col[COL_N_HEADER]
            col_o_idx = header_to_col[COL_O_HEADER]
            col_p_idx = header_to_col[COL_P_HEADER]
            col_q_idx = header_to_col[COL_Q_HEADER]

            # Column letters for K-P (derived from header lookup)
            col_k_letter = col_index_to_letter(col_k_idx)
            col_l_letter = col_index_to_letter(col_l_idx)
            col_n_letter = col_index_to_letter(col_n_idx)
            col_o_letter = col_index_to_letter(col_o_idx)
            col_q_letter = col_index_to_letter(col_q_idx)

            # Fixed column letters for existing sheet columns used in formulas
            col_h_letter = "H"  # Total Label
            col_i_letter = "I"  # Label US (tax)
            col_s_letter = "S"  # General US Tax

            for yyyymm in selected_months:
                row = month_to_row.get(yyyymm)
                if not row:
                    continue

                # --- Original 3 groups (same as before) ---
                for group_name, _ in groups:
                    col_total = header_to_col[group_name]
                    value_total = results_total.get(group_name, {}).get(yyyymm, 0.0)
                    updates.append((row, col_total, value_total))

                    col_us = header_to_col[f"{group_name} US"]
                    value_us = results_us.get(group_name, {}).get(yyyymm, 0.0)
                    updates.append((row, col_us, value_us))

                # --- Column K: Total CMS Deals (computed value) ---
                updates.append((row, col_k_idx, agg_total_per_month.get(yyyymm, 0.0)))

                # --- Column L: CMS Deals (US tax) (computed value) ---
                updates.append((row, col_l_idx, agg_us_tax_per_month.get(yyyymm, 0.0)))

                # --- Column M: =K-L (formula) ---
                formula_m = f"={col_k_letter}{row}-{col_l_letter}{row}"
                updates.append((row, col_m_idx, formula_m))

                # --- Column N: =Q-(K+H) (formula) ---
                formula_n = f"={col_q_letter}{row}-({col_k_letter}{row}+{col_h_letter}{row})"
                updates.append((row, col_n_idx, formula_n))

                # --- Column O: =S-(L+I) (formula) ---
                formula_o = f"={col_s_letter}{row}-({col_l_letter}{row}+{col_i_letter}{row})"
                updates.append((row, col_o_idx, formula_o))

                # --- Column P: =N-O (formula) ---
                formula_p = f"={col_n_letter}{row}-{col_o_letter}{row}"
                updates.append((row, col_p_idx, formula_p))

                # --- Column Q: Total CMS revenue (fetched value) ---
                updates.append((row, col_q_idx, total_cms_per_month.get(yyyymm, 0.0)))

            status.info("Writing values and formulas into Google Sheet…")
            batch_write_values(ws, updates)
            status.success("Done ✅ Sheet updated with revenue data + formulas.")

            # Show summary
            st.subheader("Revenue Summary")
            summary_data = []
            for yyyymm in selected_months:
                if yyyymm in month_to_row:
                    summary_data.append({
                        "Month": yyyymm,
                        "Total CMS Revenue (Q)": f"${total_cms_per_month.get(yyyymm, 0.0):,.2f}",
                        "Total CMS Deals (K)": f"${agg_total_per_month.get(yyyymm, 0.0):,.2f}",
                        "CMS Deals US Tax (L)": f"${agg_us_tax_per_month.get(yyyymm, 0.0):,.2f}",
                    })
            if summary_data:
                st.dataframe(pd.DataFrame(summary_data), use_container_width=True)

        except Exception as e:
            st.exception(e)
