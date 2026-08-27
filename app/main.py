"""Forge sandbox — the Forge API surface, locally, with no key and no account.

Same request bodies and same response shapes as production Forge. Simulated
data, real canonical schema. Everything is in-process: no database, no network
egress, nothing written to disk.
"""

import io
import csv
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional, Union

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from . import corpus, predict, simulate
from .output_invariants import check_response_invariants

log = logging.getLogger("forge.sandbox")

VERSION = "1.0.0"
SERVICE = "foundrynet-forge-sandbox"
STARTED = time.time()

# The MCP app owns a lifespan (its session manager), so it has to be built
# before the FastAPI app that will run it. A missing/incompatible fastmcp must
# not take the REST sandbox down with it -- /mcp then explains itself and
# /health reports the failure instead of the container looking healthy.
MCP_APP = None
MCP_ERROR = None
try:
    from . import mcp_tools
    MCP_APP = mcp_tools.http_app(path="/")
except Exception as _exc:            # pragma: no cover - import-environment only
    MCP_ERROR = f"{type(_exc).__name__}: {_exc}"

app = FastAPI(
    title="Forge Sandbox",
    version=VERSION,
    lifespan=(MCP_APP.lifespan if MCP_APP is not None else None),
    description=(
        "A local, keyless simulation of the Forge industrial telemetry kernel.\n\n"
        "Real canonical schema, real vendor tag mappings, simulated equipment and "
        "simulated predictions. Build your agent integration against this, then "
        "point the same code at https://forge.foundrynet.io with an API key.\n\n"
        "Nothing here connects to production. Nothing is persisted."
    ),
)


def _now():
    return datetime.now(timezone.utc).isoformat()


@app.middleware("http")
async def _sandbox_headers(request: Request, call_next):
    """Stamp every response. If one of these ever shows up against a real
    endpoint, something is misrouted -- and if it is missing here, you are not
    talking to the sandbox."""
    resp = await call_next(request)
    resp.headers["X-Forge-Sandbox"] = "true"
    resp.headers["X-Forge-Sandbox-Version"] = VERSION
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


# ── Root and health ──────────────────────────────────────────────────────────

@app.get("/")
async def root():
    packs, _, dictionary = corpus.load()
    return {
        "service": SERVICE,
        "version": VERSION,
        "description": "Forge sandbox — fake data, real schema.",
        "auth": "none required",
        "simulated": True,
        "machines": sorted(simulate.MACHINES),
        "canonical_fields": dictionary["field_count"],
        "mappings": sum(len(p.mappings) for p in packs.values()),
        "endpoints": {
            "POST /v1/normalize":     "raw vendor telemetry -> canonical fields",
            "POST /v1/predict_breach": "will a series cross a threshold, and when",
            "POST /v1/fleet_health":  "fleet rollup + maintenance queue",
            "GET  /v1/coverage":      "what the sandbox can normalize",
            "GET  /v1/machines":      "the five simulated machines",
            "GET  /v1/simulate/{machine}": "one raw vendor-shaped reading",
            "GET  /v1/simulate/{machine}/series": "a history for one raw tag",
            "GET  /health":           "liveness",
            "ANY  /mcp":              "MCP server (Streamable HTTP)",
            "GET  /docs":             "OpenAPI browser",
        },
        "upgrade": "https://foundrynet.io",
    }


# GET and HEAD are registered separately rather than as one api_route: FastAPI
# emits one OpenAPI operation per method, so a shared operation_id collides and
# the generated spec warns. HEAD stays out of the schema and reuses the body.
@app.head("/health", include_in_schema=False)
@app.get("/health")
async def health():
    """Liveness. Production gates deploys on this and UptimeRobot sends HEAD
    (a GET-only route answers those with 405), so both verbs answer here."""
    packs, _, dictionary = corpus.load()
    return {
        "status": "healthy",
        "service": SERVICE,
        "version": VERSION,
        "mode": "sandbox",
        "simulated": True,
        "uptime_seconds": round(time.time() - STARTED, 1),
        "db": None,
        "embedder_ready": None,
        "capabilities": {
            "ingestion":  ["http", "csv"],
            "prediction": ["breach", "fleet_health"],
            "normalization_layers": ["corpus", "identity", "signal_classifier"],
        },
        "not_included": [
            "production mapping corpus",
            "embedding similarity layer",
            "LLM field research",
            "physics validators",
            "TimesFM forecasting",
            "persistence, identity, triggers, guardrails, billing",
        ],
        "packs": {oem: len(p.mappings) for oem, p in sorted(packs.items())},
        "canonical_fields": dictionary["field_count"],
        "mcp": {"mounted": MCP_APP is not None, "path": "/mcp",
                "transport": "streamable-http", "error": MCP_ERROR},
        "timestamp": _now(),
    }


