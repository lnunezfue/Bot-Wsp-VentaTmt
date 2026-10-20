"""
pagos_niubiz.py — cliente de Niubiz (pago con tarjeta, formulario de checkout.js).

Contrato tomado de la integración del compañero, probada contra el sandbox
de Niubiz (apisandbox.vnforappstest.com):
  1. GET  /api.security/v1/security                        (Basic usuario:clave) → token
  2. POST /api.ecommerce/v2/ecommerce/token/session/<comercio>                   → sessionKey
     El navegador abre checkout.js con ese sessionKey; al pagar, Niubiz envía
     por POST (formulario) un `transactionToken` a nuestra URL de `action`.
  3. POST /api.authorization/v3/authorization/ecommerce/<comercio>               → autorización

Lo importante para el dinero: con Niubiz el cargo ocurre recién en el paso 3,
y ese paso lo hace NUESTRO servidor con el monto que calculó el servidor. Un
transactionToken que nunca se autoriza no cobra nada. Por eso:
  - el monto y el número de pedido salen de la tabla `pago`, nunca del navegador;
  - `evaluar_autorizacion` exige STATUS=Authorized y ACTION_CODE=000, y que
    monto, moneda y pedido (si Niubiz los devuelve) coincidan.
  - En JELAF la venta se registra como tarjeta VISA/MASTERCARD con los 4
    últimos dígitos (`medio_tarjeta`); de la tarjeta no se guarda nada más.

Diferencias con el código original, a propósito: credenciales solo por .env
(el original las tenía escritas y el repo es público, S6), sin `print` de la
respuesta (trae datos de la tarjeta) y solo se guarda un resumen sin PAN ni
titular (`resumen_autorizacion`).
"""

from __future__ import annotations

import base64
import logging
import re
from decimal import Decimal

import requests

from jelaf_http import a_decimal

log = logging.getLogger("tmt.niubiz")

TIMEOUT = 30
MONEDA = "PEN"
# ISO 4217 numérico: Niubiz a veces devuelve la moneda así en dataMap.
_MONEDAS_PEN = frozenset({"PEN", "604", "0604"})
# El token de seguridad de Niubiz es de UN solo uso: la segunda sesión con el
# mismo token responde 400 "Token has been used before" (sonda en el sandbox,
# 2026-10-10). Antes se guardaba 50 minutos y, tras el primer pago con tarjeta,
# nadie más podía abrir el formulario hasta reiniciar el backend. Ahora se pide
# uno nuevo para cada sesión y cada autorización.
# Datos que exige el antifraude de Niubiz y que el bot no recolecta: los mismos
# valores fijos con los que se probó en sandbox. No son datos del cliente.
_ANTIFRAUDE_FIJO = {
    "cardholderCity": "Tacna",
    "cardholderCountry": "PE",
    "cardholderAddress": "Transportes Moquegua Turismo",
    "cardholderPostalCode": "23001",
    "cardholderState": "TAC",
    "cardholderPhoneNumber": "999999999",
}
_UBICACION_SERVICIO = {
    "serviceLocationCityName": "Tacna",
    "serviceLocationCountrySubdivisionCode": "TAC",
    "serviceLocationCountryCode": "PER",
    "serviceLocationPostalCode": "23001",
}
# Campos que se guardan de la autorización (para conciliar y reembolsar).
# Nada de número de tarjeta, titular ni correo.
_CAMPOS_RESUMEN = ("STATUS", "ACTION_CODE", "ACTION_DESCRIPTION", "TRACE_NUMBER", "TRANSACTION_ID",
                   "AUTHORIZATION_CODE", "ID_UNICO", "BRAND", "TRANSACTION_DATE")


class ErrorNiubiz(RuntimeError):
    """Niubiz no respondió o respondió algo inutilizable. El mensaje no contiene secretos."""


def _motivo(r) -> str:
    """El porqué de un rechazo de Niubiz (errorMessage/errorCode), corto. Nunca datos de tarjeta:
    en una sesión no hay tarjeta todavía."""
    try:
        datos = r.json()
    except ValueError:
        return "sin detalle"
    if not isinstance(datos, dict):
        return "sin detalle"
    texto = str(datos.get("errorMessage") or datos.get("message") or datos.get("errorCode") or "sin detalle")
    return re.sub(r"\d{6,}", "…", texto)[:120]


def _datamap(respuesta: dict) -> dict:
    datos = respuesta.get("dataMap")
    return datos if isinstance(datos, dict) else {}


