# Configuración de Meta — WhatsApp Cloud API

> **App:** VENTA_MOQUEGUA_TURISMO
> **Business Manager:** el de Transportes Moquegua Turismo
> **Última actualización:** 2026-08-14

⚠️ **Este documento no debe usarse para guardar tokens, contraseñas ni credenciales reales.** Los valores sensibles (token de acceso, App Secret, credenciales de JELAF) van en variables de entorno (`.env`, excluido por `.gitignore`), nunca en este archivo ni en el código.

---

## 1. Resumen del objetivo

Que un cliente pueda escribirle al WhatsApp de la empresa, y el bot le responda con un botón que abre el mini-webapp de compra de boletos ya construido (búsqueda de viaje → selección de asiento → datos del pasajero → términos y condiciones → pasarela de pago → boleto), sin necesidad de que el usuario salga de WhatsApp (el link abre en el navegador integrado de la app).

---

## 2. Aplicación en Meta for Developers

- **Nombre de la app:** `VENTA_MOQUEGUA_TURISMO`
- **App ID:** `875077705659589`
- **Consola:** [developers.facebook.com/apps](https://developers.facebook.com/apps)

### Cómo se agregó el producto WhatsApp

En la interfaz actual de Meta, esto ya no se llama "Agregar producto" sino **Casos de uso**:

1. Panel de la app → sección **"Casos de uso en esta app"**.
2. Clic en **"Conectarte con los clientes a través de WhatsApp"** → botón **"Personalizar"**.
3. Se eligió el tipo de integración **"Integrar con la API"** (opción para negocios con desarrolladores propios, en vez de "Convertirte en socio").
4. Al confirmar, se agregó el ítem **WhatsApp** al menú lateral izquierdo, con submenús "Introducción" / "Paso 1: Pruébalo" / "Paso 2: Configuración de producción" / "Paso 3: Verificación del negocio".

---

## 3. Número de prueba (usado durante el desarrollo)

Ubicación en el panel: **WhatsApp → Configuración básica → Paso 1. Pruébalo**.

| Dato | Valor |
|---|---|
| Número de prueba | +1 (555) 638-7804 |
| Phone Number ID | `1180929478439391` |
| WABA ID (WhatsApp Business Account) | `1663638782432991` |
| Token de acceso | Temporal (24h) — se regenera desde el mismo panel, botón "Generar token" |

**Al generar el token**, se seleccionó el alcance:
- ✅ *"Activar solo las cuentas de WhatsApp actuales"* → cuenta marcada: **Test WhatsApp Business Account** (`...432991`).
- ❌ No se marcó *"Empresa de Transportes Moquegua Turismo"* (`...948998`), que corresponde a la WABA real/producción — se dejará para cuando se configure el número definitivo.

### Destinatarios de prueba autorizados

Mientras la app esté en modo desarrollo, **solo pueden recibir mensajes del número de prueba hasta 5 números** que se agreguen manualmente en el selector "Destinatario" del Paso 1 (cada uno recibe un código SMS de confirmación). Un usuario público cualquiera no puede usar el bot hasta pasar al número real y completar la verificación del negocio (ver sección 6).

---

## 4. Número real (producción) — pendiente

Estado: **bloqueado**, no se tiene acceso físico al número de teléfono definitivo que usará la empresa.

Pasos ya definidos para cuando se tenga acceso (en **Paso 2: Configuración de producción → "Registra tu número de teléfono de WhatsApp"**):

1. Completar perfil de negocio (nombre visible, categoría, descripción, dirección).
2. Ingresar el número real. Si el número ya está activo en la app normal de WhatsApp, hay que migrarlo desde ahí (no puede estar en ambos lados a la vez).
3. Verificar por SMS o llamada (código de 6 dígitos).
4. Configurar el PIN de verificación en dos pasos (6 dígitos) — guardarlo en un gestor de contraseñas, no en texto plano.
5. Anotar el **nuevo Phone Number ID** que Meta asigna a ese número (será distinto al de prueba) y actualizarlo en la configuración del backend.

La URL del webhook y el verify token **no cambian** al pasar al número real — solo cambia el Phone Number ID.

---

## 5. Webhook

### Verify token acordado

```
MoqueguaBot2026
```

Debe coincidir exactamente entre lo que se configura en el panel de Meta y lo que valida el backend en el `GET /webhook`.

### Dónde se configura en Meta

**WhatsApp → Paso 2: Configuración de producción → "Configurar webhooks"**:
- **URL de devolución de llamada:** `<URL pública de Ngrok>/webhook`
- **Verify token:** `MoqueguaBot2026`
- **Campo suscrito:** `messages` (obligatorio; es el que notifica mensajes entrantes)

### Estado del backend (`api_ventas.py`)

> **Actualización 2026-08-14:** esto ya se implementó (commit `Payment-demo`). Lo que sigue abajo
> describía el diseño *antes* de implementarse; se deja como referencia de la decisión original,
> pero el estado real es el que dice el checklist.

- ✅ **Implementado.** Endpoints reales en `api_ventas.py` (ver
  [`backend-api.md`](backend-api.md#whatsapp-cloud-api-webhook) para el detalle):
  - `GET /api/v1/webhook` — valida `hub.verify_token` contra `META_VERIFY_TOKEN` y responde
    `hub.challenge`.
  - `POST /api/v1/webhook` — recibe el mensaje, si el texto contiene la palabra `"comprar"`
    dispara `enviar_boton_compra()` en segundo plano, que manda el botón CTA hacia
    `buscar viaje/1-2-corregir.html`, y responde `200` de inmediato.
- ❌ **Nota**: la ruta real quedó bajo `/api/v1/webhook`, no `/webhook` a secas como decía el diseño
  original — si se reconfigura el webhook en el panel de Meta, la URL de devolución de llamada debe
  incluir el prefijo `/api/v1`.
- ❌ Sigue **pendiente** lo que ya se preveía antes de producción: deduplicar por `message.id`
  (Meta reintenta si la respuesta tarda) y validar la firma `X-Hub-Signature-256` con el App
  Secret. Tampoco hay más lógica de intención que buscar la palabra `"comprar"` en el texto.
- ❌ La URL del frontend a la que apunta el botón CTA está **hardcodeada** en
  `enviar_boton_compra()` (`https://transportesmoquegua.com/beta/...`, con un comentario en el
  código marcado `⚠️ MUY IMPORTANTE` para no olvidar cambiarla antes de ir a producción).

---

## 6. Pendiente para salir de pruebas (no configurado aún)

- **Verificación del negocio** en Business Manager (subir RUC y documentos legales) — sin esto, el límite de 5 destinatarios de prueba es permanente.
- **Token permanente**: reemplazar el token temporal (24h) por uno de System User (Configuración del negocio → Usuarios del sistema → generar token con permiso `whatsapp_business_messaging`).
- **Plantillas de mensaje aprobadas**, si se necesitan notificaciones fuera de la ventana de 24h (ej. recordatorio de viaje).
- **Nombre visible y foto de perfil** del WhatsApp Business — pasan por revisión de Meta.
- **URL de webhook estable**: Ngrok gratuito cambia de dominio en cada reinicio; para producción real se necesita Ngrok con dominio fijo (de pago) o migrar el backend a un hosting con dominio propio.

---

## 7. Notas de seguridad relacionadas

> **Actualización 2026-08-14 — esto ya pasó, no es un "pendiente" hipotético:** el `.env` con el
> token real de WhatsApp, las credenciales de JELAF y las de Yupy quedó **commiteado** en el commit
> `Payment-demo`, y el repo es **público** en GitHub. El detalle completo, con qué hacer al
> respecto, está en [`known-issues-and-security.md`](known-issues-and-security.md) — léase antes
> de seguir usando cualquiera de estas credenciales.

- El archivo `api_ventas.py` tiene usuario y contraseña de JELAF (`JELAF_USUARIO`, `JELAF_PASSWORD`) como *default* en texto plano si `.env` no está presente — y además el `.env` real está commiteado (ver arriba). Pendiente: rotar la contraseña de JELAF y quitar los defaults hardcodeados del código.
- El token de acceso de WhatsApp (temporal o permanente) nunca debe subirse al repo ni escribirse en documentación — está pensado para vivir solo en `.env`, pero **hoy ese `.env` está en el repo público** (ver arriba). El `.gitignore` actual no lo excluye.
