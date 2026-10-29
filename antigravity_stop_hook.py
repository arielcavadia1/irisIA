#!/usr/bin/env python3
"""
Antigravity Stop Hook: Notificación de alta fidelidad a Telegram (Mission Control).
Incluye motor de tablas Unicode autorresponsivas para pantallas móviles, formateo HTML limpio,
conversión inteligente de listas clave-valor en tablas y referencias de archivos legibles.
"""
import sys
import json
import os
import socket
import urllib.request
import re
import textwrap
import unicodedata
import subprocess
import time

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


def display_width(text):
    """Calcula el ancho real de visualización monospace en terminal/Telegram (manejando emojis y caracteres anchos)."""
    w = 0
    for ch in str(text):
        if unicodedata.east_asian_width(ch) in ('W', 'F') or ord(ch) >= 0x1F300:
            w += 2
        else:
            w += 1
    return w


def pad_to(text, width):
    """Rellena un texto con espacios para que coincida exactamente con el ancho visual monospace deseado."""
    dw = display_width(text)
    pad = max(0, width - dw)
    return str(text) + (' ' * pad)


def _clean_cell_markdown(cell_text):
    """Limpia etiquetas markdown dentro de celdas para renderizado monospace impecable."""
    s = str(cell_text).strip()
    # [label](url) -> label
    s = re.sub(r'\[`?([^`\]]+)`?\]\([^)]+\)', r'\1', s)
    # **bold** or *italic* or `code`
    s = re.sub(r'\*\*([^*]+)\*\*', r'\1', s)
    s = re.sub(r'\*([^*]+)\*', r'\1', s)
    s = re.sub(r'`([^`]+)`', r'\1', s)
    return s.strip()


def render_box_table(headers, rows, max_width=35):
    """
    Renderiza una tabla con caracteres Unicode de caja (box-drawing)
    optimizada para la pantalla estrecha de Telegram en móviles (máx ~35-37 caracteres).
    Garantiza alineación milimétrica incluso con emojis y caracteres multibyte.
    """
    cleaned_headers = [_clean_cell_markdown(h) for h in headers]
    cleaned_rows = [[_clean_cell_markdown(c) for c in r] for r in rows]

    num_cols = len(cleaned_headers)
    if num_cols == 0:
        return ""

    # Asegurar que todas las filas tengan igual número de columnas
    for r in cleaned_rows:
        while len(r) < num_cols:
            r.append("")

    borders_w = 1 + 3 * num_cols  # '│ ' por columna + '│' final
    avail_w = max(10, max_width - borders_w)

    raw_col_widths = [display_width(h) for h in cleaned_headers]
    for r in cleaned_rows:
        for idx in range(num_cols):
            raw_col_widths[idx] = max(raw_col_widths[idx], display_width(r[idx]))

    total_raw = sum(raw_col_widths) or 1
    col_widths = []

    # Ajuste proporcional inteligente de columnas
    if sum(raw_col_widths) + borders_w <= max_width:
        col_widths = [max(w, 3) for w in raw_col_widths]
    else:
        if num_cols == 2:
            key_w = min(max(raw_col_widths[0], 5), 11)
            val_w = max(avail_w - key_w, 8)
            col_widths = [key_w, val_w]
        elif num_cols == 3:
            col1_w = min(max(raw_col_widths[0], 4), 9)
            col3_w = min(max(raw_col_widths[2], 3), 7)
            col2_w = max(avail_w - col1_w - col3_w, 6)
            col_widths = [col1_w, col2_w, col3_w]
        else:
            for w in raw_col_widths:
                allocated = max(3, int((w / total_raw) * avail_w))
                col_widths.append(allocated)

    # Word wrapping de celdas por columna
    wrapped_rows = []

    # Cabeceras
    h_wrapped = []
    for c_idx, h in enumerate(cleaned_headers):
        lines = textwrap.wrap(str(h), width=col_widths[c_idx]) or [""]
        h_wrapped.append(lines)
    wrapped_rows.append(h_wrapped)

    for r in cleaned_rows:
        row_wrapped = []
        for c_idx in range(num_cols):
            cell = str(r[c_idx])
            lines = textwrap.wrap(cell, width=col_widths[c_idx]) or [""]
            row_wrapped.append(lines)
        wrapped_rows.append(row_wrapped)

    def draw_sep(left, mid, right, fill="─"):
        return left + mid.join(fill * (w + 2) for w in col_widths) + right

    out = [draw_sep("┌", "┬", "┐")]
    for r_idx, w_cells in enumerate(wrapped_rows):
        max_lines = max(len(c) for c in w_cells)
        for l_idx in range(max_lines):
            line_parts = []
            for c_idx in range(num_cols):
                cell_lines = w_cells[c_idx]
                val = cell_lines[l_idx] if l_idx < len(cell_lines) else ""
                line_parts.append(" " + pad_to(val, col_widths[c_idx]) + " ")
            out.append("│" + "│".join(line_parts) + "│")
        if r_idx == 0:
            out.append(draw_sep("├", "┼", "┤", "═"))
        elif r_idx < len(wrapped_rows) - 1:
            out.append(draw_sep("├", "┼", "┤", "─"))
    out.append(draw_sep("└", "┴", "┘"))

    return "\n".join(out)


