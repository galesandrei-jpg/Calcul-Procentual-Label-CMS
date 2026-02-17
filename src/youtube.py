from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

import streamlit as st
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


DEFAULT_SCOPES = [
    "https://www.googleapis.com/auth/yt-analytics.readonly",
    "https://www.googleapis.com/auth/youtube.readonly",
]


@dataclass
class YoutubeConfig:
    content_owner: str
    on_behalf_of_content_owner: Optional[str] = None
    currency: str = "EUR"


def _build_credentials_from_secrets() -> Credentials:
    yt = st.secrets.get("youtube", {})
    client_id = yt.get("client_id")
    client_secret = yt.get("client_secret")
    refresh_token = yt.get("refresh_token")
    token_uri = yt.get("token_uri", "https://oauth2.googleapis.com/token")
    scopes = yt.get("scopes", DEFAULT_SCOPES)

    if not client_id or not client_secret or not refresh_token:
        raise RuntimeError("Missing youtube.client_id / youtube.client_secret / youtube.refresh_token in secrets.")

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=token_uri,
        client_id=client_id,
        client_secret=client_secret,
        scopes=scopes,
    )

    # Always refresh to ensure access token is valid
    creds.refresh(Request())
    return creds


def build_yta_service(cfg: YoutubeConfig):
    creds = _build_credentials_from_secrets()
    # cache_discovery=False avoids occasional Streamlit Cloud caching issues
    return build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)


def list_groups(yta, cfg: YoutubeConfig) -> List[Dict[str, str]]:
    """
    Returns [{id, title}, ...] for CMS groups accessible by the authorized user.
    """
    kwargs = {"mine": True}
    # Some accounts support onBehalfOfContentOwner for groups; harmless if ignored.
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


def query_monthly_estimated_revenue(
    yta,
    cfg: YoutubeConfig,
    startDate: str,
    endDate: str,
    group_id: str,
    country: Optional[str] = None,
    currency: Optional[str] = None,
) -> Dict[str, float]:
    """
    Returns dict: {"YYYY-MM": revenue_float, ...}
    Pulls estimatedRevenue for a group, optionally filtered by country.
    Requests revenue directly in the requested currency (default EUR).
    """
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
        currency=currency_code,  # request EUR directly
    )

    # Some client versions might not accept 'currency' kwarg; fallback safely.
    try:
        resp = yta.reports().query(**kwargs).execute()
    except TypeError:
        kwargs.pop("currency", None)
        resp = yta.reports().query(**kwargs).execute()

    out: Dict[str, float] = {}
    rows = resp.get("rows", []) or []
    for month_str, revenue in rows:
        out[str(month_str)] = float(revenue or 0.0)

    return out
