---
name: rpa-jelaf
description: Usar al modificar jelaf_http.py (login, llamadas HTTP a JELAF), venta_jelaf.py (cuerpos de bloqueo/pasajero/venta) o vendedor_http.py (VentaHttp, VendedorHttp, capacidad, emisión, reaper). Protege el contrato con JELAF, la concurrencia de las ventas simultáneas, los bloqueos de asientos y las credenciales.
---

# Venta en JELAF por HTTP (jelaf_http.py · venta_jelaf.py · vendedor_http.py)

## Antes de tocar
- Leer `docs/venta-sin-navegador.md` y `docs/descubrimiento-venta-http.md`.
- ¿El cambio **bloquea, graba o emite** en JELAF? Con `VENTA_MODO=real` es inventario y
  comprobantes electrónicos reales: avisar al usuario.
- El contrato de JELAF sale de capturas reales. **No adivinar campos ni valores**: lo no
  confirmado va en `venta_jelaf.POR_VALIDAR` y se confirma con `descubrir_endpoints_venta.py`.
  `tests/test_venta_jelaf.py` compara con la captura: si cambia, primero la captura.

## Concurrencia
- Cada venta es un `VentaHttp` con su propio lock; operar siempre con el lock tomado y el dueño
  re-verificado dentro (`es_dueno`).
- Solo `liberarArregloAsientos + grabar-venta` va bajo `VendedorHttp.lock_emision` (una caja).
  No meter otras llamadas lentas ahí: frena a todas las ventas.
- Las llamadas de `ClienteJelafHttp` son bloqueantes: desde código async, siempre
  `asyncio.to_thread`. Llamarlas en el loop lanza un error a propósito (el re-login deadlockearía).
- Respetar `CantidadMaxBloqAsi` del turno (suma de bloqueos de todas las ventas del bot).
- Todo camino que cierra una venta sin emitir suelta sus bloqueos (`liberar_y_resetear`).

## Escrituras
- `grabar_venta` sin reintento (`reintentar=False`). `ErrorJelafRechazo` = no se hizo;
  `ErrorJelaf` después de enviar = no se sabe → conciliar contra el plano, nunca reenviar.
- Revisar el plano fresco antes de `bloquearAsiento` (JELAF deja bloquear asientos ya vendidos).
- En modo simulación ninguna escritura sale a JELAF (`vendedor.simulacion`).

## Seguridad
- Credenciales, Token (`Authorization`) y cookies: nunca en logs, excepciones ni archivos.
- Los cuerpos guardados (simulación, pruebas) pasan por `descubrir_endpoints_venta.redactar`.
- `descubrimiento_output/` fuera del repo.

## Verificación
- `python -m pytest` (usa `JelafHttpFalso` con estado y fallos a pedido).
- Contra JELAF real, solo lectura: `python prueba_venta_http.py`, y lo ejecuta el usuario.
