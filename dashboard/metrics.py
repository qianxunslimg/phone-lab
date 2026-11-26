"""System, hardware thermal, battery, network, port, and process telemetry collectors."""
import os
import shutil
import socket
import subprocess
import time
from collections import deque

_prev_cpu = None
_prev_net = None
_history = {
    "cpu": deque(maxlen=60),
    "mem": deque(maxlen=60),
    "rx_kbps": deque(maxlen=60),
    "tx_kbps": deque(maxlen=60),
}


def read_text(path: str, default: str = "") -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read().strip()
    except OSError:
        return default


def read_int(path: str, default: int = 0) -> int:
    try:
        return int(read_text(path, ""))
    except ValueError:
        return default


def get_cpu_usage() -> float:
    global _prev_cpu

    def _sample():
        lines = read_text("/proc/stat").splitlines()
        if not lines:
            return 0, 1
        parts = [int(x) for x in lines[0].split()[1:9]]
        return parts[3] + parts[4], sum(parts)

    idle, total = _sample()
    if _prev_cpu is None:
        _prev_cpu = (idle, total)
        time.sleep(0.12)
        idle, total = _sample()
    prev_idle, prev_total = _prev_cpu
    _prev_cpu = (idle, total)
    d_total = max(total - prev_total, 1)
    return round(100.0 * (1.0 - (idle - prev_idle) / d_total), 1)


def get_cpu_cores() -> list:
    cores = []
    for i in range(os.cpu_count() or 8):
        cur = read_int(f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_cur_freq", 0)
        mx = read_int(f"/sys/devices/system/cpu/cpu{i}/cpufreq/cpuinfo_max_freq", 2_000_000)
        cur_mhz = round(cur / 1000)
        max_mhz = max(round(mx / 1000), 1)
        cores.append({
            "id": i,
            "cur_mhz": cur_mhz,
            "max_mhz": max_mhz,
            "pct": min(round(cur_mhz * 100.0 / max_mhz, 1), 100.0),
        })
    return cores


def get_thermals() -> dict:
    wanted = {
        "mtktscpu": "cpu",
        "mtktsAP": "ap",
        "mtktscharger": "charger",
        "mtktsbattery": "battery",
    }
    res = {}
    base = "/sys/class/thermal"
    try:
        for name in os.listdir(base):
            if not name.startswith("thermal_zone"):
                continue
            sensor = read_text(f"{base}/{name}/type")
            if sensor in wanted:
                raw = read_int(f"{base}/{name}/temp", -127000)
                if raw > -100000:
                    res[wanted[sensor]] = round(raw / 1000.0, 1) if abs(raw) > 200 else round(raw / 10.0, 1)
    except OSError:
        pass
    return res


def get_memory() -> dict:
    info = {}
    for line in read_text("/proc/meminfo").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            info[k.strip()] = int(v.strip().split()[0]) * 1024
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", info.get("MemFree", 0))
    cached = info.get("Cached", 0) + info.get("Buffers", 0)
    used = max(total - avail, 0)
    swap_total = info.get("SwapTotal", 0)
    swap_used = max(swap_total - info.get("SwapFree", 0), 0)
    return {
        "total": total,
        "used": used,
        "avail": avail,
        "cached": cached,
        "pct": round(used * 100.0 / total, 1) if total else 0.0,
        "swap_total": swap_total,
        "swap_used": swap_used,
    }


def get_ip(iface: str = "wlan0") -> str:
    try:
        out = subprocess.check_output(["ip", "-4", "-o", "addr", "show", iface], text=True, timeout=2)
        for part in out.split():
            if "/" in part and part[0].isdigit():
                return part.split("/")[0]
    except Exception:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("223.5.5.5", 80))
            return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"


def get_network(iface: str = "wlan0") -> dict:
    global _prev_net
    now = time.monotonic()
    rx_bytes = tx_bytes = 0
    for line in read_text("/proc/net/dev").splitlines():
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        if name.strip() == iface:
            cols = rest.split()
            rx_bytes, tx_bytes = int(cols[0]), int(cols[8])
            break
    rx_kbps = tx_kbps = 0.0
    if _prev_net is not None:
        dt = max(now - _prev_net[0], 0.1)
        rx_kbps = round(max(rx_bytes - _prev_net[1], 0) / 1024.0 / dt, 1)
        tx_kbps = round(max(tx_bytes - _prev_net[2], 0) / 1024.0 / dt, 1)
    _prev_net = (now, rx_bytes, tx_bytes)
    return {
        "iface": iface,
        "ip": get_ip(iface),
        "rx_bytes": rx_bytes,
        "tx_bytes": tx_bytes,
        "rx_kbps": rx_kbps,
        "tx_kbps": tx_kbps,
    }


def get_disk(path: str, label: str) -> dict:
    try:
        u = shutil.disk_usage(path)
        return {
            "path": path,
            "label": label,
            "total": u.total,
            "used": u.used,
            "free": u.free,
            "pct": round(u.used * 100.0 / u.total, 1) if u.total else 0.0,
        }
    except OSError:
        return {"path": path, "label": label, "total": 0, "used": 0, "free": 0, "pct": 0.0}


