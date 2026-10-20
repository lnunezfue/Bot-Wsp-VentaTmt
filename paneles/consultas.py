"""
Capa de LECTURA de los paneles. Solo SELECTs; nada de aqui escribe.

Cada funcion corresponde a un bloque visible de alguno de los tres paneles,
y devuelve estructuras ya listas para serializar a JSON. La idea es que
api_paneles.py sea una cascara delgada sobre este archivo.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import privacidad

from . import db, tiempo
from .registro import DIAS_DORMIDO, PASOS, VIAJES_FRECUENTE, VIAJES_OCASIONAL

DIAS_SEMANA = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]

# strftime('%w') en SQLite: 0 = domingo. Lo giramos a lunes = 0.
_SQL_DIA_SEMANA = "(CAST(strftime('%w', {col}) AS INTEGER) + 6) % 7"
_SQL_BLOQUE_2H = "CAST(strftime('%H', {col}) AS INTEGER) / 2"


def rango(desde: str | None = None, hasta: str | None = None, dias: int = 7) -> tuple[str, str]:
    """
    Normaliza el rango de fechas a dos ISO-8601 comparables como texto.
    Sin argumentos devuelve los ultimos `dias` dias, hasta el final de hoy.
    Lanza ValueError si una fecha no es YYYY-MM-DD o si desde > hasta (el
    router lo traduce a 400, N9).
    """
    fin = date.fromisoformat(hasta) if hasta else tiempo.hoy()
    ini = date.fromisoformat(desde) if desde else fin - timedelta(days=dias - 1)
    if ini > fin:
        raise ValueError("'desde' es posterior a 'hasta'.")
    if (fin - ini).days > 366:
        raise ValueError("El rango máximo es de un año.")
    return f"{ini.isoformat()}T00:00:00", f"{fin.isoformat()}T23:59:59"


# ===========================================================================
#  PANEL 1 - VENTAS
# ===========================================================================

def resumen_ventas(desde: str, hasta: str) -> dict:
    v = db.consultar_uno(
        """
        SELECT COUNT(*) AS ventas,
               COALESCE(SUM(cantidad_asientos), 0) AS boletos,
               COALESCE(SUM(monto_total), 0)       AS ingresos,
               COALESCE(SUM(monto_descuento), 0)   AS descuentos
        FROM venta
        WHERE estado = 'emitida' AND comprado_en BETWEEN ? AND ?
        """,
        (desde, hasta),
    )
    sesiones = db.consultar_uno(
        "SELECT COUNT(*) AS n FROM sesion_compra WHERE abierta_en BETWEEN ? AND ?",
        (desde, hasta),
    )
    boletos = v["boletos"] or 0
    ingresos = round(v["ingresos"] or 0, 2)
    intentos = sesiones["n"] or 0
    return {
        "ventas": v["ventas"] or 0,
        "boletos": boletos,
        "ingresos": ingresos,
        "descuentos": round(v["descuentos"] or 0, 2),
        "ticket_promedio": round(ingresos / boletos, 2) if boletos else 0,
        "sesiones": intentos,
        "conversion": round((v["ventas"] or 0) / intentos * 100, 1) if intentos else 0,
    }


def heatmap_horas(desde: str, hasta: str, base: str = "compra") -> dict:
    """
    Matriz dia de la semana x bloque de 2 horas.

    base='compra' -> cuando el cliente paga  (cuando hay que tener mostrador)
    base='salida' -> cuando sale el bus      (que turnos se llenan)

    Son dos preguntas distintas y el panel las muestra con el mismo dibujo.
    """
    col = "comprado_en" if base == "compra" else "sale_en"
    filas = db.consultar(
        f"""
        SELECT {_SQL_DIA_SEMANA.format(col=col)} AS dia,
               {_SQL_BLOQUE_2H.format(col=col)}  AS bloque,
               COALESCE(SUM(cantidad_asientos), 0) AS boletos
        FROM venta
        WHERE estado = 'emitida' AND {col} IS NOT NULL
          AND comprado_en BETWEEN ? AND ?
        GROUP BY dia, bloque
        """,
        (desde, hasta),
    )

    matriz = [[0] * 12 for _ in range(7)]
    for f in filas:
        if f["dia"] is None or f["bloque"] is None:
            continue
        matriz[int(f["dia"])][int(f["bloque"])] = f["boletos"]

    maximo = max((c for fila in matriz for c in fila), default=0)
    pico = None
    if maximo:
        for i, fila in enumerate(matriz):
            for j, v in enumerate(fila):
                if v == maximo:
                    pico = {"dia": DIAS_SEMANA[i], "desde": f"{j*2:02d}:00",
                            "hasta": f"{(j+1)*2:02d}:00", "boletos": v}
                    break
            if pico:
                break

    return {
        "base": base,
        "dias": DIAS_SEMANA,
        "bloques": [f"{h:02d}" for h in range(0, 24, 2)],
        "matriz": matriz,
        "maximo": maximo,
        "pico": pico,
    }


def ventas_por_dia(desde: str, hasta: str) -> list[dict]:
    filas = db.consultar(
        """
        SELECT date(comprado_en) AS dia,
               COALESCE(SUM(cantidad_asientos), 0) AS boletos,
               COALESCE(SUM(monto_total), 0)       AS ingresos
        FROM venta
        WHERE estado = 'emitida' AND comprado_en BETWEEN ? AND ?
        GROUP BY dia ORDER BY dia
        """,
        (desde, hasta),
    )
    return [
        {"dia": f["dia"], "boletos": f["boletos"], "ingresos": round(f["ingresos"], 2)}
        for f in filas
    ]


def rutas_top(desde: str, hasta: str, limite: int = 8) -> list[dict]:
    filas = db.consultar(
        """
        SELECT origen, destino,
               COALESCE(SUM(cantidad_asientos), 0) AS boletos,
               COALESCE(SUM(monto_total), 0)       AS ingresos
        FROM venta
        WHERE estado = 'emitida' AND comprado_en BETWEEN ? AND ?
        GROUP BY origen, destino
        ORDER BY boletos DESC LIMIT ?
        """,
        (desde, hasta, limite),
    )
    return [
        {"ruta": f"{f['origen']} → {f['destino']}", "origen": f["origen"],
         "destino": f["destino"], "boletos": f["boletos"],
         "ingresos": round(f["ingresos"], 2)}
        for f in filas
    ]


def servicios_top(desde: str, hasta: str) -> list[dict]:
    filas = db.consultar(
        """
        SELECT COALESCE(servicio, 'Sin registrar') AS servicio,
               COALESCE(SUM(cantidad_asientos), 0) AS boletos,
               COALESCE(SUM(monto_total), 0)       AS ingresos
        FROM venta
        WHERE estado = 'emitida' AND comprado_en BETWEEN ? AND ?
        GROUP BY servicio ORDER BY boletos DESC
        """,
        (desde, hasta),
    )
    return db.filas_a_dict(filas)


def ultimas_ventas(limite: int = 20) -> list[dict]:
    filas = db.consultar(
        """
        SELECT v.id, v.comprado_en, v.nro_boleto, v.origen, v.destino, v.servicio,
               v.monto_total, v.estado, v.codigo_descuento,
               (SELECT GROUP_CONCAT(b.asiento, ', ') FROM boleto b WHERE b.venta_id = v.id) AS asientos,
               (SELECT b.documento FROM boleto b WHERE b.venta_id = v.id LIMIT 1) AS documento
        FROM venta v
        ORDER BY v.comprado_en DESC LIMIT ?
        """,
        (limite,),
    )
    salida = []
    for f in filas:
        d = dict(f)
        doc = d.pop("documento", None)
        d["cliente"] = f"•••{doc[-5:]}" if doc else "—"
        d["ruta"] = f"{d['origen']} → {d['destino']}"
        salida.append(d)
    return salida


# ===========================================================================
#  PANEL 2 - INCIDENCIAS
# ===========================================================================

ETIQUETA_PASO = {
    "enlace_abierto": "Abrió el enlace de compra",
    "viaje_elegido": "Eligió un viaje",
    "asiento_tocado": "Tocó un asiento",
    "pasajero_lleno": "Llenó los datos del pasajero",
    "pago_iniciado": "Llegó al pago",
    "boleto_emitido": "Boleto emitido",
}


def embudo(desde: str, hasta: str) -> list[dict]:
    """
    Cuantas sesiones distintas alcanzaron cada paso. Como paso_maximo solo
    avanza, una sesion que llego a 'pago_iniciado' cuenta tambien en todos
    los pasos anteriores.
    """
    filas = db.consultar(
        """
        SELECT paso_maximo, COUNT(*) AS n
        FROM sesion_compra
        WHERE abierta_en BETWEEN ? AND ?
        GROUP BY paso_maximo
        """,
        (desde, hasta),
    )
    conteo = {f["paso_maximo"]: f["n"] for f in filas}

    pasos_visibles = list(ETIQUETA_PASO.keys())
    salida, anterior = [], None
    for paso in pasos_visibles:
        idx = PASOS.index(paso)
        # todas las sesiones cuyo paso_maximo esta en este paso o mas adelante
        alcanzaron = sum(n for p, n in conteo.items()
                         if p in PASOS and PASOS.index(p) >= idx)
        caida = None
        if anterior:
            caida = round((anterior - alcanzaron) / anterior * 100, 1) if anterior else 0
        salida.append({"paso": paso, "etiqueta": ETIQUETA_PASO[paso],
                       "sesiones": alcanzaron, "caida_pct": caida})
        anterior = alcanzaron
    return salida


def resumen_incidencias(desde: str, hasta: str) -> dict:
    inc = db.consultar_uno(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN severidad = 'critica' THEN 1 ELSE 0 END) AS criticas,
               SUM(CASE WHEN severidad = 'grave'   THEN 1 ELSE 0 END) AS graves,
               SUM(CASE WHEN severidad = 'leve'    THEN 1 ELSE 0 END) AS leves,
               SUM(CASE WHEN resuelta = 0 THEN 1 ELSE 0 END) AS abiertas
        FROM incidencia WHERE ocurrido_en BETWEEN ? AND ?
        """,
        (desde, hasta),
    )
    pagos = db.consultar_uno(
        """
        SELECT COUNT(*) AS intentos,
               SUM(CASE WHEN estado = 'emitida' THEN 1 ELSE 0 END) AS emitidas,
               SUM(CASE WHEN estado = 'cobrada_sin_emitir' THEN 1 ELSE 0 END) AS sin_emitir
        FROM venta WHERE comprado_en BETWEEN ? AND ?
        """,
        (desde, hasta),
    )
    intentos = pagos["intentos"] or 0
    emitidas = pagos["emitidas"] or 0
    return {
        "total": inc["total"] or 0,
        "criticas": inc["criticas"] or 0,
        "graves": inc["graves"] or 0,
        "leves": inc["leves"] or 0,
        "abiertas": inc["abiertas"] or 0,
        "intentos_pago": intentos,
        "emitidas": emitidas,
        "cobradas_sin_emitir": pagos["sin_emitir"] or 0,
        "tasa_exito": round(emitidas / intentos * 100, 1) if intentos else 0,
    }


