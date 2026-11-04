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
- A newly recognized employee avoids future missing-name selection; a container already
  marked by the persistent guard still requires a separate administrator whitelist action.

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
