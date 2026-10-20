"""
Router FastAPI de los tres paneles.

Se monta sobre el backend que ya existe con dos lineas en api_ventas.py:

    from paneles.api_paneles import router as router_paneles
    app.include_router(router_paneles)

Todo cuelga de /api/paneles/... para no chocar con /api/v1 ni /api/v2.

SEGURIDAD: el tablero exige la cabecera X-Paneles-Token = PANELES_TOKEN.
  - ENTORNO=produccion sin PANELES_TOKEN → todo responde 503 (falla cerrada;
    config.py ademas impide arrancar api_ventas.py en ese caso).
  - ENTORNO=desarrollo sin PANELES_TOKEN → abierto, solo para trabajar en local.
Los unicos endpoints publicos son /codigos/validar y /evento, que usa la
mini-webapp; ambos con limite de peticiones.
"""

from __future__ import annotations

import hmac
import os
from datetime import date
from typing import Callable, Literal, Optional

from contextlib import contextmanager

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

import rutas
from limitador import Limitador, ip_cliente

from . import atencion, bonos, consultas, db, fidelizacion, registro

router = APIRouter(prefix="/api/paneles", tags=["paneles"])

# Callback opcional que api_ventas.py inyecta para que el panel de incidencias
# pueda mostrar el estado del pool de workers de JELAF sin importar jelaf_worker
# desde aqui (evita una dependencia circular).
_estado_pool: Optional[Callable[[], dict]] = None

_limite_validar = Limitador(maximo=10, ventana_segundos=60)
_limite_evento = Limitador(maximo=30, ventana_segundos=60)
# Cambios de Administración (cada uno lleva su clave): frena probar claves a ciegas.
_limite_admin = Limitador(maximo=10, ventana_segundos=600)



def configurar(estado_pool: Callable[[], dict] | None = None) -> None:
    global _estado_pool
    _estado_pool = estado_pool


# Lo inyecta api_ventas.py (como _estado_pool). Sin backend de venta (por
# ejemplo servidor_paneles.py) quedan en None y el panel lo dice.
_pasarelas_configuradas: Optional[Callable[[], dict]] = None
_ciudades_jelaf: Optional[Callable[[], set]] = None


def configurar_admin(pasarelas_configuradas: Callable[[], dict] | None = None,
                     ciudades_jelaf: Callable[[], set] | None = None) -> None:
    global _pasarelas_configuradas, _ciudades_jelaf
    _pasarelas_configuradas = pasarelas_configuradas
    _ciudades_jelaf = ciudades_jelaf


def _verificar(token: str | None) -> None:
    esperado = (os.getenv("PANELES_TOKEN") or "").strip()
    if not esperado:
        if (os.getenv("ENTORNO") or "desarrollo").strip().lower() == "produccion":
            raise HTTPException(status_code=503, detail="Panel sin configurar.")
        return  # desarrollo local sin clave (config.py avisa al arrancar)
    if not token or not hmac.compare_digest(token.encode(), esperado.encode()):
        raise HTTPException(status_code=401, detail="No autorizado.")


def exigir_token(x_paneles_token: str | None) -> None:
    """Para que api_ventas.py proteja con la misma clave sus endpoints de diagnostico."""
    _verificar(x_paneles_token)


def _rango(desde: str | None, hasta: str | None, dias: int) -> tuple[str, str]:
    try:
        return consultas.rango(desde, hasta, dias)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Rango de fechas inválido: {e}") from e


def _pool() -> dict:
    return _estado_pool() if _estado_pool else {"workers": [], "nota": "pool no conectado"}


# ===========================================================================
#  PANEL 1 - VENTAS
# ===========================================================================

@router.get("/ventas")
def panel_ventas(
    desde: str | None = None,
    hasta: str | None = None,
    dias: int = Query(7, ge=1, le=365),
    x_paneles_token: str | None = Header(None),
):
    _verificar(x_paneles_token)
    d, h = _rango(desde, hasta, dias)
    return {
        "rango": {"desde": d, "hasta": h},
        "resumen": consultas.resumen_ventas(d, h),
        "por_dia": consultas.ventas_por_dia(d, h),
        "rutas": consultas.rutas_top(d, h),
        "servicios": consultas.servicios_top(d, h),
        "ultimas": consultas.ultimas_ventas(10),
    }


