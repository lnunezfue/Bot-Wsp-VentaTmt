"""
descubrir_endpoints_venta.py — Captura PASIVA de las llamadas internas de JELAF en una venta
=============================================================================================

Objetivo: saber qué peticiones HTTP hace la página de JELAF al seleccionar un
asiento, grabar un pasajero y aceptar el pago (la emisión). Con eso se podría
vender por HTTP directo, sin navegador, y atender varias ventas a la vez
(ver la conversación sobre capacidad de workers y docs/REVISION_2026-09-26.md, R1).

Hoy se conocen: lista-itinerarios, turnos, consultar-usuario, bloquearAsiento,
liberarAsiento y grabar-pasajero. NO se conoce la llamada que emite el boleto.

Qué hace y qué NO hace:
  - Abre un Chrome y se queda MIRANDO. No hace clics, no escribe, no inicia
    sesión, no lee el .env. Una persona de boletería entra con SU usuario y hace
    una venta que igual iba a hacer. El script no crea ventas.
  - Guarda solo la FORMA de cada petición: método, ruta, nombres de campos y
    tipos. Los textos (nombres, DNI, teléfonos, tarjeta, usuario, contraseña)
    se reemplazan por "<texto:N>" ANTES de escribir en disco. Las cabeceras se
    guardan solo por nombre (para saber si JELAF exige un token anti-CSRF),
    nunca su valor. Cookies: nunca.
  - Escribe en descubrimiento_output/ (ignorado por git).

Uso (lo decide y ejecuta el usuario, contra JELAF real):
    python descubrir_endpoints_venta.py
    → se abre Chrome en JELAF. Inicia sesión y haz la venta normal.
    → en esta consola escribe una etiqueta y Enter para marcar cada paso
      ("asiento", "pasajero", "pago", "aceptar"...). Ayuda a leer el resumen.
    → cierra el navegador (o Ctrl+C) al terminar.

Prueba local sin JELAF:
    python descubrir_endpoints_venta.py --url http://127.0.0.1:8765/ --headless --segundos 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

URL_ITINERARIOS = "https://moquegua.2jelaf.net.pe/pasajes/itinerarios"
SALIDA = Path("descubrimiento_output")

IGNORAR_EXT = (".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
               ".woff", ".woff2", ".ttf", ".map")

# Claves cuyo valor NUNCA se guarda, aunque sea número (DNI como número, etc.).
_CLAVE_SENSIBLE = re.compile(
    r"doc|dni|ruc|nombre|apellido|paterno|materno|razon|telef|celular|correo|mail|"
    r"nacim|sexo|direcc|tarjeta|card|cvv|numerotp|clave|pass|contras|usuario|user|"
    r"token|cookie|auth|firma|session|cliente|^numero",
    re.IGNORECASE,
)
# Claves cuyo valor SÍ ayuda a entender el contrato y no identifica a nadie.
_CLAVE_CODIGO = re.compile(
    # "nume" solo como NumeAsiento: en JELAF "Numero" suele ser el DNI (fuga vista el 29/09).
    r"^(codi|nro|numeasiento|tipo|flag|es[A-Z]|id[A-Z_]|estado|hora|fechaviaje|fechaprogramacion|"
    r"terminal|moneda|precio|monto|total|cantidad|piso|serie|correlativ)",
    re.IGNORECASE,
)
# Valores que el bot necesita confirmar (venta_jelaf.POR_VALIDAR) y que no
# identifican a nadie: se muestran tal cual aunque el filtro general los tape.
# CodiDocumento: qué comprobante pide boletería (boleta/factura); "doc" lo tapaba.
CLAVES_VISIBLES = {"nacionalidad", "descuentotipodc", "importedescuentodc", "importedescontadodc",
                   "coditarjetacredito", "canalventa", "codidocumento"}

_CABECERAS_ESTANDAR = {
    "accept", "accept-encoding", "accept-language", "cache-control", "connection",
    "content-length", "content-type", "host", "origin", "pragma", "referer",
    "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform", "sec-fetch-dest",
    "sec-fetch-mode", "sec-fetch-site", "user-agent", "cookie",
}


# --------------------------------------------------------------------------- #
# Funciones puras (tests/test_descubrir.py)
# --------------------------------------------------------------------------- #
def redactar(valor, clave: str = ""):
    """Copia de `valor` sin datos personales: conserva estructura, tipos y códigos."""
    if isinstance(valor, dict):
        return {k: redactar(v, str(k)) for k, v in valor.items()}
    if isinstance(valor, list):
        return [redactar(v, clave) for v in valor[:3]] + ([f"<+{len(valor) - 3} más>"] if len(valor) > 3 else [])
    if valor is None or isinstance(valor, bool):
        return valor
    if clave.lower() in CLAVES_VISIBLES and isinstance(valor, (str, int, float)) and len(str(valor)) <= 20:
        return valor
    # "TipoDocumento"/"tipoDoc" es un código (01 = DNI), no identifica a nadie.
    sensible = bool(_CLAVE_SENSIBLE.search(clave)) and not clave.lower().startswith("tipo")
    if isinstance(valor, (int, float)):
        return "<numero>" if sensible else valor
    if isinstance(valor, str):
        texto = valor.strip()
        if texto[:1] in "{[" and not sensible:
            try:   # JELAF manda a veces JSON dentro de un string ({"request": "{...}"})
                return {"<json-en-texto>": redactar(json.loads(texto), clave)}
            except ValueError:
                pass
        if not sensible and _CLAVE_CODIGO.search(clave) and len(texto) <= 25:
            return valor
        return f"<texto:{len(valor)}>"
    return f"<{type(valor).__name__}>"


def esquema(valor, profundidad: int = 0):
    """Solo la forma de una respuesta: claves y tipos, nunca valores."""
    if profundidad > 6:
        return "…"
    if isinstance(valor, dict):
        return {k: esquema(v, profundidad + 1) for k, v in valor.items()}
    if isinstance(valor, list):
        return [esquema(valor[0], profundidad + 1), f"<lista:{len(valor)}>"] if valor else ["<lista:0>"]
    return type(valor).__name__


def redactar_cuerpo(cuerpo: str | None, tipo: str) -> object:
    """Cuerpo de una petición (JSON, formulario u otro), ya sin datos personales."""
    if not cuerpo:
        return None
    if "json" in tipo or cuerpo.lstrip()[:1] in "{[":
        try:
            return redactar(json.loads(cuerpo))
        except ValueError:
            pass
    if "form" in tipo or ("=" in cuerpo and " " not in cuerpo):
        return {"<formulario>": {k: redactar(v, k) for k, v in parse_qsl(cuerpo, keep_blank_values=True)}}
    return f"<cuerpo no reconocido: {len(cuerpo)} bytes>"


def ruta_sin_datos(url: str) -> dict:
    """Ruta y nombres de parámetros de la URL; valores tapados salvo códigos."""
    partes = urlsplit(url)
    params = {k: redactar(v, k) for k, v in parse_qsl(partes.query, keep_blank_values=True)}
    return {"ruta": partes.path, "params": params}


def cabeceras_propias(cabeceras: dict) -> list[str]:
    """Nombres de cabeceras no estándar (p. ej. un token anti-CSRF). Sin valores."""
    return sorted(k for k in cabeceras if k.lower() not in _CABECERAS_ESTANDAR)


def describir_autorizacion(valor: str | None, vistas: dict) -> dict | None:
    """
    Forma de la cabecera Authorization sin guardarla: esquema ("Bearer", "Basic"...),
    largo y un número de versión que sube cada vez que el valor cambia. Sirve
    para saber si JELAF usa un token fijo por sesión o uno por petición.
    """
    if not valor:
        return None
    esquema_auth, _, resto = valor.partition(" ")
    if not resto:
        esquema_auth, resto = "<sin esquema>", valor
    version = vistas.setdefault(hash(valor), len(vistas) + 1)
    return {"esquema": esquema_auth[:20], "largo": len(resto), "version": version}


# Catálogos cuyo contenido (id + etiqueta) no identifica a nadie y hace falta
# conocer: qué código es "tarjeta", si existe "Yape", etc. get-usuarios NO va
# aquí: sus etiquetas son nombres de empleados.
CATALOGOS = ("get-tipoPago", "get-tipoPago2", "get-tarjetasCredito", "get-tiposDoc",
             "get-servicios", "get-cuidades", "get-empresas")


def _es_catalogo(ruta: str) -> bool:
    # Cualquier get-tipo… es un catálogo de códigos (p. ej. tipos de comprobante para
    # la factura, cuyo nombre aún no se conoce); get-usuarios nunca (nombres de empleados).
    final = ruta.rsplit("/", 1)[-1]
    return final in CATALOGOS or final.lower().startswith("get-tipo")


def valores_catalogo(ruta: str, datos) -> list[dict] | None:
    """[{id, label}] de un catálogo permitido; None para cualquier otra ruta."""
    if not _es_catalogo(ruta) or not isinstance(datos, dict):
        return None
    filas = datos.get("Valor")
    if not isinstance(filas, list):
        return None
    return [{"id": f.get("id"), "label": f.get("label")} for f in filas if isinstance(f, dict)]


def correlativo_sin_datos(ruta: str, datos) -> object | None:
    """
    Valores de busca-correlativo (series y números de comprobante, tipo de
    terminal), pasados por `redactar`. Para facturar hay que saber de dónde
    sale la serie de la factura; la forma sola no alcanza. None en otras rutas.
    """
    if "correlativo" not in ruta.lower():
        return None
    return redactar(datos)


# Precios, niveles y bloqueos del plano. La captura del 03/10 mostró que
# /turnos y lista-itinerarios-paginado traen Precio_N1/Precio_N2 (¿precio por
# piso?), y por asiento Nivel, PrecioNormal, PrecioVenta, TipoAsiento... y
# TablaBloqueoAsientos (¿asientos apartados?). Hacen falta los VALORES para
# saber de dónde sale el precio de un asiento en un viaje sin ventas (hallazgo
# N19) y si los apartados se ven ahí. Lista CERRADA de campos: nada de nombres,
# documentos, teléfonos, correos ni usuarios de JELAF.
_CAMPOS_ASIENTO = ("NumeAsiento", "Nivel", "PrecioNormal", "PrecioVenta", "PrecioMinimo", "PrecioMaximo",
                   "PrecioNormalPanel", "Id_TipoAsiento", "TipoAsiento", "FlagVenta", "Tipo", "Color",
                   "AsientoPromo")
_CAMPOS_TURNO = ("HoraPartida", "NomServicio", "PlacaBus", "PlanoBus", "CapacidadBus", "AsientosVendidos",
                 "Precio_N1", "Precio_N2", "CantidadMaxBloqAsi", "CantidadMaxBloqAsiR", "ProgramacionCerrada",
                 "PlanoCerradoWeb")


def _valor_simple(valor, largo: int = 30):
    """Solo números, booleanos y textos cortos (códigos, colores, listas de asientos)."""
    if valor is None or isinstance(valor, (bool, int, float)):
        return valor
    if isinstance(valor, str):
        return valor[:largo]
    return f"<{type(valor).__name__}>"


def precios_y_bloqueos(ruta: str, datos) -> dict | list | None:
    """
    De /turnos: precios por piso, límites, TablaBloqueoAsientos, resumen y,
    por asiento, solo número, nivel, precios, tipo y estado. De
    lista-itinerarios(-paginado): por turno, hora, bus, capacidad y precios.
    None para cualquier otra ruta. Nunca devuelve datos de pasajeros.
    """
    valor = datos.get("Valor") if isinstance(datos, dict) else None
    if ruta.endswith("/itinerarios/turnos") and isinstance(valor, dict):
        bloqueo = valor.get("TablaBloqueoAsientos")
        resumen = valor.get("ListaResumenProgramacion")
        return {
            **{k: _valor_simple(valor.get(k)) for k in _CAMPOS_TURNO if k in valor},
            # Listas de asientos ("05,06,…"): pueden ser largas, pero no identifican a nadie.
            "TablaBloqueoAsientos": ({k: _valor_simple(v, 300) for k, v in bloqueo.items()}
                                     if isinstance(bloqueo, dict) else _valor_simple(bloqueo)),
            "ListaResumenProgramacion": ({k: _valor_simple(v) for k, v in resumen.items()}
                                         if isinstance(resumen, dict) else _valor_simple(resumen)),
            "asientos": [{k: _valor_simple(a.get(k)) for k in _CAMPOS_ASIENTO if k in a}
                         for a in (valor.get("ListaPlanoBus") or []) if isinstance(a, dict)],
        }
    if ruta.endswith(("/itinerarios/lista-itinerarios", "/itinerarios/lista-itinerarios-paginado")) \
            and isinstance(valor, list):
        return [{k: _valor_simple(v.get(k)) for k in _CAMPOS_TURNO if k in v} for v in valor if isinstance(v, dict)]
    return None


def claves_con_token(almacen: dict, token: str | None) -> list[str]:
    """Nombres de las claves cuyo valor contiene el token (sin guardar ni el valor ni el token)."""
    if not token or len(token) < 4:
        return []
    return sorted(k for k, v in almacen.items() if isinstance(v, str) and token in v)


def redactar_almacen(almacen: dict) -> dict:
    """localStorage/sessionStorage sin datos personales (los valores JSON se recorren)."""
    salida = {}
    for clave, valor in almacen.items():
        try:
            salida[clave] = redactar(json.loads(valor), clave)
        except (ValueError, TypeError):
            salida[clave] = redactar(valor, clave)
    return salida


def _normalizar_nombre(texto: str) -> str:
    return " ".join(str(texto or "").upper().split())


def orden_del_nombre(nombre: str, pasajeros: list[tuple[str, str, str]]) -> str:
    """
    ¿En qué orden arma JELAF el "Nombre" completo de grabar-venta? Se compara
    con los datos de grabar-pasajero de la misma venta (en memoria, nunca se
    guardan). Devuelve solo la conclusión.
    """
    objetivo = _normalizar_nombre(nombre)
    for nombres, paterno, materno in pasajeros:
        if objetivo == _normalizar_nombre(f"{nombres} {paterno} {materno}"):
            return "nombres_primero"
        if objetivo == _normalizar_nombre(f"{paterno} {materno} {nombres}"):
            return "apellidos_primero"
    return "otro"


def es_relevante(url: str, host: str) -> bool:
    partes = urlsplit(url)
    return partes.hostname == host and not partes.path.lower().endswith(IGNORAR_EXT)


# --------------------------------------------------------------------------- #
# Captura
# --------------------------------------------------------------------------- #
class Captura:
    def __init__(self, host: str):
        self.host = host
        self.eventos: list[dict] = []
        self.etiqueta = "inicio"
        self._lock = threading.Lock()
        self._autorizaciones: dict = {}
        self._token: str | None = None         # solo en memoria, nunca se escribe
        self.catalogos: dict[str, list] = {}
        self.login: object = None
        self.almacen: dict = {}
        self.origen_token: dict[str, list] = {}
        self._pasajeros: list[tuple[str, str, str]] = []   # solo en memoria
        self.orden_nombre: list[str] = []
        self.precios: list[dict] = []          # (paso, ruta, precios_y_bloqueos), los últimos 40
        self.correlativos: list[dict] = []     # (paso, ruta, valores sin datos personales)

    def marcar(self, texto: str) -> None:
        with self._lock:
            self.etiqueta = texto.strip()[:40] or self.etiqueta
            self.eventos.append({"ts": datetime.now().isoformat(timespec="milliseconds"),
                                 "marca": self.etiqueta})
        print(f"   ── marca: {self.etiqueta}")

    async def al_responder(self, respuesta) -> None:
        pedido = respuesta.request
        if pedido.resource_type not in ("xhr", "fetch", "document") or not es_relevante(pedido.url, self.host):
            return
        cabeceras = await pedido.all_headers()
        if cabeceras.get("authorization"):
            self._token = cabeceras["authorization"].partition(" ")[2] or cabeceras["authorization"]
        tipo = cabeceras.get("content-type", "")
        resp_esquema = None
        try:
            if pedido.resource_type != "document":
                texto = await respuesta.text()
                try:   # JELAF a veces responde JSON con otro content-type
                    datos = json.loads(texto)
                    resp_esquema = esquema(datos)
                    catalogo = valores_catalogo(urlsplit(pedido.url).path, datos)
                    if catalogo is not None:
                        self.catalogos[urlsplit(pedido.url).path] = catalogo
                    precios = precios_y_bloqueos(urlsplit(pedido.url).path, datos)
                    if precios:
                        with self._lock:
                            self.precios = (self.precios + [{"paso": self.etiqueta,
                                                             "ruta": urlsplit(pedido.url).path,
                                                             "datos": precios}])[-40:]
                    correlativo = correlativo_sin_datos(urlsplit(pedido.url).path, datos)
                    if correlativo is not None:
                        with self._lock:
                            self.correlativos = (self.correlativos + [{"paso": self.etiqueta,
                                                                       "ruta": urlsplit(pedido.url).path,
                                                                       "datos": correlativo}])[-20:]
                except ValueError:
                    resp_esquema = f"<{respuesta.headers.get('content-type', '?')}: {len(texto)} bytes>"
        except Exception as e:
            resp_esquema = f"<no se pudo leer: {type(e).__name__}>"
        self._aprender_nombres(urlsplit(pedido.url).path, pedido.post_data)
        evento = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "paso": self.etiqueta,
            "metodo": pedido.method,
            **ruta_sin_datos(pedido.url),
            "estado": respuesta.status,
            "cabeceras_propias": cabeceras_propias(cabeceras),
            "autorizacion": describir_autorizacion(cabeceras.get("authorization"), self._autorizaciones),
            "cuerpo": redactar_cuerpo(pedido.post_data, tipo),
            "respuesta": resp_esquema,
        }
        with self._lock:
            self.eventos.append(evento)
        print(f"   {evento['metodo']:6} {evento['estado']} {evento['ruta']}")

    def guardar(self) -> tuple[Path, Path]:
        SALIDA.mkdir(exist_ok=True)
        sello = datetime.now().strftime("%Y%m%d_%H%M%S")
        crudo = SALIDA / f"venta_{sello}.json"
        resumen = SALIDA / f"venta_{sello}_resumen.md"
        crudo.write_text(json.dumps(self.eventos, ensure_ascii=False, indent=2), encoding="utf-8")

        lineas = ["# Llamadas de JELAF durante una venta (sin datos personales)", "",
                  "| # | Paso | Método | Ruta | Estado | Authorization | Cabeceras propias |",
                  "|---|---|---|---|---|---|---|"]
        vistos = set()
        for i, e in enumerate(x for x in self.eventos if "ruta" in x):
            a = e.get("autorizacion")
            auth = f"{a['esquema']} ({a['largo']} car., v{a['version']})" if a else "—"
            propias = [c for c in e["cabeceras_propias"] if not c.startswith(":") and c != "authorization"]
            lineas.append(f"| {i} | {e['paso']} | {e['metodo']} | `{e['ruta']}` | {e['estado']} | {auth} | "
                          f"{', '.join(propias) or '—'} |")
        lineas += ["", "## Detalle por ruta (primera aparición)", ""]
        for e in self.eventos:
            if "ruta" not in e or (e["metodo"], e["ruta"]) in vistos:
                continue
            vistos.add((e["metodo"], e["ruta"]))
            lineas += [f"### {e['metodo']} `{e['ruta']}` — paso: {e['paso']}", "",
                       "Cuerpo:", "```json", json.dumps(e["cuerpo"], ensure_ascii=False, indent=2), "```",
                       "Respuesta (forma):", "```json", json.dumps(e["respuesta"], ensure_ascii=False, indent=2), "```", ""]
        lineas += ["## Orden del nombre en grabar-venta", "",
                   ", ".join(self.orden_nombre) if self.orden_nombre else "No hubo grabar-venta en esta captura.",
                   "", "## Respuesta del login (valores sin datos personales)", "", "```json",
                   json.dumps(self.login, ensure_ascii=False, indent=2), "```", "",
                   "## Catálogos (id = etiqueta)", ""]
        for ruta, filas in self.catalogos.items():
            lineas.append(f"- `{ruta}`: " + ", ".join(f"{f['id']} = {f['label']}" for f in filas))
        lineas += ["", "## Precios, niveles y bloqueos del plano (sin datos personales)", "",
                   "Por cada consulta de turno/itinerarios: precios por piso (Precio_N1/N2), límites,",
                   "TablaBloqueoAsientos y, por asiento, número, nivel, precios, tipo y estado.", ""]
        for p in self.precios:
            lineas += [f"### {p['ruta']} — paso: {p['paso']}", "", "```json",
                       json.dumps(p["datos"], ensure_ascii=False), "```", ""]
        if not self.precios:
            lineas.append("No hubo consultas de turno en esta captura.")
        lineas += ["", "## Correlativos y series de comprobante (boleta / factura)", ""]
        for c in self.correlativos:
            lineas += [f"### {c['ruta']} — paso: {c['paso']}", "", "```json",
                       json.dumps(c["datos"], ensure_ascii=False, indent=2), "```", ""]
        if not self.correlativos:
            lineas.append("No hubo consultas de correlativo en esta captura.")
        lineas += ["", "## ¿Dónde guarda la página el Token?", "",
                   json.dumps(self.origen_token, ensure_ascii=False) if self.origen_token
                   else "No se encontró en localStorage, sessionStorage ni cookies (probablemente fijo en el código JS).",
                   "", "## localStorage / sessionStorage (sin datos personales)", "", "```json",
                   json.dumps(self.almacen, ensure_ascii=False, indent=2), "```"]
        resumen.write_text("\n".join(lineas), encoding="utf-8")
        return crudo, resumen

    def _aprender_nombres(self, ruta: str, cuerpo: str | None) -> None:
        """Deduce el orden del nombre en grabar-venta sin guardar ningún nombre."""
        try:
            interno = json.loads(json.loads(cuerpo or "{}").get("request") or "null")
        except (ValueError, AttributeError, TypeError):
            return
        if ruta.endswith("/grabar-pasajero") and isinstance(interno, list):
            self._pasajeros = [(c.get("NombreCliente", ""), c.get("ApellidoPaterno", ""), c.get("ApellidoMaterno", ""))
                               for c in interno if isinstance(c, dict)]
        elif ruta.endswith("/grabar-venta") and isinstance(interno, dict):
            for item in interno.get("Listado") or []:
                if isinstance(item, dict):
                    self.orden_nombre.append(orden_del_nombre(item.get("Nombre", ""), self._pasajeros))

    async def leer_login(self, route) -> None:
        """
        La respuesta del login no se podía leer después (la página navega y el
        navegador la descarta). Se hace la MISMA petición, se entrega tal cual a
        la página y se guarda su contenido sin datos personales.
        """
        respuesta = await route.fetch()
        try:
            self.login = redactar(json.loads(await respuesta.text()))
        except Exception as e:
            self.login = f"<no se pudo leer: {type(e).__name__}>"
        await route.fulfill(response=respuesta)

    async def fotografiar_almacen(self, contexto) -> None:
        """Estado del almacenamiento del navegador y dónde aparece el Token (solo nombres de clave)."""
        cookies = {c["name"]: c["value"] for c in await contexto.cookies()}
        origen = {"cookies": claves_con_token(cookies, self._token)}
        for i, pagina in enumerate(contexto.pages):
            try:
                datos = await pagina.evaluate(
                    "() => ({local: {...localStorage}, session: {...sessionStorage}})")
            except Exception:
                continue
            self.almacen[f"pagina{i}"] = {"localStorage": redactar_almacen(datos["local"]),
                                          "sessionStorage": redactar_almacen(datos["session"])}
            origen[f"pagina{i}.localStorage"] = claves_con_token(datos["local"], self._token)
            origen[f"pagina{i}.sessionStorage"] = claves_con_token(datos["session"], self._token)
        self.origen_token = {k: v for k, v in origen.items() if v}


def _leer_marcas(captura: Captura, fin: threading.Event) -> None:
    while not fin.is_set():
        try:
            linea = input()
        except (EOFError, OSError):
            return
        captura.marcar(linea)


async def capturar(captura: Captura, url: str, headless: bool, segundos: float | None) -> None:
    from playwright.async_api import async_playwright

    fin = threading.Event()
    if sys.stdin and sys.stdin.isatty():
        threading.Thread(target=_leer_marcas, args=(captura, fin), daemon=True).start()

    async with async_playwright() as p:
        navegador = await p.chromium.launch(channel="chrome", headless=headless)
        contexto = await navegador.new_context(viewport={"width": 1366, "height": 768})
        pendientes: set[asyncio.Task] = set()

        def al_responder(r):
            t = asyncio.create_task(captura.al_responder(r))
            pendientes.add(t)
            t.add_done_callback(pendientes.discard)

        contexto.on("response", al_responder)   # todas las pestañas y ventanas emergentes
        await contexto.route("**/login/post-usuario", captura.leer_login)

        async def fotografiar_cada_rato():
            while True:
                await asyncio.sleep(3)
                try:
                    await captura.fotografiar_almacen(contexto)
                except Exception:
                    pass   # la página puede estar navegando; se reintenta en 3 s
        fotos = asyncio.create_task(fotografiar_cada_rato())
        cerrado = asyncio.Event()
        navegador.on("disconnected", lambda *_: cerrado.set())
        pagina = await contexto.new_page()
        await pagina.goto(url, wait_until="domcontentloaded")
        print("\nNavegador listo. Inicia sesión y haz la venta; escribe una etiqueta + Enter para marcar pasos.")
        print("Cierra el navegador (o Ctrl+C) para terminar.\n")
        try:
            if segundos is not None:
                await asyncio.wait_for(cerrado.wait(), timeout=segundos)
            else:
                await cerrado.wait()
        except (asyncio.TimeoutError, KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            fin.set()
            fotos.cancel()
            if pendientes:
                await asyncio.gather(*pendientes, return_exceptions=True)
            # En Windows, Ctrl+C mata también el proceso interno de Playwright:
            # cerrar el navegador falla ("Connection closed while reading from
            # the driver"). No importa: lo capturado ya está en memoria.
            try:
                if navegador.is_connected():
                    await navegador.close()
            except Exception:
                pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Captura pasiva de las llamadas de JELAF en una venta.")
    parser.add_argument("--url", default=URL_ITINERARIOS)
    parser.add_argument("--headless", action="store_true", help="solo para la prueba local")
    parser.add_argument("--segundos", type=float, help="terminar sola tras N segundos (prueba local)")
    args = parser.parse_args()

    for flujo in (sys.stdout, sys.stderr):
        try:
            flujo.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    captura = Captura(urlsplit(args.url).hostname or "")
    try:
        asyncio.run(capturar(captura, args.url, args.headless, args.segundos))
    except KeyboardInterrupt:
        pass   # Ctrl+C también guarda lo capturado hasta ese momento
    except Exception as e:
        # Cualquier cierre brusco de Playwright: se avisa, pero SE GUARDA igual.
        print(f"\n(El navegador se cerró de forma brusca: {type(e).__name__}. Se guarda lo capturado.)")
    crudo, resumen = captura.guardar()
    total = sum(1 for e in captura.eventos if "ruta" in e)
    print(f"\n{total} peticiones capturadas.\n  Detalle: {crudo}\n  Resumen: {resumen}")


if __name__ == "__main__":
    main()
