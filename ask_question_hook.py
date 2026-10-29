#!/usr/bin/env python3
"""
Hook to intercept ask_question dialogs and send interactive options to Telegram.
"""
import sys
import json
import os
import socket
import urllib.request
import subprocess
import time
import tempfile

# Forzar prioridad IPv4 para evitar blackholes IPv6 hacia api.telegram.org
_orig_getaddrinfo = socket.getaddrinfo
def _ipv4_priority_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    res = _orig_getaddrinfo(host, port, family, type, proto, flags)
    ipv4 = [r for r in res if r[0] == socket.AF_INET]
    return ipv4 + [r for r in res if r[0] != socket.AF_INET] if ipv4 else res
socket.getaddrinfo = _ipv4_priority_getaddrinfo

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
STATE_PATH = os.path.join(SCRIPT_DIR, "state.json")
PENDING_DIALOG_PATH = os.path.join(SCRIPT_DIR, "pending_dialog.json")

def take_screenshot():
    shot_path = os.path.join(tempfile.gettempdir(), "antigravity_dialog_shot.png")
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
        except Exception:
            pass
    else:
        try:
            subprocess.run(["import", "-window", "root", shot_path], check=False, timeout=10)
        except Exception:
            pass
    return shot_path

def is_user_actively_on_mac(idle_threshold_seconds=180):
    """
    Detecta si el usuario está físicamente presente frente al Mac interactuando:
    - La tapa no está cerrada (clamshell == No).
    - La pantalla no está apagada ni en reposo.
    - Ha utilizado teclado o ratón en los últimos idle_threshold_seconds (por defecto 3 min).
    """
    if sys.platform != "darwin":
        return False
    try:
        clamshell = subprocess.check_output(["ioreg", "-r", "-c", "IOPMrootDomain"], timeout=2).decode("utf-8", errors="ignore")
        if '"AppleClamshellState" = Yes' in clamshell:
            return False

        if '"DisplayAsleep" = Yes' in clamshell:
            return False

        raw_hid = subprocess.check_output(["ioreg", "-c", "IOHIDSystem"], timeout=2)
        parts = raw_hid.split(b"HIDIdleTime")
        if len(parts) > 1:
            idle_ns = int(parts[1].split(b"=")[1].split()[0])
            idle_s = idle_ns / 1_000_000_000.0
            if idle_s < idle_threshold_seconds:
                return True
    except Exception:
        pass
    return False


def main():
    if os.environ.get("IRIS_SUBPROCESS") == "1":
        print(json.dumps({
            "decision": "block",
            "reason": "Estás ejecutando desde el chatbot de Telegram sin pantalla interactiva. NO uses ask_question porque bloquea la ejecución en el Mac; ejecuta la solución directamente o haz la pregunta en tu respuesta de texto."
        }))
        return

    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config = json.load(f)
        token = config.get("bot_token")
        user_ids = config.get("allowed_user_ids", [])
    except Exception:
        token = None
        user_ids = []

    # Verificar estado de ejecución y modo reposo
    try:
        if os.path.exists(STATE_PATH):
            with open(STATE_PATH, "r", encoding="utf-8") as sf:
                state = json.load(sf)
                # Si la tarea corre desde el chatbot de Telegram (CLI sin UI), bloquear ask_question
                # para que el agente no congele la terminal esperando un clic en el Mac.
                if state.get("iris_task_running"):
                    print(json.dumps({
                        "decision": "block",
                        "reason": "Estás ejecutando desde el chatbot de Telegram sin pantalla interactiva. NO uses ask_question porque bloquea la ejecución en el Mac; ejecuta la solución directamente o haz la pregunta en tu respuesta de texto."
                    }))
                    return

                # Si el Modo Reposo está activo (Reposo: ON), no enviar notificaciones a Telegram
                if state.get("is_paused") or state.get("mute_notifications"):
                    print(json.dumps({"decision": "allow"}))
                    return
    except Exception:
        pass

    tool_call = payload.get("toolCall", {})
    args = tool_call.get("args", {})
    questions = args.get("questions", [])

    if not questions:
        print(json.dumps({"decision": "allow"}))
        return

    # Construir mensaje y botones interactivos
    q_data = questions[0]
    question_text = q_data.get("question", "Antigravity necesita que elijas una opción:")
    options = q_data.get("options", [])

    msg_lines = [
        "<b>Antigravity abrió un cuadro de opciones en tu Mac</b>\n",
        f"<b>Pregunta:</b>\n<i>«{question_text}»</i>\n",
        "<b>Opciones disponibles:</b>"
    ]

    keyboard_buttons = []
    for idx, opt in enumerate(options, 1):
        msg_lines.append(f"• <b>{idx}.</b> {opt}")
        # Botones en filas
        keyboard_buttons.append([{"text": f"{idx}. {opt[:30]}", "callback_data": f"dialog_choice:{idx-1}"}])

    msg_lines.append("\n<i>Toca uno de los botones abajo o responde con el número de la opción desde tu celular.</i>")
    text = "\n".join(msg_lines)

    # Guardar diálogo pendiente para que el daemon lo conozca
    dialog_info = {
        "question": question_text,
        "options": options,
        "timestamp": time.time(),
        "resolved": False,
        "selected_option": None
    }
    with open(PENDING_DIALOG_PATH, "w", encoding="utf-8") as df:
        json.dump(dialog_info, df)

    # Enviar a Telegram con botones interactivos de forma inmediata y óptima
    if token and user_ids:
        for uid in user_ids:
            try:
                # 1. Enviar mensaje con botones
                req_data = {
                    "chat_id": uid,
                    "text": text,
                    "parse_mode": "HTML",
                    "reply_markup": {
                        "inline_keyboard": keyboard_buttons
                    }
                }
                data = json.dumps(req_data).encode("utf-8")
                req = urllib.request.Request(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    data=data,
                    headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=10):
                    pass

                # 2. Captura opcional (desactivada por defecto para máxima velocidad)
                if config.get("dialog_screenshot", False):
                    shot_path = take_screenshot()
                    if os.path.exists(shot_path):
                        boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
                        body = bytearray()
                        body.extend(f"--{boundary}\r\n".encode("utf-8"))
                        body.extend(f'Content-Disposition: form-data; name="chat_id"\r\n\r\n'.encode("utf-8"))
                        body.extend(f"{uid}\r\n".encode("utf-8"))
                        body.extend(f"--{boundary}\r\n".encode("utf-8"))
                        body.extend(f'Content-Disposition: form-data; name="caption"\r\n\r\n'.encode("utf-8"))
                        body.extend("Cuadro de diálogo en tu pantalla:".encode("utf-8"))
                        body.extend(f"--{boundary}\r\n".encode("utf-8"))
                        body.extend(f'Content-Disposition: form-data; name="photo"; filename="dialog.png"\r\n'.encode("utf-8"))
                        body.extend(b"Content-Type: image/png\r\n\r\n")
                        with open(shot_path, "rb") as pf:
                            body.extend(pf.read())
                        body.extend(b"\r\n")
                        body.extend(f"--{boundary}--\r\n".encode("utf-8"))

                        photo_req = urllib.request.Request(
                            f"https://api.telegram.org/bot{token}/sendPhoto",
                            data=bytes(body),
                            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
                        )
                        with urllib.request.urlopen(photo_req, timeout=15):
                            pass
            except Exception as e:
                print(f"Error enviando diálogo a Telegram: {e}", file=sys.stderr)

    # Permitir que el tool continúe mostrando el diálogo en la pantalla
    print(json.dumps({"decision": "allow"}))

if __name__ == "__main__":
    main()
