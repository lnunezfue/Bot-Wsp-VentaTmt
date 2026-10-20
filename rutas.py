"""
rutas.py — red de rutas de TMT y sus tramos (rutas cortas), sin JELAF.

Fuentes (2026-10-07):
  - "DATOS PARA VENTAS 1.xlsx" de la empresa (hojas PG-LIMA, PG-AREQUIPA y
    PG-TACNA, programación del 08/02/2026): orden de escalas y horas acumuladas.
  - Barrido de solo lectura en JELAF (lista-itinerarios de 18 orígenes, 3 días,
    y ListaDestinosRutaParaPdfUsuario de /turnos): confirmó esas rutas y agregó
    Cerro Azul, Fiscal y la ruta Tacna-Cusco por Puno y Juliaca (no está en el
    Excel). Las horas de esas paradas salen de la misma programación vista desde
    cada parada.

Cada ruta es la lista ORDENADA de sus paradas con la hora acumulada desde la
salida (None = no medida: el tramo existe pero su duración queda "por
confirmar"). Un tramo origen→destino existe si alguna ruta pasa por el origen
y, más adelante, por el destino: Ica→Nasca o Moquegua→Tacna, no solo de
terminal a terminal; Ilo→Moquegua no, porque ningún bus pasa por las dos en ese
orden.

Funciones puras: ni JELAF ni red. Qué ciudades se pueden vender de verdad lo
decide quien tenga sus códigos de JELAF (jelaf_http.CIUDADES_CODIGOS).
"""
from __future__ import annotations

