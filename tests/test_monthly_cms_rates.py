"""Invented clients and financial values exercise monthly calculations offline."""
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
    header[3:8] = ["Luna", "AN", "Partner", "Unused column", "Deal%"]
    header[12] = "Retention"
    def entry(month, year, name, rate, post):
        row = [None]*14
        row[3],row[4],row[5],row[7],row[12] = month,year,name,rate,post
        return row
    return [[], [], header,
        entry(12,2023,"Example CMS B",0.21,1),
        entry(2,2024,"Example CMS B",0.26,1),
        entry(8,2024,"Example CMS B","26%","100%"),
        entry(8,2024,"Example CMS A Legal",0.17,0.60),
        entry(8,2024,"Example CMS C",0.19,1),
        entry(8,2024,"Example CMS D",0.32,1),
        entry(8,2024,"Example CMS E",0.14,1),
        entry(6,2024,"Example CMS F Legal",0.32,1),
        entry(8,2024,"Example Unmapped",0.31,1),  # unrelated to Example CMS G
        entry(9,2024,"Example CMS B","not a numeric rate",1),
    ]


def test_correct_rate_columns_and_six_confirmed_groups():
    rates = build_cms_rate_history(fixture_rows())
    assert set(rates) == {"Example CMS B", "Example CMS A", "Example CMS C", "Example CMS D", "Example CMS E", "Example CMS F"}
    assert rates["Example CMS A"]["2024-08"][0].post_intermediation_pct == 60
    assert rates["Example CMS A"]["2024-08"][0].hahahaha_pct == 17
    assert rates["Example CMS B"]["2024-08"][0].hahahaha_pct == 26
    assert "2024-09" not in rates["Example CMS B"]  # narrative is not a usable rate


def test_monthly_rate_changes_are_respected():
    history = build_cms_rate_history(fixture_rows())
    rule = Rule("CMS Deals", "Example CMS B", "EXAMPLE_CMS_B")
    assert resolve_cms_monthly_rate(rule, "2023-12", history).hahahaha_pct == 21
    assert resolve_cms_monthly_rate(rule, "2024-02", history).hahahaha_pct == 26
    assert resolve_cms_monthly_rate(rule, "2024-08", history).hahahaha_pct == 26
    carry = resolve_cms_monthly_rate(rule, "2024-09", history)
    assert carry.hahahaha_pct == 26
    assert "CARRIED FORWARD" in " ".join(carry.warnings)


def test_income_uses_correct_month_rate_not_one_rate_for_date_range():
    history = build_cms_rate_history(fixture_rows())
    rule = Rule("CMS Deals", "Example CMS B", "EXAMPLE_CMS_B")
    revenues = {rule.group_id: {"total":{"2023-12": 1000,"2024-02": 1000},
                                "us":{"2023-12": 0,"2024-02": 0}}}
    report = build_report([rule], ["2023-12","2024-02"], revenues, 10, "UTC", history)
    assert [r["HaHaHa deal %"] for r in report] == [0.21,0.26]
    assert [r["Payable to client EUR"] for r in report] == [790.0,740.0]


def test_intermediary_intermediary_deducted_from_hahahaha_commission_only():
    x = calculate_split(1000,100,17,10,60)
    assert x["after_withholding"] == 990
    assert x["client_payable"] == 821.7
    assert x["hahahaha_gross_commission"] == 168.3
    assert x["broker_fee"] == 67.32
    assert x["hahahaha_final_net"] == 100.98
    assert round(x["client_payable"]+x["broker_fee"]+x["hahahaha_final_net"],2) == 990


def test_intermediary_without_broker_rate_must_not_fake_full_profit():
    rule = Rule("CMS Deals", "Example CMS A", "EXAMPLE_CMS_A", 17)
    r = build_report([rule],["2024-08"], {rule.group_id: {
        "total":{"2024-08":1000},"us":{"2024-08":100}}},10,"UTC")[0]
    assert "CHECK INTERMEDIATION" in r["Status"]
    assert r["HaHaHa final net EUR"] is None
    assert r["Payable to client EUR"] is not None


def test_second_group_not_auto_assigned_intermediary_contract_if_renamed():
    source = build_cms_rate_history(fixture_rows())
    r = resolve_cms_monthly_rate(Rule("CMS Deals","Example CMS A","EXAMPLE_CMS_H"),
                                 "2024-08", source)
    assert r.hahahaha_pct is None
    assert r.post_intermediation_pct is None


def test_private_groups_are_not_bundled_in_defaults():
    assert all(r["Category"] == "CMS Deals" for r in default_rule_rows())


def test_disabled_invalid_id_cannot_accidentally_go_live():
    rows = default_rule_rows()
    broken = {"Category": "Distributie", "Client": "Example Disabled", "CMS group ID": "EXAMPLE—BAD",
              "Enabled": False}
    rows.append(broken)
    assert len(read_rules(rows)) == len(rows)
    broken["Enabled"] = True
    with pytest.raises(ClientSplitError, match="CMS group ID"):
        read_rules(rows)