@router.get("/ventas/heatmap")
def panel_ventas_heatmap(
    base: str = Query("compra", pattern="^(compra|salida)$"),
    desde: str | None = None,
    hasta: str | None = None,
    dias: int = Query(7, ge=1, le=365),
    x_paneles_token: str | None = Header(None),
):
    _verificar(x_paneles_token)
    d, h = _rango(desde, hasta, dias)
    return consultas.heatmap_horas(d, h, base)


# ===========================================================================
#  PANEL 2 - INCIDENCIAS
# ===========================================================================

# N58-SN: antes solo 20; la #147 salió de la bandeja sin estar atendida. La
# pantalla pagina en el navegador; este tope solo evita una respuesta enorme.
MAX_ABIERTAS_BANDEJA = 500


@router.get("/incidencias")
def panel_incidencias(
    desde: str | None = None,
    hasta: str | None = None,
    dias: int = Query(7, ge=1, le=365),
    x_paneles_token: str | None = Header(None),
):
    _verificar(x_paneles_token)
    d, h = _rango(desde, hasta, dias)
    abiertas = consultas.resumen_abiertas()
    return {
        "rango": {"desde": d, "hasta": h},
        "resumen": consultas.resumen_incidencias(d, h),
        "embudo": consultas.embudo(d, h),
        "por_tipo": consultas.incidencias_por_tipo(d, h),
        "abiertas": consultas.incidencias_abiertas(MAX_ABIERTAS_BANDEJA),
        # Totales reales: la bandeja trae hasta MAX_ABIERTAS_BANDEJA y el panel avisa si hay más.
        "abiertas_total": abiertas["total"],
        "abiertas_por_severidad": abiertas["por_severidad"],
        "fallos_por_hora": consultas.fallos_por_hora(24),
        "pool": _pool(),
        "registro_fallos": registro.fallos_registro(),
    }


class ResolverIncidencia(BaseModel):
    nota: str | None = Field(None, max_length=500)


@router.post("/incidencias/{incidencia_id}/resolver")
def resolver(incidencia_id: int, body: ResolverIncidencia,
             x_paneles_token: str | None = Header(None)):
    _verificar(x_paneles_token)
    with _errores_de_atencion():
        registro.resolver_incidencia(incidencia_id, body.nota)
    return {"status": "success"}


@router.get("/incidencias/{incidencia_id}/cadena")
def cadena_incidencia(incidencia_id: int, x_paneles_token: str | None = Header(None)):
    _verificar(x_paneles_token)
    inc = db.consultar_uno("SELECT * FROM incidencia WHERE id = ?", (incidencia_id,))
    if not inc:
        raise HTTPException(status_code=404, detail="Incidencia no encontrada.")
    inc_dict = dict(inc)
    cadena = consultas.cadena_booking(inc_dict["booking_id"]) if inc_dict.get("booking_id") else {}
    venta = cadena.get("venta")
    sesion = cadena.get("sesion")
    telefono = inc_dict.get("telefono") or (sesion or {}).get("telefono") or (venta or {}).get("telefono")
    return {
        "incidencia": inc_dict, **cadena,
        "contacto": consultas.contacto_de(telefono),
        "pasajeros": consultas.pasajeros_de_venta(venta["id"] if venta else None),
        "atenciones": consultas.atenciones_de(incidencia_id),
        "acciones": [{"accion": a, "etiqueta": atencion.ACCIONES[a][1], "resultado": atencion.ACCIONES[a][0]}
                     for a in atencion.acciones_para(inc_dict["tipo"], inc_dict["severidad"])],
    }


class DerivarIncidencia(BaseModel):
    motivo: str = Field(..., min_length=1, max_length=300)
    derivada_a: str | None = Field(None, max_length=100)


@router.post("/incidencias/{incidencia_id}/derivar")
def derivar(incidencia_id: int, body: DerivarIncidencia,
            x_paneles_token: str | None = Header(None)):
    _verificar(x_paneles_token)
    with _errores_de_atencion():
        registro.derivar_incidencia(incidencia_id, body.motivo, body.derivada_a)
    return {"status": "success"}


