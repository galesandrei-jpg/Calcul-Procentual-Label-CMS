from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import streamlit as st
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


@dataclass
class YoutubeConfig:
    content_owner: str
    on_behalf_of_content_owner: Optional[str] = None
    currency: str = "EUR"


def _build_credentials_from_secrets() -> Credentials:
    """
    Reads OAuth from:
      [youtube_oauth] client_id / client_secret / refresh_token / token_uri (optional)
    Does NOT pass scopes into Credentials() to avoid invalid_scope on refresh.
    """
    yto = st.secrets.get("youtube_oauth", {}) or {}
    yt = st.secrets.get("youtube", {}) or {}  # fallback if needed

    client_id = (yto.get("client_id") or yt.get("client_id") or "").strip()
    client_secret = (yto.get("client_secret") or yt.get("client_secret") or "").strip()
    refresh_token = (yto.get("refresh_token") or yt.get("refresh_token") or "").strip()
    token_uri = (yto.get("token_uri") or yt.get("token_uri") or "https://oauth2.googleapis.com/token").strip()

    if not client_id or not client_secret or not refresh_token:
        raise RuntimeError(
            "Missing OAuth secrets. Put client_id/client_secret/refresh_token in [youtube_oauth]."
        )

    # KEY FIX: no scopes passed here -> avoids invalid_scope on refresh
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=token_uri,
        client_id=client_id,
        client_secret=client_secret,
    )

    creds.refresh(Request())
    return creds


def build_yta_service(cfg: YoutubeConfig):
    creds = _build_credentials_from_secrets()
    return build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)


def build_youtube_data_service():
    """Build a YouTube Data API v3 service (for channel titles etc.)."""
    creds = _build_credentials_from_secrets()
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def list_groups(yta, cfg: YoutubeConfig) -> List[Dict[str, str]]:
    kwargs = {"mine": True}
    if cfg.on_behalf_of_content_owner:
        kwargs["onBehalfOfContentOwner"] = cfg.on_behalf_of_content_owner

    resp = yta.groups().list(**kwargs).execute()
    items = resp.get("items", []) or []

    out: List[Dict[str, str]] = []
    for it in items:
        gid = it.get("id", "")
        title = (it.get("snippet", {}) or {}).get("title") or it.get("title") or gid
        out.append({"id": gid, "title": title})
    return out


def list_group_items(yta, cfg: YoutubeConfig, group_id: str) -> List[Dict[str, str]]:
    """
    List all items (channels) in a YouTube Analytics group.
    Returns list of dicts with 'channelId' key.
    Uses pagination to get all items.
    Tries without onBehalfOfContentOwner first (matches working pattern),
    falls back to including it if needed.
    """
    all_items: List[Dict[str, str]] = []
    page_token = None

    # Try without onBehalfOfContentOwner first
    while True:
        kwargs = {"groupId": group_id}
        if page_token:
            kwargs["pageToken"] = page_token

        try:
            resp = yta.groupItems().list(**kwargs).execute()
        except Exception:
            # If it fails, try with onBehalfOfContentOwner
            if cfg.on_behalf_of_content_owner:
                kwargs["onBehalfOfContentOwner"] = cfg.on_behalf_of_content_owner
                resp = yta.groupItems().list(**kwargs).execute()
            else:
                raise

        items = resp.get("items", []) or []

        for it in items:
            resource = it.get("resource", {}) or {}
            channel_id = resource.get("id", "")
            if channel_id:
                all_items.append({"channelId": channel_id})

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return all_items


def get_channel_titles(channel_ids: List[str]) -> Dict[str, str]:
    """
    Fetch channel titles from YouTube Data API v3 for a list of channel IDs.
    Returns dict: {channel_id: channel_title}
    Handles batching (max 50 per request).
    """
    yt_data = build_youtube_data_service()
    titles: Dict[str, str] = {}

    # YouTube Data API allows up to 50 IDs per request
    batch_size = 50
    for i in range(0, len(channel_ids), batch_size):
        batch = channel_ids[i : i + batch_size]
        ids_str = ",".join(batch)

        resp = yt_data.channels().list(
            part="snippet",
            id=ids_str,
            maxResults=batch_size,
        ).execute()

        for item in resp.get("items", []):
            cid = item.get("id", "")
            title = (item.get("snippet", {}) or {}).get("title", cid)
            titles[cid] = title

    return titles


