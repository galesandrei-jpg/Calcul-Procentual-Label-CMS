"""Offline tests: no YouTube credentials, Google Sheet writes or app startup needed."""

import sys
import types

import pytest

from src.client_split import (
    CONFIG_HEADERS,
    EXISTING_CMS_GROUPS,
    MAIN_WORKSHEET,
    REPORT_HEADERS,
    SUMMARY_HEADERS,
    TARGET_WORKSHEET,
    ClientSplitError,
    Rule,
    build_rate_catalog,
    build_report,
    calculate_split,
    config_from_cells,
    config_to_cells,
    default_rule_rows,
    merge_month_rows,
    month_sequence,
    parse_money,
    parse_percent,
    publish_report,
    read_rules,
    reconcile_to_main_sheet,
    report_row_for_sheet,
    resolve_rates,
    summarize_by_client,
    find_channel_overlaps,
    source_channel_ids,
    query_monthly_revenue_for_source,
)


def test_eight_legacy_cms_deals_groups_are_copied_without_mutation():
    rows = default_rule_rows()
    assert len(rows) == 16  # eight untouched CMS groups + Tibi's eight Distribution mappings
    assert [r["Client"] for r in rows[:8]] == [name for name, _ in EXISTING_CMS_GROUPS]
    assert rows[0]["CMS group ID"] == "WBbE5H4__OU"
    assert all(r["HaHaHa % (manual)"] is None for r in rows)  # never guess confidential rates
    assert sum(r["Category"] == "Distributie" for r in rows) == 8
    assert {r["CMS group ID"] for r in rows if r["Client"] == "Zurli"} == {
        "ZhkeOTUjhr8", "a0FL5zlfU1M", "DXRapvE6aOc"}
    national = next(r for r in rows if r["Client"] == "National TV")
    assert national["CMS group ID"] == "channels:UCEiSdg0rO7AyJ7ky_vhzMBg"
    soundfeed = next(r for r in rows if r["Client"] == "Soundfeed")
    assert soundfeed["Enabled"] is False and "—" in soundfeed["CMS group ID"]
    assert not any("Mixton" in r["Client"] for r in rows)


@pytest.mark.parametrize("value,expected", [("15%", 15.0), ("15.00%", 15.0),
                                             ("10,5%", 10.5), (15, 15.0),
                                             ("0%", 0.0), ("100%", 100.0),
                                             ("20% 1 an / 15% dupa", None),
                                             ("120%", None), ("", None),
                                             ("=IMPORTXML()", None)])
def test_strict_pct(value, expected):
    assert parse_percent(value) == expected


def test_contract_catalog_conflicts_are_not_silently_resolved():
    contracts = [["date", "Cedent", "Nr", "Ment", "Procent HA"],
                 ["2026-01", "ZURLI", "9HAHM", "", "15%"],
                 ["2026-01", "SOUNDFEED", "100", "", "10%"],
                 ["2026-01", "PASSIONWAY", "123", "", "20% 1 an / 15% dupa"]]
    songs = [["Title", "Artist", "Share1", "Firma", "Share2"],
             ["A", "Gasca Zurli", "15.00%", "ZURLI", "85.00%"],
             ["B", "Edine", "10%", "SOUNDFEED", "90%"],
             ["C", "LaLaPlanet", "15%", "SOUNDFEED", "85%"]]
    catalog = build_rate_catalog(contracts, songs)
    assert catalog["zurli"].single_rate == 15
    assert catalog["soundfeed"].single_rate is None
    assert catalog["soundfeed"].percentages == (10.0, 15.0)
    assert catalog["passionway"].single_rate is None
    assert "conditional" in " ".join(catalog["passionway"].issues).lower()
    rules = [Rule("Distributie", "Zurli", "GROUP_Z", contract_ref="ZURLI"),
             Rule("Distributie", "Sounfeed", "GROUP_S", contract_ref="SOUNDFEED"),
             Rule("CMS Deals", "Unicorn", "GROUP_U")]
    rr = resolve_rates(rules, catalog)
    assert rr[0].hahahaha_pct == 15.0
    assert rr[1].hahahaha_pct is None
    assert rr[2].hahahaha_pct is None
    rr2 = resolve_rates([Rule("Distributie", "Sounfeed", "G", 10, "SOUNDFEED")], catalog)
    assert rr2[0].hahahaha_pct == 10.0
    assert rr2[0].warnings  # manual override marked for review


