import argparse
import csv
import json
import math
import os
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import quote


URL_RADIATIONS = "https://antenasmoviles.es/v1/radiations?id={}"

INPUT_CSV = os.path.join("salida_antenas", "Antenas.csv")
OUTPUT_DIR = "salida_radiaciones"

OUTPUT_CSV = os.path.join(OUTPUT_DIR, "Radiaciones_antenas.csv")
RAW_JSONL = os.path.join(OUTPUT_DIR, "radiaciones_raw.jsonl")
CHECKPOINT = os.path.join(OUTPUT_DIR, "checkpoint_radiaciones.json")
ERRORS_CSV = os.path.join(OUTPUT_DIR, "errores_radiaciones.csv")

REQUESTS_PER_MINUTE_DEFAULT = 10
EARTH_RADIUS_M = 6378137.0


def calcular_destino(lat_deg, lon_deg, distancia_m, azimut_deg):
    """
    Calcula la coordenada destino desde una coordenada WGS84
    desplazando una distancia en metros con un azimut:
      0° = Norte
      90° = Este
      180° = Sur
      270° = Oeste
    """
    lat1 = math.radians(lat_deg)
    lon1 = math.radians(lon_deg)
    bearing = math.radians(azimut_deg)

    angular_distance = distancia_m / EARTH_RADIUS_M

    lat2 = math.asin(
        math.sin(lat1) * math.cos(angular_distance)
        + math.cos(lat1)
        * math.sin(angular_distance)
        * math.cos(bearing)
    )

    lon2 = lon1 + math.atan2(
        math.sin(bearing)
        * math.sin(angular_distance)
        * math.cos(lat1),
        math.cos(angular_distance)
        - math.sin(lat1) * math.sin(lat2),
    )

    return math.degrees(lat2), math.degrees(lon2)


def leer_antenas():
    antenas = {}

    with open(INPUT_CSV, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=";")

        for row in reader:
            codigo = row["codigo_antena"].strip()

            if not codigo:
                continue

            if codigo not in antenas:
                antenas[codigo] = {
                    "codigo_antena": codigo,
                    "latitud": float(row["latitud"]),
                    "longitud": float(row["longitud"]),
                }

    return list(antenas.values())


def cargar_checkpoint():
    if not os.path.exists(CHECKPOINT):
        return set()

    with open(CHECKPOINT, "r", encoding="utf-8") as f:
        data = json.load(f)

    return set(data.get("procesados", []))


def guardar_checkpoint(procesados):
    temporal = CHECKPOINT + ".tmp"

    with open(temporal, "w", encoding="utf-8") as f:
        json.dump(
            {"procesados": sorted(procesados)},
            f,
            ensure_ascii=False,
            indent=2,
        )

    os.replace(temporal, CHECKPOINT)


def preparar_salida(reset=False):
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    if reset:
        for path in (OUTPUT_CSV, RAW_JSONL, CHECKPOINT, ERRORS_CSV):
            if os.path.exists(path):
                os.remove(path)

    if not os.path.exists(OUTPUT_CSV):
        with open(OUTPUT_CSV, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f, delimiter=";")
            writer.writerow(
                [
                    "codigo_antena",
                    "latitud_antena",
                    "longitud_antena",
                    "distancia",
                    "azimut",
                    "valor_medido",
                    "indicador_origen",
                    "fecha_medicion",
                    "latitud_medicion",
                    "longitud_medicion",
                ]
            )

    if not os.path.exists(ERRORS_CSV):
        with open(ERRORS_CSV, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f, delimiter=";")
            writer.writerow(["codigo_antena", "error"])


def pedir_radiaciones(codigo, max_reintentos=4):
    url = URL_RADIATIONS.format(quote(codigo, safe=""))

    for intento in range(1, max_reintentos + 1):
        try:
            req = Request(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 Chrome/120 Safari/537.36"
                    ),
                    "Accept": "application/json",
                    "Referer": "https://antenasmoviles.es/",
                },
            )

            with urlopen(req, timeout=30) as response:
                contenido = response.read().decode("utf-8")
                return json.loads(contenido)

        except HTTPError as e:
            if e.code == 429:
                espera = 60
                retry_after = e.headers.get("Retry-After")

                if retry_after:
                    try:
                        espera = max(espera, int(retry_after))
                    except ValueError:
                        pass

                print(f"      HTTP 429. Esperando {espera}s...")
                time.sleep(espera)
                continue

            if 500 <= e.code <= 599 and intento < max_reintentos:
                espera = intento * 15
                print(
                    f"      HTTP {e.code}. "
                    f"Reintento {intento}/{max_reintentos} en {espera}s..."
                )
                time.sleep(espera)
                continue

            raise

        except (URLError, TimeoutError) as e:
            if intento < max_reintentos:
                espera = intento * 15
                print(
                    f"      Error de red. "
                    f"Reintento {intento}/{max_reintentos} en {espera}s..."
                )
                time.sleep(espera)
                continue

            raise