def query_channel_revenue_for_month(
    yta,
    cfg: YoutubeConfig,
    year: int,
    month: int,
    group_id: str,
    country: Optional[str] = None,
) -> Dict[str, Dict[str, float]]:
    """
    Query per-channel revenue for a single month within a group.

    Returns dict: {channel_id: {metric_name: value}}

    Metrics returned (normalized keys):
    - estimatedRevenue
    - estimatedAdRevenue
    - estimatedRedPartnerRevenue

    If country is specified, filters to that country only.
    Falls back to 2 metrics if 3-metric query fails, computing Premium = Revenue - Ad.
    """
    import datetime as dt
    import calendar
    import time
    from googleapiclient.errors import HttpError

    start_date = dt.date(year, month, 1).isoformat()
    # End date must be the last day of the month for channel dimension queries
    last_day = calendar.monthrange(year, month)[1]
    end_date = dt.date(year, month, last_day).isoformat()

    filters = [f"group=={group_id}"]
    if country:
        filters.append(f"country=={country}")
    filters_str = ";".join(filters)

    def _do_query(metrics_str: str) -> dict:
        kwargs = dict(
            ids=f"contentOwner=={cfg.content_owner}",
            startDate=start_date,
            endDate=end_date,
            metrics=metrics_str,
            dimensions="channel",
            filters=filters_str,
            currency="USD",
        )
        # Retry up to 3 times for 500 errors
        for attempt in range(3):
            try:
                return yta.reports().query(**kwargs).execute()
            except HttpError as e:
                if e.resp.status == 500 and attempt < 2:
                    time.sleep(2 * (attempt + 1))  # 2s, 4s backoff
                    continue
                # On last attempt or non-500, try without currency
                if "currency" in kwargs:
                    kwargs.pop("currency")
                    try:
                        return yta.reports().query(**kwargs).execute()
                    except HttpError:
                        if attempt < 2:
                            time.sleep(2 * (attempt + 1))
                            continue
                        raise
                raise
            except TypeError:
                kwargs.pop("currency", None)
                return yta.reports().query(**kwargs).execute()

    def _parse_response(resp: dict) -> Dict[str, Dict[str, float]]:
        column_headers = [h.get("name", "") for h in (resp.get("columnHeaders", []) or [])]
        rows = resp.get("rows", []) or []
        result: Dict[str, Dict[str, float]] = {}
        for row in rows:
            channel_id = str(row[0])
            metrics_dict = {}
            for idx, header in enumerate(column_headers):
                if idx == 0:
                    continue
                metrics_dict[header] = float(row[idx] or 0.0)
            result[channel_id] = metrics_dict
        return result

    # Try with all 3 metrics first
    try:
        resp = _do_query("estimatedRevenue,estimatedAdRevenue,estimatedRedPartnerRevenue")
        return _parse_response(resp)
    except HttpError:
        pass

    # Fallback: query with just 2 metrics, compute Premium = Revenue - Ad
    resp = _do_query("estimatedRevenue,estimatedAdRevenue")
    result = _parse_response(resp)

    # Add computed estimatedRedPartnerRevenue
    for cid, metrics_dict in result.items():
        rev = metrics_dict.get("estimatedRevenue", 0.0)
        ad = metrics_dict.get("estimatedAdRevenue", 0.0)
        metrics_dict["estimatedRedPartnerRevenue"] = rev - ad

    return result


def query_monthly_estimated_revenue(
    yta,
    cfg: YoutubeConfig,
    startDate: str,
    endDate: str,
    group_id: str,
    country: Optional[str] = None,
    currency: Optional[str] = None,
) -> Dict[str, float]:
    currency_code = (currency or cfg.currency or "EUR").upper()

    filters = [f"group=={group_id}"]
    if country:
        filters.append(f"country=={country}")
    filters_str = ";".join(filters)

    kwargs = dict(
        ids=f"contentOwner=={cfg.content_owner}",
        startDate=startDate,
        endDate=endDate,
        metrics="estimatedRevenue",
        dimensions="month",
        filters=filters_str,
        currency=currency_code,
    )

    try:
        resp = yta.reports().query(**kwargs).execute()
    except TypeError:
        kwargs.pop("currency", None)
        resp = yta.reports().query(**kwargs).execute()

    out: Dict[str, float] = {}
    for month_str, revenue in (resp.get("rows", []) or []):
        out[str(month_str)] = float(revenue or 0.0)
    return out


def query_monthly_total_cms_revenue(
    yta,
    cfg: YoutubeConfig,
    startDate: str,
    endDate: str,
    country: Optional[str] = None,
    currency: Optional[str] = None,
) -> Dict[str, float]:
    """
    Query total CMS revenue per month across the entire content owner,
    WITHOUT any group filter. Optionally filtered by country.
    Returns {YYYY-MM: revenue}.
    """
    currency_code = (currency or cfg.currency or "EUR").upper()

    filters_list = []
    if country:
        filters_list.append(f"country=={country}")

    kwargs = dict(
        ids=f"contentOwner=={cfg.content_owner}",
        startDate=startDate,
        endDate=endDate,
        metrics="estimatedRevenue",
        dimensions="month",
        currency=currency_code,
    )
    if filters_list:
        kwargs["filters"] = ";".join(filters_list)

    try:
        resp = yta.reports().query(**kwargs).execute()
    except TypeError:
        kwargs.pop("currency", None)
        resp = yta.reports().query(**kwargs).execute()

    out: Dict[str, float] = {}
    for month_str, revenue in (resp.get("rows", []) or []):
        out[str(month_str)] = float(revenue or 0.0)
    return out
