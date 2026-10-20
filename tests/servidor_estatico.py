"""
Sirve la mini-webapp (carpeta frontend/) en local SIN caché del navegador.

`python -m http.server` no manda cabeceras de caché y el navegador sigue usando
las páginas viejas después de editarlas (se vio al corregir N15: la pestaña
seguía ejecutando la versión anterior). Este servidor manda
`Cache-Control: no-store`, así cada recarga trae lo último del disco.

    python tests/servidor_estatico.py                                  # http://localhost:5500
    python tests/servidor_estatico.py 5502 --api http://127.0.0.1:8001  # otro backend

--api reescribe al vuelo la URL del backend en config.js, para probar contra
un backend de pruebas sin tocar el que esté corriendo en :8000.
"""

from __future__ import annotations

import functools
import re
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent / "frontend"


API_ALTERNATIVA: str | None = None


class SinCache(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        if API_ALTERNATIVA and self.path.split("?")[0] == "/config.js":
            texto = (RAIZ / "config.js").read_text(encoding="utf-8")
            # Sea cual sea la URL que tenga config.js (local o la de producción).
            texto = re.sub(r"API_BASE_URL:\s*(['\"]).*?\1", f"API_BASE_URL: {API_ALTERNATIVA!r}", texto, count=1)
            cuerpo = texto.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(cuerpo)))
            self.end_headers()
            self.wfile.write(cuerpo)
            return
        super().do_GET()


def main():
    global API_ALTERNATIVA
    puerto = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 5500
    if "--api" in sys.argv:
        API_ALTERNATIVA = sys.argv[sys.argv.index("--api") + 1]
    manejador = functools.partial(SinCache, directory=str(RAIZ))
    with ThreadingHTTPServer(("127.0.0.1", puerto), manejador) as servidor:
        print(f"Mini-webapp sin caché en http://localhost:{puerto}/buscar-viaje.html")
        servidor.serve_forever()


if __name__ == "__main__":
    main()
