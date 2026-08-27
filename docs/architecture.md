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
   `buscar viaje/1-2-corregir.html`, el primer paso de la mini-webapp (ver
   [`frontend.md`](frontend.md) para el flujo completo de pantallas).
4. **La mini-webapp guarda el estado del pedido en `localStorage`** (clave `vibeTransitBooking`) y
   avanza de pantalla en pantalla navegando a otro archivo `.html`, sin framework ni proceso de
   build. Cada pantalla llama a `api_ventas.py` mediante `fetch()` cuando necesita datos reales
   (buscar viajes, obtener el plano del bus, bloquear un asiento, etc.).
5. **`api_ventas.py` no tiene base de datos propia.** Toda la información sobre viajes, asientos y
   precios se obtiene en vivo desde JELAF, automatizando su interfaz web (no existe una API REST
   documentada de JELAF que pueda invocarse directamente; ver más abajo).
6. **Selección y bloqueo de asiento:** `POST /api/v1/bloquear-asiento` y
   `POST /api/v1/liberar-asiento` reflejan un bloqueo real en JELAF. Si el cliente abandona la
   pantalla sin confirmar, el frontend libera el asiento automáticamente mediante el evento
   `pagehide`.
7. **Pago:** la pantalla de pasarela (`pasarela de pago/16.html`) monta el checkout oficial de Yupy
   (`generar-checkout-yupi`) o utiliza una interfaz de tarjeta/QR simulada si el SDK de Yupy no
   carga. Al confirmarse el pago, se dispara `POST /api/v1/confirmar-compra`.
8. **Emisión real del boleto:** `confirmar-compra` es un robot RPA construido con Playwright que
   abre Chrome, inicia sesión en JELAF con las credenciales configuradas, busca el viaje,
   selecciona los mismos asientos, completa los datos de cada pasajero, llena el formulario de
   "Tipo de Pago" con una tarjeta de prueba generada en el momento, y confirma la venta,
   reproduciendo la interacción que realizaría un operador en la interfaz real de JELAF. El
   backend recoge el número de boleto que JELAF devuelve.
9. **PDF del boleto:** `POST /api/v1/descargar-boleto` genera un PDF con ReportLab en memoria (sin
   persistencia en disco) y lo devuelve como descarga.

## Por qué la automatización se implementa como RPA y no como una integración convencional

JELAF (`moquegua.2jelaf.net.pe`) no expone una API pública que el backend pueda invocar
directamente con credenciales de aplicación. En su lugar:

- Al arrancar, el backend usa Playwright para abrir un Chrome real, iniciar sesión en JELAF con
  `JELAF_USUARIO`/`JELAF_PASSWORD`, y copiar las cookies de esa sesión a una `requests.Session`
  (`sesion_global_jelaf`) que se reutiliza para las llamadas de bajo costo (buscar viajes, obtener
  plano, bloquear o liberar asiento). Estas sí son peticiones HTTP estándar, autenticadas con
  cookies de una sesión de navegador real.
- Para la confirmación de compra, ese mecanismo no es suficiente: JELAF no ofrece un endpoint
  interno para "confirmar venta" que pueda invocarse con esas cookies, de modo que el backend abre
  un segundo Chrome visible y reproduce, paso a paso, la misma secuencia que realizaría un
  operador humano en el sistema.

Esto hace que la emisión de boletos sea frágil por diseño: depende de que los identificadores y
selectores de la interfaz de JELAF no cambien, de tiempos de espera fijos (`wait_for_timeout`), y
de que Chrome pueda abrirse en la máquina donde corre el backend (no funciona en modo headless).

## Componentes externos

| Servicio | Uso | Estado |
|---|---|---|
| JELAF (`moquegua.2jelaf.net.pe`) | Sistema real de venta de pasajes de la empresa; fuente de verdad de viajes, asientos y precios | En producción real. Las pruebas contra este sistema bloquean y liberan asientos reales. |
| Meta / WhatsApp Cloud API | Canal de entrada del cliente y envío del botón de compra | Número de prueba únicamente; ver [`meta-whatsapp-config.md`](meta-whatsapp-config.md) |
| Yupy (`sandbox-api.yupy.us`) | Pasarela de pago | Sandbox, con credenciales de prueba |
| Ngrok | Exposición de `localhost:8000` a Internet para que Meta pueda invocar el webhook durante el desarrollo | Dominio gratuito, cambia en cada reinicio |

## Decisiones de diseño relevantes

- **Sin base de datos.** El estado de un pedido reside en el `localStorage` del navegador del
  cliente hasta el momento del pago; el backend es esencialmente sin estado, salvo por
  `sesion_global_jelaf` (una única sesión JELAF global compartida por todos los clientes que usen
  el backend de forma concurrente).
- **Un solo archivo de backend.** Toda la lógica de `api_ventas.py` (configuración, modelos
  Pydantic, RPA, integración con Yupy, webhook de Meta) reside en un único módulo de
  aproximadamente 820 líneas.
- **Frontend sin proceso de build.** Cada pantalla es un archivo `.html` independiente con Tailwind
  vía CDN; no hay bundler ni componentes compartidos reales (los fragmentos de encabezado y estilos
  se repiten copiados en cada archivo).
