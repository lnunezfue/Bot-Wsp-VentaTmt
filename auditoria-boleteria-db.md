# Auditoría de `boleteria.db`: plan para solidificar la base

> **Para Claude Code:** esta auditoría se hizo solo sobre el archivo `boleteria.db` (snapshot del 2026-10-12), **sin acceso al código de la aplicación**. Cada hallazgo trae una sección **"Verificar en el código"**: confírmala antes de cambiar nada. Si el código contradice algo de aquí, manda el código; ajusta la propuesta y deja anotado por qué. No ejecutes ninguna migración sin un respaldo previo (ver Fase 0).

---

## 1. Contexto

| Dato | Valor |
|---|---|
| Motor | SQLite (archivo escrito por 3.49.1), `journal_mode = WAL` |
| Tablas | 16 (más `sqlite_sequence`) |
| `PRAGMA integrity_check` | `ok` |
| `PRAGMA user_version` | `0` (no hay versionado de esquema) |
| FOREIGN KEY declaradas | 3 de 20 relaciones reales |
| Volumen actual | `evento` 133, `sesion_compra` 18, `pago` 10, `venta` 9, `boleto` 9, `incidencia` 6, `cliente` 1, el resto vacías |

El volumen es mínimo, así que **es el mejor momento para migrar**: reconstruir tablas tarda milisegundos y la conversión de datos se puede revisar a mano.

### Mapa de relaciones (reales e implícitas)

```
contacto (telefono) ◄── sesion_compra.telefono, cliente.telefono, aceptacion_tyc.telefono (1:1)
mensaje_whatsapp (id) ◄── aceptacion_tyc.mensaje_id
sesion_compra (booking_id) ◄── venta.booking_id, pago.booking_id (1:1), evento.booking_id, incidencia.booking_id
pago (booking_id) ◄── yupy_webhook.booking_id
venta (id) ◄── boleto.venta_id [FK, CASCADE], atencion.venta_id [FK], codigo_enviado.venta_id
incidencia (id) ◄── atencion.incidencia_id [FK]
cliente (documento) ◄── boleto.documento, codigo_enviado.documento, codigo_descuento.documento_exclusivo
codigo_descuento (codigo) ◄── venta.codigo_descuento, codigo_enviado.codigo
regla_bono (id) ◄── codigo_descuento.regla_id
lote_bono (id) ◄── codigo_descuento.lote_id
```

En los datos actuales **no hay huérfanos**: todo `booking_id`, `documento`, etc. existe en su tabla padre.

---

## 2. Resumen de hallazgos

| ID | Hallazgo | Severidad | Esfuerzo |
|---|---|---|---|
| H1 | `venta.sale_en` y `sesion_compra.hora_viaje` no están en ISO-8601 (`10:50PM`); la zona horaria no está definida | **Alta** | Bajo |
| H2 | Dinero en `REAL` (punto flotante) y con unidades mezcladas (soles vs céntimos) | **Alta** | Medio |
| H3 | Integridad referencial: 17 de 20 relaciones sin FOREIGN KEY; hay que confirmar si `PRAGMA foreign_keys` está activo | **Alta** | Medio |
| H4 | Estados y enumeraciones sin `CHECK`; los datos ya se desviaron de lo documentado | **Media-Alta** | Bajo |
| H5 | `venta.booking_id` sin índice ni `UNIQUE`; faltan otros índices en columnas de cruce | **Media** | Bajo |
| H6 | Unicidad faltante en `boleto` (asiento por venta) y en `nro_boleto` | **Media** | Bajo |
| H7 | `incidencia` tiene dos fuentes de verdad (`resuelta` y `estado`) | **Media** | Bajo |
| H8 | Incidencias duplicadas idénticas (sin deduplicación) | **Media** | Bajo |
| H9 | `telefono` vacío en todas las ventas y sesiones; `contacto` vacío | **Media** (trazabilidad) | Por definir |
| H10 | Esquema sin versionado; columnas agregadas con `ALTER` dentro de `db.init_db()` | **Media** | Medio |
| H11 | Tablas no `STRICT`: SQLite acepta cualquier tipo en cualquier columna | **Media-Baja** | Incluido en la reconstrucción |
| H12 | Ventas simuladas mezcladas con ventas reales | **Baja-Media** | Bajo |
| H13 | Agregados de `cliente` (`total_viajes`, `total_gastado`, `segmento`) se pueden desfasar | **Baja** | Bajo |
| H14 | `pago`: historial de intentos en JSON, nombre de columna engañoso, `proveedor NULL = yupy` implícito | **Baja-Media** | Medio |
| H15 | Columnas JSON sin validar | **Baja** | Bajo |

**Lo que ya está bien y hay que conservar:** las claves de deduplicación (`mensaje_whatsapp.id`, `yupy_webhook.event_id`, `pago.yupy_transaction_id UNIQUE`, `ux_env_codigo_doc`, el índice único parcial `ux_bono_regla_hito`); los índices pensados para consultas reales; los `CHECK` de `regla_bono`; la copia de viaje y montos dentro de `venta` (el historial no cambia si cambian las tarifas); y que `pago.autorizacion` solo guarde la marca y los últimos 4 dígitos de la tarjeta.

---

## 3. Hallazgos en detalle

### H1. Fechas y horas fuera de formato — Alta

**Evidencia**
- `venta.sale_en` está documentado como ISO-8601, pero contiene `2026-10-11T10:50PM` y `2026-10-11T10:30PM`.
- `sesion_compra.hora_viaje` está documentado como `HH:MM`, pero contiene `10:50PM`.
- Ninguna marca de tiempo (`comprado_en`, `abierta_en`, `ocurrido_en`…) indica su zona horaria. Todas parecen hora local de Perú, pero no está escrito en ningún lado.

