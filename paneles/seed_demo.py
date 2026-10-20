"""
Datos de ejemplo para ver los paneles funcionando, sin tocar JELAF ni Meta.

    python -m paneles.seed_demo           # agrega datos de ejemplo (no borra nada)
    python -m paneles.seed_demo --quitar  # quita SOLO los datos de ejemplo
    python -m paneles.seed_demo --reset   # vacia la base y la vuelve a llenar (solo bases sin ventas reales)

Genera 30 dias de actividad con forma realista: mas ventas de noche y los
viernes, sesiones que se abandonan en cada paso, incidencias ligadas a esas
compras, atenciones (resueltas, en seguimiento, derivadas), clientes
recurrentes, reglas de bono, un lote masivo y bonos en todos sus estados.

Todo lo inventado lleva una marca que no se confunde con datos reales, para
poder quitarlo despues sin tocar nada mas:
  - booking_id 'demo-...'          (sesiones, eventos, ventas, boletos, pagos)
  - documento  'DEMO0000'          (no es un DNI posible: nunca pisa a un cliente real)
  - telefono   '51000xxxxxx'       (ningun numero peruano empieza en 0: si algun dia
                                    se activa el envio, no le llega nada a nadie)
  - incidencias con contexto {"origen": "seed_demo"}
  - reglas, lotes y codigos generales con '[DEMO]' / prefijo DEMO

OJO: son datos INVENTADOS. Sirven para ver los paneles, no para decidir.
"""

from __future__ import annotations

import argparse
import json
import os
import random
from datetime import datetime, timedelta
from pathlib import Path

from . import db, fidelizacion, registro, tiempo

MARCA_CONTEXTO = json.dumps({"origen": "seed_demo"})

RUTAS = [
    ("LIMA", "AREQUIPA", 145.0, ["Golden Suite", "Emperador VIP", "Premium"]),
    ("AREQUIPA", "LIMA", 140.0, ["Golden Suite", "Emperador", "Premium"]),
    ("LIMA", "TACNA", 160.0, ["Emperador VIP", "Emperador Plus"]),
    ("TACNA", "AREQUIPA", 95.0, ["Emperador", "Ejecutivo"]),
    ("AREQUIPA", "TACNA", 95.0, ["Emperador", "Ejecutivo"]),
    ("LIMA", "CUSCO", 170.0, ["Golden Suite", "Emperador VIP"]),
]
PLACAS = ["V2H-955", "B7T-841", "X9C-203", "D4M-717", "F1K-660"]
HORAS_SALIDA = ["06:30", "08:00", "13:00", "15:30", "20:00", "20:30", "21:00", "22:00"]

NOMBRES = [
    ("Rosa", "Quispe", "Mamani"), ("Luis", "Vargas", "Ticona"),
    ("Carmen", "Flores", "Choque"), ("Juan", "Apaza", "Condori"),
    ("Miriam", "Cuadros", "Núñez"), ("Diego", "Salas", "Puma"),
    ("Elena", "Huanca", "Ramos"), ("Marco", "Zeballos", "Pari"),
    ("Sofía", "Chura", "Vilca"), ("Raúl", "Cutipa", "Layme"),
    ("Patricia", "Coaquira", "Sucso"), ("Andrés", "Calisaya", "Mendoza"),
    ("Lucía", "Ccalla", "Yupanqui"), ("Víctor", "Arapa", "Huaman"),
    ("Nadia", "Poma", "Larico"), ("Óscar", "Machaca", "Quenta"),
]

# Peso por bloque de 2 h para la HORA DE COMPRA: la gente compra de noche,
# despues de que la boleteria fisica cerro.
PESO_HORA_COMPRA = [1, 1, 1, 3, 6, 6, 5, 4, 6, 9, 12, 4]
# Peso por dia de la semana (lunes=0): el viernes es el pico.
PESO_DIA = [1.0, 0.9, 1.0, 1.15, 1.6, 1.2, 0.8]

