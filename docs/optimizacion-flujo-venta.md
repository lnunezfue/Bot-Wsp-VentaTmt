# Optimización del flujo de venta (asiento, pasajeros, pago, concurrencia)

Continuación de [`optimizacion-buscar-viaje.md`](optimizacion-buscar-viaje.md). **Estos cambios NO
están medidos en vivo todavía**: los tiempos "antes" salen de sumar las esperas fijas del código y
los "después" son objetivos. Cada paso del worker ahora imprime `⏱️ [worker] ...` para medirlos.

## Cambios

| Paso | Antes | Ahora |
|---|---|---|
| Tocar asiento | 2 s fijos | sondeo del estado de Vue cada 100 ms (+ espera de la pestaña con 2+ asientos) |
| Quitar asiento | 1 s fijo | 0.5 s |
| Pasajero | 2.5 s fijos tras el DNI + varios sleeps | espera de la respuesta `consultar-usuario` (techo 2.5 s) |
| Pasajeros en el frontend | todos al pulsar *Continuar*, en serie | cada uno al completar su tarjeta, en segundo plano; *Continuar* solo envía los que faltan o cambiaron |
| Pago | 3 s + combos + 2 s + popup + 1.5 s, **después** de cobrar | el modal de JELAF se deja lleno mientras el cliente paga en Yupy (`POST /api/v2/venta/preparar-pago`); al aprobarse solo falta *Aceptar* |
| Espera tras *Aceptar* | 2 s fijos + 3 s de popup | sondeo cada 100 ms de botón de cierre / popup de autorización |
| Combos del modal | escribir + Enter | clic en la opción exacta (ver `_elegir_opcion`) |
| Abandono | worker suelto a los 10 min | 5 min sin heartbeat en selección/datos; 10 min una vez en fase de pago |
| Todos los workers ocupados | rechazo inmediato (423) | cola de hasta 15 s (`ESPERA_MOSTRADOR_SEGUNDOS`) antes de rechazar |
| Worker libre | sesión JELAF podía expirar sin que nadie lo notara | keepalive cada 4 min |

`seleccionar-asiento.html` ahora también manda heartbeat (antes no lo hacía).

## Variables de entorno nuevas (opcionales)

- `JELAF_WORKERS_POR_CUENTA` (por defecto `1`): abre N navegadores con la misma cuenta. **Validar
  antes de subirlo**: 2 ventas a la vez, asientos distintos del mismo bus, y comprobar que JELAF no
  cierra la sesión de uno al entrar el otro ni muestra los asientos de uno como "míos" en el otro.
- `JELAF_HEADLESS` (por defecto `0`): `1` abre Chrome sin ventana.

## Riesgos a validar en la primera prueba real

1. `_cerrar_modal_pago_si_abierto` usa el botón *Cancelar* del modal "Tipo de Pago"; hay que
   confirmar que solo cierra el modal y no cancela la venta.
2. Los selectores de la lista de opciones (`.select2-results__option, [role='option'], li`) son
   suposición; si no coinciden se cae al `Enter` de antes.
3. La tecla a 15 ms/tecla en `#txtNumeroTP`: si la máscara pierde teclas, repite a 50 ms.
