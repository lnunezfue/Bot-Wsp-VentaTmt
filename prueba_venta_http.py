"""
prueba_venta_http.py — prueba EN VIVO contra JELAF, de SOLO LECTURA.
=====================================================================

Comprueba, antes de pasar a VENTA_MODO=real, que la venta por HTTP tiene todo
lo que necesita:

  1. Inicia sesión (el navegador se abre SOLO para esto y se cierra) y revisa
     que se obtuvieron el Token, los datos del cajero (Idcaja, Terminal…) y los
     de la empresa.
  2. Busca un viaje real y lee su plano (precio, nivel, disponibilidad).
  3. Arma el cuerpo de bloquearAsiento, grabar-pasajero y grabar-venta con un
     pasajero de prueba, y lo guarda SIN DATOS PERSONALES en
     descubrimiento_output/ para compararlo con una venta real de boletería.

NO bloquea asientos, NO graba pasajeros y NO emite: solo usa consultas.

Uso:
    python prueba_venta_http.py
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import config as config_mod
from descubrir_endpoints_venta import redactar
from jelaf_http import CIUDADES_CODIGOS, ClienteJelafHttp, ViajeJelaf, a_decimal, parsear_turnos
from venta_jelaf import (
    POR_VALIDAR,
    AsientoVenta,
    DatosPago,
    Pasajero,
    asiento_en_plano,
    cliente_para_jelaf,
    cuerpo_bloqueo,
    cuerpo_venta,
)

SALIDA = Path("descubrimiento_output")
CLAVES_CAJERO = ("CodiEmpresa", "CodiSucursal", "CodiPuntoVenta", "CodiUsuario", "Terminal", "Idcaja",
                 "FechaLiq", "NomSucursal", "NomPuntoVenta")


async def preguntar(texto: str) -> str:
    return (await asyncio.to_thread(input, texto)).strip()


def marca(ok: bool) -> str:
    return "OK " if ok else "FALTA"


async def main() -> None:
    config_mod.configurar_salida_utf8()
    cfg = config_mod.cargar()
    usuario, password = cfg.jelaf_credenciales[0]
    jelaf = ClienteJelafHttp(usuario, password)

    print("\n1) Iniciando sesión en JELAF (por HTTP, sin navegador)...")
    await jelaf.iniciar()
    if not jelaf.disponible:
        print("   No se pudo iniciar sesión. Revisa JELAF_USUARIO / JELAF_PASSWORD.")
        return
    cajero = jelaf.cajero or {}
    for clave in CLAVES_CAJERO:
        print(f"   [{marca(cajero.get(clave) not in (None, ''))}] cajero.{clave}")
    print(f"   [{marca(bool(jelaf.empresa))}] datos de la empresa (get-empresas)")
    print(f"   [{marca(bool(jelaf.correlativo))}] tipo de terminal/impresora (busca-correlativo)")
    print(f"   → lista para vender: {'SÍ' if jelaf.lista_para_vender else 'NO'}")

    print("\n2) Viaje real")
    origen = (await preguntar("   Origen (LIMA/TACNA/AREQUIPA/CUSCO): ")).upper()
    destino = (await preguntar("   Destino: ")).upper()
    fecha_iso = await preguntar("   Fecha (YYYY-MM-DD): ")
    fecha_jelaf = "/".join(reversed(fecha_iso.split("-")))
    viajes = await asyncio.to_thread(jelaf.buscar_itinerarios, CIUDADES_CODIGOS[origen], fecha_jelaf,
                                     datetime.now().strftime("%I:%M %p").lstrip("0"))
    viajes = [v for v in viajes if destino in str(v.get("NomDestino", "")).upper()]
    if not viajes:
        print("   No hay viajes para esa ruta y fecha.")
        return
    for i, v in enumerate(viajes):
        print(f"   [{i}] {v.get('HoraPartida')}  {v.get('NomServicio')}  placa {v.get('PlacaBus')}")
    v = viajes[int(await preguntar("   Número de viaje: ") or 0)]
    viaje = ViajeJelaf(v.get("CodiEmpresa", 1), v.get("CodiOrigen", 0), v.get("CodiDestino", 0),
                       v.get("CodiSucursal", 0), v.get("CodiRuta", 0), v.get("CodiPuntoVenta", 0),
                       v.get("CodiServicio", 0), fecha_jelaf, v.get("HoraPartida"))
    turno = await asyncio.to_thread(jelaf.turno_crudo, viaje)
    estado = parsear_turnos(turno)
    print(f"   Programación {turno.get('CodiProgramacion')}, viaje {turno.get('NroViaje')}, "
          f"máx. bloqueos por bus: {turno.get('CantidadMaxBloqAsi')}, ocupados: {len(estado.ocupados)}")

    numero = (await preguntar("   Asiento libre para la prueba (no se bloquea): ")).zfill(2)
    plano = asiento_en_plano(turno, numero)
    if plano is None or numero in estado.ocupados:
        print("   Ese asiento no existe o está ocupado.")
        return
    precio = estado.precio_de(numero)
    print(f"   Asiento {numero}: precio S/ {precio}, normal S/ {a_decimal(plano.get('PrecioNormal'))}, "
          f"nivel {plano.get('Nivel')}")

    print("\n3) Cuerpos que se enviarían (NO se envían)")
    pasajero = Pasajero(documento="00000000", tipo_documento_web="01", nombres="PRUEBA", paterno="BOT",
                        materno="TMT", fecha_nacimiento="01/01/1990", sexo="M", telefono="900000000")
    asiento = AsientoVenta(numero=numero, precio=Decimal(precio),
                           precio_normal=a_decimal(plano.get("PrecioNormal")) or Decimal(precio),
                           nivel=int(plano.get("Nivel") or 1), ids_bloqueo=0, pasajero=pasajero)
    cuerpos = {
        "bloquearAsiento": cuerpo_bloqueo(turno, fecha_jelaf, numero),
        "grabar-pasajero": [cliente_para_jelaf(pasajero, date.today())],
        "grabar-venta": cuerpo_venta(turno=turno, cajero=cajero, empresa=jelaf.empresa or {},
                                     correlativo=jelaf.correlativo or {}, asientos=[asiento],
                                     pago=DatosPago(), fecha_viaje_jelaf=fecha_jelaf, hoy=date.today()),
    }
    SALIDA.mkdir(exist_ok=True)
    archivo = SALIDA / f"prueba_http_{datetime.now():%Y%m%d_%H%M%S}.json"
    archivo.write_text(json.dumps(redactar(cuerpos), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"   Guardado (sin datos personales): {archivo}")
    print("\n   Valores POR VALIDAR (confírmalos con una captura de boletería):")
    for clave, valor in POR_VALIDAR.items():
        print(f"     - {clave}: {valor!r}")
    print("\nListo. No se bloqueó, grabó ni emitió nada en JELAF.")


if __name__ == "__main__":
    asyncio.run(main())
