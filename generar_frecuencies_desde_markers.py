#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Genera FREQUENCIES_ANTENASMOVILES.csv a partir de los JSON ya guardados en
<version>/markers/. Envoltura fina: la lógica vive en
extraer_imagenes_infoantenas.generar_frecuencias (la misma que usa la
extracción), sin peticiones de red.
"""

import argparse
import json
import sys
from pathlib import Path

import extraer_imagenes_infoantenas as extractor

MARKERS_DIR = Path(r"C:\datos\antenas\actual\markers")
OUTPUT_CSV = Path(r"C:\datos\antenas\actual") / extractor.NOMBRE_FRECUENCIAS


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--markers-dir", default=str(MARKERS_DIR))
    parser.add_argument("--output", default=str(OUTPUT_CSV))
    args = parser.parse_args(argv)

    markers_dir = Path(args.markers_dir)
    output = Path(args.output)

    if not markers_dir.is_dir():
        print(f"ERROR: no existe {markers_dir}", file=sys.stderr)
        return 1

    gis_ids, ilegibles = extractor.listar_gis_ids_guardados(markers_dir)
    resumen = extractor.generar_frecuencias(markers_dir, gis_ids, output)

    print("MARKERS PROCESADOS:", resumen["markers_procesados"])
    print("FILAS DE FRECUENCIAS:", resumen["filas"])
    print("ERRORES:", ilegibles + resumen["markers_no_disponibles"])
    print("CSV GENERADO:", output)
    print(json.dumps(resumen, ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())