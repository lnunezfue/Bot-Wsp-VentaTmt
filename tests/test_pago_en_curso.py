"""
Pago en curso al volver de la billetera (N60-SN).

El cliente sale de WhatsApp para pagar con Yape/Plin; WhatsApp congela o
descarta la página y la confirmación de Niubiz no llega (caso #147). Al volver,
la mini-webapp pregunta en qué quedó la compra (/estado-pago, sin efectos) y
ofrece "Ya pagué" (/ya-pague): una alerta crítica con el número de operación
y una retención de los asientos para verificar en el portal. "Ya pagué" NUNCA
emite: el boleto solo sale si la pasarela confirma el pago o si el equipo lo
ve en el portal y emite a mano.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

import vendedor_http
import whatsapp
from paneles import atencion, db
from tests.test_venta import BOOKING, comprar, elegir_y_llenar, iniciar


def correr(coro):
    return asyncio.run(coro)


def incidencias(tipo=None, booking=BOOKING):
    sql = "SELECT tipo, severidad, mensaje, contexto FROM incidencia WHERE booking_id = ?"
    params = [booking]
    if tipo:
        sql += " AND tipo = ?"
        params.append(tipo)
    return db.filas_a_dict(db.consultar(sql + " ORDER BY id", tuple(params)))


async def abrir_niubiz(c, booking=BOOKING, contacto=None, asiento="05"):
    if contacto:
        assert (await iniciar(c, booking, contacto=contacto)).status_code == 200
    await elegir_y_llenar(c, asientos=(asiento,), booking=booking)
    r = await c.post("/api/v2/venta/niubiz/sesion", json={"booking_id": booking})
    assert r.status_code == 200, r.text
    return r.json()


async def estado(c, booking=BOOKING, **extra):
    r = await c.post("/api/v2/venta/estado-pago", json={"booking_id": booking, **extra})
    assert r.status_code == 200, r.text
    return r.json()


async def ya_pague(c, booking=BOOKING, operacion="00350376"):
    return await c.post("/api/v2/venta/ya-pague", json={"booking_id": booking, "numero_operacion": operacion})


# --------------------------------------------------------------------------- #
# /estado-pago: en qué quedó la compra (solo lectura)
# --------------------------------------------------------------------------- #
def test_estado_sin_cobro(entorno_venta, cliente):
    async def escenario():
        await elegir_y_llenar(cliente)
        return await estado(cliente)
    assert correr(escenario())["estado"] == "sin_pago"


def test_estado_con_cobro_niubiz_abierto(entorno_venta, cliente):
    async def escenario():
        sesion = await abrir_niubiz(cliente)
        return sesion, await estado(cliente)
    sesion, e = correr(escenario())
    assert e["estado"] == "esperando_pago" and e["venta_activa"] is True
    assert e["pedido"] == sesion["pedido"] and e["proveedor"] == "niubiz" and e["monto_total"] == sesion["monto_total"]


def test_estado_de_una_compra_emitida_trae_los_boletos(entorno_venta, cliente):
    async def escenario():
        await comprar(cliente, entorno_venta)
        return await estado(cliente)
    e = correr(escenario())
    assert e["estado"] == "emitida" and e["boletos"][0]["nro_boleto"]


def test_consultar_el_estado_no_cambia_nada(entorno_venta, cliente):
    async def escenario():
        await abrir_niubiz(cliente)
        for _ in range(3):
            await estado(cliente)
    correr(escenario())
    assert incidencias() == []
    assert entorno_venta.jelaf.llamadas["grabar_venta"] == 0
    assert db.consultar_uno("SELECT estado FROM pago WHERE booking_id = ?", (BOOKING,))[0] == "pendiente"


def test_pagina_descartada_queda_en_la_bitacora(entorno_venta, cliente):
    async def escenario():
        await abrir_niubiz(cliente)
        await estado(cliente, contexto="descartada")
    correr(escenario())
    assert db.consultar_uno("SELECT COUNT(*) FROM evento WHERE paso = 'pagina_descartada' AND booking_id = ?",
                            (BOOKING,))[0] == 1


# --------------------------------------------------------------------------- #
# /ya-pague: alerta + retención, nunca emisión
# --------------------------------------------------------------------------- #
def test_ya_pague_alerta_retiene_y_no_emite(entorno_venta, cliente):
    async def escenario():
        sesion = await abrir_niubiz(cliente)
        r = await ya_pague(cliente)
        return sesion, r
    sesion, r = correr(escenario())
    assert r.status_code == 200 and r.json()["retenido"] is True
    [inc] = incidencias("pago_reclamado")
    assert inc["severidad"] == "critica"
    assert "00350376" in inc["mensaje"] and sesion["pedido"] in inc["mensaje"]
    assert "No emitir sin ver el pago" in inc["mensaje"]
    assert json.loads(inc["contexto"])["numero_operacion"] == "00350376"
    venta = entorno_venta.venta(BOOKING)
    assert venta.pago_pendiente_hasta >= time.monotonic() + 29 * 60
    assert entorno_venta.jelaf.llamadas["grabar_venta"] == 0
    assert db.consultar_uno("SELECT COUNT(*) FROM venta WHERE booking_id = ?", (BOOKING,))[0] == 0


def test_ya_pague_vale_una_sola_vez_por_compra(entorno_venta, cliente):
    async def escenario():
        await abrir_niubiz(cliente)
        await ya_pague(cliente)
        primera = entorno_venta.venta(BOOKING).pago_pendiente_hasta
        r = await ya_pague(cliente, operacion="99999999")
        return primera, r, await estado(cliente)
    primera, r, e = correr(escenario())
    assert r.status_code == 200
    assert len(incidencias("pago_reclamado")) == 1
    assert entorno_venta.venta(BOOKING).pago_pendiente_hasta == primera     # no se retiene de nuevo
    assert e["estado"] == "reclamado"


def test_ya_pague_sin_cobro_abierto_se_rechaza(entorno_venta, cliente):
    async def escenario():
        await elegir_y_llenar(cliente)
        return await ya_pague(cliente)
    assert correr(escenario()).status_code == 409
    assert incidencias() == []


@pytest.mark.parametrize("operacion", ["", "12", "abc<script>", "1" * 40])
def test_ya_pague_exige_un_numero_de_operacion_valido(entorno_venta, cliente, operacion):
    async def escenario():
        await abrir_niubiz(cliente)
        return await ya_pague(cliente, operacion=operacion)
    assert correr(escenario()).status_code == 422
    assert incidencias("pago_reclamado") == []


def test_ya_pague_con_la_venta_ya_soltada_alerta_sin_retener(entorno_venta, cliente):
    async def escenario():
        await abrir_niubiz(cliente)
        venta = entorno_venta.venta(BOOKING)
        async with venta.lock:
            await venta.liberar_y_resetear()
        return await ya_pague(cliente)
    r = correr(escenario())
    assert r.status_code == 200 and r.json()["retenido"] is False
    [inc] = incidencias("pago_reclamado")
    assert inc["severidad"] == "critica" and "asientos ya se habían soltado" in inc["mensaje"]


def test_un_telefono_que_reclama_mucho_ya_no_retiene_asientos(entorno_venta, cliente):
    contacto = whatsapp.firmar_contacto("51900000001", entorno_venta.api.CONFIG.enlace_secreto)
    compras = [f"0000000{i}-2222-3333-4444-555555555555" for i in range(1, 4)]

    async def escenario():
        respuestas = []
        for i, b in enumerate(compras):
            await abrir_niubiz(cliente, booking=b, contacto=contacto, asiento=f"0{i + 5}")
            respuestas.append((await ya_pague(cliente, booking=b)).json())
        return respuestas
    respuestas = correr(escenario())
    assert [r["retenido"] for r in respuestas] == [True, True, False]
    [tercera] = incidencias("pago_reclamado", booking=compras[2])
    assert tercera["severidad"] == "critica" and "sin retener" in tercera["mensaje"]


def test_al_vencer_la_retencion_no_se_duplica_la_alerta(entorno_venta, cliente):
    async def escenario():
        await abrir_niubiz(cliente)
        await ya_pague(cliente)
        venta = entorno_venta.venta(BOOKING)
        venta.last_activity -= 10_000
        venta.pago_pendiente_hasta = 0.0
        await entorno_venta.api._pasada_de_limpieza()
    correr(escenario())
    assert entorno_venta.venta(BOOKING) is None
    assert [i["tipo"] for i in incidencias()] == ["pago_reclamado", "sesion_liberada"]
    assert "reclamo de pago" in incidencias("sesion_liberada")[0]["mensaje"]


def test_pago_reclamado_ofrece_emitir_a_mano_devolucion_y_sin_accion():
    acciones = atencion.acciones_para("pago_reclamado", "critica")
    assert {"emitio_manual", "devolucion", "sin_accion"} <= set(acciones)


# --------------------------------------------------------------------------- #
# Más tolerancia mientras llena datos (mirar otra app no cuesta los asientos)
# --------------------------------------------------------------------------- #
def test_seis_minutos_fuera_llenando_datos_no_suelta_la_venta(entorno_venta, cliente):
    async def escenario():
        await elegir_y_llenar(cliente)
        venta = entorno_venta.venta(BOOKING)
        venta.last_activity = time.monotonic() - 6 * 60
        return venta.motivo_para_liberar(20 * 60)
    assert correr(escenario()) is None


def test_once_minutos_fuera_llenando_datos_si_suelta_la_venta(entorno_venta, cliente):
    async def escenario():
        await elegir_y_llenar(cliente)
        venta = entorno_venta.venta(BOOKING)
        venta.last_activity = time.monotonic() - 11 * 60
        return venta.motivo_para_liberar(20 * 60)
    assert correr(escenario()).startswith("inactividad")
    assert vendedor_http.TIMEOUT_INACTIVIDAD_SELECCION_SEGUNDOS == 10 * 60


# --------------------------------------------------------------------------- #
# "No pagué: salir sin pagar" (pedido de boletería, 18/10: desde el aviso no
# había cómo salir). Antes de soltar nada se le pregunta a la pasarela.
# --------------------------------------------------------------------------- #
async def salir(c, booking=BOOKING):
    return await c.post("/api/v2/venta/salir-sin-pagar", json={"booking_id": booking})


def test_salir_sin_pagar_suelta_los_asientos_y_anula_el_pedido(entorno_venta, cliente):
    async def escenario():
        sesion = await abrir_niubiz(cliente)
        return sesion, await salir(cliente), await estado(cliente)
    sesion, r, e = correr(escenario())
    assert r.status_code == 200 and r.json()["estado"] == "salio"
    assert entorno_venta.venta(BOOKING) is None
    assert entorno_venta.jelaf.bloqueos == {}                       # asientos sueltos en JELAF
    assert db.consultar_uno("SELECT estado FROM pago WHERE booking_id = ?", (BOOKING,))[0] == "abandonado"
    assert e["estado"] == "sin_pago"
    [inc] = incidencias("sesion_liberada")
    assert inc["severidad"] == "leve" and sesion["pedido"] in inc["mensaje"] and "No pagué" in inc["mensaje"]
    assert entorno_venta.jelaf.llamadas["grabar_venta"] == 0


def test_tras_salir_un_aviso_de_niubiz_de_ese_pedido_no_cobra(entorno_venta, cliente):
    from tests.test_pago_niubiz import enviar_formulario

    async def escenario():
        sesion = await abrir_niubiz(cliente)
        await salir(cliente)
        return await enviar_formulario(cliente, sesion["action"])
    r = correr(escenario())
    assert r.headers["location"].endswith("niubiz=vencido")
    assert entorno_venta.niubiz.autorizaciones == []
    assert incidencias("niubiz_aviso_descartado")[0]["severidad"] == "grave"


def test_salir_sin_pagar_si_la_pasarela_ya_cobro_emite_en_vez_de_soltar(entorno_venta, cliente):
    from tests.test_venta import preparar_venta

    async def escenario():
        tx = await preparar_venta(cliente)
        entorno_venta.yupy.aprobar(tx)          # sí pagó (se equivocó de botón)
        return await salir(cliente)
    r = correr(escenario())
    assert r.status_code == 200 and r.json()["estado"] == "emitida"
    assert db.consultar_uno("SELECT estado FROM venta WHERE booking_id = ?", (BOOKING,))[0] == "emitida"


def test_salir_sin_pagar_con_un_aviso_de_pago_abierto_se_rechaza(entorno_venta, cliente):
    async def escenario():
        await abrir_niubiz(cliente)
        await ya_pague(cliente)
        return await salir(cliente)
    assert correr(escenario()).status_code == 409
    assert entorno_venta.venta(BOOKING) is not None


def test_salir_cancela_el_qr_de_yupy(entorno_venta, cliente, monkeypatch):
    from tests.test_venta import preparar_venta
    monkeypatch.setattr(entorno_venta.yupy, "api", "checkouts")

    async def escenario():
        tx = await preparar_venta(cliente)
        await salir(cliente)
        return tx
    tx = correr(escenario())
    assert entorno_venta.yupy.cancelados == [entorno_venta.yupy.tokens[tx]]


def test_despues_de_salir_la_misma_compra_puede_volver_a_pagar(entorno_venta, cliente):
    async def escenario():
        await abrir_niubiz(cliente)
        await salir(cliente)
        await elegir_y_llenar(cliente)
        return await cliente.post("/api/v2/venta/niubiz/sesion", json={"booking_id": BOOKING})
    r = correr(escenario())
    assert r.status_code == 200, r.text
    assert db.consultar_uno("SELECT estado FROM pago WHERE booking_id = ?", (BOOKING,))[0] == "pendiente"


def test_salir_cuando_la_pasarela_cobro_y_la_venta_ya_se_solto_avisa_cobrada(entorno_venta, cliente):
    from tests.test_venta import preparar_venta

    async def escenario():
        tx = await preparar_venta(cliente)
        entorno_venta.yupy.aprobar(tx)
        venta = entorno_venta.venta(BOOKING)
        async with venta.lock:
            await venta.liberar_y_resetear()
        return await salir(cliente)
    r = correr(escenario())
    assert r.status_code == 200 and r.json()["estado"] == "cobrada_sin_emitir"
    assert incidencias("cobrado_sin_emitir")[0]["severidad"] == "critica"
    assert incidencias("sesion_liberada") == []
