# Personnel roster management

The `/a5-control/guard` entry now presents a searchable employee-ID/name list.
It does not expose container IDs or container mutations in the personnel page.
The existing container guard state and administrator-only control APIs remain in place.

## Access and persistence

- Only a verified Grafana administrator can read the page and `/a5-control/roster`; responses are not cached.
- Only a verified Grafana administrator can POST `/a5-control/roster/add` or `/a5-control/roster/remove`.
  Same-origin JSON, the existing control header and explicit confirmation are required.
- `personnel_roster.py` accesses only the configured `guard.owners_file` through each
  existing mTLS host agent. There is no caller-selected file, host, command or container.
- Additions and confirmed removals change only the selected entry in `employee_names`. Other mappings, file permissions and
  ownership are preserved. A different name for an existing ID is a conflict, not an overwrite.
- All nodes are read before an addition or removal. Each write carries its observed file digest,
  is atomically installed and audited. An identical retry performs no write.
- Cross-host writes are not a distributed transaction. Partial failures name the
  unconfirmed nodes, retain successful additions, and allow the same ID/name to be retried.
- A recognized employee avoids automatic interception, including existing marks whose recorded employee IDs are all in the valid roster. Marks remain stored; removing the person restores enforcement. No container is automatically started.
- The administrator can enable or disable automatic container stopping from the personnel page. New deployments start disabled; existing explicit settings are preserved during upgrade. Enabling requires readable synchronized rosters on all nodes. The switch persists per node, reports partial failures and does not erase guard marks.
- Administrators can grant or remove urgent-queue permission for people already present with matching names on both nodes. The permission list is stored centrally in the queue database and is initially empty. Removing permission prevents future urgent markings but leaves previously marked tasks unchanged.
- Authorized applicants can mark their own normal waiting or offered task as urgent, either when submitting or afterwards. Urgent tasks enter ahead of ordinary waiters, ordered by when they were marked; active tasks and existing resource offers are not preempted. Night reservations cannot be marked urgent. Cancelling a mark moves that task behind ordinary waiters.
- Personal queue login currently uses an employee ID and name without an independent identity credential. Urgent permissions therefore follow that account identity and do not prevent someone who knows another person's details from impersonating them.

## Upgrade and validation

Install `personnel_roster.py` and the updated `container_agent.py` on both hosts;
install the updated module, proxy and `guard_ui.py` on the center. Preserve the
colleague's deployed unified queue files and configuration. Existing listeners,
certificates, roster data, reservations and container guard state are reused.

Run `test_personnel_roster.py`, `test_personnel_http.py` and the existing control suite.
Test mutations use temporary rosters and fake Grafana. Verify anonymous access is denied,
administrator-only access and form, search, additions/removals, duplicate/conflict handling, partial synchronization
and the existing homepage/queue after deployment. A production verification may
resubmit an existing identical entry to test the idempotent path; do not add fictitious
people to the real allowlist.

Removal requires the displayed ID/name pair and warns about renewed automatic interception. The last roster entry cannot be removed, preserving the nonempty roster invariant.