def incidencias_por_tipo(desde: str, hasta: str) -> list[dict]:
    filas = db.consultar(
        """
        SELECT tipo, severidad, COUNT(*) AS n, MAX(ocurrido_en) AS ultima
        FROM incidencia WHERE ocurrido_en BETWEEN ? AND ?
        GROUP BY tipo, severidad
        ORDER BY CASE severidad WHEN 'critica' THEN 0 WHEN 'grave' THEN 1 ELSE 2 END, n DESC
        """,
        (desde, hasta),
    )
    return db.filas_a_dict(filas)


def incidencias_abiertas(limite: int = 20) -> list[dict]:
    filas = db.consultar(
        """
        SELECT id, booking_id, telefono, tipo, severidad, mensaje, contexto, ocurrido_en, estado
        FROM incidencia WHERE resuelta = 0
        ORDER BY CASE severidad WHEN 'critica' THEN 0 WHEN 'grave' THEN 1 ELSE 2 END,
                 ocurrido_en DESC
        LIMIT ?
        """,
        (limite,),
    )
    return db.filas_a_dict(filas)


def resumen_abiertas() -> dict:
    """Total real de incidencias sin resolver (todas las fechas), por severidad: no depende del tope de la bandeja."""
    filas = db.consultar("SELECT severidad, COUNT(*) AS n FROM incidencia WHERE resuelta = 0 GROUP BY severidad")
    por_severidad = {"critica": 0, "grave": 0, "leve": 0}
    for f in filas:
        por_severidad[f["severidad"]] = f["n"]
    return {"total": sum(por_severidad.values()), "por_severidad": por_severidad}


