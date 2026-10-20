# Auditoría del sistema — Boletería Virtual TMT

Revisión completa del código de `CHATBOT PANELES` al 22/09/2026: backend
(`api_ventas.py`, `jelaf_worker.py`), las siete pantallas activas del flujo de
compra y el paquete `paneles/`, que también se audita aunque lo hayamos escrito
nosotros.

**Nada de esto está aplicado.** Es un documento para decidir. Cada hallazgo
tiene un código (`V1`, `S3`…) para poder referirse a él; al final hay una tabla
para marcar cuáles se aplican.

---

## Resumen

El sistema está bien pensado donde era difícil — el pool de sesiones, el
prefetch, las optimizaciones medidas en vivo — y flojo donde parecía fácil: **el
backend confía en todo lo que le dice el navegador**. El precio, el monto a
cobrar y la confirmación de que el pago pasó salen del celular del cliente, y el
servidor no verifica ninguno de los tres.

En la práctica eso significa que hoy, con conocimientos básicos, un cliente
puede **obtener un pasaje real sin pagar**, o **pagar el precio que él quiera**.
No hace falta nada sofisticado: basta un bloqueador de anuncios.

Los cinco hallazgos que no pueden esperar, en orden:

1. **V1** — Se emiten boletos reales sin verificar el pago.
2. **V2** — El precio lo decide el navegador.
3. **V3** — El bot puede vender un asiento en el bus equivocado.
4. **S2** — Cualquiera que vea el panel de incidencias puede secuestrar la venta de otro cliente.
5. **S5** — El proyecto no tiene control de versiones y el `.env` no está excluido.

**Recomendación:** no hacer una venta real con dinero real hasta cerrar la
tanda 1 (§9). El resto se puede ir decidiendo.

### Cómo leer cada hallazgo

| Campo | Qué significa |
|---|---|
| **Severidad** | **Crítica** = dinero o datos personales en riesgo con una acción al alcance de cualquier cliente. **Alta** = daño real, pero requiere una condición o un atacante con intención. **Media** = falla operativa o de calidad de datos. **Baja** = mantenimiento. |
| **Estado** | **Nuevo** = no figuraba en `known-issues-and-security.md`. **Conocido** = ya estaba anotado; se incluye porque sigue abierto. |
| **Esfuerzo** | **S** = menos de medio día. **M** = uno o dos días. **L** = tres o más, o depende de terceros. |

---

## 1. Ventas y dinero

### V1 · Se emiten boletos reales sin verificar el pago
**Crítica · Nuevo · Esfuerzo M**

`/api/v2/venta/confirmar` ([api_ventas.py:1285](../api_ventas.py)) emite el boleto
en JELAF en cuanto se le llama. No consulta a Yupy si el pago existe, por cuánto
fue, ni si corresponde a esa venta: `medio_pago` y `referencia_pago` llegan del
navegador y se guardan tal cual.

Hay dos formas de aprovecharlo, y la primera no requiere ningún conocimiento:

- **La interfaz de demostración emite boletos reales.** Si el script de Yupy no
  carga, `pago.html` muestra un formulario de tarjeta/QR simulado
  ([pago.html:247](../pasarela%20de%20pago/pago.html)). Su botón "Pagar" acepta
  cualquier número de 15 dígitos, espera dos segundos y llama a `emitirBoletos()`
  ([pago.html:300](../pasarela%20de%20pago/pago.html)). Un bloqueador de anuncios
  que filtre el dominio de Yupy basta para ver esa pantalla y salir con un pasaje
  sin haber pagado.
- **Llamar a `/confirmar` directamente.** Con el `booking_id` de una venta con
  pasajeros cargados (está en el `localStorage` del propio cliente), una línea de
  `curl` emite el boleto.

Incluso el camino "bueno" depende del navegador: el pago se da por hecho cuando
el SDK de Yupy emite `payment.reconciled` *en el celular del cliente*
([pago.html:235](../pasarela%20de%20pago/pago.html)).

Además, el formulario de demostración **pide número de tarjeta y vencimiento**.
No los envía a ningún lado, pero pedir datos de tarjeta fuera de la pasarela
certificada es un problema de cumplimiento (PCI) aunque no se guarden.

**Propuesta**
- Al crear el checkout, guardar el `yupy_transaction_id` en la sesión del
  `booking_id`, del lado del servidor.
- En `/confirmar`, antes de tocar JELAF, consultar a Yupy el estado de esa orden y
  exigir: pagada, mismo monto que el calculado por el servidor (ver V2), misma
  moneda. Si no, rechazar.
- Si Yupy ofrece notificación servidor-a-servidor (webhook), usarla como fuente de
  verdad en vez del evento del navegador.
