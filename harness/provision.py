"""Provisioner — Onedata space lifecycle (create / support / teardown).

Complements `fixtures.FixtureManager`, which assumes a space already exists
and is already supported on the providers it targets — this module is what
creates that space (and its per-provider supports) around FixtureManager, and
tears it down again, per the sweep's "fresh space per sweep + teardown"
requirement.

ONE onezone-admin token (`fixtures.mint_token_local()`) authenticates BOTH
onezone AND every provider's onepanel via the `X-Auth-Token` header — no
per-provider passphrase.

Stdlib-only (urllib/ssl/json/time) — same conventions as fixtures.py /
verifier.py: closed research federation with self-signed certs → unverified
SSL context (`SSL_CTX`, reused from fixtures.py). No top-level side effects
on import.
"""
from __future__ import annotations

import dataclasses
import json
import time
import urllib.error
import urllib.request
import uuid
from typing import Callable, Dict, List, Optional, Sequence, Tuple

try:
    from .fixtures import ONEZONE_URL, SSL_CTX, mint_token_local
    from . import settings
except ImportError:
    from fixtures import ONEZONE_URL, SSL_CTX, mint_token_local
    import settings

try:
    from .verifier import Verifier
except ImportError:
    from verifier import Verifier


@dataclasses.dataclass(frozen=True)
class ProviderSupport:
    """One provider's onepanel connection + storage backend for space support."""
    label: str
    onepanel_host: str
    storage_id: str


DEFAULT_PROVIDERS: Tuple[ProviderSupport, ...] = tuple(
    ProviderSupport(
        label=settings.get(f"{site}.label"),
        onepanel_host=settings.get(f"{site}.host"),
        storage_id=settings.get(f"{site}.storage_id"),
    )
    for site in ("source", "target")
)


# --- fail-fast precheck constants: waiting/latency is deliberately NOT baked
# into the test suite — the harness is engineered to FAIL if a readiness or
# health problem occurs, rather than wait it out (see the module-section
# comment above `wait_for_space_ready` + `assert_transfer_path_healthy`
# below). Every bounded-wait knob in this module lives here so it's auditable
# in one place. ---

# Inter-provider SUPPORT propagation is seconds-scale eventual consistency —
# a long tolerance window would itself be the kind of arbitrary wait the
# fail-fast design rules out. 45s remains generous relative to the observed
# propagation latency while still failing loud well short of spending a live
# trial against a space whose support hasn't converged.
SPACE_READY_TIMEOUT_S = 45.0

# The fail-fast transfer-health canary (assert_transfer_path_healthy, below).
# CANARY_TIMEOUT_S is deliberately SHORT and bounded — this is the
# fail-fast mechanism for a class of failure where a fully-supported space
# (wait_for_space_ready says ready) can still have a stalled DATA path
# (e.g. an rtransfer fetch rejected as unauthorized, or a transfer stuck
# `scheduled` with `startTime==0` indefinitely). The canary NEVER waits
# longer than CANARY_TIMEOUT_S — the remedy for a stalled path is a manual,
# out-of-band mesh restart of the participating providers; this harness
# never waits on it.
CANARY_CONTENT_SIZE = 4096  # the calibrated-range floor
CANARY_TIMEOUT_S = 60.0
CANARY_POLL_INTERVAL_S = 3.0
# Settle-retry budget for the transient `400 spaceNotSupportedBy` some
# providers surface for a few seconds immediately after wait_for_space_ready
# reports ready (a distinct, narrower eventual-consistency gap than the
# support-map propagation wait_for_space_ready itself closes) — NOT a
# general retry-on-any-failure policy; see assert_transfer_path_healthy's
# docstring for the exact transient signature this covers.
CANARY_SCHEDULE_MAX_RETRIES = 3
CANARY_SCHEDULE_RETRY_BACKOFF_S = 2.0

