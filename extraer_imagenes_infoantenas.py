#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Extractor de imágenes de antenasmoviles para los Gis_ID de Infoantenas.

FASE 1 (offline): no hace peticiones HTTP, no descarga imágenes y no genera
miniaturas. Valida la lógica de entrada, el parser de /v1/markers y el cruce
usando solo ficheros locales.

Entradas:
  infoantenas/01_DADES_FINALS/inventari_infoantenas.csv
  salida_markers/markers_raw.jsonl   (respuestas ya guardadas de /v1/markers)

Salidas (por defecto en salida_imagenes_infoantenas_test/):
  CONTROL_GIS_ID.csv         una fila por Gis_ID oficial
  IMATGES_INFOANTENAS.csv    una fila por relación Gis_ID <-> imagen
  resum_offline.json         recuentos y estadísticas

Reglas:
- Gis_ID siempre como texto literal (sin quitar ceros ni normalizar).
- El cruce es solo por coincidencia literal de código.
- No se define ningún umbral de distancia ni un estado de match definitivo.
- El operador de Infoantenas se deduce del prefijo de properties_Gis_Codigo
  ("OPERADOR - GIS_ID"); la comparación con /markers es por igualdad exacta.
"""

import argparse
import csv
import hashlib
import json
import math
import os
import sys
import unicodedata
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

IMAGE_PREFIX = "https://antenasmoviles.es/archive/antenas/"

DEFAULT_INVENTARIO = "infoantenas/01_DADES_FINALS/inventari_infoantenas.csv"
DEFAULT_RAW = "salida_markers/markers_raw.jsonl"
DEFAULT_OUTPUT_DIR = "salida_imagenes_infoantenas_test"

RADIO_TIERRA_M = 6371008.8
SEPARADOR_OBSERVACIONES = "|"
MAX_ELEMENTOS_LISTA_RESUMEN = 100

COLUMNA_GIS_ID = "properties_Gis_ID"
COLUMNA_GIS_CODIGO = "properties_Gis_Codigo"
COLUMNA_LATITUD = "geometry_latitud"
COLUMNA_LONGITUD = "geometry_longitud"
COLUMNAS_INVENTARIO_OBLIGATORIAS = (
    COLUMNA_GIS_ID,
    COLUMNA_GIS_CODIGO,
    COLUMNA_LATITUD,
    COLUMNA_LONGITUD,
)

CAMPOS_IMAGEN = (
    "id",
    "fichero",
    "autor",
    "fecha_foto",
    "fecha_subida",
    "old",
    "desmantelada",
    "lat_subida",
    "lon_subida",
    "asociadas",
)

CAMPOS_CONTROL = [
    "GIS_ID",
    "ENCONTRADO_ANTENASMOVILES",
    "CODIGO_ANTENASMOVILES",
    "OPERADOR_ANTENASMOVILES",
    "OPERADOR_COINCIDE",
    "DISTANCIA_M",
    "NUM_IMAGENES",
    "OBSERVACIONES",
]

CAMPOS_IMAGENES = [
    "GIS_ID",
    "CODIGO_ANTENASMOVILES",
    "ID_IMAGEN_ORIGEN",
    "NOMBRE_FICHERO_ORIGEN",
    "AUTOR",
    "FECHA_FOTO",
    "FECHA_SUBIDA",
    "URL_ORIGEN",
    "OLD",
    "DESMANTELADA",
    "LAT_SUBIDA",
    "LON_SUBIDA",
    "OPERADOR_COINCIDE",
    "DISTANCIA_M",
]


# ---------------------------------------------------------------------------
# Utilidades puras
# ---------------------------------------------------------------------------

def a_texto(valor):
    return "" if valor is None else str(valor)


def a_float(valor):
    """Convierte a float; devuelve None si no es un número finito."""
    if valor is None or isinstance(valor, bool):
        return None

    try:
        numero = float(str(valor).strip()) if isinstance(valor, str) else float(valor)
    except (TypeError, ValueError):
        return None

    return numero if math.isfinite(numero) else None


def distancia_haversine_m(lat1, lon1, lat2, lon2):
    """Distancia en metros entre dos puntos (grados decimales)."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )

    return 2 * RADIO_TIERRA_M * math.asin(math.sqrt(a))


