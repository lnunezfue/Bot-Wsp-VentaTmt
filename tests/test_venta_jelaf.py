"""
Los cuerpos que arma el bot deben tener la MISMA forma que la venta real
capturada a boletería el 29/09/2026 (docs/descubrimiento-venta-http.md).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from tests.conftest import CAJERO, CORRELATIVO, EMPRESA, JelafHttpFalso
from venta_jelaf import (
    AsientoVenta,
    DatosIncompletos,
    DatosPago,
    Facturacion,
    Pasajero,
    boletos_de_respuesta,
    cliente_para_jelaf,
    cuerpo_bloqueo,
    cuerpo_venta,
    edad,
    nombre_completo,
    nombre_para_jelaf,
    numero_tarjeta,
    ruc_valido,
    tipo_doc_jelaf,
)

# Claves de un elemento de "Listado" en grabar-venta, en el orden capturado.
CLAVES_ITEM_CAPTURA = [
    "CodiEmpresa", "CodiOficina", "CodiPuntoVenta", "CodiOrigen", "CodiDestino", "CodiProgramacion",
    "RucCliente", "NumeAsiento", "FlagVenta", "PrecioVenta", "Nombre", "Edad", "Telefono", "CodiUsuario",
    "Dni", "NomUsuario", "TipoDocumento", "CodiDocumento", "Tipo", "Sexo", "TipoPago", "FechaViaje",
    "HoraViaje", "Nacionalidad", "CodiServicio", "CodiEmbarque", "CodiArribo", "HoraEmbarque",
    "NivelAsiento", "CodiTerminal", "NomOficina", "NomPuntoVenta", "NomDestino", "NomEmpresaRuc",
    "DirEmpresaRuc", "NomServicio", "NomOrigen", "NroViaje", "FechaProgramacion", "HoraProgramacion",
    "CodiBus", "CodiSucursal", "CodiRuta", "CodiTarjetaCredito", "NumeTarjetaCredito", "NumeOperacionTarj",
    "SerieNC", "NumeroNC", "CodiZona", "Direccion", "Observacion", "DepositoCuenta", "DirEmbarque",
    "PrecioNormal", "PrecioMinimo", "ValidadorDescuento", "ObservacionDescuento", "ValidadorDescuentoControl",
    "DescuentoTipoDC", "ImporteDescuentoDC", "ImporteDescontadoDC", "AutorizadoDC", "ListObjAcompaniante",
    "IngresoManualPasajes", "EstadoAsiento", "NomEmpresa", "RucEmpresa", "DireccionEmpresa",
    "ElectronicoEmpresa", "TipoTerminalElectronico", "TipoImpresora", "CodiComida", "DirArribo", "Concepto",
    "FechaAbierta", "Fecha_liq", "TurnoLiq", "TerminalLiq", "AsientoPromo", "SwClienteNuevo", "Correo",
    "TipoVenta_", "NroVale", "Marcado", "Id_CCaja", "EsClienteFrecuente", "CodAutorizadoDC", "ObservacionDC",
]
CLAVES_CABECERA_CAPTURA = [
    "Listado", "FlagVenta", "CodiRutaConcatenado", "MacAddress", "ListaEmbarques", "UsrCodiEmpresa",
    "UsrCodiSucursal", "UsrCodiPuntoVenta", "id_puerta", "TurnoLiq", "TerminalLiq", "JsonPinpad",
    "UsrTerminal", "UsrCod", "CanalVenta",
]
CLAVES_CLIENTE_CAPTURA = [
    "IdCliente", "TipoDoc", "NumeroDoc", "NombreCliente", "ApellidoPaterno", "ApellidoMaterno",
    "FechaNacimiento", "Edad", "Telefono", "Sexo", "RucContacto", "RazonSocial", "Direccion", "Correo",
    "ObjCodTelefono",
]
# Tipos vistos en la captura (str vs int importa: JELAF es .NET y deserializa estricto).
TIPOS_CAPTURA = {
    "CodiEmpresa": str, "CodiOficina": int, "CodiPuntoVenta": int, "CodiOrigen": str, "CodiDestino": str,
    "CodiProgramacion": int, "NumeAsiento": str, "PrecioVenta": str, "CodiEmbarque": int, "CodiArribo": int,
    "NivelAsiento": int, "CodiTerminal": str, "NroViaje": int, "CodiBus": str, "CodiSucursal": str,
    "CodiRuta": str, "DepositoCuenta": dict, "PrecioNormal": str, "ValidadorDescuento": bool,
    "ListObjAcompaniante": list, "Id_CCaja": int, "CodAutorizadoDC": int, "TurnoLiq": int,
}

HOY = date(2026, 9, 29)
PASAJERO = Pasajero(documento="00000001", tipo_documento_web="01", nombres="Ana María", paterno="Pérez",
                    materno="Rojas", fecha_nacimiento="15/10/1990", sexo="F", telefono="987654321", id_cliente=555)


def _turno():
    return JelafHttpFalso().turno_crudo(None)


def _venta(asientos=("55",), pago=None):
    return cuerpo_venta(
        turno=_turno(), cajero=CAJERO, empresa=EMPRESA, correlativo=CORRELATIVO,
        asientos=[AsientoVenta(numero=a, precio=Decimal("130"), precio_normal=Decimal("130"), nivel=1,
                               ids_bloqueo=1, pasajero=PASAJERO) for a in asientos],
        pago=pago or DatosPago(), fecha_viaje_jelaf="04/10/2026", hoy=HOY,
    )


def test_item_de_venta_tiene_las_claves_de_la_captura_en_orden():
    assert list(_venta()["Listado"][0].keys()) == CLAVES_ITEM_CAPTURA


def test_cabecera_de_venta_tiene_las_claves_de_la_captura_en_orden():
    assert list(_venta().keys()) == CLAVES_CABECERA_CAPTURA


def test_tipos_iguales_a_la_captura():
    item = _venta()["Listado"][0]
    for clave, tipo in TIPOS_CAPTURA.items():
        assert isinstance(item[clave], tipo), (clave, type(item[clave]))


def test_valores_clave_de_la_venta():
    venta = _venta(("55", "56"))
    item = venta["Listado"][0]
    assert item["NumeAsiento"] == "55" and venta["Listado"][1]["NumeAsiento"] == "56"
    assert item["PrecioVenta"] == "130.00" and item["PrecioMinimo"] == "0.00"
    assert item["CodiTerminal"] == "022" and item["Id_CCaja"] == 80000
    assert item["CodiEmbarque"] == 2 and item["CodiArribo"] == 1 and item["DirEmbarque"] == "TACNA"
    assert item["Edad"] == "35" and item["Dni"] == "00000001" and item["TipoDocumento"] == "01"
    assert venta["UsrTerminal"] == 22 and venta["UsrCod"] == 999
    assert venta["ListaEmbarques"] == [{"CodiPuntoVenta": 2, "Lugar": "TACNA", "Hora": "08:30PM"}]


def test_pago_con_tarjeta_y_al_contado():
    tarjeta = _venta(pago=DatosPago(tipo_pago="03", codi_tarjeta="0204", ultimos4="98765"))["Listado"][0]
    assert tarjeta["TipoPago"] == "03" and tarjeta["CodiTarjetaCredito"] == "0204"
    assert tarjeta["NumeTarjetaCredito"] == "0000-0000-0000-8765"          # 19 caracteres, como la captura
    contado = _venta(pago=DatosPago(tipo_pago="01"))["Listado"][0]
    assert contado["CodiTarjetaCredito"] == "" and contado["NumeTarjetaCredito"] == "0000-0000-0000-"


FACTURA = Facturacion(ruc="20534857153", razon_social="Empresa Cliente S.A.C.", direccion="Av. Lima 123")


def test_ruc_valido_con_digito_verificador_de_sunat():
    assert ruc_valido("20534857153")           # el de la empresa (sale en cada boleto)
    assert ruc_valido("10726778537")           # RUC 10 (persona con negocio): mismo trato
    assert not ruc_valido("20534857154")        # dígito verificador mal
    assert not ruc_valido("30534857153")        # prefijo inexistente
    assert not ruc_valido("2053485715") and not ruc_valido("2053485715A") and not ruc_valido("")


def test_factura_igual_que_la_pantalla_de_jelaf():
    # codigo_jelaf/itinerarios.html (12/10/2026): por asiento RucCliente = Ruc,
    # NomEmpresaRuc = RazonSocial, DirEmpresaRuc = Direccion; CodiDocumento y Tipo
    # siguen vacíos como en la boleta. Con RUC, JELAF usa la serie FP… sola.
    venta = cuerpo_venta(
        turno=_turno(), cajero=CAJERO, empresa=EMPRESA, correlativo=CORRELATIVO,
        asientos=[AsientoVenta(numero=a, precio=Decimal("130"), precio_normal=Decimal("130"), nivel=1,
                               ids_bloqueo=1, pasajero=PASAJERO) for a in ("55", "56")],
        pago=DatosPago(), fecha_viaje_jelaf="04/10/2026", hoy=HOY, factura=FACTURA,
    )
    for item in venta["Listado"]:
        assert list(item.keys()) == CLAVES_ITEM_CAPTURA
        assert item["RucCliente"] == "20534857153"
        assert item["NomEmpresaRuc"] == "EMPRESA CLIENTE S.A.C." and item["DirEmpresaRuc"] == "AV. LIMA 123"
        assert item["CodiDocumento"] == "" and item["Tipo"] == ""
        assert item["Dni"] == "00000001"                     # el pasajero sigue con su DNI
    boleta = _venta()["Listado"][0]
    assert boleta["RucCliente"] == boleta["NomEmpresaRuc"] == boleta["DirEmpresaRuc"] == ""


def test_ficha_del_pasajero_lleva_el_ruc_de_la_factura():
    c = cliente_para_jelaf(PASAJERO, HOY, factura=FACTURA)
    assert list(c.keys()) == CLAVES_CLIENTE_CAPTURA
    assert c["RucContacto"] == "20534857153" and c["RazonSocial"] == "EMPRESA CLIENTE S.A.C."
    assert c["Direccion"] == "AV. LIMA 123" and c["NumeroDoc"] == "00000001"
    sin = cliente_para_jelaf(PASAJERO, HOY)
    assert sin["RucContacto"] == sin["RazonSocial"] == sin["Direccion"] == ""


def test_cliente_de_grabar_pasajero_igual_a_la_captura():
    c = cliente_para_jelaf(PASAJERO, HOY)
    assert list(c.keys()) == CLAVES_CLIENTE_CAPTURA
    assert c["IdCliente"] == 555 and c["NombreCliente"] == "ANA MARÍA" and c["Edad"] == "35"
    assert c["ObjCodTelefono"]["id"] == 179 and c["ObjCodTelefono"]["phone_code"] == "51"


def test_bloqueo_igual_a_la_captura():
    b = cuerpo_bloqueo(_turno(), "01/10/2026", "05")
    assert list(b.keys()) == ["CodiProgramacion", "NroViaje", "CodiOrigen", "CodiDestino", "NumeAsiento",
                              "FechaProgramacion", "Precio"]
    # Captura del 03/10: bloquearAsiento manda el asiento con dos dígitos ("01").
    assert b["CodiOrigen"] == "2" and b["NumeAsiento"] == "05" and b["Precio"] == 0


def test_documentos_se_traducen_a_codigos_de_jelaf():
    assert [tipo_doc_jelaf(t) for t in ("01", "02", "03")] == ["01", "07", "04"]
    with pytest.raises(DatosIncompletos):
        tipo_doc_jelaf("04")                     # RUC no es documento de pasajero


def test_edad_nombre_y_tarjeta():
    assert edad("15/10/1990", HOY) == 35 and edad("29/09/1990", HOY) == 36
    assert nombre_completo(PASAJERO) == "ANA MARÍA PÉREZ ROJAS"
    assert nombre_completo(PASAJERO, "apellidos_primero") == "PÉREZ ROJAS ANA MARÍA"


def test_nombre_para_jelaf_como_la_pantalla_de_venta():
    """
    Código de la pantalla de venta de JELAF (descargado el 05/10/2026):
        input.Nombre = (objVenta.Nombres + ',' + objVenta.ApellidoPaterno + ',' + objVenta.ApellidoMaterno);
    Con espacios en vez de comas, JELAF no puede partirlo y registra la venta sin nombre.
    """
    from dataclasses import replace
    assert nombre_para_jelaf(PASAJERO) == "ANA MARÍA,PÉREZ,ROJAS"
    assert nombre_para_jelaf(replace(PASAJERO, materno="")) == "ANA MARÍA,PÉREZ,"      # como JS con materno vacío
    assert nombre_para_jelaf(replace(PASAJERO, nombres=" ana  maría ", paterno="pérez ")) == "ANA MARÍA,PÉREZ,ROJAS"
    assert numero_tarjeta("") == "0000-0000-0000-0000"


def test_venta_sin_datos_del_cajero_no_se_arma():
    with pytest.raises(DatosIncompletos, match="Idcaja"):
        cuerpo_venta(turno=_turno(), cajero={k: v for k, v in CAJERO.items() if k != "Idcaja"}, empresa=EMPRESA,
                     correlativo=CORRELATIVO, asientos=[], pago=DatosPago(), fecha_viaje_jelaf="x", hoy=HOY)


def test_boletos_por_asiento_y_anomalia_si_falta_uno():
    valor = {"ListaVentasRealizadas": [
        {"NumeAsiento": "56", "BoletoCompleto": "B058-00001", "LinkPag_FE": "https://fe/2", "IdVenta": 2},
        {"NumeAsiento": "5", "BoletoCompleto": "B058-00002", "LinkPag_FE": "javascript:alert(1)", "IdVenta": 3},
    ]}
    por_asiento, anomalia = boletos_de_respuesta(valor, ["05", "56", "57"])
    assert por_asiento["56"]["nro_boleto"] == "B058-00001" and por_asiento["05"]["nro_boleto"] == "B058-00002"
    assert por_asiento["05"]["link_comprobante"] is None           # solo enlaces http(s)
    assert por_asiento["57"]["nro_boleto"] is None and "57" in anomalia
