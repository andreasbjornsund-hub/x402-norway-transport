"""Tests for the Entur response parsers."""


def test_geocoder_parses_features(parsers_module):
    payload = {"features": [
        {
            "geometry": {"type": "Point", "coordinates": [10.7525, 59.9112]},
            "properties": {
                "id": "NSR:StopPlace:58366",
                "name": "Jernbanetorget",
                "label": "Jernbanetorget, Oslo",
                "category": ["onstreetTram", "onstreetBus"],
                "locality": "Oslo",
                "county": "Oslo",
            },
        }
    ]}
    rows = parsers_module.parse_geocoder_results(payload)
    assert len(rows) == 1
    r = rows[0]
    assert r["id"] == "NSR:StopPlace:58366"
    assert r["name"] == "Jernbanetorget"
    assert r["lat"] == 59.9112 and r["lon"] == 10.7525
    assert r["municipality"] == "Oslo"


def test_geocoder_best_match(parsers_module):
    payload = {"features": [
        {"geometry": {"coordinates": [10.0, 60.0]}, "properties": {"id": "A", "name": "First"}},
        {"geometry": {"coordinates": [11.0, 61.0]}, "properties": {"id": "B", "name": "Second"}},
    ]}
    m = parsers_module.best_geocoder_match(payload)
    assert m["id"] == "A"


def test_geocoder_empty(parsers_module):
    assert parsers_module.parse_geocoder_results({}) == []
    assert parsers_module.best_geocoder_match({}) is None


def test_trip_parses_pattern(parsers_module):
    payload = {"data": {"trip": {"tripPatterns": [{
        "duration": 23820,
        "startTime": "2026-05-09T08:25:00+02:00",
        "endTime": "2026-05-09T15:02:00+02:00",
        "legs": [{
            "mode": "rail",
            "duration": 23820,
            "distance": 470000,
            "expectedStartTime": "2026-05-09T08:25:00+02:00",
            "expectedEndTime": "2026-05-09T15:02:00+02:00",
            "fromPlace": {"name": "Oslo S"},
            "toPlace": {"name": "Bergen"},
            "line": {"publicCode": "R10", "name": "Bergensbanen"},
            "authority": {"name": "Vy"},
        }],
    }]}}}
    out = parsers_module.parse_trip(payload, from_label="Oslo S", to_label="Bergen", departure_label="2026-05-09T08:00")
    assert out["from"] == "Oslo S"
    assert len(out["options"]) == 1
    o = out["options"][0]
    assert o["transfers"] == 0
    assert o["duration_min"] == 397
    assert o["legs"][0]["mode"] == "train"  # rail -> train
    assert o["legs"][0]["line"] == "R10"
    assert o["legs"][0]["line_full_name"] == "Bergensbanen"
    assert o["legs"][0]["distance_m"] == 470000


def test_trip_counts_transfers(parsers_module):
    """Transfers = PT-mode legs - 1 (walking legs don't count)."""
    payload = {"data": {"trip": {"tripPatterns": [{
        "duration": 1800, "startTime": "2026-05-09T08:00", "endTime": "2026-05-09T08:30",
        "legs": [
            {"mode": "foot", "fromPlace": {"name": "Home"}, "toPlace": {"name": "Stop A"}},
            {"mode": "bus", "fromPlace": {"name": "Stop A"}, "toPlace": {"name": "Stop B"}, "line": {"publicCode": "21"}},
            {"mode": "foot", "fromPlace": {"name": "Stop B"}, "toPlace": {"name": "Stop C"}},
            {"mode": "tram", "fromPlace": {"name": "Stop C"}, "toPlace": {"name": "Stop D"}, "line": {"publicCode": "13"}},
            {"mode": "foot", "fromPlace": {"name": "Stop D"}, "toPlace": {"name": "Office"}},
        ],
    }]}}}
    out = parsers_module.parse_trip(payload, "Home", "Office", None)
    assert out["options"][0]["transfers"] == 1


def test_departures_parses_with_realtime(parsers_module):
    payload = {"data": {"stopPlace": {
        "id": "NSR:StopPlace:58366",
        "name": "Jernbanetorget",
        "estimatedCalls": [{
            "aimedDepartureTime": "2026-05-09T08:15:00+02:00",
            "expectedDepartureTime": "2026-05-09T08:17:00+02:00",
            "realtime": True,
            "destinationDisplay": {"frontText": "Bekkestua"},
            "quay": {"publicCode": "1"},
            "serviceJourney": {"line": {
                "publicCode": "13", "name": "13 Bekkestua",
                "transportMode": "tram", "authority": {"name": "Ruter"},
            }},
        }],
    }}}
    out = parsers_module.parse_departures(payload)
    assert out["stop_name"] == "Jernbanetorget"
    assert out["count"] == 1
    d = out["departures"][0]
    assert d["line"] == "13"
    assert d["destination"] == "Bekkestua"
    assert d["delay_min"] == 2
    assert d["mode"] == "tram"
    assert d["authority"] == "Ruter"
    assert d["realtime"] is True


def test_departures_empty(parsers_module):
    assert parsers_module.parse_departures({}) == {"stop_name": "", "stop_id": "", "departures": []}


def test_line_parses_stops_in_sequence(parsers_module):
    payload = {"data": {"line": {
        "id": "RUT:Line:13", "publicCode": "13", "name": "13 Bekkestua",
        "transportMode": "tram", "authority": {"name": "Ruter"},
        "quays": [
            {"id": "q1", "stopPlace": {"id": "NSR:StopPlace:58366", "name": "Jernbanetorget"},
             "latitude": 59.9112, "longitude": 10.7525},
            {"id": "q2", "stopPlace": {"id": "NSR:StopPlace:58366", "name": "Jernbanetorget"},
             "latitude": 59.9112, "longitude": 10.7525},  # duplicate stop place
            {"id": "q3", "stopPlace": {"id": "NSR:StopPlace:99999", "name": "Stortinget"},
             "latitude": 59.9131, "longitude": 10.7400},
        ],
    }}}
    out = parsers_module.parse_line(payload)
    assert out["line_id"] == "RUT:Line:13"
    assert out["mode"] == "tram"
    assert out["authority"] == "Ruter"
    assert len(out["stops"]) == 2  # duplicate stop place deduped
    assert [s["sequence"] for s in out["stops"]] == [1, 2]


def test_norm_mode_mapping(parsers_module):
    f = parsers_module._norm_mode
    assert f("rail") == "train"
    assert f("water") == "ferry"
    assert f("foot") == "walk"
    assert f("BUS") == "bus"
    assert f(None) == ""
    # Unknown mode is passed through lowercased
    assert f("magic-bus") == "magic-bus"
