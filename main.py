"""
x402-norway-transport — Norwegian Transport

x402 micropayment API wrapping Entur's free public-transport APIs:
journey planning, real-time departures, stop search, and line detail.

Endpoints (free):
  GET /                       — landing page (HTML or JSON)
  GET /health                 — health check
  GET /api-status             — uptime + cache shape
  GET /modes                  — list transport modes
  GET /services.json          — agent-readable services manifest
  GET /llms.txt               — LLMs.txt for AI crawlers
  GET /robots.txt             — robots policy
  GET /.well-known/x402.json  — x402 agent-discovery manifest

Endpoints (paid, USDC on Base):
  GET /journey                $0.01    plan a journey between two places
  GET /departures             $0.005   real-time departures from a stop
  GET /stops                  $0.005   search stops by name/coords
  GET /line/{line_id}         $0.005   line metadata + ordered stops

Data: Entur (api.entur.io), the Norwegian national transport authority.
Free, no API key — just send an `ET-Client-Name` header (handled in
entur_client.py). Cache: stops 7d, line detail 24h, departures and
journeys are real-time and never cached.
"""
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

import cache
import entur_client as entur
from entur_client import EnturError
import modes
import parsers

from cdp_auth import create_cdp_auth_provider

from x402.http import FacilitatorConfig, HTTPFacilitatorClient, PaymentOption
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http.types import RouteConfig
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.schemas import Network
from x402.server import x402ResourceServer

load_dotenv()

# ── Config ──────────────────────────────────────────────────────────

SERVICE_ID = "norway-transport"
SERVICE_NAME = "Norwegian Transport"
SERVICE_DESCRIPTION = (
    "Journey planning, real-time departures, and stop search across all "
    "Norwegian public transport. Powered by Entur. Pay per query with USDC via x402."
)
SERVICE_CATEGORY = "transport"

EVM_ADDRESS = os.getenv("EVM_ADDRESS")
EVM_NETWORK: Network = "eip155:8453"
FACILITATOR_URL = os.getenv("FACILITATOR_URL", "https://x402.org/facilitator")
SITE_URL = os.getenv("SITE_URL", "https://x402-norway-transport.fly.dev")
USDC_BASE_MAINNET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

if not EVM_ADDRESS:
    raise ValueError("Set EVM_ADDRESS in .env")

# ── FastAPI app ─────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await _http.aclose()


app = FastAPI(
    title=SERVICE_NAME,
    description=SERVICE_DESCRIPTION,
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)

import json as _json

cdp_auth = None
if "cdp.coinbase.com" in FACILITATOR_URL:
    cdp_auth = create_cdp_auth_provider()
facilitator_config = FacilitatorConfig(url=FACILITATOR_URL, auth_provider=cdp_auth)
facilitator = HTTPFacilitatorClient(facilitator_config)

_CAIP2_TO_V1 = {"eip155:8453": "base", "eip155:84532": "base-sepolia"}


def _v2_payload_to_v1(payload_dict: dict) -> dict:
    v1 = {"x402Version": 1}
    v1["scheme"] = payload_dict.get("scheme", "exact")
    raw_net = payload_dict.get("network", EVM_NETWORK)
    v1["network"] = _CAIP2_TO_V1.get(raw_net, raw_net)
    v1["payload"] = payload_dict.get("payload", payload_dict)
    return v1


def _v2_requirements_to_v1(req_dict: dict) -> dict:
    raw_net = req_dict.get("network", EVM_NETWORK)
    extra = req_dict.get("extra", {})
    if isinstance(extra, str):
        try:
            extra = _json.loads(extra)
        except Exception:
            extra = {}
    v1 = {
        "scheme": req_dict.get("scheme", "exact"),
        "network": _CAIP2_TO_V1.get(raw_net, raw_net),
        "maxAmountRequired": req_dict.get("amount", req_dict.get("maxAmountRequired", "0")),
        "resource": req_dict.get("resource", ""),
        "description": req_dict.get("description", ""),
        "mimeType": req_dict.get("mimeType", req_dict.get("mime_type", "application/json")),
        "asset": req_dict.get("asset", ""),
        "payTo": req_dict.get("payTo", req_dict.get("pay_to", "")),
        "maxTimeoutSeconds": req_dict.get("maxTimeoutSeconds", req_dict.get("max_timeout_seconds", 300)),
        "extra": extra,
    }
    extensions = req_dict.get("extensions", {})
    bazaar = extensions.get("bazaar", {})
    if bazaar.get("info"):
        v1["outputSchema"] = bazaar["info"]
    return v1


