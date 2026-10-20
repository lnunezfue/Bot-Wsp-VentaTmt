"""
api_ventas.py — backend de la Boletería Virtual TMT (FastAPI, un solo proceso).

Flujo de venta (v2) SIN NAVEGADOR: cada venta es un `VentaHttp`
(vendedor_http.py) que habla con JELAF por HTTP con una de las cuentas
configuradas (PoolCuentas): VENTAS_POR_CUENTA ventas a la vez por cuenta. No
se abre navegador, ni para iniciar sesión. Con VENTA_MODO=simulacion (por defecto) no se bloquean
asientos ni se emite. Ver docs/venta-sin-navegador.md.

Reglas de dinero (docs/REVISION_2026-09-26.md, skill venta-y-pago):
  - El precio de cada asiento lo pone el SERVIDOR con datos de JELAF; el que
    manda el navegador se ignora (V2).
  - El monto de Yupy lo calcula el servidor y queda guardado en la tabla
    `pago`; /confirmar exige que Yupy haya cobrado exactamente eso (V1).
  - /confirmar es idempotente: dos llamadas nunca emiten dos veces (N3/C2).
  - Si algo falla después de cobrar, la venta se registra y se concilia
    contra el plano real de JELAF; nunca desaparece (C4/N4/N5).

Los endpoints v1 que emitían o bloqueaban inventario sin pago
(confirmar-compra, bloquear-asiento, liberar-asiento, pre-checkout) se
eliminaron (N2/S4). Quedan en el historial de git.
"""

from __future__ import annotations

import asyncio
import collections
import hmac
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal
from urllib.parse import parse_qs, urljoin

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AfterValidator, BaseModel, Field, StringConstraints

import config as config_mod

config_mod.configurar_salida_utf8()

import boleto_pdf  # noqa: E402
import correo  # noqa: E402
import pagos_niubiz  # noqa: E402
import pagos_yupy  # noqa: E402
import planos  # noqa: E402
import privacidad  # noqa: E402
import rutas  # noqa: E402
import whatsapp  # noqa: E402
from jelaf_http import (  # noqa: E402
    CIUDADES_CODIGOS,
    CIUDADES_POR_CODIGO,
    ClienteJelafHttp,
    ErrorJelaf,
    EstadoTurno,
    ViajeJelaf,
    a_fecha_jelaf,
    minutos_del_dia,
)
from vendedor_http import JelafAsientoNoDisponibleError, PoolCuentas, VendedorHttp  # noqa: E402
from venta_jelaf import TARJETAS, DatosIncompletos, DatosPago, Facturacion, ruc_valido, tipo_doc_jelaf  # noqa: E402
from limitador import Limitador, ip_cliente  # noqa: E402
from pagos_niubiz import ClienteNiubiz, ErrorNiubiz  # noqa: E402
from pagos_yupy import ClienteYupy, ErrorYupy, esquema_orden, evaluar_orden, medio_de_pago, pago_aprobado  # noqa: E402
from paneles import consultas, registro, tiempo  # noqa: E402
from paneles import db as paneles_db  # noqa: E402
from paneles.api_paneles import configurar as configurar_paneles  # noqa: E402
from paneles.api_paneles import configurar_admin  # noqa: E402
from paneles.api_paneles import exigir_token  # noqa: E402
from paneles.api_paneles import router as router_paneles  # noqa: E402

log = logging.getLogger("tmt.api")

CONFIG = config_mod.cargar()

ESPERA_MOSTRADOR_SEGUNDOS = 15    # cola corta cuando todos los workers están ocupados
CACHE_PRECIOS_SEGUNDOS = 120      # el precio de un turno no cambia mientras el cliente elige
MARGEN_CHECKOUT_SEGUNDOS = 120    # el reaper espera esto de más tras vencer el checkout de Yupy
RESCATE_NIUBIZ_SEGUNDOS = 90      # si el navegador no vuelve a /confirmar tras un cargo con tarjeta
PREFIJO_NIUBIZ = "NIUBIZ-"        # referencia de un cobro de Niubiz en la tabla pago
# Tope de asientos por compra, igual al de la pantalla: sin él, una sola
# persona podía apartar un bus entero en JELAF (probado con navegador: 13).
MAX_ASIENTOS_POR_COMPRA = 5
DIAS_VENTA_ANTICIPADA = 120       # hasta cuándo se puede buscar/comprar
MINUTOS_CIERRE_VENTA = 30         # un viaje que sale antes de esto ya no se vende en línea

jelaf: ClienteJelafHttp | None = None             # cuenta preferida para consultas de lectura
pool_jelaf: PoolCuentas | VendedorHttp | None = None
yupy = ClienteYupy(CONFIG.yupy_base_url, CONFIG.yupy_client_id, CONFIG.yupy_client_secret, CONFIG.yupy_api)
niubiz = ClienteNiubiz(CONFIG.niubiz_base_url, CONFIG.niubiz_merchant_id,
                       CONFIG.niubiz_usuario, CONFIG.niubiz_password)
# Una autorización de Niubiz a la vez: es el paso que COBRA, y un doble envío
# del formulario no debe autorizar dos veces el mismo pedido.
_lock_niubiz = asyncio.Lock()
_reaper_task: asyncio.Task | None = None
_logins_task: asyncio.Task | None = None
# Entre un login y otro al arrancar: JELAF falla con logins muy seguidos (docs/setup-local.md).
PAUSA_ENTRE_LOGINS_SEGUNDOS = 5
REINTENTO_CUENTAS_CAIDAS_SEGUNDOS = 300

# Respaldo en memoria de las emisiones de este proceso, por si el registro en
# la base fallara justo después de emitir (el @seguro de paneles traga errores):
# sin esto, un reintento de /confirmar no sabría que ya se emitió.
_emisiones: dict[str, dict] = {}
_MAX_EMISIONES_EN_MEMORIA = 5000

# Tareas lanzadas "y olvidadas": se guarda la referencia para que el recolector
# de basura no las cancele a mitad de camino.
_tareas_fondo: set[asyncio.Task] = set()

log = logging.getLogger("tmt.yupy")
YUPY_WEBHOOK_SECRET = os.getenv("YUPY_WEBHOOK_SECRET", "")


def _en_segundo_plano(coro) -> None:
    tarea = asyncio.create_task(coro)
    _tareas_fondo.add(tarea)
    tarea.add_done_callback(_tareas_fondo.discard)


def _recordar_emision(booking_id: str, respuesta: dict) -> None:
    if len(_emisiones) >= _MAX_EMISIONES_EN_MEMORIA:
        _emisiones.pop(next(iter(_emisiones)))
    _emisiones[booking_id] = respuesta

_limite_iniciar = Limitador(maximo=10, ventana_segundos=60)
_limite_cliente_ip = Limitador(maximo=30, ventana_segundos=600)
_limite_cliente_venta = Limitador(maximo=15, ventana_segundos=600)
_limite_pdf = Limitador(maximo=30, ventana_segundos=60)
_limite_niubiz = Limitador(maximo=20, ventana_segundos=600)


# ==========================================
# ⚙️ CICLO DE VIDA
# ==========================================
async def _reaper_pool_inactivo():
    """
    Suelta las ventas abandonadas (celular apagado, WhatsApp en segundo plano,
    red caída): suelta sus asientos en JELAF y libera su lugar. Nunca toca una
    venta que está trabajando ni una con un checkout de Yupy vivo (ver
    VentaHttp.motivo_para_liberar).
    """
    while True:
        await asyncio.sleep(30)
        if not pool_jelaf:
            continue
        try:
            await _pasada_de_limpieza()
            await pool_jelaf.mantener_vivos()
            await _reintentar_cuentas_sin_sesion()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("[reaper] %s: %s", type(e).__name__, e)


async def _pasada_de_limpieza() -> None:
    """Primero se emite lo que ya se pagó; recién después se sueltan las abandonadas (en ese orden)."""
    await _conciliar_pagos_abiertos()
    await _soltar_inactivas()


# Conciliador (A1, N59-SN): el boleto ya no depende de que el NAVEGADOR vuelva
# a /confirmar. Con Yape el cliente sale de WhatsApp para pagar y, al volver,
# la página pudo recargarse o rendirse; el dinero entraba y nadie emitía.
CONCILIAR_CADA_SEGUNDOS = 20
_ultima_conciliacion: dict[str, float] = {}


async def _conciliar_pagos_abiertos() -> None:
    """
    Por cada venta activa en fase de pago: ¿la pasarela ya cobró? Si sí, se
    emite. A cada venta se le pregunta como mucho cada CONCILIAR_CADA_SEGUNDOS,
    salvo a la que la limpieza está por soltar: a esa se le pregunta siempre
    (última oportunidad antes de perder los asientos). Nunca lanza.
    """
    if not pool_jelaf:
        return
    ahora = time.monotonic()
    activas = set()
    for w in pool_jelaf.workers:      # propiedad: ya es una copia
        booking_id = w.owner_token
        if not booking_id or not w.es_dueno(booking_id) or not w.en_fase_pago:
            continue
        activas.add(booking_id)
        if w.lock.locked():
            continue                      # /confirmar u otra operación ya está trabajando en ella
        por_soltar = w.motivo_para_liberar(CONFIG.duracion_maxima_seleccion_segundos, ahora) is not None
        if not por_soltar and ahora - _ultima_conciliacion.get(booking_id, 0.0) < CONCILIAR_CADA_SEGUNDOS:
            continue
        await _conciliar_venta(booking_id)
    for booking_id in [b for b in _ultima_conciliacion if b not in activas]:
        del _ultima_conciliacion[booking_id]


async def _conciliar_venta(booking_id: str) -> bool:
    """
    Pregunta a la pasarela (servidor a servidor) por el cobro de esta venta y,
    si se pagó exactamente lo que calculó el servidor, emite con /confirmar,
    que es idempotente: si el navegador llega a la vez, se emite una sola vez.
    Un aviso del webhook nunca decide: solo adelanta esta consulta. True si se emitió.
    """
    _ultima_conciliacion[booking_id] = time.monotonic()
    if booking_id in _emisiones:
        return False
    try:
        pago = consultas.pago_de_booking(booking_id)
        if not pago or pago["estado"] != "pendiente":
            return False
        monto = (Decimal(pago["monto_centimos"]) / 100).quantize(Decimal("0.01"))
        tx_pagada, _, _, _ = await asyncio.to_thread(_buscar_pago, pago, monto)
    except Exception as e:
        log.info("Conciliador: no se pudo consultar el cobro de %s: %s", privacidad.booking(booking_id), e)
        return False
    if not tx_pagada:
        return False
    if pago.get("proveedor") == "niubiz":
        medio = "NIUBIZ"
    else:
        medio = "YUPY_QR" if yupy.api == "checkouts" else "YUPY"
    log.info("Conciliador: %s cobró la venta %s y el navegador no la confirmó; se emite.",
             medio, privacidad.booking(booking_id))
    registro.registrar_evento("pago_conciliado", booking_id=booking_id, detalle={"medio": medio})
    try:
        await v2_confirmar_venta(PeticionV2Confirmar(booking_id=booking_id, medio_pago=medio,
                                                     referencia_pago=tx_pagada))
        return True
    except HTTPException as e:
        # 409 = cobrado sin boleto: /confirmar ya dejó la venta y la incidencia crítica.
        log.info("Conciliador: /confirmar de %s respondió %s", privacidad.booking(booking_id), e.status_code)
    except Exception as e:
        log.warning("Conciliador: la emisión de %s falló: %s", privacidad.booking(booking_id), type(e).__name__)
    return False


async def _soltar_inactivas() -> None:
    """Una pasada de la limpieza: suelta las ventas abandonadas y deja constancia de cada una."""
    liberados = await pool_jelaf.liberar_inactivos(CONFIG.duracion_maxima_seleccion_segundos)
    for booking_id, motivo in liberados:
        await _registrar_venta_soltada(booking_id, motivo)


async def _registrar_venta_soltada(booking_id: str, motivo: str) -> None:
    """
    Deja constancia de una venta que soltó la limpieza (N56-SN). Si tenía un
    cobro abierto, el cliente pudo haber pagado: caso #147 (17/10/2026), pagó
    con Yape dentro del formulario de Niubiz, el aviso nunca llegó y quedó como
    "compra abandonada · leve". Con un cobro abierto:
      - la pasarela confirma el pago      → cobrado_sin_emitir (crítica)
      - Yupy confirma que no se pagó      → sesion_liberada (leve), dicho en el mensaje
      - Niubiz, o no se pudo saber        → pago_sin_confirmar (crítica): revisar el portal.
    Niubiz no tiene consulta de estado aquí (no se inventa el contrato).
    Nunca lanza: la limpieza sigue con las demás ventas.
    """
    try:
        pago = consultas.pago_de_booking(booking_id)
    except Exception as e:
        log.warning("[reaper] no se pudo leer el cobro de %s: %s", privacidad.booking(booking_id), type(e).__name__)
        pago = {"estado": "pendiente", "_ilegible": True}
    if pago and pago.get("estado") == "pendiente":
        await _soltada_con_cobro_abierto(booking_id, motivo, pago)
    else:
        registro.registrar_incidencia("sesion_liberada", f"Se liberó la venta: {motivo}.", booking_id=booking_id)
    registro.cerrar_sesion(booking_id, "abandonada")


async def _soltada_con_cobro_abierto(booking_id: str, motivo: str, pago: dict) -> None:
    sesion = _sesion_de_booking(booking_id)
    if pago.get("_ilegible"):
        registro.registrar_incidencia(
            "pago_sin_confirmar",
            f"Se soltó la venta ({motivo}) y no se pudo revisar si tenía un cobro abierto (falló la base). "
            "Revisar en el portal de Yupy y de Niubiz si el cliente pagó.",
            booking_id=booking_id, telefono=sesion.get("telefono"))
        return
    proveedor = pago.get("proveedor") or "yupy"
    nombre = "Niubiz" if proveedor == "niubiz" else "Yupy"
    monto = (Decimal(pago["monto_centimos"]) / 100).quantize(Decimal("0.01"))
    asientos = json.loads(pago.get("asientos") or "[]")
    contexto = {"proveedor": proveedor, "pedido": pago.get("pedido"),
                "referencia_pago": pago.get("yupy_transaction_id"), "monto": float(monto), "asientos": asientos}
    try:
        tx_pagada, motivo_pago, cobrado, _ = await asyncio.to_thread(_buscar_pago, pago, monto)
        # Cobró, pero no lo que correspondía (otro monto): no es "no se pagó".
        se_sabe = not cobrado
    except Exception as e:
        log.warning("[reaper] no se pudo consultar el cobro de %s: %s", privacidad.booking(booking_id), e)
        tx_pagada, motivo_pago, se_sabe = None, f"{nombre} no respondió", False

    if tx_pagada:
        _registrar_cobro_sin_boleto(
            booking_id, sesion, {a: {"precio": 0} for a in asientos},
            f"Se soltó la venta ({motivo}), pero {nombre} confirma el pago de S/ {monto} "
            f"(pedido {pago.get('pedido')}) y el navegador nunca volvió a confirmar. No se emitió.",
            estado_venta="cobrada_sin_emitir", referencia=tx_pagada, medio=proveedor.upper(), monto_cobrado=monto,
        )
        return
    reclamo = _reclamo_de(booking_id)
    if reclamo:
        # El cliente ya avisó que pagó (N60-SN): esa alerta crítica sigue abierta; no se duplica.
        registro.registrar_incidencia(
            "sesion_liberada",
            f"Se liberó la venta: {motivo}. Venció la retención por el reclamo de pago #{reclamo}: "
            "si el pago se confirma en el portal, emitir en otro asiento o devolver.",
            booking_id=booking_id, telefono=sesion.get("telefono"), contexto=contexto)
        return
    if proveedor == "yupy" and se_sabe:
        registro.registrar_incidencia(
            "sesion_liberada",
            f"Se liberó la venta: {motivo}. Tenía un cobro de Yupy abierto (pedido {pago.get('pedido')}); "
            f"Yupy confirma que no se pagó ({motivo_pago}).",
            booking_id=booking_id, telefono=sesion.get("telefono"), contexto=contexto)
        return
    registro.registrar_incidencia(
        "pago_sin_confirmar",
        f"Se soltó la venta ({motivo}) con un cobro de {nombre} abierto que nunca se confirmó "
        f"(pedido {pago.get('pedido')}, S/ {monto}; {motivo_pago}). El cliente pudo haber pagado "
        f"(por ejemplo, con Yape dentro del formulario): revisar el pedido en el portal de {nombre}.",
        booking_id=booking_id, telefono=sesion.get("telefono"), contexto=contexto)


def _estado_pool_para_paneles() -> dict:
    return {"workers": pool_jelaf.estado() if pool_jelaf else []}


async def _iniciar_cuenta(cliente: ClienteJelafHttp) -> None:
    """Login de una cuenta. Si falla, el bot sigue con las demás y queda la incidencia."""
    await cliente.iniciar()
    if not cliente.disponible:
        log.error("JELAF: la cuenta %s no pudo iniciar sesión; no recibirá ventas.", cliente.usuario)
        registro.registrar_incidencia("jelaf_caido", f"No se pudo iniciar sesión en JELAF con la cuenta {cliente.usuario}.")
    elif not CONFIG.simulacion and not cliente.lista_para_vender:
        log.error("JELAF: la cuenta %s entró sin datos del cajero/empresa: no podrá emitir.", cliente.usuario)
        registro.registrar_incidencia("jelaf_caido", f"Cuenta {cliente.usuario}: login sin datos del cajero o "
                                                     "de la empresa; no se puede emitir.")
    else:
        log.info("JELAF: cuenta %s con sesión (Terminal %s).", cliente.usuario,
                 (cliente.cajero or {}).get("Terminal"))


