# CLAUDE.md — Contexto del proyecto CERG · CDC

> Este archivo lo lee Claude Code al abrir el repo. Resume qué es el proyecto,
> cómo está construido, las decisiones tomadas y las trampas que ya costó
> descubrir. Leelo entero antes de tocar código.

## Qué es

Servicio de monitoreo en tiempo real para el **Centro de Despacho de Cargas
(CDC)** de la **Cooperativa Eléctrica Río Grande (CERG)**, Tierra del Fuego,
Argentina. Muestra en un tablero web la generación (4 turbogeneradores TG1–TG4)
y la distribución (celdas de media tensión 13,2 kV) de la planta, leyendo de un
SCADA existente (ScadaVision).

El proyecto nació como un prototipo HTML autónomo y se portó a una arquitectura
cliente-servidor. La razón del port: en el prototipo, cada pestaña de navegador
consultaba el SCADA por su cuenta cada 5 s (N operadores = N×2 requests cada 5 s
a los relés, y el histórico del gráfico se perdía al cerrar la pestaña).

## Idioma

Todo el proyecto está en **español (es-AR)**: comentarios, mensajes de log,
textos de UI, mensajes de commit. Mantené ese idioma. El usuario (Rodolfo) se
comunica en español.

## Arquitectura

```
              1 conexión                              N navegadores
ScadaVision ◄── poller (systemd) ──► PostgreSQL ◄── web (gunicorn/gevent) ══SSE══► clientes
  /api/*      proceso independiente     histórico     lee, NO consulta SCADA
              normaliza + persiste     + snapshot
                     │                      ▲
                     └── NOTIFY (pg_notify) ─┘  LISTEN/NOTIFY = fan-out SSE, SIN Redis
```

**Dos procesos systemd separados a propósito.** Si el poller viviera dentro del
web, cada worker de Gunicorn tendría su propio poller y volveríamos al problema
de las consultas paralelas al SCADA. El poller es **único**; el web solo lee de
la base.

- **`cerg-poller.service`** — único proceso que habla con el SCADA. Bucle cada
  5 s: consulta `/api/protection` y `/api/genprotection`, normaliza, persiste
  (snapshot + muestras crudas + eventos de alarma) y hace `pg_notify`.
- **`cerg-web.service`** — Gunicorn con worker **gevent**, sirve el HTML y
  mantiene las conexiones SSE. Nunca toca el SCADA.

## Stack

- **Ubuntu 25.04** en un **LXC de Proxmox** (unprivileged, nesting=1). Es una
  release no-LTS; planificar upgrade a futuro.
- **PostgreSQL** (versión por defecto del release; sirve 16+). Comunicación
  poller↔web vía **LISTEN/NOTIFY** — decisión explícita de NO usar Redis.
- **Python 3.12**, Flask, gunicorn+gevent, psycopg 3 (con pool), httpx.
- **Front**: un único `index.html` autocontenido (HTML+CSS+JS, sin build).
  Consume el backend por SSE; tiene modo demo de fallback.

## Estructura del repo

```
app/
  normalize.py   # interpretación del SCADA (FUENTE DE VERDAD de los campos crudos)
  config.py      # configuración por variables de entorno
  db.py          # acceso a PostgreSQL (pool, escrituras, lecturas)
  poller.py      # servicio: SCADA -> normaliza -> persiste -> pg_notify
  web.py         # Flask: /, /api/state, /api/trend, /api/alarms, /api/stream, /healthz
  templates/index.html  # dashboard (SSE + modo demo)
sql/schema.sql   # esquema PostgreSQL (histórico crudo)
deploy/          # systemd units, env example, install.sh, lxc-bootstrap.sh, nginx (referencia)
tests_integration.py  # prueba de flujo con SQLite (sin Postgres real)
requirements.txt
README.md
```

## Endpoints del web

