---
name: venta-y-pago
description: Usar al modificar endpoints de venta (v2 /api/v2/venta/..., v1 legacy), la integración con Yupy, /confirmar, el cálculo de precio o el PDF del boleto en api_ventas.py. Evita emisiones duplicadas, cobros sin verificar y manipulación de precio desde el navegador.
---

# Venta y pago (api_ventas.py)

## Contexto que no se puede olvidar
- `/api/v2/venta/confirmar` **emite boletos reales en JELAF** (la ruta v1 `confirmar-compra` se eliminó; no reintroducirla).
- El frontend es HTML estático: todo lo que manda (precio, asiento, estado en `localStorage`)
  es manipulable por el cliente.
- Leer V1/V2 (y relacionados) en `docs/AUDITORIA.md` y su estado en `docs/REVISION_*.md`.

## Reglas
1. **Precio en el servidor.** El precio se toma de JELAF/del estado del worker al seleccionar
   asiento y se guarda del lado del servidor asociado al `booking_id`. Cualquier precio que venga
   en la petición se ignora (o se usa solo para detectar discrepancia y rechazar).
2. **Pago verificado servidor a servidor.** Antes de emitir, consultar a Yupy (`yupy.consultar_orden` + `pagos_yupy.evaluar_orden` contra la tabla `pago`)
   y exigir: estado pagado, **monto == precio del servidor**, moneda `PEN`, referencia que
   corresponde a ese `booking_id`, y que la referencia de pago no se haya usado antes.
   Nunca confiar en un "pagado" enviado por el frontend ni en el redirect.
3. **Idempotencia de `/confirmar`.** Guardar estado por `booking_id` (`pendiente → emitiendo →
   emitido | fallido`). Si llega otra llamada:
   - `emitiendo` → responder "en proceso", no relanzar.
   - `emitido` → devolver el mismo boleto.
   El cambio a `emitiendo` debe ser atómico (bajo el lock del worker o una operación
   condicional), no "leer y luego escribir".
4. **Fallo después de cobrar.** Si Yupy cobró y JELAF falla al emitir: no reintentar a ciegas
   (podría emitir dos veces); registrar incidencia en paneles vía `registro.py` con el
   `booking_id` y la referencia de pago, y responder al cliente con un mensaje claro de que el
   equipo lo revisará. Proponer al usuario el proceso de reembolso/reintento manual.
5. **Worker.** Toda llamada a JELAF va dentro de `async with w.lock:` con `w.es_dueno(booking_id)`
   re-verificado dentro. Liberar el worker en `finally` y en abandono/timeout.
6. **PDF del boleto.** Solo para el dueño (enlace firmado), nunca enumerable por `booking_id`
   secuencial; sin datos personales en la URL.
7. **v1.** No agregar funcionalidad. Si se toca algo cercano, proponer desactivar v1 (flag por
   variable de entorno, por defecto desactivado) y dejar constancia en el doc de revisión.
8. **Dinero.** `Decimal` o céntimos enteros, nunca `float` en código nuevo.
9. **Errores.** Al cliente, mensaje genérico en español; al log, contexto (`booking_id`, paso,
   tipo de error) sin DNI/teléfono completos.

## Cómo verificar (sin tocar nada real)
Con `httpx.ASGITransport(app=api_ventas.app)` sin lifespan, un `JelafWorker` falso y
`YupyFalso` (fixture `entorno_venta` de `tests/conftest.py`). Casos mínimos (ya cubiertos en `tests/test_venta.py`):
- Precio alterado en la petición → rechazado o ignorado.
- Yupy devuelve monto distinto → no emite.
- `/confirmar` llamado dos veces (también en paralelo con `asyncio.gather`) → **una** emisión.
- Referencia de pago reutilizada con otro `booking_id` → rechazada.
- Worker de otro `booking_id` → rechazado (C2).
- Excepción en JELAF tras cobro → incidencia registrada, worker liberado.
