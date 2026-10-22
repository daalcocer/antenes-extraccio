from __future__ import annotations

from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
import argparse
import csv
import hashlib
import html
import json
import os
import re
import socket
import ssl
import time
import unicodedata

try:
    import truststore
except ImportError:
    truststore = None


BASE_URL = (
    "https://geoportal.minetur.gob.es/"
    "VCTEL/detalleEstacion.do"
)
PETICIONS_PER_MINUT_PER_DEFECTE = 10
MAXIM_PETICIONS_PER_MINUT = 20
REINTENTS_PER_DEFECTE = 2


def llegir_arguments():
    parser = argparse.ArgumentParser(
        description=(
            "Descarrega de manera reanudable els HTML de detall "
            "dels Gis_ID d'Infoantenas."
        )
    )
    parser.add_argument(
        "fitxer_gis_ids",
        help="CSV que conté la columna Gis_ID.",
    )
    parser.add_argument(
        "--peticions-per-minut",
        type=int,
        default=PETICIONS_PER_MINUT_PER_DEFECTE,
        help="Peticions per minut, entre 1 i 20.",
    )
    parser.add_argument(
        "--maxim",
        type=int,
        help="Processa només els primers N Gis_ID.",
    )
    parser.add_argument(
        "--reintents",
        type=int,
        default=REINTENTS_PER_DEFECTE,
        help="Reintents addicionals per Gis_ID després d'un error.",
    )
    parser.add_argument(
        "--carpeta-sortida",
        help=(
            "Carpeta dels HTML. Per defecte: "
            "data_raw/extraccio_final/html_detalls."
        ),
    )
    parser.add_argument(
        "--carpeta-logs",
        help=(
            "Carpeta dels registres. Per defecte: "
            "data_raw/extraccio_final/logs."
        ),
    )
    return parser.parse_args()


def llegir_gis_ids(ruta):
    resultat = []
    detectats = set()
    with ruta.open("r", encoding="utf-8-sig", newline="") as fitxer:
        lector = csv.DictReader(fitxer, delimiter=";")
        if not lector.fieldnames:
            raise ValueError("El CSV de Gis_ID no té capçalera.")
        columna = next(
            (
                nom
                for nom in ("Gis_ID", "properties_Gis_ID")
                if nom in lector.fieldnames
            ),
            None,
        )
        if columna is None:
            raise ValueError(
                "El CSV no conté la columna Gis_ID ni properties_Gis_ID."
            )

        for numero_fila, fila in enumerate(lector, start=2):
            gis_id = (fila.get(columna) or "").strip()
            if not gis_id:
                continue
            if any(
                unicodedata.category(caracter).startswith("C")
                for caracter in gis_id
            ):
                raise ValueError(
                    "Gis_ID amb caràcters de control a la fila "
                    f"{numero_fila}: {gis_id!r}"
                )
            if gis_id not in detectats:
                detectats.add(gis_id)
                resultat.append(gis_id)
    if not resultat:
        raise ValueError("No s'ha trobat cap Gis_ID al CSV.")
    return resultat


def nom_fitxer_html(gis_id):
    resum = hashlib.sha256(gis_id.encode("utf-8")).hexdigest()[:12]
    # El Gis_ID és text opac de la font oficial i pot contenir, per exemple,
    # barres, punts o caràcters no ASCII. Es codifica únicament per obtenir un
    # component de fitxer segur; la consulta HTTP conserva el valor original.
    component_segur = quote(gis_id, safe="")[:160]
    return f"{component_segur}_{resum}.html"


def construir_url(gis_id):
    return f"{BASE_URL}?{urlencode({'emplazamiento': gis_id})}"


def normalitzar_text(text):
    sense_accents = "".join(
        caracter
        for caracter in unicodedata.normalize("NFD", text)
        if unicodedata.category(caracter) != "Mn"
    )
    return " ".join(sense_accents.lower().split())


