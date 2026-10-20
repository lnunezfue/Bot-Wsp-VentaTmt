"""
El bot hace las llamadas que hace la pantalla de venta de JELAF (04/10/2026).

Dos ventas reales del bot quedaron en JELAF sin nombre. La venta y la ficha
eran iguales a las de boletería (comparar_venta_boleteria.py), pero la pantalla
de JELAF hace más llamadas alrededor: el manifiesto antes de bloquear y antes
de vender, las promociones al cargar el DNI, el permiso del usuario al aceptar
y, después de grabar la venta, convertir-venta-to-base64 (el comprobante, que
reenvía tal cual lo que devolvió grabar-venta, con PasNombreCom) y grabar-log.
PANELES y la versión del compañero las tenían gratis porque usaban la pantalla.
"""

from __future__ import annotations

import asyncio

from paneles import db
from tests.test_venta import comprar


def correr(coro):
    return asyncio.run(coro)


def contar(sql, params=()):
    return db.consultar_uno(sql, params)[0]


def test_venta_real_hace_las_llamadas_en_el_orden_de_boleteria(entorno_venta, cliente):
    from tests.test_venta import BOOKING, preparar_venta

    async def escenario():
        tx = await preparar_venta(cliente)
        # Mientras el cliente paga (minutos con tarjeta), la sesión de JELAF del bot
        # se usa para otras cosas: autocompletar otro DNI, otra compra...
        entorno_venta.jelaf.consultar_usuario("01", "99999999")
        entorno_venta.jelaf.secuencia.append("otra_actividad")
        entorno_venta.yupy.aprobar(tx)
        return await cliente.post("/api/v2/venta/confirmar", json={"booking_id": BOOKING, "referencia_pago": tx})
    r = correr(escenario())
    assert r.status_code == 200, r.text
    s = entorno_venta.jelaf.secuencia
    # Al elegir asiento: manifiesto y luego el bloqueo.
    assert s.index("manifiesto") < s.index("bloquear")
    # Al cargar el DNI: consultar-usuario y las tres promociones.
    assert s.index("consultar_usuario") < s.index("promociones")
    # Al vender, TODO SEGUIDO como boletería (DNI → promociones → grabar-pasajero →
    # aceptar): JELAF arma la venta con el pasajero grabado en la sesión; si entre
    # grabar-pasajero y grabar-venta pasan minutos, la venta salía sin nombre.
    final = s[s.index("otra_actividad") + 1:]
    assert final == ["consultar_usuario", "promociones", "grabar_pasajero", "acceso", "manifiesto",
                     "liberar_arreglo", "grabar_venta", "convertir", "grabar_log"]


def test_el_comprobante_se_arma_como_en_la_pantalla_de_jelaf(entorno_venta, cliente):
    assert correr(comprar(cliente, entorno_venta)).status_code == 200
    j = entorno_venta.jelaf
    cuerpo, respuesta = j.convertidos[0], j.respuestas_venta[-1]
    # Lo que devolvió grabar-venta, más lo que agrega la pantalla (captura del 29/09).
    item, original = cuerpo["ListaVentasRealizadas"][0], respuesta["ListaVentasRealizadas"][0]
    assert all(item[k] == v for k, v in original.items())
    assert item["PasNombreCom"] and item["UsuarioCodTerminal"] == "022"
    assert item["UsuarioNombre"] == j.cajero["Nombre"] and item["UsuarioNomPVenta"] == j.cajero["NomPuntoVenta"]
    assert item["verPaseDeEmbarque"] is False and item["ocultarQRTerminosYCondiciones"] is False
    assert {k: cuerpo[k] for k in ("TipoImpresion", "CodiRutaConcatenado")} == {
        "TipoImpresion": "Impresion", "CodiRutaConcatenado": ""}
    assert "TurnoLiq" in cuerpo and "TerminalLiq" in cuerpo


def test_las_promociones_llevan_el_dni_y_el_nombre_completo(entorno_venta, cliente):
    assert correr(comprar(cliente, entorno_venta)).status_code == 200
    promo = entorno_venta.jelaf.promociones[-1]
    assert promo["Numero"] == "00000001" and promo["Tipo"] == "01"
    assert promo["Nombre"] == "ANA MARÍA,PÉREZ,ROJAS"


def test_si_el_comprobante_falla_la_venta_queda_hecha_y_hay_incidencia(entorno_venta, cliente):
    entorno_venta.jelaf.falla_convertir = True
    r = correr(comprar(cliente, entorno_venta))
    assert r.status_code == 200 and r.json()["data"]["boletos"][0]["nro_boleto"].startswith("B001-")
    assert contar("SELECT COUNT(*) FROM incidencia WHERE tipo = 'comprobante_jelaf'") == 1


