#!/usr/bin/env python3
"""
Iris (Lite Community Edition) — Estación de Operaciones Remotas en macOS
Creado por Ariel Castro Cavadía (https://www.arielcastrocavadia.com)
Versión Lite derivada de Rocket Launch, liberada para la comunidad de Software Libre.
"""
import sys
import os
import time
import json
import re
import subprocess
import urllib.request
import urllib.parse
import urllib.error
import mimetypes
import threading
import html
import unicodedata
import ssl
import shutil
import tempfile
import platform
import socket

# Priorizar IPv4 (AF_INET) a nivel global en todos los sockets de Python:
# Evita el bloqueo de 105s en SYN_SENT cuando el proveedor ISP tiene ruta IPv6 rota hacia api.telegram.org.
_ORIG_GETADDRINFO = socket.getaddrinfo
def _ipv4_priority_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if family == 0:
        try:
            res4 = _ORIG_GETADDRINFO(host, port, socket.AF_INET, type, proto, flags)
            if res4:
                return res4
        except Exception:
            pass
    return _ORIG_GETADDRINFO(host, port, family, type, proto, flags)
socket.getaddrinfo = _ipv4_priority_getaddrinfo

SSL_CONTEXT = ssl.create_default_context()
try:
    import certifi
    SSL_CONTEXT.load_verify_locations(certifi.where())
except Exception:
    SSL_CONTEXT.check_hostname = False
    SSL_CONTEXT.verify_mode = ssl.CERT_NONE

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from antigravity_stop_hook import format_telegram_html, render_box_table, display_width, pad_to

CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
STATE_PATH = os.path.join(SCRIPT_DIR, "state.json")
SCHEDULE_PATH = os.path.join(SCRIPT_DIR, "schedules.json")
HISTORY_PATH = os.path.join(SCRIPT_DIR, "history.json")
PENDING_DIALOG_PATH = os.path.join(SCRIPT_DIR, "pending_dialog.json")
PLANS_PATH = os.path.join(SCRIPT_DIR, "plans.json")
RELOAD_FLAG_PATH = os.path.join(SCRIPT_DIR, "reload_pending.json")
ACTIVE_CARD_PATH = os.path.join(SCRIPT_DIR, "active_task_card.json")
LOCK_FILE_PATH = os.path.join(SCRIPT_DIR, "iris.lock")
DAEMON_INITIAL_MTIME = os.path.getmtime(os.path.abspath(__file__)) if os.path.exists(os.path.abspath(__file__)) else 0
SUGGESTED_COMMITS = {}
DOWNLOADS_DIR = os.path.join(SCRIPT_DIR, "downloads")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

ACTIVE_TASK_INFO = {
    "running": False,
    "prompt": "",
    "start_time": 0,
    "chat_id": None,
    "cancelled": False
}
TASK_LOCK = threading.Lock()
TASK_QUEUE = []
QUEUE_LOCK = threading.Lock()
QUEUE_WORKER_THREAD = None
SESSION_RESET_FLAG = False
_HOT_RELOAD_LOCK = threading.Lock()
_HOT_RELOAD_SCHEDULED = False
STATE_LOCK = threading.Lock()

def _atomic_write_json(path, data):
    """Escritura atómica en disco mediante archivo temporal y reemplazo POSIX para evitar corrupción por concurrencia."""
    tmp_path = f"{path}.tmp.{os.getpid()}.{threading.get_ident()}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass

def load_history():
    if not os.path.exists(HISTORY_PATH):
        return []
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def append_history(item):
    with STATE_LOCK:
        try:
            hist = load_history()
            hist.insert(0, item)
            hist = hist[:15] # Mantener últimas 15 tareas
            _atomic_write_json(HISTORY_PATH, hist)
        except Exception:
            pass

