"""
Reglas PURAS de la atencion de incidencias: que acciones tiene cada tipo de
problema y que datos exige cada una para quedar registrada como solucion.

No toca la base; registro.registrar_atencion() las aplica antes de escribir.
"""

from __future__ import annotations

# accion → (resultado, etiqueta para el panel)
ACCIONES: dict[str, tuple[str, str]] = {
    "contacto": ("en_seguimiento", "Se contactó al cliente"),
    "ofrecio_asiento": ("en_seguimiento", "Se le ofreció otro asiento o viaje"),
    "nota": ("en_seguimiento", "Nota de seguimiento"),
    "compro_otro": ("resuelta", "Compró otro asiento o viaje"),
    "desistio": ("resuelta", "El cliente desistió de comprar"),
    "emitio_manual": ("resuelta", "Boleto emitido a mano en JELAF"),
    "devolucion": ("resuelta", "Devolución del pago"),
    "sin_accion": ("resuelta", "No requiere acción"),
    "otra": ("resuelta", "Otra solución"),
    "derivar": ("derivada", "Derivada"),
}

# El cliente pago (o pudo pagar): no se cierra sin dejar resuelto el dinero.
TIPOS_CON_PAGO = {"cobrado_sin_emitir", "por_conciliar", "pago_no_verificado",
                  "fallo_emision", "emision_conciliada", "pago_sin_confirmar",
                  "niubiz_aviso_descartado", "pago_reclamado"}

# No se sabe si hubo cobro: si el portal de la pasarela dice que no, no hay nada que hacer.
TIPOS_COBRO_DUDOSO = {"pago_no_verificado", "pago_sin_confirmar", "niubiz_aviso_descartado", "pago_reclamado"}

# El cliente no llego a pagar: se le ofrece otra opcion o desiste.
TIPOS_SIN_PAGO = {"asiento_no_disponible", "pool_ocupado", "sesion_liberada", "pago_rechazado"}

_COMUNES = ["contacto", "nota", "derivar", "otra"]


def acciones_para(tipo: str, severidad: str) -> list[str]:
    """Acciones permitidas para una incidencia, en el orden en que se ofrecen."""
    if tipo in TIPOS_CON_PAGO or severidad == "critica":
        acciones = ["emitio_manual", "compro_otro", "devolucion"] + _COMUNES
        if tipo in TIPOS_COBRO_DUDOSO:
            acciones.append("sin_accion")
        return acciones
    if tipo in TIPOS_SIN_PAGO:
        return ["ofrecio_asiento", "compro_otro", "desistio", "sin_accion"] + _COMUNES
    return ["sin_accion"] + _COMUNES


def validar(accion: str, tipo: str, severidad: str, datos: dict) -> str:
    """
    Comprueba que la accion aplica y trae lo que necesita. Devuelve el
    resultado (en_seguimiento | resuelta | derivada) o lanza ValueError con un
    mensaje para el panel.
    """
    if accion not in ACCIONES:
        raise ValueError("Acción desconocida.")
    if accion not in acciones_para(tipo, severidad):
        raise ValueError("Esa acción no aplica a este tipo de incidencia.")
    resultado = ACCIONES[accion][0]
    nota = (datos.get("nota") or "").strip()

    if accion == "emitio_manual" and not datos.get("nro_boleto"):
        raise ValueError("Escribe el número del boleto emitido en JELAF.")
    if accion == "compro_otro" and not (datos.get("nro_boleto") or datos.get("booking_nuevo")):
        raise ValueError("Escribe el número de boleto o el booking de la compra nueva.")
    if accion == "devolucion" and not (datos.get("referencia_devolucion") and datos.get("monto_devuelto_centimos")):
        raise ValueError("Escribe la referencia de la devolución en Yupy y el monto devuelto.")
    if accion == "derivar" and not datos.get("derivada_a"):
        raise ValueError("Indica a quién se deriva.")
    if accion in ("nota", "otra", "derivar") and not nota:
        raise ValueError("Escribe una nota con lo que se hizo.")
    # Un cobro sin boleto no se cierra con una frase suelta: queda el detalle.
    if severidad == "critica" and resultado != "en_seguimiento" and not nota:
        raise ValueError("En una incidencia crítica la nota es obligatoria.")
    return resultado


def resumen(accion: str, datos: dict) -> str:
    """Texto que queda en incidencia.nota_resolucion (compatibilidad con reportes viejos)."""
    partes = [ACCIONES[accion][1]]
    if datos.get("nro_boleto"):
        partes.append(f"boleto {datos['nro_boleto']}")
    if datos.get("booking_nuevo"):
        partes.append(f"compra {str(datos['booking_nuevo'])[:8]}")
    if datos.get("referencia_devolucion"):
        partes.append(f"devolución {datos['referencia_devolucion']}")
    if datos.get("derivada_a"):
        partes.append(f"→ {datos['derivada_a']}")
    texto = " · ".join(partes)
    nota = (datos.get("nota") or "").strip()
    return f"{texto}. {nota}" if nota else texto
