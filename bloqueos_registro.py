"""
bloqueos_registro.py — anota en disco cada asiento que el bot bloquea en JELAF.

Por qué existe (2026-10-09): los bloqueos de cada venta vivían solo en memoria
(VentaHttp.asientos_en_venta). Si el backend se caía o se reiniciaba con una
compra a medias, el asiento quedaba apartado en JELAF y nadie sabía su `IDS`
(el id que hace falta para soltarlo). Aquí queda escrito hasta que se suelta o
se vende, y `liberar_asientos.py` los muestra y permite liberarlos.

Solo códigos: cuenta de JELAF, Terminal, asiento, IDS, códigos del viaje y el
booking ENMASCARADO. Nunca nombres, documentos ni teléfonos. El archivo
(`bloqueos_jelaf.db`) está fuera del repositorio (`*.db` en .gitignore).

Nunca tumba una venta: si el disco falla, se avisa en el log y la venta sigue.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

log = logging.getLogger("tmt.bloqueos")

RUTA = Path("bloqueos_jelaf.db")

_ESQUEMA = """
CREATE TABLE IF NOT EXISTS bloqueo (
    ids          INTEGER PRIMARY KEY,   -- id del bloqueo en JELAF (lo que pide liberarArregloAsientos)
    cuenta       TEXT    NOT NULL,      -- usuario de JELAF que lo hizo (Y1, Y2…)
    terminal     INTEGER NOT NULL,
    asiento      TEXT    NOT NULL,      -- "05"
    programacion INTEGER NOT NULL,      -- CodiProgramacion del turno
    viaje        TEXT    NOT NULL,      -- JSON con los códigos de ViajeJelaf (para releer el plano)
    booking      TEXT    NOT NULL,      -- enmascarado (privacidad.booking)
    creado_en    REAL    NOT NULL       -- epoch
)
"""


def _conexion() -> sqlite3.Connection:
    con = sqlite3.connect(RUTA, timeout=5)
    con.execute(_ESQUEMA)
    return con


def anotar(*, ids: int, cuenta: str, terminal: int, asiento: str, programacion: int,
           viaje: dict, booking: str) -> None:
    try:
        with _conexion() as con:
            con.execute(
                "INSERT OR REPLACE INTO bloqueo (ids, cuenta, terminal, asiento, programacion, viaje, booking, creado_en)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (int(ids), cuenta, int(terminal), asiento, int(programacion),
                 json.dumps(viaje, ensure_ascii=False), booking, time.time()),
            )
    except (sqlite3.Error, OSError, ValueError, TypeError) as e:
        log.warning("No se pudo anotar el bloqueo del asiento %s: %s", asiento, type(e).__name__)


def quitar(ids: list[int]) -> None:
    reales = [int(i) for i in ids if i and int(i) > 0]
    if not reales:
        return
    try:
        with _conexion() as con:
            con.executemany("DELETE FROM bloqueo WHERE ids = ?", [(i,) for i in reales])
    except (sqlite3.Error, OSError) as e:
        log.warning("No se pudieron borrar %d bloqueo(s) del registro: %s", len(reales), type(e).__name__)


def listar() -> list[dict]:
    """Bloqueos que el bot hizo y todavía no soltó ni vendió, del más antiguo al más nuevo."""
    if not Path(RUTA).exists():
        return []
    with _conexion() as con:
        con.row_factory = sqlite3.Row
        filas = con.execute("SELECT * FROM bloqueo ORDER BY creado_en, ids").fetchall()
    return [{**dict(f), "viaje": json.loads(f["viaje"])} for f in filas]
