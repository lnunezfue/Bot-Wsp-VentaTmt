# Arquitectura

## Vista general

```
┌─────────────┐   1. "comprar"    ┌──────────────────────┐
│   Cliente   │ ────────────────► │  WhatsApp Cloud API   │
│  (WhatsApp) │                   │      (Meta)            │
└─────────────┘                   └───────────┬───────────┘
       ▲                                       │ POST /api/v1/webhook
       │ 4. boton "Comprar Pasaje"             ▼
       │                          ┌──────────────────────┐
       └───────────────────────── │   api_ventas.py        │
                                   │   (FastAPI, :8000)     │
                                   └───────────┬───────────┘
                                               │
                       ┌───────────────────────┼───────────────────────┐
                       ▼                       ▼                       ▼
              ┌──────────────┐        ┌──────────────┐        ┌──────────────┐
              │    JELAF      │        │     Yupy      │        │  planos_buses │
              │ (Playwright,  │        │ (pasarela de  │        │     /*.json   │
              │  automatizacion)│      │  pago, sandbox)│        │  (locales)    │
              └──────────────┘        └──────────────┘        └──────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│  Mini-webapp (HTML, Tailwind y JavaScript sin framework, servida como   │
│  archivos estaticos, sin backend propio para las paginas) — se abre     │
│  dentro del navegador integrado de WhatsApp cuando el cliente toca el   │
│  boton de compra. Se comunica con api_ventas.py mediante fetch() a      │
│  /api/v1/...                                                            │
└─────────────────────────────────────────────────────────────────────────┘
```

## Flujo de punta a punta

1. **El cliente escribe al WhatsApp de la empresa.** Meta reenvía el mensaje al webhook
   (`POST /api/v1/webhook`) del backend.
2. **El backend detecta la intención** (busca la palabra `"comprar"` en el texto) y responde de
   forma asíncrona (`BackgroundTasks`), llamando a la Graph API de Meta para enviar un mensaje
   interactivo tipo `cta_url` con un botón "Comprar Pasaje".
3. **El cliente toca el botón.** WhatsApp abre esa URL en su navegador integrado y carga
   `frontend/buscar-viaje.html`, el primer paso de la mini-webapp (ver
   [`frontend.md`](frontend.md) para el flujo completo de pantallas).
4. **La mini-webapp guarda el estado del pedido en `sessionStorage`** (clave `vibeTransitBooking`, uno por pestaña: dos pestañas son dos compras distintas) y
   avanza de pantalla en pantalla navegando a otro archivo `.html`, sin framework ni proceso de
   build. Cada pantalla llama a `api_ventas.py` mediante `fetch()` cuando necesita datos reales
   (buscar viajes, obtener el plano del bus, bloquear un asiento, etc.).
5. **`api_ventas.py` no tiene base de datos propia.** Toda la información sobre viajes, asientos y
   precios se obtiene en vivo desde JELAF, automatizando su interfaz web (no existe una API REST
   documentada de JELAF que pueda invocarse directamente; ver más abajo).
6. **Selección de asiento — el clic ES el bloqueo:** desde el primer asiento que el cliente toca,
   `POST /api/v2/venta/iniciar` reserva un worker persistente del pool (ver más abajo) y
   `POST /api/v2/venta/seleccionar-asiento` hace un clic real sobre esa página, la misma que se
   usará hasta el cierre de la venta. Ya no existe un "bloqueo blando" desechable que luego se
   libera y se rehace — la selección nunca se suelta hasta que el cliente la deselecciona, cancela,
   o confirma la compra.
7. **Pago:** la pantalla de pasarela (`frontend/pago.html`) monta el checkout oficial de Yupy
   (`generar-checkout-yupi`) o utiliza una interfaz de tarjeta/QR simulada si el SDK de Yupy no
   carga. Al confirmarse el pago, se dispara `POST /api/v2/venta/confirmar`.
8. **Emisión real del boleto:** `confirmar_compra` (en `jelaf_worker.py`) llena el formulario de
   "Tipo de Pago" con una tarjeta de prueba generada en el momento y confirma la venta, sobre la
   MISMA página del worker que ya tiene los asientos seleccionados y los pasajeros cargados desde
   las pantallas anteriores — no vuelve a abrir Chrome ni a rehacer nada. El backend recoge el
   número de boleto que JELAF devuelve.
