"""
Modulo de fidelizacion: segmentos, codigos de descuento y su envio por WhatsApp.

Estado actual: TODO CORRE EN LOCAL. El envio real a Meta esta detras de la
variable de entorno PANELES_ENVIO_REAL. Mientras valga 0 (el valor por
defecto), enviar_codigo() hace exactamente lo mismo que en produccion -graba
el codigo_enviado, arma el texto del mensaje- pero en vez de llamar a la
Graph API lo imprime en consola. Asi el panel se puede usar completo sin
haber tramitado todavia la plantilla con Meta.

Para poder mandar el mensaje de verdad hacen falta dos cosas que NO dependen
de este archivo (ver docs/PLAN_PANELES.md):
  1. El telefono del cliente, que hoy no llega a la webapp de compra.
  2. Una plantilla de marketing aprobada por Meta, porque fuera de la ventana
     de 24 h de conversacion no se puede mandar texto libre.
"""

from __future__ import annotations

import os
import random
import string
from datetime import date, datetime

import requests

from . import db, tiempo
from .registro import ahora, seguro

# Nombre de la plantilla aprobada en Meta. Mientras no exista, el envio real
# fallara con un error explicito de la Graph API en vez de en silencio.
PLANTILLA_META = os.getenv("META_PLANTILLA_DESCUENTO", "descuento_fidelizacion")
IDIOMA_PLANTILLA = os.getenv("META_PLANTILLA_IDIOMA", "es")


def envio_real_activo() -> bool:
    return os.getenv("PANELES_ENVIO_REAL", "0") == "1"


# ---------------------------------------------------------------------------
# Codigos
# ---------------------------------------------------------------------------

def generar_codigo(prefijo: str = "TMT", largo: int = 4) -> str:
    sufijo = "".join(random.choices(string.ascii_uppercase + string.digits, k=largo))
    return f"{prefijo}{sufijo}"


def crear_codigo(
    codigo: str,
    valor: float,
    *,
    tipo: str = "porcentaje",
    descripcion: str | None = None,
    segmento_objetivo: str | None = None,
    vence_en: str | None = None,
    usos_por_cliente: int = 1,
    tope_descuento: float | None = None,
    estado: str = "borrador",
) -> dict:
    db.escribir(
        """
        INSERT INTO codigo_descuento
            (codigo, descripcion, tipo, valor, segmento_objetivo, vence_en,
             usos_por_cliente, tope_descuento, estado, creado_en)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(codigo) DO UPDATE SET
            descripcion       = excluded.descripcion,
            tipo              = excluded.tipo,
            valor             = excluded.valor,
            segmento_objetivo = excluded.segmento_objetivo,
            vence_en          = excluded.vence_en,
            usos_por_cliente  = excluded.usos_por_cliente,
            tope_descuento    = excluded.tope_descuento,
            estado            = excluded.estado
        """,
        (codigo.upper(), descripcion, tipo, valor, segmento_objetivo, vence_en,
         usos_por_cliente, tope_descuento, estado, ahora()),
    )
    return db.fila_a_dict(
        db.consultar_uno("SELECT * FROM codigo_descuento WHERE codigo = ?", (codigo.upper(),))
    )


TIPOS_CODIGO = ("porcentaje", "monto")
ESTADOS_CODIGO = ("borrador", "activo", "pausado", "vencido")
SEGMENTOS = ("nuevo", "ocasional", "frecuente", "dormido")


def cambiar_estado(codigo: str, estado: str) -> None:
    db.escribir("UPDATE codigo_descuento SET estado = ? WHERE codigo = ?", (estado, codigo.upper()))


# ---------------------------------------------------------------------------
# Validacion y aplicacion del descuento
# ---------------------------------------------------------------------------