@contextmanager
def _errores_de_atencion():
    """Traduce los errores de registro.registrar_atencion a respuestas para el panel."""
    try:
        yield
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except registro.IncidenciaCerrada as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


TextoLibre = Field(None, max_length=500)


class NuevaAtencion(BaseModel):
    accion: Literal[tuple(atencion.ACCIONES)]  # type: ignore[valid-type]
    canal_contacto: Literal["whatsapp", "llamada", "presencial"] | None = None
    nota: str | None = TextoLibre
    nro_boleto: str | None = Field(None, pattern=r"^[A-Za-z0-9-]{3,30}$")
    booking_nuevo: str | None = Field(None, pattern=r"^[A-Za-z0-9-]{8,64}$")
    referencia_devolucion: str | None = Field(None, min_length=3, max_length=60)
    monto_devuelto_centimos: int | None = Field(None, gt=0, le=10_000_000)
    derivada_a: str | None = Field(None, min_length=2, max_length=80)

    @field_validator("nro_boleto", "booking_nuevo", "referencia_devolucion", "derivada_a", "nota",
                     "canal_contacto", mode="before")
    @classmethod
    def _vacio_es_nulo(cls, v):
        return v.strip() or None if isinstance(v, str) else v


@router.post("/incidencias/{incidencia_id}/atencion")
def nueva_atencion(incidencia_id: int, body: NuevaAtencion,
                   x_paneles_token: str | None = Header(None)):
    _verificar(x_paneles_token)
    with _errores_de_atencion():
        r = registro.registrar_atencion(incidencia_id, body.accion, body.model_dump(exclude={"accion"}))
    return {"status": "success", **r}


@router.get("/atenciones")
def panel_atenciones(
    desde: str | None = None,
    hasta: str | None = None,
    dias: int = Query(30, ge=1, le=365),
    accion: Literal[tuple(atencion.ACCIONES)] | None = None,  # type: ignore[valid-type]
    resultado: Literal["en_seguimiento", "resuelta", "derivada"] | None = None,
    pagina: int = Query(1, ge=1, le=10_000),
    x_paneles_token: str | None = Header(None),
):
    _verificar(x_paneles_token)
    d, h = _rango(desde, hasta, dias)
    datos = consultas.casos_atendidos(d, h, accion=accion, resultado=resultado, pagina=pagina)
    datos["etiquetas"] = {a: e for a, (_, e) in atencion.ACCIONES.items()}
    return {"rango": {"desde": d, "hasta": h}, **datos}


@router.get("/abandonos")
def panel_abandonos(
    desde: str | None = None,
    hasta: str | None = None,
    dias: int = Query(30, ge=1, le=365),
    x_paneles_token: str | None = Header(None),
):
    _verificar(x_paneles_token)
    d, h = _rango(desde, hasta, dias)
    return {"rango": {"desde": d, "hasta": h}, **consultas.abandonos(d, h)}


# ===========================================================================
#  PANEL 3 - FIDELIZACION
# ===========================================================================

@router.get("/fidelizacion")
def panel_fidelizacion(
    segmento: Literal["nuevo", "ocasional", "frecuente", "dormido"] | None = None,
    x_paneles_token: str | None = Header(None),
):
    _verificar(x_paneles_token)
    return {
        "resumen": consultas.resumen_fidelizacion(),
        # Más boletos: clientes que viajan con más frecuencia.
        "top_por_boletos": consultas.top_clientes(20, segmento, ordenar_por="viajes"),
        # Más caro: clientes que más dinero han gastado (Tacna–Lima, servicios premium).
        "top_por_gasto": consultas.top_clientes(20, segmento, ordenar_por="gasto"),
        "frecuencia": consultas.distribucion_frecuencia(),
        "codigos": consultas.codigos(),
        "envio_real": fidelizacion.envio_real_activo(),
    }


