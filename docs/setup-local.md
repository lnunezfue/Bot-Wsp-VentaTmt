# Instalación y ejecución local

## Requisitos

- Python 3.11 o superior (en la máquina de desarrollo: 3.13).
- No hace falta Chrome ni Playwright: el bot entra a JELAF por HTTP (también el login). Solo
  `descubrir_endpoints_venta.py` usa Chrome, y su dependencia está en `requirements-dev.txt`.
- Un archivo `.env` en la raíz del proyecto (ver abajo). Nunca se sube al repositorio.

## Dependencias

```bash
pip install -r requirements.txt          # aplicación
pip install -r requirements-dev.txt      # tests (pytest, httpx, pyflakes)
```

## Variables de entorno (`.env`)

`config.py` las lee una sola vez al arrancar. Con `ENTORNO=produccion`, si falta cualquier secreto
de seguridad **el backend no arranca**; con `ENTORNO=desarrollo` (por defecto) arranca y avisa en
consola de cada protección relajada.

| Variable | Obligatoria | Uso |
|---|---|---|
| `ENTORNO` | — | `desarrollo` (defecto) o `produccion`. |
| `HOST`, `PORT` | — | Por defecto `127.0.0.1:8000` (antes `0.0.0.0`: exponía el backend a toda la red, S8). |
| `CORS_ORIGENES` | prod | Orígenes permitidos, separados por coma. En desarrollo: `http://localhost:5500,http://127.0.0.1:5500`. Agregar aquí el dominio donde se publique la mini-webapp. |
| `JELAF_USUARIO_1`/`JELAF_PASSWORD_1`, `_2`, … | **siempre** (estas o las de abajo) | Cuentas con las que vende el bot (Y1…Y6, sucursal 46 ONLINE). Se usan en ese orden; cada venta queda atada a una cuenta de principio a fin. Un usuario numerado sin su clave no arranca. |
| `JELAF_USUARIO`, `JELAF_PASSWORD` | — | La cuenta de siempre. Si hay numeradas, queda de **reserva**: solo recibe ventas cuando todas las demás están llenas. Sola, sigue funcionando como antes. |
| `VENTA_MODO` | prod | `simulacion` (defecto en desarrollo): consulta JELAF pero no bloquea, no graba y no emite. `real`: vende de verdad. |
| `VENTAS_POR_CUENTA` | — | Compras abiertas a la vez **por cada cuenta** (defecto 1; total = cuentas × este número). Reemplaza a `MAX_VENTAS_SIMULTANEAS`, que ya no se usa (si está en el `.env`, el arranque avisa). |
| `JELAF_TARJETA_DEFECTO` | — | Medio con que se registra el pago en JELAF si Yupy no informa cuál: `visa` (defecto), `mastercard`, `deposito`, `yape`, `plin`, `bbva`. |
| `YUPY_BASE_URL` | — | Una sola URL para crear y verificar pagos (defecto: sandbox). |
| `YUPY_CLIENT_ID`, `YUPY_CLIENT_SECRET` | prod | Sin ellas, `/checkout` responde 503. |
| `YUPY_CHECKOUT_SEGUNDOS` | — | Vida del checkout (defecto 900). El reaper no libera una venta con checkout vivo. |
| `YUPY_API` | — | `payment_orders` (defecto) o `checkouts` (`/v1/checkouts`: QR Yape/Plin con el modal `YupyCheckout.open`). Con `checkouts` el pago se verifica servidor a servidor con `POST /v1/checkouts/browser/status` y el `checkout_token` guardado al crear el cobro (contrato público de Yupy, https://api.yupy.us/docs). |
| `YUPY_PERMITIR_SIMULACION` | — | `1` permite vender **de verdad** con una credencial de Yupy que muestra "Simular pago" (`payments:simulate`). Por defecto no: un pago simulado es idéntico a uno real y cualquiera se llevaría un boleto gratis. En `VENTA_MODO=simulacion` siempre se puede simular. |
| `YUPY_WEBHOOK_SECRET` | — | Signing Secret (`whsec_...`) del webhook de Yupy, generado en YUPY Admin → Webhooks. URL de callback: `https://<dominio>/api/v2/yupy/webhook`. Sin él, el webhook responde 503. Los avisos se registran (tabla `yupy_webhook`) pero NO emiten: emitir es solo de `/confirmar`. |
| `NIUBIZ_MERCHANT_ID`, `NIUBIZ_USUARIO`, `NIUBIZ_PASSWORD` | — | Pago con tarjeta. Sin `NIUBIZ_MERCHANT_ID` la opción no se muestra; con él, en producción se exigen usuario y clave. Nunca en el código. |
| `NIUBIZ_BASE_URL`, `NIUBIZ_CHECKOUT_JS` | — | Defecto: sandbox. Cambiar ambas al pasar a producción: `https://apiprod.vnforapps.com` y `https://static-content.vnforapps.com/v2/js/checkout.js` (comprobado 2026-10-12; `apiprod.niubiz.com.pe` no existe y da `ConnectionError`). |
| `NIUBIZ_SESION_SEGUNDOS` | — | Tiempo que el reaper respeta mientras el cliente escribe su tarjeta (defecto 900). |
| `URL_TERMINOS` | — | Enlace de Términos y Condiciones que va en el correo con los boletos (desde 2026-10-15 WhatsApp ya no los pide). Defecto: `https://transportesmoquegua.com/terminos-pasajes.html` (`/terminos` da 404). |
| `WHATSAPP_TOKEN`, `PHONE_NUMBER_ID` | prod | Envío del botón de compra. |
| `META_VERIFY_TOKEN` | prod | Verificación del webhook. Sin ella, 403. |
| `META_APP_SECRET` | prod | Firma `X-Hub-Signature-256` de cada mensaje (developers.facebook.com → la app → Configuración → Básica → "Clave secreta de la app"). |
| `URL_FRONTEND` | prod | Página a la que lleva el botón "Comprar Pasaje" (antes fija en el código: `https://transportesmoquegua.com/beta/buscar%20viaje/buscar-viaje.html`). |
| `ENLACE_SECRETO` | prod | Firma del teléfono en el enlace. En desarrollo, sin ella, se usa uno aleatorio por proceso. |
| `EXIGIR_CONTACTO` | — | `1` exige el enlace de WhatsApp para iniciar una venta. Defecto: sí en producción, no en desarrollo. |
| `SMTP_HOST`, `SMTP_PUERTO`, `SMTP_USUARIO`, `SMTP_PASSWORD` | — | Servidor de correo para mandar los boletos (la empresa usa Titan Email: `smtp.titan.email`, puerto 465 SSL, usuario `tuboleto@transportesmoquegua.com`; dato de la configuración de Outlook, 2026-10-14). Sin ellos los boletos no se envían (queda la incidencia `correo_fallido`). La clave solo en el `.env`. |
| `CORREO_REMITENTE`, `CORREO_NOMBRE_REMITENTE` | — | Desde qué correo y con qué nombre salen los boletos (defecto: `SMTP_USUARIO` y "Transportes Moquegua Turismo"). |
| `EXIGIR_CORREO` | — | `1` = sin correo no se puede pagar. Defecto: sí en producción. |
| `WHATSAPP_NUMEROS_PERMITIDOS` | — | Lista blanca de la etapa de prueba, separada por comas (`51900000001,51900000002`). Si tiene números, el bot solo responde a esos y los demás mensajes se ignoran. Vacía = atiende a todos. Va solo en el `.env`: son teléfonos de personas. |
| `WHATSAPP_UNA_VEZ_AL_DIA` | — | `1` (defecto): el bot responde una sola vez al día por número (hora de Perú). `0`: responde a cada mensaje, **solo para probar en desarrollo** (al arrancar sale un aviso). |
| `PANELES_TOKEN` | prod | Clave del tablero `/paneles/`. |
| `PANELES_CLAVE_CIUDADES`, `PANELES_CLAVE_PASARELAS` | — | Claves que pide el panel de Administración para activar o desactivar una ciudad o una pasarela de pago (además de `PANELES_TOKEN`). Sin ellas el backend arranca, pero el panel no deja cambiar nada (avisa al arrancar). Usar valores distintos de `PANELES_TOKEN`. |
| `PANELES_DB`, `PANELES_ENVIO_REAL`, `META_PLANTILLA_*` | — | Ver `docs/PLAN_PANELES.md`. |
| `DURACION_MAXIMA_SELECCION_SEGUNDOS` | — | Tope para elegir asientos aunque haya heartbeat (defecto 1200, S3). |

## Ejecución del backend (toca JELAF REAL)

```bash
python api_ventas.py
```

Inicia sesión en JELAF por HTTP (sin navegador). La salida se
fuerza a UTF-8 al arrancar, así que ya no hace falta `PYTHONIOENCODING`.

## Ejecución del frontend

```bash
python tests/servidor_estatico.py      # sirve sin caché; con http.server el navegador guarda páginas viejas
```

Abrir `http://localhost:5500/buscar-viaje.html`. La URL del backend se configura en
un solo lugar: `frontend/config.js` (`window.TMT.API_BASE_URL`).

## Probar sin servicios reales

```bash
python -m pytest                                                   # 114 tests (sin JELAF, Yupy ni Meta)
python tests/servidor_con_dobles.py --puerto 8001                  # backend real con JELAF/Yupy/Meta falsos
python tests/servidor_estatico.py 5502 --api http://127.0.0.1:8001 # mini-webapp apuntando a ese backend
```

Así no se choca con un `api_ventas.py` corriendo en :8000. Abrir varias pestañas en
`http://127.0.0.1:5502/buscar-viaje.html` (origen TACNA, destino LIMA) para comprar a la
vez. El SDK de Yupy no carga en local; para simular un pago aprobado:
`POST http://127.0.0.1:8001/__dobles/aprobar-pago` con `{"booking_id": "..."}` y luego
`emitirBoletos(<tx>, 'YUPI_OFICIAL')` desde la consola de `pago.html`. `GET /__dobles/jelaf` muestra
los bloqueos y ventas del JELAF falso.

## Probar contra JELAF real sin vender

```bash
python prueba_venta_http.py      # login + viaje real + cuerpos armados SIN enviarlos
```

Con `VENTA_MODO=simulacion` (defecto), `api_ventas.py` también consulta JELAF de verdad pero no
bloquea asientos, no graba pasajeros y no emite. Antes de `VENTA_MODO=real`, seguir la lista de
`docs/venta-sin-navegador.md`.