def percentil(valores_ordenados, p):
    """Percentil p (0-100) con interpolación lineal sobre una lista ordenada."""
    if not valores_ordenados:
        return None

    posicion = (len(valores_ordenados) - 1) * p / 100.0
    inferior = math.floor(posicion)
    superior = math.ceil(posicion)

    if inferior == superior:
        return valores_ordenados[inferior]

    fraccion = posicion - inferior
    return (
        valores_ordenados[inferior] * (1 - fraccion)
        + valores_ordenados[superior] * fraccion
    )


def estadisticas_distancia(distancias):
    ordenadas = sorted(distancias)

    return {
        "n": len(ordenadas),
        "min": ordenadas[0] if ordenadas else None,
        "mediana": percentil(ordenadas, 50),
        "p99": percentil(ordenadas, 99),
        "max": ordenadas[-1] if ordenadas else None,
    }


def extraer_operador(gis_codigo, gis_id):
    """
    Deduce el operador de properties_Gis_Codigo con formato "OPERADOR - GIS_ID".
    Devuelve None si el valor no sigue ese formato para este Gis_ID.
    """
    sufijo = " - " + gis_id

    if gis_codigo.endswith(sufijo) and len(gis_codigo) > len(sufijo):
        return gis_codigo[: -len(sufijo)]

    return None


# ---------------------------------------------------------------------------
# Lectura del inventario de Infoantenas
# ---------------------------------------------------------------------------

def leer_inventario(path: Path):
    """
    Lee el inventario y agrupa las filas por Gis_ID (texto literal).

    Devuelve (inventario, estadisticas) donde inventario es un OrderedDict:
      gis_id -> {
          "filas": int,
          "operadores": [str, ...]       distintos, en orden de aparición,
          "coordenadas": [(lat, lon)]    distintas, de la geometría del GeoJSON,
          "coordenadas_invalidas": int,
      }
    """
    inventario = OrderedDict()
    estadisticas = {
        "filas": 0,
        "filas_sin_gis_id": 0,
        "filas_operador_no_deducible": 0,
    }

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=";")
        faltan = [
            c for c in COLUMNAS_INVENTARIO_OBLIGATORIAS
            if c not in (reader.fieldnames or [])
        ]

        if faltan:
            raise ValueError(
                "Faltan columnas en el inventario: " + ", ".join(faltan)
            )

        for numero_fila, fila in enumerate(reader, start=2):
            estadisticas["filas"] += 1
            gis_id = (fila.get(COLUMNA_GIS_ID) or "").strip()

            if not gis_id:
                estadisticas["filas_sin_gis_id"] += 1
                continue

            if any(unicodedata.category(c).startswith("C") for c in gis_id):
                raise ValueError(
                    f"Gis_ID con caracteres de control en la fila "
                    f"{numero_fila}: {gis_id!r}"
                )

            entrada = inventario.get(gis_id)
            if entrada is None:
                entrada = {
                    "filas": 0,
                    "operadores": [],
                    "coordenadas": [],
                    "coordenadas_invalidas": 0,
                }
                inventario[gis_id] = entrada

            entrada["filas"] += 1

            operador = extraer_operador(fila.get(COLUMNA_GIS_CODIGO) or "", gis_id)
            if operador is None:
                estadisticas["filas_operador_no_deducible"] += 1
            elif operador not in entrada["operadores"]:
                entrada["operadores"].append(operador)

            lat = a_float(fila.get(COLUMNA_LATITUD))
            lon = a_float(fila.get(COLUMNA_LONGITUD))
            if lat is None or lon is None:
                entrada["coordenadas_invalidas"] += 1
            elif (lat, lon) not in entrada["coordenadas"]:
                entrada["coordenadas"].append((lat, lon))

    return inventario, estadisticas


# ---------------------------------------------------------------------------
# Parser de /v1/markers (función pura, única)
# ---------------------------------------------------------------------------

def _lista_en(elemento, indice):
    if len(elemento) > indice and isinstance(elemento[indice], list):
        return elemento[indice]
    return []


def _parsear_imagen(imagen):
    if isinstance(imagen, dict):
        return {campo: a_texto(imagen.get(campo)) for campo in CAMPOS_IMAGEN}

    resultado = {campo: "" for campo in CAMPOS_IMAGEN}
    resultado["fichero"] = a_texto(imagen)
    return resultado


