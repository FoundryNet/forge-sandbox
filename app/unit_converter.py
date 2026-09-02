"""Unit normalization for the Forge kernel — Option A (converge on SI).

WHY THIS EXISTS
---------------
Before this module, unit handling lived in `universal_normalize.detect_and_convert_units`
and keyed off substring tests against the canonical field name ("pressure" in cf,
cf.endswith("_kw")). That had two failure modes, both confirmed against production
on 2026-08-21:

  1. SILENT CORRUPTION. A tag declaring a non-SI unit — `COOL_TEMP (F)`,
     `Abs X [inch]`, `Feedrate_Act (m/min)` — landed unconverted in a field whose
     name implies SI. 86 °F was served as 86 into a Celsius-convention field; an
     inch position was served as-is into a millimetre-convention field. The value
     is plausible, so nothing ever looks wrong. 32 corpus fields are affected.

  2. NAME/VALUE CONTRADICTION. The LLM mapper invents canonical names that carry
     the SOURCE unit as a suffix (`condenser_pressure_psi`, `infeed_pressure_psi`).
     The substring rule ("pressure" in cf) then converted psi→bar and stored a bar
     value under a `_psi` name. 98 of 154 cached LLM mappings carry such a suffix,
     so every converter added naively promotes a latent case into an active one.

The fix is a single explicit table plus a NAME-AWARE guard: the canonical field's
own name is authoritative about what unit it holds. We never write a value into a
field whose name declares a different unit — we convert, or we flag, never both.

THE CONVENTION (Option A)
-------------------------
Each physical quantity has exactly one target unit. A field name either declares
that target explicitly (`..._c`, `..._bar`, `..._mm`) or declares nothing and is
assumed to hold the target. Cross-vendor comparison is then always valid: the same
canonical field holds the same unit regardless of OEM.

    temperature  -> C          energy      -> kWh
    pressure     -> bar        power       -> kW
    length       -> mm         flow        -> L/min
    linear speed -> mm/min     mass        -> kg
    rotational   -> rpm        torque      -> Nm
    vibration    -> mm/s       time        -> h

Nothing here mutates field NAMES; renaming is a corpus migration (see
UNIT_CONVERSION_AUDIT.md). This module makes values honest under whatever name the
resolver produced, and reports every decision it makes.
"""
from __future__ import annotations

import re
from typing import Any, Optional, Tuple

__all__ = [
    "convert_value", "declared_unit", "field_declared_unit", "target_unit",
    "quantity_of", "TARGET_UNIT", "CONVERSIONS", "DIMENSION",
]

