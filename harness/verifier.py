"""Verifier — isolated state-timeline poller.

Holds its OWN token, separate from FixtureManager's instance. It is the same
onezone access-token type; the verifier issues only read requests.

**CRITICAL PREDICATE:** the convergence predicate is `physicalSize ==
expected_size` ONLY. NEVER virtualSize; NEVER aggregate QoS status. Both
are diagnostic-only and lag real data placement.

Stdlib-only. No top-level side effects on import.
"""
from __future__ import annotations

import json
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from typing import Callable, Dict, Optional

# --- deployment settings (config/federation.json; see harness/settings.py) ---
try:
    from . import settings
except ImportError:
    import settings
KNOWN_HOSTS_PATH = settings.get("ssh_mint.known_hosts", "")
SSH_HOST = settings.get("ssh_mint.ssh_host", "")
ONEZONE_URL = settings.get("onezone_url")
ONEZONE_ADMIN_USER = settings.get("onezone_admin_user", "admin")
REMOTE_ONEZONE_VALUES_PATH = settings.get("ssh_mint.onezone_values_path", "")

TOKEN_REFRESH_AGE_S = 3600.0
SSH_MINT_TIMEOUT_S = 30
SSH_MINT_RETRIES = 5
SSH_MINT_RETRY_BACKOFF_S = 2.0

# Bounded transient retry for the REST poller (federation `Errno 104` resets
# etc.). Applies ONLY to network-layer errors (URLError/timeout/connection
# reset) — NOT to HTTPError, which is a real answer (e.g. 404) and must never
# be retried.
HTTP_TRANSIENT_RETRIES = 3
HTTP_TRANSIENT_BACKOFF_S = 1.0

# Closed research federation with self-signed provider certs — no hostname/CA
# verification (public API equivalent of the former _create_unverified_context()).
SSL_CTX = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


class VerifierError(RuntimeError):
    """Raised on any verifier REST/SSH failure. Fails loud — never silently swallowed."""