- La interfaz de demostración solo debe existir con una variable explícita
  (`PAGO_DEMO=1`) y **nunca** en el servidor que vende de verdad. Quitar los campos
  de tarjeta.

---

### V2 · El precio lo decide el navegador
**Crítica · Nuevo · Esfuerzo M**

El precio de cada asiento viaja desde el celular y el backend lo escribe en JELAF
sin compararlo con nada:

| Paso | Dónde | Qué se confía |
|---|---|---|
| Tocar asiento | [seleccionar-asiento.html:329](../seleccionar%20asiento/seleccionar-asiento.html) → [api_ventas.py:1228](../api_ventas.py) | `precio` |
| Datos del pasajero | [datos-pasajeros.html:384](../rellenar%20datos/datos-pasajeros.html) → `llenar_pasajero` escribe `#txtPrecio` ([jelaf_worker.py:634](../jelaf_worker.py)) | `precio_venta` |
| Resumen | `/api/v1/pre-checkout` ([api_ventas.py:667](../api_ventas.py)) suma lo que manda el cliente y lo devuelve | el total |
| Pago | `/generar-checkout-yupi` ([api_ventas.py:684](../api_ventas.py)) cobra `req.monto` | el monto |

`resumen-compra.html:393` guarda ese total con el comentario *"monto total
validado por el backend"*. No lo está: el backend solo lo suma.

**Agravante — el popup de autorización se resuelve solo.** Cuando JELAF pide una
autorización durante el pago, el bot la escribe con dos claves fijas en el código
(`"1030"` o `"set11"`, [jelaf_worker.py:894](../jelaf_worker.py)). **Hay que
confirmar con la empresa qué autoriza ese popup.** Si es la aprobación de un
supervisor para vender fuera del precio de lista — que es lo que su existencia
sugiere — el bot está aprobando automáticamente justo el control que frenaría
este problema. Y esas claves están en un repositorio público.

**Propuesta**
- El servidor toma el precio de JELAF, no del cliente: `consultar_turnos_jelaf()`
  ([api_ventas.py:481](../api_ventas.py)) ya lo obtiene. Se guarda en el worker al
  seleccionar el asiento y se ignora el que manda el navegador.
- El monto de Yupy lo calcula el servidor (suma de precios de JELAF menos
  descuento válido). El frontend solo lo muestra.
- El descuento de fidelización se aplica solo del lado del servidor (ya está
  preparado en `fidelizacion.aplicar_descuento`).
- Sacar las claves de autorización a variables de entorno, y registrar como
  incidencia cada vez que aparezca el popup, con su texto. Si aparece seguido, es
  una señal que hoy nadie ve.

---

### V3 · El bot puede vender en el bus equivocado
**Crítica · Nuevo · Esfuerzo M**

`buscar_viaje()` busca en JELAF todas las salidas del origen para esa fecha
(casilla "Todos" marcada) y abre **la primera fila cuyo texto contenga la hora**
([jelaf_worker.py:449](../jelaf_worker.py)):

```python
fila_bus = page.locator(f"#tblListaItinerarios tbody tr:has-text('{hora_viaje}')").first
```

`/iniciar` solo recibe origen, fecha y hora — ni destino, ni servicio, ni
placa. Basta con que dos salidas del mismo origen compartan hora (Lima 20:30 a
Arequipa y Lima 20:30 a Tacna) para que el bot abra la otra.

Lo grave es que **no se nota**: el plano que ve el cliente sale de
`/plano-bus`, que sí usa los códigos correctos, pero el clic se hace en el bus que
abrió el worker. Los números de asiento coinciden entre buses, así que el clic
funciona y se vende un pasaje para otra ruta.

**Propuesta**
- Mandar a `/iniciar` el `id_programacion`, el destino y la placa (el frontend ya
  los tiene en `viajeSeleccionadoParaPlano`).
- Elegir la fila que coincida en hora **y** destino **y** placa, no solo hora.
- **Verificar antes de tocar el primer asiento**: leer del plano abierto la placa o
  el destino y compararlos con lo pedido. Si no coinciden, abortar con una
  incidencia. Esta verificación es lo que convierte un error silencioso en uno
  visible.

---

### V4 · El PDF del boleto se puede falsificar
**Alta · Nuevo · Esfuerzo S–M**

`/api/v1/descargar-boleto` ([api_ventas.py:1436](../api_ventas.py)) genera una
"BOLETA DE VENTA ELECTRÓNICA" con la razón social y el RUC de la empresa a partir
de **lo que venga en la petición**: número, nombre, ruta, asiento, precio. No
consulta si esa venta existe. Cualquiera puede generar un boleto de aspecto
oficial con cualquier dato.

