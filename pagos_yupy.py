"""
pagos_yupy.py — cliente de la pasarela Yupy.

Reglas (ver skill venta-y-pago y hallazgos V1/V2/N5/N6):
  - Una sola URL base para crear y para verificar (antes el checkout se creaba
    siempre en sandbox y se verificaba contra YUPY_BASE_URL, N6).
  - Todo con timeout.
  - El monto NUNCA sale del navegador: el llamador pasa el que calculó el
    servidor, y `evaluar_orden` exige que Yupy haya cobrado exactamente eso.

Los nombres de campo de la respuesta (status / payment_status, amount,
currency) y los estados aprobados son los que usa hoy el código; si Yupy
documenta otros, se corrigen solo aquí.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import requests

from jelaf_http import a_decimal

log = logging.getLogger("tmt.yupy")

TIMEOUT = 10
# "PAGADO": el estado con que el checkout QR de Yupy (/v1/checkouts) informa un
# pago terminado (su sdk.js 1.0.0 usa pagado/cancelado/timeout). Igual se exige
# monto y moneda exactos en evaluar_orden.
ESTADOS_APROBADOS = frozenset({"RECONCILED", "PAID", "APPROVED", "COMPLETED", "PAGADO"})
MONEDA = "PEN"


class ErrorYupy(RuntimeError):
    """Yupy no respondió o respondió algo inutilizable. El mensaje no contiene secretos."""


def evaluar_orden(orden: dict, monto_esperado: Decimal) -> str | None:
    """
    Decide si una orden de Yupy prueba que se cobró lo que se tenía que cobrar.
    Devuelve None si está bien, o el motivo en texto si no. Función pura.
    """
    estado = str(orden.get("status") or orden.get("payment_status") or "").upper()
    if estado not in ESTADOS_APROBADOS:
        return f"el pago no está aprobado (estado: {estado or 'desconocido'})"
    moneda = str(orden.get("currency") or MONEDA).upper()
    if moneda != MONEDA:
        return f"moneda inesperada ({moneda})"
    cobrado = a_decimal(orden.get("amount"))
    if cobrado != monto_esperado.quantize(Decimal("0.01")):
        return f"monto cobrado S/ {cobrado} distinto del esperado S/ {monto_esperado:.2f}"
    return None


def pago_aprobado(orden: dict) -> bool:
    """True si Yupy dice que el dinero se cobró, sin importar el monto."""
    return str(orden.get("status") or orden.get("payment_status") or "").upper() in ESTADOS_APROBADOS


API_PAYMENT_ORDERS = "payment_orders"
API_CHECKOUTS = "checkouts"
SDK_PAYMENT_ORDERS = "https://checkout.yupy.us/v1/yupy-checkout.js"


class ClienteYupy:
    """
    Dos APIs de checkout (YUPY_API):
      - "payment_orders" (por defecto): /v1/payment-orders + checkout-sessions,
        con el SDK checkout.yupy.us/v1/yupy-checkout.js.
      - "checkouts": /v1/checkouts con QR Yape/Plin y el SDK
        <base>/v1/checkouts/browser/sdk.js (YupyCheckout.open), como lo integró
        el compañero en CHATBOT PANELES. Devuelve `payment_order_uid` y el pago
        se verifica con GET /v1/payment-orders/<uid>. POR CONFIRMAR en sandbox:
        la integración original no verificaba en el servidor; si Yupy usa otra
        ruta, /confirmar responde 503 y NO emite (falla cerrado).
    """

    def __init__(self, base_url: str, client_id: str, client_secret: str,
                 api: str = API_PAYMENT_ORDERS):
        self.base_url = base_url.rstrip("/")
        self.api = api
        self._client_id = client_id
        self._client_secret = client_secret
        self._token: str | None = None
        self._token_vence = 0.0
        self._lock = threading.Lock()

    @property
    def configurado(self) -> bool:
        return bool(self._client_id and self._client_secret)

    @property
    def sdk_url(self) -> str:
        """SDK de navegador que corresponde a la API configurada."""
        if self.api == API_CHECKOUTS:
            return f"{self.base_url}/v1/checkouts/browser/sdk.js"
        return SDK_PAYMENT_ORDERS

    def _credenciales(self) -> dict:
        if self.api != API_CHECKOUTS:
            return {"client_id": self._client_id, "client_secret": self._client_secret}
        # /v1/checkouts: client_id numérico y grant_type, como en la integración del compañero.
        client_id = int(self._client_id) if self._client_id.isdigit() else self._client_id
        return {"client_id": client_id, "client_secret": self._client_secret,
                "grant_type": "client_credentials"}

    def _obtener_token(self) -> str:
        with self._lock:
            if self._token and time.monotonic() < self._token_vence:
                return self._token
            try:
                r = requests.post(
                    f"{self.base_url}/v1/auth/token",
                    json=self._credenciales(),
                    timeout=TIMEOUT,
                )
            except requests.RequestException as e:
                raise ErrorYupy(f"Yupy no respondió al autenticar ({type(e).__name__}).") from e
            if r.status_code != 200:
                raise ErrorYupy(f"Yupy rechazó la autenticación ({r.status_code}).")
            datos = r.json()
            token = datos.get("access_token")
            if not token:
                raise ErrorYupy("Yupy no devolvió access_token.")
            # Se renueva un minuto antes de que venza; si Yupy no dice cuánto dura, 5 min.
            dura = int(datos.get("expires_in") or 300)
            self._token = token
            self._token_vence = time.monotonic() + max(30, dura - 60)
            return token

    def crear_checkout(self, pedido: str, monto: Decimal, comprador: str, expira_segundos: int) -> dict:
        """
        Crea el cobro. Con cualquiera de las dos APIs devuelve al menos
        {"yupy_transaction_id": <id a verificar>, "checkout_session": {"checkout_url": ...}}.
        """
        if self.api == API_CHECKOUTS:
            return self._crear_checkout_qr(pedido, monto, comprador, expira_segundos)
        token = self._obtener_token()
        try:
            r = requests.post(
                f"{self.base_url}/v1/payment-orders",
                headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())},
                json={
                    "external_transaction_id": pedido,
                    "amount": f"{monto:.2f}",
                    "currency": MONEDA,
                    "buyer_name": comprador[:80],
                    "integration_experience": "web_checkout",
                },
                timeout=TIMEOUT,
            )
        except requests.RequestException as e:
            raise ErrorYupy(f"Yupy no respondió al crear la orden ({type(e).__name__}).") from e
        if r.status_code not in (200, 201):
            raise ErrorYupy(f"Yupy rechazó la orden ({r.status_code}).")
        orden = r.json()
        tx = orden.get("yupy_transaction_id")
        if not tx:
            raise ErrorYupy("Yupy no devolvió yupy_transaction_id.")

        if "checkout_session" not in orden:
            try:
                rs = requests.post(
                    f"{self.base_url}/v1/payment-orders/{tx}/checkout-sessions",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"expires_in_seconds": expira_segundos},
                    timeout=TIMEOUT,
                )
            except requests.RequestException as e:
                raise ErrorYupy(f"Yupy no respondió al crear el checkout ({type(e).__name__}).") from e
            if rs.status_code not in (200, 201):
                raise ErrorYupy(f"Yupy rechazó la sesión de checkout ({rs.status_code}).")
            orden["checkout_session"] = rs.json()
        return orden

    def _crear_checkout_qr(self, pedido: str, monto: Decimal, comprador: str, expira_segundos: int) -> dict:
        token = self._obtener_token()
        try:
            r = requests.post(
                f"{self.base_url}/v1/checkouts",
                headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())},
                json={
                    "external_transaction_id": pedido,
                    "amount": f"{monto:.2f}",
                    "currency": MONEDA,
                    "payment_method": "qr",
                    "payer": {"first_name": comprador[:120]},
                    "timeout_seconds": expira_segundos,
                },
                timeout=TIMEOUT,
            )
        except requests.RequestException as e:
            raise ErrorYupy(f"Yupy no respondió al crear el checkout ({type(e).__name__}).") from e
        if r.status_code not in (200, 201):
            raise ErrorYupy(f"Yupy rechazó el checkout ({r.status_code}).")
        datos = r.json()
        tx = datos.get("payment_order_uid") or datos.get("yupy_transaction_id")
        url = datos.get("checkout_url")
        if not tx or not url:
            raise ErrorYupy("Yupy no devolvió payment_order_uid / checkout_url.")
        # El checkout_token es lo único con que después se puede consultar el
        # estado del pago (browser/status): sin él no se entrega el checkout.
        if not datos.get("checkout_token"):
            raise ErrorYupy("Yupy no devolvió el checkout_token: no habría cómo verificar el pago.")
        # Yupy repite el monto y la moneda del checkout: tienen que ser los del servidor.
        if datos.get("amount") is not None and a_decimal(datos.get("amount")) != monto.quantize(Decimal("0.01")):
            raise ErrorYupy(f"Yupy creó el checkout por otro monto ({datos.get('amount')}).")
        if str(datos.get("currency") or MONEDA).upper() != MONEDA:
            raise ErrorYupy(f"Yupy creó el checkout en otra moneda ({datos.get('currency')}).")
        return {**datos, "yupy_transaction_id": tx, "checkout_session": {"checkout_url": url}}

    # --- Browser Checkout (API /v1/checkouts) -------------------------------------
    # Contrato público (https://api.yupy.us/docs, sección 4 y 14.4): el navegador
    # y el comercio consultan el estado canónico de la Payment Order con
    # POST /v1/checkouts/browser/status {"checkout_token"}; los resultados
    # terminales son pagado | cancelado | timeout. GET /v1/payment-orders/{id} es
    # del orquestador interno de Yupy, no de la API pública (por eso fallaba).
    def _post_navegador(self, ruta: str, checkout_token: str | None, que: str) -> dict:
        if not checkout_token:
            raise ErrorYupy(f"Falta el checkout_token para consultar {que}.")
        try:
            r = requests.post(f"{self.base_url}/v1/checkouts/browser/{ruta}",
                              json={"checkout_token": checkout_token}, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise ErrorYupy(f"Yupy no respondió al consultar {que} ({type(e).__name__}).") from e
        if r.status_code != 200:
            raise ErrorYupy(f"Yupy no devolvió {que} ({r.status_code}).")
        datos = r.json()
        if not isinstance(datos, dict):
            raise ErrorYupy(f"Yupy devolvió {que} en un formato inesperado.")
        return datos

    def simulacion_habilitada(self, checkout_token: str | None) -> bool | None:
        """
        ¿Este checkout muestra "Simular pago"? (credencial con payments:simulate).
        Una simulación da un `pagado` canónico idéntico a uno real: con eso, en
        venta real cualquiera obtendría un boleto sin pagar. None = no se supo.
        """
        try:
            contexto = self._post_navegador("context", checkout_token, "el contexto del checkout")
        except ErrorYupy as e:
            log.warning("No se pudo leer el contexto del checkout de Yupy: %s", e)
            return None
        return bool(contexto.get("payment_simulation_enabled"))

    def cancelar_checkout(self, checkout_token: str | None) -> None:
        """Cancelación canónica de un checkout que no se va a usar (best-effort)."""
        try:
            self._post_navegador("cancel", checkout_token, "la cancelación del checkout")
        except ErrorYupy as e:
            log.warning("No se pudo cancelar el checkout de Yupy: %s", e)

    def consultar_orden(self, yupy_transaction_id: str, checkout_token: str | None = None,
                        monto_creado: Decimal | None = None) -> dict:
        """
        Estado del cobro para `evaluar_orden`. Con la API /v1/checkouts se
        consulta browser/status con el checkout_token guardado al crear el
        checkout; si Yupy no repite el monto, vale `monto_creado` (el del
        servidor, que Yupy confirmó al crear el checkout).
        """
        if self.api == API_CHECKOUTS:
            estado = self._post_navegador("status", checkout_token, "el estado del pago")
            log.info("Yupy browser/status (forma): %s", esquema_orden(estado))
            return normalizar_estado_checkout(estado, monto_creado)
        token = self._obtener_token()
        try:
            r = requests.get(
                f"{self.base_url}/v1/payment-orders/{yupy_transaction_id}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=TIMEOUT,
            )
        except requests.RequestException as e:
            raise ErrorYupy(f"Yupy no respondió al consultar la orden ({type(e).__name__}).") from e
        if r.status_code != 200:
            raise ErrorYupy(f"Yupy no encontró la orden ({r.status_code}).")
        return r.json()


# --- Webhook (https://api.yupy.us/docs, 14.7) -------------------------------------------
# "YUPY envía X-YUPY-Timestamp y X-YUPY-Signature. La firma usa HMAC-SHA256 sobre
# timestamp + "." + raw_body, usando el Secret de firma configurado para el
# endpoint. La verificación debe hacerse contra los bytes crudos del cuerpo."
TOLERANCIA_WEBHOOK_SEGUNDOS = 300


def firma_webhook_valida(cuerpo: bytes, timestamp: str | None, firma: str | None, secreto: str) -> bool:
    """
    Compara en tiempo constante. La documentación no fija la codificación de la
    firma: se acepta hex o base64, con o sin prefijo "sha256=" / "v1=".
    """
    if not secreto or not timestamp or not firma:
        return False
    digest = hmac.new(secreto.encode(), timestamp.encode() + b"." + cuerpo, hashlib.sha256).digest()
    recibida = firma.strip()
    for prefijo in ("sha256=", "v1="):
        if recibida.lower().startswith(prefijo):
            recibida = recibida[len(prefijo):]
    candidatos = (digest.hex(), base64.b64encode(digest).decode())
    return any(hmac.compare_digest(recibida.encode(), c.encode()) for c in candidatos) or \
        hmac.compare_digest(recibida.lower().encode(), digest.hex().encode())


def timestamp_vigente(timestamp: str | None, ahora: float | None = None,
                      tolerancia: int = TOLERANCIA_WEBHOOK_SEGUNDOS) -> bool | None:
    """
    ¿El aviso es reciente? (evita que alguien reenvíe un aviso viejo capturado).
    Acepta segundos o milisegundos Unix, o ISO 8601. None = formato desconocido
    (se registra; la firma y la deduplicación por event_id siguen protegiendo).
    """
    ahora = time.time() if ahora is None else ahora
    texto = str(timestamp or "").strip()
    if not texto:
        return None
    try:
        valor = float(texto)
        momento = valor / 1000 if valor > 1e11 else valor
    except ValueError:
        try:
            dt = datetime.fromisoformat(texto.replace("Z", "+00:00"))
        except ValueError:
            return None
        momento = (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
    return abs(ahora - momento) <= tolerancia


def normalizar_estado_checkout(estado: dict, monto_creado: Decimal | None) -> dict:
    """
    browser/status → la forma que entiende `evaluar_orden` (status, amount,
    currency). La documentación no fija los nombres de los campos de la
    respuesta: se busca el estado en status/result/estado (también dentro de
    payment_order) y se registra la forma en el log para confirmarlo.
    """
    def buscar(d: dict, claves: tuple[str, ...]):
        for k in claves:
            v = d.get(k)
            if v not in (None, "") and not isinstance(v, (dict, list)):
                return v
        return None

    anidado = estado.get("payment_order") if isinstance(estado.get("payment_order"), dict) else {}
    claves_estado = ("status", "result", "estado", "payment_status")
    valor_estado = buscar(estado, claves_estado) or buscar(anidado, claves_estado) or ""
    monto = buscar(estado, ("amount",)) or buscar(anidado, ("amount",))
    if monto is None and monto_creado is not None:
        monto = f"{Decimal(monto_creado):.2f}"
    return {
        "status": str(valor_estado),
        "amount": monto,
        "currency": buscar(estado, ("currency",)) or buscar(anidado, ("currency",)) or MONEDA,
        "payment_method": buscar(estado, ("payment_method",)) or buscar(anidado, ("payment_method",)),
    }


def esquema_orden(orden, profundidad: int = 0):
    """Solo los nombres y tipos de los campos de una orden de Yupy, nunca los valores."""
    if profundidad > 4:
        return "…"
    if isinstance(orden, dict):
        return {k: esquema_orden(v, profundidad + 1) for k, v in orden.items()}
    if isinstance(orden, list):
        return [esquema_orden(orden[0], profundidad + 1)] if orden else []
    return type(orden).__name__


def medio_de_pago(orden: dict) -> tuple[str | None, str | None]:
    """
    (medio, 4 últimos dígitos) con que pagó el cliente según Yupy:
    medio ∈ {"yape", "visa", "mastercard"} o None si no se sabe.

    PENDIENTE: todavía no se conoce en qué campos informa Yupy el medio, la
    marca de la tarjeta y el número de operación (no hay documentación en el
    proyecto y el sandbox estaba caído). No se adivinan nombres de campo: hasta
    confirmarlo devuelve (None, None) y el backend usa JELAF_TARJETA_DEFECTO.
    El backend deja en el log la forma de cada orden pagada (esquema_orden)
    para completar esta función con datos reales.
    """
    return None, None
