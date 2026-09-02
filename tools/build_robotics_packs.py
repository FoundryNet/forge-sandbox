#!/usr/bin/env python3
"""Build the robotics / warehouse-automation packs and the canonical fields
they resolve into.

WHY A GENERATOR AND NOT SIX HAND-WRITTEN JSON FILES
---------------------------------------------------
Six vendors ship the same six-axis arm and the same four-wheel AMR under six
spellings. Written by hand that is ~250 mappings and ~110 canonical entries in
which a single typo produces a field the resolver silently records as
UNRESOLVED -- the exact failure this pack set exists to remove. Generating them
from one table means a joint is described once and every vendor's spelling of
it is derived.

Idempotent: re-running rewrites the same bytes. Safe to run after editing the
tables below.

    python3 tools/build_robotics_packs.py

WHAT IT WRITES
    app/packs/<oem>.json            one pack per vendor family
    app/packs/_canonical_fields.json  resolution vocabulary (adds only)
    app/canonical_fields.json         unit contract (adds only)
    app/packs/_index.json             pack manifest

DESIGN NOTES THAT ARE NOT OBVIOUS
    * Joint angles are canonically DEGREES. UR is the only vendor here that
      puts radians on the wire; deg is what every teach pendant and operator
      reads. unit_converter grew an `angle` quantity so rad -> deg is a
      recorded conversion rather than a silent reinterpretation.
    * Robot joints do NOT reuse `axes.N.position_actual`. That field is
      declared `mm` -- it is a CNC linear axis. Putting a joint angle in it
      would be the same class of bug as the tcp_speed mm/min defect.
    * Existing `robot.*` / `amr.*` / `ros.*` canonicals are REUSED wherever one
      already means the right thing. Minting a parallel flat name for a field
      that already exists is what produced four disagreeing canonical lists in
      the first place (see field_registry.py).
"""
from __future__ import annotations

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PACK_DIR = os.path.join(ROOT, "app", "packs")
REGISTRY = os.path.join(ROOT, "app", "canonical_fields.json")
PACK_DICT = os.path.join(PACK_DIR, "_canonical_fields.json")

JOINTS = range(6)
# Wheel/motor positions an AMR actually labels. `drive` and `lift` are the two
# functional motors on a pick robot; the rest are geometric.
MOTOR_POSITIONS = ["fl", "fr", "rl", "rr", "left", "right", "drive", "lift",
                   "steer"]

# ── new canonical fields ────────────────────────────────────────────────────
# (name, unit, physical_quantity, vertical, description)
NEW_FIELDS: list[tuple] = []


def add(name, unit, pq, vertical, desc):
    NEW_FIELDS.append((name, unit, pq, vertical, desc))


for i in JOINTS:
    add(f"robot.joint.{i}.position", "deg", "angle", "robotics",
        f"Joint {i} angular position. Degrees; radians on the wire are converted.")
    add(f"robot.joint.{i}.velocity", "rad/s", None, "robotics",
        f"Joint {i} angular velocity.")
    add(f"robot.joint.{i}.current", "A", "current", "robotics",
        f"Joint {i} motor current.")
    add(f"robot.joint.{i}.torque", "Nm", "torque", "robotics",
        f"Joint {i} torque.")
    add(f"robot.joint.{i}.temperature", "C", "temperature", "robotics",
        f"Joint {i} motor winding temperature.")
    add(f"robot.joint.{i}.drive_temperature", "C", "temperature", "robotics",
        f"Joint {i} servo drive (amplifier) temperature. Distinct from the "
        f"motor winding temperature -- different component, different limits.")

for ax in ("x", "y", "z"):
    add(f"robot.tcp.position_{ax}", "mm", "length", "robotics",
        f"Tool centre point {ax.upper()} position.")
    add(f"robot.tcp.force_{ax}", "N", "force", "robotics",
        f"Tool centre point force along {ax.upper()}.")
for ax in ("rx", "ry", "rz"):
    add(f"robot.tcp.orientation_{ax}", "deg", "angle", "robotics",
        f"Tool centre point orientation about {ax[1].upper()}.")

add("robot.controller.temperature", "C", "temperature", "robotics",
    "Robot controller cabinet temperature.")
add("robot.controller.uptime_s", "s", "time", "robotics",
    "Controller uptime.")
add("robot.operating_mode", None, None, "robotics",
    "Controller operating mode (KUKA AUT/T1/T2, ABB AUTO/MANUAL).")
add("robot.safety.gate_state", None, None, "robotics",
    "Safety gate / light curtain state.")
add("robot.motor_on", None, None, "robotics",
    "Drive power enabled.")

# AMR / logistics
add("amr.odometer_m", "m", "length", "amr", "Lifetime distance travelled.")
add("amr.lift_height_mm", "mm", "length", "amr", "Lift / mast height.")
add("amr.picks_completed", None, None, "amr", "Pick operations completed.")
add("amr.tasks_completed", None, None, "amr", "Transport tasks completed.")
add("amr.duty_cycles", None, None, "amr",
    "Robot duty cycles. NOT battery charge cycles -- see charge_cycles.")
