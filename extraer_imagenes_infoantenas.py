#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Extractor de imágenes de antenasmoviles para los Gis_ID de Infoantenas.

FASES IMPLEMENTADAS: 1 (offline), 2 (consulta online de /v1/markers) y 3
(descarga de imágenes completas y miniaturas locales, opcional con
--descargar-imagenes). Todavía no hay versionado nueva/actual/anterior.

Dos modos; a partir de la respuesta del marker ambos siguen el mismo camino
(parsear_marker -> cruzar -> CSV):

  OFFLINE (por defecto)
    inventari_infoantenas.csv + salida_markers/markers_raw.jsonl
    Salidas: CONTROL_GIS_ID.csv, IMATGES_INFOANTENAS.csv, resum_offline.json

  ONLINE (--modo online)
    inventari_infoantenas.csv -> GET /v1/markers?id=<Gis_ID> (secuencial)
    -> una respuesta por Gis_ID en <trabajo>/markers/<codigo>_<sha12>.json
    Salidas: las mismas, resum_online.json, y en <trabajo>/: run.json y
    log_markers.csv

Reanudación (--resume): solo dentro de UNA ejecución interrumpida. Reutiliza
las respuestas válidas ya guardadas y consulta únicamente las pendientes. Sin
--resume se descarta el estado previo del directorio de trabajo y se vuelve a
consultar todo (no hay lógica incremental entre ejecuciones).

Imágenes (--descargar-imagenes, solo online):
  Cada fichero físico se descarga UNA vez por ejecución aunque lo compartan
  varios Gis_ID (el CSV mantiene una fila por relación). Descarga secuencial a
  <imagenes>/full/<fichero>, siempre vía .part y validada (HTTP 200,
  Content-Type de imagen, no vacía, decodificable con Pillow). La miniatura
  (<imagenes>/thumb/) se genera localmente a partir de la completa; nunca se
  descarga. Ritmo propio (--imatges-per-minut), independiente de /markers.
  Las imágenes old/desmantelada y la distancia/operador NO excluyen ninguna
  descarga. Con --resume: original y miniatura válidos se reutilizan, si falta
  solo la miniatura se regenera, y un original ausente o corrupto se vuelve a
  descargar. Salidas extra: RUTA_IMAGEN, RUTA_MINIATURA (relativas a
  <imagenes>) y ESTADO_DESCARGA en IMATGES_INFOANTENAS.csv, y log_imatges.csv
  (una fila por fichero único) en el directorio de trabajo.

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
import http.client
import json
import math
import os
import re
import shutil
import ssl
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, OrderedDict
from datetime import datetime
from pathlib import Path

try:
    import truststore
except ImportError:
    truststore = None

try:
    from PIL import Image, ImageOps
except ImportError:
    Image = None
    ImageOps = None

IMAGE_PREFIX = "https://antenasmoviles.es/archive/antenas/"
MARKERS_URL = "https://antenasmoviles.es/v1/markers"

DEFAULT_INVENTARIO = "infoantenas/01_DADES_FINALS/inventari_infoantenas.csv"
DEFAULT_RAW = "salida_markers/markers_raw.jsonl"
DEFAULT_OUTPUT_DIR = "salida_imagenes_infoantenas_test"

DEFAULT_PETICIONS_PER_MINUT = 10.0
DEFAULT_TIMEOUT = 60
DEFAULT_REINTENTS = 3
FORMATO_RUN = 1
USER_AGENT = "Mozilla/5.0 (compatible; SITEP-Antenas/1.0)"
ESPERA_MAXIMA_S = 300
ESPERA_429_POR_DEFECTO_S = 60
MUESTRA_CUERPO_MAX = 200

# Estados de la consulta de un Gis_ID (log_markers.csv).
EST_YA_EXISTENTE = "YA_EXISTENTE"
EST_CON_MARKER = "RESPUESTA_CON_MARKER"
EST_JSON_VACIO = "JSON_VACIO"
EST_SIN_MARKER_COMPATIBLE = "SIN_MARKER_COMPATIBLE"
EST_ESTRUCTURA_INESPERADA = "ESTRUCTURA_INESPERADA"
EST_JSON_INVALIDO = "JSON_INVALIDO"
EST_HTTP_NO_ENCONTRADO = "HTTP_NO_ENCONTRADO"
EST_HTTP_NO_RECUPERABLE = "HTTP_NO_RECUPERABLE"
EST_ERROR_HTTP_RECUPERABLE = "ERROR_HTTP_RECUPERABLE"
EST_ERROR_RED = "ERROR_RED"

# Estados cuya respuesta se guarda en markers/ (el JSON es una lista).
ESTADOS_PERSISTIDOS = {EST_CON_MARKER, EST_JSON_VACIO, EST_SIN_MARKER_COMPATIBLE}
# Estados que cuentan como fallo técnico (código de salida 2).
ESTADOS_ERROR_TECNICO = {
    EST_ESTRUCTURA_INESPERADA,
    EST_JSON_INVALIDO,
    EST_HTTP_NO_RECUPERABLE,
    EST_ERROR_HTTP_RECUPERABLE,
    EST_ERROR_RED,
}
HTTP_NO_ENCONTRADO = {404, 410}

# Cabeceras que se guardan en el log (nunca Set-Cookie).
CABECERAS_RELEVANTES = {
    "date", "content-type", "content-length", "content-encoding",
    "transfer-encoding", "server", "retry-after", "etag", "last-modified",
    "cache-control", "expires", "age", "location", "cf-cache-status", "cf-ray",
    "cf-mitigated",
}
PREFIJOS_RATELIMIT = ("x-ratelimit", "ratelimit")
MARCAS_DESAFIO = (b"just a moment", b"cf-chl", b"attention required", b"cf-browser-verification")
MAX_CUERPO_ERROR = 1024 * 1024

# Descarga de imágenes (Fase 3).
DEFAULT_IMATGES_PER_MINUT = 5.0
DEFAULT_IMATGES_REINTENTS = 2
DEFAULT_MINIATURA_MAX = 300
MAX_BYTES_IMAGEN = 50 * 1024 * 1024
TAMANO_BLOQUE = 64 * 1024
IMAGEN_ACCEPT = "image/jpeg,image/png,image/*;q=0.8"

# Estados de la descarga de una imagen (log_imatges.csv y ESTADO_DESCARGA).
EST_IMG_OK = "OK"
EST_IMG_HTTP_ERROR = "HTTP_ERROR"
EST_IMG_CONTENT_TYPE_INVALIDO = "CONTENT_TYPE_INVALIDO"
EST_IMG_VACIA = "VACIA"
EST_IMG_CORRUPTA = "IMAGEN_CORRUPTA"
EST_IMG_ERROR_RED = "ERROR_RED"
EST_IMG_PENDIENTE = "PENDIENTE"  # no intentada (fuera de selección o ejecución detenida)

ACCION_DESCARGA = "DESCARGA"
ACCION_REUTILIZADA = "REUTILIZADA"
ACCION_SOLO_MINIATURA = "SOLO_MINIATURA"

CAMPOS_DESCARGA = ["RUTA_IMAGEN", "RUTA_MINIATURA", "ESTADO_DESCARGA"]

CAMPOS_LOG_IMAGENES = [
    "MOMENTO",
    "ID_IMAGEN_ORIGEN",
    "NOMBRE_FICHERO",
    "NOMBRE_LOCAL",
    "URL_ORIGEN",
    "ESTADO",
    "ACCION",
    "HTTP_STATUS",
    "INTENTOS",
    "CONTENT_TYPE",
    "BYTES",
    "FORMATO",
    "ANCHO",
    "ALTO",
    "MINIATURA_ANCHO",
    "MINIATURA_ALTO",
    "CABECERAS",
    "ERROR",
]