class Verifier:
    """Polls the state-timeline via its own REST client + own token.

    The token is a onezone access token of the same type FixtureManager uses;
    the verifier issues only read requests with it.
    """

    def __init__(self, source_host: str, mint_mode: str = "local"):
        self.source_host = source_host.rstrip("/")
        # mint_mode: "local" (HTTPS, default — avoids the assh/sandbox DNS issue)
        # or "ssh" (mint on a remote host, kept for portability).
        self.mint_mode = mint_mode
        self._token: Optional[str] = None
        self._token_minted_at: Optional[float] = None

    # --- token / REST plumbing (own instance; isolated from FixtureManager's) ---

    def _mint_token(self) -> str:
        """Mint a fresh token via the configured mint_mode. Never logs the value."""
        if self.mint_mode == "local":
            # Reuse the single local-mint helper (keeps one code path for creds).
            try:
                from .fixtures import mint_token_local
            except ImportError:
                from harness.fixtures import mint_token_local
            token = mint_token_local()
            self._token = token
            self._token_minted_at = time.time()
            return token
        return self._mint_token_ssh()

    def _mint_token_ssh(self) -> str:
        now = int(time.time())
        exp = now + 14400
        remote_cmd = (
            "PASS=$(awk '/main_onezone_admin:/{f=1} f&&/password:/{print $2;exit}' "
            f"{REMOTE_ONEZONE_VALUES_PATH}); "
            f"curl -sk -u \"{ONEZONE_ADMIN_USER}:$PASS\" -X POST {ONEZONE_URL}/api/v3/onezone/user/tokens/temporary "
            "-H 'content-type: application/json' "
            f"-d '{{\"type\":{{\"accessToken\":{{}}}},\"caveats\":[{{\"type\":\"time\",\"validUntil\":{exp}}}]}}'"
        )
        last_err = None
        for attempt in range(1, SSH_MINT_RETRIES + 1):
            result = subprocess.run(
                [
                    "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                    "-o", f"UserKnownHostsFile={KNOWN_HOSTS_PATH}", SSH_HOST, remote_cmd,
                ],
                capture_output=True, text=True, timeout=SSH_MINT_TIMEOUT_S,
            )
            if result.returncode == 0:
                try:
                    data = json.loads(result.stdout)
                    token = data["token"]
                except (json.JSONDecodeError, KeyError) as e:
                    raise VerifierError(
                        f"token mint: SSH succeeded but response was not parseable as {{'token': ...}}: {e}"
                    ) from e
                self._token = token
                self._token_minted_at = time.time()
                return token
            last_err = f"ssh rc={result.returncode} stderr={result.stderr[:300]!r}"
            if attempt < SSH_MINT_RETRIES:
                time.sleep(SSH_MINT_RETRY_BACKOFF_S)
        raise VerifierError(
            f"Verifier token mint failed after {SSH_MINT_RETRIES} attempts: {last_err}"
        )

    def _get_token(self) -> str:
        if self._token is None or (time.time() - self._token_minted_at) > TOKEN_REFRESH_AGE_S:
            self._mint_token()
        return self._token

    def _http(self, method: str, url: str, body: Optional[bytes] = None,
              content_type: Optional[str] = None, timeout: int = 60):
        """Issue one HTTP request, tolerating transient network failures.

        `HTTPError` (a real HTTP status code, e.g. 404/500) is returned as-is
        — never retried, since it's a genuine answer from the server.
        Network-layer failures (`URLError`, socket timeouts, connection
        resets/refusals — the federation `Errno 104` reset class) are
        retried up to `HTTP_TRANSIENT_RETRIES` times with a fixed backoff;
        if every attempt fails, this returns the `(0, None)` sentinel rather
        than raising, so callers (`get_distribution` et al.) fall through
        their existing `status != 200` → `None` dead-probe path instead of
        `poll_state_timeline` crashing outright.
        """
        req = urllib.request.Request(url, method=method)
        req.add_header("X-Auth-Token", self._get_token())
        if content_type:
            req.add_header("content-type", content_type)
        status = None
        raw = None
        for attempt in range(1, HTTP_TRANSIENT_RETRIES + 1):
            try:
                with urllib.request.urlopen(req, data=body, timeout=timeout, context=SSL_CTX) as resp:
                    status = resp.getcode()
                    raw = resp.read()
                break
            except urllib.error.HTTPError as e:
                status = e.code
                raw = e.read()
                break
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError):
                if attempt < HTTP_TRANSIENT_RETRIES:
                    time.sleep(HTTP_TRANSIENT_BACKOFF_S)
                    continue
                return 0, None
        try:
            parsed = json.loads(raw) if raw else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            parsed = raw
        return status, parsed

    # --- public API ---

    def get_transfer_status(self, transfer_id: str) -> Optional[dict]:
        """Diagnostic-only: fetch the CURRENT get_transfer detail (includes
        `replicationStatus`, `transferState`, `bytesReplicated`, etc.) for
        `transfer_id`. NEVER used in the convergence predicate — see
        `convergence_rel_s`'s docstring ("metadata/status lags data
        placement"; verdicts cut on physicalSize only). Mirrors
        `get_distribution`'s shape: returns None
        on any non-200/non-dict response rather than raising, so a
        diagnostic probe failure never aborts a trial. Uses this Verifier's
        own token/instance (same isolation as get_distribution).
        """
        status, body = self._http(
            "GET", f"{self.source_host}/api/v3/oneprovider/transfers/{transfer_id}", timeout=20
        )
        if status != 200 or not isinstance(body, dict):
            return None
        return body

    def get_distribution(self, file_id: str) -> Optional[dict]:
        status, body = self._http(
            "GET", f"{self.source_host}/api/v3/oneprovider/data/{file_id}/distribution", timeout=20
        )
        if status != 200 or not isinstance(body, dict):
            return None
        return body

    def get_provider_online(self, provider_id: str) -> Optional[bool]:
        """Intent-agnostic per-provider liveness — onezone's registry view.

        This is the `provider_health` SLI: exactly what a
        classical, intent-blind A1 monitor reads — onezone reporting whether a
        provider is online — as opposed to the physicalSize predicate (which is
        source-host-polled, so a TARGET outage stalls the value rather than
        failing the probe; that asymmetry is WHY a separate health SLI is
        needed). Hits ONEZONE (not the oneprovider `source_host`):
        `GET {ONEZONE_URL}/api/v3/onezone/providers/{id}` → `.online` (bool).
        Returns None on any non-200 / non-dict / missing-field response so a
        dead health probe is distinguishable from a genuinely-offline provider
        (probe_ok=False vs value=False at the sample level), mirroring
        get_distribution's None-on-failure contract.
        """
        status, body = self._http(
            "GET", f"{ONEZONE_URL}/api/v3/onezone/providers/{provider_id}", timeout=20
        )
        if status != 200 or not isinstance(body, dict):
            return None
        online = body.get("online")
        return online if isinstance(online, bool) else None

    def probe_residue(self, file_id: str, path: str, provider_ids: list) -> dict:
        """E3 negative-probe: an OUTCOME-COMPLETE residue
        probe combining POSITIVE absence ("does `path` still resolve?")
        with NEGATIVE residue ("does the pre-captured `file_id`'s
        distribution still show bytes on any of `provider_ids`?").

        Returns {"path_absent": bool, "total_residue": int,
        "per_provider": {pid: int}, "probe_ok": bool}.

        `path_absent` resolves `path` via the same lookup-file-id POST
        `resolve_root`/`write_trial_file` use (fixtures.py): a 200 with a
        `fileId` in the body means the path still exists (`path_absent =
        False`); a POSIX ENOENT signature (400, per
        `fixtures._is_posix_enoent`) or a bare 404 means it's gone
        (`path_absent = True`). Any OTHER outcome (a network-layer failure
        -- this Verifier's own `_http`'s `(0, None)` dead-probe sentinel --
        or an uninterpretable response) means the probe itself is
        unreliable here: `path_absent = None`, `probe_ok = False`. Never
        guesses.

        The residue side NEVER fails `probe_ok`: `get_distribution` already
        returns `None` on any dead probe / non-200 response, and
        `_physical_size(None, pid)` is `0` — so a dead distribution probe
        reads as "no residue" (`total_residue = 0`, all-zero
        `per_provider`), matching the scenario contract's E3 rule ("Both
        possible deleted-fileId distribution responses -- an error/404 OR
        an all-zeros map -- count as 'no residue'"). `probe_ok` is
        therefore driven
        entirely by the absence-check; a residue-side dead probe is a
        contractually-defined "gone" reading, not an unreliable one.
        """
        try:
            from .fixtures import _is_posix_enoent
        except ImportError:
            from harness.fixtures import _is_posix_enoent

        encoded_path = path.replace("/", "%2F")
        status, body = self._http(
            "POST", f"{self.source_host}/api/v3/oneprovider/lookup-file-id/{encoded_path}",
            timeout=20,
        )
        if status == 200 and isinstance(body, dict) and "fileId" in body:
            path_absent: Optional[bool] = False
            probe_ok = True
        elif status == 404 or _is_posix_enoent(status, body):
            path_absent = True
            probe_ok = True
        else:
            # Network-layer failure (the _http (0, None) sentinel) or an
            # unexpected/uninterpretable status -- the probe machinery
            # itself is unreliable here, not a real yes/no answer.
            path_absent = None
            probe_ok = False

        distribution = self.get_distribution(file_id)
        per_provider = {pid: self._physical_size(distribution, pid) for pid in provider_ids}
        total_residue = sum(per_provider.values())

        return {
            "path_absent": path_absent,
            "total_residue": total_residue,
            "per_provider": per_provider,
            "probe_ok": probe_ok,
        }

    @staticmethod
    def _physical_size(distribution: Optional[dict], provider_id: str) -> int:
        """Sum physicalSize across storage backends for one provider."""
        if not distribution:
            return 0
        entry = distribution.get("distributionPerProvider", {}).get(provider_id, {})
        if not isinstance(entry, dict):
            return 0
        backends = entry.get("distributionPerStorageBackend", {}) or {}
        return sum(
            sb.get("physicalSize", 0)
            for sb in backends.values() if isinstance(sb, dict)
        )

    def poll_residue_timeline(
        self,
        file_id: str,
        path: str,
        provider_ids: list,
        poll_interval_s: float,
        poll_until_rel_s: float,
        t0_epoch: float,
    ) -> dict:
        """Poll `probe_residue` every `poll_interval_s` until `t_rel_s >=
        poll_until_rel_s`. Mirrors `poll_state_timeline`'s forward-blocking
        shape, for the E3 residue
        probe instead of the physicalSize convergence probe -- this poll IS
        the wait for a live `wait_repoll` gate cycle, same as
        `poll_state_timeline` (no separate sleep needed by the caller).

        Each entry mirrors `runner._residue_sample_dict`'s shape exactly
        (replicated INLINE here, not imported -- this module must not
        import `runner`): `{t_rel_s, probe_ok, path_absent,
        total_residue, per_provider}`, with `probe_ok is False` nulling the
        three data fields (a dead probe never guesses).

        Returns `{"residue_samples": [...], "poll_interval_s": ...,
        "poll_until_rel_s": ..., "polled_past_t_max": bool}`. Unlike
        `poll_state_timeline`, this method takes no `t_max_s` argument (the
        caller already folds T_max into its own `poll_until_rel_s` choice —
        see `a4_gate.make_live_e3_verify_fn`, which always passes
        `effective_t_max + OBSERVATION_MARGIN_S`), so `polled_past_t_max` is
        unconditionally `True` here — this poll is BY CONSTRUCTION always
        run past whatever T_max the caller cares about; the field is kept
        only for return-shape parity with `poll_state_timeline`'s dict and
        is not read by any E3 lens (`harness/lenses/e3.py`'s consumers only
        read `residue_samples`).

        No unit test needed (live-only, same as `poll_state_timeline`) —
        stdlib + the existing `probe_residue` only.
        """
        residue_samples: list = []
        while True:
            t_rel_s = time.time() - t0_epoch
            t_rel_r = round(t_rel_s, 3)
            probe = self.probe_residue(file_id, path, provider_ids)
            probe_ok = bool(probe.get("probe_ok"))
            residue_samples.append({
                "t_rel_s": t_rel_r,
                "probe_ok": probe_ok,
                "path_absent": probe.get("path_absent") if probe_ok else None,
                "total_residue": probe.get("total_residue") if probe_ok else None,
                "per_provider": probe.get("per_provider") if probe_ok else None,
            })
            if t_rel_s >= poll_until_rel_s:
                break
            time.sleep(poll_interval_s)

        return {
            "residue_samples": residue_samples,
            "poll_interval_s": poll_interval_s,
            "poll_until_rel_s": poll_until_rel_s,
            "polled_past_t_max": True,
        }

    def poll_state_timeline(
        self,
        file_id: str,
        expected_size: int,
        target_provider_id: str,
        term_id: str,
        poll_interval_s: float,
        poll_until_rel_s: float,
        t0_epoch: float,
        t_max_s: float,
        health_provider_ids: Optional[list] = None,
    ) -> dict:
        """Poll physicalSize every poll_interval_s until t_rel_s >= poll_until_rel_s.

        Polls PAST convergence by design (the timeline-past-T_max is ground
        truth) — never stops early at first convergence. `t0_epoch` is the
        SAME epoch as the recording's `trial_start_utc` (one clock, per §3),
        not necessarily the transfer-trigger instant. `t_max_s` is the
        contract term's T_max — used only to derive `polled_past_t_max`, not
        the poll loop's own stop condition (that's `poll_until_rel_s`).

        Each physicalSize sample records `probe_ok` so a dead probe
        (get_distribution returned None — non-200 or non-dict body) is
        distinguishable from a genuinely empty target: on failure `value` is
        `None` (JSON null) and `probe_ok` is False; on success `value` is the
        numeric physicalSize and `probe_ok` is True.

        `health_provider_ids`: when given, ALSO
        emit a `provider_health` boolean SLI sample per listed provider on each
        tick — onezone's intent-agnostic liveness view (get_provider_online),
        interleaved into the SAME `samples` list (consumers filter by
        `sli_name`). This is what an intent-blind A1 monitor reads; it lets A1
        catch a target pod-kill (a target outage stalls physicalSize but does
        NOT fail the source-host-polled physicalSize probe, so it's invisible
        without this stream) and feeds A3b's E-health corroboration. Same
        probe_ok discipline: a dead health probe → `value=None, probe_ok=False`;
        a live one → `value=<online bool>, probe_ok=True`. Health samples are
        tagged with `term_id` so the per-term consumer filters match.
        """
        health_provider_ids = health_provider_ids or []
        samples = []
        while True:
            t_rel_s = time.time() - t0_epoch
            t_rel_r = round(t_rel_s, 3)
            dist = self.get_distribution(file_id)
            probe_ok = dist is not None
            value = self._physical_size(dist, target_provider_id) if probe_ok else None
            samples.append({
                "t_rel_s": t_rel_r,
                "term_id": term_id,
                "sli_name": "physicalSize",
                "value": value,
                "provider": target_provider_id,
                "probe_ok": probe_ok,
            })
            for pid in health_provider_ids:
                online = self.get_provider_online(pid)
                h_ok = online is not None
                samples.append({
                    "t_rel_s": t_rel_r,
                    "term_id": term_id,
                    "sli_name": "provider_health",
                    "value": online if h_ok else None,
                    "provider": pid,
                    "probe_ok": h_ok,
                })
            if t_rel_s >= poll_until_rel_s:
                break
            time.sleep(poll_interval_s)

        return {
            "poll_interval_s": poll_interval_s,
            "poll_until_rel_s": poll_until_rel_s,
            "polled_past_t_max": poll_until_rel_s >= t_max_s,
            "samples": samples,
        }

    @staticmethod
    def convergence_rel_s(state_timeline, expected_size, target_provider_id, term_id):
        """First t_rel_s where a SUCCESSFUL physicalSize probe on the TARGET provider
        for THIS term equals expected_size, else None.
        Equality (==) is justified by the unique-fresh-file fixture discipline (each
        trial writes a distinct file; expected_size is the a-priori write-time size).
        Filters on provider AND term so source-side samples (source holds the file
        from t=0) and other terms' samples can't false-fire. Requires probe_ok.
        NEVER virtualSize, NEVER aggregate QoS status.
        """
        for s in state_timeline.get("samples", []):
            if (s.get("probe_ok") is True
                    and s.get("provider") == target_provider_id
                    and s.get("term_id") == term_id
                    and s.get("sli_name") == "physicalSize"
                    and s.get("value") == expected_size):
                return s["t_rel_s"]
        return None


