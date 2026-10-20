"""
Capa de ESCRITURA de los paneles.

Todo lo que api_ventas.py necesita llamar para dejar rastro esta aqui. Ningun
otro modulo escribe en la base.

REGLA IRROMPIBLE: ninguna funcion de este archivo puede tumbar una venta.
Todas estan envueltas en @seguro, que atrapa cualquier excepcion, la imprime
y devuelve None. Si la base se corrompe o el disco se llena, el cliente
igual compra su pasaje; lo unico que se pierde es el reporte.
"""

from __future__ import annotations

import functools
import json
import sqlite3
import threading
import traceback
from datetime import datetime, timedelta

from . import atencion, bonos, db, tiempo

# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

# Orden real del flujo de compra. Sirve para que paso_maximo solo avance:
# si el cliente vuelve atras y vuelve a tocar un asiento, la sesion no
# "retrocede" en el embudo.
PASOS = [
    "enlace_enviado",
    "enlace_abierto",
    "viaje_elegido",
    "asiento_tocado",
    "pasajero_lleno",
    "pago_iniciado",
    "pago_confirmado",
    "boleto_emitido",
]

# Severidad por tipo de incidencia. cobrado_sin_emitir es la unica critica:
# es plata cobrada sin servicio entregado.
SEVERIDAD = {
    "cobrado_sin_emitir": "critica",
    "por_conciliar": "critica",
    # Se soltó la venta con un cobro abierto que nunca se confirmó: pudo haber pagado (N56-SN).
    "pago_sin_confirmar": "critica",
    # Un aviso de Niubiz que no se autorizó (sin token, límite, pedido viejo): N57-SN.
    "niubiz_aviso_descartado": "critica",
    # El cliente tocó "Ya pagué" y la confirmación no llegó: verificar en el portal (N60-SN).
    "pago_reclamado": "critica",
    "fallo_emision": "grave",
    "pago_no_verificado": "grave",
    "emision_conciliada": "grave",
    "boleto_sin_numero": "grave",
    "fila_ambigua": "grave",
    "autorizacion_jelaf": "leve",
    "whatsapp_fallido": "leve",
    "jelaf_caido": "grave",
    "error_interno": "grave",
    "asiento_no_disponible": "leve",
    # JELAF no dio precio para un asiento: se perdió una venta, pero sin dinero de por medio.
    "asiento_sin_precio": "grave",
    "pool_ocupado": "leve",
    "pago_rechazado": "leve",
    "sesion_liberada": "leve",
}

# Umbrales de segmentacion (dias sin comprar / viajes acumulados).
DIAS_DORMIDO = 60
VIAJES_FRECUENTE = 4
VIAJES_OCASIONAL = 2


def ahora() -> str:
    """Hora de Lima en ISO-8601 sin zona (ver paneles/tiempo.py)."""
    return tiempo.ahora()


# P5: @seguro traga los errores para no tumbar ventas, pero entonces nadie se
# enteraba si la base dejaba de escribir. Se cuentan aqui y se publican en
# /api/paneles/salud y /api/paneles/incidencias.
_fallos_lock = threading.Lock()
FALLOS_REGISTRO = {"total": 0, "ultimo_en": None, "ultima_funcion": None, "ultimo_error": None}


def fallos_registro() -> dict:
    with _fallos_lock:
        return dict(FALLOS_REGISTRO)


