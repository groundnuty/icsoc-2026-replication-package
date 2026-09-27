"""netem service-fault injection mechanism (RQ2) — the bounded LIVE path
behind sweep.LiveFaultInjector.

Mechanism: the op-worker runs as a k3s StatefulSet pod that HAS `tc` but
LACKS `NET_ADMIN`, so a plain `kubectl exec … tc qdisc {replace,del}` fails.
netem mutation is applied via a **NET_ADMIN ephemeral debug container
sharing the op-worker's netns** (`kubectl debug --profile=netadmin`),
targeting the op-worker's `eth0`. This attaches to the RUNNING pod (no
restart), and the qdisc persists on eth0 independent of the ephemeral
container, so a later `tc qdisc del` clears it. ArgoCD is structurally
blind to both the qdisc (not a k8s object) and the ephemeral container (the
`oneprovider-<p>` app manages the StatefulSet, not the Pod) — verified
against the live managed-resource list.

**Ephemeral-container lifecycle constraint:** `kubectl debug` ephemeral
containers CANNOT be removed short of a pod restart. Building a fresh
`kubectl debug` invocation for EVERY inject, clear, AND show (verification)
call would accumulate exited ephemeral containers on the op-worker pod over
a sweep (inject+clear+verify per cell x N cells), eventually stressing
`kubectl debug` itself until it hangs. Two facts avoid this:

- `tc qdisc show` is READ-ONLY and needs no NET_ADMIN at all — it runs via a
  **plain `kubectl exec`** straight into the op-worker container. `show()` /
  `verify_clean()` (called between every serial-group cell) now create ZERO
  ephemeral containers.
- Only `tc qdisc add/replace/del` need NET_ADMIN. Rather than a fresh
  ephemeral container per inject/clear, `NetemInjector` creates ONE
  persistent netadmin ephemeral container (`INJECTOR_CONTAINER`,
  `sleep 7200`) per provider on first use and REUSES it (idempotent
  `_ensure_injector`) for every subsequent inject/clear on that provider —
  `kubectl exec -c netem-injector -- tc qdisc ...` — for the life of the
  `NetemInjector` instance. Net effect: one RQ2 sweep creates AT MOST ONE
  ephemeral container per provider instead of one per inject/clear/verify.

**Sanctioned envelope (enforced HERE as code, not discipline):**
netem-only (pod-kill rejected), provider allow-list, max delay, one active
fault, auto-clear. This module NEVER hard-codes a destructive command that
escapes the envelope — every injection is validated against `NetemEnvelope`
first, and the actual command execution is an injected `runner` callable so the
whole thing is offline-testable and auditable.

Stdlib-only. No top-level side effects on import; no network at import.
"""
from __future__ import annotations

import dataclasses
import subprocess
import time
from typing import Callable, Optional, Tuple

from . import sweep


# Name of the single, reused NET_ADMIN ephemeral container that inject/clear
# exec into. Created once per provider (on first inject) and kept alive
# (`sleep 7200`) for the life of the NetemInjector instance — never
# recreated per call.
INJECTOR_CONTAINER = "netem-injector"

# The inter-provider rtransfer data port. netem is PORT-NARROWED to this so it
# stalls the transfer (data path) WITHOUT delaying de's REST/dbsync state-read
# path — this is what enables RQ2 gate b (Violated via probe_ok=true, not
# ND-via-timeout). See build_inject_cmd.
RTRANSFER_PORT = 6665

# Bounded wait/poll for the injector container to reach `running` after
# `kubectl debug` creates it. Kept small: this is a local sidecar coming up,
# not a slow external dependency.
_ENSURE_INJECTOR_MAX_POLLS = 5
_ENSURE_INJECTOR_POLL_INTERVAL_S = 1.5

