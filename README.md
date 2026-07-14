# CERG · CDC — Monitor de Generación y Distribución (backend + web)

Servicio de monitoreo en tiempo real para el Centro de Despacho de Cargas de la
Cooperativa Eléctrica Río Grande. Reemplaza el prototipo HTML autónomo por una
arquitectura cliente-servidor donde **un único poller** consulta ScadaVision y
**todos los navegadores** reciben los datos desde nuestro backend vía SSE.

## Por qué

El prototipo HTML hacía que *cada pestaña abierta* consultara el SCADA cada 5 s
por su cuenta: N operadores = N×2 consultas cada 5 s a los relés, y al cerrar la
pestaña se perdía el histórico del trend. Este backend resuelve las dos cosas:

- **Una sola conexión al SCADA**, sin importar cuántos usuarios miren el tablero.
- **Histórico persistente 24/7** en PostgreSQL: el trend sobrevive reinicios y
  permite ver rangos largos.

## Arquitectura

```
                1 conexión                                N navegadores
 ScadaVision ◄──── poller (systemd) ────► PostgreSQL ◄──── web (gunicorn/gevent) ══SSE══► clientes
   /api/*        proceso independiente       histórico       lee, NO consulta SCADA
                 normaliza + persiste       + snapshot
                       │                        ▲
                       └── NOTIFY cdc_snapshot ──┘  (LISTEN/NOTIFY: fan-out de SSE, sin Redis)
```

Dos procesos systemd **separados a propósito**: si el poller viviera dentro del
web, cada worker de Gunicorn tendría su propio poller y volveríamos al problema
de las consultas paralelas. El poller es único; el web solo lee de la base.

## Estructura

```
cerg-cdc/
├── app/
│   ├── normalize.py      # lógica de interpretación del SCADA (puerto fiel del front)
│   ├── config.py         # configuración por variables de entorno
│   ├── db.py             # acceso a PostgreSQL (pool, escrituras, lecturas)
│   ├── poller.py         # servicio: consulta SCADA → normaliza → persiste → NOTIFY
│   ├── web.py            # servicio Flask: /, /api/state, /api/trend, /api/alarms, /api/stream
│   ├── templates/
│   │   └── index.html    # dashboard (consume el backend por SSE; modo demo de fallback)
│   └── static/
├── sql/
│   └── schema.sql        # esquema PostgreSQL (histórico crudo)
├── deploy/
│   ├── cerg-poller.service
│   ├── cerg-web.service
│   ├── nginx-cerg-cdc.conf
│   ├── cerg-cdc.env.example
│   └── install.sh
├── tests_integration.py  # prueba de flujo de datos (SQLite, sin Postgres)
└── requirements.txt
```

## Endpoints

| Método | Ruta | Descripción |
|--------|------|-------------|
| GET | `/` | Dashboard |
| GET | `/api/state` | Último snapshot normalizado (lee caché en DB) |
| GET | `/api/trend?range=SEG` | Trend de generación por TG en la ventana dada (downsampling automático) |
| GET | `/api/alarms?limit=N` | Últimos eventos de alarma |
| GET | `/api/stream` | SSE: empuja cada snapshot nuevo |
| GET | `/healthz` | Chequeo de salud (verifica la base) |

Ningún endpoint consulta ScadaVision. Todo sale de PostgreSQL.

## Base de datos

- `current_snapshot` — fila única con el último snapshot (JSONB). El poller la
  reescribe y dispara `NOTIFY`.
- `gen_sample` — una fila por TG por ciclo (trend de generación).
- `feeder_sample` — una fila por celda por ciclo.
- `alarm_event` / `alarm_state` — eventos de alarma; se registra **solo cuando
  cambia** el estado (activa/reconocida), no una fila por ciclo.

> **Retención:** por ahora se guarda **todo crudo**. A ~5 s/ciclo, `feeder_sample`
> crece ~450k filas/día. Postgres lo maneja por meses; cuando haga falta, ver el
> bloque NOTA en `sql/schema.sql` (particionado, downsampling o TimescaleDB).

## Desarrollo local

Requiere Python 3.11+ y un PostgreSQL accesible.

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Base local
createdb cerg_cdc
psql -d cerg_cdc -f sql/schema.sql

# Variables (o exportar a mano)
export CDC_DATABASE_URL=postgresql://localhost/cerg_cdc
export CDC_SCADA_BASE=https://scadavision.cooprg.net

# Terminal 1: poller
python -m app.poller

# Terminal 2: web
gunicorn --worker-class gevent --workers 1 --timeout 0 --bind 127.0.0.1:8000 app.web:app
# o para depurar:  flask --app app.web run --port 8000

