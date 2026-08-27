# Datos — planos de buses

`planos_buses/` contiene un archivo `.json` por categoría de servicio (`ejecutivo.json`,
`emperador.json`, `emp_vip.json`, `premium.json`, `golden_suite.json`, `emp_plus.json`). Al
arrancar, `cargar_planos_maestros()` en `api_ventas.py` los lee todos y los combina en memoria (sin
caché: se releen del disco en cada llamada a `resolver_plano_por_placa` y `placas_configuradas`).

## Estructura de cada archivo

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

- **`plantillas`**: cada clave es un código de plantilla arbitrario (no corresponde a la placa).
  Cada plantilla tiene un nombre de `servicio` y uno o más `pisos`; cada piso tiene `rows`, una
  matriz de filas donde cada celda representa un asiento u otro elemento del layout.
- **`placas`**: asocia cada placa de bus a un código de plantilla. Varios buses físicos (varias
  placas) pueden compartir la misma plantilla de asientos. `normalizar_placa()` elimina todo
  carácter no alfanumérico y convierte a mayúsculas antes de comparar, de modo que el formato
  exacto de la placa en el JSON no es relevante (`C0A-967` y `c0a967` se consideran equivalentes).

## Tipos de celda (`type`)

| `type` | Significado | Campos adicionales |
|---|---|---|
| `seat` | Asiento disponible en el JSON base (el backend lo reescribe a `occupied` en tiempo real según la venta registrada en JELAF; ver más abajo) | `id` (número de asiento, texto con ceros a la izquierda), `angle` (grado de reclinación, opcional) |
| `occupied` | Asiento ya vendido | Igual que `seat` |
| `empty` | Espacio vacío o pasillo | — |
| `label` | Texto libre en el layout | `text` |
| `tv`, `stairs`, `bath`, `exit` | Iconos decorativos del layout (TV, escalera, baño, salida) | — |

El JSON en disco no marca ningún asiento como `occupied`. Ese estado lo determina
`obtener_plano_bus()` en tiempo real: consulta a JELAF (`/itinerarios/turnos`), identifica los
asientos que ya tienen `Nombres`, `NumeroDocumento` o `FlagVenta` en la respuesta, y reescribe esas
celdas del plano base de `seat` a `occupied` antes de devolverlo al frontend. En otras palabras: la
geometría del plano es local y estática, mientras que la ocupación se calcula siempre en vivo.

## Precio

El JSON de un piso no incluye precio por defecto. `obtener_plano_bus()` inyecta
`data_piso["price"]` tomando el primer valor `PrecioVenta` o `PrecioNormal` distinto de cero que
encuentra entre los asientos de ese piso en la respuesta de JELAF. Si ningún asiento aporta precio,
el piso queda sin la clave `price`; ver el bug documentado en
[`backend-api.md`](backend-api.md#post-apiv1plano-bus).

## Orientación vertical y horizontal

El JSON conserva las filas tal como están en el croquis original (probablemente elaborado a partir
de una hoja de cálculo, en orientación horizontal). El frontend
(`seleccionar-asiento-bus2.html` y la herramienta `preview-planos/`) transpone esa matriz a una
vista vertical (de frente hacia atrás del bus) mediante la función `transponerAVertical()`, que
recorre las filas de abajo hacia arriba para que izquierda y derecha queden reflejadas
correctamente. Esta lógica está duplicada en ambos archivos.

## Herramienta de revisión visual

`preview-planos/index.html` no forma parte del flujo de compra: es un visor independiente que
carga directamente los archivos `.json` de `planos_buses/` (mediante
`fetch('../planos_buses/...')`, por lo que requiere servirse por HTTP y no puede abrirse como
`file://`) y permite revisar cada plantilla en orientación horizontal o vertical antes de que un
bus la utilice en producción.
