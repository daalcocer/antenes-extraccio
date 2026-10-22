from __future__ import annotations

from datetime import datetime
from pathlib import Path
import argparse
import csv
import hashlib
import json
import math
import os

from shapely import make_valid
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon
from shapely.geometry import box, mapping, shape
from shapely.ops import unary_union
from shapely.prepared import prep
from shapely.validation import explain_validity


# Dimensions de la bbox d'exemple documentada per a zoom 7.
AMPLADA_CELLA_PER_DEFECTE = 2.0210739694239 - 1.9936939797999
ALCADA_CELLA_PER_DEFECTE = 41.412410902009 - 41.363916563019
SOLAPAMENT_PERCENT_PER_DEFECTE = 10.0


def llegir_arguments():
    parser = argparse.ArgumentParser(
        description=(
            "Genera les bbox d'Infoantenas que intersecten Catalunya "
            "i valida que cobreixen tot el territori."
        )
    )
    parser.add_argument(
        "limit_catalunya",
        help="GeoJSON oficial amb el límit de Catalunya.",
    )
    parser.add_argument(
        "--solapament-percent",
        type=float,
        default=SOLAPAMENT_PERCENT_PER_DEFECTE,
        help="Percentatge de solapament entre cel·les. Per defecte: 10.",
    )
    parser.add_argument(
        "--amplada-cella",
        type=float,
        default=AMPLADA_CELLA_PER_DEFECTE,
        help="Amplada de cada bbox en graus de longitud.",
    )
    parser.add_argument(
        "--alcada-cella",
        type=float,
        default=ALCADA_CELLA_PER_DEFECTE,
        help="Alçada de cada bbox en graus de latitud.",
    )
    parser.add_argument(
        "--fitxer-csv",
        help=(
            "CSV de sortida. Per defecte: "
            "config/bbox_catalunya_infoantenas.csv."
        ),
    )
    parser.add_argument(
        "--fitxer-geojson",
        help=(
            "GeoJSON de previsualització. Per defecte: "
            "config/bbox_catalunya_infoantenas.geojson."
        ),
    )
    parser.add_argument(
        "--fitxer-resum",
        help=(
            "JSON de metadades. Per defecte: "
            "config/bbox_catalunya_infoantenas_resum.json."
        ),
    )
    return parser.parse_args()


def carregar_geojson(ruta):
    contingut = ruta.read_bytes()
    ultim_error = None
    for codificacio in ("utf-8-sig", "iso-8859-1"):
        try:
            return json.loads(contingut.decode(codificacio))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            ultim_error = error
    raise ValueError(f"No es pot interpretar el GeoJSON: {ultim_error}")


def extreure_poligons(geometria):
    if geometria.is_empty:
        return []
    if isinstance(geometria, Polygon):
        return [geometria]
    if isinstance(geometria, MultiPolygon):
        return list(geometria.geoms)
    if isinstance(geometria, GeometryCollection):
        resultat = []
        for part in geometria.geoms:
            resultat.extend(extreure_poligons(part))
        return resultat
    return []


def carregar_limit(ruta):
    dades = carregar_geojson(ruta)
    if dades.get("type") != "FeatureCollection":
        raise ValueError("El límit no és una FeatureCollection.")
    elements = dades.get("features")
    if not isinstance(elements, list) or len(elements) != 1:
        raise ValueError(
            "El límit ha de contenir exactament un element territorial."
        )

    propietats = elements[0].get("properties") or {}
    if propietats.get("NOMCAT") != "Catalunya":
        raise ValueError(
            "L'element territorial no està identificat com Catalunya."
        )

    geometria = shape(elements[0].get("geometry"))
    reparada = False
    motiu_reparacio = ""
    if not geometria.is_valid:
        reparada = True
        motiu_reparacio = explain_validity(geometria)
        geometria = make_valid(geometria)

    poligons = extreure_poligons(geometria)
    if not poligons:
        raise ValueError(
            "No s'ha pogut obtenir cap polígon vàlid del límit."
        )
    geometria_poligonal = unary_union(poligons)
    if geometria_poligonal.is_empty or not geometria_poligonal.is_valid:
        raise ValueError(
            "La geometria poligonal reparada no és vàlida."
        )

    min_x, min_y, max_x, max_y = geometria_poligonal.bounds
    if not (
        -180 <= min_x < max_x <= 180
        and -90 <= min_y < max_y <= 90
    ):
        raise ValueError(
            "Les coordenades del límit no semblen estar en WGS84."
        )

    return geometria_poligonal, reparada, motiu_reparacio, propietats


