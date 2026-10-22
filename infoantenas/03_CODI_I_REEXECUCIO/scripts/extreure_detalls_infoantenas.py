from __future__ import annotations

from collections import Counter
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote
import argparse
import csv
import hashlib
import html
import json
import os
import re
import unicodedata


class AnalitzadorTaulesHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.pila_taules = []
        self.taules = []

    def handle_starttag(self, etiqueta, atributs):
        etiqueta = etiqueta.lower()
        if etiqueta == "table":
            self.pila_taules.append(
                {
                    "files": [],
                    "fila_actual": None,
                    "celda_actual": None,
                }
            )
        elif etiqueta == "tr" and self.pila_taules:
            self.pila_taules[-1]["fila_actual"] = []
        elif (
            etiqueta in ("td", "th")
            and self.pila_taules
            and self.pila_taules[-1]["fila_actual"] is not None
        ):
            self.pila_taules[-1]["celda_actual"] = []

    def handle_data(self, dades):
        if (
            self.pila_taules
            and self.pila_taules[-1]["celda_actual"] is not None
        ):
            self.pila_taules[-1]["celda_actual"].append(dades)

    def handle_endtag(self, etiqueta):
        etiqueta = etiqueta.lower()
        if (
            etiqueta in ("td", "th")
            and self.pila_taules
            and self.pila_taules[-1]["celda_actual"] is not None
        ):
            text = " ".join(
                " ".join(self.pila_taules[-1]["celda_actual"]).split()
            )
            self.pila_taules[-1]["fila_actual"].append(text)
            self.pila_taules[-1]["celda_actual"] = None
        elif (
            etiqueta == "tr"
            and self.pila_taules
            and self.pila_taules[-1]["fila_actual"] is not None
        ):
            fila = self.pila_taules[-1]["fila_actual"]
            if fila:
                self.pila_taules[-1]["files"].append(fila)
            self.pila_taules[-1]["fila_actual"] = None
        elif etiqueta == "table" and self.pila_taules:
            taula = self.pila_taules.pop()
            self.taules.append(taula["files"])


