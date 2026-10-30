"""Isolated client-level CMS revenue forecast helpers.

Only the dedicated ``Client Split Forecast`` worksheet is ever written by this
module. The existing financial worksheet and the original Streamlit pages are
read-only to this feature.
"""

from __future__ import annotations

import calendar
import datetime as dt
import io
import re
import unicodedata
from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# Snapshot of the eight groups already configured in app.py (30 Oct 2026).
# Keeping the snapshot in this new module avoids importing/executing app.py.
EXISTING_CMS_GROUPS: Tuple[Tuple[str, str], ...] = (
    ("Unicorn", "WBbE5H4__OU"),
    ("BG Music", "aZ9OPuMpAD8"),
    ("Melosy", "Lx3MFo26GtM"),
    ("Huta Media", "SWO-QxXWTgU"),
    ("Magic", "Qv2RHPCUZOU"),
    ("Pine", "BXowAx3iy4k"),
    ("Malwin", "JGSzjsvdIDk"),
    ("Unicorn 2", "Lcy1MVF5Fck"),
)

CATEGORIES = ("CMS Deals", "Distributie")
TARGET_WORKSHEET = "Client Split Forecast"
MAIN_WORKSHEET = "Calcul Procentual Venituri Luna"

REPORT_HEADERS = (
    "Month", "Category", "Client", "CMS group ID", "Gross EUR",
    "US gross EUR", "US withholding EUR", "After withholding EUR",
    "HaHaHa deal %", "Client share %", "Payable to client EUR",
    "HaHaHa gross commission EUR", "Post-intermediation factor",
    "Intermediary fee EUR", "HaHaHa final net EUR", "Rate source",
    "Status", "Generated at UTC",
)
# The new page owns these regions; nothing in the original master worksheet changes.
CONFIG_START_COLUMN = 20  # T:AA
CONFIG_HEADERS = (
    "Config category", "Config client", "Config group ID", "Config HaHaHa %",
    "Config post-intermediation %", "Config contract ref", "Config notes", "Config enabled",
)
SUMMARY_START_COLUMN = 29  # AC:AR
SUMMARY_HEADERS = (
    "Month", "Category", "Client", "CMS group IDs", "Gross EUR", "US gross EUR",
    "US withholding EUR", "After withholding EUR", "HaHaHa deal %",
    "Client share %", "Payable to client EUR", "HaHaHa gross commission EUR",
    "Intermediary fee EUR", "HaHaHa final net EUR", "Status", "Generated at UTC",
)
# These are *mapping keys*, not contract percentages; sensitive rates stay in the
# private deal workbook or in user-edited configuration, never in the public repo.
CMS_RATE_SOURCE_NAMES = {
    "WBbE5H4__OU": "Unicorn",
    "aZ9OPuMpAD8": "BG Music",
    "Lx3MFo26GtM": "Melosy",
    "SWO-QxXWTgU": "Huta Media",
    "Qv2RHPCUZOU": "Magic",
    "BXowAx3iy4k": "Pine",
    # No confirmed rate row for Malwin or Unicorn 2.
}
CMS_PARTNER_ALIASES = {
    "unicorn recording ltd": "Unicorn",
    "bg music": "BG Music",
    "melosy": "Melosy",
    "huta media": "Huta Media",
    "magic": "Magic",
    "pine ent": "Pine",
    "pine entertainment": "Pine",
    "pine entertainement": "Pine",  # spelling in the source workbook
}
# The provided Soundfeed ID uses an EM DASH instead of the ASCII hyphen accepted
# by YouTube group IDs. Keep it disabled until user provides exact copy/paste.
SOUNDFEED_ID_NEEDS_CONFIRMATION = "a\u2014b-ocG_6s"

MONEY = Decimal("0.01")
PERCENT_RE = re.compile(r"^\s*(\d+(?:[\.,]\d+)?)\s*%?\s*$")
GROUP_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{20,}$")
CHANNELS_PREFIX = "channels:"
MAX_CHANNEL_IDS = 500


class ClientSplitError(ValueError):
    """An input/worksheet state requires manual correction before publishing."""


@dataclass(frozen=True)
class Rule:
    category: str
    client: str
    group_id: str
    hahahaha_pct: Optional[float] = None  # percentage points: 15 means 15%
    contract_ref: str = ""
    notes: str = ""
    enabled: bool = True
    rate_source: str = ""
    warnings: Tuple[str, ...] = ()
    post_intermediation_pct: Optional[float] = None  # % of HaHaHa commission retained after broker


@dataclass(frozen=True)
class RateCandidate:
    name: str
    percentages: Tuple[float, ...]
    sources: Tuple[str, ...]
    issues: Tuple[str, ...] = ()

    @property
    def single_rate(self) -> Optional[float]:
        return self.percentages[0] if len(self.percentages) == 1 and not self.issues else None


@dataclass(frozen=True)
class CmsHistoricalRate:
    month: str
    hahahaha_pct: float
    post_intermediation_pct: Optional[float]
    source_row: int


def sheet_id_from_input(value: str) -> str:
    """Accept a native Google Sheets ID or copy-pasted Google Sheets URL."""
    raw = str(value or "").strip()
    if raw.startswith("https://docs.google.com/spreadsheets/d/"):
        match = re.match(r"^https://docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]{20,})(?:[/?#].*)?$", raw)
        if match:
            return match.group(1)
    elif re.fullmatch(r"[A-Za-z0-9_-]{20,}", raw):
        return raw
    raise ClientSplitError("Enter a valid Google Sheets ID or docs.google.com/spreadsheets link")


def normalize_name(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.findall(r"[a-z0-9]+", text))


def is_blank(value: Any) -> bool:
    if value is None:
        return True
    s = str(value).strip().lower()
    return s in ("", "nan", "none", "<na>", "null")


