# Deployment and recovery

Use this reference when running or adapting the Linux deployment, not merely opening an existing dashboard.

## Inputs and support

The shipped profile uses NPU Exporter 26.2.0, Prometheus 3.15.1, Grafana 13.2.4 and node_exporter 1.12.1, Linux ARM64. The NPU stack was exercised on Ascend 950DT/openEuler 24.03. These are pinned reproducible artifacts, not a promise that they are the newest versions or support every Ascend product. Inspect the target hardware, OS, driver and the official exporter compatibility table before selecting another profile. New architectures require a matching verified `software.json` and corresponding NPU metric validation.

Sources: [NPU Exporter build and images](https://github.com/Ascend/mind-cluster/blob/master/docker/npu-exporter/OVERVIEW.zh.md), [Prometheus downloads](https://prometheus.io/download/), [node_exporter releases](https://github.com/prometheus/node_exporter/releases), [Grafana downloads](https://grafana.com/grafana/download).

The host needs Python 3.9+, systemd, npu-smi and root privileges. A non-root SSH account needs noninteractive `sudo -n` for installation. Password authentication is handled by native OpenSSH in the console. Do not ask the user to paste a password into configuration or distribute their private key.

## Configuration

`assets/runtime/configuration.py` validates all fields. Example with documentation-only addresses:

```json
{
  "id": "lab-monitor",
  "title": "实验室资源监控",
  "executor_id": "operator",
  "storage_root": "/data",
  "central_node": "node-a",
  "nodes": [
    {"name":"node-a", "host":"192.0.2.10", "ssh_port":22, "ssh_user":"root", "monitor_ip":"10.10.0.10", "node_exporter":"auto"},
    {"name":"node-b", "host":"192.0.2.11", "ssh_port":22, "ssh_user":"root", "monitor_ip":"10.10.0.11", "node_exporter":"reuse"}
  ],
  "grafana_port":3000,
  "prometheus_port":9090,
  "local_port":13000,
  "anonymous_viewer":true,
  "retention_days":7,
  "retention_gb":5,
  "shared_mounts":["/mnt/share","/mnt/weight"],
  "network_regex":".*"
}
```

Optional node fields: `node_port` (9100), `npu_port` (18082), `process_port` (18083), `expected_npus`. No default NPU count is assumed. Labels must be unique, simple directory-safe names. Multi-node monitoring IPs must be reachable from the central host; do not use loopback or 0.0.0.0. The public/SSH address may differ from this internal address.

The actual installation path is `<storage_root>/<executor_id>/ascend-monitor/<id>`. Stage scripts under that executor's directory as well. Use neutral service names `ascend-<id>-<role>.service`; timestamps rather than employee IDs name backups. For an existing deployment, reuse its known configuration and path; this installer does not migrate unrelated installations automatically.

`storage_root` must already exist on a separate mounted data filesystem, with at least retention+6 GiB free on the central node and 1 GiB on other nodes. Prometheus's central data volume must be local rather than NFS/CIFS. No model weights or large archives go to the root filesystem. The wizard stages only small scripts before checking the mount; software archives are uploaded after successful checks. Enclosing directory permissions must allow the service account to traverse to the deployment.

## Execution

Prefer the available MindIE host-binding, execution and artifact tools for agent-driven work. Read the corresponding remote receipt when submission status is uncertain. The Windows wizard implements the same sequence with SSH/SCP.

1. Copy `assets/runtime/*.py`, deployment JSON and `assets/software.json` into a fresh executor-specific remote staging directory. Record it.
2. From that directory run on **every** node:

   ```sh
   python3 deploy_node.py --config deployment.json --manifest software.json --node node-a --probe
   ```

   Stop on architecture/storage/port/config ownership conflicts. Probe output reports NPU identity for compatibility review. `auto` reuses a healthy node_exporter, otherwise installs one; `reuse` requires an existing healthy endpoint. Exporters bind to the explicit monitoring IP. An unknown owner of `/user/mind-cluster/npu-exporter-config` is a blocking conflict.

3. Download software from the pinned manifest or use the user's offline folder. Verify SHA-256 locally and again remotely. Upload only roles required by that node. The NPU archive is an OCI layer: extract only its binary and agreement. Never unpack its root filesystem on the host.
4. From each node's stage run:

   ```sh
   python3 deploy_node.py --config deployment.json --manifest software.json --node node-a --packages . --install
   ```

   On the central node add `--admin-password-stdin`, supplying the locally saved credential through SSH stdin as described in [credentials.md](credentials.md). The installer refuses unknown deployment directories/units, validates tar paths and systemd units, preserves the administrator/database and owner annotations, and backs up ordinary panel/config files. It enables services for reboot. It applies exact binary SELinux labels only when required; it does not disable SELinux or change the firewall.
5. On the central node run:

   ```sh
   python3 deploy_node.py --config deployment.json --manifest software.json --verify
   ```

   Verification checks all 3 targets per node, NPU metric count when supplied, fresh process data, dashboard and datasource. Empty NPU process tables are valid. After success, deliver the Windows client package as the last step.

`--render-only <directory>` renders reviewable configuration without remote installation. Administrator passwords remain in the operator's protected local record. `config/admin.env` contains only Grafana's encryption key and the flag disabling automatic default-account creation; it cannot be used to retrieve the login password. Pass `--admin-password-stdin` for authenticated verification, especially when anonymous viewing is disabled. See [credentials.md](credentials.md) for bootstrap, upgrades and rotation. Panel access is always over a loopback tunnel by default. Anyone allowed through the tunnel can view the dashboard when anonymous Viewer is enabled.

## Process interpretation

The process exporter takes the PID set from `npu_chip_info_process_info`, then reads only those processes. It exposes selected identity/working-directory fields, CPU and RSS, never full command lines or environment blocks. Executable/script paths and project directories themselves may still be sensitive; keep exporter IPs inside the trusted monitoring network.

When upgrading an existing collector, preserve the deployed metric namespace. The exporter defaults to `ascend_`; pass `--metric-prefix <prefix>` when existing dashboard queries and guard configuration use another prefix. Verify the actual process and guard series in Prometheus after restart, including any retained blocked-container record; a healthy scrape alone does not prove that the dashboard queries match.

Attribution order: manual container/service mapping, login audit user, process user, SUDO_USER clue, employee_id container-label clue, directory/container-name clue. The built-in ID pattern is a letter plus eight digits; adapt it when the organization uses a different convention. Container names support underscore-separated IDs and case-insensitive complete roster-account substrings, including known `p_` service accounts followed by task suffixes. A longer numeric identifier must not match a shorter employee ID. Container-only matches are labeled “容器名称线索”; conflicting container/directory IDs remain “待确认”. Initials and informal aliases are not guessed. Root alone gives no human identity. Multiple clues produce “待确认”; absent clues produce “未知”. Docker bind mappings resolve container cwd to host paths when available. Directory-group matches aid comparison but do not prove the same task/person. RSS sums may double-count shared pages. This collector supports host PIDs and Docker metadata; other container runtimes need an adapter.

Persistent overrides go in `config/process-owners.json`, for example `{"containers":{"training-container":"operator-id"},"services":{}}`. No workload restart is required; the next sample rereads the file. Process sampling is 10 s; normal resource scraping is 5 s. Process count is bounded to 200 per host and truncation is exported. NPU source failure leaves the last snapshot until it becomes stale/503, rather than inventing an empty successful list.

When the user supplies a staff roster for display, add an `employee_names` object to the same private configuration, mapping the complete lowercase account ID to its name. Use exact account matches, preserve leading zeros, and omit missing or conflicting names. Both process tables show the name next to the account ID; unmatched names remain blank. This lookup does not upgrade a directory/container clue into proven ownership. Keep real rosters and mappings outside the reusable skill, public repository, and downloadable packages. For an existing dashboard, `process_panels.add_name_columns(panels)` adds the field while preserving its queries, layout and Stop controls.

The summary's usage duration is the current time minus the earliest start time among that group's currently listed NPU processes. Start time comes from `/proc` boot time and process start ticks, so restarting the collector does not reset a running process. When all processes exit, the group disappears on the next collection/scrape; a later process starts a new duration without carrying over the old value. This is elapsed process lifetime, not accumulated NPU busy time or billing time. For an existing dashboard, `process_panels.add_usage_time(panels)` preserves the summary's group labels, including names and container control references, and uses instant queries only.

The process source must expose `npu_chip_info_process_info_num`; missing process metrics are a compatibility failure even if utilization metrics exist. An explicit zero count is a valid empty device list.

## Local validation

The unified queue supports `task_queue.flexible_scheduling: true` to expose daily avoid-start slots and opt-in shortened backfill independently of `strict_nights`. This setting does not enable the night coordinator, overtime release, container stopping, or the container guard. Existing strict-night deployments retain these scheduling preferences automatically. Normal task limits remain controlled by `normal_max_seconds` (5-minute increments through 28800 seconds / 8 hours; the default remains 7200). Updating the limit does not rewrite existing reservations or remove the next-morning deadline.

For daily 09:00 cleanup, `task_queue.daily_reset_normal_cutoff_hour` optionally sets the local start of the new normal period (an integer from 0 through 9). Only ordinary reservations created before that day's cutoff are removed; later submissions survive, including when cleanup runs after a delayed restart. For example, `2` retains reservations submitted at or after 02:00. The setting changes cleanup selection only, not start-time or enforcement policy. With no explicit cutoff, midnight-separation deployments use their night boundary and legacy deployments retain their existing clear-all behavior. Cleanup releases reservation records, not running workloads. This incorporates upstream daily-reset fix `e849924`.

For the K3.1 version pool, the avoid-time button is next to Join, with muted yellow selection colors and select-all / clear-all for the visible 09:00–24:00 slots. “仅避开饭点” selects only 12:00–14:00 and 17:30–19:30 and clears every other selection, both when submitting and when editing an existing reservation. Shared-user management appears below the reservation owner's name; only that owner can change the roster, and the server checks consistent names across all pool nodes. Agent booking uses the separate [K3.1 version reservation skill](../../k31-reservation/SKILL.md), which verifies the exact pool membership and keeps its configuration separate from A3/A5.

The K3.1 version pool uses `strict_nights`, `overtime_control`, `midnight_separation`, `night_carryover_cutoff_hour: 9`, `night_tomorrow_only: true`, and `daily_reset_normal_cutoff_hour: 0`. Normal reservations remain limited to 8 hours, start only during 09:00–24:00 and release registration at midnight; nights reserve fixed cards only for tomorrow's 00:00–09:00 window. The server enforces tomorrow-only for both web and Agent submissions, using Asia/Shanghai rather than the client clock. Existing confirmed nights survive date rollover. `night_takeover_enabled: false` disables automatic container stopping while retaining fresh-inventory and ownership checks: occupied or unverified cards wait, and reservation release alone never stops workloads. Both guard switches remain off. Other deployments retain their default takeover behavior unless they explicitly disable it.

Enable `task_queue.agent_reservations_enabled` only after configuring server-side reminders. The notification exporter uses `notification_export_token_file` on loopback; the central relay reads a restricted, fresh snapshot through the forced-command SSH account and adds this pool to `notification_sources` with its own `notification_source_enabled_at`. Keep other source settings and delivery history intact. The private deployment record stores actual hosts, activation time and backups. Notification setup does not enable guard enforcement or group broadcasts.

Run `python -m unittest discover -s <skill>/scripts/tests -v` for parser, NPU-only selection, empty-versus-unavailable source, ownership, archive traversal, configuration, and isolated install/reinstall tests. The install fixture substitutes systemd and account operations; it does not prove a deployment on new hardware.

For the K3.1 pool only, `task_queue.admin_preemption_enabled: true` exposes **紧急抢占** to a verified Grafana administrator on unstarted ordinary reservations. It requires midnight separation and operates only during 09:00–24:00. The administrator previews selected cards and affected owners before confirming; the server rejects stale previews and persists operation receipts for idempotent retries. Unused cards are preferred. Conflicting ready/active reservations return to the front as whole requests, in their previous relative order, preserving resource needs, durations and shared users while invalidating old offers. The emergency reservation receives cards immediately and still needs to be claimed within its offer window. Its immediate offer overrides avoid-start preferences; midnight truncation remains in force and fixed nights are not preempted. Durable notices reach owners and shared users through desktop notifications and the existing relay feed. No process/container stop is issued; the new owner must verify physical release before use. This feature defaults off for other pools.

## Failure and recovery

Stop after a failed mutation; do not blindly launch another install. Inspect `<base>/install-receipt.json`, service journal and the local/remote job receipts first. Partial directories lack `.complete`; inspect or remove only that deployment's failed extraction directory before retrying. Do not delete another deployment or the global NPU config link. Fix a transfer by verifying the source and remote SHA-256; resume or use small verified chunks only if normal SCP is unreliable.

Read-only diagnosis: `systemctl status ascend-<id>-<role>`, `journalctl -u <unit>`, `ss -lntp`, `findmnt -T <base>`, and local exporter `/metrics`/`/healthz`. A blocked port can be a routing/firewall issue even when ping works; do not delete the Windows default route or disable TLS/host-key checking as a generic remedy.

To stop or remove monitoring, obtain the intended cleanup scope. Disable only this deployment's owned units; retain data/backups unless removal was requested. Node exporters that were reused are never owned by this deployment. Keep the deployment and verification receipts separate from reusable skill instructions.
