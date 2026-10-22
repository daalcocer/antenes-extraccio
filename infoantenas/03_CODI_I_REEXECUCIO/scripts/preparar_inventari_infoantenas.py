from __future__ import annotations

from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
import argparse
import csv
import hashlib
import json
import math
import os

from shapely import make_valid
from shapely.geometry import GeometryCollection, MultiPolygon, Point, Polygon
from shapely.geometry import shape
from shapely.ops import unary_union
from shapely.prepared import prep
from shapely.validation import explain_validity


def llegir_arguments():
    parser = argparse.ArgumentParser(
        description=(
            "Valida i uneix els GeoJSON de les cel·les d'Infoantenas, "
            "elimina únicament les repeticions exactes entre cel·les "
            "solapades, filtra Catalunya i prepara els Gis_ID."
        )
    )
    parser.add_argument(
        "fitxer_bbox",
        help="CSV original amb totes les bbox consultades.",
    )
    parser.add_argument(
        "carpeta_json",
        help="Carpeta amb els GeoJSON descarregats per cel·la.",
    )
    parser.add_argument(
        "limit_catalunya",
        help="GeoJSON oficial amb el límit de Catalunya.",
    )
    parser.add_argument(
        "--carpeta-sortida",
        help=(
            "Carpeta dels inventaris i informes. Per defecte: "
            "data_output/extraccio_final."
        ),
    )
    parser.add_argument(
        "--fitxer-gis-ids",
        help=(
            "CSV de Gis_ID. Per defecte: "
            "config/gis_ids_infoantenas.csv."
        ),
    )
    return parser.parse_args()


def text_decimal(valor, camp, numero_fila):
    try:
        numero = Decimal(valor.strip())
    except (AttributeError, InvalidOperation) as error:
        raise ValueError(
            f"Valor no numèric a {camp}, fila {numero_fila}: {valor}"
        ) from error
    if not numero.is_finite():
        raise ValueError(
            f"Valor no finit a {camp}, fila {numero_fila}: {valor}"
        )
    resultat = format(numero, "f")
    if "." in resultat:
        resultat = resultat.rstrip("0").rstrip(".")
    return resultat or "0"


def llegir_bbox(ruta):
    consultes = []
    ids_detectats = set()
    with ruta.open("r", encoding="utf-8-sig", newline="") as fitxer:
        lector = csv.DictReader(fitxer, delimiter=";")
        camps = {
            "bbox_id",
            "min_longitud",
            "min_latitud",
            "max_longitud",
            "max_latitud",
        }
        if not lector.fieldnames:
            raise ValueError("El CSV de bbox no té capçalera.")
        faltants = camps - set(lector.fieldnames)
        if faltants:
            raise ValueError(
                "Falten camps obligatoris al CSV de bbox: "
                + ", ".join(sorted(faltants))
            )

        for numero_fila, fila in enumerate(lector, start=2):
            bbox_id = fila["bbox_id"].strip()
            if not bbox_id or not all(
                caracter.isalnum() or caracter in "_-"
                for caracter in bbox_id
            ):
                raise ValueError(
                    f"bbox_id invàlid a la fila {numero_fila}: {bbox_id}"
                )
            if bbox_id in ids_detectats:
                raise ValueError(f"bbox_id repetit: {bbox_id}")
            ids_detectats.add(bbox_id)

            noms = (
                "min_longitud",
                "min_latitud",
                "max_longitud",
                "max_latitud",
            )
            valors = [
                text_decimal(fila[nom], nom, numero_fila)
                for nom in noms
            ]
            min_lon, min_lat, max_lon, max_lat = map(float, valors)
            if not (-180 <= min_lon < max_lon <= 180):
                raise ValueError(
                    f"Longituds incorrectes a {bbox_id}: "
                    f"{min_lon}, {max_lon}"
                )
            if not (-90 <= min_lat < max_lat <= 90):
                raise ValueError(
                    f"Latituds incorrectes a {bbox_id}: "
                    f"{min_lat}, {max_lat}"
                )

            valor_bbox = ",".join(valors)
            hash_bbox = hashlib.sha256(
                valor_bbox.encode("ascii")
            ).hexdigest()[:12]
            consultes.append(
                {
                    "bbox_id": bbox_id,
                    "nom_fitxer": f"{bbox_id}_{hash_bbox}.json",
                }
            )
    if not consultes:
        raise ValueError("El CSV no conté cap bbox.")
    return consultes


