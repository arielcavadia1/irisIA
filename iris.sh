#!/bin/bash
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_FILE="$DIR/iris.pid"
LOCK_FILE="$DIR/iris.lock"
LOG_FILE="$DIR/daemon.log"
RELOAD_FLAG="$DIR/reload_pending.json"

stop_all_iris() {
    for f in "$LOCK_FILE" "$PID_FILE"; do
        if [ -f "$f" ]; then
            P=$(cat "$f" 2>/dev/null | tr -d '[:space:]')
            if [ -n "$P" ]; then
                kill -9 "$P" 2>/dev/null
            fi
        fi
    done
    pkill -9 -f "iris_daemon.py" 2>/dev/null
    rm -f "$PID_FILE" "$LOCK_FILE"
}

case "$1" in
    start)
        if [ "$IRIS_SUBPROCESS" = "1" ]; then
            echo '{"reload": true}' > "$RELOAD_FLAG"
            echo "✅ Reinicio diferido programado: Iris se recargará automáticamente apenas entregue la respuesta actual en Telegram."
            exit 0
        fi
        # Validar que iris_daemon.py no tenga errores de sintaxis; si los tiene, restaurar copia estable
        if ! python3 -m py_compile "$DIR/iris_daemon.py" 2>/dev/null; then
            if [ -f "$DIR/iris_daemon.last_good.py" ]; then
                echo "🛡️ Error de sintaxis detectado en iris_daemon.py; restaurando copia estable iris_daemon.last_good.py..."
                cp "$DIR/iris_daemon.last_good.py" "$DIR/iris_daemon.py"
            fi
        else
            cp "$DIR/iris_daemon.py" "$DIR/iris_daemon.last_good.py" 2>/dev/null || true
        fi
        # Aplicar blindaje energético pmset si está disponible sin contraseña
        sudo -n /usr/bin/pmset -a disablesleep 1 sleep 0 standby 0 autopoweroff 0 tcpkeepalive 1 powernap 1 womp 1 2>/dev/null || true
        # Limpiar cualquier proceso previo antes de iniciar
        stop_all_iris
        sleep 0.5
        echo "🚀 Iniciando Iris en segundo plano (sesión independiente)..."
        python3 -c 'import subprocess, sys; p = subprocess.Popen([sys.executable, "-u", sys.argv[1]], stdin=subprocess.DEVNULL, stdout=open(sys.argv[2], "a"), stderr=subprocess.STDOUT, start_new_session=True); open(sys.argv[3], "w").write(str(p.pid))' "$DIR/iris_daemon.py" "$LOG_FILE" "$PID_FILE"
        sleep 1
        echo "✅ Iris iniciada (PID: $(cat "$PID_FILE" 2>/dev/null)). Log en: $LOG_FILE"
        ;;
    stop)
        if [ "$IRIS_SUBPROCESS" = "1" ]; then
            echo "⚠️ No se puede detener iris_daemon.py desde su propio subproceso activo."
            exit 0
        fi
        echo "🛑 Deteniendo Iris..."
        stop_all_iris
        echo "✅ Iris detenida."
        ;;
    status)
        ACTIVE_PID=""
        for f in "$LOCK_FILE" "$PID_FILE"; do
            if [ -f "$f" ]; then
                P=$(cat "$f" 2>/dev/null | tr -d '[:space:]')
                if [ -n "$P" ] && kill -0 "$P" 2>/dev/null; then
                    ACTIVE_PID="$P"
                    break
                fi
            fi
        done
        COUNT=$(pgrep -f "iris_daemon.py" 2>/dev/null | wc -l | tr -d ' ')
        if [ -n "$ACTIVE_PID" ] || [ "${COUNT:-0}" -gt 0 ]; then
            echo "🟢 Iris está ACTIVA (PID: ${ACTIVE_PID:-$COUNT}) y escuchando en Telegram."
            echo "--- Últimas líneas del log ---"
            tail -n 5 "$LOG_FILE"
        else
            echo "🔴 Iris está DETENIDA."
        fi
        ;;
    restart)
        if [ "$IRIS_SUBPROCESS" = "1" ]; then
            echo '{"reload": true}' > "$RELOAD_FLAG"
            echo "✅ Reinicio diferido programado: Iris se recargará automáticamente apenas entregue la respuesta actual en Telegram."
            exit 0
        fi
        "$0" stop
        sleep 1
        "$0" start
        ;;
    logs)
        tail -f "$LOG_FILE"
        ;;
    *)
        echo "Uso: $0 {start|stop|restart|status|logs}"
        exit 1
        ;;
esac