# Outer bounded retry over the WHOLE canary attempt. The transfer path has an
# intermittent "completed-with-0-bytes" transient failure — the transfer
# reaches a terminal status but 0 bytes land, then a FRESH attempt (new
# canary file) converges normally. Without this retry, a single such
# transient failure would abort the head/tail canary gate in a sweep driver
# (0 cells run), even though the transfer path itself is not actually
# stalled. This is the SAME transient-vs-persistent discriminator already
# used for `resolve_root` ENOENT-settle retries and the
# `spaceNotSupportedBy` schedule-retry above (and the netem qdisc-del clear
# retry) — bounded retry absorbs a transient failure; a PERSISTENT stall
# (every attempt fails) still fails loud, preserving fail-fast. Each attempt
# gets its OWN full `timeout_s` window, so the common healthy case (attempt 1
# converges) returns fast — only a genuine persistent stall pays the full
# N x timeout_s cost.
CANARY_MAX_ATTEMPTS = 3
CANARY_RETRY_BACKOFF_S = 2.0


class ProvisionError(RuntimeError):
    """Raised on any provisioning REST failure. Fails loud — never silently swallowed."""


class Provisioner:
    """Owns ONE onezone-admin token + REST client for space create/support/teardown.

    `http` is the injectable testability seam (mirrors netem.py's injected
    `runner` + fixtures'/verifier's `_http` method): a callable
    `(method, url, body=None, content_type=None) -> (status: int, headers: dict, parsed_body)`.
    Defaults to a real urllib-based client using the unverified `SSL_CTX`.
    """

    def __init__(self, token: Optional[str] = None, http: Optional[Callable] = None):
        self._token = token if token is not None else mint_token_local()
        self._http: Callable = http if http is not None else self._real_http

    # --- real HTTP client (urllib + unverified SSL, matches fixtures.py/verifier.py) ---

    def _real_http(self, method: str, url: str, body: Optional[bytes] = None,
                    content_type: Optional[str] = None, timeout: int = 60
                    ) -> Tuple[int, dict, object]:
        req = urllib.request.Request(url, method=method)
        req.add_header("X-Auth-Token", self._token)
        if content_type:
            req.add_header("content-type", content_type)
        try:
            with urllib.request.urlopen(req, data=body, timeout=timeout, context=SSL_CTX) as resp:
                status = resp.getcode()
                headers = dict(resp.headers.items())
                raw = resp.read()
        except urllib.error.HTTPError as e:
            status = e.code
            headers = dict(e.headers.items()) if e.headers is not None else {}
            raw = e.read()
        try:
            parsed = json.loads(raw) if raw else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            parsed = raw
        return status, headers, parsed

    # --- public API ---

    def create_space(self, name: str) -> str:
        """POST /onezone/user/spaces; parse space_id from the Location header.

        Raises ProvisionError on non-201 status, or when the Location header
        is missing / doesn't contain a parseable `/spaces/<space_id>` suffix.
        """
        payload = json.dumps({"name": name}).encode()
        status, headers, body = self._http(
            "POST", f"{ONEZONE_URL}/api/v3/onezone/user/spaces",
            body=payload, content_type="application/json",
        )
        if status != 201:
            raise ProvisionError(f"create_space failed status={status} body={str(body)[:200]}")
        location = headers.get("Location") or headers.get("location")
        if not location or "/spaces/" not in location:
            raise ProvisionError(f"create_space: no parseable Location header (got {location!r})")
        space_id = location.rstrip("/").rsplit("/spaces/", 1)[-1]
        if not space_id:
            raise ProvisionError(f"create_space: could not extract space_id from Location {location!r}")
        return space_id

    def _mint_support_invite_token(self, space_id: str) -> str:
        """Mint a supportSpace invite token (1h TTL) for `space_id`."""
        valid_until = int(time.time()) + 3600
        payload = json.dumps({
            "type": {"inviteToken": {"inviteType": "supportSpace", "spaceId": space_id}},
            "caveats": [{"type": "time", "validUntil": valid_until}],
        }).encode()
        status, _headers, body = self._http(
            "POST", f"{ONEZONE_URL}/api/v3/onezone/user/tokens/temporary",
            body=payload, content_type="application/json",
        )
        # The temporary-token endpoint returns 201 Created (token in body) —
        # accept both 200 and 201 (the actual status observed live is 201).
        if status not in (200, 201) or not isinstance(body, dict) or "token" not in body:
            raise ProvisionError(
                f"support invite-token mint failed status={status} body={str(body)[:200]}"
            )
        return body["token"]

    def support_space(self, space_id: str, provider: ProviderSupport, size_bytes: int) -> None:
        """Mint an invite token, then POST /onepanel/provider/spaces on `provider`'s onepanel."""
        invite_token = self._mint_support_invite_token(space_id)
        payload = json.dumps({
            "token": invite_token,
            "size": size_bytes,
            "storageId": provider.storage_id,
        }).encode()
        status, _headers, body = self._http(
            "POST", f"{provider.onepanel_host}/api/v3/onepanel/provider/spaces",
            body=payload, content_type="application/json",
        )
        if status not in (200, 201):
            raise ProvisionError(
                f"support_space({provider.label!r}) failed status={status} body={str(body)[:200]}"
            )

    def teardown_space(self, space_id: str, providers: Sequence[ProviderSupport]) -> Dict[str, bool]:
        """Best-effort teardown: revoke each provider's support, then delete the space.

        Each step is attempted independently — a failing DELETE (raised
        exception OR non-2xx status) on one provider does not stop the
        others or the final space delete. Returns
        `{<provider.label>: ok_bool, ..., "space_deleted": ok_bool}`.
        """
        result: Dict[str, bool] = {}
        for provider in providers:
            try:
                status, _headers, _body = self._http(
                    "DELETE",
                    f"{provider.onepanel_host}/api/v3/onepanel/provider/spaces/{space_id}",
                )
                result[provider.label] = status in (200, 202, 204)
            except Exception:
                result[provider.label] = False
        try:
            status, _headers, _body = self._http(
                "DELETE", f"{ONEZONE_URL}/api/v3/onezone/spaces/{space_id}",
            )
            result["space_deleted"] = status in (200, 202, 204)
        except Exception:
            result["space_deleted"] = False
        return result

    def provision(self, name: str, providers: Sequence[ProviderSupport] = DEFAULT_PROVIDERS,
                  size_bytes: int = 1073741824) -> dict:
        """create_space → support_space per provider → {"space_id", "name", "supported"}.

        Teardown-on-failure guard: if ANY support_space call raises, tear
        down whatever succeeded so far (revoke the supports already granted
        + delete the space) before re-raising — never orphan a half-space.
        """
        space_id = self.create_space(name)
        supported: List[ProviderSupport] = []
        try:
            for provider in providers:
                self.support_space(space_id, provider, size_bytes)
                supported.append(provider)
        except Exception:
            self.teardown_space(space_id, supported)
            raise
        return {"space_id": space_id, "name": name, "supported": [p.label for p in supported]}

    # --- multi-perspective space-readiness precheck (eventual-consistency ---
    # hardening: `provision()` above can return 201/200 at every API
    # boundary (create_space, each support_space) while inter-provider
    # support/auth hasn't actually finished propagating — API-boundary
    # success does not by itself prove the support state has converged. This
    # section NEVER reads `GET /onezone/spaces/{id}.supportingProviders` —
    # that field is ALWAYS null, on manually-created spaces AND provisioned
    # ones alike, so it is a trap, not a signal. ---

    def _check_user_spaces_providers(self, space_id: str, required: Sequence[str]) -> Dict:
        """Perspective 1 — the CORRECT support map:
        `GET /onezone/user/spaces/{space_id}` -> `.providers` dict, keyed by
        provider ENTITY ID -> support size in bytes. NEVER reads
        `.supportingProviders` (a DIFFERENT field on the DIFFERENT
        admin-scoped `GET /onezone/spaces/{id}` endpoint — always null).
        """
        try:
            status, _headers, body = self._http(
                "GET", f"{ONEZONE_URL}/api/v3/onezone/user/spaces/{space_id}"
            )
        except Exception as e:  # noqa: BLE001 -- transient fault; caller retries next poll tick
            return {
                "ok": False, "providers": None, "missing": list(required),
                "error": f"{type(e).__name__}: {e}",
            }
        if status != 200 or not isinstance(body, dict):
            return {
                "ok": False, "providers": None, "missing": list(required),
                "error": f"status={status} body={str(body)[:200]}",
            }
        providers = body.get("providers")
        if not isinstance(providers, dict):
            return {
                "ok": False, "providers": None, "missing": list(required),
                "error": "response missing/malformed '.providers' field",
            }
        missing = [p for p in required if p not in providers]
        return {"ok": not missing, "providers": providers, "missing": missing}

    def _check_spaces_providers_list(self, space_id: str, required: Sequence[str]) -> Dict:
        """Perspective 2 (cross-check) — `GET /onezone/spaces/{space_id}/providers`
        -> list of provider entity IDs supporting the space. This LIST
        endpoint correctly returns the provider IDs — it is a DIFFERENT
        endpoint from the admin `GET /onezone/spaces/{id}` (whose
        `.supportingProviders` FIELD is the trap); this method never touches
        that field or that response.

        Response-shape note: the exact wire shape isn't guaranteed, so this
        defensively accepts either a bare JSON list OR a dict with a
        `"providers"` list field, mirroring the existing dual-shape
        tolerance already used for `GET .../user/spaces` in
        `scored_sweep_driver.py`/`a4_sweep_driver.py`'s
        `_best_effort_orphan_check`.
        """
        try:
            status, _headers, body = self._http(
                "GET", f"{ONEZONE_URL}/api/v3/onezone/spaces/{space_id}/providers"
            )
        except Exception as e:  # noqa: BLE001 -- transient fault; caller retries next poll tick
            return {
                "ok": False, "providers": None, "missing": list(required),
                "error": f"{type(e).__name__}: {e}",
            }
        if status != 200:
            return {
                "ok": False, "providers": None, "missing": list(required),
                "error": f"status={status} body={str(body)[:200]}",
            }
        if isinstance(body, list):
            provider_ids = body
        elif isinstance(body, dict) and isinstance(body.get("providers"), list):
            provider_ids = body["providers"]
        else:
            return {
                "ok": False, "providers": None, "missing": list(required),
                "error": f"unrecognized response shape {type(body).__name__}",
            }
        missing = [p for p in required if p not in provider_ids]
        return {"ok": not missing, "providers": provider_ids, "missing": missing}

    def _check_providers_online(self, required: Sequence[str], verifier: object) -> Dict:
        """Perspective 3 (provider-side liveness) — per required provider,
        `verifier.get_provider_online(provider_id)` (onezone's `.online`
        view — see `verifier.Verifier.get_provider_online`'s own
        docstring: it is intent-agnostic per-provider liveness, hit against
        ONEZONE, not the provider's own oneprovider host). A provider that
        isn't exactly `True` (reports `False`, or `None` on a dead probe)
        blocks readiness — an offline/unreachable provider cannot have a
        converged inter-provider support/auth state regardless of what the
        space's support map says.
        """
        online: Dict[str, Optional[bool]] = {}
        for pid in required:
            try:
                online[pid] = verifier.get_provider_online(pid)
            except Exception:  # noqa: BLE001 -- transient fault; caller retries next poll tick
                online[pid] = None
        missing = [pid for pid, is_online in online.items() if is_online is not True]
        return {"ok": not missing, "online": online, "missing": missing}

    def wait_for_space_ready(
        self,
        space_id: str,
        required_providers: Sequence[str],
        *,
        verifier: Optional[object] = None,
        timeout_s: float = SPACE_READY_TIMEOUT_S,
        poll_interval_s: float = 5.0,
        sleep_fn: Callable[[float], None] = time.sleep,
        now_fn: Callable[[], float] = time.time,
    ) -> Dict:
        """Bounded wait asserting a space's inter-provider support state has
        actually converged, from MULTIPLE independent perspectives — the
        eventual-consistency-safe replacement for trusting `provision()`'s
        201/200 API-boundary success alone (see the module-section comment
        above for why API-boundary success alone isn't sufficient).

        `required_providers` MUST be a sequence of onezone PROVIDER ENTITY
        IDs — the SAME namespace as `harness/verifier.py`'s
        `state_timeline.samples[].provider` / `sweep.FaultSpec.
        target_provider` (e.g. `"7cfe30bef82a2c904c15624f752c58ddchd932"`
        for `de`) — NOT the human-readable labels used by
        `ProviderSupport.label` (`"cloud-pl"` / `"de"`) or by
        `netem.ProviderTarget`'s own test convention. This module has no
        label->ID map; a caller holding only labels must resolve IDs itself
        before calling (see `scored_sweep_driver.py`'s `DE_ID` /
        `CLOUD_PL_ID` constants for the pattern this expects).

        Perspectives checked on EVERY poll tick (ALL must agree before
        `ready=True`):
          1. onezone user-scoped support map — `_check_user_spaces_providers`
             (the CORRECT map; NEVER `.supportingProviders`)
          2. onezone provider-list endpoint — `_check_spaces_providers_list`
             (cross-check against a second, independent endpoint)
          3. per-provider onezone-reported liveness —
             `_check_providers_online`, via `verifier.get_provider_online`

        `verifier` defaults to a fresh `harness.verifier.Verifier` instance
        (its `get_provider_online` calls onezone directly and ignores
        `source_host`, so the placeholder host below is never actually
        dialed for that call) — pass an existing `Verifier` to share a
        token, or a duck-typed fake exposing `.get_provider_online` for
        tests (mirrors `harness/test_verifier.py`'s own monkeypatch
        convention: `v.get_provider_online = lambda pid: ...`).

        NEVER raises on a transient HTTP failure mid-wait: a network blip on
        one perspective just makes that tick's check read not-ready; the
        loop retries next poll rather than aborting. NEVER raises on
        timeout either — returns `ready=False` with full detail; the
        CALLER decides whether that is fail-loud (both
        `scored_sweep_driver.py` and `a4_sweep_driver.py` treat
        `ready=False` as FATAL before spending any trial, tearing down the
        just-provisioned space regardless via their existing `finally`).

        Returns:
            {"ready": bool, "elapsed_s": float,
             "perspectives": {"user_spaces_providers": {...},
                               "spaces_providers_list": {...},
                               "provider_online": {...}},
             "missing": [<provider ids missing from ANY perspective, sorted>]}
        """
        required = list(required_providers)
        if verifier is None:
            verifier = Verifier(source_host=ONEZONE_URL)

        start = now_fn()
        while True:
            p_user = self._check_user_spaces_providers(space_id, required)
            p_list = self._check_spaces_providers_list(space_id, required)
            p_online = self._check_providers_online(required, verifier)
            perspectives = {
                "user_spaces_providers": p_user,
                "spaces_providers_list": p_list,
                "provider_online": p_online,
            }
            missing = sorted(set(p_user["missing"]) | set(p_list["missing"]) | set(p_online["missing"]))
            ready = p_user["ok"] and p_list["ok"] and p_online["ok"]
            elapsed = now_fn() - start
            if ready or elapsed >= timeout_s:
                return {
                    "ready": ready, "elapsed_s": elapsed,
                    "perspectives": perspectives, "missing": missing,
                }
            sleep_fn(poll_interval_s)


