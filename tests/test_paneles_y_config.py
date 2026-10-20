"""Paneles (autenticación, @seguro, atomicidad, validaciones) y config.py (fallar cerrado)."""

from __future__ import annotations

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
from paneles import db, fidelizacion, registro
from paneles.api_paneles import router

CLAVE = {"X-Paneles-Token": "clave-panel-prueba"}


@pytest.fixture
def panel():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# --- Acceso ------------------------------------------------------------------ #
def test_panel_sin_clave_o_con_clave_equivocada_se_niega(panel):
    assert panel.get("/api/paneles/ventas").status_code == 401
    assert panel.get("/api/paneles/ventas", headers={"X-Paneles-Token": "x"}).status_code == 401
    assert panel.get("/api/paneles/ventas", headers=CLAVE).status_code == 200


def test_panel_en_produccion_sin_clave_configurada_falla_cerrado(panel, monkeypatch):
    monkeypatch.delenv("PANELES_TOKEN")
    monkeypatch.setenv("ENTORNO", "produccion")
    assert panel.get("/api/paneles/ventas").status_code == 503


def test_rango_de_fechas_invalido_es_400_no_500(panel):
    assert panel.get("/api/paneles/ventas?desde=ayer", headers=CLAVE).status_code == 400
    assert panel.get("/api/paneles/ventas?desde=2026-09-10&hasta=2026-09-01", headers=CLAVE).status_code == 400


# --- Telemetría pública (N7) -------------------------------------------------- #
def test_evento_solo_acepta_enlace_abierto_e_ignora_telefono(panel):
    falso = panel.post("/api/paneles/evento", json={"booking_id": "b-12345678", "paso": "boleto_emitido"})
    assert falso.status_code == 422
    ok = panel.post("/api/paneles/evento",
                    json={"booking_id": "b-12345678", "paso": "enlace_abierto", "telefono": "51999999999"})
    assert ok.status_code == 200
    fila = db.consultar_uno("SELECT telefono, paso_maximo FROM sesion_compra WHERE booking_id='b-12345678'")
    assert fila["telefono"] is None and fila["paso_maximo"] == "enlace_abierto"


# --- Códigos de descuento (N8 / N9) ------------------------------------------- #
def test_validar_exige_documento(panel):
    fidelizacion.crear_codigo("TMTAAAA", 50, segmento_objetivo="frecuente", estado="activo")
    assert panel.get("/api/paneles/codigos/validar", params={"codigo": "TMTAAAA"}).status_code == 422
    r = panel.get("/api/paneles/codigos/validar", params={"codigo": "TMTAAAA", "documento": "12345678"})
    assert r.json()["valido"] is False and "segmento" not in r.json()["motivo"]


def test_codigo_con_fecha_mal_cargada_no_rompe(panel):
    fidelizacion.crear_codigo("TMTBBBB", 10, vence_en="31/12/2026", estado="activo")
    r = panel.get("/api/paneles/codigos/validar", params={"codigo": "TMTBBBB", "documento": "12345678"})
    assert r.status_code == 200 and r.json()["valido"] is False


def test_crear_codigo_valida_la_entrada(panel):
    malo = {"codigo": "TMTX", "valor": 150, "tipo": "porcentaje", "estado": "activo"}
    assert panel.post("/api/paneles/codigos", json=malo, headers=CLAVE).status_code == 422
    fecha_mala = {"codigo": "TMTX", "valor": 10, "vence_en": "31/12/2026"}
    assert panel.post("/api/paneles/codigos", json=fecha_mala, headers=CLAVE).status_code == 422
    # Lo que manda hoy el tablero sigue funcionando:
    bueno = {"codigo": "TMT_2026", "valor": 10, "segmento_objetivo": "dormido", "vence_en": None, "estado": "activo"}
    assert panel.post("/api/paneles/codigos", json=bueno, headers=CLAVE).status_code == 200


# --- Registro ---------------------------------------------------------------- #
def test_init_db_es_idempotente():
    db.init_db()
    db.init_db()
    columnas = {f["name"] for f in db.consultar("PRAGMA table_info(boleto)")}
    assert "nro_boleto" in columnas


