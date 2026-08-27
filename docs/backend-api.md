# Backend — referencia de la API

Toda la lógica reside en `api_ventas.py` (FastAPI, un solo archivo). URL base en desarrollo:
`http://localhost:8000` (o el túnel de Ngrok que la reemplace; ver [`frontend.md`](frontend.md)).
CORS está abierto a cualquier origen (`allow_origins=["*"]`).

Al arrancar (`lifespan`), el backend abre un Chrome visible con Playwright, inicia sesión en JELAF
con `JELAF_USUARIO`/`JELAF_PASSWORD`, y guarda la sesión autenticada en la variable global
`sesion_global_jelaf`. Si ese inicio de sesión falla, todos los endpoints que dependen de JELAF
responden `500` con el mensaje "Sin sesión Jelaf".

## Flujo de compra

### `POST /api/v1/buscar-viajes`
Busca viajes en JELAF para una ruta y fecha, y los filtra a los que tienen un plano de asientos
configurado en `planos_buses/` (un viaje real sin plano configurado no aparece en el resultado).

- **Body:** `{ origen, destino, fecha }` (fecha en formato `YYYY-MM-DD`, convertida a `DD/MM/YYYY`
  para JELAF).
- **Respuesta:** `{ status, data: [{ id_programacion, nro_viaje, codi_*, servicio, hora_partida, placa, asientos_libres, precio_base, ... }] }`.

### `POST /api/v1/plano-bus`
Obtiene el plano de asientos de un viaje específico, con los asientos ya vendidos marcados como
`occupied`, y el precio real de JELAF inyectado en cada piso.

- **Body:** `PeticionAsientos` — los campos `codi_*` del viaje seleccionado, más `fecha_viaje`,
  `hora_viaje` y `placa_bus`.
- El plano base proviene de `planos_buses/*.json` (ver [`data-planos-buses.md`](data-planos-buses.md)),
  resuelto por placa; si la placa no está asociada a ninguna plantilla, responde `404`.
- **Bug conocido:** el precio de cada piso (`data_piso["price"]`) solo se sobrescribe cuando
  `precio_real_jelaf > 0`. Si JELAF no informa `PrecioVenta`/`PrecioNormal` en ningún asiento de ese
  piso, el piso queda sin precio y el total mostrado en el frontend resulta en `S/ NaN`. Se detectó
  en pruebas manuales sobre un servicio con dos pisos; no está corregido.

### `POST /api/v1/bloquear-asiento`
Bloquea un asiento en JELAF (hold temporal real). Si JELAF lo rechaza (por ejemplo, ya tomado por
otro cliente), responde `409` con el mensaje devuelto por JELAF.

- **Body:** `PeticionBloqueoAsiento` (`codi_programacion`, `numero_asiento`, `fecha_viaje`,
  `precio`, ...).
- **Respuesta exitosa:** `{ status: "success", id_bloqueo }`.

### `POST /api/v1/liberar-asiento`
Libera un bloqueo previo. El frontend lo invoca automáticamente cuando el cliente abandona la
pantalla de selección de asientos sin confirmar (evento `pagehide`, usando
`fetch(..., { keepalive: true })` para que la petición se complete aunque la pestaña se esté
cerrando).

- **Body:** `{ id_bloqueo }`.

### `POST /api/v1/pre-checkout`
No interactúa con JELAF: valida la petición (máximo 5 pasajeros, misma cantidad de asientos que de
pasajeros) y suma el `precio_venta` de cada pasajero para devolver el `monto_total` que se muestra
en el resumen.

- **Body:** `PeticionPreCheckout` (`asientos_seleccionados[]`, `pasajeros[]`).

### `POST /api/v1/confirmar-compra`

