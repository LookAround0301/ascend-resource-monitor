# Ascend Resource Monitor

Reusable monitoring skill for Linux Ascend NPU servers. The recommended default is a local one-click SSH-tunnel launcher, generated after the Windows deployment wizard verifies the environment. Direct browser access is optional.

[中文](README.md) · [Windows wizard](https://github.com/LookAround0301/ascend-resource-monitor/releases/latest/download/ascend-monitor-setup.zip) · [Skill ZIP](https://github.com/LookAround0301/ascend-resource-monitor/releases/latest/download/ascend-resource-monitor-skill.zip)

See the [monitoring guide](skills/ascend-resource-monitor/references/monitoring-guide.md) for deployment/configuration and the [queue reference](skills/ascend-resource-monitor/references/task-queue.md) for detailed reservation rules. This README was checked against `main` on **2026-11-05**. Releases describe their tagged source; existing services and downloaded launchers do not update automatically when main changes.

Current main includes 16 logical cards per A3 node, isolated A5/A3 personal sessions, passwordless employee-ID/name login, shared-user name display, and an administrator container-guard switch.

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

## Unified reservations: A5 eight-card and A3 sixteen-card nodes

The unified queue supports **exactly two equal-capacity nodes**. Enable `task_queue.unified_queue: true` in the central proxy configuration. Capacity is selected by `task_queue.cards_per_node`, not automatically inferred from hardware.

| Configuration/behavior | A5 default | A3 logical16 |
| --- | --- | --- |
| `cards_per_node` | Omitted or `8` | Explicitly `16` |
| IDs per node / total pool | 0–7 / 16 cards | 0–15 / 32 cards |
| Single-node request | 1–8 cards | 1–16 cards |
| Dual-node total, split equally | 2 / 4 / 8 / 16 cards | 2 / 4 / 8 / 16 / 32 cards |
| Groups per node | Two groups of four | Two groups of eight |

Choose any available single node, a fixed node, or both nodes. A fixed-node request never falls back to the other node. Homepage card tiles represent reservations; NPU metrics and process tables show actual use. A5 allocation behavior and legacy requests remain compatible. A3 reservation matching, process card IDs and night completion use IDs 0–15. An existing A3 layout that paired two logical IDs into one slot requires the [explicit migration](skills/ascend-resource-monitor/references/task-queue.md#card-capacity-and-a3-migration); changing capacity alone is insufficient, and that migration must not be run on A5.

### Sign in and use the queue

1. Sign in on the homepage with an **employee account ID and name**, such as `a12345678` and its corresponding name. The ID is one letter followed by eight digits. Unified mode no longer asks for a personal password: first login creates the account, and an existing ID must match its registered name. This is self-declared identity, not Grafana administrator access or personnel allowlisting.
2. Enable and test desktop notifications, then select machine, cards, normal/night mode and duration. The form defaults to 30 minutes in five-minute increments; each account may have at most five unfinished requests.
3. For normal work, click Start use within the displayed claim window after allocation, then launch the workload yourself. Night work becomes active registration when its scheduled time arrives and resources are allocated, without a claim step; actual workload startup remains external.
4. Stop the workload yourself, then confirm Finish registration. Cancellation and finish require confirmation; Start use acts directly.

The same browser can sign into A5 and A3 with the same employee ID/name concurrently. Personal sessions are scoped by deployment database and entry host/port, so one deployment's login/logout does not overwrite the other; proxy restarts preserve sessions. Refresh tabs after upgrade and sign in once if an old login was already overwritten. Existing Windows launchers remain usable. **This isolates personal queue sessions; Grafana administrator authentication is managed separately.** See [concurrent logins](skills/ascend-resource-monitor/references/task-queue.md#concurrent-logins-to-multiple-deployments).

### Scheduling, night work and notifications

- **Normal requests** last at most two hours and must start and finish by 09:00 the next calendar day in the configured timezone. Requests unable to start before the next 02:00 become standby without reserving capacity ahead of night work; releases before 02:00 can make them eligible again.
- **Claim windows** are normally ten minutes, shortened by an earlier scheduling deadline. Reminders occur on allocation and each minute while awaiting claim. Timeout releases that allocation and moves the request back one position, preserving application time. Offer IDs, order and deadlines persist; stale claim clicks are rejected.
- **Night requests** select a future start from 20:00 until before 09:00 and last at most thirteen hours; an estimated finish past 09:00 is warned about. Night reservations can follow without a release buffer. An expired active night registration may hand off when its successor's calculated start is due, without waiting for processes to exit. Scheduling and handoff never launch, stop or preempt workloads.
- **Automatic night finish** uses `monitor_processes`: before 09:00, at least three fresh, complete inventories over at least twenty seconds must show no NPU process on any allocated card. Finishing before the predicted end also requires previously observed use by the owner or an allowed user. Unknown card IDs, stale or incomplete data cannot establish an empty allocation. Morning carryovers remain manually finishable.
- **Priority and overtime**: owners can move their queued/ready requests down one position within the same task type. Administrators can move queued requests to the top, up or down without preempting active registrations. Applicable normal bottlenecks receive 10/5-minute advance reminders. Overdue blockers are reminded every five minutes, other overdue owners every fifteen; after five minutes, the nearest directly affected waiters are notified.
- **Daily 09:00 reset**, explicitly enabled with `daily_reset`, clears normal registrations including active ones, cancels unstarted requests from the elapsed night, and preserves active night registrations as morning carryovers. Accounts and workloads remain intact; this does not delete every night record.
- **Notification delivery** requires an open, signed-in page and browser/OS permission. Verification is saved per browser and account and reused on reload; revoked permission or delivery failure requires re-verification. Server-side allocation and claim expiry do not depend on an open page.

### Shared users and actual-use checks

The owner can add up to 32 shared users to a queued, ready or active reservation, search by name or employee ID, and see chips labeled with both. Additions must agree across both node rosters and are rechecked on submission. Shared users match only the reservation's allocated nodes and cards; this grants neither reservation-management access nor changes to personnel allowlists or guard policy.

Optional `monitor_processes` compares current NPU processes with active registrations by employee, node and card. Unregistered use notifies the person and affected ready/active reservation owners; excess registered use overlapping another active allocation notifies its affected owner. Unknown identity/cards or stale collection remain unverified. The unified queue records resource arrangements; it neither executes workload commands nor provides exclusive hardware access. Registration checks never stop containers. See the [queue reference](skills/ascend-resource-monitor/references/task-queue.md) for all conditions.

## Administrators, personnel and container control

| Role/feature | Operations | Access |
| --- | --- | --- |
| Viewer | Resources, NPU processes and reservation state | Deployment's Viewer policy |
| Personal queue account | Create/claim reservations, finish own records, defer own waiting requests and manage own shared users | Employee-ID/name login; notification verification before enqueue |
| Grafana administrator | Reorder waiting requests, manage personnel, use provisioned Stop and guard switches | Backend-verified Grafana Admin session; employee identity alone grants no admin role |

Optional [administrator Stop](skills/ascend-resource-monitor/references/container-control.md) opens a confirmation page from the process summary. The backend rechecks current NPU processes, full container ID, start time and protection rules, writes an audit record, and stops the **entire container**. Docker allows up to thirty seconds for graceful exit before possible forced termination. Deploy this controlled backend separately; the base Windows wizard remains read-only. Process/container stopping in the older per-node queue has a [separate workflow](skills/ascend-resource-monitor/references/task-queue.md#legacy-per-node-reservations-and-stop); unified Finish registration does not invoke it. The monitor API offers no arbitrary server-command execution.

**Current main UI limitation:** with unified mode enabled, the original Stop confirmation link returns to the homepage; there is no built-in unified-queue stop-confirmation page. The manual workflows above apply to standalone container control or legacy per-node queues. Deployments with a separately integrated administrator control page must verify that entry independently; an administrator login alone does not establish that browser Stop works.

The [personnel allowlist](skills/ascend-resource-monitor/references/personnel-roster.md) is searchable and readable/editable only by Grafana administrators, with confirmed changes synchronized to both hosts. Identical retries are idempotent, conflicting names are rejected, and partial failures identify the affected nodes. Adding a person exempts qualifying containers from automatic interception; removal warns of renewed enforcement, and the last roster entry cannot be removed. Personal users can look up matching people through the shared-user search, not manage the full roster.

The [persistent container guard](skills/ascend-resource-monitor/references/container-guard.md) starts disabled. An administrator confirms the automatic-stop switch on the personnel page; actual per-node state is displayed, persisted and preserved across upgrades. Enabling evaluates current NPU containers with a clear employee ID missing from the name roster, marking and stopping eligible containers. Once marked, the same container can be intercepted on future starts even without current NPU use. Personnel/container exemptions take precedence while retaining marks; removing an exemption can restore enforcement. Invalid rosters, ambiguous identity, stale inventory or protected containers do not create new marks. Disabling stops automatic enforcement without clearing marks or restarting stopped containers. This policy is separate from registration checks and manual Stop.

## Access: local launcher recommended, direct browser optional

**Start with `Open-Monitor.cmd`**. Connect to the company network / VPN, extract the complete package, then double-click the launcher to establish an SSH tunnel and open the dashboard. Keep its terminal open. This requires SSH connectivity and login permission from the client PC; it does not require direct access to the server's HTTPS port. `Install.cmd` creates a desktop shortcut.

| Access mode | Local one-click launcher (recommended) | Direct browser entry (optional) |
| --- | --- | --- |
| Example address | `http://127.0.0.1:13000` | `https://192.0.2.10:8443` |
| Each PC | Run the launcher and keep the SSH tunnel open | Connect to the company network / VPN and open the page |
| Accounts | SSH credentials; personal login for reservations, Grafana login for administration | Direct viewing when anonymous Viewer is enabled; separate reservation/admin logins |
| Constraints | SSH network access and login permission | Corporate proxy, port access and certificate policies |

If a [shared HTTPS entry](skills/ascend-resource-monitor/references/web-entry.md) has already been deployed and verified, also provide `Open-Web.cmd` so users can try direct browser access. Both entry points use the same monitoring service. If direct access fails with `ERR_TUNNEL_CONNECTION_FAILED`, a timeout or certificate policy, use the local launcher first instead of requiring each user to change corporate proxy or certificate settings.

Configure the HTTPS interface and certificate separately; Grafana and Prometheus continue to listen on loopback. Prefer a company-trusted certificate. Success on one PC does not prove that other PCs have the same routing or browser policies. The shared entry runs independently of the operator's PC, but requires the server, network and browser to allow access. The default Windows wizard continues to deliver the local launcher.

With the current repository's Skill, ask:

```text
Deploy monitoring and generate a Windows local one-click launcher as the recommended entry.
If a verified HTTPS entry already exists, include Open-Web.cmd as an optional alternative.
Keep ordinary users read-only; require administrator login and confirmation when Stop is enabled.
```

Give concurrent deployments distinct `id` values and client `local_port` values, such as `13000` and `13001`, and run each deployment's access package. A different port alone does not isolate legacy same-name cookies; use proxy code containing the personal-session fix.

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

Two nodes with host, NPU and process exporters normally produce six targets, or seven with queue metrics; verify the configured panels and topology. Tests cover NPU-only selection and attribution, configuration/archive boundaries, reinstall behavior, credentials and Stop permissions, A5 compatibility, A3 logical16 planning/migration, shared users, night work, notifications, guard switches and concurrent sessions. Windows checks cover PowerShell syntax, wizard construction and credential ACLs. POSIX-only checks require Linux; skips are not passes. **Existing-deployment checks do not establish fresh-server installation acceptance; full downloads through a restricted proxy still need target-environment verification.**

## Upgrade an existing deployment

1. Back up configuration, running code, SQLite through a consistent backup, rosters, certificates and guard state. Record the paths and versions actually used by services.
2. Install matching proxy/runtime modules while preserving accounts, reservations, session namespace, administrator credentials and guard policy. A5 keeps the eight-card default. Switching an old paired-eight A3 layout requires the [explicit migration](skills/ascend-resource-monitor/references/task-queue.md#card-capacity-and-a3-migration); a stored capacity mismatch refuses startup.
3. Restart only the affected monitoring components. Verify scrape targets, process freshness, card IDs, preserved reservations and permissions, then refresh browser tabs. An old A3 page submitting missing/wrong capacity is asked to refresh instead of silently creating a different-sized reservation.
4. Existing SSH launchers work with the personal-session fix. Regenerate packages when entry/client configuration changes. Verify server runtime, client package and repository-main versions separately.

## Develop and package

```sh
python -m pip install aiohttp tzdata
PYTHONUTF8=1 python tools/check.py
python tools/build_release.py --output dist
```

Use Python 3.9+ for development checks. Full HTTP/session tests require `aiohttp`; environments without system timezone data, including Windows, also need `tzdata`. Skipped HTTP checks are not validation. UTF-8 mode handles Chinese scripts and child-process output; in PowerShell, set `$env:PYTHONUTF8 = '1'` before running `python tools/check.py`. Windows also checks PowerShell syntax, wizard construction and credential permissions. `dist/` contains the generic wizard, Skill ZIP and `SHA256SUMS.txt`; generated outputs are ignored by Git. The repository provides `.pre-commit-config.yaml`; with `pre-commit` installed, `pre-commit run --all-files` runs the same checks.

The skill lives in `skills/ascend-resource-monitor/`; `assets/runtime/` contains Linux code, `assets/windows/` the Windows tools, `references/` the operational guidance, and `scripts/` packaging/tests. `examples/` contains documentation-only addresses. `tools/` holds repository checks and release packaging.

See [deployment guidance](skills/ascend-resource-monitor/references/deployment.md) and [Windows tooling](skills/ascend-resource-monitor/references/windows.md). Installation checks existing ownership and does not replace another deployment's services or modify SSH keys, drivers or application containers.