# Retry budget for a transient-slow `tc qdisc del` (clear). A single clear
# exec can transiently exceed the ssh_runner's 90s timeout (an ssh/kubectl
# latency spike, not a stuck injector — a manual retry of the identical del
# typically succeeds instantly, rc=0) during a serial fault sweep. Because
# the between-cells clear-rider (`verify_clean`, called as sweep_run's
# `verify_between`) correctly aborts the serial group when it sees netem
# still present, a single transient timeout can take out most of the
# remaining cells in that serial group.
# The fix: retry the clear, using `verify_clean` (fast, read-only, reliable)
# as the authoritative success oracle rather than trusting a single del's
# rc — while still NOT raising on final failure, so a genuinely-stuck clear
# is surfaced to the rider as before (see `exec_fn`'s "clear" branch).
_CLEAR_MAX_ATTEMPTS = 3          # retry a transient-slow `tc qdisc del`
_CLEAR_RETRY_SLEEP_S = 2.0       # brief pause between clear attempts


@dataclasses.dataclass(frozen=True)
class ProviderTarget:
    """Where + how to reach one provider's op-worker for netem."""
    provider: str            # logical id (matches FaultSpec.target_provider)
    kubeconfig: str          # ABSOLUTE path on the SSH host, e.g. /path/to/kubeconfig
                             # (NOT ~/…: the cmd builders emit `KUBECONFIG=<_q(path)>` which
                             #  single-quotes the value, so a leading ~ is NOT shell-expanded on
                             #  the remote host → kubectl falls back to localhost:8080. Pass the
                             #  absolute path.)
    namespace: str           # e.g. oneprovider-de
    pod: str                 # e.g. oneprovider-0
    container: str           # the op-worker container name (kubectl debug --target)
    iface: str = "eth0"      # the shared-netns interface netem attaches to
    image: str = ""          # injector-container image with `tc` (default: reuse op-worker image)


@dataclasses.dataclass(frozen=True)
class NetemEnvelope:
    """The sanctioned bounds. LiveFaultInjector must not exceed these."""
    provider_allow: tuple = ("de",)   # only these providers are injectable
    max_delay_ms: float = 60000.0     # hard cap on netem delay
    netem_only: bool = True           # reject pod-kill / any non-netem condition


class NetemEnvelopeError(sweep.FaultInjectionError):
    """Raised when an injection request violates the sanctioned envelope."""


def _q(s: str) -> str:
    """Minimal shell-safe single-quote for values built into a remote cmd."""
    return "'" + str(s).replace("'", "'\\''") + "'"


def _image_expr(target: ProviderTarget) -> str:
    """The ephemeral-container image expression: explicit `target.image` if
    set, else a subshell that discovers the op-worker's own image via
    `kubectl get pod ... -o jsonpath` at call time (the op-worker's own image
    already has `tc`)."""
    if target.image:
        return _q(target.image)
    return (
        "$(KUBECONFIG=" + _q(target.kubeconfig) + " kubectl get pod -n " + _q(target.namespace)
        + " " + _q(target.pod) + " -o jsonpath="
        + _q("{.spec.containers[?(@.name=='" + target.container + "')].image}") + ")"
    )


def build_ensure_injector_check_cmd(target: ProviderTarget) -> str:
    """Read-only: check whether the persistent `netem-injector` ephemeral
    container already exists AND is running on the target pod. Empty output
    means absent-or-not-yet-running; non-empty means running."""
    return (
        "KUBECONFIG=" + _q(target.kubeconfig) + " kubectl get pod -n " + _q(target.namespace)
        + " " + _q(target.pod) + " -o jsonpath="
        + _q(
            "{.status.ephemeralContainerStatuses[?(@.name=='"
            + INJECTOR_CONTAINER + "')].state.running}"
        )
    )


def build_ensure_injector_create_cmd(target: ProviderTarget) -> str:
    """Create the persistent NET_ADMIN `netem-injector` ephemeral container
    (shares the op-worker's netns via `--target`). Detached — NO `-i` — so the
    command returns once the container is created; the container itself then
    just `sleep`s, staying available for later `kubectl exec` calls."""
    img = _image_expr(target)
    return (
        "KUBECONFIG=" + _q(target.kubeconfig) + " kubectl debug -n " + _q(target.namespace)
        + " " + _q(target.pod) + " --target=" + _q(target.container)
        + " --profile=netadmin --container=" + _q(INJECTOR_CONTAINER)
        + " --image=" + img + " -- sleep 7200"
    )


