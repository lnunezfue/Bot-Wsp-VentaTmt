"""Tramos (rutas cortas) del Excel y de JELAF, y su uso en /buscar-viajes."""
from datetime import timedelta

import rutas
from paneles import tiempo
from tests.test_flujo_robusto import correr


# --- rutas.py (puro) ------------------------------------------------------------------ #
def test_tramo_solo_en_el_sentido_de_la_ruta():
    assert rutas.tramo_valido("ICA", "NASCA")
    assert rutas.tramo_valido("NASCA", "ICA")            # existe la ruta de vuelta
    assert rutas.tramo_valido("MOQUEGUA", "TACNA")
    assert rutas.tramo_valido("KM48", "MOQUEGUA")       # Arequipa→Tacna pasa por las dos
    assert not rutas.tramo_valido("ILO", "MOQUEGUA")    # ninguna ruta pasa por ambas
    assert not rutas.tramo_valido("ALTO SIGUAS", "TACNA")
    assert not rutas.tramo_valido("ICA", "ICA")


def test_rutas_principales_en_los_dos_sentidos():
    for o, d in [("LIMA", "TACNA"), ("LIMA", "AREQUIPA"), ("AREQUIPA", "TACNA")]:
        assert rutas.tramo_valido(o, d) and rutas.tramo_valido(d, o)


def test_rutas_confirmadas_en_jelaf_que_no_estan_en_el_excel():
    # Barrido de JELAF (2026-10-07): Tacna-Cusco por Puno y Juliaca, Cerro Azul y Fiscal.
    for o, d in [("TACNA", "CUSCO"), ("CUSCO", "TACNA"), ("MOQUEGUA", "PUNO"), ("JULIACA", "CAMIARA"),
                 ("LIMA", "CERRO AZUL"), ("CERRO AZUL", "ICA"), ("AREQUIPA", "FISCAL"), ("FISCAL", "TACNA")]:
        assert rutas.tramo_valido(o, d), (o, d)
    assert rutas.duracion_minutos("CUSCO", "PUNO") == 7 * 60 + 30
    assert rutas.duracion_minutos("TACNA", "CUSCO") is None        # sin horas confiables


def test_tramos_sin_bus_en_jelaf_se_bloquean():
    # La web oficial lista Arequipa a Mollendo / Ilo, pero en JELAF todos los buses
    # de Arequipa van a Lima o a Tacna. Lima/Arequipa-Cusco no existen.
    for o, d in [("AREQUIPA", "MOLLENDO"), ("AREQUIPA", "ILO"), ("LIMA", "CUSCO"), ("AREQUIPA", "CUSCO"),
                 ("CUSCO", "LIMA"), ("PUNO", "AREQUIPA")]:
        assert not rutas.tramo_valido(o, d) and not rutas.tramo_valido(d, o), (o, d)


def test_ilo_no_se_ofrece_en_el_bus_de_cusco():
    # JELAF lo lista, pero el bus va de Moquegua a Camiara en 1 h: no pasa por Ilo.
    for o in ("CUSCO", "JULIACA", "PUNO", "MOQUEGUA"):
        assert not rutas.tramo_valido(o, "ILO")


def test_ciudad_desconocida_no_es_tramo():
    assert not rutas.tramo_valido("JULIACA", "LA PAZ")
    assert not rutas.tramo_valido("LIMA", "")


def test_duracion_del_tramo_sale_de_la_programacion():
    assert rutas.duracion_minutos("LIMA", "ICA") == 5 * 60
    assert rutas.duracion_minutos("CAMANA", "MOLLENDO") == 2 * 60
    assert rutas.duracion_minutos("LIMA", "TACNA") == 21 * 60 + 15    # la más corta: por Ilo
    assert rutas.duracion_minutos("TACNA", "ILO") == 2 * 60 + 30
    assert rutas.duracion_minutos("ILO", "MOQUEGUA") is None


