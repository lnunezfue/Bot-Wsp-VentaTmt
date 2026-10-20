# CLAUDE.md — Bot WhatsApp de ventas TMT, venta SIN NAVEGADOR

Bot de venta de pasajes por WhatsApp para Transportes Moquegua Turismo (TMT).
El cliente escribe "comprar" → recibe un botón → abre una mini-webapp (HTML +
Tailwind CDN, sin build) → el backend FastAPI compra el pasaje **en el sistema
real de la empresa (JELAF) por HTTP**, con las llamadas internas que usa la
propia boletería → cobra con Yupy. Varias ventas a la vez con una sola cuenta.
No abre navegador en ningún momento: también el login es HTTP (validado en vivo el 04/10/2026).
El paquete `paneles/` registra todo en SQLite y lo publica en un tablero.

Este proyecto nació como copia de `CHATBOT PANELES` (venta con navegador). Todo
el código, los comentarios y los docs están en **español**. Mantenerlo así.

## Lectura obligatoria antes de tocar algo

- `docs/venta-sin-navegador.md` — qué cambió respecto al navegador, flujo, modos y
  la lista de lo que hay que validar antes de `VENTA_MODO=real`.
- `docs/descubrimiento-venta-http.md` — las llamadas de JELAF capturadas (fuente de verdad del contrato).
- `docs/AUDITORIA.md` y `docs/REVISION_2026-09-26.md` — hallazgos (V1, S2, N3…). Referirse a ellos por su código.
- `docs/PLAN_PANELES.md` — paneles.

## Mapa del código

| Archivo | Qué es |
|---|---|
| `api_ventas.py` | App FastAPI (puerto 8000): catálogo (`/api/v1/buscar-viajes`, `plano-bus`, `consultar-cliente`), venta `/api/v2/venta/...`, PDF, webhook de Meta. Monta `paneles`. |
| `config.py` | Única lectura del entorno. `ENTORNO=produccion` no arranca si falta un secreto o `VENTA_MODO`. |
| `jelaf_http.py` | Todo el HTTP con JELAF: login por HTTP (cookie de sesión + datos del cajero), lectura y escritura (`bloquear_asiento`, `liberar_arreglo`, `grabar_pasajeros`, `grabar_venta`). `ErrorJelafRechazo` = JELAF dijo que no (no se hizo). |
| `venta_jelaf.py` | Funciones **puras** que arman los cuerpos de bloqueo, pasajero y venta, y leen la respuesta. `POR_VALIDAR`: valores fijos aún no confirmados. |
| `vendedor_http.py` | `VentaHttp` (una venta: asientos con su id de bloqueo, pasajeros, emisión), `VendedorHttp` (las ventas de UNA cuenta: capacidad, reaper, keepalive, modo simulación) y `PoolCuentas` (varias cuentas; cada venta atada a la suya; emisión de a una entre todas). |
| `pagos_yupy.py`, `pagos_niubiz.py`, `whatsapp.py`, `planos.py`, `boleto_pdf.py`, `limitador.py`, `privacidad.py` | Yupy (+ `evaluar_orden`, `medio_de_pago` pendiente, modo `YUPY_API`), Niubiz (tarjeta: autorización hecha por el servidor), enlace firmado, webhook (un solo mensaje, una vez al día por número), planos, PDF, límite de peticiones, enmascarado de PII. |
| `frontend/config.js` | URL del backend y `esc()` para la mini-webapp. |
| `tests/` | pytest con dobles (`conftest.py`: `JelafHttpFalso` con estado, `YupyFalso`), `servidor_con_dobles.py` y `servidor_estatico.py`. |
| `paneles/`, `paneles_web/index.html` | Paneles (igual que en CHATBOT PANELES + `boleto.link_comprobante`, estado de venta `simulada`). |
| `frontend/` | Mini-webapp: lo único que se sube al hosting, sin subcarpetas (ver `docs/frontend.md`). `diseno_referencia/` = capturas y borradores, no se sube. |
| `bloqueos_registro.py`, `liberar_asientos.py` | Registro en disco (`bloqueos_jelaf.db`, sin PII) de cada asiento que el bot bloquea, hasta soltarlo o venderlo; consola para ver los que quedaron pegados y **liberarlos en JELAF real** con la cuenta que los bloqueó (lo ejecuta el usuario). |
| `quitar_cliente_prueba.py` | Saca de los paneles las compras de prueba de un documento del equipo: sus ventas pasan a `anulada` y sus boletos quedan sin datos personales; borra su ficha y sus bonos; respalda la base antes y pide confirmación (lo ejecuta el usuario). |
| `prueba_login_cuentas.py` | Prueba en vivo **solo de login** de todas las cuentas del `.env` (Terminal, caja); no consulta ni vende. |
| `prueba_venta_http.py` | Prueba en vivo **de solo lectura** contra JELAF: login, viaje real, arma los cuerpos sin enviarlos. |
| `descubrir_endpoints_venta.py` | Captura **pasiva** de una venta de boletería (sin datos personales); muestra los valores POR VALIDAR. |
| `prueba.py` | Envía un WhatsApp real. No es un test. |

