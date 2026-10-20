"""Atención de incidencias (acciones, solución indexada, seguimiento) y bonos personales (reglas y lotes)."""

from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from paneles import atencion, bonos, consultas, db, fidelizacion, registro, tiempo
from paneles.api_paneles import router

CLAVE = {"X-Paneles-Token": "clave-panel-prueba"}


@pytest.fixture
def panel():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _venta(booking: str, documento: str, *, precio: float = 100, nro: str | None = None) -> int:
    return registro.registrar_venta(
        booking_id=booking, origen="TACNA", destino="LIMA", monto_total=precio, nro_boleto=nro,
        telefono="51987654321",
        pasajeros=[{"documento": documento, "nombres": "Rosa", "paterno": "Quispe",
                    "asiento": "05", "precio": precio, "nro_boleto": nro}],
    )


def _incidencia(tipo: str, booking: str | None = "b-incidencia-1") -> int:
    return registro.registrar_incidencia(tipo, "mensaje de prueba", booking_id=booking,
                                         telefono="51987654321")


# --- Atención: reglas puras --------------------------------------------------- #
def test_un_cobro_sin_boleto_no_se_cierra_sin_dejar_resuelto_el_dinero():
    acciones = atencion.acciones_para("cobrado_sin_emitir", "critica")
    assert "desistio" not in acciones and "sin_accion" not in acciones
    with pytest.raises(ValueError, match="número del boleto"):
        atencion.validar("emitio_manual", "cobrado_sin_emitir", "critica", {"nota": "x"})
    with pytest.raises(ValueError, match="nota es obligatoria"):
        atencion.validar("emitio_manual", "cobrado_sin_emitir", "critica", {"nro_boleto": "B001-9"})
    assert atencion.validar("devolucion", "cobrado_sin_emitir", "critica",
                            {"referencia_devolucion": "YP-1", "monto_devuelto_centimos": 9500,
                             "nota": "devuelto"}) == "resuelta"


def test_asiento_ocupado_ofrece_otro_asiento_o_desiste():
    acciones = atencion.acciones_para("asiento_no_disponible", "leve")
    assert acciones[:3] == ["ofrecio_asiento", "compro_otro", "desistio"]
    assert atencion.validar("ofrecio_asiento", "asiento_no_disponible", "leve", {}) == "en_seguimiento"
    with pytest.raises(ValueError, match="Esa acción no aplica"):
        atencion.validar("devolucion", "asiento_no_disponible", "leve", {})


# --- Atención: registro y seguimiento ---------------------------------------- #
def test_la_solucion_queda_indexada_a_la_venta_nueva_y_hay_seguimiento():
    inc = _incidencia("asiento_no_disponible")
    r1 = registro.registrar_atencion(inc, "ofrecio_asiento", {"canal_contacto": "whatsapp"})
    assert r1["resultado"] == "en_seguimiento"
    # Sigue en la bandeja, marcada en seguimiento.
    abiertas = consultas.incidencias_abiertas()
    assert [(i["id"], i["estado"]) for i in abiertas] == [(inc, "en_seguimiento")]

    venta_id = _venta("b-compra-nueva-1", "70000001", nro="B001-0007")
    r2 = registro.registrar_atencion(inc, "compro_otro", {"booking_nuevo": "b-compra-nueva-1"})
    assert r2 == {"id": r2["id"], "resultado": "resuelta", "venta_vinculada": True}
    assert consultas.incidencias_abiertas() == []
    historial = consultas.atenciones_de(inc)
    assert [a["accion"] for a in historial] == ["ofrecio_asiento", "compro_otro"]
    assert historial[1]["venta_id"] == venta_id
    fila = db.consultar_uno("SELECT estado, resuelta, nota_resolucion FROM incidencia WHERE id = ?", (inc,))
    assert fila["estado"] == "resuelta" and fila["resuelta"] == 1
    assert "Compró otro asiento" in fila["nota_resolucion"]