def fallos_por_hora(horas: int = 24) -> list[dict]:
    desde = (tiempo.ahora_dt() - timedelta(hours=horas)).isoformat(timespec="seconds")
    filas = db.consultar(
        """
        SELECT strftime('%Y-%m-%dT%H', ocurrido_en) AS hora, COUNT(*) AS n
        FROM incidencia WHERE ocurrido_en >= ?
        GROUP BY hora
        """,
        (desde,),
    )
    conteo = {f["hora"]: f["n"] for f in filas}
    ahora = tiempo.ahora_dt().replace(minute=0, second=0, microsecond=0)
    serie = []
    for i in range(horas - 1, -1, -1):
        h = ahora - timedelta(hours=i)
        clave = h.strftime("%Y-%m-%dT%H")
        serie.append({"hora": h.strftime("%H:00"), "fallos": conteo.get(clave, 0)})
    return serie


# ===========================================================================
#  PANEL 3 - FIDELIZACION
# ===========================================================================

def resumen_fidelizacion() -> dict:
    c = db.consultar_uno(
        """
        SELECT COUNT(*) AS clientes,
               SUM(CASE WHEN total_viajes >= 2 THEN 1 ELSE 0 END) AS recurrentes,
               SUM(CASE WHEN segmento = 'frecuente' THEN 1 ELSE 0 END) AS frecuentes,
               SUM(CASE WHEN segmento = 'dormido'   THEN 1 ELSE 0 END) AS dormidos,
               MAX(total_viajes) AS max_viajes
        FROM cliente WHERE total_viajes > 0
        """
    )
    cod = db.consultar_uno(
        """
        SELECT COUNT(*) AS enviados,
               SUM(CASE WHEN canjeado_en IS NOT NULL THEN 1 ELSE 0 END) AS canjeados
        FROM codigo_enviado WHERE estado_envio = 'enviado'
        """
    )
    clientes = c["clientes"] or 0
    enviados = cod["enviados"] or 0
    canjeados = cod["canjeados"] or 0
    return {
        "clientes": clientes,
        "recurrentes": c["recurrentes"] or 0,
        "tasa_recompra": round((c["recurrentes"] or 0) / clientes * 100, 1) if clientes else 0,
        "frecuentes": c["frecuentes"] or 0,
        "dormidos": c["dormidos"] or 0,
        "max_viajes": c["max_viajes"] or 0,
        "codigos_enviados": enviados,
        "codigos_canjeados": canjeados,
        "tasa_canje": round(canjeados / enviados * 100, 1) if enviados else 0,
        "umbrales": {"frecuente": VIAJES_FRECUENTE, "ocasional": VIAJES_OCASIONAL,
                     "dias_dormido": DIAS_DORMIDO},
    }