Flujo: `buscar-viaje` → `elegir-horario` (`/iniciar`) → `seleccionar-asiento` (bloqueo en JELAF) →
`datos-pasajeros` (`/pasajero`, con sexo y tipo de documento) → `resumen-compra` (`/monto`) →
`pago` (`/checkout`: graba pasajeros en JELAF y crea el cobro → Yupy → `/confirmar`: emite) →
`boletos-emitidos` (PDF + enlace al comprobante electrónico). Estado del cliente en
`sessionStorage["vibeTransitBooking"]`: una compra por pestaña.

## Cómo correrlo

```bash
pip install -r requirements.txt           # Python 3.13; no necesita Chrome
python api_ventas.py                      # backend :8000 — login en JELAF REAL; VENTA_MODO=simulacion por defecto
python tests/servidor_estatico.py         # mini-webapp sin caché en :5500
```

Sin servicios reales (JELAF/Yupy/Meta falsos), sin chocar con un backend en :8000:

```bash
python tests/servidor_con_dobles.py --puerto 8001 [--capacidad 10] [--simulacion]
python tests/servidor_estatico.py 5502 --api http://127.0.0.1:8001
```

## Forma de trabajar

1. **Entender antes de tocar.** Si el cambio toca venta, pago, JELAF, webhook o datos personales,
   proponer un plan corto y esperar el visto bueno.
2. **Cambios pequeños y enfocados.**
3. **Evidencia antes de decir "listo".** Correr la verificación y mostrar la salida real.
4. **Si un cambio resuelve o crea un hallazgo**, actualizar `docs/REVISION_*.md`.
5. **No inventar contratos externos** (JELAF, Yupy, Meta). El contrato de JELAF sale de las capturas
   (`docs/descubrimiento-venta-http.md`); un campo nuevo se confirma con otra captura, no se adivina.
   Lo no confirmado va en `venta_jelaf.POR_VALIDAR` y se documenta.
6. **Ante la duda sobre algo que emite, cobra, bloquea o envía, detenerse y preguntar.**

Skills del proyecto (`.claude/skills/`): `venta-y-pago`, `rpa-jelaf` (aplica a `jelaf_http.py` /
`vendedor_http.py`), `webhook-whatsapp`, `paneles-y-datos-personales`,
`verificacion-sin-servicios-reales`, y **siempre** `revision-seguridad` antes de dar algo por terminado.

## Reglas del proyecto (no negociables)

1. **Todo toca sistemas reales.** Con `VENTA_MODO=real`, `/seleccionar-asiento` bloquea inventario
   real, `/checkout` graba clientes en JELAF y `/confirmar` **emite comprobantes electrónicos reales**.
   Nunca llamarlos contra el backend real para probar sin permiso explícito. No ejecutar
   `api_ventas.py`, `prueba*.py`, `descubrir_endpoints_venta.py` ni `seed_demo --reset` sin permiso.
   Antes de lanzar servidores de prueba, mirar qué puertos están ocupados: **no tocar procesos del
   usuario** (ni por nombre: filtrar por proceso padre).
