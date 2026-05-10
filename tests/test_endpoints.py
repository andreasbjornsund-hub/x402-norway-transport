"""End-to-end tests for the HTTP handlers (Entur stubbed)."""
import pytest
from fastapi import HTTPException, Response


def _geocoder_response(stop_id="NSR:StopPlace:58366", name="Jernbanetorget"):
    return {"features": [{
        "geometry": {"coordinates": [10.7525, 59.9112]},
        "properties": {"id": stop_id, "name": name, "locality": "Oslo"},
    }]}


# ── Free endpoints ──────────────────────────────────────────────────


async def test_health(main_module):
    r = await main_module.health()
    assert r["status"] == "ok"
    assert r["service"] == "norway-transport"


async def test_modes_count(main_module):
    r = await main_module.list_modes()
    assert r["count"] >= 10
    assert any(m["code"] == "train" for m in r["modes"])
    assert any(m["code"] == "bus" for m in r["modes"])


async def test_api_status(main_module):
    r = await main_module.api_status()
    assert r["upstream"] == "api.entur.io"
    for k in ("entries", "fresh", "max"):
        assert k in r["cache"]


# ── Manifest contract ───────────────────────────────────────────────


async def test_x402_manifest(main_module):
    r = await main_module.x402_manifest()
    paid = [e for e in r["endpoints"] if e["accepts"]]
    free = [e for e in r["endpoints"] if not e["accepts"]]
    assert len(paid) == 4  # /journey, /departures, /stops, /line/{line_id}
    assert len(free) == 3  # /modes, /health, /api-status


async def test_atomic_amounts_match_price(main_module):
    for e in main_module.ENDPOINT_CATALOG:
        if e["price_usd"] is None:
            continue
        usd = float(e["price_usd"].replace("$", ""))
        expected = str(int(round(usd * 10**6)))
        assert e["amount_atomic"] == expected, f"{e['path']}: {expected} vs {e['amount_atomic']}"


# ── Paid handlers ───────────────────────────────────────────────────


async def test_stops_search_happy_path(main_module, fake_entur):
    fake_entur.stub_get("/geocoder/v1/autocomplete", 200, _geocoder_response())
    out = await main_module.stops_search(response=Response(), q="jernbanetorget", lat=None, lon=None, limit=10)
    assert out["results"][0]["name"] == "Jernbanetorget"
    assert out["results"][0]["id"] == "NSR:StopPlace:58366"


async def test_stops_search_503_on_upstream(main_module, fake_entur):
    fake_entur.stub_get("/geocoder/v1/autocomplete", 502)
    with pytest.raises(HTTPException) as exc:
        await main_module.stops_search(response=Response(), q="x", lat=None, lon=None, limit=10)
    assert exc.value.status_code == 503


async def test_journey_geocodes_then_calls_trip(main_module, fake_entur):
    fake_entur.stub_get("/geocoder/v1/autocomplete", 200, _geocoder_response("NSR:StopPlace:1", "Oslo S"))
    fake_entur.stub_post("/journey-planner/v3/graphql", 200, {"data": {"trip": {"tripPatterns": [{
        "duration": 600, "startTime": "2026-05-09T08:00", "endTime": "2026-05-09T08:10",
        "legs": [{"mode": "rail", "fromPlace": {"name": "Oslo S"}, "toPlace": {"name": "Lillestrøm"},
                  "line": {"publicCode": "L1"}}],
    }]}}})
    out = await main_module.journey(response=Response(), from_="Oslo S", to="Lillestrøm",
                                     when="2026-05-09T08:00:00", options=3)
    assert out["from"] == "Oslo S"
    assert len(out["options"]) == 1
    assert out["options"][0]["legs"][0]["mode"] == "train"


async def test_journey_with_id_skips_geocoder(main_module, fake_entur):
    """Pass NSR:StopPlace:... directly — should not hit the geocoder.

    Empty options now raise 404 (x402 SDK skips settle on 4xx so the user
    isn't charged for an empty itinerary).
    """
    fake_entur.stub_post("/journey-planner/v3/graphql", 200, {"data": {"trip": {"tripPatterns": []}}})
    with pytest.raises(HTTPException) as exc:
        await main_module.journey(
            response=Response(),
            from_="NSR:StopPlace:1", to="NSR:StopPlace:2",
            when=None, options=3,
        )
    assert exc.value.status_code == 404
    # No geocoder calls
    assert all("/geocoder/" not in url for _, url, _ in fake_entur.calls)


