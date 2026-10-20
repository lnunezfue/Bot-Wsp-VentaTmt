"""
vendedor_http.py — ventas en JELAF por HTTP, varias a la vez, sin navegador.

Reemplaza al pool de navegadores (jelaf_worker.py de CHATBOT PANELES). Antes
cada venta ocupaba un Chrome entero durante 5–15 minutos y con una cuenta
había UNA venta a la vez. Ahora:

  - `VentaHttp`: el estado de UNA venta (viaje, asientos con su id de
    bloqueo, pasajeros). Tiene la misma interfaz que el worker viejo (lock,
    dueño, inactividad, liberación), por eso api_ventas.py casi no cambió.
  - `VendedorHttp`: las ventas abiertas de UNA cuenta de JELAF. Admite
    `capacidad` ventas simultáneas (VENTAS_POR_CUENTA). Todo corre en
    paralelo salvo el último paso (liberarArregloAsientos + grabar-venta),
    que se hace de a una: ~1–2 s por venta, no limita en la práctica.
  - `PoolCuentas`: varias cuentas (Y1…Y6 y la de siempre como reserva), un
    `VendedorHttp` por cuenta. Cada venta queda atada a su cuenta de principio
    a fin (bloqueo, pasajeros, emisión y liberación con el mismo usuario y
    Terminal). La emisión sigue siendo de a una ENTRE TODAS las cuentas: no
    está confirmado que cada usuario tenga su propia caja en JELAF.

Modo simulación (VENTA_MODO=simulacion, el valor por defecto): se consulta
JELAF de verdad (viajes, plano, precios, clientes), pero NO se bloquean
asientos, no se graban pasajeros y no se emite. El cuerpo que se habría
enviado a grabar-venta se guarda (sin datos personales) en
descubrimiento_output/ para compararlo con una venta real de boletería.
"""

from __future__ import annotations

import asyncio
import dataclasses
import itertools
import json
import logging
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

import bloqueos_registro
import privacidad
from paneles import tiempo
from descubrir_endpoints_venta import redactar
from jelaf_http import ClienteJelafHttp, ErrorJelaf, ErrorJelafRechazo, ViajeJelaf, a_decimal, parsear_turnos
from venta_jelaf import (
    AsientoVenta,
    DatosIncompletos,
    DatosPago,
    Facturacion,
    Pasajero,
    asiento_en_plano,
    boletos_de_respuesta,
    cliente_para_jelaf,
    cuerpo_bloqueo,
    cuerpo_comprobante,
    cuerpo_venta,
    nombre_para_jelaf,
    tipo_doc_jelaf,
)

log = logging.getLogger("tmt.vendedor")

# Mismos límites que tenía el worker (ver docs/architecture.md).
TIMEOUT_INACTIVIDAD_SEGUNDOS = 600            # en fase de pago
# Eligiendo asientos / llenando datos. Era 300: el cliente que salía de WhatsApp
# (a buscar un DNI, a mirar su saldo en Yape) perdía los asientos (N60-SN).
TIMEOUT_INACTIVIDAD_SELECCION_SEGUNDOS = 600
KEEPALIVE_SEGUNDOS = 240
CACHE_TURNO_SEGUNDOS = 30
SALIDA_SIMULACION = Path("descubrimiento_output")


class JelafAsientoNoDisponibleError(Exception):
    """El asiento no se pudo apartar (vendido, bloqueado por otro canal, o límite de JELAF)."""


