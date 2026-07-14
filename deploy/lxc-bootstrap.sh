#!/usr/bin/env bash
# ============================================================
# CERG · CDC — preparación de un LXC Ubuntu (25.04 u otra release) desde cero
# Instala dependencias del sistema, PostgreSQL (versión por defecto del
# release), locale es_AR y
# timezone de Tierra del Fuego. Ejecutar como root DENTRO del contenedor.
# Uso:  ./deploy/lxc-bootstrap.sh
#
# Luego correr:  ./deploy/install.sh   (despliega la app)
# ============================================================
set -euo pipefail

DB_NAME=cerg_cdc
DB_USER=cerg

echo "==> Actualizando índice de paquetes"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get -y upgrade

echo "==> Instalando dependencias del sistema"
apt-get install -y \
    python3 python3-venv python3-dev \
    postgresql postgresql-client postgresql-contrib \
    build-essential libpq-dev \
    nginx \
    rsync curl ca-certificates \
    locales tzdata

PG_VER="$(psql --version 2>/dev/null | grep -oE '[0-9]+' | head -1 || echo '?')"
echo "==> PostgreSQL instalado: versión ${PG_VER} (default del release de Ubuntu)"

echo "==> Configurando locale es_AR.UTF-8"
sed -i 's/^# *es_AR.UTF-8/es_AR.UTF-8/' /etc/locale.gen || true
locale-gen es_AR.UTF-8 || true

echo "==> Configurando timezone America/Argentina/Ushuaia (Tierra del Fuego)"
ln -sf /usr/share/zoneinfo/America/Argentina/Ushuaia /etc/localtime
echo "America/Argentina/Ushuaia" > /etc/timezone
dpkg-reconfigure -f noninteractive tzdata || true

echo "==> Asegurando que PostgreSQL esté activo"
systemctl enable --now postgresql

echo "==> Creando base y rol (si no existen)"
DB_PASS="$(openssl rand -base64 18 2>/dev/null || echo cambiar_esta_clave)"
sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE USER ${DB_USER} WITH PASSWORD '${DB_PASS}';"
# ENCODING UTF8 explicito con TEMPLATE template0: no hereda el SQL_ASCII que
# suele quedar en template1 en un LXC recien creado con locale C. Sin esto,
# PostgreSQL rechaza guardar JSON con caracteres no-ASCII (p.ej. "P3.b11" con
# punto medio en protBits).
sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE DATABASE ${DB_NAME} OWNER ${DB_USER} ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0;"

# Verifica el encoding y avisa si por algun motivo no quedo en UTF8.
DB_ENC="$(sudo -u postgres psql -tAc "SELECT pg_encoding_to_char(encoding) FROM pg_database WHERE datname='${DB_NAME}'" | tr -d '[:space:]')"
echo "==> Encoding de ${DB_NAME}: ${DB_ENC}"
if [ "${DB_ENC}" != "UTF8" ]; then
    echo "    !!! ADVERTENCIA: la base NO quedo en UTF8 (${DB_ENC})."
    echo "    !!! Si la base es nueva y sin datos, recreala:"
    echo "        sudo -u postgres psql -c \"DROP DATABASE ${DB_NAME};\""
    echo "        sudo -u postgres psql -c \"CREATE DATABASE ${DB_NAME} OWNER ${DB_USER} ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0;\""
fi

cat <<NEXT

==> Contenedor preparado.

  PostgreSQL ${PG_VER} instalado y corriendo. Base '${DB_NAME}', rol '${DB_USER}'.
NEXT

if [ "${DB_PASS}" != "cambiar_esta_clave" ]; then
cat <<NEXT
  Clave generada para el rol '${DB_USER}':

      ${DB_PASS}

  >>> Anotala y ponela en /etc/cerg-cdc/cerg-cdc.env (CDC_DB_PASSWORD)
      después de correr deploy/install.sh
NEXT
fi

cat <<'NEXT'

  Siguiente paso:
      ./deploy/install.sh

  Recordatorios de Proxmox LXC:
    - Contenedor unprivileged está OK.
    - Activar Features: nesting=1 (necesario para systemd dentro del LXC).
    - Si Postgres se queja de shared memory, revisar límites de shm del CT.
NEXT
