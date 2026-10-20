"""
Enmascarado de datos personales para logs y mensajes (Ley N.° 29733).

Un único helper para no enmascarar "a mano" en cada print: DNI, teléfono e
identificadores de venta nunca van completos a la consola ni a respuestas de
diagnóstico.
"""

from __future__ import annotations

import re


def documento(valor: str | None) -> str:
    """'72677853' → '*****853'."""
    v = str(valor or "")
    if len(v) <= 3:
        return "*" * len(v)
    return "*" * (len(v) - 3) + v[-3:]


def telefono(valor: str | None) -> str:
    """'51987654321' → '+51 9** *** 321'."""
    d = re.sub(r"\D", "", str(valor or ""))
    if len(d) == 11 and d.startswith("51"):
        d = d[2:]
    if len(d) < 4:
        return "***"
    return f"+51 {d[0]}** *** {d[-3:]}"


def booking(valor: str | None) -> str:
    """Primeros 8 caracteres del booking_id: suficiente para correlacionar, inútil para secuestrar la venta (S2)."""
    v = str(valor or "")
    return (v[:8] + "…") if len(v) > 8 else v
