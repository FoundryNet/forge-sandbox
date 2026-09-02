#!/usr/bin/env python3
"""Build the SAE J1939 fleet / heavy-vehicle pack.

WHY THIS PACK
-------------
The 2026-09-02 vertical stress test put fleet at 31.6% and the mixed-fleet
payload (EquipmentShare T3, with CAT_/Deere_/JLG_/Genie_ prefixes) at 41.7%.
Nothing missing was exotic: fuel rate, fuel level, odometer, oil pressure,
turbo boost, exhaust temp, DEF level, DPF soot load. Every one is a
documented J1939 SPN, which makes this the same shape of pack as UR RTDE --
published, stable, and defensible to an engineer who knows the standard.

It also fixes a semantic defect the stress test surfaced and that no resolver
change could fix: `TotalIdleHours` and `EngineHours` both folded onto
`operating_hours`, and idle hours won. A truck with 14,203 engine hours
reported 2,400. There was no `idle_hours` and no `engine_hours` to separate
them. Now there is.

    python3 tools/build_j1939_pack.py

Idempotent. Writes app/packs/j1939.json plus additions to both dictionaries
and the pack index.

UNIT NOTES
    Every canonical here declares its unit, and the pack declares the unit of
    each RAW TAG that carries one, so a `_PSI` tag reaching a `_kpa` field
    converts and records the conversion rather than landing on magnitude.
    unit_converter grew volume (gal/L), fuel rate (gal/h, L/h), vehicle
    distance (mi/km) and psi->kPa for this pack.
"""
from __future__ import annotations

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PACK_DIR = os.path.join(ROOT, "app", "packs")
REGISTRY = os.path.join(ROOT, "app", "canonical_fields.json")
PACK_DICT = os.path.join(PACK_DIR, "_canonical_fields.json")

