"""
correo.py — los boletos por correo (SMTP).

Por qué (2026-10-14): el navegador de WhatsApp no deja descargar archivos
(prueba real en pruebas_whatsapp/prueba-pdf). La pantalla final solo MUESTRA
el boleto y el PDF llega por correo, con el detalle de compra en el mismo
formato que usa la empresa (empresa, comprador, pedido, total, ruta y tabla
de pasajero / comprobante / asiento / precio / embarque).

- `armar`: el mensaje (HTML + texto + un PDF por boleto). Función pura.
- `enviar`: SMTP con SSL (465) o STARTTLS (587). ENVÍA UN CORREO REAL: los
  tests reemplazan smtplib. Nunca lanza; la clave nunca sale en el error.
"""

from __future__ import annotations

import html
import logging
import re
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path

log = logging.getLogger("tmt.correo")

EMPRESA = "Empresa de Transportes Moquegua Turismo S.R.L."
TIMEOUT = 20
_CORREO = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}$")


def correo_valido(valor: str | None) -> bool:
    return bool(valor) and len(valor) <= 120 and bool(_CORREO.match(valor))


@dataclass(frozen=True)
class ConfigCorreo:
    host: str
    puerto: int
    usuario: str
    password: str
    remitente: str
    nombre_remitente: str

    @property
    def configurado(self) -> bool:
        return bool(self.host and self.usuario and self.password and self.remitente)


def _fecha(iso: str | None) -> tuple[str, str]:
    """'2026-08-21T15:00' → ('21/08/2026', '15:00')."""
    fecha, _, hora = str(iso or "").partition("T")
    partes = fecha.split("-")
    return ("/".join(reversed(partes)) if len(partes) == 3 else fecha), hora[:5]


def _nombre(b: dict) -> str:
    return " ".join(str(x) for x in (b.get("nombres"), b.get("paterno"), b.get("materno")) if x).upper()


def _soles(valor) -> str:
    try:
        return f"{float(valor or 0):.2f}"
    except (TypeError, ValueError):
        return "0.00"


# Colores del logo de TMT (recursos/logo.png).
VERDE_OSCURO = "#00663B"
VERDE = "#009120"
AMARILLO = "#FFB800"
RUTA_LOGO = Path(__file__).resolve().parent / "recursos" / "logo.png"

# Dirección de cada terminal de embarque, por ciudad de origen. Datos que pasó
# la empresa (correos de compra de referencia, 2026-10-14); las que faltan
# quedan en blanco hasta tenerlas: no se inventan.
DIRECCIONES_EMBARQUE = {
    "TACNA": "AV. HIPÓLITO UNANUE S/N INT. 06, TERMINAL MANUEL A. ODRÍA",
    "LIMA": "AV. PANAMERICANA SUR KM 11.300, COUNTER 24, VÍA AUXILIAR, SAN JUAN DE MIRAFLORES",
}

# Condiciones resumidas (las mismas de la ventana de Términos de la compra).
IMPORTANTE = (
    "En este correo te adjuntamos tu(s) boleto(s) en PDF. El comprobante electrónico también se puede "
    "consultar con el enlace de su número.",
    "Preséntate 30 minutos antes de la hora de viaje con tu boleto (impreso o en el celular) y tu documento "
    "de identidad físico. Si no llegas a tiempo, pierdes el boleto y el asiento se libera.",
    "Cambios y postergaciones: solo el titular, de forma presencial, hasta 4 horas antes del viaje, con su "
    "documento y su boleto. Por fuerza mayor, por WhatsApp al 953 954 708 (5:00 a. m. – 11:00 p. m.) dentro "
    "del mismo plazo.",
    "Una vez emitido el boleto no hay devolución por errores en los datos. Solo se devuelve si la empresa "
    "anula el servicio o te reubica y no aceptas la reubicación.",
    "Los menores de edad que viajan solos necesitan autorización notarial. Las gestantes desde los 8 meses, "
    "certificado médico de viaje.",
    "Equipaje de mano hasta 8 kg; de bodega hasta 20 kg sin costo (2 piezas).",
)


def _hora_ampm(hhmm: str) -> str:
    """'15:00' → '03:00PM'; si ya viene con AM/PM, igual."""
    texto = str(hhmm or "").strip()
    if not texto or "M" in texto.upper():
        return texto.upper().replace(" ", "")
    try:
        h, m = (int(x) for x in texto.split(":")[:2])
    except ValueError:
        return texto
    return f"{(h % 12) or 12:02d}:{m:02d}{'PM' if h >= 12 else 'AM'}"


