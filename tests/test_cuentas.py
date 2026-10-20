"""Varias cuentas de JELAF (Y1…Y6 + la de siempre como reserva): config y reparto de ventas."""

from __future__ import annotations

import asyncio
import pytest

import config
from tests.conftest import JelafHttpFalso
from vendedor_http import PoolCuentas


# --- Config ---------------------------------------------------------------- #
def _cuentas(monkeypatch, numeradas: dict[int, tuple[str, str]], principal: tuple[str, str] | None = None):
    for n, (u, p) in numeradas.items():
        monkeypatch.setenv(f"JELAF_USUARIO_{n}", u)
        monkeypatch.setenv(f"JELAF_PASSWORD_{n}", p)
    if principal:
        monkeypatch.setenv("JELAF_USUARIO", principal[0])
        monkeypatch.setenv("JELAF_PASSWORD", principal[1])
    else:
        monkeypatch.delenv("JELAF_USUARIO", raising=False)
        monkeypatch.delenv("JELAF_PASSWORD", raising=False)


def test_las_numeradas_van_primero_y_la_de_siempre_queda_de_reserva(monkeypatch):
    _cuentas(monkeypatch, {2: ("Y2", "c2"), 1: ("Y1", "c1"), 3: ("Y3", "c3")}, principal=("VIEJA", "cv"))
    c = config.cargar()
    assert [u for u, _ in c.jelaf_credenciales] == ["Y1", "Y2", "Y3", "VIEJA"]
    assert c.jelaf_cuenta_reserva == "VIEJA"


def test_la_de_siempre_sola_sigue_funcionando(monkeypatch):
    c = config.cargar()   # conftest solo define JELAF_USUARIO/JELAF_PASSWORD
    assert [u for u, _ in c.jelaf_credenciales] == ["usuario-prueba"]
    assert c.jelaf_cuenta_reserva is None


def test_la_de_siempre_repetida_en_las_numeradas_no_se_duplica(monkeypatch):
    _cuentas(monkeypatch, {1: ("Y1", "c1")}, principal=("Y1", "c1"))
    c = config.cargar()
    assert [u for u, _ in c.jelaf_credenciales] == ["Y1"]
    assert c.jelaf_cuenta_reserva is None


def test_usuario_numerado_sin_clave_no_arranca(monkeypatch):
    monkeypatch.setenv("JELAF_USUARIO_1", "Y1")
    with pytest.raises(config.ConfiguracionInvalida, match="JELAF_PASSWORD_1"):
        config.cargar()


def test_ventas_por_cuenta_por_defecto_una(monkeypatch):
    assert config.cargar().ventas_por_cuenta == 1
    monkeypatch.setenv("VENTAS_POR_CUENTA", "3")
    assert config.cargar().ventas_por_cuenta == 3


def test_max_ventas_simultaneas_ya_no_se_usa_y_se_avisa(monkeypatch):
    monkeypatch.setenv("MAX_VENTAS_SIMULTANEAS", "10")
    assert any("VENTAS_POR_CUENTA" in a for a in config.cargar().avisos)


# --- Reparto de ventas ------------------------------------------------------- #
def _pool(n=3, por_cuenta=1, reserva=False):
    clientes = [JelafHttpFalso() for _ in range(n)]
    for i, c in enumerate(clientes):
        c.usuario = f"Y{i + 1}"
        c.cajero["Terminal"] = str(700 + i)
    return PoolCuentas(clientes, ventas_por_cuenta=por_cuenta, simulacion=False,
                       reserva=clientes[-1].usuario if reserva else None), clientes


def test_una_venta_por_cuenta_y_la_siguiente_espera():
    pool, clientes = _pool(3)
    ventas = [pool.reservar_para(f"booking-{i}") for i in range(3)]
    assert [v.vendedor.jelaf for v in ventas] == clientes
    assert pool.capacidad == 3
    assert pool.reservar_para("booking-x") is None
    assert pool.reservar_para("booking-1") is ventas[1]          # idempotente

    ventas[1].vendedor.olvidar(ventas[1])
    assert pool.reservar_para("booking-x").vendedor.jelaf is clientes[1]
    assert pool.worker_de("booking-x").vendedor.jelaf is clientes[1]


def test_la_cuenta_de_reserva_es_la_ultima_en_usarse():
    pool, clientes = _pool(3, por_cuenta=3, reserva=True)
    usadas = [pool.reservar_para(f"b-{i}").vendedor.jelaf.usuario for i in range(9)]
    # Primero se reparten entre las numeradas (la menos cargada), la reserva al final.
    assert usadas[:6] == ["Y1", "Y2", "Y1", "Y2", "Y1", "Y2"]
    assert usadas[6:] == ["Y3", "Y3", "Y3"]


def test_una_cuenta_sin_sesion_no_recibe_ventas():
    pool, clientes = _pool(3)
    clientes[0].disponible = False
    assert pool.reservar_para("booking-a").vendedor.jelaf is clientes[1]
    assert pool.reservar_para("booking-b").vendedor.jelaf is clientes[2]
    assert pool.reservar_para("booking-c") is None
    assert pool.cliente_lectura() is clientes[1]


def test_en_modo_real_una_cuenta_sin_datos_de_cajero_no_recibe_ventas():
    pool, clientes = _pool(2)

    class SinCajero(JelafHttpFalso):
        lista_para_vender = False
    sin_cajero = SinCajero()
    pool.vendedores[0].jelaf = sin_cajero
    assert pool.reservar_para("booking-a").vendedor.jelaf is clientes[1]
    assert pool.reservar_para("booking-b") is None