# (name, unit, physical_quantity, type, description)
FIELDS = [
    # engine
    ("engine_speed_rpm", "rpm", "rotational", "float", "Engine speed. SPN 190."),
    ("engine_coolant_temp_c", "C", "temperature", "float",
     "Engine coolant temperature. SPN 110."),
    ("engine_oil_pressure_kpa", "kPa", "pressure", "float",
     "Engine oil pressure. SPN 100."),
    ("engine_oil_temp_c", "C", "temperature", "float",
     "Engine oil temperature. SPN 175."),
    ("engine_load_pct", "%", "percent", "float",
     "Percent load at current speed. SPN 92."),
    ("engine_hours", "h", "time", "float",
     "Total engine running hours. SPN 247. NOT idle hours -- see idle_hours; "
     "collapsing the two reported a 14,203-hour truck as 2,400."),
    ("fuel_temp_c", "C", "temperature", "float", "Fuel temperature. SPN 174."),
    ("turbo_boost_pressure_kpa", "kPa", "pressure", "float",
     "Turbocharger boost pressure. SPN 102."),
    ("intake_manifold_temp_c", "C", "temperature", "float",
     "Intake manifold temperature. SPN 105."),
    ("intercooler_temp_c", "C", "temperature", "float",
     "Charge air cooler temperature. SPN 52."),
    # fuel
    ("fuel_rate_lph", "L/h", "flow", "float", "Instantaneous fuel rate. SPN 183."),
    ("fuel_level_pct", "%", "percent", "float", "Fuel tank level. SPN 96."),
    ("total_fuel_used_l", "L", "volume", "float", "Total fuel consumed. SPN 250."),
    ("idle_fuel_used_l", "L", "volume", "float", "Fuel consumed while idling."),
    ("idle_hours", "h", "time", "float",
     "Hours spent idling. A subset of engine_hours, never a substitute."),
    # speed / distance
    ("vehicle_speed_km_h", "kph", "vehicle_speed", "float",
     "Wheel-based vehicle speed. SPN 84."),
    ("odometer_km", "km", "length", "float", "Total vehicle distance."),
    # transmission
    ("transmission_temp_c", "C", "temperature", "float",
     "Transmission oil temperature. SPN 177."),
    ("transmission_gear", None, None, "int", "Current gear. SPN 523."),
    ("transmission_oil_pressure_kpa", "kPa", "pressure", "float",
     "Transmission oil pressure."),
    # exhaust / aftertreatment
    ("exhaust_temp_c", "C", "temperature", "float",
     "Exhaust gas temperature. SPN 173."),
    ("def_level_pct", "%", "percent", "float",
     "Diesel exhaust fluid level. SPN 1761."),
    ("dpf_soot_load_pct", "%", "percent", "float",
     "Diesel particulate filter soot load. SPN 3719."),
    ("dpf_regen_state", None, None, "string",
     "Particulate filter regeneration state. SPN 3751."),
    ("dpf_inlet_temp_c", "C", "temperature", "float", "DPF inlet temperature."),
    ("dpf_outlet_temp_c", "C", "temperature", "float", "DPF outlet temperature."),
    # electrical
    ("alternator_voltage", "V", "voltage", "float", "Alternator output voltage."),
    ("alternator_current_a", "A", "current", "float", "Alternator output current."),
    # hydraulics
    ("hydraulic_temp_c", "C", "temperature", "float", "Hydraulic oil temperature."),
    ("hydraulic_pressure_kpa", "kPa", "pressure", "float", "Hydraulic pressure."),
    # location
    ("gps_latitude", "deg", "angle", "float", "GPS latitude."),
    ("gps_longitude", "deg", "angle", "float", "GPS longitude."),
    ("gps_altitude_m", "m", "length", "float", "GPS altitude."),
    ("gps_heading_deg", "deg", "angle", "float", "GPS heading."),
    # brakes / safety
    ("brake_air_pressure_kpa", "kPa", "pressure", "float",
     "Service brake air pressure. SPN 117/118."),
    ("parking_brake_active", None, None, "bool", "Parking brake applied. SPN 70."),
    ("service_brake_active", None, None, "bool", "Service brake applied. SPN 597."),
    # PTO / cruise
    ("pto_active", None, None, "bool", "Power take-off engaged. SPN 976."),
    ("pto_speed_rpm", "rpm", "rotational", "float", "PTO output speed."),
    ("cruise_control_active", None, None, "bool", "Cruise control active. SPN 595."),
    ("cruise_control_set_km_h", "kph", "vehicle_speed", "float",
     "Cruise control set speed. SPN 86."),
    # environment
    ("ambient_temp_c", "C", "temperature", "float",
     "Ambient air temperature. SPN 171."),
    ("barometric_pressure_kpa", "kPa", "pressure", "float",
     "Barometric pressure. SPN 108."),
    # AEMP / ISO 15143 construction overlay
    ("boom_angle_deg", "deg", "angle", "float", "Boom angle."),
    ("bucket_angle_deg", "deg", "angle", "float", "Bucket angle."),
    ("swing_angle_deg", "deg", "angle", "float", "Swing / slew angle."),
    ("platform_load_kg", "kg", "mass", "float", "Work platform load."),
    ("lift_height_m", "m", "length", "float", "Lift / platform height."),
    ("track_tension_kpa", "kPa", "pressure", "float", "Undercarriage track tension."),
    ("undercarriage_wear_pct", "%", "percent", "float", "Undercarriage wear."),
]