def armar(venta: dict, adjuntos: list[tuple[str, bytes]], *, destinatario: str,
          remitente: str, nombre_remitente: str,
          url_terminos: str = "https://transportesmoquegua.com/terminos-pasajes.html") -> EmailMessage:
    """
    venta: fila de `venta` con sus `boletos` (consultas.venta_de_booking).
    Formato de los correos de compra que la empresa dio de referencia: cabecera
    con logo y "¡Se realizó su compra con éxito!", comprador, pedido y total,
    ruta con fecha, tabla de boletos, total neto, "Importante" y despedida.
    """
    e = html.escape
    boletos = venta.get("boletos") or []
    comprador = _nombre(boletos[0]) if boletos else ""
    fecha_compra, hora_compra = _fecha(venta.get("comprado_en"))
    fecha_viaje, hora_viaje = _fecha(venta.get("sale_en"))
    origen = str(venta.get("origen") or "").upper()
    ruta = f"{origen} - {str(venta.get('destino') or '').upper()}"
    embarque = str(venta.get("embarque_lugar") or origen).upper()
    hora_embarque = _hora_ampm(venta.get("embarque_hora") or hora_viaje)
    direccion = DIRECCIONES_EMBARQUE.get(origen, "")
    factura = venta.get("comprobante") == "factura"
    total = _soles(venta.get("monto_total"))
    logo_cid = make_msgid(domain="transportesmoquegua.com")
    hay_logo = RUTA_LOGO.exists()

    celda = "padding:10px 6px;border-top:1px solid #e3e3e3;font-size:13px;color:#555;vertical-align:middle"
    cabecera = f"padding:10px 6px;font-size:13px;font-weight:bold;color:{VERDE_OSCURO};text-align:left"

    def comprobante(b: dict) -> str:
        nro = e(str(b.get("nro_boleto") or "Por confirmar"))
        link = str(b.get("link_comprobante") or "")
        return (f'<a href="{e(link)}" style="color:{VERDE};text-decoration:underline">{nro}</a>'
                if link.startswith("https://") else nro)

    filas = "".join(
        "<tr>"
        f'<td style="{celda}">{e(_nombre(b))}</td>'
        f'<td style="{celda};text-align:center">{comprobante(b)}</td>'
        f'<td style="{celda};text-align:center">{e(str(b.get("asiento") or ""))}</td>'
        f'<td style="{celda};text-align:center">{_soles(b.get("precio"))}</td>'
        f'<td style="{celda};text-align:center">{e(embarque)}</td>'
        f'<td style="{celda};text-align:center">{e(direccion)}</td>'
        f'<td style="{celda};text-align:center">{e(hora_embarque)}</td>'
        "</tr>"
        for b in boletos
    )
    datos_factura = (
        f'<tr><td style="padding:2px 0"><b>RUC:</b> {e(str(venta.get("ruc_cliente") or ""))}</td>'
        f'<td style="padding:2px 0;text-align:right"><b>Razón social:</b> {e(str(venta.get("razon_social") or ""))}</td></tr>'
        if factura else ""
    )
    puntos = "".join(f'<li style="margin:0 0 10px">{e(p)}</li>' for p in IMPORTANTE) + (
        '<li style="margin:0 0 10px">Al recibir este comprobante aceptas los términos y condiciones del '
        f'contrato de transporte, publicados en nuestra <a href="{e(url_terminos)}" style="color:{VERDE};'
        'font-weight:bold">página web</a>.</li>')
    logo = (f'<img src="cid:{logo_cid[1:-1]}" alt="Transportes Moquegua Turismo" width="180" '
            'style="display:block;margin:0 auto 14px;width:180px;height:auto">' if hay_logo else "")

    cuerpo_html = f"""<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"></head><body style="margin:0;padding:20px 0;background:#f4f4f4">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f4"><tr><td align="center">
<table role="presentation" width="640" cellpadding="0" cellspacing="0" style="max-width:640px;width:100%;font-family:Arial,Helvetica,sans-serif;color:#333">
 <tr><td style="background:#fff;border:1px solid #9e9e9e;border-radius:6px;overflow:hidden">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
   <tr><td style="background:#e6e6e6;padding:24px 20px;text-align:center">
     {logo}<h1 style="margin:0;font-size:26px;color:#111">¡Se realizó su compra con éxito!</h1></td></tr>
   <tr><td style="padding:18px 28px 6px;text-align:center">
     <p style="margin:0 0 6px;font-size:15px;font-weight:bold;color:#666">Estimado(a):</p>
     <p style="margin:0 0 16px;font-size:26px;font-weight:bold;color:{VERDE_OSCURO}">{e(comprador)}</p></td></tr>
   <tr><td style="padding:0 28px 16px">
     <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="font-size:13px;color:#666">
       <tr><td style="padding:2px 0"><b>N° Pedido:</b> {e(str(venta.get("referencia_pago") or ""))}</td>
           <td style="padding:2px 0;text-align:right"><b>Fecha Transacción:</b> {e(fecha_compra)}</td></tr>
       <tr><td style="padding:2px 0"><b>Total Neto:</b> S/ {total}</td>
           <td style="padding:2px 0;text-align:right"><b>Hora Transacción:</b> {e(_hora_ampm(hora_compra))}</td></tr>
       {datos_factura}
     </table></td></tr>
   <tr><td style="background:#e6e6e6;padding:14px 20px;text-align:center;font-size:20px;font-weight:bold;color:#111">
     {e(ruta)} ({e(fecha_viaje)} {e(_hora_ampm(hora_viaje))})</td></tr>
   <tr><td style="padding:14px 16px 4px">
     <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">
       <tr><th style="{cabecera}">Pasajero</th><th style="{cabecera};text-align:center">Comprobante</th>
           <th style="{cabecera};text-align:center">Asiento</th><th style="{cabecera};text-align:center">Precio S/</th>
           <th style="{cabecera};text-align:center">Embarque</th><th style="{cabecera};text-align:center">Dirección</th>
           <th style="{cabecera};text-align:center">Hora Embarque</th></tr>
       {filas}
     </table></td></tr>
   <tr><td style="padding:18px 20px 26px;text-align:center;font-size:20px;color:#111">
     <b>Total Neto:</b> S/ {total}</td></tr>
  </table></td></tr>
 <tr><td style="padding:22px 4px 0">
   <h2 style="margin:0 0 12px;font-size:20px;color:{VERDE_OSCURO}">Importante</h2>
   <ul style="margin:0;padding-left:20px;font-size:13px;line-height:1.5;color:#555">{puntos}</ul>
   <p style="margin:18px 0 0;font-size:15px;font-weight:bold;font-style:italic;color:#777">Gracias por su compra</p>
 </td></tr>
</table></td></tr></table></body></html>"""

    texto = "\n".join([
        "¡Se realizó su compra con éxito!", "", f"Estimado(a): {comprador}", "",
        f"N° Pedido: {venta.get('referencia_pago') or ''}",
        f"Fecha y hora de la transacción: {fecha_compra} {_hora_ampm(hora_compra)}",
        f"Total Neto: S/ {total}", "",
        f"{ruta} ({fecha_viaje} {_hora_ampm(hora_viaje)})",
        *[f"- {_nombre(b)} | {b.get('nro_boleto') or 'Por confirmar'} | Asiento {b.get('asiento')} | "
          f"S/ {_soles(b.get('precio'))} | {embarque} {direccion} {hora_embarque}" for b in boletos],
        "", "IMPORTANTE", *[f"- {p}" for p in IMPORTANTE],
        f"- Términos y condiciones: {url_terminos}", "", "Gracias por su compra",
    ])

    msg = EmailMessage()
    msg["Subject"] = (f"Tus boletos: {str(venta.get('origen') or '').title()} → "
                      f"{str(venta.get('destino') or '').title()}, {fecha_viaje}")
    msg["From"] = formataddr((nombre_remitente, remitente))
    msg["To"] = destinatario
    msg["Message-ID"] = make_msgid(domain=remitente.rpartition("@")[2] or None)
    msg.set_content(texto)
    msg.add_alternative(cuerpo_html, subtype="html")
    if hay_logo:
        # El logo va dentro del correo (cid:), no como enlace: Gmail y Outlook bloquean las imágenes remotas.
        msg.get_payload()[1].add_related(RUTA_LOGO.read_bytes(), maintype="image", subtype="png",
                                         cid=logo_cid, filename="logo.png", disposition="inline")
    for nombre, datos in adjuntos:
        msg.add_attachment(datos, maintype="application", subtype="pdf", filename=nombre)
    return msg


