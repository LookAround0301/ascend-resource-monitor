# Administrator credentials and upgrades

Use this flow for new deployments and when hardening an existing monitor. Do not put an actual password in a command argument, deployment JSON, archive, reusable skill, diagnostic output, or server-side password file.

## New deployment

The Windows wizard generates an administrator password on the operator's PC, before remote initialization. It stores the credential at `%LOCALAPPDATA%\AscendMonitor\credentials\<deployment-id>.json` with access restricted to that Windows user and SYSTEM. That file is not included in client ZIPs or the shared wizard. If a deployment already has an administrator, reuse its matching local record or prompt for the existing password with hidden input; do not reset it during installation.

For an agent-driven installation, generate or obtain the password in the local protected environment and send it through SSH standard input with `--admin-password-stdin`. Use a transport that supports stdin; do not interpolate it into remote command text or an environment assignment. A local `0600` file or equivalent restricted Windows record can hold the recovery copy. Save it before submitting the initialization so interruption does not lose access.

`admin_credentials.py` initializes Grafana with its own CLI and `--password-from-stdin`. The CLI uses an independent, short-lived random bootstrap value in its process environment while it creates the database; the final login password enters stdin. The long-running service never receives `GF_SECURITY_ADMIN_PASSWORD`. The legacy filename `config/admin.env` now contains only the encryption key and `GF_SECURITY_DISABLE_INITIAL_ADMIN_CREATION=true`.

Grafana stores the administrator password hash in its database. Preserve the encryption key: it protects other stored secrets and is not the login password. The data directory is restricted to `0700`, and SQLite database/WAL files to `0600`. Keep service ownership correct.

## Reinstall and verification

An existing administrator is never reset by a normal install. `config/admin-initialized.json` records that initialization completed. If the marker remains but the database or administrator disappears, stop and restore/inspect the database instead of silently creating a default account.

The deploy and verify commands accept `--admin-password-stdin`. Verification no longer reads a password from a server file. A public Viewer deployment can run read-only checks without a password; an authenticated/private check must receive the local credential through stdin. Never print a password as a successful deployment result: return the local record path instead.

After installation, verify login, administrator role, the dashboard and datasource, and that the service environment lacks `GF_SECURITY_ADMIN_PASSWORD`. If Stop is enabled, verify its authorization and confirmation flow using the same session; do not stop a business container to test credentials.

## Existing plaintext copies

Before changing an existing installation, inventory its known credential locations. `harden_owned_files` removes bootstrap-password entries from owned `.env` files and small `.tar.gz` templates under `config`, `scripts`, `packages` and `backups`; it preserves encryption keys and other archive members. It does not back up plaintext credentials on the server. It refuses credential symlinks and never extracts an archive onto the filesystem.

For older custom deployments, also inspect their specific staging directories, logs and shell history for disclosed values. Sanitize only the credential and authorized paths; do not delete unrelated history, workloads or system configuration. Never print matching lines. Check archives by member, not only by scanning compressed bytes. Large vendor software distributions are not credential templates.

If credentials may already have been disclosed, rotate them through the authenticated Grafana API or its stdin CLI, update only the protected local copy, and revoke old sessions. Password changes can already invalidate every session; Grafana may reject the global self-logout endpoint. Check the user's auth-token list and revoke any remaining tokens individually, then verify a fresh login. An ambiguous password-change result must be checked with the saved candidate before retrying.

These controls prevent routine plaintext discovery. They do not isolate mutually untrusted host administrators: root can read process memory, change the database, reset Grafana credentials or operate Docker directly. Separate server privileges when that is the required boundary.
