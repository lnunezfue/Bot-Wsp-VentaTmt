"""El script de descubrimiento nunca debe escribir datos personales ni credenciales."""

from __future__ import annotations

import json

from descubrir_endpoints_venta import (
    cabeceras_propias,
    es_relevante,
    redactar_cuerpo,
    ruta_sin_datos,
)

DATOS_REALES = ["72677853", "ANA MARIA", "PEREZ", "ROJAS", "987654321", "4111111111111111",
                "clave-secreta", "01/02/1990", "613"]


def _sin_pii(obj) -> bool:
    texto = json.dumps(obj, ensure_ascii=False)
    return not any(dato in texto for dato in DATOS_REALES)


def test_pasajero_en_json_queda_sin_datos_pero_con_estructura():
    cuerpo = json.dumps({
        "NumeroDocumento": "72677853", "Nombres": "ANA MARIA", "ApellidoPaterno": "PEREZ",
        "ApellidoMaterno": "ROJAS", "FechaNacimiento": "01/02/1990", "Telefono": "987654321",
        "CodiProgramacion": 4512, "NumeAsiento": "05", "PrecioVenta": 120.0, "TipoDocumento": "01",
    })
    r = redactar_cuerpo(cuerpo, "application/json")
    assert _sin_pii(r)
    assert set(r) >= {"NumeroDocumento", "Nombres", "CodiProgramacion"}    # la forma se conserva
    assert r["CodiProgramacion"] == 4512 and r["NumeAsiento"] == "05" and r["TipoDocumento"] == "01"


def test_login_por_formulario_no_guarda_usuario_ni_clave():
    r = redactar_cuerpo("usuario=613&contrasena=clave-secreta&recordar=true", "application/x-www-form-urlencoded")
    assert _sin_pii(r) and set(r["<formulario>"]) == {"usuario", "contrasena", "recordar"}


def test_json_dentro_de_un_texto_tambien_se_tapa():
    interno = json.dumps({"Terminal": 22, "IDS": 99, "Documento": "72677853"})
    r = redactar_cuerpo(json.dumps({"request": interno}), "application/json")
    assert _sin_pii(r) and r["request"]["<json-en-texto>"]["Terminal"] == 22


def test_tarjeta_y_numeros_sensibles():
    r = redactar_cuerpo(json.dumps({"NumeroTP": "4111111111111111", "Dni": 72677853}), "application/json")
    assert _sin_pii(r)


def test_url_con_documento_en_parametros():
    r = ruta_sin_datos("https://x/pasajes/itinerarios/consultar-usuario?tipoDoc=01&numeroDoc=72677853")
    assert r["ruta"] == "/pasajes/itinerarios/consultar-usuario" and _sin_pii(r)
    assert r["params"]["tipoDoc"] == "01"


def test_cabeceras_solo_nombres_propios_y_nunca_cookie():
    nombres = cabeceras_propias({"cookie": "ASP.NET_SessionId=abc", "content-type": "x",
                                 "RequestVerificationToken": "valor-secreto", "x-requested-with": "XMLHttpRequest"})
    assert nombres == ["RequestVerificationToken", "x-requested-with"]


def test_solo_peticiones_al_dominio_y_sin_estaticos():
    assert es_relevante("https://moquegua.2jelaf.net.pe/pasajes/itinerarios/turnos", "moquegua.2jelaf.net.pe")
    assert not es_relevante("https://moquegua.2jelaf.net.pe/pasajes/app.js", "moquegua.2jelaf.net.pe")
    assert not es_relevante("https://fonts.googleapis.com/css", "moquegua.2jelaf.net.pe")


def test_numero_de_documento_en_promociones_no_se_filtra():
    """Regresión: JELAF manda el DNI como "Numero" junto a "Tipo" (captura real del 29/09)."""
    cuerpo = json.dumps({"request": json.dumps({"Tipo": "01", "Numero": "72677853", "Fecha": "04/10/2026"})})
    r = redactar_cuerpo(cuerpo, "application/json")
    assert _sin_pii(r)
    assert r["request"]["<json-en-texto>"]["Tipo"] == "01"