def carregar_json(ruta):
    contingut = ruta.read_bytes()
    if not contingut or not contingut.strip():
        raise ValueError("el fitxer està buit")
    ultim_error = None
    for codificacio in ("utf-8-sig", "iso-8859-1"):
        try:
            dades = json.loads(contingut.decode(codificacio))
            return dades, contingut, codificacio
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            ultim_error = error
    raise ValueError(f"no es pot interpretar el JSON: {ultim_error}")


def carregar_resposta_infoantenas(ruta):
    dades, contingut, codificacio = carregar_json(ruta)
    if dades == {} or dades == []:
        return [], contingut, codificacio
    if not isinstance(dades, dict):
        raise ValueError("la resposta no és un objecte JSON")
    if dades.get("type") != "FeatureCollection":
        raise ValueError("la resposta no té type=FeatureCollection")
    registres = dades.get("features")
    if not isinstance(registres, list):
        raise ValueError("features no és una llista")
    for index, registre in enumerate(registres):
        if not isinstance(registre, dict):
            raise ValueError(f"el feature {index} no és un objecte")
    return registres, contingut, codificacio


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
    dades, _, _ = carregar_json(ruta)
    if not isinstance(dades, dict) or dades.get("type") != "FeatureCollection":
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
    motiu = ""
    if not geometria.is_valid:
        reparada = True
        motiu = explain_validity(geometria)
        geometria = make_valid(geometria)
    poligons = extreure_poligons(geometria)
    if not poligons:
        raise ValueError("No s'ha obtingut cap polígon del límit.")
    limit = unary_union(poligons)
    if limit.is_empty or not limit.is_valid:
        raise ValueError("La geometria reparada del límit no és vàlida.")
    min_x, min_y, max_x, max_y = limit.bounds
    if not (
        -180 <= min_x < max_x <= 180
        and -90 <= min_y < max_y <= 90
    ):
        raise ValueError(
            "Les coordenades del límit no semblen estar en WGS84."
        )
    return limit, reparada, motiu


def coordenades_punt(registre):
    geometria = registre.get("geometry")
    if not isinstance(geometria, dict):
        raise ValueError("geometry no és un objecte")
    if geometria.get("type") != "Point":
        raise ValueError(
            f"geometry.type no és Point: {geometria.get('type')}"
        )
    coordenades = geometria.get("coordinates")
    if not isinstance(coordenades, (list, tuple)) or len(coordenades) < 2:
        raise ValueError("coordinates no conté longitud i latitud")
    try:
        longitud = float(coordenades[0])
        latitud = float(coordenades[1])
    except (TypeError, ValueError) as error:
        raise ValueError("les coordenades no són numèriques") from error
    if not math.isfinite(longitud) or not math.isfinite(latitud):
        raise ValueError("les coordenades no són finites")
    if not (-180 <= longitud <= 180 and -90 <= latitud <= 90):
        raise ValueError("les coordenades no són WGS84 vàlides")
    return longitud, latitud