CAMPOS_LOG = [
    "MOMENTO",
    "GIS_ID",
    "ESTADO",
    "HTTP_STATUS",
    "INTENTOS",
    "BYTES",
    "CONTENT_TYPE",
    "CABECERAS",
    "FICHERO",
    "ERROR",
    "MUESTRA_CUERPO",
]

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


def _estadisticas_markers_vacias():
    return {
        "lineas": 0,
        "respuestas_validas": 0,
        "lineas_invalidas": 0,
        "lineas_invalidas_detalle": [],
        "codigos_duplicados": 0,
    }


def _registrar_invalida(estadisticas, referencia, error):
    estadisticas["lineas_invalidas"] += 1

    if len(estadisticas["lineas_invalidas_detalle"]) < MAX_ELEMENTOS_LISTA_RESUMEN:
        estadisticas["lineas_invalidas_detalle"].append(
            {**referencia, "error": str(error)}
        )


def _incorporar_respuesta(markers, estadisticas, codigo, http_status, data, referencia):
    """Único punto por el que una respuesta (raw u online) entra al cruce."""
    try:
        elementos = parsear_marker(data)
    except ValueError as exc:
        _registrar_invalida(estadisticas, referencia, exc)
        return

    if codigo in markers:
        estadisticas["codigos_duplicados"] += 1

    markers[codigo] = {"http_status": http_status, "elementos": elementos}
    estadisticas["respuestas_validas"] += 1


def leer_markers_raw(path: Path):
    """
    Lee markers_raw.jsonl ({codigo_antena, http_status, data} por línea).

    Devuelve (markers, estadisticas). markers: codigo_solicitado -> {
    "http_status", "elementos"}. Si un código aparece varias veces gana la
    última respuesta válida y se cuenta como duplicado.
    """
    markers = {}
    estadisticas = _estadisticas_markers_vacias()

    with path.open("r", encoding="utf-8") as f:
        for numero_linea, linea in enumerate(f, start=1):
            linea = linea.strip()

            if not linea:
                continue

            estadisticas["lineas"] += 1
            referencia = {"linea": numero_linea}

            try:
                registro = json.loads(linea)
                codigo = a_texto(registro.get("codigo_antena")).strip()

                if not codigo:
                    raise ValueError("Falta codigo_antena.")

            except (ValueError, AttributeError) as exc:
                _registrar_invalida(estadisticas, referencia, exc)
                continue

            _incorporar_respuesta(
                markers,
                estadisticas,
                codigo,
                registro.get("http_status"),
                registro.get("data"),
                referencia,
            )

    return markers, estadisticas


def leer_markers_dir(markers_dir: Path, gis_ids):
    """
    Lee las respuestas guardadas por el modo online (una por Gis_ID) y devuelve
    el mismo (markers, estadisticas) que leer_markers_raw. Los ficheros
    ausentes se omiten; los inválidos se cuentan.
    """
    markers = {}
    estadisticas = _estadisticas_markers_vacias()

    for gis_id in gis_ids:
        ruta = markers_dir / nombre_fichero_marker(gis_id)

        if not ruta.exists():
            continue

        estadisticas["lineas"] += 1
        registro = leer_respuesta_guardada(ruta, gis_id)

        if registro is None:
            _registrar_invalida(
                estadisticas, {"fichero": ruta.name}, "Respuesta guardada no válida."
            )
            continue

        _incorporar_respuesta(
            markers,
            estadisticas,
            gis_id,
            registro["HTTP_STATUS"],
            registro["RESPUESTA"],
            {"fichero": ruta.name},
        )

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


def cruzar(inventario, markers, estados_consulta=None):
    """
    Cruza por coincidencia literal Gis_ID == codigo solicitado a /markers.

    estados_consulta (opcional, modo online): gis_id -> ESTADO de la consulta,
    solo se usa para explicar en OBSERVACIONES por qué no hay marker.

    Devuelve (filas_control, filas_imagenes) con valores tipados (bool/None/
    float); el formateo a texto se hace al escribir los CSV.
    """
    filas_control = []
    filas_imagenes = []
    estados_consulta = estados_consulta or {}

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
            estado_consulta = estados_consulta.get(gis_id)
            if estado_consulta:
                observaciones.append("CONSULTA_" + estado_consulta)
            else:
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
                        "URL_ORIGEN": construir_url_imagen(imagen["fichero"]),
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


def escribir_json_atomico(path: Path, datos, sufijo=".tmp", indent=2):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporal = path.with_name(path.name + sufijo)

    with temporal.open("w", encoding="utf-8", newline="") as f:
        json.dump(datos, f, ensure_ascii=False, indent=indent)

    os.replace(temporal, path)


def sha256_fichero(path: Path):
    resumen = hashlib.sha256()

    with path.open("rb") as f:
        for bloque in iter(lambda: f.read(1024 * 1024), b""):
            resumen.update(bloque)

    return resumen.hexdigest()


# ---------------------------------------------------------------------------
# Modo online: cliente HTTP de /v1/markers, respuestas por Gis_ID y reanudación
# ---------------------------------------------------------------------------

def nombre_fichero_marker(gis_id):
    """
    Nombre de fichero seguro y determinista: Gis_ID URL-encoded (UTF-8) más un
    hash corto del valor exacto (mismo patrón que los scripts de Infoantenas).
    """
    resumen = hashlib.sha256(gis_id.encode("utf-8")).hexdigest()[:12]
    componente = urllib.parse.quote(gis_id, safe="")[:160]
    return f"{componente}_{resumen}.json"


def construir_url_marker(gis_id):
    """El Gis_ID se envía literal, codificado en UTF-8 para la URL."""
    return MARKERS_URL + "?" + urllib.parse.urlencode({"id": gis_id})


def crear_contexto_ssl():
    # truststore es opcional, igual que en los scripts de Infoantenas (usa el
    # almacén de certificados del sistema); sin él se usa el contexto estándar.
    if truststore is not None:
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return ssl.create_default_context()


def crear_apertura(contexto_ssl=None):
    contexto = contexto_ssl or crear_contexto_ssl()

    def abrir(peticion, timeout):
        return urllib.request.urlopen(peticion, timeout=timeout, context=contexto)

    return abrir


def obtener_retry_after(headers):
    """Segundos indicados por Retry-After, o None si falta o no es numérico."""
    valor = headers.get("Retry-After") if headers else None

    if not valor:
        return None

    try:
        return max(0.0, float(valor))
    except ValueError:
        return None


class Limitador:
    """Garantiza un intervalo mínimo entre inicios de petición."""

    def __init__(self, intervalo_s, reloj=time.monotonic, dormir=time.sleep):
        self.intervalo_s = intervalo_s
        self._reloj = reloj
        self._dormir = dormir
        self._ultima = None

    def esperar(self):
        if self._ultima is None:
            return

        espera = self.intervalo_s - (self._reloj() - self._ultima)

        if espera > 0:
            self._dormir(espera)

    def marcar(self):
        self._ultima = self._reloj()


def cabeceras_a_dict(headers):
    """Cabeceras relevantes para trazabilidad; excluye cookies y ruido de CDN."""
    resultado = {}

    for nombre, valor in (headers.items() if headers else []):
        clave = nombre.lower()

        if clave not in CABECERAS_RELEVANTES and not clave.startswith(PREFIJOS_RATELIMIT):
            continue

        resultado[clave] = resultado[clave] + ", " + valor if clave in resultado else valor

    return resultado


def muestra_cuerpo(cuerpo):
    texto = cuerpo.decode("utf-8", errors="replace")
    return " ".join(texto.split())[:MUESTRA_CUERPO_MAX]


