# Venta por HTTP en JELAF — lo descubierto (29/09/2026)

Captura pasiva (`descubrir_endpoints_venta.py`) de **una venta real de 1 asiento, pago al contado**,
hecha por boletería con su usuario. Aquí va solo la forma de las llamadas; nada de datos personales.

**Conclusión:** la venta completa se hace con llamadas HTTP que ya se pueden reproducir. La emisión
es **una sola llamada** (`grabar-venta`). Si se replica, el bot no necesita navegador para vender y
un solo usuario de JELAF podría atender varias ventas a la vez (el límite de hoy, R1, desaparece).
Falta resolver los puntos de la sección "Preguntas abiertas" y tener autorización de la empresa.

## Secuencia observada

| Paso | Llamada | Qué hace |
|---|---|---|
| Elegir asiento | `POST itinerarios/bloquearAsiento` `{CodiProgramacion:0, NroViaje, CodiOrigen, CodiDestino, NumeAsiento, FechaProgramacion, Precio:0}` | Aparta el asiento. `Valor` = id del bloqueo (`IDS`). |
| | `POST signalr//send` `A:[NroViaje, fecha, asiento]` | Avisa en tiempo real a las demás boleterías para que pinten el asiento. |
| Cambiar asiento | `POST itinerarios/liberarAsiento` `{"request": "{Terminal:22, IDS}"}` | Suelta un bloqueo. |
| Pasajero | `GET itinerarios/consultar-usuario` | Datos del cliente por documento (ya lo usa el bot). |
| | `POST promocion/consultar-usuario-{nuevo,corporativo,Kilometraje}` | Comprueba descuentos del cliente. |
| | `POST itinerarios/grabar-pasajero` `{"request": "[{IdCliente, TipoDoc, NumeroDoc, nombres, FechaNacimiento, Edad, Telefono, Sexo, ...}]"}` | Guarda/actualiza la ficha del cliente. `Valor` = bool. |
| Aceptar | `POST itinerarios/liberarArregloAsientos` `{"request": "{Terminal:22, arregloIDS:[...]}"}` | **JELAF mismo suelta sus bloqueos justo antes de vender** (igual que lo que se validó en v1). |
| | `POST itinerarios/grabar-venta` | **La emisión.** Ver abajo. |
| | `POST itinerarios/convertir-venta-to-base64` | Genera el comprobante imprimible (Original/Copia1/Copia2 en base64). |
| | `POST itinerarios/grabar-log` | Bitácora. |

Casi todas usan el patrón `{"request": "<JSON como texto>"}` y responden
`{Estado, Mensaje, Valor, EsCorrecto}`.

## `grabar-venta`

Cuerpo: `{"request": "<JSON>"}` con:

- `Listado`: **un elemento por asiento** (así se venden varios en una sola llamada). Campos clave:
  `CodiEmpresa, CodiOficina, CodiPuntoVenta, CodiOrigen, CodiDestino, NumeAsiento, FlagVenta:"V",
  PrecioVenta, PrecioNormal, Nombre, Edad, Telefono, Dni, TipoDocumento, Sexo, TipoPago,
  FechaViaje, HoraViaje, CodiServicio, CodiEmbarque, CodiArribo, NivelAsiento, CodiTerminal:"022",
  NroViaje, FechaProgramacion, HoraProgramacion, CodiBus, CodiSucursal, CodiRuta,
  CodiTarjetaCredito, NumeTarjetaCredito, NumeOperacionTarj, ValidadorDescuento,
  DescuentoTipoDC, ImporteDescuentoDC, AutorizadoDC, CodAutorizadoDC, Id_CCaja, Fecha_liq, TurnoLiq…`
  (unos 90 campos en total).
- Cabecera de la venta: `FlagVenta, ListaEmbarques, UsrCodiEmpresa, UsrCodiSucursal,
  UsrCodiPuntoVenta, UsrTerminal:22, UsrCod, CanalVenta:3, id_puerta, TurnoLiq, TerminalLiq`.

Respuesta: `Valor.ListaVentasRealizadas[]`, **uno por asiento**, con `IdVenta, NumeAsiento,
BoletoCompleto, BoletoSerie, BoletoNum, EmisionFecha/Hora, LinkPag_FE, CodigoX_FE, PrecioCan,
PlacaBus…`, y `Valor.CodiProgramacion`.

