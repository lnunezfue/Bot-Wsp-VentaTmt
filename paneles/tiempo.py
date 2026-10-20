"""
Hora oficial de los paneles: la de Lima, sin depender de la zona de la máquina.

En la PC de desarrollo datetime.now() es hora de Perú, pero en un servidor en
la nube suele ser UTC y el mapa de calor se correría 5 horas (hallazgo P4).
Perú no usa horario de verano desde 1994, así que un desfase fijo de -5 h es
exacto y evita depender del paquete tzdata en Windows.

Se guarda como texto ISO-8601 SIN zona ("2026-09-26T20:15:00"), igual que
antes: las consultas comparan esas cadenas y los datos viejos siguen valiendo.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

TZ_LIMA = timezone(timedelta(hours=-5), "America/Lima")


def ahora_dt() -> datetime:
    return datetime.now(TZ_LIMA).replace(tzinfo=None)


def ahora() -> str:
    return ahora_dt().isoformat(timespec="seconds")


def hoy() -> date:
    return ahora_dt().date()