def clasificar_cuerpo(cuerpo, charset):
    """Clasifica el cuerpo de un HTTP 200. Devuelve (estado, data, error)."""
    if not cuerpo or not cuerpo.strip():
        return EST_JSON_INVALIDO, None, "Cuerpo vacío."

    try:
        data = json.loads(cuerpo.decode(charset or "utf-8").lstrip("\ufeff"))
    except (LookupError, ValueError) as exc:
        return EST_JSON_INVALIDO, None, f"No es JSON válido: {exc}"

    if not isinstance(data, list):
        return (
            EST_ESTRUCTURA_INESPERADA,
            None,
            f"Se esperaba una lista JSON y es {type(data).__name__}.",
        )

    if not data:
        return EST_JSON_VACIO, data, ""

    if not parsear_marker(data):
        return (
            EST_SIN_MARKER_COMPATIBLE,
            data,
            "Lista sin elementos de marker interpretables.",
        )

    return EST_CON_MARKER, data, ""


def _espera_backoff(intento):
    return min(10 * (2 ** (intento - 1)), ESPERA_MAXIMA_S)


def detectar_bloqueo(status, headers, cuerpo=b""):
    """Señales de rate limiting o de bloqueo (incluye el desafío de Cloudflare)."""
    if status in (403, 429):
        return True

    if headers and headers.get("cf-mitigated"):
        return True

    muestra = cuerpo[:4096].lower()
    return status in (200, 503) and any(marca in muestra for marca in MARCAS_DESAFIO)


def ejecutar_peticion(
    peticion,
    abrir,
    limitador,
    resultado,
    leer,
    *,
    timeout,
    reintentos,
    dormir,
    imprimir,
    estado_http,
    estado_red,
    estado_recuperable,
    parar_ante_bloqueo=False,
):
    """
    Bucle común de petición con reintentos para marcadores e imágenes.

    leer(respuesta) se ejecuta dentro del contexto de la respuesta y rellena
    resultado (estado, bytes, ...). Reintenta HTTP 429 (respetando
    Retry-After), 5xx y errores de red; el resto de HTTP es definitivo
    (estado_http(status)). Con parar_ante_bloqueo no se reintenta ante una
    señal de bloqueo. Si Retry-After supera ESPERA_MAXIMA_S tampoco se
    reintenta y se marca resultado["bloqueo"].
    """
    resultado["bloqueo"] = False

    for intento in range(1, reintentos + 2):
        resultado.update(
            estado=None,
            http_status="",
            bytes="",
            content_type="",
            cabeceras={},
            error="",
            muestra="",
        )
        limitador.esperar()
        resultado["intentos"] = intento
        limitador.marcar()
        espera = None

        try:
            with abrir(peticion, timeout) as respuesta:
                leer(respuesta)

            return resultado

        except urllib.error.HTTPError as exc:
            status = exc.code
            headers = exc.headers
            try:
                cuerpo = exc.read(MAX_CUERPO_ERROR)
            except Exception:
                cuerpo = b""
            finally:
                exc.close()

            _registrar_respuesta(resultado, status, headers, cuerpo)
            bloqueo = detectar_bloqueo(status, headers, cuerpo)
            resultado["bloqueo"] = bloqueo

            if status == 429:
                retry_after = obtener_retry_after(headers)

                if retry_after is not None and retry_after > ESPERA_MAXIMA_S:
                    resultado["bloqueo"] = True
                    resultado.update(
                        estado=estado_recuperable,
                        error=f"HTTP 429 con Retry-After de {retry_after:.0f}s (no se espera).",
                    )
                    return resultado

                espera = (
                    max(limitador.intervalo_s, retry_after)
                    if retry_after is not None
                    else min(ESPERA_429_POR_DEFECTO_S * intento, ESPERA_MAXIMA_S)
                )
                resultado.update(estado=estado_recuperable, error="HTTP 429")
            elif 500 <= status <= 599:
                espera = _espera_backoff(intento)
                resultado.update(estado=estado_recuperable, error=f"HTTP {status}")
            else:
                resultado.update(
                    estado=estado_http(status),
                    error=f"HTTP {status}: {exc.reason}",
                )
                return resultado

            if bloqueo and parar_ante_bloqueo:
                return resultado

        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            motivo = getattr(exc, "reason", None) or exc
            resultado.update(
                estado=estado_red,
                error=f"{type(exc).__name__}: {motivo}",
            )
            espera = _espera_backoff(intento)

        if intento <= reintentos:
            imprimir(
                f"      {resultado['error']}. Reintento {intento}/{reintentos} "
                f"en {espera:.0f}s..."
            )
            dormir(espera)

    return resultado


def _estado_http_marker(status):
    if status in HTTP_NO_ENCONTRADO:
        return EST_HTTP_NO_ENCONTRADO
    return EST_HTTP_NO_RECUPERABLE


def consultar_marker(
    gis_id,
    abrir,
    limitador,
    *,
    timeout,
    reintentos,
    dormir=time.sleep,
    imprimir=print,
):
    """
    Consulta /v1/markers?id=<gis_id> con reintentos (reintentos adicionales).

    Recuperables: HTTP 429 (respeta Retry-After), HTTP 5xx y errores de red.
    El resto de HTTP no se reintenta. Nunca lanza por un problema de la
    respuesta: devuelve un dict con ESTADO, HTTP_STATUS, intentos, bytes,
    cabeceras, error y, si hay una lista JSON, data.
    """
    url = construir_url_marker(gis_id)
    peticion = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        },
    )
    resultado = {"url": url, "intentos": 0, "data": None}

    def leer(respuesta):
        cuerpo = respuesta.read()
        _registrar_respuesta(resultado, respuesta.status, respuesta.headers, cuerpo)

        if respuesta.status != 200:
            resultado.update(
                estado=EST_HTTP_NO_RECUPERABLE,
                error=f"HTTP {respuesta.status} inesperado.",
            )
            return

        estado, data, error = clasificar_cuerpo(
            cuerpo,
            respuesta.headers.get_content_charset() if respuesta.headers else None,
        )
        resultado.update(estado=estado, data=data, error=error)

    return ejecutar_peticion(
        peticion,
        abrir,
        limitador,
        resultado,
        leer,
        timeout=timeout,
        reintentos=reintentos,
        dormir=dormir,
        imprimir=imprimir,
        estado_http=_estado_http_marker,
        estado_red=EST_ERROR_RED,
        estado_recuperable=EST_ERROR_HTTP_RECUPERABLE,
    )


def _registrar_respuesta(resultado, status, headers, cuerpo):
    resultado.update(
        http_status=status,
        bytes=len(cuerpo),
        content_type=headers.get("Content-Type", "") if headers else "",
        cabeceras=cabeceras_a_dict(headers),
        muestra=muestra_cuerpo(cuerpo),
    )


def guardar_respuesta(ruta: Path, gis_id, resultado):
    """Guarda la respuesta de un Gis_ID de forma atómica (.part + replace)."""
    registro = {
        "GIS_ID": gis_id,
        "HTTP_STATUS": resultado["http_status"],
        "CONSULTADO_EN": datetime.now().isoformat(timespec="seconds"),
        "URL": resultado["url"],
        "CONTENT_TYPE": resultado["content_type"],
        "RESPUESTA": resultado["data"],
    }
    escribir_json_atomico(ruta, registro, sufijo=".part", indent=None)