2. **Escrituras en JELAF sin reintento a ciegas.** `grabar_venta` usa `reintentar=False`; si no hay
   respuesta clara, se concilia contra el plano. Distinguir `ErrorJelafRechazo` (no se hizo) de
   `ErrorJelaf` (no se sabe).
3. **Emisión de a una** (`lock_emision`, compartido por todas las cuentas de `PoolCuentas`): no está
   confirmado que cada usuario de JELAF tenga su propia caja.
4. **Todo camino libera los bloqueos** (cancelar, reaper, error antes de emitir): `liberar_y_resetear`.
5. **Los paneles nunca pueden tumbar una venta** (`@seguro` en `paneles/registro.py`).
6. **El repositorio puede ser público.** Nunca commitear `.env`, `*.db`, `descubrimiento_output/`
   ni datos personales. Sin secretos como valor por defecto.
7. **No confiar en el navegador** para precio, monto, pago ni identidad (V1/V2).
8. **Operaciones sobre una venta** dentro de `async with venta.lock:` re-verificando el dueño (C2).
9. **Emisión exactamente una vez** (`/confirmar` idempotente) y pago verificado con Yupy servidor a servidor.
10. **Fallar cerrado** si falta un secreto de seguridad.
11. **Esquema** solo en `paneles/esquema.sql` (idempotente); columnas nuevas en `db._COLUMNAS_NUEVAS`.

## Seguridad

Igual que en CHATBOT PANELES: validación Pydantic en el borde, SQL con `?`, `hmac.compare_digest`,
CORS por lista, límites de peticiones, errores genéricos al cliente, `window.TMT.esc()` para
innerHTML, PII enmascarada en logs (`privacidad.py`), el Token y las cookies de JELAF nunca en logs.

## Verificación

```bash
python -m pytest                          # suite completa (sin servicios reales)
python -m pyflakes *.py paneles/ tests/
```

- `tests/conftest.py`: anula el `.env`, pone secretos de prueba y `VENTA_MODO=real` contra
  `JelafHttpFalso` (estado real de bloqueos/ventas, fallos a pedido: `falla_grabar_venta =
  "rechazo" | "sin_respuesta" | "emite_y_cae"`). Fixture `entorno_venta`.
- `tests/test_venta_jelaf.py` compara los cuerpos con la captura real: si JELAF cambia, se actualiza
  primero la captura/documento y después el test.
- Bug corregido = test que lo reproduce primero; comprobar que el test falla sin la corrección.

## Variables de entorno

`ENTORNO`, `HOST`, `PORT`, `CORS_ORIGENES`, `JELAF_USUARIO`/`JELAF_PASSWORD`,
`JELAF_USUARIO_1…`/`JELAF_PASSWORD_1…` (la `JELAF_USUARIO` sin número queda de reserva, última en usarse), `VENTA_MODO` (`simulacion`|`real`), `VENTAS_POR_CUENTA` (1), `JELAF_TARJETA_DEFECTO`, `YUPY_API`, `NIUBIZ_*`, `URL_TERMINOS`
(`visa`), `YUPY_*`, `WHATSAPP_TOKEN`, `PHONE_NUMBER_ID`, `META_VERIFY_TOKEN`, `META_APP_SECRET`,
`URL_FRONTEND`, `ENLACE_SECRETO`, `EXIGIR_CONTACTO`, `PANELES_*`, `META_PLANTILLA_*`,
`DURACION_MAXIMA_SELECCION_SEGUNDOS`. Tabla completa en `docs/setup-local.md`. Ya no existen
`JELAF_CLAVE_AUTH_*`, `JELAF_WORKERS_POR_CUENTA` ni `MAX_VENTAS_SIMULTANEAS`.

## Convenciones

Nombres en español; comentarios que explican el porqué; fechas `YYYY-MM-DD` en la web y
`DD/MM/YYYY` en JELAF; asientos `"05"`; dinero en `Decimal`; nada de `except` mudo fuera de
`@seguro`; commits en español (`fix:`/`feat:`/`sec:`).
