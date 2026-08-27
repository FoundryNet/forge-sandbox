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


def _rockwell(rng, phase):
    """Allen-Bradley process skid: transfer pump, tank, VFD and a bake oven,
    tag names as a Kepware OPC UA browse tree presents them. Imperial units on
    the wire, because that is what a US plant actually ships."""
    duty = 0.35 + 0.65 * abs(math.sin(phase * math.pi))
    return {
        "Pump1_Flow_GPM":        round(_jitter(rng, 12.0 + duty * 18.0, 0.03), 1),
        "Pump1_Discharge_PSI":   round(_jitter(rng, 95.0 + duty * 60.0, 0.02), 1),
        "Pump1_Motor_Temp_DegF": round(_jitter(rng, 140.0 + duty * 30.0, 0.02), 1),
        "Pump1_Motor_Amps":      round(_jitter(rng, 8.0 + duty * 6.0, 0.03), 1),
        "Pump1_Motor_RPM":       int(_jitter(rng, 1760, 0.01)),
        "Tank1_Level_Pct":       round(_jitter(rng, 45.0 + duty * 30.0, 0.02), 1),
        "Valve1_Position_Pct":   round(_jitter(rng, 30.0 + duty * 40.0, 0.04), 1),
        "VFD1_Speed_Hz":         round(_jitter(rng, 40.0 + duty * 20.0, 0.02), 1),
        "VFD1_Output_Pct":       round(_jitter(rng, 55.0 + duty * 35.0, 0.03), 1),
        "Oven_Zone1_Temp_DegF":  round(_jitter(rng, 440.0 + duty * 25.0, 0.01), 1),
        "Compressor_PSI":        round(_jitter(rng, 100.0 + duty * 12.0, 0.02), 1),
        "Runtime_Hours":         14203,
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


# ── energy vertical ─────────────────────────────────────────────────────────
# `phase` is 0..1 across a simulated day, so irradiance follows a real solar
# arc and everything downstream (inverter output, meter demand, battery SOC)
# is driven by it rather than jittered independently.

def _solar(phase):
    """Normalised 0..1 solar intensity. Sunrise 0.25, solar noon 0.5, sunset 0.79."""
    if phase < 0.25 or phase > 0.79:
        return 0.0
    return math.sin(math.pi * (phase - 0.25) / 0.54)


def _tesla(rng, phase):
    """Tesla Megapack BESS. Charges on solar surplus, discharges after dark.
    Cell temperature in FAHRENHEIT and pack energy in WATT-HOURS, as shipped."""
    sun = _solar(phase)
    charging = sun > 0.35
    return {
        "SOC_pct":         round(_jitter(rng, 34.0 + sun * 52.0, 0.01), 1),
        "SOH_pct":         round(_jitter(rng, 96.1, 0.002), 1),
        "DC_Bus_V":        round(_jitter(rng, 814.3, 0.01), 1),
        "Chrg_Rate_kW":    round(_jitter(rng, 310.0 * sun if charging else 0.0, 0.05), 1),
        "Dischrg_Rate_kW": round(_jitter(rng, 0.0 if charging else 247.8, 0.05), 1),
        "Cell_Temp_Max_F": round(_jitter(rng, 84.0 + sun * 12.0, 0.01), 1),
        "Cycles":          1847,
        "Pack_Energy_Wh":  3200000,
    }


def _fronius(rng, phase):
    """Fronius PV inverter, SunSpec Modbus models 101-103 point names."""
    sun = _solar(phase)
    if sun <= 0.0:
        state = 2                                    # SLEEPING overnight
    elif sun < 0.08:
        state = 3                                    # STARTING
    else:
        state = 4                                    # MPPT
    return {
        "W":      int(_jitter(rng, 61000 * sun, 0.04)) if sun > 0 else 0,
        "WH":     12847500,
        "DCA":    round(_jitter(rng, 78.0 * sun, 0.04), 1) if sun > 0 else 0.0,
        "DCV":    round(_jitter(rng, 782.1, 0.01), 1) if sun > 0 else 0.0,
        "Hz":     round(_jitter(rng, 60.01, 0.0005), 2),
        "TmpCab": round(_jitter(rng, 28.0 + sun * 22.0, 0.02), 1),
        "St":     state,
        "Evt1":   0,
    }


def _schneider(rng, phase):
    """Schneider PowerLogic revenue meter. Units carried as a tag PREFIX."""
    sun = _solar(phase)
    load = 0.55 + 0.45 * math.sin(phase * math.pi)
    return {
        "kW_Total":       round(_jitter(rng, 312.7 * load, 0.03), 1),
        "kVAR_Total":     round(_jitter(rng, -42.1 * load, 0.05), 1),
        "PF_Avg":         round(_jitter(rng, 0.991, 0.002), 3),
        "V_LL_Avg":       round(_jitter(rng, 481.2, 0.005), 1),
        "I_Avg_A":        round(_jitter(rng, 376.4 * load, 0.03), 1),
        "Freq_Hz":        round(_jitter(rng, 60.01, 0.0005), 2),
        "kWh_Del":        4287650,
        "kWh_Rec":        187420,
        "Demand_kW_Peak": round(_jitter(rng, 487.3, 0.01), 1),
    }


def _weather(rng, phase):
    """Generic MQTT weather station. FAHRENHEIT and MPH, as consumer kit ships."""
    sun = _solar(phase)
    ambient_f = 68.0 + sun * 28.0
    return {
        "irradiance_w_m2": round(_jitter(rng, 1050.0 * sun, 0.05), 1),
        "ambient_temp_f":  round(_jitter(rng, ambient_f, 0.01), 1),
        "wind_speed_mph":  round(_jitter(rng, 7.2 + sun * 4.0, 0.20), 1),
        "humidity_pct":    round(_jitter(rng, 52.0 - sun * 30.0, 0.03), 1),
        "panel_temp_f":    round(_jitter(rng, ambient_f + sun * 52.0, 0.01), 1),
    }


# ── SunSpec devices: RAW REGISTERS, not scaled values ────────────────────────
# Everything above emits values already in engineering units, because that is
# what those protocols put on the wire. SunSpec does not. A SunSpec read returns
# integers plus a separate scale-factor register, and the pair is meaningless
# apart — `W = 6100` is 61 kW at W_SF=1 and 61 W at W_SF=0.
#
# These emitters therefore return what a Modbus client actually reads, so the
# scale-factor stage is exercised on real-shaped input instead of being handed
# pre-scaled numbers and asked to multiply by one.


def _reg(value, sf):
    """Encode an engineering value as the integer register a device would hold
    for the given scale factor. The inverse of value x 10^sf."""
    return int(round(float(value) / (10.0 ** sf)))


def _sunspec_inverter(rng, phase):
    """60 kW three-phase PV inverter, SunSpec model 103 raw registers."""
    sun = _solar(phase)
    state = 2 if sun <= 0.0 else (3 if sun < 0.08 else 4)
    w = _jitter(rng, 61000.0 * sun, 0.04) if sun > 0 else 0.0
    amps = _jitter(rng, 73.4 * sun, 0.04) if sun > 0 else 0.0
    return {
        # A_SF is SHARED: the specification has it govern A, AphA, AphB and
        # AphC together. Name-convention association cannot see that, which is
        # why the model id matters.
        "A": _reg(amps, -2), "AphA": _reg(amps / 3, -2),
        "AphB": _reg(amps / 3, -2), "AphC": _reg(amps / 3, -2), "A_SF": -2,
        "PPVphAB": _reg(_jitter(rng, 481.2, 0.005), -1),
        "PPVphBC": _reg(_jitter(rng, 480.7, 0.005), -1),
        "PPVphCA": _reg(_jitter(rng, 481.9, 0.005), -1), "V_SF": -1,
        "W": _reg(w, 1), "W_SF": 1,
        "Hz": _reg(_jitter(rng, 60.01, 0.0005), -2), "Hz_SF": -2,
        "VA": _reg(w * 1.02, 1), "VA_SF": 1,
        "VAr": _reg(_jitter(rng, -4210.0 * sun, 0.05) if sun > 0 else 0.0, 0),
        "VAr_SF": 0,
        "PF": _reg(_jitter(rng, 99.1, 0.002), -2), "PF_SF": -2,
        "WH": _reg(12847500, 0), "WH_SF": 0,
        "DCA": _reg(_jitter(rng, 78.0 * sun, 0.04) if sun > 0 else 0.0, -2),
        "DCA_SF": -2,
        "DCV": _reg(_jitter(rng, 782.1, 0.01) if sun > 0 else 0.0, -1),
        "DCV_SF": -1,
        "TmpCab": _reg(_jitter(rng, 28.0 + sun * 22.0, 0.02), -1), "Tmp_SF": -1,
        "St": state, "Evt1": 0,
    }


def _sunspec_inverter_1ph(rng, phase):
    """7.6 kW single-phase residential inverter, SunSpec model 101 registers."""
    sun = _solar(phase)
    state = 2 if sun <= 0.0 else (3 if sun < 0.08 else 4)
    w = _jitter(rng, 7600.0 * sun, 0.05) if sun > 0 else 0.0
    return {
        "A": _reg(_jitter(rng, 31.7 * sun, 0.05) if sun > 0 else 0.0, -2),
        "A_SF": -2,
        "PhVphA": _reg(_jitter(rng, 240.3, 0.004), -1), "V_SF": -1,
        "W": _reg(w, 0), "W_SF": 0,
        "Hz": _reg(_jitter(rng, 59.98, 0.0005), -2), "Hz_SF": -2,
        "PF": _reg(_jitter(rng, 98.4, 0.003), -2), "PF_SF": -2,
        "WH": _reg(3184200, 0), "WH_SF": 0,
        "DCA": _reg(_jitter(rng, 19.4 * sun, 0.05) if sun > 0 else 0.0, -2),
        "DCA_SF": -2,
        "DCV": _reg(_jitter(rng, 396.8, 0.01) if sun > 0 else 0.0, -1),
        "DCV_SF": -1,
        "TmpCab": _reg(_jitter(rng, 26.0 + sun * 19.0, 0.02), -1), "Tmp_SF": -1,
        "St": state, "Evt1": 0,
    }


def _sunspec_meter(rng, phase):
    """Wye-connect three-phase revenue meter, SunSpec model 203 registers."""
    load = 0.55 + 0.45 * math.sin(phase * math.pi)
    w = _jitter(rng, 312700.0 * load, 0.03)
    return {
        "A": _reg(_jitter(rng, 376.4 * load, 0.03), -2), "A_SF": -2,
        "PPV": _reg(_jitter(rng, 481.2, 0.005), -1),
        "PhV": _reg(_jitter(rng, 277.8, 0.005), -1), "V_SF": -1,
        "Hz": _reg(_jitter(rng, 60.01, 0.0005), -2), "Hz_SF": -2,
        "W": _reg(w, 1), "W_SF": 1,
        "VA": _reg(w * 1.01, 1), "VA_SF": 1,
        "VAR": _reg(_jitter(rng, -42100.0 * load, 0.05), 0), "VAR_SF": 0,
        "PF": _reg(_jitter(rng, 99.1, 0.002), -2), "PF_SF": -2,
        "TotWhExp": 4287650, "TotWhImp": 187420, "TotWh_SF": 0,
        "Evt": 0,
    }


def _sunspec_meter_1ph(rng, phase):
    """Single-phase meter, SunSpec model 201 registers."""
    load = 0.5 + 0.5 * math.sin(phase * math.pi)
    return {
        "A": _reg(_jitter(rng, 41.2 * load, 0.04), -2), "A_SF": -2,
        "PhV": _reg(_jitter(rng, 241.1, 0.004), -1), "V_SF": -1,
        "Hz": _reg(_jitter(rng, 59.99, 0.0005), -2), "Hz_SF": -2,
        "W": _reg(_jitter(rng, 9930.0 * load, 0.04), 0), "W_SF": 0,
        "VAR": _reg(_jitter(rng, -820.0 * load, 0.06), 0), "VAR_SF": 0,
        "PF": _reg(_jitter(rng, 98.7, 0.003), -2), "PF_SF": -2,
        "TotWhExp": 118400, "TotWhImp": 942300, "TotWh_SF": 0,
        "Evt": 0,
    }


def _sunspec_storage(rng, phase):
    """BESS exposing SunSpec model 124 (control) + model 802 (battery).

    Charges through the solar peak and discharges into the evening ramp, which
    is what a demand-charge-managed C&I battery actually does.
    """
    sun = _solar(phase)
    charging = sun > 0.35
    soc = 34.0 + 46.0 * sun
    power = (_jitter(rng, 240000.0 * sun, 0.04) if charging
             else -_jitter(rng, 185000.0 * (1.0 - sun), 0.05))
    return {
        # model 124 — control block. ChaState is percent of amp-hour rating.
        "ChaState": _reg(soc, -1), "ChaState_SF": -1,
        "InBatV": _reg(_jitter(rng, 812.4, 0.006), -1), "InBatV_SF": -1,
        "WChaMax": _reg(500000, 1), "WChaMax_SF": 1,
        # model 802 — the measurement block, with ITS OWN scale factors. A real
        # BESS advertises both blocks in one register map, so the reading is
        # only interpretable against models [124, 802] together.
        "SoC": _reg(soc, -1), "SoC_SF": -1,
        "SoH": _reg(_jitter(rng, 97.3, 0.001), -1), "SoH_SF": -1,
        "V": _reg(_jitter(rng, 812.4, 0.006), -1), "V_SF": -1,
        "A": _reg(power / 812.4, -1), "A_SF": -1,
        "W": _reg(power, 1), "W_SF": 1,
        "NCyc": 1284,
        "WHRtg": _reg(3900000, 2), "WHRtg_SF": 2,
        "ChaSt": 3 if charging else 4,
    }


def _solaredge_inverter(rng, phase):
    """SolarEdge three-phase inverter. SunSpec underneath, SolarEdge names on
    top, and its own scale-factor register spellings."""
    sun = _solar(phase)
    w = _jitter(rng, 100000.0 * sun, 0.04) if sun > 0 else 0.0
    return {
        "I_AC_Current": _reg(_jitter(rng, 120.3 * sun, 0.04) if sun > 0 else 0.0, -2),
        "I_AC_Current_SF": -2,
        "I_AC_VoltageAB": _reg(_jitter(rng, 480.9, 0.005), -1),
        "I_AC_Voltage_SF": -1,
        "I_AC_Power": _reg(w, 1), "I_AC_Power_SF": 1,
        "I_AC_Frequency": _reg(_jitter(rng, 60.01, 0.0005), -2),
        "I_AC_Frequency_SF": -2,
        "I_AC_VAR": _reg(_jitter(rng, -6900.0 * sun, 0.05) if sun > 0 else 0.0, 0),
        "I_AC_VAR_SF": 0,
        "I_AC_PF": _reg(_jitter(rng, 99.4, 0.002), -2), "I_AC_PF_SF": -2,
        "I_AC_Energy_WH": 21748300, "I_AC_Energy_WH_SF": 0,
        "I_DC_Current": _reg(_jitter(rng, 128.0 * sun, 0.04) if sun > 0 else 0.0, -2),
        "I_DC_Current_SF": -2,
        "I_DC_Voltage": _reg(_jitter(rng, 795.4, 0.01) if sun > 0 else 0.0, -1),
        "I_DC_Voltage_SF": -1,
        "I_Temp_Sink": _reg(_jitter(rng, 31.0 + sun * 26.0, 0.02), -1),
        "I_Temp_SF": -1,
        "I_Status": 2 if sun <= 0.0 else 4,
    }


def _solaredge_meter(rng, phase):
    """SolarEdge revenue meter. Line-to-line voltage is deliberately absent —
    SolarEdge meters do not expose it over Modbus, and pretending otherwise
    would hide a real integration constraint."""
    load = 0.55 + 0.45 * math.sin(phase * math.pi)
    return {
        "M_AC_Current": _reg(_jitter(rng, 214.7 * load, 0.03), -2),
        "M_AC_Current_SF": -2,
        "M_AC_Voltage_LN": _reg(_jitter(rng, 277.4, 0.005), -1),
        "M_AC_Voltage_SF": -1,
        "M_AC_Freq": _reg(_jitter(rng, 60.01, 0.0005), -2), "M_AC_Freq_SF": -2,
        "M_AC_Power": _reg(_jitter(rng, 178400.0 * load, 0.03), 1),
        "M_AC_Power_SF": 1,
        "M_AC_VAR": _reg(_jitter(rng, -21800.0 * load, 0.05), 0), "M_AC_VAR_SF": 0,
        "M_AC_PF": _reg(_jitter(rng, 99.2, 0.002), -2), "M_AC_PF_SF": -2,
        "M_Exported": 1874200, "M_Imported": 3391800, "M_Energy_W_SF": 0,
    }


def _victron(rng, phase):
    """Victron Cerbo GX. Values arrive already scaled — Victron does the
    conversion in Venus OS and publishes engineering units on D-Bus."""
    sun = _solar(phase)
    charging = sun > 0.35
    soc = 41.0 + 38.0 * sun
    power = (_jitter(rng, 9800.0 * sun, 0.04) if charging
             else -_jitter(rng, 6400.0 * (1.0 - sun), 0.05))
    return {
        "/Dc/Battery/Voltage":     round(_jitter(rng, 52.8, 0.004), 2),
        "/Dc/Battery/Current":     round(power / 52.8, 1),
        "/Dc/Battery/Soc":         round(soc, 1),
        "/Dc/Battery/Temperature": round(_jitter(rng, 24.0 + sun * 6.0, 0.02), 1),
        "/Dc/Pv/Power":            round(_jitter(rng, 11200.0 * sun, 0.05), 0) if sun > 0 else 0.0,
        "/Dc/Pv/Voltage":          round(_jitter(rng, 388.2, 0.01), 1) if sun > 0 else 0.0,
        "/Ac/Grid/L1/Power":       round(_jitter(rng, 3100.0 * (1.0 - sun), 0.06), 0),
        "/Ac/Grid/L1/Voltage":     round(_jitter(rng, 239.7, 0.004), 1),
        "/Ac/Grid/L1/Frequency":   round(_jitter(rng, 59.99, 0.0005), 2),
        "Battery/Soh":             round(_jitter(rng, 98.1, 0.001), 1),
    }


def _sungrow(rng, phase):
    """Sungrow SH10RT hybrid inverter, register names from the published
    Home Assistant Modbus map. Values arrive already scaled — Sungrow's
    exponents are fixed in the map rather than transmitted, so the client
    applies them at read time and there are no SF registers on the wire."""
    sun = _solar(phase)
    charging = sun > 0.35
    soc = 38.0 + 44.0 * sun
    return {
        "Phase A voltage":       round(_jitter(rng, 239.4, 0.004), 1),
        "Phase A current":       round(_jitter(rng, 21.7 * sun, 0.05), 1) if sun > 0 else 0.0,
        "Grid frequency":        round(_jitter(rng, 50.01, 0.0005), 2),
        "Total active power":    round(_jitter(rng, 8400.0 * sun, 0.04), 0) if sun > 0 else 0.0,
        "Reactive power":        round(_jitter(rng, -410.0 * sun, 0.06), 0) if sun > 0 else 0.0,
        "Power factor":          round(_jitter(rng, 0.987, 0.002), 3),
        "Total DC power":        round(_jitter(rng, 8900.0 * sun, 0.04), 0) if sun > 0 else 0.0,
        "Inverter temperature":  round(_jitter(rng, 27.0 + sun * 21.0, 0.02), 1),
        "MPPT1 voltage":         round(_jitter(rng, 604.2, 0.01), 1) if sun > 0 else 0.0,
        "MPPT1 current":         round(_jitter(rng, 7.4 * sun, 0.05), 1) if sun > 0 else 0.0,
        "Battery voltage":       round(_jitter(rng, 213.6, 0.005), 1),
        "Battery level":         round(soc, 1),
        "Battery state of health": round(_jitter(rng, 99.0, 0.001), 1),
        "Battery temperature":   round(_jitter(rng, 23.0 + sun * 5.0, 0.02), 1),
        "Battery charging power":    round(_jitter(rng, 3200.0 * sun, 0.05), 0) if charging else 0.0,
        "Battery discharging power": 0.0 if charging else round(_jitter(rng, 2400.0 * (1.0 - sun), 0.05), 0),
        "Total PV generation":   28471.6,
        "Total imported energy": 9184.2,
        "Total exported energy": 16302.8,
        "Daily PV generation & battery discharge": round(48.2 * sun, 1),
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
    "tesla": {
        "oem": "tesla",
        "machine_id": "SBX-TESLA-MEGAPACK-01",
        "model": "Megapack 2XL",
        "serial": "SANDBOX-MP2X0774",
        "description": "Tesla Megapack BESS over Modbus TCP; cell temp in F, pack energy in Wh",
        "protocol": "modbus_tcp",
        "vertical": "energy",
        "emit": _tesla,
    },
    "fronius": {
        "oem": "fronius",
        "machine_id": "SBX-FRONIUS-SYMO-01",
        "model": "Symo 60.0-3",
        "serial": "SANDBOX-FR6003318",
        "description": "Fronius Symo PV inverter, SunSpec Modbus model 103 point names",
        "protocol": "sunspec_modbus",
        "vertical": "energy",
        "emit": _fronius,
    },
    "schneider": {
        "oem": "schneider",
        "machine_id": "SBX-SCHNEIDER-PM8000-01",
        "model": "PowerLogic PM8000",
        "serial": "SANDBOX-PM8244901",
        "description": "Schneider PowerLogic revenue meter on Modbus RTU, unit-prefixed tags",
        "protocol": "modbus_rtu",
        "vertical": "energy",
        "emit": _schneider,
    },
    "generic_iot": {
        "oem": "generic_iot",
        "machine_id": "SBX-WEATHER-POA-01",
        "model": "POA irradiance + met station",
        "serial": "SANDBOX-WX0031",
        "description": "Generic MQTT weather station; irradiance W/m2, temps in F, wind in mph",
        "protocol": "mqtt",
        "vertical": "energy",
        "emit": _weather,
    },
    "sunspec_inverter": {
        "oem": "sunspec_inverter",
        "machine_id": "SBX-SUNSPEC-INV3P-01",
        "model": "SunSpec model 103 reference inverter",
        "serial": "SANDBOX-SS103001",
        "description": "Three-phase PV inverter, SunSpec 103 RAW registers plus scale factors",
        "protocol": "sunspec_modbus",
        "vertical": "energy",
        "sunspec_model": 103,
        "emit": _sunspec_inverter,
    },
    "sunspec_inverter_1ph": {
        "oem": "sunspec_inverter_1ph",
        "machine_id": "SBX-SUNSPEC-INV1P-01",
        "model": "SunSpec model 101 reference inverter",
        "serial": "SANDBOX-SS101001",
        "description": "Single-phase PV inverter, SunSpec 101 RAW registers plus scale factors",
        "protocol": "sunspec_modbus",
        "vertical": "energy",
        "sunspec_model": 101,
        "emit": _sunspec_inverter_1ph,
    },
    "sunspec_meter": {
        "oem": "sunspec_meter",
        "machine_id": "SBX-SUNSPEC-MTR3P-01",
        "model": "SunSpec model 203 wye meter",
        "serial": "SANDBOX-SS203001",
        "description": "Wye three-phase revenue meter, SunSpec 203 RAW registers plus scale factors",
        "protocol": "sunspec_modbus",
        "vertical": "energy",
        "sunspec_model": 203,
        "emit": _sunspec_meter,
    },
    "sunspec_meter_1ph": {
        "oem": "sunspec_meter_1ph",
        "machine_id": "SBX-SUNSPEC-MTR1P-01",
        "model": "SunSpec model 201 meter",
        "serial": "SANDBOX-SS201001",
        "description": "Single-phase meter, SunSpec 201 RAW registers plus scale factors",
        "protocol": "sunspec_modbus",
        "vertical": "energy",
        "sunspec_model": 201,
        "emit": _sunspec_meter_1ph,
    },
    "sunspec_storage": {
        "oem": "sunspec_storage",
        "machine_id": "SBX-SUNSPEC-BESS-01",
        "model": "SunSpec model 124 + 802 storage",
        "serial": "SANDBOX-SS802001",
        "description": "BESS exposing SunSpec 124 control and 802 battery blocks, RAW registers",
        "protocol": "sunspec_modbus",
        "vertical": "energy",
        "sunspec_model": [124, 802],
        "emit": _sunspec_storage,
    },
    "solaredge_inverter": {
        "oem": "solaredge_inverter",
        "machine_id": "SBX-SOLAREDGE-SE100K-01",
        "model": "SE100K three phase",
        "serial": "SANDBOX-SE1000774",
        "description": "SolarEdge three-phase inverter, SolarEdge point names over SunSpec Modbus TCP",
        "protocol": "sunspec_modbus",
        "vertical": "energy",
        "sunspec_model": 103,
        "emit": _solaredge_inverter,
    },
    "solaredge_meter": {
        "oem": "solaredge_meter",
        "machine_id": "SBX-SOLAREDGE-MTR-01",
        "model": "SolarEdge SE-MTR-3Y-400V",
        "serial": "SANDBOX-SEM400221",
        "description": "SolarEdge revenue meter; line-to-line voltage is not exposed over Modbus",
        "protocol": "sunspec_modbus",
        "vertical": "energy",
        "sunspec_model": 203,
        "emit": _solaredge_meter,
    },
    "victron": {
        "oem": "victron",
        "machine_id": "SBX-VICTRON-CERBO-01",
        "model": "Cerbo GX + MultiPlus-II",
        "serial": "SANDBOX-VE0044812",
        "description": "Victron Cerbo GX over Modbus-TCP; D-Bus paths, values already scaled",
        "protocol": "modbus_tcp",
        "vertical": "energy",
        "emit": _victron,
    },
    "sungrow": {
        "oem": "sungrow",
        "machine_id": "SBX-SUNGROW-SH10RT-01",
        "model": "SH10RT",
        "serial": "SANDBOX-SG10RT441",
        "description": "Sungrow SH10RT hybrid inverter; fixed per-register scales, no SF registers",
        "protocol": "modbus_tcp",
        "vertical": "energy",
        "emit": _sungrow,
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
    "rockwell": {
        "oem": "rockwell",
        "machine_id": "SBX-ROCKWELL-SKID-01",
        "model": "ControlLogix 1756-L83E",
        "serial": "SANDBOX-AB771204",
        "description": "Allen-Bradley process skid (pump, tank, VFD, oven) over "
                       "Kepware OPC UA, imperial tag names",
        "protocol": "opc_ua",
        "vertical": "industrial",
        "emit": _rockwell,
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
    out = {
        "machine": machine,
        "oem": spec["oem"],
        "machine_id": spec["machine_id"],
        "model": spec["model"],
        "serial": spec["serial"],
        "protocol": spec["protocol"],
        "phase": round(ph, 4),
        "data": spec["emit"](rng, ph),
    }
    # SunSpec devices emit RAW registers, so the reading is not interpretable
    # without the model id. Surfacing it here is what makes the simulate ->
    # normalize round trip work without the caller having to know which of the
    # seventeen machines happens to speak SunSpec.
    if spec.get("sunspec_model") is not None:
        out["sunspec_model"] = spec["sunspec_model"]
    return out


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
