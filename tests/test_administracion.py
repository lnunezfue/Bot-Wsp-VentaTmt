"""
Panel de Administración: ciudades fuera de la venta y pasarelas apagadas.
Con dobles (sin JELAF, Yupy, Niubiz ni Meta).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import rutas
from paneles import consultas, registro, tiempo
from paneles.api_paneles import router
from tests.test_venta import BOOKING, comprar, correr, elegir_y_llenar, iniciar, preparar_venta

CLAVE = {"X-Paneles-Token": "clave-panel-prueba"}
CIUDADES = "clave-ciudades-prueba"      # PANELES_CLAVE_CIUDADES de conftest
PASARELAS = "clave-pasarelas-prueba"    # PANELES_CLAVE_PASARELAS de conftest


@pytest.fixture
def panel():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _manana():
    return (tiempo.hoy() + timedelta(days=1)).isoformat()


# --- rutas.catalogo (puro) ----------------------------------------------------- #
def test_ciudad_cerrada_no_sale_ni_como_origen_ni_como_destino():
    cat = rutas.catalogo(set(rutas.SEDES), {"ICA"})
    assert "ICA" not in {s["codigo"] for s in cat["sedes"]}
    assert "ICA" not in cat["destinos"]
    assert all("ICA" not in destinos for destinos in cat["destinos"].values())
    assert not any("ICA" in tramo.split("-") for tramo in cat["duraciones"])
    # El resto sigue igual: Lima→Nasca pasa por Ica, pero se vende.
    assert "NASCA" in cat["destinos"]["LIMA"]


def test_sin_ciudades_cerradas_el_catalogo_no_cambia():
    assert rutas.catalogo(set(rutas.SEDES)) == rutas.catalogo(set(rutas.SEDES), set())
    assert len(rutas.catalogo(set(rutas.SEDES))["sedes"]) == len(rutas.SEDES)


# --- Panel: acceso y validación ------------------------------------------------- #
@pytest.mark.parametrize("metodo, ruta", [
    ("get", "/api/paneles/admin/ciudades"),
    ("post", "/api/paneles/admin/ciudades/ICA"),
    ("get", "/api/paneles/admin/pasarelas"),
    ("post", "/api/paneles/admin/pasarelas/yupy"),
])
def test_administracion_exige_la_clave_del_panel(panel, metodo, ruta):
    clave = PASARELAS if "pasarelas" in ruta else CIUDADES
    kwargs = {"json": {"activa": False, "clave": clave}} if metodo == "post" else {}
    assert getattr(panel, metodo)(ruta, **kwargs).status_code == 401
    assert getattr(panel, metodo)(ruta, headers={"X-Paneles-Token": "x"}, **kwargs).status_code == 401
    assert consultas.ciudades_cerradas() == {} and consultas.pasarelas_apagadas() == {}


def test_administracion_en_produccion_sin_clave_falla_cerrado(panel, monkeypatch):
    monkeypatch.delenv("PANELES_TOKEN")
    monkeypatch.setenv("ENTORNO", "produccion")
    assert panel.post("/api/paneles/admin/pasarelas/yupy",
                      json={"activa": False, "clave": PASARELAS}).status_code == 503


def test_ciudad_o_pasarela_desconocida_se_rechaza(panel):
    assert panel.post("/api/paneles/admin/ciudades/MARTE", json={"activa": False, "clave": CIUDADES},
                      headers=CLAVE).status_code == 404
    assert panel.post("/api/paneles/admin/pasarelas/paypal", json={"activa": False, "clave": PASARELAS},
                      headers=CLAVE).status_code == 422
    assert panel.post("/api/paneles/admin/ciudades/ICA", json={"activa": False, "motivo": "x" * 201, "clave": CIUDADES},
                      headers=CLAVE).status_code == 422
    assert consultas.ciudades_cerradas() == {}


def test_cerrar_y_abrir_ciudad_desde_el_panel(panel):
    r = panel.post("/api/paneles/admin/ciudades/cerro azul",
                   json={"activa": False, "motivo": "Obras", "clave": CIUDADES}, headers=CLAVE)
    assert r.status_code == 200 and r.json()["codigo"] == "CERRO AZUL"
    ciudades = {c["codigo"]: c for c in panel.get("/api/paneles/admin/ciudades", headers=CLAVE).json()["ciudades"]}
    assert set(ciudades) == set(rutas.SEDES)
    assert ciudades["CERRO AZUL"]["activa"] is False and ciudades["CERRO AZUL"]["motivo"] == "Obras"
    assert ciudades["LIMA"]["activa"] is True
    # Cerrar dos veces no falla; abrir la borra.
    for activa in (False, True):
        assert panel.post("/api/paneles/admin/ciudades/CERRO AZUL", json={"activa": activa, "clave": CIUDADES},
                          headers=CLAVE).status_code == 200
    assert consultas.ciudades_cerradas() == {}


def test_pasarelas_desde_el_panel(panel):
    assert panel.post("/api/paneles/admin/pasarelas/niubiz", json={"activa": False, "motivo": " ", "clave": PASARELAS},
                      headers=CLAVE).status_code == 200
    estado = {p["medio"]: p for p in panel.get("/api/paneles/admin/pasarelas", headers=CLAVE).json()["pasarelas"]}
    assert estado["niubiz"]["activa"] is False and estado["niubiz"]["motivo"] is None
    assert estado["yupy"]["activa"] is True


# --- Clave de cada cambio ----------------------------------------------------------- #
@pytest.mark.parametrize("ruta, buena, otra", [
    ("/api/paneles/admin/ciudades/ICA", CIUDADES, PASARELAS),
    ("/api/paneles/admin/pasarelas/yupy", PASARELAS, CIUDADES),
])
def test_cambio_sin_su_clave_no_se_guarda(panel, ruta, buena, otra):
    for clave in (None, "", "equivocada", otra, buena + " "):
        cuerpo = {"activa": False} if clave is None else {"activa": False, "clave": clave}
        assert panel.post(ruta, json=cuerpo, headers=CLAVE).status_code == 403, clave
    assert consultas.ciudades_cerradas() == {} and consultas.pasarelas_apagadas() == {}
    # También para volver a activar.
    registro.fijar_ciudad("ICA", False)
    registro.fijar_pasarela("yupy", False)
    assert panel.post(ruta, json={"activa": True, "clave": "x"}, headers=CLAVE).status_code == 403
    assert "ICA" in consultas.ciudades_cerradas() and "yupy" in consultas.pasarelas_apagadas()


@pytest.mark.parametrize("variable, ruta", [
    ("PANELES_CLAVE_CIUDADES", "/api/paneles/admin/ciudades/ICA"),
    ("PANELES_CLAVE_PASARELAS", "/api/paneles/admin/pasarelas/yupy"),
])
def test_sin_clave_configurada_no_se_puede_cambiar_nada(panel, monkeypatch, variable, ruta):
    monkeypatch.delenv(variable)
    for clave in ("", "cualquiera"):
        r = panel.post(ruta, json={"activa": False, "clave": clave}, headers=CLAVE)
        assert r.status_code == 503 and variable in r.json()["detail"]
    assert consultas.ciudades_cerradas() == {} and consultas.pasarelas_apagadas() == {}


def test_probar_claves_tiene_limite(panel):
    for _ in range(10):
        assert panel.post("/api/paneles/admin/ciudades/ICA", json={"activa": False, "clave": "x"},
                          headers=CLAVE).status_code == 403
    r = panel.post("/api/paneles/admin/ciudades/ICA", json={"activa": False, "clave": CIUDADES}, headers=CLAVE)
    assert r.status_code == 429
    assert consultas.ciudades_cerradas() == {}


def test_sin_clave_de_administracion_el_backend_arranca_con_aviso(monkeypatch):
    import config
    monkeypatch.delenv("PANELES_CLAVE_CIUDADES")
    monkeypatch.delenv("PANELES_CLAVE_PASARELAS")
    monkeypatch.setenv("ENTORNO", "produccion")
    monkeypatch.setenv("WHATSAPP_TOKEN", "token-prueba")
    monkeypatch.setenv("PHONE_NUMBER_ID", "123")
    cfg = config.cargar(exigir_jelaf=False)   # en producción no impiden arrancar
    assert any("PANELES_CLAVE_CIUDADES" in a for a in cfg.avisos)
    assert any("PANELES_CLAVE_PASARELAS" in a for a in cfg.avisos)


# --- Ciudades en la venta ------------------------------------------------------- #
def test_rutas_no_ofrece_la_ciudad_cerrada(entorno_venta, cliente):
    registro.fijar_ciudad("ICA", False)
    datos = correr(cliente.get("/api/v1/rutas")).json()["data"]
    assert "ICA" not in {s["codigo"] for s in datos["sedes"]}
    assert "ICA" not in datos["destinos"]["LIMA"]


def test_buscar_viajes_rechaza_la_ciudad_cerrada_sin_ir_a_jelaf(entorno_venta, cliente, monkeypatch):
    registro.fijar_ciudad("NASCA", False)
    llamadas = []
    monkeypatch.setattr(entorno_venta.jelaf, "buscar_itinerarios", lambda *a: llamadas.append(a) or [])
    for origen, destino in (("LIMA", "NASCA"), ("NASCA", "LIMA")):
        r = correr(cliente.post("/api/v1/buscar-viajes", json={"origen": origen, "destino": destino,
                                                                 "fecha": _manana()}))
        assert r.status_code == 400 and "Nasca" in r.json()["detail"]
    assert llamadas == []


def test_iniciar_no_empieza_una_compra_en_una_ciudad_cerrada(entorno_venta, cliente):
    registro.fijar_ciudad("TACNA", False)     # origen de iniciar() en los tests (codi_origen 2)
    r = correr(iniciar(cliente))
    assert r.status_code == 400
    assert entorno_venta.venta(BOOKING) is None


def test_compra_en_curso_termina_aunque_cierren_la_ciudad(entorno_venta, cliente):
    async def escenario():
        assert (await iniciar(cliente)).status_code == 200
        registro.fijar_ciudad("TACNA", False)
        registro.fijar_ciudad("LIMA", False)
        # El primer asiento repite /iniciar (respaldo del prefetch): no se corta.
        assert (await iniciar(cliente)).status_code == 200
        await elegir_y_llenar(cliente)
        tx = (await cliente.post("/api/v2/venta/checkout", json={"booking_id": BOOKING})).json()["yupy_transaction_id"]
        entorno_venta.yupy.aprobar(tx)
        return await cliente.post("/api/v2/venta/confirmar", json={"booking_id": BOOKING, "referencia_pago": tx})
    assert correr(escenario()).status_code == 200
    assert consultas.venta_de_booking(BOOKING)["estado"] == "emitida"


def test_si_la_base_del_panel_falla_se_vende_igual(entorno_venta, cliente, monkeypatch):
    def rota():
        raise RuntimeError("disco lleno")
    monkeypatch.setattr(consultas, "ciudades_cerradas", rota)
    monkeypatch.setattr(consultas, "pasarelas_apagadas", rota)
    assert correr(cliente.get("/api/v1/rutas")).status_code == 200
    assert correr(comprar(cliente, entorno_venta)).status_code == 200


# --- Pasarelas en la venta ------------------------------------------------------ #
def _monto(cliente):
    return correr(cliente.post("/api/v2/venta/monto", json={"booking_id": BOOKING})).json()


def test_pasarela_apagada_no_se_ofrece_ni_abre_cobros(entorno_venta, cliente):
    correr(elegir_y_llenar(cliente))
    assert _monto(cliente)["medios_pago"] == {"yupy": True, "niubiz": True}

    registro.fijar_pasarela("yupy", False)
    assert _monto(cliente)["medios_pago"] == {"yupy": False, "niubiz": True}
    r = correr(cliente.post("/api/v2/venta/checkout", json={"booking_id": BOOKING}))
    assert r.status_code == 503 and entorno_venta.yupy.creadas == []

    registro.fijar_pasarela("niubiz", False)
    assert _monto(cliente)["medios_pago"] == {"yupy": False, "niubiz": False}
    r = correr(cliente.post("/api/v2/venta/niubiz/sesion", json={"booking_id": BOOKING}))
    assert r.status_code == 503 and entorno_venta.niubiz.sesiones == []

    registro.fijar_pasarela("yupy", True)
    assert _monto(cliente)["medios_pago"] == {"yupy": True, "niubiz": False}


def test_cobro_abierto_antes_de_apagar_la_pasarela_se_entrega(entorno_venta, cliente):
    tx = correr(preparar_venta(cliente))
    registro.fijar_pasarela("yupy", False)
    entorno_venta.yupy.aprobar(tx)
    # Al volver a la pantalla de pago todavía ve su medio y llega a "ya pagado".
    assert _monto(cliente)["medios_pago"]["yupy"] is True
    r = correr(cliente.post("/api/v2/venta/checkout", json={"booking_id": BOOKING}))
    assert r.json()["status"] == "ya_pagado"
    r = correr(cliente.post("/api/v2/venta/confirmar", json={"booking_id": BOOKING, "referencia_pago": tx}))
    assert r.status_code == 200
    assert consultas.venta_de_booking(BOOKING)["estado"] == "emitida"
