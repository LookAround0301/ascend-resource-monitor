---
name: ascend-resource-monitor
description: Deploy Grafana and Prometheus monitoring for Linux Ascend NPU servers, including NPU-only process ownership and working directories, and generate a Windows setup wizard and dashboard launcher.
---

# Ascend resource monitor

Build or extend a resource dashboard for one or more Ascend servers. Collect CPU, RAM, disks, network, NPU utilization/HBM/temperature/power, and **only processes reported by NPU Exporter**. Preserve existing business workloads and working monitoring installations.

## Gather the deployment inputs

Reuse known information; ask only for missing values. Collect server names, SSH addresses/ports/users, login method, monitoring-network IPv4 addresses reachable from the central node, central Grafana/Prometheus node, an existing mounted data directory, deployment name, and local dashboard port. Ask for an executor identifier when workspace policy requires it. Do not put passwords, tokens, private keys, or current users' details into this skill or distributable templates.

Offer the Windows form when the user wants to enter the information themselves. It needs Windows PowerShell 5.1, OpenSSH Client, VPN/network access, and remote Python 3.9+ with systemd. Windows Python is **not** required to run the generated wizard. The bundled software profile is Linux ARM64, tested on Ascend 950/openEuler 24.03; select a compatible NPU Exporter and verified package manifest for other hardware/OS combinations.

## Deploy and validate

Read [references/deployment.md](references/deployment.md) for configuration, commands, boundaries, and recovery. Prefer available MindIE remote tools for agent-driven execution and retain their job receipts; the standalone Windows wizard uses native SSH/SCP. Respect workspace executor-specific working directories. Do not alter SSH authorized keys as part of deployment.

Probe all nodes before installing. Verify storage backing mounts and space, NPU/driver visibility, Python/systemd, ports, exporter compatibility, existing service ownership, and connectivity. Reuse healthy node_exporter endpoints. Never replace an unknown exporter configuration or unit. Present an actionable conflict instead. Existing deployments may only need a client access package.

The runtime scripts in `assets/runtime/` render the dashboard and install isolated services on the selected data volume. Grafana and Prometheus bind to loopback; the client connects through SSH. Do not broaden listen addresses or disable host-key, TLS, firewall, or SELinux checks to fix connectivity. New admin credentials are generated on the server and preserved on reruns.

Verify services, every expected scrape target, NPU metrics, process-source freshness, dashboard UID and datasource queries. Zero NPU processes is valid; an inaccessible process source is a failure. Missing human identity is shown as unknown; directory/container matches are labeled as clues, never proof. Keep progress, receipts and local configuration outside the reusable skill.

## Final workflow step: deliver Windows tools

After deployment verification, generate the Windows client installer and access launcher for that environment. It installs under the current Windows user's LocalAppData, creates a desktop shortcut, establishes a loopback SSH tunnel, and opens the dashboard. Also provide the deployment wizard when requested, so another operator can fill in their own servers and deploy. See [references/windows.md](references/windows.md).

```powershell
python scripts/build_windows_package.py --mode wizard --output <artifact-directory>
python scripts/build_windows_package.py --mode client --config <deployment.json> --output <artifact-directory>
```

Run commands with the script's full path when outside the skill folder. The wizard itself produces the client installer ZIP as its final successful step. Packages contain no passwords/private keys; SSH uses the receiving PC's key or interactive password. Report artifact links, validation performed, and any hardware/network checks that remain untested.