Robot RPA que emite el boleto real en JELAF. No es una llamada directa a una API: abre Chrome con
Playwright, inicia sesión, busca el viaje, selecciona cada asiento, completa los datos de cada
pasajero, llena el formulario de pago con una tarjeta de prueba generada en el momento
(`0000-0000-0000-XXXX`), y confirma la venta. El detalle de por qué se implementa como RPA está en
[`architecture.md`](architecture.md#por-qué-la-automatización-se-implementa-como-rpa-y-no-como-una-integración-convencional).

Actualmente soporta de forma confirmada la venta de un asiento por transacción. La venta de dos o
más asientos en una misma transacción está en desarrollo (ver
[`known-issues-and-security.md`](known-issues-and-security.md)).

- **Body:** `PeticionCompraFinal` (los mismos datos del viaje, más `asientos_seleccionados[]`,
  `pasajeros[]` y `pago: { metodo, referencia_operacion, monto_total }`).
- **Respuesta exitosa:** `{ status: "success", data: { boletos: [{ asiento, pasajero, documento, precio, nro_boleto, estado }] } }`.
- El timeout interno por paso llega hasta 45 segundos; cualquier selector que no aparezca a tiempo
  interrumpe el flujo completo con `500 "Fallo en la automatización: ..."`. Es el punto más frágil
  de todo el sistema, porque depende directamente de que la interfaz de JELAF no cambie.

### `POST /api/v1/descargar-boleto`
Genera el PDF del boleto en memoria con ReportLab (sin persistencia en disco) y lo devuelve como
archivo adjunto descargable.

- **Body:** `PeticionPDF` (`nro_boleto`, `pasajero`, `documento`, `origen`, `destino`, `fecha`,
  `hora`, `asiento`, `precio`).
- **Respuesta:** PDF binario (`application/pdf`, `Content-Disposition: attachment`).

## Tipos de documento

El frontend (`rellenar datos/8-9.html`) captura `tipoDocumento` (DNI `01`, Pasaporte `02`, Carnet
de Extranjería `03`, RUC `04`) con validación de formato en el navegador (DNI de 8 dígitos
numéricos, RUC de 11 dígitos numéricos, CE alfanumérico). El backend no recibe `tipoDocumento` en
ningún payload: el modelo `Pasajero` solo tiene `documento` como texto libre. La opción de generar
factura en lugar de boleta cuando el documento es RUC quedó capturada únicamente en el frontend; el
backend siempre genera "BOLETA DE VENTA ELECTRÓNICA" (texto fijo en `descargar_pdf_boleto`), sin
lógica de facturación implementada todavía.

## WhatsApp Cloud API (webhook)

### `GET /api/v1/webhook`
Verificación requerida por Meta al configurar el webhook. Compara `hub.verify_token` contra
`META_VERIFY_TOKEN` (definido en `.env`, con valor por defecto `"MoqueguaBot2026"`) y devuelve
`hub.challenge` en texto plano si coincide; en caso contrario responde `403`.

### `POST /api/v1/webhook`
Recibe los mensajes entrantes de WhatsApp. Recorre `entry[].changes[].value.messages[]`; si el
mensaje es de texto y contiene la palabra `"comprar"` (sin distinción de mayúsculas), dispara en
segundo plano (`BackgroundTasks`) la función `enviar_boton_compra`, que llama a la Graph API de
Meta para enviar al cliente un mensaje interactivo `cta_url` con el botón "Comprar Pasaje" hacia
`buscar viaje/1-2-corregir.html`. Siempre responde `200 "EVENT_RECEIVED"` de inmediato, ya que Meta
reintenta el envío si la respuesta no llega con rapidez.

- **Limitaciones actuales** (ver [`known-issues-and-security.md`](known-issues-and-security.md)):
  no valida la firma `X-Hub-Signature-256`, no deduplica por `message.id`, la URL del frontend está
  fija en el código (`https://transportesmoquegua.com/beta/...`, con un comentario en el código
  que advierte sobre la necesidad de actualizarla), y la detección de intención se limita a
  `if "comprar" in texto`, sin lógica adicional.

## Modelos de datos (Pydantic)

Definidos en la sección de modelos de datos de `api_ventas.py`: `PeticionBusqueda`,
`PeticionAsientos`, `PeticionBloqueoAsiento`, `PeticionLiberaAsiento`, `Pasajero`,
`PeticionPreCheckout`, `DatosPago`, `PeticionCompraFinal`, `PeticionYupy`, `PeticionPDF`. Sirven a
la vez como validación de entrada y como documentación de forma (la documentación interactiva
generada por Swagger está disponible en `/docs` mientras el backend está en ejecución).

## Integración con Yupy (pagos)

### `POST /api/v1/generar-checkout-yupi`
Se autentica contra `sandbox-api.yupy.us` con `YUPY_CLIENT_ID`/`YUPY_CLIENT_SECRET`, crea una orden
de pago (`payment-orders`) y, si Yupy no la incluye en la respuesta, crea también una
`checkout-session` (con expiración de 900 segundos). Devuelve la respuesta de Yupy sin
modificaciones (incluye `checkout_session.checkout_url`, que el frontend utiliza con el SDK
oficial de Yupy).

- **Body:** `{ monto, pedido, nombre_comprador }`.
- Si Yupy rechaza cualquier paso, responde `500` con el detalle textual de la respuesta de Yupy.
