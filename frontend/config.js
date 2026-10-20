// Configuración compartida de la mini-webapp (M2): un solo lugar para la URL
// del backend, en vez de repetirla en cada pantalla. Para publicar, cambiar
// API_BASE_URL aquí (y agregar ese dominio a CORS_ORIGENES en el backend).
window.TMT = {
  API_BASE_URL: "https://api.transportesmoquegua.com",

  // Escapa texto antes de meterlo en innerHTML. Nombres y datos que vienen del
  // servidor o de JELAF nunca se insertan crudos (N12: XSS almacenado).
  esc(valor) {
    return String(valor == null ? '' : valor).replace(/[&<>"']/g, c => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
    ));
  },

  // Indicador de carga (todas las pantallas cargan Tailwind: animate-spin).
  // Donde JELAF o la pasarela tardan, el cliente ve que algo está pasando.
  spinner(texto, clase) {
    return `<div class="flex items-center justify-center gap-3 py-6 text-sm text-gray-400 ${clase || ''}" role="status" aria-live="polite">` +
      '<span class="inline-block w-5 h-5 shrink-0 rounded-full border-2 border-[#25d366]/30 border-t-[#25d366] animate-spin" aria-hidden="true"></span>' +
      `<span>${this.esc(texto)}</span></div>`;
  },

  // Botón ocupado: deshabilitado, con giro y texto; devuelve cómo dejarlo como estaba.
  ocupar(boton, texto) {
    const antes = boton.innerHTML;
    boton.disabled = true;
    boton.innerHTML = '<span class="inline-flex items-center justify-center gap-2">' +
      '<span class="inline-block w-4 h-4 rounded-full border-2 border-current border-t-transparent animate-spin" aria-hidden="true"></span>' +
      `<span>${this.esc(texto)}</span></span>`;
    return () => { boton.disabled = false; boton.innerHTML = antes; };
  },

  // "Hoy" en hora de Perú (AAAA-MM-DD). Antes se usaba toISOString(), que da
  // la fecha en UTC: desde las 7 pm de Lima ya era "mañana" y no se podían
  // comprar los viajes de esa misma noche.
  hoyLima() {
    return new Date().toLocaleDateString('en-CA', { timeZone: 'America/Lima' });
  },

  // Suma días a una fecha AAAA-MM-DD sin depender de la zona del celular.
  sumarDias(iso, n) {
    const d = new Date(`${iso}T12:00:00Z`);
    d.setUTCDate(d.getUTCDate() + n);
    return d.toISOString().slice(0, 10);
  }
};

// Pago en curso (N60-SN). Para pagar con Yape/Plin el cliente sale de WhatsApp
// y, al volver, WhatsApp pudo recargar la página o abrir otra pestaña: lo que
// había en sessionStorage se pierde, la compra parecía nueva y podía pagar dos
// veces (caso #147). Esta nota va en localStorage, que sobrevive a eso. Lleva
// solo lo necesario para preguntar al servidor y mostrar la ruta: SIN DNI,
// nombres ni teléfono (la compra completa sigue en sessionStorage, ver N15 abajo).
window.TMT.pagoEnCurso = {
  CLAVE: 'tmtPagoEnCurso',
  VIGENCIA_MS: 24 * 60 * 60 * 1000,
  leer() {
    try {
      const p = JSON.parse(localStorage.getItem(this.CLAVE));
      if (p && p.bookingId && Date.now() - (p.desde || 0) < this.VIGENCIA_MS) return p;
      localStorage.removeItem(this.CLAVE);
    } catch (e) { /* sin almacenamiento: la compra funciona igual, sin esta red de seguridad */ }
    return null;
  },
  guardar(datos) {
    try { localStorage.setItem(this.CLAVE, JSON.stringify({ ...datos, desde: Date.now() })); } catch (e) { /* idem */ }
  },
  // Con bookingId, solo si la nota es de esa compra.
  borrar(bookingId) {
    const p = this.leer();
    if (bookingId && (!p || p.bookingId !== bookingId)) return;
    try { localStorage.removeItem(this.CLAVE); } catch (e) { /* idem */ }
  }
};