def test_si_jelaf_registra_la_venta_sin_nombre_queda_incidencia_critica(entorno_venta, cliente):
    entorno_venta.jelaf.nombre_en_respuesta = ""
    assert correr(comprar(cliente, entorno_venta)).status_code == 200
    assert contar("SELECT COUNT(*) FROM incidencia WHERE tipo = 'venta_sin_nombre' AND severidad = 'critica'") == 1


def test_una_consulta_que_falla_no_tumba_la_venta(entorno_venta, cliente):
    entorno_venta.jelaf.falla_consultas = True
    assert correr(comprar(cliente, entorno_venta)).status_code == 200


def test_en_simulacion_no_se_envian_ni_el_comprobante_ni_la_bitacora(entorno_venta, cliente):
    entorno_venta.vendedor.simulacion = True
    assert correr(comprar(cliente, entorno_venta)).status_code == 200
    assert "convertir" not in entorno_venta.jelaf.secuencia
    assert "grabar_log" not in entorno_venta.jelaf.secuencia


def test_si_grabar_pasajero_falla_justo_antes_de_vender_no_se_emite(entorno_venta, cliente):
    from tests.test_venta import preparar_venta, BOOKING

    async def escenario():
        tx = await preparar_venta(cliente)          # el checkout ya grabó al pasajero
        entorno_venta.yupy.aprobar(tx)
        entorno_venta.jelaf.falla_grabar_pasajero = True
        return await cliente.post("/api/v2/venta/confirmar", json={"booking_id": BOOKING, "referencia_pago": tx})
    r = correr(escenario())
    assert r.status_code != 200
    assert entorno_venta.jelaf.llamadas["grabar_venta"] == 0


def test_los_nombres_escritos_en_la_app_ganan_a_los_que_reconoce_jelaf(entorno_venta, cliente, monkeypatch):
    """
    A veces JELAF reconoce mal el DNI (otra persona, nombre a medias): lo que el
    cliente escribió o corrigió en la app es lo que va a la ficha y a la venta.
    Se usa el IdCliente de la ficha para ACTUALIZARLA, no para copiar sus nombres.
    """
    j = entorno_venta.jelaf

    def ficha_equivocada(tipo, doc):
        j.secuencia.append("consultar_usuario")
        return {"EsCorrecto": True, "Valor": {"IdCliente": 777, "NombreCliente": "JUAN", "ApellidoPaterno": "QUISPE",
                                               "ApellidoMaterno": "MAMANI", "FechaNacimiento": "01/01/1980", "Sexo": "M"}}
    monkeypatch.setattr(j, "consultar_usuario", ficha_equivocada)
    assert correr(comprar(cliente, entorno_venta)).status_code == 200
    ficha = j.clientes_grabados[-1]
    assert (ficha["IdCliente"], ficha["NombreCliente"], ficha["ApellidoPaterno"], ficha["ApellidoMaterno"]) == (
        777, "ANA MARÍA", "PÉREZ", "ROJAS")
    assert j.ventas_recibidas[-1]["Listado"][0]["Nombre"] == "ANA MARÍA,PÉREZ,ROJAS"


def test_la_venta_sin_nombre_guarda_el_diagnostico_sin_datos_personales(entorno_venta, cliente):
    import json
    entorno_venta.jelaf.nombre_en_respuesta = ""
    assert correr(comprar(cliente, entorno_venta)).status_code == 200
    fila = db.consultar_uno("SELECT contexto FROM incidencia WHERE tipo = 'venta_sin_nombre'")
    diag = json.loads(fila[0])
    assert diag["largo_nombre_enviado"] == [21] and diag["largo_nombre_registrado"] == [0]
    assert diag["misma_sesion_jelaf"] is True and diag["segundos_desde_grabar_pasajero"] >= 0
    assert "ANA" not in fila[0]


def test_la_venta_queda_registrada_con_nombre(entorno_venta, cliente):
    """El error de las 5 boletas sin nombre: JELAF devolvía PasNombreCom vacío."""
    assert correr(comprar(cliente, entorno_venta)).status_code == 200
    assert entorno_venta.jelaf.respuestas_venta[-1]["ListaVentasRealizadas"][0]["PasNombreCom"] == "ANA MARÍA PÉREZ ROJAS"
    assert contar("SELECT COUNT(*) FROM incidencia WHERE tipo = 'venta_sin_nombre'") == 0