def top_clientes(limite: int = 20, segmento: str | None = None,
                 ordenar_por: str = "viajes") -> list[dict]:
    """
    ordenar_por='viajes'  → ranking por total de boletos (más frecuentes).
    ordenar_por='gasto'   → ranking por total gastado en soles (más valiosos).
    """
    orden = (
        "total_gastado DESC, total_viajes DESC"
        if ordenar_por == "gasto"
        else "total_viajes DESC, total_gastado DESC"
    )
    sql = """
        SELECT documento, nombres, paterno, materno, telefono,
               total_viajes, total_gastado, ultima_compra, segmento
        FROM cliente WHERE total_viajes > 0
    """
    params: list = []
    if segmento:
        sql += " AND segmento = ?"
        params.append(segmento)
    sql += f" ORDER BY {orden} LIMIT ?"
    params.append(limite)

    salida = []
    for f in db.consultar(sql, tuple(params)):
        d = dict(f)
        d["nombre"] = " ".join(x for x in [d.get("nombres"), d.get("paterno"), d.get("materno")] if x)
        d["documento_visible"] = f"•••{d['documento'][-5:]}" if d.get("documento") else "—"
        d["total_gastado"] = round(d["total_gastado"] or 0, 2)
        salida.append(d)
    return salida


def cadena_booking(booking_id: str) -> dict:
    """
    Devuelve la sesión, todos los eventos cronológicos y la venta (si existe)
    para un booking_id. Para la vista de cadena en el panel de incidencias.
    """
    sesion = db.consultar_uno(
        "SELECT * FROM sesion_compra WHERE booking_id = ?", (booking_id,)
    )
    eventos = db.consultar(
        """
        SELECT paso, ocurrido_en, detalle
        FROM evento WHERE booking_id = ?
        ORDER BY ocurrido_en
        """,
        (booking_id,),
    )
    venta = db.consultar_uno(
        "SELECT * FROM venta WHERE booking_id = ?", (booking_id,)
    )
    return {
        "sesion": dict(sesion) if sesion else None,
        "eventos": db.filas_a_dict(eventos),
        "venta": dict(venta) if venta else None,
    }


def distribucion_frecuencia() -> list[dict]:
    filas = db.consultar(
        """
        SELECT CASE WHEN total_viajes >= 5 THEN '5+' ELSE CAST(total_viajes AS TEXT) END AS viajes,
               COUNT(*) AS clientes
        FROM cliente WHERE total_viajes > 0
        GROUP BY viajes ORDER BY MIN(total_viajes)
        """
    )
    return db.filas_a_dict(filas)


def codigos(estado: str | None = None) -> list[dict]:
    sql = """
        SELECT c.codigo, c.descripcion, c.tipo, c.valor, c.segmento_objetivo,
               c.vence_en, c.estado, c.creado_en,
               (SELECT COUNT(*) FROM codigo_enviado e
                 WHERE e.codigo = c.codigo AND e.estado_envio = 'enviado') AS enviados,
               (SELECT COUNT(*) FROM codigo_enviado e
                 WHERE e.codigo = c.codigo AND e.canjeado_en IS NOT NULL) AS canjeados,
               (SELECT COALESCE(SUM(v.monto_total), 0) FROM codigo_enviado e
                 JOIN venta v ON v.id = e.venta_id WHERE e.codigo = c.codigo) AS ingreso_generado
        FROM codigo_descuento c
        WHERE c.documento_exclusivo IS NULL
    """
    params: tuple = ()
    if estado:
        sql += " AND c.estado = ?"
        params = (estado,)
    sql += " ORDER BY c.creado_en DESC"
    return db.filas_a_dict(db.consultar(sql, params))


# ===========================================================================
#  VENTA EN CURSO (las usa api_ventas.py para /confirmar y el PDF)
# ===========================================================================

def pago_de_booking(booking_id: str) -> dict | None:
    """El cobro registrado para esta venta (tabla pago: Yupy o Niubiz), o None."""
    return db.fila_a_dict(db.consultar_uno("SELECT * FROM pago WHERE booking_id = ?", (booking_id,)))


