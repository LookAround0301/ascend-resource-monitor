# A3 night carryover cutoff, deployed 2026-11-11

Both A3 queue services now use `task_queue.night_carryover_cutoff_hour: 9.5` in `Asia/Shanghai`. The corresponding A5 setting is also 9.5. This ends reservation registration by 09:30 Beijing (04:30 Moscow); it does not stop containers at the deadline. Pending night reservations cannot take over after this boundary. Existing running registrations and delayed starts are bounded by the original night date, not a rolling next-day deadline.

These patches record the deployed site-specific source changes against the old SHA-256 values in the manifests. They are not generic upgrades. Preserve site configuration, take a consistent SQLite backup, and verify source hashes before applying to a copy. Configure the cutoff explicitly; the patches alone do not update proxy.json. Do not restore a database over subsequent user activity.

101/103 retains its 02:00/03:00 Beijing ordinary-task boundaries, 16-card nodes, Russian/Moscow display and disabled business-container control. 139/140 retains its ordinary-task boundaries and existing night handoff permissions.

Validation covered truncation at enqueue, 09:29:59 versus 09:30, delayed-start rejection and the absence of container stops at the cutoff. All three live services returned the configured cutoff and healthy queue APIs after deployment.
