#!/bin/bash
set -e

RULE_FILE="/etc/sudoers.d/iris_pmset"

echo "🔐 Configurando permisos seguros para control de reposo en batería y tapa cerrada..."
echo "Este paso autoriza a Iris a ejecutar '/usr/bin/pmset' sin pedir contraseña cada vez."

# Crear regla segura en sudoers.d
echo "$USER ALL=(ALL) NOPASSWD: /usr/bin/pmset" | sudo tee "$RULE_FILE" > /dev/null
sudo chmod 0440 "$RULE_FILE"

# Validar sintaxis con visudo
if sudo visudo -c -f "$RULE_FILE" >/dev/null 2>&1; then
    echo "✅ Regla de sudoers verificada correctamente."
else
    echo "❌ Error en sintaxis de sudoers. Eliminando archivo por seguridad..."
    sudo rm -f "$RULE_FILE"
    exit 1
fi

echo "🚀 Aplicando blindaje anti-reposo en batería y corriente (tapa abierta o cerrada)..."
sudo /usr/bin/pmset -a disablesleep 1 sleep 0 standby 0 autopoweroff 0 tcpkeepalive 1 powernap 1 womp 1
echo "🎉 ¡Listo! Iris mantiene tu Mac despierto en batería, corriente y tapa cerrada."