def llegir_arguments():
    parser = argparse.ArgumentParser(
        description=(
            "Valida tots els HTML de detall d'Infoantenas i genera "
            "els CSV de característiques tècniques i nivells mesurats."
        )
    )
    parser.add_argument(
        "fitxer_gis_ids",
        help="CSV que conté la columna Gis_ID.",
    )
    parser.add_argument(
        "carpeta_html",
        help="Carpeta amb els HTML descarregats.",
    )
    parser.add_argument(
        "--carpeta-sortida",
        help=(
            "Carpeta dels CSV finals. Per defecte: "
            "data_output/extraccio_final."
        ),
    )
    parser.add_argument(
        "--registre-descarrega",
        help=(
            "CSV del registre de la descàrrega dels HTML. És obligatori "
            "si es permet continuar amb algun HTML absent."
        ),
    )
    parser.add_argument(
        "--maxim-html-faltants",
        type=int,
        default=0,
        help=(
            "Nombre màxim d'HTML absents que es poden documentar i ometre. "
            "Per defecte és 0."
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
    # Ha de coincidir exactament amb el criteri del descarregador.
    component_segur = quote(gis_id, safe="")[:160]
    return f"{component_segur}_{resum}.html"


def llegir_registre_descarrega(ruta):
    with ruta.open("r", encoding="utf-8-sig", newline="") as fitxer:
        lector = csv.DictReader(fitxer, delimiter=";")
        camps_obligatoris = {
            "Gis_ID",
            "estat",
            "intents_http",
            "codi_http",
            "content_type",
            "error",
        }
        if not lector.fieldnames:
            raise ValueError("El registre de descàrrega no té capçalera.")
        camps_faltants = camps_obligatoris - set(lector.fieldnames)
        if camps_faltants:
            raise ValueError(
                "Falten camps al registre de descàrrega: "
                + ", ".join(sorted(camps_faltants))
            )

        resultat = {}
        for numero_fila, fila in enumerate(lector, start=2):
            gis_id = (fila.get("Gis_ID") or "").strip()
            if not gis_id:
                raise ValueError(
                    "Gis_ID buit al registre de descàrrega, fila "
                    f"{numero_fila}."
                )
            if gis_id in resultat:
                raise ValueError(
                    "Gis_ID repetit al registre de descàrrega: "
                    f"{gis_id}"
                )
            resultat[gis_id] = fila
    return resultat


def normalitzar_text(text):
    sense_accents = "".join(
        caracter
        for caracter in unicodedata.normalize("NFD", text)
        if unicodedata.category(caracter) != "Mn"
    )
    return " ".join(sense_accents.lower().split())


def descodificar_html(contingut):
    if not contingut or not contingut.strip():
        raise ValueError("L'HTML està buit.")
    ultim_error = None
    for codificacio in ("utf-8-sig", "cp1252", "iso-8859-1"):
        try:
            return contingut.decode(codificacio), codificacio
        except UnicodeDecodeError as error:
            ultim_error = error
    raise ValueError(f"No es pot descodificar l'HTML: {ultim_error}")


def analitzar_pagina(contingut, gis_id):
    text, codificacio = descodificar_html(contingut)
    text_minuscules = text.lower()
    if "<html" not in text_minuscules or "<table" not in text_minuscules:
        raise ValueError("El fitxer no sembla una pàgina HTML de detall.")
    text_visible = html.unescape(re.sub(r"<[^>]+>", " ", text))
    normalitzat = normalitzar_text(text_visible)
    te_caracteristiques = "caracteristicas tecnicas" in normalitzat
    te_nivells = "niveles medidos en el entorno" in normalitzat
    te_gis_id = gis_id.casefold() in text_visible.casefold()
    if not te_gis_id and not (te_caracteristiques or te_nivells):
        raise ValueError(
            "La pàgina no conté el Gis_ID ni les seccions esperades."
        )
    return text, {
        "codificacio": codificacio,
        "te_gis_id": te_gis_id,
        "te_caracteristiques": te_caracteristiques,
        "te_nivells": te_nivells,
    }


def trobar_columna(celles, text_buscat):
    for index, valor in enumerate(celles):
        if text_buscat in normalitzar_text(valor):
            return index
    return None


def tipus_capcalera(fila):
    operador = trobar_columna(fila, "operador")
    referencia = trobar_columna(fila, "referencia")
    banda = trobar_columna(fila, "banda asignada")
    if None not in (operador, referencia, banda):
        return "caracteristiques", {
            "operador": operador,
            "referencia": referencia,
            "banda": banda,
        }

    distancia = trobar_columna(fila, "distancia")
    acimut = trobar_columna(fila, "acimut")
    valor_mesurat = trobar_columna(fila, "valor medido")
    if None not in (distancia, acimut, valor_mesurat):
        return "mesures", {
            "distancia": distancia,
            "acimut": acimut,
            "valor_mesurat": valor_mesurat,
        }
    return None, None


def es_capcalera_repetida(fila, tipus):
    tipus_detectat, _ = tipus_capcalera(fila)
    return tipus_detectat == tipus


def extreure_taules(text, gis_id, nom_fitxer):
    analitzador = AnalitzadorTaulesHTML()
    analitzador.feed(text)
    analitzador.close()

    caracteristiques = []
    mesures = []
    incidencies = []
    taules_caracteristiques = 0
    taules_mesures = 0

    for index_taula, taula in enumerate(analitzador.taules):
        tipus = None
        columnes = None
        index_capcalera = None
        for index_fila, fila in enumerate(taula):
            tipus_detectat, columnes_detectades = tipus_capcalera(fila)
            if tipus_detectat:
                tipus = tipus_detectat
                columnes = columnes_detectades
                index_capcalera = index_fila
                break
        if tipus is None:
            continue

        if tipus == "caracteristiques":
            taules_caracteristiques += 1
            noms_columnes = ("operador", "referencia", "banda")
        else:
            taules_mesures += 1
            noms_columnes = ("distancia", "acimut", "valor_mesurat")

        index_maxim = max(columnes.values())
        for index_fila, fila in enumerate(
            taula[index_capcalera + 1 :],
            start=index_capcalera + 1,
        ):
            if not any(valor.strip() for valor in fila):
                continue
            if es_capcalera_repetida(fila, tipus):
                continue
            if len(fila) <= index_maxim:
                incidencies.append(
                    {
                        "Gis_ID": gis_id,
                        "fitxer": nom_fitxer,
                        "taula": index_taula,
                        "fila": index_fila,
                        "tipus": tipus,
                        "motiu": "fila amb menys columnes que la capçalera",
                        "valors": json.dumps(fila, ensure_ascii=False),
                    }
                )
                continue

            valors = {
                nom: fila[columnes[nom]].strip()
                for nom in noms_columnes
            }
            if not any(valors.values()):
                continue
            camps_buits = [nom for nom, valor in valors.items() if not valor]
            if camps_buits:
                incidencies.append(
                    {
                        "Gis_ID": gis_id,
                        "fitxer": nom_fitxer,
                        "taula": index_taula,
                        "fila": index_fila,
                        "tipus": tipus,
                        "motiu": "camps buits: " + ", ".join(camps_buits),
                        "valors": json.dumps(fila, ensure_ascii=False),
                    }
                )

            if tipus == "caracteristiques":
                caracteristiques.append(
                    {
                        "Gis_ID": gis_id,
                        "Operador": valors["operador"],
                        "Referencia": valors["referencia"],
                        "Banda Asignada (MHz)": valors["banda"],
                    }
                )
            else:
                mesures.append(
                    {
                        "Gis_ID": gis_id,
                        "Distancia (m)": valors["distancia"],
                        "(*) Acimut (º)": valors["acimut"],
                        "Valor Medido (µW/cm2)": valors["valor_mesurat"],
                    }
                )

    return {
        "caracteristiques": caracteristiques,
        "mesures": mesures,
        "incidencies": incidencies,
        "taules_caracteristiques": taules_caracteristiques,
        "taules_mesures": taules_mesures,
    }


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


def escriure_json_atomic(ruta, dades):
    ruta.parent.mkdir(parents=True, exist_ok=True)
    temporal = ruta.with_suffix(ruta.suffix + ".part")
    temporal.write_text(
        json.dumps(dades, ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="",
    )
    os.replace(temporal, ruta)


def sha256_fitxer(ruta):
    resum = hashlib.sha256()
    with ruta.open("rb") as fitxer:
        for bloc in iter(lambda: fitxer.read(1024 * 1024), b""):
            resum.update(bloc)
    return resum.hexdigest()


def repeticions_exactes(files, camps):
    comptador = Counter(tuple(fila[camp] for camp in camps) for fila in files)
    return sum(quantitat - 1 for quantitat in comptador.values() if quantitat > 1)


def main():
    arguments = llegir_arguments()
    if arguments.maxim_html_faltants < 0:
        raise ValueError("--maxim-html-faltants no pot ser negatiu.")

    ruta_gis_ids = Path(arguments.fitxer_gis_ids)
    carpeta_html = Path(arguments.carpeta_html)
    if not ruta_gis_ids.is_file():
        raise FileNotFoundError(
            f"No existeix el CSV de Gis_ID: {ruta_gis_ids}"
        )
    if not carpeta_html.is_dir():
        raise NotADirectoryError(
            f"No existeix la carpeta d'HTML: {carpeta_html}"
        )

    arrel_projecte = Path(__file__).resolve().parent.parent
    carpeta_sortida = (
        Path(arguments.carpeta_sortida)
        if arguments.carpeta_sortida
        else arrel_projecte / "data_output" / "extraccio_final"
    )
    carpeta_sortida.mkdir(parents=True, exist_ok=True)

    gis_ids = llegir_gis_ids(ruta_gis_ids)
    noms_esperats = {nom_fitxer_html(gis_id) for gis_id in gis_ids}
    gis_ids_faltants = [
        gis_id
        for gis_id in gis_ids
        if not (carpeta_html / nom_fitxer_html(gis_id)).is_file()
    ]
    if len(gis_ids_faltants) > arguments.maxim_html_faltants:
        mostra = ", ".join(gis_ids_faltants[:10])
        resta = "" if len(gis_ids_faltants) <= 10 else " ..."
        raise ValueError(
            f"Falten {len(gis_ids_faltants)} HTML esperats, però només "
            f"se'n permeten {arguments.maxim_html_faltants}: "
            f"{mostra}{resta}"
        )

    ruta_registre_descarrega = (
        Path(arguments.registre_descarrega)
        if arguments.registre_descarrega
        else None
    )
    registre_descarrega = {}
    if gis_ids_faltants:
        if ruta_registre_descarrega is None:
            raise ValueError(
                "Hi ha HTML faltants. Cal indicar --registre-descarrega "
                "per justificar-los."
            )
        if not ruta_registre_descarrega.is_file():
            raise FileNotFoundError(
                "No existeix el registre de descàrrega: "
                f"{ruta_registre_descarrega}"
            )
        registre_descarrega = llegir_registre_descarrega(
            ruta_registre_descarrega
        )
        sense_error_documentat = [
            gis_id
            for gis_id in gis_ids_faltants
            if gis_id not in registre_descarrega
            or registre_descarrega[gis_id].get("estat") != "error"
        ]
        if sense_error_documentat:
            raise ValueError(
                "Els HTML absents no consten com a error al registre: "
                + ", ".join(sense_error_documentat)
            )

    gis_ids_processables = [
        gis_id for gis_id in gis_ids if gis_id not in set(gis_ids_faltants)
    ]
    if not gis_ids_processables:
        raise ValueError("No hi ha cap HTML disponible per processar.")

    extres = sorted(
        ruta.name
        for ruta in carpeta_html.glob("*.html")
        if ruta.name not in noms_esperats
    )

    caracteristiques_totals = []
    mesures_totals = []
    incidencies_totals = []
    control = []
    ids_sense_caracteristiques = []
    ids_sense_mesures = []
    manifest = hashlib.sha256()

    print(f"Gis_ID esperats: {len(gis_ids)}")
    print(f"HTML disponibles per processar: {len(gis_ids_processables)}")
    print(f"HTML absents documentats: {len(gis_ids_faltants)}")
    print("Validant i extraient tots els HTML...")
    for posicio, gis_id in enumerate(gis_ids_processables, start=1):
        ruta = carpeta_html / nom_fitxer_html(gis_id)
        contingut = ruta.read_bytes()
        try:
            text, analisi = analitzar_pagina(contingut, gis_id)
            resultat = extreure_taules(text, gis_id, ruta.name)
        except (OSError, ValueError) as error:
            raise ValueError(f"HTML invàlid {ruta.name}: {error}") from error

        resum_html = hashlib.sha256(contingut).hexdigest()
        manifest.update(ruta.name.encode("utf-8"))
        manifest.update(b"\0")
        manifest.update(resum_html.encode("ascii"))
        manifest.update(b"\0")

        caracteristiques = resultat["caracteristiques"]
        mesures = resultat["mesures"]
        caracteristiques_totals.extend(caracteristiques)
        mesures_totals.extend(mesures)
        incidencies_totals.extend(resultat["incidencies"])
        if not caracteristiques:
            ids_sense_caracteristiques.append(gis_id)
        if not mesures:
            ids_sense_mesures.append(gis_id)
        control.append(
            {
                "Gis_ID": gis_id,
                "fitxer": ruta.name,
                "bytes": len(contingut),
                "sha256": resum_html,
                "codificacio": analisi["codificacio"],
                "te_gis_id_visible": analisi["te_gis_id"],
                "te_titol_caracteristiques": analisi[
                    "te_caracteristiques"
                ],
                "te_titol_nivells": analisi["te_nivells"],
                "taules_caracteristiques": resultat[
                    "taules_caracteristiques"
                ],
                "files_caracteristiques": len(caracteristiques),
                "taules_nivells": resultat["taules_mesures"],
                "files_nivells": len(mesures),
                "incidencies_parseig": len(resultat["incidencies"]),
            }
        )

        if (
            posicio == 1
            or posicio % 250 == 0
            or posicio == len(gis_ids_processables)
        ):
            print(
                f"[{posicio}/{len(gis_ids_processables)}] {gis_id}: "
                f"{len(caracteristiques)} característiques, "
                f"{len(mesures)} mesures."
            )

    ruta_caracteristiques = (
        carpeta_sortida / "CARACTERÍSTICAS TÉCNICAS.csv"
    )
    ruta_mesures = (
        carpeta_sortida / "NIVELES MEDIDOS EN EL ENTORNO.csv"
    )
    ruta_control = carpeta_sortida / "control_detalls_infoantenas.csv"
    ruta_incidencies = (
        carpeta_sortida / "incidencies_parseig_infoantenas.csv"
    )
    ruta_html_faltants = (
        carpeta_sortida / "gis_ids_sense_html_infoantenas.csv"
    )
    ruta_resum = carpeta_sortida / "resum_detalls_infoantenas.json"

    camps_caracteristiques = [
        "Gis_ID",
        "Operador",
        "Referencia",
        "Banda Asignada (MHz)",
    ]
    camps_mesures = [
        "Gis_ID",
        "Distancia (m)",
        "(*) Acimut (º)",
        "Valor Medido (µW/cm2)",
    ]
    escriure_csv_atomic(
        ruta_caracteristiques,
        camps_caracteristiques,
        caracteristiques_totals,
    )
    escriure_csv_atomic(ruta_mesures, camps_mesures, mesures_totals)
    escriure_csv_atomic(
        ruta_control,
        [
            "Gis_ID",
            "fitxer",
            "bytes",
            "sha256",
            "codificacio",
            "te_gis_id_visible",
            "te_titol_caracteristiques",
            "te_titol_nivells",
            "taules_caracteristiques",
            "files_caracteristiques",
            "taules_nivells",
            "files_nivells",
            "incidencies_parseig",
        ],
        control,
    )
    escriure_csv_atomic(
        ruta_incidencies,
        [
            "Gis_ID",
            "fitxer",
            "taula",
            "fila",
            "tipus",
            "motiu",
            "valors",
        ],
        incidencies_totals,
    )
    files_html_faltants = []
    for gis_id in gis_ids_faltants:
        fila_registre = registre_descarrega[gis_id]
        files_html_faltants.append(
            {
                "Gis_ID": gis_id,
                "fitxer_esperat": nom_fitxer_html(gis_id),
                "estat_descarrega": fila_registre.get("estat", ""),
                "intents_http": fila_registre.get("intents_http", ""),
                "codi_http": fila_registre.get("codi_http", ""),
                "content_type": fila_registre.get("content_type", ""),
                "error": fila_registre.get("error", ""),
                "registre_descarrega": str(ruta_registre_descarrega),
            }
        )
    escriure_csv_atomic(
        ruta_html_faltants,
        [
            "Gis_ID",
            "fitxer_esperat",
            "estat_descarrega",
            "intents_http",
            "codi_http",
            "content_type",
            "error",
            "registre_descarrega",
        ],
        files_html_faltants,
    )

    duplicats_caracteristiques = repeticions_exactes(
        caracteristiques_totals,
        camps_caracteristiques,
    )
    duplicats_mesures = repeticions_exactes(
        mesures_totals,
        camps_mesures,
    )
    resum = {
        "moment_proces": datetime.now().isoformat(timespec="seconds"),
        "fitxer_gis_ids": str(ruta_gis_ids),
        "sha256_gis_ids": sha256_fitxer(ruta_gis_ids),
        "carpeta_html": str(carpeta_html),
        "gis_ids_esperats": len(gis_ids),
        "html_processats": len(gis_ids_processables),
        "html_faltants_documentats": len(gis_ids_faltants),
        "gis_ids_sense_html": gis_ids_faltants,
        "cobertura_html_percent": round(
            100 * len(gis_ids_processables) / len(gis_ids),
            6,
        ),
        "registre_descarrega": (
            str(ruta_registre_descarrega)
            if ruta_registre_descarrega
            else None
        ),
        "sha256_registre_descarrega": (
            sha256_fitxer(ruta_registre_descarrega)
            if ruta_registre_descarrega
            else None
        ),
        "html_extres_ignorats": len(extres),
        "noms_html_extres_ignorats": extres,
        "sha256_manifest_html": manifest.hexdigest(),
        "files_caracteristiques": len(caracteristiques_totals),
        "files_nivells_mesurats": len(mesures_totals),
        "gis_ids_sense_caracteristiques": ids_sense_caracteristiques,
        "gis_ids_sense_nivells_mesurats": ids_sense_mesures,
        "incidencies_parseig": len(incidencies_totals),
        "repeticions_exactes_caracteristiques_conservades": (
            duplicats_caracteristiques
        ),
        "repeticions_exactes_nivells_conservades": duplicats_mesures,
        "criteri_conservacio": (
            "S'han conservat totes les files de totes les taules "
            "identificades per les seves capçaleres; no s'han eliminat "
            "files repetides."
        ),
        "fitxers_resultat": {
            "caracteristiques_csv": str(ruta_caracteristiques),
            "sha256_caracteristiques_csv": sha256_fitxer(
                ruta_caracteristiques
            ),
            "nivells_csv": str(ruta_mesures),
            "sha256_nivells_csv": sha256_fitxer(ruta_mesures),
            "control_csv": str(ruta_control),
            "incidencies_csv": str(ruta_incidencies),
            "gis_ids_sense_html_csv": str(ruta_html_faltants),
            "sha256_gis_ids_sense_html_csv": sha256_fitxer(
                ruta_html_faltants
            ),
        },
    }
    escriure_json_atomic(ruta_resum, resum)

    print()
    print("Extracció de detalls d'Infoantenas completada i validada.")
    print(f"Gis_ID esperats: {len(gis_ids)}")
    print(f"HTML processats: {len(gis_ids_processables)}")
    print(f"HTML absents documentats: {len(gis_ids_faltants)}")
    print(f"Files de característiques tècniques: {len(caracteristiques_totals)}")
    print(f"Files de nivells mesurats: {len(mesures_totals)}")
    print(
        "Gis_ID sense característiques tècniques: "
        f"{len(ids_sense_caracteristiques)}"
    )
    print(f"Gis_ID sense nivells mesurats: {len(ids_sense_mesures)}")
    print(f"Incidències de parseig: {len(incidencies_totals)}")
    print(
        "Repeticions exactes conservades a característiques: "
        f"{duplicats_caracteristiques}"
    )
    print(
        "Repeticions exactes conservades a nivells: "
        f"{duplicats_mesures}"
    )
    print(f"Fitxer generat: {ruta_caracteristiques}")
    print(f"Fitxer generat: {ruta_mesures}")
    print(f"HTML absents documentats a: {ruta_html_faltants}")
    print(f"Resum de validació: {ruta_resum}")


if __name__ == "__main__":
    main()