# Physics bounds, in the canonical unit. These are NOT decoration.
#
# The per-quantity defaults in value_validator are tuned for factory-floor
# equipment and are wrong for vehicles by an order of magnitude. Without these,
# two correct conversions were rejected on the first J1939 payload:
#
#   Odometer_km 142,800    -> nulled, "outside [-100000, 100000]"   (a normal
#                             truck odometer; over-the-road trucks reach 1.5M)
#   Deere_HydPSI 3,200 psi -> 22,063 kPa nulled, "outside [0, 1500]" (1500 kPa
#                             is 218 psi; construction hydraulics run 3-5k psi)
#
# A silently nulled reading is the failure mode a prospect notices last and
# trusts least, so every field here declares its own range.
BOUNDS = {
    "engine_speed_rpm": (0, 10000),
    "engine_coolant_temp_c": (-60, 200),
    "engine_oil_pressure_kpa": (0, 2000),
    "engine_oil_temp_c": (-60, 250),
    "engine_load_pct": (0, 125),          # J1939 permits brief >100% load
    "engine_hours": (0, 500000),
    "fuel_temp_c": (-60, 150),
    "turbo_boost_pressure_kpa": (0, 1000),
    "intake_manifold_temp_c": (-60, 300),
    "intercooler_temp_c": (-60, 300),
    "fuel_rate_lph": (0, 1000),
    "fuel_level_pct": (0, 100),
    "total_fuel_used_l": (0, 10000000),
    "idle_fuel_used_l": (0, 10000000),
    "idle_hours": (0, 500000),
    "vehicle_speed_km_h": (0, 250),
    "odometer_km": (0, 5000000),
    "transmission_temp_c": (-60, 250),
    "transmission_gear": (-10, 30),
    "transmission_oil_pressure_kpa": (0, 5000),
    "exhaust_temp_c": (-60, 1200),
    "def_level_pct": (0, 100),
    "dpf_soot_load_pct": (0, 200),
    "dpf_inlet_temp_c": (-60, 900),
    "dpf_outlet_temp_c": (-60, 900),
    "alternator_voltage": (0, 60),
    "alternator_current_a": (-500, 500),
    "hydraulic_temp_c": (-60, 250),
    "hydraulic_pressure_kpa": (0, 70000),   # ~10,000 psi
    "gps_latitude": (-90, 90),
    "gps_longitude": (-180, 180),
    "gps_altitude_m": (-500, 10000),
    "gps_heading_deg": (0, 360),
    "brake_air_pressure_kpa": (0, 2000),
    "pto_speed_rpm": (0, 10000),
    "cruise_control_set_km_h": (0, 250),
    "ambient_temp_c": (-60, 90),
    "barometric_pressure_kpa": (0, 120),
    "boom_angle_deg": (-90, 180),
    "bucket_angle_deg": (-180, 180),
    "swing_angle_deg": (-360, 360),
    "platform_load_kg": (0, 50000),
    "lift_height_m": (0, 200),
    "track_tension_kpa": (0, 70000),
    "undercarriage_wear_pct": (0, 100),
}

M: dict[str, str] = {}
TU: dict[str, str] = {}


def m(canon, *tags, unit=None):
    for t in tags:
        M[t] = canon
        if unit:
            TU[t] = unit


def m_u(canon, pairs):
    """pairs: (tag, source_unit) -- the unit that TAG carries on the wire."""
    for t, u in pairs:
        M[t] = canon
        if u:
            TU[t] = u


# ── engine ──────────────────────────────────────────────────────────────────
m("engine_speed_rpm", "EngineSpeed", "EngineRPM", "Engine_Speed", "Engine_RPM",
  "ENG_RPM", "engine_speed", "EngSpeed", "Eng_Speed", "RPM", "EngineSpeedRPM")
m_u("engine_coolant_temp_c", [
    ("EngineCoolantTemp", None), ("EngineCoolantTemp_F", "F"),
    ("EngineCoolantTemp_C", "C"), ("Engine_Coolant_Temp", None),
    ("Engine_Coolant_Temp_F", "F"), ("COOL_TEMP", None), ("CoolantTemp", None),
    ("coolant_temp", None), ("Coolant_Temp_F", "F"), ("EngineTemp_C", "C"),
    ("EngineTemp_F", "F"), ("EngineTemp", None), ("ECT", None)])
m_u("engine_oil_pressure_kpa", [
    ("EngineOilPressure", None), ("EngineOilPressure_PSI", "psi"),
    ("Engine_Oil_Pressure", None), ("Engine_Oil_Pressure_PSI", "psi"),
    ("OilPressure_PSI", "psi"), ("OilPressure", None),
    ("oil_pressure_psi", "psi"), ("Oil_Pressure", None), ("OIL_PRESS", None)])
m_u("engine_oil_temp_c", [
    ("EngineOilTemp", None), ("EngineOilTemp_F", "F"), ("Engine_Oil_Temp", None),
    ("OIL_TEMP", None), ("OilTemp", None), ("OilTemp_F", "F")])
m_u("fuel_temp_c", [
    ("FuelTemp", None), ("FuelTemp_F", "F"), ("Fuel_Temperature", None),
    ("Fuel_Temp", None)])
m("engine_load_pct", "EngineLoad_Pct", "EngineLoad", "Engine_Load",
  "LOAD_AT_RPM", "PercentLoadAtCurrentRPM", "engine_load_percent",
  "EngLoad", "Engine_Load_Pct")