# Donde se cae cada sesion que no termina en venta, y con que probabilidad.
ABANDONOS = [
    ("enlace_abierto", 0.32),
    ("viaje_elegido", 0.24),
    ("asiento_tocado", 0.14),
    ("pasajero_lleno", 0.07),
    ("pago_iniciado", 0.05),
]

# tipo, mensaje, cantidad, pasos de la compra en los que puede ocurrir
INCIDENCIAS_DEMO = [
    ("asiento_no_disponible", "El asiento se vendió en mostrador mientras el cliente decidía.", 26,
     ("asiento_tocado", "pasajero_lleno")),
    ("pool_ocupado", "Otra compra estaba usando el único mostrador virtual disponible.", 21,
     ("enlace_abierto", "viaje_elegido")),
    ("pago_rechazado", "Yupy rechazó la tarjeta del cliente.", 12, ("pago_iniciado",)),
    ("fallo_emision", "JELAF no confirmó la venta: la página no respondió a tiempo.", 9, ("pago_iniciado",)),
    ("sesion_liberada", "Se liberó la sesión por 10 minutos sin actividad del cliente.", 6,
     ("viaje_elegido", "asiento_tocado", "pasajero_lleno")),
]


def _tel(i: int) -> str:
    return f"51000{i:06d}"


def _dni(i: int) -> str:
    return f"DEMO{i:04d}"


def _momento_compra(dia: datetime) -> datetime:
    bloque = random.choices(range(12), weights=PESO_HORA_COMPRA, k=1)[0]
    return dia.replace(hour=bloque * 2 + random.randint(0, 1),
                       minute=random.randint(0, 59), second=random.randint(0, 59),
                       microsecond=0)


def _iso(momento: datetime) -> str:
    return momento.isoformat(timespec="seconds")


def limpiar() -> None:
    con = db.conexion()
    for tabla in ["atencion", "codigo_enviado", "codigo_descuento", "lote_bono", "regla_bono",
                  "incidencia", "boleto", "venta", "pago", "evento", "sesion_compra", "cliente",
                  "contacto", "mensaje_whatsapp", "aceptacion_tyc"]:
        con.execute(f"DELETE FROM {tabla}")
    con.commit()
    print("Base vaciada.")


# ---------------------------------------------------------------------------
# Quitar solo lo de ejemplo
# ---------------------------------------------------------------------------

