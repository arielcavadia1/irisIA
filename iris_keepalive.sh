#!/bin/bash
# Guardián de persistencia de Iris (invocado por LaunchAgent com.iris.daemon cada 15s y al iniciar sesión)
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCK_FILE="$DIR/iris.lock"
PID_FILE="$DIR/iris.pid"
STATE_FILE="$DIR/state.json"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

# 1. Mantener blindaje energético en batería, corriente y tapa cerrada (salvo que esté en modo reposo manual)
IS_PAUSED=$(python3 -c "import json; s=json.load(open('$STATE_FILE')) if __import__('os').path.exists('$STATE_FILE') else {}; print('1' if s.get('is_paused') else '0')" 2>/dev/null || echo "0")
if [ "$IS_PAUSED" != "1" ]; then
    sudo -n /usr/bin/pmset -a disablesleep 1 sleep 0 standby 0 autopoweroff 0 tcpkeepalive 1 powernap 1 womp 1 2>/dev/null || true
fi

# 2. Verificar si existe un proceso iris_daemon.py vivo y sin bloqueo de red/bucle
ALIVE=0
for f in "$LOCK_FILE" "$PID_FILE"; do
    if [ -f "$f" ]; then
        P=$(cat "$f" 2>/dev/null | tr -d '[:space:]')
        if [ -n "$P" ] && kill -0 "$P" 2>/dev/null; then
            ALIVE=1
            break
        fi
    fi
done

if [ "$ALIVE" -eq 0 ]; then
    if pgrep -f "iris_daemon.py" >/dev/null 2>&1; then
        ALIVE=1
    fi
fi

# 2b. Si el proceso existe pero el latido (iris.heartbeat) lleva >120s congelado sin tarea activa, forzar reinicio
HEARTBEAT_FILE="$DIR/iris.heartbeat"
if [ "$ALIVE" -eq 1 ] && [ -f "$HEARTBEAT_FILE" ]; then
    IS_STUCK=$(python3 -c "
import os, time, json
hb_file = '$HEARTBEAT_FILE'
st_file = '$STATE_FILE'
try:
    st = json.load(open(st_file)) if os.path.exists(st_file) else {}
    if st.get('iris_task_running'):
        print('0')
    else:
        hb = int(open(hb_file).read().strip() or '0')
        print('1' if (hb > 0 and (time.time() - hb) > 120) else '0')
except Exception:
    print('0')
" 2>/dev/null || echo "0")
    if [ "$IS_STUCK" = "1" ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] [KEEPALIVE] Bucle de Iris detectado congelado (>120s sin latido); reiniciando demonio..." >> "$DIR/daemon.log"
        ALIVE=0
    fi
fi

# 3. Si Iris no está corriendo (o estaba congelada), iniciarla automáticamente
if [ "$ALIVE" -eq 0 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [KEEPALIVE] Iris no detectada activa; reviviendo demonio..." >> "$DIR/daemon.log"
    /bin/bash "$DIR/iris.sh" start >> "$DIR/daemon.log" 2>&1
fi
