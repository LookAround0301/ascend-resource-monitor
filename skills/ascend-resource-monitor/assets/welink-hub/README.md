# Optional messaging hub for reservation queues

This integration relays reservation information to a compatible messaging platform. It is not enabled by the base monitor deployment and requires a separate service, platform credentials, and operator-managed configuration. The endpoint URLs, employee IDs, group IDs, tokens, host addresses, keys, and live queue data belong in private configuration, not in this repository.

## Components

- `src/export_snapshot.py` reads one local queue state endpoint and a SQLite connection opened with `mode=ro`. It writes an atomic, minimal snapshot; `src/read_snapshot.py` is a forced SSH command that only returns a fresh snapshot. Each queue keeps its own database and exporter.
- `src/hub.py` reads local snapshots or uses the restricted `ascendwelinkro` SSH account, validates freshness, serves callbacks, and sends private notifications. The mobile release/defer channel uses the separate `ascendwelinkcmd` account and `src/command_bridge.py`; both accounts need restricted forced commands, source restrictions, pinned host keys, and no general shell or database access. Those permissions and systemd units must be installed and reviewed by the operator.
- `queue_notifications.py`, `queue_group_alerts.py`, and `queue_mobile.py` live in `assets/runtime`. The queue's localhost-only notification and command endpoints require separate token files configured in `task_queue`. Keep those files readable only by the intended service accounts.
- `src/group_notifications.py` sends only confirmed, attributable unauthorized use of another active/ready reservation, and separate announcements from the second extension onwards. It requires explicit group configuration and verified mention behavior; a platform-accepted send is not proof of delivery. Two different fresh samples, at least 10 seconds apart, are required for the unauthorized-use announcement; cooldown and delivery records persist across restarts.

The reference callback format does not verify a cryptographic sender signature. A private path is not identity authentication. Writes therefore require a short-lived, one-time code delivered privately to the claimed employee; a command is executed only after confirmation. Use the platform's authenticated callback mechanism if available, and protect access to the callback endpoint and tokens. Delivery attempts with unknown outcomes are persisted rather than blindly repeated.

## Configuration outline

Enable the queue integrations explicitly in the private `task_queue` configuration. For the A5 three-miss policy set `"missed_claim_limit": 3`; omit it or set it to `0` to preserve the existing indefinite deferral behavior. This setting is per deployment: do not automatically enable it on A3. `"night_carryover_cutoff_hour": 9.5` is a separate, opt-in local-time cutoff requiring the existing strict-night and normal-priority modes.

Set `notification_export_token_file` and `mobile_command_token_file` to distinct private files when enabling their respective queue endpoints. The hub JSON includes `database`, `bind`, `port`, `owner`, `token_file`, `send_url`, `sources`, and exact callback `routes`. Remote sources additionally require `known_hosts` and `ssh_key`. `send_url` and `publish_url` are deployment-specific platform endpoints; `publish_url` is required only when automated plugin-list synchronization is enabled via `published_config`. No organization-specific endpoint is provided here.

Set `reminders_enabled` and notification recipients deliberately. Group notifications require an explicit `group_notifications` object with `enabled`, `mention_verified`, `cluster`, and the authorized `group` ID. Mobile writes require `mobile_commands.enabled`, a restricted command key and host, and a route with `allow_actions`. Existing pilot delivery records can be migrated without replay by explicitly setting `legacy_reminder_employee`; leave it unset when no legacy import is required. No example identifiers here represent real accounts or groups.

Snapshot readers reject stale data. The queue still rejects stale or incomplete NPU inventories: keep node clocks synchronized rather than weakening freshness checks. When comparing an exported group candidate against its feed, both timestamps are checked using the source clock, while overall feed age is checked separately by the hub.

## Verification

With Python and `aiohttp` installed, run from the repository root:

```sh
python -m unittest discover -s skills/ascend-resource-monitor/assets/welink-hub/tests -p 'test_*.py'
```

These tests use temporary databases and mocked message delivery. Before enabling a deployment, separately verify authorization, process attribution, platform callback and private/group delivery in the intended environment. Do not use another user's live reservation for destructive testing. Back up code and configuration and use SQLite's backup API; never restore a stale database over newer user activity.
