---
name: webhook-whatsapp
description: Usar al modificar el webhook de Meta en api_ventas.py, enviar_boton_compra, el envío de plantillas o paneles/fidelizacion.py. Garantiza firma verificada, idempotencia, respuesta rápida y que no se envíen WhatsApp reales por accidente.
---

# Webhook y envíos de WhatsApp (Meta Cloud API)

## Recordatorio
`enviar_boton_compra` y los envíos de `fidelizacion.py` con `PANELES_ENVIO_REAL=1` mandan
**mensajes reales a clientes reales**. Nunca ejecutarlos para probar.

## Recepción (webhook)
1. **GET de verificación:** responder `hub.challenge` solo si `hub.mode == "subscribe"` y
   `hub.verify_token` coincide con `META_VERIFY_TOKEN` (`hmac.compare_digest`). Si la variable
   falta → `403` (fallar cerrado).
2. **Firma del POST:** leer `await request.body()` **antes** de parsear; calcular
   `hmac.new(APP_SECRET, body, sha256).hexdigest()` y comparar con `X-Hub-Signature-256`
   (`sha256=...`) con `hmac.compare_digest`. Falla → `401`. Si hoy no se verifica, reportarlo como
   hallazgo y proponer añadir la variable del secreto de la app (no inventar su valor).
3. **Idempotencia:** Meta reintenta. Guardar el id de mensaje (`messages[].id`) y no procesar dos
   veces (p. ej. tabla en paneles vía `registro.py`, o caché con expiración).
4. **Respuesta rápida:** responder `200` pronto; el trabajo pesado (envío de botón, registro) en
   segundo plano. Nunca reservar un worker de JELAF desde el webhook.
5. **Entrada:** texto del usuario como dato no confiable: normalizar, limitar longitud, comparar
   comandos ("comprar") sin ejecutar nada derivado del texto. Ignorar tipos no soportados.
6. **Eventos `statuses`** (sent/delivered/read/failed) separados de `messages`; un `failed` se
   registra como incidencia.

## Envío
- `WHATSAPP_TOKEN` solo desde el entorno; nunca en logs (ojo con loguear la URL o los headers de
  `httpx`/`requests`).
- Timeouts en toda llamada HTTP; reintentos con backoff solo ante 429/5xx.
- Fuera de la ventana de 24 h, solo plantillas aprobadas (`META_PLANTILLA_*`) y a clientes con
  opt-in. Fidelización: respetar si el cliente pidió no recibir mensajes.
- El enlace del botón lleva el teléfono **firmado** con `ENLACE_SECRETO` (y expiración); nunca el
  teléfono o DNI en claro como única identificación.
- Límite de envíos por cliente y por ejecución en fidelización para evitar spam/bloqueo del número.

## Logs
Id de mensaje, tipo y resultado; teléfono enmascarado (`+51 9** *** 321`); sin contenido completo.

## Verificación
Con `TestClient`/`ASGITransport` y `enviar_boton_compra` reemplazado por un doble que registre llamadas:
- Firma válida → 200 y un envío; firma inválida/ausente → 401 y cero envíos.
- Mismo id de mensaje dos veces → un solo envío.
- Verify token incorrecto o variable ausente → 403.
- `fidelizacion` con `PANELES_ENVIO_REAL` sin definir → no llama a Meta.
