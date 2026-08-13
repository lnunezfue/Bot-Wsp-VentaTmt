import os
import json
import uuid
import asyncio
import re
import random
from datetime import datetime
from typing import List
from io import BytesIO
import requests
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Response, Request, Query, BackgroundTasks
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

# --- Importación de Playwright ASÍNCRONO ---
from playwright.async_api import async_playwright

# --- Importación de ReportLab para PDF ---
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter

# ==========================================
# ⚙️ CONFIGURACIÓN Y CONSTANTES
# ==========================================
load_dotenv() # Carga las credenciales del archivo .env automáticamente

URL_BASE = "https://moquegua.2jelaf.net.pe/pasajes"
URL_ITINERARIOS = f"{URL_BASE}/itinerarios"
URL_VENTAS = f"{URL_BASE}/ventas"

CIUDADES_CODIGOS = {"TACNA": "2", "LIMA": "1", "AREQUIPA": "3", "CUSCO": "54"}
CIUDADES_POR_CODIGO = {int(v): k for k, v in CIUDADES_CODIGOS.items()}
TERMINAL_LIBERA_ASIENTO = 22

# <--- CREDENCIALES (Ahora leídas del .env) --->
JELAF_USUARIO = os.getenv("JELAF_USUARIO", "613")
JELAF_PASSWORD = os.getenv("JELAF_PASSWORD", "3976")

sesion_global_jelaf = None
PLANOS_BUSES_DIR = "planos_buses"

# ==========================================
# 📂 GESTOR DE PLANOS DE BUSES
# ==========================================
def cargar_planos_maestros():
    combinado = {"plantillas": {}, "placas": {}}
    if not os.path.isdir(PLANOS_BUSES_DIR):
        if os.path.isfile("planos_buses.json"):
            with open("planos_buses.json", "r", encoding="utf-8") as f:
                return json.load(f)
        return combinado

    for nombre in sorted(os.listdir(PLANOS_BUSES_DIR)):
        if not nombre.endswith(".json"): continue
        with open(os.path.join(PLANOS_BUSES_DIR, nombre), "r", encoding="utf-8") as f:
            datos = json.load(f)
        combinado["plantillas"].update(datos.get("plantillas", {}))
        combinado["placas"].update(datos.get("placas", {}))
    return combinado

def normalizar_placa(placa: str) -> str:
    return "".join(ch for ch in (placa or "").upper() if ch.isalnum())

def resolver_plano_por_placa(placa_bus: str):
    datos = cargar_planos_maestros()
    placa_norm = normalizar_placa(placa_bus)
    
    if "placas" in datos and "plantillas" in datos:
        placas = {normalizar_placa(k): v for k, v in datos.get("placas", {}).items()}
        codigo_plantilla = placas.get(placa_norm)
        if not codigo_plantilla: return None
        plantilla = datos.get("plantillas", {}).get(codigo_plantilla)
        if not plantilla: return None
        return plantilla.get("pisos")
    else:
        return datos.get(placa_bus)

def placas_configuradas() -> set:
    datos = cargar_planos_maestros()
    if "placas" in datos:
        return {normalizar_placa(p) for p in datos.get("placas", {}).keys()}
    return {normalizar_placa(p) for p in datos.keys()}