_ultimo_reintento_login = time.monotonic()


async def _reintentar_cuentas_sin_sesion() -> None:
    """Una cuenta que no pudo entrar (al arrancar o después) se reintenta cada 5 minutos, de a una."""
    global _ultimo_reintento_login
    if not isinstance(pool_jelaf, PoolCuentas) or (_logins_task and not _logins_task.done()):
        return
    if time.monotonic() - _ultimo_reintento_login < REINTENTO_CUENTAS_CAIDAS_SEGUNDOS:
        return
    _ultimo_reintento_login = time.monotonic()
    for cliente in pool_jelaf.clientes():
        if not cliente.disponible:
            await cliente.iniciar()
            log.info("JELAF: reintento de login de la cuenta %s: %s", cliente.usuario,
                     "con sesión" if cliente.disponible else "sigue sin sesión")
            await asyncio.sleep(PAUSA_ENTRE_LOGINS_SEGUNDOS)


async def _iniciar_cuentas_restantes(clientes: list[ClienteJelafHttp]) -> None:
    for cliente in clientes:
        await asyncio.sleep(PAUSA_ENTRE_LOGINS_SEGUNDOS)
        try:
            await _iniciar_cuenta(cliente)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("JELAF: login de la cuenta %s: %s", cliente.usuario, type(e).__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global jelaf, pool_jelaf, _reaper_task, _logins_task
    log.info("🚀 Iniciando backend TMT (ENTORNO=%s)", CONFIG.entorno)
    for aviso in CONFIG.avisos:
        log.warning("⚠️ %s", aviso)
    # Sin esta línea no había cómo saber, mirando el servidor, por qué no
    # aparece "Tarjeta" en la pantalla de pago.
    log.info("Medios de pago: Yupy %s, Niubiz %s.",
             "activo" if yupy.configurado else "INACTIVO (faltan YUPY_*)",
             f"activo ({CONFIG.niubiz_base_url})" if niubiz.configurado
             else "INACTIVO (faltan NIUBIZ_MERCHANT_ID/NIUBIZ_USUARIO/NIUBIZ_PASSWORD)")
    log.info("Boletos por correo: %s.", f"activo (desde {CONFIG.correo_remitente})" if _config_correo().configurado
             else "INACTIVO (faltan SMTP_HOST/SMTP_USUARIO/SMTP_PASSWORD)")

    paneles_db.init_db()
    planos.recargar()

    clientes = [ClienteJelafHttp(u, p) for u, p in CONFIG.jelaf_credenciales]
    pool_jelaf = PoolCuentas(clientes, ventas_por_cuenta=CONFIG.ventas_por_cuenta, simulacion=CONFIG.simulacion,
                             hoy=tiempo.hoy, reserva=CONFIG.jelaf_cuenta_reserva)
    jelaf = clientes[0]
    # La primera cuenta entra antes de abrir el backend (las consultas la usan);
    # las demás, de a una en segundo plano, y reciben ventas apenas tienen sesión.
    await _iniciar_cuenta(clientes[0])
    _logins_task = asyncio.create_task(_iniciar_cuentas_restantes(clientes[1:]))
    log.info("Vendedor HTTP listo: %d cuenta(s) de JELAF × %d venta(s) = hasta %d a la vez, modo %s.",
             len(clientes), CONFIG.ventas_por_cuenta, pool_jelaf.capacidad,
             "SIMULACIÓN" if CONFIG.simulacion else "REAL")

    configurar_paneles(estado_pool=_estado_pool_para_paneles)
    _reaper_task = asyncio.create_task(_reaper_pool_inactivo())

    yield

    log.info("🛑 Apagando el backend...")
    for tarea in (_reaper_task, _logins_task):
        if tarea:
            tarea.cancel()
    if pool_jelaf:
        await pool_jelaf.apagar_todos()


app = FastAPI(
    title="Vibe Transit API",
    lifespan=lifespan,
    # S8: en producción no se publica la lista de endpoints.
    docs_url=None if CONFIG.es_produccion else "/docs",
    redoc_url=None,
    openapi_url=None if CONFIG.es_produccion else "/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CONFIG.cors_origenes,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-Paneles-Token"],
)


@app.middleware("http")
async def cabeceras_seguridad(request: Request, call_next):
    respuesta = await call_next(request)
    respuesta.headers.setdefault("X-Content-Type-Options", "nosniff")
    respuesta.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    respuesta.headers.setdefault("X-Frame-Options", "DENY")
    return respuesta


_NOMBRES_CAMPO = {
    "nombres": "nombres", "paterno": "apellido paterno", "materno": "apellido materno",
    "documento": "número de documento", "tipo_documento": "tipo de documento", "sexo": "sexo",
    "fecha_nacimiento": "fecha de nacimiento", "fecha_viaje": "fecha del viaje", "fecha": "fecha",
    "hora_viaje": "hora del viaje", "numero_asiento": "asiento", "telefono": "celular",
    "booking_id": "compra", "origen": "origen", "destino": "destino",
    "ruc": "RUC", "razon_social": "razón social", "direccion": "dirección fiscal", "correo": "correo",
}


@app.exception_handler(RequestValidationError)
async def entrada_invalida(request: Request, exc: RequestValidationError):
    """
    N7: el cliente veía el JSON técnico de Pydantic ("string_pattern_mismatch").
    Ahora un mensaje en español con los campos a revisar; el código 422 y la
    lista `campos` se mantienen para el frontend. Nunca se devuelve el valor
    enviado (puede ser un DNI o un nombre).
    """
    partes = []
    for error in exc.errors():
        ubicacion = error.get("loc") or ()
        campo = _NOMBRES_CAMPO.get(str(ubicacion[-1]) if ubicacion else "", "datos")
        motivo = str(error.get("msg") or "")
        if error.get("type") == "value_error" and motivo.startswith("Value error, "):
            campo = f"{campo} ({motivo[len('Value error, '):]})"
        partes.append(campo)
    partes = list(dict.fromkeys(partes)) or ["datos"]
    return JSONResponse(status_code=422, content={
        "detail": "Revisa estos datos: " + ", ".join(partes) + ".",
        "campos": partes,
    })


app.include_router(router_paneles)
# Lo que el panel de Administración necesita saber de la venta, sin importar
# este módulo desde paneles/ (dependencia circular).
configurar_admin(
    pasarelas_configuradas=lambda: {"yupy": yupy.configurado, "niubiz": niubiz.configurado},
    ciudades_jelaf=lambda: set(CIUDADES_CODIGOS),
)
# Junto a este archivo, no según la carpeta desde donde se arrancó el proceso.
_DIR_PANELES_WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paneles_web")
if os.path.isdir(_DIR_PANELES_WEB):
    app.mount("/paneles", StaticFiles(directory=_DIR_PANELES_WEB, html=True), name="paneles_web")


# ==========================================
# 📦 MODELOS (validación en el borde)
# ==========================================
def _a_fecha(valor: str) -> date:
    """AAAA-MM-DD o DD/MM/AAAA → date. ValueError si la fecha no existe (31/02)."""
    if "-" in valor:
        return datetime.strptime(valor, "%Y-%m-%d").date()
    return datetime.strptime(valor, "%d/%m/%Y").date()


def _fecha_existente(valor: str) -> str:
    try:
        _a_fecha(valor)
    except ValueError:
        raise ValueError("esa fecha no existe") from None
    return valor


def _fecha_de_viaje(valor: str) -> str:
    """N11: ni fechas pasadas (hora de Lima) ni absurdamente lejanas."""
    fecha = _a_fecha(_fecha_existente(valor))
    hoy = tiempo.hoy()
    if fecha < hoy:
        raise ValueError("la fecha del viaje ya pasó")
    if fecha > hoy + timedelta(days=DIAS_VENTA_ANTICIPADA):
        raise ValueError("todavía no hay venta para esa fecha")
    return valor


EDAD_MAXIMA = 120  # pedido de boletería (2026-10-08): nadie mayor de 120 años


def _fecha_de_nacimiento(valor: str) -> str:
    """N8: JELAF recibía fechas futuras o imposibles tal cual."""
    fecha = _a_fecha(_fecha_existente(valor))
    hoy = tiempo.hoy()
    edad = hoy.year - fecha.year - ((hoy.month, hoy.day) < (fecha.month, fecha.day))
    if fecha > hoy or edad > EDAD_MAXIMA:
        raise ValueError("revisa la fecha de nacimiento")
    return valor


def _hora_legible(valor: str) -> str:
    if minutos_del_dia(valor) is None:
        raise ValueError("hora inválida")
    return valor


def _asiento_existente(valor: str) -> str:
    if int(valor) == 0:
        raise ValueError("el asiento 00 no existe")
    return valor


def _celular(valor: str) -> str:
    """
    Celular de contacto con código de país opcional (+51, +56, +591...): 8 a 15
    dígitos. Uno peruano tiene que ser 9 dígitos que empiezan con 9. A JELAF
    solo llega el peruano (whatsapp.celular_nacional): de su catálogo de países
    solo se conoce Perú y no se inventa el código de los demás.
    """
    limpio = re.sub(r"[\s-]", "", valor)
    if not limpio:
        return ""
    if not re.fullmatch(r"\+?\d{8,15}", limpio):
        raise ValueError("solo números, de 8 a 15 dígitos")
    if limpio.startswith("+51") and not re.fullmatch(r"\+519\d{8}", limpio):
        raise ValueError("en Perú son 9 dígitos que empiezan con 9")
    return limpio


BookingId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9-]{8,64}$")]
NumeroAsiento = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^\d{1,2}$"),
                          AfterValidator(_asiento_existente)]
_FechaTexto = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})$")]
Fecha = Annotated[_FechaTexto, AfterValidator(_fecha_existente)]
FechaViaje = Annotated[_FechaTexto, AfterValidator(_fecha_de_viaje)]
FechaNacimiento = Annotated[_FechaTexto, AfterValidator(_fecha_de_nacimiento)]
Hora = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=12,
                                        pattern=r"^[0-9: APMapm.]+$"), AfterValidator(_hora_legible)]
Documento = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z0-9]{4,15}$")]
Nombre = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60,
                                          pattern=r"^[\p{L} .'-]+$")]
NombreOpcional = Annotated[str, StringConstraints(strip_whitespace=True, max_length=60,
                                                  pattern=r"^[\p{L} .'-]*$")]
TextoCorto = Annotated[str, StringConstraints(strip_whitespace=True, max_length=60)]


def _ruc_sunat(valor: str) -> str:
    if not ruc_valido(valor):
        raise ValueError("revisa el número: el RUC no es válido")
    return valor


Ruc = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^\d{11}$"), AfterValidator(_ruc_sunat)]
# Mismos largos que los campos de la pantalla de JELAF (maxlength 100 y 200).
# Sin < > ni caracteres de control: va al comprobante y se muestra en pantallas.
RazonSocial = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100,
                                               pattern=r"^[^<>\x00-\x1f]+$")]
DireccionFiscal = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200,
                                                   pattern=r"^[^<>\x00-\x1f]*$")]


class PeticionConsultaRuc(BaseModel):
    booking_id: BookingId
    ruc: Ruc


def _correo_ok(valor: str) -> str:
    if not correo.correo_valido(valor):
        raise ValueError("revisa el correo")
    return valor


Correo = Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True, max_length=120),
                   AfterValidator(_correo_ok)]


class PeticionContacto(BaseModel):
    booking_id: BookingId
    correo: Correo


class PeticionComprobante(BaseModel):
    booking_id: BookingId
    tipo: Literal["boleta", "factura"]
    ruc: Ruc | None = None
    razon_social: RazonSocial | None = None
    direccion: DireccionFiscal = ""


class PeticionBusqueda(BaseModel):
    origen: TextoCorto
    destino: TextoCorto
    fecha: FechaViaje


class CodigosViaje(BaseModel):
    """Códigos del turno tal como los devolvió /buscar-viajes."""
    codi_empresa: int = Field(..., ge=0)
    codi_destino: int = Field(..., ge=0)
    codi_sucursal: int = Field(..., ge=0)
    codi_ruta: int = Field(..., ge=0)
    codi_punto_venta: int = Field(..., ge=0)
    codi_servicio: int = Field(..., ge=0)


class PeticionAsientos(CodigosViaje):
    codi_origen: int = Field(..., ge=0)
    fecha_viaje: Fecha
    hora_viaje: Hora
    placa_bus: TextoCorto


class PeticionConsultaCliente(BaseModel):
    booking_id: BookingId
    tipo_documento: Annotated[str, StringConstraints(pattern=r"^0[1-4]$")]
    documento: Documento


class PeticionV2Iniciar(BaseModel):
    booking_id: BookingId
    codi_origen: int = Field(..., ge=0)
    fecha_viaje: FechaViaje
    hora_viaje: Hora
    viaje: CodigosViaje
    destino: TextoCorto | None = None
    servicio: TextoCorto | None = None
    placa_bus: TextoCorto | None = None
    contacto: Annotated[str, StringConstraints(max_length=200)] | None = None


class PeticionV2Asiento(BaseModel):
    booking_id: BookingId
    numero_asiento: NumeroAsiento
    # Se acepta por compatibilidad con el frontend, pero se IGNORA: el precio lo
    # pone el servidor con datos de JELAF (V2).
    precio: float | None = None


class PeticionV2Pasajero(BaseModel):
    booking_id: BookingId
    numero_asiento: NumeroAsiento
    documento: Documento
    nombres: Nombre
    paterno: Nombre
    materno: NombreOpcional = ""
    fecha_nacimiento: FechaNacimiento
    sexo: Literal["M", "F"]
    # Códigos de la mini-webapp: 01 DNI, 02 pasaporte, 03 carné de extranjería.
    tipo_documento: Literal["01", "02", "03"] = "01"
    precio_venta: float | None = None   # ignorado (V2)
    telefono: Annotated[str, StringConstraints(max_length=20), AfterValidator(_celular)] | None = None


class PeticionV2BookingId(BaseModel):
    booking_id: BookingId


class PeticionV2Confirmar(PeticionV2BookingId):
    medio_pago: TextoCorto | None = None
    referencia_pago: Annotated[str, StringConstraints(max_length=100)] | None = None
    # Los descuentos aún no se aplican en la venta (decisión de negocio
    # pendiente, docs/PLAN_PANELES.md §7): el campo se acepta y se ignora.
    codigo_descuento: TextoCorto | None = None


class PeticionEstadoPago(PeticionV2BookingId):
    # Por qué pregunta la pantalla: "descartada" = el teléfono descartó la página
    # mientras el cliente estaba en otra app (document.wasDiscarded).
    contexto: Literal["regreso", "recarga", "descartada"] | None = None


class PeticionYaPague(PeticionV2BookingId):
    # El número de operación que muestra Yape/Plin: le sirve al equipo para
    # encontrar el pago en el portal de la pasarela. No prueba nada por sí solo.
    numero_operacion: Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[A-Za-z0-9-]{4,30}$")]


class PeticionPDF(BaseModel):
    booking_id: BookingId
    asiento: NumeroAsiento


# ==========================================
# 🔧 AUXILIARES
# ==========================================
def _jelaf_o_error() -> ClienteJelafHttp:
    """Cliente para consultas de lectura: la cuenta preferida o, si no tiene sesión, otra que sí."""
    candidatos = [jelaf]
    if isinstance(pool_jelaf, PoolCuentas):
        candidatos.append(pool_jelaf.cliente_lectura())
    for cliente in candidatos:
        if cliente is not None and cliente.disponible:
            return cliente
    raise HTTPException(status_code=503, detail="El sistema de pasajes no está disponible en este momento.")


def _pool_o_error() -> PoolCuentas | VendedorHttp:
    if not pool_jelaf:
        raise HTTPException(status_code=503, detail="El sistema de pasajes no está disponible en este momento.")
    return pool_jelaf


def _worker_de_booking(pool: VendedorHttp, booking_id: str):
    w = pool.worker_de(booking_id)
    if not w:
        raise HTTPException(
            status_code=403,
            detail="Esta compra ya no está activa (expiró por inactividad o nunca se inició).",
        )
    return w


@asynccontextmanager
async def _operar(w, booking_id: str):
    """
    Toda operación sobre un worker: bajo su lock y re-verificando DENTRO que la
    venta sigue siendo de este booking_id (C2). Mientras se esperaba el lock
    el worker pudo liberarse y pasar a otro cliente.
    """
    async with w.lock:
        if not w.es_dueno(booking_id):
            raise HTTPException(status_code=403, detail="Esta compra ya no está activa.")
        w.tocar_actividad()
        yield


def _monto_de(asientos: dict) -> Decimal:
    return sum((Decimal(info["precio"]) for info in asientos.values()), Decimal("0")).quantize(Decimal("0.01"))


async def _estado_turno(w, fresco: bool = False) -> EstadoTurno:
    """Plano real del turno de la venta (precios y ocupados), con caché corta."""
    if w.viaje is None:
        raise HTTPException(status_code=409, detail="Actualiza la página y vuelve a elegir tu viaje.")
    if not fresco and w.cache_turno and time.monotonic() - w.cache_turno[0] < CACHE_PRECIOS_SEGUNDOS:
        return w.cache_turno[1]
    estado = await asyncio.to_thread(_jelaf_o_error().consultar_turno, w.viaje)
    w.cache_turno = (time.monotonic(), estado)
    return estado


async def _calentar_precios(w, booking_id: str) -> None:
    """Adelanta la consulta de precios mientras el cliente mira el plano."""
    try:
        if w.es_dueno(booking_id):
            await _estado_turno(w)
    except Exception as e:
        log.info("No se pudieron adelantar los precios de %s: %s", privacidad.booking(booking_id), e)