def parse_percent(value: Any) -> Optional[float]:
    """Parse percentages as POINTS, rejecting narrative/conditional contracts.

    For example '15%', '15.00%' and 15 -> 15, while
    '20% 1 an / 15% dupa' intentionally resolves to None.
    """
    if is_blank(value):
        return None
    if isinstance(value, bool):
        return None
    match = PERCENT_RE.fullmatch(str(value))
    if not match:
        return None
    number = float(match.group(1).replace(",", "."))
    return number if 0 <= number <= 100 else None


def parse_money(value: Any) -> Optional[float]:
    """Parse a formatted euro amount only for read-only reconciliation."""
    if is_blank(value):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).replace("\u00a0", "").strip()
    negative = s.startswith("-") or (s.startswith("(") and s.endswith(")"))
    s = s.strip("-() ").replace("€", "").replace("EUR", "").replace(" ", "")
    if not re.fullmatch(r"\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?", s):
        return None
    number = float(s.replace(",", ""))
    return -number if negative else number


def month_sequence(first: str, last: str) -> List[str]:
    def parse(s: str) -> Tuple[int, int]:
        if not re.fullmatch(r"\d{4}-\d{2}", s):
            raise ClientSplitError("Month must be YYYY-MM")
        year, month = map(int, s.split("-"))
        if not 1 <= month <= 12:
            raise ClientSplitError("Invalid month")
        return year, month
    y, m = parse(first)
    ey, em = parse(last)
    if (y, m) > (ey, em):
        raise ClientSplitError("End month precedes start month")
    months = []
    while (y, m) <= (ey, em):
        months.append(f"{y:04d}-{m:02d}")
        if len(months) > 24:
            raise ClientSplitError("Please select at most 24 months per run")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return months


def default_rule_rows() -> List[Dict[str, Any]]:
    """Add Tibi's supplied group mappings without guessing any private rates.

    Coproduction and CID may have separate rights from Zurli's primary deal.
    A malformed Unicode dash was supplied for Soundfeed: it stays disabled.
    """
    original = [
        {"Category": "CMS Deals", "Client": name, "CMS group ID": gid,
         "HaHaHa % (manual)": None, "Post-intermediation % (manual)": None,
         "Contract reference": "", "Notes": "", "Enabled": True}
        for name, gid in EXISTING_CMS_GROUPS
    ]
    additions = [
        ("Zurli", "ZhkeOTUjhr8", "ZURLI", "Zurli — main group", True),
        ("Zurli", "a0FL5zlfU1M", "VERIFY COPRODUCTION", "Zurli + Ha co-production: verify separate economic rights and rate", True),
        ("Zurli", "DXRapvE6aOc", "VERIFY ZURLI CID", "Zurli CID SR + AT: verify contract scope and no overlap", True),
        ("Soundfeed", SOUNDFEED_ID_NEEDS_CONFIRMATION, "SOUNDFEED", "DISABLED: ID contains Unicode em dash; paste actual CMS group ID", False),
        ("LaLa Planet", "CKAXSy2vZUc", "VERIFY LALAPLANET CONTRACT", "Creator Records YT 10%, but Soundfeed assets include 15%; confirm this group scope first", True),
        ("National TV", "channels:UCEiSdg0rO7AyJ7ky_vhzMBg", "ABC PLUS MEDIA", "Explicit channel ID, NOT a CMS group", True),
        ("JUNO / Elixir", "JCbxlxAweg0", "VERIFY JUNO LEGAL ENTITY", "ELIXIR and RAKISS both show 10% but their rights differ; verify group scope", True),
        ("Alina Sorescu", "Vlp5PkaizOU", "ALINA MUSIC SHOW", "", True),
    ]
    for client, source, contract, note, enabled in additions:
        original.append({"Category": "Distributie", "Client": client,
                         "CMS group ID": source, "HaHaHa % (manual)": None,
                         "Post-intermediation % (manual)": None,
                         "Contract reference": contract, "Notes": note, "Enabled": enabled})
    return original


def _text(value: Any) -> str:
    return "" if is_blank(value) else str(value).strip()


def _enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if is_blank(value):
        return False
    return str(value).strip().casefold() not in ("false", "0", "no", "off", "unchecked")


def source_channel_ids(source_id: str) -> Tuple[str, ...]:
    """Return an explicit subset of YouTube channels, or an empty tuple for a group.

    The ``channels:UC...,UC...`` prefix is mandatory; a bare channel ID
    must not accidentally be interpreted as a group ID.
    """
    if not source_id.startswith(CHANNELS_PREFIX):
        return ()
    raw = source_id[len(CHANNELS_PREFIX):]
    ids = tuple(value.strip() for value in raw.split(","))
    if (not ids or len(ids) > MAX_CHANNEL_IDS
            or any(not CHANNEL_ID_RE.fullmatch(value) for value in ids)
            or len(ids) != len(set(ids))):
        raise ClientSplitError("Channel sources require 1–500 distinct YouTube channel IDs in channels:UC...,UC... format")
    return ids


def validate_revenue_source(source_id: str) -> bool:
    if source_id.startswith(CHANNELS_PREFIX):
        source_channel_ids(source_id)
        return True
    if CHANNEL_ID_RE.fullmatch(source_id):
        raise ClientSplitError("A bare channel ID must use the channels: prefix")
    return bool(GROUP_ID_RE.fullmatch(source_id))


def query_monthly_revenue_for_source(yta: Any, cfg: Any, start_date: str,
                                     end_date: str, source_id: str,
                                     country: Optional[str] = None) -> Dict[str, float]:
    """Fetch EUR monthly revenue for either a CMS group or listed channels.

    This never changes the legacy YouTube API helper. Explicit channel lists
    are filtered with ``channel==`` per the YouTube content-owner API.
    """
    channels = source_channel_ids(source_id)
    if not channels and not validate_revenue_source(source_id):
        raise ClientSplitError("Unrecognized CMS group/channel selection")
    filters = (["channel==" + ",".join(channels)] if channels else
               ["group==" + source_id])
    if country:
        filters.append("country==" + country)
    kwargs = dict(
        ids=f"contentOwner=={cfg.content_owner}",
        startDate=start_date, endDate=end_date,
        metrics="estimatedRevenue", dimensions="month",
        filters=";".join(filters), currency="EUR",
    )
    # Never retry without currency: YouTube defaults to USD, not EUR.
    response = yta.reports().query(**kwargs).execute(num_retries=5)
    return {str(month): float(amount or 0.0)
            for month, amount in response.get("rows", []) or []}


