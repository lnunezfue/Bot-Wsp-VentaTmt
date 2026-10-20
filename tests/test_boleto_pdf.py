"""
Boleto en PDF: monto en letras ("SON: ... CON 00/100 SOLES") y logo, como en
la boleta impresa de boletería (diseño del compañero, CHATBOT PRUEBA 17-18.html),
pero generado en el servidor a partir de la venta guardada (V4).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

import boleto_pdf


@pytest.mark.parametrize("monto, letras", [
    (0, "CERO CON 00/100 SOLES"),
    (1, "UNO CON 00/100 SOLES"),
    (16, "DIECISEIS CON 00/100 SOLES"),
    (21, "VEINTIUNO CON 00/100 SOLES"),
    (35, "TREINTA Y CINCO CON 00/100 SOLES"),
    (70, "SETENTA CON 00/100 SOLES"),
    (100, "CIEN CON 00/100 SOLES"),
    (101, "CIENTO UNO CON 00/100 SOLES"),
    (120, "CIENTO VEINTE CON 00/100 SOLES"),
    (Decimal("85.50"), "OCHENTA Y CINCO CON 50/100 SOLES"),
    ("199.99", "CIENTO NOVENTA Y NUEVE CON 99/100 SOLES"),
    (500, "QUINIENTOS CON 00/100 SOLES"),
    (1000, "MIL CON 00/100 SOLES"),
    (1350, "MIL TRESCIENTOS CINCUENTA CON 00/100 SOLES"),
    (21000, "VEINTIUN MIL CON 00/100 SOLES"),
    (31000, "TREINTA Y UN MIL CON 00/100 SOLES"),
])
def test_monto_en_letras(monto, letras):
    assert boleto_pdf.monto_en_letras(monto) == letras


def test_monto_en_letras_redondea_al_centimo():
    assert boleto_pdf.monto_en_letras(Decimal("10.005")) == "DIEZ CON 01/100 SOLES"


@pytest.mark.parametrize("monto", [-1, 1_000_000])
def test_monto_fuera_de_rango_no_se_escribe_mal(monto):
    with pytest.raises(ValueError):
        boleto_pdf.monto_en_letras(monto)


VENTA = {"origen": "TACNA", "destino": "AREQUIPA", "sale_en": "2026-10-11T22:30", "nro_boleto": "BP58-00012345"}
BOLETO = {"nombres": "Ana", "paterno": "Paz", "materno": "", "documento": "12345678",
          "asiento": "5", "precio": "70.00", "nro_boleto": "BP58-00012345"}


def test_el_pdf_lleva_el_monto_en_letras_y_el_logo(monkeypatch):
    dibujado: list[str] = []
    imagenes: list[str] = []
    original_texto = boleto_pdf.canvas.Canvas.drawString
    original_imagen = boleto_pdf.canvas.Canvas.drawImage

    def texto(self, x, y, s, *a, **k):
        dibujado.append(str(s))
        return original_texto(self, x, y, s, *a, **k)

    def imagen(self, img, *a, **k):
        imagenes.append("logo")
        return original_imagen(self, img, *a, **k)

    monkeypatch.setattr(boleto_pdf.canvas.Canvas, "drawString", texto)
    monkeypatch.setattr(boleto_pdf.canvas.Canvas, "drawImage", imagen)
    pdf = boleto_pdf.generar(VENTA, BOLETO, datetime(2026, 10, 4, 18, 0))
    assert pdf.startswith(b"%PDF")
    assert "SON: SETENTA CON 00/100 SOLES" in dibujado
    assert imagenes == ["logo"]


def test_sin_logo_el_boleto_se_genera_igual(monkeypatch, tmp_path):
    monkeypatch.setattr(boleto_pdf, "RUTA_LOGO", tmp_path / "no-existe.png")
    assert boleto_pdf.generar(VENTA, BOLETO, datetime(2026, 10, 4, 18, 0)).startswith(b"%PDF")