def test_catalogo_marca_las_sedes_sin_codigo_de_jelaf():
    cat = rutas.catalogo({"LIMA", "ICA"})
    sedes = {s["codigo"]: s for s in cat["sedes"]}
    assert set(sedes) == set(rutas.SEDES)
    assert sedes["ICA"]["habilitada"] and not sedes["NASCA"]["habilitada"]
    assert cat["destinos"]["MOLLENDO"] == ["LIMA", "CERRO AZUL", "ICA", "NASCA", "YAUCA", "CHALA", "ATICO",
                                           "CAMANA", "ILO", "MOQUEGUA", "CAMIARA", "TACNA"]
    assert "TACNA-PUNO" not in cat["duraciones"]            # sin horas medidas: sin duración
    assert cat["duraciones"]["LIMA-ICA"] == 300


def test_toda_escala_de_las_rutas_tiene_sede():
    for escalas in rutas.RUTAS.values():
        for ciudad, _ in escalas:
            assert ciudad in rutas.SEDES


# --- /api/v1/rutas y /api/v1/buscar-viajes --------------------------------------------- #
def _manana():
    return (tiempo.hoy() + timedelta(days=1)).isoformat()


def test_endpoint_rutas(cliente):
    r = correr(cliente.get("/api/v1/rutas"))
    assert r.status_code == 200
    sedes = {s["codigo"]: s["habilitada"] for s in r.json()["data"]["sedes"]}
    assert sedes["LIMA"] and sedes["TACNA"]


def test_tramo_inexistente_responde_400(cliente):
    r = correr(cliente.post("/api/v1/buscar-viajes", json={"origen": "ILO", "destino": "MOQUEGUA",
                                                             "fecha": _manana()}))
    assert r.status_code == 400


def test_todas_las_sedes_tienen_codigo_de_jelaf():
    from jelaf_http import CIUDADES_CODIGOS
    assert set(rutas.SEDES) <= set(CIUDADES_CODIGOS)
    assert len(set(CIUDADES_CODIGOS.values())) == len(CIUDADES_CODIGOS)   # sin códigos repetidos


def test_sede_sin_codigo_de_jelaf_no_se_busca(entorno_venta, cliente, monkeypatch):
    import api_ventas
    monkeypatch.delitem(api_ventas.CIUDADES_CODIGOS, "NASCA")
    llamadas = []
    monkeypatch.setattr(entorno_venta.jelaf, "buscar_itinerarios", lambda *a: llamadas.append(a) or [])
    r = correr(cliente.post("/api/v1/buscar-viajes", json={"origen": "LIMA", "destino": "NASCA",
                                                             "fecha": _manana()}))
    assert r.status_code == 400 and "habilitada" in r.json()["detail"]
    assert llamadas == []


def test_tramo_busca_con_destino_y_solo_acepta_turnos_del_tramo(entorno_venta, cliente, monkeypatch):
    import api_ventas
    monkeypatch.setitem(api_ventas.CIUDADES_CODIGOS, "ICA", 77)
    monkeypatch.setattr(api_ventas.planos, "placas_configuradas", lambda: {"ZBA963"})
    pedidos = []

    def buscar(codi_origen, fecha, hora, codi_destino=0):
        pedidos.append((codi_origen, codi_destino))
        base = {"PlacaBus": "ZBA963", "HoraPartida": "09:00PM", "NomDestino": "TODOS", "NomRuta": "AREQUIPA"}
        # El primero trae los códigos del tramo; el segundo, los de la ruta
        # completa (venderlo cobraría Lima→Arequipa): no debe aparecer.
        return [{**base, "CodiProgramacion": 1, "CodiDestino": "77"},
                {**base, "CodiProgramacion": 2, "CodiDestino": 3}]
    monkeypatch.setattr(entorno_venta.jelaf, "buscar_itinerarios", buscar)

    r = correr(cliente.post("/api/v1/buscar-viajes", json={"origen": "LIMA", "destino": "ICA",
                                                             "fecha": _manana()}))
    assert r.status_code == 200
    assert pedidos == [(1, 77)]
    assert [v["id_programacion"] for v in r.json()["data"]] == [1]