add("amr.navigation_state", None, None, "amr", "Navigation stack state.")
add("amr.docking_state", None, None, "amr", "Charger docking state.")
add("amr.robot_state", None, None, "amr", "Overall robot state.")
add("amr.obstacle_detected", None, None, "amr", "Obstacle present in path.")
add("amr.safety_stop_active", None, None, "amr", "Safety stop asserted.")
add("amr.idle_time_s", "s", "time", "amr", "Time spent idle.")
add("amr.charge_time_s", "s", "time", "amr", "Time spent charging.")
add("amr.error_count", None, None, "amr", "Active error count.")
add("amr.error_code", None, None, "amr", "Vendor error code.")
add("amr.mission_id", None, None, "amr", "Current mission identifier.")
add("amr.distance_to_target_m", "m", "length", "amr",
    "Remaining distance to the current target.")
add("amr.battery.time_remaining_s", "s", "time", "amr",
    "Estimated battery runtime remaining.")
add("amr.battery.charge_current_a", "A", "current", "amr",
    "Charger current into the pack. Positive when charging.")
add("amr.battery.temperature_c", "C", "temperature", "amr",
    "Battery pack temperature.")

for p in MOTOR_POSITIONS:
    add(f"motors.{p}.temperature", "C", "temperature", "universal",
        f"Motor temperature, {p} position. The position is part of the field "
        f"name on purpose: a hot motor is only actionable if you know which one.")
    add(f"motors.{p}.current_a", "A", "current", "universal",
        f"Motor current, {p} position.")
    add(f"motors.{p}.speed_rpm", "rpm", "rotational", "universal",
        f"Motor speed, {p} position.")

# Conveyor / sortation
add("conveyor.belt_speed", "m/min", "linear_speed", "logistics",
    "Belt linear speed.")
add("conveyor.throughput_per_hour", None, None, "logistics",
    "Packages per hour.")
add("conveyor.throughput_per_minute", None, None, "logistics",
    "Packages per minute.")
add("conveyor.total_throughput", None, None, "logistics",
    "Lifetime package count.")
add("conveyor.reject_count", None, None, "logistics", "Rejected package count.")
add("conveyor.jam_detected", None, None, "logistics", "Jam condition present.")
add("conveyor.zone_occupied", None, None, "logistics", "Zone occupancy.")
add("conveyor.zone_1_occupied", None, None, "logistics", "Zone 1 occupancy.")
add("conveyor.zone_2_occupied", None, None, "logistics", "Zone 2 occupancy.")
add("conveyor.diverter_state", None, None, "logistics", "Diverter 1 state.")
add("conveyor.diverter_state_2", None, None, "logistics", "Diverter 2 state.")
add("conveyor.scanner_read_rate_pct", "%", "percent", "logistics",
    "Barcode scanner read rate.")
add("conveyor.package_weight_kg", "kg", "mass", "logistics",
    "Weighed package mass.")

# Declared TYPE per field. A boolean or an enum in a field the schema calls
# numeric trips the output invariant (TYPE_LEAK) and the reading is nulled --
# so `BatteryCharging: false` landing in a float field loses the reading and
# reports a violation. Applied to EXISTING canonicals too: several `amr.*`
# state fields predate this pack set and were never given a type, and it is
# this pack set that first sends them real enum and boolean traffic.
TYPE_OVERRIDES = {}
for _f in [
    "amr.battery.charging", "amr.obstacle_detected", "amr.safety_stop_active",
    "amr.safety.estop", "emergency_stop_active", "robot.motor_on",
    "robot.safety.protective_stop", "conveyor.jam_detected",
    "conveyor.zone_occupied", "conveyor.zone_1_occupied",
    "conveyor.zone_2_occupied",
]:
    TYPE_OVERRIDES[_f] = "bool"
for _f in [
    "amr.navigation_state", "amr.docking_state", "amr.robot_state",
    "amr.operating_mode", "amr.error_code", "amr.mission_id",
    "robot.operating_mode", "robot.safety.gate_state", "robot.mode",
    "robot.safety.mode", "robot.safety.status_bits", "robot.program.state",
    "conveyor.diverter_state", "conveyor.diverter_state_2",
]:
    TYPE_OVERRIDES[_f] = "string"
for _f in [
    "amr.picks_completed", "amr.tasks_completed", "amr.duty_cycles",
    "amr.error_count", "conveyor.reject_count", "conveyor.total_throughput",
    "conveyor.throughput_per_hour", "conveyor.throughput_per_minute",
]:
    TYPE_OVERRIDES[_f] = "int"


