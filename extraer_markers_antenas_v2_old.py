#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Extractor de detalle de antenasmoviles.es siguiendo el PDF SITEP.

Entrada:
  salida_antenas/Antenas.csv

Por cada codigo_antena diferente:
  GET https://antenasmoviles.es/v1/markers?id=COD_ANT

Reglas:
- 1 petición por codigo_antena.
- 10 peticiones/minuto por defecto.
- Sin paralelismo.
- Reintentos prudentes ante 429, 5xx y errores de red.
- Checkpoint para poder reanudar.
- Conserva la respuesta cruda en JSONL.

Salidas:
  salida_markers/
    Detalle_antenas.csv
    Frecuencias_antenas.csv
    Imagenes_antenas.csv
    markers_raw.jsonl
    checkpoint_markers.json
    errores_markers.csv

Por defecto procesa solo 10 antenas para validación.
Usa --limit 0 para procesarlas todas.
"""

import argparse
import csv
import json
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE_URL = "https://antenasmoviles.es/v1/markers"
THUMB_PREFIX = "https://antenasmoviles.es/archive/antenas/thumb/"
IMAGE_PREFIX = "https://antenasmoviles.es/archive/antenas/"

DEFAULT_RPM = 10.0
DEFAULT_LIMIT = 10
DEFAULT_TIMEOUT = 60
DEFAULT_MAX_RETRIES = 5
DEFAULT_MAX_CONSECUTIVE_ERRORS = 3


def leer_codigos(path: Path):
    """Lee codigo_antena como texto y elimina duplicados conservando el orden."""
    codigos = []
    vistos = set()

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=";")

        if "codigo_antena" not in (reader.fieldnames or []):
            raise ValueError("El CSV de entrada no contiene la columna 'codigo_antena'.")

        for row in reader:
            codigo = (row.get("codigo_antena") or "").strip()

            if codigo and codigo not in vistos:
                vistos.add(codigo)
                codigos.append(codigo)

    return codigos


def cargar_checkpoint(path: Path):
    if not path.exists():
        return {"procesados": []}

    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    data.setdefault("procesados", [])
    return data


def guardar_checkpoint(path: Path, checkpoint):
    """Escritura atómica del checkpoint."""
    temporal = path.with_suffix(".tmp")

    with temporal.open("w", encoding="utf-8") as f:
        json.dump(checkpoint, f, ensure_ascii=False, indent=2)

    temporal.replace(path)


def resetear_salida(output_dir: Path):
    if output_dir.exists():
        shutil.rmtree(output_dir)

    output_dir.mkdir(parents=True, exist_ok=True)


def obtener_retry_after(headers):
    valor = headers.get("Retry-After")

    if not valor:
        return None

    try:
        return max(0, int(valor))
    except ValueError:
        return None


def pedir_marker(codigo, timeout, max_retries):
    url = BASE_URL + "?" + urllib.parse.urlencode({"id": codigo})

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; SITEP-Antenas/1.0)",
            "Accept": "application/json",
        },
    )

    for intento in range(max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                contenido = response.read().decode(charset)
                return json.loads(contenido), response.status

        except urllib.error.HTTPError as exc:
            status = exc.code

            if status == 429:
                retry_after = obtener_retry_after(exc.headers)
                espera = (
                    retry_after
                    if retry_after is not None
                    else min(60 * (intento + 1), 300)
                )

            elif 500 <= status <= 599:
                espera = min(10 * (2 ** intento), 300)

            else:
                raise

            if intento >= max_retries:
                raise

            print(
                f"      HTTP {status}. "
                f"Reintento {intento + 1}/{max_retries} en {espera}s..."
            )
            time.sleep(espera)

        except (urllib.error.URLError, TimeoutError) as exc:
            if intento >= max_retries:
                raise

            espera = min(10 * (2 ** intento), 300)
            print(
                f"      Error de red ({exc}). "
                f"Reintento {intento + 1}/{max_retries} en {espera}s..."
            )
            time.sleep(espera)

    raise RuntimeError("No se ha podido obtener respuesta.")


def append_csv(path: Path, fieldnames, rows):
    if not rows:
        return

    nuevo = not path.exists()

    with path.open("a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=fieldnames,
            delimiter=";",
            extrasaction="ignore",
        )

        if nuevo:
            writer.writeheader()

        writer.writerows(rows)


def max_campos_extra_existentes(path: Path):
    if not path.exists():
        return 0

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f, delimiter=";")
        try:
            cabecera = next(reader)
        except StopIteration:
            return 0

    maximo = 0

    for nombre in cabecera:
        if nombre.startswith("CAMPO_"):
            try:
                maximo = max(maximo, int(nombre.split("_", 1)[1]))
            except ValueError:
                pass

    return maximo


def ampliar_cabecera_frecuencias(path: Path, nuevo_maximo: int):
    """
    Si aparece una frecuencia con más CAMPO_n de los vistos hasta ahora,
    reescribe el CSV existente añadiendo las nuevas columnas vacías.
    """
    if not path.exists():
        return

    max_actual = max_campos_extra_existentes(path)

    if nuevo_maximo <= max_actual:
        return

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=";")
        filas = list(reader)

    fieldnames = [
        "codigo_antena",
        "codigo_frecuencia",
        "frecuencia",
    ] + [f"CAMPO_{i}" for i in range(1, nuevo_maximo + 1)]

    temporal = path.with_suffix(".tmp")

    with temporal.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=fieldnames,
            delimiter=";",
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(filas)

    temporal.replace(path)


def append_frecuencias(path: Path, rows):
    if not rows:
        return

    max_nuevo = max((len(row["_extras"]) for row in rows), default=0)
    max_actual = max_campos_extra_existentes(path)
    maximo = max(max_actual, max_nuevo)

    if path.exists() and maximo > max_actual:
        ampliar_cabecera_frecuencias(path, maximo)

    fieldnames = [
        "codigo_antena",
        "codigo_frecuencia",
        "frecuencia",
    ] + [f"CAMPO_{i}" for i in range(1, maximo + 1)]

    filas_csv = []

    for row in rows:
        salida = {
            "codigo_antena": row["codigo_antena"],
            "codigo_frecuencia": row["codigo_frecuencia"],
            "frecuencia": row["frecuencia"],
        }

        for i, valor in enumerate(row["_extras"], start=1):
            salida[f"CAMPO_{i}"] = valor

        filas_csv.append(salida)

    append_csv(path, fieldnames, filas_csv)


def procesar_respuesta(codigo_solicitado, data):
    detalles = []
    frecuencias = []
    imagenes = []

    if not isinstance(data, list):
        raise ValueError("La respuesta de /markers no es una lista.")

    for elemento in data:
        if not isinstance(elemento, list):
            continue

        empresa = ""
        if len(elemento) > 1 and isinstance(elemento[1], list) and elemento[1]:
            empresa = elemento[1][0]

        geo = elemento[2] if len(elemento) > 2 and isinstance(elemento[2], list) else []

        detalles.append({
            "codigo_antena": codigo_solicitado,
            "empresa_propietaria": empresa,
            "direccion": geo[0] if len(geo) > 0 else "",
            "latitud": geo[1] if len(geo) > 1 else "",
            "longitud": geo[2] if len(geo) > 2 else "",
            "altura": geo[3] if len(geo) > 3 else "",
        })

        # PDF:
        # [codigo_frecuencia, codigo_antena, frecuencia, CAMPO_1, CAMPO_2, ...]
        if len(elemento) > 3 and isinstance(elemento[3], list):
            for freq in elemento[3]:
                if not isinstance(freq, list):
                    continue

                frecuencias.append({
                    "codigo_antena": codigo_solicitado,
                    "codigo_frecuencia": freq[0] if len(freq) > 0 else "",
                    "frecuencia": freq[2] if len(freq) > 2 else "",
                    "_extras": freq[3:] if len(freq) > 3 else [],
                })

        if len(elemento) > 4 and isinstance(elemento[4], list):
            for imagen in elemento[4]:
                if isinstance(imagen, dict):
                    fichero = str(imagen.get("fichero", "") or "")
                else:
                    fichero = str(imagen or "")

                if not fichero:
                    continue

                imagenes.append({
                    "codigo_antena": codigo_solicitado,
                    "ruta_miniatura": THUMB_PREFIX + fichero,
                    "ruta_imagen": IMAGE_PREFIX + fichero,
                })

    return detalles, frecuencias, imagenes


def main():
    parser = argparse.ArgumentParser(
        description="Extractor conservador de /v1/markers."
    )

    parser.add_argument(
        "--input",
        default="salida_antenas/Antenas.csv",
        help="CSV de entrada. Por defecto: salida_antenas/Antenas.csv",
    )

    parser.add_argument(
        "--output-dir",
        default="salida_markers",
        help="Carpeta de salida. Por defecto: salida_markers",
    )

    parser.add_argument(
        "--rpm",
        type=float,
        default=DEFAULT_RPM,
        help="Peticiones por minuto. Por defecto: 10",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help="Máximo de códigos en esta ejecución. 0=todos. Por defecto: 10",
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
    )

    parser.add_argument(
        "--max-retries",
        type=int,
        default=DEFAULT_MAX_RETRIES,
    )

    parser.add_argument(
        "--max-consecutive-errors",
        type=int,
        default=DEFAULT_MAX_CONSECUTIVE_ERRORS,
        help="Se detiene si alcanza este número de errores consecutivos.",
    )

    parser.add_argument(
        "--reset",
        action="store_true",
        help="Borra la carpeta de salida antes de comenzar.",
    )

    args = parser.parse_args()

    if args.rpm <= 0:
        raise ValueError("--rpm debe ser mayor que 0.")

    if args.max_consecutive_errors <= 0:
        raise ValueError("--max-consecutive-errors debe ser mayor que 0.")

    input_path = Path(args.input)
    output_dir = Path(args.output_dir)

    if not input_path.exists():
        raise FileNotFoundError(f"No existe el CSV de entrada: {input_path}")

    if args.reset:
        resetear_salida(output_dir)
    else:
        output_dir.mkdir(parents=True, exist_ok=True)

    detalle_path = output_dir / "Detalle_antenas.csv"
    frecuencias_path = output_dir / "Frecuencias_antenas.csv"
    imagenes_path = output_dir / "Imagenes_antenas.csv"
    raw_path = output_dir / "markers_raw.jsonl"
    checkpoint_path = output_dir / "checkpoint_markers.json"
    errores_path = output_dir / "errores_markers.csv"

    codigos = leer_codigos(input_path)
    checkpoint = cargar_checkpoint(checkpoint_path)
    procesados = set(checkpoint.get("procesados", []))

    pendientes = [codigo for codigo in codigos if codigo not in procesados]

    if args.limit > 0:
        pendientes = pendientes[:args.limit]

    intervalo = 60.0 / args.rpm

    print(f"Antenas únicas       : {len(codigos)}")
    print(f"Ya procesadas        : {len(procesados)}")
    print(f"A tratar ahora       : {len(pendientes)}")
    print(f"Ritmo                : {args.rpm:g} peticiones/minuto")
    print(f"Intervalo mínimo     : {intervalo:.2f}s")
    print()

    if not pendientes:
        print("No hay antenas pendientes.")
        return

    errores_consecutivos = 0

    for posicion, codigo in enumerate(pendientes, start=1):
        inicio = time.monotonic()

        print(f"[{posicion}/{len(pendientes)}] {codigo}")

        try:
            data, status = pedir_marker(
                codigo,
                timeout=args.timeout,
                max_retries=args.max_retries,
            )

            # Guardamos la respuesta íntegra para trazabilidad.
            with raw_path.open("a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "codigo_antena": codigo,
                            "http_status": status,
                            "data": data,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )

            detalles, frecuencias, imagenes = procesar_respuesta(codigo, data)

            append_csv(
                detalle_path,
                [
                    "codigo_antena",
                    "empresa_propietaria",
                    "direccion",
                    "latitud",
                    "longitud",
                    "altura",
                ],
                detalles,
            )

            append_frecuencias(
                frecuencias_path,
                frecuencias,
            )

            append_csv(
                imagenes_path,
                [
                    "codigo_antena",
                    "ruta_miniatura",
                    "ruta_imagen",
                ],
                imagenes,
            )

            checkpoint.setdefault("procesados", []).append(codigo)
            guardar_checkpoint(checkpoint_path, checkpoint)

            errores_consecutivos = 0

            print(
                f"      OK detalle={len(detalles)} "
                f"frecuencias={len(frecuencias)} "
                f"imagenes={len(imagenes)}"
            )

        except Exception as exc:
            errores_consecutivos += 1

            append_csv(
                errores_path,
                ["codigo_antena", "error"],
                [
                    {
                        "codigo_antena": codigo,
                        "error": str(exc),
                    }
                ],
            )

            print(f"      ERROR: {exc}")
            print(
                f"      Errores consecutivos: "
                f"{errores_consecutivos}/{args.max_consecutive_errors}"
            )

            if errores_consecutivos >= args.max_consecutive_errors:
                print()
                print(
                    "Proceso detenido preventivamente por demasiados "
                    "errores consecutivos."
                )
                break

        # El límite se aplica entre inicios de solicitudes.
        transcurrido = time.monotonic() - inicio
        espera = intervalo - transcurrido

        if posicion < len(pendientes) and espera > 0:
            print(f"      Esperando {espera:.2f}s...")
            time.sleep(espera)

    print()
    print("Ejecución finalizada.")
    print(
        f"Procesadas acumuladas: "
        f"{len(checkpoint.get('procesados', []))}/{len(codigos)}"
    )
    print(f"Salida: {output_dir.resolve()}")


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print()
        print("Proceso interrumpido. El checkpoint permite reanudar.")
        sys.exit(130)

    except Exception as exc:
        print()
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