def test_tramo_descarta_el_bus_que_va_en_sentido_contrario(entorno_venta, cliente, monkeypatch):
    # Visto en JELAF el 2026-10-07: Moquegua→Puno devuelve también el bus Cusco→Tacna
    # (pasa por Moquegua a las 4:32 AM rumbo a Tacna, ya pasó por Puno). Venderlo
    # subiría al pasajero al bus equivocado.
    import api_ventas
    monkeypatch.setattr(api_ventas.planos, "placas_configuradas", lambda: {"CPQ190"})
    base = {"PlacaBus": "CPQ190", "CodiDestino": 56, "NomDestino": "TODOS"}
    monkeypatch.setattr(entorno_venta.jelaf, "buscar_itinerarios", lambda *a: [
        {**base, "CodiProgramacion": 57132, "HoraPartida": "04:32AM", "NomRuta": "TACNA"},
        {**base, "CodiProgramacion": 57085, "HoraPartida": "10:30PM", "NomRuta": "CUSCO"},
        {**base, "CodiProgramacion": 1, "HoraPartida": "11:00PM"},          # sin NomRuta: no se arriesga
    ])
    r = correr(cliente.post("/api/v1/buscar-viajes", json={"origen": "MOQUEGUA", "destino": "PUNO",
                                                             "fecha": _manana()}))
    assert [v["id_programacion"] for v in r.json()["data"]] == [57085]


def test_sentido_por_fin_de_ruta():
    assert rutas.va_en_sentido("MOQUEGUA", "PUNO", "CUSCO")
    assert not rutas.va_en_sentido("MOQUEGUA", "PUNO", "TACNA")
    assert rutas.va_en_sentido("LIMA", "ICA", "AREQUIPA") and rutas.va_en_sentido("LIMA", "ICA", "TACNA")
    assert not rutas.va_en_sentido("ICA", "LIMA", "TACNA")
    assert not rutas.va_en_sentido("LIMA", "ICA", "")


def _turnos_por_tramo(monkeypatch, jelaf, precios_por_destino, ocupados=(), falla=()):
    """consultar_turno falso: precios según el destino del tramo (como JELAF real, 2026-10-13)."""
    from decimal import Decimal

    from jelaf_http import ErrorJelaf, EstadoTurno
    pedidos = []

    def consultar(viaje):
        pedidos.append((viaje.codi_origen, viaje.codi_destino, viaje.hora_viaje))
        if viaje.codi_destino in falla:
            raise ErrorJelaf("JELAF no respondió.")
        precios = {a: Decimal(p) for a, p in precios_por_destino[viaje.codi_destino].items()}
        return EstadoTurno(ocupados=set(ocupados), precios=precios)
    monkeypatch.setattr(jelaf, "consultar_turno", consultar)
    return pedidos


def _buscar_dos_veces(cliente, cuerpo):
    """1.ª búsqueda: encola los precios (sale al instante, pendiente). 2.ª: ya con el precio guardado."""
    import api_ventas
    primera = correr(cliente.post("/api/v1/buscar-viajes", json=cuerpo))
    assert api_ventas._esperar_desde(5), "la cola de precios no terminó"
    return primera, correr(cliente.post("/api/v1/buscar-viajes", json=cuerpo))


def test_precio_desde_es_el_del_tramo_y_solo_de_asientos_libres(entorno_venta, cliente, monkeypatch):
    # Lima→Ica mostraba "Desde (aprox.) S/ 60": un número fijo de la pantalla. JELAF
    # da el precio del tramo en el plano (Lima→Ica 130/140 frente a 180/190 a Tacna).
    import api_ventas
    monkeypatch.setitem(api_ventas.CIUDADES_CODIGOS, "ICA", 7)
    monkeypatch.setattr(api_ventas.planos, "placas_configuradas", lambda: {"CPP523"})
    api_ventas._cache_desde.clear()
    monkeypatch.setattr(entorno_venta.jelaf, "buscar_itinerarios", lambda *a: [
        {"PlacaBus": "CPP523", "HoraPartida": "10:45AM", "NomRuta": "TACNA", "CodiProgramacion": 57521,
         "CodiOrigen": 1, "CodiDestino": 7}])
    pedidos = _turnos_por_tramo(monkeypatch, entorno_venta.jelaf,
                                {7: {"33": 100, "34": 140, "39": 130}}, ocupados={"33"})
    primera, r = _buscar_dos_veces(cliente, {"origen": "LIMA", "destino": "ICA", "fecha": _manana()})
    assert primera.json()["data"][0]["precio_base"] == 0 and primera.json()["data"][0]["precio_pendiente"]
    assert r.status_code == 200
    assert r.json()["data"][0]["precio_base"] == 130          # el 33 (100) está ocupado
    assert r.json()["data"][0]["precio_pendiente"] is False
    assert pedidos == [(1, 7, "10:45AM")]                      # el plano del TRAMO, no el de la ruta


