"""
Logica PURA de los bonos de descuento: armar codigos unicos y decidir si un
cliente gano un bono segun una regla. No toca la base (las escrituras estan
en registro.py), asi se prueba sin SQLite.

Un bono es un codigo personal: PREFIJO-XXXXX (p. ej. VUELVE10-K7Q3M), valido
solo para el documento al que se le dio. El sufijo usa un alfabeto sin
caracteres que se confunden al dictarlos o leerlos (0/O, 1/I/L).
"""

from __future__ import annotations

import re
import secrets
from datetime import date, timedelta

ALFABETO = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
LARGO_SUFIJO = 5
LARGO_MAX_PREFIJO = 12   # PREFIJO + "-" + 5 = 18 caracteres: entra en el limite de 20 del codigo

DISPARADORES = ("viajes", "gasto", "dormido")
TIPOS = ("porcentaje", "monto")


def normalizar_prefijo(prefijo: str | None) -> str:
    """Solo letras y numeros, en mayusculas. Lanza ValueError si queda vacio."""
    p = re.sub(r"[^A-Z0-9]", "", (prefijo or "").upper())[:LARGO_MAX_PREFIJO]
    if len(p) < 2:
        raise ValueError("El prefijo necesita al menos 2 letras o números.")
    return p


def generar_codigo(prefijo: str) -> str:
    """Codigo unico por cliente. secrets (no random): no se puede predecir el de otro cliente."""
    sufijo = "".join(secrets.choice(ALFABETO) for _ in range(LARGO_SUFIJO))
    return f"{normalizar_prefijo(prefijo)}-{sufijo}"


def vence_en(vigencia_dias: int, hoy: date) -> str:
    return (hoy + timedelta(days=vigencia_dias)).isoformat()


def hito(regla: dict, cliente: dict, hoy: date) -> str | None:
    """
    Que "logro" del cliente le da el bono, o None si todavia no lo gano.

    El hito es la clave de idempotencia: la base guarda un solo bono por
    (regla, cliente, hito). Con 'repetir', cada bloque completo de umbral es
    un hito nuevo (5 viajes → "1", 10 viajes → "2"). Para 'dormido' el hito es
    la fecha de su ultima compra: si vuelve a comprar y vuelve a dormirse, es
    otro periodo y gana otro bono.
    """
    umbral = int(regla["umbral"])
    if umbral <= 0:
        return None
    disparador = regla["disparador"]
    if disparador in ("viajes", "gasto"):
        cantidad = (int(cliente.get("total_viajes") or 0) if disparador == "viajes"
                    else round(float(cliente.get("total_gastado") or 0) * 100))
        if cantidad < umbral:
            return None
        return str(cantidad // umbral) if regla.get("repetir") else "1"
    if disparador == "dormido":
        ultima = (cliente.get("ultima_compra") or "")[:10]
        if not ultima:
            return None
        try:
            dias = (hoy - date.fromisoformat(ultima)).days
        except ValueError:
            return None
        return ultima if dias >= umbral else None
    return None


def descripcion_regla(regla: dict) -> str:
    """Texto corto para el panel: 'Cada 5 boletos → 10 %'."""
    u = int(regla["umbral"])
    d = regla["disparador"]
    if d == "viajes":
        cuando = f"{'Cada' if regla.get('repetir') else 'Al llegar a'} {u} {'boleto' if u == 1 else 'boletos'}"
    elif d == "gasto":
        cuando = f"{'Cada' if regla.get('repetir') else 'Al gastar'} S/ {u / 100:,.2f}"
    else:
        cuando = f"{u} días sin comprar"
    return f"{cuando} → {texto_descuento(regla['tipo'], regla['valor'])}"


def texto_descuento(tipo: str, valor: float | int, *, en_centimos: bool = True) -> str:
    if tipo == "monto":
        soles = valor / 100 if en_centimos else valor
        return f"S/ {soles:,.2f}"
    return f"{int(valor)} %"
