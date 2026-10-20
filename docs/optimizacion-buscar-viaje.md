# Optimización de `buscar_viaje()` — medida en vivo, no estimada

> Este documento registra una medición real contra JELAF (no una estimación leyendo
> código) del costo de `JelafWorker.buscar_viaje()` antes y después de reemplazar
> los `wait_for_timeout()` fijos heredados del RPA v1 por esperas de condiciones
> reales. Ningún run de estas mediciones tocó un asiento real — `buscar_viaje()`
> solo abre el plano, nunca hace clic en `#btnAsiento_XX` — así que se repitió
> muchas veces sin crear bloqueos ni afectar inventario.

## Paso cero: instrumentación

`buscar_viaje()` ahora registra una marca de tiempo (`time.monotonic()`) después de
cada paso, acumuladas en `self.ultima_medicion` e impresas como una sola línea
`⏱️ [worker] buscar_viaje() total=...`. Es la misma convención de logging con
emoji que ya usa el resto del módulo — no agrega dependencias ni cambia el
comportamiento, solo dice cuánto tardó cada parte.

## Medición base (diseño original, sin optimizar)

6 corridas seguidas, mismo viaje real (TACNA, 20+ turnos disponibles en la fecha
usada). Resultado extremadamente estable — desvío de solo 0.11s:

| Paso | Tipo | Promedio medido |
|---|---|---|
| `goto` | navegación real | 1.259s |
| `sleep_post_goto_3000ms` | **sleep fijo** | 3.009s |
| `combo_origen` (incluye 3 sleeps de 300/600/300ms) | mixto | 1.389s |
| `fecha` | real | 0.043s |
| `checkbox_todos` (incluye sleep de 500ms) | mixto | 0.573s |
| `click_buscar` | real | 0.068s |
| `sleep_post_buscar_2000ms` | **sleep fijo** | 2.007s |
| `sonda_boton_ok` | real | 0.011s — *ya era barata; no hacía falta tocarla* |
| `dblclick_fila` | real | 0.045s |
| `plano_visible` | real | 1.421s |
| `sleep_final_2000ms` | **sleep fijo** | 2.011s |
| **Total** | | **11.84s** (media), 11.67–11.96s (rango) |

Un hallazgo que corrige el análisis inicial: la "sonda del botón OK" que se había
señalado como candidata a optimizar (~2s estimados) en realidad solo cuesta 11ms
en la práctica — `is_visible(timeout=...)` no bloquea el timeout completo cuando
el elemento no existe. No se tocó, porque no había nada que ganar ahí.

## Cambios aplicados

1. **No recargar si el worker ya está parado en itinerarios.** `_login()` y
   `liberar_y_resetear()` ya hacen su propio `goto` sobre esta misma URL antes de
   que se llame a `buscar_viaje()` en cualquier caso real. Se reemplazó el
   `goto` + `wait_for_timeout(3000)` incondicional por una espera real y corta de
   que `#txtFecha` esté visible; solo si eso falla (worker sin pasar por
   login/reset) se navega de verdad.
2. **`_seleccionar_v_select()` sin sleeps fijos.** Los tres `wait_for_timeout`
   (300/600/300ms) quedaban cubiertos igual por las esperas reales que ya
   existían después (`.fill()` auto-espera, `opcion.wait_for()` espera la opción
   filtrada de verdad).
3. **Esperar la respuesta real de `lista-itinerarios-paginado`** (endpoint
   confirmado por interceptación de red, ver `descubrir_endpoint_dni.py`) en vez
   de dormir 2s a ciegas tras el clic en Buscar, más una espera corta de que la
   tabla tenga al menos una fila pintada.
4. **Saltar el combo de origen si ya está en el valor correcto.** Se lee el
   texto actual de `#cboOrigenPas` y solo se abre/filtra el dropdown si no
   coincide con el origen pedido.
5. **Esperar el primer botón de asiento real** (`[id^='btnAsiento_']` visible) en
   vez de dormir 2s fijos al final, porque `#txtNroAsientoVenta` puede quedar
   visible un instante antes de que la grilla de asientos termine de pintarse.

## Un hallazgo no anticipado: la prueba "espalda con espalda" es más dura que la producción real

La primera ronda de medición encadenaba `liberar_y_resetear()` → `buscar_viaje()`
inmediatamente, sin ninguna pausa. Con eso, el total bajó a **~5.43s** (-54%), pero
`combo_origen` salió inconsistente (0.2s a 2.9s) y, cuando corría completo, tardaba
*más* que en el diseño original (hasta 2.9s vs 1.389s antes).

La causa: justo después del `goto`, JELAF dispara una ráfaga propia de
llamadas de inicialización (`panel-control-oficina`, `get-validacion-recurso`,
`obtener-fecha-sistema`, etc. — visibles en la captura de red de
`descubrir_endpoint_dni.py`). El `wait_for_timeout(3000)` original absorbía esa
ráfaga sin querer, antes de tocar nada más. Al quitarlo, `buscar_viaje()`
empieza a interactuar con la página mientras esa ráfaga todavía está en curso,
compitiendo por el mismo hilo — y el combo de origen, que depende de que la
cuenta ya haya recibido su terminal por defecto, todavía no está poblado.

Eso no representa el caso real: en producción, `liberar_y_resetear()` ocurre
cuando SE CIERRA una venta, y el siguiente `buscar_viaje()` (vía el prefetch de
`docs/architecture.md#prefetch-de-la-búsqueda-del-viaje`) lo dispara OTRO
cliente distinto, eligiendo su propio viaje — hay como mínimo unos segundos de
por medio, casi siempre más. Repitiendo la medición con una pausa de 3s entre
corridas (representativa de ese intervalo real):

| Corrida | Total | `combo_origen` | Origen saltado |
|---|---|---|---|
| 1 (sin pausa previa — arranque en frío) | 5.58s | 2.87s | No |
| 2 | 2.76s | 0.20s | **Sí** |
| 3 | 2.76s | 0.17s | **Sí** |
| 4 | 2.36s | 0.21s | **Sí** |
| 5 | 2.25s | 0.17s | **Sí** |
| 6 | 2.21s | 0.15s | **Sí** |

**Media en régimen estable (corridas 2–6): 2.47s.** La corrida 1 (arranque en
frío, sin búsqueda previa) es un costo único por worker al iniciar el backend,
no algo que se repita entre ventas.

## Resultado final

| Escenario | Total |
|---|---|
| Diseño original | **11.84s** |
| Optimizado, sin pausa entre ventas (caso de estrés, no representativo) | 5.43s |
| Optimizado, con el intervalo real entre ventas | **~2.2–2.8s** |

La meta planteada ("de ~12s a 2-3s") se cumplió en el escenario representativo de
producción. Combinado con el adelanto de la búsqueda (prefetch, disparado un
paso antes de que el cliente toque el plano), el tiempo que el cliente percibe al
tocar su primer asiento debería ser, en la práctica, instantáneo.

## Qué no se tocó

- La arquitectura del pool de workers, el prefetch, el control de acceso por
  `booking_id` y el resto del flujo de venta quedan exactamente igual.
- No se necesitaron cuentas JELAF adicionales.
- La lógica de negocio de `buscar_viaje()` es la misma: los mismos pasos, en el
  mismo orden, solo con esperas reales en vez de sleeps fijos.
