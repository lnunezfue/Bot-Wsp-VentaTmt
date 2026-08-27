# Instalación y ejecución local

## Requisitos

- Python 3.9 o superior.
- Google Chrome instalado (Playwright lo abre con `channel="chrome"`, es decir, utiliza el Chrome
  real del sistema y no el Chromium embebido; por lo tanto `playwright install` no es necesario
  para este canal, pero Chrome debe estar instalado).
- Un archivo `.env` en la raíz de `CHATBOT/` (ver más abajo).

## Dependencias

```bash
pip install -r requirements.txt
```

## Variables de entorno (`.env`)

El backend carga `.env` automáticamente mediante `load_dotenv()`. Variables utilizadas:

| Variable | Uso |
|---|---|
| `JELAF_USUARIO`, `JELAF_PASSWORD` | Inicio de sesión en JELAF (si faltan, el código utiliza valores por defecto; ver [`known-issues-and-security.md`](known-issues-and-security.md)) |
| `WHATSAPP_TOKEN`, `PHONE_NUMBER_ID` | Envío de mensajes mediante la Graph API de Meta |
| `META_VERIFY_TOKEN` | Verificación del webhook (valor por defecto en código: `MoqueguaBot2026`) |
| `YUPY_CLIENT_ID`, `YUPY_CLIENT_SECRET` | Autenticación contra la pasarela de pago Yupy (sandbox) |

**Nota importante:** el repositorio tiene actualmente un `.env` incluido en el historial con
valores reales; no se trata de un archivo de ejemplo. Antes de continuar, revisar
[`known-issues-and-security.md`](known-issues-and-security.md).

## Ejecución del backend

```bash
cd CHATBOT
python api_ventas.py
```

Al arrancar, intenta iniciar sesión en JELAF automáticamente (abre un Chrome visible). Cuando el
inicio de sesión finaliza, la consola muestra:

```
INFO:     Uvicorn running on http://0.0.0.0:8000
```

En Windows/PowerShell, si la consola falla con `UnicodeEncodeError` al imprimir el mensaje de
arranque (caracteres de caja `╔══╗`), forzar codificación UTF-8 antes de ejecutar:

```powershell
$env:PYTHONIOENCODING = "utf-8"
python api_ventas.py
```

La documentación interactiva de la API (Swagger) está disponible en `http://localhost:8000/docs`
mientras el backend se encuentra en ejecución.

## Ejecución del frontend

Las páginas utilizan `fetch()`, por lo que no pueden abrirse directamente como `file://`; se
requiere un servidor HTTP mínimo, en otra terminal:

```bash
cd CHATBOT
python -m http.server 5500
```

Abrir en el navegador:

```
http://localhost:5500/buscar%20viaje/1-2-corregir.html
```

## Configuración del frontend contra un backend local

Las páginas tienen `API_BASE_URL` definido de forma fija, apuntando a un túnel de Ngrok (ver
[`frontend.md`](frontend.md)). Para probar contra un backend local, esa línea debe cambiarse a
`http://localhost:8000` en los seis archivos que la contienen, dado que no existe un único punto de
configuración.

## Consideraciones al probar el flujo completo

- **La búsqueda de viajes, la visualización del plano, y el bloqueo/liberación de asientos**
  interactúan con JELAF real; no existe un entorno de pruebas separado. Bloquear un asiento genera
  un hold real; si se abandona la pantalla sin confirmar, el frontend lo libera automáticamente
  (`pagehide`), pero si el navegador se cierra de forma forzada, el bloqueo puede permanecer activo
  por un tiempo.
- **`confirmar-compra`** emite un boleto real en JELAF (ver
  [`backend-api.md`](backend-api.md#post-apiv1confirmar-compra)); no debe ejecutarse en pruebas sin
  tener claras sus consecuencias.
- Realizar varios inicios de sesión seguidos contra JELAF en poco tiempo puede provocar que JELAF
  comience a devolver errores (observado en pruebas: varios reinicios consecutivos del backend
  terminaron en `500` en `buscar-viajes`).