# ── unit vocabulary ─────────────────────────────────────────────────────────
# Every spelling observed in the 16,908-row production corpus maps to one token.
UNIT_ALIASES = {
    # temperature
    "c": "C", "°c": "C", "degc": "C", "deg_c": "C", "celsius": "C",
    "f": "F", "°f": "F", "degf": "F", "deg_f": "F", "fahrenheit": "F",
    "k": "K", "°k": "K", "kelvin": "K",
    "r": "R", "°r": "R", "rankine": "R",
    # pressure
    "bar": "bar", "mbar": "mbar", "psi": "psi", "psig": "psi", "psia": "psi",
    "kpa": "kPa", "mpa": "MPa", "pa": "Pa", "atm": "atm", "inhg": "inHg",
    "mmhg": "mmHg", "torr": "mmHg", "mm_hg": "mmHg",
    # length
    "mm": "mm", "cm": "cm", "m": "m", "in": "in", "inch": "in", "inches": "in",
    "ft": "ft", "feet": "ft", "um": "um", "µm": "um", "micron": "um",
    "mil": "mil", "mils": "mil",
    # linear speed
    "mm/min": "mm/min", "m/min": "m/min", "in/min": "in/min", "ipm": "in/min",
    "mm/s": "mm/s", "m/s": "m/s", "in/s": "in/s", "ips": "in/s",
    "ft/min": "ft/min", "fpm": "ft/min", "ft/s": "ft/s", "fps": "ft/s",
    "mph": "mph", "kph": "kph", "km/h": "kph", "kmh": "kph",
    # rotational
    "rpm": "rpm", "rev/min": "rpm", "1/min": "rpm", "hz": "Hz",
    # energy / power
    "wh": "Wh", "kwh": "kWh", "mwh": "MWh", "j": "J", "kj": "kJ",
    "w": "W", "kw": "kW", "mw": "MW", "hp": "hp",
    # Reactive and apparent power. Absent until 2026-08-22: a SunSpec meter
    # reporting VAr in `var` landed in reactive_power_kvar unconverted, 1000x
    # high, with no conversion record and no flag — the converter did not know
    # the unit existed, so target_unit returned None and the value fell through
    # the "nothing to do" path. Same shape as the F1 sentinel bug: every unit
    # test passed because no test named a unit the table had never heard of.
    "var": "var", "vars": "var", "v-a-r": "var", "volt-ampere-reactive": "var",
    "kvar": "kVAR", "kvars": "kVAR", "mvar": "MVAR",
    "va": "VA", "kva": "kVA", "mva": "MVA",
    # Dimensionless ratio. Power factor is a ratio in [-1, 1]; SunSpec transmits
    # it in percent (models 101-103 and 201-204 declare PF units as "Pct"), so
    # the two spellings must be inter-convertible or a PF of 0.95 arrives as 95.
    "ratio": "ratio", "pu": "ratio", "p.u.": "ratio", "per-unit": "ratio",
    # volumetric flow
    "l/min": "L/min", "lpm": "L/min", "ml/min": "mL/min",
    "gpm": "gal/min", "gal/min": "gal/min", "l/s": "L/s", "m3/h": "m3/h",
    # CFM. Recognised by the tag tokenizer but absent HERE, so the converter
    # could not parse the unit it was handed: a 450 CFM VAV box landed as 450
    # in an L/min field, 28x low, with no conversion recorded and no flag.
    # scfm/acfm are the same reading at different reference conditions; the
    # distinction is not one this table can carry, and treating them as CFM is
    # far closer than leaving them unconverted.
    "cfm": "ft3/min", "ft3/min": "ft3/min", "ft³/min": "ft3/min",
    "scfm": "ft3/min", "acfm": "ft3/min", "ft3/h": "ft3/h", "cfh": "ft3/h",
    # mass flow. NASA C-MAPSS documents bleed flow as "pps", meaning lbm/s —
    # but "pps" also means parts-per-second on a packaging line, so only the
    # explicit spellings are auto-recognised. An ambiguous "pps" tag is left
    # alone and flagged rather than guessed.
    "lbm/s": "lb/s", "lb/s": "lb/s", "kg/s": "kg/s", "kg/h": "kg/h",
    # volume, and volume-per-hour. Fuel is the reason: a truck reports fuel
    # RATE in gal/h and fuel USED in gallons, and neither quantity existed.
    "l": "L", "liter": "L", "litre": "L", "liters": "L", "litres": "L",
    "gal": "gal", "gallon": "gal", "gallons": "gal", "usgal": "gal",
    "ml": "mL", "m3": "m3", "cf": "ft3", "ft3": "ft3",
    "lph": "L/h", "l/h": "L/h", "gph": "gal/h", "gal/h": "gal/h",
    "gal/hr": "gal/h", "l/hr": "L/h",
    # distance at vehicle scale. `mi` is NOT `mil` (a thousandth of an inch);
    # both are anchored to end-of-tag so they cannot shadow each other.
    "km": "km", "kilometer": "km", "kilometers": "km", "kilometre": "km",
    "mi": "mi", "mile": "mi", "miles": "mi",
    # mass
    "kg": "kg", "g": "g", "lb": "lb", "lbs": "lb", "oz": "oz",
    "tonne": "tonne", "tonnes": "tonne", "ton": "ton", "tons": "ton",
    # torque / force
    "nm": "Nm", "n-m": "Nm", "n_m": "Nm", "n": "N",
    "ftlb": "ftlb", "ft-lb": "ftlb", "lbft": "ftlb",
    # vibration / acceleration
    "m/s²": "m/s2", "m/s2": "m/s2", "mm/s²": "mm/s2", "g-force": "g-force",
    # time
    "s": "s", "sec": "s", "secs": "s", "seconds": "s",
    "min": "min", "mins": "min", "minutes": "min",
    "h": "h", "hr": "h", "hrs": "h", "hours": "h",
    "ms": "ms", "days": "days",
    # irradiance — W/m² is NOT power. Without its own token, a tag declaring
    # "(W)" against `solar_irradiance_w_m2` resolves as watts, finds the SI
    # power target kW, and silently divides the reading by 1000.
    "w/m2": "W/m2", "w/m²": "W/m2", "w_m2": "W/m2", "wm2": "W/m2",
    "kw/m2": "kW/m2", "kw/m²": "kW/m2",
    # dimensionless / electrical
    "%": "%", "pct": "%", "percent": "%",
    "v": "V", "vac": "V", "vdc": "V", "kv": "kV", "a": "A", "ma": "mA",
    "mv": "mV", "khz": "kHz",
}