def seguro(fn):
    """Envuelve una funcion de registro para que nunca propague errores."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            with _fallos_lock:
                FALLOS_REGISTRO["total"] += 1
                FALLOS_REGISTRO["ultimo_en"] = ahora()
                FALLOS_REGISTRO["ultima_funcion"] = fn.__name__
                FALLOS_REGISTRO["ultimo_error"] = type(e).__name__
            print(f"[paneles] fallo registrando {fn.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
            return None

    return wrapper


def _json(valor) -> str | None:
    if valor is None:
        return None
    try:
        return json.dumps(valor, ensure_ascii=False, default=str)
    except Exception:
        return str(valor)


def _timestamp_salida(fecha_viaje: str | None, hora_viaje: str | None) -> str | None:
    """Junta 'YYYY-MM-DD' + 'HH:MM' en un solo ISO-8601 para la columna sale_en."""
    if not fecha_viaje:
        return None
    fecha = fecha_viaje.strip()
    # Acepta tambien DD/MM/YYYY, que es como lo maneja JELAF.
    if "/" in fecha:
        try:
            d, m, a = fecha.split("/")
            fecha = f"{a}-{m.zfill(2)}-{d.zfill(2)}"
        except ValueError:
            return None
    hora = (hora_viaje or "00:00").strip()
    if len(hora) == 5:
        hora += ":00"
    return f"{fecha}T{hora}"


# ---------------------------------------------------------------------------
# Contacto y sesion
# ---------------------------------------------------------------------------

@seguro
def registrar_mensaje(telefono: str, nombre_wa: str | None = None) -> None:
    """El cliente escribio al WhatsApp de la empresa."""
    t = ahora()
    db.escribir(
        """
        INSERT INTO contacto (telefono, nombre_wa, primer_mensaje, ultimo_mensaje, mensajes)
        VALUES (?, ?, ?, ?, 1)
        ON CONFLICT(telefono) DO UPDATE SET
            ultimo_mensaje = excluded.ultimo_mensaje,
            mensajes       = contacto.mensajes + 1,
            nombre_wa      = COALESCE(excluded.nombre_wa, contacto.nombre_wa)
        """,
        (telefono, nombre_wa, t, t),
    )
    registrar_evento("mensaje_recibido", telefono=telefono)


@seguro
def registrar_enlace_enviado(telefono: str) -> None:
    """Se le mando el boton 'Comprar Pasaje'."""
    db.escribir(
        "UPDATE contacto SET enlaces_enviados = enlaces_enviados + 1 WHERE telefono = ?",
        (telefono,),
    )
    registrar_evento("enlace_enviado", telefono=telefono)


@seguro
def abrir_sesion(booking_id: str, telefono: str | None = None) -> None:
    """Primer contacto de un booking_id con el backend. Idempotente."""
    t = ahora()
    db.escribir(
        """
        INSERT INTO sesion_compra (booking_id, telefono, abierta_en, actualizada_en, paso_maximo)
        VALUES (?, ?, ?, ?, 'enlace_abierto')
        ON CONFLICT(booking_id) DO UPDATE SET
            actualizada_en = excluded.actualizada_en,
            telefono       = COALESCE(excluded.telefono, sesion_compra.telefono)
        """,
        (booking_id, telefono, t, t),
    )


@seguro
def reabrir_sesion(booking_id: str) -> None:
    """
    El cliente volvio atras y retomo la MISMA venta (mismo booking_id): la
    sesion vuelve a 'en_curso' en vez de quedar 'abandonada' (P3).
    """
    db.escribir(
        """
        UPDATE sesion_compra SET estado = 'en_curso', cerrada_en = NULL, actualizada_en = ?
        WHERE booking_id = ? AND estado IN ('abandonada', 'rechazada')
        """,
        (ahora(), booking_id),
    )


@seguro
def actualizar_viaje(
    booking_id: str,
    origen: str | None = None,
    destino: str | None = None,
    fecha_viaje: str | None = None,
    hora_viaje: str | None = None,
    servicio: str | None = None,
    placa_bus: str | None = None,
) -> None:
    """Guarda el viaje elegido en la sesion (se conoce recien en el paso 2)."""
    abrir_sesion(booking_id)
    db.escribir(
        """
        UPDATE sesion_compra SET
            origen         = COALESCE(?, origen),
            destino        = COALESCE(?, destino),
            fecha_viaje    = COALESCE(?, fecha_viaje),
            hora_viaje     = COALESCE(?, hora_viaje),
            servicio       = COALESCE(?, servicio),
            placa_bus      = COALESCE(?, placa_bus),
            actualizada_en = ?
        WHERE booking_id = ?
        """,
        (origen, destino, fecha_viaje, hora_viaje, servicio, placa_bus, ahora(), booking_id),
    )


@seguro
def registrar_evento(
    paso: str,
    booking_id: str | None = None,
    telefono: str | None = None,
    detalle: dict | None = None,
) -> None:
    """
    Deja el renglon en la bitacora y adelanta paso_maximo de la sesion si
    corresponde. paso_maximo nunca retrocede.
    """
    db.escribir(
        "INSERT INTO evento (booking_id, telefono, paso, ocurrido_en, detalle) VALUES (?,?,?,?,?)",
        (booking_id, telefono, paso, ahora(), _json(detalle)),
    )

    if not booking_id or paso not in PASOS:
        return

    fila = db.consultar_uno(
        "SELECT paso_maximo FROM sesion_compra WHERE booking_id = ?", (booking_id,)
    )
    if fila is None:
        return
    actual = fila["paso_maximo"]
    indice_actual = PASOS.index(actual) if actual in PASOS else -1
    if PASOS.index(paso) > indice_actual:
        db.escribir(
            "UPDATE sesion_compra SET paso_maximo = ?, actualizada_en = ? WHERE booking_id = ?",
            (paso, ahora(), booking_id),
        )
    else:
        db.escribir(
            "UPDATE sesion_compra SET actualizada_en = ? WHERE booking_id = ?",
            (ahora(), booking_id),
        )


@seguro
def cerrar_sesion(booking_id: str, estado: str) -> None:
    """estado: abandonada | emitida | fallida | rechazada."""
    db.escribir(
        "UPDATE sesion_compra SET estado = ?, cerrada_en = ?, actualizada_en = ? WHERE booking_id = ?",
        (estado, ahora(), ahora(), booking_id),
    )


@seguro
def actualizar_asientos(booking_id: str, asientos: list[str]) -> None:
    db.escribir(
        "UPDATE sesion_compra SET asientos = ?, actualizada_en = ? WHERE booking_id = ?",
        (_json(asientos), ahora(), booking_id),
    )


# ---------------------------------------------------------------------------
# Incidencias
# ---------------------------------------------------------------------------

@seguro
def registrar_incidencia(
    tipo: str,
    mensaje: str,
    booking_id: str | None = None,
    telefono: str | None = None,
    contexto: dict | None = None,
    severidad: str | None = None,
) -> int | None:
    sev = severidad or SEVERIDAD.get(tipo, "leve")
    incidencia_id = db.escribir(
        """
        INSERT INTO incidencia (booking_id, telefono, tipo, severidad, mensaje, contexto, ocurrido_en)
        VALUES (?,?,?,?,?,?,?)
        """,
        (booking_id, telefono, tipo, sev, mensaje, _json(contexto), ahora()),
    )
    print(f"[incidencia:{sev}] {tipo} - {mensaje}")
    return incidencia_id


def resolver_incidencia(incidencia_id: int, nota: str | None = None) -> dict:
    """Cierre rapido con nota (endpoint heredado /resolver). Queda como atencion 'otra'."""
    return registrar_atencion(incidencia_id, "otra", {"nota": nota or "Resuelta desde el panel"})


def derivar_incidencia(incidencia_id: int, motivo: str, derivada_a: str | None = None) -> dict:
    """Endpoint heredado /derivar. Queda como atencion 'derivar'."""
    return registrar_atencion(incidencia_id, "derivar",
                              {"nota": motivo, "derivada_a": derivada_a or "Sin indicar"})


class IncidenciaCerrada(Exception):
    """Se intento atender una incidencia que ya estaba resuelta o derivada."""


def _venta_de_solucion(booking_nuevo: str | None, nro_boleto: str | None) -> int | None:
    """La venta del sistema que respalda la solucion, si existe (un boleto hecho a mano en JELAF no esta)."""
    if booking_nuevo:
        f = db.consultar_uno("SELECT id FROM venta WHERE booking_id = ?", (booking_nuevo,))
        if f:
            return f["id"]
    if nro_boleto:
        f = db.consultar_uno(
            """
            SELECT id FROM venta WHERE nro_boleto = ?
            UNION SELECT venta_id FROM boleto WHERE nro_boleto = ?
            LIMIT 1
            """,
            (nro_boleto, nro_boleto),
        )
        if f:
            return f[0]
    return None


def registrar_atencion(incidencia_id: int, accion: str, datos: dict) -> dict:
    """
    Registra una accion sobre una incidencia y actualiza su estado.

    SIN @seguro a proposito: lo llama el panel, no la venta, y el error
    (accion invalida, faltan datos, ya cerrada) tiene que llegar a quien atiende.
    Lanza LookupError, IncidenciaCerrada o ValueError.
    """
    inc = db.consultar_uno("SELECT id, tipo, severidad, estado FROM incidencia WHERE id = ?",
                           (incidencia_id,))
    if inc is None:
        raise LookupError("Incidencia no encontrada.")
    if inc["estado"] in ("resuelta", "derivada"):
        raise IncidenciaCerrada("Esta incidencia ya está cerrada.")
    resultado = atencion.validar(accion, inc["tipo"], inc["severidad"], datos)
    venta_id = _venta_de_solucion(datos.get("booking_nuevo"), datos.get("nro_boleto"))
    t = ahora()
    with db.transaccion():
        atencion_id = db.escribir(
            """
            INSERT INTO atencion (incidencia_id, accion, resultado, canal_contacto, nota,
                                  nro_boleto, booking_nuevo, venta_id, referencia_devolucion,
                                  monto_devuelto_centimos, derivada_a, atendido_por, creado_en)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (incidencia_id, accion, resultado, datos.get("canal_contacto"),
             (datos.get("nota") or "").strip() or None, datos.get("nro_boleto"),
             datos.get("booking_nuevo"), venta_id, datos.get("referencia_devolucion"),
             datos.get("monto_devuelto_centimos"), datos.get("derivada_a"),
             datos.get("atendido_por"), t),
        )
        if resultado == "en_seguimiento":
            db.escribir("UPDATE incidencia SET estado = 'en_seguimiento', actualizada_en = ? WHERE id = ?",
                        (t, incidencia_id))
        else:
            db.escribir(
                """
                UPDATE incidencia SET estado = ?, resuelta = 1, resuelta_en = ?,
                       nota_resolucion = ?, actualizada_en = ?
                WHERE id = ?
                """,
                (resultado, t, atencion.resumen(accion, datos), t, incidencia_id),
            )
    return {"id": atencion_id, "resultado": resultado, "venta_vinculada": venta_id is not None}


