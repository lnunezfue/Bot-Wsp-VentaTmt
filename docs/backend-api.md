# Backend — referencia de la API

Punto de entrada: `api_ventas.py` (FastAPI, un solo proceso). La lógica está repartida en módulos:

| Módulo | Qué hace |
|---|---|
| `config.py` | Lee y valida el entorno. `ENTORNO=produccion` no arranca si falta un secreto. |
| `jelaf_http.py` | Consultas HTTP de solo lectura a JELAF (itinerarios, plano con precios y ocupados, cliente por documento), con re-login automático. |
| `jelaf_worker.py` | Pool de navegadores JELAF persistentes (RPA de la venta). |
| `pagos_yupy.py` | Cliente de Yupy y `evaluar_orden` (¿se cobró exactamente lo esperado?). |
| `whatsapp.py` | Enlace firmado, firma del webhook, envío del botón. |
| `planos.py` | Planos de `planos_buses/` (en caché) y precio por piso. |
| `boleto_pdf.py` | PDF del boleto desde la venta guardada. |
| `limitador.py`, `privacidad.py` | Límite de peticiones y enmascarado de datos personales. |

URL base en desarrollo: `http://localhost:8000`. CORS limitado a `CORS_ORIGENES`. `/docs` solo en
desarrollo. Errores al cliente: mensajes genéricos en español; el detalle va al log.

## Reglas de dinero

1. El **precio** de cada asiento lo pone el servidor con datos de JELAF (`/itinerarios/turnos`); el
   que mande el navegador se ignora.
2. El **monto** de Yupy lo calcula el servidor y queda en la tabla `pago`.
3. `/confirmar` emite **solo** si Yupy dice que se cobró exactamente ese monto, en PEN, para esa
   transacción, y los asientos de la compra siguen siendo los mismos que se cobraron.
4. `/confirmar` es **idempotente**: una segunda llamada (doble clic, reintento) devuelve lo mismo.
5. Un fallo después de cobrar **nunca desaparece**: queda como venta `cobrada_sin_emitir` o
   `por_conciliar` con incidencia crítica, o como `emitida` si el plano de JELAF confirma la emisión.

## Catálogo (solo lectura en JELAF)

### `POST /api/v1/buscar-viajes`
- **Body:** `{ origen, destino, fecha }` (`fecha` en `YYYY-MM-DD`).
- **Respuesta:** `{ status, data: [{ id_programacion, codi_*, servicio, hora_partida, placa, asientos_libres, precio_base, ... }] }`.
  Solo viajes cuyo bus tiene plano en `planos_buses/`. `400` si el origen no se vende en línea,
  `502` si JELAF no responde.

### `POST /api/v1/plano-bus`
- **Body:** `codi_empresa, codi_origen, codi_destino, codi_sucursal, codi_ruta, codi_punto_venta, codi_servicio, fecha_viaje, hora_viaje, placa_bus`.
- Marca los asientos vendidos como `occupied` y pone a cada piso el precio real de JELAF de sus
  asientos (buses de dos pisos con precios distintos). `404` si la placa no tiene plano.

### `POST /api/v1/consultar-cliente`
Autocompleta nombre, apellidos y fecha de nacimiento desde la base de clientes de JELAF.
- **Body:** `{ booking_id, tipo_documento ("01".."04"), documento }`.
- Exige una venta activa (`403` si el `booking_id` no tiene worker). Límite: 15 consultas por venta y
  30 por IP cada 10 minutos (`429`). Ya no devuelve el sexo.
- **Respuesta:** `{ status, encontrado: false }` o `{ status, encontrado: true, nombres, paterno, materno, fecha_nacimiento }`.

## Venta — `/api/v2/venta/...`

Todos reciben `booking_id` (UUID del frontend, `^[A-Za-z0-9-]{8,64}$`). Toda operación sobre el
worker va bajo su lock y re-verifica que la venta siga siendo de ese `booking_id` (`403` si no).