QUANTITY = {
    "C": "temperature", "F": "temperature", "K": "temperature", "R": "temperature",
    "bar": "pressure", "mbar": "pressure", "psi": "pressure", "kPa": "pressure",
    "MPa": "pressure", "Pa": "pressure", "atm": "pressure", "inHg": "pressure",
    "mmHg": "pressure",
    "mm": "length", "cm": "length", "m": "length", "in": "length",
    "ft": "length", "um": "length", "mil": "length",
    "mm/min": "linear_speed", "m/min": "linear_speed", "in/min": "linear_speed",
    "mm/s": "linear_speed", "m/s": "linear_speed", "in/s": "linear_speed",
    "ft/min": "linear_speed", "ft/s": "linear_speed",
    "mph": "vehicle_speed", "kph": "vehicle_speed",
    "rpm": "rotational", "Hz": "rotational",
    "Wh": "energy", "kWh": "energy", "MWh": "energy", "J": "energy", "kJ": "energy",
    "W/m2": "irradiance", "kW/m2": "irradiance",
    "W": "power", "kW": "power", "MW": "power", "hp": "power",
    "var": "reactive_power", "kVAR": "reactive_power", "MVAR": "reactive_power",
    "VA": "apparent_power", "kVA": "apparent_power", "MVA": "apparent_power",
    # `ratio` shares the percent quantity on purpose. They are the same
    # dimensionless quantity written two ways, so %->ratio is a legal conversion
    # while VA->kVAR (apparent power into a reactive-power field) is refused as a
    # cross-quantity contradiction, which is exactly the right split.
    "ratio": "percent",
    "L/min": "flow", "mL/min": "flow", "gal/min": "flow", "L/s": "flow",
    "m3/h": "flow",
    # CFM. The tokenizer already recognised `cfm` and normalised it to
    # ft3/min, but the quantity table did not list it -- so no conversion was
    # attempted, none was recorded, and a 450 CFM VAV box was stored as 450 in
    # an L/min field. Silent, and 28x off. A recognised unit that cannot be
    # converted is worse than an unrecognised one, because the reading still
    # lands in the canonical field wearing the wrong unit.
    "ft3/min": "flow", "ft3/h": "flow",
    # Same trap, different quantity: these were all recognised by
    # _normalize_unit_token and absent here.
    "km/h": "vehicle_speed",
    "mV": "voltage", "kHz": "rotational",
    # Concentration has exactly one unit here, so source and target are always
    # equal and no conversion is ever attempted. It is listed anyway: an
    # unlisted-but-recognised unit is the CFM trap, and "no conversion because
    # src == dst" and "no conversion because the table never heard of it" must
    # not look the same to the converter.
    "ppm": "concentration", "NTU": "turbidity",
    "L": "volume", "mL": "volume", "gal": "volume", "m3": "volume",
    "ft3": "volume",
    "L/h": "flow", "gal/h": "flow",
    "km": "length", "mi": "length",
    "lb/s": "mass_flow", "kg/s": "mass_flow", "kg/h": "mass_flow",
    "kg": "mass", "g": "mass", "lb": "mass", "oz": "mass",
    "tonne": "mass", "ton": "mass",
    "Nm": "torque", "N": "force", "ftlb": "torque",
    "m/s2": "vibration_accel", "mm/s2": "vibration_accel", "g-force": "vibration_accel",
    "s": "time", "min": "time", "h": "time", "ms": "time", "days": "time",
    "%": "percent", "V": "voltage", "kV": "voltage", "A": "current", "mA": "current",
    # Robot joints are the reason this exists. UR's RTDE reports every joint
    # angle in radians and KUKA/ABB/FANUC teach pendants report the same angle
    # in degrees; without an angle quantity the two vendors cannot share a
    # canonical field, and the alternative -- a separate degrees field and a
    # separate radians field for the same axis -- is exactly the fragmentation
    # the canonical registry exists to prevent.
    "deg": "angle", "rad": "angle",
}

# Several "quantities" above are really reporting CONVENTIONS over one physical
# dimension: feed rate, vehicle speed and vibration velocity are all length/time.
# Conversions between them are legitimate; the cross-quantity guard must compare
# dimensions, not convention labels, or it blocks m/s -> kph as a "mismatch".
DIMENSION = {
    "linear_speed": "velocity",
    "vehicle_speed": "velocity",
    "vibration_velocity": "velocity",
    "vibration_accel": "acceleration",
}

# The single target unit per quantity — the whole point of Option A.
TARGET_UNIT = {
    "temperature": "C",
    "pressure": "bar",
    "length": "mm",
    "linear_speed": "mm/min",
    "vibration_velocity": "mm/s",
    "vehicle_speed": "kph",      # road speed stays kph; mm/min is meaningless here
    "rotational": "rpm",
    "energy": "kWh",
    "power": "kW", "reactive_power": "kVAR", "apparent_power": "kVA",
    "flow": "L/min",
    "mass_flow": "kg/s",
    "mass": "kg",
    "torque": "Nm",
    "vibration_accel": "mm/s2",
    "time": "h",
    "percent": "%",
    "voltage": "V",
    "current": "A",
    "irradiance": "W/m2",
    "volume": "L",
    "concentration": "ppm",
    "turbidity": "NTU",
    # Degrees, not radians. Radians are the SI unit, but every robot teach
    # pendant, work instruction and operator on the floor reads degrees, and
    # UR is the only vendor here that puts radians on the wire.
    "angle": "deg",
}