def _render_markdown_table(table_lines):
    """Convierte líneas de tabla GFM Markdown en tabla Unicode box-drawing lista para Telegram."""
    rows = []
    for line in table_lines:
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        inner = stripped.strip("|")
        cells = [c.strip() for c in inner.split("|")]
        # Ignorar fila divisoria | :--- | :--- |
        if all(re.match(r'^:?-{2,}:?$', c.replace(" ", "")) for c in cells if c):
            continue
        rows.append(cells)

    if not rows:
        return "\n".join(table_lines)

    headers = rows[0]
    data_rows = rows[1:] if len(rows) > 1 else []
    table_str = render_box_table(headers, data_rows, max_width=35)
    safe_table = table_str.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"<pre>{safe_table}</pre>"


def clean_system_noise(text):
    """Elimina cadenas de depuración y tareas en segundo plano del motor CLI."""
    if not text:
        return ""
    lines = []
    for line in text.splitlines():
        if re.search(r"root agent idle|waiting up to|\bbackground task|task-[a-zA-Z0-9_-]+|Step was canceled|<SYSTEM_MESSAGE>|\[Message\] timestamp=", line, re.I):
            continue
        if re.match(r"^\s*\*\*[¡!]?Ya terminó[¡!]?\*\*\s*$", line, re.I):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def _convert_item_action_blocks(text, save_ph=None):
    """
    Detecta listas de viñetas de acciones en archivos/módulos
    (ej: - [api.ts](file://...): Actualizó la key...)
    y las formatea con viñetas limpias y código monospace nativo en Telegram.
    """
    lines = text.splitlines()
    processed = []
    buf = []

    def flush_buf():
        for itm, dsc in buf:
            safe_itm = itm.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            code_tag = f"<code>{safe_itm}</code>"
            key_val = save_ph(code_tag) if save_ph else code_tag
            processed.append(f"• {key_val}: {dsc}")

    item_pat = re.compile(r"^\s*[-*]\s+(?:\*\*([^*:]+):\*\*|`([^`:]+)`:\s*|([^:\n]+?):\s*)(.+)$")

    for line in lines:
        m = item_pat.match(line)
        if m:
            raw_key = (m.group(1) or m.group(2) or m.group(3) or "").strip()
            desc = m.group(4).strip()
            clean_key = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", raw_key)
            clean_key = clean_key.replace("`", "").replace("**", "").strip()
            clean_key = re.sub(r"file:///[^\s\)\"\\]+/([^/\s\)\"\\]+)", r"\1", clean_key)
            buf.append([clean_key, desc])
        else:
            if buf:
                flush_buf()
                buf = []
            processed.append(line)

    if buf:
        flush_buf()

    return "\n".join(processed)


