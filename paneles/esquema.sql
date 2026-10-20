-- ============================================================================
--  Boleteria Virtual TMT - esquema de los tres paneles
--  SQLite. Se crea/actualiza solo desde paneles/db.py -> init_db().
--
--  Regla de oro: NADA de esto puede hacer fallar una venta. Todas las
--  escrituras pasan por paneles/registro.py, que traga sus propios errores.
--  Si la base se cae, el bot sigue vendiendo; solo se pierde el reporte.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- CONTACTO: quien escribio al WhatsApp de la empresa.
-- Es lo primero que sabemos de un cliente, antes de que compre nada.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS contacto (
    telefono         TEXT PRIMARY KEY,          -- formato Meta: 51987654321
    nombre_wa        TEXT,                      -- nombre del perfil de WhatsApp
    primer_mensaje   TEXT NOT NULL,             -- ISO-8601
    ultimo_mensaje   TEXT NOT NULL,
    mensajes         INTEGER NOT NULL DEFAULT 1,
    enlaces_enviados INTEGER NOT NULL DEFAULT 0
);

-- ---------------------------------------------------------------------------
-- SESION_COMPRA: un intento de compra = un booking_id.
-- Existe aunque el cliente abandone. Es la fila sobre la que se calculan el
-- embudo del panel de incidencias y la conversion del panel de ventas.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sesion_compra (
    booking_id     TEXT PRIMARY KEY,
    telefono       TEXT,
    abierta_en     TEXT NOT NULL,
    actualizada_en TEXT NOT NULL,
    cerrada_en     TEXT,

    -- viaje elegido (se conoce recien en el paso 2)
    origen         TEXT,
    destino        TEXT,
    fecha_viaje    TEXT,        -- YYYY-MM-DD
    hora_viaje     TEXT,        -- HH:MM
    servicio       TEXT,
    placa_bus      TEXT,

    -- estado del intento
    paso_maximo    TEXT NOT NULL DEFAULT 'enlace_abierto',
    estado         TEXT NOT NULL DEFAULT 'en_curso',
                   -- en_curso | abandonada | emitida | fallida | rechazada
    asientos       TEXT         -- JSON con el ultimo estado conocido
);
CREATE INDEX IF NOT EXISTS ix_sesion_abierta  ON sesion_compra(abierta_en);
CREATE INDEX IF NOT EXISTS ix_sesion_telefono ON sesion_compra(telefono);
CREATE INDEX IF NOT EXISTS ix_sesion_estado   ON sesion_compra(estado);

-- ---------------------------------------------------------------------------
-- EVENTO: bitacora del flujo, un renglon por paso alcanzado.
-- El embudo cuenta sesiones distintas por paso, no eventos.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS evento (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id  TEXT,
    telefono    TEXT,
    paso        TEXT NOT NULL,
                -- mensaje_recibido | enlace_enviado | enlace_abierto |
                -- viaje_elegido | asiento_tocado | asiento_liberado |
                -- pasajero_lleno | pago_iniciado | pago_confirmado |
                -- boleto_emitido | abandono
    ocurrido_en TEXT NOT NULL,
    detalle     TEXT           -- JSON libre
);
CREATE INDEX IF NOT EXISTS ix_evento_tiempo  ON evento(ocurrido_en);
CREATE INDEX IF NOT EXISTS ix_evento_paso    ON evento(paso, ocurrido_en);
CREATE INDEX IF NOT EXISTS ix_evento_booking ON evento(booking_id);

-- ---------------------------------------------------------------------------
-- CLIENTE: persona real, identificada por su documento (DNI/CE/pasaporte).
-- Es la tabla del panel de fidelizacion. Los contadores se recalculan en
-- cada venta emitida (ver registro.recalcular_cliente).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cliente (
    documento       TEXT PRIMARY KEY,
    tipo_documento  TEXT NOT NULL DEFAULT '01',   -- 01 DNI, 02 pasaporte, 03 CE, 04 RUC
    nombres         TEXT,
    paterno         TEXT,
    materno         TEXT,
    telefono        TEXT,          -- ultimo telefono con el que compro
    primera_compra  TEXT,
    ultima_compra   TEXT,
    total_viajes    INTEGER NOT NULL DEFAULT 0,
    total_gastado   REAL    NOT NULL DEFAULT 0,
    segmento        TEXT NOT NULL DEFAULT 'nuevo'
                    -- nuevo | ocasional | frecuente | dormido
);
CREATE INDEX IF NOT EXISTS ix_cliente_viajes   ON cliente(total_viajes DESC);
CREATE INDEX IF NOT EXISTS ix_cliente_segmento ON cliente(segmento);
CREATE INDEX IF NOT EXISTS ix_cliente_ultima   ON cliente(ultima_compra);

