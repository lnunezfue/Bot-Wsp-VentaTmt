"""
prueba_login_http.py — sonda de LOGIN por HTTP puro contra JELAF (sin navegador).

Objetivo
--------
Averiguar, de forma empírica y SIN comprometer nada, si el login de JELAF se
puede hacer con `requests` (sin Playwright). Hoy `login_http_jelaf()` abre Chrome
solo para iniciar sesión; si esta sonda confirma el flujo HTTP, ese paso se puede
reemplazar y el bot queda 100 % sin navegador.

Qué hace, en orden (todo de SOLO LECTURA / autenticación; nada que reserve):
  1. GET de la página de login  -> cookies iniciales + ¿viene `userToken` ya?
  2. GET /base/get-usuarios?value=<usuario>  -> resuelve usuario tecleado -> Codigo.
  3. POST /login/post-usuario  -> inicia sesión (Clave = JELAF_PASSWORD) y guarda
     la cookie de sesión del cajero + el `Valor` (datos del cajero).
  4. GET de la página autenticada  -> scrapea `main_globals.userToken` (el token
     que va en `Authorization: Token ...`). Es el dato que hoy se captura con el
     navegador.
  5. GET /base/obtener-fecha-sistema  -> UNA llamada de solo lectura para confirmar
     que la sesión HTTP sirve.

Lo que NUNCA hace: bloquear/liberar asientos, grabar pasajero, grabar venta,
pagar, ni ninguna acción que deje algo pendiente o comprometido.

Credenciales
-----------
Se leen del entorno (`.env`: JELAF_USUARIO y JELAF_PASSWORD) si existen; si no,
el script las PIDE al arrancar (la contraseña con getpass: no se ve ni se guarda
en disco). No se escriben en código ni se imprimen. El token tampoco se imprime:
solo se reporta su largo y si se obtuvo. El reporte va a descubrimiento_output/
(ignorado por git).

Lo ejecuta el usuario (toca el sistema real de la empresa):

    python prueba_login_http.py
"""

from __future__ import annotations

import datetime as _dt
import getpass
import json
import os
import re
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

URL_BASE = "https://moquegua.2jelaf.net.pe/pasajes"
URL_LOGIN = f"{URL_BASE}/Autenticacion"
URL_REDIRECT = f"{URL_BASE}/"           # SPA autenticada; de aquí sale userToken
SALIDA = Path("descubrimiento_output")

# --- utilidades de reporte (sin filtrar secretos ni PII) ----------------------- #
_lineas: list[str] = []


def anota(texto: str = "") -> None:
    print(texto)
    _lineas.append(texto)


def enmascarar(valor: str, visibles: int = 2) -> str:
    """Deja ver solo los últimos `visibles` caracteres: '******73'."""
    valor = str(valor or "")
    if len(valor) <= visibles:
        return "*" * len(valor)
    return "*" * (len(valor) - visibles) + valor[-visibles:]


def forma(objeto, prof: int = 0) -> str:
    """Describe la FORMA de una respuesta (claves y tipos), nunca los valores."""
    if isinstance(objeto, dict):
        items = [f"{k}: {forma(v, prof + 1)}" for k, v in list(objeto.items())[:40]]
        return "{" + ", ".join(items) + "}"
    if isinstance(objeto, list):
        return f"[{forma(objeto[0], prof + 1)} x{len(objeto)}]" if objeto else "[]"
    return type(objeto).__name__


def extraer_user_token(html: str) -> str | None:
    # Varias formas posibles: userToken: '...', userToken = "...", "userToken":"..."
    for pat in (r"userToken['\"]?\s*[:=]\s*['\"]([^'\"]+)['\"]",
                r"Token\s+([A-Za-z0-9]{6,40})['\"]"):
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return None


def claves_main_globals(html: str) -> list[str]:
    """Nombres de las variables main_globals.* en la página (sin sus valores)."""
    return sorted(set(re.findall(r"main_globals\.(\w+)", html)))


