"""
config.py — configuración única del backend, leída del entorno (.env).

Por qué existe: los secretos vivían como valores por defecto de os.getenv(...)
repartidos por api_ventas.py, y el repositorio es público (hallazgos S6/N1 de
docs/REVISION_2026-09-26.md). Aquí se leen UNA vez, sin valores por defecto
para nada sensible, y se valida al arrancar:

  - ENTORNO=produccion  → si falta un secreto de seguridad, el backend NO arranca.
  - ENTORNO=desarrollo  → (valor por defecto) arranca, pero avisa en consola de
                          cada protección que queda relajada para poder probar
                          en local.

La credencial de JELAF se exige en ambos entornos. VENTA_MODO decide si el
bot emite de verdad ("real") o solo simula ("simulacion", por defecto): en
producción hay que declararlo explícitamente.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import sys
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("tmt.config")


class ConfiguracionInvalida(RuntimeError):
    """Falta una variable obligatoria para el entorno actual."""


def _texto(nombre: str) -> str:
    return (os.getenv(nombre) or "").strip()


def _bool(nombre: str, defecto: bool = False) -> bool:
    valor = _texto(nombre).lower()
    if not valor:
        return defecto
    return valor in ("1", "true", "si", "sí", "yes")


def _entero(nombre: str, defecto: int) -> int:
    valor = _texto(nombre)
    try:
        return int(valor) if valor else defecto
    except ValueError as e:
        raise ConfiguracionInvalida(f"{nombre} debe ser un número entero (vino {valor!r}).") from e


def _lista(nombre: str, defecto: list[str]) -> list[str]:
    valor = _texto(nombre)
    if not valor:
        return defecto
    return [v.strip() for v in valor.split(",") if v.strip()]


@dataclass(frozen=True)
class Config:
    entorno: str
    host: str
    port: int
    cors_origenes: list[str]

    # JELAF
    jelaf_credenciales: list[tuple[str, str]]   # en orden de uso; la de reserva al final
    jelaf_cuenta_reserva: str | None    # JELAF_USUARIO cuando hay numeradas: se usa última
    venta_modo: str                     # "simulacion" | "real"
    ventas_por_cuenta: int
    jelaf_tarjeta_defecto: str          # medio de pago en JELAF si Yupy no dice cuál

    # Yupy
    yupy_base_url: str
    yupy_client_id: str
    yupy_client_secret: str
    yupy_checkout_segundos: int

    # Meta / WhatsApp
    whatsapp_token: str
    phone_number_id: str
    meta_verify_token: str
    meta_app_secret: str
    url_frontend: str

    # Enlace firmado y paneles
    enlace_secreto: bytes
    exigir_contacto: bool
    paneles_token: str

    # Límites operativos
    duracion_maxima_seleccion_segundos: int

    # Yupy: qué API de checkout usar. "payment_orders" es la de siempre;
    # "checkouts" es la que integró el compañero (/v1/checkouts, QR Yape/Plin).
    yupy_api: str = "payment_orders"

    # Niubiz (pago con tarjeta). Opcional: sin estas variables la opción de
    # tarjeta no se ofrece. Sin valores por defecto para credenciales (S6).
    niubiz_base_url: str = ""
    # Yupy: permitir VENTA REAL con una credencial que muestra "Simular pago"
    # (payments:simulate). Por defecto NO: un pago simulado es idéntico a uno real.
    yupy_permitir_simulacion: bool = False
    # Signing Secret (whsec_...) del webhook de Yupy, generado en YUPY Admin →
    # Webhooks. Sin él, /api/v2/yupy/webhook responde 503 (falla cerrado).
    yupy_webhook_secret: str = ""
    niubiz_merchant_id: str = ""
    niubiz_usuario: str = ""
    niubiz_password: str = ""
    niubiz_checkout_js: str = ""
    niubiz_sesion_segundos: int = 900

    # Términos y condiciones que el cliente acepta por WhatsApp antes de comprar.
    url_terminos: str = ""

    # Lista blanca de la etapa de prueba: si tiene números, el bot solo atiende
    # esos (los demás se ignoran sin responder). Vacía = atiende a todos. Vive
    # en el .env y no en el código: son teléfonos de personas y el repo puede ser público.
    whatsapp_numeros_permitidos: tuple[str, ...] = ()
    # Una respuesta por número y por día (2026-10-15). WHATSAPP_UNA_VEZ_AL_DIA=0
    # responde a cada mensaje: solo para probar en desarrollo.
    whatsapp_una_vez_al_dia: bool = True

    # Correo con los boletos (2026-10-14: el navegador de WhatsApp no deja
    # descargar). Remitente editable: la empresa pasará el correo oficial.
    smtp_host: str = ""
    smtp_puerto: int = 465
    smtp_usuario: str = ""
    smtp_password: str = ""
    correo_remitente: str = ""
    correo_nombre_remitente: str = "Transportes Moquegua Turismo"
    # El correo del comprador es obligatorio para pagar (defecto: sí en producción).
    exigir_correo: bool = False

    avisos: list[str] = field(default_factory=list)

    @property
    def es_produccion(self) -> bool:
        return self.entorno == "produccion"

    @property
    def simulacion(self) -> bool:
        return self.venta_modo != "real"


MAX_CUENTAS_JELAF = 50


def _credenciales_jelaf() -> tuple[list[tuple[str, str]], str | None]:
    """
    Cuentas de JELAF para vender (vendedor_http.PoolCuentas), en orden de uso:
    JELAF_USUARIO_1/JELAF_PASSWORD_1, _2, … y al final, como reserva, la
    JELAF_USUARIO/JELAF_PASSWORD de siempre (pedido del 2026-10-09: que sea la
    última en usarse). Devuelve (cuentas, usuario_de_reserva o None).
    """
    cuentas: list[tuple[str, str]] = []
    for n in range(1, MAX_CUENTAS_JELAF + 1):
        usuario, password = _texto(f"JELAF_USUARIO_{n}"), _texto(f"JELAF_PASSWORD_{n}")
        if usuario and not password:
            raise ConfiguracionInvalida(f"Falta JELAF_PASSWORD_{n} para JELAF_USUARIO_{n}.")
        if password and not usuario:
            raise ConfiguracionInvalida(f"Falta JELAF_USUARIO_{n} para JELAF_PASSWORD_{n}.")
        if usuario and usuario not in (u for u, _ in cuentas):
            cuentas.append((usuario, password))

    reserva = None
    usuario, password = _texto("JELAF_USUARIO"), _texto("JELAF_PASSWORD")
    if bool(usuario) != bool(password):
        raise ConfiguracionInvalida("JELAF_USUARIO y JELAF_PASSWORD van juntos.")
    if usuario and usuario not in (u for u, _ in cuentas):
        if cuentas:
            reserva = usuario
        cuentas.append((usuario, password))

    if not cuentas:
        raise ConfiguracionInvalida("Faltan las cuentas de JELAF (JELAF_USUARIO_1/JELAF_PASSWORD_1… "
                                    "o JELAF_USUARIO/JELAF_PASSWORD) en el entorno.")
    return cuentas, reserva


def cargar(exigir_jelaf: bool = True) -> Config:
    """
    Lee y valida el entorno. `exigir_jelaf=False` solo lo usan los tests y el
    servidor de paneles, que no hablan con JELAF.
    """
    entorno = (_texto("ENTORNO") or "desarrollo").lower()
    if entorno not in ("desarrollo", "produccion"):
        raise ConfiguracionInvalida("ENTORNO debe ser 'desarrollo' o 'produccion'.")
    prod = entorno == "produccion"
    faltan: list[str] = []
    avisos: list[str] = []

    def secreto(nombre: str, que_protege: str) -> str:
        valor = _texto(nombre)
        if not valor:
            if prod:
                faltan.append(nombre)
            else:
                avisos.append(f"{nombre} no está definido: {que_protege}.")
        return valor

    credenciales: list[tuple[str, str]] = []
    reserva: str | None = None
    if exigir_jelaf:
        credenciales, reserva = _credenciales_jelaf()
    if _texto("MAX_VENTAS_SIMULTANEAS"):
        avisos.append("MAX_VENTAS_SIMULTANEAS ya no se usa: ahora son VENTAS_POR_CUENTA ventas a la vez "
                      "por cada cuenta de JELAF (defecto 1).")

    modo = _texto("VENTA_MODO").lower()
    if not modo:
        if prod:
            faltan.append("VENTA_MODO")
        modo = "simulacion"
    if modo not in ("simulacion", "real"):
        raise ConfiguracionInvalida("VENTA_MODO debe ser 'simulacion' o 'real'.")
    if modo == "simulacion":
        avisos.append("VENTA_MODO=simulacion: se consulta JELAF pero NO se bloquean asientos ni se emiten boletos.")

    tarjeta = (_texto("JELAF_TARJETA_DEFECTO") or "visa").lower()
    if tarjeta not in ("visa", "mastercard", "deposito", "yape", "plin", "bbva"):
        raise ConfiguracionInvalida("JELAF_TARJETA_DEFECTO: visa, mastercard, deposito, yape, plin o bbva.")

    yupy_id = secreto("YUPY_CLIENT_ID", "no se podrá cobrar con Yupy")
    yupy_secret = secreto("YUPY_CLIENT_SECRET", "no se podrá cobrar con Yupy")
    verify = secreto("META_VERIFY_TOKEN", "la verificación del webhook de Meta responderá 403")
    app_secret = secreto(
        "META_APP_SECRET",
        "el webhook acepta mensajes SIN verificar la firma de Meta (solo aceptable en local)",
    )
    whatsapp_token = secreto("WHATSAPP_TOKEN", "no se podrá enviar el botón de compra")
    phone_id = secreto("PHONE_NUMBER_ID", "no se podrá enviar el botón de compra")
    paneles_token = secreto("PANELES_TOKEN", "el tablero /paneles/ queda ABIERTO sin clave")
    # No impiden arrancar (no protegen la venta): sin ellas el panel solo no deja cambiar nada.
    for nombre, que in (("PANELES_CLAVE_CIUDADES", "ciudades"), ("PANELES_CLAVE_PASARELAS", "pasarelas de pago")):
        if not _texto(nombre):
            avisos.append(f"{nombre} no está definido: el panel de Administración no dejará activar ni desactivar {que}.")
    yupy_webhook_secret = _texto("YUPY_WEBHOOK_SECRET")
    if yupy_id and not yupy_webhook_secret:
        avisos.append("YUPY_WEBHOOK_SECRET no está definido: el webhook de Yupy responderá 503 (YUPY Admin → Webhooks).")

    enlace = _texto("ENLACE_SECRETO")
    if not enlace:
        if prod:
            faltan.append("ENLACE_SECRETO")
        else:
            # Secreto aleatorio por proceso: los enlaces de compra vencen al
            # reiniciar, pero nunca se firma con un valor conocido públicamente.
            enlace = secrets.token_urlsafe(48)
            avisos.append("ENLACE_SECRETO no está definido: se usa uno aleatorio (los enlaces vencen al reiniciar).")

    url_frontend = _texto("URL_FRONTEND")
    if not url_frontend:
        if prod:
            faltan.append("URL_FRONTEND")
        url_frontend = "http://localhost:5500/buscar-viaje.html"

    yupy_base = _texto("YUPY_BASE_URL") or "https://sandbox-api.yupy.us"
    if prod and "sandbox" in yupy_base:
        avisos.append("ENTORNO=produccion pero YUPY_BASE_URL apunta a sandbox.")

    yupy_api = (_texto("YUPY_API") or "payment_orders").lower()
    if yupy_api not in ("payment_orders", "checkouts"):
        raise ConfiguracionInvalida("YUPY_API debe ser 'payment_orders' o 'checkouts'.")

    # Niubiz es opcional; si se activa (NIUBIZ_MERCHANT_ID), van las tres cosas juntas.
    niubiz_merchant = _texto("NIUBIZ_MERCHANT_ID")
    niubiz_usuario = _texto("NIUBIZ_USUARIO")
    niubiz_password = _texto("NIUBIZ_PASSWORD")
    niubiz_base = (_texto("NIUBIZ_BASE_URL") or "https://apisandbox.vnforappstest.com").rstrip("/")
    niubiz_js = (_texto("NIUBIZ_CHECKOUT_JS")
                 or "https://static-content-qas.vnforapps.com/env/sandbox/js/checkout.js")
    if niubiz_merchant and not (niubiz_usuario and niubiz_password):
        if prod:
            faltan.extend(["NIUBIZ_USUARIO", "NIUBIZ_PASSWORD"])
        else:
            avisos.append("NIUBIZ_MERCHANT_ID sin NIUBIZ_USUARIO/NIUBIZ_PASSWORD: no se ofrecerá pago con tarjeta.")
    if prod and niubiz_merchant and ("sandbox" in niubiz_base or "qas" in niubiz_js):
        avisos.append("ENTORNO=produccion pero Niubiz apunta a sandbox.")
    # En simulación no se emite en JELAF, pero las pasarelas SÍ cobran: con
    # credenciales de producción el cliente pagaría un boleto que no existe.
    if modo == "simulacion":
        if "sandbox" not in yupy_base:
            avisos.append("VENTA_MODO=simulacion con Yupy de PRODUCCIÓN: se cobraría sin emitir el boleto.")
        if niubiz_merchant and "sandbox" not in niubiz_base:
            avisos.append("VENTA_MODO=simulacion con Niubiz de PRODUCCIÓN: se cobraría sin emitir el boleto.")

    if faltan:
        raise ConfiguracionInvalida(
            "ENTORNO=produccion y faltan variables obligatorias: " + ", ".join(sorted(set(faltan)))
        )

    # Solo dígitos: "+51 900 000 001" y "51900000001" son el mismo número (como llega de Meta).
    permitidos = tuple(n for n in (re.sub(r"\D", "", v) for v in _lista("WHATSAPP_NUMEROS_PERMITIDOS", [])) if n)
    if permitidos:
        avisos.append(f"WHATSAPP_NUMEROS_PERMITIDOS: el bot solo atiende a {len(permitidos)} número(s).")
    una_vez_al_dia = _bool("WHATSAPP_UNA_VEZ_AL_DIA", True)
    if not una_vez_al_dia:
        avisos.append("WHATSAPP_UNA_VEZ_AL_DIA=0: el bot responde a CADA mensaje de WhatsApp (solo para desarrollo).")

    smtp_host = _texto("SMTP_HOST")
    smtp_usuario = _texto("SMTP_USUARIO")
    correo_remitente = _texto("CORREO_REMITENTE") or smtp_usuario
    if not (smtp_host and smtp_usuario and _texto("SMTP_PASSWORD") and correo_remitente):
        avisos.append("SMTP_HOST/SMTP_USUARIO/SMTP_PASSWORD sin definir: los boletos NO se enviarán por correo.")

    cors_defecto = [] if prod else ["http://localhost:5500", "http://127.0.0.1:5500"]
    return Config(
        entorno=entorno,
        host=_texto("HOST") or "127.0.0.1",
        port=_entero("PORT", 8000),
        cors_origenes=_lista("CORS_ORIGENES", cors_defecto),
        jelaf_credenciales=credenciales,
        jelaf_cuenta_reserva=reserva,
        venta_modo=modo,
        ventas_por_cuenta=max(1, _entero("VENTAS_POR_CUENTA", 1)),
        jelaf_tarjeta_defecto=tarjeta,
        yupy_base_url=yupy_base.rstrip("/"),
        yupy_client_id=yupy_id,
        yupy_client_secret=yupy_secret,
        yupy_checkout_segundos=_entero("YUPY_CHECKOUT_SEGUNDOS", 900),
        whatsapp_token=whatsapp_token,
        phone_number_id=phone_id,
        meta_verify_token=verify,
        meta_app_secret=app_secret,
        url_frontend=url_frontend,
        enlace_secreto=enlace.encode(),
        # S3: en producción solo inicia una venta quien llegó por el enlace de WhatsApp.
        exigir_contacto=_bool("EXIGIR_CONTACTO", prod),
        paneles_token=paneles_token,
        duracion_maxima_seleccion_segundos=_entero("DURACION_MAXIMA_SELECCION_SEGUNDOS", 20 * 60),
        yupy_api=yupy_api,
        niubiz_base_url=niubiz_base,
        niubiz_merchant_id=niubiz_merchant,
        niubiz_usuario=niubiz_usuario,
        niubiz_password=niubiz_password,
        niubiz_checkout_js=niubiz_js,
        niubiz_sesion_segundos=_entero("NIUBIZ_SESION_SEGUNDOS", 900),
        # /terminos daba 404 (comprobado 2026-10-12); la página completa es terminos-pasajes.html.
        url_terminos=_texto("URL_TERMINOS") or "https://transportesmoquegua.com/terminos-pasajes.html",
        yupy_permitir_simulacion=_bool("YUPY_PERMITIR_SIMULACION", False),
        yupy_webhook_secret=yupy_webhook_secret,
        whatsapp_numeros_permitidos=permitidos,
        whatsapp_una_vez_al_dia=una_vez_al_dia,
        smtp_host=smtp_host,
        smtp_puerto=_entero("SMTP_PUERTO", 465),
        smtp_usuario=smtp_usuario,
        smtp_password=_texto("SMTP_PASSWORD"),
        correo_remitente=correo_remitente,
        correo_nombre_remitente=_texto("CORREO_NOMBRE_REMITENTE") or "Transportes Moquegua Turismo",
        exigir_correo=_bool("EXIGIR_CORREO", prod),
        avisos=avisos,
    )


def configurar_salida_utf8() -> None:
    """
    N10: con la salida redirigida (archivo, servicio de Windows) la consola usa
    cp1252 y cualquier print con emoji lanza UnicodeEncodeError — dentro del
    webhook eso terminaba en un 500 a Meta. Se fuerza UTF-8 sin romper nunca.
    """
    for flujo in (sys.stdout, sys.stderr):
        try:
            flujo.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