def guardar_raw(codigo, respuesta):
    with open(RAW_JSONL, "a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "codigo_antena": codigo,
                    "respuesta": respuesta,
                },
                ensure_ascii=False,
            )
            + "\n"
        )


def guardar_error(codigo, error):
    with open(ERRORS_CSV, "a", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow([codigo, str(error)])


def guardar_radiaciones(antena, respuesta):
    filas = []

    for elemento in respuesta:
        if not isinstance(elemento, list) or len(elemento) < 5:
            continue

        distancia_raw = elemento[0]
        azimut_raw = elemento[1]
        valor_raw = elemento[2]
        indicador = elemento[3]
        fecha = elemento[4]

        try:
            distancia = float(distancia_raw)
            azimut = float(azimut_raw)
        except (TypeError, ValueError):
            continue

        lat_medicion, lon_medicion = calcular_destino(
            antena["latitud"],
            antena["longitud"],
            distancia,
            azimut,
        )

        filas.append(
            [
                antena["codigo_antena"],
                f'{antena["latitud"]:.8f}',
                f'{antena["longitud"]:.8f}',
                distancia_raw,
                azimut_raw,
                valor_raw,
                str(indicador).lower(),
                fecha,
                f"{lat_medicion:.8f}",
                f"{lon_medicion:.8f}",
            ]
        )

    if filas:
        with open(OUTPUT_CSV, "a", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f, delimiter=";")
            writer.writerows(filas)

    return len(filas)


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Número máximo de códigos a tratar. 0 = todos.",
    )

    parser.add_argument(
        "--rpm",
        type=int,
        default=REQUESTS_PER_MINUTE_DEFAULT,
        help="Peticiones por minuto.",
    )

    parser.add_argument(
        "--reset",
        action="store_true",
        help="Borra la salida previa y comienza desde cero.",
    )

    args = parser.parse_args()

    preparar_salida(args.reset)

    antenas = leer_antenas()
    procesados = cargar_checkpoint()

    pendientes = [
        antena
        for antena in antenas
        if antena["codigo_antena"] not in procesados
    ]

    if args.limit > 0:
        pendientes = pendientes[: args.limit]

    intervalo = 60.0 / args.rpm

    print(f"Antenas únicas       : {len(antenas)}")
    print(f"Ya procesadas        : {len(procesados)}")
    print(f"A tratar ahora       : {len(pendientes)}")
    print(f"Ritmo                : {args.rpm} peticiones/minuto")
    print(f"Intervalo mínimo     : {intervalo:.2f}s")
    print()

    errores_consecutivos = 0

    for i, antena in enumerate(pendientes, start=1):
        codigo = antena["codigo_antena"]

        print(f"[{i}/{len(pendientes)}] {codigo}")

        inicio = time.monotonic()

        try:
            respuesta = pedir_radiaciones(codigo)

            guardar_raw(codigo, respuesta)

            numero = guardar_radiaciones(antena, respuesta)

            procesados.add(codigo)
            guardar_checkpoint(procesados)

            errores_consecutivos = 0

            print(f"      OK radiaciones={numero}")

        except Exception as e:
            errores_consecutivos += 1
            guardar_error(codigo, e)

            print(f"      ERROR: {e}")

            if errores_consecutivos >= 3:
                print()
                print(
                    "Se detiene la ejecución tras "
                    "3 errores consecutivos."
                )
                break

        transcurrido = time.monotonic() - inicio
        espera = intervalo - transcurrido

        if espera > 0:
            time.sleep(espera)

    print()
    print("Ejecución finalizada.")
    print(f"Procesadas acumuladas: {len(procesados)}/{len(antenas)}")
    print(f"Salida: {os.path.abspath(OUTPUT_DIR)}")


if __name__ == "__main__":
    main()