def leer_respuesta_guardada(ruta: Path, gis_id):
    """
    Devuelve el registro guardado si es válido para ese Gis_ID; None si el
    fichero falta, está vacío o corrupto, pertenece a otro Gis_ID o no contiene
    una respuesta HTTP 200 con una lista JSON.
    """
    try:
        with ruta.open("r", encoding="utf-8") as f:
            registro = json.load(f)
    except (OSError, ValueError):
        return None

    if not isinstance(registro, dict) or registro.get("GIS_ID") != gis_id:
        return None

    http_status = registro.get("HTTP_STATUS")
    if isinstance(http_status, bool) or http_status != 200:
        return None

    if not isinstance(registro.get("RESPUESTA"), list):
        return None

    return registro


def preparar_trabajo(trabajo_dir: Path, inventario_path: Path, inventario_sha256, parametros, resume):
    """
    Prepara el directorio de trabajo y escribe run.json.

    Con resume: exige un run.json previo con el mismo SHA-256 de inventario.
    Sin resume: descarta únicamente markers/, run.json y log_markers.csv de una
    ejecución anterior, de modo que se vuelva a consultar todo.
    """
    run_path = trabajo_dir / "run.json"
    markers_dir = trabajo_dir / "markers"
    ahora = datetime.now().isoformat(timespec="seconds")

    if resume:
        if not run_path.is_file():
            raise ValueError(
                f"No hay ninguna ejecución que reanudar: no existe {run_path}."
            )

        try:
            run = json.loads(run_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"No se puede leer {run_path}: {exc}") from exc

        if not isinstance(run, dict) or run.get("formato") != FORMATO_RUN:
            raise ValueError(
                f"{run_path} tiene un formato no soportado "
                f"(se esperaba formato {FORMATO_RUN})."
            )

        if run.get("inventario_sha256") != inventario_sha256:
            raise ValueError(
                "No se puede reanudar: el SHA-256 del inventario actual "
                f"({inventario_sha256}) no coincide con el de la ejecución "
                f"que se quiere reanudar ({run.get('inventario_sha256')}). "
                "Lanza una ejecución nueva sin --resume."
            )

        run.setdefault("reanudaciones", []).append(
            {"inicio": ahora, "parametros": parametros}
        )
    else:
        trabajo_dir.mkdir(parents=True, exist_ok=True)

        if markers_dir.exists():
            shutil.rmtree(markers_dir)

        for antiguo in (
            run_path,
            trabajo_dir / "log_markers.csv",
            trabajo_dir / "log_imatges.csv",
        ):
            antiguo.unlink(missing_ok=True)

        run = {
            "formato": FORMATO_RUN,
            "inicio": ahora,
            "inventario": str(inventario_path),
            "inventario_sha256": inventario_sha256,
            "parametros": parametros,
            "reanudaciones": [],
        }

    markers_dir.mkdir(parents=True, exist_ok=True)
    escribir_json_atomico(run_path, run, sufijo=".part")

    return run


def seleccionar_gis_ids(inventario, gis_ids=None, maxim=None):
    """Gis_ID a tratar: los indicados (en ese orden) o todos; luego los primeros maxim."""
    if gis_ids:
        desconocidos = [g for g in gis_ids if g not in inventario]

        if desconocidos:
            raise ValueError(
                "Gis_ID no presentes en el inventario: " + ", ".join(desconocidos)
            )

        seleccion = list(OrderedDict.fromkeys(gis_ids))
    else:
        seleccion = list(inventario)

    return seleccion[:maxim] if maxim is not None else seleccion


def consultar_pendientes(
    gis_ids,
    markers_dir: Path,
    log_path: Path,
    abrir,
    limitador,
    *,
    timeout,
    reintentos,
    max_errores_consecutivos=0,
    dormir=time.sleep,
    imprimir=print,
):
    """
    Consulta los Gis_ID sin respuesta válida guardada y reutiliza el resto.

    Escribe un log nuevo (una fila por Gis_ID tratado en esta invocación).
    max_errores_consecutivos=0 desactiva la parada por errores consecutivos.
    Devuelve {"estados", "contadores", "intentos_http", "detenido"}.
    """
    estados = OrderedDict()
    contadores = Counter()
    intentos_http = 0
    errores_consecutivos = 0
    detenido = False

    with log_path.open("w", encoding="utf-8-sig", newline="") as f_log:
        writer = csv.DictWriter(f_log, fieldnames=CAMPOS_LOG, delimiter=";")
        writer.writeheader()
        f_log.flush()

        for posicion, gis_id in enumerate(gis_ids, start=1):
            ruta = markers_dir / nombre_fichero_marker(gis_id)
            existente = leer_respuesta_guardada(ruta, gis_id)

            if existente is not None:
                estado = EST_YA_EXISTENTE
                fila = {
                    "HTTP_STATUS": existente["HTTP_STATUS"],
                    "INTENTOS": 0,
                    "BYTES": ruta.stat().st_size,
                    "CONTENT_TYPE": existente.get("CONTENT_TYPE", ""),
                    "CABECERAS": "",
                    "FICHERO": ruta.name,
                    "ERROR": "",
                    "MUESTRA_CUERPO": "",
                }
            else:
                ruta.unlink(missing_ok=True)

                resultado = consultar_marker(
                    gis_id,
                    abrir,
                    limitador,
                    timeout=timeout,
                    reintentos=reintentos,
                    dormir=dormir,
                    imprimir=imprimir,
                )
                estado = resultado["estado"]
                intentos_http += resultado["intentos"]
                persistido = estado in ESTADOS_PERSISTIDOS

                if persistido:
                    guardar_respuesta(ruta, gis_id, resultado)

                fila = {
                    "HTTP_STATUS": resultado["http_status"],
                    "INTENTOS": resultado["intentos"],
                    "BYTES": resultado["bytes"],
                    "CONTENT_TYPE": resultado["content_type"],
                    "CABECERAS": json.dumps(resultado["cabeceras"], ensure_ascii=False),
                    "FICHERO": ruta.name if persistido else "",
                    "ERROR": resultado["error"],
                    "MUESTRA_CUERPO": "" if persistido else resultado["muestra"],
                }

            fila.update({
                "MOMENTO": datetime.now().isoformat(timespec="seconds"),
                "GIS_ID": gis_id,
                "ESTADO": estado,
            })
            writer.writerow(fila)
            f_log.flush()

            estados[gis_id] = estado
            contadores[estado] += 1
            imprimir(f"[{posicion}/{len(gis_ids)}] {gis_id}: {estado}")

            if estado in ESTADOS_ERROR_TECNICO:
                errores_consecutivos += 1
            else:
                errores_consecutivos = 0

            if max_errores_consecutivos > 0 and errores_consecutivos >= max_errores_consecutivos:
                detenido = True
                imprimir(
                    f"Detenido: {errores_consecutivos} errores técnicos "
                    "consecutivos. Corrige la causa y relanza con --resume."
                )
                break

    return {
        "estados": estados,
        "contadores": dict(contadores),
        "intentos_http": intentos_http,
        "detenido": detenido,
    }


# ---------------------------------------------------------------------------
# Imágenes (Fase 3): descarga, validación y miniaturas locales
# ---------------------------------------------------------------------------

class ImagenInvalida(Exception):
    """La imagen no se puede abrir, decodificar o convertir en miniatura."""


class ErrorEscrituraLocal(Exception):
    """Fallo de disco local: no es un error de red y no se reintenta."""


_NOMBRE_SEGURO = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*")


def construir_url_imagen(fichero):
    """URL de la imagen completa (los nombres habituales no cambian al codificarse)."""
    return IMAGE_PREFIX + urllib.parse.quote(fichero, safe="")


def nombre_local_seguro(fichero):
    """
    Nombre de fichero local. Se conserva el original; solo si pudiera salirse
    del directorio o no ser válido en disco se codifica y se añade un hash.
    """
    if _NOMBRE_SEGURO.fullmatch(fichero):
        return fichero

    resumen = hashlib.sha256(fichero.encode("utf-8")).hexdigest()[:12]
    return f"{urllib.parse.quote(fichero, safe='')[:120]}_{resumen}"