# --- fail-fast transfer-health canary ----------------------------------
# `wait_for_space_ready` above proves inter-provider SUPPORT has converged —
# it does NOT prove the rtransfer DATA PATH actually moves bytes. A space
# can pass every wait_for_space_ready perspective and STILL have a stalled
# data path: an rtransfer fetch rejected as `unauthorized`, or a
# transfer stuck `scheduled` with `startTime==0` indefinitely. Rather than
# bake waiting/latency into the test suite, this function is a SINGLE cheap
# canary transfer, bounded by CANARY_TIMEOUT_S, that a driver runs ONCE per
# sweep (not per cell) right after wait_for_space_ready succeeds and BEFORE
# spending any real trial. A manual mesh restart is the known remedy for a
# stalled path — the harness NEVER waits on it; it only detects the
# condition and fails loud so the caller can abort before wasting a live
# trial against a dead data path. ---

def _transfer_looks_unauthorized(transfer: Optional[dict]) -> bool:
    """Best-effort match for the `unauthorized` transfer-failure signature.

    There is no confirmed live example of the failed-transfer REST
    response shape (the evidence for this failure mode is op_worker/
    rtransfer_link LOG lines — `Fetch ... failed due to: unauthorized` /
    `ErrorStatus = {error,{connection,<<"unauthorized">>}}` — not a captured
    `GET /oneprovider/transfers/{id}` body). Rather than guess a specific
    field name, this searches the ENTIRE `get_transfer_status` response
    case-insensitively for the substring `"unauthorized"` — deliberately
    broad so it fires regardless of which field (if any) onedata surfaces
    the reason under. False positives would require the literal word
    "unauthorized" to appear somewhere else in a transfer's status body,
    which is not a known false-positive source in the reproductions seen
    so far.
    """
    if not isinstance(transfer, dict):
        return False
    try:
        blob = json.dumps(transfer, default=str)
    except (TypeError, ValueError):
        blob = str(transfer)
    return "unauthorized" in blob.lower()


