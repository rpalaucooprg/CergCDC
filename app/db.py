"""
Capa de acceso a PostgreSQL.

Usa psycopg (v3) con un pool de conexiones. Concentra todas las escrituras
(poller) y lecturas (web) para que el SQL viva en un solo lugar.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import config

_pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        # client_encoding=UTF8 en kwargs fuerza la codificacion al abrir cada
        # conexion, sin depender del locale del sistema ni ejecutar un SET
        # posterior (que podria dejar la conexion en transaccion).
        _pool = ConnectionPool(
            conninfo=config.DATABASE_URL,
            min_size=1,
            max_size=8,
            kwargs={"row_factory": dict_row, "client_encoding": "UTF8"},
            open=True,
        )
    return _pool


def init_schema(schema_sql_path: str) -> None:
    """Aplica sql/schema.sql (idempotente: usa CREATE TABLE IF NOT EXISTS)."""
    with open(schema_sql_path, "r", encoding="utf-8") as fh:
        ddl = fh.read()
    with get_pool().connection() as conn:
        conn.execute(ddl)
        conn.commit()


# ------------------------------------------------------------------
# Escrituras (poller)
# ------------------------------------------------------------------
def write_snapshot(snapshot: dict[str, Any]) -> None:
    """Reescribe la fila única de current_snapshot y dispara NOTIFY."""
    ts = snapshot["ts"]
    payload = json.dumps(snapshot)
    with get_pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO current_snapshot (id, ts, payload, updated_at)
            VALUES (1, %s, %s, now())
            ON CONFLICT (id) DO UPDATE
              SET ts = EXCLUDED.ts,
                  payload = EXCLUDED.payload,
                  updated_at = now()
            """,
            (ts, payload),
        )
        # pg_notify(canal, payload): la funcion SI acepta parametros enlazados.
        # El comando NOTIFY (sintaxis SQL) no admite $1, de ahi el error previo.
        conn.execute("SELECT pg_notify(%s, %s)", (config.NOTIFY_CHANNEL, ts))
        conn.commit()


