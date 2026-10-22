#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Regenera Detalle_antenas.csv a partir de markers_raw.jsonl sin hacer peticiones HTTP.

Uso:
  python regenerar_detalle_desde_raw.py

Por defecto espera:
  entrada: salida_markers/markers_raw.jsonl
  salida : salida_markers/Detalle_antenas.csv

También admite rutas personalizadas:
  python regenerar_detalle_desde_raw.py --raw ruta/markers_raw.jsonl --output ruta/Detalle_antenas.csv
"""

import argparse
import csv
import json
from pathlib import Path


def procesar_respuesta(codigo_solicitado, data):
    detalles = []

    if not isinstance(data, list):
        return detalles

    for elemento in data:
        if not isinstance(elemento, list):
            continue

        empresa = ""
        codigo_operador = ""
        if len(elemento) > 1 and isinstance(elemento[1], list):
            if len(elemento[1]) > 0:
                empresa = elemento[1][0]
            if len(elemento[1]) > 1:
                codigo_operador = elemento[1][1]

        geo = elemento[2] if len(elemento) > 2 and isinstance(elemento[2], list) else []

        detalles.append({
            "codigo_antena": codigo_solicitado,
            "codigo_operador": codigo_operador,
            "empresa_propietaria": empresa,
            "direccion": geo[0] if len(geo) > 0 else "",
            "latitud": geo[1] if len(geo) > 1 else "",
            "longitud": geo[2] if len(geo) > 2 else "",
            "altura": geo[3] if len(geo) > 3 else "",
        })

    return detalles


def main():
    parser = argparse.ArgumentParser(description="Regenera Detalle_antenas.csv desde markers_raw.jsonl.")
    parser.add_argument("--raw", default="salida_markers/markers_raw.jsonl", help="Ruta al markers_raw.jsonl")
    parser.add_argument("--output", default="salida_markers/Detalle_antenas.csv", help="CSV de salida")
    args = parser.parse_args()

    raw_path = Path(args.raw)
    output_path = Path(args.output)

    if not raw_path.exists():
        raise FileNotFoundError(f"No existe el fichero RAW: {raw_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "codigo_antena",
        "codigo_operador",
        "empresa_propietaria",
        "direccion",
        "latitud",
        "longitud",
        "altura",
    ]

    total_lineas = 0
    total_detalles = 0
    errores = 0

    with raw_path.open("r", encoding="utf-8") as fin, output_path.open("w", encoding="utf-8-sig", newline="") as fout:
        writer = csv.DictWriter(fout, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()

        for num_linea, linea in enumerate(fin, start=1):
            linea = linea.strip()
            if not linea:
                continue

            total_lineas += 1
            try:
                registro = json.loads(linea)
                codigo = str(registro.get("codigo_antena", "") or "").strip()
                data = registro.get("data")

                if not codigo:
                    raise ValueError("Falta codigo_antena")

                detalles = procesar_respuesta(codigo, data)
                writer.writerows(detalles)
                total_detalles += len(detalles)

            except Exception as exc:
                errores += 1
                print(f"AVISO línea {num_linea}: {exc}")

    print("Regeneración finalizada.")
    print(f"Respuestas leídas : {total_lineas}")
    print(f"Filas de detalle : {total_detalles}")
    print(f"Errores           : {errores}")
    print(f"Salida            : {output_path.resolve()}")


if __name__ == "__main__":
    main()
