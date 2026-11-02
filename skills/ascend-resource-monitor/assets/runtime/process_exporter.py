#!/usr/bin/env python3
"""Bounded, read-only inventory of processes reported by Ascend NPU Exporter."""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import posixpath
from pathlib import Path
import re
import signal
import subprocess
import threading
import time
import urllib.request
from job_origin import OriginClassifier, SUMMARY_LABELS, agent_hint, environment_origin, summarize_origin, temporary_script
from job_evidence import EvidenceProbe, merge_evidence

try:
    import pwd
except ImportError:
    pwd = None

EMPLOYEE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z][0-9]{8})(?![A-Za-z0-9])")
CONTAINER = re.compile(r"(?:docker[-/])([0-9a-f]{64})(?:[./\n]|$)")
ROOT = Path(__file__).resolve().parent.parent
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def employee_ids(text):
    return sorted(set(x.lower() for x in EMPLOYEE.findall(text or "")))

def employee_name_for(employee, mapping):
    """Look up a roster name without changing process ownership evidence."""
    account = str(employee).strip().lower()
    if not re.fullmatch(r"[a-z][0-9]{8}", account):
        return ""
    names = mapping.get("employee_names", {})
    value = names.get(account, "") if isinstance(names, dict) else ""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    return value if len(value) <= 120 and not any(ord(c) < 32 for c in value) else ""

def username(uid):
    if pwd is None:
        return str(uid)
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)

def parse_stat(text):
    tail = text[text.rfind(")") + 2:].split()
    return {"state": tail[0], "ppid": int(tail[1]), "tty_nr": int(tail[4]),
            "ticks": int(tail[11]) + int(tail[12]),
            "start_ticks": int(tail[19]), "rss_pages": int(tail[21])}

def entrypoint(argv, comm):
    """Only return executable/module/script identity, never arbitrary arguments."""
    if not argv:
        return comm
    exe = posixpath.basename(argv[0])
    if any(s in exe.lower() for s in ("python", "pypy")):
        for i, arg in enumerate(argv[1:], 1):
            if arg == "-m" and i + 1 < len(argv) and re.fullmatch(r"[A-Za-z0-9_.]+", argv[i + 1]):
                return exe + " -m " + argv[i + 1]
            if arg in ("-c", "--command"):
                return exe + " -c [code omitted]"
            if arg.endswith(".py") and not arg.startswith("-") and "://" not in arg:
                return exe + " " + arg
    if exe in ("bash", "sh", "zsh"):
        for arg in argv[1:]:
            if arg in ("-c", "--command"):
                return exe + " -c [command omitted]"
            if arg.endswith(".sh") and not arg.startswith("-"):
                return exe + " " + arg
    return comm if comm else exe

def map_host_path(cwd, mounts):
    matches = []
    for mount in mounts:
        dest = mount.get("Destination", "").rstrip("/") or "/"
        source = mount.get("Source", "")
        if source and (cwd == dest or (dest == "/" or cwd.startswith(dest + "/"))):
            suffix = cwd[len(dest):].lstrip("/") if dest != "/" else cwd.lstrip("/")
            matches.append((len(dest), posixpath.join(source, suffix)))
    return max(matches, default=(0, ""))[1]

def owner_for(row, container, mapping):
    manual = mapping.get("containers", {}).get(container.get("name", ""))
    if manual:
        return str(manual), "人工标注"
    manual = mapping.get("services", {}).get(row.get("service", ""))
    if manual:
        return str(manual), "人工标注"
    for key, source in (("login_user", "登录审计用户"), ("user", "运行用户")):
        found = employee_ids(row.get(key, ""))
        if len(found) == 1:
            return found[0], source
    found = employee_ids(row.get("sudo_user", ""))
    if len(found) == 1:
        return found[0], "SUDO_USER线索"
    explicit = employee_ids(container.get("owner", ""))
    if len(explicit) == 1:
        return explicit[0], "容器标签线索"
    clues = " ".join([row.get("cwd", ""), row.get("host_cwd", ""),
                      row.get("entrypoint", ""), container.get("name", ""),
                      container.get("cwd", "")])
    found = employee_ids(clues)
    if len(found) == 1:
        return found[0], "目录/容器线索"
    return ("待确认", "多个工号线索") if found else ("未知", "无可用工号线索")

