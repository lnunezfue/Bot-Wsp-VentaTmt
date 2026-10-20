"""
Varias compras a la vez, de punta a punta, contra el JELAF HTTP falso.
Es lo que pedía el negocio: al menos 3 ventas simultáneas con una sola cuenta.
"""

from __future__ import annotations

import asyncio

import pytest

from paneles import consultas
from tests.test_venta import comprar, iniciar

BOOKINGS = [f"{i:08d}-0000-0000-0000-00000000000{i}" for i in range(1, 7)]


@pytest.mark.parametrize("cantidad", [3, 5])
def test_varias_compras_simultaneas_se_emiten_todas(entorno_venta, cliente, cantidad):
    entorno_venta.jelaf.demora_emision = 0.1

    async def escenario():
        return await asyncio.gather(*(
            comprar(cliente, entorno_venta, asientos=(f"{10 + 2 * i:02d}", f"{11 + 2 * i:02d}"), booking=b)
            for i, b in enumerate(BOOKINGS[:cantidad])
        ))
    respuestas = asyncio.run(escenario())
    assert [r.status_code for r in respuestas] == [200] * cantidad, [r.text for r in respuestas]

    numeros = [b["nro_boleto"] for r in respuestas for b in r.json()["data"]["boletos"]]
    assert len(numeros) == 2 * cantidad and len(set(numeros)) == 2 * cantidad      # sin repetidos
    assert len(entorno_venta.jelaf.vendidos) == 2 * cantidad
    assert entorno_venta.jelaf.llamadas["grabar_venta"] == cantidad
    assert entorno_venta.jelaf.bloqueos == {}
    for i, b in enumerate(BOOKINGS[:cantidad]):
        venta = consultas.venta_de_booking(b)
        assert venta["estado"] == "emitida"
        assert [x["asiento"] for x in venta["boletos"]] == [f"{10 + 2 * i:02d}", f"{11 + 2 * i:02d}"]
    assert entorno_venta.vendedor.workers == []                                     # todo liberado


def test_las_compras_avanzan_en_paralelo_no_en_fila(entorno_venta, cliente):
    """Con 3 clientes eligiendo asientos a la vez, ninguno espera a que otro termine."""
    async def escenario():
        return await asyncio.gather(*(iniciar(cliente, b) for b in BOOKINGS[:3]))
    assert [r.status_code for r in asyncio.run(escenario())] == [200, 200, 200]
    assert len(entorno_venta.vendedor.workers) == 3


def test_dos_clientes_por_el_mismo_asiento_solo_uno_lo_consigue(entorno_venta, cliente):
    async def escenario():
        await iniciar(cliente, BOOKINGS[0])
        await iniciar(cliente, BOOKINGS[1])
        return await asyncio.gather(*(
            cliente.post("/api/v2/venta/seleccionar-asiento", json={"booking_id": b, "numero_asiento": "20"})
            for b in BOOKINGS[:2]))
    codigos = sorted(r.status_code for r in asyncio.run(escenario()))
    assert codigos == [200, 409]
    assert list(entorno_venta.jelaf.bloqueos.values()) == ["20"]


def test_superada_la_capacidad_la_siguiente_compra_recibe_423(entorno_venta, cliente, monkeypatch):
    entorno_venta.vendedor.capacidad = 3
    monkeypatch.setattr(entorno_venta.api, "ESPERA_MOSTRADOR_SEGUNDOS", 0.3)

    async def escenario():
        return [await iniciar(cliente, b) for b in BOOKINGS[:4]]
    assert [r.status_code for r in asyncio.run(escenario())] == [200, 200, 200, 423]


def test_emisiones_simultaneas_van_de_a_una_a_la_caja(entorno_venta, cliente):
    """grabar-venta nunca se solapa: todas las ventas van a la misma caja del cajero."""
    en_curso = {"ahora": 0, "maximo": 0}
    original = entorno_venta.jelaf.grabar_venta

    def contar(venta):
        en_curso["ahora"] += 1
        en_curso["maximo"] = max(en_curso["maximo"], en_curso["ahora"])
        try:
            return original(venta)
        finally:
            en_curso["ahora"] -= 1
    entorno_venta.jelaf.grabar_venta = contar
    entorno_venta.jelaf.demora_emision = 0.1

    async def escenario():
        return await asyncio.gather(*(comprar(cliente, entorno_venta, asientos=(f"{30 + i:02d}",), booking=b)
                                      for i, b in enumerate(BOOKINGS[:4])))
    assert all(r.status_code == 200 for r in asyncio.run(escenario()))
    assert en_curso["maximo"] == 1