# ── Coverage ─────────────────────────────────────────────────────────────────

@app.get("/v1/coverage")
async def coverage(oem: Optional[str] = None):
    """What can this sandbox normalize? Ask before you integrate."""
    packs, _, dictionary = corpus.load()

    if oem:
        pack = corpus.get_pack(oem)
        if not pack:
            return {
                "oem": oem,
                "recognized": False,
                "vertical": None,
                "note": ("Unknown OEM. Normalization still runs — the pack "
                         "lookup is skipped and the signal classifier handles "
                         "what it can. Production behaves the same way."),
                "known_oems": sorted(packs),
                "simulated": True,
            }
        return {
            "oem": pack.oem,
            "recognized": True,
            "display_name": pack.display_name,
            "vertical": pack.vertical,
            "protocol": pack.protocol,
            "aliases": pack.aliases,
            "mapping_count": len(pack.mappings),
            "canonical_fields": pack.canonical_fields,
            "source": pack.source,
            "simulated": True,
        }

    verticals = {}
    for entry in dictionary["fields"].values():
        v = entry.get("vertical") or "unknown"
        verticals[v] = verticals.get(v, 0) + 1

    return {
        "canonical_schema": {
            "base_version": dictionary["base_version"],
            "license": dictionary["license"],
            "upstream": dictionary["upstream"],
            "field_count": dictionary["field_count"],
            "fields_per_vertical": dict(sorted(verticals.items())),
        },
        "oems": {oem_: {"display_name": p.display_name, "vertical": p.vertical,
                        "protocol": p.protocol, "aliases": p.aliases,
                        "mapping_count": len(p.mappings)}
                 for oem_, p in sorted(packs.items())},
        "total_mappings": sum(len(p.mappings) for p in packs.values()),
        "note": ("Sandbox subset. Production covers 18 OEM families and 16,908 "
                 "curated mappings, plus embedding and LLM resolution for tags "
                 "no pack has seen."),
        "simulated": True,
    }


@app.get("/v1/canonical-fields")
async def canonical_fields(vertical: Optional[str] = None,
                           prefix: Optional[str] = None):
    """The canonical dictionary itself: name, type, unit, vertical."""
    _, _, dictionary = corpus.load()
    fields = dictionary["fields"]
    out = {
        name: entry for name, entry in fields.items()
        if (vertical is None or entry.get("vertical") == vertical)
        and (prefix is None or name.startswith(prefix))
    }
    return {"count": len(out), "fields": dict(sorted(out.items())),
            "license": dictionary["license"], "upstream": dictionary["upstream"]}


# ── Simulated equipment ──────────────────────────────────────────────────────

@app.get("/v1/machines")
async def machines():
    """The five simulated machines and how to address them."""
    return {"count": len(simulate.MACHINES), "machines": simulate.list_machines(),
            "simulated": True}


@app.get("/v1/simulate/{machine}")
async def simulate_reading(machine: str, seed: Optional[int] = None):
    """One raw reading in the machine's own vendor tag names — exactly what you
    would get off the wire. POST the `data` object straight to /v1/normalize."""
    if machine not in simulate.MACHINES:
        raise HTTPException(404, detail={
            "error": "unknown_machine", "machine": machine,
            "known": sorted(simulate.MACHINES)})
    out = simulate.reading(machine, seed)
    out["simulated"] = True
    out["timestamp"] = _now()
    return out