class NuevoCodigo(BaseModel):
    codigo: str | None = Field(None, pattern=r"^[A-Za-z0-9_-]{3,20}$")
    valor: float = Field(..., gt=0)
    tipo: Literal["porcentaje", "monto"] = "porcentaje"
    descripcion: str | None = Field(None, max_length=200)
    segmento_objetivo: Literal["nuevo", "ocasional", "frecuente", "dormido"] | None = None
    vence_en: str | None = None
    usos_por_cliente: int = Field(1, ge=1, le=100)
    tope_descuento: float | None = Field(None, gt=0)
    estado: Literal["borrador", "activo", "pausado", "vencido"] = "borrador"

    @field_validator("segmento_objetivo", "vence_en", mode="before")
    @classmethod
    def _vacio_es_nulo(cls, v):
        return v or None

    @field_validator("vence_en")
    @classmethod
    def _fecha_iso(cls, v: str | None) -> str | None:
        if v:
            date.fromisoformat(v)   # ValueError → 422 con mensaje claro (N9)
        return v or None

    @field_validator("valor")
    @classmethod
    def _porcentaje_razonable(cls, v: float, info) -> float:
        if info.data.get("tipo", "porcentaje") == "porcentaje" and v > 100:
            raise ValueError("Un porcentaje no puede superar 100.")
        return v


@router.post("/codigos")
def crear_codigo(body: NuevoCodigo, x_paneles_token: str | None = Header(None)):
    _verificar(x_paneles_token)
    codigo = (body.codigo or fidelizacion.generar_codigo()).upper()
    creado = fidelizacion.crear_codigo(
        codigo, body.valor, tipo=body.tipo, descripcion=body.descripcion,
        segmento_objetivo=body.segmento_objetivo, vence_en=body.vence_en,
        usos_por_cliente=body.usos_por_cliente, tope_descuento=body.tope_descuento,
        estado=body.estado,
    )
    return {"status": "success", "codigo": creado}


class CambioEstado(BaseModel):
    estado: Literal["borrador", "activo", "pausado", "vencido"]


@router.post("/codigos/{codigo}/estado")
def cambiar_estado_codigo(codigo: str, body: CambioEstado,
                          x_paneles_token: str | None = Header(None)):
    _verificar(x_paneles_token)
    fidelizacion.cambiar_estado(codigo, body.estado)
    return {"status": "success"}


class EnvioCodigo(BaseModel):
    documento: str | None = Field(None, pattern=r"^[A-Za-z0-9]{4,15}$")
    segmento: Literal["nuevo", "ocasional", "frecuente", "dormido"] | None = None
    limite: int = Field(100, ge=1, le=500)


@router.post("/codigos/{codigo}/enviar")
def enviar_codigo(codigo: str, body: EnvioCodigo,
                  x_paneles_token: str | None = Header(None)):
    _verificar(x_paneles_token)
    if body.documento:
        r = fidelizacion.enviar_codigo(codigo, body.documento)
        if not r:
            raise HTTPException(status_code=500, detail="No se pudo registrar el envío.")
        return {"status": "success" if r.get("ok") else "error", **r,
                "simulado": not fidelizacion.envio_real_activo()}
    return {"status": "success", **fidelizacion.enviar_campana(codigo, body.segmento, body.limite)}


@router.get("/codigos/validar")
def validar_codigo(
    request: Request,
    codigo: str = Query(..., min_length=1, max_length=20),
    documento: str = Query(..., pattern=r"^[A-Za-z0-9]{4,15}$"),
):
    """
    Publico a proposito: lo llama la mini-webapp antes de cobrar, no el panel
    de administracion. Documento obligatorio y limite de intentos por IP para
    que no sirva para adivinar codigos (N8).
    """
    _limite_validar.exigir(ip_cliente(request))
    return fidelizacion.validar_codigo(codigo, documento)


# ===========================================================================
#  Top compradores y bonos (solo se crean: el envio y el canje van aparte)
# ===========================================================================

@router.get("/top-compradores")
def panel_top_compradores(
    dias: int = Query(90, ge=0, le=365),   # 0 = todo el historico
    orden: Literal["boletos", "gasto"] = "boletos",
    x_paneles_token: str | None = Header(None),
):
    _verificar(x_paneles_token)
    return {"dias": dias, "orden": orden,
            "clientes": consultas.top_compradores(dias=dias, ordenar_por=orden, limite=50)}