_orig_verify = facilitator._verify_http
_orig_settle = facilitator._settle_http


async def _v1_verify(version, payload_dict, requirements_dict):
    return await _orig_verify(1, _v2_payload_to_v1(payload_dict), _v2_requirements_to_v1(requirements_dict))


async def _v1_settle(version, payload_dict, requirements_dict):
    return await _orig_settle(1, _v2_payload_to_v1(payload_dict), _v2_requirements_to_v1(requirements_dict))


facilitator._verify_http = _v1_verify
facilitator._settle_http = _v1_settle

server = x402ResourceServer(facilitator)
server.register(EVM_NETWORK, ExactEvmServerScheme())

# ── Endpoint catalog ────────────────────────────────────────────────

ENDPOINT_CATALOG: list[dict] = [
    {
        "method": "GET",
        "path": "/journey",
        "route_pattern": "GET /journey",
        "description": "Plan a journey between two Norwegian places. Names are geocoded to stop/place IDs, then the JourneyPlanner returns up to 3 best trip options with full leg-by-leg detail.",
        "price_usd": "$0.01",
        "amount_atomic": "10000",
        "query_params": {"from": "Oslo S", "to": "Bergen", "when": "2026-05-09T08:00:00"},
        "path_params": {},
        "output_example": {
            "from": "Oslo S", "to": "Bergen", "departure": "2026-05-09T08:00:00",
            "options": [{
                "departure": "2026-05-09T08:25:00", "arrival": "2026-05-09T15:02:00",
                "duration_min": 397, "transfers": 0,
                "legs": [{
                    "mode": "train", "from_stop": "Oslo S", "to_stop": "Bergen",
                    "line": "R10", "line_full_name": "Bergensbanen",
                    "departure": "2026-05-09T08:25:00", "arrival": "2026-05-09T15:02:00",
                    "duration_min": 397,
                }],
            }],
        },
    },
    {
        "method": "GET",
        "path": "/departures",
        "route_pattern": "GET /departures",
        "description": "Real-time departures from a Norwegian stop place. Stop name is geocoded; pass a stop ID directly to skip the geocoder. Always live (not cached).",
        "price_usd": "$0.005",
        "amount_atomic": "5000",
        "query_params": {"stop": "Jernbanetorget", "minutes": 30},
        "path_params": {},
        "output_example": {
            "stop_name": "Jernbanetorget", "stop_id": "NSR:StopPlace:58366", "count": 1,
            "departures": [{
                "line": "13", "destination": "Bekkestua",
                "scheduled": "2026-05-09T08:15:00", "expected": "2026-05-09T08:17:00",
                "delay_min": 2, "platform": "1", "mode": "tram",
            }],
        },
    },
    {
        "method": "GET",
        "path": "/stops",
        "route_pattern": "GET /stops",
        "description": "Search Norwegian stops by name and/or proximity (lat/lon). Returns top 10 with coordinates, modes, municipality.",
        "price_usd": "$0.005",
        "amount_atomic": "5000",
        "query_params": {"q": "jernbanetorget"},
        "path_params": {},
        "output_example": {
            "results": [{
                "name": "Jernbanetorget", "id": "NSR:StopPlace:58366",
                "lat": 59.9112, "lon": 10.7525, "municipality": "Oslo",
            }],
        },
    },
    {
        "method": "GET",
        "path": "/line/{line_id}",
        "route_pattern": "GET /line/*",
        "description": "Line metadata and ordered stop list. Cached 24h.",
        "price_usd": "$0.005",
        "amount_atomic": "5000",
        "query_params": {},
        "path_params": {"line_id": "RUT:Line:13"},
        "output_example": {
            "line_id": "RUT:Line:13", "line_name": "13 Bekkestua",
            "authority": "Ruter", "mode": "tram",
            "stops": [{"name": "Jernbanetorget", "id": "NSR:StopPlace:58366",
                       "lat": 59.9112, "lon": 10.7525, "sequence": 1}],
        },
    },
    {"method": "GET", "path": "/modes", "route_pattern": None,
     "description": "Reference list of transport modes (free).",
     "price_usd": None, "amount_atomic": None, "query_params": {}, "path_params": {}, "output_example": None},
    {"method": "GET", "path": "/health", "route_pattern": None,
     "description": "Service health check.",
     "price_usd": None, "amount_atomic": None, "query_params": {}, "path_params": {}, "output_example": {"status": "ok"}},
    {"method": "GET", "path": "/api-status", "route_pattern": None,
     "description": "Operational status — uptime and Entur-cache shape.",
     "price_usd": None, "amount_atomic": None, "query_params": {}, "path_params": {}, "output_example": None},
]