def test_un_boleto_hecho_a_mano_en_jelaf_se_registra_aunque_no_este_en_la_base():
    inc = _incidencia("cobrado_sin_emitir")
    r = registro.registrar_atencion(inc, "emitio_manual", {"nro_boleto": "B009-1234", "nota": "Emitido en boletería"})
    assert r["resultado"] == "resuelta" and r["venta_vinculada"] is False
    # El boleto de una venta del sistema sí se vincula, aunque se busque por número.
    venta_id = _venta("b-otra-venta-1", "70000002", nro="B001-5555")
    inc2 = _incidencia("fallo_emision", "b-incidencia-2")
    assert registro.registrar_atencion(inc2, "emitio_manual",
                                       {"nro_boleto": "B001-5555", "nota": "ok"})["venta_vinculada"] is True
    assert consultas.atenciones_de(inc2)[0]["venta_id"] == venta_id


def test_api_de_atencion_valida_y_no_reabre_una_cerrada(panel):
    inc = _incidencia("cobrado_sin_emitir")
    url = f"/api/paneles/incidencias/{inc}/atencion"
    assert panel.post(url, json={"accion": "devolucion"}).status_code == 401
    r = panel.post(url, json={"accion": "desistio", "nota": "x"}, headers=CLAVE)
    assert r.status_code == 422 and "no aplica" in r.json()["detail"]
    r = panel.post(url, json={"accion": "devolucion", "referencia_devolucion": "YP-77",
                              "monto_devuelto_centimos": 12000, "nota": "Devuelto en Yupy"}, headers=CLAVE)
    assert r.status_code == 200 and r.json()["resultado"] == "resuelta"
    assert panel.post(url, json={"accion": "nota", "nota": "otra"}, headers=CLAVE).status_code == 409
    assert panel.post("/api/paneles/incidencias/99999/atencion", json={"accion": "nota", "nota": "x"},
                      headers=CLAVE).status_code == 404


def test_la_ficha_trae_contacto_pasajeros_y_acciones(panel):
    _venta("b-incidencia-1", "71234567")
    registro.registrar_mensaje("51987654321", "Rosa Q.")
    inc = _incidencia("cobrado_sin_emitir")
    d = panel.get(f"/api/paneles/incidencias/{inc}/cadena", headers=CLAVE).json()
    assert d["contacto"]["whatsapp"]["nombre_wa"] == "Rosa Q."
    assert d["contacto"]["ventana_hasta"]                       # escribió hace poco
    assert d["contacto"]["cliente"]["documento_visible"] == "*****567"
    assert d["pasajeros"][0]["documento"] == "*****567"         # nunca el DNI completo
    assert [a["accion"] for a in d["acciones"]][:3] == ["emitio_manual", "compro_otro", "devolucion"]


def test_resolver_y_derivar_heredados_pasan_por_la_atencion(panel):
    a, b = _incidencia("pool_ocupado", None), _incidencia("jelaf_caido", None)
    assert panel.post(f"/api/paneles/incidencias/{a}/resolver", json={"nota": "ok"}, headers=CLAVE).status_code == 200
    assert panel.post(f"/api/paneles/incidencias/{b}/derivar", json={"motivo": "caído", "derivada_a": "Soporte JELAF"},
                      headers=CLAVE).status_code == 200
    estados = dict(db.consultar("SELECT id, estado FROM incidencia"))
    assert estados == {a: "resuelta", b: "derivada"}
    casos = panel.get("/api/paneles/atenciones", headers=CLAVE).json()
    assert casos["total"] == 2 and casos["resumen"]["derivadas"] == 1


