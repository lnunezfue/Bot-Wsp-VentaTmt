# Venta sin navegador (CHATBOT SIN NAVEGADOR)

Este proyecto es `CHATBOT PANELES` con el worker de navegador reemplazado por
ventas **por HTTP** contra JELAF, usando las llamadas descubiertas en tres
capturas reales de boletería (`docs/descubrimiento-venta-http.md`).

## Qué cambia

| | CHATBOT PANELES (navegador) | CHATBOT SIN NAVEGADOR (HTTP) |
|---|---|---|
| Ventas a la vez | 1 por cuenta (un Chrome por venta) | `VENTAS_POR_CUENTA` × número de cuentas (`JELAF_USUARIO_1…`; defecto 1 por cuenta). La emisión sigue siendo de a una entre todas. |
| Navegador | abierto todo el día, hace los clics | solo para iniciar sesión; se cierra |
| Elegir la fila del viaje (V3) | buscar la fila por hora/destino | no aplica: el turno se pide por sus códigos |
| Pestaña del pasajero (C3) | clic en la pestaña correcta | no aplica: un elemento por asiento |
| Número de boleto (V5) | leído del popup | viene por asiento en la respuesta |
| Popup de autorización (1030/set11) | lo resolvía el bot | no aplica: se vende a precio de JELAF |
| Comprobante | PDF propio | + enlace al comprobante electrónico de JELAF |
| Pago registrado en JELAF (V6) | VISA + 4 dígitos al azar | tipo según el medio + 4 dígitos reales |

## Flujo

```
/iniciar            → VentaHttp nueva (si hay lugar) + turno en segundo plano
/seleccionar-asiento→ turno fresco (¿vendido?) → bloquearAsiento → IDS
/liberar-asiento    → liberarArregloAsientos([IDS])
/pasajero           → datos en memoria + consultar-usuario (IdCliente)
/checkout           → grabar-pasajero en JELAF → cobro en Yupy
/confirmar          → verifica Yupy → [de a una] liberarArregloAsientos + grabar-venta
                      → boletos por asiento (número + LinkPag_FE)
reaper / cancelar   → liberarArregloAsientos de los bloqueos que queden
```

- Todo va en paralelo salvo el último paso, que se hace **de a una** porque todas
  las ventas van a la misma caja del cajero (`Idcaja`). Tarda 1–2 s por venta.
- Si JELAF **rechaza** la venta (`EsCorrecto=false`), no se emitió: queda
  `cobrada_sin_emitir` con incidencia crítica.
- Si JELAF **no responde** después de enviar `grabar-venta`, se concilia contra
  el plano real (documento por asiento): `emitida`, `cobrada_sin_emitir` o
  `por_conciliar`. Nunca se reintenta a ciegas.
- Antes de bloquear se revisa el plano fresco, porque JELAF deja bloquear un
  asiento que ya tiene venta (solo rechaza si otro *bloqueo* lo tiene).
- Se respeta `CantidadMaxBloqAsi` del turno: si JELAF limita los asientos
  apartados por bus, el bot no pasa de ese número sumando todas sus ventas.

## Modos

| `VENTA_MODO` | Qué hace |
|---|---|
| `simulacion` (por defecto) | Consulta JELAF de verdad (viajes, plano, precios, clientes). **No** bloquea, **no** graba pasajeros, **no** emite. Guarda el cuerpo de `grabar-venta` que habría enviado, sin datos personales, en `descubrimiento_output/simulacion_*.json`. Las ventas quedan con estado `simulada` (no cuentan en los paneles). |
| `real` | Bloquea, graba y **emite boletos reales**. En producción hay que declararlo explícitamente. |

## Antes de pasar a `VENTA_MODO=real`

1. **Usuario de JELAF propio para el bot.** Las capturas se hicieron con el
   usuario 613, que también es el de la boletería. Si el bot vende con él, sus
   ventas caen en la **misma caja** que las de la persona de boletería. Pedir a
   la empresa un usuario y una caja para ventas web.
2. `python prueba_venta_http.py` (solo lectura): debe mostrar `OK` en Token,
   cajero, `Idcaja`, empresa y correlativo, y guarda el cuerpo que se enviaría.
3. Confirmar los valores **POR VALIDAR** (`venta_jelaf.POR_VALIDAR`) con una
   captura de una venta de boletería (`python descubrir_endpoints_venta.py`):
   el resumen ya muestra `Nacionalidad`, `DescuentoTipoDC`,
   `CodiTarjetaCredito`, `CanalVenta` y el orden del nombre.
   - Nacionalidad (hoy `PERUANA`), tipo de descuento (hoy `0`), canal
     (hoy `3`, el de boletería), orden del nombre (hoy nombres primero).
   - Formato del asiento menor que 10: hoy se envía `"5"` (sin cero). Vender
     un asiento < 10 en la captura para confirmarlo.