def booking_de_orden_yupy(payment_order_uid: str) -> str | None:
    """La venta cuyo cobro (actual o de un checkout anterior) es esta orden de Yupy."""
    fila = db.consultar_uno(
        """SELECT booking_id FROM pago
           WHERE yupy_transaction_id = ?
              OR EXISTS (SELECT 1 FROM json_each(COALESCE(transacciones_anteriores, '[]')) WHERE value = ?)""",
        (payment_order_uid, payment_order_uid),
    )
    return fila[0] if fila else None


def venta_de_booking(booking_id: str) -> dict | None:
    """La ultima venta registrada para este booking_id, con sus boletos, o None."""
    return _con_boletos(db.fila_a_dict(db.consultar_uno(
        "SELECT * FROM venta WHERE booking_id = ? ORDER BY id DESC LIMIT 1", (booking_id,)
    )))


def venta_por_id(venta_id: int) -> dict | None:
    """La venta con ese id (el del enlace firmado del PDF), con sus boletos, o None."""
    return _con_boletos(db.fila_a_dict(db.consultar_uno("SELECT * FROM venta WHERE id = ?", (venta_id,))))


def _con_boletos(venta: dict | None) -> dict | None:
    if venta is None:
        return None
    venta["boletos"] = db.filas_a_dict(db.consultar(
        "SELECT documento, nombres, paterno, materno, asiento, precio, nro_boleto, link_comprobante "
        "FROM boleto WHERE venta_id = ? ORDER BY id", (venta["id"],)
    ))
    return venta


# ===========================================================================
#  INCIDENCIAS - contacto, atenciones y casos atendidos
# ===========================================================================

def contacto_de(telefono: str | None) -> dict | None:
    """
    Lo que se sabe de quien escribio: perfil de WhatsApp, cuando escribio por
    ultima vez (de eso depende si se le puede mandar un mensaje normal: 24 h)
    y su historial como cliente. El documento va enmascarado.
    """
    if not telefono:
        return None
    c = db.fila_a_dict(db.consultar_uno(
        "SELECT nombre_wa, primer_mensaje, ultimo_mensaje, mensajes, enlaces_enviados "
        "FROM contacto WHERE telefono = ?", (telefono,)))
    cli = db.fila_a_dict(db.consultar_uno(
        """
        SELECT documento, nombres, paterno, materno, total_viajes, total_gastado,
               ultima_compra, segmento
        FROM cliente WHERE telefono = ? ORDER BY ultima_compra DESC LIMIT 1
        """, (telefono,)))
    ventana = None
    if c and c.get("ultimo_mensaje"):
        try:
            ventana = (datetime.fromisoformat(c["ultimo_mensaje"]) + timedelta(hours=24)).isoformat(timespec="seconds")
        except ValueError:
            ventana = None
    if cli:
        cli["nombre"] = " ".join(x for x in [cli.get("nombres"), cli.get("paterno"), cli.get("materno")] if x)
        cli["documento_visible"] = privacidad.documento(cli.pop("documento"))
        cli["total_gastado"] = round(cli["total_gastado"] or 0, 2)
    return {"telefono": telefono, "whatsapp": c, "ventana_hasta": ventana, "cliente": cli}


def pasajeros_de_venta(venta_id: int | None) -> list[dict]:
    if not venta_id:
        return []
    filas = db.consultar(
        "SELECT nombres, paterno, materno, documento, asiento, nro_boleto FROM boleto WHERE venta_id = ? ORDER BY asiento",
        (venta_id,))
    salida = []
    for f in filas:
        d = dict(f)
        d["nombre"] = " ".join(x for x in [d.pop("nombres"), d.pop("paterno"), d.pop("materno")] if x)
        d["documento"] = privacidad.documento(d["documento"])
        salida.append(d)
    return salida


def atenciones_de(incidencia_id: int) -> list[dict]:
    return db.filas_a_dict(db.consultar(
        """
        SELECT id, accion, resultado, canal_contacto, nota, nro_boleto, booking_nuevo, venta_id,
               referencia_devolucion, monto_devuelto_centimos, derivada_a, atendido_por, creado_en
        FROM atencion WHERE incidencia_id = ? ORDER BY creado_en, id
        """, (incidencia_id,)))


