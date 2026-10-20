"""
quitar_cliente_prueba.py — sacar de los paneles las compras de PRUEBA de una
persona del equipo (por ejemplo, quien probó el bot con su DNI y aparece como
"mejor cliente").

Qué hace con ese documento:
  - Sus ventas de prueba (todas las del documento en las que NO hay otros
    pasajeros) pasan a estado 'anulada': salen de ventas, ingresos, rankings y
    fidelización. No se borran: son comprobantes que existieron en JELAF (y se
    anularon allá), y la fila sigue cuadrando con JELAF.
  - Sus boletos en esas ventas quedan sin datos personales (documento, nombres)
    y sin teléfono la venta.
  - Se borran su ficha de cliente y sus bonos/códigos personales.
  - Una venta donde viaja con OTRAS personas no se toca: se muestra para
    revisarla a mano (puede ser una compra real).

Antes de cambiar nada guarda una copia de la base (respaldo-AAAAMMDD-HHMMSS.db,
junto a la base; *.db no se sube al repositorio) y pide confirmación.

    python quitar_cliente_prueba.py --documento 12345678            # solo muestra qué haría
    python quitar_cliente_prueba.py --documento 12345678 --aplicar  # respalda, confirma y aplica
    python quitar_cliente_prueba.py --documento 12345678 --aplicar --db /ruta/boleteria.db

Cambia la base REAL de los paneles: lo ejecuta el usuario, en el servidor, con
el backend corriendo o detenido (SQLite con WAL lo admite). No toca JELAF ni las
pasarelas. En pantalla el documento sale enmascarado.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import privacidad
from paneles import db


def _ventas_con(documento: str) -> list[dict]:
    return db.filas_a_dict(db.consultar(
        """
        SELECT v.id, v.comprado_en, v.estado, v.monto_total, v.nro_boleto, v.origen, v.destino,
               SUM(CASE WHEN b.documento = ? THEN 1 ELSE 0 END) AS suyos,
               COUNT(b.id) AS pasajeros
        FROM venta v JOIN boleto b ON b.venta_id = v.id
        WHERE v.id IN (SELECT venta_id FROM boleto WHERE documento = ?)
        GROUP BY v.id ORDER BY v.comprado_en
        """, (documento, documento)))


def revisar(documento: str) -> dict:
    """Qué se cambiaría. Solo lectura."""
    ventas = _ventas_con(documento)
    return {
        "propias": [v for v in ventas if v["suyos"] == v["pasajeros"] and v["estado"] != "anulada"],
        "mixtas": [v for v in ventas if v["suyos"] < v["pasajeros"]],
        "cliente": db.consultar_uno("SELECT 1 FROM cliente WHERE documento = ?", (documento,)) is not None,
        "bonos": db.consultar_uno("SELECT COUNT(*) FROM codigo_descuento WHERE documento_exclusivo = ?",
                                  (documento,))[0],
        "envios": db.consultar_uno("SELECT COUNT(*) FROM codigo_enviado WHERE documento = ?", (documento,))[0],
    }


def respaldar(carpeta: Path | None = None) -> Path:
    """Copia consistente de la base (API de respaldo de SQLite, sirve con WAL) antes de cambiarla."""
    carpeta = Path(carpeta) if carpeta else Path(db.RUTA_DB).parent
    destino = carpeta / f"respaldo-{datetime.now():%Y%m%d-%H%M%S}.db"
    origen = sqlite3.connect(db.RUTA_DB)
    copia = sqlite3.connect(destino)
    try:
        origen.backup(copia)
    finally:
        copia.close()
        origen.close()
    return destino


def aplicar(documento: str) -> dict:
    """Todo en una transacción: o se aplica completo o no cambia nada."""
    plan = revisar(documento)
    ids = [v["id"] for v in plan["propias"]]
    marcas = ",".join("?" * len(ids))
    with db.transaccion():
        if ids:
            db.escribir_contando(f"UPDATE venta SET estado = 'anulada', telefono = NULL WHERE id IN ({marcas})",
                                 tuple(ids))
            db.escribir_contando(
                f"UPDATE boleto SET documento = NULL, nombres = 'PRUEBA', paterno = 'INTERNA', materno = NULL "
                f"WHERE venta_id IN ({marcas})", tuple(ids))
        db.escribir_contando("DELETE FROM codigo_enviado WHERE documento = ?", (documento,))
        db.escribir_contando("DELETE FROM codigo_descuento WHERE documento_exclusivo = ?", (documento,))
        borrado = db.escribir_contando("DELETE FROM cliente WHERE documento = ?", (documento,))
    return {"ventas_anuladas": len(ids), "cliente_borrado": borrado == 1,
            "bonos_borrados": plan["bonos"], "mixtas": [v["id"] for v in plan["mixtas"]]}


def _mostrar(plan: dict, visible: str) -> None:
    print(f"\nDocumento {visible}")
    print(f"  Ventas de prueba a anular: {len(plan['propias'])}")
    for v in plan["propias"]:
        print(f"    #{v['id']}  {v['comprado_en'][:16]}  {v['origen']} → {v['destino']}  "
              f"S/ {v['monto_total']:.2f}  boleto {v['nro_boleto'] or '—'}  ({v['estado']})")
    if plan["mixtas"]:
        print(f"  Ventas con OTROS pasajeros (no se tocan, revisar a mano): {len(plan['mixtas'])}")
        for v in plan["mixtas"]:
            print(f"    #{v['id']}  {v['comprado_en'][:16]}  {v['pasajeros']} pasajeros  boleto {v['nro_boleto'] or '—'}")
    print(f"  Ficha de cliente: {'sí, se borra' if plan['cliente'] else 'no hay'}")
    print(f"  Bonos personales: {plan['bonos']}  ·  envíos de códigos: {plan['envios']}")


def main() -> int:
    import os

    import config      # carga el .env (PANELES_DB) y deja la consola en UTF-8
    config.configurar_salida_utf8()
    p = argparse.ArgumentParser(description="Saca de los paneles las compras de prueba de un documento.")
    p.add_argument("--documento", required=True, help="DNI/CE/pasaporte de la persona del equipo")
    p.add_argument("--aplicar", action="store_true", help="aplica los cambios (sin esto, solo muestra)")
    p.add_argument("--db", help="ruta de la base (por defecto la de los paneles: PANELES_DB o paneles/boleteria.db)")
    args = p.parse_args()
    documento = args.documento.strip().upper()
    if not documento.isalnum() or not 4 <= len(documento) <= 15:
        print("Documento inválido.")
        return 2
    # Misma base que el tablero (como seed_demo.py): el .env puede traer PANELES_DB.
    db.RUTA_DB = Path(args.db) if args.db else Path(os.getenv("PANELES_DB", db.BASE_DIR / "boleteria.db"))
    if not Path(db.RUTA_DB).exists():
        print(f"No existe la base {db.RUTA_DB}.")
        return 2
    print(f"Base: {db.RUTA_DB}")
    visible = privacidad.documento(documento)
    plan = revisar(documento)
    _mostrar(plan, visible)
    if not (plan["propias"] or plan["cliente"] or plan["bonos"] or plan["envios"]):
        print("\nNada que cambiar.")
        return 0
    if not args.aplicar:
        print("\nNo se cambió nada. Para aplicarlo: agrega --aplicar")
        return 0
    copia = respaldar()
    print(f"\nRespaldo guardado en {copia}")
    if input(f"Escribe los 3 últimos dígitos del documento ({visible}) para confirmar: ").strip() != documento[-3:]:
        print("No coincide. No se cambió nada.")
        return 1
    r = aplicar(documento)
    print(f"Listo: {r['ventas_anuladas']} venta(s) anulada(s), ficha de cliente "
          f"{'borrada' if r['cliente_borrado'] else 'no existía'}, {r['bonos_borrados']} bono(s) borrado(s).")
    if r["mixtas"]:
        print(f"Quedan sin tocar las ventas con otros pasajeros: {r['mixtas']}")
    print("Recarga el panel para verlo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
