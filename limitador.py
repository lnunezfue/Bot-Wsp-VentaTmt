"""
Límite de peticiones en memoria (ventana deslizante) para los endpoints
públicos: consultar-cliente (S1), iniciar venta (S3), validar códigos (N8),
telemetría.

Es deliberadamente simple: un solo proceso, sin Redis. El backend corre como
un único proceso (el pool de Chrome vive en él), así que un contador en
memoria es exacto. Si algún día hay varios procesos, esto tiene que pasar a
un almacén compartido.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request


class Limitador:
    def __init__(self, maximo: int, ventana_segundos: float, reloj=time.monotonic):
        self.maximo = maximo
        self.ventana = ventana_segundos
        self._reloj = reloj
        self._golpes: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def permitir(self, clave: str) -> bool:
        ahora = self._reloj()
        with self._lock:
            cola = self._golpes[clave]
            while cola and ahora - cola[0] > self.ventana:
                cola.popleft()
            if len(cola) >= self.maximo:
                return False
            cola.append(ahora)
            # Evita que el dict crezca sin fin con IPs que ya no vuelven.
            if len(self._golpes) > 10_000:
                for k in [k for k, v in self._golpes.items() if not v]:
                    del self._golpes[k]
            return True

    def exigir(self, clave: str) -> None:
        if not self.permitir(clave):
            raise HTTPException(status_code=429, detail="Demasiadas solicitudes. Espera un momento e inténtalo de nuevo.")


def ip_cliente(request: Request) -> str:
    """
    IP del cliente. No se confía en X-Forwarded-For: cualquiera puede
    inventarla. Si se publica detrás de un proxy, configurar uvicorn con
    --proxy-headers y --forwarded-allow-ips para que request.client ya sea la IP real.
    """
    return request.client.host if request.client else "desconocida"