m_u("turbo_boost_pressure_kpa", [
    ("TurboBoost_PSI", "psi"), ("TurboBoostPressure", None),
    ("Turbo_Boost", None), ("TurboBoost", None), ("BoostPressure", None),
    ("Boost_PSI", "psi")])
m_u("intake_manifold_temp_c", [
    ("IntakeManifoldTemp", None), ("IntakeManifoldTemp_F", "F"),
    ("Intake_Manifold_Temp", None), ("IMT", None)])
m_u("intercooler_temp_c", [
    ("IntercoolerTemp", None), ("IntercoolerTemp_F", "F"),
    ("INTC_TEMP", None), ("Intercooler_Temp", None)])

# ── fuel ────────────────────────────────────────────────────────────────────
m_u("fuel_rate_lph", [
    ("FuelRate_GPH", "gal/h"), ("FuelRate_LPH", "L/h"), ("FuelRate", None),
    ("Fuel_Rate", None), ("fuel_rate_gph", "gal/h"), ("InstantFuelRate", None),
    ("FuelConsumptionRate", None), ("Fuel_Rate_GPH", "gal/h")])
m("fuel_level_pct", "FuelLevel_Pct", "FuelLevel", "Fuel_Level",
  "fuel_level_percent", "FUEL_LEVEL", "FuelLevelPercent", "Fuel_Level_Pct")
m_u("total_fuel_used_l", [
    ("TotalFuelUsed_gal", "gal"), ("TotalFuelUsed_L", "L"),
    ("TotalFuelUsed", None), ("Total_Fuel_Used", None),
    ("Fuel_Used_gal", "gal"), ("FUEL_USED", None), ("FuelUsed", None),
    ("TotalFuelConsumed", None)])
m_u("idle_fuel_used_l", [
    ("TotalIdleFuel_gal", "gal"), ("TotalIdleFuel", None),
    ("IdleFuelUsed", None), ("Idle_Fuel_Used", None)])

# ── speed / distance ────────────────────────────────────────────────────────
m_u("vehicle_speed_km_h", [
    ("VehicleSpeed_MPH", "mph"), ("VehicleSpeed_KPH", "kph"),
    ("VehicleSpeed", None), ("Vehicle_Speed", None),
    ("GroundSpeed_MPH", "mph"), ("Ground_Speed_MPH", "mph"),
    ("ground_speed", None), ("GroundSpeed", None), ("GPS_Speed_MPH", "mph"),
    ("GPS_Speed", None), ("WheelBasedSpeed", None), ("Speed_MPH", "mph")])
m_u("odometer_km", [
    ("Odometer_km", "km"), ("Odometer_mi", "mi"), ("Odometer", None),
    ("TotalDistance_km", "km"), ("total_distance", None),
    ("TotalVehicleDistance", None), ("Total_Distance", None)])

# ── hours ───────────────────────────────────────────────────────────────────
m_u("engine_hours", [
    ("EngineHours", "h"), ("Engine_Hours", "h"), ("ENG_HRS", "h"),
    ("TotalEngineHours", "h"), ("engine_hours", "h"), ("Machine_Hours", "h"),
    ("MachineHours", "h"), ("EngHrs", "h"), ("Hourmeter", "h"),
    ("HourMeter", "h"), ("Operating_Hours_Engine", "h")])
m_u("idle_hours", [
    ("TotalIdleHours", "h"), ("Idle_Hours", "h"), ("IdleHours", "h"),
    ("idle_hours", "h"), ("idle_time_hours", "h"), ("TotalIdleTime", "h")])

# ── transmission ────────────────────────────────────────────────────────────
m_u("transmission_temp_c", [
    ("TransmissionTemp", None), ("TransmissionTemp_F", "F"),
    ("Transmission_Temp", None), ("TransOilTemp", None),
    ("TransOilTemp_F", "F"), ("Trans_Temp", None)])
m("transmission_gear", "TransmissionGear", "Transmission_Gear", "CurrentGear",
  "SelectedGear", "Gear")
m_u("transmission_oil_pressure_kpa", [
    ("TransmissionOilPressure", None), ("TransmissionOilPressure_PSI", "psi"),
    ("Trans_Oil_Pressure", None)])

