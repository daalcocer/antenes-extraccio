#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Filtra las antenas de https://antenasmoviles.es/v1/showf
para conservar solo las ubicadas dentro de Cataluña.

El propio script descarga:
1) el límite oficial de Cataluña desde ICGC (ArcGIS REST, GeoJSON, EPSG:4326)
2) los datos de /v1/showf

Salidas:
- salida_antenas/catalunya.geojson
- salida_antenas/showf_completo.json
- salida_antenas/showf_catalunya.json
- salida_antenas/Antenas.csv

Notas:
- codigo_icono = item[3] de /v1/showf
- indice_showf se conserva solo como trazabilidad
- NO se considera indice_showf como el "codigo_interno" del PDF
"""

import csv
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from shapely.geometry import Point, shape
    from shapely.ops import unary_union
except ImportError:
    print(
        "Falta la dependencia 'shapely'. Instálala con:\n"
        "  python -m pip install shapely",
        file=sys.stderr,
    )
    sys.exit(2)


SHOWF_URL = "https://antenasmoviles.es/v1/showf"

# ICGC - capa "divisions_administratives_catalunya_1000000" (Layer 24)
ICGC_QUERY_URL = (
    "https://geoserveis.icgc.cat/vector01/rest/services/"
    "divisions_administratives_wms/MapServer/24/query"
)

OUTPUT_DIR = Path("salida_antenas")


def descargar_json(url, params=None, timeout=120):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 Chrome/120 Safari/537.36"
        },
    )

    with urllib.request.urlopen(req, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        raw = response.read().decode(charset)
        return json.loads(raw)


def descargar_limite_catalunya():
    params = {
        "where": "1=1",
        "outFields": "*",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "geojson",
    }

    print("[1/5] Descargando límite oficial de Cataluña (ICGC)...")
    data = descargar_json(ICGC_QUERY_URL, params=params)

    if data.get("type") != "FeatureCollection":
        raise ValueError(
            "El ICGC no ha devuelto un FeatureCollection GeoJSON válido."
        )

    features = data.get("features", [])
    if not features:
        raise ValueError("El ICGC no ha devuelto ninguna geometría.")

    return data


def geometria_desde_geojson(data):
    geoms = [
        shape(feature["geometry"])
        for feature in data.get("features", [])
        if feature.get("geometry")
    ]

    if not geoms:
        raise ValueError("No se han encontrado geometrías válidas en el GeoJSON.")

    return unary_union(geoms)


def descargar_showf():
    print("[2/5] Descargando /v1/showf...")
    data = descargar_json(SHOWF_URL)

    if not isinstance(data, list):
        raise ValueError("La respuesta de /v1/showf no es una lista JSON.")

    return data


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Límite de Cataluña
    catalunya_geojson = descargar_limite_catalunya()
    catalunya_path = OUTPUT_DIR / "catalunya.geojson"
    with catalunya_path.open("w", encoding="utf-8") as f:
        json.dump(catalunya_geojson, f, ensure_ascii=False, indent=2)

    catalunya_geom = geometria_desde_geojson(catalunya_geojson)

    # 2. showf completo
    showf = descargar_showf()
    print(f"      Elementos recibidos: {len(showf)}")

    showf_completo_path = OUTPUT_DIR / "showf_completo.json"
    with showf_completo_path.open("w", encoding="utf-8") as f:
        json.dump(showf, f, ensure_ascii=False)

    # 3. Filtrado
    print("[3/5] Filtrando ubicaciones dentro de Cataluña...")

    showf_catalunya = []
    filas_antenas = []
    ignorados = 0

    for indice, item in enumerate(showf):
        if not isinstance(item, list) or len(item) < 4:
            ignorados += 1
            continue

        lat = item[0]
        lon = item[1]
        codigos = item[2]
        codigo_icono = item[3]

        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            ignorados += 1
            continue

        if not isinstance(codigos, list):
            ignorados += 1
            continue

        # GeoJSON EPSG:4326 -> X=longitud, Y=latitud
        punto = Point(lon, lat)

        # covers() también incluye puntos exactamente sobre el límite
        if catalunya_geom.covers(punto):
            showf_catalunya.append(item)

            # Una fila por código de antena, conservando ceros iniciales
            for codigo_antena in codigos:
                filas_antenas.append({
                    "codigo_antena": str(codigo_antena),
                    "latitud": lat,
                    "longitud": lon,
                    "codigo_icono": str(codigo_icono),
                    "indice_showf": indice,
                })

    # 4. Guardar JSON filtrado
    print("[4/5] Guardando JSON filtrado...")

    showf_cat_path = OUTPUT_DIR / "showf_catalunya.json"
    with showf_cat_path.open("w", encoding="utf-8") as f:
        json.dump(showf_catalunya, f, ensure_ascii=False, indent=2)

    # 5. Guardar CSV
    print("[5/5] Generando Antenas.csv...")

    antenas_csv_path = OUTPUT_DIR / "Antenas.csv"
    with antenas_csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "codigo_antena",
                "latitud",
                "longitud",
                "codigo_icono",
                "indice_showf",
            ],
            delimiter=";",
        )
        writer.writeheader()
        writer.writerows(filas_antenas)

    print()
    print("Proceso finalizado correctamente.")
    print(f"Ubicaciones totales /showf : {len(showf)}")
    print(f"Ubicaciones en Cataluña    : {len(showf_catalunya)}")
    print(f"Filas en Antenas.csv       : {len(filas_antenas)}")
    print(f"Items ignorados            : {ignorados}")
    print()
    print("Archivos generados:")
    print(f"  {catalunya_path}")
    print(f"  {showf_completo_path}")
    print(f"  {showf_cat_path}")
    print(f"  {antenas_csv_path}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print()
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
