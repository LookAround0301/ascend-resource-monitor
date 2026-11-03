# Optional personal reservations and two-mode Stop

Use this extension when the user requests personal task queues or individual Stop permissions. Reuse the existing [control proxy and mutual-TLS host agents](container-control.md). The default deployment wizard remains read-only. This implementation reserves whole nodes in FIFO order; it does not execute launch commands, allocate individual cards, or stop tasks merely because another user is waiting.

## Establish the ownership model

Confirm whether startup is manual or automated, and the unit of reservation, when unknown. This implementation supports manual startup and one active reservation per node. Do not deploy it as an automatic executable-job scheduler.

Users register an employee ID, display name and personal password. Names are self-declared, not verified corporate identities. A real Grafana administrator must verify ownership and grant a full immutable container ID to the account before it can submit a reservation. Never grant access from matching names, directory labels, shell history, or a client-supplied role. One container has one account grant. Reassignment is refused while it has an unfinished reservation.

During a reservation, the whole container belongs to that task: claiming it associates existing and later NPU processes of the same container instance. Do not use a shared multi-user container. A user with host SSH/root access can still start jobs outside the queue; the webpage cannot enforce exclusive hardware use against them. Personal registration does not modify the trusted process-name roster or bypass the automatic container guard.

## Installation on an existing control deployment

Keep actual host paths in the workspace-required operator directory on the existing data volume. Preserve config, certificates, guard state, roster and administrator credentials. Back up changed files with neutral timestamped names. No new port is needed.

1. Install the updated `container_agent.py` and `queue_processes.py` on every host. The latter uses the existing `control_common.py` and `process_exporter.py` modules. Verify Linux `os.pidfd_open`, `signal.pidfd_send_signal`, `/proc` visibility, current Docker/NPU access and Python 3.9+.
2. Add `"task_queue":{"operations_dir":"/data/operator/monitor/data/task-queue-operations"}` to each agent's existing JSON. Create that directory mode `0700`, owned by the agent user. Add only that directory to the service's `ReadWritePaths` if `ProtectSystem=strict` is in use. Keep the mTLS listener and Docker permissions unchanged.
3. Install updated `control_proxy.py` plus `queue_http.py`, `queue_store.py`, `queue_ui.py` and `queue_processes.py` on the central host, readable by its dedicated unprivileged service user. Retain existing runtime dependencies. Add `"task_queue":{"database":"/data/operator/monitor/data/task-queue/queue.db"}` to the proxy JSON. Create the DB parent mode `0700`, owned by the proxy user; allow this parent in `ReadWritePaths` for SQLite's WAL sidecars. Keep `UMask=0077`. The proxy must not gain Docker socket access.
4. Apply `patch_dashboard.add_queue_link()` to the existing dashboard JSON. Preserve all existing panels, data-source IDs, metric prefixes, titles, Stop links and guard links. Existing `/a5-control/confirm` links now redirect to `/a5-queue/control` with the same target; keep `targetBlank:true` so Grafana's client router does not swallow the route.
5. Reload systemd only if its configuration changed, then restart the agents and proxy. Do not restart workloads, clear queue data or reset passwords. Check service health, the queue page, the new dashboard link, anonymous viewing, real administrator identity, and every existing scrape target.
6. Generate the Windows access package as the final workflow step. Existing launchers continue to use the same proxy port; users need no new client dependency. Optional HTTPS and the local tunnel access the same server-side accounts and reservations, but browsers have separate sessions on different origins.

## User workflow

1. Open the dashboard's **任务排队 / 我的任务** link. Register an employee ID, name and personal password (10–128 characters), or sign in from any PC.
2. An administrator signs in with the existing Grafana account and grants the user's own container in **管理员：容器授权**. No password is preset for personal accounts. Registration alone has no process-control permission.
3. Select an authorized container and enter a task description to join that node's queue. The container must already be running when claiming; users start it themselves.
4. When first and the node has no active reservation, select **开始使用** and confirm the current process list. Launch the service yourself over SSH. This button only records occupancy.
5. **完成 / 释放** succeeds only when no NPU processes remain in that container instance. **Stop** offers **仅停止 NPU 进程** (TERM, then KILL for remaining selected processes after five seconds) or **停止整个容器** (Docker stop with 30-second grace). The latter affects all container processes, including non-NPU services. Cancelling a waiting reservation never signals anything.
6. Administrators can cancel others' reservations and use the dashboard Stop link directly. Personal users can follow that same link for their own active reservation. The guard/whitelist page still requires a real Grafana administrator.

## Security and failure behavior

Personal passwords use random salts and scrypt hashes in the private SQLite database. Session tokens are random, hashed in the database, HttpOnly and SameSite=Strict; HTTPS cookies are Secure. Do not ship databases, server key files, personal details or local passwords in packages. Password/name forms and API bodies must never enter logs.

Every mutation checks same origin and a custom header, and destructive confirmation tokens expire after 60 seconds and are consumed once. Ownership comes from the stored session account and reservation; administrator authority comes from Grafana. Client-supplied node names are restricted to the configured hosts. No browser-supplied command, hostname or Docker API path is executed.

The host compares full container ID, container start time, current NPU exporter membership, Docker membership and PID start ticks against the confirmation. Process mode opens PID handles and rechecks identity before any signal, avoiding numeric PID reuse. It does not kill non-NPU parent/supervisor processes, so an external launcher can respawn workers. In that case the reservation stays active and reports remaining processes; it does not repeatedly kill unconfirmed workers. A restarted container instance is not substituted for the original target.

Both modes record the actor, mode, target, selected PIDs and result without passwords or commands. Cancellation stays pending until the host's durable operation record confirms the result. After a service interruption, reconcile status without automatically repeating signals. Unknown results retain the reservation; review state before retrying. Protect and back up the SQLite database and operation records with their containing directories.

## Validation

Run `test_queue.py` and `test_queue_http.py` alongside the existing suite. The HTTP tests need aiohttp and fake both Grafana and host agents; they cover cross-account denial, forged roles, grants, FIFO/occupancy, one-use confirmation, stale PIDs, CSRF, both Stop modes and the old link redirect. The Linux PID-handle test signals only its own disposable CPU subprocess and verifies another subprocess survives. Do not test with business containers.

In a real browser verify: anonymous view, registration/login status, administrator grant controls, the actual dashboard link, and the Stop modal's two choices. Preview a real target if needed but do not confirm a production stop for testing. Do not confuse a skipped local aiohttp test with an executed HTTP test; use the server's isolated staging directory if it has the required dependency.
