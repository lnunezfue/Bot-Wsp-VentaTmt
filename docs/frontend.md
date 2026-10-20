# Frontend — flujo de pantallas

La mini-webapp es un conjunto de páginas HTML independientes (Tailwind vía CDN, JavaScript sin
framework, sin proceso de build). El estado del pedido se guarda en `sessionStorage` (una compra por pestaña) bajo la clave
`vibeTransitBooking` y se acumula a medida que el cliente avanza; cada pantalla lee lo que necesita
de ahí y navega a la siguiente con `window.location.href`.

## Qué se publica

Todo lo que se sube al hosting está en **`frontend/`**, sin subcarpetas: las 7 páginas del flujo y
`config.js` (único lugar con la URL del backend, `window.TMT.API_BASE_URL`). Se sube la carpeta tal
cual; `URL_FRONTEND` del backend debe apuntar a `<dominio>/<ruta>/buscar-viaje.html`, y `pago.html`
(retorno de Niubiz) se deriva de ahí, así que todas las páginas deben quedar juntas.

Fuera de `frontend/` (no se sube):

- `diseno_referencia/`: capturas `.png`, `.zip` del diseño exportado y borradores HTML de iteraciones
  anteriores (`borrador-*.html`, la redirección vieja `1-2-corregir.html`). Solo referencia visual.
- `preview-planos/index.html`: herramienta interna para revisar visualmente los archivos
  `planos_buses/*.json` sin recorrer todo el flujo de compra.

## Flujo real, en orden

| # | Archivo | Función | Navega a |
|---|---|---|---|
| 1 | `frontend/buscar-viaje.html` | Formulario de origen, destino y fecha. Invoca `POST /api/v1/buscar-viajes`. Guarda `busqueda` y `resultados` en el booking. | `frontend/elegir-horario.html` |
| 2 | `frontend/elegir-horario.html` | Lista de turnos disponibles para esa búsqueda (nombre de servicio, horarios, precio). El cliente elige uno. Genera el `bookingId` (`crypto.randomUUID()`) y dispara `POST /api/v2/venta/iniciar` **sin esperar la respuesta** (reserva un worker del pool y arranca la búsqueda del viaje en JELAF en segundo plano, antes de que el cliente llegue siquiera a la pantalla de asientos — ver [`architecture.md`](architecture.md#prefetch-de-la-búsqueda-del-viaje)). Guarda `viajeSeleccionado` y `bookingId`. | `frontend/seleccionar-asiento.html` |
| 3 | `frontend/seleccionar-asiento.html` | Invoca `POST /api/v1/plano-bus` para obtener el plano real, con los asientos ocupados marcados (llamada de solo lectura, sin cambios). El cliente elige hasta 5 asientos; el primer clic vuelve a llamar `POST /api/v2/venta/iniciar` (de respaldo — no hace nada si el prefetch del paso anterior ya está en curso o ya terminó) y cada selección invoca `POST /api/v2/venta/seleccionar-asiento` — normalmente instantáneo, porque la búsqueda del viaje ya terminó mientras el cliente miraba el plano. El clic ES el bloqueo real, sobre la página persistente de esa venta. Deseleccionar llama a `POST /api/v2/venta/liberar-asiento`. Si el cliente sale sin confirmar, `pagehide` llama a `POST /api/v2/venta/cancelar` una sola vez. Guarda `asientos`. | `frontend/datos-pasajeros.html` |
| 4 | `frontend/datos-pasajeros.html` | Formulario de datos por pasajero (documento, nombres, apellidos, sexo, fecha de nacimiento; el primero incluye además correo electrónico y teléfono). Al llegar el DNI a 8 dígitos, `POST /api/v1/consultar-cliente` autocompleta nombres/apellidos/fecha de nacimiento si esa persona ya compró antes con la empresa. Si el cliente cambia el DNI, se reemplazan por los del nuevo; si el nuevo no está en JELAF, se borra lo autocompletado del anterior (lo corregido a mano se conserva). La fecha de nacimiento no admite más de 120 años (también lo valida el backend). Ver [`backend-api.md`](backend-api.md#tipos-de-documento) para el detalle de validación por tipo de documento. Al continuar, envía cada pasajero con `POST /api/v2/venta/pasajero` (uno por uno, en orden) antes de avanzar — si alguno falla, se detiene ahí. Guarda `pasajeros`. | `frontend/resumen-compra.html` |
| 5 | `frontend/resumen-compra.html` | Resumen de compra, modal de Términos y Condiciones (requiere desplazamiento hasta el final para poder aceptar) y captura del correo de contacto. Al continuar, `POST /api/v1/pre-checkout` calcula el monto total (sin cambios, es pura validación). Muestra un aviso si hay pasajeros menores de edad. | `frontend/pago.html` |
| 6 | `frontend/pago.html` | Monta el checkout oficial de Yupy (`POST /api/v1/generar-checkout-yupi`); si el SDK no carga, utiliza una interfaz de tarjeta/QR simulada. Al confirmar el pago, dispara `POST /api/v2/venta/confirmar` (pago + cierre sobre la MISMA sesión del worker que ya tiene los asientos y pasajeros cargados — no vuelve a abrir Chrome ni a rehacer nada). | `frontend/boletos-emitidos.html` |
| 7 | `frontend/boletos-emitidos.html` | Pantalla de confirmación: muestra los boletos emitidos y un botón por boleto para descargar el PDF (`POST /api/v1/descargar-boleto`). | Fin del flujo. El botón "Realizar otra compra" limpia el `localStorage` y vuelve al paso 1. |

## Convenciones compartidas entre pantallas

- **Sin encabezado simulado de navegador o WhatsApp.** Se retiró de todas las pantallas porque el
  propio cliente de WhatsApp ya muestra su encabezado al abrir el enlace (ver el historial de
  commits de agosto de 2026 para el detalle de este cambio).
- **Colores de asiento** (`seleccionar-asiento.html`): libre en blanco, ocupado en rojo sólido
  (con el número visible y un aviso tipo toast al intentar seleccionarlo), seleccionado en verde
  WhatsApp.
- **Caché (N44-SN):** las páginas cargan `config.js?v=…`; al cambiar `config.js`, subir ese número en las 7 páginas. `frontend/.htaccess` (también se sube) pide `Cache-Control: no-cache` para HTML y JS.
- **`API_BASE_URL`** se define en un solo lugar, `frontend/config.js`; las siete páginas lo cargan
  y leen `window.TMT.API_BASE_URL`. Al publicar, cambiarlo ahí y agregar el dominio del frontend a
  `CORS_ORIGENES` en el backend.
- **Todas las pantallas dependen de que existan `booking.viajeSeleccionado` y `booking.busqueda`
  en `sessionStorage`**; si faltan, redirigen de vuelta a `frontend/buscar-viaje.html`. Desde el
  paso 4 en adelante, también dependen de `booking.bookingId` (la venta reservada en el pool de
  workers); si falta, redirigen a la selección de asientos.
- **`bookingId` y el heartbeat.** `seleccionar-asiento.html` genera el `bookingId` con
  `crypto.randomUUID()` en cuanto el cliente elige un viaje. Desde ahí, `datos-pasajeros.html`, `resumen-compra.html` y
  `pago.html` mandan un `POST /api/v2/venta/heartbeat` cada 30 segundos mientras la pestaña sigue
  abierta, para que el backend sepa que la venta sigue viva aunque el cliente tarde en llenar
  formularios o leer los términos (ver [`architecture.md`](architecture.md#pool-de-sesiones-jelaf-persistentes)).
