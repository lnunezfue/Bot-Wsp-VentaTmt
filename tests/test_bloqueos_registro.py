"""Registro local de bloqueos (bloqueos_registro.py) y su consola (liberar_asientos.py)."""

from __future__ import annotations

import time

import bloqueos_registro
import liberar_asientos
from tests.conftest import JelafHttpFalso
from tests.test_flujo_robusto import elegir
from tests.test_venta import BOOKING, comprar, correr, iniciar


def _ids_registrados():
    return sorted(r["ids"] for r in bloqueos_registro.listar())


# --- El bot anota y borra sus bloqueos -------------------------------------------- #
def test_bloquear_anota_y_soltar_borra(entorno_venta, cliente):
    async def escenario():
        await iniciar(cliente)
        await elegir(cliente, "05", "06")
        antes = _ids_registrados()
        await cliente.post("/api/v2/venta/liberar-asiento", json={"booking_id": BOOKING, "numero_asiento": "05"})
        return antes
    antes = correr(escenario())
    assert len(antes) == 2
    restantes = bloqueos_registro.listar()
    assert [r["asiento"] for r in restantes] == ["06"]
    r = restantes[0]
    assert r["terminal"] == 22 and r["viaje"]["codi_origen"] == 2 and r["programacion"]
    # Sin datos personales: solo códigos, asiento, cuenta y el booking enmascarado.
    assert "documento" not in r and "nombres" not in r and BOOKING not in str(r)


def test_cancelar_borra_los_bloqueos_del_registro(entorno_venta, cliente):
    async def escenario():
        await iniciar(cliente)
        await elegir(cliente, "05", "06")
        await cliente.post("/api/v2/venta/cancelar", json={"booking_id": BOOKING})
    correr(escenario())
    assert bloqueos_registro.listar() == []


def test_vender_borra_los_bloqueos_del_registro(entorno_venta, cliente):
    r = correr(comprar(cliente, entorno_venta, asientos=("05",)))
    assert r.status_code == 200, r.text
    assert bloqueos_registro.listar() == []


def test_si_jelaf_no_suelta_el_bloqueo_queda_registrado(entorno_venta, cliente, monkeypatch):
    from jelaf_http import ErrorJelaf

    def cae(*a, **k):
        raise ErrorJelaf("JELAF no respondió.")

    async def escenario():
        await iniciar(cliente)
        await elegir(cliente, "05")
        monkeypatch.setattr(entorno_venta.jelaf, "liberar_arreglo", cae)
        await cliente.post("/api/v2/venta/cancelar", json={"booking_id": BOOKING})
    correr(escenario())
    assert [r["asiento"] for r in bloqueos_registro.listar()] == ["05"]   # para liberar_asientos.py


def test_en_simulacion_no_se_anota_nada(entorno_venta, cliente):
    entorno_venta.vendedor.simulacion = True

    async def escenario():
        await iniciar(cliente)
        await elegir(cliente, "05")
    correr(escenario())
    assert bloqueos_registro.listar() == []


def test_un_fallo_del_registro_no_tumba_la_venta(entorno_venta, cliente, monkeypatch, tmp_path):
    monkeypatch.setattr(bloqueos_registro, "RUTA", tmp_path / "no_existe" / "x" / "bloqueos.db")

    async def escenario():
        await iniciar(cliente)
        return await elegir(cliente, "05")
    (r,) = correr(escenario())
    assert r.status_code == 200, r.text


# --- Consola: revisar y liberar --------------------------------------------------- #
VIAJE = {"codi_empresa": 1, "codi_origen": 2, "codi_destino": 1, "codi_sucursal": 2, "codi_ruta": 1,
         "codi_punto_venta": 17, "codi_servicio": 3, "fecha_jelaf": "20/10/2026", "hora_viaje": "08:30 PM"}


def _anotar(ids, asiento, cuenta="Y1", hace_min=60):
    bloqueos_registro.anotar(ids=ids, cuenta=cuenta, terminal=22, asiento=asiento, programacion=1,
                             viaje=VIAJE, booking="1111…5555")
    if hace_min:
        with bloqueos_registro._conexion() as c:
            c.execute("UPDATE bloqueo SET creado_en = ? WHERE ids = ?", (time.time() - hace_min * 60, ids))


def test_revisar_marca_vendidos_y_los_saca_del_registro():
    jelaf = JelafHttpFalso()
    jelaf.vendidos["06"] = "00000009"
    jelaf.bloqueos.update({1001: "05", 1002: "06"})
    _anotar(1001, "05")
    _anotar(1002, "06")
    filas = liberar_asientos.revisar(bloqueos_registro.listar(), {"Y1": jelaf})
    assert {f["asiento"]: f["estado"] for f in filas} == {"05": "bloqueado"}
    assert _ids_registrados() == [1001]


def test_liberar_suelta_en_jelaf_con_su_cuenta_y_borra_el_registro():
    y1, y2 = JelafHttpFalso(), JelafHttpFalso()
    y1.bloqueos[1001] = "05"
    y2.bloqueos[2001] = "07"
    _anotar(1001, "05", cuenta="Y1")
    _anotar(2001, "07", cuenta="Y2")
    fila = next(f for f in bloqueos_registro.listar() if f["ids"] == 2001)
    ok, mensaje = liberar_asientos.liberar(fila, y2)
    assert ok, mensaje
    assert y2.bloqueos == {} and y1.bloqueos == {1001: "05"}
    assert _ids_registrados() == [1001]


def test_una_compra_reciente_se_marca_como_posible_compra_en_curso():
    jelaf = JelafHttpFalso()
    _anotar(1001, "05", hace_min=0)
    (fila,) = liberar_asientos.revisar(bloqueos_registro.listar(), {"Y1": jelaf})
    assert fila["reciente"] is True


def test_cuenta_sin_credenciales_no_se_puede_liberar():
    _anotar(1001, "05", cuenta="VIEJA")
    (fila,) = liberar_asientos.revisar(bloqueos_registro.listar(), {})
    assert fila["estado"] == "sin cuenta"


def test_elegir_entiende_numeros_cuenta_y_todos():
    filas = [{"cuenta": "Y1"}, {"cuenta": "Y2"}, {"cuenta": "Y1"}]
    assert liberar_asientos.elegir("1,3", filas) == [0, 2]
    assert liberar_asientos.elegir("c y1", filas) == [0, 2]
    assert liberar_asientos.elegir("t", filas) == [0, 1, 2]
    assert liberar_asientos.elegir("9", filas) is None
