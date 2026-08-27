# Instalación y ejecución local

## Requisitos

- Python 3.9+
- **Google Chrome instalado** (Playwright lo abre con `channel="chrome"`, es decir, usa el Chrome
  real del sistema, no el Chromium embebido — así que `playwright install` no hace falta para este
  canal, pero sí tener Chrome instalado)
- Un archivo `.env` en la raíz de `CHATBOT/` (ver más abajo)

## Dependencias

```bash
pip install -r requirements.txt
```

## Variables de entorno (`.env`)

El backend carga `.env` automáticamente con `load_dotenv()`. Variables usadas:

| Variable | Para qué |
|---|---|
| `JELAF_USUARIO`, `JELAF_PASSWORD` | Login en JELAF (si faltan, el código tiene *defaults* hardcodeados — ver [`known-issues-and-security.md`](known-issues-and-security.md)) |
| `WHATSAPP_TOKEN`, `PHONE_NUMBER_ID` | Envío de mensajes vía Graph API de Meta |
| `META_VERIFY_TOKEN` | Verificación del webhook (default en código: `MoqueguaBot2026`) |
| `YUPY_CLIENT_ID`, `YUPY_CLIENT_SECRET` | Autenticación contra la pasarela de pago Yupy (sandbox) |

⚠️ El repo **ya tiene un `.env` commiteado con valores reales** — no es un ejemplo. Antes de tocar
esto, leer [`known-issues-and-security.md`](known-issues-and-security.md).

## Levantar el backend

```bash
cd CHATBOT
python api_ventas.py
```

Al arrancar intenta iniciar sesión en JELAF automáticamente (abre un Chrome visible). Cuando el
login termina ves en consola:

```
INFO:     Uvicorn running on http://0.0.0.0:8000
```

**En Windows/PowerShell**, si la consola revienta con `UnicodeEncodeError` al imprimir el banner de
arranque (caracteres de caja `╔══╗`), forzar UTF-8 antes de correrlo:

```powershell
$env:PYTHONIOENCODING = "utf-8"
python api_ventas.py
```

Documentación interactiva de la API (Swagger) disponible en `http://localhost:8000/docs` mientras
corre.

## Levantar el frontend

Las páginas usan `fetch()`, así que **no se pueden abrir como `file://`** — hace falta un servidor
HTTP mínimo, en otra terminal:

```bash
cd CHATBOT
python -m http.server 5500
```

Abrir en el navegador:

```
http://localhost:5500/buscar%20viaje/1-2-corregir.html
```

## Apuntar el frontend a tu backend local

Las páginas tienen `API_BASE_URL` hardcodeado apuntando a un túnel de Ngrok (ver
[`frontend.md`](frontend.md)). Para probar contra tu backend local, cambiar esa línea a
`http://localhost:8000` en los 6 archivos que la tienen (no hay un único punto de configuración).

## ⚠️ Cuidado al probar el flujo completo

- **Buscar viajes, ver el plano, bloquear/liberar asiento** pegan contra el **JELAF real** (no hay
  entorno de pruebas separado). Bloquear un asiento genera un hold real; si sales de la pantalla
  sin confirmar, el frontend lo libera automáticamente (`pagehide`), pero si el navegador se cierra
  a la fuerza puede quedar colgado un rato.
- **`confirmar-compra`** emite un **boleto real** en JELAF (ver
  [`backend-api.md`](backend-api.md#post-apiv1confirmar-compra)) — no lo dispares en pruebas salvo
  que sepas lo que implica.
- Hacer varios logins seguidos contra JELAF en poco tiempo puede hacer que JELAF empiece a devolver
  errores (visto en pruebas: varios reinicios del backend seguidos terminaron en `500` en
  `buscar-viajes`).
