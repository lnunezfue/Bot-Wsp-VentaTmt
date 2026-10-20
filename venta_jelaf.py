"""
venta_jelaf.py — arma los cuerpos de las llamadas de venta de JELAF y lee sus
respuestas. Funciones PURAS: sin red, sin estado. Se prueban en
tests/test_venta_jelaf.py contra la forma exacta capturada de una venta real
(docs/descubrimiento-venta-http.md).

Valores POR VALIDAR: las capturas tapan los textos (podían ser datos
personales), así que unos pocos valores fijos no se vieron. Están todos en
POR_VALIDAR, abajo. En modo simulación (VENTA_MODO=simulacion) el backend
guarda el cuerpo que habría enviado, para compararlo con una venta hecha por
boletería antes de pasar a modo real.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

# --------------------------------------------------------------------------- #
# Catálogos de JELAF (capturados el 29/09/2026)
# --------------------------------------------------------------------------- #
TIPO_PAGO_CONTADO = "01"
TIPO_PAGO_TARJETA = "03"
TARJETAS = {"visa": "0201", "mastercard": "0202", "deposito": "0203",
            "yape": "0204", "plin": "0205", "bbva": "0206"}

# Tipos de documento: la mini-webapp usa sus propios códigos; JELAF usa otros
# (get-tiposDoc: 00 CEDULA, 01 DNI, 04 C.EXTRANJERIA, 07 PASAP., 09 CEDULA).
DOC_WEB_A_JELAF = {"01": "01", "03": "04", "02": "07"}
# (04 = RUC en la web es para factura, no es documento de un pasajero)

# Perú en lista-paises-codigo-telefono (id 179; largos coinciden con la captura).
PAIS_PERU = {"id": 179, "nombre_es": "Perú", "nombre_en": "Peru", "nombre_fr": "Pérou",
             "iso2": "PE", "iso3": "PER", "phone_code": "51"}

MASCARA_TARJETA = "0000-0000-0000-"   # 15 caracteres: lo que JELAF manda en una venta al contado

POR_VALIDAR = {
    # 7 caracteres en la captura; ¿"PERUANA" o "PERUANO"?
    "nacionalidad": "PERUANA",
    # 1 carácter en la captura; valor exacto desconocido.
    "descuento_tipo": "0",
    # Canal observado en la venta de boletería. ¿Hay uno para venta web?
    "canal_venta": 3,
    # (El formato de "Nombre" ya está confirmado: ver nombre_para_jelaf.)
}


class DatosIncompletos(ValueError):
    """Falta algo imprescindible para armar la venta (no se envía nada a JELAF)."""


# --------------------------------------------------------------------------- #
# Tipos simples
# --------------------------------------------------------------------------- #
@dataclass
class Pasajero:
    documento: str
    tipo_documento_web: str       # código de la mini-webapp
    nombres: str
    paterno: str
    materno: str
    fecha_nacimiento: str         # DD/MM/YYYY
    sexo: str                     # "M" | "F"
    telefono: str                 # 9 dígitos o ""
    id_cliente: int = 0           # de consultar-usuario; 0 si es cliente nuevo


@dataclass
class AsientoVenta:
    numero: str                   # "05"
    precio: Decimal               # precio de JELAF (PrecioVenta)
    precio_normal: Decimal
    nivel: int
    ids_bloqueo: int | None = None
    pasajero: Pasajero | None = None


@dataclass(frozen=True)
class Facturacion:
    """Datos de la factura (una por asiento, todas al mismo RUC). Sin esto, boleta."""
    ruc: str                      # 11 dígitos, validado con ruc_valido
    razon_social: str
    direccion: str = ""


@dataclass
class DatosPago:
    tipo_pago: str = TIPO_PAGO_TARJETA
    codi_tarjeta: str = TARJETAS["visa"]
    ultimos4: str = "0000"
    extra: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Conversión de datos
# --------------------------------------------------------------------------- #
def tipo_doc_jelaf(tipo_web: str) -> str:
    try:
        return DOC_WEB_A_JELAF[tipo_web]
    except KeyError as e:
        raise DatosIncompletos(f"Tipo de documento {tipo_web!r} no admitido para un pasajero.") from e


_PREFIJOS_RUC = ("10", "15", "16", "17", "20")
_PESOS_RUC = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)


def ruc_valido(ruc: str) -> bool:
    """11 dígitos, prefijo de SUNAT y dígito verificador (módulo 11): ataja RUCs mal tipeados."""
    if not re.fullmatch(r"\d{11}", ruc or "") or ruc[:2] not in _PREFIJOS_RUC:
        return False
    resto = sum(int(d) * p for d, p in zip(ruc[:10], _PESOS_RUC)) % 11
    return int(ruc[10]) == (11 - resto) % 10


def edad(fecha_nacimiento_ddmmyyyy: str, hoy: date) -> int:
    d, m, a = (int(x) for x in fecha_nacimiento_ddmmyyyy.split("/"))
    return hoy.year - a - ((hoy.month, hoy.day) < (m, d))


def nombre_para_jelaf(p: Pasajero) -> str:
    """
    El `Nombre` de grabar-venta, como lo arma la pantalla de venta de JELAF
    (código descargado el 05/10/2026, descargar_codigo_pantalla_jelaf.py):
        input.Nombre = (objVenta.Nombres + ',' + objVenta.ApellidoPaterno + ',' + objVenta.ApellidoMaterno);
    JELAF lo parte por las comas para guardar nombres y apellidos de la venta y
    armar el comprobante. Con espacios (lo que mandaba el bot) no puede, y la
    venta quedaba SIN nombre (BP58-00008349/50/51/52/59, anuladas). Sin materno
    queda la coma final, igual que en la pantalla.
    """
    def limpio(x: str) -> str:
        return " ".join(str(x or "").split()).upper()
    return f"{limpio(p.nombres)},{limpio(p.paterno)},{limpio(p.materno)}"


def nombre_completo(p: Pasajero, orden: str = "nombres_primero") -> str:
    """Nombre legible (PDF, mensajes). Para JELAF usar nombre_para_jelaf."""
    partes = [p.nombres, p.paterno, p.materno] if orden == "nombres_primero" else [p.paterno, p.materno, p.nombres]
    return " ".join(x.strip().upper() for x in partes if x and x.strip())


def numero_tarjeta(ultimos4: str) -> str:
    """'0000-0000-0000-1234'. Solo cuentan los 4 últimos dígitos (tarjeta u operación Yape)."""
    digitos = re.sub(r"\D", "", ultimos4 or "")[-4:].rjust(4, "0")
    return MASCARA_TARJETA + digitos


def numero_asiento_jelaf(numero: str) -> str:
    """
    Asiento en grabar-venta: "55" en la captura de una venta. POR VALIDAR con
    asientos < 10 (en bloquearAsiento JELAF manda "01", ver numero_asiento_bloqueo).
    """
    return str(int(numero))


def numero_asiento_bloqueo(numero: str) -> str:
    """bloquearAsiento: siempre dos dígitos ("01"), confirmado en la captura del 03/10."""
    return f"{int(numero):02d}"


def _dinero(valor: Decimal) -> str:
    return f"{Decimal(valor):.2f}"


def asiento_en_plano(turno: dict, numero: str) -> dict | None:
    """
    Celda del asiento en ListaPlanoBus. El asiento libre viene con
    NumeAsiento=0 y su número en "Tipo" (captura del 03/10): ver
    jelaf_http.numero_de_celda. Antes se buscaba solo por NumeAsiento y todo
    asiento libre salía como "no existe en este bus".
    """
    from jelaf_http import numero_de_celda   # sin import circular: jelaf_http no importa este módulo
    objetivo = f"{int(numero):02d}"
    for a in turno.get("ListaPlanoBus") or []:
        if isinstance(a, dict) and numero_de_celda(a) == objetivo:
            return a
    return None


def fecha_programacion(turno: dict, fecha_viaje_jelaf: str) -> str:
    valor = str(turno.get("FechaProgramacion") or "")[:10]
    return valor if re.fullmatch(r"\d{2}/\d{2}/\d{4}", valor) else fecha_viaje_jelaf


# --------------------------------------------------------------------------- #
# Cuerpos de las llamadas
# --------------------------------------------------------------------------- #
def cuerpo_bloqueo(turno: dict, fecha_viaje_jelaf: str, numero: str) -> dict:
    return {
        "CodiProgramacion": int(turno.get("CodiProgramacion") or 0),
        "NroViaje": int(turno.get("NroViaje") or 0),
        "CodiOrigen": str(turno.get("CodiOrigen")),
        "CodiDestino": str(turno.get("CodiDestino")),
        "NumeAsiento": numero_asiento_bloqueo(numero),
        "FechaProgramacion": fecha_programacion(turno, fecha_viaje_jelaf),
        "Precio": 0,
    }


def cliente_para_jelaf(p: Pasajero, hoy: date, factura: Facturacion | None = None) -> dict:
    """
    Un elemento de grabar-pasajero (misma forma que la captura). Con factura,
    la ficha lleva el RUC y la razón social, como la grabó boletería en la
    captura del 12/10/2026.
    """
    return {
        "IdCliente": int(p.id_cliente or 0),
        "TipoDoc": tipo_doc_jelaf(p.tipo_documento_web),
        "NumeroDoc": p.documento,
        "NombreCliente": p.nombres.upper(),
        "ApellidoPaterno": p.paterno.upper(),
        "ApellidoMaterno": (p.materno or "").upper(),
        "FechaNacimiento": p.fecha_nacimiento,
        "Edad": str(edad(p.fecha_nacimiento, hoy)),
        "Telefono": p.telefono or "",
        "Sexo": p.sexo,
        "RucContacto": factura.ruc if factura else "",
        "RazonSocial": factura.razon_social.upper() if factura else "",
        "Direccion": factura.direccion.upper() if factura else "",
        "Correo": "",
        "ObjCodTelefono": dict(PAIS_PERU),
    }


def _primero(lista) -> dict:
    return lista[0] if isinstance(lista, list) and lista and isinstance(lista[0], dict) else {}


def item_venta(*, turno: dict, cajero: dict, empresa: dict, correlativo: dict,
               asiento: AsientoVenta, pago: DatosPago, fecha_viaje_jelaf: str, hoy: date,
               factura: Facturacion | None = None) -> dict:
    """
    Un elemento de `Listado` en grabar-venta (≈90 campos, en el orden capturado).
    Factura: como la pantalla de JELAF (codigo_jelaf/itinerarios.html), solo
    cambian RucCliente, NomEmpresaRuc y DirEmpresaRuc; CodiDocumento y Tipo van
    vacíos igual que en la boleta y JELAF usa la serie FP… por el RUC.
    """
    p = asiento.pasajero
    if p is None:
        raise DatosIncompletos(f"El asiento {asiento.numero} no tiene pasajero.")
    embarque = _primero(turno.get("ListaEmbarques"))
    arribo = _primero(turno.get("ListaArribos"))
    if not embarque or not arribo:
        raise DatosIncompletos("El turno no trae embarque/arribo.")
    contado = pago.tipo_pago == TIPO_PAGO_CONTADO
    return {
        "CodiEmpresa": str(turno.get("CodiEmpresa")),
        "CodiOficina": cajero["CodiSucursal"],
        "CodiPuntoVenta": cajero["CodiPuntoVenta"],
        "CodiOrigen": str(turno.get("CodiOrigen")),
        "CodiDestino": str(turno.get("CodiDestino")),
        "CodiProgramacion": int(turno.get("CodiProgramacion") or 0),
        "RucCliente": factura.ruc if factura else "",
        "NumeAsiento": numero_asiento_jelaf(asiento.numero),
        "FlagVenta": "V",
        "PrecioVenta": _dinero(asiento.precio),
        "Nombre": nombre_para_jelaf(p),
        "Edad": str(edad(p.fecha_nacimiento, hoy)),
        "Telefono": p.telefono or "",
        "CodiUsuario": cajero["CodiUsuario"],
        "Dni": p.documento,
        "NomUsuario": cajero.get("Nombre") or "",
        "TipoDocumento": tipo_doc_jelaf(p.tipo_documento_web),
        "CodiDocumento": "",
        "Tipo": "",
        "Sexo": p.sexo,
        "TipoPago": pago.tipo_pago,
        "FechaViaje": fecha_viaje_jelaf,
        "HoraViaje": turno.get("HoraProgramacion") or turno.get("HoraPartida"),
        "Nacionalidad": POR_VALIDAR["nacionalidad"],
        "CodiServicio": str(turno.get("CodiServicio")),
        "CodiEmbarque": embarque.get("CodiPuntoVenta"),
        "CodiArribo": arribo.get("CodiPuntoVenta"),
        "HoraEmbarque": embarque.get("Hora"),
        "NivelAsiento": asiento.nivel,
        "CodiTerminal": f"{int(cajero['Terminal']):03d}",
        "NomOficina": cajero.get("NomSucursal") or "",
        "NomPuntoVenta": cajero.get("NomPuntoVenta") or "",
        "NomDestino": turno.get("NomDestino") or "",
        "NomEmpresaRuc": factura.razon_social.upper() if factura else "",
        "DirEmpresaRuc": factura.direccion.upper() if factura else "",
        "NomServicio": turno.get("NomServicio") or "",
        "NomOrigen": turno.get("NomOrigen") or "",
        "NroViaje": int(turno.get("NroViaje") or 0),
        "FechaProgramacion": fecha_programacion(turno, fecha_viaje_jelaf),
        "HoraProgramacion": turno.get("HoraProgramacion") or turno.get("HoraPartida"),
        "CodiBus": turno.get("CodiBus"),
        "CodiSucursal": str(turno.get("CodiSucursal")),
        "CodiRuta": str(turno.get("CodiRuta")),
        "CodiTarjetaCredito": "" if contado else pago.codi_tarjeta,
        "NumeTarjetaCredito": MASCARA_TARJETA if contado else numero_tarjeta(pago.ultimos4),
        "NumeOperacionTarj": "",
        "SerieNC": "",
        "NumeroNC": "",
        "CodiZona": "",
        "Direccion": "",
        "Observacion": "",
        "DepositoCuenta": {},
        "DirEmbarque": embarque.get("Lugar") or "",
        "PrecioNormal": _dinero(asiento.precio_normal),
        "PrecioMinimo": "0.00",
        "ValidadorDescuento": False,
        "ObservacionDescuento": "",
        "ValidadorDescuentoControl": False,
        "DescuentoTipoDC": POR_VALIDAR["descuento_tipo"],
        "ImporteDescuentoDC": "0.00",
        "ImporteDescontadoDC": "0.00",
        "AutorizadoDC": "",
        "ListObjAcompaniante": [],
        "IngresoManualPasajes": False,
        "EstadoAsiento": "N",
        "NomEmpresa": empresa.get("label") or "",
        "RucEmpresa": empresa.get("Ruc") or "",
        "DireccionEmpresa": empresa.get("Direccion") or "",
        "ElectronicoEmpresa": empresa.get("Electronico") or "",
        "TipoTerminalElectronico": correlativo.get("TipoTerminalElectronico") or "",
        "TipoImpresora": str(correlativo.get("TipoImpresora") or ""),
        "CodiComida": "",
        "DirArribo": arribo.get("Lugar") or "",
        "Concepto": "",
        "FechaAbierta": False,
        "Fecha_liq": cajero.get("FechaLiq") or "",
        "TurnoLiq": cajero.get("TurnoLiq", 0),
        "TerminalLiq": cajero.get("TerminalLiq", 0),
        "AsientoPromo": None,
        "SwClienteNuevo": "",
        "Correo": "",
        "TipoVenta_": "",
        "NroVale": "",
        "Marcado": False,
        "Id_CCaja": cajero["Idcaja"],
        "EsClienteFrecuente": False,
        "CodAutorizadoDC": 0,
        "ObservacionDC": "",
    }


def cuerpo_comprobante(valor_venta: dict, cajero: dict) -> dict:
    """
    convertir-venta-to-base64 como lo arma la pantalla de venta (captura del
    29/09): cada elemento de ListaVentasRealizadas de grabar-venta, más los datos
    del usuario que JELAF devuelve vacíos y cuatro opciones del PDF; arriba,
    TipoImpresion/CodiRutaConcatenado/TurnoLiq/TerminalLiq (sin CodiProgramacion).
    """
    terminal = cajero.get("Terminal")
    items = []
    for v in valor_venta.get("ListaVentasRealizadas") or []:
        if not isinstance(v, dict):
            continue
        items.append({
            **v,
            "UsuarioNombre": v.get("UsuarioNombre") or cajero.get("Nombre") or "",
            "UsuarioNomOficina": v.get("UsuarioNomOficina") or cajero.get("NomSucursal") or "",
            "UsuarioCodTerminal": v.get("UsuarioCodTerminal")
            or (f"{int(terminal):03d}" if terminal not in (None, "") else ""),
            "UsuarioNomPVenta": v.get("UsuarioNomPVenta") or cajero.get("NomPuntoVenta") or "",
            "verDireccionEmbarque_EnCuadrePDF": False,
            "verDireccionDesembarque_EnCuadrePDF": False,
            "verPaseDeEmbarque": False,
            "ocultarQRTerminosYCondiciones": False,
        })
    return {"ListaVentasRealizadas": items, "TipoImpresion": "Impresion", "CodiRutaConcatenado": "",
            "TurnoLiq": cajero.get("TurnoLiq", 0), "TerminalLiq": cajero.get("TerminalLiq", 0)}


def cuerpo_venta(*, turno: dict, cajero: dict, empresa: dict, correlativo: dict,
                 asientos: list[AsientoVenta], pago: DatosPago, fecha_viaje_jelaf: str, hoy: date,
                 factura: Facturacion | None = None) -> dict:
    """El JSON completo de grabar-venta (va como texto dentro de {"request": ...})."""
    faltan = [k for k in ("CodiSucursal", "CodiPuntoVenta", "CodiUsuario", "Terminal", "Idcaja", "CodiEmpresa")
              if (cajero or {}).get(k) in (None, "")]
    if faltan:
        raise DatosIncompletos(f"Faltan datos del cajero de JELAF: {', '.join(faltan)}.")
    if not empresa:
        raise DatosIncompletos("Faltan los datos de la empresa (get-empresas).")
    if not asientos:
        raise DatosIncompletos("La venta no tiene asientos.")
    embarques = [{"CodiPuntoVenta": e.get("CodiPuntoVenta"), "Lugar": e.get("Lugar"), "Hora": e.get("Hora")}
                 for e in (turno.get("ListaEmbarques") or []) if isinstance(e, dict)]
    return {
        "Listado": [item_venta(turno=turno, cajero=cajero, empresa=empresa, correlativo=correlativo,
                               asiento=a, pago=pago, fecha_viaje_jelaf=fecha_viaje_jelaf, hoy=hoy,
                               factura=factura)
                    for a in asientos],
        "FlagVenta": "V",
        "CodiRutaConcatenado": "",
        "MacAddress": None,
        "ListaEmbarques": embarques,
        "UsrCodiEmpresa": cajero["CodiEmpresa"],
        "UsrCodiSucursal": cajero["CodiSucursal"],
        "UsrCodiPuntoVenta": cajero["CodiPuntoVenta"],
        "id_puerta": 0,
        "TurnoLiq": cajero.get("TurnoLiq", 0),
        "TerminalLiq": cajero.get("TerminalLiq", 0),
        "JsonPinpad": "",
        "UsrTerminal": cajero["Terminal"],
        "UsrCod": cajero["CodiUsuario"],
        "CanalVenta": POR_VALIDAR["canal_venta"],
    }


# --------------------------------------------------------------------------- #
# Respuesta de grabar-venta
# --------------------------------------------------------------------------- #
def boletos_de_respuesta(valor: dict, asientos: list[str]) -> tuple[dict[str, dict], str | None]:
    """
    {asiento: {nro_boleto, link_comprobante, id_venta}} a partir de
    Valor.ListaVentasRealizadas (uno por asiento, con su NumeAsiento). Si algún
    asiento no aparece, se devuelve la anomalía para conciliar: nunca se inventa.
    """
    por_asiento: dict[str, dict] = {}
    for v in (valor or {}).get("ListaVentasRealizadas") or []:
        if not isinstance(v, dict):
            continue
        try:
            clave = f"{int(v.get('NumeAsiento')):02d}"
        except (TypeError, ValueError):
            continue
        link = v.get("LinkPag_FE") or None
        por_asiento[clave] = {
            "nro_boleto": v.get("BoletoCompleto") or None,
            "link_comprobante": link if isinstance(link, str) and link.startswith("http") else None,
            "id_venta": v.get("IdVenta"),
        }
    faltan = [a for a in asientos if a not in por_asiento]
    anomalia = f"JELAF no devolvió boleto para: {', '.join(faltan)}." if faltan else None
    return {a: por_asiento.get(a, {"nro_boleto": None, "link_comprobante": None, "id_venta": None})
            for a in asientos}, anomalia