**Riesgo**
- Ordenar o filtrar por `sale_en` como texto da resultados incorrectos: `"…T10:50PM"` queda antes que `"…T11:00AM"`. El índice `ix_venta_salida` ordena mal.
- `datetime(sale_en)` devuelve `NULL`, así que cualquier cálculo del tipo "sale en menos de 2 horas" falla sin avisar.
- Si algún día se mezclan horas UTC (servidor) con horas locales (JELAF), los reportes quedan desplazados 5 horas.

**Propuesta**
1. Corregir en el código el punto donde se arma `sale_en` y `hora_viaje` (probablemente se copia el texto que devuelve JELAF). El formato objetivo es `YYYY-MM-DDTHH:MM:SS` (24 h) y `HH:MM` (24 h).
2. Convenir y documentar la zona: **hora local de Lima (America/Lima, UTC-5, sin horario de verano), sin sufijo de zona**. La alternativa es UTC con `Z`; lo importante es que sea una sola y esté escrita.
3. Convertir los datos existentes (script en la sección 5).
4. Agregar `CHECK` en la reconstrucción. Está probado en SQLite 3.45: `datetime()` sobre una columna es válido en un `CHECK` y rechaza `10:50PM`.
   ```sql
   sale_en TEXT CHECK (sale_en IS NULL OR datetime(sale_en) IS NOT NULL)
   hora_viaje TEXT CHECK (hora_viaje IS NULL OR time(hora_viaje) IS NOT NULL)
   ```
   Aplicar el mismo patrón a todas las columnas `*_en` (con `NOT NULL` donde corresponda) y a `date(...)` para `fecha_viaje` y `vence_en`.

**Verificar en el código:** dónde se parsea la respuesta de JELAF; si hay comparaciones de fechas hechas en SQL o en Python; qué hora usa el servidor (`datetime.now()` sin zona vs `datetime.now(ZoneInfo("America/Lima"))`).

---

### H2. Dinero en punto flotante y con unidades mezcladas — Alta

**Evidencia**

| Tabla.columna | Tipo | Unidad |
|---|---|---|
| `pago.monto_centimos` | INTEGER | céntimos |
| `regla_bono.valor`, `regla_bono.tope_centimos` | INTEGER | céntimos / % |
| `atencion.monto_devuelto_centimos` | INTEGER | céntimos |
| `venta.monto_bruto`, `monto_descuento`, `monto_total` | REAL | soles |
| `boleto.precio` | REAL | soles |
| `cliente.total_gastado` | REAL | soles |
| `codigo_descuento.valor`, `tope_descuento` | REAL | soles / % |

**Riesgo:** `REAL` acumula errores de redondeo (`0.1 + 0.2 ≠ 0.3`), sobre todo al sumar descuentos porcentuales. Además, conciliar `pago` contra `venta` obliga a multiplicar por 100 cada vez, y cualquier comparación que lo olvide da un falso descuadre o, peor, un falso cuadre.

**Propuesta:** todo el dinero pasa a `INTEGER` en céntimos, **renombrando la columna con sufijo `_centimos`**. Así, el código viejo que lea `monto_total` falla de inmediato en lugar de leer un valor 100 veces mayor.

| Antes | Después |
|---|---|
| `venta.monto_bruto` | `venta.monto_bruto_centimos` |
| `venta.monto_descuento` | `venta.monto_descuento_centimos` |
| `venta.monto_total` | `venta.monto_total_centimos` |
| `boleto.precio` | `boleto.precio_centimos` |
| `cliente.total_gastado` | `cliente.total_gastado_centimos` |
| `codigo_descuento.tope_descuento` | `codigo_descuento.tope_centimos` |
| `codigo_descuento.valor` (REAL) | `codigo_descuento.valor` INTEGER: % entero si `tipo='porcentaje'`, céntimos si `tipo='monto'` (igual que `regla_bono`) |

Conversión: `CAST(ROUND(x * 100) AS INTEGER)`.

`CHECK` sugeridos:
```sql
CHECK (monto_bruto_centimos >= 0 AND monto_descuento_centimos >= 0 AND monto_total_centimos >= 0)
CHECK (monto_total_centimos = monto_bruto_centimos - monto_descuento_centimos)  -- solo si es la regla de negocio
CHECK (tipo <> 'porcentaje' OR valor BETWEEN 1 AND 100)                          -- en codigo_descuento
```

**Verificar en el código:** todos los lugares que leen o escriben estas columnas (buscar `monto_`, `precio`, `total_gastado`, `tope_descuento`, `valor`); dónde se calcula el descuento (redondear una sola vez, al final, en céntimos); los formatos de salida a pantalla y a WhatsApp (dividir por 100 solo al mostrar). Confirmar si `monto_total = monto_bruto - monto_descuento` siempre se cumple; en los datos actuales sí.

> Este cambio **no es compatible hacia atrás**: el código y el esquema se despliegan juntos, con la app detenida.

---

### H3. Integridad referencial — Alta

**Evidencia:** solo existen `boleto.venta_id → venta` (CASCADE), `atencion.incidencia_id → incidencia` y `atencion.venta_id → venta`. Las otras 17 relaciones del mapa no tienen restricción. Además, en SQLite las FOREIGN KEY **solo se aplican si cada conexión ejecuta `PRAGMA foreign_keys = ON`**; el archivo no lo guarda.

**Riesgo:** se puede guardar una venta con un `codigo_descuento` inexistente, un bono con un `regla_id` inventado o un `codigo_enviado` apuntando a una venta borrada. Los reportes cruzados pierden filas sin avisar.

**Propuesta, separando tablas de negocio y tablas de registro:**