9. **PDF del boleto:** `POST /api/v1/descargar-boleto` genera un PDF con ReportLab en memoria (sin
   persistencia en disco) y lo devuelve como descarga.

## Pool de sesiones JELAF persistentes

En vez de una sesión HTTP compartida para bloqueos + un Chrome desechable al final (el diseño
original, todavía presente sin usarse en `api_ventas.py` bajo `/api/v1/...`), el flujo real usa
`jelaf_worker.py`: un **pool de N sesiones de navegador persistentes**, una por credencial JELAF
disponible. Cada worker se loguea una sola vez al arrancar el backend y queda abierto todo el
tiempo que corra el proceso.

Por qué: JELAF ata el bloqueo temporal de un asiento a una cookie de sesión (compartible entre
procesos), pero la "venta en construcción" (qué asiento corresponde a qué pestaña de pasajero) vive
solo en memoria de Vue, en la página concreta que hizo los clics — no sobrevive a cambiar de
pestaña ni de proceso. La solución anterior lidiaba con esto liberando todo y reconstruyéndolo al
final, en una sola navegación; el pool lo evita directamente: **una venta = un worker exclusivo,
desde el primer asiento hasta el cierre**, así que nunca hay nada que reconstruir.

- Con una sola cuenta JELAF (`JELAF_USUARIO`/`JELAF_PASSWORD`), el pool tiene tamaño 1: solo puede
  haber una venta con asientos ya tocados a la vez en todo el sistema. Cualquier otro cliente que
  intente seleccionar su primer asiento mientras tanto recibe `423` de forma explícita, en vez de
  competir en silencio por la misma sesión (el riesgo real que tenía `sesion_global_jelaf`
  compartida entre confirmaciones concurrentes en el diseño anterior).
- Agregar credenciales `JELAF_USUARIO_2`/`JELAF_PASSWORD_2`, `_3`, etc. sube el paralelismo real sin
  cambiar código — cada cuenta adicional es, en la práctica, un mostrador virtual más. Es una
  gestión con la empresa/JELAF (pedir usuarios de punto de venta adicionales), no solo un cambio de
  configuración.
- Un `asyncio.Lock` por worker evita que dos requests concurrentes al mismo `booking_id` toquen la
  página a la vez. Un *reaper* de fondo libera cualquier worker inactivo por más de 10 minutos (sin
  llamadas de la venta ni heartbeat del frontend) — red de seguridad para el cliente que abandona
  sin que dispare `pagehide` (app cerrada de golpe, red caída, WhatsApp en segundo plano).
- El detalle de la validación en vivo que llevó a este diseño (incluida la pregunta de si se puede
  llenar el pasajero de un asiento revisitando su pestaña, fuera de orden) está en
  [`prototipo-pool-sesiones.md`](prototipo-pool-sesiones.md).

### Prefetch de la búsqueda del viaje

`buscar_viaje()` (login ya hecho, pero aún así: abrir el buscador, filtrar origen, escribir fecha,
click en Buscar, esperar resultados, doble clic en la fila, esperar el plano) es la parte más lenta
de toda la interacción con JELAF. Medido en vivo tras optimizarla (ver
[`optimizacion-buscar-viaje.md`](optimizacion-buscar-viaje.md) para el detalle completo): **~2.2 a
2.8 segundos** en el caso normal (worker que ya venía de una venta anterior), con un costo único de
~5.5s la primera vez que un worker recién iniciado hace una búsqueda. Antes de esa optimización
tardaba ~11.8s — casi todo en `wait_for_timeout()` fijos heredados del RPA original, reemplazados
por esperas de condiciones reales (respuesta AJAX, fila pintada, botón de asiento visible). Si esto
se disparara recién cuando el cliente toca su primer asiento, ese toque se sentiría con un retraso
grande y desproporcionado frente a lo rápido que responde el resto del frontend (que hasta ese punto
solo usa la sesión ligera compartida).