def _bazaar_info(entry: dict) -> dict:
    inp = {"type": "http", "method": entry["method"]}
    if entry["query_params"]:
        inp["queryParams"] = entry["query_params"]
    if entry["path_params"]:
        inp["pathParams"] = entry["path_params"]
    return {
        "info": {"input": inp, "output": {"type": "json", "example": entry["output_example"]}},
        "schema": {"$schema": "https://json-schema.org/draft/2020-12/schema",
                   "type": "object",
                   "properties": {"input": {"type": "object"}, "output": {"type": "object"}}},
    }


def _build_paid_routes(catalog: list[dict]) -> dict[str, RouteConfig]:
    return {
        e["route_pattern"]: RouteConfig(
            accepts=[PaymentOption(scheme="exact", pay_to=EVM_ADDRESS, price=e["price_usd"], network=EVM_NETWORK)],
            mime_type="application/json",
            description=e["description"],
            extensions={"bazaar": _bazaar_info(e)},
        )
        for e in catalog if e["route_pattern"] is not None
    }


routes = _build_paid_routes(ENDPOINT_CATALOG)
app.add_middleware(PaymentMiddlewareASGI, routes=routes, server=server)
# Outer middleware that polishes 402 responses to match the x402 spec.
from x402_polish import X402ResponsePolish  # noqa: E402
app.add_middleware(X402ResponsePolish)

# ── Shared HTTP client ──────────────────────────────────────────────

_http = httpx.AsyncClient(timeout=30, headers={"Accept": "application/json"})

_PROCESS_START_TS = time.time()


# ── Discovery / metadata endpoints ──────────────────────────────────


@app.get("/")
async def landing(request: Request):
    accept = request.headers.get("accept", "")
    if "text/html" in accept and os.path.isfile("static/index.html"):
        return FileResponse("static/index.html")
    return {
        "service": SERVICE_NAME, "version": "0.1.0", "description": SERVICE_DESCRIPTION,
        "endpoints": {e["path"]: f"{e['description']} ({e['price_usd']} USDC)" if e["price_usd"]
                      else f"{e['description']} (free)"
                      for e in ENDPOINT_CATALOG} | {"/.well-known/x402.json": "Agent discovery"},
        "payment": "x402 protocol — USDC on Base network",
        "data_source": "Entur (https://api.entur.io)",
    }


@app.get("/health")
async def health():
    return {"status": "ok", "service": SERVICE_ID, "timestamp": int(time.time())}


@app.get("/api-status")
async def api_status():
    return {
        "status": "ok", "service": SERVICE_ID, "version": "0.1.0",
        "uptime_seconds": int(time.time() - _PROCESS_START_TS),
        "upstream": "api.entur.io",
        "cache": cache.stats(),
    }


@app.get("/modes")
async def list_modes():
    ms = modes.all_modes()
    return {"count": len(ms), "modes": ms}


