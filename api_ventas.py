import os
import json
import asyncio
from datetime import datetime
from typing import List
import requests
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware
from fpdf import FPDF

# --- Importación de Playwright ASÍNCRONO ---
from playwright.async_api import async_playwright

# ==========================================
# ⚙️ CONFIGURACIÓN Y CONSTANTES
# ==========================================
URL_BASE = "https://moquegua.2jelaf.net.pe/pasajes"
URL_ITINERARIOS = f"{URL_BASE}/itinerarios"
URL_VENTAS = f"{URL_BASE}/ventas" # <--- Ruta base para la facturación
CIUDADES_CODIGOS = {"TACNA": "2", "LIMA": "1", "AREQUIPA": "3", "CUSCO": "54"}
CIUDADES_POR_CODIGO = {int(v): k for k, v in CIUDADES_CODIGOS.items()}

# Visto en una petición real de liberarAsiento (no es CodiPuntoVenta ni
# CodiSucursal, es un campo aparte que pide ese endpoint). Si liberar
# asientos falla para otra ruta/sucursal, este es el primer sospechoso.
TERMINAL_LIBERA_ASIENTO = 22

BOLETOS_DIR = "boletos_generados"
os.makedirs(BOLETOS_DIR, exist_ok=True)

JELAF_USUARIO = "613"       # <--- COLOCA TU USUARIO
JELAF_PASSWORD = "3976"     # <--- COLOCA TU CONTRASEÑA

sesion_global_jelaf = None

PLANOS_BUSES_DIR = "planos_buses"

def cargar_planos_maestros():
    """
    La carpeta planos_buses/ tiene un .json por servicio (ejecutivo,
    emperador, emp_vip, premium, golden_suite, emp_plus), cada uno con
    la forma:
      { "plantillas": { "<codigo>": {"servicio":.., "pisos": {...}}, ... },
        "placas": { "<PLACA>": "<codigo>", ... } }
    Varias placas comparten la misma plantilla (mismo bus físico), así
    que el plano no se guarda repetido por placa. Esta función junta
    todos los archivos de la carpeta en un solo diccionario en memoria.
    """
    combinado = {"plantillas": {}, "placas": {}}
    if not os.path.isdir(PLANOS_BUSES_DIR):
        return combinado

    for nombre in sorted(os.listdir(PLANOS_BUSES_DIR)):
        if not nombre.endswith(".json"):
            continue
        with open(os.path.join(PLANOS_BUSES_DIR, nombre), "r", encoding="utf-8") as f:
            datos = json.load(f)
        combinado["plantillas"].update(datos.get("plantillas", {}))
        combinado["placas"].update(datos.get("placas", {}))

    return combinado

def normalizar_placa(placa: str) -> str:
    return "".join(ch for ch in (placa or "").upper() if ch.isalnum())

def resolver_plano_por_placa(placa_bus: str):
    """Devuelve los pisos ({"1": {...}, "2": {...}}) de la plantilla
    asignada a esa placa, o None si la placa no está configurada."""
    datos = cargar_planos_maestros()
    placa_norm = normalizar_placa(placa_bus)
    placas = {normalizar_placa(k): v for k, v in datos.get("placas", {}).items()}
    codigo_plantilla = placas.get(placa_norm)
    if not codigo_plantilla:
        return None
    plantilla = datos.get("plantillas", {}).get(codigo_plantilla)
    if not plantilla:
        return None
    return plantilla.get("pisos")

def placas_configuradas() -> set:
    datos = cargar_planos_maestros()
    return {normalizar_placa(p) for p in datos.get("placas", {}).keys()}