def nombre_posicions(extensio, mida_cella, pas):
    if extensio <= mida_cella:
        return 1
    return math.ceil((extensio - mida_cella) / pas) + 1


def escriure_atomic_text(ruta, text, encoding):
    ruta.parent.mkdir(parents=True, exist_ok=True)
    temporal = ruta.with_suffix(ruta.suffix + ".part")
    temporal.write_text(text, encoding=encoding, newline="")
    os.replace(temporal, ruta)


def main():
    arguments = llegir_arguments()

    if not 0 <= arguments.solapament_percent < 50:
        raise ValueError(
            "El solapament ha d'estar entre 0 i menys de 50%."
        )
    if arguments.amplada_cella <= 0 or arguments.alcada_cella <= 0:
        raise ValueError("Les dimensions de la cel·la han de ser positives.")

    ruta_limit = Path(arguments.limit_catalunya)
    if not ruta_limit.exists():
        raise FileNotFoundError(f"No existeix el límit: {ruta_limit}")

    arrel_projecte = Path(__file__).resolve().parent.parent
    ruta_csv = Path(arguments.fitxer_csv) if arguments.fitxer_csv else (
        arrel_projecte / "config" / "bbox_catalunya_infoantenas.csv"
    )
    ruta_geojson = Path(arguments.fitxer_geojson) if arguments.fitxer_geojson else (
        arrel_projecte / "config" / "bbox_catalunya_infoantenas.geojson"
    )
    ruta_resum = Path(arguments.fitxer_resum) if arguments.fitxer_resum else (
        arrel_projecte / "config" / "bbox_catalunya_infoantenas_resum.json"
    )

    print("Carregant i validant el límit de Catalunya...")
    limit, reparada, motiu_reparacio, propietats = carregar_limit(ruta_limit)
    if reparada:
        print("El límit original necessita una reparació geomètrica en memòria.")
        print(f"Motiu: {motiu_reparacio}")
    else:
        print("El límit original és geomètricament vàlid.")

    solapament = arguments.solapament_percent / 100
    pas_x = arguments.amplada_cella * (1 - solapament)
    pas_y = arguments.alcada_cella * (1 - solapament)
    min_x, min_y, max_x, max_y = limit.bounds
    columnes = nombre_posicions(
        max_x - min_x,
        arguments.amplada_cella,
        pas_x,
    )
    files = nombre_posicions(
        max_y - min_y,
        arguments.alcada_cella,
        pas_y,
    )

    print("Generant les cel·les que intersecten Catalunya...")
    limit_preparat = prep(limit)
    seleccionades = []
    for fila in range(files):
        cel_min_y = min_y + fila * pas_y
        cel_max_y = cel_min_y + arguments.alcada_cella
        for columna in range(columnes):
            cel_min_x = min_x + columna * pas_x
            cel_max_x = cel_min_x + arguments.amplada_cella
            geometria_cella = box(
                cel_min_x,
                cel_min_y,
                cel_max_x,
                cel_max_y,
            )
            if limit_preparat.intersects(geometria_cella):
                seleccionades.append(
                    {
                        "fila_grid": fila,
                        "columna_grid": columna,
                        "min_longitud": cel_min_x,
                        "min_latitud": cel_min_y,
                        "max_longitud": cel_max_x,
                        "max_latitud": cel_max_y,
                        "geometry": geometria_cella,
                    }
                )

    if not seleccionades:
        raise ValueError("No s'ha generat cap bbox.")

    print("Validant la cobertura territorial completa...")
    cobertura = unary_union(
        [element["geometry"] for element in seleccionades]
    )
    area_no_coberta = limit.difference(cobertura).area
    tolerancia = max(limit.area * 1e-12, 1e-15)
    cobertura_correcta = area_no_coberta <= tolerancia
    if not cobertura_correcta:
        raise ValueError(
            "La quadrícula no cobreix tot Catalunya. "
            f"Àrea no coberta: {area_no_coberta}"
        )

    capcalera = [
        "bbox_id",
        "min_longitud",
        "min_latitud",
        "max_longitud",
        "max_latitud",
        "fila_grid",
        "columna_grid",
    ]
    ruta_csv.parent.mkdir(parents=True, exist_ok=True)
    temporal_csv = ruta_csv.with_suffix(ruta_csv.suffix + ".part")
    with temporal_csv.open(
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as fitxer:
        escriptor = csv.DictWriter(
            fitxer,
            fieldnames=capcalera,
            delimiter=";",
        )
        escriptor.writeheader()
        for index, element in enumerate(seleccionades, start=1):
            escriptor.writerow(
                {
                    "bbox_id": f"bbox_{index:05d}",
                    "min_longitud": f"{element['min_longitud']:.12f}",
                    "min_latitud": f"{element['min_latitud']:.12f}",
                    "max_longitud": f"{element['max_longitud']:.12f}",
                    "max_latitud": f"{element['max_latitud']:.12f}",
                    "fila_grid": element["fila_grid"],
                    "columna_grid": element["columna_grid"],
                }
            )
    os.replace(temporal_csv, ruta_csv)

    geojson = {
        "type": "FeatureCollection",
        "name": "bbox_catalunya_infoantenas",
        "features": [
            {
                "type": "Feature",
                "id": f"bbox_{index:05d}",
                "properties": {
                    "bbox_id": f"bbox_{index:05d}",
                    "fila_grid": element["fila_grid"],
                    "columna_grid": element["columna_grid"],
                },
                "geometry": mapping(element["geometry"]),
            }
            for index, element in enumerate(seleccionades, start=1)
        ],
    }
    escriure_atomic_text(
        ruta_geojson,
        json.dumps(geojson, ensure_ascii=False, separators=(",", ":")),
        "utf-8",
    )

    total_candidats = columnes * files
    hores_10_rpm = len(seleccionades) / 600
    resum = {
        "moment_generacio": datetime.now().isoformat(timespec="seconds"),
        "font_limit": str(ruta_limit),
        "sha256_limit": hashlib.sha256(ruta_limit.read_bytes()).hexdigest(),
        "territori": propietats.get("NOMCAT", ""),
        "limit_reparat_en_memoria": reparada,
        "motiu_reparacio": motiu_reparacio,
        "bounds_limit": [min_x, min_y, max_x, max_y],
        "amplada_cella_graus": arguments.amplada_cella,
        "alcada_cella_graus": arguments.alcada_cella,
        "solapament_percent": arguments.solapament_percent,
        "pas_longitud_graus": pas_x,
        "pas_latitud_graus": pas_y,
        "files_grid_rectangle": files,
        "columnes_grid_rectangle": columnes,
        "celles_candidates_rectangle": total_candidats,
        "bbox_seleccionades": len(seleccionades),
        "cobertura_completa_validada": cobertura_correcta,
        "area_no_coberta_graus_quadrats": area_no_coberta,
        "durada_minima_hores_a_10_pet_min": hores_10_rpm,
        "fitxer_csv": str(ruta_csv),
        "sha256_csv": hashlib.sha256(ruta_csv.read_bytes()).hexdigest(),
        "fitxer_geojson": str(ruta_geojson),
        "sha256_geojson": hashlib.sha256(
            ruta_geojson.read_bytes()
        ).hexdigest(),
    }
    escriure_atomic_text(
        ruta_resum,
        json.dumps(resum, ensure_ascii=False, indent=2),
        "utf-8",
    )

    print()
    print("Quadrícula generada i validada correctament.")
    print(f"Files del rectangle: {files}")
    print(f"Columnes del rectangle: {columnes}")
    print(f"Cel·les candidates del rectangle: {total_candidats}")
    print(f"Bbox que intersecten Catalunya: {len(seleccionades)}")
    print(f"Solapament: {arguments.solapament_percent:.1f}%")
    print("Cobertura completa de Catalunya: True")
    print(f"Durada mínima a 10 peticions/minut: {hores_10_rpm:.2f} hores")
    print(f"CSV generat: {ruta_csv}")
    print(f"GeoJSON de control: {ruta_geojson}")
    print(f"Resum generat: {ruta_resum}")


if __name__ == "__main__":
    main()
