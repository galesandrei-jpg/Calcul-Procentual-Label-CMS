def query_channel_revenue_for_period(
    yta,
    cfg: YoutubeConfig,
    start_date: str,
    end_date: str,
    group_id: str,
    country: Optional[str] = None,
) -> Dict[str, Dict[str, float]]:
    """
    Query per-channel revenue aggregated over an arbitrary date range within a group.

    Args:
        start_date: ISO date string (YYYY-MM-DD), inclusive
        end_date:   ISO date string (YYYY-MM-DD), inclusive
        group_id:   YouTube channel group ID
        country:    Optional ISO country code to filter by (e.g. "US")

    Returns dict: {channel_id: {metric_name: value}}

    Metrics returned (normalized keys):
      - estimatedRevenue
      - estimatedAdRevenue
      - estimatedRedPartnerRevenue

    If country is specified, filters to that country only.
    Falls back to 2 metrics if the 3-metric query fails, computing
    Premium = Revenue - Ad.
    """
    import time
    from googleapiclient.errors import HttpError

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
    for cid, metrics_dict in result.items():
        rev = metrics_dict.get("estimatedRevenue", 0.0)
        ad = metrics_dict.get("estimatedAdRevenue", 0.0)
        metrics_dict["estimatedRedPartnerRevenue"] = rev - ad
    return result
