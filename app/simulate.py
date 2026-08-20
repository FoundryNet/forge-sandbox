"""Synthetic equipment for the Forge sandbox.

Five machines, each emitting telemetry under its REAL vendor tag names -- the
same spellings the mapping packs were built from. That is the point: a Haas
posts "SP_LOAD_PCT (%)", a SINUMERIK posts "SPINDEL_AUSLASTUNG (%)", and an
agent that has not seen either one still gets spindle_load_pct back.

Values are physically plausible, not random noise: temperatures ramp toward a
setpoint and hold, spindle load tracks whether the machine is cutting, a
printer's hotend overshoots slightly on first reach. Nothing is stored -- a
reading is a pure function of (machine, phase), and `seed` makes it repeatable.
"""

import math
import random
import time

# One shift, compressed. A machine's phase in this cycle decides whether it is
# warming, running, idling, or alarmed.
CYCLE_SECONDS = 600.0


def _phase(seed=None, t=None):
    """Position in the synthetic shift, 0.0 -> 1.0."""
    if seed is not None:
        return (float(seed) % CYCLE_SECONDS) / CYCLE_SECONDS
    now = time.time() if t is None else t
    return (now % CYCLE_SECONDS) / CYCLE_SECONDS


def _rng(machine, seed, phase):
    """Deterministic per (machine, seed) so `seed` really does reproduce."""
    basis = seed if seed is not None else int(phase * CYCLE_SECONDS)
    return random.Random(f"{machine}:{basis}")


def _ramp(start, target, progress, overshoot=0.0):
    """Exponential approach to a setpoint, with optional first-reach overshoot."""
    progress = max(0.0, min(1.0, progress))
    value = start + (target - start) * (1.0 - math.exp(-4.0 * progress))
    if overshoot and 0.55 < progress < 0.85:
        value += overshoot * math.sin((progress - 0.55) / 0.30 * math.pi)
    return value


def _jitter(rng, value, pct=0.01):
    return value * (1.0 + rng.uniform(-pct, pct))


def _haas(rng, phase):
    """Haas VF-2SS running a production part, MTConnect-style tags."""
    cutting = 0.15 < phase < 0.85
    warmup = min(1.0, phase / 0.15)
    coolant = _ramp(19.5, 26.5, warmup)
    load = _jitter(rng, 78.0 if cutting else 4.0, 0.12)
    rpm = int(_jitter(rng, 8500 if cutting else 0, 0.01))
    return {
        "MACHINE STATUS":   "ACTIVE" if cutting else "READY",
        "PROG NAME":        "O01204_BRACKET.NC",
        "S SPEED (RPM)":    rpm,
        "SP_LOAD_PCT (%)":  round(load, 1),
        "SPINDLE_SPEED_CMD": 8500,
        "F Rate (mm/min)":  round(_jitter(rng, 2540.0 if cutting else 0.0, 0.05), 1),
        "FEED_OVERRIDE":    100,
        "COOL_TEMP(C)":     round(_jitter(rng, coolant, 0.02), 1),
        "COOLANT_PRESSURE": round(_jitter(rng, 4.8 if cutting else 0.2, 0.06), 2),
        "X Mach Pos (mm)":  round(_jitter(rng, 122.5, 0.35), 3),
        "Y Mach Pos":       round(_jitter(rng, -88.2, 0.35), 3),
        "Z MACH POS (mm)":  round(_jitter(rng, -45.9, 0.20), 3),
        "X LOAD (%)":       round(_jitter(rng, 22.0 if cutting else 1.0, 0.30), 1),
        "X MOTOR TEMP [°C]": round(_ramp(21.0, 44.0, warmup) + rng.uniform(-0.6, 0.6), 1),
        "T NUM":            7,
        "TOOL_LIFE_COUNT":  312,
        "PART_CNT":         1204,
        "M30_COUNT":        1204,
        "TOTAL_HOURS (hrs)": 8123.5,
        "LAST_CYCLE_TIME":  184.2,
        "ALARM NUM":        0,
    }


