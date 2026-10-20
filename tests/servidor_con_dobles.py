"""
Backend REAL (api_ventas.app) con JELAF, Yupy y Meta reemplazados por dobles.

Sirve para recorrer la mini-webapp en el navegador sin tocar ningún servicio
real: no abre Chrome, no entra a JELAF, no cobra y no manda WhatsApp. El
vendedor HTTP es el de verdad (vendedor_http.py): se pueden hacer varias
compras a la vez, una por pestaña.

    python tests/servidor_con_dobles.py [--capacidad 10] [--simulacion]
    python tests/servidor_estatico.py                         # mini-webapp, otra terminal
    http://localhost:5500/buscar-viaje.html    (origen TACNA, destino LIMA)

El pago de Yupy no se puede completar (su SDK es externo): para simular un
pago aprobado, POST /__dobles/aprobar-pago con {"booking_id": "..."}.

Con `--correo-real` pide los datos de un SMTP (p. ej. Gmail con contraseña de
aplicación) y manda de verdad el correo con los boletos de prueba al terminar
la compra. Sin esa opción, el correo no sale (queda "No pudimos enviarlo").

El pago con tarjeta (Niubiz) sí se recorre entero: el checkout.js es un doble
servido por este backend (/__dobles/niubiz-checkout.js) con un botón "Pagar
(simulado)". `--web http://localhost:5502` dice adónde vuelve Niubiz (pago.html).
"""

from __future__ import annotations

import dataclasses
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import conftest  # noqa: E402  (entorno de prueba: sin .env real, sin secretos reales)
import os  # noqa: E402

# Orígenes de la mini-webapp de prueba (5500 y 5502, ver servidor_estatico.py --api).
os.environ["CORS_ORIGENES"] = ",".join(f"http://{h}:{p}" for h in ("localhost", "127.0.0.1") for p in (5500, 5502))

import uvicorn  # noqa: E402
from fastapi import HTTPException, Response  # noqa: E402

import api_ventas  # noqa: E402
from paneles import db  # noqa: E402
from vendedor_http import VendedorHttp  # noqa: E402


class JelafDemo(conftest.JelafHttpFalso):
    def buscar_itinerarios(self, codi_origen, fecha, hora, codi_destino=0):
        base = {"CodiEmpresa": 1, "CodiOrigen": codi_origen, "CodiDestino": 1, "CodiSucursal": 2,
                "CodiRuta": 1, "CodiPuntoVenta": 17, "CodiServicio": 3, "NomDestino": "LIMA",
                "FechaPartida": fecha, "AsientosLibres": 30, "PrecioBase": 120}
        return [
            {**base, "CodiProgramacion": 101, "NomServicio": "PREMIUM", "HoraPartida": "08:30 PM", "PlacaBus": "B9J954"},
            {**base, "CodiProgramacion": 102, "NomServicio": "EJECUTIVO", "HoraPartida": "10:00 PM", "PlacaBus": "VBD290"},
        ]


def _arg(nombre: str, defecto: str) -> str:
    return sys.argv[sys.argv.index(nombre) + 1] if nombre in sys.argv else defecto


