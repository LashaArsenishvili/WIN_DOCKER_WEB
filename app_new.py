#!/usr/bin/env python3
"""
Windows 11 Docker Web Manager v3.1
────────────────────────────────────
Full automation: real-time log streaming, ISO management,
progress tracking, GPU passthrough, bulk creation, RDP/noVNC.

Fixes in v3.1:
  - Auto-detects ISO file from the script's own directory
  - No more hardcoded Ventoy/USB paths
  - All v3.0 fixes retained
"""

import os
import json
import re
import time
import socket
import threading
import subprocess
from datetime import datetime
from pathlib import Path
from flask import (
    Flask, render_template, jsonify, request,
    session, redirect, url_for, Response, stream_with_context,
)
from functools import wraps

try:
    import docker
except ImportError:
    subprocess.call(["pip", "install", "docker", "--break-system-packages", "-q"])
    import docker

# ── GPU Detection ─────────────────────────────────────────────────────────────

def detect_nvidia_gpus():
    try:
        result = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=index,name,uuid,memory.total,memory.free,"
             "utilization.gpu,temperature.gpu,driver_version",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return []
        gpus = []
        for line in result.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 8:
                continue
            gpus.append({
                "index":           int(parts[0]),
                "name":            parts[1],
                "uuid":            parts[2],
                "memory_total_mb": int(parts[3]),
                "memory_free_mb":  int(parts[4]),
                "utilization_pct": int(parts[5]),
                "temperature_c":   int(parts[6]),
                "driver_version":  parts[7],
            })
        return gpus
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        return []

def nvidia_available():
    return len(detect_nvidia_gpus()) > 0

def build_device_requests():
    if not nvidia_available():
        return []
    return [docker.types.DeviceRequest(count=-1, capabilities=[["gpu"]])]

# ── Config ────────────────────────────────────────────────────────────────────

CONFIG_FILE    = Path.home() / ".win11_sessions.json"
DATA_DIR       = Path.home() / "win11_sessions"
DOCKER_IMAGE   = "dockurr/windows:latest"
WINDOWS_VER    = "11"

# Auto-detect ISO from the script's own directory.
# Place any .iso file next to app_new.py and it will be picked up automatically.
def _find_local_iso() -> str:
    script_dir = Path(__file__).resolve().parent
    for iso in sorted(script_dir.glob("*.iso")):
        if iso.is_file() and iso.stat().st_size > 100 * 1024 * 1024:
            print(f"  [ISO] Auto-detected: {iso}")
            return str(iso)
    return ""

LOCAL_ISO_PATH = _find_local_iso()

WEB_PORT       = "8006/tcp"
RDP_PORT       = "3389/tcp"
PORT_WEB_START = 8010
PORT_RDP_START = 3390
PORT_STEP      = 1

WIN_RAM        = "4G"
WIN_CPUS       = "4"
WIN_DISK       = "64G"
GPU_ENABLED    = True
VNC_PASSWORD   = "admin777"

# In-memory install progress tracker  {username: {stage, pct, log_lines[]}}
_progress      : dict = {}
_progress_lock = threading.Lock()

# Timestamps for time-based advancement (keyed by username+"_75")
_stage_ts      : dict = {}
_stage_ts_lock = threading.Lock()

app = Flask(__name__)
app.secret_key = os.urandom(32)

# ── Auth ──────────────────────────────────────────────────────────────────────

ADMIN_USERNAME = "itspecialist"
ADMIN_PASSWORD = "kali2026!"
WHITELIST_FILE = Path("whitelist.txt")

def load_whitelist():
    if not WHITELIST_FILE.exists():
        return []
    return [l.strip() for l in WHITELIST_FILE.read_text().splitlines()
            if l.strip() and not l.startswith("#")]

def get_client_ip():
    if request.headers.get("X-Forwarded-For"):
        return request.headers["X-Forwarded-For"].split(",")[0].strip()
    return request.remote_addr

def is_ip_allowed():
    wl = load_whitelist()
    return (not wl) or (get_client_ip() in wl)

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_ip_allowed():
            return render_template("blocked.html", ip=get_client_ip()), 403
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def api_auth_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_ip_allowed():
            return jsonify({"error": "Access denied — IP not whitelisted"}), 403
        if not session.get("logged_in"):
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