// Al abrir la mini-webapp o volver a la pantalla de pago con un pago en curso:
// se pregunta al servidor en qué quedó (solo lectura) ANTES de dejar comprar o
// pagar de nuevo. Devuelve true si mostró un aviso o llevó a los boletos.
// "Ya pagué" no emite nada: avisa al equipo, que verifica en el portal de la pasarela.
window.TMT.revisarPagoEnCurso = async function ({ bookingId, contexto = 'regreso', enPago = false, alReclamar } = {}) {
  const T = window.TMT;
  const nota = T.pagoEnCurso.leer();
  const id = bookingId || (nota && nota.bookingId);
  if (!id) return false;
  let e;
  try {
    const r = await fetch(`${T.API_BASE_URL}/api/v2/venta/estado-pago`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ booking_id: id, contexto })
    });
    if (!r.ok) return false;
    e = await r.json();
  } catch (err) { return false; }
  const deEsta = nota && nota.bookingId === id ? nota : {};
  if (e.estado === 'sin_pago') { T.pagoEnCurso.borrar(id); return false; }
  if (e.estado === 'emitida') {
    // Ya se emitió (por ejemplo, la confirmó el servidor mientras el cliente estaba en Yape).
    try {
      let previa = {};
      try { previa = JSON.parse(sessionStorage.getItem('vibeTransitBooking')) || {}; } catch (x) { previa = {}; }
      const base = previa.bookingId === id ? previa : {};
      sessionStorage.setItem('vibeTransitBooking', JSON.stringify({
        ...base, bookingId: id,
        busqueda: base.busqueda || deEsta.busqueda || {},
        viajeSeleccionado: base.viajeSeleccionado || deEsta.viaje || {},
        boletos: e.boletos || [],
        pago: { metodo: 'CONFIRMADO', referenciaOperacion: deEsta.pedido || null,
                montoTotal: deEsta.monto || base.montoTotal || 0 }
      }));
    } catch (x) { /* sin almacenamiento: la pantalla final igual muestra lo que tenga */ }
    window.location.href = 'boletos-emitidos.html';
    return true;
  }
  T._avisoPagoEnCurso(id, e, { enPago, alReclamar });
  return true;
};