def test_id_de_cliente_se_tapa_pero_numeasiento_e_ids_de_venta_no():
    r = redactar_cuerpo(json.dumps({"IdCliente": 571295, "NumeAsiento": "55", "IdVenta": 2064072, "IDS": 7704239}),
                        "application/json")
    assert r["IdCliente"] == "<numero>"
    assert r["NumeAsiento"] == "55" and r["IdVenta"] == 2064072 and r["IDS"] == 7704239


def test_autorizacion_se_describe_sin_guardar_el_valor():
    from descubrir_endpoints_venta import describir_autorizacion
    vistas = {}
    a = describir_autorizacion("Bearer abc.def.secreto", vistas)
    b = describir_autorizacion("Bearer abc.def.secreto", vistas)
    c = describir_autorizacion("Bearer otro-token", vistas)
    assert a == b == {"esquema": "Bearer", "largo": 15, "version": 1}
    assert c["version"] == 2
    assert "secreto" not in json.dumps([a, b, c])


def test_catalogos_permitidos_si_usuarios_no():
    from descubrir_endpoints_venta import valores_catalogo
    datos = {"Valor": [{"id": "03", "label": "TARJETA", "Extra": "x"}]}
    assert valores_catalogo("/pasajes/base/get-tipoPago", datos) == [{"id": "03", "label": "TARJETA"}]
    assert valores_catalogo("/pasajes/base/get-usuarios", {"Valor": [{"id": "1", "label": "JUAN PEREZ"}]}) is None


def test_token_se_ubica_por_nombre_de_clave_sin_guardarlo():
    from descubrir_endpoints_venta import claves_con_token, redactar_almacen
    almacen = {"usuario": json.dumps({"Token": "Ab12Cd34X", "Nombre": "ANA MARIA", "Id_CCaja": 80523}),
               "tema": "oscuro"}
    assert claves_con_token(almacen, "Ab12Cd34X") == ["usuario"]
    red = redactar_almacen(almacen)
    assert "Ab12Cd34X" not in json.dumps(red) and _sin_pii(red)
    assert red["usuario"]["Id_CCaja"] == 80523


def test_factura_muestra_los_codigos_y_tapa_ruc_razon_social_y_direccion():
    # Para facturar hay que ver QUÉ códigos manda boletería (CodiDocumento, Tipo),
    # pero nunca el RUC, la razón social ni la dirección del cliente (un RUC 10 es una persona).
    cuerpo = json.dumps({"request": json.dumps({"Listado": [{
        "CodiDocumento": "01", "Tipo": "F", "RucCliente": "10726778531",
        "NomEmpresaRuc": "ANA MARIA PEREZ ROJAS", "DirEmpresaRuc": "AV LIMA 123", "Dni": "72677853"}]})})
    item = redactar_cuerpo(cuerpo, "application/json")["request"]["<json-en-texto>"]["Listado"][0]
    assert item["CodiDocumento"] == "01" and item["Tipo"] == "F"
    assert item["RucCliente"] == "<texto:11>" and item["NomEmpresaRuc"].startswith("<texto")
    assert _sin_pii(item) and "10726778531" not in json.dumps(item) and "AV LIMA" not in json.dumps(item)


def test_series_del_correlativo_se_ven_y_nada_personal():
    from descubrir_endpoints_venta import correlativo_sin_datos
    datos = {"Valor": {"SerieBoleta": "BP96", "SerieFactura": "FP96", "Correlativo": 6263,
                       "TipoTerminalElectronico": "E", "NomUsuario": "ANA MARIA"}}
    r = correlativo_sin_datos("/pasajes/itinerarios/busca-correlativo", datos)
    assert r["Valor"]["SerieFactura"] == "FP96" and r["Valor"]["SerieBoleta"] == "BP96"
    assert r["Valor"]["Correlativo"] == 6263 and _sin_pii(r)
    assert correlativo_sin_datos("/pasajes/itinerarios/turnos", datos) is None


def test_catalogos_de_tipos_nuevos_tambien_se_guardan():
    from descubrir_endpoints_venta import valores_catalogo
    datos = {"Valor": [{"id": "01", "label": "FACTURA"}, {"id": "03", "label": "BOLETA"}]}
    assert valores_catalogo("/pasajes/base/get-tipoComprobante", datos) == [
        {"id": "01", "label": "FACTURA"}, {"id": "03", "label": "BOLETA"}]