def test_manual_share_is_hahahaha_not_client_share():
    x = calculate_split(1000, 100, 15.0, 10)
    assert x["gross"] == 1000
    assert x["us_gross"] == 100
    assert x["us_withholding"] == 10
    assert x["after_withholding"] == 990
    assert x["client_share"] == 85
    assert x["client_payable"] == 841.5
    assert x["hahahaha_gross_commission"] == 148.5
    assert x["broker_fee"] == 0.0
    assert x["hahahaha_final_net"] == 148.5
    assert round(x["client_payable"]+x["hahahaha_final_net"],2) == x["after_withholding"]


def test_missing_pct_never_generates_a_fake_payment():
    amounts = calculate_split(1000, 100, None)
    assert amounts["client_payable"] is None
    assert amounts["hahahaha_final_net"] is None
    rules = resolve_rates([Rule("CMS Deals", "Unicorn", "GROUP_U")], {})
    report = build_report(rules, ["2026-08"], {"GROUP_U": {
        "total": {"2026-08": 1000.00}, "us": {"2026-08": 100.00}
    }}, 10, "2026-10-30 UTC")
    assert report[0]["Status"] == "CHECK RATE"
    assert report[0]["Payable to client EUR"] is None


def test_month_sequence_and_range_limit():
    assert month_sequence("2026-11", "2027-02") == ["2026-11", "2026-12", "2027-01", "2027-02"]
    with pytest.raises(ClientSplitError):
        month_sequence("2026-12", "2026-11")
    with pytest.raises(ClientSplitError):
        month_sequence("2024-01", "2026-07")


def test_double_counting_same_group_is_blocked():
    rows = [{"Category": "CMS Deals", "Client": "A", "CMS group ID": "X_X", "Enabled": True},
            {"Category": "Distributie", "Client": "B", "CMS group ID": "X_X", "Enabled": True}]
    with pytest.raises(ClientSplitError, match="cannot be counted twice"):
        read_rules(rows)
    rows[1]["CMS group ID"] = "bad;filter==x"
    with pytest.raises(ClientSplitError, match="CMS group ID or channels"):
        read_rules(rows)


def test_config_round_trip_and_strings_that_look_like_formulas_are_data():
    rows = [{"Category": "CMS Deals", "Client": "=SUM(A1:A10)", "CMS group ID": "X_2",
             "HaHaHa % (manual)": 15.0, "Contract reference": "ZURLI",
             "Notes": "=IMPORTXML('bad')", "Enabled": True}]
    cells = config_to_cells(rows)
    assert config_from_cells([list(CONFIG_HEADERS)] + cells)[0]["Client"] == "=SUM(A1:A10)"
    from src.client_split import _write_request
    req = _write_request(99, 2, 19, 8, [list(CONFIG_HEADERS), cells[0]])["updateCells"]
    assert req["rows"][1]["values"][1]["userEnteredValue"] == {"stringValue": "=SUM(A1:A10)"}
    assert req["rows"][1]["values"][6]["userEnteredValue"] == {"stringValue": "=IMPORTXML('bad')"}


def test_monthly_report_and_xlsx_equivalent_formula_logic():
    rule = Rule("CMS Deals", "Unicorn", "ID", 13, rate_source="Manual")
    report = build_report([rule], ["2026-08"], {"ID": {
        "total": {"2026-08": 101.25}, "us": {"2026-08": 5.00}
    }}, 10, "2026-10-30 UTC")
    row = report_row_for_sheet(report[0], 2, 10)
    assert row[6] == "=F2*0.1"
    assert row[7] == "=E2-G2"
    assert row[9] == '=IF(I2="","",1-I2)'
    assert row[10] == '=IF(I2="","",H2*J2)'
    assert row[11] == '=IF(I2="","",H2-K2)'
    assert row[8] == 0.13
    assert row[13] == '=IF(OR(L2="",M2=""),"",L2*(1-M2))'
    assert row[14] == '=IF(OR(L2="",N2=""),"",L2-N2)'


def test_only_selected_months_are_replaced():
    previous = [["2026-07", "CMS Deals", "A", "id-a"],
                ["2026-08", "CMS Deals", "Old client", "old-id"],
                ["2026-09", "Distributie", "C", "id-c"]]
    replacement = [["2026-08", "CMS Deals", "New client", "new-id"]]
    merged = merge_month_rows(previous, replacement, ["2026-08"])
    assert [row[2] for row in merged] == ["A", "New client", "C"]