def quitar_demo() -> dict:
    """
    Borra todo lo marcado como ejemplo y deja intacto lo real. Si un cliente
    real compro junto con datos de ejemplo (no deberia pasar), sus contadores
    se recalculan a partir de lo que queda.
    """
    borrado: dict[str, int] = {}

    def borrar(nombre: str, sql: str, params: tuple = ()) -> None:
        borrado[nombre] = borrado.get(nombre, 0) + db.escribir_contando(sql, params)

    with db.transaccion():
        inc_demo = ("SELECT id FROM incidencia WHERE booking_id LIKE 'demo-%' "
                    "OR contexto = ? OR contexto LIKE '%\"origen\": \"seed_demo\"%'")
        borrar("atenciones", f"DELETE FROM atencion WHERE incidencia_id IN ({inc_demo})", (MARCA_CONTEXTO,))
        borrar("incidencias", inc_demo.replace("SELECT id FROM", "DELETE FROM"), (MARCA_CONTEXTO,))

        regla_demo = "SELECT id FROM regla_bono WHERE nombre LIKE '[DEMO]%'"
        lote_demo = "SELECT id FROM lote_bono WHERE nombre LIKE '[DEMO]%'"
        codigos_demo = f"""
            SELECT codigo FROM codigo_descuento
            WHERE documento_exclusivo LIKE 'DEMO%' OR codigo LIKE 'DEMO%'
               OR regla_id IN ({regla_demo}) OR lote_id IN ({lote_demo})
        """
        borrar("envios de codigos", f"DELETE FROM codigo_enviado WHERE documento LIKE 'DEMO%' OR codigo IN ({codigos_demo})")
        borrar("codigos y bonos", f"DELETE FROM codigo_descuento WHERE codigo IN ({codigos_demo})")
        borrar("reglas", "DELETE FROM regla_bono WHERE nombre LIKE '[DEMO]%'")
        borrar("lotes", "DELETE FROM lote_bono WHERE nombre LIKE '[DEMO]%'")

        # Versiones anteriores de este script usaban DNI y telefonos con forma
        # real: esos clientes se reconocen porque solo tenian compras de ejemplo.
        afectados = [f["documento"] for f in db.consultar(
            "SELECT DISTINCT b.documento FROM boleto b JOIN venta v ON v.id = b.venta_id "
            "WHERE v.booking_id LIKE 'demo-%' AND b.documento NOT LIKE 'DEMO%'")]
        telefonos = [f["telefono"] for f in db.consultar(
            "SELECT DISTINCT telefono FROM sesion_compra WHERE booking_id LIKE 'demo-%' "
            "AND telefono IS NOT NULL AND telefono NOT LIKE '51000%'")]
        borrar("boletos", "DELETE FROM boleto WHERE venta_id IN (SELECT id FROM venta WHERE booking_id LIKE 'demo-%')")
        borrar("ventas", "DELETE FROM venta WHERE booking_id LIKE 'demo-%'")
        borrar("pagos", "DELETE FROM pago WHERE booking_id LIKE 'demo-%'")
        borrar("eventos", "DELETE FROM evento WHERE booking_id LIKE 'demo-%' OR telefono LIKE '51000%'")
        borrar("sesiones", "DELETE FROM sesion_compra WHERE booking_id LIKE 'demo-%'")
        borrar("clientes", "DELETE FROM cliente WHERE documento LIKE 'DEMO%'")
        borrar("contactos", "DELETE FROM contacto WHERE telefono LIKE '51000%'")
        for documento in afectados:
            borrar("clientes", "DELETE FROM cliente WHERE documento = ? "
                   "AND NOT EXISTS (SELECT 1 FROM boleto WHERE documento = ?)", (documento, documento))
        for tel in telefonos:
            borrar("contactos", "DELETE FROM contacto WHERE telefono = ? "
                   "AND NOT EXISTS (SELECT 1 FROM sesion_compra WHERE telefono = ?) "
                   "AND NOT EXISTS (SELECT 1 FROM venta WHERE telefono = ?)", (tel, tel, tel))
    for documento in afectados:
        registro.recalcular_cliente(documento)   # si era real y le quedan compras, se corrige
    return borrado


# ---------------------------------------------------------------------------
# Sembrar
# ---------------------------------------------------------------------------