async def _liberar(w, booking_id: str) -> None:
    """Libera el worker bajo su lock, solo si sigue siendo de esta venta."""
    async with w.lock:
        if w.es_dueno(booking_id):
            await w.liberar_y_resetear()


def _sesion_de_booking(booking_id: str) -> dict:
    try:
        fila = paneles_db.consultar_uno("SELECT * FROM sesion_compra WHERE booking_id = ?", (booking_id,))
        return dict(fila) if fila else {}
    except Exception as e:
        log.warning("No se pudo leer la sesión %s: %s", privacidad.booking(booking_id), e)
        return {}


def _registrar_venta(booking_id: str, sesion: dict, asientos: dict, *, estado: str,
                     nros: dict | None = None, links: dict | None = None,
                     medio_pago: str | None, referencia_pago: str | None,
                     monto_total: Decimal | None = None, facturacion: Facturacion | None = None,
                     correo_comprador: str | None = None, embarque: dict | None = None):
    pasajeros = [
        {
            "documento": info.get("documento"),
            "nombres": info.get("nombres"),
            "paterno": info.get("paterno"),
            "materno": info.get("materno"),
            "asiento": asiento,
            "precio": float(info.get("precio") or 0),
            "nro_boleto": (nros or {}).get(asiento),
            "link_comprobante": (links or {}).get(asiento),
        }
        for asiento, info in asientos.items()
    ]
    nro_venta = next((p["nro_boleto"] for p in pasajeros if p["nro_boleto"]), None)
    return registro.registrar_venta(
        booking_id=booking_id,
        nro_boleto=nro_venta,
        origen=sesion.get("origen") or "?",
        destino=sesion.get("destino") or "?",
        servicio=sesion.get("servicio"),
        placa_bus=sesion.get("placa_bus"),
        fecha_viaje=sesion.get("fecha_viaje"),
        hora_viaje=sesion.get("hora_viaje"),
        telefono=sesion.get("telefono"),
        pasajeros=pasajeros,
        monto_total=float(monto_total if monto_total is not None else _monto_de(asientos)),
        medio_pago=medio_pago,
        referencia_pago=referencia_pago,
        codigo_descuento=None,
        estado=estado,
        comprobante="factura" if facturacion else "boleta",
        ruc_cliente=facturacion.ruc if facturacion else None,
        razon_social=facturacion.razon_social.upper() if facturacion else None,
        direccion_fiscal=facturacion.direccion.upper() if facturacion else None,
        correo=correo_comprador,
        embarque_lugar=(embarque or {}).get("lugar"),
        embarque_hora=(embarque or {}).get("hora"),
    )


def _respuesta_boletos(boletos: list[dict], simulada: bool = False) -> dict:
    return {"status": "success", "data": {"boletos": [
        {
            "asiento": b.get("asiento"),
            "pasajero": " ".join(x for x in (b.get("nombres"), b.get("paterno")) if x),
            "documento": b.get("documento"),
            "precio": float(b.get("precio") or 0),
            "nro_boleto": b.get("nro_boleto"),
            "link_comprobante": b.get("link_comprobante"),
            "estado": "SIMULADO" if simulada else "EMITIDO",
        }
        for b in boletos
    ]}}


MENSAJE_COBRADO_SIN_BOLETO = (
    "Recibimos tu pago, pero no pudimos terminar de emitir tu boleto. No vuelvas a "
    "pagar: nuestro equipo ya fue avisado y te contactará por WhatsApp para resolverlo."
)


def _respuesta_previa(booking_id: str) -> dict | None:
    """
    Si esta venta ya se cerró, devuelve lo mismo que se respondió entonces
    (idempotencia de /confirmar). Si se cerró con problema, lanza el mismo error.
    """
    if booking_id in _emisiones:
        return _emisiones[booking_id]
    venta = consultas.venta_de_booking(booking_id)
    if not venta:
        return None
    if venta["estado"] in ("emitida", "simulada"):
        return _respuesta_boletos(venta["boletos"], simulada=venta["estado"] == "simulada")
    raise HTTPException(status_code=409, detail=MENSAJE_COBRADO_SIN_BOLETO)


def _transacciones(pago: dict) -> list[str]:
    """Todas las transacciones de Yupy de esta venta: la actual y las de checkouts anteriores."""
    todas = [pago["yupy_transaction_id"], *json.loads(pago.get("transacciones_anteriores") or "[]")]
    return list(dict.fromkeys(todas))


def _buscar_pago(pago: dict, monto: Decimal) -> tuple[str | None, str | None, bool, dict | None]:
    """
    Recorre las transacciones de la venta (bloqueante: correr en un hilo).
    Devuelve (transacción que pagó exactamente `monto` | None, motivo del
    rechazo, ¿alguna cobró dinero?, orden de Yupy que pagó). Lanza ErrorYupy
    si Yupy no responde.
    """
    motivo, cobrado = "no hay transacciones", False
    # Niubiz: la prueba del pago es la autorización que hizo NUESTRO servidor
    # (guardada en pago.autorizacion); no se vuelve a consultar.
    if pago.get("proveedor") == "niubiz":
        motivo = "Niubiz todavía no autorizó el pago"
        resumen = json.loads(pago.get("autorizacion") or "null")
        if isinstance(resumen, dict):
            respuesta = pagos_niubiz.reconstruir(resumen)
            problema = pagos_niubiz.evaluar_autorizacion(respuesta, pago["pedido"], monto)
            if problema is None:
                return pago["yupy_transaction_id"], None, True, {"_niubiz": resumen}
            motivo, cobrado = problema, pagos_niubiz.autorizada(respuesta)
    tokens = json.loads(pago.get("checkout_tokens") or "{}")
    for tx in _transacciones(pago):
        if tx.startswith(PREFIJO_NIUBIZ):
            continue
        orden = yupy.consultar_orden(tx, checkout_token=tokens.get(tx), monto_creado=monto)
        problema = evaluar_orden(orden, monto)
        if problema is None:
            return tx, None, True, orden
        motivo = problema
        cobrado = cobrado or pago_aprobado(orden)
    return None, motivo, cobrado, None


def _datos_pago_jelaf(orden: dict | None, tx: str) -> DatosPago:
    """
    Cómo se registra el pago en JELAF (decisión de la empresa): Yape → YAPE +
    4 últimos del nro. de operación; tarjeta → VISA/MASTERCARD + 4 últimos de
    la tarjeta. Si Yupy no dice el medio, se usa JELAF_TARJETA_DEFECTO y los 4
    últimos dígitos de la transacción de Yupy (así la venta se puede cruzar con
    el cobro).
    """
    if orden and "_niubiz" in orden:
        # Tarjeta por Niubiz: marca y 4 últimos dígitos de la autorización.
        medio, ultimos4 = pagos_niubiz.medio_tarjeta(orden["_niubiz"])
    else:
        medio, ultimos4 = medio_de_pago(orden or {})
    if not ultimos4:
        digitos = "".join(ch for ch in tx if ch.isdigit())
        ultimos4 = digitos[-4:] if digitos else "0000"
    codigo = TARJETAS.get(medio or CONFIG.jelaf_tarjeta_defecto, TARJETAS["visa"])
    return DatosPago(codi_tarjeta=codigo, ultimos4=ultimos4, extra={"medio_yupy": medio})


async def _conciliar(viaje: ViajeJelaf | None, asientos: dict) -> str:
    """
    Tras un fallo DESPUÉS de pulsar Aceptar, mira el plano real: ¿los asientos
    quedaron vendidos a estos documentos? (C4). Nunca asume.
      "emitida"      → todos los asientos tienen el documento esperado.
      "no_emitida"   → ningún asiento aparece ocupado.
      "por_conciliar"→ cualquier otra cosa, o no se pudo consultar.
    """
    if viaje is None or not jelaf:
        return "por_conciliar"
    try:
        estado = await asyncio.to_thread(jelaf.consultar_turno, viaje)
    except Exception as e:
        log.warning("No se pudo conciliar contra JELAF: %s", e)
        return "por_conciliar"
    coinciden = [a for a, info in asientos.items()
                 if info.get("documento") and estado.documentos.get(a) == info["documento"]]
    if len(coinciden) == len(asientos):
        return "emitida"
    if not any(a in estado.ocupados for a in asientos):
        return "no_emitida"
    return "por_conciliar"


def _registrar_cobro_sin_boleto(booking_id: str, sesion: dict, asientos: dict, motivo: str,
                                *, estado_venta: str, referencia: str | None, medio: str | None,
                                monto_cobrado: Decimal | None = None) -> None:
    _registrar_venta(booking_id, sesion, asientos, estado=estado_venta,
                     medio_pago=medio, referencia_pago=referencia, monto_total=monto_cobrado)
    registro.registrar_incidencia(
        "cobrado_sin_emitir" if estado_venta == "cobrada_sin_emitir" else "por_conciliar",
        motivo, booking_id=booking_id, telefono=sesion.get("telefono"),
        contexto={"asientos": list(asientos.keys()), "referencia_pago": referencia},
    )
    registro.marcar_pago_usado(booking_id)


# ==========================================
# 🔎 CATÁLOGO (solo lectura en JELAF)
# ==========================================
def _asientos_libres(v: dict) -> int:
    """
    lista-itinerarios no trae AsientosLibres (llegaba siempre 0); sí trae
    CapacidadBus y AsientosVendidos, que es lo que usa el bot de itinerarios.
    """
    if v.get("AsientosLibres") not in (None, ""):
        return int(v.get("AsientosLibres") or 0)
    try:
        return max(0, int(v.get("CapacidadBus") or 0) - int(v.get("AsientosVendidos") or 0))
    except (TypeError, ValueError):
        return 0


def _precio_desde(v: dict) -> float:
    """
    "Desde S/ …" del catálogo (solo para mostrar: el cobro usa el precio de
    cada asiento). JELAF no manda PrecioBase. NO se usan Precio_N1/Precio_N2:
    en la captura del 03/10 decían 70/70 para un bus con asientos de 60, 70 y
    80, y en lista-itinerarios vienen en 0. Sin dato, 0 y la pantalla muestra
    su estimación "(aprox.)".
    """
    try:
        valor = float(v.get("PrecioBase") or 0)
    except (TypeError, ValueError):
        return 0
    return valor if valor > 0 else 0


# "Desde S/ …" real del catálogo: el precio más bajo entre los asientos LIBRES
# del plano del tramo (/itinerarios/turnos con el origen y destino del
# pasajero: comprobado el 2026-10-13, Lima→Ica 130/140 frente a Lima→Tacna
# 180/190, igual que el tarifario de JELAF). lista-itinerarios no trae precio
# y la pantalla mostraba una tabla fija ("Desde (aprox.) S/ 60" para Lima→Ica).
_cache_desde: dict[tuple, tuple[float, tuple[Decimal, bool]]] = {}
# Incidente 2026-10-14 19:40: en producción todas las consultas de lectura usan
# UNA sesión de JELAF y JELAF atiende las de una misma sesión de a una. Abrir
# los planos de todos los horarios en paralelo armó una cola que tapó también
# /plano-bus de los clientes ("Failed to fetch"). Ahora los planos del "desde"
# se piden de a UNO en un hilo de fondo (nunca más de una consulta extra a la
# vez), la búsqueda responde al instante con lo ya guardado y la pantalla va
# llenando los precios (/api/v1/precios-desde). Encendido con
# PRECIO_DESDE_PLANO=1; con 0 la pantalla dice "al elegir tu asiento".
PRECIO_DESDE_PLANO = os.getenv("PRECIO_DESDE_PLANO", "0") == "1"
CACHE_DESDE_SEGUNDOS = 300          # el "desde" de un turno no cambia en minutos
REINTENTO_DESDE_SEGUNDOS = 60       # un plano que falló no se vuelve a pedir enseguida
MAX_COLA_DESDE = 60                 # sin lugar en la cola, ese horario sale "al elegir asiento"
PAUSA_DESDE_SEGUNDOS = 0.3          # respiro para las consultas de los clientes entre plano y plano
_MAX_CACHE_DESDE = 2000
_cola_desde: "collections.OrderedDict[tuple, ViajeJelaf]" = collections.OrderedDict()
_fallos_desde: dict[tuple, float] = {}
_cond_desde = threading.Condition()
_desde_en_curso: list = [None]
_hilo_desde: threading.Thread | None = None


def _desde_del_turno(estado: EstadoTurno) -> tuple[Decimal, bool]:
    """
    (precio "desde", ¿cerrado?). Cerrado: JELAF lo marca (Xcierre) o el plano no
    trae ningún precio (2026-10-14: turnos cerrados llegaban así y ningún
    asiento se podía vender). Un bus lleno NO es cerrado: sus asientos vendidos
    sí traen precio.
    """
    if estado.cerrado or not any(p > 0 for p in estado.precios.values()):
        return Decimal("0"), True
    libres = [p for a, p in estado.precios.items() if a not in estado.ocupados and p > 0]
    return (min(libres) if libres else Decimal("0")), False


def _clave_desde(prog, codi_origen, codi_destino, fecha_jelaf: str) -> tuple:
    return int(prog or 0), int(codi_origen or 0), int(codi_destino or 0), fecha_jelaf


def _desde_guardado(clave: tuple) -> tuple[Decimal, bool] | None:
    guardado = _cache_desde.get(clave)
    if guardado and time.monotonic() - guardado[0] < CACHE_DESDE_SEGUNDOS:
        return guardado[1]
    return None


def _desde_pendiente(clave: tuple) -> bool:
    with _cond_desde:
        return clave in _cola_desde or _desde_en_curso[0] == clave


def _encolar_desde(clave: tuple, viaje: ViajeJelaf) -> bool:
    """Pone el plano en la cola (sin repetir). True si quedó pendiente."""
    global _hilo_desde
    with _cond_desde:
        if clave in _cola_desde or _desde_en_curso[0] == clave:
            return True
        if time.monotonic() - _fallos_desde.get(clave, -1e9) < REINTENTO_DESDE_SEGUNDOS:
            return False
        if len(_cola_desde) >= MAX_COLA_DESDE:
            return False
        _cola_desde[clave] = viaje
        if _hilo_desde is None or not _hilo_desde.is_alive():
            _hilo_desde = threading.Thread(target=_trabajar_desde, name="precios-desde", daemon=True)
            _hilo_desde.start()
        _cond_desde.notify_all()
        return True


def _trabajar_desde() -> None:
    """Hilo de fondo: un plano a la vez, con una pausa entre uno y otro. Nunca termina por un error."""
    while True:
        with _cond_desde:
            while not _cola_desde:
                _cond_desde.wait(timeout=60)
            clave, viaje = _cola_desde.popitem(last=False)
            _desde_en_curso[0] = clave
        try:
            precio = _desde_del_turno(_jelaf_o_error().consultar_turno(viaje))
            if len(_cache_desde) > _MAX_CACHE_DESDE:
                _cache_desde.clear()
            _cache_desde[clave] = (time.monotonic(), precio)
        except Exception as e:   # sin precio no pasa nada: la pantalla dice "al elegir tu asiento"
            _fallos_desde[clave] = time.monotonic()
            log.info("Precio desde de %s: %s", clave[0], type(e).__name__)
        finally:
            with _cond_desde:
                _desde_en_curso[0] = None
                _cond_desde.notify_all()
        time.sleep(PAUSA_DESDE_SEGUNDOS)


def _esperar_desde(timeout: float) -> bool:
    """True cuando la cola quedó vacía (lo usan los tests)."""
    with _cond_desde:
        return _cond_desde.wait_for(lambda: not _cola_desde and _desde_en_curso[0] is None, timeout=timeout)


def _viaje_de_listado(v: dict, fecha_jelaf: str) -> ViajeJelaf:
    return ViajeJelaf(
        int(v.get("CodiEmpresa") or 1), int(v.get("CodiOrigen") or 0), int(v.get("CodiDestino") or 0),
        int(v.get("CodiSucursal") or 0), int(v.get("CodiRuta") or 0), int(v.get("CodiPuntoVenta") or 0),
        int(v.get("CodiServicio") or 0), fecha_jelaf, v.get("HoraPartida") or v.get("HoraSalida") or "")


def _motivo_cierre(v: dict) -> str | None:
    """
    Turno cerrado según el listado de JELAF, igual que su pantalla de venta
    (codigo_jelaf/itinerarios.html, sendRow): ProgramacionCerrada '1' = "La ruta
    está bloqueada para esta fecha", '2' = "...desde esta fecha hacia adelante";
    PlanoCerradoWeb = plano cerrado para la venta web. None = abierto.
    """
    cerrada = str(v.get("ProgramacionCerrada") or "0").strip()
    if cerrada == "1":
        return "Turno bloqueado por la empresa para esta fecha."
    if cerrada == "2":
        return "Ruta bloqueada por la empresa desde esta fecha."
    if v.get("PlanoCerradoWeb") in (True, "1", 1, "true", "True"):
        return "Turno cerrado para la venta en línea."
    return None


def _ya_salio(v: dict, corte_minutos: int | None) -> bool:
    if corte_minutos is None:
        return False
    salida = minutos_del_dia(v.get("HoraPartida") or v.get("HoraSalida"))
    return salida is not None and salida < corte_minutos


def _ciudades_cerradas() -> set[str]:
    """
    Ciudades que la empresa sacó de la venta desde el panel (Administración).
    Si la base del panel falla, ninguna: los paneles nunca tumban una venta
    (regla 5); queda en el log.
    """
    try:
        return set(consultas.ciudades_cerradas())
    except Exception as e:
        log.warning("No se pudo leer las ciudades cerradas del panel (se vende sin ese filtro): %s", type(e).__name__)
        return set()


