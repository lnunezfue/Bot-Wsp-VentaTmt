"""
whatsapp.py — todo lo que toca Meta (WhatsApp Cloud API) y el enlace firmado.

- `firmar_contacto` / `verificar_contacto`: el teléfono viaja firmado (HMAC) en
  el enlace de compra, con vencimiento. Nunca en claro como única identidad.
- `firma_webhook_valida`: X-Hub-Signature-256 sobre el cuerpo CRUDO (S7).
- `extraer_mensajes`: saca de la notificación de Meta solo lo que usamos.
- `enviar_boton_compra`: el ÚNICO mensaje del bot (bienvenida, botón de compra
  y asesor humano; desde 2026-10-15 sin el paso de Términos y Condiciones, que
  se aceptan en la mini-webapp antes de pagar). **Envía un mensaje real**:
  nunca llamarlo en pruebas (los tests lo reemplazan por un doble).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import requests

import privacidad

log = logging.getLogger("tmt.whatsapp")

CONTACTO_VIGENCIA_SEGUNDOS = 60 * 60 * 24 * 7   # el enlace de compra sirve una semana
GRAPH_URL = "https://graph.facebook.com/v19.0"
TIMEOUT = 10
MAX_TEXTO = 500


# --------------------------------------------------------------------------- #
# Enlace firmado
# --------------------------------------------------------------------------- #
LARGO_FIRMA = 16
# Tokens emitidos antes de este cambio usaban 12 bytes de firma: se siguen
# aceptando para no invalidar enlaces ya enviados por WhatsApp.
LARGOS_FIRMA_ACEPTADOS = (16, 12)


def firmar_contacto(telefono: str, secreto: bytes, ahora: float | None = None) -> str:
    payload = f"{telefono}.{int(ahora if ahora is not None else time.time())}".encode()
    firma = hmac.new(secreto, payload, hashlib.sha256).digest()[:LARGO_FIRMA]
    return base64.urlsafe_b64encode(payload + b"." + firma).decode().rstrip("=")


def _separar(crudo: bytes, secreto: bytes) -> bytes | None:
    """
    Devuelve el payload si alguna longitud de firma aceptada verifica.
    La firma son bytes arbitrarios y puede contener b"." (0x2E): separar con
    rsplit(b".") rompía ~1 de cada 20 tokens. Se corta por longitud fija.
    """
    for largo in LARGOS_FIRMA_ACEPTADOS:
        if len(crudo) <= largo + 1 or crudo[-largo - 1:-largo] != b".":
            continue
        payload, firma = crudo[:-largo - 1], crudo[-largo:]
        esperada = hmac.new(secreto, payload, hashlib.sha256).digest()[:largo]
        if hmac.compare_digest(firma, esperada):
            return payload
    return None


def verificar_contacto(token: str | None, secreto: bytes, ahora: float | None = None) -> str | None:
    """Teléfono si el token es auténtico y no venció; si no, None. Nunca lanza."""
    if not token or len(token) > 200:
        return None
    try:
        crudo = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        payload = _separar(crudo, secreto)
        if payload is None:
            return None
        telefono, emitido = payload.decode().rsplit(".", 1)
        momento = ahora if ahora is not None else time.time()
        if not (0 <= momento - int(emitido) <= CONTACTO_VIGENCIA_SEGUNDOS):
            return None
        return telefono if re.fullmatch(r"\d{8,15}", telefono) else None
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Enlace firmado del PDF del boleto
# --------------------------------------------------------------------------- #
# El navegador de WhatsApp no descarga archivos: le pasa la URL a otra app
# (Chrome, el gestor de descargas). Un blob: no existe fuera de la página, así
# que el PDF necesita una URL real. Va firmada, para un solo boleto, y vence
# pronto: se pide justo al tocar "PDF". Lleva el id interno de la venta, no el
# booking_id: base64 no oculta nada, y el booking_id es la llave de la compra
# (quedaría legible en el historial de Chrome).
PDF_VIGENCIA_SEGUNDOS = 60 * 60


def _clave_pdf(secreto: bytes) -> bytes:
    # Clave derivada: un token de compra nunca verifica como token de PDF ni al revés.
    return hmac.new(secreto, b"tmt-pdf-boleto", hashlib.sha256).digest()


def firmar_pdf(venta_id: int, asiento: str, secreto: bytes, ahora: float | None = None) -> str:
    payload = f"{int(venta_id)}.{asiento}.{int(ahora if ahora is not None else time.time())}".encode()
    firma = hmac.new(_clave_pdf(secreto), payload, hashlib.sha256).digest()[:LARGO_FIRMA]
    return base64.urlsafe_b64encode(payload + b"." + firma).decode().rstrip("=")


def verificar_pdf(token: str | None, secreto: bytes, ahora: float | None = None) -> tuple[int, str] | None:
    """(id de la venta, asiento) si el token es auténtico y no venció; si no, None. Nunca lanza."""
    if not token or len(token) > 200:
        return None
    try:
        crudo = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        if len(crudo) <= LARGO_FIRMA + 1 or crudo[-LARGO_FIRMA - 1:-LARGO_FIRMA] != b".":
            return None
        payload, firma = crudo[:-LARGO_FIRMA - 1], crudo[-LARGO_FIRMA:]
        esperada = hmac.new(_clave_pdf(secreto), payload, hashlib.sha256).digest()[:LARGO_FIRMA]
        if not hmac.compare_digest(firma, esperada):
            return None
        venta_id, asiento, emitido = payload.decode().split(".")
        momento = ahora if ahora is not None else time.time()
        if not (0 <= momento - int(emitido) <= PDF_VIGENCIA_SEGUNDOS):
            return None
        if not (re.fullmatch(r"\d{1,12}", venta_id) and re.fullmatch(r"\d{1,3}", asiento)):
            return None
        return int(venta_id), asiento
    except Exception:
        return None


def celular_nacional(numero: str | None) -> str:
    """Los 9 dígitos del celular peruano (sin +51). "" si no es utilizable."""
    digitos = re.sub(r"\D", "", numero or "")
    if len(digitos) == 11 and digitos.startswith("51"):
        digitos = digitos[2:]
    return digitos if re.fullmatch(r"9\d{8}", digitos) else ""


# --------------------------------------------------------------------------- #
# Webhook
# --------------------------------------------------------------------------- #
def firma_webhook_valida(cuerpo: bytes, cabecera: str | None, app_secret: str) -> bool:
    """X-Hub-Signature-256: 'sha256=<hex>' = HMAC-SHA256(app_secret, cuerpo crudo)."""
    if not cabecera or not cabecera.startswith("sha256=") or not app_secret:
        return False
    esperada = hmac.new(app_secret.encode(), cuerpo, hashlib.sha256).hexdigest()
    return hmac.compare_digest(cabecera[len("sha256="):].strip().lower(), esperada)


@dataclass(frozen=True)
class MensajeEntrante:
    id: str
    telefono: str
    tipo: str
    texto: str
    nombre_perfil: str | None
    boton_id: str = ""     # id del botón pulsado (mensajes "interactive" / button_reply)
    # phone_number_id del número de la empresa que RECIBIÓ el mensaje (metadata
    # del aviso de Meta). La respuesta sale de ese mismo número: con dos números
    # en la cuenta, al cliente que escribió al oficial le contestaba el otro
    # (el fijo de PHONE_NUMBER_ID, 2026-10-16).
    numero_negocio_id: str = ""


@dataclass(frozen=True)
class EstadoEntrega:
    id: str
    telefono: str
    estado: str
    error: str | None


def extraer_eventos(body: dict) -> tuple[list[MensajeEntrante], list[EstadoEntrega]]:
    """Recorre la notificación de Meta sin confiar en su forma (todo con .get)."""
    mensajes: list[MensajeEntrante] = []
    estados: list[EstadoEntrega] = []
    for entry in body.get("entry") or []:
        for change in (entry or {}).get("changes") or []:
            value = (change or {}).get("value") or {}
            metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
            negocio_id = re.sub(r"\D", "", str(metadata.get("phone_number_id") or ""))[:30]
            contactos = value.get("contacts") or []
            nombre = None
            if contactos and isinstance(contactos[0], dict):
                nombre = ((contactos[0].get("profile") or {}).get("name") or "")[:80] or None
            for msg in value.get("messages") or []:
                if not isinstance(msg, dict) or not msg.get("id") or not msg.get("from"):
                    continue
                texto, boton = "", ""
                if msg.get("type") == "text":
                    texto = str((msg.get("text") or {}).get("body") or "")[:MAX_TEXTO]
                elif msg.get("type") == "interactive":
                    respuesta = (msg.get("interactive") or {}).get("button_reply") or {}
                    if isinstance(respuesta, dict):
                        boton = str(respuesta.get("id") or "")[:64]
                mensajes.append(MensajeEntrante(
                    id=str(msg["id"])[:128], telefono=re.sub(r"\D", "", str(msg["from"]))[:15],
                    tipo=str(msg.get("type") or ""), texto=texto, nombre_perfil=nombre, boton_id=boton,
                    numero_negocio_id=negocio_id,
                ))
            for st in value.get("statuses") or []:
                if not isinstance(st, dict) or not st.get("id"):
                    continue
                errores = st.get("errors") or []
                error = None
                if errores and isinstance(errores[0], dict):
                    error = f"{errores[0].get('code')}: {errores[0].get('title')}"[:200]
                estados.append(EstadoEntrega(
                    id=str(st["id"])[:128], telefono=re.sub(r"\D", "", str(st.get("recipient_id") or ""))[:15],
                    estado=str(st.get("status") or ""), error=error,
                ))
    return mensajes, estados


# Lo que escribe una persona (también un botón de un mensaje viejo, una foto o
# un audio). Fuera: reacciones, avisos del sistema y tipos que Meta no soporta.
TIPOS_QUE_RESPONDEN = frozenset({
    "text", "interactive", "button", "image", "audio", "video", "document", "sticker", "location", "contacts",
})


def merece_respuesta(msg: MensajeEntrante) -> bool:
    return msg.tipo in TIPOS_QUE_RESPONDEN


# --------------------------------------------------------------------------- #
# Envío
# --------------------------------------------------------------------------- #
def _enviar(numero_destino: str, payload: dict, *, token: str, phone_id: str,
            que: str) -> tuple[bool, str | None]:
    """POST a la Cloud API. Devuelve (ok, error); el error nunca incluye el token."""
    if not token or not phone_id:
        return False, "Faltan WHATSAPP_TOKEN o PHONE_NUMBER_ID."
    try:
        r = requests.post(
            f"{GRAPH_URL}/{phone_id}/messages",
            headers={"Authorization": f"Bearer {token}"},
            json=payload, timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        return False, f"Meta no respondió ({type(e).__name__})."
    log.info("%s a %s → %s", que, privacidad.telefono(numero_destino), r.status_code)
    if r.status_code >= 300:
        return False, f"Meta rechazó el envío ({r.status_code}): {r.text[:200]}"
    return True, None


def enviar_boton_compra(numero_destino: str, *, token: str, phone_id: str,
                        url_frontend: str, secreto_enlace: bytes) -> tuple[bool, str | None]:
    """
    El único mensaje del bot: bienvenida, botón "COMPRAR PASAJE" y canales del
    asesor humano. Sin encabezado (logo) ni paso de Términos y Condiciones
    (pedido de la empresa, 2026-10-15). Un mensaje con botón de enlace admite
    UN solo botón: por eso el asesor va como enlaces en el texto.
    ENVÍA UN WHATSAPP REAL. Devuelve (ok, error); el error nunca incluye el token.
    """
    if not token or not phone_id:
        return False, "Faltan WHATSAPP_TOKEN o PHONE_NUMBER_ID."

    # El enlace sale de URL_FRONTEND (.env). Hubo una URL fija a
    # /beta/buscar%20viaje/1-2-corregir.html que ignoraba el .env: con la carpeta
    # frontend/ esa página ya no existe y el cliente no llegaba a buscar-viaje.html.

    texto = (
        "🚌 *¡Bienvenido(a) a Transportes Moquegua Turismo!*\n"
        "Tu viaje comienza con nosotros.\n\n"
        "¿Qué deseas realizar? Selecciona una opción:\n\n"
        "🎫 *COMPRAR PASAJE*\n"
        "Compra tu pasaje de forma rápida y segura. Nuestro asistente automático te guiará paso a paso durante todo el proceso.\n\n"
        "👩‍💻 *ASESOR HUMANO*\n"
        "¿Tienes alguna consulta o necesitas orientación? Accede a nuestros canales de atención virtual haciendo clic en:\n"
        " ✈️ *TELEGRAM:* https://t.me/TmoqueguaBot"
        " 💬 *MESSENGER:* https://m.me/103404837807133"
    )

    separador = "&" if "?" in url_frontend else "?"
    url = f"{url_frontend}{separador}{urlencode({'c': firmar_contacto(numero_destino, secreto_enlace)})}"
    
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": numero_destino,
        "type": "interactive",
        "interactive": {
            "type": "cta_url",
            "body": {"text": texto},
            "footer": {"text": "Toca el botón para continuar 👇"},
            "action": {
                "name": "cta_url",
                "parameters": {
                    "display_text": "COMPRAR PASAJE",
                    "url": url
                }
            },
        },
    }
    return _enviar(numero_destino, payload, token=token, phone_id=phone_id, que="Botón de compra")