def content_type_es_imagen(content_type):
    return content_type.split(";")[0].strip().lower().startswith("image/")


def _exigir_pillow():
    if Image is None:
        raise RuntimeError(
            "Pillow no está instalado. Ejecuta: python -m pip install -r requirements.txt"
        )


def inspeccionar_imagen(ruta: Path):
    """
    Valida con Pillow (verify y decodificación completa, para detectar
    truncados) sin fiarse de la extensión. Devuelve formato, ancho y alto.
    """
    _exigir_pillow()

    try:
        with Image.open(ruta) as img:
            formato = img.format
            ancho, alto = img.size
            img.verify()

        with Image.open(ruta) as img:
            img.load()
    except Exception as exc:
        raise ImagenInvalida(f"{type(exc).__name__}: {exc}") from exc

    if not formato:
        raise ImagenInvalida("Formato de imagen no identificado.")

    return {"formato": formato, "ancho": ancho, "alto": alto}


def generar_miniatura(origen: Path, destino: Path, max_lado):
    """
    Miniatura local: conserva proporción y formato, sin recortar ni ampliar
    (lado máximo max_lado). Respeta la orientación EXIF solo en la miniatura.
    Devuelve (ancho, alto).
    """
    _exigir_pillow()

    try:
        with Image.open(origen) as img:
            formato = img.format
            img.load()
            miniatura = ImageOps.exif_transpose(img)
            miniatura.thumbnail((max_lado, max_lado), Image.Resampling.LANCZOS)

            opciones = {}
            if formato == "JPEG":
                opciones = {"quality": 85, "optimize": True}
            elif formato == "PNG":
                opciones = {"optimize": True}

            miniatura.save(destino, format=formato, **opciones)
            return miniatura.size
    except Exception as exc:
        raise ImagenInvalida(f"Miniatura: {type(exc).__name__}: {exc}") from exc


def miniatura_valida(ruta: Path, max_lado):
    """Info de la miniatura si existe, se decodifica y respeta max_lado; si no, None."""
    if not ruta.is_file():
        return None

    try:
        info = inspeccionar_imagen(ruta)
    except ImagenInvalida:
        return None

    return info if max(info["ancho"], info["alto"]) <= max_lado else None


def descargar_imagen(
    url,
    ruta_part: Path,
    abrir,
    limitador,
    *,
    timeout,
    reintentos,
    dormir=time.sleep,
    imprimir=print,
    parar_ante_bloqueo=False,
    max_bytes=MAX_BYTES_IMAGEN,
):
    """
    Descarga url a ruta_part (en streaming) y valida lo básico: HTTP 200,
    Content-Type de imagen, cuerpo completo y no vacío. La validación con
    Pillow y el paso a nombre definitivo los hace el llamador.

    Devuelve un dict con estado (OK/HTTP_ERROR/CONTENT_TYPE_INVALIDO/VACIA/
    IMAGEN_CORRUPTA/ERROR_RED), http_status, intentos, content_type, bytes,
    cabeceras, error y bloqueo (se vio una señal de rate limiting o bloqueo).
    """
    peticion = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": IMAGEN_ACCEPT,
            "Accept-Encoding": "identity",
        },
    )
    resultado = {"url": url, "intentos": 0}

    def leer(respuesta):
        headers = respuesta.headers
        content_type = headers.get("Content-Type", "") if headers else ""
        resultado.update(
            http_status=respuesta.status,
            content_type=content_type,
            cabeceras=cabeceras_a_dict(headers),
        )

        if respuesta.status != 200:
            resultado.update(
                estado=EST_IMG_HTTP_ERROR,
                error=f"HTTP {respuesta.status} inesperado.",
            )
            return

        if not content_type_es_imagen(content_type):
            muestra = respuesta.read(4096)
            resultado.update(
                bytes=len(muestra),
                muestra=muestra_cuerpo(muestra),
                estado=EST_IMG_CONTENT_TYPE_INVALIDO,
                error=f"Content-Type no es de imagen: {content_type!r}",
            )
            if detectar_bloqueo(respuesta.status, headers, muestra):
                resultado["bloqueo"] = True
            return

        try:
            destino = ruta_part.open("wb")
        except OSError as exc:
            raise ErrorEscrituraLocal(f"No se puede escribir {ruta_part}: {exc}") from exc

        total = 0
        with destino:
            while True:
                bloque = respuesta.read(TAMANO_BLOQUE)

                if not bloque:
                    break

                total += len(bloque)

                if total > max_bytes:
                    resultado.update(
                        bytes=total,
                        estado=EST_IMG_CORRUPTA,
                        error=f"Supera el tamaño máximo permitido ({max_bytes} bytes).",
                    )
                    return

                try:
                    destino.write(bloque)
                except OSError as exc:
                    raise ErrorEscrituraLocal(f"No se puede escribir {ruta_part}: {exc}") from exc

        resultado["bytes"] = total
        declarado = headers.get("Content-Length") if headers else None

        if declarado and declarado.strip().isdigit() and int(declarado) != total:
            raise http.client.IncompleteRead(b"", max(int(declarado) - total, 0))

        if total == 0:
            resultado.update(estado=EST_IMG_VACIA, error="La respuesta no tiene contenido.")
            return

        resultado["estado"] = EST_IMG_OK

    return ejecutar_peticion(
        peticion,
        abrir,
        limitador,
        resultado,
        leer,
        timeout=timeout,
        reintentos=reintentos,
        dormir=dormir,
        imprimir=imprimir,
        estado_http=lambda status: EST_IMG_HTTP_ERROR,
        estado_red=EST_IMG_ERROR_RED,
        estado_recuperable=EST_IMG_HTTP_ERROR,
        parar_ante_bloqueo=parar_ante_bloqueo,
    )