| Relación | ¿FK estricta? | Motivo |
|---|---|---|
| `venta.booking_id → sesion_compra` | Sí, si la sesión siempre existe antes de la venta | Núcleo del modelo |
| `pago.booking_id → sesion_compra` | Sí, con la misma condición | |
| `boleto.venta_id → venta` | Sí (ya existe). Cambiar `ON DELETE CASCADE` a `RESTRICT` | Una venta nunca debería borrarse; se anula con `estado='anulada'`. CASCADE borra boletos emitidos sin dejar rastro |
| `atencion.* → incidencia / venta` | Sí (ya existen) | |
| `venta.codigo_descuento → codigo_descuento` | Sí | |
| `codigo_enviado.codigo → codigo_descuento` | Sí | |
| `codigo_enviado.venta_id → venta` | Sí | |
| `codigo_descuento.regla_id → regla_bono`, `lote_id → lote_bono` | Sí | |
| `codigo_descuento.documento_exclusivo`, `codigo_enviado.documento`, `boleto.documento → cliente` | Solo si `cliente` se crea **antes** del insert | Si `cliente` se actualiza después de la venta, la FK rompería la emisión |
| `aceptacion_tyc.mensaje_id → mensaje_whatsapp` | No; dejar implícita | La tabla de deduplicación puede purgarse |
| `evento.booking_id`, `incidencia.booking_id`, `yupy_webhook.booking_id` | **No.** Índice + chequeo periódico de huérfanos | Son registros: **escribir un log nunca debe fallar**. Un evento o incidencia puede ocurrir antes de que exista la sesión |
| `* .telefono → contacto` | No por ahora (ver H9) | `contacto` está vacía |

Activar en **cada conexión**, justo después de abrirla y fuera de cualquier transacción:
```python
conn.execute("PRAGMA foreign_keys = ON")
```

Toda columna con FK necesita índice (SQLite no lo crea solo): ver H5.

**Verificar en el código:** si existe `PRAGMA foreign_keys` en la función que abre la conexión; el orden real de inserts (¿se crea `sesion_compra` antes que `pago` y `venta`? ¿`cliente` antes que `boleto`?); si en algún lugar se hace `DELETE FROM venta`.

---

### H4. Estados y enumeraciones sin validar — Media-Alta

**Evidencia de desviación (datos reales vs documentación):**
- `incidencia.tipo` contiene `venta_sin_nombre`, que no figura en la lista documentada.
- `venta.medio_pago` contiene `NIUBIZ` y `YUPY_QR`; el comentario dice `yupy_tarjeta | yupy_qr`.
- `pago.proveedor` usa minúsculas (`niubiz`, `yupy`); `venta.medio_pago` usa mayúsculas.
- `sesion_compra.paso_maximo` no tiene lista ni `CHECK`.

**Riesgo:** un error de tipeo (`'emitda'`) se guarda sin aviso y desaparece de reportes y filtros.

**Propuesta**
1. Centralizar los valores en el código (constantes o `Enum` en un solo módulo) y usar esas constantes en todas las escrituras.
2. Agregar `CHECK (col IN (...))` en la reconstrucción con la lista **real**. Valores observados más los documentados:

| Columna | Valores |
|---|---|
| `venta.estado` | `emitida, cobrada_sin_emitir, por_conciliar, anulada, simulada` |
| `venta.medio_pago` | normalizar a minúsculas: `niubiz, yupy_qr, yupy_tarjeta` (confirmar lista completa) |
| `sesion_compra.estado` | `en_curso, abandonada, emitida, fallida, rechazada` |
| `sesion_compra.paso_maximo` y `evento.paso` | `mensaje_recibido, enlace_enviado, enlace_abierto, viaje_elegido, asiento_tocado, asiento_liberado, pasajero_lleno, pago_iniciado, pago_confirmado, boleto_emitido, abandono` |
| `incidencia.tipo` | `cobrado_sin_emitir, fallo_emision, asiento_no_disponible, pool_ocupado, pago_rechazado, sesion_liberada, jelaf_caido, error_interno, venta_sin_nombre` (+ los que aparezcan en el código) |
| `incidencia.severidad` | `critica, grave, leve` |
| `incidencia.estado` | sacar los valores del código |
| `atencion.accion` | `contacto, ofrecio_asiento, compro_otro, desistio, emitio_manual, devolucion, derivar, sin_accion, otra, nota` |
| `atencion.resultado` | `en_seguimiento, resuelta, derivada` |
| `atencion.canal_contacto` | `whatsapp, llamada, presencial` |
| `pago.estado` | `pendiente, usado` |
| `pago.proveedor` | `yupy, niubiz` (NOT NULL después de rellenar, ver H14) |
| `pago.moneda` | `PEN` |
| `cliente.segmento` | `nuevo, ocasional, frecuente, dormido` |
| `cliente.tipo_documento`, `boleto.tipo_documento` | `01, 02, 03, 04` |
| `codigo_descuento.tipo` | `porcentaje, monto` |
| `codigo_descuento.estado` | `borrador, activo, pausado, vencido` |
| `codigo_descuento.segmento_objetivo` | `NULL` o los mismos valores que `cliente.segmento` |
| `codigo_enviado.estado_envio` | `enviado, fallido, pendiente` |
| `yupy_webhook.result` | `NULL, pagado, cancelado, timeout` |
| Booleanos (`incidencia.resuelta`, etc.) | `CHECK (col IN (0,1))` |

> Para tablas de registro (`evento`, `incidencia`), evaluar si conviene que un `CHECK` haga fallar la escritura de un log. Una alternativa razonable: `CHECK` en `incidencia.severidad` y `estado` (operativos), y solo validación en el código para `evento.paso` e `incidencia.tipo`, que crecen con el tiempo.

**Verificar en el código:** buscar todos los literales que se escriben en estas columnas (`grep -rn "estado\s*=\|'emitida'\|'venta_sin_nombre'"` o similar) antes de fijar las listas.

