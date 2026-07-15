-- =====================================================================
-- CERG - CDC - Esquema de base de datos (historico crudo)
-- PostgreSQL 12+
--
-- Se guardan muestras crudas cada ciclo de poll. Sin politica de
-- retencion por ahora (decision: "guardar todo crudo"); ver NOTA al final.
-- =====================================================================

-- ------------------------------------------------------------------
-- Snapshot actual: una sola fila que el poller reescribe cada ciclo.
-- El web lo lee para GET /api/state y para el primer render de un SSE.
-- Guardamos el JSON ya normalizado (build_snapshot) para no re-derivar.
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS current_snapshot (
    id          SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    ts          TIMESTAMPTZ NOT NULL,
    payload     JSONB       NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ------------------------------------------------------------------
-- Trend de generacion: una fila por TG por ciclo.
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gen_sample (
    ts          TIMESTAMPTZ NOT NULL,
    tg_id       TEXT        NOT NULL,
    p_mw        REAL,
    q_mvar      REAL,
    s_mva       REAL,
    fp          REAL,
    i_linea     REAL,
    i_neutro    REAL,
    running     BOOLEAN,
    PRIMARY KEY (ts, tg_id)
);
CREATE INDEX IF NOT EXISTS idx_gen_sample_ts ON gen_sample (ts DESC);
CREATE INDEX IF NOT EXISTS idx_gen_sample_tg_ts ON gen_sample (tg_id, ts DESC);

-- ------------------------------------------------------------------
-- Muestras de celdas de MT: una fila por celda por ciclo.
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS feeder_sample (
    ts          TIMESTAMPTZ NOT NULL,
    feeder_id   TEXT        NOT NULL,
    p_mw        REAL,
    q_mvar      REAL,
    i_max       REAL,
    v_ll        REAL,
    fp          REAL,
    fr          REAL,
    closed      BOOLEAN,
    bus         TEXT,
    state       TEXT,
    PRIMARY KEY (ts, feeder_id)
);
CREATE INDEX IF NOT EXISTS idx_feeder_sample_ts ON feeder_sample (ts DESC);
CREATE INDEX IF NOT EXISTS idx_feeder_sample_fid_ts ON feeder_sample (feeder_id, ts DESC);

-- ------------------------------------------------------------------
-- Eventos de alarma: se inserta SOLO cuando cambia el estado de una
-- alarma (activa/reconocida), no una fila por ciclo. Ver upsert en el
-- poller: se compara contra el ultimo estado conocido por (cell, bit).
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alarm_event (
    id          BIGSERIAL PRIMARY KEY,
    ts_ingest   TIMESTAMPTZ NOT NULL DEFAULT now(),
    ts_relay    TIMESTAMPTZ,              -- fecha del rele (epoca 2000), puede ser NULL
    cell_id     TEXT        NOT NULL,
    event_bit   INTEGER     NOT NULL,
    active      BOOLEAN     NOT NULL,
    ack         BOOLEAN     NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alarm_event_ts ON alarm_event (ts_ingest DESC);
CREATE INDEX IF NOT EXISTS idx_alarm_event_cell ON alarm_event (cell_id, event_bit);

-- Estado vigente de cada alarma por (celda, bit): permite detectar cambios
-- sin recorrer toda la tabla de eventos.
CREATE TABLE IF NOT EXISTS alarm_state (
    cell_id     TEXT    NOT NULL,
    event_bit   INTEGER NOT NULL,
    active      BOOLEAN NOT NULL,
    ack         BOOLEAN NOT NULL,
    ts_relay    TIMESTAMPTZ,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (cell_id, event_bit)
);

-- ------------------------------------------------------------------
-- Conexiones SSE activas (monitor de conexiones en tiempo real).
-- El web registra una fila al abrir un stream, refresca last_seen en cada
-- keepalive y la borra al cerrar. Vive en la base (no en memoria) porque hay
-- varios workers de gunicorn y cada uno veria solo sus propias conexiones.
-- Las filas huerfanas (cliente caido sin cierre limpio) se purgan por last_seen.
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sse_connection (
    session_id   TEXT        PRIMARY KEY,
    ip           TEXT,
    user_agent   TEXT,
    connected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sse_conn_seen ON sse_connection (last_seen DESC);

-- ------------------------------------------------------------------
-- Canal de notificacion para SSE (LISTEN/NOTIFY).
-- El poller ejecuta:  NOTIFY cdc_snapshot;  al terminar cada ciclo.
-- El web hace LISTEN cdc_snapshot y reenvia por SSE.
-- No requiere tabla; se documenta aca el nombre del canal.
-- ------------------------------------------------------------------

-- =====================================================================
-- NOTA sobre retencion (pendiente de decidir):
-- A ~5 s por ciclo, feeder_sample crece ~450k filas/dia y gen_sample
-- ~70k/dia. Postgres lo maneja sin problema por meses, pero conviene
-- definir a futuro una de estas estrategias:
--   (a) particionado por rango de tiempo (pg_partman / PARTITION BY RANGE)
--   (b) downsampling: crudo N dias + agregados por minuto para lo viejo
--   (c) TimescaleDB (hypertables + retention policies)
-- Por ahora: TODO CRUDO. Dejar este bloque como recordatorio.
-- =====================================================================
