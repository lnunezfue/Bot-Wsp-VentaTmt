# Problemas conocidos y seguridad

Ordenado por prioridad. Lo de la sección 1 es urgente y accionable hoy mismo; el resto es deuda
técnica que conviene conocer pero no bloquea el uso del sistema.

## 1. 🔴 Crítico — credenciales reales expuestas en un repo público

**Estado verificado el 2026-08-14:** el repositorio `lnunezfue/Bot-Wsp-VentaTmt` en GitHub es
**público** (`visibility: public`). El commit `Payment-demo` (de un colaborador) agregó un archivo
`.env` con secretos reales, y quedó commiteado — **ahora mismo cualquiera puede verlo** en
`https://github.com/lnunezfue/Bot-Wsp-VentaTmt/blob/main/.env`.

Lo que contiene ese `.env` (valores no reproducidos aquí a propósito):

- `WHATSAPP_TOKEN` — token de acceso a la Graph API de Meta para el número de WhatsApp de la empresa.
- `PHONE_NUMBER_ID`, `WABA_ID` — identificadores de la cuenta de WhatsApp Business.
- `META_VERIFY_TOKEN` — token de verificación del webhook.
- `JELAF_USUARIO`, `JELAF_PASSWORD` — credenciales de acceso al sistema real de venta de pasajes.
- `YUPY_CLIENT_ID`, `YUPY_CLIENT_SECRET` — credenciales de la pasarela de pago (sandbox).

**Agravante:** `.gitignore` **no incluye `.env`** (solo ignora `__pycache__/`, `*.pyc`,
`boletos_generados/`, `.venv/`, `venv/`), a pesar de que `docs/meta-whatsapp-config.md` ya decía
que debía estar excluido — es decir, la intención existía pero nunca se aplicó.

**Además**, independientemente del `.env`, hay secretos **hardcodeados directamente en el código
fuente** de `api_ventas.py` como valores por defecto (se usan si la variable de entorno no está
definida):

```python
JELAF_USUARIO = os.getenv("JELAF_USUARIO", "613")
JELAF_PASSWORD = os.getenv("JELAF_PASSWORD", "3976")
...
client_id = os.getenv("YUPY_CLIENT_ID", "cli_sbx_EKWeMZcR_Ougox6ZjSyfWu74")
client_secret = os.getenv("YUPY_CLIENT_SECRET", "sec_sbx_M1PqaLzIwZbQgtAvap73MQNDbpe5ERzXHIQFchUxzR0")
```

Esos valores están en `.py`, no en `.env` — así que aunque se arregle el `.gitignore`, **siguen
expuestos** mientras sigan en el código.

### Qué hacer (en este orden)

1. **Rotar ya las credenciales expuestas**, sin esperar a limpiar el repo (el repo público ya las
   filtró; rotarlas es lo único que las neutraliza de verdad):
   - WhatsApp: generar un token nuevo desde el panel de Meta (ver
     [`meta-whatsapp-config.md`](meta-whatsapp-config.md)) e invalidar el actual.
   - JELAF: cambiar la contraseña de esa cuenta si el sistema lo permite.
   - Yupy: regenerar el `client_secret` desde su panel de sandbox.
2. **Quitar `.env` del control de versiones:**
   ```bash
   git rm --cached .env
   echo ".env" >> .gitignore
   git commit -m "Quitar .env del repo y agregarlo a .gitignore"
   ```
3. **Quitar los defaults hardcodeados** de `api_ventas.py` (usar `os.getenv("X")` sin segundo
   argumento, y fallar rápido al arrancar si falta alguna variable requerida, en vez de caer
   silenciosamente a un valor real).
4. **Purgar el `.env` del historial de git** (rotar credenciales ya las invalida, pero el archivo
   seguiría visible en commits viejos). Esto reescribe historia pública compartida — coordinar con
   el compañero antes de hacerlo (`git filter-repo` o BFG Repo-Cleaner + `push --force` + que todos
   vuelvan a clonar).
5. Considerar si el repo debería ser **privado** en vez de público, dado que además de secretos
   contiene la lógica de negocio completa de la empresa.

## 2. 🟠 Arquitectura frágil por diseño

- **`confirmar-compra` es RPA puro** (Playwright haciendo clic en la UI real de JELAF). Depende de
  que los `id`/selectores de esa interfaz no cambien y de tiempos de espera fijos
  (`wait_for_timeout` en vez de esperar condiciones). Cualquier cambio visual en JELAF puede romper
  la emisión de boletos sin previo aviso. No hay reintentos ni recuperación parcial si falla a
  mitad del flujo (ej. después de bloquear asientos pero antes de emitir el boleto).