class VentaHttp:
    def __init__(self, vendedor: "VendedorHttp", booking_id: str):
        self.vendedor = vendedor
        self.lock = asyncio.Lock()
        self.estado = "en_uso"
        self.owner_token = booking_id
        self.last_activity = time.monotonic()
        self.reservado_en = time.monotonic()

        self.viaje: ViajeJelaf | None = None
        self.destino = None
        self.placa = None
        self.cache_turno = None               # (monotonic, EstadoTurno) — lo usa api_ventas
        self._turno: tuple[float, dict] | None = None
        self.tarea_busqueda: asyncio.Task | None = None

        # {"05": {precio, precio_normal, nivel, ids, pasajero_cargado, documento, ...}}
        self.asientos_en_venta: dict[str, dict] = {}
        self.pago_preparado = False
        self.en_fase_pago = False
        self.pago_pendiente_hasta = 0.0
        self.fase_emision: str | None = None  # None | preparando | aceptado | rechazado
        self.datos_pago: DatosPago | None = None
        self.facturacion: Facturacion | None = None   # None = boleta
        self.correo: str | None = None                # adonde se mandan los boletos
        self._emitida = False
        self._bloqueos_soltados = False        # liberarArregloAsientos ya se envió (paso previo a emitir)

    # ------------------------------------------------------------------ #
    # Dueño, actividad y liberación (misma lógica que el worker viejo)
    # ------------------------------------------------------------------ #
    def es_dueno(self, booking_id: str) -> bool:
        return self.estado == "en_uso" and self.owner_token == booking_id

    def tocar_actividad(self) -> None:
        self.last_activity = time.monotonic()

    def segundos_inactivo(self) -> float:
        return time.monotonic() - self.last_activity

    def timeout_inactividad(self) -> int:
        return TIMEOUT_INACTIVIDAD_SEGUNDOS if self.en_fase_pago else TIMEOUT_INACTIVIDAD_SELECCION_SEGUNDOS

    def motivo_para_liberar(self, duracion_maxima_seleccion: float, ahora: float | None = None) -> str | None:
        if self.estado != "en_uso":
            return None
        ahora = time.monotonic() if ahora is None else ahora
        if ahora < self.pago_pendiente_hasta:
            return None                       # checkout de Yupy vivo: el cliente puede estar pagando (N4)
        silencio = ahora - self.last_activity
        if silencio > self.timeout_inactividad():
            return f"inactividad de {int(silencio)}s"
        if not self.en_fase_pago and ahora - self.reservado_en > duracion_maxima_seleccion:
            return f"superó la duración máxima de {int(duracion_maxima_seleccion)}s eligiendo asientos"
        return None

    async def liberar_y_resetear(self) -> None:
        """
        Cierra la venta: suelta en JELAF los asientos que sigan bloqueados (si
        no se llegó a emitir) y la quita del registro. Llamar con `self.lock`.
        """
        tarea = self.tarea_busqueda
        if tarea is not None and not tarea.done():
            tarea.cancel()
        ids = [a["ids"] for a in self.asientos_en_venta.values() if a.get("ids")]
        if ids and not self._emitida and not self._bloqueos_soltados:
            await self.vendedor.soltar_bloqueos(ids)
        self.vendedor.olvidar(self)
        self.estado = "libre"
        self.owner_token = None
        self.asientos_en_venta = {}

    # ------------------------------------------------------------------ #
    # Viaje
    # ------------------------------------------------------------------ #
    def lanzar_busqueda_en_fondo(self, booking_id: str, codi_origen: int, fecha_jelaf: str, hora: str) -> None:
        """Prefetch del turno (antes: abrir el viaje en el navegador). Idempotente."""
        if self.tarea_busqueda is not None and not self.tarea_busqueda.done():
            return

        async def _tarea():
            await self._turno_crudo(fresco=True)

        self.tarea_busqueda = asyncio.create_task(_tarea())
        self.tarea_busqueda.add_done_callback(_consumir_excepcion)

    async def esperar_viaje_listo(self) -> None:
        if self.tarea_busqueda is not None:
            await asyncio.shield(self.tarea_busqueda)

    async def _turno_crudo(self, fresco: bool = False) -> dict:
        if self.viaje is None:
            raise ErrorJelaf("La venta no tiene viaje.")
        if not fresco and self._turno and time.monotonic() - self._turno[0] < CACHE_TURNO_SEGUNDOS:
            return self._turno[1]
        turno = await asyncio.to_thread(self.vendedor.jelaf.turno_crudo, self.viaje)
        self._turno = (time.monotonic(), turno)
        return turno

    # ------------------------------------------------------------------ #
    # Asientos
    # ------------------------------------------------------------------ #
    async def seleccionar_asiento(self, numero: str, precio: Decimal) -> None:
        numero = str(numero).zfill(2)
        if numero in self.asientos_en_venta:
            return                                # ya es de esta venta: idempotente
        turno = await self._turno_crudo(fresco=True)
        plano = asiento_en_plano(turno, numero)
        if plano is None:
            raise JelafAsientoNoDisponibleError(f"El asiento {numero} no existe en este bus.")
        # JELAF acepta bloquear un asiento que ya tiene venta registrada (solo
        # rechaza si otro BLOQUEO lo tiene): por eso se revisa el plano fresco antes.
        if numero in parsear_turnos(turno).ocupados:
            raise JelafAsientoNoDisponibleError(f"El asiento {numero} ya no está disponible.")
        prog = int(turno.get("CodiProgramacion") or 0)
        maximo = int(turno.get("CantidadMaxBloqAsi") or 0)
        if maximo and self.vendedor.bloqueos_en(prog) >= maximo:
            raise JelafAsientoNoDisponibleError(
                f"JELAF permite {maximo} asientos apartados a la vez por bus y ya están en uso por otras compras.")

        # Como la pantalla de JELAF: consulta el manifiesto del viaje antes de bloquear.
        await self._consulta_suelta("manifiesto", self.vendedor.jelaf.consultar_manifiesto,
                                    turno.get("CodiSucursal"), prog)
        cuerpo = cuerpo_bloqueo(turno, self.viaje.fecha_jelaf, numero)
        if self.vendedor.simulacion:
            ids = self.vendedor.ids_simulado()
            log.info("[simulación] bloquearAsiento %s (no se envía a JELAF)", cuerpo)
        else:
            try:
                ids = await asyncio.to_thread(self.vendedor.jelaf.bloquear_asiento, cuerpo)
            except ErrorJelafRechazo as e:
                raise JelafAsientoNoDisponibleError(str(e)) from e
            # En disco hasta soltarlo o venderlo: si el backend se cae a mitad de
            # la compra, liberar_asientos.py sabe qué quedó apartado y con qué cuenta.
            jelaf = self.vendedor.jelaf
            bloqueos_registro.anotar(
                ids=ids, cuenta=str(getattr(jelaf, "usuario", "") or ""),
                terminal=int((jelaf.cajero or {}).get("Terminal") or 0), asiento=numero, programacion=prog,
                viaje=dataclasses.asdict(self.viaje), booking=privacidad.booking(self.owner_token))

        self.asientos_en_venta[numero] = {
            "precio": Decimal(precio),
            "precio_normal": a_decimal(plano.get("PrecioNormal")) or Decimal(precio),
            "nivel": int(plano.get("Nivel") or 1),
            "ids": ids,
            "programacion": prog,
            "pasajero_cargado": False,
            "documento": None, "nombres": None, "paterno": None, "materno": None,
        }
        self.pago_preparado = False
        self.tocar_actividad()

    async def deseleccionar_asiento(self, numero: str) -> None:
        numero = str(numero).zfill(2)
        info = self.asientos_en_venta.pop(numero, None)
        if info and info.get("ids"):
            await self.vendedor.soltar_bloqueos([info["ids"]])
        self.pago_preparado = False
        self.tocar_actividad()

    def bloqueos_en(self, programacion: int) -> int:
        return sum(1 for a in self.asientos_en_venta.values() if a.get("programacion") == programacion)

    # ------------------------------------------------------------------ #
    # Pasajeros
    # ------------------------------------------------------------------ #
    async def llenar_pasajero(self, numero: str, documento: str, nombres: str, paterno: str, materno: str,
                              fecha_nacimiento_ddmmyyyy: str, precio_venta: float, telefono: str = "",
                              *, sexo: str = "", tipo_documento: str = "01") -> None:
        numero = str(numero).zfill(2)
        info = self.asientos_en_venta.get(numero)
        if info is None:
            raise JelafAsientoNoDisponibleError(f"El asiento {numero} no está seleccionado en esta venta.")
        tipo_doc_jelaf(tipo_documento)   # DatosIncompletos si no es de pasajero
        pasajero = Pasajero(documento=documento, tipo_documento_web=tipo_documento, nombres=nombres,
                            paterno=paterno, materno=materno, fecha_nacimiento=fecha_nacimiento_ddmmyyyy,
                            sexo=sexo, telefono=telefono)
        id_cliente = await self._dni_como_pantalla(pasajero)
        info.update({
            "pasajero_cargado": True, "documento": documento, "nombres": nombres, "paterno": paterno,
            "materno": materno, "fecha_nacimiento": fecha_nacimiento_ddmmyyyy, "sexo": sexo,
            "tipo_documento": tipo_documento, "telefono": telefono, "id_cliente": id_cliente,
        })
        self.pago_preparado = False
        self.tocar_actividad()

    async def _dni_como_pantalla(self, p: Pasajero) -> int:
        """
        Lo que hace la pantalla de JELAF al dar Enter en el DNI: consultar-usuario
        (el IdCliente de la ficha) y las tres consultas de promoción (solo
        lectura; el bot no aplica descuentos; "Fecha" = la del viaje, POR
        CONFIRMAR si es la de venta). Devuelve el IdCliente (0 si no hay ficha).
        """
        jelaf = self.vendedor.jelaf
        tipo_jelaf = tipo_doc_jelaf(p.tipo_documento_web)
        id_cliente = 0
        try:
            datos = await asyncio.to_thread(jelaf.consultar_usuario, tipo_jelaf, p.documento)
            if datos.get("EsCorrecto"):
                ficha = datos.get("Valor") or {}
                id_cliente = int(ficha.get("IdCliente") or 0) if isinstance(ficha, dict) else 0
                if isinstance(ficha, dict) and ficha and not id_cliente:
                    # Con id 0, grabar-pasajero crearía OTRA ficha. Solo nombres de campo, sin valores.
                    log.warning("consultar-usuario devolvió una ficha sin IdCliente utilizable; campos: %s",
                                ", ".join(sorted(ficha.keys())))
        except (ErrorJelaf, TypeError, ValueError) as e:
            log.info("No se pudo buscar el cliente %s en JELAF: %s", privacidad.documento(p.documento), e)
        await self._consulta_suelta("promociones", jelaf.consultar_promociones, {
            "Tipo": tipo_jelaf, "Numero": p.documento, "Fecha": self.viaje.fecha_jelaf,
            "Origen": str(self.viaje.codi_origen), "Destino": str(self.viaje.codi_destino),
            "Servicio": str(self.viaje.codi_servicio), "Empresa": str(self.viaje.codi_empresa),
            "Usuario": (jelaf.cajero or {}).get("CodiUsuario") or 0,
            "Nombre": nombre_para_jelaf(p),   # mismo formato interno de JELAF que en la venta
        })
        return id_cliente

    async def _pasajeros_como_pantalla(self) -> None:
        """
        Justo antes de vender, DENTRO del lock de emisión: DNI → promociones de
        cada pasajero y grabar-pasajero, seguidos y en la misma sesión, como
        boletería (consultar-usuario … grabar-pasajero … Aceptar → grabar-venta).

        Por qué (04/10/2026): tres ventas reales del bot salieron con
        PasNombreCom vacío aunque `Nombre` iba lleno, la ficha tenía los nombres
        y el cuerpo era igual al de boletería. JELAF arma la venta con el
        pasajero grabado en la sesión; el bot grababa al abrir el pago y vendía
        minutos después, con la sesión ya usada por otras consultas/compras.
        Dentro del lock nadie se mete en medio. Si grabar-pasajero falla, no se
        emite (fase "preparando").
        """
        for info in self.asientos_en_venta.values():
            p = Pasajero(documento=info["documento"], tipo_documento_web=info.get("tipo_documento") or "01",
                         nombres=info["nombres"], paterno=info["paterno"], materno=info.get("materno") or "",
                         fecha_nacimiento=info["fecha_nacimiento"], sexo=info.get("sexo") or "",
                         telefono=info.get("telefono") or "")
            id_cliente = await self._dni_como_pantalla(p)
            if id_cliente:
                info["id_cliente"] = id_cliente
        clientes = [cliente_para_jelaf(a.pasajero, self.vendedor.hoy(), self.facturacion)
                    for a in self._asientos_para_jelaf()]
        await asyncio.to_thread(self.vendedor.jelaf.grabar_pasajeros, clientes)

    def definir_comprobante(self, facturacion: Facturacion | None) -> None:
        """Boleta (None) o factura. Antes de emitir; la ficha se vuelve a grabar con el RUC."""
        if self.fase_emision is not None or self._emitida:
            raise DatosIncompletos("Esta compra ya se está emitiendo: no se puede cambiar el comprobante.")
        self.facturacion = facturacion
        self.pago_preparado = False
        self.tocar_actividad()

    def _asientos_para_jelaf(self) -> list[AsientoVenta]:
        return [
            AsientoVenta(
                numero=n, precio=Decimal(i["precio"]), precio_normal=Decimal(i["precio_normal"]),
                nivel=i["nivel"], ids_bloqueo=i.get("ids"),
                pasajero=Pasajero(
                    documento=i["documento"], tipo_documento_web=i.get("tipo_documento") or "01",
                    nombres=i["nombres"], paterno=i["paterno"], materno=i.get("materno") or "",
                    fecha_nacimiento=i["fecha_nacimiento"], sexo=i.get("sexo") or "",
                    telefono=i.get("telefono") or "", id_cliente=i.get("id_cliente") or 0,
                ),
            )
            for n, i in self.asientos_en_venta.items()
        ]

    async def preparar_pago(self) -> None:
        """
        Graba en JELAF la ficha de cada pasajero (lo que boletería hace al pasar
        al pago). Se llama ANTES de crear el cobro en Yupy: si falla, no se cobra.
        """
        faltan = [a for a, i in self.asientos_en_venta.items() if not i["pasajero_cargado"]]
        if faltan or not self.asientos_en_venta:
            raise DatosIncompletos("Faltan datos de pasajeros.")
        self.en_fase_pago = True
        self.tocar_actividad()
        if self.pago_preparado:
            return
        clientes = [cliente_para_jelaf(a.pasajero, self.vendedor.hoy(), self.facturacion)
                    for a in self._asientos_para_jelaf()]
        if self.vendedor.simulacion:
            log.info("[simulación] grabar-pasajero de %d pasajero(s) (no se envía a JELAF)", len(clientes))
        else:
            await asyncio.to_thread(self.vendedor.jelaf.grabar_pasajeros, clientes)
        self.pago_preparado = True

    # ------------------------------------------------------------------ #
    # Emisión
    # ------------------------------------------------------------------ #
    async def pagar_y_confirmar(self) -> dict:
        """
        EMITE en JELAF. `fase_emision` dice hasta dónde llegó si algo falla:
          preparando → no se envió grabar-venta: JELAF no emitió.
          rechazado  → JELAF dijo que no (EsCorrecto=false): no emitió.
          aceptado   → se envió grabar-venta y no hubo respuesta clara: conciliar (C4).
        """
        self.fase_emision = "preparando"
        self.tocar_actividad()
        if not self.pago_preparado:
            await self.preparar_pago()
        turno = await self._turno_crudo(fresco=True)
        asientos = self._asientos_para_jelaf()
        jelaf = self.vendedor.jelaf
        venta = cuerpo_venta(
            turno=turno, cajero=jelaf.cajero or {}, empresa=jelaf.empresa or {},
            correlativo=jelaf.correlativo or {}, asientos=asientos,
            pago=self.datos_pago or DatosPago(), fecha_viaje_jelaf=self.viaje.fecha_jelaf,
            hoy=self.vendedor.hoy(), factura=self.facturacion,
        )
        numeros = [a.numero for a in asientos]

        # Diagnóstico de la venta sin nombre (04/10/2026), sin datos personales:
        # ¿cuánto pasó entre grabar-pasajero y grabar-venta? ¿cambió la sesión de
        # JELAF en medio (re-login)? ¿cuántos caracteres de nombre se mandaron y
        # cuántos registró JELAF (PasNombreCom)?
        diagnostico: dict = {"largo_nombre_enviado": [len(str(i.get("Nombre") or "")) for i in venta.get("Listado", [])]}
        async with self.vendedor.lock_emision:
            if self.vendedor.simulacion:
                self.vendedor.guardar_simulacion(self.owner_token, venta)
                self.fase_emision = "aceptado"
                valor = {"ListaVentasRealizadas": [
                    {"NumeAsiento": n, "BoletoCompleto": f"SIMULADO-{n}", "LinkPag_FE": None, "IdVenta": 0}
                    for n in numeros]}
            else:
                # Como boletería, todo seguido y sin nadie en medio: DNI → promociones →
                # grabar-pasajero → Aceptar (permiso, manifiesto, soltar bloqueos, venta).
                await self._pasajeros_como_pantalla()
                t_pasajero, sesion_pasajero = time.monotonic(), getattr(jelaf, "_sesion", None)
                await self._consulta_suelta("acceso", jelaf.acceso_usuario_modulo)
                await self._consulta_suelta("manifiesto", jelaf.consultar_manifiesto,
                                            turno.get("CodiSucursal"), turno.get("CodiProgramacion") or 0)
                ids = [a.ids_bloqueo for a in asientos if a.ids_bloqueo]
                if ids:
                    # Boletería suelta sus propios bloqueos justo antes de vender (captura real).
                    await asyncio.to_thread(jelaf.liberar_arreglo, int(jelaf.cajero["Terminal"]), ids)
                    self._bloqueos_soltados = True
                    bloqueos_registro.quitar(ids)
                self.fase_emision = "aceptado"
                try:
                    valor = await asyncio.to_thread(jelaf.grabar_venta, venta)
                except ErrorJelafRechazo:
                    self.fase_emision = "rechazado"
                    raise
                diagnostico.update({
                    "segundos_desde_grabar_pasajero": round(time.monotonic() - t_pasajero, 1),
                    "misma_sesion_jelaf": getattr(jelaf, "_sesion", None) is sesion_pasajero,
                    "largo_nombre_registrado": [len(str(v.get("PasNombreCom") or "").strip())
                                                for v in (valor.get("ListaVentasRealizadas") or [])
                                                if isinstance(v, dict)],
                })
                log.info("Emisión: diagnóstico del nombre %s", diagnostico)

        self._emitida = True
        por_asiento, anomalia = boletos_de_respuesta(valor, numeros)
        comprobante_ok, sin_nombre = None, []
        if not self.vendedor.simulacion:
            comprobante_ok, sin_nombre = await self._despues_de_vender(valor, numeros, por_asiento)
        boletos = []
        for a in asientos:
            b = por_asiento[a.numero]
            boletos.append({
                "asiento": a.numero,
                "pasajero": f"{a.pasajero.nombres} {a.pasajero.paterno}",
                "documento": a.pasajero.documento,
                "precio": float(a.precio),
                "nro_boleto": b["nro_boleto"],
                "link_comprobante": b["link_comprobante"],
                "estado": "SIMULADO" if self.vendedor.simulacion else "EMITIDO",
                "nombres": a.pasajero.nombres,
                "paterno": a.pasajero.paterno,
                "materno": a.pasajero.materno,
            })
        self.tocar_actividad()
        embarque = next((e for e in (turno.get("ListaEmbarques") or []) if isinstance(e, dict)), {})
        return {"boletos": boletos, "anomalia_boleto": anomalia, "popup_autorizacion": None,
                # Para el correo con el detalle de compra (terminal y hora de embarque).
                "embarque": {"lugar": embarque.get("Lugar") or None, "hora": embarque.get("Hora") or None},
                "simulado": self.vendedor.simulacion,
                "comprobante_jelaf": comprobante_ok, "asientos_sin_nombre": sin_nombre,
                "diagnostico_nombre": diagnostico}

    async def _despues_de_vender(self, valor: dict, numeros: list[str], por_asiento: dict) -> tuple[bool, list[str]]:
        """
        Lo que la pantalla de JELAF hace justo después de grabar-venta. La venta
        YA está hecha: nada de esto la repite ni la deshace.
          - PasNombreCom de la respuesta: el nombre con que JELAF registró la
            venta. Vacío = venta sin nombre (lo que pasó el 04/10/2026).
          - convertir-venta-to-base64 (cuerpo como la pantalla): el comprobante.
          - grabar-log: la bitácora.
        Devuelve (¿se generó el comprobante?, asientos registrados sin nombre).
        """
        jelaf = self.vendedor.jelaf
        sin_nombre = [str(v.get("NumeAsiento") or "").zfill(2) for v in (valor.get("ListaVentasRealizadas") or [])
                      if isinstance(v, dict) and not str(v.get("PasNombreCom") or "").strip()]
        if sin_nombre:
            log.error("JELAF registró la venta SIN nombre del pasajero (asientos %s).", ", ".join(sin_nombre))
        try:
            await asyncio.to_thread(jelaf.convertir_venta_base64, cuerpo_comprobante(valor, jelaf.cajero or {}))
            comprobante_ok = True
        except ErrorJelaf as e:
            comprobante_ok = False
            log.warning("convertir-venta-to-base64 falló después de vender (la venta queda hecha): %s", e)
        boletos = ", ".join(por_asiento[n]["nro_boleto"] or n for n in numeros)
        await self._consulta_suelta("grabar-log", jelaf.grabar_log,
                                    f"VENTA WEB WHATSAPP {boletos} ASIENTO(S) {', '.join(numeros)}")
        return comprobante_ok, sin_nombre

    async def _consulta_suelta(self, que: str, funcion, *args):
        """
        Llamadas de apoyo que hace la pantalla de JELAF (consultas y bitácora):
        si una falla, la venta sigue; se deja constancia en el log.
        """
        try:
            return await asyncio.to_thread(funcion, *args)
        except (ErrorJelaf, KeyError, TypeError, ValueError) as e:
            log.info("Llamada de apoyo '%s' a JELAF falló (la venta sigue): %s", que, e)
            return None