-- ---------------------------------------------------------------------------
-- VENTA: una compra cerrada. Puede llevar hasta 5 boletos.
--
-- comprado_en y sale_en son campos SEPARADOS a proposito: el panel de ventas
-- pregunta "a que hora compran" (comprado_en) y "que turnos se llenan"
-- (sale_en). Son dos reportes distintos sobre la misma fila.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS venta (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id        TEXT,
    nro_boleto        TEXT,        -- el que devuelve JELAF; NULL si se cobro y no se emitio
    telefono          TEXT,

    origen            TEXT NOT NULL,
    destino           TEXT NOT NULL,
    servicio          TEXT,
    placa_bus         TEXT,

    comprado_en       TEXT NOT NULL,   -- ISO-8601, hora de la compra
    sale_en           TEXT,            -- ISO-8601, fecha + hora de salida del bus
    cantidad_asientos INTEGER NOT NULL DEFAULT 1,

    monto_bruto       REAL NOT NULL DEFAULT 0,   -- precio de lista JELAF
    monto_descuento   REAL NOT NULL DEFAULT 0,
    monto_total       REAL NOT NULL DEFAULT 0,   -- lo que realmente se cobro
    codigo_descuento  TEXT,

    medio_pago        TEXT,        -- yupy_tarjeta | yupy_qr | ...
    referencia_pago   TEXT,        -- id de operacion de la pasarela

    estado            TEXT NOT NULL DEFAULT 'emitida'
                      -- emitida | cobrada_sin_emitir | por_conciliar | anulada | simulada
);
CREATE INDEX IF NOT EXISTS ix_venta_compra ON venta(comprado_en);
CREATE INDEX IF NOT EXISTS ix_venta_salida ON venta(sale_en);
CREATE INDEX IF NOT EXISTS ix_venta_ruta   ON venta(origen, destino);
CREATE INDEX IF NOT EXISTS ix_venta_estado ON venta(estado);

-- ---------------------------------------------------------------------------
-- BOLETO: un asiento vendido a una persona. Puente entre venta y cliente.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS boleto (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    venta_id       INTEGER NOT NULL REFERENCES venta(id) ON DELETE CASCADE,
    documento      TEXT,
    tipo_documento TEXT DEFAULT '01',
    nombres        TEXT,
    paterno        TEXT,
    materno        TEXT,
    asiento        TEXT NOT NULL,
    precio         REAL NOT NULL DEFAULT 0
    -- nro_boleto, link_comprobante TEXT: agregadas por db.init_db() (_COLUMNAS_NUEVAS)
);
CREATE INDEX IF NOT EXISTS ix_boleto_venta ON boleto(venta_id);
CREATE INDEX IF NOT EXISTS ix_boleto_doc   ON boleto(documento);

-- ---------------------------------------------------------------------------
-- INCIDENCIA: todo lo que hoy solo sale por print() en la consola.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS incidencia (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id       TEXT,
    telefono         TEXT,
    tipo             TEXT NOT NULL,
                     -- cobrado_sin_emitir | fallo_emision | asiento_no_disponible |
                     -- pool_ocupado | pago_rechazado | sesion_liberada |
                     -- jelaf_caido | error_interno
    severidad        TEXT NOT NULL,      -- critica | grave | leve
    mensaje          TEXT NOT NULL,
    contexto         TEXT,               -- JSON
    ocurrido_en      TEXT NOT NULL,
    resuelta         INTEGER NOT NULL DEFAULT 0,
    resuelta_en      TEXT,
    nota_resolucion  TEXT
);
CREATE INDEX IF NOT EXISTS ix_inc_tiempo  ON incidencia(ocurrido_en);
CREATE INDEX IF NOT EXISTS ix_inc_tipo    ON incidencia(tipo, ocurrido_en);
CREATE INDEX IF NOT EXISTS ix_inc_abierta ON incidencia(resuelta, severidad);