class FakeWorksheet:
    id = 123456
    row_count = 150
    col_count = 44

    def __init__(self, report=None, config=None, summary=None):
        self.report = report or []
        self.config = config or []
        self.summary = summary or []

    def get(self, range_name, value_render_option=None):
        if range_name.startswith("A1:R"):
            return self.report
        if range_name.startswith("T1:AA"):
            return self.config
        if range_name.startswith("AC1:AR"):
            return self.summary
        raise AssertionError(f"unexpected read: {range_name}")


class FakeSpreadsheet:
    def __init__(self, ws):
        self.ws = ws
        self.requests = None

    def worksheet(self, title):
        assert title == "Client Split Forecast"
        return self.ws

    def batch_update(self, body):
        self.requests = body["requests"]


def _report(month="2026-08"):
    return build_report([Rule("CMS Deals", "A", "ID", 15, rate_source="Manual", post_intermediation_pct=100)],
                        [month], {"ID": {"total": {month: 1000}, "us": {month: 100}}},
                        10.0, "2026-10-30 UTC")


def test_fail_closed_if_target_is_an_unrelated_worksheet():
    ws = FakeWorksheet(report=[["not this app", "a totally different table"]])
    sh = FakeSpreadsheet(ws)
    with pytest.raises(ClientSplitError, match="unfamiliar detail headers"):
        publish_report(sh, "Client Split Forecast", _report(), ["2026-08"], 10, [])
    assert sh.requests is None


def test_main_sheet_is_always_rejected_as_write_target():
    sh = FakeSpreadsheet(FakeWorksheet())
    with pytest.raises(ClientSplitError, match="Only the dedicated"):
        publish_report(sh, MAIN_WORKSHEET, _report(), ["2026-08"], 10, [])
    assert sh.requests is None


def test_new_sheet_writes_only_managed_regions_and_is_atomic():
    ws = FakeWorksheet(report=[list(REPORT_HEADERS)])
    sh = FakeSpreadsheet(ws)
    configured = default_rule_rows()
    _, count = publish_report(sh, "Client Split Forecast", _report(), ["2026-08"], 10, configured)
    assert count == 1
    writes = [r["updateCells"] for r in sh.requests if "updateCells" in r]
    assert len(writes) == 3
    assert [(r["range"]["startColumnIndex"], r["range"]["endColumnIndex"])
            for r in writes] == [(0, 18), (19, 27), (28, 44)]
    assert all(r["range"]["sheetId"] == ws.id for r in writes)
    report_row = writes[0]["rows"][1]["values"]
    assert report_row[6]["userEnteredValue"]["formulaValue"] == "=F2*0.1"
    assert report_row[4]["userEnteredValue"]["numberValue"] == 1000
    assert writes[1]["rows"][0]["values"][0]["userEnteredValue"]["stringValue"] == "Config category"
    assert writes[2]["rows"][0]["values"][0]["userEnteredValue"]["stringValue"] == "Month"
    assert writes[2]["rows"][1]["values"][2]["userEnteredValue"]["stringValue"] == "A"


def test_reconcile_only_compares_existing_main_amounts(monkeypatch):
    # Fake just the legacy month parser import, avoiding Streamlit/gspread deps offline.
    monkeypatch.setitem(sys.modules, "src.sheets", types.SimpleNamespace(
        parse_month_cell_to_yyyymm=lambda value: "2026-08" if value == "Aug 2026" else None
    ))
    rows = [["Period", "Other", "Total CMS Deals", "Total Distr"],
            ["Aug 2026", "", "€1,000.00", "€100.00"]]
    report = _report()
    recon = reconcile_to_main_sheet(report, rows)
    assert len(recon) == 1
    assert recon[0]["Category"] == "CMS Deals"
    assert recon[0]["Matches"] is True
    rows[1][2] = "€1,100.00"
    recon = reconcile_to_main_sheet(report, rows)
    assert recon[0]["Matches"] is False
    assert recon[0]["Difference EUR"] == -100.0


def test_money_parser():
    assert parse_money("€1,234.56") == 1234.56
    assert parse_money("-€1,234.56") == -1234.56
    assert parse_money("€bad") is None


