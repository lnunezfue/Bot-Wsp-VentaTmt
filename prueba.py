import os
import requests
from dotenv import load_dotenv

load_dotenv()

def enviar_mensaje_plantilla(numero_destino: str):
    token = os.getenv("WHATSAPP_TOKEN")
    phone_number_id = os.getenv("PHONE_NUMBER_ID")
    
    url = f"https://graph.facebook.com/v25.0/{phone_number_id}/messages"
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    
    payload = {
        "messaging_product": "whatsapp",
        "to": "51958627120",  # Ejemplo: "51958627120"
        "type": "template",
        "template": {
            "name": "3p_direct_integration_test_template",
            "language": { "code": "en_US" }
        }
    }
    
    response = requests.post(url, headers=headers, json=payload)
    
    print("Status Code:", response.status_code)
    print("Respuesta de Meta:", response.text)
    
    return response.json()