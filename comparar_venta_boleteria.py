"""
comparar_venta_boleteria.py — grabar-venta de boletería vs el del bot, campo por campo (SIN CONEXIÓN).

Por qué existe (04/10/2026): las ventas del bot (BP58-00008349/50, ya anuladas)
se veían en JELAF sin nombres aunque la ficha del cliente sí los tenía. El
problema está en el cuerpo de grabar-venta. Este script compara:

  - la captura de una venta real de boletería (descubrir_endpoints_venta.py,
    descubrimiento_output/venta_*.json), y
  - el cuerpo que arma el bot (simulación, descubrimiento_output/simulacion_*.json).

Los dos archivos ya están sin datos personales (`redactar`): los textos se ven
como <texto:N> (N = largo). No llama a JELAF ni a nada.

    python comparar_venta_boleteria.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SALIDA = Path("descubrimiento_output")
# Las capturas de ventas reales de boletería (29/09) se hicieron desde CHATBOT
# PANELES, antes de que existiera esta versión: también se buscan ahí. Se puede
# indicar otra carpeta como argumento.
CARPETAS_CAPTURA = [SALIDA, Path("..") / "CHATBOT PANELES" / "descubrimiento_output"]


def interno(cuerpo):
    """{"request": {"<json-en-texto>": {...}}} -> {...}"""
    if isinstance(cuerpo, dict) and "request" in cuerpo:
        cuerpo = cuerpo["request"]
    if isinstance(cuerpo, dict) and "<json-en-texto>" in cuerpo:
        cuerpo = cuerpo["<json-en-texto>"]
    return cuerpo


# Llamadas a JELAF que hace el bot (jelaf_http.py). Lo que boletería haga y no
# esté aquí es candidato a ser lo que falta.
LLAMADAS_DEL_BOT = {
    "base/get-empresas", "base/obtener-fecha-sistema", "itinerarios/bloquearAsiento",
    "itinerarios/busca-correlativo", "itinerarios/consultar-usuario", "itinerarios/grabar-pasajero",
    "itinerarios/grabar-venta", "itinerarios/liberarArregloAsientos", "itinerarios/liberarAsiento",
    "itinerarios/lista-itinerarios", "itinerarios/turnos", "login/post-usuario", "base/get-usuarios",
}


def ruta_de(e: dict) -> str:
    ruta = str(e.get("ruta") or e.get("url") or "")
    ruta = ruta.split("?")[0]
    return ruta.split("/pasajes/")[-1].lstrip("/")


def secuencia(archivo: Path) -> None:
    """Todas las llamadas de la captura, en orden, marcando las que el bot NO hace."""
    eventos = json.loads(archivo.read_text(encoding="utf-8"))
    print("\n=== Llamadas de boletería durante la venta, en orden")
    anterior, faltan = None, []
    for e in eventos if isinstance(eventos, list) else []:
        if not isinstance(e, dict):
            continue
        if "marca" in e and "ruta" not in e:
            print(f"   ── marca: {e['marca']}")
            continue
        ruta = ruta_de(e)
        if not ruta or ruta == anterior:
            continue
        anterior = ruta
        hace = any(ruta.endswith(b) or ruta == b for b in LLAMADAS_DEL_BOT)
        if not hace:
            faltan.append(ruta)
        print(f"   {str(e.get('metodo', '')):6} {ruta:55} {'bot: sí' if hace else '<-- el bot NO la hace'}")
    print("\nLlamadas que el bot no hace:", ", ".join(dict.fromkeys(faltan)) or "ninguna")


# Llamadas que la pantalla de JELAF hace DURANTE la venta (después de elegir
# asiento) y el bot no: se muestra qué mandan y qué reciben, para replicarlas.
FASE_VENTA = ("consultaManifiestoProgramacion", "promocion/", "obtener-puertas-puntoventa",
              "get-acceso-usuario-modulo", "convertir-venta-to-base64", "grabar-log")


def detalle_fase_venta(archivo: Path) -> None:
    eventos = json.loads(archivo.read_text(encoding="utf-8"))
    print("\n=== Qué mandan y reciben las llamadas de la venta que el bot no hace")
    vistas = set()
    for e in eventos if isinstance(eventos, list) else []:
        if not isinstance(e, dict):
            continue
        ruta = ruta_de(e)
        if not any(f in ruta for f in FASE_VENTA) or ruta in vistas:
            continue
        vistas.add(ruta)
        print(f"\n--- {e.get('metodo', '')} {ruta}  (HTTP {e.get('estado', '?')})")
        completo = "convertir-venta-to-base64" in ruta   # sus ~60 campos hacen falta todos
        limite = None if completo else 900
        print("  manda:  ", json.dumps(e.get("cuerpo"), ensure_ascii=False)[:limite])
        print("  recibe: ", json.dumps(e.get("respuesta"), ensure_ascii=False)[:limite])

    # De la respuesta de grabar-venta salen los datos que se reenvían a
    # convertir-venta-to-base64: se muestra completa.
    for e in eventos if isinstance(eventos, list) else []:
        if isinstance(e, dict) and "grabar-venta" in ruta_de(e):
            print("\n--- respuesta de grabar-venta (completa)")
            print("  recibe: ", json.dumps(e.get("respuesta"), ensure_ascii=False))


def cuerpo_de(archivo: Path, parte_ruta: str):
    eventos = json.loads(archivo.read_text(encoding="utf-8"))
    for e in eventos if isinstance(eventos, list) else []:
        if isinstance(e, dict) and parte_ruta in ruta_de(e):
            return interno(e.get("cuerpo"))
    return None


def ficha_del_bot() -> dict:
    """grabar-pasajero del bot con un pasajero de ejemplo, ya sin datos personales."""
    from datetime import date
    from descubrir_endpoints_venta import redactar
    from venta_jelaf import Pasajero, cliente_para_jelaf
    p = Pasajero(documento="00000000", tipo_documento_web="01", nombres="NOMBRE DE PRUEBA", paterno="PATERNO",
                 materno="MATE", fecha_nacimiento="01/01/1990", sexo="M", telefono="900000000", id_cliente=1)
    return redactar(cliente_para_jelaf(p, date.today()))


def detalle_signalr(archivo: Path) -> None:
    """
    Cada mensaje de SignalR de la captura, con el paso en que ocurre. Las
    ventas del bot salen sin nombre aun con el mismo cuerpo, sesión y tiempos
    que boletería (diagnóstico del 05/10): lo único que la pantalla hace y el
    bot no son estos mensajes (hay uno justo antes de grabar-venta).
    """
    eventos = json.loads(archivo.read_text(encoding="utf-8"))
    print("\n=== Mensajes de SignalR de la pantalla, en orden")
    marca, anterior = "(inicio)", ""
    for e in eventos if isinstance(eventos, list) else []:
        if not isinstance(e, dict):
            continue
        if "marca" in e and "ruta" not in e:
            marca = e["marca"]
            continue
        ruta_completa = str(e.get("ruta") or e.get("url") or "")
        ruta = ruta_de(e)
        if "signalr" not in ruta:
            anterior = ruta
            continue
        print(f"\n--- [{marca}] después de {anterior}: {e.get('metodo', '')} {ruta}")
        consulta = ruta_completa.split("?", 1)[1] if "?" in ruta_completa else ""
        if consulta:
            print("  consulta:", consulta[:400])
        print("  manda:   ", json.dumps(e.get("cuerpo"), ensure_ascii=False))
        print("  recibe:  ", json.dumps(e.get("respuesta"), ensure_ascii=False)[:400])


def captura_boleteria(carpetas) -> tuple[Path, dict] | None:
    archivos = [a for c in carpetas if c.is_dir() for a in c.glob("venta_*.json")]
    for archivo in sorted(archivos, key=lambda a: a.name, reverse=True):
        try:
            eventos = json.loads(archivo.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for e in eventos if isinstance(eventos, list) else []:
            if isinstance(e, dict) and "grabar-venta" in str(e.get("ruta") or e.get("url") or ""):
                cuerpo = interno(e.get("cuerpo"))
                if isinstance(cuerpo, dict):
                    return archivo, cuerpo
    return None


def simulacion_bot() -> tuple[Path, dict] | None:
    archivos = sorted(SALIDA.glob("simulacion_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for archivo in archivos:
        try:
            return archivo, json.loads(archivo.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return None


def vacio(v) -> bool:
    return v in (None, "", "<texto:0>", [], {}) or v == 0 and not isinstance(v, bool)


def mostrar(v) -> str:
    texto = json.dumps(v, ensure_ascii=False)
    return texto if len(texto) <= 28 else texto[:25] + "…"


def comparar(titulo: str, boleteria: dict, bot: dict) -> list[str]:
    print(f"\n=== {titulo}")
    print(f"{'campo':26} {'boletería':30} {'bot':30}")
    importantes = []
    for campo in sorted(set(boleteria) | set(bot)):
        b, n = boleteria.get(campo, "<NO ENVÍA>"), bot.get(campo, "<NO ENVÍA>")
        marca = ""
        if campo not in bot:
            marca = "  <-- el bot NO lo envía"
            importantes.append(campo)
        elif campo not in boleteria:
            marca = "  (boletería no lo envía)"
        elif not vacio(b) and vacio(n):
            marca = "  <-- boletería lo LLENA, el bot lo deja vacío"
            importantes.append(campo)
        elif isinstance(b, str) and isinstance(n, str) and b.startswith("<texto:") and n.startswith("<texto:") and b != n:
            marca = "  (largo distinto)"
        elif b != n and not (isinstance(b, str) and b.startswith("<texto:"))                 and not (isinstance(n, str) and n.startswith("<texto:")):
            # Códigos visibles (no son datos personales) con VALOR distinto: JELAF
            # puede decidir por ellos (venta sin nombre del 04/10/2026).
            marca = "  <-- VALOR distinto"
            importantes.append(campo)
        if marca or campo in ("Nombre", "Dni", "TipoDocumento", "SwClienteNuevo", "CodiDocumento", "Tipo"):
            print(f"{campo:26} {mostrar(b):30} {mostrar(n):30}{marca}")
    return importantes


def main() -> int:
    carpetas = [Path(a) for a in sys.argv[1:]] or CARPETAS_CAPTURA
    cap, sim = captura_boleteria(carpetas), simulacion_bot()
    if not cap:
        print("No encontré una captura de boletería con grabar-venta (venta_*.json) en:")
        for c in carpetas:
            n = len(list(c.glob("venta_*.json"))) if c.is_dir() else 0
            print(f"  {c.resolve()}  ({'no existe' if not c.is_dir() else f'{n} captura(s), ninguna con grabar-venta'})")
        print("Si la captura está en otra carpeta: python comparar_venta_boleteria.py <carpeta>")
        return 1
    if not sim:
        print("No encontré una simulación del bot (simulacion_*.json). Haz una compra en VENTA_MODO=simulacion.")
        return 1
    print(f"Boletería: {cap[0].name}\nBot:       {sim[0].name}")
    b, n = cap[1], sim[1]
    cab_b = {k: v for k, v in b.items() if k != "Listado"}
    cab_n = {k: v for k, v in n.items() if k != "Listado"}
    item_b = (b.get("Listado") or [{}])[0]
    item_n = (n.get("Listado") or [{}])[0]
    item_b = item_b if isinstance(item_b, dict) else {}
    item_n = item_n if isinstance(item_n, dict) else {}
    pendientes = comparar("Cabecera de grabar-venta", cab_b, cab_n)
    pendientes += comparar("Listado[0] (el asiento)", item_b, item_n)

    ficha_b = cuerpo_de(cap[0], "grabar-pasajero")
    if isinstance(ficha_b, list) and ficha_b and isinstance(ficha_b[0], dict):
        pendientes += comparar("grabar-pasajero (la ficha del cliente)", ficha_b[0], ficha_del_bot())
    else:
        print("\n=== grabar-pasajero: boletería NO lo llamó en esta captura (o con otra forma):",
              json.dumps(ficha_b, ensure_ascii=False)[:200])
    secuencia(cap[0])
    detalle_fase_venta(cap[0])
    detalle_signalr(cap[0])
    print("\nPara revisar:", ", ".join(pendientes) or "ninguno")
    print("\n(Solo se leyeron archivos locales; no se llamó a JELAF.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