def test_missing_youtube_month_cannot_be_silently_treated_as_paid_zero():
    rule = Rule("CMS Deals", "Unicorn", "ID", 15.0)
    report = build_report([rule], ["2026-09"], {"ID": {"total": {}, "us": {}}},
                          10.0, "now")
    assert "NO ANALYTICS ROW" in report[0]["Status"]
    assert report[0]["Payable to client EUR"] is None
    assert summarize_by_client(report)[0]["Payable to client EUR"] is None


def test_google_sheet_url_or_id_parser():
    from src.client_split import sheet_id_from_input
    key = "A" * 25
    assert sheet_id_from_input(key) == key
    assert sheet_id_from_input(f"https://docs.google.com/spreadsheets/d/{key}/edit?gid=0#gid=0") == key
    with pytest.raises(ClientSplitError):
        sheet_id_from_input("https://example.com/spreadsheets/d/" + key)
    with pytest.raises(ClientSplitError):
        sheet_id_from_input("../bad")


def test_new_sheet_creation_only_creates_dedicated_tab(monkeypatch):
    import types
    import sys
    gspread = types.ModuleType("gspread")
    gspread_exceptions = types.ModuleType("gspread.exceptions")
    class WorksheetNotFound(Exception):
        pass
    gspread_exceptions.WorksheetNotFound = WorksheetNotFound
    gspread.exceptions = gspread_exceptions
    monkeypatch.setitem(sys.modules, "gspread", gspread)
    monkeypatch.setitem(sys.modules, "gspread.exceptions", gspread_exceptions)

    class NewSheet(FakeSpreadsheet):
        def __init__(self):
            self.created = []
            self.requests = None
        def worksheet(self, title):
            raise WorksheetNotFound(title)
        def add_worksheet(self, title, rows, cols):
            self.created.append((title, rows, cols))
            return FakeWorksheet()
    sh = NewSheet()
    _ws, count = publish_report(sh, "Client Split Forecast", _report(), ["2026-08"], 10, [])
    assert count == 1
    assert sh.created == [("Client Split Forecast", 150, 44)]
    assert all(req.get("updateCells", {}).get("range", {}).get("sheetId", 123456) == 123456
               for req in sh.requests)



def test_explicit_channel_id_lists_are_validated_and_cannot_be_duplicated():
    a = "UC" + "a" * 22
    b = "UC" + "b" * 22
    assert source_channel_ids(f"channels:{a},{b}") == (a, b)
    assert source_channel_ids("known_group") == ()
    rows = [{"Category": "Distributie", "Client": "Zurli", "CMS group ID": f"channels:{a},{b}"}]
    assert len(read_rules(rows)) == 1
    rows[0]["CMS group ID"] = a
    with pytest.raises(ClientSplitError, match="channels: prefix"):
        read_rules(rows)
    rows[0]["CMS group ID"] = f"channels:{a},{a}"
    with pytest.raises(ClientSplitError, match="distinct"):
        read_rules(rows)
    rows[0]["CMS group ID"] = f"channels:{a},fake"
    with pytest.raises(ClientSplitError, match="1–500"):
        read_rules(rows)
    rows[0]["CMS group ID"] = "channels:" + ",".join(f"UC{i:022d}" for i in range(501))
    with pytest.raises(ClientSplitError, match="1–500"):
        read_rules(rows)


def test_explicit_channel_id_queries_use_content_owner_filter_in_eur():
    class Execution:
        def execute(self, num_retries=0):
            assert num_retries == 5
            return {"rows": [["2026-08", 123.45]]}
    class FakeReports:
        def __init__(self):
            self.calls = []
        def query(self, **kwargs):
            self.calls.append(kwargs)
            return Execution()
    class FakeService:
        def __init__(self):
            self.api = FakeReports()
        def reports(self):
            return self.api
    service = FakeService()
    cfg = types.SimpleNamespace(content_owner="TEST_OWNER")
    a, b = "UC" + "a"*22, "UC" + "b"*22
    result = query_monthly_revenue_for_source(
        service, cfg, "2026-08-01", "2026-08-01", f"channels:{a},{b}", country="US"
    )
    assert result == {"2026-08": 123.45}
    call = service.api.calls[0]
    assert call["ids"] == "contentOwner==TEST_OWNER"
    assert call["filters"] == f"channel=={a},{b};country==US"
    assert call["metrics"] == "estimatedRevenue"
    assert call["dimensions"] == "month"
    assert call["currency"] == "EUR"