- **Chrome no-headless en el servidor.** Tanto el login inicial como `confirmar-compra` abren
  Chrome con `headless=False` — el proceso que corre el backend necesita poder abrir una ventana
  gráfica. En un servidor sin entorno gráfico esto no funciona tal cual (necesitaría un display
  virtual tipo Xvfb, o pasar a headless si JELAF lo permite).
- **Una sola sesión JELAF global** (`sesion_global_jelaf`) compartida por todos los usuarios
  concurrentes del backend. Si esa sesión expira o el login inicial falla, **todos** los endpoints
  que dependen de JELAF quedan caídos hasta reiniciar el backend — no hay reintento automático de
  login.
- **Sin base de datos.** El pedido vive en el `localStorage` del cliente hasta el pago; no hay
  forma de auditar o recuperar un pedido si el navegador se cierra antes de confirmar, ni de saber
  desde el backend cuántos pedidos están "en curso".

## 3. 🟡 Bugs conocidos

- **Total "S/ NaN"** en selección de asiento: si JELAF no devuelve precio (`PrecioVenta` /
  `PrecioNormal`) para ningún asiento de un piso, ese piso se queda sin la clave `price` y el
  cálculo del total en el frontend da `NaN`. Detalle técnico en
  [`backend-api.md`](backend-api.md#post-apiv1plano-bus). Reproducido en pruebas manuales sobre un
  servicio de 2 pisos.
- **RUC / tipo de documento no llega al backend.** El frontend captura y valida `tipoDocumento`
  (DNI/Pasaporte/CE/RUC), pero el modelo `Pasajero` del backend solo tiene `documento` como texto
  libre — el dato se pierde. La idea de "RUC = generar factura en vez de boleta" está solo en la
  etiqueta del `<option>`, no hay lógica de facturación en `descargar_pdf_boleto` (siempre genera
  "BOLETA DE VENTA ELECTRÓNICA").
- **Webhook sin dedup ni verificación de firma.** `POST /api/v1/webhook` no valida
  `X-Hub-Signature-256` (cualquiera que adivine la URL podría mandarle payloads falsos) ni
  deduplica por `message.id` (si Meta reintenta por timeout, se puede procesar el mismo mensaje dos
  veces y mandar el botón de compra repetido).
- **URL del frontend hardcodeada en el webhook**, apuntando a un dominio (`transportesmoquegua.com/beta/...`)
  que puede no estar desplegado todavía — hay un comentario `⚠️ MUY IMPORTANTE` en el código
  recordando actualizarla.

## 4. 🟢 Deuda técnica / limpieza pendiente

- ~~Sin `requirements.txt`~~ — **resuelto** (2026-08-14): ya existe `requirements.txt` en la raíz
  con las versiones fijadas (`fastapi==0.139.2`, `uvicorn==0.51.0`, `requests==2.34.2`,
  `pydantic==2.11.5`, `python-dotenv==1.1.0`, `playwright==1.58.0`, `reportlab==5.0.1`).
- **`API_BASE_URL` hardcodeado en 6 archivos HTML distintos**, sin un punto central de
  configuración (ver [`frontend.md`](frontend.md)) — cambiar de entorno significa editar 6
  archivos a mano.
- **`boletos_generados/` es una carpeta muerta.** La generación de PDF quedó en memoria
  (`descargar_pdf_boleto` con `BytesIO`, sin guardar en disco); nada en el código escribe ya en
  esa carpeta.
- **Archivos HTML de diseño sin usar** mezclados con los reales en las mismas carpetas
  (`buscar viaje/code.html`, `seleccionar asiento/5-7.html`, `seleccionar asiento/seleccionar-asiento.html`),
  más capturas `.png` y `.zip` del diseño original — ver [`frontend.md`](frontend.md) para la lista
  completa de cuáles son los archivos "vivos".
- **`Bot-Wsp-VentaTmt.zip` (~4 MB) commiteado en el repo** — bloat innecesario en el historial de
  git; probablemente un respaldo que se subió sin querer.
- **`prueba.py`** es un script suelto de prueba manual (envío de una plantilla de WhatsApp a un
  número fijo) que no forma parte de la app — dejarlo claro para que no se confunda con código de
  producción.
- **Meta app en modo desarrollo** — número de prueba, límite de 5 destinatarios, verificación de
  negocio pendiente. Ver el checklist completo en
  [`meta-whatsapp-config.md`](meta-whatsapp-config.md#6-pendiente-para-salir-de-pruebas-no-configurado-aún).
- **Ngrok gratuito**: el dominio del webhook cambia en cada reinicio, así que hay que
  reconfigurar la URL en el panel de Meta cada vez que se reinicia el túnel durante desarrollo.