def _convert_key_value_blocks(text, save_ph=None):
    """
    Detecta bloques de viñetas clave-valor (ej: * **Ruta:** app.apk)
    y los formatea con negritas limpias en Telegram.
    """
    lines = text.split("\n")
    processed = []
    kv_buf = []

    def flush_kv():
        for k, v in kv_buf:
            processed.append(f"• <b>{k}:</b> {v}")

    for line in lines:
        m = re.match(r'^[ \t]*[*-]\s+\*\*([^*:]+):\*\*\s*(.+)$', line)
        if m:
            kv_buf.append([m.group(1).strip(), m.group(2).strip()])
        else:
            if kv_buf:
                flush_kv()
                kv_buf = []
            processed.append(line)

    if kv_buf:
        flush_kv()

    return "\n".join(processed)


def format_telegram_html(text):
    """Convierte Markdown de Antigravity en Telegram HTML estructurado, limpio y visualmente amigable."""
    if not text:
        return ""

    # Limpiar ruido interno de tareas CLI
    text = clean_system_noise(text)

    placeholders = {}
    ph_counter = [0]

    def save_ph(html_fragment):
        key = f"@@IRISPH{ph_counter[0]}@@"
        ph_counter[0] += 1
        placeholders[key] = html_fragment
        return key

    # 1. Extraer bloques de código ```lang ... ```
    def repl_code_block(m):
        code = m.group(1).strip("\n")
        safe_code = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return save_ph(f"<pre>{safe_code}</pre>")

    s = re.sub(r'```(?:[a-zA-Z0-9_-]+)?\n(.*?)```', repl_code_block, text, flags=re.DOTALL)

    # 2. Extraer y convertir tablas Markdown (| Col 1 | Col 2 |) en tablas Unicode
    lines = s.split("\n")
    processed_lines = []
    table_buf = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2:
            table_buf.append(line)
        else:
            if table_buf:
                if len(table_buf) >= 2:
                    processed_lines.append(save_ph(_render_markdown_table(table_buf)))
                else:
                    processed_lines.extend(table_buf)
                table_buf = []
            processed_lines.append(line)
    if table_buf:
        if len(table_buf) >= 2:
            processed_lines.append(save_ph(_render_markdown_table(table_buf)))
        else:
            processed_lines.extend(table_buf)
    s = "\n".join(processed_lines)

    # 3. Limpiar enlaces antes de convertir items a tablas
    s = re.sub(r'\[([^\]]+)\]\((?:file|conversation)://[^)]+\)', r'\1', s)
    s = re.sub(r'file:///[^\s\)\"\\]+/([^/\s\)\"\\]+)', r'\1', s)

    # 4. Convertir listas de items y acciones en tablas
    s = _convert_item_action_blocks(s, save_ph)

    # 5. Convertir listas clave-valor en tablas
    s = _convert_key_value_blocks(s, save_ph)

    # 6. Enlaces web https://
    def repl_web_link(m):
        label = m.group(1).strip().strip("`")
        url = m.group(2).strip()
        safe_label = label.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return save_ph(f'<a href="{url}">{safe_label}</a>')

    s = re.sub(r'\[([^\]]+)\]\((https?://[^)]+)\)', repl_web_link, s)

    # 7. Extraer código inline `code`
    def repl_inline_code(m):
        code = m.group(1)
        safe_code = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return save_ph(f"<code>{safe_code}</code>")

    s = re.sub(r'`([^`\n]+)`', repl_inline_code, s)

    # 8. Escapar entidades HTML restantes
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # 9. Separadores horizontales
    s = re.sub(r'^\s*[-*_]{3,}\s*$', '─────────────────────────', s, flags=re.MULTILINE)

    # 10. Encabezados
    s = re.sub(r'^#{1,2}\s+(.+)$', r'\n<b>🔷 \1</b>', s, flags=re.MULTILINE)
    s = re.sub(r'^#{3,6}\s+(.+)$', r'\n<b>▸ \1</b>', s, flags=re.MULTILINE)

    # 11. Negrita y Cursiva
    s = re.sub(r'\*\*([^*\n]+)\*\*', r'<b>\1</b>', s)
    s = re.sub(r'(?<!\*)\*([^*\n]+)\*(?!\*)', r'<i>\1</i>', s)

    # 12. Citas tipo Blockquote
    s = re.sub(r'^&gt;\s*\[!(?:NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*', '💡 ', s, flags=re.MULTILINE)
    s = re.sub(r'^&gt;\s?(.*)$', r'┃ <i>\1</i>', s, flags=re.MULTILINE)

    # 13. Formato para listas numeradas y viñetas
    s = re.sub(r'^(\d+)\.\s+<b>([^<]+)</b>', r'<b>\1. 📦 \2</b>', s, flags=re.MULTILINE)
    s = re.sub(r'^[ \t]{2,}[*-]\s+', '   ◦ ', s, flags=re.MULTILINE)
    s = re.sub(r'^[*-]\s+', '• ', s, flags=re.MULTILINE)
    s = re.sub(r'^(\d+)\.\s+', r'<b>\1.</b> ', s, flags=re.MULTILINE)

    # 14. Restaurar placeholders preservados
    for key, val in placeholders.items():
        s = s.replace(key, val)

    # Colapsar líneas en blanco innecesarias
    s = re.sub(r'\n{3,}', '\n\n', s).strip()
    return s


