"""
Servidor SOLO de paneles, para trabajar en local.

    python servidor_paneles.py      ->  http://localhost:8010/paneles/

Levanta unicamente el router /api/paneles/... y el tablero estatico. No abre
Playwright, no entra a JELAF, no necesita credenciales ni token de Meta: se
alimenta de la base SQLite, asi que se puede desarrollar y revisar el diseño
de los tres paneles con los datos de ejemplo (python -m paneles.seed_demo).

En produccion NO se usa este archivo: ahi los paneles viajan dentro de
api_ventas.py, que ya monta el mismo router (ver docs/PLAN_PANELES.md).
"""

import os

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

import config
from paneles import db as paneles_db
from paneles.api_paneles import router as router_paneles

config.configurar_salida_utf8()

app = FastAPI(title="Paneles Boletería TMT (local)")
# El tablero se sirve desde este mismo origen: no hace falta abrir CORS a nadie más.
app.add_middleware(
    CORSMiddleware, allow_origins=["http://localhost:8010", "http://127.0.0.1:8010"],
    allow_credentials=False, allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-Paneles-Token"],
)
app.include_router(router_paneles)


@app.get("/", include_in_schema=False)
def raiz():
    return RedirectResponse("/paneles/")


if os.path.isdir("paneles_web"):
    app.mount("/paneles", StaticFiles(directory="paneles_web", html=True), name="paneles_web")


if __name__ == "__main__":
    paneles_db.init_db()
    print(f"📊 Base: {paneles_db.RUTA_DB}")
    print("📊 Tablero: http://localhost:8010/paneles/")
    uvicorn.run(app, host="127.0.0.1", port=8010)
