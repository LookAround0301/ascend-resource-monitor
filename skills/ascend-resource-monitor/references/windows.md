# Windows wizard and final client installer

Build the requested ZIP with `scripts/build_windows_package.py`. It copies the runtime and Windows assets, with UTF-8 BOM for Windows PowerShell 5.1. The skill package contains no large software binaries or user credentials.

## Deployment wizard

```powershell
python <skill>/scripts/build_windows_package.py --mode wizard --output <new-output-directory>
```

Optional `--config deployment.json` prefills known information; optional `--software verified-software.json` selects another package profile. Do not prefill another person's identity, private key, passwords or token. Normally share the blank wizard, and generate an environment-specific client separately.

The recipient extracts the ZIP and double-clicks `Setup.cmd`:

1. Server grid: node name, SSH address/port/user, monitoring IPv4, node_exporter policy. Select the central node. The private-key picker is optional and stays on that PC.
2. Storage/access: deployment ID, title, executor ID, data directory, localhost and server ports, optional offline package folder, anonymous Viewer choice.
3. Check/deploy: check first; review the console and NPU hardware profile. Start deployment when the inputs and hardware match. Large packages download only after every preflight passes. Deployment verification generates the final client ZIP automatically.

The console remains visible for native SSH password prompts and progress. SSH passwords are not saved and host-key checks are retained. With password login, several SSH/SCP calls can ask for it; using an existing authorized key is smoother. The wizard does not install a new server key itself.

Grafana's administrator credential is separate from SSH. The wizard creates a random password on this PC, saves it before bootstrap in `%LOCALAPPDATA%\AscendMonitor\credentials\<id>.json` with an operator-and-SYSTEM ACL, and passes it to the remote initializer through stdin. For an existing deployment it reuses that record or requests hidden input for the existing password; it does not reset the account. No administrator password enters server command arguments, configuration, archives or the long-running service environment. The console prints the local file path, not the password. The final client ZIP never contains that record. See [credentials.md](credentials.md).

Advanced per-node ports, expected NPU counts, retention and mount filters can be set in exported `deployment.json`. Run:

```powershell
.\run-deployment.ps1 -ConfigPath .\deployment.json -CheckOnly
.\run-deployment.ps1 -ConfigPath .\deployment.json -PackageDirectory D:\verified-packages
```

Logs and receipts: `%LOCALAPPDATA%\AscendMonitor\runs\<run-id>`. SSH host keys use OpenSSH's normal known_hosts. Close the terminal to interrupt; inspect receipts before retrying an interrupted install. Check-only stages small scripts but does not install or restart services.

OpenSSH missing: enable the Windows **OpenSSH Client** optional feature using the user's normal Windows administration process. This package does not alter execution-policy settings permanently, install drivers, adjust VPN routes or make firewall changes. The CMD wrapper uses a process-scoped execution-policy option for its bundled scripts.

Downloads use HTTPS and SHA-256. Proxy behavior follows HTTPS_PROXY/HTTP_PROXY when configured, otherwise Windows/.NET proxy configuration. On a restricted network, supply the exact filenames required by the selected node roles from `software.json` in an offline folder. Do not disable certificate checks. The NPU registry's normal bearer challenge is handled without persisting the token.

## Final access package

```powershell
python <skill>/scripts/build_windows_package.py --mode client --config deployment.json --output <new-output-directory>
```

Generate this **after** verifying the specified environment. For an existing working monitor, use its actual dashboard UID, SSH target and Grafana port; a server reinstall is unnecessary.

Recommend `Open-Monitor.cmd` as the primary entry. The recipient can use it directly without installation, or run `Install.cmd` to install into `%LOCALAPPDATA%\AscendMonitor\<id>` and create a desktop shortcut to that SSH launcher. `install-client.ps1 -NoLaunch -NoShortcut -DestinationRoot <temporary-folder>` supports isolated verification.

For a separately deployed and verified HTTPS entry, add an optional browser shortcut while retaining the local launcher:

```powershell
python <skill>/scripts/build_windows_package.py --mode client --config deployment.json --shared-url "https://192.0.2.10:8443/d/lab-monitor" --output <new-output-directory>
```

The resulting package contains both `Open-Monitor.cmd` (recommended) and `Open-Web.cmd` (optional). The URL goes into `client.json`; never include login credentials or tokens in it. The web shortcut only opens the configured URL and makes no proxy, certificate or server changes. If direct browser access is blocked, use the SSH launcher. The deployment wizard's normal final step keeps producing the SSH client; advanced callers of `New-ClientPackage` can supply `-SharedUrl` when the HTTPS service has already been verified. Do not invent an HTTPS address from the SSH host or assume every PC shares the same proxy policy.

When the optional HTTPS URL names the same dashboard UID, the SSH launcher reuses its canonical dashboard path and query on the local loopback address. Both shortcuts therefore open the same panel. An invalid or different-dashboard HTTPS shortcut does not change the independent SSH fallback.

The launcher uses `ssh -N -T -L 127.0.0.1:<local>:127.0.0.1:<remote>` with keepalive and forward-failure detection. It reuses an existing endpoint only when its dashboard UID matches. Keep the SSH window open; the VPN/network and tunnel must be available. Linux services survive reboot through systemd, but a disconnected PC must reopen its tunnel. `127.0.0.1` always means the PC opening the link, so sharing the URL alone does not connect another PC.

Test package configuration with `open-monitor.ps1 -ValidateOnly`; test form construction with `wizard.ps1 -SelfTest`. These checks do not prove deployment on a new host. For software/profile changes, also test archive extraction, render output, meaningful parser/ownership cases, and a permitted Linux deployment. Report these distinctions honestly.
