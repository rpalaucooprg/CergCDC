#!/usr/bin/env bash
# ============================================================
# CERG · CDC — instalación en Ubuntu Server
# Idempotente en lo posible. Ejecutar como root (o con sudo).
# Uso:  sudo ./deploy/install.sh
# ============================================================
set -euo pipefail

APP_DIR=/opt/cerg-cdc
ENV_DIR=/etc/cerg-cdc
SERVICE_USER=cerg
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> Repo: $REPO_DIR"

# 1. Usuario de servicio (sin login)
if ! id "$SERVICE_USER" &>/dev/null; then
    echo "==> Creando usuario $SERVICE_USER"
    useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

# 2. Copiar código a APP_DIR
echo "==> Copiando aplicación a $APP_DIR"
mkdir -p "$APP_DIR"
rsync -a --delete \
    --exclude 'venv' --exclude '__pycache__' --exclude '.git' \
    --exclude 'tests_integration.py' \
    "$REPO_DIR"/app "$REPO_DIR"/sql "$APP_DIR"/
cp "$REPO_DIR"/requirements.txt "$APP_DIR"/

# 3. Entorno virtual
echo "==> Creando/actualizando venv"
if [ ! -d "$APP_DIR/venv" ]; then
    python3 -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

# 4. Archivo de entorno
mkdir -p "$ENV_DIR"
if [ ! -f "$ENV_DIR/cerg-cdc.env" ]; then
    echo "==> Instalando plantilla de entorno en $ENV_DIR/cerg-cdc.env"
    cp "$REPO_DIR/deploy/cerg-cdc.env.example" "$ENV_DIR/cerg-cdc.env"
    chmod 640 "$ENV_DIR/cerg-cdc.env"
    chown root:"$SERVICE_USER" "$ENV_DIR/cerg-cdc.env"
    echo "    !!! EDITAR $ENV_DIR/cerg-cdc.env con la clave real de la base !!!"
else
    echo "==> $ENV_DIR/cerg-cdc.env ya existe, no se sobrescribe"
fi

# 5. Permisos
chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR"

# 6. Servicios systemd
echo "==> Instalando unidades systemd"
cp "$REPO_DIR/deploy/cerg-poller.service" /etc/systemd/system/
cp "$REPO_DIR/deploy/cerg-web.service" /etc/systemd/system/
systemctl daemon-reload

# 7. nginx (si esta instalado): reverse proxy en :80 -> gunicorn en 127.0.0.1:8000.
#    Asi el tablero es accesible desde la LAN por el puerto 80 SIN exponer
#    gunicorn directo. gunicorn queda seguro escuchando solo en localhost.
if command -v nginx >/dev/null 2>&1; then
    echo "==> Configurando nginx (reverse proxy en puerto 80)"
    cp "$REPO_DIR/deploy/nginx-cerg-cdc.conf" /etc/nginx/sites-available/cerg-cdc
    ln -sf /etc/nginx/sites-available/cerg-cdc /etc/nginx/sites-enabled/cerg-cdc
    rm -f /etc/nginx/sites-enabled/default
    if nginx -t 2>/dev/null; then
        systemctl reload nginx || systemctl restart nginx
        echo "    nginx OK: el tablero quedara en http://<IP-del-CT>/ (puerto 80)"
    else
        echo "    !!! nginx -t fallo; revisar /etc/nginx/sites-available/cerg-cdc"
    fi
else
    echo "==> nginx no esta instalado; el web quedara solo en 127.0.0.1:8000."
    echo "    Para acceso desde la LAN: instalar nginx (ver bootstrap) o exponer"
    echo "    gunicorn cambiando --bind a 0.0.0.0:8000 en el servicio (menos seguro)."
fi

cat <<'NEXT'

==> Instalación de archivos completa. Pasos manuales restantes:

  NOTA: en un LXC dedicado, deploy/lxc-bootstrap.sh ya hace los pasos 0-1 y 6
  (paquetes, PostgreSQL en UTF8, locale, timezone, base/rol y nginx). Si lo
  usaste, saltá directo al paso 2.

  0) Instalar PostgreSQL (versión por defecto del release de Ubuntu):
       sudo apt update
       sudo apt install -y postgresql postgresql-client postgresql-contrib nginx

  1) Crear la base y el rol (LA BASE DEBE SER UTF8, si no falla con caracteres
     no-ASCII; usar TEMPLATE template0 para no heredar SQL_ASCII):
       sudo -u postgres psql -c "CREATE USER cerg WITH PASSWORD 'clave';"
       sudo -u postgres psql -c "CREATE DATABASE cerg_cdc OWNER cerg \
         ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0;"
     Verificar:  sudo -u postgres psql -l | grep cerg_cdc   (Encoding = UTF8)

  2) Editar la clave real y el locale en el entorno:
       sudo nano /etc/cerg-cdc/cerg-cdc.env
       # CDC_DB_PASSWORD=...  (cualquier caracter va: el codigo la URL-encodea)
       # LANG=es_AR.UTF-8
       # LC_ALL=es_AR.UTF-8

  3) Aplicar el esquema (el poller también lo hace al arrancar):
       sudo -u postgres psql -d cerg_cdc -f /opt/cerg-cdc/sql/schema.sql

  4) Habilitar y arrancar los servicios:
       sudo systemctl enable --now cerg-poller.service
       sudo systemctl enable --now cerg-web.service

  5) Verificar:
       systemctl status cerg-poller cerg-web
       journalctl -u cerg-poller -f          # esperar "ok . gen XX MW ..."
       curl -s localhost:8000/healthz         # {"status":"ok"}
       # desde la LAN, con nginx:  http://<IP-del-CT>/

NEXT