def sembrar(dias: int = 30) -> dict:
    random.seed(20260914)
    ahora = tiempo.ahora_dt()
    hoy = ahora.replace(hour=0, minute=0, second=0, microsecond=0)
    clientes = [(NOMBRES[i % len(NOMBRES)], _dni(i), _tel(i)) for i in range(90)]

    # Unos pocos clientes compran mucho: son los que hacen interesante el
    # panel de fidelizacion. El resto compra una sola vez.
    recurrentes = clientes[:12]

    total_sesiones = 0
    ventas: list[dict] = []
    abandonadas: list[dict] = []

    for d in range(dias, -1, -1):
        dia = hoy - timedelta(days=d)
        peso = PESO_DIA[dia.weekday()]
        sesiones_hoy = max(6, int(random.gauss(26 * peso, 4)))

        for _ in range(sesiones_hoy):
            momento = _momento_compra(dia)
            if momento > ahora:
                continue   # hoy solo hasta la hora actual
            total_sesiones += 1
            booking_id = f"demo-{momento.strftime('%y%m%d%H%M%S')}-{random.randint(100, 999)}"

            if random.random() < 0.55:
                persona, doc, tel = random.choice(recurrentes)
            else:
                persona, doc, tel = random.choice(clientes[12:])

            _sembrar_contacto(tel, persona, momento)
            _sembrar_sesion(booking_id, tel, momento)

            corte = random.random()
            acumulado = 0.0
            paso_final = "compra"
            for paso, prob in ABANDONOS:
                acumulado += prob
                if corte < acumulado:
                    paso_final = paso
                    break

            origen, destino, precio, servicios = random.choice(RUTAS)
            servicio = random.choice(servicios)
            hora_salida = random.choices(HORAS_SALIDA, weights=[2, 2, 1, 1, 6, 5, 4, 3], k=1)[0]
            fecha_salida = (dia + timedelta(days=random.randint(0, 12))).strftime("%Y-%m-%d")

            _sembrar_flujo(booking_id, tel, momento, paso_final, origen, destino,
                           fecha_salida, hora_salida, servicio)

            if paso_final != "compra":
                registro.cerrar_sesion(booking_id, "abandonada")
                abandonadas.append({"booking_id": booking_id, "telefono": tel,
                                    "paso": paso_final, "momento": momento})
                continue

            letra = random.choice(["A", "B"])
            cantidad = random.choices([1, 1, 1, 1, 2, 2, 3], k=1)[0]
            pasajeros = []
            for k in range(cantidad):
                if k == 0:
                    (nom, pat, mat), documento = persona, doc
                else:
                    nom, pat, mat = random.choice(NOMBRES)
                    documento = _dni(random.randint(90, 400))
                pasajeros.append({
                    "documento": documento, "tipo_documento": "01",
                    "nombres": nom, "paterno": pat, "materno": mat,
                    "asiento": f"{letra}-{random.randint(1, 40):02d}", "precio": precio,
                })

            falla = random.random() < 0.012   # cobrado y no emitido
            monto = round(precio * cantidad, 2)
            nro = None if falla else f"B-{4800000 + len(ventas):07d}"
            for p in pasajeros:
                p["nro_boleto"] = nro
            venta_id = registro.registrar_venta(
                booking_id=booking_id, origen=origen, destino=destino,
                pasajeros=pasajeros, monto_total=monto, nro_boleto=nro,
                telefono=tel, servicio=servicio, placa_bus=random.choice(PLACAS),
                fecha_viaje=fecha_salida, hora_viaje=hora_salida,
                medio_pago=random.choice(["yupy_tarjeta", "yupy_qr"]),
                referencia_pago=f"YP{random.randint(10**8, 10**9)}",
                estado="cobrada_sin_emitir" if falla else "emitida",
            )
            # La hora real de la compra no es "ahora": se corrige.
            db.escribir("UPDATE venta SET comprado_en = ? WHERE id = ?", (_iso(momento), venta_id))
            ventas.append({"id": venta_id, "booking_id": booking_id, "nro": nro, "momento": momento})

            if falla:
                inc = registro.registrar_incidencia(
                    "cobrado_sin_emitir", "El pago se aprobó y JELAF falló al emitir el boleto.",
                    booking_id=booking_id, telefono=tel,
                    contexto={"origen": "seed_demo", "venta_id": venta_id, "monto": monto},
                )
                db.escribir("UPDATE incidencia SET ocurrido_en = ? WHERE id = ?",
                            (_iso(momento + timedelta(minutes=4)), inc))

    _corregir_fechas_cliente()
    registro.resegmentar_todos()
    incidencias = _sembrar_incidencias(abandonadas)
    _sembrar_atenciones(incidencias, ventas, ahora)
    _sembrar_codigos(ahora)
    bonos = _sembrar_bonos(ahora)

    print(f"Sembradas {total_sesiones} sesiones, {len(ventas)} ventas, {len(incidencias)} incidencias "
          f"y {bonos} bonos en {dias} días.")
    return {"sesiones": total_sesiones, "ventas": len(ventas), "incidencias": len(incidencias), "bonos": bonos}


