from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import argparse
import csv
import hashlib
import json
import os
import socket
import ssl
import time

try:
    import truststore
except ImportError:
    truststore = None


BASE_URL = (
    "https://geoportal.minetur.gob.es/"
    "VCTEL/infoantenasGeoJSON.do"
)
PETICIONS_PER_MINUT_PER_DEFECTE = 10
MAXIM_PETICIONS_PER_MINUT = 18
LIMIT_PRUDENT_PETICIONS_PER_MINUT = 10
REINTENTS_PER_DEFECTE = 2


def llegir_arguments():
    parser = argparse.ArgumentParser(
        description=(
            "Descarrega les respostes GeoJSON d'Infoantenas "
            "a partir d'un CSV de bbox. El procés és reanudable."
        )
    )
    parser.add_argument(
        "fitxer_bbox",
        help="CSV amb les bbox que s'han de consultar.",
    )
    parser.add_argument(
        "--peticions-per-minut",
        type=int,
        default=PETICIONS_PER_MINUT_PER_DEFECTE,
        help="Peticions per minut, entre 1 i 18.",
    )
    parser.add_argument(
        "--maxim",
        type=int,
        help="Processa només les primeres N bbox del CSV.",
    )
    parser.add_argument(
        "--reintents",
        type=int,
        default=REINTENTS_PER_DEFECTE,
        help="Reintents addicionals per bbox després d'un error.",
    )
    parser.add_argument(
        "--carpeta-sortida",
        help=(
            "Carpeta dels JSON. Per defecte: "
            "data_raw/extraccio_final/geojson_caselles."
        ),
    )
    parser.add_argument(
        "--carpeta-logs",
        help=(
            "Carpeta dels registres d'execució. Per defecte: "
            "data_raw/extraccio_final/logs."
        ),
    )
    return parser.parse_args()


def text_decimal(valor, camp, numero_fila):
    try:
        numero = Decimal(valor.strip())
    except InvalidOperation as error:
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


def llegir_bbox(fitxer_bbox):
    consultes = []
    ids_detectats = set()

    with fitxer_bbox.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as fitxer:
        lector = csv.DictReader(fitxer, delimiter=";")
        camps_obligatoris = {
            "bbox_id",
            "min_longitud",
            "min_latitud",
            "max_longitud",
            "max_latitud",
        }

        if not lector.fieldnames:
            raise ValueError("El CSV de bbox no té capçalera.")

        camps_faltants = camps_obligatoris - set(lector.fieldnames)
        if camps_faltants:
            raise ValueError(
                "Falten camps obligatoris: "
                + ", ".join(sorted(camps_faltants))
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
                    "bbox": valor_bbox,
                    "hash_bbox": hash_bbox,
                }
            )

    return consultes


def construir_url(valor_bbox):
    parametres = {
        "idCapa": "null",
        "bbox": valor_bbox,
        "zoom": "7",
    }
    return f"{BASE_URL}?{urlencode(parametres)}"


def analitzar_json(contingut, charset_preferit=None):
    if not contingut or not contingut.strip():
        raise ValueError("La resposta està buida i no conté JSON.")

    codificacions = []
    for codificacio in (
        charset_preferit,
        "utf-8-sig",
        "iso-8859-1",
    ):
        if codificacio and codificacio.lower() not in {
            valor.lower() for valor in codificacions
        }:
            codificacions.append(codificacio)

    ultim_error = None
    for codificacio in codificacions:
        try:
            dades = json.loads(contingut.decode(codificacio))
            break
        except (LookupError, UnicodeDecodeError, json.JSONDecodeError) as error:
            ultim_error = error
    else:
        raise ValueError(f"No es pot interpretar el JSON: {ultim_error}")

    if dades == {} or dades == []:
        return 0, "json_buit", codificacio

    if not isinstance(dades, dict):
        raise ValueError(
            "La resposta JSON no és una FeatureCollection."
        )
    if dades.get("type") != "FeatureCollection":
        raise ValueError(
            "La resposta JSON no té type=FeatureCollection."
        )

    registres = dades.get("features")
    if not isinstance(registres, list):
        raise ValueError(
            "La FeatureCollection no conté una llista features."
        )

    for index, registre in enumerate(registres):
        if not isinstance(registre, dict):
            raise ValueError(
                f"El feature {index} no és un objecte JSON."
            )

    return len(registres), "feature_collection", codificacio


