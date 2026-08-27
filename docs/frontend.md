# Frontend — flujo de pantallas

La mini-webapp es un conjunto de páginas HTML independientes (Tailwind por CDN, JS vanilla, sin
build). El estado del pedido se guarda en `localStorage` bajo la clave **`vibeTransitBooking`** y
se va acumulando a medida que el cliente avanza; cada pantalla lee lo que necesita de ahí y navega
a la siguiente con `window.location.href`.

## ⚠️ Cada carpeta tiene archivos "vivos" y archivos de referencia muertos

Las carpetas de captura de pantalla (`buscar viaje/`, `seleccionar asiento/`, `rellenar datos/`,
`pasarela de pago/`) contienen, además del HTML real:

- **Capturas `.png`** y **`.zip`** con el diseño exportado originalmente — son solo referencia
  visual, no se usan en ejecución.
- **HTML alternativos que no están enlazados desde ningún otro archivo** (verificado con
  `grep` sobre todo el proyecto): `buscar viaje/code.html`, `seleccionar asiento/5-7.html`,
  `seleccionar asiento/seleccionar-asiento.html`. Son iteraciones de diseño anteriores; **no
  editar pensando que afectan el flujo real** — no se cargan desde ningún lado.
- `preview-planos/index.html` tampoco es parte del flujo del cliente: es una **herramienta interna**
  para revisar visualmente los `planos_buses/*.json` sin tener que pasar por todo el flujo de
  compra.

## El flujo real, en orden

| # | Archivo | Qué hace | A dónde navega |
|---|---|---|---|
| 1 | `buscar viaje/1-2-corregir.html` | Formulario: origen, destino, fecha. `POST /api/v1/buscar-viajes`. Guarda `busqueda` + `resultados` en el booking. | `buscar viaje/3-4.html` |
| 2 | `buscar viaje/3-4.html` | Lista de turnos disponibles para esa búsqueda (nombre de servicio, horarios, precio). El cliente elige uno. Guarda `viajeSeleccionado`. | `seleccionar asiento/seleccionar-asiento-bus2.html` |
| 3 | `seleccionar asiento/seleccionar-asiento-bus2.html` | `POST /api/v1/plano-bus` trae el plano real (con asientos ocupados marcados). El cliente elige hasta 5 asientos; cada clic en un asiento libre llama a `POST /api/v1/bloquear-asiento` (bloqueo real en JELAF). Si sale sin confirmar, el evento `pagehide` libera los asientos bloqueados. Guarda `asientos`. | `rellenar datos/8-9.html` |
| 4 | `rellenar datos/8-9.html` | Un formulario de datos por pasajero (documento, nombres, apellidos, sexo, nacimiento; el primero además con email/teléfono). Ver [`backend-api.md`](backend-api.md#tipos-de-documento) para el detalle de validación por tipo de documento. Guarda `pasajeros`. | `rellenar datos/10-11.html` |
| 5 | `rellenar datos/10-11.html` | Resumen de compra + modal de Términos y Condiciones (debe hacerse scroll hasta el final para poder aceptar) + captura de email de contacto. Al continuar, `POST /api/v1/pre-checkout` calcula el monto total. Muestra un aviso si hay menores de edad. | `pasarela de pago/16.html` |
| 6 | `pasarela de pago/16.html` | Monta el checkout oficial de Yupy (`POST /api/v1/generar-checkout-yupi`); si el SDK no carga, cae a una UI de tarjeta/QR simulada. Al "pagar", dispara `POST /api/v1/confirmar-compra` (el robot RPA que emite el boleto real en JELAF). | `pasarela de pago/17-18.html` |
| 7 | `pasarela de pago/17-18.html` | Pantalla de confirmación: muestra los boletos emitidos y un botón por boleto para descargar el PDF (`POST /api/v1/descargar-boleto`). | — (fin del flujo; botón "Realizar otra compra" limpia el `localStorage` y vuelve al paso 1) |

## Convenciones compartidas entre pantallas

- **Sin header simulado de navegador/WhatsApp.** Se quitó de todas las pantallas porque el propio
  cliente de WhatsApp ya muestra su propio encabezado al abrir el link — ver el historial de commits
  de agosto 2026 si hace falta revisar por qué.
- **Colores de asiento** (`seleccionar-asiento-bus2.html`): libre = blanco, ocupado = rojo sólido
  (con número visible y aviso tipo *toast* al tocarlo), seleccionado = verde WhatsApp.
- **`API_BASE_URL`** está *hardcodeado* al inicio del `<script>` de las 6 páginas que llaman al
  backend (todas menos `rellenar datos/8-9.html`, que solo lee/escribe `localStorage`) y hoy
  apunta a un túnel de Ngrok temporal. Si el backend se mueve de URL, hay que actualizarlo **a
  mano en cada archivo** — no hay un único punto de configuración (ver
  [`known-issues-and-security.md`](known-issues-and-security.md)).
- **Todas dependen de que exista `booking.viajeSeleccionado` / `booking.busqueda` en
  `localStorage`**; si faltan, redirigen de vuelta a `buscar viaje/1-2-corregir.html`.
