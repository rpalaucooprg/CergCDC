"""
Configuración central, leída de variables de entorno.

Todas las opciones se pasan por entorno (systemd EnvironmentFile o .env),
nunca hardcodeadas. Ver deploy/cerg-cdc.env.example.
"""
from __future__ import annotations

import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# --- SCADA ---
SCADA_BASE = os.environ.get("CDC_SCADA_BASE", "https://scadavision.cooprg.net").rstrip("/")
SCADA_TIMEOUT = _float("CDC_SCADA_TIMEOUT", 8.0)
POLL_INTERVAL = _float("CDC_POLL_INTERVAL", 5.0)          # segundos entre ciclos
POLL_MAX_BACKOFF = _float("CDC_POLL_MAX_BACKOFF", 60.0)   # backoff máximo ante fallos

# --- PostgreSQL ---
# Se acepta una URL completa o piezas sueltas.
DATABASE_URL = os.environ.get("CDC_DATABASE_URL")
if not DATABASE_URL:
    _user = os.environ.get("CDC_DB_USER", "cerg")
    _pass = os.environ.get("CDC_DB_PASSWORD", "")
    _host = os.environ.get("CDC_DB_HOST", "localhost")
    _port = os.environ.get("CDC_DB_PORT", "5432")
    _name = os.environ.get("CDC_DB_NAME", "cerg_cdc")
    # URL-encode: la clave puede tener /, +, @, : u otros caracteres reservados
    # de URL. Sin esto, la concatenación produce una URL malformada y libpq
    # interpreta mal el host (síntoma: "failed to resolve host 'cerg'").
    from urllib.parse import quote
    _u = quote(_user, safe="")
    if _pass:
        _auth = f"{_u}:{quote(_pass, safe='')}"
    else:
        _auth = _u
    DATABASE_URL = f"postgresql://{_auth}@{_host}:{_port}/{_name}"

# Canal LISTEN/NOTIFY para el fan-out de SSE.
NOTIFY_CHANNEL = os.environ.get("CDC_NOTIFY_CHANNEL", "cdc_snapshot")

# --- Web ---
# Ventana por defecto del trend al cargar la página (segundos).
TREND_DEFAULT_RANGE = _int("CDC_TREND_DEFAULT_RANGE", 8 * 3600)
TREND_MAX_RANGE = _int("CDC_TREND_MAX_RANGE", 7 * 24 * 3600)
# Máximo de puntos que devuelve /api/trend (downsampling en consulta).
TREND_MAX_POINTS = _int("CDC_TREND_MAX_POINTS", 600)
# Cada cuánto el SSE manda un keepalive si no hubo snapshot (segundos).
SSE_KEEPALIVE = _float("CDC_SSE_KEEPALIVE", 20.0)