async def test_stops_search_404_on_no_matches(main_module, fake_entur):
    fake_entur.stub_get("/geocoder/v1/autocomplete", 200, {"features": []})
    with pytest.raises(HTTPException) as exc:
        await main_module.stops_search(response=Response(), q="zzz-no-match", lat=None, lon=None, limit=10)
    assert exc.value.status_code == 404


async def test_departures_404_on_no_departures(main_module, fake_entur):
    fake_entur.stub_get("/geocoder/v1/autocomplete", 200, _geocoder_response())
    fake_entur.stub_post("/journey-planner/v3/graphql", 200, {"data": {"stopPlace": {
        "id": "NSR:StopPlace:58366", "name": "Jernbanetorget", "estimatedCalls": [],
    }}})
    with pytest.raises(HTTPException) as exc:
        await main_module.departures(response=Response(), stop="jernbanetorget", minutes=30)
    assert exc.value.status_code == 404


async def test_journey_404_on_unknown_place(main_module, fake_entur):
    fake_entur.stub_get("/geocoder/v1/autocomplete", 200, {"features": []})
    with pytest.raises(HTTPException) as exc:
        await main_module.journey(response=Response(), from_="Atlantis", to="El Dorado",
                                   when=None, options=3)
    assert exc.value.status_code == 404


async def test_departures_happy_path(main_module, fake_entur):
    fake_entur.stub_get("/geocoder/v1/autocomplete", 200, _geocoder_response())
    fake_entur.stub_post("/journey-planner/v3/graphql", 200, {"data": {"stopPlace": {
        "id": "NSR:StopPlace:58366", "name": "Jernbanetorget",
        "estimatedCalls": [{
            "aimedDepartureTime": "2026-05-09T08:15", "expectedDepartureTime": "2026-05-09T08:17",
            "realtime": True,
            "destinationDisplay": {"frontText": "Bekkestua"},
            "quay": {"publicCode": "1"},
            "serviceJourney": {"line": {"publicCode": "13", "transportMode": "tram"}},
        }],
    }}})
    out = await main_module.departures(response=Response(), stop="jernbanetorget", minutes=30)
    assert out["count"] == 1
    assert out["departures"][0]["delay_min"] == 2


async def test_departures_404_on_no_stop(main_module, fake_entur):
    fake_entur.stub_get("/geocoder/v1/autocomplete", 200, _geocoder_response())
    fake_entur.stub_post("/journey-planner/v3/graphql", 200, {"data": {"stopPlace": None}})
    with pytest.raises(HTTPException) as exc:
        await main_module.departures(response=Response(), stop="nope", minutes=30)
    assert exc.value.status_code == 404


async def test_line_detail_happy_path(main_module, fake_entur):
    fake_entur.stub_post("/journey-planner/v3/graphql", 200, {"data": {"line": {
        "id": "RUT:Line:13", "publicCode": "13", "name": "13 Bekkestua",
        "transportMode": "tram", "authority": {"name": "Ruter"},
        "quays": [{"id": "q1",
                   "stopPlace": {"id": "NSR:StopPlace:58366", "name": "Jernbanetorget"},
                   "latitude": 59.9112, "longitude": 10.7525}],
    }}})
    out = await main_module.line_detail(response=Response(), line_id="RUT:Line:13")
    assert out["line_id"] == "RUT:Line:13"
    assert out["mode"] == "tram"
    assert len(out["stops"]) == 1


async def test_line_detail_404(main_module, fake_entur):
    fake_entur.stub_post("/journey-planner/v3/graphql", 200, {"data": {"line": None}})
    with pytest.raises(HTTPException) as exc:
        await main_module.line_detail(response=Response(), line_id="RUT:Line:99999")
    assert exc.value.status_code == 404


async def test_line_detail_invalid_id_400(main_module):
    with pytest.raises(HTTPException) as exc:
        await main_module.line_detail(response=Response(), line_id="x" * 100)
    assert exc.value.status_code == 400


async def test_stops_caches_second_call(main_module, fake_entur):
    fake_entur.stub_get("/geocoder/v1/autocomplete", 200, _geocoder_response())
    r1 = Response()
    await main_module.stops_search(response=r1, q="jern", lat=None, lon=None, limit=10)
    assert r1.headers["X-Cache"] == "MISS"
    r2 = Response()
    await main_module.stops_search(response=r2, q="jern", lat=None, lon=None, limit=10)
    assert r2.headers["X-Cache"] == "HIT"