# ---------------------------------------------------------------------------
# Ventas y clientes
# ---------------------------------------------------------------------------

@seguro
def registrar_venta(
    booking_id: str | None,
    origen: str,
    destino: str,
    pasajeros: list[dict],
    monto_total: float,
    *,
    nro_boleto: str | None = None,
    telefono: str | None = None,
    servicio: str | None = None,
    placa_bus: str | None = None,
    fecha_viaje: str | None = None,
    hora_viaje: str | None = None,
    monto_bruto: float | None = None,
    monto_descuento: float = 0.0,
    codigo_descuento: str | None = None,
    medio_pago: str | None = None,
    referencia_pago: str | None = None,
    estado: str = "emitida",
    comprobante: str = "boleta",
    ruc_cliente: str | None = None,
    razon_social: str | None = None,
    direccion_fiscal: str | None = None,
    correo: str | None = None,
    embarque_lugar: str | None = None,
    embarque_hora: str | None = None,
) -> int | None:
    """
    Guarda una venta cerrada con todos sus boletos y actualiza la ficha de
    cada cliente.

    pasajeros: [{documento, tipo_documento, nombres, paterno, materno,
                 asiento, precio}, ...]

    Se llama tambien con estado='cobrada_sin_emitir' cuando el pago paso y
    JELAF fallo: esa venta EXISTE (el dinero se movio) y tiene que aparecer
    en el panel, marcada, no desaparecer.
    """
    # P6: la venta, sus boletos y las fichas de cliente entran juntas o no
    # entra nada. Antes eran quince commits sueltos.
    with db.transaccion():
        venta_id = db.escribir(
            """
            INSERT INTO venta (
                booking_id, nro_boleto, telefono, origen, destino, servicio, placa_bus,
                comprado_en, sale_en, cantidad_asientos,
                monto_bruto, monto_descuento, monto_total, codigo_descuento,
                medio_pago, referencia_pago, estado,
                comprobante, ruc_cliente, razon_social, direccion_fiscal,
                correo, embarque_lugar, embarque_hora, correo_estado
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                booking_id, nro_boleto, telefono, origen, destino, servicio, placa_bus,
                ahora(), _timestamp_salida(fecha_viaje, hora_viaje), len(pasajeros) or 1,
                monto_bruto if monto_bruto is not None else monto_total,
                monto_descuento, monto_total, codigo_descuento,
                medio_pago, referencia_pago, estado,
                comprobante, ruc_cliente, razon_social, direccion_fiscal,
                correo, embarque_lugar, embarque_hora,
                "pendiente" if correo and estado == "emitida" else None,
            ),
        )

        for p in pasajeros:
            db.escribir(
                """
                INSERT INTO boleto (venta_id, documento, tipo_documento, nombres, paterno, materno,
                                    asiento, precio, nro_boleto, link_comprobante)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    venta_id,
                    p.get("documento"),
                    p.get("tipo_documento", "01"),
                    p.get("nombres"),
                    p.get("paterno"),
                    p.get("materno"),
                    p.get("asiento", ""),
                    float(p.get("precio") or 0),
                    p.get("nro_boleto"),
                    p.get("link_comprobante"),
                ),
            )
            if p.get("documento"):
                _upsert_cliente(p, telefono)
                recalcular_cliente(p["documento"])

    if booking_id:
        # 'boleto_emitido' SOLO si de verdad salió el boleto. Una venta
        # cobrada sin emitir llegó al pago pero no lo cruzó: si contara como
        # emitida, el embudo diría que se entregaron boletos que no existen.
        if estado == "emitida":
            registrar_evento("boleto_emitido", booking_id=booking_id, telefono=telefono,
                             detalle={"nro_boleto": nro_boleto, "monto": monto_total})
            cerrar_sesion(booking_id, "emitida")
        else:
            registrar_evento("pago_confirmado", booking_id=booking_id, telefono=telefono,
                             detalle={"estado": estado, "monto": monto_total})
            cerrar_sesion(booking_id, "fallida")

    if codigo_descuento:
        marcar_canje(codigo_descuento, pasajeros[0].get("documento") if pasajeros else None, venta_id)

    if estado == "emitida":
        # Reglas de bono por boletos o gasto acumulado. Va aparte con su propio
        # @seguro: si falla, la venta ya quedo registrada igual.
        bonos_por_venta([p["documento"] for p in pasajeros if p.get("documento")])

    return venta_id