def extract_transcript_data(transcript_path):
    if not transcript_path or not os.path.exists(transcript_path):
        return None, None
    try:
        last_request = None
        last_response = None
        with open(transcript_path, "r", encoding="utf-8") as f:
            for line in f:
                if '"type":"USER_INPUT"' in line:
                    data = json.loads(line)
                    content = data.get("content", "")
                    if "<USER_REQUEST>" in content:
                        clean = content.split("<USER_REQUEST>")[1].split("</USER_REQUEST>")[0].strip()
                        if clean:
                            last_request = clean
                elif '"PLANNER_RESPONSE"' in line:
                    data = json.loads(line)
                    c = data.get("content")
                    if c and c.strip():
                        last_response = c.strip()
        return last_request, last_response
    except Exception:
        return None, None


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
        # 1. Comprobar si la tapa de la laptop está cerrada (clamshell)
        clamshell = subprocess.check_output(["ioreg", "-r", "-c", "IOPMrootDomain"], timeout=2).decode("utf-8", errors="ignore")
        if '"AppleClamshellState" = Yes' in clamshell:
            return False

        # 2. Comprobar si la pantalla está apagada / en reposo
        if '"DisplayAsleep" = Yes' in clamshell:
            return False

        # 3. Comprobar inactividad de teclado y ratón
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