def parsear_marker(data):
    """
    Interpreta la respuesta de /v1/markers?id=<codigo>.

    Estructura confirmada en markers_raw.jsonl: lista de elementos; cada uno:
      [0] [codigo]
      [1] [empresa, codigo_operador, <3er valor sin uso>]
      [2] [direccion, lat, lon, altura]
      [3] frecuencias (no se usan aquí)
      [4] imágenes: dict con id, fichero, autor, fecha_foto, fecha_subida,
          old, desmantelada, lat_subida, lon_subida, asociadas

    Devuelve una lista de dicts, uno por elemento. Las imágenes sin fichero se
    descartan y se cuentan en "imagenes_descartadas". Lanza ValueError si la
    respuesta no es una lista.
    """
    if not isinstance(data, list):
        raise ValueError("La respuesta de /markers no es una lista.")

    elementos = []

    for elemento in data:
        if not isinstance(elemento, list):
            continue

        codigos = _lista_en(elemento, 0)
        operador = _lista_en(elemento, 1)
        geo = _lista_en(elemento, 2)

        imagenes = []
        descartadas = 0

        for imagen in _lista_en(elemento, 4):
            parseada = _parsear_imagen(imagen)

            if parseada["fichero"]:
                imagenes.append(parseada)
            else:
                descartadas += 1

        elementos.append({
            "codigo": a_texto(codigos[0]) if codigos else "",
            "empresa": a_texto(operador[0]) if len(operador) > 0 else "",
            "codigo_operador": a_texto(operador[1]) if len(operador) > 1 else "",
            "direccion": a_texto(geo[0]) if len(geo) > 0 else "",
            "latitud": a_float(geo[1]) if len(geo) > 1 else None,
            "longitud": a_float(geo[2]) if len(geo) > 2 else None,
            "altura": a_texto(geo[3]) if len(geo) > 3 else "",
            "imagenes": imagenes,
            "imagenes_descartadas": descartadas,
        })

    return elementos


def leer_markers_raw(path: Path):
    """
    Lee markers_raw.jsonl ({codigo_antena, http_status, data} por línea).

    Devuelve (markers, estadisticas). markers: codigo_solicitado -> {
    "http_status", "elementos"}. Si un código aparece varias veces gana la
    última respuesta válida y se cuenta como duplicado.
    """
    markers = {}
    estadisticas = {
        "lineas": 0,
        "respuestas_validas": 0,
        "lineas_invalidas": 0,
        "lineas_invalidas_detalle": [],
        "codigos_duplicados": 0,
    }

    with path.open("r", encoding="utf-8") as f:
        for numero_linea, linea in enumerate(f, start=1):
            linea = linea.strip()

            if not linea:
                continue

            estadisticas["lineas"] += 1

            try:
                registro = json.loads(linea)
                codigo = a_texto(registro.get("codigo_antena")).strip()

                if not codigo:
                    raise ValueError("Falta codigo_antena.")

                elementos = parsear_marker(registro.get("data"))

            except (ValueError, AttributeError) as exc:
                estadisticas["lineas_invalidas"] += 1

                if len(estadisticas["lineas_invalidas_detalle"]) < MAX_ELEMENTOS_LISTA_RESUMEN:
                    estadisticas["lineas_invalidas_detalle"].append(
                        {"linea": numero_linea, "error": str(exc)}
                    )
                continue

            if codigo in markers:
                estadisticas["codigos_duplicados"] += 1

            markers[codigo] = {
                "http_status": registro.get("http_status"),
                "elementos": elementos,
            }
            estadisticas["respuestas_validas"] += 1

    return markers, estadisticas


# ---------------------------------------------------------------------------
# Cruce Infoantenas <-> antenasmoviles
# ---------------------------------------------------------------------------

def comparar_elemento(entrada_inventario, elemento):
    """
    Devuelve (operador_coincide, distancia_m) para un elemento de /markers.
    operador_coincide: True/False, o None si no es comparable.
    distancia_m: mínima a las coordenadas del Gis_ID, o None si no calculable.
    """
    operadores = entrada_inventario["operadores"]

    if operadores and elemento["empresa"]:
        coincide = elemento["empresa"] in operadores
    else:
        coincide = None

    distancia = None
    if (
        elemento["latitud"] is not None
        and elemento["longitud"] is not None
        and entrada_inventario["coordenadas"]
    ):
        distancia = min(
            distancia_haversine_m(lat, lon, elemento["latitud"], elemento["longitud"])
            for lat, lon in entrada_inventario["coordenadas"]
        )

    return coincide, distancia