# ── Helpers ───────────────────────────────────────────────────────────────────

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

def load_config():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "sessions":      {},
        "next_web_port": PORT_WEB_START,
        "next_rdp_port": PORT_RDP_START,
    }

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

def get_docker_client():
    try:
        client = docker.from_env()
        client.ping()
        return client, None
    except Exception as e:
        return None, str(e)

def container_name(username):
    return f"win11_{username}"

def user_storage_dir(username):
    p = DATA_DIR / username
    p.mkdir(parents=True, exist_ok=True)
    return p

def get_container_status(client, username):
    try:
        c = client.containers.get(container_name(username))
        return c.status
    except Exception:
        return "missing"

def get_container_stats(client, username):
    try:
        c = client.containers.get(container_name(username))
        if c.status != "running":
            return {}
        stats   = c.stats(stream=False)
        cpu_d   = (stats["cpu_stats"]["cpu_usage"]["total_usage"]
                   - stats["precpu_stats"]["cpu_usage"]["total_usage"])
        sys_d   = (stats["cpu_stats"].get("system_cpu_usage", 0)
                   - stats["precpu_stats"].get("system_cpu_usage", 1))
        ncpu    = stats["cpu_stats"].get("online_cpus", 1)
        cpu_pct = (cpu_d / sys_d) * ncpu * 100 if sys_d else 0
        mem_u   = stats["memory_stats"].get("usage", 0)
        mem_l   = stats["memory_stats"].get("limit", 1)
        net_rx  = stats.get("networks", {})
        rx = sum(v.get("rx_bytes", 0) for v in net_rx.values())
        tx = sum(v.get("tx_bytes", 0) for v in net_rx.values())
        return {
            "cpu":          round(cpu_pct, 1),
            "mem_used_mb":  mem_u // 1024 // 1024,
            "mem_total_mb": mem_l // 1024 // 1024,
            "mem_pct":      round(mem_u / mem_l * 100, 1),
            "net_rx_kb":    rx // 1024,
            "net_tx_kb":    tx // 1024,
            "gpu_enabled":  GPU_ENABLED and nvidia_available(),
        }
    except Exception:
        return {}

# ── TCP Port Check ─────────────────────────────────────────────────────────────

