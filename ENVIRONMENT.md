# Experiment environment

The environment is documented here; it is not shipped.

## Storage system

| Component | Version |
|---|---|
| Onedata | 25.0 (the version the services report) |
| Kubernetes | k3s `v1.34.1+k3s1` |
| Helm charts | `https://onedata.github.io/charts`: `onezone 0.2.20-rc2`, `oneprovider 0.2.20-rc7` |

**Topology used by the trials:** one Onezone, and two storage sites, each running a
Oneprovider with a POSIX storage backend.

- **Source site:** holds each trial's file at the start. The agent's tool server and the
  verifier both talk to this site's REST API.
- **Target site:** where placement must converge.

Deletion trials use the same two sites; the file is present on both at the start.

## Agent tools

| Component | Version |
|---|---|
| onedata-mcp (MCP tool server) | commit `79739257367791e86f13dbc0bc9e7ec72917c8c8` |
| MCP Python SDK | 1.28.1 (protocol `2025-11-25`) |

## Trial parameters

| Parameter | Value |
|---|---|
| Deadline (T_max) | 30 s |
| Verifier poll interval | 3 s |
| Verifier observation window | to 45 s after trial start |
| Placement file size | 4096 bytes |

## Injected fault

- **What:** a `netem` delay of **35 s** on the target site's inter-site data-transfer
  traffic, applied at trial start.
- **Checked:** its presence is confirmed from the traffic-control state before the agent
  starts. Each faulted recording stores that snapshot in `fault.qdisc_snapshot`.
- **Cleared:** at 45 s in ungated trials. In gated trials it is cleared when the trial's
  last gate cycle ends. Each recording stores the clearing time in `fault.cleared_at_rel_s`.

## Verifier host

| | |
|---|---|
| CPU | AMD Opteron 62xx class CPU, 16 cores |
| Python | 3.14.6 (overhead benchmark and `make test`) |
