# Ascend Resource Monitor

Reusable monitoring skill for Linux Ascend NPU servers, with a native Windows deployment wizard and per-environment dashboard launcher.

[中文](README.md) · [Windows wizard](https://github.com/LookAround0301/ascend-resource-monitor/releases/latest/download/ascend-monitor-setup.zip) · [Skill ZIP](https://github.com/LookAround0301/ascend-resource-monitor/releases/latest/download/ascend-resource-monitor-skill.zip)

## Features

- Grafana/Prometheus dashboards for host CPU, RAM, disks, shared storage, networking, and per-device NPU utilization, HBM, temperature and power.
- **NPU processes only**: PID, OS user, employee-identity evidence, container, working directory and host mount mapping.
- Unknown/conflicting identities are explicit. A shared root account or matching directory does not prove who started a workload.
- Multi-node filtering, 5-second resource refresh and 10-second process sampling by default.

Optional [administrator-only Stop controls](skills/ascend-resource-monitor/references/container-control.md) add a confirmed container-stop action to the summary table. They require a separately configured control backend and Grafana authentication; the default Windows deployment remains read-only.

## Install as a skill

Ask a skill-capable agent to install this repository's `skills/ascend-resource-monitor` directory, or extract the [Skill ZIP](https://github.com/LookAround0301/ascend-resource-monitor/releases/latest/download/ascend-resource-monitor-skill.zip) into the client's skill directory. Invoke `$ascend-resource-monitor` and supply the target servers when asked. The workflow gathers necessary inputs, checks the environment, deploys and verifies monitoring, then produces a Windows client installer.

## Windows wizard

1. Extract [ascend-monitor-setup.zip](https://github.com/LookAround0301/ascend-resource-monitor/releases/latest/download/ascend-monitor-setup.zip), then run `Setup.cmd`.
2. Enter SSH endpoints/users/ports, reachable monitoring IPv4 addresses and the central monitoring node.
3. Enter an executor identifier, an existing mounted data directory and ports. An SSH identity file or offline package directory is optional.
4. Check the environment, review hardware compatibility, then start deployment.
5. Successful verification generates `client-<deployment>.zip`. Run its `Install.cmd` to install a desktop launcher.

Windows requires PowerShell 5.1 and OpenSSH Client, **not Python**. Servers require Python 3.9+, systemd, working NPU drivers and root or noninteractive sudo. Connect the appropriate VPN/network first. Passwords are entered in OpenSSH's terminal and are not saved. Private keys remain on the current PC. Grafana/Prometheus listen on server loopback by default; keep the SSH tunnel window open while using the dashboard.

## Profile and size

The default profile is **Linux ARM64**, based on an Ascend 950DT / openEuler 24.03 deployment: NPU Exporter 26.2.0, Prometheus 3.15.1, Grafana 13.2.4, with node_exporter 1.12.1 when a host exporter needs installation. Review compatibility and replace `software.json` for other hardware, systems or architectures.

The Skill ZIP is approximately **47 KiB** and the Windows wizard **32 KiB**; see Release assets for exact sizes. Monitoring binaries, model weights, passwords, private keys and real machine configurations are excluded. Deployment downloads and SHA-256-verifies binaries separately (roughly 550 MB for a full installation), or uses an offline package directory.

An existing deployment has 20 panels and six working targets across two nodes. Nineteen local tests cover NPU-only selection, attribution, configuration, archive boundaries and simulated install/reinstall behavior. Windows syntax and form construction are checked. **A complete installation on a fresh server and a full download through the restricted proxy have not been accepted end to end.**

## Develop and package

```sh
python tools/check.py
python tools/build_release.py --output dist
```

Checks use the Python standard library. On Windows they also check PowerShell syntax and construct the wizard. `dist/` contains the generic wizard, Skill ZIP and `SHA256SUMS.txt`; generated outputs are ignored by Git.

The skill lives in `skills/ascend-resource-monitor/`; `assets/runtime/` contains Linux code, `assets/windows/` the Windows tools, `references/` the operational guidance, and `scripts/` packaging/tests. `examples/` contains documentation-only addresses. `tools/` holds repository checks and release packaging.

See [deployment guidance](skills/ascend-resource-monitor/references/deployment.md) and [Windows tooling](skills/ascend-resource-monitor/references/windows.md). Installation checks existing ownership and does not replace another deployment's services or modify SSH keys, drivers or application containers.