def _exigir_ciudades_abiertas(*ciudades: str | None) -> None:
    cerradas = _ciudades_cerradas()
    for ciudad in ciudades:
        if ciudad in cerradas:
            nombre = rutas.SEDES.get(ciudad, {}).get("nombre", ciudad.title())
            raise HTTPException(status_code=400, detail=f"Por ahora no vendemos pasajes en línea desde ni hacia {nombre}.")


@app.get("/api/v1/rutas")
def listar_rutas():
    """Sedes, destinos posibles desde cada una (tramos del Excel, rutas.py) y duraciones."""
    return {"status": "success", "data": rutas.catalogo(set(CIUDADES_CODIGOS), _ciudades_cerradas())}


def _consulta_turnos(req: PeticionBusqueda):
    """
    Turnos de JELAF para una búsqueda y la función que dice por qué se
    descartaría cada uno (None = se ofrece). La usan /buscar-viajes y su
    diagnóstico: los dos filtran exactamente igual.
    """
    origen, destino = req.origen.upper(), req.destino.upper()
    if not rutas.tramo_valido(origen, destino):
        raise HTTPException(status_code=400, detail="No tenemos viajes entre esas ciudades.")
    # El buscador ya no las ofrece, pero la petición puede armarse a mano.
    _exigir_ciudades_abiertas(origen, destino)
    codi_origen = CIUDADES_CODIGOS.get(origen)
    # Entre las ciudades de siempre se busca como antes (todos los destinos y
    # filtro por nombre). Un tramo con una escala (Ica, Moquegua...) se busca
    # con su destino, como boletería: así JELAF devuelve los turnos que paran
    # ahí, con los códigos del tramo y no los de la ruta completa.
    tramo = not (origen in rutas.TERMINALES and destino in rutas.TERMINALES)
    codi_destino = CIUDADES_CODIGOS.get(destino) if tramo else 0
    if codi_origen is None or codi_destino is None:
        raise HTTPException(status_code=400, detail="Esa ciudad todavía no está habilitada para la venta en línea.")
    cliente = _jelaf_o_error()
    hora = tiempo.ahora_dt().strftime("%I:%M %p").lstrip("0")
    try:
        viajes = cliente.buscar_itinerarios(codi_origen, a_fecha_jelaf(req.fecha), hora, codi_destino)
    except ErrorJelaf as e:
        log.warning("buscar-viajes: %s", e)
        raise HTTPException(status_code=502, detail="No pudimos consultar los viajes. Inténtalo de nuevo.")

    placas_con_plano = planos.placas_configuradas()
    # Hoy no se ofrecen los viajes que ya salieron o están por salir.
    corte = None
    if _a_fecha(req.fecha) == tiempo.hoy():
        ahora = tiempo.ahora_dt()
        corte = ahora.hour * 60 + ahora.minute + MINUTOS_CIERRE_VENTA

    def descarte(v: dict) -> str | None:
        # En un tramo solo sirve el turno cuyo CodiDestino es el del tramo: si
        # JELAF devolviera el de la ruta completa, se venderían (y cobrarían)
        # pasajes hasta el final de la ruta. Y el bus tiene que ir en ese
        # sentido: JELAF también devuelve el que pasa por las dos paradas al
        # revés (Moquegua→Puno trae el bus Cusco→Tacna).
        if tramo:
            if str(v.get("CodiDestino")) != str(codi_destino):
                return f"destino: JELAF lo devolvió con CodiDestino {v.get('CodiDestino')!r} y el tramo es {codi_destino}"
            if not rutas.va_en_sentido(origen, destino, v.get("NomRuta")):
                return f"sentido: el bus termina en {v.get('NomRuta')!r}, que no lleva de {origen} a {destino}"
        elif destino not in str(v.get("NomDestino", "")).upper():
            return f"destino: el bus va a {v.get('NomDestino')!r}"
        if planos.normalizar_placa(v.get("PlacaBus", "")) not in placas_con_plano:
            return f"plano: el bus {v.get('PlacaBus')!r} no tiene plano configurado"
        if _ya_salio(v, corte):
            return f"hora: sale en menos de {MINUTOS_CIERRE_VENTA} min o ya salió"
        return None

    consulta = {"origen": origen, "destino": destino, "fecha": req.fecha, "tramo": tramo,
                "codi_origen": codi_origen, "codi_destino": codi_destino, "hora_enviada": hora}
    return consulta, viajes, descarte, cliente


@app.post("/api/v2/diagnostico/buscar-viajes")
def diagnostico_buscar_viajes(req: PeticionBusqueda, x_paneles_token: str | None = Header(None)):
    """
    Qué devolvió JELAF para una búsqueda y por qué se ofrece o se descarta
    cada turno (2026-10-14: Ica→Lima daba 0 y JELAF tenía 6). Solo datos del
    itinerario, sin pasajeros. Protegido con la clave del panel.
    """
    exigir_token(x_paneles_token)
    consulta, viajes, descarte, _ = _consulta_turnos(req)
    campos = ("CodiProgramacion", "HoraPartida", "PlacaBus", "NomServicio", "CodiOrigen", "NomOrigen",
              "CodiDestino", "NomDestino", "CodiRuta", "NomRuta", "ProgramacionCerrada", "PlanoCerradoWeb")
    return {"consulta": consulta,
            "turnos": [{**{c: v.get(c) for c in campos}, "descarte": descarte(v)} for v in viajes]}


@app.post("/api/v1/buscar-viajes")
def buscar_viajes(req: PeticionBusqueda):
    consulta, viajes, descarte, cliente = _consulta_turnos(req)
    motivos = [descarte(v) for v in viajes]   # uno por turno (no por CodiProgramacion: podría repetirse o faltar)
    elegibles = [v for v, m in zip(viajes, motivos) if m is None]
    if viajes and not elegibles:
        log.info("buscar-viajes %s→%s %s: JELAF dio %d turno(s), todos descartados: %s", consulta["origen"],
                 consulta["destino"], req.fecha, len(viajes), sorted({str(m).split(":")[0] for m in motivos}))
    fecha_jelaf = a_fecha_jelaf(req.fecha)
    info: list[tuple[tuple[Decimal, bool] | None, str | None, bool]] = []
    for v in elegibles:
        cierre = _motivo_cierre(v)
        clave = _clave_desde(v.get("CodiProgramacion"), v.get("CodiOrigen"), v.get("CodiDestino"), fecha_jelaf)
        guardado = _desde_guardado(clave) if not cierre else None
        pendiente = False
        if PRECIO_DESDE_PLANO and not cierre and guardado is None:
            pendiente = _encolar_desde(clave, _viaje_de_listado(v, fecha_jelaf))
        if guardado and guardado[1]:
            cierre = "Turno cerrado para la venta."
        info.append((guardado, cierre, pendiente))
    res = [
        {
            "id_programacion": v.get("CodiProgramacion"),
            "nro_viaje": v.get("NroViaje", 0),
            "codi_empresa": v.get("CodiEmpresa", 1),
            "codi_origen": v.get("CodiOrigen", 0),
            "codi_destino": v.get("CodiDestino", 0),
            "codi_sucursal": v.get("CodiSucursal", 0),
            "codi_ruta": v.get("CodiRuta", 0),
            "codi_punto_venta": v.get("CodiPuntoVenta", 0),
            "codi_servicio": v.get("CodiServicio", 0),
            "servicio": v.get("NomServicio", "Bus Cama"),
            "fecha_partida": v.get("FechaPartida", req.fecha),
            "hora_partida": v.get("HoraPartida") or v.get("HoraSalida", "—"),
            "placa": v.get("PlacaBus") or "Por asignar",
            "asientos_libres": _asientos_libres(v),
            # Del plano del tramo (como boletería), nunca el PrecioBase del listado: no siempre
            # es el del tramo (Tacna→Moquegua decía 30 y JELAF cobra 25/40). 0 = "al elegir tu asiento".
            "precio_base": float(guardado[0]) if guardado and not guardado[1] else 0,
            # Todavía en la cola: la pantalla lo pide a /precios-desde y lo va llenando.
            "precio_pendiente": pendiente,
            # Turno que JELAF tiene cerrado: se muestra con candado y no se puede elegir.
            "bloqueado": cierre is not None,
            "motivo_bloqueo": cierre,
        }
        for v, (guardado, cierre, pendiente) in zip(elegibles, info)
    ]
    return {"status": "success", "data": res}


class ViajeParaPrecio(BaseModel):
    id_programacion: int = Field(..., ge=0)
    codi_origen: int = Field(..., ge=0)
    codi_destino: int = Field(..., ge=0)


class PeticionPreciosDesde(BaseModel):
    fecha: FechaViaje
    viajes: Annotated[list[ViajeParaPrecio], Field(max_length=40)]


_limite_precios_desde = Limitador(maximo=120, ventana_segundos=60)


@app.post("/api/v1/precios-desde")
def precios_desde(req: PeticionPreciosDesde, request: Request):
    """
    Los "desde" que el hilo de fondo ya calculó (la pantalla de horarios pregunta
    cada 1,5 s y va llenando las tarjetas). Solo lee lo guardado: nunca consulta
    JELAF, así no suma carga a su sesión.
    """
    _limite_precios_desde.exigir(ip_cliente(request))
    fecha_jelaf = a_fecha_jelaf(req.fecha)
    precios = {}
    for v in req.viajes:
        clave = _clave_desde(v.id_programacion, v.codi_origen, v.codi_destino, fecha_jelaf)
        guardado = _desde_guardado(clave)
        cerrado = bool(guardado and guardado[1])
        precios[str(v.id_programacion)] = {
            "precio_base": float(guardado[0]) if guardado and not cerrado else 0.0,
            "bloqueado": cerrado,
            "motivo_bloqueo": "Turno cerrado para la venta." if cerrado else None,
            "pendiente": guardado is None and _desde_pendiente(clave),
        }
    return {"status": "success", "precios": precios}


@app.post("/api/v1/plano-bus")
def obtener_plano_bus(req: PeticionAsientos):
    cliente = _jelaf_o_error()
    pisos = planos.plano_por_placa(req.placa_bus)
    if not pisos:
        raise HTTPException(status_code=404, detail="Este bus todavía no tiene plano configurado.")
    viaje = ViajeJelaf(req.codi_empresa, req.codi_origen, req.codi_destino, req.codi_sucursal,
                       req.codi_ruta, req.codi_punto_venta, req.codi_servicio,
                       a_fecha_jelaf(req.fecha_viaje), req.hora_viaje)
    try:
        estado = cliente.consultar_turno(viaje)
    except ErrorJelaf as e:
        log.warning("plano-bus: %s", e)
        raise HTTPException(status_code=502, detail="No pudimos cargar los asientos. Inténtalo de nuevo.")
    if _desde_del_turno(estado)[1]:
        raise HTTPException(status_code=409, detail="Este turno está cerrado para la venta. Elige otro horario.")
    return {"status": "success", "data": planos.aplicar_estado(pisos, estado)}


@app.post("/api/v1/consultar-cliente")
def consultar_cliente(req: PeticionConsultaCliente, request: Request):
    """
    Autocompleta nombre/apellidos/fecha de nacimiento desde la base de
    clientes de JELAF. S1: solo para una venta activa (booking_id con worker
    asignado) y con límite de consultas por venta y por IP; así no sirve para
    recorrer DNIs y armarse una base de datos personales. Ya no devuelve el
    sexo (el formulario no lo necesita para vender).
    """
    _limite_cliente_ip.exigir(ip_cliente(request))
    pool = _pool_o_error()
    if not pool.worker_de(req.booking_id):
        raise HTTPException(status_code=403, detail="Esta compra ya no está activa.")
    _limite_cliente_venta.exigir(req.booking_id)

    try:
        datos = _jelaf_o_error().consultar_usuario(tipo_doc_jelaf(req.tipo_documento), req.documento)
    except DatosIncompletos:
        return {"status": "success", "encontrado": False}
    except ErrorJelaf as e:
        log.info("consultar-cliente: %s", e)
        return {"status": "success", "encontrado": False}

    valor = datos.get("Valor") or {}
    nombres = str(valor.get("NombreCliente") or "").strip()
    paterno = str(valor.get("ApellidoPaterno") or "").strip()
    materno = str(valor.get("ApellidoMaterno") or "").strip()
    # "12345678" es el documento genérico de JELAF para ventas sin documento:
    # su registro dice "RESERVA DE" / "RESERVA" (visto en vivo el 03/10) y no
    # debe autocompletarse como un nombre real.
    es_placeholder = req.documento == "12345678" or (
        nombres.upper().startswith("RESERVA") and paterno.upper().startswith("RESERVA"))
    if not datos.get("EsCorrecto") or not nombres or es_placeholder:
        return {"status": "success", "encontrado": False}

    fecha_iso = ""
    partes = str(valor.get("FechaNacimiento") or "").strip().split("/")
    if len(partes) == 3 and all(p.isdigit() for p in partes):
        fecha_iso = f"{partes[2]}-{partes[1].zfill(2)}-{partes[0].zfill(2)}"
    return {"status": "success", "encontrado": True, "nombres": nombres, "paterno": paterno,
            "materno": materno, "fecha_nacimiento": fecha_iso}


@app.post("/api/v2/venta/consultar-ruc")
def v2_consultar_ruc(req: PeticionConsultaRuc, request: Request):
    """
    Razón social y dirección del RUC, como boletería al dar Enter en el RUC
    (consultar-empresa). Mismas protecciones que consultar-cliente (S1): solo
    con una venta activa y con límite por venta y por IP. Si JELAF no lo
    conoce, el cliente escribe la razón social (igual que en boletería).
    """
    _limite_cliente_ip.exigir(ip_cliente(request))
    pool = _pool_o_error()
    if not pool.worker_de(req.booking_id):
        raise HTTPException(status_code=403, detail="Esta compra ya no está activa.")
    _limite_cliente_venta.exigir(req.booking_id)
    try:
        datos = _jelaf_o_error().consultar_empresa(req.ruc)
    except ErrorJelaf as e:
        log.info("consultar-ruc: %s", e)
        return {"status": "success", "encontrado": False}
    valor = datos.get("Valor") if datos.get("EsCorrecto") else None
    razon = str((valor or {}).get("RazonSocial") or "").strip() if isinstance(valor, dict) else ""
    if not razon:
        return {"status": "success", "encontrado": False}
    return {"status": "success", "encontrado": True, "razon_social": razon[:100],
            "direccion": str(valor.get("Direccion") or "").strip()[:200]}


@app.post("/api/v2/venta/contacto")
async def v2_contacto_venta(req: PeticionContacto):
    """Correo adonde se mandan los boletos (obligatorio para pagar en producción)."""
    w = _worker_de_booking(_pool_o_error(), req.booking_id)
    async with _operar(w, req.booking_id):
        w.correo = req.correo
    return {"status": "success", "correo": req.correo}


@app.post("/api/v2/venta/comprobante")
async def v2_comprobante(req: PeticionComprobante):
    """
    Boleta (por defecto) o factura. Con factura, cada asiento sale como una
    factura al mismo RUC (decisión de la empresa, 2026-10-13). Se elige antes
    de pagar; la ficha del pasajero se vuelve a grabar con el RUC.
    """
    facturacion = None
    if req.tipo == "factura":
        if not req.ruc or not req.razon_social:
            raise HTTPException(status_code=422, detail="Para la factura faltan el RUC y la razón social.")
        facturacion = Facturacion(ruc=req.ruc, razon_social=req.razon_social, direccion=req.direccion)
    w = _worker_de_booking(_pool_o_error(), req.booking_id)
    async with _operar(w, req.booking_id):
        try:
            w.definir_comprobante(facturacion)
        except DatosIncompletos as e:
            raise HTTPException(status_code=409, detail=str(e))
    registro.registrar_evento("comprobante_elegido", booking_id=req.booking_id, detalle={"tipo": req.tipo})
    return {"status": "success", "tipo": req.tipo}


