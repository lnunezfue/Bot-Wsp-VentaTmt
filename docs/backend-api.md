# Backend — referencia de la API

Todo vive en **`api_ventas.py`** (FastAPI, un solo archivo). Base URL en desarrollo:
`http://localhost:8000` (o el túnel de Ngrok que la reemplace, ver [`frontend.md`](frontend.md)).
CORS abierto a cualquier origen (`allow_origins=["*"]`).

Al arrancar (`lifespan`), el backend abre un Chrome visible con Playwright, inicia sesión en
JELAF con `JELAF_USUARIO`/`JELAF_PASSWORD`, y guarda la sesión autenticada en la variable global
`sesion_global_jelaf`. **Si ese login falla, todos los endpoints que dependen de JELAF responden
500 "Sin sesión Jelaf".**

## Flujo de compra

### `POST /api/v1/buscar-viajes`
Busca viajes en JELAF para una ruta/fecha y los filtra a solo los que tienen un plano de asientos
configurado en `planos_buses/` (si un bus no tiene plano, no aparece aunque exista el viaje real).

- **Body:** `{ origen, destino, fecha }` (fecha en `YYYY-MM-DD`, se convierte a `DD/MM/YYYY` para JELAF)
- **Respuesta:** `{ status, data: [{ id_programacion, nro_viaje, codi_*, servicio, hora_partida, placa, asientos_libres, precio_base, ... }] }`

### `POST /api/v1/plano-bus`
Trae el plano de asientos de un viaje específico, con los asientos ya vendidos marcados como
`occupied`, y el precio real de JELAF inyectado en cada piso.

- **Body:** `PeticionAsientos` — todos los `codi_*` del viaje elegido + `fecha_viaje`, `hora_viaje`, `placa_bus`
- El plano base sale de `planos_buses/*.json` (ver [`data-planos-buses.md`](data-planos-buses.md)),
  resuelto por placa; si la placa no está mapeada a ninguna plantilla → `404`.
- **Bug conocido:** el precio de cada piso (`data_piso["price"]`) solo se sobreescribe
  `if precio_real_jelaf > 0`. Si JELAF no trae `PrecioVenta`/`PrecioNormal` en ningún asiento de ese
  piso, el piso se queda **sin precio**, y el total en el frontend termina en `S/ NaN`. Se detectó
  en pruebas manuales sobre un servicio con 2 pisos; no está corregido.

### `POST /api/v1/bloquear-asiento`
Bloquea un asiento **de verdad** en JELAF (hold temporal). Si JELAF lo rechaza (ya tomado por otro
cliente, etc.) responde `409` con el mensaje de JELAF.

- **Body:** `PeticionBloqueoAsiento` (`codi_programacion`, `numero_asiento`, `fecha_viaje`, `precio`, ...)
- **Respuesta éxito:** `{ status: "success", id_bloqueo }`

### `POST /api/v1/liberar-asiento`
Libera un bloqueo previo. El frontend lo llama automáticamente si el cliente sale de la pantalla de
asientos sin confirmar (evento `pagehide`, con `fetch(..., { keepalive: true })` para que la
petición sobreviva aunque la pestaña ya se esté cerrando).

- **Body:** `{ id_bloqueo }`

### `POST /api/v1/pre-checkout`
No toca JELAF — solo valida (máx. 5 pasajeros, misma cantidad de asientos que pasajeros) y suma
`precio_venta` de cada pasajero para devolver el `monto_total` a mostrar en el resumen.

- **Body:** `PeticionPreCheckout` (`asientos_seleccionados[]`, `pasajeros[]`)

### `POST /api/v1/confirmar-compra`