**Propuesta**
- Generar el PDF desde la venta guardada en la base (ahora existe, gracias a los
  paneles): `GET /boleto/{nro}?t=<firma>` con un token firmado, igual que el del
  teléfono.
- Agregar un QR con una URL de verificación para que el personal de embarque
  pueda comprobar que el boleto es real.
- **Pregunta para la empresa:** ¿este PDF es el comprobante legal o una
  constancia? Si la boleta electrónica real la emite JELAF ante SUNAT, este PDF
  no debería llamarse "Boleta de venta electrónica".

---

### V5 · Números de boleto inventados y repetidos
**Alta · Nuevo · Esfuerzo S**

Tras emitir, el bot lee el número del popup de JELAF. Si no lo encuentra, **se lo
inventa**: `BP58-0000` + hora y minuto ([jelaf_worker.py:902](../jelaf_worker.py)).
Dos ventas en el mismo minuto reciben el mismo número falso, que llega al PDF del
cliente y a la base como si fuera real.

Además, el patrón toma solo la **primera** coincidencia y le asigna ese número a
**todos** los asientos de la venta ([jelaf_worker.py:925](../jelaf_worker.py)). Si
JELAF emite un boleto por asiento — lo normal —, del segundo al quinto pasajero
reciben un número que no es el suyo.

**Propuesta**
- Nunca inventar: si no se pudo leer el número, registrar la venta como
  `emitida_sin_numero` y crear una incidencia para conciliarla a mano.
- Leer todos los números (`re.findall`) y asignarlos por asiento, verificando que
  la cantidad coincida.

---

### V6 · El pago se registra en JELAF como una tarjeta falsa
**Media · Nuevo · Esfuerzo L (depende de la empresa)**

Para cerrar la venta, el bot elige en JELAF "Tarjeta de crédito · VISA" y escribe
`0000-0000-0000-` más cuatro dígitos al azar ([jelaf_worker.py:731](../jelaf_worker.py)).
Cada venta online entra a los libros de la empresa como un pago con una tarjeta
que no existe, sin ningún vínculo con la transacción de Yupy. **Cruzar lo que
depositó Yupy con lo que registró JELAF es imposible.**