# Units to BACKFILL onto canonicals that already exist but declare none. These
# are fields this pack set now drives real traffic into, and a null unit is how
# a scale error hides (see the PWM 0-127 case in the printer pack).
BACKFILL_UNITS = {
    "robot.tcp.speed": ("mm/s", "linear_speed"),
    "robot.tcp.force": ("N", "force"),
    "robot.tcp.pose": (None, None),
    "robot.joint.position": ("deg", "angle"),
    "robot.joint.velocity": ("rad/s", None),
    "robot.joint.current": ("A", "current"),
    "robot.joint.temperature": ("C", "temperature"),
    "amr.velocity.linear": ("m/s", "linear_speed"),
    "amr.velocity.angular": ("rad/s", None),
    "amr.position.x": ("m", "length"),
    "amr.position.y": ("m", "length"),
    "amr.position.theta": ("deg", "angle"),
    "amr.battery.voltage": ("V", "voltage"),
    "ros.wifi_signal_dbm": ("dBm", None),
    "ros.battery_voltage": ("V", "voltage"),
    "ros.battery_current": ("A", "current"),
}


# ── pack mapping tables ─────────────────────────────────────────────────────
def joint_map(prefix_fmt, canon_suffix, first=0):
    """{prefix_fmt.format(n) : robot.joint.<i>.<canon_suffix>} for six joints.

    `first` is the vendor's own numbering base: UR counts joints from 0, KUKA
    and ABB label the same six axes 1..6. The canonical is always 0-based.
    """
    return {prefix_fmt.format(n + first): f"robot.joint.{n}.{canon_suffix}"
            for n in JOINTS}


UR_MAPPINGS: dict[str, str] = {}
UR_TAG_UNITS: dict[str, str] = {}
UR_MAPPINGS.update(joint_map("actual_q_{}", "position"))
UR_MAPPINGS.update(joint_map("target_q_{}", "position"))
UR_MAPPINGS.update(joint_map("actual_qd_{}", "velocity"))
UR_MAPPINGS.update(joint_map("actual_current_{}", "current"))
UR_MAPPINGS.update(joint_map("target_moment_{}", "torque"))
UR_MAPPINGS.update(joint_map("joint_temperatures_{}", "temperature"))
# The legacy primary-interface spelling of the same six sensors.
UR_MAPPINGS.update(joint_map("motor_temperatures_{}", "temperature"))
for n in JOINTS:
    # RTDE is radians; the canonical is degrees. Declaring it here is what makes
    # the conversion happen and get recorded instead of a 57x silent error.
    UR_TAG_UNITS[f"actual_q_{n}"] = "rad"
    UR_TAG_UNITS[f"target_q_{n}"] = "rad"
UR_MAPPINGS.update({
    "actual_TCP_pose_0": "robot.tcp.position_x",
    "actual_TCP_pose_1": "robot.tcp.position_y",
    "actual_TCP_pose_2": "robot.tcp.position_z",
    "actual_TCP_pose_3": "robot.tcp.orientation_rx",
    "actual_TCP_pose_4": "robot.tcp.orientation_ry",
    "actual_TCP_pose_5": "robot.tcp.orientation_rz",
    "actual_TCP_speed_0": "robot.tcp.speed",
    "actual_TCP_force_0": "robot.tcp.force_x",
    "actual_TCP_force_1": "robot.tcp.force_y",
    "actual_TCP_force_2": "robot.tcp.force_z",
    "tcp_force": "robot.tcp.force",
    "robot_mode": "robot.mode",
    "safety_mode": "robot.safety.mode",
    "safety_status_bits": "robot.safety.status_bits",
    "safety_status": "robot.safety.status_bits",
    "runtime_state": "robot.program.state",
    # `speed_scaling` is the ACTUAL scaling; `target_speed_fraction` is the
    # commanded one. Mapping both onto robot.speed_scaling made every UR
    # payload that ships both collide, and silently kept whichever sorted
    # first. Actual is the measurement; the target is not mapped.
    "speed_scaling": "robot.speed_scaling",
    "payload_mass": "payload_kg",
    "payload": "payload_kg",
    "tool_analog_input_0": "ros.sensor_quality_pct",
    "actual_robot_current": "robot.joint.0.current",
    "joint_control_output_0": "robot.joint.0.current",
    "timestamp": "robot.controller.uptime_s",
})
UR_TAG_UNITS.update({
    "actual_TCP_pose_0": "m", "actual_TCP_pose_1": "m", "actual_TCP_pose_2": "m",
    "actual_TCP_pose_3": "rad", "actual_TCP_pose_4": "rad",
    "actual_TCP_pose_5": "rad",
    "actual_TCP_speed_0": "m/s",
    "payload_mass": "kg", "timestamp": "s",
})

