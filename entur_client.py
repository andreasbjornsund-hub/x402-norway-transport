"""Entur upstream client.

Three free public APIs from Entur (the Norwegian national journey-planner
operator):

  - JourneyPlanner v3 (GraphQL):  POST https://api.entur.io/journey-planner/v3/graphql
  - Geocoder v1 (REST):           GET  https://api.entur.io/geocoder/v1/autocomplete
  - Stop Places v1 (GraphQL):     POST https://api.entur.io/stop-places/v1/graphql

Auth: per Entur's docs, every request must carry an ET-Client-Name header
identifying the calling application. Format `<organization>-<app>`. No key.

Rate limit: ~100 req/min unless you contact them. We cap concurrency at
20 as a buffer.

Cache policy (per the build prompt):
  - Geocoder + stop search:  7 days  (places rarely move)
  - Line details:            24 hours
  - Journey trips:           NOT cached (real-time data)
  - Departures:              NOT cached (real-time data)
"""
import asyncio
import json as _json

import httpx

import cache


ET_CLIENT_NAME = "x402agent-transport"

JP_URL = "https://api.entur.io/journey-planner/v3/graphql"
GEOCODER_URL = "https://api.entur.io/geocoder/v1/autocomplete"
STOPS_URL = "https://api.entur.io/stop-places/v1/graphql"

_SEM = asyncio.Semaphore(20)


class EnturError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"Entur {status_code}: {message}")


def _headers() -> dict:
    return {
        "ET-Client-Name": ET_CLIENT_NAME,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


async def _post_graphql(
    client: httpx.AsyncClient,
    url: str,
    query: str,
    variables: dict | None = None,
    cache_ttl: float = 0,
    cache_key: str | None = None,
) -> tuple[dict, bool]:
    """POST a GraphQL query, with optional caching.

    cache_ttl == 0 means never cache (real-time endpoints).
    """
    if cache_ttl > 0 and cache_key:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached, True

    body = {"query": query, "variables": variables or {}}
    async with _SEM:
        resp = await client.post(url, json=body, headers=_headers())

    if resp.status_code != 200:
        raise EnturError(resp.status_code, resp.text[:300])

    data = resp.json()
    if isinstance(data, dict) and data.get("errors"):
        msg = "; ".join(e.get("message", "") for e in data["errors"])
        raise EnturError(502, f"GraphQL error: {msg[:300]}")

    if cache_ttl > 0 and cache_key:
        cache.put(cache_key, data, cache_ttl)
    return data, False


async def _get_rest(
    client: httpx.AsyncClient,
    url: str,
    params: dict,
    cache_ttl: float,
    cache_key: str,
) -> tuple[dict, bool]:
    cached = cache.get(cache_key)
    if cached is not None:
        return cached, True
    async with _SEM:
        resp = await client.get(url, params=params, headers=_headers())
    if resp.status_code != 200:
        raise EnturError(resp.status_code, resp.text[:300])
    data = resp.json()
    if cache_ttl > 0:
        cache.put(cache_key, data, cache_ttl)
    return data, False


# ── Geocoder (cached 7 days) ────────────────────────────────────────


async def geocode(client: httpx.AsyncClient, text: str, lat: float | None = None,
                  lon: float | None = None, size: int = 10):
    """Search for places by name (and optional proximity)."""
    params: dict = {"text": text, "size": size, "lang": "en"}
    if lat is not None and lon is not None:
        params["focus.point.lat"] = lat
        params["focus.point.lon"] = lon
    key = f"entur:geocode:{_json.dumps(params, sort_keys=True)}"
    return await _get_rest(client, GEOCODER_URL, params, ttl_geocode(), key)


def ttl_geocode() -> float:
    return 7 * 86400.0


# ── Trip (NOT cached — real-time) ───────────────────────────────────

_TRIP_QUERY = """\
query Trip($from: Location!, $to: Location!, $dateTime: DateTime, $numTripPatterns: Int) {
  trip(
    from: $from
    to: $to
    dateTime: $dateTime
    numTripPatterns: $numTripPatterns
    walkSpeed: 1.4
  ) {
    tripPatterns {
      duration
      startTime
      endTime
      legs {
        mode
        distance
        duration
        aimedStartTime
        expectedStartTime
        aimedEndTime
        expectedEndTime
        fromPlace { name quay { id } }
        toPlace { name quay { id } }
        line { id publicCode name transportMode }
        authority { name }
      }
    }
  }
}"""


async def trip(client: httpx.AsyncClient, from_id: str, to_id: str,
               date_time: str | None, num: int = 3):
    """Plan trips between two place/stop IDs. Not cached."""
    variables = {
        "from": {"place": from_id},
        "to": {"place": to_id},
        "dateTime": date_time,
        "numTripPatterns": max(1, min(num, 10)),
    }
    return await _post_graphql(client, JP_URL, _TRIP_QUERY, variables, cache_ttl=0)


# ── Departures (NOT cached — real-time) ─────────────────────────────

_DEPARTURES_QUERY = """\
query Departures($id: String!, $minutes: Int!) {
  stopPlace(id: $id) {
    id
    name
    estimatedCalls(timeRange: $minutes, numberOfDepartures: 50) {
      aimedDepartureTime
      expectedDepartureTime
      realtime
      destinationDisplay { frontText }
      quay { publicCode }
      serviceJourney {
        line { publicCode name transportMode authority { name } }
      }
    }
  }
}"""


async def departures(client: httpx.AsyncClient, stop_id: str, minutes: int = 30):
    """Real-time estimated departures for a stop place. Not cached."""
    return await _post_graphql(client, JP_URL, _DEPARTURES_QUERY,
                               {"id": stop_id, "minutes": int(minutes) * 60},
                               cache_ttl=0)


# ── Stops search via Geocoder + filter to stop places (cached 7d) ───


async def stops_search(client: httpx.AsyncClient, query: str,
                       lat: float | None = None, lon: float | None = None,
                       size: int = 10):
    """Search for stop places by name. Uses the geocoder under the hood."""
    return await geocode(client, query, lat, lon, size)


# ── Line details (cached 24h) ───────────────────────────────────────

_LINE_QUERY = """\
query Line($id: ID!) {
  line(id: $id) {
    id
    publicCode
    name
    transportMode
    authority { name }
    quays {
      id
      publicCode
      name
      latitude
      longitude
      stopPlace { id name }
    }
  }
}"""


async def line_detail(client: httpx.AsyncClient, line_id: str):
    """Fetch line metadata + ordered list of stops. Cached 24h."""
    key = f"entur:line:{line_id}"
    return await _post_graphql(client, JP_URL, _LINE_QUERY, {"id": line_id},
                               cache_ttl=24 * 3600.0, cache_key=key)
