# Bot-Wsp-VentaTmt · Venta SIN NAVEGADOR (Vibe Transit)

> **Esta carpeta es `CHATBOT PANELES` con la venta hecha por HTTP** contra JELAF
> (las mismas llamadas que usa la boletería), en vez de un Chrome haciendo
> clics. Atiende **varias compras a la vez con varias cuentas de JELAF**
> (`JELAF_USUARIO_1…`, `VENTAS_POR_CUENTA` por cuenta, 1 por defecto). No abre
> navegador.
> **Empezar por [`docs/venta-sin-navegador.md`](docs/venta-sin-navegador.md)**:
> qué cambió, el modo simulación (por defecto) y qué validar antes de vender de verdad.
>
> Para ver los paneles sin levantar JELAF ni Playwright:
> `python -m paneles.seed_demo --reset` y luego `python servidor_paneles.py`
> → <http://localhost:8010/paneles/>


Bot de ventas de pasajes por WhatsApp para Transportes Moquegua Turismo. Un cliente escribe al
WhatsApp de la empresa, el bot responde con un botón que abre una mini-webapp de compra (páginas
HTML construidas con Tailwind), y esa mini-webapp compra el pasaje contra el sistema real de la
empresa (JELAF), automatizando su interfaz web con Playwright.

## Resumen del sistema

Cliente en WhatsApp → botón de compra → mini-webapp (buscar viaje → elegir asiento → datos del
pasajero → términos y condiciones → pago con Yupy) → backend FastAPI → un worker Playwright
persistente, reservado en exclusiva para esa venta desde el primer asiento tocado, compra el
boleto real en JELAF → boleto en PDF.

## Estructura del proyecto

```
CHATBOT SIN NAVEGADOR/
├── api_ventas.py            Backend: FastAPI + /api/v2/venta/... + Yupy + webhook de WhatsApp
├── jelaf_http.py            Todo el HTTP con JELAF (login con navegador solo para cookies/Token)
├── venta_jelaf.py           Cuerpos de bloqueo, pasajero y venta (iguales a la captura real)
├── vendedor_http.py         Ventas simultáneas por HTTP (VentaHttp / VendedorHttp)
├── prueba_venta_http.py     Prueba en vivo de SOLO LECTURA contra JELAF
├── descubrir_endpoints_venta.py  Captura pasiva de una venta de boletería (sin datos personales)
├── prueba.py                 Script de prueba para el envio de una plantilla de WhatsApp
├── planos_buses/*.json       Planos de asientos por plantilla de bus y mapeo placa -> plantilla
├── docs/                     Documentacion del proyecto (indice: este README)
├── boletos_generados/        Carpeta legacy, sin uso actual (ver known-issues-and-security.md)
├── frontend/                 Mini-webapp: LO ÚNICO que se sube al hosting (7 páginas + config.js)
├── diseno_referencia/        Capturas, .zip y borradores del diseño original (no se sube)
└── preview-planos/           Herramienta interna para revisar visualmente los planos de bus.
```

## Documentación

| Documento | Contenido |
|---|---|
| [`docs/known-issues-and-security.md`](docs/known-issues-and-security.md) | Lectura obligatoria previa. Credenciales expuestas, bugs conocidos, deuda técnica. |
| [`docs/architecture.md`](docs/architecture.md) | Cómo encajan las piezas: mini-webapp, backend, pool de workers JELAF, Yupy, Meta. |
| [`docs/prototipo-pool-sesiones.md`](docs/prototipo-pool-sesiones.md) | Por qué se reemplazó el diseño original (bloqueo HTTP + reconstrucción al final) por el pool de workers persistentes, y qué se validó en vivo. |
| [`docs/optimizacion-buscar-viaje.md`](docs/optimizacion-buscar-viaje.md) | Medición en vivo (antes/después) de `buscar_viaje()`: de ~11.8s a ~2.2-2.8s reemplazando sleeps fijos por esperas de condiciones reales. |
| [`docs/optimizacion-flujo-venta.md`](docs/optimizacion-flujo-venta.md) | Optimización de asiento, pasajeros, pago adelantado y concurrencia (pendiente de medir en vivo). |
| [`docs/frontend.md`](docs/frontend.md) | Las siete pantallas del flujo de compra, en orden, con el detalle de cada una. |
| [`docs/backend-api.md`](docs/backend-api.md) | Referencia de cada endpoint de `api_ventas.py`, incluido `/api/v2/venta/...`. |
| [`docs/data-planos-buses.md`](docs/data-planos-buses.md) | Formato de los archivos JSON de planos de asientos. |
| [`docs/meta-whatsapp-config.md`](docs/meta-whatsapp-config.md) | Configuración de la aplicación de Meta / WhatsApp Cloud API. |
| [`docs/setup-local.md`](docs/setup-local.md) | Instalación y ejecución del backend y del frontend en un entorno local. |
| [`docs/PLAN_PANELES.md`](docs/PLAN_PANELES.md) | **Los tres paneles**: requerimientos de David, base de datos, módulos ya construidos y el trabajo que falta. |

## Puesta en marcha rápida

Ver [`docs/setup-local.md`](docs/setup-local.md) para el detalle completo. Resumen:

```bash
pip install -r requirements.txt
python api_ventas.py            # backend en :8000
python tests/servidor_estatico.py   # frontend estatico sin caché en :5500, en otra terminal
```

Abrir `http://localhost:5500/buscar-viaje.html`.

Sin tocar JELAF, Yupy ni Meta:

```bash
pip install -r requirements-dev.txt
python -m pytest                        # suite de tests con dobles
python tests/servidor_con_dobles.py     # backend con servicios falsos, para recorrer la webapp
```

Antes de arrancar el backend real, revisar las variables obligatorias en
[`docs/setup-local.md`](docs/setup-local.md): el backend ya no tiene credenciales por defecto.

## Stack tecnológico

- **Frontend:** HTML, Tailwind CSS (vía CDN) y JavaScript sin framework ni proceso de build.
  Estado del pedido almacenado en `sessionStorage` (clave `vibeTransitBooking`, uno por pestaña); navegación entre
  pasos mediante `window.location.href`.
- **Backend:** FastAPI (Python) en `api_ventas.py`; la venta vive en `vendedor_http.py`.
- **Integración con JELAF:** no hay API pública documentada; el bot usa por HTTP las mismas llamadas
  internas que la pantalla de boletería (descubiertas por captura, ver
  [`docs/descubrimiento-venta-http.md`](docs/descubrimiento-venta-http.md)). Playwright solo inicia
  sesión. Varias ventas a la vez con una cuenta (ver [`docs/venta-sin-navegador.md`](docs/venta-sin-navegador.md)).
- **Pagos:** Yupy (pasarela peruana), en modo sandbox.
- **Mensajería:** WhatsApp Cloud API (Meta), con el webhook implementado en el propio backend.
- **PDF del boleto:** generado en memoria con ReportLab, sin persistencia en disco.
