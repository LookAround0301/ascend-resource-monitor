# Monitoring: capabilities, deployment and improvements

[Repository](https://github.com/LookAround0301/ascend-resource-monitor)

This guide describes the repository source as of 2026-11-04. Basic monitoring can run independently. The unified queue, personnel allowlist and persistent guard are separately enabled extensions. All configuration below is illustrative; real endpoints, rosters and credentials remain private.

## 1. Current capabilities

| Area | Behavior |
| --- | --- |
| Resource monitoring | Host CPU, RAM, disks, shared storage and networking; per-NPU utilization, HBM, temperature, power and collection health |
| NPU processes | Only processes reported by NPU Exporter, with employee evidence, optional name, container, working directory and host mount mapping |
| Summaries and origin | Group by person, directory, node and container; show current process duration and evidence-based job origin |
| Unified reservations | A pool of two equal-capacity nodes (8 by default; opt-in 16), with personal login, queuing, allocation, claiming, cancellation and completion |
| Personnel allowlist | Search/add/remove only for verified Grafana administrators, synchronized across host rosters |
| Persistent guard | When explicitly enabled, intercept NPU containers with a valid employee ID but no mapped name; allowlist exemptions take precedence |
| Navigation | One canonical homepage, including old-home redirects, preserved query parameters and Grafana client-side navigation |
| Refresh | 1/5/10/30-second choices, default 1 second; queue polling follows the choice and pauses when automatic refresh is Off |
| Queue operations | Owners can move their queued or ready reservations down one place; administrators can reorder waiting reservations; active reservations cannot be moved down |
| Registration checks | Read-only process/queue matching; suspected unregistered use notifies the offender and reservation owners of overlapping node/cards only; no automatic Stop |
| Allowed users | The owner searches by employee ID or name; matching results must agree across both rosters. The owner can add or remove shared users for that reservation’s allocated node/cards |
| Night completion | Automatically ends registration after repeated fresh NPU inventories show allocated cards empty; no workload is stopped |
| Windows access | `Open-Monitor.cmd` for a local SSH tunnel, optional `Open-Web.cmd` for verified HTTPS, and a separate user guide |

Page refresh is not sampling frequency: resource scraping is normally every five seconds and process collection every ten seconds. A displayed name or origin is evidence from configured sources, not independent corporate identity verification.

Registration checks tolerate an exporter timestamp up to 15 seconds ahead of the central server to avoid false unverified status from small host clock differences. Samples more than 30 seconds old, clock differences beyond that tolerance, and a local cache older than 20 seconds remain invalid. Keep host time synchronization healthy.

### Unified queue rules

The table below describes the default A5 eight-card profile. With explicit `task_queue.cards_per_node: 16`, single-node requests allow 1–16 cards, balanced dual-node requests allow 2/4/8/16/32 total cards, and half-node groups contain eight cards. All other queue rules stay the same.

| Setting | Rule |
| --- | --- |
| Single node | Any available node or one specified node, 1–8 cards |
| Two nodes | 2/4/8/16 cards in total, split 1+1 / 2+2 / 4+4 / 8+8 |
| Card placement | Use available queue capacity; requests for 1–4 cards per node stay in one four-card group |
| Duration | Defaults to 0 hours and 30 minutes; hour and minute selectors use five-minute steps. Normal tasks are limited to 2 hours and new night tasks to 13 hours; existing reservations retain their duration |
| Reservation horizon | Normal tasks finish by next-day 09:00; night tasks start from 20:00 until before next-day 09:00, enter active registration automatically at allocation and may finish later with a warning |
| Scheduling priority | Plan in queue order; backfill must preserve earlier reservations and the release buffer. Unknown release times protect resources an earlier eligible reservation may need. Normal tasks that cannot start before 02:00 remain on standby; active and offered allocations are not preempted |
| Claiming | Click Start use directly within at most ten minutes of allocation; an earlier scheduling deadline still applies |
| Reminders | At allocation and each minute during an ordinary claim window; keep the page open, signed in and notifications enabled |
| Unclaimed offer | Release that allocation and move back one waiting position, preserving the original application time |
| Completion | Stop the workload yourself, then confirm Finish registration; the button releases the reservation without stopping processes or containers |
| Daily reset | At 09:00 clear normal reservations and cancel unstarted reservations from the elapsed night; retain active night reservations with their original times and enable manual completion, without stopping workloads |

Application time remains visible after a task starts. Offer deadlines and order are persisted; reloads or proxy restarts do not reset them. A stale offer ID cannot claim a new allocation. Consecutive simultaneous misses pass the next reservation as one block, moving each missed reservation one position; a trailing reservation stays at the end when nobody follows it.

Reservations coordinate capacity recorded in the queue. They do not execute startup commands or enforce hardware isolation against workloads launched outside it. Active workloads are never stopped for a missed claim or estimated-duration expiry. The older independent-node queue and browser Stop workflow are described separately in the [legacy queue reference](task-queue.md).

### Four-step user workflow

1. Connect to the company network/VPN, extract the complete access package and run `Open-Monitor.cmd`. Sign into the personal queue account and verify notifications.
2. Choose machine, card count and duration; select a future start for a scheduled night task.
3. When allocated, check the machine/cards and click Start use within ten minutes, then launch the workload yourself.
4. Stop the workload yourself, then confirm Finish registration.

If the direct web link fails, use the CMD tunnel and leave its window open. SSH credentials, personal queue credentials and Grafana administrator credentials serve different purposes.

## 2. Architecture and state

```mermaid
flowchart LR
    PC[Browser / Windows SSH tunnel] --> Proxy[Central proxy: HTTP / optional HTTPS]
    Proxy --> Grafana[Grafana: loopback only]
    Grafana --> Prom[Prometheus: loopback only]
    Prom --> Exporters[Host / NPU / process exporters]
    Prom --> QueueMetrics[Proxy queue metrics]
    Proxy --> QueueDB[(SQLite accounts and reservations)]
    Proxy -->|mTLS| Agents[Host container agents]
    Agents --> State[Private rosters / guard state / audit logs]
```

A typical extended deployment uses proxy HTTP on loopback 3000, Grafana on loopback 3001 and Prometheus on loopback 9090. Optional HTTPS binds an explicitly selected intranet interface. Match the actual deployment configuration; do not expose underlying services or Docker.

| File/configuration | Purpose and retention |
| --- | --- |
| `deployment.json` | Base installer nodes, data volume, ports and retention; queue extension fields do not belong here |
| `<base>/config/container-control/proxy.json` | Grafana URL, node agents, mTLS, canonical home, queue and optional HTTPS |
| `<base>/config/container-control/agent.json` | Local node, mTLS, protected containers and optional guard |
| `<base>/config/process-owners.json` | `employee_names` and other owner mappings; keep private and out of client packages |
| `<base>/data/task-queue/queue.db` | Hashed accounts, sessions, order, claim deadlines and offer IDs; back up with SQLite backup |
| `<base>/data/container-guard/state.json` | Full-container-ID marks and exemptions; never reset it during an upgrade |
| Certificates, private keys and audit logs | Restricted deployment storage; no server private key or administrator password in access packages |

Allowlist retries with the same name are idempotent; conflicting names are rejected and partial cross-host failures are reported. Allowlisted historical guard marks remain stored but disappear from the active-interception table. Removing an exemption can restore enforcement. Allowlisting does not restart a stopped container.

## 3. Configuration and deployment

### Basic monitoring

1. Prepare a private `deployment.json` from the [example](deployment.md#configuration), choosing an existing mounted data volume, executor directory, central node, monitoring IPs and ports.
2. Follow the [deployment reference](deployment.md) to inspect architecture, drivers, mounts, free space and existing ownership. The base installer uses `<storage_root>/<executor_id>/ascend-monitor/<id>`; reuse a verified existing path for upgrades.
3. Use the Windows wizard, or run `deploy_node.py --probe` and `--install` from each host's executor-specific staging directory. Use `assets/software.json`; verify SHA-256 for offline packages too.
4. Run `--verify` centrally and check every expected target, NPU data, process freshness, dashboard UID and datasource.

Build the wizard from the repository root:

```powershell
python skills/ascend-resource-monitor/scripts/build_windows_package.py --mode wizard --output dist
```

Windows needs PowerShell 5.1 and OpenSSH Client, not Python. Hosts need Python 3.9+, systemd, working drivers and installation privileges. Match the hardware/software profile in the deployment reference rather than generalizing one existing installation to every device.

### Unified queue, allowlist and guard

These use the central proxy and mTLS host agents; the base wizard does not enable them automatically. First follow the [container-control reference](container-control.md) for service users, certificates and restricted directories. Merge this illustrative fragment into the **proxy configuration**, not the base installer's configuration:

```json
{
  "dashboard_uid": "lab-monitor",
  "canonical_dashboard_path": "/d/lab-monitor/overview",
  "task_queue": {
    "unified_queue": true,
    "database": "/data/operator/ascend-monitor/lab-monitor/data/task-queue/queue.db",
    "timezone": "Asia/Shanghai",
    "metric_prefix": "ascend",
    "reservation_buffer_seconds": 600,
    "daily_reset": true,
    "monitor_processes": true,
    "prometheus_url": "http://127.0.0.1:9090",
    "process_metric_prefix": "ascend_process_"
  }
}
```

- Configure exactly two equal-capacity nodes in the proxy's `nodes`, matching monitoring labels. Keep `task_queue.cards_per_node` omitted or `8` for A5. Set it explicitly to `16` for A3 logical IDs 0–15; use the [card-capacity migration procedure](task-queue.md#card-capacity-and-a3-migration) only for a verified old paired-card A3 layout. The canonical path must belong to the same dashboard UID.
- Install matching versions of the proxy, queue, planner and dependencies from `assets/runtime/`. Run `control_proxy.py --config .../proxy.json` centrally and `container_agent.py --config .../agent.json` on each host under their established service accounts.
- Use `patch_dashboard.add_queue_home_panel(dashboard, nodes)` to add the unified region. Preserve existing panels, datasource UIDs, selectors and control links. The proxy supports both old and new Grafana dashboard APIs.
- With `monitor_processes` enabled, also apply `patch_dashboard.add_registration_status(dashboard, prefix)` to add the process table's registration-status column. Match the queue metric prefix and validate `*_queue_process_source_up` on every node. This is a read-only match of current employee/node registrations; ambiguous or stale evidence remains unverified, and no container is stopped.
- Install personnel modules per the [roster reference](personnel-roster.md). Enable automatic interception only under the separately authorized [guard procedure](container-guard.md), after reviewing candidates and protected workloads.
- Scrape queue metrics at `/a5-queue/metrics`. Two nodes with three exporters each plus queue metrics normally produce seven targets; the configured topology is authoritative.
- Set Grafana `[dashboards] min_refresh_interval = 1s`, dashboard `refresh` to `1s`, and `timepicker.refresh_intervals` to `1s/5s/10s/30s`.
- Keep process-exporter, Prometheus and guard prefixes aligned. The exporter defaults to `ascend_`; preserve a custom namespace with `--metric-prefix <prefix>` and matching guard `<prefix>_process_`. A healthy scrape does not prove query compatibility.

Use device-wide `npu_chip_info_overall_utilization` for the NPU headline and per-card usage when available, falling back to AICore utilization on older exporters. These are distinct metrics in the [official metric definition](https://github.com/Ascend/mind-cluster/blob/master/docs/zh/scheduling/06_api/00_npu_exporter/01_prometheus_metrics_api.md). AICore may report zero while device-wide usage is nonzero; HBM occupancy is not a substitute for compute usage.

See [control deployment](container-control.md) for permissions and [HTTPS entry](web-entry.md) for certificates, cookies and WebSockets. Unified Start/Finish does not invoke the legacy browser Stop workflow; keep their instructions separate.

### Access package

After verification, build the client with a verified shared URL if available:

```powershell
python skills/ascend-resource-monitor/scripts/build_windows_package.py --mode client --config deployment.json --shared-url "https://monitor.example.internal:8443/d/lab-monitor/overview?refresh=1s" --output dist
```

`Open-Monitor.cmd` uses the same dashboard path/query through a local tunnel; `Open-Web.cmd` opens HTTPS; `Install.cmd` creates a shortcut. Extract the full package and retain its PowerShell/JSON files instead of sending a lone CMD file. Real addresses, the deployment-specific HTML guide and personalized access packages remain in private handoff storage.

## 4. Upgrades and rollback

1. Record the revision, service names, paths, file hashes and intended changes. Back up changed code/configuration and create a consistent SQLite backup.
2. Preserve accounts, certificates, rosters, guard state, reservations, ports and metric namespaces. Do not run a baseline reinstall over an extended proxy layout.
3. Update dependent modules together and preserve file ownership, read permissions and the service's required writable data directories.
4. Restart only affected monitoring components; run `daemon-reload` only for changed unit definitions. Do not restart or stop business containers for verification.
5. Verify actual browser navigation, refresh choices, permissions, machine/card selection, time boundaries, application time, reminders/claims, allowlist filtering, queries and all expected targets before delivering access tools.
6. If a mutation fails, inspect its original receipt/logs, then restore the corresponding backups. Never blindly retry a container stop or overwrite newer reservation activity with an old database.

## 5. Improvements in this iteration

| Problem | Improvement |
| --- | --- |
| Divergent default/new homes and missing queue after client-side navigation | Canonical home redirects and queue loading across both dashboard APIs |
| Limited resource combinations | Separate machine/card selectors, automatic or fixed single-node placement, four balanced dual-node sizes |
| Awkward hour-only entry and unbounded future reservations | Five-minute choices, a normal-task 09:00 deadline and visible warnings for night tasks extending beyond 09:00 |
| Allocations held without being claimed | Server-side ten-minute window, repeated reminders, one-position deferral and stale-offer rejection |
| Extra Start dialog and disappearing application time | Direct claim, preserved application time, retained cancellation/completion confirmation |
| Confusing allowlist/login flow | Administrator-only roster access, no redundant login link, cross-host synchronization and conflict checks |
| Exempt containers mixed into active interception | Filter the active table while retaining stored marks |
| Process/guard tables disappeared after collector updates | Configurable metric namespace and validation of actual returned series |
| Inconsistent polling/access instructions | One-second default, four refresh choices, CMD fallback and synchronized handoff documentation |
| Overdue use and rigid waiting order | Identify immediate, shared and other overdue blockers; notify the nearest directly affected waiter after five minutes |
| Stale daily reservations and awkward duration defaults | Optional 09:00 clearing of normal reservations without workload interruption; night reservations remain visible |
| AICore-only headline remained zero | Prefer device-wide utilization, with per-card fallback for older exporters |
| Unregistered NPU use was hard to see | Fresh-source registration matching, informational status and online notices, with no automatic Stop |

## 6. Validation scope

Use `python tools/check.py` and the required commit-range `pre-commit` checks. This iteration verified an existing two-node deployment's pages, permissions, entry points and all seven scrape targets. The latest isolated Linux backend suite ran 178 tests with three skips. Allocation, claim expiry, exact queue movement, restart persistence, stale offers, reminders, direct claiming, daily-reset boundaries, priority authorization and process-source freshness were tested in isolation or with browser fixtures; production workloads were not stopped for testing. The next live 09:00 reset is not forced as an acceptance test.

Fresh-host installation, other hardware profiles and different corporate network/certificate policies still require their own acceptance checks. Skipped tests are not reported as passed.
