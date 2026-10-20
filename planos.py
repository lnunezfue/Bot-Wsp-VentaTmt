"""
Planos de asientos por plantilla de bus (planos_buses/*.json) y su mapeo
placa → plantilla. Formato en docs/data-planos-buses.md.

Se cargan una vez y se reutilizan (antes se releían todos los JSON en cada
búsqueda, hallazgo R2). Cambian muy rara vez: para recargarlos, reiniciar el
backend o llamar a `recargar()`.
"""

from __future__ import annotations

import copy
import json
import os
import threading
from decimal import Decimal

from jelaf_http import EstadoTurno

PLANOS_BUSES_DIR = "planos_buses"

_cache: dict | None = None
_lock = threading.Lock()


def normalizar_placa(placa: str | None) -> str:
    return "".join(ch for ch in (placa or "").upper() if ch.isalnum())


def _leer() -> dict:
    combinado = {"plantillas": {}, "placas": {}}
    if not os.path.isdir(PLANOS_BUSES_DIR):
        return combinado
    for nombre in sorted(os.listdir(PLANOS_BUSES_DIR)):
        if not nombre.endswith(".json"):
            continue
        with open(os.path.join(PLANOS_BUSES_DIR, nombre), "r", encoding="utf-8") as f:
            datos = json.load(f)
        combinado["plantillas"].update(datos.get("plantillas", {}))
        combinado["placas"].update(datos.get("placas", {}))
    combinado["placas"] = {normalizar_placa(k): v for k, v in combinado["placas"].items()}
    return combinado


def recargar() -> dict:
    global _cache
    with _lock:
        _cache = _leer()
        return _cache


def _datos() -> dict:
    return _cache if _cache is not None else recargar()


def placas_configuradas() -> set[str]:
    return set(_datos()["placas"].keys())


def plano_por_placa(placa: str) -> dict | None:
    """Copia profunda de los pisos del plano (el llamador puede modificarla)."""
    datos = _datos()
    codigo = datos["placas"].get(normalizar_placa(placa))
    plantilla = datos["plantillas"].get(codigo) if codigo else None
    return copy.deepcopy(plantilla.get("pisos")) if plantilla else None


def aplicar_estado(pisos: dict, estado: EstadoTurno) -> dict:
    """
    Marca los asientos ocupados y pone a cada piso el precio REAL de JELAF de
    sus propios asientos (buses de dos pisos con precios distintos, U4). Es el
    mismo precio que el servidor cobrará después: lo que el cliente ve es lo
    que paga.
    """
    for data_piso in pisos.values():
        precio_piso = Decimal("0")
        for fila in data_piso.get("rows", []):
            for celda in fila:
                if celda.get("type") != "seat":
                    continue
                asiento = str(celda.get("id", "")).zfill(2)
                if asiento in estado.ocupados:
                    celda["type"] = "occupied"
                if precio_piso == 0 and estado.precios.get(asiento):
                    precio_piso = estado.precios[asiento]
        precio = precio_piso or estado.precio_general
        if precio > 0:
            data_piso["price"] = float(precio)
        # Precio de cada asiento, para mostrarlo en el plano: en un mismo piso
        # JELAF tiene asientos de distinto precio (captura del 03/10). Solo es
        # informativo: lo que se cobra lo vuelve a calcular el servidor (V2).
        for fila in data_piso.get("rows", []):
            for celda in fila:
                if celda.get("type") in ("seat", "occupied"):
                    propio = estado.precios.get(str(celda.get("id", "")).zfill(2))
                    if propio or precio > 0:
                        celda["price"] = float(propio or precio)
    return pisos