# ── conversions ─────────────────────────────────────────────────────────────
CONVERSIONS = {
    # temperature
    ("F", "C"): (lambda v: (v - 32.0) * 5.0 / 9.0, "fahrenheit_to_celsius"),
    ("K", "C"): (lambda v: v - 273.15, "kelvin_to_celsius"),
    ("R", "C"): (lambda v: (v - 491.67) * 5.0 / 9.0, "rankine_to_celsius"),
    # pressure
    ("psi", "bar"): (lambda v: v * 0.06894757293168361, "psi_to_bar"),
    ("kPa", "bar"): (lambda v: v / 100.0, "kpa_to_bar"),
    ("MPa", "bar"): (lambda v: v * 10.0, "mpa_to_bar"),
    ("Pa", "bar"): (lambda v: v / 100_000.0, "pa_to_bar"),
    ("atm", "bar"): (lambda v: v * 1.01325, "atm_to_bar"),
    ("mbar", "bar"): (lambda v: v / 1000.0, "mbar_to_bar"),
    ("inHg", "bar"): (lambda v: v * 0.03386388640341, "inhg_to_bar"),
    ("mmHg", "bar"): (lambda v: v * 0.00133322387415, "mmhg_to_bar"),
    # length
    ("in", "mm"): (lambda v: v * 25.4, "inch_to_mm"),
    ("ft", "mm"): (lambda v: v * 304.8, "foot_to_mm"),
    ("m", "mm"): (lambda v: v * 1000.0, "meter_to_mm"),
    ("cm", "mm"): (lambda v: v * 10.0, "cm_to_mm"),
    ("um", "mm"): (lambda v: v / 1000.0, "micron_to_mm"),
    ("mil", "mm"): (lambda v: v * 0.0254, "mil_to_mm"),
    # linear speed
    ("m/min", "mm/min"): (lambda v: v * 1000.0, "m_min_to_mm_min"),
    ("in/min", "mm/min"): (lambda v: v * 25.4, "in_min_to_mm_min"),
    ("m/s", "mm/min"): (lambda v: v * 60_000.0, "m_s_to_mm_min"),
    ("mm/s", "mm/min"): (lambda v: v * 60.0, "mm_s_to_mm_min"),
    ("ft/min", "mm/min"): (lambda v: v * 304.8, "ft_min_to_mm_min"),
    ("ft/s", "mm/min"): (lambda v: v * 18_288.0, "ft_s_to_mm_min"),
    # vibration velocity is reported in mm/s, not mm/min
    ("in/s", "mm/s"): (lambda v: v * 25.4, "in_s_to_mm_s"),
    ("m/s", "mm/s"): (lambda v: v * 1000.0, "m_s_to_mm_s"),
    # vehicle speed
    ("mph", "kph"): (lambda v: v * 1.609344, "mph_to_kph"),
    ("m/s", "kph"): (lambda v: v * 3.6, "m_s_to_kph"),
    # Meteorological wind speed is conventionally m/s (WMO), while consumer
    # weather hardware ships mph or km/h. Without these the energy vertical's
    # `wind_speed_m_s` field takes an mph value flagged "unit_unconvertible"
    # and keeps it — a 2.24x error that looks entirely plausible.
    ("kW/m2", "W/m2"): (lambda v: v * 1000.0, "kw_m2_to_w_m2"),
    ("W/m2", "kW/m2"): (lambda v: v / 1000.0, "w_m2_to_kw_m2"),
    ("mph", "m/s"): (lambda v: v * 0.44704, "mph_to_m_s"),
    ("kph", "m/s"): (lambda v: v / 3.6, "kph_to_m_s"),
    ("m/s", "mph"): (lambda v: v / 0.44704, "m_s_to_mph"),
    # energy
    ("Wh", "kWh"): (lambda v: v / 1000.0, "watthour_to_kwh"),
    ("MWh", "kWh"): (lambda v: v * 1000.0, "megawatthour_to_kwh"),
    ("J", "kWh"): (lambda v: v / 3_600_000.0, "joule_to_kwh"),
    ("kJ", "kWh"): (lambda v: v / 3600.0, "kilojoule_to_kwh"),
    # power
    ("W", "kW"): (lambda v: v / 1000.0, "watt_to_kw"),
    ("var", "kVAR"): (lambda v: v / 1000.0, "var_to_kvar"),
    ("kVAR", "var"): (lambda v: v * 1000.0, "kvar_to_var"),
    ("MVAR", "kVAR"): (lambda v: v * 1000.0, "mvar_to_kvar"),
    ("VA", "kVA"): (lambda v: v / 1000.0, "va_to_kva"),
    ("kVA", "VA"): (lambda v: v * 1000.0, "kva_to_va"),
    ("MVA", "kVA"): (lambda v: v * 1000.0, "mva_to_kva"),
    ("%", "ratio"): (lambda v: v / 100.0, "percent_to_ratio"),
    ("ratio", "%"): (lambda v: v * 100.0, "ratio_to_percent"),
    ("MW", "kW"): (lambda v: v * 1000.0, "megawatt_to_kw"),
    ("hp", "kW"): (lambda v: v * 0.7456998715822702, "hp_to_kw"),
    # flow
    ("gal/min", "L/min"): (lambda v: v * 3.785411784, "gpm_to_l_min"),
    ("mL/min", "L/min"): (lambda v: v / 1000.0, "ml_min_to_l_min"),
    ("L/s", "L/min"): (lambda v: v * 60.0, "l_s_to_l_min"),
    ("m3/h", "L/min"): (lambda v: v * (1000.0 / 60.0), "m3_h_to_l_min"),
    # mass flow
    ("lb/s", "kg/s"): (lambda v: v * 0.45359237, "lbm_s_to_kg_s"),
    ("kg/h", "kg/s"): (lambda v: v / 3600.0, "kg_h_to_kg_s"),
    # mass
    ("lb", "kg"): (lambda v: v * 0.45359237, "pound_to_kg"),
    ("oz", "kg"): (lambda v: v * 0.028349523125, "ounce_to_kg"),
    ("g", "kg"): (lambda v: v / 1000.0, "gram_to_kg"),
    ("tonne", "kg"): (lambda v: v * 1000.0, "tonne_to_kg"),
    ("ton", "kg"): (lambda v: v * 907.18474, "shortton_to_kg"),
    # torque
    ("ftlb", "Nm"): (lambda v: v * 1.3558179483314004, "ftlb_to_nm"),
    # angle -- UR ships joint angles in radians, everyone else in degrees
    ("rad", "deg"): (lambda v: v * 57.29577951308232, "radians_to_degrees"),
    ("deg", "rad"): (lambda v: v / 57.29577951308232, "degrees_to_radians"),
    # vibration acceleration
    ("m/s2", "mm/s2"): (lambda v: v * 1000.0, "m_s2_to_mm_s2"),
    ("g-force", "mm/s2"): (lambda v: v * 9806.65, "g_to_mm_s2"),
    # volume
    ("gal", "L"): (lambda v: v * 3.785411784, "gallons_to_litres"),
    ("mL", "L"): (lambda v: v / 1000.0, "millilitres_to_litres"),
    ("m3", "L"): (lambda v: v * 1000.0, "cubic_metres_to_litres"),
    ("ft3", "L"): (lambda v: v * 28.316846592, "cubic_feet_to_litres"),
    # fuel rate
    ("gal/h", "L/h"): (lambda v: v * 3.785411784, "gph_to_lph"),
    # ...and on to the flow TARGET, so no recognised unit is left without a
    # route to its quantity's target unit (test_no_recognized_unit_lacks_a
    # _converter_to_its_target). Fuel rate fields declare L/h and take the
    # pair above; these two only fire if a field declares the flow default.
    ("L/h", "L/min"): (lambda v: v / 60.0, "lph_to_lpm"),
    ("gal/h", "L/min"): (lambda v: v * 3.785411784 / 60.0, "gph_to_lpm"),
    ("L/min", "L/h"): (lambda v: v * 60.0, "lpm_to_lph"),
    ("gal/min", "L/h"): (lambda v: v * 3.785411784 * 60.0, "gpm_to_lph"),
    # distance at vehicle scale
    ("mi", "km"): (lambda v: v * 1.609344, "miles_to_km"),
    # Vehicle distances into the length target. Numerically absurd but
    # dimensionally correct, and only reachable if a field declares mm and is
    # handed a road distance; leaving the pair out would mean a RECOGNISED
    # unit with no route to its target, which is the CFM trap again.
    ("mi", "mm"): (lambda v: v * 1609344.0, "miles_to_mm"),
    ("km", "mm"): (lambda v: v * 1000000.0, "km_to_mm"),
    ("m", "km"): (lambda v: v / 1000.0, "metres_to_km"),
    ("ft", "m"): (lambda v: v * 0.3048, "feet_to_metres"),
    ("mm", "m"): (lambda v: v / 1000.0, "millimetres_to_metres"),
    ("in", "m"): (lambda v: v * 0.0254, "inches_to_metres"),
    # pressure into kPa. J1939 reports oil, boost and brake-air pressure in
    # kPa; the SI target here is bar, so a psi source had no route to a
    # kPa-declared field.
    ("psi", "kPa"): (lambda v: v * 6.894757293168361, "psi_to_kpa"),
    ("bar", "kPa"): (lambda v: v * 100.0, "bar_to_kpa"),
    ("MPa", "kPa"): (lambda v: v * 1000.0, "mpa_to_kpa"),
    ("Pa", "kPa"): (lambda v: v / 1000.0, "pa_to_kpa"),
    ("mbar", "kPa"): (lambda v: v / 10.0, "mbar_to_kpa"),
    # flow
    ("ft3/min", "L/min"): (lambda v: v * 28.316846592, "cfm_to_lpm"),
    ("ft3/h", "L/min"): (lambda v: v * 28.316846592 / 60.0, "ft3h_to_lpm"),
    # voltage / rotational
    ("mV", "V"): (lambda v: v / 1000.0, "millivolts_to_volts"),
    ("kHz", "rpm"): (lambda v: v * 1000.0 * 60.0, "khz_to_rpm"),
    # km/h and kph are the same unit spelled two ways; declaring the identity
    # keeps a km/h source from looking unconvertible.
    ("km/h", "kph"): (lambda v: v, "kmh_to_kph"),
    # time
    ("min", "h"): (lambda v: v / 60.0, "minutes_to_hours"),
    ("s", "h"): (lambda v: v / 3600.0, "seconds_to_hours"),
    ("ms", "h"): (lambda v: v / 3_600_000.0, "ms_to_hours"),
    ("days", "h"): (lambda v: v * 24.0, "days_to_hours"),
    # electrical scale prefixes
    #
    # `kV` and `mA` were already RECOGNIZED as voltage and current but had no
    # converter, so a reading arrived flagged `unit_unconvertible` and was kept
    # as-is: 0.2771 kV sat in a volts field as 0.2771, off by 1000x. The
    # fail-closed guard does not catch these -- it is a deliberately narrow list
    # of flow units -- and widening that list to null them would be the worse
    # trade when the factor is exact and the conversion is trivial.
    ("kV", "V"): (lambda v: v * 1000.0, "kilovolt_to_volt"),
    ("mV", "V"): (lambda v: v / 1000.0, "millivolt_to_volt"),
    ("mA", "A"): (lambda v: v / 1000.0, "milliamp_to_amp"),
    ("kA", "A"): (lambda v: v * 1000.0, "kiloamp_to_amp"),
    # Hz -> rpm is deliberately ABSENT. It looks like a unit conversion and is
    # not one: a VFD's 60 Hz is 3600 rpm on a 2-pole machine and 1800 on a
    # 4-pole, so the factor depends on the MACHINE, not the units. Pint reads
    # the same pair as 9.549 (treating Hz as rad/s), which is a third answer
    # again -- three defensible numbers is the signature of a conversion that
    # should not be automatic. Left unconvertible so it is flagged, not guessed.
    # imperial feed rate straight to the machining target
    ("in/s", "mm/min"): (lambda v: v * 1524.0, "in_s_to_mm_min"),
}

