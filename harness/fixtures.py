"""FixtureManager — per-trial namespaced writes into a reused, pre-supported space.

Isolation is **per-trial unique path within a per-sweep space**, NOT per-trial
space creation. The per-sweep space is created/supported ONCE (out of band,
already done for the reuse space this module targets); each trial writes a
unique file under `harness/` and teardown-verifies that one file.

Stdlib-only (urllib/ssl/subprocess/json/time) — no dependencies. REST +
SSH-token-mint patterns are scoped to this instance (own token, own http
client), independent of any other module that mints its own tokens.

No top-level side effects on import.
"""
from __future__ import annotations

import json
import os
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from typing import Optional

try:
    from . import settings
except ImportError:
    import settings

# --- deployment settings (config/federation.json; see harness/settings.py) ---
KNOWN_HOSTS_PATH = settings.get("ssh_mint.known_hosts", "")
SSH_HOST = settings.get("ssh_mint.ssh_host", "")
ONEZONE_URL = settings.get("onezone_url")
ONEZONE_ADMIN_USER = settings.get("onezone_admin_user", "admin")
ONEZONE_ADMIN_PASSWORD_ENV = settings.get("onezone_admin_password_env", "ONEZONE_ADMIN_PASSWORD")
REMOTE_ONEZONE_VALUES_PATH = settings.get("ssh_mint.onezone_values_path", "")

# Local onezone-admin creds readable on this VM (avoids the assh/sandbox DNS
# issue the SSH mint path hits). Password/token are held in-memory only —
# never printed, logged, or written to disk.
ONEZONE_VALUES_YAML = settings.get("onezone_values_path", "")

# Token TTL is ~2h in production; refresh with a safety margin.
TOKEN_REFRESH_AGE_S = 3600.0
SSH_MINT_TIMEOUT_S = 30
SSH_MINT_RETRIES = 5
SSH_MINT_RETRY_BACKOFF_S = 2.0
LOCAL_MINT_TIMEOUT_S = 30

# Bounded settle-retry for the post-provision root-resolution race. For ~1s
# after a fresh space is created + support has propagated (wait_for_space_ready
# green), the SOURCE provider's file tree can transiently return
# `400 {id: posix, errno: enoent}` on lookup-file-id /<space_name> before the
# space root materialises there. This is a THIRD eventual-consistency surface,
# distinct from support-propagation and the transfer-path convergence surface:
# it affects a minority of fresh spaces and clears within a retry or two
# (settle times on the order of ~1s). Following the fail-fast philosophy, a
# SECONDS-scale eventual-consistency gap is handled with a bounded settle-retry
# (NOT a fail-fast abort, and NOT a long transfer-recovery wait). It surfaces
# first on the transfer-health canary's write (the first write on a fresh space);
# real trial writes usually dodge it because verify_leg runs first. The bound is
# generous enough to absorb the race yet still fails LOUD (raises) if a
# genuine/persistent ENOENT does not clear — never a silent or unbounded wait.
ROOT_RESOLVE_SETTLE_MAX_RETRIES = 15
ROOT_RESOLVE_SETTLE_INTERVAL_S = 1.0

# Closed research federation with self-signed provider certs — no hostname/CA
# verification (public API equivalent of the former _create_unverified_context()).
SSL_CTX = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


def _read_admin_password(values_yaml: str = ONEZONE_VALUES_YAML) -> str:
    """Parse the onezone-admin password from the local values.yaml. Never logged.

    Mirrors the awk extraction used elsewhere: first `password:` line after the
    `main_onezone_admin:` key.
    """
    from_env = os.environ.get(ONEZONE_ADMIN_PASSWORD_ENV, "")
    if from_env:
        return from_env
    if not values_yaml:
        raise FixtureError(
            f"no onezone admin password: set {ONEZONE_ADMIN_PASSWORD_ENV} "
            "(or onezone_values_path in config/federation.json)")
    password = None
    seen_key = False
    with open(values_yaml) as f:
        for line in f:
            if "main_onezone_admin:" in line:
                seen_key = True
                continue
            if seen_key and "password:" in line:
                # take the token after 'password:'
                parts = line.split("password:", 1)[1].strip().split()
                if parts:
                    password = parts[0]
                break
    if not password:
        raise FixtureError(f"could not parse admin password from {values_yaml}")
    return password


