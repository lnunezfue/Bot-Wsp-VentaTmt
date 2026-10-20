"""
Yupy con la API /v1/checkouts (QR Yape/Plin, modal YupyCheckout.open) — 05/10/2026.

El pago no se podía confirmar: el servidor consultaba GET /v1/payment-orders/{id},
que NO es parte de la API pública de Yupy (https://api.yupy.us/openapi.json y
/docs). La consulta oficial del estado es POST /v1/checkouts/browser/status con
el checkout_token que entrega POST /v1/checkouts; el resultado terminal es
pagado | cancelado | timeout. "Simular pago" da un `pagado` idéntico a uno real
(no existen estados simulados): con esa credencial, la venta real se bloquea.

La versión del compañero (CHATBOT PRUEBA) "funcionaba" porque /confirmar emitía
sin verificar nada con Yupy: no se replica (regla 8 del proyecto).
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import pytest

import pagos_yupy
from paneles import consultas, db
from pagos_yupy import ClienteYupy, ErrorYupy, evaluar_orden
from tests.test_venta import BOOKING, elegir_y_llenar, preparar_venta


def correr(coro):
    return asyncio.run(coro)


class Respuesta:
    def __init__(self, cuerpo, estado=200):
        self._cuerpo, self.status_code = cuerpo, estado

    def json(self):
        return self._cuerpo


@pytest.fixture
def yupy_real(monkeypatch):
    """ClienteYupy real (modo checkouts) con la red reemplazada: registra cada POST."""
    llamadas: list[tuple[str, dict, dict]] = []
    respuestas: dict[str, object] = {}

    def post(url, headers=None, json=None, timeout=None):
        llamadas.append((url, headers or {}, json or {}))
        for fin, r in respuestas.items():
            if url.endswith(fin):
                return r
        raise AssertionError(f"POST inesperado a {url}")

    monkeypatch.setattr(pagos_yupy.requests, "post", post)
    cliente = ClienteYupy("https://api.yupy.us", "3", "csec_prueba", api="checkouts")
    respuestas["/v1/auth/token"] = Respuesta({"access_token": "tok", "expires_in": 3600})
    return cliente, llamadas, respuestas


# --- Crear el checkout ----------------------------------------------------------------- #
def test_crear_checkout_devuelve_el_checkout_token(yupy_real):
    cliente, _, respuestas = yupy_real
    respuestas["/v1/checkouts"] = Respuesta({"payment_order_uid": "po_1", "checkout_token": "cks_prd_x",
                                             "checkout_url": "https://api.yupy.us/v1/checkouts/browser#token=x",
                                             "amount": "140.00", "currency": "PEN", "status": "pending"}, 201)
    orden = cliente.crear_checkout("TMT-1", Decimal("140.00"), "Ana", 900)
    assert orden["yupy_transaction_id"] == "po_1" and orden["checkout_token"] == "cks_prd_x"


def test_sin_checkout_token_no_se_entrega_el_checkout(yupy_real):
    cliente, _, respuestas = yupy_real
    respuestas["/v1/checkouts"] = Respuesta({"payment_order_uid": "po_1", "checkout_url": "https://x"}, 201)
    with pytest.raises(ErrorYupy, match="checkout_token"):
        cliente.crear_checkout("TMT-1", Decimal("140.00"), "Ana", 900)


def test_si_yupy_confirma_otro_monto_no_se_entrega_el_checkout(yupy_real):
    cliente, _, respuestas = yupy_real
    respuestas["/v1/checkouts"] = Respuesta({"payment_order_uid": "po_1", "checkout_token": "cks", "amount": "1.00",
                                             "currency": "PEN", "checkout_url": "https://x"}, 201)
    with pytest.raises(ErrorYupy, match="monto"):
        cliente.crear_checkout("TMT-1", Decimal("140.00"), "Ana", 900)


# --- Consultar el estado (la verificación del pago) ---------------------------------------- #
def test_el_estado_se_consulta_con_browser_status_y_el_token(yupy_real):
    cliente, llamadas, respuestas = yupy_real
    respuestas["/v1/checkouts/browser/status"] = Respuesta({"status": "pagado"})
    orden = cliente.consultar_orden("po_1", checkout_token="cks_prd_x", monto_creado=Decimal("140.00"))
    url, _, cuerpo = llamadas[-1]
    assert url == "https://api.yupy.us/v1/checkouts/browser/status" and cuerpo == {"checkout_token": "cks_prd_x"}
    # Sin monto en la respuesta vale el que Yupy confirmó al crear el checkout (el del servidor).
    assert evaluar_orden(orden, Decimal("140.00")) is None


@pytest.mark.parametrize("estado", ["pending", "presented", "cancelado", "timeout"])
def test_un_checkout_no_pagado_no_se_acepta(yupy_real, estado):
    cliente, _, respuestas = yupy_real
    respuestas["/v1/checkouts/browser/status"] = Respuesta({"status": estado})
    orden = cliente.consultar_orden("po_1", checkout_token="cks", monto_creado=Decimal("140.00"))
    assert "no está aprobado" in evaluar_orden(orden, Decimal("140.00"))


def test_si_yupy_informa_el_monto_tiene_que_coincidir(yupy_real):
    cliente, _, respuestas = yupy_real
    respuestas["/v1/checkouts/browser/status"] = Respuesta({"status": "pagado", "amount": "1.00", "currency": "PEN"})
    orden = cliente.consultar_orden("po_1", checkout_token="cks", monto_creado=Decimal("140.00"))
    assert "monto" in evaluar_orden(orden, Decimal("140.00"))


def test_sin_token_no_se_puede_verificar(yupy_real):
    cliente, _, _ = yupy_real
    with pytest.raises(ErrorYupy, match="checkout_token"):
        cliente.consultar_orden("po_1", checkout_token=None, monto_creado=Decimal("140.00"))


def test_yupy_caido_al_consultar_es_error_de_yupy(yupy_real):
    cliente, _, respuestas = yupy_real
    respuestas["/v1/checkouts/browser/status"] = Respuesta({"detail": "x"}, 502)
    with pytest.raises(ErrorYupy):
        cliente.consultar_orden("po_1", checkout_token="cks", monto_creado=Decimal("140.00"))


def test_simulacion_habilitada_se_lee_del_contexto(yupy_real):
    cliente, llamadas, respuestas = yupy_real
    respuestas["/v1/checkouts/browser/context"] = Respuesta({"payment_simulation_enabled": True})
    assert cliente.simulacion_habilitada("cks") is True
    assert llamadas[-1][2] == {"checkout_token": "cks"}
    respuestas["/v1/checkouts/browser/context"] = Respuesta({"payment_simulation_enabled": False})
    assert cliente.simulacion_habilitada("cks") is False


# --- Flujo completo (API) --------------------------------------------------------------------- #
def test_confirmar_verifica_con_el_token_guardado_del_checkout(entorno_venta, cliente):
    entorno_venta.yupy.api = "checkouts"

    async def escenario():
        tx = await preparar_venta(cliente)
        entorno_venta.yupy.aprobar(tx)
        return tx, await cliente.post("/api/v2/venta/confirmar", json={"booking_id": BOOKING, "referencia_pago": tx})
    tx, r = correr(escenario())
    assert r.status_code == 200, r.text
    assert (tx, entorno_venta.yupy.tokens[tx]) in entorno_venta.yupy.consultas
    guardados = json.loads(consultas.pago_de_booking(BOOKING)["checkout_tokens"])
    assert guardados == {tx: entorno_venta.yupy.tokens[tx]}


def test_venta_real_con_credencial_que_simula_pagos_se_bloquea(entorno_venta, cliente):
    entorno_venta.yupy.api = "checkouts"
    entorno_venta.yupy.simulacion = True

    async def escenario():
        await elegir_y_llenar(cliente)
        return await cliente.post("/api/v2/venta/checkout", json={"booking_id": BOOKING})
    r = correr(escenario())
    assert r.status_code == 503
    assert entorno_venta.yupy.cancelados == [entorno_venta.yupy.tokens["yupy-0001"]]
    assert db.consultar_uno("SELECT COUNT(*) FROM incidencia WHERE tipo = 'yupy_simulacion_habilitada' "
                            "AND severidad = 'critica'")[0] == 1


def test_en_simulacion_si_se_permite_probar_con_simular_pago(entorno_venta, cliente):
    entorno_venta.yupy.api = "checkouts"
    entorno_venta.yupy.simulacion = True
    entorno_venta.vendedor.simulacion = True
    assert correr(preparar_venta(cliente))   # entrega el checkout
