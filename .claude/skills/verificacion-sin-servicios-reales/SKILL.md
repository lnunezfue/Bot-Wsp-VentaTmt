---
name: verificacion-sin-servicios-reales
description: Usar al verificar cualquier cambio o al escribir tests en el proyecto TMT. Explica cómo probar api_ventas, el pool de workers y paneles con dobles, sin tocar JELAF, Yupy ni Meta, y cómo introducir pytest de forma gradual.
---

# Verificación sin servicios reales

## Regla de oro
Ningún test ni verificación llama a JELAF, Yupy o Meta, ni arranca Chrome. Si un cambio solo
puede validarse en vivo, decirlo y dejar que el usuario ejecute `prueba_v2_flujo.py`, indicando
qué observar.

## Mínimo en todo cambio
```bash
python -m py_compile *.py paneles/*.py
python -m pyflakes .
```
Mostrar la salida real.

## Dobles disponibles
- **App sin lifespan:** `httpx.AsyncClient(transport=httpx.ASGITransport(app=api_ventas.app), base_url="http://test")`.
  `ASGITransport` no ejecuta el lifespan, así que no arranca Chrome ni hace login.
- **Worker falso:** subclase de `JelafWorker` que sobrescribe los métodos de Playwright y devuelve
  datos fijos (viajes, asientos, precio, número de boleto) y cuenta cuántas veces se llamó a emitir.
  Asignar `api_ventas.pool_jelaf` a un pool con ese worker.
- **Yupy:** reemplazar `api_ventas.yupy` por `YupyFalso` (`tests/conftest.py`; `aprobar(tx, monto)` simula el cobro) para que devuelva
  pagado/no pagado/monto distinto según el caso.
- **Meta:** reemplazar `enviar_boton_compra` por un doble que registre llamadas.
- **Paneles:** `PANELES_DB` apuntando a `tmp_path / "test.db"`; `TestClient` sobre el router.
- **Reloj:** si hay expiraciones, inyectar la función de tiempo en vez de dormir.

## Si se introduce pytest
- Carpeta `tests/`, `conftest.py` con fixtures para: base temporal, app con pool falso, dobles de
  Yupy y Meta. Asegurar en `conftest.py` que las variables reales no se usen (p. ej. vaciar
  `WHATSAPP_TOKEN`, `YUPY_*`, forzar `PANELES_ENVIO_REAL=0`).
- Prioridad de tests (lo que más dinero/clientes cuesta si se rompe):
  1. `/confirmar`: una sola emisión con llamadas repetidas y concurrentes; no emite si el pago no
     cuadra.
  2. Pool: dueño del worker (C2), liberación en error/timeout/abandono.
  3. Webhook: firma, verify token, deduplicación.
  4. Paneles: autenticación, `@seguro`, esquema idempotente, sin PII en respuestas.
- **Bug corregido = test primero:** reproducir el fallo con un test que falle, corregir, mostrar
  que pasa.
- No mockear lo que se está probando; solo los bordes (JELAF, Yupy, Meta, reloj).
- Tests deterministas: sin red, sin hora actual, sin depender del orden.
- Nombres descriptivos en español: `test_confirmar_no_emite_dos_veces`.
- Datos sintéticos (DNI `00000001`, teléfonos ficticios).
- Si un test es inestable, reportarlo; no desactivarlo.