def descodificar_html(contingut, charset_preferit=None):
    codificacions = []
    for codificacio in (
        charset_preferit,
        "utf-8-sig",
        "cp1252",
        "iso-8859-1",
    ):
        if codificacio and codificacio.lower() not in {
            valor.lower() for valor in codificacions
        }:
            codificacions.append(codificacio)
    ultim_error = None
    for codificacio in codificacions:
        try:
            return contingut.decode(codificacio), codificacio
        except (LookupError, UnicodeDecodeError) as error:
            ultim_error = error
    raise ValueError(f"No es pot descodificar l'HTML: {ultim_error}")


def analitzar_html(contingut, gis_id, charset_preferit=None):
    if not contingut or not contingut.strip():
        raise ValueError("La resposta HTML està buida.")
    text, codificacio = descodificar_html(contingut, charset_preferit)
    text_minuscules = text.lower()
    if "<html" not in text_minuscules or "<table" not in text_minuscules:
        raise ValueError("La resposta no sembla una pàgina HTML de detall.")

    text_visible = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text_normalitzat = normalitzar_text(text_visible)
    te_caracteristiques = "caracteristicas tecnicas" in text_normalitzat
    te_nivells = "niveles medidos en el entorno" in text_normalitzat
    te_gis_id = gis_id.casefold() in text_visible.casefold()
    if not te_gis_id and not (te_caracteristiques or te_nivells):
        raise ValueError(
            "La pàgina no conté el Gis_ID ni les seccions de detall esperades."
        )
    return {
        "codificacio": codificacio,
        "te_gis_id": te_gis_id,
        "te_caracteristiques": te_caracteristiques,
        "te_nivells": te_nivells,
    }


def validar_fitxer_existent(ruta, gis_id):
    if not ruta.exists():
        return False, None
    try:
        analisi = analitzar_html(ruta.read_bytes(), gis_id)
        return True, analisi
    except (OSError, ValueError):
        return False, None


def esperar_limit(moment_ultima_peticio, segons_entre_peticions):
    if moment_ultima_peticio is None:
        return
    transcorregut = time.monotonic() - moment_ultima_peticio
    espera = max(0, segons_entre_peticions - transcorregut)
    if espera > 0:
        print(f"Esperant {espera:.1f} segons...", flush=True)
        time.sleep(espera)


def segons_retry_after(capcaleres, per_defecte=60.0):
    valor = capcaleres.get("Retry-After") if capcaleres else None
    try:
        return max(0.0, float(valor))
    except (TypeError, ValueError):
        return per_defecte