@router.get("/bonos")
def panel_bonos(
    estado: Literal["vigente", "canjeado", "vencido", "anulado"] | None = None,
    origen: Literal["regla", "lote"] | None = None,
    pagina: int = Query(1, ge=1, le=10_000),
    x_paneles_token: str | None = Header(None),
):
    _verificar(x_paneles_token)
    datos = consultas.bonos_panel(estado=estado, origen=origen, pagina=pagina)
    for r in datos["reglas"]:
        r["descripcion"] = bonos.descripcion_regla(r)
    return datos


class ReglaBono(BaseModel):
    nombre: str = Field(..., min_length=2, max_length=60)
    disparador: Literal["viajes", "gasto", "dormido"]
    umbral: int = Field(..., gt=0, le=10_000_000)        # boletos | centimos | dias
    repetir: bool = False
    tipo: Literal["porcentaje", "monto"] = "porcentaje"
    valor: int = Field(..., gt=0, le=10_000_000)         # % entero | centimos
    tope_centimos: int | None = Field(None, gt=0, le=10_000_000)
    vigencia_dias: int = Field(30, ge=1, le=365)
    prefijo: str = Field(..., min_length=2, max_length=12, pattern=r"^[A-Za-z0-9]+$")

    @field_validator("valor")
    @classmethod
    def _porcentaje_razonable(cls, v: int, info) -> int:
        if info.data.get("tipo") == "porcentaje" and v > 100:
            raise ValueError("Un porcentaje no puede superar 100.")
        return v

    @field_validator("repetir")
    @classmethod
    def _repetir_solo_acumulados(cls, v: bool, info) -> bool:
        if v and info.data.get("disparador") == "dormido":
            raise ValueError("La regla de dormidos no se repite: da un bono por cada periodo sin comprar.")
        return v


def _errores_de_bonos():
    return _errores_de_atencion()


@router.post("/bonos/reglas")
def crear_regla(body: ReglaBono, x_paneles_token: str | None = Header(None)):
    _verificar(x_paneles_token)
    with _errores_de_bonos():
        regla_id = registro.guardar_regla(body.model_dump())
    return {"status": "success", "id": regla_id}


@router.put("/bonos/reglas/{regla_id}")
def editar_regla(regla_id: int, body: ReglaBono, x_paneles_token: str | None = Header(None)):
    _verificar(x_paneles_token)
    with _errores_de_bonos():
        registro.guardar_regla(body.model_dump(), regla_id)
    return {"status": "success", "id": regla_id}


class EstadoRegla(BaseModel):
    activa: bool


@router.post("/bonos/reglas/{regla_id}/estado")
def estado_regla(regla_id: int, body: EstadoRegla, x_paneles_token: str | None = Header(None)):
    _verificar(x_paneles_token)
    with _errores_de_bonos():
        registro.activar_regla(regla_id, body.activa)
    return {"status": "success"}


class AplicarReglas(BaseModel):
    regla_id: int | None = None
    simular: bool = True


@router.post("/bonos/reglas/aplicar")
def aplicar_reglas(body: AplicarReglas, x_paneles_token: str | None = Header(None)):
    """Con simular=True (por defecto) solo dice cuantos bonos se crearian."""
    _verificar(x_paneles_token)
    with _errores_de_bonos():
        r = registro.aplicar_reglas(regla_id=body.regla_id, simular=body.simular)
    return {"status": "success", **r}


class GrupoLote(BaseModel):
    tipo: Literal["top", "segmento", "documentos"]
    n: int = Field(50, ge=1, le=50)
    dias: Literal[0, 30, 90, 365] = 90
    orden: Literal["boletos", "gasto"] = "boletos"
    segmento: Literal["nuevo", "ocasional", "frecuente", "dormido"] | None = None
    documentos: list[str] | None = Field(None, max_length=500)

    @field_validator("documentos")
    @classmethod
    def _documentos_validos(cls, v):
        if v and any(not (4 <= len(d) <= 15 and d.isalnum()) for d in v):
            raise ValueError("Documento inválido en la lista.")
        return v