def npu_processes(url):
    result = defaultdict(set)
    try:
        with HTTP.open(url, timeout=2) as response:
            text = response.read(2 * 1024 * 1024).decode()
        if not any(line.startswith('npu_chip_info_process_info_num{') for line in text.splitlines()):
            raise RuntimeError('NPU Exporter does not expose process-count metrics')
        for line in text.splitlines():
            if not line.startswith("npu_chip_info_process_info{"):
                continue
            match = re.search(r'process_id="([0-9]+)"', line)
            device = re.search(r'(?:[{,])id="([^"]+)"', line)
            if match and device:
                result[int(match.group(1))].add(device.group(1))
    except (OSError, ValueError) as exc:
        raise RuntimeError("NPU process source unavailable") from exc
    return result

class Inventory:
    def __init__(self, args):
        self.args = args
        self.previous = {}
        self.last_sample = time.monotonic()
        self.containers = {}
        self.last_container_refresh = 0
        self.last_container_success = 0
        self.tick_rate = os.sysconf("SC_CLK_TCK")
        self.page_size = os.sysconf("SC_PAGE_SIZE")
        self.boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()[:12]
        self.boot_time = next(int(s.split()[1]) for s in Path("/proc/stat").read_text().splitlines() if s.startswith("btime "))
        self.body = b""
        self.last_success = 0
        self.stop = threading.Event()

    def refresh_containers(self):
        if time.monotonic() - self.last_container_refresh < 30:
            return
        self.last_container_refresh = time.monotonic()
        try:
            ids = subprocess.run(["docker", "ps", "-q", "--no-trunc"], capture_output=True, text=True, timeout=4, check=True).stdout.split()
            if not ids:
                self.containers = {}
            else:
                fmt = '{"id":{{json .Id}},"name":{{json .Name}},"pid":{{json .State.Pid}},"cwd":{{json .Config.WorkingDir}},"started_at":{{json .State.StartedAt}},"owner":{{json (index .Config.Labels "employee_id")}},"mounts":{{json .Mounts}}}'
                result = subprocess.run(["docker", "inspect", "--format", fmt, *ids[:256]],
                                        capture_output=True, text=True, timeout=6, check=True)
                entries = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
                self.containers = {x["id"]: {**x, "name": x["name"].lstrip("/")} for x in entries if x.get("pid", 0) > 0}
            self.last_container_success = time.time()
        except (OSError, ValueError, subprocess.SubprocessError):
            pass

    def read_process(self, pid):
        path = Path("/proc") / str(pid)
        stat = parse_stat((path / "stat").read_text())
        if stat["state"] == "Z":
            return None
        status = (path / "status").read_text().splitlines()
        uid = int(next(x for x in status if x.startswith("Uid:")).split()[2])
        cgroup = (path / "cgroup").read_text()
        match = CONTAINER.search(cgroup)
        container_id = match.group(1) if match else ""
        container = self.containers.get(container_id, {})
        cwd = os.readlink(path / "cwd")
        args = (path / "cmdline").read_bytes()[:16384].decode("utf-8", "replace").strip("\0").split("\0")
        comm = (path / "comm").read_text().strip()
        login_uid = (path / "loginuid").read_text().strip()
        login_user = username(int(login_uid)) if login_uid.isdigit() and int(login_uid) not in (0, 4294967295) else ""
        sudo_user = ""
        origin_marker = ""
        # Read only the whitelist; no environment values are exposed as labels.
        try:
            environment = (path / "environ").read_bytes()[:65536]
            origin_marker = environment_origin(environment)
            for value in environment.split(b"\0"):
                if value.startswith(b"SUDO_USER="):
                    sudo_user = value.split(b"=", 1)[1].decode("utf-8", "replace")
        except OSError:
            pass
        service = next((s for line in cgroup.splitlines() for s in line.split("/") if s.endswith(".service")), "")
        if service in {"sshd.service", "docker.service", "containerd.service"}:
            service = ""
        return {
            "pid": pid, "ppid": stat["ppid"], "start_ticks": stat["start_ticks"],
            "ticks": stat["ticks"], "rss": max(0, stat["rss_pages"]) * self.page_size,
            "user": username(uid), "login_user": login_user, "sudo_user": sudo_user,
            "name": comm, "entrypoint": entrypoint(args, comm),
            "cwd": cwd, "host_cwd": (map_host_path(cwd, container.get("mounts", [])) or "未映射（容器内部）") if container_id else cwd,
            "container_id": container_id, "container": container.get("name", ""),
            "container_started_at": container.get("started_at", ""),
            "control_ref": (container_id + "@" + container["started_at"]) if container_id and container.get("started_at") else "",
            "container_initial": container.get("pid") == pid,
            "service": service,
            "origin_marker": origin_marker,
            "agent_hint": agent_hint(args[0] if args else '', args, cwd),
            "temporary_script": temporary_script(args[0] if args else '', args),
            "has_tty": bool(stat['tty_nr']),
        }

    def sample(self):
        start = time.monotonic()
        self.refresh_containers()
        try:
            mapping = json.loads(Path(self.args.owners).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            mapping = {}
        npu = npu_processes(self.args.npu_url)
        now = time.monotonic()
        elapsed = max(now - self.last_sample, 0.01)
        new_previous = {}
        rows = []
        origins = OriginClassifier()
        # The device exporter is the authority for which PIDs occupy an NPU.
        # CPU usage, usernames, and container membership must not broaden this list.
        for pid in npu:
            try:
                row = self.read_process(pid)
                if row is None:
                    continue
                key = (row["pid"], row["start_ticks"])
                old = self.previous.get(key)
                cpu = max(0, (row["ticks"] - old) / self.tick_rate / elapsed * 100) if old is not None else 0
                new_previous[key] = row["ticks"]
                container = self.containers.get(row["container_id"], {})
                employee, evidence = owner_for(row, container, mapping)
                devices = sorted(npu.get(row["pid"], set()))
                if not devices:
                    continue
                row.update(employee_id=employee, employee_name=employee_name_for(employee, mapping), attribution=evidence, cpu=cpu,
                           npu_ids=",".join(devices) or "-", npu_count=len(devices))
                group_owner = employee if employee not in {"未知", "待确认"} else row["container"]
                row["directory_group"] = hashlib.sha256(
                    (group_owner + "\0" + row["host_cwd"] + "\0" + row["entrypoint"]).encode()).hexdigest()[:10]
                row["process_key"] = self.boot_id + ":" + str(row["pid"]) + ":" + str(row["start_ticks"])
                row["start_time_seconds"] = self.boot_time + row["start_ticks"] / self.tick_rate
                row["started_at"] = datetime.fromtimestamp(row["start_time_seconds"], timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                row["job_origin"] = origins.classify(row)
                rows.append(row)
            except (OSError, ValueError, StopIteration, IndexError, ProcessLookupError):
                continue
        self.previous, self.last_sample = new_previous, now
        rows.sort(key=lambda row: (row["npu_count"], row["cpu"], row["rss"]), reverse=True)
        total = len(rows)
        rows = rows[:self.args.limit]
        if not hasattr(self, 'evidence_probe'):
            self.evidence_probe = EvidenceProbe()
        evidence = self.evidence_probe.check(rows)
        for row in rows:
            row['job_origin'] = merge_evidence(row['job_origin'], evidence.get(row['process_key'], {}))
        labels = ["pid", "ppid", "user", "employee_id", "employee_name", "attribution", "container", "service",
                  "name", "entrypoint", "cwd", "host_cwd", "npu_ids", "directory_group",
                  "started_at", "process_key", "control_ref"]
        body = [
            "# HELP ascend_process_inventory_success Successful current process inventory.",
            "# TYPE ascend_process_inventory_success gauge",
            "ascend_process_inventory_success 1",
            "# TYPE ascend_process_inventory_timestamp_seconds gauge",
            f"ascend_process_inventory_timestamp_seconds {time.time()}",
            "# TYPE ascend_process_inventory_processes gauge",
            f"ascend_process_inventory_processes {len(rows)}",
            "# TYPE ascend_process_inventory_truncated gauge",
            f"ascend_process_inventory_truncated {int(total > len(rows))}",
            "# TYPE ascend_process_container_metadata_age_seconds gauge",
            f"ascend_process_container_metadata_age_seconds {max(0, time.time() - self.last_container_success)}",
            '# TYPE ascend_process_origin_evidence_jobs gauge',
            f'ascend_process_origin_evidence_jobs {len(evidence)}',
            '# TYPE ascend_process_origin_evidence_read_failures gauge',
            f'ascend_process_origin_evidence_read_failures {int(self.evidence_probe.status in {"timeout", "failed"})}',
        ]
        for metric, key in (("ascend_process_cpu_percent", "cpu"), ("ascend_process_resident_memory_bytes", "rss"),
                            ("ascend_process_start_time_seconds", "start_time_seconds")):
            body.extend([f"# TYPE {metric} gauge"])
            for row in rows:
                pairs = [key_name + "=" + json.dumps(str(row[key_name])[:512], ensure_ascii=False)
                         for key_name in labels]
                body.append(metric + "{" + ",".join(pairs) + "} " + str(row[key]))
        groups = defaultdict(list)
        for row in rows:
            groups[tuple(str(row.get(key, ''))[:512] for key in SUMMARY_LABELS)].append(row['job_origin'])
        body.append('# TYPE ascend_process_group_origin gauge')
        for values, codes in groups.items():
            pairs = [key + '=' + json.dumps(value, ensure_ascii=False) for key, value in zip(SUMMARY_LABELS, values)]
            body.append('ascend_process_group_origin{' + ','.join(pairs) + '} ' + str(summarize_origin(codes)))
        self.body = ("\n".join(body) + "\n").encode("utf-8")
        self.last_success = time.time()
        return {"rows": len(rows), "truncated": total > len(rows), "duration_seconds": time.monotonic() - start}

    def run(self):
        while not self.stop.is_set():
            try:
                self.sample()
            except Exception as exc:
                print("Process collection failed: " + type(exc).__name__, flush=True)
            self.stop.wait(self.args.interval)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-ip", required=True)
    parser.add_argument("--port", type=int, default=18083)
    parser.add_argument("--interval", type=float, default=10)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--npu-url", required=True)
    parser.add_argument("--owners", default=str(ROOT / "config/process-owners.json"))
    args = parser.parse_args()
    inventory = Inventory(args)
    inventory.sample()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ("/metrics", "/healthz"):
                self.send_error(404)
                return
            healthy = bool(inventory.body) and time.time() - inventory.last_success < max(45, args.interval * 3)
            body = inventory.body if self.path == "/metrics" else (b"ok\n" if healthy else b"stale\n")
            self.send_response(200 if healthy else 503)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass

    worker = threading.Thread(target=inventory.run, daemon=True)
    worker.start()
    server = ThreadingHTTPServer((args.listen_ip, args.port), Handler)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(SystemExit()))
    try:
        server.serve_forever()
    finally:
        inventory.stop.set()
        server.server_close()

if __name__ == "__main__":
    main()

