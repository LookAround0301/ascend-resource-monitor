"""Validated portable configuration shared by the renderer and installer."""
import copy
import ipaddress
import json
import re
from pathlib import Path, PurePosixPath

SLUG = re.compile(r"[a-z][a-z0-9-]{0,27}\Z")
IDENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,39}\Z")
PATH = re.compile(r"/[A-Za-z0-9_./-]+\Z")
HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}\Z")

def validate(config):
    c = copy.deepcopy(config)
    allowed = {"id", "title", "executor_id", "storage_root", "central_node", "nodes",
               "grafana_port", "prometheus_port", "local_port", "anonymous_viewer",
               "retention_days", "retention_gb", "shared_mounts", "network_regex"}
    if set(c) - allowed:
        raise ValueError("Unknown configuration fields: " + ", ".join(sorted(set(c) - allowed)))
    if not SLUG.fullmatch(c.get("id", "")):
        raise ValueError("id must be a lowercase slug (max 28 characters)")
    if not IDENT.fullmatch(c.get("executor_id", "")) or c["executor_id"] in (".", ".."):
        raise ValueError("executor_id must be a single safe directory component")
    root = c.get("storage_root", "")
    if not PATH.fullmatch(root) or ".." in PurePosixPath(root).parts or root == "/":
        raise ValueError("storage_root must be an absolute data directory without spaces or '..'")
    c["storage_root"] = str(PurePosixPath(root))
    c.setdefault("title", "Ascend resource monitor")
    if not isinstance(c["title"], str) or not 1 <= len(c["title"]) <= 120 or any(ord(x) < 32 for x in c["title"]):
        raise ValueError("Invalid title")
    for name, default in (("grafana_port", 3000), ("prometheus_port", 9090), ("local_port", 13000)):
        c.setdefault(name, default)
        if type(c[name]) is not int or not 1024 <= c[name] <= 65535:
            raise ValueError("Invalid port: " + name)
    if c["grafana_port"] == c["prometheus_port"]:
        raise ValueError("Grafana and Prometheus ports must differ")
    c.setdefault("anonymous_viewer", True)
    if type(c["anonymous_viewer"]) is not bool:
        raise ValueError("anonymous_viewer must be boolean")
    for name, default, maximum in (("retention_days", 7, 365), ("retention_gb", 5, 1000)):
        c.setdefault(name, default)
        if type(c[name]) is not int or not 1 <= c[name] <= maximum:
            raise ValueError("Invalid retention: " + name)
    c.setdefault("shared_mounts", ["/mnt/share", "/mnt/weight"])
    if not isinstance(c["shared_mounts"], list):
        raise ValueError("shared_mounts must be a list")
    for path in c["shared_mounts"]:
        if not isinstance(path, str) or not PATH.fullmatch(path) or ".." in PurePosixPath(path).parts:
            raise ValueError("Invalid shared mount")
    c.setdefault("network_regex", ".*")
    if not isinstance(c["network_regex"], str) or len(c["network_regex"]) > 240 or any(ord(x) < 32 for x in c["network_regex"]):
        raise ValueError("Invalid network interface expression")
    nodes = c.get("nodes", [])
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 32:
        raise ValueError("Provide 1 to 32 nodes")
    labels, hosts, ips = set(), set(), set()
    for n in nodes:
        if set(n) - {"name", "host", "ssh_port", "ssh_user", "monitor_ip", "node_port", "npu_port", "process_port", "node_exporter", "expected_npus"}:
            raise ValueError("Unknown node fields")
        if not IDENT.fullmatch(n.get("name", "")) or n["name"] in labels:
            raise ValueError("Node names must be unique safe labels")
        labels.add(n["name"])
        if not HOST.fullmatch(n.get("host", "")) or n["host"] in hosts:
            raise ValueError("SSH hosts must be valid and unique")
        hosts.add(n["host"])
        n.setdefault("ssh_user", "root")
        if not IDENT.fullmatch(n["ssh_user"]):
            raise ValueError("Invalid SSH user")
        address = ipaddress.IPv4Address(n.get("monitor_ip", ""))
        if address.is_unspecified or address.is_multicast or (len(nodes) > 1 and address.is_loopback) or str(address) in ips:
            raise ValueError("Provide unique reachable monitoring IPv4 addresses")
        ips.add(str(address))
        for key, default in (("ssh_port", 22), ("node_port", 9100), ("npu_port", 18082), ("process_port", 18083)):
            n.setdefault(key, default)
            if type(n[key]) is not int or not 1 <= n[key] <= 65535:
                raise ValueError("Invalid node port: " + key)
        ports = [n[k] for k in ("node_port", "npu_port", "process_port")]
        if len(set(ports)) != 3 or set(ports) & {n["ssh_port"], c["grafana_port"], c["prometheus_port"]}:
            raise ValueError("Conflicting service ports")
        n.setdefault("node_exporter", "auto")
        if n["node_exporter"] not in ("auto", "reuse", "install"):
            raise ValueError("node_exporter: auto, reuse or install")
        if "expected_npus" in n and (type(n["expected_npus"]) is not int or not 1 <= n["expected_npus"] <= 256):
            raise ValueError("Invalid expected_npus")
    if c.get("central_node") not in labels:
        raise ValueError("central_node must match a node name")
    return c

def load(path):
    return validate(json.loads(Path(path).read_text(encoding="utf-8-sig")))

def base_path(c):
    return PurePosixPath(c["storage_root"]) / c["executor_id"] / "ascend-monitor" / c["id"]

def client_config(c):
    c = validate(c)
    n = next(n for n in c["nodes"] if n["name"] == c["central_node"])
    return {"id": c["id"], "title": c["title"], "host": n["host"], "ssh_user": n["ssh_user"],
            "ssh_port": n["ssh_port"], "remote_port": c["grafana_port"], "local_port": c["local_port"],
            "dashboard_uid": c["id"], "anonymous_viewer": c["anonymous_viewer"]}