# ==========================================
# 🤖 RUTINA DE PLAYWRIGHT ASÍNCRONO
# ==========================================
async def obtener_sesion_jelaf():
    """
    Abre Playwright de forma asíncrona, inicia sesión, extrae las cookies y 
    las guarda en requests. Luego cierra el navegador.
    """
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0", 
        "Content-Type": "application/json;charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest"
    })
    
    print("🔌 Iniciando Playwright (Asíncrono) para capturar sesión...")
    async with async_playwright() as p:
        # headless=False para que lo veas abrirse
        browser = await p.chromium.launch(channel="chrome", headless=False)
        context = await browser.new_context(viewport={'width': 1280, 'height': 720})
        page = await context.new_page()
        page.set_default_timeout(30000)
        
        try:
            print("🌐 Navegando a JELAF...")
            await page.goto(URL_ITINERARIOS, wait_until="domcontentloaded")
            await asyncio.sleep(2)
            
            try:
                await page.locator("#txtUsuario").wait_for(state="visible", timeout=5000)
            except:
                pass
                
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
                
                print("⏳ Validando inicio de sesión...")
                try:
                    await page.locator("#txtUsuario").wait_for(state="hidden", timeout=15000)
                    print("✅ Login Exitoso.")
                except Exception:
                    print("⚠️ Falló el login. Credenciales incorrectas o red lenta.")
                    return None
            else:
                print("✅ Sesión activa previa detectada.")
                
            print("🍪 Extrayendo cookies de seguridad...")
            cookies = await context.cookies()
            for c in cookies:
                s.cookies.set(c['name'], c['value'])
                
            print("🛑 Cerrando navegador visual, sesión transferida a la API (Modo Turbo)...")
            
        except Exception as e:
            print(f"❌ Error durante la captura con Playwright: {e}")
            return None
        finally:
            await browser.close()
            
    return s


# ==========================================
# 🔌 CICLO DE VIDA DE FASTAPI
# ==========================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    global sesion_global_jelaf
    print("\n╔═════════════════════════════════════════════════╗")
    print("║ 🚀 INICIANDO VIBE TRANSIT BACKEND (Playwright)  ║")
    print("╚═════════════════════════════════════════════════╝")
    
    if not JELAF_USUARIO or not JELAF_PASSWORD:
        print("⚠️  ADVERTENCIA: Credenciales de JELAF no configuradas en el código.")
    
    sesion_global_jelaf = await obtener_sesion_jelaf()
    
    if sesion_global_jelaf:
        print("✅ El puente asíncrono con JELAF está operando en tiempo real.")
    else:
        print("❌ No se pudo establecer la sesión con JELAF.")
        
    yield 
    print("\n🛑 Apagando el backend...")

app = FastAPI(title="Vibe Transit - API Conector Jelaf", lifespan=lifespan)

# Configurar CORS 
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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
    nro_viaje: int
    codi_origen: str
    codi_destino: str
    numero_asiento: str
    fecha_viaje: str  # AAAA-MM-DD (mismo formato que el resto de la API)
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

class DatosPago(BaseModel):
    metodo: str 
    referencia_operacion: str
    monto_total: float

class PeticionCompraFinal(BaseModel):
    # Identificadores del Viaje
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
    
    # Detalle de la Venta
    asientos_seleccionados: List[str]
    pasajeros: List[Pasajero]
    pago: DatosPago

class PeticionPreCheckout(BaseModel):
    asientos_seleccionados: List[str]
    pasajeros: List[Pasajero]


