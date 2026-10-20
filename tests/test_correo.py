"""Correo con los boletos: contenido (como el detalle de compra de la empresa), adjuntos y envío."""

from __future__ import annotations

import correo

VENTA = {
    "origen": "LIMA", "destino": "MOQUEGUA", "sale_en": "2026-08-21T15:00", "comprado_en": "2026-08-21T11:04:00",
    "monto_total": 290.0, "referencia_pago": "ZP000000057726", "comprobante": "boleta",
    "embarque_lugar": "TERMINAL ATOCONGO", "embarque_hora": "03:00PM",
    "boletos": [{"nombres": "Serafin", "paterno": "Cuyo", "materno": "Villegas", "asiento": "43",
                 "precio": 290.0, "nro_boleto": "F996-0010500", "documento": "00000001"}],
}
CFG = correo.ConfigCorreo(host="mail.ejemplo.com", puerto=465, usuario="boletos@ejemplo.com",
                          password="clave", remitente="boletos@ejemplo.com", nombre_remitente="Transportes Moquegua Turismo")


def _texto_html(msg) -> str:
    return msg.get_body(("html",)).get_content()


def test_correo_con_el_detalle_de_compra_y_un_pdf_por_boleto():
    msg = correo.armar(VENTA, [("boleto-F996-0010500.pdf", b"%PDF-1.4 x")], destinatario="cliente@ejemplo.com",
                       remitente=CFG.remitente, nombre_remitente=CFG.nombre_remitente)
    assert msg["To"] == "cliente@ejemplo.com" and "boletos@ejemplo.com" in msg["From"]
    assert "Lima" in msg["Subject"] and "Moquegua" in msg["Subject"]
    html = _texto_html(msg)
    # Formato de los correos de compra que dio la empresa de referencia (2026-10-14).
    for dato in ("¡Se realizó su compra con éxito!", "Estimado(a):", "SERAFIN CUYO VILLEGAS", "ZP000000057726",
                 "Total Neto:", "290.00", "LIMA - MOQUEGUA (21/08/2026 03:00PM)", "F996-0010500", "43",
                 "TERMINAL ATOCONGO", "03:00PM", "PANAMERICANA SUR", "Importante", "Gracias por su compra",
                 "terminos-pasajes.html"):
        assert dato in html, dato
    assert 'src="cid:' in html                                        # logo dentro del correo
    adjuntos = list(msg.iter_attachments())
    assert [a.get_filename() for a in adjuntos] == ["boleto-F996-0010500.pdf"]
    assert adjuntos[0].get_content_type() == "application/pdf"
    logo = [p for p in msg.walk() if p.get_content_type() == "image/png"]
    assert len(logo) == 1 and logo[0]["Content-ID"]
    assert "SERAFIN CUYO VILLEGAS" in msg.get_body(("plain",)).get_content()


def test_numero_de_comprobante_enlaza_al_comprobante_electronico():
    venta = {**VENTA, "boletos": [{**VENTA["boletos"][0], "link_comprobante": "https://fe.ejemplo.com/x?id=1"}]}
    html = _texto_html(correo.armar(venta, [], destinatario="c@ejemplo.com", remitente="a@ejemplo.com",
                                    nombre_remitente="TMT"))
    assert 'href="https://fe.ejemplo.com/x?id=1"' in html


def test_factura_muestra_ruc_y_razon_social():
    venta = {**VENTA, "comprobante": "factura", "ruc_cliente": "20534857153", "razon_social": "EMPRESA CLIENTE S.A.C."}
    html = _texto_html(correo.armar(venta, [], destinatario="c@ejemplo.com", remitente="a@ejemplo.com",
                                    nombre_remitente="TMT"))
    assert "20534857153" in html and "EMPRESA CLIENTE S.A.C." in html


def test_los_datos_del_cliente_no_inyectan_html():
    venta = {**VENTA, "boletos": [{**VENTA["boletos"][0], "nombres": "<script>alert(1)</script>"}]}
    html = _texto_html(correo.armar(venta, [], destinatario="c@ejemplo.com", remitente="a@ejemplo.com",
                                    nombre_remitente="TMT"))
    assert "<script>" not in html and "&lt;SCRIPT&gt;" in html


def test_envio_por_ssl_con_login_y_sin_configuracion_no_envia(monkeypatch):
    enviados = []

    class SmtpFalso:
        def __init__(self, host, puerto, **k):
            self.host, self.puerto = host, puerto

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, usuario, clave):
            self.login_hecho = (usuario, clave)

        def send_message(self, msg):
            enviados.append((self.host, self.puerto, self.login_hecho, msg["To"]))
    monkeypatch.setattr(correo.smtplib, "SMTP_SSL", SmtpFalso)
    msg = correo.armar(VENTA, [], destinatario="cliente@ejemplo.com", remitente=CFG.remitente,
                       nombre_remitente=CFG.nombre_remitente)
    assert correo.enviar(msg, CFG) == correo.Resultado(True)
    assert enviados == [("mail.ejemplo.com", 465, ("boletos@ejemplo.com", "clave"), "cliente@ejemplo.com")]
    sin = correo.ConfigCorreo(host="", puerto=465, usuario="", password="", remitente="", nombre_remitente="")
    r = correo.enviar(msg, sin)
    assert not r.ok and "SMTP" in r.error and r.causa == "servicio" and len(enviados) == 1


def test_direccion_rechazada_se_distingue_de_un_problema_del_servicio(monkeypatch):
    # Al cliente solo se le pide corregir el correo si el problema es SU dirección.
    class Rechaza:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def login(self, *a): pass
        def send_message(self, msg):
            raise correo.smtplib.SMTPRecipientsRefused({msg["To"]: (550, b"No such user")})
    monkeypatch.setattr(correo.smtplib, "SMTP_SSL", Rechaza)
    msg = correo.armar(VENTA, [], destinatario="nadie@ejemplo.com", remitente="a@ejemplo.com", nombre_remitente="T")
    r = correo.enviar(msg, CFG)
    assert not r.ok and r.causa == "destinatario"

    class SinConexion(Rechaza):
        def __init__(self, *a, **k): raise OSError("sin red")
    monkeypatch.setattr(correo.smtplib, "SMTP_SSL", SinConexion)
    assert correo.enviar(msg, CFG).causa == "servicio"


def test_correo_valido():
    assert correo.correo_valido("cliente.uno@ejemplo.com.pe")
    assert not correo.correo_valido("sin-arroba") and not correo.correo_valido("a@b") and not correo.correo_valido("")
    assert not correo.correo_valido("a<b>@ejemplo.com")
