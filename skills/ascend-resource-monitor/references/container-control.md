# Optional administrator-only container Stop controls

Use this workflow only when the user asks to add container control. This is an optional capability, not part of the default read-only installer. Do not treat path or employee-ID clues as authorization.

## Components

- `assets/runtime/control_common.py`: validation, Docker Unix-socket client, current NPU PID/container checks, audit logging and the restricted stop operation.
- `assets/runtime/container_agent.py`: an HTTPS agent on each monitored host, requiring a client certificate issued by the deployment CA. It exposes health and stop only, not Docker's generic API.
- `assets/runtime/control_proxy.py`: an unprivileged same-origin Grafana reverse proxy. Requires `aiohttp` (tested with 3.14.3). It verifies the caller against Grafana `/api/user` and `/api/user/orgs`; only a server administrator or an Admin in the configured organization is allowed.
- `assets/runtime/patch_dashboard.py`: adds a native Grafana table Actions cell, Stop button, confirmation text and administrator login link. The collector's `control_ref` combines the immutable container ID and start timestamp.

Grafana 13.2.4 supports this native Actions cell and confirmation. Check the deployed version before adapting the dashboard schema. No executable JavaScript injection or secret in dashboard JSON is needed.

Native actions also require the dashboard's frontend edit capability. Set the controlled dashboard's `editable` flag when enabling actions, while leaving viewer RBAC unchanged. A present but empty Actions column is not proof that the control is visible; check a real administrator session and a viewer session separately. Backend authorization remains mandatory because editors may also see native action buttons.

## Deployment shape

Keep the existing externally tunneled port stable. For example, move Grafana from loopback 3000 to free loopback 3001 and put the control proxy on loopback 3000. Forward normal HTTP/WebSocket traffic to Grafana. Reserve `/a5-control/*` for the control API. Back up the previous ini and dashboard first; roll back the port change if proxy health fails.

Run the proxy under its own unprivileged service account, separate from Grafana and without Docker socket access. Run each small host agent under an account authorized to access the local Docker socket; bind it only to the explicit monitoring-network IP. Require mutual TLS between proxy and agents. Issue separate server certificates with exact IP SANs and one client certificate for the proxy. Protect private keys and keep them out of Git, releases, logs and dashboard configuration.

Example proxy configuration (adapt paths/addresses):

```json
{
  "port":3000,
  "grafana_url":"http://127.0.0.1:3001",
  "org_id":1,
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

The native button sends a same-origin JSON POST with a custom header, node and `control_ref`, after its confirmation dialog. The proxy checks Origin, content type, authenticated identity, current administrator role and an allowlisted node URL. Host/URL/command input is never accepted from the client.

The agent requires a deployment client certificate, checks exact full container ID and unchanged start timestamp, running state, current NPU PIDs intersected with that container's host PIDs, and protected-container rules. Kubernetes-managed containers are refused; stopping managed workloads belongs to their scheduler. A Docker stop affects every process in the selected container. It allows 30 seconds for graceful exit, after which Docker may force termination.

Record actor, node, container ID/name, start timestamp, matching NPU PIDs and result. The audit log must be writable before sending a stop. An ambiguous timeout is recorded as uncertain and must not trigger an automatic retry. No authentication cookies, passwords, full command lines or environment blocks go into audit logs.

## Verification

Run the pure tests in `scripts/tests/test_control.py`. With aiohttp available, also run `scripts/tests/test_control_http.py` through unittest discovery; it uses a fake Grafana and fake agent and never accesses Docker. The compressed-auth-response case is included because normal proxy traffic preserves compression while identity JSON must be decompressed.

When copying scripts onto a host, install them atomically with permissions readable by the configured service user. Give the proxy traversal permission through parent log directories and write permission only to its own audit folder. A file upload that replaces modes with 0600 can prevent an unprivileged service from starting. Bound proxy shutdown time so a browser WebSocket does not unnecessarily delay reloads.

Check live anonymous, Viewer and Editor requests are refused, a real Admin session is recognized, wrong Origin is refused, missing confirmation is refused, and an invalid container never calls stop. Use temporary test accounts only when needed and remove them afterward. Verify both agents reject clients without a certificate. Confirm all monitoring scrape targets and dashboard queries remain healthy.

Do not stop an existing business container to test the feature. Positive stop tests use the fake Docker implementation or an explicitly authorized disposable workload. Only after these checks should the user operate the live Stop button.