def test_manual_override_does_not_hide_source_disagreement():
    h = build_cms_rate_history(fixture_rows())
    rule = Rule("CMS Deals", "Example CMS A", "EXAMPLE_CMS_A", 20,
                post_intermediation_pct=80)
    result = resolve_cms_monthly_rate(rule, "2024-08", h)
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
    r=resolve_cms_monthly_rate(Rule("CMS Deals","Example CMS B","EXAMPLE_CMS_B"),"2024-08",h)
    assert r.hahahaha_pct is None
    assert "CONFLICTING" in " ".join(r.warnings)


def test_missing_client_rate_blocks_summary_payment_and_publish():
    h=build_cms_rate_history(fixture_rows())
    r=Rule("CMS Deals","Example CMS G","EXAMPLE_CMS_G")
    report=build_report([r],["2024-08"], {r.group_id:{
        "total":{"2024-08":10},"us":{"2024-08":1}}},10,"UTC",h)
    assert summarize_by_client(report)[0]["Payable to client EUR"] is None
    with pytest.raises(ClientSplitError, match="Incomplete client payouts"):
        publish_report(object(), TARGET_WORKSHEET, report, ["2024-08"], 10, [])


def test_wrong_source_headers_are_rejected():
    with pytest.raises(ClientSplitError, match="Expected Partner"):
        build_cms_rate_history([["a","b","c"]])


def test_missing_brokerage_factor_for_cms_deal_blocks_final_net():
    r=Rule("CMS Deals","Example CMS F","EXAMPLE_CMS_F",30)
    detail=build_report([r],["2024-08"],{r.group_id:{
        "total":{"2024-08":200},"us":{"2024-08":0}}},10,"UTC")[0]
    assert detail["Payable to client EUR"] == 140
    assert "CHECK INTERMEDIATION" in detail["Status"]
    assert detail["HaHaHa final net EUR"] is None


def test_report_months_cannot_inject_other_month_data():
    r=Rule("Distributie","Example Distribution A","EXAMPLE_DIST_A",15)
    report=build_report([r],["2024-08"],{r.group_id:{
        "total":{"2024-08":100},"us":{"2024-08":0}}},10,"UTC")
    with pytest.raises(ClientSplitError, match="Report months"):
        publish_report(object(),TARGET_WORKSHEET,report,["2024-09"],10,[])


def test_uploaded_excel_rates_work_without_a_google_sheet_connection():
    wb = Workbook()
    ws = wb.active
    ws.title = "Monthly rates"
    header = fixture_rows()[2]
    for col, cell in enumerate(header, 1):
        if cell is not None:
            ws.cell(row=3, column=col, value=cell)
    ws.cell(row=4, column=4, value=8)
    ws.cell(row=4, column=5, value=2024)
    ws.cell(row=4, column=6, value="Example CMS A Legal")
    ws.cell(row=4, column=8, value=0.17)
    ws.cell(row=4, column=13, value=0.60)
    out = io.BytesIO()
    wb.save(out)
    history = build_cms_rate_history_from_xlsx(out.getvalue())
    item = history["Example CMS A"]["2024-08"][0]
    assert item.hahahaha_pct == 17
    assert item.post_intermediation_pct == 60


def test_uploaded_excel_rejects_missing_reference_tab_and_bad_bytes():
    wb = Workbook()
    wb.active.title = "Different tab"
    out = io.BytesIO()
    wb.save(out)
    with pytest.raises(ClientSplitError, match="missing the configured monthly-rate tab"):
        build_cms_rate_history_from_xlsx(out.getvalue())
    with pytest.raises(ClientSplitError, match="Could not open"):
        build_cms_rate_history_from_xlsx(b"not a workbook")


def test_zero_percent_accounting_line_does_not_override_contract_rate():
    rows = fixture_rows()
    zero = list(rows[5])
    zero[3:5] = [9, 2024]
    zero[7] = 0.0
    zero[12] = 0.0
    rows.append(zero)
    h = build_cms_rate_history(rows)
    assert "2024-09" not in h["Example CMS B"]
    decision = resolve_cms_monthly_rate(Rule("CMS Deals", "Example CMS B", "EXAMPLE_CMS_B"), "2024-09", h)
    assert decision.hahahaha_pct == 26
    assert "CARRIED FORWARD" in " ".join(decision.warnings)


def test_private_worksheet_header_and_partner_alias_are_runtime_inputs():
    wb = Workbook()
    ws = wb.active
    ws.title = "Example Custom Rates"
    header = [None]*14
    header[5], header[7], header[12] = "Partner", "Deal%", "Example retained share"
    ws.append(header)
    row = [None]*14
    row[3], row[4], row[5], row[7], row[12] = 4, 2024, "Example Private Legal Name", .23, .40
    ws.append(row)
    payload = io.BytesIO()
    wb.save(payload)
    history = build_cms_rate_history_from_xlsx(payload.getvalue(), "Example Custom Rates",
        {"Example Private Legal Name": "Example CMS A"}, "Example retained share")
    report = build_report([Rule("CMS Deals", "Example CMS A", "EXAMPLE_CMS_A")],
        ["2024-04"], {"EXAMPLE_CMS_A": {"total": {"2024-04": 200}, "us": {"2024-04": 20}}},
        10, "example", history)
    assert report[0]["Payable to client EUR"] == pytest.approx(152.46)
    assert report[0]["HaHaHa final net EUR"] == pytest.approx(18.216)