def casos_atendidos(desde: str, hasta: str, *, accion: str | None = None,
                    resultado: str | None = None, pagina: int = 1, por_pagina: int = 25) -> dict:
    """Registro de todas las acciones tomadas, paginado, con su incidencia."""
    filtros, params = ["a.creado_en BETWEEN ? AND ?"], [desde, hasta]
    if accion:
        filtros.append("a.accion = ?")
        params.append(accion)
    if resultado:
        filtros.append("a.resultado = ?")
        params.append(resultado)
    donde = " AND ".join(filtros)
    total = db.consultar_uno(f"SELECT COUNT(*) AS n FROM atencion a WHERE {donde}", tuple(params))["n"]
    filas = db.consultar(
        f"""
        SELECT a.id, a.incidencia_id, a.accion, a.resultado, a.canal_contacto, a.nota,
               a.nro_boleto, a.booking_nuevo, a.venta_id, a.referencia_devolucion,
               a.monto_devuelto_centimos, a.derivada_a, a.creado_en,
               i.tipo, i.severidad, i.ocurrido_en, i.estado AS estado_incidencia, i.booking_id
        FROM atencion a JOIN incidencia i ON i.id = a.incidencia_id
        WHERE {donde}
        ORDER BY a.creado_en DESC, a.id DESC
        LIMIT ? OFFSET ?
        """, (*params, por_pagina, (pagina - 1) * por_pagina))
    por_accion = db.filas_a_dict(db.consultar(
        "SELECT accion, COUNT(*) AS n FROM atencion WHERE creado_en BETWEEN ? AND ? "
        "GROUP BY accion ORDER BY n DESC", (desde, hasta)))
    cierre = db.consultar_uno(
        """
        SELECT COUNT(*) AS cerradas,
               AVG((julianday(resuelta_en) - julianday(ocurrido_en)) * 24) AS horas_promedio,
               SUM(CASE WHEN estado = 'derivada' THEN 1 ELSE 0 END) AS derivadas
        FROM incidencia
        WHERE estado IN ('resuelta', 'derivada') AND resuelta_en BETWEEN ? AND ?
        """, (desde, hasta))
    seguimiento = db.consultar_uno(
        "SELECT COUNT(*) AS n FROM incidencia WHERE estado = 'en_seguimiento'")["n"]
    # Los indicadores describen todo el rango; los filtros solo cambian la tabla.
    en_rango = db.consultar_uno(
        "SELECT COUNT(*) AS n, COALESCE(SUM(monto_devuelto_centimos), 0) AS c "
        "FROM atencion WHERE creado_en BETWEEN ? AND ?", (desde, hasta))
    return {
        "total": total, "pagina": pagina, "por_pagina": por_pagina,
        "filas": db.filas_a_dict(filas),
        "por_accion": por_accion,
        "resumen": {
            "acciones": en_rango["n"],
            "cerradas": cierre["cerradas"] or 0,
            "derivadas": cierre["derivadas"] or 0,
            "horas_promedio": round(cierre["horas_promedio"], 1) if cierre["horas_promedio"] is not None else None,
            "en_seguimiento": seguimiento,
            "devuelto_centimos": en_rango["c"],
        },
    }


# ===========================================================================
#  INCIDENCIAS - abandonos y caidas
# ===========================================================================

def abandonos(desde: str, hasta: str) -> dict:
    """
    Donde se quedan las compras que no terminan, como evoluciona por dia y que
    errores aparecen en cada paso del flujo.
    """
    orden = {p: i for i, p in enumerate(PASOS)}
    donde = db.filas_a_dict(db.consultar(
        """
        SELECT paso_maximo AS paso, COUNT(*) AS sesiones,
               SUM(CASE WHEN estado IN ('fallida', 'rechazada') THEN 1 ELSE 0 END) AS con_error
        FROM sesion_compra
        WHERE abierta_en BETWEEN ? AND ? AND estado NOT IN ('emitida', 'en_curso')
        GROUP BY paso_maximo
        """, (desde, hasta)))
    donde.sort(key=lambda x: orden.get(x["paso"], 99))
    en_curso = db.consultar_uno(
        "SELECT COUNT(*) AS n FROM sesion_compra WHERE abierta_en BETWEEN ? AND ? AND estado = 'en_curso'",
        (desde, hasta))["n"]
    por_dia = db.filas_a_dict(db.consultar(
        """
        SELECT substr(abierta_en, 1, 10) AS dia, COUNT(*) AS sesiones,
               SUM(CASE WHEN estado = 'emitida' THEN 1 ELSE 0 END) AS emitidas,
               SUM(CASE WHEN estado = 'abandonada' THEN 1 ELSE 0 END) AS abandonadas,
               SUM(CASE WHEN estado IN ('fallida', 'rechazada') THEN 1 ELSE 0 END) AS con_error
        FROM sesion_compra WHERE abierta_en BETWEEN ? AND ?
        GROUP BY dia ORDER BY dia
        """, (desde, hasta)))
    errores = db.filas_a_dict(db.consultar(
        """
        SELECT COALESCE(s.paso_maximo, 'sin_compra') AS paso, i.tipo, i.severidad, COUNT(*) AS n
        FROM incidencia i LEFT JOIN sesion_compra s ON s.booking_id = i.booking_id
        WHERE i.ocurrido_en BETWEEN ? AND ?
        GROUP BY paso, i.tipo, i.severidad
        """, (desde, hasta)))
    errores.sort(key=lambda x: (orden.get(x["paso"], 99), -x["n"]))
    return {"donde": donde, "en_curso": en_curso, "por_dia": por_dia, "errores_por_paso": errores}


# ===========================================================================
#  FIDELIZACION - top compradores y bonos
# ===========================================================================