def _agregar_coincidencia(valores):
    if any(v is True for v in valores):
        return True
    if any(v is False for v in valores):
        return False
    return None


def _unicos_ordenados(valores):
    return list(OrderedDict.fromkeys(v for v in valores if v))


def cruzar(inventario, markers):
    """
    Cruza por coincidencia literal Gis_ID == codigo solicitado a /markers.

    Devuelve (filas_control, filas_imagenes) con valores tipados (bool/None/
    float); el formateo a texto se hace al escribir los CSV.
    """
    filas_control = []
    filas_imagenes = []

    for gis_id, entrada in inventario.items():
        observaciones = []
        marker = markers.get(gis_id)

        if len(entrada["operadores"]) > 1:
            observaciones.append("VARIOS_OPERADORES_INFOANTENAS")
        if len(entrada["coordenadas"]) > 1:
            observaciones.append("COORDENADAS_DISTINTAS_EN_INVENTARIO")
        if entrada["coordenadas_invalidas"]:
            observaciones.append("COORDENADAS_INVENTARIO_INVALIDAS")
        if not entrada["operadores"]:
            observaciones.append("OPERADOR_INVENTARIO_NO_DEDUCIBLE")

        fila = {
            "GIS_ID": gis_id,
            "ENCONTRADO_ANTENASMOVILES": False,
            "CODIGO_ANTENASMOVILES": "",
            "OPERADOR_ANTENASMOVILES": "",
            "OPERADOR_COINCIDE": None,
            "DISTANCIA_M": None,
            "NUM_IMAGENES": 0,
        }

        if marker is None:
            observaciones.append("SIN_RESPUESTA_EN_MARKERS_RAW")
        elif not marker["elementos"]:
            observaciones.append("MARKER_SIN_ELEMENTOS")
        else:
            elementos = marker["elementos"]
            coincidencias = []
            distancias = []

            if len(elementos) > 1:
                observaciones.append("VARIOS_ELEMENTOS_EN_RESPUESTA")

            for elemento in elementos:
                coincide, distancia = comparar_elemento(entrada, elemento)
                coincidencias.append(coincide)

                if distancia is not None:
                    distancias.append(distancia)

                if elemento["codigo"] and elemento["codigo"] != gis_id:
                    observaciones.append("CODIGO_RESPUESTA_DISTINTO")

                if elemento["imagenes_descartadas"]:
                    observaciones.append("IMAGENES_SIN_FICHERO_DESCARTADAS")

                for imagen in elemento["imagenes"]:
                    filas_imagenes.append({
                        "GIS_ID": gis_id,
                        "CODIGO_ANTENASMOVILES": elemento["codigo"],
                        "ID_IMAGEN_ORIGEN": imagen["id"],
                        "NOMBRE_FICHERO_ORIGEN": imagen["fichero"],
                        "AUTOR": imagen["autor"],
                        "FECHA_FOTO": imagen["fecha_foto"],
                        "FECHA_SUBIDA": imagen["fecha_subida"],
                        "URL_ORIGEN": IMAGE_PREFIX + imagen["fichero"],
                        "OLD": imagen["old"],
                        "DESMANTELADA": imagen["desmantelada"],
                        "LAT_SUBIDA": imagen["lat_subida"],
                        "LON_SUBIDA": imagen["lon_subida"],
                        "OPERADOR_COINCIDE": coincide,
                        "DISTANCIA_M": distancia,
                    })

            coincide_total = _agregar_coincidencia(coincidencias)

            if coincide_total is False:
                observaciones.append("OPERADOR_NO_COINCIDE")
            elif coincide_total is None:
                observaciones.append("OPERADOR_NO_COMPARABLE")

            if not distancias:
                observaciones.append("DISTANCIA_NO_CALCULABLE")

            fila.update({
                "ENCONTRADO_ANTENASMOVILES": True,
                "CODIGO_ANTENASMOVILES": SEPARADOR_OBSERVACIONES.join(
                    _unicos_ordenados(e["codigo"] for e in elementos)
                ),
                "OPERADOR_ANTENASMOVILES": SEPARADOR_OBSERVACIONES.join(
                    _unicos_ordenados(e["empresa"] for e in elementos)
                ),
                "OPERADOR_COINCIDE": coincide_total,
                "DISTANCIA_M": min(distancias) if distancias else None,
                "NUM_IMAGENES": sum(len(e["imagenes"]) for e in elementos),
            })

        fila["OBSERVACIONES"] = SEPARADOR_OBSERVACIONES.join(
            _unicos_ordenados(observaciones)
        )
        filas_control.append(fila)

    return filas_control, filas_imagenes


