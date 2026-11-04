---
name: ascend-resource-monitor
description: Deploy and maintain Grafana/Prometheus monitoring for Ascend NPU servers, with NPU process ownership, working directories, optional administrator container controls, and Windows setup/access tools.
---

# Ascend resource monitor

Build or extend a resource dashboard for one or more Ascend servers. Collect CPU, RAM, disks, network, NPU utilization/HBM/temperature/power, and **only processes reported by NPU Exporter**. Preserve existing business workloads and working monitoring installations.

Recommend and deliver the local one-click SSH-tunnel launcher by default. Direct HTTPS browser access is optional: corporate proxy, port and certificate policies can differ across PCs. When both modes are available, keep the local launcher and explain how to choose; do not treat one successful browser check as proof that every PC can connect.

## Gather the deployment inputs

Reuse known information; ask only for missing values. Collect server names, SSH addresses/ports/users, login method, monitoring-network IPv4 addresses reachable from the central node, central Grafana/Prometheus node, an existing mounted data directory, deployment name, and local dashboard port. Establish whether administrator container Stop controls are wanted and where the operator will keep credentials locally. Ask for an executor identifier when workspace policy requires it. Do not put passwords, tokens, private keys, or current users' details into this skill or distributable templates.

Offer the Windows form when the user wants to enter the information themselves. It needs Windows PowerShell 5.1, OpenSSH Client, VPN/network access, and remote Python 3.9+ with systemd. Windows Python is **not** required to run the generated wizard. The bundled software profile is Linux ARM64, tested on Ascend 950/openEuler 24.03; select a compatible NPU Exporter and verified package manifest for other hardware/OS combinations.

## Deploy and validate

Read [references/deployment.md](references/deployment.md) for configuration, commands, boundaries, and recovery. Prefer available MindIE remote tools for agent-driven execution and retain their job receipts; the standalone Windows wizard uses native SSH/SCP. Respect workspace executor-specific working directories. Do not alter SSH authorized keys as part of deployment.

Probe all nodes before installing. Verify storage backing mounts and space, NPU/driver visibility, Python/systemd, ports, exporter compatibility, existing service ownership, and connectivity. Reuse healthy node_exporter endpoints. Never replace an unknown exporter configuration or unit. Present an actionable conflict instead. Existing deployments may only need a client access package.

The runtime scripts in `assets/runtime/` render the dashboard and install isolated services on the selected data volume. Grafana and Prometheus bind to loopback; the client connects through SSH. Do not broaden listen addresses or disable host-key, TLS, firewall, or SELinux checks to fix connectivity. Read [references/credentials.md](references/credentials.md) before initialization or credential upgrades: create/reuse the password locally, send it through stdin, and leave no plaintext administrator password in server configuration, templates, archives or the long-running environment. Reinstalls preserve the existing account and encryption key.

Verify services, every expected scrape target, NPU metrics, process-source freshness, dashboard UID and datasource queries. Zero NPU processes is valid; an inaccessible process source is a failure. Missing human identity is shown as unknown; directory/container matches are labeled as clues, never proof. Keep progress, receipts and local configuration outside the reusable skill.

For job-origin display, read [references/job-origin.md](references/job-origin.md). The summary shows origin instead of RSS. The bounded evidence worker reads scripts and records actually linked to current NPU processes through their container filesystem; never execute a discovered script or crawl a container. Treat startup markers and lifetime-matched origin records as declarations, script/ancestry evidence as clues, and missing evidence as unknown. Periodic status checks alone do not establish agent origin. Do not infer manual execution from a terminal, SSH, username, or absence of agent hints; do not mark every process in a container from its creator's identity.

If the user explicitly requests container Stop controls, read [references/container-control.md](references/container-control.md). Keep ordinary viewing available, but require a real Grafana administrator session and an explicit confirmation before a stop. Verify the visible Stop link, actual browser navigation, per-session login status and server-side authorization separately. The default installer remains read-only; enable this capability only for an authorized deployment.

For personnel allowlist management, read [references/personnel-roster.md](references/personnel-roster.md). The existing guard link shows the employee-ID/name roster only to a real Grafana administrator; confirmed additions and deletions are administrator-only. Update both host rosters through the existing mTLS agents, preserve every other mapping, report partial synchronization, and allow safe same-name retries. Adding a person exempts that employee from automatic interception, including existing marks whose recorded IDs are all allowlisted; retain the marks for re-enforcement after removal. Deletion warns that the employee's jobs may again be subject to automatic interception.

For personal task queues or owner-scoped Stop, read [references/task-queue.md](references/task-queue.md). Each node has a separate self-service queue: registration, start and end records require no administrator approval or container. When the deployment uses employee IDs consistently, offer explicit opt-in automatic association with current NPU containers by the logged-in employee ID; this removes manual binding. Recheck fresh process ownership and every target before Stop. Keep manual association available for other deployments. Personal passwords protect task records; declared identities and monitored ownership clues are not independent proof of corporate identity. Stop and finish-registration both offer exact NPU processes or whole containers, with fresh confirmation. Finishing releases the queue only after all stops are verified; failure or uncertainty retains occupancy. With no target, finishing sends no stop command. Preserve the administrator override and the separate guard/whitelist policy.

When queue visibility is requested on the monitoring homepage, add a separate Grafana table for each node using the queue metrics endpoint. Show application time, requested duration, actual start and elapsed use since the user claimed the task. Requested duration is informational; do not enable automatic stopping on expiry.

If the user explicitly requests automatic stopping of NPU containers with an employee ID but no mapped name, follow [references/container-guard.md](references/container-guard.md). Clarify ambiguous selection wording before enabling it. This opt-in policy is separate from manual Stop: persist marked container IDs across restarts, permit only administrator-confirmed whitelist changes, and keep unknown/unavailable attribution data from becoming a stop decision.

For an explicitly requested shared company/VPN browser entry, read [references/web-entry.md](references/web-entry.md). Add a restricted HTTPS listener without exposing the underlying services, retain Viewer access and administrator-only Stop, and verify browser proxy/certificate behavior from a real client. The default SSH-tunnel listener stays loopback-only.

## Final workflow step: deliver Windows tools

After deployment verification, generate the Windows client installer and access launcher for that environment. It installs under the current Windows user's LocalAppData, creates a desktop shortcut, establishes a loopback SSH tunnel, and opens the dashboard. Also provide the deployment wizard when requested, so another operator can fill in their own servers and deploy. See [references/windows.md](references/windows.md).

If the same deployment already has a verified HTTPS entry, include the optional `Open-Web.cmd` with the client builder's `--shared-url` flag. `Open-Monitor.cmd` and the desktop shortcut remain the recommended entry. Adding a web shortcut must not deploy a listener, change proxy settings or install a certificate. When direct browser access is blocked, return to the local launcher, which still requires SSH connectivity and permission.

```powershell
python scripts/build_windows_package.py --mode wizard --output <artifact-directory>
python scripts/build_windows_package.py --mode client --config <deployment.json> --output <artifact-directory>
```

Run commands with the script's full path when outside the skill folder. The wizard itself produces the client installer ZIP as its final successful step. Packages contain no passwords/private keys; SSH uses the receiving PC's key or interactive password. Report artifact links, validation performed, and any hardware/network checks that remain untested.