def write_samples(snapshot: dict[str, Any]) -> None:
    """Inserta muestras crudas de generación y celdas para este ciclo."""
    ts = snapshot["ts"]
    gens = snapshot["gens"]
    feeders = snapshot["feeders"]

    gen_rows = [
        (ts, g["id"], g["pMW"], g["qMVAr"], g["sMVA"], g["fp"],
         g["iL"], g["iN"], g["running"])
        for g in gens
    ]
    feeder_rows = [
        (ts, f["id"], f["p"], f["q"], f["imax"], f["vll"], f["fp"], f["fr"],
         f["closed"], f["bus"], f["state"])
        for f in feeders
    ]

    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO gen_sample
                  (ts, tg_id, p_mw, q_mvar, s_mva, fp, i_linea, i_neutro, running)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (ts, tg_id) DO NOTHING
                """,
                gen_rows,
            )
            cur.executemany(
                """
                INSERT INTO feeder_sample
                  (ts, feeder_id, p_mw, q_mvar, i_max, v_ll, fp, fr, closed, bus, state)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (ts, feeder_id) DO NOTHING
                """,
                feeder_rows,
            )
        conn.commit()


def sync_alarms(snapshot: dict[str, Any]) -> int:
    """Detecta cambios de alarma comparando contra alarm_state y registra un
    alarm_event solo cuando cambia (active/ack). Devuelve cuántos eventos
    nuevos se registraron."""
    from .normalize import relay_datetime

    changes = 0
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            for f in snapshot["feeders"]:
                cell = f["id"]
                for a in f.get("alarms", []):
                    bit = a.get("eventBitNumber")
                    if bit is None:
                        continue
                    active = bool(a.get("alarm_active"))
                    ack = bool(a.get("alarm_ack"))
                    ts_relay = None
                    if a.get("datetime"):
                        ts_relay = relay_datetime(a["datetime"])

                    cur.execute(
                        "SELECT active, ack FROM alarm_state WHERE cell_id=%s AND event_bit=%s",
                        (cell, bit),
                    )
                    prev = cur.fetchone()
                    if prev is None or prev["active"] != active or prev["ack"] != ack:
                        cur.execute(
                            """
                            INSERT INTO alarm_event (ts_relay, cell_id, event_bit, active, ack)
                            VALUES (%s,%s,%s,%s,%s)
                            """,
                            (ts_relay, cell, bit, active, ack),
                        )
                        cur.execute(
                            """
                            INSERT INTO alarm_state (cell_id, event_bit, active, ack, ts_relay, updated_at)
                            VALUES (%s,%s,%s,%s,%s, now())
                            ON CONFLICT (cell_id, event_bit) DO UPDATE
                              SET active=EXCLUDED.active, ack=EXCLUDED.ack,
                                  ts_relay=EXCLUDED.ts_relay, updated_at=now()
                            """,
                            (cell, bit, active, ack, ts_relay),
                        )
                        changes += 1
        conn.commit()
    return changes


# ------------------------------------------------------------------
# Lecturas (web)
# ------------------------------------------------------------------
def read_snapshot() -> dict[str, Any] | None:
    with get_pool().connection() as conn:
        row = conn.execute(
            "SELECT payload FROM current_snapshot WHERE id=1"
        ).fetchone()
    if not row:
        return None
    payload = row["payload"]
    # psycopg devuelve JSONB ya deserializado (dict); por robustez toleramos str.
    return payload if isinstance(payload, dict) else json.loads(payload)


def read_trend(range_seconds: int, max_points: int) -> dict[str, Any]:
    """Devuelve el trend de generación agrupado por TG en la ventana dada.

    Downsampling en la consulta: se agrega por buckets de tiempo con
    time_bucket casero (date_trunc no da granularidad fina, así que se usa
    aritmética sobre epoch) para no devolver más de ~max_points por serie.
    """
    since = datetime.now(timezone.utc).timestamp() - range_seconds
    # Ancho de bucket para no exceder max_points en la ventana.
    bucket = max(1, int(range_seconds / max_points))

    sql = """
        SELECT
          to_timestamp(floor(extract(epoch FROM ts) / %(bucket)s) * %(bucket)s) AS bucket_ts,
          tg_id,
          avg(p_mw)   AS p_mw,
          bool_or(running) AS running
        FROM gen_sample
        WHERE ts >= to_timestamp(%(since)s)
        GROUP BY bucket_ts, tg_id
        ORDER BY bucket_ts ASC
    """
    with get_pool().connection() as conn:
        rows = conn.execute(sql, {"bucket": bucket, "since": since}).fetchall()

    # Reorganiza a { ts: [...], series: { TGx: [p,...] } } para el front.
    ts_index: dict[str, int] = {}
    times: list[str] = []
    series: dict[str, list] = {}
    for r in rows:
        key = r["bucket_ts"].isoformat()
        if key not in ts_index:
            ts_index[key] = len(times)
            times.append(key)
        series.setdefault(r["tg_id"], [None] * 0)

    # Asegura longitud alineada por serie.
    for tg in series:
        series[tg] = [None] * len(times)
    for r in rows:
        i = ts_index[r["bucket_ts"].isoformat()]
        series[r["tg_id"]][i] = round(r["p_mw"], 4) if r["p_mw"] is not None else None

    return {"ts": times, "series": series, "bucket_seconds": bucket}


def read_feeder_trend(feeder_id: str, range_seconds: int, max_points: int) -> dict[str, Any]:
    """Devuelve el trend histórico de UNA celda en la ventana dada.

    Mismo downsampling casero que read_trend (bucket por aritmética sobre
    epoch). Devuelve varias series (P, Q, I máx, U, fp) en un solo viaje para
    que el front pueda alternar la magnitud sin re-consultar. I máx se agrega
    con max() (nos interesa el pico del bucket, no el promedio); el resto con
    avg().
    """
    since = datetime.now(timezone.utc).timestamp() - range_seconds
    bucket = max(1, int(range_seconds / max_points))

    sql = """
        SELECT
          to_timestamp(floor(extract(epoch FROM ts) / %(bucket)s) * %(bucket)s) AS bucket_ts,
          avg(p_mw)   AS p_mw,
          avg(q_mvar) AS q_mvar,
          max(i_max)  AS i_max,
          avg(v_ll)   AS v_ll,
          avg(fp)     AS fp
        FROM feeder_sample
        WHERE feeder_id = %(fid)s AND ts >= to_timestamp(%(since)s)
        GROUP BY bucket_ts
        ORDER BY bucket_ts ASC
    """
    with get_pool().connection() as conn:
        rows = conn.execute(
            sql, {"bucket": bucket, "since": since, "fid": feeder_id}
        ).fetchall()

    times: list[str] = []
    series: dict[str, list] = {"p": [], "q": [], "imax": [], "vll": [], "fp": []}

    def _r(v, nd):
        return round(v, nd) if v is not None else None

    for r in rows:
        times.append(r["bucket_ts"].isoformat())
        series["p"].append(_r(r["p_mw"], 4))
        series["q"].append(_r(r["q_mvar"], 4))
        series["imax"].append(_r(r["i_max"], 1))
        series["vll"].append(_r(r["v_ll"], 4))
        series["fp"].append(_r(r["fp"], 4))

    return {"id": feeder_id, "ts": times, "series": series, "bucket_seconds": bucket}


# ------------------------------------------------------------------
# Conexiones SSE (monitor en tiempo real)
# ------------------------------------------------------------------
def sse_register(session_id: str, ip: str | None, user_agent: str | None) -> None:
    """Registra (o refresca) una conexión SSE activa."""
    with get_pool().connection() as conn:
        conn.execute(
            """
            INSERT INTO sse_connection (session_id, ip, user_agent, connected_at, last_seen)
            VALUES (%s, %s, %s, now(), now())
            ON CONFLICT (session_id) DO UPDATE
              SET last_seen = now()
            """,
            (session_id, ip, user_agent),
        )
        conn.commit()


def sse_touch(session_id: str) -> None:
    """Marca que la conexión sigue viva (se llama en cada keepalive)."""
    with get_pool().connection() as conn:
        conn.execute(
            "UPDATE sse_connection SET last_seen = now() WHERE session_id = %s",
            (session_id,),
        )
        conn.commit()


def sse_unregister(session_id: str) -> None:
    """Elimina una conexión SSE al cerrarse limpiamente."""
    with get_pool().connection() as conn:
        conn.execute("DELETE FROM sse_connection WHERE session_id = %s", (session_id,))
        conn.commit()


def read_connections(stale_seconds: int) -> list[dict[str, Any]]:
    """Lista de conexiones SSE vivas. Purga primero las huérfanas (clientes que
    se cayeron sin cierre limpio) usando last_seen."""
    with get_pool().connection() as conn:
        conn.execute(
            "DELETE FROM sse_connection WHERE last_seen < now() - make_interval(secs => %s)",
            (stale_seconds,),
        )
        conn.commit()
        rows = conn.execute(
            """
            SELECT session_id, ip, user_agent, connected_at, last_seen
            FROM sse_connection
            ORDER BY connected_at ASC
            """
        ).fetchall()
    out = []
    for r in rows:
        out.append({
            "id": (r["session_id"] or "")[:8],
            "ip": r["ip"],
            "userAgent": r["user_agent"],
            "connectedAt": r["connected_at"].isoformat() if r["connected_at"] else None,
            "lastSeen": r["last_seen"].isoformat() if r["last_seen"] else None,
        })
    return out


def read_recent_alarms(limit: int = 50) -> list[dict[str, Any]]:
    with get_pool().connection() as conn:
        rows = conn.execute(
            """
            SELECT ts_ingest, ts_relay, cell_id, event_bit, active, ack
            FROM alarm_event
            ORDER BY ts_ingest DESC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        out.append({
            "tsIngest": r["ts_ingest"].isoformat() if r["ts_ingest"] else None,
            "tsRelay": r["ts_relay"].isoformat() if r["ts_relay"] else None,
            "cell": r["cell_id"],
            "eventBit": r["event_bit"],
            "active": r["active"],
            "ack": r["ack"],
        })
    return out
