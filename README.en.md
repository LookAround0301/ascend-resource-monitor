# Ascend Resource Monitor

Reusable monitoring skill for Linux Ascend NPU servers. The recommended default is a local one-click SSH-tunnel launcher, generated after the Windows deployment wizard verifies the environment. Direct browser access is optional.

[中文](README.md) · [Windows wizard](https://github.com/LookAround0301/ascend-resource-monitor/releases/latest/download/ascend-monitor-setup.zip) · [Skill ZIP](https://github.com/LookAround0301/ascend-resource-monitor/releases/latest/download/ascend-resource-monitor-skill.zip)

The personnel allowlist shows searchable employee IDs and names. Only a verified Grafana administrator can view it and confirm additions or removals on both host rosters. Identical retries are idempotent, conflicting names are rejected, and existing marks are retained while exempt containers disappear from the active-interception table. See [personnel roster management](skills/ascend-resource-monitor/references/personnel-roster.md).

See the [current capabilities, deployment and improvements guide](skills/ascend-resource-monitor/references/monitoring-guide.md) for the complete configuration and upgrade workflow.

## Features

- Grafana/Prometheus dashboards for host CPU, RAM, disks, shared storage, networking, and per-device NPU utilization, HBM, temperature and power.
- **NPU processes only**: PID, OS user, employee-identity evidence, container, working directory and host mount mapping.
- Unknown/conflicting identities are explicit. A shared root account or matching directory does not prove who started a workload.
- Multi-node filtering, 1/5/10/30-second page refresh with a one-second default, normally five-second resource scraping and ten-second process sampling.
- NPU process summaries by employee, directory, node and container, with host mount mapping.
- Current NPU process duration starts at the group's earliest running process. Once all processes exit, the row disappears; a later process starts a new duration without carrying over the previous run.
- The summary replaces RSS with job origin: declared agent/manual, suspected agent, unknown, or mixed/conflicting. Missing agent evidence never implies manual execution. See [origin markers](skills/ascend-resource-monitor/references/job-origin.md).
- A bounded worker reads container scripts and records linked to current NPU jobs, including literal nested launch commands. Records must match a process lifetime; polling alone never proves agent origin. Script contents and sensitive arguments are not published.
- Optional names from a user-supplied roster, matched by account ID and blank when unmatched; rosters remain in private deployment configuration.
- Administrator passwords stay in a protected local operator record, never server configuration; reinstalls preserve existing accounts.

Optional [administrator-only Stop controls](skills/ascend-resource-monitor/references/container-control.md) add Stop links, an authenticated confirmation page and per-session login status. The backend verifies current NPU processes, full container ID and unchanged start time, then audits the stop. This stops the entire selected container with a 30-second graceful timeout. The control backend is deployed separately; the default Windows deployment remains read-only.

Optional [persistent container guard](skills/ascend-resource-monitor/references/container-guard.md): explicitly opt in to stopping NPU containers with an employee ID missing from the supplied name roster. Marks survive restarts until an administrator adds the container to the whitelist. Disabled by default; invalid or stale input pauses new marking.

Optional [unified reservations](skills/ascend-resource-monitor/references/monitoring-guide.md#unified-queue-rules) combine two eight-card nodes. Choose any available single node, a specific node or both nodes. Single-node requests use 1–8 cards; dual-node requests use 2/4/8/16 total cards split equally. Duration uses hours plus five-minute choices; normal tasks are at most two hours, and new requests must start and finish by 09:00 the next calendar day.

Claim an allocation directly within ten minutes, without an extra dialog. Desktop reminders occur at allocation and after 2/4/6/8/9 minutes. An unclaimed allocation is released and the request moves back one position, retaining its application time. Order, deadlines and offer IDs persist across reloads and proxy restarts. Application time remains visible after claiming.

Stop the workload yourself before confirming Finish registration. The unified queue records and allocates reservations; it does not start or stop workloads or provide hardware isolation. The older per-node queues and browser Stop have a [separate reference](skills/ascend-resource-monitor/references/task-queue.md); do not apply their stopping workflow to unified reservations.

## Access: local launcher recommended, direct browser optional

**Start with `Open-Monitor.cmd`**. Connect to the company network / VPN, extract the complete package, then double-click the launcher to establish an SSH tunnel and open the dashboard. Keep its terminal open. This requires SSH connectivity and login permission from the client PC; it does not require direct access to the server's HTTPS port. `Install.cmd` creates a desktop shortcut.

| Access mode | Local one-click launcher (recommended) | Direct browser entry (optional) |
| --- | --- | --- |
| Example address | `http://127.0.0.1:13000` | `https://192.0.2.10:8443` |
| Each PC | Run the launcher and keep the SSH tunnel open | Connect to the company network / VPN and open the page |
| Accounts | SSH credentials; separate admin login for Stop | Anonymous viewing; admin login for Stop |
| Constraints | SSH network access and login permission | Corporate proxy, port access and certificate policies |

If a [shared HTTPS entry](skills/ascend-resource-monitor/references/web-entry.md) has already been deployed and verified, also provide `Open-Web.cmd` so users can try direct browser access. Both entry points use the same monitoring service. If direct access fails with `ERR_TUNNEL_CONNECTION_FAILED`, a timeout or certificate policy, use the local launcher first instead of requiring each user to change corporate proxy or certificate settings.

Configure the HTTPS interface and certificate separately; Grafana and Prometheus continue to listen on loopback. Prefer a company-trusted certificate. Success on one PC does not prove that other PCs have the same routing or browser policies. The shared entry runs independently of the operator's PC, but requires the server, network and browser to allow access. The default Windows wizard continues to deliver the local launcher.

With the current repository's Skill, ask:

```text
Deploy monitoring and generate a Windows local one-click launcher as the recommended entry.
If a verified HTTPS entry already exists, include Open-Web.cmd as an optional alternative.
Keep ordinary users read-only; require administrator login and confirmation when Stop is enabled.
```

This document describes current source. Release packages follow their tagged version; previously downloaded access packages do not update themselves.

Enable `task_queue.unified_queue: true` in the existing proxy to show the homepage reservation region. The base Windows wizard does not automatically enable queues, allowlist writes or the container guard; follow the [extension deployment steps](skills/ascend-resource-monitor/references/monitoring-guide.md#unified-queue-allowlist-and-guard).

## Install as a skill

Ask a skill-capable agent to install this repository's `skills/ascend-resource-monitor` directory, or extract the [Skill ZIP](https://github.com/LookAround0301/ascend-resource-monitor/releases/latest/download/ascend-resource-monitor-skill.zip) into the client's skill directory. Invoke `$ascend-resource-monitor` and supply the target servers when asked. The workflow gathers necessary inputs, checks the environment, deploys and verifies monitoring, then produces a Windows client installer.

## Windows wizard

1. Extract [ascend-monitor-setup.zip](https://github.com/LookAround0301/ascend-resource-monitor/releases/latest/download/ascend-monitor-setup.zip), then run `Setup.cmd`.
2. Enter SSH endpoints/users/ports, reachable monitoring IPv4 addresses and the central monitoring node.
3. Enter an executor identifier, an existing mounted data directory and ports. An SSH identity file or offline package directory is optional.
4. Check the environment, review hardware compatibility, then start deployment.
5. Successful verification generates `client-<deployment>.zip`. Run its `Install.cmd` to install a desktop launcher.

Windows requires PowerShell 5.1 and OpenSSH Client, **not Python**. Servers require Python 3.9+, systemd, working NPU drivers and root or noninteractive sudo. Connect the appropriate VPN/network first. SSH passwords are entered in OpenSSH's terminal and are not saved. Grafana admin credentials are generated locally and kept in `%LOCALAPPDATA%\AscendMonitor\credentials\<id>.json`, accessible only to that Windows user and SYSTEM. Existing credentials are reused or requested with hidden input, never reset during installation. Initialization receives the password through stdin; no server configuration or client ZIP contains it. See [credential hardening](skills/ascend-resource-monitor/references/credentials.md). Private keys remain on the current PC. Grafana/Prometheus listen on server loopback; each PC establishes its own tunnel and administrator session. Server services start on reboot; reopen the launcher after a client or network disconnect.

## Profile and size

The default profile is **Linux ARM64**, based on an Ascend 950DT / openEuler 24.03 deployment: NPU Exporter 26.2.0, Prometheus 3.15.1, Grafana 13.2.4, with node_exporter 1.12.1 when a host exporter needs installation. Review compatibility and replace `software.json` for other hardware, systems or architectures.

The Skill and Windows wizard mainly contain lightweight text and scripts; see build outputs and Release assets for exact sizes. Monitoring binaries, model weights, passwords, private keys and real machine configurations are excluded. Deployment downloads and SHA-256-verifies binaries separately (roughly 550 MB for a full installation), or uses an offline package directory.

The base monitor has been verified with 20 panels and six targets across two nodes. This iteration verified seven targets with queue metrics, plus the unified queue, allowlist and active-interception UI in an existing deployment. Local tests cover NPU-only selection, attribution, configuration, archive boundaries, install/reinstall behavior, credentials and Stop authorization. Windows syntax, forms and credential ACLs are checked; an isolated real Grafana instance validates bootstrap and login. **A complete installation on a fresh server and a full download through the restricted proxy have not been accepted end to end.**

## Develop and package

```sh
python tools/check.py
python tools/build_release.py --output dist
```

Checks use the Python standard library. On Windows they also check PowerShell syntax and construct the wizard. `dist/` contains the generic wizard, Skill ZIP and `SHA256SUMS.txt`; generated outputs are ignored by Git.

The skill lives in `skills/ascend-resource-monitor/`; `assets/runtime/` contains Linux code, `assets/windows/` the Windows tools, `references/` the operational guidance, and `scripts/` packaging/tests. `examples/` contains documentation-only addresses. `tools/` holds repository checks and release packaging.

See [deployment guidance](skills/ascend-resource-monitor/references/deployment.md) and [Windows tooling](skills/ascend-resource-monitor/references/windows.md). Installation checks existing ownership and does not replace another deployment's services or modify SSH keys, drivers or application containers.