# ---------------------------------------------------------------------------
# Resumen
# ---------------------------------------------------------------------------

def construir_resumen(
    inventario,
    estadisticas_inventario,
    markers,
    estadisticas_raw,
    filas_control,
    filas_imagenes,
):
    encontrados = [f for f in filas_control if f["ENCONTRADO_ANTENASMOVILES"]]
    no_encontrados = [f["GIS_ID"] for f in filas_control if not f["ENCONTRADO_ANTENASMOVILES"]]
    codigos_fuera_inventario = sorted(c for c in markers if c not in inventario)

    ids = {f["ID_IMAGEN_ORIGEN"] for f in filas_imagenes}
    ficheros = {f["NOMBRE_FICHERO_ORIGEN"] for f in filas_imagenes}

    ficheros_por_id = {}
    ids_por_fichero = {}
    for f in filas_imagenes:
        ficheros_por_id.setdefault(f["ID_IMAGEN_ORIGEN"], set()).add(f["NOMBRE_FICHERO_ORIGEN"])
        ids_por_fichero.setdefault(f["NOMBRE_FICHERO_ORIGEN"], set()).add(f["ID_IMAGEN_ORIGEN"])

    descartadas = sum(
        e["imagenes_descartadas"]
        for m in markers.values()
        for e in m["elementos"]
    )

    distancias = [f["DISTANCIA_M"] for f in encontrados if f["DISTANCIA_M"] is not None]

    return {
        "inventario": {
            "filas": estadisticas_inventario["filas"],
            "filas_sin_gis_id": estadisticas_inventario["filas_sin_gis_id"],
            "filas_operador_no_deducible": estadisticas_inventario["filas_operador_no_deducible"],
            "gis_id_unicos": len(inventario),
            "gis_id_con_varios_operadores": sum(
                1 for e in inventario.values() if len(e["operadores"]) > 1
            ),
            "gis_id_con_coordenadas_distintas": sum(
                1 for e in inventario.values() if len(e["coordenadas"]) > 1
            ),
        },
        "markers_raw": {
            "lineas": estadisticas_raw["lineas"],
            "respuestas_validas": estadisticas_raw["respuestas_validas"],
            "lineas_invalidas": estadisticas_raw["lineas_invalidas"],
            "lineas_invalidas_detalle": estadisticas_raw["lineas_invalidas_detalle"],
            "codigos_duplicados": estadisticas_raw["codigos_duplicados"],
            "codigos_unicos": len(markers),
            "codigos_fuera_del_inventario": len(codigos_fuera_inventario),
            "codigos_fuera_del_inventario_lista": codigos_fuera_inventario[:MAX_ELEMENTOS_LISTA_RESUMEN],
        },
        "cruce": {
            "gis_id_encontrados": len(encontrados),
            "gis_id_no_encontrados": len(no_encontrados),
            "gis_id_no_encontrados_lista": no_encontrados[:MAX_ELEMENTOS_LISTA_RESUMEN],
            "operador_coincide": sum(1 for f in encontrados if f["OPERADOR_COINCIDE"] is True),
            "operador_no_coincide": sum(1 for f in encontrados if f["OPERADOR_COINCIDE"] is False),
            "operador_no_comparable": sum(1 for f in encontrados if f["OPERADOR_COINCIDE"] is None),
            "distancia_m": estadisticas_distancia(distancias),
        },
        "imagenes": {
            "relaciones_gis_id_imagen": len(filas_imagenes),
            "unicas_por_id": len(ids),
            "unicas_por_fichero": len(ficheros),
            "gis_id_con_imagenes": sum(1 for f in filas_control if f["NUM_IMAGENES"] > 0),
            "ids_con_varios_ficheros": sum(1 for v in ficheros_por_id.values() if len(v) > 1),
            "ficheros_con_varios_ids": sum(1 for v in ids_por_fichero.values() if len(v) > 1),
            "relaciones_old": sum(1 for f in filas_imagenes if f["OLD"]),
            "relaciones_desmantelada": sum(1 for f in filas_imagenes if f["DESMANTELADA"]),
            "imagenes_sin_fichero_descartadas": descartadas,
        },
    }


