"""SAFETY-CRITICAL PARITY — READ BEFORE EDITING THIS PACKAGE.

The production kernel (forge-prod/forge_core/) and the evaluation sandbox
(forge-sandbox/app/) carry SEPARATE copies of the contract tables: physics_validator,
unit_converter, value_validator, value_coercion, the type gate and sentinel
detection. They are hand-ported.

Any change to those MUST be ported to the other engine in the SAME work session,
with scripts/check_engine_parity.py re-run against both. Not later. Now.

The engine a licensee deploys must behave identically to the engine a prospect
evaluates. On 2026-09-06 it did not: prod served 9999 as a real spindle load,
"sixty-seven" as a number, German "4.200" as 4.2, and robot joint angles in
radians — every one of them handled correctly by the sandbox the prospect had
already run. The parity check found all of it AFTER the fact.

Until forge_core is a shared package, that script is the only thing standing
between a sandbox fix and a licensee running the unfixed engine.
"""

"""Forge sandbox — a local, keyless simulation of the Forge telemetry kernel."""

__version__ = "1.0.0"