@seguro
def _upsert_cliente(pasajero: dict, telefono: str | None) -> None:
    db.escribir(
        """
        INSERT INTO cliente (documento, tipo_documento, nombres, paterno, materno, telefono)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(documento) DO UPDATE SET
            nombres  = COALESCE(excluded.nombres, cliente.nombres),
            paterno  = COALESCE(excluded.paterno, cliente.paterno),
            materno  = COALESCE(excluded.materno, cliente.materno),
            telefono = COALESCE(excluded.telefono, cliente.telefono)
        """,
        (
            pasajero.get("documento"),
            pasajero.get("tipo_documento", "01"),
            pasajero.get("nombres"),
            pasajero.get("paterno"),
            pasajero.get("materno"),
            telefono,
        ),
    )


@seguro
def recalcular_cliente(documento: str) -> None:
    """
    Recalcula contadores y segmento desde los boletos reales. Se recalcula en
    vez de incrementar para que una anulacion o una correccion manual no deje
    el contador mentido.
    """
    fila = db.consultar_uno(
        """
        SELECT COUNT(*) AS viajes,
               COALESCE(SUM(b.precio), 0) AS gastado,
               MIN(v.comprado_en) AS primera,
               MAX(v.comprado_en) AS ultima
        FROM boleto b
        JOIN venta v ON v.id = b.venta_id
        WHERE b.documento = ? AND v.estado = 'emitida'
        """,
        (documento,),
    )
    if fila is None:
        return

    viajes = fila["viajes"] or 0
    ultima = fila["ultima"]
    segmento = "nuevo"
    if viajes >= VIAJES_FRECUENTE:
        segmento = "frecuente"
    elif viajes >= VIAJES_OCASIONAL:
        segmento = "ocasional"
    if ultima:
        try:
            dias = (tiempo.ahora_dt() - datetime.fromisoformat(ultima)).days
            if dias > DIAS_DORMIDO and viajes > 0:
                segmento = "dormido"
        except ValueError:
            pass

    db.escribir(
        """
        UPDATE cliente SET total_viajes = ?, total_gastado = ?,
                           primera_compra = ?, ultima_compra = ?, segmento = ?
        WHERE documento = ?
        """,
        (viajes, fila["gastado"] or 0, fila["primera"], ultima, segmento, documento),
    )


