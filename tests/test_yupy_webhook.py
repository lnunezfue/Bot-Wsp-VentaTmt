"""
Webhook de Yupy (05/10/2026): Yupy lo pidió para cerrar la integración. Se
configura en https://api.yupy.us/admin (sección Webhooks): URL HTTPS de callback
+ Signing Secret (whsec_...). Contrato público (https://api.yupy.us/docs, 14.7):

  - Cabeceras X-YUPY-Timestamp y X-YUPY-Signature; la firma es HMAC-SHA256 con
    el Signing Secret sobre timestamp + "." + cuerpo crudo.
  - Envelope: event_id, event_type, event_version, created_at, data{...}.
  - Eventos: payment_order.created / .presented, payment.reported,
    payment.reconciled, payment_order.terminal (data.result = pagado | cancelado
    | timeout) y webhook.test ("Enviar prueba" desde Admin).

El aviso por sí solo NO emite boletos: no reemplaza la verificación servidor a
servidor de /confirmar (browser/status). Se valida, se deduplica, se registra y
adelanta al conciliador, que emite solo si Yupy confirma (tests/test_conciliador.py).
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import hmac
import json
import time

import pytest

from paneles import db
from tests.test_venta import BOOKING, preparar_venta

SECRETO = "whsec_prueba_123"
RUTA = "/api/v2/yupy/webhook"


def correr(coro):
    return asyncio.run(coro)


def firmar(cuerpo: bytes, ts: str, secreto: str = SECRETO) -> str:
    return hmac.new(secreto.encode(), ts.encode() + b"." + cuerpo, hashlib.sha256).hexdigest()


def evento(event_id="evt_1", tipo="webhook.test", **data) -> bytes:
    return json.dumps({"event_id": event_id, "event_type": tipo, "event_version": 1,
                       "created_at": "2026-10-05T22:00:00Z", "data": data}).encode()


@pytest.fixture
def con_secreto(entorno_venta, monkeypatch):
    api = entorno_venta.api
    monkeypatch.setattr(api, "CONFIG", dataclasses.replace(api.CONFIG, yupy_webhook_secret=SECRETO))
    return entorno_venta


async def enviar(c, cuerpo: bytes, ts: str | None = None, firma: str | None = None):
    ts = ts if ts is not None else str(int(time.time()))
    firma = firma if firma is not None else firmar(cuerpo, ts)
    return await c.post(RUTA, content=cuerpo, headers={"Content-Type": "application/json",
                                                       "X-YUPY-Timestamp": ts, "X-YUPY-Signature": firma})


def recibidos(tipo=None):
    if tipo:
        return db.consultar_uno("SELECT COUNT(*) FROM yupy_webhook WHERE event_type = ?", (tipo,))[0]
    return db.consultar_uno("SELECT COUNT(*) FROM yupy_webhook")[0]


def test_enviar_prueba_desde_admin_responde_200(con_secreto, cliente):
    r = correr(enviar(cliente, evento()))
    assert r.status_code == 200 and recibidos("webhook.test") == 1


def test_firma_con_prefijo_sha256_tambien_se_acepta(con_secreto, cliente):
    cuerpo, ts = evento(), str(int(time.time()))
    r = correr(enviar(cliente, cuerpo, ts, "sha256=" + firmar(cuerpo, ts)))
    assert r.status_code == 200


def test_firma_invalida_se_rechaza_y_no_se_registra(con_secreto, cliente):
    r = correr(enviar(cliente, evento(), firma="0" * 64))
    assert r.status_code == 401 and recibidos() == 0


def test_cuerpo_alterado_despues_de_firmar_se_rechaza(con_secreto, cliente):
    ts = str(int(time.time()))
    firma = firmar(evento(tipo="payment_order.terminal", result="cancelado"), ts)
    r = correr(enviar(cliente, evento(tipo="payment_order.terminal", result="pagado"), ts, firma))
    assert r.status_code == 401


def test_sin_signing_secret_configurado_falla_cerrado(entorno_venta, cliente):
    r = correr(enviar(cliente, evento()))
    assert r.status_code == 503 and recibidos() == 0


def test_aviso_viejo_reenviado_se_rechaza(con_secreto, cliente):
    viejo = str(int(time.time()) - 3600)
    r = correr(enviar(cliente, evento(), ts=viejo))
    assert r.status_code == 401


def test_el_mismo_aviso_dos_veces_se_registra_una(con_secreto, cliente):
    async def escenario():
        a = await enviar(cliente, evento("evt_9"))
        b = await enviar(cliente, evento("evt_9"))
        return a, b
    a, b = correr(escenario())
    assert a.status_code == b.status_code == 200 and recibidos() == 1


def test_cuerpo_enorme_se_rechaza(con_secreto, cliente):
    r = correr(enviar(cliente, b"{" + b" " * 200_000 + b"}"))
    assert r.status_code == 413


def test_aviso_de_pagado_queda_asociado_a_la_venta_pero_no_emite(con_secreto, cliente):
    async def escenario():
        tx = await preparar_venta(cliente)
        cuerpo = evento("evt_pag", "payment_order.terminal", payment_order_uid=tx, result="pagado",
                        amount="70.00", currency="PEN", external_transaction_id="TMT-x")
        return await enviar(cliente, cuerpo)
    r = correr(escenario())
    assert r.status_code == 200
    fila = db.consultar_uno("SELECT booking_id, result FROM yupy_webhook WHERE event_id = 'evt_pag'")
    assert tuple(fila) == (BOOKING, "pagado")
    assert con_secreto.jelaf.llamadas["grabar_venta"] == 0          # emitir es SOLO de /confirmar