Lo que esto resuelve:

- **V5 (números de boleto):** el número llega por asiento, en JSON. No hay que leerlo del popup.
- **V4 (PDF del boleto):** `LinkPag_FE` y `CodigoX_FE` son del comprobante **electrónico** real, y
  `convertir-venta-to-base64` da el imprimible oficial. El PDF propio podría reemplazarse por estos.
- **V6 (tarjeta falsa):** el pago se manda como campos (`TipoPago`, `CodiTarjetaCredito`,
  `NumeTarjetaCredito`, `NumeOperacionTarj`); se podría enviar la operación de Yupy en vez de un
  número inventado, si la empresa define cómo registrarlo.
- **Popup de autorización (claves 1030/set11):** los campos `ValidadorDescuento`, `AutorizadoDC` y
  `CodAutorizadoDC` indican que autoriza **vender por debajo de `PrecioNormal`**. Con la corrección de
  V2 (precio de JELAF) no debería volver a aparecer. Confirmar con la empresa.

## Segunda captura (29/09/2026): 2 asientos, pago con tarjeta

- **`authorization: Token <9 caracteres>`**, el mismo valor durante toda la sesión y presente
  **antes** del login: es una clave fija de la aplicación, no la sesión del usuario. La identidad va
  en la cookie.
- **2 asientos = 1 sola venta:** `grabar-pasajero` una vez, `liberarArregloAsientos` con los 2
  `IDS`, `grabar-venta` con 2 elementos en `Listado` → 2 boletos, cada uno con su número.
- **Pago con tarjeta:** `TipoPago: "03"` (contado: `"01"`), `CodiTarjetaCredito` (4 caracteres) y
  `NumeTarjetaCredito` de 19 caracteres con forma `0000-0000-0000-NNNN`. Según boletería, **solo
  importan los 4 últimos dígitos**: de la tarjeta, o del **número de operación** si se pagó con Yape.
  → Para V6: en vez de 4 dígitos al azar, poner los 4 últimos de la operación de Yupy; así cada
  venta de JELAF se puede cruzar con su cobro.
- **`Id_CCaja` = 80523 en las dos capturas** (misma caja del día). Todavía no se sabe de qué
  respuesta sale.
- **Comportamiento de la pantalla** (boletería): al dar Enter en el precio salta al siguiente
  asiento seleccionado, en cadena; en el último aparece el botón de pago, que lleva al formulario
  de tarjeta/contado. Coincide con lo que ya hace el RPA (`_pasar_por_todas_las_pestanas`).

Para la tercera captura, el script ya registra: la respuesta del login (antes ilegible), los
catálogos `get-tipoPago`, `get-tipoPago2` y `get-tarjetasCredito` (id y etiqueta: ¿existe "Yape"?),
el `localStorage`/`sessionStorage` sin datos personales (¿ahí está `Id_CCaja`?) y **en qué clave**
guarda la página el Token, sin guardar su valor. Basta con iniciar sesión y abrir la pantalla de
venta; no hace falta vender.

## Tercera captura (29/09/2026): login + plano

- **`login/post-usuario` responde** `Valor: {CodiEmpresa, CodiPuntoVenta, CodiSucursal, CodiUsuario,
  Nombre, Nivel, Terminal, FechaLiq, TurnoLiq, TerminalLiq, **Idcaja**, OpcionesPerfil…}`.
  `Idcaja` es el `Id_CCaja` que exige `grabar-venta`: todo lo del cajero sale del login.
- **El Token de `authorization`** no está en localStorage, sessionStorage ni cookies: está fijo en
  el JS de JELAF. Para replicarlo, leerlo de la cabecera de una petición durante el login.
  **Actualización 04/10/2026:** el JS arma `"Token " + main_globals.userToken` y `userToken` no se
  define en ninguna página, así que el valor es `Token undefined`. `prueba_login_http.py` confirmó
  que el login completo (`get-usuarios` → `post-usuario`) y las lecturas (`obtener-fecha-sistema`,
  `get-empresas`, `lista-itinerarios`) funcionan por HTTP con la cookie `ASP.NET_SessionId`. El bot
  ya no abre navegador; manda `Token undefined` igual que la web.