@app.get("/services.json")
async def services_manifest():
    return {
        "id": SERVICE_ID, "name": SERVICE_NAME, "description": SERVICE_DESCRIPTION,
        "category": SERVICE_CATEGORY, "x402Version": 2, "networks": [EVM_NETWORK],
        "website": SITE_URL,
        "endpoints": [{"method": e["method"], "path": e["path"], "description": e["description"],
                       "price": e["price_usd"] or "$0.00", "currency": "USDC"}
                      for e in ENDPOINT_CATALOG],
    }


@app.get("/.well-known/x402.json")
async def x402_manifest():
    return {
        "x402Version": 2,
        "service": {"id": SERVICE_ID, "name": SERVICE_NAME, "description": SERVICE_DESCRIPTION,
                    "category": SERVICE_CATEGORY, "website": SITE_URL,
                    "documentation": f"{SITE_URL}/llms.txt",
                    "servicesManifest": f"{SITE_URL}/services.json"},
        "payment": {"schemes": ["exact"], "networks": [EVM_NETWORK],
                    "asset": {"symbol": "USDC", "decimals": 6, "address": USDC_BASE_MAINNET, "chain": "Base"},
                    "payTo": EVM_ADDRESS, "facilitator": FACILITATOR_URL},
        "endpoints": [
            {"method": e["method"], "path": e["path"], "description": e["description"],
             "accepts": [{"scheme": "exact", "network": EVM_NETWORK, "asset": "USDC",
                          "amount": e["amount_atomic"], "amountDisplay": e["price_usd"], "payTo": EVM_ADDRESS}]
                        if e["amount_atomic"] else [],
             "input": {"type": "http", "method": e["method"],
                       **({"queryParams": e["query_params"]} if e["query_params"] else {}),
                       **({"pathParams": e["path_params"]} if e["path_params"] else {})},
             "output": ({"type": "json", "example": e["output_example"]}
                        if e["output_example"] is not None else {"type": "json"})}
            for e in ENDPOINT_CATALOG
        ],
    }


@app.get("/llms.txt")
async def llms_txt():
    lines = [f"# {SERVICE_NAME}", f"> {SERVICE_DESCRIPTION}", "", "## Endpoints"]
    for e in ENDPOINT_CATALOG:
        price = f"{e['price_usd']} USDC" if e["price_usd"] else "Free"
        lines.append(f"- {e['method']} {e['path']} — {price} — {e['description']}")
    lines += [
        "", "## Payment",
        "- Protocol: x402 (HTTP 402 micropayments)",
        "- Currency: USDC on Base",
        "- No API keys or accounts needed",
        "- Agent discovery: GET /.well-known/x402.json",
        "", "## Source data",
        "- Entur (api.entur.io) — official Norwegian national transport authority",
        "- Free, no key; we identify ourselves with the ET-Client-Name header",
        "- Cached: stops 7d, line detail 24h. Departures and journeys are real-time (NOT cached).",
        "", "## Links",
        f"- Website: {SITE_URL}",
        f"- Services manifest: {SITE_URL}/services.json",
        "",
    ]
    return PlainTextResponse("\n".join(lines), media_type="text/plain")


@app.get("/robots.txt")
async def robots_txt():
    return PlainTextResponse(
        "User-agent: *\nAllow: /\n\n"
        "User-agent: GPTBot\nAllow: /\n\n"
        "User-agent: ClaudeBot\nAllow: /\n\n"
        "User-agent: PerplexityBot\nAllow: /\n\n"
        "User-agent: Google-Extended\nAllow: /\n",
        media_type="text/plain",
    )


def _set_cache_header(response: Response, hit: bool) -> None:
    response.headers["X-Cache"] = "HIT" if hit else "MISS"


def _looks_like_id(s: str) -> bool:
    """Heuristic: Entur place IDs look like NSR:StopPlace:12345 or similar."""
    return ":" in s and any(k in s for k in (":StopPlace:", ":Quay:", ":GroupOfStopPlaces:"))


