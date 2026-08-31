# Forge Sandbox

**Fake data, real schema.**

A local, keyless simulation of the [Forge](https://foundrynet.io) industrial
telemetry kernel. Run it on your laptop, build your agent integration against
it, then point the same code at production Forge to talk to real equipment.

No API key. No account. No signup. Nothing persisted. The app makes no
outbound calls.

```bash
docker run -p 8000:8000 ghcr.io/foundrynet/forge-sandbox
```

Multi-arch: `linux/amd64` and `linux/arm64`. Pin a version with
`ghcr.io/foundrynet/forge-sandbox:1.0.0` if you would rather not track `latest`.

Port 8000 already busy? `docker run -p 8099:8000 ...`, or with compose:
`FORGE_SANDBOX_PORT=8099 docker compose up`.

```bash
curl -X POST http://localhost:8000/v1/normalize \
  -H "Content-Type: application/json" \
  -d '{"oem": "haas", "data": {"S SPEED (RPM)": 8500, "SP_LOAD_PCT (%)": 84.7, "COOL_TEMP [°F]": 161.8}}'
```

```json
{
  "normalized": {
    "spindle_speed_rpm": 8500,
    "spindle_load_pct": 84.7,
    "sensor_readings.coolant_temp": 72.1111
  },
  "coverage_pct": 100.0,
  "fields_total": 3,
  "fields_distinct_canonical": 3,
  "unit_conversions": [
    {"raw_field": "COOL_TEMP [°F]", "canonical_field": "sensor_readings.coolant_temp",
     "from": "f", "to": "c", "conversion": "fahrenheit_to_celsius",
     "raw_value": 161.8, "converted_value": 72.1111}
  ],
  "oem": "haas",
  "vertical": "cnc",
  "simulated": true
}
```

---

## What this is for

Industrial equipment from N manufacturers produces telemetry in N incompatible
formats. Spindle speed is `S SPEED (RPM)` on a Haas, `Nist_Spindle (RPM)` on a
SINUMERIK, and `ACT_SP_SPEED_1/min` on a FANUC. Your agent should not have to
learn all three.

Forge translates any of them into one canonical vocabulary. The sandbox lets
you build against that vocabulary before you have equipment, credentials, or a
budget.

|  | Sandbox | Production |
|---|---|---|
| Data | simulated | your real machines |
| Canonical schema | **real** | **real** |
| Vendor tag mappings | 1,515 (public sources) | 16,908 curated |
| Unresolved tags | signal classifier | + embeddings, + LLM research, + self-healing |
| Forecasting | least squares | TimesFM (200M params) |
| Auth | none | API key |
| Persistence | none | history, identity, triggers, guardrails |
| Cost | free | [see pricing](https://foundrynet.io) |

**The response shapes are identical.** That is the contract. Build against the
sandbox, change the base URL, add a `Authorization: Bearer` header, and your
client code does not change.

---

## Five minutes

The sandbox ships five simulated machines. Each emits its **real vendor tag
names** — the actual spellings you meet on the wire.

```bash
# 1. See what's here
curl -s localhost:8000/v1/machines | jq '.machines[].description'

# 2. Pull a raw reading — vendor tags, unnormalized
curl -s localhost:8000/v1/simulate/siemens | jq .data
```

```json
{
  "Betriebszustand": "AUTOMATIK",
  "PROGRAMM": "WELLE_STUFE3.MPF",
  "Nist_Spindle (RPM)": 1203,
  "SPINDEL_AUSLASTUNG (%)": 62.4,
  "Kuehlmittel Temp (C)": 30.6,
  "STUECKZAHL (pcs)": 842,
  "Betriebsstunden": 14203.5
}
```

Your agent cannot guess that `STUECKZAHL` is a part count and `Betriebsstunden`
is operating hours. It does not have to:

```bash
# 3. Normalize it
curl -s localhost:8000/v1/simulate/siemens \
  | jq '{oem, data}' \
  | curl -s -X POST localhost:8000/v1/normalize -H 'Content-Type: application/json' -d @- \
  | jq .normalized
```

```json
{
  "execution_state": "AUTOMATIK",
  "program_name": "WELLE_STUFE3.MPF",
  "spindle_speed_rpm": 1203,
  "spindle_load_pct": 62.4,
  "sensor_readings.coolant_temp": 30.6,
  "part_count": 842,
  "operating_hours": 14203.5
}
```

```bash
# 4. Forecast — grab a series, ask whether it breaches
curl -s 'localhost:8000/v1/simulate/fanuc/series?field=MOTOR_TEMP&points=48' > /tmp/s.json

jq '{time_series: .values, threshold: 75.0, canonical_field: .canonical_field}' /tmp/s.json \
  | curl -s -X POST localhost:8000/v1/predict_breach -H 'Content-Type: application/json' -d @- \
  | jq '{will_breach, estimated_steps_to_breach, confidence, breach_window}'
```

---

## The five machines

| Key | Equipment | Protocol | Tag style |
|---|---|---|---|
| `haas` | Haas VF-2SS machining centre | MTConnect | `S SPEED (RPM)`, `SP_LOAD_PCT (%)` |
| `fanuc` | FANUC R-30iB 6-axis robot | FOCAS | `TCPVEL (mm/s)`, `PAYLOADKG(kg)` |
| `siemens` | SINUMERIK 840D sl / S7-1500 | PROFINET | `SPINDEL_AUSLASTUNG (%)`, `STUECKZAHL (pcs)` |
| `prusa` | Prusa MK3S+ 3D printer | Marlin serial | `hotend_temp`, `heater_power`, `pinda_temp` |
| `carrier` | Carrier 48TC rooftop HVAC | BACnet/IP | `SupplyTemp`, `DamperPosition`, `CO2` |

Add `?seed=N` to any simulate call to make it repeatable.

---

## Endpoints

| Endpoint | What it does |
|---|---|
| `POST /v1/normalize` | raw vendor telemetry → canonical fields (JSON or `text/csv`) |
| `POST /v1/predict_breach` | will a series cross a threshold, and when |
| `POST /v1/fleet_health` | fleet rollup, risk distribution, maintenance queue |
| `POST /v1/predict_batch` | per-machine predictions, no rollup |
| `GET /v1/coverage` | what can be normalized; pass `?oem=` to check one |
| `GET /v1/canonical-fields` | the canonical dictionary: name, type, unit, vertical |
| `GET /v1/machines` | the five simulated machines |
| `GET /v1/simulate/{machine}` | one raw reading |
| `GET /v1/simulate/{machine}/series?field=` | a history for one raw tag |
| `GET /health` | liveness (GET and HEAD) |
| `ANY /mcp` | MCP server, Streamable HTTP |
| `GET /docs` | OpenAPI browser |

Endpoints that exist in production but need durable state — `/v1/history`,
`/v1/identify`, `/v1/guardrails`, `/v1/triggers`, `/v1/attest`,
`/v1/billing/usage` — return **501 with the reason**, not a bare 404, so you can
tell "not in the sandbox" from "you typed it wrong".

---

## MCP

The sandbox is also an MCP server. Point Claude Desktop, Claude Code, or any
MCP client at `http://localhost:8000/mcp`.

```json
{
  "mcpServers": {
    "forge-sandbox": {
      "type": "http",
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

Claude Code:

```bash
claude mcp add --scope user --transport http forge-sandbox http://localhost:8000/mcp
```

`--scope user` matters. Without it `claude mcp add` registers the server **local
to the current directory**, so it resolves there and nowhere else — run
`claude mcp get forge-sandbox` from the project you actually want to use it in
and you get *"No MCP server named forge-sandbox"*. User scope makes it
available everywhere. To take it back out:

```bash
claude mcp remove forge-sandbox -s user
```

Eight tools. The five that exist in production carry **production's tool
descriptions verbatim**, because the description is the interface your agent
reasons about — if it reads differently here, the prompt behaviour you tune
against the sandbox will not carry over.

| Tool | |
|---|---|
| `normalize_telemetry` | production |
| `get_coverage` | production |
| `predict_breach` | production |
| `fleet_health` | production |
| `predict_batch` | production |
| `list_sandbox_machines` | sandbox only |
| `get_sandbox_reading` | sandbox only |
| `get_sandbox_series` | sandbox only |

Every tool description ends with a `SANDBOX:` note, so an agent reading the tool
list is told the data is simulated before it acts on anything.

Production Forge exposes **32** tools at `https://mcp.foundrynet.io/mcp`. The
other 24 need durable identity, history, guardrails, triggers, billing, or
on-chain attestation.

---

## How resolution actually works here

Production resolves a tag through five layers. The sandbox ships the three that
need no model weights, no network, and no proprietary data.

| Layer | Match type | Confidence | What it is |
|---|---|---|---|
| 1 | `corpus` | 1.00 | exact vendor tag in a mapping pack |
| 1b | `corpus_normalized` | 0.95 | same row, once case/punctuation/unit suffix are folded |
| 1c | `cross_oem` | 0.60 | another vendor's pack knew it — reported, not hidden |
| 2 | `identity` | 1.00 | the tag already IS a canonical field name |
| 3 | `signal` | 0.55–0.72 | deterministic subject+quantity classifier |
| — | `unknown` | 0.00 | honest miss |

A tag that resolves to nothing **keeps its raw name and value** in the output.
Nothing is silently dropped, and it does not count toward coverage.

`coverage_pct` is **distinct canonical fields ÷ total tags**. Ten spellings of
one quantity is one field covered, not ten. (Production had exactly this bug and
reported 100% coverage on an unseeded corpus.)

The sandbox never invents a canonical name. Every name it emits comes out of the
shipped dictionary, and the classifier's targets are validated against that
dictionary at startup — a typo fails the container, it does not ship a
plausible-looking wrong field.

---

## What is NOT in this image

Deliberately, and stated plainly so nothing here is mistaken for the real thing:

- **The production mapping corpus.** 16,908 curated mappings with confidence
  scores and provenance. The sandbox ships 1,515 mappings assembled from
  already-public sources only: the
  [MIT-licensed canonical schema](https://github.com/FoundryNet/canonical-schema)
  (haas, fanuc, siemens, octoprint), the shipped BACnet/IP vertical pack plus
  Carrier i-Vu object names, and the Marlin M105/M114 field names any Prusa
  emits over serial. `tools/build_packs.py` shows exactly where each row came
  from.
- **The embedding layer.** Production embeds unrecognized tags and matches them
  by similarity. No model weights here.
- **LLM field research.** Production sends genuinely novel tags to a model,
  caches the answer, confirms it at 5 uses, and packs it at 10. Not here.
- **Physics validators and read-time validators.** Rate-of-change, stuck sensor,
  dropout, operating mode, correlation, confidence decay. Not here.
- **TimesFM.** Production forecasts with a 200M-parameter time-series foundation
  model. The sandbox uses least squares with a residual-scaled quantile band.
  Every prediction is stamped `"model": "sandbox-ols-v1"` and `"simulated": true`.
- **Persistence, identity, history, triggers, guardrails, billing,
  attestation.** All stateful, all server-side.
- **Any connection to production.** The application imports no HTTP client and
  no socket API, so it makes no outbound calls — `grep -rE "httpx|requests|urllib|socket" app/`
  comes back empty. `docker-compose.yml` additionally runs it `read_only` with
  all capabilities dropped. Note that this hardens the filesystem, not the
  network: Docker's default bridge still permits egress, so if you need that
  enforced rather than merely true, run on an `internal` network.

Every response carries `"simulated": true` and an `X-Forge-Sandbox: true`
header. If you ever see those against a real endpoint, something is misrouted.

---

## Two things the sandbox does better than production

Both are known production issues, fixed here because a sandbox that teaches you
the wrong shape is worse than no sandbox.

1. **PWM scale is declared.** Marlin's `@:` heater field is a **0–127 duty
   byte**, not a percentage. Production's corpus emits `unit: null` for it, so a
   reading of `95` gets interpreted as "95%, near maximum" when it is really
   about 75%. The sandbox declares `unit: "pwm_0_127"`.

2. **Null units are backfilled from field names.** The published corpus declares
   a unit for only 58 of 366 fields. Where the field name states the unit
   (`_temperature_c`, `_pressure_bar`, `_rpm`), the sandbox fills it in and
   marks it `unit_source: "sandbox_inferred_from_name"`.

---

## Local development

```bash
docker compose up --build          # build and run your local changes
docker build --target test .       # run the suite inside the shipping image
```

Without Docker:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest tests/ -q
uvicorn app.main:app --reload --port 8000
```

### Before a demo or an evaluator call

```bash
python3 -m pytest tests/ --tb=short
```

Green means safe to demo. Red means fix it before dialing. One command runs
everything:

| suite | what it holds |
|---|---|
| `test_sandbox.py` | response shape and fidelity against production |
| `test_final_boss.py` | 49 fields, every bug class, all of them DECIDED |
| `test_sunspec_103.py` | Model 103, all 28 registers, shared scale factors |
| `test_own_pack.py` | 2,131 mappings across 19 packs resolve to their own canonical |
| `test_relief_valve.py` | the output invariants, on clean and on garbage |
| `test_opc_quality.py` | OPC UA Bad quality never ships as a reading |
| `test_impersonation.py` | all eight evaluator scenarios, shortfalls pinned |
| `test_demo_check.py` | the 35 beats a prospect sees, in the real container |
| `test_energy_vertical.py`, `test_evidence_gate.py` | vertical + gate coverage |

`test_demo_check.py` is the only one that leaves the process. It drives
`~/Desktop/licensing-demo/run_demo.sh --check` against the pinned demo image and
**skips** when docker or that directory is absent. Deselect the whole class with
`-m "not slow"`.

It is there because source being green does not mean the demo is. `run_demo.sh`
runs off a local pinned tag on purpose, so a demo cannot change mid-call — which
also means a fix in source never reaches it. On 2026-08-31 the demo had been
failing for five days while every source suite passed. After a GHCR push,
re-pin:

```bash
docker tag ghcr.io/foundrynet/forge-sandbox:latest forge-demo:pinned
```

Regenerate the mapping packs from source (needs the canonical-schema repo
checked out):

```bash
python3 tools/build_packs.py
```

```
forge-sandbox/
  app/
    main.py        FastAPI surface — the production response envelopes
    corpus.py      tag → canonical resolution, unit conversion, collisions
    simulate.py    the five machines
    predict.py     deterministic forecasting, production's response contract
    mcp_tools.py   MCP server, production tool descriptions
    packs/         generated mapping packs + the canonical dictionary
  tools/
    build_packs.py regenerates packs from the public sources
  tests/
    test_sandbox.py
```

---

## Upgrading to production

Two changes:

```diff
- BASE_URL = "http://localhost:8000"
- headers = {}
+ BASE_URL = "https://forge.foundrynet.io"
+ headers = {"Authorization": f"Bearer {FORGE_API_KEY}"}
```

For MCP, swap `http://localhost:8000/mcp` for `https://mcp.foundrynet.io/mcp`.

What changes underneath:

- Tags the sandbox reported as `unknown` get resolved by the embedding layer,
  the LLM research path, or the vertical packs.
- Predictions come from TimesFM instead of a straight line.
- Readings persist, so history, triggers, and guardrails start working.
- `/v1/identify` issues a durable machine identity.
- Predictions can be attested.

**Get a key: [foundrynet.io](https://foundrynet.io)**

---

```
Sandbox:     fake data, real schema
Production:  real data, real schema, real predictions

Upgrade:     foundrynet.io
```

## License

MIT. The mapping packs are derived from the MIT-licensed
[FoundryNet canonical schema](https://github.com/FoundryNet/canonical-schema);
`tools/build_packs.py` documents the provenance of every pack.

---

Forge by Foundry Labs · [forge@foundrynet.io](mailto:forge@foundrynet.io)
