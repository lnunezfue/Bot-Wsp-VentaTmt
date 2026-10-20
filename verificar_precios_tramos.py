"""
verificar_precios_tramos.py — ¿cada tramo muestra el precio del TRAMO o el de la ruta grande?

SOLO LECTURA. Para cada tramo que vende el bot pregunta al backend
(/api/v1/buscar-viajes, lo mismo que hace un cliente al buscar) los horarios
de una fecha con su precio "desde" (el asiento libre más barato del plano del
tramo, sacado de JELAF) y lo compara con el tarifario de JELAF exportado a
Excel (los "Formato_Precios" / lima-tac.xlsx, etc.):

  OK            el "desde" es uno de los precios del tarifario para ese tramo y servicio.
  RUTA GRANDE   no es precio del tramo pero sí de la ruta completa del bus (lo que hay que corregir).
  NO COINCIDE   no aparece ni en el tramo ni en la ruta completa (tarifario viejo o canal distinto).
  SIN PRECIO    JELAF no dio precio (turno cerrado o sin respuesta a tiempo).
  SIN TARIFARIO el Excel no tiene filas para ese tramo y servicio en esa fecha.

No bloquea, no vende, no abre planos. Pausa entre búsquedas para no cargar JELAF.

    python verificar_precios_tramos.py --fecha 2026-10-16 --excel C:/ruta/a/los/excels [--api URL] [--todos]

Sin --todos revisa solo los tramos entre ciudades principales (rutas.SEDES).
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import sys
import time
import urllib.request
from datetime import datetime

import rutas
from jelaf_http import CIUDADES_CODIGOS

API = "https://api.transportesmoquegua.com"


def _ciudades(texto) -> set[str]:
    return {c.strip().strip("*").strip().upper() for c in str(texto or "").split(",") if c.strip()}


def cargar_tarifario(carpeta: str, fecha: datetime) -> list[dict]:
    """Filas vigentes (ESTADO 1, fecha dentro de la vigencia) de todos los .xlsx de la carpeta."""
    import openpyxl
    filas = []
    for archivo in sorted(glob.glob(os.path.join(carpeta, "*.xlsx"))):
        ws = openpyxl.load_workbook(archivo, data_only=True).worksheets[0]
        datos = list(ws.iter_rows(values_only=True))
        if not datos or "PRECIO NORMAL" not in datos[0]:
            continue
        i = {c: n for n, c in enumerate(datos[0])}
        for r in datos[1:]:
            if not r or r[0] is None or str(r[i["ESTADO"]]) != "1":
                continue
            ini, fin = r[i["FECHA INI"]], r[i["FECHA FIN"]]
            if not (isinstance(ini, datetime) and isinstance(fin, datetime) and ini <= fecha <= fin):
                continue
            filas.append({
                "archivo": os.path.basename(archivo),
                "bus": (str(r[i["ORIGEN BUS"]]).upper(), str(r[i["DESTINO BUS"]]).upper()),
                "origen": _ciudades(r[i["ORIGEN PX"]]), "destino": _ciudades(r[i["DESTINO PX"]]),
                "servicio": str(r[i["SERVICIO"]] or "").upper(), "canal": str(r[i["CANAL"]] or ""),
                "nivel": r[i["NIVEL"]], "precio": float(r[i["PRECIO NORMAL"]] or 0),
            })
    return filas


def finales_de_ruta(origen: str, destino: str) -> set[str]:
    """Destino final de los buses que pasan por origen y luego por destino."""
    finales = set()
    for paradas in rutas.RUTAS.values():
        nombres = [p for p, _ in paradas]
        if origen in nombres and destino in nombres and nombres.index(origen) < nombres.index(destino):
            finales.add(nombres[-1])
    return finales


def precios(tarifario: list[dict], origen: str, destino: str, servicio: str) -> dict[float, set[str]]:
    """{precio: canales} del tarifario para ese tramo y servicio."""
    out: dict[float, set[str]] = collections.defaultdict(set)
    for f in tarifario:
        if f["servicio"] == servicio and origen in f["origen"] and destino in f["destino"]:
            out[f["precio"]].add(f["canal"])
    return out


def buscar(api: str, origen: str, destino: str, fecha: str) -> list[dict]:
    req = urllib.request.Request(f"{api}/api/v1/buscar-viajes", method="POST",
                                 data=json.dumps({"origen": origen, "destino": destino, "fecha": fecha}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read()).get("data") or []


def desde_del_plano(api: str, v: dict, fecha: str) -> float:
    """Asiento libre más barato del plano del tramo (/plano-bus: lo mismo que ve boletería). 0 = sin dato."""
    cuerpo = {k: v.get(k) for k in ("codi_empresa", "codi_destino", "codi_sucursal", "codi_ruta",
                                     "codi_punto_venta", "codi_servicio", "codi_origen")}
    cuerpo.update({"fecha_viaje": fecha, "hora_viaje": v.get("hora_partida"), "placa_bus": v.get("placa")})
    req = urllib.request.Request(f"{api}/api/v1/plano-bus", method="POST", data=json.dumps(cuerpo).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            datos = json.loads(r.read()).get("data") or {}
    except Exception:
        return 0.0
    precios = []

    def recorrer(x):
        if isinstance(x, dict):
            if x.get("type") == "seat" and x.get("price"):
                precios.append(float(x["price"]))
            for y in x.values():
                recorrer(y)
        elif isinstance(x, list):
            for y in x:
                recorrer(y)
    recorrer(datos)
    return min(precios) if precios else 0.0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--fecha", required=True, help="AAAA-MM-DD")
    ap.add_argument("--excel", required=True, help="carpeta con los .xlsx del tarifario")
    ap.add_argument("--api", default=API)
    ap.add_argument("--todos", action="store_true", help="también tramos con escalas menores")
    ap.add_argument("--pausa", type=float, default=1.5)
    ap.add_argument("--salida", default="", help="archivo .json con el detalle")
    ap.add_argument("--plano", action="store_true",
                    help="el 'desde' se calcula abriendo el plano de cada horario (más lento, independiente "
                         "de la versión del backend)")
    a = ap.parse_args()
    for flujo in (sys.stdout, sys.stderr):   # consola de Windows (cp1252): la flecha "→" rompía el print
        try:
            flujo.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    tarifario = cargar_tarifario(a.excel, datetime.fromisoformat(a.fecha))
    print(f"Tarifario: {len(tarifario)} filas vigentes al {a.fecha}.\n")
    principales = {c for c, d in rutas.SEDES.items() if d["principal"]}
    pares = [(o, d) for o in rutas.SEDES for d in rutas.destinos_desde(o)
             if o in CIUDADES_CODIGOS and d in CIUDADES_CODIGOS
             and (a.todos or (o in principales and d in principales))]
    resumen = collections.Counter()
    detalle = []
    for n, (o, d) in enumerate(pares, 1):
        try:
            viajes = buscar(a.api, o, d, a.fecha)
        except Exception as e:
            print(f"[{n}/{len(pares)}] {o}→{d}: ERROR {type(e).__name__}")
            resumen["ERROR"] += 1
            time.sleep(a.pausa)
            continue
        finales = finales_de_ruta(o, d) - {d}
        for v in viajes:
            serv = str(v.get("servicio") or "").upper()
            if a.plano:
                time.sleep(a.pausa / 2)
                desde = desde_del_plano(a.api, v, a.fecha)
            else:
                desde = float(v.get("precio_base") or 0)
            tramo = precios(tarifario, o, d, serv)
            grande = {p: c for f in finales for p, c in precios(tarifario, o, f, serv).items()}
            if v.get("bloqueado") or desde <= 0:
                estado = "SIN PRECIO"
            elif not tramo:
                estado = "SIN TARIFARIO"
            elif desde in tramo:
                estado = "OK"
            elif desde in grande:
                estado = "RUTA GRANDE"
            else:
                estado = "NO COINCIDE"
            resumen[estado] += 1
            fila = {"tramo": f"{o}-{d}", "hora": v.get("hora_partida"), "servicio": serv, "placa": v.get("placa"),
                    "desde": desde, "estado": estado, "tarifario_tramo": sorted(tramo),
                    "tarifario_ruta_grande": sorted(grande), "destinos_finales": sorted(finales)}
            detalle.append(fila)
            if estado not in ("OK", "SIN PRECIO"):
                print(f"  {estado:13} {o}→{d} {v.get('hora_partida')} {serv} {v.get('placa')}: desde S/ {desde:g} | "
                      f"tramo {sorted(tramo)} | ruta grande ({', '.join(sorted(finales))}) {sorted(grande)}")
        print(f"[{n}/{len(pares)}] {o}→{d}: {len(viajes)} horario(s)")
        time.sleep(a.pausa)
    print("\nResumen:", dict(resumen))
    if a.salida:
        with open(a.salida, "w", encoding="utf-8") as f:
            json.dump(detalle, f, ensure_ascii=False, indent=1)
        print("Detalle en", a.salida)


if __name__ == "__main__":
    main()