def top_compradores(*, dias: int = 90, ordenar_por: str = "boletos", limite: int = 50) -> list[dict]:
    """
    Ranking por boletos o por gasto dentro del periodo (dias=0: todo el
    historico). A diferencia de top_clientes, mira solo las compras del
    periodo, no los totales de toda la vida.
    """
    orden = "gasto DESC, boletos DESC" if ordenar_por == "gasto" else "boletos DESC, gasto DESC"
    filtro, params = "", []
    if dias:
        filtro = "AND v.comprado_en >= ?"
        params.append(f"{(tiempo.hoy() - timedelta(days=dias - 1)).isoformat()}T00:00:00")
    filas = db.filas_a_dict(db.consultar(
        f"""
        SELECT b.documento, COUNT(*) AS boletos, COUNT(DISTINCT v.id) AS compras,
               COALESCE(SUM(b.precio), 0) AS gasto, MAX(v.comprado_en) AS ultima
        FROM boleto b JOIN venta v ON v.id = b.venta_id
        WHERE v.estado = 'emitida' AND b.documento IS NOT NULL {filtro}
        GROUP BY b.documento ORDER BY {orden} LIMIT ?
        """, (*params, limite)))
    if not filas:
        return []
    docs = [f["documento"] for f in filas]
    marcas = ",".join("?" * len(docs))
    clientes = {f["documento"]: dict(f) for f in db.consultar(
        f"SELECT documento, nombres, paterno, materno, telefono, segmento, total_viajes "
        f"FROM cliente WHERE documento IN ({marcas})", tuple(docs))}
    rutas = {f["documento"]: f["ruta"] for f in db.consultar(
        f"""
        SELECT documento, ruta FROM (
            SELECT b.documento, v.origen || ' → ' || v.destino AS ruta,
                   ROW_NUMBER() OVER (PARTITION BY b.documento
                                      ORDER BY COUNT(*) DESC, MAX(v.comprado_en) DESC) AS rn
            FROM boleto b JOIN venta v ON v.id = b.venta_id
            WHERE v.estado = 'emitida' AND b.documento IN ({marcas}) {filtro}
            GROUP BY b.documento, ruta
        ) WHERE rn = 1
        """, (*docs, *params))}
    hoy = tiempo.hoy().isoformat()
    con_bono = {f["documento_exclusivo"]: f["codigo"] for f in db.consultar(
        f"""
        SELECT c.documento_exclusivo, c.codigo FROM codigo_descuento c
        WHERE c.documento_exclusivo IN ({marcas}) AND c.estado = 'activo'
          AND (c.vence_en IS NULL OR c.vence_en >= ?)
          AND NOT EXISTS (SELECT 1 FROM codigo_enviado e WHERE e.codigo = c.codigo AND e.canjeado_en IS NOT NULL)
        """, (*docs, hoy))}
    salida = []
    for i, f in enumerate(filas, 1):
        c = clientes.get(f["documento"], {})
        salida.append({
            "puesto": i,
            # El documento completo solo sirve para "Crear bono"; en pantalla va enmascarado.
            "documento": f["documento"],
            "documento_visible": privacidad.documento(f["documento"]),
            "nombre": " ".join(x for x in [c.get("nombres"), c.get("paterno"), c.get("materno")] if x) or None,
            "telefono_visible": privacidad.telefono(c.get("telefono")) if c.get("telefono") else None,
            "segmento": c.get("segmento"),
            "boletos": f["boletos"], "compras": f["compras"],
            "gasto": round(f["gasto"], 2),
            "promedio": round(f["gasto"] / f["boletos"], 2) if f["boletos"] else 0,
            "ultima": f["ultima"], "ruta_favorita": rutas.get(f["documento"]),
            "viajes_totales": c.get("total_viajes"),
            "bono_activo": con_bono.get(f["documento"]),
        })
    return salida


def destinatarios_lote(grupo: dict) -> list[dict]:
    """Clientes a los que se les crearia un lote de bonos ({documento, telefono})."""
    if grupo["tipo"] == "top":
        top = top_compradores(dias=grupo.get("dias", 90), ordenar_por=grupo.get("orden", "boletos"),
                              limite=grupo.get("n", 50))
        docs = [t["documento"] for t in top]
    elif grupo["tipo"] == "segmento":
        docs = [f["documento"] for f in db.consultar(
            "SELECT documento FROM cliente WHERE segmento = ? AND total_viajes > 0 "
            "ORDER BY total_viajes DESC LIMIT 2000", (grupo["segmento"],))]
    else:
        docs = list(dict.fromkeys(grupo.get("documentos") or []))
    if not docs:
        return []
    filas = db.consultar(
        f"SELECT documento, telefono FROM cliente WHERE documento IN ({','.join('?' * len(docs))})",
        tuple(docs))
    return db.filas_a_dict(filas)