def test_precio_desde_sin_respuesta_de_jelaf_no_tumba_la_busqueda(entorno_venta, cliente, monkeypatch):
    import api_ventas
    monkeypatch.setattr(api_ventas.planos, "placas_configuradas", lambda: {"ZBA963"})
    api_ventas._cache_desde.clear()
    monkeypatch.setattr(entorno_venta.jelaf, "buscar_itinerarios", lambda *a: [
        {"PlacaBus": "ZBA963", "HoraPartida": "09:00PM", "NomDestino": "AREQUIPA", "CodiDestino": 3,
         "CodiProgramacion": 1}])
    _turnos_por_tramo(monkeypatch, entorno_venta.jelaf, {}, falla={3})
    _, r = _buscar_dos_veces(cliente, {"origen": "TACNA", "destino": "AREQUIPA", "fecha": _manana()})
    assert r.status_code == 200 and r.json()["data"][0]["precio_base"] == 0   # la pantalla dice "al elegir asiento"
    assert r.json()["data"][0]["bloqueado"] is False


def test_precio_desde_se_reusa_un_rato_sin_volver_a_pedir_el_plano(entorno_venta, cliente, monkeypatch):
    import api_ventas
    monkeypatch.setattr(api_ventas.planos, "placas_configuradas", lambda: {"ZBA963"})
    api_ventas._cache_desde.clear()
    monkeypatch.setattr(entorno_venta.jelaf, "buscar_itinerarios", lambda *a: [
        {"PlacaBus": "ZBA963", "HoraPartida": "09:00PM", "NomDestino": "AREQUIPA", "CodiDestino": 3,
         "CodiProgramacion": 1}])
    pedidos = _turnos_por_tramo(monkeypatch, entorno_venta.jelaf, {3: {"05": 60}})
    for _ in range(3):
        _buscar_dos_veces(cliente, {"origen": "TACNA", "destino": "AREQUIPA", "fecha": _manana()})
    assert len(pedidos) == 1


def test_precios_de_a_uno_nunca_en_paralelo(entorno_venta, cliente, monkeypatch):
    # Incidente 2026-10-14: abrir todos los planos a la vez trabó la sesión de JELAF
    # (atiende de a una) y tumbó /plano-bus de los clientes. Ahora, de a uno.
    import threading
    import time as _t
    from decimal import Decimal

    from jelaf_http import EstadoTurno
    import api_ventas
    monkeypatch.setattr(api_ventas.planos, "placas_configuradas", lambda: {"ZBA963"})
    api_ventas._cache_desde.clear()
    monkeypatch.setattr(entorno_venta.jelaf, "buscar_itinerarios", lambda *a: [
        {"PlacaBus": "ZBA963", "HoraPartida": f"0{h}:00PM", "NomDestino": "AREQUIPA", "CodiDestino": 3,
         "CodiProgramacion": h} for h in range(1, 9)])
    en_curso, maximo, lock = [0], [0], threading.Lock()

    def consultar(viaje):
        with lock:
            en_curso[0] += 1
            maximo[0] = max(maximo[0], en_curso[0])
        _t.sleep(0.05)
        with lock:
            en_curso[0] -= 1
        return EstadoTurno(precios={"05": Decimal(60)})
    monkeypatch.setattr(entorno_venta.jelaf, "consultar_turno", consultar)
    cuerpo = {"origen": "TACNA", "destino": "AREQUIPA", "fecha": _manana()}
    for _ in range(3):        # varias búsquedas seguidas: no se repiten ni se suman en paralelo
        correr(cliente.post("/api/v1/buscar-viajes", json=cuerpo))
    assert api_ventas._esperar_desde(5)
    assert maximo[0] == 1
    r = correr(cliente.post("/api/v1/buscar-viajes", json=cuerpo))
    assert all(v["precio_base"] == 60 and not v["precio_pendiente"] for v in r.json()["data"])