KUKA_MAPPINGS: dict[str, str] = {}
KUKA_TAG_UNITS: dict[str, str] = {}
KUKA_MAPPINGS.update(joint_map("Axis{}_Torque", "torque", first=1))
KUKA_MAPPINGS.update(joint_map("Axis{}_Position", "position", first=1))
KUKA_MAPPINGS.update(joint_map("Axis{}_Current", "current", first=1))
KUKA_MAPPINGS.update(joint_map("Axis{}_Temp", "temperature", first=1))
KUKA_MAPPINGS.update(joint_map("A{}_Pos", "position", first=1))
KUKA_MAPPINGS.update(joint_map("$AXIS_ACT[{}]", "position", first=1))
KUKA_MAPPINGS.update(joint_map("$TORQUE_AXIS_ACT[{}]", "torque", first=1))
KUKA_MAPPINGS.update(joint_map("$CURR_ACT[{}]", "current", first=1))
KUKA_MAPPINGS.update(joint_map("MotorTemp_{}", "temperature", first=1))
KUKA_MAPPINGS.update(joint_map("DriveTemp_{}", "drive_temperature", first=1))
for n in JOINTS:
    KUKA_TAG_UNITS[f"Axis{n + 1}_Position"] = "deg"
    KUKA_TAG_UNITS[f"$AXIS_ACT[{n + 1}]"] = "deg"
KUKA_MAPPINGS.update({
    "$VEL_ACT": "robot.tcp.speed",
    "$POS_ACT": "robot.tcp.position_x",
    "TCP_Speed": "robot.tcp.speed",
    "TCP_Speed_mm_s": "sensor_readings.tcp_speed",
    "TCP_Velocity": "robot.tcp.speed",
    "TCP_Force": "robot.tcp.force",
    "TCP_Force_N": "robot.tcp.force",
    "CycleTime": "sensor_readings.cycle_time",
    "CycleTime_s": "sensor_readings.cycle_time",
    "PartsProduced": "part_count",
    "PartCount": "part_count",
    "ProgramNumber": "program_id",
    "ProgramName": "program_name",
    "OperatingMode": "robot.operating_mode",
    "$MODE_OP": "robot.operating_mode",
    "DriveTemp": "robot.joint.0.drive_temperature",
    "DriveTemp_C": "robot.joint.0.drive_temperature",
    "ControllerTemp": "robot.controller.temperature",
    "ControllerTemp_C": "robot.controller.temperature",
    "CabinetTemp": "robot.controller.temperature",
    "RunHours": "operating_hours",
    "OperatingHours": "operating_hours",
    "EStop": "emergency_stop_active",
    "EStop_Active": "emergency_stop_active",
    "$STOPMESS": "emergency_stop_active",
    "SafetyGate": "robot.safety.gate_state",
    "SafetyStop": "robot.safety.protective_stop",
    "MotorOn": "robot.motor_on",
    "$PRO_MODE": "robot.program.state",
    "Payload": "payload_kg",
    "PayloadMass": "payload_kg",
})
KUKA_TAG_UNITS.update({
    "TCP_Speed_mm_s": "mm/s", "$VEL_ACT": "mm/s", "CycleTime_s": "s",
    "RunHours": "h", "DriveTemp_C": "C", "ControllerTemp_C": "C",
    "TCP_Force_N": "N", "$POS_ACT": "mm",
})

ABB_MAPPINGS: dict[str, str] = {}
ABB_TAG_UNITS: dict[str, str] = {}
ABB_MAPPINGS.update(joint_map("Axis{}_Angle", "position", first=1))
ABB_MAPPINGS.update(joint_map("Axis{}_Torque", "torque", first=1))
ABB_MAPPINGS.update(joint_map("Axis{}_Current", "current", first=1))
ABB_MAPPINGS.update(joint_map("Motor{}_Temp", "temperature", first=1))
ABB_MAPPINGS.update(joint_map("Drive{}_Temp", "drive_temperature", first=1))
ABB_MAPPINGS.update(joint_map("J{}_Angle", "position", first=1))
for n in JOINTS:
    ABB_TAG_UNITS[f"Axis{n + 1}_Angle"] = "deg"
    ABB_TAG_UNITS[f"J{n + 1}_Angle"] = "deg"
ABB_MAPPINGS.update({
    "TCP_Speed": "robot.tcp.speed",
    "TCP_Force": "robot.tcp.force",
    "Cycle_Time": "sensor_readings.cycle_time",
    "Parts_Count": "part_count",
    "Program_Name": "program_name",
    "Program_Number": "program_id",
    "Operating_Mode": "robot.operating_mode",
    "Run_Hours": "operating_hours",
    "EStop_Active": "emergency_stop_active",
    "Motor_On": "robot.motor_on",
    "Controller_Temp": "robot.controller.temperature",
    "Speed_Override": "robot.speed_scaling",
    "Payload_Mass": "payload_kg",
})
ABB_TAG_UNITS.update({"TCP_Speed": "mm/s", "Cycle_Time": "s", "Run_Hours": "h"})