# ==========================================
# 🤖 RUTINA DE PLAYWRIGHT ASÍNCRONO
# ==========================================
async def obtener_sesion_jelaf():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0", 
        "Content-Type": "application/json;charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest"
    })
    
    print("🔌 Iniciando Playwright (Asíncrono) para capturar sesión...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=False)
        context = await browser.new_context(viewport={'width': 1280, 'height': 720})
        page = await context.new_page()
        page.set_default_timeout(30000)
        
        try:
            print("🌐 Navegando a JELAF...")
            await page.goto(URL_ITINERARIOS, wait_until="domcontentloaded")
            await asyncio.sleep(2)
            
            try: await page.locator("#txtUsuario").wait_for(state="visible", timeout=5000)
            except: pass
                
            if await page.locator("#txtUsuario").is_visible():
                print("🔐 Ingresando credenciales...")
                user_input = page.locator("#txtUsuario input[type='search']")
                await page.locator("#txtUsuario").click()
                await user_input.press_sequentially(JELAF_USUARIO, delay=100)
                await page.wait_for_timeout(1500)
                await user_input.press("Enter")
                await page.wait_for_timeout(1000)
                
                await page.fill("#txtContrasena", JELAF_PASSWORD)
                await page.click("#btnLogIn")
                
                try:
                    await page.locator("#txtUsuario").wait_for(state="hidden", timeout=15000)
                    print("✅ Login Exitoso.")
                except:
                    print("⚠️ Falló el login.")
                    return None
            else:
                print("✅ Sesión activa previa detectada.")
                
            cookies = await context.cookies()
            for c in cookies:
                s.cookies.set(c['name'], c['value'])
                
        except Exception as e:
            print(f"❌ Error Playwright: {e}")
            return None
        finally:
            await browser.close()
            
    return s

@asynccontextmanager
async def lifespan(app: FastAPI):
    global sesion_global_jelaf
    print("\n🚀 INICIANDO VIBE TRANSIT BACKEND")
    sesion_global_jelaf = await obtener_sesion_jelaf()
    yield 
    print("\n🛑 Apagando el backend...")

app = FastAPI(title="Vibe Transit API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)

# ==========================================
# 📦 MODELOS DE DATOS (Pydantic)
# ==========================================
class PeticionBusqueda(BaseModel):
    origen: str
    destino: str
    fecha: str 

class PeticionAsientos(BaseModel):
    codi_empresa: int
    codi_origen: int
    codi_destino: int
    codi_sucursal: int
    codi_ruta: int
    codi_punto_venta: int
    codi_servicio: int
    fecha_viaje: str
    hora_viaje: str
    placa_bus: str 

class PeticionBloqueoAsiento(BaseModel):
    codi_programacion: int
    nro_viaje: int = 0  
    codi_origen: str
    codi_destino: str
    numero_asiento: str
    fecha_viaje: str  
    precio: float = 0

class PeticionLiberaAsiento(BaseModel):
    id_bloqueo: int

class Pasajero(BaseModel):
    documento: str
    nombres: str
    paterno: str
    materno: str
    fecha_nacimiento: str
    precio_venta: float

class PeticionPreCheckout(BaseModel):
    codi_programacion: int = 0
    asientos_seleccionados: List[str] = Field(..., max_length=5)
    pasajeros: List[Pasajero] = Field(..., max_length=5)

class DatosPago(BaseModel):
    metodo: str 
    referencia_operacion: str
    monto_total: float

class PeticionCompraFinal(BaseModel):
    codi_programacion: int
    codi_empresa: int
    codi_origen: int
    codi_destino: int
    codi_sucursal: int
    codi_ruta: int
    codi_punto_venta: int
    codi_servicio: int
    fecha_viaje: str
    hora_viaje: str
    asientos_seleccionados: List[str] = Field(..., max_length=5)
    pasajeros: List[Pasajero] = Field(..., max_length=5)
    pago: DatosPago

class PeticionYupy(BaseModel):
    monto: float
    pedido: str
    nombre_comprador: str

class PeticionPDF(BaseModel):
    nro_boleto: str
    pasajero: str
    documento: str
    origen: str
    destino: str
    fecha: str
    hora: str
    asiento: str 
    precio: float

# ==========================================
# 🚀 ENDPOINTS DE LA API VENTAS
# ==========================================
@app.post("/api/v1/buscar-viajes")
def buscar_viajes(req: PeticionBusqueda):
    if not sesion_global_jelaf: raise HTTPException(status_code=500, detail="Sin sesión Jelaf")
    codi_origen = CIUDADES_CODIGOS.get(req.origen.upper())
    
    fecha_jelaf = req.fecha
    if "-" in req.fecha:
        partes = req.fecha.split("-")
        if len(partes) == 3: fecha_jelaf = f"{partes[2]}/{partes[1]}/{partes[0]}"

    payload = {
        "CodiOrigen": codi_origen, "CodiDestino": "0", "CodiRuta": "0",
        "Hora": datetime.now().strftime("%I:%M %p").lstrip("0"),
        "CodiServicio": 0, "NomDestino": "TODOS", "FechaViaje": fecha_jelaf,
        "SoloProgramados": False, "TodosTurnos": True
    }
    
    try:
        r = sesion_global_jelaf.post(f"{URL_BASE}/itinerarios/lista-itinerarios", json=payload, timeout=15)
        datos = r.json()
        viajes = datos.get("Valor")
        if not isinstance(viajes, list): viajes = []
        
        placas_con_plano = placas_configuradas()
        
        filtrados = [
            v for v in viajes 
            if isinstance(v, dict) 
            and req.destino.upper() in str(v.get("NomDestino", "")).upper()
            and normalizar_placa(v.get("PlacaBus", "")) in placas_con_plano
        ]
        
        res = []
        for v in filtrados:
            res.append({
                "id_programacion": v.get("CodiProgramacion"), 
                "nro_viaje": v.get("NroViaje", 0),
                "codi_empresa": v.get("CodiEmpresa", 1),
                "codi_origen": v.get("CodiOrigen", 0), 
                "codi_destino": v.get("CodiDestino", 0),
                "codi_sucursal": v.get("CodiSucursal", 0), 
                "codi_ruta": v.get("CodiRuta", 0),
                "codi_punto_venta": v.get("CodiPuntoVenta", 0),
                "codi_servicio": v.get("CodiServicio", 0),
                "servicio": v.get("NomServicio", "Bus Cama"),
                "fecha_partida": v.get("FechaPartida", req.fecha), 
                "hora_partida": v.get("HoraPartida") or v.get("HoraSalida", "—"),
                "placa": v.get("PlacaBus") or "Por asignar",
                "asientos_libres": v.get("AsientosLibres", 0),
                "precio_base": v.get("PrecioBase", 0)
            })
        return {"status": "success", "data": res}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/v1/plano-bus")
def obtener_plano_bus(req: PeticionAsientos):
    if not sesion_global_jelaf: raise HTTPException(status_code=500, detail="Sin sesión Jelaf")
    
    plano_base = resolver_plano_por_placa(req.placa_bus)
    if not plano_base: raise HTTPException(status_code=404, detail=f"Plano no configurado para {req.placa_bus}")

    fecha_jelaf = req.fecha_viaje
    if "-" in req.fecha_viaje:
        partes = req.fecha_viaje.split("-")
        if len(partes) == 3: fecha_jelaf = f"{partes[2]}/{partes[1]}/{partes[0]}"

    payload_jelaf = {
        "CodiEmpresa": req.codi_empresa, "CodiOrigen": req.codi_origen, "CodiDestino": req.codi_destino,
        "CodiSucursal": req.codi_sucursal, "CodiRuta": req.codi_ruta, "CodiPuntoVenta": req.codi_punto_venta,
        "CodiServicio": req.codi_servicio, "FechaViaje": fecha_jelaf, "HoraViaje": req.hora_viaje
    }
    
    try:
        r = sesion_global_jelaf.post(f"{URL_BASE}/itinerarios/turnos", json=payload_jelaf, timeout=10)
        datos_jelaf = r.json().get("Valor") or {}
        lista_asientos = datos_jelaf.get("ListaPlanoBus") or []
        
        asientos_ocupados = set()
        precio_real_jelaf = 0 
        
        for asiento in lista_asientos:
            if precio_real_jelaf == 0:
                precio_real_jelaf = asiento.get("PrecioVenta", 0) or asiento.get("PrecioNormal", 0)
                
            nombres = str(asiento.get("Nombres", "")).strip()
            doc = str(asiento.get("NumeroDocumento", "")).strip()
            flag_venta = str(asiento.get("FlagVenta", "")).strip()
            
            if nombres != "" or doc != "" or flag_venta != "":
                nume_asiento = asiento.get("NumeAsiento", 0)
                if nume_asiento != 0:
                    asientos_ocupados.add(str(nume_asiento).zfill(2))

        plano_procesado = json.loads(json.dumps(plano_base)) 
        for piso, data_piso in plano_procesado.items():
            if precio_real_jelaf > 0: data_piso["price"] = precio_real_jelaf
            for fila in data_piso["rows"]:
                for celda in fila:
                    if celda.get("type") == "seat" and celda["id"] in asientos_ocupados:
                        celda["type"] = "occupied"

        return {"status": "success", "data": plano_procesado}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/bloquear-asiento")
def bloquear_asiento(req: PeticionBloqueoAsiento):
    if not sesion_global_jelaf: raise HTTPException(status_code=500, detail="Sin sesión Jelaf")

    fecha_jelaf = req.fecha_viaje
    if "-" in req.fecha_viaje:
        anio, mes, dia = req.fecha_viaje.split("-")
        fecha_jelaf = f"{dia}/{mes}/{anio}"

    payload_jelaf = {
        "CodiProgramacion": req.codi_programacion,
        "NroViaje": req.nro_viaje,
        "CodiOrigen": req.codi_origen,
        "CodiDestino": req.codi_destino,
        "NumeAsiento": req.numero_asiento,
        "FechaProgramacion": fecha_jelaf,
        "Precio": req.precio
    }

    try:
        r = sesion_global_jelaf.post(f"{URL_BASE}/itinerarios/bloquearAsiento", json=payload_jelaf, timeout=10)
        datos = r.json()
        if not datos.get("EsCorrecto"):
            raise HTTPException(status_code=409, detail=datos.get("Mensaje", "No se pudo bloquear el asiento."))
        return {"status": "success", "id_bloqueo": datos.get("Valor")}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/liberar-asiento")
def liberar_asiento(req: PeticionLiberaAsiento):
    if not sesion_global_jelaf: raise HTTPException(status_code=500, detail="Sin sesión Jelaf")
    request_interno = json.dumps({"Terminal": TERMINAL_LIBERA_ASIENTO, "IDS": req.id_bloqueo})
    try:
        r = sesion_global_jelaf.post(f"{URL_BASE}/itinerarios/liberarAsiento", json={"request": request_interno}, timeout=10)
        datos = r.json()
        if not datos.get("EsCorrecto"):
            raise HTTPException(status_code=409, detail=datos.get("Mensaje", "No se pudo liberar el asiento."))
        return {"status": "success"}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/pre-checkout")
def pre_checkout_bloqueo(req: PeticionPreCheckout):
    if len(req.asientos_seleccionados) > 5:
        raise HTTPException(status_code=400, detail="Máximo 5 pasajes permitidos por compra.")
    if len(req.asientos_seleccionados) != len(req.pasajeros):
        raise HTTPException(status_code=400, detail="La cantidad de asientos y pasajeros no coincide.")

    monto_total = sum(p.precio_venta for p in req.pasajeros)

    return {
        "status": "success", 
        "mensaje": "Reserva temporal exitosa.",
        "monto_total": monto_total 
    }

# ==========================================
# 💳 INTEGRACIÓN OFICIAL CON YUPY (SANDBOX)
# ==========================================
@app.post("/api/v1/generar-checkout-yupi")
def generar_checkout_yupi(req: PeticionYupy):
    try:
        yupy_base_url = "https://sandbox-api.yupy.us"
        client_id = os.getenv("YUPY_CLIENT_ID", "cli_sbx_EKWeMZcR_Ougox6ZjSyfWu74")
        client_secret = os.getenv("YUPY_CLIENT_SECRET", "sec_sbx_M1PqaLzIwZbQgtAvap73MQNDbpe5ERzXHIQFchUxzR0")

        res_auth = requests.post(
            f"{yupy_base_url}/v1/auth/token",
            json={"client_id": client_id, "client_secret": client_secret},
            headers={"Content-Type": "application/json"}
        )
        if res_auth.status_code != 200: raise Exception(f"Fallo autenticación YUPY: {res_auth.text}")
        access_token = res_auth.json().get("access_token")

        llave_unica = str(uuid.uuid4())
        headers_yupy = {
            "Authorization": f"Bearer {access_token}",
            "Idempotency-Key": llave_unica,
            "Content-Type": "application/json"
        }

        payload_orden = {
            "external_transaction_id": req.pedido,
            "amount": f"{req.monto:.2f}",
            "currency": "PEN",
            "buyer_name": req.nombre_comprador,
            "integration_experience": "web_checkout"
        }

        res_orden = requests.post(f"{yupy_base_url}/v1/payment-orders", headers=headers_yupy, json=payload_orden)
        if res_orden.status_code not in [200, 201]: raise Exception(f"Error orden YUPY: {res_orden.text}")

        datos_orden = res_orden.json()
        id_transaccion = datos_orden.get("yupy_transaction_id")

        if "checkout_session" not in datos_orden:
            res_sesion = requests.post(
                f"{yupy_base_url}/v1/payment-orders/{id_transaccion}/checkout-sessions",
                headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
                json={"expires_in_seconds": 900} 
            )
            if res_sesion.status_code in [200, 201]:
                datos_orden["checkout_session"] = res_sesion.json()
                
        return datos_orden
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ==========================================
# 🚀 AUTOMATIZACIÓN DE VENTA RPA (JELAF)
# ==========================================
@app.post("/api/v1/confirmar-compra")
async def confirmar_compra(req: PeticionCompraFinal):
    boletos_generados = []
    print("🔌 Iniciando Chrome para confirmar compra...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=False)
        context = await browser.new_context(viewport={'width': 1280, 'height': 720})
        page = await context.new_page()
        page.set_default_timeout(45000)

        try:
            await page.goto(URL_ITINERARIOS, wait_until="domcontentloaded")
            try: await page.locator("#txtUsuario").wait_for(state="visible", timeout=5000)
            except: pass
                
            if await page.locator("#txtUsuario").is_visible():
                user_input = page.locator("#txtUsuario input[type='search']")
                await page.locator("#txtUsuario").click()
                await user_input.press_sequentially(JELAF_USUARIO, delay=150)
                await page.wait_for_timeout(2000)
                await user_input.press("Enter")
                await page.wait_for_timeout(1000)
                
                await page.fill("#txtContrasena", JELAF_PASSWORD)
                await page.click("#btnLogIn")
                await page.locator("#txtUsuario").wait_for(state="hidden", timeout=15000)

            await page.goto("https://moquegua.2jelaf.net.pe/pasajes/itinerarios")
            await page.wait_for_timeout(3000)

            fecha_jelaf = req.fecha_viaje
            if "-" in fecha_jelaf:
                partes = fecha_jelaf.split("-")
                fecha_jelaf = f"{partes[2]}/{partes[1]}/{partes[0]}"

            await page.locator("#txtFecha").first.fill(fecha_jelaf)

            try:
                checkbox_real = page.locator("#chckTodos").first
                await checkbox_real.wait_for(state="attached", timeout=3000)
                if not await checkbox_real.is_checked():
                    await checkbox_real.evaluate("node => node.click()")
                    await page.wait_for_timeout(500)
            except: pass

            await page.locator("#buscar").first.click()
            await page.wait_for_timeout(2000)

            try:
                btn_ok = page.locator("button").filter(has_text=re.compile(r"^OK$", re.IGNORECASE)).first
                if await btn_ok.is_visible(timeout=2000): await btn_ok.click()
            except: pass

            fila_bus = page.locator(f"#tblListaItinerarios tbody tr:has-text('{req.hora_viaje}')").first
            await fila_bus.dblclick()
            
            await page.locator("#txtNroAsientoVenta").wait_for(state="visible", timeout=10000)
            await page.wait_for_timeout(2000) 

            for i, pasajero in enumerate(req.pasajeros):
                asiento_str = str(req.asientos_seleccionados[i]).zfill(2)
                
                boton_asiento = page.locator(f"#btnAsiento_{asiento_str}").first
                await boton_asiento.wait_for(state="attached", timeout=5000)
                await boton_asiento.scroll_into_view_if_needed()
                
                try: await boton_asiento.click(force=True, timeout=1500)
                except: await boton_asiento.evaluate("node => node.click()")
                
                await page.wait_for_timeout(1000)

                await page.locator("#txtDocumento").wait_for(state="visible", timeout=5000)
                await page.fill("#txtDocumento", pasajero.documento)
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(2500) 

                nombre_cargado = await page.locator("#txtNombres").input_value()
                if not nombre_cargado.strip():
                    await page.fill("#txtNombres", pasajero.nombres)
                    await page.fill("#txtApellidoPaterno", pasajero.paterno)
                    await page.fill("#txtApellidoMaterno", pasajero.materno)

                await page.fill("#txtPrecio", str(pasajero.precio_venta))
                await page.wait_for_timeout(500)

            await page.click("#btnTipoPago")
            await page.wait_for_timeout(2000)

            tipo_pago_input = page.locator("#cboTipoPagoTP input[type='search']").first
            await tipo_pago_input.click(force=True)
            await page.wait_for_timeout(200)
            await tipo_pago_input.fill("TARJETA DE CREDITO")
            await page.wait_for_timeout(400)
            await tipo_pago_input.press("Enter")
            await page.wait_for_timeout(300)

            tipo_input = page.locator("#cboTipoTP input[type='search']").first
            await tipo_input.click(force=True)
            await page.wait_for_timeout(200)
            await tipo_input.fill("VISA")
            await page.wait_for_timeout(400)
            await tipo_input.press("Enter")
            await page.wait_for_timeout(300)

            num_input = page.locator("#txtNumeroTP").first
            random_4 = str(random.randint(1000, 9999))
            fake_card = f"0000-0000-0000-{random_4}"
            
            await num_input.click()
            await page.keyboard.press("End")
            await page.wait_for_timeout(200)
            await page.keyboard.type(fake_card, delay=50)
            await page.wait_for_timeout(300)
            await num_input.press("Enter")
            await page.wait_for_timeout(300)

            emb_input = page.locator("#cboEmbarqueTP input[type='search']").first
            await emb_input.click(force=True)
            await page.wait_for_timeout(300)
            await emb_input.press("ArrowDown")
            await emb_input.press("Enter")
            await page.wait_for_timeout(300)

            arr_input = page.locator("#cboArriboTP input[type='search']").first
            await arr_input.click(force=True)
            await page.wait_for_timeout(300)
            await arr_input.press("ArrowDown")
            await arr_input.press("Enter")
            await page.wait_for_timeout(400)
            await arr_input.press("Enter") 
            await page.wait_for_timeout(600)

            btn_aceptar_modal = page.locator("#btnSaveVentaTipoPago").first
            await btn_aceptar_modal.scroll_into_view_if_needed()
            try: await btn_aceptar_modal.click(force=True, timeout=3000)
            except: await btn_aceptar_modal.evaluate("node => node.click()")
            await page.wait_for_timeout(2000)

            try:
                popup_auth = page.locator(".swal2-popup.swal2-show").first
                if await popup_auth.is_visible(timeout=3000):
                    texto_popup = await popup_auth.inner_text()
                    clave = "1030" if "044" in texto_popup.upper() or "045" in texto_popup.upper() else "set11"
                    await page.locator(".swal2-input").first.fill(clave)
                    await popup_auth.locator(".swal2-confirm").first.click(force=True)
                    await page.wait_for_timeout(2000)
            except: pass

            btn_cerrar = page.locator("#btnCerrarVenta")
            await btn_cerrar.wait_for(state="visible", timeout=15000)

            nro_boleto_base = f"BP58-0000{datetime.now().strftime('%H%M')}"
            p_boleto = page.locator(".boletosVendidos").first
            if await p_boleto.is_visible():
                texto_popup = await p_boleto.inner_text()
                match = re.search(r"N°\s*([A-Z0-9-]+)", texto_popup)
                if match: nro_boleto_base = match.group(1)

            for i, pasajero in enumerate(req.pasajeros):
                boletos_generados.append({
                    "asiento": str(req.asientos_seleccionados[i]).zfill(2),
                    "pasajero": f"{pasajero.nombres} {pasajero.paterno}",
                    "documento": pasajero.documento,
                    "precio": pasajero.precio_venta,
                    "nro_boleto": nro_boleto_base, 
                    "estado": "EMITIDO"
                })

            await btn_cerrar.click()
            await page.wait_for_timeout(1500)

            return {
                "status": "success", 
                "mensaje": "Venta procesada correctamente.",
                "data": {"boletos": boletos_generados}
            }

        except Exception as e:
            print(f"❌ Error en Robot: {e}")
            raise HTTPException(status_code=500, detail="Fallo en la automatización: " + str(e))
        finally:
            await browser.close()


@app.post("/api/v1/descargar-boleto")
def descargar_pdf_boleto(datos: PeticionPDF):
    try:
        buffer = BytesIO()
        c = canvas.Canvas(buffer, pagesize=letter)
        
        c.setFont("Helvetica-Bold", 14)
        c.drawString(40, 750, "EMPRESA DE TRANSPORTES MOQUEGUA TURISMO S.R.L.")
        
        c.setFont("Helvetica", 10)
        c.drawString(40, 735, "R.U.C. 20534857153")
        c.drawString(40, 720, "CAL. JOSE GALVEZ NRO. 591 LIMA - LIMA - LA VICTORIA")
        
        c.setFont("Helvetica-Bold", 12)
        c.drawString(40, 690, "BOLETA DE VENTA ELECTRÓNICA")
        c.drawString(40, 675, str(datos.nro_boleto))
        
        c.setFont("Helvetica", 10)
        c.drawString(40, 640, f"Fecha de emisión : {datetime.now().strftime('%d/%m/%Y')}    Hora : {datetime.now().strftime('%H:%M:%S')}")
        c.drawString(40, 625, f"Doc. Identidad   : {datos.documento}")
        c.drawString(40, 610, f"Cliente          : {str(datos.pasajero).upper()}")
        c.drawString(40, 580, f"EMBARQUE         : TERMINAL TERRESTRE {str(datos.origen).upper()}")
        
        c.setFont("Helvetica-Bold", 10)
        c.drawString(40, 540, "DESCRIPCION")
        c.drawString(450, 540, "TOTAL")
        c.line(40, 535, 500, 535)
        
        c.setFont("Helvetica", 9)
        c.drawString(40, 520, f"POR EL SERVICIO DE TRANSPORTE DE LA RUTA {str(datos.origen).upper()} - {str(datos.destino).upper()}")
        c.drawString(40, 505, f"NRO ASIENTO: {str(datos.asiento).zfill(2)} / PASAJERO: {str(datos.pasajero).upper()}")
        c.drawString(40, 490, f"FECHA VIAJE: {datos.fecha} / HORA VIAJE: {datos.hora}")
        c.drawString(450, 520, f"S/ {datos.precio:.2f}")
        
        c.line(40, 470, 500, 470)
        c.drawString(380, 450, "Op. Gravada    : S/ 0.00")
        c.drawString(380, 435, f"Op. Exonerada  : S/ {datos.precio:.2f}")
        c.drawString(380, 420, "I.G.V. (18%)   : S/ 0.00")
        c.setFont("Helvetica-Bold", 10)
        c.drawString(380, 400, f"Importe Total  : S/ {datos.precio:.2f}")
        
        c.setFont("Helvetica", 7)
        terminos = [
            "MAPFRE SEGUROS N° 3022600086975",
            "Adquirido el boleto de viaje no hay devolucion/embarcar 30 minutos antes, con su pasaje y DNI en fisico",
            "/menor de edad que viaja sin padres debe presentar autorizacion notarial/a partir de los 5 años a mas pagan",
            "boleto de viaje/la hora de embarque en escalas comerciales es referencial/EL PASAJERO no podra embarcar en",
            "estado de drogas o etilico/peso max. de equipaje 20kg. se paga el excedente/Solo se considera equipaje maletas",
            "El plazo maximo de postergacion es de 4 horas antes"
        ]
        y_pos = 350
        for linea in terminos:
            c.drawString(40, y_pos, linea)
            y_pos -= 10
            
        c.showPage()
        c.save()
        
        pdf_bytes = buffer.getvalue()
        buffer.close()
        
        return Response(
            content=pdf_bytes, 
            media_type="application/pdf", 
            headers={"Content-Disposition": f"attachment; filename={datos.nro_boleto}.pdf"}
        )
    except Exception as e:
        print(f"❌ Error al generar PDF: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ==========================================
# 🟢 MOTOR DE WHATSAPP (WEBHOOKS META)
# ==========================================
def enviar_boton_compra(numero_destino: str):
    """ Función que dispara la tarjeta nativa interactiva hacia WhatsApp """
    token = os.getenv("WHATSAPP_TOKEN")
    phone_id = os.getenv("PHONE_NUMBER_ID")
    
    # ⚠️ MUY IMPORTANTE: Cambia esta URL por el enlace real donde tienes alojado tu index.html
    url_frontend = "https://transportesmoquegua.com/beta/buscar%20viaje/1-2-corregir.html"
    
    url_api = f"https://graph.facebook.com/v19.0/{phone_id}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
    # Este formato (cta_url) le indica al celular que debe abrir el enlace dentro de WhatsApp
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": numero_destino,
        "type": "interactive",
        "interactive": {
            "type": "cta_url",
            "header": {
                "type": "text",
                "text": "🚌 Transportes Moquegua"
            },
            "body": {
                "text": "¡Hola! Inicia tu compra de pasajes de forma rápida y segura aquí. 👇"
            },
            "footer": {
                "text": "Boletería Virtual"
            },
            "action": {
                "name": "cta_url",
                "parameters": {
                    "display_text": "Comprar Pasaje",
                    "url": url_frontend
                }
            }
        }
    }
    
    try:
        res = requests.post(url_api, headers=headers, json=payload)
        print("📤 CTA Enviado a", numero_destino, "| Status:", res.status_code)
    except Exception as e:
        print("❌ Error enviando CTA:", e)


@app.get("/api/v1/webhook")
async def verificar_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge")
):
    """ Verificación oficial de seguridad que exige Meta """
    verify_token_esperado = os.getenv("META_VERIFY_TOKEN", "MoqueguaBot2026")
    
    if hub_mode == "subscribe" and hub_verify_token == verify_token_esperado:
        print("✅ Webhook validado por Meta.")
        return Response(content=hub_challenge, media_type="text/plain")
    raise HTTPException(status_code=403, detail="Error de autenticación")


@app.post("/api/v1/webhook")
async def recibir_mensajes(request: Request, background_tasks: BackgroundTasks):
    """ El cerebro que lee todos los mensajes que recibe la línea de la empresa """
    try:
        body = await request.json()
        
        # Navegar por el JSON gigante que envía Meta
        if "entry" in body:
            for entry in body["entry"]:
                if "changes" in entry:
                    for change in entry["changes"]:
                        value = change.get("value", {})
                        if "messages" in value:
                            for msg in value["messages"]:
                                # Extraemos el número del cliente
                                numero_cliente = msg["from"]
                                
                                # Verificamos si el mensaje es de texto puro
                                if msg.get("type") == "text":
                                    texto_recibido = msg["text"]["body"].lower()
                                    print(f"📩 Mensaje recibido de {numero_cliente}: {texto_recibido}")
                                    
                                    # 🔥 EL DISPARADOR MÁGICO
                                    if "comprar" in texto_recibido:
                                        # Usamos background_tasks para que FastAPI responda rápido a Meta
                                        # y luego ejecute el envío de la tarjeta de compra.
                                        background_tasks.add_task(enviar_boton_compra, numero_cliente)
                                        
    except Exception as e:
        print(f"⚠️ Error procesando webhook: {e}")
        
    # Siempre debemos responder 200 OK a Meta para que no bloquee el Webhook
    return Response(content="EVENT_RECEIVED", status_code=200)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)