# --- sonda ---------------------------------------------------------------------- #
def main() -> int:
    usuario = (os.getenv("JELAF_USUARIO") or "").strip()
    password = (os.getenv("JELAF_PASSWORD") or "").strip()
    # Sin .env: se piden al momento. La contraseña con getpass (no se ve ni se
    # guarda); lo tipea el usuario en su terminal.
    if not usuario:
        try:
            usuario = input("Usuario JELAF: ").strip()
        except EOFError:
            usuario = ""
    if not password:
        try:
            password = getpass.getpass("Contraseña JELAF (no se muestra): ").strip()
        except EOFError:
            password = ""
    if not usuario or not password:
        anota("Sin usuario/contraseña no se puede probar el login. Aborto.")
        return 2

    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/json;charset=UTF-8",
    })

    hoy_jelaf = _dt.date.today().strftime("%d/%m/%Y")
    anota(f"Sonda de login HTTP a JELAF — {_dt.datetime.now():%Y-%m-%d %H:%M}")
    anota(f"Usuario (enmascarado): {enmascarar(usuario)}")
    anota("")

    # 1) Página de login: cookies iniciales ------------------------------------- #
    try:
        r = s.get(URL_LOGIN, timeout=30)
    except requests.RequestException as e:
        anota(f"[1] No se pudo abrir el login: {type(e).__name__}. ¿Red/VPN?")
        return 1
    tok_inicial = extraer_user_token(r.text)
    anota(f"[1] GET login -> {r.status_code}; cookies: {sorted(s.cookies.keys())}")
    anota(f"    userToken ya en la página de login: "
          f"{'sí (' + str(len(tok_inicial)) + ' chars)' if tok_inicial else 'no'}")
    if tok_inicial:
        s.headers["Authorization"] = f"Token {tok_inicial}"

    # 2) Resolver usuario -> Codigo --------------------------------------------- #
    codigo = None
    try:
        r = s.get(f"{URL_BASE}/base/get-usuarios", params={"value": usuario}, timeout=30)
        anota(f"[2] GET get-usuarios -> {r.status_code}")
        try:
            data = r.json()
            valor = data.get("Valor") if isinstance(data, dict) else data
            anota(f"    forma: {forma(data)[:300]}")
            if isinstance(valor, list):
                for u in valor:
                    etiqueta = str(u.get("label") or u.get("Nombre") or "")
                    if etiqueta.strip().upper() == usuario.upper():
                        codigo = u.get("id") or u.get("Codigo")
                        break
                if codigo is None and valor:
                    codigo = valor[0].get("id") or valor[0].get("Codigo")
            anota(f"    Codigo resuelto: {'sí (' + enmascarar(str(codigo)) + ')' if codigo else 'NO'}")
        except ValueError:
            anota("    respuesta no-JSON (puede exigir token/cookie distinta)")
    except requests.RequestException as e:
        anota(f"[2] get-usuarios falló: {type(e).__name__}")

    # 3) POST de login ----------------------------------------------------------- #
    cuerpo = {
        "Codigo": str(codigo) if codigo is not None else "",
        "Nombre": usuario,
        "Clave": password,                 # desde el entorno; no se imprime
        "Sucursal": "0", "PuntoVenta": "0",
        "NomSucursal": "", "NomPuntoVenta": "",
        "Terminal": "", "MacAddress": "",
        "FechaLiq": hoy_jelaf, "TurnoLiq": 0, "TerminalLiq": 0,
        "Panel481": False,
    }
    cajero = None
    try:
        r = s.post(f"{URL_BASE}/login/post-usuario", data=json.dumps(cuerpo), timeout=30)
        anota(f"[3] POST post-usuario -> {r.status_code}; cookies ahora: {sorted(s.cookies.keys())}")
        try:
            data = r.json()
            anota(f"    EsCorrecto: {data.get('EsCorrecto')}; "
                  f"Mensaje: {str(data.get('Mensaje') or '')[:120]}")
            anota(f"    forma de Valor: {forma(data.get('Valor'))[:400]}")
            if isinstance(data.get("Valor"), dict):
                cajero = data["Valor"]
        except ValueError:
            anota(f"    respuesta no-JSON: {r.text[:160]!r}")
    except requests.RequestException as e:
        anota(f"[3] post-usuario falló: {type(e).__name__}")

    # 4) Buscar el userToken en las páginas autenticadas ------------------------- #
    # OJO: NO fijamos Authorization todavía, para probar si el catálogo anda con
    # SOLO la cookie de sesión (pasos 5-7). Si el token hiciera falta, lo veremos.
    token = tok_inicial
    candidatas = [URL_REDIRECT, f"{URL_BASE}/login/redirect",
                  f"{URL_BASE}/Index", f"{URL_BASE}/itinerarios"]
    for url in candidatas:
        try:
            r = s.get(url, timeout=30, allow_redirects=True)
        except requests.RequestException as e:
            anota(f"[4] GET {url.split('/pasajes')[-1] or '/'} falló: {type(e).__name__}")
            continue
        tok = extraer_user_token(r.text)
        claves = claves_main_globals(r.text)
        anota(f"[4] GET {url.split('/pasajes')[-1] or '/'} -> {r.status_code}; "
              f"userToken: {'sí (' + str(len(tok)) + ' chars)' if tok else 'no'}; "
              f"main_globals: {claves or '—'}")
        if tok and not token:
            token = tok
    # Guardo una muestra (sin valores sensibles) de la última página para inspección
    try:
        muestra = re.sub(r"(userToken['\"]?\s*[:=]\s*['\"])([^'\"]+)",
                         lambda m: m.group(1) + "<REDACTADO>", r.text[:4000])
        (SALIDA / "pagina_autenticada_muestra.html").parent.mkdir(exist_ok=True)
    except Exception:
        muestra = ""

    # 5) obtener-fecha-sistema (solo lectura) — ¿anda con cookie sola? ----------- #
    tiene_token = "Authorization" in s.headers
    anota(f"    (pasos 5-7 corren {'CON' if tiene_token else 'SIN'} header Authorization)")
    try:
        r = s.get(f"{URL_BASE}/base/obtener-fecha-sistema", timeout=30)
        anota(f"[5] GET obtener-fecha-sistema -> {r.status_code}")
        try:
            anota(f"    forma: {forma(r.json())[:200]}")
        except ValueError:
            anota(f"    no-JSON: {r.text[:120]!r}")
    except requests.RequestException as e:
        anota(f"[5] obtener-fecha-sistema falló: {type(e).__name__}")

    # 6) get-empresas (solo lectura) -------------------------------------------- #
    try:
        r = s.get(f"{URL_BASE}/base/get-empresas", timeout=30)
        anota(f"[6] GET get-empresas -> {r.status_code}")
        try:
            d = r.json()
            anota(f"    EsCorrecto: {d.get('EsCorrecto')}; forma Valor: {forma(d.get('Valor'))[:200]}")
        except ValueError:
            anota(f"    no-JSON: {r.text[:120]!r}")
    except requests.RequestException as e:
        anota(f"[6] get-empresas falló: {type(e).__name__}")

    # 7) lista-itinerarios (solo lectura: busca viajes, NO reserva nada) --------- #
    fecha_fut = (_dt.date.today() + _dt.timedelta(days=7)).strftime("%d/%m/%Y")
    cat_ok = False
    try:
        r = s.post(f"{URL_BASE}/itinerarios/lista-itinerarios", data=json.dumps({
            "CodiOrigen": "2", "CodiDestino": "0", "CodiRuta": "0",   # 2 = TACNA
            "Hora": "00:00", "CodiServicio": 0, "NomDestino": "TODOS",
            "FechaViaje": fecha_fut, "SoloProgramados": False, "TodosTurnos": True,
        }), timeout=30)
        anota(f"[7] POST lista-itinerarios (TACNA, {fecha_fut}) -> {r.status_code}")
        try:
            d = r.json()
            valor = d.get("Valor") if isinstance(d, dict) else None
            n = len(valor) if isinstance(valor, list) else 0
            anota(f"    EsCorrecto: {d.get('EsCorrecto')}; viajes devueltos: {n}")
            cat_ok = bool(d.get("EsCorrecto")) and n >= 0
        except ValueError:
            anota(f"    no-JSON: {r.text[:120]!r}")
    except requests.RequestException as e:
        anota(f"[7] lista-itinerarios falló: {type(e).__name__}")

    # --- veredicto -------------------------------------------------------------- #
    anota("")
    sesion_ok = bool(cajero)
    anota("=== RESUMEN ===")
    anota(f"  Login HTTP sin navegador: {'FUNCIONA' if sesion_ok else 'NO confirmado'}")
    anota(f"  Identidad de sesión: cookie ASP.NET_SessionId "
          f"({'presente' if 'ASP.NET_SessionId' in s.cookies else 'ausente'})")
    anota(f"  userToken encontrado en la página: {'sí (' + str(len(token)) + ' chars)' if token else 'no'}")
    anota(f"  Catálogo (get-empresas/lista-itinerarios) con SOLO cookie: "
          f"{'FUNCIONA' if cat_ok else 'no confirmado'}")
    if sesion_ok and cat_ok:
        anota("  -> Login Y catálogo andan por HTTP puro con la cookie. El navegador")
        anota("     y el scraping del token se pueden eliminar del login.")
    elif sesion_ok:
        anota("  -> Login HTTP OK; falta confirmar que el catálogo anda con la cookie")
        anota("     (revisar pasos 6-7). El token quizá no sea necesario.")
    else:
        anota("  -> Login no confirmado; revisar los pasos de arriba.")

    SALIDA.mkdir(exist_ok=True)
    destino = SALIDA / "login_http.txt"
    destino.write_text("\n".join(_lineas), encoding="utf-8")
    if muestra:
        (SALIDA / "pagina_autenticada_muestra.html").write_text(muestra, encoding="utf-8")
    anota(f"\nReporte (sin secretos ni PII) en: {destino}")
    return 0 if sesion_ok else 1


if __name__ == "__main__":
    sys.exit(main())
