"""
Poller — el ÚNICO proceso que habla con ScadaVision.

Bucle: consulta /api/protection y /api/genprotection cada CDC_POLL_INTERVAL
segundos, normaliza con app.normalize, persiste (snapshot + muestras crudas +
eventos de alarma) y ejecuta NOTIFY para que el web empuje por SSE.

Corre como servicio systemd independiente del web (cerg-poller.service).
Así, aunque el web tenga varios workers de Gunicorn, hay UN solo poller.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone

import httpx

from . import config, db
from .normalize import build_snapshot

logging.basicConfig(
    level=os.environ.get("CDC_LOG_LEVEL", "INFO"),
    format="%(asctime)s [poller] %(levelname)s %(message)s",
)
log = logging.getLogger("cerg.poller")

_running = True


def _stop(signum, frame):
    global _running
    log.info("señal %s recibida, deteniendo…", signum)
    _running = False


def fetch_all(client: httpx.Client) -> tuple[list, list]:
    """Consulta ambos endpoints. Lanza excepción si alguno falla."""
    r_feeders = client.get(f"{config.SCADA_BASE}/api/protection",
                           timeout=config.SCADA_TIMEOUT)
    r_feeders.raise_for_status()
    r_gens = client.get(f"{config.SCADA_BASE}/api/genprotection",
                        timeout=config.SCADA_TIMEOUT)
    r_gens.raise_for_status()
    return r_feeders.json(), r_gens.json()


def one_cycle(client: httpx.Client) -> None:
    raw_feeders, raw_gens = fetch_all(client)
    ts = datetime.now(timezone.utc)
    snapshot = build_snapshot(raw_feeders, raw_gens, ts=ts)

    db.write_samples(snapshot)      # muestras crudas (gen + celdas)
    changes = db.sync_alarms(snapshot)  # eventos de alarma (solo cambios)
    db.write_snapshot(snapshot)     # snapshot actual + NOTIFY

    t = snapshot["totals"]
    msg = (f"ok · gen {t['genMW']:.2f} MW · dist {t['distMW']:.2f} MW · "
           f"{t['gensRunning']} TG · {t['alarmsActive']} alarmas")
    if changes:
        msg += f" · {changes} cambio(s) de alarma"
    log.info(msg)


def main() -> int:
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    # Esquema idempotente al arrancar (útil en primer despliegue).
    schema_path = os.path.join(os.path.dirname(__file__), "..", "sql", "schema.sql")
    schema_path = os.path.abspath(schema_path)
    try:
        db.init_schema(schema_path)
        log.info("esquema verificado")
    except Exception as e:  # noqa: BLE001
        log.error("no se pudo inicializar el esquema: %s", e)
        return 1

    backoff = config.POLL_INTERVAL
    with httpx.Client(headers={"Cache-Control": "no-store"}) as client:
        while _running:
            start = time.monotonic()
            try:
                one_cycle(client)
                backoff = config.POLL_INTERVAL  # reset tras éxito
            except Exception as e:  # noqa: BLE001
                log.warning("fallo de ciclo: %s", e)
                backoff = min(backoff * 1.5, config.POLL_MAX_BACKOFF)

            elapsed = time.monotonic() - start
            sleep_for = max(0.0, backoff - elapsed)
            # Duerme en tramos cortos para responder rápido a SIGTERM.
            while _running and sleep_for > 0:
                chunk = min(0.5, sleep_for)
                time.sleep(chunk)
                sleep_for -= chunk

    log.info("poller detenido limpiamente")
    return 0


if __name__ == "__main__":
    sys.exit(main())
