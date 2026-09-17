#!/usr/bin/env python3
"""
fetch_municipal_data.py
Script para descargar y estructurar los datos geoespaciales oficiales de la
Municipalidad de Asunción desde sus servicios públicos ArcGIS Server.

Genera:
  - bus-tracker-web/data/asuncion_stops.json
  - bus-tracker-web/data/asuncion_traffic_lights.json
  - bus-tracker-web/data/asuncion_pois.json
"""

import os
import re
import json
import ssl
import urllib.request
from typing import List, Dict, Any

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "data")
os.makedirs(DATA_DIR, exist_ok=True)

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) BusTracker/1.0"
}


def fetch_json(url: str, timeout: int = 30) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, context=SSL_CTX, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def identify_layer(base_url: str, layer_id: int) -> List[Dict[str, Any]]:
    """Extrae elementos usando identify con bounding box de todo Asuncion."""
    url = (
        f"{base_url}/identify?"
        f"geometry=-57.6,-25.3&geometryType=esriGeometryPoint&sr=4326"
        f"&layers=all:{layer_id}&tolerance=50000"
        f"&mapExtent=-57.75,-25.45,-57.45,-25.15"
        f"&imageDisplay=1000,1000,96&returnGeometry=true&f=pjson"
    )
    data = fetch_json(url)
    return data.get("results", [])


def clean_stop_name(raw_name: str) -> str:
    # Remover numeración inicial como "1. ", "14. "
    name = re.sub(r"^\d+\.\s*", "", raw_name.strip())
    # Normalizar espacios
    name = re.sub(r"\s+", " ", name)
    return name


def fetch_stops() -> List[Dict[str, Any]]:
    print("-> Descargando paradas y refugios oficiales...")
    base_url = "https://www.asuncion.gov.py/arcgis/rest/services/Mapa_Web/Movilidad_Urbana/MapServer"
    
    # 1. Refugios sustentables (Layer 4)
    raw_refugios = identify_layer(base_url, 4)
    print(f"   Refugios encontrados (Layer 4): {len(raw_refugios)}")
    
    stops = []
    seen = set()
    
    for idx, item in enumerate(raw_refugios):
        geom = item.get("geometry", {})
        lon = geom.get("x")
        lat = geom.get("y")
        raw_name = item.get("value") or item.get("attributes", {}).get("Name", "")
        if not lat or not lon or not raw_name:
            continue
            
        name = clean_stop_name(raw_name)
        key = (round(lat, 5), round(lon, 5))
        if key in seen:
            continue
        seen.add(key)
        
        stops.append({
            "id": f"refugio-{idx + 1}",
            "name": name,
            "raw_name": raw_name,
            "type": "refugio",
            "lat": round(float(lat), 6),
            "lon": round(float(lon), 6)
        })
        
    # 2. Terminales y paradas de líneas (Layer 1)
    raw_terminales = identify_layer(base_url, 1)
    print(f"   Terminales encontradas (Layer 1): {len(raw_terminales)}")
    for idx, item in enumerate(raw_terminales):
        geom = item.get("geometry", {})
        lon = geom.get("x")
        lat = geom.get("y")
        name = item.get("value") or item.get("attributes", {}).get("PARADAS", "")
        if not lat or not lon or not name:
            continue
            
        key = (round(lat, 5), round(lon, 5))
        if key in seen:
            continue
        seen.add(key)
        
        stops.append({
            "id": f"terminal-{idx + 1}",
            "name": clean_stop_name(name),
            "raw_name": name,
            "type": "terminal",
            "lat": round(float(lat), 6),
            "lon": round(float(lon), 6)
        })

    print(f"   Total paradas/refugios consolidados: {len(stops)}")
    return stops


def fetch_traffic_lights() -> List[Dict[str, Any]]:
    print("-> Descargando semáforos...")
    base_url = "https://www.asuncion.gov.py/arcgis/rest/services/Mapa_Web/Movilidad_Urbana/MapServer"
    
    lights = []
    seen = set()
    
    # Layer 3: ATMS Inteligentes
    raw_atms = identify_layer(base_url, 3)
    print(f"   Semáforos ATMS (Layer 3): {len(raw_atms)}")
    for idx, item in enumerate(raw_atms):
        geom = item.get("geometry", {})
        lon = geom.get("x")
        lat = geom.get("y")
        val = item.get("value") or item.get("attributes", {}).get("Name", "")
        if not lat or not lon:
            continue
            
        # Parsear código TSCS si existe
        m = re.match(r"^(l?TSCS-\d+)\s*(.*)$", val, re.IGNORECASE)
        code = m.group(1).upper() if m else ""
        name = m.group(2).strip() if m else val.strip()
        if not name:
            name = val.strip()
            
        key = (round(lat, 5), round(lon, 5))
        if key in seen:
            continue
        seen.add(key)
        
        lights.append({
            "id": f"tl-atms-{idx + 1}",
            "name": name,
            "code": code,
            "type": "atms",
            "lat": round(float(lat), 6),
            "lon": round(float(lon), 6)
        })
        
    # Layer 2: Descentralizados
    raw_desc = identify_layer(base_url, 2)
    print(f"   Semáforos Descentralizados (Layer 2): {len(raw_desc)}")
    for idx, item in enumerate(raw_desc):
        geom = item.get("geometry", {})
        lon = geom.get("x")
        lat = geom.get("y")
        val = item.get("value") or item.get("attributes", {}).get("Name", "")
        if not lat or not lon:
            continue
            
        key = (round(lat, 5), round(lon, 5))
        if key in seen:
            continue
        seen.add(key)
        
        lights.append({
            "id": f"tl-desc-{idx + 1}",
            "name": f"Semáforo Descentralizado {val}".strip(),
            "code": str(val).strip(),
            "type": "descentralizado",
            "lat": round(float(lat), 6),
            "lon": round(float(lon), 6)
        })
        
    print(f"   Total semáforos consolidados: {len(lights)}")
    return lights


