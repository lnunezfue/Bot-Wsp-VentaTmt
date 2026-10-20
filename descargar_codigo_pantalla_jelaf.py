"""
descargar_codigo_pantalla_jelaf.py — baja el código de la pantalla de venta de JELAF (SOLO LECTURA).

Por qué existe (05/10/2026): cinco ventas reales del bot salieron sin nombre
aunque `Nombre` iba lleno, con la misma sesión, tiempos y cuerpo que boletería.
La venta de JELAF muestra Nombres/Paterno/Materno por separado pero recibe un
solo `Nombre`: el formato con que la pantalla lo arma (orden, separador) está en
su código. Este script lo descarga para leerlo; no envía nada a JELAF.

Hace: login por HTTP + GET de la pantalla de itinerarios (venta) y de sus
scripts propios (no las librerías conocidas). Guarda todo en codigo_jelaf/
(excluida de git: es código de JELAF y el repo es público) y muestra dónde se
arma la llamada a grabar-venta y el campo Nombre.

    python descargar_codigo_pantalla_jelaf.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import urljoin

import config as config_mod
from jelaf_http import URL_BASE, login_http_jelaf

SALIDA = Path("codigo_jelaf")
LIBRERIAS = ("jquery", "bootstrap", "vue.js", "vue.min", "lodash", "axios", "kendo", "moment", "sweetalert",
             "handlebars", "vee-validate", "vue-select", "vue-paginate", "v-mask", "datetimepicker", "slimscroll",
             "waves", "toast", "styleswitcher", "cbpFWTabs", "sidebar-nav", "pretty-checkbox", "signalR", "signalr")
PATRONES = (r"grabar-venta", r"grabar-pasajero", r"\.Nombre\s*=", r"Nombre\s*:", r"NombreCliente", r"PasNombreCom")


def mostrar_contexto(nombre: str, texto: str) -> None:
    for patron in PATRONES:
        for m in list(re.finditer(patron, texto))[:6]:
            ini = max(0, m.start() - 250)
            fin = min(len(texto), m.end() + 350)
            fragmento = re.sub(r"\s+", " ", texto[ini:fin])
            print(f"\n[{nombre}] «{patron}»\n  …{fragmento}…")


def main() -> int:
    config_mod.configurar_salida_utf8()
    cfg = config_mod.cargar()
    usuario, password = cfg.jelaf_credenciales[0]
    sesion = login_http_jelaf(usuario, password)
    if sesion is None:
        print("No se pudo iniciar sesión en JELAF.")
        return 1
    SALIDA.mkdir(exist_ok=True)
    (SALIDA / "LEEME.txt").write_text(
        "Código de la pantalla de venta de JELAF, descargado para entender el formato de grabar-venta.\n"
        "NO SUBIR AL REPOSITORIO (está en .gitignore).\n", encoding="utf-8")

    pagina = sesion.http.get(f"{URL_BASE}/itinerarios", timeout=30)
    print(f"Pantalla de venta: HTTP {pagina.status_code}, {len(pagina.text)} caracteres")
    (SALIDA / "itinerarios.html").write_text(pagina.text, encoding="utf-8")
    mostrar_contexto("itinerarios.html (scripts en la página)", pagina.text)

    fuentes = re.findall(r'<script[^>]+src="([^"]+)"', pagina.text)
    propias = [s for s in fuentes if not any(lib.lower() in s.lower() for lib in LIBRERIAS)]
    print(f"\nScripts de la página: {len(fuentes)} | propios de JELAF: {len(propias)}")
    for src in propias:
        url = urljoin(pagina.url, src)
        r = sesion.http.get(url, timeout=30)
        nombre = re.sub(r"[^A-Za-z0-9._-]", "_", src.split("?")[0].strip("/"))[-120:]
        (SALIDA / nombre).write_text(r.text, encoding="utf-8")
        print(f"  {r.status_code} {len(r.text):>8} caracteres  {src}")
        mostrar_contexto(nombre, r.text)

    print(f"\nGuardado en {SALIDA.resolve()} (no se envió nada a JELAF).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