El robot RPA que emite el boleto.
Emite el boleto **real** en JELAF. No es una llamada a una API: abre Chrome con Playwright,
inicia sesión, busca el viaje, hace clic asiento por asiento, llena los datos de cada pasajero,
completa el formulario de pago con una tarjeta de prueba generada al vuelo
(`0000-0000-0000-XXXX`), y confirma la venta. Ver
[`architecture.md`](architecture.md#por-qué-la-automatización-es-rpa-y-no-una-integración-normal)
para el porqué.

- **Body:** `PeticionCompraFinal` (mismos datos del viaje + `asientos_seleccionados[]` +
  `pasajeros[]` + `pago: { metodo, referencia_operacion, monto_total }`)
- **Respuesta éxito:** `{ status: "success", data: { boletos: [{ asiento, pasajero, documento, precio, nro_boleto, estado }] } }`
- Timeout interno por paso hasta 45s; cualquier selector que no aparezca a tiempo revienta el flujo
  completo con `500 "Fallo en la automatización: ..."` — es el punto más frágil de todo el sistema
  porque depende 1:1 de que la UI de JELAF no cambie.

### `POST /api/v1/descargar-boleto`
Genera el PDF del boleto **en memoria** con ReportLab (no se guarda en disco) y lo devuelve como
adjunto descargable.

- **Body:** `PeticionPDF` (`nro_boleto`, `pasajero`, `documento`, `origen`, `destino`, `fecha`, `hora`, `asiento`, `precio`)
- **Respuesta:** PDF binario (`application/pdf`, `Content-Disposition: attachment`)

## Tipos de documento

El frontend (`rellenar datos/8-9.html`) captura `tipoDocumento` (DNI `01`, Pasaporte `02`, Carnet
de Extranjería `03`, RUC `04`) con validación de formato en el navegador (DNI 8 dígitos numéricos,
RUC 11 dígitos numéricos, CE alfanumérico). **El backend no recibe `tipoDocumento` en ningún
payload** — el modelo `Pasajero` solo tiene `documento` como string libre. La idea de "RUC para
generar factura en vez de boleta" quedó solo capturada en el frontend; el backend siempre genera
"BOLETA DE VENTA ELECTRÓNICA" (texto fijo en `descargar_pdf_boleto`), no hay lógica de facturación
todavía.

## WhatsApp Cloud API (webhook)

### `GET /api/v1/webhook`
Verificación que exige Meta al configurar el webhook. Compara `hub.verify_token` contra
`META_VERIFY_TOKEN` (`.env`, default `"MoqueguaBot2026"`) y devuelve `hub.challenge` en texto plano
si coincide; si no, `403`.

### `POST /api/v1/webhook`
Recibe los mensajes entrantes de WhatsApp. Recorre `entry[].changes[].value.messages[]`; si el
mensaje es de texto y contiene la palabra `"comprar"` (case-insensitive), dispara en segundo plano
(`BackgroundTasks`) la función `enviar_boton_compra`, que llama a la Graph API de Meta para
mandarle al cliente un mensaje interactivo `cta_url` con el botón "Comprar Pasaje" apuntando a
`buscar viaje/1-2-corregir.html`. Siempre responde `200 "EVENT_RECEIVED"` de inmediato (Meta
reintenta si no responde rápido).

- **Limitaciones actuales** (ver [`known-issues-and-security.md`](known-issues-and-security.md)):
  no valida la firma `X-Hub-Signature-256`, no deduplica por `message.id`, la URL del frontend está
  hardcodeada (`https://transportesmoquegua.com/beta/...`, con un comentario `MUY IMPORTANTE`
  recordando cambiarla), y la detección de intención es un `if "comprar" in texto` sin más lógica.

## Modelos de datos (Pydantic)

Definidos todos en la sección `MODELOS DE DATOS` de `api_ventas.py`:
`PeticionBusqueda`, `PeticionAsientos`, `PeticionBloqueoAsiento`, `PeticionLiberaAsiento`,
`Pasajero`, `PeticionPreCheckout`, `DatosPago`, `PeticionCompraFinal`, `PeticionYupy`,
`PeticionPDF`. Sirven a la vez como validación de entrada y como documentación de forma (Swagger
autogenerado disponible en `/docs` mientras el backend está corriendo).

## Integración Yupy (pagos)

### `POST /api/v1/generar-checkout-yupi`
Se autentica contra `sandbox-api.yupy.us` con `YUPY_CLIENT_ID`/`YUPY_CLIENT_SECRET`, crea una
orden de pago (`payment-orders`) y, si Yupy no la devuelve incluida, crea también una
`checkout-session` (expira en 900s). Devuelve la respuesta de Yupy tal cual (incluye
`checkout_session.checkout_url` que el frontend monta con el SDK oficial de Yupy).

- **Body:** `{ monto, pedido, nombre_comprador }`
- Si Yupy rechaza cualquier paso, `500` con el detalle textual de la respuesta de Yupy.
