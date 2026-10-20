"""
diagnostico_venta_jelaf.py — cómo guardó JELAF una venta del bot vs una de boletería (SOLO LECTURA).

Por qué existe (04/10/2026): las ventas BP58-00008349/00008350 del bot se ven
en JELAF sin nombres, aunque la ficha del cliente sí los tiene (lo confirmó
diagnostico_cliente_jelaf.py). Entonces lo que falta está en la VENTA. El
plano del bus (itinerarios/turnos) trae, por cada asiento vendido, los datos de
su venta: comparando el asiento del bot con los de boletería se ve qué campo
queda vacío.

Hace: login por HTTP + búsqueda de viajes + plano del turno elegido. Muestra
los NOMBRES de los campos y si están llenos o vacíos, nunca los valores (Ley
29733). No bloquea, no graba, no vende.

    python diagnostico_venta_jelaf.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime

import config as config_mod
from jelaf_http import CIUDADES_CODIGOS, ClienteJelafHttp, ViajeJelaf, numero_de_celda, parsear_turnos


def lleno(valor) -> bool:
    return valor not in (None, "", 0, "0", [], {}) and str(valor).strip() != ""


def describir(valor) -> str:
    if not lleno(valor):
        return "vacío"
    return f"lleno ({len(str(valor).strip())})" if isinstance(valor, str) else f"lleno ({type(valor).__name__})"


async def preguntar(texto: str) -> str:
    return (await asyncio.to_thread(input, texto)).strip()


async def main() -> int:
    config_mod.configurar_salida_utf8()
    cfg = config_mod.cargar()
    usuario, password = cfg.jelaf_credenciales[0]
    jelaf = ClienteJelafHttp(usuario, password)
    await jelaf.iniciar()
    if not jelaf.disponible:
        print("No se pudo iniciar sesión en JELAF.")
        return 1

    origen = (await preguntar("Origen (LIMA/TACNA/AREQUIPA/CUSCO): ")).upper()
    destino = (await preguntar("Destino: ")).upper()
    fecha_iso = await preguntar("Fecha del viaje (YYYY-MM-DD): ")
    fecha_jelaf = "/".join(reversed(fecha_iso.split("-")))
    viajes = await asyncio.to_thread(jelaf.buscar_itinerarios, CIUDADES_CODIGOS[origen], fecha_jelaf,
                                     datetime.now().strftime("%I:%M %p").lstrip("0"))
    viajes = [v for v in viajes if destino in str(v.get("NomDestino", "")).upper()]
    if not viajes:
        print("No hay viajes para esa ruta y fecha.")
        return 0
    for i, v in enumerate(viajes):
        print(f"  [{i}] {v.get('HoraPartida')}  {v.get('NomServicio')}  placa {v.get('PlacaBus')}")
    v = viajes[int(await preguntar("Número de viaje: ") or 0)]
    asiento_bot = (await preguntar("Asiento que vendió el bot (ej. 11): ")).zfill(2)

    viaje = ViajeJelaf(v.get("CodiEmpresa", 1), v.get("CodiOrigen", 0), v.get("CodiDestino", 0),
                       v.get("CodiSucursal", 0), v.get("CodiRuta", 0), v.get("CodiPuntoVenta", 0),
                       v.get("CodiServicio", 0), fecha_jelaf, v.get("HoraPartida"))
    turno = await asyncio.to_thread(jelaf.turno_crudo, viaje)
    ocupados = parsear_turnos(turno).ocupados
    celdas = {numero_de_celda(c): c for c in (turno.get("ListaPlanoBus") or []) if isinstance(c, dict)}
    vendidos = {n: c for n, c in celdas.items() if n in ocupados}

    if asiento_bot not in vendidos:
        print(f"\nEl asiento {asiento_bot} no figura vendido en este viaje. Vendidos: {', '.join(sorted(vendidos)) or 'ninguno'}")
        return 0
    del_bot = vendidos[asiento_bot]
    otros = {n: c for n, c in vendidos.items() if n != asiento_bot}
    campos = sorted(set(del_bot) | {k for c in otros.values() for k in c})

    print(f"\nAsiento del bot: {asiento_bot} | otros vendidos (boletería u otros canales): {len(otros)}")
    print(f"\n{'campo':24} {'bot':14} lleno en los demás")
    diferencias = []
    for campo in campos:
        en_otros = sum(1 for c in otros.values() if lleno(c.get(campo)))
        marca = ""
        if otros and not lleno(del_bot.get(campo)) and en_otros == len(otros):
            marca = "  <-- vacío solo en el del bot"
            diferencias.append(campo)
        print(f"{campo:24} {describir(del_bot.get(campo)):14} {en_otros}/{len(otros)}{marca}")
    print("\nCampos vacíos SOLO en la venta del bot:", ", ".join(diferencias) or "ninguno")
    print("\nNo se bloqueó, grabó ni emitió nada en JELAF.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