def validar_codigo(codigo: str, documento: str) -> dict:
    """
    Devuelve {valido: bool, motivo: str|None, codigo: dict|None}.

    El documento es OBLIGATORIO: sin el, el limite de usos y el segmento no
    se pueden comprobar y antes se saltaban (N8). Nunca lanza: un codigo mal
    escrito o mal configurado es un caso normal, no un error del sistema.
    """
    if not documento:
        return {"valido": False, "motivo": "Falta el documento del pasajero.", "codigo": None}
    fila = db.consultar_uno(
        "SELECT * FROM codigo_descuento WHERE codigo = ?", ((codigo or "").upper().strip(),)
    )
    if fila is None:
        return {"valido": False, "motivo": "Ese código no existe.", "codigo": None}

    c = dict(fila)
    # Bono personal: para cualquier otro documento la respuesta es la misma
    # que si no aplicara, sin decir de quien es ni si sigue vigente.
    if c.get("documento_exclusivo") and c["documento_exclusivo"] != documento:
        return {"valido": False, "motivo": "Ese código no aplica a esta compra.", "codigo": None}
    if c["estado"] != "activo":
        return {"valido": False, "motivo": "Ese código no está activo.", "codigo": None}
    if c["vence_en"]:
        try:
            vencido = date.fromisoformat(c["vence_en"]) < tiempo.hoy()
        except ValueError:
            vencido = True   # fecha mal cargada: se trata como no vigente (N9)
        if vencido:
            return {"valido": False, "motivo": "Ese código ya venció.", "codigo": None}

    usos = db.consultar_uno(
        "SELECT COUNT(*) AS n FROM codigo_enviado WHERE codigo = ? AND documento = ? AND canjeado_en IS NOT NULL",
        (c["codigo"], documento),
    )
    if (usos["n"] or 0) >= c["usos_por_cliente"]:
        return {"valido": False, "motivo": "Ya usaste ese código.", "codigo": None}

    if c["segmento_objetivo"]:
        cli = db.consultar_uno("SELECT segmento FROM cliente WHERE documento = ?", (documento,))
        segmento = cli["segmento"] if cli else "nuevo"
        if segmento != c["segmento_objetivo"]:
            # Mensaje neutro: no revelar a que segmento pertenece el cliente (P7).
            return {"valido": False, "motivo": "Ese código no aplica a esta compra.", "codigo": None}

    return {"valido": True, "motivo": None, "codigo": c}


def aplicar_descuento(precio: float, codigo_dict: dict) -> tuple[float, float]:
    """Devuelve (precio_final, descuento). No toca la base."""
    if codigo_dict["tipo"] == "monto":
        descuento = min(codigo_dict["valor"], precio)
    else:
        descuento = precio * (codigo_dict["valor"] / 100.0)
    if codigo_dict.get("tope_descuento"):
        descuento = min(descuento, codigo_dict["tope_descuento"])
    descuento = round(descuento, 2)
    return round(precio - descuento, 2), descuento


# ---------------------------------------------------------------------------
# Destinatarios y envio
# ---------------------------------------------------------------------------

def destinatarios(segmento: str | None = None, limite: int = 500) -> list[dict]:
    """Clientes de un segmento que tienen telefono registrado."""
    sql = """
        SELECT documento, nombres, paterno, telefono, total_viajes, ultima_compra, segmento
        FROM cliente
        WHERE total_viajes > 0 AND telefono IS NOT NULL AND telefono <> ''
    """
    params: list = []
    if segmento:
        sql += " AND segmento = ?"
        params.append(segmento)
    sql += " ORDER BY total_viajes DESC LIMIT ?"
    params.append(limite)
    return db.filas_a_dict(db.consultar(sql, tuple(params)))


def texto_mensaje(cliente: dict, codigo: dict) -> str:
    """
    El texto que ve el cliente. Los parametros {{1}}..{{4}} de la plantilla de
    Meta se llenan en este mismo orden: nombre, descuento, codigo, vencimiento.
    """
    nombre = (cliente.get("nombres") or "").split(" ")[0].title() or "Hola"
    if codigo["tipo"] == "monto":
        descuento = f"S/ {codigo['valor']:.2f}"
    else:
        descuento = f"{codigo['valor']:.0f}%"
    vence = ""
    if codigo.get("vence_en"):
        vence = f", válido hasta el {datetime.fromisoformat(codigo['vence_en']).strftime('%d/%m/%Y')}"
    return (
        f"¡Hola {nombre}! 🚌 Gracias por viajar con Transportes Moquegua. "
        f"Te dejamos {descuento} de descuento en tu próximo pasaje con el código "
        f"{codigo['codigo']}{vence}."
    )