window.TMT._avisoPagoEnCurso = function (bookingId, estado, { enPago, alReclamar } = {}) {
  const T = window.TMT;
  const nodo = (tag, clase, texto) => {
    const n = document.createElement(tag);
    if (clase) n.className = clase;
    if (texto != null) n.textContent = texto;     // texto, nunca HTML
    return n;
  };
  const caja = nodo('div', 'fixed inset-0 z-[60] flex items-center justify-center bg-black/80 px-4');
  caja.setAttribute('role', 'dialog');
  caja.setAttribute('aria-modal', 'true');
  const tarjeta = nodo('div', 'w-full max-w-sm rounded-2xl bg-[#1f2c34] border border-white/10 p-5 text-white shadow-2xl');
  caja.appendChild(tarjeta);
  const boton = (texto, principal, accion) => {
    const b = nodo('button', principal
      ? 'w-full bg-[#25d366] text-[#0b141b] font-bold rounded-xl py-3 mt-3 disabled:opacity-60'
      : 'w-full border border-white/20 text-white rounded-xl py-3 mt-3', texto);
    b.type = 'button';
    b.addEventListener('click', accion);
    return b;
  };
  const cerrar = () => caja.remove();
  const compraNueva = () => {
    T.pagoEnCurso.borrar(bookingId);
    // Si esta pestaña seguía con esa compra, empieza otra (conserva el enlace de WhatsApp,
    // que /iniciar exige en producción).
    try {
      const b = JSON.parse(sessionStorage.getItem('vibeTransitBooking')) || {};
      if (b.bookingId === bookingId) {
        sessionStorage.setItem('vibeTransitBooking',
          JSON.stringify({ contactoToken: b.contactoToken || null, bookingId: crypto.randomUUID() }));
      }
    } catch (e) { /* sin almacenamiento */ }
    cerrar();
  };

  function pintar(titulo, texto, botones) {
    tarjeta.replaceChildren(nodo('h2', 'text-lg font-bold mb-2', titulo), nodo('p', 'text-sm text-gray-300', texto),
      ...botones);
  }
  // Desde la pantalla de pago siempre hay una salida (boletería, 18/10: "no puedo salir de esa zona").
  const irAlInicio = () => { window.location.href = 'buscar-viaje.html'; };
  const otraCompra = () => enPago ? boton('Volver al inicio', false, irAlInicio)
                                  : boton('Hacer otra compra', false, compraNueva);
  function verificando(texto) {
    if (alReclamar) alReclamar(texto);
    pintar('Estamos verificando tu pago', texto, [boton('Entendido', true, cerrar), otraCompra()]);
  }
  // "No pagué: salir sin pagar" (N61-SN): el servidor primero le pregunta a la
  // pasarela; si no cobró, anula el pedido y suelta los asientos.
  function confirmarSalida() {
    const error = nodo('p', 'text-xs text-red-300 mt-2 hidden', '');
    const salir = boton('Sí, salir sin pagar', false, async () => {
      salir.disabled = true;
      try {
        const r = await fetch(`${T.API_BASE_URL}/api/v2/venta/salir-sin-pagar`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ booking_id: bookingId })
        });
        const cuerpo = await r.json().catch(() => ({}));
        if (r.ok && cuerpo.estado === 'salio') {
          compraNueva();
          if (enPago) irAlInicio();
          return;
        }
        if (r.ok) {        // la pasarela sí cobró: emitida o cobrada sin boleto
          cerrar();
          return T.revisarPagoEnCurso({ bookingId, enPago, alReclamar });
        }
        error.textContent = typeof cuerpo.detail === 'string' ? cuerpo.detail
          : 'No pudimos cancelar tu compra. Inténtalo de nuevo.';
      } catch (err) {
        error.textContent = 'No pudimos conectar. Revisa tu conexión e inténtalo de nuevo.';
      }
      error.classList.remove('hidden');
      salir.disabled = false;
    });
    tarjeta.replaceChildren(
      nodo('h2', 'text-lg font-bold mb-2', '¿Salir sin pagar?'),
      nodo('p', 'text-sm text-gray-300', 'Se cancela tu compra y se liberan tus asientos. Si ya pagaste, ' +
        'no salgas: vuelve y toca «Ya pagué».'),
      error, boton('No, volver', true, inicio), salir);
  }
  function formulario() {
    const campo = nodo('input', 'w-full mt-3 rounded-lg bg-[#0b141b] border border-white/20 text-white px-3 py-3');
    campo.inputMode = 'numeric';
    campo.autocomplete = 'off';
    campo.maxLength = 30;
    campo.placeholder = 'Ej.: 00350376';
    const error = nodo('p', 'text-xs text-red-300 mt-2 hidden', '');
    const enviar = boton('Enviar aviso', true, async () => {
      const numero = campo.value.trim();
      if (!/^[A-Za-z0-9-]{4,30}$/.test(numero)) {
        error.textContent = 'Escribe el número de operación tal como aparece en tu comprobante.';
        error.classList.remove('hidden');
        return;
      }
      enviar.disabled = true;
      try {
        const r = await fetch(`${T.API_BASE_URL}/api/v2/venta/ya-pague`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ booking_id: bookingId, numero_operacion: numero })
        });
        const cuerpo = await r.json().catch(() => ({}));
        if (r.ok) return verificando(cuerpo.mensaje || 'Recibimos tu aviso. No vuelvas a pagar.');
        error.textContent = r.status === 409 ? 'No encontramos un pago en curso para esta compra. Escríbenos por WhatsApp.'
          : r.status === 429 ? 'Demasiados intentos. Espera unos minutos.'
          : 'Revisa el número de operación e inténtalo de nuevo.';
      } catch (err) {
        error.textContent = 'No pudimos enviar tu aviso. Revisa tu conexión e inténtalo de nuevo.';
      }
      error.classList.remove('hidden');
      enviar.disabled = false;
    });
    tarjeta.replaceChildren(
      nodo('h2', 'text-lg font-bold mb-2', 'Avísanos tu pago'),
      nodo('p', 'text-sm text-gray-300', 'Escribe el número de operación que aparece en el comprobante de Yape o Plin. ' +
        'Con él buscamos tu pago; tu boleto se emite cuando lo confirmemos.'),
      campo, error, enviar, boton('Volver', false, inicio));
    campo.focus();
  }
  function inicio() {
    if (estado.estado === 'cobrada_sin_emitir') {
      return pintar('Recibimos tu pago', estado.mensaje || 'Nuestro equipo te contactará. No vuelvas a pagar.',
        [boton('Entendido', true, cerrar), otraCompra()]);
    }
    if (estado.estado === 'reclamado') {
      return verificando('Ya recibimos tu aviso. Revisamos tu pago con la pasarela antes de emitir tu boleto y te ' +
        'escribiremos por WhatsApp. No vuelvas a pagar.');
    }
    const monto = Number(estado.monto_total || 0).toFixed(2);
    pintar('¿Ya pagaste con Yape o Plin?',
      `Tienes un pago en curso de S/ ${monto} (pedido ${estado.pedido || '—'}). Si ya pagaste, no vuelvas a pagar: ` +
      'avísanos y lo verificamos.',
      [boton('Ya pagué', true, formulario),
       ...(enPago ? [boton('Todavía no pagué: volver al pago', false, cerrar)] : []),
       boton(enPago ? 'No pagué: salir sin pagar' : 'No pagué: empezar una compra nueva', false, confirmarSalida)]);
  }
  inicio();
  document.body.appendChild(caja);
};

// El estado de la compra vive en sessionStorage (uno por pestaña). Antes vivía
// en localStorage, que comparten TODAS las pestañas: dos compras abiertas a la
// vez usaban el mismo bookingId y se pisaban (N15). Se borra lo que haya quedado
// de la versión anterior: además contenía datos personales de pasajeros.
try { localStorage.removeItem('vibeTransitBooking'); } catch (e) { /* sin almacenamiento */ }