def test_una_base_vieja_pasa_sus_incidencias_cerradas_al_estado_nuevo(tmp_path, monkeypatch):
    ruta = tmp_path / "vieja.db"
    con = sqlite3.connect(ruta)
    con.executescript("""
        CREATE TABLE incidencia (id INTEGER PRIMARY KEY AUTOINCREMENT, booking_id TEXT, telefono TEXT,
            tipo TEXT NOT NULL, severidad TEXT NOT NULL, mensaje TEXT NOT NULL, contexto TEXT,
            ocurrido_en TEXT NOT NULL, resuelta INTEGER NOT NULL DEFAULT 0, resuelta_en TEXT, nota_resolucion TEXT);
        INSERT INTO incidencia (tipo, severidad, mensaje, ocurrido_en, resuelta, nota_resolucion) VALUES
            ('jelaf_caido', 'grave', 'm', '2026-09-01T10:00:00', 1, 'DERIVADA: caído → Soporte'),
            ('pool_ocupado', 'leve', 'm', '2026-09-01T10:00:00', 1, 'ok'),
            ('pool_ocupado', 'leve', 'm', '2026-09-01T10:00:00', 0, NULL);
    """)
    con.commit()
    con.close()
    monkeypatch.setattr(db, "RUTA_DB", ruta)
    db.init_db()
    db.init_db()
    assert [f[0] for f in db.consultar("SELECT estado FROM incidencia ORDER BY id")] == ["derivada", "resuelta", "abierta"]


# --- Bonos: lógica pura -------------------------------------------------------- #
def test_codigo_unico_con_prefijo_legible():
    codigo = bonos.generar_codigo("vuelve 10!")
    assert re.fullmatch(r"VUELVE10-[ABCDEFGHJKMNPQRSTUVWXYZ23456789]{5}", codigo)
    assert len({bonos.generar_codigo("VUELVE10") for _ in range(200)}) == 200
    with pytest.raises(ValueError):
        bonos.normalizar_prefijo("-")


def test_hito_de_cada_disparador():
    hoy = date(2026, 9, 30)
    cada5 = {"disparador": "viajes", "umbral": 5, "repetir": 1}
    assert bonos.hito(cada5, {"total_viajes": 4}, hoy) is None
    assert bonos.hito(cada5, {"total_viajes": 7}, hoy) == "1"
    assert bonos.hito(cada5, {"total_viajes": 10}, hoy) == "2"
    assert bonos.hito({**cada5, "repetir": 0}, {"total_viajes": 10}, hoy) == "1"
    gasto = {"disparador": "gasto", "umbral": 50000, "repetir": 0}          # S/ 500
    assert bonos.hito(gasto, {"total_gastado": 499.99}, hoy) is None
    assert bonos.hito(gasto, {"total_gastado": 500}, hoy) == "1"
    dormido = {"disparador": "dormido", "umbral": 60}
    assert bonos.hito(dormido, {"ultima_compra": "2026-08-15T10:00:00"}, hoy) is None
    assert bonos.hito(dormido, {"ultima_compra": "2026-07-01T10:00:00"}, hoy) == "2026-07-01"


# --- Bonos: reglas automáticas -------------------------------------------------- #
def _regla(**cambios) -> int:
    datos = {"nombre": "Cada 2 viajes", "disparador": "viajes", "umbral": 2, "repetir": True,
             "tipo": "porcentaje", "valor": 10, "vigencia_dias": 30, "prefijo": "VUELVE10"} | cambios
    return registro.guardar_regla(datos)


def _bonos_de(documento: str) -> list[dict]:
    return db.filas_a_dict(db.consultar(
        "SELECT codigo, hito, regla_id, lote_id FROM codigo_descuento WHERE documento_exclusivo = ? ORDER BY hito",
        (documento,)))


def test_una_regla_inactiva_no_crea_bonos_al_vender():
    _regla()
    _venta("b-r-0001", "80000001")
    _venta("b-r-0002", "80000001")
    assert _bonos_de("80000001") == []


def test_la_regla_crea_un_bono_por_hito_al_vender_y_es_idempotente():
    regla = _regla()
    registro.activar_regla(regla, True)
    _venta("b-r-0011", "80000002")
    assert _bonos_de("80000002") == []                       # 1 viaje: todavía no
    _venta("b-r-0012", "80000002")
    assert [b["hito"] for b in _bonos_de("80000002")] == ["1"]
    assert registro.aplicar_reglas()["total"] == 0           # volver a aplicar no duplica
    _venta("b-r-0013", "80000002")
    _venta("b-r-0014", "80000002")
    assert [b["hito"] for b in _bonos_de("80000002")] == ["1", "2"]
    envio = db.consultar_uno("SELECT estado_envio FROM codigo_enviado WHERE documento = '80000002' LIMIT 1")
    assert envio["estado_envio"] == "pendiente"              # creado, todavía no enviado


