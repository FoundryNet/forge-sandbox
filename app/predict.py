"""Deterministic forecasting for the Forge sandbox.

Production Forge forecasts with TimesFM, a 200M-parameter time-series
foundation model. That model is not in this image -- it would make the sandbox
a multi-gigabyte download and it is not what a developer is testing when they
wire up an integration.

What IS identical is the response contract: the same keys, the same types, the
same semantics for will_breach / estimated_steps_to_breach / breach_window /
confidence. Build against these shapes locally and the production endpoint is a
URL change.

The model here is ordinary least squares on the tail of the series, with a
residual-scaled quantile band. It is honest about being that: every response
carries model "sandbox-ols-v1" and a `simulated: true` flag, so nothing that
comes out of this container can be mistaken for a real prediction.
"""

import hashlib
import json
import math

MODEL_NAME = "sandbox-ols-v1"

# Production wants >= 16 points and warns below that. Same threshold here, so a
# series that is too short fails in the sandbox instead of in production.
MIN_POINTS = 16


def _ols(values):
    """Slope and intercept over index, by least squares."""
    n = len(values)
    mean_x = (n - 1) / 2.0
    mean_y = sum(values) / n
    sxx = sum((i - mean_x) ** 2 for i in range(n))
    if sxx == 0:
        return 0.0, mean_y
    sxy = sum((i - mean_x) * (v - mean_y) for i, v in enumerate(values))
    slope = sxy / sxx
    return slope, mean_y - slope * mean_x


def _residual_sigma(values, slope, intercept):
    n = len(values)
    if n <= 2:
        return 0.0
    ss = sum((v - (slope * i + intercept)) ** 2 for i, v in enumerate(values))
    return math.sqrt(ss / (n - 2))


def forecast(values, horizon=96):
    """Point forecast plus 0.1/0.9 quantile paths, `horizon` steps ahead.

    The fit uses the recent tail rather than the whole series: a machine that
    warmed up two hours ago and has been steady since should not have that ramp
    dragged into its forecast.
    """
    n = len(values)
    tail = values[-min(n, 32):]
    slope, intercept = _ols(tail)
    sigma = _residual_sigma(tail, slope, intercept)
    last_index = len(tail) - 1

    point, q10, q90 = [], [], []
    for step in range(1, horizon + 1):
        mu = slope * (last_index + step) + intercept
        # Uncertainty widens with the square root of the horizon, the usual
        # random-walk growth. A floor keeps a perfectly straight series from
        # claiming a zero-width band.
        spread = (sigma + abs(mu) * 1e-3) * 1.2816 * math.sqrt(step)
        point.append(round(mu, 6))
        q10.append(round(mu - spread, 6))
        q90.append(round(mu + spread, 6))
    return {"point_forecast": point,
            "quantile_forecasts": {"0.1": q10, "0.9": q90},
            "slope_per_step": round(slope, 6),
            "residual_sigma": round(sigma, 6)}


def _first_cross(path, threshold, direction):
    for i, v in enumerate(path):
        if (direction == "above" and v >= threshold) or \
           (direction == "below" and v <= threshold):
            return i
    return None


def _summary(path, current):
    return {
        "current": round(current, 4),
        "forecast_min": round(min(path), 4),
        "forecast_max": round(max(path), 4),
        "forecast_end": round(path[-1], 4),
        "direction_of_travel": ("rising" if path[-1] > current * 1.001
                                else "falling" if path[-1] < current * 0.999
                                else "flat"),
    }


