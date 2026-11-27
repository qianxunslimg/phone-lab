"""Root-level Android SMS & OTP watcher with QQ/SMTP email forwarding.

Reads Android's telephony SQLite databases via /proc/1/root using short-lived
read-only snapshot copies (never locks Android's telephony provider), watches
for incoming SMS via Linux inotify (with low-overhead stat fallback), extracts
OTP codes into email subjects, and supports per-SIM filtering.
"""
import ctypes
import ctypes.util
import email.utils
import html
import json
import os
import re
import select
import shutil
import smtplib
import sqlite3
import ssl
import struct
import tempfile
import threading
import time
from datetime import datetime
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("SMS_CONFIG_PATH", "/etc/phone-sms-forwarder.json"))
LOG_PATH = Path("/var/log/phone-server/sms-forwarder.log")

SMS_DB_CANDIDATES = [
    "/proc/1/root/data/data/com.android.providers.telephony/databases/mmssms.db",
    "/proc/1/root/data/user_de/0/com.android.providers.telephony/databases/mmssms.db",
]
TELEPHONY_DB_CANDIDATES = [
    "/proc/1/root/data/user_de/0/com.android.providers.telephony/databases/telephony.db",
    "/proc/1/root/data/data/com.android.providers.telephony/databases/telephony.db",
]

DEFAULT_CONFIG = {
    "enabled": False,
    "smtp_host": "smtp.qq.com",
    "smtp_port": 465,
    "smtp_ssl": True,
    "smtp_user": "",
    "smtp_pass": "",
    "to_email": "",
    "sim_ids": [],        # empty list => forward ALL SIM cards; otherwise list of int sub_id
    "only_otp": False,    # True => only forward messages with detected verification code
    "last_sms_id": None,  # persisted high-water mark so reboots never re-forward old SMS
    "forwarded_count": 0,
    "last_error": "",
    "last_sent_at": "",
    "history": [],        # recent forwarding records (max 50)
}

_lock = threading.Lock()
_config = None
_started = False
_wake_watcher = threading.Event()


def _log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}\n"
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def _pick_existing_db(candidates: list) -> str:
    best = ""
    best_mtime = -1.0
    for p in candidates:
        try:
            st = os.stat(p)
            if st.st_size > 0 and st.st_mtime > best_mtime:
                best = p
                best_mtime = st.st_mtime
        except OSError:
            continue
    return best


def _open_snapshot_sqlite(db_path: str):
    """Copy SQLite db (+ wal/shm/journal if present) to a temp dir and open read-only."""
    tmpdir = tempfile.mkdtemp(prefix="sms_snap_")
    dst = os.path.join(tmpdir, "snap.db")
    shutil.copyfile(db_path, dst)
    for ext in ("-wal", "-shm", "-journal"):
        src_ext = db_path + ext
        if os.path.exists(src_ext):
            try:
                shutil.copyfile(src_ext, dst + ext)
            except OSError:
                pass
    con = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
    return con, tmpdir


def _close_snapshot(con, tmpdir: str):
    try:
        con.close()
    except Exception:
        pass
    shutil.rmtree(tmpdir, ignore_errors=True)


def get_sim_cards() -> list:
    """Return SIM / eSIM list from Android's telephony.db siminfo table."""
    db_path = _pick_existing_db(TELEPHONY_DB_CANDIDATES)
    if not db_path:
        return []
    con = None
    tmpdir = ""
    try:
        con, tmpdir = _open_snapshot_sqlite(db_path)
        cur = con.cursor()
        cols = {r[1] for r in cur.execute("PRAGMA table_info(siminfo)").fetchall()}
        if not cols:
            return []
        has_embedded = "is_embedded" in cols
        sql = (
            "SELECT _id, sim_id, display_name, carrier_name, number"
            + (", is_embedded" if has_embedded else ", 0")
            + " FROM siminfo ORDER BY sim_id ASC, _id ASC"
        )
        sims = []
        for row in cur.execute(sql).fetchall():
            sub_id, slot_idx, display_name, carrier_name, number, is_emb = row
            slot_label = f"卡{slot_idx + 1}" if isinstance(slot_idx, int) and slot_idx >= 0 else f"SIM#{sub_id}"
            name = (display_name or carrier_name or "未知运营商").strip()
            num = (number or "").strip()
            sims.append({
                "sub_id": int(sub_id),
                "slot": int(slot_idx) if isinstance(slot_idx, int) else -1,
                "slot_label": slot_label,
                "display_name": name,
                "carrier_name": (carrier_name or "").strip(),
                "number": num,
                "is_embedded": bool(is_emb),
                "label": f"{slot_label} · {name}" + (f" ({num})" if num else ""),
            })
        return sims
    except Exception as exc:
        _log(f"read siminfo failed: {exc}")
        return []
    finally:
        if con is not None:
            _close_snapshot(con, tmpdir)