# (parada, horas:minutos desde la salida o None), en el orden en que el bus pasa.
RUTAS: dict[str, list[tuple[str, str | None]]] = {
    "LIMA-AREQUIPA": [
        ("LIMA", "00:00"), ("CERRO AZUL", "02:30"), ("ICA", "05:00"), ("NASCA", "08:00"), ("YAUCA", "10:00"),
        ("CHALA", "11:00"), ("ATICO", "12:00"), ("CAMANA", "14:45"), ("ALTO SIGUAS", "16:00"),
        ("KM48", "17:15"), ("AREQUIPA", "18:15"),
    ],
    # Cerro Azul en los buses hacia Lima: 2:30 antes de llegar (como Lima→Cerro Azul).
    "AREQUIPA-LIMA": [
        ("AREQUIPA", "00:00"), ("KM48", "01:00"), ("ALTO SIGUAS", "02:15"), ("CAMANA", "03:15"),
        ("ATICO", "06:00"), ("CHALA", "07:00"), ("YAUCA", "08:00"), ("NASCA", "10:15"),
        ("ICA", "13:15"), ("CERRO AZUL", "15:45"), ("LIMA", "18:15"),
    ],
    "LIMA-MOQUEGUA-TACNA": [
        ("LIMA", "00:00"), ("CERRO AZUL", "02:30"), ("ICA", "05:00"), ("NASCA", "08:00"), ("YAUCA", "10:00"),
        ("CHALA", "11:00"), ("ATICO", "12:00"), ("CAMANA", "14:45"), ("MOLLENDO", "16:45"),
        ("MOQUEGUA", "19:15"), ("CAMIARA", "20:45"), ("TACNA", "22:15"),
    ],
    "TACNA-MOQUEGUA-LIMA": [
        ("TACNA", "00:00"), ("CAMIARA", "01:30"), ("MOQUEGUA", "03:00"), ("MOLLENDO", "05:30"),
        ("CAMANA", "07:30"), ("ATICO", "10:30"), ("CHALA", "12:00"), ("YAUCA", "13:00"),
        ("NASCA", "14:15"), ("ICA", "17:45"), ("CERRO AZUL", "20:15"), ("LIMA", "22:45"),
    ],
    "LIMA-ILO-TACNA": [
        ("LIMA", "00:00"), ("CERRO AZUL", "02:30"), ("ICA", "05:00"), ("NASCA", "08:00"), ("YAUCA", "10:00"),
        ("CHALA", "11:00"), ("ATICO", "12:00"), ("CAMANA", "14:45"), ("MOLLENDO", "16:45"),
        ("ILO", "18:45"), ("TACNA", "21:15"),
    ],
    "TACNA-ILO-LIMA": [
        ("TACNA", "00:00"), ("ILO", "02:30"), ("MOLLENDO", "04:30"), ("CAMANA", "06:30"),
        ("ATICO", "09:30"), ("CHALA", "11:00"), ("YAUCA", "12:00"), ("NASCA", "13:15"),
        ("ICA", "16:15"), ("CERRO AZUL", "18:45"), ("LIMA", "21:15"),
    ],
    # Fiscal: medido en JELAF (Arequipa +2:00 / Tacna +4:30).
    "AREQUIPA-TACNA": [
        ("AREQUIPA", "00:00"), ("KM48", "01:00"), ("FISCAL", "02:00"), ("MOQUEGUA", "04:00"),
        ("CAMIARA", "05:30"), ("TACNA", "07:00"),
    ],
    "TACNA-AREQUIPA": [
        ("TACNA", "00:00"), ("CAMIARA", "01:30"), ("MOQUEGUA", "03:00"), ("FISCAL", "04:30"),
        ("KM48", "06:00"), ("AREQUIPA", "07:00"),
    ],
    # Solo JELAF (CodiRuta 54). Tacna-Camiara-Moquegua como en las demás rutas
    # de Tacna; más allá no hubo horas confiables: por confirmar.
    "TACNA-CUSCO": [
        ("TACNA", "00:00"), ("CAMIARA", "01:30"), ("MOQUEGUA", "03:00"), ("PUNO", None),
        ("JULIACA", None), ("CUSCO", None),
    ],
    # Solo JELAF. Horas medidas (Cusco 3:00 PM → Camiara 5:30 AM); Tacna, 1:30
    # después de Camiara como en el Excel. JELAF lista Ilo entre Moquegua y
    # Camiara, pero el bus tarda 1 h entre esas dos (ir a Ilo y volver son
    # horas): no se ofrece, para no vender Moquegua→Ilo o Puno→Ilo en un bus
    # que no pasa por ahí.
    "CUSCO-TACNA": [
        ("CUSCO", "00:00"), ("JULIACA", "06:00"), ("PUNO", "07:30"), ("MOQUEGUA", "13:30"),
        ("CAMIARA", "14:30"), ("TACNA", "16:00"),
    ],
}

# Orden en que se muestran (de norte a sur por la costa, luego el sur y el
# altiplano) y nombre para la pantalla. `principal`: celda naranja del Excel
# ("oficinas principales") o ciudad con oficina en JELAF; las demás son escalas.
SEDES: dict[str, dict] = {
    "LIMA": {"nombre": "Lima", "principal": True},
    "CERRO AZUL": {"nombre": "Cerro Azul", "principal": False},
    "ICA": {"nombre": "Ica", "principal": True},
    "NASCA": {"nombre": "Nasca", "principal": True},
    "YAUCA": {"nombre": "Yauca", "principal": False},
    "CHALA": {"nombre": "Chala", "principal": False},
    "ATICO": {"nombre": "Atico", "principal": False},
    "CAMANA": {"nombre": "Camaná", "principal": False},
    "ALTO SIGUAS": {"nombre": "Alto Siguas", "principal": False},
    "KM48": {"nombre": "Km 48", "principal": False},
    "AREQUIPA": {"nombre": "Arequipa", "principal": True},
    "FISCAL": {"nombre": "El Fiscal", "principal": False},
    "MOLLENDO": {"nombre": "Mollendo", "principal": True},
    "ILO": {"nombre": "Ilo", "principal": True},
    "MOQUEGUA": {"nombre": "Moquegua", "principal": True},
    "CAMIARA": {"nombre": "Camiara", "principal": False},
    "TACNA": {"nombre": "Tacna", "principal": True},
    "PUNO": {"nombre": "Puno", "principal": True},
    "JULIACA": {"nombre": "Juliaca", "principal": True},
    "CUSCO": {"nombre": "Cusco", "principal": True},
}

