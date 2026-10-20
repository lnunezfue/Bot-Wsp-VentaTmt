# Plan de los tres paneles — Boletería Virtual TMT

Documento de trabajo para la petición de David: tres paneles sobre el bot de
ventas de WhatsApp. Recoge qué se pidió, qué ya está construido en esta copia
del proyecto, y qué falta hacer, con el detalle suficiente para que cualquiera
lo pueda ejecutar sin volver a preguntar.

**Estado a hoy:** los módulos, la base de datos y el tablero están hechos, y el
flujo de venta ya está instrumentado de punta a punta: una venta real deja sus
filas. Lo que falta son dos gestiones que no dependen de nosotros (plantilla de
Meta y la decisión sobre el descuento) y el endurecimiento previo al despliegue.

---

## 1. Qué pidió David

El pedido llegó por WhatsApp, en desorden. Agrupado queda así:

| Lo que dijo | Panel | Qué significa en concreto |
|---|---|---|
| "Almacenar la información de las ventas" | Ventas | Persistir cada venta: boleto, ruta, servicio, asientos, monto, medio de pago |
| "Panel de ventas, reportes" | Ventas | KPIs de boletos, ingresos, ticket promedio, conversión |
| "Rango de horas donde más se vende" | Ventas | Mapa de calor día × hora |
| "Control de errores, reporte de incidencias" | Incidencias | Registrar y clasificar los fallos, hoy invisibles |
| "Monitorear si falla en algo" | Incidencias | Estado en vivo + alarma cuando algo se cae |
| "Listado de clientes con más viajes" | Fidelización | Ranking de compradores por cantidad de viajes |
| "Top compradores" | Fidelización | Lo mismo, con gasto acumulado y segmento |
| "Códigos de descuento" | Fidelización | Crear códigos, aplicarlos en la compra |
| "Enviar un código en su siguiente compra" | Fidelización | Campaña saliente por WhatsApp |

### Una ambigüedad que había que resolver

"Rango de horas donde más se vende" son **dos preguntas distintas**:

- **Hora de compra** — cuándo el cliente paga. Sirve para saber cuándo hay que
  tener el mostrador virtual libre y cuándo conviene pautar.
- **Hora de salida** — qué turnos se llenan. Sirve para programación de buses.

No se eligió una: el panel muestra el mismo mapa con un interruptor entre las
dos, y en la base son dos columnas separadas (`venta.comprado_en` y
`venta.sale_en`). Es la diferencia entre "la gente compra de noche" y "los buses
que se llenan son los nocturnos" — dos hallazgos distintos que se confunden si
se guarda un solo campo.

### Un cuarto reporte que David no pidió y hace falta

**Venta cobrada sin boleto emitido.** El pago pasa por Yupy y JELAF falla al
confirmar. Es el único caso donde hay plata cobrada sin servicio entregado, y
hoy solo se entera el cliente cuando reclama. Tiene alarma propia en el panel de
incidencias y un estado propio en la base (`venta.estado = 'cobrada_sin_emitir'`).

---

## 2. Por qué nada de esto existía

El backend actual (`api_ventas.py`) **no tiene base de datos**. Consulta JELAF en
vivo con Playwright, devuelve el resultado al navegador y lo descarta. Los
errores salen por `print()` a la consola del servidor. El teléfono del cliente se
queda en el webhook y nunca llega a la mini-webapp de compra.

Los tres paneles no son "una pantalla más": son una capa de persistencia que hoy
no existe. Ese es el trabajo real.

---

## 3. Lo que ya está construido

Esta carpeta (`CHATBOT PANELES`) es una copia de `CHATBOT PRUEBA` con los
módulos nuevos. Todo lo agregado es **aditivo**: si se borra la carpeta
`paneles/`, el bot sigue vendiendo igual.