def build_inject_cmd(target: ProviderTarget, delay_ms: float) -> str:
    """Remote shell command that applies a PORT-NARROWED netem delay to the
    op-worker's eth0 by exec'ing into the already-running persistent
    `netem-injector` sidecar (shares the op-worker's netns, HAS NET_ADMIN).
    Caller must have already called `NetemInjector._ensure_injector`.

    **Port-narrowed to RTRANSFER_PORT (6665) — necessary for RQ2
    validity.** A whole-interface `netem delay` on eth0 delays ALL de traffic
    including the verifier's state-read path → distribution probes time out
    (`probe_ok=false`) → the cell comes back NotDetermined, an environment
    artefact, NOT a service Violated. Delaying ONLY :6665 (the rtransfer
    data path) stalls the transfer (target physicalSize stays short →
    Violated) while leaving de's REST/dbsync clear so the verifier still
    reads `probe_ok=true` samples of the stalled value → a genuine
    **Violated via probe_ok=true** (RQ2 gate b). A `prio` root qdisc + a
    `netem` band + u32 src/dst-port filters; deletes any existing root
    qdisc first."""
    ms = int(round(delay_ms))
    iface = target.iface
    tc_seq = (
        f"tc qdisc del dev {iface} root 2>/dev/null; "
        f"tc qdisc add dev {iface} root handle 1: prio && "
        f"tc qdisc add dev {iface} parent 1:3 handle 30: netem delay {ms}ms && "
        f"tc filter add dev {iface} protocol ip parent 1:0 prio 1 u32 match ip dport {RTRANSFER_PORT} 0xffff flowid 1:3 && "
        f"tc filter add dev {iface} protocol ip parent 1:0 prio 1 u32 match ip sport {RTRANSFER_PORT} 0xffff flowid 1:3"
    )
    return (
        "KUBECONFIG=" + _q(target.kubeconfig) + " kubectl exec -n " + _q(target.namespace)
        + " " + _q(target.pod) + " -c " + _q(INJECTOR_CONTAINER)
        + " -- sh -c " + _q(tc_seq)
    )


def build_clear_cmd(target: ProviderTarget) -> str:
    """Remote shell command that removes netem from the op-worker's eth0 by
    exec'ing into the persistent `netem-injector` sidecar. Idempotent-safe: a
    `del` with no qdisc present errors harmlessly (the caller treats a clear
    as best-effort)."""
    return (
        "KUBECONFIG=" + _q(target.kubeconfig) + " kubectl exec -n " + _q(target.namespace)
        + " " + _q(target.pod) + " -c " + _q(INJECTOR_CONTAINER)
        + " -- tc qdisc del dev " + _q(target.iface) + " root"
    )


def build_show_cmd(target: ProviderTarget) -> str:
    """Read-only: show the current qdisc on the op-worker eth0 (for verifying
    inject/clear took effect). `tc qdisc show` needs no NET_ADMIN, so this is
    a PLAIN `kubectl exec` straight into the op-worker container — no
    ephemeral debug container at all. This is what `show()` and
    `verify_clean()` call, so verification between cells creates zero
    containers."""
    return (
        "KUBECONFIG=" + _q(target.kubeconfig) + " kubectl exec -n " + _q(target.namespace)
        + " " + _q(target.pod) + " -c " + _q(target.container)
        + " -- tc qdisc show dev " + _q(target.iface)
    )


def ssh_runner(ssh_host: str, timeout_s: int = 90) -> Callable:
    """Default runner: run a remote shell command over SSH, return
    (rc, stdout, stderr). Kept separate + injectable so tests never touch SSH."""
    def _run(remote_cmd: str):
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", ssh_host, remote_cmd],
            capture_output=True, text=True, timeout=timeout_s,
        )
        return proc.returncode, proc.stdout, proc.stderr
    return _run


