# Bot-Wsp-VentaTmt (Vibe Transit)

Bot de ventas de pasajes por WhatsApp para **Transportes Moquegua Turismo**. Un cliente le escribe
al WhatsApp de la empresa, el bot le responde con un botón que abre una mini-webapp de compra
(construida como páginas HTML sueltas con Tailwind), y esa mini-webapp compra el pasaje de verdad
contra el sistema real de la empresa (**JELAF**) automatizando su interfaz web con Playwright.

## Qué hace el sistema

`Cliente en WhatsApp → botón CTA → mini-webapp (buscar viaje → elegir asiento → datos del pasajero
→ T&C → pagar con Yupy) → backend FastAPI → robot Playwright compra el boleto real en JELAF →
boleto en PDF`.

## Estructura del proyecto

```
CHATBOT/
├── api_ventas.py            # Backend único: FastAPI + Playwright (scraping/RPA de JELAF) +
│                             # integración Yupy (pagos) + webhook de WhatsApp Cloud API
├── prueba.py                 # Script suelto para probar el envío de una plantilla de WhatsApp
├── planos_buses/*.json       # Planos de asientos por plantilla de bus + mapeo placa → plantilla
├── docs/                     # Toda la documentación del proyecto (estás en el índice, README.md)
├── boletos_generados/        # Carpeta legacy, ya no se usa (ver docs/known-issues-and-security.md)
├── buscar viaje/              ┐
├── seleccionar asiento/       │  Mini-webapp: una carpeta por paso del flujo de compra.
├── rellenar datos/            │  Cada carpeta tiene el HTML "vivo" + capturas/zips del diseño
├── pasarela de pago/          │  original (ver docs/frontend.md para saber cuál HTML es el real).
└── preview-planos/           ┘  Herramienta interna para revisar visualmente los planos de bus.
```

## Documentación

| Documento | Para qué sirve |
|---|---|
| [`docs/known-issues-and-security.md`](docs/known-issues-and-security.md) | **Leer primero.** Credenciales expuestas, bugs conocidos, deuda técnica. |
| [`docs/architecture.md`](docs/architecture.md) | Cómo encajan las piezas: mini-webapp, backend, JELAF, Yupy, Meta. |
| [`docs/frontend.md`](docs/frontend.md) | Las 7 pantallas del flujo real, en orden, con qué hace cada una. |
| [`docs/backend-api.md`](docs/backend-api.md) | Referencia de cada endpoint de `api_ventas.py`. |
| [`docs/data-planos-buses.md`](docs/data-planos-buses.md) | Formato de los JSON de planos de asientos. |
| [`docs/meta-whatsapp-config.md`](docs/meta-whatsapp-config.md) | Configuración de la app de Meta / WhatsApp Cloud API. |
| [`docs/setup-local.md`](docs/setup-local.md) | Cómo levantar el backend + frontend en tu máquina. |

## Quick start

Ver [`docs/setup-local.md`](docs/setup-local.md) para el detalle. Resumen:

```bash
pip install fastapi uvicorn requests pydantic reportlab playwright python-dotenv
python api_ventas.py            # backend en :8000
python -m http.server 5500      # frontend estático en :5500, en otra terminal
```

Abrir `http://localhost:5500/buscar%20viaje/1-2-corregir.html`.

## Stack

- **Frontend:** HTML + Tailwind CSS (vía CDN) + JS vanilla, sin build step. Estado del pedido en
  `localStorage` (clave `vibeTransitBooking`), navegación por `window.location.href` entre archivos.
- **Backend:** FastAPI (Python), un solo archivo (`api_ventas.py`).
- **"Integración" con JELAF:** no hay una API pública documentada — se automatiza su interfaz web
  real con **Playwright** (login, búsqueda, bloqueo/liberación de asiento, y una automatización RPA
  completa que hace clic por clic la venta como lo haría un operador humano).
- **Pagos:** **Yupy** (pasarela peruana), en modo sandbox.
- **Mensajería:** **WhatsApp Cloud API** (Meta), webhook implementado en el propio backend.
- **PDF del boleto:** ReportLab, generado en memoria (no se guarda en disco).
