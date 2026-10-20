"""
jelaf_http.py — todo el diálogo HTTP con JELAF: lectura (itinerarios, plano
con precios, cliente por documento) y VENTA (bloquear/liberar asientos,
grabar pasajeros, grabar-venta). Los formatos salen de capturas reales de
una venta hecha por boletería (docs/descubrimiento-venta-http.md).

Sin navegador: el login también es HTTP (`login_http_jelaf`), validado en vivo
con `prueba_login_http.py` el 04/10/2026. La identidad de la sesión es la cookie
`ASP.NET_SessionId`.

Antes vivía dentro de api_ventas.py como `sesion_global_jelaf`: un
requests.Session con las cookies de un login hecho una sola vez al arrancar.
Si JELAF expiraba esa sesión, buscar viajes / plano / DNI respondían 500
hasta reiniciar el backend (hallazgo C5). Aquí la sesión se renueva sola: si
JELAF contesta algo que no es JSON (su página de login) o 401/403, se vuelve a
iniciar sesión UNA vez y se reintenta.

Importante: estas funciones son síncronas (requests). Se llaman desde
endpoints `def` (FastAPI los corre en un hilo) o con `asyncio.to_thread` desde
endpoints `async` — nunca directo desde el event loop: son bloqueantes y el
re-login dentro de `_pedir` congelaría a todo el backend mientras dura.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

import requests

log = logging.getLogger("tmt.jelaf_http")

URL_BASE = "https://moquegua.2jelaf.net.pe/pasajes"
URL_ITINERARIOS = f"{URL_BASE}/itinerarios"

# Código de cada ciudad en JELAF (CodiOrigen/CodiDestino). Salen de la lista
# `rutas` (sucursales: id y nombre) que trae incrustada la pantalla de venta
# /itinerarios, leída en vivo el 2026-10-07 (get-cuidades NO sirve: son regiones
# NORTE/CENTRO/SUR). Una sede de rutas.py sin código aquí se muestra
# deshabilitada y /buscar-viajes responde que aún no se vende en línea.
CIUDADES_CODIGOS = {
    "LIMA": 1, "TACNA": 2, "AREQUIPA": 3, "MOLLENDO": 4, "ILO": 5, "MOQUEGUA": 6, "ICA": 7, "NASCA": 8,
    "CAMANA": 9, "KM48": 10, "ALTO SIGUAS": 11, "ATICO": 12, "CHALA": 13, "YAUCA": 14, "CAMIARA": 15,
    # Paradas que aparecieron en el barrido de rutas de JELAF (2026-10-07).
    "FISCAL": 48, "CERRO AZUL": 53, "CUSCO": 54, "PUNO": 56, "JULIACA": 58,
}
CIUDADES_POR_CODIGO = {v: k for k, v in CIUDADES_CODIGOS.items()}

TIMEOUT_HTTP = 15

# Cabecera `authorization` que manda la web de JELAF en TODAS sus peticiones,
# antes y después del login: su JS hace "Token " + main_globals.userToken, y
# userToken no se define en ninguna página (comprobado con prueba_login_http.py),
# así que vale literalmente "Token undefined" (los "9 caracteres fijos" de las
# capturas, docs/descubrimiento-venta-http.md). Las lecturas responden sin ella;
# se replica tal cual para que la escritura llegue igual que desde boletería.
AUTORIZACION_JELAF = "Token undefined"
PAUSA_MINIMA_ENTRE_LOGINS = 30  # JELAF empieza a fallar con logins muy seguidos (docs/setup-local.md)


class ErrorJelaf(RuntimeError):
    """JELAF no respondió algo utilizable (caído, sesión inválida, formato inesperado)."""


class ErrorJelafRechazo(ErrorJelaf):
    """JELAF respondió bien pero dijo que no (EsCorrecto=false): la operación NO se hizo."""


def a_decimal(valor) -> Decimal:
    try:
        d = Decimal(str(valor if valor is not None else 0))
    except (InvalidOperation, ValueError):
        return Decimal("0")
    return d.quantize(Decimal("0.01")) if d > 0 else Decimal("0")


_RE_HORA = re.compile(r"(\d{1,2})\s*:\s*(\d{2})(?:\s*:\s*\d{2})?\s*([AaPp])?\.?\s*[Mm]?\.?")


def minutos_del_dia(texto) -> int | None:
    """
    "10:30 PM", "10:30PM", "22:30", "22:30:00", "10:30 p.m." → 1350. None si
    no hay una hora legible. Así se compara la hora aunque JELAF y la web la
    escriban distinto.
    """
    m = _RE_HORA.search(str(texto or ""))
    if not m:
        return None
    horas, minutos, sufijo = int(m.group(1)), int(m.group(2)), (m.group(3) or "").upper()
    if sufijo == "P" and horas < 12:
        horas += 12
    elif sufijo == "A" and horas == 12:
        horas = 0
    if horas > 23 or minutos > 59:
        return None
    return horas * 60 + minutos


def a_fecha_jelaf(fecha: str) -> str:
    """'2026-09-26' → '26/09/2026'. Deja igual lo que ya viene en DD/MM/YYYY."""
    if "-" in fecha:
        anio, mes, dia = fecha.split("-")
        return f"{dia}/{mes}/{anio}"
    return fecha


@dataclass(frozen=True)
class ViajeJelaf:
    """Los códigos que identifican UN turno en JELAF (los devuelve buscar-viajes)."""
    codi_empresa: int
    codi_origen: int
    codi_destino: int
    codi_sucursal: int
    codi_ruta: int
    codi_punto_venta: int
    codi_servicio: int
    fecha_jelaf: str   # DD/MM/YYYY
    hora_viaje: str

    def payload_turnos(self) -> dict:
        return {
            "CodiEmpresa": self.codi_empresa, "CodiOrigen": self.codi_origen,
            "CodiDestino": self.codi_destino, "CodiSucursal": self.codi_sucursal,
            "CodiRuta": self.codi_ruta, "CodiPuntoVenta": self.codi_punto_venta,
            "CodiServicio": self.codi_servicio, "FechaViaje": self.fecha_jelaf,
            "HoraViaje": self.hora_viaje,
        }


@dataclass
class EstadoTurno:
    """Foto del plano real de un turno: qué está ocupado, por quién, y a qué precio."""
    ocupados: set[str] = field(default_factory=set)
    precios: dict[str, Decimal] = field(default_factory=dict)
    documentos: dict[str, str] = field(default_factory=dict)
    precio_general: Decimal = Decimal("0")
    # Xcierre = '1': "La programación se encuentra cerrada" (pantalla de JELAF):
    # boletería no puede vender ese turno, el bot tampoco.
    cerrado: bool = False

    def precio_de(self, asiento: str) -> Decimal:
        """Precio del asiento según JELAF; si ese asiento no trae precio, el del turno."""
        return self.precios.get(asiento) or self.precio_general


def numero_de_celda(celda: dict) -> str | None:
    """
    Número de asiento ("05") de una celda de ListaPlanoBus, o None si la celda
    no es un asiento. Captura real del 03/10/2026: el plano es la grilla
    entera; un asiento LIBRE viene con NumeAsiento=0 y su número en "Tipo"
    ("05"), y solo el vendido trae NumeAsiento. Las celdas que no son
    asientos traen en "Tipo" un código de letras (VA pasillo, TV, LI, EE, CU).
    Antes solo se miraba NumeAsiento y se perdían todos los asientos libres
    con su precio.
    """
    try:
        numero = int(celda.get("NumeAsiento") or 0)
    except (TypeError, ValueError):
        numero = 0
    if numero <= 0:
        tipo = str(celda.get("Tipo") or "").strip()
        if not tipo.isdigit():
            return None
        numero = int(tipo)
    return f"{numero:02d}" if numero > 0 else None


def parsear_turnos(valor: dict | None) -> EstadoTurno:
    """
    Traduce `Valor` de /itinerarios/turnos. Un asiento está ocupado si trae
    nombre, documento o FlagVenta (mismo criterio que usaba api_ventas.py).
    """
    estado = EstadoTurno(cerrado=str((valor or {}).get("Xcierre") or "0").strip() == "1")
    for asiento in (valor or {}).get("ListaPlanoBus") or []:
        clave = numero_de_celda(asiento)
        if clave is None:
            continue
        precio = a_decimal(asiento.get("PrecioVenta") or asiento.get("PrecioNormal"))
        if precio > 0:
            estado.precios[clave] = precio
            if estado.precio_general == 0:
                estado.precio_general = precio
        nombres = str(asiento.get("Nombres") or "").strip()
        doc = str(asiento.get("NumeroDocumento") or "").strip()
        flag = str(asiento.get("FlagVenta") or "").strip()
        if nombres or doc or flag:
            estado.ocupados.add(clave)
            if doc:
                estado.documentos[clave] = doc
    return estado


@dataclass
class SesionJelaf:
    """
    Todo lo que deja el login y hace falta para vender por HTTP:
      - `http`: requests.Session con la cookie de sesión (la identidad del cajero).
      - `cajero`: `Valor` de login/post-usuario (CodiEmpresa, CodiSucursal,
        CodiPuntoVenta, CodiUsuario, Nombre, NomSucursal, NomPuntoVenta,
        Terminal, FechaLiq, TurnoLiq, TerminalLiq, Idcaja…).
    """
    http: requests.Session
    cajero: dict | None


def _elegir_usuario(opciones, usuario: str) -> dict | None:
    """
    La web busca lo tecleado en /base/get-usuarios y el cajero elige una opción
    ({id: código, label: nombre}). Aquí: coincidencia exacta por código o
    nombre; si no la hay, solo se acepta una respuesta única. Con varias y
    ninguna exacta no se adivina: entrar con otro usuario vendería con su caja.
    """
    if not isinstance(opciones, list):
        return None
    validas = [o for o in opciones if isinstance(o, dict) and str(o.get("id") or "").strip()]
    buscado = usuario.strip().upper()
    for o in validas:
        if buscado in (str(o.get("id")).strip().upper(), str(o.get("label") or "").strip().upper()):
            return o
    return validas[0] if len(validas) == 1 else None


def login_http_jelaf(usuario: str, password: str, *, hoy=None,
                     http: requests.Session | None = None) -> SesionJelaf | None:
    """
    Inicia sesión en JELAF por HTTP, con los mismos pasos que hace su página de
    login (validados en vivo con prueba_login_http.py):
      1. GET de la página de login.
      2. GET /base/get-usuarios?value=<usuario>  -> código y nombre del cajero.
      3. POST /login/post-usuario                -> cookie de sesión + datos del cajero.
      4. GET /login/redirect                     -> lo que hace el formulario al terminar.

    Síncrona y bloqueante: llamarla desde un hilo. Nunca escribe la clave ni la
    cookie en logs. Devuelve None si JELAF no responde o rechaza el login.
    """
    if hoy is None:
        from paneles import tiempo
        hoy = tiempo.hoy  # fecha de Lima: la que JELAF considera "hoy" (FechaLiq)
    s = http or requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0",
        "Content-Type": "application/json;charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Authorization": AUTORIZACION_JELAF,
    })
    log.info("Iniciando sesión en JELAF por HTTP...")
    try:
        s.get(f"{URL_BASE}/Autenticacion", timeout=TIMEOUT_HTTP)
        r = s.get(f"{URL_BASE}/base/get-usuarios", params={"value": usuario}, timeout=TIMEOUT_HTTP)
        try:
            datos = r.json()
        except ValueError:
            datos = None
        opcion = _elegir_usuario(datos.get("Valor") if isinstance(datos, dict) else None, usuario)
        if opcion is None:
            log.error("Login en JELAF: el usuario configurado no aparece (o es ambiguo) en get-usuarios.")
            return None

        cuerpo = {
            "Codigo": str(opcion["id"]).strip(),
            "Nombre": str(opcion.get("label") or "").strip(),
            "Clave": password,
            # Sucursal/PuntoVenta "0": la web solo los elige para el usuario raíz;
            # para los demás los decide JELAF y vienen en la respuesta.
            "Sucursal": "0", "PuntoVenta": "0",
            "NomSucursal": "", "NomPuntoVenta": "",
            "Terminal": "", "MacAddress": "",
            "FechaLiq": hoy().strftime("%d/%m/%Y"),  # otra fecha pediría clave de autorización
            "TurnoLiq": 0, "TerminalLiq": 0, "Panel481": False,
        }
        r = s.post(f"{URL_BASE}/login/post-usuario", data=json.dumps(cuerpo), timeout=TIMEOUT_HTTP)
        try:
            datos = r.json()
        except ValueError:
            datos = None
        if not isinstance(datos, dict) or not datos.get("EsCorrecto") or not isinstance(datos.get("Valor"), dict):
            mensaje = str(datos.get("Mensaje") or "")[:120] if isinstance(datos, dict) else f"HTTP {r.status_code}"
            log.error("JELAF rechazó el login: %s", mensaje)
            return None
        if not s.cookies:
            log.error("JELAF aceptó el login pero no entregó cookie de sesión.")
            return None

        s.get(f"{URL_BASE}/login/redirect", timeout=TIMEOUT_HTTP)
    except requests.RequestException as e:
        log.error("JELAF no respondió durante el login: %s", type(e).__name__)
        return None
    log.info("Sesión de JELAF lista (por HTTP, sin navegador).")
    return SesionJelaf(http=s, cajero=datos["Valor"])


class ClienteJelafHttp:
    def __init__(self, usuario: str, password: str):
        self._usuario = usuario
        self._password = password
        self._sesion: SesionJelaf | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()
        self._ultimo_login = 0.0
        self.empresa: dict | None = None        # get-empresas[0]
        self.correlativo: dict | None = None    # busca-correlativo (tipo de terminal/impresora)

    @property
    def usuario(self) -> str:
        """Login de la cuenta (Y1, Y2…): para logs y paneles. La clave nunca sale de aquí."""
        return self._usuario

    @property
    def disponible(self) -> bool:
        return self._sesion is not None

    @property
    def cajero(self) -> dict | None:
        return self._sesion.cajero if self._sesion else None

    @property
    def lista_para_vender(self) -> bool:
        """Para emitir hacen falta los datos del cajero (del login) y los de la empresa."""
        return bool(self._sesion and self._sesion.cajero and self.empresa)

    async def iniciar(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._sesion = await asyncio.to_thread(login_http_jelaf, self._usuario, self._password)
        self._ultimo_login = time.monotonic()
        if self._sesion:
            try:
                await asyncio.to_thread(self.cargar_datos_empresa)
            except ErrorJelaf as e:
                log.error("No se pudieron leer los datos de la empresa en JELAF: %s", e)

    def cargar_datos_empresa(self) -> None:
        """Datos fijos que van en cada venta (razón social, RUC, tipo de terminal)."""
        empresas = self._pedir("GET", "/base/get-empresas").get("Valor") or []
        self.empresa = empresas[0] if empresas and isinstance(empresas[0], dict) else None
        codi_empresa = (self.cajero or {}).get("CodiEmpresa", 1)
        self.correlativo = self._pedir("POST", "/itinerarios/busca-correlativo",
                                       json={"CodiEmpresa": codi_empresa, "FlagVenta": "V"}).get("Valor") or {}

    def _relogin(self, sesion_fallida) -> None:
        """
        Re-login desde el hilo que hizo la petición (el login es HTTP, no
        necesita el event loop). Si otro hilo ya renovó la sesión mientras se
        esperaba el lock, no se repite.
        """
        with self._lock:
            if self._sesion is not sesion_fallida and self._sesion is not None:
                return
            if time.monotonic() - self._ultimo_login < PAUSA_MINIMA_ENTRE_LOGINS:
                raise ErrorJelaf("La sesión de JELAF no responde y se acaba de reintentar el login.")
            self._ultimo_login = time.monotonic()
            nueva = login_http_jelaf(self._usuario, self._password)
            if nueva is None:
                raise ErrorJelaf("No se pudo renovar la sesión de JELAF.")
            self._sesion = nueva
            log.warning("Sesión de JELAF renovada tras expirar.")

    def _verificar_hilo(self) -> None:
        """Llamado desde el loop, una petición bloqueante (y su posible re-login) congelaría el backend."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # hilo sin loop: correcto
        if loop is self._loop:
            raise RuntimeError("ClienteJelafHttp se llamó desde el event loop: usar asyncio.to_thread.")

    def _pedir(self, metodo: str, ruta: str, *, reintentar: bool = True,
               timeout: float = TIMEOUT_HTTP, **kwargs) -> dict:
        """
        `reintentar=False` para operaciones que cambian estado en JELAF y que no
        deben repetirse a ciegas (grabar-venta): si la respuesta es rara, se
        lanza el error y quien llama concilia.
        """
        self._verificar_hilo()
        for intento in (1, 2):
            sesion = self._sesion
            if sesion is None:
                self._relogin(None)
                sesion = self._sesion
            try:
                r = sesion.http.request(metodo, f"{URL_BASE}{ruta}", timeout=timeout, **kwargs)
            except requests.RequestException as e:
                raise ErrorJelaf(f"JELAF no respondió ({type(e).__name__}).") from e
            try:
                if r.status_code in (401, 403):
                    raise ValueError("sesión rechazada")
                datos = r.json()
                if not isinstance(datos, dict):
                    raise ValueError("respuesta no es un objeto")
                return datos
            except ValueError:
                if intento == 1 and reintentar:
                    self._relogin(sesion)
                    continue
                raise ErrorJelaf(f"JELAF devolvió una respuesta inesperada en {ruta} ({r.status_code}).")
        raise ErrorJelaf("Inalcanzable")

    def _exigir_correcto(self, datos: dict, que: str) -> dict:
        if not datos.get("EsCorrecto"):
            raise ErrorJelafRechazo(f"JELAF rechazó {que}: {str(datos.get('Mensaje') or '')[:200]}")
        return datos

    # ------------------------------------------------------------------ #
    # Lectura
    # ------------------------------------------------------------------ #
    def buscar_itinerarios(self, codi_origen: int, fecha_jelaf: str, hora_referencia: str,
                           codi_destino: int = 0) -> list[dict]:
        """
        Turnos que salen de `codi_origen`. Con `codi_destino` (tramos), igual que
        la pantalla de boletería cuando se elige destino (CodiDestino:"1" en la
        cuarta captura); 0 = todos los destinos.
        """
        datos = self._pedir("POST", "/itinerarios/lista-itinerarios", json={
            "CodiOrigen": str(codi_origen), "CodiDestino": str(codi_destino), "CodiRuta": "0",
            "Hora": hora_referencia, "CodiServicio": 0, "NomDestino": "TODOS",
            "FechaViaje": fecha_jelaf, "SoloProgramados": False, "TodosTurnos": True,
        })
        viajes = datos.get("Valor")
        return [v for v in viajes if isinstance(v, dict)] if isinstance(viajes, list) else []

    def turno_crudo(self, viaje: ViajeJelaf) -> dict:
        """`Valor` completo de /itinerarios/turnos (programación, embarques, plano con precios)."""
        datos = self._pedir("POST", "/itinerarios/turnos", json=viaje.payload_turnos())
        valor = datos.get("Valor")
        if not isinstance(valor, dict):
            raise ErrorJelaf("JELAF no devolvió el turno pedido.")
        return valor

    def consultar_turno(self, viaje: ViajeJelaf) -> EstadoTurno:
        return parsear_turnos(self.turno_crudo(viaje))

    def consultar_usuario(self, tipo_documento: str, documento: str) -> dict:
        return self._pedir("GET", "/itinerarios/consultar-usuario",
                           params={"tipoDoc": tipo_documento, "numeroDoc": documento})

    def consultar_empresa(self, ruc: str) -> dict:
        """
        Lo que hace boletería al dar Enter en el RUC (captura del 12/10/2026):
        Valor = {RucCliente, RazonSocial, Direccion, Telefono}. Solo lectura.
        """
        return self._pedir("GET", "/itinerarios/consultar-empresa", params={"rucContacto": ruc})

    def mantener_sesion(self) -> None:
        """Petición liviana para que la sesión no expire por inactividad."""
        self._pedir("GET", "/base/obtener-fecha-sistema")

    # ------------------------------------------------------------------ #
    # Escritura (venta). Formatos tomados de las capturas reales.
    # ------------------------------------------------------------------ #
    def bloquear_asiento(self, cuerpo: dict) -> int:
        datos = self._exigir_correcto(self._pedir("POST", "/itinerarios/bloquearAsiento", json=cuerpo),
                                      "el bloqueo del asiento")
        try:
            return int(datos.get("Valor"))
        except (TypeError, ValueError) as e:
            raise ErrorJelaf("JELAF no devolvió el id del bloqueo.") from e

    def liberar_asiento(self, terminal: int, ids: int) -> None:
        self._exigir_correcto(self._pedir("POST", "/itinerarios/liberarAsiento", json={
            "request": json.dumps({"Terminal": terminal, "IDS": ids})}), "liberar el asiento")

    def liberar_arreglo(self, terminal: int, ids: list[int]) -> None:
        self._exigir_correcto(self._pedir("POST", "/itinerarios/liberarArregloAsientos", json={
            "request": json.dumps({"Terminal": terminal, "arregloIDS": [str(i) for i in ids]})}),
            "liberar los asientos")

    def grabar_pasajeros(self, clientes: list[dict]) -> None:
        self._exigir_correcto(self._pedir("POST", "/itinerarios/grabar-pasajero", json={
            "request": json.dumps(clientes, ensure_ascii=False)}), "los datos del pasajero")

    def grabar_venta(self, venta: dict) -> dict:
        """EMITE los boletos. Sin reintento automático: el llamador concilia si falla."""
        datos = self._pedir("POST", "/itinerarios/grabar-venta", reintentar=False, timeout=45,
                            json={"request": json.dumps(venta, ensure_ascii=False)})
        return self._exigir_correcto(datos, "la venta").get("Valor") or {}

    # ------------------------------------------------------------------ #
    # Lo que la pantalla de venta de JELAF hace alrededor de la venta.
    # Formatos de la captura de boletería del 29/09 (comparar_venta_boleteria.py):
    # sin estas llamadas, dos ventas reales del bot quedaron sin nombre en JELAF.
    # ------------------------------------------------------------------ #
    def consultar_manifiesto(self, sucursal, programacion) -> dict:
        """Antes de bloquear un asiento y antes de vender (solo lectura). Cuerpo JSON plano."""
        return self._pedir("POST", "/itinerarios/consultaManifiestoProgramacion",
                           json={"Suc": str(sucursal), "Prog": int(programacion)})

    def consultar_promociones(self, datos: dict) -> None:
        """
        Las tres consultas que la pantalla hace al cargar el DNI (solo lectura;
        el bot no aplica descuentos). `datos`: Tipo, Numero, Fecha, Origen,
        Destino, Servicio, Empresa, Usuario, Nombre.
        """
        base = {"Tipo": datos["Tipo"], "Numero": datos["Numero"]}
        self._pedir("POST", "/promocion/consultar-usuario-nuevo",
                    json={"request": json.dumps({**base, "Fecha": datos["Fecha"]}, ensure_ascii=False)})
        self._pedir("POST", "/promocion/consultar-usuario-corporativo",
                    json={"request": json.dumps(base, ensure_ascii=False)})
        self._pedir("POST", "/promocion/consultar-usuario-Kilometraje", json={"request": json.dumps({
            "Origen": datos["Origen"], "Destino": datos["Destino"], "Servicio": datos["Servicio"],
            "Empresa": datos["Empresa"], **base, "Fecha": datos["Fecha"], "Usuario": datos["Usuario"],
            "Nombre": datos["Nombre"]}, ensure_ascii=False)})

    def acceso_usuario_modulo(self) -> dict:
        """Al aceptar la venta, antes del manifiesto (solo lectura)."""
        return self._pedir("GET", "/base/get-acceso-usuario-modulo")

    def convertir_venta_base64(self, cuerpo: dict) -> list:
        """
        Después de grabar-venta: JELAF genera el comprobante (Original/Copia1/
        Copia2 en base64). `cuerpo` = venta_jelaf.cuerpo_comprobante(...). Sin
        reintento: la venta ya está hecha.
        """
        datos = self._pedir("POST", "/itinerarios/convertir-venta-to-base64", reintentar=False, timeout=45,
                            json={"request": json.dumps(cuerpo, ensure_ascii=False)})
        valor = self._exigir_correcto(datos, "el comprobante").get("Valor")
        return valor if isinstance(valor, list) else []

    def grabar_log(self, mensaje: str) -> None:
        """Bitácora de JELAF, al final de la venta (como boletería)."""
        self._pedir("POST", "/itinerarios/grabar-log", reintentar=False,
                    json={"request": json.dumps({"Mensaje": mensaje[:200]}, ensure_ascii=False)})