def _canary_attempt(
    fixture_mgr: object,
    verifier: object,
    target_provider_id: str,
    *,
    content_bytes: Optional[bytes],
    timeout_s: float,
    poll_interval_s: float,
    schedule_max_retries: int,
    schedule_retry_backoff_s: float,
    sleep_fn: Callable[[float], None],
    now_fn: Callable[[], float],
) -> Dict:
    """ONE cheap canary transfer, bounded by `timeout_s` — a SINGLE attempt
    of the fail-fast replacement for "wait an arbitrary/long time and hope
    the transfer path is healthy." Called by `assert_transfer_path_healthy`
    (below), which wraps this in a bounded OUTER retry to distinguish a
    transient failure from a persistent stall — see that function's docstring.

    `fixture_mgr` (a `fixtures.FixtureManager` or duck-typed fake exposing
    `.write_trial_file(rel_path, content_bytes)` +
    `.schedule_file_replication(file_id, target_provider_id)`) and
    `verifier` (a `verifier.Verifier` or duck-typed fake exposing
    `.get_transfer_status(transfer_id)` + `.get_distribution(file_id)`) are
    both INJECTED collaborators — this module has no dependency on either
    class, mirroring `verifier.verify_convergence_multiperspective`'s own
    injected-collaborator shape (that helper closes the same silent-fallback
    class from the outcome-confirmation side; this one closes it from the
    pre-flight/fail-fast side).

    Steps (never raises — always returns the structured dict below; the
    CALLER decides whether `healthy=False` is fail-loud, matching
    `wait_for_space_ready`'s own never-raises contract):

    1. Write a `CANARY_CONTENT_SIZE`-byte (4 KiB — the calibrated-range
       floor) canary file to the source via `fixture_mgr.write_trial_file`.
       A write failure itself is reported as `healthy=False` (never raised).
    2. `fixture_mgr.schedule_file_replication(file_id, target_provider_id)`.
       Retries up to `schedule_max_retries` times, `schedule_retry_backoff_s`
       apart, ONLY for the specific transient signature `status==400` with
       `"spaceNotSupportedBy"` in the body — a narrower, distinct
       eventual-consistency gap than the one `wait_for_space_ready` closes
       (support has converged per every wait_for_space_ready perspective,
       but a provider can still 400 for a few seconds after that). Any
       other non-201/no-transfer-id outcome (including the transient
       signature with retries exhausted) is reported as `healthy=False`
       immediately — never an unbounded retry.
    3. Poll `verifier.get_transfer_status(transfer_id)` +
       `verifier.get_distribution(file_id)` every `poll_interval_s`, bounded
       by `timeout_s`. Convergence is decided on `Verifier._physical_size(
       distribution, target_provider_id) == expected_size` ONLY — the SAME
       frozen predicate `Verifier.convergence_rel_s` uses (never
       `replicationStatus`/`virtualSize`/QoS: status and byte-placement can
       diverge in BOTH directions — `replicationStatus` reads `scheduled`
       for transfers that actually converged within seconds, AND separately
       can read `completed` while physicalSize stays 0 for far longer.
       Gating `healthy=True` on `replicationStatus=="completed"` as well as
       bytes — a literal reading of "transfer reaches completed with bytes
       on target" — would therefore make this canary report UNHEALTHY on the
       common case of a perfectly healthy, already-converged transfer,
       which is the OPPOSITE of fail-fast. `replicationStatus`/`startTime`
       are read every tick and folded into the UNHEALTHY diagnostic
       `reason` (the stuck-`scheduled`/`startTime==0` signature) but never
       gate the HEALTHY decision.
       An `unauthorized` signature (`_transfer_looks_unauthorized`) is
       checked every tick and short-circuits to `healthy=False`
       immediately — no reason to burn the rest of the window once that
       signature has been seen.

    Returns:
        {"healthy": bool, "elapsed_s": float,
         "final_status": <str|None, last replicationStatus observed>,
         "physical_size": <int|None, last successfully-probed physicalSize>,
         "reason": str}
    """
    payload = content_bytes if content_bytes is not None else (b"x" * CANARY_CONTENT_SIZE)
    rel_path = f"canary/transfer-health-{uuid.uuid4().hex[:8]}.bin"

    start = now_fn()
    try:
        write_result = fixture_mgr.write_trial_file(rel_path, payload)
    except Exception as e:  # noqa: BLE001 -- never raise; caller decides on healthy=False
        return {
            "healthy": False, "elapsed_s": now_fn() - start, "final_status": None,
            "physical_size": None,
            "reason": f"canary write_trial_file failed: {type(e).__name__}: {e}",
        }
    file_id = write_result["file_id"]
    expected_size = write_result["expected_size"]

    transfer_id: Optional[str] = None
    for attempt in range(1, schedule_max_retries + 1):
        try:
            sched = fixture_mgr.schedule_file_replication(file_id, target_provider_id)
        except Exception as e:  # noqa: BLE001
            return {
                "healthy": False, "elapsed_s": now_fn() - start, "final_status": None,
                "physical_size": None,
                "reason": f"canary schedule_file_replication raised: {type(e).__name__}: {e}",
            }
        sched_status = sched.get("status")
        sched_body = sched.get("body")
        transfer_id = sched.get("transfer_id")
        if sched_status == 201 and transfer_id:
            break
        is_transient_settle = sched_status == 400 and "spaceNotSupportedBy" in str(sched_body)
        if is_transient_settle and attempt < schedule_max_retries:
            sleep_fn(schedule_retry_backoff_s)
            continue
        return {
            "healthy": False, "elapsed_s": now_fn() - start, "final_status": None,
            "physical_size": None,
            "reason": (
                f"canary transfer schedule failed status={sched_status!r} "
                f"body={str(sched_body)[:200]!r} (attempt {attempt}/{schedule_max_retries}"
                f"{', transient spaceNotSupportedBy retries exhausted' if is_transient_settle else ''})"
            ),
        }

    last_physical_size: Optional[int] = None
    last_status: Optional[str] = None
    last_start_time = None
    while True:
        elapsed = now_fn() - start

        try:
            transfer = verifier.get_transfer_status(transfer_id)
        except Exception:  # noqa: BLE001 -- dead probe; tolerated like poll_state_timeline's samples
            transfer = None
        if isinstance(transfer, dict):
            last_status = transfer.get("replicationStatus", last_status)
            last_start_time = transfer.get("startTime", last_start_time)

        try:
            distribution = verifier.get_distribution(file_id)
        except Exception:  # noqa: BLE001
            distribution = None
        physical_size = (
            Verifier._physical_size(distribution, target_provider_id)
            if distribution is not None else None
        )
        if physical_size is not None:
            last_physical_size = physical_size

        if physical_size == expected_size:
            return {
                "healthy": True, "elapsed_s": elapsed, "final_status": last_status,
                "physical_size": physical_size,
                "reason": (
                    f"canary transfer placed {physical_size} bytes on {target_provider_id!r} "
                    f"(expected {expected_size}) at t={elapsed:.1f}s"
                ),
            }

        if _transfer_looks_unauthorized(transfer):
            return {
                "healthy": False, "elapsed_s": elapsed, "final_status": last_status,
                "physical_size": last_physical_size,
                "reason": (
                    f"canary transfer status carries an 'unauthorized' error "
                    f"at t={elapsed:.1f}s: {transfer!r}"
                ),
            }

        if elapsed >= timeout_s:
            stuck_signature = last_status == "scheduled" and last_start_time in (0, None)
            return {
                "healthy": False, "elapsed_s": elapsed, "final_status": last_status,
                "physical_size": last_physical_size,
                "reason": (
                    f"canary transfer did not converge within {timeout_s:.0f}s "
                    f"(final_status={last_status!r} start_time={last_start_time!r} "
                    f"physical_size={last_physical_size!r} expected_size={expected_size}). "
                    + (
                        "Matches the stuck-scheduled/startTime==0 signature: the transfer is still "
                        "scheduled with no start time, so the inter-site data path is not "
                        "processing transfers."
                        if stuck_signature else
                        "The transfer path did not move the expected bytes within the fail-fast window."
                    )
                ),
            }

        sleep_fn(poll_interval_s)


