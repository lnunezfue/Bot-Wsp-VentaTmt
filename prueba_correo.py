"""
prueba_correo.py — manda UN correo de prueba con los boletos (datos inventados).

Sirve para ver cómo le llega el correo a un cliente sin vender nada: no toca
JELAF, ni Yupy, ni Niubiz, ni la base. Arma el mismo correo que el bot
(correo.armar) con una venta de ejemplo y un PDF de ejemplo, y lo envía por
SMTP real (correo.enviar). ENVÍA UN CORREO REAL a la dirección que indiques.

Gmail: hace falta una "contraseña de aplicación" (cuenta con verificación en
dos pasos → https://myaccount.google.com/apppasswords). La clave normal de
Gmail NO sirve. Servidor smtp.gmail.com, puerto 465.

    python prueba_correo.py

Los datos del SMTP se piden en la consola (la clave sin mostrarse) o se toman
de SMTP_HOST / SMTP_PUERTO / SMTP_USUARIO / SMTP_PASSWORD / CORREO_REMITENTE
si ya están en el entorno. Nunca se escriben en disco.
"""

from __future__ import annotations

import getpass
import os
from datetime import datetime

import boleto_pdf
import correo


def pedir_config_smtp() -> correo.ConfigCorreo:
    """Datos del SMTP: del entorno o preguntados en la consola (la clave sin eco)."""
    host = os.getenv("SMTP_HOST") or input("Servidor SMTP [smtp.gmail.com]: ").strip() or "smtp.gmail.com"
    puerto = int(os.getenv("SMTP_PUERTO") or input("Puerto [465]: ").strip() or "465")
    usuario = os.getenv("SMTP_USUARIO") or input("Usuario (tu correo de Gmail): ").strip()
    clave = os.getenv("SMTP_PASSWORD") or getpass.getpass("Contraseña de aplicación (no se muestra): ").replace(" ", "")
    remitente = os.getenv("CORREO_REMITENTE") or usuario
    return correo.ConfigCorreo(host=host, puerto=puerto, usuario=usuario, password=clave,
                               remitente=remitente, nombre_remitente="Transportes Moquegua Turismo (PRUEBA)")


def venta_de_ejemplo() -> dict:
    ahora = datetime.now()
    return {
        "origen": "LIMA", "destino": "MOQUEGUA", "sale_en": "2026-10-20T15:00",
        "comprado_en": ahora.isoformat(timespec="seconds"), "monto_total": 290.0,
        "referencia_pago": "PRUEBA-000000", "comprobante": "boleta",
        "embarque_lugar": "TERMINAL ATOCONGO", "embarque_hora": "03:00PM",
        "boletos": [{"nombres": "Pasajero", "paterno": "De", "materno": "Prueba", "asiento": "43",
                     "precio": 290.0, "nro_boleto": "PRUEBA-0000001", "documento": "00000000"}],
    }


def main() -> None:
    print("Correo de PRUEBA con un boleto inventado (no se vende nada).\n")
    cfg = pedir_config_smtp()
    destino = input("¿A qué correo lo mando? ").strip()
    if not correo.correo_valido(destino):
        print("Ese correo no parece válido.")
        return
    venta = venta_de_ejemplo()
    boleto = venta["boletos"][0]
    pdf = boleto_pdf.generar(venta, boleto, datetime.now())
    mensaje = correo.armar(venta, [(boleto_pdf.nombre_archivo(boleto["nro_boleto"], "43"), pdf)],
                           destinatario=destino, remitente=cfg.remitente, nombre_remitente=cfg.nombre_remitente)
    mensaje.replace_header("Subject", "[PRUEBA] " + mensaje["Subject"])
    resultado = correo.enviar(mensaje, cfg)
    if resultado.ok:
        print(f"\nEnviado a {destino}. Revisa la bandeja de entrada (y spam/promociones).")
    else:
        print(f"\nNo se envió ({resultado.causa}): {resultado.error}")
        if "usuario o la clave" in (resultado.error or ""):
            print("Con Gmail usa una contraseña de aplicación, no la clave normal.")


if __name__ == "__main__":
    main()