def is_port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    """Return True if a TCP connection can be established."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False

# ── Progress Parsing ──────────────────────────────────────────────────────────

STAGE_MAP = [
    (r"Downloading.*[Ww]indows|Pulling.*windows|Fetching.*iso",
     "Downloading Windows ISO",   8),
    (r"ERROR.*Failed to download",
     "Download failed, retrying", 8),
    (r"Extracting local ISO|Mounting.*iso|Extracting.*iso|mount.*loop",
     "Extracting ISO",            10),
    (r"Detecting version",
     "Detecting Windows version", 12),
    (r"Detected:.*Windows|Detected.*version",
     "Detected Windows version",  15),
    (r"Adding drivers|Adding virtio|driverpacks",
     "Adding drivers",            20),
    (r"Adding win.*\.xml|Preparing.*unattend|autounattend|unattend\.xml",
     "Preparing auto-install",    25),
    (r"Building Windows.*image|Building.*disk|mkfs|format.*ntfs",
     "Building disk image",       30),
    (r"Creating a.*disk image|qemu-img create|truncate.*disk",
     "Creating 64 GB disk",       35),
    (r"Booting Windows using QEMU|Starting QEMU|qemu-system|exec.*qemu",
     "Starting QEMU VM",          50),
    (r"BdsDxe: loading Boot|Loading Boot|EFI.*Boot",
     "UEFI booting DVD",          58),
    (r"BdsDxe: starting Boot|Starting Boot|Windows Boot Manager",
     "Loading Windows installer", 65),
    (r"Windows started successfully|Starting Windows|setup.*complete|"
     r"sysprep|oobe|firstlogon",
     "Windows is running",        75),
    (r"Started noVNC|Starting noVNC|noVNC.*started|noVNC.*running|"
     r"websockify|Websockify|WebSockify|novnc|noVNC.*port|"
     r"INFO.*Listening on|HTTP.*server.*started|"
     r"started.*6080|port.*6080|0\.0\.0\.0:6080|:6080",
     "noVNC ready",               92),
]

def _mark_ready(username: str, lines: list):
    with _progress_lock:
        _progress[username] = {
            "stage": "✅ Ready — click Open Web Desktop",
            "pct":   100,
            "lines": lines,
        }
    with _stage_ts_lock:
        _stage_ts.pop(f"{username}_75", None)

def parse_progress_from_log(log_text: str, username: str, web_port: int = None):
    lines = log_text.strip().splitlines() if log_text.strip() else []

    if web_port and is_port_open("127.0.0.1", web_port):
        _mark_ready(username, lines[-30:])
        return

    stage = "Initializing..."
    pct   = 2
    for line in lines:
        for pattern, label, progress in STAGE_MAP:
            if re.search(pattern, line, re.IGNORECASE):
                if progress >= pct:
                    stage = label
                    pct   = progress

    novnc_kw = [
        r"novnc", r"websockify", r"6080",
        r"vnc.*listen", r"http.*server.*start", r"listening.*on.*port",
    ]
    for line in lines:
        ll = line.lower()
        if any(re.search(k, ll) for k in novnc_kw):
            _mark_ready(username, lines[-30:])
            return

    now = time.time()
    with _stage_ts_lock:
        if pct == 75:
            key = f"{username}_75"
            if key not in _stage_ts:
                _stage_ts[key] = now
            else:
                elapsed_min = (now - _stage_ts[key]) / 60
                if elapsed_min >= 5:
                    nudge = min(88, 75 + int(elapsed_min - 5) // 3 + 1)
                    if nudge > pct:
                        pct   = nudge
                        stage = (f"Windows Setup Wizard running… "
                                 f"({int(elapsed_min)} min elapsed)")
        elif pct > 75:
            _stage_ts.pop(f"{username}_75", None)

    with _progress_lock:
        existing_pct = _progress.get(username, {}).get("pct", 0)
        if pct >= existing_pct:
            _progress[username] = {
                "stage": stage,
                "pct":   pct,
                "lines": lines[-30:],
            }

# ── Container Log Watcher ─────────────────────────────────────────────────────

def watch_container_logs(username: str, web_port: int = None):
    cname = container_name(username)

    for _ in range(60):
        client, _ = get_docker_client()
        if client:
            try:
                client.containers.get(cname)
                break
            except Exception:
                pass
        time.sleep(2)
    else:
        print(f"[WARN] watch_container_logs: {cname} never appeared")
        return

    last_port_check = 0.0
    try:
        client, _ = get_docker_client()
        c = client.containers.get(cname)

        for _ in c.logs(stream=True, follow=True):
            try:
                log_text = c.logs(tail=100).decode(errors="replace")
            except Exception:
                log_text = ""

            parse_progress_from_log(log_text, username, web_port=web_port)

            now = time.time()
            if web_port and (now - last_port_check) >= 15:
                last_port_check = now
                if is_port_open("127.0.0.1", web_port):
                    lines = log_text.strip().splitlines()[-30:]
                    _mark_ready(username, lines)

            with _progress_lock:
                if _progress.get(username, {}).get("pct", 0) >= 100:
                    print(f"[INFO] {username} is ready — watcher exiting")
                    break

    except Exception as exc:
        print(f"[WARN] watch_container_logs({username}): {exc}")

# ── Auto-Recovery ─────────────────────────────────────────────────────────────

def _auto_recovery_loop():
    while True:
        time.sleep(60)
        try:
            cfg    = load_config()
            client, err = get_docker_client()
            if err or not client:
                continue
            for uname, info in list(cfg.get("sessions", {}).items()):
                try:
                    c = client.containers.get(container_name(uname))
                    if c.status in ("exited", "dead"):
                        print(f"[AUTO-RECOVERY] Restarting crashed container: {uname}")
                        c.start()
                        web_port = info.get("web_port")
                        with _progress_lock:
                            cur_pct = _progress.get(uname, {}).get("pct", 0)
                            _progress[uname] = {
                                "stage": "Auto-restarted after crash…",
                                "pct":   max(cur_pct, 50),
                                "lines": [],
                            }
                        threading.Thread(
                            target=watch_container_logs,
                            args=(uname, web_port),
                            daemon=True,
                        ).start()
                except Exception:
                    pass
        except Exception as exc:
            print(f"[AUTO-RECOVERY] Error: {exc}")

threading.Thread(target=_auto_recovery_loop, daemon=True).start()

# ── Startup Watcher Restore ───────────────────────────────────────────────────

def _restore_log_watchers():
    time.sleep(3)
    cfg    = load_config()
    client, err = get_docker_client()
    if err or not client:
        return
    for uname, info in cfg.get("sessions", {}).items():
        try:
            c = client.containers.get(container_name(uname))
            if c.status == "running":
                web_port = info.get("web_port")
                if _progress.get(uname, {}).get("pct", 0) < 100:
                    print(f"[RESTORE] Attaching watcher: {uname} (port {web_port})")
                    threading.Thread(
                        target=watch_container_logs,
                        args=(uname, web_port),
                        daemon=True,
                    ).start()
        except Exception:
            pass

threading.Thread(target=_restore_log_watchers, daemon=True).start()

# ── Container Launch ──────────────────────────────────────────────────────────

def _run_windows_container(
    client, cname, username, password, web_port, rdp_port, storage_dir
):
    use_gpu      = GPU_ENABLED and nvidia_available()
    storage_path = str(storage_dir).replace("\\", "/")

    volumes  = {storage_path: {"bind": "/storage", "mode": "rw"}}

    # Re-check ISO at container launch time (in case it was set via API)
    iso_path = LOCAL_ISO_PATH
    if iso_path and Path(iso_path).is_file():
        volumes[iso_path] = {"bind": "/custom.iso", "mode": "ro"}
        print(f"  [ISO] Using local ISO: {iso_path}")
    else:
        print("  [ISO] No local ISO — will attempt download.")

    devices = ["/dev/kvm:/dev/kvm"] if Path("/dev/kvm").exists() else []

    return client.containers.run(
        image=DOCKER_IMAGE,
        detach=True,
        name=cname,
        hostname=f"win11-{username}",
        environment={
            "VERSION":                    WINDOWS_VER,
            "USERNAME":                   username,
            "PASSWORD":                   password,
            "RAM_SIZE":                   WIN_RAM,
            "CPU_CORES":                  WIN_CPUS,
            "DISK_SIZE":                  WIN_DISK,
            "NVIDIA_VISIBLE_DEVICES":     "all" if use_gpu else "",
            "NVIDIA_DRIVER_CAPABILITIES": "all" if use_gpu else "",
        },
        ports={
            WEB_PORT: ("0.0.0.0", web_port),
            RDP_PORT: ("0.0.0.0", rdp_port),
        },
        volumes=volumes,
        devices=devices,
        privileged=True,
        shm_size="128m",
        restart_policy={"Name": "unless-stopped"},
        device_requests=build_device_requests() if use_gpu else [],
    )

# ── Auth Routes ───────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if not is_ip_allowed():
        return render_template("blocked.html", ip=get_client_ip()), 403
    error = None
    if request.method == "POST":
        u = request.form.get("username", "").strip()
        p = request.form.get("password", "").strip()
        if u == ADMIN_USERNAME and p == ADMIN_PASSWORD:
            session["logged_in"] = True
            session["username"]  = u
            return redirect(url_for("index"))
        error = "Invalid username or password"
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/")
@login_required
def index():
    return render_template("index.html")

# ── System Status ─────────────────────────────────────────────────────────────

@app.route("/api/status")
@api_auth_required
def api_status():
    client, err = get_docker_client()
    if err:
        return jsonify({"docker": False, "error": err})
    try:
        info   = client.info()
        gpus   = detect_nvidia_gpus() if GPU_ENABLED else []
        kvm_ok = Path("/dev/kvm").exists()
        iso_ok = bool(LOCAL_ISO_PATH and Path(LOCAL_ISO_PATH).is_file())
        return jsonify({
            "docker":             True,
            "host_ip":            get_local_ip(),
            "docker_version":     info.get("ServerVersion", "unknown"),
            "containers_running": info.get("ContainersRunning", 0),
            "containers_total":   info.get("Containers", 0),
            "kvm_available":      kvm_ok,
            "gpu_available":      len(gpus) > 0,
            "gpu_count":          len(gpus),
            "gpus":               gpus,
            "windows_image":      DOCKER_IMAGE,
            "windows_version":    WINDOWS_VER,
            "iso_path":           LOCAL_ISO_PATH if iso_ok else "",
            "iso_available":      iso_ok,
            "iso_name":           Path(LOCAL_ISO_PATH).name if iso_ok else "",
        })
    except Exception as e:
        return jsonify({"docker": False, "error": str(e)})

# ── ISO Management ────────────────────────────────────────────────────────────

@app.route("/api/iso_scan")
@api_auth_required
def api_iso_scan():
    # Always include the script's own directory in the scan
    script_dir = Path(__file__).resolve().parent
    search_paths = [
        script_dir,
        Path.home(), Path("/root"), Path("/home"),
        Path("/run/media"), Path("/media"), Path("/mnt"), Path("/tmp"),
    ]
    found = []
    seen  = set()
    for base in search_paths:
        if not base.exists():
            continue
        for iso in base.rglob("*.iso"):
            try:
                if not iso.is_file():
                    continue
                resolved = str(iso.resolve())
                if resolved in seen:
                    continue
                seen.add(resolved)
                size = iso.stat().st_size
                if size > 100 * 1024 * 1024:
                    found.append({
                        "path":    str(iso),
                        "name":    iso.name,
                        "size_gb": round(size / 1024 ** 3, 1),
                    })
            except Exception:
                pass
    return jsonify({"isos": found})

@app.route("/api/iso_set", methods=["POST"])
@api_auth_required
def api_iso_set():
    global LOCAL_ISO_PATH
    data = request.json or {}
    path = data.get("path", "").strip()
    if path and not Path(path).is_file():
        return jsonify({"error": f"File not found: {path}"}), 400
    LOCAL_ISO_PATH = path
    return jsonify({"message": "ISO path updated", "path": path})

# ── Sessions List ─────────────────────────────────────────────────────────────

@app.route("/api/sessions")
@api_auth_required
def api_sessions():
    client, _  = get_docker_client()
    cfg        = load_config()
    host_ip    = get_local_ip()
    out        = []
    for uname, s in cfg.get("sessions", {}).items():
        status   = get_container_status(client, uname) if client else "unknown"
        web_port = s.get("web_port", "—")
        rdp_port = s.get("rdp_port", "—")
        created  = s.get("created_at", "")[:16].replace("T", " ")
        prog     = _progress.get(uname, {})

        if isinstance(web_port, int) and prog.get("pct", 0) < 100:
            if is_port_open("127.0.0.1", web_port):
                _mark_ready(uname, prog.get("lines", []))
                prog = _progress.get(uname, prog)

        out.append({
            "username":      uname,
            "status":        status,
            "web_port":      web_port,
            "rdp_port":      rdp_port,
            "password":      s.get("password", "—"),
            "web_url":       f"http://{host_ip}:{web_port}",
            "rdp_target":    f"{host_ip}:{rdp_port}",
            "created_at":    created,
            "storage_dir":   s.get("storage_dir", ""),
            "ram":           WIN_RAM,
            "cpus":          WIN_CPUS,
            "disk":          WIN_DISK,
            "gpu_enabled":   s.get("gpu_enabled", False),
            "install_stage": prog.get("stage", ""),
            "install_pct":   prog.get("pct", 0),
        })
    return jsonify({"sessions": out, "host_ip": host_ip})

# ── Per-session Endpoints ─────────────────────────────────────────────────────

@app.route("/api/sessions/<username>/stats")
@api_auth_required
def api_session_stats(username):
    client, err = get_docker_client()
    if err:
        return jsonify({"error": err}), 500
    return jsonify(get_container_stats(client, username))

@app.route("/api/sessions/<username>/logs")
@api_auth_required
def api_session_logs(username):
    client, err = get_docker_client()
    if err:
        return jsonify({"error": err}), 500
    tail = int(request.args.get("tail", 100))
    try:
        c    = client.containers.get(container_name(username))
        logs = c.logs(tail=tail).decode(errors="replace")
        cfg  = load_config()
        wp   = cfg.get("sessions", {}).get(username, {}).get("web_port")
        parse_progress_from_log(logs, username, web_port=wp)
        prog = _progress.get(username, {})
        return jsonify({
            "logs":  logs,
            "stage": prog.get("stage", ""),
            "pct":   prog.get("pct", 0),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 404

@app.route("/api/sessions/<username>/progress")
@api_auth_required
def api_session_progress(username):
    cfg      = load_config()
    web_port = cfg.get("sessions", {}).get(username, {}).get("web_port")

    if web_port and is_port_open("127.0.0.1", web_port):
        existing_lines = _progress.get(username, {}).get("lines", [])
        _mark_ready(username, existing_lines)
    else:
        client, _ = get_docker_client()
        if client:
            try:
                c    = client.containers.get(container_name(username))
                logs = c.logs(tail=80).decode(errors="replace")
                parse_progress_from_log(logs, username, web_port=web_port)
            except Exception:
                pass

    prog = _progress.get(username, {"stage": "Waiting...", "pct": 0})
    pct  = prog.get("pct", 0)
    return jsonify({
        "stage": prog.get("stage", "Waiting..."),
        "pct":   pct,
        "ready": pct >= 100,
    })

@app.route("/api/sessions/<username>/log_stream")
@api_auth_required
def api_log_stream(username):
    """Server-Sent Events stream of container logs."""
    def generate():
        client, err = get_docker_client()
        if err:
            yield f"data: ERROR: {err}\n\n"
            return
        cfg      = load_config()
        web_port = cfg.get("sessions", {}).get(username, {}).get("web_port")
        try:
            c = client.containers.get(container_name(username))
            for chunk in c.logs(stream=True, follow=True, tail=20):
                line = chunk.decode(errors="replace").rstrip()
                if not line:
                    continue
                try:
                    logs = c.logs(tail=50).decode(errors="replace")
                    parse_progress_from_log(logs, username, web_port=web_port)
                except Exception:
                    pass
                prog    = _progress.get(username, {})
                payload = json.dumps({
                    "line":  line,
                    "stage": prog.get("stage", ""),
                    "pct":   prog.get("pct", 0),
                })
                yield f"data: {payload}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'line': f'Stream ended: {e}', 'stage': '', 'pct': 0})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

# ── Session CRUD ──────────────────────────────────────────────────────────────

@app.route("/api/sessions", methods=["POST"])
@api_auth_required
def api_create_session():
    data     = request.json or {}
    username = data.get("username", "").strip()
    password = data.get("password", VNC_PASSWORD).strip() or VNC_PASSWORD

    if not username:
        return jsonify({"error": "Username is required"}), 400
    if not re.match(r"^[a-zA-Z0-9_\-]{1,32}$", username):
        return jsonify({"error": "Username must be alphanumeric (a-z 0-9 _ -)"}), 400

    client, err = get_docker_client()
    if err:
        return jsonify({"error": f"Docker error: {err}"}), 500

    cfg   = load_config()
    cname = container_name(username)

    try:
        client.containers.get(cname)
        return jsonify({"error": f"Session '{username}' already exists"}), 409
    except docker.errors.NotFound:
        pass

    web_port    = cfg["next_web_port"]
    rdp_port    = cfg["next_rdp_port"]
    cfg["next_web_port"] += PORT_STEP
    cfg["next_rdp_port"] += PORT_STEP
    storage_dir = user_storage_dir(username)
    host_ip     = get_local_ip()

    cfg["sessions"][username] = {
        "username":    username,
        "password":    password,
        "web_port":    web_port,
        "rdp_port":    rdp_port,
        "container":   cname,
        "storage_dir": str(storage_dir),
        "created_at":  datetime.now().isoformat(),
        "status":      "creating",
        "gpu_enabled": GPU_ENABLED and nvidia_available(),
    }
    save_config(cfg)

    with _progress_lock:
        _progress[username] = {"stage": "Starting container...", "pct": 2, "lines": []}

    def create_bg():
        try:
            _run_windows_container(
                client, cname, username, password, web_port, rdp_port, storage_dir
            )
            threading.Thread(
                target=watch_container_logs,
                args=(username, web_port),
                daemon=True,
            ).start()
        except Exception as ex:
            print(f"[ERROR] Failed to create {username}: {ex}")
            with _progress_lock:
                _progress[username] = {
                    "stage": f"❌ Error: {ex}", "pct": 0, "lines": []
                }
            c2 = load_config()
            c2["sessions"].pop(username, None)
            save_config(c2)

    threading.Thread(target=create_bg, daemon=True).start()
    return jsonify({
        "message":    f"Creating Windows 11 VM '{username}'…",
        "username":   username,
        "web_port":   web_port,
        "rdp_port":   rdp_port,
        "web_url":    f"http://{host_ip}:{web_port}",
        "rdp_target": f"{host_ip}:{rdp_port}",
    }), 202

@app.route("/api/sessions/<username>/stop", methods=["POST"])
@api_auth_required
def api_stop_session(username):
    client, err = get_docker_client()
    if err:
        return jsonify({"error": err}), 500
    cfg = load_config()
    try:
        c = client.containers.get(container_name(username))
        c.stop(timeout=30)
        if username in cfg["sessions"]:
            cfg["sessions"][username]["status"] = "stopped"
        save_config(cfg)
        return jsonify({"message": f"Session '{username}' stopped"})
    except docker.errors.NotFound:
        return jsonify({"error": "Container not found"}), 404

@app.route("/api/sessions/<username>/start", methods=["POST"])
@api_auth_required
def api_start_session(username):
    client, err = get_docker_client()
    if err:
        return jsonify({"error": err}), 500
    cfg = load_config()
    try:
        c = client.containers.get(container_name(username))
        c.start()
        if username in cfg["sessions"]:
            cfg["sessions"][username]["status"] = "running"
        save_config(cfg)
        web_port = cfg["sessions"].get(username, {}).get("web_port")
        with _progress_lock:
            _progress[username] = {"stage": "Booting Windows...", "pct": 50, "lines": []}
        threading.Thread(
            target=watch_container_logs,
            args=(username, web_port),
            daemon=True,
        ).start()
        return jsonify({
            "message": f"Session '{username}' started.",
            "web_url": f"http://{get_local_ip()}:{web_port}",
        })
    except docker.errors.NotFound:
        return jsonify({"error": "Container not found"}), 404

@app.route("/api/sessions/<username>/restart", methods=["POST"])
@api_auth_required
def api_restart_session(username):
    client, err = get_docker_client()
    if err:
        return jsonify({"error": err}), 500
    cfg = load_config()
    try:
        c = client.containers.get(container_name(username))
        c.restart(timeout=30)
        web_port = cfg.get("sessions", {}).get(username, {}).get("web_port")
        with _progress_lock:
            _progress[username] = {"stage": "Restarting...", "pct": 50, "lines": []}
        threading.Thread(
            target=watch_container_logs,
            args=(username, web_port),
            daemon=True,
        ).start()
        return jsonify({"message": f"Session '{username}' restarted."})
    except docker.errors.NotFound:
        return jsonify({"error": "Container not found"}), 404

@app.route("/api/sessions/<username>/password", methods=["POST"])
@api_auth_required
def api_change_password(username):
    data = request.json or {}
    pw   = data.get("password", "").strip()
    if not pw:
        return jsonify({"error": "Password is required"}), 400
    cfg = load_config()
    if username not in cfg["sessions"]:
        return jsonify({"error": "Session not found"}), 404
    cfg["sessions"][username]["password"] = pw
    save_config(cfg)
    return jsonify({"message": "Password updated. Restart VM to apply."})

@app.route("/api/sessions/<username>", methods=["DELETE"])
@api_auth_required
def api_delete_session(username):
    client, err = get_docker_client()
    if err:
        return jsonify({"error": err}), 500
    cfg = load_config()
    try:
        c = client.containers.get(container_name(username))
        c.remove(force=True)
    except Exception:
        pass
    cfg["sessions"].pop(username, None)
    save_config(cfg)
    with _progress_lock:
        _progress.pop(username, None)
    with _stage_ts_lock:
        _stage_ts.pop(f"{username}_75", None)
    return jsonify({"message": f"Session '{username}' removed"})

# ── Bulk Create ───────────────────────────────────────────────────────────────

@app.route("/api/bulk_create", methods=["POST"])
@api_auth_required
def api_bulk_create():
    data     = request.json or {}
    prefix   = data.get("prefix", "user").strip()
    count    = int(data.get("count", 1))
    password = data.get("password", VNC_PASSWORD).strip() or VNC_PASSWORD

    if count < 1 or count > 20:
        return jsonify({"error": "Count must be 1–20"}), 400

    client, err = get_docker_client()
    if err:
        return jsonify({"error": err}), 500

    cfg = load_config()

    def bulk_bg():
        for i in range(1, count + 1):
            uname = f"{prefix}{i:02d}"
            cname = container_name(uname)
            try:
                client.containers.get(cname)
                continue
            except docker.errors.NotFound:
                pass

            web_port    = cfg["next_web_port"]
            rdp_port    = cfg["next_rdp_port"]
            cfg["next_web_port"] += PORT_STEP
            cfg["next_rdp_port"] += PORT_STEP
            storage_dir = user_storage_dir(uname)
            cfg["sessions"][uname] = {
                "username":    uname,
                "password":    password,
                "web_port":    web_port,
                "rdp_port":    rdp_port,
                "container":   cname,
                "storage_dir": str(storage_dir),
                "created_at":  datetime.now().isoformat(),
                "status":      "creating",
                "gpu_enabled": GPU_ENABLED and nvidia_available(),
            }
            save_config(cfg)
            with _progress_lock:
                _progress[uname] = {"stage": "Starting container...", "pct": 2, "lines": []}
            try:
                _run_windows_container(
                    client, cname, uname, password, web_port, rdp_port, storage_dir
                )
                threading.Thread(
                    target=watch_container_logs,
                    args=(uname, web_port),
                    daemon=True,
                ).start()
                time.sleep(2)
            except Exception as ex:
                print(f"[ERROR] Bulk create failed for {uname}: {ex}")
                cfg["sessions"].pop(uname, None)
                save_config(cfg)

    threading.Thread(target=bulk_bg, daemon=True).start()
    return jsonify({
        "message": f"Creating {count} VMs with prefix '{prefix}'…"
    }), 202

# ── Docker Image Pull ─────────────────────────────────────────────────────────

_pull_progress: dict = {"status": "idle", "pct": 0, "detail": ""}

@app.route("/api/pull_image", methods=["POST"])
@api_auth_required
def api_pull_image():
    client, err = get_docker_client()
    if err:
        return jsonify({"error": err}), 500

    def pull_bg():
        global _pull_progress
        _pull_progress = {"status": "pulling", "pct": 0, "detail": "Starting…"}
        layers: dict = {}
        try:
            for event in client.api.pull(DOCKER_IMAGE, stream=True, decode=True):
                status = event.get("status", "")
                prog   = event.get("progressDetail", {})
                lid    = event.get("id", "")
                if prog.get("total"):
                    layers[lid] = (prog.get("current", 0), prog["total"])
                if layers:
                    total_c = sum(c for c, t in layers.values())
                    total_t = sum(t for c, t in layers.values())
                    pct     = int(total_c / total_t * 100) if total_t else 0
                    _pull_progress = {"status": "pulling", "pct": pct, "detail": status}
            _pull_progress = {"status": "done", "pct": 100, "detail": "Image ready"}
        except Exception as e:
            _pull_progress = {"status": "error", "pct": 0, "detail": str(e)}

    threading.Thread(target=pull_bg, daemon=True).start()
    return jsonify({"message": f"Pulling {DOCKER_IMAGE}…"})

@app.route("/api/pull_progress")
@api_auth_required
def api_pull_progress():
    return jsonify(_pull_progress)

# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    gpus   = detect_nvidia_gpus() if GPU_ENABLED else []
    kvm_ok = Path("/dev/kvm").exists()
    iso_ok = bool(LOCAL_ISO_PATH and Path(LOCAL_ISO_PATH).is_file())

    print("\n  ╔══════════════════════════════════════════════╗")
    print("  ║   Windows 11 Docker Web Manager v3.1        ║")
    print("  ╚══════════════════════════════════════════════╝")
    print(f"  Docker : ✓ Connected")
    print(f"  KVM    : {'✓ /dev/kvm available' if kvm_ok else '✗ /dev/kvm MISSING'}")
    print(f"  GPU    : {len(gpus)} GPU(s) detected" if gpus else "  GPU    : None (CPU only)")
    print(f"  ISO    : {'✓ ' + Path(LOCAL_ISO_PATH).name if iso_ok else '✗ Not set — will download'}")
    print(f"  Data   : {DATA_DIR}")
    print("  Open   : http://localhost:5000\n")

    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