---

### H5. Índices y unicidad en columnas de cruce — Media

**Evidencia:** `venta.booking_id` es la columna con la que se cruzan venta, pago y sesión, y no tiene índice ni `UNIQUE`. Tampoco tienen índice `incidencia.booking_id`, `atencion.venta_id`, `codigo_enviado.venta_id`, `codigo_descuento.regla_id` (está dentro de un índice parcial que no sirve para joins generales) ni `venta.codigo_descuento`. Hoy no se nota por el volumen; con miles de filas, cada cruce recorre la tabla completa.

**Propuesta**
```sql
CREATE UNIQUE INDEX ux_venta_booking ON venta(booking_id);   -- si 1 booking = 1 venta (ver abajo)
CREATE INDEX ix_inc_booking        ON incidencia(booking_id);
CREATE INDEX ix_atencion_venta     ON atencion(venta_id);
CREATE INDEX ix_env_venta          ON codigo_enviado(venta_id);
CREATE INDEX ix_venta_codigo       ON venta(codigo_descuento);
CREATE INDEX ix_codigo_regla       ON codigo_descuento(regla_id);
CREATE INDEX ix_yupy_webhook_booking ON yupy_webhook(booking_id);
```
`ix_atencion_incidencia` ya existe (`atencion(incidencia_id, creado_en)`), y `ix_boleto_venta` también.

**Verificar en el código:** ¿una sesión (`booking_id`) puede producir más de una fila en `venta`, por ejemplo al reemitir tras un fallo? Si puede, usar índice normal en lugar de `UNIQUE`, o `UNIQUE` parcial `WHERE estado <> 'anulada'`. Revisar también las consultas `SELECT ... WHERE telefono = ?` sobre `venta` (no hay índice en `venta.telefono`).

---

### H6. Unicidad en `boleto` — Media

**Evidencia:** nada impide dos boletos con el mismo asiento en una venta. `nro_boleto` no es único; hoy se repite `SIMULADO-11` en dos ventas simuladas.

**Propuesta**
```sql
CREATE UNIQUE INDEX ux_boleto_asiento ON boleto(venta_id, asiento);
CREATE UNIQUE INDEX ux_boleto_nro     ON boleto(nro_boleto) WHERE nro_boleto IS NOT NULL AND nro_boleto NOT LIKE 'SIMULADO-%';
CREATE UNIQUE INDEX ux_venta_nro      ON venta(nro_boleto)  WHERE nro_boleto IS NOT NULL AND estado <> 'simulada';
```
Agregar `CHECK (precio_centimos >= 0)` y `asiento <> ''`.

**Verificar en el código:** ¿en una venta de varios asientos, `venta.nro_boleto` guarda uno solo y `boleto.nro_boleto` uno por asiento? Ajustar los índices según eso.

---

### H7. Dos fuentes de verdad en `incidencia` — Media

**Evidencia:** `resuelta` (0/1) y `estado` (`abierta`, …) indican lo mismo. Además hay dos índices casi iguales (`ix_inc_abierta` sobre `resuelta` e `ix_inc_estado` sobre `estado`).

**Propuesta:** dejar `estado` como única fuente. Mientras haya código que lea `resuelta`, mantener las dos columnas sincronizadas con un `CHECK`:
```sql
CHECK ((estado = 'resuelta') = (resuelta = 1))
```
Luego migrar las lecturas a `estado`, eliminar `resuelta` y su índice en una migración posterior (deprecar → dejar de escribir → borrar). Ajustar el `CHECK` si hay más estados "cerrados" (p. ej. `descartada`).

**Verificar en el código:** dónde se escribe `resuelta = 1` y dónde se escribe `estado`; si ambos se actualizan en la misma sentencia.

---

### H8. Incidencias duplicadas — Media

**Evidencia:** las incidencias 1, 2 y 3 son idénticas (mismo `booking_id`, `tipo = pago_rechazado` y mismo mensaje: "Yupy rechazó la sesión de checkout (404)"). El bot reintenta y registra una incidencia por intento.

**Riesgo:** el panel de soporte muestra el mismo problema varias veces, y quien atiende puede contactar al cliente más de una vez.

**Propuesta:** deduplicar mientras la incidencia siga abierta, con un índice único parcial y un insert idempotente:
```sql
CREATE UNIQUE INDEX ux_inc_abierta_unica ON incidencia(booking_id, tipo) WHERE estado = 'abierta' AND booking_id IS NOT NULL;
```
```sql
INSERT INTO incidencia (...) VALUES (...)
ON CONFLICT (booking_id, tipo) WHERE estado = 'abierta' AND booking_id IS NOT NULL
DO UPDATE SET actualizada_en = excluded.ocurrido_en,
              contexto = excluded.contexto;   -- opcional: agregar columna repeticiones INTEGER y sumar 1
```
Antes de crear el índice, cerrar o fusionar los duplicados existentes (ids 2 y 3).

**Verificar en el código:** la función que registra incidencias y si hay casos donde sí se quieren varias incidencias abiertas del mismo tipo por booking.

---

### H9. Teléfono vacío y `contacto` sin uso — Media

**Evidencia:** `telefono` es `NULL` en las 9 ventas y en las 18 sesiones, y `cliente.telefono` también. `contacto` y `aceptacion_tyc` tienen 0 filas, aunque hay compras.

**Riesgo:** no hay forma de unir una compra con la conversación de WhatsApp que la originó. Soporte no puede contactar al cliente desde la base, y la aceptación de términos no queda registrada.

**Propuesta:** primero aclarar la causa. Puede ser un flujo de prueba que no pasa por WhatsApp o un bug en el que el teléfono no se propaga al abrir el enlace (por ejemplo, el `booking_id` se genera sin el teléfono en la URL). Si el flujo real siempre viene de WhatsApp:
- Propagar el teléfono al crear la sesión.
- Hacer `sesion_compra.telefono NOT NULL` (en una migración posterior, cuando los datos nuevos ya lo traigan).
- Registrar `contacto` y `aceptacion_tyc`.

