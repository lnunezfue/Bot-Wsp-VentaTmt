---
name: paneles-y-datos-personales
description: Usar al modificar paneles/ (db, esquema.sql, registro, consultas, api_paneles, fidelizacion), paneles_web/index.html, o cualquier código que maneje DNI, nombres o teléfonos de pasajeros. Aplica las reglas de @seguro, esquema idempotente y protección de datos (Ley 29733, Perú).
---

# Paneles y datos personales

## Reglas de arquitectura de paneles
- **Escritura solo en `paneles/registro.py` con `@seguro`**: un error en paneles jamás interrumpe
  una venta. Nuevas funciones de registro siguen el mismo patrón.
- **Lectura en `consultas.py`**; el router (`api_paneles.py`) no arma SQL.
- **DDL solo en `esquema.sql`**, idempotente (`CREATE ... IF NOT EXISTS`). Columnas nuevas:
  `ALTER TABLE` explícito y tolerante a que ya exista (se ejecuta en cada arranque).
- SQL siempre con parámetros `?`. Nombres de columna/orden dinámicos solo desde una lista permitida.
- Consultas del panel con límite/paginación; nada de `SELECT *` sin tope sobre tablas que crecen.
- `seed_demo --reset` vacía la base: nunca ejecutarlo sin permiso ni con `PANELES_DB` apuntando a
  la base real.

## Acceso al panel
- `PANELES_TOKEN` obligatorio: si falta, el panel y la API **deben negar** (hoy quedan abiertos;
  corregirlo o reportarlo, no empeorarlo). Comparación con `hmac.compare_digest`.
- El token no viaja en la URL (queda en historial y logs): header `Authorization`.
- CORS del router con orígenes explícitos.

## Frontend del panel (`paneles_web/index.html`)
- Pintar datos con `textContent` o escapando; nunca `innerHTML` con nombres, notas o mensajes
  de clientes (XSS almacenado).
- Mostrar DNI y teléfono enmascarados por defecto.

## Datos personales (Ley N.° 29733)
- **Minimizar:** guardar en paneles solo lo que el tablero necesita. ¿Hace falta el DNI completo
  para un KPI? Normalmente no: usar un hash con sal (`hmac` con secreto) para contar clientes
  únicos, o los últimos dígitos.
- **Logs y prints:** DNI `*****678`, teléfono `+51 9** *** 321`, nombre solo inicial si hace falta.
  Usar un helper único de enmascarado, no hacerlo a mano en cada sitio.
- **Base SQLite:** `*.db` nunca al repo (repo público); permisos de archivo restringidos en el
  servidor; copias de seguridad también protegidas.
- **Retención:** proponer tiempo de conservación y una función de anonimizado/borrado.
- **Fidelización:** solo a clientes con consentimiento; permitir baja.
- **Datos de prueba:** solo sintéticos (`seed_demo`); nunca copiar datos reales a local ni a docs.

## Verificación
`TestClient` sobre `paneles.api_paneles.router` con `PANELES_DB` en un archivo temporal:
- Sin token o token incorrecto → 401/403; sin `PANELES_TOKEN` definido → también negado.
- Una función de `registro.py` que lanza excepción no la propaga (`@seguro`).
- `init_db()` dos veces seguidas no falla (esquema idempotente).
- Las respuestas del panel no contienen DNI/teléfono completos.
