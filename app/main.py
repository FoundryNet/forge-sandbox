"""Forge sandbox — the Forge API surface, locally, with no key and no account.

Same request bodies and same response shapes as production Forge. Simulated
data, real canonical schema. Everything is in-process: no database, no network
egress, nothing written to disk.
"""

import contextlib
import io
import csv
import json
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

@contextlib.asynccontextmanager
async def _lifespan(app_):
    """Start the satellite heartbeat daemon, then hand off to the MCP lifespan.

    Passing an explicit `lifespan=` makes Starlette IGNORE every @app.on_event
    handler, so the satellite has to start from inside this function -- an
    on_event("startup") hook here is silently dead code, which is exactly how
    the first wiring of this failed: agent enabled, zero heartbeats, no error.
    """
    try:
        from app.satellite import AGENT
        AGENT.start()
    except Exception as exc:
        logging.getLogger("forge").warning("satellite start failed: %s", exc)
    if MCP_APP is not None:
        async with MCP_APP.lifespan(app_):
            yield
    else:
        yield
    try:
        from app.satellite import AGENT
        AGENT.stop()
    except Exception:
        pass


app = FastAPI(
    title="Forge Sandbox",
    version=VERSION,
    lifespan=_lifespan,
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
@app.post("/_satellite/beat", include_in_schema=False)
async def _satellite_beat(request: Request):
    """Force a heartbeat now, instead of waiting out the interval.

    Operationally useful (an operator who just fixed a licence wants the engine
    to re-check immediately) and it is what makes the fleet behaviour testable
    without 60-second waits. Guarded by the engine's own bearer token, so it is
    not an open trigger on a public port.
    """
    from app.satellite import AGENT
    if not AGENT.enabled():
        return JSONResponse(status_code=404, content={"error": "satellite_disabled"})
    sent = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
    if sent != AGENT.token:
        return JSONResponse(status_code=401, content={"error": "bad_token"})
    ok, resp = AGENT.beat()
    return {"ok": ok, "response": resp, "rtt_ms": AGENT.last_rtt_ms,
            "corpus_version": AGENT.corpus_version,
            "fleet_overlay_version": corpus.fleet_overlay_version(),
            "queued": len(AGENT._queued()),
            "rejected_deltas": AGENT.rejected_deltas,
            "license_valid": AGENT.license_valid}


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
        "satellite": _satellite_health(),
        "timestamp": _now(),
    }


def _satellite_health():
    """Fleet state, so an operator can see corpus version and heartbeat health
    without shelling into the container. Absent when the agent is not enabled."""
    try:
        from app.satellite import AGENT
        if not AGENT.enabled():
            return {"enabled": False}
        return {
            "enabled": True,
            "engine_id": AGENT.engine_id,
            "license_id": AGENT.license_id,
            "license_valid": AGENT.license_valid,
            "corpus_version": AGENT.corpus_version,
            "fleet_overlay_version": corpus.fleet_overlay_version(),
            "control_plane": AGENT.control_plane,
            "heartbeat_interval_s": AGENT.interval_s,
            "last_rtt_ms": AGENT.last_rtt_ms,
            "last_ack_at": (AGENT.last_ack or {}).get("server_time"),
            "queued_heartbeats": len(AGENT._queued()),
            "rejected_deltas": AGENT.rejected_deltas,
            "token_rotations": AGENT.rotations,
        }
    except Exception as exc:
        return {"enabled": False, "error": f"{type(exc).__name__}: {exc}"}


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


# ── Quality telemetry ────────────────────────────────────────────────────────

@app.get("/v1/quality")
async def quality():
    """The quality half of the heartbeat, readable locally.

    Same numbers the satellite ships to the control plane -- evidence-gate
    refusals, relief-valve fires, the confidence distribution and coverage by
    OEM -- so an operator can see engine health without a control plane and
    without shelling into the container.

    READ-ONLY BY CONSTRUCTION: this calls `quality_snapshot()`, never
    `snapshot()`. `snapshot()` resets the counters on the way out, so polling
    this endpoint would silently drain the very metrics the next heartbeat is
    supposed to report -- monitoring that destroys what it measures.
    """
    from app.satellite import COUNTERS
    return {
        "window": "since last heartbeat",
        "resets_on_read": False,
        "quality": COUNTERS.quality_snapshot(),
        "timestamp": _now(),
    }


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


def _wants_context(request) -> bool:
    """?include_context=true. Default FALSE -- backwards compatible."""
    return (request.query_params.get("include_context") or "").strip().lower() \
        in ("1", "true", "yes", "on")


MAX_BODY_BYTES = int(os.environ.get("SANDBOX_MAX_BODY_BYTES", 1_000_000))
MAX_FIELDS = int(os.environ.get("SANDBOX_MAX_FIELDS", 2000))


