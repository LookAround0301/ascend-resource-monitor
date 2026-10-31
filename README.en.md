# Ascend Resource Monitor

Reusable monitoring skill for Linux Ascend NPU servers, with a native Windows deployment wizard and per-environment dashboard launcher.

[中文](README.md) · [Windows wizard](https://github.com/LookAround0301/ascend-resource-monitor/releases/latest/download/ascend-monitor-setup.zip) · [Skill ZIP](https://github.com/LookAround0301/ascend-resource-monitor/releases/latest/download/ascend-resource-monitor-skill.zip)

## Features

- Grafana/Prometheus dashboards for host CPU, RAM, disks, shared storage, networking, and per-device NPU utilization, HBM, temperature and power.
- **NPU processes only**: PID, OS user, employee-identity evidence, container, working directory and host mount mapping.
- Unknown/conflicting identities are explicit. A shared root account or matching directory does not prove who started a workload.
- Multi-node filtering, 5-second resource refresh and 10-second process sampling by default.
- NPU process summaries by employee, directory, node and container, with host mount mapping.
- Administrator passwords stay in a protected local operator record, never server configuration; reinstalls preserve existing accounts.

Optional [administrator-only Stop controls](skills/ascend-resource-monitor/references/container-control.md) add Stop links, an authenticated confirmation page and per-session login status. The backend verifies current NPU processes, full container ID and unchanged start time, then audits the stop. This stops the entire selected container with a 30-second graceful timeout. The control backend is deployed separately; the default Windows deployment remains read-only.

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

The Skill ZIP and Windows wizard are each tens of KiB; see build outputs and Release assets for exact sizes. Monitoring binaries, model weights, passwords, private keys and real machine configurations are excluded. Deployment downloads and SHA-256-verifies binaries separately (roughly 550 MB for a full installation), or uses an offline package directory.

An existing deployment has 20 panels and six working targets across two nodes. Local tests cover NPU-only selection, attribution, configuration, archive boundaries, install/reinstall behavior, credentials and Stop authorization. Windows syntax, forms and credential ACLs are checked; an isolated real Grafana instance validates bootstrap and login. **A complete installation on a fresh server and a full download through the restricted proxy have not been accepted end to end.**

## Develop and package

```sh
python tools/check.py
python tools/build_release.py --output dist
```

Checks use the Python standard library. On Windows they also check PowerShell syntax and construct the wizard. `dist/` contains the generic wizard, Skill ZIP and `SHA256SUMS.txt`; generated outputs are ignored by Git.

The skill lives in `skills/ascend-resource-monitor/`; `assets/runtime/` contains Linux code, `assets/windows/` the Windows tools, `references/` the operational guidance, and `scripts/` packaging/tests. `examples/` contains documentation-only addresses. `tools/` holds repository checks and release packaging.

See [deployment guidance](skills/ascend-resource-monitor/references/deployment.md) and [Windows tooling](skills/ascend-resource-monitor/references/windows.md). Installation checks existing ownership and does not replace another deployment's services or modify SSH keys, drivers or application containers.