def clau_exacta(registre):
    return json.dumps(
        registre,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def valor_propietat(registre, nom):
    propietats = registre.get("properties")
    if not isinstance(propietats, dict):
        return ""
    valor = propietats.get(nom, "")
    return "" if valor is None else str(valor)


def empremta_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_fitxer(ruta):
    resum = hashlib.sha256()
    with ruta.open("rb") as fitxer:
        for bloc in iter(lambda: fitxer.read(1024 * 1024), b""):
            resum.update(bloc)
    return resum.hexdigest()


def escriure_json_atomic(ruta, dades, *, indent=None):
    ruta.parent.mkdir(parents=True, exist_ok=True)
    temporal = ruta.with_suffix(ruta.suffix + ".part")
    temporal.write_text(
        json.dumps(
            dades,
            ensure_ascii=False,
            indent=indent,
            separators=None if indent else (",", ":"),
        ),
        encoding="utf-8",
        newline="",
    )
    os.replace(temporal, ruta)


def escriure_csv_atomic(ruta, camps, files):
    ruta.parent.mkdir(parents=True, exist_ok=True)
    temporal = ruta.with_suffix(ruta.suffix + ".part")
    with temporal.open("w", encoding="utf-8-sig", newline="") as fitxer:
        escriptor = csv.DictWriter(
            fitxer,
            fieldnames=camps,
            delimiter=";",
            extrasaction="ignore",
        )
        escriptor.writeheader()
        escriptor.writerows(files)
    os.replace(temporal, ruta)


def fila_control(registre, bbox_id, index, longitud, latitud):
    return {
        "bbox_origen": bbox_id,
        "index_dins_bbox": index,
        "feature_id": registre.get("id", ""),
        "Gis_ID": valor_propietat(registre, "Gis_ID"),
        "Gis_Codigo": valor_propietat(registre, "Gis_Codigo"),
        "longitud": longitud,
        "latitud": latitud,
    }


def main():
    arguments = llegir_arguments()
    ruta_bbox = Path(arguments.fitxer_bbox)
    carpeta_json = Path(arguments.carpeta_json)
    ruta_limit = Path(arguments.limit_catalunya)
    for ruta, descripcio in (
        (ruta_bbox, "CSV de bbox"),
        (carpeta_json, "carpeta de JSON"),
        (ruta_limit, "límit de Catalunya"),
    ):
        if not ruta.exists():
            raise FileNotFoundError(f"No existeix {descripcio}: {ruta}")
    if not carpeta_json.is_dir():
        raise NotADirectoryError(
            f"La ruta dels JSON no és una carpeta: {carpeta_json}"
        )

    arrel_projecte = Path(__file__).resolve().parent.parent
    carpeta_sortida = (
        Path(arguments.carpeta_sortida)
        if arguments.carpeta_sortida
        else arrel_projecte / "data_output" / "extraccio_final"
    )
    ruta_gis_ids = (
        Path(arguments.fitxer_gis_ids)
        if arguments.fitxer_gis_ids
        else arrel_projecte / "config" / "gis_ids_infoantenas.csv"
    )

    ruta_inventari_geojson = (
        carpeta_sortida / "inventari_infoantenas.geojson"
    )
    ruta_inventari_csv = carpeta_sortida / "inventari_infoantenas.csv"
    ruta_duplicats = (
        carpeta_sortida / "duplicats_solapament_infoantenas.csv"
    )
    ruta_fora = (
        carpeta_sortida / "registres_fora_catalunya_infoantenas.csv"
    )
    ruta_conflictes = (
        carpeta_sortida / "conflictes_feature_id_infoantenas.csv"
    )
    ruta_resum = (
        carpeta_sortida / "resum_inventari_infoantenas.json"
    )

    print("Validant que hi siguin totes les cel·les esperades...")
    consultes = llegir_bbox(ruta_bbox)
    esperats = {
        consulta["nom_fitxer"]: consulta
        for consulta in consultes
    }
    faltants = [
        nom for nom in esperats
        if not (carpeta_json / nom).is_file()
    ]
    if faltants:
        mostra = ", ".join(faltants[:10])
        resta = "" if len(faltants) <= 10 else " ..."
        raise ValueError(
            f"Falten {len(faltants)} JSON esperats: {mostra}{resta}"
        )
    json_trobats = set(
        ruta.name for ruta in carpeta_json.glob("*.json")
    )
    extres = sorted(json_trobats - set(esperats))

    print("Carregant i validant el límit de Catalunya...")
    limit, limit_reparat, motiu_reparacio = carregar_limit(ruta_limit)
    if limit_reparat:
        print("El límit s'ha reparat geomètricament només en memòria.")
        print(f"Motiu: {motiu_reparacio}")
    limit_preparat = prep(limit)

    print("Validant, filtrant i unint els GeoJSON...")
    registres_final = []
    duplicats_solapament = []
    registres_fora = []
    conflictes_feature_id = []
    errors_registre = []
    primera_font_clau = {}
    primer_contingut_feature_id = {}
    gis_ids = Counter()
    claus_propietats = []
    claus_propietats_vistes = set()
    coordenades_uniques = set()
    manifest = hashlib.sha256()

    total_brut = 0
    json_buits = 0
    repetits_mateixa_bbox_conservats = 0
    gis_id_buits = 0

    for posicio, consulta in enumerate(consultes, start=1):
        ruta_json = carpeta_json / consulta["nom_fitxer"]
        try:
            registres, contingut, _ = carregar_resposta_infoantenas(
                ruta_json
            )
        except (OSError, ValueError) as error:
            raise ValueError(
                f"JSON invàlid {ruta_json.name}: {error}"
            ) from error

        resum_fitxer = hashlib.sha256(contingut).hexdigest()
        manifest.update(ruta_json.name.encode("utf-8"))
        manifest.update(b"\0")
        manifest.update(resum_fitxer.encode("ascii"))
        manifest.update(b"\0")
        if not registres:
            json_buits += 1
        total_brut += len(registres)

        if posicio == 1 or posicio % 250 == 0 or posicio == len(consultes):
            print(
                f"[{posicio}/{len(consultes)}] "
                f"{ruta_json.name}: {len(registres)} registres."
            )

        bbox_id = consulta["bbox_id"]
        for index, registre in enumerate(registres):
            try:
                longitud, latitud = coordenades_punt(registre)
            except ValueError as error:
                errors_registre.append(
                    f"{ruta_json.name}, feature {index}: {error}"
                )
                continue

            if not limit_preparat.covers(Point(longitud, latitud)):
                registres_fora.append(
                    fila_control(
                        registre,
                        bbox_id,
                        index,
                        longitud,
                        latitud,
                    )
                )
                continue

            propietats = registre.get("properties")
            if not isinstance(propietats, dict):
                errors_registre.append(
                    f"{ruta_json.name}, feature {index}: "
                    "properties no és un objecte"
                )
                continue

            gis_id_original = propietats.get("Gis_ID")
            if gis_id_original is not None and not isinstance(
                gis_id_original,
                str,
            ):
                errors_registre.append(
                    f"{ruta_json.name}, feature {index}: "
                    "Gis_ID no és text"
                )
                continue

            clau = clau_exacta(registre)
            font_anterior = primera_font_clau.get(clau)
            if font_anterior is not None and font_anterior != bbox_id:
                fila = fila_control(
                    registre,
                    bbox_id,
                    index,
                    longitud,
                    latitud,
                )
                fila["bbox_origen_conservada"] = font_anterior
                duplicats_solapament.append(fila)
                continue
            if font_anterior == bbox_id:
                repetits_mateixa_bbox_conservats += 1
            else:
                primera_font_clau[clau] = bbox_id

            feature_id = registre.get("id", "")
            feature_id_text = "" if feature_id is None else str(feature_id)
            if feature_id_text:
                anterior = primer_contingut_feature_id.get(feature_id_text)
                if anterior is None:
                    primer_contingut_feature_id[feature_id_text] = (
                        clau,
                        bbox_id,
                    )
                elif anterior[0] != clau:
                    conflictes_feature_id.append(
                        {
                            "feature_id": feature_id_text,
                            "bbox_primera_variant": anterior[1],
                            "bbox_variant_addicional": bbox_id,
                            "Gis_ID": valor_propietat(registre, "Gis_ID"),
                            "Gis_Codigo": valor_propietat(
                                registre,
                                "Gis_Codigo",
                            ),
                            "sha256_primera_variant": empremta_text(
                                anterior[0]
                            ),
                            "sha256_variant_addicional": empremta_text(clau),
                        }
                    )

            registres_final.append(registre)
            coordenades_uniques.add((longitud, latitud))
            for nom_propietat in propietats:
                if nom_propietat not in claus_propietats_vistes:
                    claus_propietats_vistes.add(nom_propietat)
                    claus_propietats.append(nom_propietat)

            gis_id = (gis_id_original or "").strip()
            if gis_id:
                gis_ids[gis_id] += 1
            else:
                gis_id_buits += 1

    if errors_registre:
        mostra = "\n".join(errors_registre[:20])
        resta = (
            ""
            if len(errors_registre) <= 20
            else f"\n... i {len(errors_registre) - 20} errors més."
        )
        raise ValueError(
            f"S'han detectat {len(errors_registre)} registres invàlids:\n"
            f"{mostra}{resta}"
        )
    if not registres_final:
        raise ValueError("No queda cap registre després del filtre territorial.")
    if not gis_ids:
        raise ValueError("No s'ha trobat cap Gis_ID vàlid.")

    print("Escrivint els resultats validats...")
    inventari = {
        "type": "FeatureCollection",
        "name": "inventari_infoantenas_catalunya",
        "features": registres_final,
    }
    escriure_json_atomic(ruta_inventari_geojson, inventari)

    camps_inventari = [
        "feature_type",
        "geometry_type",
        "geometry_longitud",
        "geometry_latitud",
    ]
    camps_inventari.extend(
        f"properties_{nom}" for nom in claus_propietats
    )
    camps_inventari.append("feature_id")
    files_inventari = []
    for registre in registres_final:
        geometria = registre.get("geometry") or {}
        longitud, latitud = coordenades_punt(registre)
        propietats = registre.get("properties") or {}
        fila = {
            "feature_type": registre.get("type", ""),
            "geometry_type": geometria.get("type", ""),
            "geometry_longitud": longitud,
            "geometry_latitud": latitud,
            "feature_id": registre.get("id", ""),
        }
        for nom_propietat in claus_propietats:
            fila[f"properties_{nom_propietat}"] = propietats.get(
                nom_propietat,
                "",
            )
        files_inventari.append(fila)
    escriure_csv_atomic(
        ruta_inventari_csv,
        camps_inventari,
        files_inventari,
    )

    files_gis_ids = [
        {
            "Gis_ID": gis_id,
            "registres_inventari": gis_ids[gis_id],
        }
        for gis_id in sorted(gis_ids, key=lambda valor: (valor.casefold(), valor))
    ]
    escriure_csv_atomic(
        ruta_gis_ids,
        ["Gis_ID", "registres_inventari"],
        files_gis_ids,
    )

    camps_control = [
        "bbox_origen_conservada",
        "bbox_origen",
        "index_dins_bbox",
        "feature_id",
        "Gis_ID",
        "Gis_Codigo",
        "longitud",
        "latitud",
    ]
    escriure_csv_atomic(
        ruta_duplicats,
        camps_control,
        duplicats_solapament,
    )
    escriure_csv_atomic(
        ruta_fora,
        [camp for camp in camps_control if camp != "bbox_origen_conservada"],
        registres_fora,
    )
    escriure_csv_atomic(
        ruta_conflictes,
        [
            "feature_id",
            "bbox_primera_variant",
            "bbox_variant_addicional",
            "Gis_ID",
            "Gis_Codigo",
            "sha256_primera_variant",
            "sha256_variant_addicional",
        ],
        conflictes_feature_id,
    )

    resum = {
        "moment_proces": datetime.now().isoformat(timespec="seconds"),
        "font_bbox": str(ruta_bbox),
        "sha256_bbox": sha256_fitxer(ruta_bbox),
        "font_limit_catalunya": str(ruta_limit),
        "sha256_limit_catalunya": sha256_fitxer(ruta_limit),
        "limit_reparat_en_memoria": limit_reparat,
        "motiu_reparacio_limit": motiu_reparacio,
        "carpeta_json": str(carpeta_json),
        "bbox_esperades": len(consultes),
        "bbox_processades": len(consultes),
        "json_buits": json_buits,
        "json_extres_ignorats": len(extres),
        "noms_json_extres_ignorats": extres,
        "sha256_manifest_json_entrada": manifest.hexdigest(),
        "registres_bruts": total_brut,
        "registres_fora_catalunya": len(registres_fora),
        "repeticions_exactes_solapament_eliminades": len(
            duplicats_solapament
        ),
        "repeticions_exactes_mateixa_bbox_conservades": (
            repetits_mateixa_bbox_conservats
        ),
        "registres_inventari_final": len(registres_final),
        "gis_ids_unics": len(gis_ids),
        "registres_sense_gis_id": gis_id_buits,
        "gis_ids_amb_mes_dun_registre": sum(
            1 for quantitat in gis_ids.values() if quantitat > 1
        ),
        "coordenades_uniques": len(coordenades_uniques),
        "variants_amb_mateix_feature_id": len(conflictes_feature_id),
        "criteri_deduplicacio": (
            "Només registres JSON exactament iguals trobats en bbox diferents; "
            "no es normalitzen ni fusionen Gis_ID, codis, operadors o coordenades."
        ),
        "fitxers_resultat": {
            "inventari_geojson": str(ruta_inventari_geojson),
            "sha256_inventari_geojson": sha256_fitxer(
                ruta_inventari_geojson
            ),
            "inventari_csv": str(ruta_inventari_csv),
            "sha256_inventari_csv": sha256_fitxer(ruta_inventari_csv),
            "gis_ids_csv": str(ruta_gis_ids),
            "sha256_gis_ids_csv": sha256_fitxer(ruta_gis_ids),
            "duplicats_solapament_csv": str(ruta_duplicats),
            "fora_catalunya_csv": str(ruta_fora),
            "conflictes_feature_id_csv": str(ruta_conflictes),
        },
    }
    escriure_json_atomic(ruta_resum, resum, indent=2)

    print()
    print("Inventari d'Infoantenas preparat i validat correctament.")
    print(f"Bbox processades: {len(consultes)}")
    print(f"JSON buits: {json_buits}")
    print(f"JSON extres ignorats: {len(extres)}")
    print(f"Registres bruts: {total_brut}")
    print(f"Registres fora de Catalunya: {len(registres_fora)}")
    print(
        "Repeticions exactes de solapament eliminades: "
        f"{len(duplicats_solapament)}"
    )
    print(
        "Repeticions exactes dins la mateixa bbox conservades: "
        f"{repetits_mateixa_bbox_conservats}"
    )
    print(f"Registres de l'inventari final: {len(registres_final)}")
    print(f"Gis_ID únics preparats: {len(gis_ids)}")
    print(f"Registres sense Gis_ID: {gis_id_buits}")
    print(
        "Variants amb el mateix feature_id conservades: "
        f"{len(conflictes_feature_id)}"
    )
    print(f"Inventari GeoJSON: {ruta_inventari_geojson}")
    print(f"Inventari CSV: {ruta_inventari_csv}")
    print(f"Llista per descarregar detalls: {ruta_gis_ids}")
    print(f"Resum de validació: {ruta_resum}")


if __name__ == "__main__":
    main()
