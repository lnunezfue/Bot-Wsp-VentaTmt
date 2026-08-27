# Problemas conocidos y seguridad

Ordenado por prioridad. El punto 1 es urgente y accionable de inmediato; el resto corresponde a
deuda técnica que conviene conocer, pero que no bloquea el uso del sistema.

## 1. Prioridad crítica — credenciales reales expuestas en un repositorio público

**Estado verificado el 2026-08-14:** el repositorio `lnunezfue/Bot-Wsp-VentaTmt` en GitHub es
público (`visibility: public`). El commit `Payment-demo` (de un colaborador) agregó un archivo
`.env` con secretos reales, y quedó incluido en el historial. Actualmente es visible para cualquier
persona en `https://github.com/lnunezfue/Bot-Wsp-VentaTmt/blob/main/.env`.

Contenido de ese `.env` (los valores no se reproducen en este documento de forma intencional):

- `WHATSAPP_TOKEN`: token de acceso a la Graph API de Meta para el número de WhatsApp de la
  empresa.
- `PHONE_NUMBER_ID`, `WABA_ID`: identificadores de la cuenta de WhatsApp Business.
- `META_VERIFY_TOKEN`: token de verificación del webhook.
- `JELAF_USUARIO`, `JELAF_PASSWORD`: credenciales de acceso al sistema real de venta de pasajes.
- `YUPY_CLIENT_ID`, `YUPY_CLIENT_SECRET`: credenciales de la pasarela de pago (sandbox).

**Factor agravante:** `.gitignore` no incluye `.env` (solo excluye `__pycache__/`, `*.pyc`,
`boletos_generados/`, `.venv/` y `venv/`), a pesar de que `docs/meta-whatsapp-config.md` ya
indicaba que debía estar excluido. La intención existía, pero nunca se aplicó.

Además, independientemente del `.env`, existen secretos escritos directamente en el código fuente
de `api_ventas.py` como valores por defecto (se utilizan cuando la variable de entorno
correspondiente no está definida):

```python
JELAF_USUARIO = os.getenv("JELAF_USUARIO", "613")
JELAF_PASSWORD = os.getenv("JELAF_PASSWORD", "3976")
...
client_id = os.getenv("YUPY_CLIENT_ID", "cli_sbx_EKWeMZcR_Ougox6ZjSyfWu74")
client_secret = os.getenv("YUPY_CLIENT_SECRET", "sec_sbx_M1PqaLzIwZbQgtAvap73MQNDbpe5ERzXHIQFchUxzR0")
```

Estos valores están en el archivo `.py`, no en `.env`; por lo tanto, aunque se corrija el
`.gitignore`, seguirán expuestos mientras permanezcan en el código.

### Acciones recomendadas, en este orden

1. **Rotar de inmediato las credenciales expuestas**, sin esperar a limpiar el repositorio (el
   repositorio público ya las filtró; rotarlas es la única acción que las neutraliza de forma
   efectiva):
   - WhatsApp: generar un token nuevo desde el panel de Meta (ver
     [`meta-whatsapp-config.md`](meta-whatsapp-config.md)) e invalidar el actual.
   - JELAF: cambiar la contraseña de esa cuenta si el sistema lo permite.
   - Yupy: regenerar el `client_secret` desde su panel de sandbox.
2. **Retirar `.env` del control de versiones:**
   ```bash
   git rm --cached .env
   echo ".env" >> .gitignore
   git commit -m "Quitar .env del repo y agregarlo a .gitignore"
   ```
3. **Eliminar los valores por defecto** de `api_ventas.py` (usar `os.getenv("X")` sin un segundo
   argumento, y detener el arranque del backend si falta alguna variable requerida, en lugar de
   recurrir de forma silenciosa a un valor real).
4. **Purgar el `.env` del historial de git.** Rotar las credenciales las invalida, pero el archivo
   seguirá siendo visible en commits antiguos. Esta operación reescribe historia compartida;
   coordinar con el resto del equipo antes de ejecutarla (`git filter-repo` o BFG Repo-Cleaner,
   seguido de `push --force` y una nueva clonación por parte de todos los colaboradores).
5. Evaluar si el repositorio debería ser privado en lugar de público, considerando que además de
   secretos contiene la lógica de negocio completa de la empresa.

## 2. Prioridad alta — arquitectura frágil por diseño

- **`confirmar-compra` se implementa como RPA puro** (Playwright interactuando con la interfaz real
  de JELAF). Depende de que los identificadores y selectores de esa interfaz no cambien, y de
  tiempos de espera fijos (`wait_for_timeout`) en lugar de esperar condiciones específicas.
  Cualquier cambio visual en JELAF puede interrumpir la emisión de boletos sin previo aviso. No
  existen reintentos ni recuperación parcial si el proceso falla a mitad del flujo (por ejemplo,
  después de bloquear los asientos pero antes de emitir el boleto).