def _sembrar_contacto(tel: str, persona: tuple, momento: datetime) -> None:
    t = _iso(momento)
    db.escribir(
        """
        INSERT INTO contacto (telefono, nombre_wa, primer_mensaje, ultimo_mensaje, mensajes, enlaces_enviados)
        VALUES (?,?,?,?,1,1)
        ON CONFLICT(telefono) DO UPDATE SET
            primer_mensaje   = MIN(contacto.primer_mensaje, excluded.primer_mensaje),
            ultimo_mensaje   = MAX(contacto.ultimo_mensaje, excluded.ultimo_mensaje),
            mensajes         = contacto.mensajes + 1,
            enlaces_enviados = contacto.enlaces_enviados + 1
        """,
        (tel, f"{persona[0]} {persona[1]}", t, t),
    )


def _sembrar_sesion(booking_id: str, tel: str, momento: datetime) -> None:
    t = _iso(momento)
    db.escribir(
        """
        INSERT INTO sesion_compra (booking_id, telefono, abierta_en, actualizada_en, paso_maximo)
        VALUES (?,?,?,?,'enlace_abierto')
        """,
        (booking_id, tel, t, t),
    )


def _sembrar_flujo(booking_id, tel, momento, paso_final, origen, destino,
                   fecha_salida, hora_salida, servicio) -> None:
    """Escribe los eventos del flujo hasta donde llego el cliente."""
    orden = ["enlace_abierto", "viaje_elegido", "asiento_tocado", "pasajero_lleno",
             "pago_iniciado", "boleto_emitido"]
    tope = len(orden) if paso_final == "compra" else orden.index(paso_final) + 1

    for i, paso in enumerate(orden[:tope]):
        if paso == "boleto_emitido":
            continue   # lo registra registrar_venta() con la venta
        t = _iso(momento + timedelta(seconds=i * random.randint(20, 90)))
        db.escribir(
            "INSERT INTO evento (booking_id, telefono, paso, ocurrido_en, detalle) VALUES (?,?,?,?,?)",
            (booking_id, tel, paso, t, None),
        )

    paso_maximo = orden[min(tope, len(orden) - 1) - 1] if paso_final == "compra" else orden[tope - 1]
    db.escribir(
        """
        UPDATE sesion_compra SET paso_maximo = ?, origen = ?, destino = ?,
               fecha_viaje = ?, hora_viaje = ?, servicio = ?
        WHERE booking_id = ?
        """,
        (paso_maximo, origen if tope > 1 else None, destino if tope > 1 else None,
         fecha_salida if tope > 1 else None, hora_salida if tope > 1 else None,
         servicio if tope > 1 else None, booking_id),
    )


def _sembrar_incidencias(abandonadas: list[dict]) -> list[dict]:
    """Incidencias ligadas a compras abandonadas en el paso donde tienen sentido."""
    creadas = [dict(f) for f in db.consultar(
        "SELECT id, tipo, severidad, ocurrido_en, booking_id FROM incidencia "
        "WHERE booking_id LIKE 'demo-%' AND tipo = 'cobrado_sin_emitir'")]
    for tipo, mensaje, cantidad, pasos in INCIDENCIAS_DEMO:
        candidatas = [a for a in abandonadas if a["paso"] in pasos] or abandonadas
        for a in random.sample(candidatas, min(cantidad, len(candidatas))):
            t = _iso(a["momento"] + timedelta(minutes=random.randint(1, 6)))
            sev = registro.SEVERIDAD.get(tipo, "leve")
            inc_id = db.escribir(
                """
                INSERT INTO incidencia (booking_id, telefono, tipo, severidad, mensaje, contexto, ocurrido_en)
                VALUES (?,?,?,?,?,?,?)
                """,
                (a["booking_id"], a["telefono"], tipo, sev, mensaje, MARCA_CONTEXTO, t),
            )
            creadas.append({"id": inc_id, "tipo": tipo, "severidad": sev, "ocurrido_en": t,
                            "booking_id": a["booking_id"]})
    return creadas