```
CHATBOT PANELES/
├── api_ventas.py            Backend original + 4 enganches (ya aplicados, ver §3.2)
├── jelaf_worker.py          Sin cambios
├── servidor_paneles.py      NUEVO · servidor solo-paneles para trabajar en local
├── paneles/                 NUEVO · el módulo
│   ├── esquema.sql            DDL: 9 tablas. Única fuente de verdad de la estructura
│   ├── db.py                  Conexión SQLite (una por hilo) + init_db()
│   ├── registro.py            ESCRITURA · lo que llama api_ventas.py
│   ├── consultas.py           LECTURA · una función por bloque visible del panel
│   ├── fidelizacion.py        Códigos, segmentos y envío por WhatsApp
│   ├── api_paneles.py         Router FastAPI /api/paneles/...
│   ├── seed_demo.py           Datos de ejemplo para desarrollo
│   └── boleteria.db           La base (NO se versiona — ver §8)
└── paneles_web/
    └── index.html           NUEVO · el tablero, tres pestañas, sin framework
```

### 3.1 Los módulos

**`paneles/registro.py` — capa de escritura.** Es la única que escribe. Todas
sus funciones están envueltas en `@seguro`, que atrapa cualquier excepción, la
imprime y devuelve `None`.

> **Regla irrompible del módulo:** ninguna función de registro puede tumbar una
> venta. Si la base se corrompe o el disco se llena, el cliente igual compra su
> pasaje y lo único que se pierde es el reporte. Cualquier cambio futuro
> mantiene esto.

Funciones que api_ventas.py necesita:

| Función | Cuándo se llama |
|---|---|
| `registrar_mensaje(telefono, nombre_wa)` | El cliente escribe al WhatsApp |
| `registrar_enlace_enviado(telefono)` | Se le manda el botón "Comprar Pasaje" |
| `abrir_sesion(booking_id, telefono)` | Primer contacto de ese booking con el backend |
| `actualizar_viaje(booking_id, origen, destino, …)` | El cliente eligió viaje |
| `registrar_evento(paso, booking_id, …)` | Cada paso del flujo |
| `registrar_venta(…)` | La venta se cerró (emitida o cobrada-sin-emitir) |
| `registrar_incidencia(tipo, mensaje, …)` | Cualquier fallo |
| `cerrar_sesion(booking_id, estado)` | Abandono o cierre |
| `resegmentar_todos()` | Tarea diaria (ver §6.3) |

**`paneles/consultas.py` — capa de lectura.** Solo `SELECT`. Una función por
bloque visible: `resumen_ventas`, `heatmap_horas`, `ventas_por_dia`, `rutas_top`,
`ultimas_ventas`, `embudo`, `resumen_incidencias`, `incidencias_por_tipo`,
`incidencias_abiertas`, `fallos_por_hora`, `resumen_fidelizacion`,
`top_clientes`, `distribucion_frecuencia`, `codigos`.

**`paneles/fidelizacion.py`** — crear y validar códigos, calcular el descuento,
elegir destinatarios por segmento y despachar el mensaje. El envío real está
detrás de `PANELES_ENVIO_REAL`; mientras valga `0` hace todo igual pero imprime
el mensaje en consola en vez de llamar a Meta.

**`paneles/api_paneles.py`** — el router. Todo cuelga de `/api/paneles/`:

| Método | Ruta | Para qué |
|---|---|---|
| GET | `/api/paneles/ventas?dias=7` | Panel 1 completo |
| GET | `/api/paneles/ventas/heatmap?base=compra\|salida` | El mapa de calor |
| GET | `/api/paneles/incidencias?dias=7` | Panel 2 completo (incluye estado del pool) |
| POST | `/api/paneles/incidencias/{id}/resolver` | Marcar una incidencia resuelta |
| GET | `/api/paneles/fidelizacion` | Panel 3 completo |
| POST | `/api/paneles/codigos` | Crear/editar un código |
| POST | `/api/paneles/codigos/{codigo}/estado` | Activar, pausar |
| POST | `/api/paneles/codigos/{codigo}/enviar` | A un cliente o a todo un segmento |
| GET | `/api/paneles/codigos/validar` | **Público** — lo llama la webapp de compra |
| POST | `/api/paneles/evento` | **Público** — la webapp reporta pasos del flujo |
| GET | `/api/paneles/salud` | Diagnóstico |

