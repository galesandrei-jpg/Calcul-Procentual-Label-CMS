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


def _build_credentials_from_secrets() -> Credentials:
    yto = st.secrets.get("youtube_oauth", {}) or {}
    client_id = yto.get("client_id")
    client_secret = yto.get("client_secret")
    refresh_token = yto.get("refresh_token")
    token_uri = yto.get("token_uri", "https://oauth2.googleapis.com/token")
    scopes = yto.get("scopes", DEFAULT_SCOPES)

    if not client_id or not client_secret or not refresh_token:
        raise RuntimeError(
            "Missing youtube_oauth.client_id / youtube_oauth.client_secret / youtube_oauth.refresh_token in secrets."
        )

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=token_uri,
        client_id=client_id,
        client_secret=client_secret,
        scopes=scopes,
    )
    creds.refresh(Request())
    return creds


def build_yta_service(cfg: YoutubeConfig):
    creds = _build_credentials_from_secrets()
    return build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)


def list_groups(yta, cfg: YoutubeConfig) -> List[Dict[str, str]]:
    kwargs = {"mine": True}
    if cfg.on_behalf_of_content_owner:
        kwargs["onBehalfOfContentOwner"] = cfg.on_behalf_of_content_owner

    resp = yta.groups().list(**kwargs).execute()
    items = resp.get("items", []) or []

    out: List[Dict[str, str]] = []
    for it in items:
        gid = it.get("id", "")
        title = (it.get("snippet", {}) or {}).get("title") or gid
        out.append({"id": gid, "title": title})
    return out


def query_monthly_estimated_revenue(
    yta,
    cfg: YoutubeConfig,
    startDate: str,
    endDate: str,
    group_id: str,
    country: Optional[str] = None,
) -> Dict[str, float]:
    filters = [f"group=={group_id}"]
    if country:
        filters.append(f"country=={country}")

    resp = (
        yta.reports()
        .query(
            ids=f"contentOwner=={cfg.content_owner}",
            startDate=startDate,
            endDate=endDate,
            metrics="estimatedRevenue",
            dimensions="month",
            filters=";".join(filters),
        )
        .execute()
    )

    out: Dict[str, float] = {}
    for month_str, revenue in (resp.get("rows", []) or []):
        out[str(month_str)] = float(revenue or 0.0)
    return out