**Verificar en el código:** dónde se crea `sesion_compra` y de dónde sale el teléfono; si `contacto` se escribe en algún lado; si la aceptación de términos se está guardando en otra parte.

---

### H10. Esquema sin versionado — Media

**Evidencia:** `PRAGMA user_version = 0`. Varias columnas se agregaron con `ALTER TABLE` desde `db.init_db()` (`_COLUMNAS_NUEVAS`): `boleto.nro_boleto`, `boleto.link_comprobante`, `pago.transacciones_anteriores`, `pago.proveedor`, `pago.autorizacion`, `pago.checkout_tokens`, `incidencia.estado`, `incidencia.actualizada_en` y las de `codigo_descuento`. Los comentarios del `CREATE TABLE` ya no reflejan el esquema real.

**Riesgo:** no se sabe qué versión del esquema tiene cada copia de la base (producción, respaldo, local). Un `ALTER` que falla a medias deja la base en un estado intermedio.

**Propuesta:** un sistema mínimo de migraciones basado en `user_version`:
```
migrations/
  001_baseline.sql        -- esquema actual tal cual (no cambia nada; solo fija user_version = 1)
  002_solidificar.sql     -- la reconstrucción de la sección 5
  003_...                 -- siguientes cambios, uno por concepto
```
```python
def migrar(conn):
    actual = conn.execute("PRAGMA user_version").fetchone()[0]
    for n, archivo in sorted(MIGRACIONES):
        if n > actual:
            conn.executescript(open(archivo).read())   # cada archivo termina con PRAGMA user_version = n;
```
`init_db()` deja de hacer `ALTER` sueltos y solo llama a `migrar()`. Para la reconstrucción usar un script Python en lugar de `executescript`, porque necesita `PRAGMA foreign_keys = OFF` fuera de la transacción (sección 5).

**Verificar en el código:** el contenido de `_COLUMNAS_NUEVAS` y de `init_db()`, por si agrega columnas o índices que no están en este snapshot.

---

### H11. Tablas no `STRICT` — Media-Baja

**Evidencia:** SQLite usa afinidad de tipos: una columna `INTEGER` acepta `'abc'` y una `REAL` acepta texto sin quejarse.

**Propuesta:** en la reconstrucción, declarar las tablas con `STRICT` (requiere SQLite ≥ 3.37). Los tipos permitidos son `INTEGER`, `REAL`, `TEXT`, `BLOB` y `ANY`, y todos los del esquema actual son compatibles.

**Verificar en el código / servidor:** la versión de SQLite con la que corre la app en producción: `python -c "import sqlite3; print(sqlite3.sqlite_version)"`. Si es menor a 3.37, omitir `STRICT`; el resto de la auditoría sigue aplicando.

---

### H12. Ventas simuladas mezcladas con reales — Baja-Media

**Evidencia:** 3 de 9 ventas tienen `estado = 'simulada'` (`nro_boleto = SIMULADO-*`), con pagos Niubiz reales autorizados en `pago`.

**Riesgo:** cualquier reporte de ingresos que no filtre `estado <> 'simulada'` infla los números. Hoy `cliente.total_viajes = 6` y `total_gastado = 420` sí las excluyen; hay que mantener ese cuidado en todas las consultas.

**Propuesta:** crear vistas de reporte que ya filtren, y usarlas siempre:
```sql
CREATE VIEW venta_real AS SELECT * FROM venta WHERE estado <> 'simulada';
```
Alternativa más fuerte: una base separada para el modo simulación. Documentar si los pagos de las ventas simuladas fueron reales (y si se devolvieron).

---

### H13. Agregados de `cliente` — Baja

**Evidencia:** `total_viajes`, `total_gastado`, `primera_compra`, `ultima_compra` y `segmento` se calculan a partir de `venta` y `boleto`. Hoy cuadran (6 viajes, 420 soles = 6 × 70).

**Riesgo:** si una venta se anula o se corrige y el agregado no se recalcula, la segmentación y los bonos (`regla_bono` usa viajes y gasto como disparador) se aplican mal.

**Propuesta:** una función `recalcular_cliente(documento)` que derive todo desde `venta_real` + `boleto`, llamada después de cada cambio de estado de una venta, más un chequeo de consistencia en la sección 6.

---

### H14. Modelo de pagos — Baja-Media

**Evidencia**
- `pago.yupy_transaction_id` también guarda ids de Niubiz (`NIUBIZ-<pedido>`); el nombre es histórico.
- `proveedor IS NULL` significa "yupy" de forma implícita.
- Los intentos de pago anteriores se guardan en `transacciones_anteriores` como un arreglo JSON (p. ej. `["po_0c8b…", "NIUBIZ-4338…"]`), y `checkout_tokens` es otro JSON.

**Riesgo:** ante un "cobrado sin emitir", hay que buscar dentro de JSON para saber qué intento se cobró. No hay unicidad ni índice sobre esos ids, así que un webhook de un intento anterior no se puede ubicar con una consulta simple.

**Propuesta, por etapas y sin romper nada:**
1. Rellenar `proveedor` donde es `NULL` con `'yupy'` y hacerlo `NOT NULL` con su `CHECK`.
2. Agregar `transaccion_id` como alias nuevo (copiando el valor), pasar las lecturas a ese nombre y dejar de usar `yupy_transaction_id` en una migración posterior (patrón agregar → copiar → cambiar lecturas → borrar).
3. A mediano plazo, una tabla `pago_intento (id, booking_id, proveedor, transaccion_id UNIQUE, monto_centimos, estado, creado_en)` con un registro por checkout. Así, un webhook se resuelve con `WHERE transaccion_id = ?`.