- **Catálogos:**
  - `get-tipoPago`: 01 CONTADO, 02 MULTI PAGO, 03 TARJETA DE CREDITO.
  - `get-tarjetasCredito`: 0201 VISA, 0202 MASTERCARD, 0203 DEPOSITO EN CTA, 0204 YAPE, 0205 PLIN, 0206 BBVA.
  - `get-tiposDoc`: 00 CEDULA, 01 DNI, 04 C.EXTRANJERIA, 07 PASAP., 09 CEDULA.
    **Ojo:** la mini-webapp usa 02 = pasaporte, 03 = CE y 04 = RUC; hay que traducir antes de enviar
    a JELAF (hoy solo se usa 01 = DNI, así que no causa errores todavía).
  - `get-servicios`: 1 ECONOMICO, 2 IMPERIAL, 3 EMPERADOR, 4 EMPERADOR VIP, 5 PREMIUM, 6 CARGUERO,
    7 EJECUTIVO, 8 GOLDEN SUITE, 9 PREMIUM PLUS, 10 EMPERADOR PLUS.
  - `get-cuidades`: **regiones**, no ciudades: 01 NORTE, 02 CENTRO, 03 SUR (leído el 2026-10-07).
  - **Ciudades (CodiOrigen/CodiDestino):** la pantalla `/itinerarios` trae incrustada
    `var rutas = [{id, label}…]` (72 sucursales; si viene vacía, la pide a
    `/base/get-oficinas`). Leído en vivo el 2026-10-07: 1 LIMA, 2 TACNA, 3 AREQUIPA,
    4 MOLLENDO, 5 ILO, 6 MOQUEGUA, 7 ICA, 8 NASCA, 9 CAMANA, 10 KM48, 11 ALTO SIGUAS,
    12 ATICO, 13 CHALA, 14 YAUCA, 15 CAMIARA, 54 CUSCO. Hay variantes que el bot no usa
    (32 ICA TERMINAL, 39 MOLLENDO- CENTRO, 36 PUERTO DE ILO, 47 ATICO C1, 41 YAUCA C-1…),
    terminales y agencias, y un punto **980 VENTAS WHATSAPP** (relevante para N17).
  - **Paradas de una ruta:** cada turno de `/turnos` trae `ListaDestinosRutaParaPdfUsuario`
    (`CodiSucursal`, `NomOficina`, en orden, con el origen) y `NomRuta`. En
    `lista-itinerarios` esa lista viene vacía; sí trae `CodiRuta`, `NomRuta` y `HoraPartida`
    en el origen consultado (la misma `CodiProgramacion` vista desde cada parada da sus horas).
  - `lista-itinerarios` con `CodiDestino` de una escala devuelve los turnos con los
    códigos del tramo y `NomDestino: "TODOS"`; `/turnos` con esos códigos da precio y
    ocupación del tramo (ver `venta-sin-navegador.md`, punto 6).

## Registro del pago en JELAF (decisión de la empresa, 29/09/2026)

Según cómo pagó el cliente en la web:

| Pagó con | `TipoPago` | `CodiTarjetaCredito` | Número |
|---|---|---|---|
| Yape (QR) | 03 | 0204 YAPE | `0000-0000-0000-` + 4 últimos del nro. de operación |
| Tarjeta | 03 | 0201 VISA / 0202 MASTERCARD según la marca | `0000-0000-0000-` + 4 últimos de la tarjeta |

Pendiente: confirmar qué campos de la respuesta de Yupy dicen el medio, la marca y los dígitos.

## Preguntas abiertas (antes de programar nada)

1. ~~Cabecera `authorization`~~ — resuelto: `Token undefined` (ver tercera captura). Login HTTP validado el 04/10/2026.
2. ~~`Id_CCaja`, `Fecha_liq`, `TurnoLiq`~~ — resuelto: vienen en la respuesta del login.
3. **`CanalVenta: 3`**: ¿qué canal es? ¿Existe uno para venta web/WhatsApp?
4. **SignalR:** si el bot no manda los avisos, ¿las demás boleterías ven el asiento como libre hasta
   refrescar? El bloqueo en el servidor existe igual, pero conviene replicarlo.
