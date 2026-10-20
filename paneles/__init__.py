"""
Paneles de la Boleteria Virtual TMT.

Tres modulos de reporte sobre el bot de ventas de WhatsApp:
  1. ventas        - cuanto se vende, de que, y a que hora
  2. incidencias   - donde se cae la venta y que fallo
  3. fidelizacion  - quien compra seguido y como se le premia

Estructura:
  db.py            conexion SQLite + init del esquema
  esquema.sql      el DDL, unica fuente de verdad de la estructura
  registro.py      capa de escritura (la llama api_ventas.py)
  consultas.py     capa de lectura (la llama api_paneles.py)
  fidelizacion.py  codigos de descuento y envio por WhatsApp
  api_paneles.py   router FastAPI /api/paneles/...
  seed_demo.py     datos de ejemplo para trabajar en local
"""

from . import db  # noqa: F401

__all__ = ["db", "registro", "consultas", "fidelizacion", "api_paneles"]