def procesar_imagen(
    fichero,
    imagenes_dir: Path,
    abrir,
    limitador,
    *,
    thumb_max,
    timeout,
    reintentos,
    parar_ante_bloqueo=False,
    dormir=time.sleep,
    imprimir=print,
):
    """
    Deja una imagen completa y su miniatura en imagenes_dir/full y /thumb.

    Reanudación: original válido y miniatura válida -> se reutilizan; original
    válido sin miniatura válida -> solo se regenera la miniatura; original
    ausente o corrupto -> se vuelve a descargar. Todo se escribe en .part y
    solo pasa al nombre definitivo tras validarse.
    """
    nombre = nombre_local_seguro(fichero)
    ruta_full = imagenes_dir / "full" / nombre
    ruta_thumb = imagenes_dir / "thumb" / nombre
    part_full = ruta_full.with_name(nombre + ".part")
    part_thumb = ruta_thumb.with_name(nombre + ".part")

    r = {
        "url": construir_url_imagen(fichero),
        "nombre_local": nombre,
        "estado": None,
        "accion": "",
        "http_status": "",
        "intentos": 0,
        "content_type": "",
        "bytes": "",
        "formato": "",
        "ancho": "",
        "alto": "",
        "miniatura_ancho": "",
        "miniatura_alto": "",
        "cabeceras": {},
        "error": "",
        "bloqueo": False,
    }

    def anotar_imagen(info):
        r.update(formato=info["formato"], ancho=info["ancho"], alto=info["alto"])

    existente = None
    if ruta_full.is_file():
        try:
            existente = inspeccionar_imagen(ruta_full)
        except ImagenInvalida:
            existente = None

    if existente is not None:
        anotar_imagen(existente)
        r["bytes"] = ruta_full.stat().st_size
        mini = miniatura_valida(ruta_thumb, thumb_max)

        if mini is not None:
            r.update(
                estado=EST_IMG_OK,
                accion=ACCION_REUTILIZADA,
                miniatura_ancho=mini["ancho"],
                miniatura_alto=mini["alto"],
            )
            return r

        r["accion"] = ACCION_SOLO_MINIATURA
        try:
            ancho, alto = generar_miniatura(ruta_full, part_thumb, thumb_max)
            os.replace(part_thumb, ruta_thumb)
            r.update(estado=EST_IMG_OK, miniatura_ancho=ancho, miniatura_alto=alto)
        except ImagenInvalida as exc:
            r.update(estado=EST_IMG_CORRUPTA, error=str(exc))
        finally:
            part_thumb.unlink(missing_ok=True)

        return r

    ruta_full.unlink(missing_ok=True)
    ruta_thumb.unlink(missing_ok=True)
    r["accion"] = ACCION_DESCARGA

    descarga = descargar_imagen(
        r["url"],
        part_full,
        abrir,
        limitador,
        timeout=timeout,
        reintentos=reintentos,
        dormir=dormir,
        imprimir=imprimir,
        parar_ante_bloqueo=parar_ante_bloqueo,
    )
    r.update(
        estado=descarga["estado"],
        http_status=descarga.get("http_status", ""),
        intentos=descarga["intentos"],
        content_type=descarga.get("content_type", ""),
        bytes=descarga.get("bytes", ""),
        cabeceras=descarga.get("cabeceras", {}),
        error=" | ".join(
            t for t in (descarga.get("error", ""), descarga.get("muestra", "")) if t
        ),
        bloqueo=descarga.get("bloqueo", False),
    )

    try:
        if r["estado"] == EST_IMG_OK:
            try:
                anotar_imagen(inspeccionar_imagen(part_full))
                ancho, alto = generar_miniatura(part_full, part_thumb, thumb_max)
            except ImagenInvalida as exc:
                r.update(estado=EST_IMG_CORRUPTA, error=str(exc))
            else:
                os.replace(part_full, ruta_full)
                os.replace(part_thumb, ruta_thumb)
                r.update(miniatura_ancho=ancho, miniatura_alto=alto)
    finally:
        part_full.unlink(missing_ok=True)
        part_thumb.unlink(missing_ok=True)

    return r


def preparar_directorio_imagenes(imagenes_dir: Path, resume):
    """Crea full/ y thumb/. Sin resume descarta los de una ejecución anterior."""
    for subdirectorio in ("full", "thumb"):
        ruta = imagenes_dir / subdirectorio

        if not resume and ruta.exists():
            shutil.rmtree(ruta)

        ruta.mkdir(parents=True, exist_ok=True)


def procesar_imagenes(
    filas_imagenes,
    imagenes_dir: Path,
    log_path: Path,
    abrir,
    limitador,
    *,
    thumb_max,
    timeout,
    reintentos,
    maxim_imagenes=None,
    parar_ante_bloqueo=False,
    max_errores_consecutivos=0,
    dormir=time.sleep,
    imprimir=print,
):
    """
    Descarga cada imagen física UNA sola vez (deduplicada por fichero) aunque
    la compartan varios Gis_ID. Secuencial. Escribe log_imatges.csv (una fila
    por fichero único). Devuelve {"por_fichero", "resumen"}, donde por_fichero
    da RUTA_IMAGEN, RUTA_MINIATURA y ESTADO_DESCARGA de cada fichero.
    """
    unicas = OrderedDict()
    relaciones = OrderedDict()

    for fila in filas_imagenes:
        fichero = fila["NOMBRE_FICHERO_ORIGEN"]
        unicas.setdefault(fichero, fila["ID_IMAGEN_ORIGEN"])
        relaciones[fichero] = relaciones.get(fichero, 0) + 1

    seleccion = list(unicas)[:maxim_imagenes] if maxim_imagenes is not None else list(unicas)
    seleccionadas = set(seleccion)

    por_fichero = {}
    estados = Counter()
    acciones = Counter()
    intentos_http = 0
    bytes_descargados = 0
    senales_bloqueo = 0
    errores_consecutivos = 0
    detenido_bloqueo = False
    detenido_errores = False

    def escribir(writer, f_log, fichero, r, estado=None, motivo=""):
        writer.writerow({
            "MOMENTO": datetime.now().isoformat(timespec="seconds"),
            "ID_IMAGEN_ORIGEN": unicas[fichero],
            "NOMBRE_FICHERO": fichero,
            "NOMBRE_LOCAL": r["nombre_local"] if r else nombre_local_seguro(fichero),
            "URL_ORIGEN": construir_url_imagen(fichero),
            "ESTADO": estado or r["estado"],
            "ACCION": r["accion"] if r else "",
            "HTTP_STATUS": r["http_status"] if r else "",
            "INTENTOS": r["intentos"] if r else 0,
            "CONTENT_TYPE": r["content_type"] if r else "",
            "BYTES": r["bytes"] if r else "",
            "FORMATO": r["formato"] if r else "",
            "ANCHO": r["ancho"] if r else "",
            "ALTO": r["alto"] if r else "",
            "MINIATURA_ANCHO": r["miniatura_ancho"] if r else "",
            "MINIATURA_ALTO": r["miniatura_alto"] if r else "",
            "CABECERAS": json.dumps(r["cabeceras"], ensure_ascii=False) if r else "",
            "ERROR": r["error"] if r else motivo,
        })
        f_log.flush()

    with log_path.open("w", encoding="utf-8-sig", newline="") as f_log:
        writer = csv.DictWriter(f_log, fieldnames=CAMPOS_LOG_IMAGENES, delimiter=";")
        writer.writeheader()
        f_log.flush()

        tratadas = set()

        for posicion, fichero in enumerate(seleccion, start=1):
            r = procesar_imagen(
                fichero,
                imagenes_dir,
                abrir,
                limitador,
                thumb_max=thumb_max,
                timeout=timeout,
                reintentos=reintentos,
                parar_ante_bloqueo=parar_ante_bloqueo,
                dormir=dormir,
                imprimir=imprimir,
            )
            tratadas.add(fichero)
            escribir(writer, f_log, fichero, r)

            estados[r["estado"]] += 1
            acciones[r["accion"]] += 1
            intentos_http += r["intentos"]
            if r["accion"] == ACCION_DESCARGA and r["estado"] == EST_IMG_OK:
                bytes_descargados += r["bytes"]
            if r["bloqueo"]:
                senales_bloqueo += 1

            correcta = r["estado"] == EST_IMG_OK
            por_fichero[fichero] = {
                "RUTA_IMAGEN": f"full/{r['nombre_local']}" if correcta else "",
                "RUTA_MINIATURA": f"thumb/{r['nombre_local']}" if correcta else "",
                "ESTADO_DESCARGA": r["estado"],
            }
            imprimir(
                f"[{posicion}/{len(seleccion)}] {fichero}: {r['estado']}"
                + (f" ({r['accion']})" if r["accion"] else "")
            )

            errores_consecutivos = 0 if correcta else errores_consecutivos + 1

            if r["bloqueo"] and parar_ante_bloqueo:
                detenido_bloqueo = True
                imprimir(
                    "Señal de rate limiting o bloqueo detectada: se detiene la "
                    "descarga de imágenes. Se conservan los resultados obtenidos."
                )
                break

            if max_errores_consecutivos > 0 and errores_consecutivos >= max_errores_consecutivos:
                detenido_errores = True
                imprimir(
                    f"Detenido: {errores_consecutivos} imágenes seguidas con error."
                )
                break

        for fichero in unicas:
            if fichero in tratadas:
                continue

            motivo = (
                "Fuera de la selección (--maxim-imatges)."
                if fichero not in seleccionadas
                else "No intentada: la descarga se detuvo antes."
            )
            por_fichero[fichero] = {
                "RUTA_IMAGEN": "",
                "RUTA_MINIATURA": "",
                "ESTADO_DESCARGA": EST_IMG_PENDIENTE,
            }
            estados[EST_IMG_PENDIENTE] += 1
            escribir(writer, f_log, fichero, None, estado=EST_IMG_PENDIENTE, motivo=motivo)

    total_relaciones = sum(relaciones.values())

    return {
        "por_fichero": por_fichero,
        "resumen": {
            "relaciones_gis_id_imagen": total_relaciones,
            "ficheros_unicos": len(unicas),
            "descargas_evitadas_por_deduplicacion": total_relaciones - len(unicas),
            "seleccionados": len(seleccion),
            "estados": dict(sorted(estados.items())),
            "acciones": dict(sorted(acciones.items())),
            "intentos_http": intentos_http,
            "bytes_descargados": bytes_descargados,
            "senales_bloqueo": senales_bloqueo,
            "detenido_por_bloqueo": detenido_bloqueo,
            "detenido_por_errores": detenido_errores,
            "miniatura_max": thumb_max,
        },
    }