# ── exhaust / aftertreatment ────────────────────────────────────────────────
m_u("exhaust_temp_c", [
    ("ExhaustTemp_F", "F"), ("ExhaustTemp_C", "C"), ("ExhaustTemp", None),
    ("EGT_F", "F"), ("EGT", None), ("Exhaust_Temp", None),
    ("ExhaustGasTemp", None)])
m("def_level_pct", "DEF_Level_Pct", "DEF_Level", "DEFLevel",
  "def_level_percent", "Urea_Level", "UreaLevel", "DEF_Tank_Level")
m("dpf_soot_load_pct", "DPF_Soot_Load_Pct", "DPF_Soot_Load", "DPFSootLoad",
  "dpf_soot_level", "SootLoad", "DPF_Soot")
m("dpf_regen_state", "DPF_Regen_Status", "DPFRegenStatus", "RegenStatus",
  "DPF_Regen_State")
m_u("dpf_inlet_temp_c", [("DPF_InletTemp_F", "F"), ("DPF_InletTemp", None),
                        ("DPF_Inlet_Temp", None)])
m_u("dpf_outlet_temp_c", [("DPF_OutletTemp_F", "F"), ("DPF_OutletTemp", None),
                         ("DPF_Outlet_Temp", None)])

# ── electrical ──────────────────────────────────────────────────────────────
m("battery_voltage", "BatteryVoltage", "Battery_Voltage", "battery_voltage",
  "BattVoltage", "SystemVoltage")
m("alternator_voltage", "AlternatorVoltage", "Alternator_Voltage", "AltVoltage")
m("alternator_current_a", "AlternatorCurrent", "Alternator_Current", "AltCurrent")

# ── hydraulics ──────────────────────────────────────────────────────────────
m_u("hydraulic_temp_c", [
    ("HydraulicTemp_F", "F"), ("HydraulicTemp_C", "C"), ("HydraulicTemp", None),
    ("Hydraulic_Temp", None), ("HydTemp", None), ("Hyd_Temp", None),
    ("HydraulicOilTemp", None)])
m_u("hydraulic_pressure_kpa", [
    ("HydraulicPressure_PSI", "psi"), ("HydraulicPressure", None),
    ("Hydraulic_Pressure", None), ("HydPSI", "psi"), ("HydPressure", None),
    ("Hyd_Pressure", None)])

# ── location ────────────────────────────────────────────────────────────────
m("gps_latitude", "GPS_Latitude", "Location_Lat", "Latitude", "lat", "GPS_Lat")
m("gps_longitude", "GPS_Longitude", "Location_Lon", "Longitude", "lon",
  "GPS_Lon", "Location_Long", "lng")
m_u("gps_altitude_m", [("GPS_Altitude_m", "m"), ("GPS_Altitude", None),
                       ("Altitude", None)])
m("gps_heading_deg", "GPS_Heading", "Heading", "GPS_Course", "Bearing")

# ── brakes ──────────────────────────────────────────────────────────────────
m_u("brake_air_pressure_kpa", [
    ("BrakeAirPressure_PSI", "psi"), ("BrakeAirPressure", None),
    ("Brake_Air_Pressure", None), ("AirPressure", None)])
m("parking_brake_active", "ParkingBrake", "Parking_Brake", "ParkBrake")
m("service_brake_active", "ServiceBrake", "Service_Brake", "BrakeSwitch")

# ── PTO / cruise ────────────────────────────────────────────────────────────
m("pto_active", "PTO_Status", "PTO_Active", "PTOStatus")
m("pto_speed_rpm", "PTO_Speed_RPM", "PTO_Speed", "PTOSpeed")
m("cruise_control_active", "CruiseControl_Active", "CruiseControlActive",
  "Cruise_Active")
m_u("cruise_control_set_km_h", [
    ("CruiseControl_SetSpeed", None), ("CruiseSetSpeed_MPH", "mph"),
    ("CruiseControlSetSpeed", None)])

# ── environment ─────────────────────────────────────────────────────────────
m_u("ambient_temp_c", [
    ("AmbientTemp_F", "F"), ("AmbientTemp_C", "C"), ("AmbientTemp", None),
    ("Ambient_Temperature", None), ("Ambient_Temp", None),
    ("OutsideAirTemp", None)])
m_u("barometric_pressure_kpa", [
    ("Barometric_kPa", "kPa"), ("BarometricPressure", None),
    ("Barometric_Pressure", None), ("BaroPressure", None)])