# Las cuatro ciudades que se vendían antes de los tramos. Solo decide CÓMO se
# busca en JELAF (api_ventas.buscar_viajes), ya no qué pares se permiten.
# No hay Arequipa-Mollendo ni Arequipa-Ilo (la web oficial los lista, pero en
# JELAF todos los buses de Arequipa van a Lima o a Tacna) ni Lima/Arequipa-Cusco.
TERMINALES = ("LIMA", "TACNA", "AREQUIPA", "CUSCO")


def _minutos(hhmm: str) -> int:
    horas, minutos = hhmm.split(":")
    return int(horas) * 60 + int(minutos)


def duracion_minutos(origen: str, destino: str) -> int | None:
    """Minutos de viaje del tramo según la programación (el más corto si hay varias rutas)."""
    duraciones = [_minutos(horas[destino]) - _minutos(horas[origen])
                  for horas in (dict(e) for e in RUTAS.values() if _pasa_en_orden(e, origen, destino))
                  if horas[origen] is not None and horas[destino] is not None]
    return min(duraciones) if duraciones else None


def _pasa_en_orden(escalas: list, origen: str, destino: str) -> bool:
    nombres = [e for e, _ in escalas]
    return origen in nombres and destino in nombres and nombres.index(origen) < nombres.index(destino)


def tramo_valido(origen: str, destino: str) -> bool:
    """
    Hay bus de `origen` a `destino` si alguna ruta pasa por los dos en ese
    orden (no se viaja contra el sentido de la ruta).
    """
    if origen == destino or origen not in SEDES or destino not in SEDES:
        return False
    return any(_pasa_en_orden(e, origen, destino) for e in RUTAS.values())


def va_en_sentido(origen: str, destino: str, fin_de_ruta: str) -> bool:
    """
    ¿Un bus cuya ruta termina en `fin_de_ruta` (NomRuta de JELAF) lleva de
    `origen` a `destino`? JELAF, al buscar un tramo, devuelve también buses que
    pasan por las dos paradas en sentido contrario (Moquegua→Puno trae el bus
    Cusco→Tacna): esos no sirven. Sin `fin_de_ruta` conocido, no.
    """
    fin = str(fin_de_ruta or "").strip().upper()
    return any(e[-1][0] == fin and _pasa_en_orden(e, origen, destino) for e in RUTAS.values())


def destinos_desde(origen: str) -> list[str]:
    return [d for d in SEDES if tramo_valido(origen, d)]


def catalogo(habilitadas: set[str], cerradas: set[str] = frozenset()) -> dict:
    """
    Lo que necesita la pantalla de búsqueda: sedes (con `habilitada` = JELAF
    tiene su código y se puede vender), destinos posibles desde cada origen y
    la duración de cada tramo en minutos (para estimar la llegada).

    `cerradas`: ciudades que la empresa sacó de la venta desde el panel. No
    aparecen ni como origen ni como destino (el bus puede seguir pasando por
    ellas: solo no se venden pasajes desde ni hacia ahí).
    """
    abiertas = [c for c in SEDES if c not in cerradas]
    destinos = {o: [d for d in destinos_desde(o) if d not in cerradas] for o in abiertas}
    duraciones = {}
    for origen, lista in destinos.items():
        for destino in lista:
            minutos = duracion_minutos(origen, destino)
            if minutos is not None:
                duraciones[f"{origen}-{destino}"] = minutos
    return {
        "sedes": [{"codigo": c, "nombre": SEDES[c]["nombre"], "principal": SEDES[c]["principal"],
                   "habilitada": c in habilitadas} for c in abiertas],
        "destinos": destinos,
        "duraciones": duraciones,
    }