-- ---------------------------------------------------------------------------
-- CODIGO_DESCUENTO / CODIGO_ENVIADO: la campana de fidelizacion.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS codigo_descuento (
    codigo            TEXT PRIMARY KEY,
    descripcion       TEXT,
    tipo              TEXT NOT NULL DEFAULT 'porcentaje',  -- porcentaje | monto
    valor             REAL NOT NULL,
    segmento_objetivo TEXT,          -- frecuente | dormido | ocasional | nuevo | NULL = cualquiera
    vence_en          TEXT,          -- YYYY-MM-DD
    usos_por_cliente  INTEGER NOT NULL DEFAULT 1,
    tope_descuento    REAL,          -- soles maximos por boleto, NULL = sin tope
    estado            TEXT NOT NULL DEFAULT 'borrador',    -- borrador | activo | pausado | vencido
    creado_en         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS codigo_enviado (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    codigo       TEXT NOT NULL,
    documento    TEXT,
    telefono     TEXT,
    enviado_en   TEXT NOT NULL,
    estado_envio TEXT NOT NULL DEFAULT 'enviado',   -- enviado | fallido | pendiente
    error_envio  TEXT,
    canjeado_en  TEXT,
    venta_id     INTEGER
);
-- ---------------------------------------------------------------------------
-- PAGO: el checkout de Yupy de cada booking_id. Antes vivia solo en memoria
-- (un reinicio entre cobrar y emitir dejaba al cliente pagado y sin boleto,
-- hallazgo N5). monto_centimos lo calcula el SERVIDOR con precios de JELAF;
-- nunca sale del navegador (V2). Una transaccion de Yupy pertenece a un solo
-- booking_id (UNIQUE): no se puede reutilizar un pago para otra venta.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pago (
    booking_id          TEXT PRIMARY KEY,
    yupy_transaction_id TEXT NOT NULL UNIQUE,
    pedido              TEXT,
    monto_centimos      INTEGER NOT NULL CHECK (monto_centimos > 0),
    moneda              TEXT NOT NULL DEFAULT 'PEN',
    asientos            TEXT NOT NULL,     -- JSON: asientos cubiertos por este cobro
    estado              TEXT NOT NULL DEFAULT 'pendiente',
                        -- pendiente | usado | abandonado (el cliente salió sin pagar, N61-SN)
    creado_en           TEXT NOT NULL,
    actualizado_en      TEXT NOT NULL
    -- transacciones_anteriores TEXT (JSON): checkouts previos de la misma venta,
    -- agregada por db.init_db(). Si el cliente recarga la pantalla de pago se
    -- crea otro checkout, pero pudo haber pagado el anterior: no se olvida.
    -- proveedor TEXT ('yupy' | 'niubiz', NULL = yupy) y autorizacion TEXT (JSON
    -- con el resumen de la autorizacion de Niubiz, sin datos de la tarjeta):
    -- agregadas por db.init_db(). Con Niubiz, yupy_transaction_id guarda
    -- "NIUBIZ-<pedido>" (el nombre de la columna es historico).
    -- checkout_tokens TEXT (JSON {payment_order_uid: checkout_token}): Yupy
    -- /v1/checkouts; agregada por db.init_db(). No se escribe en logs.
);

-- ---------------------------------------------------------------------------
-- ACEPTACION_TYC: quien acepto los Terminos y Condiciones por WhatsApp antes
-- de recibir el boton de compra (constancia del consentimiento, Ley 29733).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS aceptacion_tyc (
    telefono     TEXT PRIMARY KEY,
    aceptado_en  TEXT NOT NULL,
    mensaje_id   TEXT          -- id del mensaje de Meta con el boton "Aceptar"
);

-- ---------------------------------------------------------------------------
-- MENSAJE_WHATSAPP: ids de mensajes ya procesados. Meta reintenta las
-- notificaciones; sin esto un reintento manda dos veces el boton (S7).
-- ---------------------------------------------------------------------------
-- ---------------------------------------------------------------------------
-- YUPY_WEBHOOK: cada aviso recibido de Yupy, una sola vez por event_id (Yupy
-- reintenta). Registro de auditoría: los avisos NO emiten boletos (eso solo lo
-- hace /confirmar tras verificar el pago con Yupy servidor a servidor).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS yupy_webhook (
    event_id          TEXT PRIMARY KEY,
    event_type        TEXT NOT NULL,
    payment_order_uid TEXT,
    result            TEXT,          -- solo en payment_order.terminal: pagado | cancelado | timeout
    booking_id        TEXT,          -- la venta a la que corresponde (si se encontró)
    recibido_en       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_yupy_webhook_orden ON yupy_webhook(payment_order_uid);

CREATE TABLE IF NOT EXISTS mensaje_whatsapp (
    id           TEXT PRIMARY KEY,
    recibido_en  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_msg_recibido ON mensaje_whatsapp(recibido_en);

CREATE INDEX IF NOT EXISTS ix_env_codigo ON codigo_enviado(codigo);
CREATE INDEX IF NOT EXISTS ix_env_doc    ON codigo_enviado(documento);
CREATE UNIQUE INDEX IF NOT EXISTS ux_env_codigo_doc ON codigo_enviado(codigo, documento);

-- ---------------------------------------------------------------------------
-- ATENCION: cada accion tomada sobre una incidencia. Una incidencia puede
-- tener varias (se contacto al cliente → se le ofrecio otro asiento → compro),
-- asi queda el seguimiento completo y no solo una nota final. La solucion se
-- "indexa" aqui: el boleto nuevo, la venta que lo respalda o la devolucion.
-- La columna incidencia.estado (abierta | en_seguimiento | resuelta |
-- derivada) la agrega db.init_db().
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS atencion (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    incidencia_id           INTEGER NOT NULL REFERENCES incidencia(id),
    accion                  TEXT NOT NULL,
                            -- contacto | ofrecio_asiento | compro_otro | desistio |
                            -- emitio_manual | devolucion | derivar | sin_accion | otra | nota
    resultado               TEXT NOT NULL,   -- en_seguimiento | resuelta | derivada
    canal_contacto          TEXT,            -- whatsapp | llamada | presencial
    nota                    TEXT,
    nro_boleto              TEXT,            -- boleto emitido como solucion
    booking_nuevo           TEXT,            -- compra nueva hecha por el bot
    venta_id                INTEGER REFERENCES venta(id),  -- si la solucion esta en la base
    referencia_devolucion   TEXT,
    monto_devuelto_centimos INTEGER CHECK (monto_devuelto_centimos IS NULL OR monto_devuelto_centimos > 0),
    derivada_a              TEXT,
    atendido_por            TEXT,
    creado_en               TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_atencion_incidencia ON atencion(incidencia_id, creado_en);
CREATE INDEX IF NOT EXISTS ix_atencion_creado     ON atencion(creado_en);
CREATE INDEX IF NOT EXISTS ix_atencion_accion     ON atencion(accion, creado_en);

-- ---------------------------------------------------------------------------
-- REGLA_BONO: bonos automaticos configurables desde el panel.
--   disparador 'viajes'  → umbral = boletos acumulados (repetir: cada N boletos)
--   disparador 'gasto'   → umbral = soles acumulados, EN CENTIMOS (repetir: cada S/ N)
--   disparador 'dormido' → umbral = dias sin comprar
-- valor: porcentaje entero, o centimos si tipo = 'monto'.
-- Cada bono generado es una fila de codigo_descuento con documento_exclusivo
-- (solo lo puede usar esa persona), regla_id e hito (que logro lo gano).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS regla_bono (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre         TEXT NOT NULL,
    disparador     TEXT NOT NULL CHECK (disparador IN ('viajes', 'gasto', 'dormido')),
    umbral         INTEGER NOT NULL CHECK (umbral > 0),
    repetir        INTEGER NOT NULL DEFAULT 0 CHECK (repetir IN (0, 1)),
    tipo           TEXT NOT NULL CHECK (tipo IN ('porcentaje', 'monto')),
    valor          INTEGER NOT NULL CHECK (valor > 0),
    tope_centimos  INTEGER CHECK (tope_centimos IS NULL OR tope_centimos > 0),
    vigencia_dias  INTEGER NOT NULL DEFAULT 30 CHECK (vigencia_dias BETWEEN 1 AND 365),
    prefijo        TEXT NOT NULL,
    activa         INTEGER NOT NULL DEFAULT 0 CHECK (activa IN (0, 1)),
    creada_en      TEXT NOT NULL,
    actualizada_en TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- LOTE_BONO: una creacion masiva de bonos (top 50, un segmento, una lista).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lote_bono (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre    TEXT NOT NULL,
    filtro    TEXT,               -- JSON: a quienes se eligio
    cantidad  INTEGER NOT NULL DEFAULT 0,
    creado_en TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- CIUDAD_CERRADA: ciudades (sedes de rutas.SEDES) que la empresa sacó de la
-- venta desde el panel de Administración. Una fila = cerrada; activarla la
-- borra. No se venden pasajes desde ni hacia ellas y no salen en el buscador;
-- las compras que ya habían empezado terminan normalmente.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ciudad_cerrada (
    codigo     TEXT PRIMARY KEY,    -- "ICA", "CERRO AZUL"...
    motivo     TEXT,
    cerrada_en TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- PASARELA_APAGADA: medios de pago apagados desde el panel (además de los que
-- faltan en el .env). Una fila = apagada; encenderla la borra. Los cobros ya
-- abiertos terminan (se verifican y emiten como siempre).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pasarela_apagada (
    medio      TEXT PRIMARY KEY CHECK (medio IN ('yupy', 'niubiz')),
    motivo     TEXT,
    apagada_en TEXT NOT NULL
);