def main():
    arguments = llegir_arguments()
    if not 1 <= arguments.peticions_per_minut <= MAXIM_PETICIONS_PER_MINUT:
        raise ValueError("Les peticions per minut han d'estar entre 1 i 20.")
    if arguments.maxim is not None and arguments.maxim <= 0:
        raise ValueError("--maxim ha de ser superior a zero.")
    if arguments.reintents < 0:
        raise ValueError("--reintents no pot ser negatiu.")

    ruta_gis_ids = Path(arguments.fitxer_gis_ids)
    if not ruta_gis_ids.is_file():
        raise FileNotFoundError(
            f"No existeix el CSV de Gis_ID: {ruta_gis_ids}"
        )
    gis_ids = llegir_gis_ids(ruta_gis_ids)
    if arguments.maxim is not None:
        gis_ids = gis_ids[: arguments.maxim]

    arrel_projecte = Path(__file__).resolve().parent.parent
    carpeta_sortida = (
        Path(arguments.carpeta_sortida)
        if arguments.carpeta_sortida
        else arrel_projecte
        / "data_raw"
        / "extraccio_final"
        / "html_detalls"
    )
    carpeta_logs = (
        Path(arguments.carpeta_logs)
        if arguments.carpeta_logs
        else arrel_projecte / "data_raw" / "extraccio_final" / "logs"
    )
    carpeta_sortida.mkdir(parents=True, exist_ok=True)
    carpeta_logs.mkdir(parents=True, exist_ok=True)

    # A la represa pot haver-hi milers d'HTML. Llegir i analitzar-los tots en
    # aquesta fase feia que el programa semblés bloquejat i després els tornava
    # a validar dins del bucle principal. Aquí només comprovem l'existència;
    # cada fitxer existent es continua validant abans de donar-lo per bo.
    print("Comprovant els HTML que ja existeixen...", flush=True)
    existents_detectats = 0
    for posicio, gis_id in enumerate(gis_ids, start=1):
        ruta_html = carpeta_sortida / nom_fitxer_html(gis_id)
        if ruta_html.is_file():
            existents_detectats += 1
        if posicio % 1000 == 0 or posicio == len(gis_ids):
            print(
                f"  Fitxers comprovats: {posicio}/{len(gis_ids)}",
                flush=True,
            )

    pendents = len(gis_ids) - existents_detectats
    interval = 60 / arguments.peticions_per_minut
    durada_minima = max(0, pendents - 1) * interval / 3600

    print(f"Gis_ID seleccionats: {len(gis_ids)}")
    print(f"HTML existents detectats: {existents_detectats}")
    print(f"HTML pendents estimats: {pendents}")
    print(f"Límit: {arguments.peticions_per_minut} peticions per minut.")
    print(f"Durada mínima estimada: {durada_minima:.2f} hores.")
    print(f"Carpeta de sortida: {carpeta_sortida}")
    print()

    marca_execucio = datetime.now().strftime("%Y%m%d_%H%M%S")
    ruta_log = carpeta_logs / f"detalls_{marca_execucio}.csv"
    camps_log = [
        "moment",
        "Gis_ID",
        "estat",
        "intents_http",
        "codi_http",
        "content_type",
        "codificacio_html",
        "te_gis_id",
        "te_caracteristiques",
        "te_nivells",
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
    descarregats = 0
    existents = 0
    errors = 0
    intents_http_totals = 0
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
            for posicio, gis_id in enumerate(gis_ids, start=1):
                ruta_sortida = carpeta_sortida / nom_fitxer_html(gis_id)
                ruta_part = ruta_sortida.with_suffix(".html.part")
                valida, analisi_existent = validar_fitxer_existent(
                    ruta_sortida,
                    gis_id,
                )
                print(
                    f"[{posicio}/{len(gis_ids)}] Processant {gis_id}...",
                    flush=True,
                )

                if valida:
                    existents += 1
                    contingut = ruta_sortida.read_bytes()
                    fila_log = {
                        "moment": datetime.now().isoformat(timespec="seconds"),
                        "Gis_ID": gis_id,
                        "estat": "ja_existent",
                        "intents_http": 0,
                        "codi_http": "",
                        "content_type": "",
                        "codificacio_html": analisi_existent["codificacio"],
                        "te_gis_id": analisi_existent["te_gis_id"],
                        "te_caracteristiques": analisi_existent[
                            "te_caracteristiques"
                        ],
                        "te_nivells": analisi_existent["te_nivells"],
                        "bytes": len(contingut),
                        "sha256": hashlib.sha256(contingut).hexdigest(),
                        "fitxer": str(ruta_sortida),
                        "error": "",
                    }
                    escriptor_log.writerow(fila_log)
                    fitxer_log.flush()
                    print("Ja existent i vàlid.")
                    print()
                    continue

                if ruta_sortida.exists():
                    marca_invalida = datetime.now().strftime("%Y%m%d_%H%M%S")
                    ruta_invalida = ruta_sortida.with_suffix(
                        f".invalid_{marca_invalida}.html"
                    )
                    ruta_sortida.replace(ruta_invalida)
                    print(f"HTML anterior invàlid conservat a: {ruta_invalida}")

                exit_correcte = False
                ultim_error = ""
                ultim_codi = ""
                ultim_content_type = ""
                intents_gis_id = 0

                for intent in range(1, arguments.reintents + 2):
                    esperar_limit(moment_ultima_peticio, interval)
                    intents_gis_id += 1
                    intents_http_totals += 1
                    peticio = Request(
                        construir_url(gis_id),
                        headers={
                            "User-Agent": "Mozilla/5.0 TFG-Infoantenas/1.0",
                            "Accept": "text/html,application/xhtml+xml,*/*",
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
                                "Content-Type",
                                "",
                            )
                            charset = resposta.headers.get_content_charset()
                            contingut = resposta.read()

                        analisi = analitzar_html(contingut, gis_id, charset)
                        ruta_part.write_bytes(contingut)
                        os.replace(ruta_part, ruta_sortida)
                        descarregats += 1
                        exit_correcte = True
                        escriptor_log.writerow(
                            {
                                "moment": datetime.now().isoformat(
                                    timespec="seconds"
                                ),
                                "Gis_ID": gis_id,
                                "estat": "html_valid",
                                "intents_http": intents_gis_id,
                                "codi_http": ultim_codi,
                                "content_type": ultim_content_type,
                                "codificacio_html": analisi["codificacio"],
                                "te_gis_id": analisi["te_gis_id"],
                                "te_caracteristiques": analisi[
                                    "te_caracteristiques"
                                ],
                                "te_nivells": analisi["te_nivells"],
                                "bytes": len(contingut),
                                "sha256": hashlib.sha256(
                                    contingut
                                ).hexdigest(),
                                "fitxer": str(ruta_sortida),
                                "error": "",
                            }
                        )
                        fitxer_log.flush()
                        print(f"Codi HTTP: {ultim_codi}")
                        print(f"Bytes: {len(contingut)}")
                        print(
                            "Característiques tècniques detectades: "
                            f"{analisi['te_caracteristiques']}"
                        )
                        print(
                            "Nivells mesurats detectats: "
                            f"{analisi['te_nivells']}"
                        )
                        print(f"Fitxer: {ruta_sortida}")
                        print()
                        break

                    except HTTPError as error:
                        ultim_codi = error.code
                        ultim_content_type = (
                            error.headers.get("Content-Type", "")
                            if error.headers
                            else ""
                        )
                        ultim_error = f"HTTP {error.code}"
                        if error.code == 429:
                            espera = max(
                                interval,
                                segons_retry_after(error.headers),
                            )
                            print(
                                f"HTTP 429: esperant {espera:.1f} segons...",
                                flush=True,
                            )
                            time.sleep(espera)
                        elif 500 <= error.code < 600:
                            espera = min(60.0, 10.0 * intent)
                            print(
                                f"Error temporal: esperant {espera:.1f} "
                                "segons...",
                                flush=True,
                            )
                            time.sleep(espera)
                    except (
                        URLError,
                        TimeoutError,
                        socket.timeout,
                        OSError,
                        ValueError,
                    ) as error:
                        ultim_error = str(error)

                    print(f"Intent {intent} fallit: {ultim_error}", flush=True)
                    if intent <= arguments.reintents:
                        print("Es tornarà a intentar.", flush=True)

                if not exit_correcte:
                    errors += 1
                    escriptor_log.writerow(
                        {
                            "moment": datetime.now().isoformat(
                                timespec="seconds"
                            ),
                            "Gis_ID": gis_id,
                            "estat": "error",
                            "intents_http": intents_gis_id,
                            "codi_http": ultim_codi,
                            "content_type": ultim_content_type,
                            "codificacio_html": "",
                            "te_gis_id": "",
                            "te_caracteristiques": "",
                            "te_nivells": "",
                            "bytes": "",
                            "sha256": "",
                            "fitxer": str(ruta_sortida),
                            "error": ultim_error,
                        }
                    )
                    fitxer_log.flush()
                    print(f"Error definitiu a {gis_id}: {ultim_error}")
                    print()

        except KeyboardInterrupt:
            interromput = True
            print()
            print("Procés interromput per l'usuari.")
            print(
                "Els HTML completats s'han conservat i la pròxima "
                "execució els saltarà."
            )

    print()
    print("Resum del procés:")
    print(f"HTML nous descarregats: {descarregats}")
    print(f"HTML ja existents: {existents}")
    print(f"Gis_ID amb error: {errors}")
    print(f"Intents HTTP totals: {intents_http_totals}")
    print(f"Registre d'execució: {ruta_log}")

    if interromput:
        return 130
    if errors:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