class NuevoLote(BaseModel):
    nombre: str = Field(..., min_length=2, max_length=60)
    prefijo: str = Field(..., min_length=2, max_length=12, pattern=r"^[A-Za-z0-9]+$")
    tipo: Literal["porcentaje", "monto"] = "porcentaje"
    valor: int = Field(..., gt=0, le=10_000_000)
    tope_centimos: int | None = Field(None, gt=0, le=10_000_000)
    vigencia_dias: int = Field(30, ge=1, le=365)
    grupo: GrupoLote
    simular: bool = True

    @field_validator("valor")
    @classmethod
    def _porcentaje_razonable(cls, v: int, info) -> int:
        if info.data.get("tipo") == "porcentaje" and v > 100:
            raise ValueError("Un porcentaje no puede superar 100.")
        return v


@router.post("/bonos/lotes")
def crear_lote(body: NuevoLote, x_paneles_token: str | None = Header(None)):
    """Con simular=True (por defecto) solo devuelve cuantos clientes entran."""
    _verificar(x_paneles_token)
    if body.grupo.tipo == "segmento" and not body.grupo.segmento:
        raise HTTPException(status_code=422, detail="Elige el segmento.")
    if body.grupo.tipo == "documentos" and not body.grupo.documentos:
        raise HTTPException(status_code=422, detail="Elige al menos un cliente.")
    destinatarios = consultas.destinatarios_lote(body.grupo.model_dump())
    if body.simular:
        return {"status": "success", "simulado": True, "clientes": len(destinatarios),
                "ejemplo": bonos.generar_codigo(body.prefijo)}
    with _errores_de_bonos():
        r = registro.crear_lote(
            body.nombre, destinatarios, prefijo=body.prefijo, tipo=body.tipo, valor=body.valor,
            tope_centimos=body.tope_centimos, vigencia_dias=body.vigencia_dias,
            filtro=body.grupo.model_dump(exclude={"documentos"}) | {
                "cantidad_elegida": len(body.grupo.documentos or [])},
        )
    return {"status": "success", "simulado": False, **r}


@router.post("/bonos/{codigo}/anular")
def anular_bono(codigo: str, x_paneles_token: str | None = Header(None)):
    _verificar(x_paneles_token)
    if not (3 <= len(codigo) <= 20 and all(ch.isalnum() or ch in "-_" for ch in codigo)):
        raise HTTPException(status_code=422, detail="Código inválido.")
    with _errores_de_bonos():
        registro.anular_bono(codigo)
    return {"status": "success"}


# ===========================================================================
#  Telemetria desde la mini-webapp
# ===========================================================================

# --- Administración: ciudades y pasarelas ------------------------------------- #
PASARELAS = {
    "yupy": "Yupy — QR (Yape, Plin)",
    "niubiz": "Niubiz — Tarjeta (Visa, Mastercard)",
}


class CambioActiva(BaseModel):
    activa: bool
    motivo: str | None = Field(None, max_length=200)
    # Clave de la acción (PANELES_CLAVE_CIUDADES / _PASARELAS), además de la del
    # panel: evita apagar o encender algo por un toque equivocado. Nunca se guarda.
    clave: str = Field("", max_length=200)

    @field_validator("motivo", mode="before")
    @classmethod
    def _vacio_es_nulo(cls, v):
        return (v or "").strip() or None


def _exigir_clave_admin(variable: str, clave: str, request: Request) -> None:
    """
    Clave propia de cada cambio de Administración. Sin la variable definida no
    se puede cambiar nada (falla cerrado, también en desarrollo).
    """
    _limite_admin.exigir(ip_cliente(request))
    esperada = (os.getenv(variable) or "").strip()
    if not esperada:
        raise HTTPException(status_code=503, detail=f"Falta configurar {variable} en el servidor.")
    if not clave or not hmac.compare_digest(clave.encode(), esperada.encode()):
        raise HTTPException(status_code=403, detail="Clave de autorización incorrecta.")


