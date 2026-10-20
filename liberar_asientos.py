"""
liberar_asientos.py — ver y liberar asientos que el bot dejó bloqueados en JELAF.

Cuándo usarlo: un asiento aparece apartado en JELAF y nadie lo está comprando
(se cayó o se reinició el backend con una compra a medias, o JELAF no respondió
al soltarlo). El bot anota cada bloqueo en `bloqueos_jelaf.db`
(bloqueos_registro.py) y lo borra al soltarlo o venderlo; lo que queda ahí es
lo que este script muestra.

Qué hace:
  1. Lista los bloqueos registrados, por cuenta (Y1, Y2…): viaje, asiento y
     hace cuánto se hicieron.
  2. Inicia sesión con esas cuentas (login por HTTP, como el backend) y LEE el
     plano de cada viaje: si el asiento ya se vendió, lo saca de la lista y no
     lo toca.
  3. Libera los que elijas, cada uno con la cuenta y la Terminal que lo
     bloquearon (`liberarArregloAsientos`, la misma llamada que usa boletería),
     y pide confirmación antes de cada tanda.

Ojo: un bloqueo de los últimos minutos puede ser una compra EN CURSO (el
cliente llenando datos o pagando). Se marca y pide una confirmación extra.

    python liberar_asientos.py            # ver y liberar
    python liberar_asientos.py --listar   # solo ver, no libera nada

Toca JELAF real: lo ejecuta el usuario. No muestra claves, cookies ni datos
personales (el registro no los tiene).
"""

from __future__ import annotations

import asyncio
import sys
import time

import bloqueos_registro
import config as config_mod
from jelaf_http import CIUDADES_POR_CODIGO, ClienteJelafHttp, ErrorJelaf, ErrorJelafRechazo, ViajeJelaf, parsear_turnos

# Una compra puede seguir viva hasta ~30 min (selección + datos + pago).
MINUTOS_POSIBLE_COMPRA = 30