| Endpoint | Body | Qué hace / respuestas |
|---|---|---|
| `POST /iniciar` | `booking_id, codi_origen, fecha_viaje, hora_viaje, viaje{codi_empresa, codi_destino, codi_sucursal, codi_ruta, codi_punto_venta, codi_servicio}, destino?, servicio?, placa_bus?, contacto?` | Reserva un worker y lanza en segundo plano la búsqueda del viaje (prefetch) y la consulta de precios. Con `EXIGIR_CONTACTO` (por defecto en producción) exige el token `contacto` del enlace de WhatsApp (`403`). `423` si todos los workers están ocupados. `409` si ya hay asientos en otro viaje. Límite 10/min por IP. |
| `POST /seleccionar-asiento` | `booking_id, numero_asiento` (`precio` se ignora) | Clic real en JELAF con el precio del servidor. Responde `{ precio, asientos_en_venta }`. `409` asiento no disponible o sin precio en JELAF; `409` si la fila del viaje es ambigua (V3, se libera la venta); `502` si JELAF falla. |
| `POST /liberar-asiento` | `booking_id, numero_asiento` | Deselecciona. `409` si ya hay un pago en curso. |
| `POST /pasajero` | `booking_id, numero_asiento, documento, nombres, paterno, materno, fecha_nacimiento, sexo ("M"/"F"), tipo_documento ("01" DNI, "02" pasaporte, "03" CE), telefono?` (`precio_venta` se ignora) | Guarda el pasajero de ese asiento y busca su `IdCliente` en JELAF. Entradas validadas (`422`). |
| `POST /monto` | `booking_id` | `{ monto_total, moneda, asientos[{asiento, precio}], pasajeros_completos }`. Reemplaza al viejo `/api/v1/pre-checkout`. |
| `POST /checkout` | `booking_id` | Graba la ficha de los pasajeros en JELAF (`grabar-pasajero`; si JELAF la rechaza, `502` y no se cobra) y crea el cobro en Yupy por el monto del servidor y lo guarda en `pago`. Responde `{ yupy_transaction_id, pedido, monto_total, checkout_session{checkout_url} }`. Desde aquí el reaper no libera la venta mientras el checkout siga vivo. `409` si faltan pasajeros; `502/503` si Yupy o la base fallan (no se entrega el checkout). |
| `POST /preparar-pago` | `booking_id` | Adelanta el modal de pago de JELAF (best-effort). |
| `POST /confirmar` | `booking_id, referencia_pago?, medio_pago?` (`codigo_descuento` se ignora) | Ver "Reglas de dinero". `200` con `{ data: { boletos[{asiento, pasajero, documento, precio, nro_boleto, estado}] } }`; `402` sin pago o pago no aprobado (el frontend reintenta); `503` si Yupy no responde; `409` si se cobró y no se pudo emitir (mensaje para el cliente: no volver a pagar). |
| `POST /cancelar` | `booking_id` | Libera el worker. `409` si hay un pago en curso. |
| `POST /heartbeat` | `booking_id` | Marca actividad. Nunca falla: `{ status: "ignorado" }` si ya no hay venta. |
| `GET /estado` | — (cabecera `X-Paneles-Token`) | Estado del pool con el `booking_id` enmascarado. |

Cada boleto trae además `link_comprobante` (el comprobante electrónico de JELAF, `LinkPag_FE`).
En `VENTA_MODO=simulacion` los boletos vienen como `SIMULADO-NN` y la venta queda con estado `simulada`.
`nro_boleto` puede venir `null`: JELAF emitió pero no devolvió el número de ese asiento (queda incidencia
`boleto_sin_numero` para conciliar). Nunca se inventa un número.

### `POST /api/v2/boleto/pdf`
- **Body:** `{ booking_id, asiento }`. PDF generado con la venta guardada (estado `emitida`); `404`
  si no existe. Límite 30/min por IP.

### `GET /api/v2/contacto?c=<token>`
Celular (9 dígitos) de quien abrió el enlace firmado; `""` si el token no es válido o venció.

## WhatsApp (webhook de Meta)

### `GET /api/v1/webhook`
Responde `hub.challenge` si `hub.verify_token` coincide con `META_VERIFY_TOKEN` (comparación en
tiempo constante). Sin la variable, `403`.

### `POST /api/v1/webhook`
- Verifica `X-Hub-Signature-256` con `META_APP_SECRET` sobre el cuerpo crudo (`401` si no
  coincide). En desarrollo, sin la variable, acepta con aviso al arrancar; en producción no arranca.
- Deduplica por id de mensaje (tabla `mensaje_whatsapp`): los reintentos de Meta no reenvían el botón.
- Si el texto contiene "comprar", envía en segundo plano el botón "Comprar Pasaje" con el teléfono
  firmado en el enlace (`URL_FRONTEND?c=...`).
- `statuses` con `failed` se registran como incidencia `whatsapp_fallido`.
- Siempre `200 EVENT_RECEIVED` para mensajes válidos.

## Rutas retiradas

`/api/v1/confirmar-compra`, `/api/v1/bloquear-asiento`, `/api/v1/liberar-asiento`,
`/api/v1/pre-checkout`, `/api/v1/generar-checkout-yupi` y `/api/v1/descargar-boleto` ya no existen
(emitían boletos o PDFs sin pago, o confiaban en el monto del navegador). Están en el historial de git.

## Paneles

Ver [`PLAN_PANELES.md`](PLAN_PANELES.md). Todo bajo `/api/paneles/...` exige `X-Paneles-Token`, salvo
`/codigos/validar` (documento obligatorio, 10/min por IP) y `/evento` (solo `enlace_abierto`, 30/min
por IP).