| Ruta | Descripción |
|------|-------------|
| `GET /` | Dashboard (sirve `templates/index.html` estático) |
| `GET /api/state` | Último snapshot normalizado (lee caché en DB) |
| `GET /api/trend?range=SEG` | Trend de generación por TG, con downsampling |
| `GET /api/trend/feeder?id=CELDA&range=SEG` | Trend histórico de UNA celda (P, Q, I máx, U, fp); mismo downsampling que el de generación |
| `GET /api/alarms?limit=N` | Últimos eventos de alarma |
| `GET /api/stream` | SSE: empuja cada snapshot nuevo (LISTEN/NOTIFY) |
| `GET /healthz` | Chequeo de salud |

**Ningún endpoint consulta ScadaVision.** Todo sale de PostgreSQL.

## Dominio: cómo interpretar el SCADA (crítico)

Toda la lógica vive en `app/normalize.py`. Es el puerto fiel de la lógica que
estaba en el front. Puntos clave:

- **Unidades distintas por endpoint:**
  - `/api/protection` (celdas MT): `p`, `q` YA vienen en MW / MVAr.
  - `/api/genprotection` (generadores): `p`, `q`, `s` vienen en **W / VA crudos**
    → se dividen por `1e6`.
- **Celdas (feeders):** 22 celdas de 13,2 kV. `A0`–`A17` alimentadores, `SA1`/`SA2`
  auxiliares (`mode==2`), `AC` acople de barras. Doble juego de barras (A/B):
  `seca_cerrado`/`secb_cerrado` indican a qué barra está conectada cada celda.
- **Generadores:** TG1–TG4. Doble juego de TI: línea (`irl/isl/itl`) y neutro
  (`irn/isn/itn`), esquema 87G. `intPie_cerrado` = pie de máquina (acople físico).
- **Pie de máquina = potencia real:** si `intPie_cerrado` es **false**, la máquina
  está desacoplada y su P/Q/S se fuerzan a **0** (el SCADA a veces reporta valores
  espurios de potencia con la máquina parada). Esta regla está en `norm_gen`.
- **Alarmas:** `alarmList` con `eventBitNumber`, `alarm_active`, `alarm_ack`, y
  `datetime` en **ms desde la época del relé (01/01/2000 UTC)** — no la época Unix.
  Ver `relay_datetime()`.
- **Anomalía conocida — TG4:** los TI de lado línea leen ~0 mientras el neutro
  lee ~1200 A con la máquina generando. Es un problema de cableado/config del TI
  de línea, no una falla real. `norm_gen` detecta esta discrepancia
  (`tiMismatch`) y el front la marca con un badge de advertencia.

## Base de datos

- `current_snapshot` — fila única (JSONB) con el último snapshot. El poller la
  reescribe y dispara `pg_notify`.
- `gen_sample` — una fila por TG por ciclo (trend de generación).
- `feeder_sample` — una fila por celda por ciclo.
- `alarm_event` / `alarm_state` — eventos de alarma; se registra SOLO cuando
  cambia el estado (activa/reconocida), no una fila por ciclo.

**Retención:** por ahora se guarda TODO crudo (decisión tomada). A ~5 s/ciclo,
`feeder_sample` crece ~450k filas/día. Postgres lo maneja por meses. Cuando haga
falta, ver la NOTA en `sql/schema.sql` (particionado / downsampling / TimescaleDB).

## Trampas ya descubiertas (NO repetir)

Estas costaron un ciclo de debugging cada una. Están resueltas en el código; si
algo similar reaparece, acá está el diagnóstico:

1. **Credenciales con caracteres especiales en la URL de conexión.** Si la clave
   de la base tiene `/`, `+`, `@`, etc., rompe la URL y libpq malinterpreta el
   host (`failed to resolve host 'cerg'`) o el puerto. `config.py` hace
   URL-encoding de usuario y clave. NO revertir eso.
2. **Locale ASCII del contenedor.** Un LXC recién creado suele estar en locale C.
   `schema.sql` está en ASCII puro y el pool fuerza `client_encoding=UTF8`. El
   `.env` debe tener `LANG`/`LC_ALL` en `es_AR.UTF-8`.
3. **Base creada en SQL_ASCII.** `CREATE DATABASE` sin `ENCODING` explícito hereda
   SQL_ASCII de `template1` y rechaza JSON con caracteres no-ASCII (los `·` de
   `protBits`). La base DEBE ser UTF8: `CREATE DATABASE ... ENCODING 'UTF8'
   LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0;`. Verificar con `psql -l`.