# ==========================================
# 🎟️ VENTA — por HTTP, varias a la vez (vendedor_http.py)
# ==========================================
@app.post("/api/v2/venta/iniciar")
async def v2_iniciar_venta(req: PeticionV2Iniciar, request: Request):
    """
    Reserva un worker libre (o el que ya tenía este booking_id) y lanza EN
    SEGUNDO PLANO la búsqueda real del viaje en JELAF (prefetch, ver
    docs/architecture.md). Con todos los workers ocupados espera unos
    segundos y, si sigue igual, responde 423.
    """
    _limite_iniciar.exigir(ip_cliente(request))
    pool = _pool_o_error()

    telefono = whatsapp.verificar_contacto(req.contacto, CONFIG.enlace_secreto)
    if CONFIG.exigir_contacto and not telefono:
        # S3: sin enlace de WhatsApp válido no se reserva el único mostrador.
        raise HTTPException(status_code=403, detail="Abre el enlace de compra que te enviamos por WhatsApp.")
    origen = CIUDADES_POR_CODIGO.get(req.codi_origen)
    if pool.worker_de(req.booking_id) is None:
        # Una compra nueva no empieza en una ciudad cerrada desde el panel; la
        # que ya estaba en curso (esta llamada se repite al tocar el primer
        # asiento) sigue hasta el final.
        destino = (req.destino or "").strip().upper()
        _exigir_ciudades_abiertas(origen, CIUDADES_POR_CODIGO.get(req.viaje.codi_destino),
                                  destino if destino in rutas.SEDES else None)
    fecha_jelaf = a_fecha_jelaf(req.fecha_viaje)
    viaje = ViajeJelaf(req.viaje.codi_empresa, req.codi_origen, req.viaje.codi_destino,
                       req.viaje.codi_sucursal, req.viaje.codi_ruta, req.viaje.codi_punto_venta,
                       req.viaje.codi_servicio, fecha_jelaf, req.hora_viaje)

    w = await pool.reservar_con_espera(req.booking_id, ESPERA_MOSTRADOR_SEGUNDOS)
    if not w:
        registro.abrir_sesion(req.booking_id, telefono)
        registro.registrar_incidencia(
            "pool_ocupado", f"Se alcanzó el máximo de {pool.capacidad} compras simultáneas.",
            booking_id=req.booking_id, telefono=telefono,
            contexto={"origen": origen, "fecha_viaje": req.fecha_viaje, "hora_viaje": req.hora_viaje},
        )
        registro.cerrar_sesion(req.booking_id, "rechazada")
        raise HTTPException(
            status_code=423,
            detail="En este momento estamos atendiendo otra(s) compra(s). Intenta de nuevo en unos segundos.",
        )

    if w.viaje is not None and w.viaje != viaje and w.asientos_en_venta:
        raise HTTPException(status_code=409,
                            detail="Ya tienes asientos elegidos en otro viaje. Cancela esa compra para cambiar de viaje.")
    w.viaje, w.destino, w.placa = viaje, req.destino, req.placa_bus

    registro.abrir_sesion(req.booking_id, telefono)
    registro.reabrir_sesion(req.booking_id)
    registro.actualizar_viaje(
        req.booking_id, origen=origen, destino=req.destino,
        fecha_viaje=req.fecha_viaje, hora_viaje=req.hora_viaje,
        servicio=req.servicio, placa_bus=req.placa_bus,
    )
    registro.registrar_evento(
        "viaje_elegido", booking_id=req.booking_id, telefono=telefono,
        detalle={"origen": origen, "destino": req.destino, "servicio": req.servicio},
    )

    w.lanzar_busqueda_en_fondo(req.booking_id, req.codi_origen, fecha_jelaf, req.hora_viaje)
    _en_segundo_plano(_calentar_precios(w, req.booking_id))
    return {"status": "success", "mensaje": "Preparando tu viaje..."}


@app.post("/api/v2/venta/seleccionar-asiento")
async def v2_seleccionar_asiento(req: PeticionV2Asiento):
    pool = _pool_o_error()
    w = _worker_de_booking(pool, req.booking_id)
    asiento = req.numero_asiento.zfill(2)

    # Instantáneo si el prefetch de /iniciar ya terminó (caso normal).
    try:
        await w.esperar_viaje_listo()
    except Exception as e:
        await _liberar(w, req.booking_id)
        registro.registrar_incidencia("jelaf_caido", f"No se pudo abrir el viaje en JELAF: {e}",
                                      booking_id=req.booking_id)
        registro.cerrar_sesion(req.booking_id, "fallida")
        raise HTTPException(status_code=502, detail="No pudimos abrir tu viaje. Inténtalo de nuevo.")

    # V2: el precio sale de JELAF, no de la petición.
    try:
        estado = await _estado_turno(w)
    except ErrorJelaf as e:
        log.warning("precio de %s: %s", privacidad.booking(req.booking_id), e)
        raise HTTPException(status_code=502, detail="No pudimos obtener el precio del asiento. Inténtalo de nuevo.")
    if estado.cerrado:
        # JELAF cerró la programación (Xcierre): boletería tampoco puede vender.
        raise HTTPException(status_code=409, detail="Este turno está cerrado para la venta. Elige otro horario.")
    # Cada asiento trae su propio precio en el plano (captura del 03/10: en un
    # mismo piso hay asientos de S/ 60 y de S/ 70). El precio "general" del
    # turno sale de OTRO asiento: no se usa para cobrar.
    precio = estado.precios.get(asiento) or Decimal("0")
    if precio <= 0:
        registro.registrar_incidencia("asiento_sin_precio", f"JELAF no dio precio para el asiento {asiento}.",
                                      booking_id=req.booking_id, contexto={"asiento": asiento})
        raise HTTPException(status_code=409, detail="No pudimos obtener el precio de ese asiento. Elige otro.")

    async with _operar(w, req.booking_id):
        if asiento not in w.asientos_en_venta and len(w.asientos_en_venta) >= MAX_ASIENTOS_POR_COMPRA:
            raise HTTPException(status_code=409,
                                detail=f"Puedes elegir hasta {MAX_ASIENTOS_POR_COMPRA} asientos por compra.")
        try:
            await w.seleccionar_asiento(asiento, precio)
        except JelafAsientoNoDisponibleError as e:
            registro.registrar_incidencia("asiento_no_disponible", str(e), booking_id=req.booking_id,
                                          contexto={"asiento": asiento})
            raise HTTPException(status_code=409, detail="Ese asiento ya no está disponible. Elige otro.")
        except ErrorJelaf as e:
            log.warning("bloquear asiento %s: %s", privacidad.booking(req.booking_id), e)
            raise HTTPException(status_code=502, detail="No pudimos apartar el asiento. Inténtalo de nuevo.")
        registro.registrar_evento("asiento_tocado", booking_id=req.booking_id,
                                  detalle={"asiento": asiento, "precio": float(precio)})
        registro.actualizar_asientos(req.booking_id, list(w.asientos_en_venta.keys()))
        return {"status": "success", "precio": float(precio),
                "asientos_en_venta": list(w.asientos_en_venta.keys())}


@app.post("/api/v2/venta/liberar-asiento")
async def v2_liberar_asiento(req: PeticionV2Asiento):
    pool = _pool_o_error()
    w = _worker_de_booking(pool, req.booking_id)
    async with _operar(w, req.booking_id):
        if w.pago_pendiente_hasta > time.monotonic():
            raise HTTPException(status_code=409, detail="Tu pago está en curso: ya no se pueden cambiar los asientos.")
        await w.deseleccionar_asiento(req.numero_asiento.zfill(2))
        return {"status": "success", "asientos_en_venta": list(w.asientos_en_venta.keys())}


@app.post("/api/v2/venta/pasajero")
async def v2_llenar_pasajero(req: PeticionV2Pasajero):
    """Llena el pasajero de UN asiento ya seleccionado, en cualquier orden."""
    pool = _pool_o_error()
    w = _worker_de_booking(pool, req.booking_id)
    asiento = req.numero_asiento.zfill(2)

    async with _operar(w, req.booking_id):
        info = w.asientos_en_venta.get(asiento)
        if not info:
            raise HTTPException(status_code=409, detail=f"El asiento {asiento} no está en tu compra.")
        try:
            await w.llenar_pasajero(
                asiento, req.documento, req.nombres.upper(), req.paterno.upper(), req.materno.upper(),
                a_fecha_jelaf(req.fecha_nacimiento), float(info["precio"]),
                whatsapp.celular_nacional(req.telefono),
                sexo=req.sexo, tipo_documento=req.tipo_documento,
            )
        except DatosIncompletos as e:
            raise HTTPException(status_code=422, detail=str(e))
        except JelafAsientoNoDisponibleError as e:
            registro.registrar_incidencia("asiento_no_disponible", str(e), booking_id=req.booking_id,
                                          contexto={"asiento": asiento})
            raise HTTPException(status_code=409, detail="Ese asiento ya no está disponible. Vuelve a elegirlo.")
        registro.registrar_evento("pasajero_lleno", booking_id=req.booking_id, detalle={"asiento": asiento})
        return {"status": "success"}


@app.post("/api/v2/venta/monto")
async def v2_monto(req: PeticionV2BookingId):
    """Total a pagar calculado por el SERVIDOR (reemplaza al viejo /pre-checkout, V2)."""
    pool = _pool_o_error()
    w = _worker_de_booking(pool, req.booking_id)
    asientos = dict(w.asientos_en_venta)
    if not asientos:
        raise HTTPException(status_code=409, detail="Tu compra no tiene asientos.")
    faltan = [a for a, i in asientos.items() if not i.get("pasajero_cargado")]
    return {
        "status": "success",
        "monto_total": float(_monto_de(asientos)),
        "moneda": "PEN",
        "asientos": [{"asiento": a, "precio": float(i["precio"])} for a, i in asientos.items()],
        "pasajeros_completos": not faltan,
        "medios_pago": _medios_pago(req.booking_id),
    }


def _pasarelas_apagadas() -> set[str]:
    """Medios de pago apagados desde el panel. Si la base falla, ninguno (regla 5)."""
    try:
        return set(consultas.pasarelas_apagadas())
    except Exception as e:
        log.warning("No se pudo leer las pasarelas apagadas del panel (quedan como en el .env): %s", type(e).__name__)
        return set()


def _medios_pago(booking_id: str) -> dict:
    """
    Medios que se ofrecen: configurados en el .env y no apagados desde el
    panel. Si esta venta ya abrió un cobro con un medio que después se apagó,
    ese medio se le sigue mostrando: al volver a la pantalla tiene que poder
    llegar a "ya pagado" y recibir su boleto.
    """
    apagadas = _pasarelas_apagadas()
    if apagadas:
        try:
            previo = consultas.pago_de_booking(booking_id)
        except Exception as e:
            log.warning("No se pudo leer el cobro previo de %s: %s", privacidad.booking(booking_id), type(e).__name__)
            previo = None
        if previo and previo["estado"] == "pendiente":
            apagadas.discard(previo.get("proveedor") or "yupy")
    return {"yupy": yupy.configurado and "yupy" not in apagadas,
            "niubiz": niubiz.configurado and "niubiz" not in apagadas}


async def _cobro_previo_pagado(booking_id: str) -> dict | None:
    """
    Si un cobro anterior de esta venta (Yupy o Niubiz) ya se pagó, devuelve
    la respuesta "ya_pagado" para que el frontend pase directo a /confirmar
    en vez de abrir un segundo cobro.
    """
    previo = consultas.pago_de_booking(booking_id)
    if not previo or previo["estado"] != "pendiente":
        return None
    monto_previo = Decimal(previo["monto_centimos"]) / 100
    try:
        tx_pagada, _, _, _ = await asyncio.to_thread(_buscar_pago, previo, monto_previo)
    except ErrorYupy:
        tx_pagada = None
    if not tx_pagada:
        return None
    return {"status": "ya_pagado", "yupy_transaction_id": tx_pagada, "referencia_pago": tx_pagada,
            "monto_total": float(monto_previo), "pedido": previo["pedido"]}


async def _preparar_cobro(w, booking_id: str, vida_segundos: int) -> tuple[dict, Decimal, str]:
    """
    Asientos, monto (del SERVIDOR) y comprador para abrir un cobro. Graba
    antes la ficha de los pasajeros en JELAF (como boletería al pasar al
    pago): si JELAF la rechaza, mejor enterarse ANTES de cobrar. Desde aquí
    el reaper no suelta la venta mientras el cobro siga vivo (N4).
    """
    async with _operar(w, booking_id):
        asientos = dict(w.asientos_en_venta)
        if not asientos or any(not i.get("pasajero_cargado") for i in asientos.values()):
            raise HTTPException(status_code=409, detail="Completa los datos de todos los pasajeros antes de pagar.")
        monto = _monto_de(asientos)
        if monto <= 0:
            raise HTTPException(status_code=409, detail="No pudimos calcular el monto de tu compra.")
        if CONFIG.exigir_correo and not w.correo:
            # Los boletos solo llegan por correo (WhatsApp no deja descargar): sin correo no se cobra.
            raise HTTPException(status_code=409, detail="Escribe tu correo para recibir tus boletos.")
        primero = next(iter(asientos.values()))
        comprador = f"{primero.get('nombres') or ''} {primero.get('paterno') or ''}".strip() or "Cliente"
        try:
            await w.preparar_pago()
        except (ErrorJelaf, DatosIncompletos) as e:
            registro.registrar_incidencia("error_interno", f"JELAF no aceptó los datos de los pasajeros: {e}",
                                          booking_id=booking_id)
            raise HTTPException(status_code=502, detail="No pudimos registrar los datos de los pasajeros. Revísalos e inténtalo de nuevo.")
        w.en_fase_pago = True
        w.pago_pendiente_hasta = time.monotonic() + vida_segundos + MARGEN_CHECKOUT_SEGUNDOS
    return asientos, monto, comprador


@app.post("/api/v2/venta/checkout")
async def v2_checkout(req: PeticionV2BookingId):
    """
    Crea el cobro en Yupy por el monto que calcula el servidor y lo guarda en
    la base (tabla `pago`) para verificarlo en /confirmar. El navegador no
    decide el monto (V1/V2) y un reinicio no pierde el pago (N5).
    """
    if not yupy.configurado:
        raise HTTPException(status_code=503, detail="El pago en línea no está disponible en este momento.")
    pool = _pool_o_error()
    w = _worker_de_booking(pool, req.booking_id)

    # Si un cobro anterior de esta venta ya se pagó (el cliente recargó la
    # pantalla), no se crea otro: el frontend pasa directo a /confirmar.
    ya_pagado = await _cobro_previo_pagado(req.booking_id)
    if ya_pagado:
        return ya_pagado
    # Apagada desde el panel: no se abren cobros nuevos (el de arriba, ya
    # pagado, igual se entrega).
    if "yupy" in _pasarelas_apagadas():
        raise HTTPException(status_code=503, detail="El pago con QR no está disponible en este momento.")

    asientos, monto, comprador = await _preparar_cobro(w, req.booking_id, CONFIG.yupy_checkout_segundos)

    pedido = f"TMT-{uuid.uuid4().hex[:16]}"
    try:
        orden = await asyncio.to_thread(yupy.crear_checkout, pedido, monto, comprador, CONFIG.yupy_checkout_segundos)
    except ErrorYupy as e:
        w.pago_pendiente_hasta = 0.0
        registro.registrar_incidencia("pago_rechazado", f"No se pudo generar el checkout de Yupy: {e}",
                                      booking_id=req.booking_id, contexto={"monto": float(monto)})
        raise HTTPException(status_code=502, detail="No pudimos conectar con la pasarela de pago. Inténtalo de nuevo.")

    tx = orden["yupy_transaction_id"]
    token_checkout = orden.get("checkout_token")
    # El modo que decide si se emite de verdad es el del vendedor (VENTA_MODO).
    if (yupy.api == "checkouts" and not pool.simulacion and not CONFIG.yupy_permitir_simulacion
            and await asyncio.to_thread(yupy.simulacion_habilitada, token_checkout)):
        # La credencial de Yupy permite "Simular pago", y un pago simulado es
        # idéntico a uno real (docs de Yupy): en venta real, cualquiera se
        # llevaría un boleto sin pagar. Falla cerrado (CLAUDE.md, regla 9).
        await asyncio.to_thread(yupy.cancelar_checkout, token_checkout)
        w.pago_pendiente_hasta = 0.0
        registro.registrar_incidencia(
            "yupy_simulacion_habilitada",
            "La credencial de Yupy permite simular pagos: se bloqueó el cobro en venta real. "
            "Usar una credencial sin payments:simulate (o VENTA_MODO=simulacion para probar).",
            booking_id=req.booking_id, severidad="critica")
        raise HTTPException(status_code=503, detail="El pago con QR no está disponible en este momento.")
    centimos = int(monto * 100)
    if registro.guardar_pago(req.booking_id, tx, pedido, centimos, list(asientos.keys()),
                             checkout_token=token_checkout) is not True:
        # Sin el registro no habría cómo verificar el pago después: no se
        # entrega el checkout (mejor que el cliente no pueda pagar a que pague
        # y no pueda recibir su boleto).
        w.pago_pendiente_hasta = 0.0
        raise HTTPException(status_code=503, detail="No pudimos preparar tu pago. Inténtalo de nuevo en unos segundos.")

    registro.registrar_evento("pago_iniciado", booking_id=req.booking_id,
                              detalle={"monto": float(monto), "pedido": pedido})
    sesion_checkout = orden.get("checkout_session") or {}
    return {
        "status": "success",
        "yupy_transaction_id": tx,
        "pedido": pedido,
        "monto_total": float(monto),
        "checkout_session": {"checkout_url": sesion_checkout.get("checkout_url")},
        # Qué SDK de Yupy carga el navegador (YUPY_API, ver pagos_yupy.py).
        "modo": yupy.api,
        "sdk_url": yupy.sdk_url,
    }

@app.post("/api/v1/yupy-webhook")
async def recibir_webhook_yupy(
    request: Request,
    x_yupy_signature: str = Header(None),
    x_yupy_timestamp: str = Header(None)
):
    if not x_yupy_signature or not x_yupy_timestamp:
        raise HTTPException(status_code=400, detail="Bad Request")

    cuerpo = await request.body()
    mensaje_a_firmar = x_yupy_timestamp.encode('utf-8') + b'.' + cuerpo
    
    firma_calculada = hmac.new(
        YUPY_WEBHOOK_SECRET.encode('utf-8'),
        mensaje_a_firmar,
        hashlib.sha256
    ).hexdigest()

    firma_limpia = x_yupy_signature.replace("v1=", "")

    if not hmac.compare_digest(firma_limpia, firma_calculada):
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        payload = await request.json()
    except ValueError:
        return Response(content="Bad Request", status_code=400)

    origen = payload.get("origen", "")
    
    if origen == "web_checkout":
        orden_id = payload.get("orden_id")
        estado = payload.get("estado")
        log.info(f"YUPY Web Checkout: {orden_id} - {estado}")
    elif origen == "pos":
        log.info("YUPY POS")

    return Response(content="OK", status_code=200)

# ==========================================
# 💳 NIUBIZ (pago con tarjeta)
# ==========================================
def _url_pantalla_pago() -> str:
    """pago.html del frontend, derivada de URL_FRONTEND (nunca de la petición: sin redirección abierta)."""
    return urljoin(CONFIG.url_frontend, "pago.html")