@app.get("/v1/simulate/{machine}/series")
async def simulate_series(machine: str, field: str, points: int = 48,
                          seed: Optional[int] = None):
    """History for one RAW tag, oldest -> newest. Feed it to /v1/predict_breach,
    which is stateless and will not fetch a series for you."""
    if machine not in simulate.MACHINES:
        raise HTTPException(404, detail={
            "error": "unknown_machine", "machine": machine,
            "known": sorted(simulate.MACHINES)})
    if not 2 <= points <= 512:
        raise HTTPException(422, detail={"error": "bad_points",
                                         "message": "points must be 2..512"})
    try:
        values = simulate.series(machine, field, points, seed)
    except KeyError:
        sample = simulate.reading(machine, seed)["data"]
        raise HTTPException(404, detail={
            "error": "unknown_field", "field": field,
            "available": sorted(sample)})
    except ValueError as exc:
        raise HTTPException(422, detail={"error": "non_numeric_field",
                                         "message": str(exc)})

    # Resolve the raw tag so the caller knows which canonical field the series
    # represents -- predict_breach wants that name.
    _, _, dictionary = corpus.load()
    pack = corpus.get_pack(simulate.MACHINES[machine]["oem"])
    rec = corpus.resolve_field(field, pack, dictionary,
                               oem=simulate.MACHINES[machine]["oem"])
    return {
        "machine": machine, "raw_field": field,
        "canonical_field": corpus.resolve_canonical(rec["canonical_field"]),
        "match_type": rec["match_type"],
        "points": len(values), "values": values,
        "simulated": True, "timestamp": _now(),
    }


# ── Normalize ────────────────────────────────────────────────────────────────

class NormalizeRequest(BaseModel):
    data: dict[str, Any] = Field(
        ..., description="Flat {raw_vendor_tag: value} reading.")
    machine_id: Optional[str] = Field(None, max_length=200)
    oem: Optional[str] = Field(
        None, max_length=64,
        description="OEM hint. Selects the mapping pack. Unknown values are "
                    "allowed — resolution falls back to the signal classifier.")
    model: Optional[str] = Field(None, max_length=128)
    serial: Optional[str] = Field(None, max_length=128)
    site: Optional[str] = Field(None, max_length=128)
    observed_at: Optional[str] = Field(None, max_length=64)
    locale: Optional[str] = Field(
        None, max_length=16,
        description="BCP-47 / CLDR locale of the NUMBER FORMAT this controller "
                    "writes, e.g. de_DE, fr_FR, en_IN. Only needed when values "
                    "arrive as strings: '1.842' is 1842 in de_DE and 1.842 in "
                    "en_US, and a bare '1,234' is genuinely ambiguous. Without "
                    "this the OEM's home convention is used, and a value that "
                    "reads differently under two conventions is REFUSED rather "
                    "than guessed.")
    sunspec_model: Optional[Union[int, list[int]]] = Field(
        None,
        description="SunSpec model id (101, 103, 124, 203, 802 …) for a reading "
                    "of RAW registers, or a LIST of ids for a device that "
                    "advertises several blocks — a BESS exposes 124 (storage "
                    "control) and 802 (battery measurements) in one register "
                    "map, and scaling against either alone leaves the other "
                    "block unscaled. Scale factors are resolved from the "
                    "published model definition, which is the only way to see a "
                    "SHARED factor such as A_SF governing AphA/AphB/AphC. "
                    "Omit it and the model is taken from the ID register if the "
                    "reading carries one, then from the pack's default.")
    metadata: Optional[dict] = None


MAX_BODY_BYTES = int(os.environ.get("SANDBOX_MAX_BODY_BYTES", 1_000_000))
MAX_FIELDS = int(os.environ.get("SANDBOX_MAX_FIELDS", 2000))