### 3.2 Enganches ya aplicados a `api_ventas.py`

Cuatro cambios, todos hechos y probados:

1. Import del paquete (junto al de `jelaf_worker`).
2. `paneles_db.init_db()` al arrancar, dentro de `lifespan`.
3. `configurar_paneles(estado_pool=…)` — le pasa al panel de incidencias una
   forma de leer el estado del pool sin importar `jelaf_worker` desde `paneles/`.
   Además, si el pool no arranca, se registra una incidencia `jelaf_caido`.
4. `app.include_router(router_paneles)` + `app.mount("/paneles", …)`.

---

## 4. La base de datos

SQLite, archivo `paneles/boleteria.db`, esquema en `paneles/esquema.sql`.

### 4.1 Por qué SQLite

El pool de JELAF es de tamaño 1: **el sistema vende como máximo un pasaje a la
vez**. En el peor día eso son unas pocas escrituras por minuto. SQLite en modo
WAL sobra de largo, no hay servidor que instalar ni mantener, y el esquema es
SQL estándar, así que migrar a Postgres el día que haga falta es cambiar
`db.py` y poco más. Meter un motor de base de datos ahora sería resolver un
problema que no tenemos.

### 4.2 Las nueve tablas

| Tabla | Qué guarda | Alimenta |
|---|---|---|
| `contacto` | Quién escribió al WhatsApp | Conversión, fidelización |
| `sesion_compra` | Un intento de compra = un `booking_id` | Embudo, conversión |
| `evento` | Bitácora paso a paso del flujo | Embudo |
| `venta` | Compra cerrada | Panel de ventas |
| `boleto` | Un asiento vendido a una persona | Puente venta ↔ cliente |
| `cliente` | Persona real, por documento | Fidelización |
| `incidencia` | Todo lo que hoy es un `print()` | Panel de incidencias |
| `codigo_descuento` | Definición del código | Fidelización |
| `codigo_enviado` | A quién se le mandó y si lo canjeó | Fidelización |

### 4.3 Tres decisiones del esquema que conviene entender

**`comprado_en` y `sale_en` separadas.** Ya explicado en §1. No se pueden
juntar sin perder uno de los dos reportes.

**`paso_maximo` en `sesion_compra` solo avanza.** Si el cliente vuelve atrás y
vuelve a tocar un asiento, la sesión no retrocede en el embudo. Por eso el
embudo cuenta sesiones cuyo `paso_maximo` está en ese paso *o más adelante*, en
vez de contar eventos.

**Los contadores del cliente se recalculan, no se incrementan.**
`recalcular_cliente()` vuelve a leer los boletos reales. Así una anulación o una
corrección manual no deja el contador mintiendo para siempre.

---

## 5. El flujo de venta, ya instrumentado ✅

Esta es la parte que convierte los paneles de "bonitos con datos de ejemplo" a
"reportan la realidad". **Ya está hecha.** Son ediciones quirúrgicas en
`api_ventas.py` y tres pantallas del frontend; ninguna cambia el resultado de
una venta.

### 5.1 Enganches aplicados