def _consumir_excepcion(tarea: asyncio.Task) -> None:
    if not tarea.cancelled() and tarea.exception() is not None:
        log.warning("El prefetch del turno falló: %s", tarea.exception())


class VendedorHttp:
    """Registro de ventas abiertas. Misma interfaz que el JelafWorkerPool viejo."""

    def __init__(self, jelaf: ClienteJelafHttp, capacidad: int = 10, simulacion: bool = True,
                 hoy=None, lock_emision: asyncio.Lock | None = None, pool: "PoolCuentas | None" = None):
        self.jelaf = jelaf
        self.capacidad = max(1, capacidad)
        self.simulacion = simulacion
        # Compartido entre cuentas cuando hay varias (ver PoolCuentas).
        self.lock_emision = lock_emision or asyncio.Lock()
        self.pool = pool
        self._ventas: dict[str, VentaHttp] = {}
        self._contador_simulado = itertools.count(1)
        self._ultimo_keepalive = time.monotonic()
        # "Hoy" de Lima, no del servidor: en un servidor en UTC, date.today() ya es
        # mañana desde las 7 pm (edades y fecha de la venta saldrían mal).
        self._hoy = hoy or tiempo.hoy

    # Compatibilidad con el pool viejo --------------------------------- #
    @property
    def workers(self) -> list[VentaHttp]:
        return list(self._ventas.values())

    async def iniciar_todos(self) -> None:
        return None

    async def apagar_todos(self) -> None:
        return None

    # ------------------------------------------------------------------ #
    def hoy(self) -> date:
        return self._hoy()

    def ids_simulado(self) -> int:
        return -next(self._contador_simulado)

    def worker_de(self, booking_id: str) -> VentaHttp | None:
        venta = self._ventas.get(booking_id)
        return venta if venta and venta.es_dueno(booking_id) else None

    def reservar_para(self, booking_id: str) -> VentaHttp | None:
        propia = self.worker_de(booking_id)
        if propia:
            return propia
        if len(self._ventas) >= self.capacidad:
            return None
        venta = VentaHttp(self, booking_id)
        self._ventas[booking_id] = venta
        return venta

    async def reservar_con_espera(self, booking_id: str, espera_segundos: float = 15) -> VentaHttp | None:
        limite = time.monotonic() + espera_segundos
        while True:
            venta = self.reservar_para(booking_id)
            if venta or time.monotonic() >= limite:
                return venta
            await asyncio.sleep(0.5)

    def olvidar(self, venta: VentaHttp) -> None:
        if self._ventas.get(venta.owner_token) is venta:
            del self._ventas[venta.owner_token]

    def bloqueos_en(self, programacion: int) -> int:
        """Bloqueos del bot en ese bus. Con varias cuentas, los de TODAS (CantidadMaxBloqAsi)."""
        if self.pool is not None:
            return self.pool.bloqueos_en(programacion)
        return self.bloqueos_propios(programacion)

    def bloqueos_propios(self, programacion: int) -> int:
        return sum(v.bloqueos_en(programacion) for v in self._ventas.values())

    @property
    def libres(self) -> int:
        return self.capacidad - len(self._ventas)

    async def soltar_bloqueos(self, ids: list[int]) -> None:
        """Suelta bloqueos en JELAF. Nunca lanza: si falla, JELAF los vence solo."""
        reales = [i for i in ids if i and i > 0]
        if not reales or self.simulacion:
            return
        try:
            await asyncio.to_thread(self.jelaf.liberar_arreglo, int((self.jelaf.cajero or {}).get("Terminal") or 0),
                                    reales)
        except ErrorJelaf as e:
            # Quedan en bloqueos_registro: liberar_asientos.py los muestra para soltarlos a mano.
            log.warning("No se pudieron soltar %d bloqueo(s) en JELAF: %s", len(reales), e)
            return
        bloqueos_registro.quitar(reales)

    def guardar_simulacion(self, booking_id: str, venta: dict) -> None:
        """Cuerpo de grabar-venta que se habría enviado, SIN datos personales (para comparar)."""
        try:
            SALIDA_SIMULACION.mkdir(exist_ok=True)
            archivo = SALIDA_SIMULACION / f"simulacion_{privacidad.booking(booking_id).rstrip('…')}.json"
            archivo.write_text(json.dumps(redactar(venta), ensure_ascii=False, indent=2), encoding="utf-8")
            log.info("[simulación] grabar-venta NO enviado; cuerpo guardado en %s", archivo)
        except OSError as e:
            log.warning("No se pudo guardar la simulación: %s", e)

    async def liberar_inactivos(self, duracion_maxima_seleccion: float) -> list[tuple[str, str]]:
        liberados = []
        for venta in list(self._ventas.values()):
            if venta.lock.locked() or not venta.motivo_para_liberar(duracion_maxima_seleccion):
                continue
            async with venta.lock:
                motivo = venta.motivo_para_liberar(duracion_maxima_seleccion)
                if not motivo:
                    continue
                dueno = venta.owner_token
                log.info("Liberando venta %s: %s", privacidad.booking(dueno), motivo)
                await venta.liberar_y_resetear()
                liberados.append((dueno, motivo))
        return liberados

    async def mantener_vivos(self) -> None:
        if time.monotonic() - self._ultimo_keepalive < KEEPALIVE_SEGUNDOS:
            return
        self._ultimo_keepalive = time.monotonic()
        try:
            await asyncio.to_thread(self.jelaf.mantener_sesion)
        except ErrorJelaf as e:
            log.warning("Keepalive de JELAF falló: %s", e)

    def estado(self) -> list[dict]:
        activas = [
            {
                "indice": i,
                "estado": "en_uso",
                "owner_token": privacidad.booking(v.owner_token),
                "asientos_en_venta": list(v.asientos_en_venta.keys()),
                "segundos_inactivo": round(v.segundos_inactivo(), 1),
            }
            for i, v in enumerate(self._ventas.values())
        ]
        libres = [{"indice": len(activas) + j, "estado": "libre", "owner_token": None,
                   "asientos_en_venta": [], "segundos_inactivo": None}
                  for j in range(self.capacidad - len(activas))]
        return activas + libres