def test_cancelar_suelta_los_bloqueos_con_la_cuenta_que_los_hizo():
    pool, clientes = _pool(2)
    a, b = pool.reservar_para("booking-a"), pool.reservar_para("booking-b")
    a.asientos_en_venta["05"] = {"ids": 1001, "programacion": 1}
    clientes[0].bloqueos[1001] = "05"
    b.asientos_en_venta["07"] = {"ids": 2001, "programacion": 1}
    clientes[1].bloqueos[2001] = "07"

    async def cancelar_b():
        async with b.lock:
            await b.liberar_y_resetear()
    asyncio.run(cancelar_b())
    assert clientes[1].bloqueos == {} and clientes[0].bloqueos == {1001: "05"}
    assert clientes[0].llamadas["liberar"] == 0


def test_el_tope_de_bloqueos_por_bus_cuenta_todas_las_cuentas():
    pool, _ = _pool(2)
    a, b = pool.reservar_para("booking-a"), pool.reservar_para("booking-b")
    a.asientos_en_venta["05"] = {"ids": 1, "programacion": 9}
    b.asientos_en_venta["07"] = {"ids": 2, "programacion": 9}
    assert a.vendedor.bloqueos_en(9) == 2 == pool.bloqueos_en(9)


def test_la_emision_es_de_a_una_entre_todas_las_cuentas():
    pool, _ = _pool(3)
    locks = {id(v.lock_emision) for v in pool.vendedores}
    assert len(locks) == 1


def test_estado_dice_con_que_cuenta_va_cada_venta():
    pool, _ = _pool(2)
    pool.reservar_para("booking-a")
    estado = pool.estado()
    assert [e["cuenta"] for e in estado] == ["Y1", "Y2"]
    assert [e["estado"] for e in estado] == ["en_uso", "libre"]
    assert [e["indice"] for e in estado] == [0, 1]


def test_el_reaper_recorre_todas_las_cuentas():
    pool, clientes = _pool(2)
    pool.reservar_para("booking-a")
    b = pool.reservar_para("booking-b")
    b.last_activity -= 10_000
    b.asientos_en_venta["07"] = {"ids": 2001, "programacion": 1}
    clientes[1].bloqueos[2001] = "07"
    liberados = asyncio.run(pool.liberar_inactivos(1200))
    assert [d for d, _ in liberados] == ["booking-b"]
    assert clientes[1].bloqueos == {} and pool.worker_de("booking-b") is None
    assert pool.worker_de("booking-a") is not None


def test_venta_completa_en_la_segunda_cuenta(entorno_venta, cliente, monkeypatch):
    """De punta a punta por la API: con la primera cuenta ocupada, la compra
    bloquea, graba y emite con la segunda (y su Terminal), nunca con la primera."""
    import api_ventas
    from tests.test_venta import comprar, correr

    primera, otra = entorno_venta.jelaf, JelafHttpFalso()
    primera.usuario, otra.usuario = "Y1", "Y2"
    otra.cajero["Terminal"] = "702"
    pool = PoolCuentas([primera, otra], ventas_por_cuenta=1, simulacion=False)
    monkeypatch.setattr(api_ventas, "pool_jelaf", pool)

    assert pool.reservar_para("ocupa-la-primera-cuenta").vendedor.jelaf is primera
    r = correr(comprar(cliente, entorno_venta))
    assert r.status_code == 200, r.text
    assert r.json()["data"]["boletos"][0]["nro_boleto"] == "B001-00000001"
    assert otra.llamadas["grabar_venta"] == 1 and otra.llamadas["bloquear"] == 1
    assert primera.llamadas["grabar_venta"] == 0 and primera.llamadas["bloquear"] == 0
    assert otra.ventas_recibidas[0]["Listado"][0]["CodiTerminal"] == "702"


def test_arranque_con_varias_cuentas_y_una_que_no_entra(monkeypatch):
    """lifespan: la primera entra antes de abrir, las demás en segundo plano;
    la que falla no recibe ventas y deja incidencia (sin la clave en el texto)."""
    import dataclasses

    import api_ventas
    from paneles import consultas

    class ClienteFalso(JelafHttpFalso):
        def __init__(self, usuario, password):
            super().__init__()
            self._usuario, self._password, self.disponible = usuario, password, False

        @property
        def usuario(self):
            return self._usuario

        async def iniciar(self):
            self.disponible = self._usuario != "Y2"     # Y2 no logra entrar

    monkeypatch.setattr(api_ventas, "ClienteJelafHttp", ClienteFalso)
    monkeypatch.setattr(api_ventas, "PAUSA_ENTRE_LOGINS_SEGUNDOS", 0)
    monkeypatch.setattr(api_ventas, "CONFIG", dataclasses.replace(
        api_ventas.CONFIG, jelaf_credenciales=[("Y1", "c1"), ("Y2", "c2"), ("Y3", "c3"), ("VIEJA", "cv")],
        jelaf_cuenta_reserva="VIEJA", ventas_por_cuenta=1))

    async def escenario():
        async with api_ventas.lifespan(api_ventas.app):
            await api_ventas._logins_task
            pool = api_ventas.pool_jelaf
            usadas = [pool.reservar_para(f"b-{i}") for i in range(4)]
            return pool, [v.vendedor.jelaf.usuario if v else None for v in usadas]
    pool, usadas = asyncio.run(escenario())
    assert pool.capacidad == 4
    assert usadas == ["Y1", "Y3", "VIEJA", None]          # Y2 sin sesión; la reserva al final
    textos = [i["mensaje"] for i in consultas.incidencias_abiertas()]
    assert any("Y2" in m for m in textos) and not any("c2" in m for m in textos)
