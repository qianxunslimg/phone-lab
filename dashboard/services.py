"""Service inspection, ACC charging config, bypass control, log tailing, and auth helpers."""
import base64
import re
import subprocess
import time
from pathlib import Path

from metrics import read_text

AUTH_FILE = "/etc/phone-server-auth"
ACC_CONFIG_FILE = Path("/mnt/acc-data/config.txt")
MTK_CURRENT_CMD = Path("/proc/mtk_battery_cmd/current_cmd")
MTK_POWER_PATH = Path("/proc/mtk_battery_cmd/en_power_path")

SERVICE_PATTERNS = {
    "sshd": "/usr/sbin/sshd",
    "dashboard": "/opt/phone-dashboard/server.py",
    "filebrowser": "/usr/local/bin/filebrowser",
    "ttyd": "/usr/local/bin/ttyd",
}

LOG_FILES = {
    "dashboard": "/var/log/phone-server/dashboard.log",
    "filebrowser": "/var/log/phone-server/filebrowser.log",
    "ttyd": "/var/log/phone-server/ttyd.log",
    "sms": "/var/log/phone-server/sms-forwarder.log",
}


def get_all_services() -> dict:
    """Single-pass /proc scan for all tracked services (avoids spawning 4x pgrep)."""
    results = {name: {"running": False, "rss_mb": 0.0, "pid": "-"} for name in SERVICE_PATTERNS}
    remaining = set(SERVICE_PATTERNS.keys())
    try:
        for entry in Path("/proc").iterdir():
            if not remaining:
                break
            if not entry.name.isdigit():
                continue
            try:
                raw_cmd = (entry / "cmdline").read_bytes()
                if not raw_cmd:
                    continue
                cmdline = raw_cmd.replace(b"\x00", b" ").decode("utf-8", errors="ignore")
            except OSError:
                continue
            for name in list(remaining):
                if SERVICE_PATTERNS[name] in cmdline:
                    rss_kb = 0
                    for line in read_text(str(entry / "status")).splitlines():
                        if line.startswith("VmRSS:"):
                            rss_kb = int(line.split()[1])
                            break
                    results[name] = {
                        "running": True,
                        "rss_mb": round(rss_kb / 1024.0, 1),
                        "pid": entry.name,
                    }
                    remaining.remove(name)
    except OSError:
        pass
    return results


def inspect_service(pattern: str) -> dict:
    for name, pat in SERVICE_PATTERNS.items():
        if pat == pattern:
            return get_all_services()[name]
    return {"running": False, "rss_mb": 0.0, "pid": "-"}


def verify_basic_auth(headers) -> bool:
    expected = read_text(AUTH_FILE, "")
    if not expected:
        return True
    auth = headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        return base64.b64decode(auth[6:].strip()).decode("utf-8") == expected
    except Exception:
        return False


def tail_log(name: str, lines: int = 80) -> tuple:
    path = LOG_FILES.get(name)
    if not path:
        return False, "Invalid log name"
    content = "\n".join(read_text(path, "(暂无日志)").splitlines()[-lines:])
    return True, content


