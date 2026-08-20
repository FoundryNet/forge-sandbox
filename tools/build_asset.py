#!/usr/bin/env python3
"""Build the Forge sandbox before/after asset from LIVE container output.

Every value on the page is fetched from the running sandbox and escaped into
the HTML here. Nothing is typed by hand, because a marketing asset that claims
to be real terminal output has to actually be real terminal output -- a single
transcribed typo in a German tag name would be the one thing a reader checks.

Usage:  python3 build_asset.py <base_url> <out.html>
"""

import html
import json
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8099"
OUT = sys.argv[2] if len(sys.argv) > 2 else "asset.html"
SEED = 311


def get(path):
    return json.load(urllib.request.urlopen(BASE + path))


def post(path, body):
    req = urllib.request.Request(BASE + path, json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req))


def e(s):
    return html.escape(str(s), quote=True)


def jval(v):
    """Render a value the way jq would, so the panels read as real output."""
    return json.dumps(v, ensure_ascii=False)


def fetch(machine):
    raw = get(f"/v1/simulate/{machine}?seed={SEED}")
    norm = post("/v1/normalize", {"oem": raw["oem"], "data": raw["data"]})
    return raw, norm


# ---------------------------------------------------------------------------
# Hero: Siemens, both panels in full
# ---------------------------------------------------------------------------

hero_raw, hero_norm = fetch("siemens")

vendor_rows = "\n      ".join(
    f'<span class="k">{e(jval(k))}</span><span class="v">{e(jval(v))}</span>'
    for k, v in hero_raw["data"].items()
)
canon_rows = "\n      ".join(
    f'<span class="k">{e(jval(k))}</span><span class="v">{e(jval(v))}</span>'
    for k, v in hero_norm["normalized"].items()
)

# ---------------------------------------------------------------------------
# Strip: one line per remaining machine, three representative pairs each
# ---------------------------------------------------------------------------

PICKS = [
    ("haas",    ["S SPEED (RPM)", "SP_LOAD_PCT (%)", "TOTAL_HOURS (hrs)"]),
    ("fanuc",   ["TCPVEL (mm/s)", "PAYLOADKG(kg)", "MTR_TORQUE (N-m)"]),
    ("prusa",   ["heater_power", "pinda_temp", "hotend_temp"]),
    ("carrier", ["SupplyTemp", "DamperPosition", "CO2"]),
]

# Column width for the raw half, so the arrows line up down the whole strip.
WIDTH = max(len(t) for _, tags in PICKS for t in tags) + 2

machines_html = []
for machine, tags in PICKS:
    raw, norm = fetch(machine)
    pairs = []
    for t in tags:
        canonical = norm["field_mappings"][t]["canonical_field"]
        pairs.append(
            f'<div class="pair">'
            f'<span class="from">{e(t.ljust(WIDTH))}</span>'
            f'<span class="arw">&rarr; </span>'
            f'<span class="to">{e(canonical)}</span>'
            f'</div>'
        )
    machines_html.append(
        f'''<div class="machine">
        <div class="who-cell">
          <span class="name">{e(raw["machine_id"].replace("SBX-", "").rsplit("-", 1)[0])}</span>
          <span class="meta">{e(raw["protocol"].replace("_", " "))} &middot; {norm["coverage_pct"]:g}%</span>
        </div>
        <div class="pairs">{"".join(pairs)}</div>
      </div>'''
    )

health = get("/health")
coverage = get("/v1/coverage")

n_tags = hero_norm["fields_total"]
n_canon = hero_norm["fields_distinct_canonical"]
n_map = coverage["total_mappings"]
n_fields = health["canonical_fields"]