def _orden(respuesta: dict) -> dict:
    datos = respuesta.get("order")
    return datos if isinstance(datos, dict) else {}


def autorizada(respuesta: dict) -> bool:
    """True si Niubiz dice que el cargo se aprobó, sin mirar el monto."""
    dm = _datamap(respuesta)
    return str(dm.get("STATUS") or "").lower() == "authorized" and str(dm.get("ACTION_CODE") or "") == "000"


def evaluar_autorizacion(respuesta: dict, pedido: str, monto_esperado: Decimal) -> str | None:
    """
    ¿Esta autorización prueba que se cobró lo que se tenía que cobrar?
    None si está bien; si no, el motivo. Función pura (como evaluar_orden de Yupy).
    """
    if not isinstance(respuesta, dict):
        return "respuesta de Niubiz ilegible"
    dm, orden = _datamap(respuesta), _orden(respuesta)
    if not autorizada(respuesta):
        # Un rechazo de tarjeta viene en `data` (no en `dataMap`) y un error de
        # Niubiz (p. ej. token ya usado) en errorMessage/errorCode: se muestra el
        # motivo real en vez de "desconocido".
        detalle = dm or (respuesta.get("data") if isinstance(respuesta.get("data"), dict) else {})
        estado = detalle.get("STATUS") or respuesta.get("errorMessage") or "desconocido"
        codigo = detalle.get("ACTION_CODE") or respuesta.get("errorCode") or "?"
        descripcion = detalle.get("ACTION_DESCRIPTION")
        return (f"el pago no está autorizado (estado: {estado}, código: {codigo}"
                f"{', ' + str(descripcion) if descripcion else ''})")
    esperado = monto_esperado.quantize(Decimal("0.01"))
    for monto in (orden.get("amount"), orden.get("authorizedAmount"), dm.get("AMOUNT")):
        if monto not in (None, "") and a_decimal(monto) != esperado:
            return f"monto autorizado S/ {a_decimal(monto)} distinto del esperado S/ {esperado}"
    for moneda in (orden.get("currency"), dm.get("CURRENCY")):
        if moneda not in (None, "") and str(moneda).upper() not in _MONEDAS_PEN:
            return f"moneda inesperada ({moneda})"
    numero = orden.get("purchaseNumber")
    if numero not in (None, "") and str(numero) != str(pedido):
        return "la autorización es de otro pedido"
    return None


def resumen_autorizacion(respuesta: dict) -> dict:
    """Lo que se guarda en la base: estado, códigos y montos. Sin datos de la tarjeta."""
    dm, orden = _datamap(respuesta), _orden(respuesta)
    resumen = {k: dm[k] for k in _CAMPOS_RESUMEN if k in dm}
    for k in ("purchaseNumber", "amount", "authorizedAmount", "currency", "transactionId", "actionCode"):
        if k in orden:
            resumen[f"order.{k}"] = orden[k]
    if "dataMap" not in respuesta and "data" in respuesta:   # rechazo: Niubiz responde en `data`
        datos = respuesta.get("data") if isinstance(respuesta.get("data"), dict) else {}
        resumen.update({k: datos[k] for k in _CAMPOS_RESUMEN if k in datos})
    if respuesta.get("errorCode") is not None:
        resumen["errorCode"] = respuesta.get("errorCode")
    # Solo los 4 últimos dígitos (nunca el número enmascarado completo): es lo
    # que JELAF registra para cruzar la venta con el cobro (decisión de la
    # empresa, docs/descubrimiento-venta-http.md "Registro del pago en JELAF").
    tarjeta = str(dm.get("CARD") or "")
    if re.search(r"\d{4}$", tarjeta):
        resumen["ultimos4"] = tarjeta[-4:]
    return resumen


def medio_tarjeta(resumen: dict) -> tuple[str | None, str | None]:
    """
    (medio, 4 últimos dígitos) de una autorización guardada, para registrar el
    pago en JELAF: medio ∈ {"visa", "mastercard"} o None si la marca no está en
    el catálogo de JELAF (entonces se usa JELAF_TARJETA_DEFECTO).
    """
    marca = str((resumen or {}).get("BRAND") or "").strip().lower()
    medio = marca if marca in ("visa", "mastercard") else None
    return medio, (resumen or {}).get("ultimos4")