def data_hash(payload):
    """Deterministic hash over the inputs, mirroring production's provable
    prediction record. The sandbox computes it the same way so integration
    code that stores or compares hashes has something real to work with."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()


def predict_threshold_breach(values, threshold, direction="above", horizon=96,
                             confidence=0.75):
    """Will this series cross `threshold`, and how soon?

    Returns production's key set exactly: will_breach,
    estimated_steps_to_breach, confidence, current_value, threshold, direction,
    forecast_at_breach, breach_window{earliest,latest}, point_forecast_summary,
    model.
    """
    if direction not in ("above", "below"):
        return {"error": "invalid_direction",
                "detail": "direction must be 'above' or 'below'.",
                "will_breach": None}
    if not values:
        return {"error": "empty_series",
                "detail": "time_series must contain at least one value.",
                "will_breach": None}

    fc = forecast(values, horizon)
    point = fc["point_forecast"]
    current = float(values[-1])
    breach_step = _first_cross(point, threshold, direction)

    # The optimistic bound leads and the conservative one trails, so the window
    # brackets the point estimate rather than sitting to one side of it.
    q90 = fc["quantile_forecasts"]["0.9"]
    q10 = fc["quantile_forecasts"]["0.1"]
    lead, trail = (q90, q10) if direction == "above" else (q10, q90)

    conf = 0.0
    if breach_step is not None:
        # Nearer breaches are more trustworthy than ones at the far edge of the
        # horizon, and a series with a clear trend beats a noisy flat one.
        horizon_decay = 1.0 - (breach_step / horizon) * 0.35
        spread = fc["residual_sigma"]
        trend_strength = (abs(fc["slope_per_step"]) /
                          (abs(fc["slope_per_step"]) + spread + 1e-9))
        conf = round(max(0.05, min(0.95,
                                   confidence * horizon_decay *
                                   (0.6 + 0.4 * trend_strength))), 4)

    return {
        "will_breach":               breach_step is not None,
        "estimated_steps_to_breach": breach_step,
        "confidence":                conf,
        "current_value":             round(current, 4),
        "threshold":                 threshold,
        "direction":                 direction,
        "forecast_at_breach":        point[breach_step] if breach_step is not None else None,
        "breach_window": {
            "earliest": _first_cross(lead, threshold, direction),
            "latest":   _first_cross(trail, threshold, direction),
        },
        "point_forecast_summary":    _summary(point, current),
        "model":                     MODEL_NAME,
        "simulated":                 True,
    }


def _series_trend(values):
    if not values or len(values) < 2:
        return "stable"
    slope, _ = _ols(values[-min(len(values), 32):])
    spread = _residual_sigma(values[-min(len(values), 32):], slope,
                             sum(values[-min(len(values), 32):]) / min(len(values), 32))
    if abs(slope) < max(1e-9, spread * 0.05):
        return "stable"
    return "rising" if slope > 0 else "falling"


def _trend_tier(trend):
    return {"declining": 0, "stable": 1, "improving": 2}.get(trend, 1)


def predict_batch(machines):
    """Score a list of {id, canonical_field, values, threshold, direction} and
    roll them up. Same structure production's _predict_batch_internal returns."""
    results = []
    fleet_risk = 0
    at_risk = 0

    for spec in machines:
        mid = spec.get("id") or "unknown"
        field = spec.get("canonical_field")
        values = spec.get("values") or []
        threshold = spec.get("threshold")
        direction = spec.get("direction") or "above"

        errors = []
        if len(values) < MIN_POINTS:
            errors.append(f"values needs >= {MIN_POINTS} points, got {len(values)}")
        if threshold is None:
            errors.append("threshold is required to score a machine")
        if direction not in ("above", "below"):
            errors.append("direction must be 'above' or 'below'")

        if errors:
            # Never let a machine that could not be scored pass as healthy.
            results.append({"machine_id": mid, "canonical_field": field,
                            "status": "unscored", "reason": "validation_error",
                            "errors": errors})
            continue

        pred = predict_threshold_breach(values, float(threshold),
                                        direction=direction)
        if pred.get("will_breach"):
            at_risk += 1
            steps = pred.get("estimated_steps_to_breach")
            steps = 999 if steps is None else steps
            fleet_risk += 30 if steps < 6 else 18 if steps < 24 else 8
            pred["recommendation"] = (
                f"Schedule intervention on {mid}: {field or 'series'} is "
                f"forecast to cross {threshold} in ~{steps} steps.")
        results.append({"machine_id": mid, "canonical_field": field,
                        "status": "ok", "prediction": pred})

    fleet_risk = min(100, fleet_risk)
    analyzed = len([r for r in results if r["status"] == "ok"])
    unscored = [r for r in results if r["status"] != "ok"]
    priority = sorted(
        [r for r in results if r.get("prediction", {}).get("will_breach")],
        key=lambda r: (r["prediction"].get("estimated_steps_to_breach")
                       if r["prediction"].get("estimated_steps_to_breach") is not None
                       else 999))[:5]

    fleet_summary = {
        "total_machines":    len(machines),
        "analyzed":          analyzed,
        "unscored":          len(unscored),
        "coverage_complete": len(unscored) == 0,
        "unscored_machines": [{
            "machine_id": r["machine_id"], "canonical_field": r.get("canonical_field"),
            "reason": r.get("reason") or r.get("status"),
            "errors": r.get("errors") or [],
        } for r in unscored],
        "at_risk":            at_risk,
        "fleet_health_score": 100 - fleet_risk,
        "score_basis":        "scored_machines_only" if unscored else "all_machines",
        "fleet_risk_level":  ("critical" if fleet_risk > 70
                              else "elevated" if fleet_risk > 40
                              else "moderate" if fleet_risk > 20
                              else "incomplete" if unscored
                              else "healthy"),
        "priority_maintenance": [{
            "machine_id": r["machine_id"], "canonical_field": r.get("canonical_field"),
            "steps_to_breach": r["prediction"].get("estimated_steps_to_breach"),
            "recommendation": r["prediction"].get("recommendation"),
        } for r in priority],
    }
    return {"fleet_summary": fleet_summary, "machines": results}