def categorize_poi(name: str) -> str:
    n = name.upper()
    if any(k in n for k in ["HOSP", "SANAT", "CLIN", "SALUD", "CRUZ ROJA", "MATERNO", "EMERG"]):
        return "Salud"
    if any(k in n for k in ["COL.", "ESC.", "UNIV", "FACULTAD", "INSTITUTO", "COLEGIO", "ESCUELA"]):
        return "Educación"
    if any(k in n for k in ["SHOPPING", "MALL", "SUPER", "MERCADO", "GALERIA", "PLAZA"]):
        return "Comercial"
    if any(k in n for k in ["CLUB", "ESTADIO", "CANCHA", "PARQUE", "POLIDEPORTIVO"]):
        return "Deporte/Recreación"
    if any(k in n for k in ["MINISTERIO", "MUNICIP", "SECRETARIA", "JUZGADO", "POLICIA", "COMISARIA", "CORTE"]):
        return "Gobierno"
    if any(k in n for k in ["TERMINAL", "AEROPUERTO", "PUERTO"]):
        return "Transporte"
    return "Lugar de Interés"


def fetch_pois() -> List[Dict[str, Any]]:
    print("-> Descargando Puntos de Interés (Lugares de Asunción)...")
    base_url = "https://www.asuncion.gov.py/arcgis/rest/services/Mapa_Web/Lugares/MapServer"
    
    # Layer 13: LUGARES DE ASUNCION_ESC_8000/4000
    raw_lugares = identify_layer(base_url, 13)
    print(f"   Lugares encontrados (Layer 13): {len(raw_lugares)}")
    
    pois = []
    seen = set()
    
    for idx, item in enumerate(raw_lugares):
        geom = item.get("geometry", {})
        lon = geom.get("x")
        lat = geom.get("y")
        raw_name = item.get("value") or item.get("attributes", {}).get("NOMBRE", "")
        if not lat or not lon or not raw_name:
            continue
            
        clean_name = re.sub(r"\s+", " ", raw_name).strip()
        if len(clean_name) < 3:
            continue
            
        key = (clean_name.upper(), round(lat, 4), round(lon, 4))
        if key in seen:
            continue
        seen.add(key)
        
        pois.append({
            "id": f"poi-{idx + 1}",
            "name": clean_name,
            "category": categorize_poi(clean_name),
            "lat": round(float(lat), 6),
            "lon": round(float(lon), 6)
        })
        
    print(f"   Total POIs consolidados: {len(pois)}")
    return pois


def main():
    print("=== EXTRACCIÓN DE DATOS MUNICIPALES DE ASUNCIÓN ===")
    
    # 1. Paradas y Refugios
    stops = fetch_stops()
    stops_file = os.path.join(DATA_DIR, "asuncion_stops.json")
    with open(stops_file, "w", encoding="utf-8") as f:
        json.dump(stops, f, ensure_ascii=False, indent=2)
    print(f"-> Guardado: {stops_file} ({len(stops)} paradas)")

    # 2. Semáforos
    lights = fetch_traffic_lights()
    lights_file = os.path.join(DATA_DIR, "asuncion_traffic_lights.json")
    with open(lights_file, "w", encoding="utf-8") as f:
        json.dump(lights, f, ensure_ascii=False, indent=2)
    print(f"-> Guardado: {lights_file} ({len(lights)} semáforos)")

    # 3. POIs
    pois = fetch_pois()
    pois_file = os.path.join(DATA_DIR, "asuncion_pois.json")
    with open(pois_file, "w", encoding="utf-8") as f:
        json.dump(pois, f, ensure_ascii=False, indent=2)
    print(f"-> Guardado: {pois_file} ({len(pois)} POIs)")
    
    print("\n¡Extracción completada con éxito!")


if __name__ == "__main__":
    main()