# ── parsing a unit off a name ───────────────────────────────────────────────
_PARENTHESISED = re.compile(r"[\(\[\{]\s*([^)\]\}]{1,12})\s*[\)\]\}]\s*$")
_TRAILING = re.compile(r"[_\s]([A-Za-z°µ/%²0-9\-]{1,8})$")

# A bare trailing "_s" / "_in" / "_m" / "_g" is far more often a word fragment
# ("alarms", "digital_input", "program", "config") than a unit. Underscore
# suffixes are only trusted when the token is unambiguous standing alone;
# parenthesised units — "(F)", "(in)", "(s)" — are explicit and always trusted.
_UNAMBIGUOUS_SUFFIX = {
    "mm", "cm", "um", "µm", "inch", "inches", "feet", "ft", "mil", "mils",
    "psi", "psig", "psia", "kpa", "mpa", "mbar", "bar", "inhg",
    "rpm", "hz", "kwh", "wh", "kw", "mw", "hp",
    "nm", "n-m", "n_m", "ftlb", "ft-lb", "lbft",
    "kg", "lb", "lbs", "oz", "tonne", "tonnes",
    "gpm", "lpm", "l/min", "ml/min", "l/s", "m3/h", "lbm/s", "kg/s", "kg/h",
    # CFM is as unambiguous as GPM and was simply missing. A `_CFM` tag is an
    # air flow, full stop -- and until it was listed here, declared_unit()
    # returned None for it, so a 450 CFM VAV box was written into an L/min
    # field as 450: no conversion, no record, 28x low.
    "cfm", "scfm", "acfm", "cfh", "ft3/min", "ft3/h",
    # Millivolts and kilohertz: same shape, both recognised by the tag
    # tokenizer and unparseable here. `mv` is not the ambiguous bare `v`.
    "mv", "khz",
    "lph", "l/h", "gph", "gal/h", "gal", "gallons", "km", "mi", "miles",
    "mmhg", "mm_hg", "torr", "inhg",
    "pct", "percent", "hrs", "hours", "minutes", "seconds",
    "mph", "kph", "km/h", "kmh", "ipm", "fpm",
    "mm/s", "mm/min", "m/min", "in/min", "m/s2", "m/s²",
    "w/m2", "w/m²", "w_m2", "kw/m2",
    "degc", "degf", "celsius", "fahrenheit", "kelvin", "rankine",
    "°c", "°f", "°k", "°r",
}