# ==========================================
# 🎟️ GENERACIÓN DE BOLETA EN PDF (simulación)
# ==========================================
def generar_boleta_pdf(nro_boleto: str, pasajero: Pasajero, asiento: str, req: PeticionCompraFinal) -> str:
    """
    Arma un PDF de boleto tipo ticket (80mm, como una impresora térmica de
    terminal) con los datos de la venta. Es una simulación: no depende de
    JELAF, solo usa los datos que ya llegaron en la petición.
    """
    origen_nombre = CIUDADES_POR_CODIGO.get(req.codi_origen, f"COD-{req.codi_origen}")
    destino_nombre = CIUDADES_POR_CODIGO.get(req.codi_destino, f"COD-{req.codi_destino}")

    pdf = FPDF(unit="mm", format=(80, 150))
    pdf.add_page()
    pdf.set_margins(5, 5, 5)
    pdf.set_auto_page_break(False)

    pdf.set_font("Courier", "B", 12)
    pdf.multi_cell(0, 6, "VIBE TRANSIT\nBOLETO DE VIAJE (SIMULADO)", align="C")
    pdf.ln(2)
    pdf.set_font("Courier", "", 8)
    pdf.cell(0, 4, "-" * 40, ln=True)

    pdf.set_font("Courier", "B", 10)
    pdf.cell(0, 6, f"N Boleto: {nro_boleto}", ln=True)
    pdf.set_font("Courier", "", 9)
    pdf.cell(0, 5, "Estado: EMITIDO", ln=True)
    pdf.ln(2)

    pdf.set_font("Courier", "B", 10)
    pdf.cell(0, 5, f"{origen_nombre} -> {destino_nombre}", ln=True)
    pdf.set_font("Courier", "", 9)
    pdf.cell(0, 5, f"Fecha: {req.fecha_viaje}   Hora: {req.hora_viaje}", ln=True)
    pdf.ln(2)

    pdf.cell(0, 4, "-" * 40, ln=True)
    pdf.set_font("Courier", "B", 9)
    pdf.cell(0, 5, f"Pasajero: {pasajero.nombres} {pasajero.paterno} {pasajero.materno}".strip(), ln=True)
    pdf.set_font("Courier", "", 9)
    pdf.cell(0, 5, f"Documento: {pasajero.documento}", ln=True)
    pdf.cell(0, 5, f"Asiento: {str(asiento).zfill(2)}", ln=True)
    pdf.cell(0, 5, f"Precio: S/ {pasajero.precio_venta:.2f}", ln=True)
    pdf.ln(2)

    pdf.cell(0, 4, "-" * 40, ln=True)
    pdf.set_font("Courier", "", 8)
    pdf.cell(0, 5, f"Ref. pago: {req.pago.referencia_operacion}", ln=True)
    pdf.cell(0, 5, f"Metodo: {req.pago.metodo}", ln=True)
    pdf.ln(3)
    pdf.set_font("Courier", "I", 7)
    pdf.multi_cell(0, 4, "Este boleto es una simulacion generada\npor el backend de Vibe Transit.", align="C")

    ruta_pdf = os.path.join(BOLETOS_DIR, f"{nro_boleto}.pdf")
    pdf.output(ruta_pdf)
    return ruta_pdf


# ==========================================
# 🚀 ENDPOINTS DE LA API
# ==========================================