# ── AEMP / ISO 15143 construction overlay ───────────────────────────────────
m_u("boom_angle_deg", [("BoomAngle_deg", "deg"), ("BoomAngle", None),
                       ("Boom_Angle", None)])
m_u("bucket_angle_deg", [("BucketAngle_deg", "deg"), ("BucketAngle", None),
                         ("Bucket_Angle", None)])
m_u("swing_angle_deg", [("SwingAngle_deg", "deg"), ("SwingAngle", None),
                        ("Swing_Angle", None)])
m_u("platform_load_kg", [("PlatformLoad_lb", "lb"), ("PlatformLoad", None),
                         ("Platform_Load", None), ("PlatformLoad_kg", "kg")])
m_u("lift_height_m", [("LiftHeight_ft", "ft"), ("LiftHeight_m", "m"),
                      ("LiftHeight", None), ("Lift_Height", None),
                      ("PlatformHeight", None)])
m_u("track_tension_kpa", [("TrackTension_PSI", "psi"), ("TrackTension", None),
                          ("Track_Tension", None)])
m("undercarriage_wear_pct", "UndercarriageWear_Pct", "UndercarriageWear",
  "Undercarriage_Wear")

# ── OEM-prefixed aliases ────────────────────────────────────────────────────
# EquipmentShare's T3 and other mixed-fleet platforms prefix every tag with the
# machine's make. Same measurement, six vendors, one canonical.
_OEM_PREFIXES = ["CAT", "Deere", "JLG", "Genie", "Doosan", "Volvo", "Komatsu",
                 "Hitachi", "Kubota", "Bobcat", "Case", "NewHolland", "Takeuchi"]
_OEM_SUFFIXES = {
    "EngineRPM": "engine_speed_rpm", "EngRPM": "engine_speed_rpm",
    "EngineSpeed": "engine_speed_rpm",
    "CoolantTemp": "engine_coolant_temp_c", "EngineTemp": "engine_coolant_temp_c",
    "FuelRate": "fuel_rate_lph", "FuelLevel": "fuel_level_pct",
    "FuelUsed": "total_fuel_used_l",
    "EngHrs": "engine_hours", "EngineHours": "engine_hours",
    "Hours": "engine_hours", "Hourmeter": "engine_hours",
    "IdleHours": "idle_hours",
    "HydTemp": "hydraulic_temp_c", "HydraulicTemp": "hydraulic_temp_c",
    "HydPSI": "hydraulic_pressure_kpa",
    "HydraulicPressure": "hydraulic_pressure_kpa",
    "EngineLoad": "engine_load_pct", "Load": "engine_load_pct",
    "BoomAngle": "boom_angle_deg", "BucketAngle": "bucket_angle_deg",
    "SwingAngle": "swing_angle_deg",
    "PlatformLoad": "platform_load_kg", "LiftHeight": "lift_height_m",
    "BatterySOC": "battery_soc_pct", "BatteryVoltage": "battery_voltage",
    "DEFLevel": "def_level_pct", "Odometer": "odometer_km",
    "OilPressure": "engine_oil_pressure_kpa",
    "ExhaustTemp": "exhaust_temp_c",
}
_OEM_UNITS = {"HydPSI": "psi", "PlatformLoad": "lb", "LiftHeight": "ft"}
for _p in _OEM_PREFIXES:
    for _sfx, _canon in _OEM_SUFFIXES.items():
        tag = f"{_p}_{_sfx}"
        M.setdefault(tag, _canon)
        if _sfx in _OEM_UNITS:
            TU.setdefault(tag, _OEM_UNITS[_sfx])

PACK = {
    "oem": "j1939",
    "display_name": "SAE J1939 heavy vehicle / AEMP off-highway (CAN bus)",
    "vertical": "fleet",
    "protocol": "j1939_can",
    "aliases": ["can_bus", "canbus", "sae_j1939", "heavy_truck", "truck",
                "fleet", "aemp", "fms", "iso15143", "telematics",
                "geotab", "motive", "samsara", "trackunit", "equipmentshare",
                "caterpillar", "cat", "deere", "john_deere", "jlg", "genie",
                # NOT bare "doosan": that already means Doosan machine tools
                # (CNC) in the OEM domain table, and stealing it would route a
                # lathe's tags into a truck pack.
                "doosan_ce", "doosan_infracore",
                "volvo_ce", "komatsu", "kubota", "bobcat",
                "off_highway", "construction"],
    "source": "SAE J1939-71 SPN definitions, plus the field names Geotab, "
              "Motive, Samsara and Trackunit expose them under, plus the "
              "AEMP 2.0 / ISO 15143-3 off-highway overlay",
}


