"""Enlace firmado, webhook de Meta (S7) y evaluación de pagos de Yupy (V1)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from decimal import Decimal

import whatsapp
from pagos_yupy import evaluar_orden

SECRETO = b"secreto-de-prueba"


# --- Enlace firmado ---------------------------------------------------------- #
def test_token_valido_devuelve_el_telefono():
    token = whatsapp.firmar_contacto("51987654321", SECRETO, ahora=1_000_000)
    assert whatsapp.verificar_contacto(token, SECRETO, ahora=1_000_100) == "51987654321"


def test_token_alterado_o_con_otro_secreto_no_vale():
    token = whatsapp.firmar_contacto("51987654321", SECRETO, ahora=1_000_000)
    assert whatsapp.verificar_contacto(token, b"otro", ahora=1_000_100) is None
    crudo = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).replace(b"51987654321", b"51900000000")
    falso = base64.urlsafe_b64encode(crudo).decode().rstrip("=")
    assert whatsapp.verificar_contacto(falso, SECRETO, ahora=1_000_100) is None


def test_token_vencido_no_vale():
    token = whatsapp.firmar_contacto("51987654321", SECRETO, ahora=0)
    assert whatsapp.verificar_contacto(token, SECRETO, ahora=whatsapp.CONTACTO_VIGENCIA_SEGUNDOS + 1) is None


def test_firma_con_un_punto_dentro_sigue_siendo_valida():
    """Regresión N13: rsplit(b'.') rompía los tokens cuya firma contenía 0x2E."""
    for momento in range(1_000_000, 1_010_000):
        payload = f"51987654321.{momento}".encode()
        firma = hmac.new(SECRETO, payload, hashlib.sha256).digest()[:whatsapp.LARGO_FIRMA]
        if b"." in firma:
            break
    else:
        raise AssertionError("no se encontró un caso con 0x2E en la firma")
    token = whatsapp.firmar_contacto("51987654321", SECRETO, ahora=momento)
    assert whatsapp.verificar_contacto(token, SECRETO, ahora=momento + 1) == "51987654321"


def test_tokens_viejos_de_12_bytes_siguen_valiendo():
    payload = b"51987654321.1000000"
    firma = hmac.new(SECRETO, payload, hashlib.sha256).digest()[:12]
    viejo = base64.urlsafe_b64encode(payload + b"." + firma).decode().rstrip("=")
    assert whatsapp.verificar_contacto(viejo, SECRETO, ahora=1_000_001) == "51987654321"


def test_celular_nacional():
    assert whatsapp.celular_nacional("51987654321") == "987654321"
    assert whatsapp.celular_nacional("+51 987 654 321") == "987654321"
    assert whatsapp.celular_nacional("12345") == ""


# --- Webhook ----------------------------------------------------------------- #
def _cuerpo(mensaje_id="wamid.1", texto="Quiero COMPRAR"):
    return json.dumps({"entry": [{"changes": [{"value": {
        "contacts": [{"profile": {"name": "Ana"}}],
        "messages": [{"id": mensaje_id, "from": "51987654321", "type": "text", "text": {"body": texto}}],
    }}]}]}).encode()


def _firma(cuerpo: bytes, secreto="secreto-app-prueba") -> str:
    return "sha256=" + hmac.new(secreto.encode(), cuerpo, hashlib.sha256).hexdigest()


def test_webhook_sin_firma_se_rechaza_y_no_envia_nada(entorno_venta, cliente):
    async def escenario():
        return await cliente.post("/api/v1/webhook", content=_cuerpo(),
                                  headers={"Content-Type": "application/json"})
    assert asyncio.run(escenario()).status_code == 401
    assert entorno_venta.enviados == []


def test_webhook_con_firma_de_otro_secreto_se_rechaza(entorno_venta, cliente):
    cuerpo = _cuerpo()

    async def escenario():
        return await cliente.post("/api/v1/webhook", content=cuerpo,
                                  headers={"X-Hub-Signature-256": _firma(cuerpo, "otro")})
    assert asyncio.run(escenario()).status_code == 401
    assert entorno_venta.enviados == []


def _boton(mensaje_id="wamid.btn", boton="ACEPTAR_TYC"):
    """Pulsación de un botón (p. ej. "Aceptar" de un mensaje de T&C viejo)."""
    return json.dumps({"entry": [{"changes": [{"value": {
        "messages": [{"id": mensaje_id, "from": "51987654321", "type": "interactive",
                      "interactive": {"type": "button_reply", "button_reply": {"id": boton, "title": "Aceptar"}}}],
    }}]}]}).encode()


def _mensaje(mensaje_id, tipo="text", numero="51987654321", **extra):
    return json.dumps({"entry": [{"changes": [{"value": {
        "messages": [{"id": mensaje_id, "from": numero, "type": tipo, **extra}],
    }}]}]}).encode()


def _publicar(cliente, *cuerpos):
    async def escenario():
        return [await cliente.post("/api/v1/webhook", content=c, headers={"X-Hub-Signature-256": _firma(c)})
                for c in cuerpos]
    return asyncio.run(escenario())


def test_webhook_firmado_envia_el_boton_una_sola_vez_aunque_meta_reintente(entorno_venta, cliente):
    cuerpo = _cuerpo()

    async def escenario():
        cab = {"X-Hub-Signature-256": _firma(cuerpo)}
        a = await cliente.post("/api/v1/webhook", content=cuerpo, headers=cab)
        b = await cliente.post("/api/v1/webhook", content=cuerpo, headers=cab)
        return a, b
    a, b = asyncio.run(escenario())
    assert a.status_code == b.status_code == 200
    assert entorno_venta.enviados == ["51987654321"]


def test_responde_desde_el_numero_que_recibio_el_mensaje(entorno_venta, cliente, monkeypatch):
    # 2026-10-16: con dos números en la cuenta de Meta, al cliente que escribió al
    # número oficial le respondía el otro (el fijo de PHONE_NUMBER_ID).
    api = entorno_venta.api
    desde = []
    monkeypatch.setattr(api.whatsapp, "enviar_boton_compra",
                        lambda numero, *, phone_id, **k: desde.append(phone_id) or (True, None))
    monkeypatch.setattr(api, "enviar_boton_compra", entorno_venta.enviar_boton_compra_real)
    al_oficial = json.dumps({"entry": [{"changes": [{"value": {
        "metadata": {"display_phone_number": "51953954708", "phone_number_id": "1112223334445"},
        "messages": [{"id": "wamid.of", "from": "51987654321", "type": "text", "text": {"body": "hola"}}],
    }}]}]}).encode()
    _publicar(cliente, al_oficial)
    # Sin metadata (aviso viejo o incompleto): el de PHONE_NUMBER_ID, como antes.
    _publicar(cliente, _mensaje("wamid.sin", numero="51900000002", text={"body": "hola"}))
    assert desde == ["1112223334445", api.CONFIG.phone_number_id]


def test_cualquier_primer_mensaje_recibe_el_mensaje_unico(entorno_venta, cliente):
    # Ya no hay paso de Términos y Condiciones: "hola" recibe directo el botón de compra.
    _publicar(cliente, _cuerpo(texto="hola"))
    assert entorno_venta.enviados == ["51987654321"]


def test_responde_una_sola_vez_al_dia(entorno_venta, cliente):
    _publicar(cliente, _cuerpo("wamid.1", "hola"), _cuerpo("wamid.2", "quiero comprar"),
              _boton("wamid.3"), _mensaje("wamid.4", "image", image={"id": "x"}))
    assert entorno_venta.enviados == ["51987654321"]
    # Los demás mensajes igual quedan registrados para los paneles.
    from paneles import db
    assert db.consultar_uno("SELECT mensajes FROM contacto WHERE telefono = '51987654321'")["mensajes"] == 4


def test_sin_limite_diario_responde_a_cada_mensaje(entorno_venta, cliente, monkeypatch):
    import dataclasses
    api = entorno_venta.api
    monkeypatch.setattr(api, "CONFIG", dataclasses.replace(api.CONFIG, whatsapp_una_vez_al_dia=False))
    _publicar(cliente, _cuerpo("wamid.1"), _cuerpo("wamid.2"), _cuerpo("wamid.2"))   # el 3.º es un reintento de Meta
    assert entorno_venta.enviados == ["51987654321", "51987654321"]


def test_limite_diario_se_lee_del_entorno(monkeypatch):
    import config
    monkeypatch.delenv("WHATSAPP_UNA_VEZ_AL_DIA", raising=False)
    assert config.cargar(exigir_jelaf=False).whatsapp_una_vez_al_dia is True     # por defecto, activo
    monkeypatch.setenv("WHATSAPP_UNA_VEZ_AL_DIA", "0")
    c = config.cargar(exigir_jelaf=False)
    assert c.whatsapp_una_vez_al_dia is False and any("WHATSAPP_UNA_VEZ_AL_DIA" in a for a in c.avisos)


def test_al_dia_siguiente_vuelve_a_responder(entorno_venta, cliente, monkeypatch):
    from datetime import timedelta

    from paneles import tiempo
    _publicar(cliente, _cuerpo("wamid.1"))
    manana = tiempo.hoy() + timedelta(days=1)
    monkeypatch.setattr(tiempo, "hoy", lambda: manana)
    _publicar(cliente, _cuerpo("wamid.2"), _cuerpo("wamid.3"))
    assert entorno_venta.enviados == ["51987654321", "51987654321"]


def test_cada_numero_tiene_su_respuesta_del_dia(entorno_venta, cliente):
    _publicar(cliente, _mensaje("wamid.a", text={"body": "hola"}),
              _mensaje("wamid.b", numero="51900000002", text={"body": "hola"}))
    assert entorno_venta.enviados == ["51987654321", "51900000002"]


def test_reaccion_o_mensaje_no_soportado_no_responde_ni_gasta_la_respuesta(entorno_venta, cliente):
    _publicar(cliente, _mensaje("wamid.r", "reaction", reaction={"emoji": "👍"}), _mensaje("wamid.u", "unsupported"))
    assert entorno_venta.enviados == []
    _publicar(cliente, _cuerpo("wamid.t", "hola"))
    assert entorno_venta.enviados == ["51987654321"]


def test_si_meta_falla_el_proximo_mensaje_vuelve_a_intentar(entorno_venta, cliente, monkeypatch):
    from paneles import consultas, registro
    api = entorno_venta.api
    intentos = []
    monkeypatch.setattr(api.whatsapp, "enviar_boton_compra",
                        lambda numero, **k: intentos.append(numero) or (False, "Meta rechazó el envío (500)"))
    monkeypatch.setattr(api, "enviar_boton_compra", entorno_venta.enviar_boton_compra_real)
    _publicar(cliente, _cuerpo("wamid.1"))
    _publicar(cliente, _cuerpo("wamid.2"))
    assert intentos == ["51987654321", "51987654321"]   # el fallo no gastó la respuesta del día
    assert any(i["tipo"] == "error_interno" for i in consultas.incidencias_abiertas(10))
    # Ya salió bien: el tercero del día no recibe otra.
    monkeypatch.setattr(api.whatsapp, "enviar_boton_compra", lambda numero, **k: intentos.append(numero) or (True, None))
    _publicar(cliente, _cuerpo("wamid.3"), _cuerpo("wamid.4"))
    assert intentos == ["51987654321"] * 3
    assert registro.reservar_respuesta_del_dia("51987654321") is False


def test_si_la_base_falla_igual_responde(entorno_venta, cliente, monkeypatch):
    from paneles import db

    def rota(*a, **k):
        raise RuntimeError("disco lleno")
    monkeypatch.setattr(db, "escribir_contando", rota)
    _publicar(cliente, _cuerpo("wamid.1"))
    assert entorno_venta.enviados == ["51987654321"]


def test_mensaje_de_ventas_sin_encabezado_ni_terminos(monkeypatch):
    enviados = []
    monkeypatch.setattr(whatsapp, "_enviar", lambda numero, payload, **k: enviados.append(payload) or (True, None))
    whatsapp.enviar_boton_compra("51987654321", token="t", phone_id="p",
                                 url_frontend="https://x.invalid/buscar-viaje.html", secreto_enlace=b"s" * 32)
    interactivo = enviados[0]["interactive"]
    assert interactivo["type"] == "cta_url" and "header" not in interactivo
    texto = interactivo["body"]["text"]
    assert "COMPRAR PASAJE" in texto and "ASESOR HUMANO" in texto
    assert "Términos" not in texto and len(texto) <= 1024          # tope de Meta para el cuerpo
    assert not hasattr(whatsapp, "enviar_bienvenida_tyc")


def test_lista_blanca_solo_responde_a_los_numeros_permitidos(entorno_venta, cliente, monkeypatch):
    import dataclasses

    api = entorno_venta.api
    monkeypatch.setattr(api, "CONFIG", dataclasses.replace(api.CONFIG, whatsapp_numeros_permitidos=("51900000001",)))
    _publicar(cliente, _cuerpo(mensaje_id="wamid.fuera"))
    assert entorno_venta.enviados == []

    monkeypatch.setattr(api, "CONFIG", dataclasses.replace(api.CONFIG, whatsapp_numeros_permitidos=("51987654321",)))
    _publicar(cliente, _cuerpo(mensaje_id="wamid.dentro"))
    assert entorno_venta.enviados == ["51987654321"]


def test_lista_blanca_se_lee_del_entorno_sin_simbolos(monkeypatch):
    import config
    monkeypatch.setenv("WHATSAPP_NUMEROS_PERMITIDOS", " 51900000001, +51 900 000 002 ,")
    c = config.cargar(exigir_jelaf=False)
    assert c.whatsapp_numeros_permitidos == ("51900000001", "51900000002")
    monkeypatch.setenv("WHATSAPP_NUMEROS_PERMITIDOS", "")
    assert config.cargar(exigir_jelaf=False).whatsapp_numeros_permitidos == ()


def test_verificacion_de_suscripcion(entorno_venta, cliente):
    async def escenario():
        ok = await cliente.get("/api/v1/webhook", params={
            "hub.mode": "subscribe", "hub.verify_token": "verifica-prueba", "hub.challenge": "123"})
        mal = await cliente.get("/api/v1/webhook", params={
            "hub.mode": "subscribe", "hub.verify_token": "MoqueguaBot2026", "hub.challenge": "123"})
        return ok, mal
    ok, mal = asyncio.run(escenario())
    assert ok.status_code == 200 and ok.text == "123"
    assert mal.status_code == 403


# --- Yupy -------------------------------------------------------------------- #
def test_pago_aprobado_por_el_monto_exacto():
    assert evaluar_orden({"status": "reconciled", "amount": "240.00", "currency": "PEN"}, Decimal("240")) is None


def test_pago_por_otro_monto_se_rechaza():
    assert "monto" in evaluar_orden({"status": "PAID", "amount": "1.00"}, Decimal("240.00"))


def test_pago_pendiente_se_rechaza():
    assert "no está aprobado" in evaluar_orden({"status": "PENDING", "amount": "240.00"}, Decimal("240.00"))


def test_pago_en_otra_moneda_se_rechaza():
    assert "moneda" in evaluar_orden({"status": "PAID", "amount": "240.00", "currency": "USD"}, Decimal("240.00"))


def test_estado_pagado_del_checkout_qr_se_acepta_con_monto_exacto():
    """El SDK de /v1/checkouts usa 'pagado' (sdk.js 1.0.0): se acepta, pero solo con monto y moneda exactos."""
    assert evaluar_orden({"status": "pagado", "amount": "140.00", "currency": "PEN"}, Decimal("140")) is None
    assert "monto" in evaluar_orden({"status": "pagado", "amount": "1.00", "currency": "PEN"}, Decimal("140"))


def test_el_boton_de_compra_lleva_a_url_frontend(monkeypatch):
    """El enlace sale de URL_FRONTEND (.env). Una URL fija a /beta/.../1-2-corregir.html
    mandaba a una página que la carpeta frontend/ ya no tiene (2026-10-10)."""
    enviados = []
    monkeypatch.setattr(whatsapp, "_enviar", lambda numero, payload, **k: enviados.append(payload) or (True, None))
    ok, _ = whatsapp.enviar_boton_compra("51987654321", token="t", phone_id="p",
                                         url_frontend="https://transportesmoquegua.com/app/buscar-viaje.html",
                                         secreto_enlace=b"s" * 32)
    url = enviados[0]["interactive"]["action"]["parameters"]["url"]
    assert ok and url.startswith("https://transportesmoquegua.com/app/buscar-viaje.html?c=")
