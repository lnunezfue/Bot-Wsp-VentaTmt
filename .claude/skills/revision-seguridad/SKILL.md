---
name: revision-seguridad
description: Usar SIEMPRE antes de dar un cambio por terminado, antes de commitear o cuando el usuario pida una revisión de seguridad del bot TMT. Revisa el diff contra los riesgos propios de venta real en JELAF, cobro con Yupy, webhook de Meta, paneles y repo público.
---

# Revisión de seguridad (TMT)

## Paso 1 — Alcance
Ejecutar `git status`, `git diff` y `git diff --staged`. Listar archivos tocados y qué flujo
afectan (venta v2, v1, Yupy, webhook, worker, paneles, frontend). Leer en `docs/AUDITORIA.md` y
`docs/REVISION_*.md` los hallazgos relacionados.

## Paso 2 — Checklist (marcar OK / N.A. / HALLAZGO)

**Repo público y secretos**
- [ ] El diff no incluye `.env`, `*.db`, `descubrimiento_output/`, perfiles de Chrome, capturas,
      trazas de Playwright, PDFs de boletos ni datos reales.
- [ ] Ningún `os.getenv("X", "<secreto>")` nuevo (S6). Variables nuevas agregadas a `.env.example`.
- [ ] Sin tokens de Meta/Yupy/JELAF en `print`, logs o mensajes de error.

**Venta y pago**
- [ ] Precio y monto salen del servidor, no del cuerpo de la petición ni de `localStorage` (V1/V2).
- [ ] `/confirmar` verifica el pago con Yupy servidor a servidor y compara monto y moneda.
- [ ] `/confirmar` es idempotente: repetir la llamada no emite otro boleto.
- [ ] No se añadió lógica a endpoints v1 ni se ampliaron sus permisos.

**Identidad y autorización**
- [ ] El recurso (reserva, PDF, datos del pasajero) se valida contra el cliente del enlace firmado,
      no solo por `booking_id`.
- [ ] Firmas comparadas con `hmac.compare_digest`; enlaces con expiración.
- [ ] Endpoints del panel exigen `PANELES_TOKEN`; si falta la variable, se niega (fallar cerrado).

**Worker / concurrencia**
- [ ] Toda operación sobre el worker dentro de `async with w.lock:` con `es_dueno()` re-verificado
      dentro (C2).
- [ ] Todos los caminos (error, timeout, cancelación, abandono) liberan el worker.

**Entrada y salida**
- [ ] Entradas validadas con Pydantic (tipo, formato, longitud).
- [ ] SQL solo con parámetros `?`; nada de f-strings en consultas.
- [ ] Frontend/panel: sin `innerHTML` con datos no escapados (XSS).
- [ ] Errores genéricos al cliente; sin trazas ni HTML de JELAF.

**Webhook de Meta**
- [ ] Firma `X-Hub-Signature-256` verificada sobre el cuerpo crudo; deduplicación por id de mensaje.

**Datos personales**
- [ ] DNI, nombres y teléfonos enmascarados en logs/prints/capturas.
- [ ] No se recolectan datos nuevos sin necesidad.

**Dependencias**
- [ ] Paquetes nuevos justificados y con versión fijada; `pip-audit` si está disponible.

## Paso 3 — Verificación
Correr lo indicado en la sección *Verificación* de `CLAUDE.md` y, si existen, `gitleaks protect --staged`
y `pip-audit`. Mostrar la salida real. **Nunca** llamar servicios reales para verificar.

## Paso 4 — Reporte
```
Revisión de seguridad
- Archivos: ...
- Hallazgos: [alta|media|baja] archivo:línea — problema — corrección (o código Nx propuesto)
- Hallazgos de la auditoría afectados: V1 (resuelto), C2 (sin cambio)...
- Verificaciones: comando → resultado
```
Un hallazgo **alto** bloquea el "terminado" hasta corregirlo o que el usuario lo acepte por escrito.
Si se descubre un problema nuevo fuera del alcance, registrarlo como `N<n>` en el doc de revisión
en vez de arreglarlo sin avisar.