_ESTADO_BONO_SQL = """
    CASE
        WHEN EXISTS (SELECT 1 FROM codigo_enviado e WHERE e.codigo = c.codigo AND e.canjeado_en IS NOT NULL) THEN 'canjeado'
        WHEN c.estado = 'anulado' THEN 'anulado'
        WHEN c.vence_en IS NOT NULL AND c.vence_en < :hoy THEN 'vencido'
        ELSE 'vigente'
    END
"""


def bonos_panel(*, estado: str | None = None, origen: str | None = None,
                pagina: int = 1, por_pagina: int = 25) -> dict:
    """Bonos personales con su estado calculado, reglas y ultimos lotes."""
    hoy = tiempo.hoy().isoformat()
    base = f"""
        SELECT c.codigo, c.descripcion, c.tipo, c.valor, c.tope_descuento, c.vence_en, c.creado_en,
               c.documento_exclusivo, c.regla_id, c.lote_id,
               {_ESTADO_BONO_SQL} AS estado_bono,
               (SELECT e.estado_envio FROM codigo_enviado e WHERE e.codigo = c.codigo LIMIT 1) AS envio,
               cl.nombres, cl.paterno
        FROM codigo_descuento c LEFT JOIN cliente cl ON cl.documento = c.documento_exclusivo
        WHERE c.documento_exclusivo IS NOT NULL
    """
    params: dict = {"hoy": hoy}
    if origen == "regla":
        base += " AND c.regla_id IS NOT NULL"
    elif origen == "lote":
        base += " AND c.lote_id IS NOT NULL"
    consulta = f"SELECT * FROM ({base})"
    if estado:
        consulta += " WHERE estado_bono = :estado"
        params["estado"] = estado
    total = db.consultar_uno(f"SELECT COUNT(*) AS n FROM ({consulta})", params)["n"]
    filas = db.filas_a_dict(db.consultar(
        consulta + " ORDER BY creado_en DESC LIMIT :lim OFFSET :off",
        {**params, "lim": por_pagina, "off": (pagina - 1) * por_pagina}))
    for f in filas:
        f["documento_visible"] = privacidad.documento(f.pop("documento_exclusivo"))
        f["nombre"] = " ".join(x for x in [f.pop("nombres"), f.pop("paterno")] if x) or None
    conteo = {f["estado_bono"]: f["n"] for f in db.consultar(
        f"SELECT {_ESTADO_BONO_SQL} AS estado_bono, COUNT(*) AS n FROM codigo_descuento c "
        "WHERE c.documento_exclusivo IS NOT NULL GROUP BY estado_bono", {"hoy": hoy})}
    por_vencer = db.consultar_uno(
        f"SELECT COUNT(*) AS n FROM codigo_descuento c WHERE c.documento_exclusivo IS NOT NULL "
        f"AND ({_ESTADO_BONO_SQL}) = 'vigente' AND c.vence_en IS NOT NULL AND c.vence_en <= :limite",
        {"hoy": hoy, "limite": (tiempo.hoy() + timedelta(days=7)).isoformat()})["n"]
    reglas = db.filas_a_dict(db.consultar(
        """
        SELECT r.*, (SELECT COUNT(*) FROM codigo_descuento c WHERE c.regla_id = r.id) AS bonos_creados,
               (SELECT COUNT(*) FROM codigo_descuento c JOIN codigo_enviado e ON e.codigo = c.codigo
                 WHERE c.regla_id = r.id AND e.canjeado_en IS NOT NULL) AS bonos_canjeados
        FROM regla_bono r ORDER BY r.activa DESC, r.id DESC
        """))
    lotes = db.filas_a_dict(db.consultar(
        "SELECT id, nombre, filtro, cantidad, creado_en FROM lote_bono ORDER BY id DESC LIMIT 10"))
    return {
        "resumen": {"vigentes": conteo.get("vigente", 0), "canjeados": conteo.get("canjeado", 0),
                    "vencidos": conteo.get("vencido", 0), "anulados": conteo.get("anulado", 0),
                    "por_vencer_7_dias": por_vencer},
        "total": total, "pagina": pagina, "por_pagina": por_pagina,
        "bonos": filas, "reglas": reglas, "lotes": lotes,
    }


# --- Administración: ciudades y pasarelas fuera de la venta ------------------ #
# Sin @seguro a propósito: api_ventas decide qué hacer si la base falla (vende
# como si no hubiera nada cerrado, regla 5) y el panel muestra el error.
def ciudades_cerradas() -> dict[str, dict]:
    """Ciudades que el panel sacó de la venta: {codigo: {motivo, cerrada_en}}."""
    return {f["codigo"]: {"motivo": f["motivo"], "cerrada_en": f["cerrada_en"]}
            for f in db.consultar("SELECT codigo, motivo, cerrada_en FROM ciudad_cerrada")}


def pasarelas_apagadas() -> dict[str, dict]:
    """Medios de pago apagados desde el panel: {medio: {motivo, apagada_en}}."""
    return {f["medio"]: {"motivo": f["motivo"], "apagada_en": f["apagada_en"]}
            for f in db.consultar("SELECT medio, motivo, apagada_en FROM pasarela_apagada")}
