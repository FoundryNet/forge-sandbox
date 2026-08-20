# Forge sandbox — fake data, real schema.
#
# Deliberately small and dependency-light. Production Forge ships a sentence
# embedding model and will not serve traffic until those weights load; this
# image has no model, so it starts in about a second and the whole download is
# a fraction of the size. That difference is the point: a developer trying an
# integration should not wait on a 2GB pull.

FROM python:3.12-slim AS base

# Predictable Python: no .pyc litter, unbuffered logs so `docker logs` is live.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Requirements first: this layer is cached across source edits, so iterating on
# the sandbox does not re-resolve the dependency tree every build.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# ── test stage ───────────────────────────────────────────────────────────────
# docker build --target test -t forge-sandbox:test .
# Runs the suite inside the image that ships, so a dependency that resolves on
# a laptop but not in the container fails the build rather than the user.
FROM base AS test
COPY requirements-dev.txt pytest.ini ./
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY tests/ ./tests/
RUN python -m pytest tests/ -q

# ── runtime ──────────────────────────────────────────────────────────────────
FROM base AS runtime

# Run unprivileged. The sandbox writes nothing, so it needs no writable path.
RUN useradd --create-home --shell /usr/sbin/nologin forge \
    && chown -R forge:forge /app
USER forge

EXPOSE 8000

# 0.0.0.0 is correct here and is not the fail-open it would be in production:
# there is no data, no credential, and no upstream behind this process. It has
# to bind the wildcard or the published port reaches nothing.
ENV FORGE_SANDBOX_HOST=0.0.0.0 \
    FORGE_SANDBOX_PORT=8000

# One worker. The sandbox holds its packs in process memory and serves one
# developer; more workers would just duplicate the corpus.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"

CMD ["sh", "-c", "exec uvicorn app.main:app --host ${FORGE_SANDBOX_HOST} --port ${FORGE_SANDBOX_PORT} --no-server-header"]