def _sembrar_atenciones(incidencias: list[dict], ventas: list[dict], ahora: datetime) -> None:
    """
    Atiende la mayoria como lo haria quien atiende, deja algunas en
    seguimiento y las criticas recientes abiertas (son las que deben verse).
    """
    emitidas = [v for v in ventas if v["nro"]]
    for inc in incidencias:
        tipo = inc["tipo"]
        ocurrio = datetime.fromisoformat(inc["ocurrido_en"])
        reciente = (ahora - ocurrio) < timedelta(days=2)
        azar = random.random()
        pasos: list[tuple[str, dict]] = []

        if tipo == "cobrado_sin_emitir":
            if reciente:
                continue
            pasos = [("contacto", {"canal_contacto": "whatsapp"}),
                     ("devolucion" if azar < 0.5 else "emitio_manual",
                      {"referencia_devolucion": f"YP-DEV-{random.randint(1000, 9999)}",
                       "monto_devuelto_centimos": random.choice([14500, 16000, 29000]),
                       "nota": "Se devolvió el pago en Yupy y se avisó al cliente."}
                      if azar < 0.5 else
                      {"nro_boleto": f"B009-{random.randint(1000, 9999)}",
                       "nota": "Emitido a mano en boletería con los datos de la compra."})]
        elif tipo == "fallo_emision":
            if azar < 0.25:
                continue
            pasos = [("emitio_manual", {"nro_boleto": f"B009-{random.randint(1000, 9999)}",
                                        "nota": "El asiento seguía libre; se emitió desde JELAF."})
                     if azar < 0.75 else
                     ("derivar", {"derivada_a": "Soporte JELAF",
                                  "nota": "JELAF devuelve error al grabar la venta."})]
        elif tipo in ("asiento_no_disponible", "sesion_liberada", "pago_rechazado"):
            if azar < 0.2:
                continue
            if azar < 0.35:
                pasos = [("ofrecio_asiento", {"canal_contacto": "whatsapp",
                                              "nota": "Se le ofrecieron dos asientos libres del mismo turno."})]
            elif azar < 0.65 and emitidas:
                v = random.choice(emitidas)
                pasos = [("ofrecio_asiento", {"canal_contacto": "whatsapp"}),
                         ("compro_otro", {"booking_nuevo": v["booking_id"],
                                          "nota": "Compró otro asiento por el bot."})]
            elif azar < 0.85:
                pasos = [("ofrecio_asiento", {"canal_contacto": "llamada"}),
                         ("desistio", {"nota": "Prefirió viajar otro día."})]
            else:
                pasos = [("sin_accion", {})]
        elif tipo == "pool_ocupado":
            if azar < 0.3:
                continue
            pasos = [("sin_accion", {"nota": "Pico de compras simultáneas."})]

        momento = ocurrio
        for accion, datos in pasos:
            momento = min(momento + timedelta(minutes=random.randint(8, 240)), ahora)
            try:
                registro.registrar_atencion(inc["id"], accion, datos)
            except (ValueError, LookupError, registro.IncidenciaCerrada) as e:
                print(f"[seed_demo] atención omitida en #{inc['id']}: {e}")
                break
            t = _iso(momento)
            db.escribir("UPDATE atencion SET creado_en = ? WHERE id = (SELECT MAX(id) FROM atencion WHERE incidencia_id = ?)",
                        (t, inc["id"]))
            db.escribir("UPDATE incidencia SET actualizada_en = ?, "
                        "resuelta_en = CASE WHEN resuelta = 1 THEN ? ELSE resuelta_en END WHERE id = ?",
                        (t, t, inc["id"]))