def mint_token_local(onezone_url: str = ONEZONE_URL,
                     values_yaml: str = ONEZONE_VALUES_YAML,
                     ttl_s: int = 14400) -> str:
    """Mint an onezone temporary access token LOCALLY via HTTPS (no SSH).

    Reads the admin password from the local values.yaml and POSTs directly to
    onezone. Password + token are in-memory only — never printed/logged/written.
    Raises FixtureError (fail-loud) on any failure; never returns empty.
    """
    password = _read_admin_password(values_yaml)
    exp = int(time.time()) + ttl_s
    payload = json.dumps({
        "type": {"accessToken": {}},
        "caveats": [{"type": "time", "validUntil": exp}],
    }).encode()

    # Basic-auth admin:password, held only in the header for this one request.
    import base64
    userpass = base64.b64encode(f"{ONEZONE_ADMIN_USER}:{password}".encode()).decode()
    req = urllib.request.Request(
        f"{onezone_url}/api/v3/onezone/user/tokens/temporary", method="POST"
    )
    req.add_header("authorization", f"Basic {userpass}")
    req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, data=payload, timeout=LOCAL_MINT_TIMEOUT_S, context=SSL_CTX) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise FixtureError(f"local token mint HTTP {e.code}: {e.read()[:200]!r}") from e
    try:
        token = json.loads(raw)["token"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise FixtureError(f"local token mint: response not parseable as {{'token': ...}}: {e}") from e
    if not token:
        raise FixtureError("local token mint returned an empty token")
    return token


class FixtureError(RuntimeError):
    """Raised on any fixture-manager REST/SSH failure. Fails loud — never silently swallowed."""


def _is_posix_enoent(status: int, body: object) -> bool:
    """True for the post-provision root-resolution transient: a `400` whose
    body carries a POSIX ENOENT signature. Deliberately broad (substring on the
    serialised body, mirroring provision._transfer_looks_unauthorized) so it
    fires regardless of the exact field the errno surfaces under; the bounded
    settle-retry (ROOT_RESOLVE_SETTLE_*) makes a rare non-transient ENOENT still
    fail loud after the window rather than loop forever."""
    if status != 400:
        return False
    try:
        blob = json.dumps(body, default=str)
    except (TypeError, ValueError):
        blob = str(body)
    return "enoent" in blob.lower()


class FixtureManager:
    """Owns ONE onezone-admin token + REST client scoped to a single reused space.

    Space creation is out of scope for this class (admin-heavy, and the space
    is already supported on the target providers before this class is used).
    This class only resolves the existing root, writes namespaced per-trial
    files (capturing the write-time expected_size), and teardown-verifies
    deletion.
    """

    def __init__(self, space_name: str, space_id: str, source_host: str, source_provider: str,
                 mint_mode: str = "local"):
        self.space_name = space_name
        self.space_id = space_id
        self.source_host = source_host.rstrip("/")
        self.source_provider = source_provider
        # mint_mode: "local" (HTTPS, default — avoids the assh/sandbox DNS issue)
        # or "ssh" (a remote-mint path via SSH, kept for portability).
        self.mint_mode = mint_mode

        self._token: Optional[str] = None
        self._token_minted_at: Optional[float] = None
        self._root_id: Optional[str] = None

    # --- token / REST plumbing (own instance; isolated from Verifier's) ---

    def _mint_token(self) -> str:
        """Mint a fresh token via the configured mint_mode. Never logs the value."""
        if self.mint_mode == "local":
            token = mint_token_local()
            self._token = token
            self._token_minted_at = time.time()
            return token
        return self._mint_token_ssh()

    def _mint_token_ssh(self) -> str:
        """SSH to the ops host, read the admin password REMOTELY, mint the token REMOTELY.

        The password never crosses back to this process — only the token JSON does.
        Never logs/prints/writes the token value.
        """
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
                    raise FixtureError(
                        f"token mint: SSH succeeded but response was not parseable as {{'token': ...}}: {e}"
                    ) from e
                self._token = token
                self._token_minted_at = time.time()
                return token
            last_err = f"ssh rc={result.returncode} stderr={result.stderr[:300]!r}"
            if attempt < SSH_MINT_RETRIES:
                time.sleep(SSH_MINT_RETRY_BACKOFF_S)
        raise FixtureError(
            f"FixtureManager token mint failed after {SSH_MINT_RETRIES} attempts: {last_err}"
        )

    def _get_token(self) -> str:
        if self._token is None or (time.time() - self._token_minted_at) > TOKEN_REFRESH_AGE_S:
            self._mint_token()
        return self._token

    def _http(self, method: str, url: str, body: Optional[bytes] = None,
              content_type: Optional[str] = None, timeout: int = 60):
        req = urllib.request.Request(url, method=method)
        req.add_header("X-Auth-Token", self._get_token())
        if content_type:
            req.add_header("content-type", content_type)
        try:
            with urllib.request.urlopen(req, data=body, timeout=timeout, context=SSL_CTX) as resp:
                status = resp.getcode()
                raw = resp.read()
        except urllib.error.HTTPError as e:
            status = e.code
            raw = e.read()
        try:
            parsed = json.loads(raw) if raw else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            parsed = raw
        return status, parsed

    # --- public API ---

    def resolve_root(self, *, _sleep=time.sleep) -> str:
        """Resolve + cache the reused space's root fileId (POST lookup-file-id on %2F<space_name>).

        Bounded settle-retry on the post-provision `400 posix enoent` transient
        (ROOT_RESOLVE_SETTLE_*): the space root can take ~1s to materialise on
        the source provider's file tree after wait_for_space_ready is green.
        Any OTHER non-200 fails immediately (fail-fast on genuine errors); an
        ENOENT that persists past the settle window also raises (never silent,
        never unbounded). `_sleep` is injectable for tests."""
        if self._root_id is not None:
            return self._root_id
        encoded = f"%2F{self.space_name}"
        url = f"{self.source_host}/api/v3/oneprovider/lookup-file-id/{encoded}"
        status = body = None
        for attempt in range(1, ROOT_RESOLVE_SETTLE_MAX_RETRIES + 2):
            status, body = self._http("POST", url)
            if status == 200 and isinstance(body, dict) and "fileId" in body:
                self._root_id = body["fileId"]
                return self._root_id
            # Retry ONLY the transient post-provision ENOENT, bounded.
            if _is_posix_enoent(status, body) and attempt <= ROOT_RESOLVE_SETTLE_MAX_RETRIES:
                _sleep(ROOT_RESOLVE_SETTLE_INTERVAL_S)
                continue
            break
        suffix = (
            f" (POSIX ENOENT persisted past {ROOT_RESOLVE_SETTLE_MAX_RETRIES}x"
            f"{ROOT_RESOLVE_SETTLE_INTERVAL_S:.0f}s settle window -- space root did not "
            "materialise on the source; NOT the transient race)"
            if _is_posix_enoent(status, body) else ""
        )
        raise FixtureError(f"resolve_root failed status={status} body={str(body)[:200]}{suffix}")

    def write_trial_file(self, rel_path: str, content_bytes: bytes) -> dict:
        """PUT a namespaced per-trial file, then resolve its fileId.

        Returns {"file_id", "full_path", "expected_size"} where expected_size
        is captured HERE, at write time, from the source — the a-priori value
        the convergence predicate keys on (§3).
        """
        # Binding range-scope assertion: the calibrated [4KiB,16MiB] window is
        # enforced HERE so a future scenario edit can't silently write outside
        # the calibrated range.
        if not (4096 <= len(content_bytes) <= 16777216):
            raise FixtureError(
                f"write_trial_file: content_bytes size {len(content_bytes)} is outside "
                "the calibrated [4096, 16777216] byte range"
            )

        root_id = self.resolve_root()
        namespaced_rel_path = rel_path if rel_path.startswith("harness/") else f"harness/{rel_path}"

        status, body = self._http(
            "PUT",
            f"{self.source_host}/api/v3/oneprovider/data/{root_id}/path/{namespaced_rel_path}?create_parents=true",
            body=content_bytes,
            content_type="application/octet-stream",
        )
        if status not in (200, 201, 204):
            raise FixtureError(f"write_trial_file PUT failed status={status} body={str(body)[:200]}")

        full_path = f"/{self.space_name}/{namespaced_rel_path}"
        encoded_full = full_path.replace("/", "%2F")
        status, lookup_body = self._http("POST", f"{self.source_host}/api/v3/oneprovider/lookup-file-id/{encoded_full}")
        if status != 200 or not isinstance(lookup_body, dict) or "fileId" not in lookup_body:
            raise FixtureError(f"write_trial_file lookup failed status={status} body={str(lookup_body)[:200]}")

        return {
            "file_id": lookup_body["fileId"],
            "full_path": full_path,
            "expected_size": len(content_bytes),
        }

    def schedule_file_replication(self, file_id: str, target_provider_id: str) -> dict:
        """POST /oneprovider/transfers — schedule a replication of `file_id`
        to `target_provider_id`. Payload: `type=replication`,
        `dataSourceType=file`.

        DELIBERATE DEPARTURE from `write_trial_file`/`resolve_root`'s
        raise-on-failure convention: this method NEVER raises on a non-201
        response. Its primary caller
        (`provision.assert_transfer_path_healthy`) needs the RAW status/body
        to distinguish a transient, settle-and-retry `400
        spaceNotSupportedBy` (support has converged per
        `wait_for_space_ready` but the provider hasn't yet locally caught up
        — a related but distinct eventual-consistency gap) from a fatal
        scheduling error, and to do so without exception-message string
        parsing. Returns `{"status": int, "body": <parsed body>,
        "transfer_id": Optional[str]}` uniformly; `transfer_id` is None
        whenever `status != 201` or the body doesn't carry one.
        """
        payload = json.dumps({
            "type": "replication",
            "replicatingProviderId": target_provider_id,
            "dataSourceType": "file",
            "fileId": file_id,
        }).encode()
        status, body = self._http(
            "POST", f"{self.source_host}/api/v3/oneprovider/transfers",
            body=payload, content_type="application/json",
        )
        transfer_id = (
            body["transferId"]
            if status == 201 and isinstance(body, dict) and "transferId" in body
            else None
        )
        return {"status": status, "body": body, "transfer_id": transfer_id}

    def _distribution_is_absent(self, file_id: str) -> bool:
        """True if the fileId's distribution shows no residue anywhere (or errors, both == gone).

        For the E3 (deletion) scenario, a deleted fileId's distribution
        response may come back as an error/404-shaped body OR an all-zeros map —
        both count as "no residue."
        """
        status, body = self._http(
            "GET", f"{self.source_host}/api/v3/oneprovider/data/{file_id}/distribution", timeout=20
        )
        if status != 200 or not isinstance(body, dict):
            return True  # error/404-shaped response post-delete == gone
        dpp = body.get("distributionPerProvider") or {}
        for entry in dpp.values():
            if not isinstance(entry, dict):
                continue
            backends = entry.get("distributionPerStorageBackend", {}) or {}
            phys = sum(
                sb.get("physicalSize", 0)
                for sb in backends.values() if isinstance(sb, dict)
            )
            if phys > 0:
                return False
        return True

    def teardown_trial(self, file_id: str, timeout_s: float = 30.0, poll_interval_s: float = 2.0) -> bool:
        """DELETE the trial file, then poll (bounded ~timeout_s) until residue is gone.

        Returns True if teardown was verified absent within the timeout, else False.
        """
        try:
            self._http("DELETE", f"{self.source_host}/api/v3/oneprovider/data/{file_id}", timeout=30)
        except Exception as e:
            # DELETE errors don't necessarily mean the file is still there — proceed to poll.
            pass

        deadline = time.time() + timeout_s
        while True:
            try:
                if self._distribution_is_absent(file_id):
                    return True
            except Exception:
                return True  # a request erroring out post-delete counts as "gone"
            if time.time() >= deadline:
                return False
            time.sleep(poll_interval_s)