def assert_transfer_path_healthy(
    fixture_mgr: object,
    verifier: object,
    target_provider_id: str,
    *,
    content_bytes: Optional[bytes] = None,
    timeout_s: float = CANARY_TIMEOUT_S,
    poll_interval_s: float = CANARY_POLL_INTERVAL_S,
    schedule_max_retries: int = CANARY_SCHEDULE_MAX_RETRIES,
    schedule_retry_backoff_s: float = CANARY_SCHEDULE_RETRY_BACKOFF_S,
    max_attempts: int = CANARY_MAX_ATTEMPTS,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], float] = time.time,
) -> Dict:
    """The fail-fast transfer-health canary — bounded OUTER retry over
    `_canary_attempt` (above), which does the actual write/schedule/poll
    work for ONE attempt.

    Why the outer retry exists: the live transfer path has an intermittent
    "completed-with-0-bytes" transient failure — a transfer reaches a
    terminal status but 0 bytes land, then a FRESH attempt (new canary file)
    converges normally seconds later. Without this retry, a single such
    transient failure would make `_canary_attempt` return `healthy=False`,
    and a sweep driver's HEAD canary would fail-fast-abort the entire sweep
    on that one transient failure (0 cells run), even though the transfer
    path itself was not actually stalled. This is the SAME
    transient-vs-persistent discriminator already used elsewhere in this
    module/harness for eventual-consistency gaps (`resolve_root`
    ENOENT-settle retries, the `spaceNotSupportedBy` schedule-retry inside
    `_canary_attempt` itself, the netem qdisc-del clear retry) — a bounded
    retry absorbs a TRANSIENT failure (clears on a fresh attempt); a
    PERSISTENT stall (every attempt fails) still fails loud, preserving
    fail-fast.

    EVERY unhealthy outcome from `_canary_attempt` is retryable here (write
    raise, schedule failure, timeout/completed-0-bytes, unauthorized
    signature) — a fresh full attempt (new canary file; never the failed
    one) is itself the transient-vs-persistent discriminator, so this
    doesn't need to special-case which failure reason is "worth" retrying.
    Each attempt gets its OWN full `timeout_s` window: the common healthy
    case (attempt 1 converges) returns fast; only a genuine persistent stall
    pays the full `max_attempts x timeout_s` cost.

    On success, the winning attempt's dict is returned with an added
    `"attempts"` key (how many attempts it took, 1-indexed). On exhausting
    `max_attempts` without success, the LAST attempt's dict is returned
    (`"attempts": max_attempts`) with `"reason"` suffixed to make clear this
    is a persistent stall, not a one-off transient failure — matching every
    other never-raises canary/readiness helper in this module (the CALLER
    decides whether `healthy=False` is fail-loud).
    """
    last_result: Optional[Dict] = None
    for attempt in range(1, max_attempts + 1):
        result = _canary_attempt(
            fixture_mgr, verifier, target_provider_id,
            content_bytes=content_bytes,
            timeout_s=timeout_s,
            poll_interval_s=poll_interval_s,
            schedule_max_retries=schedule_max_retries,
            schedule_retry_backoff_s=schedule_retry_backoff_s,
            sleep_fn=sleep_fn,
            now_fn=now_fn,
        )
        if result["healthy"]:
            result["attempts"] = attempt
            return result
        last_result = result
        if attempt < max_attempts:
            sleep_fn(CANARY_RETRY_BACKOFF_S)

    last_result["attempts"] = max_attempts
    last_result["reason"] = (
        last_result["reason"]
        + f" (unhealthy after {max_attempts} canary attempts — persistent, not transient)"
    )
    return last_result