def fleet_recommendation(risk_distribution, unscored=0):
    crit = risk_distribution.get("critical", 0)
    elev = risk_distribution.get("elevated", 0)
    prefix = (f"{unscored} machine(s) could NOT be scored (see unscored_machines) — "
              f"fleet status below covers only the scored machines. "
              if unscored else "")
    if crit:
        return (f"{prefix}{crit} machine(s) forecast to breach within 6 steps. "
                f"Pull those into the next maintenance window before anything else.")
    if elev:
        return (f"{prefix}{elev} machine(s) trending toward a breach within 24 steps. "
                f"Schedule inspection this shift.")
    if risk_distribution.get("moderate"):
        return (f"{prefix}Breaches forecast, but none imminent. Monitor and "
                f"re-assess next shift.")
    if unscored:
        return prefix + "No breaches among the scored machines."
    return "No breaches forecast. Fleet is operating inside limits."


def fleet_health(machines):
    """Fleet rollup with the same top-level keys production returns."""
    batch = predict_batch(machines)

    risk_distribution = {"critical": 0, "elevated": 0, "moderate": 0, "healthy": 0}
    field_analysis = {}
    for m in batch["machines"]:
        if m["status"] != "ok":
            continue
        pred = m.get("prediction", {})
        if pred.get("will_breach"):
            steps = pred.get("estimated_steps_to_breach")
            steps = 999 if steps is None else steps
            bucket = "critical" if steps < 6 else "elevated" if steps < 24 else "moderate"
            risk_distribution[bucket] += 1
        else:
            risk_distribution["healthy"] += 1
        field = m.get("canonical_field") or "unknown"
        field_analysis.setdefault(field, []).append({
            "machine_id": m["machine_id"],
            "will_breach": pred.get("will_breach", False),
            "steps": pred.get("estimated_steps_to_breach"),
        })

    # Trend-first ranking: a machine moving toward its limit outranks a stable
    # one that merely sits closer to it.
    by_id = {m.get("machine_id"): m for m in batch["machines"]}
    ranked = []
    for spec in machines:
        mid = spec.get("id") or "unknown"
        res = by_id.get(mid, {}) or {}
        pred = res.get("prediction", {}) if isinstance(res, dict) else {}
        vtrend = _series_trend(spec.get("values") or [])
        if (spec.get("direction") or "above") == "below":
            health = ("declining" if vtrend == "falling"
                      else "improving" if vtrend == "rising" else "stable")
        else:
            health = ("declining" if vtrend == "rising"
                      else "improving" if vtrend == "falling" else "stable")
        ranked.append({"machine_id": mid,
                       "canonical_field": spec.get("canonical_field"),
                       "trend": health,
                       "will_breach": pred.get("will_breach", False),
                       "steps_to_breach": pred.get("estimated_steps_to_breach")})
    ranked.sort(key=lambda r: (
        0 if r["will_breach"] else 1,
        _trend_tier(r["trend"]),
        r["steps_to_breach"] if r["steps_to_breach"] is not None else 9999))

    return {
        "fleet_health":      batch["fleet_summary"],
        "risk_distribution": risk_distribution,
        "field_analysis":    field_analysis,
        "ranked_machines":   ranked,
        "maintenance_queue": batch["fleet_summary"]["priority_maintenance"],
        "recommendation":    fleet_recommendation(
                                 risk_distribution,
                                 unscored=batch["fleet_summary"]["unscored"]),
        # Production bills $0.50 per fleet assessment. The sandbox reports the
        # rate so cost modelling is possible, and charges nothing.
        "billing":           {"flat_usd": 0.0, "production_rate_usd": 0.50},
        "simulated":         True,
    }