# ── ambiguous bare suffixes, resolved only in context ───────────────────────
# A bare trailing `_F` could be Fahrenheit or a boolean flag; `_V` could be
# volts or "verified"; `_C` could be Celsius or a CNC C-axis. Excluding them
# outright was the safe call in isolation, but it produced a worse failure than
# the one it prevented (finding F2, 2026-08-22):
#
#     tag `ambient_temp_f`      -> sensor_readings.ambient_temp = 94.6
#     tag `ambient_temp (degF)` -> sensor_readings.ambient_temp = 34.777778
#
# One sensor, one reading, two stored values sixty degrees apart, and the bare
# form recorded no conversion and raised no warning. Silence was the failure.
#
# The fix is not to trust bare suffixes generally — it is to resolve one ONLY
# when another token in the SAME tag names the quantity it would belong to.
# `Cell_Temp_Max_F` says "temp", so the F is Fahrenheit. `axis_pos_c` says
# nothing about temperature, so its C stays unresolved and nothing is converted.
#
# Every context token here has to be a word that would not plausibly appear in a
# tag of a different quantity. Keep these lists tight: a false positive converts
# a value that was already correct, which is the exact failure mode this module
# exists to prevent.
_BARE_SUFFIX_CONTEXT = {
    "f": ("F", ("temp", "temperature", "tmp", "therm", "thermal", "thermo",
                "degrees", "degree", "heat", "coolant", "ambient")),
    "c": ("C", ("temp", "temperature", "tmp", "therm", "thermal", "thermo",
                "degrees", "degree", "heat", "coolant", "ambient")),
    "k": ("K", ("temp", "temperature", "tmp", "therm", "thermal", "thermo")),
    "r": ("R", ("temp", "temperature", "tmp", "therm", "thermal", "thermo")),
    "v": ("V", ("volt", "volts", "voltage", "vdc", "vac", "bus", "vll",
                "potential", "emf")),
    "a": ("A", ("curr", "current", "amp", "amps", "amperage", "ampere")),
    # A bare trailing `_L` is litres only when the tag says what is being
    # measured by volume. `TotalFuelUsed_L` qualifies; `Motor_L` (left) does
    # not, and must keep falling through.
    "l": ("L", ("fuel", "volume", "tank", "coolant", "oil", "used",
                "consumed", "capacity", "def", "urea")),
}


def _bare_suffix_unit(parts: list) -> Optional[str]:
    """Resolve a single-letter trailing unit using the rest of the tag's tokens.

    `parts` is the already-lowercased token list. Returns None unless the last
    token is an ambiguous bare unit AND an earlier token names its quantity.
    """
    if len(parts) < 2:
        return None
    entry = _BARE_SUFFIX_CONTEXT.get(parts[-1])
    if not entry:
        return None
    unit, context = entry
    head = parts[:-1]
    for tok in head:
        for c in context:
            if c in tok:          # substring: matches "temp" inside "temperature"
                return unit
    return None


def declared_unit(tag: str) -> Optional[str]:
    """The unit a raw source tag declares, or None.

    `COOL_TEMP (F)` -> "F";  `energieverbrauch(Wh)` -> "Wh";  `SPINDLE_LOAD` -> None.
    """
    t = (tag or "").strip()
    if not t:
        return None
    m = _PARENTHESISED.search(t)
    if m:
        u = UNIT_ALIASES.get(m.group(1).strip().lower())
        if u:
            return u
    # Try the trailing 1..3 underscore/space segments, longest first, so that
    # `Press_mm_hg` resolves as "mm_hg" (mmHg) rather than "hg" (unknown) and
    # `flow_rate_l_min` resolves as "l_min" rather than "min" (time!).
    parts = re.split(r"[_\s]+", t.lower())
    for n in (3, 2, 1):
        if len(parts) <= n:
            continue
        tok = "_".join(parts[-n:])
        if tok in _UNAMBIGUOUS_SUFFIX:
            return UNIT_ALIASES.get(tok)
        slashed = "/".join(parts[-n:])
        if slashed in _UNAMBIGUOUS_SUFFIX:
            return UNIT_ALIASES.get(slashed)
    # Last resort: a bare single-letter unit, but only when the rest of the tag
    # names the quantity. See _BARE_SUFFIX_CONTEXT.
    return _bare_suffix_unit(parts)