def test_simular_no_crea_y_dormidos_se_aplican_a_mano(monkeypatch):
    _venta("b-r-0021", "80000003")
    regla = _regla(nombre="Dormidos 60", disparador="dormido", umbral=60, repetir=False, prefijo="VUELVE15")
    registro.activar_regla(regla, True)
    monkeypatch.setattr(tiempo, "hoy", lambda: date.today() + timedelta(days=90))
    vista = registro.aplicar_reglas(regla_id=regla, simular=True)
    assert vista["total"] == 1 and _bonos_de("80000003") == []
    assert registro.aplicar_reglas(regla_id=regla, simular=False)["total"] == 1
    assert len(_bonos_de("80000003")) == 1


def test_si_las_reglas_fallan_la_venta_igual_se_registra(monkeypatch):
    _regla()

    def explota(**_):
        raise RuntimeError("regla rota")
    monkeypatch.setattr(registro, "aplicar_reglas", explota)
    antes = registro.fallos_registro()["total"]
    assert _venta("b-r-0031", "80000004") is not None
    assert registro.fallos_registro()["total"] == antes + 1


# --- Bonos: lotes, validación y top compradores ------------------------------ #
def test_lote_top_crea_un_codigo_distinto_por_cliente_y_solo_sirve_a_su_dueno():
    for i, doc in enumerate(["90000001", "90000002", "90000003"]):
        for k in range(i + 1):
            _venta(f"b-top-{doc}-{k}", doc)
    top = consultas.top_compradores(dias=0)
    assert [t["documento"] for t in top] == ["90000003", "90000002", "90000001"]
    assert top[0]["documento_visible"] == "*****003" and top[0]["boletos"] == 3

    destinatarios = consultas.destinatarios_lote({"tipo": "top", "n": 2, "dias": 0, "orden": "boletos"})
    r = registro.crear_lote("Top 2 septiembre", destinatarios, prefijo="TOP10", tipo="porcentaje",
                            valor=10, tope_centimos=None, vigencia_dias=15, filtro={"tipo": "top"})
    assert r["creados"] == 2
    codigos = {d: _bonos_de(d)[0]["codigo"] for d in ("90000003", "90000002")}
    assert len(set(codigos.values())) == 2 and _bonos_de("90000001") == []

    propio = fidelizacion.validar_codigo(codigos["90000003"], "90000003")
    ajeno = fidelizacion.validar_codigo(codigos["90000003"], "90000002")
    assert propio["valido"] is True
    assert ajeno == {"valido": False, "motivo": "Ese código no aplica a esta compra.", "codigo": None}

    registro.anular_bono(codigos["90000003"])
    assert fidelizacion.validar_codigo(codigos["90000003"], "90000003")["valido"] is False
    # Los bonos personales no llenan la lista de códigos generales.
    assert all(c["codigo"] not in codigos.values() for c in consultas.codigos())
    assert consultas.top_compradores(dias=0)[1]["bono_activo"] == codigos["90000002"]


def test_api_de_bonos_simula_por_defecto(panel):
    _venta("b-api-0001", "91000001")
    cuerpo = {"nombre": "Prueba", "prefijo": "VUELVE10", "valor": 10,
              "grupo": {"tipo": "top", "n": 50, "dias": 0}}
    vista = panel.post("/api/paneles/bonos/lotes", json=cuerpo, headers=CLAVE).json()
    assert vista["simulado"] is True and vista["clientes"] == 1 and _bonos_de("91000001") == []
    creado = panel.post("/api/paneles/bonos/lotes", json=cuerpo | {"simular": False}, headers=CLAVE).json()
    assert creado["creados"] == 1
    assert panel.post("/api/paneles/bonos/lotes", json=cuerpo | {"valor": 150}, headers=CLAVE).status_code == 422
    d = panel.get("/api/paneles/bonos", headers=CLAVE).json()
    assert d["resumen"]["vigentes"] == 1 and d["bonos"][0]["documento_visible"] == "*****001"
    regla = {"nombre": "Dormidos", "disparador": "dormido", "umbral": 60, "repetir": True,
             "valor": 15, "prefijo": "VUELVE15"}
    assert panel.post("/api/paneles/bonos/reglas", json=regla, headers=CLAVE).status_code == 422
    assert panel.get("/api/paneles/top-compradores").status_code == 401
    top = panel.get("/api/paneles/top-compradores?dias=90&orden=gasto", headers=CLAVE)
    assert top.status_code == 200 and top.json()["clientes"][0]["documento_visible"] == "*****001"