# One AMR pack serves Locus, Fetch/Zebra, 6 River, OTTO, Vecna and anything
# generic, because they ship the same measurements under different spellings.
AMR_MAPPINGS: dict[str, str] = {
    # battery
    "battery_pct": "battery_soc_pct",
    "battery_percent": "battery_soc_pct",
    "battery_percentage": "battery_soc_pct",
    "battery_level": "battery_soc_pct",
    "BatteryLevel": "battery_soc_pct",
    "BatteryPct": "battery_soc_pct",
    "battery_soc": "battery_soc_pct",
    "BatterySOC": "battery_soc_pct",
    "BatteryStateOfCharge": "battery_soc_pct",
    "state_of_charge": "battery_soc_pct",
    "soc": "battery_soc_pct",
    "battery_voltage": "battery_voltage",
    "battery_voltage_v": "battery_voltage",
    "BatteryVoltage": "battery_voltage",
    "battery_current": "battery_current_a",
    "BatteryCurrent": "battery_current_a",
    "charge_current_a": "amr.battery.charge_current_a",
    "charge_current": "amr.battery.charge_current_a",
    "battery_temp": "amr.battery.temperature_c",
    "battery_temp_c": "amr.battery.temperature_c",
    "BatteryTemp": "amr.battery.temperature_c",
    "battery_temperature": "amr.battery.temperature_c",
    "BatteryCharging": "amr.battery.charging",
    "is_charging": "amr.battery.charging",
    "battery_time_remaining": "amr.battery.time_remaining_s",
    # motors, with the position kept
    "drive_motor_temp_c": "motors.drive.temperature",
    "drive_motor_temp": "motors.drive.temperature",
    "lift_motor_temp_c": "motors.lift.temperature",
    "lift_motor_temp": "motors.lift.temperature",
    "drive_motor_current": "motors.drive.current_a",
    "lift_motor_current": "motors.lift.current_a",
    "steer_motor_temp": "motors.steer.temperature",
    # navigation
    "current_speed_mps": "amr.velocity.linear",
    "current_speed": "amr.velocity.linear",
    "Velocity_m_s": "amr.velocity.linear",
    "LinearVelocity": "amr.velocity.linear",
    "velocity_linear": "amr.velocity.linear",
    "linear_speed": "amr.velocity.linear",
    "AngularVelocity": "amr.velocity.angular",
    "velocity_angular": "amr.velocity.angular",
    "Heading_deg": "amr.position.theta",
    "heading": "amr.position.theta",
    "orientation": "amr.position.theta",
    "yaw": "amr.position.theta",
    "position_x": "amr.position.x",
    "position_y": "amr.position.y",
    "Odometer_m": "amr.odometer_m",
    "odometer": "amr.odometer_m",
    "distance_traveled_m": "amr.odometer_m",
    "distance_traveled": "amr.odometer_m",
    "TotalDistance": "amr.odometer_m",
    "distance_to_next_target": "amr.distance_to_target_m",
    "LocalizationScore": "ros.localization_confidence",
    "localization_score": "ros.localization_confidence",
    "obstacle_detected": "amr.obstacle_detected",
    "obstacle_distance": "ros.obstacle_distance_m",
    # work
    "picks_completed": "amr.picks_completed",
    "picks": "amr.picks_completed",
    "TasksCompleted": "amr.tasks_completed",
    "tasks_completed": "amr.tasks_completed",
    "missions_completed": "amr.tasks_completed",
    "CycleCount": "amr.duty_cycles",
    "cycle_count": "amr.duty_cycles",
    "mission_queue_id": "amr.mission_id",
    "mission_id": "amr.mission_id",
    # status
    "navigation_status": "amr.navigation_state",
    "nav_status": "amr.navigation_state",
    "RobotState": "amr.robot_state",
    "robot_state": "amr.robot_state",
    "state_id": "amr.robot_state",
    "DockingState": "amr.docking_state",
    "docking_state": "amr.docking_state",
    "SafetyStop": "amr.safety_stop_active",
    "safety_stop": "amr.safety_stop_active",
    "EmergencyStop": "amr.safety.estop",
    "mode_id": "amr.operating_mode",
    "operating_mode": "amr.operating_mode",
    # connectivity
    "wifi_rssi_dbm": "ros.wifi_signal_dbm",
    "wifi_rssi": "ros.wifi_signal_dbm",
    "WiFiSignal": "ros.wifi_signal_dbm",
    "wifi_signal": "ros.wifi_signal_dbm",
    # runtime
    "total_runtime_h": "operating_hours",
    "total_runtime": "operating_hours",
    "System_Runtime_Hours": "operating_hours",
    "runtime_hours": "operating_hours",
    "Uptime": "operating_hours",
    "uptime": "operating_hours",
    "time_idle_s": "amr.idle_time_s",
    "time_idle": "amr.idle_time_s",
    "time_charging_s": "amr.charge_time_s",
    "time_charging": "amr.charge_time_s",
    # errors
    "error_count": "amr.error_count",
    "errors": "amr.error_count",
    "ErrorCode": "amr.error_code",
    "error_code": "amr.error_code",
    # payload / lift
    "payload_kg": "payload_kg",
    "PayloadWeight_kg": "payload_kg",
    "payload_mass": "payload_kg",
    "payload_weight": "payload_kg",
    "LiftHeight_mm": "amr.lift_height_mm",
    "lift_height": "amr.lift_height_mm",
    "bot_id": "machine_id",
    "robot_id": "machine_id",
}
for pos in ("FL", "FR", "RL", "RR"):
    AMR_MAPPINGS[f"MotorTemp_{pos}"] = f"motors.{pos.lower()}.temperature"
    AMR_MAPPINGS[f"MotorCurrent_{pos}"] = f"motors.{pos.lower()}.current_a"
    AMR_MAPPINGS[f"WheelSpeed_RPM_{pos}"] = f"motors.{pos.lower()}.speed_rpm"
    AMR_MAPPINGS[f"WheelSpeed_{pos}"] = f"motors.{pos.lower()}.speed_rpm"