def _sim_label_map() -> dict:
    return {s["sub_id"]: s["label"] for s in get_sim_cards()}


def extract_otp(body: str) -> str:
    """Extract likely verification code (OTP) from Chinese or English SMS text."""
    if not body:
        return ""
    text = body.strip()

    # 1. Explicit keyword before code (Chinese / English)
    kw_before = re.search(
        r"(?:验证码|校验码|动态码|确认码|安全码|登录码|注册码|授权码|激活码|认证码|短信码|提取码|口令"
        r"|verification\s*code|security\s*code|passcode|one[- ]time\s*(?:code|password)|auth(?:entication)?\s*code|otp|pin|code)"
        r"[\s:：为是\-\(\[【]*([0-9]{4,8}|[0-9]{3}-[0-9]{3})\b",
        text,
        re.IGNORECASE,
    )
    if kw_before:
        return kw_before.group(1).replace("-", "")

    # 2. Code before keyword (e.g. "482910 is your verification code" / "482910为您的验证码")
    kw_after = re.search(
        r"\b([0-9]{4,8}|[0-9]{3}-[0-9]{3})[\s,，]*(?:is\s+your|为您的|是您的|用于|验证码|校验码|动态码|code|otp)",
        text,
        re.IGNORECASE,
    )
    if kw_after:
        return kw_after.group(1).replace("-", "")

    # 3. Alphanumeric 6-8 char code explicitly after keyword (e.g. "code: A8K92P")
    alnum = re.search(
        r"(?:验证码|校验码|code|otp)[\s:：为是]+([A-Z0-9]{5,8})\b",
        text,
        re.IGNORECASE,
    )
    if alnum and re.search(r"\d", alnum.group(1)):
        return alnum.group(1)

    return ""


def query_recent_sms(limit: int = 20, after_id: int = None) -> list:
    """Read received SMS (type=1) from Android mmssms.db."""
    db_path = _pick_existing_db(SMS_DB_CANDIDATES)
    if not db_path:
        return []
    con = None
    tmpdir = ""
    try:
        con, tmpdir = _open_snapshot_sqlite(db_path)
        cur = con.cursor()
        cols = {r[1] for r in cur.execute("PRAGMA table_info(sms)").fetchall()}
        sub_col = "sub_id" if "sub_id" in cols else "-1"
        if after_id is not None:
            sql = (
                f"SELECT _id, address, date, body, {sub_col} "
                f"FROM sms WHERE type = 1 AND _id > ? ORDER BY _id ASC"
            )
            rows = cur.execute(sql, (int(after_id),)).fetchall()
        else:
            sql = (
                f"SELECT _id, address, date, body, {sub_col} "
                f"FROM sms WHERE type = 1 ORDER BY _id DESC LIMIT ?"
            )
            rows = cur.execute(sql, (int(limit),)).fetchall()

        sim_map = _sim_label_map()
        items = []
        for sms_id, address, date_ms, body, sub_id in rows:
            sub_int = int(sub_id) if sub_id is not None else -1
            ts_str = ""
            try:
                ts_str = datetime.fromtimestamp(int(date_ms) / 1000.0).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                ts_str = str(date_ms or "")
            body_str = body or ""
            items.append({
                "id": int(sms_id),
                "address": (address or "未知号码").strip(),
                "date_ms": int(date_ms or 0),
                "time": ts_str,
                "body": body_str,
                "sub_id": sub_int,
                "sim_label": sim_map.get(sub_int, f"SIM#{sub_int}" if sub_int > 0 else "未知卡槽"),
                "otp": extract_otp(body_str),
            })
        return items
    except Exception as exc:
        _log(f"query sms failed: {exc}")
        return []
    finally:
        if con is not None:
            _close_snapshot(con, tmpdir)