class PoolCuentas:
    """
    Varias cuentas de JELAF, cada una con su `VendedorHttp`. Misma interfaz
    que `VendedorHttp` para api_ventas.py (worker_de, reservar_con_espera,
    liberar_inactivos, mantener_vivos, estado, capacidad, simulacion).

    Reparto: una venta nueva va a la cuenta con sesión que tenga lugar y menos
    ventas abiertas (a igual carga, la primera de la lista). La cuenta de
    `reserva` (la JELAF_USUARIO de siempre) solo se usa cuando todas las demás
    están llenas.
    """

    def __init__(self, clientes: list, ventas_por_cuenta: int = 1, simulacion: bool = True,
                 hoy=None, reserva: str | None = None):
        if not clientes:
            raise ValueError("PoolCuentas necesita al menos una cuenta de JELAF.")
        self.simulacion = simulacion
        self.lock_emision = asyncio.Lock()
        self.vendedores = [
            VendedorHttp(c, capacidad=ventas_por_cuenta, simulacion=simulacion, hoy=hoy,
                         lock_emision=self.lock_emision, pool=self)
            for c in clientes
        ]
        self._reserva = reserva

    # ------------------------------------------------------------------ #
    @property
    def capacidad(self) -> int:
        return sum(v.capacidad for v in self.vendedores)

    @property
    def workers(self) -> list[VentaHttp]:
        return [w for v in self.vendedores for w in v.workers]

    async def iniciar_todos(self) -> None:
        return None

    async def apagar_todos(self) -> None:
        return None

    def hoy(self) -> date:
        return self.vendedores[0].hoy()

    def clientes(self) -> list:
        return [v.jelaf for v in self.vendedores]

    def cliente_lectura(self):
        """Cuenta para las consultas de solo lectura (viajes, plano, DNI): la primera con sesión."""
        return next((v.jelaf for v in self.vendedores if getattr(v.jelaf, "disponible", False)), None)

    def _puede_vender(self, vendedor: VendedorHttp) -> bool:
        """Con sesión, y en modo real además con los datos del cajero y la empresa (sin ellos no emite)."""
        jelaf = vendedor.jelaf
        return bool(getattr(jelaf, "disponible", False)) and (self.simulacion or bool(jelaf.lista_para_vender))

    def _es_reserva(self, vendedor: VendedorHttp) -> bool:
        return bool(self._reserva) and getattr(vendedor.jelaf, "usuario", None) == self._reserva

    # ------------------------------------------------------------------ #
    def worker_de(self, booking_id: str) -> VentaHttp | None:
        for v in self.vendedores:
            venta = v.worker_de(booking_id)
            if venta:
                return venta
        return None

    def reservar_para(self, booking_id: str) -> VentaHttp | None:
        propia = self.worker_de(booking_id)
        if propia:
            return propia
        candidatos = [(self._es_reserva(v), v.capacidad - v.libres, i, v)
                      for i, v in enumerate(self.vendedores)
                      if v.libres > 0 and self._puede_vender(v)]
        if not candidatos:
            return None
        return min(candidatos, key=lambda c: c[:3])[3].reservar_para(booking_id)

    async def reservar_con_espera(self, booking_id: str, espera_segundos: float = 15) -> VentaHttp | None:
        limite = time.monotonic() + espera_segundos
        while True:
            venta = self.reservar_para(booking_id)
            if venta or time.monotonic() >= limite:
                return venta
            await asyncio.sleep(0.5)

    def bloqueos_en(self, programacion: int) -> int:
        return sum(v.bloqueos_propios(programacion) for v in self.vendedores)

    async def liberar_inactivos(self, duracion_maxima_seleccion: float) -> list[tuple[str, str]]:
        liberados = []
        for v in self.vendedores:
            liberados.extend(await v.liberar_inactivos(duracion_maxima_seleccion))
        return liberados

    async def mantener_vivos(self) -> None:
        # Solo las que tienen sesión: a una caída no se le fuerza el login cada 4 minutos.
        for v in self.vendedores:
            if getattr(v.jelaf, "disponible", False):
                await v.mantener_vivos()

    def estado(self) -> list[dict]:
        filas = []
        for v in self.vendedores:
            cuenta = getattr(v.jelaf, "usuario", None)
            for fila in v.estado():
                filas.append({**fila, "indice": len(filas), "cuenta": cuenta,
                              "sesion_jelaf": bool(getattr(v.jelaf, "disponible", False))})
        return filas