4. **`NOTIFY` no acepta parámetros enlazados.** Usar `SELECT pg_notify(canal,
   payload)`, no `NOTIFY canal, $1`. Ya está en `db.py`.
5. **Pool con `configure` que dejaba la conexión en transacción (INTRANS).** No
   agregar hooks `configure` que ejecuten SQL sin cerrar la transacción; el
   `client_encoding` va en los `kwargs` del pool, no en un hook.

## PENDIENTE / próximas tareas

- **[BUG de despliegue] Verificar el bind en `deploy/cerg-web.service`.** Debe
  decir `--bind 0.0.0.0:8000` (NO `127.0.0.1`). El reverse proxy corre en el
  firewall **IPFire**, no en este contenedor, así que gunicorn tiene que escuchar
  en todas las interfaces. Históricamente el `install.sh` revirtió este valor
  porque el repo tenía `127.0.0.1`: **confirmar que el repo ya tiene `0.0.0.0`**
  para que `install.sh` no lo vuelva a pisar. (Si el server sigue necesitando el
  cambio manual, es porque este archivo del repo quedó viejo.)
- **Reverse proxy en IPFire:** `deploy/nginx-cerg-cdc.conf` es solo REFERENCIA
  para configurar el proxy en el IPFire. Lo crítico para el SSE:
  `proxy_buffering off` en `location /api/stream`, si no las actualizaciones en
  vivo se retrasan o no llegan.
- **Forma final del tablero:** el trabajo de UI sigue en curso. Últimos cambios:
  selector de ventana del trend (1h/6h/12h/24h), filtro de generadores detenidos
  y **detalle histórico por celda**: al hacer clic en una fila de la tabla de
  distribución o en una celda del unifilar se abre un modal (`#feederDetail`) con
  los valores actuales y un gráfico histórico. Consume `GET /api/trend/feeder`
  (una consulta trae P/Q/Imáx/U; el selector de magnitud alterna sin re-consultar,
  el de ventana sí re-consulta). En modo demo el modal avisa que el histórico
  requiere backend. Los generadores siguen usando su tarjeta (clic en el unifilar
  hace scroll a la card), no el modal.
- **Histórico viejo con spikes:** las muestras espurias guardadas antes del filtro
  del pie de máquina siguen en `gen_sample`. El front no las dibuja (corta la
  línea en <=0.05 MW), pero si se quiere limpiar de verdad, correr un UPDATE.

## Flujo de trabajo

- Repo en GitHub: `https://github.com/rpalaucooprg/CergCDC.git` (público por ahora).
- Clon de trabajo en el servidor: `/var/cerg-cdc`. La app corre desde
  `/opt/cerg-cdc` (ahí la copia `install.sh`).
- Ciclo de despliegue:
  ```bash
  cd /var/cerg-cdc && git pull
  sudo ./deploy/install.sh
  sudo systemctl restart cerg-poller cerg-web   # reiniciar solo lo que cambió
  ```
- **`install.sh` respeta el `.env` existente** (`/etc/cerg-cdc/cerg-cdc.env`),
  no lo sobrescribe. La clave de la base vive ahí, FUERA del repo (`.gitignore`).

## Cómo probar sin infraestructura

- `python tests_integration.py` — ejercita normalización → persistencia →
  endpoints con un doble SQLite en memoria (no requiere Postgres).
- Sin backend, el front entra solo en **modo demostración** con datos de ejemplo
  claramente marcados (útil para trabajar la UI sin servidor).

## Convenciones

- Mantené el estilo existente: comentarios en español, mensajes de log con
  prefijo `[poller]`, textos de UI en es-AR.
- El front NO usa build ni frameworks: es HTML+CSS+JS plano en un solo archivo.
  Los cambios de UI se hacen directo en `templates/index.html`.
- Antes de dar por bueno un cambio de lógica del SCADA, validá contra los valores
  reales conocidos (ej. balance gen≈dist, TG4 con discrepancia de TI).