# Longest-first so `_mm_s` wins over `_s` and `_kwh` over `_h`.
_NAME_SUFFIXES = sorted(
    [
        ("_w_m2", "W/m2"), ("_kw_m2", "kW/m2"),
        ("_mm_s", "mm/s"), ("_mm_min", "mm/min"), ("_m_s", "m/s"),
        ("_m_s2", "m/s2"), ("_l_min", "L/min"), ("_ml_min", "mL/min"),
        ("_kwh", "kWh"), ("_wh", "Wh"), ("_kw", "kW"), ("_mw", "MW"),
        ("_kvar", "kVAR"), ("_var", "var"), ("_kva", "kVA"),
        ("_bar", "bar"), ("_mbar", "mbar"), ("_psi", "psi"),
        ("_kpa", "kPa"), ("_mpa", "MPa"), ("_pa", "Pa"),
        ("_c", "C"), ("_f", "F"), ("_k", "K"), ("_r", "R"),
        ("_celsius", "C"), ("_fahrenheit", "F"),
        ("_mm", "mm"), ("_cm", "cm"), ("_um", "um"),
        ("_inch", "in"), ("_in", "in"), ("_ft", "ft"),
        ("_rpm", "rpm"), ("_hz", "Hz"),
        ("_kg", "kg"), ("_lb", "lb"), ("_nm", "Nm"),
        ("_kmh", "kph"), ("_kph", "kph"), ("_mph", "mph"),
        ("_gpm", "gal/min"), ("_kg_s", "kg/s"), ("_lb_s", "lb/s"),
        ("_mm_hg", "mmHg"), ("_mmhg", "mmHg"),
        ("_pct", "%"), ("_percent", "%"),
        ("_minutes", "min"), ("_min", "min"),
        ("_hours", "h"), ("_hrs", "h"), ("_h", "h"),
        ("_seconds", "s"), ("_sec", "s"), ("_ms", "ms"), ("_s", "s"),
        ("_days", "days"), ("_v", "V"), ("_a", "A"),
    ],
    key=lambda p: -len(p[0]),
)


def field_declared_unit(canonical: str) -> Optional[str]:
    """The unit a canonical field NAME promises, or None.

    `condenser_pressure_psi` -> "psi";  `axes.x_position_actual` -> None.
    This is authoritative: we never store a value that contradicts it.
    """
    c = (canonical or "").lower()
    if not c:
        return None
    for suf, unit in _NAME_SUFFIXES:
        if c.endswith(suf):
            return unit
    return None


def quantity_of(unit: Optional[str]) -> Optional[str]:
    return QUANTITY.get(unit) if unit else None


def _registry_unit(canonical: str) -> Optional[str]:
    """The unit the canonical registry declares for a field, if any.

    Imported lazily and defensively: unit_converter is shared with the
    production kernel, where the registry module may not be importable, and a
    converter that cannot convert because a lookup table failed to load is worse
    than one that falls back to the SI target."""
    try:
        from app import field_registry as _fr
        return _fr.unit_of(canonical)
    except Exception:
        return None


def _is_vibration_field(canonical: str) -> bool:
    c = (canonical or "").lower()
    return "vibration" in c or c.endswith("_vib") or ".vib" in c


# A velocity unit (m/s, in/s, mm/s, m/min…) means different things on different
# fields, and each domain has its own reporting convention. Same dimension, four
# legitimate targets — so the FIELD decides, not the unit.
_VELOCITY_CONTEXT = (
    # (substring match on the canonical field, target unit)
    ("vibration", "mm/s"),      # ISO 10816 reports vibration velocity in mm/s
    ("tcp_speed", "mm/s"),      # robot tool-centre-point speed
    ("tcp_velocity", "mm/s"),
    ("tool_speed", "mm/s"),
    ("travel_speed", "mm/s"),
    ("ground_speed", "kph"),    # vehicle road speed
    ("vehicle_speed", "kph"),
    ("road_speed", "kph"),
    ("wheel_speed", "kph"),
    ("feed", "mm/min"),         # machining feed rate
    ("jog", "mm/min"),
    ("rapid", "mm/min"),
)


def _velocity_target(canonical: str) -> str:
    """Which velocity convention this field reports in. Defaults to the feed-rate
    convention (mm/min) because that is what the machining corpus overwhelmingly
    means by a linear speed."""
    c = (canonical or "").lower()
    for needle, unit in _VELOCITY_CONTEXT:
        if needle in c:
            return unit
    return TARGET_UNIT["linear_speed"]


