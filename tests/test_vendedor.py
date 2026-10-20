"""Reglas del vendedor HTTP que no pasan por la API: liberación, reaper, capacidad."""

from __future__ import annotations

import asyncio
import time

import vendedor_http

from tests.conftest import JelafHttpFalso
from vendedor_http import VendedorHttp


def _venta_abierta(capacidad=10):
    vendedor = VendedorHttp(JelafHttpFalso(), capacidad=capacidad, simulacion=False)
    return vendedor, vendedor.reservar_para("booking-1234")


def test_venta_activa_no_se_libera():
    _, v = _venta_abierta()
    assert v.motivo_para_liberar(1200) is None


def test_silencio_largo_libera():
    _, v = _venta_abierta()
    # El límite subió de 300 a 600 s (N60-SN): mirar otra app no cuesta los asientos.
    limite = vendedor_http.TIMEOUT_INACTIVIDAD_SELECCION_SEGUNDOS
    assert v.motivo_para_liberar(10_000, ahora=time.monotonic() + limite - 5) is None
    assert "inactividad" in v.motivo_para_liberar(10_000, ahora=time.monotonic() + limite + 1)


def test_duracion_maxima_libera_aunque_haya_heartbeat():
    _, v = _venta_abierta()
    ahora = time.monotonic() + 1300
    v.last_activity = ahora
    assert "duración máxima" in v.motivo_para_liberar(1200, ahora=ahora)


def test_con_checkout_vivo_nunca_se_libera():
    _, v = _venta_abierta()
    ahora = time.monotonic() + 5000
    v.pago_pendiente_hasta = ahora + 60
    assert v.motivo_para_liberar(1200, ahora=ahora) is None


def test_reaper_no_toca_una_venta_que_esta_trabajando_y_suelta_sus_bloqueos():
    vendedor, v = _venta_abierta()
    v.last_activity -= 10_000
    v.asientos_en_venta["05"] = {"ids": 1234, "programacion": 1}
    vendedor.jelaf.bloqueos[1234] = "05"

    async def escenario():
        async with v.lock:
            durante = await vendedor.liberar_inactivos(1200)
        return durante, await vendedor.liberar_inactivos(1200)
    durante, despues = asyncio.run(escenario())
    assert durante == []
    assert despues and despues[0][0] == "booking-1234"
    assert vendedor.workers == [] and vendedor.jelaf.bloqueos == {}


def test_capacidad_y_reserva_idempotente():
    vendedor = VendedorHttp(JelafHttpFalso(), capacidad=2, simulacion=False)
    a = vendedor.reservar_para("a-000000")
    assert vendedor.reservar_para("a-000000") is a
    assert vendedor.reservar_para("b-000000") is not None
    assert vendedor.reservar_para("c-000000") is None


def test_estado_enmascara_y_muestra_lugares_libres():
    vendedor, _ = _venta_abierta(capacidad=3)
    estado = vendedor.estado()
    assert [e["estado"] for e in estado] == ["en_uso", "libre", "libre"]
    assert estado[0]["owner_token"] == "booking-…"


def test_en_simulacion_no_se_sueltan_bloqueos_reales():
    vendedor = VendedorHttp(JelafHttpFalso(), simulacion=True)
    asyncio.run(vendedor.soltar_bloqueos([5, 6]))
    assert vendedor.jelaf.llamadas["liberar"] == 0