def registry_entry(name, unit, pq, ftype, desc):
    b = BOUNDS.get(name)
    entry = {
        "accepted_input_units": [],
        "conversion_required": [],
        "corpus_tags": 0,
        "description": desc,
        "measurement_type": "instantaneous",
        "observed_input_units": [],
        "quantity": pq,
        "si": True,
        "type": ftype,
        "unit": unit,
        "vertical": "fleet",
        "physical_quantity": pq,
        "isa95_category": "equipment_performance",
    }
    if b:
        entry["physics_bounds"] = {"min": float(b[0]), "max": float(b[1])}
        entry["valid_range"] = [b[0], b[1]]
    return entry


def dict_entry(unit, pq, ftype, desc):
    return {
        "description": desc,
        "example_value": None,
        "type": ftype,
        "unit": unit,
        "unit_source": "sandbox_declared",
        "vertical": "fleet",
        "physical_quantity": pq,
        "isa95_category": "equipment_performance",
    }


def main():
    reg = json.load(open(REGISTRY))
    pdict = json.load(open(PACK_DICT))

    added = 0
    for name, unit, pq, ftype, desc in FIELDS:
        if name not in reg["fields"]:
            reg["fields"][name] = registry_entry(name, unit, pq, ftype, desc)
            added += 1
        if name not in pdict["fields"]:
            pdict["fields"][name] = dict_entry(unit, pq, ftype, desc)

    for name, b in BOUNDS.items():
        e = reg["fields"].get(name)
        if e is not None:
            e["physics_bounds"] = {"min": float(b[0]), "max": float(b[1])}
            e["valid_range"] = [b[0], b[1]]

    known = set(pdict["fields"])
    missing = sorted({c for c in M.values() if c not in known})
    if missing:
        raise SystemExit("mapping targets missing from the dictionary:\n  "
                         + "\n  ".join(missing))

    reg["field_count"] = len(reg["fields"])
    pdict["field_count"] = len(pdict["fields"])
    reg["fields"] = dict(sorted(reg["fields"].items()))
    pdict["fields"] = dict(sorted(pdict["fields"].items()))

    canon_fields = sorted(set(M.values()))
    pack = {
        "aliases": sorted(PACK["aliases"]),
        "canonical_fields": canon_fields,
        "display_name": PACK["display_name"],
        "mappings": dict(sorted(M.items())),
        "oem": PACK["oem"],
        "protocol": PACK["protocol"],
        "source": PACK["source"],
        "tag_units": dict(sorted(TU.items())),
        "vertical": PACK["vertical"],
    }
    with open(os.path.join(PACK_DIR, "j1939.json"), "w") as fh:
        json.dump(pack, fh, indent=2, sort_keys=True)
        fh.write("\n")

    index = json.load(open(os.path.join(PACK_DIR, "_index.json")))
    index["packs"]["j1939"] = {
        "aliases": sorted(PACK["aliases"]),
        "canonical_field_count": len(canon_fields),
        "display_name": PACK["display_name"],
        "mapping_count": len(M),
        "protocol": PACK["protocol"],
        "vertical": PACK["vertical"],
    }
    index["packs"] = dict(sorted(index["packs"].items()))
    with open(os.path.join(PACK_DIR, "_index.json"), "w") as fh:
        json.dump(index, fh, indent=1, sort_keys=True)
        fh.write("\n")
    for path, blob in ((REGISTRY, reg), (PACK_DICT, pdict)):
        with open(path, "w") as fh:
            json.dump(blob, fh, indent=2, sort_keys=True)
            fh.write("\n")

    print(f"  j1939  {len(M)} mappings  {len(canon_fields)} canonicals  "
          f"{len(TU)} tag units")
    print(f"  {added} new canonical fields; registry now {reg['field_count']}")


if __name__ == "__main__":
    main()