| # | Dónde | Qué registra |
|---|---|---|
| 1 | `recibir_mensajes` (webhook) | `registrar_mensaje` + nombre del perfil de WhatsApp; incidencia `error_interno` si el webhook revienta |
| 2 | `enviar_boton_compra` | Teléfono firmado en la URL + `registrar_enlace_enviado`; incidencia si Meta rechaza |
| 3 | `v2_iniciar_venta` | Abre la sesión, guarda ruta/servicio/placa, evento `viaje_elegido`; incidencia `pool_ocupado` en el `423` |
| 4 | `v2_seleccionar_asiento` | Evento `asiento_tocado`; incidencia `asiento_no_disponible` en el `409`; `jelaf_caido` si no abre el viaje |
| 5 | `v2_llenar_pasajero` | Evento `pasajero_lleno` |
| 6 | `generar_checkout_yupi` | Evento `pago_iniciado`; incidencia `pago_rechazado` si falla el checkout |
| 7 | `v2_confirmar_venta` | `registrar_venta` en éxito y **`cobrado_sin_emitir` en el `except`** |
| 8 | `v2_cancelar_venta` | Evento `abandono` + cierra la sesión |
| 9 | `_reaper_pool_inactivo` | Incidencia `sesion_liberada` por cada worker que suelta |
| 10 | `buscar viaje/buscar-viaje.html` | `POST /api/paneles/evento` con `enlace_abierto` + guarda el token del teléfono |
| 11 | `buscar viaje/elegir-horario.html` | Manda destino, servicio, placa y token de contacto a `/iniciar` |
| 12 | `pasarela de pago/pago.html` | Manda `medio_pago`, `referencia_pago` y `codigo_descuento` al confirmar |

### 5.2 El enganche crítico (#7) y por qué se hizo así

En el `except` de `confirmar`, el pago **ya pasó**. La venta se registra igual,
con `nro_boleto = NULL` y `estado = 'cobrada_sin_emitir'`. Si desapareciera del
panel, nadie se enteraría hasta que reclame el cliente.

Un detalle que no es obvio: los asientos se fotografían **antes** de llamar a
`pagar_y_confirmar()`, porque el `finally` ejecuta `liberar_y_resetear()`, que
vacía `asientos_en_venta`. Sin esa copia previa no quedaría con qué registrar la
venta cobrada.

Y el evento `boleto_emitido` se registra **solo** si el boleto salió de verdad.
Una venta cobrada sin emitir llegó al pago pero no lo cruzó: si contara como
emitida, el embudo diría que se entregaron boletos que no existen.

### 5.3 Dos cambios que sí tocan el flujo (y por qué eran necesarios)

**El `booking_id` ahora nace en la primera pantalla.** Antes se creaba en
`elegir-horario.html` al elegir viaje; ahora `buscar-viaje.html` lo genera al abrirse el
enlace y `elegir-horario.html` lo reutiliza. Sin esto el embudo no puede unir "abrió el
enlace" con "eligió un viaje": serían dos sesiones distintas y la conversión no
tendría denominador. Si el cliente **cambia** de viaje después de haber elegido
uno, se genera un id nuevo igual que antes — ese caso sí es otra venta para el
pool, y el comportamiento no cambió.

**`materno` se guarda en el worker.** `jelaf_worker.py` solo retenía documento,
nombres y paterno; ahora también el materno, y `pagar_y_confirmar()` devuelve
los tres campos por separado además del `pasajero` armado (que el PDF y el
frontend siguen usando igual). Era el único punto del flujo donde el backend ve
el apellido materno.

**El backend ya conoce el destino.** `PeticionV2Iniciar` recibe ahora `destino`,
`servicio`, `placa_bus` y `contacto`, todos opcionales y solo para el registro.
El origen sale de `CIUDADES_POR_CODIGO[codi_origen]`, que ya existía.

---

## 6. Las tres piezas que no dependen del código

### 6.1 El teléfono del cliente ✅ resuelto — falta una variable de entorno

Antes, `enviar_boton_compra()` mandaba una URL fija y el `booking_id` nacía en
el navegador: **no había forma de unir una venta con un contacto de WhatsApp**.

Ya está resuelto. `firmar_contacto()` / `verificar_contacto()` en
`api_ventas.py` empaquetan el teléfono en un token HMAC que viaja como `?c=` en
el enlace; `buscar-viaje.html` lo guarda en `sessionStorage` y `elegir-horario.html` lo manda
a `/iniciar`, donde el backend lo verifica y lo guarda en la sesión.