def read_rules(editor_rows: Sequence[Mapping[str, Any]]) -> List[Rule]:
    rules = []
    for index, row in enumerate(editor_rows, start=1):
        category = _text(row.get("Category"))
        client = _text(row.get("Client"))
        group_id = _text(row.get("CMS group ID"))
        contract_ref = _text(row.get("Contract reference"))
        manual_raw = row.get("HaHaHa % (manual)")
        intermediary_raw = row.get("Post-intermediation % (manual)")
        enabled = _enabled(row.get("Enabled", True))
        if not any((category, client, group_id, contract_ref, _text(manual_raw), _text(intermediary_raw))):
            continue
        if enabled and category not in CATEGORIES:
            raise ClientSplitError(f"Row {index}: category must be CMS Deals or Distributie")
        if enabled and not client:
            raise ClientSplitError(f"Row {index}: client name is required")
        if enabled:
            try:
                valid_source = validate_revenue_source(group_id)
            except ClientSplitError as exc:
                raise ClientSplitError(f"Row {index}: {exc}") from exc
            if not valid_source:
                raise ClientSplitError(
                    f"Row {index}: enter a CMS group ID or channels:UC123...,UC456..."
                )
        manual = parse_percent(manual_raw)
        if not is_blank(manual_raw) and manual is None:
            raise ClientSplitError(f"Row {index}: HaHaHa percentage is invalid")
        intermediary = parse_percent(intermediary_raw)
        if not is_blank(intermediary_raw) and intermediary is None:
            raise ClientSplitError(f"Row {index}: post-intermediation percentage is invalid")
        rules.append(Rule(category=category, client=client, group_id=group_id,
                          hahahaha_pct=manual, post_intermediation_pct=intermediary,
                          contract_ref=contract_ref,
                          notes=_text(row.get("Notes")), enabled=enabled))
    seen: Dict[str, str] = {}
    for rule in rules:
        if not rule.enabled:
            continue
        if rule.group_id in seen:
            raise ClientSplitError(
                f"Group {rule.group_id} is listed twice ({seen[rule.group_id]} / {rule.client}); "
                "a group cannot be counted twice."
            )
        seen[rule.group_id] = rule.client
    if not seen:
        raise ClientSplitError("Enable at least one client group to generate a report")
    return rules


def build_rate_catalog(contract_rows: Sequence[Sequence[Any]],
                       detailed_rows: Sequence[Sequence[Any]]) -> Dict[str, RateCandidate]:
    """Read two source tabs; reject conflicting, conditional, or invalid rates.

    - 'Identificare contract': B cedent, E Procent HA
    - 'Informatii Deal Distributie': D Firma contract, C Share Producer 1,
      E Share Producer 2 (verified only when both sum to 100)
    """
    aggregate: Dict[str, Dict[str, Any]] = {}

    def add(name: Any, rate: Any, source: str, issue: str = ""):
        name_str = _text(name)
        key = normalize_name(name_str)
        if not key:
            return
        item = aggregate.setdefault(key, {"name": name_str, "rates": set(),
                                          "sources": set(), "issues": set()})
        item["sources"].add(source)
        pct = parse_percent(rate)
        if isinstance(rate, (int, float)) and 0 < rate < 1:
            item["issues"].add("Ambiguous numeric fraction; format source cell as a percent")
        if pct is None:
            item["issues"].add(issue or "Invalid or conditional percentage in source")
        else:
            item["rates"].add(round(pct, 6))
        if issue:
            item["issues"].add(issue)

    for row in contract_rows[1:]:
        if len(row) >= 5 and _text(row[1]):
            add(row[1], row[4], "Identificare contract")

    for row in detailed_rows[1:]:
        if len(row) < 4 or not _text(row[3]):
            continue
        a = row[2] if len(row) > 2 else None
        b = row[4] if len(row) > 4 else None
        pct_a, pct_b = parse_percent(a), parse_percent(b)
        issue = ""
        if pct_a is not None and pct_b is None:
            issue = "Counterparty share missing on a track; verify before applying to a whole CMS group"
        elif pct_a is not None and pct_b is not None and abs(pct_a + pct_b - 100) > 0.05:
            issue = "Track shares do not add up to 100%"
        add(row[3], a, "Informatii Deal Distributie", issue)

    return {
        key: RateCandidate(name=item["name"],
                           percentages=tuple(sorted(item["rates"])),
                           sources=tuple(sorted(item["sources"])),
                           issues=tuple(sorted(item["issues"])))
        for key, item in aggregate.items()
    }



def _monthly_reference_percent(value: Any) -> Optional[float]:
    """Interpret a *percentage-formatted* Google Sheets numeric cell.

    Unlike the manually entered percentages, native Sheets returns 0.13 for
    13%. This conversion is restricted to known columns H and M of the source.
    """
    if is_blank(value) or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = float(value)
        return round(n * 100 if 0 <= n <= 1 else n, 6) if 0 <= n <= 100 else None
    s = str(value).strip()
    if s.endswith("%"):
        return parse_percent(s)
    try:
        n = float(s.replace(",", "."))
    except ValueError:
        return None
    return round(n * 100 if 0 <= n <= 1 else n, 6) if 0 <= n <= 100 else None


