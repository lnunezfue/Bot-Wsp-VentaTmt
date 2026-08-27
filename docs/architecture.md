# Arquitectura

## Vista general

```
┌─────────────┐   1. "comprar"    ┌──────────────────────┐
│   Cliente   │ ────────────────► │  WhatsApp Cloud API   │
│  (WhatsApp) │                   │      (Meta)            │
└─────────────┘                   └───────────┬───────────┘
       ▲                                       │ POST /api/v1/webhook
       │ 4. botón CTA "Comprar Pasaje"         ▼
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
              │  scraping/RPA)│        │  pago, sandbox)│        │  (locales)    │
              └──────────────┘        └──────────────┘        └──────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│  Mini-webapp (HTML + Tailwind + JS vanilla, servida como archivos       │
│  estáticos, sin backend propio para las páginas) — se abre dentro del    │
│  navegador integrado de WhatsApp cuando el cliente toca el botón CTA.    │
│  Habla con api_ventas.py por fetch() a /api/v1/...                       │
└─────────────────────────────────────────────────────────────────────────┘
```

## Flujo de punta a punta

1. **Cliente escribe al WhatsApp de la empresa.** Meta reenvía el mensaje al webhook
   (`POST /api/v1/webhook`) del backend.
2. **El backend detecta la intención** (busca la palabra `"comprar"` en el texto) y responde de
   forma asíncrona (`BackgroundTasks`) llamando a la Graph API de Meta para mandar un mensaje
   interactivo tipo `cta_url` con un botón "Comprar Pasaje".
3. **El cliente toca el botón** → WhatsApp abre esa URL en su navegador integrado → carga
   `buscar viaje/1-2-corregir.html`, el primer paso de la mini-webapp (ver
   [`frontend.md`](frontend.md) para el flujo completo de pantallas).
4. **La mini-webapp guarda todo el estado del pedido en `localStorage`** (clave
   `vibeTransitBooking`) y avanza de pantalla en pantalla navegando a otro archivo `.html`, sin
   framework ni build step. Cada pantalla llama a `api_ventas.py` por `fetch()` para lo que
   necesita datos reales (buscar viajes, traer el plano del bus, bloquear un asiento, etc.).
5. **`api_ventas.py` no tiene base de datos propia.** Todo lo que "sabe" sobre viajes, asientos y
   precios lo obtiene en vivo de JELAF, automatizando su interfaz web (no hay una API REST
   documentada de JELAF que se pueda llamar directo — ver más abajo).
6. **Selección/bloqueo de asiento**: `POST /api/v1/bloquear-asiento` y
   `POST /api/v1/liberar-asiento` reflejan un bloqueo real en JELAF (si el cliente se arrepiente o
   sale de la pantalla, el frontend libera el asiento automáticamente vía el evento `pagehide`).
7. **Pago**: la pantalla de pasarela (`pasarela de pago/16.html`) monta el checkout oficial de
   **Yupy** (`generar-checkout-yupi`) o cae a una UI de tarjeta/QR simulada si el SDK de Yupy no
   carga. Cuando el pago se confirma, dispara `POST /api/v1/confirmar-compra`.
8. **Emisión real del boleto**: `confirmar-compra` es un **robot RPA con Playwright** que abre
   Chrome, inicia sesión en JELAF con las credenciales configuradas, busca el viaje, selecciona los
   mismos asientos, llena los datos de cada pasajero, completa el formulario de "Tipo de Pago" con
   una tarjeta de prueba generada al vuelo, y confirma la venta — literalmente haciendo clic en la
   interfaz real de JELAF como lo haría una persona. El backend recoge el número de boleto que
   devuelve JELAF.
9. **PDF del boleto**: `POST /api/v1/descargar-boleto` genera un PDF con ReportLab **en memoria**
   (no se guarda en disco) y lo devuelve como descarga.

## Por qué la automatización es "RPA" y no una integración normal

JELAF (`moquegua.2jelaf.net.pe`) no expone una API pública que el backend pueda llamar
directamente con credenciales de aplicación. En su lugar:

- Al arrancar, el backend usa Playwright para **abrir un Chrome real, iniciar sesión** en JELAF
  con `JELAF_USUARIO`/`JELAF_PASSWORD`, y **copiar las cookies de sesión** a una `requests.Session`
  (`sesion_global_jelaf`) que se reutiliza para las llamadas "rápidas" (buscar viajes, traer plano,
  bloquear/liberar asiento) — esas sí son peticiones HTTP normales, solo que autenticadas con
  cookies robadas de una sesión de navegador real.
- Para la **confirmación de compra**, eso no alcanza: JELAF no tiene un endpoint interno para
  "confirmar venta" que se pueda llamar con esas cookies, así que el backend abre **otro** Chrome
  visible y repite, paso a paso, exactamente lo que haría un operador humano en el sistema.

Esto hace que la emisión de boletos sea **frágil por diseño**: depende de que los `id`/`selector`
de la interfaz de JELAF no cambien, de tiempos de espera fijos (`wait_for_timeout`), y de que
Chrome pueda abrirse en la máquina donde corre el backend (no es headless).

## Componentes externos

| Servicio | Para qué se usa | Estado |
|---|---|---|
| **JELAF** (`moquegua.2jelaf.net.pe`) | Sistema real de venta de pasajes de la empresa; fuente de verdad de viajes, asientos y precios | En producción real (cuidado: las pruebas contra este sistema bloquean/liberan asientos reales) |
| **Meta / WhatsApp Cloud API** | Canal de entrada del cliente + envío del botón de compra | Número de prueba únicamente, ver [`meta-whatsapp-config.md`](meta-whatsapp-config.md) |
| **Yupy** (`sandbox-api.yupy.us`) | Pasarela de pago | Sandbox, credenciales de prueba |
| **Ngrok** | Exponer `localhost:8000` a Internet para que Meta pueda llamar al webhook durante desarrollo | Dominio gratuito (cambia en cada reinicio) |

## Decisiones de diseño a tener en cuenta

- **Sin base de datos.** El "estado" de un pedido vive en el `localStorage` del navegador del
  cliente hasta el momento del pago; el backend es esencialmente *stateless* salvo por
  `sesion_global_jelaf` (una sola sesión JELAF global compartida por todos los clientes que usen el
  backend al mismo tiempo).
- **Un solo archivo de backend.** Todo `api_ventas.py` (configuración, modelos Pydantic, RPA,
  integración Yupy, webhook de Meta) vive en un único módulo de ~820 líneas.
- **Frontend sin build.** Cada pantalla es un `.html` independiente con Tailwind por CDN; no hay
  bundler, ni componentes compartidos reales (los fragmentos de header/estilos se repiten copiados
  en cada archivo).
