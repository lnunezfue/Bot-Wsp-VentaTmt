# Frontend — flujo de pantallas

La mini-webapp es un conjunto de páginas HTML independientes (Tailwind vía CDN, JavaScript sin
framework, sin proceso de build). El estado del pedido se guarda en `localStorage` bajo la clave
`vibeTransitBooking` y se acumula a medida que el cliente avanza; cada pantalla lee lo que necesita
de ahí y navega a la siguiente con `window.location.href`.

## Archivos activos y archivos de referencia por carpeta

Las carpetas de cada paso del flujo (`buscar viaje/`, `seleccionar asiento/`, `rellenar datos/`,
`pasarela de pago/`) contienen, además del HTML activo:

- Capturas `.png` y archivos `.zip` con el diseño exportado originalmente. Son referencia visual y
  no se utilizan en ejecución.
- HTML alternativos que no están enlazados desde ningún otro archivo (verificado mediante búsqueda
  en todo el proyecto): `buscar viaje/code.html`, `seleccionar asiento/5-7.html`,
  `seleccionar asiento/seleccionar-asiento.html`. Corresponden a iteraciones de diseño anteriores y
  no forman parte del flujo real; no se cargan desde ningún otro archivo.
- `preview-planos/index.html` tampoco forma parte del flujo del cliente: es una herramienta interna
  para revisar visualmente los archivos `planos_buses/*.json` sin recorrer todo el flujo de compra.

## Flujo real, en orden

| # | Archivo | Función | Navega a |
|---|---|---|---|
| 1 | `buscar viaje/1-2-corregir.html` | Formulario de origen, destino y fecha. Invoca `POST /api/v1/buscar-viajes`. Guarda `busqueda` y `resultados` en el booking. | `buscar viaje/3-4.html` |
| 2 | `buscar viaje/3-4.html` | Lista de turnos disponibles para esa búsqueda (nombre de servicio, horarios, precio). El cliente elige uno. Guarda `viajeSeleccionado`. | `seleccionar asiento/seleccionar-asiento-bus2.html` |
| 3 | `seleccionar asiento/seleccionar-asiento-bus2.html` | Invoca `POST /api/v1/plano-bus` para obtener el plano real, con los asientos ocupados marcados. El cliente elige hasta 5 asientos; cada selección de un asiento libre invoca `POST /api/v1/bloquear-asiento` (bloqueo real en JELAF). Si el cliente sale sin confirmar, el evento `pagehide` libera los asientos bloqueados. Guarda `asientos`. | `rellenar datos/8-9.html` |
| 4 | `rellenar datos/8-9.html` | Formulario de datos por pasajero (documento, nombres, apellidos, sexo, fecha de nacimiento; el primero incluye además correo electrónico y teléfono). Ver [`backend-api.md`](backend-api.md#tipos-de-documento) para el detalle de validación por tipo de documento. Guarda `pasajeros`. | `rellenar datos/10-11.html` |
| 5 | `rellenar datos/10-11.html` | Resumen de compra, modal de Términos y Condiciones (requiere desplazamiento hasta el final para poder aceptar) y captura del correo de contacto. Al continuar, `POST /api/v1/pre-checkout` calcula el monto total. Muestra un aviso si hay pasajeros menores de edad. | `pasarela de pago/16.html` |
| 6 | `pasarela de pago/16.html` | Monta el checkout oficial de Yupy (`POST /api/v1/generar-checkout-yupi`); si el SDK no carga, utiliza una interfaz de tarjeta/QR simulada. Al confirmar el pago, dispara `POST /api/v1/confirmar-compra` (el robot RPA que emite el boleto real en JELAF). | `pasarela de pago/17-18.html` |
| 7 | `pasarela de pago/17-18.html` | Pantalla de confirmación: muestra los boletos emitidos y un botón por boleto para descargar el PDF (`POST /api/v1/descargar-boleto`). | Fin del flujo. El botón "Realizar otra compra" limpia el `localStorage` y vuelve al paso 1. |

## Convenciones compartidas entre pantallas

- **Sin encabezado simulado de navegador o WhatsApp.** Se retiró de todas las pantallas porque el
  propio cliente de WhatsApp ya muestra su encabezado al abrir el enlace (ver el historial de
  commits de agosto de 2026 para el detalle de este cambio).
- **Colores de asiento** (`seleccionar-asiento-bus2.html`): libre en blanco, ocupado en rojo sólido
  (con el número visible y un aviso tipo toast al intentar seleccionarlo), seleccionado en verde
  WhatsApp.
- **`API_BASE_URL`** está definido de forma fija al inicio del `<script>` en las seis páginas que
  llaman al backend (todas excepto `rellenar datos/8-9.html`, que solo lee y escribe
  `localStorage`), y actualmente apunta a un túnel de Ngrok temporal. Si el backend cambia de URL,
  debe actualizarse manualmente en cada archivo, ya que no existe un único punto de configuración
  (ver [`known-issues-and-security.md`](known-issues-and-security.md)).
- **Todas las pantallas dependen de que existan `booking.viajeSeleccionado` y `booking.busqueda`
  en `localStorage`**; si faltan, redirigen de vuelta a `buscar viaje/1-2-corregir.html`.