// Celular (incidente 2026-10-14: "al hacer scroll la pantalla se sube un poco"):
// sin esto Android estira/rebota el contenido al llegar al final y la barra de
// WhatsApp cambia el alto de la pantalla mientras se desliza. Aquí, una sola vez
// para las siete páginas.
(function () {
  try {
    const estilo = document.createElement('style');
    estilo.textContent = 'html,body{overscroll-behavior-y:none;}';
    document.head.appendChild(estilo);
  } catch (e) { /* sin esto la página funciona igual */ }
})();

// Teclado abierto en el celular: la barra fija de abajo (.fixed-bottom: "Total a
// Pagar", "Continuar") sube pegada al teclado, tapa el campo donde se escribe y
// deja muy poco para deslizar (prueba 2026-10-15 en el resumen: 137 de 350 px
// visibles, el correo quedaba detrás de la barra). Mientras se escribe en un
// campo de texto se oculta; al salir del campo vuelve. Solo en pantallas táctiles.
(function () {
  const ESCRIBE = 'input:not([type=checkbox]):not([type=radio]):not([type=button]):not([type=submit])' +
                  ':not([type=date]):not([type=hidden]), textarea';
  try {
    const estilo = document.createElement('style');
    estilo.textContent = '@media (pointer: coarse) { html.tmt-escribiendo .fixed-bottom { display: none !important; } }';
    document.head.appendChild(estilo);
  } catch (e) { /* sin esto la página funciona igual */ }
  const escribiendo = el => !!(el && el.matches && el.matches(ESCRIBE));
  // N62-SN (boletería, 18/10): tras escribir el correo, "Continuar" no volvía.
  // En Android cerrar el teclado (botón atrás o bajar el teclado) NO quita el
  // foco del campo, y la barra quedaba oculta hasta tocar en otro lado. Ahora
  // se oculta solo si hay un campo con foco Y el teclado está abierto: la
  // pantalla visible (visualViewport) se achicó respecto de la más alta vista.
  // En el navegador de WhatsApp el teclado puede achicar también innerHeight,
  // por eso se compara con la altura máxima y no con innerHeight.
  const vv = window.visualViewport;
  let alturaMaxima = vv ? vv.height : 0;
  function tecladoAbierto() {
    if (!vv) return true;            // sin visualViewport: como antes (solo el foco)
    alturaMaxima = Math.max(alturaMaxima, vv.height);
    return vv.height < alturaMaxima - 120;
  }
  function actualizar() {
    const ocultar = escribiendo(document.activeElement) && tecladoAbierto();
    document.documentElement.classList.toggle('tmt-escribiendo', ocultar);
  }
  // Al pasar de un campo a otro no debe parpadear: se mira a dónde fue el foco.
  document.addEventListener('focusin', () => setTimeout(actualizar, 0));
  document.addEventListener('focusout', () => setTimeout(actualizar, 0));
  if (vv) vv.addEventListener('resize', actualizar);
  // Algunos navegadores dentro de apps achican la ventana entera al abrir el teclado.
  window.addEventListener('resize', actualizar);
  // Al girar el teléfono cambia la altura de referencia.
  window.addEventListener('orientationchange', () => {
    setTimeout(() => { alturaMaxima = vv ? vv.height : 0; actualizar(); }, 300);
  });
})();

// Ventanas sobre la página (términos, menor de edad, correo, progreso del pago,
// avisos): todas son ".fixed.inset-0". Mientras una está abierta, la página de
// atrás queda quieta. Prueba con toques reales (2026-10-14): con los términos
// abiertos, deslizar sobre el fondo, la cabecera o al llegar al final del texto
// movía la página de atrás ("se sube") y costaba llegar a "He leído y acepto".
// position:fixed en el body (no solo overflow:hidden) porque así también lo
// respeta Safari; al cerrar se vuelve al mismo punto.
(function () {
  let bloqueada = false;
  let y = 0;
  function hayVentanaAbierta() {
    return Array.from(document.querySelectorAll('.fixed.inset-0'))
      .some(el => !el.classList.contains('hidden') && getComputedStyle(el).display !== 'none');
  }
  function actualizar() {
    const abrir = hayVentanaAbierta();
    if (abrir === bloqueada) return;
    bloqueada = abrir;
    const s = document.body.style;
    if (abrir) {
      y = window.scrollY;
      Object.assign(s, { position: 'fixed', top: `-${y}px`, left: '0', right: '0', width: '100%' });
    } else {
      Object.assign(s, { position: '', top: '', left: '', right: '', width: '' });
      window.scrollTo(0, y);
    }
  }
  document.addEventListener('DOMContentLoaded', () => {
    try {
      new MutationObserver(actualizar).observe(document.body,
        { subtree: true, childList: true, attributes: true, attributeFilter: ['class'] });
      actualizar();
    } catch (e) { /* sin esto la página funciona igual */ }
  });
})();