def test_valores_por_validar_se_muestran_y_los_personales_no():
    cuerpo = json.dumps({"request": json.dumps({"Listado": [{
        "Nacionalidad": "PERUANA", "DescuentoTipoDC": "M", "CodiTarjetaCredito": "0204",
        "Nombre": "ANA MARIA PEREZ ROJAS", "Dni": "72677853", "NumeTarjetaCredito": "0000-0000-0000-4111"}]})})
    item = redactar_cuerpo(cuerpo, "application/json")["request"]["<json-en-texto>"]["Listado"][0]
    assert item["Nacionalidad"] == "PERUANA" and item["DescuentoTipoDC"] == "M" and item["CodiTarjetaCredito"] == "0204"
    assert _sin_pii(item) and "4111" not in json.dumps(item)


def test_orden_del_nombre_sin_guardar_nombres():
    from descubrir_endpoints_venta import orden_del_nombre
    pasajeros = [("Ana María", "Pérez", "Rojas")]
    assert orden_del_nombre("ANA MARÍA PÉREZ ROJAS", pasajeros) == "nombres_primero"
    assert orden_del_nombre("PÉREZ ROJAS ANA MARÍA", pasajeros) == "apellidos_primero"
    assert orden_del_nombre("OTRO NOMBRE", pasajeros) == "otro"


# --- Precios, niveles y bloqueos (captura del 03/10) ------------------------------ #
def test_precios_del_turno_sin_ningun_dato_del_pasajero():
    from descubrir_endpoints_venta import precios_y_bloqueos
    turno = {"Estado": True, "Valor": {
        "HoraPartida": "10:50PM", "PlacaBus": "ZBA963", "Precio_N1": 70, "Precio_N2": 60,
        "CantidadMaxBloqAsi": 4, "NombreChofer": "JUAN PEREZ", "CodiChofer": "123",
        "TablaBloqueoAsientos": {"CodiOrigen": 2, "AsientosOcupados": "27,29,31", "CodiDestino": 3,
                                 "AsientosLiberados": ""},
        "ListaResumenProgramacion": {"CAP": "43", "VTT": "0"},
        "ListaPlanoBus": [{"NumeAsiento": 25, "Nivel": 2, "PrecioNormal": 70, "PrecioVenta": 0,
                           "TipoAsiento": "CAMA", "FlagVenta": "", "Nombres": "ANA", "ApellidoPaterno": "PEREZ",
                           "NumeroDocumento": "72677853", "Telefono": "987654321", "Correo": "a@b.pe",
                           "FechaNacimiento": "01/02/1990", "NomUsuario": "CAJERO", "CodiUsuario": 613}],
    }}
    r = precios_y_bloqueos("/pasajes/itinerarios/turnos", turno)
    assert (r["Precio_N1"], r["Precio_N2"], r["CantidadMaxBloqAsi"]) == (70, 60, 4)
    assert r["TablaBloqueoAsientos"]["AsientosOcupados"] == "27,29,31"
    assert r["asientos"] == [{"NumeAsiento": 25, "Nivel": 2, "PrecioNormal": 70, "PrecioVenta": 0,
                              "TipoAsiento": "CAMA", "FlagVenta": ""}]
    texto = json.dumps(r)
    for dato in ("ANA", "PEREZ", "72677853", "987654321", "a@b.pe", "1990", "CAJERO", "613", "JUAN"):
        assert dato not in texto


def test_precios_de_la_lista_de_itinerarios_y_otras_rutas_nada():
    from descubrir_endpoints_venta import precios_y_bloqueos
    lista = {"Valor": [{"HoraPartida": "10:50PM", "Precio_N1": 70, "Precio_N2": 60, "NomOrigen": "TACNA",
                        "NombreChofer": "JUAN PEREZ"}]}
    assert precios_y_bloqueos("/pasajes/itinerarios/lista-itinerarios-paginado", lista) == \
        [{"HoraPartida": "10:50PM", "Precio_N1": 70, "Precio_N2": 60}]
    assert precios_y_bloqueos("/pasajes/itinerarios/consultar-usuario", {"Valor": {"Nombres": "ANA"}}) is None
