"""
Nombres y tipos de las celdas del CDC — dato de REFERENCIA, no del SCADA.

El SCADA identifica cada celda por su Id (A0, A1, …, SA1, SA2, AC, TG1…TG4).
Los operadores, en cambio, las conocen por el nombre de la línea de distribución
(Mosconi, Irigoyen, Rio Chico 1, …). Este módulo carga ese mapeo desde el
archivo auxiliar `celdas.json` y lo expone para que `build_snapshot` inyecte el
nombre en cada celda y generador del snapshot.

`celdas.json` es editable a mano (sin tocar código): agregar/renombrar una línea
es cambiar una fila del JSON. Si el archivo falta o está mal formado, se degrada
a un mapeo vacío (el sistema sigue funcionando, solo sin nombres).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

log = logging.getLogger("cerg.celdas")

_HERE = os.path.dirname(os.path.abspath(__file__))
_PATH = os.path.join(_HERE, "celdas.json")


def _load() -> dict[str, dict[str, Any]]:
    try:
        with open(_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
        log.warning("celdas.json no es un objeto; se ignora")
    except FileNotFoundError:
        log.warning("celdas.json no encontrado en %s; sin nombres de línea", _PATH)
    except (OSError, json.JSONDecodeError) as e:  # noqa: BLE001
        log.warning("no se pudo leer celdas.json: %s", e)
    return {}


# Se carga una vez al importar. El poller es un proceso de larga vida; si se
# edita el JSON, reiniciar el servicio (o recargar) toma los cambios.
CELDAS: dict[str, dict[str, Any]] = _load()


def nombre(cell_id: str) -> str | None:
    """Nombre de línea para un Id de celda, o None si no está mapeado."""
    return (CELDAS.get(cell_id) or {}).get("nombre") or None


def tipo(cell_id: str) -> str | None:
    """Tipo descriptivo (Alimentador, Generador, …) para un Id, o None."""
    return (CELDAS.get(cell_id) or {}).get("tipo") or None