def build_cms_rate_history(rows: Sequence[Sequence[Any]]) -> Dict[str, Dict[str, List[CmsHistoricalRate]]]:
    """Parse 'VIETNAM Mixes Tracking' -> 'Tabel Raportari'.

    Strict column/heading validation prevents accidentally using different
    accounting columns. No rates are baked into the public repository.
    """
    headers = None
    header_row = None
    expected = {"partner": 5, "deal": 7, "month": 3, "year": 4, "broker": 12}
    for ix, raw in enumerate(rows[:12]):
        r = list(raw) + [None] * max(0, 13-len(raw))
        if (normalize_name(r[5]) == "partner" and
            normalize_name(r[7]).replace(" ", "") == "deal" and
            normalize_name(r[12]).startswith("procent ha dupa intermediere")):
            headers, header_row = r, ix
            break
    if headers is None:
        raise ClientSplitError("Tabel Raportari: expected Partner (F), Deal% (H), and Procent Ha dupa intermediere (M) headers")
    catalog: Dict[str, Dict[str, List[CmsHistoricalRate]]] = {}
    for row_ix, raw in enumerate(rows[header_row + 1:], start=header_row+2):
        row = list(raw) + [None] * max(0, 13-len(raw))
        client_key = CMS_PARTNER_ALIASES.get(normalize_name(row[5]))
        if not client_key:
            continue
        try:
            year = int(row[4]); month = int(row[3])
            if not (2015 <= year <= 2100 and 1 <= month <= 12):
                continue
        except (ValueError, TypeError):
            continue
        rate = _monthly_reference_percent(row[7])
        # Zero-rate accounting lines (for example a partner not yet eligible
        # for a payout) are not evidence of a negotiated zero-commission deal.
        # A genuine zero-rate agreement can still be entered explicitly by
        # finance in the client editor, where it remains auditable.
        if rate is None or rate == 0:
            continue
        post = _monthly_reference_percent(row[12])
        period = f"{year:04d}-{month:02d}"
        entry = CmsHistoricalRate(month=period, hahahaha_pct=rate,
                                  post_intermediation_pct=post, source_row=row_ix)
        catalog.setdefault(client_key, {}).setdefault(period, []).append(entry)
    return catalog


def build_cms_rate_history_from_xlsx(file_bytes: bytes) -> Dict[str, Dict[str, List[CmsHistoricalRate]]]:
    """Read a user-uploaded private workbook, without Google Sheets permissions.

    The workbook is held in the Streamlit session only, not added to the public
    repository or written to the generated report. The sheet contents are
    passed to the same strict parser as the live Google Sheets path.
    """
    from openpyxl import load_workbook

    if not file_bytes or len(file_bytes) > 15 * 1024 * 1024:
        raise ClientSplitError("Upload a nonempty workbook under 15 MB")
    try:
        wb = load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    except Exception as exc:
        raise ClientSplitError("Could not open uploaded Excel workbook") from exc
    try:
        if "Tabel Raportari" not in wb.sheetnames:
            raise ClientSplitError("Excel workbook is missing the 'Tabel Raportari' tab")
        ws = wb["Tabel Raportari"]
        rows = ws.iter_rows(min_row=1, max_row=min(ws.max_row or 1000, 1000),
                            min_col=1, max_col=14, values_only=True)
        result = build_cms_rate_history(list(rows))
        if not result:
            raise ClientSplitError("No recognized dated CMS rates in the uploaded workbook")
        return result
    finally:
        wb.close()


def resolve_cms_monthly_rate(rule: Rule, month: str,
                             history: Mapping[str, Mapping[str, Sequence[CmsHistoricalRate]]]) -> Rule:
    """Apply rate for the actual reporting month, with conservative carry-forward.

    Malwin and Unicorn 2 deliberately have no source mapping. Even if the
    user renames Unicorn 2 to Unicorn it cannot silently inherit that rate.
    """
    if not rule.enabled or rule.category != "CMS Deals":
        return rule
    source_name = CMS_RATE_SOURCE_NAMES.get(rule.group_id)
    if not source_name:
        return rule
    by_month = history.get(source_name, {})
    candidates = [m for m in by_month if m <= month]
    if not candidates:
        return rule
    used_month = max(candidates)
    entries = by_month[used_month]
    pairs = {(e.hahahaha_pct, e.post_intermediation_pct) for e in entries}
    if len(pairs) != 1:
        return replace(rule, hahahaha_pct=rule.hahahaha_pct,
                       warnings=tuple(dict.fromkeys((*rule.warnings,
                           "CONFLICTING MONTHLY RATE / BROKERAGE"))))
    selected = entries[-1]
    issues = list(rule.warnings)
    if used_month != month:
        issues.append(f"CARRIED FORWARD FROM {used_month}: review period")
    rate = rule.hahahaha_pct
    if rate is None:
        rate = selected.hahahaha_pct
    elif abs(rate - selected.hahahaha_pct) > 0.00001:
        issues.append("MANUAL RATE DIFFERS FROM MONTHLY REFERENCE")
    post = rule.post_intermediation_pct
    if post is None:
        post = selected.post_intermediation_pct
    elif selected.post_intermediation_pct is not None and abs(post - selected.post_intermediation_pct) > 0.00001:
        issues.append("MANUAL INTERMEDIATION DIFFERS FROM REFERENCE")
    source = f"Tabel Raportari row {selected.source_row}, {used_month}"
    if rule.hahahaha_pct is not None:
        source = f"Manual (cross-check: {source})"
    return replace(rule, hahahaha_pct=rate, post_intermediation_pct=post,
                   rate_source=source, warnings=tuple(dict.fromkeys(issues)))

def resolve_rates(rules: Sequence[Rule], catalog: Mapping[str, RateCandidate]) -> List[Rule]:
    resolved = []
    for rule in rules:
        if not rule.enabled:
            resolved.append(rule)
            continue
        warnings: List[str] = []
        selected = catalog.get(normalize_name(rule.contract_ref or
                             (rule.client if rule.category == "Distributie" else "")))
        if rule.hahahaha_pct is not None:
            if selected and (selected.single_rate is None or
                             abs(selected.single_rate - rule.hahahaha_pct) > 0.0001):
                warnings.append("Manual rate overrides conflicting/unresolved reference")
            resolved.append(replace(rule, rate_source="Manual – verify contract",
                                    warnings=tuple(warnings)))
        elif rule.category == "CMS Deals" and rule.hahahaha_pct is None:
            # A dated CMS rate will be resolved separately for each report month.
            resolved.append(replace(rule, rate_source="CMS monthly reference pending",
                                    warnings=tuple(warnings)))
        elif selected and selected.single_rate is not None:
            resolved.append(replace(rule, hahahaha_pct=selected.single_rate,
                                    rate_source=" / ".join(selected.sources),
                                    warnings=tuple(warnings)))
        else:
            reason = "No verified HaHaHa percentage"
            if selected:
                reason = "Conflicting or conditional percentages in source"
            resolved.append(replace(rule, rate_source="Unresolved",
                                    warnings=(reason,)))
    return resolved