def test_explicit_channels_intersecting_existing_cms_group_are_flagged():
    cid = "UC" + "a" * 22
    rules = [Rule("CMS Deals", "Unicorn", "cms_group"),
             Rule("Distributie", "Zurli", f"channels:{cid}")]
    assert find_channel_overlaps(rules, {"cms_group": [cid], f"channels:{cid}": [cid]})



def test_missing_counterparty_rate_requires_review_before_bulk_application():
    contracts = [["Date", "Cedent", "Number", "Notes", "Procent HA"]]
    assets = [["Title", "Artist", "Share1", "Firm", "Share2"],
              ["One", "Artist", "15%", "Company X", ""]]
    catalog = build_rate_catalog(contracts, assets)
    assert catalog["company x"].single_rate is None
    assert any("missing" in issue.lower() for issue in catalog["company x"].issues)


def test_summary_keeps_missing_analytics_warning_explicit():
    report = build_report([Rule("Distributie", "Zurli", "GROUP_Z", 15)],
                          ["2026-09"], {"GROUP_Z": {"total": {}, "us": {}}},
                          10, "today")
    result = summarize_by_client(report)[0]
    assert "NO ANALYTICS ROW" in result["Status"]


def test_republishing_earlier_month_preserves_and_rebases_later_month():
    from src.client_split import summarize_by_client
    later = _report(month="2026-09")[0]
    old_detail = report_row_for_sheet(later,2,7.5)
    ws = FakeWorksheet(report=[list(REPORT_HEADERS),old_detail],
                       summary=[list(SUMMARY_HEADERS),[
                           later["Month"],later["Category"],later["Client"],later["CMS group ID"]]])
    sh = FakeSpreadsheet(ws)
    _ws,n=publish_report(sh,TARGET_WORKSHEET,_report("2026-08"),["2026-08"],10,[])
    assert n == 2
    rows=[r["updateCells"] for r in sh.requests if "updateCells" in r][0]["rows"]
    old=rows[2]["values"]
    # Later month's original 7.5% tax parameter remains, but references shift row 2 -> row 3.
    assert old[6]["userEnteredValue"]["formulaValue"] == "=F3*0.075"
    assert old[13]["userEnteredValue"]["formulaValue"] == '=IF(OR(L3="",M3=""),"",L3*(1-M3))'
    assert old[0]["userEnteredValue"]["stringValue"] == "2026-09"


def test_existing_unfamiliar_settings_block_publish_before_batch_write():
    ws=FakeWorksheet(report=[list(REPORT_HEADERS)],config=[["Unrelated private settings"]])
    sh=FakeSpreadsheet(ws)
    with pytest.raises(ClientSplitError,match="unfamiliar configuration headers"):
        publish_report(sh,TARGET_WORKSHEET,_report(),["2026-08"],10,[])
    assert sh.requests is None


@pytest.mark.parametrize("source", ["TEST_GROUP", "channels:" + "UC" + "a"*22])
def test_eur_query_failure_never_retries_in_default_usd(source):
    class Reports:
        calls = []
        def query(self, **kwargs):
            self.calls.append(kwargs)
            raise TypeError("currency unsupported")
    api = Reports()
    service = types.SimpleNamespace(reports=lambda: api)
    with pytest.raises(TypeError):
        query_monthly_revenue_for_source(service, types.SimpleNamespace(content_owner="OWNER"),
                                         "2026-08-01", "2026-08-31", source)
    assert len(api.calls) == 1
    assert api.calls[0]["currency"] == "EUR"


def test_missing_us_row_is_unverified_and_cannot_publish():
    report = build_report([Rule("Distributie", "Synthetic", "TEST_GROUP", 20)],
                          ["2026-08"], {"TEST_GROUP": {"total": {"2026-08": 100}, "us": {}}},
                          10, "UTC")
    assert "US REVENUE UNVERIFIED" in report[0]["Status"]
    assert report[0]["Payable to client EUR"] is None
    with pytest.raises(ClientSplitError):
        publish_report(None, TARGET_WORKSHEET, report, ["2026-08"], 10, [])


def test_subcent_inputs_are_not_rounded_before_split():
    result = calculate_split("100.009", "1.009", 13, 10, 75)
    assert result["us_withholding"] == pytest.approx(0.1009)
    assert result["client_payable"] == pytest.approx(99.9081 * 0.87)
    assert result["hahahaha_final_net"] == pytest.approx(99.9081 * 0.13 * 0.75)