def _get_max_inbox_id() -> int:
    db_path = _pick_existing_db(SMS_DB_CANDIDATES)
    if not db_path:
        return 0
    con = None
    tmpdir = ""
    try:
        con, tmpdir = _open_snapshot_sqlite(db_path)
        row = con.execute("SELECT COALESCE(MAX(_id), 0) FROM sms WHERE type = 1").fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0
    finally:
        if con is not None:
            _close_snapshot(con, tmpdir)


def _load_config_unlocked() -> dict:
    global _config
    if _config is not None:
        return _config
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.is_file():
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                cfg.update(saved)
        except Exception as exc:
            _log(f"load config error: {exc}")
    if cfg.get("last_sms_id") is None:
        cfg["last_sms_id"] = _get_max_inbox_id()
        _save_config_unlocked(cfg)
    _config = cfg
    return _config


def _save_config_unlocked(cfg: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(CONFIG_PATH)


def send_sms_email(cfg: dict, sms: dict, is_test: bool = False) -> tuple:
    """Send formatted HTML+text email via SMTP SSL/STARTTLS."""
    host = (cfg.get("smtp_host") or "smtp.qq.com").strip()
    port = int(cfg.get("smtp_port") or 465)
    use_ssl = bool(cfg.get("smtp_ssl", True))
    user = (cfg.get("smtp_user") or "").strip()
    passwd = (cfg.get("smtp_pass") or "").strip()
    to_addr = (cfg.get("to_email") or user).strip()

    if not user or not passwd or not to_addr:
        return False, "请先填写完整的发件邮箱、SMTP 授权码和收件邮箱"

    otp = sms.get("otp") or extract_otp(sms.get("body", ""))
    sender = sms.get("address") or "未知号码"
    sim_label = sms.get("sim_label") or "未知卡槽"
    recv_time = sms.get("time") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    body = sms.get("body") or ""

    if is_test:
        subject = f"【测试成功】手机短信转发已就绪 ({sim_label})"
    elif otp:
        subject = f"【验证码 {otp}】{sender} · {sim_label}"
    else:
        subject = f"【新短信】{sender} · {sim_label}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = email.utils.formataddr(("手机短信转发", user))
    msg["To"] = to_addr
    msg["Date"] = email.utils.formatdate(localtime=True)

    plain_lines = []
    if otp:
        plain_lines.append(f"验证码: {otp}")
    plain_lines.extend([
        f"发件人: {sender}",
        f"接收卡: {sim_label}",
        f"时间: {recv_time}",
        "",
        "短信内容:",
        body,
    ])
    msg.attach(MIMEText("\n".join(plain_lines), "plain", "utf-8"))

    otp_block = ""
    if otp:
        otp_block = (
            f'<div style="background:#0f172a;border:1px solid #38bdf8;border-radius:10px;'
            f'padding:14px 18px;margin-bottom:14px;text-align:center;">'
            f'<div style="font-size:12px;color:#94a3b8;margin-bottom:4px;">识别到验证码</div>'
            f'<div style="font-size:30px;font-weight:700;letter-spacing:4px;color:#38bdf8;'
            f'font-family:monospace;">{html.escape(otp)}</div></div>'
        )
    html_body = (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;'
        'max-width:520px;margin:0 auto;padding:18px;background:#111827;color:#e5e7eb;'
        'border-radius:14px;border:1px solid #1f2937;">'
        f'{otp_block}'
        '<div style="font-size:13px;color:#9ca3af;line-height:1.7;margin-bottom:12px;">'
        f'<div><b>发件人：</b>{html.escape(sender)}</div>'
        f'<div><b>接收卡：</b>{html.escape(sim_label)}</div>'
        f'<div><b>时间：</b>{html.escape(recv_time)}</div>'
        '</div>'
        '<div style="background:#1f2937;border-radius:10px;padding:14px;font-size:15px;'
        f'line-height:1.65;color:#f9fafb;white-space:pre-wrap;word-break:break-word;">'
        f'{html.escape(body)}</div>'
        '<div style="margin-top:12px;font-size:11px;color:#6b7280;">'
        'Sent by Phone Mini-Server SMS Forwarder</div></div>'
    )
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        ctx = ssl.create_default_context()
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, timeout=15, context=ctx) as server:
                server.login(user, passwd)
                server.sendmail(user, [to_addr], msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=15) as server:
                server.ehlo()
                server.starttls(context=ctx)
                server.login(user, passwd)
                server.sendmail(user, [to_addr], msg.as_string())
        return True, f"已发送至 {to_addr}"
    except smtplib.SMTPAuthenticationError as exc:
        return False, f"SMTP 认证失败（请确认填写的是 QQ 邮箱 16 位授权码而非 QQ 密码）: {exc.smtp_error}"
    except Exception as exc:
        return False, f"邮件发送失败: {exc}"