@dataclass(frozen=True)
class Resultado:
    """
    ok, error (para el log y el panel; sin claves) y causa:
      "destinatario" = el servidor rechazó la dirección: el cliente puede corregirla.
      "servicio"     = problema nuestro (configuración, clave, red): no es culpa del cliente.
    Un correo mal escrito con dominio válido se acepta y rebota DESPUÉS: no se ve aquí.
    """
    ok: bool
    error: str | None = None
    causa: str | None = None


def enviar(msg: EmailMessage, cfg: ConfigCorreo) -> Resultado:
    """ENVÍA UN CORREO REAL. 465 = SSL directo; otro puerto = STARTTLS. Nunca lanza."""
    if not cfg.configurado:
        return Resultado(False, "Faltan SMTP_HOST, SMTP_USUARIO, SMTP_PASSWORD o CORREO_REMITENTE.", "servicio")
    contexto = ssl.create_default_context()
    try:
        if cfg.puerto == 465:
            with smtplib.SMTP_SSL(cfg.host, cfg.puerto, timeout=TIMEOUT, context=contexto) as s:
                s.login(cfg.usuario, cfg.password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(cfg.host, cfg.puerto, timeout=TIMEOUT) as s:
                s.starttls(context=contexto)
                s.login(cfg.usuario, cfg.password)
                s.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        return Resultado(False, "El servidor de correo rechazó el usuario o la clave (SMTP_USUARIO/SMTP_PASSWORD).",
                         "servicio")
    except smtplib.SMTPRecipientsRefused:
        return Resultado(False, "El servidor de correo rechazó la dirección del cliente.", "destinatario")
    except (smtplib.SMTPException, OSError) as e:
        return Resultado(False, f"No se pudo enviar el correo ({type(e).__name__}).", "servicio")
    return Resultado(True)