def _fanuc(rng, phase):
    """FANUC R-30iB robot arm tending the cell, FOCAS-style tags."""
    moving = phase < 0.80
    duty = min(1.0, phase / 0.10)
    return {
        "Mode":              "AUTO" if moving else "IDLE",
        "TCPVEL (mm/s)":     round(_jitter(rng, 1250.0 if moving else 0.0, 0.08), 1),
        "PAYLOADKG(kg)":     round(_jitter(rng, 12.5 if moving else 0.0, 0.02), 2),
        "MTR_TORQUE (N-m)":  round(_jitter(rng, 48.2 if moving else 6.1, 0.10), 2),
        "actual_position_a": round(_jitter(rng, 91.4, 0.10), 3),
        "AxisCount":         6,
        "MaxAxes":           6,
        "BUS VOLT (V)":      round(_jitter(rng, 565.0, 0.01), 1),
        "DIGITAL IN":        1024,
        "DIGITAL_OUT":       512,
        "DATA REG":          77,
        "TOTAL_HOURS (h)":   21447.0,
        # Servo temperature climbing under a long duty cycle -- this is the
        # series that makes predict_breach interesting.
        "MOTOR_TEMP":        round(_ramp(28.0, 66.0, duty) + phase * 9.0
                                   + rng.uniform(-0.8, 0.8), 1),
    }


def _siemens(rng, phase):
    """SINUMERIK 840D sl on an S7 backplane. Tags are German, as shipped."""
    running = 0.10 < phase < 0.90
    warmup = min(1.0, phase / 0.12)
    return {
        "Betriebszustand":         "AUTOMATIK" if running else "HALT",
        "PROGRAMM":                "WELLE_STUFE3.MPF",
        "Nist_Spindle (RPM)":      int(_jitter(rng, 1200 if running else 0, 0.02)),
        "SPINDEL_AUSLASTUNG (%)":  round(_jitter(rng, 63.0 if running else 2.0, 0.10), 1),
        "Drehmoment_Nm":           round(_jitter(rng, 187.0 if running else 3.0, 0.08), 1),
        "Feedrate_Act (mm/min)":   round(_jitter(rng, 410.0 if running else 0.0, 0.05), 1),
        "Kuehlmittel Temp (C)":    round(_ramp(18.0, 31.0, warmup) + rng.uniform(-0.4, 0.4), 1),
        "DriveTemp":               round(_ramp(24.0, 58.0, warmup) + rng.uniform(-0.7, 0.7), 1),
        "ActPos_X":                round(_jitter(rng, 341.220, 0.02), 3),
        "FollowingError_X":        round(_jitter(rng, 0.0042, 0.40), 5),
        "STUECKZAHL (pcs)":        842,
        "Betriebsstunden":         14203.5,
        "energieverbrauch(Wh)":    4820500.0,
        "EmergencyStop":           False,
        "AlarmNumber":             0,
    }


def _prusa(rng, phase):
    """Prusa MK3S mid-print. Tags are what Marlin M105/M114 actually return."""
    heating = phase < 0.20
    progress = max(0.0, min(1.0, (phase - 0.20) / 0.75))
    hotend = _ramp(21.0, 215.0, min(1.0, phase / 0.18), overshoot=3.5)
    bed = _ramp(20.5, 60.0, min(1.0, phase / 0.14))
    return {
        # @: is a 0-127 duty byte, NOT a percentage. Full during heat-up,
        # settling to a holding duty once at temperature.
        "heater_power":      int(127 if heating else rng.randint(58, 82)),
        "bed_power":         int(127 if heating else rng.randint(30, 55)),
        "hotend_temp":       round(hotend + rng.uniform(-0.4, 0.4), 1),
        "hotend_target":     215.0,
        "bed_temp":          round(bed + rng.uniform(-0.2, 0.2), 1),
        "bed_target":        60.0,
        "pinda_temp":        round(_ramp(22.0, 41.0, min(1.0, phase / 0.30)), 1),
        "ambient_temp":      round(_jitter(rng, 24.5, 0.03), 1),
        "position_x":        round(_jitter(rng, 118.4, 0.30), 2),
        "position_y":        round(_jitter(rng, 102.7, 0.30), 2),
        "position_z":        round(0.2 + progress * 42.0, 2),
        "extruder_position": round(progress * 4820.0, 2),
        "progress":          round(progress * 100.0, 1),
        "printer_state":     "Heating" if heating else "Printing",
        "nozzle_diameter":   0.4,
        "layer_height":      0.2,
    }