**Verificar en el código:** qué contiene `checkout_tokens` y si son credenciales reutilizables. Si lo son, no deberían quedar guardadas después de usarse.

---

### H15. JSON sin validar — Baja

**Evidencia:** las columnas `pago.asientos`, `pago.transacciones_anteriores`, `pago.autorizacion`, `sesion_compra.asientos`, `evento.detalle` (30 nulos, 103 válidos), `incidencia.contexto` y `lote_bono.filtro` son JSON en `TEXT`. Hoy todo el JSON presente es válido.

**Propuesta:** `CHECK (col IS NULL OR json_valid(col))` en las columnas de negocio (`pago.*`, `sesion_compra.asientos`). En logs (`evento.detalle`, `incidencia.contexto`), validar solo en el código para no perder registros.

---

## 4. Esquema objetivo de las tablas principales

Propuesta de referencia: **ajusta las listas de valores y los `NOT NULL` según lo que confirmes en el código.** El resto de tablas sigue el mismo patrón: `STRICT`, `CHECK` de enumeraciones, fechas validadas con `datetime()`/`date()` y dinero en céntimos.

```sql
CREATE TABLE venta (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id                TEXT NOT NULL REFERENCES sesion_compra(booking_id),
    nro_boleto                TEXT,
    telefono                  TEXT,
    origen                    TEXT NOT NULL,
    destino                   TEXT NOT NULL,
    servicio                  TEXT,
    placa_bus                 TEXT,
    comprado_en               TEXT NOT NULL CHECK (datetime(comprado_en) IS NOT NULL),
    sale_en                   TEXT CHECK (sale_en IS NULL OR datetime(sale_en) IS NOT NULL),
    cantidad_asientos         INTEGER NOT NULL DEFAULT 1 CHECK (cantidad_asientos > 0),
    monto_bruto_centimos      INTEGER NOT NULL DEFAULT 0 CHECK (monto_bruto_centimos >= 0),
    monto_descuento_centimos  INTEGER NOT NULL DEFAULT 0 CHECK (monto_descuento_centimos >= 0),
    monto_total_centimos      INTEGER NOT NULL DEFAULT 0 CHECK (monto_total_centimos >= 0),
    codigo_descuento          TEXT REFERENCES codigo_descuento(codigo),
    medio_pago                TEXT CHECK (medio_pago IS NULL OR medio_pago IN ('niubiz','yupy_qr','yupy_tarjeta')),
    referencia_pago           TEXT,
    estado                    TEXT NOT NULL DEFAULT 'emitida'
                              CHECK (estado IN ('emitida','cobrada_sin_emitir','por_conciliar','anulada','simulada')),
    CHECK (monto_total_centimos = monto_bruto_centimos - monto_descuento_centimos)
) STRICT;

CREATE TABLE boleto (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    venta_id          INTEGER NOT NULL REFERENCES venta(id) ON DELETE RESTRICT,
    documento         TEXT,                      -- FK a cliente solo si cliente se crea antes (H3)
    tipo_documento    TEXT NOT NULL DEFAULT '01' CHECK (tipo_documento IN ('01','02','03','04')),
    nombres           TEXT,
    paterno           TEXT,
    materno           TEXT,
    asiento           TEXT NOT NULL CHECK (asiento <> ''),
    precio_centimos   INTEGER NOT NULL DEFAULT 0 CHECK (precio_centimos >= 0),
    nro_boleto        TEXT,
    link_comprobante  TEXT
) STRICT;

CREATE TABLE incidencia (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id       TEXT,                       -- sin FK: es un registro (H3)
    telefono         TEXT,
    tipo             TEXT NOT NULL,              -- validar en código (H4)
    severidad        TEXT NOT NULL CHECK (severidad IN ('critica','grave','leve')),
    mensaje          TEXT NOT NULL,
    contexto         TEXT,
    ocurrido_en      TEXT NOT NULL CHECK (datetime(ocurrido_en) IS NOT NULL),
    resuelta         INTEGER NOT NULL DEFAULT 0 CHECK (resuelta IN (0,1)),   -- a deprecar (H7)
    resuelta_en      TEXT CHECK (resuelta_en IS NULL OR datetime(resuelta_en) IS NOT NULL),
    nota_resolucion  TEXT,
    estado           TEXT NOT NULL DEFAULT 'abierta',  -- + CHECK con la lista del código
    actualizada_en   TEXT,
    CHECK ((estado = 'resuelta') = (resuelta = 1))      -- ajustar si hay más estados cerrados
) STRICT;

CREATE TABLE codigo_descuento (
    codigo              TEXT PRIMARY KEY,
    descripcion         TEXT,
    tipo                TEXT NOT NULL DEFAULT 'porcentaje' CHECK (tipo IN ('porcentaje','monto')),
    valor               INTEGER NOT NULL CHECK (valor > 0),
    segmento_objetivo   TEXT CHECK (segmento_objetivo IS NULL OR segmento_objetivo IN ('nuevo','ocasional','frecuente','dormido')),
    vence_en            TEXT CHECK (vence_en IS NULL OR date(vence_en) IS NOT NULL),
    usos_por_cliente    INTEGER NOT NULL DEFAULT 1 CHECK (usos_por_cliente > 0),
    tope_centimos       INTEGER CHECK (tope_centimos IS NULL OR tope_centimos > 0),
    estado              TEXT NOT NULL DEFAULT 'borrador' CHECK (estado IN ('borrador','activo','pausado','vencido')),
    creado_en           TEXT NOT NULL CHECK (datetime(creado_en) IS NOT NULL),
    documento_exclusivo TEXT,
    regla_id            INTEGER REFERENCES regla_bono(id),
    lote_id             INTEGER REFERENCES lote_bono(id),
    hito                TEXT,
    CHECK (tipo <> 'porcentaje' OR valor BETWEEN 1 AND 100)
) STRICT;

CREATE TABLE codigo_enviado (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    codigo       TEXT NOT NULL REFERENCES codigo_descuento(codigo),
    documento    TEXT,
    telefono     TEXT,
    enviado_en   TEXT NOT NULL CHECK (datetime(enviado_en) IS NOT NULL),
    estado_envio TEXT NOT NULL DEFAULT 'enviado' CHECK (estado_envio IN ('enviado','fallido','pendiente')),
    error_envio  TEXT,
    canjeado_en  TEXT CHECK (canjeado_en IS NULL OR datetime(canjeado_en) IS NOT NULL),
    venta_id     INTEGER REFERENCES venta(id)
) STRICT;
```