def validar_fitxer_existent(ruta):
    if not ruta.exists():
        return False, 0
    try:
        registres, _, _ = analitzar_json(ruta.read_bytes())
        return True, registres
    except (OSError, ValueError):
        return False, 0


def esperar_limit(moment_ultima_peticio, segons_entre_peticions):
    if moment_ultima_peticio is None:
        return
    transcorregut = time.monotonic() - moment_ultima_peticio
    espera = max(0, segons_entre_peticions - transcorregut)
    if espera > 0:
        print(f"Esperant {espera:.1f} segons...", flush=True)
        time.sleep(espera)


def main():
    arguments = llegir_arguments()

    if not 1 <= arguments.peticions_per_minut <= MAXIM_PETICIONS_PER_MINUT:
        raise ValueError("Les peticions per minut han d'estar entre 1 i 18.")
    if arguments.maxim is not None and arguments.maxim <= 0:
        raise ValueError("--maxim ha de ser superior a zero.")
    if arguments.reintents < 0:
        raise ValueError("--reintents no pot ser negatiu.")

    fitxer_bbox = Path(arguments.fitxer_bbox)
    if not fitxer_bbox.exists():
        raise FileNotFoundError(f"No existeix el fitxer: {fitxer_bbox}")

    consultes = llegir_bbox(fitxer_bbox)
    if arguments.maxim is not None:
        consultes = consultes[: arguments.maxim]
    if not consultes:
        raise ValueError("No s'ha trobat cap bbox per processar.")

    arrel_projecte = Path(__file__).resolve().parent.parent
    carpeta_sortida = Path(arguments.carpeta_sortida) if arguments.carpeta_sortida else (
        arrel_projecte / "data_raw" / "extraccio_final" / "geojson_caselles"
    )
    carpeta_logs = Path(arguments.carpeta_logs) if arguments.carpeta_logs else (
        arrel_projecte / "data_raw" / "extraccio_final" / "logs"
    )
    carpeta_sortida.mkdir(parents=True, exist_ok=True)
    carpeta_logs.mkdir(parents=True, exist_ok=True)

    pendents = 0
    for consulta in consultes:
        nom = f"{consulta['bbox_id']}_{consulta['hash_bbox']}.json"
        valida, _ = validar_fitxer_existent(carpeta_sortida / nom)
        if not valida:
            pendents += 1

    interval = 60 / arguments.peticions_per_minut
    durada_minima = max(0, pendents - 1) * interval / 3600
    print(f"Bbox seleccionades: {len(consultes)}")
    print(f"Bbox pendents de descarregar: {pendents}")
    print(
        f"Límit: {arguments.peticions_per_minut} peticions per minut."
    )
    if (
        arguments.peticions_per_minut
        > LIMIT_PRUDENT_PETICIONS_PER_MINUT
    ):
        print(
            "Avís: s'està utilitzant un ritme superior al límit "
            "prudent de 10 peticions/minut."
        )
        print(
            "Si el servidor respon HTTP 429, el programa respectarà "
            "Retry-After o esperarà 60 segons abans del reintent."
        )
    print(f"Durada mínima estimada: {durada_minima:.2f} hores.")
    print(f"Carpeta de sortida: {carpeta_sortida}")
    print()

    marca_execucio = datetime.now().strftime("%Y%m%d_%H%M%S")
    ruta_log = carpeta_logs / f"bbox_{marca_execucio}.csv"
    camps_log = [
        "moment",
        "bbox_id",
        "bbox",
        "hash_bbox",
        "estat",
        "intents_http",
        "codi_http",
        "content_type",
        "codificacio_json",
        "registres",
        "bytes",
        "sha256",
        "fitxer",
        "error",
    ]

    if truststore is not None:
        context_ssl = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    else:
        context_ssl = ssl.create_default_context()
    moment_ultima_peticio = None
    descarregades = 0
    existents = 0
    errors = 0
    intents_http_totals = 0
    registres_totals = 0
    interromput = False

    with ruta_log.open("w", encoding="utf-8-sig", newline="") as fitxer_log:
        escriptor_log = csv.DictWriter(
            fitxer_log,
            fieldnames=camps_log,
            delimiter=";",
        )
        escriptor_log.writeheader()
        fitxer_log.flush()

        try:
            for posicio, consulta in enumerate(consultes, start=1):
                bbox_id = consulta["bbox_id"]
                nom_fitxer = f"{bbox_id}_{consulta['hash_bbox']}.json"
                ruta_sortida = carpeta_sortida / nom_fitxer
                ruta_part = ruta_sortida.with_suffix(".json.part")
                valida, registres_existents = validar_fitxer_existent(
                    ruta_sortida
                )

                print(
                    f"[{posicio}/{len(consultes)}] Processant {bbox_id}...",
                    flush=True,
                )

                if valida:
                    existents += 1
                    registres_totals += registres_existents
                    fila_log = {
                        "moment": datetime.now().isoformat(timespec="seconds"),
                        "bbox_id": bbox_id,
                        "bbox": consulta["bbox"],
                        "hash_bbox": consulta["hash_bbox"],
                        "estat": "ja_existent",
                        "intents_http": 0,
                        "codi_http": "",
                        "content_type": "",
                        "codificacio_json": "",
                        "registres": registres_existents,
                        "bytes": ruta_sortida.stat().st_size,
                        "sha256": hashlib.sha256(
                            ruta_sortida.read_bytes()
                        ).hexdigest(),
                        "fitxer": str(ruta_sortida),
                        "error": "",
                    }
                    escriptor_log.writerow(fila_log)
                    fitxer_log.flush()
                    print(
                        f"Ja existent i vàlid: {registres_existents} registres."
                    )
                    print()
                    continue

                if ruta_sortida.exists():
                    marca_invalida = datetime.now().strftime("%Y%m%d_%H%M%S")
                    ruta_invalida = ruta_sortida.with_suffix(
                        f".invalid_{marca_invalida}.json"
                    )
                    ruta_sortida.replace(ruta_invalida)
                    print(
                        f"Fitxer anterior invàlid conservat a: {ruta_invalida}"
                    )

                ultim_error = ""
                ultim_codi = ""
                ultim_content_type = ""
                intents_bbox = 0
                exit_correcte = False

                for intent in range(1, arguments.reintents + 2):
                    esperar_limit(moment_ultima_peticio, interval)
                    intents_bbox += 1
                    intents_http_totals += 1
                    url = construir_url(consulta["bbox"])
                    peticio = Request(
                        url,
                        headers={
                            "User-Agent": "Mozilla/5.0 TFG-Infoantenas/1.0",
                            "Accept": "application/json,text/plain,*/*",
                            "Accept-Encoding": "identity",
                        },
                    )
                    moment_ultima_peticio = time.monotonic()

                    try:
                        with urlopen(
                            peticio,
                            timeout=45,
                            context=context_ssl,
                        ) as resposta:
                            ultim_codi = resposta.status
                            ultim_content_type = resposta.headers.get(
                                "Content-Type", ""
                            )
                            charset = resposta.headers.get_content_charset()
                            contingut = resposta.read()

                        ruta_part.write_bytes(contingut)
                        registres, tipus_json, codificacio = analitzar_json(
                            contingut,
                            charset,
                        )
                        os.replace(ruta_part, ruta_sortida)
                        sha256 = hashlib.sha256(contingut).hexdigest()
                        descarregades += 1
                        registres_totals += registres
                        exit_correcte = True

                        escriptor_log.writerow(
                            {
                                "moment": datetime.now().isoformat(
                                    timespec="seconds"
                                ),
                                "bbox_id": bbox_id,
                                "bbox": consulta["bbox"],
                                "hash_bbox": consulta["hash_bbox"],
                                "estat": tipus_json,
                                "intents_http": intents_bbox,
                                "codi_http": ultim_codi,
                                "content_type": ultim_content_type,
                                "codificacio_json": codificacio,
                                "registres": registres,
                                "bytes": len(contingut),
                                "sha256": sha256,
                                "fitxer": str(ruta_sortida),
                                "error": "",
                            }
                        )
                        fitxer_log.flush()
                        print(f"Codi HTTP: {ultim_codi}")
                        print(f"Tipus JSON: {tipus_json}")
                        print(f"Registres retornats: {registres}")
                        print(f"Bytes: {len(contingut)}")
                        print(f"Fitxer: {ruta_sortida}")
                        print()
                        break

                    except HTTPError as error:
                        ultim_codi = error.code
                        ultim_content_type = error.headers.get(
                            "Content-Type", ""
                        ) if error.headers else ""
                        ultim_error = f"HTTP {error.code}"
                        if error.code == 429:
                            retry_after = (
                                error.headers.get("Retry-After")
                                if error.headers
                                else None
                            )
                            try:
                                espera_servidor = float(retry_after)
                            except (TypeError, ValueError):
                                espera_servidor = 60.0
                            espera_servidor = max(
                                interval,
                                espera_servidor,
                            )
                            print(
                                "El servidor ha limitat temporalment "
                                "les peticions (HTTP 429)."
                            )
                            print(
                                f"Esperant {espera_servidor:.1f} segons "
                                "abans de continuar...",
                                flush=True,
                            )
                            time.sleep(espera_servidor)
                    except (
                        URLError,
                        TimeoutError,
                        socket.timeout,
                        OSError,
                        ValueError,
                    ) as error:
                        ultim_error = str(error)

                    print(
                        f"Intent {intent} fallit: {ultim_error}",
                        flush=True,
                    )
                    if intent <= arguments.reintents:
                        print("Es tornarà a intentar.", flush=True)

                if not exit_correcte:
                    errors += 1
                    escriptor_log.writerow(
                        {
                            "moment": datetime.now().isoformat(
                                timespec="seconds"
                            ),
                            "bbox_id": bbox_id,
                            "bbox": consulta["bbox"],
                            "hash_bbox": consulta["hash_bbox"],
                            "estat": "error",
                            "intents_http": intents_bbox,
                            "codi_http": ultim_codi,
                            "content_type": ultim_content_type,
                            "codificacio_json": "",
                            "registres": "",
                            "bytes": (
                                ruta_part.stat().st_size
                                if ruta_part.exists()
                                else ""
                            ),
                            "sha256": "",
                            "fitxer": str(ruta_part),
                            "error": ultim_error,
                        }
                    )
                    fitxer_log.flush()
                    print(f"Error definitiu a {bbox_id}: {ultim_error}")
                    print()

        except KeyboardInterrupt:
            interromput = True
            print()
            print("Procés interromput per l'usuari.")
            print(
                "Els JSON completats s'han conservat i la pròxima "
                "execució els saltarà."
            )

    print()
    print("Resum del procés:")
    print(f"JSON nous descarregats: {descarregades}")
    print(f"JSON ja existents: {existents}")
    print(f"Bbox amb error: {errors}")
    print(f"Intents HTTP totals: {intents_http_totals}")
    print(f"Registres totals informats: {registres_totals}")
    print(f"Registre d'execució: {ruta_log}")

    if interromput:
        return 130
    if errors:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
