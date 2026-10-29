#!/usr/bin/env python3
import sys
import os
import json
import socket
import ssl
import urllib.request
import urllib.parse

# Forzar prioridad IPv4 para evitar blackholes IPv6 hacia api.telegram.org
_orig_getaddrinfo = socket.getaddrinfo
def _ipv4_priority_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    res = _orig_getaddrinfo(host, port, family, type, proto, flags)
    ipv4 = [r for r in res if r[0] == socket.AF_INET]
    return ipv4 + [r for r in res if r[0] != socket.AF_INET] if ipv4 else res
socket.getaddrinfo = _ipv4_priority_getaddrinfo

SSL_CONTEXT = ssl.create_default_context()
SSL_CONTEXT.check_hostname = False
SSL_CONTEXT.verify_mode = ssl.CERT_NONE

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def send_telegram_message(text, parse_mode="HTML"):
    config = load_config()
    token = config["bot_token"]
    user_ids = config["allowed_user_ids"]
    
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    
    success = True
    for user_id in user_ids:
        data = {
            "chat_id": user_id,
            "text": text,
            "parse_mode": parse_mode
        }
        encoded_data = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=encoded_data,
            headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=15):
                pass
        except Exception as e:
            print(f"Error enviando mensaje a {user_id}: {e}", file=sys.stderr)
            success = False
    return success

def send_telegram_photo(photo_path, caption=""):
    config = load_config()
    token = config["bot_token"]
    user_ids = config["allowed_user_ids"]
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    
    if not os.path.exists(photo_path):
        print(f"Error: Foto no encontrada en {photo_path}", file=sys.stderr)
        return False

    with open(photo_path, "rb") as f:
        file_bytes = f.read()

    boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
    for user_id in user_ids:
        body = bytearray()
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="chat_id"\r\n\r\n'.encode("utf-8"))
        body.extend(f"{user_id}\r\n".encode("utf-8"))
        
        if caption:
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(f'Content-Disposition: form-data; name="caption"\r\n\r\n'.encode("utf-8"))
            body.extend(f"{caption}\r\n".encode("utf-8"))

        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="photo"; filename="{os.path.basename(photo_path)}"\r\n'.encode("utf-8"))
        body.extend(b"Content-Type: image/jpeg\r\n\r\n")
        body.extend(file_bytes)
        body.extend(b"\r\n")
        body.extend(f"--{boundary}--\r\n".encode("utf-8"))

        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        req = urllib.request.Request(url, data=bytes(body), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15):
                pass
        except Exception as e:
            print(f"Error enviando foto a {user_id}: {e}", file=sys.stderr)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: notify.py 'Mensaje'")
        print("     notify.py --photo /ruta/a/imagen.png 'Mensaje opcional'")
        sys.exit(1)
        
    if sys.argv[1] == "--photo":
        photo = sys.argv[2]
        caption = " ".join(sys.argv[3:]) if len(sys.argv) > 3 else ""
        send_telegram_photo(photo, caption)
    else:
        msg = " ".join(sys.argv[1:])
        send_telegram_message(msg)