Por eso `buscar_viaje()` se lanza **en segundo plano desde `frontend/elegir-horario.html`**, en el momento
en que el cliente elige el viaje — no desde la pantalla de asientos. `POST /api/v2/venta/iniciar`
reserva el worker y arranca la tarea sin esperar a que termine (`lanzar_busqueda_en_fondo` en
`jelaf_worker.py`); `POST /api/v2/venta/seleccionar-asiento` espera esa tarea
(`esperar_viaje_listo()`) antes de hacer clic — instantáneo si ya terminó (el caso normal, porque el
cliente tuvo que mirar el plano y decidir qué asiento tocar mientras tanto), o esperando lo que
falte si fue más rápido que el prefetch. Es idempotente: si el cliente vuelve atrás y elige un
viaje distinto, se detecta el cambio y se relanza; si vuelve a llamarse para el mismo viaje, no hace
nada.

## Por qué la automatización se implementa como RPA y no como una integración convencional

JELAF (`moquegua.2jelaf.net.pe`) no expone una API pública que el backend pueda invocar
directamente con credenciales de aplicación. En su lugar:

- Al arrancar, el backend usa Playwright para abrir un Chrome real por cada credencial del pool
  (ver arriba), e iniciar sesión en JELAF. Ese Chrome queda abierto y persistente durante toda la
  vida del proceso — no se cierra entre ventas.
- Las llamadas de bajo costo que no dependen de "quién soy en JELAF" (buscar viajes, obtener el
  plano de un bus) siguen usando una `requests.Session` aparte (`sesion_global_jelaf`), con cookies
  copiadas de uno de los workers — son peticiones HTTP estándar, seguras de compartir entre
  clientes concurrentes porque son de solo lectura.
- Para todo lo que sí depende de una venta concreta (seleccionar asiento, llenar pasajero, pagar y
  confirmar), JELAF no ofrece endpoints internos invocables directamente con esas cookies — de modo
  que el worker reproduce, paso a paso, la misma secuencia que realizaría un operador humano,
  **sobre la página persistente que tiene reservada esa venta** (nunca abre un Chrome nuevo a mitad
  de una compra).

Esto hace que la emisión de boletos sea frágil por diseño: depende de que los identificadores y
selectores de la interfaz de JELAF no cambien, de tiempos de espera fijos (`wait_for_timeout`), y
de que Chrome pueda abrirse en la máquina donde corre el backend (no se ha probado aún en modo
headless; ver [`known-issues-and-security.md`](known-issues-and-security.md)).

## Componentes externos

| Servicio | Uso | Estado |
|---|---|---|
| JELAF (`moquegua.2jelaf.net.pe`) | Sistema real de venta de pasajes de la empresa; fuente de verdad de viajes, asientos y precios | En producción real. Las pruebas contra este sistema bloquean y liberan asientos reales. |
| Meta / WhatsApp Cloud API | Canal de entrada del cliente y envío del botón de compra | Número de prueba únicamente; ver [`meta-whatsapp-config.md`](meta-whatsapp-config.md) |
| Yupy (`sandbox-api.yupy.us`) | Pasarela de pago | Sandbox, con credenciales de prueba |
| Ngrok | Exposición de `localhost:8000` a Internet para que Meta pueda invocar el webhook durante el desarrollo | Dominio gratuito, cambia en cada reinicio |

## Decisiones de diseño relevantes

- **Sin base de datos.** El detalle del pedido (ruta, pasajeros, contacto) sigue viviendo en el
  `sessionStorage` del navegador del cliente. Lo que SÍ tiene estado en el servidor ahora es la venta
  en curso dentro del worker que la tiene reservada (asientos seleccionados, pasajeros cargados) —
  vive en memoria del proceso del backend, identificada por el `bookingId` que el frontend genera y
  reenvía en cada llamada a `/api/v2/venta/...`. Si el backend se reinicia, esas ventas en curso se
  pierden (no hay persistencia en disco de este estado); es una limitación conocida, aceptable
  mientras el pool sea pequeño y las ventas duren minutos, no horas.
- **Un solo archivo de backend.** Toda la lógica de `api_ventas.py` (configuración, modelos
  Pydantic, RPA, integración con Yupy, webhook de Meta) reside en un único módulo de
  aproximadamente 820 líneas.
- **Frontend sin proceso de build.** Cada pantalla es un archivo `.html` independiente con Tailwind
  vía CDN; no hay bundler ni componentes compartidos reales (los fragmentos de encabezado y estilos
  se repiten copiados en cada archivo).