@seguro
def resegmentar_todos() -> int:
    """
    Repasa a todos los clientes. Hay que correrlo una vez al dia (tarea
    programada): un cliente pasa a 'dormido' por el paso del tiempo, no por
    una compra, asi que nadie dispara su recalculo solo.
    """
    limite = (tiempo.ahora_dt() - timedelta(days=DIAS_DORMIDO)).isoformat(timespec="seconds")
    db.escribir(
        """
        UPDATE cliente SET segmento = 'dormido'
        WHERE total_viajes > 0 AND ultima_compra IS NOT NULL AND ultima_compra < ?
        """,
        (limite,),
    )
    db.escribir(
        """
        UPDATE cliente SET segmento = CASE
            WHEN total_viajes >= ? THEN 'frecuente'
            WHEN total_viajes >= ? THEN 'ocasional'
            ELSE 'nuevo' END
        WHERE ultima_compra IS NULL OR ultima_compra >= ?
        """,
        (VIAJES_FRECUENTE, VIAJES_OCASIONAL, limite),
    )
    fila = db.consultar_uno("SELECT COUNT(*) AS n FROM cliente")
    return fila["n"] if fila else 0


@seguro
def marcar_canje(codigo: str, documento: str | None, venta_id: int) -> None:
    if not documento:
        return
    db.escribir(
        """
        UPDATE codigo_enviado SET canjeado_en = ?, venta_id = ?
        WHERE codigo = ? AND documento = ? AND canjeado_en IS NULL
        """,
        (ahora(), venta_id, codigo, documento),
    )


# ---------------------------------------------------------------------------
# Pago (Yupy) y mensajes de WhatsApp
# ---------------------------------------------------------------------------

@seguro
def guardar_pago(booking_id: str, yupy_transaction_id: str, pedido: str,
                 monto_centimos: int, asientos: list[str], proveedor: str = "yupy",
                 checkout_token: str | None = None) -> bool:
    """
    Guarda (o reemplaza, si el cliente reintento el checkout) el cobro de un
    booking_id mientras siga 'pendiente'. Devuelve True si quedo guardado y
    None si la base fallo: en ese caso el llamador NO debe mostrar el
    checkout, porque despues no habria forma de verificar el pago.
    `proveedor` es 'yupy' o 'niubiz' (con Niubiz, la referencia es "NIUBIZ-<pedido>").
    """
    t = ahora()
    tokens = _json({yupy_transaction_id: checkout_token} if checkout_token else {})
    cambiadas = db.escribir_contando(
        """
        INSERT INTO pago (booking_id, yupy_transaction_id, pedido, monto_centimos, moneda,
                          asientos, estado, creado_en, actualizado_en, proveedor, checkout_tokens)
        VALUES (?, ?, ?, ?, 'PEN', ?, 'pendiente', ?, ?, ?, ?)
        ON CONFLICT(booking_id) DO UPDATE SET
            transacciones_anteriores = json_insert(
                COALESCE(pago.transacciones_anteriores, '[]'), '$[#]', pago.yupy_transaction_id),
            yupy_transaction_id = excluded.yupy_transaction_id,
            pedido              = excluded.pedido,
            monto_centimos      = excluded.monto_centimos,
            asientos            = excluded.asientos,
            proveedor           = excluded.proveedor,
            autorizacion        = NULL,
            -- los tokens de checkouts anteriores se conservan: pudo pagarse uno viejo
            checkout_tokens     = json_patch(COALESCE(pago.checkout_tokens, '{}'), excluded.checkout_tokens),
            estado              = 'pendiente',
            actualizado_en      = excluded.actualizado_en
        -- 'abandonado': el cliente salió sin pagar y volvió a intentar con la misma compra (N61-SN).
        WHERE pago.estado IN ('pendiente', 'abandonado')
        """,
        (booking_id, yupy_transaction_id, pedido, monto_centimos, _json(sorted(asientos)), t, t, proveedor, tokens),
    )
    return cambiadas == 1