Cambios puntuales en las demás tablas:
- **`sesion_compra`:** `CHECK` de `estado` y `paso_maximo`; `fecha_viaje` con `date()`; `hora_viaje` con `time()`; `asientos` con `json_valid`.
- **`pago`:** `proveedor TEXT NOT NULL CHECK (proveedor IN ('yupy','niubiz'))`; `estado CHECK IN ('pendiente','usado')`; `moneda CHECK (moneda = 'PEN')`; `asientos CHECK (json_valid(asientos))`; FK `booking_id → sesion_compra` si se confirma el orden de inserts.
- **`cliente`:** `total_gastado_centimos INTEGER`; `CHECK` de `segmento` y `tipo_documento`; `total_viajes >= 0`.
- **`atencion`:** `CHECK` de `accion`, `resultado` y `canal_contacto`.

---

## 5. Plan de migración

### Fase 0: preparación (obligatoria)

1. **Detener la app.** SQLite admite un solo escritor, y el cambio de unidades (H2) no es compatible con el código anterior.
2. **Respaldo consistente** (con WAL, copiar solo el `.db` puede dejar fuera transacciones del archivo `-wal`):
   ```bash
   sqlite3 boleteria.db "VACUUM INTO 'boleteria-backup-$(date +%Y%m%d-%H%M).db'"
   ```
3. Anotar los conteos por tabla y la suma de montos antes de migrar (sección 6).
4. Confirmar la versión de SQLite en producción (H11).
5. Ejecutar primero sobre una **copia** de la base de producción y verificar.

### Fase 1: cambios solo de código (se pueden desplegar antes)

- `PRAGMA foreign_keys = ON` en la función que abre conexiones (H3). Hasta la Fase 2 solo afecta a las 3 FK existentes.
- Corregir la generación de `sale_en` y `hora_viaje` (H1).
- Centralizar las constantes de estados (H4).
- Deduplicar el registro de incidencias en el código (H8).
- Investigar el teléfono vacío (H9).

### Fase 2: reconstrucción del esquema (`002_solidificar`)

