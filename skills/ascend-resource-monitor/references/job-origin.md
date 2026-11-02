# Job origin in NPU summaries

The `作业来源` column replaces RSS in the grouped table. RSS is still collected for other consumers. Duration, ownership/name lookup, grouping and administrator Stop controls retain their existing semantics. Only NPU-reported processes contribute; inspecting ancestors does not add them to the inventory.

| Display | Meaning |
| --- | --- |
| Agent 作业 | Every listed process has an `agent` startup declaration or a matching origin record. |
| 人工作业 | Every listed process has a `manual` declaration/record, without conflicting agent hints. |
| 疑似 Agent | Agent-specific ancestry, linked script or matching record clues, or only part of the group declares agent origin. |
| 未知 | No sufficient evidence; also used for a partly manual, partly unknown group. |
| 混合/待确认 | Manual and agent evidence coexist, or a manual marker conflicts with agent ancestry. |

These categories are descriptive, not authentication or stop authorization. Environment markers can be supplied by the workload owner. A known remote execution directory is a clue, not proof of AI involvement. Existing unmarked SSH/Docker jobs commonly remain unknown; a shell, terminal or employee ID does not prove manual execution.

A script under `/tmp` or `/var/tmp`, a visible SSH ancestor, and an entirely noninteractive observed process chain together produce suspected origin. This combination can also be human-authored automation. Neither a temporary path nor SSH alone qualifies. The collector does not read shell history: noninteractive commands may be absent, history has no reliable PID association, and its contents can expose credentials.

## Declare future workloads

Run the helper in the same Linux environment as the workload. Replace `<scripts>` with this deployment's scripts directory:

```sh
python3 <scripts>/launch_job.py --origin agent -- python3 /path/to/job.py
python3 <scripts>/launch_job.py --origin manual -- python3 /path/to/job.py
```

The helper executes the command with `ASCEND_MONITOR_JOB_ORIGIN=agent` or `manual`; child processes normally inherit it. It does not store full commands, grant permissions or create an audit record. The exporter reads only this exact enum in addition to the existing ownership whitelist. Arbitrary environment values and full parent command lines are not published.

For commands launched inside an existing Docker container, pass the variable to the workload explicitly, for example:

```sh
docker exec -e ASCEND_MONITOR_JOB_ORIGIN=agent <container> python3 /path/to/job.py
```

Use `manual` when the operator is directly starting the workload. A label on a shared container is deliberately insufficient to classify every later process. Do not restart existing workloads merely to add a marker; allow unknown for historical jobs.

## Implementation and maintenance

`job_origin.py` checks at most 12 ancestors per NPU process, caches parent metadata within a sample, checks parent start ticks, and stops at shared daemon/session boundaries such as Docker, SSH and tmux. Only known executable identities and specific paths produce suspected origin; a generic occurrence of the word `agent` does not. There is no permanent PID cache, so a reused PID or new process cannot inherit an old classification.

The exporter emits one numeric `*_process_group_origin` sample per current summary group: 0 unknown, 1 manual, 2 suspected agent, 3 agent, 4 mixed/conflicting. The group includes employee name, ownership evidence, host/container directory and the container control reference. It is recomputed from the same bounded set of NPU rows as the duration query. All processes ending removes the group.

For existing dashboards, `process_panels.add_job_origin(panels)` preserves Stop overrides and duration, removes RSS from this table, and maps codes to text and colors. Deploy `job_origin.py` and `job_evidence.py` alongside `process_exporter.py`; `launch_job.py` is the optional startup helper. Verify all category mappings with browser-only fixtures and check live queries separately. Do not manufacture NPU metrics or stop real workloads to test classification.

## Container script and record evidence

`job_evidence.py` reads the filesystem visible to the current container process through `/proc/<host-pid>/root`. This provides container paths without launching a shell or running code inside a workload. It validates PID start ticks and container identity before and after reading. It inspects only literal script paths from the NPU process and at most 12 ancestors in the same container, including `bash -c` wrappers and an open shell script at fd 255. Within scripts it follows literal shell calls and constant `subprocess` argument lists, at most two levels. Dynamic expressions and arbitrary generated code are not evaluated.

The worker checks only records explicitly named by log redirections/log-file arguments, `<script>.origin.json`, and `.ascend-monitor-origin.json` beside a linked script. It does not search all of `/tmp`, read shell history, or scan unrelated agent conversations. JSON/JSONL records must match the current process or an observed ancestor by one of:

- Exact `process_key` from this exporter's metric: `<boot-id-first-12>:<host-pid>:<start-ticks>`.
- `boot_id`, `host_pid` and `start_ticks` together.
- Full `container_id`, container-namespace `pid` and `process_start_time_seconds` within one second of the observed start.

A minimal compatible record is:

```json
{"process_key":"abcdef123456:1234:56789","origin":"agent","agent_name":"codex"}
```

An exact lifetime match plus `origin: agent|manual` is an explicit declaration, not cryptographic proof. `agent_name` matching a recognized agent is a suspected-origin clue. Merely mentioning the same container, directory or PID is insufficient. Script headers explicitly stating generation by a recognized agent and literal agent-origin assignments are suspected clues. Repeated `container_status`, `health_check` or `poll` records with distinct numeric `timestamp` values, or polling loops in linked scripts, are auxiliary evidence only. An agent checking Docker from another PC may leave no usable record in the container and remain unknown.

Each refresh reads at most 24 files / 512 KiB total, six scripts per process, eight candidate record paths and 12 ancestors. Scripts are capped at 32 KiB; record/log reads at 16 KiB (bounded tail for logs/JSONL). Symlinks and nonregular files below the container root are rejected. Shared script reads are cached within a worker. Refreshes run at most every 30 seconds; identity-keyed results expire after 60 seconds and are removed when the process exits. The subprocess has a two-second deadline; inspection failure leaves the basic classification available. No workload is restarted to obtain evidence.

Only fixed evidence flags, counts and origin categories leave the worker, never script contents, raw command lines, tokens or record text. Exporter metrics `*_process_origin_evidence_jobs` and `*_process_origin_evidence_read_failures` show inspection coverage/status without exposing these files.
