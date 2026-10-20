"""
Fixtures compartidas. REGLA: ningún test habla con JELAF, Yupy ni Meta, ni
abre Chrome (skill verificacion-sin-servicios-reales).

- El .env real NO se carga: se anula load_dotenv antes de importar config.
- Variables sensibles con valores de prueba; WHATSAPP_TOKEN vacío y
  PANELES_ENVIO_REAL=0 para que nada pueda salir a Meta.
- La app se usa sin lifespan (httpx.ASGITransport no lo ejecuta).
- `JelafHttpFalso` imita a JELAF por HTTP CON ESTADO: bloqueos, ventas,
  números de boleto, y fallos a pedido. Así se prueba el camino de escritura.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path

import dotenv
import pytest

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
os.chdir(RAIZ)

dotenv.load_dotenv = lambda *a, **k: False   # nunca el .env real

VARIABLES_PRUEBA = {
    "ENTORNO": "desarrollo",
    "JELAF_USUARIO": "usuario-prueba",
    "JELAF_PASSWORD": "clave-prueba",
    "VENTA_MODO": "real",
    "YUPY_CLIENT_ID": "yupy-id-prueba",
    "YUPY_CLIENT_SECRET": "yupy-secreto-prueba",
    "YUPY_BASE_URL": "https://yupy.invalid",
    "WHATSAPP_TOKEN": "",
    "PHONE_NUMBER_ID": "",
    "META_VERIFY_TOKEN": "verifica-prueba",
    "META_APP_SECRET": "secreto-app-prueba",
    "ENLACE_SECRETO": "enlace-secreto-prueba",
    "PANELES_TOKEN": "clave-panel-prueba",
    "PANELES_CLAVE_CIUDADES": "clave-ciudades-prueba",
    "PANELES_CLAVE_PASARELAS": "clave-pasarelas-prueba",
    "PANELES_ENVIO_REAL": "0",
    "EXIGIR_CONTACTO": "0",
    "URL_FRONTEND": "http://localhost:5500/buscar-viaje.html",
    "PRECIO_DESDE_PLANO": "1",   # los tests del "desde" lo prueban encendido
}
for k in [k for k in os.environ if k.startswith(("JELAF_", "YUPY_", "META_", "PANELES_", "VENTA_"))]:
    del os.environ[k]
os.environ.update(VARIABLES_PRUEBA)

from jelaf_http import ErrorJelaf, ErrorJelafRechazo, parsear_turnos  # noqa: E402
from pagos_yupy import ErrorYupy  # noqa: E402
from paneles import db  # noqa: E402
from vendedor_http import VendedorHttp  # noqa: E402

PRECIO_JELAF = Decimal("120.00")

CAJERO = {"CodiEmpresa": 1, "CodiPuntoVenta": 17, "CodiSucursal": 2, "CodiUsuario": 999,
          "Nombre": "BOT PRUEBA", "Nivel": 1, "NomSucursal": "TACNA", "NomPuntoVenta": "BOLETERIA WEB",
          "Terminal": 22, "FechaLiq": "01/10/2026", "TurnoLiq": 0, "TerminalLiq": 0, "Idcaja": 80000}
EMPRESA = {"id": "1", "label": "EMPRESA DE PRUEBA S.R.L", "Ruc": "20000000001",
           "Direccion": "AV. PRUEBA 123", "Electronico": "S"}
CORRELATIVO = {"TipoTerminalElectronico": "E", "TipoImpresora": "1"}


@pytest.fixture(autouse=True)
def base_temporal(tmp_path, monkeypatch):
    """Cada test con su propia base SQLite vacía (y su registro de bloqueos)."""
    import bloqueos_registro
    monkeypatch.setattr(db, "RUTA_DB", tmp_path / "prueba.db")
    monkeypatch.setattr(bloqueos_registro, "RUTA", tmp_path / "bloqueos_jelaf.db")
    db.init_db()
    yield


@pytest.fixture(autouse=True)
def limitadores_limpios():
    """Los límites de peticiones son globales: se vacían entre tests."""
    import gc

    from limitador import Limitador
    for obj in gc.get_objects():
        if isinstance(obj, Limitador):
            obj._golpes.clear()
    yield


# --------------------------------------------------------------------------- #
# Dobles de JELAF, Yupy y Meta
# --------------------------------------------------------------------------- #
def nombre_registrado(nombre: str) -> str:
    partes = str(nombre or "").split(",")
    return " ".join(p for p in partes if p) if len(partes) == 3 and partes[0] and partes[1] else ""


class JelafHttpFalso:
    """JELAF por HTTP, en memoria. Métodos síncronos (el código los llama con to_thread)."""

    disponible = True

    def __init__(self, asientos: int = 60):
        self._lock = threading.Lock()
        self.cajero = dict(CAJERO)
        self.empresa = dict(EMPRESA)
        self.correlativo = dict(CORRELATIVO)
        self.precio_general = PRECIO_JELAF
        self.precios: dict[str, Decimal] = {}
        self.vendidos: dict[str, str] = {}          # asiento → documento
        self.bloqueos: dict[int, str] = {}          # ids → asiento
        self._ids = 1000
        self._boleto = 0
        self.total_asientos = asientos
        self.max_bloqueos = 0
        self.clientes_grabados: list[dict] = []
        self.ventas_recibidas: list[dict] = []
        self.falla_grabar_venta: str | None = None  # None | "rechazo" | "sin_respuesta" | "emite_y_cae"
        self.falla_grabar_pasajero = False
        self.demora_emision = 0.05
        self.llamadas = {"turno": 0, "bloquear": 0, "liberar": 0, "grabar_venta": 0}
        # Orden de las llamadas, para compararlo con el de la pantalla de JELAF.
        self.secuencia: list[str] = []
        self.respuestas_venta: list[dict] = []
        self.convertidos: list[dict] = []
        self.promociones: list[dict] = []
        self.logs: list[str] = []
        self.falla_convertir = False
        self.falla_consultas = False
        self.nombre_en_respuesta: str | None = None   # None = JELAF devuelve el Nombre que recibió

    @property
    def lista_para_vender(self) -> bool:
        return True

    # Lectura ----------------------------------------------------------- #
    def turno_crudo(self, viaje) -> dict:
        with self._lock:
            self.llamadas["turno"] += 1
            # Como el JELAF real (captura del 03/10): ListaPlanoBus es la grilla
            # entera; un asiento LIBRE viene con NumeAsiento=0 y su número en
            # "Tipo"; solo el vendido trae NumeAsiento. Hay celdas que no son
            # asientos (pasillo "VA", "TV"). PrecioVenta es 0 si no se vendió.
            plano = [{"NumeAsiento": 0, "Nivel": 1, "Tipo": "VA", "PrecioNormal": 0, "PrecioVenta": 0,
                      "Nombres": "", "NumeroDocumento": "", "FlagVenta": ""},
                     {"NumeAsiento": 0, "Nivel": 1, "Tipo": "TV", "PrecioNormal": 0, "PrecioVenta": 0,
                      "Nombres": "", "NumeroDocumento": "", "FlagVenta": ""}]
            for n in range(1, self.total_asientos + 1):
                clave = f"{n:02d}"
                precio = self.precios.get(clave, self.precio_general)
                doc = self.vendidos.get(clave, "")
                plano.append({"NumeAsiento": n if doc else 0, "Tipo": clave, "Nivel": 1 if n <= 40 else 2,
                              "PrecioNormal": int(precio), "PrecioVenta": int(precio) if doc else 0,
                              "Nombres": "PASAJERO" if doc else "", "NumeroDocumento": doc,
                              "FlagVenta": "V" if doc else ""})
            return {"CodiEmpresa": 1, "CodiOrigen": 2, "CodiDestino": 1, "CodiSucursal": 2, "CodiRuta": 1,
                    "CodiServicio": 3, "CodiProgramacion": 56846, "NroViaje": 117,
                    "FechaProgramacion": "01/10/2026", "HoraPartida": "08:30PM", "HoraProgramacion": "08:30PM",
                    "NomOrigen": "TACNA", "NomDestino": "LIMA", "NomServicio": "EMPERADOR", "CodiBus": "0035",
                    "CantidadMaxBloqAsi": self.max_bloqueos,
                    "ListaEmbarques": [{"CodiPuntoVenta": 2, "Lugar": "TACNA", "Hora": "08:30PM"}],
                    "ListaArribos": [{"CodiPuntoVenta": 1, "Lugar": "LIMA", "Hora": ""}],
                    "ListaPlanoBus": plano}

    def consultar_turno(self, viaje):
        return parsear_turnos(self.turno_crudo(viaje))

    def consultar_usuario(self, tipo, doc):
        self.secuencia.append("consultar_usuario")
        return {"EsCorrecto": True, "Valor": {"IdCliente": 555, "NombreCliente": "ANA", "ApellidoPaterno": "PEREZ",
                                               "ApellidoMaterno": "ROJAS", "FechaNacimiento": "01/02/1990",
                                               "Sexo": "F"}}

    def buscar_itinerarios(self, *a):
        return []

    def consultar_empresa(self, ruc: str) -> dict:
        # Forma real (captura del 12/10/2026): Valor = {RucCliente, RazonSocial, Direccion, Telefono}.
        if ruc == "20534857153":
            return {"EsCorrecto": True, "Valor": {"RucCliente": ruc, "RazonSocial": "EMPRESA CLIENTE S.A.C.",
                                                   "Direccion": "AV. LIMA 123", "Telefono": ""}}
        return {"EsCorrecto": False, "Mensaje": "No existe", "Valor": None}

    # Lo que la pantalla de JELAF hace alrededor de la venta (captura del 29/09).
    def consultar_manifiesto(self, sucursal, programacion):
        self.secuencia.append("manifiesto")
        if self.falla_consultas:
            raise ErrorJelaf("JELAF no respondió (manifiesto).")
        return {"EsCorrecto": True, "Valor": ""}

    def consultar_promociones(self, datos: dict) -> None:
        self.secuencia.append("promociones")
        if self.falla_consultas:
            raise ErrorJelaf("JELAF no respondió (promociones).")
        self.promociones.append(dict(datos))

    def acceso_usuario_modulo(self):
        self.secuencia.append("acceso")
        if self.falla_consultas:
            raise ErrorJelaf("JELAF no respondió (acceso).")
        return {"EsCorrecto": True, "Valor": True}

    def convertir_venta_base64(self, valor_venta: dict) -> list:
        self.secuencia.append("convertir")
        if self.falla_convertir:
            raise ErrorJelaf("JELAF no respondió (convertir-venta-to-base64).")
        self.convertidos.append(valor_venta)
        return [{"Original": "UERG", "Copia1": "UERG", "Copia2": "UERG",
                 "IdVenta": v.get("IdVenta")} for v in valor_venta.get("ListaVentasRealizadas", [])]

    def grabar_log(self, mensaje: str) -> None:
        self.secuencia.append("grabar_log")
        self.logs.append(mensaje)

    def mantener_sesion(self):
        return None

    # Escritura --------------------------------------------------------- #
    def bloquear_asiento(self, cuerpo: dict) -> int:
        with self._lock:
            self.llamadas["bloquear"] += 1
            self.secuencia.append("bloquear")
            asiento = f"{int(cuerpo['NumeAsiento']):02d}"
            if asiento in self.bloqueos.values():
                raise ErrorJelafRechazo("JELAF rechazó el bloqueo del asiento: asiento bloqueado")
            self._ids += 1
            self.bloqueos[self._ids] = asiento
            return self._ids

    def liberar_asiento(self, terminal, ids):
        self.liberar_arreglo(terminal, [ids])

    def liberar_arreglo(self, terminal, ids):
        with self._lock:
            self.llamadas["liberar"] += 1
            self.secuencia.append("liberar_arreglo")
            for i in ids:
                self.bloqueos.pop(int(i), None)

    def grabar_pasajeros(self, clientes):
        if self.falla_grabar_pasajero:
            raise ErrorJelafRechazo("JELAF rechazó los datos del pasajero: datos inválidos")
        with self._lock:
            self.secuencia.append("grabar_pasajero")
            self.clientes_grabados.extend(clientes)

    def grabar_venta(self, venta: dict) -> dict:
        time.sleep(self.demora_emision)
        with self._lock:
            self.llamadas["grabar_venta"] += 1
            self.secuencia.append("grabar_venta")
            self.ventas_recibidas.append(venta)
            if self.falla_grabar_venta == "rechazo":
                raise ErrorJelafRechazo("JELAF rechazó la venta: caja cerrada")
            if self.falla_grabar_venta == "sin_respuesta":
                raise ErrorJelaf("JELAF no respondió (ReadTimeout).")
            realizadas = []
            for item in venta["Listado"]:
                asiento = f"{int(item['NumeAsiento']):02d}"
                if asiento in self.vendidos:
                    raise ErrorJelafRechazo(f"JELAF rechazó la venta: asiento {asiento} vendido")
                self.vendidos[asiento] = item["Dni"]
                self._boleto += 1
                realizadas.append({"IdVenta": 2000000 + self._boleto, "NumeAsiento": item["NumeAsiento"],
                                   "BoletoCompleto": f"B001-{self._boleto:08d}",
                                   "LinkPag_FE": f"https://fe.invalid/{self._boleto}",
                                   # Como JELAF real: parte `Nombre` por comas (NOMBRES,PATERNO,MATERNO,
                                   # pantalla de venta) y con eso arma el nombre de la venta; sin las
                                   # comas no puede y lo deja vacío (ventas sin nombre del 04-05/10/2026).
                                   "PasNombreCom": nombre_registrado(item["Nombre"]) if self.nombre_en_respuesta is None
                                   else self.nombre_en_respuesta})
            if self.falla_grabar_venta == "emite_y_cae":
                raise ErrorJelaf("JELAF no respondió (ReadTimeout).")
            respuesta = {"ListaVentasRealizadas": realizadas, "CodiProgramacion": 56846}
            self.respuestas_venta.append(respuesta)
            return respuesta


class YupyFalso:
    configurado = True
    api = "payment_orders"
    sdk_url = "https://pago.invalid/sdk.js"

    def __init__(self):
        self.ordenes: dict[str, dict] = {}
        self.creadas: list[tuple[str, Decimal]] = []
        self._lock = threading.Lock()
        # API /v1/checkouts: cada checkout trae su checkout_token; el estado se
        # consulta con él (browser/status). `simulacion` = la credencial tiene
        # payments:simulate (botón "Simular pago").
        self.tokens: dict[str, str] = {}
        self.consultas: list[tuple[str, str | None]] = []
        self.simulacion = False
        self.cancelados: list[str] = []

    def crear_checkout(self, pedido, monto, comprador, expira):
        with self._lock:
            tx = f"yupy-{len(self.creadas) + 1:04d}"
            self.creadas.append((tx, monto))
            self.ordenes[tx] = {"yupy_transaction_id": tx, "status": "PENDING", "amount": f"{monto:.2f}",
                                "currency": "PEN"}
            self.tokens[tx] = f"cks_prueba_{len(self.creadas):04d}"
        return {"yupy_transaction_id": tx, "checkout_token": self.tokens[tx],
                "checkout_session": {"checkout_url": "https://pago.invalid/x"}}

    def simulacion_habilitada(self, checkout_token):
        return self.simulacion

    def cancelar_checkout(self, checkout_token):
        self.cancelados.append(checkout_token)

    def aprobar(self, tx, monto=None):
        self.ordenes[tx]["status"] = "RECONCILED"
        if monto is not None:
            self.ordenes[tx]["amount"] = monto

    def consultar_orden(self, tx, checkout_token=None, monto_creado=None):
        self.consultas.append((tx, checkout_token))
        if self.api == "checkouts" and checkout_token != self.tokens.get(tx):
            raise ErrorYupy("Sin el checkout_token de esta orden no se puede consultar su estado.")
        return dict(self.ordenes[tx])


class NiubizFalso:
    """Niubiz sin red: guarda qué se autorizó; `rechazar`/`monto_cobrado` simulan casos."""
    configurado = True
    merchant_id = "000000000"

    def __init__(self):
        self.sesiones: list[tuple[Decimal, str]] = []
        self.autorizaciones: list[tuple[str, str, Decimal]] = []
        self.rechazar = False
        self.monto_cobrado = None      # None = cobra lo pedido
        self.sin_respuesta = False
        self.marca = "visa"

    def crear_sesion(self, monto, ip, pedido):
        self.sesiones.append((monto, pedido))
        return f"sesion-{len(self.sesiones)}"

    def autorizar(self, token, pedido, monto, url):
        from pagos_niubiz import ErrorNiubiz
        if self.sin_respuesta:
            raise ErrorNiubiz("Niubiz no respondió al autorizar (Timeout).")
        self.autorizaciones.append((token, pedido, monto))
        if self.rechazar:
            return {"errorCode": 400, "data": {"STATUS": "Reject", "ACTION_CODE": "190",
                                               "ACTION_DESCRIPTION": "Denegada", "CARD": "455170******8059"}}
        cobrado = self.monto_cobrado if self.monto_cobrado is not None else monto
        return {"order": {"purchaseNumber": pedido, "amount": float(cobrado), "currency": "PEN",
                          "transactionId": f"tx-{pedido}"},
                "dataMap": {"STATUS": "Authorized", "ACTION_CODE": "000", "TRACE_NUMBER": "123",
                            "CARD": "455170******8059", "BRAND": self.marca}}


@pytest.fixture
def entorno_venta(monkeypatch, tmp_path):
    """api_ventas con JELAF HTTP falso (modo REAL contra el doble), Yupy falso y Meta interceptado."""
    import api_ventas
    import vendedor_http

    jelaf = JelafHttpFalso()
    vendedor = VendedorHttp(jelaf, capacidad=10, simulacion=False)
    yupy = YupyFalso()
    niubiz = NiubizFalso()
    enviados: list[str] = []

    monkeypatch.setattr(api_ventas, "pool_jelaf", vendedor)
    monkeypatch.setattr(api_ventas, "jelaf", jelaf)
    monkeypatch.setattr(api_ventas, "yupy", yupy)
    monkeypatch.setattr(api_ventas, "niubiz", niubiz)
    # El envío real queda a mano para probar qué pasa cuando Meta falla.
    enviar_boton_compra_real = api_ventas.enviar_boton_compra
    monkeypatch.setattr(api_ventas, "enviar_boton_compra", lambda n, *a, **k: enviados.append(n))
    correos_enviados: list = []
    # Resultados a devolver en orden (vacía = siempre bien), p. ej. una dirección rechazada.
    resultados_correo: list = []

    def correo_falso(mensaje, cfg):
        correos_enviados.append(mensaje)
        return resultados_correo.pop(0) if resultados_correo else api_ventas.correo.Resultado(True)
    # El correo con los boletos sale por SMTP REAL: nunca en tests.
    monkeypatch.setattr(api_ventas.correo, "enviar", correo_falso)
    monkeypatch.setattr(vendedor_http, "SALIDA_SIMULACION", tmp_path / "simulacion")
    api_ventas._emisiones.clear()
    api_ventas._ultima_conciliacion.clear()
    api_ventas._correos_enviados.clear()
    api_ventas._correos_en_curso.clear()
    api_ventas._esperar_desde(3)          # que el hilo de precios no arrastre nada del test anterior
    api_ventas._cache_desde.clear()
    api_ventas._fallos_desde.clear()

    class Entorno:
        def venta(self, booking):
            return vendedor.worker_de(booking)

    e = Entorno()
    e.api, e.vendedor, e.jelaf, e.yupy, e.enviados = api_ventas, vendedor, jelaf, yupy, enviados
    e.niubiz, e.enviar_boton_compra_real = niubiz, enviar_boton_compra_real
    e.correos_enviados = correos_enviados
    e.resultados_correo = resultados_correo
    return e


@pytest.fixture
def cliente(entorno_venta):
    import httpx

    transporte = httpx.ASGITransport(app=entorno_venta.api.app)
    return httpx.AsyncClient(transport=transporte, base_url="http://test")
