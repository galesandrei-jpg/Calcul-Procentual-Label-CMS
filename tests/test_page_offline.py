"""Static and client-total tests, without a Streamlit server or Google API."""

import ast
from pathlib import Path

from src.client_split import (
    ClientSplitError,
    Rule,
    REPORT_HEADERS,
    SUMMARY_HEADERS,
    build_report,
    find_channel_overlaps,
    publish_report,
    report_row_for_sheet,
    summarize_by_client,
)

PAGE = Path(__file__).resolve().parents[1] / "pages" / "client_split_forecast.py"


def test_new_streamlit_page_compiles_without_old_app_changes():
    assert PAGE.is_file()
    ast.parse(PAGE.read_text(encoding="utf-8"))
    code = PAGE.read_text()
    assert 'st.set_page_config' in code
    assert 'st.data_editor' in code
    assert 'st.file_uploader' in code  # upload private rates; no extra account needed
    assert 'build_yta_service' in code
    assert 'query_monthly_revenue_for_source' in code
    assert 'publish_report' in code
    assert 'src.sheets import _sa_client' in code
    assert 'import app' not in code
    assert '.delete_worksheet(' not in code
    assert 'ws.clear(' not in code


def test_client_totals_sum_two_cms_groups_and_label_rate_once():
    rules = [Rule("CMS Deals", "Unicorn", "GROUP_1", 15, rate_source="Manual", post_intermediation_pct=100),
             Rule("CMS Deals", "Unicorn", "GROUP_2", 15, rate_source="Manual", post_intermediation_pct=100)]
    amounts = {"GROUP_1": {"total": {"2026-08": 500}, "us": {"2026-08": 50}},
               "GROUP_2": {"total": {"2026-08": 500}, "us": {"2026-08": 50}}}
    report = build_report(rules, ["2026-08"], amounts, 10, "2026-10-30 UTC")
    summary = summarize_by_client(report)
    assert len(summary) == 1
    r = summary[0]
    assert r["Client"] == "Unicorn"
    assert r["Gross EUR"] == 1000
    assert r["US withholding EUR"] == 10
    assert r["HaHaHa deal %"] == 0.15
    assert r["Client share %"] == 0.85
    assert r["Payable to client EUR"] == 841.5
    assert r["HaHaHa final net EUR"] == 148.5
    assert "GROUP_1" in r["CMS group IDs"] and "GROUP_2" in r["CMS group IDs"]


def test_different_rates_are_marked_mixed_instead_of_misleading_average():
    rules = [Rule("Distributie", "Soundfeed", "A", 10),
             Rule("Distributie", "Soundfeed", "B", 15)]
    data = {"A": {"total": {"2026-08": 500}, "us": {"2026-08": 0}},
            "B": {"total": {"2026-08": 500}, "us": {"2026-08": 0}}}
    summary = summarize_by_client(build_report(rules, ["2026-08"], data, 10, "now"))[0]
    assert summary["HaHaHa deal %"] == "MIXED"
    assert summary["Client share %"] == "MIXED"
    assert summary["Payable to client EUR"] == 875
    assert summary["HaHaHa final net EUR"] == 125


def test_missing_rate_blocks_aggregated_client_payment():
    rules = [Rule("Distributie", "Zurli", "A", 15),
             Rule("Distributie", "Zurli", "B")]
    data = {"A": {"total": {"2026-08": 500}, "us": {"2026-08": 0}},
            "B": {"total": {"2026-08": 500}, "us": {"2026-08": 0}}}
    summary = summarize_by_client(build_report(rules, ["2026-08"], data, 10, "now"))[0]
    assert summary["HaHaHa deal %"] == "UNVERIFIED"
    assert summary["Payable to client EUR"] is None
    assert summary["HaHaHa final net EUR"] is None


def test_channel_overlaps_are_detected_across_different_group_ids():
    groups = [Rule("CMS Deals", "Unicorn", "g1"),
              Rule("CMS Deals", "Unicorn", "g2"),
              Rule("Distributie", "Zurli", "g3")]
    matches = find_channel_overlaps(groups, {"g1": ["UCshared", "UCa"],
                                             "g2": ["UCshared", "UCb"],
                                             "g3": ["UCc"]})
    assert len(matches) == 1
    assert matches[0]["Channel ID"] == "UCshared"
    assert "g1" in matches[0]["Groups with same channel"]
    assert "g2" in matches[0]["Groups with same channel"]


def test_page_loads_in_pinned_streamlit_without_network_or_writes(monkeypatch):
    import streamlit as st
    from streamlit.testing.v1 import AppTest
    monkeypatch.setattr(st, "secrets", {})
    page = AppTest.from_file(str(PAGE)).run(timeout=30)
    assert not page.exception
    assert next(b.disabled for b in page.button if b.key == "cs_publish")


def test_preview_uses_api_month_boundaries_and_resets_approvals_without_writing(monkeypatch):
    import streamlit as st
    import src.youtube as youtube
    import src.client_split as split
    import src.sheets as sheets
    from streamlit.testing.v1 import AppTest
    monkeypatch.setattr(st, "secrets", {"youtube": {"content_owner": "SYNTHETIC_OWNER"},
                                      "sheets": {"sheet_id": "SYNTHETIC_SHEET"}})
    monkeypatch.setattr(youtube, "build_yta_service", lambda cfg: object())
    monkeypatch.setattr(youtube, "list_group_items", lambda *args: [{"channelId": args[-1]}])
    calls = []
    def revenue(service, cfg, start, end, source, country=None):
        calls.append((start, end, country))
        return {"2026-08": 1.0}
    monkeypatch.setattr(split, "query_monthly_revenue_for_source", revenue)
    def no_write(*args, **kwargs):
        raise AssertionError("Preview attempted to publish")
    monkeypatch.setattr(split, "publish_report", no_write)
    # Reconciliation is optional and remains read-only.
    monkeypatch.setattr(sheets, "_sa_client", lambda: None)
    page = AppTest.from_file(str(PAGE)).run(timeout=30)
    page.selectbox(key="cs_start_year").set_value(2026)
    page.selectbox(key="cs_end_year").set_value(2026)
    page.selectbox(key="cs_start_month").set_value(8)
    page.selectbox(key="cs_end_month").set_value(8)
    page.checkbox(key="cs_confirm").check()
    page.checkbox(key="cs_manual_confirm").check()
    page.button(key="cs_preview").click().run(timeout=30)
    assert not page.exception
    assert calls and all(start == "2026-08-01" and end == "2026-08-01" for start, end, _ in calls)
    assert not page.checkbox(key="cs_confirm").value
    assert not page.checkbox(key="cs_manual_confirm").value
    assert page.button(key="cs_publish").disabled
