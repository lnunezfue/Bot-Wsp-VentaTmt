"""
Pago con tarjeta (Niubiz) y API /v1/checkouts de Yupy, con dobles (sin red).
Lo que importa: el cargo lo hace el servidor con SU monto, una sola vez por
pedido, y /confirmar emite solo con una autorización verificada.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import pytest

import pagos_niubiz
import pagos_yupy
from paneles import consultas, db
from tests.test_venta import BOOKING, elegir_y_llenar


def correr(coro):
    return asyncio.run(coro)


async def preparar_pasajeros(c, asientos=("05",)):
    await elegir_y_llenar(c, asientos)


async def abrir_sesion(c, asientos=("05",)):
    await preparar_pasajeros(c, asientos)
    r = await c.post("/api/v2/venta/niubiz/sesion", json={"booking_id": BOOKING})
    assert r.status_code == 200, r.text
    return r.json()


async def enviar_formulario(c, action, token="tok-123", extra=""):
    return await c.post(action + extra, content=f"transactionToken={token}&channel=web",
                        headers={"Content-Type": "application/x-www-form-urlencoded"})


def contar(sql, params=()):
    return db.consultar_uno(sql, params)[0]


# --------------------------------------------------------------------------- #
# Decisiones puras
# --------------------------------------------------------------------------- #
AUTORIZADA = {"order": {"purchaseNumber": "123456789012", "amount": 120.0, "currency": "PEN"},
              "dataMap": {"STATUS": "Authorized", "ACTION_CODE": "000", "CARD": "455170******8059",
                          "TRACE_NUMBER": "9"}}


def test_autorizacion_por_el_monto_exacto_es_valida():
    assert pagos_niubiz.evaluar_autorizacion(AUTORIZADA, "123456789012", Decimal("120")) is None


@pytest.mark.parametrize("cambio, motivo", [
    ({"dataMap": {"STATUS": "Reject", "ACTION_CODE": "190"}}, "no está autorizado"),
    ({"dataMap": {"STATUS": "Authorized", "ACTION_CODE": "101"}}, "no está autorizado"),
    ({"order": {"purchaseNumber": "123456789012", "amount": 1.0, "currency": "PEN"}}, "monto"),
    ({"order": {"purchaseNumber": "999", "amount": 120.0, "currency": "PEN"}}, "otro pedido"),
    ({"order": {"purchaseNumber": "123456789012", "amount": 120.0, "currency": "USD"}}, "moneda"),
])
def test_autorizacion_que_no_prueba_el_cobro_correcto_se_rechaza(cambio, motivo):
    respuesta = {**AUTORIZADA, **cambio}
    assert motivo in pagos_niubiz.evaluar_autorizacion(respuesta, "123456789012", Decimal("120"))


def test_moneda_en_codigo_numerico_es_pen():
    respuesta = {**AUTORIZADA, "dataMap": {**AUTORIZADA["dataMap"], "CURRENCY": "0604"}}
    assert pagos_niubiz.evaluar_autorizacion(respuesta, "123456789012", Decimal("120")) is None


def test_resumen_no_guarda_la_tarjeta_y_sigue_sirviendo_para_verificar():
    resumen = pagos_niubiz.resumen_autorizacion(AUTORIZADA)
    assert "455170" not in json.dumps(resumen) and resumen["ultimos4"] == "8059"
    assert pagos_niubiz.evaluar_autorizacion(pagos_niubiz.reconstruir(resumen), "123456789012",
                                             Decimal("120")) is None


# --------------------------------------------------------------------------- #
# Flujo con el backend
# --------------------------------------------------------------------------- #
def test_flujo_completo_cobra_el_monto_del_servidor_y_emite_una_vez(entorno_venta, cliente):
    async def escenario():
        sesion = await abrir_sesion(cliente)
        # Alguien manipula la URL del formulario: el monto de la URL se ignora.
        r = await enviar_formulario(cliente, sesion["action"], extra="&monto=1.00")
        c1 = await cliente.post("/api/v2/venta/confirmar", json={"booking_id": BOOKING, "medio_pago": "NIUBIZ"})
        c2 = await cliente.post("/api/v2/venta/confirmar", json={"booking_id": BOOKING, "medio_pago": "NIUBIZ"})
        return sesion, r, c1, c2

    sesion, r, c1, c2 = correr(escenario())
    assert sesion["monto_total"] == float(entorno_venta.jelaf.precio_general) and sesion["pedido"].isdigit() and len(sesion["pedido"]) == 12
    assert r.status_code == 303
    assert r.headers["location"] == entorno_venta.api._url_pantalla_pago() + "?niubiz=ok"
    assert entorno_venta.niubiz.autorizaciones == [("tok-123", sesion["pedido"], Decimal(str(sesion["monto_total"])).quantize(Decimal("0.01")))]
    assert c1.status_code == c2.status_code == 200
    assert c1.json() == c2.json()
    assert entorno_venta.jelaf.llamadas["grabar_venta"] == 1
    venta = consultas.venta_de_booking(BOOKING)
    assert venta["estado"] == "emitida" and venta["referencia_pago"] == sesion["referencia_pago"]


def test_doble_envio_del_formulario_autoriza_una_sola_vez(entorno_venta, cliente):
    async def escenario():
        sesion = await abrir_sesion(cliente)
        a, b = await asyncio.gather(enviar_formulario(cliente, sesion["action"]),
                                    enviar_formulario(cliente, sesion["action"], token="tok-otro"))
        return a, b
    a, b = correr(escenario())
    assert len(entorno_venta.niubiz.autorizaciones) == 1
    assert a.headers["location"].endswith("niubiz=ok") and b.headers["location"].endswith("niubiz=ok")


def test_pedido_que_ya_no_es_el_vigente_no_se_cobra(entorno_venta, cliente):
    async def escenario():
        vieja = await abrir_sesion(cliente)
        nueva = await cliente.post("/api/v2/venta/niubiz/sesion", json={"booking_id": BOOKING})
        r = await enviar_formulario(cliente, vieja["action"])
        return nueva, r
    nueva, r = correr(escenario())
    assert nueva.status_code == 200
    assert r.headers["location"].endswith("niubiz=vencido")
    assert entorno_venta.niubiz.autorizaciones == []


def test_tarjeta_rechazada_no_emite_y_no_guarda_la_tarjeta(entorno_venta, cliente):
    entorno_venta.niubiz.rechazar = True

    async def escenario():
        sesion = await abrir_sesion(cliente)
        r = await enviar_formulario(cliente, sesion["action"])
        c = await cliente.post("/api/v2/venta/confirmar", json={"booking_id": BOOKING})
        return r, c
    r, c = correr(escenario())
    assert r.headers["location"].endswith("niubiz=denegado")
    assert c.status_code == 402
    assert entorno_venta.jelaf.llamadas["grabar_venta"] == 0
    guardado = db.consultar_uno("SELECT autorizacion FROM pago WHERE booking_id = ?", (BOOKING,))[0]
    assert "455170" not in guardado
    assert contar("SELECT COUNT(*) FROM incidencia WHERE tipo = 'pago_rechazado'") == 1


def test_cobro_por_otro_monto_no_emite_y_es_critico(entorno_venta, cliente):
    entorno_venta.niubiz.monto_cobrado = Decimal("1.00")

    async def escenario():
        sesion = await abrir_sesion(cliente)
        r = await enviar_formulario(cliente, sesion["action"])
        c = await cliente.post("/api/v2/venta/confirmar", json={"booking_id": BOOKING})
        return r, c
    r, c = correr(escenario())
    assert r.headers["location"].endswith("niubiz=denegado")
    assert c.status_code == 402
    assert entorno_venta.jelaf.llamadas["grabar_venta"] == 0
    assert contar("SELECT COUNT(*) FROM incidencia WHERE tipo = 'pago_no_verificado' "
                  "AND severidad = 'critica'") >= 1


def test_niubiz_sin_respuesta_queda_por_conciliar(entorno_venta, cliente):
    entorno_venta.niubiz.sin_respuesta = True

    async def escenario():
        sesion = await abrir_sesion(cliente)
        return await enviar_formulario(cliente, sesion["action"])
    r = correr(escenario())
    assert r.headers["location"].endswith("niubiz=error")
    assert contar("SELECT COUNT(*) FROM incidencia WHERE tipo = 'por_conciliar'") == 1


def test_formulario_sin_token_no_llama_a_niubiz(entorno_venta, cliente):
    async def escenario():
        sesion = await abrir_sesion(cliente)
        return await enviar_formulario(cliente, sesion["action"], token="")
    assert correr(escenario()).headers["location"].endswith("niubiz=error")
    assert entorno_venta.niubiz.autorizaciones == []


# --------------------------------------------------------------------------- #
# Ningún aviso de Niubiz se descarta en silencio (N57-SN, caso #147)
# --------------------------------------------------------------------------- #
def descartes(booking=BOOKING):
    return db.filas_a_dict(db.consultar(
        "SELECT severidad, mensaje, contexto FROM incidencia WHERE booking_id = ? AND tipo = 'niubiz_aviso_descartado'",
        (booking,)))


def test_formulario_sin_token_deja_incidencia_critica_con_los_campos(entorno_venta, cliente):
    async def escenario():
        sesion = await abrir_sesion(cliente)
        r = await cliente.post(sesion["action"], content="channel=web&otroCampo=valor-secreto",
                               headers={"Content-Type": "application/x-www-form-urlencoded"})
        return sesion, r
    sesion, r = correr(escenario())
    assert r.headers["location"].endswith("niubiz=error")
    assert entorno_venta.niubiz.autorizaciones == []
    [inc] = descartes()
    assert inc["severidad"] == "critica" and sesion["pedido"] in inc["mensaje"]
    contexto = json.loads(inc["contexto"])
    assert contexto["motivo"] == "sin_token" and contexto["campos"] == ["channel", "otroCampo"]
    # Solo los NOMBRES de los campos: nunca sus valores.
    assert "valor-secreto" not in inc["mensaje"] + inc["contexto"]


def test_nombres_de_campo_raros_no_meten_saltos_de_linea_en_el_log(entorno_venta, cliente):
    async def escenario():
        sesion = await abrir_sesion(cliente)
        await cliente.post(sesion["action"], content="otro%0A2026-10-18 INFO falso=1",
                           headers={"Content-Type": "application/x-www-form-urlencoded"})
    correr(escenario())
    [inc] = descartes()
    assert all(chr(10) not in c and " " not in c for c in json.loads(inc["contexto"])["campos"])


def test_cuerpo_demasiado_grande_deja_incidencia(entorno_venta, cliente):
    async def escenario():
        sesion = await abrir_sesion(cliente)
        return await enviar_formulario(cliente, sesion["action"], token="t" + "x" * 9000)
    assert correr(escenario()).headers["location"].endswith("niubiz=error")
    [inc] = descartes()
    assert inc["severidad"] == "critica" and json.loads(inc["contexto"])["motivo"] == "cuerpo_grande"


def test_limite_de_avisos_redirige_en_vez_de_mostrar_json(entorno_venta, cliente, monkeypatch):
    from limitador import Limitador
    monkeypatch.setattr(entorno_venta.api, "_limite_niubiz", Limitador(maximo=0, ventana_segundos=60))

    async def escenario():
        sesion = await abrir_sesion(cliente)
        return await enviar_formulario(cliente, sesion["action"])
    r = correr(escenario())
    assert r.status_code == 303 and r.headers["location"].endswith("niubiz=error")
    assert entorno_venta.niubiz.autorizaciones == []
    [inc] = descartes()
    assert json.loads(inc["contexto"])["motivo"] == "limite_peticiones"


def test_pedido_no_vigente_deja_incidencia_grave(entorno_venta, cliente):
    async def escenario():
        vieja = await abrir_sesion(cliente)
        await cliente.post("/api/v2/venta/niubiz/sesion", json={"booking_id": BOOKING})
        r = await enviar_formulario(cliente, vieja["action"])
        return vieja, r
    vieja, r = correr(escenario())
    assert r.headers["location"].endswith("niubiz=vencido")
    [inc] = descartes()
    assert inc["severidad"] == "grave" and vieja["pedido"] in inc["mensaje"]


def test_aviso_con_compra_inventada_no_registra_nada(entorno_venta, cliente):
    async def escenario():
        await abrir_sesion(cliente)
        return await cliente.post("/api/v2/venta/niubiz/autorizar?booking_id=aaaaaaaa-0000&pedido=123456789012",
                                  content="channel=web", headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert correr(escenario()).headers["location"].endswith("niubiz=error")
    assert contar("SELECT COUNT(*) FROM incidencia WHERE tipo = 'niubiz_aviso_descartado'") == 0


def test_aviso_repetido_registra_una_sola_incidencia(entorno_venta, cliente):
    async def escenario():
        sesion = await abrir_sesion(cliente)
        for _ in range(3):
            await enviar_formulario(cliente, sesion["action"], token="")
    correr(escenario())
    assert len(descartes()) == 1


def test_sin_niubiz_configurado_no_se_ofrece_tarjeta(entorno_venta, cliente):
    entorno_venta.niubiz.configurado = False

    async def escenario():
        await preparar_pasajeros(cliente)
        s = await cliente.post("/api/v2/venta/niubiz/sesion", json={"booking_id": BOOKING})
        m = await cliente.post("/api/v2/venta/monto", json={"booking_id": BOOKING})
        return s, m
    s, m = correr(escenario())
    assert s.status_code == 503
    assert m.json()["medios_pago"] == {"yupy": True, "niubiz": False}


def test_tras_pagar_con_tarjeta_no_se_abre_otro_cobro_con_yupy(entorno_venta, cliente):
    async def escenario():
        sesion = await abrir_sesion(cliente)
        await enviar_formulario(cliente, sesion["action"])
        return await cliente.post("/api/v2/venta/checkout", json={"booking_id": BOOKING})
    r = correr(escenario())
    assert r.json()["status"] == "ya_pagado"
    assert entorno_venta.yupy.creadas == []


def test_si_el_navegador_no_vuelve_la_emision_se_rescata(entorno_venta, cliente, monkeypatch):
    monkeypatch.setattr(entorno_venta.api, "RESCATE_NIUBIZ_SEGUNDOS", 0)

    async def escenario():
        sesion = await abrir_sesion(cliente)
        await enviar_formulario(cliente, sesion["action"])
        # El navegador nunca llama a /confirmar: el rescate en segundo plano emite.
        await asyncio.gather(*list(entorno_venta.api._tareas_fondo))
    correr(escenario())
    assert entorno_venta.jelaf.llamadas["grabar_venta"] == 1
    assert consultas.venta_de_booking(BOOKING)["estado"] == "emitida"


def test_checkout_de_yupy_informa_que_sdk_cargar(entorno_venta, cliente):
    async def escenario():
        await preparar_pasajeros(cliente)
        return await cliente.post("/api/v2/venta/checkout", json={"booking_id": BOOKING})
    datos = correr(escenario()).json()
    assert datos["modo"] == "payment_orders" and datos["sdk_url"] == "https://pago.invalid/sdk.js"


# --------------------------------------------------------------------------- #
# Yupy /v1/checkouts (modo del compañero), con requests simulado
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, status, datos):
        self.status_code, self._datos = status, datos

    def json(self):
        return self._datos


def test_yupy_checkouts_crea_el_qr_y_devuelve_el_uid_a_verificar(monkeypatch):
    llamadas = []

    def post(url, json=None, headers=None, timeout=None):
        llamadas.append((url, json))
        if url.endswith("/v1/auth/token"):
            return _Resp(200, {"access_token": "t", "expires_in": 3600})
        # Respuesta documentada de POST /v1/checkouts (https://api.yupy.us/docs, 14.2).
        return _Resp(201, {"checkout_url": "https://pago.invalid/c/1", "payment_order_uid": "po_1",
                           "checkout_uid": "chk_1", "checkout_token": "cks_1", "status": "pending",
                           "amount": "120.00", "currency": "PEN"})

    monkeypatch.setattr(pagos_yupy.requests, "post", post)
    cliente = pagos_yupy.ClienteYupy("https://api.yupy.invalid", "3", "secreto", "checkouts")
    orden = cliente.crear_checkout("TMT-1", Decimal("120"), "Ana Pérez", 900)

    assert orden["yupy_transaction_id"] == "po_1"
    assert orden["checkout_session"]["checkout_url"] == "https://pago.invalid/c/1"
    auth, checkout = llamadas
    assert auth[1] == {"client_id": 3, "client_secret": "secreto", "grant_type": "client_credentials"}
    assert checkout[0] == "https://api.yupy.invalid/v1/checkouts"
    assert checkout[1]["amount"] == "120.00" and checkout[1]["payment_method"] == "qr"
    assert cliente.sdk_url == "https://api.yupy.invalid/v1/checkouts/browser/sdk.js"


def test_yupy_por_defecto_sigue_con_payment_orders(monkeypatch):
    cliente = pagos_yupy.ClienteYupy("https://sandbox.invalid", "cli", "sec")
    assert cliente.api == "payment_orders"
    assert cliente._credenciales() == {"client_id": "cli", "client_secret": "sec"}
    assert cliente.sdk_url == pagos_yupy.SDK_PAYMENT_ORDERS


# --------------------------------------------------------------------------- #
# Registro del pago con tarjeta en JELAF (sin navegador)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("marca, codigo", [("visa", "0201"), ("mastercard", "0202")])
def test_la_venta_se_registra_en_jelaf_como_tarjeta_con_sus_4_ultimos(entorno_venta, cliente, marca, codigo):
    entorno_venta.niubiz.marca = marca

    async def escenario():
        sesion = await abrir_sesion(cliente)
        await enviar_formulario(cliente, sesion["action"])
        return await cliente.post("/api/v2/venta/confirmar", json={"booking_id": BOOKING, "medio_pago": "NIUBIZ"})
    assert correr(escenario()).status_code == 200
    item = entorno_venta.jelaf.ventas_recibidas[-1]["Listado"][0]
    assert item["CodiTarjetaCredito"] == codigo
    assert item["NumeTarjetaCredito"].endswith("8059")
    assert "455170" not in json.dumps(entorno_venta.jelaf.ventas_recibidas[-1])


def test_marca_fuera_del_catalogo_de_jelaf_usa_la_tarjeta_por_defecto(entorno_venta, cliente):
    entorno_venta.niubiz.marca = "amex"

    async def escenario():
        sesion = await abrir_sesion(cliente)
        await enviar_formulario(cliente, sesion["action"])
        return await cliente.post("/api/v2/venta/confirmar", json={"booking_id": BOOKING})
    assert correr(escenario()).status_code == 200
    item = entorno_venta.jelaf.ventas_recibidas[-1]["Listado"][0]
    assert item["CodiTarjetaCredito"] == "0201" and item["NumeTarjetaCredito"].endswith("8059")


def test_antes_de_abrir_la_tarjeta_se_graban_los_pasajeros_en_jelaf(entorno_venta, cliente):
    entorno_venta.jelaf.falla_grabar_pasajero = True

    async def escenario():
        await preparar_pasajeros(cliente)
        return await cliente.post("/api/v2/venta/niubiz/sesion", json={"booking_id": BOOKING})
    r = correr(escenario())
    assert r.status_code == 502 and entorno_venta.niubiz.sesiones == []   # no se abre el cobro


# --------------------------------------------------------------------------- #
# Token de seguridad de Niubiz: de UN solo uso (sonda en el sandbox, 2026-10-10)
# --------------------------------------------------------------------------- #
class _NiubizRedFalsa:
    """Como el sandbox real: cada GET /security da un token nuevo y Niubiz
    rechaza con 400 "Token has been used before" un token ya usado."""

    def __init__(self):
        self.emitidos, self.usados = 0, set()

    def get(self, url, headers=None, timeout=None):
        self.emitidos += 1
        resp = _Resp(201, None)
        resp.text = f"token-{self.emitidos}"
        return resp

    def post(self, url, headers=None, json=None, timeout=None):
        token = headers["Authorization"]
        if token in self.usados:
            return _Resp(400, {"errorCode": 400, "errorMessage": "Token has been used before"})
        self.usados.add(token)
        if "/token/session/" in url:
            return _Resp(200, {"sessionKey": f"sesion-{token}"})
        return _Resp(200, {"dataMap": {"STATUS": "Not Authorized", "ACTION_CODE": "116",
                                       "ACTION_DESCRIPTION": "Fondos insuficientes"}})


def _cliente_con_red(monkeypatch):
    red = _NiubizRedFalsa()
    monkeypatch.setattr(pagos_niubiz.requests, "get", red.get)
    monkeypatch.setattr(pagos_niubiz.requests, "post", red.post)
    return pagos_niubiz.ClienteNiubiz("https://apisandbox.invalid", "123", "u", "p"), red


def test_varias_sesiones_seguidas_no_se_traban(monkeypatch):
    """El bug: el token se guardaba 50 min y la 2.ª sesión (de cualquier cliente) fallaba."""
    niubiz, red = _cliente_con_red(monkeypatch)
    claves = [niubiz.crear_sesion(Decimal("80.00"), "1.2.3.4", f"10000000000{i}") for i in range(3)]
    assert len(set(claves)) == 3 and red.emitidos == 3


def test_tras_un_rechazo_se_puede_volver_a_intentar(monkeypatch):
    niubiz, _ = _cliente_con_red(monkeypatch)
    niubiz.crear_sesion(Decimal("80.00"), "1.2.3.4", "100000000001")
    rechazo = niubiz.autorizar("tx-token", "100000000001", Decimal("80.00"), "https://x.invalid/pago.html")
    assert pagos_niubiz.evaluar_autorizacion(rechazo, "100000000001", Decimal("80.00")) is not None
    assert niubiz.crear_sesion(Decimal("80.00"), "1.2.3.4", "100000000002")


def test_el_motivo_del_rechazo_de_sesion_se_informa(monkeypatch):
    """Si Niubiz rechaza la sesión, el error dice POR QUÉ (antes solo "(400)")."""
    niubiz, red = _cliente_con_red(monkeypatch)
    red.usados.add("token-1")          # Niubiz ya considera usado el próximo token
    with pytest.raises(pagos_niubiz.ErrorNiubiz, match="Token has been used before"):
        niubiz.crear_sesion(Decimal("80.00"), "1.2.3.4", "100000000001")
