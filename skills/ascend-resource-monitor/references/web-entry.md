# Optional shared intranet HTTPS entry

Use this mode when the user explicitly asks for browser-only access across the company network or VPN. It changes the access boundary; keep the default SSH-tunnel mode for other deployments. It does not require SSO: anonymous users can remain Viewers while administrators use the existing Grafana login.

## Listener and certificate

Keep Grafana, Prometheus and the existing control proxy's HTTP listener on loopback. Add a native TLS listener to `control_proxy.py` using an explicitly selected host interface and an available unprivileged port, for example:

```json
"https": {
  "listen_ip": "192.0.2.10",
  "port": 8443,
  "cert": "/data/operator/monitor/config/web-tls/server.crt",
  "key": "/data/operator/monitor/config/web-tls/server.key"
}
```

The listener requires TLS 1.2 or newer and derives request security from the actual transport, not client-supplied forwarded headers. Set the certificate SAN to the exact address or DNS name users will open. Prefer an organization-trusted certificate. A self-signed certificate needs an explicit trust decision and its fingerprint must be verified through an already authenticated channel. If browser policy prevents using it, obtain an approved certificate rather than weakening policy or global TLS verification.

Make the private key readable only by root and the proxy's dedicated group; never package it with the Skill or Windows client. Back up the existing proxy configuration, source and Grafana ini before changing the listener. Do not expose the node agents, Docker socket or raw Prometheus endpoints.

## Grafana and permissions

Set Grafana `server.root_url` to the shared HTTPS URL and its `server.domain` to the chosen host. Keep `auth.anonymous.enabled=true`, `auth.anonymous.org_role=Viewer`, registration disabled and Viewer editing disabled. Include only the intended shared and backup origins in `live.allowed_origins`.

The TLS proxy adds `Secure` to forwarded login cookies while preserving HttpOnly, SameSite and the separate Set-Cookie headers. This allows an intentionally retained loopback HTTP tunnel to continue working without sending a shared-entry session over HTTP. Do not trust X-WEBAUTH-USER or forwarded scheme headers as authorization. Preserve administrator verification, exact Origin checks and explicit confirmation for Stop.

## Reachability and verification

Verify direct TCP/TLS access from an actual company/VPN client, then verify the browser separately. Corporate browsers may send the address through a proxy even when direct TCP works. Prefer a centrally configured intranet hostname/proxy rule for many PCs. For a user-authorized local workaround, add only the specific monitor host to proxy bypass rules; preserve every existing rule and save a rollback copy. Do not disable the corporate proxy globally. Other PCs with the same routing issue also require that exception or a centrally deployed rule.

Check anonymous dashboard/datasource access, fresh administrator login, secure cookies, per-session status, and a successful WebSocket upgrade. Confirm that anonymous users, Viewers and Editors cannot stop containers, and that wrong-origin, unconfirmed and invalid-target requests are refused. Visiting the confirmation page must never stop a container. Use trusted certificate verification in tests, and report which client/network paths were actually verified.

Record the final URL, certificate fingerprint/expiry, backup paths and rollback commands in the deployment record. Keep the original SSH launcher available if requested. A shared URL removes the need to start a local tunnel; it does not by itself integrate company SSO or eliminate the administrator account.
