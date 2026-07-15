"""
Web — Flask + gevent.

Sirve el tablero y expone:
  GET /                 -> HTML del dashboard
  GET /api/celdas       -> mapeo Id -> {tipo, nombre} (dato de referencia)
  GET /api/state        -> último snapshot (lee caché en DB, NO consulta SCADA)
  GET /api/trend?range= -> trend histórico de generación
  GET /api/trend/feeder?id=&range= -> trend histórico de una celda
  GET /api/alarms       -> últimos eventos de alarma
  GET /api/stream       -> SSE: empuja cada snapshot nuevo (LISTEN/NOTIFY)
  GET /api/connections  -> monitor de conexiones SSE (protegido por contraseña)
  GET /healthz          -> chequeo de salud

Ningún endpoint consulta ScadaVision: todo sale de PostgreSQL, que llena el
poller. Con esto, N navegadores = 0 consultas extra al SCADA.
"""
from __future__ import annotations

import hmac
import json
import os
import time
import uuid

import psycopg
from flask import Flask, Response, jsonify, request, send_from_directory, stream_with_context

from . import celdas, config, db

_HERE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(_HERE, "static"))


@app.route("/")
def index():
    # El dashboard es un único HTML autocontenido; se sirve tal cual.
    return send_from_directory(os.path.join(_HERE, "templates"), "index.html")


@app.route("/healthz")
def healthz():
    try:
        with db.get_pool().connection() as conn:
            conn.execute("SELECT 1")
        return jsonify(status="ok"), 200
    except Exception as e:  # noqa: BLE001
        return jsonify(status="error", detail=str(e)), 503


@app.route("/api/celdas")
def api_celdas():
    """Mapeo Id -> {tipo, nombre} de las celdas (dato de referencia estático).
    El front lo usa como respaldo de nombres; los snapshots ya traen el nombre
    inyectado, así que este endpoint es sobre todo para el modo demo y consumo
    externo."""
    return jsonify(celdas.CELDAS)


@app.route("/api/state")
def api_state():
    snap = db.read_snapshot()
    if not snap:
        return jsonify(error="sin datos todavía"), 503
    return jsonify(snap)


@app.route("/api/trend")
def api_trend():
    try:
        rng = int(request.args.get("range", config.TREND_DEFAULT_RANGE))
    except ValueError:
        rng = config.TREND_DEFAULT_RANGE
    rng = max(60, min(rng, config.TREND_MAX_RANGE))
    data = db.read_trend(rng, config.TREND_MAX_POINTS)
    return jsonify(data)


@app.route("/api/trend/feeder")
def api_trend_feeder():
    """Trend histórico de una sola celda. Igual que /api/trend pero filtrado
    por id de celda y con varias magnitudes (P, Q, I máx, U, fp)."""
    fid = (request.args.get("id") or "").strip()
    if not fid:
        return jsonify(error="falta el parámetro id"), 400
    try:
        rng = int(request.args.get("range", config.TREND_DEFAULT_RANGE))
    except ValueError:
        rng = config.TREND_DEFAULT_RANGE
    rng = max(60, min(rng, config.TREND_MAX_RANGE))
    return jsonify(db.read_feeder_trend(fid, rng, config.TREND_MAX_POINTS))


@app.route("/api/alarms")
def api_alarms():
    try:
        limit = int(request.args.get("limit", 50))
    except ValueError:
        limit = 50
    limit = max(1, min(limit, 500))
    return jsonify(db.read_recent_alarms(limit))


@app.route("/api/stream")
def api_stream():
    """SSE respaldado por LISTEN/NOTIFY.

    Abre una conexión dedicada (fuera del pool) en modo autocommit, hace
    LISTEN al canal, y cada vez que llega un NOTIFY relee el snapshot y lo
    empuja. Manda un evento inicial con el estado actual y keepalives
    periódicos para que proxies no corten la conexión.

    Registra la conexión en sse_connection (monitor de conexiones): alta al
    abrir, refresco de last_seen en cada keepalive, baja al cerrar.
    """
    # Datos del cliente: se capturan acá (dentro del contexto de request). El
    # reverse proxy corre en IPFire, así que la IP real viene en X-Forwarded-For.
    xff = request.headers.get("X-Forwarded-For", "")
    client_ip = xff.split(",")[0].strip() if xff else (request.remote_addr or None)
    user_agent = request.headers.get("User-Agent") or None
    session_id = uuid.uuid4().hex

    @stream_with_context
    def gen():
        # Estado inicial inmediato.
        snap = db.read_snapshot()
        if snap:
            yield _sse("snapshot", snap)

        try:
            db.sse_register(session_id, client_ip, user_agent)
        except Exception:  # noqa: BLE001
            pass  # el monitor es accesorio: no debe tumbar el stream

        conn = psycopg.connect(config.DATABASE_URL, autocommit=True)
        try:
            conn.execute(f"LISTEN {config.NOTIFY_CHANNEL}")
            last_keepalive = time.monotonic()
            while True:
                got = False
                # gen.notifies() con timeout: espera notificaciones o vence.
                for notify in conn.notifies(timeout=config.SSE_KEEPALIVE):
                    got = True
                    snap = db.read_snapshot()
                    if snap:
                        yield _sse("snapshot", snap)
                now = time.monotonic()
                if not got and (now - last_keepalive) >= config.SSE_KEEPALIVE:
                    yield ": keepalive\n\n"
                    last_keepalive = now
                    try:
                        db.sse_touch(session_id)
                    except Exception:  # noqa: BLE001
                        pass
                elif got:
                    last_keepalive = now
        finally:
            try:
                conn.execute(f"UNLISTEN {config.NOTIFY_CHANNEL}")
            except Exception:  # noqa: BLE001
                pass
            conn.close()
            try:
                db.sse_unregister(session_id)
            except Exception:  # noqa: BLE001
                pass

    headers = {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",   # desactiva buffering en nginx
        "Connection": "keep-alive",
    }
    return Response(gen(), headers=headers)


@app.route("/api/connections")
def api_connections():
    """Monitor de conexiones SSE en tiempo real, protegido por contraseña.

    La contraseña se pasa en la cabecera X-Monitor-Password (no en la URL, para
    no dejarla en logs/historial). Sin CDC_MONITOR_PASSWORD configurada el
    monitor queda desactivado (503): no se exponen datos sin contraseña.
    """
    pw = config.MONITOR_PASSWORD
    if not pw:
        return jsonify(error="monitor desactivado: falta CDC_MONITOR_PASSWORD"), 503
    given = request.headers.get("X-Monitor-Password", "")
    # Comparación en tiempo constante para no filtrar la clave por timing.
    if not hmac.compare_digest(given, pw):
        return jsonify(error="no autorizado"), 401
    try:
        return jsonify(db.read_connections(config.SSE_STALE_SECONDS))
    except Exception as e:  # noqa: BLE001
        return jsonify(error=f"no disponible: {e}"), 503


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


# Punto de entrada para gunicorn: "app.web:app"