# ---------------------------------------------------------------------------
# Escritura segura
# ---------------------------------------------------------------------------

def _texto_booleano(valor):
    if valor is None:
        return ""
    return "SI" if valor else "NO"


def formatear_fila_control(fila):
    salida = dict(fila)
    salida["ENCONTRADO_ANTENASMOVILES"] = _texto_booleano(fila["ENCONTRADO_ANTENASMOVILES"])
    salida["OPERADOR_COINCIDE"] = _texto_booleano(fila["OPERADOR_COINCIDE"])
    salida["DISTANCIA_M"] = "" if fila["DISTANCIA_M"] is None else f"{fila['DISTANCIA_M']:.3f}"
    return salida


def formatear_fila_imagen(fila):
    salida = dict(fila)
    salida["OPERADOR_COINCIDE"] = _texto_booleano(fila["OPERADOR_COINCIDE"])
    salida["DISTANCIA_M"] = "" if fila["DISTANCIA_M"] is None else f"{fila['DISTANCIA_M']:.3f}"
    return salida


def escribir_csv_atomico(path: Path, fieldnames, filas):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporal = path.with_name(path.name + ".tmp")

    with temporal.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=fieldnames,
            delimiter=";",
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(filas)

    os.replace(temporal, path)


def escribir_json_atomico(path: Path, datos):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporal = path.with_name(path.name + ".tmp")

    with temporal.open("w", encoding="utf-8", newline="") as f:
        json.dump(datos, f, ensure_ascii=False, indent=2)

    os.replace(temporal, path)


def sha256_fichero(path: Path):
    resumen = hashlib.sha256()

    with path.open("rb") as f:
        for bloque in iter(lambda: f.read(1024 * 1024), b""):
            resumen.update(bloque)

    return resumen.hexdigest()


# ---------------------------------------------------------------------------
# Programa principal
# ---------------------------------------------------------------------------

def ejecutar(inventario_path: Path, raw_path: Path, output_dir: Path):
    inventario, estadisticas_inventario = leer_inventario(inventario_path)
    markers, estadisticas_raw = leer_markers_raw(raw_path)
    filas_control, filas_imagenes = cruzar(inventario, markers)

    resumen = construir_resumen(
        inventario,
        estadisticas_inventario,
        markers,
        estadisticas_raw,
        filas_control,
        filas_imagenes,
    )
    resumen = {
        "modo": "offline",
        "generado_en": datetime.now().isoformat(timespec="seconds"),
        "entradas": {
            "inventario": str(inventario_path),
            "inventario_sha256": sha256_fichero(inventario_path),
            "markers_raw": str(raw_path),
            "markers_raw_sha256": sha256_fichero(raw_path),
        },
        **resumen,
    }

    escribir_csv_atomico(
        output_dir / "CONTROL_GIS_ID.csv",
        CAMPOS_CONTROL,
        (formatear_fila_control(f) for f in filas_control),
    )
    escribir_csv_atomico(
        output_dir / "IMATGES_INFOANTENAS.csv",
        CAMPOS_IMAGENES,
        (formatear_fila_imagen(f) for f in filas_imagenes),
    )
    escribir_json_atomico(output_dir / "resum_offline.json", resumen)

    return resumen


def main():
    parser = argparse.ArgumentParser(
        description="Fase 1 offline: cruce Infoantenas <-> markers e imágenes."
    )
    parser.add_argument("--inventario", default=DEFAULT_INVENTARIO)
    parser.add_argument("--raw", default=DEFAULT_RAW)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    inventario_path = Path(args.inventario)
    raw_path = Path(args.raw)
    output_dir = Path(args.output_dir)

    for ruta in (inventario_path, raw_path):
        if not ruta.is_file():
            raise FileNotFoundError(f"No existe el fichero de entrada: {ruta}")

    resumen = ejecutar(inventario_path, raw_path, output_dir)

    print(json.dumps(
        {k: v for k, v in resumen.items() if k != "entradas"},
        ensure_ascii=False,
        indent=2,
    ))
    print(f"Salida: {output_dir.resolve()}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