def _normalize_payload(data, oem, machine_id=None, model=None, serial=None,
                       site=None, observed_at=None, rows=1, is_csv=False,
                       csv_rows=None, sunspec_model=None, locale=None):
    normalized, field_mappings, stats, unit_conv, collisions, null_states, enum_states = \
        corpus.normalize_row(data, oem, sunspec_model=sunspec_model, locale=locale)

    pack = corpus.get_pack(oem)
    unresolved = sorted(t for t, r in field_mappings.items()
                        if r["match_type"] == "unknown")

    ingested = _now()
    payload = {
        "normalized":       csv_rows if is_csv else normalized,
        "field_mappings":   field_mappings,
        "fields_total":     stats["fields_total"],
        "fields_mapped":    stats["fields_mapped"],
        "fields_unknown":   stats["fields_unknown"],
        "fields_distinct_canonical": stats["fields_distinct_canonical"],
        "coverage_pct":     stats["coverage_pct"],
        "collisions":       collisions or None,
        # Present only for SunSpec readings. Carries every scale factor applied
        # (raw -> scaled, with the exponent and which register supplied it) and
        # every diagnostic, so a 100x correction is reviewable rather than an
        # unexplained number change.
        "sunspec":          stats.get("sunspec"),
        "value_coercions":  stats.get("value_coercions"),
        "normalization_layers": {
            "layer1_deterministic": stats["layer1_deterministic"],
            "layer2_identity":      stats["layer2_identity"],
            "layer3_signal":        stats["layer3_signal"],
        },
        "unit_conversions": unit_conv or None,
        "null_states": null_states or None,
        "enum_states": enum_states or None,
        "unresolved_tags":  unresolved or None,
        "oem":              oem,
        "oem_recognized":   pack is not None,
        "vertical":         pack.vertical if pack else None,
        "machine_id":       machine_id,
        "model":            model,
        "serial":           serial,
        "site":             site,
        "rows":             rows,
        "observed_at":      observed_at or ingested,
        "ingested_at":      ingested,
        "timestamp":        ingested,
        # Production fields that only a stateful kernel can populate. They are
        # present and null rather than absent, so client code that reads them
        # behaves identically against both.
        "history_id":       None,
        "triggers_fired":   [],
        "guardrails_triggered": [],
        "simulated":        True,
        "sandbox_note":     ("Deterministic resolution only. Production adds "
                             "embedding similarity, LLM research, physics "
                             "validation, and self-healing mapping."),
    }

    # ── THE RELIEF VALVE — absolutely last, on the finished response ───────
    # Checks what must never be true of an answer regardless of how it got
    # there. Clean pipeline => finds nothing, costs microseconds. Buggy
    # pipeline => the value is nulled with a reason and the violation logged,
    # so an evaluator never sees a sentinel, a string in a float field, an OPC
    # wrapper or a NaN, even from a bug we have not found yet.
    #
    # A CSV response carries a list of rows rather than one field map, so the
    # per-field invariants do not apply to it; the checker no-ops on those.
    try:
        _viol = check_response_invariants(payload)
        if _viol:
            # Reported, never silent: a corrected response must be
            # distinguishable from one that was right the first time.
            payload["_invariant_violations"] = len(_viol)
    except Exception as exc:                       # never take down a response
        log.warning("invariant checker failed: %s: %s", type(exc).__name__, exc)
    return payload


