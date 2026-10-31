# Job origin in NPU summaries

The `作业来源` column replaces RSS in the grouped table. RSS is still collected for other consumers. Duration, ownership/name lookup, grouping and administrator Stop controls retain their existing semantics. Only NPU-reported processes contribute; inspecting ancestors does not add them to the inventory.

| Display | Meaning |
| --- | --- |
| Agent 作业 | Every listed process in this group declares `agent` at startup. |
| 人工作业 | Every listed process declares `manual`, without conflicting agent hints. |
| 疑似 Agent | A known agent executable/path appears in bounded ancestry, or only part of the group declares agent origin. |
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

For existing dashboards, `process_panels.add_job_origin(panels)` preserves Stop overrides and duration, removes RSS from this table, and maps codes to text and colors. Deploy `job_origin.py` alongside `process_exporter.py`; `launch_job.py` is the optional startup helper. Verify all category mappings with browser-only fixtures and check live queries separately. Do not manufacture NPU metrics or stop real workloads to test classification.
