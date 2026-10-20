"""
Conciliador de pagos (A1 de la auditoría del 18/10/2026, N59-SN).

Antes el boleto solo salía si el NAVEGADOR volvía a llamar a /confirmar: con
Yape el cliente sale de WhatsApp para pagar y, al volver, la página pudo
recargarse o rendirse (6 intentos de 3 s). El dinero entraba y nadie emitía.
Ahora el servidor pregunta a la pasarela por las ventas activas con un cobro
abierto y, si se pagó exactamente, emite él mismo, una sola vez. Un aviso del
webhook de Yupy solo ADELANTA esa consulta: nunca se emite por el aviso.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time

from paneles import consultas, db
from tests.test_venta import BOOKING, preparar_venta
from tests.test_yupy_webhook import SECRETO, enviar, evento


def correr(coro):
    return asyncio.run(coro)


def contar(sql, params=()):
    return db.consultar_uno(sql, params)[0]


def test_pago_que_el_navegador_no_confirmo_se_emite_solo(entorno_venta, cliente):
    async def escenario():
        tx = await preparar_venta(cliente)
        entorno_venta.yupy.aprobar(tx)           # pagó; el navegador nunca volvió a /confirmar
        await entorno_venta.api._conciliar_pagos_abiertos()
        return tx
    tx = correr(escenario())
    venta = consultas.venta_de_booking(BOOKING)
    assert venta["estado"] == "emitida" and venta["referencia_pago"] == tx
    assert entorno_venta.jelaf.llamadas["grabar_venta"] == 1
    assert entorno_venta.venta(BOOKING) is None
    assert contar("SELECT COUNT(*) FROM evento WHERE paso = 'pago_conciliado' AND booking_id = ?", (BOOKING,)) == 1


def test_sin_pago_no_emite_ni_suelta_la_venta(entorno_venta, cliente):
    async def escenario():
        await preparar_venta(cliente)
        await entorno_venta.api._conciliar_pagos_abiertos()
    correr(escenario())
    assert consultas.venta_de_booking(BOOKING) is None
    assert entorno_venta.jelaf.llamadas["grabar_venta"] == 0
    assert entorno_venta.venta(BOOKING) is not None
    # Preguntar no es un fallo: no deja incidencias.
    assert contar("SELECT COUNT(*) FROM incidencia") == 0


def test_antes_de_soltar_la_venta_se_pregunta_y_se_emite(entorno_venta, cliente):
    async def escenario():
        tx = await preparar_venta(cliente)
        entorno_venta.yupy.aprobar(tx)
        venta = entorno_venta.venta(BOOKING)
        venta.last_activity -= 10_000            # abandonada y con el cobro vencido
        venta.pago_pendiente_hasta = 0.0
        # Recién consultada: aun así, una venta por soltar se vuelve a preguntar.
        entorno_venta.api._ultima_conciliacion[BOOKING] = time.monotonic()
        await entorno_venta.api._pasada_de_limpieza()
    correr(escenario())
    assert consultas.venta_de_booking(BOOKING)["estado"] == "emitida"
    assert contar("SELECT COUNT(*) FROM incidencia WHERE booking_id = ?", (BOOKING,)) == 0


def test_navegador_y_conciliador_a_la_vez_emiten_una_sola_vez(entorno_venta, cliente):
    async def escenario():
        tx = await preparar_venta(cliente)
        entorno_venta.yupy.aprobar(tx)
        r, _ = await asyncio.gather(
            cliente.post("/api/v2/venta/confirmar", json={"booking_id": BOOKING, "referencia_pago": tx}),
            entorno_venta.api._conciliar_pagos_abiertos())
        return r
    r = correr(escenario())
    assert r.status_code == 200
    assert entorno_venta.jelaf.llamadas["grabar_venta"] == 1


def test_no_pregunta_a_yupy_en_cada_pasada(entorno_venta, cliente):
    async def escenario():
        await preparar_venta(cliente)
        antes = len(entorno_venta.yupy.consultas)
        await entorno_venta.api._conciliar_pagos_abiertos()
        await entorno_venta.api._conciliar_pagos_abiertos()
        return len(entorno_venta.yupy.consultas) - antes
    assert correr(escenario()) == 1


def test_el_aviso_del_webhook_adelanta_la_emision(entorno_venta, cliente, monkeypatch):
    api = entorno_venta.api
    monkeypatch.setattr(api, "CONFIG", dataclasses.replace(api.CONFIG, yupy_webhook_secret=SECRETO))

    async def escenario():
        tx = await preparar_venta(cliente)
        entorno_venta.yupy.aprobar(tx)
        api._ultima_conciliacion[BOOKING] = time.monotonic()   # el aviso no espera el intervalo
        r = await enviar(cliente, evento("evt_pago", "payment_order.terminal", payment_order_uid=tx, result="pagado"))
        await asyncio.gather(*list(api._tareas_fondo))
        return r
    assert correr(escenario()).status_code == 200
    assert consultas.venta_de_booking(BOOKING)["estado"] == "emitida"


def test_un_aviso_falso_de_pagado_no_emite(entorno_venta, cliente, monkeypatch):
    """El aviso dice "pagado", pero Yupy (consultado servidor a servidor) dice que no: no se emite."""
    api = entorno_venta.api
    monkeypatch.setattr(api, "CONFIG", dataclasses.replace(api.CONFIG, yupy_webhook_secret=SECRETO))

    async def escenario():
        tx = await preparar_venta(cliente)
        await enviar(cliente, evento("evt_falso", "payment_order.terminal", payment_order_uid=tx, result="pagado"))
        await asyncio.gather(*list(api._tareas_fondo))
    correr(escenario())
    assert consultas.venta_de_booking(BOOKING) is None
    assert entorno_venta.jelaf.llamadas["grabar_venta"] == 0


def test_el_medio_de_pago_queda_registrado(entorno_venta, cliente):
    async def escenario():
        tx = await preparar_venta(cliente)
        entorno_venta.yupy.aprobar(tx)
        await entorno_venta.api._conciliar_pagos_abiertos()
    correr(escenario())
    assert consultas.venta_de_booking(BOOKING)["medio_pago"] == "YUPY"


def test_venta_soltada_con_yupy_cobrado_por_otro_monto_es_critica(entorno_venta, cliente):
    async def escenario():
        tx = await preparar_venta(cliente)
        entorno_venta.yupy.aprobar(tx, monto="1.00")
        venta = entorno_venta.venta(BOOKING)
        venta.last_activity -= 10_000
        venta.pago_pendiente_hasta = 0.0
        await entorno_venta.api._pasada_de_limpieza()
    correr(escenario())
    [inc] = db.filas_a_dict(db.consultar("SELECT tipo, severidad, mensaje FROM incidencia WHERE booking_id = ?",
                                         (BOOKING,)))
    assert inc["severidad"] == "critica" and "monto cobrado" in inc["mensaje"]
    assert json.loads(consultas.pago_de_booking(BOOKING)["asientos"]) == ["05"]
