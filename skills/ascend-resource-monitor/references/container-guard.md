# Optional persistent container guard

Use only when the user explicitly asks to automatically stop containers whose current NPU jobs show a valid employee ID but no name in their supplied roster. A missing name is a local policy condition, not evidence of abuse or unauthorized access. Do not enable this for ordinary monitoring or a request to hide the name column. Keep existing names visible unless asked otherwise.

Before enabling, list the currently matching container IDs/names and explain that enabling applies to them too. Reuse an explicit scope confirmation; do not request a second confirmation for the same authorized policy. Validate with fake Docker tests and a disabled live guard before activation. Never restart a business container merely to exercise the guard.

## Behavior

`container_guard.py` runs inside each optional mTLS `container_agent.py` service. Every ten seconds it reads a fresh, complete local process inventory and a valid nonempty `employee_names` mapping. Candidates have a single syntactically valid employee ID and no mapped name. It rechecks the exact container start, current NPU PIDs and container membership before persisting a mark. Unknown/ambiguous IDs, broken rosters, stale/truncated inventories, protected containers and Kubernetes workloads do not create new marks.

Marks are keyed by full Docker container ID on that node. They survive process/service/host restarts and container renames. Once marked, the same container is stopped after future starts even if it has no current NPU process. A valid administrator-maintained personnel allowlist takes precedence when every recorded employee ID for the mark is included. The mark remains stored and resumes enforcement if the personnel exemption is removed. An explicit container whitelist also exempts it. A deleted and recreated container has a different ID and is evaluated independently; a prior whitelist does not transfer by name. Removing a whitelist entry restores an existing mark, or allows future candidate detection if never marked.

Enforcement scans default to every two seconds and use Docker's existing graceful stop (up to 30 seconds). This is a polling guard, not an admission controller: it cannot prevent a container briefly running before detection, and a preceding slow stop can delay a scan. Network/daemon outages can also delay enforcement. Persist the attempted start timestamp before submitting a stop. On an uncertain response, inspect state but do not resubmit for the same start; a genuinely new start is a new authorized attempt. Display uncertain results for administrator review.

The whitelist bypasses automatic stopping only. It does not restart a container and does not disable the separate administrator-confirmed manual Stop capability. Whitelist changes require a current Grafana Admin session, same-origin JSON, the existing control header, exact node/container ID and confirmation. Persist the state and audit mutations. Ordinary users can see the dashboard's guard records but cannot modify them.

## Provisioning an existing control-enabled deployment

Follow the control deployment's executor-specific data path and backup conventions. Install the updated `container_agent.py`, `control_proxy.py`, `guard_ui.py`, `container_guard.py` and `process_exporter.py` with their existing runtime dependencies. The proxy remains unprivileged with no Docker access. The host agent retains its existing Docker access; no broader remote API is exposed.

Create a root-owned 0700 directory on the deployment data volume and initialize `state.json` as `{"version":1,"entries":{}}`, mode 0600, **only if this is a new guard**. Never replace/reset an existing state file during upgrades. Missing or invalid state must prevent guard startup rather than silently erasing blocks. Add this section to the host-agent configuration, using the actual paths and metric prefix:

```json
{
  "guard": {
    "enabled": false,
    "state_file": "/data/operator/monitor/data/container-guard/state.json",
    "owners_file": "/data/operator/monitor/config/process-owners.json",
    "process_url": "http://10.10.0.10:18083/metrics",
    "metric_prefix": "ascend_process_",
    "interval": 2
  }
}
```

Give the process exporter read access to the state through its existing service account; do not make the file world-readable. Add `--guard-state <state.json>` to its ExecStart and preserve any deployed `--metric-prefix`. `patch_dashboard.add_guard_panel(dashboard, prefix='ascend')` adds the active-interception table and the administrator personnel link, preserving the existing NPU summary. Stopped containers remain visible while intercepted; personnel/container exemptions are filtered from this table without deleting their marks. The normal process tables remain NPU-only.

Restart just the updated monitoring components. Check the disabled guard through the mTLS `/guard` endpoint or administrator `/a5-control/guard/status`, and verify the personnel list through `/a5-control/guard`. Once the administrator has configured and reviewed the synchronized allowlists and current candidates, use the administrator-only “自动停止容器” control on that page to enable it on all hosts. The page displays each host's actual state; partial failures must be reviewed and retried. Disabling stops automatic enforcement without deleting existing marks or starting containers. The switch is persisted in the guard state file and survives service restarts; it overrides the initial `guard.enabled` configuration. Legacy deployments with no saved switch retain their explicitly configured `guard.enabled` value, so upgrades do not silently change an existing policy. New installations must provision `enabled: false`. Preserve existing certificates, credential hardening, HTTPS/SSH entry points and whitelist data on later upgrades. The default wizard does not silently enable this policy.

## Verify

Run `test_container_guard.py` and `test_control_http.py`. They cover persistent restart interception, whitelist addition/removal, preemptive exemptions, stale/invalid source rejection, exact container identity, protected/non-NPU workloads, durable uncertain attempts and administrator/CSRF checks. HTTP tests require aiohttp and should not be treated as passed if skipped.

Check both host agents, all monitoring targets, `*_container_guard_state_read_success`, the persistent table in a real browser and the admin page. Observe the authorized initial stops and audit result; never claim restart behavior was live-tested by restarting somebody else's job. Generate the normal Windows access package at the end; the guard runs on the server and does not depend on a PC or browser staying open.
