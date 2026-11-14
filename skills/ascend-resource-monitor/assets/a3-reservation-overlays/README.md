# A3 site-specific reservation overlays

These files record the tested A3 queue integrations. They are site-specific snapshots, not drop-in replacements for a different deployment. Preserve the live configuration, node collection, authentication, site-specific takeover policy, and any subsequent changes. Compare and merge with the actual runtime before deploying; back up the files, configuration, and a consistent database snapshot, run isolated tests, then perform a health check. Do not restore an old database over newer reservations.

- `103`: 101/103 queue, 16 cards per node; normal tasks up to two hours, Beijing 02:00–09:00 night boundary; keeps extended employee IDs, Russian UI and read-only container control.
- `140`: 139/140 queue, 16 cards per node; normal tasks up to two hours, Beijing 00:00–09:00 night boundary; preserves the existing site takeover behavior.
- Agent endpoints, collaborator management and notification feeds require the existing compatible runtime and explicit settings; see the separate `a3-reservation` skill and existing WeLink hub documentation. Export tokens are generated and stored locally, not included here.
- The three-miss rule starts counting existing reservations from the migration baseline rather than canceling based on earlier misses. Group violation alerts require an active reservation; repeated extensions notify from the second extension. Test and verify actual messaging delivery per environment.
- Test fixtures operate on temporary databases and mock messaging; never run mutation tests on production data.