def ensure_iris_daemon_and_launchagent():
    """Garantiza que el LaunchAgent TCC-safe esté actualizado y que iris_daemon.py esté activo fuera del sandbox."""
    if sys.platform != "darwin":
        return
    try:
        src_plist = os.path.join(SCRIPT_DIR, "com.iris.daemon.plist")
        dst_dir = os.path.expanduser("~/Library/LaunchAgents")
        dst_plist = os.path.join(dst_dir, "com.iris.daemon.plist")
        if os.path.exists(src_plist):
            with open(src_plist, "r", encoding="utf-8") as sf:
                src_content = sf.read()
            dst_content = ""
            if os.path.exists(dst_plist):
                with open(dst_plist, "r", encoding="utf-8") as df:
                    dst_content = df.read()
            if src_content != dst_content:
                os.makedirs(dst_dir, exist_ok=True)
                with open(dst_plist, "w", encoding="utf-8") as df:
                    df.write(src_content)
                subprocess.Popen(
                    ["/bin/bash", "-c", f'launchctl unload "{dst_plist}" 2>/dev/null; launchctl load -w "{dst_plist}" 2>/dev/null'],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True
                )
    except Exception:
        pass

    try:
        force_flag = os.path.join(SCRIPT_DIR, "force_native_restart.flag")
        force_restart = os.path.exists(force_flag)
        if force_restart:
            try:
                os.remove(force_flag)
            except Exception:
                pass

        alive = False
        if not force_restart:
            for fname in ("iris.lock", "iris.pid"):
                fpath = os.path.join(SCRIPT_DIR, fname)
                if os.path.exists(fpath):
                    try:
                        pid = int(open(fpath, "r").read().strip())
                        os.kill(pid, 0)
                        alive = True
                        break
                    except Exception:
                        pass
            if not alive:
                r = subprocess.run(["pgrep", "-f", "iris_daemon.py"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if r.returncode == 0:
                    alive = True

        if not alive or force_restart:
            subprocess.Popen(
                ["/bin/bash", os.path.join(SCRIPT_DIR, "iris.sh"), "start"],
                cwd=SCRIPT_DIR,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True
            )
    except Exception:
        pass


def main():
    ensure_iris_daemon_and_launchagent()
    if os.environ.get("IRIS_SUBPROCESS") == "1":
        print(json.dumps({"decision": "allow"}))
        return


    try:
        raw_input = sys.stdin.read()
        payload = json.loads(raw_input) if raw_input.strip() else {}
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

    try:
        if os.path.exists(STATE_PATH):
            with open(STATE_PATH, "r", encoding="utf-8") as sf:
                state = json.load(sf)
                # Si la tarea fue lanzada desde el chatbot de Telegram (iris_daemon.py),
                # el daemon ya envía la respuesta directamente; no duplicar el mensaje por el hook.
                if state.get("iris_task_running") or (time.time() - state.get("iris_task_finished_at", 0) < 15):
                    print(json.dumps({"decision": "allow"}))
                    return

                # Si el Modo Reposo está activo (Reposo: ON), silenciar notificaciones a Telegram
                if state.get("is_paused") or state.get("mute_notifications"):
                    try:
                        subprocess.Popen(["afplay", "/System/Library/Sounds/Glass.aiff"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    except Exception:
                        pass
                    print(json.dumps({"decision": "allow"}))
                    return
    except Exception:
        pass

    transcript_path = payload.get("transcriptPath", "")
    error = payload.get("error")
    user_request, agent_response = extract_transcript_data(transcript_path)
    # Emitir sonido de confirmación en macOS
    try:
        subprocess.Popen(["afplay", "/System/Library/Sounds/Glass.aiff"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

    lines = [
        "🚀 <b>IRIS LITE · COMMUNITY EDITION</b>",
        "─────────────────────────────",
        "✨ <b>¡Acción finalizada con éxito!</b>\n"
    ]

    if user_request:
        cleaned_req = user_request.strip()
        if len(cleaned_req) > 240:
            cleaned_req = cleaned_req[:237] + "..."
        safe_req = cleaned_req.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        lines.append(f"📋 <b>Solicitud recibida:</b>\n┃ <i>«{safe_req}»</i>\n")

    if agent_response:
        formatted_resp = format_telegram_html(agent_response)
        if len(formatted_resp) > 3400:
            cut = formatted_resp.rfind("\n", 0, 3400)
            if cut == -1:
                cut = 3400
            formatted_resp = formatted_resp[:cut] + "\n\n<i>... (resumen completo en tu monitor)</i>"
        lines.append(f"🎯 <b>Acción realizada:</b>\n{formatted_resp}\n")

    if error:
        safe_err = str(error)[:180].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        lines.append(f"⚠️ <b>Alerta:</b> Detalle durante ejecución: <code>{safe_err}</code>\n")

    lines.append("─────────────────────────────")
    os_name = "Mac" if sys.platform == "darwin" else ("PC Windows" if sys.platform == "win32" else "Equipo Linux")
    lines.append(f"👉 <i>Tu {os_name} está libre y listo para recibir nuevas instrucciones.</i>")
    text = "\n".join(lines)

    if token and user_ids:
        for uid in user_ids:
            try:
                data = json.dumps({"chat_id": uid, "text": text, "parse_mode": "HTML"}).encode("utf-8")
                req = urllib.request.Request(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    data=data,
                    headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=8):
                    pass
            except Exception:
                try:
                    plain = re.sub(r'<[^>]+>', '', text)
                    data = json.dumps({"chat_id": uid, "text": plain}).encode("utf-8")
                    req = urllib.request.Request(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        data=data,
                        headers={"Content-Type": "application/json"}
                    )
                    with urllib.request.urlopen(req, timeout=8):
                        pass
                except Exception:
                    pass

    print(json.dumps({"decision": "allow"}))


if __name__ == "__main__":
    main()
