"""The attached private deal workbook is *not* copied into the public repo.

These synthetic rows check its documented column structure and safeguards.
"""
from dataclasses import replace

import pytest
import io
from openpyxl import Workbook

from src.client_split import (
    ClientSplitError, Rule, build_cms_rate_history, build_cms_rate_history_from_xlsx, build_report,
    calculate_split, default_rule_rows, read_rules, resolve_cms_monthly_rate,
    resolve_rates, summarize_by_client, publish_report, TARGET_WORKSHEET,
)


def fixture_rows():
    header = [None] * 14
    header[3:8] = ["Luna", "AN", "Partner", "Furnizor pt Factura", "Deal%"]
    header[12] = "Procent Ha dupa intermediere"
    def entry(month, year, name, rate, post):
        row = [None]*14
        row[3],row[4],row[5],row[7],row[12] = month,year,name,rate,post
        return row
    return [[], [], header,
        entry(12,2025,"BG Music",0.25,1),
        entry(2,2026,"BG Music",0.27,1),
        entry(8,2026,"BG Music","27%","100%"),
        entry(8,2026,"Unicorn Recording Ltd,.",0.13,0.75),
        entry(8,2026,"Melosy",0.25,1),
        entry(8,2026,"Huta Media",0.30,1),
        entry(8,2026,"Magic",0.10,1),
        entry(6,2026,"Pine Entertainement",0.30,1),
        entry(8,2026,"Unitech Media",0.35,1),  # unrelated to Malwin
        entry(9,2026,"BG Music","prea putini bani pt raport",1),
    ]


def test_correct_rate_columns_and_six_confirmed_groups():
    rates = build_cms_rate_history(fixture_rows())
    assert set(rates) == {"BG Music", "Unicorn", "Melosy", "Huta Media", "Magic", "Pine"}
    assert rates["Unicorn"]["2026-08"][0].post_intermediation_pct == 75
    assert rates["Unicorn"]["2026-08"][0].hahahaha_pct == 13
    assert rates["BG Music"]["2026-08"][0].hahahaha_pct == 27
    assert "2026-09" not in rates["BG Music"]  # narrative is not a usable rate


def test_monthly_rate_changes_are_respected():
    history = build_cms_rate_history(fixture_rows())
    rule = Rule("CMS Deals", "BG Music", "aZ9OPuMpAD8")
    assert resolve_cms_monthly_rate(rule, "2025-12", history).hahahaha_pct == 25
    assert resolve_cms_monthly_rate(rule, "2026-02", history).hahahaha_pct == 27
    assert resolve_cms_monthly_rate(rule, "2026-08", history).hahahaha_pct == 27
    carry = resolve_cms_monthly_rate(rule, "2026-09", history)
    assert carry.hahahaha_pct == 27
    assert "CARRIED FORWARD" in " ".join(carry.warnings)


def test_income_uses_correct_month_rate_not_one_rate_for_date_range():
    history = build_cms_rate_history(fixture_rows())
    rule = Rule("CMS Deals", "BG Music", "aZ9OPuMpAD8")
    revenues = {rule.group_id: {"total":{"2025-12": 1000,"2026-02": 1000},
                                "us":{"2025-12": 0,"2026-02": 0}}}
    report = build_report([rule], ["2025-12","2026-02"], revenues, 10, "UTC", history)
    assert [r["HaHaHa deal %"] for r in report] == [0.25,0.27]
    assert [r["Payable to client EUR"] for r in report] == [750.0,730.0]


def test_unicorn_intermediary_deducted_from_hahahaha_commission_only():
    x = calculate_split(1000,100,13,10,75)
    assert x["after_withholding"] == 990
    assert x["client_payable"] == 861.3
    assert x["hahahaha_gross_commission"] == 128.7
    assert x["broker_fee"] == 32.175
    assert x["hahahaha_final_net"] == 96.525
    assert round(x["client_payable"]+x["broker_fee"]+x["hahahaha_final_net"],2) == 990


def test_unicorn_without_broker_rate_must_not_fake_full_profit():
    rule = Rule("CMS Deals", "Unicorn", "WBbE5H4__OU", 13)
    r = build_report([rule],["2026-08"], {rule.group_id: {
        "total":{"2026-08":1000},"us":{"2026-08":100}}},10,"UTC")[0]
    assert "CHECK INTERMEDIATION" in r["Status"]
    assert r["HaHaHa final net EUR"] is None
    assert r["Payable to client EUR"] is not None


def test_unicorn2_not_auto_assigned_unicorn_contract_if_renamed():
    source = build_cms_rate_history(fixture_rows())
    r = resolve_cms_monthly_rate(Rule("CMS Deals","Unicorn","Lcy1MVF5Fck"),
                                 "2026-08", source)
    assert r.hahahaha_pct is None
    assert r.post_intermediation_pct is None


def test_coproduction_and_cid_rates_cannot_be_inferred_from_zurli_contract():
    rows = read_rules(default_rule_rows())
    zurli = [r for r in rows if r.client == "Zurli"]
    assert len(zurli) == 3
    assert zurli[0].contract_ref == "ZURLI"
    assert "VERIFY" in zurli[1].contract_ref
    assert "VERIFY" in zurli[2].contract_ref