@seguro
def guardar_autorizacion_niubiz(booking_id: str, referencia: str, resumen: dict) -> bool:
    """
    Guarda el resumen de la autorizacion de Niubiz (sin datos de la tarjeta)
    en el pago pendiente cuya referencia es `referencia`. True si quedo
    guardado; False si el pago ya no es ese (el cliente abrio otro cobro).
    """
    return db.escribir_contando(
        """
        UPDATE pago SET autorizacion = ?, actualizado_en = ?
        WHERE booking_id = ? AND yupy_transaction_id = ? AND proveedor = 'niubiz'
          AND estado = 'pendiente'
        """,
        (_json(resumen), ahora(), booking_id, referencia),
    ) == 1


@seguro
def reservar_respuesta_del_dia(telefono: str) -> bool:
    """
    ¿Le toca la respuesta de hoy (hora de Lima)? Un solo INSERT/UPDATE marca el
    día: de varios mensajes seguidos del mismo número, solo uno gana.
    """
    hoy, t = tiempo.hoy().isoformat(), ahora()
    return db.escribir_contando(
        """
        INSERT INTO contacto (telefono, primer_mensaje, ultimo_mensaje, mensajes, respondido_el)
        VALUES (?, ?, ?, 1, ?)
        ON CONFLICT(telefono) DO UPDATE SET respondido_el = excluded.respondido_el
        WHERE contacto.respondido_el IS NULL OR contacto.respondido_el <> excluded.respondido_el
        """,
        (telefono, t, t, hoy),
    ) == 1


@seguro
def liberar_respuesta_del_dia(telefono: str) -> None:
    """El envío falló: hoy todavía no se le respondió."""
    db.escribir("UPDATE contacto SET respondido_el = NULL WHERE telefono = ? AND respondido_el = ?",
                (telefono, tiempo.hoy().isoformat()))


@seguro
def marcar_pago_abandonado(booking_id: str) -> bool:
    """
    El cliente dijo "No pagué: salir" (N61-SN). Solo si el cobro sigue
    pendiente y Niubiz no lo autorizó: desde aquí /niubiz/autorizar trata ese
    pedido como vencido y no cobra. True si quedó marcado.
    """
    return db.escribir_contando(
        "UPDATE pago SET estado = 'abandonado', actualizado_en = ? "
        "WHERE booking_id = ? AND estado = 'pendiente' AND autorizacion IS NULL",
        (ahora(), booking_id),
    ) == 1


@seguro
def marcar_pago_usado(booking_id: str) -> None:
    db.escribir(
        "UPDATE pago SET estado = 'usado', actualizado_en = ? WHERE booking_id = ?",
        (ahora(), booking_id),
    )


@seguro
def marcar_correo(venta_id: int, estado: str, motivo: str | None = None, *,
                  correo: str | None = None, reenvio: bool = False) -> None:
    """Estado del correo con los boletos de una venta (pendiente | enviado | fallido)."""
    db.escribir(
        "UPDATE venta SET correo_estado = ?, correo_motivo = ?, correo = COALESCE(?, correo), "
        "correo_reenvios = correo_reenvios + ? WHERE id = ?",
        (estado, motivo, correo, 1 if reenvio else 0, venta_id),
    )


@seguro
def marcar_mensaje_nuevo(mensaje_id: str) -> bool:
    """
    True si es la primera vez que se ve este id de mensaje de Meta; False si
    es un reintento. None si la base fallo (el llamador decide: procesar).
    """
    return db.escribir_contando(
        "INSERT OR IGNORE INTO mensaje_whatsapp (id, recibido_en) VALUES (?, ?)",
        (mensaje_id, ahora()),
    ) == 1