@app.post("/v1/normalize")
async def v1_normalize(request: Request):
    """Normalize raw OEM telemetry into canonical form.

    Accepts EITHER:
      application/json — {"data": {tag: value, ...}, "oem"?, "machine_id"?, ...}
      text/csv         — raw CSV body, header row required; hints via
                         X-OEM / X-Machine-Id / X-Model / X-Serial / X-Site

    No Authorization header. No key. No quota.
    """
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        return JSONResponse(status_code=413, content={
            "error": "body_too_large", "received": len(raw),
            "max_bytes": MAX_BODY_BYTES, "service": SERVICE})

    if content_type in ("text/csv", "application/csv"):
        oem = (request.headers.get("x-oem") or "").lower() or None
        try:
            reader = csv.DictReader(io.StringIO(raw.decode("utf-8")))
            rows = list(reader)
        except Exception as exc:
            return JSONResponse(status_code=422, content={
                "error": "csv_parse_failed", "message": str(exc), "service": SERVICE})
        if not rows:
            return JSONResponse(status_code=422, content={
                "error": "empty_csv",
                "message": "CSV needs a header row and at least one data row.",
                "service": SERVICE})
        if len(rows[0]) > MAX_FIELDS:
            return JSONResponse(status_code=413, content={
                "error": "too_many_fields", "received": len(rows[0]),
                "max_fields": MAX_FIELDS, "service": SERVICE})

        typed = [{k: _coerce(v) for k, v in row.items() if k is not None}
                 for row in rows]
        # X-SunSpec-Model on the CSV path, mirroring the JSON body field. Passed
        # to EVERY row, not just the summary one — otherwise a CSV upload
        # returns a correctly scaled header row and unscaled data beneath it.
        _ss = request.headers.get("x-sunspec-model")
        try:
            _ss = int(_ss) if _ss else None
        except ValueError:
            _ss = None
        out_rows = [corpus.normalize_row(row, oem, sunspec_model=_ss)[0]
                    for row in typed]
        return _normalize_payload(
            typed[0], oem, sunspec_model=_ss,
            machine_id=request.headers.get("x-machine-id") or None,
            model=request.headers.get("x-model") or None,
            serial=request.headers.get("x-serial") or None,
            site=request.headers.get("x-site") or None,
            observed_at=request.headers.get("x-observed-at") or None,
            rows=len(out_rows), is_csv=True, csv_rows=out_rows)

    try:
        body = NormalizeRequest.model_validate_json(raw)
    except Exception as exc:
        return JSONResponse(status_code=422, content={
            "error": "invalid_body",
            "message": "Send {\"data\": {tag: value, ...}, \"oem\": \"haas\"} "
                       "as application/json, or a CSV body as text/csv.",
            "detail": str(exc)[:500], "service": SERVICE})

    if not body.data:
        return JSONResponse(status_code=422, content={
            "error": "empty_data",
            "message": "`data` must contain at least one {tag: value} pair.",
            "service": SERVICE})
    if len(body.data) > MAX_FIELDS:
        return JSONResponse(status_code=413, content={
            "error": "too_many_fields", "received": len(body.data),
            "max_fields": MAX_FIELDS, "service": SERVICE})

    # Non-finite floats. JSON has no Infinity or NaN, but Python's own encoder
    # emits them by default and `1e309` overflows to inf on parse — so they
    # arrive without anyone meaning to send them. They used to be accepted,
    # travel through normalization untouched (an unresolved tag keeps its raw
    # value by design), and then break the RESPONSE encoder:
    #
    #   POST /v1/normalize {"oem":"haas","data":{"S1Temp":1e309}}
    #     -> ValueError: Out of range float values are not JSON compliant: inf
    #     -> HTTP 500, unauthenticated, from a 38-byte body
    #
    # Rejected at the boundary rather than nulled downstream: the value is not
    # representable in the protocol the caller claims to be speaking, so the
    # honest answer is that the request is malformed.
    for _k, _v in body.data.items():
        _floats = ([_v] if isinstance(_v, float)
                   else [e for e in _v if isinstance(e, float)]
                   if isinstance(_v, list) else [])
        if any(f != f or f in (float("inf"), float("-inf")) for f in _floats):
            return JSONResponse(status_code=422, content={
                "error": "non_finite_value",
                "field": f"data.{_k}",
                "message": ("Telemetry values must be finite. JSON has no "
                            "Infinity or NaN; send null for a missing reading."),
                "received": repr(_v)[:80], "service": SERVICE})

    return _normalize_payload(
        body.data, (body.oem or "").lower() or None,
        machine_id=body.machine_id, model=body.model, serial=body.serial,
        site=body.site, observed_at=body.observed_at,
        sunspec_model=body.sunspec_model, locale=body.locale)


