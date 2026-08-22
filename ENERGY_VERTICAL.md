# Energy Vertical

Extends the Forge sandbox corpus to cover solar / storage / power-metering
equipment, and fixes two defects the 2026-08-22 microgrid simulation found in
the normalization pipeline.

Added 2026-08-22. `git log energy-vertical`.

---

## What changed

| | before | after |
|---|---|---|
| packs | 5 (HVAC, CNC, robotics, additive) | **9** (+ tesla, fronius, schneider, generic_iot) |
| registry `canonical_fields.json` | 366 fields | **394** |
| corpus dict `packs/_canonical_fields.json` | 408 fields | **435** |
| energy mappings | 0 | **151** |
| tests | 61 | **99** |

Coverage on the four simulation devices went from **16.67% to 100%**, and the
baseline and unit-instrumented runs now produce byte-identical output — tag
spelling no longer changes the result.

---

## The two fixes

### F1 — sentinel detection now runs before unit conversion

The pipeline converted units at `corpus.py:478` and checked for sentinels at
`corpus.py:489`. A wire sentinel on any converting field was laundered into a
plausible number before the gate could see it:

```
65535 in ALWAYS_SENTINEL_NUMBERS      -> True        (uint16 max, Modbus "no data")
convert_value(...) 65535 Wh           -> 65.535 kWh
validate_value('energy_kwh', 65.535)  -> passes      <-- sentinel is gone
```

`validate_value` was correct in isolation and caught the value every time it was
called first. Only the ordering defeated it, which is why every validator unit
test passed while the pipeline was broken.

**The fix is a two-stage split, because the two checks need different inputs.**

```
stage 1  validate_sentinel(canonical, RAW value)        <- sentinels only
stage 2  convert_value(...)
stage 3  normalize_enum(...) then validate_bounds(canonical, CONVERTED value)
```

Sentinels are properties of the number **as the device put it on the wire** —
65535 is uint16 max whether the register holds watt-hours or millibars — so they
must be tested before anything rewrites the value.

Physics bounds are the opposite: they are expressed in the canonical field's
**target unit**, so they must be tested after conversion. Moving them earlier
would swap one silent-corruption bug for a false-rejection bug. A thermal
runaway reading of 200 °F is 93.3 °C, comfortably inside
`battery_cell_temp_max_c`'s `[-40, 100]`; checked raw against those Celsius
bounds it would be nulled as impossible. `tests/test_energy_vertical.py::
test_f1_valid_fahrenheit_not_rejected_by_celsius_bounds` pins that case.

Every `null_state` now records which stage rejected the value:

```json
{"null_state": true, "null_reason": "numeric_sentinel: 65535 (integer type boundary)",
 "raw_value": 65535, "raw_field": "Pack_Energy_Wh", "stage": "pre_conversion"}
```

`validate_value(field, value)` still exists and still composes both stages, for
callers that already hold a value in canonical units.

### F2 — bare unit suffixes are detected in context

`_UNAMBIGUOUS_SUFFIX` excluded bare `"f"` and `"c"` (a trailing `_F` could be a
flag), but `_NAME_SUFFIXES` — used for *canonical field* names — included
`("_f", "F")`. The asymmetry produced one reading stored two ways:

```
tag `ambient_temp_f`      ->  ambient temp = 94.6        (no conversion, no warning)
tag `ambient_temp (degF)` ->  ambient temp = 34.777778
```

Excluding bare suffixes outright was the safe call in isolation. It produced a
worse failure than the one it prevented, because the wrong value was silent.

The fix resolves a bare single-letter unit **only when another token in the same
tag names the quantity it would belong to** (`_BARE_SUFFIX_CONTEXT`):

```
Cell_Temp_Max_F   -> F     "temp" is present
ambient_temp_f    -> F     "temp" is present
DC_Bus_V          -> V     "bus" is present
axis_pos_c        -> None  nothing says temperature — a CNC C-axis stays alone
status_f          -> None
```

Keep those context lists tight. A false positive converts a value that was
already correct, which is the exact failure this module exists to prevent.

---

## Three more defects found while building this

**Pack `tag_units` was loaded and never used.** `Pack.__init__` read
`raw.get("units", {})` into `self.units` and nothing ever consulted it — carrier
declared 11 unit overrides and prusa 2, all silently ignored. A new `tag_units`
map (raw tag → wire unit, distinct from `units`, which describes the canonical
field) is now honoured by `convert_value(source_unit=...)`.

This is required for SunSpec, which names a register for its quantity and
nothing else: models 101-103 call AC power `W` and lifetime energy `WH`. There
is no suffix to read and no safe way to guess from a one-token tag, so the
knowledge belongs to the pack.

**Precedence:** an explicit unit on the tag always beats the pack default. The
tag describes *this message*; the pack describes the model in general. So
`panel_temp (degC)` is Celsius even though generic_iot declares `panel_temp` as
Fahrenheit.

**`mph`/`kph` → `m/s` did not exist.** Wind speed lands on `wind_speed_m_s`
(WMO convention), consumer weather hardware ships mph, and the pair was missing
from `CONVERSIONS` — so the value was flagged `unit_unconvertible` and kept, a
2.24x error that looks entirely plausible.