for side in ("Left", "Right"):
    AMR_MAPPINGS[f"Motor{side}Temp"] = f"motors.{side.lower()}.temperature"
    AMR_MAPPINGS[f"Motor{side}Current"] = f"motors.{side.lower()}.current_a"
    AMR_MAPPINGS[f"motor_{side.lower()}_temp"] = f"motors.{side.lower()}.temperature"

AMR_TAG_UNITS = {
    "battery_voltage_v": "V", "charge_current_a": "A",
    "drive_motor_temp_c": "C", "lift_motor_temp_c": "C", "battery_temp_c": "C",
    "current_speed_mps": "m/s", "Velocity_m_s": "m/s",
    "Heading_deg": "deg", "Odometer_m": "m", "distance_traveled_m": "m",
    "wifi_rssi_dbm": "dBm", "total_runtime_h": "h",
    "System_Runtime_Hours": "h", "time_idle_s": "s", "time_charging_s": "s",
    "payload_kg": "kg", "PayloadWeight_kg": "kg", "LiftHeight_mm": "mm",
    "Uptime": "s", "uptime": "s",
}
for pos in ("fl", "fr", "rl", "rr"):
    AMR_TAG_UNITS[f"WheelSpeed_RPM_{pos.upper()}"] = "rpm"

MIR_MAPPINGS = {
    "battery_percentage": "battery_soc_pct",
    "battery_voltage": "battery_voltage",
    "battery_current": "battery_current_a",
    "battery_time_remaining": "amr.battery.time_remaining_s",
    "position_x": "amr.position.x",
    "position_y": "amr.position.y",
    "orientation": "amr.position.theta",
    "velocity_linear": "amr.velocity.linear",
    "velocity_angular": "amr.velocity.angular",
    "mission_queue_id": "amr.mission_id",
    "mission_text": "amr.navigation_state",
    "state_id": "amr.robot_state",
    "state_text": "amr.robot_state",
    "mode_id": "amr.operating_mode",
    "mode_text": "amr.operating_mode",
    "uptime": "operating_hours",
    "distance_to_next_target": "amr.distance_to_target_m",
    "errors": "amr.error_count",
    "robot_name": "machine_id",
    "serial_number": "serial_number",
}
MIR_TAG_UNITS = {"uptime": "s", "battery_time_remaining": "s"}

CONVEYOR_MAPPINGS = {
    "Belt_Speed_FPM": "conveyor.belt_speed",
    "Belt_Speed_MPM": "conveyor.belt_speed",
    "Belt_Speed": "conveyor.belt_speed",
    "belt_speed": "conveyor.belt_speed",
    "conveyor_speed": "conveyor.belt_speed",
    "Belt_Motor_Amps": "motor_current_a",
    "Belt_Motor_Current": "motor_current_a",
    "Belt_Motor_Temp_F": "motor_temperature",
    "Belt_Motor_Temp_C": "motor_temperature",
    "Belt_Motor_Temp": "motor_temperature",
    "Diverter_1_State": "conveyor.diverter_state",
    "Diverter_2_State": "conveyor.diverter_state_2",
    "Diverter_State": "conveyor.diverter_state",
    "Packages_Per_Hour": "conveyor.throughput_per_hour",
    "Packages_Per_Min": "conveyor.throughput_per_minute",
    "Throughput_Per_Hour": "conveyor.throughput_per_hour",
    "Jam_Detected": "conveyor.jam_detected",
    "Jam": "conveyor.jam_detected",
    "Zone_Occupied": "conveyor.zone_occupied",
    "Zone_1_Occupied": "conveyor.zone_1_occupied",
    "Zone_2_Occupied": "conveyor.zone_2_occupied",
    "Scanner_Read_Rate_Pct": "conveyor.scanner_read_rate_pct",
    "Scanner_Read_Rate": "conveyor.scanner_read_rate_pct",
    "Weight_Scale_lb": "conveyor.package_weight_kg",
    "Weight_Scale_kg": "conveyor.package_weight_kg",
    "Package_Weight": "conveyor.package_weight_kg",
    "Reject_Count": "conveyor.reject_count",
    "Rejects": "conveyor.reject_count",
    "Total_Throughput": "conveyor.total_throughput",
    "System_Runtime_Hours": "operating_hours",
    "Runtime_Hours": "operating_hours",
    "Emergency_Stop": "emergency_stop_active",
    "EStop": "emergency_stop_active",
}
CONVEYOR_TAG_UNITS = {
    "Belt_Speed_FPM": "ft/min", "Belt_Speed_MPM": "m/min",
    "Belt_Motor_Amps": "A", "Belt_Motor_Temp_F": "F", "Belt_Motor_Temp_C": "C",
    "Weight_Scale_lb": "lb", "Weight_Scale_kg": "kg",
    "Scanner_Read_Rate_Pct": "%", "System_Runtime_Hours": "h",
}