def _coerce(value):
    """CSV gives strings. Numbers should arrive as numbers, so the unit
    converter and the forecaster can work on them."""
    if value is None:
        return None
    s = value.strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        f = float(s)
        # A CSV cell of "inf" / "nan" parses to a non-finite float, which the
        # response encoder cannot serialise — the same HTTP 500 the JSON path
        # had. Left as the original STRING: the sentinel gate already treats
        # "NaN" and "inf" as non-values, so it becomes a flagged null.
        if f != f or f in (float("inf"), float("-inf")):
            return value
        return f
    except ValueError:
        pass
    low = s.lower()
    if low in ("true", "false"):
        return low == "true"
    return s


# ── Prediction ───────────────────────────────────────────────────────────────

class PredictBreachRequest(BaseModel):
    """STATELESS, exactly like production: you supply the series every call.
    {machine_id, field, threshold} alone is a 422."""
    time_series: list[float] = Field(
        ..., min_length=1,
        description="Numeric readings, oldest to newest. 16+ recommended.")
    threshold: float = Field(..., description="The value to test for a crossing.")
    canonical_field: Optional[str] = Field(None, max_length=128)
    direction: str = Field("above", pattern="^(above|below)$")
    horizon: int = Field(96, ge=1, le=256)
    mint_id: Optional[str] = Field(None, max_length=200)
    settle: bool = Field(False)


@app.post("/v1/predict_breach")
async def v1_predict_breach(req: PredictBreachRequest):
    """Predict whether — and when — a series crosses a threshold.

    Same contract as production: will_breach, estimated_steps_to_breach,
    confidence, breach_window {earliest, latest}. The forecast is a
    deterministic least-squares fit, NOT TimesFM, and every response says so.
    """
    warnings = []
    if len(req.time_series) < predict.MIN_POINTS:
        warnings.append(
            f"time_series has {len(req.time_series)} points; production wants "
            f"at least {predict.MIN_POINTS}. Short series forecast poorly here "
            f"and will be rejected there.")
    if req.canonical_field:
        _, _, dictionary = corpus.load()
        resolved = corpus.resolve_canonical(req.canonical_field)
        if resolved not in dictionary["fields"]:
            warnings.append(
                f"'{req.canonical_field}' is not a canonical field in the "
                f"shipped schema. Production validates this and will 422.")
        elif resolved != req.canonical_field:
            warnings.append(
                f"'{req.canonical_field}' is an alias; the primary canonical "
                f"is '{resolved}'.")

    result = predict.predict_threshold_breach(
        req.time_series, req.threshold, direction=req.direction,
        horizon=req.horizon)
    if result.get("error"):
        return JSONResponse(status_code=422, content={**result, "service": SERVICE})

    if req.canonical_field:
        result["canonical_field"] = req.canonical_field
    if warnings:
        result["field_warnings"] = warnings

    # Production returns a real attestation and can settle it on-chain. The
    # sandbox computes the same data_hash but settles nothing.
    result["attestation"] = {
        "data_hash": predict.data_hash({
            "time_series": req.time_series, "threshold": req.threshold,
            "direction": req.direction, "horizon": req.horizon,
            "canonical_field": req.canonical_field}),
        "settled": False,
        "note": "Sandbox attestation. Not anchored; not verifiable externally.",
    }
    if req.settle:
        result["attestation"]["settle_requested_but_ignored"] = True
    result["billing"] = {"charged_usd": 0.0, "production_rate_usd": 0.05}
    result["timestamp"] = _now()
    return result


class MachineSpec(BaseModel):
    id: str = Field("unknown", max_length=200)
    canonical_field: Optional[str] = Field(None, max_length=128)
    values: list[float] = Field(default_factory=list)
    threshold: Optional[float] = None
    direction: str = Field("above", pattern="^(above|below)$")


class FleetHealthRequest(BaseModel):
    machines: list[MachineSpec] = Field(
        default_factory=list, description="Up to 100 machines to score.")