def get_battery() -> dict:
    base = "/sys/class/power_supply/battery"
    cap = read_int(f"{base}/capacity", 0)
    status = read_text(f"{base}/status", "Unknown")
    temp_c = round(read_int(f"{base}/temp", 0) / 10.0, 1)
    volt_raw = read_int(f"{base}/voltage_now", 0)
    curr_raw = read_int(f"{base}/current_now", 0)
    cycles = read_text(f"{base}/cycle_count", "-")
    full = read_int(f"{base}/charge_full", 0)
    design = read_int(f"{base}/charge_full_design", 0)

    volt_v = round(volt_raw / 1e6, 2) if volt_raw > 10000 else round(volt_raw / 1e3, 2)
    curr_ma = round(curr_raw / 1000.0) if abs(curr_raw) > 10000 else curr_raw
    power_w = round(abs(volt_v * curr_ma) / 1000.0, 2)
    health_pct = round(full * 100.0 / design, 1) if design > 0 else 0.0

    power_path = read_text("/proc/mtk_battery_cmd/en_power_path", "") == "1"
    current_cmd = read_text("/proc/mtk_battery_cmd/current_cmd", "")
    bypass_active = power_path and (
        current_cmd.endswith("1") or (status in ("Full", "Not charging") and abs(curr_ma) <= 15)
    )
    return {
        "capacity": cap,
        "status": status,
        "temp_c": temp_c,
        "volt_v": volt_v,
        "curr_ma": curr_ma,
        "power_w": power_w,
        "cycles": cycles,
        "health_pct": health_pct,
        "power_path": power_path,
        "current_cmd": current_cmd or "-",
        "bypass_active": bypass_active,
    }


def get_uptime() -> str:
    raw = read_text("/proc/uptime", "0 0").split()
    sec = int(float(raw[0])) if raw else 0
    days, rem = divmod(sec, 86400)
    hours, rem = divmod(rem, 3600)
    mins, _ = divmod(rem, 60)
    return f"{days}天 {hours}小时 {mins}分" if days > 0 else f"{hours}小时 {mins}分"


def get_ports() -> list:
    ports, seen = [], set()
    try:
        out = subprocess.check_output(["ss", "-tlnp"], text=True, timeout=2)
        for line in out.splitlines()[1:]:
            cols = line.split()
            if len(cols) < 5 or ":" not in cols[3]:
                continue
            addr, port = cols[3].rsplit(":", 1)
            proc = "-"
            if len(cols) >= 6 and 'users:(("' in cols[5]:
                proc = cols[5].split('users:(("', 1)[1].split('"', 1)[0]
            key = (addr, port, proc)
            if key not in seen:
                seen.add(key)
                ports.append({"addr": addr, "port": port, "proc": proc})
    except Exception:
        pass
    ports.sort(key=lambda x: int(x["port"]) if x["port"].isdigit() else 99999)
    return ports


def get_top_procs(sort_by: str = "cpu") -> list:
    sort_key = "-%mem" if sort_by == "mem" else "-%cpu"
    rows = []
    try:
        out = subprocess.check_output(
            ["ps", "-eo", "pid,user:12,comm,%cpu,%mem,rss", f"--sort={sort_key}"],
            text=True,
            timeout=2,
        )
        for line in out.splitlines()[1:]:
            parts = line.split(None, 5)
            if len(parts) != 6:
                continue
            if parts[2] == "ps":
                continue
            rows.append({
                "pid": parts[0],
                "user": parts[1],
                "cmd": parts[2],
                "cpu": parts[3],
                "mem": parts[4],
                "rss_mb": round(int(parts[5]) / 1024.0, 1) if parts[5].isdigit() else 0.0,
            })
            if len(rows) >= 12:
                break
    except Exception:
        pass
    return rows


def collect_snapshot(service_status: dict, sort_by: str = "cpu") -> dict:
    cpu_pct = get_cpu_usage()
    mem = get_memory()
    net = get_network()
    _history["cpu"].append(cpu_pct)
    _history["mem"].append(mem["pct"])
    _history["rx_kbps"].append(net["rx_kbps"])
    _history["tx_kbps"].append(net["tx_kbps"])

    load = read_text("/proc/loadavg", "0 0 0").split()[:3]
    return {
        "hostname": read_text("/etc/hostname", socket.gethostname()),
        "uptime": get_uptime(),
        "cores_count": os.cpu_count() or 8,
        "cpu_pct": cpu_pct,
        "cpu_cores": get_cpu_cores(),
        "thermals": get_thermals(),
        "load": " / ".join(load),
        "memory": mem,
        "network": net,
        "disks": [
            get_disk("/", "Ubuntu 根分区 (ext4)"),
            get_disk("/mnt/sdcard", "手机内置存储"),
        ],
        "battery": get_battery(),
        "services": service_status,
        "ports": get_ports(),
        "top_procs": get_top_procs("cpu"),
        "top_procs_mem": get_top_procs("mem"),
        "history": {k: list(v) for k, v in _history.items()},
        "timestamp": time.strftime("%H:%M:%S"),
    }