@app.post("/api/v1/buscar-viajes")
def buscar_viajes(req: PeticionBusqueda):
    if not sesion_global_jelaf:
        raise HTTPException(status_code=500, detail="Sesión con Jelaf no inicializada")

    codi_origen = CIUDADES_CODIGOS.get(req.origen.upper())
    if not codi_origen:
        raise HTTPException(status_code=400, detail="Ciudad de origen no válida")

    fecha_jelaf = req.fecha
    if "-" in req.fecha:
        partes = req.fecha.split("-")
        if len(partes) == 3:
            anio, mes, dia = partes
            fecha_jelaf = f"{dia}/{mes}/{anio}"

    payload = {
        "CodiOrigen": codi_origen,
        "CodiDestino": "0", 
        "CodiRuta": "0",
        "Hora": datetime.now().strftime("%I:%M %p").lstrip("0"),
        "CodiServicio": 0,
        "NomDestino": "TODOS", 
        "FechaViaje": fecha_jelaf, # <--- Enviamos la fecha ya procesada a JELAF
        "SoloProgramados": False,
        "TodosTurnos": True
    }

    try:
        # --- MODO DEPURACIÓN: Veremos qué fecha está llegando ---
        print(f"\n🔎 BUSCANDO VIAJES: {req.origen} -> {req.destino} | Fecha recibida: {req.fecha}")
        
        r = sesion_global_jelaf.post(f"{URL_BASE}/itinerarios/lista-itinerarios", json=payload, timeout=15)
        if "autenticacion" in r.url.lower():
            raise HTTPException(status_code=401, detail="La sesión con Jelaf ha expirado.")

        datos = r.json()
        print("📥 RESPUESTA JELAF:", datos) # Imprimimos lo que devuelve el servidor
        
        viajes_encontrados = datos.get("Valor")
        
        # 🛡️ BLINDAJE EXTREMO: Si JELAF devuelve null (None), texto, o cualquier cosa rara, lo forzamos a lista vacía
        if not isinstance(viajes_encontrados, list):
            viajes_encontrados = []

        # Solo se muestran turnos cuya placa ya tiene un plano de asientos
        # configurado en la carpeta planos_buses/ (evita elegir un bus y toparse
        # con un 404 al llegar a "Elige tu asiento").
        placas_con_plano = placas_configuradas()

        viajes_filtrados = [
            v for v in viajes_encontrados
            if isinstance(v, dict)
            and req.destino.upper() in str(v.get("NomDestino", "")).upper()
            and normalizar_placa(v.get("PlacaBus", "")) in placas_con_plano
        ]

        respuesta_frontend = []
        for v in viajes_filtrados:
            respuesta_frontend.append({
                "id_programacion": v.get("CodiProgramacion"),
                "nro_viaje": v.get("NroViaje", 0),
                "codi_empresa": v.get("CodiEmpresa", 1),
                "codi_origen": v.get("CodiOrigen", 0),
                "codi_destino": v.get("CodiDestino", 0),
                "codi_sucursal": v.get("CodiSucursal", 0),
                "codi_ruta": v.get("CodiRuta", 0),
                "codi_punto_venta": v.get("CodiPuntoVenta", 0),
                "codi_servicio": v.get("CodiServicio", 0),
                "hora_partida": v.get("HoraPartida") or v.get("HoraProgramacion"),
                "precio_base": v.get("PrecioBase", 0), 
                "servicio": v.get("NomServicio", "Bus Cama"),
                "placa": v.get("PlacaBus", "Por asignar")
            })

        return {"status": "success", "data": respuesta_frontend}

    except Exception as e:
        print(f"❌ ERROR INTERNO: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/v1/plano-bus")
def obtener_plano_bus(req: PeticionAsientos):
    if not sesion_global_jelaf:
        raise HTTPException(status_code=500, detail="Sesión con Jelaf no inicializada")

    plano_base = resolver_plano_por_placa(req.placa_bus)
    if not plano_base:
        raise HTTPException(status_code=404, detail=f"Plano no configurado para la placa {req.placa_bus}")

    # Igual que en buscar-viajes: el frontend manda la fecha en ISO
    # (AAAA-MM-DD), pero JELAF espera DD/MM/AAAA. Sin esta conversión,
    # /itinerarios/turnos no encuentra el turno y siempre devuelve la
    # lista de ocupados vacía (por eso no se marcaba ningún asiento).
    fecha_jelaf = req.fecha_viaje
    if "-" in req.fecha_viaje:
        anio, mes, dia = req.fecha_viaje.split("-")
        fecha_jelaf = f"{dia}/{mes}/{anio}"

    payload_jelaf = {
        "CodiEmpresa": req.codi_empresa,
        "CodiOrigen": req.codi_origen,
        "CodiDestino": req.codi_destino,
        "CodiSucursal": req.codi_sucursal,
        "CodiRuta": req.codi_ruta,
        "CodiPuntoVenta": req.codi_punto_venta,
        "CodiServicio": req.codi_servicio,
        "FechaViaje": fecha_jelaf,
        "HoraViaje": req.hora_viaje
    }

    try:
        r = sesion_global_jelaf.post(f"{URL_BASE}/itinerarios/turnos", json=payload_jelaf, timeout=10)
        # Igual que en buscar-viajes: si JELAF no tiene data para este
        # turno, "Valor" viene explícito como null (no ausente), así que
        # ".get('Valor', {})" no alcanza -> hay que forzar el default.
        datos_jelaf = r.json().get("Valor") or {}

        lista_asientos = datos_jelaf.get("ListaPlanoBus", [])
        asientos_ocupados = set()
        
        if lista_asientos:
            for asiento in lista_asientos:
                nombres = str(asiento.get("Nombres", "")).strip()
                doc = str(asiento.get("NumeroDocumento", "")).strip()
                flag_venta = str(asiento.get("FlagVenta", "")).strip()
                
                if nombres != "" or doc != "" or flag_venta != "":
                    nume_asiento = asiento.get("NumeAsiento", 0)
                    if nume_asiento != 0:
                        num_asiento = str(nume_asiento).zfill(2)
                        asientos_ocupados.add(num_asiento)

        plano_procesado = json.loads(json.dumps(plano_base)) 

        for piso, data_piso in plano_procesado.items():
            for fila in data_piso["rows"]:
                for celda in fila:
                    if celda.get("type") == "seat":
                        if celda["id"] in asientos_ocupados:
                            celda["type"] = "occupied"

        return {"status": "success", "data": plano_procesado}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/v1/bloquear-asiento")