def test_no_automated_lalaplanet_or_juno_contract_assumption():
    rows = read_rules(default_rule_rows())
    assert any("VERIFY" in r.contract_ref for r in rows if r.client == "LaLa Planet")
    assert any("VERIFY" in r.contract_ref for r in rows if r.client == "JUNO / Elixir")


def test_disabled_soundfeed_unicode_id_cannot_accidentally_go_live():
    rows = default_rule_rows()
    broken = next(r for r in rows if r["Client"] == "Soundfeed")
    assert not broken["Enabled"]
    broken["Enabled"] = True
    with pytest.raises(ClientSplitError, match="CMS group ID"):
        read_rules(rows)


def test_manual_override_does_not_hide_source_disagreement():
    h = build_cms_rate_history(fixture_rows())
    rule = Rule("CMS Deals", "Unicorn", "WBbE5H4__OU", 20,
                post_intermediation_pct=80)
    result = resolve_cms_monthly_rate(rule, "2026-08", h)
    assert result.hahahaha_pct == 20
    assert result.post_intermediation_pct == 80
    assert any("MANUAL RATE DIFFERS" in w for w in result.warnings)
    assert any("MANUAL INTERMEDIATION" in w for w in result.warnings)


def test_conflicting_same_month_records_require_manual_decision():
    raw=fixture_rows()
    other = list(raw[5])
    other[7] = 0.23
    raw.append(other)
    h=build_cms_rate_history(raw)
    r=resolve_cms_monthly_rate(Rule("CMS Deals","BG Music","aZ9OPuMpAD8"),"2026-08",h)
    assert r.hahahaha_pct is None
    assert "CONFLICTING" in " ".join(r.warnings)


def test_missing_client_rate_blocks_summary_payment_and_publish():
    h=build_cms_rate_history(fixture_rows())
    r=Rule("CMS Deals","Malwin","JGSzjsvdIDk")
    report=build_report([r],["2026-08"], {r.group_id:{
        "total":{"2026-08":10},"us":{"2026-08":1}}},10,"UTC",h)
    assert summarize_by_client(report)[0]["Payable to client EUR"] is None
    with pytest.raises(ClientSplitError, match="Incomplete client payouts"):
        publish_report(object(), TARGET_WORKSHEET, report, ["2026-08"], 10, [])


def test_wrong_source_headers_are_rejected():
    with pytest.raises(ClientSplitError, match="expected Partner"):
        build_cms_rate_history([["a","b","c"]])


def test_missing_brokerage_factor_for_cms_deal_blocks_final_net():
    r=Rule("CMS Deals","Pine","BXowAx3iy4k",30)
    detail=build_report([r],["2026-08"],{r.group_id:{
        "total":{"2026-08":200},"us":{"2026-08":0}}},10,"UTC")[0]
    assert detail["Payable to client EUR"] == 140
    assert "CHECK INTERMEDIATION" in detail["Status"]
    assert detail["HaHaHa final net EUR"] is None


def test_report_months_cannot_inject_other_month_data():
    r=Rule("Distributie","Zurli","ZhkeOTUjhr8",15)
    report=build_report([r],["2026-08"],{r.group_id:{
        "total":{"2026-08":100},"us":{"2026-08":0}}},10,"UTC")
    with pytest.raises(ClientSplitError, match="Report months"):
        publish_report(object(),TARGET_WORKSHEET,report,["2026-09"],10,[])


def test_uploaded_excel_rates_work_without_a_google_sheet_connection():
    wb = Workbook()
    ws = wb.active
    ws.title = "Tabel Raportari"
    header = fixture_rows()[2]
    for col, cell in enumerate(header, 1):
        if cell is not None:
            ws.cell(row=3, column=col, value=cell)
    ws.cell(row=4, column=4, value=8)
    ws.cell(row=4, column=5, value=2026)
    ws.cell(row=4, column=6, value="Unicorn Recording Ltd,.")
    ws.cell(row=4, column=8, value=0.13)
    ws.cell(row=4, column=13, value=0.75)
    out = io.BytesIO()
    wb.save(out)
    history = build_cms_rate_history_from_xlsx(out.getvalue())
    item = history["Unicorn"]["2026-08"][0]
    assert item.hahahaha_pct == 13
    assert item.post_intermediation_pct == 75


def test_uploaded_excel_rejects_missing_reference_tab_and_bad_bytes():
    wb = Workbook()
    wb.active.title = "Different tab"
    out = io.BytesIO()
    wb.save(out)
    with pytest.raises(ClientSplitError, match="missing the 'Tabel Raportari' tab"):
        build_cms_rate_history_from_xlsx(out.getvalue())
    with pytest.raises(ClientSplitError, match="Could not open"):
        build_cms_rate_history_from_xlsx(b"not a workbook")


def test_zero_percent_accounting_line_does_not_override_contract_rate():
    rows = fixture_rows()
    zero = list(rows[5])
    zero[3:5] = [9, 2026]
    zero[7] = 0.0
    zero[12] = 0.0
    rows.append(zero)
    h = build_cms_rate_history(rows)
    assert "2026-09" not in h["BG Music"]
    decision = resolve_cms_monthly_rate(Rule("CMS Deals", "BG Music", "aZ9OPuMpAD8"), "2026-09", h)
    assert decision.hahahaha_pct == 27
    assert "CARRIED FORWARD" in " ".join(decision.warnings)