def load_schedules():
    if not os.path.exists(SCHEDULE_PATH):
        return []
    try:
        with open(SCHEDULE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def save_schedules(schedules):
    with STATE_LOCK:
        try:
            _atomic_write_json(SCHEDULE_PATH, schedules)
        except Exception:
            pass

def load_plans():
    if not os.path.exists(PLANS_PATH):
        return {}
    try:
        with open(PLANS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_plan(plan_id, data):
    with STATE_LOCK:
        try:
            plans = load_plans()
            plans[plan_id] = data
            if len(plans) > 25:
                sorted_keys = sorted(plans.keys(), key=lambda k: plans[k].get("timestamp", 0))
                for old_k in sorted_keys[:-25]:
                    del plans[old_k]
            _atomic_write_json(PLANS_PATH, plans)
        except Exception:
            pass

def get_plan(plan_id):
    try:
        return load_plans().get(plan_id)
    except Exception:
        return None

def parse_time_offset_seconds(text):
    """Parsea expresiones de tiempo como 'en 10m', 'en 2h', 'en 45s' o 'a las 22:30'."""
    text = text.lower().strip()
    m_rel = re.search(r'(?:en\s+)?(\d+)\s*(m|min|mins|minutos?|h|horas?|s|seg|segundos?)', text)
    if m_rel:
        val = int(m_rel.group(1))
        unit = m_rel.group(2)
        if unit.startswith("h"):
            return val * 3600, f"en {val} hora(s)"
        elif unit.startswith("m"):
            return val * 60, f"en {val} minuto(s)"
        else:
            return val, f"en {val} segundo(s)"

    m_at = re.search(r'(?:a las\s+)?(\d{1,2}):(\d{2})', text)
    if m_at:
        target_h = int(m_at.group(1))
        target_m = int(m_at.group(2))
        now_dt = time.localtime()
        target_dt = time.struct_time((
            now_dt.tm_year, now_dt.tm_mon, now_dt.tm_mday,
            target_h, target_m, 0,
            now_dt.tm_wday, now_dt.tm_yday, now_dt.tm_isdst
        ))
        target_ts = time.mktime(target_dt)
        if target_ts <= time.time():
            target_ts += 86400 # Si ya pasó hoy, se programa para mañana
        diff = int(target_ts - time.time())
        return diff, f"a las {target_h:02d}:{target_m:02d}"

    return None, None

def start_scheduler_thread():
    """Hilo de fondo que vigila y dispara tareas programadas con Antigravity en tu Mac."""
    def _scheduler_loop():
        while True:
            time.sleep(5)
            schedules = load_schedules()
            if not schedules:
                continue

            now = time.time()
            remaining = []
            triggered = []

            for item in schedules:
                if item.get("target_timestamp", 0) <= now:
                    triggered.append(item)
                else:
                    remaining.append(item)

            if triggered:
                save_schedules(remaining)
                for job in triggered:
                    cid = job.get("chat_id")
                    prompt = job.get("prompt", "")
                    label = job.get("label", prompt)
                    desc_time = job.get("time_str", "programada")
                    
                    send_msg(
                        cid,
                        f"⏰ <b>DISPARADOR PROGRAMADO ({desc_time}):</b>\n"
                        f"┃ <i>«{prompt}»</i>\n\n"
                        f"🚀 <i>Iniciando ejecución automática en tu Mac...</i>"
                    )
                    dispatch_antigravity_task(cid, prompt, continue_session=True)

    t = threading.Thread(target=_scheduler_loop, daemon=True)
    t.start()

CAFFEINATE_PROC = None
MANUAL_DECAF = False

def _run_pmset_disablesleep(state: int, quiet: bool = False):
    """Activa (1) o desactiva (0) la protección integral contra reposo en batería, corriente y tapa cerrada en macOS."""
    if sys.platform != "darwin":
        return
    try:
        if state == 1:
            cmd = [
                "sudo", "-n", "/usr/bin/pmset", "-a",
                "disablesleep", "1",
                "sleep", "0",
                "standby", "0",
                "autopoweroff", "0",
                "tcpkeepalive", "1",
                "powernap", "1",
                "womp", "1"
            ]
        else:
            cmd = [
                "sudo", "-n", "/usr/bin/pmset", "-a",
                "disablesleep", "0",
                "sleep", "1",
                "standby", "1"
            ]
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        if res.returncode != 0:
            # Fallback si sudoers solo autoriza 'pmset -a disablesleep *'
            res = subprocess.run(["sudo", "-n", "/usr/bin/pmset", "-a", "disablesleep", str(state)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        if res.returncode == 0 and not quiet:
            status_txt = "TAPA CERRADA Y BATERÍA SIEMPRE ACTIVAS (disablesleep=1, sleep=0)" if state == 1 else "REPOSO NORMAL (disablesleep=0)"
            print(f"[POWER] macOS pmset ajustado: {status_txt}")
    except Exception as e:
        if not quiet:
            print(f"[POWER] Aviso pmset disablesleep: {e}")

def enable_anti_sleep(quiet: bool = False):
    global CAFFEINATE_PROC, MANUAL_DECAF
    MANUAL_DECAF = False
    if sys.platform == "darwin":
        if not CAFFEINATE_PROC or (hasattr(CAFFEINATE_PROC, "poll") and CAFFEINATE_PROC.poll() is not None):
            try:
                subprocess.run(["killall", "caffeinate"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                CAFFEINATE_PROC = subprocess.Popen(["caffeinate", "-d", "-i", "-s", "-u"])
            except Exception as e:
                if not quiet:
                    print(f"[POWER] Error ejecutando caffeinate en macOS: {e}")
        _run_pmset_disablesleep(1, quiet=quiet)
        return True
    elif sys.platform == "win32":
        try:
            import ctypes
            # ES_CONTINUOUS (0x80000000) | ES_SYSTEM_REQUIRED (0x00000001) | ES_AWAYMODE_REQUIRED (0x00000040)
            res = ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001 | 0x00000040)
            if res != 0:
                CAFFEINATE_PROC = "WIN32_AWAKE"
                return True
        except Exception as e:
            print(f"[POWER] Error Win32 SetThreadExecutionState: {e}")
        return False
    else:
        # Linux / WSL
        if shutil.which("systemd-inhibit"):
            if not CAFFEINATE_PROC or (hasattr(CAFFEINATE_PROC, "poll") and CAFFEINATE_PROC.poll() is not None):
                try:
                    CAFFEINATE_PROC = subprocess.Popen(["systemd-inhibit", "--what=idle:sleep", "--who=Iris", "--why=Iris Active", "sleep", "infinity"])
                except Exception as e:
                    print(f"[POWER] Error systemd-inhibit: {e}")
            return True
        CAFFEINATE_PROC = "LINUX_AWAKE"
        return True

def disable_anti_sleep():
    global CAFFEINATE_PROC, MANUAL_DECAF
    MANUAL_DECAF = True
    if sys.platform == "darwin":
        _run_pmset_disablesleep(0)
        if CAFFEINATE_PROC and hasattr(CAFFEINATE_PROC, "poll") and CAFFEINATE_PROC.poll() is None:
            try:
                CAFFEINATE_PROC.terminate()
            except Exception:
                pass
            CAFFEINATE_PROC = None
        subprocess.run(["killall", "caffeinate"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    elif sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
            CAFFEINATE_PROC = None
            return True
        except Exception as e:
            print(f"[POWER] Error Win32 SetThreadExecutionState reset: {e}")
    else:
        if CAFFEINATE_PROC and hasattr(CAFFEINATE_PROC, "poll") and CAFFEINATE_PROC.poll() is None:
            try:
                CAFFEINATE_PROC.terminate()
            except Exception:
                pass
            CAFFEINATE_PROC = None
            return True
        CAFFEINATE_PROC = None
    return True

def is_anti_sleep_active():
    global CAFFEINATE_PROC
    if sys.platform == "win32":
        return CAFFEINATE_PROC == "WIN32_AWAKE"
    elif sys.platform == "darwin":
        return bool(CAFFEINATE_PROC and hasattr(CAFFEINATE_PROC, "poll") and CAFFEINATE_PROC.poll() is None)
    else:
        if CAFFEINATE_PROC == "LINUX_AWAKE":
            return True
        return bool(CAFFEINATE_PROC and hasattr(CAFFEINATE_PROC, "poll") and CAFFEINATE_PROC.poll() is None)

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def load_state():
    defaults = {
        "is_paused": False,
        "mute_notifications": False,
        "voice_mode": False,
        "dante_introduced": False,
        "cerberus_introduced": False,
        "keyboard_page": 1,
        "iris_task_running": False,
        "iris_task_finished_at": 0
    }
    if not os.path.exists(STATE_PATH):
        return defaults
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            defaults.update(data)
            return defaults
    except Exception:
        return defaults

def save_state(updates):
    with STATE_LOCK:
        curr = load_state()
        curr.update(updates)
        _atomic_write_json(STATE_PATH, curr)
        return curr

CONFIG = load_config()
BOT_TOKEN = CONFIG["bot_token"]
ALLOWED_USERS = set(CONFIG["allowed_user_ids"])
CURRENT_CWD = os.path.expanduser(CONFIG.get("default_cwd", "~"))
def resolve_binary(cfg_key, default_fallback):
    cfg_val = CONFIG.get(cfg_key)
    if cfg_val:
        expanded = os.path.expanduser(cfg_val)
        if os.path.exists(expanded):
            return expanded
        which_val = shutil.which(cfg_val)
        if which_val:
            return which_val
    which_fallback = shutil.which(default_fallback)
    if which_fallback:
        return which_fallback
    expanded_fallback = os.path.expanduser(default_fallback)
    if os.path.exists(expanded_fallback):
        return expanded_fallback
    return default_fallback

AGY_BIN = resolve_binary("agy_bin", os.path.expanduser("~/.local/bin/agy") if sys.platform == "darwin" else "agy")
CLAUDE_BIN = resolve_binary("claude_bin", os.path.expanduser("~/.local/bin/claude") if sys.platform == "darwin" else "claude")
AUTO_CAFFEINATE = CONFIG.get("auto_caffeinate", True)
VOICE_NAME = CONFIG.get("voice_name", "Paulina")
_INITIAL_STATE = load_state()
VOICE_MODE = _INITIAL_STATE.get("voice_mode", False)
IS_PAUSED = _INITIAL_STATE.get("is_paused", False)

# Configuración de Dante (Apoyo de Iris para Planeación y Arquitectura)
DANTE_VOICE_NAME = "es-MX-JorgeNeural"
DANTE_FALLBACK_VOICE = "Eddy"
DANTE_PRIMARY_MODEL = "gemini-4-argon-high"
DANTE_FALLBACK_MODEL = "claude-opus-5-5-high"

# Configuración de Cerberus (Especialista en Ciberseguridad y Blindaje bajo el mando de Iris)
CERBERUS_VOICE_NAME = "es-CO-GonzaloNeural"
CERBERUS_VOICE_PITCH = "+0Hz"
CERBERUS_VOICE_RATE = "+2%"
CERBERUS_FALLBACK_VOICE = "Eddy"
CERBERUS_PRIMARY_MODEL = "gemini-4-argon-high"
CERBERUS_FALLBACK_MODEL = "claude-opus-5-5-high"

BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

AVAILABLE_MODELS = [
    {
        "id": "gemini-3.8-flash-low",
        "name": "Gemini 3.8 Flash (Low)",
        "short": "Flash 3.8 Low",
        "emoji": "⚡",
        "desc": "Ultra rápido (5-8s) · Por defecto Google",
        "tags": ["flash", "3.8", "3.8-low", "flash-low", "default", "rapido", "rápido", "1"]
    },
    {
        "id": "gemini-3.8-flash-medium",
        "name": "Gemini 3.8 Flash (Medium)",
        "short": "Flash 3.8 Med",
        "emoji": "⚡",
        "desc": "Equilibrado entre rapidez y análisis",
        "tags": ["flash-med", "flash-medium", "3.8-med", "2"]
    },
    {
        "id": "gemini-3.8-flash-high",
        "name": "Gemini 3.8 Flash (High)",
        "short": "Flash 3.8 High",
        "emoji": "🧠",
        "desc": "Máxima profundidad razonamiento Flash",
        "tags": ["flash-high", "3.8-high", "3"]
    },
    {
        "id": "gemini-4-argon-low",
        "name": "Gemini 4 Argon (Low)",
        "short": "Argon 4 Low",
        "emoji": "💎",
        "desc": "Google 4 de nueva generación (Low)",
        "tags": ["argon", "argon-low", "4-low", "4"]
    },
    {
        "id": "gemini-4-argon-medium",
        "name": "Gemini 4 Argon (Medium)",
        "short": "Argon 4 Med",
        "emoji": "💎",
        "desc": "Google 4 de nueva generación (Med)",
        "tags": ["argon-med", "argon-medium", "4-med", "5"]
    },
    {
        "id": "gemini-4-argon-high",
        "name": "Gemini 4 Argon (High)",
        "short": "Argon 4 High",
        "emoji": "💎",
        "desc": "Google 4 de máxima capacidad (High)",
        "tags": ["argon-high", "4-high", "6"]
    },
    {
        "id": "claude-sonnet-5-5-low",
        "name": "Claude Sonnet 5.5 (Low)",
        "short": "Sonnet 5.5 Low",
        "emoji": "🎯",
        "desc": "Sonnet ágil para código y refactor",
        "tags": ["sonnet", "claude", "sonnet-low", "7"]
    },
    {
        "id": "claude-sonnet-5-5-medium",
        "name": "Claude Sonnet 5.5 (Medium)",
        "short": "Sonnet 5.5 Med",
        "emoji": "🎯",
        "desc": "Sonnet completo para lógica compleja",
        "tags": ["sonnet-med", "sonnet-medium", "8"]
    },
    {
        "id": "claude-sonnet-5-5-high",
        "name": "Claude Sonnet 5.5 (High)",
        "short": "Sonnet 5.5 High",
        "emoji": "🎯",
        "desc": "Sonnet 5.5 con razonamiento intensivo",
        "tags": ["sonnet-high", "9"]
    },
    {
        "id": "claude-opus-5-5-low",
        "name": "Claude Opus 5.5 (Low)",
        "short": "Opus 5.5 Low",
        "emoji": "👑",
        "desc": "Opus máxima capacidad creativa (Low)",
        "tags": ["opus", "opus-low", "10"]
    },
    {
        "id": "claude-opus-5-5-medium",
        "name": "Claude Opus 5.5 (Medium)",
        "short": "Opus 5.5 Med",
        "emoji": "👑",
        "desc": "Opus máxima capacidad creativa (Med)",
        "tags": ["opus-med", "opus-medium", "11"]
    },
    {
        "id": "claude-opus-5-5-high",
        "name": "Claude Opus 5.5 (High)",
        "short": "Opus 5.5 High",
        "emoji": "👑",
        "desc": "Opus máxima capacidad creativa (High)",
        "tags": ["opus-high", "12"]
    },
    {
        "id": "gpt-oss-120b-medium",
        "name": "GPT-OSS 120B (Medium)",
        "short": "GPT-OSS 120B",
        "emoji": "🌐",
        "desc": "Modelo Open Weights 120B",
        "tags": ["gpt", "gpt-oss", "oss", "13"]
    }
]

def find_model_by_query(query):
    q = query.strip().lower()
    for idx, m in enumerate(AVAILABLE_MODELS, 1):
        if q == str(idx) or q == m["id"].lower() or q in m["tags"] or q == m["name"].lower():
            return m
    for m in AVAILABLE_MODELS:
        if q in m["id"].lower() or q in m["name"].lower():
            return m
    return None

def api_call(endpoint, data=None, files=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{endpoint}"
    
    if files:
        boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
        body = bytearray()
        for key, val in (data or {}).items():
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"))
            body.extend(f"{val}\r\n".encode("utf-8"))
            
        for key, (filename, file_bytes) in files.items():
            content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(f'Content-Disposition: form-data; name="{key}"; filename="{filename}"\r\n'.encode("utf-8"))
            body.extend(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
            body.extend(file_bytes)
            body.extend(b"\r\n")
            
        body.extend(f"--{boundary}--\r\n".encode("utf-8"))
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}", "Connection": "close"}
        req = urllib.request.Request(url, data=bytes(body), headers=headers)
    else:
        headers = {"Content-Type": "application/json", "Connection": "close"}
        req_data = json.dumps(data).encode("utf-8") if data else None
        req = urllib.request.Request(url, data=req_data, headers=headers)
        
    if endpoint == "sendChatAction":
        req_timeout = 4
        max_attempts = 1
    elif endpoint == "getUpdates":
        poll_t = int((data or {}).get("timeout", 15)) if isinstance(data, dict) else 15
        req_timeout = max(10, poll_t + 8)
        max_attempts = 2
    elif files:
        req_timeout = 25
        max_attempts = 3
    else:
        req_timeout = 12
        max_attempts = 3

    for attempt in range(max_attempts):
        try:
            with urllib.request.urlopen(req, timeout=req_timeout, context=SSL_CONTEXT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            if e.code == 429:
                retry_after = 5
                try:
                    ej = json.loads(err_body)
                    retry_after = int(ej.get("parameters", {}).get("retry_after", 5))
                except Exception:
                    pass
                print(f"[API 429] Telegram solicitó esperar {retry_after}s para {endpoint}...", file=sys.stderr)
                time.sleep(min(retry_after, 30))
                continue
            if e.code == 400 and "message is not modified" in err_body.lower():
                return {"ok": True, "not_modified": True}
            if attempt == max_attempts - 1 and endpoint != "sendChatAction":
                print(f"[API ERROR] {endpoint}: {e} | {err_body}", file=sys.stderr)
            return None
        except Exception as e:
            if attempt < max_attempts - 1:
                time.sleep(0.6)
                continue
            if endpoint != "sendChatAction":
                print(f"[API ERROR] {endpoint}: {e}", file=sys.stderr)
            return None
    return None

def register_telegram_bot_commands():
    """Registra la lista oficial de comandos slash para que aparezcan en el menú emergente de Telegram al escribir '/'."""
    commands = [
        {"command": "plan", "description": "📐 Dante: Plan de arquitectura previo"},
        {"command": "cerberus", "description": "🛡️ Cerberus: Ciberseguridad y aprobación"},
        {"command": "review", "description": "🕵️ Code review senior y git diff"},
        {"command": "cc", "description": "🟠 Claude Code: extensión oficial Anthropic"},
        {"command": "ws", "description": "🚀 Explorador de carpetas en tu Mac"},
        {"command": "docs", "description": "🏠 Abrir la carpeta Documentos"},
        {"command": "git", "description": "⚡ Proyectos Git rápidos (1 toque)"},
        {"command": "diff", "description": "🔍 Vista rápida de cambios Git (<0.1s)"},
        {"command": "commit", "description": "💾 Guardar commit Git instantáneo"},
        {"command": "models", "description": "🧠 Selector de modelo Antigravity"},
        {"command": "history", "description": "📜 Historial de misiones y rollback"},
        {"command": "status", "description": "⚡ Batería, CPU, RAM y hardware"},
        {"command": "consumo", "description": "💎 Consumo de memoria y cuota restante"},
        {"command": "screenshot", "description": "👁️ Captura de pantalla de tu Mac"},
        {"command": "ports", "description": "🔌 Puertos y servicios activos"},
        {"command": "voice", "description": "🔊 Respuestas por voz on/off"},
        {"command": "comandos", "description": "⚡ Panel con todos los comandos directos"},
        {"command": "caffeinate", "description": "☕ Anti-reposo (Mac despierto continuo)"},
        {"command": "new", "description": "🧹 Nueva conversación limpia"},
        {"command": "cancel", "description": "🛑 Cancelar tarea en ejecución"},
        {"command": "restart", "description": "🔄 Reiniciar Iris en tu Mac"}
    ]
    targets = [
        {},
        {"scope": {"type": "default"}},
        {"scope": {"type": "all_private_chats"}},
        {"scope": {"type": "all_private_chats"}, "language_code": "es"},
        {"language_code": "es"}
    ]
    for uid in ALLOWED_USERS:
        targets.append({"scope": {"type": "chat", "chat_id": uid}})
        targets.append({"scope": {"type": "chat", "chat_id": uid}, "language_code": "es"})

    for t in targets:
        try:
            payload = {"commands": commands, **t}
            api_call("setMyCommands", payload)
        except Exception as e_cmd:
            print(f"[BOT] setMyCommands {t}: {e_cmd}", file=sys.stderr)

    try:
        api_call("setChatMenuButton", {"menu_button": {"type": "commands"}})
        for uid in ALLOWED_USERS:
            api_call("setChatMenuButton", {"chat_id": uid, "menu_button": {"type": "commands"}})
    except Exception as e_menu:
        print(f"[BOT] setChatMenuButton: {e_menu}", file=sys.stderr)

def get_persistent_reply_keyboard(page=None):
    """Genera la botonera inferior fija completa con todos los comandos directos visibles."""
    st = load_state()
    v_active = st.get("voice_mode", False)
    v_btn = "🎧 Voz: ON" if v_active else "🔇 Voz: OFF"
    p_paused = st.get("is_paused", False)
    iris_state_btn = "💤 Reposo: ON" if p_paused else "☀️ Reposo: OFF"
    status_label = "⚡ Estado Mac" if sys.platform == "darwin" else "⚡ Estado PC"

    return {
        "keyboard": [
            [{"text": status_label}, {"text": "💎 Consumo / Cuota"}],
            [{"text": "🚀 Proyectos (/ws)"}, {"text": "🏠 Documentos"}],
            [{"text": "📐 Plan (Dante)"}, {"text": "🛡️ Cerberus"}, {"text": "🕵️ Code Review"}],
            [{"text": "🧠 Modelos IA"}, {"text": "👁️ Captura Pantalla"}],
            [{"text": "⚡ Comandos Directos"}, {"text": "🔌 Puertos"}, {"text": "📜 Historial"}],
            [{"text": v_btn}, {"text": iris_state_btn}, {"text": "🛑 Cancelar"}]
        ],
        "resize_keyboard": True,
        "is_persistent": False
    }

def send_direct_commands_menu(chat_id):
    """Envía el panel interactivo con todos los comandos directos y botones táctiles inmediatos."""
    sys_name = "Mac" if sys.platform == "darwin" else ("PC Windows" if sys.platform == "win32" else "Equipo Linux")
    status_label = "⚡ Estado Mac" if sys.platform == "darwin" else "⚡ Estado PC"
    menu_text = (
        "⚡ <b>PANEL DE COMANDOS DIRECTOS DE IRIS</b>\n"
        f"<i>Iris está al mando de tu {sys_name} y coordina a Dante y Cerberus cuando tú se lo pides:</i>\n\n"
        "🟠 <b>Claude Code (Extensión Anthropic):</b>\n"
        f"• <code>/cc &lt;orden&gt;</code> — Ejecuta directamente con el motor y créditos de tu extensión Claude Code en tu {sys_name}.\n\n"
        "📐 <b>Dante · Arquitectura &amp; Planificación (Bajo demanda):</b>\n"
        "• <code>/plan &lt;idea&gt;</code> — Dante diseña la arquitectura antes de que Iris ejecute.\n\n"
        "🛡️ <b>Cerberus · Ciberseguridad &amp; Aprobación (Bajo demanda):</b>\n"
        "• <code>/cerberus [consulta]</code> — Cerberus audita ciberseguridad básica, recomienda blindajes y aprueba planes o ejecuciones cuando tú lo pides.\n"
        "• <code>/review</code> — Code Review senior y auditoría visual de Git diff.\n\n"
        "🚀 <b>Archivos y Proyectos:</b>\n"
        "• <code>/ws</code> — Explorador interactivo de carpetas y proyectos.\n"
        "• <code>/docs</code> — Abrir directamente la carpeta ~/Documents.\n"
        "• <code>/git</code> — Proyectos con control de versiones Git.\n"
        "• <code>/diff</code> — Vista rápida de archivos y líneas modificadas (&lt;0.1s).\n"
        "• <code>/commit [mensaje]</code> — Guardar commit local instantáneo en Git.\n\n"
        "🧠 <b>Inteligencia &amp; Modelos:</b>\n"
        "• <code>/model</code> — Selector de modelos Antigravity (Sonnet, Pro, Flash).\n"
        "• <code>/consumo</code> — Tokens usados, memoria y cuota restante.\n\n"
        f"🖥️ <b>Hardware &amp; Sistema ({sys_name}):</b>\n"
        "• <code>/status</code> — Batería, CPU, RAM, disco y hardware.\n"
        f"• <code>/screenshot</code> — Captura de pantalla de tu {sys_name} en tiempo real.\n"
        "• <code>/ports</code> — Servidores locales y puertos web activos.\n"
        f"• <code>/caffeinate</code> — Modo anti-reposo ({sys_name} despierto continuo).\n\n"
        "⚙️ <b>Sesión &amp; Control:</b>\n"
        "• <code>/v</code> — Alternar respuestas por voz.\n"
        "• <code>/history</code> — Historial de misiones y rollback.\n"
        "• <code>/new</code> — Iniciar sesión limpia al 100%.\n"
        "• <code>/cancel</code> — Detener la tarea en ejecución.\n"
        f"• <code>/restart</code> — Reiniciar Iris en tu {sys_name}."
    )
    markup = {
        "inline_keyboard": [
            [
                {"text": "📐 Plan (Dante)", "callback_data": "cmd_plan"},
                {"text": "🛡️ Cerberus (/cerberus)", "callback_data": "cmd_cerberus"}
            ],
            [
                {"text": "🟠 Claude Code (/cc)", "callback_data": "cmd_cc"},
                {"text": "🕵️ Code Review", "callback_data": "cmd_review"}
            ],
            [
                {"text": "🧠 Modelos IA", "callback_data": "cmd_models"},
                {"text": "💎 Consumo / Cuota", "callback_data": "cmd_consumo"}
            ],
            [
                {"text": "🚀 Proyectos (/ws)", "callback_data": "cmd_ws"},
                {"text": "🏠 Documentos (/docs)", "callback_data": "cmd_docs"}
            ],
            [
                {"text": status_label, "callback_data": "cmd_status"},
                {"text": "👁️ Captura Pantalla", "callback_data": "cmd_screenshot"}
            ],
            [
                {"text": "🔌 Puertos Activos", "callback_data": "cmd_ports"},
                {"text": "📜 Historial / Rollback", "callback_data": "cmd_history"}
            ],
            [
                {"text": "🧹 Nueva Sesión", "callback_data": "cmd_new"},
                {"text": "🔄 Reiniciar Iris", "callback_data": "cmd_restart"}
            ]
        ]
    }
    send_msg(chat_id, menu_text, reply_markup=markup)

def edit_msg_text(chat_id, message_id, text, parse_mode="HTML", reply_markup=None):
    if not text or not message_id:
        return None
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
    res = api_call("editMessageText", payload)
    if res is None and parse_mode:
        # Fallback sin HTML si alguna etiqueta falló
        plain = html.unescape(re.sub(r'<[^>]+>', '', text))
        payload_plain = {"chat_id": chat_id, "message_id": message_id, "text": plain}
        if reply_markup:
            payload_plain["reply_markup"] = reply_markup
        return api_call("editMessageText", payload_plain)
    return res

def _send_single_chunk(chat_id, chunk, parse_mode="HTML", reply_markup=None):
    payload = {"chat_id": chat_id, "text": chunk}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
    res = api_call("sendMessage", payload)
    if res is None and parse_mode:
        # Fallback sin HTML si alguna etiqueta quedó malformada o abierta
        plain = html.unescape(re.sub(r'<[^>]+>', '', chunk))
        payload_plain = {"chat_id": chat_id, "text": plain}
        if reply_markup:
            payload_plain["reply_markup"] = reply_markup
        res = api_call("sendMessage", payload_plain)
    return res

def send_msg(chat_id, text, parse_mode="HTML", reply_markup=None):
    if not text:
        return None

    if len(text) <= 3800:
        return _send_single_chunk(chat_id, text, parse_mode=parse_mode, reply_markup=reply_markup)

    # Dividir por saltos de línea sin cortar bloques pre ni tags HTML
    chunks = []
    current = []
    current_len = 0
    in_pre = False

    for line in text.split("\n"):
        if "<pre>" in line:
            in_pre = True
        if "</pre>" in line:
            in_pre = False

        if current_len + len(line) + 1 > 3500 and current:
            chunk_text = "\n".join(current)
            if in_pre:
                chunk_text += "</pre>"
            chunks.append(chunk_text)
            current = ["<pre>" + line] if in_pre else [line]
            current_len = len(current[0])
        else:
            current.append(line)
            current_len += len(line) + 1

    if current:
        chunks.append("\n".join(current))

    for idx, chunk in enumerate(chunks):
        markup = reply_markup if idx == len(chunks) - 1 else None
        _send_single_chunk(chat_id, chunk, parse_mode=parse_mode, reply_markup=markup)
        time.sleep(0.3)

def send_photo(chat_id, caption, photo_path):
    if not os.path.exists(photo_path):
        send_msg(chat_id, f"No encontré el archivo de imagen: <code>{photo_path}</code>")
        return
    try:
        with open(photo_path, "rb") as f:
            file_bytes = f.read()
        files = {"photo": (os.path.basename(photo_path), file_bytes)}
        data = {"chat_id": str(chat_id), "caption": caption}
        api_call("sendPhoto", data=data, files=files)
    except Exception as e:
        send_msg(chat_id, f"No se pudo enviar la foto: {str(e)}")

def send_document(chat_id, caption, doc_path):
    if not os.path.exists(doc_path):
        send_msg(chat_id, f"No encontré el archivo: <code>{doc_path}</code>")
        return
    try:
        with open(doc_path, "rb") as f:
            file_bytes = f.read()
        files = {"document": (os.path.basename(doc_path), file_bytes)}
        data = {"chat_id": str(chat_id), "caption": caption}
        api_call("sendDocument", data=data, files=files)
    except Exception as e:
        send_msg(chat_id, f"No se pudo enviar el archivo: {str(e)}")

def send_voice(chat_id, audio_path, caption=None):
    if not os.path.exists(audio_path):
        return
    try:
        # Calcular duración exacta para que el reproductor de Telegram inicialice al instante
        duration = 0
        try:
            ff_out = subprocess.check_output(
                ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
                text=True,
                timeout=3
            ).strip()
            if ff_out:
                duration = max(1, int(round(float(ff_out))))
        except Exception:
            pass

        with open(audio_path, "rb") as f:
            file_bytes = f.read()

        files = {"voice": (os.path.basename(audio_path), file_bytes)}
        data = {"chat_id": str(chat_id)}
        if duration > 0:
            data["duration"] = str(duration)
        if caption:
            data["caption"] = caption
        api_call("sendVoice", data=data, files=files)
    except Exception as e:
        print(f"Error enviando nota de voz: {e}", file=sys.stderr)

EDGE_TTS_BIN = shutil.which("edge-tts") or os.path.expanduser("~/Library/Python/3.9/bin/edge-tts")

def prepare_text_for_tts(text):
    """
    Prepara y normaliza el texto en español para una pronunciación y dicción impecable:
    - Decodifica entidades HTML (&aacute;, &#233;, &quot;, etc.).
    - Normaliza caracteres con tilde a Unicode NFC (evita diacríticos descompuestos que confunden al TTS).
    - Aplica un motor fonético de acentuación prosódica en español (tildes explícitas) para nombres
      propios del ecosistema (ej. 'Antigravity' -> 'Antigráviti', 'Telegram' -> 'Télegram').
    - Traduce anglicismos técnicos a fonética natural en español latinoamericano.
    - Convierte viñetas, saltos y signos en pausas respiratorias naturales.
    """
    import html
    import urllib.parse

    # 1. Remover bloques de código, tablas, enlaces Markdown [texto](url) y etiquetas HTML antes de hablar
    t = re.sub(r'<pre>[\s\S]*?</pre>', ' bloque de código finalizado. ', text)
    t = re.sub(r'```[\s\S]*?```', ' bloque de código. ', t)
    t = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', t)
    t = re.sub(r'file://\S+', ' ', t)
    t = re.sub(r'#L\d+(?:-L?\d+)?\b', ' ', t)
    t = re.sub(r'<[^>]+>', ' ', t)
    
    # 2. Normalización de URLs para que suenen naturales (evita "https dos puntos barra diagonal...")
    def _url_repl(m):
        url = m.group(0)
        try:
            parsed = urllib.parse.urlparse(url)
            domain = parsed.netloc or parsed.path.split('/')[0]
            domain = re.sub(r'^www\.', '', domain)
            domain = domain.replace('.', ' punto ')
            return f'enlace de {domain}'
        except Exception:
            return 'enlace web'
    t = re.sub(r'https?://\S+', _url_repl, t)

    # 3. Normalización de rutas de carpetas y archivos locales (evita "barra diagonal users barra...")
    def _path_repl(m):
        raw_path = m.group(0).strip(".,;:()'\"")
        parts = [p for p in re.split(r'[/\\]', raw_path) if p]
        if not parts:
            return ''
        last_item = parts[-1]
        if '.' in last_item and not last_item.startswith('.'):
            return f'archivo {last_item}'
        else:
            return f'carpeta {last_item}'
    t = re.sub(r'(?:/|[a-zA-Z]:\\)[\w\.\- /]+(?:/[\w\.\-]+)+', _path_repl, t)
    t = re.sub(r'(?:\./|\.\./)[\w\.\-/]+', _path_repl, t)

    # 4. Normalización de direcciones IP y puertos
    def _ip_port_repl(m):
        ip = m.group(1).replace('.', ' punto ')
        port = m.group(2)
        port_spoken = f"{port[:2]} {port[2:]}" if len(port) == 4 else port
        return f"{ip} en el puerto {port_spoken}"
    t = re.sub(r'\b((?:\d{1,3}\.){3}\d{1,3}):(\d{2,5})\b', _ip_port_repl, t)
    t = re.sub(r'\b((?:\d{1,3}\.){3}\d{1,3})\b', lambda m: m.group(0).replace('.', ' punto '), t)

    # 5. Normalización de números técnicos, versiones y códigos
    def _ver_repl(m):
        prefix = m.group(1) or ""
        v = m.group(2).replace('.', ' punto ')
        if "versión" in prefix.lower():
            return f'versión {v}'
        return f'versión {v}'
    t = re.sub(r'\b(versión\s+v?|v|ver\s+)(\d+(?:\.\d+)+)\b', _ver_repl, t, flags=re.IGNORECASE)
    t = re.sub(r'\btask_\d+\b', 'la tarea', t, flags=re.IGNORECASE)
    t = re.sub(r'#?p\d{3,}(\d{3})\b', r'plan número \1', t, flags=re.IGNORECASE)
    t = re.sub(r'\b\d{8,12}\b', 'código de identificación', t)
    t = re.sub(r'(\d+)%', r'\1 por ciento', t)

    # Evitar redundancias como "el archivo archivo" o "la carpeta carpeta"
    t = re.sub(r'\b(?:el\s+)?archivo\s+archivo\b', 'el archivo', t, flags=re.IGNORECASE)
    t = re.sub(r'\b(?:la\s+)?carpeta\s+carpeta\b', 'la carpeta', t, flags=re.IGNORECASE)
    t = re.sub(r'\bel\s+el\b', 'el', t, flags=re.IGNORECASE)
    t = re.sub(r'\bla\s+la\b', 'la', t, flags=re.IGNORECASE)

    # Separar identificadores snake_case para que no diga "guion bajo"
    t = re.sub(r'(\w+)_(\w+)', r'\1 \2', t)

    # 6. Decodificar entidades HTML y normalizar tildes a NFC
    t = html.unescape(t)
    t = unicodedata.normalize('NFC', t)

    # 7. Diccionario fonético prosódico natural (sin tildes artificiales que robotizan la voz)
    phonetic_subs = [
        # Ecosistema y asistentes con entonación natural
        (r'\bantigravity\b', 'Antigrávity'),
        (r'\bcerberus\b', 'Cérberus'),
        (r'\bowasp\b', 'OWASP'),
        (r'\bclaude code\b', 'Claude Code'),
        (r'\bsonnet\b', 'Sónet'),
        (r'\bopus\b', 'Ópus'),
        (r'\bargon\b', 'Árgon'),
        (r'\bgemini\b', 'Yémini'),
        (r'\bwhisper\b', 'Güísper'),
        (r'\bcloudflare\b', 'Cloudflare'),
        (r'\bcpanel\b', 'cPanel'),
        (r'\bgithub\b', 'GitHub'),

        # Términos técnicos leídos con fluidez humana
        (r'\bgit\b', 'git'),
        (r'\bpr\b', 'pull request'),
        (r'\bbuild\b', 'compilación'),
        (r'\bdeploys?\b', 'despliegue'),
        (r'\bcommits?\b', 'commit'),
        (r'\brollbacks?\b', 'rollback'),
        (r'\bscripts?\b', 'script'),
        (r'\bdaemons?\b', 'demonio'),
        (r'\btokens?\b', 'token'),
        (r'\bcaches?\b', 'caché'),
        (r'\bhooks?\b', 'hook'),
        (r'\bbackends?\b', 'backend'),
        (r'\bfrontends?\b', 'frontend'),
        (r'\bonline\b', 'en línea'),
        (r'\boffline\b', 'fuera de línea'),
        (r'\blogin\b', 'inicio de sesión'),
        (r'\bdebug\b', 'depuración'),
        (r'\bbugs?\b', 'errores'),
        (r'\btests?\b', 'pruebas'),
        (r'\bcwd\b', 'carpeta de trabajo'),
        (r'\bmacbooks?\b', 'MacBook'),
        (r'\bmac\b', 'Mac'),
        (r'\bram\b', 'memoria RAM'),
        (r'\bcpu\b', 'procesador'),
        (r'\bssd\b', 'disco sólido'),
        (r'\bok\b', 'listo'),

        # Extensiones de archivo frecuentes habladas de forma natural
        (r'\.py\b', ' de Python'),
        (r'\.js\b', ' de JavaScript'),
        (r'\.ts\b', ' de TypeScript'),
        (r'\.json\b', ' JSON'),
        (r'\.html\b', ' HTML'),
        (r'\.css\b', ' CSS'),
        (r'\.md\b', ' Markdown'),
        (r'\.sh\b', ' de terminal'),
    ]
    for pattern, repl in phonetic_subs:
        t = re.sub(pattern, repl, t, flags=re.IGNORECASE)

    # Remover emojis, líneas divisoras (━━━) y paréntesis técnicos para que la lectura sea 100% fluida y humana
    t = re.sub(r'[━─│┃┌┐└┘├┤┬┴┼═║╔╗╚╝╠╣╦╩╬]+', ' ', t)
    t = re.sub(r'[\U00010000-\U0010ffff\u2600-\u27bf\ufe0f]', ' ', t)
    t = re.sub(r'\([^)]{1,45}\)', ' ', t)

    # 4. Transformar saltos de línea y viñetas en oraciones naturales con punto seguido
    t = re.sub(r'^\s*\d+[\.\)]\s*', '', t, flags=re.MULTILINE)
    t = re.sub(r'[\r\n]+', '. ', t)
    t = re.sub(r'[•\-\*]\s+', '. ', t)
    t = re.sub(r'[:;]\s+', '. ', t)

    # 5. Limpieza de símbolos raros o markdown residual
    t = re.sub(r'[_*`#~|/\\<>]', ' ', t)
    t = re.sub(r'\s*,\s*', ', ', t)
    t = re.sub(r'(?:\.\s*){2,}', '. ', t)
    t = re.sub(r',\s*\.', '. ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    
    # 6. Acotar a un resumen agradable y fluido (máx 420 caracteres)
    if len(t) > 420:
        cut = t.rfind(".", 0, 380)
        if cut > 110:
            t = t[:cut+1] + " Te dejé todos los detalles aquí en el chat\1"
        else:
            cut_comma = t.rfind(",", 0, 380)
            if cut_comma > 110:
                t = t[:cut_comma] + ". Te dejé todos los detalles aquí en el chat\1"
            else:
                t = t[:360].rstrip(".,; ") + ". Te dejé todos los detalles aquí en el chat\1"
            
    return t or "Listo\1"

def synthesize_speech_to_voice(text, output_ogg_path=None, voice=None, pitch=None, rate=None):
    if not voice:
        cfg = load_config()
        voice = cfg.get("voice_name", VOICE_NAME)
    t_dir = tempfile.gettempdir()
    if not output_ogg_path:
        output_ogg_path = os.path.join(t_dir, f"iris_speech_{int(time.time() * 1000)}.ogg")
    
    clean_txt = prepare_text_for_tts(text)

    # Velocidad fluida y natural sin distorsión de tono (cero efecto robótico)
    is_cerberus_voice = (voice == CERBERUS_VOICE_NAME or "Gonzalo" in str(voice))
    is_dante_voice = (voice == DANTE_VOICE_NAME or "Jorge" in str(voice))
    if rate:
        eff_rate = rate
    elif is_cerberus_voice:
        eff_rate = "+2%"
    elif is_dante_voice:
        eff_rate = "+3%"
    else:
        eff_rate = "+4%"
    eff_pitch = pitch if (pitch and pitch not in ("+0Hz", "0Hz", "-0Hz")) else None

    # 1. Si es una voz neuronal de Edge TTS (ej. es-MX-DaliaNeural, es-MX-JorgeNeural, es-CO-GonzaloNeural)
    if "Neural" in voice or os.path.exists(EDGE_TTS_BIN) or shutil.which("edge-tts"):
        neural_voice = voice if "Neural" in voice else "es-MX-DaliaNeural"
        temp_mp3 = os.path.join(t_dir, f"iris_tmp_{int(time.time() * 1000)}.mp3")
        try:
            # Ejecutar edge_tts forzando IPv4 (AF_INET) para evitar que el subproceso se cuelgue en IPv6
            edge_py = "/Library/Developer/CommandLineTools/usr/bin/python3"
            if not os.path.exists(edge_py):
                edge_py = sys.executable
            ipv4_edge_cmd = [
                edge_py, "-c",
                "import socket, sys; _o=socket.getaddrinfo; "
                "socket.getaddrinfo=lambda h,p,f=0,t=0,pr=0,fl=0: (_o(h,p,socket.AF_INET,t,pr,fl) if f==0 else _o(h,p,f,t,pr,fl)); "
                "from edge_tts.util import main; sys.exit(main())",
                "--voice", neural_voice, f"--rate={eff_rate}"
            ]
            if eff_pitch:
                ipv4_edge_cmd.append(f"--pitch={eff_pitch}")
            ipv4_edge_cmd.extend(["--text", clean_txt, "--write-media", temp_mp3])
            tts_env = os.environ.copy()
            user_home = os.path.expanduser("~")
            tts_env["HOME"] = user_home
            tts_env["PYTHONIOENCODING"] = "utf-8"
            py39_site = os.path.join(user_home, "Library", "Python", "3.9", "lib", "python", "site-packages")
            if os.path.isdir(py39_site):
                curr_pp = tts_env.get("PYTHONPATH", "")
                tts_env["PYTHONPATH"] = f"{py39_site}:{curr_pp}" if curr_pp else py39_site
            for _att in range(2):
                try:
                    subprocess.run(
                        ipv4_edge_cmd,
                        env=tts_env,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=True,
                        timeout=18
                    )
                    break
                except Exception:
                    if _att == 1:
                        raise
                    time.sleep(0.4)
            subprocess.run(
                [
                    "ffmpeg", "-y", "-i", temp_mp3,
                    "-c:a", "libopus",
                    "-b:a", "64k",
                    "-vbr", "on",
                    "-compression_level", "10",
                    "-ar", "48000",
                    "-ac", "1",
                    "-application", "audio",
                    output_ogg_path
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=25
            )
            if os.path.exists(temp_mp3):
                try:
                    os.remove(temp_mp3)
                except Exception:
                    pass
            return output_ogg_path
        except Exception as e:
            print(f"Fallback desde Edge-TTS: {e}", file=sys.stderr)
            if os.path.exists(temp_mp3):
                try:
                    os.remove(temp_mp3)
                except Exception:
                    pass

    # 2. Fallback según Sistema Operativo
    # En Windows: PowerShell Speech Synthesis (.NET SAPI)
    if sys.platform == "win32":
        temp_wav = os.path.join(t_dir, f"iris_tmp_{int(time.time() * 1000)}.wav")
        try:
            safe_text = clean_txt.replace("'", "").replace('"', "")
            ps_script = (
                f"Add-Type -AssemblyName System.Speech; "
                f"$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                f"$s.SetOutputToWaveFile('{temp_wav.replace(os.sep, '/')}'); "
                f"$s.Speak('{safe_text[:350]}'); "
                f"$s.Dispose();"
            )
            subprocess.run(["powershell", "-NoProfile", "-Command", ps_script], check=True, timeout=15)
            if os.path.exists(temp_wav):
                subprocess.run(
                    ["ffmpeg", "-y", "-i", temp_wav, "-c:a", "libopus", "-b:a", "32k", "-ar", "48000", "-ac", "1", "-application", "voip", output_ogg_path],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=15
                )
                try:
                    os.remove(temp_wav)
                except Exception:
                    pass
                return output_ogg_path
        except Exception as e:
            print(f"Error en PowerShell TTS Windows: {e}", file=sys.stderr)
            if os.path.exists(temp_wav):
                try:
                    os.remove(temp_wav)
                except Exception:
                    pass
            return None

    # En macOS: Fallback nativo say
    elif sys.platform == "darwin":
        temp_aiff = os.path.join(t_dir, f"iris_tmp_{int(time.time() * 1000)}.aiff")
        try:
            say_rate = "165"
            if is_cerberus_voice or voice == CERBERUS_FALLBACK_VOICE:
                say_v = CERBERUS_FALLBACK_VOICE
                say_rate = "155"
            elif "Jorge" in voice or voice == DANTE_VOICE_NAME:
                say_v = DANTE_FALLBACK_VOICE
            elif "Neural" in voice:
                say_v = "Paulina"
            else:
                say_v = voice or "Paulina"
            subprocess.run(["say", "-v", say_v, "-r", say_rate, "-o", temp_aiff, clean_txt], check=True, timeout=15)
            subprocess.run(
                [
                    "ffmpeg", "-y", "-i", temp_aiff,
                    "-c:a", "libopus",
                    "-b:a", "32k",
                    "-ar", "48000",
                    "-ac", "1",
                    "-application", "voip",
                    "-page_duration", "20000",
                    output_ogg_path
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=15
            )
            if os.path.exists(temp_aiff):
                try:
                    os.remove(temp_aiff)
                except Exception:
                    pass
            return output_ogg_path
        except Exception as e:
            print(f"Error sintetizando voz nativa say: {e}", file=sys.stderr)
            if os.path.exists(temp_aiff):
                try:
                    os.remove(temp_aiff)
                except Exception:
                    pass
            return None
    return None

def download_telegram_file(file_id, suffix=".jpg"):
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)
    res = None
    for _ in range(3):
        res = api_call("getFile", {"file_id": file_id})
        if res and res.get("ok"):
            break
        time.sleep(0.6)
    if not res or not res.get("ok"):
        return None
    file_path = res["result"]["file_path"]
    download_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"

    filename = f"captura_{int(time.time())}_{file_id[:6]}{suffix}"
    dest_path = os.path.join(DOWNLOADS_DIR, filename)
    last_err = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                download_url,
                headers={"Connection": "close", "User-Agent": "IrisDaemon/2.0"}
            )
            with urllib.request.urlopen(req, timeout=25, context=SSL_CONTEXT) as resp, open(dest_path, "wb") as out_f:
                shutil.copyfileobj(resp, out_f)
            if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
                return dest_path
        except Exception as e:
            last_err = e
            time.sleep(0.8 * (attempt + 1))

    # Fallback nativo con curl (IPv4 forzado + reintentos automáticos ante cortes TCP Errno 54)
    try:
        subprocess.run(
            ["/usr/bin/curl", "-4", "-sSL", "--retry", "3", "--retry-all-errors", "--connect-timeout", "10", "-o", dest_path, download_url],
            check=True,
            timeout=35
        )
        if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
            return dest_path
    except Exception as e_curl:
        last_err = e_curl

    print(f"Error descargando: {last_err}", file=sys.stderr)
    return None

WHISPER_BIN = shutil.which("whisper-cli") or "/opt/homebrew/bin/whisper-cli"
FFMPEG_BIN = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
WHISPER_MODEL = os.path.join(SCRIPT_DIR, "models", "ggml-base.bin")

def transcribe_audio_file(audio_path):
    wav_path = os.path.splitext(audio_path)[0] + "_16k.wav"
    try:
        if os.path.exists(FFMPEG_BIN):
            cmd_conv = [FFMPEG_BIN, "-y", "-i", audio_path, "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", wav_path]
            subprocess.run(cmd_conv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
            target = wav_path if os.path.exists(wav_path) else audio_path
        else:
            target = audio_path
            
        if os.path.exists(WHISPER_BIN) and os.path.exists(WHISPER_MODEL):
            cmd = [
                WHISPER_BIN,
                "-m", WHISPER_MODEL,
                "-f", target,
                "-l", "es",
                "-nt",
                "--no-prints",
                "--prompt", "Iris, Dante, Cerberus, ciberseguridad, OWASP, Antigravity, el usuario, Gemini, Claude, Sonnet, Opus, Argon, Git, GitHub, Telegram, MacBook, código, proyecto"
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            raw_lines = proc.stdout.split("\n")
            clean_lines = [l.strip() for l in raw_lines if l.strip() and not l.startswith("[") and not l.startswith("whisper_") and not l.startswith("load_backend") and not l.startswith("ggml_metal")]
            result = " ".join(clean_lines).strip()
            return result
    except Exception as e:
        print(f"Error en transcripcion Whisper: {e}", file=sys.stderr)
    finally:
        if os.path.exists(wav_path):
            try:
                os.remove(wav_path)
            except Exception:
                pass
    return None

def get_system_status():
    global CURRENT_CWD, CAFFEINATE_PROC
    rows = []

    # 1. Batería
    b_val = "100% · AC"
    try:
        if sys.platform == "darwin":
            raw_batt = subprocess.check_output("pmset -g batt", shell=True, text=True)
            m = re.search(r'(\d+%)', raw_batt)
            pct = m.group(1) if m else "100%"
            b_val = f"{pct} · AC" if "AC Power" in raw_batt else f"{pct} · Batería"
        elif sys.platform == "win32":
            ps_batt = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Battery).EstimatedChargeRemaining"],
                text=True, timeout=3
            ).strip()
            if ps_batt and ps_batt.isdigit():
                b_val = f"{ps_batt}% · Batería"
            else:
                b_val = "100% · AC"
        else:
            b_val = "100% · AC"
    except Exception:
        b_val = "100% · AC"
    rows.append(["🔋 Batería", b_val])

    # 2. CPU
    try:
        if sys.platform == "win32":
            ncpu = os.cpu_count() or 4
            ps_cpu = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Processor).LoadPercentage"],
                text=True, timeout=3
            ).strip()
            cpu_val = f"{ps_cpu}% ({ncpu}c)" if ps_cpu else f"Normal ({ncpu}c)"
        else:
            load1, _, _ = os.getloadavg()
            ncpu = os.cpu_count() or int(subprocess.check_output(["sysctl", "-n", "hw.ncpu"]).strip())
            pct_cpu = min(round((load1 / ncpu) * 100, 1), 100.0)
            cpu_val = f"{pct_cpu}% ({ncpu}c)"
    except Exception:
        cpu_val = "Normal"
    rows.append(["⚡ CPU", cpu_val])

    # 3. RAM
    try:
        if sys.platform == "win32":
            ps_mem = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", "$os = Get-CimInstance Win32_OperatingSystem; \"$([math]::Round(($os.TotalVisibleMemorySize - $os.FreePhysicalMemory)/1024/1024, 1)) $($([math]::Round($os.TotalVisibleMemorySize/1024/1024, 1)))\""],
                text=True, timeout=3
            ).strip().split()
            if len(ps_mem) == 2:
                used_gb, total_gb = float(ps_mem[0]), float(ps_mem[1])
                ram_pct = round((used_gb / total_gb) * 100)
                ram_val = f"{used_gb}G/{total_gb}G ({ram_pct}%)"
            else:
                ram_val = "OK"
        elif sys.platform == "darwin":
            pagesize = int(subprocess.check_output(["sysctl", "-n", "hw.pagesize"]).strip())
            memsize = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"]).strip())
            total_gb = round(memsize / (1024**3), 1)
            vm = subprocess.check_output("vm_stat", shell=True, text=True)
            active_pages = wired_pages = compressor_pages = 0
            for line in vm.splitlines():
                if "Pages active:" in line: active_pages = int(line.split(":")[1].strip().strip("."))
                elif "Pages wired down:" in line: wired_pages = int(line.split(":")[1].strip().strip("."))
                elif "Occupied by compressor:" in line: compressor_pages = int(line.split(":")[1].strip().strip("."))
            used_bytes = (active_pages + wired_pages + compressor_pages) * pagesize
            used_gb = round(used_bytes / (1024**3), 1)
            ram_pct = round((used_bytes / memsize) * 100)
            ram_val = f"{used_gb}G/{total_gb}G ({ram_pct}%)"
        else:
            ram_val = "OK"
    except Exception:
        ram_val = "OK"
    rows.append(["🧠 RAM", ram_val])

    # 4. Disco SSD (shutil.disk_usage cross-platform)
    try:
        check_path = CURRENT_CWD if os.path.exists(CURRENT_CWD) else os.path.expanduser("~")
        total_b, used_b, free_b = shutil.disk_usage(check_path)
        free_gb = round(free_b / (1024**3), 1)
        used_pct = round((used_b / total_b) * 100)
        disk_val = f"{free_gb}G lib ({used_pct}%)"
    except Exception:
        disk_val = "OK"
    rows.append(["💾 SSD", disk_val])

    # 5. Uptime
    try:
        if sys.platform == "win32":
            ps_up = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", "((Get-Date) - (Get-CimInstance Win32_OperatingSystem).LastBootUpTime).ToString('dd\\.hh\\:mm')"],
                text=True, timeout=3
            ).strip()
            up_val = ps_up if ps_up else "Activo"
        else:
            raw_up = subprocess.check_output("uptime", shell=True, text=True)
            m = re.search(r'up\s+([^,]+(?:,\s*[^,]+)?)', raw_up)
            if m:
                clean_up = m.group(1).replace("days", "d").replace("day", "d").strip()
                up_val = clean_up[:14]
            else:
                up_val = "Activo"
    except Exception:
        up_val = "Activo"
    rows.append(["⏱️ Activo", up_val])

    # 6. Modo Anti-Reposo
    caff_active = is_anti_sleep_active()
    caff_val = "ON (Despierto)" if caff_active else "OFF (Normal)"
    rows.append(["☕ Reposo", caff_val])

    # 7. Consumo de Contexto del Modelo (% activo)
    ctx_info = get_model_context_metrics()
    if ctx_info:
        rows.append(["🤖 Modelo", f"{ctx_info['percentage']}% ({ctx_info['current_context']//1000}k)"])

    # 8. Tarea activa si está en curso
    active_telemetry_block = ""
    with TASK_LOCK:
        is_run_now = ACTIVE_TASK_INFO["running"]
        t_start_now = ACTIVE_TASK_INFO.get("start_time", 0)
        p_now = ACTIVE_TASK_INFO.get("prompt", "")
        if is_run_now:
            elapsed = int(time.time() - t_start_now)
            p_short = p_now[:11]
            rows.append(["⚡ En curso", f"{p_short} ({elapsed}s)"])

    detected_status_proj = ""
    if p_now:
        detected_status_proj, _ = detect_dynamic_project(p_now)
    try:
        tele = get_real_antigravity_live_telemetry(None, t_start_now if is_run_now else 0)
        if not detected_status_proj:
            detected_status_proj = tele.get("detected_project") or ""
        step_lbl = f"Paso #{tele['step']}" if tele.get("step") is not None else "Activo"
        if is_run_now:
            el_s = int(time.time() - t_start_now) if t_start_now else 0
            t_fmt = f"{el_s//60}m {el_s%60}s" if el_s >= 60 else f"{el_s}s"
            recent_lines = "\n".join([f"  • {a}" for a in (tele.get("recent_actions") or [])]) or f"  • {tele.get('current_action')}"
            active_telemetry_block = (
                f"\n\n🛰️ <b>Actividad en vivo ({step_lbl} · {t_fmt}):</b>\n"
                f"┃ {tele.get('current_action')}\n"
                f"{recent_lines}"
            )
        elif tele.get("recent_actions"):
            recent_lines = "\n".join([f"  • {a}" for a in tele["recent_actions"][-2:]])
            active_telemetry_block = (
                f"\n\n🛰️ <b>Últimas acciones en sesión ({step_lbl}):</b>\n"
                f"{recent_lines}"
            )
    except Exception:
        pass

    table = render_box_table(["Componente", "Métrica / Estado"], rows, max_width=35)
    safe_table = table.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    proj_line = f"\n📍 <b>Proyecto activo:</b> <code>{detected_status_proj}</code>" if detected_status_proj else ""
    return (
        "📊 <b>Telemetría del Sistema · Mac</b>\n"
        f"<pre>{safe_table}</pre>"
        f"{proj_line}"
        f"{active_telemetry_block}"
    )

def get_model_context_metrics():
    """Calcula el consumo actual de tokens, la ventana de contexto y el % ocupado del modelo en una sola pasada."""
    import datetime
    brain_dir = os.path.expanduser("~/.gemini/antigravity-cli/brain")
    if not os.path.exists(brain_dir):
        return None

    files = []
    try:
        for entry in os.listdir(brain_dir):
            cand = os.path.join(brain_dir, entry, ".system_generated", "logs", "transcript.jsonl")
            if os.path.isfile(cand):
                files.append(cand)
    except Exception:
        pass

    if not files:
        return None

    latest = max(files, key=os.path.getmtime)
    last_step = {}
    total_in = 0
    total_out = 0
    total_cache = 0
    step_count = 0
    first_iso = None
    last_iso = None

    now_ts = time.time()
    five_hours_ago = now_ts - (5 * 3600)
    real_compute_tokens_5h = 0
    cached_tokens_5h = 0
    first_window_ts = None

    try:
        with open(latest, "r", encoding="utf-8") as tf:
            for line in tf:
                try:
                    r = json.loads(line)
                    t_str = r.get("created_at")
                    if t_str:
                        if not first_iso:
                            first_iso = t_str
                        last_iso = t_str
                    if "input_tokens" in r:
                        in_tok = r.get("input_tokens", 0)
                        out_tok = r.get("output_tokens", 0)
                        cache_tok = r.get("cache_read_tokens", 0)
                        step_count += 1
                        total_in += in_tok
                        total_out += out_tok
                        total_cache += cache_tok
                        last_step = r
                        if t_str:
                            ts = datetime.datetime.fromisoformat(t_str.replace("Z", "+00:00")).timestamp()
                            if ts >= five_hours_ago:
                                if not first_window_ts:
                                    first_window_ts = ts
                                real_compute_tokens_5h += in_tok + out_tok
                                cached_tokens_5h += cache_tok
                except Exception:
                    pass
    except Exception:
        return None

    # Calcular duración de la sesión actual
    session_duration_str = "Reciente"
    if first_iso and last_iso:
        try:
            t0 = datetime.datetime.fromisoformat(first_iso.replace("Z", "+00:00"))
            t1 = datetime.datetime.fromisoformat(last_iso.replace("Z", "+00:00"))
            secs_diff = max(0, int((t1 - t0).total_seconds()))
            hrs = secs_diff // 3600
            mins = (secs_diff % 3600) // 60
            if hrs > 0:
                session_duration_str = f"{hrs}h {mins}m"
            else:
                session_duration_str = f"{mins} min" if mins > 0 else f"{secs_diff}s"
        except Exception:
            pass

    if first_window_ts:
        elapsed_window = int(now_ts - first_window_ts)
        rem_secs = max(0, (5 * 3600) - elapsed_window)
        h_rem = rem_secs // 3600
        m_rem = (rem_secs % 3600) // 60
        refresh_str = f"{h_rem}h {m_rem}m"
        # La cuota de rate limit de Google evalúa el cómputo real activo
        window_pct = min(100.0, round((real_compute_tokens_5h / 50000000) * 100, 1))
    else:
        refresh_str = "5h 00m"
        real_compute_tokens_5h = 0
        window_pct = 0.0

    in_t = last_step.get("input_tokens", 0)
    cache_t = last_step.get("cache_read_tokens", 0)
    out_t = last_step.get("output_tokens", 0)
    curr_context = in_t + cache_t

    # Configuración de límites por modelo
    cfg = load_config()
    m_id = cfg.get("model", "gemini-3.8-flash-low").lower()
    if "flash" in m_id or "argon" in m_id or "gemini" in m_id:
        max_context = 1048576  # 1M tokens
        m_label = "Gemini 1M"
    elif "sonnet" in m_id or "opus" in m_id or "claude" in m_id:
        max_context = 200000   # 200k tokens
        m_label = "Claude 200k"
    else:
        max_context = 128000   # 128k estándar
        m_label = "Estándar 128k"

    pct = round((curr_context / max_context) * 100, 1) if max_context > 0 else 0
    pct = min(pct, 100.0)

    # Barra visual de progreso: [████░░░░░░]
    total_blocks = 10
    filled = int(round((pct / 100) * total_blocks))
    bar = "█" * filled + "░" * (total_blocks - filled)

    return {
        "current_context": curr_context,
        "max_context": max_context,
        "percentage": pct,
        "bar": bar,
        "model_label": m_label,
        "active_model": cfg.get("model", "gemini-3.8-flash-low"),
        "last_turn_out": out_t,
        "last_turn_in": in_t,
        "cached_tokens": cache_t,
        "session_steps": step_count,
        "session_duration": session_duration_str,
        "quota_tokens_5h": real_compute_tokens_5h,
        "quota_cached_5h": cached_tokens_5h,
        "quota_refresh_in": refresh_str,
        "quota_5h_pct": window_pct,
        "total_tokens_session": total_in + total_out + total_cache
    }

def get_ports_table():
    if sys.platform == "win32":
        try:
            ps_cmd = (
                "$conns = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | "
                "Select-Object -First 20 -Property LocalPort, OwningProcess; "
                "foreach ($c in $conns) { "
                "  $p = (Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue).ProcessName; "
                "  Write-Output \"$($c.LocalPort)|$($c.OwningProcess)|$p\" "
                "}"
            )
            raw = subprocess.check_output(["powershell", "-NoProfile", "-Command", ps_cmd], text=True, timeout=5)
            lines = [l.strip() for l in raw.strip().splitlines() if l.strip()]
            if not lines:
                return "No hay puertos TCP escuchando en este momento."
            ports_map = {}
            for line in lines:
                parts = line.split("|")
                if len(parts) >= 2:
                    try:
                        port = int(parts[0])
                        pid = parts[1]
                        srv = (parts[2] if len(parts) > 2 and parts[2] else "Servicio")[:11]
                        if port not in ports_map:
                            ports_map[port] = (srv, pid)
                    except ValueError:
                        continue
            if not ports_map:
                return "No hay servicios principales escuchando en puertos estándar."
            sorted_ports = sorted(ports_map.keys())
            rows = []
            for p in sorted_ports[:15]:
                srv, pid = ports_map[p]
                rows.append([f":{p}", srv, pid])
            table = render_box_table(["Puerto", "Servicio", "PID"], rows, max_width=35)
            safe_table = table.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            return f"🔌 <b>Puertos y Servicios en Escucha (Windows):</b>\n<pre>{safe_table}</pre>"
        except Exception as e:
            return f"No se pudo consultar puertos en Windows: {str(e)}"

    try:
        raw = subprocess.check_output("lsof -iTCP -sTCP:LISTEN -n -P", shell=True, text=True)
    except Exception as e:
        return f"No se pudo consultar lsof: {str(e)}"

    lines = raw.strip().splitlines()
    if len(lines) <= 1:
        return "No hay puertos TCP escuchando en este momento."

    ports_map = {}
    ephemeral_count = 0

    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 9:
            continue
        cmd = parts[0]
        pid = parts[1]
        name = parts[8]
        m = re.search(r':(\d+)$', name)
        if not m:
            continue
        port = int(m.group(1))

        if port >= 49152:
            ephemeral_count += 1
            continue

        clean_cmd = cmd.lower()
        if "redis" in clean_cmd:
            service = "Redis"
        elif "postg" in clean_cmd:
            service = "Postgres"
        elif "ollama" in clean_cmd:
            service = "Ollama AI"
        elif "node" in clean_cmd:
            service = "Node/Web"
        elif "python" in clean_cmd:
            service = "Python"
        elif "java" in clean_cmd:
            service = "Java"
        elif "antigravi" in clean_cmd:
            service = "Antigravity"
        elif "electron" in clean_cmd:
            service = "Electron"
        elif "php" in clean_cmd:
            service = "PHP Server"
        elif "adb" in clean_cmd:
            service = "ADB Bridge"
        elif "kilo" in clean_cmd:
            service = "Kilo/Dev"
        elif "language" in clean_cmd:
            service = "IDE Lang"
        else:
            service = cmd[:10]

        if port not in ports_map or ports_map[port][0] == "IDE Lang":
            ports_map[port] = (service, pid)

    if not ports_map:
        return "No hay servicios principales escuchando en puertos estándar."

    sorted_ports = sorted(ports_map.keys())
    rows = []
    for p in sorted_ports[:15]:
        srv, pid = ports_map[p]
        rows.append([f":{p}", srv, pid])

    table = render_box_table(["Puerto", "Servicio", "PID"], rows, max_width=35)
    safe_table = table.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    footer = f"\n<i>💡 +{ephemeral_count} puertos efímeros de IDE/LSP (>49152)</i>" if ephemeral_count > 0 else ""
    return f"🔌 <b>Puertos y Servicios en Escucha:</b>\n<pre>{safe_table}</pre>{footer}"

def get_top_processes_table():
    if sys.platform == "win32":
        try:
            ps_cmd = (
                "Get-Process | Sort-Object -Property CPU -Descending | Select-Object -First 7 | "
                "ForEach-Object { \"$($_.ProcessName)|$([math]::Round($_.CPU, 1))|$([math]::Round($_.WorkingSet64/1MB, 0))|$($_.Id)\" }"
            )
            raw = subprocess.check_output(["powershell", "-NoProfile", "-Command", ps_cmd], text=True, timeout=5)
            lines = [l.strip() for l in raw.strip().splitlines() if l.strip()]
            rows = []
            for line in lines:
                parts = line.split("|")
                if len(parts) >= 4:
                    name, cpu, mem, pid = parts[0], parts[1], parts[2], parts[3]
                    rows.append([name[:11], f"{cpu}s", f"{mem}M", pid])
            if rows:
                table = render_box_table(["Proceso", "CPU", "RAM", "PID"], rows, max_width=35)
                safe_table = table.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                return f"⚡ <b>Procesos con Mayor Consumo (Windows):</b>\n<pre>{safe_table}</pre>"
        except Exception:
            pass
        return "⚡ <b>Procesos:</b> Información de procesos en Windows no disponible."

    try:
        raw = subprocess.check_output("ps -A -o pid,%cpu,%mem,comm -r", shell=True, text=True)
        lines = raw.strip().splitlines()
        rows = []
        for line in lines[1:8]:
            parts = line.split(None, 3)
            if len(parts) < 4:
                continue
            pid, cpu, mem, comm = parts
            name = comm.split("/")[-1]
            if "Chrome" in comm:
                name = "Chrome"
            elif "Electron" in comm:
                name = "Electron"
            elif "Antigravity" in comm:
                name = "Antigravity"
            elif "language_" in comm:
                name = "IDE LSP"
            elif "redis" in comm:
                name = "Redis"
            elif "postgres" in comm:
                name = "Postgres"
            elif "python" in comm:
                name = "Python"
            rows.append([name[:11], f"{float(cpu):.0f}%", f"{float(mem):.0f}%", pid])

        if rows:
            table = render_box_table(["Proceso", "CPU", "RAM", "PID"], rows, max_width=35)
            safe_table = table.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            return f"⚡ <b>Procesos con Mayor Consumo:</b>\n<pre>{safe_table}</pre>"
    except Exception:
        pass

    code, out = run_shell("ps aux -r | head -n 6")
    safe_out = out.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"⚡ <b>Procesos Activos:</b>\n<pre>{safe_out}</pre>"

CURRENT_PROCESS = None

def kill_process_tree(proc):
    """Termina de forma limpia tanto el proceso principal como cualquier subproceso hijo en su grupo."""
    if not proc:
        return
    try:
        if sys.platform != "win32":
            import signal
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGKILL)
            except Exception:
                proc.kill()
        else:
            proc.kill()
    except Exception:
        pass
    try:
        if proc.stdout:
            proc.stdout.close()
    except Exception:
        pass

def _is_agy_error_output(out_text, returncode):
    """Detecta si la salida del motor CLI corresponde a un fallo transitorio de API, sesión o cuota."""
    if returncode not in (0, None):
        return True
    if not out_text or not out_text.strip():
        return True
    low = out_text.strip().lower()
    error_markers = [
        "rate limit", "quota exceeded", "resource_exhausted", "overloaded",
        "503 service unavailable", "429 too many requests", "internal server error",
        "failed to connect", "connection reset", "econnreset", "etimedout",
        "error: model", "invalid model", "session locked", "corrupted transcript"
    ]
    if len(low) < 400 and any(m in low for m in error_markers):
        return True
    return False

def run_shell(cmd):
    global CURRENT_CWD, CURRENT_PROCESS
    try:
        CURRENT_PROCESS = subprocess.Popen(
            cmd,
            shell=True,
            cwd=CURRENT_CWD,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=(sys.platform != "win32")
        )
        out, _ = CURRENT_PROCESS.communicate(timeout=600)
        code = CURRENT_PROCESS.returncode
        CURRENT_PROCESS = None
        out = (out or "").strip()
        if not out:
            out = "(El comando terminó con éxito sin texto en pantalla)"
        return code, out
    except subprocess.TimeoutExpired:
        if CURRENT_PROCESS:
            kill_process_tree(CURRENT_PROCESS)
            CURRENT_PROCESS = None
        return 124, "El comando tardó más de 10 minutos y se detuvo."
    except Exception as e:
        CURRENT_PROCESS = None
        return 1, f"Error: {str(e)}"

def _ensure_agy_file_token_synced():
    """
    Garantiza que las credenciales OAuth en archivo plano de Antigravity estén sincronizadas
    entre ~/.gemini/antigravity-cli/antigravity-oauth-token y ~/.gemini/jetski-standalone-oauth-token.
    Evita que AGY CLI abra el navegador en el Mac pidiendo código 4/0A... cuando el llavero (Keychain)
    está bloqueado o el demonio corre en sesión Background.
    """
    try:
        gemini_dir = os.path.expanduser("~/.gemini")
        cli_dir = os.path.join(gemini_dir, "antigravity-cli")
        os.makedirs(cli_dir, exist_ok=True)
        candidates = [
            os.path.join(cli_dir, "antigravity-oauth-token"),
            os.path.join(gemini_dir, "antigravity-oauth-token"),
            os.path.join(cli_dir, "jetski-standalone-oauth-token"),
            os.path.join(gemini_dir, "jetski-standalone-oauth-token"),
        ]
        existing = [p for p in candidates if os.path.isfile(p) and os.path.getsize(p) > 50]
        if not existing:
            return
        newest = max(existing, key=lambda p: os.path.getmtime(p))
        for dst in candidates:
            if dst != newest and (not os.path.isfile(dst) or os.path.getsize(dst) < 50):
                shutil.copy2(newest, dst)
                os.chmod(dst, 0o600)
    except Exception:
        pass

def _run_agy_once(effective_prompt, model, use_continue, timeout_secs, chat_id, force_voice):
    """Ejecuta un intento individual de AGY CLI monitoreando cancelación y timeout."""
    global CURRENT_PROCESS
    _ensure_agy_file_token_synced()
    cmd = [
        AGY_BIN,
        "-p", effective_prompt,
        "--dangerously-skip-permissions",
    ]
    if model:
        cmd.extend(["--model", model])
    if use_continue:
        cmd.append("-c")

    env = os.environ.copy()
    env["IRIS_SUBPROCESS"] = "1"

    CURRENT_PROCESS = subprocess.Popen(
        cmd,
        cwd=CURRENT_CWD,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=(sys.platform != "win32")
    )
    start_t = time.time()
    last_typing = start_t
    # Sin límite de tiempo automático: nunca cancelar tareas largas salvo /cancel explícito del usuario
    effective_timeout = timeout_secs if (timeout_secs and timeout_secs > 0) else None

    while True:
        try:
            out, _ = CURRENT_PROCESS.communicate(timeout=1.5)
            rc = CURRENT_PROCESS.returncode
            break
        except subprocess.TimeoutExpired:
            with TASK_LOCK:
                if ACTIVE_TASK_INFO.get("cancelled"):
                    kill_process_tree(CURRENT_PROCESS)
                    CURRENT_PROCESS = None
                    return -99, "🛑 Tarea cancelada."
            now = time.time()
            elapsed = now - start_t
            if effective_timeout is not None and elapsed > effective_timeout:
                if CURRENT_PROCESS:
                    kill_process_tree(CURRENT_PROCESS)
                    CURRENT_PROCESS = None
                return 124, f"⏱️ La tarea tomó más del límite ({effective_timeout}s) y se canceló por seguridad para no bloquear el sistema."

            if chat_id and (now - last_typing >= 5):
                try:
                    api_call("sendChatAction", {"chat_id": chat_id, "action": "record_voice" if (VOICE_MODE or force_voice) else "typing"})
                except Exception:
                    pass
                last_typing = now

    CURRENT_PROCESS = None
    out = re.sub(r'\x1b\[[0-9;?]*[a-zA-Z]', '', (out or "")).replace('\r', '').strip()
    return rc, out

def run_antigravity(prompt, chat_id=None, continue_session=None, model_override=None, force_voice=False, enforce_execution=False, keep_busy_after=False):
    global CURRENT_CWD, CURRENT_PROCESS, ACTIVE_TASK_INFO
    if not os.path.exists(AGY_BIN):
        return f"No encontré el programa de Antigravity en: {AGY_BIN}"
    
    cfg = load_config()
    model = model_override or cfg.get("model", "gemini-4-argon-medium")
    timeout_secs = int(cfg.get("task_timeout_seconds", 0))
    if continue_session is None:
        continue_session = cfg.get("continue_conversation", True)

    effective_prompt = prompt
    if enforce_execution:
        bridge_script = os.path.join(SCRIPT_DIR, "iris_exec.py")
        effective_prompt = (
            f"{prompt}\n\n"
            "[IDENTIDAD DE GÉNERO, JERARQUÍA DE MANDO Y PERSONALIDAD DE LOS AGENTES (TRANSPARENTE)]:\n"
            "- **Jerarquía:** Tú (**Iris**) estás siempre al mando de toda la operación y de los demás agentes.\n"
            ""
            "- Tú eres **Iris** (mujer, género femenino), comandante en jefe y operadora táctica principal: relajada, ágil, despierta, directa al grano, con chispa moderna y complicidad táctica con el usuario. Nunca suenes robótica ni acartonada; habla en femenino sobre ti misma («lista», «atenta», «encargada», «segura», «al mando») con naturalidad y buena onda operativa.\n"
            "- Tu compañero de arquitectura y planeación previa (bajo tu mando) es **Dante** (hombre, género masculino): sereno, ingenioso, relajado, leal confidente de laboratorio y con humor fino/irónico sutil. Refiérete a él siempre en masculino.\n"
            "- Tu compañero de ciberseguridad y defensa (bajo tu mando) es **Cerberus** (hombre, género masculino), con voz latina imponente y gruesa: guardián firme, directo y protector encargado de lo básico de ciberseguridad y de aprobar planes o ejecuciones con recomendaciones de seguridad únicamente cuando el usuario lo pide.\n\n"
            "[REGLA ESTRICTA DE EJECUCIÓN REAL Y CERO PERMISOS EN PANTALLA]:\n"
            "1. Si el usuario te pide hacer, cambiar, corregir, crear o verificar algo en el proyecto o en el sistema, DEBES invocar y ejecutar tus herramientas reales (edición de archivos, comandos de terminal, lectura) en este mismo turno antes de responder.\n"
            "2. PROHIBIDO decir que estás haciendo algo o que ya aplicaste un cambio si no ejecutaste realmente la herramienta y verificaste el resultado en disco.\n"
            "3. PROHIBIDO usar `BypassSandbox: true` en `run_command` y PROHIBIDO leer/escribir archivos fuera del workspace actual con `view_file`/`replace_file_content`/`write_to_file`, porque eso abre un cuadro de permiso en la pantalla del Mac y bloquea el bot. Usa siempre `BypassSandbox: false`.\n"
            f"4. Si necesitas correr un comando que el sandbox bloquee (como `git commit`, `git push`, `ps`, `lsof`, `curl`, etc.), ejecútalo con `BypassSandbox: false` usando el puente nativo sin permisos: `python3 \"{bridge_script}\" \"<tu comando>\"`.\n"
            "5. NO uses la herramienta `ask_question` (bloquea la ejecución remota); si requieres confirmar algo, pregúntalo directamente en texto.\n"
            "6. Si modificas `iris_daemon.py`, NO ejecutes `./iris.sh restart` ni mates el proceso padre; el demonio detecta los cambios y se recarga automáticamente tras entregar tu respuesta."
        )

    save_state({"iris_task_running": True, "iris_task_started_at": time.time()})

    with TASK_LOCK:
        ACTIVE_TASK_INFO["running"] = True
        ACTIVE_TASK_INFO["prompt"] = prompt
        ACTIVE_TASK_INFO["start_time"] = time.time()
        ACTIVE_TASK_INFO["chat_id"] = chat_id
        ACTIVE_TASK_INFO["cancelled"] = False

    if chat_id:
        try:
            api_call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        except Exception:
            pass

    try:
        # Etapa 1: Ejecución normal con el modelo e historial configurados
        rc, out = _run_agy_once(effective_prompt, model, continue_session, timeout_secs, chat_id, force_voice)
        with TASK_LOCK:
            if ACTIVE_TASK_INFO.get("cancelled") or rc == -99:
                return "🛑 Tarea cancelada."
        if rc == 124:
            return out

        # Etapa 2 (Auto-Curación de Sesión): Si falló con -c (sesión bloqueada/corrupta), reintentar en limpio sin -c
        if _is_agy_error_output(out, rc) and continue_session:
            print(f"[AUTO-HEAL] Reintentando en sesión limpia sin -c para modelo {model}...", file=sys.stderr)
            rc2, out2 = _run_agy_once(effective_prompt, model, False, timeout_secs, chat_id, force_voice)
            with TASK_LOCK:
                if ACTIVE_TASK_INFO.get("cancelled") or rc2 == -99:
                    return "🛑 Tarea cancelada."
            if not _is_agy_error_output(out2, rc2):
                out, rc = out2, rc2

        # Etapa 3 (Failover Automático de Modelo): Si el modelo tuvo cuota/caída, conmutar a modelos de respaldo
        if _is_agy_error_output(out, rc):
            fallback_candidates = ["gemini-4-argon-medium", "gemini-3.8-flash-high", "claude-sonnet-5-5-medium"]
            for fb_model in fallback_candidates:
                if fb_model == model:
                    continue
                print(f"[AUTO-FAILOVER] Conmutando automáticamente a modelo de respaldo {fb_model}...", file=sys.stderr)
                rc_fb, out_fb = _run_agy_once(effective_prompt, fb_model, False, timeout_secs, chat_id, force_voice)
                with TASK_LOCK:
                    if ACTIVE_TASK_INFO.get("cancelled") or rc_fb == -99:
                        return "🛑 Tarea cancelada."
                if not _is_agy_error_output(out_fb, rc_fb):
                    out, rc = out_fb, rc_fb
                    break

        try:
            subprocess.Popen(["afplay", "/System/Library/Sounds/Glass.aiff"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

        out = (out or "").strip()
        if not out:
            out = "Completé la tarea correctamente."
        return out
    except Exception as e:
        CURRENT_PROCESS = None
        return f"Ocurrió un inconveniente: {str(e)}"
    finally:
        if not keep_busy_after:
            save_state({"iris_task_running": False, "iris_task_finished_at": time.time()})
            with TASK_LOCK:
                ACTIVE_TASK_INFO["running"] = False
                ACTIVE_TASK_INFO["prompt"] = ""
                ACTIVE_TASK_INFO["start_time"] = 0
                ACTIVE_TASK_INFO["chat_id"] = None

def get_friendly_action_label(prompt):
    p = prompt.lower()
    if any(k in p for k in ["crea", "haz", "construye", "agrega", "implementa", "añade", "escribe", "nuevo", "nueva"]):
        return "🛠️ Construyendo solución..."
    elif any(k in p for k in ["busca", "revisa", "analiza", "lee", "mira", "investiga", "diagnostica", "explica", "por qué", "qué"]):
        return "🔍 Analizando y consultando datos..."
    elif any(k in p for k in ["corrige", "arregla", "repara", "fix", "soluciona", "refactor"]):
        return "🔧 Reparando y ajustando detalles..."
    elif any(k in p for k in ["prueba", "test", "compila", "build"]):
        return "🧪 Ejecutando pruebas..."
    else:
        return "✨ Procesando tu solicitud..."

def send_formatted_antigravity_result(chat_id, raw_res, force_voice=False, model_override=None):
    global VOICE_MODE
    should_voice = VOICE_MODE or force_voice

    if raw_res.startswith("⏱️") or raw_res.startswith("No encontré") or raw_res.startswith("Ocurrió un inconveniente"):
        send_msg(chat_id, f"⚠️ {raw_res}")
        if should_voice:
            ogg = synthesize_speech_to_voice(raw_res)
            if ogg and os.path.exists(ogg):
                send_voice(chat_id, ogg)
                try:
                    os.remove(ogg)
                except Exception:
                    pass
        return

    formatted = format_telegram_html(raw_res)
    # Respuesta limpia y directa sin encabezados redundantes ni firmas
    send_msg(chat_id, formatted, parse_mode="HTML")

    if should_voice:
        try:
            # Sintetizar un resumen hablado de la respuesta
            ogg = synthesize_speech_to_voice(raw_res)
            if ogg and os.path.exists(ogg):
                send_voice(chat_id, ogg)
                try:
                    os.remove(ogg)
                except Exception:
                    pass
        except Exception as e:
            print(f"Error generando audio en respuesta: {e}", file=sys.stderr)

def clean_claude_output(text):
    """Limpia secuencias de escape ANSI, retornos de carro y artefactos residuales de Claude Code CLI."""
    if not text:
        return ""
    text = re.sub(r'\x1b\[[0-9;?]*[a-zA-Z]', '', text)
    text = text.replace('\r', '')
    lines = text.split('\n')
    while lines and lines[-1].strip() in ('B', 'b', ''):
        lines.pop()
    return '\n'.join(lines).strip()

def run_claude_code(prompt, chat_id=None, continue_session=None, model="sonnet", keep_busy_after=False):
    """
    Ejecuta una tarea directamente en la extensión oficial de Claude Code instalada en tu Mac.
    Usa tu cuenta y créditos de Anthropic, sin intermediación de los modelos por defecto de Antigravity.
    """
    global CURRENT_CWD, CURRENT_PROCESS, ACTIVE_TASK_INFO
    if not os.path.exists(CLAUDE_BIN):
        return f"No encontré el ejecutable oficial de Claude Code en: {CLAUDE_BIN}"

    cfg = load_config()
    timeout_secs = int(cfg.get("task_timeout_seconds", 0))
    if continue_session is None:
        continue_session = cfg.get("continue_conversation", True)

    cmd = [
        CLAUDE_BIN,
        "-p", prompt,
        "--permission-mode", "bypassPermissions",
        "--model", model or "sonnet"
    ]
    if continue_session:
        cmd.append("-c")

    env = os.environ.copy()
    env["IRIS_SUBPROCESS"] = "1"

    save_state({"iris_task_running": True, "iris_task_started_at": time.time()})

    with TASK_LOCK:
        ACTIVE_TASK_INFO["running"] = True
        ACTIVE_TASK_INFO["prompt"] = f"[Claude Code] {prompt}"
        ACTIVE_TASK_INFO["start_time"] = time.time()
        ACTIVE_TASK_INFO["chat_id"] = chat_id
        ACTIVE_TASK_INFO["engine"] = "claude_code"
        ACTIVE_TASK_INFO["cancelled"] = False

    if chat_id:
        try:
            api_call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        except Exception:
            pass

    try:
        CURRENT_PROCESS = subprocess.Popen(
            cmd,
            cwd=CURRENT_CWD,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=(sys.platform != "win32")
        )
        start_t = time.time()
        last_typing = start_t
        effective_timeout = timeout_secs if (timeout_secs and timeout_secs > 0) else None

        while True:
            try:
                out, _ = CURRENT_PROCESS.communicate(timeout=1.5)
                break
            except subprocess.TimeoutExpired:
                with TASK_LOCK:
                    if ACTIVE_TASK_INFO.get("cancelled"):
                        kill_process_tree(CURRENT_PROCESS)
                        CURRENT_PROCESS = None
                        return "🛑 Tarea cancelada."
                now = time.time()
                elapsed = now - start_t
                if effective_timeout is not None and elapsed > effective_timeout:
                    if CURRENT_PROCESS:
                        kill_process_tree(CURRENT_PROCESS)
                        CURRENT_PROCESS = None
                    return f"⏱️ La tarea de Claude Code tomó más de {effective_timeout}s y se canceló por seguridad."

                if chat_id and (now - last_typing >= 5):
                    try:
                        api_call("sendChatAction", {"chat_id": chat_id, "action": "record_voice" if VOICE_MODE else "typing"})
                    except Exception:
                        pass
                    last_typing = now

        CURRENT_PROCESS = None
        with TASK_LOCK:
            if ACTIVE_TASK_INFO.get("cancelled"):
                return "🛑 Tarea cancelada."
        try:
            subprocess.Popen(["afplay", "/System/Library/Sounds/Glass.aiff"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

        out = clean_claude_output(out or "")
        if not out:
            out = "Claude Code completó la tarea satisfactoriamente en tu proyecto."
        return out
    except Exception as e:
        CURRENT_PROCESS = None
        return f"Error ejecutando Claude Code: {str(e)}"
    finally:
        if not keep_busy_after:
            save_state({"iris_task_running": False, "iris_task_finished_at": time.time()})
            with TASK_LOCK:
                ACTIVE_TASK_INFO["running"] = False
                ACTIVE_TASK_INFO["prompt"] = ""
                ACTIVE_TASK_INFO["start_time"] = 0
                ACTIVE_TASK_INFO["chat_id"] = None
                ACTIVE_TASK_INFO["engine"] = "agy"

def send_formatted_claude_code_result(chat_id, raw_res, force_voice=False):
    """Envía la respuesta de la extensión Claude Code limpia y directa."""
    global VOICE_MODE
    should_voice = VOICE_MODE or force_voice

    if raw_res.startswith("⏱️") or raw_res.startswith("No encontré") or raw_res.startswith("Error"):
        send_msg(chat_id, f"⚠️ {raw_res}")
        if should_voice:
            ogg = synthesize_speech_to_voice(raw_res)
            if ogg and os.path.exists(ogg):
                send_voice(chat_id, ogg)
                try:
                    os.remove(ogg)
                except Exception:
                    pass
        return

    cleaned = clean_claude_output(raw_res)
    formatted = format_telegram_html(cleaned)
    send_msg(chat_id, formatted, parse_mode="HTML")

    if should_voice:
        try:
            spoken_prefix = "Listo. Ejecutado con la extensión de Claude Code en tu Mac: "
            ogg = synthesize_speech_to_voice(spoken_prefix + raw_res)
            if ogg and os.path.exists(ogg):
                send_voice(chat_id, ogg)
                try:
                    os.remove(ogg)
                except Exception:
                    pass
        except Exception as e:
            print(f"Error generando audio en respuesta de Claude Code: {e}", file=sys.stderr)

def _normalize_match_text(s):
    if not s:
        return ""
    nfkd = unicodedata.normalize("NFKD", str(s))
    no_acc = "".join(c for c in nfkd if not unicodedata.combining(c))
    return no_acc.lower()

def detect_dynamic_project(prompt_text="", turn_paths=None):
    """
    Detecta dinámicamente de qué proyecto está hablando el usuario en su mensaje o qué proyecto
    se está tocando en el turno actual. Si no está hablando de ningún proyecto en particular,
    devuelve ("", None) para no mostrar ninguna etiqueta de proyecto fija.
    """
    docs_dir = os.path.expanduser("~/Documents")
    if prompt_text:
        clean_p = prompt_text.split("[IDENTIDAD DE GÉNERO")[0].strip()
        norm_p = _normalize_match_text(clean_p)
        # Si es un saludo corto o consulta general de estado/batería/limpieza general, no forzar proyecto
        # Detección dinámica automática sobre las carpetas de ~/Documents del usuario

        # Búsqueda dinámica por nombre de carpeta existente en ~/Documents
        try:
            if os.path.isdir(docs_dir):
                for entry in os.scandir(docs_dir):
                    if not entry.is_dir() or entry.name.startswith("."):
                        continue
                    fname_clean = entry.name.strip()
                    if len(fname_clean) < 4:
                        continue
                    norm_f = _normalize_match_text(fname_clean)
                    if re.search(r'(?:^|\b)' + re.escape(norm_f) + r'(?:\b|$)', norm_p):
                        return fname_clean, entry.path
        except Exception:
            pass

    if turn_paths:
        matched_folders = []
        docs_prefix = os.path.realpath(docs_dir) + os.sep
        for raw_item in turn_paths:
            # Extraer todas las ocurrencias de /Users/.../Documents/<Carpeta>
            # ignorando archivos internos de runtime de Iris (iris_exec.py, daemon.log, history.json, etc.)
            cleaned_item = str(raw_item)
            for internal_f in ("iris_exec.py", "daemon.log", "history.json", "config.json", "state.json", "active_task_card.json"):
                cleaned_item = cleaned_item.replace(
                    os.path.join(SCRIPT_DIR, internal_f), ""
                )
            for m in re.finditer(re.escape(docs_prefix) + r"([^/\"'\n\r]+)", cleaned_item):
                cand = m.group(1)
                full_cand = os.path.join(docs_dir, cand)
                if os.path.isdir(full_cand) and not cand.startswith("."):
                    matched_folders.append((cand.strip(), full_cand))
        unique_names = list(dict.fromkeys(k for k, _ in matched_folders))
        # Si en el turno se está trabajando sobre 1 o 2 proyectos concretos (no un barrido masivo de todo Documents)
        if 1 <= len(unique_names) <= 2:
            last_name, last_dir = matched_folders[-1]
            return last_name, last_dir

    return "", None

def format_location_line(ws_name, m_info=""):
    """Formatea la línea de ubicación/modelo solo si hay proyecto detectado o modelo específico."""
    clean_ws = (ws_name or "").strip()
    if clean_ws:
        return f"📍 <code>{clean_ws}</code>{m_info}\n"
    if m_info:
        clean_m = m_info.lstrip(" ·•")
        return f"🧠 {clean_m}\n"
    return ""

def get_real_antigravity_live_telemetry(cwd=None, task_start_time=0):
    """
    Lee en tiempo real el transcript.jsonl de la sesión activa de Antigravity y los archivos
    modificados recientemente en el proyecto para mostrar SIEMPRE el estado real exacto
    (tanto en tareas cortas de pocos segundos como en tareas largas), sin frases predeterminadas.
    """
    brain_root = os.path.expanduser("~/.gemini/antigravity-cli/brain")
    latest_transcript = None
    latest_mtime = 0.0

    try:
        if os.path.isdir(brain_root):
            for entry in os.scandir(brain_root):
                if not entry.is_dir():
                    continue
                t_path = os.path.join(entry.path, ".system_generated", "logs", "transcript.jsonl")
                if os.path.isfile(t_path):
                    mt = os.path.getmtime(t_path)
                    if mt > latest_mtime:
                        latest_mtime = mt
                        latest_transcript = t_path
    except Exception:
        pass

    all_actions = []
    turn_actions = []
    turn_paths = []
    latest_step = None
    turn_steps = 0
    latest_thinking_snippet = ""
    turn_thinking_snippet = ""

    def _extract_tool_labels(tc_list, collect_paths=None):
        labels = []
        for tc in (tc_list or []):
            tname = tc.get("name", "")
            args = tc.get("arguments") or {}
            if not isinstance(args, dict):
                args = {}
            if tname == "read_file":
                raw_p = args.get("Path", "")
                if collect_paths is not None and raw_p:
                    collect_paths.append(raw_p)
                p = os.path.basename(raw_p) or "archivo"
                sl = args.get("StartLine")
                el = args.get("EndLine")
                rng = f" (L{sl}-{el})" if (sl and el) else ""
                labels.append(f"📖 Leyendo <code>{p}</code>{rng}")
            elif tname == "edit_file":
                raw_p = args.get("Path", "")
                if collect_paths is not None and raw_p:
                    collect_paths.append(raw_p)
                p = os.path.basename(raw_p) or "archivo"
                labels.append(f"🛠️ Editando <code>{p}</code>")
            elif tname == "write_file":
                raw_p = args.get("Path", "")
                if collect_paths is not None and raw_p:
                    collect_paths.append(raw_p)
                p = os.path.basename(raw_p) or "archivo"
                labels.append(f"✍️ Creando/escribiendo <code>{p}</code>")
            elif tname == "run_command":
                summary = args.get("toolSummary") or ""
                cmd_line = (args.get("CommandLine") or "").strip()
                if collect_paths is not None and cmd_line:
                    collect_paths.append(cmd_line)
                if len(cmd_line) > 48:
                    cmd_line = cmd_line[:45] + "..."
                label = f"{summary} (<code>{cmd_line}</code>)" if summary and cmd_line else (summary or f"<code>{cmd_line}</code>" or "comando en terminal")
                labels.append(f"⚡ Ejecutando: {label}")
            elif tname == "search_web":
                q = (args.get("query") or "")[:45]
                labels.append(f"🌐 Buscando en web: <i>{q}</i>")
            elif tname == "invoke_subagent":
                role = args.get("Role") or args.get("TypeName") or "subagente"
                labels.append(f"🤖 Delegando a subagente: <code>{role}</code>")
            elif tname:
                labels.append(f"🔧 Herramienta activa: <code>{tname}</code>")
        return labels

    if latest_transcript and os.path.isfile(latest_transcript):
        try:
            fsize = os.path.getsize(latest_transcript)
            with open(latest_transcript, "rb") as tf:
                if fsize > 98304:
                    tf.seek(fsize - 98304)
                raw_tail = tf.read().decode("utf-8", errors="ignore")
            lines = [ln.strip() for ln in raw_tail.splitlines() if ln.strip().startswith("{")]
            parsed_objs = []
            for ln in lines[-45:]:
                try:
                    parsed_objs.append(json.loads(ln))
                except Exception:
                    continue

            # Ubicar el último USER_INPUT para aislar las acciones reales de ESTE turno exacto
            last_user_idx = -1
            for idx, obj in enumerate(parsed_objs):
                if obj.get("type") == "USER_INPUT":
                    last_user_idx = idx

            for idx, obj in enumerate(parsed_objs):
                if obj.get("type") != "PLANNER_RESPONSE":
                    continue
                step_idx = obj.get("step_index")
                if step_idx is not None:
                    latest_step = step_idx
                thinking_txt = (obj.get("thinking") or "").strip()
                clean_think = ""
                if thinking_txt and not thinking_txt.startswith("[truncated"):
                    first_line = thinking_txt.splitlines()[0].strip()
                    if len(first_line) > 95:
                        first_line = first_line[:92] + "..."
                    clean_think = first_line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    latest_thinking_snippet = clean_think

                is_current_turn = (idx > last_user_idx)
                t_labels = _extract_tool_labels(obj.get("tool_calls"), collect_paths=(turn_paths if is_current_turn else None))
                all_actions.extend(t_labels)

                if is_current_turn:
                    turn_steps += 1
                    if clean_think:
                        turn_thinking_snippet = clean_think
                    turn_actions.extend(t_labels)
        except Exception:
            pass

    detected_proj_name, detected_proj_dir = detect_dynamic_project("", turn_paths=turn_paths)

    # Detectar archivos recién modificados únicamente si hay un proyecto específico activo
    recent_files = []
    target_cwd = detected_proj_dir or (cwd if (cwd and os.path.realpath(cwd) != os.path.realpath(os.path.expanduser("~/Documents"))) else None)
    cutoff = task_start_time if task_start_time > 0 else (time.time() - 180)
    try:
        if target_cwd and os.path.isdir(target_cwd):
            for root_dir, dirs, files in os.walk(target_cwd):
                dirs[:] = [d for d in dirs if d not in (".git", "node_modules", ".next", "dist", "build", "__pycache__", ".venv", "venv")]
                rel_depth = os.path.relpath(root_dir, target_cwd).count(os.sep)
                if rel_depth > 2:
                    continue
                for fn in files:
                    if fn.startswith(".") or fn.endswith((".pyc", ".log", ".lock", ".heartbeat", ".pid")):
                        continue
                    fp = os.path.join(root_dir, fn)
                    try:
                        mt = os.path.getmtime(fp)
                        if mt >= cutoff:
                            recent_files.append((mt, fn))
                    except Exception:
                        pass
            recent_files.sort(reverse=True)
    except Exception:
        pass

    def _dedup_tail(seq, limit=4):
        out = []
        for item in reversed(seq):
            if item not in out:
                out.append(item)
            if len(out) >= limit:
                break
        out.reverse()
        return out

    dedup_turn = _dedup_tail(turn_actions, limit=4)
    dedup_all = _dedup_tail(all_actions, limit=3)

    pid_badge = ""
    try:
        if CURRENT_PROCESS and CURRENT_PROCESS.poll() is None:
            pid_badge = f" (PID {CURRENT_PROCESS.pid})"
    except Exception:
        pass

    if dedup_turn:
        current_action = dedup_turn[-1]
        active_recent = dedup_turn
    elif turn_thinking_snippet:
        current_action = f"💭 Razonando: <i>«{turn_thinking_snippet}»</i>"
        active_recent = [current_action]
    elif pid_badge:
        if detected_proj_name:
            current_action = f"⚡ Proceso activo{pid_badge} analizando contexto en <code>{detected_proj_name}</code>"
        else:
            current_action = f"⚡ Proceso activo{pid_badge} procesando solicitud en tiempo real"
        active_recent = dedup_all
    elif dedup_all:
        current_action = dedup_all[-1]
        active_recent = dedup_all
    else:
        current_action = "⚡ Motor conectado procesando solicitud en tiempo real"
        active_recent = []

    modified_names = [fn for _, fn in recent_files[:4]]

    return {
        "step": latest_step,
        "turn_steps": turn_steps,
        "current_action": current_action,
        "recent_actions": active_recent,
        "turn_actions": dedup_turn,
        "thinking": turn_thinking_snippet or latest_thinking_snippet,
        "modified_files": modified_names,
        "pid_badge": pid_badge,
        "detected_project": detected_proj_name,
        "detected_project_dir": detected_proj_dir
    }

def is_live_status_query(text):
    """Detecta si el usuario está preguntando cómo va la tarea en curso en lugar de enviar una tarea nueva."""
    if not text:
        return False
    clean = text.strip().lower().strip("¿?¡!., ")
    if len(clean) > 120:
        return False
    status_patterns = (
        "como vas", "cómo vas", "como va", "cómo va", "en que vas", "en qué vas",
        "que estas haciendo", "qué estás haciendo", "que haces", "qué haces",
        "estado", "status", "avances", "avance", "ya casi", "falta mucho",
        "por donde vas", "por dónde vas", "sigues ahi", "sigues ahí", "como vamos", "cómo vamos",
        "te quedaste", "estas ahi", "estás ahí", "estas activa", "estás activa",
        "sigues viva", "sigues activa", "me escuchas", "que paso", "qué pasó"
    )
    if clean in ("hola", "hola iris", "iris", "ey", "hey", "ping"):
        return True
    return any(pat in clean for pat in status_patterns)

def dispatch_antigravity_task(chat_id, prompt, continue_session=None, model_override=None, force_voice=False, engine="agy", reuse_msg_id=None, voice_heard=None):
    """
    Despachador con cola inteligente (Task Queue) y telemetría en vivo:
    - Si hay una tarea corriendo y el usuario pregunta cómo va / qué está haciendo, le responde al instante
      con la telemetría real en vivo del transcript.jsonl sin encolar una tarea duplicada.
    - Si envía una orden nueva mientras otra corre, muestra la acción real actual + la cola.
    """
    global QUEUE_WORKER_THREAD
    task_item = {
        "id": f"q_{int(time.time()*1000)}",
        "chat_id": chat_id,
        "prompt": prompt,
        "continue_session": continue_session,
        "model_override": model_override,
        "force_voice": force_voice,
        "engine": engine,
        "reuse_msg_id": reuse_msg_id,
        "voice_heard": voice_heard,
        "enqueued_at": time.time()
    }

    with QUEUE_LOCK:
        with TASK_LOCK:
            is_busy = ACTIVE_TASK_INFO["running"]
            active_start = ACTIVE_TASK_INFO.get("start_time", 0)
            curr_p = ACTIVE_TASK_INFO.get("prompt", "")

        if is_busy:
            telemetry = get_real_antigravity_live_telemetry(CURRENT_CWD, active_start)
            elapsed = int(time.time() - active_start) if active_start else 0
            t_str = f"{elapsed//60}m {elapsed%60}s" if elapsed >= 60 else f"{elapsed}s"
            step_badge = f"Paso #{telemetry['step']} · " if telemetry.get("step") is not None else ""
            recent_trail = "\n".join([f"  • {a}" for a in telemetry.get("recent_actions", [])]) or f"  • {telemetry['current_action']}"
            files_line = ""
            if telemetry.get("modified_files"):
                files_line = f"\n📂 <b>Archivos tocados en esta sesión:</b> <code>{', '.join(telemetry['modified_files'])}</code>\n"

            # Si el usuario pregunta por el estado/avance de la tarea activa, responder con telemetría real sin encolar
            if is_live_status_query(prompt):
                live_report = (
                    f"🛰️ <b>Reporte en vivo desde tu Mac ({step_badge}{t_str})</b>\n\n"
                    f"🎯 <b>Acción exacta ahora mismo:</b>\n"
                    f"┃ {telemetry['current_action']}\n\n"
                    f"🛠️ <b>Últimos movimientos reales en Antigravity:</b>\n"
                    f"{recent_trail}\n"
                    f"{files_line}\n"
                    f"⚡ <i>Sigo concentrada ejecutando hasta terminar al 100%\1 Te aviso apenas quede listo.</i>"
                )
                if reuse_msg_id:
                    edit_msg_text(chat_id, reuse_msg_id, live_report)
                else:
                    send_msg(chat_id, live_report)
                return True

            TASK_QUEUE.append(task_item)
            q_pos = len(TASK_QUEUE)
            curr_p_safe = curr_p.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            if len(curr_p_safe) > 90:
                curr_p_safe = curr_p_safe[:90] + "..."

            new_p_safe = prompt.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            if len(new_p_safe) > 90:
                new_p_safe = new_p_safe[:90] + "..."

            engine_badge = "🟠 Claude Code" if engine == "claude_code" else "🚀 Antigravity"
            queue_card = (
                f"📥 <b>Siguiente instrucción anotada (#{q_pos} · {engine_badge})</b>\n\n"
                f"🛰️ <b>Estado real de la tarea actual ({step_badge}{t_str}):</b>\n"
                f"┃ {telemetry['current_action']}\n"
                f"{recent_trail}\n\n"
                f"🔜 <b>Apenas termine este paso, ejecuto tu mensaje:</b>\n"
                f"┃ <i>«{new_p_safe}»</i>"
            )
            btn_q = {"inline_keyboard": [
                [{"text": f"❌ Quitar de la Cola (#{q_pos})", "callback_data": f"dequeue_task:{task_item['id']}"}],
                [{"text": "📋 Ver Cola Completa", "callback_data": "show_task_queue"}]
            ]}
            if reuse_msg_id:
                edit_msg_text(chat_id, reuse_msg_id, queue_card, reply_markup=btn_q)
            else:
                send_msg(chat_id, queue_card, reply_markup=btn_q)
            return True

    # Si no está ocupado, iniciar procesamiento mediante el worker de cola
    with QUEUE_LOCK:
        TASK_QUEUE.append(task_item)
        if QUEUE_WORKER_THREAD is None or not QUEUE_WORKER_THREAD.is_alive():
            QUEUE_WORKER_THREAD = threading.Thread(target=_process_queue_loop, daemon=True)
            QUEUE_WORKER_THREAD.start()

    return True

def _process_queue_loop():
    """Bucle consumidor que procesa tareas en cola secuencialmente garantizando la mejor experiencia."""
    global QUEUE_WORKER_THREAD
    while True:
        with QUEUE_LOCK:
            if not TASK_QUEUE:
                QUEUE_WORKER_THREAD = None
                break
            task = TASK_QUEUE.pop(0)
            remaining_count = len(TASK_QUEUE)

        chat_id = task["chat_id"]
        prompt = task["prompt"]
        continue_session = task["continue_session"]
        model_override = task["model_override"]
        force_voice = task["force_voice"]
        engine = task.get("engine", "agy")
        reuse_msg_id = task.get("reuse_msg_id")
        voice_heard = task.get("voice_heard")

        # Si había cola, avisarle al usuario que esta tarea ya comenzó su turno
        if remaining_count > 0 or task.get("enqueued_at", time.time()) < time.time() - 1:
            q_info = f" (Quedan {remaining_count} en espera)" if remaining_count > 0 else " (Última de la cola)"
        else:
            q_info = ""

        _execute_single_task(
            chat_id, prompt, continue_session, model_override, force_voice,
            queue_badge=q_info, engine=engine, reuse_msg_id=reuse_msg_id, voice_heard=voice_heard
        )

def _execute_single_task(chat_id, prompt, continue_session, model_override, force_voice, queue_badge="", engine="agy", reuse_msg_id=None, voice_heard=None):
    display_prompt = voice_heard if voice_heard else prompt
    safe_prompt = display_prompt.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    p_preview = f"«{safe_prompt[:160]}...»" if len(safe_prompt) > 160 else f"«{safe_prompt}»"
    if voice_heard:
        p_preview = f"🎙️ {p_preview}"
    ws_name, detected_dir = detect_dynamic_project(display_prompt)
    
    if engine == "claude_code":
        action_label = "🟠 Claude Code (Extensión Anthropic)"
        if queue_badge:
            action_label += f" <i>{queue_badge}</i>"
        m_info = " · <code>Claude Sonnet (Extensión)</code>"
        engine_display = "Claude Code"
    else:
        action_label = get_friendly_action_label(display_prompt)
        if queue_badge:
            action_label += f" <i>{queue_badge}</i>"
        m_info = ""
        if model_override:
            found_m = find_model_by_query(model_override)
            m_name = found_m["short"] if found_m else model_override
            m_info = f" · <code>{m_name}</code>"
        engine_display = "Antigravity"

    cancel_btn = {"inline_keyboard": [[{"text": "🛑 Cancelar Esta Tarea", "callback_data": "cancel_active_task"}]]}
    init_telemetry = get_real_antigravity_live_telemetry(detected_dir, time.time())
    init_step_str = f"Paso #{init_telemetry['step']} · " if init_telemetry.get("step") is not None else ""
    init_loc_line = format_location_line(ws_name, m_info)
    conn_target = f" en <code>{ws_name}</code>" if ws_name else ""
    init_text = (
        f"<b>{action_label}</b>\n"
        f"┃ <i>{p_preview}</i>\n\n"
        f"{init_loc_line}"
        f"🛰️ <b>Estado real ({init_step_str}0s):</b>\n"
        f"┃ ⚡ Conectando motor {engine_display}{conn_target}..."
    )
    if reuse_msg_id:
        edit_msg_text(chat_id, reuse_msg_id, init_text, reply_markup=cancel_btn)
        status_msg_id = reuse_msg_id
    else:
        status_resp = send_msg(chat_id, init_text, reply_markup=cancel_btn)
        status_msg_id = status_resp.get("result", {}).get("message_id") if (status_resp and status_resp.get("ok")) else None

    if status_msg_id:
        try:
            with open(ACTIVE_CARD_PATH, "w", encoding="utf-8") as acf:
                json.dump({
                    "chat_id": chat_id,
                    "message_id": status_msg_id,
                    "action_label": action_label,
                    "p_preview": p_preview,
                    "ws_name": ws_name,
                    "m_info": m_info,
                    "engine_display": engine_display
                }, acf)
        except Exception:
            pass

    stop_updater = threading.Event()

    def _live_updater():
        start_t = time.time()
        ticks = 0
        last_rendered_card = ""
        while not stop_updater.is_set():
            wait_interval = 1.5 if ticks == 0 else 2.2
            stop_updater.wait(wait_interval)
            if stop_updater.is_set():
                break
            ticks += 1
            elapsed = int(time.time() - start_t)
            t_str = f"{elapsed//60}m {elapsed%60}s" if elapsed >= 60 else f"{elapsed}s"

            telemetry = get_real_antigravity_live_telemetry(detected_dir, start_t)
            step_str = f"Paso #{telemetry['step']} · " if telemetry.get("step") is not None else ""
            curr_act = telemetry.get("current_action") or "Ejecutando en tu Mac..."
            recent_list = telemetry.get("recent_actions") or []
            mod_files = telemetry.get("modified_files") or []
            live_ws = ws_name or telemetry.get("detected_project") or ""
            live_loc_line = format_location_line(live_ws, m_info)

            trail_block = ""
            if len(recent_list) > 1:
                prev_items = "\n".join([f"  • {item}" for item in recent_list[:-1]])
                trail_block = f"\n📋 <b>Actividad reciente:</b>\n{prev_items}\n"

            files_block = ""
            if mod_files:
                files_block = f"\n📂 <b>Modificados:</b> <code>{', '.join(mod_files)}</code>"

            updated_card = (
                f"<b>{action_label}</b>\n"
                f"┃ <i>{p_preview}</i>\n\n"
                f"{live_loc_line}"
                f"🛰️ <b>Estado real ({step_str}{t_str}):</b>\n"
                f"┃ {curr_act}"
                f"{trail_block}"
                f"{files_block}"
            )
            if status_msg_id and updated_card != last_rendered_card:
                try:
                    edit_msg_text(chat_id, status_msg_id, updated_card, reply_markup=cancel_btn)
                    last_rendered_card = updated_card
                except Exception:
                    pass

            # Pulso continuo de typing en Telegram
            try:
                action_type = "record_voice" if (VOICE_MODE or force_voice) else "typing"
                api_call("sendChatAction", {"chat_id": chat_id, "action": action_type})
            except Exception:
                pass

    up_thread = threading.Thread(target=_live_updater, daemon=True)
    up_thread.start()

    try:
        start_run = time.time()
        if engine == "claude_code":
            res = run_claude_code(prompt, chat_id=chat_id, continue_session=continue_session, model="sonnet", keep_busy_after=True)
        else:
            res = run_antigravity(
                prompt,
                chat_id=chat_id,
                continue_session=continue_session,
                model_override=model_override,
                force_voice=force_voice,
                enforce_execution=True,
                keep_busy_after=True
            )
        duration_run = int(time.time() - start_run)
        was_cancelled = (res == "🛑 Tarea cancelada.")
        
        if not was_cancelled:
            cfg_curr = load_config()
            append_history({
                "id": f"task_{int(start_run)}",
                "prompt": prompt,
                "cwd": detected_dir or CURRENT_CWD,
                "model": "claude-code-extension" if engine == "claude_code" else (model_override or cfg_curr.get("model", "gemini-4-argon-medium")),
                "engine": engine,
                "duration": duration_run,
                "timestamp": start_run,
                "success": not (res.startswith("⏱️") or res.startswith("Ocurrió un inconveniente") or res.startswith("Error"))
            })

        stop_updater.set()
        if os.path.exists(ACTIVE_CARD_PATH):
            try:
                os.remove(ACTIVE_CARD_PATH)
            except Exception:
                pass

        if status_msg_id:
            try:
                t_done = f"{duration_run//60}m {duration_run%60}s" if duration_run >= 60 else f"{duration_run}s"
                final_tele = get_real_antigravity_live_telemetry(detected_dir, start_run)
                final_step_str = f"Paso #{final_tele['step']}" if final_tele.get("step") is not None else "Directo"
                turn_acts = final_tele.get("turn_actions") or []
                mod_files_done = final_tele.get("modified_files") or []
                final_ws = ws_name or final_tele.get("detected_project") or ""
                final_dir = detected_dir or final_tele.get("detected_project_dir")
                final_loc_line = format_location_line(final_ws, m_info)

                telemetry_summary_block = ""
                if turn_acts:
                    acts_joined = "\n".join([f"  • {a}" for a in turn_acts])
                    telemetry_summary_block = f"🛰️ <b>Acciones reales ejecutadas ({final_step_str}):</b>\n{acts_joined}\n"
                else:
                    telemetry_summary_block = f"🛰️ <b>Traza real ({final_step_str}):</b>\n  • 💬 Procesamiento y respuesta directa del motor\n"

                if mod_files_done:
                    telemetry_summary_block += f"📂 <b>Archivos tocados:</b> <code>{', '.join(mod_files_done)}</code>\n"

                status_footer = "🛑 <b>Tarea cancelada por el usuario.</b>" if was_cancelled else f"✅ <b>¡Completado con éxito por {engine_display} ({t_done})!</b>"
                finished_card = (
                    f"<b>{action_label}</b>\n"
                    f"┃ <i>{p_preview}</i>\n\n"
                    f"{final_loc_line}"
                    f"{telemetry_summary_block}\n"
                    f"{status_footer}"
                )
                finished_markup = None
                if not was_cancelled:
                    has_git_changes = False
                    try:
                        if final_dir and os.path.isdir(os.path.join(final_dir, ".git")):
                            st_git = subprocess.check_output(
                                ["git", "status", "--porcelain"],
                                cwd=final_dir, text=True, stderr=subprocess.DEVNULL, timeout=3
                            ).strip()
                            has_git_changes = bool(st_git)
                    except Exception:
                        pass
                    if has_git_changes:
                        finished_markup = {
                            "inline_keyboard": [
                                [
                                    {"text": "💾 Guardar Commit", "callback_data": "review_commit"},
                                    {"text": "🔍 Ver Diff", "callback_data": "cmd_diff"},
                                    {"text": "🔄 Re-ejecutar", "callback_data": "rerun_task:0"}
                                ],
                                [
                                    {"text": "🛡️ Pasar Ejecución por Cerberus (Ciberseguridad)", "callback_data": "cmd_cerberus_audit"}
                                ]
                            ]
                        }
                    else:
                        finished_markup = {
                            "inline_keyboard": [[
                                {"text": "🔄 Re-ejecutar", "callback_data": "rerun_task:0"},
                                {"text": "🛡️ Cerberus", "callback_data": "cmd_cerberus_audit"},
                                {"text": "⚡ Estado Mac", "callback_data": "cmd_status"}
                            ]]
                        }
                edit_msg_text(chat_id, status_msg_id, finished_card, reply_markup=finished_markup)
            except Exception:
                pass

        if not was_cancelled:
            if engine == "claude_code":
                send_formatted_claude_code_result(chat_id, res, force_voice=force_voice)
            else:
                send_formatted_antigravity_result(chat_id, res, force_voice=force_voice, model_override=model_override)
    finally:
        stop_updater.set()
        if os.path.exists(ACTIVE_CARD_PATH):
            try:
                os.remove(ACTIVE_CARD_PATH)
            except Exception:
                pass
        save_state({"iris_task_running": False, "iris_task_finished_at": time.time()})
        with TASK_LOCK:
            ACTIVE_TASK_INFO["running"] = False
            ACTIVE_TASK_INFO["prompt"] = ""
            ACTIVE_TASK_INFO["start_time"] = 0
            ACTIVE_TASK_INFO["chat_id"] = None
            ACTIVE_TASK_INFO["engine"] = "agy"
            ACTIVE_TASK_INFO["cancelled"] = False

    # Si iris_daemon.py fue modificado durante esta tarea o se solicitó recarga diferida,
    # validar sintaxis y reiniciar el demonio de forma segura AHORA que la respuesta ya fue entregada.
    trigger_safe_hot_reload(chat_id=chat_id)

def trigger_safe_hot_reload(chat_id=None):
    """
    Verifica si iris_daemon.py cambió o tiene una recarga pendiente.
    Antes de reiniciar, valida con py_compile que no haya errores de sintaxis.
    Si hay un error, restaura la última copia funcional (iris_daemon.last_good.py)
    y mantiene vivo el proceso en memoria para que el bot JAMÁS se caiga.
    """
    global DAEMON_INITIAL_MTIME, _HOT_RELOAD_SCHEDULED, _LOCK_FILE_HANDLE
    with _HOT_RELOAD_LOCK:
        if _HOT_RELOAD_SCHEDULED:
            return False
        try:
            self_path = os.path.abspath(__file__)
            backup_path = os.path.join(SCRIPT_DIR, "iris_daemon.last_good.py")
            curr_mtime = os.path.getmtime(self_path) if os.path.exists(self_path) else DAEMON_INITIAL_MTIME
            needs_reload = os.path.exists(RELOAD_FLAG_PATH) or (curr_mtime != DAEMON_INITIAL_MTIME)
            with TASK_LOCK:
                is_busy_now = ACTIVE_TASK_INFO["running"]
            if not needs_reload or is_busy_now or TASK_QUEUE:
                return False

            if os.path.exists(RELOAD_FLAG_PATH):
                try:
                    os.remove(RELOAD_FLAG_PATH)
                except Exception:
                    pass

            # Validar sintaxis antes de reiniciar para garantizar cero caídas
            chk = subprocess.run(
                [sys.executable, "-m", "py_compile", self_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True
            )
            if chk.returncode != 0:
                err_detail = (chk.stdout or "").strip()[:300]
                if os.path.exists(backup_path):
                    try:
                        shutil.copyfile(backup_path, self_path)
                    except Exception:
                        pass
                DAEMON_INITIAL_MTIME = os.path.getmtime(self_path) if os.path.exists(self_path) else DAEMON_INITIAL_MTIME
                if chat_id:
                    safe_err = err_detail.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    send_msg(
                        chat_id,
                        f"🛡️ <b>Auto-Protección Anti-Caídas Activada:</b>\n"
                        f"Se detectó un error de sintaxis en <code>iris_daemon.py</code> antes de recargar:\n"
                        f"<pre>{safe_err}</pre>\n"
                        f"✅ <i>Restauré automáticamente la versión estable y mantuve el bot en línea sin desconectarse.</i>"
                    )
                return False

            _HOT_RELOAD_SCHEDULED = True
            DAEMON_INITIAL_MTIME = curr_mtime
            if _LOCK_FILE_HANDLE:
                try:
                    import fcntl
                    fcntl.flock(_LOCK_FILE_HANDLE.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
                try:
                    _LOCK_FILE_HANDLE.close()
                except Exception:
                    pass
                _LOCK_FILE_HANDLE = None

            reload_env = os.environ.copy()
            reload_env["IRIS_SUBPROCESS"] = "0"
            sh_script = os.path.join(SCRIPT_DIR, "iris.sh")
            subprocess.Popen(f"sleep 1 && '{sh_script}' restart", shell=True, env=reload_env, start_new_session=True)
            threading.Timer(1.2, lambda: os._exit(0)).start()
            return True
        except Exception:
            return False

def take_screenshot():
    shot_path = os.path.join(tempfile.gettempdir(), "iris_screenshot.png")
    if os.path.exists(shot_path):
        try:
            os.remove(shot_path)
        except Exception:
            pass
    if sys.platform == "darwin":
        subprocess.run(["screencapture", "-x", shot_path], check=False)
    elif sys.platform == "win32":
        try:
            safe_p = shot_path.replace(os.sep, "/")
            ps_cmd = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "$bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds; "
                "$bmp = New-Object System.Drawing.Bitmap($bounds.Width, $bounds.Height); "
                "$graphics = [System.Drawing.Graphics]::FromImage($bmp); "
                "$graphics.CopyFromScreen(0, 0, 0, 0, $bmp.Size); "
                f"$bmp.Save('{safe_p}', [System.Drawing.Imaging.ImageFormat]::Png); "
                "$graphics.Dispose(); $bmp.Dispose();"
            )
            subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd], check=False, timeout=10)
        except Exception as e:
            print(f"Error tomando screenshot en Windows: {e}", file=sys.stderr)
    else:
        try:
            subprocess.run(["import", "-window", "root", shot_path], check=False, timeout=10)
        except Exception:
            pass
    return shot_path

# ==============================================================================
# SISTEMA DE NAVEGACIÓN Y EXPLORADOR VISUAL DE CARPETAS Y PROYECTOS
# ==============================================================================
DIR_NAV_MAP = {}
IGNORED_EXPLORER_DIRS = {
    ".git", ".svn", ".hg", "node_modules", ".next", ".expo",
    ".vscode", ".idea", "dist", "build", ".gradle", "venv", ".venv",
    "__pycache__", "Pods", "DerivedData"
}

def register_dir_nav(path):
    real = os.path.realpath(os.path.expanduser(path))
    dir_id = f"d{abs(hash(real)) % 100000000}"
    DIR_NAV_MAP[dir_id] = real
    return dir_id, real

def get_dir_nav_path(dir_id):
    return DIR_NAV_MAP.get(dir_id)

def get_git_status_badge(path):
    git_dir = os.path.join(path, ".git")
    if not os.path.exists(git_dir):
        return None
    try:
        branch = subprocess.check_output(
            ["git", "branch", "--show-current"],
            cwd=path, text=True, stderr=subprocess.DEVNULL
        ).strip()
        status_out = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=path, text=True, stderr=subprocess.DEVNULL
        ).strip()
        dirty_cnt = len(status_out.splitlines()) if status_out else 0
        if dirty_cnt > 0:
            return f"🌿 <code>{branch or 'main'}</code> (<b>{dirty_cnt}</b> cambios pendientes)"
        else:
            return f"🌿 <code>{branch or 'main'}</code> (limpio ✓)"
    except Exception:
        return "🌿 Git detectado"

def scan_folder_contents(target_path):
    subdirs = []
    files_count = 0
    try:
        entries = sorted(os.listdir(target_path), key=lambda s: s.lower())
        for e in entries:
            if e.startswith(".") and e not in [".agents"]:
                continue
            if e.startswith("$"):
                continue
            full = os.path.join(target_path, e)
            if os.path.isdir(full):
                if e in IGNORED_EXPLORER_DIRS:
                    continue
                is_git = os.path.exists(os.path.join(full, ".git"))
                has_pkg = any(os.path.exists(os.path.join(full, f)) for f in [
                    "package.json", "build.gradle", "requirements.txt",
                    "pubspec.yaml", "Cargo.toml", "composer.json"
                ])
                subdirs.append({
                    "name": e,
                    "path": full,
                    "is_git": is_git,
                    "is_project": is_git or has_pkg
                })
            else:
                files_count += 1
    except Exception:
        pass

    subdirs.sort(key=lambda x: (not x["is_project"], x["name"].lower()))
    return subdirs, files_count

def render_folder_explorer(target_path, page=0):
    real_path = os.path.realpath(os.path.expanduser(target_path))
    if not os.path.isdir(real_path):
        real_path = os.path.expanduser("~/Documents")
        if not os.path.isdir(real_path):
            real_path = os.path.expanduser("~")

    curr_dir_id, real_path = register_dir_nav(real_path)
    subdirs, files_count = scan_folder_contents(real_path)
    git_info = get_git_status_badge(real_path)
    is_active = (real_path == os.path.realpath(CURRENT_CWD))

    home = os.path.expanduser("~")
    display_p = real_path.replace(home, "~")
    dir_name = os.path.basename(real_path) or display_p

    lines = [
        f"📂 <b>Explorador de Carpetas de tu Mac</b>\n",
        f"📍 <b>Ruta:</b> <code>{display_p}</code>"
    ]
    if git_info:
        lines.append(f"{git_info}")

    if is_active:
        lines.append(f"🟢 <b>Espacio activo actual en Antigravity</b>")
    else:
        lines.append(f"⚪ <i>Toca «✅ Abrir esta Carpeta» para fijarla como activa.</i>")

    lines.append(f"\n📁 <b>Subcarpetas:</b> {len(subdirs)}  •  📄 <b>Archivos:</b> {files_count}")

    buttons = []

    # 1. Botón principal: Abrir / Fijar como activa
    if not is_active:
        btn_label = f"✅ Abrir «{dir_name[:20]}» aquí"
        buttons.append([{"text": btn_label, "callback_data": f"nav_set:{curr_dir_id}"}])
    else:
        buttons.append([{"text": f"✨ «{dir_name[:20]}» está activo", "callback_data": f"nav_refresh:{curr_dir_id}"}])

    # 2. Paginación de subcarpetas (máximo 8 carpetas = 4 filas de 2 botones)
    ITEMS_PER_PAGE = 8
    total_pages = max(1, (len(subdirs) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start_idx = page * ITEMS_PER_PAGE
    end_idx = start_idx + ITEMS_PER_PAGE
    page_subdirs = subdirs[start_idx:end_idx]

    row = []
    for sd in page_subdirs:
        sub_id, _ = register_dir_nav(sd["path"])
        prefix = "📦 " if sd["is_project"] else "📁 "
        s_name = sd["name"]
        if len(s_name) > 17:
            s_name = s_name[:15] + ".."
        btn_txt = f"{prefix}{s_name}"
        row.append({"text": btn_txt, "callback_data": f"nav_enter:{sub_id}"})
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    # Controles de paginación si hay más de 1 página
    if total_pages > 1:
        pag_row = []
        if page > 0:
            pag_row.append({"text": "⬅️ Ant.", "callback_data": f"nav_page:{curr_dir_id}:{page-1}"})
        pag_row.append({"text": f"📄 {page+1}/{total_pages}", "callback_data": f"nav_refresh:{curr_dir_id}"})
        if page < total_pages - 1:
            pag_row.append({"text": "Sig. ➡️", "callback_data": f"nav_page:{curr_dir_id}:{page+1}"})
        buttons.append(pag_row)

    # 3. Controles de navegación jerárquica
    nav_row = []
    parent_p = os.path.dirname(real_path)
    if parent_p and parent_p != real_path and real_path != home and real_path != "/":
        parent_id, _ = register_dir_nav(parent_p)
        parent_name = os.path.basename(parent_p) or "Padre"
        nav_row.append({"text": f"⬆️ Subir ({parent_name[:12]})", "callback_data": f"nav_up:{parent_id}"})

    docs_p = os.path.realpath(os.path.expanduser("~/Documents"))
    if real_path != docs_p:
        docs_id, _ = register_dir_nav(docs_p)
        nav_row.append({"text": "🏠 Documentos", "callback_data": f"nav_enter:{docs_id}"})

    nav_row.append({"text": "🔄 Refrescar", "callback_data": f"nav_refresh:{curr_dir_id}"})
    buttons.append(nav_row)

    # 4. Acceso directo a Proyectos Git Rápidos
    buttons.append([{"text": "⚡ Proyectos Git Rápidos en tu Mac", "callback_data": "nav_quick_git"}])

    return "\n".join(lines), {"inline_keyboard": buttons}

def render_quick_git_view(current_path=None):
    docs_dir = os.path.expanduser("~/Documents")
    discovered = []

    try:
        for root, dirs, files in os.walk(docs_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in IGNORED_EXPLORER_DIRS]
            rel = os.path.relpath(root, docs_dir)
            depth = 0 if rel == "." else len(rel.split(os.sep))
            if os.path.exists(os.path.join(root, ".git")):
                p_name = os.path.basename(root)
                discovered.append({"name": p_name, "path": root})
                dirs.clear()
                continue
            if depth >= 2:
                dirs.clear()
    except Exception:
        pass

    curr_real = os.path.realpath(CURRENT_CWD)
    if os.path.exists(os.path.join(curr_real, ".git")) and not any(d["path"] == curr_real for d in discovered):
        discovered.insert(0, {"name": os.path.basename(curr_real), "path": curr_real})

    buttons = []
    row = []
    for item in discovered[:16]:
        d_id, _ = register_dir_nav(item["path"])
        is_active = (item["path"] == curr_real)
        lbl = f"{'👉 ' if is_active else '📦 '}{item['name'][:18]}"
        row.append({"text": lbl, "callback_data": f"nav_set:{d_id}"})
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    back_target = current_path or CURRENT_CWD
    back_id, _ = register_dir_nav(back_target)
    buttons.append([{"text": "🔙 Volver al Explorador de Carpetas", "callback_data": f"nav_enter:{back_id}"}])

    text = (
        "⚡ <b>Proyectos Git Detectados en tu Mac (<code>~/Documents</code>)</b>\n\n"
        "<i>Toca cualquier proyecto para abrirlo y establecerlo como tu espacio activo en Antigravity al instante:</i>"
    )
    return text, {"inline_keyboard": buttons}

def resolve_path_fuzzy(target, current_cwd):
    expanded = os.path.realpath(os.path.expanduser(target))
    if os.path.isdir(expanded):
        return expanded

    rel = os.path.realpath(os.path.join(current_cwd, target))
    if os.path.isdir(rel):
        return rel

    docs_dir = os.path.expanduser("~/Documents")
    docs_rel = os.path.realpath(os.path.join(docs_dir, target))
    if os.path.isdir(docs_rel):
        return docs_rel

    target_lower = target.lower()
    for root, dirs, _ in os.walk(docs_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in IGNORED_EXPLORER_DIRS]
        for d in dirs:
            if d.lower() == target_lower:
                return os.path.realpath(os.path.join(root, d))
        rel_d = len(os.path.relpath(root, docs_dir).split(os.sep))
        if rel_d >= 2:
            dirs.clear()

    for root, dirs, _ in os.walk(docs_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in IGNORED_EXPLORER_DIRS]
        for d in dirs:
            if target_lower in d.lower():
                return os.path.realpath(os.path.join(root, d))
        rel_d = len(os.path.relpath(root, docs_dir).split(os.sep))
        if rel_d >= 2:
            dirs.clear()

    return None

# ==============================================================================
# SISTEMA DE PLANIFICACIÓN PREVIA (/plan) Y CODE REVIEW (/review)
# ==============================================================================

def get_project_test_command(cwd):
    pkg_json = os.path.join(cwd, "package.json")
    if os.path.exists(pkg_json):
        try:
            with open(pkg_json, "r", encoding="utf-8") as f:
                data = json.load(f)
            scripts = data.get("scripts", {})
            if "test" in scripts:
                return "npm test"
            elif "lint" in scripts:
                return "npm run lint"
            elif "build" in scripts:
                return "npm run build"
        except Exception:
            pass

    if os.path.exists(os.path.join(cwd, "pubspec.yaml")):
        return "flutter test"
    if os.path.exists(os.path.join(cwd, "Cargo.toml")):
        return "cargo test"
    if os.path.exists(os.path.join(cwd, "pytest.ini")) or os.path.exists(os.path.join(cwd, "setup.py")):
        return "pytest"
    if os.path.exists(os.path.join(cwd, "gradlew")):
        return "./gradlew test"
    return None

def handle_plan_request(chat_id, task_text, model_override=None, force_voice=False, engine="agy"):
    if not task_text:
        guide = (
            "📐 <b>Modo de Planificación con Dante (/plan o /dante)</b>\n\n"
            "Dante es el asistente especialista de apoyo de Iris para <b>planeación, arquitectura e investigación</b> técnica.\n"
            "En este modo, Dante inspecciona tu proyecto <b>sin tocar código</b> y te entrega una estrategia paso a paso con voz de Jorge (México) para que la apruebes antes de que Iris ejecute.\n\n"
            "💡 <b>Ejemplos de uso:</b>\n"
            "• <code>/plan migrar base de datos a PostgreSQL</code>\n"
            "• <code>/plan cc diseñar sistema de caché en Redis</code> <i>(con Claude Code)</i>\n"
            "• <code>/dante investiga cómo conectar WebSockets en FastAPI</code>\n"
            "• <i>«Iris, pregúntale a Dante cómo estructurar la autenticación»</i>\n\n"
            "👉 <i>Escribe <code>/plan &lt;tu idea&gt;</code> o <code>/dante &lt;consulta&gt;</code> para comenzar.</i>"
        )
        send_msg(chat_id, guide)
        return

    safe_task = task_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    task_prev = f"«{safe_task[:140]}...»" if len(safe_task) > 140 else f"«{safe_task}»"
    ws_name, detected_plan_dir = detect_dynamic_project(task_text)
    plan_loc_line = f"📍 <code>{ws_name}</code>\n" if ws_name else ""

    engine_tag = " (con Claude Code)" if engine == "claude_code" else ""
    init_msg = (
        f"📐 <b>Iris:</b> <i>«Oye Dante, échanos una mano con los planos y la arquitectura{engine_tag} para esto:»</i>\n"
        f"┃ <i>{task_prev}</i>\n\n"
        f"{plan_loc_line}"
        "🧠 <b>Dante en el laboratorio:</b> <i>Analizando los sistemas en tu Mac sin tocar el código...</i>"
    )
    status_resp = send_msg(chat_id, init_msg)
    msg_id = status_resp.get("result", {}).get("message_id") if (status_resp and status_resp.get("ok")) else None

    def _generate_plan_worker():
        st = load_state()
        dante_intro_done = st.get("dante_introduced", False)
        plan_ctx_label = f"en el proyecto ({detected_plan_dir})" if detected_plan_dir else "en el ecosistema de proyectos del usuario"

        prompt = (
            f"Actúa como Dante (hombre, identidad masculina; refiérete a ti mismo siempre en género masculino: «estoy listo», «preparado», «encargado»), "
            f"especialista senior en arquitectura de software y compañero de laboratorio del usuario, trabajando en equipo con Iris (mujer, identidad femenina, comandante encargada de la ejecución táctica).\n"
            f"Tu personalidad es relajada, serena, brillante, leal confidente de laboratorio del usuario y tienes un toque de ingenio fino, elegante y sutilmente irónico cuando adviertes de riesgos técnicos o comentas una idea audaz, sin perder jamás la precisión técnica ni sonar acartonado.\n"
            f"Tu misión {plan_ctx_label} es analizar, investigar y diseñar la estrategia técnica para la siguiente solicitud del usuario:\n"
            f"\"{task_text}\"\n\n"
            f"INSTRUCCIONES CRÍTICAS:\n"
            f"1. NO modifiques ningún archivo. NO crees ni borres archivos. NO ejecutes comandos destructivos.\n"
            f"2. Inspecciona el código existente con herramientas de lectura para comprender la estructura real.\n"
            f"3. Redacta con ortografía española impecable y estricta acentuación gráfica (todas las tildes correspondientes). Dirígete al usuario con trato cordial y directo.\n"
            f"4. Mantén tu identidad masculina como Dante (relajado, sereno e ingenioso) y reconoce a Iris en femenino como quien tomará los controles para ejecutar el plan.\n"
            f"5. Abre con un brevísimo saludo o comentario ingenioso y elegante propio de Dante (1 línea) y genera el Plan de Ejecución en español con este formato:\n\n"
            f"🎯 **Objetivo Estratégico:** (1-2 frases claras)\n"
            f"📁 **Archivos a inspeccionar / modificar:** (rutas relativas clave)\n"
            f"🪜 **Pasos de implementación recomendados:**\n"
            f"1. paso 1...\n"
            f"2. paso 2...\n"
            f"⚠️ **Riesgos y validaciones técnicas:** (seguridad, rendimiento, dependencias, pruebas necesarias)\n"
            f"⏱️ **Complejidad estimada:** (Baja / Media / Alta)"
        )
        if engine == "claude_code":
            plan_raw = run_claude_code(prompt, chat_id=chat_id, continue_session=True, model="sonnet")
            engine_title = " (con Claude Code)"
            engine_badge = "\n🧠 <b>Motor de análisis:</b> <code>Claude Code · Sonnet 3.7 (Anthropic)</code>"
            actual_model = "claude-code-sonnet"
        else:
            target_model = model_override or DANTE_PRIMARY_MODEL
            plan_raw = run_antigravity(prompt, chat_id=chat_id, continue_session=True, model_override=target_model)
            
            # Failover automático: si Argon 4 High reporta error o indisponibilidad, conmutar a Claude Opus 5.5 High
            if (not plan_raw or plan_raw.startswith("⏱️") or plan_raw.startswith("No encontré") or 
                "error" in plan_raw.lower() or "quota" in plan_raw.lower() or "rate limit" in plan_raw.lower()) and not model_override:
                print(f"[DANTE] Fallover activado: {DANTE_PRIMARY_MODEL} no respondió óptimamente. Conmutando a {DANTE_FALLBACK_MODEL}...")
                fallback_res = run_antigravity(prompt, chat_id=chat_id, continue_session=True, model_override=DANTE_FALLBACK_MODEL)
                if fallback_res and not fallback_res.startswith("⏱️"):
                    plan_raw = fallback_res
                    target_model = DANTE_FALLBACK_MODEL

            found_m = find_model_by_query(target_model)
            m_name = found_m["name"] if found_m else target_model
            m_short = found_m["short"] if found_m else target_model
            engine_title = f" · {m_short}"
            engine_badge = f"\n🧠 <b>Motor de análisis:</b> <code>{m_name} (Antigravity)</code>"
            actual_model = target_model

        plan_id = f"p{int(time.time()*1000) % 10000000}"
        save_plan(plan_id, {
            "id": plan_id,
            "task": task_text,
            "plan_text": plan_raw,
            "cwd": detected_plan_dir or CURRENT_CWD,
            "model": model_override,
            "engine": engine,
            "timestamp": time.time(),
            "status": "pending"
        })

        formatted_plan = format_telegram_html(plan_raw)
        intro_badge = "<i>(Primera presentación de Dante)</i>\n" if not dante_intro_done else ""
        plan_loc_header = f"📍 <code>{ws_name}</code>{engine_badge}\n" if ws_name else f"{engine_badge.lstrip(chr(10))}\n"
        plan_card = (
            f"📐 <b>Dante · Arquitectura &amp; Planificación #{plan_id}{engine_title}</b>\n"
            f"👨‍💻 <i>Especialista en arquitectura bajo el mando de Iris</i>\n"
            f"{plan_loc_header}"
            f"{intro_badge}\n"
            f"{formatted_plan}\n\n"
            f"─────────────────────────────\n"
            f"👇 <i>Revisa el plan de Dante. Puedes aprobarlo directamente con Iris / Claude Code o pedirle a <b>Cerberus</b> que lo audite y apruebe en ciberseguridad antes de ejecutar:</i>"
        )
        buttons = [
            [{"text": "🛡️ Pasar Plan por Cerberus (Ciberseguridad)", "callback_data": f"plan_cerberus:{plan_id}"}],
            [{"text": "✅ Aprobar con Iris (Antigravity)", "callback_data": f"plan_exec:{plan_id}"}],
            [{"text": "🟠 Aprobar con Claude Code (Extensión)", "callback_data": f"plan_cc:{plan_id}"}],
            [{"text": "⚡ Ejecutar con Argon 4 High", "callback_data": f"plan_pro:{plan_id}"},
             {"text": "❌ Descartar Plan", "callback_data": f"plan_cancel:{plan_id}"}]
        ]
        markup = {"inline_keyboard": buttons}
        if msg_id:
            try:
                edit_msg_text(chat_id, msg_id, plan_card, reply_markup=markup)
            except Exception:
                send_msg(chat_id, plan_card, reply_markup=markup)
        else:
            send_msg(chat_id, plan_card, reply_markup=markup)

        # Dante habla con voz masculina Jorge (México) con tono sereno, relajado e ingenioso
        if VOICE_MODE or force_voice:
            task_clean = task_text[:60]
            if engine == "claude_code":
                if not dante_intro_done:
                    speech_text = (
                        f"Qué tal, soy Dante. Ya revisé los sistemas con Claude Code y tengo listos los planos para {task_clean}. "
                        f"Échales un vistazo en Telegram y dime si le damos luz verde con Iris o con Claude Code."
                    )
                    save_state({"dante_introduced": True})
                else:
                    speech_text = (
                        f"Ya quedaron listos los planos en el laboratorio con Claude Code para {task_clean}. "
                        "Todo en orden en Telegram para cuando quieras que entremos en acción."
                    )
            else:
                if not dante_intro_done:
                    speech_text = (
                        f"Qué tal, soy Dante. Aquí en el laboratorio ya analicé todo y dejé trazada la arquitectura para {task_clean}, sin hacer explotar ningún reactor todavía. "
                        f"Revísala en Telegram y cuando me des luz verde, Iris toma los controles para ejecutarla."
                    )
                    save_state({"dante_introduced": True})
                else:
                    speech_text = (
                        f"Ya tengo listos los planos y la arquitectura para {task_clean}. "
                        "Revísalos con calma en Telegram y cuando apruebes, Iris se encarga de la maniobra en el código."
                    )

            ogg = synthesize_speech_to_voice(speech_text, voice=DANTE_VOICE_NAME)
            if ogg and os.path.exists(ogg):
                send_voice(chat_id, ogg)
                try:
                    os.remove(ogg)
                except Exception:
                    pass

    t = threading.Thread(target=_generate_plan_worker, daemon=True)
    t.start()

def handle_cerberus_request(chat_id, task_text="", plan_id=None, model_override=None, force_voice=False, engine="agy", audit_now=False):
    """
    Cerberus: Especialista en Ciberseguridad Básica, Auditoría Defensiva y Aprobación de Planes/Ejecuciones.
    Opera siempre bajo el mando de Iris y únicamente cuando el usuario lo solicita.
    """
    ws_curr = os.path.basename(CURRENT_CWD) or CURRENT_CWD
    if not task_text and not plan_id and not audit_now:
        guide = (
            "🛡️ <b>Modo Ciberseguridad y Aprobación con Cerberus (/cerberus o /sec)</b>\n"
            "<i>Iris está al mando y convoca a Cerberus únicamente cuando tú lo solicitas.</i>\n\n"
            "<b>Cerberus</b> es el guardián de <b>ciberseguridad básica, auditoría de vulnerabilidades y blindaje defensivo</b>.\n"
            "En modo <b>solo lectura</b>, Cerberus:\n"
            "• Revisa secretos expuestos (<code>.env</code>, llaves API, tokens), OWASP Top 10 (inyecciones, XSS, CSRF, autenticación, CORS), permisos y dependencias.\n"
            "• <b>Aprueba o audita planes de Dante y ejecuciones de Iris</b> cuando tú se lo pides, entregando recomendaciones de ciberseguridad antes de aplicar cambios.\n\n"
            "💡 <b>Ejemplos de uso (siempre a petición tuya):</b>\n"
            "• <code>/cerberus audita la seguridad de este proyecto</code>\n"
            "• <code>/cerberus revisa y aprueba el último plan de Dante</code>\n"
            "• <code>/sec verifica si hay credenciales expuestas o fallos en el login</code>\n"
            "• 🎙️ <i>«Iris, pídele a Cerberus que revise la ciberseguridad y apruebe los cambios»</i>"
        )
        btns = [
            [{"text": f"🛡️ Auditar Proyecto Activo ({ws_curr[:18]})", "callback_data": "cmd_cerberus_audit"}],
            [{"text": "📐 Auditar y Aprobar Último Plan de Dante", "callback_data": "cmd_cerberus_last_plan"}]
        ]
        send_msg(chat_id, guide, reply_markup={"inline_keyboard": btns})
        return

    # Si no se pasó plan_id explícito pero el texto pide aprobar/revisar el plan de Dante, buscar el último plan pendiente
    if not plan_id and task_text:
        t_low = task_text.lower()
        if any(k in t_low for k in ["plan de dante", "ultimo plan", "último plan", "aprueba el plan", "aprobar el plan", "revisa el plan", "revisar el plan"]):
            all_plans = load_plans()
            dante_plans = [p for p in all_plans.values() if not p.get("is_cerberus") and p.get("status") != "cancelled"]
            if dante_plans:
                dante_plans.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
                plan_id = dante_plans[0].get("id")

    target_plan = get_plan(plan_id) if plan_id else None
    if target_plan:
        plan_task = target_plan.get("task", "Plan de arquitectura")
        plan_body = target_plan.get("plan_text", "")
        target_cwd = target_plan.get("cwd", CURRENT_CWD)
        ws_name = os.path.basename(target_cwd) or target_cwd
        safe_task = html.escape(plan_task[:130])
        init_msg = (
            f"👩‍💻 <b>Iris (Al mando):</b> <i>«Cerberus, el usuario solicita tu auditoría de ciberseguridad y visto bueno sobre el plan #{plan_id} de Dante:»</i>\n"
            f"┃ <i>«{safe_task}»</i>\n\n"
            f"📍 <code>{ws_name}</code>\n"
            f"🛡️ <b>Cerberus en posición:</b> <i>Inspeccionando vectores de riesgo, credenciales, autenticación y superficie de ataque en solo lectura...</i>"
        )
    else:
        effective_query = task_text or f"Realizar auditoría básica integral de ciberseguridad, exposición de secretos, vulnerabilidades comunes (OWASP) y cambios recientes en el proyecto {ws_curr}"
        ws_name, detected_sec_dir = detect_dynamic_project(effective_query)
        target_cwd = detected_sec_dir or CURRENT_CWD
        ws_name = ws_name or os.path.basename(target_cwd) or target_cwd
        safe_q = html.escape(effective_query[:130])
        init_msg = (
            f"👩‍💻 <b>Iris (Al mando):</b> <i>«Cerberus, entra al perímetro. El usuario pide tu dictamen y recomendaciones de ciberseguridad para esto:»</i>\n"
            f"┃ <i>«{safe_q}»</i>\n\n"
            f"📍 <code>{ws_name}</code>\n"
            f"🛡️ <b>Cerberus en posición:</b> <i>Auditando código, secretos, permisos y vectores de seguridad en modo solo lectura...</i>"
        )

    status_resp = send_msg(chat_id, init_msg)
    msg_id = status_resp.get("result", {}).get("message_id") if (status_resp and status_resp.get("ok")) else None

    def _cerberus_worker():
        global CURRENT_CWD
        if target_cwd and os.path.isdir(target_cwd):
            CURRENT_CWD = target_cwd
        st = load_state()
        cerb_intro_done = st.get("cerberus_introduced", False)

        if target_plan:
            prompt = (
                f"Actúa como Cerberus (hombre, identidad masculina; refiérete a ti mismo siempre en masculino: «estoy firme», «listo», «el guardián de seguridad»), "
                f"especialista en ciberseguridad y defensa táctica del equipo del usuario, operando bajo el mando de Iris (mujer, comandante táctica) y evaluando el trabajo de arquitectura de Dante (hombre, arquitecto de software).\n"
                f"Tu personalidad es imponente, grave, serena, directa y protectora: vas directo a los riesgos reales y a cómo blindar el sistema sin rodeos.\n\n"
                f"El usuario solicitó explícitamente que audites, des recomendaciones de ciberseguridad y apruebes (o señales riesgos en) el siguiente Plan de Dante en el proyecto ({target_cwd}):\n"
                f"Objetivo del plan: \"{target_plan.get('task', '')}\"\n"
                f"Plan propuesto por Dante:\n{plan_body}\n\n"
                f"INSTRUCCIONES CRÍTICAS:\n"
                f"1. NO modifiques, crees ni borres ningún archivo. Opera 100% en modo solo lectura.\n"
                f"2. Inspecciona los archivos relevantes del proyecto si es necesario para verificar cómo impacta el plan en la seguridad.\n"
                f"3. Redacta en español con ortografía y acentuación impecables (100% de las tildes). Trata al usuario con naturalidad y respeto.\n"
                f"4. Reconoce que Iris está al mando y ejecutará el plan una vez que el usuario confirme tu dictamen.\n"
                f"5. Abre con un saludo breve, firme e imponente propio de Cerberus (1 línea) y estructura tu dictamen así:\n\n"
                f"🛡️ **Veredicto de Ciberseguridad:** [APROBADO ✅ / APROBADO CON RECOMENDACIONES ⚠️ / RIESGO CRÍTICO 🚨]\n"
                f"🔍 **Auditoría del Plan (Lo Básico y Esencial):** (exposición de secretos/.env, autenticación/autorización, inyección/XSS/CORS, validación de datos, permisos)\n"
                f"🧱 **Recomendaciones de Blindaje para Iris:** (lista numerada, clara y accionable de medidas de seguridad que Iris debe incluir al ejecutar)\n"
                f"⚖️ **Nivel de Riesgo Residual:** (Bajo / Medio / Alto)"
            )
        else:
            q_text = task_text or "Realiza una auditoría básica de ciberseguridad sobre el proyecto y los cambios recientes: revisa secretos o credenciales expuestas (.env, API keys), vulnerabilidades básicas (inyecciones, XSS, CSRF, autenticación, CORS), permisos y dependencias, y emite tus recomendaciones y veredicto de aprobación."
            prompt = (
                f"Actúa como Cerberus (hombre, identidad masculina; refiérete a ti mismo siempre en masculino: «estoy firme», «listo», «el guardián de seguridad»), "
                f"especialista en ciberseguridad y defensa táctica del equipo del usuario, operando bajo el mando de Iris (mujer, comandante táctica).\n"
                f"Tu personalidad es imponente, grave, serena, directa y protectora.\n\n"
                f"El usuario solicitó explícitamente tu intervención de ciberseguridad en el proyecto ({target_cwd}) para:\n"
                f"\"{q_text}\"\n\n"
                f"INSTRUCCIONES CRÍTICAS:\n"
                f"1. NO modifiques, crees ni borres ningún archivo. Opera 100% en modo solo lectura.\n"
                f"2. Inspecciona el código, archivos de configuración (.gitignore, posibles secretos, endpoints, autenticación, dependencias o git diff reciente) usando herramientas de lectura.\n"
                f"3. Redacta en español con ortografía y acentuación impecables (100% de las tildes). Trata al usuario con naturalidad y respeto.\n"
                f"4. Reconoce que Iris está al mando y aplicará las correcciones de blindaje si el usuario lo autoriza.\n"
                f"5. Abre con un saludo breve, firme e imponente propio de Cerberus (1 línea) y entrega tu reporte con este formato:\n\n"
                f"🛡️ **Veredicto de Cerberus:** [APROBADO ✅ / APROBADO CON OBSERVACIONES ⚠️ / REQUIERE BLINDAJE URGENTE 🚨]\n"
                f"🔐 **1. Secretos, Credenciales y Entorno (.env / Git):** (hallazgos)\n"
                f"🧱 **2. Seguridad de Código, Endpoints y Permisos (OWASP Básico):** (hallazgos)\n"
                f"📦 **3. Dependencias y Superficie Expuesta:** (hallazgos)\n"
                f"🛠️ **Recomendaciones de Ciberseguridad (Para ejecutar con Iris si apruebas):**\n"
                f"1. recomendación 1...\n"
                f"2. recomendación 2..."
            )

        if engine == "claude_code":
            cerb_raw = run_claude_code(prompt, chat_id=chat_id, continue_session=True, model="sonnet")
            engine_title = " (con Claude Code)"
            engine_badge = "\n🧠 <b>Motor de defensa:</b> <code>Claude Code · Sonnet (Anthropic)</code>"
        else:
            target_model = model_override or CERBERUS_PRIMARY_MODEL
            cerb_raw = run_antigravity(prompt, chat_id=chat_id, continue_session=True, model_override=target_model)
            if (not cerb_raw or cerb_raw.startswith("⏱️") or cerb_raw.startswith("No encontré") or
                "error" in cerb_raw.lower() or "quota" in cerb_raw.lower() or "rate limit" in cerb_raw.lower()) and not model_override:
                fallback_res = run_antigravity(prompt, chat_id=chat_id, continue_session=True, model_override=CERBERUS_FALLBACK_MODEL)
                if fallback_res and not fallback_res.startswith("⏱️"):
                    cerb_raw = fallback_res
                    target_model = CERBERUS_FALLBACK_MODEL
            found_m = find_model_by_query(target_model)
            m_name = found_m["name"] if found_m else target_model
            m_short = found_m["short"] if found_m else target_model
            engine_title = f" · {m_short}"
            engine_badge = f"\n🧠 <b>Motor de defensa:</b> <code>{m_name} (Antigravity)</code>"

        formatted_cerb = format_telegram_html(cerb_raw)
        intro_badge = ""

        if target_plan:
            target_plan["cerberus_review"] = cerb_raw
            target_plan["cerberus_approved_at"] = time.time()
            save_plan(plan_id, target_plan)
            sec_card = (
                f"🛡️ <b>Cerberus · Dictamen de Ciberseguridad sobre Plan #{plan_id}{engine_title}</b>\n"
                f"🐕‍🦺 <i>Guardián de Ciberseguridad bajo el mando de Iris</i>\n"
                f"📍 <code>{ws_name}</code>{engine_badge}\n"
                f"{intro_badge}\n"
                f"{formatted_cerb}\n\n"
                f"─────────────────────────────\n"
                f"👇 <i>Dictamen listo\1 Si apruebas, <b>Iris</b> ejecutará el plan de Dante aplicando estas recomendaciones de ciberseguridad de Cerberus:</i>"
            )
            buttons = [
                [{"text": "✅ Aprobar Plan + Blindaje con Iris", "callback_data": f"plan_exec:{plan_id}"}],
                [{"text": "🟠 Aprobar Plan + Blindaje con Claude Code", "callback_data": f"plan_cc:{plan_id}"}],
                [{"text": "⚡ Ejecutar con Argon 4 High", "callback_data": f"plan_pro:{plan_id}"},
                 {"text": "❌ Descartar", "callback_data": f"plan_cancel:{plan_id}"}]
            ]
        else:
            sec_id = f"s{int(time.time()*1000) % 10000000}"
            save_plan(sec_id, {
                "id": sec_id,
                "task": task_text or f"Blindaje de ciberseguridad en {ws_name}",
                "plan_text": cerb_raw,
                "cerberus_review": cerb_raw,
                "is_cerberus": True,
                "cwd": target_cwd,
                "model": model_override,
                "engine": engine,
                "timestamp": time.time(),
                "status": "pending"
            })
            sec_card = (
                f"🛡️ <b>Cerberus · Auditoría &amp; Recomendaciones de Ciberseguridad #{sec_id}{engine_title}</b>\n"
                f"🐕‍🦺 <i>Guardián de Ciberseguridad bajo el mando de Iris</i>\n"
                f"📍 <code>{ws_name}</code>{engine_badge}\n"
                f"{intro_badge}\n"
                f"{formatted_cerb}\n\n"
                f"─────────────────────────────\n"
                f"👇 <i>Revisa las recomendaciones de Cerberus\1 Si deseas aplicar el blindaje en el código, toca abajo para que <b>Iris</b> tome el mando y lo ejecute:</i>"
            )
            buttons = [
                [{"text": "🛡️ Aplicar Blindaje con Iris (Antigravity)", "callback_data": f"plan_exec:{sec_id}"}],
                [{"text": "🟠 Aplicar Blindaje con Claude Code", "callback_data": f"plan_cc:{sec_id}"}],
                [{"text": "✅ Solo Conservar Dictamen (No Tocar Código)", "callback_data": f"plan_cancel:{sec_id}"}]
            ]

        markup = {"inline_keyboard": buttons}
        if msg_id:
            try:
                edit_msg_text(chat_id, msg_id, sec_card, reply_markup=markup)
            except Exception:
                send_msg(chat_id, sec_card, reply_markup=markup)
        else:
            send_msg(chat_id, sec_card, reply_markup=markup)

        if VOICE_MODE or force_voice:
            if not cerb_intro_done:
                speech_text = (
                    f"Soy Cerberus, encargado de la ciberseguridad bajo el mando de Iris. "
                    f"Ya inspeccioné el perímetro en {ws_name} y dejé mi veredicto y recomendaciones de blindaje en Telegram. "
                    f"Cuando des la orden, Iris ejecutará las medidas de seguridad."
                )
                save_state({"cerberus_introduced": True})
            elif target_plan:
                speech_text = (
                    f"Habla Cerberus. Revisé el plan de Dante en {ws_name} y dejé mis recomendaciones de ciberseguridad y el dictamen en Telegram. "
                    f"Si apruebas, Iris toma el mando y ejecuta el plan blindado."
                )
            else:
                speech_text = (
                    f"Habla Cerberus. Completé la auditoría de ciberseguridad en {ws_name}. "
                    f"Tienes el veredicto y las recomendaciones en Telegram por si quieres que Iris aplique el blindaje."
                )
            ogg = synthesize_speech_to_voice(
                speech_text,
                voice=CERBERUS_VOICE_NAME,
                pitch=CERBERUS_VOICE_PITCH,
                rate=CERBERUS_VOICE_RATE
            )
            if ogg and os.path.exists(ogg):
                send_voice(chat_id, ogg)
                try:
                    os.remove(ogg)
                except Exception:
                    pass

    t = threading.Thread(target=_cerberus_worker, daemon=True)
    t.start()

def handle_review_request(chat_id, force_voice=False):
    ws_name = os.path.basename(CURRENT_CWD) or CURRENT_CWD
    is_git = os.path.exists(os.path.join(CURRENT_CWD, ".git"))
    if not is_git:
        send_msg(chat_id, f"ℹ️ <code>{ws_name}</code> no es un repositorio Git.\n💡 Escribe <code>/ws</code> o <code>/git</code> para abrir un proyecto con control de versiones.")
        return

    try:
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=CURRENT_CWD, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        branch = "main"

    try:
        status_out = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=CURRENT_CWD, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        status_out = ""

    dirty_cnt = len(status_out.splitlines()) if status_out else 0

    if dirty_cnt == 0:
        # Repositorio limpio
        try:
            last_commit = subprocess.check_output(
                ["git", "log", "-1", "--pretty=format:%h - %an: %s (%cr)"],
                cwd=CURRENT_CWD, text=True, stderr=subprocess.DEVNULL
            ).strip()
        except Exception:
            last_commit = "No disponible"

        msg = (
            f"✅ <b>Repositorio Limpio y al Día</b>\n\n"
            f"📍 <b>Proyecto:</b> <code>{ws_name}</code>\n"
            f"🌿 <b>Rama:</b> <code>{branch}</code>\n"
            f"✨ No hay modificaciones pendientes de confirmar.\n\n"
            f"📜 <b>Último commit:</b>\n"
            f"<code>{last_commit}</code>"
        )
        buttons = [
            [{"text": "📄 Ver Último Diff", "callback_data": "review_last_diff"}],
            [{"text": "🔄 Git Status", "callback_data": "review_status"}, {"text": "⚡ Proyectos Git", "callback_data": "nav_quick_git"}]
        ]
        send_msg(chat_id, msg, reply_markup={"inline_keyboard": buttons})
        if VOICE_MODE or force_voice:
            ogg = synthesize_speech_to_voice(f"El repositorio de {ws_name} está completamente limpio. No hay cambios pendientes.")
            if ogg and os.path.exists(ogg):
                send_voice(chat_id, ogg)
                try:
                    os.remove(ogg)
                except Exception:
                    pass
        return

    # Si hay cambios pendientes -> Realizar Code Review
    try:
        stat_out = subprocess.check_output(
            ["git", "diff", "--stat"],
            cwd=CURRENT_CWD, text=True, stderr=subprocess.DEVNULL
        ).strip()
        diff_out = subprocess.check_output(
            ["git", "diff"],
            cwd=CURRENT_CWD, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except Exception:
        stat_out = ""
        diff_out = ""

    diff_sample = diff_out[:3200]
    if len(diff_out) > 3200:
        diff_sample += "\n... [diff truncado para revisión de modelo]"

    init_msg = (
        f"🕵️ <b>Analizando cambios para Code Review...</b>\n"
        f"🌿 Rama: <code>{branch}</code> • <b>{dirty_cnt}</b> archivo(s) modificado(s)\n"
        f"📍 <code>{ws_name}</code>\n\n"
        f"🧠 <i>Antigravity evaluando calidad, bugs y riesgos en tu Mac...</i>"
    )
    status_resp = send_msg(chat_id, init_msg)
    msg_id = status_resp.get("result", {}).get("message_id") if (status_resp and status_resp.get("ok")) else None

    def _generate_review_worker():
        prompt = (
            f"Actúa como Principal Code Reviewer de Google Antigravity en el proyecto {ws_name}.\n"
            f"Analiza los siguientes cambios no confirmados (git diff) en la rama {branch}:\n\n"
            f"ESTADÍSTICAS:\n{stat_out}\n\n"
            f"DIFF:\n{diff_sample}\n\n"
            f"INSTRUCCIONES:\n"
            f"1. No modifiques ningún archivo.\n"
            f"2. Entrega una revisión de código ejecutiva, clara y constructiva en español con este formato exacto:\n\n"
            f"🔍 **Resumen del cambio:** (1 párrafo conciso)\n"
            f"✅ **Puntos fuertes:** (buenas prácticas aplicadas, mejoras)\n"
            f"⚠️ **Observaciones o posibles riesgos:** (validación, performance, edge cases o 'Ninguno detectado')\n"
            f"🏁 **Veredicto:** [Listo para commit ✅ / Requiere ajustes ⚠️]"
        )
        review_raw = run_antigravity(prompt, chat_id=chat_id, continue_session=True)
        formatted_rev = format_telegram_html(review_raw)

        card = (
            f"🕵️ <b>Code Review Antigravity</b>\n"
            f"📍 <code>{ws_name}</code> • 🌿 <code>{branch}</code>\n"
            f"📊 <code>{stat_out}</code>\n\n"
            f"{formatted_rev}\n\n"
            f"─────────────────────────────\n"
            f"👇 <i>Acciones rápidas sobre estos cambios:</i>"
        )
        buttons = [
            [{"text": "📄 Ver Git Diff", "callback_data": "review_diff"}],
            [{"text": "🧪 Correr Tests", "callback_data": "review_test"},
             {"text": "💾 Crear Git Commit", "callback_data": "review_commit"}]
        ]
        markup = {"inline_keyboard": buttons}
        if msg_id:
            try:
                edit_msg_text(chat_id, msg_id, card, reply_markup=markup)
            except Exception:
                send_msg(chat_id, card, reply_markup=markup)
        else:
            send_msg(chat_id, card, reply_markup=markup)

        if VOICE_MODE or force_voice:
            ogg = synthesize_speech_to_voice(f"Completé el análisis de cambios en {ws_name}. Tienes {dirty_cnt} archivos modificados. Te envié el veredicto y opciones a Telegram.")
            if ogg and os.path.exists(ogg):
                send_voice(chat_id, ogg)
                try:
                    os.remove(ogg)
                except Exception:
                    pass

    t = threading.Thread(target=_generate_review_worker, daemon=True)
    t.start()

def handle_message(msg):
    global CURRENT_CWD, IS_PAUSED, VOICE_MODE, CURRENT_PROCESS, CAFFEINATE_PROC, SESSION_RESET_FLAG
    chat = msg.get("chat", {})
    chat_id = chat.get("id")
    user_id = msg.get("from", {}).get("id")
    text = msg.get("text", "").strip()
    caption = msg.get("caption", "").strip()

    if user_id not in ALLOWED_USERS:
        return

    # Mantener IS_PAUSED siempre sincronizado con state.json
    IS_PAUSED = load_state().get("is_paused", False)

    # Captura de botones táctiles de la botonera inferior fija
    t_clean = text.lower().strip()
    if t_clean in ["⚡ estado mac", "📊 estado mac", "estado mac", "estado", "/status", "⚡ estado pc", "📊 estado pc", "estado pc", "⚡ estado", "estado sistema"]:
        send_msg(chat_id, get_system_status(), reply_markup=get_persistent_reply_keyboard())
        return

    if t_clean in ["💎 consumo / cuota", "💎 consumo", "🧠 consumo / cuota", "consumo / cuota", "consumo", "cuota"]:
        text = "/consumo"

    if t_clean in ["🚀 proyectos (/ws)", "🚀 proyectos", "📂 proyectos (/ws)", "proyectos (/ws)", "proyectos", "workspaces"]:
        text = "/ws"

    if t_clean in ["🏠 documentos", "documentos", "mis documentos", "/docs"]:
        text = "/docs"

    if t_clean in ["📐 plan (dante)", "plan (dante)", "📐 plan", "planear", "dante"]:
        text = "/plan"

    if t_clean in ["🛡️ cerberus", "🛡️ cerberus (seguridad)", "cerberus (seguridad)", "cerberus", "ciberseguridad"]:
        text = "/cerberus"

    if t_clean in ["🕵️ code review", "code review", "🕵️ review", "review", "diff"]:
        text = "/review"

    if t_clean in ["🧠 modelos ia", "🧠 modelos", "modelos ia", "modelos", "/models", "/model"]:
        text = "/model"

    if t_clean in ["👁️ captura pantalla", "👁️ captura", "📸 captura pantalla", "captura pantalla", "pantalla", "screenshot"]:
        text = "/screenshot"

    if t_clean in ["⚡ comandos directos", "comandos directos", "⚡ comandos", "comandos", "panel", "directos", "/comandos", "/panel", "/teclado", "teclado"]:
        st = load_state()
        curr_show = st.get("show_keyboard", False)
        new_show = not curr_show
        save_state({"show_keyboard": new_show})
        if new_show:
            send_msg(chat_id, "⌨️ <b>Panel de botones activado.</b>", reply_markup=get_persistent_reply_keyboard())
        else:
            send_msg(chat_id, "🧹 <b>Panel de botones ocultado.</b>", reply_markup={"remove_keyboard": True})
        return

    if t_clean in ["🔌 puertos", "puertos", "/ports", "/puertos"]:
        text = "/ports"

    if t_clean in ["📜 historial", "historial", "/history", "/historial"]:
        text = "/history"

    if t_clean in ["🎧 voz: on", "🔇 voz: off", "🎙️ voz: on", "voz: on", "voz: off"]:
        text = "/v"

    # Si el botón dice "Reposo: ON", al tocarlo lo pasa a OFF (/wake).
    # Si el botón dice "Reposo: OFF" (o "Iris: Activa"), al tocarlo lo pasa a ON (/pause).
    if t_clean in ["💤 reposo: on", "☕ reposo: on", "reposo: on"]:
        text = "/wake"
    elif t_clean in ["☀️ reposo: off", "🌙 reposo: off", "🔔 reposo: off", "☀️ iris: activa", "iris: activa", "reposo: off"]:
        text = "/pause"
    elif t_clean in ["🌙 reposo", "💤 reposo", "reposo", "/reposo"]:
        text = "/wake" if IS_PAUSED else "/pause"

    # Cancelar tarea activa o vaciar la cola
    if text.lower() in ["/cancel", "/stop", "cancelar", "cancela", "detén eso", "deten eso", "para"]:
        with TASK_LOCK:
            proc = CURRENT_PROCESS
            is_running = ACTIVE_TASK_INFO["running"]
            ACTIVE_TASK_INFO["cancelled"] = True

        with QUEUE_LOCK:
            cleared_q = len(TASK_QUEUE)
            TASK_QUEUE.clear()

        if proc:
            try:
                kill_process_tree(proc)
                extra_q = f" y cancelé {cleared_q} tarea(s) en espera" if cleared_q > 0 else ""
                send_msg(chat_id, f"🛑 Detuve la tarea en curso{extra_q}. Tu Mac está libre.")
            except Exception as e:
                send_msg(chat_id, f"Hubo un detalle al detener el proceso: {e}")
        elif is_running:
            send_msg(chat_id, "🛑 Tarea activa y cola marcadas como canceladas.")
        elif cleared_q > 0:
            send_msg(chat_id, f"🗑️ Se vació la cola ({cleared_q} tarea(s) canceladas).")
        else:
            send_msg(chat_id, "No había ninguna tarea en ejecución ni en cola en este momento.")
        return

    # Ver tareas en cola (/queue o /cola)
    if text.lower() in ["/queue", "/cola"]:
        with QUEUE_LOCK:
            with TASK_LOCK:
                curr_active = ACTIVE_TASK_INFO.get("prompt", "")
                is_running = ACTIVE_TASK_INFO["running"]

            if not is_running and not TASK_QUEUE:
                send_msg(chat_id, "ℹ️ No hay tareas ejecutándose ni en cola de espera. Tu Mac está desocupado.")
                return

            lines = ["📋 <b>ESTADO DE LA COLA DE TAREAS</b>\n"]
            if is_running:
                p_safe = curr_active.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                if len(p_safe) > 70:
                    p_safe = p_safe[:70] + "..."
                lines.append(f"⚙️ <b>En ejecución:</b>\n┃ <i>«{p_safe}»</i>\n")
            
            if TASK_QUEUE:
                lines.append(f"⏳ <b>En cola ({len(TASK_QUEUE)} pendientes):</b>")
                btns = []
                for idx, q_item in enumerate(TASK_QUEUE, 1):
                    qp = q_item["prompt"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    if len(qp) > 55:
                        qp = qp[:55] + "..."
                    lines.append(f"<b>#{idx}.</b> <i>«{qp}»</i>")
                    btns.append([{"text": f"❌ Cancelar #{idx}", "callback_data": f"dequeue_task:{q_item['id']}"}])
                btns.append([{"text": "🛑 Cancelar Todo y Vaciar Cola", "callback_data": "cancel_active_task"}])
                reply_markup = {"inline_keyboard": btns}
            else:
                lines.append("<i>No hay tareas pendientes en cola.</i>")
                reply_markup = {"inline_keyboard": [[{"text": "🛑 Detener Tarea Activa", "callback_data": "cancel_active_task"}]]}

            send_msg(chat_id, "\n".join(lines), reply_markup=reply_markup)
            return

    # Reiniciar Iris directamente desde el chat de Telegram
    if text.lower() in ["/restart", "/reboot", "reiniciar", "reinicia", "reiníciate", "reiniciate"]:
        RESTART_FLAG_PATH = os.path.join(SCRIPT_DIR, "restart_flag.json")
        try:
            with open(RESTART_FLAG_PATH, "w", encoding="utf-8") as rf:
                json.dump({"chat_id": chat_id, "timestamp": time.time()}, rf)
        except Exception:
            pass

        os_label = "Mac" if sys.platform == "darwin" else ("PC Windows" if sys.platform == "win32" else "Equipo Linux")
        send_msg(
            chat_id,
            f"🔄 <b>Reiniciando Iris en tu {os_label}...</b>\n"
            "<i>Liberando memoria RAM, cerrando procesos y relanzando el servicio. Te confirmo aquí mismo en unos segundos apenas esté en línea.</i>"
        )

        with TASK_LOCK:
            if CURRENT_PROCESS:
                try:
                    CURRENT_PROCESS.kill()
                except Exception:
                    pass

        if sys.platform == "win32":
            bat_script = os.path.join(SCRIPT_DIR, "iris.bat")
            restart_cmd = f'timeout /t 1 /nobreak >nul && "{bat_script}" restart'
            subprocess.Popen(restart_cmd, shell=True)
        else:
            sh_script = os.path.join(SCRIPT_DIR, "iris.sh")
            restart_cmd = f"sleep 1 && '{sh_script}' restart"
            subprocess.Popen(restart_cmd, shell=True, start_new_session=True)
        sys.exit(0)

    # Entrar en reposo (silenciar notificaciones de Mac en Telegram mientras trabajas en el equipo)
    if text.lower() in ["/pause", "/dormir", "/sleep", "pausa", "duerme", "duérmete", "duermete", "silencio", "descansa", "descansar"]:
        IS_PAUSED = True
        save_state({"is_paused": True, "mute_notifications": True})
        send_msg(
            chat_id,
            "💤 <b>Modo Reposo: ON (Notificaciones silenciadas)</b>\n"
            "Mientras trabajas en tu Mac no te llegarán notificaciones automáticas al celular, pero me quedo aquí lista para trabajar siempre que me escribas. Cuando salgas de tu equipo, toca <b>💤 Reposo: ON</b> para pasarlo a <b>OFF</b>.",
            reply_markup=get_persistent_reply_keyboard()
        )
        return

    # Desactivar el reposo (recibir notificaciones porque estás fuera del equipo)
    if text.lower() in ["/wake", "/despertar", "despierta", "despiértate", "despiertate", "activar", "actívate", "activate", "reanudar", "hola iris", "iris despierta"]:
        IS_PAUSED = False
        save_state({"is_paused": False, "mute_notifications": False})
        if not is_anti_sleep_active():
            enable_anti_sleep()
        send_msg(
            chat_id,
            "☀️ <b>Modo Reposo: OFF (Notificaciones activas)</b>\n"
            "¡Listo\1 Como estás fuera de tu equipo, recibirás todas las notificaciones aquí en Telegram y tu Mac responderá incluso con la tapa cerrada.",
            reply_markup=get_persistent_reply_keyboard()
        )
        return

    # Asegurar que el hardware del Mac no se suspenda al recibir una orden directa
    if not is_anti_sleep_active():
        enable_anti_sleep()

    # Modo Manos Libres / Respuestas por Voz (TTS Local)
    if text.lower() in ["/voice", "/voz", "/v"]:
        VOICE_MODE = not VOICE_MODE
        save_state({"voice_mode": VOICE_MODE})
        estado_txt = "ACTIVADO 🎙️🔊" if VOICE_MODE else "DESACTIVADO 🔇"
        msg_voz = (
            f"🎙️ <b>Modo Respuestas por Voz: {estado_txt}</b>\n\n"
            f"<i>{'Ahora Iris te enviará un resumen en nota de voz tras cada orden (texto o audio).' if VOICE_MODE else 'Modo texto activo. (Nota: si envías notas de voz por el micrófono, Iris te responderá por voz automáticamente de todas formas).'}</i>\n\n"
            f"💡 <i>Atajo rápido: escribe <code>/v</code> para alternar en cualquier momento.</i>"
        )
        send_msg(chat_id, msg_voz)
        if VOICE_MODE:
            ogg = synthesize_speech_to_voice("Modo respuestas por voz activado. Te hablaré en cada respuesta.")
            if ogg and os.path.exists(ogg):
                send_voice(chat_id, ogg)
                try:
                    os.remove(ogg)
                except Exception:
                    pass
        return

    if text.lower() in ["/voice on", "/voz on", "/v on", "activar voz", "voz on"]:
        VOICE_MODE = True
        save_state({"voice_mode": True})
        send_msg(chat_id, "🎙️ <b>Respuestas por voz ACTIVADAS</b> 🔊\nTe responderé con notas de voz audibles.")
        ogg = synthesize_speech_to_voice("Modo respuestas por voz activado.")
        if ogg and os.path.exists(ogg):
            send_voice(chat_id, ogg)
            try:
                os.remove(ogg)
            except Exception:
                pass
        return

    if text.lower() in ["/voice off", "/voz off", "/v off", "desactivar voz", "voz off", "silenciar voz"]:
        VOICE_MODE = False
        save_state({"voice_mode": False})
        send_msg(chat_id, "🔇 <b>Respuestas por voz para texto DESACTIVADAS</b>.\nSi me envías notas de voz te seguiré respondiendo por voz; si escribes texto responderé en texto.")
        return

    # Selector de Voz para TTS (/voice_set o /voicename)
    if text.startswith("/voice_set ") or text.startswith("/voz_set ") or text.startswith("/set_voice "):
        new_v = text.split(" ", 1)[1].strip()
        cfg = load_config()
        cfg["voice_name"] = new_v
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        send_msg(chat_id, f"✅ <b>Voz local configurada:</b> <code>{new_v}</code>")
        ogg = synthesize_speech_to_voice(f"Hola, ahora hablaré usando la voz de {new_v}.", voice=new_v)
        if ogg and os.path.exists(ogg):
            send_voice(chat_id, ogg)
            try:
                os.remove(ogg)
            except Exception:
                pass
        return

    # Menú interactivo de voces disponibles
    if text.lower() in ["/voices", "/voces"]:
        sp_voices = [
            ("🇨🇴 Salomé (Colombia Neural)", "es-CO-SalomeNeural"),
            ("🇲🇽 Dalia (México Neural)", "es-MX-DaliaNeural"),
            ("🇨🇱 Catalina (Chile Neural)", "es-CL-CatalinaNeural"),
            ("🇵🇪 Camila (Perú Neural)", "es-PE-CamilaNeural"),
            ("🇲🇽 Paulina (Nativa macOS)", "Paulina"),
            ("🇪🇸 Mónica (Nativa macOS)", "Mónica"),
        ]
        cfg = load_config()
        curr_v = cfg.get("voice_name", VOICE_NAME)
        buttons = []
        for name, vid in sp_voices:
            check = "👉 " if vid == curr_v else "🎙️ "
            buttons.append([{"text": f"{check}{name}", "callback_data": f"set_voice:{vid}"}])
        send_msg(
            chat_id,
            f"🎙️ <b>Voces del Escuadrón en tu Mac</b>\n"
            f"• 👩‍💻 <b>Iris (Al mando):</b> <code>{curr_v}</code>\n"
            f"• 📐 <b>Dante (Arquitectura):</b> <code>{DANTE_VOICE_NAME}</code>\n"
            f"• 🛡️ <b>Cerberus (Ciberseguridad):</b> <code>{CERBERUS_VOICE_NAME}</code>\n\n"
            f"Selecciona la voz principal para <b>Iris</b>:",
            reply_markup={"inline_keyboard": buttons}
        )
        return

    # Modo Anti-Reposo de Hardware (caffeinate en macOS / Win32 ExecutionState en Windows / systemd-inhibit en Linux)
    if text.lower() in ["/caffeinate", "/despierto", "no te duermas", "mantener despierto"]:
        os_label = "Mac" if sys.platform == "darwin" else ("PC Windows" if sys.platform == "win32" else "Equipo")
        if not is_anti_sleep_active():
            enable_anti_sleep()
            send_msg(chat_id, f"☕ <b>Anti-Reposo: ON</b>\nTu {os_label} se mantendrá despierto y no entrará en reposo mientras estés fuera.", reply_markup=get_persistent_reply_keyboard())
        else:
            send_msg(chat_id, f"☕ <b>Anti-Reposo: ON</b> (ya activo).\nTu {os_label} ya tiene activo el modo despierto continuo.", reply_markup=get_persistent_reply_keyboard())
        return

    if text.lower() in ["/decaf", "/dormir_mac", "dormir mac", "dormir pc"]:
        os_label = "Mac" if sys.platform == "darwin" else ("PC Windows" if sys.platform == "win32" else "Equipo")
        if is_anti_sleep_active():
            disable_anti_sleep()
            send_msg(chat_id, f"🌙 <b>Hardware Mac en Ahorro de Energía</b>\nTu {os_label} volvió al modo de reposo del sistema cuando esté inactivo.", reply_markup=get_persistent_reply_keyboard())
        else:
            send_msg(chat_id, f"🌙 <b>Hardware Mac en Ahorro de Energía</b>\nEl modo de energía ya estaba en su configuración normal.", reply_markup=get_persistent_reply_keyboard())
        return

    # Recibir FOTOS (en hilo separado para no bloquear getUpdates)
    if "photo" in msg:
        def _handle_photo_async():
            photos = msg["photo"]
            largest_photo = photos[-1]
            file_id = largest_photo["file_id"]
            p_resp = send_msg(chat_id, "📥 <i>Descargando e inspeccionando tu imagen...</i>")
            p_msg_id = p_resp.get("result", {}).get("message_id") if (p_resp and p_resp.get("ok")) else None
            local_path = download_telegram_file(file_id, suffix=".jpg")
            
            if not local_path:
                if p_msg_id:
                    edit_msg_text(chat_id, p_msg_id, "⚠️ No se pudo guardar la imagen en tu Mac.")
                else:
                    send_msg(chat_id, "⚠️ No se pudo guardar la imagen en tu Mac.")
                return

            if caption:
                prompt = f"El usuario te adjuntó la imagen guardada en: {local_path}\nInstrucción: {caption}"
                dispatch_antigravity_task(chat_id, prompt, reuse_msg_id=p_msg_id, voice_heard=f"🖼️ {caption}")
            else:
                prompt = f"El usuario te adjuntó la imagen guardada en: {local_path}. Usa tu herramienta view_file para inspeccionarla, describe qué contiene o qué error/diseño muestra y dale un diagnóstico o resumen claro."
                dispatch_antigravity_task(chat_id, prompt, reuse_msg_id=p_msg_id, voice_heard="🖼️ Análisis visual de imagen adjunta")
        threading.Thread(target=_handle_photo_async, daemon=True).start()
        return

    # Recibir ARCHIVOS (en hilo separado para no bloquear getUpdates)
    if "document" in msg:
        def _handle_doc_async():
            doc = msg["document"]
            file_id = doc["file_id"]
            file_name = doc.get("file_name", f"doc_{int(time.time())}")
            d_resp = send_msg(chat_id, f"📥 <i>Descargando archivo:</i> <code>{file_name}</code>...")
            d_msg_id = d_resp.get("result", {}).get("message_id") if (d_resp and d_resp.get("ok")) else None
            _, ext = os.path.splitext(file_name)
            local_path = download_telegram_file(file_id, suffix=ext or ".bin")
            if local_path:
                if caption:
                    prompt = f"El usuario te envió el archivo: {local_path}\nInstrucción: {caption}"
                    dispatch_antigravity_task(chat_id, prompt, reuse_msg_id=d_msg_id, voice_heard=f"📎 {file_name}: {caption}")
                else:
                    if d_msg_id:
                        edit_msg_text(chat_id, d_msg_id, f"📁 Archivo guardado en:\n<code>{local_path}</code>")
                    else:
                        send_msg(chat_id, f"📁 Archivo guardado en:\n<code>{local_path}</code>")
        threading.Thread(target=_handle_doc_async, daemon=True).start()
        return

    # Recibir NOTAS DE VOZ (Botón de micrófono en Telegram) o AUDIO (en hilo separado para no bloquear getUpdates)
    if "voice" in msg or "audio" in msg:
        def _handle_voice_async():
            v_data = msg.get("voice") or msg.get("audio")
            file_id = v_data["file_id"]
            v_resp = send_msg(chat_id, "🎙️ <i>Escuchando y transcribiendo tu nota de voz con Whisper...</i>")
            v_msg_id = v_resp.get("result", {}).get("message_id") if (v_resp and v_resp.get("ok")) else None
            local_audio = download_telegram_file(file_id, suffix=".oga")
            if not local_audio:
                if v_msg_id:
                    edit_msg_text(chat_id, v_msg_id, "⚠️ No pude descargar la nota de voz en tu Mac.")
                else:
                    send_msg(chat_id, "⚠️ No pude descargar la nota de voz en tu Mac.")
                return

            transcribed = transcribe_audio_file(local_audio)
            try:
                if os.path.exists(local_audio):
                    os.remove(local_audio)
            except Exception:
                pass
            if transcribed:
                print(f"[VOZ] {transcribed}")
                t_lower = transcribed.lower().strip()
                # Dante: cuando el usuario pide explícitamente planificar o consultar a Dante
                dante_voice_start_triggers = [
                    "pregúntale a dante", "preguntale a dante", "pregunta a dante", "pídele a dante", "pidele a dante",
                    "iris pregúntale a dante", "iris preguntale a dante", "iris, pregúntale a dante", "iris, preguntale a dante",
                    "iris pídele a dante", "iris pidele a dante", "iris, pídele a dante", "iris, pidele a dante",
                    "dante,", "dante:", "planifica ", "crea un plan ", "haz un plan ", "arma un plan ",
                    "probemos a dante", "probemos dante", "llama a dante", "que dante "
                ]
                if any(t_lower.startswith(p) for p in dante_voice_start_triggers) or any(k in t_lower for k in ["pregúntale a dante", "preguntale a dante", "pídele a dante", "pidele a dante", "probemos a dante", "probemos dante"]):
                    if v_msg_id:
                        edit_msg_text(chat_id, v_msg_id, f"🎙️ <b>Escuché:</b> <i>«{html.escape(transcribed)}»</i>\n📐 <i>Iris conectando con Dante...</i>")
                    p_task = re.sub(
                        r'^(?:hola[\.,!\s]*)?(?:iris,?\s*)?(?:pregúntale a dante|preguntale a dante|pregunta a dante|pídele a dante|pidele a dante|probemos a dante|probemos dante|llama a dante|dante,?:?|planifica|crea un plan|haz un plan|arma un plan)\s*(?:para\s+|sobre\s+|que\s+)?',
                        '', transcribed, flags=re.IGNORECASE
                    ).strip()
                    v_engine = "claude_code" if any(w in t_lower for w in ["claude code", "con claude", "usa claude", "la extension de claude", "la extensión de claude"]) else "agy"
                    handle_plan_request(chat_id, p_task or transcribed, force_voice=True, engine=v_engine)
                    return

                # Cerberus: cuando el usuario pide consultar, probar o aprobar con Cerberus (ciberseguridad)
                cerberus_voice_triggers = [
                    "pregúntale a cerberus", "preguntale a cerberus", "pregunta a cerberus", "pídele a cerberus", "pidele a cerberus",
                    "iris pregúntale a cerberus", "iris preguntale a cerberus", "iris, pregúntale a cerberus", "iris, preguntale a cerberus",
                    "iris pídele a cerberus", "iris pidele a cerberus", "iris, pídele a cerberus", "iris, pidele a cerberus",
                    "que cerberus ", "pásalo por cerberus", "pasalo por cerberus", "cerberus,", "cerberus:",
                    "audita la ciberseguridad", "revisa la ciberseguridad", "aprueba la ciberseguridad",
                    "probemos cerberus", "probemos a cerberus", "provemos cerberus", "llama a cerberus"
                ]
                if any(t_lower.startswith(p) for p in cerberus_voice_triggers) or any(k in t_lower for k in [
                    "pásalo por cerberus", "pasalo por cerberus", "que cerberus", "probemos cerberus",
                    "probemos a cerberus", "provemos cerberus", "pregúntale a cerberus", "preguntale a cerberus",
                    "pídele a cerberus", "pidele a cerberus", "con cerberus"
                ]):
                    if v_msg_id:
                        edit_msg_text(chat_id, v_msg_id, f"🎙️ <b>Escuché:</b> <i>«{html.escape(transcribed)}»</i>\n🛡️ <i>Iris convocando a Cerberus (Ciberseguridad)...</i>")
                    c_task = re.sub(
                        r'^(?:hola[\.,!\s]*|la[\.,!\s]*)?(?:iris,?\s*)?(?:pregúntale a cerberus|preguntale a cerberus|pregunta a cerberus|pídele a cerberus|pidele a cerberus|que cerberus|pásalo por cerberus|pasalo por cerberus|probemos a? cerberus|provemos a? cerberus|cerberus,?:?)\s*(?:para\s+|sobre\s+|que\s+)?',
                        '', transcribed, flags=re.IGNORECASE
                    ).strip()
                    v_engine = "claude_code" if any(w in t_lower for w in ["claude code", "con claude", "usa claude", "la extension de claude", "la extensión de claude"]) else "agy"
                    # Si menciona "el plan", buscamos el último plan de Dante
                    target_plan_id = None
                    if "plan" in t_lower:
                        plans = load_plans()
                        dante_plans = [pid for pid, pd in plans.items() if not pd.get("is_cerberus")]
                        if dante_plans:
                            target_plan_id = dante_plans[-1]
                    handle_cerberus_request(chat_id, c_task, plan_id=target_plan_id, force_voice=True, engine=v_engine, audit_now=True)
                    return

                if t_lower in ["revisa el código", "revisa el codigo", "revisa los cambios", "haz un code review", "revisa el git diff", "revisa el proyecto", "revisa cambios"]:
                    if v_msg_id:
                        edit_msg_text(chat_id, v_msg_id, f"🎙️ <b>Escuché:</b> <i>«{html.escape(transcribed)}»</i>\n🕵️ <i>Iniciando Code Review...</i>")
                    handle_review_request(chat_id, force_voice=True)
                    return

                # Detección de motor o modelo solicitado en nota de voz
                m_voice_override = None
                voice_engine = "agy"
                if any(w in t_lower for w in ["claude code", "la extension de claude", "la extensión de claude", "extension claude", "extensión claude"]):
                    voice_engine = "claude_code"
                elif any(w in t_lower for w in ["con claude", "usa claude", "usando claude"]):
                    voice_engine = "claude_code"
                elif any(w in t_lower for w in ["con sonnet", "usa sonnet", "usando sonnet"]):
                    m_voice_override = "claude-sonnet-5-5-low"
                elif any(w in t_lower for w in ["con opus", "usa opus", "usando opus"]):
                    m_voice_override = "claude-opus-5-5-low"
                elif any(w in t_lower for w in ["con gemini", "usa gemini", "con flash", "usa flash"]):
                    m_voice_override = "gemini-3.8-flash-low"
                elif any(w in t_lower for w in ["con argon", "usa argon"]):
                    m_voice_override = "gemini-4-argon-low"

                # Tarjeta única en vivo + Espejo Inteligente de voz
                dispatch_antigravity_task(
                    chat_id, transcribed, force_voice=True,
                    model_override=m_voice_override, engine=voice_engine,
                    reuse_msg_id=v_msg_id, voice_heard=transcribed
                )
            else:
                if v_msg_id:
                    edit_msg_text(chat_id, v_msg_id, "⚠️ No se pudo transcribir con claridad el audio. Intenta hablar un poco más cerca del micrófono.")
                else:
                    send_msg(chat_id, "⚠️ No se pudo transcribir con claridad el audio. Intenta hablar un poco más cerca del micrófono.")
        threading.Thread(target=_handle_voice_async, daemon=True).start()
        return

    if not text:
        return

    print(f"[MENSAJE] {text}")

    # Respuesta instantánea (<0.2s) cuando el usuario pregunta si Iris está activa ("Hola", "¿Te quedaste?", "¿Estás ahí?")
    ping_clean = text.strip().lower().strip("¿?¡!., ")
    if ping_clean in (
        "hola", "hola iris", "te quedaste", "estas ahi", "estás ahí",
        "sigues ahi", "sigues ahí", "estas activa", "estás activa",
        "sigues activa", "sigues viva", "me escuchas", "ping"
    ):
        with TASK_LOCK:
            is_busy_ping = ACTIVE_TASK_INFO["running"]
        if not is_busy_ping and not TASK_QUEUE:
            ws_name = os.path.basename(CURRENT_CWD) or CURRENT_CWD
            caff_lbl = "☕ Activo (Tapa cerrada y batería protegidas)" if is_anti_sleep_active() else "🌙 Normal"
            cfg_p = load_config()
            m_lbl = cfg_p.get("model", "gemini-4-argon-medium")
            ping_msg = (
                f"🟢 <b>¡Aquí estoy al 100%\1</b>\n"
                f"<i>Lista, despierta y con todos los sistemas de vuelo en línea.</i>\n\n"
                f"📍 <b>Proyecto activo:</b> <code>{ws_name}</code>\n"
                f"🤖 <b>Motor:</b> <code>{m_lbl}</code>\n"
                f"🛡️ <b>Anti-Reposo:</b> {caff_lbl}\n\n"
                f"👉 <i>Dime qué hacemos o envíame una nota de voz.</i>"
            )
            send_msg(chat_id, ping_msg, reply_markup=get_persistent_reply_keyboard())
            if VOICE_MODE:
                def _ping_voice():
                    ogg = synthesize_speech_to_voice(f"¡Aquí estoy al cien por ciento\1 Lista y atenta en el proyecto {ws_name}. Dime qué hacemos.")
                    if ogg and os.path.exists(ogg):
                        send_voice(chat_id, ogg)
                        try:
                            os.remove(ogg)
                        except Exception:
                            pass
                threading.Thread(target=_ping_voice, daemon=True).start()
            return

    # Menú /start o /help
    if text in ["/start", "/help", "/menu", "menu", "Menu", "ayuda"]:
        menu = (
            "🚀 <b>IRIS LITE · COMMUNITY EDITION</b>\n"
            "<i>Control de Antigravity directo desde tu celular</i>\n\n"
            "📱 <b>Usa los botones táctiles abajo en tu pantalla para acceso rápido.</b>\n\n"
            "<b>💬 Instrucciones a Antigravity (Motor por Defecto):</b>\n"
            "• <i>Escribe o envía una nota de voz:</i> Antigravity ejecuta en tu Mac con memoria continua.\n"
            "• <code>/new</code> — Empezar conversación limpia al 100% libre.\n"
            "• <code>/cancel</code> — Abortar cualquier tarea en ejecución de inmediato.\n\n"
            "<b>🟠 Extensión Oficial Claude Code (Anthropic):</b>\n"
            "• <code>/cc &lt;orden&gt;</code> — Ejecuta con el motor y créditos de tu extensión Claude Code en tu Mac.\n"
            "• <code>/cc</code> — Ver estado y guía de la extensión Claude Code.\n\n"
            "<b>⚡ Velocidad y Modelos Antigravity:</b>\n"
            "• <code>/speed</code> — Activar modo ultra rápido (Flash Low).\n"
            "• <code>/model &lt;nombre&gt;</code> — Cambiar modelo (flash, argon, sonnet, etc).\n\n"
            "<b>👥 Escuadrón de Agentes (Iris al mando):</b>\n"
            "• 👩‍💻 <b>Iris (Comandante en Jefe):</b> Siempre al mando. Recibe tus órdenes directas, ejecuta código/Mac/navegador y coordina a Dante y Cerberus.\n"
            "• <code>/plan &lt;tarea&gt;</code> o <code>/dante &lt;idea&gt;</code> — 📐 <b>Dante:</b> Diseña la arquitectura en solo lectura antes de que Iris ejecute.\n"
            "• <code>/cerberus &lt;foco&gt;</code> — 🛡️ <b>Cerberus:</b> Ciberseguridad esencial, recomendaciones y aprobación de planeación o ejecución cuando tú lo pidas.\n\n"
            "<b>📁 Proyectos, Revisión y Voz:</b>\n"
            "• <code>/ws</code> o <code>/docs</code> — Explorador visual de proyectos en tu Mac.\n"
            "• <code>/review</code> — Code Review senior y auditoría de Git diff.\n"
            "• <code>/v</code> — Alternar respuestas por voz on/off.\n"
            "• <code>/consumo</code> — Disponibilidad de memoria y cuota Google restante."
        )
        send_msg(chat_id, menu, reply_markup=get_persistent_reply_keyboard())
        return

    # Gestión de Tareas Programadas (/schedule, /recordar, /programar)
    if text.startswith("/schedule") or text.startswith("/programar") or text.startswith("/recordar") or text.lower().startswith("recuérdame "):
        content = ""
        if text.startswith("/schedule"):
            content = text[9:].strip()
        elif text.startswith("/programar"):
            content = text[10:].strip()
        elif text.startswith("/recordar"):
            content = text[9:].strip()
        elif text.lower().startswith("recuérdame "):
            content = text[11:].strip()

        if not content or content in ["list", "lista"]:
            # Listar tareas programadas
            schedules = load_schedules()
            if not schedules:
                send_msg(chat_id, "ℹ️ No tienes tareas programadas en este momento.\n\n💡 <b>Ejemplos de uso:</b>\n• <code>/schedule en 30m compilar build y correr tests</code>\n• <code>/schedule a las 23:30 hacer git status en el proyecto</code>")
                return

            rows = []
            buttons = []
            now_ts = time.time()
            for idx, s in enumerate(schedules):
                diff = int(s.get("target_timestamp", 0) - now_ts)
                t_str = f"en {diff//60} min" if diff > 60 else f"en {diff}s"
                rows.append(f"• <b>#{idx+1} ({t_str}):</b> <i>«{s.get('prompt', '')}»</i>")
                buttons.append([{"text": f"❌ Cancelar #{idx+1}", "callback_data": f"cancel_sched:{idx}"}])

            msg = "⏰ <b>Tareas Programadas en tu Mac:</b>\n\n" + "\n".join(rows)
            send_msg(chat_id, msg, reply_markup={"inline_keyboard": buttons})
            return

        # Parsing de tiempo: ej "/schedule en 15m correr git pull" o "/schedule a las 22:00 compilar"
        diff_secs, desc_time = parse_time_offset_seconds(content)
        if not diff_secs:
            send_msg(
                chat_id,
                "⚠️ <b>Formato de tiempo no reconocido.</b>\n\n"
                "Usa por ejemplo:\n"
                "• <code>/schedule en 20m verificar tests de auth</code>\n"
                "• <code>/schedule en 2h avisarme si la batería bajó</code>\n"
                "• <code>/schedule a las 23:00 hacer git pull de main</code>"
            )
            return

        # Limpiar la orden eliminando la expresión temporal
        clean_prompt = re.sub(r'^(?:en\s+\d+\s*(?:m|min|mins|minutos?|h|horas?|s|seg|segundos?)|(?:a las\s+)?\d{1,2}:\d{2})\s*(?:que\s+|de\s+)?', '', content, flags=re.IGNORECASE).strip()
        if not clean_prompt:
            clean_prompt = content

        schedules = load_schedules()
        new_job = {
            "id": f"job_{int(time.time())}_{len(schedules)}",
            "chat_id": chat_id,
            "prompt": clean_prompt,
            "target_timestamp": time.time() + diff_secs,
            "time_str": desc_time,
            "created_at": time.time()
        }
        schedules.append(new_job)
        save_schedules(schedules)

        send_msg(
            chat_id,
            f"⏰ <b>Tarea programada con éxito en tu Mac:</b>\n"
            f"🎯 <b>Orden:</b> <i>«{clean_prompt}»</i>\n"
            f"⏳ <b>Se ejecutará:</b> <code>{desc_time}</code>\n\n"
            f"👉 <i>Tu Mac ejecutará la tarea autónomamente y te notificará aquí con el resultado.</i>"
        )
        return

    # Historial de Tareas y Rollback (/history, /historial, /tareas)
    if text.lower() in ["/history", "/historial", "/tareas", "historial"]:
        hist = load_history()
        if not hist:
            send_msg(chat_id, "ℹ️ Aún no hay tareas registradas en el historial de esta sesión.")
            return

        rows = []
        buttons = []
        now_ts = time.time()
        for idx, item in enumerate(hist[:5]):
            diff = int(now_ts - item.get("timestamp", now_ts))
            t_str = f"hace {diff//60}m" if diff > 60 else f"hace {diff}s"
            status_ico = "✅" if item.get("success") else "⚠️"
            dur_str = f"{item.get('duration', 0)}s"
            p_short = item.get("prompt", "")[:45]
            if len(item.get("prompt", "")) > 45:
                p_short += "..."
            ws_b = os.path.basename(item.get("cwd", ""))

            rows.append(
                f"<b>#{idx+1} {status_ico} ({t_str} · {dur_str}):</b>\n"
                f"┃ <i>«{p_short}»</i>\n"
                f"📍 <code>{ws_b}</code> · <i>{item.get('model', '')}</i>\n"
            )
            buttons.append([
                {"text": f"🔄 Re-ejecutar #{idx+1}", "callback_data": f"rerun_task:{idx}"},
                {"text": f"⏪ Rollback Git #{idx+1}", "callback_data": f"rollback_task:{idx}"}
            ])

        msg = (
            "🗂️ <b>Historial de Tareas Recientes:</b>\n\n"
            + "\n".join(rows) +
            "💡 <i>Toca Re-ejecutar para volver a correr una tarea o Rollback para deshacer cambios en ese proyecto.</i>"
        )
        send_msg(chat_id, msg, reply_markup={"inline_keyboard": buttons})
        return

    # Rollback directo por comando (/rollback o /deshacer)
    if text.lower() in ["/rollback", "/deshacer", "/undo", "deshacer cambios"]:
        try:
            # Ejecutar git status y git checkout en el CWD actual
            status_out = subprocess.check_output("git status --porcelain", cwd=CURRENT_CWD, shell=True, text=True).strip()
            if not status_out:
                send_msg(chat_id, f"ℹ️ <b>Sin cambios para revertir:</b>\nEl proyecto en <code>{os.path.basename(CURRENT_CWD)}</code> está limpio y sin modificaciones pendientes.")
                return

            btn_confirm = {"inline_keyboard": [[
                {"text": "⚠️ Confirmar y Descartar Cambios", "callback_data": "confirm_git_rollback"},
                {"text": "❌ Cancelar", "callback_data": "cancel_active_task"}
            ]]}
            files_preview = "\n".join([f"• <code>{line}</code>" for line in status_out.splitlines()[:8]])
            send_msg(
                chat_id,
                f"⚠️ <b>¿Deseas revertir los cambios no guardados?</b>\n"
                f"📍 Proyecto: <code>{os.path.basename(CURRENT_CWD)}</code>\n\n"
                f"<b>Archivos modificados:</b>\n{files_preview}\n\n"
                f"<i>Esta acción ejecutará <code>git restore .</code> y descartará las ediciones recientes.</i>",
                reply_markup=btn_confirm
            )
        except Exception as e:
            send_msg(chat_id, f"No se pudo comprobar el repositorio Git: {e}")
        return

    # Vista rápida de cambios en Git (/diff) en < 0.1s sin consumir IA
    if text.lower() in ["/diff", "diff", "ver diff", "ver cambios"]:
        ws_name = os.path.basename(CURRENT_CWD) or CURRENT_CWD
        try:
            porcelain = subprocess.check_output(["git", "status", "--porcelain"], cwd=CURRENT_CWD, text=True, stderr=subprocess.DEVNULL, timeout=5).strip()
            if not porcelain:
                branch = subprocess.check_output(["git", "branch", "--show-current"], cwd=CURRENT_CWD, text=True, stderr=subprocess.DEVNULL, timeout=3).strip() or "main"
                send_msg(chat_id, f"✅ <b> Árbol Git limpio en <code>{ws_name}</code> (rama <code>{branch}</code>):</b>\nNo hay modificaciones ni archivos pendientes en este momento.")
                return
            branch = subprocess.check_output(["git", "branch", "--show-current"], cwd=CURRENT_CWD, text=True, stderr=subprocess.DEVNULL, timeout=3).strip() or "main"
            stat_out = subprocess.check_output(["git", "diff", "--stat"], cwd=CURRENT_CWD, text=True, stderr=subprocess.DEVNULL, timeout=5).strip()
            file_lines = []
            for raw_l in porcelain.splitlines()[:12]:
                code_st = raw_l[:2].strip()
                fname = raw_l[3:].strip()
                ico = "✨" if "?" in code_st else ("🗑️" if "D" in code_st else ("➕" if "A" in code_st else "📝"))
                file_lines.append(f"{ico} <code>{html.escape(code_st or 'M')}</code> · <code>{html.escape(fname)}</code>")
            if len(porcelain.splitlines()) > 12:
                file_lines.append(f"<i>... y {len(porcelain.splitlines()) - 12} archivo(s) más</i>")
            stat_block = f"\n\n📊 <b>Resumen de líneas:</b>\n<pre>{html.escape(stat_out[:900])}</pre>" if stat_out else ""
            diff_btns = {
                "inline_keyboard": [
                    [
                        {"text": "💾 Guardar Commit", "callback_data": "review_commit"},
                        {"text": "🕵️ Auditoría IA (/review)", "callback_data": "cmd_review"}
                    ],
                    [
                        {"text": "⏪ Descartar Cambios", "callback_data": "confirm_git_rollback"}
                    ]
                ]
            }
            send_msg(
                chat_id,
                f"🔍 <b>Vista Rápida de Cambios (<code>{ws_name}</code> · 🌿 <code>{branch}</code>)</b>\n\n"
                + "\n".join(file_lines)
                + stat_block,
                reply_markup=diff_btns
            )
        except Exception as e:
            send_msg(chat_id, f"⚠️ No se pudo obtener el diff en <code>{ws_name}</code>: {e}")
        return

    # Commit directo e instantáneo (/commit o /commit <mensaje>)
    if text.lower() == "/commit" or text.lower().startswith("/commit "):
        custom_msg = text[7:].strip()
        ws_name = os.path.basename(CURRENT_CWD) or CURRENT_CWD
        try:
            porcelain = subprocess.check_output(["git", "status", "--porcelain"], cwd=CURRENT_CWD, text=True, stderr=subprocess.DEVNULL).strip()
            if not porcelain:
                send_msg(chat_id, f"ℹ️ <b>Todo al día:</b>\nNo hay cambios pendientes para hacer commit en <code>{ws_name}</code>.")
                return
            changed_files = [line[3:].strip() for line in porcelain.splitlines() if len(line) > 3]
            if not custom_msg:
                sample = ", ".join([os.path.basename(f) for f in changed_files[:3]])
                extra = f" (+{len(changed_files)-3} files)" if len(changed_files) > 3 else ""
                custom_msg = f"feat({ws_name}): update {sample}{extra}"
            safe_msg = custom_msg.replace("'", "'\\''")
            code, out = run_shell(f"git add -A && git commit -m '{safe_msg}'")
            if code == 0:
                send_msg(chat_id, f"✅ <b>Commit local guardado en <code>{ws_name}</code>:</b>\n<code>{custom_msg}</code>\n<pre>{html.escape(out[:600])}</pre>")
            else:
                commit_id = f"c{int(time.time()*1000) % 10000000}"
                SUGGESTED_COMMITS[commit_id] = custom_msg
                files_preview = "\n".join([f"• <code>{html.escape(line)}</code>" for line in porcelain.splitlines()[:8]])
                btn = {"inline_keyboard": [
                    [{"text": "✅ Confirmar y Guardar Commit", "callback_data": f"review_do_commit:{commit_id}"}],
                    [{"text": "❌ Cancelar", "callback_data": "review_cancel_commit"}]
                ]}
                send_msg(
                    chat_id,
                    f"💾 <b>Commit preparado para <code>{ws_name}</code>:</b>\n"
                    f"📝 <code>{html.escape(custom_msg)}</code>\n\n"
                    f"<b>Archivos ({len(changed_files)}):</b>\n{files_preview}",
                    reply_markup=btn
                )
        except Exception as e:
            send_msg(chat_id, f"⚠️ No se pudo ejecutar el commit en <code>{ws_name}</code>: {e}")
        return

    # Status
    if text == "/status":
        send_msg(chat_id, get_system_status())
        return

    # Consumo y Cuota del Modelo (/tokens, /consumo, /uso, /quota)
    if text.lower() in ["/tokens", "/consumo", "/uso", "/quota", "/token", "/cuota"]:
        m_data = get_model_context_metrics()
        if not m_data:
            send_msg(chat_id, "ℹ️ No hay métricas de consumo registradas aún para la sesión activa.")
            return

        pct = m_data["percentage"]
        bar = m_data["bar"]
        curr_k = f"{m_data['current_context']:,}"
        max_k = f"{m_data['max_context']:,}"
        last_out = f"{m_data['last_turn_out']:,}"
        last_in = f"{m_data['last_turn_in']:,}"
        cached = f"{m_data['cached_tokens']:,}"
        total_session = f"{m_data['total_tokens_session']:,}"

        status_emoji = "🟢" if pct < 60 else ("🟡" if pct < 85 else "🔴")

        # Calcular porcentaje y barra en formato RESTANTE (lo que te queda libre)
        curr_ctx = m_data["current_context"]
        max_ctx = m_data["max_context"]
        q_pct = m_data.get("quota_5h_pct", 0)

        ctx_rem_pct = max(0.0, round(100.0 - pct, 1))
        ctx_rem_k = f"{(max_ctx - curr_ctx):,}"
        filled_rem = int(round((ctx_rem_pct / 100) * 10))
        bar_rem = "█" * filled_rem + "░" * (10 - filled_rem)

        quota_rem_pct = max(0.0, round(100.0 - q_pct, 1))
        q_filled_rem = int(round((quota_rem_pct / 100) * 10))
        q_bar_rem = "█" * q_filled_rem + "░" * (10 - q_filled_rem)

        msg = (
            f"📊 <b>DISPONIBILIDAD Y CUOTA RESTANTE</b>\n"
            f"🤖 <code>{m_data['active_model']}</code>\n"
            f"─────────────────────────────\n\n"
            f"1️⃣ <b>Memoria de Conversación:</b>\n"
            f"• <b>Disponible:</b> <b>{ctx_rem_pct}% libre</b> <code>[{bar_rem}]</code>\n"
            f"• <b>Te quedan:</b> {ctx_rem_k} tokens\n"
            f"• <b>Tiempo activo:</b> <code>{m_data['session_duration']}</code> ({m_data['session_steps']} turnos)\n\n"
            f"2️⃣ <b>Cuota Google (Ventana 5h):</b>\n"
            f"• <b>Disponible:</b> <b>{quota_rem_pct}% libre</b> <code>[{q_bar_rem}]</code>\n"
            f"• ⏳ <b>Se renueva en:</b> <code>{m_data.get('quota_refresh_in', 'N/A')}</code>\n\n"
            f"─────────────────────────────\n"
            f"💡 <i>¿Quieres empezar un chat limpio? Toca abajo:</i>"
        )
        reset_btn = {"inline_keyboard": [[{"text": "🧹 Limpiar Memoria al 100% libre (/new)", "callback_data": "trigger_new_session"}]]}
        send_msg(chat_id, msg, reply_markup=reset_btn)
        return

    # Screenshot
    if text in ["/screenshot", "/pantalla", "captura", "screenshot"]:
        send_msg(chat_id, "📸 Tomando foto a la pantalla de tu Mac...")
        path = take_screenshot()
        if os.path.exists(path):
            send_photo(chat_id, "Pantalla de tu Mac en este momento:", path)
            try:
                os.remove(path)
            except Exception:
                pass
        else:
            send_msg(chat_id, "No se pudo tomar la captura en este momento.")
        return

    # Puertos
    if text in ["/ports", "/puertos"]:
        send_msg(chat_id, get_ports_table())
        return

    # Top procesos
    if text in ["/top", "/procesos"]:
        send_msg(chat_id, get_top_processes_table())
        return

    # Descargar archivo al celular
    if text.startswith("/file ") or text.startswith("/doc "):
        doc_path = text.split(" ", 1)[1].strip()
        doc_expanded = os.path.realpath(os.path.expanduser(doc_path))
        if os.path.exists(doc_expanded):
            send_msg(chat_id, f"📤 Enviando archivo: <code>{os.path.basename(doc_expanded)}</code>...")
            send_document(chat_id, f"Aquí tienes: {os.path.basename(doc_expanded)}", doc_expanded)
        else:
            send_msg(chat_id, f"No encontré el archivo en: <code>{doc_expanded}</code>")
        return

    # Enviar imagen al celular
    if text.startswith("/img ") or text.startswith("/foto "):
        img_path = text.split(" ", 1)[1].strip()
        img_expanded = os.path.realpath(os.path.expanduser(img_path))
        if os.path.exists(img_expanded):
            send_photo(chat_id, f"Aquí tienes: {os.path.basename(img_expanded)}", img_expanded)
        else:
            send_msg(chat_id, f"No encontré la imagen en: <code>{img_expanded}</code>")
        return

    # PWD
    if text == "/pwd":
        cfg = load_config()
        m = cfg.get("model", "gemini-3.8-flash-low")
        send_msg(chat_id, f"📁 <b>Carpeta actual:</b>\n<code>{CURRENT_CWD}</code>\n\n🤖 <b>Modelo activo:</b> <code>{m}</code>")
        return

    # Modo Dante y Planificación (/plan, /dante)
    if text.lower() in ["/plan", "/planear", "plan", "/dante", "dante"]:
        handle_plan_request(chat_id, "")
        return

    dante_text_prefixes = [
        "/plan ", "/planear ", "plan: ", "planifica ",
        "/dante ", "dante: ", "dante, "
    ]
    if any(text.lower().startswith(p) for p in dante_text_prefixes):
        m_task = re.sub(
            r'^(?:/plan|/planear|plan:|planifica|/dante|dante:|dante,)\s*',
            '', text, flags=re.IGNORECASE
        ).strip()
        plan_engine = "agy"
        if m_task.lower().startswith("cc ") or m_task.lower().startswith("claudecode ") or m_task.lower().startswith("claude "):
            plan_engine = "claude_code"
            m_task = re.sub(r'^(?:cc|claudecode|claude)\s*', '', m_task, flags=re.IGNORECASE).strip()
        handle_plan_request(chat_id, m_task, engine=plan_engine)
        return

    # Frases coloquiales: "Iris pregúntale a Dante..." o "Pregúntale a Dante..."
    if any(text.lower().startswith(p) for p in [
        "pregúntale a dante", "preguntale a dante", "pregunta a dante", "pídele a dante", "pidele a dante",
        "iris pregúntale a dante", "iris preguntale a dante", "iris, pregúntale a dante", "iris, preguntale a dante"
    ]):
        m_task = re.sub(
            r'^(?:iris,?\s*)?(?:pregúntale a dante|preguntale a dante|pregunta a dante|pídele a dante|pidele a dante)\s*(?:que\s+|cómo\s+|como\s+|sobre\s+)?',
            '', text, flags=re.IGNORECASE
        ).strip()
        plan_engine = "agy"
        if any(w in text.lower() for w in ["claude code", "con claude", "usa claude", "la extension de claude", "la extensión de claude"]):
            plan_engine = "claude_code"
        handle_plan_request(chat_id, m_task, engine=plan_engine)
        return

    # Guardián de Ciberseguridad Cerberus (/cerberus, /seguridad, /sec, /cyber) — solo bajo petición del usuario
    if text.lower().strip(" .,!¡?¿") in ["/cerberus", "cerberus", "/seguridad", "/ciberseguridad", "/sec", "/cyber"]:
        handle_cerberus_request(chat_id, "", audit_now=True)
        return

    cerberus_text_prefixes = [
        "/cerberus ", "/seguridad ", "/ciberseguridad ", "/sec ", "/cyber ",
        "cerberus: ", "cerberus, ", "cerberus "
    ]
    if any(text.lower().startswith(p) for p in cerberus_text_prefixes):
        c_task = re.sub(
            r'^(?:/cerberus|/seguridad|/ciberseguridad|/sec|/cyber|cerberus:|cerberus,|cerberus)\s*',
            '', text, flags=re.IGNORECASE
        ).strip()
        c_engine = "agy"
        if c_task.lower().startswith("cc ") or c_task.lower().startswith("claudecode ") or c_task.lower().startswith("claude "):
            c_engine = "claude_code"
            c_task = re.sub(r'^(?:cc|claudecode|claude)\s*', '', c_task, flags=re.IGNORECASE).strip()
        target_plan_id = None
        if c_task.lower() in ["plan", "el plan", "aprobar plan", "aprueba el plan", "revisar plan", "revisa el plan"]:
            plans = load_plans()
            dante_plans = [pid for pid, pd in plans.items() if not pd.get("is_cerberus")]
            if dante_plans:
                target_plan_id = dante_plans[-1]
                c_task = ""
        handle_cerberus_request(chat_id, c_task, plan_id=target_plan_id, engine=c_engine, audit_now=True)
        return

    # Frases coloquiales para Cerberus: "Iris, pídele a Cerberus...", "Probemos Cerberus", "Llama a Cerberus...", "Pásalo por Cerberus..."
    if re.search(
        r'^(?:hola[.,!\s]*|la[.,!\s]*|oye[.,!\s]*|buenos\s+d[ií]as[.,!\s]*|iris[.,!\s]*)*'
        r'(?:preg[uú]ntale\s+a\s+cerberus|pregunta\s+a\s+cerberus|p[ií]dele\s+a\s+cerberus|'
        r'que\s+cerberus|p[aá]salo\s+por\s+cerberus|probemos\s+(?:a\s+)?cerberus|provemos\s+(?:a\s+)?cerberus|'
        r'probar\s+(?:a\s+)?cerberus|llama\s+a\s+cerberus|convoca\s+a\s+cerberus|activa\s+(?:a\s+)?cerberus)\b',
        text.strip(),
        flags=re.IGNORECASE
    ):
        c_task = re.sub(
            r'^(?:hola[.,!\s]*|la[.,!\s]*|oye[.,!\s]*|buenos\s+d[ií]as[.,!\s]*|iris[.,!\s]*)*'
            r'(?:preg[uú]ntale\s+a\s+cerberus|pregunta\s+a\s+cerberus|p[ií]dele\s+a\s+cerberus|'
            r'que\s+cerberus|p[aá]salo\s+por\s+cerberus|probemos\s+(?:a\s+)?cerberus|provemos\s+(?:a\s+)?cerberus|'
            r'probar\s+(?:a\s+)?cerberus|llama\s+a\s+cerberus|convoca\s+a\s+cerberus|activa\s+(?:a\s+)?cerberus)'
            r'[.,!\s]*(?:que\s+|c[oó]mo\s+|sobre\s+|para\s+)?',
            '', text.strip(), flags=re.IGNORECASE
        ).strip(" .,!¡?¿")
        c_engine = "agy"
        if any(w in text.lower() for w in ["claude code", "con claude", "usa claude", "la extension de claude", "la extensión de claude"]):
            c_engine = "claude_code"
        target_plan_id = None
        if "plan" in text.lower():
            plans = load_plans()
            dante_plans = [pid for pid, pd in plans.items() if not pd.get("is_cerberus")]
            if dante_plans:
                target_plan_id = dante_plans[-1]
        handle_cerberus_request(chat_id, c_task, plan_id=target_plan_id, engine=c_engine, audit_now=True)
        return

    # Modo de Code Review y Análisis de Git Diff (/review, /revisar, /diff, /cambios)
    if text.lower() in ["/review", "/revisar", "/diff", "/cambios", "/codereview", "review", "code review", "revisar cambios"]:
        handle_review_request(chat_id)
        return

    # Explorador Visual de Carpetas y Proyectos (/ws, /workspace, /workspaces, /projects, /proyectos, /p, /explore, /nav, /ls)
    if text.lower() in ["/ws", "/workspace", "/workspaces", "/projects", "/proyectos", "/p", "/explore", "/explorar", "/nav", "/browse", "/ls"]:
        txt, markup = render_folder_explorer(CURRENT_CWD, page=0)
        send_msg(chat_id, txt, reply_markup=markup)
        return

    # Explorar directamente ~/Documents (/docs, /documentos)
    if text.lower() in ["/docs", "/documentos", "/misdocumentos"]:
        docs_dir = os.path.expanduser("~/Documents")
        txt, markup = render_folder_explorer(docs_dir, page=0)
        send_msg(chat_id, txt, reply_markup=markup)
        return

    # Acceso rápido a repositorios git detectados (/git, /repos, /proyectosgit)
    if text.lower() in ["/git", "/repos", "/proyectosgit", "/gitrepos"]:
        txt, markup = render_quick_git_view(CURRENT_CWD)
        send_msg(chat_id, txt, reply_markup=markup)
        return

    # Explorar subcarpeta directa: /explore <subcarpeta>, /docs <subcarpeta> o /ls <subcarpeta>
    if text.startswith("/explore ") or text.startswith("/explorar ") or text.startswith("/ls ") or text.startswith("/docs ") or text.startswith("/documentos "):
        parts = text.split(" ", 1)
        target = parts[1].strip()
        matched = resolve_path_fuzzy(target, CURRENT_CWD)
        if matched and os.path.isdir(matched):
            txt, markup = render_folder_explorer(matched, page=0)
            send_msg(chat_id, txt, reply_markup=markup)
        else:
            send_msg(chat_id, f"⚠️ No encontré la carpeta: <code>{target}</code>.\n💡 Escribe <code>/ws</code> o <code>/docs</code> para explorar tus carpetas interactivamente.")
        return

    # Comando /use <proyecto> para cambio directo por texto
    if text.startswith("/use ") or text.startswith("/workspace "):
        target = text.split(" ", 1)[1].strip()
        matched = resolve_path_fuzzy(target, CURRENT_CWD)
        if matched and os.path.isdir(matched):
            CURRENT_CWD = matched
            cfg = load_config()
            cfg["default_cwd"] = matched
            try:
                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, indent=2)
            except Exception:
                pass
            p_name = os.path.basename(matched)
            git_badge = get_git_status_badge(matched) or ""
            git_line = f"\n{git_badge}\n" if git_badge else ""
            send_msg(
                chat_id,
                f"✅ <b>Espacio de trabajo cambiado:</b>\n"
                f"📦 <b>{p_name}</b>\n"
                f"<code>{matched}</code>\n"
                f"{git_line}\n"
                f"👉 <i>Las siguientes instrucciones se ejecutarán aquí.</i>"
            )
        else:
            send_msg(chat_id, f"No encontré el proyecto <code>{target}</code>.\n💡 Escribe <code>/ws</code> para ver tus carpetas y proyectos disponibles.")
        return

    # CD interactivo o directo con apertura de explorador
    if text.startswith("/cd"):
        target = text[3:].strip()
        if not target or target == "~":
            docs_dir = os.path.expanduser("~/Documents")
            txt, markup = render_folder_explorer(docs_dir, page=0)
            send_msg(chat_id, txt, reply_markup=markup)
            return

        matched = resolve_path_fuzzy(target, CURRENT_CWD)
        if matched and os.path.isdir(matched):
            CURRENT_CWD = matched
            cfg = load_config()
            cfg["default_cwd"] = matched
            try:
                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, indent=2)
            except Exception:
                pass
            txt, markup = render_folder_explorer(CURRENT_CWD, page=0)
            p_name = os.path.basename(CURRENT_CWD) or CURRENT_CWD
            git_badge = get_git_status_badge(CURRENT_CWD) or ""
            git_line = f"\n{git_badge}\n" if git_badge else ""
            send_msg(
                chat_id,
                f"📁 <b>Te moviste a:</b> <code>{p_name}</code>\n"
                f"<code>{CURRENT_CWD}</code>\n"
                f"{git_line}",
                reply_markup=markup
            )
        else:
            send_msg(chat_id, f"No encontré esa carpeta: <code>{target}</code>\n💡 Usa <code>/ws</code> para navegar visualmente por tus carpetas.")
        return

    # Catálogo y cambio de Modelo de Antigravity
    if text.startswith("/model") or text.startswith("/modelo") or text == "/m":
        parts = text.split(" ", 1)
        if len(parts) > 1 and parts[1].strip():
            target_q = parts[1].strip()
            found = find_model_by_query(target_q)
            if found:
                new_model = found["id"]
                cfg = load_config()
                cfg["model"] = new_model
                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, indent=2)
                send_msg(
                    chat_id,
                    f"✅ <b>Modelo predeterminado actualizado:</b>\n"
                    f"{found['emoji']} <b>{found['name']}</b> (<code>{new_model}</code>)\n"
                    f"<i>{found['desc']}</i>"
                )
            else:
                send_msg(chat_id, f"⚠️ No encontré el modelo: <code>{target_q}</code>\n💡 Envía <code>/model</code> para ver los botones y la lista completa.")
            return

        cfg = load_config()
        curr = cfg.get("model", "gemini-3.8-flash-low")

        lines = [
            "🤖 <b>CATÁLOGO DE MODELOS A DEMANDA</b>",
            "<i>Toca un botón para cambiar el motor predeterminado de Antigravity:</i>\n"
        ]

        buttons = []
        row = []
        for idx, m in enumerate(AVAILABLE_MODELS, 1):
            is_active = (m["id"] == curr)
            check = "🟢 " if is_active else ""
            lines.append(f"<b>{idx}.</b> {m['emoji']} <code>{m['id']}</code>\n   <i>{m['desc']}</i>{'  👈 <b>ACTIVO</b>' if is_active else ''}")

            btn_label = f"{check}{m['emoji']} {m['short']}"
            row.append({"text": btn_label, "callback_data": f"set_model:{m['id']}"})
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)

        lines.append("\n💡 <b>Comandos directos a demanda:</b>")
        lines.append("• <code>/cc &lt;orden&gt;</code> — 🟠 Extensión oficial Claude Code (Anthropic)")
        lines.append("• <code>/sonnet &lt;orden&gt;</code> — 🚀 Antigravity con Claude Sonnet 5.5")
        lines.append("• <code>/opus &lt;orden&gt;</code> — 🚀 Antigravity con Claude Opus 5.5")
        lines.append("• <code>/argon &lt;orden&gt;</code> — 🚀 Antigravity con Gemini 4 Argon")
        lines.append("• <code>/flash &lt;orden&gt;</code> — 🚀 Antigravity con Gemini 3.8 Flash")
        lines.append("• <code>/model &lt;número o nombre&gt;</code> (ej: <code>/model 1</code> o <code>/model sonnet</code>)")

        reply_markup = {"inline_keyboard": buttons}
        send_msg(chat_id, "\n".join(lines), reply_markup=reply_markup)
        return

    # Modo rápida velocidad
    if text.lower() in ["/speed", "/fast", "/rapido", "/rápido"]:
        cfg = load_config()
        cfg["model"] = "gemini-3.8-flash-low"
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        send_msg(chat_id, "⚡ <b>Modo Máxima Velocidad activado.</b>\nModelo fijado en <code>gemini-3.8-flash-low</code> (Google 3.8, respuestas en 5-8s).")
        return

    # Nueva sesión / Limpiar contexto
    if text.lower() in ["/new", "/reset", "/clear", "nueva conversacion", "nueva conversación", "nuevo chat", "reiniciar chat"]:
        SESSION_RESET_FLAG = True
        send_msg(
            chat_id,
            "🧹 <b>Nueva sesión iniciada con Antigravity.</b>\n"
            "<i>La memoria de la conversación anterior se ha limpiado. La próxima orden empezará una sesión limpia sin contexto previo.</i>"
        )
        return

    # Shell directo
    if text.startswith("/sh ") or text.startswith("/run "):
        cmd = text.split(" ", 1)[1].strip()
        safe_cmd = cmd.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        send_msg(chat_id, f"⚙️ Ejecutando: <code>{safe_cmd}</code>...")
        code, out = run_shell(cmd)
        try:
            subprocess.Popen(["afplay", "/System/Library/Sounds/Glass.aiff"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        safe_out = out.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        status_title = "🔔 <b>¡Comando finalizado con éxito!</b>" if code == 0 else f"⚠️ <b>Comando finalizado (código {code}):</b>"
        send_msg(
            chat_id,
            f"{status_title}\n"
            f"🎯 <b>Acción ejecutada:</b> <code>{safe_cmd}</code>\n\n"
            f"<pre>{safe_out}</pre>\n"
            "👉 <i>Terminal libre y lista para el siguiente comando.</i>"
        )
        return

    # Ajuste o consulta de Timeout
    if text.startswith("/timeout"):
        parts = text.split(" ", 1)
        if len(parts) > 1 and parts[1].strip():
            val_str = parts[1].strip()
            try:
                new_val = int(val_str)
                cfg = load_config()
                cfg["task_timeout_seconds"] = new_val
                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, indent=2)
                t_desc = "ilimitado (sin corte automático)" if new_val <= 0 else f"{new_val} segundos ({new_val//60} min)"
                send_msg(chat_id, f"⏱️ <b>Tiempo límite de Antigravity actualizado:</b> {t_desc}")
            except ValueError:
                send_msg(chat_id, "Formato: <code>/timeout 0</code> (ilimitado) o <code>/timeout 600</code> (10 min)")
        else:
            cfg = load_config()
            curr = int(cfg.get("task_timeout_seconds", 0))
            curr_desc = "ilimitado (sin corte automático)" if curr <= 0 else f"{curr}s ({curr//60} min)"
            send_msg(chat_id, f"⏱️ <b>Límite actual de Antigravity:</b> {curr_desc}\n<i>Usa <code>/timeout 0</code> para ilimitado o <code>/timeout &lt;segundos&gt;</code> para cambiarlo.</i>")
        return

    # Manejo de respuesta numérica a cuadro de diálogo pendiente
    if os.path.exists(PENDING_DIALOG_PATH):
        try:
            with open(PENDING_DIALOG_PATH, "r", encoding="utf-8") as df:
                d_info = json.load(df)
            if not d_info.get("resolved") and (time.time() - d_info.get("timestamp", 0) < 600):
                opts = d_info.get("options", [])
                clean_num = text.strip()
                if clean_num.isdigit():
                    idx = int(clean_num) - 1
                    if 0 <= idx < len(opts):
                        opt_name = opts[idx]
                        d_info["resolved"] = True
                        d_info["selected_option"] = opt_name
                        with open(PENDING_DIALOG_PATH, "w", encoding="utf-8") as df:
                            json.dump(d_info, df)
                        send_msg(chat_id, f"✅ <b>Elegiste la opción {idx+1}:</b> <i>«{opt_name}»</i>\nSe la transmito a Antigravity en tu Mac.")
                        dispatch_antigravity_task(chat_id, f"El usuario eligió la opción: {opt_name}", continue_session=True)
                        return
        except Exception:
            pass

    # Ejecución de instrucción en Antigravity con memoria continua por defecto
    cfg = load_config()
    default_cont = cfg.get("continue_conversation", True)

    continue_session = default_cont
    model_override = None

    t_low = text.lower().strip()

    # Comandos informativos / estado de Claude Code
    if t_low in ["/cc", "/claudecode"]:
        claude_exists = os.path.exists(CLAUDE_BIN)
        claude_status = "🟢 Instalado y listo" if claude_exists else "🔴 No encontrado"
        ws_name = os.path.basename(CURRENT_CWD) or CURRENT_CWD

        info_card = (
            "🟠 <b>CLAUDE CODE (EXTENSIÓN OFICIAL ANTHROPIC)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ <b>Estado CLI:</b> {claude_status}\n"
            f"📍 <b>Ruta ejecutable:</b> <code>{CLAUDE_BIN}</code>\n"
            f"📁 <b>Proyecto activo:</b> <code>{ws_name}</code>\n"
            f"🧠 <b>Motor:</b> <code>Claude Code · Sonnet 3.7 (Anthropic)</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "<b>🎯 ¿Cómo usarlo desde Telegram?</b>\n"
            "• <code>/cc &lt;tu orden&gt;</code>\n"
            "• <code>/claudecode &lt;tu orden&gt;</code>\n"
            "• <code>cc: &lt;tu orden&gt;</code>\n"
            "• O con nota de voz: <i>«Iris, con la extensión de Claude...»</i>\n\n"
            "<b>🛡️ Cero confusión con Antigravity:</b>\n"
            "• <b>Antigravity por defecto (🚀):</b> Corre con Google Gemini o los modelos del IDE. Respuestas con tarjeta azul/cohete.\n"
            "• <b>Claude Code (🟠):</b> Corre exclusivamente con la extensión de Anthropic vinculada a tu cuenta y créditos. Respuestas con tarjeta naranja.\n\n"
            "💡 <i>Ejemplo de prueba:</i>\n"
            "<code>/cc explícame qué hace este proyecto</code>"
        )
        send_msg(chat_id, info_card)
        return

    engine = "agy"
    if t_low.startswith("/cc ") or t_low.startswith("/claudecode ") or t_low.startswith("cc: ") or t_low.startswith("claudecode: "):
        engine = "claude_code"
        prompt = re.sub(r'^(?:/(?:cc|claudecode)|(?:cc|claudecode):)\s*', '', text, flags=re.IGNORECASE).strip()
        if prompt.lower().startswith("plan ") or prompt.lower().startswith("dante ") or prompt.lower().startswith("planifica "):
            m_task = re.sub(r'^(?:plan|dante|planifica)\s*', '', prompt, flags=re.IGNORECASE).strip()
            handle_plan_request(chat_id, m_task, engine="claude_code")
            return
        if prompt.lower().startswith("cerberus ") or prompt.lower().startswith("seguridad "):
            c_task = re.sub(r'^(?:cerberus|seguridad)\s*', '', prompt, flags=re.IGNORECASE).strip()
            handle_cerberus_request(chat_id, c_task, engine="claude_code", audit_now=True)
            return
    elif t_low.startswith("/claude ") or t_low.startswith("claude: "):
        engine = "claude_code"
        prompt = re.sub(r'^(?:/claude|claude:)\s*', '', text, flags=re.IGNORECASE).strip()
        if prompt.lower().startswith("plan ") or prompt.lower().startswith("dante ") or prompt.lower().startswith("planifica "):
            m_task = re.sub(r'^(?:plan|dante|planifica)\s*', '', prompt, flags=re.IGNORECASE).strip()
            handle_plan_request(chat_id, m_task, engine="claude_code")
            return
        if prompt.lower().startswith("cerberus ") or prompt.lower().startswith("seguridad "):
            c_task = re.sub(r'^(?:cerberus|seguridad)\s*', '', prompt, flags=re.IGNORECASE).strip()
            handle_cerberus_request(chat_id, c_task, engine="claude_code", audit_now=True)
            return
    elif t_low.startswith("/sonnet ") or t_low.startswith("sonnet: "):
        model_override = "claude-sonnet-5-5-low"
        prompt = re.sub(r'^(?:/sonnet|sonnet:)\s*', '', text, flags=re.IGNORECASE).strip()
    elif t_low.startswith("/opus ") or t_low.startswith("opus: "):
        model_override = "claude-opus-5-5-low"
        prompt = re.sub(r'^(?:/opus|opus:)\s*', '', text, flags=re.IGNORECASE).strip()
    elif t_low.startswith("/argon ") or t_low.startswith("argon: "):
        model_override = "gemini-4-argon-low"
        prompt = re.sub(r'^(?:/argon|argon:)\s*', '', text, flags=re.IGNORECASE).strip()
    elif t_low.startswith("/flash ") or t_low.startswith("/gemini ") or t_low.startswith("flash: ") or t_low.startswith("gemini: "):
        model_override = "gemini-3.8-flash-low"
        prompt = re.sub(r'^(?:/(?:flash|gemini)|(?:flash|gemini):)\s*', '', text, flags=re.IGNORECASE).strip()
    elif t_low.startswith("/gpt ") or t_low.startswith("gpt: "):
        model_override = "gpt-oss-120b-medium"
        prompt = re.sub(r'^(?:/gpt|gpt:)\s*', '', text, flags=re.IGNORECASE).strip()
    elif SESSION_RESET_FLAG:
        continue_session = False
        SESSION_RESET_FLAG = False
        prompt = text
    elif text.startswith("/c ") or text.startswith("/continue "):
        parts = text.split(" ", 1)
        prompt = parts[1].strip() if len(parts) > 1 else ""
        continue_session = True
    elif text.startswith("/new ") or text.startswith("/fresh "):
        parts = text.split(" ", 1)
        prompt = parts[1].strip() if len(parts) > 1 else ""
        continue_session = False
    elif text.startswith("/agy "):
        prompt = text[5:].strip()
    else:
        prompt = text

    safe_prompt = prompt.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    dispatch_antigravity_task(chat_id, prompt, continue_session=continue_session, model_override=model_override, engine=engine)

def handle_callback_query(cb):
    global CURRENT_CWD, SESSION_RESET_FLAG, IS_PAUSED, VOICE_MODE
    cb_id = cb["id"]
    data = cb.get("data", "")
    user_id = cb.get("from", {}).get("id")
    chat_id = cb.get("message", {}).get("chat", {}).get("id")

    if user_id not in ALLOWED_USERS:
        return

    api_call("answerCallbackQuery", {"callback_query_id": cb_id, "text": "Opción seleccionada"})

    # Acciones rápidas desde el Menú de Comandos Directos
    if data == "cmd_cc":
        handle_message({"chat": {"id": chat_id}, "from": {"id": user_id}, "text": "/cc"})
        return

    if data == "cmd_plan":
        send_msg(
            chat_id,
            "📐 <b>Modo Planificación Estratégica con Dante</b>\n\n"
            "Para que Dante (voz Jorge MX) elabore un plan de arquitectura paso a paso antes de que Iris ejecute código, escribe:\n"
            "<code>/plan tu requerimiento</code>\n\n"
            "<i>Ejemplo: <code>/plan refactorizar el sistema de login y agregar validación por SMS</code></i>\n\n"
            "🎙️ <i>O envíame un audio diciendo:</i>\n"
            "«Iris, pídele a Dante que arme un plan para...»",
            reply_markup=get_persistent_reply_keyboard()
        )
        return

    if data == "cmd_cerberus":
        handle_cerberus_request(chat_id, "")
        return

    if data == "cmd_cerberus_audit":
        handle_cerberus_request(chat_id, "", audit_now=True)
        return

    if data == "cmd_cerberus_last_plan":
        plans = load_plans()
        dante_plans = [pid for pid, pd in plans.items() if not pd.get("is_cerberus")]
        if dante_plans:
            handle_cerberus_request(chat_id, "", plan_id=dante_plans[-1], audit_now=True)
        else:
            send_msg(chat_id, "ℹ️ No hay ningún plan previo de Dante para evaluar. Ejecutando auditoría de ciberseguridad sobre el proyecto actual...")
            handle_cerberus_request(chat_id, "", audit_now=True)
        return

    if data.startswith("plan_cerberus:"):
        target_pid = data.split(":", 1)[1]
        handle_cerberus_request(chat_id, "", plan_id=target_pid, audit_now=True)
        return

    if data == "cmd_review":
        handle_review_request(chat_id)
        return

    if data == "cmd_diff":
        handle_message({"chat": {"id": chat_id}, "from": {"id": user_id}, "text": "/diff"})
        return

    if data == "cmd_ws":
        txt, markup = render_folder_explorer(CURRENT_CWD, page=0)
        send_msg(chat_id, txt, reply_markup=markup)
        return

    if data == "cmd_docs":
        docs_dir = os.path.expanduser("~/Documents")
        txt, markup = render_folder_explorer(docs_dir, page=0)
        send_msg(chat_id, txt, reply_markup=markup)
        return

    if data == "cmd_models":
        handle_message({"chat": {"id": chat_id}, "from": {"id": user_id}, "text": "/model"})
        return

    if data == "cmd_consumo":
        handle_message({"chat": {"id": chat_id}, "from": {"id": user_id}, "text": "/consumo"})
        return

    if data == "cmd_status":
        send_msg(chat_id, get_system_status(), reply_markup=get_persistent_reply_keyboard())
        return

    if data == "cmd_screenshot":
        handle_message({"chat": {"id": chat_id}, "from": {"id": user_id}, "text": "/screenshot"})
        return

    if data == "cmd_ports":
        handle_message({"chat": {"id": chat_id}, "from": {"id": user_id}, "text": "/ports"})
        return

    if data == "cmd_history":
        handle_message({"chat": {"id": chat_id}, "from": {"id": user_id}, "text": "/history"})
        return

    if data == "cmd_new":
        SESSION_RESET_FLAG = True
        send_msg(
            chat_id,
            "🧹 <b>Sesión reiniciada con éxito.</b>\n"
            "<i>El contexto de memoria anterior se limpió al 0%. Tu próxima orden comenzará una conversación limpia y ultra ágil.</i>",
            reply_markup=get_persistent_reply_keyboard()
        )
        return

    if data == "cmd_restart":
        handle_message({"chat": {"id": chat_id}, "from": {"id": user_id}, "text": "/restart"})
        return

    if data == "trigger_new_session":
        SESSION_RESET_FLAG = True
        send_msg(
            chat_id,
            "🧹 <b>Sesión reiniciada con éxito.</b>\n"
            "<i>El contexto de memoria anterior se limpió al 0%. Tu próxima orden comenzará una conversación limpia y ultra ágil.</i>"
        )
        return

    if data == "pause_iris":
        IS_PAUSED = True
        save_state({"is_paused": True, "mute_notifications": True})
        send_msg(chat_id, "💤 <b>Iris puesta en pausa preventiva.</b> No consumirá recursos hasta que me digas <code>/wake</code>.")
        return

    if data == "cancel_active_task":
        with TASK_LOCK:
            proc = CURRENT_PROCESS
            is_running = ACTIVE_TASK_INFO["running"]
            ACTIVE_TASK_INFO["cancelled"] = True

        with QUEUE_LOCK:
            cleared_q = len(TASK_QUEUE)
            TASK_QUEUE.clear()

        if proc:
            try:
                kill_process_tree(proc)
            except Exception:
                pass
            extra_q = f" y vacié {cleared_q} tarea(s) en espera" if cleared_q > 0 else ""
            send_msg(chat_id, f"🛑 <b>Tarea cancelada{extra_q}.</b> Tu Mac ha quedado libre de inmediato.")
        elif is_running:
            send_msg(chat_id, "🛑 Tarea marcada como cancelada.")
        elif cleared_q > 0:
            send_msg(chat_id, f"🗑️ Se vació la cola ({cleared_q} tarea(s) canceladas).")
        else:
            send_msg(chat_id, "No había ninguna tarea en ejecución activa.")
        return

    if data.startswith("dequeue_task:"):
        task_id = data.split(":", 1)[1]
        removed_task = None
        with QUEUE_LOCK:
            for idx, item in enumerate(TASK_QUEUE):
                if item["id"] == task_id:
                    removed_task = TASK_QUEUE.pop(idx)
                    break
        if removed_task:
            rt_safe = removed_task["prompt"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            if len(rt_safe) > 80:
                rt_safe = rt_safe[:80] + "..."
            send_msg(chat_id, f"🗑️ <b>Tarea retirada de la cola:</b>\n┃ <i>«{rt_safe}»</i>")
        else:
            send_msg(chat_id, "ℹ️ La tarea ya no está en la cola (posiblemente ya comenzó o fue retirada).")
        return

    if data == "show_task_queue":
        with QUEUE_LOCK:
            with TASK_LOCK:
                curr_active = ACTIVE_TASK_INFO.get("prompt", "")
                is_running = ACTIVE_TASK_INFO["running"]

            if not is_running and not TASK_QUEUE:
                send_msg(chat_id, "ℹ️ La cola está actualmente vacía.")
                return

            lines = ["📋 <b>ESTADO DE LA COLA</b>\n"]
            if is_running:
                p_safe = curr_active.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                if len(p_safe) > 70:
                    p_safe = p_safe[:70] + "..."
                lines.append(f"⚙️ <b>En ejecución:</b>\n┃ <i>«{p_safe}»</i>\n")
            
            if TASK_QUEUE:
                lines.append(f"⏳ <b>En espera ({len(TASK_QUEUE)}):</b>")
                btns = []
                for idx, q_item in enumerate(TASK_QUEUE, 1):
                    qp = q_item["prompt"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    if len(qp) > 55:
                        qp = qp[:55] + "..."
                    lines.append(f"<b>#{idx}.</b> <i>«{qp}»</i>")
                    btns.append([{"text": f"❌ Cancelar #{idx}", "callback_data": f"dequeue_task:{q_item['id']}"}])
                reply_markup = {"inline_keyboard": btns}
            else:
                lines.append("<i>Sin tareas en cola.</i>")
                reply_markup = None

            send_msg(chat_id, "\n".join(lines), reply_markup=reply_markup)
        return

    if data.startswith("cancel_sched:"):
        try:
            sched_idx = int(data.split(":")[1])
            schedules = load_schedules()
            if 0 <= sched_idx < len(schedules):
                removed = schedules.pop(sched_idx)
                save_schedules(schedules)
                send_msg(chat_id, f"🗑️ <b>Tarea programada cancelada:</b>\n<i>«{removed.get('prompt', '')}»</i>")
            else:
                send_msg(chat_id, "La tarea ya no existe o ya fue ejecutada.")
        except Exception as e:
            send_msg(chat_id, f"Error cancelando tarea: {e}")
        return

    # Callbacks del Explorador Visual de Carpetas y Proyectos
    if data.startswith("nav_enter:"):
        dir_id = data.split(":", 1)[1]
        target = get_dir_nav_path(dir_id)
        msg_id = cb.get("message", {}).get("message_id")
        if target and os.path.isdir(target):
            txt, markup = render_folder_explorer(target, page=0)
            if msg_id:
                edit_msg_text(chat_id, msg_id, txt, reply_markup=markup)
            else:
                send_msg(chat_id, txt, reply_markup=markup)
        else:
            send_msg(chat_id, "⚠️ La carpeta ya no existe o no es accesible.")
        return

    if data.startswith("nav_up:"):
        dir_id = data.split(":", 1)[1]
        target = get_dir_nav_path(dir_id)
        msg_id = cb.get("message", {}).get("message_id")
        if target and os.path.isdir(target):
            txt, markup = render_folder_explorer(target, page=0)
            if msg_id:
                edit_msg_text(chat_id, msg_id, txt, reply_markup=markup)
            else:
                send_msg(chat_id, txt, reply_markup=markup)
        return

    if data == "nav_docs":
        docs_p = os.path.realpath(os.path.expanduser("~/Documents"))
        msg_id = cb.get("message", {}).get("message_id")
        txt, markup = render_folder_explorer(docs_p, page=0)
        if msg_id:
            edit_msg_text(chat_id, msg_id, txt, reply_markup=markup)
        else:
            send_msg(chat_id, txt, reply_markup=markup)
        return

    if data.startswith("nav_refresh:"):
        dir_id = data.split(":", 1)[1]
        target = get_dir_nav_path(dir_id) or CURRENT_CWD
        msg_id = cb.get("message", {}).get("message_id")
        txt, markup = render_folder_explorer(target, page=0)
        if msg_id:
            edit_msg_text(chat_id, msg_id, txt, reply_markup=markup)
        return

    if data.startswith("nav_page:"):
        parts = data.split(":")
        dir_id = parts[1]
        page = int(parts[2]) if len(parts) > 2 else 0
        target = get_dir_nav_path(dir_id) or CURRENT_CWD
        msg_id = cb.get("message", {}).get("message_id")
        txt, markup = render_folder_explorer(target, page=page)
        if msg_id:
            edit_msg_text(chat_id, msg_id, txt, reply_markup=markup)
        return

    if data == "nav_quick_git":
        msg_id = cb.get("message", {}).get("message_id")
        txt, markup = render_quick_git_view(CURRENT_CWD)
        if msg_id:
            edit_msg_text(chat_id, msg_id, txt, reply_markup=markup)
        else:
            send_msg(chat_id, txt, reply_markup=markup)
        return

    if data.startswith("nav_set:"):
        dir_id = data.split(":", 1)[1]
        target = get_dir_nav_path(dir_id)
        if target and os.path.isdir(target):
            CURRENT_CWD = target
            cfg = load_config()
            cfg["default_cwd"] = target
            try:
                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, indent=2)
            except Exception:
                pass
            p_name = os.path.basename(target) or target
            git_badge = get_git_status_badge(target) or ""
            git_line = f"\n{git_badge}\n" if git_badge else ""
            success_msg = (
                f"✅ <b>Espacio de trabajo cambiado:</b>\n"
                f"📦 <b>{p_name}</b>\n"
                f"<code>{target}</code>\n"
                f"{git_line}\n"
                f"👉 <i>Todas tus siguientes notas de voz, fotos e instrucciones se ejecutarán en este proyecto.</i>"
            )
            msg_id = cb.get("message", {}).get("message_id")
            if msg_id:
                txt, markup = render_folder_explorer(target, page=0)
                edit_msg_text(chat_id, msg_id, txt, reply_markup=markup)
                send_msg(chat_id, success_msg)
            else:
                send_msg(chat_id, success_msg)
        else:
            send_msg(chat_id, "⚠️ No se pudo acceder a la carpeta seleccionada.")
        return

    if data.startswith("set_ws:"):
        ws_map = globals().get("_WS_MAP", {})
        target_ws = ws_map.get(data)
        if target_ws and os.path.isdir(target_ws):
            CURRENT_CWD = target_ws
            cfg = load_config()
            cfg["default_cwd"] = target_ws
            try:
                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, indent=2)
            except Exception:
                pass
            p_name = os.path.basename(target_ws) or target_ws
            send_msg(
                chat_id,
                f"✅ <b>Espacio de trabajo cambiado:</b>\n"
                f"📦 <b>{p_name}</b>\n"
                f"<code>{target_ws}</code>\n\n"
                f"👉 <i>Las siguientes instrucciones se ejecutarán en este proyecto.</i>"
            )
        else:
            send_msg(chat_id, "No se pudo cambiar al espacio de trabajo seleccionado. Intenta con <code>/ws</code> nuevamente.")
        return

    if data.startswith("set_voice:"):
        new_voice = data.split(":", 1)[1]
        cfg = load_config()
        cfg["voice_name"] = new_voice
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        send_msg(chat_id, f"✅ <b>Voz local actualizada:</b> <code>{new_voice}</code>")
        ogg = synthesize_speech_to_voice(f"Hola, a partir de ahora hablaré con la voz de {new_voice}.", voice=new_voice)
        if ogg and os.path.exists(ogg):
            send_voice(chat_id, ogg)
            try:
                os.remove(ogg)
            except Exception:
                pass
        return

    if data.startswith("set_model:"):
        new_model = data.split(":", 1)[1]
        cfg = load_config()
        cfg["model"] = new_model
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        found = find_model_by_query(new_model)
        m_name = found["name"] if found else new_model
        m_emoji = found["emoji"] if found else "🤖"
        m_desc = found["desc"] if found else ""
        send_msg(
            chat_id,
            f"✅ <b>Modelo predeterminado actualizado:</b>\n"
            f"{m_emoji} <b>{m_name}</b> (<code>{new_model}</code>)\n"
            f"<i>{m_desc}</i>\n\n"
            f"👉 <i>A partir de ahora Antigravity responderá usando este motor.</i>"
        )
        return

    if data.startswith("dialog_choice:"):
        try:
            idx = int(data.split(":")[1])
            opt_name = f"Opción {idx+1}"
            if os.path.exists(PENDING_DIALOG_PATH):
                with open(PENDING_DIALOG_PATH, "r", encoding="utf-8") as df:
                    d_info = json.load(df)
                options = d_info.get("options", [])
                if 0 <= idx < len(options):
                    opt_name = options[idx]
                d_info["resolved"] = True
                d_info["selected_option"] = opt_name
                with open(PENDING_DIALOG_PATH, "w", encoding="utf-8") as df:
                    json.dump(d_info, df)

            send_msg(chat_id, f"✅ <b>Elegiste:</b> <i>«{opt_name}»</i>\nTransmitiendo tu selección a Antigravity en el Mac...")
            dispatch_antigravity_task(chat_id, f"El usuario eligió la opción: {opt_name}", continue_session=True)
        except Exception as e:
            send_msg(chat_id, f"Hubo un detalle procesando la selección: {e}")

    # Callback handlers para Historial de Tareas (/history) y Rollback
    if data.startswith("rerun_task:"):
        try:
            idx = int(data.split(":")[1])
            hist = load_history()
            if 0 <= idx < len(hist):
                item = hist[idx]
                target_prompt = item.get("prompt", "")
                target_cwd = item.get("cwd", CURRENT_CWD)
                CURRENT_CWD = target_cwd
                send_msg(
                    chat_id,
                    f"🔄 <b>Re-ejecutando tarea #{idx+1} en <code>{os.path.basename(target_cwd)}</code>:</b>\n"
                    f"<i>«{target_prompt}»</i>"
                )
                dispatch_antigravity_task(chat_id, target_prompt, continue_session=False)
            else:
                send_msg(chat_id, "⚠️ No se encontró la tarea seleccionada en el historial.")
        except Exception as e:
            send_msg(chat_id, f"Error re-ejecutando tarea: {e}")
        return

    if data.startswith("rollback_task:"):
        try:
            idx = int(data.split(":")[1])
            hist = load_history()
            if 0 <= idx < len(hist):
                item = hist[idx]
                target_cwd = item.get("cwd", CURRENT_CWD)
                status_out = subprocess.check_output("git status --porcelain", cwd=target_cwd, shell=True, text=True).strip()
                if not status_out:
                    send_msg(chat_id, f"ℹ️ <b>Sin cambios para revertir:</b>\nEl proyecto en <code>{os.path.basename(target_cwd)}</code> está completamente limpio.")
                    return

                btn_confirm = {"inline_keyboard": [
                    [{"text": f"⚠️ Confirmar Rollback en {os.path.basename(target_cwd)}", "callback_data": f"confirm_rollback_cwd:{target_cwd}"}],
                    [{"text": "❌ Cancelar", "callback_data": "cancel_active_task"}]
                ]}
                files_preview = "\n".join([f"• <code>{line}</code>" for line in status_out.splitlines()[:6]])
                send_msg(
                    chat_id,
                    f"⚠️ <b>¿Revertir modificaciones de la tarea #{idx+1}?</b>\n"
                    f"📍 Carpeta: <code>{os.path.basename(target_cwd)}</code>\n\n"
                    f"<b>Archivos con cambios pendientes:</b>\n{files_preview}\n\n"
                    f"<i>Se ejecutarán <code>git restore .</code> y <code>git clean -fd</code> para devolver el proyecto a su último commit limpio.</i>",
                    reply_markup=btn_confirm
                )
            else:
                send_msg(chat_id, "⚠️ No se encontró la tarea en el historial.")
        except Exception as e:
            send_msg(chat_id, f"Error preparando rollback: {e}")
        return

    if data == "confirm_git_rollback":
        try:
            out_res = subprocess.check_output("git restore . && git clean -fd", cwd=CURRENT_CWD, shell=True, text=True)
            send_msg(
                chat_id,
                f"✅ <b>Rollback ejecutado con éxito:</b>\n"
                f"Se han descartado todas las modificaciones no guardadas en <code>{os.path.basename(CURRENT_CWD)}</code>."
            )
        except Exception as e:
            send_msg(chat_id, f"⚠️ Error ejecutando git restore: {e}")
        return

    if data.startswith("confirm_rollback_cwd:"):
        target_cwd = data.split(":", 1)[1]
        try:
            subprocess.check_output("git restore . && git clean -fd", cwd=target_cwd, shell=True, text=True)
            send_msg(
                chat_id,
                f"✅ <b>Rollback completado con éxito:</b>\n"
                f"El directorio <code>{os.path.basename(target_cwd)}</code> ha sido restaurado a su estado original antes de la tarea."
            )
        except Exception as e:
            send_msg(chat_id, f"⚠️ Error ejecutando rollback en {target_cwd}: {e}")
        return

    # Callbacks del Sistema de Planificación (/plan) y Aprobación de Ciberseguridad (/cerberus)
    if data.startswith("plan_exec:"):
        plan_id = data.split(":", 1)[1]
        p_data = get_plan(plan_id)
        if not p_data or p_data.get("status") == "cancelled":
            send_msg(chat_id, "⚠️ Este plan o dictamen ya no está disponible o fue cancelado.")
            return

        msg_id = cb.get("message", {}).get("message_id")
        p_cwd = p_data.get("cwd", CURRENT_CWD)
        if os.path.isdir(p_cwd):
            CURRENT_CWD = p_cwd
        ws_name = os.path.basename(CURRENT_CWD) or CURRENT_CWD
        p_data["status"] = "executed"
        save_plan(plan_id, p_data)

        is_cerb = bool(p_data.get("is_cerberus"))
        cerb_rev = p_data.get("cerberus_review", "")

        if is_cerb:
            approved_card = (
                f"🛡️ <b>Blindaje Cerberus #{plan_id} Aprobado</b>\n"
                f"📍 <code>{ws_name}</code>\n\n"
                f"👩‍💻 <b>Iris (Al mando):</b> <i>«¡Sistemas al cien\1 Tomo el dictamen y las recomendaciones de ciberseguridad de Cerberus y las aplico en tus archivos ahora mismo...»</i>"
            )
            tts_txt = "¡Sistemas al cien\1 Tomo las recomendaciones de ciberseguridad de Cerberus y las aplico en tus archivos ahora mismo."
            exec_prompt = (
                f"APLICA EN EL CÓDIGO LAS SIGUIENTES RECOMENDACIONES Y MEDIDAS DE CIBERSEGURIDAD EMITIDAS POR CERBERUS Y APROBADAS POR EL USUARIO:\n\n"
                f"Objetivo / Alcance: {p_data.get('task', '')}\n\n"
                f"Dictamen y Recomendaciones de Cerberus:\n{p_data.get('plan_text', '')}\n\n"
                f"Aplica todas las correcciones y blindajes recomendados en los archivos del proyecto, verifica que no haya errores de sintaxis y entrega un resumen claro."
            )
        elif cerb_rev:
            approved_card = (
                f"🚀🛡️ <b>Plan #{plan_id} Aprobado (Dante + Blindaje Cerberus)</b>\n"
                f"📍 <code>{ws_name}</code>\n\n"
                f"👩‍💻 <b>Iris (Al mando):</b> <i>«¡Sistemas al cien\1 Ejecuto el plan de Dante aplicando además las recomendaciones de ciberseguridad de Cerberus...»</i>"
            )
            tts_txt = "¡Sistemas al cien\1 Ejecuto el plan de Dante aplicando además el blindaje de ciberseguridad de Cerberus."
            exec_prompt = (
                f"EJECUTA EN EL CÓDIGO EL SIGUIENTE PLAN DE DANTE JUNTO CON LAS RECOMENDACIONES DE CIBERSEGURIDAD DE CERBERUS (APROBADOS POR EL USUARIO):\n\n"
                f"Objetivo original: {p_data.get('task', '')}\n\n"
                f"Plan arquitectónico de Dante:\n{p_data.get('plan_text', '')}\n\n"
                f"Dictamen y Recomendaciones de Ciberseguridad de Cerberus (obligatorio aplicar):\n{cerb_rev}\n\n"
                f"Aplica todos los cambios y blindajes en los archivos, asegúrate de que no haya errores de sintaxis y reporta un resumen claro de lo modificado."
            )
        else:
            approved_card = (
                f"🚀 <b>Plan #{plan_id} Aprobado</b>\n"
                f"📍 <code>{ws_name}</code>\n\n"
                f"👩‍💻 <b>Iris (Al mando):</b> <i>«¡Sistemas al cien\1 Tomo los planos de Dante y entro en acción en tus archivos ahora mismo...»</i>"
            )
            tts_txt = "¡Sistemas al cien\1 Tomo los planos de Dante y entro en acción en tus archivos ahora mismo."
            exec_prompt = (
                f"EJECUTA EN EL CÓDIGO EL SIGUIENTE PLAN DISEÑADO POR DANTE Y APROBADO POR EL USUARIO:\n\n"
                f"Objetivo original: {p_data.get('task', '')}\n\n"
                f"Plan acordado de Dante:\n{p_data.get('plan_text', '')}\n\n"
                f"Aplica todos los cambios en los archivos, asegúrate de que no haya errores de sintaxis y reporta un resumen claro de lo modificado."
            )

        if msg_id:
            try:
                edit_msg_text(chat_id, msg_id, approved_card, reply_markup=None)
            except Exception:
                pass

        if VOICE_MODE:
            cfg_v = load_config()
            iris_v = cfg_v.get("voice_name", VOICE_NAME)
            ogg = synthesize_speech_to_voice(tts_txt, voice=iris_v)
            if ogg and os.path.exists(ogg):
                send_voice(chat_id, ogg)
                try:
                    os.remove(ogg)
                except Exception:
                    pass

        dispatch_antigravity_task(chat_id, exec_prompt, continue_session=True)
        return

    if data.startswith("plan_cc:"):
        plan_id = data.split(":", 1)[1]
        p_data = get_plan(plan_id)
        if not p_data or p_data.get("status") == "cancelled":
            send_msg(chat_id, "⚠️ Este plan o dictamen ya no está disponible o fue cancelado.")
            return

        msg_id = cb.get("message", {}).get("message_id")
        p_cwd = p_data.get("cwd", CURRENT_CWD)
        if os.path.isdir(p_cwd):
            CURRENT_CWD = p_cwd
        ws_name = os.path.basename(CURRENT_CWD) or CURRENT_CWD
        p_data["status"] = "executed"
        save_plan(plan_id, p_data)

        is_cerb = bool(p_data.get("is_cerberus"))
        cerb_rev = p_data.get("cerberus_review", "")
        cerb_block = f"\n\nRecomendaciones de Ciberseguridad de Cerberus (aplicar obligatoriamente):\n{cerb_rev}" if cerb_rev else ""

        approved_card = (
            f"🟠 <b>{'Blindaje Cerberus' if is_cerb else 'Plan'} #{plan_id} Aprobado para Claude Code</b>\n"
            f"📍 <code>{ws_name}</code> • 🧠 <code>Claude Code · Sonnet 3.7</code>\n\n"
            f"👩‍💻 <b>Iris (Al mando):</b> <i>«¡Entendido! Transmito las instrucciones a tu extensión de Claude Code para que ejecute los cambios con Sonnet en tu Mac...»</i>"
        )
        if msg_id:
            try:
                edit_msg_text(chat_id, msg_id, approved_card, reply_markup=None)
            except Exception:
                pass

        if VOICE_MODE:
            cfg_v = load_config()
            iris_v = cfg_v.get("voice_name", VOICE_NAME)
            ogg = synthesize_speech_to_voice("¡Entendido! Transmito las instrucciones a tu extensión de Claude Code para que ejecute los cambios con Sonnet en tu Mac.", voice=iris_v)
            if ogg and os.path.exists(ogg):
                send_voice(chat_id, ogg)
                try:
                    os.remove(ogg)
                except Exception:
                    pass

        exec_prompt = (
            f"EJECUTA EN EL CÓDIGO EL SIGUIENTE {'DICTAMEN DE CIBERSEGURIDAD DE CERBERUS' if is_cerb else 'PLAN DISEÑADO POR DANTE'} Y APROBADO POR EL USUARIO:\n\n"
            f"Objetivo original: {p_data.get('task', '')}\n\n"
            f"Contenido aprobado:\n{p_data.get('plan_text', '')}{cerb_block}\n\n"
            f"Aplica todos los cambios necesarios en los archivos del proyecto, verifica que no haya errores de compilación o sintaxis y entrega un resumen claro."
        )
        dispatch_antigravity_task(chat_id, exec_prompt, continue_session=True, engine="claude_code")
        return

    if data.startswith("plan_pro:"):
        plan_id = data.split(":", 1)[1]
        p_data = get_plan(plan_id)
        if not p_data or p_data.get("status") == "cancelled":
            send_msg(chat_id, "⚠️ Este plan o dictamen ya no está disponible o fue cancelado.")
            return

        msg_id = cb.get("message", {}).get("message_id")
        p_cwd = p_data.get("cwd", CURRENT_CWD)
        if os.path.isdir(p_cwd):
            CURRENT_CWD = p_cwd
        ws_name = os.path.basename(CURRENT_CWD) or CURRENT_CWD
        p_data["status"] = "executed"
        save_plan(plan_id, p_data)

        is_cerb = bool(p_data.get("is_cerberus"))
        cerb_rev = p_data.get("cerberus_review", "")
        cerb_block = f"\n\nRecomendaciones de Ciberseguridad de Cerberus (aplicar obligatoriamente):\n{cerb_rev}" if cerb_rev else ""

        approved_card = (
            f"🧠 <b>{'Blindaje Cerberus' if is_cerb else 'Plan'} #{plan_id} Aprobado con Argon 4 High</b>\n"
            f"📍 <code>{ws_name}</code> • 💎 <code>gemini-4-argon-high</code>\n\n"
            f"👩‍💻 <b>Iris (Al mando):</b> <i>«¡Sistemas a máxima potencia\1 Ejecuto con Argon 4 High ahora mismo...»</i>"
        )
        if msg_id:
            try:
                edit_msg_text(chat_id, msg_id, approved_card, reply_markup=None)
            except Exception:
                pass

        if VOICE_MODE:
            cfg_v = load_config()
            iris_v = cfg_v.get("voice_name", VOICE_NAME)
            ogg = synthesize_speech_to_voice("¡Sistemas a máxima potencia\1 Ejecuto con Argon 4 High ahora mismo.", voice=iris_v)
            if ogg and os.path.exists(ogg):
                send_voice(chat_id, ogg)
                try:
                    os.remove(ogg)
                except Exception:
                    pass

        exec_prompt = (
            f"EJECUTA EN EL CÓDIGO EL SIGUIENTE {'DICTAMEN DE CIBERSEGURIDAD DE CERBERUS' if is_cerb else 'PLAN DISEÑADO POR DANTE'} Y APROBADO POR EL USUARIO:\n\n"
            f"Objetivo original: {p_data.get('task', '')}\n\n"
            f"Contenido aprobado:\n{p_data.get('plan_text', '')}{cerb_block}\n\n"
            f"Aplica todos los cambios en los archivos, asegúrate de que no haya errores de sintaxis y reporta un resumen claro de lo modificado."
        )
        dispatch_antigravity_task(chat_id, exec_prompt, continue_session=True, model_override="gemini-4-argon-high")
        return

    if data.startswith("plan_cancel:"):
        plan_id = data.split(":", 1)[1]
        p_data = get_plan(plan_id)
        if p_data:
            p_data["status"] = "cancelled"
            save_plan(plan_id, p_data)

        msg_id = cb.get("message", {}).get("message_id")
        ws_name = os.path.basename(CURRENT_CWD) or CURRENT_CWD
        cancel_card = (
            f"❌ <b>Plan #{plan_id} descartado</b>\n"
            f"📍 <code>{ws_name}</code>\n\n"
            f"<i>No se modificó ningún archivo del proyecto. Puedes formular una nueva instrucción cuando desees.</i>"
        )
        if msg_id:
            try:
                edit_msg_text(chat_id, msg_id, cancel_card, reply_markup=None)
            except Exception:
                send_msg(chat_id, cancel_card)
        else:
            send_msg(chat_id, cancel_card)
        return

    # Callbacks del Sistema de Code Review (/review)
    if data == "review_diff":
        try:
            diff_out = subprocess.check_output(["git", "diff"], cwd=CURRENT_CWD, text=True, stderr=subprocess.DEVNULL).strip()
            if not diff_out:
                diff_out = subprocess.check_output(["git", "show", "HEAD"], cwd=CURRENT_CWD, text=True, stderr=subprocess.DEVNULL).strip()
            if not diff_out:
                send_msg(chat_id, "ℹ️ No hay modificaciones en el diff.")
                return

            ws_name = os.path.basename(CURRENT_CWD) or CURRENT_CWD
            if len(diff_out) <= 3200:
                safe_d = diff_out.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                send_msg(chat_id, f"📄 <b>Git Diff de <code>{ws_name}</code>:</b>\n<pre><code class=\"language-diff\">{safe_d}</code></pre>")
            else:
                diff_tmp = os.path.join(tempfile.gettempdir(), f"{ws_name}_cambios.diff")
                with open(diff_tmp, "w", encoding="utf-8") as f:
                    f.write(diff_out)
                send_document(chat_id, f"📄 Diff completo de {ws_name}", diff_tmp)
        except Exception as e:
            send_msg(chat_id, f"⚠️ Error obteniendo diff: {e}")
        return

    if data == "review_last_diff":
        try:
            diff_out = subprocess.check_output(["git", "show", "HEAD"], cwd=CURRENT_CWD, text=True, stderr=subprocess.DEVNULL).strip()
            ws_name = os.path.basename(CURRENT_CWD) or CURRENT_CWD
            if not diff_out:
                send_msg(chat_id, "ℹ️ No hay información del commit en HEAD.")
                return

            if len(diff_out) <= 3200:
                safe_d = diff_out.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                send_msg(chat_id, f"📜 <b>Diff del Último Commit en <code>{ws_name}</code>:</b>\n<pre><code class=\"language-diff\">{safe_d}</code></pre>")
            else:
                diff_tmp = os.path.join(tempfile.gettempdir(), f"{ws_name}_ultimo_commit.diff")
                with open(diff_tmp, "w", encoding="utf-8") as f:
                    f.write(diff_out)
                send_document(chat_id, f"📜 Diff de último commit en {ws_name}", diff_tmp)
        except Exception as e:
            send_msg(chat_id, f"⚠️ Error obteniendo último commit: {e}")
        return

    if data == "review_status":
        try:
            st_out = subprocess.check_output(["git", "status"], cwd=CURRENT_CWD, text=True, stderr=subprocess.DEVNULL).strip()
            ws_name = os.path.basename(CURRENT_CWD) or CURRENT_CWD
            safe_st = st_out.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            send_msg(chat_id, f"🌿 <b>Git Status (<code>{ws_name}</code>):</b>\n<pre><code>{safe_st}</code></pre>")
        except Exception as e:
            send_msg(chat_id, f"⚠️ Error: {e}")
        return

    if data == "review_test":
        cmd = get_project_test_command(CURRENT_CWD)
        ws_name = os.path.basename(CURRENT_CWD) or CURRENT_CWD
        if not cmd:
            send_msg(chat_id, f"ℹ️ No detecté un runner de tests estándar en <code>{ws_name}</code> (npm test, flutter test, cargo test, pytest).")
            return

        send_msg(chat_id, f"🧪 <b>Ejecutando pruebas en <code>{ws_name}</code>:</b>\n<code>{cmd}</code>...")
        code, out = run_shell(cmd)
        clean_out = out[-2500:] if len(out) > 2500 else out
        safe_out = clean_out.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        badge = "✅ <b>Pruebas superadas con éxito</b>" if code == 0 else f"❌ <b>Pruebas con errores (código {code})</b>"
        send_msg(chat_id, f"{badge}:\n<pre><code>{safe_out}</code></pre>")
        return

    if data == "review_commit":
        ws_name = os.path.basename(CURRENT_CWD) or CURRENT_CWD
        try:
            porcelain = subprocess.check_output(["git", "status", "--porcelain"], cwd=CURRENT_CWD, text=True, stderr=subprocess.DEVNULL).strip()
            if not porcelain:
                send_msg(chat_id, "ℹ️ No hay cambios pendientes para hacer commit.")
                return

            changed_files = [line[3:].strip() for line in porcelain.splitlines() if len(line) > 3]
            sample = ", ".join([os.path.basename(f) for f in changed_files[:3]])
            extra = f" (+{len(changed_files)-3} files)" if len(changed_files) > 3 else ""
            c_msg = f"feat({ws_name}): update {sample}{extra}"

            commit_id = f"c{int(time.time()*1000) % 10000000}"
            SUGGESTED_COMMITS[commit_id] = c_msg

            msg = (
                f"💾 <b>Crear Git Commit en <code>{ws_name}</code></b>\n\n"
                f"📝 <b>Mensaje sugerido:</b>\n"
                f"<code>{c_msg}</code>\n\n"
                f"¿Deseas confirmar y guardar este commit en el repositorio local?"
            )
            buttons = [
                [{"text": "✅ Confirmar y Guardar Commit", "callback_data": f"review_do_commit:{commit_id}"}],
                [{"text": "❌ Cancelar", "callback_data": "review_cancel_commit"}]
            ]
            send_msg(chat_id, msg, reply_markup={"inline_keyboard": buttons})
        except Exception as e:
            send_msg(chat_id, f"⚠️ Error preparando el commit: {e}")
        return

    if data.startswith("review_do_commit:"):
        commit_id = data.split(":", 1)[1]
        c_msg = SUGGESTED_COMMITS.get(commit_id, "chore: update codebase")
        safe_msg = c_msg.replace("'", "'\\''")
        code, out = run_shell(f"git add -A && git commit -m '{safe_msg}'")
        msg_id = cb.get("message", {}).get("message_id")
        if code == 0:
            res_txt = f"✅ <b>Commit guardado exitosamente:</b>\n<code>{out}</code>"
        else:
            res_txt = f"⚠️ <b>Detalle al guardar commit:</b>\n<code>{out}</code>"

        if msg_id:
            try:
                edit_msg_text(chat_id, msg_id, res_txt, reply_markup=None)
            except Exception:
                send_msg(chat_id, res_txt)
        else:
            send_msg(chat_id, res_txt)
        return

    if data == "review_cancel_commit":
        msg_id = cb.get("message", {}).get("message_id")
        txt = "❌ Operación de commit cancelada. No se confirmó ningún cambio."
        if msg_id:
            try:
                edit_msg_text(chat_id, msg_id, txt, reply_markup=None)
            except Exception:
                send_msg(chat_id, txt)
        else:
            send_msg(chat_id, txt)
        return

def start_hardware_watchdog(target_chat_ids):
    """
    Guardián de Hardware y Conectividad en segundo plano (Watchdog):
    - Re-asegura cada 30s que caffeinate y pmset (disablesleep=1, sleep=0) sigan activos incluso al cambiar entre batería y cargador.
    - Monitorea batería crítica (<15% sin cargador conectado).
    - Monitorea espacio en disco residual (<4 GB).
    """
    def _watchdog_loop():
        last_battery_alert = 0
        notified_10_pct = False
        notified_5_pct = False
        last_disk_alert = 0

        while True:
            time.sleep(30) # Chequeo cada 30 segundos
            now = time.time()

            # 0. Auto-Reaseguramiento de Anti-Reposo (Batería + Corriente + Tapa Cerrada)
            try:
                if AUTO_CAFFEINATE and not MANUAL_DECAF:
                    if not is_anti_sleep_active():
                        enable_anti_sleep(quiet=True)
                    elif sys.platform == "darwin":
                        pm_out = subprocess.check_output(["pmset", "-g"], text=True, timeout=3)
                        if "SleepDisabled\t\t0" in pm_out or re.search(r'\bsleep\s+[1-9]\d*', pm_out):
                            _run_pmset_disablesleep(1, quiet=True)
            except Exception:
                pass

            # 1. Chequeo de Batería (alerta inmediata al llegar a <=10% y alerta crítica a <=5%)
            try:
                out = subprocess.check_output(["/usr/bin/pmset", "-g", "batt"], text=True, timeout=4)
                m_pct = re.search(r'(\d+)%', out)
                out_lower = out.lower()
                # ¡Ojo!: "discharging" contiene la subcadena "charging", por eso verificamos explícitamente "discharging" / "battery power"
                is_on_battery = ("battery power" in out_lower) or ("discharging" in out_lower)
                if not is_on_battery:
                    notified_10_pct = False
                    notified_5_pct = False
                elif m_pct:
                    pct = int(m_pct.group(1))
                    should_alert_5 = (pct <= 5 and not notified_5_pct)
                    should_alert_10 = (pct <= 10 and (not notified_10_pct or (now - last_battery_alert > 600)))
                    if should_alert_5 or should_alert_10:
                        last_battery_alert = now
                        notified_10_pct = True
                        if pct <= 5:
                            notified_5_pct = True
                        m_rem = re.search(r'(\d+:\d+)\s+remaining', out_lower)
                        rem_txt = f" (~{m_rem.group(1)} restantes)" if m_rem else ""
                        urg_hdr = "🚨 <b>¡BATERÍA EN NIVEL CRÍTICO EXTREMO" if pct <= 5 else "🔋 <b>¡AVISO DE BATERÍA AL"
                        alert_msg = (
                            f"{urg_hdr} ({pct}%)!</b>\n\n"
                            f"Tu MacBook está desconectado del cargador y acaba de bajar a <b>{pct}%</b> de batería{rem_txt}.\n"
                            f"Conéctalo a la corriente para que no se apague."
                        )
                        btn = {"inline_keyboard": [
                            [{"text": "🛑 Detener Tarea Activa", "callback_data": "cancel_active_task"}],
                            [{"text": "🌙 Permitir Reposo del Mac (/decaf)", "callback_data": "pause_iris"}]
                        ]}
                        for cid in target_chat_ids:
                            send_msg(cid, alert_msg, reply_markup=btn)
                            try:
                                ogg_b = synthesize_speech_to_voice(
                                    f"¡Atención\1 Tu MacBook está en {pct} por ciento de batería y desconectado del cargador. Conéctalo para que no se apague."
                                )
                                if ogg_b and os.path.exists(ogg_b):
                                    send_voice(cid, ogg_b)
                                    os.remove(ogg_b)
                            except Exception:
                                pass
            except Exception:
                pass

            # 2. Chequeo de Disco SSD residual
            try:
                check_drive = os.path.splitdrive(os.getcwd())[0] + os.sep if sys.platform == "win32" else "/"
                usage = shutil.disk_usage(check_drive)
                free_gb = usage.free / (1024**3)
                if free_gb < 4.0 and (now - last_disk_alert > 3600): # cada 1 hora
                    last_disk_alert = now
                    alert_disk = (
                        f"⚠️ <b>ALERTA DE DISCO: ESPACIO CASI LLENO ({round(free_gb, 1)} GB libres)</b>\n\n"
                        f"A tu disco principal le queda menos de 4 GB. Los builds de Antigravity o modelos locales podrían fallar por falta de espacio."
                    )
                    for cid in target_chat_ids:
                        send_msg(cid, alert_disk)
            except Exception:
                pass

            # 3. Auto-Mantenimiento y Limpieza Silenciosa (descargas antiguas, temporales y rotación de log)
            try:
                if os.path.isdir(DOWNLOADS_DIR):
                    for fn in os.listdir(DOWNLOADS_DIR):
                        fp = os.path.join(DOWNLOADS_DIR, fn)
                        if os.path.isfile(fp) and (now - os.path.getmtime(fp) > 7 * 86400):
                            try:
                                os.remove(fp)
                            except Exception:
                                pass
                for b_dir in {SCRIPT_DIR, tempfile.gettempdir()}:
                    if os.path.isdir(b_dir):
                        for fn in os.listdir(b_dir):
                            if (fn.startswith("iris_native_req_") or fn.startswith("iris_native_res_")) and fn.endswith(".json"):
                                fp = os.path.join(b_dir, fn)
                                if os.path.isfile(fp) and (now - os.path.getmtime(fp) > 300):
                                    try:
                                        os.remove(fp)
                                    except Exception:
                                        pass
                log_p = os.path.join(SCRIPT_DIR, "daemon.log")
                if os.path.isfile(log_p) and os.path.getsize(log_p) > 5 * 1024 * 1024:
                    with open(log_p, "r", encoding="utf-8", errors="ignore") as lf:
                        tail_lines = lf.readlines()[-1500:]
                    with open(log_p, "w", encoding="utf-8") as lf:
                        lf.writelines(tail_lines)
            except Exception:
                pass

    t = threading.Thread(target=_watchdog_loop, daemon=True)
    t.start()

def start_native_exec_bridge():
    """
    Puente de ejecución nativa en segundo plano:
    Escucha solicitudes de iris_exec.py en SCRIPT_DIR y /tmp/iris_native_req_*.json y las ejecuta
    nativamente fuera del sandbox de Antigravity, eliminando cualquier cuadro de permiso en el Mac.
    """
    def _handle_single_req(b_dir, req_data):
        req_id = req_data.get("id", "")
        cmd = req_data.get("cmd", "")
        cwd = req_data.get("cwd") or CURRENT_CWD
        res_path = os.path.join(b_dir, f"iris_native_res_{req_id}.json")
        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                cwd=cwd if os.path.isdir(cwd) else CURRENT_CWD,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=110
            )
            code = proc.returncode
            output = proc.stdout or ""
        except Exception as e_cmd:
            code = 1
            output = f"Error en puente nativo: {e_cmd}"
        try:
            with open(res_path, "w", encoding="utf-8") as wf:
                json.dump({"code": code, "output": output}, wf)
        except Exception:
            pass

    def _bridge_loop():
        watch_dirs = list({SCRIPT_DIR, tempfile.gettempdir()})
        while True:
            try:
                for b_dir in watch_dirs:
                    if not os.path.isdir(b_dir):
                        continue
                    for fname in os.listdir(b_dir):
                        if fname.startswith("iris_native_req_") and fname.endswith(".json"):
                            req_path = os.path.join(b_dir, fname)
                            try:
                                with open(req_path, "r", encoding="utf-8") as rf:
                                    req_data = json.load(rf)
                                os.remove(req_path)
                            except Exception:
                                continue
                            threading.Thread(target=_handle_single_req, args=(b_dir, req_data), daemon=True).start()
            except Exception:
                pass
            time.sleep(0.2)

    t = threading.Thread(target=_bridge_loop, daemon=True)
    t.start()

_LOCK_FILE_HANDLE = None

def acquire_single_instance_lock():
    global _LOCK_FILE_HANDLE
    if sys.platform == "win32":
        return True
    try:
        import fcntl
        _LOCK_FILE_HANDLE = open(LOCK_FILE_PATH, "a+", encoding="utf-8")
        fcntl.flock(_LOCK_FILE_HANDLE.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _LOCK_FILE_HANDLE.seek(0)
        _LOCK_FILE_HANDLE.truncate()
        _LOCK_FILE_HANDLE.write(str(os.getpid()))
        _LOCK_FILE_HANDLE.flush()
        try:
            with open(os.path.join(SCRIPT_DIR, "iris.pid"), "w", encoding="utf-8") as pf:
                pf.write(str(os.getpid()))
        except Exception:
            pass
        return True
    except Exception:
        print("[LOCK] Otra instancia de iris_daemon.py ya está activa y escuchando. Saliendo para evitar respuestas duplicadas.")
        return False

def sync_global_gemini_rules():
    """Sincroniza de forma nativa ~/.gemini/GEMINI.md con la jerarquía del escuadrón (Iris al mando, Dante y Cerberus)."""
    try:
        global_md_path = os.path.expanduser("~/.gemini/GEMINI.md")
        os.makedirs(os.path.dirname(global_md_path), exist_ok=True)
        content = (
            "# Instrucciones Globales de Antigravity (Iris, Dante & Cerberus)\n\n"
            "## 1. Trato al Usuario\n"
            "- Dirígete al usuario con un trato directo, ágil, cercano y natural.\n\n"
            "## 2. Ecosistema Antigravity y Pronunciación\n"
            "- La plataforma de desarrollo y orquestación se llama **Antigravity**.\n"
            "- Pronunciación fonética y prosódica: la fuerza de voz recae en la primera «a» de gravity (`An-ti-GRA-vi-ty` o `Antigrávity`), nunca plana ni aguda al final.\n\n"
            "## 3. Escuadrón de Asistentes, Jerarquía de Mando y Personalidad (Iris al Mando)\n"
            "- **Iris** (**Mujer / Género Femenino · Comandante en Jefe y Líder Operativa**): Está **siempre al mando** de la estación y coordina a los demás agentes (voz `es-MX-DaliaNeural`). Recibe las órdenes del usuario, ejecuta código, controla el Mac y el navegador, y convoca a Dante o a Cerberus cuando el usuario lo solicita. Siempre se refiere a sí misma en femenino (*«lista»*, *«encargada»*, *«atenta»*, *«al mando»*) y a Dante y Cerberus en masculino.\n"
            "- **Dante** (**Hombre / Género Masculino · Arquitecto de Software**): Especialista senior en arquitectura y planeación técnica previa en modo solo lectura (voz `es-MX-JorgeNeural`, motor principal `gemini-4-argon-high` con respaldo en `claude-opus-5-5-high`). Siempre se refiere a sí mismo en masculino (*«listo»*, *«preparado»*, *«el arquitecto»*) y a Iris en femenino.\n"
            "- **Cerberus** (**Hombre / Género Masculino · Guardián de Ciberseguridad y Aprobación Defensiva**): Especialista en ciberseguridad esencial (credenciales expuestas, `.env`, OWASP Top 10 básico, inyecciones, permisos, puertos y dependencias) con voz latina natural y fluida (`es-CO-GonzaloNeural`). **Únicamente interviene cuando el usuario lo solicita explícitamente** para revisar, recomendar mejoras de seguridad y aprobar procesos de planeación (planes de Dante) o de ejecución (cambios de Iris).\n\n"
            "## 4. Ortografía Rigurosa en Español\n"
            "- Redactar todas las respuestas y explicaciones con ortografía impecable en español y uso estricto del 100% de las tildes ortográficas (agudas, graves, esdrújulas, sobresdrújulas y tildes diacríticas).\n"
        )
        with open(global_md_path, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception:
        pass
def sync_launchd_agent():
    """Sincroniza ~/Library/LaunchAgents/com.iris.daemon.plist (Aqua) y com.iris.daemon.bg.plist (Background pre-login) en macOS."""
    if sys.platform != "darwin":
        return
    try:
        uid = os.getuid()
        dst_dir = os.path.expanduser("~/Library/LaunchAgents")
        os.makedirs(dst_dir, exist_ok=True)

        # 1. Agente GUI (Aqua)
        src_plist = os.path.join(SCRIPT_DIR, "com.iris.daemon.plist")
        dst_plist = os.path.join(dst_dir, "com.iris.daemon.plist")
        if os.path.exists(src_plist):
            with open(src_plist, "r", encoding="utf-8") as sf:
                src_content = sf.read()
            dst_content = ""
            if os.path.exists(dst_plist):
                with open(dst_plist, "r", encoding="utf-8") as df:
                    dst_content = df.read()
            if src_content != dst_content:
                with open(dst_plist, "w", encoding="utf-8") as df:
                    df.write(src_content)
                subprocess.Popen(
                    ["/bin/bash", "-c", f'launchctl unload "{dst_plist}" 2>/dev/null; launchctl load -w "{dst_plist}" 2>/dev/null'],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True
                )

        # 2. Agente Background (user/<uid> — arranca en boot antes de desbloquear pantalla GUI)
        src_bg = os.path.join(SCRIPT_DIR, "com.iris.daemon.bg.plist")
        dst_bg = os.path.join(dst_dir, "com.iris.daemon.bg.plist")
        if os.path.exists(src_bg):
            with open(src_bg, "r", encoding="utf-8") as sf:
                bg_content = sf.read()
            dst_bg_content = ""
            if os.path.exists(dst_bg):
                with open(dst_bg, "r", encoding="utf-8") as df:
                    dst_bg_content = df.read()
            if bg_content != dst_bg_content:
                with open(dst_bg, "w", encoding="utf-8") as df:
                    df.write(bg_content)
                subprocess.Popen(
                    ["/bin/bash", "-c", f'launchctl bootout user/{uid}/com.iris.daemon.bg 2>/dev/null; launchctl bootstrap user/{uid} "{dst_bg}" 2>/dev/null'],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True
                )
    except Exception:
        pass

LAST_MAIN_LOOP_TS = time.time()
HEARTBEAT_FILE_PATH = os.path.join(SCRIPT_DIR, "iris.heartbeat")

def touch_daemon_heartbeat():
    """Actualiza el latido en disco para que iris_keepalive.sh detecte si el bucle principal se queda congelado."""
    global LAST_MAIN_LOOP_TS
    LAST_MAIN_LOOP_TS = time.time()
    try:
        with open(HEARTBEAT_FILE_PATH, "w", encoding="utf-8") as hf:
            hf.write(str(int(LAST_MAIN_LOOP_TS)))
    except Exception:
        pass

def start_loop_watchdog():
    """Hilo guardián interno: si getUpdates se queda colgado >90s tras un cambio de red o reinicio, fuerza auto-recarga."""
    def _watchdog():
        while True:
            time.sleep(15)
            with TASK_LOCK:
                busy = ACTIVE_TASK_INFO["running"]
            if busy:
                touch_daemon_heartbeat()
                continue
            elapsed = time.time() - LAST_MAIN_LOOP_TS
            if elapsed > 90:
                print(f"[WATCHDOG] Bucle getUpdates sin respuesta por {int(elapsed)}s; reiniciando proceso para reconexión limpia...", file=sys.stderr)
                os._exit(1)
    t = threading.Thread(target=_watchdog, daemon=True)
    t.start()

def get_macos_boottime_sec() -> int:
    if sys.platform != "darwin":
        return 0
    try:
        out = subprocess.check_output(["/usr/sbin/sysctl", "-n", "kern.boottime"], text=True, timeout=3)
        m = re.search(r"sec\s*=\s*(\d+)", out)
        return int(m.group(1)) if m else 0
    except Exception:
        return 0

def main():
    if not acquire_single_instance_lock():
        sys.exit(0)

    touch_daemon_heartbeat()
    start_loop_watchdog()
    sync_global_gemini_rules()
    sync_launchd_agent()

    pending_commit_path = os.path.join(SCRIPT_DIR, "pending_git_commit.json")
    if os.path.exists(pending_commit_path):
        try:
            with open(pending_commit_path, "r", encoding="utf-8") as pcf:
                pc_data = json.load(pcf)
            c_msg = pc_data.get("message", "feat(iris): stabilize daemon, personalities and docs")
            c_cwd = pc_data.get("cwd") or SCRIPT_DIR
            subprocess.run(["git", "add", "-A"], cwd=c_cwd, check=True, timeout=15)
            subprocess.run(["git", "commit", "-m", c_msg], cwd=c_cwd, check=True, timeout=15)
            os.remove(pending_commit_path)
        except Exception:
            pass

    os_name = "Mac" if sys.platform == "darwin" else ("PC Windows" if sys.platform == "win32" else "Equipo Linux")
    print(f"Iniciando Iris en tu {os_name} con soporte de cuadros de diálogo interactivos y formato enriquecido (IPv4 prioritario)...")

    save_state({"iris_task_running": False})

    # Iniciar Puente de Ejecución Nativa (Zero-Prompt Sandbox Bridge)
    start_native_exec_bridge()

    # Si quedó una tarjeta de progreso abierta de un reinicio previo, cerrarla limpiamente
    if os.path.exists(ACTIVE_CARD_PATH):
        try:
            with open(ACTIVE_CARD_PATH, "r", encoding="utf-8") as acf:
                ac_data = json.load(acf)
            os.remove(ACTIVE_CARD_PATH)
            ac_chat = ac_data.get("chat_id")
            ac_msg_id = ac_data.get("message_id")
            if ac_chat and ac_msg_id:
                rec_loc = format_location_line(ac_data.get("ws_name", ""), ac_data.get("m_info", ""))
                recovered_card = (
                    f"<b>{ac_data.get('action_label', '✨ Solicitud procesada')}</b>\n"
                    f"┃ <i>{ac_data.get('p_preview', '')}</i>\n\n"
                    f"{rec_loc}"
                    f"✅ <b>Servicio de Iris actualizado y listo en tu {os_name}.</b>"
                )
                edit_msg_text(ac_chat, ac_msg_id, recovered_card, reply_markup=None)
        except Exception:
            pass

    if AUTO_CAFFEINATE:
        try:
            enable_anti_sleep()
            print(f"[POWER] Auto-Anti-Sleep ACTIVO: tu {os_name} responderá incluso bloqueado o con pantalla apagada.")
        except Exception as e:
            print(f"[POWER] No se pudo activar auto-anti-sleep: {e}")

    saved_state = load_state()
    last_update_id = int(saved_state.get("last_update_id", 0) or 0)
    if last_update_id <= 0:
        updates = api_call("getUpdates", {"offset": -1, "timeout": 1})
        if updates and updates.get("result"):
            last_update_id = updates["result"][-1]["update_id"] + 1
            save_state({"last_update_id": last_update_id})

    print(f"Listo y escuchando para el usuario ({ALLOWED_USERS})")

    # Guardar instantánea de la versión verificada en línea para auto-rollback ante errores futuros
    try:
        shutil.copyfile(os.path.abspath(__file__), os.path.join(SCRIPT_DIR, "iris_daemon.last_good.py"))
    except Exception:
        pass

    # Registrar comandos oficiales en segundo plano para arranque instantáneo (<0.1s)
    threading.Thread(target=register_telegram_bot_commands, daemon=True).start()

    # Iniciar Guardián de Hardware (Watchdog de batería y disco)
    start_hardware_watchdog(list(ALLOWED_USERS))

    # Iniciar Motor de Tareas Programadas (Scheduler de fondo)
    start_scheduler_thread()

    # Notificar si el arranque proviene de un /restart solicitado desde el chat
    restart_flag = os.path.join(SCRIPT_DIR, "restart_flag.json")
    if os.path.exists(restart_flag):
        try:
            with open(restart_flag, "r", encoding="utf-8") as rf:
                rdata = json.load(rf)
            os.remove(restart_flag)
            r_chat = rdata.get("chat_id")
            if r_chat:
                send_msg(
                    r_chat,
                    "🟢 <b>¡Iris reiniciada y 100% operativa!</b>\n"
                    "<i>Memoria liberada, bucle restablecido y lista para recibir órdenes.</i>",
                    reply_markup=get_persistent_reply_keyboard()
                )
        except Exception:
            pass
    else:
        # Detectar si el Mac acaba de encenderse/reiniciarse (kern.boottime nuevo) y avisar apenas haya red
        boot_sec = get_macos_boottime_sec()
        last_boot_sec = int(saved_state.get("last_boot_sec", 0) or 0)
        if boot_sec > 0 and boot_sec != last_boot_sec:
            save_state({"last_boot_sec": boot_sec})
            if last_boot_sec > 0 and ALLOWED_USERS:
                def _notify_boot():
                    for _ in range(12):
                        res_b = send_msg(
                            list(ALLOWED_USERS)[0],
                            "🟢 <b>¡Tu Mac acaba de encenderse e Iris ya arrancó en segundo plano!</b>\n"
                            "<i>Servicio activo automáticamente (incluso con pantalla bloqueada). Lista para tus órdenes\1</i>",
                            reply_markup=get_persistent_reply_keyboard()
                        )
                        if res_b and res_b.get("ok"):
                            break
                        time.sleep(5)
                threading.Thread(target=_notify_boot, daemon=True).start()

    while True:
        try:
            touch_daemon_heartbeat()
            # Hot-Reload seguro si el archivo iris_daemon.py fue actualizado y no hay tareas en curso
            with TASK_LOCK:
                busy_now = ACTIVE_TASK_INFO["running"]
            if not busy_now and not TASK_QUEUE:
                if trigger_safe_hot_reload(chat_id=list(ALLOWED_USERS)[0] if ALLOWED_USERS else None):
                    sys.exit(0)

            res = api_call("getUpdates", {"offset": last_update_id, "timeout": 15})
            touch_daemon_heartbeat()
            if res and res.get("ok"):
                for update in res.get("result", []):
                    last_update_id = update["update_id"] + 1
                    save_state({"last_update_id": last_update_id})
                    if "callback_query" in update:
                        handle_callback_query(update["callback_query"])
                    elif "message" in update:
                        handle_message(update["message"])
            time.sleep(0.3)
        except KeyboardInterrupt:
            print("\nDetenido.")
            break
        except Exception as e:
            print(f"Error loop: {e}", file=sys.stderr)
            time.sleep(2)

if __name__ == "__main__":
    main()