**Propuesta:** pedir a la empresa un tipo de pago propio en JELAF ("Online /
Yupy") o, como mínimo, escribir el `yupy_transaction_id` en el campo de número
de operación. Es una decisión contable de ellos, no nuestra.

---

## 2. Seguridad y datos personales

### S1 · `consultar-cliente` entrega datos personales a quien pregunte
**Crítica · Nuevo · Esfuerzo S**

`/api/v1/consultar-cliente` ([api_ventas.py:546](../api_ventas.py)) devuelve
nombre, apellidos, **fecha de nacimiento y sexo** de cualquier persona que haya
comprado alguna vez a la empresa, a partir de su DNI. No pide autenticación ni
tiene límite de consultas. Su propio comentario dice que es *"segura de llamar en
cada pulsación de teclado"*: segura para JELAF, no para las personas.

Cualquiera puede recorrer DNIs y armarse una base con los datos de los clientes
de la empresa, lo que choca con la Ley 29733 de Protección de Datos Personales.

**Propuesta**
- Exigir una venta en curso: `booking_id` con worker asignado.
- Límite de consultas por venta (5 pasajeros → unas 10 consultas) y por IP.
- Registrar cada consulta. Valorar si la fecha de nacimiento y el sexo tienen que
  salir del servidor o basta con prellenar el nombre.

---

### S2 · Se puede secuestrar la venta de otro cliente
**Crítica · Nuevo · Esfuerzo S**

El único "permiso" para operar una venta es conocer su `booking_id`. Es un UUID
imposible de adivinar, pero **el sistema lo publica** en dos lugares sin clave:

- `GET /api/v2/venta/estado` ([api_ventas.py:1428](../api_ventas.py)) — `owner_token`
  de cada worker.
- `GET /api/paneles/incidencias` — el mismo dato, y el panel está abierto
  mientras no se defina `PANELES_TOKEN` (ver P1).

Con ese id, cualquiera puede cancelar la venta de otra persona (dejarla sin sus
asientos), cambiarle los datos del pasajero o confirmarla.

**Propuesta:** no devolver nunca el `booking_id` completo fuera del backend
(mostrar solo los primeros caracteres), y proteger `/api/v2/venta/estado` igual
que los paneles.

---

### S3 · Una sola persona puede bloquear todas las ventas
**Alta · Nuevo · Esfuerzo S–M**

Con una cuenta JELAF hay un solo mostrador virtual. `/iniciar` lo reserva para
cualquier `booking_id`, sin exigir que venga de un enlace de WhatsApp, y el
heartbeat lo mantiene tomado **indefinidamente**: el timeout de inactividad se
reinicia con cada latido. Un script de pocas líneas deja a la empresa sin ventas
por el bot el tiempo que quiera. También lo hace, sin mala intención, un cliente
que deja la pestaña abierta.

**Propuesta**
- `/iniciar` exige un token de contacto válido (`?c=`, el que ya firmamos): solo
  entra quien recibió el enlace por WhatsApp.
- Una sesión activa por teléfono a la vez.
- **Duración máxima absoluta** de una venta (p. ej. 20 minutos), con o sin
  heartbeat.

---

### S4 · Endpoints viejos siguen expuestos, y uno bloquea inventario real
**Alta · Conocido en parte · Esfuerzo S**

La versión v1 del flujo ya no se usa desde el frontend, pero sus endpoints
siguen activos:

| Endpoint | Riesgo |
|---|---|
| `POST /api/v1/bloquear-asiento` | Bloquea asientos **reales** en JELAF. Se puede dejar un bus entero sin asientos disponibles para la boletería física. |
| `POST /api/v1/liberar-asiento` (v1) | Libera cualquier `id_bloqueo`. Si son correlativos, se pueden soltar bloqueos que no son nuestros. |
| `POST /api/v1/confirmar-compra` | ~340 líneas. Abre un Chrome nuevo por llamada: unas cuantas llamadas seguidas agotan la memoria del servidor. |

**Propuesta:** eliminarlos. Quedan en el historial de git (ver S5) si algún día
hicieran falta. De paso, el archivo baja de ~1 700 a ~1 250 líneas.

---

### S5 · Sin control de versiones, y el `.env` no está excluido
**Crítica · Nuevo · Esfuerzo S**

`CHATBOT PANELES` **no tiene repositorio git propio**. Al copiarlo desde
`CHATBOT PRUEBA` se quitó la carpeta `.git` — fue una decisión nuestra en la
sesión anterior y quedó mal resuelta —. Hoy git lo trata como parte de un
repositorio vacío que existe en `C:\Users\ASUS`, **la carpeta personal completa
de Windows**.

Dos consecuencias:

- **No hay forma de revisar ni revertir** los cambios que se apliquen a partir de
  esta auditoría.
- **`.gitignore` no excluye `.env`**. Un `git add .` hecho desde la carpeta
  equivocada metería al repositorio los secretos del proyecto… o el perfil
  entero de Windows.

**Propuesta, antes de aplicar cualquier otro cambio:**

1. Agregar `.env` al `.gitignore` del proyecto.
2. `git init` dentro de `CHATBOT PANELES` y un primer commit con el estado
   actual, revisando que no entre el `.env` ni la base `.db`.
3. Revisar el repositorio vacío de `C:\Users\ASUS` (no tiene commits) y
   eliminarlo si nadie lo creó a propósito.

---

### S6 · Secretos escritos en el código
**Crítica · Conocido en parte · Esfuerzo S**

`known-issues-and-security.md` ya pedía rotar las credenciales y sacar los
valores por defecto. Sigue igual, y hay secretos nuevos:

| Secreto | Dónde | Estado |
|---|---|---|
| Usuario y clave JELAF (`613` / `3976`) | [api_ventas.py:54](../api_ventas.py) | Conocido |
| `client_id` / `client_secret` de Yupy | [api_ventas.py:692](../api_ventas.py) | Conocido |
| `META_VERIFY_TOKEN` por defecto | [api_ventas.py:1647](../api_ventas.py) | Nuevo |
| Claves de autorización de JELAF (`1030`, `set11`) | [jelaf_worker.py:894](../jelaf_worker.py) | Nuevo |
| Secreto de firma del enlace, con valor de desarrollo | [api_ventas.py:1513](../api_ventas.py) | Nuevo (lo pusimos nosotros) |

**Propuesta:** un módulo `config.py` que lea todas las variables y **detenga el
arranque** si falta alguna obligatoria, en vez de caer en silencio a un valor real.

---

### S7 · El webhook no valida firma ni duplicados
**Alta · Conocido · Esfuerzo S**

Sin cambios desde que se anotó. Cualquiera que conozca la URL puede hacer que el
bot mande el botón de compra a cualquier número (gasta la cuota de mensajes de la
empresa), y los reintentos de Meta duplican envíos.

**Propuesta:** validar `X-Hub-Signature-256` con el *app secret* y descartar
`message.id` ya vistos (una tabla de la misma base basta).

---

### S8 · Todo expuesto en la red local
**Media · Nuevo · Esfuerzo S**

`uvicorn.run(host="0.0.0.0")` ([api_ventas.py:1700](../api_ventas.py)) publica el
backend en toda la red a la que esté conectada la computadora: cualquiera en el
mismo wifi ve `/paneles/` (datos de clientes) y `/docs` (la lista de todos los
endpoints). CORS acepta cualquier origen (`allow_origins=["*"]`).

**Propuesta:** en desarrollo, `127.0.0.1`. En producción, CORS limitado al dominio
de la webapp y `/docs` desactivado o protegido.

---

## 3. Correctitud y concurrencia

### C1 · El reaper puede liberar una venta en pleno pago
**Alta · Nuevo · Esfuerzo S**

`liberar_inactivos()` ([jelaf_worker.py:1008](../jelaf_worker.py)) libera
cualquier worker inactivo **sin mirar si está trabajando**: no revisa el
`lock`, y `liberar_y_resetear()` navega la página fuera de la venta.
`pagar_y_confirmar()` solo marca actividad al terminar.

Escenario: el cliente sale a la app del banco y vuelve a los 9 minutos y 50
segundos. Se dispara la confirmación, que tarda unos 15 segundos; a mitad de
camino el reaper ve más de 10 minutos de silencio y navega la página. El boleto
queda a medio emitir, en un estado que nadie conoce.

**Propuesta:** el reaper salta los workers con el `lock` tomado; cada operación
marca actividad **al empezar**; la liberación se hace siempre bajo el `lock`.

---

### C2 · Nadie verifica que la venta siga siendo tuya al tomar el lock
**Alta · Nuevo · Esfuerzo S**

Los endpoints buscan el worker del `booking_id` y **después** esperan el `lock`.
Mientras esperan, el worker puede liberarse y pasar a otra venta, y el endpoint
sigue igual.

Escenario concreto, sin nada raro: el cliente toca "Pagar" dos veces. La segunda
llamada espera; la primera emite y libera el worker; la segunda entra, corre
`pagar_y_confirmar()` sobre un worker vacío, falla, y **registra una "venta
cobrada sin emitir" falsa**, con cero asientos, que dispara la alarma crítica
del panel.

**Propuesta:** dentro del `lock`, volver a comprobar `w.es_dueno(booking_id)`.
Hacer `/confirmar` idempotente: si ese `booking_id` ya tiene una venta emitida,
devolver la misma respuesta en vez de reintentar.

---

### C3 · El pasajero puede terminar en el asiento equivocado
**Alta · Nuevo · Esfuerzo S**

Para ubicar la pestaña de un asiento, `_activar_pestana_de_asiento()` compara por
**contenido de texto** ([jelaf_worker.py:569](../jelaf_worker.py)):

```python
if asiento_str in texto_tab or str(int(asiento_str)) in texto_tab:
```

Con los asientos 1 y 12 en la misma venta, buscar "1" coincide con la pestaña
"12" si aparece antes. Los datos del pasajero del asiento 1 se escriben en el 12.

**Propuesta:** comparar el número exacto (expresión regular con límites de
palabra, o comparación de enteros).

---

### C4 · Sin conciliación cuando algo falla al emitir
**Alta · Nuevo · Esfuerzo M**

Cualquier excepción en `/confirmar` se registra como "cobrado sin emitir". Pero
la excepción puede ocurrir **después** de que JELAF emitió — por ejemplo, si el
botón de cierre tarda más de lo previsto. En ese caso el boleto existe y el panel
dice que no. Si alguien lo emite a mano para "arreglarlo", el asiento queda
vendido dos veces.

**Propuesta:** ante un fallo, consultar `consultar_turnos_jelaf()` y comprobar si
los asientos quedaron vendidos a esos documentos. Clasificar como emitida, no
emitida o **por conciliar** — nunca asumir.

---

### C5 · La sesión de JELAF no se renueva sola
**Media · Conocido en parte · Esfuerzo M**

La búsqueda de viajes, el plano y la consulta de DNI usan `sesion_global_jelaf`:
una sesión HTTP con las cookies de un login hecho **una vez** al arrancar
([api_ventas.py:138](../api_ventas.py)). Si JELAF la expira, esas tres funciones
fallan con error 500 hasta reiniciar el backend. Los workers tienen keepalive
(bien); esta sesión no. Tampoco hay forma de reemplazar un worker cuyo Chrome se
cerró.

**Propuesta:** ante una respuesta de sesión expirada, reloguear y reintentar una
vez. Si un worker falla su keepalive varias veces seguidas, recrearlo.

---

## 4. Rendimiento y capacidad

### R1 · El mostrador único es el techo de ventas
**Alta · Nuevo · Esfuerzo S (técnico) + L (empresa)**

Es el límite real del negocio, más que cualquier optimización. El worker se
reserva cuando el cliente **elige horario** (para adelantar la búsqueda) y lo
retiene mientras mira el plano, llena datos y paga. Durante esos minutos, nadie
más puede comprar; los demás esperan 15 segundos y reciben "intenta de nuevo".

Palancas, de más barata a más cara:

1. **`JELAF_WORKERS_POR_CUENTA=2`**: el código ya lo soporta
   ([api_ventas.py:83](../api_ventas.py)). Duplica la capacidad sin trámites, **si**
   JELAF admite dos sesiones del mismo usuario sin cruzar asientos. Hay que
   probarlo en vivo antes.
2. **Prefetch condicional:** adelantar la búsqueda solo si queda al menos otro
   worker libre. Con uno solo, reservarlo recién al tocar el primer asiento. Se
   pierden unos segundos en el primer toque, pero el mostrador no se queda
   ocupado por quien solo está mirando horarios.
3. **Tope absoluto de duración** (S3) y un mensaje honesto de espera en vez de
   "intenta de nuevo".
4. **Más cuentas JELAF**, que es gestión con la empresa.

Para decidir con datos: el panel de incidencias ya cuenta los rechazos por
`pool_ocupado`. Unas semanas de datos reales dicen cuánto se está perdiendo.

---

### R2 · Llamadas externas sin tiempo límite, y trabajo repetido
**Media · Nuevo · Esfuerzo S**

- Las tres llamadas a Yupy y el envío del botón a Meta no tienen `timeout`
  ([api_ventas.py:695](../api_ventas.py), [:718](../api_ventas.py),
  [:725](../api_ventas.py), [:1622](../api_ventas.py)). Si uno de esos servicios
  se cuelga, la petición queda esperando para siempre y ocupa un hilo del
  servidor.
- El token de Yupy se pide de nuevo en **cada** checkout. Dura un tiempo;
  guardarlo hasta que venza ahorra una llamada por venta.
- `cargar_planos_maestros()` lee y parsea todos los JSON de planos **en cada
  búsqueda** ([api_ventas.py:96](../api_ventas.py)). Cambian una vez al mes:
  cargarlos al arrancar.
- `buscar-viajes` se consulta a JELAF dos veces por cliente (una por pantalla).
  Una caché de 30–60 segundos por origen y fecha lo reduce a una.

---

### R3 · Un Chrome de más al arrancar
**Baja · Nuevo · Esfuerzo S**

`obtener_sesion_jelaf()` abre un Chrome completo, hace login y lo cierra, solo
para quedarse con las cookies ([api_ventas.py:138](../api_ventas.py)). Los workers
hacen su propio login un momento después. Se pueden tomar las cookies del
contexto de un worker: un login menos, un arranque más rápido y una sesión JELAF
menos en uso.

---

## 5. Paneles (autoauditoría)

Hallazgos en el código que escribimos en las sesiones anteriores.

### P1 · El tablero no envía la clave: activarla lo rompe
**Alta · Nuevo · Esfuerzo S**

`api_paneles.py` exige `PANELES_TOKEN` si está definido, pero `paneles_web/index.html`
**nunca envía** la cabecera `X-Paneles-Token`
([index.html:423](../paneles_web/index.html)). El día que se proteja el panel,
el tablero deja de cargar. Es decir: tal como está, la única forma de que el
panel funcione es dejarlo abierto.

**Propuesta:** pantalla de ingreso simple que guarde la clave y la envíe en cada
petición (o una cookie de sesión). Comparar con `hmac.compare_digest`, no con `!=`.

---

### P2 · `/api/paneles/evento` acepta cualquier teléfono
**Media · Nuevo · Esfuerzo S**

El endpoint público que usa la webapp acepta un campo `telefono` libre
([api_paneles.py:209](../paneles/api_paneles.py)) y lo asocia a la sesión.
Cualquiera puede asignar números ajenos a sesiones y ensuciar la lista de
fidelización, que es justamente a quién se le mandan mensajes. El frontend ni
siquiera usa ese campo.

**Propuesta:** quitarlo. El teléfono solo entra por el token firmado.

---

### P3 · Una sesión retomada queda como "abandonada"
**Media · Nuevo · Esfuerzo S**

Si el cliente vuelve atrás desde los asientos, se cancela la sesión (queda
`abandonada`). Si luego elige el mismo viaje, se reutiliza el mismo `booking_id`
— el cambio que hicimos para el embudo — pero `abrir_sesion()`
([registro.py:138](../paneles/registro.py)) no restablece el estado. Hasta que la
venta se cierre, el panel la muestra abandonada.

**Propuesta:** al reabrir, volver a `en_curso` y borrar `cerrada_en`.

---

### P4 · Zona horaria: en un servidor en la nube, el mapa de calor se corre 5 horas
**Media · Nuevo · Esfuerzo S**

Todas las horas se guardan con `datetime.now()` ([registro.py:59](../paneles/registro.py)),
la hora local de la máquina. En la PC de desarrollo es la de Perú. En un servidor
en la nube suele ser UTC: el pico de las 20:00 aparecería a la 01:00.

**Propuesta:** fijar `America/Lima` con `zoneinfo` y guardar con desfase explícito.

---

### P5 · Si el registro falla, nadie se entera
**Media · Nuevo · Esfuerzo S**

`@seguro` ([registro.py:63](../paneles/registro.py)) atrapa todo error para que
el registro nunca tumbe una venta — correcto —, pero solo lo imprime en
consola. Si la base deja de escribir, los paneles se quedan quietos en silencio,
y el panel de incidencias no puede reportar su propia falla.

**Propuesta:** contar los fallos en memoria y mostrarlos en `/api/paneles/salud`
y como aviso en el tablero ("el registro falló N veces desde las 14:32").

---

### P6 · Ventas registradas sin atomicidad
**Media · Nuevo · Esfuerzo S**

`registrar_venta()` hace diez o quince escrituras con un `commit` cada una. Si el
proceso se corta a mitad, queda una venta sin boletos, o boletos sin cliente.

**Propuesta:** una sola transacción por operación de registro.

---

### P7 · Otros menores

| Qué | Propuesta |
|---|---|
| Datos de ejemplo y datos reales conviven en la misma base; borrarlos es manual. | `seed_demo` usa por defecto otro archivo (`boleteria_demo.db`) y se niega a correr sobre una base con ventas reales. |
| `enviar_campana()` manda los mensajes uno por uno dentro de la petición HTTP: con 100 clientes y 15 s de *timeout* puede quedar colgada minutos. | Tarea en segundo plano, con estado consultable. |
| `/codigos/validar` es público y dice *"ese código no es para tu caso"*: revela el segmento del cliente y permite probar códigos. | Límite de intentos y un mensaje único para todo rechazo. |
| La clave del cliente es solo el número de documento: un pasaporte y un DNI con el mismo número se mezclan. | Clave compuesta tipo + número. |

---

## 6. Mantenibilidad

| Código | Hallazgo | Propuesta | Esfuerzo |
|---|---|---|---|
| **M1** | `api_ventas.py` tiene 1 700 líneas, ~450 de código muerto (v1). | Tras quitar v1 (S4), separar en routers: catálogo, venta, pagos, WhatsApp, boletos. | M |
| **M2** | Constantes duplicadas entre archivos (`URL_BASE`, `CIUDADES_POR_CODIGO`). `API_BASE_URL` fija en seis HTML *(conocido)*. | Un `config.py` y un `config.js`. | S |
| **M3** | Todo el registro es `print()`, sin forma de seguir una venta concreta en los logs. | `logging` con el `booking_id` en cada línea, a un archivo rotado. | S |
| **M4** | No hay ni una prueba automática. | Pruebas para lo que no depende de JELAF: firma de tokens, cálculo de precio y descuento, embudo. Para el RPA, una lista de verificación manual antes de cada despliegue. | M |

---

## 7. Producto

No son fallas. Son mejoras que el sistema tiene casi listas, y que mueven la
conversión más que cualquier optimización técnica.

| Código | Idea | Por qué ahora |
|---|---|---|
| **U1** | **Responder a cualquier mensaje**, no solo a "comprar". Hoy quien escribe "hola", "precio" u "horarios" no recibe respuesta. | Es la primera impresión del canal, y cuesta una línea. |
| **U2** | **Mandar la confirmación y el boleto por WhatsApp** al terminar la compra. | Estamos dentro de la ventana de 24 h (el cliente acaba de escribir): no hace falta plantilla. Reduce el "¿se compró o no?". |
| **U3** | **Recuperar carritos abandonados**: a quien llegó al pago y se fue, mandarle un recordatorio. | Los paneles ya guardan el teléfono y el paso donde se quedó. |
| **U4** | **Precio por piso** en buses de dos pisos *(conocido: el "S/ NaN")*. Hoy se aplica el precio del primer asiento a todo el bus. | Se resuelve junto con V2. |
| **U5** | Verificar si `pagehide` cancela la venta cuando el cliente cambia de app en iPhone. | Si pasa, pierde sus asientos por ir a copiar su DNI. Hay que probarlo en un iPhone real. |

---

## 8. Lo que está bien

Para que las decisiones no se tomen solo mirando lo malo:

- **El pool de sesiones persistentes** resolvió de verdad el problema de fondo de
  v1 (reconstruir la venta al final) y está bien documentado.
- **Las optimizaciones de `buscar_viaje()` están medidas**, no adivinadas: se
  reemplazó cada espera fija por una condición real, con los tiempos anotados.
- **El prefetch** y el **pago adelantado** muestran buen criterio de producto:
  atacan exactamente las esperas que siente el cliente.
- La documentación es inusualmente buena para un proyecto de este tamaño, y
  `known-issues-and-security.md` es honesto.
- La venta "cobrada sin emitir" ya se registra y tiene alarma. Falta hacerla
  confiable (C2, C4), pero existe.

---

## 9. Plan sugerido

| Tanda | Qué | Por qué juntas | Esfuerzo total |
|---|---|---|---|
| **0 · Antes de tocar nada** | S5 (repositorio y `.env`), rotar credenciales (S6) | Sin esto no se puede revisar ni revertir ningún cambio. | ½ día |
| **1 · Condición para vender** | V1, V2, V3, V5, C1, C2, S2, S4 | Todo lo que hoy permite perder dinero o vender mal. | 4–6 días |
| **2 · Datos y abuso** | S1, S3, S6, S7, S8, C3, C4, V4, P1, P2 | Datos personales y bloqueo del servicio. | 3–4 días |
| **3 · Operación** | C5, R1, R2, R3, P3–P7, V6 | Estabilidad y capacidad en el día a día. | 3–5 días |
| **4 · Producto y orden** | U1–U5, M1–M4 | Conversión y facilidad para seguir trabajando. | Variable |

La tanda 1 es la única que recomiendo **no negociar** antes de la primera venta
con dinero real.

---

## 10. Preguntas para la empresa

1. **¿Qué autoriza el popup que el bot resuelve con las claves `1030` / `set11`?**
   ¿Quién las entregó y quién más las conoce? (V2)
2. **¿Existe en JELAF un tipo de pago para ventas online**, o se puede crear uno?
   Hoy todo entra como una tarjeta VISA que no existe. (V6)
3. **¿El PDF que descarga el cliente es el comprobante legal** o solo una
   constancia? (V4)
4. **¿JELAF admite dos sesiones simultáneas con el mismo usuario?** Si sí, se
   duplica la capacidad sin trámites. (R1)
5. **¿Qué se hace hoy con una venta cobrada y no emitida?** ¿Se emite a mano o se
   devuelve el dinero? Define cómo tiene que funcionar la conciliación. (C4)

---

## 11. Decisión

Para marcar qué se aplica. Los códigos remiten a las secciones de arriba.

| Código | Hallazgo | Severidad | Esfuerzo | ¿Aplicar? |
|---|---|---|---|---|
| S5 | Sin repositorio propio; `.env` no excluido | Crítica | S | |
| V1 | Boletos emitidos sin verificar el pago | Crítica | M | |
| V2 | El precio lo decide el navegador | Crítica | M | |
| V3 | Venta en el bus equivocado | Crítica | M | |
| S1 | `consultar-cliente` expone datos personales | Crítica | S | |
| S2 | Secuestro de ventas ajenas | Crítica | S | |
| S6 | Secretos en el código | Crítica | S | |
| V4 | PDF de boleto falsificable | Alta | S–M | |
| V5 | Números de boleto inventados y repetidos | Alta | S | |
| S3 | Bloqueo del único mostrador | Alta | S–M | |
| S4 | Endpoints v1 expuestos | Alta | S | |
| S7 | Webhook sin firma ni deduplicación | Alta | S | |
| C1 | Reaper libera en pleno pago | Alta | S | |
| C2 | Sin verificación de dueño tras el lock | Alta | S | |
| C3 | Pasajero en el asiento equivocado | Alta | S | |
| C4 | Sin conciliación tras fallo al emitir | Alta | M | |
| R1 | Capacidad: el mostrador único | Alta | S + L | |
| P1 | El tablero no envía la clave | Alta | S | |
| V6 | Pago registrado como tarjeta falsa | Media | L | |
| S8 | Backend expuesto en la red local | Media | S | |
| C5 | Sesión JELAF sin renovación | Media | M | |
| R2 | Sin *timeouts*; trabajo repetido | Media | S | |
| P2 | `/evento` acepta cualquier teléfono | Media | S | |
| P3 | Sesión retomada queda abandonada | Media | S | |
| P4 | Zona horaria | Media | S | |
| P5 | Fallos de registro silenciosos | Media | S | |
| P6 | Registro sin atomicidad | Media | S | |
| P7 | Menores de paneles | Baja | S | |
| R3 | Un Chrome de más al arrancar | Baja | S | |
| M1–M4 | Mantenibilidad | Baja | S–M | |
| U1–U5 | Producto | — | S–M | |
