from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

import streamlit as st
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build


YT_ANALYTICS_SCOPES = [
    # Revenue metrics require the monetary scope
    "https://www.googleapis.com/auth/yt-analytics-monetary.readonly",
]


@dataclass
class YoutubeConfig:
    content_owner: str
    # We keep this for future/manual HTTP support, but we DO NOT pass it into
    # reports.query() because some google-api-python-client discovery builds
    # reject onBehalfOfContentOwner* for youtubeAnalytics v2.
    on_behalf_of_content_owner: Optional[str] = None


def _build_credentials_from_secrets() -> Credentials:
    yts = st.secrets["youtube_oauth"]
    client_id = yts["client_id"]
    client_secret = yts["client_secret"]
    refresh_token = yts["refresh_token"]

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=YT_ANALYTICS_SCOPES,
    )
    creds.refresh(Request())
    return creds


def build_yta_service(cfg: YoutubeConfig):
    creds = _build_credentials_from_secrets()
    return build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)


def list_groups(yta, cfg: YoutubeConfig) -> List[dict]:
    """
    List YouTube Analytics groups.

    NOTE: We intentionally do NOT pass any onBehalfOfContentOwner* parameter here,
    because the discovery doc for youtubeAnalytics v2 is inconsistent across environments.
    If you need group discovery, your OAuth user must have access to the content owner.
    """
    params = {"mine": True}

    groups = []
    req = yta.groups().list(**params)
    while req is not None:
        resp = req.execute()
        for item in resp.get("items", []):
            groups.append(
                {
                    "id": item["id"],
                    "title": item["snippet"]["title"],
                    "type": item.get("contentDetails", {}).get("itemType"),
                }
            )
        req = yta.groups().list_next(req, resp)
    return groups


def query_monthly_estimated_revenue(
    yta,
    cfg: YoutubeConfig,
    startDate: str,
    endDate: str,
    group_id: str,
    country: Optional[str] = None,
) -> Dict[str, float]:
    """
    Returns mapping: YYYY-MM -> estimatedRevenue

    Query uses:
      - ids = contentOwner==<CONTENT_OWNER_ID_FROM_/owner/...>
      - dimensions = month
      - metrics = estimatedRevenue
      - filters = group=={group_id}[;country==US]

    NOTE: We do NOT pass onBehalfOfContentOwner* because the python discovery for
    reports.query() rejects it in some installations.
    """
    ids = f"contentOwner=={cfg.content_owner}"

    filters = [f"group=={group_id}"]
    if country:
        filters.append(f"country=={country}")
    filters_str = ";".join(filters)

    params = dict(
        ids=ids,
        startDate=startDate,
        endDate=endDate,
        metrics="estimatedRevenue",
        dimensions="month",
        filters=filters_str,
        sort="month",
    )

    resp = yta.reports().query(**params).execute()
    rows = resp.get("rows", []) or []

    out: Dict[str, float] = {}
    for r in rows:
        yyyymm = r[0]  # "YYYY-MM"
        val = float(r[1]) if len(r) > 1 and r[1] is not None else 0.0
        out[yyyymm] = val
    return out