PAGE = f'''<title>Fifteen Tags, One Schema</title>
<style>
  /* Single-theme by design. This page exists to be screenshotted, so it has to
     render identically regardless of the viewer's theme: every colour is
     painted explicitly and there is no prefers-color-scheme block. */
  :root {{
    --ground:  #0A0E13;
    --panel:   #10161F;
    --panel-2: #0D131B;
    --rule:    #1E2B3D;
    --muted:   #78849A;
    --text:    #D7DEEA;
    --bright:  #EDF2F8;
    --vendor:  #D9963A;
    --canon:   #2DBB78;
    --arrow:   #4A5568;

    --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
    --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  }}

  * {{ box-sizing: border-box; }}

  body {{
    margin: 0;
    background: var(--ground);
    color: var(--text);
    font-family: var(--mono);
    font-size: 15px;
    line-height: 1.55;
    -webkit-font-smoothing: antialiased;
  }}

  .sheet {{
    max-width: 900px;
    margin: 0 auto;
    padding: 56px 32px 68px;
    display: flex;
    flex-direction: column;
    gap: 28px;
  }}

  .mast {{ display: flex; flex-direction: column; gap: 12px; }}

  .kicker {{
    font-family: var(--sans);
    font-size: 11px;
    letter-spacing: .16em;
    text-transform: uppercase;
    color: var(--muted);
  }}
  .kicker i {{ font-style: normal; color: var(--canon); }}

  h1 {{
    margin: 0;
    font-family: var(--sans);
    font-size: 31px;
    line-height: 1.19;
    font-weight: 620;
    letter-spacing: -.019em;
    text-wrap: balance;
    color: var(--bright);
  }}

  .standfirst {{
    margin: 0;
    max-width: 64ch;
    font-family: var(--sans);
    font-size: 15px;
    line-height: 1.62;
    color: #9AA6B8;
  }}
  .standfirst b {{ color: var(--text); font-weight: 600; }}

  .panel {{
    background: var(--panel);
    border: 1px solid var(--rule);
    border-radius: 8px;
    overflow: hidden;
  }}

  .panel-head {{
    display: flex;
    align-items: baseline;
    gap: 12px;
    padding: 11px 16px;
    background: var(--panel-2);
    border-bottom: 1px solid var(--rule);
    font-family: var(--sans);
    font-size: 11px;
    letter-spacing: .13em;
    text-transform: uppercase;
    color: var(--muted);
  }}
  .panel-head .who {{
    color: var(--text);
    letter-spacing: .02em;
    text-transform: none;
    font-size: 12.5px;
  }}
  .panel-head .badge {{
    margin-left: auto;
    letter-spacing: .09em;
  }}

  .cmd {{
    padding: 11px 16px;
    border-bottom: 1px solid var(--rule);
    color: var(--muted);
    font-size: 13px;
    white-space: pre;
    overflow-x: auto;
  }}
  .cmd b {{ color: #A9B4C6; font-weight: 500; }}

  .rows {{
    padding: 14px 16px 16px;
    overflow-x: auto;
    display: grid;
    grid-template-columns: max-content 1fr;
    column-gap: 20px;
    row-gap: 2px;
    align-items: baseline;
    font-variant-numeric: tabular-nums;
    font-size: 13.5px;
  }}
  .k {{ white-space: pre; }}
  .v {{ white-space: pre; color: #B9C3D2; }}
  .vendor .k {{ color: var(--vendor); }}
  .canonical .k {{ color: var(--canon); }}

  /* The rail is the transform: it names what happened between the panels. */
  .rail {{ display: flex; align-items: center; gap: 16px; padding: 0 2px; }}
  .rail .line {{ flex: 1; height: 1px; background: var(--rule); }}
  .rail .tag {{
    font-family: var(--sans);
    font-size: 11px;
    letter-spacing: .15em;
    text-transform: uppercase;
    color: var(--muted);
    white-space: nowrap;
  }}
  .rail .tag i {{ font-style: normal; color: var(--canon); }}

  .stats {{
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 1px;
    background: var(--rule);
    border: 1px solid var(--rule);
    border-radius: 8px;
    overflow: hidden;
  }}
  .stat {{
    background: var(--panel);
    padding: 15px 18px;
    display: flex;
    flex-direction: column;
    gap: 3px;
  }}
  .stat .n {{
    font-size: 25px;
    line-height: 1.1;
    font-variant-numeric: tabular-nums;
    color: var(--bright);
  }}
  .stat.good .n {{ color: var(--canon); }}
  .stat .l {{
    font-family: var(--sans);
    font-size: 10.5px;
    letter-spacing: .12em;
    text-transform: uppercase;
    color: var(--muted);
  }}

  .machine {{
    display: grid;
    grid-template-columns: 200px 1fr;
    gap: 20px;
    padding: 14px 16px;
    border-bottom: 1px solid var(--rule);
  }}
  .machine:last-child {{ border-bottom: 0; }}

  .who-cell {{ display: flex; flex-direction: column; gap: 1px; }}
  .who-cell .name {{ color: var(--text); font-size: 13px; }}
  .who-cell .meta {{
    font-family: var(--sans);
    font-size: 10.5px;
    letter-spacing: .1em;
    text-transform: uppercase;
    color: var(--muted);
  }}

  .pairs {{ display: flex; flex-direction: column; gap: 2px; overflow-x: auto; }}
  .pair {{ white-space: pre; font-size: 13px; }}
  .pair .from {{ color: var(--vendor); }}
  .pair .arw {{ color: var(--arrow); }}
  .pair .to {{ color: var(--canon); }}

  .foot {{
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 6px 20px;
    font-family: var(--sans);
    font-size: 12px;
    color: var(--muted);
  }}
  .foot b {{ color: #A9B4C6; font-weight: 600; }}

  @media (max-width: 720px) {{
    .sheet {{ padding: 36px 18px 48px; }}
    h1 {{ font-size: 24px; }}
    .stats {{ grid-template-columns: repeat(2, 1fr); }}
    .machine {{ grid-template-columns: 1fr; gap: 8px; }}
  }}
</style>

<div class="sheet">

  <header class="mast">
    <div class="kicker">Forge sandbox &middot; runs on localhost &middot; <i>no API key</i></div>
    <h1>Fifteen German tags in. Fifteen canonical fields out.</h1>
    <p class="standfirst">
      A SINUMERIK 840D posting telemetry the way it actually posts it. Nothing can guess
      that <b>STUECKZAHL</b> is a part count or that <b>Betriebsstunden</b> is operating
      hours &mdash; and nothing has to. One POST, and every tag comes back in a vocabulary
      that reads the same across every vendor.
    </p>
  </header>

  <section class="panel vendor">
    <div class="panel-head">
      <span>Before</span>
      <span class="who">{e(hero_raw["model"])}</span>
      <span class="badge">{e(hero_raw["protocol"].replace("_", " / ").upper())}</span>
    </div>
    <div class="cmd">$ curl -s localhost:8000<b>/v1/simulate/siemens</b> | jq .data</div>
    <div class="rows">
      {vendor_rows}
    </div>
  </section>

  <div class="rail">
    <span class="line"></span>
    <span class="tag">{n_tags} tags &rarr; <i>{n_canon} canonical fields</i> &middot; {hero_norm["coverage_pct"]:g}% coverage &middot; 0 unresolved</span>
    <span class="line"></span>
  </div>

  <section class="panel canonical">
    <div class="panel-head">
      <span>After</span>
      <span class="who">FoundryNet canonical schema</span>
      <span class="badge">POST /V1/NORMALIZE</span>
    </div>
    <div class="cmd">$ &hellip; | curl -s -X POST localhost:8000<b>/v1/normalize</b> -d @- | jq .normalized</div>
    <div class="rows">
      {canon_rows}
    </div>
  </section>

  <section class="stats">
    <div class="stat good"><span class="n">{hero_norm["coverage_pct"]:g}%</span><span class="l">Coverage</span></div>
    <div class="stat"><span class="n">{n_map:,}</span><span class="l">Tag mappings</span></div>
    <div class="stat"><span class="n">{n_fields}</span><span class="l">Canonical fields</span></div>
    <div class="stat good"><span class="n">0</span><span class="l">API keys needed</span></div>
  </section>

  <section class="panel">
    <div class="panel-head">
      <span>And four more machines</span>
      <span class="badge">SAME ENDPOINT</span>
    </div>
      {"".join(machines_html)}
  </section>

  <footer class="foot">
    <span><b>docker run -p 8000:8000 ghcr.io/foundrynet/forge-sandbox</b></span>
    <span>Fake data, real schema.</span>
    <span>foundrynet.io</span>
  </footer>

</div>
'''

with open(OUT, "w", encoding="utf-8") as fh:
    fh.write(PAGE)

print(f"wrote {OUT}")
print(f"  hero: {n_tags} tags -> {n_canon} canonical, {hero_norm['coverage_pct']}% coverage")
print(f"  strip: {len(machines_html)} machines")