**`W/m²` was not a distinct quantity.** It shared the `W` token, so a tag
declaring `(W)` against `solar_irradiance_w_m2` resolved as power, found the SI
power target `kW`, and divided the reading by 1000. Irradiance is now its own
quantity with its own target, and a power-labelled tag on an irradiance field is
refused with `unit_quantity_mismatch` instead of being silently scaled.

---

## Canonical fields

24 as specified, plus 3 the request did not cover but the device tags require.
All 27 were added to **both** field files — the registry
(`app/canonical_fields.json`, which gates `is_canonical` and carries
`physics_bounds`) and the corpus dictionary
(`app/packs/_canonical_fields.json`, which drives resolution). A field in only
one of them normalizes fine and then makes `predict_breach` warn that it is not
canonical.

**Battery** `battery_soc_pct` `battery_soh_pct` `charge_rate_kw`
`discharge_rate_kw` `charge_cycles` `dc_bus_voltage_v`

**Inverter** `inverter_output_kw` `inverter_cabinet_temp_c` `inverter_state`
`inverter_event_flags` `dc_current_a` `dc_voltage_v` `grid_frequency_hz`

**Meter** `active_power_kw` `reactive_power_kvar` `power_factor`
`line_voltage_v` `line_current_a` `energy_delivered_kwh` `energy_received_kwh`
`peak_demand_kw`

**Weather** `solar_irradiance_w_m2` `panel_temperature_c` `wind_speed_m_s`

**Added beyond the request** — each because a device tag had nowhere to land:

| field | why |
|---|---|
| `battery_cell_temp_max_c` | Tesla reports `Cell_Temp_Max`; no requested field holds a cell temperature. |
| `battery_capacity_kwh` | `Pack_Energy_Wh` is a nameplate rating. Mapping it to `energy_delivered_kwh` would put a static capacity into a cumulative production field — a category error that would corrupt any energy total computed from it. |
| `ambient_temperature_c` | The weather station's ambient reading had no home. |

Also added `relative_humidity_pct`, which the carrier and generic_iot packs
already targeted but which was missing from the registry (see *Known gap*).

`inverter_state` is an enum over SunSpec model 103 `St`, mapping both the
numeric codes (`1`=off … `8`=standby) and the common string spellings onto
`off / sleeping / starting / mppt / throttled / shutting_down / fault /
standby / unknown`. `unknown` was added to the requested value list because
`normalize_enum_value` emits it for unrecognised input and the contract has to
be able to represent what the validator produces.

---

## Packs

| pack | protocol | mappings | source |
|---|---|---|---|
| `tesla` | modbus_tcp | 42 | Observed register naming. **Tesla publishes no open register map** — these are tags seen on the wire, not a normative spec. |
| `fronius` | sunspec_modbus | 40 | **SunSpec Alliance models 101-103**, a public standard. Point names are normative. |
| `schneider` | modbus_rtu | 41 | Observed PowerLogic / ION naming. Units are carried as a tag **prefix** (`kWh_Del`), which no suffix rule can read — hence `tag_units`. |
| `generic_iot` | mqtt | 28 | Common MQTT weather-station topic naming across vendors. |

Each also gets a simulator machine, so `GET /v1/simulate/tesla` works and the
packs are demoable without supplying your own telemetry. The four energy
emitters share one solar-arc function, so irradiance, inverter output, meter
demand and battery SOC all move together across a simulated day rather than
jittering independently.

---

## Known gap: the registry and the corpus dictionary still disagree

Building this surfaced that **25 pre-existing pack targets are absent from the
registry** — 11 carrier fields (`space_temperature_c`, `co2_ppm`,
`condenser_pressure_bar`, …) and 14 prusa fields (`hotend_temperature_c`,
`print_progress_pct`, …). They resolve and emit normally, but `field_spec()`
returns `{}` for them, so they carry no `physics_bounds`, and `predict_breach`
reports them as non-canonical.

This is the same two-list divergence the field registry docstring was written
about. It is **not fixed here** — those are HVAC and additive fields outside
this change, and correcting them means choosing bounds for each, which should be
a deliberate pass rather than a side effect of the energy work. Only
`relative_humidity_pct` was added, because the generic_iot pack targets it.

`tests/test_energy_vertical.py::test_energy_fields_are_in_both_field_lists`
enforces the invariant for the energy packs so this class of gap cannot recur
there.

Findings F3, F4 and F5 from the simulation are also still open: `fleet_health`
emits no `field_warnings` at all, forecasts are not clamped to the field's
physics bounds (SOC still projects to −142%), and forecast confidence still
*rises* with horizon as the extrapolation leaves the physical envelope.

---

## Verifying

```bash
python3 -m pytest                       # 99 passed
docker build -t forge-sandbox:energy .
docker run -d -p 8101:8000 forge-sandbox:energy

cd ~/Desktop/energy-depin-simulation
FORGE_SANDBOX=http://localhost:8101 python3 scripts/verify_f1_f2.py   # exit 0
```