5. **Varias ventas a la vez con el mismo usuario y la misma caja:** ¿JELAF lo permite? ¿Los
   correlativos se asignan en el servidor (parece que sí)? Por seguridad, serializar `grabar-venta`.
6. ~~Pago con tarjeta y 2+ asientos~~ — resuelto en la segunda captura.
7. **Autorización de la empresa** para operar su API interna. `grabar-venta` emite un comprobante
   electrónico real ante SUNAT: equivocarse en un campo es un comprobante mal emitido.

## Cuarta captura (03/10/2026): elegir y soltar asiento, sin vender

- **Búsqueda de la pantalla** (`lista-itinerarios-paginado`): `{"request": "<JSON>"}` con
  `CodiOrigen:"2"`, `CodiDestino:"0"`, `NomDestino:"TODOS"`, `TodosTurnos:false`,
  `InicioPag:1`, `FinPag:11` (páginas de 10). Confirma la forma que usa el arreglo de búsqueda del
  bot con navegador (CHATBOT PANELES N16).
- La pantalla también pide `lista-itinerarios` **con destino** (`CodiDestino:"1"`, cuerpo plano).
- **Precios:** cada turno de la lista y la respuesta de `/turnos` traen `Precio_N1` y `Precio_N2`
  (¿precio por piso/nivel?). Cada asiento de `ListaPlanoBus` trae `Nivel`, `PrecioNormal`,
  `PrecioVenta`, `PrecioMinimo`, `PrecioMaximo`, `PrecioNormalPanel`, `Id_TipoAsiento` y
  `TipoAsiento`. La captura solo guardó tipos, no valores: falta saber cuál de ellos da el precio
  de un asiento vacío en un viaje sin ventas (hallazgo N19). El catálogo ya usa `Precio_N1/N2`
  para el "Desde S/ …" (solo para mostrar).
- **Apartados:** `/turnos` trae `TablaBloqueoAsientos {CodiOrigen, AsientosOcupados (texto),
  CodiDestino, AsientosLiberados (texto)}`. Probablemente ahí están los asientos apartados que el
  plano no muestra como ocupados (los "libres" que no se podían tomar: 27, 29 y 31 del bus ZBA963).
  Falta ver los valores.
- `obtener-minutos-cierre` respondió `hora1`/`hora2` vacíos: JELAF no tiene configurado un cierre
  de venta antes de la salida. El bot mantiene 30 minutos (`MINUTOS_CIERRE_VENTA`).
- Elegir asiento: `bloquearAsiento` con `Precio: 0`; soltarlo: `liberarAsiento {Terminal, IDS}`.

`descubrir_endpoints_venta.py` ahora guarda, además, los **valores** de esos campos de precio,
nivel, tipo y bloqueo (lista cerrada; nunca nombres, documentos, teléfonos ni usuarios), en la
sección "Precios, niveles y bloqueos del plano" del resumen.

## Quinta captura (03/10/2026): valores del plano, bus ZBA963 (Tacna→Arequipa 10:50 PM)

- **`ListaPlanoBus` es la grilla entera (80 celdas para 43 asientos).** Un asiento **libre** viene
  con `NumeAsiento: 0` y su número en **`Tipo`** (`"34"`, `"03"`); solo el vendido trae
  `NumeAsiento` (32 y 33, `FlagVenta: "V"`). Las celdas que no son asientos traen en `Tipo` un
  código de letras: `VA` (pasillo), `TV`, `LI`, `EE`, `CU`.
  → El bot solo miraba `NumeAsiento`: ignoraba todos los asientos libres y su precio. Corregido
  con `jelaf_http.numero_de_celda` (también en CHATBOT PANELES).
- **El precio es por asiento, en `PrecioNormal`** (`PrecioVenta` es 0 si no se vendió). Piso 1:
  34/37/40/43 a S/ 80, el resto a S/ 70. Piso 2: la columna 01-02-03-10-13-…-31 a S/ 70 y el
  resto a S/ 60. Coincide con lo leído en la pantalla (25 y 28: S/ 70; 26 y 30: S/ 60).
  `PrecioMinimo`/`PrecioMaximo` (20–30 / 200) son el rango que acepta boletería.