def test_la_pantalla_pide_los_precios_ya_guardados(entorno_venta, cliente, monkeypatch):
    import api_ventas
    monkeypatch.setattr(api_ventas.planos, "placas_configuradas", lambda: {"ZBA963"})
    api_ventas._cache_desde.clear()
    monkeypatch.setattr(entorno_venta.jelaf, "buscar_itinerarios", lambda *a: [
        {"PlacaBus": "ZBA963", "HoraPartida": "09:00PM", "NomDestino": "AREQUIPA", "CodiDestino": 3,
         "CodiOrigen": 2, "CodiProgramacion": 77}])
    _turnos_por_tramo(monkeypatch, entorno_venta.jelaf, {3: {"05": 60, "06": 55}}, ocupados={"06"})
    fecha = _manana()
    v = correr(cliente.post("/api/v1/buscar-viajes", json={"origen": "TACNA", "destino": "AREQUIPA",
                                                             "fecha": fecha})).json()["data"][0]
    assert api_ventas._esperar_desde(5)
    pedido = {"fecha": fecha, "viajes": [{"id_programacion": 77, "codi_origen": v["codi_origen"],
                                          "codi_destino": v["codi_destino"]}]}
    r = correr(cliente.post("/api/v1/precios-desde", json=pedido))
    assert r.json() == {"status": "success", "precios": {"77": {"precio_base": 60.0, "bloqueado": False,
                                                                "motivo_bloqueo": None, "pendiente": False}}}


def test_turnos_cerrados_se_muestran_bloqueados_y_sin_pedir_su_plano(entorno_venta, cliente, monkeypatch):
    # Como la pantalla de JELAF (codigo_jelaf/itinerarios.html): ProgramacionCerrada
    # '1' = "La ruta está bloqueada para esta fecha", '2' = "desde esta fecha hacia
    # adelante"; PlanoCerradoWeb = cerrado para la venta web. 2026-10-14: la app los
    # ofrecía y ningún asiento tenía precio.
    import api_ventas
    monkeypatch.setattr(api_ventas.planos, "placas_configuradas", lambda: {"ZBA963"})
    api_ventas._cache_desde.clear()
    base = {"PlacaBus": "ZBA963", "NomDestino": "AREQUIPA", "CodiDestino": 3}
    monkeypatch.setattr(entorno_venta.jelaf, "buscar_itinerarios", lambda *a: [
        {**base, "CodiProgramacion": 1, "HoraPartida": "08:00PM", "ProgramacionCerrada": "0", "PlanoCerradoWeb": False},
        {**base, "CodiProgramacion": 2, "HoraPartida": "09:00PM", "ProgramacionCerrada": "1"},
        {**base, "CodiProgramacion": 3, "HoraPartida": "10:00PM", "ProgramacionCerrada": "2"},
        {**base, "CodiProgramacion": 4, "HoraPartida": "11:00PM", "ProgramacionCerrada": "0", "PlanoCerradoWeb": True},
    ])
    pedidos = _turnos_por_tramo(monkeypatch, entorno_venta.jelaf, {3: {"05": 60}})
    _, r = _buscar_dos_veces(cliente, {"origen": "TACNA", "destino": "AREQUIPA", "fecha": _manana()})
    viajes = {v["id_programacion"]: v for v in r.json()["data"]}
    assert viajes[1]["bloqueado"] is False and viajes[1]["precio_base"] == 60
    assert all(viajes[p]["bloqueado"] for p in (2, 3, 4))
    assert "bloquead" in viajes[2]["motivo_bloqueo"] and "línea" in viajes[4]["motivo_bloqueo"]
    assert len(pedidos) == 1                                    # el plano solo del turno abierto


def test_turno_sin_ningun_precio_se_muestra_cerrado(entorno_venta, cliente, monkeypatch):
    import api_ventas
    monkeypatch.setattr(api_ventas.planos, "placas_configuradas", lambda: {"ZBA963"})
    api_ventas._cache_desde.clear()
    monkeypatch.setattr(entorno_venta.jelaf, "buscar_itinerarios", lambda *a: [
        {"PlacaBus": "ZBA963", "NomDestino": "AREQUIPA", "CodiDestino": 3, "CodiProgramacion": 1,
         "HoraPartida": "08:00PM", "ProgramacionCerrada": "0"}])
    _turnos_por_tramo(monkeypatch, entorno_venta.jelaf, {3: {"05": 0, "06": 0}})
    _, r = _buscar_dos_veces(cliente, {"origen": "TACNA", "destino": "AREQUIPA", "fecha": _manana()})
    v = r.json()["data"][0]
    assert v["bloqueado"] is True and v["precio_base"] == 0