def control_service(name: str, action: str) -> tuple:
    if name not in ("filebrowser", "ttyd"):
        return False, f"不支持操作服务: {name}"
    if action not in ("start", "stop", "restart"):
        return False, f"不支持的操作: {action}"

    pattern = SERVICE_PATTERNS[name]
    if action in ("stop", "restart"):
        subprocess.run(["pkill", "-f", pattern], stderr=subprocess.DEVNULL)
        time.sleep(0.5)
        if action == "stop":
            return True, f"{name} 已停止"

    if inspect_service(pattern)["running"]:
        return True, f"{name} 已在运行中"

    if name == "filebrowser":
        with open(LOG_FILES["filebrowser"], "a", encoding="utf-8") as logf:
            subprocess.Popen(
                ["/usr/local/bin/filebrowser", "-d", "/etc/filebrowser/filebrowser.db"],
                stdout=logf,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        return True, f"FileBrowser 已{'重启' if action == 'restart' else '启动'}"

    auth = read_text(AUTH_FILE, "")
    login_user = auth.split(":", 1)[0] if ":" in auth else "root"
    with open(LOG_FILES["ttyd"], "a", encoding="utf-8") as logf:
        subprocess.Popen(
            ["/usr/local/bin/ttyd", "-W", "-p", "7681", "-c", auth, "-t", "fontSize=15", "su", "-", login_user],
            stdout=logf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return True, f"ttyd 网页终端已{'重启' if action == 'restart' else '启动'}"


def read_acc_config() -> dict:
    if not ACC_CONFIG_FILE.is_file():
        return {"available": False}
    txt = ACC_CONFIG_FILE.read_text(encoding="utf-8", errors="ignore")

    cap_m = re.search(r"^capacity=\(([^)]+)\)", txt, re.MULTILINE)
    temp_m = re.search(r"^temperature=\(([^)]+)\)", txt, re.MULTILINE)
    idle_m = re.search(r"^prioritizeBattIdleMode=(\S+)", txt, re.MULTILINE)

    cap_parts = cap_m.group(1).split() if cap_m else ["5", "50", "70", "75", "false", "false"]
    temp_parts = temp_m.group(1).split() if temp_m else ["35", "50", "45", "55"]
    idle_val = idle_m.group(1).lower() if idle_m else "true"

    return {
        "available": True,
        "shutdown_capacity": int(cap_parts[0]),
        "cooldown_capacity": int(cap_parts[1]),
        "resume_capacity": int(cap_parts[2]),
        "pause_capacity": int(cap_parts[3]),
        "cooldown_temp": int(temp_parts[0]),
        "max_temp": int(temp_parts[1]),
        "resume_temp": int(re.sub(r"[^0-9]", "", temp_parts[2]) or "45"),
        "shutdown_temp": int(temp_parts[3]),
        "prioritize_idle": idle_val == "true",
        "power_path": read_text(str(MTK_POWER_PATH), "") == "1",
        "current_cmd": read_text(str(MTK_CURRENT_CMD), "-"),
    }


def update_acc_config(data: dict) -> tuple:
    if not ACC_CONFIG_FILE.is_file():
        return False, "未挂载 ACC 配置目录 (/mnt/acc-data)"

    try:
        rc = int(data.get("resume_capacity", 70))
        pc = int(data.get("pause_capacity", 75))
        mt = int(data.get("max_temp", 50))
        rt = int(data.get("resume_temp", 45))
        idle = bool(data.get("prioritize_idle", True))
    except (TypeError, ValueError):
        return False, "参数必须是合法整数"

    if not (15 <= rc < pc <= 100):
        return False, "电量区间需满足 15 <= 复充% < 停充% <= 100"
    if not (30 <= rt < mt <= 55):
        return False, "温度区间需满足 30 <= 降温复充 < 高温停充 <= 55"

    txt = ACC_CONFIG_FILE.read_text(encoding="utf-8", errors="ignore")

    def _replace_cap(m):
        parts = m.group(1).split()
        while len(parts) < 6:
            parts.append("false")
        parts[2] = str(rc)
        parts[3] = str(pc)
        return f"capacity=({' '.join(parts)})"

    def _replace_temp(m):
        parts = m.group(1).split()
        while len(parts) < 4:
            parts.append("55")
        parts[1] = str(mt)
        parts[2] = str(rt)
        return f"temperature=({' '.join(parts)})"

    txt = re.sub(r"^capacity=\(([^)]+)\)", _replace_cap, txt, count=1, flags=re.MULTILINE)
    txt = re.sub(r"^temperature=\(([^)]+)\)", _replace_temp, txt, count=1, flags=re.MULTILINE)
    txt = re.sub(
        r"^prioritizeBattIdleMode=\S+",
        f"prioritizeBattIdleMode={'true' if idle else 'false'}",
        txt,
        count=1,
        flags=re.MULTILINE,
    )
    ACC_CONFIG_FILE.write_text(txt, encoding="utf-8")
    return True, f"ACC 充电策略已更新：{rc}% 复充 / {pc}% 停充，{mt}°C 高温停充（数秒内自动生效）"


def set_hardware_bypass(enable: bool) -> tuple:
    if not MTK_CURRENT_CMD.exists():
        return False, "当前设备不支持 MTK 硬件直驱节点"
    try:
        if MTK_POWER_PATH.exists():
            MTK_POWER_PATH.write_text("1\n", encoding="utf-8")
        MTK_CURRENT_CMD.write_text("0 1\n" if enable else "0 0\n", encoding="utf-8")
        return True, "已切换为硬件直驱停充 (0 1)" if enable else "已恢复正常充电 (0 0)"
    except OSError as exc:
        return False, f"写入硬件节点失败: {exc}"


def drop_memory_caches() -> tuple:
    try:
        subprocess.run(["sync"], timeout=5)
        Path("/proc/sys/vm/drop_caches").write_text("3\n", encoding="utf-8")
        return True, "已清理系统页缓存"
    except Exception as exc:
        return False, f"清理失败: {exc}"