SQLite no permite agregar FK ni `CHECK` con `ALTER TABLE`, así que hay que reconstruir cada tabla con el procedimiento oficial (https://www.sqlite.org/lang_altertable.html#otheralter):

```python
import sqlite3

def reconstruir(conn, tabla, ddl_nueva, select_conversion, indices):
    """ddl_nueva crea '<tabla>_nueva'; select_conversion produce las filas ya convertidas."""
    conn.execute(ddl_nueva)
    conn.execute(f"INSERT INTO {tabla}_nueva {select_conversion}")
    conn.execute(f"DROP TABLE {tabla}")
    conn.execute(f"ALTER TABLE {tabla}_nueva RENAME TO {tabla}")
    for sql in indices:
        conn.execute(sql)

conn = sqlite3.connect("boleteria.db", isolation_level=None)
conn.execute("PRAGMA foreign_keys = OFF")      # fuera de la transacción, obligatorio
conn.execute("BEGIN IMMEDIATE")
try:
    # ... una llamada a reconstruir() por tabla ...
    problemas = conn.execute("PRAGMA foreign_key_check").fetchall()
    if problemas:
        raise RuntimeError(f"FK rotas: {problemas}")
    conn.execute("PRAGMA user_version = 2")
    conn.execute("COMMIT")
except Exception:
    conn.execute("ROLLBACK")
    raise
finally:
    conn.execute("PRAGMA foreign_keys = ON")
assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
```

Conversiones dentro de `select_conversion`:

```sql
-- dinero
CAST(ROUND(monto_bruto * 100) AS INTEGER) AS monto_bruto_centimos

-- medio_pago normalizado
lower(medio_pago) AS medio_pago
```

```python
# sale_en '2026-10-11T10:50PM' -> '2026-10-11T22:50:00'
# Registrar como función SQL antes del INSERT: conn.create_function("iso_12h", 1, iso_12h, deterministic=True)
from datetime import datetime
def iso_12h(v):
    if v is None:
        return None
    for fmt in ("%Y-%m-%dT%I:%M%p", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(v, fmt).strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            pass
    raise ValueError(f"fecha no reconocida: {v!r}")   # mejor fallar que guardar basura

def hora_24(v):   # '10:50PM' -> '22:50'
    if v is None:
        return None
    for fmt in ("%I:%M%p", "%H:%M"):
        try:
            return datetime.strptime(v, fmt).strftime("%H:%M")
        except ValueError:
            pass
    raise ValueError(f"hora no reconocida: {v!r}")
```

Orden sugerido dentro de la transacción (con FK desactivadas el orden no es crítico, pero así se lee mejor): `regla_bono`, `lote_bono`, `cliente`, `codigo_descuento`, `sesion_compra`, `pago`, `venta`, `boleto`, `codigo_enviado`, `incidencia`, `atencion`. Las tablas sin cambios de estructura (`contacto`, `mensaje_whatsapp`, `yupy_webhook`, `evento`, `aceptacion_tyc`) pueden quedarse como están, o reconstruirse solo para agregar `STRICT`.

Antes de la Fase 2, limpiar los datos que romperían las restricciones nuevas: los duplicados de incidencias (H8) y `proveedor IS NULL → 'yupy'` (H14).

Recrear **todos** los índices existentes (sección 1 del esquema actual) más los nuevos de H5, H6 y H8. `DROP TABLE` borra los índices de la tabla.

### Fase 3: código adaptado (mismo despliegue que la Fase 2)

- Leer y escribir `*_centimos` en todas partes; formatear a soles solo al mostrar.
- Usar las vistas de reporte (`venta_real`).
- `init_db()` llama a `migrar()` en lugar de hacer `ALTER` sueltos (H10).

### Fase 4: posterior (migraciones separadas, sin urgencia)

- Eliminar `incidencia.resuelta` cuando nada la lea (H7).
- Pasar a `transaccion_id` y, si se justifica, crear la tabla `pago_intento` (H14).
- `sesion_compra.telefono NOT NULL` cuando el flujo lo garantice (H9).

**Rollback:** restaurar el respaldo de la Fase 0 y volver a la versión anterior del código. Como las columnas de dinero cambian de nombre, el código viejo no puede funcionar contra el esquema nuevo; el rollback siempre es de los dos juntos.

---

## 6. Verificación

Ejecutar antes y después de la Fase 2 y comparar:

```sql
-- conteos (deben coincidir)
SELECT 'venta', count(*) FROM venta UNION ALL
SELECT 'boleto', count(*) FROM boleto UNION ALL
SELECT 'pago', count(*) FROM pago UNION ALL
SELECT 'sesion_compra', count(*) FROM sesion_compra UNION ALL
SELECT 'incidencia', count(*) FROM incidencia UNION ALL
SELECT 'evento', count(*) FROM evento;

-- dinero (antes: SUM(monto_total)*100 ; después: SUM(monto_total_centimos))
SELECT SUM(monto_total_centimos), SUM(monto_bruto_centimos), SUM(monto_descuento_centimos) FROM venta;
SELECT SUM(precio_centimos) FROM boleto;

-- integridad
PRAGMA integrity_check;          -- 'ok'
PRAGMA foreign_key_check;        -- sin filas

-- fechas convertidas
SELECT id, sale_en FROM venta WHERE sale_en IS NOT NULL AND datetime(sale_en) IS NULL;   -- sin filas

-- pago vs venta cuadran
SELECT v.booking_id, v.monto_total_centimos, p.monto_centimos
FROM venta v JOIN pago p USING (booking_id)
WHERE v.monto_total_centimos <> p.monto_centimos;   -- sin filas (revisar a mano si hay descuentos)

-- huérfanos en tablas de registro (sin FK)
SELECT 'evento', count(*) FROM evento WHERE booking_id IS NOT NULL AND booking_id NOT IN (SELECT booking_id FROM sesion_compra)
UNION ALL
SELECT 'incidencia', count(*) FROM incidencia WHERE booking_id IS NOT NULL AND booking_id NOT IN (SELECT booking_id FROM sesion_compra);

-- agregados de cliente consistentes
SELECT c.documento, c.total_viajes, x.viajes, c.total_gastado_centimos, x.gasto
FROM cliente c
JOIN (SELECT b.documento, count(*) AS viajes, SUM(b.precio_centimos) AS gasto
      FROM boleto b JOIN venta v ON v.id = b.venta_id
      WHERE v.estado = 'emitida' GROUP BY b.documento) x USING (documento)
WHERE c.total_viajes <> x.viajes OR c.total_gastado_centimos <> x.gasto;   -- sin filas
```

Prueba funcional mínima después del despliegue: una compra simulada completa (enlace → asiento → pago → emisión). Revisar que se creen `sesion_compra`, `pago`, `venta` y `boleto` con montos en céntimos y `sale_en` en ISO.

Las consultas de huérfanos y de agregados de `cliente` pueden quedar como un chequeo diario (o en el panel de soporte), ya que esas relaciones no tienen FK.

---

## 7. Decisiones que debe tomar el dueño del proyecto

1. ¿Una sesión (`booking_id`) puede generar más de una venta, por ejemplo al reemitir? Define si `venta.booking_id` es `UNIQUE` (H5).
2. ¿La zona horaria oficial de la base es hora de Lima sin sufijo, o UTC? (H1)
3. ¿`cliente` se crea antes o después de insertar `boleto`? Define si `boleto.documento` lleva FK (H3).
4. ¿Las compras siempre vienen de WhatsApp? Define si el teléfono vacío es un bug (H9).
5. ¿Las ventas simuladas deben vivir en la base de producción? (H12)
6. ¿Qué estados existen realmente para `incidencia.estado`, y cuáles cuentan como cerradas? (H4, H7)

---

## 8. Checklist para Claude Code

- [ ] Leer el módulo de base de datos (`db.py` o similar), `init_db()` y `_COLUMNAS_NUEVAS`.
- [ ] Responder las secciones "Verificar en el código" de H1–H15 y anotar diferencias con esta auditoría.
- [ ] Proponer las respuestas a la sección 7 o preguntarlas.
- [ ] Fase 1: `PRAGMA foreign_keys`, formato de fechas, constantes de estados, deduplicación de incidencias.
- [ ] Crear el sistema de migraciones (`user_version`) con `001_baseline`.
- [ ] Escribir `002_solidificar` (reconstrucción) y probarla sobre una copia de la base.
- [ ] Adaptar el código a `*_centimos` y a las vistas de reporte.
- [ ] Ejecutar la verificación de la sección 6 antes y después, y comparar.
- [ ] Hacer una compra simulada de punta a punta.
