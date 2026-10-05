# Third-Party Notices

`ghcr.io/foundrynet/forge-sandbox` bundles the third-party software listed below.
Each component remains under its own license and copyright; nothing here is
assigned to Foundry Labs LLC.

Forge Sandbox itself is MIT licensed — see `LICENSE`.

**No copyleft (GPL / AGPL / LGPL / SSPL) component is included in this image.**
Every direct Python dependency is MIT, BSD or Apache-2.0.

---

## Direct Python dependencies

| Component | License | Project |
|---|---|---|
| FastAPI | MIT | https://github.com/fastapi/fastapi |
| Uvicorn (with `[standard]` extras) | BSD-3-Clause | https://github.com/encode/uvicorn |
| Pydantic | MIT | https://github.com/pydantic/pydantic |
| FastMCP | Apache-2.0 | https://github.com/jlowin/fastmcp |
| Pint | BSD-3-Clause | https://github.com/hgrecco/pint |
| Babel | BSD-3-Clause | https://github.com/python-babel/babel |
| cryptography | Apache-2.0 OR BSD-3-Clause | https://github.com/pyca/cryptography |

These pull in transitive dependencies (Starlette, anyio, h11, httptools,
websockets, click, typing-extensions, annotated-types, pydantic-core, cffi and
others), all under MIT, BSD or Apache-2.0 terms. To produce the exact resolved
set for a given build:

```bash
docker run --rm --entrypoint pip ghcr.io/foundrynet/forge-sandbox freeze
```

## Development-only dependencies

Not present in the `runtime` stage of the image: pytest (MIT),
pytest-asyncio (Apache-2.0), httpx (BSD-3-Clause).

## Base image

`python:3.12-slim` — Debian-based. The Python interpreter is under the PSF
License Agreement; Debian system packages carry their own licenses, recorded in
`/usr/share/doc/*/copyright` inside the image. No additional system packages are
installed by this Dockerfile.

## Mapping packs and canonical schema

The mapping packs and the canonical field list are derived from the MIT-licensed
FoundryNet canonical schema (https://github.com/FoundryNet/canonical-schema).
`tools/build_packs.py` documents the provenance of every pack.

Packs in this image are built from **public protocol specifications and public
vendor documentation only** — SunSpec, Modbus register maps, MTConnect, Marlin
G-code, BACnet object names and similar. They contain no customer data and no
proprietary vendor material.

## Attribution for benchmark datasets

Where the sandbox ships example or benchmark data derived from public research
datasets, those datasets are used under CC BY 4.0 and are credited in the
directory that contains them. CC BY 4.0 requires attribution; it does not
require that this image be licensed under the same terms.

---

Questions about licensing or attribution, or a correction to this file:
foundrynet@proton.me

(This is a live, monitored inbox and is the address of record for licensing and
attribution requests. Do not replace it with an @foundrynet.io address: that
domain only sends — Resend DKIM on send.foundrynet.io — and bounces inbound.)