def _build_field_context(field_mappings, emitted):
    """Per-field metadata for UNS / CDM consumers.

    Assembly only -- every value here is already computed somewhere in the
    pipeline, and this reads it rather than deriving it a second time. A second
    derivation is a second thing to disagree with the first.

      unit, physical_quantity, isa95_category  <- the canonical dictionary
      confidence, match_type                   <- the resolution record

    Keyed by CANONICAL field, matching `normalized`, because that is what a
    consumer subscribes to. The raw tag that produced it rides along as
    `source_tag` so the mapping stays auditable from either end.
    """
    try:
        _, _, dictionary = corpus.load()
        fields = dictionary.get("fields", {})
    except Exception:
        fields = {}

    # A canonical field can be claimed by more than one raw tag (unit variants,
    # vendor spellings). The highest-confidence record wins, so the context
    # describes the reading that was actually emitted.
    best = {}
    for tag, rec in (field_mappings or {}).items():
        if not isinstance(rec, dict):
            continue
        cf = rec.get("canonical_field")
        if not cf:
            continue
        prior = best.get(cf)
        if prior is None or (rec.get("confidence") or 0) > (prior[1].get("confidence") or 0):
            best[cf] = (tag, rec)

    ctx = {}
    for name in emitted:
        spec = fields.get(name) or {}
        # An unresolved tag passes through under its RAW name, so it never
        # appears in `best` (which is keyed by canonical field). Fall back to a
        # direct lookup so a passthrough reports match_type "unknown" rather
        # than a row of nulls that reads like "we have no idea what this is" --
        # we do know: we know we could not resolve it, which is different.
        tag, rec = best.get(name, (None, None))
        if rec is None:
            direct = (field_mappings or {}).get(name)
            rec = direct if isinstance(direct, dict) else {}
            tag = name if direct else None
        ctx[name] = {
            "unit": spec.get("unit"),
            "physical_quantity": spec.get("physical_quantity"),
            "isa95_category": spec.get("isa95_category"),
            "confidence": rec.get("confidence"),
            "match_type": rec.get("match_type"),
            "source_tag": tag,
        }
    return ctx


def _normalize_payload(data, oem, machine_id=None, model=None, serial=None,
                       site=None, observed_at=None, rows=1, is_csv=False,
                       csv_rows=None, sunspec_model=None, locale=None,
                       include_context=False):
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

    # Opt-in enrichment. Default off: an existing integration must not have
    # its response shape change under it because we shipped a new key.
    if include_context:
        emitted = (sorted({r["canonical_field"] for r in field_mappings.values()
                           if isinstance(r, dict) and r.get("canonical_field")})
                   if is_csv else list(normalized.keys()))
        payload["field_context"] = _build_field_context(field_mappings, emitted)

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

    # ── METERING + FLEET QUALITY SIGNAL ───────────────────────────────────
    # Runs AFTER the relief valve, because a valve fire is one of the things
    # worth reporting and it does not exist until the valve has run.
    #
    # Tag NAMES, field NAMES and COUNTS only -- the control plane learns that
    # `Axis_1.ActVel` went unresolved 40 times on a siemens line, and never
    # learns what it read. Metering must never be able to fail a normalize, so
    # it is fully contained.
    try:
        from app.satellite import COUNTERS as _sat_counters
        _sat_counters.record_normalize(
            unresolved_tags=unresolved, oem=oem, machine_id=machine_id,
            kind="normalize_csv" if is_csv else "normalize",
            field_mappings=field_mappings,
            null_states=null_states,
            coercions=stats.get("value_coercions") or (),
            relief_valve_fires=payload.get("_invariant_violations", 0),
            fields_total=stats["fields_total"],
            fields_mapped=stats["fields_mapped"])
    except Exception:
        pass
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
            rows=len(out_rows), is_csv=True, csv_rows=out_rows,
            include_context=_wants_context(request))

    try:
        body = NormalizeRequest.model_validate_json(raw)
    except Exception as exc:
        return JSONResponse(status_code=422, content={
            "error": "invalid_body",
            "message": "Send {\"data\": {tag: value, ...}, \"oem\": \"haas\"} "
                       "as application/json, or a CSV body as text/csv.",
            "detail": str(exc)[:500], "service": SERVICE})

    # An unknown top-level key is usually a caller asking for something the
    # sandbox does not do -- `{"write": true}` being the common one. Accepting
    # and silently dropping it is the worst answer: the caller has no way to
    # tell the write did not happen. Say so.
    try:
        _sent = json.loads(raw)
    except ValueError:
        _sent = {}
    if isinstance(_sent, dict):
        _known = set(NormalizeRequest.model_fields)
        _extra = sorted(set(_sent) - _known)
        if _extra:
            return JSONResponse(status_code=422, content={
                "error": "unsupported_field",
                "unsupported": _extra,
                "message": ("Forge is read-only: it normalizes telemetry you "
                            "send and never writes back to a device. "
                            f"{', '.join(repr(k) for k in _extra)} is not a "
                            "field this endpoint accepts, and it was NOT "
                            "applied. Accepted fields: "
                            f"{', '.join(sorted(_known))}."),
                "service": SERVICE})

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
        sunspec_model=body.sunspec_model, locale=body.locale,
        include_context=_wants_context(request))


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
    # The route may exist under a different method. /v1/normalize is POST-only,
    # so a bare `curl localhost:8000/v1/normalize` -- no -X, no payload, which is
    # what a browser and a half-copied command both send -- lands here as a GET
    # and used to be told "unknown_endpoint" while that same endpoint was listed
    # two lines below as available. Reading that, the honest conclusion is that
    # the image is broken. Answer the actual question instead: the path is real,
    # the method is wrong, and here is the call that works.
    allowed = sorted({m for r in app.routes
                      if getattr(r, "path", None) == route
                      for m in (getattr(r, "methods", None) or set())
                      if m not in ("HEAD", "OPTIONS")}
                     - {request.method})
    if allowed:
        body = {"error": "method_not_allowed", "endpoint": route,
                "method": request.method, "allowed": allowed,
                "service": SERVICE}
        if route == "/v1/normalize":
            body["hint"] = ("POST a JSON body: "
                            "curl -X POST http://localhost:8000/v1/normalize "
                            "-H 'Content-Type: application/json' "
                            "-d '{\"oem\":\"haas\",\"data\":{\"S1Temp\":72.1}}'")
        return JSONResponse(status_code=405, content=body,
                            headers={"Allow": ", ".join(allowed)})

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