def _sembrar_codigos(ahora: datetime) -> None:
    """Codigos generales de ejemplo, enviados solo a clientes de ejemplo."""
    hoy = ahora.date()
    fidelizacion.crear_codigo(
        "DEMOVUELVE10", 10, descripcion="[DEMO] Recuperar clientes dormidos",
        segmento_objetivo="dormido", vence_en=str(hoy + timedelta(days=20)), estado="activo")
    fidelizacion.crear_codigo(
        "DEMOFIEL15", 15, descripcion="[DEMO] Premio a los clientes frecuentes",
        segmento_objetivo="frecuente", vence_en=str(hoy + timedelta(days=10)),
        tope_descuento=30, estado="activo")
    fidelizacion.crear_codigo(
        "DEMOBIENV5", 5, descripcion="[DEMO] Primera compra por el bot",
        segmento_objetivo="nuevo", vence_en=str(hoy + timedelta(days=60)), estado="borrador")

    for codigo, segmento in [("DEMOVUELVE10", "dormido"), ("DEMOFIEL15", "frecuente")]:
        for cli in db.consultar(
            "SELECT documento, telefono FROM cliente WHERE segmento = ? AND documento LIKE 'DEMO%' LIMIT 25",
            (segmento,),
        ):
            enviado = _iso(ahora - timedelta(days=random.randint(1, 15)))
            canjeado = _iso(ahora - timedelta(days=random.randint(0, 5))) if random.random() < 0.35 else None
            db.escribir(
                """
                INSERT INTO codigo_enviado (codigo, documento, telefono, enviado_en, estado_envio, canjeado_en)
                VALUES (?,?,?,?,'enviado',?)
                ON CONFLICT(codigo, documento) DO NOTHING
                """,
                (codigo, cli["documento"], cli["telefono"], enviado, canjeado),
            )


def _sembrar_bonos(ahora: datetime) -> int:
    """
    Reglas de ejemplo (quedan DESACTIVADAS: en una base real no deben dar bonos
    a clientes reales), un lote al top 20 y bonos en todos sus estados.
    """
    demo = [f["documento"] for f in db.consultar("SELECT documento FROM cliente WHERE documento LIKE 'DEMO%'")]
    reglas = [
        {"nombre": "[DEMO] Cliente frecuente: cada 5 boletos", "disparador": "viajes", "umbral": 5,
         "repetir": True, "tipo": "porcentaje", "valor": 10, "vigencia_dias": 30, "prefijo": "VUELVE10"},
        {"nombre": "[DEMO] Gastó S/ 500", "disparador": "gasto", "umbral": 50000, "repetir": False,
         "tipo": "monto", "valor": 2000, "vigencia_dias": 45, "prefijo": "GRACIAS20"},
        {"nombre": "[DEMO] Dormidos 20 días", "disparador": "dormido", "umbral": 20, "repetir": False,
         "tipo": "porcentaje", "valor": 15, "tope_centimos": 3000, "vigencia_dias": 15, "prefijo": "VUELVE15"},
    ]
    for datos in reglas:
        regla_id = registro.guardar_regla(datos)
        registro.activar_regla(regla_id, True)
        registro.aplicar_reglas(regla_id=regla_id, documentos=demo)
        registro.activar_regla(regla_id, False)

    top = db.consultar(
        "SELECT documento, telefono FROM cliente WHERE documento LIKE 'DEMO%' "
        "ORDER BY total_viajes DESC, total_gastado DESC LIMIT 20")
    registro.crear_lote("[DEMO] Top 20 del mes", [dict(f) for f in top], prefijo="TOP10", tipo="porcentaje",
                        valor=10, tope_centimos=None, vigencia_dias=30, filtro={"tipo": "top", "n": 20})

    # Estados variados: algunos canjeados, vencidos y anulados.
    bonos = [dict(f) for f in db.consultar(
        "SELECT codigo, documento_exclusivo FROM codigo_descuento WHERE documento_exclusivo LIKE 'DEMO%' ORDER BY codigo")]
    random.shuffle(bonos)
    ventas = [f["id"] for f in db.consultar("SELECT id FROM venta WHERE booking_id LIKE 'demo-%' AND estado = 'emitida'")]
    for b in bonos[:8]:
        db.escribir("UPDATE codigo_enviado SET canjeado_en = ?, venta_id = ? WHERE codigo = ?",
                    (_iso(ahora - timedelta(days=random.randint(0, 6))), random.choice(ventas) if ventas else None, b["codigo"]))
    for b in bonos[8:12]:
        db.escribir("UPDATE codigo_descuento SET vence_en = ? WHERE codigo = ?",
                    ((ahora.date() - timedelta(days=random.randint(1, 5))).isoformat(), b["codigo"]))
    for b in bonos[12:14]:
        registro.anular_bono(b["codigo"])
    for b in bonos[14:20]:
        db.escribir("UPDATE codigo_descuento SET vence_en = ? WHERE codigo = ?",
                    ((ahora.date() + timedelta(days=random.randint(1, 6))).isoformat(), b["codigo"]))
    return len(bonos)


