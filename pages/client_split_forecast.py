"""New, optional Streamlit page: monthly client splits and forecasts.

No changes to existing app.py or pages/cms_deal_report.py are required.
The only write target is a new worksheet named 'Client Split Forecast'.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import io
import json
from dataclasses import asdict
from typing import Any, Dict, List, Mapping

import pandas as pd
import streamlit as st

from src.client_split import (
    CATEGORIES,
    MAIN_WORKSHEET,
    TARGET_WORKSHEET,
    ClientSplitError,
    build_rate_catalog,
    build_cms_rate_history,
    build_cms_rate_history_from_xlsx,
    resolve_cms_monthly_rate,
    parse_private_configuration,
    build_report,
    config_from_cells,
    default_rule_rows,
    find_channel_overlaps,
    month_sequence,
    publish_report,
    read_rules,
    read_sheet_configuration,
    reconcile_to_main_sheet,
    query_monthly_revenue_for_source,
    source_channel_ids,
    resolve_rates,
    sheet_id_from_input,
    summarize_by_client,
)
from src.youtube import (
    YoutubeConfig,
    build_yta_service,
    list_groups,
    list_group_items,
)

st.set_page_config(page_title="Client Split – CMS Deals & Distributie", layout="wide")
st.title("Client Split — CMS Deals & Distributie")

# A public source repository must not imply anonymous access to financial data.
# This gate applies only to the new page; existing app authentication is unchanged.
page_settings = st.secrets.get("client_split", {}) or {}
access_password = page_settings.get("access_password", "")
if not isinstance(access_password, str) or len(access_password) < 16:
    st.info("Forecast access has not been configured. Contact the app owner.")
    st.stop()
provided_password = st.text_input("Forecast access code", type="password", key="cs_access_password")
if not hmac.compare_digest(provided_password.encode(), access_password.encode()):
    st.info("Enter your forecast access code to continue.")
    st.stop()

st.caption(
    "Breakdown pe client: brut, retinere US, procent HaHaHa, suma de platit clientului "
    "si net HaHaHa, lunar, in EUR. Separate page; existing reports are not changed."
)
st.info(
    "Preview is read-only. 'Publish' updates only the dedicated 'Client Split Forecast' "
    "tab in the selected spreadsheet; it never changes the existing monthly tab. "
    "Amounts are forecasts based on YouTube estimatedRevenue, not approved invoices."
)

# Session state is used only for the new page, not existing app components.
if "client_split_rows" not in st.session_state:
    st.session_state["client_split_rows"] = default_rule_rows()
if "client_split_editor_rev" not in st.session_state:
    st.session_state["client_split_editor_rev"] = 0
if "client_split_catalog" not in st.session_state:
    st.session_state["client_split_catalog"] = {}
if "client_split_cms_history" not in st.session_state:
    st.session_state["client_split_cms_history"] = {}
if "client_split_preview" not in st.session_state:
    st.session_state["client_split_preview"] = None


def _connect(sheet_id: str):
    # Reuse the deployed app's existing Google service-account authentication.
    # No secrets are stored in the new report or the public repository.
    from src.sheets import _sa_client
    return _sa_client().open_by_key(sheet_id_from_input(sheet_id))


def _rules_signature(rules, months, tax_pct, sheet_id, contract_id,
                     cms_rate_sheet_id, cms_history, content_owner_id, behalf_id):
    data = {"rules": [asdict(r) for r in rules], "months": list(months),
            "tax_pct": tax_pct, "target_id": sheet_id.strip(),
            "contract_id": contract_id.strip(),
            "cms_rate_sheet_id": cms_rate_sheet_id.strip(),
            "cms_rate_history": {client: {month: [asdict(entry) for entry in entries]
                                  for month, entries in sorted(items.items())}
                                  for client, items in sorted(cms_history.items())},
            "content_owner_id": content_owner_id.strip(), "behalf_id": behalf_id.strip()}
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


def _invalid_report(report):
    # Missing API rows and missing rates are both blocked from publishing.
    return any(row["HaHaHa deal %"] is None or row["Payable to client EUR"] is None
               or row["HaHaHa final net EUR"] is None
               or "REVIEW REVENUE" in row["Status"] for row in report)


def _render_report_df(report):
    df = pd.DataFrame(report)
    for col in ("HaHaHa deal %", "Client share %", "Post-intermediation factor"):
        df[col] = df[col].map(lambda v: f"{v * 100:,.2f}%" if pd.notna(v) else "VERIFY RATE")
    return df.round(2)


def _safe_export_csv(df: pd.DataFrame) -> bytes:
    """Prevent spreadsheet-formula execution when a user opens downloaded CSVs."""
    copy = df.copy()
    for column in copy.select_dtypes(include=["object", "string"]).columns:
        copy[column] = copy[column].map(
            lambda v: "'" + v if isinstance(v, str) and
            v.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else v
        )
    return copy.to_csv(index=False).encode("utf-8-sig")


def _client_summary_view(report):
    df = pd.DataFrame(summarize_by_client(report))
    if df.empty:
        return df
    for col in ("HaHaHa deal %", "Client share %"):
        df[col] = df[col].map(lambda v: f"{v * 100:,.2f}%" if isinstance(v, (int, float)) else v)
    return df


secrets = st.secrets.get("client_split", {}) or {}
with st.expander("Private client configuration", expanded=True):
    st.caption("Upload your private configuration here. It is available only in your current app session. "
               "Do not add this file or financial workbooks to the repository.")
    settings_upload = st.file_uploader("Private configuration (.json)", type=["json"], key="cs_private_config")
    settings_bytes = settings_upload.getvalue() if settings_upload is not None else b""
    settings_digest = hashlib.sha256(settings_bytes).hexdigest()
    if settings_digest != st.session_state.get("cs_private_config_digest"):
        try:
            private_settings = parse_private_configuration(settings_bytes) if settings_bytes else {}
        except ClientSplitError as exc:
            st.error(str(exc))
            st.session_state["client_split_preview"] = None
            st.stop()
        st.session_state["cs_private_config_digest"] = settings_digest
        st.session_state["cs_private_settings"] = private_settings
        st.session_state["client_split_rows"] = private_settings.get("rules", default_rule_rows())
        st.session_state["client_split_editor_rev"] += 1
        st.session_state["client_split_cms_history"] = {}
        st.session_state["client_split_catalog"] = {}
        st.session_state["client_split_cms_upload_digest"] = None
        st.session_state["client_split_preview"] = None
    private_settings = st.session_state.get("cs_private_settings", {})
    if private_settings:
        st.success("Private client configuration loaded for this session.")

rate_worksheet = private_settings.get("cms_rates_worksheet", "Monthly rates")
retention_header = private_settings.get("retention_header", "Retention")
partner_aliases = private_settings.get("partner_aliases", {})
terms_worksheet = private_settings.get("contract_terms_worksheet", "Contracts")
assets_worksheet = private_settings.get("contract_assets_worksheet", "Assets")
youtube_secrets = st.secrets.get("youtube", {}) or {}
sheets_secrets = st.secrets.get("sheets", {}) or {}

with st.expander("1. Connection & period", expanded=True):
    left, right = st.columns(2)
    with left:
        owner = st.text_input(
            "YouTube CMS content owner ID",
            value=youtube_secrets.get("content_owner", ""),
            help="Uses the app's existing YouTube OAuth credentials (read-only).",
        )
        on_behalf = st.text_input(
            "onBehalfOfContentOwner (optional)",
            value=youtube_secrets.get("on_behalf_of_content_owner", ""),
        )
        target_id = st.text_input(
            "Existing 'Calcul Procentual Venituri Luna' spreadsheet ID",
            value=sheets_secrets.get("sheet_id", ""),
            help="Paste the existing Google Sheets link or ID. The new tab is created inside this file.",
        )
    with right:
        contract_id = st.text_input(
            "Deal-reference spreadsheet ID (optional)",
            value=secrets.get("contract_sheet_id", ""),
            help="Paste its Google Sheets link or ID. Worksheet names come from your private configuration. "
                 "The service account must have at least Viewer access.",
        )
        cms_rates_id = st.text_input(
            "monthly-rate workbook Google Sheet ID (monthly CMS deal rates)",
            value=secrets.get("cms_rates_sheet_id", ""),
            help="Uses the configured private rate worksheet. Rates are read per month, not hardcoded in GitHub. "
                 "Share the file with the app's service account as Viewer.",
        )
        now = dt.date.today()
        previous = (now.replace(day=1) - dt.timedelta(days=1))
        years = list(range(2022, now.year + 4))
        cols = st.columns(4)
        with cols[0]:
            start_year = st.selectbox("From year", years, index=years.index(previous.year), key="cs_start_year")
        with cols[1]:
            start_month = st.selectbox("From month", list(range(1, 13)),
                                       index=previous.month - 1, key="cs_start_month")
        with cols[2]:
            end_year = st.selectbox("To year", years, index=years.index(previous.year), key="cs_end_year")
        with cols[3]:
            end_month = st.selectbox("To month", list(range(1, 13)),
                                     index=previous.month - 1, key="cs_end_month")
        tax_pct = st.number_input(
            "US withholding rate (%)", min_value=0.0, max_value=100.0,
            value=float(secrets.get("us_withholding_rate", 10.0)), step=0.5,
            help="Forecast assumption only; legacy default is 10% of US revenue. Verify the applicable treatment before use.",
        )

try:
    months = month_sequence(f"{start_year:04d}-{start_month:02d}",
                            f"{end_year:04d}-{end_month:02d}")
except ClientSplitError as exc:
    months = []
    st.error(str(exc))

# A previously loaded catalog must never be applied to a different spreadsheet ID.
if st.session_state.get("client_split_catalog_source") != contract_id.strip():
    st.session_state["client_split_catalog"] = {}
    st.session_state["client_split_catalog_source"] = contract_id.strip()
    st.session_state["client_split_preview"] = None

if st.session_state.get("client_split_cms_history_source") != cms_rates_id.strip():
    st.session_state["client_split_cms_history"] = {}
    st.session_state["client_split_cms_history_source"] = cms_rates_id.strip()
    st.session_state["client_split_cms_upload_digest"] = None
    st.session_state["client_split_preview"] = None

with st.expander("2. Optional reference rates & saved configuration", expanded=False):
    col_a, col_c, col_b = st.columns(3)
    with col_a:
        if st.button("Load contract percentages (read only)", key="cs_load_rates"):
            if not contract_id.strip():
                st.error("Enter the deal-reference spreadsheet ID first.")
            else:
                try:
                    reference = _connect(contract_id)
                    contract_rows = reference.worksheet(terms_worksheet).get(
                        "A1:H150", value_render_option="FORMATTED_VALUE"
                    )
                    details = reference.worksheet(assets_worksheet).get(
                        "A1:E850", value_render_option="FORMATTED_VALUE"
                    )
                    st.session_state["client_split_catalog"] = build_rate_catalog(contract_rows, details)
                    st.success("Contract data loaded. Conflicting rates will not be chosen automatically.")
                except Exception as exc:
                    st.error(f"Cannot read source spreadsheet: {exc}")
    with col_c:
        st.caption("One-time import: upload your private monthly-rate workbook Excel "
                   "file here. No Google Sheets sharing or extra connection is required.")
        cms_excel_upload = st.file_uploader(
            "Use rates from monthly-rate workbook (.xlsx)",
            type=["xlsx"], key="cs_cms_rates_excel",
            help="Reads the configured worksheet into this Streamlit session. "
                 "The file is not saved to GitHub or written to the output worksheet.",
        )
        if cms_excel_upload is not None:
            payload = cms_excel_upload.getvalue()
            upload_digest = hashlib.sha256(payload).hexdigest()
            if upload_digest != st.session_state.get("client_split_cms_upload_digest"):
                try:
                    history = build_cms_rate_history_from_xlsx(payload, rate_worksheet, partner_aliases, retention_header)
                    st.session_state["client_split_cms_history"] = history
                    st.session_state["client_split_cms_upload_digest"] = upload_digest
                    st.session_state["client_split_preview"] = None
                    st.success(f"Loaded dated rates for {len(history)} CMS clients "
                               "from the uploaded file; no sheet connection required.")
                except ClientSplitError as exc:
                    st.session_state["client_split_cms_history"] = {}
                    st.session_state["client_split_cms_upload_digest"] = None
                    st.session_state["client_split_preview"] = None
                    st.error(f"Cannot use uploaded rate workbook: {exc}")
            else:
                st.caption("Using uploaded Excel rates (private session data).")
        elif st.session_state.get("client_split_cms_upload_digest") is not None:
            # Clearing the upload invalidates the cached private reference.
            st.session_state["client_split_cms_history"] = {}
            st.session_state["client_split_cms_upload_digest"] = None
            st.session_state["client_split_preview"] = None

        st.caption("Optional automatic-refresh alternative: enter the private Google "
                   "Sheet ID above and grant the app's service account Viewer access.")
        if st.button("Load CMS deal monthly rates (read only)", key="cs_load_cms_rates"):
            if cms_excel_upload is not None:
                st.info("Uploaded Excel rates are already loaded. Remove the upload "
                        "before switching to the live Google Sheet source.")
            elif not cms_rates_id.strip():
                st.error("Enter the monthly-rate workbook sheet ID first, or upload its .xlsx file above.")
            else:
                try:
                    reference = _connect(cms_rates_id)
                    raw = reference.worksheet(rate_worksheet).get(
                        "A1:N1000", value_render_option="UNFORMATTED_VALUE"
                    )
                    st.session_state["client_split_cms_history"] = build_cms_rate_history(raw, partner_aliases, retention_header)
                    st.session_state["client_split_preview"] = None
                    st.success("Loaded monthly CMS deal rates. Missing or changed rates remain visible.")
                except Exception as exc:
                    st.error(f"Could not load monthly CMS deal rates: {exc}")
    with col_b:
        if st.button("Load previously saved client settings", key="cs_load_saved"):
            if not target_id.strip():
                st.error("Enter the target spreadsheet ID first.")
            else:
                try:
                    saved = read_sheet_configuration(_connect(target_id).worksheet(TARGET_WORKSHEET))
                    if saved is None:
                        st.warning("This report worksheet has no saved settings yet.")
                    else:
                        st.session_state["client_split_rows"] = saved
                        st.session_state["client_split_editor_rev"] += 1
                        st.session_state["client_split_preview"] = None
                        st.rerun()
                except Exception as exc:
                    st.error(f"Cannot load saved settings: {exc}")

    catalog = st.session_state["client_split_catalog"]
    if catalog:
        records = [{
            "Reference name": candidate.name,
            "HaHaHa %": candidate.single_rate,
            "Status": "Ready" if candidate.single_rate is not None else "Manual review required",
            "Source": ", ".join(candidate.sources),
            "Why flagged": "; ".join(candidate.issues) if candidate.issues else (
                "Different rates on separate lines" if len(candidate.percentages) > 1 else ""),
        } for candidate in sorted(catalog.values(), key=lambda item: item.name.casefold())]
        st.dataframe(pd.DataFrame(records), use_container_width=True, hide_index=True)
        st.caption("Reference rates are HaHaHa's percentage (Procent HA / Share Producer 1). "
                   "Rates differing by recording, scope or date must be manually verified.")

    history = st.session_state["client_split_cms_history"]
    if history:
        cms_records = []
        for client, monthly in sorted(history.items()):
            for period, records in sorted(monthly.items(), reverse=True):
                unique_rates = sorted({item.hahahaha_pct for item in records})
                unique_factors = sorted({str(item.post_intermediation_pct) for item in records})
                cms_records.append({"Client": client, "Month": period,
                                    "Deal %": ", ".join(f"{x:g}%" for x in unique_rates),
                                    "HaHaHa kept after intermediary %": ", ".join(unique_factors),
                                    "Source rows": ", ".join(str(x.source_row) for x in records)})
        st.dataframe(pd.DataFrame(cms_records), use_container_width=True, hide_index=True)
        st.caption("Historic rates are applied for their original month. Later months may carry the last "
                   "recorded rate forward, visibly flagged for manual confirmation.")

st.subheader("3. Configure client groups")
st.caption(
    "The eight CMS Deals group IDs come from the existing app. Add Distributie groups via "
    "'Discover YouTube groups', or enter an ID yourself. If you only have channel IDs, "
    "use channels:UCxxxxxxxx...,UCyyyyyyyy... (up to 500 distinct channels). "
    "Enter the HaHaHa percentage or a verified contract reference, NOT the client payout %. "
    "Post-intermediation % is the portion of HaHaHa's commission it keeps after brokers."
)

if st.button("Discover available YouTube CMS groups", key="cs_discover"):
    try:
        if not owner.strip():
            raise ClientSplitError("Missing YouTube CMS content owner ID")
        cfg = YoutubeConfig(content_owner=owner.strip(),
                            on_behalf_of_content_owner=on_behalf.strip() or None,
                            currency="EUR")
        st.session_state["client_split_discovered"] = list_groups(build_yta_service(cfg), cfg)
        st.success(f"Found {len(st.session_state['client_split_discovered'])} CMS groups. "
                   "Select only genuine Distribution client groups below.")
    except Exception as exc:
        st.error(f"YouTube group discovery failed: {exc}")

discovered = st.session_state.get("client_split_discovered", [])
if discovered:
    group_options = {f"{g['title']} [{g['id']}]": g for g in discovered if g.get("id")}
    selected_groups = st.multiselect("Select new Distribution groups to add", options=list(group_options),
                                      key="cs_group_add")
    if st.button("Add selected as Distribution", key="cs_add_groups"):
        existing_ids = {str(row.get("CMS group ID", "")) for row in st.session_state["client_split_rows"]}
        for name in selected_groups:
            g = group_options[name]
            if g["id"] not in existing_ids:
                st.session_state["client_split_rows"].append({
                    "Category": "Distributie", "Client": g["title"], "CMS group ID": g["id"],
                    "HaHaHa % (manual)": None,
                    "Post-intermediation % (manual)": None, "Contract reference": "",
                    "Notes": "Verify channel-group membership and contract rate", "Enabled": True,
                })
                existing_ids.add(g["id"])
        st.session_state["client_split_editor_rev"] += 1
        st.session_state["client_split_preview"] = None
        st.rerun()

st.caption("Verify each additional source ID, contract scope and rate before enabling it.")
columns = ["Category", "Client", "CMS group ID", "HaHaHa % (manual)",
           "Post-intermediation % (manual)", "Contract reference", "Notes", "Enabled"]
editor_df = pd.DataFrame(st.session_state["client_split_rows"], columns=columns)
# Ensure Streamlit treats a column initialized with None as editable numeric cells.
for percent_col in ("HaHaHa % (manual)", "Post-intermediation % (manual)"):
    editor_df[percent_col] = pd.to_numeric(editor_df[percent_col], errors="coerce")
edited = st.data_editor(
    editor_df, hide_index=True, use_container_width=True, num_rows="dynamic",
    key=f"client_split_editor_{st.session_state['client_split_editor_rev']}",
    column_config={
        "Category": st.column_config.SelectboxColumn("Category", options=list(CATEGORIES), required=True),
        "Client": st.column_config.TextColumn("Client", required=True),
        "CMS group ID": st.column_config.TextColumn(
            "CMS group ID / channels:UC...", required=True,
            help="Either a YouTube Analytics CMS group ID, or channels:UCxxx,UCyyy"
        ),
        "HaHaHa % (manual)": st.column_config.NumberColumn(
            "HaHaHa % (manual)", min_value=0, max_value=100, step=0.5, format="%.2f"
        ),
        "Post-intermediation % (manual)": st.column_config.NumberColumn(
            "Post-intermediation % (manual)", min_value=0, max_value=100,
            step=0.5, format="%.2f", help="If an intermediary takes a cut of HaHaHa's commission, "
                                           "enter the verified retained percentage of the commission."
        ),
        "Contract reference": st.column_config.TextColumn("Contract reference"),
        "Notes": st.column_config.TextColumn("Notes"),
        "Enabled": st.column_config.CheckboxColumn("Enabled", default=True),
    },
)
editor_rows = edited.to_dict("records")
st.session_state["client_split_rows"] = editor_rows
st.download_button(
    "Download client-group configuration CSV (backup)",
    data=_safe_export_csv(edited),
    file_name="client_split_configuration.csv", mime="text/csv",
    key="cs_download_config",
)

uploaded = st.file_uploader("Import previously saved client-group CSV", type=["csv"], key="cs_import_csv")
if uploaded is not None and st.button("Load this CSV into the editor", key="cs_apply_csv"):
    try:
        imported = pd.read_csv(io.BytesIO(uploaded.getvalue()), keep_default_na=False)
        missing_columns = [c for c in columns if c not in imported.columns]
        if missing_columns:
            raise ClientSplitError("CSV is missing columns: " + ", ".join(missing_columns))
        imported = imported.loc[:, columns]
        # Validate before replacing an editor configuration.
        read_rules(imported.to_dict("records"))
        st.session_state["client_split_rows"] = imported.to_dict("records")
        st.session_state["client_split_editor_rev"] += 1
        st.session_state["client_split_preview"] = None
        st.rerun()
    except Exception as exc:
        st.error(f"Could not import CSV: {exc}")

catalog = st.session_state["client_split_catalog"]
try:
    parsed_rules = read_rules(editor_rows)
    resolved_rules = resolve_rates(parsed_rules, catalog)
    cms_history = st.session_state["client_split_cms_history"]
    signature = _rules_signature(resolved_rules, months, tax_pct, target_id, contract_id,
                                 cms_rates_id, cms_history, owner, on_behalf)
    active = [r for r in resolved_rules if r.enabled]
    unresolved = [r for r in active if any(
        resolve_cms_monthly_rate(r, m, cms_history).hahahaha_pct is None
        for m in months
    )]
    overrides = [r for r in active if r.warnings or r.hahahaha_pct is not None
                 or r.post_intermediation_pct is not None]
    category_set = {r.category for r in active}
    st.write(f"**Active groups:** {len(active)} · **Rates requiring review:** {len(unresolved)}")
    if unresolved:
        st.warning("Unresolved rates: " + ", ".join(r.client for r in unresolved) +
                   ". The preview will show revenue, but publishing is blocked until each rate is verified.")
    if CATEGORIES[1] not in category_set:
        st.warning("No Distributie client groups configured yet. A complete Distribution forecast needs group IDs.")
except ClientSplitError as exc:
    parsed_rules = []
    resolved_rules = []
    active = []
    unresolved = []
    overrides = []
    category_set = set()
    signature = ""
    st.error(f"Fix configuration before previewing: {exc}")

st.subheader("4. Read-only preview and reconciliation")
preview_clicked = st.button("Fetch YouTube data and preview (no writes)",
                            type="primary", key="cs_preview")
if preview_clicked:
    for confirmation in ("cs_confirm", "cs_recon_confirm", "cs_manual_confirm", "cs_overlap_confirm"):
        st.session_state[confirmation] = False
    st.session_state["client_split_preview"] = None
    if not active or not owner.strip() or not target_id.strip() or not months:
        st.error("Provide valid groups, a CMS owner, a target Sheet ID and a valid month range.")
    else:
        try:
            # Re-read the current contract rates for every preview. This prevents
            # stale rates cached in a Streamlit session from driving payouts.
            if contract_id.strip() and any(r.hahahaha_pct is None for r in parsed_rules if r.enabled):
                reference = _connect(contract_id)
                source_contracts = reference.worksheet(terms_worksheet).get(
                    "A1:H150", value_render_option="FORMATTED_VALUE"
                )
                source_details = reference.worksheet(assets_worksheet).get(
                    "A1:E850", value_render_option="FORMATTED_VALUE"
                )
                st.session_state["client_split_catalog"] = build_rate_catalog(
                    source_contracts, source_details
                )
                resolved_rules = resolve_rates(parsed_rules, st.session_state["client_split_catalog"])
                cms_history = st.session_state["client_split_cms_history"]
                signature = _rules_signature(resolved_rules, months, tax_pct, target_id, contract_id,
                                             cms_rates_id, cms_history, owner, on_behalf)
            if cms_rates_id.strip() and cms_excel_upload is None:
                # Only live-sheet mode refreshes via service account. An Excel
                # upload uses the snapshot uploaded in this Streamlit session.
                # Refresh the source before *every* new forecast preview, not
                # just when the user clicks the load button.
                sheet = _connect(cms_rates_id)
                rows = sheet.worksheet(rate_worksheet).get(
                    "A1:N1000", value_render_option="UNFORMATTED_VALUE"
                )
                st.session_state["client_split_cms_history"] = build_cms_rate_history(rows, partner_aliases, retention_header)
                cms_history = st.session_state["client_split_cms_history"]
                signature = _rules_signature(resolved_rules, months, tax_pct, target_id, contract_id,
                                             cms_rates_id, cms_history, owner, on_behalf)
            cfg = YoutubeConfig(content_owner=owner.strip(),
                                on_behalf_of_content_owner=on_behalf.strip() or None,
                                currency="EUR")
            service = build_yta_service(cfg)
            # The month dimension requires both API dates on the first day.
            start_date, end_date = months[0] + "-01", months[-1] + "-01"
            unique_rules = [r for r in resolved_rules if r.enabled]
            revenues: Dict[str, Dict[str, Dict[str, float]]] = {}
            memberships = {}
            member_warnings = []
            progress = st.progress(0.0)
            info = st.empty()
            for index, rule in enumerate(unique_rules):
                info.write(f"Reading {rule.client} ({rule.category})…")
                total = query_monthly_revenue_for_source(
                    service, cfg, start_date, end_date, rule.group_id
                )
                us = query_monthly_revenue_for_source(
                    service, cfg, start_date, end_date, rule.group_id, country="US"
                )
                revenues[rule.group_id] = {"total": total, "us": us}
                try:
                    explicit_channels = source_channel_ids(rule.group_id)
                    channel_ids = (list(explicit_channels) if explicit_channels else
                                   [item["channelId"] for item in list_group_items(service, cfg, rule.group_id)])
                    # Legacy groupItems helper also returns video/asset IDs.
                    # Only valid channel IDs prove channel-group membership.
                    for channel_id in channel_ids:
                        source_channel_ids("channels:" + channel_id)
                    memberships[rule.group_id] = channel_ids
                    if not channel_ids:
                        member_warnings.append(f"No channel IDs returned for {rule.client} ({rule.group_id})")
                except Exception as exc:
                    member_warnings.append(f"Membership not verified for {rule.client}: {exc}")
                progress.progress((index + 1) / len(unique_rules))
            info.empty()
            stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            report = build_report(unique_rules, months, revenues, tax_pct, stamp,
                                  cms_rate_history=cms_history)
            overlap_rows = find_channel_overlaps(unique_rules, memberships)
            reconciliation: List[Dict[str, Any]] = []
            recon_error = ""
            try:
                main = _connect(target_id).worksheet(MAIN_WORKSHEET)
                existing = main.get(f"A1:N{min(main.row_count, 1200)}",
                                    value_render_option="FORMATTED_VALUE")
                reconciliation = reconcile_to_main_sheet(report, existing)
            except Exception as exc:
                recon_error = str(exc)
            st.session_state["client_split_preview"] = {
                "signature": signature, "report": report, "months": months,
                "reconciliation": reconciliation, "recon_error": recon_error,
                "missing_rates": _invalid_report(report),
                "overlaps": overlap_rows, "membership_warnings": member_warnings,
            }
        except Exception as exc:
            st.error(f"Preview failed without writing anything: {exc}")

preview = st.session_state.get("client_split_preview")
if preview:
    if signature != preview["signature"]:
        st.warning("Inputs changed after preview. Generate a new preview before publishing.")
    report = preview["report"]
    summary = _client_summary_view(report)
    st.markdown("**Per-client totals (EUR) — monthly forecast**")
    st.dataframe(summary, use_container_width=True, hide_index=True)
    st.markdown("**Group-by-group audit detail (EUR)**")
    st.dataframe(_render_report_df(report), use_container_width=True, hide_index=True)
    review_rows = [r for r in report if r["Status"] != "OK"]
    if review_rows:
        st.warning("Review the rows flagged below. Missing YouTube Analytics rows are **not** "
                   "treated as confirmed zero payouts and cannot be published.")
        st.dataframe(pd.DataFrame(review_rows)[["Month", "Category", "Client", "Status"]],
                     use_container_width=True, hide_index=True)
    if preview["overlaps"]:
        st.error("Some channels appear in multiple selected groups and may be double-counted.")
        st.dataframe(pd.DataFrame(preview["overlaps"]), use_container_width=True, hide_index=True)
    if preview["membership_warnings"]:
        st.error("Publishing is blocked: group membership could not be verified.\n" +
                 "\n".join("• " + issue for issue in preview["membership_warnings"]))
    recon = preview["reconciliation"]
    if recon:
        st.markdown("**Reconciliation against the existing monthly worksheet (read-only)**")
        st.dataframe(pd.DataFrame(recon), hide_index=True, use_container_width=True)
        if any(not row["Matches"] for row in recon):
            st.warning("The breakdown does not fully match the existing monthly totals. "
                       "Check missing/overlapping groups, dates, and whether the main tab is updated.")
    elif preview["recon_error"]:
        st.warning("Could not reconcile with the existing main worksheet: " + preview["recon_error"])
    else:
        st.warning("No matching monthly totals were found for reconciliation. "
                   "Verify dates and group coverage before publishing.")

    st.download_button(
        "Download preview CSV (without writing to Sheets)",
        data=_safe_export_csv(_render_report_df(report)),
        file_name=f"client_split_detail_{months[0]}_to_{months[-1]}.csv", mime="text/csv",
        key="cs_download_preview",
    )
    st.download_button(
        "Download per-client totals CSV",
        data=_safe_export_csv(summary),
        file_name=f"client_split_totals_{months[0]}_to_{months[-1]}.csv", mime="text/csv",
        key="cs_download_summary",
    )

st.subheader("5. Publish to a NEW worksheet only")
st.caption(
    "Publishing is opt-in and requires verified rates plus actual YouTube data. "
    "Existing months in 'Client Split Forecast' are kept unless "
    "selected for regeneration; the original 'Calcul Procentual Venituri Luna' tab is never modified. "
    "Client settings are saved in T–AA; a separate per-client summary appears in AC–AR."
)
accept_report = st.checkbox("I reviewed the client percentages, source groups and preview totals.",
                            key="cs_confirm")
accept_recon = st.checkbox(
    "I understand that missing/mismatching reconciliation means this forecast may be incomplete.",
    key="cs_recon_confirm"
)
accept_manual = st.checkbox(
    "I have verified all manual rates, carried-forward historical rates, and intermediary deductions for these months.",
    key="cs_manual_confirm"
)
accept_groups = st.checkbox(
    "I verified the group membership and checked for overlapping channels before publishing.",
    key="cs_overlap_confirm"
)
reconciliation_problem = bool(preview and (
    preview["recon_error"] or not preview["reconciliation"] or
    any(not item["Matches"] for item in preview["reconciliation"]) or
    len(category_set) != len(CATEGORIES)
))
needs_rate_review = bool(preview and any(
    "VERIFY RATE / INTERMEDIATION" in r["Status"] or
    "Manual" in r["Rate source"] for r in preview["report"]
))
ready = bool(preview and signature == preview["signature"] and accept_report and
             not preview["missing_rates"] and (not needs_rate_review or accept_manual) and
             (not reconciliation_problem or accept_recon) and
             not preview["overlaps"] and not preview["membership_warnings"] and accept_groups)
if st.button("Publish verified forecast to 'Client Split Forecast'", type="primary",
             disabled=not ready, key="cs_publish"):
    try:
        # Confirm current settings correspond exactly to the read-only preview.
        if signature != preview["signature"] or _invalid_report(preview["report"]):
            raise ClientSplitError("Stale or incomplete preview; run again")
        spreadsheet = _connect(target_id)
        # Fail closed if main worksheet is absent; do not accidentally write to wrong file.
        spreadsheet.worksheet(MAIN_WORKSHEET)
        ws, num_rows = publish_report(
            spreadsheet, TARGET_WORKSHEET, preview["report"], preview["months"],
            tax_pct, editor_rows,
        )
        actual_header = ws.row_values(1)
        if actual_header[:4] != ["Month", "Category", "Client", "CMS group ID"]:
            raise RuntimeError("Write completed but output header verification failed")
        st.success(f"Published {num_rows} group-month detail rows plus client totals. "
                   f"Only '{TARGET_WORKSHEET}' was updated.")
        st.markdown(
            f"[Open client summary (AC:AR)](https://docs.google.com/spreadsheets/d/"
            f"{sheet_id_from_input(target_id)}/edit#gid={ws.id}&range=AC1)"
        )
    except Exception as exc:
        st.error(f"No legacy tab was changed. Could not publish: {exc}")