@app.post("/v1/fleet_health")
async def v1_fleet_health(req: FleetHealthRequest):
    """Roll a fleet of predictions into one health view: a score, a
    critical/elevated/moderate/healthy distribution, per-field rollups, and a
    maintenance queue. Machines that cannot be scored are reported as unscored,
    never counted as healthy."""
    if not req.machines:
        return JSONResponse(status_code=422, content={
            "error": "empty_batch", "message": "machines array is required",
            "service": SERVICE})
    if len(req.machines) > 100:
        return JSONResponse(status_code=400, content={
            "error": "batch_too_large", "message": "maximum 100 machines per batch",
            "service": SERVICE})

    out = predict.fleet_health([m.model_dump() for m in req.machines])
    out["attestation"] = {
        "data_hash": predict.data_hash(out["fleet_health"]),
        "settled": False,
        "note": "Sandbox attestation. Not anchored; not verifiable externally.",
    }
    out["timestamp"] = _now()
    return out


@app.post("/v1/predict_batch")
async def v1_predict_batch(req: FleetHealthRequest):
    """Per-machine numbers without the fleet rollup."""
    if not req.machines:
        return JSONResponse(status_code=422, content={
            "error": "empty_batch", "message": "machines array is required",
            "service": SERVICE})
    out = predict.predict_batch([m.model_dump() for m in req.machines])
    out["simulated"] = True
    out["timestamp"] = _now()
    return out


# ── Endpoints that exist in production but cannot exist here ─────────────────

_STATEFUL = {
    "/v1/identify":              "machine identity is issued by the kernel",
    "/v1/history":               "the sandbox persists nothing",
    "/v1/guardrails":            "guardrails are enforced server-side",
    "/v1/triggers":              "triggers need durable state and webhooks",
    "/v1/attest":                "attestation settles on-chain",
    "/v1/billing/usage":         "nothing is metered here",
}


@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE"],
               include_in_schema=False)
async def _unimplemented(path: str, request: Request):
    """Anything else under /v1 gets a straight answer about why it is missing,
    rather than a bare 404 that reads like a bug in your client."""
    route = "/v1/" + path.rstrip("/")
    for known, why in _STATEFUL.items():
        if route.startswith(known):
            return JSONResponse(status_code=501, content={
                "error": "not_in_sandbox", "endpoint": route, "reason": why,
                "available_in_production": True,
                "upgrade": "https://foundrynet.io", "service": SERVICE})
    return JSONResponse(status_code=404, content={
        "error": "unknown_endpoint", "endpoint": route,
        "available": ["/v1/normalize", "/v1/predict_breach", "/v1/fleet_health",
                      "/v1/predict_batch", "/v1/coverage", "/v1/canonical-fields",
                      "/v1/machines", "/v1/simulate/{machine}"],
        "service": SERVICE})


@app.get("/robots.txt", include_in_schema=False)
async def robots():
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


# ── MCP ──────────────────────────────────────────────────────────────────────
# Mounted last so the explicit routes above always win. Streamable HTTP, the
# transport production uses; SSE is not served here (Smithery and Claude
# Desktop both want Streamable HTTP anyway).

class _McpExactPath:
    """Serve the MCP endpoint at /mcp without a redirect.

    Mounting an ASGI app whose own route is "/" at "/mcp" makes Starlette answer
    a request for "/mcp" with a 307 to "/mcp/". A 307 preserves method and body,
    so a well-behaved client recovers -- but plenty of MCP clients do not follow
    redirects on POST at all, and the ones that do pay an extra round trip on
    every call. Since /mcp is the URL in the README and in every client config,
    it should be the URL that works. Rewriting the path before routing costs one
    dict copy and removes the redirect entirely.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("path") == "/mcp":
            scope = dict(scope, path="/mcp/", raw_path=b"/mcp/")
        await self.app(scope, receive, send)


if MCP_APP is not None:
    app.mount("/mcp", MCP_APP)
    app.add_middleware(_McpExactPath)
else:
    @app.api_route("/mcp", methods=["GET", "POST"], include_in_schema=False)
    @app.api_route("/mcp/{rest:path}", methods=["GET", "POST"],
                   include_in_schema=False)
    async def _mcp_unavailable(rest: str = ""):
        return JSONResponse(status_code=503, content={
            "error": "mcp_unavailable",
            "message": "The MCP server failed to start; REST endpoints are "
                       "unaffected. Check that fastmcp installed correctly.",
            "detail": MCP_ERROR, "service": SERVICE})