def _corregir_fechas_cliente() -> None:
    """
    recalcular_cliente() usa las fechas reales de venta, pero durante la
    siembra las ventas se insertan con la hora actual y despues se corrigen.
    Se recalcula al final, solo para los clientes de ejemplo.
    """
    for fila in db.consultar("SELECT documento FROM cliente WHERE documento LIKE 'DEMO%'"):
        registro.recalcular_cliente(fila["documento"])


def main() -> None:
    parser = argparse.ArgumentParser(description="Datos de ejemplo para la base de paneles.")
    accion = parser.add_mutually_exclusive_group()
    accion.add_argument("--reset", action="store_true", help="vacía las tablas antes de sembrar")
    accion.add_argument("--quitar", action="store_true", help="quita solo los datos de ejemplo")
    parser.add_argument("--dias", type=int, default=30, help="cuántos días de historia generar")
    parser.add_argument("--borrar-datos-reales", action="store_true",
                        help="permite --reset aunque la base tenga ventas reales (no demo)")
    args = parser.parse_args()

    # Misma base que el tablero: servidor_paneles.py y api_ventas.py cargan el
    # .env (config) antes de abrirla, y ahi puede estar PANELES_DB.
    import config
    config.configurar_salida_utf8()
    db.RUTA_DB = Path(os.getenv("PANELES_DB", db.BASE_DIR / "boleteria.db"))
    db.init_db()
    print(f"Base: {db.RUTA_DB}")
    if args.quitar:
        borrado = quitar_demo()
        print("Quitados:", {k: v for k, v in borrado.items() if v})
        return
    reales = db.consultar_uno(
        "SELECT COUNT(*) AS n FROM venta WHERE booking_id IS NULL OR booking_id NOT LIKE 'demo-%'"
    )["n"]
    if args.reset:
        # Una base con ventas que no son de ejemplo tiene datos REALES de
        # clientes: no se vacía sin pedirlo explícitamente.
        if reales and not args.borrar_datos_reales:
            raise SystemExit(
                f"La base {db.RUTA_DB} tiene {reales} ventas reales. No se vacía. "
                "Usa --quitar para sacar solo los datos de ejemplo, otra base (PANELES_DB=...) "
                "o agrega --borrar-datos-reales si de verdad quieres borrarla."
            )
        limpiar()
    elif db.consultar_uno("SELECT 1 FROM venta WHERE booking_id LIKE 'demo-%' LIMIT 1"):
        print("Ya había datos de ejemplo: se quitan y se vuelven a generar.")
        quitar_demo()
    if reales:
        print(f"Hay {reales} ventas reales: se conservan. Los datos de ejemplo se quitan con --quitar.")
    sembrar(args.dias)


if __name__ == "__main__":
    main()