def _volver_a_pago(resultado: str) -> RedirectResponse:
    # Solo un código fijo en la URL; el detalle del rechazo queda en paneles.
    return RedirectResponse(url=f"{_url_pantalla_pago()}?niubiz={resultado}", status_code=303)


@app.post("/api/v2/venta/niubiz/sesion")
async def v2_niubiz_sesion(req: PeticionV2BookingId, request: Request):
    """
    Abre el formulario de tarjeta de Niubiz por el monto que calcula el
    servidor. Devuelve lo que checkout.js necesita; el cargo recién ocurre en
    /niubiz/autorizar, y lo hace este servidor.
    """
    if not niubiz.configurado:
        raise HTTPException(status_code=503, detail="El pago con tarjeta no está disponible en este momento.")
    pool = _pool_o_error()
    w = _worker_de_booking(pool, req.booking_id)

    ya_pagado = await _cobro_previo_pagado(req.booking_id)
    if ya_pagado:
        return ya_pagado
    # Apagada desde el panel: no se abren formularios nuevos. /autorizar no se
    # corta: el cliente que ya escribió su tarjeta termina su pago.
    if "niubiz" in _pasarelas_apagadas():
        raise HTTPException(status_code=503, detail="El pago con tarjeta no está disponible en este momento.")

    asientos, monto, _ = await _preparar_cobro(w, req.booking_id, CONFIG.niubiz_sesion_segundos)
    # Número de pedido de Niubiz: solo dígitos (12), distinto en cada intento.
    pedido = str(10**11 + secrets.randbelow(9 * 10**11))
    try:
        session_key = await asyncio.to_thread(niubiz.crear_sesion, monto, ip_cliente(request), pedido)
    except ErrorNiubiz as e:
        w.pago_pendiente_hasta = 0.0
        # También en consola: la incidencia queda en la base, pero sin el panel abierto nadie la veía.
        log.warning("Niubiz: no se pudo abrir la sesión de %s: %s", privacidad.booking(req.booking_id), e)
        registro.registrar_incidencia("pago_rechazado", f"No se pudo abrir la sesión de Niubiz: {e}",
                                      booking_id=req.booking_id, contexto={"monto": float(monto)})
        raise HTTPException(status_code=502, detail="No pudimos conectar con la pasarela de pago. Inténtalo de nuevo.")

    referencia = f"{PREFIJO_NIUBIZ}{pedido}"
    if registro.guardar_pago(req.booking_id, referencia, pedido, int(monto * 100), list(asientos.keys()),
                             proveedor="niubiz") is not True:
        w.pago_pendiente_hasta = 0.0
        raise HTTPException(status_code=503, detail="No pudimos preparar tu pago. Inténtalo de nuevo en unos segundos.")

    registro.registrar_evento("pago_iniciado", booking_id=req.booking_id,
                              detalle={"monto": float(monto), "pedido": pedido, "medio": "niubiz"})
    return {
        "status": "success",
        "session_key": session_key,
        "merchant_id": niubiz.merchant_id,
        "pedido": pedido,
        "referencia_pago": referencia,
        "monto_total": float(monto),
        "checkout_js": CONFIG.niubiz_checkout_js,
        # Ruta (relativa a la API) a la que checkout.js envía el transactionToken.
        "action": f"/api/v2/venta/niubiz/autorizar?booking_id={req.booking_id}&pedido={pedido}",
    }


@app.post("/api/v2/venta/niubiz/autorizar")
async def v2_niubiz_autorizar(
    request: Request,
    booking_id: Annotated[str, Query(pattern=r"^[A-Za-z0-9-]{8,64}$")],
    pedido: Annotated[str, Query(pattern=r"^\d{6,12}$")],
):
    """
    Destino del formulario de checkout.js (POST x-www-form-urlencoded con
    `transactionToken`). Aquí se hace el CARGO, una sola vez por pedido, con
    el monto guardado en `pago`: nunca con lo que venga en la URL o el
    formulario. Redirige a pago.html con ?niubiz=ok|denegado|vencido|error, y
    pago.html llama a /confirmar como con Yupy.
    """
    # Una redirección, nunca un JSON 429: es el navegador del cliente que vuelve de pagar.
    if not _limite_niubiz.permitir(ip_cliente(request)):
        _aviso_niubiz_descartado(booking_id, pedido, "limite_peticiones", [])
        return _volver_a_pago("error")
    cuerpo = await request.body()
    if len(cuerpo) > 8192:
        _aviso_niubiz_descartado(booking_id, pedido, "cuerpo_grande", [])
        return _volver_a_pago("error")
    formulario = parse_qs(cuerpo.decode("utf-8", "replace"), keep_blank_values=True)
    token = (formulario.get("transactionToken") or [""])[0].strip()
    if not token or len(token) > 512:
        _aviso_niubiz_descartado(booking_id, pedido, "sin_token", list(formulario))
        return _volver_a_pago("error")
    referencia = f"{PREFIJO_NIUBIZ}{pedido}"

    async with _lock_niubiz:
        pago = consultas.pago_de_booking(booking_id)
        if (not pago or pago.get("proveedor") != "niubiz" or pago["yupy_transaction_id"] != referencia
                or pago["estado"] != "pendiente"):
            # Pedido vencido o reemplazado por otro intento: NO se autoriza, así
            # que Niubiz no cobra nada con este token.
            _aviso_niubiz_descartado(booking_id, pedido, "pedido_no_vigente", list(formulario))
            return _volver_a_pago("vencido")
        if pago.get("autorizacion"):
            # Doble envío del formulario: se responde lo mismo que la primera vez.
            previa = pagos_niubiz.reconstruir(json.loads(pago["autorizacion"]))
            return _volver_a_pago("ok" if pagos_niubiz.autorizada(previa) else "denegado")

        monto = (Decimal(pago["monto_centimos"]) / 100).quantize(Decimal("0.01"))
        sesion = _sesion_de_booking(booking_id)
        try:
            respuesta = await asyncio.to_thread(niubiz.autorizar, token, pedido, monto, _url_pantalla_pago())
        except ErrorNiubiz as e:
            # No sabemos si cobró: queda para conciliar en el portal de Niubiz.
            log.error("Niubiz: sin respuesta al autorizar el pedido %s (%s): %s",
                      pedido, privacidad.booking(booking_id), e)
            registro.registrar_incidencia(
                "por_conciliar", f"Niubiz no respondió al autorizar el pedido {pedido}: {e}. "
                "Revisar en el portal de Niubiz si se cobró.",
                booking_id=booking_id, telefono=sesion.get("telefono"), severidad="critica",
                contexto={"referencia_pago": referencia, "monto": float(monto)},
            )
            return _volver_a_pago("error")

        resumen = pagos_niubiz.resumen_autorizacion(respuesta)
        guardado = registro.guardar_autorizacion_niubiz(booking_id, referencia, resumen)
        problema = pagos_niubiz.evaluar_autorizacion(respuesta, pedido, monto)

    if problema is None:
        if guardado is not True:
            registro.registrar_incidencia(
                "cobrado_sin_emitir", f"Niubiz cobró el pedido {pedido} pero no se pudo guardar la autorización.",
                booking_id=booking_id, telefono=sesion.get("telefono"), severidad="critica",
                contexto={"referencia_pago": referencia, "autorizacion": resumen},
            )
        w = pool_jelaf.worker_de(booking_id) if pool_jelaf else None
        if w:
            w.tocar_actividad()
            w.pago_pendiente_hasta = max(w.pago_pendiente_hasta, time.monotonic() + 5 * 60)
        registro.registrar_evento("pago_confirmado", booking_id=booking_id,
                                  detalle={"medio": "niubiz", "pedido": pedido})
        _en_segundo_plano(_rescatar_emision(booking_id, "NIUBIZ"))
        return _volver_a_pago("ok")

    # Solo estado y códigos de Niubiz (`problema`); nunca datos de la tarjeta.
    log.warning("Niubiz: pedido %s (%s) no aprobado: %s", pedido, privacidad.booking(booking_id), problema)
    if pagos_niubiz.autorizada(respuesta):
        # Cobró, pero no lo que correspondía: no se emite y se revisa a mano.
        registro.registrar_incidencia("pago_no_verificado", f"Niubiz: {problema}.", booking_id=booking_id,
                                      telefono=sesion.get("telefono"), severidad="critica",
                                      contexto={"referencia_pago": referencia, "autorizacion": resumen})
    else:
        registro.registrar_incidencia("pago_rechazado", f"Niubiz rechazó la tarjeta: {problema}.",
                                      booking_id=booking_id, contexto={"referencia_pago": referencia})
    return _volver_a_pago("denegado")


_MOTIVOS_AVISO_NIUBIZ = {
    "limite_peticiones": "se superó el límite de avisos por IP",
    "cuerpo_grande": "el formulario superó 8 KB",
    "sin_token": "el formulario no trajo transactionToken (o era demasiado largo)",
    "pedido_no_vigente": "el pedido ya no era el vigente (el cliente abrió otro intento)",
}


def _aviso_niubiz_descartado(booking_id: str, pedido: str, motivo: str, campos: list[str]) -> None:
    """
    N57-SN: un aviso de Niubiz que no se autoriza no puede desaparecer sin
    rastro (caso #147: pagó con Yape dentro del formulario y no quedó nada).
    Siempre va al log; al panel, solo si booking_id + pedido son un cobro
    Niubiz real de esa compra (nadie llena el panel inventando URLs) y una vez
    por compra, pedido y motivo. De los campos del formulario se guardan solo
    los NOMBRES: si Yape manda otro campo en vez de transactionToken, se verá.
    """
    # Los nombres los manda el navegador: sin saltos de línea ni símbolos raros (van al log).
    nombres = sorted({re.sub(r"[^\w.-]", "?", str(c))[:40] for c in campos})[:20]
    log.warning("niubiz/autorizar %s, pedido %s: aviso descartado (%s); campos: %s",
                privacidad.booking(booking_id), pedido, motivo, nombres)
    try:
        pago = consultas.pago_de_booking(booking_id)
        if not pago or f"{PREFIJO_NIUBIZ}{pedido}" not in _transacciones(pago):
            return
        repetido = paneles_db.consultar_uno(
            "SELECT 1 FROM incidencia WHERE booking_id = ? AND tipo = 'niubiz_aviso_descartado' "
            "AND json_extract(contexto, '$.pedido') = ? AND json_extract(contexto, '$.motivo') = ?",
            (booking_id, pedido, motivo))
        if repetido:
            return
    except Exception as e:
        log.warning("niubiz/autorizar: no se pudo revisar el cobro de %s: %s",
                    privacidad.booking(booking_id), type(e).__name__)
        return
    monto = (Decimal(pago["monto_centimos"]) / 100).quantize(Decimal("0.01"))
    no_vigente = motivo == "pedido_no_vigente"
    registro.registrar_incidencia(
        "niubiz_aviso_descartado",
        f"Llegó un aviso de Niubiz del pedido {pedido} (S/ {monto}) y no se autorizó: "
        f"{_MOTIVOS_AVISO_NIUBIZ.get(motivo, motivo)}. Revisar el pedido en el portal de Niubiz: "
        + ("con tarjeta no se cobró nada, pero con Yape el dinero pudo haber salido."
           if no_vigente else "el cliente pudo haber pagado."),
        booking_id=booking_id, telefono=_sesion_de_booking(booking_id).get("telefono"),
        severidad="grave" if no_vigente else None,
        contexto={"pedido": pedido, "motivo": motivo, "campos": nombres, "monto": float(monto)},
    )


async def _rescatar_emision(booking_id: str, medio: str) -> None:
    """
    Tras un cargo con tarjeta, si el navegador no vuelve a llamar a
    /confirmar (cerró la pestaña, se quedó sin señal), se emite igual: el
    cliente ya pagó. /confirmar es idempotente: si el navegador sí llegó,
    esto solo recibe la misma respuesta.
    """
    await asyncio.sleep(RESCATE_NIUBIZ_SEGUNDOS)
    if booking_id in _emisiones or consultas.venta_de_booking(booking_id):
        return
    try:
        await v2_confirmar_venta(PeticionV2Confirmar(booking_id=booking_id, medio_pago=medio))
        log.info("Emisión rescatada para %s (el navegador no volvió).", privacidad.booking(booking_id))
    except HTTPException as e:
        log.info("Rescate de emisión %s: %s", privacidad.booking(booking_id), e.status_code)
    except Exception as e:
        log.warning("Rescate de emisión %s falló: %s", privacidad.booking(booking_id), type(e).__name__)


@app.post("/api/v2/venta/confirmar")
async def v2_confirmar_venta(req: PeticionV2Confirmar):
    """
    Emite el boleto en JELAF, SOLO si Yupy confirma que se cobró exactamente el
    monto calculado por el servidor para estos asientos.

    Exactamente una vez: si ya se emitió (o ya se registró un cobro sin
    boleto), devuelve lo mismo que la primera vez en vez de reintentar.
    """
    previa = _respuesta_previa(req.booking_id)
    if previa:
        return previa

    pago = consultas.pago_de_booking(req.booking_id)
    if not pago:
        raise HTTPException(status_code=402, detail="No encontramos un pago iniciado para esta compra.")
    if req.referencia_pago and req.referencia_pago not in _transacciones(pago):
        registro.registrar_incidencia("pago_no_verificado", "La referencia de pago no corresponde a esta compra.",
                                      booking_id=req.booking_id)
        raise HTTPException(status_code=402, detail="La referencia de pago no corresponde a esta compra.")

    monto_pagado = (Decimal(pago["monto_centimos"]) / 100).quantize(Decimal("0.01"))
    try:
        tx_pagada, motivo, cobrado, orden_pagada = await asyncio.to_thread(_buscar_pago, pago, monto_pagado)
    except ErrorYupy as e:
        log.warning("confirmar %s: %s", privacidad.booking(req.booking_id), e)
        raise HTTPException(status_code=503, detail="Todavía no pudimos confirmar tu pago. Espera unos segundos y reintenta.")

    sesion = _sesion_de_booking(req.booking_id)
    if not tx_pagada:
        # El motivo trae el estado y los montos que informó Yupy (sin datos
        # personales): es lo que hace falta para ajustar la verificación.
        log.warning("confirmar %s: pago no verificado: %s", privacidad.booking(req.booking_id), motivo)
        registro.registrar_incidencia(
            "pago_no_verificado", f"Yupy: {motivo}.", booking_id=req.booking_id,
            # Sin dinero cobrado es normal (el frontend reintenta mientras Yupy
            # concilia): leve. Con dinero cobrado por otro monto: crítica.
            telefono=sesion.get("telefono"), severidad="critica" if cobrado else "leve",
            contexto={"transacciones": _transacciones(pago)},
        )
        raise HTTPException(status_code=402, detail="Tu pago todavía no figura como aprobado.")
    pago = {**pago, "yupy_transaction_id": tx_pagada}   # la que efectivamente cobró
    # Forma de la respuesta de Yupy (solo nombres de campos): sirve para
    # confirmar dónde informa Yupy el medio de pago (docs/venta-sin-navegador.md).
    log.info("Orden de Yupy (forma): %s", esquema_orden(orden_pagada or {}))

    pool = _pool_o_error()
    w = pool.worker_de(req.booking_id)
    if not w:
        # N4: cobrado, pero la venta ya se había soltado. Nunca en silencio.
        return _cobrado_sin_venta_activa(req, sesion, pago)

    async with w.lock:
        if not w.es_dueno(req.booking_id):
            # C2/N3: otra llamada terminó la venta mientras esta esperaba el lock.
            previa = _respuesta_previa(req.booking_id)
            if previa:
                return previa
            return _cobrado_sin_venta_activa(req, sesion, pago)
        w.tocar_actividad()

        asientos = dict(w.asientos_en_venta)
        if _monto_de(asientos) != monto_pagado or sorted(asientos) != json.loads(pago["asientos"]):
            # Se cobró por otros asientos (el cliente los cambió después de abrir el pago).
            _registrar_cobro_sin_boleto(
                req.booking_id, sesion, asientos,
                f"Se cobró S/ {monto_pagado} por {json.loads(pago['asientos'])}, pero la compra tiene "
                f"{sorted(asientos)} por S/ {_monto_de(asientos)}. No se emitió.",
                estado_venta="cobrada_sin_emitir", referencia=pago["yupy_transaction_id"], medio=req.medio_pago,
                monto_cobrado=monto_pagado,
            )
            await w.liberar_y_resetear()
            raise HTTPException(status_code=409, detail=MENSAJE_COBRADO_SIN_BOLETO)

        w.datos_pago = _datos_pago_jelaf(orden_pagada, tx_pagada)
        try:
            resultado = await w.pagar_y_confirmar()
        except Exception as e:
            fase = w.fase_emision
            estado = await _conciliar(w.viaje, asientos) if fase == "aceptado" else "no_emitida"
            log.error("Emisión fallida %s (fase=%s, conciliación=%s): %s",
                      privacidad.booking(req.booking_id), fase, estado, e)
            if estado == "emitida":
                _registrar_venta(req.booking_id, sesion, asientos, estado="emitida",
                                 medio_pago=req.medio_pago, referencia_pago=pago["yupy_transaction_id"],
                                 facturacion=w.facturacion, correo_comprador=w.correo)
                registro.registrar_incidencia(
                    "emision_conciliada",
                    f"JELAF emitió (verificado en el plano) pero el bot falló al leer la confirmación: {e}. "
                    "Buscar el número de boleto en JELAF.",
                    booking_id=req.booking_id, telefono=sesion.get("telefono"),
                )
                registro.marcar_pago_usado(req.booking_id)
                respuesta = _respuesta_boletos([{**i, "asiento": a} for a, i in asientos.items()])
                _recordar_emision(req.booking_id, respuesta)
                await w.liberar_y_resetear()
                return respuesta
            _registrar_cobro_sin_boleto(
                req.booking_id, sesion, asientos,
                f"El pago se aprobó y JELAF falló al emitir ({type(e).__name__}: {e}).",
                estado_venta="cobrada_sin_emitir" if estado == "no_emitida" else "por_conciliar",
                referencia=pago["yupy_transaction_id"], medio=req.medio_pago,
            )
            await w.liberar_y_resetear()
            raise HTTPException(status_code=409, detail=MENSAJE_COBRADO_SIN_BOLETO)

        nros = {b["asiento"]: b["nro_boleto"] for b in resultado["boletos"]}
        links = {b["asiento"]: b.get("link_comprobante") for b in resultado["boletos"]}
        simulada = bool(resultado.get("simulado"))
        _registrar_venta(req.booking_id, sesion, asientos, estado="simulada" if simulada else "emitida",
                         nros=nros, links=links,
                         medio_pago=req.medio_pago, referencia_pago=pago["yupy_transaction_id"],
                         facturacion=w.facturacion, correo_comprador=w.correo,
                         embarque=resultado.get("embarque"))
        registro.marcar_pago_usado(req.booking_id)
        if not simulada:
            _en_segundo_plano(asyncio.to_thread(enviar_boletos_correo, req.booking_id))
        if resultado.get("anomalia_boleto"):
            registro.registrar_incidencia("boleto_sin_numero", resultado["anomalia_boleto"],
                                          booking_id=req.booking_id, telefono=sesion.get("telefono"))
        if resultado.get("asientos_sin_nombre"):
            # La venta se hizo, pero JELAF la registró sin nombre: hay que corregirla en JELAF.
            registro.registrar_incidencia(
                "venta_sin_nombre",
                f"JELAF registró la venta sin nombre del pasajero (asientos {', '.join(resultado['asientos_sin_nombre'])}).",
                booking_id=req.booking_id, telefono=sesion.get("telefono"), severidad="critica",
                contexto=resultado.get("diagnostico_nombre") or {})
        if resultado.get("comprobante_jelaf") is False:
            registro.registrar_incidencia("comprobante_jelaf",
                                          "JELAF no generó el comprobante (convertir-venta-to-base64): reimprimir desde JELAF.",
                                          booking_id=req.booking_id)
        if resultado.get("popup_autorizacion"):
            registro.registrar_incidencia("autorizacion_jelaf",
                                          f"JELAF pidió autorización al emitir: {resultado['popup_autorizacion']}",
                                          booking_id=req.booking_id)
        respuesta = {"status": "success", "data": {"boletos": resultado["boletos"]}}
        _recordar_emision(req.booking_id, respuesta)
        await w.liberar_y_resetear()
        return respuesta


