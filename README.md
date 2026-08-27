# Bot-Wsp-VentaTmt (Vibe Transit)

Bot de ventas de pasajes por WhatsApp para Transportes Moquegua Turismo. Un cliente escribe al
WhatsApp de la empresa, el bot responde con un botón que abre una mini-webapp de compra (páginas
HTML construidas con Tailwind), y esa mini-webapp compra el pasaje contra el sistema real de la
empresa (JELAF), automatizando su interfaz web con Playwright.

## Resumen del sistema

Cliente en WhatsApp → botón de compra → mini-webapp (buscar viaje → elegir asiento → datos del
pasajero → términos y condiciones → pago con Yupy) → backend FastAPI → robot Playwright compra el
boleto real en JELAF → boleto en PDF.

## Estructura del proyecto

```
CHATBOT/
├── api_ventas.py            Backend único: FastAPI + Playwright (automatizacion de JELAF)
│                             + integracion Yupy (pagos) + webhook de WhatsApp Cloud API
├── prueba.py                 Script de prueba para el envio de una plantilla de WhatsApp
├── planos_buses/*.json       Planos de asientos por plantilla de bus y mapeo placa -> plantilla
├── docs/                     Documentacion del proyecto (indice: este README)
├── boletos_generados/        Carpeta legacy, sin uso actual (ver known-issues-and-security.md)
├── buscar viaje/              |
├── seleccionar asiento/       |  Mini-webapp: una carpeta por paso del flujo de compra.
├── rellenar datos/            |  Cada carpeta contiene el HTML activo junto con capturas y
├── pasarela de pago/          |  archivos del diseño original (ver docs/frontend.md para
└── preview-planos/            |  identificar cual HTML corresponde al flujo real).
                                   Herramienta interna para revisar visualmente los planos de bus.
```

## Documentación

| Documento | Contenido |
|---|---|
| [`docs/known-issues-and-security.md`](docs/known-issues-and-security.md) | Lectura obligatoria previa. Credenciales expuestas, bugs conocidos, deuda técnica. |
| [`docs/architecture.md`](docs/architecture.md) | Cómo encajan las piezas: mini-webapp, backend, JELAF, Yupy, Meta. |
| [`docs/frontend.md`](docs/frontend.md) | Las siete pantallas del flujo de compra, en orden, con el detalle de cada una. |
| [`docs/backend-api.md`](docs/backend-api.md) | Referencia de cada endpoint de `api_ventas.py`. |
| [`docs/data-planos-buses.md`](docs/data-planos-buses.md) | Formato de los archivos JSON de planos de asientos. |
| [`docs/meta-whatsapp-config.md`](docs/meta-whatsapp-config.md) | Configuración de la aplicación de Meta / WhatsApp Cloud API. |
| [`docs/setup-local.md`](docs/setup-local.md) | Instalación y ejecución del backend y del frontend en un entorno local. |

## Puesta en marcha rápida

Ver [`docs/setup-local.md`](docs/setup-local.md) para el detalle completo. Resumen:

```bash
pip install -r requirements.txt
python api_ventas.py            # backend en :8000
python -m http.server 5500      # frontend estatico en :5500, en otra terminal
```

Abrir `http://localhost:5500/buscar%20viaje/1-2-corregir.html`.

## Stack tecnológico

- **Frontend:** HTML, Tailwind CSS (vía CDN) y JavaScript sin framework ni proceso de build.
  Estado del pedido almacenado en `localStorage` (clave `vibeTransitBooking`); navegación entre
  pasos mediante `window.location.href`.
- **Backend:** FastAPI (Python), en un único archivo (`api_ventas.py`).
- **Integración con JELAF:** no existe una API pública documentada; se automatiza su interfaz web
  real con Playwright (inicio de sesión, búsqueda, bloqueo y liberación de asiento, y una
  automatización RPA completa que reproduce la venta paso a paso como lo haría un operador).
- **Pagos:** Yupy (pasarela peruana), en modo sandbox.
- **Mensajería:** WhatsApp Cloud API (Meta), con el webhook implementado en el propio backend.
- **PDF del boleto:** generado en memoria con ReportLab, sin persistencia en disco.