@router.get("/admin/ciudades")
def admin_ciudades(x_paneles_token: str | None = Header(None)):
    """Todas las ciudades del buscador, con su estado de venta."""
    _verificar(x_paneles_token)
    cerradas = consultas.ciudades_cerradas()
    en_jelaf = _ciudades_jelaf() if _ciudades_jelaf else None
    return {"status": "success", "ciudades": [
        {"codigo": c, "nombre": s["nombre"], "principal": s["principal"],
         "en_jelaf": None if en_jelaf is None else c in en_jelaf,
         "activa": c not in cerradas,
         "motivo": cerradas.get(c, {}).get("motivo"),
         "cerrada_en": cerradas.get(c, {}).get("cerrada_en"),
         "destinos": len(rutas.destinos_desde(c))}
        for c, s in rutas.SEDES.items()]}


@router.post("/admin/ciudades/{codigo}")
def admin_fijar_ciudad(codigo: str, body: CambioActiva, request: Request,
                       x_paneles_token: str | None = Header(None)):
    _verificar(x_paneles_token)
    codigo = codigo.strip().upper()
    if codigo not in rutas.SEDES:
        raise HTTPException(status_code=404, detail="Esa ciudad no existe.")
    _exigir_clave_admin("PANELES_CLAVE_CIUDADES", body.clave, request)
    registro.fijar_ciudad(codigo, body.activa, body.motivo)
    return {"status": "success", "codigo": codigo, "activa": body.activa}


@router.get("/admin/pasarelas")
def admin_pasarelas(x_paneles_token: str | None = Header(None)):
    """Yupy y Niubiz: si están en el .env del servidor y si el panel las dejó encendidas."""
    _verificar(x_paneles_token)
    apagadas = consultas.pasarelas_apagadas()
    configuradas = _pasarelas_configuradas() if _pasarelas_configuradas else {}
    return {"status": "success", "pasarelas": [
        {"medio": m, "nombre": nombre, "configurada": configuradas.get(m),
         "activa": m not in apagadas,
         "motivo": apagadas.get(m, {}).get("motivo"),
         "apagada_en": apagadas.get(m, {}).get("apagada_en")}
        for m, nombre in PASARELAS.items()]}


@router.post("/admin/pasarelas/{medio}")
def admin_fijar_pasarela(medio: Literal["yupy", "niubiz"], body: CambioActiva, request: Request,
                         x_paneles_token: str | None = Header(None)):
    _verificar(x_paneles_token)
    _exigir_clave_admin("PANELES_CLAVE_PASARELAS", body.clave, request)
    registro.fijar_pasarela(medio, body.activa, body.motivo)
    return {"status": "success", "medio": medio, "activa": body.activa}


class EventoWeb(BaseModel):
    booking_id: str = Field(..., pattern=r"^[A-Za-z0-9-]{8,64}$")
    # Unico paso que el navegador puede reportar. El resto del embudo lo
    # registra el backend; aceptarlos desde fuera permitia inflar la
    # conversion con dos POST anonimos (N7).
    paso: Literal["enlace_abierto"]


@router.post("/evento")
def evento_web(body: EventoWeb, request: Request):
    """
    La mini-webapp reporta los pasos que el backend no ve por si solo, sobre
    todo 'enlace_abierto' (el cliente toco el boton en WhatsApp y cargo la
    primera pantalla). Sin esto no hay denominador para la conversion.

    El telefono NO se acepta aqui: solo entra por el token firmado del enlace
    (P2/N7), cuando /api/v2/venta/iniciar lo verifica.
    """
    _limite_evento.exigir(ip_cliente(request))
    registro.abrir_sesion(body.booking_id)
    registro.registrar_evento(body.paso, booking_id=body.booking_id)
    return {"status": "success"}


@router.get("/salud")
def salud(x_paneles_token: str | None = Header(None)):
    _verificar(x_paneles_token)
    tablas = db.consultar("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    return {
        "base": db.RUTA_DB.name,
        "tablas": [t["name"] for t in tablas],
        "ventas": db.consultar_uno("SELECT COUNT(*) AS n FROM venta")["n"],
        "clientes": db.consultar_uno("SELECT COUNT(*) AS n FROM cliente")["n"],
        "incidencias_abiertas": db.consultar_uno(
            "SELECT COUNT(*) AS n FROM incidencia WHERE resuelta = 0")["n"],
        "envio_whatsapp_real": fidelizacion.envio_real_activo(),
        "registro_fallos": registro.fallos_registro(),
        "pool": _estado_pool() if _estado_pool else None,
    }