def bloquear_asiento(req: PeticionBloqueoAsiento):
    """
    Bloquea un asiento en JELAF para que nadie más pueda elegirlo
    mientras este cliente llena sus datos. Se debe llamar apenas el
    usuario toca un asiento libre (no esperar a confirmar la compra).
    Devuelve un id_bloqueo que hay que guardar: se necesita para
    liberar el asiento si el usuario lo deselecciona.
    """
    if not sesion_global_jelaf:
        raise HTTPException(status_code=500, detail="Sesión con Jelaf no inicializada")

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
            raise HTTPException(status_code=409, detail=datos.get("Mensaje", "No se pudo bloquear el asiento (puede que ya lo haya tomado otro cliente)."))
        return {"status": "success", "id_bloqueo": datos.get("Valor")}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/v1/liberar-asiento")
def liberar_asiento(req: PeticionLiberaAsiento):
    """
    Libera un asiento previamente bloqueado (el usuario lo deselecciona,
    se cae del flujo, cambia de asiento, etc.). JELAF espera el body en
    un formato particular: un campo "request" cuyo valor es el propio
    JSON codificado como texto (no un objeto anidado normal).
    """
    if not sesion_global_jelaf:
        raise HTTPException(status_code=500, detail="Sesión con Jelaf no inicializada")

    request_interno = json.dumps({"Terminal": TERMINAL_LIBERA_ASIENTO, "IDS": req.id_bloqueo})

    try:
        r = sesion_global_jelaf.post(
            f"{URL_BASE}/itinerarios/liberarAsiento",
            json={"request": request_interno},
            timeout=10
        )
        datos = r.json()
        if not datos.get("EsCorrecto"):
            raise HTTPException(status_code=409, detail=datos.get("Mensaje", "No se pudo liberar el asiento."))
        return {"status": "success"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/v1/pre-checkout")
def pre_checkout_bloqueo(req: PeticionPreCheckout):
    if len(req.asientos_seleccionados) > 5:
        raise HTTPException(status_code=400, detail="Máximo 5 pasajes permitidos por compra.")
    
    if len(req.asientos_seleccionados) != len(req.pasajeros):
        raise HTTPException(status_code=400, detail="La cantidad de asientos y pasajeros no coincide.")

    return {
        "status": "success", 
        "mensaje": "Reserva temporal exitosa.",
        "monto_total": 60.00 * len(req.asientos_seleccionados) 
    }


# ==========================================
# 🎯 NUEVO ENDPOINT: GENERACIÓN DE BOLETO MASIVO
# ==========================================
@app.post("/api/v1/confirmar-compra")
def confirmar_compra(req: PeticionCompraFinal):
    # Este endpoint es 100% simulado (la inyección real a JELAF sigue
    # comentada más abajo), así que no depende de sesion_global_jelaf.
    if len(req.asientos_seleccionados) != len(req.pasajeros):
        raise HTTPException(status_code=400, detail="Descuadre entre asientos y pasajeros.")

    print(f"\nProcesando pago validado ({req.pago.metodo})...")
    print(f"Preparando inyección de {len(req.pasajeros)} boletos al servidor JELAF...")

    # 1. Armamos el payload masivo tal como lo hacías en el bot de tickets.
    # Usamos las variables base del viaje que nos envió el frontend.
    payload_jelaf = {
        "CodiProgramacion": req.codi_programacion,
        "CodiEmpresa": req.codi_empresa,
        "CodiOrigen": req.codi_origen,
        "CodiDestino": req.codi_destino,
        "CodiSucursal": req.codi_sucursal,
        "CodiRuta": req.codi_ruta,
        "CodiPuntoVenta": req.codi_punto_venta,
        "CodiServicio": req.codi_servicio,
        "FechaViaje": req.fecha_viaje,
        "HoraViaje": req.hora_viaje,
        "TipoPago": "01", 
        "ListaAsientos": []
    }

    # 2. Iteramos para inyectar cada pasajero en el arreglo de ventas
    for i, pasajero in enumerate(req.pasajeros):
        asiento_nro = int(req.asientos_seleccionados[i])
        
        # Mapeamos a la estructura estricta que vimos en el JSON de respuesta de JELAF
        payload_jelaf["ListaAsientos"].append({
            "NumeAsiento": asiento_nro,
            "Nombres": pasajero.nombres,
            "ApellidoPaterno": pasajero.paterno,
            "ApellidoMaterno": pasajero.materno,
            "NumeroDocumento": pasajero.documento,
            "TipoDocumento": "01", # 01 suele ser DNI, adaptar si es Pasaporte
            "PrecioVenta": pasajero.precio_venta,
            "Edad": 0, 
            "Sexo": "M", # Valor por defecto, se puede expandir el modelo Pydantic
            "Telefono": "",
            "FlagVenta": "VI" # Venta por Internet o el flag que exija el sistema
        })

    try:
        # 3. Disparamos la petición POST para grabar en la base de datos central
        # NOTA: Cambia "/grabar-venta" por el endpoint real que descubras con F12
        URL_GRABAR = f"{URL_VENTAS}/grabar-venta" 
        
        # r = sesion_global_jelaf.post(URL_GRABAR, json=payload_jelaf, timeout=20)
        # respuesta_jelaf = r.json()
        
        # if respuesta_jelaf.get("Error"):
        #     raise Exception(respuesta_jelaf.get("Mensaje", "Error desconocido de JELAF"))

        # --- SIMULADOR DE RESPUESTA EXITOSA ---
        boletos_generados = []
        for i, asiento in enumerate(payload_jelaf["ListaAsientos"]):
            nro_boleto = f"BP43-0000{7100 + i}"
            generar_boleta_pdf(nro_boleto, req.pasajeros[i], asiento["NumeAsiento"], req)

            boletos_generados.append({
                "asiento": str(asiento["NumeAsiento"]).zfill(2),
                "pasajero": f"{asiento['Nombres']} {asiento['ApellidoPaterno']}",
                "documento": asiento["NumeroDocumento"],
                "nro_boleto": nro_boleto,
                "estado": "EMITIDO",
                "boleta_url": f"/api/v1/boleto/{nro_boleto}"
            })

        print(f"{len(boletos_generados)} boleta(s) PDF generada(s) en '{BOLETOS_DIR}/'.")

        return {
            "status": "success",
            "mensaje": "Venta procesada y boletos emitidos correctamente.",
            "data": {
                "referencia_pago": req.pago.referencia_operacion,
                "boletos": boletos_generados
            }
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error durante la emisión masiva de tickets: {str(e)}")


@app.get("/api/v1/boleto/{nro_boleto}")
def descargar_boleto(nro_boleto: str):
    ruta_pdf = os.path.join(BOLETOS_DIR, f"{nro_boleto}.pdf")
    if not os.path.isfile(ruta_pdf):
        raise HTTPException(status_code=404, detail="Boleto no encontrado.")
    return FileResponse(ruta_pdf, media_type="application/pdf", filename=f"{nro_boleto}.pdf")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)