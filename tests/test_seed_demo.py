"""Datos de ejemplo: se agregan sin pisar lo real y se quitan sin dejar rastro."""

from __future__ import annotations

from paneles import db, fidelizacion, registro, seed_demo


def _contar(sql: str, params: tuple = ()) -> int:
    return db.consultar_uno(sql, params)[0]


def test_los_datos_de_ejemplo_no_tocan_lo_real_y_se_quitan_completos():
    # Datos "reales" previos: una venta, su cliente, una incidencia y un código propio.
    registro.registrar_venta(
        booking_id="real-0001-abcd", origen="TACNA", destino="LIMA", monto_total=120,
        nro_boleto="B001-1", telefono="51987654321",
        pasajeros=[{"documento": "72677853", "nombres": "Ana", "paterno": "Real", "asiento": "05", "precio": 120}])
    registro.registrar_incidencia("pool_ocupado", "real", booking_id="real-0001-abcd")
    fidelizacion.crear_codigo("VUELVE10", 10, estado="activo")
    antes = dict(db.consultar_uno("SELECT nombres, total_viajes, total_gastado FROM cliente WHERE documento = '72677853'"))

    r = seed_demo.sembrar(dias=4)
    assert r["ventas"] > 0 and r["bonos"] > 0
    assert _contar("SELECT COUNT(*) FROM atencion") > 0
    assert _contar("SELECT COUNT(*) FROM incidencia WHERE estado = 'en_seguimiento'") >= 0
    # Las reglas de ejemplo quedan apagadas: en una base real no dan bonos a clientes reales.
    assert _contar("SELECT COUNT(*) FROM regla_bono WHERE activa = 1") == 0
    # Lo real sigue igual y no recibió nada de ejemplo.
    assert dict(db.consultar_uno("SELECT nombres, total_viajes, total_gastado FROM cliente WHERE documento = '72677853'")) == antes
    assert _contar("SELECT COUNT(*) FROM codigo_enviado WHERE documento = '72677853'") == 0
    assert db.consultar_uno("SELECT descripcion FROM codigo_descuento WHERE codigo = 'VUELVE10'")[0] is None
    # Nada de ejemplo con forma de dato real.
    assert _contar("SELECT COUNT(*) FROM cliente WHERE documento NOT LIKE 'DEMO%' AND documento <> '72677853'") == 0
    assert _contar("SELECT COUNT(*) FROM contacto WHERE telefono NOT LIKE '51000%'") == 0

    seed_demo.quitar_demo()
    assert _contar("SELECT COUNT(*) FROM venta") == 1
    assert _contar("SELECT COUNT(*) FROM cliente") == 1
    assert _contar("SELECT COUNT(*) FROM incidencia") == 1
    assert [f[0] for f in db.consultar("SELECT codigo FROM codigo_descuento")] == ["VUELVE10"]
    for tabla in ("atencion", "regla_bono", "lote_bono", "codigo_enviado", "contacto"):
        assert _contar(f"SELECT COUNT(*) FROM {tabla}") == 0, tabla
    assert _contar("SELECT COUNT(*) FROM sesion_compra WHERE booking_id LIKE 'demo-%'") == 0
    assert dict(db.consultar_uno("SELECT nombres, total_viajes, total_gastado FROM cliente WHERE documento = '72677853'")) == antes