**Por qué firmado y no el teléfono en claro:** esa URL viaja por WhatsApp, queda
en el historial del navegador y se puede reenviar. Con el número a la vista,
cualquiera podría cambiarlo, atribuirse las compras de otra persona y recibir
sus códigos de descuento. La firma hace que el enlace solo sirva para el número
al que se le mandó, y caduca a la semana.

> **Pendiente:** definir `ENLACE_SECRETO` en el `.env`. Mientras no exista, el
> código usa un secreto de desarrollo escrito en el archivo — sirve para probar
> en local, **no** para producción. Debe ser una cadena aleatoria larga.

### 6.2 Plantilla de Meta — **bloquea el envío de códigos**

Fuera de la ventana de 24 h desde el último mensaje del cliente, WhatsApp no
permite texto libre: hay que usar una **plantilla de marketing aprobada**. La
aprobación la da Meta y tarda; conviene presentarla ya, en paralelo al
desarrollo.

Plantilla a presentar (4 parámetros, en este orden):

> ¡Hola {{1}}! 🚌 Gracias por viajar con Transportes Moquegua. Te dejamos {{2}}
> de descuento en tu próximo pasaje con el código {{3}}, válido hasta el {{4}}.

`fidelizacion.parametros_plantilla()` ya arma esos cuatro valores en ese orden.
Cuando la plantilla esté aprobada: poner su nombre en `META_PLANTILLA_DESCUENTO`
y `PANELES_ENVIO_REAL=1`.

### 6.3 Tarea diaria de resegmentación

Un cliente pasa a "dormido" **por el paso del tiempo**, no por una compra, así
que nadie dispara su recálculo solo. Hay que correr una vez al día:

```bash
python -c "from paneles import registro; registro.resegmentar_todos()"
```

Con el Programador de tareas de Windows basta. Sin esto, el segmento "dormidos"
—que es justamente el que justifica la campaña de descuento— nunca se llena.

---

## 7. Los descuentos: decisión de negocio pendiente

Técnicamente está resuelto. `w.llenar_pasajero()` ya manda `precio_venta` a
JELAF (`jelaf_worker.py:536`), así que el bot **puede vender más barato**:
`fidelizacion.validar_codigo()` + `aplicar_descuento()` calculan el precio final
antes de cobrar.

Pero eso mueve la contabilidad de la empresa: JELAF va a registrar una venta por
un monto menor al precio de lista. **Antes de prometerle esto a nadie hay que
acordarlo con la empresa.** Las opciones son dos:

- **Descuento en el precio JELAF.** Es lo que el código ya permite. Simple, pero
  la diferencia la absorbe la empresa y aparece en su cuadre.
- **Descuento fuera de JELAF** (reembolso, saldo a favor, pasaje gratis al
  N-ésimo viaje). No toca la contabilidad de ellos, pero hay que construir el
  mecanismo aparte.

Mientras no se decida, los códigos se crean, se envían y se registran —
simplemente nadie los canjea. El panel funciona igual.

---

## 8. Seguridad y datos personales

Tres cosas que no son opcionales:

**El `.db` no se sube.** Contiene documentos, nombres completos, teléfonos e
historial de compras de personas reales, y este repositorio es público. Ya está
en `.gitignore` (`*.db`, `*.db-wal`, `*.db-shm`). Lo que se versiona es
`esquema.sql`, no el contenido.

**El panel necesita clave antes de salir de local.** `api_paneles.py` respeta
`PANELES_TOKEN`: si la variable no está definida (el caso local), el router
queda abierto. En cuanto el tablero se publique fuera de la máquina de
desarrollo hay que definirla — sin eso, cualquiera con la URL ve todas las
ventas y los datos de los clientes. **Tarea bloqueante para el despliegue.**

**Los documentos se muestran enmascarados.** El panel enseña `•••45821`, no el
DNI completo. El dato completo está en la base para poder cruzarlo, pero no se
pasea por la pantalla.

---

## 9. Cómo correrlo en local

```bash
cd "CHATBOT PANELES"
pip install -r requirements.txt
```

Crear la base y llenarla con datos de ejemplo:

```bash
python -m paneles.db
python -m paneles.seed_demo --reset --dias 120
```

Levantar **solo los paneles** (no abre Playwright, no entra a JELAF, no necesita
credenciales ni token de Meta):

```bash
python servidor_paneles.py
```

→ `http://localhost:8010/paneles/`

Levantar **todo el bot** con los paneles dentro (necesita credenciales JELAF y
abre Chrome):

```bash
python api_ventas.py
```

→ `http://localhost:8000/paneles/`

Los datos de `seed_demo` son **inventados**: 120 días de actividad con forma
realista (más ventas de noche y los viernes, buses nocturnos más llenos,
abandonos repartidos por paso, un puñado de clientes recurrentes). Sirven para
ver los paneles funcionando y probar las consultas, no para decidir nada.

---

## 10. Plan por etapas

| Etapa | Qué incluye | Depende de | Estado |
|---|---|---|---|
| **0** | Base de datos, módulos, router, tablero, datos de ejemplo | — | ✅ Hecho |
| **1** | Enganches #1–#9 de §5.1 en `api_ventas.py` | Etapa 0 | ✅ Hecho |
| **2** | Enganches #10–#12 en el frontend: `enlace_abierto`, destino/servicio/placa, datos del pago | Etapa 1 | ✅ Hecho |
| **3** | Teléfono firmado en el enlace (§6.1) | Etapa 1 | ✅ Hecho (falta `ENLACE_SECRETO`) |
| **4** | Plantilla de Meta aprobada + `PANELES_ENVIO_REAL=1` (§6.2) | **Meta**, no nosotros | Trámite |
| **5** | Canje de descuentos en la compra (§7) | **Decisión de la empresa** | Bloqueado |
| **6** | `ENLACE_SECRETO`, `PANELES_TOKEN`, tarea diaria, despliegue (§6.3, §8) | Etapas 1–3 | Pendiente |
| **7** | Prueba en vivo contra JELAF: una venta real de punta a punta | Etapas 1–3 | Pendiente |

Con las etapas 1–3 hechas, los paneles de **ventas** e **incidencias** ya
reportan la realidad, y **fidelización** arma la lista de clientes y el
contacto; lo único que le falta es poder enviar (etapa 4).

**Lo que falta que dependa de nosotros es poco:** las tres variables de entorno,
la tarea diaria y probar una venta real contra JELAF. Todo lo demás son
decisiones de terceros.

**Orden recomendado:** empezar el trámite de Meta (etapa 4) y plantearle a la
empresa la decisión del descuento (etapa 5) **cuanto antes**, porque no dependen
de nosotros y son las que más tardan.

> ⚠️ **La etapa 7 no es opcional.** Todo lo anterior se probó con simulaciones y
> datos de ejemplo: el flujo entero se verificó llamando a las funciones de
> registro en el orden real, pero **ninguna venta pasó todavía por JELAF de
> verdad** con este código. Antes de darlo por bueno hay que vender un pasaje
> real y mirar que la fila caiga bien.

---

## 11. Preguntas abiertas para David

1. **¿Descuento sobre el precio JELAF o fuera de JELAF?** (§7) Bloquea la etapa 5.
2. **¿Quién entra al panel?** Determina si basta un token compartido o hace falta
   usuarios con contraseña.
3. **¿A dónde llega la alarma de "cobrado sin emitir"?** Lo natural es un
   WhatsApp al administrador, pero hay que saber a qué número.
4. **¿Qué es un cliente "frecuente"?** Hoy está en 4 viajes y "dormido" en 60
   días sin comprar (`paneles/registro.py`, constantes arriba del archivo). Son
   números puestos por nosotros; si la empresa tiene un criterio propio, se
   cambian ahí y listo.
5. **¿Se agregan cuentas JELAF?** El pool de tamaño 1 es el cuello de botella
   real que va a mostrar el panel de incidencias. Cada cuenta adicional es un
   mostrador virtual más, y es gestión con la empresa, no código.