- **Venta de un único asiento por transacción confirmada; la venta de dos o más asientos en una
  misma transacción está en desarrollo.** El flujo de un asiento fue validado con boletos reales
  emitidos en JELAF. El flujo de múltiples asientos requiere pasos adicionales dentro de la
  interfaz de JELAF (selección secuencial de cada asiento, navegación entre pestañas de pasajero,
  y un formulario de pago que solo se habilita tras completar todos los pasajeros) y todavía
  presenta fallas intermitentes.
- **Chrome se ejecuta sin modo headless en el servidor.** Tanto el inicio de sesión inicial como
  `confirmar-compra` abren Chrome con `headless=False`; el proceso que ejecuta el backend necesita
  poder abrir una ventana gráfica. En un servidor sin entorno gráfico esto no funciona sin
  modificaciones (requeriría un display virtual como Xvfb, o pasar a modo headless si JELAF lo
  permite).
- **Una única sesión JELAF global** (`sesion_global_jelaf`) compartida por todos los usuarios
  concurrentes del backend. Si esa sesión expira o el inicio de sesión inicial falla, todos los
  endpoints que dependen de JELAF quedan inoperativos hasta reiniciar el backend; no hay reintento
  automático de inicio de sesión.
- **Sin base de datos.** El pedido reside en el `localStorage` del cliente hasta el momento del
  pago; no existe forma de auditar o recuperar un pedido si el navegador se cierra antes de
  confirmar, ni de determinar desde el backend cuántos pedidos están en curso.

## 3. Prioridad media — bugs conocidos

- **Total "S/ NaN"** en la selección de asiento: si JELAF no devuelve precio (`PrecioVenta` o
  `PrecioNormal`) para ningún asiento de un piso, ese piso queda sin la clave `price` y el cálculo
  del total en el frontend resulta en `NaN`. Detalle técnico en
  [`backend-api.md`](backend-api.md#post-apiv1plano-bus). Reproducido en pruebas manuales sobre un
  servicio de dos pisos.
- **El tipo de documento y el RUC no llegan al backend.** El frontend captura y valida
  `tipoDocumento` (DNI, Pasaporte, CE, RUC), pero el modelo `Pasajero` del backend solo tiene
  `documento` como texto libre, por lo que ese dato se pierde. La opción de generar factura en
  lugar de boleta cuando el documento es RUC quedó únicamente en la etiqueta del `<option>`; no
  existe lógica de facturación en `descargar_pdf_boleto` (siempre genera "BOLETA DE VENTA
  ELECTRÓNICA").
- **El webhook no valida firma ni deduplica mensajes.** `POST /api/v1/webhook` no valida
  `X-Hub-Signature-256` (cualquiera que conozca la URL podría enviar payloads falsos) ni deduplica
  por `message.id` (si Meta reintenta por timeout, el mismo mensaje puede procesarse dos veces y
  enviar el botón de compra de forma repetida).
- **URL del frontend fija en el código del webhook**, apuntando a un dominio
  (`transportesmoquegua.com/beta/...`) que puede no estar desplegado todavía; existe un comentario
  en el código que advierte sobre la necesidad de actualizarla.

## 4. Prioridad baja — deuda técnica y limpieza pendiente

- Falta de `requirements.txt`: resuelto (2026-08-14). Ya existe `requirements.txt` en la raíz con
  las versiones fijadas (`fastapi==0.139.2`, `uvicorn==0.51.0`, `requests==2.34.2`,
  `pydantic==2.11.5`, `python-dotenv==1.1.0`, `playwright==1.58.0`, `reportlab==5.0.1`).
- **`API_BASE_URL` fijo en seis archivos HTML distintos**, sin un punto central de configuración
  (ver [`frontend.md`](frontend.md)); cambiar de entorno implica editar los seis archivos de forma
  manual.
- **`boletos_generados/` es una carpeta sin uso actual.** La generación de PDF quedó en memoria
  (`descargar_pdf_boleto`, usando `BytesIO`, sin escritura en disco); ninguna parte del código
  escribe ya en esa carpeta.
- **Archivos HTML de diseño sin uso**, mezclados con los archivos activos en las mismas carpetas
  (`buscar viaje/code.html`, `seleccionar asiento/5-7.html`,
  `seleccionar asiento/seleccionar-asiento.html`), junto con capturas `.png` y `.zip` del diseño
  original; ver [`frontend.md`](frontend.md) para la lista completa de archivos activos.
- **`Bot-Wsp-VentaTmt.zip` (aproximadamente 4 MB) incluido en el repositorio**: incrementa
  innecesariamente el tamaño del historial de git; probablemente corresponde a un respaldo subido
  por error.
- **`prueba.py`** es un script de prueba manual (envío de una plantilla de WhatsApp a un número
  fijo) que no forma parte de la aplicación; debe quedar claro que no es código de producción.
- **Aplicación de Meta en modo desarrollo:** número de prueba, límite de 5 destinatarios,
  verificación de negocio pendiente. Ver el detalle completo en
  [`meta-whatsapp-config.md`](meta-whatsapp-config.md#6-pendiente-para-salir-de-modo-pruebas).
- **Ngrok gratuito:** el dominio del webhook cambia en cada reinicio, por lo que la URL debe
  reconfigurarse en el panel de Meta cada vez que se reinicia el túnel durante el desarrollo.