# --- Lógica (probada en tests/test_bloqueos_registro.py) ---------------------------- #
def revisar(registros: list[dict], clientes: dict) -> list[dict]:
    """
    Cruza el registro con el plano real. `clientes`: cuenta → cliente con sesión
    (o None si no pudo entrar). Saca del registro los asientos ya VENDIDOS y
    devuelve el resto con su estado: "bloqueado", "sin verificar", "sin sesión"
    o "sin cuenta".
    """
    planos: dict[str, set | None] = {}
    filas = []
    ahora = time.time()
    for r in registros:
        fila = {**r, "edad_min": int((ahora - r["creado_en"]) // 60)}
        fila["reciente"] = fila["edad_min"] < MINUTOS_POSIBLE_COMPRA
        cliente = clientes.get(r["cuenta"])
        if r["cuenta"] not in clientes:
            fila["estado"] = "sin cuenta"
        elif cliente is None:
            fila["estado"] = "sin sesión"
        else:
            clave = f'{r["cuenta"]}|{sorted(r["viaje"].items())}'
            if clave not in planos:
                try:
                    planos[clave] = parsear_turnos(cliente.turno_crudo(ViajeJelaf(**r["viaje"]))).ocupados
                except (ErrorJelaf, TypeError) as e:
                    print(f"  (no se pudo leer el plano de {ruta(r)}: {type(e).__name__})")
                    planos[clave] = None
            ocupados = planos[clave]
            if ocupados is not None and r["asiento"] in ocupados:
                bloqueos_registro.quitar([r["ids"]])   # vendido: ya no es un bloqueo pegado
                print(f'  Asiento {r["asiento"]} ({r["cuenta"]}, {ruta(r)}) ya está vendido: se quitó de la lista.')
                continue
            fila["estado"] = "bloqueado" if ocupados is not None else "sin verificar"
        filas.append(fila)
    return filas


def liberar(fila: dict, cliente) -> tuple[bool, str]:
    """Suelta UN bloqueo con la cuenta que lo hizo. Sin reintentos a ciegas."""
    terminal = int((cliente.cajero or {}).get("Terminal") or fila["terminal"])
    try:
        cliente.liberar_arreglo(terminal, [fila["ids"]])
    except ErrorJelafRechazo as e:
        return False, f"JELAF no lo liberó: {e}"
    except ErrorJelaf as e:
        return False, f"JELAF no respondió ({e}). Revisa el plano antes de volver a intentar."
    bloqueos_registro.quitar([fila["ids"]])
    return True, "liberado"


def elegir(texto: str, filas: list[dict]) -> list[int] | None:
    """'1,3' → filas 1 y 3; 'c Y1' → todas las de esa cuenta; 't' → todas. None si no se entiende."""
    texto = texto.strip().lower()
    if texto == "t":
        return list(range(len(filas)))
    if texto.startswith("c "):
        cuenta = texto[2:].strip()
        indices = [i for i, f in enumerate(filas) if str(f["cuenta"]).lower() == cuenta]
        return indices or None
    try:
        indices = sorted({int(x) - 1 for x in texto.replace(" ", "").split(",") if x})
    except ValueError:
        return None
    return indices if indices and all(0 <= i < len(filas) for i in indices) else None


# --- Consola -------------------------------------------------------------------- #
def ruta(r: dict) -> str:
    v = r["viaje"]
    origen = CIUDADES_POR_CODIGO.get(v.get("codi_origen"), v.get("codi_origen"))
    destino = CIUDADES_POR_CODIGO.get(v.get("codi_destino"), v.get("codi_destino"))
    return f'{origen}→{destino} {v.get("fecha_jelaf", "")} {v.get("hora_viaje", "")}'


def mostrar(filas: list[dict]) -> None:
    print(f'\n {"#":>2}  {"Cuenta":<8} {"Viaje":<34} {"Asiento":<7} {"IDS":<9} {"Hace":<8} Estado')
    print(" " + "-" * 86)
    for i, f in enumerate(filas, 1):
        aviso = "  ⚠ posible compra en curso" if f["reciente"] else ""
        print(f' {i:>2}  {f["cuenta"]:<8} {ruta(f):<34} {f["asiento"]:<7} {f["ids"]:<9} '
              f'{str(f["edad_min"]) + " min":<8} {f["estado"]}{aviso}')
    print()


def confirmar(pregunta: str, palabra: str = "s") -> bool:
    return input(f"{pregunta} ").strip().lower() == palabra


def iniciar_sesiones(cuentas: set[str], credenciales: dict[str, str]) -> dict:
    clientes = {}
    for n, cuenta in enumerate(sorted(cuentas)):
        if cuenta not in credenciales:
            continue
        if n:
            time.sleep(5)   # JELAF falla con logins muy seguidos
        print(f"Iniciando sesión en JELAF con {cuenta}…")
        cliente = ClienteJelafHttp(cuenta, credenciales[cuenta])
        asyncio.run(cliente.iniciar())
        clientes[cuenta] = cliente if cliente.disponible else None
        if not cliente.disponible:
            print(f"  {cuenta}: no pudo iniciar sesión.")
    return clientes


def main() -> int:
    config_mod.configurar_salida_utf8()
    solo_listar = "--listar" in sys.argv
    registros = bloqueos_registro.listar()
    if not registros:
        print("No hay asientos bloqueados por el bot en el registro. Nada que liberar.")
        return 0

    cfg = config_mod.cargar()
    credenciales = dict(cfg.jelaf_credenciales)
    clientes = iniciar_sesiones({r["cuenta"] for r in registros}, credenciales)
    filas = revisar(registros, clientes)
    if not filas:
        print("Todos los asientos registrados ya estaban vendidos; se limpiaron del registro.")
        return 0

    while True:
        mostrar(filas)
        if solo_listar:
            return 0
        print("Elige: números (ej. 1,3) · 'c Y1' = todos los de una cuenta · 't' = todos · 'r' = refrescar · 'q' = salir")
        opcion = input("> ").strip()
        if opcion.lower() in ("q", ""):
            return 0
        if opcion.lower() == "r":
            filas = revisar(bloqueos_registro.listar(), clientes)
            if not filas:
                print("No quedan asientos bloqueados en el registro.")
                return 0
            continue
        indices = elegir(opcion, filas)
        if indices is None:
            print("No entendí la opción.")
            continue
        elegidas = [filas[i] for i in indices]
        liberables = [f for f in elegidas if clientes.get(f["cuenta"])]
        for f in elegidas:
            if f not in liberables:
                print(f'  Asiento {f["asiento"]} ({f["cuenta"]}): {f["estado"]}, no se puede liberar desde aquí.')
        if not liberables:
            continue
        print("Se liberarán: " + ", ".join(f'{f["asiento"]} ({f["cuenta"]}, {ruta(f)})' for f in liberables))
        if not confirmar("¿Confirmas? (s/n)"):
            continue
        if any(f["reciente"] for f in liberables) and not confirmar(
                f"Hay bloqueos de hace menos de {MINUTOS_POSIBLE_COMPRA} min: puede haber un cliente comprando "
                "ahora mismo. Escribe SI para liberarlos igual:", "si"):
            continue
        for f in liberables:
            ok, mensaje = liberar(f, clientes[f["cuenta"]])
            print(f'  Asiento {f["asiento"]} ({f["cuenta"]}): {mensaje}')
            if not ok and isinstance(mensaje, str) and mensaje.startswith("JELAF no lo liberó") and confirmar(
                    "    ¿Quitarlo de la lista igual (ya no estaría bloqueado)? (s/n)"):
                bloqueos_registro.quitar([f["ids"]])
        filas = revisar(bloqueos_registro.listar(), clientes)
        if not filas:
            print("No quedan asientos bloqueados en el registro.")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
