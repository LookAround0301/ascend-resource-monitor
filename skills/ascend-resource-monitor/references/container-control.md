# Optional administrator-only container Stop controls

Use this workflow only when the user asks to add container control. This is an optional capability, not part of the default read-only installer. Do not treat path or employee-ID clues as authorization.

For the separately authorized automatic policy for unmapped employee IDs, see [container-guard.md](container-guard.md). Manual Stop keeps its administrator session and confirmation requirements; the persistent guard uses the explicitly enabled policy instead of a per-stop browser session.

## Components

- `assets/runtime/control_common.py`: validation, Docker Unix-socket client, current NPU PID/container checks, audit logging and the restricted stop operation.
- `assets/runtime/container_agent.py`: an HTTPS agent on each monitored host, requiring a client certificate issued by the deployment CA. It exposes health and stop only, not Docker's generic API.
- `assets/runtime/control_proxy.py`: an unprivileged same-origin Grafana reverse proxy. Requires `aiohttp` (tested with 3.14.3). It verifies the caller against Grafana `/api/user` and `/api/user/orgs`; only a server administrator or an Admin in the configured organization is allowed.
- `assets/runtime/patch_dashboard.py`: adds a visible Stop data link to the summary table. The collector's `control_ref` combines the immutable container ID and start timestamp.
- `assets/runtime/control_ui.py`: the administrator-only confirmation page and submit handler. Viewing this page never stops a container; only explicit confirmation submits the protected POST.

This implementation uses ordinary Grafana data links, which remain visible in read-only dashboards. The destination authenticates the user, fetches the actual container name through the host agent, and presents a confirmation. No executable JavaScript injection or secret in dashboard JSON is needed.

Native Actions cells can appear empty when their frontend permission context disallows execution. Merely saving an action or checking the API does not prove the button rendered. The data-link approach avoids that UI gate while retaining backend authorization. The dashboard remains read-only. Verify the actual operation link and the confirmation page separately.

## Deployment shape

Keep the existing externally tunneled port stable. For example, move Grafana from loopback 3000 to free loopback 3001 and put the control proxy on loopback 3000. Forward normal HTTP/WebSocket traffic to Grafana. Reserve `/a5-control/*` for the control API. Back up the previous ini and dashboard first; roll back the port change if proxy health fails.

Run the proxy under its own unprivileged service account, separate from Grafana and without Docker socket access. Run each small host agent under an account authorized to access the local Docker socket; bind it only to the explicit monitoring-network IP. Require mutual TLS between proxy and agents. Issue separate server certificates with exact IP SANs and one client certificate for the proxy. Protect private keys and keep them out of Git, releases, logs and dashboard configuration.

Example proxy configuration (adapt paths/addresses):

```json
{
  "port":3000,
  "grafana_url":"http://127.0.0.1:3001",
  "org_id":1,
  "dashboard_uid":"lab-monitor",
  "ca":"/data/operator/monitor/config/control/ca.crt",
  "cert":"/data/operator/monitor/config/control/client.crt",
  "key":"/data/operator/monitor/config/control/client.key",
  "audit_log":"/data/operator/monitor/logs/control/audit.jsonl",
  "nodes":{"node-a":{"url":"https://10.10.0.10:18084"}}
}
```

Example host-agent configuration:

```json
{
  "node":"node-a",
  "listen_ip":"10.10.0.10",
  "port":18084,
  "npu_url":"http://10.10.0.10:18082/metrics",
  "ca":"/data/operator/monitor/config/control/ca.crt",
  "cert":"/data/operator/monitor/config/control/server.crt",
  "key":"/data/operator/monitor/config/control/server.key",
  "audit_log":"/data/operator/monitor/logs/control-agent.jsonl",
  "protected_containers":[]
}
```

The per-node names must match Prometheus's node labels. Preserve the workspace-required executor component in all remote script working directories. Account permissions, dedicated systemd units, exact SELinux labels and backups should follow the selected host's existing deployment conventions. Create only the necessary control endpoint; do not change drivers, workload containers, SSH keys or host-wide Docker permissions.

## Request and safety checks

The Stop link opens `/a5-control/confirm` with `targetBlank: true`, allowing a full browser request rather than Grafana's client router handling an unknown page. Keep the relative same-origin URL so the administrator cookie works with any tunnel hostname and port. This GET performs no stop. The page is shown only to an administrator and displays the real node, container name and full ID. Its confirmation button sends a same-origin JSON POST with a custom header, node and `control_ref`. The proxy checks Origin, content type, authenticated identity, current administrator role and an allowlisted node URL. Host/URL/command input is never accepted from the client.

The proxy personalizes the dashboard login link from the current Grafana session: an administrator sees their login name, another authenticated user sees read-only status, and an anonymous user sees the login entry. These responses use `Cache-Control: no-store` and vary by authentication headers to avoid leaking one user's state to another. A new browser tab or hostname can have a different login session.

Personalization must cover both the legacy `/api/dashboards/uid/*` response and the resource APIs under `/apis/dashboard.grafana.app/*/namespaces/*/dashboards/*`. Confirm the actual browser shows the status and link: API configuration alone does not prove rendering/navigation. If a cached dashboard still sends the click through Grafana and shows its own 404, fully reload the dashboard after provisioning refreshes and retest from that fresh page. Do not mistake a successful direct GET for a successful click.

The agent requires a deployment client certificate, checks exact full container ID and unchanged start timestamp, running state, current NPU PIDs intersected with that container's host PIDs, and protected-container rules. Kubernetes-managed containers are refused; stopping managed workloads belongs to their scheduler. A Docker stop affects every process in the selected container. It allows 30 seconds for graceful exit, after which Docker may force termination.

Record actor, node, container ID/name, start timestamp, matching NPU PIDs and result. The audit log must be writable before sending a stop. An ambiguous timeout is recorded as uncertain and must not trigger an automatic retry. No authentication cookies, passwords, full command lines or environment blocks go into audit logs.

## Verification

Run the pure tests in `scripts/tests/test_control.py`. With aiohttp available, also run `scripts/tests/test_control_http.py` through unittest discovery; it uses a fake Grafana and fake agent and never accesses Docker. The compressed-auth-response case is included because normal proxy traffic preserves compression while identity JSON must be decompressed.

When copying scripts onto a host, install them atomically with permissions readable by the configured service user. Give the proxy traversal permission through parent log directories and write permission only to its own audit folder. A file upload that replaces modes with 0600 can prevent an unprivileged service from starting. Bound proxy shutdown time so a browser WebSocket does not unnecessarily delay reloads.

Check live anonymous, Viewer and Editor requests are refused, a real Admin session is recognized, wrong Origin is refused, missing confirmation is refused, and an invalid container never calls stop. Use temporary test accounts only when needed and remove them afterward. Verify both agents reject clients without a certificate. Confirm all monitoring scrape targets and dashboard queries remain healthy.

Do not stop an existing business container to test the feature. Positive stop tests use the fake Docker implementation or an explicitly authorized disposable workload. Only after these checks should the user operate the live Stop button.

Use the local password through an authenticated session or stdin during verification; never restore a server password file just to make a test pass. [Credential hardening](credentials.md) applies to the control-enabled deployment too. Test that anonymous users, Viewers and Editors cannot stop containers, that a real Admin can reach confirmation, and that wrong-origin, stale-instance and non-NPU requests fail. Empty NPU tables can be correct: compare each node's current exporter data and the dashboard's node filter before treating an empty table as a collection failure.
