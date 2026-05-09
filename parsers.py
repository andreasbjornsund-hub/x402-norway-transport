"""Parsers for Entur GraphQL responses.

Pure functions over plain dicts — easy to test without httpx.
Tolerant of partial responses (Entur omits fields rather than nulling them).
"""
from datetime import datetime, timezone


# Entur transportMode values mapped to our friendlier vocab.
_MODE_MAP = {
    "rail": "train",
    "bus": "bus",
    "tram": "tram",
    "metro": "metro",
    "water": "ferry",
    "air": "air",
    "coach": "coach",
    "funicular": "funicular",
    "lift": "lift",
    "cableway": "cableway",
    "foot": "walk",
    "bicycle": "bike",
    "scooter": "scooter",
}


def _norm_mode(mode: str | None) -> str:
    if not mode:
        return ""
    return _MODE_MAP.get(mode.lower(), mode.lower())


def _iso_to_minutes(start: str, end: str) -> int | None:
    """Minutes between two ISO8601 strings; tolerates 'Z' suffix."""
    try:
        a = datetime.fromisoformat(start.replace("Z", "+00:00"))
        b = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError):
        return None
    return int((b - a).total_seconds() // 60)


def _minutes_diff(aimed: str | None, expected: str | None) -> int | None:
    if not aimed or not expected:
        return None
    try:
        a = datetime.fromisoformat(aimed.replace("Z", "+00:00"))
        e = datetime.fromisoformat(expected.replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError):
        return None
    return int((e - a).total_seconds() // 60)


# ── Geocoder ────────────────────────────────────────────────────────


def parse_geocoder_results(data: dict, limit: int = 10) -> list[dict]:
    """Flatten geocoder GeoJSON-ish features into simple dicts."""
    if not isinstance(data, dict):
        return []
    features = data.get("features", []) or []
    out: list[dict] = []
    for f in features[:limit]:
        if not isinstance(f, dict):
            continue
        props = f.get("properties", {}) or {}
        geom = f.get("geometry", {}) or {}
        coords = geom.get("coordinates", []) or []
        out.append({
            "id": props.get("id", ""),
            "name": props.get("name", ""),
            "label": props.get("label", ""),
            "category": props.get("category", []),
            "municipality": props.get("locality", "") or props.get("borough", ""),
            "county": props.get("county", ""),
            "lat": coords[1] if len(coords) > 1 else None,
            "lon": coords[0] if len(coords) > 0 else None,
        })
    return out


def best_geocoder_match(data: dict) -> dict | None:
    rows = parse_geocoder_results(data, limit=1)
    return rows[0] if rows else None


# ── Trip / journey ──────────────────────────────────────────────────


def parse_trip_legs(legs_raw: list) -> list[dict]:
    out: list[dict] = []
    for leg in legs_raw or []:
        if not isinstance(leg, dict):
            continue
        from_place = (leg.get("fromPlace") or {})
        to_place = (leg.get("toPlace") or {})
        line = leg.get("line") or {}
        authority = leg.get("authority") or {}
        start = leg.get("expectedStartTime") or leg.get("aimedStartTime") or ""
        end = leg.get("expectedEndTime") or leg.get("aimedEndTime") or ""
        out.append({
            "mode": _norm_mode(leg.get("mode")),
            "from_stop": from_place.get("name", ""),
            "to_stop": to_place.get("name", ""),
            "line": line.get("publicCode") or line.get("name") or "",
            "line_full_name": line.get("name", ""),
            "authority": authority.get("name", ""),
            "departure": start,
            "arrival": end,
            "duration_min": (
                int(leg["duration"] // 60) if isinstance(leg.get("duration"), (int, float))
                else _iso_to_minutes(start, end)
            ),
            "distance_m": (
                int(leg["distance"]) if isinstance(leg.get("distance"), (int, float))
                else None
            ),
        })
    return out


def parse_trip(data: dict, from_label: str, to_label: str, departure_label: str | None) -> dict:
    """Flatten a JourneyPlanner trip query response."""
    trip = ((data or {}).get("data") or {}).get("trip") or {}
    patterns = trip.get("tripPatterns") or []
    options: list[dict] = []
    for pat in patterns:
        if not isinstance(pat, dict):
            continue
        legs = parse_trip_legs(pat.get("legs", []))
        # Count transfers as PT-mode legs minus 1 (walking legs in between
        # don't count as transfers in normal usage).
        pt_legs = [l for l in legs if l["mode"] not in ("walk", "bike", "scooter")]
        transfers = max(0, len(pt_legs) - 1)
        start = pat.get("startTime", "") or (legs[0]["departure"] if legs else "")
        end = pat.get("endTime", "") or (legs[-1]["arrival"] if legs else "")
        options.append({
            "departure": start,
            "arrival": end,
            "duration_min": (
                int(pat["duration"] // 60) if isinstance(pat.get("duration"), (int, float))
                else _iso_to_minutes(start, end)
            ),
            "transfers": transfers,
            "legs": legs,
        })
    return {
        "from": from_label,
        "to": to_label,
        "departure": departure_label,
        "options": options,
    }


# ── Departures ──────────────────────────────────────────────────────


def parse_departures(data: dict) -> dict:
    sp = ((data or {}).get("data") or {}).get("stopPlace") or {}
    if not sp:
        return {"stop_name": "", "stop_id": "", "departures": []}
    rows: list[dict] = []
    for c in sp.get("estimatedCalls", []) or []:
        if not isinstance(c, dict):
            continue
        sj = c.get("serviceJourney") or {}
        line = sj.get("line") or {}
        authority = (line.get("authority") or {})
        aimed = c.get("aimedDepartureTime", "")
        expected = c.get("expectedDepartureTime", "") or aimed
        delay = _minutes_diff(aimed, expected)
        quay = c.get("quay") or {}
        rows.append({
            "line": line.get("publicCode") or line.get("name") or "",
            "line_full_name": line.get("name", ""),
            "destination": (c.get("destinationDisplay") or {}).get("frontText", ""),
            "scheduled": aimed,
            "expected": expected,
            "delay_min": delay,
            "platform": quay.get("publicCode", ""),
            "mode": _norm_mode(line.get("transportMode")),
            "authority": authority.get("name", ""),
            "realtime": bool(c.get("realtime")),
        })
    return {
        "stop_name": sp.get("name", ""),
        "stop_id": sp.get("id", ""),
        "count": len(rows),
        "departures": rows,
    }


# ── Line detail ─────────────────────────────────────────────────────


def parse_line(data: dict) -> dict:
    line = ((data or {}).get("data") or {}).get("line") or {}
    if not line:
        return {"line_id": "", "line_name": "", "authority": "", "mode": "", "stops": []}
    auth = line.get("authority") or {}
    quays = line.get("quays") or []
    stops_seen: dict[str, dict] = {}
    seq = 0
    for q in quays:
        if not isinstance(q, dict):
            continue
        sp = q.get("stopPlace") or {}
        sp_id = sp.get("id") or q.get("id") or ""
        if not sp_id or sp_id in stops_seen:
            continue
        seq += 1
        stops_seen[sp_id] = {
            "name": sp.get("name") or q.get("name", ""),
            "id": sp_id,
            "lat": q.get("latitude"),
            "lon": q.get("longitude"),
            "sequence": seq,
        }
    return {
        "line_id": line.get("id", ""),
        "line_name": line.get("name", "") or line.get("publicCode", ""),
        "public_code": line.get("publicCode", ""),
        "authority": auth.get("name", ""),
        "mode": _norm_mode(line.get("transportMode")),
        "stops": list(stops_seen.values()),
    }