# ---------------------------------------------------------------------------
# Programa principal
# ---------------------------------------------------------------------------

def generar_salidas(
    modo,
    inventario,
    estadisticas_inventario,
    markers,
    estadisticas_markers,
    output_dir: Path,
    entradas,
    estados_consulta=None,
    extra=None,
    procesar_imagenes=None,
):
    """
    Camino común a offline y online: cruce, CSV y resumen.

    procesar_imagenes (opcional, Fase 3): callable(filas_imagenes) que descarga
    cada fichero único y devuelve {"por_fichero", "resumen"}; añade
    RUTA_IMAGEN, RUTA_MINIATURA y ESTADO_DESCARGA a cada relación.
    """
    filas_control, filas_imagenes = cruzar(inventario, markers, estados_consulta)

    campos_imagenes = CAMPOS_IMAGENES
    resumen_descarga = {}

    if procesar_imagenes is not None:
        descarga = procesar_imagenes(filas_imagenes)

        for fila in filas_imagenes:
            fila.update(descarga["por_fichero"][fila["NOMBRE_FICHERO_ORIGEN"]])

        campos_imagenes = CAMPOS_IMAGENES + CAMPOS_DESCARGA
        resumen_descarga = {"descarga": descarga["resumen"]}

    resumen = construir_resumen(
        inventario,
        estadisticas_inventario,
        markers,
        estadisticas_markers,
        filas_control,
        filas_imagenes,
    )
    resumen = {
        "modo": modo,
        "generado_en": datetime.now().isoformat(timespec="seconds"),
        "entradas": entradas,
        **resumen,
        **(extra or {}),
        **resumen_descarga,
    }

    escribir_csv_atomico(
        output_dir / "CONTROL_GIS_ID.csv",
        CAMPOS_CONTROL,
        (formatear_fila_control(f) for f in filas_control),
    )
    escribir_csv_atomico(
        output_dir / "IMATGES_INFOANTENAS.csv",
        campos_imagenes,
        (formatear_fila_imagen(f) for f in filas_imagenes),
    )
    escribir_json_atomico(output_dir / f"resum_{modo}.json", resumen)

    return resumen


def ejecutar(inventario_path: Path, raw_path: Path, output_dir: Path):
    """Modo offline: markers_raw.jsonl -> parser común -> cruce común -> CSV."""
    inventario, estadisticas_inventario = leer_inventario(inventario_path)
    markers, estadisticas_raw = leer_markers_raw(raw_path)

    return generar_salidas(
        "offline",
        inventario,
        estadisticas_inventario,
        markers,
        estadisticas_raw,
        output_dir,
        {
            "inventario": str(inventario_path),
            "inventario_sha256": sha256_fichero(inventario_path),
            "markers_raw": str(raw_path),
            "markers_raw_sha256": sha256_fichero(raw_path),
        },
    )