def _cobrado_sin_venta_activa(req: PeticionV2Confirmar, sesion: dict, pago: dict):
    asientos = {a: {"precio": 0} for a in json.loads(pago["asientos"])}
    _registrar_cobro_sin_boleto(
        req.booking_id, sesion, asientos,
        "El pago se aprobó pero la venta ya no estaba activa (el mostrador se liberó). No se emitió.",
        estado_venta="cobrada_sin_emitir", referencia=pago["yupy_transaction_id"], medio=req.medio_pago,
        monto_cobrado=Decimal(pago["monto_centimos"]) / 100,
    )
    raise HTTPException(status_code=409, detail=MENSAJE_COBRADO_SIN_BOLETO)


@app.post("/api/v2/venta/preparar-pago")
async def v2_preparar_pago(req: PeticionV2BookingId):
    """
    Marca la fase de pago y asegura que la ficha de los pasajeros ya esté en
    JELAF (normalmente /checkout ya lo hizo). Best-effort: si falla aquí,
    /confirmar lo vuelve a intentar antes de emitir.
    """
    pool = _pool_o_error()
    w = pool.worker_de(req.booking_id)
    if not w:
        return {"status": "ignorado"}
    # Marca la fase de pago YA (sin esperar el lock) para que el reaper use el
    # límite largo aunque el adelanto tarde unos segundos en arrancar.
    w.en_fase_pago = True
    w.tocar_actividad()
    async with _operar(w, req.booking_id):
        try:
            await w.preparar_pago()
        except (ErrorJelaf, DatosIncompletos) as e:
            log.info("preparar-pago %s: %s", privacidad.booking(req.booking_id), e)
        return {"status": "success", "adelantado": w.pago_preparado}


@app.post("/api/v2/venta/cancelar")
async def v2_cancelar_venta(req: PeticionV2BookingId):
    """Abandono explícito (botón/pagehide en seleccionar-asiento): libera el worker ya."""
    pool = _pool_o_error()
    w = pool.worker_de(req.booking_id)
    if not w:
        return {"status": "success", "mensaje": "Ya no había ninguna venta activa para esta compra."}
    async with _operar(w, req.booking_id):
        if w.pago_pendiente_hasta > time.monotonic():
            # El dinero puede estar en camino: no se suelta la venta (N4).
            raise HTTPException(status_code=409, detail="Tu pago está en curso; no se puede cancelar ahora.")
        registro.registrar_evento("abandono", booking_id=req.booking_id)
        registro.cerrar_sesion(req.booking_id, "abandonada")
        await w.liberar_y_resetear()
        return {"status": "success"}


@app.post("/api/v2/venta/heartbeat")
async def v2_heartbeat(req: PeticionV2BookingId):
    """El frontend avisa cada ~30 s que el cliente sigue en la compra."""
    pool = _pool_o_error()
    w = pool.worker_de(req.booking_id)
    if w:
        w.tocar_actividad()
        return {"status": "success"}
    return {"status": "ignorado", "mensaje": "Esta compra ya no está activa."}


# ==========================================
# 📱 VUELTA DESDE LA BILLETERA (N60-SN)
# ==========================================
# El cliente sale de WhatsApp para pagar con Yape/Plin; WhatsApp congela o
# descarta la página y la confirmación de Niubiz no llega (caso #147). Al volver,
# la pantalla pregunta en qué quedó la compra y, si sigue esperando, ofrece
# "Ya pagué": alerta crítica + retención de los asientos. "Ya pagué" NUNCA
# emite: el boleto sale solo si la pasarela confirma el pago, o si el equipo
# lo ve en el portal y emite a mano.
RETENCION_RECLAMO_SEGUNDOS = 30 * 60
# Un número que reclama pagos que no aparecen no puede bloquear asientos sin fin.
MAX_RECLAMOS_CON_RETENCION_POR_DIA = 2
_limite_estado_pago = Limitador(maximo=60, ventana_segundos=60)
_limite_ya_pague = Limitador(maximo=5, ventana_segundos=600)
_lock_reclamos = asyncio.Lock()     # dos toques seguidos de "Ya pagué" = un solo reclamo


def _reclamo_de(booking_id: str) -> int | None:
    fila = paneles_db.consultar_uno(
        "SELECT id FROM incidencia WHERE booking_id = ? AND tipo = 'pago_reclamado' ORDER BY id LIMIT 1",
        (booking_id,))
    return fila[0] if fila else None


def _reclamos_del_telefono_hoy(telefono: str) -> int:
    desde = (tiempo.ahora_dt() - timedelta(hours=24)).isoformat(timespec="seconds")
    return paneles_db.consultar_uno(
        "SELECT COUNT(*) FROM incidencia WHERE tipo = 'pago_reclamado' AND telefono = ? AND ocurrido_en >= ?",
        (telefono, desde))[0]


def _nombre_pasarela(pago: dict) -> str:
    return "Niubiz" if pago.get("proveedor") == "niubiz" else "Yupy"


@app.post("/api/v2/venta/estado-pago")
async def v2_estado_pago(req: PeticionEstadoPago, request: Request):
    """
    ¿En qué quedó esta compra? Solo lectura: no cobra, no emite, no registra
    incidencias. emitida (con sus boletos) | cobrada_sin_emitir | esperando_pago
    | reclamado | sin_pago. Si la venta sigue activa, además adelanta al
    conciliador (que solo emite si la pasarela confirma el pago).
    """
    _limite_estado_pago.exigir(ip_cliente(request))
    if req.contexto == "descartada":
        registro.registrar_evento("pagina_descartada", booking_id=req.booking_id)
    venta = consultas.venta_de_booking(req.booking_id)
    if venta:
        if venta["estado"] in ("emitida", "simulada"):
            boletos = _respuesta_boletos(venta["boletos"], simulada=venta["estado"] == "simulada")["data"]
            return {"status": "success", "estado": "emitida", **boletos}
        return {"status": "success", "estado": "cobrada_sin_emitir", "mensaje": MENSAJE_COBRADO_SIN_BOLETO}
    pago = consultas.pago_de_booking(req.booking_id)
    if not pago or pago["estado"] != "pendiente":
        return {"status": "success", "estado": "sin_pago"}
    activa = bool(pool_jelaf and pool_jelaf.worker_de(req.booking_id))
    if activa and time.monotonic() - _ultima_conciliacion.get(req.booking_id, 0.0) >= CONCILIAR_CADA_SEGUNDOS:
        _en_segundo_plano(_conciliar_venta(req.booking_id))
    return {
        "status": "success",
        "estado": "reclamado" if _reclamo_de(req.booking_id) else "esperando_pago",
        "venta_activa": activa,
        "pedido": pago["pedido"],
        "proveedor": pago.get("proveedor") or "yupy",
        "monto_total": float(Decimal(pago["monto_centimos"]) / 100),
    }


@app.post("/api/v2/venta/ya-pague")
async def v2_ya_pague(req: PeticionYaPague, request: Request):
    """
    El cliente dice que ya pagó y la confirmación no llegó. Alerta CRÍTICA con
    el número de operación y, una sola vez, retiene los asientos 30 minutos
    para que el equipo verifique en el portal y emita a mano. No emite nada.
    Una vez por compra; un número con muchos reclamos en el día ya no retiene.
    """
    _limite_ya_pague.exigir(ip_cliente(request))
    async with _lock_reclamos:
        pago = consultas.pago_de_booking(req.booking_id)
        if consultas.venta_de_booking(req.booking_id) or not pago or pago["estado"] != "pendiente":
            raise HTTPException(status_code=409, detail="No encontramos un pago en curso para esta compra.")
        mensaje = {"status": "recibido",
                   "mensaje": "Recibimos tu aviso. Verificaremos tu pago con la pasarela antes de emitir tu "
                              "boleto y te escribiremos por WhatsApp. No vuelvas a pagar."}
        if _reclamo_de(req.booking_id):
            return {**mensaje, "retenido": False, "repetido": True}

        sesion = _sesion_de_booking(req.booking_id)
        telefono = sesion.get("telefono")
        nombre = _nombre_pasarela(pago)
        monto = (Decimal(pago["monto_centimos"]) / 100).quantize(Decimal("0.01"))
        asientos = json.loads(pago.get("asientos") or "[]")
        previos = _reclamos_del_telefono_hoy(telefono) if telefono else 0
        w = pool_jelaf.worker_de(req.booking_id) if pool_jelaf else None
        retenido = False
        if w and previos < MAX_RECLAMOS_CON_RETENCION_POR_DIA:
            try:
                async with _operar(w, req.booking_id):
                    w.pago_pendiente_hasta = max(w.pago_pendiente_hasta,
                                                 time.monotonic() + RETENCION_RECLAMO_SEGUNDOS)
                    retenido = True
            except HTTPException:
                w = None                  # se soltó justo ahora

        texto = (f"El cliente dice que ya pagó el pedido {pago['pedido']} (S/ {monto}, {nombre}) con Yape, Plin u "
                 f"otra billetera, nro. de operación {req.numero_operacion}, y la confirmación no llegó. Buscar el "
                 f"pago en el portal de {nombre} con ese número. No emitir sin ver el pago en el portal.")
        if retenido:
            texto += f" Asientos {', '.join(asientos)} retenidos {RETENCION_RECLAMO_SEGUNDOS // 60} min."
        elif not w:
            texto += " Los asientos ya se habían soltado: si se confirma el pago, emitir en otro asiento o devolver."
        else:
            texto += f" Asientos sin retener: este número ya reclamó {previos} pago(s) en las últimas 24 h."
        registro.registrar_incidencia(
            "pago_reclamado", texto, booking_id=req.booking_id, telefono=telefono,
            contexto={"pedido": pago["pedido"], "proveedor": pago.get("proveedor") or "yupy", "monto": float(monto),
                      "numero_operacion": req.numero_operacion, "asientos": asientos, "retenido": retenido})
        registro.registrar_evento("pago_reclamado", booking_id=req.booking_id, telefono=telefono,
                                  detalle={"retenido": retenido})
    return {**mensaje, "retenido": retenido}


_limite_salir = Limitador(maximo=10, ventana_segundos=600)


async def _cerrada_por_la_pasarela(booking_id: str) -> str | None:
    """¿La pasarela ya cobró? Se concilia y se dice cómo quedó: emitida | cobrada_sin_emitir | None (no cobró)."""
    await _conciliar_venta(booking_id)
    venta = consultas.venta_de_booking(booking_id)
    if not venta:
        return None
    return "emitida" if venta["estado"] in ("emitida", "simulada") else "cobrada_sin_emitir"


@app.post("/api/v2/venta/salir-sin-pagar")
async def v2_salir_sin_pagar(req: PeticionV2BookingId, request: Request):
    """
    "No pagué: salir sin pagar" (N61-SN, pedido de boletería del 18/10: desde
    el aviso de pago en curso no había cómo salir). Distinto de /cancelar, que
    con un pago abierto se niega (N4): aquí lo pide el cliente a propósito.
    Antes de soltar nada se le pregunta a la pasarela: si ya cobró, se emite.
    Si no, el pedido se anula (Niubiz ya no lo autoriza; el QR de Yupy se
    cancela), se sueltan los asientos y queda constancia en el panel.
    """
    _limite_salir.exigir(ip_cliente(request))
    async with _lock_reclamos:
        if consultas.venta_de_booking(req.booking_id):
            return {"status": "success", "estado": "emitida"}
        pago = consultas.pago_de_booking(req.booking_id)
        abierto = bool(pago and pago["estado"] == "pendiente")
        if abierto:
            if _reclamo_de(req.booking_id):
                raise HTTPException(status_code=409, detail="Ya nos avisaste que pagaste: lo estamos verificando. "
                                                            "Si fue un error, escríbenos por WhatsApp.")
            if cerrada := await _cerrada_por_la_pasarela(req.booking_id):
                return {"status": "success", "estado": cerrada}
            async with _lock_niubiz:      # que no se cuele una autorización de Niubiz entre medio
                anulado = registro.marcar_pago_abandonado(req.booking_id)
            if anulado is not True:
                # Llegó una autorización mientras tanto, o la base falló: no se suelta nada.
                if cerrada := await _cerrada_por_la_pasarela(req.booking_id):
                    return {"status": "success", "estado": cerrada}
                raise HTTPException(status_code=409, detail="Tu pago se está confirmando. Espera unos segundos.")
            if (pago.get("proveedor") or "yupy") == "yupy" and yupy.api == "checkouts":
                for token in json.loads(pago.get("checkout_tokens") or "{}").values():
                    try:
                        await asyncio.to_thread(yupy.cancelar_checkout, token)
                    except Exception as e:      # el pedido ya quedó anulado aquí; Yupy lo vence solo
                        log.warning("salir-sin-pagar: no se pudo cancelar un QR de Yupy: %s", type(e).__name__)

    w = pool_jelaf.worker_de(req.booking_id) if pool_jelaf else None
    if w:
        try:
            async with _operar(w, req.booking_id):
                await w.liberar_y_resetear()
        except HTTPException:
            log.info("salir-sin-pagar %s: la venta ya se había soltado", privacidad.booking(req.booking_id))
    sesion = _sesion_de_booking(req.booking_id)
    if abierto:
        texto = (f"El cliente eligió «No pagué: salir» con el cobro de {_nombre_pasarela(pago)} abierto "
                 f"(pedido {pago['pedido']}). Se anuló el pedido y se soltaron los asientos. Si después aparece "
                 "un pago de ese pedido en el portal, el panel lo mostrará como aviso descartado.")
    else:
        texto = "El cliente salió sin pagar desde el aviso de pago en curso."
    registro.registrar_incidencia("sesion_liberada", texto, booking_id=req.booking_id, telefono=sesion.get("telefono"))
    registro.registrar_evento("abandono", booking_id=req.booking_id)
    registro.cerrar_sesion(req.booking_id, "abandonada")
    return {"status": "success", "estado": "salio"}


@app.get("/api/v2/venta/estado")
async def v2_estado_pool(x_paneles_token: str | None = Header(None)):
    """Diagnóstico del pool. Protegido con la clave del panel (S2)."""
    exigir_token(x_paneles_token)
    return {"workers": _pool_o_error().estado()}


def _boleto_emitido(venta: dict | None, asiento: str) -> tuple[dict, dict, str]:
    """La venta y el boleto EMITIDOS de ese asiento, de los datos guardados; si no, 404."""
    if not venta or venta["estado"] not in ("emitida", "simulada"):
        raise HTTPException(status_code=404, detail="No encontramos ese boleto.")
    asiento = asiento.zfill(2)
    boleto = next((b for b in venta["boletos"] if str(b.get("asiento")).zfill(2) == asiento), None)
    if not boleto:
        raise HTTPException(status_code=404, detail="No encontramos ese boleto.")
    return venta, boleto, asiento