def reconstruir(resumen: dict) -> dict:
    """Vuelve a armar, desde el resumen guardado, la forma que espera evaluar_autorizacion."""
    orden = {k.split(".", 1)[1]: v for k, v in resumen.items() if k.startswith("order.")}
    return {"dataMap": {k: v for k, v in resumen.items() if "." not in k}, "order": orden}


class ClienteNiubiz:
    def __init__(self, base_url: str, merchant_id: str, usuario: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.merchant_id = merchant_id
        self._usuario = usuario
        self._password = password

    @property
    def configurado(self) -> bool:
        return bool(self.base_url and self.merchant_id and self._usuario and self._password)

    def _token_seguridad(self) -> str:
        """Token NUEVO en cada llamada: Niubiz no acepta uno ya usado (ver arriba)."""
        credencial = base64.b64encode(f"{self._usuario}:{self._password}".encode()).decode()
        try:
            r = requests.get(
                f"{self.base_url}/api.security/v1/security",
                headers={"Authorization": f"Basic {credencial}", "Accept": "*/*"},
                timeout=TIMEOUT,
            )
        except requests.RequestException as e:
            raise ErrorNiubiz(f"Niubiz no respondió al autenticar ({type(e).__name__}).") from e
        if r.status_code not in (200, 201):
            raise ErrorNiubiz(f"Niubiz rechazó la autenticación ({r.status_code}).")
        token = r.text.strip().strip('"')
        if not token:
            raise ErrorNiubiz("Niubiz no devolvió el token de seguridad.")
        return token

    def crear_sesion(self, monto: Decimal, ip_cliente: str, id_cliente: str) -> str:
        """sessionKey para abrir el formulario de checkout.js por `monto` (lo pone el servidor)."""
        token = self._token_seguridad()
        try:
            r = requests.post(
                f"{self.base_url}/api.ecommerce/v2/ecommerce/token/session/{self.merchant_id}",
                headers={"Authorization": token, "Content-Type": "application/json"},
                json={
                    "channel": "web",
                    "amount": float(monto.quantize(Decimal("0.01"))),
                    # MDD*: valores con los que se probó en sandbox (el bot no pide correo).
                    "antifraud": {
                        "clientIp": ip_cliente or "127.0.0.1",
                        "merchantDefineData": {"MDD4": "cliente@email.com", "MDD32": id_cliente[:40],
                                               "MDD75": "Invitado", "MDD77": 1},
                    },
                    "dataMap": _ANTIFRAUDE_FIJO,
                },
                timeout=TIMEOUT,
            )
        except requests.RequestException as e:
            raise ErrorNiubiz(f"Niubiz no respondió al crear la sesión ({type(e).__name__}).") from e
        if r.status_code not in (200, 201):
            raise ErrorNiubiz(f"Niubiz rechazó la sesión ({r.status_code}: {_motivo(r)}).")
        clave = (r.json() or {}).get("sessionKey")
        if not clave:
            raise ErrorNiubiz("Niubiz no devolvió sessionKey.")
        return clave

    def autorizar(self, transaction_token: str, pedido: str, monto: Decimal, url_comercio: str) -> dict:
        """
        Hace el CARGO. Devuelve la respuesta de Niubiz tal cual (aprobada o
        rechazada: un rechazo también es una respuesta válida). Lanza
        ErrorNiubiz solo si no hubo respuesta utilizable.
        """
        token = self._token_seguridad()
        try:
            r = requests.post(
                f"{self.base_url}/api.authorization/v3/authorization/ecommerce/{self.merchant_id}",
                headers={"Authorization": token, "Content-Type": "application/json"},
                json={
                    "channel": "web",
                    "captureType": "manual",
                    "countable": True,
                    "order": {
                        "tokenId": transaction_token,
                        "purchaseNumber": pedido,
                        "amount": float(monto.quantize(Decimal("0.01"))),
                        "currency": MONEDA,
                    },
                    "dataMap": {"urlAddress": url_comercio, **_UBICACION_SERVICIO},
                },
                timeout=TIMEOUT,
            )
        except requests.RequestException as e:
            raise ErrorNiubiz(f"Niubiz no respondió al autorizar ({type(e).__name__}).") from e
        try:
            datos = r.json()
        except ValueError as e:
            raise ErrorNiubiz(f"Niubiz respondió algo ilegible al autorizar ({r.status_code}).") from e
        if not isinstance(datos, dict):
            raise ErrorNiubiz(f"Niubiz respondió algo ilegible al autorizar ({r.status_code}).")
        return datos
