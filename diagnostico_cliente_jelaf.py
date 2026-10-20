"""
diagnostico_cliente_jelaf.py — qué guarda JELAF en la ficha de un cliente (SOLO LECTURA).

Por qué existe (04/10/2026): dos ventas reales (BP58-00008349 y 00008350)
quedaron en JELAF sin nombres. El bot manda los nombres en grabar-pasajero con
el `IdCliente` que lee de consultar-usuario; si ese campo no se llama así, se
manda 0, JELAF crea OTRA ficha y la venta muestra la vieja, sin nombres. Este
script confirma cómo responde consultar-usuario, sin vender nada.

Hace: login por HTTP + GET itinerarios/consultar-usuario del documento que
escribas. Muestra los NOMBRES de los campos y si vienen llenos o vacíos, nunca
los valores (Ley 29733). No bloquea, no graba, no vende.

    python diagnostico_cliente_jelaf.py
"""

from __future__ import annotations

import sys

import config as config_mod
from jelaf_http import URL_BASE, login_http_jelaf

CAMPOS_CLAVE = ("IdCliente", "NombreCliente", "ApellidoPaterno", "ApellidoMaterno",
                "FechaNacimiento", "Telefono", "Correo", "Sexo")


def estado(valor) -> str:
    if valor is None:
        return "nulo"
    if isinstance(valor, (int, float)):
        return f"número ({'mayor que 0' if valor > 0 else '0 o menos'})"
    texto = str(valor).strip()
    return "vacío" if not texto else f"lleno ({len(texto)} caracteres)"


def main() -> int:
    config_mod.configurar_salida_utf8()
    cfg = config_mod.cargar()
    usuario, password = cfg.jelaf_credenciales[0]
    documento = input("DNI a consultar (8 dígitos): ").strip()
    if not (documento.isdigit() and len(documento) == 8):
        print("Escribe un DNI de 8 dígitos.")
        return 2

    sesion = login_http_jelaf(usuario, password)
    if sesion is None:
        print("No se pudo iniciar sesión en JELAF (revisa la consola).")
        return 1
    r = sesion.http.get(f"{URL_BASE}/itinerarios/consultar-usuario",
                        params={"tipoDoc": "01", "numeroDoc": documento}, timeout=15)
    try:
        datos = r.json()
    except ValueError:
        print(f"consultar-usuario no devolvió JSON (HTTP {r.status_code}).")
        return 1

    valor = datos.get("Valor") if isinstance(datos, dict) else None
    print(f"\nHTTP {r.status_code} | EsCorrecto: {datos.get('EsCorrecto')} | Valor es: {type(valor).__name__}")
    if isinstance(valor, list):
        print(f"  ¡Valor es una LISTA con {len(valor)} ficha(s)! Se muestra la primera.")
        valor = valor[0] if valor and isinstance(valor[0], dict) else {}
    if not isinstance(valor, dict):
        print("  JELAF no devolvió una ficha para ese documento.")
        return 0

    print("\nCampos clave:")
    for campo in CAMPOS_CLAVE:
        print(f"  {campo:16} {'presente' if campo in valor else 'NO EXISTE':10} {estado(valor.get(campo))}")
    print("\nTodos los campos que trae la ficha (solo nombres):")
    print("  " + ", ".join(sorted(valor.keys())))
    posibles_id = [k for k in valor if "id" in k.lower() or "codi" in k.lower()]
    print("\nCampos que parecen un id:", ", ".join(f"{k} = {estado(valor[k])}" for k in posibles_id) or "ninguno")
    return 0


if __name__ == "__main__":
    sys.exit(main())