def _should_forward(cfg: dict, sms: dict) -> tuple:
    sim_ids = cfg.get("sim_ids") or []
    if sim_ids and sms.get("sub_id") not in sim_ids:
        return False, f"已过滤（非目标 SIM 卡: {sms.get('sim_label')}）"
    if cfg.get("only_otp") and not sms.get("otp"):
        return False, "已过滤（未检测到验证码）"
    return True, ""


def _record_history(cfg: dict, sms: dict, status: str, detail: str):
    hist = list(cfg.get("history") or [])
    hist.insert(0, {
        "sms_id": sms.get("id", 0),
        "address": sms.get("address", ""),
        "sim_label": sms.get("sim_label", ""),
        "otp": sms.get("otp", ""),
        "body": (sms.get("body") or "")[:120],
        "time": sms.get("time", ""),
        "forwarded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status": status,
        "detail": detail,
    })
    cfg["history"] = hist[:50]


def _process_new_sms():
    with _lock:
        cfg = _load_config_unlocked()
        enabled = bool(cfg.get("enabled"))
        last_id = int(cfg.get("last_sms_id") or 0)
        cfg_snapshot = dict(cfg)

    new_msgs = query_recent_sms(after_id=last_id)
    if not new_msgs:
        return

    for sms in new_msgs:
        sms_id = sms["id"]
        if not enabled:
            with _lock:
                cfg = _load_config_unlocked()
                if sms_id > int(cfg.get("last_sms_id") or 0):
                    cfg["last_sms_id"] = sms_id
                    _save_config_unlocked(cfg)
            continue

        allowed, reason = _should_forward(cfg_snapshot, sms)
        if not allowed:
            _log(f"SMS #{sms_id} from {sms['address']} skipped: {reason}")
            with _lock:
                cfg = _load_config_unlocked()
                cfg["last_sms_id"] = max(int(cfg.get("last_sms_id") or 0), sms_id)
                _record_history(cfg, sms, "skipped", reason)
                _save_config_unlocked(cfg)
            continue

        ok, detail = False, ""
        for attempt, delay in enumerate((0, 3, 10), start=1):
            if delay:
                time.sleep(delay)
            ok, detail = send_sms_email(cfg_snapshot, sms, is_test=False)
            if ok:
                break
            _log(f"SMS #{sms_id} attempt {attempt} failed: {detail}")

        with _lock:
            cfg = _load_config_unlocked()
            cfg["last_sms_id"] = max(int(cfg.get("last_sms_id") or 0), sms_id)
            if ok:
                cfg["forwarded_count"] = int(cfg.get("forwarded_count") or 0) + 1
                cfg["last_sent_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cfg["last_error"] = ""
                _record_history(cfg, sms, "ok", detail)
                _log(f"SMS #{sms_id} ({sms['sim_label']}) from {sms['address']} forwarded -> {cfg.get('to_email')}")
            else:
                cfg["last_error"] = detail
                _record_history(cfg, sms, "error", detail)
            _save_config_unlocked(cfg)


def _setup_inotify(watch_dir: str):
    """Best-effort Linux inotify fd via ctypes (zero external packages)."""
    try:
        libc_name = ctypes.util.find_library("c") or "libc.so.6"
        libc = ctypes.CDLL(libc_name, use_errno=True)
        IN_NONBLOCK = 0x800
        IN_CLOEXEC = 0x80000
        IN_CLOSE_WRITE = 0x00000008
        IN_MOVED_TO = 0x00000080
        IN_MODIFY = 0x00000002
        fd = libc.inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
        if fd < 0:
            return -1
        mask = IN_CLOSE_WRITE | IN_MOVED_TO | IN_MODIFY
        wd = libc.inotify_add_watch(fd, watch_dir.encode("utf-8"), mask)
        if wd < 0:
            os.close(fd)
            return -1
        return fd
    except Exception as exc:
        _log(f"inotify unavailable, using stat fallback: {exc}")
        return -1


def _db_signature(db_path: str):
    sig = []
    for suffix in ("", "-wal", "-journal"):
        p = db_path + suffix
        try:
            st = os.stat(p)
            sig.append((st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append((0, 0))
    return tuple(sig)


def _watcher_loop():
    _log("SMS forwarder watcher thread started")
    db_path = _pick_existing_db(SMS_DB_CANDIDATES)
    inotify_fd = _setup_inotify(os.path.dirname(db_path)) if db_path else -1
    if inotify_fd >= 0:
        _log(f"watching SMS DB dir with inotify: {os.path.dirname(db_path)}")
    last_sig = _db_signature(db_path) if db_path else None

    while True:
        try:
            triggered = False
            if inotify_fd >= 0:
                rlist, _, _ = select.select([inotify_fd], [], [], 3.0)
                if rlist:
                    try:
                        os.read(inotify_fd, 4096)
                    except OSError:
                        pass
                    # Brief settle so Android telephony finishes committing SQLite transaction
                    time.sleep(0.35)
                    triggered = True
            else:
                time.sleep(3.0)

            cur_db = _pick_existing_db(SMS_DB_CANDIDATES)
            if cur_db and cur_db != db_path:
                db_path = cur_db
                last_sig = None

            if db_path:
                sig = _db_signature(db_path)
                if triggered or sig != last_sig:
                    last_sig = sig
                    _process_new_sms()
        except Exception as exc:
            _log(f"watcher loop error: {exc}")
            time.sleep(3.0)


def start_watcher():
    global _started
    with _lock:
        if _started:
            return
        _load_config_unlocked()
        _started = True
    threading.Thread(target=_watcher_loop, name="sms-forwarder", daemon=True).start()


def get_status() -> dict:
    with _lock:
        cfg = dict(_load_config_unlocked())
    sims = get_sim_cards()
    recent = query_recent_sms(limit=20)
    db_path = _pick_existing_db(SMS_DB_CANDIDATES)
    return {
        "ok": True,
        "db_available": bool(db_path),
        "config": {
            "enabled": bool(cfg.get("enabled")),
            "smtp_host": cfg.get("smtp_host", "smtp.qq.com"),
            "smtp_port": int(cfg.get("smtp_port", 465)),
            "smtp_ssl": bool(cfg.get("smtp_ssl", True)),
            "smtp_user": cfg.get("smtp_user", ""),
            "smtp_pass_set": bool(cfg.get("smtp_pass")),
            "to_email": cfg.get("to_email", ""),
            "sim_ids": list(cfg.get("sim_ids") or []),
            "only_otp": bool(cfg.get("only_otp", False)),
            "last_sms_id": int(cfg.get("last_sms_id") or 0),
            "forwarded_count": int(cfg.get("forwarded_count") or 0),
            "last_sent_at": cfg.get("last_sent_at", ""),
            "last_error": cfg.get("last_error", ""),
        },
        "sims": sims,
        "recent_sms": recent,
        "history": list(cfg.get("history") or [])[:30],
    }


def update_config(data: dict) -> tuple:
    with _lock:
        cfg = _load_config_unlocked()
        was_enabled = bool(cfg.get("enabled"))

        if "enabled" in data:
            cfg["enabled"] = bool(data["enabled"])
        if "smtp_host" in data and str(data["smtp_host"]).strip():
            cfg["smtp_host"] = str(data["smtp_host"]).strip()
        if "smtp_port" in data:
            try:
                port = int(data["smtp_port"])
                if not (1 <= port <= 65535):
                    return False, "SMTP 端口必须在 1~65535 之间"
                cfg["smtp_port"] = port
            except (TypeError, ValueError):
                return False, "SMTP 端口格式错误"
        if "smtp_ssl" in data:
            cfg["smtp_ssl"] = bool(data["smtp_ssl"])
        if "smtp_user" in data:
            cfg["smtp_user"] = str(data["smtp_user"]).strip()
        if "smtp_pass" in data and str(data["smtp_pass"]).strip():
            cfg["smtp_pass"] = str(data["smtp_pass"]).strip()
        if "to_email" in data:
            cfg["to_email"] = str(data["to_email"]).strip()
        if "sim_ids" in data and isinstance(data["sim_ids"], list):
            clean_ids = []
            for x in data["sim_ids"]:
                try:
                    clean_ids.append(int(x))
                except (TypeError, ValueError):
                    pass
            cfg["sim_ids"] = sorted(set(clean_ids))
        if "only_otp" in data:
            cfg["only_otp"] = bool(data["only_otp"])

        # When turning ON from OFF, advance watermark so old historical messages aren't spammed
        if cfg["enabled"] and not was_enabled:
            cfg["last_sms_id"] = max(int(cfg.get("last_sms_id") or 0), _get_max_inbox_id())

        if cfg["enabled"] and (not cfg.get("smtp_user") or not cfg.get("smtp_pass")):
            cfg["enabled"] = False
            _save_config_unlocked(cfg)
            return False, "配置已保存，但请先填好发件邮箱和 SMTP 授权码再开启转发"

        _save_config_unlocked(cfg)
        _log(
            f"config updated: enabled={cfg['enabled']}, sim_ids={cfg['sim_ids'] or 'ALL'}, "
            f"only_otp={cfg['only_otp']}, to={cfg['to_email']}"
        )
    return True, "短信转发配置已保存并立即生效"


def send_test_email(override: dict = None) -> tuple:
    with _lock:
        cfg = dict(_load_config_unlocked())
    if isinstance(override, dict):
        for k in ("smtp_host", "smtp_user", "to_email"):
            if override.get(k):
                cfg[k] = str(override[k]).strip()
        if override.get("smtp_port"):
            cfg["smtp_port"] = int(override["smtp_port"])
        if "smtp_ssl" in override:
            cfg["smtp_ssl"] = bool(override["smtp_ssl"])
        if override.get("smtp_pass"):
            cfg["smtp_pass"] = str(override["smtp_pass"]).strip()

    sims = get_sim_cards()
    sim_summary = " / ".join(s["label"] for s in sims) if sims else "未检测到 SIM 卡"
    sample = {
        "id": 0,
        "address": "Phone-Mini-Server",
        "sim_label": sim_summary,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "body": "【小主机短信转发】测试验证码 839204，10分钟内有效。收到这封邮件说明你的备用机已成功打通 QQ 邮箱秒推通道！",
        "otp": "839204",
    }
    ok, msg = send_sms_email(cfg, sample, is_test=True)
    with _lock:
        real_cfg = _load_config_unlocked()
        real_cfg["last_error"] = "" if ok else msg
        if ok:
            real_cfg["last_sent_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _save_config_unlocked(real_cfg)
    _log(f"test email -> {'OK' if ok else 'FAIL'}: {msg}")
    return ok, msg


def resend_sms_by_id(sms_id: int) -> tuple:
    with _lock:
        cfg = dict(_load_config_unlocked())
    recent = query_recent_sms(limit=50)
    target = next((m for m in recent if m["id"] == int(sms_id)), None)
    if not target:
        return False, f"未找到短信 #{sms_id}"
    ok, msg = send_sms_email(cfg, target, is_test=False)
    with _lock:
        real_cfg = _load_config_unlocked()
        _record_history(real_cfg, target, "ok" if ok else "error", f"手动重发: {msg}")
        if ok:
            real_cfg["forwarded_count"] = int(real_cfg.get("forwarded_count") or 0) + 1
            real_cfg["last_sent_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            real_cfg["last_error"] = ""
        else:
            real_cfg["last_error"] = msg
        _save_config_unlocked(real_cfg)
    return ok, msg