4. Confirmar con Yupy en qué campos informa el medio de pago (Yape/tarjeta,
   marca, 4 últimos dígitos). Mientras tanto se usa `JELAF_TARJETA_DEFECTO`
   con los 4 últimos dígitos de la transacción de Yupy. El log muestra la
   **forma** de cada orden pagada para completar `pagos_yupy.medio_de_pago`.
5. Primera venta real: un asiento, un viaje con boletos anulables, y revisar en
   JELAF que el boleto quedó igual que uno de boletería.
6. **Tramos (rutas cortas, `rutas.py`, 2026-10-07).** Las sedes intermedias del
   Excel de programación (Ica, Nasca, Moquegua, Ilo, Mollendo, Camaná, Km 48…)
   se venden con su código de JELAF (`jelaf_http.CIUDADES_CODIGOS`).
   Confirmado en vivo, solo lectura, el 2026-10-07:
   - Códigos: lista `rutas` de la pantalla `/itinerarios` (ver
     `descubrimiento-venta-http.md`). `get-cuidades` son regiones, no ciudades.
   - `lista-itinerarios` con `CodiDestino` = la escala devuelve los turnos con
     los códigos **del tramo** (Moquegua→Tacna: `CodiOrigen 6`, `CodiDestino 2`;
     `NomDestino` viene "TODOS"). Si algún día devolviera el destino final,
     `/buscar-viajes` descarta ese turno.
   - `/turnos` con esos códigos da el precio y la ocupación **del tramo**: mismo
     bus, Moquegua→Tacna S/ 25 y Arequipa→Tacna S/ 50; Arequipa→Camaná S/ 80 y
     Arequipa→Lima S/ 110.
   - Qué pares se ofrecen (`rutas.tramo_valido`): los que pasan en orden por
     alguna ruta de `rutas.RUTAS`. Las rutas salen del Excel y de un barrido de
     solo lectura en JELAF (2026-10-07: 18 orígenes, 3 días, paradas de cada
     ruta en `ListaDestinosRutaParaPdfUsuario` de `/turnos`). JELAF confirmó las
     8 rutas del Excel y agregó Cerro Azul (53), El Fiscal (48) y Tacna⇄Cusco
     por Puno (56) y Juliaca (58). No hay Arequipa⇄Mollendo/Ilo (los lista la
     web oficial, pero en JELAF todo bus de Arequipa va a Lima o a Tacna) ni
     Lima/Arequipa⇄Cusco. JELAF pone Ilo en el bus Cusco→Tacna, pero ese bus
     va de Moquegua a Camiara en 1 h: no se ofrece Ilo en esa ruta.
     Repetir el barrido si la empresa cambia rutas.
   - POR VALIDAR: una venta de boletería de un tramo (captura) para confirmar
     `bloquearAsiento` y `grabar-venta` con códigos de escala antes de vender
     tramos con `VENTA_MODO=real`.

## Pago

- **Yupy** (QR/checkout, `YUPY_API`) y **Niubiz** (tarjeta). En ambos el servidor fija el monto y
  verifica el cobro antes de emitir. Con Niubiz el cargo lo hace el propio servidor
  (`/niubiz/autorizar`), y en JELAF la venta queda como VISA/MASTERCARD con los 4 últimos dígitos.
- Antes de abrir cualquier cobro se graban los pasajeros en JELAF: si JELAF los rechaza, no se cobra.

## Precio de los asientos

Sale de `/itinerarios/turnos`. En un viaje **sin ventas** los asientos vacíos pueden venir sin
precio: entonces no se venden (falla cerrado, hallazgo N19) hasta confirmar con una captura de
dónde lo toma la pantalla de JELAF.

## Lo que no se replica (a propósito)

- **SignalR** (`/signalr/send`): el aviso en tiempo real a otras boleterías. El
  bloqueo existe igual en el servidor de JELAF; las demás pantallas lo ven al
  refrescar el plano.
- `consultaManifiestoProgramacion`, `obtener-puertas-puntoventa`,
  `convertir-venta-to-base64`, `grabar-log`: auxiliares de la pantalla o de la
  impresión. Se pueden agregar si la empresa lo pide.

## Verificación

- `python -m pytest`: 114 tests, incluidos 3 y 5 compras simultáneas de punta a
  punta, dos clientes por el mismo asiento, capacidad, emisión de a una, fallos
  de JELAF (rechazo, sin respuesta, emitió y cayó), y el cuerpo de
  `grabar-venta` comparado campo por campo con la captura real.
- `python tests/servidor_con_dobles.py --puerto 8001` +
  `python tests/servidor_estatico.py 5502 --api http://127.0.0.1:8001`:
  la mini-webapp completa contra un JELAF falso, varias pestañas a la vez, sin
  tocar el backend que esté corriendo en :8000. Probado con 3 compras
  simultáneas en 3 pestañas.
