"""
boleto_pdf.py — PDF del boleto, generado SOLO a partir de una venta guardada.

Antes /api/v1/descargar-boleto armaba una "Boleta de venta electrónica" con lo
que viniera en la petición: cualquiera podía fabricar un boleto con los datos
de la empresa (V4). Ahora el endpoint busca la venta en la base y este módulo
solo dibuja lo que hay ahí.

El logo y el monto en letras ("SON: ... CON 00/100 SOLES") vienen del diseño de
boleta del compañero (CHATBOT PRUEBA, 17-18.html), que lo armaba en el
navegador con los datos de la sesión; aquí se dibujan en el servidor.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from io import BytesIO
from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

RUTA_LOGO = Path(__file__).resolve().parent / "recursos" / "logo.png"

TERMINOS = [
    "MAPFRE SEGUROS N° 3022600086975",
    "Adquirido el boleto de viaje no hay devolucion/embarcar 30 minutos antes, con su pasaje y DNI en fisico",
    "/menor de edad que viaja sin padres debe presentar autorizacion notarial/a partir de los 5 años a mas pagan",
    "boleto de viaje/la hora de embarque en escalas comerciales es referencial/EL PASAJERO no podra embarcar en",
    "estado de drogas o etilico/peso max. de equipaje 20kg. se paga el excedente/Solo se considera equipaje maletas",
    "El plazo maximo de postergacion es de 4 horas antes",
]


_UNIDADES = ["", "UNO", "DOS", "TRES", "CUATRO", "CINCO", "SEIS", "SIETE", "OCHO", "NUEVE"]
_DIEZ_A_VEINTINUEVE = [
    "DIEZ", "ONCE", "DOCE", "TRECE", "CATORCE", "QUINCE", "DIECISEIS", "DIECISIETE", "DIECIOCHO",
    "DIECINUEVE", "VEINTE", "VEINTIUNO", "VEINTIDOS", "VEINTITRES", "VEINTICUATRO", "VEINTICINCO",
    "VEINTISEIS", "VEINTISIETE", "VEINTIOCHO", "VEINTINUEVE",
]
_DECENAS = ["TREINTA", "CUARENTA", "CINCUENTA", "SESENTA", "SETENTA", "OCHENTA", "NOVENTA"]
_CENTENAS = ["CIENTO", "DOSCIENTOS", "TRESCIENTOS", "CUATROCIENTOS", "QUINIENTOS", "SEISCIENTOS",
             "SETECIENTOS", "OCHOCIENTOS", "NOVECIENTOS"]


def _hasta_999(n: int) -> str:
    if n == 100:
        return "CIEN"
    centenas, resto = divmod(n, 100)
    partes = [_CENTENAS[centenas - 1]] if centenas else []
    if resto >= 30:
        decenas, unidad = divmod(resto, 10)
        partes.append(_DECENAS[decenas - 3] + (f" Y {_UNIDADES[unidad]}" if unidad else ""))
    elif resto >= 10:
        partes.append(_DIEZ_A_VEINTINUEVE[resto - 10])
    elif resto:
        partes.append(_UNIDADES[resto])
    return " ".join(partes)


def monto_en_letras(monto) -> str:
    """70 -> 'SETENTA CON 00/100 SOLES', como en la boleta impresa de boletería."""
    d = Decimal(str(monto)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if d < 0 or d >= 1_000_000:
        raise ValueError(f"Monto fuera de rango para escribirlo en letras: {d}")
    entero = int(d)
    centimos = int((d - entero) * 100)
    miles, resto = divmod(entero, 1000)
    partes = []
    if miles == 1:
        partes.append("MIL")
    elif miles > 1:
        partes.append(re.sub(r"UNO$", "UN", _hasta_999(miles)) + " MIL")  # VEINTIUN MIL
    if resto:
        partes.append(_hasta_999(resto))
    return f"{' '.join(partes) or 'CERO'} CON {centimos:02d}/100 SOLES"


def nombre_archivo(nro_boleto: str | None, asiento: str) -> str:
    """Nombre de archivo seguro para Content-Disposition (sin comillas ni saltos de línea)."""
    base = re.sub(r"[^A-Za-z0-9-]", "", nro_boleto or "") or f"asiento-{asiento}"
    return f"boleto-{base[:40]}.pdf"


def titulo_comprobante(venta: dict) -> str:
    """Con RUC (venta.comprobante = factura) el comprobante de JELAF es una factura (serie FP…)."""
    return "FACTURA ELECTRÓNICA" if venta.get("comprobante") == "factura" else "BOLETA DE VENTA ELECTRÓNICA"


def generar(venta: dict, boleto: dict, emitido_en: datetime) -> bytes:
    """
    venta: fila de `venta` (origen, destino, sale_en, nro_boleto).
    boleto: fila de `boleto` (nombres, paterno, materno, documento, asiento, precio).
    """
    nro = boleto.get("nro_boleto") or venta.get("nro_boleto") or "POR CONFIRMAR"
    pasajero = " ".join(x for x in (boleto.get("nombres"), boleto.get("paterno"), boleto.get("materno")) if x).upper()
    origen = str(venta.get("origen") or "").upper()
    destino = str(venta.get("destino") or "").upper()
    sale_en = str(venta.get("sale_en") or "")
    fecha_viaje, _, hora_viaje = sale_en.partition("T")
    precio = Decimal(str(boleto.get("precio") or 0)).quantize(Decimal("0.01"))
    asiento = str(boleto.get("asiento") or "").zfill(2)

    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    if RUTA_LOGO.exists():
        # 512x148 px: se dibuja a 120 pt de ancho, arriba a la derecha.
        c.drawImage(ImageReader(str(RUTA_LOGO)), 450, 728, width=120, height=35, mask="auto")
    c.setFont("Helvetica-Bold", 14)
    c.drawString(40, 750, "EMPRESA DE TRANSPORTES MOQUEGUA TURISMO S.R.L.")
    c.setFont("Helvetica", 10)
    c.drawString(40, 735, "R.U.C. 20534857153")
    c.drawString(40, 720, "CAL. JOSE GALVEZ NRO. 591 LIMA - LIMA - LA VICTORIA")

    c.setFont("Helvetica-Bold", 12)
    c.drawString(40, 690, titulo_comprobante(venta))
    c.drawString(40, 675, str(nro))

    c.setFont("Helvetica", 10)
    c.drawString(40, 640, f"Fecha de emisión : {emitido_en.strftime('%d/%m/%Y')}    Hora : {emitido_en.strftime('%H:%M:%S')}")
    c.drawString(40, 625, "Tipo de moneda   : PEN")
    if venta.get("comprobante") == "factura":
        # La factura va al RUC; el pasajero (con su DNI) sale en la descripción.
        c.drawString(40, 610, f"R.U.C.           : {venta.get('ruc_cliente') or ''}")
        c.drawString(40, 595, f"Razón social     : {str(venta.get('razon_social') or '')[:70]}")
        c.drawString(40, 580, f"Dirección        : {str(venta.get('direccion_fiscal') or '')[:70]}")
        c.drawString(40, 565, f"EMBARQUE         : TERMINAL TERRESTRE {origen}")
    else:
        c.drawString(40, 610, f"Doc. Identidad   : {boleto.get('documento') or ''}")
        c.drawString(40, 595, f"Cliente          : {pasajero}")
        c.drawString(40, 580, f"EMBARQUE         : TERMINAL TERRESTRE {origen}")

    c.setFont("Helvetica-Bold", 10)
    c.drawString(40, 540, "DESCRIPCION")
    c.drawString(450, 540, "TOTAL")
    c.line(40, 535, 500, 535)

    c.setFont("Helvetica", 9)
    c.drawString(40, 520, f"POR EL SERVICIO DE TRANSPORTE DE LA RUTA {origen} - {destino}")
    c.drawString(40, 505, f"NRO ASIENTO: {asiento} / PASAJERO: {pasajero}"
                          + (f" / DOC: {boleto.get('documento') or ''}" if venta.get("comprobante") == "factura" else ""))
    c.drawString(40, 490, f"FECHA VIAJE: {fecha_viaje or '—'} / HORA VIAJE: {hora_viaje[:5] or '—'}")
    c.drawString(450, 520, f"S/ {precio:.2f}")

    c.line(40, 470, 500, 470)
    c.drawString(380, 450, "Op. Gravada    : S/ 0.00")
    c.drawString(380, 435, f"Op. Exonerada  : S/ {precio:.2f}")
    c.drawString(380, 420, "I.G.V. (18%)   : S/ 0.00")
    c.setFont("Helvetica-Bold", 10)
    c.drawString(380, 400, f"Importe Total  : S/ {precio:.2f}")
    c.setFont("Helvetica", 9)
    c.drawString(40, 380, f"SON: {monto_en_letras(precio)}")

    c.setFont("Helvetica", 7)
    y = 350
    for linea in TERMINOS:
        c.drawString(40, y, linea)
        y -= 10
    c.showPage()
    c.save()
    return buffer.getvalue()
