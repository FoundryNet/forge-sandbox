"""MCP server for the Forge sandbox, mounted at /mcp.

The tools below carry production Forge's descriptions, because the description
IS the interface an agent reasons about. If a tool reads differently here than
it does in production, prompt-level behaviour you tuned against the sandbox
will not carry over -- which would defeat the point of the sandbox.

Production exposes 32 tools. This server exposes the 8 that can work with no
state, no account, and no network: the five real ones, plus three that hand you
simulated equipment to point them at. The other 24 need durable identity,
history, guardrails, triggers, billing, or on-chain settlement. Asking for one
by name returns a 501 from the REST side with the reason.
"""

from typing import Optional

from fastmcp import FastMCP

from . import corpus, predict, simulate

mcp = FastMCP("foundrynet-sandbox", version="1.0.0")

_SANDBOX = ("\n\nSANDBOX: this server simulates Forge locally. Canonical field "
            "names and vendor mappings are real; equipment readings and "
            "forecasts are simulated. No API key, no billing, no persistence. "
            "Production: https://forge.foundrynet.io")


def sandbox_tool(fn):
    """Register a tool, appending the sandbox note to its docstring.

    Not cosmetic. A triple-quoted literal followed by `+ CONST` is an
    expression, not a docstring: __doc__ comes out None and the tool ships with
    an EMPTY description -- the one thing an agent actually reads. Keeping the
    docstring pure and passing description= explicitly makes that impossible,
    and the empty-docstring guard below turns it into a startup failure rather
    than a silent one.
    """
    doc = (fn.__doc__ or "").strip()
    if not doc:
        raise ValueError(f"{fn.__name__} has no docstring to describe it")
    return mcp.tool(description=doc + _SANDBOX)(fn)


# ── Production tools ─────────────────────────────────────────────────────────

@sandbox_tool
async def normalize_telemetry(
    data: dict,
    machine_id: Optional[str] = None,
    oem: Optional[str] = None,
    model: Optional[str] = None,
    serial: Optional[str] = None,
    site: Optional[str] = None,
) -> dict:
    """Give your agent a semantic understanding of machine data from any OEM:
    translate raw vendor telemetry into one universal canonical schema (FCS,
    FoundryNet Canonical Schema) so the agent can reason across vendors it has
    never seen before.
    Maps vendor-specific column names like "Spindle_Speed", "servo_load_x",
    "CoolantTemp", "FeedRateOverride" into standard fields like
    spindle_speed_rpm, axes.x_load_pct, sensor_readings.coolant_temp,
    feed_override_pct.

    Accepts a `data` dict of {raw_field: value}. Pass `oem` to select the
    mapping pack; an unrecognized oem still normalizes, using the signal
    classifier alone.

    Each call returns the canonical reading plus a per-field mapping record
    saying HOW each tag resolved (corpus / identity / signal / unknown) and at
    what confidence. Tags that resolve to nothing keep their raw name and value
    rather than being dropped.

    USE WHEN: you have raw machine data — a CSV row, a sensor reading, an MES
    export, an alarm log line — and need to understand it semantically using
    canonical field names."""
    normalized, field_mappings, stats, unit_conv, collisions, null_states, enum_states = \
        corpus.normalize_row(data, (oem or "").lower() or None)
    pack = corpus.get_pack(oem)
    return {
        "normalized": normalized,
        "null_states": null_states or None,
        "enum_states": enum_states or None,
        "field_mappings": field_mappings,
        "coverage_pct": stats["coverage_pct"],
        "fields_total": stats["fields_total"],
        "fields_mapped": stats["fields_mapped"],
        "fields_distinct_canonical": stats["fields_distinct_canonical"],
        "unit_conversions": unit_conv or None,
        "collisions": collisions or None,
        "unresolved_tags": sorted(t for t, r in field_mappings.items()
                                  if r["match_type"] == "unknown") or None,
        "oem": oem, "oem_recognized": pack is not None,
        "vertical": pack.vertical if pack else None,
        "machine_id": machine_id, "model": model, "serial": serial, "site": site,
        "simulated": True,
    }


