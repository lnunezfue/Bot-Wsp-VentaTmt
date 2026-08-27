# Datos — planos de buses

`planos_buses/` tiene un `.json` por categoría de servicio (`ejecutivo.json`, `emperador.json`,
`emp_vip.json`, `premium.json`, `golden_suite.json`, `emp_plus.json`). Al arrancar,
`cargar_planos_maestros()` en `api_ventas.py` los lee todos y los combina en memoria (no hay caché:
se releen del disco en cada llamada a `resolver_plano_por_placa` / `placas_configuradas`).

## Forma de cada archivo

```json
{
  "plantillas": {
    "020-40": {
      "servicio": "Emperador",
      "pisos": {
        "1": {
          "rows": [
            [
              { "type": "seat", "id": "51", "angle": "160" },
              { "type": "seat", "id": "54", "angle": "160" },
              { "type": "empty" },
              { "type": "seat", "id": "03", "angle": "145" }
            ]
          ]
        }
      }
    }
  },
  "placas": {
    "C0A967": "020-40",
    "C0A969": "020-40"
  }
}
```

- **`plantillas`**: cada clave es un **código de plantilla** arbitrario (no es la placa). Cada
  plantilla tiene un nombre de `servicio` y uno o más `pisos`; cada piso tiene `rows`, una matriz de
  filas donde cada celda es un asiento u otro elemento del layout.
- **`placas`**: mapea **placa de bus → código de plantilla**. Varios buses físicos (varias placas)
  pueden compartir la misma plantilla de asientos. `normalizar_placa()` quita todo lo que no sea
  alfanumérico y pasa a mayúsculas antes de comparar, así que el formato exacto de la placa en el
  JSON no importa mucho (`C0A-967` y `c0a967` matchean igual).

## Tipos de celda (`type`)

| `type` | Significado | Campos extra |
|---|---|---|
| `seat` | Asiento disponible/libre en el JSON base (el backend lo reescribe a `occupied` en tiempo real según la venta en JELAF, ver abajo) | `id` (número de asiento, string con ceros a la izquierda), `angle` (grado de reclinación, opcional) |
| `occupied` | Asiento ya vendido | igual que `seat` |
| `empty` | Espacio vacío / pasillo | — |
| `label` | Texto libre en el layout | `text` |
| `tv`, `stairs`, `bath`, `exit` | Iconos decorativos del layout (TV, escalera, baño, salida) | — |

**El JSON en disco no marca ningún asiento como `occupied`.** Eso lo hace
`obtener_plano_bus()` en tiempo real: llama a JELAF (`/itinerarios/turnos`), junta los asientos que
ya tienen `Nombres`/`NumeroDocumento`/`FlagVenta` en la respuesta, y reescribe esas celdas del
plano base de `seat` a `occupied` antes de devolverlo al frontend. Es decir: **el plano (geometría)
es local y estático; la ocupación es siempre en vivo.**

## Precio

El JSON de un piso **no trae precio** por defecto. `obtener_plano_bus()` le inyecta
`data_piso["price"]` tomando el primer `PrecioVenta`/`PrecioNormal` distinto de cero que encuentre
entre los asientos de ese piso en la respuesta de JELAF. Si ningún asiento trae precio, el piso se
queda sin la clave `price` — ver el bug documentado en
[`backend-api.md`](backend-api.md#post-apiv1plano-bus).

## Cómo se usa la orientación vertical/horizontal

El JSON guarda las filas tal como están en el croquis original (probablemente copiado de un Excel,
horizontal). El frontend (`seleccionar-asiento-bus2.html` y la herramienta `preview-planos/`)
**transpone** esa matriz a una vista vertical (de frente hacia atrás del bus) con la función
`transponerAVertical()`, que recorre las filas de abajo hacia arriba para que izquierda/derecha
queden reflejadas correctamente. Es la misma lógica duplicada en ambos archivos.

## Herramienta de revisión visual

`preview-planos/index.html` no es parte del flujo de compra: es un visor standalone que carga
directamente los `.json` de `planos_buses/` (con `fetch('../planos_buses/...')`, por eso necesita
servirse por HTTP, no abrirse como `file://`) y permite ver cada plantilla en horizontal u
orientación vertical antes de que un bus la use en producción.