def ejecutar_online(
    inventario_path: Path,
    output_dir: Path,
    trabajo_dir: Path,
    *,
    abrir,
    limitador,
    parametros,
    resume=False,
    gis_ids=None,
    maxim=None,
    max_errores_consecutivos=0,
    descarga=None,
    dormir=time.sleep,
    imprimir=print,
):
    """
    Modo online: consulta /v1/markers, guarda una respuesta por Gis_ID y sigue
    el mismo camino que el offline. Devuelve (resumen, consulta); resumen es
    None si la ejecución se detuvo por errores consecutivos.

    descarga (opcional, Fase 3): dict con abrir, limitador, imagenes_dir,
    thumb_max, reintentos, maxim_imagenes y parar_ante_bloqueo. Si se indica,
    se descargan las imágenes y se generan las miniaturas.
    """
    inventario_completo, estadisticas_inventario = leer_inventario(inventario_path)
    inventario_sha256 = sha256_fichero(inventario_path)
    seleccion = seleccionar_gis_ids(inventario_completo, gis_ids, maxim)

    run = preparar_trabajo(
        trabajo_dir, inventario_path, inventario_sha256, parametros, resume
    )
    markers_dir = trabajo_dir / "markers"

    procesar = None
    if descarga is not None:
        _exigir_pillow()
        preparar_directorio_imagenes(descarga["imagenes_dir"], resume)

        def procesar(filas_imagenes):
            return procesar_imagenes(
                filas_imagenes,
                descarga["imagenes_dir"],
                trabajo_dir / "log_imatges.csv",
                descarga["abrir"],
                descarga["limitador"],
                thumb_max=descarga["thumb_max"],
                timeout=parametros["timeout"],
                reintentos=descarga["reintentos"],
                maxim_imagenes=descarga.get("maxim_imagenes"),
                parar_ante_bloqueo=descarga.get("parar_ante_bloqueo", False),
                max_errores_consecutivos=max_errores_consecutivos,
                dormir=dormir,
                imprimir=imprimir,
            )

    consulta = consultar_pendientes(
        seleccion,
        markers_dir,
        trabajo_dir / "log_markers.csv",
        abrir,
        limitador,
        timeout=parametros["timeout"],
        reintentos=parametros["reintentos"],
        max_errores_consecutivos=max_errores_consecutivos,
        dormir=dormir,
        imprimir=imprimir,
    )

    if consulta["detenido"]:
        return None, consulta

    inventario = OrderedDict((g, inventario_completo[g]) for g in seleccion)
    markers, estadisticas_markers = leer_markers_dir(markers_dir, seleccion)

    resumen = generar_salidas(
        "online",
        inventario,
        estadisticas_inventario,
        markers,
        estadisticas_markers,
        output_dir,
        {
            "inventario": str(inventario_path),
            "inventario_sha256": inventario_sha256,
            "markers_dir": str(markers_dir),
            "trabajo_dir": str(trabajo_dir),
        },
        estados_consulta=consulta["estados"],
        extra={
            "seleccion": {
                "gis_id_en_inventario": len(inventario_completo),
                "gis_id_seleccionados": len(seleccion),
            },
            "consulta": {
                "estados": dict(sorted(consulta["contadores"].items())),
                "intentos_http_esta_invocacion": consulta["intentos_http"],
                "reanudacion": bool(resume),
                "inicio_ejecucion": run["inicio"],
            },
        },
        procesar_imagenes=procesar,
    )

    return resumen, consulta


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Cruce Infoantenas <-> antenasmoviles /markers e imágenes. "
            "Modo offline (markers_raw.jsonl) u online (consulta /v1/markers)."
        )
    )
    parser.add_argument("--modo", choices=("offline", "online"), default="offline")
    parser.add_argument("--inventario", default=DEFAULT_INVENTARIO)
    parser.add_argument("--raw", default=DEFAULT_RAW, help="Solo offline.")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--trabajo-dir",
        help="Solo online. Por defecto: <output-dir>/_treball.",
    )
    parser.add_argument(
        "--peticions-per-minut",
        type=float,
        default=DEFAULT_PETICIONS_PER_MINUT,
        help="Solo online. Por defecto: 10.",
    )
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Segundos.")
    parser.add_argument(
        "--reintents",
        type=int,
        default=DEFAULT_REINTENTS,
        help="Reintentos adicionales por Gis_ID. Por defecto: 3.",
    )
    parser.add_argument(
        "--maxim",
        type=int,
        help="Solo online. Trata únicamente los primeros N Gis_ID.",
    )
    parser.add_argument(
        "--gis-id",
        action="append",
        dest="gis_ids",
        help="Solo online. Restringe a este Gis_ID (repetible), para pruebas.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Solo online. Reanuda una ejecución interrumpida (mismo inventario).",
    )
    parser.add_argument(
        "--max-errores-consecutivos",
        type=int,
        default=0,
        help=(
            "Solo online. Detiene tras N errores técnicos seguidos (consultas o "
            "descargas de imagen). 0 = desactivado."
        ),
    )
    parser.add_argument(
        "--descargar-imagenes",
        action="store_true",
        help="Solo online. Descarga las imágenes completas y genera miniaturas locales.",
    )
    parser.add_argument(
        "--imatges-per-minut",
        type=float,
        default=DEFAULT_IMATGES_PER_MINUT,
        help="Descargas de imagen por minuto, independiente de /markers. Por defecto: 5.",
    )
    parser.add_argument(
        "--imatges-reintents",
        type=int,
        default=DEFAULT_IMATGES_REINTENTS,
        help="Reintentos adicionales por imagen. Por defecto: 2.",
    )
    parser.add_argument(
        "--miniatura-max",
        type=int,
        default=DEFAULT_MINIATURA_MAX,
        help="Lado máximo de la miniatura en píxeles. Por defecto: 300.",
    )
    parser.add_argument(
        "--maxim-imatges",
        type=int,
        help="Trata solo los primeros N ficheros de imagen únicos; el resto queda PENDIENTE.",
    )
    parser.add_argument(
        "--imagenes-dir",
        help="Directorio con full/ y thumb/. Por defecto: <output-dir>/imagenes.",
    )
    parser.add_argument(
        "--parar-ante-bloqueo",
        action="store_true",
        help=(
            "Detiene la descarga al primer HTTP 429/403 o desafío de Cloudflare, "
            "sin reintentar ni seguir con otras imágenes."
        ),
    )
    args = parser.parse_args(argv)

    solo_online = (
        args.resume
        or args.maxim is not None
        or args.gis_ids
        or args.trabajo_dir
        or args.descargar_imagenes
    )
    if args.modo == "offline" and solo_online:
        parser.error(
            "--resume, --maxim, --gis-id, --trabajo-dir y --descargar-imagenes "
            "requieren --modo online."
        )

    if (
        args.maxim_imatges is not None or args.imagenes_dir or args.parar_ante_bloqueo
    ) and not args.descargar_imagenes:
        parser.error(
            "--maxim-imatges, --imagenes-dir y --parar-ante-bloqueo requieren "
            "--descargar-imagenes."
        )

    if args.peticions_per_minut <= 0 or args.imatges_per_minut <= 0:
        parser.error("--peticions-per-minut e --imatges-per-minut deben ser mayores que 0.")
    if args.timeout <= 0:
        parser.error("--timeout debe ser mayor que 0.")
    if args.reintents < 0 or args.imatges_reintents < 0:
        parser.error("--reintents e --imatges-reintents no pueden ser negativos.")
    if args.maxim is not None and args.maxim <= 0:
        parser.error("--maxim debe ser mayor que 0.")
    if args.maxim_imatges is not None and args.maxim_imatges <= 0:
        parser.error("--maxim-imatges debe ser mayor que 0.")
    if args.miniatura_max <= 0:
        parser.error("--miniatura-max debe ser mayor que 0.")
    if args.max_errores_consecutivos < 0:
        parser.error("--max-errores-consecutivos no puede ser negativo.")

    inventario_path = Path(args.inventario)
    output_dir = Path(args.output_dir)

    if not inventario_path.is_file():
        raise FileNotFoundError(f"No existe el inventario: {inventario_path}")

    if args.modo == "offline":
        raw_path = Path(args.raw)

        if not raw_path.is_file():
            raise FileNotFoundError(f"No existe el fichero de entrada: {raw_path}")

        resumen = ejecutar(inventario_path, raw_path, output_dir)
        codigo_salida = 0
    else:
        trabajo_dir = Path(args.trabajo_dir) if args.trabajo_dir else output_dir / "_treball"
        parametros = {
            "peticions_per_minut": args.peticions_per_minut,
            "timeout": args.timeout,
            "reintentos": args.reintents,
            "maxim": args.maxim,
            "gis_ids": args.gis_ids,
            "max_errores_consecutivos": args.max_errores_consecutivos,
            "descargar_imagenes": args.descargar_imagenes,
        }
        abrir = crear_apertura()
        descarga = None

        if args.descargar_imagenes:
            imagenes_dir = Path(args.imagenes_dir) if args.imagenes_dir else output_dir / "imagenes"
            descarga = {
                "abrir": abrir,
                "limitador": Limitador(60.0 / args.imatges_per_minut),
                "imagenes_dir": imagenes_dir,
                "thumb_max": args.miniatura_max,
                "reintentos": args.imatges_reintents,
                "maxim_imagenes": args.maxim_imatges,
                "parar_ante_bloqueo": args.parar_ante_bloqueo,
            }
            parametros.update(
                imatges_per_minut=args.imatges_per_minut,
                imatges_reintentos=args.imatges_reintents,
                miniatura_max=args.miniatura_max,
                maxim_imatges=args.maxim_imatges,
                parar_ante_bloqueo=args.parar_ante_bloqueo,
            )
            print(f"Imágenes: {args.imatges_per_minut:g} descargas/minuto -> {imagenes_dir}")

        print(f"Ritmo: {args.peticions_per_minut:g} peticiones/minuto")
        print(f"Trabajo: {trabajo_dir}")

        resumen, consulta = ejecutar_online(
            inventario_path,
            output_dir,
            trabajo_dir,
            abrir=abrir,
            limitador=Limitador(60.0 / args.peticions_per_minut),
            parametros=parametros,
            resume=args.resume,
            gis_ids=args.gis_ids,
            maxim=args.maxim,
            max_errores_consecutivos=args.max_errores_consecutivos,
            descarga=descarga,
        )

        if resumen is None:
            return 3

        hay_errores = any(e in ESTADOS_ERROR_TECNICO for e in consulta["estados"].values())
        codigo_salida = 2 if hay_errores else 0

        resumen_descarga = resumen.get("descarga")
        if resumen_descarga:
            estados_imagen = resumen_descarga["estados"]

            if any(e not in (EST_IMG_OK, EST_IMG_PENDIENTE) for e in estados_imagen):
                codigo_salida = 2
            if resumen_descarga["detenido_por_errores"]:
                codigo_salida = 3
            if resumen_descarga["detenido_por_bloqueo"]:
                codigo_salida = 4

    print(json.dumps(
        {k: v for k, v in resumen.items() if k != "entradas"},
        ensure_ascii=False,
        indent=2,
    ))
    print(f"Salida: {output_dir.resolve()}")

    return codigo_salida


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        print("Proceso interrumpido. Relanza con --resume para continuar.")
        sys.exit(130)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