def main():
    # Base limpia en cada arranque (una por puerto): YupyFalso numera sus
    # transacciones desde yupy-0001 en cada proceso, y con la base de una corrida
    # anterior /checkout chocaba con UNIQUE(pago.yupy_transaction_id) y daba 503.
    db.RUTA_DB = Path(tempfile.gettempdir()) / f"tmt_sin_navegador_dobles_{_arg('--puerto', '8000')}.db"
    for sufijo in ("", "-wal", "-shm"):
        Path(f"{db.RUTA_DB}{sufijo}").unlink(missing_ok=True)
    db.init_db()
    # Los bloqueos del JELAF falso tampoco van al registro real (bloqueos_jelaf.db):
    # liberar_asientos.py los mostraría como si fueran de JELAF.
    import bloqueos_registro
    bloqueos_registro.RUTA = Path(tempfile.gettempdir()) / f"tmt_bloqueos_dobles_{_arg('--puerto', '8000')}.db"
    bloqueos_registro.RUTA.unlink(missing_ok=True)

    jelaf = JelafDemo()
    jelaf.precios["01"] = Decimal("150.00")   # asiento con precio distinto, para ver que manda el servidor
    capacidad = int(_arg("--capacidad", "10"))
    vendedor = VendedorHttp(jelaf, capacidad=capacidad, simulacion="--simulacion" in sys.argv)
    yupy = conftest.YupyFalso()

    api_ventas.pool_jelaf = vendedor
    api_ventas.jelaf = jelaf
    api_ventas.yupy = yupy
    api_ventas.niubiz = conftest.NiubizFalso()
    puerto = int(_arg("--puerto", "8000"))
    web = _arg("--web", "http://localhost:5502").rstrip("/")
    api_ventas.CONFIG = dataclasses.replace(
        api_ventas.CONFIG,
        niubiz_checkout_js=f"http://127.0.0.1:{puerto}/__dobles/niubiz-checkout.js",
        url_frontend=f"{web}/buscar-viaje.html",
    )
    if "--correo-real" in sys.argv:
        # Todo falso (JELAF, Yupy, Niubiz) MENOS el correo: los boletos (de
        # mentira) salen por SMTP real al correo que se escriba en la compra.
        import prueba_correo
        print("\n--correo-real: los boletos de prueba se enviarán por correo REAL.")
        cfg = prueba_correo.pedir_config_smtp()
        api_ventas.CONFIG = dataclasses.replace(
            api_ventas.CONFIG, smtp_host=cfg.host, smtp_puerto=cfg.puerto, smtp_usuario=cfg.usuario,
            smtp_password=cfg.password, correo_remitente=cfg.remitente,
            correo_nombre_remitente="Transportes Moquegua Turismo (PRUEBA)")
    api_ventas.enviar_boton_compra = lambda n, *a, **k: print(f"[doble] botón de compra a {n} (no enviado)")

    @api_ventas.app.get("/__dobles/niubiz-checkout.js")
    def niubiz_checkout_js():
        # Imita el checkout.js de Niubiz: botón dentro del <form> que, al
        # "pagar", envía el transactionToken al action (el backend autoriza).
        js = """
        (function () {
          var s = document.currentScript, form = s.closest('form');
          var b = document.createElement('button');
          b.type = 'button';
          b.textContent = 'Pagar S/ ' + s.getAttribute('data-amount') + ' (tarjeta simulada)';
          b.className = 'bg-blue-600 text-white font-bold py-3 px-6 rounded-xl w-full';
          b.onclick = function () {
            var t = document.createElement('input');
            t.type = 'hidden'; t.name = 'transactionToken'; t.value = 'tok-demo-' + Date.now();
            form.appendChild(t); form.submit();
          };
          form.appendChild(b);
        })();
        """
        return Response(content=js, media_type="application/javascript")

    @api_ventas.app.post("/__dobles/aprobar-pago")
    def aprobar(cuerpo: dict):
        pago = api_ventas.consultas.pago_de_booking(cuerpo.get("booking_id", ""))
        if not pago:
            raise HTTPException(404, "sin pago")
        yupy.aprobar(pago["yupy_transaction_id"])
        return {"aprobado": pago["yupy_transaction_id"]}

    @api_ventas.app.post("/__dobles/conciliar")
    async def conciliar():
        """Una pasada de la limpieza (este servidor corre sin lifespan: sin reaper ni conciliador solos)."""
        await api_ventas._pasada_de_limpieza()
        return {"ok": True}

    @api_ventas.app.get("/__dobles/jelaf")
    def ver_jelaf():
        """Lo que 'JELAF' tiene ahora: asientos bloqueados y vendidos, ventas recibidas."""
        return {"bloqueos": jelaf.bloqueos, "vendidos": sorted(jelaf.vendidos),
                "ventas": len(jelaf.ventas_recibidas), "llamadas": jelaf.llamadas}

    print(f"Backend con dobles en http://127.0.0.1:{puerto} (capacidad {capacidad}, "
          f"{'SIMULACIÓN' if vendedor.simulacion else 'venta contra JELAF falso'}, base: {db.RUTA_DB})")
    uvicorn.run(api_ventas.app, host="127.0.0.1", port=puerto, lifespan="off")


if __name__ == "__main__":
    main()
