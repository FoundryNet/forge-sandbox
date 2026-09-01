"""Satellite engine agent — heartbeats a licensed engine to the control plane.

This is the licensee-side half of the fleet protocol. It runs inside the engine
process, so it sees real usage rather than a synthetic counter, and it is built
around one assumption: THE CONTROL PLANE WILL BE UNREACHABLE SOMETIMES, and the
engine must keep normalizing anyway.

What it does:
  * heartbeats on the interval the control plane dictates (not a local constant)
  * meters usage — events since the last successful heartbeat, by kind
  * clusters unresolved tags by (tag, oem) and reports NAMES AND COUNTS ONLY.
    No telemetry values ever leave the licensee's network.
  * queues heartbeats to disk when the control plane is down and flushes them
    afterwards, so an outage costs no billable events and bills none twice
  * downloads corpus updates, verifies the Ed25519 signature BEFORE trusting a
    byte of them, and hot-reloads without restarting the process
  * rotates its bearer token when the control plane hands it a new one
  * keeps serving on the cached corpus when the license is invalid, while
    accepting no further corpus updates

Signature scheme (verified against the live control plane):
    canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    digest    = sha256(canonical).hexdigest()      # advisory, cross-checked
    signature = Ed25519(canonical_bytes)           # what actually gates trust
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from collections import Counter

log = logging.getLogger("forge.satellite")

_UA = "forge-satellite/1.0 (+https://foundrynet.io)"


def _env(name, default=None):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


# Confidence tiers the resolver actually emits. Anything else lands in
# "other", which is itself a signal: a new tier appeared without us noticing.
_CONF_TIERS = ((1.0, "1.0"), (0.85, "0.85"), (0.65, "0.65"))


def _conf_bucket(conf):
    try:
        c = float(conf)
    except (TypeError, ValueError):
        return "other"
    for value, label in _CONF_TIERS:
        if abs(c - value) < 1e-6:
            return label
    return "other"


class Counters:
    """Usage and QUALITY the engine accumulates between heartbeats.

    Snapshot-and-swap rather than read-then-clear: a heartbeat that FAILS must
    not lose its events, so the caller keeps the snapshot until the control
    plane acknowledges it.

    Everything recorded here is a NAME, a COUNT or a SCORE. No telemetry value
    ever enters these counters, so the quality signal can leave the licensee's
    network on the same terms the usage signal already does.
    """

    # Bounded so a pathological hour cannot grow the heartbeat without limit.
    MAX_REASONS = 25
    MAX_KEYS = 50

    def __init__(self):
        self._lock = threading.Lock()
        self._reset_locked()

    def _reset_locked(self):
        self.events = 0
        self.by_kind = Counter()
        self.unresolved = Counter()          # (tag, oem) -> count
        self.machines = set()
        self.errors = []
        # ── quality ────────────────────────────────────────────────────────
        self.evidence_refusals = 0
        self.refusal_reasons = []            # [{tag, candidate, score}]
        self.relief_valve_fires = 0
        self.relief_valve_fields = Counter()
        self.sentinel_catches = 0
        self.sentinels_by_token = Counter()
        self.physics_violations = 0
        self.physics_fields = Counter()
        self.coercions = Counter()           # kind -> count
        self.confidence = Counter()          # bucket -> count
        self.fields_seen = 0
        self.coverage_mapped = Counter()     # oem -> mapped fields
        self.coverage_total = Counter()      # oem -> total fields
        self.oems_seen = set()
        self.signature_failures = 0
        # Structurally zero: no override path exists in the resolver. The wire
        # is here so that if one is ever added, the control plane sees it the
        # same hour rather than the next audit.
        self.evidence_gate_overrides = 0

    # ── recording ──────────────────────────────────────────────────────────
    def record_normalize(self, *, unresolved_tags=(), oem=None, machine_id=None,
                         kind="normalize", field_mappings=None, null_states=None,
                         coercions=(), relief_valve_fires=0, fields_total=0,
                         fields_mapped=0):
        """Meter one normalize. Every argument past `kind` is optional so an
        older caller keeps working and simply reports no quality signal."""
        with self._lock:
            self.events += 1
            self.by_kind[kind] += 1
            if machine_id:
                self.machines.add(str(machine_id))
            for t in unresolved_tags:
                self.unresolved[(str(t), str(oem or "unknown"))] += 1

            oem_key = str(oem or "unknown")
            self.oems_seen.add(oem_key)
            self.fields_seen += int(fields_total or 0)
            self.coverage_mapped[oem_key] += int(fields_mapped or 0)
            self.coverage_total[oem_key] += int(fields_total or 0)

            if relief_valve_fires:
                self.relief_valve_fires += int(relief_valve_fires)

            for tag, rec in (field_mappings or {}).items():
                if not isinstance(rec, dict):
                    continue
                match = rec.get("match_type")
                if match == "insufficient_evidence":
                    self.confidence["refused"] += 1
                    self.evidence_refusals += 1
                    if len(self.refusal_reasons) < self.MAX_REASONS:
                        ev = rec.get("evidence") or {}
                        self.refusal_reasons.append({
                            "tag": str(tag)[:80],
                            "candidate": (str(rec.get("refused_candidate"))[:80]
                                          if rec.get("refused_candidate") else None),
                            "score": ev.get("score"),
                            "class": ev.get("class"),
                        })
                elif match in (None, "unknown", "quantity_mismatch", "ambiguous_fold"):
                    self.confidence["unresolved"] += 1
                else:
                    self.confidence[_conf_bucket(rec.get("confidence"))] += 1

            # Sentinels and physics violations are read off the null reasons the
            # validator already writes, so the two can never disagree.
            for key, state in (null_states or {}).items():
                reason = str((state or {}).get("null_reason") or "")
                head = reason.split(":", 1)[0].strip()
                if head in ("numeric_sentinel", "string_sentinel"):
                    self.sentinel_catches += 1
                    token = reason.split(":", 1)[1].strip() if ":" in reason else ""
                    self.sentinels_by_token[token.split("(")[0].strip()[:40] or head] += 1
                elif head == "physics_violation":
                    self.physics_violations += 1
                    self.physics_fields[str(key)[:80]] += 1

            for c in coercions or ():
                if isinstance(c, dict):
                    label = c.get("coercion") or c.get("applied") or c.get("kind")
                    if label:
                        # `extracted_unit:degF` and `extracted_unit:psi` are the
                        # same coercion. Group on the kind, not its argument, or
                        # the distribution is a list of one-offs.
                        self.coercions[str(label).split(":", 1)[0][:40]] += 1

    def record_signature_failure(self):
        """A corpus bundle failed Ed25519 verification and was NOT applied."""
        with self._lock:
            self.signature_failures += 1

    def record_error(self, msg):
        with self._lock:
            if len(self.errors) < 25:
                self.errors.append(str(msg)[:300])

    # ── transport ──────────────────────────────────────────────────────────
    def quality_snapshot(self):
        """The quality half of the payload. Split out so it is testable
        without driving a whole heartbeat."""
        top = lambda ctr: dict(ctr.most_common(self.MAX_KEYS))
        # Raw mapped/total, not just a percentage: averaging percentages
        # across engines is arithmetically wrong, and the control plane needs a
        # fleet number it can defend.
        coverage_by_oem = {
            oem: {"mapped": self.coverage_mapped[oem],
                  "total": self.coverage_total[oem],
                  "pct": round(100.0 * self.coverage_mapped[oem] / self.coverage_total[oem], 2)}
            for oem in self.coverage_total if self.coverage_total[oem] > 0
        }
        mapped, total = sum(self.coverage_mapped.values()), sum(self.coverage_total.values())
        return {
            "evidence_refusals": self.evidence_refusals,
            "evidence_refusal_reasons": list(self.refusal_reasons),
            "evidence_gate_overrides": self.evidence_gate_overrides,
            "relief_valve_fires": self.relief_valve_fires,
            "relief_valve_fields": top(self.relief_valve_fields),
            "sentinel_catches": self.sentinel_catches,
            "sentinels_by_token": top(self.sentinels_by_token),
            "physics_violations": self.physics_violations,
            "physics_violation_fields": top(self.physics_fields),
            "coercions_applied": top(self.coercions),
            "confidence_distribution": top(self.confidence),
            "coverage_by_oem": coverage_by_oem,
            "coverage_pct": round(100.0 * mapped / total, 2) if total else None,
            "fields_seen": self.fields_seen,
            "fields_mapped": mapped,
            "oems_seen": sorted(self.oems_seen)[:self.MAX_KEYS],
            "signature_failures": self.signature_failures,
        }

    def snapshot(self):
        with self._lock:
            snap = {
                "events_since_last": self.events,
                "events_by_kind": dict(self.by_kind),
                "machines_active": len(self.machines),
                "unresolved_tags": [
                    {"tag": t, "oem": o, "count": c}
                    for (t, o), c in self.unresolved.most_common(200)
                ],
                "errors": list(self.errors),
            }
            snap.update(self.quality_snapshot())
            self._reset_locked()
            return snap

    def restore(self, snap):
        """Give a failed heartbeat's events back, so nothing is billed away —
        and nothing is silently un-reported either. A quality signal dropped on
        a failed beat is a blind spot exactly when the fleet is unhealthy."""
        with self._lock:
            self.events += snap.get("events_since_last", 0)
            for k, v in (snap.get("events_by_kind") or {}).items():
                self.by_kind[k] += v
            for row in (snap.get("unresolved_tags") or []):
                self.unresolved[(row["tag"], row["oem"])] += row.get("count", 0)

            self.evidence_refusals += snap.get("evidence_refusals", 0)
            for r in (snap.get("evidence_refusal_reasons") or []):
                if len(self.refusal_reasons) < self.MAX_REASONS:
                    self.refusal_reasons.append(r)
            self.evidence_gate_overrides += snap.get("evidence_gate_overrides", 0)
            self.relief_valve_fires += snap.get("relief_valve_fires", 0)
            self.sentinel_catches += snap.get("sentinel_catches", 0)
            self.physics_violations += snap.get("physics_violations", 0)
            self.signature_failures += snap.get("signature_failures", 0)
            self.fields_seen += snap.get("fields_seen", 0)
            for name, ctr in (("relief_valve_fields", self.relief_valve_fields),
                              ("sentinels_by_token", self.sentinels_by_token),
                              ("physics_violation_fields", self.physics_fields),
                              ("coercions_applied", self.coercions),
                              ("confidence_distribution", self.confidence)):
                for k, v in (snap.get(name) or {}).items():
                    ctr[k] += v
            self.oems_seen.update(snap.get("oems_seen") or ())
            for oem, d in (snap.get("coverage_by_oem") or {}).items():
                if isinstance(d, dict):
                    self.coverage_mapped[oem] += d.get("mapped", 0)
                    self.coverage_total[oem] += d.get("total", 0)


COUNTERS = Counters()


class SignatureError(Exception):
    """A corpus bundle did not verify. Never applied."""


def verify_bundle(bundle, public_key_hex):
    """Verify a signed corpus bundle. Returns the payload, or raises.

    Ed25519 over the canonical payload bytes is what gates trust; the sha256 is
    cross-checked as well so a mismatch is reported precisely rather than as a
    generic signature failure.
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if not isinstance(bundle, dict) or "payload" not in bundle:
        raise SignatureError("bundle has no payload")
    sig = bundle.get("signature") or {}
    if (sig.get("algo") or "").lower() != "ed25519":
        raise SignatureError(f"unsupported signature algo {sig.get('algo')!r}")
    for f in ("signature", "digest_sha256"):
        if not sig.get(f):
            raise SignatureError(f"signature block missing {f}")

    payload = bundle["payload"]
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()

    digest = hashlib.sha256(canonical).hexdigest()
    if digest != sig["digest_sha256"]:
        raise SignatureError(
            f"digest mismatch: computed {digest[:16]}..., bundle claims "
            f"{sig['digest_sha256'][:16]}... — content was altered in transit")
    try:
        Ed25519PublicKey.from_public_bytes(
            bytes.fromhex(public_key_hex)
        ).verify(bytes.fromhex(sig["signature"]), canonical)
    except InvalidSignature:
        raise SignatureError(
            "Ed25519 signature does not verify against the control plane's "
            "public key — the bundle is not authentic")
    except ValueError as exc:
        raise SignatureError(f"malformed signature material: {exc}")
    return payload


