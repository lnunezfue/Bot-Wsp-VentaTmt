"""
quitar_cliente_prueba.py: sacar de los paneles las compras de prueba de una
persona del equipo (aparecía como "mejor cliente" con sus pruebas).

Las ventas de prueba se marcan 'anulada' (corresponden a comprobantes reales
emitidos y anulados en JELAF: no se borran, para que cuadren con JELAF) y sus
boletos quedan sin datos personales. La ficha del cliente y sus bonos se borran.
Una venta con otros pasajeros no se toca.
"""

from __future__ import annotations

import sqlite3

import quitar_cliente_prueba as quitar
from paneles import consultas, db, registro, tiempo

YO = "11111111"
OTRO = "22222222"


def _venta(documentos: list[str], precio: float = 70) -> int:
    return registro.registrar_venta(
        booking_id=None, origen="TACNA", destino="LIMA", monto_total=precio * len(documentos),
        telefono="51900000001", nro_boleto="B001-1",
        pasajeros=[{"documento": d, "nombres": "Ana", "paterno": "Prueba", "materno": "Demo",
                    "asiento": f"0{i + 1}", "precio": precio, "nro_boleto": f"B001-{i + 1}"}
                   for i, d in enumerate(documentos)],
    )


def _ranking() -> list[str]:
    return [f["documento"] for f in consultas.top_compradores(dias=0)]


def test_revisar_no_cambia_nada():
    _venta([YO])
    _venta([YO, OTRO])
    r = quitar.revisar(YO)
    assert len(r["propias"]) == 1 and len(r["mixtas"]) == 1 and r["cliente"] is True
    assert YO in _ranking()
    assert db.consultar_uno("SELECT COUNT(*) FROM venta WHERE estado = 'emitida'")[0] == 2


def test_aplicar_saca_al_cliente_del_ranking_y_de_las_ventas():
    for _ in range(3):
        _venta([YO])
    _venta([OTRO])
    resumen = quitar.aplicar(YO)
    assert resumen["ventas_anuladas"] == 3 and resumen["cliente_borrado"] is True
    assert _ranking() == [OTRO]
    assert db.consultar_uno("SELECT COUNT(*) FROM venta WHERE estado = 'anulada'")[0] == 3
    # Sin datos personales en lo que queda, y la ficha del cliente ya no existe.
    assert db.consultar_uno("SELECT COUNT(*) FROM boleto WHERE documento = ?", (YO,))[0] == 0
    assert db.consultar_uno("SELECT COUNT(*) FROM cliente WHERE documento = ?", (YO,))[0] == 0
    assert db.consultar_uno("SELECT COUNT(*) FROM boleto WHERE nombres = 'Ana'")[0] == 1   # el del otro
    # Los ingresos del panel ya no cuentan las pruebas.
    d, h = consultas.rango(None, None, 7)
    assert consultas.resumen_ventas(d, h)["boletos"] == 1


def test_una_venta_con_otros_pasajeros_no_se_toca():
    mixta = _venta([YO, OTRO])
    resumen = quitar.aplicar(YO)
    assert resumen["ventas_anuladas"] == 0 and resumen["mixtas"] == [mixta]
    assert db.consultar_uno("SELECT estado FROM venta WHERE id = ?", (mixta,))[0] == "emitida"


def test_borra_sus_bonos_pero_no_los_de_otros():
    _venta([YO])
    _venta([OTRO])
    db.escribir("INSERT INTO codigo_descuento (codigo, tipo, valor, creado_en, documento_exclusivo) "
                "VALUES ('YO-1', 'porcentaje', 10, ?, ?)", (tiempo.ahora(), YO))
    db.escribir("INSERT INTO codigo_descuento (codigo, tipo, valor, creado_en, documento_exclusivo) "
                "VALUES ('OTRO-1', 'porcentaje', 10, ?, ?)", (tiempo.ahora(), OTRO))
    quitar.aplicar(YO)
    codigos = [f[0] for f in db.consultar("SELECT codigo FROM codigo_descuento")]
    assert codigos == ["OTRO-1"]


def test_si_algo_falla_no_queda_a_medias(monkeypatch):
    _venta([YO])
    original = db.escribir_contando

    def falla_al_borrar_cliente(sql, params=()):
        if sql.lstrip().upper().startswith("DELETE FROM CLIENTE"):
            raise sqlite3.OperationalError("disco lleno")
        return original(sql, params)
    monkeypatch.setattr(db, "escribir_contando", falla_al_borrar_cliente)
    try:
        quitar.aplicar(YO)
    except sqlite3.OperationalError:
        pass
    assert db.consultar_uno("SELECT estado FROM venta")[0] == "emitida"
    assert YO in _ranking()


def test_respaldo_antes_de_cambiar(tmp_path):
    _venta([YO])
    copia = quitar.respaldar(tmp_path)
    assert copia.suffix == ".db"          # *.db: el .gitignore lo deja fuera del repo
    con = sqlite3.connect(copia)
    assert con.execute("SELECT COUNT(*) FROM venta").fetchone()[0] == 1
    con.close()