# Abrir http://localhost:8000
```

Sin base o sin poller, el dashboard entra solo en **modo demostración** con datos
de ejemplo claramente marcados — útil para trabajar el front sin infraestructura.

## Prueba de flujo (sin Postgres)

```bash
python tests_integration.py
```

Ejercita normalización → persistencia → `/api/state` → `/api/trend` → alarmas con
un doble SQLite en memoria. Valida además que las alarmas no se duplican y que el
reconocimiento genera un evento nuevo.

## Requisitos

- **Ubuntu 25.04** (Plucky Puffin). Nota: es una release **no-LTS** (soporte de
  9 meses); planificar el upgrade a la próxima release cuando corresponda. El
  código no depende de la versión de Ubuntu.
- **PostgreSQL** — se instala la versión por defecto del release (metapaquete
  `postgresql`). Sirve cualquier rama **16 o superior**: `LISTEN/NOTIFY`,
  `TIMESTAMPTZ` y el resto de lo que usamos funciona igual en 16/17/18. Los
  scripts autodetectan la versión instalada; no hay que fijarla a mano.
  Para verificar qué instalará apt en tu release:  `apt-cache policy postgresql`
- **Python 3** — el que trae la release (3.12+ en 25.04).

## Despliegue en LXC de Proxmox (contenedor dedicado)

Escenario recomendado: un LXC **unprivileged** dedicado a esta app, sobre
Ubuntu 25.04 (el código funciona igual en cualquier release reciente).

**Recursos sugeridos:** 2 vCPU · 2 GB RAM · 16–20 GB disco. Como se guarda todo
crudo, el disco crece ~0,5 GB/mes; conviene un dataset expandible y vigilar el uso.

**Opciones del contenedor en Proxmox:**
- Activar **Features → nesting=1** (systemd dentro del LXC lo necesita).
- Zona horaria del contenedor en `America/Argentina/Ushuaia` (Tierra del Fuego),
  para que logs y pantalla muestren hora local correcta. El bootstrap la fija.

Dentro del contenedor, como root:

```bash
# 1) Preparar el sistema (paquetes, PostgreSQL UTF8, locale, timezone, base y rol)
./deploy/lxc-bootstrap.sh      # anotá la clave que genera para el rol cerg

# 2) Desplegar la app (venv, systemd)
./deploy/install.sh

# 3) Poner la clave de la base y el locale en el entorno
nano /etc/cerg-cdc/cerg-cdc.env    # CDC_DB_PASSWORD=...  / LANG / LC_ALL

# 4) Arrancar
systemctl enable --now cerg-poller.service cerg-web.service
systemctl status cerg-poller cerg-web
```

El tablero queda accesible en **`http://<IP-del-CT>:8000/`**. gunicorn escucha
en `0.0.0.0:8000`. El reverse proxy / publicación hacia el resto de la red se
hace en el firewall **IPFire** (no en este contenedor); ver
`deploy/nginx-cerg-cdc.conf` como referencia para la config de SSE del proxy.

## Despliegue genérico en Ubuntu Server

```bash
sudo ./deploy/install.sh
```

El script crea el usuario de servicio, copia la app a `/opt/cerg-cdc`, arma el
venv, instala las unidades systemd y deja una plantilla de entorno en
`/etc/cerg-cdc/cerg-cdc.env`. Luego seguí los pasos manuales que imprime
(crear base/rol, editar la clave, habilitar servicios, nginx opcional).

Comprobación rápida:

```bash
systemctl status cerg-poller cerg-web
journalctl -u cerg-poller -f      # ver ciclos del poller
curl -s localhost:8000/healthz
```

## Problemas resueltos en el primer despliegue (referencia)

Cinco puntos que dieron trabajo la primera vez y ya quedaron corregidos en el
código/scripts. Si algo similar reaparece, acá está el diagnóstico:

1. **`failed to resolve host 'cerg'`** — la clave de la base tenía caracteres
   reservados de URL (`/`, `+`) y rompía la URL de conexión. `config.py` ahora
   hace URL-encoding de usuario y clave; cualquier caracter es válido.
2. **`invalid integer value ... for connection option "port"`** — misma causa
   (el `/` de la clave partía la URL). Resuelto por lo anterior.
3. **`'ascii' codec can't encode ...`** — locale del contenedor en C/ASCII.
   `schema.sql` se reescribió en ASCII puro y el pool fuerza `client_encoding=UTF8`.
   Además el bootstrap configura locale es_AR.UTF-8.
4. **`unsupported Unicode escape sequence ... SQL_ASCII`** — la base se había
   creado con encoding SQL_ASCII. El bootstrap ahora la crea con `ENCODING 'UTF8'
   TEMPLATE template0`. Verificar con `psql -l | grep cerg_cdc`.
5. **`syntax error at or near "$1"` en `NOTIFY`** — el comando `NOTIFY` no acepta
   parámetros enlazados. Se cambió por `SELECT pg_notify(canal, payload)`.

Y un punto de red: gunicorn escucha en `0.0.0.0:8000`. El reverse proxy y la
publicacion hacia el resto de la red se hacen en el firewall **IPFire**; ver
`deploy/nginx-cerg-cdc.conf` como referencia (lo critico es `proxy_buffering off`
en `/api/stream` para que el SSE no se retrase).

## Notas de operación

- **Encoding de la base:** debe ser **UTF8**. En un LXC recién creado con locale
  C, `CREATE DATABASE` sin `ENCODING` explícito hereda `SQL_ASCII` de
  `template1`, y PostgreSQL rechaza guardar JSON con caracteres no-ASCII
  (síntoma: `unsupported Unicode escape sequence ... SQL_ASCII`). El bootstrap
  ya crea la base con `ENCODING 'UTF8' TEMPLATE template0`. Verificar con:
  `sudo -u postgres psql -l | grep cerg_cdc`  (columna Encoding = UTF8).
  Si quedó en SQL_ASCII y la base no tiene datos, recrearla:
  ```
  sudo -u postgres psql -c "DROP DATABASE cerg_cdc;"
  sudo -u postgres psql -c "CREATE DATABASE cerg_cdc OWNER cerg ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0;"
  ```
- **SSE + nginx:** el `location /api/stream` debe tener `proxy_buffering off`
  (ya viene en `deploy/nginx-cerg-cdc.conf`), si no los eventos llegan tarde.
- **Gunicorn:** usar worker `gevent` (no `sync`) y `--timeout 0`, porque las
  conexiones SSE son de larga duración.
- **Un solo poller:** no correr `app.poller` en más de un proceso; duplicaría
  las consultas al SCADA y las inserciones (aunque el `ON CONFLICT DO NOTHING`
  evita filas duplicadas, el tráfico al SCADA sí se duplicaría).