class Satellite:
    def __init__(self, *, engine_id=None, license_id=None, token=None,
                 control_plane=None, interval_s=None, corpus_version=None,
                 state_dir=None, engine_version="1.0.0"):
        self.engine_id = engine_id or _env("ENGINE_ID")
        self.license_id = license_id or _env("LICENSE_ID")
        self.token = token or _env("HEARTBEAT_TOKEN")
        self.control_plane = (control_plane or _env("CONTROL_PLANE_URL") or "").rstrip("/")
        self.interval_s = int(interval_s or _env("HEARTBEAT_INTERVAL_S", 60))
        self.corpus_version = corpus_version or _env("CORPUS_VERSION", "1.47")
        self.engine_version = engine_version
        self.state_dir = state_dir or _env("SATELLITE_STATE_DIR", "/tmp/forge-satellite")
        os.makedirs(self.state_dir, exist_ok=True)
        self.queue_path = os.path.join(self.state_dir, "heartbeat_queue.jsonl")
        self.corpus_path = os.path.join(self.state_dir, "corpus.json")

        self._pubkey = None
        self._stop = threading.Event()
        self._thread = None
        self.license_valid = True
        self.last_ack = None
        self.last_rtt_ms = None
        self.rejected_deltas = 0
        self.applied_versions = []
        self.rotations = 0

    # ── plumbing ────────────────────────────────────────────────────────────
    def enabled(self):
        return bool(self.engine_id and self.token and self.control_plane)

    def _req(self, path, *, method="GET", body=None, timeout=25):
        url = path if path.startswith("http") else f"{self.control_plane}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "User-Agent": _UA,
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")

    def public_key(self):
        if self._pubkey:
            return self._pubkey
        _, doc = self._req("/v1/pubkey")
        self._pubkey = doc["public_key"]
        return self._pubkey

    # ── disk queue ──────────────────────────────────────────────────────────
    def _queue(self, payload):
        payload = dict(payload, queued=True)
        with open(self.queue_path, "a") as fh:
            fh.write(json.dumps(payload) + "\n")

    def _queued(self):
        if not os.path.exists(self.queue_path):
            return []
        with open(self.queue_path) as fh:
            return [json.loads(l) for l in fh if l.strip()]

    def _rewrite_queue(self, rows):
        tmp = self.queue_path + ".tmp"
        with open(tmp, "w") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
        os.replace(tmp, self.queue_path)

    def flush_queue(self):
        """Send every queued heartbeat. Each one keeps its own event count, so
        an outage delays billing rather than losing or duplicating it."""
        rows = self._queued()
        if not rows:
            return 0
        sent, remaining = 0, []
        for row in rows:
            try:
                status, _ = self._req("/v1/heartbeat", method="POST", body=row)
                if status == 200:
                    sent += 1
                else:
                    remaining.append(row)
            except Exception:
                remaining.append(row)
        self._rewrite_queue(remaining)
        return sent

    # ── corpus ──────────────────────────────────────────────────────────────
    def fetch_and_apply(self, url, *, expect_version=None):
        """Download, verify, then apply. Verification happens before anything
        touches the running corpus, and a partial or tampered download is
        discarded rather than half-applied."""
        try:
            status, bundle = self._req(url)
        except Exception as exc:
            log.warning("corpus download failed: %s", exc)
            return None, f"download_failed: {exc}"
        try:
            payload = verify_bundle(bundle, self.public_key())
        except SignatureError as exc:
            self.rejected_deltas += 1
            log.error("REFUSED corpus bundle: %s", exc)
            COUNTERS.record_error(f"delta_rejected: {exc}")
            # Counted as well as logged: a rejected delta is a CRITICAL fleet
            # alert, and a log line on the licensee's box is not something we
            # can see.
            COUNTERS.record_signature_failure()
            return None, f"delta_rejected: {exc}"

        # Two bundle shapes: a FULL corpus {version, mappings[]} and a DELTA
        # {from_version, to_version, added[], updated[], removed[]}. The version
        # a delta declares lives under a different key, and reading only
        # `version` rejected every legitimate delta as a version mismatch.
        is_delta = "to_version" in payload or "added" in payload
        got_version = payload.get("to_version") if is_delta else payload.get("version")
        if expect_version and got_version != expect_version:
            self.rejected_deltas += 1
            return None, (f"version_mismatch: asked for {expect_version}, "
                          f"bundle carries {got_version}")
        # Atomic: write beside, then rename. A crash mid-write cannot leave a
        # truncated corpus behind.
        tmp = self.corpus_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(payload, fh)
        os.replace(tmp, self.corpus_path)
        self.corpus_version = got_version or self.corpus_version
        self.applied_versions.append(self.corpus_version)
        applied = self._hot_reload(payload, is_delta=is_delta)
        log.info("corpus %s applied (%d mappings), no restart",
                 self.corpus_version, applied)
        return self.corpus_version, None

    def _hot_reload(self, payload, *, is_delta=False):
        from app import corpus as _corpus
        if is_delta:
            return _corpus.apply_fleet_delta(payload)
        return _corpus.apply_fleet_overlay(payload)

    # ── heartbeat ───────────────────────────────────────────────────────────
    def build(self, snap):
        return {
            "engine_id": self.engine_id,
            "license_id": self.license_id,
            "engine_version": self.engine_version,
            "corpus_version": self.corpus_version,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            **snap,
        }

    def beat(self, *, timeout=25):
        """One heartbeat. Returns (ok, response_or_error).

        `timeout` is shortened on the shutdown drain so a slow control plane
        cannot hold a container terminating.
        """
        snap = COUNTERS.snapshot()
        payload = self.build(snap)
        t0 = time.time()
        try:
            status, resp = self._req("/v1/heartbeat", method="POST", body=payload,
                                     timeout=timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode()[:300]
            if exc.code in (401, 403):
                # Auth or licence problem: the events are real and still owed,
                # so hand them back rather than dropping them on the floor.
                COUNTERS.restore(snap)
                if exc.code == 403:
                    self.license_valid = False
                return False, {"status": exc.code, "detail": detail}
            COUNTERS.restore(snap)
            self._queue(payload)
            return False, {"status": exc.code, "detail": detail}
        except Exception as exc:
            # Control plane unreachable. Queue to disk; keep normalizing.
            self._queue(payload)
            return False, {"status": None, "detail": str(exc)}

        self.last_rtt_ms = round((time.time() - t0) * 1000, 1)
        self.last_ack = resp
        self.license_valid = bool(resp.get("license_valid", True))

        if resp.get("heartbeat_interval_s"):
            self.interval_s = int(resp["heartbeat_interval_s"])

        rot = resp.get("token_rotation")
        if rot:
            # The control plane names this `new_token`; accept the obvious
            # synonyms too rather than silently keeping the old credential,
            # which is how a rotation looks like it worked right up until the
            # grace window closes and every heartbeat starts 401ing.
            new = rot if isinstance(rot, str) else (
                rot.get("new_token") or rot.get("token") or rot.get("bearer_token"))
            if isinstance(new, str) and new and new != self.token:
                self.token = new
                self.rotations += 1
                log.info("bearer token rotated (grace until %s)",
                         rot.get("old_token_valid_until") if isinstance(rot, dict) else "?")

        # A suspended licence keeps the engine running on its cached corpus and
        # accepting no updates. Refusing to normalize would punish the plant
        # floor for a billing dispute.
        if self.license_valid and resp.get("corpus_update_available"):
            url = self._choose_corpus_url(resp)
            if url:
                self.fetch_and_apply(url, expect_version=resp.get("corpus_version"))
        elif resp.get("rollback") and self.license_valid:
            url = resp.get("full_url") or resp.get("delta_url")
            if url:
                self.fetch_and_apply(url, expect_version=resp.get("corpus_version"))

        self.flush_queue()
        return True, resp

    def _choose_corpus_url(self, resp):
        """Delta when we hold a baseline to apply it to, full otherwise.

        Applying a 1.47->1.48 delta to an engine with an EMPTY overlay would
        leave it holding only the rows that changed -- one mapping instead of
        forty-eight.
        """
        from app import corpus as _corpus
        if _corpus.fleet_overlay_size() == 0:
            return resp.get("full_url") or resp.get("delta_url")
        return resp.get("delta_url") or resp.get("full_url")

    # ── daemon ──────────────────────────────────────────────────────────────
    def _loop(self):
        while not self._stop.is_set():
            try:
                self.beat()
            except Exception as exc:                     # never kill the engine
                log.exception("heartbeat loop error: %s", exc)
            self._stop.wait(self.interval_s)

    def load_cached_corpus(self):
        """Reinstate the last verified corpus from disk at boot.

        Without this a restarted engine reports the corpus version its env says
        while holding an EMPTY overlay -- and because it reports the right
        version, the control plane sees no update to send, so it silently serves
        with none of the fleet mappings it claims to have. The cache on disk was
        already signature-verified before it was written, which is why it can be
        trusted here without a network round trip.
        """
        if not os.path.exists(self.corpus_path):
            return 0
        try:
            with open(self.corpus_path) as fh:
                payload = json.load(fh)
        except Exception as exc:
            log.warning("cached corpus unreadable, ignoring: %s", exc)
            return 0
        from app import corpus as _corpus
        if "mappings" in payload:
            n = _corpus.apply_fleet_overlay(payload)
        else:
            n = _corpus.apply_fleet_delta(payload)
        self.corpus_version = (payload.get("version")
                               or payload.get("to_version") or self.corpus_version)
        log.info("restored cached corpus v%s (%d mappings) from disk",
                 self.corpus_version, n)
        return n

    def start(self):
        if not self.enabled():
            log.info("satellite disabled (ENGINE_ID / HEARTBEAT_TOKEN / "
                     "CONTROL_PLANE_URL not all set)")
            return False
        self.load_cached_corpus()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="forge-satellite")
        self._thread.start()
        log.info("satellite %s heartbeating to %s every %ss",
                 self.engine_id, self.control_plane, self.interval_s)
        return True

    def stop(self, *, drain=True, timeout=5):
        """Stop the daemon, draining whatever has not been billed yet.

        Counters live in memory between heartbeats, so every event since the
        last successful beat dies with the process unless it is drained here.
        A restart is routine -- a rolling fleet upgrade restarts every engine --
        so dropping that window silently under-bills the licensee on every
        deploy, by up to one heartbeat interval of traffic per engine.

        One last beat on a short timeout. If the control plane cannot be
        reached, beat() queues the payload to disk exactly as it does during an
        outage, and the next boot flushes it -- so the events survive the
        restart either way.
        """
        self._stop.set()
        if not drain or not self.enabled() or COUNTERS.events <= 0:
            return
        try:
            self.beat(timeout=timeout)
        except Exception as exc:                         # shutdown must not raise
            log.warning("final heartbeat on shutdown failed: %s", exc)


AGENT = Satellite()
