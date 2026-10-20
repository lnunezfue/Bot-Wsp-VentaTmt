# Prototipo — sesión JELAF persistente por venta (origen del diseño actual)

> **Actualización:** lo que este documento describe como prototipo ya es la implementación real,
> generalizada a un pool de N workers y conectada al frontend completo — ver
> [`architecture.md`](architecture.md#pool-de-sesiones-jelaf-persistentes) y
> [`backend-api.md`](backend-api.md#flujo-de-venta-real-pool-de-workers-persistentes--apiv2venta)
> para el diseño vigente. Este documento se conserva porque explica el **porqué** original — la
> hipótesis, el problema de fondo, y qué se validó paso a paso antes de generalizar — que sigue
> siendo la referencia para entender decisiones como el timeout del *reaper* o por qué
> `llenar_pasajero` reactiva pestañas en vez de asumir que ya está activa la correcta.
>
> No se ha tocado nada de `CHATBOT/` (el proyecto original, que sigue en el diseño anterior); todo
> esto vive en `CHATBOT PRUEBA/`.

## El problema que se intenta resolver

El flujo v1 (`api_ventas.py`, endpoints `/api/v1/...`) usa dos sesiones distintas para una
misma venta:

1. **`sesion_global_jelaf`** (HTTP ligero, cookies compartidas) — para buscar viajes, leer
   el plano y hacer bloqueos "blandos" de asiento mientras el cliente navega.
2. **Un Chrome nuevo dentro de `confirmar_compra`** — al momento de pagar, se **liberan**
   todos los bloqueos anteriores y se **rehace desde cero**, en una sola navegación
   continua, toda la selección de asientos + llenado de pasajeros + pago.

Esto funciona (validado con boletos reales para 1 asiento), pero por una razón muy
concreta documentada en los comentarios del propio `confirmar_compra`: JELAF ata el
bloqueo temporal a una **cookie de sesión** (compartible entre procesos), pero la
**"venta en construcción"** (qué asiento corresponde a qué pestaña de pasajero) vive
solo en memoria de Vue, **en esa página concreta** — no sobrevive a cambiar de pestaña
ni de proceso, así que hay que reconstruirla de una sola vez, sin interrupciones.

Consecuencias de resolverlo así:

- **Ventana de riesgo duplicada**: se libera el asiento y se vuelve a tomar minutos
  después; en ese hueco alguien más puede llevárselo.
- **Sin garantía de concurrencia**: solo existe una cuenta JELAF (`sesion_global_jelaf`)
  para todo el backend. Si dos clientes llegan a pagar casi al mismo tiempo, dos Chromes
  distintos actúan como el mismo operador logueado en el servidor de JELAF de forma
  simultánea — nada en el código impide que sus ventas se mezclen.

## La hipótesis de este prototipo

**Un worker = una sesión de navegador persistente, asignada en exclusiva a una venta
desde el primer clic en un asiento hasta el cierre de la venta.** Nunca se libera y se
vuelve a pedir nada: el clic que selecciona el asiento YA ES el bloqueo real, y esa misma
página llega hasta el pago.

Esto es un prototipo de **pool de tamaño 1** a propósito: mientras haya una venta en
curso, cualquier otra debe esperar explícitamente (`423 Locked`) en vez de competir en
silencio por la misma sesión, como pasa hoy. Generalizar a N workers (uno por cada cuenta
JELAF adicional que consiga la empresa) es el siguiente paso, una vez validado esto.

## Qué se construyó

- **`jelaf_worker.py`** — clase `JelafWorker`: navegador Playwright persistente (login
  una sola vez), con métodos que reutilizan literalmente la lógica ya probada de
  `confirmar_compra` en v1 (mismos selectores, mismos *workarounds* documentados), pero
  expuestos como pasos independientes en vez de un único bloque monolítico:
  `buscar_viaje`, `seleccionar_asiento`, `deseleccionar_asiento`, `llenar_pasajero`,
  `pagar_y_confirmar`. Incluye control de dueño (`owner_token`) y un candado
  (`asyncio.Lock`) para que dos requests no toquen la página a la vez.
- **`api_ventas.py`** — se agregó una sección nueva `/api/v2/venta/...` que convive con
  `/api/v1/...` sin modificarlo. Incluye un *reaper* (tarea de fondo) que libera el
  worker si nadie lo usa por `TIMEOUT_INACTIVIDAD_SEGUNDOS` (180s) — para no depender
  solo del `pagehide` del frontend, que puede no dispararse (app cerrada de golpe, red
  caída, WhatsApp en segundo plano).
- **`prueba_v2_flujo.py`** — harness manual (sin frontend) para probar el flujo completo
  contra JELAF real, paso a paso, mirando el Chrome que abre el backend.

## Endpoints del prototipo

| Endpoint | Reemplaza (v1) | Qué hace distinto |
|---|---|---|
| `POST /api/v2/venta/iniciar` | — (nuevo) | Reserva el worker para un `booking_id` y busca el viaje real. `423` si otro `booking_id` ya lo tiene. |
| `POST /api/v2/venta/seleccionar-asiento` | `bloquear-asiento` | Hace el clic real sobre el asiento en la página persistente — el "bloqueo" y la "selección real" son la misma acción. |
| `POST /api/v2/venta/liberar-asiento` | `liberar-asiento` | Clic de nuevo sobre el mismo asiento (toggle) — ver riesgo abierto abajo. |
| `POST /api/v2/venta/pasajero` | (parte de `confirmar-compra`) | Llena el pasajero de un asiento ya seleccionado, reactivando su pestaña — se puede llamar en cualquier orden. |
| `POST /api/v2/venta/confirmar` | `confirmar-compra` | Pago + cierre sobre la MISMA página que ya tiene todo cargado. Libera el worker al terminar (éxito o error). |
| `POST /api/v2/venta/cancelar` | (pagehide implícito) | Abandono explícito: libera el worker ya. |
| `GET /api/v2/venta/estado` | — (diagnóstico) | Estado actual del worker único: libre/en uso, dueño, asientos, inactividad. |

## Cómo probarlo

1. `cd "CHATBOT PRUEBA"` y `python api_ventas.py` — espera a ver `[worker] listo y libre.`
   en la consola (esto abre un Chrome real logueado en JELAF y lo deja abierto).
2. En otra terminal: `python prueba_v2_flujo.py`. Pide datos de un viaje **real de hoy**
   con al menos 2 asientos libres, y va deteniéndose entre cada paso para poder mirar el
   Chrome y confirmar visualmente que hizo lo esperado antes de seguir.
3. Repetir la prueba dos veces con **orden distinto** de llenado de pasajeros (en el mismo
   orden de selección de asientos, y luego en orden inverso) — eso es exactamente lo que
   valida o refuta la segunda hipótesis (ver abajo).

**Importante:** el paso de confirmar emite un boleto real (con tarjeta de prueba) en el
sistema de producción de la empresa, igual que ya hace v1. El script pide escribir `SI`
explícitamente antes de confirmar.

## Riesgos / preguntas abiertas para validar en vivo

1. **¿Un segundo clic sobre un asiento ya seleccionado por esta sesión lo deselecciona?**
   `deseleccionar_asiento` lo asume (toggle), pero no está confirmado como sí lo está el
   resto de la lógica (heredada 1:1 de v1). Si JELAF abre un modal en vez de deseleccionar,
   hay que ajustar ese método primero.
2. **¿Se puede llenar el pasajero de un asiento DESPUÉS de haber seleccionado otros
   asientos posteriores, revisitando su pestaña?** Esta es la hipótesis central del
   prototipo — v1 nunca lo necesitó porque siempre llena cada pasajero inmediatamente
   después de seleccionar su asiento. Si esto falla (p. ej. JELAF pisa datos de otra
   pestaña), la implicación de producto es real: la pantalla de "seleccionar asientos"
   tendría que pedir al menos el documento del pasajero en el momento de tocar cada
   asiento, en vez de posponerlo todo a una pantalla posterior.
3. **Tamaño del pool.** Este prototipo es deliberadamente de 1 worker. Con una sola
   cuenta JELAF, el sistema solo admite una venta en curso a la vez a partir del primer
   asiento tocado — el resto de clientes deben ver un mensaje de espera explícito en vez
   de competir en silencio. Subir el paralelismo real requiere conseguir cuentas JELAF
   adicionales (una por "mostrador virtual" deseado).
4. **`headless=False`** se mantiene igual que en v1 para poder observar la prueba. Antes
   de llevar esto a un servidor sin entorno gráfico, probar si JELAF tolera
   `headless=True` (Chromium moderno) o si hace falta un display virtual (Xvfb).
