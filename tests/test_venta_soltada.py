"""
Venta soltada por la limpieza automática con un cobro abierto (N56-SN).

Caso real #147 (17/10/2026): el cliente pagó S/ 100 con Yape dentro del
formulario de Niubiz, el aviso nunca llegó y la limpieza soltó la venta como
"Compra abandonada · leve". Un cobro abierto al soltar es dinero que pudo
entrar sin boleto: crítica. Con Yupy se le pregunta antes de decidir (opción A).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

from pagos_yupy import ErrorYupy
from paneles import atencion, consultas, db
from tests.test_venta import BOOKING, elegir_y_llenar, preparar_venta


def correr(coro):
    return asyncio.run(coro)


def incidencias(booking=BOOKING):
    return db.filas_a_dict(db.consultar(
        "SELECT tipo, severidad, mensaje, contexto FROM incidencia WHERE booking_id = ? ORDER BY id", (booking,)))


def vencer(entorno, booking=BOOKING):
    """La venta quedó sin actividad y su cobro venció: la próxima pasada de la limpieza la suelta."""
    venta = entorno.venta(booking)
    venta.last_activity -= 10_000
    venta.reservado_en -= 10_000
    venta.pago_pendiente_hasta = 0.0


async def soltar(entorno, booking=BOOKING):
    vencer(entorno, booking)
    await entorno.api._soltar_inactivas()


def test_venta_soltada_con_cobro_niubiz_abierto_es_critica(entorno_venta, cliente):
    async def escenario():
        await elegir_y_llenar(cliente)
        r = await cliente.post("/api/v2/venta/niubiz/sesion", json={"booking_id": BOOKING})
        assert r.status_code == 200, r.text
        await soltar(entorno_venta)
        return r.json()
    sesion = correr(escenario())
    [inc] = incidencias()
    assert inc["tipo"] == "pago_sin_confirmar" and inc["severidad"] == "critica"
    assert "portal de Niubiz" in inc["mensaje"]
    contexto = json.loads(inc["contexto"])
    assert contexto["proveedor"] == "niubiz" and contexto["pedido"] == sesion["pedido"]
    assert contexto["monto"] == sesion["monto_total"] and contexto["asientos"] == ["05"]
    assert entorno_venta.venta(BOOKING) is None


def test_venta_soltada_sin_cobro_sigue_siendo_leve(entorno_venta, cliente):
    async def escenario():
        await elegir_y_llenar(cliente)
        await soltar(entorno_venta)
    correr(escenario())
    [inc] = incidencias()
    assert inc["tipo"] == "sesion_liberada" and inc["severidad"] == "leve"


def test_venta_soltada_con_yupy_pagado_queda_cobrada_sin_emitir(entorno_venta, cliente):
    async def escenario():
        tx = await preparar_venta(cliente)
        entorno_venta.yupy.aprobar(tx)          # pagó, pero el navegador nunca volvió a /confirmar
        await soltar(entorno_venta)
        return tx
    tx = correr(escenario())
    [inc] = incidencias()
    assert inc["tipo"] == "cobrado_sin_emitir" and inc["severidad"] == "critica"
    venta = consultas.venta_de_booking(BOOKING)
    assert venta["estado"] == "cobrada_sin_emitir" and venta["referencia_pago"] == tx
    assert consultas.pago_de_booking(BOOKING)["estado"] == "usado"
    assert entorno_venta.jelaf.llamadas["grabar_venta"] == 0


def test_venta_soltada_con_yupy_sin_pagar_es_leve_y_lo_dice(entorno_venta, cliente):
    async def escenario():
        await preparar_venta(cliente)           # abrió el QR y se fue sin pagar
        await soltar(entorno_venta)
    correr(escenario())
    [inc] = incidencias()
    assert inc["tipo"] == "sesion_liberada" and inc["severidad"] == "leve"
    assert "Yupy confirma que no se pagó" in inc["mensaje"]


def test_venta_soltada_con_yupy_sin_respuesta_es_critica(entorno_venta, cliente, monkeypatch):
    def sin_respuesta(*a, **k):
        raise ErrorYupy("Yupy no respondió (Timeout).")

    async def escenario():
        await preparar_venta(cliente)
        monkeypatch.setattr(entorno_venta.yupy, "consultar_orden", sin_respuesta)
        await soltar(entorno_venta)
    correr(escenario())
    [inc] = incidencias()
    assert inc["tipo"] == "pago_sin_confirmar" and inc["severidad"] == "critica"
    assert "portal de Yupy" in inc["mensaje"]


def test_si_la_base_falla_la_limpieza_sigue_con_las_demas(entorno_venta, cliente, monkeypatch):
    otra = "99999999-2222-3333-4444-555555555555"

    def base_caida(booking_id):
        if booking_id == BOOKING:
            raise sqlite3.OperationalError("database is locked")
        return None

    async def escenario():
        await elegir_y_llenar(cliente)
        await elegir_y_llenar(cliente, asientos=("06",), booking=otra)
        monkeypatch.setattr(consultas, "pago_de_booking", base_caida)
        vencer(entorno_venta, otra)
        await soltar(entorno_venta)            # no lanza
    correr(escenario())
    assert entorno_venta.venta(BOOKING) is None and entorno_venta.venta(otra) is None
    # Sin poder revisar el cobro no se sabe si hubo pago: se avisa como crítica.
    assert [i["tipo"] for i in incidencias()] == ["pago_sin_confirmar"]
    assert [i["tipo"] for i in incidencias(otra)] == ["sesion_liberada"]


def test_pago_sin_confirmar_ofrece_emitir_a_mano_y_devolucion():
    acciones = atencion.acciones_para("pago_sin_confirmar", "critica")
    assert {"emitio_manual", "devolucion", "compro_otro", "sin_accion"} <= set(acciones)