def test_abandonos_ordena_por_paso_del_flujo():
    for i, paso in enumerate(["pago_iniciado", "viaje_elegido", "viaje_elegido"]):
        registro.abrir_sesion(f"b-ab-{i:04d}")
        registro.registrar_evento(paso, booking_id=f"b-ab-{i:04d}")
        registro.cerrar_sesion(f"b-ab-{i:04d}", "abandonada")
    d, h = consultas.rango(dias=1)
    datos = consultas.abandonos(d, h)
    assert [(x["paso"], x["sesiones"]) for x in datos["donde"]] == [("viaje_elegido", 2), ("pago_iniciado", 1)]


def test_los_indicadores_de_casos_no_dependen_del_filtro_de_la_tabla(panel):
    a = _incidencia("pool_ocupado", None)
    registro.registrar_atencion(a, "sin_accion", {})
    filtrado = panel.get("/api/paneles/atenciones?resultado=derivada", headers=CLAVE).json()
    assert filtrado["total"] == 0                       # la tabla sí se filtra
    assert filtrado["resumen"]["acciones"] == 1         # el indicador cuenta todo el rango


# --- Bandeja de incidencias sin tope oculto (N58-SN) --------------------------- #
# Caso real: la #147 era la "16 de 20" y, al entrar incidencias nuevas, salió de
# la bandeja sin estar atendida (solo se traían las 20 más urgentes).
def _llenar_bandeja(leves: int = 25) -> int:
    for i in range(leves):
        _incidencia("sesion_liberada", booking=f"b-leve-{i}")
    critica = _incidencia("cobrado_sin_emitir", booking="b-critica")
    db.escribir("UPDATE incidencia SET ocurrido_en = '2020-01-01T00:00:00' WHERE id = ?", (critica,))
    return critica


def test_bandeja_trae_todas_las_abiertas_no_solo_20(panel):
    _llenar_bandeja(25)
    d = panel.get("/api/paneles/incidencias", headers=CLAVE).json()
    assert len(d["abiertas"]) == 26
    assert d["abiertas_total"] == 26
    assert d["abiertas_por_severidad"] == {"critica": 1, "grave": 0, "leve": 25}


def test_tope_de_la_bandeja_informa_el_total_real(panel, monkeypatch):
    from paneles import api_paneles
    _llenar_bandeja(25)
    monkeypatch.setattr(api_paneles, "MAX_ABIERTAS_BANDEJA", 5)
    d = panel.get("/api/paneles/incidencias", headers=CLAVE).json()
    assert len(d["abiertas"]) == 5 and d["abiertas_total"] == 26


def test_criticas_van_primero_aunque_sean_viejas(panel):
    critica = _llenar_bandeja(25)
    d = panel.get("/api/paneles/incidencias", headers=CLAVE).json()
    assert d["abiertas"][0]["id"] == critica


def test_en_seguimiento_sigue_en_la_bandeja(panel):
    inc = _incidencia("sesion_liberada")
    registro.registrar_atencion(inc, "ofrecio_asiento", {"canal_contacto": "whatsapp"})
    d = panel.get("/api/paneles/incidencias", headers=CLAVE).json()
    assert [(i["id"], i["estado"]) for i in d["abiertas"]] == [(inc, "en_seguimiento")]
    assert d["abiertas_total"] == 1