class NetemInjector:
    """Builds sweep.LiveFaultInjector's `exec_fn` from a runner + per-provider
    targets + the sanctioned envelope. Validates EVERY request against the
    envelope before emitting any command."""

    def __init__(
        self,
        runner: Callable,
        targets: dict,
        envelope: Optional[NetemEnvelope] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
    ):
        self._run = runner
        self._targets = targets  # {provider: ProviderTarget}
        self._envelope = envelope or NetemEnvelope()
        self._sleep = sleep_fn or time.sleep

    def _validate(self, fault_spec) -> ProviderTarget:
        env = self._envelope
        if env.netem_only and fault_spec.condition != sweep.FAULT_NETEM_LAG:
            raise NetemEnvelopeError(
                f"envelope is netem-only; condition {fault_spec.condition!r} (e.g. pod-kill) is refused"
            )
        p = fault_spec.target_provider
        if p not in env.provider_allow:
            raise NetemEnvelopeError(
                f"provider {p!r} not in sanctioned allow-list {env.provider_allow!r}"
            )
        if p not in self._targets:
            raise NetemEnvelopeError(f"no ProviderTarget configured for {p!r}")
        delay = fault_spec.netem_delay_ms
        if delay is None or delay <= 0:
            raise NetemEnvelopeError("netem_lag requires a positive netem_delay_ms")
        if delay > env.max_delay_ms:
            raise NetemEnvelopeError(
                f"delay {delay}ms exceeds sanctioned max {env.max_delay_ms}ms"
            )
        return self._targets[p]

    def _ensure_injector(self, target: ProviderTarget) -> None:
        """Idempotently ensure the persistent NET_ADMIN `netem-injector`
        ephemeral container is running on `target`'s pod — create it on first
        use, reuse it on every later call. This avoids ephemeral-container
        accumulation: a fresh `kubectl debug` per inject would otherwise
        leave an unremovable exited container behind every time; instead,
        at most one is ever created per provider."""
        check_cmd = build_ensure_injector_check_cmd(target)
        rc, out, _err = self._run(check_cmd)
        if rc == 0 and out.strip():
            return  # already running — reuse it, no create needed

        rc, _out, err = self._run(build_ensure_injector_create_cmd(target))
        if rc != 0:
            raise NetemEnvelopeError(
                f"failed to create persistent netem-injector container on {target.provider!r}: "
                f"rc={rc}: {err.strip()[:300]}"
            )

        for _attempt in range(_ENSURE_INJECTOR_MAX_POLLS):
            rc, out, _err = self._run(check_cmd)
            if rc == 0 and out.strip():
                return
            self._sleep(_ENSURE_INJECTOR_POLL_INTERVAL_S)

        raise NetemEnvelopeError(
            f"netem-injector container did not reach running state on {target.provider!r} "
            f"after {_ENSURE_INJECTOR_MAX_POLLS} polls"
        )

    def exec_fn(self, action: str, fault_spec) -> dict:
        """The callable sweep.LiveFaultInjector(exec_fn=...) invokes. `action`
        is "inject" or "clear". Returns {rc, stdout, stderr, cmd, ...}. A
        failed inject raises (fail-loud); a failed clear is returned but NOT
        raised (clear is best-effort — the watchdog + any pod restart also
        clear).

        `clear` additionally retries up to `_CLEAR_MAX_ATTEMPTS` times,
        using `verify_clean` (fast, read-only `tc qdisc show`) as the
        authoritative success oracle rather than trusting a single `tc
        qdisc del`'s rc — a transient ssh/kubectl latency spike on ONE del
        must not look like a stuck qdisc to the caller. The result dict
        carries `attempts` (how many del attempts were made) and
        `verified_clean` (whether `verify_clean` confirmed clean before
        attempts were exhausted); a genuinely-stuck clear still returns
        `verified_clean: False` without raising, exactly as before this
        retry was added — the between-cells clear-rider (`verify_between`)
        remains the backstop that aborts a serial fault group on a real
        stuck qdisc."""
        target = self._validate(fault_spec)
        if action == "inject":
            self._ensure_injector(target)
            cmd = build_inject_cmd(target, fault_spec.netem_delay_ms)
            rc, out, err = self._run(cmd)
            if rc != 0:
                raise NetemEnvelopeError(f"netem inject failed rc={rc}: {err.strip()[:300]}")
            # Per-cell inject-verification: the add's rc==0 is NOT proof
            # netem actually took effect — the qdisc state itself must be
            # asserted via a READ-ONLY `tc qdisc show`, never by trusting
            # the mutating call's exit code alone. `build_show_cmd` needs no
            # NET_ADMIN (plain `kubectl exec`), so this adds no new
            # ephemeral-container churn.
            show_rc, show_out, show_err = self._run(build_show_cmd(target))
            inject_verified = bool(show_rc == 0 and "netem" in show_out)
            if not inject_verified:
                # FAIL-LOUD: rc==0 on the add but the show does not confirm
                # netem is applied is a silent non-apply — a real failure
                # this gate exists to catch, never passed through as a
                # false "verified" success.
                raise NetemEnvelopeError(
                    f"netem inject for {target.provider!r} returned rc=0 but the post-inject "
                    f"verify (tc qdisc show) does not confirm netem is applied — a silent "
                    f"non-apply. show_rc={show_rc} show_out={show_out.strip()[:300]!r} "
                    f"show_err={show_err.strip()[:300]!r}"
                )
            return {
                "action": "inject",
                "rc": rc,
                "stdout": out,
                "stderr": err,
                "cmd": cmd,
                "inject_verified": inject_verified,
                "qdisc_snapshot": show_out,
            }
        if action == "clear":
            cmd = build_clear_cmd(target)
            rc, out, err = "", "", ""
            for attempt in range(1, _CLEAR_MAX_ATTEMPTS + 1):
                # ssh_runner._run is subprocess.run(timeout=...) with NO
                # try/except — on a timeout it RAISES subprocess.TimeoutExpired
                # (NOT rc!=0), and ssh transport failures raise OSError. That
                # ssh-layer timeout is exactly the failure mode this retry
                # exists to survive, so it must be caught HERE and treated as
                # a failed attempt — else the timeout would propagate
                # straight out of exec_fn and the retry would never engage.
                try:
                    rc, out, err = self._run(cmd)
                except subprocess.TimeoutExpired as e:
                    rc, out, err = None, "", f"ssh timeout after {e.timeout}s"
                except OSError as e:  # ssh transport failure (conn reset, etc.)
                    rc, out, err = None, "", f"ssh transport error: {type(e).__name__}: {e}"
                if self.verify_clean(fault_spec.target_provider):
                    return {
                        "action": "clear", "rc": rc, "stdout": out, "stderr": err,
                        "cmd": cmd, "attempts": attempt, "verified_clean": True,
                    }
                if attempt < _CLEAR_MAX_ATTEMPTS:
                    self._sleep(_CLEAR_RETRY_SLEEP_S)
            # Exhausted retries without a clean verify — still best-effort:
            # do NOT raise. A genuinely-stuck clear is surfaced via this
            # `verified_clean: False` result; sweep_run's clear-rider
            # (`verify_between`) is the backstop that aborts the serial
            # group on a real stuck qdisc, same as before this fix.
            return {
                "action": "clear", "rc": rc, "stdout": out, "stderr": err,
                "cmd": cmd, "attempts": _CLEAR_MAX_ATTEMPTS, "verified_clean": False,
            }
        raise NetemEnvelopeError(f"unknown action {action!r} (expected inject|clear)")

    def show(self, provider: str) -> Tuple:
        """Read-only qdisc inspection for a configured provider (verification).
        Plain `kubectl exec` — creates no ephemeral container."""
        target = self._targets[provider]
        return self._run(build_show_cmd(target))

    def verify_clean(self, provider: str) -> bool:
        """Clear-verification rider (used as sweep_run.py's
        `verify_between`): confirm the provider's qdisc has returned to clean
        (no netem) between serial-group cells, so a fault cell that ERRORS
        mid-inject/clear can't silently leave a lingering netem qdisc for the
        NEXT cell on the same provider. Fail-closed: any ambiguity (runner
        error/exception, non-zero rc) is treated as NOT clean — unconfirmed
        clean is treated as dirty."""
        try:
            target = self._targets[provider]
            rc, out, _err = self._run(build_show_cmd(target))
        except Exception:  # noqa: BLE001 — fail-closed: unverifiable == dirty
            return False
        if rc != 0:
            return False
        return "netem" not in out
