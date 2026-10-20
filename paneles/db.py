"""
Conexion a la base de datos de los paneles.

Una sola base SQLite local (paneles/boleteria.db) al lado del backend. No hay
servidor de base de datos que instalar: esto corre en la misma maquina que
api_ventas.py y se versiona el esquema, no el archivo .db (ver .gitignore).

Por que SQLite y no Postgres: el bot vende como maximo un pasaje a la vez
(el pool de JELAF es de tamano 1), o sea unas pocas escrituras por minuto en
el peor caso. SQLite en modo WAL sobra, y el dia que haya que migrar, el
esquema es SQL estandar. La decision esta explicada en
docs/PLAN_PANELES.md#por-que-sqlite.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
RUTA_DB = Path(os.getenv("PANELES_DB", BASE_DIR / "boleteria.db"))
RUTA_ESQUEMA = BASE_DIR / "esquema.sql"

# Columnas agregadas despues de la primera version del esquema. esquema.sql
# solo tiene CREATE ... IF NOT EXISTS (se ejecuta en cada arranque) y un
# ALTER TABLE ADD COLUMN falla si la columna ya existe, asi que las columnas
# nuevas se agregan aqui, solo si faltan.
_COLUMNAS_NUEVAS = [
    ("boleto", "nro_boleto", "TEXT"),
    ("boleto", "link_comprobante", "TEXT"),   # comprobante electrónico de JELAF (LinkPag_FE)
    ("pago", "transacciones_anteriores", "TEXT"),
    # Pago con tarjeta (Niubiz): proveedor del cobro y resumen de la autorizacion.
    ("pago", "proveedor", "TEXT"),
    ("pago", "autorizacion", "TEXT"),
    # Yupy /v1/checkouts: {payment_order_uid: checkout_token} de cada checkout de
    # la venta; con el token se consulta el estado del pago (browser/status).
    ("pago", "checkout_tokens", "TEXT"),
    # Atencion de incidencias: estado del caso y ultima actualizacion.
    ("incidencia", "estado", "TEXT NOT NULL DEFAULT 'abierta'"),
    ("incidencia", "actualizada_en", "TEXT"),
    # Bonos personales: el codigo solo sirve para ese documento.
    ("codigo_descuento", "documento_exclusivo", "TEXT"),
    ("codigo_descuento", "regla_id", "INTEGER"),
    ("codigo_descuento", "lote_id", "INTEGER"),
    ("codigo_descuento", "hito", "TEXT"),
    # Factura (2026-10-13): boleta | factura y los datos del RUC, para el PDF.
    # Un RUC 10 es de una persona: dato personal, como el DNI.
    ("venta", "comprobante", "TEXT"),
    ("venta", "ruc_cliente", "TEXT"),
    ("venta", "razon_social", "TEXT"),
    ("venta", "direccion_fiscal", "TEXT"),
    # Boletos por correo (2026-10-14): adonde se mandaron (dato personal) y el embarque.
    ("venta", "correo", "TEXT"),
    ("venta", "embarque_lugar", "TEXT"),
    ("venta", "embarque_hora", "TEXT"),
    # Estado del correo (pendiente | enviado | fallido), motivo para el cliente y
    # cuántas veces lo corrigió/reenvió desde la pantalla final.
    ("venta", "correo_estado", "TEXT"),
    ("venta", "correo_motivo", "TEXT"),
    ("venta", "correo_reenvios", "INTEGER NOT NULL DEFAULT 0"),
    # WhatsApp responde una vez al día por número (2026-10-15): fecha de Lima de la última respuesta.
    ("contacto", "respondido_el", "TEXT"),
]

# Indices sobre columnas de _COLUMNAS_NUEVAS: no pueden ir en esquema.sql
# porque en una base vieja la columna todavia no existe cuando corre el script.
_INDICES_NUEVOS = [
    "CREATE INDEX IF NOT EXISTS ix_inc_estado ON incidencia(estado, severidad)",
    "CREATE INDEX IF NOT EXISTS ix_codigo_exclusivo ON codigo_descuento(documento_exclusivo)",
    "CREATE INDEX IF NOT EXISTS ix_codigo_lote ON codigo_descuento(lote_id)",
    # Una regla da un solo bono por cliente y por hito (idempotente aunque se
    # vuelva a aplicar o la venta se registre dos veces).
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_bono_regla_hito "
    "ON codigo_descuento(regla_id, documento_exclusivo, hito) WHERE regla_id IS NOT NULL",
]

# Una conexion por hilo: FastAPI atiende con varios hilos y sqlite3 no permite
# compartir una conexion entre ellos.
_local = threading.local()


def conexion() -> sqlite3.Connection:
    """Devuelve la conexion de este hilo, creandola la primera vez."""
    con = getattr(_local, "con", None)
    # Si RUTA_DB cambio (los tests usan una base temporal por prueba), la
    # conexion vieja de este hilo apunta a otro archivo: se abre una nueva.
    if con is not None and getattr(_local, "ruta", None) != RUTA_DB:
        con.close()
        con = None
    if con is None:
        RUTA_DB.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(RUTA_DB, timeout=10, check_same_thread=False)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("PRAGMA synchronous = NORMAL")
        con.execute("PRAGMA foreign_keys = ON")
        _local.con = con
        _local.ruta = RUTA_DB
        _local.en_transaccion = False
    return con


def init_db() -> None:
    """Crea las tablas si no existen. Es idempotente: se puede llamar siempre."""
    con = conexion()
    con.executescript(RUTA_ESQUEMA.read_text(encoding="utf-8"))
    for tabla, columna, tipo in _COLUMNAS_NUEVAS:
        existentes = {f["name"] for f in con.execute(f"PRAGMA table_info({tabla})")}
        if columna not in existentes:
            con.execute(f"ALTER TABLE {tabla} ADD COLUMN {columna} {tipo}")
            if (tabla, columna) == ("incidencia", "estado"):
                # Las ya cerradas pasan al estado nuevo; las derivadas se
                # reconocian solo por el prefijo de la nota.
                con.execute(
                    """
                    UPDATE incidencia SET estado = CASE
                        WHEN nota_resolucion LIKE 'DERIVADA:%' THEN 'derivada'
                        ELSE 'resuelta' END
                    WHERE resuelta = 1
                    """
                )
    for sql in _INDICES_NUEVOS:
        con.execute(sql)
    con.commit()


@contextmanager
def transaccion():
    """
    Agrupa varias escrituras en una sola transaccion: o entran todas o
    ninguna (P6). Dentro del bloque, escribir() no hace commit por su cuenta.
    Anidable: solo la transaccion mas externa confirma o deshace.
    """
    con = conexion()
    if getattr(_local, "en_transaccion", False):
        yield con
        return
    if con.in_transaction:   # transaccion implicita del modulo sqlite3 sin cerrar
        con.commit()
    _local.en_transaccion = True
    try:
        con.execute("BEGIN")
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        _local.en_transaccion = False


def consultar(sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
    return conexion().execute(sql, params).fetchall()


def consultar_uno(sql: str, params: tuple | dict = ()):
    return conexion().execute(sql, params).fetchone()


def _ejecutar(sql: str, params) -> sqlite3.Cursor:
    con = conexion()
    cur = con.execute(sql, params)
    if not getattr(_local, "en_transaccion", False):
        con.commit()
    return cur


def escribir(sql: str, params: tuple | dict = ()) -> int:
    """Ejecuta un INSERT/UPDATE/DELETE y devuelve el lastrowid."""
    return _ejecutar(sql, params).lastrowid


def escribir_contando(sql: str, params: tuple | dict = ()) -> int:
    """Como escribir(), pero devuelve cuantas filas cambio (rowcount)."""
    return _ejecutar(sql, params).rowcount


def fila_a_dict(fila: sqlite3.Row | None) -> dict | None:
    return dict(fila) if fila is not None else None


def filas_a_dict(filas) -> list[dict]:
    return [dict(f) for f in filas]


if __name__ == "__main__":
    init_db()
    tablas = consultar(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    print(f"Base creada en {RUTA_DB}")
    for t in tablas:
        print("  -", t["name"])