# --- outcome-side multi-perspective hardening helper (eventual-consistency ---
# safe convergence confirmation; addresses the same support-propagation
# silent-fallback class as provision.Provisioner.wait_for_space_ready).
# REUSABLE: this is NOT wired into the main verdict pipeline
# (poll_state_timeline / convergence_rel_s / the A0-A3b lenses) by this
# change — it is a helper a caller opts into when it wants a convergence
# read that isn't a single eventually-consistent sample. Candidate call
# sites: a live post-trial spot-check in
# scored_sweep_driver.py/a4_sweep_driver.py right after
# Verifier.convergence_rel_s first reports a hit (to rule out a one-off
# blip before trusting it); a future harder-outcome A0/A1 lens variant; a
# manually-run live diagnostic probe. ---

def verify_convergence_multiperspective(
    verifier: "Verifier",
    file_id: str,
    expected_size: int,
    target_provider_id: str,
    *,
    timeout_s: float = 15.0,
    poll_interval_s: float = 3.0,
    consecutive_required: int = 2,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], float] = time.time,
) -> Dict:
    """Confirm convergence from more than one angle before trusting it.

    (a) distribution-based predicate — `Verifier._physical_size(dist,
        target_provider_id) == expected_size` — the EXACT SAME predicate
        `Verifier.convergence_rel_s` uses (NEVER virtualSize, NEVER
        aggregate QoS status, per this module's docstring). Called as
        `Verifier._physical_size`
        (the class, not the passed-in `verifier` instance) so a caller can
        hand in any object exposing `.get_distribution(file_id)` without
        also needing to inherit `_physical_size` itself.
    (b) CONSISTENCY across >= `consecutive_required` back-to-back polls
        (`poll_interval_s` apart) — guards against a single
        eventually-consistent blip: a read that happens to match
        `expected_size` on one poll but reverts/lags on the very next one,
        which a ONE-SHOT check (a single `get_distribution` call) would
        misreport as converged. `consecutive_required` resets to 0 on any
        non-matching poll, so the requirement is genuinely CONSECUTIVE, not
        merely "matched N times total."

    Defaults are deliberately small (`timeout_s=15.0`, `poll_interval_s=3.0`)
    relative to a primary convergence-wait window (e.g.
    `Verifier.poll_state_timeline`'s own `poll_until_rel_s`) — this helper
    is meant to run as a SHORT confirmatory pass after convergence has
    plausibly already happened once (e.g. immediately after
    `Verifier.convergence_rel_s` first reports a hit on a state timeline),
    not to perform the full wait from scratch. Override for a different use.

    NEVER raises: a dead probe (`get_distribution` returns `None`, or
    raises) records `probe_ok=False` for that poll and is treated as
    non-matching (never a crash, never a false-positive match) — same
    probe_ok discipline as `poll_state_timeline`'s samples.

    Returns:
        {"converged": bool,
         "physical_size": <int, the LAST successfully-probed value, or
                           None if every probe in the run was dead>,
         "checks": [{"t_rel_s": float, "probe_ok": bool,
                     "physical_size": int|None, "matches": bool}, ...]}
        `checks` is the ordered, complete list of polls taken during the
        run (bounded by `timeout_s`) so a caller can audit exactly which
        polls agreed/disagreed rather than trusting a single boolean.
    """
    checks: list = []
    consecutive = 0
    last_physical_size: Optional[int] = None
    start = now_fn()
    while True:
        t_rel = now_fn() - start
        try:
            dist = verifier.get_distribution(file_id)
        except Exception:  # noqa: BLE001 -- dead probe; recorded, never raised
            dist = None
        probe_ok = dist is not None
        physical_size = Verifier._physical_size(dist, target_provider_id) if probe_ok else None
        matches = probe_ok and physical_size == expected_size
        checks.append({
            "t_rel_s": round(t_rel, 3), "probe_ok": probe_ok,
            "physical_size": physical_size, "matches": matches,
        })
        if probe_ok:
            last_physical_size = physical_size
        consecutive = consecutive + 1 if matches else 0
        if consecutive >= consecutive_required:
            return {"converged": True, "physical_size": physical_size, "checks": checks}
        if t_rel >= timeout_s:
            return {"converged": False, "physical_size": last_physical_size, "checks": checks}
        sleep_fn(poll_interval_s)