def test_plano_de_un_turno_cerrado_no_se_abre(entorno_venta, cliente, monkeypatch):
    # Xcierre = '1' en el turno: "La programación se encuentra cerrada" (JELAF).
    import api_ventas
    monkeypatch.setattr(api_ventas.planos, "plano_por_placa", lambda placa: {"1": {"rows": []}})
    crudo = entorno_venta.jelaf.turno_crudo
    monkeypatch.setattr(entorno_venta.jelaf, "turno_crudo", lambda viaje: {**crudo(viaje), "Xcierre": "1"})
    cuerpo = {"codi_empresa": 1, "codi_destino": 1, "codi_sucursal": 2, "codi_ruta": 1, "codi_punto_venta": 17,
              "codi_servicio": 3, "codi_origen": 2, "fecha_viaje": _manana(), "hora_viaje": "08:30PM",
              "placa_bus": "ZBA963"}
    r = correr(cliente.post("/api/v1/plano-bus", json=cuerpo))
    assert r.status_code == 409 and "cerrado" in r.json()["detail"]


def test_diagnostico_dice_por_que_se_descarta_cada_turno(entorno_venta, cliente, monkeypatch):
    # 2026-10-14: Ica→Lima devolvía 0 turnos y JELAF tenía 6. Sin ver qué
    # respondió JELAF no se sabía qué filtro los quitaba.
    import api_ventas
    monkeypatch.setitem(api_ventas.CIUDADES_CODIGOS, "ICA", 7)
    monkeypatch.setattr(api_ventas.planos, "placas_configuradas", lambda: {"ZBA963"})
    api_ventas._cache_desde.clear()
    base = {"PlacaBus": "ZBA963", "HoraPartida": "09:00PM", "CodiDestino": 1, "NomRuta": "LIMA"}
    monkeypatch.setattr(entorno_venta.jelaf, "buscar_itinerarios", lambda *a: [
        {**base, "CodiProgramacion": 1},
        {**base, "CodiProgramacion": 2, "CodiDestino": 2},
        {**base, "CodiProgramacion": 3, "NomRuta": "TACNA"},
        {**base, "CodiProgramacion": 4, "PlacaBus": "XXX000"},
    ])
    _turnos_por_tramo(monkeypatch, entorno_venta.jelaf, {1: {"05": 60}})
    cuerpo = {"origen": "ICA", "destino": "LIMA", "fecha": _manana()}
    sin_clave = correr(cliente.post("/api/v2/diagnostico/buscar-viajes", json=cuerpo))
    r = correr(cliente.post("/api/v2/diagnostico/buscar-viajes", json=cuerpo,
                            headers={"X-Paneles-Token": "clave-panel-prueba"}))
    assert sin_clave.status_code == 401
    motivos = {t["CodiProgramacion"]: t["descarte"] for t in r.json()["turnos"]}
    assert motivos[1] is None
    assert "destino" in motivos[2] and "sentido" in motivos[3] and "plano" in motivos[4]
    assert r.json()["consulta"]["codi_destino"] == 1


def test_entre_ciudades_de_siempre_se_busca_como_antes(entorno_venta, cliente, monkeypatch):
    import api_ventas
    monkeypatch.setattr(api_ventas.planos, "placas_configuradas", lambda: {"ZBA963"})
    pedidos = []

    def buscar(codi_origen, fecha, hora, codi_destino=0):
        pedidos.append((codi_origen, codi_destino))
        return [{"PlacaBus": "ZBA963", "HoraPartida": "09:00PM", "NomDestino": "AREQUIPA", "CodiDestino": 3}]
    monkeypatch.setattr(entorno_venta.jelaf, "buscar_itinerarios", buscar)

    r = correr(cliente.post("/api/v1/buscar-viajes", json={"origen": "TACNA", "destino": "AREQUIPA",
                                                             "fecha": _manana()}))
    assert pedidos == [(2, 0)] and len(r.json()["data"]) == 1