@sandbox_tool
async def get_coverage(oem: Optional[str] = None) -> dict:
    """Ask Forge what it can normalize BEFORE you try: the recognized OEM
    verticals (CNC / robot / vehicle / AMR / additive / building automation),
    the canonical-field families, and the field list per family. Optionally
    pass an `oem` to see which vertical it resolves to.

    USE WHEN: starting a new integration, or deciding whether to call
    normalize_telemetry — confirm the machine's OEM and your fields are in
    coverage. Unknown OEMs still normalize, so absence here is a soft signal,
    not a hard block."""
    packs, _, dictionary = corpus.load()
    if oem:
        pack = corpus.get_pack(oem)
        if not pack:
            return {"oem": oem, "recognized": False,
                    "known_oems": sorted(packs), "simulated": True}
        return {"oem": pack.oem, "recognized": True,
                "display_name": pack.display_name, "vertical": pack.vertical,
                "protocol": pack.protocol, "aliases": pack.aliases,
                "mapping_count": len(pack.mappings),
                "canonical_fields": pack.canonical_fields, "simulated": True}
    return {
        "oems": {o: {"display_name": p.display_name, "vertical": p.vertical,
                     "protocol": p.protocol, "mapping_count": len(p.mappings)}
                 for o, p in sorted(packs.items())},
        "canonical_field_count": dictionary["field_count"],
        "total_mappings": sum(len(p.mappings) for p in packs.values()),
        "note": ("Sandbox subset. Production covers 18 OEM families and 16,908 "
                 "curated mappings plus embedding and LLM resolution."),
        "simulated": True,
    }


@sandbox_tool
async def predict_breach(
    time_series: list[float],
    threshold: float,
    canonical_field: Optional[str] = None,
    direction: str = "above",
    horizon: int = 96,
    mint_id: Optional[str] = None,
    settle: bool = False,
) -> dict:
    """Predict whether — and when — a canonical series will cross a threshold.
    This is the parametric-insurance primitive: it answers "will this machine's
    <field> exceed <threshold> within the forecast window, and how soon?".

    Returns will_breach, estimated_steps_to_breach, a confidence, and a
    quantile-derived breach_window {earliest, latest}. Every result carries a
    deterministic data_hash so the prediction is reproducible.

    Args:
      time_series      historical canonical values, oldest→newest (≥16 recommended)
      threshold        the value to test for a crossing (e.g. 95.0 for 95% load)
      canonical_field  FCS field the series represents (e.g. "spindle_load_pct")
      direction        "above" (default) or "below" — which side is the breach
      horizon          steps to look ahead (1–256, default 96)
      mint_id          caller-owned machine to record provenance to (optional)
      settle           production only; ignored here

    USE WHEN: a user asks if/when a limit will be hit — "will spindle load breach
    95% this shift", "is coolant temp going to exceed 35°C", "alert me before
    pressure drops below 2 bar".

    The sandbox forecasts with least squares, not TimesFM. The response shape is
    identical; the numbers are not production's."""
    result = predict.predict_threshold_breach(
        time_series, threshold, direction=direction, horizon=horizon)
    if canonical_field:
        result["canonical_field"] = canonical_field
        _, _, dictionary = corpus.load()
        if corpus.resolve_canonical(canonical_field) not in dictionary["fields"]:
            result["field_warnings"] = [
                f"'{canonical_field}' is not a canonical field in the shipped "
                f"schema; production validates this and will 422."]
    result["attestation"] = {
        "data_hash": predict.data_hash({
            "time_series": time_series, "threshold": threshold,
            "direction": direction, "horizon": horizon,
            "canonical_field": canonical_field}),
        "settled": False,
    }
    if settle:
        result["attestation"]["settle_requested_but_ignored"] = True
    if mint_id:
        result["mint_id"] = mint_id
    return result


@sandbox_tool
async def fleet_health(machines: list[dict]) -> dict:
    """Roll a fleet of machine predictions up into a single health dashboard: a
    fleet health score, a critical/elevated/moderate/healthy risk distribution,
    per-canonical-field risk rollups, and a maintenance priority queue with a
    plain-English recommendation.

    Args:
      machines  list (≤100) of
                { id, canonical_field?, values:[...], threshold?, direction? }.
                Machines with a `threshold` are bucketed by steps-to-breach
                (critical <6, elevated <24, moderate otherwise); machines that
                cannot be scored are reported as unscored, never as healthy.

    USE WHEN: your agent needs to concentrate on where fleet risk is — "how
    healthy is my fleet", "give me the maintenance queue", "where's my risk
    concentrated". For raw per-machine numbers use predict_batch."""
    if len(machines) > 100:
        return {"error": "batch_too_large", "message": "maximum 100 machines"}
    if not machines:
        return {"error": "empty_batch", "message": "machines array is required"}
    return predict.fleet_health(machines)


