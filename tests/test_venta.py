"""
Flujo de venta por HTTP con dobles (sin JELAF, Yupy ni Meta). Cubre lo que
más dinero cuesta si se rompe: precio, monto, verificación de pago, emisión
exactamente una vez, fallos después de cobrar, bloqueos en JELAF y simulación.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from datetime import timedelta
from decimal import Decimal

import pytest

from paneles import consultas, db, tiempo

BOOKING = "11111111-2222-3333-4444-555555555555"
FECHA_VIAJE = (tiempo.hoy() + timedelta(days=7)).isoformat()   # siempre futura
VIAJE = {"codi_empresa": 1, "codi_destino": 1, "codi_sucursal": 2, "codi_ruta": 1,
         "codi_punto_venta": 17, "codi_servicio": 3}
DATOS_PERSONALES = ["00000001", "00000002", "ANA MARÍA", "987654321"]


def correr(coro):
    return asyncio.run(coro)


async def iniciar(c, booking=BOOKING, **extra):
    cuerpo = {"booking_id": booking, "codi_origen": 2, "fecha_viaje": FECHA_VIAJE,
              "hora_viaje": "08:30 PM", "viaje": VIAJE, "destino": "LIMA", "placa_bus": "B9J954"}
    cuerpo.update(extra)
    return await c.post("/api/v2/venta/iniciar", json=cuerpo)


async def elegir_y_llenar(c, asientos=("05",), booking=BOOKING):
    r = await iniciar(c, booking)
    assert r.status_code == 200, r.text
    for i, a in enumerate(asientos):
        r = await c.post("/api/v2/venta/seleccionar-asiento",
                         json={"booking_id": booking, "numero_asiento": a, "precio": 1})
        assert r.status_code == 200, r.text
        r = await c.post("/api/v2/venta/pasajero", json={
            "booking_id": booking, "numero_asiento": a, "documento": f"0000000{i + 1}",
            "nombres": "Ana María", "paterno": "Pérez", "materno": "Rojas", "sexo": "F",
            "tipo_documento": "01", "fecha_nacimiento": "1990-02-01", "precio_venta": 1,
            "telefono": "987654321"})
        assert r.status_code == 200, r.text


async def preparar_venta(c, asientos=("05",), booking=BOOKING):
    """iniciar → asientos → pasajeros → checkout. Devuelve el yupy_transaction_id."""
    await elegir_y_llenar(c, asientos, booking)
    r = await c.post("/api/v2/venta/checkout", json={"booking_id": booking})
    assert r.status_code == 200, r.text
    return r.json()["yupy_transaction_id"]


async def comprar(c, entorno, asientos=("05",), booking=BOOKING):
    tx = await preparar_venta(c, asientos, booking)
    entorno.yupy.aprobar(tx)
    return await c.post("/api/v2/venta/confirmar", json={"booking_id": booking, "referencia_pago": tx})


def contar(sql, params=()):
    return db.consultar_uno(sql, params)[0]


# --------------------------------------------------------------------------- #
# Precio y monto (V2)
# --------------------------------------------------------------------------- #
def test_precio_del_navegador_se_ignora_y_se_usa_el_de_jelaf(entorno_venta, cliente):
    async def escenario():
        await iniciar(cliente)
        return await cliente.post("/api/v2/venta/seleccionar-asiento",
                                  json={"booking_id": BOOKING, "numero_asiento": "5", "precio": 1})
    r = correr(escenario())
    assert r.status_code == 200 and r.json()["precio"] == 120.0
    assert entorno_venta.venta(BOOKING).asientos_en_venta["05"]["precio"] == Decimal("120.00")


def test_precio_por_asiento_de_jelaf_prevalece_sobre_el_general(entorno_venta, cliente):
    entorno_venta.jelaf.precios["07"] = Decimal("150.00")

    async def escenario():
        await iniciar(cliente)
        return await cliente.post("/api/v2/venta/seleccionar-asiento",
                                  json={"booking_id": BOOKING, "numero_asiento": "07"})
    assert correr(escenario()).json()["precio"] == 150.0


def test_sin_precio_de_jelaf_no_se_vende(entorno_venta, cliente):
    entorno_venta.jelaf.precio_general = Decimal("0")

    async def escenario():
        await iniciar(cliente)
        return await cliente.post("/api/v2/venta/seleccionar-asiento",
                                  json={"booking_id": BOOKING, "numero_asiento": "05"})
    assert correr(escenario()).status_code == 409
    assert entorno_venta.jelaf.llamadas["bloquear"] == 0


def test_grabar_venta_lleva_el_precio_de_jelaf(entorno_venta, cliente):
    r = correr(comprar(cliente, entorno_venta))
    assert r.status_code == 200
    item = entorno_venta.jelaf.ventas_recibidas[0]["Listado"][0]
    assert item["PrecioVenta"] == "120.00" and item["PrecioNormal"] == "120.00"


def test_checkout_cobra_el_monto_del_servidor(entorno_venta, cliente):
    correr(preparar_venta(cliente, asientos=("05", "06")))
    assert entorno_venta.yupy.creadas[0][1] == Decimal("240.00")
    assert consultas.pago_de_booking(BOOKING)["monto_centimos"] == 24000


def test_checkout_exige_pasajeros_completos(entorno_venta, cliente):
    async def escenario():
        await iniciar(cliente)
        await cliente.post("/api/v2/venta/seleccionar-asiento", json={"booking_id": BOOKING, "numero_asiento": "05"})
        return await cliente.post("/api/v2/venta/checkout", json={"booking_id": BOOKING})
    assert correr(escenario()).status_code == 409
    assert entorno_venta.yupy.creadas == []


def test_checkout_graba_pasajeros_en_jelaf_antes_de_cobrar(entorno_venta, cliente):
    correr(preparar_venta(cliente, asientos=("05", "06")))
    grabados = entorno_venta.jelaf.clientes_grabados
    assert [c["NumeroDoc"] for c in grabados] == ["00000001", "00000002"]
    assert grabados[0]["IdCliente"] == 555 and grabados[0]["Sexo"] == "F" and grabados[0]["TipoDoc"] == "01"


def test_si_jelaf_rechaza_los_pasajeros_no_se_cobra(entorno_venta, cliente):
    entorno_venta.jelaf.falla_grabar_pasajero = True

    async def escenario():
        await elegir_y_llenar(cliente)
        return await cliente.post("/api/v2/venta/checkout", json={"booking_id": BOOKING})
    assert correr(escenario()).status_code == 502
    assert entorno_venta.yupy.creadas == []


# --------------------------------------------------------------------------- #
# Verificación del pago (V1)
# --------------------------------------------------------------------------- #
def test_confirmar_sin_pago_no_emite(entorno_venta, cliente):
    async def escenario():
        await iniciar(cliente)
        return await cliente.post("/api/v2/venta/confirmar", json={"booking_id": BOOKING})
    assert correr(escenario()).status_code == 402
    assert entorno_venta.jelaf.llamadas["grabar_venta"] == 0


def test_confirmar_con_pago_no_aprobado_no_emite(entorno_venta, cliente):
    async def escenario():
        tx = await preparar_venta(cliente)
        return await cliente.post("/api/v2/venta/confirmar", json={"booking_id": BOOKING, "referencia_pago": tx})
    assert correr(escenario()).status_code == 402
    assert entorno_venta.jelaf.llamadas["grabar_venta"] == 0


def test_confirmar_con_monto_distinto_en_yupy_no_emite(entorno_venta, cliente):
    async def escenario():
        tx = await preparar_venta(cliente)
        entorno_venta.yupy.aprobar(tx, monto="1.00")
        return await cliente.post("/api/v2/venta/confirmar", json={"booking_id": BOOKING, "referencia_pago": tx})
    assert correr(escenario()).status_code == 402
    assert entorno_venta.jelaf.llamadas["grabar_venta"] == 0
    assert contar("SELECT COUNT(*) FROM incidencia WHERE tipo='pago_no_verificado' AND severidad='critica'") == 1


def test_referencia_de_pago_ajena_se_rechaza(entorno_venta, cliente):
    async def escenario():
        tx = await preparar_venta(cliente)
        entorno_venta.yupy.aprobar(tx)
        return await cliente.post("/api/v2/venta/confirmar",
                                  json={"booking_id": BOOKING, "referencia_pago": "yupy-de-otra-venta"})
    assert correr(escenario()).status_code == 402
    assert entorno_venta.jelaf.llamadas["grabar_venta"] == 0


# --------------------------------------------------------------------------- #
# Emisión exactamente una vez (N3 / C2)
# --------------------------------------------------------------------------- #
def test_confirmar_emite_por_http_y_registra_la_venta(entorno_venta, cliente):
    r = correr(comprar(cliente, entorno_venta, asientos=("05", "06")))
    assert r.status_code == 200
    boletos = r.json()["data"]["boletos"]
    assert [b["nro_boleto"] for b in boletos] == ["B001-00000001", "B001-00000002"]
    assert boletos[0]["link_comprobante"] == "https://fe.invalid/1"
    # Los bloqueos se sueltan justo antes de grabar-venta, como hace boletería.
    assert entorno_venta.jelaf.bloqueos == {}
    assert entorno_venta.jelaf.vendidos == {"05": "00000001", "06": "00000002"}
    venta = consultas.venta_de_booking(BOOKING)
    assert venta["estado"] == "emitida" and venta["monto_total"] == 240.0
    assert [b["link_comprobante"] for b in venta["boletos"]] == ["https://fe.invalid/1", "https://fe.invalid/2"]
    assert consultas.pago_de_booking(BOOKING)["estado"] == "usado"
    assert entorno_venta.venta(BOOKING) is None          # la venta ya no ocupa lugar


def test_pago_en_jelaf_lleva_los_4_ultimos_de_la_transaccion(entorno_venta, cliente):
    correr(comprar(cliente, entorno_venta))
    item = entorno_venta.jelaf.ventas_recibidas[0]["Listado"][0]
    assert item["TipoPago"] == "03" and item["CodiTarjetaCredito"] == "0201"
    assert item["NumeTarjetaCredito"] == "0000-0000-0000-0001"   # tx "yupy-0001"


def test_doble_confirmar_simultaneo_emite_una_sola_vez(entorno_venta, cliente):
    entorno_venta.jelaf.demora_emision = 0.2

    async def escenario():
        tx = await preparar_venta(cliente)
        entorno_venta.yupy.aprobar(tx)
        cuerpo = {"booking_id": BOOKING, "referencia_pago": tx}
        return await asyncio.gather(cliente.post("/api/v2/venta/confirmar", json=cuerpo),
                                    cliente.post("/api/v2/venta/confirmar", json=cuerpo))
    r1, r2 = correr(escenario())
    assert (r1.status_code, r2.status_code) == (200, 200) and r1.json() == r2.json()
    assert entorno_venta.jelaf.llamadas["grabar_venta"] == 1
    assert contar("SELECT COUNT(*) FROM venta") == 1
    assert contar("SELECT COUNT(*) FROM incidencia WHERE severidad='critica'") == 0


def test_reintento_de_confirmar_devuelve_lo_mismo_sin_reemitir(entorno_venta, cliente):
    async def escenario():
        tx = await preparar_venta(cliente)
        entorno_venta.yupy.aprobar(tx)
        cuerpo = {"booking_id": BOOKING, "referencia_pago": tx}
        a = await cliente.post("/api/v2/venta/confirmar", json=cuerpo)
        entorno_venta.api._emisiones.clear()   # aun sin la memoria, la base basta
        b = await cliente.post("/api/v2/venta/confirmar", json=cuerpo)
        return a, b
    a, b = correr(escenario())
    assert a.status_code == b.status_code == 200
    assert a.json()["data"]["boletos"][0]["nro_boleto"] == b.json()["data"]["boletos"][0]["nro_boleto"]
    assert entorno_venta.jelaf.llamadas["grabar_venta"] == 1


# --------------------------------------------------------------------------- #
# Fallos después de cobrar (C4 / N4 / N5)
# --------------------------------------------------------------------------- #
def test_pago_aprobado_con_venta_ya_liberada_queda_registrado(entorno_venta, cliente):
    async def escenario():
        tx = await preparar_venta(cliente)
        entorno_venta.yupy.aprobar(tx)
        venta = entorno_venta.venta(BOOKING)
        async with venta.lock:                  # el reaper la soltó
            await venta.liberar_y_resetear()
        return await cliente.post("/api/v2/venta/confirmar", json={"booking_id": BOOKING, "referencia_pago": tx})
    r = correr(escenario())
    assert r.status_code == 409 and "No vuelvas a pagar" in r.json()["detail"]
    assert consultas.venta_de_booking(BOOKING)["estado"] == "cobrada_sin_emitir"
    assert contar("SELECT COUNT(*) FROM incidencia WHERE tipo='cobrado_sin_emitir' AND severidad='critica'") == 1


def test_jelaf_rechaza_la_venta_es_cobrado_sin_emitir_sin_conciliar(entorno_venta, cliente):
    entorno_venta.jelaf.falla_grabar_venta = "rechazo"

    async def escenario():
        tx = await preparar_venta(cliente)
        entorno_venta.yupy.aprobar(tx)
        antes = entorno_venta.jelaf.llamadas["turno"]
        r = await cliente.post("/api/v2/venta/confirmar", json={"booking_id": BOOKING, "referencia_pago": tx})
        return r, entorno_venta.jelaf.llamadas["turno"] - antes
    r, turnos = correr(escenario())
    assert r.status_code == 409
    assert consultas.venta_de_booking(BOOKING)["estado"] == "cobrada_sin_emitir"
    assert turnos == 1   # solo el turno para armar la venta: un rechazo claro no necesita conciliar


def test_jelaf_emite_pero_no_responde_se_concilia_como_emitida(entorno_venta, cliente):
    entorno_venta.jelaf.falla_grabar_venta = "emite_y_cae"
    r = correr(comprar(cliente, entorno_venta))
    assert r.status_code == 200
    assert consultas.venta_de_booking(BOOKING)["estado"] == "emitida"
    assert contar("SELECT COUNT(*) FROM incidencia WHERE tipo='emision_conciliada'") == 1


def test_jelaf_no_responde_y_no_emitio_es_cobrado_sin_emitir(entorno_venta, cliente):
    entorno_venta.jelaf.falla_grabar_venta = "sin_respuesta"
    r = correr(comprar(cliente, entorno_venta))
    assert r.status_code == 409
    assert consultas.venta_de_booking(BOOKING)["estado"] == "cobrada_sin_emitir"


def test_sin_respuesta_y_asiento_ocupado_por_otro_queda_por_conciliar(entorno_venta, cliente):
    entorno_venta.jelaf.falla_grabar_venta = "sin_respuesta"

    async def escenario():
        tx = await preparar_venta(cliente)
        entorno_venta.yupy.aprobar(tx)
        entorno_venta.jelaf.vendidos["05"] = "99999999"   # otro canal lo vendió en el hueco
        return await cliente.post("/api/v2/venta/confirmar", json={"booking_id": BOOKING, "referencia_pago": tx})
    assert correr(escenario()).status_code == 409
    assert consultas.venta_de_booking(BOOKING)["estado"] == "por_conciliar"


def test_asientos_cambiados_despues_de_pagar_no_se_emiten(entorno_venta, cliente):
    async def escenario():
        tx = await preparar_venta(cliente)
        entorno_venta.yupy.aprobar(tx)
        venta = entorno_venta.venta(BOOKING)
        await venta.seleccionar_asiento("09", Decimal("120.00"))      # cambio colado
        venta.asientos_en_venta["09"].update(pasajero_cargado=True, documento="9", nombres="X", paterno="Y",
                                             fecha_nacimiento="01/01/1990", sexo="M")
        return await cliente.post("/api/v2/venta/confirmar", json={"booking_id": BOOKING, "referencia_pago": tx})
    assert correr(escenario()).status_code == 409
    assert entorno_venta.jelaf.llamadas["grabar_venta"] == 0
    assert consultas.venta_de_booking(BOOKING)["estado"] == "cobrada_sin_emitir"
    assert entorno_venta.jelaf.bloqueos == {}           # los bloqueos se soltaron al cerrar


# --------------------------------------------------------------------------- #
# Bloqueos en JELAF
# --------------------------------------------------------------------------- #
def test_seleccionar_bloquea_y_deseleccionar_suelta_en_jelaf(entorno_venta, cliente):
    async def escenario():
        await iniciar(cliente)
        await cliente.post("/api/v2/venta/seleccionar-asiento", json={"booking_id": BOOKING, "numero_asiento": "05"})
        bloqueados = dict(entorno_venta.jelaf.bloqueos)
        await cliente.post("/api/v2/venta/liberar-asiento", json={"booking_id": BOOKING, "numero_asiento": "05"})
        return bloqueados
    assert list(correr(escenario()).values()) == ["05"]
    assert entorno_venta.jelaf.bloqueos == {}


def test_cancelar_suelta_los_bloqueos(entorno_venta, cliente):
    async def escenario():
        await iniciar(cliente)
        for a in ("05", "06"):
            await cliente.post("/api/v2/venta/seleccionar-asiento", json={"booking_id": BOOKING, "numero_asiento": a})
        return await cliente.post("/api/v2/venta/cancelar", json={"booking_id": BOOKING})
    assert correr(escenario()).status_code == 200
    assert entorno_venta.jelaf.bloqueos == {} and entorno_venta.venta(BOOKING) is None


def test_asiento_vendido_en_el_plano_no_se_bloquea(entorno_venta, cliente):
    entorno_venta.jelaf.vendidos["05"] = "88888888"

    async def escenario():
        await iniciar(cliente)
        return await cliente.post("/api/v2/venta/seleccionar-asiento",
                                  json={"booking_id": BOOKING, "numero_asiento": "05"})
    assert correr(escenario()).status_code == 409
    assert entorno_venta.jelaf.llamadas["bloquear"] == 0


def test_limite_de_bloqueos_de_jelaf_se_respeta(entorno_venta, cliente):
    entorno_venta.jelaf.max_bloqueos = 2

    async def escenario():
        await iniciar(cliente)
        codigos = []
        for a in ("05", "06", "07"):
            r = await cliente.post("/api/v2/venta/seleccionar-asiento",
                                   json={"booking_id": BOOKING, "numero_asiento": a})
            codigos.append(r.status_code)
        return codigos
    assert correr(escenario()) == [200, 200, 409]


# --------------------------------------------------------------------------- #
# Dueño de la venta y protecciones (S2 / S3 / N4)
# --------------------------------------------------------------------------- #
def test_otro_booking_no_puede_operar_la_venta(entorno_venta, cliente):
    async def escenario():
        await iniciar(cliente)
        return await cliente.post("/api/v2/venta/seleccionar-asiento",
                                  json={"booking_id": "otro-booking-0000", "numero_asiento": "05"})
    assert correr(escenario()).status_code == 403


def test_no_se_puede_cancelar_ni_cambiar_asientos_con_el_pago_en_curso(entorno_venta, cliente):
    async def escenario():
        await preparar_venta(cliente)
        a = await cliente.post("/api/v2/venta/cancelar", json={"booking_id": BOOKING})
        b = await cliente.post("/api/v2/venta/liberar-asiento", json={"booking_id": BOOKING, "numero_asiento": "05"})
        return a, b
    a, b = correr(escenario())
    assert a.status_code == b.status_code == 409
    assert entorno_venta.venta(BOOKING) is not None and entorno_venta.jelaf.bloqueos


def test_estado_del_pool_exige_clave_y_enmascara_el_booking(entorno_venta, cliente):
    async def escenario():
        await iniciar(cliente)
        sin = await cliente.get("/api/v2/venta/estado")
        con = await cliente.get("/api/v2/venta/estado", headers={"X-Paneles-Token": "clave-panel-prueba"})
        return sin, con
    sin, con = correr(escenario())
    assert sin.status_code == 401
    assert BOOKING not in con.text and con.json()["workers"][0]["owner_token"].startswith(BOOKING[:8])


def test_iniciar_exige_enlace_de_whatsapp_si_esta_configurado(entorno_venta, cliente, monkeypatch):
    import dataclasses

    import whatsapp
    api = entorno_venta.api
    monkeypatch.setattr(api, "CONFIG", dataclasses.replace(api.CONFIG, exigir_contacto=True))
    token = whatsapp.firmar_contacto("51987654321", api.CONFIG.enlace_secreto)

    async def escenario():
        return await iniciar(cliente), await iniciar(cliente, contacto=token)
    sin, con = correr(escenario())
    assert sin.status_code == 403 and con.status_code == 200


def test_consultar_cliente_exige_venta_activa_y_no_devuelve_sexo(entorno_venta, cliente):
    async def escenario():
        cuerpo = {"booking_id": BOOKING, "tipo_documento": "01", "documento": "12345670"}
        sin = await cliente.post("/api/v1/consultar-cliente", json=cuerpo)
        await iniciar(cliente)
        con = await cliente.post("/api/v1/consultar-cliente", json=cuerpo)
        return sin, con
    sin, con = correr(escenario())
    assert sin.status_code == 403
    assert con.status_code == 200 and con.json()["fecha_nacimiento"] == "1990-02-01" and "sexo" not in con.json()


def test_pasajero_sin_sexo_o_con_ruc_se_rechaza(entorno_venta, cliente):
    async def escenario():
        await iniciar(cliente)
        await cliente.post("/api/v2/venta/seleccionar-asiento", json={"booking_id": BOOKING, "numero_asiento": "05"})
        base = {"booking_id": BOOKING, "numero_asiento": "05", "documento": "00000001", "nombres": "Ana",
                "paterno": "Perez", "fecha_nacimiento": "1990-02-01"}
        sin_sexo = await cliente.post("/api/v2/venta/pasajero", json=base)
        con_ruc = await cliente.post("/api/v2/venta/pasajero", json={**base, "sexo": "F", "tipo_documento": "04"})
        return sin_sexo, con_ruc
    a, b = correr(escenario())
    assert a.status_code == b.status_code == 422


# --------------------------------------------------------------------------- #
# Modo simulación
# --------------------------------------------------------------------------- #
def test_simulacion_no_escribe_en_jelaf_y_guarda_el_cuerpo_sin_datos_personales(entorno_venta, cliente, tmp_path):
    entorno_venta.vendedor.simulacion = True
    r = correr(comprar(cliente, entorno_venta, asientos=("05", "06")))
    assert r.status_code == 200
    assert [b["nro_boleto"] for b in r.json()["data"]["boletos"]] == ["SIMULADO-05", "SIMULADO-06"]
    j = entorno_venta.jelaf
    assert j.llamadas["bloquear"] == j.llamadas["grabar_venta"] == j.llamadas["liberar"] == 0
    assert j.clientes_grabados == [] and j.vendidos == {}
    assert consultas.venta_de_booking(BOOKING)["estado"] == "simulada"
    archivos = list((tmp_path / "simulacion").glob("simulacion_*.json"))
    assert len(archivos) == 1
    texto = archivos[0].read_text(encoding="utf-8")
    assert not any(d in texto for d in DATOS_PERSONALES)
    assert len(json.loads(texto)["Listado"]) == 2


# --------------------------------------------------------------------------- #
# PDF, recargas y rutas retiradas
# --------------------------------------------------------------------------- #
def test_pdf_solo_de_boletos_emitidos(entorno_venta, cliente):
    async def escenario():
        antes = await cliente.post("/api/v2/boleto/pdf", json={"booking_id": BOOKING, "asiento": "05"})
        await comprar(cliente, entorno_venta)
        despues = await cliente.post("/api/v2/boleto/pdf", json={"booking_id": BOOKING, "asiento": "05"})
        return antes, despues
    antes, despues = correr(escenario())
    assert antes.status_code == 404
    assert despues.status_code == 200 and despues.content.startswith(b"%PDF")


def test_pdf_por_enlace_firmado_para_el_navegador_de_whatsapp(entorno_venta, cliente):
    # El navegador de WhatsApp no descarga un blob: le pasa la URL a otra app.
    # Por eso el PDF tiene que tener una URL https real, firmada y sin el booking_id.
    async def escenario():
        antes = await cliente.post("/api/v2/boleto/enlace", json={"booking_id": BOOKING, "asiento": "05"})
        await comprar(cliente, entorno_venta)
        enlace = await cliente.post("/api/v2/boleto/enlace", json={"booking_id": BOOKING, "asiento": "05"})
        otro = await cliente.post("/api/v2/boleto/enlace", json={"booking_id": BOOKING, "asiento": "06"})
        url = enlace.json()["url"]
        pdf = await cliente.get(url)
        alterado = await cliente.get(url[:-2] + ("AA" if not url.endswith("AA") else "BB"))
        return antes, enlace, otro, url, pdf, alterado
    antes, enlace, otro, url, pdf, alterado = correr(escenario())
    assert antes.status_code == 404 and otro.status_code == 404
    assert enlace.status_code == 200 and url.startswith("/api/v2/boleto/pdf/")
    # base64 no oculta nada: el booking_id (la llave de la compra) no puede ir dentro.
    token = url.rsplit("/", 1)[1]
    assert BOOKING.encode() not in base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    # inline, no attachment: en el navegador de WhatsApp (Android, prueba del 14/10/2026)
    # abrir el PDF funciona y "descargar" (download/attachment) muestra el aviso y no hace nada.
    assert pdf.headers["content-disposition"].startswith("inline;")
    assert "no-store" in pdf.headers["cache-control"]
    assert alterado.status_code == 404


def test_enlace_del_pdf_vence_y_no_sirve_como_enlace_de_compra(entorno_venta, cliente):
    import whatsapp
    secreto = entorno_venta.api.CONFIG.enlace_secreto
    viejo = whatsapp.firmar_pdf(1, "05", secreto, ahora=time.time() - whatsapp.PDF_VIGENCIA_SEGUNDOS - 5)
    assert whatsapp.verificar_pdf(viejo, secreto) is None
    assert whatsapp.verificar_pdf(whatsapp.firmar_pdf(1, "05", secreto), secreto) == (1, "05")
    assert whatsapp.verificar_pdf(whatsapp.firmar_pdf(1, "05", secreto), b"otro-secreto" * 4) is None
    # Ninguno de los dos tokens sirve en lugar del otro.
    assert whatsapp.verificar_pdf(whatsapp.firmar_contacto("51987654321", secreto), secreto) is None
    assert whatsapp.verificar_contacto(whatsapp.firmar_pdf(51987654321, "05", secreto), secreto) is None

    async def escenario():
        await comprar(cliente, entorno_venta)
        venta_id = consultas.venta_de_booking(BOOKING)["id"]
        vencido = whatsapp.firmar_pdf(venta_id, "05", secreto, ahora=time.time() - whatsapp.PDF_VIGENCIA_SEGUNDOS - 5)
        vigente = whatsapp.firmar_pdf(venta_id, "05", secreto)
        return await cliente.get(f"/api/v2/boleto/pdf/{vencido}"), await cliente.get(f"/api/v2/boleto/pdf/{vigente}")
    vencido, vigente = correr(escenario())
    assert vencido.status_code == 404 and vigente.status_code == 200


def test_tras_emitir_se_mandan_los_boletos_por_correo_una_sola_vez(entorno_venta, cliente):
    # El navegador de WhatsApp no deja descargar: la pantalla solo muestra el
    # boleto y el PDF llega por correo (decisión del 14/10/2026).
    async def escenario():
        await elegir_y_llenar(cliente, asientos=("05", "06"))
        c = await cliente.post("/api/v2/venta/contacto", json={"booking_id": BOOKING, "correo": " Cliente@Ejemplo.com "})
        assert c.status_code == 200 and c.json()["correo"] == "cliente@ejemplo.com"
        tx = (await cliente.post("/api/v2/venta/checkout", json={"booking_id": BOOKING})).json()["yupy_transaction_id"]
        entorno_venta.yupy.aprobar(tx)
        uno = await cliente.post("/api/v2/venta/confirmar", json={"booking_id": BOOKING, "referencia_pago": tx})
        dos = await cliente.post("/api/v2/venta/confirmar", json={"booking_id": BOOKING, "referencia_pago": tx})
        await asyncio.sleep(0.3)        # el envío va en segundo plano
        return uno, dos
    uno, dos = correr(escenario())
    assert uno.status_code == dos.status_code == 200
    assert len(entorno_venta.correos_enviados) == 1                 # una vez, aunque /confirmar se repita
    msg = entorno_venta.correos_enviados[0]
    assert msg["To"] == "cliente@ejemplo.com"
    adjuntos = list(msg.iter_attachments())
    assert len(adjuntos) == 2 and all(a.get_content().startswith(b"%PDF") for a in adjuntos)   # uno por asiento
    html = msg.get_body(("html",)).get_content()
    assert "Se realizó su compra con éxito" in html and "TACNA" in html   # embarque del turno
    assert consultas.venta_de_booking(BOOKING)["correo"] == "cliente@ejemplo.com"


async def _comprar_con_correo(cliente, entorno, correo="cliente@ejemplo.com"):
    await elegir_y_llenar(cliente)
    await cliente.post("/api/v2/venta/contacto", json={"booking_id": BOOKING, "correo": correo})
    tx = (await cliente.post("/api/v2/venta/checkout", json={"booking_id": BOOKING})).json()["yupy_transaction_id"]
    entorno.yupy.aprobar(tx)
    r = await cliente.post("/api/v2/venta/confirmar", json={"booking_id": BOOKING, "referencia_pago": tx})
    assert r.status_code == 200, r.text
    await asyncio.sleep(0.3)


def test_la_pantalla_ve_si_el_correo_salio(entorno_venta, cliente):
    async def escenario():
        await _comprar_con_correo(cliente, entorno_venta)
        return await cliente.post("/api/v2/boleto/correo-estado", json={"booking_id": BOOKING})
    r = correr(escenario())
    assert r.json() == {"status": "success", "estado": "enviado", "correo": "cliente@ejemplo.com",
                        "motivo": None, "reenvios_restantes": 3}


def test_correo_rechazado_el_cliente_lo_corrige_y_se_reenvia(entorno_venta, cliente):
    from correo import Resultado
    entorno_venta.resultados_correo.append(Resultado(False, "rechazada", "destinatario"))

    async def escenario():
        await _comprar_con_correo(cliente, entorno_venta, "clinete@ejemplo.com")
        fallido = await cliente.post("/api/v2/boleto/correo-estado", json={"booking_id": BOOKING})
        reenvio = await cliente.post("/api/v2/boleto/correo-reenviar",
                                     json={"booking_id": BOOKING, "correo": "cliente@ejemplo.com"})
        await asyncio.sleep(0.3)
        final = await cliente.post("/api/v2/boleto/correo-estado", json={"booking_id": BOOKING})
        return fallido, reenvio, final
    fallido, reenvio, final = correr(escenario())
    assert fallido.json()["estado"] == "fallido" and "clinete@ejemplo.com" in fallido.json()["motivo"]
    assert reenvio.status_code == 200 and reenvio.json()["estado"] == "pendiente"
    assert final.json()["estado"] == "enviado" and final.json()["correo"] == "cliente@ejemplo.com"
    assert final.json()["reenvios_restantes"] == 2
    assert [m["To"] for m in entorno_venta.correos_enviados] == ["clinete@ejemplo.com", "cliente@ejemplo.com"]
    assert consultas.venta_de_booking(BOOKING)["correo"] == "cliente@ejemplo.com"
    # Dirección mal escrita: la corrige el cliente; no es una incidencia para el equipo.
    assert contar("SELECT COUNT(*) FROM incidencia WHERE tipo = 'correo_fallido'") == 0


def test_falla_del_servicio_de_correo_queda_como_incidencia(entorno_venta, cliente):
    from correo import Resultado
    entorno_venta.resultados_correo.append(Resultado(False, "sin conexión", "servicio"))

    async def escenario():
        await _comprar_con_correo(cliente, entorno_venta)
        return await cliente.post("/api/v2/boleto/correo-estado", json={"booking_id": BOOKING})
    r = correr(escenario())
    assert r.json()["estado"] == "fallido" and "captura" in r.json()["motivo"]
    assert contar("SELECT COUNT(*) FROM incidencia WHERE tipo = 'correo_fallido'") == 1


def test_reenvios_con_tope_y_solo_de_compras_emitidas(entorno_venta, cliente):
    async def escenario():
        sin_compra = await cliente.post("/api/v2/boleto/correo-estado", json={"booking_id": BOOKING})
        await _comprar_con_correo(cliente, entorno_venta)
        codigos = []
        for _ in range(4):
            r = await cliente.post("/api/v2/boleto/correo-reenviar",
                                   json={"booking_id": BOOKING, "correo": "otro@ejemplo.com"})
            codigos.append(r.status_code)
            await asyncio.sleep(0.2)
        malo = await cliente.post("/api/v2/boleto/correo-reenviar", json={"booking_id": BOOKING, "correo": "x"})
        return sin_compra, codigos, malo
    sin_compra, codigos, malo = correr(escenario())
    assert sin_compra.status_code == 404
    assert codigos == [200, 200, 200, 429]
    assert malo.status_code == 422


def test_sin_correo_no_se_manda_nada(entorno_venta, cliente):
    async def escenario():
        await comprar(cliente, entorno_venta)
        await asyncio.sleep(0.2)
    correr(escenario())
    assert entorno_venta.correos_enviados == []


def test_correo_obligatorio_para_cobrar_y_validado(entorno_venta, cliente, monkeypatch):
    import dataclasses
    api = entorno_venta.api
    monkeypatch.setattr(api, "CONFIG", dataclasses.replace(api.CONFIG, exigir_correo=True))

    async def escenario():
        await elegir_y_llenar(cliente)
        sin = await cliente.post("/api/v2/venta/checkout", json={"booking_id": BOOKING})
        malo = await cliente.post("/api/v2/venta/contacto", json={"booking_id": BOOKING, "correo": "sin-arroba"})
        await cliente.post("/api/v2/venta/contacto", json={"booking_id": BOOKING, "correo": "a@ejemplo.com"})
        con = await cliente.post("/api/v2/venta/checkout", json={"booking_id": BOOKING})
        return sin, malo, con
    sin, malo, con = correr(escenario())
    assert sin.status_code == 409 and "correo" in sin.json()["detail"]
    assert malo.status_code == 422
    assert con.status_code == 200


# --------------------------------------------------------------------------- #
# Factura (RUC 10 o 20; una por asiento, todas al mismo RUC)
# --------------------------------------------------------------------------- #
RUC = "20534857153"


def test_consultar_ruc_solo_con_venta_activa_y_ruc_valido(entorno_venta, cliente):
    async def escenario():
        sin_venta = await cliente.post("/api/v2/venta/consultar-ruc", json={"booking_id": BOOKING, "ruc": RUC})
        await iniciar(cliente)
        mal = await cliente.post("/api/v2/venta/consultar-ruc", json={"booking_id": BOOKING, "ruc": "20534857154"})
        bien = await cliente.post("/api/v2/venta/consultar-ruc", json={"booking_id": BOOKING, "ruc": RUC})
        desconocido = await cliente.post("/api/v2/venta/consultar-ruc",
                                         json={"booking_id": BOOKING, "ruc": "10726778537"})
        return sin_venta, mal, bien, desconocido
    sin_venta, mal, bien, desconocido = correr(escenario())
    assert sin_venta.status_code == 403 and mal.status_code == 422
    assert bien.json() == {"status": "success", "encontrado": True, "razon_social": "EMPRESA CLIENTE S.A.C.",
                           "direccion": "AV. LIMA 123"}
    assert desconocido.json()["encontrado"] is False       # el cliente escribe la razón social a mano


def test_factura_llega_a_jelaf_a_la_ficha_y_al_pdf(entorno_venta, cliente):
    async def escenario():
        await elegir_y_llenar(cliente, asientos=("05", "06"))
        r = await cliente.post("/api/v2/venta/comprobante", json={
            "booking_id": BOOKING, "tipo": "factura", "ruc": RUC,
            "razon_social": "Empresa Cliente S.A.C.", "direccion": "Av. Lima 123"})
        assert r.status_code == 200, r.text
        tx = (await cliente.post("/api/v2/venta/checkout", json={"booking_id": BOOKING})).json()["yupy_transaction_id"]
        entorno_venta.yupy.aprobar(tx)
        conf = await cliente.post("/api/v2/venta/confirmar", json={"booking_id": BOOKING, "referencia_pago": tx})
        pdf = await cliente.post("/api/v2/boleto/pdf", json={"booking_id": BOOKING, "asiento": "05"})
        return conf, pdf
    conf, pdf = correr(escenario())
    assert conf.status_code == 200 and pdf.status_code == 200
    venta = entorno_venta.jelaf.ventas_recibidas[-1]
    assert [i["RucCliente"] for i in venta["Listado"]] == [RUC, RUC]          # una factura por asiento
    assert all(i["NomEmpresaRuc"] == "EMPRESA CLIENTE S.A.C." for i in venta["Listado"])
    assert all(c["RucContacto"] == RUC for c in entorno_venta.jelaf.clientes_grabados[-2:])
    guardada = consultas.venta_de_booking(BOOKING)
    assert guardada["comprobante"] == "factura" and guardada["ruc_cliente"] == RUC
    import boleto_pdf
    assert boleto_pdf.titulo_comprobante(guardada) == "FACTURA ELECTRÓNICA"


def test_sin_elegir_comprobante_es_boleta_y_se_puede_volver_a_boleta(entorno_venta, cliente):
    async def escenario():
        await elegir_y_llenar(cliente)
        await cliente.post("/api/v2/venta/comprobante", json={
            "booking_id": BOOKING, "tipo": "factura", "ruc": RUC, "razon_social": "X SAC"})
        await cliente.post("/api/v2/venta/comprobante", json={"booking_id": BOOKING, "tipo": "boleta"})
        tx = (await cliente.post("/api/v2/venta/checkout", json={"booking_id": BOOKING})).json()["yupy_transaction_id"]
        entorno_venta.yupy.aprobar(tx)
        return await cliente.post("/api/v2/venta/confirmar", json={"booking_id": BOOKING, "referencia_pago": tx})
    assert correr(escenario()).status_code == 200
    assert entorno_venta.jelaf.ventas_recibidas[-1]["Listado"][0]["RucCliente"] == ""
    guardada = consultas.venta_de_booking(BOOKING)
    assert guardada["comprobante"] == "boleta" and not guardada["ruc_cliente"]
    import boleto_pdf
    assert boleto_pdf.titulo_comprobante(guardada) == "BOLETA DE VENTA ELECTRÓNICA"


def test_factura_con_ruc_invalido_o_sin_razon_social_se_rechaza(entorno_venta, cliente):
    async def escenario():
        await elegir_y_llenar(cliente)
        a = await cliente.post("/api/v2/venta/comprobante", json={
            "booking_id": BOOKING, "tipo": "factura", "ruc": "20534857154", "razon_social": "X SAC"})
        b = await cliente.post("/api/v2/venta/comprobante", json={
            "booking_id": BOOKING, "tipo": "factura", "ruc": RUC, "razon_social": "  "})
        return a, b
    a, b = correr(escenario())
    assert a.status_code == 422 and b.status_code == 422


def test_pago_en_un_checkout_anterior_no_se_pierde(entorno_venta, cliente):
    async def escenario():
        tx1 = await preparar_venta(cliente)
        await cliente.post("/api/v2/venta/checkout", json={"booking_id": BOOKING})
        entorno_venta.yupy.aprobar(tx1)
        return await cliente.post("/api/v2/venta/confirmar", json={"booking_id": BOOKING, "referencia_pago": tx1})
    assert correr(escenario()).status_code == 200
    assert entorno_venta.jelaf.llamadas["grabar_venta"] == 1


@pytest.mark.parametrize("ruta", ["/api/v1/confirmar-compra", "/api/v1/bloquear-asiento",
                                  "/api/v1/liberar-asiento", "/api/v1/pre-checkout",
                                  "/api/v1/descargar-boleto", "/api/v1/generar-checkout-yupi"])
def test_rutas_v1_peligrosas_ya_no_existen(entorno_venta, cliente, ruta):
    async def escenario():
        return await cliente.post(ruta, json={})
    assert correr(escenario()).status_code == 404