def target_unit(canonical: str, source_unit: Optional[str]) -> Optional[str]:
    """The unit this field should hold.

    The field's own NAME wins when it declares a unit — a field called
    `..._psi` holds psi, full stop, even though the SI target for pressure is
    bar. Renaming such fields is a corpus migration, not something a converter
    may decide at runtime. Otherwise the SI target for the source quantity.

    One quantity is field-dependent: velocity units (in/s, m/s, mm/s) mean
    *feed rate* on a machining field and *vibration velocity* on a vibration
    field, and ISO 10816 reports the latter in mm/s while feed is conventionally
    mm/min. Same dimension, two legitimate targets — so the field decides.
    """
    named = field_declared_unit(canonical)
    declared_now = _registry_unit(canonical)
    if named:
        # ...unless the registry EXPLICITLY declares a unit of a different
        # dimension, in which case the name was not declaring a unit at all.
        #
        # `ac_voltage_phase_a` ends in `_a` and `ac_current_phase_c` ends in
        # `_c`, but those are PHASE DESIGNATORS -- reading them as amperes and
        # degrees Celsius made a voltage field claim to hold current and a
        # current field claim to hold temperature. The registry says V and A
        # and even documents the trap ("The trailing letter is the phase
        # designator, not a unit"), and nothing consulted it because the name
        # won unconditionally.
        #
        # An explicit declaration beats a suffix inference. This cannot regress
        # the `..._psi` case the name rule exists for: there the registry either
        # agrees or is silent, and only a same-dimension disagreement (psi vs
        # bar) is left to the name, exactly as before.
        if declared_now and declared_now in QUANTITY and declared_now != named:
            n_dim = DIMENSION.get(quantity_of(named), quantity_of(named))
            d_dim = DIMENSION.get(quantity_of(declared_now),
                                  quantity_of(declared_now))
            if n_dim and d_dim and n_dim != d_dim:
                return declared_now
        return named

    # The registry's declared unit, when the NAME carries no suffix to read.
    #
    # Without this, a field's unit contract was only enforced if it happened to
    # be spelled into the field name. `power_factor` is declared `ratio` in the
    # registry and has no suffix, so target_unit returned the SI target for the
    # SOURCE quantity — percent — decided src == dst, and passed a SunSpec PF of
    # 95 (Pct) straight into a ratio field. The registry said "ratio" the whole
    # time and nothing consulted it.
    #
    # Restricted to units the converter actually knows (present in QUANTITY),
    # so a bookkeeping unit like `cycles` does not become a conversion target
    # that no converter can satisfy and every reading gets flagged unconvertible.
    declared = _registry_unit(canonical)
    if declared and declared in QUANTITY:
        return declared

    q = quantity_of(source_unit)
    if q in ("linear_speed", "vehicle_speed"):
        return _velocity_target(canonical)
    return TARGET_UNIT.get(q) if q else None


def convert_value(tag: str, value: Any, canonical: str,
                  source_unit: Optional[str] = None) -> Tuple[Any, Optional[dict]]:
    """Convert `value` into the unit `canonical` is supposed to hold.

    Returns (value, record). `record` is None when nothing needed doing;
    otherwise it is a dict describing what happened, suitable for the response's
    `unit_conversions` list. A record with "converted": False is a FLAG — the
    value was left alone because converting it would have contradicted the field
    name or because no converter exists. Nothing is ever silently passed through.

    `source_unit` lets a caller state the wire unit outright, for tags that
    declare nothing themselves. Some protocols name a register after
    its quantity and nothing else — SunSpec models 101-103 call AC power `W` and
    lifetime energy `WH` — so there is no suffix to read and no safe way to
    guess from a one-token tag. That knowledge belongs to the OEM pack, which
    declares it per tag in `tag_units`.

    The caller decides precedence. corpus.normalize_row passes this ONLY when
    the tag string is silent, so an explicit `(degC)` on the tag always beats
    the pack's model-wide default — the tag describes this message, the pack
    describes the model.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return value, None

    src = source_unit or declared_unit(tag)
    unit_source = "pack" if source_unit else "tag"
    if not src:
        return value, None

    dst = target_unit(canonical, src)
    if not dst or src == dst:
        return value, None

    src_q, dst_q = quantity_of(src), quantity_of(dst)

    # The field name declares a unit of the same quantity but a DIFFERENT one
    # than the source. Converting is correct and safe: the name is the contract.
    # (e.g. tag "(psi)" -> field "..._bar": convert psi->bar.)
    #
    # The dangerous inverse — tag "(psi)" -> field "..._psi" — is already
    # short-circuited above by src == dst.
    src_dim = DIMENSION.get(src_q, src_q)
    dst_dim = DIMENSION.get(dst_q, dst_q)
    if src_dim and dst_dim and src_dim != dst_dim:
        return value, {
            "raw_field": tag, "canonical_field": canonical,
            "from": src, "to": dst, "converted": False,
            "unit_source": unit_source, "flag": "unit_quantity_mismatch",
            "detail": f"tag declares {src} ({src_q}) but field implies "
                      f"{dst} ({dst_q}); refusing to convert across quantities",
        }

    conv = CONVERSIONS.get((src, dst))
    if not conv:
        return value, {
            "raw_field": tag, "canonical_field": canonical,
            "from": src, "to": dst, "converted": False,
            "unit_source": unit_source, "flag": "unit_unconvertible",
            "detail": f"no converter for {src} -> {dst}; value left in {src}",
        }

    fn, label = conv
    try:
        out = round(float(fn(float(value))), 6)
    except (TypeError, ValueError, ZeroDivisionError):
        return value, {
            "raw_field": tag, "canonical_field": canonical,
            "from": src, "to": dst, "converted": False,
            "unit_source": unit_source, "flag": "unit_conversion_failed", "detail": f"{label} raised on {value!r}",
        }

    return out, {
        "raw_field": tag, "canonical_field": canonical,
        "from": src, "to": dst, "converted": True, "unit_source": unit_source,
        "conversion": label, "raw_value": value, "converted_value": out,
    }