@app.post("/api/v2/boleto/enlace")
def enlace_pdf_boleto(req: PeticionPDF, request: Request):
    """
    URL firmada del PDF de un boleto emitido. La pide la pantalla final al
    tocar "PDF" y navega a ella: el navegador de WhatsApp le pasa esa URL a
    Chrome o al gestor de descargas, que sí pueden bajarla (un blob: no).
    """
    _limite_pdf.exigir(ip_cliente(request))
    venta, _, asiento = _boleto_emitido(consultas.venta_de_booking(req.booking_id), req.asiento)
    return {"url": f"/api/v2/boleto/pdf/{whatsapp.firmar_pdf(venta['id'], asiento, CONFIG.enlace_secreto)}"}


@app.get("/api/v2/boleto/pdf/{token}")
def descargar_pdf_firmado(token: Annotated[str, Path(max_length=200)], request: Request):
    _limite_pdf.exigir(ip_cliente(request))
    datos = whatsapp.verificar_pdf(token, CONFIG.enlace_secreto)
    if not datos:
        raise HTTPException(status_code=404, detail="Este enlace venció. Vuelve a tocar «PDF».")
    venta_id, asiento = datos
    return _respuesta_pdf(*_boleto_emitido(consultas.venta_por_id(venta_id), asiento))


@app.post("/api/v2/boleto/pdf")
def descargar_pdf_boleto(req: PeticionPDF, request: Request):
    """
    PDF de un boleto EMITIDO, armado con los datos guardados, nunca con lo que
    manda el navegador (V4). El booking_id es un UUID aleatorio que solo
    conoce el comprador. Se mantiene para pantallas ya abiertas con la versión
    anterior; la nueva usa /boleto/enlace.
    """
    _limite_pdf.exigir(ip_cliente(request))
    return _respuesta_pdf(*_boleto_emitido(consultas.venta_de_booking(req.booking_id), req.asiento))


def _pdf_de(venta: dict, boleto: dict, asiento: str) -> tuple[bytes, str]:
    try:
        emitido = datetime.fromisoformat(venta["comprado_en"])
    except (TypeError, ValueError):
        emitido = datetime.now()
    return (boleto_pdf.generar(venta, boleto, emitido),
            boleto_pdf.nombre_archivo(boleto.get("nro_boleto") or venta.get("nro_boleto"), asiento))


# Ventas cuyos boletos ya se mandaron por correo en este proceso (además,
# /confirmar solo lo dispara en la primera emisión: nunca dos veces). Un
# reenvío pedido por el cliente pasa con forzar=True. _correos_en_curso evita
# dos envíos a la vez de la misma venta.
_correos_enviados: set[str] = set()
_correos_en_curso: set[str] = set()
MAX_REENVIOS_CORREO = 3
_limite_correo = Limitador(maximo=10, ventana_segundos=600)
_limite_estado_correo = Limitador(maximo=60, ventana_segundos=60)


def _motivo_para_cliente(resultado: correo.Resultado, destino: str) -> str:
    if resultado.causa == "destinatario":
        return f"El servidor de correo rechazó {destino}. Revisa que esté bien escrito y vuelve a enviarlo."
    return ("No pudimos enviar el correo en este momento. Inténtalo de nuevo en unos minutos; "
            "mientras tanto, guarda tus boletos con una captura de pantalla.")


def _config_correo() -> correo.ConfigCorreo:
    return correo.ConfigCorreo(host=CONFIG.smtp_host, puerto=CONFIG.smtp_puerto, usuario=CONFIG.smtp_usuario,
                               password=CONFIG.smtp_password, remitente=CONFIG.correo_remitente,
                               nombre_remitente=CONFIG.correo_nombre_remitente)


def enviar_boletos_correo(booking_id: str, forzar: bool = False) -> None:
    """
    Manda al correo del comprador el detalle de compra y un PDF por boleto.
    El navegador de WhatsApp no deja descargar (prueba del 14/10/2026): la
    pantalla solo MUESTRA el boleto y el PDF llega por aquí. Antes se mandaba
    por WhatsApp; se quitó (desde octubre esos mensajes se cobran). Corre en
    segundo plano y nunca afecta la venta; si falla, queda la incidencia.
    """
    if (booking_id in _correos_enviados and not forzar) or booking_id in _correos_en_curso:
        return
    _correos_enviados.add(booking_id)
    _correos_en_curso.add(booking_id)
    venta = None
    try:
        venta = consultas.venta_de_booking(booking_id)
        if not venta or venta.get("estado") != "emitida" or not venta.get("correo"):
            return
        adjuntos = []
        for boleto in venta["boletos"]:
            pdf, nombre = _pdf_de(venta, boleto, str(boleto.get("asiento") or "").zfill(2))
            adjuntos.append((nombre, pdf))
        cfg = _config_correo()
        mensaje = correo.armar(venta, adjuntos, destinatario=venta["correo"], remitente=cfg.remitente,
                               nombre_remitente=cfg.nombre_remitente, url_terminos=CONFIG.url_terminos)
        resultado = correo.enviar(mensaje, cfg)
        if resultado.ok:
            registro.marcar_correo(venta["id"], "enviado")
            log.info("Boletos de %s enviados por correo.", privacidad.booking(booking_id))
            return
        registro.marcar_correo(venta["id"], "fallido", _motivo_para_cliente(resultado, venta["correo"]))
        log.warning("No se pudo mandar el correo de %s (%s): %s", privacidad.booking(booking_id),
                    resultado.causa, resultado.error)
        if resultado.causa != "destinatario":   # dirección mal escrita: la corrige el cliente
            registro.registrar_incidencia(
                "correo_fallido", f"No se pudieron mandar los boletos por correo: {resultado.error}",
                booking_id=booking_id, telefono=venta.get("telefono"))
    except Exception as e:   # el boleto ya está emitido: esto nunca puede tumbar nada
        log.warning("Envío del correo de %s falló: %s", privacidad.booking(booking_id), type(e).__name__)
        if venta and venta.get("id"):
            registro.marcar_correo(venta["id"], "fallido", _motivo_para_cliente(correo.Resultado(False), ""))
    finally:
        _correos_en_curso.discard(booking_id)


def _estado_correo(venta: dict) -> dict:
    estado = venta.get("correo_estado") or ("sin_correo" if not venta.get("correo") else "pendiente")
    return {"status": "success", "estado": estado, "correo": venta.get("correo"),
            "motivo": venta.get("correo_motivo") if estado == "fallido" else None,
            "reenvios_restantes": max(0, MAX_REENVIOS_CORREO - int(venta.get("correo_reenvios") or 0))}


def _venta_emitida(booking_id: str) -> dict:
    venta = consultas.venta_de_booking(booking_id)
    if not venta or venta.get("estado") != "emitida":
        raise HTTPException(status_code=404, detail="No encontramos esa compra.")
    return venta


@app.post("/api/v2/boleto/correo-estado")
def v2_correo_estado(req: PeticionV2BookingId, request: Request):
    """¿Llegó a salir el correo con los boletos? La pantalla final lo consulta mientras está pendiente."""
    _limite_estado_correo.exigir(ip_cliente(request))
    return _estado_correo(_venta_emitida(req.booking_id))


@app.post("/api/v2/boleto/correo-reenviar")
async def v2_correo_reenviar(req: PeticionContacto, request: Request):
    """
    El cliente corrige su correo (o pide reenviarlo) desde la pantalla final.
    Solo con el booking_id de la compra (como el PDF), con tope de reenvíos y
    límite por IP: no sirve para mandar boletos a terceros en cantidad.
    """
    _limite_correo.exigir(ip_cliente(request))
    venta = await asyncio.to_thread(_venta_emitida, req.booking_id)
    if req.booking_id in _correos_en_curso:
        raise HTTPException(status_code=409, detail="Estamos enviando tus boletos. Espera unos segundos.")
    if int(venta.get("correo_reenvios") or 0) >= MAX_REENVIOS_CORREO:
        raise HTTPException(status_code=429, detail="Ya no se puede reenviar más veces. Escríbenos por WhatsApp "
                                                    "y te los enviamos.")
    registro.marcar_correo(venta["id"], "pendiente", None, correo=req.correo, reenvio=True)
    registro.registrar_evento("correo_reenviado", booking_id=req.booking_id)
    _en_segundo_plano(asyncio.to_thread(enviar_boletos_correo, req.booking_id, True))
    return {**_estado_correo({**venta, "correo": req.correo, "correo_estado": "pendiente",
                              "correo_reenvios": int(venta.get("correo_reenvios") or 0) + 1})}


def _respuesta_pdf(venta: dict, boleto: dict, asiento: str) -> Response:
    pdf, nombre = _pdf_de(venta, boleto, asiento)
    return Response(content=pdf, media_type="application/pdf",
                    # inline: prueba real en el navegador de WhatsApp (Android, 2026-10-14):
                    # abrir el PDF funciona; "descargar" (atributo download, blob) muestra
                    # "Estás saliendo de WhatsApp" y no hace nada. iPhone lo muestra en la ventana.
                    # Lleva nombre y documento: que ni el navegador ni un proxy lo guarden.
                    headers={"Content-Disposition": f'inline; filename="{nombre}"',
                             "Cache-Control": "no-store, private"})


# ==========================================
# 🟢 WHATSAPP (webhook de Meta)
# ==========================================
@app.get("/api/v2/contacto")
def v2_contacto(c: str | None = Query(None, max_length=200)):
    """Celular de quien abrió el enlace de compra (token firmado), para prellenar el formulario."""
    return {"telefono": whatsapp.celular_nacional(whatsapp.verificar_contacto(c, CONFIG.enlace_secreto))}


def enviar_boton_compra(numero_destino: str, numero_negocio_id: str = "") -> None:
    """
    Envía el mensaje del bot (botón de compra) desde el número de la empresa
    que recibió el mensaje; sin ese dato, desde PHONE_NUMBER_ID.
    MANDA UN WHATSAPP REAL: los tests lo reemplazan.
    """
    ok, error = whatsapp.enviar_boton_compra(
        numero_destino, token=CONFIG.whatsapp_token, phone_id=numero_negocio_id or CONFIG.phone_number_id,
        url_frontend=CONFIG.url_frontend, secreto_enlace=CONFIG.enlace_secreto,
    )
    if ok:
        registro.registrar_enlace_enviado(numero_destino)
    else:
        # No contó como la respuesta del día: el próximo mensaje lo vuelve a intentar.
        registro.liberar_respuesta_del_dia(numero_destino)
        registro.registrar_incidencia("error_interno", f"No se pudo enviar el botón de compra: {error}",
                                      telefono=numero_destino)


@app.get("/api/v1/webhook")
async def verificar_webhook(
    hub_mode: str | None = Query(None, alias="hub.mode"),
    hub_verify_token: str | None = Query(None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(None, alias="hub.challenge"),
):
    """Verificación de suscripción que exige Meta. Sin META_VERIFY_TOKEN, 403 (falla cerrada)."""
    esperado = CONFIG.meta_verify_token
    if (hub_mode == "subscribe" and esperado and hub_verify_token
            and hmac.compare_digest(hub_verify_token.encode(), esperado.encode())):
        return Response(content=hub_challenge or "", media_type="text/plain")
    raise HTTPException(status_code=403, detail="Error de autenticación")


MAX_CUERPO_WEBHOOK_YUPY = 64 * 1024


@app.post("/api/v2/yupy/webhook")
async def webhook_yupy(request: Request):
    """
    Avisos de Yupy (YUPY Admin → Webhooks). Se verifica la firma HMAC sobre el
    cuerpo crudo ANTES de leerlo, se descarta lo repetido (event_id) y se
    registra. El aviso por sí solo NO emite: un "pagado" no reemplaza la
    verificación servidor a servidor de /confirmar (regla 8). Solo adelanta al
    conciliador (N59-SN), que le pregunta a Yupy y emite si Yupy lo confirma.
    Respuesta rápida: Yupy reintenta si no recibe 2xx.
    """
    if not CONFIG.yupy_webhook_secret:
        log.error("webhook Yupy: YUPY_WEBHOOK_SECRET no está configurado; aviso rechazado.")
        raise HTTPException(status_code=503, detail="Webhook no configurado.")
    cuerpo = await request.body()
    if len(cuerpo) > MAX_CUERPO_WEBHOOK_YUPY:
        raise HTTPException(status_code=413, detail="Aviso demasiado grande.")
    ts = request.headers.get("X-YUPY-Timestamp")
    if not pagos_yupy.firma_webhook_valida(cuerpo, ts, request.headers.get("X-YUPY-Signature"),
                                           CONFIG.yupy_webhook_secret):
        log.warning("webhook Yupy: firma inválida (cabeceras: timestamp %s, firma %s).",
                    "sí" if ts else "no", "sí" if request.headers.get("X-YUPY-Signature") else "no")
        raise HTTPException(status_code=401, detail="Firma inválida.")
    vigente = pagos_yupy.timestamp_vigente(ts)
    if vigente is False:
        log.warning("webhook Yupy: aviso fuera de tiempo (posible reenvío); rechazado.")
        raise HTTPException(status_code=401, detail="Aviso vencido.")
    if vigente is None:
        log.warning("webhook Yupy: formato de X-YUPY-Timestamp no reconocido (%d caracteres).", len(ts or ""))
    try:
        aviso = json.loads(cuerpo)
    except ValueError:
        raise HTTPException(status_code=400, detail="Cuerpo inválido.")
    datos = aviso.get("data") if isinstance(aviso, dict) and isinstance(aviso.get("data"), dict) else {}
    event_id = str((aviso or {}).get("event_id") or datos.get("event_uid") or "")[:120]
    event_type = str((aviso or {}).get("event_type") or datos.get("event_type") or "")[:80]
    if not event_id or not event_type:
        raise HTTPException(status_code=400, detail="Aviso sin event_id/event_type.")
    orden = str(datos.get("payment_order_uid") or "")[:120] or None
    resultado = str(datos.get("result") or "")[:20] or None
    booking = consultas.booking_de_orden_yupy(orden) if orden else None

    nuevo = registro.registrar_webhook_yupy(event_id, event_type, orden, resultado, booking)
    if nuevo is None:
        raise HTTPException(status_code=503, detail="No se pudo registrar el aviso; reintentar.")
    if nuevo:
        registro.registrar_evento("yupy_" + event_type.replace(".", "_"), booking_id=booking,
                                  detalle={"resultado": resultado, "event_id": event_id})
        log.info("webhook Yupy: %s%s%s", event_type, f" ({resultado})" if resultado else "",
                 f" venta {privacidad.booking(booking)}" if booking else "")
        if booking:
            # El aviso no decide nada (su tipo y resultado no se usan para emitir):
            # solo adelanta la consulta servidor a servidor del conciliador (N59-SN).
            _en_segundo_plano(_conciliar_venta(booking))
    return {"ok": True}


@app.post("/api/v1/webhook")
async def recibir_mensajes(request: Request, background_tasks: BackgroundTasks):
    """
    Mensajes entrantes. S7: la firma X-Hub-Signature-256 se verifica sobre el
    cuerpo crudo ANTES de parsear, y cada id de mensaje se procesa una sola
    vez (Meta reintenta). Los envíos van en segundo plano.

    Un solo mensaje (bienvenida + compra + asesor humano) y una sola vez al
    día por número, hora de Lima (pedido de la empresa, 2026-10-15): el primer
    mensaje del día recibe la respuesta; los demás de ese día solo se
    registran. Los Términos y Condiciones ya no se piden por WhatsApp: se
    aceptan en la mini-webapp antes de pagar.
    """
    cuerpo = await request.body()
    if CONFIG.meta_app_secret:
        if not whatsapp.firma_webhook_valida(cuerpo, request.headers.get("X-Hub-Signature-256"),
                                             CONFIG.meta_app_secret):
            raise HTTPException(status_code=401, detail="Firma inválida")
    elif CONFIG.es_produccion:   # config.py ya lo impide; defensa en profundidad
        raise HTTPException(status_code=401, detail="Firma inválida")

    try:
        body = json.loads(cuerpo or b"{}")
        mensajes, estados = whatsapp.extraer_eventos(body if isinstance(body, dict) else {})
    except ValueError:
        log.warning("Webhook con cuerpo no JSON; se ignora.")
        return Response(content="EVENT_RECEIVED", status_code=200)

    for msg in mensajes:
        if CONFIG.whatsapp_numeros_permitidos and msg.telefono not in CONFIG.whatsapp_numeros_permitidos:
            continue   # etapa de prueba: fuera de la lista blanca no se responde ni se registra
        if registro.marcar_mensaje_nuevo(msg.id) is False:
            continue   # reintento de Meta: ya procesado
        registro.registrar_mensaje(msg.telefono, msg.nombre_perfil)
        if not whatsapp.merece_respuesta(msg):
            continue
        # False = ya se le respondió hoy. None = la base falló (@seguro): se
        # responde igual, mejor un mensaje de más que un cliente sin respuesta.
        # WHATSAPP_UNA_VEZ_AL_DIA=0 (desarrollo) responde a cada mensaje.
        if CONFIG.whatsapp_una_vez_al_dia and registro.reservar_respuesta_del_dia(msg.telefono) is False:
            continue
        background_tasks.add_task(enviar_boton_compra, msg.telefono, msg.numero_negocio_id)
    for st in estados:
        if st.estado == "failed":
            registro.registrar_incidencia("whatsapp_fallido", f"Meta no entregó un mensaje: {st.error}",
                                          telefono=st.telefono)

    # Siempre 200 a Meta para que no deshabilite el webhook.
    return Response(content="EVENT_RECEIVED", status_code=200)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=CONFIG.host, port=CONFIG.port)