def test_registrar_venta_es_atomica_y_el_fallo_se_cuenta():
    antes = registro.fallos_registro()["total"]
    r = registro.registrar_venta(
        booking_id="b-atomica", origen="TACNA", destino="LIMA", monto_total=240,
        pasajeros=[{"documento": "1", "asiento": "05", "precio": 120},
                   {"documento": "2", "asiento": "06", "precio": "no-es-numero"}],
    )
    assert r is None                                    # @seguro no propaga
    assert db.consultar_uno("SELECT COUNT(*) FROM venta")[0] == 0
    assert db.consultar_uno("SELECT COUNT(*) FROM boleto")[0] == 0
    assert registro.fallos_registro()["total"] == antes + 1


def test_sesion_retomada_vuelve_a_en_curso():
    registro.abrir_sesion("b-retomada")
    registro.cerrar_sesion("b-retomada", "abandonada")
    registro.reabrir_sesion("b-retomada")
    assert db.consultar_uno("SELECT estado FROM sesion_compra WHERE booking_id='b-retomada'")[0] == "en_curso"


def test_un_pago_usado_no_se_puede_reemplazar():
    assert registro.guardar_pago("b-pago", "tx-1", "p", 12000, ["05"]) is True
    registro.marcar_pago_usado("b-pago")
    assert registro.guardar_pago("b-pago", "tx-2", "p", 100, ["05"]) is False
    assert db.consultar_uno("SELECT yupy_transaction_id FROM pago WHERE booking_id='b-pago'")[0] == "tx-1"


def test_una_transaccion_de_yupy_no_sirve_para_dos_ventas():
    assert registro.guardar_pago("b-uno", "tx-unica", "p", 12000, ["05"]) is True
    assert registro.guardar_pago("b-dos", "tx-unica", "p", 12000, ["05"]) is None   # UNIQUE


def test_hora_de_lima_no_depende_de_la_maquina():
    from datetime import datetime, timezone
    from paneles import tiempo
    utc = datetime.now(timezone.utc).replace(tzinfo=None)
    diferencia = (utc - tiempo.ahora_dt()).total_seconds()
    assert abs(diferencia - 5 * 3600) < 5


# --- config.py (fallar cerrado) --------------------------------------------- #
def test_produccion_sin_secretos_no_arranca(monkeypatch):
    monkeypatch.setenv("ENTORNO", "produccion")
    monkeypatch.delenv("META_APP_SECRET")
    with pytest.raises(config.ConfiguracionInvalida, match="META_APP_SECRET"):
        config.cargar()


def test_desarrollo_sin_secretos_arranca_con_avisos(monkeypatch):
    monkeypatch.delenv("META_APP_SECRET")
    monkeypatch.delenv("ENLACE_SECRETO")
    c = config.cargar()
    assert any("META_APP_SECRET" in a for a in c.avisos)
    assert len(c.enlace_secreto) >= 32                     # aleatorio, nunca un valor conocido


def test_venta_modo_por_defecto_es_simulacion_y_se_avisa(monkeypatch):
    monkeypatch.delenv("VENTA_MODO")
    c = config.cargar()
    assert c.simulacion and any("simulacion" in a for a in c.avisos)


def test_produccion_exige_declarar_venta_modo(monkeypatch):
    monkeypatch.setenv("ENTORNO", "produccion")
    monkeypatch.delenv("VENTA_MODO")
    with pytest.raises(config.ConfiguracionInvalida, match="VENTA_MODO"):
        config.cargar()


def test_venta_modo_invalido_no_arranca(monkeypatch):
    monkeypatch.setenv("VENTA_MODO", "casi-real")
    with pytest.raises(config.ConfiguracionInvalida):
        config.cargar()


def test_sin_credenciales_jelaf_no_hay_valor_por_defecto(monkeypatch):
    monkeypatch.delenv("JELAF_PASSWORD")
    with pytest.raises(config.ConfiguracionInvalida):
        config.cargar()
    assert "3976" not in open("config.py", encoding="utf-8").read()
    assert os.environ.get("JELAF_PASSWORD") is None