PACKS = [
    {
        "oem": "universal_robots",
        "display_name": "Universal Robots cobot (RTDE / primary interface)",
        "vertical": "robotics",
        "protocol": "rtde",
        "aliases": ["ur", "ur3", "ur5", "ur10", "ur16", "ur20", "ur_cobot",
                    "universalrobots", "polyscope", "rtde"],
        "source": "Universal Robots RTDE client interface field list "
                  "(docs.universal-robots.com, published and version-stable)",
        "mappings": UR_MAPPINGS,
        "tag_units": UR_TAG_UNITS,
    },
    {
        "oem": "kuka",
        "display_name": "KUKA industrial robot (KRL / KUKA.RobotSensorInterface)",
        "vertical": "robotics",
        "protocol": "krl_variable",
        "aliases": ["kuka_robot", "krc4", "krc5", "krl", "kuka_kr"],
        "source": "KUKA KRL system variables ($AXIS_ACT, $VEL_ACT, $POS_ACT) "
                  "and KUKA.SmartHMI signal names",
        "mappings": KUKA_MAPPINGS,
        "tag_units": KUKA_TAG_UNITS,
    },
    {
        "oem": "abb_robot",
        "display_name": "ABB industrial robot (RAPID / IRC5, OmniCore)",
        "vertical": "robotics",
        "protocol": "rapid_variable",
        # NB: bare "abb" is deliberately NOT an alias. It already means ABB the
        # grid/utility vendor in the OEM domain table, and stealing it would
        # route substation tags into a robot pack.
        "aliases": ["abb_irc5", "abb_omnicore", "irb", "rapid"],
        "source": "ABB RAPID motion and diagnostic variable conventions",
        "mappings": ABB_MAPPINGS,
        "tag_units": ABB_TAG_UNITS,
    },
    {
        "oem": "amr",
        "display_name": "Warehouse AMR (Locus, Fetch/Zebra, 6 River, OTTO, Vecna)",
        "vertical": "amr",
        "protocol": "rest_json",
        "aliases": ["locus", "locus_robotics", "fetch", "fetch_robotics",
                    "zebra", "zebra_robotics", "6river", "six_river",
                    "6_river_systems", "ocado", "otto", "otto_motors",
                    "vecna", "vecna_robotics", "hai_robotics", "haibot",
                    "addverb", "geek_plus", "greyorange", "grey_orange",
                    "generic_amr", "amr_generic", "agv"],
        "source": "Vendor REST/telemetry field names as shipped by Locus, "
                  "Fetch/Zebra and 6 River, plus the generic AMR spellings an "
                  "orchestration layer (SVT) normalises across them",
        "mappings": AMR_MAPPINGS,
        "tag_units": AMR_TAG_UNITS,
    },
    {
        "oem": "mir",
        "display_name": "Mobile Industrial Robots (MiR REST API v2)",
        "vertical": "amr",
        "protocol": "rest_json",
        "aliases": ["mobile_industrial_robots", "mir100", "mir250", "mir600",
                    "mir_amr"],
        "source": "MiR REST API v2.0 /status response fields",
        "mappings": MIR_MAPPINGS,
        "tag_units": MIR_TAG_UNITS,
    },
    {
        "oem": "generic_conveyor",
        "display_name": "Conveyor / sortation (Modula, AutoStore, generic MHE)",
        "vertical": "logistics",
        "protocol": "opcua",
        "aliases": ["conveyor", "sortation", "sorter", "modula", "autostore",
                    "mhe", "material_handling", "intralogistics"],
        "source": "Common warehouse conveyor / sortation PLC tag conventions",
        "mappings": CONVEYOR_MAPPINGS,
        "tag_units": CONVEYOR_TAG_UNITS,
    },
]


