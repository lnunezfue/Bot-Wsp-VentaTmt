"""
Reglas que salieron de probar el sistema como usuario (03/10/2026, en
CHATBOT PANELES) y se migraron a la venta sin navegador: ningún camino deja
bloqueos sin dueño en JELAF, una compra no acapara un bus, las entradas
imposibles se rechazan antes de ocupar un lugar, y "hoy" es el de Lima.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from paneles import db, tiempo
from tests.test_venta import BOOKING, iniciar


def correr(coro):
    return asyncio.run(coro)


def contar(sql, params=()):
    return db.consultar_uno(sql, params)[0]


async def elegir(c, *asientos):
    return [await c.post("/api/v2/venta/seleccionar-asiento", json={"booking_id": BOOKING, "numero_asiento": a})
            for a in asientos]


# --- Bloqueos en JELAF ------------------------------------------------------------ #
def test_quitar_un_asiento_ajeno_no_toca_jelaf(entorno_venta, cliente):
    async def escenario():
        await iniciar(cliente)
        await elegir(cliente, "05")
        antes = entorno_venta.jelaf.llamadas["liberar"]
        r = await cliente.post("/api/v2/venta/liberar-asiento", json={"booking_id": BOOKING, "numero_asiento": "29"})
        return r, antes
    r, antes = correr(escenario())
    assert r.status_code == 200 and r.json()["asientos_en_venta"] == ["05"]
    assert entorno_venta.jelaf.llamadas["liberar"] == antes


def test_cancelar_suelta_en_jelaf_los_bloqueos_de_la_compra(entorno_venta, cliente):
    async def escenario():
        await iniciar(cliente)
        await elegir(cliente, "05", "06")
        antes = entorno_venta.jelaf.llamadas["liberar"]
        await cliente.post("/api/v2/venta/cancelar", json={"booking_id": BOOKING})
        return antes
    antes = correr(escenario())
    assert entorno_venta.jelaf.llamadas["liberar"] == antes + 1
    assert entorno_venta.venta(BOOKING) is None


# --- N5: tope de asientos por compra -------------------------------------------------- #
def test_no_se_pueden_apartar_mas_de_5_asientos(entorno_venta, cliente):
    async def escenario():
        await iniciar(cliente)
        return await elegir(cliente, "01", "02", "03", "04", "05", "06")
    respuestas = correr(escenario())
    assert [r.status_code for r in respuestas] == [200] * 5 + [409]
    assert "hasta 5" in respuestas[-1].json()["detail"]
    assert entorno_venta.jelaf.llamadas["bloquear"] == 5


def test_volver_a_tocar_un_asiento_propio_no_cuenta_para_el_tope(entorno_venta, cliente):
    async def escenario():
        await iniciar(cliente)
        return await elegir(cliente, "01", "02", "03", "04", "05", "05")
    assert [r.status_code for r in correr(escenario())] == [200] * 6


# --- N11/N8: entradas imposibles ------------------------------------------------------- #
@pytest.mark.parametrize("cambio, campo", [
    ({"fecha_viaje": "2026-02-31"}, "fecha del viaje"),
    ({"fecha_viaje": (tiempo.hoy() - timedelta(days=1)).isoformat()}, "ya pasó"),
    ({"fecha_viaje": (tiempo.hoy() + timedelta(days=400)).isoformat()}, "todavía no hay venta"),
    ({"hora_viaje": "99:99 PM"}, "hora del viaje"),
])
def test_iniciar_rechaza_fechas_y_horas_imposibles_sin_ocupar_lugar(entorno_venta, cliente, cambio, campo):
    async def escenario():
        return await iniciar(cliente, **cambio)
    r = correr(escenario())
    assert r.status_code == 422 and campo in r.json()["detail"]
    assert entorno_venta.venta(BOOKING) is None


def test_el_asiento_00_no_existe(entorno_venta, cliente):
    async def escenario():
        await iniciar(cliente)
        return await elegir(cliente, "00")
    (r,) = correr(escenario())
    assert r.status_code == 422 and "asiento" in r.json()["detail"]


@pytest.mark.parametrize("fecha", ["2030-01-01", "1850-13-45", "1899-12-31"])
def test_fecha_de_nacimiento_imposible_no_llega_a_jelaf(entorno_venta, cliente, fecha):
    async def escenario():
        await iniciar(cliente)
        await elegir(cliente, "05")
        return await cliente.post("/api/v2/venta/pasajero", json={
            "booking_id": BOOKING, "numero_asiento": "05", "documento": "12345678", "sexo": "F",
            "nombres": "Ana", "paterno": "Paz", "fecha_nacimiento": fecha})
    r = correr(escenario())
    assert r.status_code == 422 and "fecha de nacimiento" in r.json()["detail"]
    assert not entorno_venta.venta(BOOKING).asientos_en_venta["05"]["pasajero_cargado"]


def _hace_anios(anios: int, mas_dias: int = 0) -> str:
    hoy = tiempo.hoy()
    return (hoy.replace(year=hoy.year - anios) + timedelta(days=mas_dias)).isoformat()


@pytest.mark.parametrize("fecha,aceptada", [
    (_hace_anios(121), False),             # cumple 121 hoy
    (_hace_anios(130), False),
    (_hace_anios(121, mas_dias=1), True),  # 120 años, cumple 121 mañana
    (_hace_anios(30), True),
])
def test_nadie_mayor_de_120_anios(entorno_venta, cliente, fecha, aceptada):
    """Observación de boletería (2026-10-08): no registrar a nadie mayor de 120 años."""
    async def escenario():
        await iniciar(cliente)
        await elegir(cliente, "05")
        return await cliente.post("/api/v2/venta/pasajero", json={
            "booking_id": BOOKING, "numero_asiento": "05", "documento": "12345678", "sexo": "F",
            "nombres": "Ana", "paterno": "Paz", "fecha_nacimiento": fecha})
    r = correr(escenario())
    if aceptada:
        assert r.status_code == 200, r.text
    else:
        assert r.status_code == 422 and "fecha de nacimiento" in r.json()["detail"]
        assert not entorno_venta.venta(BOOKING).asientos_en_venta["05"]["pasajero_cargado"]


def test_el_error_de_validacion_es_legible_y_no_repite_lo_enviado(entorno_venta, cliente):
    async def escenario():
        await iniciar(cliente)
        await elegir(cliente, "05")
        return await cliente.post("/api/v2/venta/pasajero", json={
            "booking_id": BOOKING, "numero_asiento": "05", "documento": "12345678", "sexo": "F",
            "nombres": "<img src=x onerror=alert(1)>", "paterno": "Paz", "fecha_nacimiento": "1990-01-01"})
    r = correr(escenario())
    assert r.status_code == 422
    assert r.json() == {"detail": "Revisa estos datos: nombres.", "campos": ["nombres"]}
    assert "onerror" not in r.text


# --- N12: documento genérico de JELAF ------------------------------------------------ #
def test_documento_generico_no_autocompleta_reserva_de(entorno_venta, cliente, monkeypatch):
    monkeypatch.setattr(entorno_venta.jelaf, "consultar_usuario", lambda t, d: {
        "EsCorrecto": True, "Valor": {"NombreCliente": "RESERVA DE", "ApellidoPaterno": "RESERVA",
                                      "ApellidoMaterno": "RESERVA"}})

    async def escenario():
        await iniciar(cliente)
        return await cliente.post("/api/v1/consultar-cliente", json={
            "booking_id": BOOKING, "tipo_documento": "01", "documento": "12345678"})
    assert correr(escenario()).json() == {"status": "success", "encontrado": False}


# --- Catálogo ---------------------------------------------------------------------- #
def test_asientos_libres_salen_de_capacidad_menos_vendidos(entorno_venta):
    libres = entorno_venta.api._asientos_libres
    assert libres({"CapacidadBus": 44, "AsientosVendidos": 2}) == 42
    assert libres({"AsientosLibres": 7, "CapacidadBus": 44}) == 7
    assert libres({}) == 0


def test_hoy_solo_aparecen_los_viajes_que_aun_no_salen(entorno_venta, cliente, monkeypatch):
    import api_ventas
    hoy = tiempo.hoy()
    monkeypatch.setattr(api_ventas.tiempo, "ahora_dt", lambda: datetime(hoy.year, hoy.month, hoy.day, 12, 0))
    monkeypatch.setattr(api_ventas.planos, "placas_configuradas", lambda: {"ZBA963"})
    base = {"NomDestino": "AREQUIPA", "PlacaBus": "ZBA963"}
    monkeypatch.setattr(entorno_venta.jelaf, "buscar_itinerarios", lambda *a: [
        {**base, "HoraPartida": "06:30AM"}, {**base, "HoraPartida": "12:15PM"},
        {**base, "HoraPartida": "12:45PM"}, {**base, "HoraPartida": "10:50PM"},
    ])

    async def buscar(fecha):
        r = await cliente.post("/api/v1/buscar-viajes", json={"origen": "TACNA", "destino": "AREQUIPA", "fecha": fecha})
        return [v["hora_partida"] for v in r.json()["data"]]
    assert correr(buscar(hoy.isoformat())) == ["12:45PM", "10:50PM"]   # 12:15 sale en < 30 min
    manana = (hoy + timedelta(days=1)).isoformat()
    assert correr(buscar(manana)) == ["06:30AM", "12:15PM", "12:45PM", "10:50PM"]


# --- "Hoy" en hora de Lima (servidor en UTC) ------------------------------------------- #
def test_el_vendedor_usa_la_fecha_de_lima_no_la_del_servidor(monkeypatch):
    """A las 20:30 de Lima un servidor en UTC ya está en el día siguiente."""
    from paneles import tiempo as t
    from vendedor_http import VendedorHttp
    monkeypatch.setattr(t, "ahora_dt", lambda: datetime(2026, 10, 3, 20, 30))
    assert VendedorHttp(jelaf=None).hoy() == date(2026, 10, 3)


# --- Simulación con pasarelas de producción ------------------------------------------ #
def test_simulacion_con_pasarela_de_produccion_avisa(monkeypatch):
    import config
    monkeypatch.setenv("VENTA_MODO", "simulacion")
    monkeypatch.setenv("YUPY_BASE_URL", "https://api.yupy.us")
    monkeypatch.setenv("NIUBIZ_MERCHANT_ID", "123")
    monkeypatch.setenv("NIUBIZ_USUARIO", "u")
    monkeypatch.setenv("NIUBIZ_PASSWORD", "p")
    monkeypatch.setenv("NIUBIZ_BASE_URL", "https://apiprod.vnforapps.com")
    avisos = config.cargar().avisos
    assert any("Yupy de PRODUCCIÓN" in a for a in avisos)
    assert any("Niubiz de PRODUCCIÓN" in a for a in avisos)


def test_precio_desde_no_usa_los_precios_por_piso_de_jelaf(entorno_venta):
    desde = entorno_venta.api._precio_desde
    assert desde({"Precio_N1": 70, "Precio_N2": 70}) == 0      # no son el precio real de los asientos
    assert desde({"PrecioBase": 120}) == 120
    assert desde({}) == 0 and desde({"PrecioBase": "x"}) == 0

def test_asiento_sin_precio_propio_no_se_vende_con_el_de_otro_asiento(entorno_venta, cliente, monkeypatch):
    import api_ventas
    from jelaf_http import EstadoTurno

    async def estado(w, fresco=False):
        return EstadoTurno(precios={"06": Decimal("70.00")}, precio_general=Decimal("70.00"))
    monkeypatch.setattr(api_ventas, "_estado_turno", estado)

    async def escenario():
        await iniciar(cliente)
        return await elegir(cliente, "05")
    (r,) = correr(escenario())
    assert r.status_code == 409 and entorno_venta.jelaf.llamadas["bloquear"] == 0
    assert contar("SELECT COUNT(*) FROM incidencia WHERE tipo = 'asiento_sin_precio'") == 1


# --- Celular con código de país (diseño del compañero, CHATBOT PRUEBA 8-9.html) --------- #
async def llenar_con_celular(c, telefono):
    await iniciar(c)
    await elegir(c, "05")
    return await c.post("/api/v2/venta/pasajero", json={
        "booking_id": BOOKING, "numero_asiento": "05", "documento": "12345678", "sexo": "F",
        "nombres": "Ana", "paterno": "Paz", "fecha_nacimiento": "1990-01-01", "telefono": telefono})


@pytest.mark.parametrize("enviado, a_jelaf", [
    ("987654321", "987654321"),
    ("+51987654321", "987654321"),
    ("+56 9 1234 5678", ""),      # JELAF solo conoce el código de país de Perú: no se inventa el de Chile
    ("+59171234567", ""),
])
def test_celular_peruano_o_extranjero(entorno_venta, cliente, enviado, a_jelaf):
    r = correr(llenar_con_celular(cliente, enviado))
    assert r.status_code == 200, r.text
    assert entorno_venta.venta(BOOKING).asientos_en_venta["05"]["telefono"] == a_jelaf


@pytest.mark.parametrize("malo", ["abc", "12345", "+51 12345678", "+5691234567890123"])
def test_celular_imposible_se_rechaza_en_el_servidor(entorno_venta, cliente, malo):
    r = correr(llenar_con_celular(cliente, malo))
    assert r.status_code == 422 and r.json()["campos"][0].startswith("celular")
    assert not entorno_venta.venta(BOOKING).asientos_en_venta["05"]["pasajero_cargado"]
