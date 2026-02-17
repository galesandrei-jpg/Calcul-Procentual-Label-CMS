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
    """
    Supports your secrets layout:
      [youtube] -> content_owner / on_behalf_of_content_owner / currency
      [youtube_oauth] -> client_id / client_secret / refresh_token (+ optional token_uri/scopes)

    Also supports legacy fallback where oauth keys might be inside [youtube].
    """
    yt = st.secrets.get("youtube", {}) or {}
    yto = st.secrets.get("youtube_oauth", {}) or {}

    client_id = (yto.get("client_id") or yt.get("client_id") or "").strip()
    client_secret = (yto.get("client_secret") or yt.get("client_secret") or "").strip()
    refresh_token = (yto.get("refresh_token") or yt.get("refresh_token") or "").strip()

    token_uri = (yto.get("token_uri") or yt.get("token_uri") or "https://oauth2.googleapis.com/token").strip()
    scopes = yto.get("scopes") or yt.get("scopes") or DEFAULT_SCOPES

    if not client_id or not client_secret or not refresh_token:
        raise RuntimeError(
            "Missing OAuth secrets. Put client_id/client_secret/refresh_token in [youtube_oauth] "
            "(or inside [youtube] as fallback)."
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

    # fallback if library rejects "currency" kwarg
    try:
        resp = yta.reports().query(**kwargs).execute()
    except TypeError:
        kwargs.pop("currency", None)
        resp = yta.reports().query(**kwargs).execute()

    out: Dict[str, float] = {}
    for month_str, revenue in (resp.get("rows", []) or []):
        out[str(month_str)] = float(revenue or 0.0)
    return out