# ── writers ─────────────────────────────────────────────────────────────────
def registry_entry(name, unit, pq, vertical, desc):
    e = {
        "type": TYPE_OVERRIDES.get(name, "float"),
        "accepted_input_units": [],
        "conversion_required": [],
        "corpus_tags": 0,
        "description": desc,
        "measurement_type": "instantaneous",
        "observed_input_units": [],
        "quantity": pq,
        "si": True,
        "unit": unit,
        "vertical": vertical,
        "physical_quantity": pq,
        "isa95_category": "equipment_performance",
    }
    return e


def dict_entry(name, unit, pq, vertical, desc):
    return {
        "description": desc,
        "example_value": None,
        "type": TYPE_OVERRIDES.get(name, "float"),
        "unit": unit,
        "unit_source": "sandbox_declared",
        "vertical": vertical,
        "physical_quantity": pq,
        "isa95_category": "equipment_performance",
    }


def main():
    with open(REGISTRY) as fh:
        reg = json.load(fh)
    with open(PACK_DICT) as fh:
        pdict = json.load(fh)

    added = 0
    for name, unit, pq, vertical, desc in NEW_FIELDS:
        if name not in reg["fields"]:
            reg["fields"][name] = registry_entry(name, unit, pq, vertical, desc)
            added += 1
        if name not in pdict["fields"]:
            pdict["fields"][name] = dict_entry(name, unit, pq, vertical, desc)

    backfilled = 0
    for name, (unit, pq) in BACKFILL_UNITS.items():
        for store in (reg["fields"], pdict["fields"]):
            if name in store and store[name].get("unit") in (None, ""):
                store[name]["unit"] = unit
                store[name]["physical_quantity"] = pq
                if "quantity" in store[name]:
                    store[name]["quantity"] = pq
                backfilled += 1

    retyped = 0
    for name, t in TYPE_OVERRIDES.items():
        for store in (reg["fields"], pdict["fields"]):
            if name in store and store[name].get("type") != t:
                store[name]["type"] = t
                # A non-numeric field has no physics bounds to enforce, and a
                # leftover pair would keep it looking like a measurement.
                store[name].pop("physics_bounds", None)
                store[name].pop("valid_range", None)
                retyped += 1

    reg["field_count"] = len(reg["fields"])
    pdict["field_count"] = len(pdict["fields"])
    reg["fields"] = dict(sorted(reg["fields"].items()))
    pdict["fields"] = dict(sorted(pdict["fields"].items()))

    # Every mapping target must exist, or the pack ships tags that resolve to
    # a name the dictionary rejects -- silently UNRESOLVED, which is the whole
    # failure mode this build is meant to remove.
    known = set(pdict["fields"])
    problems = []
    for spec in PACKS:
        for tag, canon in spec["mappings"].items():
            if canon not in known:
                problems.append(f"{spec['oem']}: {tag} -> {canon}")
    if problems:
        raise SystemExit("mapping targets missing from the dictionary:\n  "
                         + "\n  ".join(sorted(problems)))

    index = json.load(open(os.path.join(PACK_DIR, "_index.json")))
    for spec in PACKS:
        canon_fields = sorted(set(spec["mappings"].values()))
        pack = {
            "aliases": sorted(spec["aliases"]),
            "canonical_fields": canon_fields,
            "display_name": spec["display_name"],
            "mappings": dict(sorted(spec["mappings"].items())),
            "oem": spec["oem"],
            "protocol": spec["protocol"],
            "source": spec["source"],
            "tag_units": dict(sorted(spec["tag_units"].items())),
            "vertical": spec["vertical"],
        }
        with open(os.path.join(PACK_DIR, f"{spec['oem']}.json"), "w") as fh:
            json.dump(pack, fh, indent=2, sort_keys=True)
            fh.write("\n")
        index["packs"][spec["oem"]] = {
            "aliases": sorted(spec["aliases"]),
            "canonical_field_count": len(canon_fields),
            "display_name": spec["display_name"],
            "mapping_count": len(spec["mappings"]),
            "protocol": spec["protocol"],
            "vertical": spec["vertical"],
        }
        print(f"  {spec['oem']:20s} {len(spec['mappings']):4d} mappings  "
              f"{len(canon_fields):3d} canonicals")

    index["packs"] = dict(sorted(index["packs"].items()))
    with open(os.path.join(PACK_DIR, "_index.json"), "w") as fh:
        json.dump(index, fh, indent=1, sort_keys=True)
        fh.write("\n")
    with open(REGISTRY, "w") as fh:
        json.dump(reg, fh, indent=2, sort_keys=True)
        fh.write("\n")
    with open(PACK_DICT, "w") as fh:
        json.dump(pdict, fh, indent=2, sort_keys=True)
        fh.write("\n")

    total = sum(len(s["mappings"]) for s in PACKS)
    print(f"\n{len(PACKS)} packs, {total} mappings, "
          f"{added} new canonical fields, {backfilled} unit backfills, "
          f"{retyped} retyped")
    print(f"registry now {reg['field_count']} fields")


if __name__ == "__main__":
    main()