def _money(v: Any) -> Decimal:
    return Decimal(str(v or 0))


def calculate_split(total: Any, us_total: Any, hahahaha_pct: Optional[float],
                    tax_pct: float = 10.0,
                    post_intermediation_pct: Optional[float] = 100.0) -> Dict[str, Any]:
    """Financial estimate: brokerage is paid FROM HaHaHa's own commission."""
    if not 0 <= tax_pct <= 100:
        raise ClientSplitError("Invalid US withholding percentage")
    gross = _money(total)
    us_gross = _money(us_total)
    withholding = us_gross * Decimal(str(tax_pct)) / 100
    after_tax = gross - withholding
    result: Dict[str, Any] = {
        "gross": float(gross), "us_gross": float(us_gross),
        "us_withholding": float(withholding), "after_withholding": float(after_tax),
        "client_share": None, "client_payable": None,
        "hahahaha_gross_commission": None,
        "broker_fee": None, "hahahaha_final_net": None,
    }
    if hahahaha_pct is None:
        return result
    if not 0 <= hahahaha_pct <= 100:
        raise ClientSplitError("Invalid HaHaHa percentage")
    client_pct = 100 - hahahaha_pct
    payout = after_tax * Decimal(str(client_pct)) / 100
    gross_fee = after_tax - payout
    result.update(client_share=client_pct, client_payable=float(payout),
                  hahahaha_gross_commission=float(gross_fee))
    if post_intermediation_pct is not None:
        if not 0 <= post_intermediation_pct <= 100:
            raise ClientSplitError("Invalid post-intermediation percentage")
        broker_fee = gross_fee * (100-Decimal(str(post_intermediation_pct))) / 100
        result.update(broker_fee=float(broker_fee), hahahaha_final_net=float(gross_fee-broker_fee))
    return result


def build_report(rules: Sequence[Rule], months: Sequence[str],
                 revenues: Mapping[str, Mapping[str, Mapping[str, float]]],
                 tax_pct: float, generated_at: str,
                 cms_rate_history: Optional[Mapping[str, Mapping[str, Sequence[CmsHistoricalRate]]]] = None
                 ) -> List[Dict[str, Any]]:
    """Use each month's own CMS deal rate, never the latest rate for all months."""
    report: List[Dict[str, Any]] = []
    for month in months:
        for original_rule in rules:
            if not original_rule.enabled:
                continue
            rule = resolve_cms_monthly_rate(original_rule, month, cms_rate_history or {})
            item = revenues.get(rule.group_id, {})
            total = item.get("total", {}).get(month, 0.0)
            us = item.get("us", {}).get(month, 0.0)
            # CMS deal sheets record a post-intermediation factor for each partner.
            # Unknown factor cannot be silently replaced by 100% HaHaHa retention.
            needs_broker = rule.category == "CMS Deals"
            factor = rule.post_intermediation_pct
            if factor is None and not needs_broker:
                factor = 100.0
            amounts = calculate_split(total, us, rule.hahahaha_pct, tax_pct, factor)
            flags = []
            if rule.hahahaha_pct is None:
                flags.append("CHECK RATE")
            if rule.hahahaha_pct is not None and factor is None:
                flags.append("CHECK INTERMEDIATION")
            if rule.warnings:
                flags.append("VERIFY RATE / INTERMEDIATION")
            if month not in item.get("total", {}) or month not in item.get("us", {}):
                flags.append("NO ANALYTICS ROW")
                if month not in item.get("us", {}):
                    flags.append("US REVENUE UNVERIFIED")
                amounts["client_payable"] = None
                amounts["hahahaha_gross_commission"] = None
                amounts["broker_fee"] = None
                amounts["hahahaha_final_net"] = None
            if amounts["gross"] < 0 or amounts["us_gross"] < 0 or (
                amounts["gross"] >= 0 and amounts["us_gross"] > amounts["gross"] + 0.01
            ):
                flags.append("REVIEW REVENUE")
            status = " | ".join(flags) if flags else "OK"
            report.append({
                "Month": month, "Category": rule.category, "Client": rule.client,
                "CMS group ID": rule.group_id, "Gross EUR": amounts["gross"],
                "US gross EUR": amounts["us_gross"],
                "US withholding EUR": amounts["us_withholding"],
                "After withholding EUR": amounts["after_withholding"],
                "HaHaHa deal %": (rule.hahahaha_pct / 100 if rule.hahahaha_pct is not None else None),
                "Client share %": (amounts["client_share"] / 100 if amounts["client_share"] is not None else None),
                "Payable to client EUR": amounts["client_payable"],
                "HaHaHa gross commission EUR": amounts["hahahaha_gross_commission"],
                "Post-intermediation factor": factor / 100 if factor is not None else None,
                "Intermediary fee EUR": amounts["broker_fee"],
                "HaHaHa final net EUR": amounts["hahahaha_final_net"],
                "Rate source": rule.rate_source or ("Manual – verify contract" if rule.hahahaha_pct is not None else "Unresolved"),
                "Status": status, "Generated at UTC": generated_at,
            })
    return sorted(report, key=lambda row: (row["Month"], row["Category"], row["Client"], row["CMS group ID"]))