async def _resolve_place(name_or_id: str) -> tuple[str, str]:
    """Return (canonical_label, place_id). If input is an ID, no geocode call."""
    if _looks_like_id(name_or_id):
        return name_or_id, name_or_id
    try:
        data, _ = await entur.geocode(_http, name_or_id, size=1)
    except EnturError as e:
        raise HTTPException(503, f"Entur geocoder: {e.message}")
    match = parsers.best_geocoder_match(data)
    if not match or not match.get("id"):
        raise HTTPException(404, f"No Norwegian place matched '{name_or_id}'")
    return match.get("name", name_or_id), match["id"]


# ── Paid endpoints ──────────────────────────────────────────────────


@app.get("/journey")
async def journey(
    response: Response,
    from_: str = Query(..., alias="from", min_length=1, max_length=200),
    to: str = Query(..., min_length=1, max_length=200),
    when: str | None = Query(None, description="ISO 8601 datetime; default: now"),
    options: int = Query(3, ge=1, le=10),
):
    """Plan a journey. Geocode both endpoints, then call JourneyPlanner."""
    from_label, from_id = await _resolve_place(from_)
    to_label, to_id = await _resolve_place(to)
    date_time = when or datetime.now(timezone.utc).isoformat()
    try:
        data, _ = await entur.trip(_http, from_id, to_id, date_time, num=options)
    except EnturError as e:
        raise HTTPException(503, f"Entur upstream: {e.message}")
    parsed = parsers.parse_trip(data, from_label, to_label, when)
    if not parsed.get("options"):
        # 4xx so x402 SDK skips settle — no charge for an empty itinerary
        raise HTTPException(404, f"No journey options between '{from_label}' and '{to_label}'")
    response.headers["X-Cache"] = "MISS"  # never cached
    return parsed


@app.get("/departures")
async def departures(
    response: Response,
    stop: str = Query(..., min_length=1, max_length=200),
    minutes: int = Query(30, ge=1, le=180),
):
    """Real-time departures from a stop place."""
    _, stop_id = await _resolve_place(stop)
    try:
        data, _ = await entur.departures(_http, stop_id, minutes=minutes)
    except EnturError as e:
        raise HTTPException(503, f"Entur upstream: {e.message}")
    parsed = parsers.parse_departures(data)
    if not parsed.get("stop_id"):
        raise HTTPException(404, f"No stop place found for '{stop}'")
    if not parsed.get("departures"):
        # 4xx so x402 SDK skips settle — no charge if the stop has no
        # scheduled departures in the requested window
        raise HTTPException(404, f"No departures from '{stop}' in next {minutes} minutes")
    response.headers["X-Cache"] = "MISS"  # never cached
    return parsed


@app.get("/stops")
async def stops_search(
    response: Response,
    q: str = Query(..., min_length=2, max_length=200),
    lat: float | None = Query(None, ge=-90, le=90),
    lon: float | None = Query(None, ge=-180, le=180),
    limit: int = Query(10, ge=1, le=50),
):
    """Stop / place search via the Entur geocoder."""
    try:
        data, hit = await entur.stops_search(_http, q, lat=lat, lon=lon, size=limit)
    except EnturError as e:
        raise HTTPException(503, f"Entur upstream: {e.message}")
    results = parsers.parse_geocoder_results(data, limit=limit)
    if not results:
        # 4xx so x402 SDK skips settle — no charge for empty results
        raise HTTPException(404, f"No stops match '{q}'")
    _set_cache_header(response, hit)
    return {"results": results}


@app.get("/line/{line_id}")
async def line_detail(
    response: Response,
    line_id: str,
):
    """Line detail by NSR id (e.g. RUT:Line:13)."""
    if not line_id or len(line_id) > 64:
        raise HTTPException(400, "Invalid line_id")
    try:
        data, hit = await entur.line_detail(_http, line_id)
    except EnturError as e:
        if e.status_code == 404:
            raise HTTPException(404, f"Line '{line_id}' not found")
        raise HTTPException(503, f"Entur upstream: {e.message}")
    parsed = parsers.parse_line(data)
    if not parsed["line_id"]:
        raise HTTPException(404, f"Line '{line_id}' not found")
    _set_cache_header(response, hit)
    return parsed


# ── Static files ────────────────────────────────────────────────────

if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")