- **`Precio_N1`/`Precio_N2` no sirven para cobrar:** 70/70 en la lista paginada para este bus, 0/0
  en `/turnos` y en `lista-itinerarios`.
- **`bloquearAsiento` manda el asiento con dos dígitos** (`"NumeAsiento": "01"`) y la
  `CodiProgramacion` real (56860). El bot ya lo hace así. En `grabar-venta` sigue POR VALIDAR
  ("55" en la captura de venta; falta una venta con asiento < 10).
- `TablaBloqueoAsientos` vino vacío y `ListaResumenProgramacion` = `{CAP: 43, VTT: 2, LBR: 41,
  RVA: 0}` (consultado ANTES de apartar 01, 02 y 41). Para saber si ahí aparecen los apartados hay
  que consultar el turno de nuevo después de apartar.

## Llamadas de la pantalla de venta alrededor de grabar-venta (04/10/2026)

Dos ventas reales del bot (BP58-00008349/50, anuladas) se vieron en JELAF sin nombre. La ficha del
cliente sí tenía los nombres (`diagnostico_cliente_jelaf.py`) y `grabar-venta`/`grabar-pasajero`
eran iguales a los de boletería (`comparar_venta_boleteria.py`, captura del 29/09). Lo que faltaba
eran las llamadas que la pantalla de JELAF hace alrededor de la venta, y que PANELES y la versión
del compañero hacían sin saberlo porque usaban la pantalla. Ahora el bot las hace en el mismo orden:

| Momento | Llamada | Cuerpo (captura) |
|---|---|---|
| Antes de `bloquearAsiento` | `itinerarios/consultaManifiestoProgramacion` | JSON plano `{Suc: "<1>", Prog: CodiProgramacion}` |
| Tras `consultar-usuario` | `promocion/consultar-usuario-nuevo`, `-corporativo`, `-Kilometraje` | `{"request": {Tipo, Numero[, Fecha]}}`; Kilometraje agrega Origen, Destino, Servicio, Empresa, Usuario, Nombre |
| Al aceptar | `base/get-acceso-usuario-modulo`, `consultaManifiestoProgramacion` | GET sin cuerpo / igual que arriba |
| Tras `grabar-venta` | `itinerarios/convertir-venta-to-base64` | `{"request": {ListaVentasRealizadas (de grabar-venta + UsuarioNombre/NomOficina/CodTerminal/NomPVenta + 4 opciones `ver…`/`ocultarQR…` en false), TipoImpresion: "Impresion", CodiRutaConcatenado: "", TurnoLiq, TerminalLiq}}` → Original/Copia1/Copia2 en base64 (`venta_jelaf.cuerpo_comprobante`) |
| Al final | `itinerarios/grabar-log` | `{"request": {Mensaje}}` |

- La respuesta de `grabar-venta` trae `PasNombreCom` (el nombre con que JELAF registró la venta): si
  viene vacío, el bot deja la incidencia crítica `venta_sin_nombre`.
- Si `convertir-venta-to-base64` falla, la venta ya está hecha: incidencia `comprobante_jelaf`.
- Por confirmar en vivo: `Suc` (se usa el `CodiSucursal` del turno), `Fecha` de las promociones (se
  usa la del viaje) y el texto de `grabar-log` (66 caracteres en boletería; el bot escribe el suyo).
- No replicado: `obtener-puertas-puntoventa` (`{punto_venta: 2}`, origen del valor sin confirmar) y
  los avisos `signalr/send` a las otras boleterías.

**Tercera venta real sin nombre (BP58-00008351, 22:07):** ya con esas llamadas, la incidencia
`venta_sin_nombre` saltó: JELAF respondió `PasNombreCom` vacío. En boletería `PasNombreCom` tiene el
mismo largo que `Nombre` (27), y el cuerpo del bot es igual (todos los códigos coinciden; solo
difieren los datos propios del viaje). Conclusión: JELAF arma la venta con el pasajero grabado **en la
sesión**. Boletería hace DNI → promociones → `grabar-pasajero` → Aceptar → `grabar-venta` seguidos; el
bot grababa al abrir el pago y vendía minutos después. Ahora, dentro del lock de emisión, el bot repite
DNI → promociones → `grabar-pasajero` justo antes de Aceptar (`VentaHttp._pasajeros_como_pantalla`).
Validar con una venta real controlada.

