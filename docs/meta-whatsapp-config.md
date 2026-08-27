# Configuración de Meta — WhatsApp Cloud API

> **Aplicación:** VENTA_MOQUEGUA_TURISMO
> **Business Manager:** Transportes Moquegua Turismo
> **Última actualización:** 2026-08-14

**Nota importante:** este documento no debe usarse para registrar tokens, contraseñas ni
credenciales reales. Los valores sensibles (token de acceso, App Secret, credenciales de JELAF) se
guardan en variables de entorno (`.env`, excluido por `.gitignore`), nunca en este archivo ni en el
código.

---

## 1. Resumen del objetivo

Permitir que un cliente escriba al WhatsApp de la empresa y el bot le responda con un botón que
abre la mini-webapp de compra de boletos (búsqueda de viaje, selección de asiento, datos del
pasajero, términos y condiciones, pasarela de pago, boleto), sin que el usuario necesite salir de
WhatsApp, ya que el enlace se abre en el navegador integrado de la aplicación.

---

## 2. Aplicación en Meta for Developers

- **Nombre de la aplicación:** `VENTA_MOQUEGUA_TURISMO`
- **App ID:** `875077705659589`
- **Consola:** [developers.facebook.com/apps](https://developers.facebook.com/apps)

### Incorporación del producto WhatsApp

En la interfaz actual de Meta, esta sección se denomina "Casos de uso" en lugar de "Agregar
producto":

1. Panel de la aplicación, sección "Casos de uso en esta app".
2. Seleccionar "Conectarte con los clientes a través de WhatsApp" y luego "Personalizar".
3. Se eligió el tipo de integración "Integrar con la API" (opción para negocios con desarrollo
   propio, en lugar de "Convertirte en socio").
4. Al confirmar, se agregó el ítem WhatsApp al menú lateral, con los submenús "Introducción",
   "Paso 1: Pruébalo", "Paso 2: Configuración de producción" y "Paso 3: Verificación del negocio".

---

## 3. Número de prueba (usado durante el desarrollo)

Ubicación en el panel: WhatsApp → Configuración básica → Paso 1: Pruébalo.

| Dato | Valor |
|---|---|
| Número de prueba | +1 (555) 638-7804 |
| Phone Number ID | `1180929478439391` |
| WABA ID (WhatsApp Business Account) | `1663638782432991` |
| Token de acceso | Temporal (24 horas); se regenera desde el mismo panel con el botón "Generar token" |

Al generar el token se seleccionó el siguiente alcance:
- Activado: "Activar solo las cuentas de WhatsApp actuales", con la cuenta marcada: Test WhatsApp
  Business Account (`...432991`).
- No activado: "Empresa de Transportes Moquegua Turismo" (`...948998`), que corresponde a la WABA
  real de producción; queda pendiente para cuando se configure el número definitivo.

### Destinatarios de prueba autorizados

Mientras la aplicación esté en modo desarrollo, el número de prueba solo puede enviar mensajes a un
máximo de 5 números, agregados manualmente en el selector "Destinatario" del Paso 1 (cada uno
recibe un código SMS de confirmación). Un usuario público no puede usar el bot hasta que se
configure el número real y se complete la verificación del negocio (ver sección 6).

---

## 4. Número real de producción — pendiente

Estado: bloqueado. No se cuenta todavía con acceso al número de teléfono definitivo que utilizará
la empresa.

Pasos definidos para cuando se tenga acceso (en Paso 2: Configuración de producción → "Registra tu
número de teléfono de WhatsApp"):

1. Completar el perfil de negocio (nombre visible, categoría, descripción, dirección).
2. Ingresar el número real. Si el número ya está activo en la aplicación estándar de WhatsApp, debe
   migrarse desde ahí, ya que no puede estar activo en ambos entornos simultáneamente.
3. Verificar por SMS o llamada telefónica (código de 6 dígitos).
4. Configurar el PIN de verificación en dos pasos (6 dígitos); debe guardarse en un gestor de
   contraseñas, no en texto plano.
5. Registrar el nuevo Phone Number ID que Meta asigna a ese número (será distinto al de prueba) y
   actualizarlo en la configuración del backend.

La URL del webhook y el verify token no cambian al pasar al número real; solo cambia el Phone
Number ID.

---

## 5. Webhook

### Verify token acordado

```
MoqueguaBot2026
```

Debe coincidir exactamente entre el valor configurado en el panel de Meta y el que valida el
backend en `GET /webhook`.

### Configuración en Meta

WhatsApp → Paso 2: Configuración de producción → "Configurar webhooks":
- **URL de devolución de llamada:** `<URL pública de Ngrok>/webhook`
- **Verify token:** `MoqueguaBot2026`
- **Campo suscrito:** `messages` (obligatorio; notifica los mensajes entrantes)

### Estado en el backend (`api_ventas.py`)

> Actualización 2026-08-14: esta sección ya se implementó (commit `Payment-demo`). El contenido que
> sigue describía el diseño previo a la implementación y se mantiene como referencia de la decisión
> original; el estado real es el que indica la lista siguiente.

- Implementado: endpoints reales en `api_ventas.py` (ver
  [`backend-api.md`](backend-api.md#whatsapp-cloud-api-webhook) para el detalle):
  - `GET /api/v1/webhook`: valida `hub.verify_token` contra `META_VERIFY_TOKEN` y responde
    `hub.challenge`.
  - `POST /api/v1/webhook`: recibe el mensaje; si el texto contiene la palabra `"comprar"`,
    dispara `enviar_boton_compra()` en segundo plano, que envía el botón de compra hacia
    `buscar viaje/1-2-corregir.html`, y responde `200` de inmediato.
- Diferencia respecto al diseño original: la ruta real quedó bajo `/api/v1/webhook`, no bajo
  `/webhook` como indicaba el diseño inicial. Si se reconfigura el webhook en el panel de Meta, la
  URL de devolución de llamada debe incluir el prefijo `/api/v1`.
- Pendiente: deduplicar por `message.id` (Meta reintenta el envío si la respuesta tarda) y validar
  la firma `X-Hub-Signature-256` con el App Secret. Tampoco hay lógica de intención adicional más
  allá de la búsqueda de la palabra `"comprar"` en el texto.
- Pendiente: la URL del frontend a la que apunta el botón de compra está fija en el código, dentro
  de `enviar_boton_compra()` (`https://transportesmoquegua.com/beta/...`), con un comentario que
  advierte sobre la necesidad de actualizarla antes de pasar a producción.

---

## 6. Pendiente para salir de modo pruebas

- **Verificación del negocio** en Business Manager (carga de RUC y documentos legales). Sin este
  paso, el límite de 5 destinatarios de prueba es permanente.
- **Token permanente:** reemplazar el token temporal (24 horas) por uno de System User
  (Configuración del negocio → Usuarios del sistema → generar token con el permiso
  `whatsapp_business_messaging`).
- **Plantillas de mensaje aprobadas**, en caso de requerirse notificaciones fuera de la ventana de
  24 horas (por ejemplo, un recordatorio de viaje).
- **Nombre visible y foto de perfil** del WhatsApp Business, sujetos a revisión por parte de Meta.
- **URL de webhook estable:** el dominio de Ngrok gratuito cambia en cada reinicio, por lo que para
  producción se necesita un plan de Ngrok con dominio fijo, o migrar el backend a un hosting con
  dominio propio.

---

## 7. Notas de seguridad relacionadas

> Actualización 2026-08-14: esta situación ya ocurrió y no es un riesgo hipotético. El archivo
> `.env` con el token real de WhatsApp, las credenciales de JELAF y las de Yupy quedó incluido en
> el commit `Payment-demo`, y el repositorio es público en GitHub. El detalle completo, junto con
> las acciones correctivas, está en
> [`known-issues-and-security.md`](known-issues-and-security.md); debe revisarse antes de continuar
> usando cualquiera de estas credenciales.

- El archivo `api_ventas.py` mantiene el usuario y la contraseña de JELAF
  (`JELAF_USUARIO`, `JELAF_PASSWORD`) como valores por defecto en texto plano cuando `.env` no está
  presente, y adicionalmente el `.env` real está incluido en el repositorio (ver arriba). Queda
  pendiente rotar la contraseña de JELAF y eliminar los valores por defecto del código.
- El token de acceso de WhatsApp (temporal o permanente) no debe subirse al repositorio ni
  registrarse en documentación; su lugar es exclusivamente `.env`. Actualmente ese `.env` está en
  el repositorio público (ver arriba) y el `.gitignore` vigente no lo excluye.