def _carrier(rng, phase):
    """Carrier 48/50 rooftop unit on BACnet/IP, mid-afternoon cooling call."""
    occupied = 0.10 < phase < 0.92
    load = math.sin(phase * math.pi) if occupied else 0.05
    return {
        "OccupancyStatus":   "OCCUPIED" if occupied else "UNOCCUPIED",
        "SupplyTemp":        round(_jitter(rng, 12.8 + (1 - load) * 4.0, 0.03), 1),
        "ReturnTemp":        round(_jitter(rng, 22.4 + load * 1.8, 0.02), 1),
        "SpaceTemp":         round(_jitter(rng, 22.8 + load * 1.2, 0.015), 1),
        "SupplyFanSpeed":    round(_jitter(rng, 35.0 + load * 50.0, 0.04), 1),
        "DamperPosition":    round(_jitter(rng, 20.0 + load * 35.0, 0.06), 1),
        "ChilledWaterTemp":  round(_jitter(rng, 7.2, 0.04), 1),
        "CondenserPressure": round(_jitter(rng, 14.8 + load * 3.4, 0.03), 2),
        "PowerKW":           round(_jitter(rng, 8.0 + load * 46.0, 0.05), 1),
        "EnergyKWH":         412885.0,
        "CO2":               int(_jitter(rng, 480 + load * 420, 0.04)),
        "Humidity":          round(_jitter(rng, 48.0 + load * 8.0, 0.03), 1),
        "AlarmStatus":       0,
    }


MACHINES = {
    "haas": {
        "oem": "haas",
        "machine_id": "SBX-HAAS-VF2SS-01",
        "model": "VF-2SS",
        "serial": "SANDBOX-1120847",
        "description": "Haas VF-2SS vertical machining centre, MTConnect tag names",
        "protocol": "mtconnect",
        "vertical": "cnc",
        "emit": _haas,
    },
    "fanuc": {
        "oem": "fanuc",
        "machine_id": "SBX-FANUC-R30IB-01",
        "model": "R-2000iC/210F",
        "serial": "SANDBOX-F884201",
        "description": "FANUC R-30iB controller on a 6-axis arm, FOCAS tag names",
        "protocol": "focas",
        "vertical": "robotics",
        "emit": _fanuc,
    },
    "siemens": {
        "oem": "siemens",
        "machine_id": "SBX-SIEMENS-840D-01",
        "model": "SINUMERIK 840D sl",
        "serial": "SANDBOX-S7150022",
        "description": "Siemens SINUMERIK 840D sl / S7-1500, German tag names as shipped",
        "protocol": "profinet_s7",
        "vertical": "cnc",
        "emit": _siemens,
    },
    "prusa": {
        "oem": "prusa",
        "machine_id": "SBX-PRUSA-MK3S-01",
        "model": "MK3S+",
        "serial": "SANDBOX-CZPX0918",
        "description": "Prusa MK3S+ over Marlin serial, raw M105/M114 field names",
        "protocol": "serial_gcode",
        "vertical": "additive",
        "emit": _prusa,
    },
    "carrier": {
        "oem": "carrier",
        "machine_id": "SBX-CARRIER-48TC-01",
        "model": "48TCED14",
        "serial": "SANDBOX-RTU4412",
        "description": "Carrier 48TC rooftop unit on BACnet/IP, standard object names",
        "protocol": "bacnet_ip",
        "vertical": "building_automation",
        "emit": _carrier,
    },
}


def list_machines():
    return [{k: v for k, v in spec.items() if k != "emit"}
            for spec in MACHINES.values()]


def reading(machine: str, seed=None):
    """One raw vendor-shaped reading. Raises KeyError for an unknown machine."""
    spec = MACHINES[machine]
    ph = _phase(seed)
    rng = _rng(machine, seed, ph)
    return {
        "machine": machine,
        "oem": spec["oem"],
        "machine_id": spec["machine_id"],
        "model": spec["model"],
        "serial": spec["serial"],
        "protocol": spec["protocol"],
        "phase": round(ph, 4),
        "data": spec["emit"](rng, ph),
    }


# How much of a shift one series spans. Wide enough that warm-up ramps and
# duty-cycle drift are visible, narrow enough to stay inside one shift.
SERIES_SPAN = 0.5


def series(machine: str, field: str, points: int = 48, seed=None):
    """A history for one RAW tag, oldest -> newest. This is what you feed to
    predict_breach: it is stateless and will not invent a series for you.

    The window is kept inside a single shift. Walking wall-clock time instead
    would let a series wrap past the end of the cycle, so the machine appears
    to cool from 66C back to 28C mid-series -- and a forecaster handed that
    reports a confident downward trend on a machine that is actually heating.
    """
    spec = MACHINES[machine]
    end = _phase(seed)
    # Slide rather than compress when the window would run off the front, so a
    # series has a real trend in it regardless of when it was asked for.
    start = end - SERIES_SPAN
    if start < 0.0:
        start, end = 0.0, SERIES_SPAN

    step = (end - start) / max(1, points - 1)
    out = []
    for i in range(points):
        ph = start + i * step
        row = spec["emit"](_rng(machine, (seed or 0) + i, ph), ph)
        if field not in row:
            raise KeyError(field)
        value = row[field]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{field} is not numeric")
        out.append(float(value))
    return out