def find_channel_overlaps(rules: Sequence[Rule],
                          memberships: Mapping[str, Iterable[str]]) -> List[Dict[str, Any]]:
    """Identify channels present in two or more configured revenue groups."""
    seen: Dict[str, List[str]] = {}
    active = {r.group_id: r for r in rules if r.enabled}
    for group_id, channels in memberships.items():
        if group_id not in active:
            continue
        for channel_id in set(channels):
            if not channel_id:
                continue
            seen.setdefault(str(channel_id), []).append(
                f"{active[group_id].category} / {active[group_id].client} ({group_id})"
            )
    return [{"Channel ID": cid, "Groups with same channel": " | ".join(groups)}
            for cid, groups in sorted(seen.items()) if len(groups) > 1]


def summarize_by_client(report: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """One row per month/client/category; disclose mixed rates and brokerage."""
    groups: Dict[Tuple[str, str, str], List[Mapping[str, Any]]] = {}
    for row in report:
        key = (str(row["Month"]), str(row["Category"]), normalize_name(row["Client"]))
        groups.setdefault(key, []).append(row)
    summary = []
    for (month, category, _), items in sorted(groups.items()):
        rates = {r["HaHaHa deal %"] for r in items if r["HaHaHa deal %"] is not None}
        missing = any(r["HaHaHa deal %"] is None or r.get("Payable to client EUR") is None
                      or r.get("HaHaHa final net EUR") is None for r in items)
        single_rate = next(iter(rates)) if len(rates) == 1 and not missing else None
        def addmoney(label):
            if missing and label in ("Payable to client EUR", "HaHaHa gross commission EUR",
                                     "Intermediary fee EUR", "HaHaHa final net EUR"):
                return None
            return float(sum((Decimal(str(r.get(label) or 0)) for r in items), Decimal(0)).quantize(
                MONEY, rounding=ROUND_HALF_UP))
        issues = sorted({flag.strip() for item in items
                         for flag in str(item.get("Status", "OK")).split("|")
                         if flag.strip() and flag.strip() != "OK"})
        status = " | ".join(issues) if issues else "OK"
        summary.append({
            "Month": month, "Category": category, "Client": items[0]["Client"],
            "CMS group IDs": ", ".join(sorted({r["CMS group ID"] for r in items})),
            "Gross EUR": addmoney("Gross EUR"), "US gross EUR": addmoney("US gross EUR"),
            "US withholding EUR": addmoney("US withholding EUR"),
            "After withholding EUR": addmoney("After withholding EUR"),
            "HaHaHa deal %": single_rate if single_rate is not None else (
                "UNVERIFIED" if missing else "MIXED"),
            "Client share %": 1 - single_rate if single_rate is not None else (
                "UNVERIFIED" if missing else "MIXED"),
            "Payable to client EUR": addmoney("Payable to client EUR"),
            "HaHaHa gross commission EUR": addmoney("HaHaHa gross commission EUR"),
            "Intermediary fee EUR": addmoney("Intermediary fee EUR"),
            "HaHaHa final net EUR": addmoney("HaHaHa final net EUR"),
            "Status": status, "Generated at UTC": items[0]["Generated at UTC"],
        })
    return summary


def report_row_for_sheet(row: Mapping[str, Any], index: int, tax_pct: float) -> List[Any]:
    """Sheet formulas separate client payout, broker fee and HaHaHa final net."""
    r = index
    return [row["Month"], row["Category"], row["Client"], row["CMS group ID"],
            row["Gross EUR"], row["US gross EUR"],
            f"=F{r}*{tax_pct / 100}", f"=E{r}-G{r}",
            row["HaHaHa deal %"], f'=IF(I{r}="","",1-I{r})',
            f'=IF(I{r}="","",H{r}*J{r})',
            f'=IF(I{r}="","",H{r}-K{r})',
            row["Post-intermediation factor"],
            f'=IF(OR(L{r}="",M{r}=""),"",L{r}*(1-M{r}))',
            f'=IF(OR(L{r}="",N{r}=""),"",L{r}-N{r})',
            row["Rate source"], row["Status"], row["Generated at UTC"]]


def merge_month_rows(existing_rows: Sequence[Sequence[Any]],
                     new_rows: Sequence[Sequence[Any]], months: Iterable[str]) -> List[List[Any]]:
    """Replace only specified months' rows, preserving all other months."""
    selected = set(months)
    kept = [list(row) for row in existing_rows if row and str(row[0]) not in selected and any(not is_blank(c) for c in row)]
    combined = kept + [list(row) for row in new_rows]
    combined.sort(key=lambda row: (str(row[0]), str(row[1]) if len(row)>1 else "",
                                  str(row[2]) if len(row)>2 else "",
                                  str(row[3]) if len(row)>3 else ""))
    return combined


def config_to_cells(editor_rows: Sequence[Mapping[str, Any]]) -> List[List[Any]]:
    cells: List[List[Any]] = []
    for row in editor_rows:
        if not any(not is_blank(v) for v in row.values()):
            continue
        cells.append([
            _text(row.get("Category")), _text(row.get("Client")),
            _text(row.get("CMS group ID")),
            _text(row.get("HaHaHa % (manual)")),
            _text(row.get("Post-intermediation % (manual)")),
            _text(row.get("Contract reference")), _text(row.get("Notes")),
            _enabled(row.get("Enabled", True)),
        ])
    return cells


def config_from_cells(rows: Sequence[Sequence[Any]]) -> List[Dict[str, Any]]:
    if not rows or list(rows[0][:len(CONFIG_HEADERS)]) != list(CONFIG_HEADERS):
        raise ClientSplitError("Existing configuration headers differ from expected layout")
    result = []
    for row in rows[1:]:
        a = list(row) + [None] * (len(CONFIG_HEADERS) - len(row))
        if not any(not is_blank(x) for x in a):
            continue
        result.append({
            "Category": _text(a[0]), "Client": _text(a[1]),
            "CMS group ID": _text(a[2]),
            "HaHaHa % (manual)": parse_percent(a[3]),
            "Post-intermediation % (manual)": parse_percent(a[4]),
            "Contract reference": _text(a[5]), "Notes": _text(a[6]),
            "Enabled": _enabled(a[7]),
        })
    return result


def _sheet_cell(value: Any, allow_formula: bool = False) -> Dict[str, Any]:
    if is_blank(value):
        return {}
    if isinstance(value, bool):
        return {"userEnteredValue": {"boolValue": value}}
    if isinstance(value, (int, float)):
        return {"userEnteredValue": {"numberValue": float(value)}}
    if allow_formula and isinstance(value, str) and value.startswith("="):
        return {"userEnteredValue": {"formulaValue": value}}
    return {"userEnteredValue": {"stringValue": str(value)}}


def _write_request(sheet_id: int, row_count: int, start_col: int, width: int,
                   rows: Sequence[Sequence[Any]]) -> Dict[str, Any]:
    # The explicit range plus fields mask clears stale cells *within our own region*.
    return {"updateCells": {
        "range": {"sheetId": sheet_id, "startRowIndex": 0,
                  "endRowIndex": row_count, "startColumnIndex": start_col,
                  "endColumnIndex": start_col + width},
        "rows": [{"values": [_sheet_cell(v, allow_formula=(start_col == 0 and i in (6, 7, 9, 10, 11, 13, 14)))
                           for i, v in enumerate(list(row)[:width])]}
                 for row in rows],
        "fields": "userEnteredValue",
    }}


def read_sheet_configuration(worksheet: Any) -> Optional[List[Dict[str, Any]]]:
    rows = worksheet.get(f"T1:AA{min(worksheet.row_count, 500)}", value_render_option="FORMATTED_VALUE")
    if not rows or not any(rows[0]):
        return None
    return config_from_cells(rows)


def publish_report(spreadsheet: Any, target_title: str,
                   report: Sequence[Mapping[str, Any]],
                   months: Sequence[str], tax_pct: float,
                   editor_rows: Sequence[Mapping[str, Any]]) -> Tuple[Any, int]:
    """Upsert only this feature's own tab, in one Google Sheets batchUpdate.

    This function never writes any old sheet; unknown data/formulas fail closed.
    The report is A:R, config is T:AA, and client summary is AC:AR.
    """
    if target_title != TARGET_WORKSHEET:
        raise ClientSplitError("Only the dedicated Client Split Forecast tab can be updated")
    if not report or any(str(row.get("Month")) not in set(months) for row in report):
        raise ClientSplitError("Report months do not match the selected months")
    if any(r.get("Payable to client EUR") is None or
                         r.get("HaHaHa final net EUR") is None or
                         "REVIEW REVENUE" in str(r.get("Status", "")) for r in report):
        raise ClientSplitError("Incomplete client payouts or invalid revenue cannot be published")
    try:
        ws = spreadsheet.worksheet(target_title)
        existed = True
    except Exception as exc:
        try:
            from gspread.exceptions import WorksheetNotFound
        except ImportError:
            raise
        if not isinstance(exc, WorksheetNotFound):
            raise
        ws = spreadsheet.add_worksheet(title=target_title, rows=150, cols=44)
        existed = False

    old_report = ws.get(f"A1:R{ws.row_count}", value_render_option="FORMULA") if existed else []
    # Read even partially occupied regions. If a 22-column legacy-sized tab
    # contains unrelated text at T:W, we must not append and overwrite it.
    def column_letter(index: int) -> str:
        result = ""
        while index:
            index, remainder = divmod(index-1, 26)
            result = chr(65+remainder) + result
        return result
    old_config = (ws.get(f"T1:{column_letter(min(ws.col_count, 27))}{min(ws.row_count, 500)}",
                         value_render_option="FORMATTED_VALUE")
                  if existed and ws.col_count >= 20 else [])
    old_summary = (ws.get(f"AC1:{column_letter(min(ws.col_count, 44))}{ws.row_count}",
                          value_render_option="UNFORMATTED_VALUE")
                   if existed and ws.col_count >= 29 else [])

    for old, expected, label in ((old_report, REPORT_HEADERS, "detail"),
                                 (old_config, CONFIG_HEADERS, "configuration"),
                                 (old_summary, SUMMARY_HEADERS, "summary")):
        if old and any(not is_blank(v) for row in old for v in row):
            if list(old[0][:len(expected)]) != list(expected):
                raise ClientSplitError(f"Target tab contains unfamiliar {label} headers. No cells changed.")
    previous = old_report[1:] if old_report else []
    for old_row in previous:
        if any(not is_blank(c) for c in old_row) and not re.fullmatch(r"\d{4}-\d{2}", str(old_row[0])):
            raise ClientSplitError("Unknown previous-month report row; refusing to overwrite")
    new_for_merge = [[r.get(h, "") for h in REPORT_HEADERS] for r in report]
    combined = merge_month_rows(previous, new_for_merge, months)
    output = [list(REPORT_HEADERS)]
    for row_number, item in enumerate(combined, start=2):
        row = list(item) + [None] * max(0, len(REPORT_HEADERS) - len(item))
        data = dict(zip(REPORT_HEADERS, row))
        if str(data["Month"]) in months:
            output.append(report_row_for_sheet(data, row_number, tax_pct))
            continue
        # Rebase only formulas created by this module. Preserve each old month's
        # original tax percentage rather than replacing it with this run's rate.
        old_formula = data.get("US withholding EUR")
        m = re.fullmatch(r"=F\d+\*([0-9.]+)", str(old_formula))
        if not m:
            raise ClientSplitError("Unknown older-month withholding formula; aborting safe rewrite")
        old_tax_pct = float(m.group(1)) * 100
        known_formulas = {
            7: r"=E\d+-G\d+",
            9: r'=IF\(I\d+="","",1-I\d+\)',
            10: r'=IF\(I\d+="","",H\d+\*J\d+\)',
            11: r'=IF\(I\d+="","",H\d+-K\d+\)',
            13: r'=IF\(OR\(L\d+="",M\d+=""\),"",L\d+\*\(1-M\d+\)\)',
            14: r'=IF\(OR\(L\d+="",N\d+=""\),"",L\d+-N\d+\)',
        }
        for idx, pattern in known_formulas.items():
            if not re.fullmatch(pattern, str(row[idx])):
                raise ClientSplitError("Unknown formula structure in older-month report; aborting")
        data["HaHaHa deal %"] = float(data["HaHaHa deal %"]) if not is_blank(data["HaHaHa deal %"]) else None
        data["Post-intermediation factor"] = (float(data["Post-intermediation factor"])
                                                if not is_blank(data["Post-intermediation factor"]) else None)
        output.append(report_row_for_sheet(data, row_number, old_tax_pct))

    config = [list(CONFIG_HEADERS)] + config_to_cells(editor_rows)
    new_summary = [[r.get(h, "") for h in SUMMARY_HEADERS] for r in summarize_by_client(report)]
    previous_summary = old_summary[1:] if old_summary else []
    for row in previous_summary:
        if any(not is_blank(c) for c in row) and not re.fullmatch(r"\d{4}-\d{2}", str(row[0])):
            raise ClientSplitError("Unknown client-summary row; refusing to overwrite")
    summary_output = [list(SUMMARY_HEADERS)] + merge_month_rows(previous_summary, new_summary, months)

    count = max(len(old_report), len(output), len(old_config), len(config),
                len(old_summary), len(summary_output), 1)
    reqs: List[Dict[str, Any]] = []
    if ws.row_count < count:
        reqs.append({"appendDimension": {"sheetId": ws.id, "dimension": "ROWS",
                                         "length": count-ws.row_count}})
    target_cols = max(CONFIG_START_COLUMN-1+len(CONFIG_HEADERS),
                      SUMMARY_START_COLUMN-1+len(SUMMARY_HEADERS))
    if ws.col_count < target_cols:
        reqs.append({"appendDimension": {"sheetId": ws.id, "dimension": "COLUMNS",
                                         "length": target_cols-ws.col_count}})
    reqs.append(_write_request(ws.id, max(len(old_report), len(output)), 0,
                               len(REPORT_HEADERS), output))
    reqs.append(_write_request(ws.id, max(len(old_config), len(config)),
                               CONFIG_START_COLUMN-1, len(CONFIG_HEADERS), config))
    reqs.append(_write_request(ws.id, max(len(old_summary), len(summary_output)),
                               SUMMARY_START_COLUMN-1, len(SUMMARY_HEADERS), summary_output))
    reqs.append({"updateSheetProperties": {
        "properties": {"sheetId": ws.id, "gridProperties": {"frozenRowCount": 1}},
        "fields": "gridProperties.frozenRowCount"}})

    for first, last, rgb in ((0, len(REPORT_HEADERS), (0.12, 0.22, 0.37)),
                             (CONFIG_START_COLUMN-1, CONFIG_START_COLUMN-1+len(CONFIG_HEADERS), (0.20, 0.31, 0.24)),
                             (SUMMARY_START_COLUMN-1, target_cols, (0.12, 0.22, 0.37))):
        reqs.append({"repeatCell": {"range": {
            "sheetId": ws.id, "startRowIndex": 0, "endRowIndex": 1,
            "startColumnIndex": first, "endColumnIndex": last},
            "cell": {"userEnteredFormat": {
                "backgroundColor": dict(zip(("red", "green", "blue"), rgb)),
                "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}}}},
            "fields": "userEnteredFormat.backgroundColor,userEnteredFormat.textFormat"}})
    def number_fmt(begin, end, row_count, kind):
        return {"repeatCell": {"range": {"sheetId": ws.id, "startRowIndex": 1,
            "endRowIndex": max(row_count, 2), "startColumnIndex": begin,
            "endColumnIndex": end}, "cell": {"userEnteredFormat": {"numberFormat": {
            "type": kind, "pattern": '0.00%' if kind == 'PERCENT' else '€#,##0.00'}}},
            "fields": "userEnteredFormat.numberFormat"}}
    for begin, end, kind in ((4, 8, "CURRENCY"), (8, 10, "PERCENT"),
                            (10, 12, "CURRENCY"), (12, 13, "PERCENT"), (13, 15, "CURRENCY")):
        reqs.append(number_fmt(begin, end, len(output), kind))
    off = SUMMARY_START_COLUMN-1
    for begin, end, kind in ((4, 8, "CURRENCY"), (8, 10, "PERCENT"),
                            (10, 14, "CURRENCY")):
        reqs.append(number_fmt(off+begin, off+end, len(summary_output), kind))
    spreadsheet.batch_update({"requests": reqs})
    return ws, len(combined)


def reconcile_to_main_sheet(report: Sequence[Mapping[str, Any]],
                            main_rows: Sequence[Sequence[Any]]) -> List[Dict[str, Any]]:
    """Compare gross monthly totals against main sheet K and N (read-only)."""
    if not main_rows:
        return []
    from src.sheets import parse_month_cell_to_yyyymm
    hdr = {str(v).strip(): ix for ix, v in enumerate(main_rows[0])}
    if "Total CMS Deals" not in hdr or "Total Distr" not in hdr:
        return []
    actual: Dict[Tuple[str, str], Decimal] = {}
    for r in report:
        key = (str(r["Month"]), str(r["Category"]))
        actual[key] = actual.get(key, Decimal("0")) + Decimal(str(r["Gross EUR"]))
    result = []
    for row in main_rows[1:]:
        if not row:
            continue
        month = parse_month_cell_to_yyyymm(row[0])
        if not month:
            continue
        for category, heading in (("CMS Deals", "Total CMS Deals"),
                                  ("Distributie", "Total Distr")):
            key = (month, category)
            if key not in actual:
                continue
            idx = hdr[heading]
            expected = parse_money(row[idx] if idx < len(row) else None)
            if expected is None:
                continue
            difference = float((actual[key] - Decimal(str(expected))).quantize(MONEY,
                               rounding=ROUND_HALF_UP))
            result.append({"Month": month, "Category": category,
                           "New breakdown EUR": float(actual[key]),
                           "Existing total EUR": expected, "Difference EUR": difference,
                           "Matches": abs(difference) <= 0.05})
    return sorted(result, key=lambda r: (r["Month"], r["Category"]))