@sandbox_tool
async def predict_batch(machines: list[dict]) -> dict:
    """Score many machines in one call and get per-machine predictions back,
    without the fleet-level rollup.

    Args:
      machines  list (≤100) of
                { id, canonical_field?, values:[...], threshold?, direction? }

    USE WHEN: you want the raw numbers per machine. For a prioritized fleet
    view use fleet_health instead."""
    if len(machines) > 100:
        return {"error": "batch_too_large", "message": "maximum 100 machines"}
    if not machines:
        return {"error": "empty_batch", "message": "machines array is required"}
    out = predict.predict_batch(machines)
    out["simulated"] = True
    return out


# ── Sandbox-only tools ───────────────────────────────────────────────────────
# Production has no equivalent: there, the telemetry comes from real equipment.

@sandbox_tool
async def list_sandbox_machines() -> dict:
    """List the simulated machines this sandbox can produce telemetry for: a
    Haas CNC, a FANUC robot, a Siemens SINUMERIK, a Prusa 3D printer, and a
    Carrier rooftop HVAC unit. Each emits its REAL vendor tag names.

    USE WHEN: you need machine data to work with and have no equipment to hand.
    SANDBOX ONLY — production reads from actual machines."""
    return {"count": len(simulate.MACHINES), "machines": simulate.list_machines(),
            "simulated": True}


@sandbox_tool
async def get_sandbox_reading(machine: str, seed: Optional[int] = None) -> dict:
    """Fetch one raw telemetry reading from a simulated machine, in that
    vendor's own tag names — the shape you would get off the wire. Feed the
    returned `data` object straight into normalize_telemetry.

    Args:
      machine  one of: haas, fanuc, siemens, prusa, carrier
      seed     fix the reading so it repeats exactly

    USE WHEN: you want to see what unnormalized vendor telemetry looks like, or
    need input for normalize_telemetry. SANDBOX ONLY."""
    if machine not in simulate.MACHINES:
        return {"error": "unknown_machine", "machine": machine,
                "known": sorted(simulate.MACHINES)}
    out = simulate.reading(machine, seed)
    out["simulated"] = True
    return out


@sandbox_tool
async def get_sandbox_series(machine: str, field: str, points: int = 48,
                             seed: Optional[int] = None) -> dict:
    """Fetch a history for one raw tag on a simulated machine, oldest→newest,
    together with the canonical field it maps to.

    predict_breach is stateless — it never fetches a series for you — so this is
    how you get one to forecast on.

    Args:
      machine  one of: haas, fanuc, siemens, prusa, carrier
      field    a RAW tag name from that machine (see get_sandbox_reading)
      points   how many readings, 2–512 (default 48)
      seed     fix the series so it repeats exactly

    USE WHEN: you need a time series to pass to predict_breach or fleet_health.
    SANDBOX ONLY."""
    if machine not in simulate.MACHINES:
        return {"error": "unknown_machine", "machine": machine,
                "known": sorted(simulate.MACHINES)}
    if not 2 <= points <= 512:
        return {"error": "bad_points", "message": "points must be 2..512"}
    try:
        values = simulate.series(machine, field, points, seed)
    except KeyError:
        sample = simulate.reading(machine, seed)["data"]
        return {"error": "unknown_field", "field": field,
                "available": sorted(sample)}
    except ValueError as exc:
        return {"error": "non_numeric_field", "message": str(exc)}

    _, _, dictionary = corpus.load()
    pack = corpus.get_pack(simulate.MACHINES[machine]["oem"])
    rec = corpus.resolve_field(field, pack, dictionary,
                               oem=simulate.MACHINES[machine]["oem"])
    return {"machine": machine, "raw_field": field,
            "canonical_field": corpus.resolve_canonical(rec["canonical_field"]),
            "match_type": rec["match_type"], "points": len(values),
            "values": values, "simulated": True}


def http_app(path: str = "/"):
    """The MCP ASGI app, Streamable HTTP. Mounted by app.main at /mcp."""
    return mcp.http_app(transport="http", path=path)
