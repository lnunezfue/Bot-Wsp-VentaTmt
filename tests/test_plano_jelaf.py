"""
Cómo viene el plano en /itinerarios/turnos (captura real del 03/10/2026, bus
ZBA963, Tacna→Arequipa 10:50 PM, sin datos personales): la grilla entera, con
los asientos LIBRES en NumeAsiento=0 y su número en "Tipo". Antes el bot solo
miraba NumeAsiento: ignoraba todos los asientos vacíos, y con ellos su precio
("No pudimos obtener el precio" en viajes sin ventas).
"""

from __future__ import annotations

from decimal import Decimal

from jelaf_http import numero_de_celda, parsear_turnos


def _celda(tipo, nivel, precio, nume=0, flag="", venta=0):
    return {"NumeAsiento": nume, "Nivel": nivel, "PrecioNormal": precio, "PrecioVenta": venta,
            "PrecioMinimo": 20, "PrecioMaximo": 200, "FlagVenta": flag, "Tipo": tipo, "Color": "",
            "Nombres": "", "NumeroDocumento": ""}


TURNO_REAL = {"ListaPlanoBus": [
    _celda("34", 1, 80), _celda("VA", 1, 0), _celda("TV", 1, 0),
    _celda("33", 1, 70, nume=33, flag="V", venta=70), _celda("32", 1, 70, nume=32, flag="V", venta=70),
    _celda("35", 1, 70), _celda("LI", 0, 0), _celda("03", 2, 70), _celda("EE", 2, 0),
    _celda("25", 2, 70), _celda("26", 2, 60), _celda("28", 2, 70), _celda("30", 2, 60), _celda("CU", 2, 0),
]}


def test_el_numero_del_asiento_libre_viene_en_tipo():
    assert numero_de_celda(_celda("03", 2, 70)) == "03"
    assert numero_de_celda(_celda("33", 1, 70, nume=33, flag="V")) == "33"
    for no_asiento in ("VA", "TV", "LI", "EE", "CU", ""):
        assert numero_de_celda(_celda(no_asiento, 1, 0)) is None


def test_cada_asiento_libre_tiene_su_precio_y_solo_los_vendidos_estan_ocupados():
    estado = parsear_turnos(TURNO_REAL)
    assert estado.ocupados == {"32", "33"}
    assert estado.precio_de("34") == Decimal("80.00")
    assert estado.precio_de("35") == Decimal("70.00")
    # Lo que se vio en vivo: mismo piso, precios distintos por asiento.
    assert (estado.precio_de("25"), estado.precio_de("28")) == (Decimal("70.00"), Decimal("70.00"))
    assert (estado.precio_de("26"), estado.precio_de("30")) == (Decimal("60.00"), Decimal("60.00"))
    assert set(estado.precios) == {"34", "33", "32", "35", "03", "25", "26", "28", "30"}


def test_el_plano_lleva_el_precio_de_cada_asiento_para_mostrarlo():
    """Pedido de boletería (2026-10-09): ver el precio en cada asiento. En un mismo
    piso hay asientos de 60 y de 70; el del piso solo sirve de respaldo."""
    import planos

    estado = parsear_turnos(TURNO_REAL)
    pisos = {"2": {"rows": [[{"type": "seat", "id": "25"}, {"type": "seat", "id": "26"},
                             {"type": "seat", "id": "99"}, {"type": "tv"}]]}}
    fila = planos.aplicar_estado(pisos, estado)["2"]["rows"][0]
    assert [c.get("price") for c in fila] == [70.0, 60.0, 70.0, None]   # 99 sin precio propio: el del piso