## CAUSA CONFIRMADA de las ventas sin nombre (05/10/2026)

El diagnóstico de la venta BP58-00008359 descartó la sesión: `Nombre` de 27 caracteres, `grabar-pasajero`
2.5 s antes, misma sesión… y `PasNombreCom` vacío. El código de la pantalla de venta de JELAF
(`descargar_codigo_pantalla_jelaf.py`, guardado en `codigo_jelaf/`, fuera de git) arma el campo así:

```js
input.Nombre = (objVenta.Nombres + ',' + objVenta.ApellidoPaterno + ',' + objVenta.ApellidoMaterno);
```

JELAF parte `Nombre` por las **comas** para guardar nombres y apellidos de la venta y el nombre del
comprobante. El bot mandaba `NOMBRES PATERNO MATERNO` con espacios: mismo largo (por eso la
comparación por largo no lo veía), pero sin comas JELAF no puede partirlo y deja la venta sin nombre.
Corregido en `venta_jelaf.nombre_para_jelaf` (`NOMBRES,PATERNO,MATERNO`; sin materno, coma final como en
la pantalla). La conclusión anterior ("JELAF arma la venta con el pasajero de la sesión") era incorrecta;
el `grabar-pasajero` justo antes de vender se deja porque replica a boletería y no hace daño.


## Sexta captura (12/10/2026): venta con FACTURA, sin emitir (parcial)

Boletería armó una venta con RUC y la soltó antes de emitir (`liberarAsiento`), así que **no hay
`grabar-venta` de factura**. Captura: `descubrimiento_output/venta_20261012_175255_resumen.md`
(sin datos personales; el script ahora muestra `CodiDocumento`, series y catálogos `get-tipo…`).

Lo que se confirmó:

- **Serie de factura:** `busca-correlativo` ya trae las dos, por terminal:
  `CorrelativoVentaBoleta` (`BP58-…`) y `CorrelativoVentaFactura` (`FP58-…`). También
  `CorrelativoPaseBoleta/Factura` y `CorrelativoCredito` (vacíos).
- **Consulta de RUC:** `GET itinerarios/consultar-empresa?rucContacto=<11 dígitos>` →
  `Valor = {RucCliente, RazonSocial, Direccion, Telefono}`. La cajera puede corregir la razón social
  antes de vender.
- **Ficha del cliente:** en `grabar-pasajero` van llenos `RucContacto` (11) y `RazonSocial`;
  `Direccion` con 1 carácter. El pasajero sigue con su DNI (`TipoDoc` 01).
- No apareció ningún catálogo de tipos de comprobante.

**Falta (bloquea la factura):** una captura que **termine la emisión** de una factura, para ver en
`grabar-venta` qué cambia respecto de la boleta: `CodiDocumento`, `Tipo`, `RucCliente`,
`NomEmpresaRuc`, `DirEmpresaRuc`, la serie usada y la respuesta (número `FP…` y enlace).

**Confirmado en el código de la pantalla (`codigo_jelaf/itinerarios.html`, 12/10/2026):** la venta con
factura es la misma llamada que la boleta. Por cada asiento, la pantalla manda
`RucCliente = Ruc`, `NomEmpresaRuc = RazonSocial` y `DirEmpresaRuc = Direccion` del pasajero (lo que
trajo `consultar-empresa` al dar Enter en el RUC, editable), y **`CodiDocumento` y `Tipo` siguen en
`''`**, igual que en la boleta. No se elige serie: el servidor de JELAF usa la `FP…` cuando hay RUC.
El RUC va por asiento (cada elemento de `Listado` tiene el suyo). La pantalla también tiene un botón
"Consultar a SUNAT" (`consultaSunat`, llamada aún no capturada) para RUCs que JELAF no conoce.
Pendiente menor: la forma de la respuesta de `grabar-venta` con factura (se espera igual a la de boleta).
