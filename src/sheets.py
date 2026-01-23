from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import streamlit as st
import gspread
from gspread.utils import rowcol_to_a1
from google.oauth2.service_account import Credentials as ServiceAccountCredentials


SHEETS_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


@dataclass
class SheetConfig:
    sheet_id: str
    worksheet_name: str


# Month parsing (Romanian + English)
_MONTHS = {
    # Romanian
    "ianuarie": 1, "ian": 1,
    "februarie": 2, "feb": 2,
    "martie": 3, "mar": 3,
    "aprilie": 4, "apr": 4,
    "mai": 5,
    "iunie": 6, "iun": 6,
    "iulie": 7, "iul": 7,
    "august": 8, "aug": 8,
    "septembrie": 9, "sept": 9, "sep": 9,
    "octombrie": 10, "oct": 10,
    "noiembrie": 11, "noi": 11, "nov": 11,
    "decembrie": 12, "dec": 12,

    # English
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}


def _sa_client():
    sa_info = dict(st.secrets["gcp_service_account"])
    creds = ServiceAccountCredentials.from_service_account_info(sa_info, scopes=SHEETS_SCOPES)
    return gspread.authorize(creds)


def open_sheet(cfg: SheetConfig):
    gc = _sa_client()
    sh = gc.open_by_key(cfg.sheet_id)
    return sh.worksheet(cfg.worksheet_name)


def find_header_columns(headers: List[str]) -> Dict[str, int]:
    """Build header -> column index (1-based)."""
    out: Dict[str, int] = {}
    for idx, h in enumerate(headers, start=1):
        if h is None:
            continue
        h2 = str(h).strip()
        if h2:
            out[h2] = idx
    return out


def parse_month_cell_to_yyyymm(cell_value: str) -> Optional[str]:
    """
    Parse a Google Sheets cell into YYYY-MM.

    Supports:
      - "2025-01-01" / "2025-01-01 00:00:00"
      - "01/01/2025" (dd/mm or mm/dd heuristic)
      - "Jan 2025" / "Sept 2025"
      - "Mai 2025" / "Iunie 2025"
      - "2025-01"
    """
    if cell_value is None:
        return None
    s = str(cell_value).strip()
    if not s:
        return None

    # ISO-like date
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}"

    # Slash dates: dd/mm/yyyy OR mm/dd/yyyy (handle both safely)
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        a = int(m.group(1))
        b = int(m.group(2))
        yy = int(m.group(3))

        # Heuristic:
        # - if a > 12 => dd/mm
        # - elif b > 12 => mm/dd
        # - else assume mm/dd (common in Sheets US formatting)
        if a > 12:
            mm = b
        elif b > 12:
            mm = a
        else:
            mm = a

        return f"{yy:04d}-{mm:02d}"

    # Month name + year (English or Romanian)
    parts = re.split(r"\s+", s.lower())
    if len(parts) >= 2 and parts[-1].isdigit():
        year = int(parts[-1])
        month_name = " ".join(parts[:-1]).strip()
        # remove punctuation and diacritics-like chars are kept as word chars by \w for most locales
        month_name = re.sub(r"[^\w]+", "", month_name)

        if month_name in _MONTHS:
            mm = _MONTHS[month_name]
            return f"{year:04d}-{mm:02d}"

    # Already YYYY-MM
    m = re.match(r"^(\d{4})-(\d{2})$", s)
    if m:
        return s

    return None


def build_month_row_index(ws) -> Dict[str, int]:
    """Reads column A and returns mapping YYYY-MM -> row index."""
    colA = ws.col_values(1)
    out: Dict[str, int] = {}
    for i, v in enumerate(colA, start=1):
        if i == 1:
            continue  # header row
        yyyymm = parse_month_cell_to_yyyymm(v)
        if yyyymm:
            out[yyyymm] = i
    return out


def batch_write_values(ws, updates: List[Tuple[int, int, float]]):
    """updates: list of (row, col, value). Uses a single batch_update."""
    if not updates:
        return

    data = []
    for row, col, value in updates:
        a1 = rowcol_to_a1(row, col)
        data.append({"range": a1, "values": [[value]]})

    ws.batch_update(data, value_input_option="USER_ENTERED")


def ensure_month_rows(ws, months: List[str]) -> Dict[str, int]:
    """
    Ensure every YYYY-MM in `months` exists as a row in column A.
    If missing, insert a new row in chronological position (based on YYYY-MM order),
    writing the month as YYYY-MM-01 in column A.

    Returns updated mapping: YYYY-MM -> row index.
    """
    if not months:
        return build_month_row_index(ws)

    # Read existing months in column A (skip header row)
    colA = ws.col_values(1)
    existing: List[Tuple[int, str]] = []  # (row_index, yyyymm)

    for i, v in enumerate(colA, start=1):
        if i == 1:
            continue
        yyyymm = parse_month_cell_to_yyyymm(v)
        if yyyymm:
            existing.append((i, yyyymm))

    existing_months = {m for _, m in existing}
    target_months = sorted(set(months))
    missing = [m for m in target_months if m not in existing_months]
    if not missing:
        return build_month_row_index(ws)

    # Keep a live sorted list by month key
    existing_sorted = sorted(existing, key=lambda x: x[1])

    def insert_row_index_for_month(target: str) -> int:
        # Insert before the first row whose month is > target
        for row_idx, m in existing_sorted:
            if m > target:
                return row_idx
        # Else append after last known month row
        if existing_sorted:
            return existing_sorted[-1][0] + 1
        # If no months exist at all, place first month at row 2
        return 2

    for m in missing:
        insert_at = insert_row_index_for_month(m)
        date_str = f"{m}-01"  # put date in column A

        # Insert row (this shifts all rows >= insert_at down by 1)
        ws.insert_row([date_str], index=insert_at, value_input_option="USER_ENTERED")

        # Shift tracked indices
        shifted: List[Tuple[int, str]] = []
        for row_idx, mm in existing_sorted:
            if row_idx >= insert_at:
                shifted.append((row_idx + 1, mm))
            else:
                shifted.append((row_idx, mm))
        existing_sorted = shifted

        # Add inserted month
        existing_sorted.append((insert_at, m))
        existing_sorted.sort(key=lambda x: x[1])

    # Return fresh mapping (ground truth)
    return build_month_row_index(ws)