def parametros_plantilla(cliente: dict, codigo: dict) -> list[str]:
    nombre = (cliente.get("nombres") or "cliente").split(" ")[0].title()
    valor = f"S/ {codigo['valor']:.2f}" if codigo["tipo"] == "monto" else f"{codigo['valor']:.0f}%"
    vence = codigo.get("vence_en") or "sin fecha límite"
    return [nombre, valor, codigo["codigo"], vence]


@seguro
def enviar_codigo(codigo_str: str, documento: str) -> dict:
    """
    Registra el envio y dispara el mensaje. Devuelve el resultado del intento.

    Se registra SIEMPRE, aunque el envio falle: 'a quien se le intento mandar'
    es dato del panel tanto como 'a quien le llego'.
    """
    cod = db.fila_a_dict(
        db.consultar_uno("SELECT * FROM codigo_descuento WHERE codigo = ?", (codigo_str.upper(),))
    )
    if not cod:
        return {"ok": False, "motivo": "El código no existe."}

    cli = db.fila_a_dict(db.consultar_uno("SELECT * FROM cliente WHERE documento = ?", (documento,)))
    if not cli:
        return {"ok": False, "motivo": "El cliente no existe."}
    if not cli.get("telefono"):
        return {"ok": False, "motivo": "Ese cliente no tiene teléfono registrado."}

    mensaje = texto_mensaje(cli, cod)
    ok, error = _despachar(cli["telefono"], cli, cod, mensaje)

    db.escribir(
        """
        INSERT INTO codigo_enviado (codigo, documento, telefono, enviado_en, estado_envio, error_envio)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(codigo, documento) DO UPDATE SET
            enviado_en   = excluded.enviado_en,
            estado_envio = excluded.estado_envio,
            error_envio  = excluded.error_envio
        """,
        (cod["codigo"], documento, cli["telefono"], ahora(),
         "enviado" if ok else "fallido", error),
    )
    return {"ok": ok, "motivo": error, "mensaje": mensaje, "telefono": cli["telefono"]}


def enviar_campana(codigo_str: str, segmento: str | None = None, limite: int = 100) -> dict:
    """Envia el mismo codigo a todo un segmento. Devuelve el conteo."""
    objetivo = destinatarios(segmento, limite)
    enviados = fallidos = 0
    for cli in objetivo:
        r = enviar_codigo(codigo_str, cli["documento"])
        if r and r.get("ok"):
            enviados += 1
        else:
            fallidos += 1
    return {"destinatarios": len(objetivo), "enviados": enviados, "fallidos": fallidos,
            "simulado": not envio_real_activo()}


def _despachar(telefono: str, cliente: dict, codigo: dict, mensaje: str) -> tuple[bool, str | None]:
    """Llama a la Graph API, o simula el envio si PANELES_ENVIO_REAL no esta en 1."""
    if not envio_real_activo():
        print(f"[fidelizacion:SIMULADO] -> {telefono}\n    {mensaje}")
        return True, None

    token = os.getenv("WHATSAPP_TOKEN")
    phone_id = os.getenv("PHONE_NUMBER_ID")
    if not token or not phone_id:
        return False, "Faltan WHATSAPP_TOKEN o PHONE_NUMBER_ID en el entorno."

    payload = {
        "messaging_product": "whatsapp",
        "to": telefono,
        "type": "template",
        "template": {
            "name": PLANTILLA_META,
            "language": {"code": IDIOMA_PLANTILLA},
            "components": [{
                "type": "body",
                "parameters": [{"type": "text", "text": p}
                               for p in parametros_plantilla(cliente, codigo)],
            }],
        },
    }
    try:
        r = requests.post(
            f"https://graph.facebook.com/v19.0/{phone_id}/messages",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload, timeout=15,
        )
        if r.status_code >= 300:
            return False, f"Meta respondió {r.status_code}: {r.text[:300]}"
        return True, None
    except Exception as e:
        return False, str(e)
