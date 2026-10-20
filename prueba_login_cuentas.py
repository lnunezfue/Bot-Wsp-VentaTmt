"""
prueba_login_cuentas.py — ¿entran a JELAF todas las cuentas del .env? (SOLO LOGIN)

Para las cuentas de venta (JELAF_USUARIO_1…, y la JELAF_USUARIO de reserva):
hace el mismo login por HTTP que el backend al arrancar, una cuenta a la vez y
con pausa entre una y otra (JELAF falla con logins muy seguidos). No consulta
viajes, no bloquea, no graba, no vende.

Muestra, por cuenta: si entró, su Terminal, su caja (Idcaja) y su sucursal /
punto de venta. Sirve además para saber si los usuarios comparten caja (hoy la
emisión es de a una entre todas las cuentas por no saberlo). Nunca muestra la
clave ni la cookie.

    python prueba_login_cuentas.py
"""

from __future__ import annotations

import time

import config as config_mod
from jelaf_http import login_http_jelaf

PAUSA_SEGUNDOS = 5


def main() -> int:
    config_mod.configurar_salida_utf8()
    cfg = config_mod.cargar()
    cuentas = cfg.jelaf_credenciales
    print(f"{len(cuentas)} cuenta(s) en el .env; reserva: {cfg.jelaf_cuenta_reserva or '(ninguna)'}\n")

    fallidas, cajas = [], {}
    for n, (usuario, password) in enumerate(cuentas):
        if n:
            time.sleep(PAUSA_SEGUNDOS)
        sesion = login_http_jelaf(usuario, password)
        if sesion is None:
            fallidas.append(usuario)
            print(f"  {usuario:<10} NO ENTRÓ (ver el log de arriba)")
            continue
        c = sesion.cajero or {}
        cajas.setdefault(str(c.get("Idcaja")), []).append(usuario)
        print(f"  {usuario:<10} OK   Terminal {c.get('Terminal')}   Idcaja {c.get('Idcaja')}   "
              f"{c.get('NomSucursal') or c.get('CodiSucursal')} / {c.get('NomPuntoVenta') or ''}")
        sesion.http.close()

    print()
    compartidas = {caja: us for caja, us in cajas.items() if len(us) > 1}
    if compartidas:
        print("Cajas compartidas:", "; ".join(f"Idcaja {k}: {', '.join(v)}" for k, v in compartidas.items()))
    elif len(cajas) > 1:
        print("Cada cuenta tiene su propia caja (Idcaja distinto).")
    print("Resultado:", "todas entraron." if not fallidas else f"no entraron: {', '.join(fallidas)}")
    return 1 if fallidas else 0


if __name__ == "__main__":
    raise SystemExit(main())