@seguro
def registrar_webhook_yupy(event_id: str, event_type: str, payment_order_uid: str | None,
                           result: str | None, booking_id: str | None) -> bool:
    """
    True si es la primera vez que llega este event_id; False si Yupy lo
    reintentó. None si la base falló (el llamador responde 503 para que Yupy
    lo vuelva a mandar).
    """
    return db.escribir_contando(
        """INSERT OR IGNORE INTO yupy_webhook
               (event_id, event_type, payment_order_uid, result, booking_id, recibido_en)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (event_id, event_type, payment_order_uid, result, booking_id, ahora()),
    ) == 1


# ---------------------------------------------------------------------------
# Bonos: reglas automaticas, bonos personales y lotes masivos
# ---------------------------------------------------------------------------
# Solo se CREAN y registran. Enviarlos por WhatsApp y canjearlos en la venta
# son etapas aparte (decision del 30/09/2026): cada bono tiene su fila en
# codigo_enviado con estado_envio 'pendiente' = generado, aun no enviado.

MAX_BONOS_POR_OPERACION = 2000


def guardar_regla(datos: dict, regla_id: int | None = None) -> int:
    """Crea o edita una regla (queda inactiva al crearla). Sin @seguro: es una accion del panel."""
    prefijo = bonos.normalizar_prefijo(datos["prefijo"])
    t = ahora()
    campos = (datos["nombre"].strip(), datos["disparador"], int(datos["umbral"]),
              1 if datos.get("repetir") else 0, datos["tipo"], int(datos["valor"]),
              datos.get("tope_centimos"), int(datos.get("vigencia_dias") or 30), prefijo)
    if regla_id is None:
        return db.escribir(
            """
            INSERT INTO regla_bono (nombre, disparador, umbral, repetir, tipo, valor,
                                    tope_centimos, vigencia_dias, prefijo, activa,
                                    creada_en, actualizada_en)
            VALUES (?,?,?,?,?,?,?,?,?,0,?,?)
            """,
            (*campos, t, t),
        )
    cambiadas = db.escribir_contando(
        """
        UPDATE regla_bono SET nombre = ?, disparador = ?, umbral = ?, repetir = ?, tipo = ?,
               valor = ?, tope_centimos = ?, vigencia_dias = ?, prefijo = ?, actualizada_en = ?
        WHERE id = ?
        """,
        (*campos, t, regla_id),
    )
    if not cambiadas:
        raise LookupError("Regla no encontrada.")
    return regla_id


def activar_regla(regla_id: int, activa: bool) -> None:
    if not db.escribir_contando("UPDATE regla_bono SET activa = ?, actualizada_en = ? WHERE id = ?",
                                (1 if activa else 0, ahora(), regla_id)):
        raise LookupError("Regla no encontrada.")


def crear_bono(documento: str, *, prefijo: str, tipo: str, valor: int,
               tope_centimos: int | None, vence_en: str, telefono: str | None = None,
               descripcion: str | None = None, regla_id: int | None = None,
               lote_id: int | None = None, hito: str | None = None) -> str | None:
    """
    Crea un bono personal y devuelve su codigo, o None si esa regla ya le dio
    el bono de ese hito. valor: porcentaje entero, o centimos si tipo='monto'
    (codigo_descuento guarda soles, como los codigos de siempre).
    """
    valor_tabla = valor / 100 if tipo == "monto" else valor
    tope_tabla = tope_centimos / 100 if tope_centimos else None
    for _ in range(6):
        codigo = bonos.generar_codigo(prefijo)
        try:
            with db.transaccion():
                db.escribir(
                    """
                    INSERT INTO codigo_descuento
                        (codigo, descripcion, tipo, valor, vence_en, usos_por_cliente,
                         tope_descuento, estado, creado_en, documento_exclusivo,
                         regla_id, lote_id, hito)
                    VALUES (?,?,?,?,?,1,?,'activo',?,?,?,?,?)
                    """,
                    (codigo, descripcion, tipo, valor_tabla, vence_en, tope_tabla, ahora(),
                     documento, regla_id, lote_id, hito),
                )
                db.escribir(
                    """
                    INSERT INTO codigo_enviado (codigo, documento, telefono, enviado_en, estado_envio)
                    VALUES (?,?,?,?,'pendiente')
                    """,
                    (codigo, documento, telefono, ahora()),
                )
            return codigo
        except sqlite3.IntegrityError:
            if regla_id is not None and db.consultar_uno(
                "SELECT 1 FROM codigo_descuento WHERE regla_id = ? AND documento_exclusivo = ? AND hito = ?",
                (regla_id, documento, hito),
            ):
                return None   # ya lo tenia: idempotente
            continue          # el codigo al azar ya existia: se prueba otro
    raise RuntimeError("No se pudo generar un código único.")


def _candidatos(regla: dict, documentos: list[str] | None, hoy) -> list[dict]:
    """Clientes que podrian haber ganado el bono; el filtro fino lo hace bonos.hito()."""
    d = regla["disparador"]
    if d == "viajes":
        condicion, params = "total_viajes >= ?", [regla["umbral"]]
    elif d == "gasto":
        condicion, params = "total_gastado * 100 >= ?", [regla["umbral"]]
    else:
        limite = (hoy - timedelta(days=int(regla["umbral"]))).isoformat()
        condicion, params = "ultima_compra IS NOT NULL AND substr(ultima_compra, 1, 10) <= ?", [limite]
    consulta = ("SELECT documento, telefono, total_viajes, total_gastado, ultima_compra "
                f"FROM cliente WHERE total_viajes > 0 AND {condicion}")
    if documentos:
        consulta += f" AND documento IN ({','.join('?' * len(documentos))})"
        params += list(documentos)
    return db.filas_a_dict(db.consultar(consulta, tuple(params)))


def aplicar_reglas(*, regla_id: int | None = None, documentos: list[str] | None = None,
                   disparadores: tuple[str, ...] | None = None, simular: bool = False) -> dict:
    """
    Revisa las reglas y crea los bonos que correspondan. Con simular=True solo
    cuenta (vista previa). Con regla_id se puede simular una regla inactiva.
    """
    if regla_id is not None:
        reglas = db.filas_a_dict(db.consultar("SELECT * FROM regla_bono WHERE id = ?", (regla_id,)))
        if not reglas:
            raise LookupError("Regla no encontrada.")
        if not simular and not reglas[0]["activa"]:
            raise ValueError("Activa la regla antes de aplicarla.")
    else:
        reglas = db.filas_a_dict(db.consultar("SELECT * FROM regla_bono WHERE activa = 1 ORDER BY id"))
    hoy = tiempo.hoy()
    salida, total = [], 0
    for r in reglas:
        if disparadores and r["disparador"] not in disparadores:
            continue
        nuevos = []
        for c in _candidatos(r, documentos, hoy):
            h = bonos.hito(r, c, hoy)
            if h and not db.consultar_uno(
                "SELECT 1 FROM codigo_descuento WHERE regla_id = ? AND documento_exclusivo = ? AND hito = ?",
                (r["id"], c["documento"], h),
            ):
                nuevos.append((c, h))
        if total + len(nuevos) > MAX_BONOS_POR_OPERACION:
            raise ValueError(f"Se crearían más de {MAX_BONOS_POR_OPERACION} bonos de una vez. Ajusta la regla.")
        creados = 0
        if not simular:
            vence = bonos.vence_en(r["vigencia_dias"], hoy)
            for c, h in nuevos:
                if crear_bono(c["documento"], prefijo=r["prefijo"], tipo=r["tipo"], valor=r["valor"],
                              tope_centimos=r["tope_centimos"], vence_en=vence, telefono=c["telefono"],
                              descripcion=r["nombre"], regla_id=r["id"], hito=h):
                    creados += 1
        total += len(nuevos)
        salida.append({"regla_id": r["id"], "nombre": r["nombre"],
                       "clientes": len(nuevos), "creados": creados})
    return {"simulado": simular, "total": total, "reglas": salida}


@seguro
def bonos_por_venta(documentos: list[str]) -> None:
    """Gancho de registrar_venta: reglas por boletos o gasto, solo para quienes acaban de comprar."""
    if documentos:
        aplicar_reglas(documentos=documentos, disparadores=("viajes", "gasto"))


def crear_lote(nombre: str, destinatarios: list[dict], *, prefijo: str, tipo: str, valor: int,
               tope_centimos: int | None, vigencia_dias: int, filtro: dict) -> dict:
    """Bonos masivos: uno por destinatario ({documento, telefono}). Sin @seguro: accion del panel."""
    if not destinatarios:
        raise ValueError("No hay clientes en ese grupo.")
    if len(destinatarios) > MAX_BONOS_POR_OPERACION:
        raise ValueError(f"Máximo {MAX_BONOS_POR_OPERACION} bonos por lote.")
    bonos.normalizar_prefijo(prefijo)   # valida antes de crear el lote
    vence = bonos.vence_en(vigencia_dias, tiempo.hoy())
    lote_id = db.escribir(
        "INSERT INTO lote_bono (nombre, filtro, cantidad, creado_en) VALUES (?,?,0,?)",
        (nombre.strip(), _json(filtro), ahora()),
    )
    creados = 0
    for d in destinatarios:
        if crear_bono(d["documento"], prefijo=prefijo, tipo=tipo, valor=valor,
                      tope_centimos=tope_centimos, vence_en=vence, telefono=d.get("telefono"),
                      descripcion=nombre.strip(), lote_id=lote_id):
            creados += 1
    db.escribir("UPDATE lote_bono SET cantidad = ? WHERE id = ?", (creados, lote_id))
    return {"lote_id": lote_id, "creados": creados, "vence_en": vence}


def anular_bono(codigo: str) -> None:
    """Un bono personal no canjeado deja de servir. Los codigos generales se pausan como siempre."""
    cambiadas = db.escribir_contando(
        """
        UPDATE codigo_descuento SET estado = 'anulado'
        WHERE codigo = ? AND documento_exclusivo IS NOT NULL AND estado = 'activo'
          AND NOT EXISTS (SELECT 1 FROM codigo_enviado e
                          WHERE e.codigo = codigo_descuento.codigo AND e.canjeado_en IS NOT NULL)
        """,
        (codigo.upper(),),
    )
    if not cambiadas:
        raise LookupError("No hay un bono activo sin canjear con ese código.")


# ---------------------------------------------------------------------------
# Administración (ciudades y pasarelas). Sin @seguro: son acciones del panel y
# quien las hace tiene que enterarse si no se guardaron.
# ---------------------------------------------------------------------------
def fijar_ciudad(codigo: str, activa: bool, motivo: str | None = None) -> None:
    """Saca una ciudad de la venta (activa=False) o la vuelve a poner."""
    if activa:
        db.escribir("DELETE FROM ciudad_cerrada WHERE codigo = ?", (codigo,))
    else:
        db.escribir(
            """INSERT INTO ciudad_cerrada (codigo, motivo, cerrada_en) VALUES (?, ?, ?)
               ON CONFLICT(codigo) DO UPDATE SET motivo = excluded.motivo""",
            (codigo, motivo, ahora()))


def fijar_pasarela(medio: str, activa: bool, motivo: str | None = None) -> None:
    """Apaga un medio de pago (activa=False) o lo vuelve a encender."""
    if activa:
        db.escribir("DELETE FROM pasarela_apagada WHERE medio = ?", (medio,))
    else:
        db.escribir(
            """INSERT INTO pasarela_apagada (medio, motivo, apagada_en) VALUES (?, ?, ?)
               ON CONFLICT(medio) DO UPDATE SET motivo = excluded.motivo""",
            (medio, motivo, ahora()))
