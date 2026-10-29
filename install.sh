#!/bin/bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "================================================================================"
echo "  Iris (Lite Community Edition) — Instalador Automático para macOS"
echo "  Creado por Ariel Castro Cavadía (https://www.arielcastrocavadia.com)"
echo "================================================================================"

OS="$(uname -s)"
ARCH="$(uname -m)"

if [ "$OS" != "Darwin" ]; then
    echo "Iris está diseñado actualmente para macOS (Darwin). Windows y Linux próximamente."
    exit 1
fi

echo "Sistema detectado: macOS ($OS) | Arquitectura: $ARCH"

# 1. Configuración sencilla de config.json (cada usuario usa su propio Bot privado de @BotFather)
if [ ! -f "$DIR/config.json" ] || grep -q "TU_TELEGRAM_BOT_TOKEN_AQUI" "$DIR/config.json" 2>/dev/null; then
    echo ""
    echo "--- Paso 1: Configuración de tu Bot Privado de Telegram ---"
    echo "(Para proteger tu privacidad, cada usuario crea su propio bot gratis en @BotFather)"
    if [ -t 0 ]; then
        read -p "1. ¿Cómo quieres que te llamen los agentes? [Por defecto: Comandante]: " INPUT_NAME
        INPUT_NAME=${INPUT_NAME:-Comandante}
        read -p "2. Pega el Token de tu bot (créalo en @BotFather con /newbot): " INPUT_TOKEN
        read -p "3. Pega tu ID numérico de Telegram (consúltalo en @userinfobot): " INPUT_UID
        if [ -n "$INPUT_TOKEN" ] && [ -n "$INPUT_UID" ]; then
            cat <<JSON_EOF > "$DIR/config.json"
{
  "user_name": "$INPUT_NAME",
  "bot_token": "$INPUT_TOKEN",
  "allowed_user_ids": [$INPUT_UID],
  "default_cwd": "$HOME/Documents",
  "agy_bin": "$HOME/.local/bin/agy",
  "claude_bin": "$HOME/.local/bin/claude",
  "auto_caffeinate": true,
  "model": "gemini-4-argon-medium",
  "voice_name": "es-MX-DaliaNeural",
  "task_timeout_seconds": 0,
  "continue_conversation": true
}
JSON_EOF
            echo "config.json configurado automáticamente para $INPUT_NAME."
        else
            cp "$DIR/config.example.json" "$DIR/config.json"
            echo "Plantilla config.json creada. Recuerda colocar tu bot_token y tu ID en $DIR/config.json."
        fi
    else
        cp "$DIR/config.example.json" "$DIR/config.json"
        echo "Plantilla config.json creada. Edita $DIR/config.json con tu bot_token y tu ID."
    fi
fi

# 2. Generar LaunchAgents (.plist) con la ruta exacta de este Mac
cat <<PLIST_EOF > "$DIR/com.iris.daemon.plist"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.iris.daemon</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$DIR/iris_keepalive.sh</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/tmp</string>
    <key>RunAtLoad</key>
    <true/>
    <key>StartInterval</key>
    <integer>15</integer>
    <key>StandardOutPath</key>
    <string>/dev/null</string>
    <key>StandardErrorPath</key>
    <string>/dev/null</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>LANG</key>
        <string>en_US.UTF-8</string>
    </dict>
</dict>
</plist>
PLIST_EOF

cp "$DIR/com.iris.daemon.plist" "$DIR/com.iris.daemon.bg.plist"

# 3. Dependencias de voz y audio en macOS (Homebrew + edge-tts)
echo "Verificando herramientas de audio y transcripción local..."
if command -v brew >/dev/null 2>&1; then
    command -v ffmpeg >/dev/null 2>&1 || brew install ffmpeg
    command -v whisper-cli >/dev/null 2>&1 || brew install whisper-cpp
fi

if ! python3 -c "import edge_tts" >/dev/null 2>&1; then
    python3 -m pip install --user edge-tts 2>/dev/null || pip3 install edge-tts 2>/dev/null || true
fi

# 4. Modelo Whisper base local
mkdir -p "$DIR/models"
MODEL_PATH="$DIR/models/ggml-base.bin"
if [ ! -f "$MODEL_PATH" ]; then
    echo "Descargando modelo Whisper local (140 MB)..."
    curl -L -s -o "$MODEL_PATH" "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin"
fi

# 5. Permisos de ejecución
chmod +x "$DIR/iris_daemon.py" "$DIR/antigravity_stop_hook.py" "$DIR/ask_question_hook.py" "$DIR/notify.py" "$DIR/iris_exec.py" "$DIR/iris.sh" "$DIR/iris_keepalive.sh" "$DIR/setup_closed_lid.sh"

# 6. Enlace notify-tg y Hooks
mkdir -p "$HOME/.local/bin" "$HOME/.gemini/config"
ln -sf "$DIR/notify.py" "$HOME/.local/bin/notify-tg"

cat << HOOKS_EOF > "$HOME/.gemini/config/hooks.json"
{
  "telegram-notifier": {
    "Stop": [
      {
        "type": "command",
        "command": "$DIR/antigravity_stop_hook.py",
        "timeout": 10
      }
    ],
    "PreToolUse": [
      {
        "matcher": "ask_question",
        "hooks": [
          {
            "type": "command",
            "command": "$DIR/ask_question_hook.py",
            "timeout": 25
          }
        ]
      }
    ]
  }
}
HOOKS_EOF

# 7. Iniciar Iris si ya tiene un token configurado
if ! grep -q "TU_TELEGRAM_BOT_TOKEN_AQUI" "$DIR/config.json" 2>/dev/null; then
    "$DIR/iris.sh" start
fi

echo "================================================================================"
echo "¡Listo! Iris quedó instalada en tu Mac."
echo "Si aún no pusiste tu Token en config.json, edítalo y ejecuta: ./iris.sh start"
echo "Creado por Ariel Castro Cavadía — https://www.arielcastrocavadia.com"
echo "================================================================================"
