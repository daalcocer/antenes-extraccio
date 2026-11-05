# -*- coding: utf-8 -*-

import contextlib
import csv
import email.message
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from PIL import Image

import extraer_imagenes_infoantenas as m

# Respuesta real (reducida) de /v1/markers?id=0000758 tomada de markers_raw.jsonl.
MARKER_REAL = [[
    ["0000758"],
    ["VODAFONE ESPAÑA, S.A.U.", "1", "55"],
    ["CL SANT MARIA, 99. VILADECANS, BARCELONA", 41.321464, 2.014425, "41m"],
    [["BB-2402052", "0000758", "949.90 - 959.90", False, "24/09/2024", "03/09/2026", "24/09/2024", "1"]],
    [{
        "id": "173568",
        "fichero": "1729885461_6fbc2e6031932d57e56c.jpg",
        "autor": "Planer",
        "fecha_foto": "2024-10-25 21:41:01",
        "fecha_subida": "2024-10-25 21:44:21",
        "old": None,
        "desmantelada": None,
        "lat_subida": "41.321464",
        "lon_subida": "2.014425",
        "asociadas": "0000758",
    }],
    False,
    [],
]]

CABECERA_INVENTARIO = [
    "feature_type", "geometry_type", "geometry_longitud", "geometry_latitud",
    "properties_Gis_Latitud", "properties_Gis_Longitud", "properties_Gis_ID",
    "properties_Gis_Codigo",
]


def escribir_inventario(path, filas):
    """filas: (gis_id, operador, lat, lon)."""
    with Path(path).open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(CABECERA_INVENTARIO)
        for gis_id, operador, lat, lon in filas:
            writer.writerow([
                "Feature", "Point", lon, lat, lat, lon, gis_id,
                f"{operador} - {gis_id}",
            ])


def marker(codigo, operador, lat, lon, imagenes, codigo_operador="1"):
    return [[
        [codigo],
        [operador, codigo_operador, "1"],
        ["DIRECCION", lat, lon, "10m"],
        [],
        imagenes,
        False,
        [],
    ]]


def imagen(id_, fichero, asociadas=""):
    return {
        "id": id_, "fichero": fichero, "autor": "autor",
        "fecha_foto": "2024-01-01 10:00:00", "fecha_subida": "2024-01-01 10:01:00",
        "old": None, "desmantelada": "1", "lat_subida": "41.1", "lon_subida": "2.1",
        "asociadas": asociadas,
    }


def inventario_de(filas):
    with tempfile.TemporaryDirectory() as tmp:
        ruta = Path(tmp) / "inv.csv"
        escribir_inventario(ruta, filas)
        return m.leer_inventario(ruta)


class TestInventario(unittest.TestCase):
    def test_preserva_ceros_iniciales_y_texto_literal(self):
        inventario, est = inventario_de([
            ("0000758", "VODAFONE ESPAÑA, S.A.U.", 41.321464, 2.014425),
            ("000758", "VODAFONE ESPAÑA, S.A.U.", 41.321464, 2.014425),
            ("758", "VODAFONE ESPAÑA, S.A.U.", 41.321464, 2.014425),
            ("CATB0020Ñ", "ORANGE ESPAGNE, S.A.U.", 41.39, 2.12),
        ])

        self.assertEqual(
            list(inventario), ["0000758", "000758", "758", "CATB0020Ñ"]
        )
        self.assertEqual(est["filas"], 4)

    def test_gis_id_con_dos_operadores(self):
        inventario, est = inventario_de([
            ("4300034", "DS MOBILE NETWORKS, S.L.", 40.546265, 0.483325),
            ("4300034", "TELEFONICA MOVILES ESPAÑA, S.A.U.", 40.546265, 0.483325),
        ])

        entrada = inventario["4300034"]
        self.assertEqual(len(inventario), 1)
        self.assertEqual(entrada["filas"], 2)
        self.assertEqual(
            entrada["operadores"],
            ["DS MOBILE NETWORKS, S.L.", "TELEFONICA MOVILES ESPAÑA, S.A.U."],
        )
        self.assertEqual(entrada["coordenadas"], [(40.546265, 0.483325)])

    def test_operador_no_deducible(self):
        self.assertIsNone(m.extraer_operador("OTRO TEXTO", "123"))
        self.assertEqual(m.extraer_operador("ORANGE - 123", "123"), "ORANGE")

    def test_falta_columna_obligatoria(self):
        with tempfile.TemporaryDirectory() as tmp:
            ruta = Path(tmp) / "inv.csv"
            ruta.write_text("a;b\n1;2\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                m.leer_inventario(ruta)


class TestParser(unittest.TestCase):
    def test_parsea_marker_real(self):
        elementos = m.parsear_marker(MARKER_REAL)

        self.assertEqual(len(elementos), 1)
        e = elementos[0]
        self.assertEqual(e["codigo"], "0000758")
        self.assertEqual(e["empresa"], "VODAFONE ESPAÑA, S.A.U.")
        self.assertEqual(e["codigo_operador"], "1")
        self.assertEqual(e["latitud"], 41.321464)
        self.assertEqual(e["longitud"], 2.014425)
        self.assertEqual(len(e["imagenes"]), 1)

        img = e["imagenes"][0]
        self.assertEqual(img["id"], "173568")
        self.assertEqual(img["fichero"], "1729885461_6fbc2e6031932d57e56c.jpg")
        self.assertEqual(img["autor"], "Planer")
        self.assertEqual(img["old"], "")
        self.assertEqual(img["desmantelada"], "")
        self.assertEqual(img["asociadas"], "0000758")

    def test_respuesta_no_lista(self):
        with self.assertRaises(ValueError):
            m.parsear_marker({"error": "x"})

    def test_lista_vacia(self):
        self.assertEqual(m.parsear_marker([]), [])

    def test_imagen_sin_fichero_se_descarta(self):
        data = marker("1", "OP", 41.0, 2.0, [imagen("1", ""), imagen("2", "a.jpg")])
        e = m.parsear_marker(data)[0]
        self.assertEqual([i["fichero"] for i in e["imagenes"]], ["a.jpg"])
        self.assertEqual(e["imagenes_descartadas"], 1)


class TestDistancia(unittest.TestCase):
    def test_cero(self):
        self.assertEqual(m.distancia_haversine_m(41.3, 2.0, 41.3, 2.0), 0.0)

    def test_un_grado_de_latitud(self):
        self.assertAlmostEqual(m.distancia_haversine_m(0, 0, 1, 0), 111195, delta=100)

    def test_par_real_cercano(self):
        d = m.distancia_haversine_m(41.321464, 2.014425, 41.321556, 2.014389)
        self.assertAlmostEqual(d, 10.7, delta=0.5)

    def test_percentiles(self):
        est = m.estadisticas_distancia([4.0, 1.0, 3.0, 2.0])
        self.assertEqual((est["n"], est["min"], est["max"]), (4, 1.0, 4.0))
        self.assertAlmostEqual(est["mediana"], 2.5)
        self.assertEqual(m.estadisticas_distancia([])["min"], None)


class TestCruce(unittest.TestCase):
    def cruzar(self, filas_inventario, respuestas):
        inventario, _ = inventario_de(filas_inventario)
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw.jsonl"
            with raw.open("w", encoding="utf-8") as f:
                for codigo, data in respuestas:
                    f.write(json.dumps(
                        {"codigo_antena": codigo, "http_status": 200, "data": data},
                        ensure_ascii=False,
                    ) + "\n")
            markers, est_raw = m.leer_markers_raw(raw)
        control, imagenes = m.cruzar(inventario, markers)
        return inventario, markers, est_raw, control, imagenes

    def test_gis_id_no_encontrado(self):
        _, _, _, control, imagenes = self.cruzar(
            [("999", "ORANGE ESPAGNE, S.A.U.", 41.0, 2.0)], []
        )

        self.assertEqual(len(control), 1)
        fila = control[0]
        self.assertFalse(fila["ENCONTRADO_ANTENASMOVILES"])
        self.assertIsNone(fila["OPERADOR_COINCIDE"])
        self.assertIsNone(fila["DISTANCIA_M"])
        self.assertEqual(fila["NUM_IMAGENES"], 0)
        self.assertIn("SIN_RESPUESTA_EN_MARKERS_RAW", fila["OBSERVACIONES"])
        self.assertEqual(imagenes, [])

    def test_coincidencia_literal_no_normaliza_ceros(self):
        _, _, _, control, _ = self.cruzar(
            [("56502", "VODAFONE ESPAÑA, S.A.U.", 41.56, 2.42)],
            [("056502", marker("056502", "VODAFONE ESPAÑA, S.A.U.", 41.56, 2.42, []))],
        )
        self.assertFalse(control[0]["ENCONTRADO_ANTENASMOVILES"])

    def test_dos_operadores_coincide_con_alguno(self):
        _, _, _, control, _ = self.cruzar(
            [
                ("4300034", "DS MOBILE NETWORKS, S.L.", 40.546265, 0.483325),
                ("4300034", "TELEFONICA MOVILES ESPAÑA, S.A.U.", 40.546265, 0.483325),
            ],
            [("4300034", marker("4300034", "DS MOBILE NETWORKS, S.L.", 40.546265, 0.483325, []))],
        )

        fila = control[0]
        self.assertTrue(fila["ENCONTRADO_ANTENASMOVILES"])
        self.assertIs(fila["OPERADOR_COINCIDE"], True)
        self.assertAlmostEqual(fila["DISTANCIA_M"], 0.0, places=6)
        self.assertIn("VARIOS_OPERADORES_INFOANTENAS", fila["OBSERVACIONES"])

    def test_operador_no_coincide(self):
        _, _, _, control, _ = self.cruzar(
            [("1", "ORANGE ESPAGNE, S.A.U.", 41.0, 2.0)],
            [("1", marker("1", "VODAFONE ESPAÑA, S.A.U.", 41.0, 2.001, []))],
        )

        self.assertIs(control[0]["OPERADOR_COINCIDE"], False)
        self.assertGreater(control[0]["DISTANCIA_M"], 0)
        self.assertIn("OPERADOR_NO_COINCIDE", control[0]["OBSERVACIONES"])

    def test_imagen_compartida_entre_varios_codigos(self):
        compartida = imagen("173568", "compartida.jpg")
        inventario, markers, est_raw, control, imagenes = self.cruzar(
            [
                ("0000758", "VODAFONE ESPAÑA, S.A.U.", 41.321464, 2.014425),
                ("000758", "VODAFONE ESPAÑA, S.A.U.", 41.321464, 2.014425),
                ("758", "VODAFONE ESPAÑA, S.A.U.", 41.321464, 2.014425),
            ],
            [
                (c, marker(c, "VODAFONE ESPAÑA, S.A.U.", 41.321464, 2.014425,
                           [dict(compartida, asociadas=c)]))
                for c in ("0000758", "000758", "758")
            ],
        )

        self.assertEqual(len(imagenes), 3)
        self.assertEqual({f["GIS_ID"] for f in imagenes}, {"0000758", "000758", "758"})
        self.assertEqual({f["URL_ORIGEN"] for f in imagenes},
                         {m.IMAGE_PREFIX + "compartida.jpg"})

        resumen = m.construir_resumen(
            inventario, {"filas": 3, "filas_sin_gis_id": 0, "filas_operador_no_deducible": 0},
            markers, est_raw, control, imagenes,
        )
        self.assertEqual(resumen["imagenes"]["relaciones_gis_id_imagen"], 3)
        self.assertEqual(resumen["imagenes"]["unicas_por_id"], 1)
        self.assertEqual(resumen["imagenes"]["unicas_por_fichero"], 1)
        self.assertEqual(resumen["cruce"]["gis_id_encontrados"], 3)

    def test_codi_operador_por_fila_de_imagen(self):
        _, _, _, _, imagenes = self.cruzar(
            [
                ("0000758", "VODAFONE ESPAÑA, S.A.U.", 41.0, 2.0),
                ("000758", "ORANGE ESPAGNE, S.A.U.", 41.0, 2.0),
                ("4300034", "TELEFONICA MOVILES ESPAÑA, S.A.U.", 41.0, 2.0),
            ],
            [
                ("0000758", marker("0000758", "VODAFONE", 41.0, 2.0,
                                   [imagen("1", "a.jpg")], "1")),
                ("000758", marker("000758", "ORANGE", 41.0, 2.0,
                                  [imagen("1", "a.jpg")], "3")),
                # Un marker con dos elementos de operadores distintos.
                ("4300034", marker("4300034", "TELEFONICA", 41.0, 2.0,
                                   [imagen("2", "b.jpg")], "7")
                 + marker("4300034", "DIGI", 41.0, 2.0, [imagen("3", "c.jpg")], "22")),
            ],
        )

        self.assertEqual(
            [(f["GIS_ID"], f["NOMBRE_FICHERO_ORIGEN"], f["CODI_OPERADOR"]) for f in imagenes],
            [("0000758", "a.jpg", "1"), ("000758", "a.jpg", "3"),
             ("4300034", "b.jpg", "7"), ("4300034", "c.jpg", "22")],
        )

    def test_raw_con_codigo_fuera_del_inventario(self):
        inventario, markers, est_raw, control, _ = self.cruzar(
            [("1", "OP", 41.0, 2.0)],
            [("1", marker("1", "OP", 41.0, 2.0, [])), ("X", marker("X", "OP", 41.0, 2.0, []))],
        )
        resumen = m.construir_resumen(
            inventario, {"filas": 1, "filas_sin_gis_id": 0, "filas_operador_no_deducible": 0},
            markers, est_raw, control, [],
        )
        self.assertEqual(resumen["markers_raw"]["codigos_fuera_del_inventario_lista"], ["X"])


class TestSalida(unittest.TestCase):
    def test_ejecutar_genera_ficheros_con_formato(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            escribir_inventario(tmp / "inv.csv", [
                ("0000758", "VODAFONE ESPAÑA, S.A.U.", 41.321464, 2.014425),
                ("999", "ORANGE ESPAGNE, S.A.U.", 41.0, 2.0),
            ])
            (tmp / "raw.jsonl").write_text(
                json.dumps({"codigo_antena": "0000758", "http_status": 200,
                            "data": MARKER_REAL}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            resumen = m.ejecutar(tmp / "inv.csv", tmp / "raw.jsonl", tmp / "out")

            self.assertEqual(resumen["inventario"]["gis_id_unicos"], 2)
            self.assertEqual(resumen["cruce"]["gis_id_no_encontrados"], 1)
            self.assertFalse(list((tmp / "out").glob("*.tmp")))

            with (tmp / "out" / "CONTROL_GIS_ID.csv").open(encoding="utf-8-sig", newline="") as f:
                control = list(csv.DictReader(f, delimiter=";"))
            self.assertEqual([r["GIS_ID"] for r in control], ["0000758", "999"])
            self.assertEqual(control[0]["ENCONTRADO_ANTENASMOVILES"], "SI")
            self.assertEqual(control[1]["ENCONTRADO_ANTENASMOVILES"], "NO")

            with (tmp / "out" / "IMATGES_INFOANTENAS.csv").open(encoding="utf-8-sig", newline="") as f:
                imagenes = list(csv.DictReader(f, delimiter=";"))
            self.assertEqual(len(imagenes), 1)
            self.assertEqual(imagenes[0]["GIS_ID"], "0000758")
            self.assertEqual(imagenes[0]["CODI_OPERADOR"], "1")
            with (tmp / "out" / "IMATGES_INFOANTENAS.csv").open(encoding="utf-8-sig", newline="") as f:
                self.assertEqual(next(csv.reader(f, delimiter=";")), m.CAMPOS_IMAGENES)
            self.assertIn("CODI_OPERADOR", m.CAMPOS_IMAGENES)
            self.assertEqual(
                imagenes[0]["URL_ORIGEN"],
                "https://antenasmoviles.es/archive/antenas/1729885461_6fbc2e6031932d57e56c.jpg",
            )
            self.assertEqual(imagenes[0]["OPERADOR_COINCIDE"], "SI")

    def test_mismo_gis_id_literal_con_dos_operadores_se_resuelve_por_gis_id_y_codi_operador(self):
        vodafone = "VODAFONE ESPAÑA, S.A.U."
        telefonica = "TELEFONICA MOVILES ESPAÑA, S.A.U."
        compartida = imagen("10", "compartida.jpg")
        respuesta = (
            marker("4300034", vodafone, 40.5, 0.4,
                   [compartida, imagen("11", "solo_vodafone.jpg")], "1")
            + marker("4300034", telefonica, 40.5, 0.4,
                     [compartida, imagen("12", "solo_telefonica.jpg")], "7")
        )

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            escribir_inventario(tmp / "inv.csv", [
                ("4300034", vodafone, 40.5, 0.4),
                ("4300034", telefonica, 40.5, 0.4),
            ])
            (tmp / "raw.jsonl").write_text(
                json.dumps({"codigo_antena": "4300034", "http_status": 200,
                            "data": respuesta}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            resumen = m.ejecutar(tmp / "inv.csv", tmp / "raw.jsonl", tmp / "out")

            with (tmp / "out" / "IMATGES_INFOANTENAS.csv").open(encoding="utf-8-sig", newline="") as f:
                filas = list(csv.DictReader(f, delimiter=";"))

        self.assertEqual(resumen["inventario"]["gis_id_unicos"], 1)
        self.assertEqual({f["GIS_ID"] for f in filas}, {"4300034"})

        # La clave funcional es (GIS_ID, CODI_OPERADOR); el fichero solo es físico.
        claves = [(f["GIS_ID"], f["CODI_OPERADOR"], f["ID_IMAGEN_ORIGEN"]) for f in filas]
        self.assertEqual(sorted(claves), [
            ("4300034", "1", "10"), ("4300034", "1", "11"),
            ("4300034", "7", "10"), ("4300034", "7", "12"),
        ])

        por_operador = {
            codi: {f["ID_IMAGEN_ORIGEN"] for f in filas if f["CODI_OPERADOR"] == codi}
            for codi in ("1", "7")
        }
        self.assertEqual(por_operador, {"1": {"10", "11"}, "7": {"10", "12"}})

        # La imagen compartida genera una relación por operador, sin colapsarse.
        compartidas = [f for f in filas if f["NOMBRE_FICHERO_ORIGEN"] == "compartida.jpg"]
        self.assertEqual({f["CODI_OPERADOR"] for f in compartidas}, {"1", "7"})
        self.assertTrue(all(f["OPERADOR_COINCIDE"] == "SI" for f in filas))


# ---------------------------------------------------------------------------
# Fase 2: modo online (sin red real; el cliente HTTP está simulado)
# ---------------------------------------------------------------------------

def cabeceras(**valores):
    mensaje = email.message.Message()
    for nombre, valor in valores.items():
        mensaje[nombre.replace("_", "-")] = valor
    return mensaje


class RespuestaFalsa:
    def __init__(self, cuerpo, status=200, content_type="application/json; charset=utf-8",
                 content_length=None):
        self._flujo = io.BytesIO(cuerpo)
        self.status = status
        self.headers = cabeceras(Content_Type=content_type)
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def read(self, n=-1):
        return self._flujo.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def json_bytes(datos):
    return json.dumps(datos, ensure_ascii=False).encode("utf-8")


def http_error(codigo, cuerpo=b"", **cabecera):
    return urllib.error.HTTPError(
        "https://x", codigo, f"Error {codigo}", cabeceras(**cabecera), io.BytesIO(cuerpo)
    )


class Abridor:
    """Sustituye a urlopen: devuelve/lanza los elementos de la secuencia en orden."""

    def __init__(self, *secuencia, por_defecto=None):
        self.secuencia = list(secuencia)
        self.por_defecto = por_defecto
        self.peticiones = []

    def __call__(self, peticion, timeout):
        self.peticiones.append(peticion)
        elemento = self.secuencia.pop(0) if self.secuencia else self.por_defecto

        if isinstance(elemento, BaseException):
            raise elemento
        if elemento is None:
            raise AssertionError("Petición inesperada: " + peticion.full_url)
        return elemento


def marker_ok(codigo="1"):
    return RespuestaFalsa(json_bytes(marker(codigo, "OP", 41.0, 2.0, [imagen("1", "a.jpg")])))


def consultar(abridor, gis_id="1", reintentos=3, intervalo=0.0):
    pausas = []
    resultado = m.consultar_marker(
        gis_id,
        abridor,
        m.Limitador(intervalo),
        timeout=5,
        reintentos=reintentos,
        dormir=pausas.append,
        imprimir=lambda *_: None,
    )
    return resultado, pausas


class TestNombresYUrl(unittest.TestCase):
    def test_nombre_seguro_conserva_ceros(self):
        nombre = m.nombre_fichero_marker("0000758")
        self.assertRegex(nombre, r"^0000758_[0-9a-f]{12}\.json$")
        self.assertEqual(nombre, m.nombre_fichero_marker("0000758"))
        self.assertNotEqual(nombre, m.nombre_fichero_marker("000758"))

    def test_nombre_seguro_no_ascii(self):
        nombre = m.nombre_fichero_marker("CATB0020Ñ")
        self.assertRegex(nombre, r"^CATB0020%C3%91_[0-9a-f]{12}\.json$")
        self.assertTrue(nombre.isascii())

    def test_nombre_distingue_mayusculas(self):
        self.assertNotEqual(
            m.nombre_fichero_marker("abc").lower(), m.nombre_fichero_marker("ABC").lower()
        )

    def test_url_literal_y_codificada(self):
        self.assertEqual(m.construir_url_marker("0000758"), m.MARKERS_URL + "?id=0000758")
        self.assertEqual(
            m.construir_url_marker("CATB0020Ñ"), m.MARKERS_URL + "?id=CATB0020%C3%91"
        )

    def test_peticion_lleva_url_y_cabeceras(self):
        abridor = Abridor(marker_ok("CATB0020Ñ"))
        consultar(abridor, "CATB0020Ñ")

        peticion = abridor.peticiones[0]
        self.assertTrue(peticion.full_url.endswith("?id=CATB0020%C3%91"))
        self.assertEqual(peticion.get_header("Accept"), "application/json")
        self.assertTrue(peticion.get_header("User-agent"))


class TestClienteMarkers(unittest.TestCase):
    def test_429_con_retry_after(self):
        abridor = Abridor(http_error(429, Retry_After="7"), marker_ok())
        resultado, pausas = consultar(abridor)

        self.assertEqual(resultado["estado"], m.EST_CON_MARKER)
        self.assertEqual(resultado["intentos"], 2)
        self.assertEqual(pausas, [7.0])

    def test_429_sin_retry_after_usa_espera_por_defecto(self):
        abridor = Abridor(http_error(429), marker_ok())
        resultado, pausas = consultar(abridor)

        self.assertEqual(resultado["estado"], m.EST_CON_MARKER)
        self.assertEqual(pausas, [m.ESPERA_429_POR_DEFECTO_S])

    def test_5xx_se_reintenta(self):
        abridor = Abridor(http_error(503), http_error(502), marker_ok())
        resultado, pausas = consultar(abridor)

        self.assertEqual(resultado["estado"], m.EST_CON_MARKER)
        self.assertEqual(resultado["intentos"], 3)
        self.assertEqual(pausas, [10, 20])

    def test_5xx_agotado(self):
        abridor = Abridor(por_defecto=http_error(500))
        resultado, pausas = consultar(abridor, reintentos=2)

        self.assertEqual(resultado["estado"], m.EST_ERROR_HTTP_RECUPERABLE)
        self.assertEqual(resultado["http_status"], 500)
        self.assertEqual(resultado["intentos"], 3)
        self.assertEqual(len(pausas), 2)

    def test_error_de_red_recuperable(self):
        abridor = Abridor(urllib.error.URLError("sin conexion"), TimeoutError("lento"), marker_ok())
        resultado, _ = consultar(abridor)

        self.assertEqual(resultado["estado"], m.EST_CON_MARKER)
        self.assertEqual(resultado["intentos"], 3)

    def test_error_de_red_agotado(self):
        abridor = Abridor(por_defecto=urllib.error.URLError("sin conexion"))
        resultado, _ = consultar(abridor, reintentos=1)

        self.assertEqual(resultado["estado"], m.EST_ERROR_RED)
        self.assertEqual(resultado["intentos"], 2)
        self.assertIn("sin conexion", resultado["error"])

    def test_http_no_recuperable_no_se_reintenta(self):
        abridor = Abridor(http_error(403, b"<html>prohibido</html>"))
        resultado, pausas = consultar(abridor)

        self.assertEqual(resultado["estado"], m.EST_HTTP_NO_RECUPERABLE)
        self.assertEqual(resultado["http_status"], 403)
        self.assertEqual(resultado["intentos"], 1)
        self.assertEqual(pausas, [])
        self.assertIn("prohibido", resultado["muestra"])

    def test_http_404_se_distingue(self):
        resultado, _ = consultar(Abridor(http_error(404)))

        self.assertEqual(resultado["estado"], m.EST_HTTP_NO_ENCONTRADO)
        self.assertEqual(resultado["http_status"], 404)
        self.assertEqual(resultado["intentos"], 1)

    def test_json_vacio(self):
        resultado, _ = consultar(Abridor(RespuestaFalsa(b"[]")))

        self.assertEqual(resultado["estado"], m.EST_JSON_VACIO)
        self.assertEqual(resultado["data"], [])

    def test_estructura_inesperada(self):
        resultado, _ = consultar(Abridor(RespuestaFalsa(b'{"error": "x"}')))

        self.assertEqual(resultado["estado"], m.EST_ESTRUCTURA_INESPERADA)
        self.assertIsNone(resultado["data"])

    def test_lista_sin_marker_compatible(self):
        resultado, _ = consultar(Abridor(RespuestaFalsa(b"[1, 2]")))
        self.assertEqual(resultado["estado"], m.EST_SIN_MARKER_COMPATIBLE)

    def test_json_invalido_y_cuerpo_vacio(self):
        for cuerpo in (b"<html>Cloudflare</html>", b"", b"   "):
            resultado, _ = consultar(Abridor(RespuestaFalsa(cuerpo)))
            self.assertEqual(resultado["estado"], m.EST_JSON_INVALIDO)

    def test_registra_cabeceras_y_content_type(self):
        respuesta = RespuestaFalsa(b"[]")
        respuesta.headers["ETag"] = '"abc"'
        respuesta.headers["Set-Cookie"] = "ci_session=secreto"
        respuesta.headers["X-RateLimit-Remaining"] = "5"
        respuesta.headers["Report-To"] = "ruido"
        resultado, _ = consultar(Abridor(respuesta))

        self.assertEqual(resultado["content_type"], "application/json; charset=utf-8")
        self.assertEqual(resultado["cabeceras"]["etag"], '"abc"')
        self.assertEqual(resultado["cabeceras"]["x-ratelimit-remaining"], "5")
        self.assertNotIn("set-cookie", resultado["cabeceras"])
        self.assertNotIn("report-to", resultado["cabeceras"])
        self.assertEqual(resultado["bytes"], 2)

    def test_limitador_respeta_intervalo(self):
        ahora = [0.0]
        pausas = []

        def dormir(segundos):
            pausas.append(segundos)
            ahora[0] += segundos

        limitador = m.Limitador(6.0, reloj=lambda: ahora[0], dormir=dormir)
        limitador.esperar()
        limitador.marcar()
        ahora[0] += 2.0
        limitador.esperar()

        self.assertEqual(pausas, [4.0])


class TestRespuestasGuardadas(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def guardar(self, gis_id="1", data=None):
        ruta = self.dir / m.nombre_fichero_marker(gis_id)
        resultado = {
            "http_status": 200, "url": m.construir_url_marker(gis_id),
            "content_type": "application/json", "data": [] if data is None else data,
        }
        m.guardar_respuesta(ruta, gis_id, resultado)
        return ruta

    def test_guardar_y_leer(self):
        ruta = self.guardar("CATB0020Ñ", MARKER_REAL)
        registro = m.leer_respuesta_guardada(ruta, "CATB0020Ñ")

        self.assertEqual(registro["GIS_ID"], "CATB0020Ñ")
        self.assertEqual(registro["HTTP_STATUS"], 200)
        self.assertEqual(registro["RESPUESTA"], MARKER_REAL)
        self.assertFalse(list(self.dir.glob("*.part")))

    def test_invalidos(self):
        ruta = self.guardar("1")
        casos = {
            "vacio": "",
            "json_invalido": "{no es json",
            "no_objeto": "[]",
            "otro_gis_id": json.dumps({"GIS_ID": "2", "HTTP_STATUS": 200, "RESPUESTA": []}),
            "status_404": json.dumps({"GIS_ID": "1", "HTTP_STATUS": 404, "RESPUESTA": []}),
            "respuesta_no_lista": json.dumps({"GIS_ID": "1", "HTTP_STATUS": 200, "RESPUESTA": {}}),
        }

        for nombre, contenido in casos.items():
            with self.subTest(nombre):
                ruta.write_text(contenido, encoding="utf-8")
                self.assertIsNone(m.leer_respuesta_guardada(ruta, "1"))

        self.assertIsNone(m.leer_respuesta_guardada(self.dir / "no_existe.json", "1"))


class TestReanudacion(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.markers = self.tmp / "markers"
        self.markers.mkdir()
        self.log = self.tmp / "log_markers.csv"

    def pendientes(self, ids, abridor, **kwargs):
        return m.consultar_pendientes(
            ids, self.markers, self.log, abridor, m.Limitador(0.0),
            timeout=5, reintentos=0, dormir=lambda s: None,
            imprimir=lambda *_: None, **kwargs,
        )

    def leer_log(self):
        with self.log.open(encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f, delimiter=";"))

    def test_fichero_valido_se_reutiliza(self):
        self.pendientes(["1", "0002"], Abridor(marker_ok("1"), marker_ok("0002")))

        abridor = Abridor()
        resultado = self.pendientes(["1", "0002"], abridor)

        self.assertEqual(abridor.peticiones, [])
        self.assertEqual(resultado["contadores"], {m.EST_YA_EXISTENTE: 2})
        self.assertEqual([r["ESTADO"] for r in self.leer_log()], [m.EST_YA_EXISTENTE] * 2)

    def test_pendientes_invalidos_se_reconsultan(self):
        self.pendientes(["vacio", "corrupto", "ajeno", "bueno"],
                        Abridor(*[marker_ok() for _ in range(4)]))

        (self.markers / m.nombre_fichero_marker("vacio")).write_text("", encoding="utf-8")
        (self.markers / m.nombre_fichero_marker("corrupto")).write_text("{x", encoding="utf-8")
        (self.markers / m.nombre_fichero_marker("ajeno")).write_text(
            json.dumps({"GIS_ID": "otro", "HTTP_STATUS": 200, "RESPUESTA": []}), encoding="utf-8")

        abridor = Abridor(marker_ok(), marker_ok(), marker_ok())
        resultado = self.pendientes(["vacio", "corrupto", "ajeno", "bueno"], abridor)

        self.assertEqual(len(abridor.peticiones), 3)
        self.assertEqual(resultado["estados"]["bueno"], m.EST_YA_EXISTENTE)
        for gis_id in ("vacio", "corrupto", "ajeno"):
            self.assertEqual(resultado["estados"][gis_id], m.EST_CON_MARKER)
            self.assertIsNotNone(
                m.leer_respuesta_guardada(self.markers / m.nombre_fichero_marker(gis_id), gis_id)
            )

    def test_interrupcion_y_reanudacion(self):
        abridor = Abridor(marker_ok("1"), KeyboardInterrupt())

        with self.assertRaises(KeyboardInterrupt):
            self.pendientes(["1", "2", "3"], abridor)

        self.assertEqual([r["GIS_ID"] for r in self.leer_log()], ["1"])

        abridor = Abridor(marker_ok("2"), marker_ok("3"))
        resultado = self.pendientes(["1", "2", "3"], abridor)

        self.assertEqual(len(abridor.peticiones), 2)
        self.assertTrue(abridor.peticiones[0].full_url.endswith("id=2"))
        self.assertEqual(resultado["estados"]["1"], m.EST_YA_EXISTENTE)
        self.assertEqual([r["GIS_ID"] for r in self.leer_log()], ["1", "2", "3"])

    def test_un_gis_id_con_error_no_aborta(self):
        resultado = self.pendientes(
            ["a", "b", "c"], Abridor(http_error(403), http_error(404), marker_ok("c"))
        )

        self.assertEqual(
            list(resultado["estados"].values()),
            [m.EST_HTTP_NO_RECUPERABLE, m.EST_HTTP_NO_ENCONTRADO, m.EST_CON_MARKER],
        )
        self.assertFalse((self.markers / m.nombre_fichero_marker("a")).exists())
        self.assertFalse((self.markers / m.nombre_fichero_marker("b")).exists())

    def test_errores_no_persistidos_y_log_con_campos(self):
        self.pendientes(["a"], Abridor(http_error(403, b"prohibido")))
        fila = self.leer_log()[0]

        self.assertEqual(fila["ESTADO"], m.EST_HTTP_NO_RECUPERABLE)
        self.assertEqual(fila["HTTP_STATUS"], "403")
        self.assertEqual(fila["INTENTOS"], "1")
        self.assertEqual(fila["BYTES"], "9")
        self.assertIn("403", fila["ERROR"])
        self.assertEqual(fila["MUESTRA_CUERPO"], "prohibido")

    def test_log_es_nuevo_en_cada_invocacion(self):
        self.pendientes(["a"], Abridor(http_error(403)))
        self.pendientes(["a"], Abridor(marker_ok("a")))

        filas = self.leer_log()
        self.assertEqual(len(filas), 1)
        self.assertEqual(filas[0]["ESTADO"], m.EST_CON_MARKER)

    def test_parada_por_errores_consecutivos_configurable(self):
        abridor = Abridor(por_defecto=http_error(403))
        resultado = self.pendientes(["a", "b", "c", "d"], abridor, max_errores_consecutivos=2)

        self.assertTrue(resultado["detenido"])
        self.assertEqual(len(abridor.peticiones), 2)

        resultado = self.pendientes(["a", "b", "c"], Abridor(por_defecto=http_error(403)))
        self.assertFalse(resultado["detenido"])
        self.assertEqual(len(resultado["estados"]), 3)

    def test_no_encontrado_no_cuenta_como_error_consecutivo(self):
        abridor = Abridor(por_defecto=http_error(404))
        resultado = self.pendientes(["a", "b", "c"], abridor, max_errores_consecutivos=1)

        self.assertFalse(resultado["detenido"])


class TestRunJson(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.trabajo = Path(self._tmp.name) / "trabajo"
        self.addCleanup(self._tmp.cleanup)
        self.params = {"timeout": 5, "reintentos": 0}

    def preparar(self, sha="aaa", resume=False):
        return m.preparar_trabajo(self.trabajo, Path("inv.csv"), sha, self.params, resume)

    def test_nueva_ejecucion_escribe_run_y_limpia_estado_previo(self):
        self.preparar("aaa")
        (self.trabajo / "markers" / "viejo.json").write_text("{}", encoding="utf-8")
        (self.trabajo / "log_markers.csv").write_text("x", encoding="utf-8")

        run = self.preparar("bbb")

        self.assertEqual(run["inventario_sha256"], "bbb")
        self.assertEqual(run["formato"], m.FORMATO_RUN)
        self.assertEqual(run["parametros"], self.params)
        self.assertFalse((self.trabajo / "markers" / "viejo.json").exists())
        self.assertFalse((self.trabajo / "log_markers.csv").exists())
        guardado = json.loads((self.trabajo / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(guardado["inventario_sha256"], "bbb")

    def test_resume_con_mismo_sha(self):
        inicial = self.preparar("aaa")
        (self.trabajo / "markers" / "respuesta.json").write_text("{}", encoding="utf-8")

        run = self.preparar("aaa", resume=True)

        self.assertEqual(run["inicio"], inicial["inicio"])
        self.assertEqual(len(run["reanudaciones"]), 1)
        self.assertTrue((self.trabajo / "markers" / "respuesta.json").exists())

    def test_resume_con_sha_distinto_se_rechaza(self):
        self.preparar("aaa")

        with self.assertRaises(ValueError) as ctx:
            self.preparar("bbb", resume=True)

        self.assertIn("SHA-256", str(ctx.exception))
        self.assertIn("sin --resume", str(ctx.exception))

    def test_resume_sin_run_previo(self):
        with self.assertRaises(ValueError):
            self.preparar("aaa", resume=True)

    def test_resume_con_run_ilegible(self):
        self.preparar("aaa")
        (self.trabajo / "run.json").write_text("{roto", encoding="utf-8")

        with self.assertRaises(ValueError):
            self.preparar("aaa", resume=True)


class TestOnlineIntegracion(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.inv = self.tmp / "inv.csv"
        escribir_inventario(self.inv, [
            ("0000758", "VODAFONE ESPAÑA, S.A.U.", 41.321464, 2.014425),
            ("CATB0020Ñ", "ORANGE ESPAGNE, S.A.U.", 41.392092, 2.124386),
            ("999", "ORANGE ESPAGNE, S.A.U.", 41.0, 2.0),
        ])
        self.params = {"timeout": 5, "reintentos": 0}

    def online(self, abridor, **kwargs):
        return m.ejecutar_online(
            self.inv, self.tmp / "out", self.tmp / "out" / "_treball",
            abrir=abridor, limitador=m.Limitador(0.0), parametros=self.params,
            dormir=lambda s: None, imprimir=lambda *_: None, **kwargs,
        )

    def test_online_y_offline_generan_lo_mismo(self):
        datos = {
            "0000758": MARKER_REAL,
            "CATB0020Ñ": marker("CATB0020Ñ", "ORANGE ESPAGNE, S.A.U.", 41.392092, 2.124386,
                                [imagen("5", "b.jpg")]),
        }
        abridor = Abridor(
            RespuestaFalsa(json_bytes(datos["0000758"])),
            RespuestaFalsa(json_bytes(datos["CATB0020Ñ"])),
            http_error(404),
        )
        resumen, consulta = self.online(abridor)

        raw = self.tmp / "raw.jsonl"
        raw.write_text("".join(
            json.dumps({"codigo_antena": c, "http_status": 200, "data": d}, ensure_ascii=False) + "\n"
            for c, d in datos.items()
        ), encoding="utf-8")
        m.ejecutar(self.inv, raw, self.tmp / "off")

        for nombre in ("CONTROL_GIS_ID.csv", "IMATGES_INFOANTENAS.csv"):
            online = (self.tmp / "out" / nombre).read_text(encoding="utf-8-sig").splitlines()
            offline = (self.tmp / "off" / nombre).read_text(encoding="utf-8-sig").splitlines()
            if nombre == "IMATGES_INFOANTENAS.csv":
                self.assertEqual(online, offline)
            else:
                # Solo difiere la explicación del no encontrado.
                self.assertEqual(online[:-1], offline[:-1])
                self.assertIn("CONSULTA_HTTP_NO_ENCONTRADO", online[-1])

        self.assertEqual(resumen["modo"], "online")
        self.assertEqual(resumen["cruce"]["gis_id_encontrados"], 2)
        self.assertEqual(resumen["consulta"]["estados"],
                         {m.EST_CON_MARKER: 2, m.EST_HTTP_NO_ENCONTRADO: 1})
        self.assertTrue((self.tmp / "out" / "resum_online.json").is_file())
        self.assertFalse((self.tmp / "out" / "resum_offline.json").exists())

    def test_seleccion_por_gis_id_y_maxim(self):
        abridor = Abridor(marker_ok("999"))
        resumen, _ = self.online(abridor, gis_ids=["999", "0000758"], maxim=1)

        self.assertEqual(resumen["seleccion"]["gis_id_seleccionados"], 1)
        self.assertEqual(len(abridor.peticiones), 1)
        self.assertTrue(abridor.peticiones[0].full_url.endswith("id=999"))

        with self.assertRaises(ValueError):
            self.online(Abridor(), gis_ids=["no_existe"])

    def test_resume_completa_solo_los_pendientes(self):
        with self.assertRaises(KeyboardInterrupt):
            self.online(Abridor(marker_ok("0000758"), KeyboardInterrupt()))

        abridor = Abridor(marker_ok("CATB0020Ñ"), marker_ok("999"))
        resumen, consulta = self.online(abridor, resume=True)

        self.assertEqual(len(abridor.peticiones), 2)
        self.assertEqual(consulta["contadores"][m.EST_YA_EXISTENTE], 1)
        self.assertTrue(resumen["consulta"]["reanudacion"])

    def test_nueva_ejecucion_vuelve_a_consultar_todo(self):
        self.online(Abridor(marker_ok("0000758"), marker_ok("CATB0020Ñ"), marker_ok("999")))

        abridor = Abridor(marker_ok("0000758"), marker_ok("CATB0020Ñ"), marker_ok("999"))
        self.online(abridor)

        self.assertEqual(len(abridor.peticiones), 3)

    def test_sha_distinto_impide_resume(self):
        self.online(Abridor(marker_ok("0000758"), marker_ok("CATB0020Ñ"), marker_ok("999")))
        escribir_inventario(self.inv, [("0000758", "VODAFONE ESPAÑA, S.A.U.", 41.3, 2.0)])

        with self.assertRaises(ValueError) as ctx:
            self.online(Abridor(), resume=True)

        self.assertIn("SHA-256", str(ctx.exception))

    def test_parada_por_errores_no_genera_salidas(self):
        resumen, consulta = self.online(
            Abridor(por_defecto=http_error(403)), max_errores_consecutivos=2
        )

        self.assertIsNone(resumen)
        self.assertTrue(consulta["detenido"])
        self.assertFalse((self.tmp / "out" / "CONTROL_GIS_ID.csv").exists())


class TestCli(unittest.TestCase):
    def test_opciones_online_requieren_modo_online(self):
        for opcion in (["--resume"], ["--maxim", "3"], ["--gis-id", "1"], ["--descargar-imagenes"]):
            with self.subTest(opcion), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    m.main(opcion)

    def test_opciones_de_imagen_requieren_descargar_imagenes(self):
        for opcion in (["--maxim-imatges", "3"], ["--parar-ante-bloqueo"], ["--imagenes-dir", "x"]):
            with self.subTest(opcion), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    m.main(["--modo", "online"] + opcion)


# ---------------------------------------------------------------------------
# Fase 3: descarga de imágenes y miniaturas (sin red; imágenes sintéticas)
# ---------------------------------------------------------------------------

def imagen_bytes(formato="JPEG", tamano=(400, 200), color=(200, 30, 30)):
    flujo = io.BytesIO()
    Image.new("RGB", tamano, color).save(flujo, format=formato)
    return flujo.getvalue()


def respuesta_imagen(formato="JPEG", tamano=(400, 200), **kwargs):
    tipo = {"JPEG": "image/jpeg", "PNG": "image/png"}[formato]
    return RespuestaFalsa(imagen_bytes(formato, tamano), content_type=tipo, **kwargs)


class RespuestaTruncada(RespuestaFalsa):
    """Entrega unos bytes y luego falla, como una conexión cortada."""

    def __init__(self):
        super().__init__(imagen_bytes(), content_type="image/jpeg")
        self._entregado = False

    def read(self, n=-1):
        if self._entregado:
            raise ConnectionResetError("conexión cortada")
        self._entregado = True
        return super().read(50)


def sin_partes(directorio):
    return list(Path(directorio).rglob("*.part"))


class ImagenesBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.imagenes = self.tmp / "imagenes"
        m.preparar_directorio_imagenes(self.imagenes, resume=True)
        self.pausas = []

    def procesar(self, abridor, fichero="foto.jpg", reintentos=2, thumb_max=300, **kwargs):
        return m.procesar_imagen(
            fichero, self.imagenes, abridor, m.Limitador(0.0),
            thumb_max=thumb_max, timeout=5, reintentos=reintentos,
            dormir=self.pausas.append, imprimir=lambda *_: None, **kwargs,
        )

    def existe_full(self, nombre="foto.jpg"):
        return (self.imagenes / "full" / nombre).exists()

    def existe_thumb(self, nombre="foto.jpg"):
        return (self.imagenes / "thumb" / nombre).exists()


class TestDescargaImagen(ImagenesBase):
    def test_jpg_valido(self):
        r = self.procesar(Abridor(respuesta_imagen("JPEG", (400, 200))), "foto.jpg")

        self.assertEqual(r["estado"], m.EST_IMG_OK)
        self.assertEqual((r["formato"], r["ancho"], r["alto"]), ("JPEG", 400, 200))
        self.assertEqual((r["miniatura_ancho"], r["miniatura_alto"]), (300, 150))
        self.assertTrue(self.existe_full() and self.existe_thumb())
        self.assertEqual(sin_partes(self.tmp), [])

    def test_jpeg_valido(self):
        r = self.procesar(Abridor(respuesta_imagen("JPEG")), "foto.jpeg")

        self.assertEqual(r["estado"], m.EST_IMG_OK)
        self.assertTrue(self.existe_full("foto.jpeg") and self.existe_thumb("foto.jpeg"))

    def test_png_valido_conserva_formato_en_la_miniatura(self):
        r = self.procesar(Abridor(respuesta_imagen("PNG", (600, 300))), "foto.png")

        self.assertEqual(r["estado"], m.EST_IMG_OK)
        self.assertEqual(r["formato"], "PNG")
        with Image.open(self.imagenes / "thumb" / "foto.png") as miniatura:
            self.assertEqual(miniatura.format, "PNG")
            self.assertEqual(miniatura.size, (300, 150))

    def test_el_formato_real_manda_sobre_la_extension(self):
        r = self.procesar(Abridor(respuesta_imagen("PNG")), "engañosa.jpg")

        self.assertEqual(r["estado"], m.EST_IMG_OK)
        self.assertEqual(r["formato"], "PNG")

    def test_http_200_con_html(self):
        html = RespuestaFalsa(b"<html>Error</html>", content_type="text/html; charset=UTF-8")
        r = self.procesar(Abridor(html))

        self.assertEqual(r["estado"], m.EST_IMG_CONTENT_TYPE_INVALIDO)
        self.assertEqual(r["intentos"], 1)
        self.assertFalse(self.existe_full() or self.existe_thumb())
        self.assertEqual(sin_partes(self.tmp), [])

    def test_content_type_invalido_aunque_el_cuerpo_sea_imagen(self):
        r = self.procesar(Abridor(RespuestaFalsa(imagen_bytes(), content_type="application/json")))

        self.assertEqual(r["estado"], m.EST_IMG_CONTENT_TYPE_INVALIDO)
        self.assertFalse(self.existe_full())

    def test_desafio_de_cloudflare_se_marca_como_bloqueo(self):
        pagina = RespuestaFalsa(b"<title>Just a moment...</title>", content_type="text/html")
        r = self.procesar(Abridor(pagina))

        self.assertEqual(r["estado"], m.EST_IMG_CONTENT_TYPE_INVALIDO)
        self.assertTrue(r["bloqueo"])

    def test_imagen_vacia(self):
        r = self.procesar(Abridor(RespuestaFalsa(b"", content_type="image/jpeg")))

        self.assertEqual(r["estado"], m.EST_IMG_VACIA)
        self.assertFalse(self.existe_full())
        self.assertEqual(sin_partes(self.tmp), [])

    def test_imagen_corrupta(self):
        r = self.procesar(Abridor(RespuestaFalsa(b"no soy una imagen", content_type="image/jpeg")))

        self.assertEqual(r["estado"], m.EST_IMG_CORRUPTA)
        self.assertFalse(self.existe_full() or self.existe_thumb())
        self.assertEqual(sin_partes(self.tmp), [])

    def test_imagen_truncada_se_detecta_al_decodificar(self):
        mitad = imagen_bytes("JPEG", (800, 600))[:400]
        r = self.procesar(Abridor(RespuestaFalsa(mitad, content_type="image/jpeg")))

        self.assertEqual(r["estado"], m.EST_IMG_CORRUPTA)
        self.assertFalse(self.existe_full())

    def test_429_con_retry_after(self):
        abridor = Abridor(http_error(429, Retry_After="7"), respuesta_imagen())
        r = self.procesar(abridor)

        self.assertEqual(r["estado"], m.EST_IMG_OK)
        self.assertEqual(r["intentos"], 2)
        self.assertEqual(self.pausas, [7.0])

    def test_429_con_retry_after_excesivo_no_se_espera(self):
        r = self.procesar(Abridor(http_error(429, Retry_After="3600"), respuesta_imagen()))

        self.assertEqual(r["estado"], m.EST_IMG_HTTP_ERROR)
        self.assertTrue(r["bloqueo"])
        self.assertEqual(r["intentos"], 1)
        self.assertEqual(self.pausas, [])

    def test_429_parar_ante_bloqueo_no_reintenta(self):
        abridor = Abridor(http_error(429, Retry_After="7"), respuesta_imagen())
        r = self.procesar(abridor, parar_ante_bloqueo=True)

        self.assertEqual(r["estado"], m.EST_IMG_HTTP_ERROR)
        self.assertTrue(r["bloqueo"])
        self.assertEqual(len(abridor.peticiones), 1)
        self.assertEqual(self.pausas, [])

    def test_403_es_bloqueo_y_no_se_reintenta(self):
        abridor = Abridor(http_error(403, b"<html>Forbidden</html>"), respuesta_imagen())
        r = self.procesar(abridor)

        self.assertEqual(r["estado"], m.EST_IMG_HTTP_ERROR)
        self.assertTrue(r["bloqueo"])
        self.assertEqual(len(abridor.peticiones), 1)

    def test_404_se_registra_sin_reintentar_ni_bloqueo(self):
        abridor = Abridor(http_error(404))
        r = self.procesar(abridor)

        self.assertEqual(r["estado"], m.EST_IMG_HTTP_ERROR)
        self.assertEqual(r["http_status"], 404)
        self.assertFalse(r["bloqueo"])
        self.assertEqual(len(abridor.peticiones), 1)

    def test_5xx_con_reintento(self):
        r = self.procesar(Abridor(http_error(503), respuesta_imagen()))

        self.assertEqual(r["estado"], m.EST_IMG_OK)
        self.assertEqual(r["intentos"], 2)
        self.assertEqual(self.pausas, [10])

    def test_5xx_agotado(self):
        r = self.procesar(Abridor(por_defecto=http_error(500)), reintentos=1)

        self.assertEqual(r["estado"], m.EST_IMG_HTTP_ERROR)
        self.assertEqual(r["intentos"], 2)
        self.assertFalse(self.existe_full())

    def test_error_de_red_con_reintento(self):
        r = self.procesar(Abridor(urllib.error.URLError("sin red"), respuesta_imagen()))

        self.assertEqual(r["estado"], m.EST_IMG_OK)
        self.assertEqual(r["intentos"], 2)

    def test_error_de_red_agotado(self):
        r = self.procesar(Abridor(por_defecto=urllib.error.URLError("sin red")), reintentos=1)

        self.assertEqual(r["estado"], m.EST_IMG_ERROR_RED)
        self.assertEqual(r["intentos"], 2)

    def test_descarga_incompleta_segun_content_length(self):
        cuerpo = imagen_bytes()
        corta = RespuestaFalsa(cuerpo[:100], content_type="image/jpeg", content_length=len(cuerpo))
        r = self.procesar(Abridor(corta, respuesta_imagen()))

        self.assertEqual(r["estado"], m.EST_IMG_OK)
        self.assertEqual(r["intentos"], 2)

    def test_part_se_elimina_si_la_conexion_se_corta(self):
        r = self.procesar(Abridor(RespuestaTruncada()), reintentos=0)

        self.assertEqual(r["estado"], m.EST_IMG_ERROR_RED)
        self.assertEqual(sin_partes(self.tmp), [])
        self.assertFalse(self.existe_full())

    def test_definitivo_solo_tras_descarga_valida(self):
        class Comprobador(Abridor):
            def __call__(self, peticion, timeout):
                respuesta = super().__call__(peticion, timeout)
                test.assertFalse(test.existe_full())
                return respuesta

        test = self
        self.procesar(Comprobador(respuesta_imagen()))
        self.assertTrue(self.existe_full())

    def test_registra_cabeceras_utiles_sin_cookies(self):
        respuesta = respuesta_imagen(content_length=None)
        respuesta.headers["ETag"] = '"abc"'
        respuesta.headers["Last-Modified"] = "Wed, 01 Jan 2025 00:00:00 GMT"
        respuesta.headers["Set-Cookie"] = "ci_session=secreto"
        r = self.procesar(Abridor(respuesta))

        self.assertEqual(r["cabeceras"]["etag"], '"abc"')
        self.assertIn("last-modified", r["cabeceras"])
        self.assertNotIn("set-cookie", r["cabeceras"])
        self.assertEqual(r["content_type"], "image/jpeg")

    def test_la_peticion_pide_la_url_de_imagen_completa(self):
        abridor = Abridor(respuesta_imagen())
        self.procesar(abridor, "1729885461_x.jpg")

        self.assertEqual(
            abridor.peticiones[0].full_url,
            "https://antenasmoviles.es/archive/antenas/1729885461_x.jpg",
        )


class TestMiniaturas(ImagenesBase):
    def miniatura(self, formato, tamano, max_lado=300):
        origen = self.tmp / "origen.img"
        origen.write_bytes(imagen_bytes(formato, tamano))
        destino = self.tmp / "mini.img"
        return m.generar_miniatura(origen, destino, max_lado), destino

    def test_mantiene_proporcion_horizontal(self):
        (ancho, alto), _ = self.miniatura("JPEG", (1000, 400))
        self.assertEqual((ancho, alto), (300, 120))

    def test_mantiene_proporcion_vertical(self):
        (ancho, alto), _ = self.miniatura("PNG", (200, 800))
        self.assertEqual((ancho, alto), (75, 300))

    def test_no_amplia_imagenes_pequenas(self):
        (ancho, alto), _ = self.miniatura("JPEG", (100, 50))
        self.assertEqual((ancho, alto), (100, 50))

    def test_maximo_configurable(self):
        (ancho, alto), destino = self.miniatura("JPEG", (400, 200), max_lado=100)
        self.assertEqual((ancho, alto), (100, 50))
        with Image.open(destino) as imagen_final:
            self.assertEqual(imagen_final.size, (100, 50))

    def test_no_modifica_el_original(self):
        origen = self.tmp / "origen.jpg"
        contenido = imagen_bytes("JPEG", (400, 200))
        origen.write_bytes(contenido)
        m.generar_miniatura(origen, self.tmp / "mini.jpg", 100)

        self.assertEqual(origen.read_bytes(), contenido)

    def test_nombre_local_y_url(self):
        self.assertEqual(m.nombre_local_seguro("1729885461_6fbc.jpg"), "1729885461_6fbc.jpg")

        for raro in ("../x.jpg", "a/b.jpg", "..", "con espacio.jpg"):
            with self.subTest(raro):
                nombre = m.nombre_local_seguro(raro)
                self.assertNotIn("/", nombre)
                self.assertNotIn("\\", nombre)
                self.assertNotIn(nombre, ("..", "."))

        self.assertEqual(
            m.construir_url_imagen("1729885461_6fbc.jpg"),
            m.IMAGE_PREFIX + "1729885461_6fbc.jpg",
        )

    def test_deteccion_de_bloqueo(self):
        self.assertTrue(m.detectar_bloqueo(429, None))
        self.assertTrue(m.detectar_bloqueo(403, None))
        self.assertTrue(m.detectar_bloqueo(503, cabeceras(CF_Mitigated="challenge")))
        self.assertTrue(m.detectar_bloqueo(200, None, b"<html>Just a moment...</html>"))
        self.assertFalse(m.detectar_bloqueo(404, None))
        self.assertFalse(m.detectar_bloqueo(500, None))


class TestReanudacionImagenes(ImagenesBase):
    def test_reutiliza_original_y_miniatura_validos(self):
        self.procesar(Abridor(respuesta_imagen()))

        abridor = Abridor()
        r = self.procesar(abridor)

        self.assertEqual(abridor.peticiones, [])
        self.assertEqual((r["estado"], r["accion"]), (m.EST_IMG_OK, m.ACCION_REUTILIZADA))
        self.assertEqual(r["intentos"], 0)

    def test_original_valido_y_miniatura_ausente_solo_genera_miniatura(self):
        self.procesar(Abridor(respuesta_imagen()))
        original = (self.imagenes / "full" / "foto.jpg").read_bytes()
        (self.imagenes / "thumb" / "foto.jpg").unlink()

        abridor = Abridor()
        r = self.procesar(abridor)

        self.assertEqual(abridor.peticiones, [])
        self.assertEqual((r["estado"], r["accion"]), (m.EST_IMG_OK, m.ACCION_SOLO_MINIATURA))
        self.assertTrue(self.existe_thumb())
        self.assertEqual((self.imagenes / "full" / "foto.jpg").read_bytes(), original)

    def test_miniatura_corrupta_se_regenera(self):
        self.procesar(Abridor(respuesta_imagen()))
        (self.imagenes / "thumb" / "foto.jpg").write_bytes(b"basura")

        abridor = Abridor()
        r = self.procesar(abridor)

        self.assertEqual(abridor.peticiones, [])
        self.assertEqual(r["accion"], m.ACCION_SOLO_MINIATURA)
        self.assertIsNotNone(m.miniatura_valida(self.imagenes / "thumb" / "foto.jpg", 300))

    def test_original_corrupto_se_vuelve_a_descargar(self):
        self.procesar(Abridor(respuesta_imagen()))
        (self.imagenes / "full" / "foto.jpg").write_bytes(b"corrupto")

        abridor = Abridor(respuesta_imagen("JPEG", (800, 400)))
        r = self.procesar(abridor)

        self.assertEqual(len(abridor.peticiones), 1)
        self.assertEqual((r["estado"], r["accion"]), (m.EST_IMG_OK, m.ACCION_DESCARGA))
        self.assertEqual(r["ancho"], 800)

    def test_original_corrupto_y_descarga_fallida_no_deja_ficheros(self):
        self.procesar(Abridor(respuesta_imagen()))
        (self.imagenes / "full" / "foto.jpg").write_bytes(b"corrupto")

        r = self.procesar(Abridor(http_error(404)))

        self.assertEqual(r["estado"], m.EST_IMG_HTTP_ERROR)
        self.assertFalse(self.existe_full() or self.existe_thumb())


class TestProcesarImagenes(ImagenesBase):
    def filas(self, *relaciones):
        """relaciones: (gis_id, id_imagen, fichero)."""
        return [
            {"GIS_ID": g, "ID_IMAGEN_ORIGEN": i, "NOMBRE_FICHERO_ORIGEN": f}
            for g, i, f in relaciones
        ]

    def procesar_todas(self, filas, abridor, **kwargs):
        return m.procesar_imagenes(
            filas, self.imagenes, self.tmp / "log_imatges.csv", abridor, m.Limitador(0.0),
            thumb_max=300, timeout=5, reintentos=0,
            dormir=lambda s: None, imprimir=lambda *_: None, **kwargs,
        )

    def leer_log(self):
        with (self.tmp / "log_imatges.csv").open(encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f, delimiter=";"))

    def test_imagen_compartida_se_descarga_una_sola_vez(self):
        filas = self.filas(
            ("0000758", "1", "compartida.jpg"),
            ("000758", "1", "compartida.jpg"),
            ("758", "1", "compartida.jpg"),
            ("4300034", "2", "otra.jpg"),
        )
        abridor = Abridor(respuesta_imagen(), respuesta_imagen())
        resultado = self.procesar_todas(filas, abridor)

        self.assertEqual(len(abridor.peticiones), 2)
        self.assertEqual(sorted(resultado["por_fichero"]), ["compartida.jpg", "otra.jpg"])
        self.assertEqual(resultado["resumen"]["relaciones_gis_id_imagen"], 4)
        self.assertEqual(resultado["resumen"]["ficheros_unicos"], 2)
        self.assertEqual(resultado["resumen"]["descargas_evitadas_por_deduplicacion"], 2)

    def test_log_una_fila_por_fichero_unico(self):
        filas = self.filas(("a", "1", "x.jpg"), ("b", "1", "x.jpg"), ("c", "2", "y.jpg"))
        self.procesar_todas(filas, Abridor(respuesta_imagen(), http_error(404)))
        log = self.leer_log()

        self.assertEqual([r["NOMBRE_FICHERO"] for r in log], ["x.jpg", "y.jpg"])
        self.assertEqual([r["ESTADO"] for r in log], [m.EST_IMG_OK, m.EST_IMG_HTTP_ERROR])
        self.assertEqual(log[0]["ID_IMAGEN_ORIGEN"], "1")
        self.assertEqual(log[0]["HTTP_STATUS"], "200")
        self.assertEqual(log[0]["CONTENT_TYPE"], "image/jpeg")
        self.assertEqual(log[1]["HTTP_STATUS"], "404")
        self.assertEqual(log[0]["URL_ORIGEN"], m.IMAGE_PREFIX + "x.jpg")
        self.assertNotIn("cookie", log[0]["CABECERAS"].lower())

    def test_un_error_no_aborta_el_resto(self):
        filas = self.filas(("a", "1", "x.jpg"), ("b", "2", "y.jpg"), ("c", "3", "z.jpg"))
        resultado = self.procesar_todas(
            filas, Abridor(http_error(404), RespuestaFalsa(b"", content_type="image/jpeg"), respuesta_imagen())
        )

        estados = {f: v["ESTADO_DESCARGA"] for f, v in resultado["por_fichero"].items()}
        self.assertEqual(
            estados,
            {"x.jpg": m.EST_IMG_HTTP_ERROR, "y.jpg": m.EST_IMG_VACIA, "z.jpg": m.EST_IMG_OK},
        )
        self.assertEqual(resultado["por_fichero"]["x.jpg"]["RUTA_IMAGEN"], "")
        self.assertEqual(resultado["por_fichero"]["z.jpg"]["RUTA_IMAGEN"], "full/z.jpg")
        self.assertEqual(resultado["por_fichero"]["z.jpg"]["RUTA_MINIATURA"], "thumb/z.jpg")

    def test_maxim_imatges_deja_el_resto_pendiente(self):
        filas = self.filas(("a", "1", "x.jpg"), ("b", "2", "y.jpg"), ("c", "3", "z.jpg"))
        abridor = Abridor(respuesta_imagen(), respuesta_imagen())
        resultado = self.procesar_todas(filas, abridor, maxim_imagenes=2)

        self.assertEqual(len(abridor.peticiones), 2)
        self.assertEqual(resultado["por_fichero"]["z.jpg"]["ESTADO_DESCARGA"], m.EST_IMG_PENDIENTE)
        self.assertEqual(self.leer_log()[-1]["ESTADO"], m.EST_IMG_PENDIENTE)

    def test_bloqueo_detiene_y_conserva_lo_descargado(self):
        filas = self.filas(("a", "1", "x.jpg"), ("b", "2", "y.jpg"), ("c", "3", "z.jpg"))
        abridor = Abridor(respuesta_imagen(), http_error(429), respuesta_imagen())
        resultado = self.procesar_todas(filas, abridor, parar_ante_bloqueo=True)

        self.assertEqual(len(abridor.peticiones), 2)
        self.assertTrue(resultado["resumen"]["detenido_por_bloqueo"])
        estados = [v["ESTADO_DESCARGA"] for v in resultado["por_fichero"].values()]
        self.assertEqual(estados, [m.EST_IMG_OK, m.EST_IMG_HTTP_ERROR, m.EST_IMG_PENDIENTE])
        self.assertTrue(self.existe_full("x.jpg"))

    def test_errores_consecutivos_detienen(self):
        filas = self.filas(*[("g", str(i), f"{i}.jpg") for i in range(4)])
        abridor = Abridor(por_defecto=http_error(404))
        resultado = self.procesar_todas(filas, abridor, max_errores_consecutivos=2)

        self.assertEqual(len(abridor.peticiones), 2)
        self.assertTrue(resultado["resumen"]["detenido_por_errores"])


class TestOnlineConImagenes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.out = self.tmp / "out"
        self.inv = self.tmp / "inv.csv"
        # Operador y coordenadas del inventario distintos de los del marker.
        escribir_inventario(self.inv, [
            ("0000758", "VODAFONE ESPAÑA, S.A.U.", 42.5, 3.0),
            ("000758", "VODAFONE ESPAÑA, S.A.U.", 42.5, 3.0),
            ("4300034", "ORANGE ESPAGNE, S.A.U.", 40.5, 0.4),
        ])
        self.params = {"timeout": 5, "reintentos": 0}
        compartida = dict(imagen("173568", "compartida.jpg"), old="1")
        self.respuestas = {
            "0000758": [compartida, imagen("200", "propia.png")],
            "000758": [compartida],
            "4300034": [],
        }

    def marcadores(self):
        return Abridor(*[
            RespuestaFalsa(json_bytes(marker(c, "OP", 41.0, 2.0, imgs)))
            for c, imgs in self.respuestas.items()
        ])

    def descarga(self, abridor, **kwargs):
        return {
            "abrir": abridor, "limitador": m.Limitador(0.0),
            "imagenes_dir": self.out / "imagenes", "thumb_max": 300,
            "reintentos": 0, **kwargs,
        }

    def online(self, abridor_markers, abridor_imagenes, resume=False, **kwargs):
        return m.ejecutar_online(
            self.inv, self.out, self.out / "_treball",
            abrir=abridor_markers, limitador=m.Limitador(0.0), parametros=self.params,
            resume=resume, descarga=self.descarga(abridor_imagenes, **kwargs),
            dormir=lambda s: None, imprimir=lambda *_: None,
        )

    def leer_csv(self, nombre="IMATGES_INFOANTENAS.csv"):
        with (self.out / nombre).open(encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f, delimiter=";"))

    def imagenes_ok(self):
        return Abridor(respuesta_imagen("JPEG"), respuesta_imagen("PNG", (500, 500)))

    def test_relaciones_apuntan_al_mismo_fichero_y_se_descarga_una_vez(self):
        abridor_imagenes = self.imagenes_ok()
        resumen, _ = self.online(self.marcadores(), abridor_imagenes)
        filas = self.leer_csv()

        self.assertEqual(len(filas), 3)
        self.assertEqual(len(abridor_imagenes.peticiones), 2)
        compartidas = [f for f in filas if f["NOMBRE_FICHERO_ORIGEN"] == "compartida.jpg"]
        self.assertEqual({f["GIS_ID"] for f in compartidas}, {"0000758", "000758"})
        self.assertEqual({f["RUTA_IMAGEN"] for f in compartidas}, {"full/compartida.jpg"})
        self.assertEqual({f["RUTA_MINIATURA"] for f in compartidas}, {"thumb/compartida.jpg"})
        self.assertEqual({f["ESTADO_DESCARGA"] for f in filas}, {m.EST_IMG_OK})
        self.assertEqual(resumen["descarga"]["descargas_evitadas_por_deduplicacion"], 1)
        self.assertEqual(resumen["imagenes"]["unicas_por_fichero"], 2)

    def test_csv_con_descarga_incluye_codi_operador(self):
        self.online(self.marcadores(), self.imagenes_ok())
        with (self.out / "IMATGES_INFOANTENAS.csv").open(encoding="utf-8-sig", newline="") as f:
            self.assertEqual(
                next(csv.reader(f, delimiter=";")), m.CAMPOS_IMAGENES + m.CAMPOS_DESCARGA
            )
        self.assertEqual({f["CODI_OPERADOR"] for f in self.leer_csv()}, {"1"})

    def test_rutas_relativas_y_ficheros_existentes(self):
        self.online(self.marcadores(), self.imagenes_ok())

        for fila in self.leer_csv():
            for campo in ("RUTA_IMAGEN", "RUTA_MINIATURA"):
                self.assertFalse(Path(fila[campo]).is_absolute())
                self.assertNotIn(":", fila[campo])
                self.assertTrue((self.out / "imagenes" / fila[campo]).is_file())

    def test_old_y_desmantelada_no_excluyen_y_se_conservan(self):
        self.online(self.marcadores(), self.imagenes_ok())
        compartida = [f for f in self.leer_csv() if f["NOMBRE_FICHERO_ORIGEN"] == "compartida.jpg"][0]

        self.assertEqual(compartida["OLD"], "1")
        self.assertEqual(compartida["ESTADO_DESCARGA"], m.EST_IMG_OK)
        propia = [f for f in self.leer_csv() if f["NOMBRE_FICHERO_ORIGEN"] == "propia.png"][0]
        self.assertEqual(propia["DESMANTELADA"], "1")
        self.assertEqual(propia["ESTADO_DESCARGA"], m.EST_IMG_OK)

    def test_distancia_alta_y_operador_distinto_no_impiden_la_descarga(self):
        resumen, _ = self.online(self.marcadores(), self.imagenes_ok())
        control = {f["GIS_ID"]: f for f in self.leer_csv("CONTROL_GIS_ID.csv")}

        self.assertEqual(control["0000758"]["OPERADOR_COINCIDE"], "NO")
        self.assertGreater(float(control["0000758"]["DISTANCIA_M"]), 100000)
        self.assertEqual(resumen["descarga"]["estados"], {m.EST_IMG_OK: 2})

    def test_log_imatges_en_el_directorio_de_trabajo(self):
        self.online(self.marcadores(), self.imagenes_ok())
        with (self.out / "_treball" / "log_imatges.csv").open(encoding="utf-8-sig", newline="") as f:
            log = list(csv.DictReader(f, delimiter=";"))

        self.assertEqual([r["NOMBRE_FICHERO"] for r in log], ["compartida.jpg", "propia.png"])
        for campo in ("ID_IMAGEN_ORIGEN", "URL_ORIGEN", "ESTADO", "HTTP_STATUS",
                      "INTENTOS", "CONTENT_TYPE", "BYTES", "ERROR"):
            self.assertIn(campo, log[0])

    def test_sin_descargar_el_csv_no_tiene_columnas_de_descarga(self):
        m.ejecutar_online(
            self.inv, self.out, self.out / "_treball",
            abrir=self.marcadores(), limitador=m.Limitador(0.0), parametros=self.params,
            dormir=lambda s: None, imprimir=lambda *_: None,
        )
        with (self.out / "IMATGES_INFOANTENAS.csv").open(encoding="utf-8-sig", newline="") as f:
            self.assertEqual(next(csv.reader(f, delimiter=";")), m.CAMPOS_IMAGENES)
        self.assertFalse((self.out / "imagenes").exists())

    def test_resume_tras_interrupcion_solo_descarga_lo_pendiente(self):
        with self.assertRaises(KeyboardInterrupt):
            self.online(self.marcadores(), Abridor(respuesta_imagen(), KeyboardInterrupt()))

        abridor_imagenes = Abridor(respuesta_imagen("PNG", (500, 500)))
        resumen, _ = self.online(Abridor(), abridor_imagenes, resume=True)

        self.assertEqual(len(abridor_imagenes.peticiones), 1)
        self.assertEqual(resumen["descarga"]["acciones"],
                         {m.ACCION_DESCARGA: 1, m.ACCION_REUTILIZADA: 1})
        self.assertEqual({f["ESTADO_DESCARGA"] for f in self.leer_csv()}, {m.EST_IMG_OK})

    def test_resume_regenera_solo_miniatura_ausente(self):
        self.online(self.marcadores(), self.imagenes_ok())
        (self.out / "imagenes" / "thumb" / "propia.png").unlink()

        abridor_imagenes = Abridor()
        resumen, _ = self.online(Abridor(), abridor_imagenes, resume=True)

        self.assertEqual(abridor_imagenes.peticiones, [])
        self.assertEqual(resumen["descarga"]["acciones"],
                         {m.ACCION_REUTILIZADA: 1, m.ACCION_SOLO_MINIATURA: 1})

    def test_nueva_ejecucion_vuelve_a_descargar_todo(self):
        self.online(self.marcadores(), self.imagenes_ok())

        abridor_imagenes = self.imagenes_ok()
        self.online(self.marcadores(), abridor_imagenes)

        self.assertEqual(len(abridor_imagenes.peticiones), 2)

    def test_bloqueo_conserva_resultados_y_genera_csv(self):
        abridor_imagenes = Abridor(respuesta_imagen(), http_error(429))
        resumen, _ = self.online(self.marcadores(), abridor_imagenes, parar_ante_bloqueo=True)

        self.assertTrue(resumen["descarga"]["detenido_por_bloqueo"])
        estados = {f["NOMBRE_FICHERO_ORIGEN"]: f["ESTADO_DESCARGA"] for f in self.leer_csv()}
        self.assertEqual(estados["compartida.jpg"], m.EST_IMG_OK)
        self.assertEqual(estados["propia.png"], m.EST_IMG_HTTP_ERROR)


# ---------------------------------------------------------------------------
# Fase 4: versionado nueva / actual / anterior (sin red)
# ---------------------------------------------------------------------------

def arbol(ruta):
    """Contenido completo de un directorio: ruta relativa -> bytes."""
    ruta = Path(ruta)
    return {
        str(p.relative_to(ruta)): p.read_bytes()
        for p in sorted(ruta.rglob("*")) if p.is_file()
    }


def crear_version(ruta, contenido="datos", completa=True):
    ruta = Path(ruta)
    ruta.mkdir(parents=True)
    (ruta / "datos.txt").write_text(contenido, encoding="utf-8")
    if completa:
        (ruta / m.MARCADOR_COMPLETA).write_text("{}", encoding="utf-8")
    return ruta


class TestPromocion(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.raiz = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.nueva, self.actual, self.anterior = m.rutas_versiones(self.raiz)

    def texto(self, ruta):
        return (ruta / "datos.txt").read_text(encoding="utf-8")

    def test_promocion_sin_version_previa(self):
        crear_version(self.nueva, "N")

        m.promover_nueva(self.raiz)

        self.assertFalse(self.nueva.exists())
        self.assertFalse(self.anterior.exists())
        self.assertEqual(self.texto(self.actual), "N")

    def test_promocion_con_version_previa(self):
        crear_version(self.actual, "A")
        crear_version(self.anterior, "B_antiguo")
        crear_version(self.nueva, "N")

        m.promover_nueva(self.raiz)

        self.assertEqual(self.texto(self.actual), "N")
        self.assertEqual(self.texto(self.anterior), "A")
        self.assertFalse(self.nueva.exists())
        self.assertNotIn("B_antiguo", [p.read_text(encoding="utf-8") for p in self.raiz.rglob("datos.txt")])

    def test_promocion_con_actual_y_sin_anterior(self):
        crear_version(self.actual, "A")
        crear_version(self.nueva, "N")

        m.promover_nueva(self.raiz)

        self.assertEqual(self.texto(self.anterior), "A")
        self.assertEqual(self.texto(self.actual), "N")

    def test_nueva_sin_completa_no_se_promociona(self):
        crear_version(self.actual, "A")
        crear_version(self.nueva, "N", completa=False)
        antes = arbol(self.raiz)

        with self.assertRaises(ValueError):
            m.promover_nueva(self.raiz)

        self.assertEqual(arbol(self.raiz), antes)

    def test_actual_sin_completa_no_se_sustituye(self):
        crear_version(self.actual, "A", completa=False)
        crear_version(self.nueva, "N")
        antes = arbol(self.raiz)

        with self.assertRaises(m.EstadoVersionesAmbiguo):
            m.promover_nueva(self.raiz)

        self.assertEqual(arbol(self.raiz), antes)


class TestRecuperacion(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.raiz = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.nueva, self.actual, self.anterior = m.rutas_versiones(self.raiz)

    def texto(self, ruta):
        return (ruta / "datos.txt").read_text(encoding="utf-8")

    def test_caso_a_completa_nueva_si_falta_actual(self):
        crear_version(self.anterior, "A")
        crear_version(self.nueva, "N")

        accion = m.recuperar_versiones(self.raiz)

        self.assertIn("nueva -> actual", accion)
        self.assertEqual(self.texto(self.actual), "N")
        self.assertEqual(self.texto(self.anterior), "A")
        self.assertFalse(self.nueva.exists())

    def test_caso_b_restaura_anterior_si_nueva_incompleta(self):
        crear_version(self.anterior, "A")
        crear_version(self.nueva, "parcial", completa=False)

        accion = m.recuperar_versiones(self.raiz)

        self.assertIn("anterior -> actual", accion)
        self.assertEqual(self.texto(self.actual), "A")
        self.assertFalse(self.anterior.exists())
        self.assertEqual(self.texto(self.nueva), "parcial")

    def test_caso_b_tambien_si_no_hay_nueva(self):
        crear_version(self.anterior, "A")

        m.recuperar_versiones(self.raiz)

        self.assertEqual(self.texto(self.actual), "A")

    def test_caso_c_con_actual_no_se_toca_nada(self):
        crear_version(self.actual, "A")
        crear_version(self.anterior, "B")
        crear_version(self.nueva, "N")
        antes = arbol(self.raiz)

        self.assertIsNone(m.recuperar_versiones(self.raiz))
        self.assertEqual(arbol(self.raiz), antes)

    def test_sin_nada_que_recuperar(self):
        self.assertIsNone(m.recuperar_versiones(self.raiz))
        crear_version(self.nueva, "parcial", completa=False)
        self.assertIsNone(m.recuperar_versiones(self.raiz))

    def test_ambiguo_actual_sin_completa(self):
        crear_version(self.actual, "A", completa=False)
        crear_version(self.nueva, "N")
        antes = arbol(self.raiz)

        with self.assertRaises(m.EstadoVersionesAmbiguo):
            m.recuperar_versiones(self.raiz)

        self.assertEqual(arbol(self.raiz), antes)

    def test_ambiguo_anterior_sin_completa_y_sin_actual(self):
        crear_version(self.anterior, "A", completa=False)
        crear_version(self.nueva, "parcial", completa=False)
        antes = arbol(self.raiz)

        with self.assertRaises(m.EstadoVersionesAmbiguo):
            m.recuperar_versiones(self.raiz)

        self.assertEqual(arbol(self.raiz), antes)

    def test_ambiguo_si_una_ruta_es_un_fichero(self):
        self.nueva.write_text("no soy un directorio", encoding="utf-8")
        crear_version(self.anterior, "A")

        with self.assertRaises(m.EstadoVersionesAmbiguo):
            m.recuperar_versiones(self.raiz)

        self.assertEqual(self.nueva.read_text(encoding="utf-8"), "no soy un directorio")
        self.assertTrue(self.anterior.exists())
        self.assertFalse(self.actual.exists())

    def test_corte_entre_actual_a_anterior_y_nueva_a_actual(self):
        crear_version(self.actual, "A")
        crear_version(self.nueva, "N")
        llamadas = []

        def renombrar_con_corte(origen, destino):
            llamadas.append((origen.name, destino.name))
            if len(llamadas) == 2:
                raise OSError("corte simulado")
            m._renombrar(origen, destino)

        with self.assertRaises(OSError):
            m.promover_nueva(self.raiz, renombrar_con_corte)

        self.assertEqual(llamadas, [("actual", "anterior"), ("nueva", "actual")])
        self.assertFalse(self.actual.exists())
        self.assertEqual(self.texto(self.anterior), "A")
        self.assertTrue(m.version_completa(self.nueva))

        m.recuperar_versiones(self.raiz)

        self.assertEqual(self.texto(self.actual), "N")
        self.assertEqual(self.texto(self.anterior), "A")
        self.assertFalse(self.nueva.exists())

    def test_corte_con_nueva_incompleta_restaura_anterior(self):
        crear_version(self.anterior, "A")
        crear_version(self.nueva, "reanudable", completa=False)

        m.recuperar_versiones(self.raiz)

        self.assertEqual(self.texto(self.actual), "A")
        self.assertEqual(self.texto(self.nueva), "reanudable")


class VersionesBase(unittest.TestCase):
    """Escenario: 3 Gis_ID; uno con marker [] y dos con imágenes (una compartida)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.raiz = self.tmp / "versiones"
        self.nueva, self.actual, self.anterior = m.rutas_versiones(self.raiz)
        self.inv = self.tmp / "inv.csv"
        escribir_inventario(self.inv, [
            ("0000758", "OP", 41.0, 2.0),
            ("4300034", "OP", 41.0, 2.0),
            ("999", "OP", 41.0, 2.0),
        ])
        self.params = {"timeout": 5, "reintentos": 0, "descargar_imagenes": True,
                       "miniatura_max": 300}
        compartida = dict(imagen("173568", "compartida.jpg"), old="1")
        self.respuestas = {
            "0000758": marker("0000758", "OP", 41.0, 2.0,
                              [compartida, imagen("200", "propia.png")]),
            "4300034": marker("4300034", "OP", 41.0, 2.0, [compartida]),
            "999": [],
        }

    def marcadores(self):
        return Abridor(*[
            RespuestaFalsa(json_bytes(self.respuestas[g])) for g in ("0000758", "4300034", "999")
        ])

    def imagenes_ok(self):
        return Abridor(respuesta_imagen("JPEG"), respuesta_imagen("PNG", (500, 500)))

    def ejecutar(self, markers=None, imagenes=None, resume=False, maxim_imagenes=None,
                 parar_ante_bloqueo=False, **kwargs):
        descarga = {
            "abrir": imagenes if imagenes is not None else self.imagenes_ok(),
            "limitador": m.Limitador(0.0), "thumb_max": 300, "reintentos": 0,
            "maxim_imagenes": maxim_imagenes, "parar_ante_bloqueo": parar_ante_bloqueo,
        }
        parametros = dict(
            self.params, gis_ids=kwargs.get("gis_ids"), maxim=kwargs.get("maxim"),
            maxim_imatges=maxim_imagenes,
        )
        return m.ejecutar_version(
            self.inv, self.raiz,
            abrir=markers if markers is not None else self.marcadores(),
            limitador=m.Limitador(0.0), parametros=parametros, descarga=descarga,
            resume=resume, dormir=lambda s: None, imprimir=lambda *_: None, **kwargs,
        )

    def leer_csv(self, version, nombre="IMATGES_INFOANTENAS.csv"):
        with (version / nombre).open(encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f, delimiter=";"))

    def leer_resumen(self, version):
        return json.loads((version / "resum.json").read_text(encoding="utf-8"))


class TestVersionadoPromocion(VersionesBase):
    def test_ejecucion_correcta_crea_completa_y_promociona(self):
        resultado = self.ejecutar()

        self.assertTrue(resultado["promocionable"])
        self.assertTrue(resultado["promovida"])
        self.assertEqual(resultado["motivos"], [])
        self.assertTrue((self.actual / m.MARCADOR_COMPLETA).is_file())
        self.assertFalse(self.nueva.exists())
        self.assertFalse(self.anterior.exists())
        for nombre in m.ARCHIVOS_VERSION:
            self.assertTrue((self.actual / nombre).is_file(), nombre)
        self.assertTrue((self.actual / "full" / "compartida.jpg").is_file())
        self.assertTrue((self.actual / "thumb" / "propia.png").is_file())
        self.assertEqual(list(self.actual.rglob("*.part")), [])
        self.assertEqual(m.codigo_salida_version(resultado), 0)

    def test_completa_es_lo_ultimo_que_se_crea(self):
        original = m.crear_completa
        visto = {}

        def espia(version_dir, metadatos):
            visto["antes"] = sorted(p.name for p in version_dir.iterdir())
            original(version_dir, metadatos)

        with mock.patch.object(m, "crear_completa", side_effect=espia):
            self.ejecutar()

        self.assertNotIn(m.MARCADOR_COMPLETA, visto["antes"])
        for nombre in m.ARCHIVOS_VERSION:
            self.assertIn(nombre, visto["antes"])

        completa = (self.actual / m.MARCADOR_COMPLETA).stat().st_mtime_ns
        otros = [p.stat().st_mtime_ns for p in self.actual.rglob("*")
                 if p.is_file() and p.name != m.MARCADOR_COMPLETA]
        self.assertGreaterEqual(completa, max(otros))

    def test_error_de_imagen_no_crea_completa(self):
        imagenes = Abridor(respuesta_imagen("JPEG"), http_error(404))
        resultado = self.ejecutar(imagenes=imagenes)

        self.assertFalse(resultado["promocionable"])
        self.assertFalse(resultado["promovida"])
        self.assertIn("ERRORES_IMAGEN:1", resultado["motivos"])
        self.assertFalse((self.nueva / m.MARCADOR_COMPLETA).exists())
        self.assertTrue(self.nueva.is_dir())
        self.assertFalse(self.actual.exists())
        self.assertEqual(m.codigo_salida_version(resultado), 2)
        self.assertFalse(self.leer_resumen(self.nueva)["version"]["promocionable"])

    def test_error_de_markers_no_promociona(self):
        markers = Abridor(RespuestaFalsa(json_bytes(self.respuestas["0000758"])),
                          http_error(403), RespuestaFalsa(b"[]"))
        resultado = self.ejecutar(markers=markers)

        self.assertFalse(resultado["promocionable"])
        self.assertIn("ERRORES_MARKERS:1", resultado["motivos"])
        self.assertFalse((self.nueva / m.MARCADOR_COMPLETA).exists())

    def test_consulta_detenida_deja_nueva_sin_completa(self):
        resultado = self.ejecutar(markers=Abridor(por_defecto=http_error(403)),
                                  max_errores_consecutivos=1)

        self.assertFalse(resultado["promocionable"])
        self.assertIn("CONSULTA_DETENIDA_POR_ERRORES", resultado["motivos"])
        self.assertEqual(m.codigo_salida_version(resultado), 3)
        self.assertFalse((self.nueva / m.MARCADOR_COMPLETA).exists())
        self.assertFalse(self.leer_resumen(self.nueva)["version"]["promocionable"])

    def test_bloqueo_no_se_promociona(self):
        imagenes = Abridor(respuesta_imagen("JPEG"), http_error(429))
        resultado = self.ejecutar(imagenes=imagenes, parar_ante_bloqueo=True)

        self.assertIn("BLOQUEO_DETECTADO", resultado["motivos"])
        self.assertEqual(m.codigo_salida_version(resultado), 4)
        self.assertFalse(self.actual.exists())

    def test_pendientes_no_se_promociona(self):
        resultado = self.ejecutar(maxim_imagenes=1, permitir_parcial=True)

        self.assertFalse(resultado["promocionable"])
        self.assertIn("IMAGENES_PENDIENTES:1", resultado["motivos"])
        self.assertFalse(self.actual.exists())
        self.assertEqual(m.codigo_salida_version(resultado), 5)

    def test_marker_vacio_no_impide_promocion(self):
        resultado = self.ejecutar()
        control = {f["GIS_ID"]: f for f in self.leer_csv(self.actual, "CONTROL_GIS_ID.csv")}

        self.assertTrue(resultado["promovida"])
        self.assertEqual(control["999"]["ENCONTRADO_ANTENASMOVILES"], "NO")
        self.assertEqual(control["999"]["NUM_IMAGENES"], "0")
        self.assertEqual(control["999"]["OBSERVACIONES"], "MARKER_SIN_ELEMENTOS")

    def test_old_y_desmantelada_no_impiden_promocion(self):
        resultado = self.ejecutar()
        filas = {f["NOMBRE_FICHERO_ORIGEN"]: f for f in self.leer_csv(self.actual)}

        self.assertTrue(resultado["promovida"])
        self.assertEqual(filas["compartida.jpg"]["OLD"], "1")
        self.assertEqual(filas["propia.png"]["DESMANTELADA"], "1")
        self.assertEqual({f["ESTADO_DESCARGA"] for f in filas.values()}, {m.EST_IMG_OK})

    def test_operador_y_distancia_no_impiden_promocion(self):
        escribir_inventario(self.inv, [
            ("0000758", "OTRO OPERADOR", 45.0, 5.0),
            ("4300034", "OTRO OPERADOR", 45.0, 5.0),
            ("999", "OTRO OPERADOR", 45.0, 5.0),
        ])

        resultado = self.ejecutar()
        control = {f["GIS_ID"]: f for f in self.leer_csv(self.actual, "CONTROL_GIS_ID.csv")}

        self.assertTrue(resultado["promovida"])
        self.assertEqual(control["0000758"]["OPERADOR_COINCIDE"], "NO")
        self.assertGreater(float(control["0000758"]["DISTANCIA_M"]), 100000)

    def test_seleccion_parcial_requiere_permiso(self):
        sin_permiso = self.ejecutar(markers=Abridor(RespuestaFalsa(json_bytes(self.respuestas["0000758"]))),
                                    gis_ids=["0000758"])

        self.assertIn("SELECCION_PARCIAL", sin_permiso["motivos"])
        self.assertFalse(self.actual.exists())

        con_permiso = self.ejecutar(markers=Abridor(RespuestaFalsa(json_bytes(self.respuestas["0000758"]))),
                                    gis_ids=["0000758"], permitir_parcial=True)

        self.assertTrue(con_permiso["promovida"])
        self.assertTrue(self.actual.is_dir())

    def test_segunda_ejecucion_actual_pasa_a_anterior(self):
        self.ejecutar()
        primera = arbol(self.actual)

        resultado = self.ejecutar()

        self.assertTrue(resultado["promovida"])
        self.assertTrue((self.actual / m.MARCADOR_COMPLETA).is_file())
        self.assertFalse(self.nueva.exists())
        self.assertEqual(
            {k for k in arbol(self.anterior) if k.endswith(".jpg") or k.endswith(".png")},
            {k for k in primera if k.endswith(".jpg") or k.endswith(".png")},
        )
        self.assertEqual(arbol(self.anterior)["IMATGES_INFOANTENAS.csv"], primera["IMATGES_INFOANTENAS.csv"])

    def test_tercera_ejecucion_elimina_el_anterior_antiguo(self):
        self.ejecutar()
        (self.actual / "marca_v1.txt").write_text("v1", encoding="utf-8")
        self.ejecutar()
        (self.actual / "marca_v2.txt").write_text("v2", encoding="utf-8")
        self.assertTrue((self.anterior / "marca_v1.txt").exists())

        self.ejecutar()

        self.assertTrue((self.anterior / "marca_v2.txt").exists())
        self.assertFalse((self.anterior / "marca_v1.txt").exists())
        self.assertFalse((self.actual / "marca_v2.txt").exists())

    def test_actual_y_anterior_intactos_si_la_ejecucion_falla(self):
        self.ejecutar()
        self.ejecutar()
        actual_antes, anterior_antes = arbol(self.actual), arbol(self.anterior)

        resultado = self.ejecutar(imagenes=Abridor(respuesta_imagen("JPEG"), http_error(404)))

        self.assertFalse(resultado["promovida"])
        self.assertEqual(arbol(self.actual), actual_antes)
        self.assertEqual(arbol(self.anterior), anterior_antes)
        self.assertTrue(self.nueva.is_dir())
        self.assertFalse((self.nueva / m.MARCADOR_COMPLETA).exists())

    def test_durante_la_ejecucion_actual_no_se_modifica(self):
        self.ejecutar()
        antes = arbol(self.actual)

        class Espia(Abridor):
            def __call__(self_, peticion, timeout):
                self.assertEqual(arbol(self.actual), antes)
                self.assertTrue(self.nueva.is_dir())
                return super().__call__(peticion, timeout)

        self.ejecutar(imagenes=Espia(respuesta_imagen("JPEG"), respuesta_imagen("PNG", (500, 500))))

    def test_rutas_del_csv_siguen_siendo_validas_tras_la_promocion(self):
        self.ejecutar()
        filas = self.leer_csv(self.actual)

        self.assertTrue(filas)
        for fila in filas:
            for campo, subdirectorio in (("RUTA_IMAGEN", "full/"), ("RUTA_MINIATURA", "thumb/")):
                ruta = fila[campo]
                self.assertTrue(ruta.startswith(subdirectorio), ruta)
                self.assertNotIn("nueva", ruta)
                self.assertTrue((self.actual / ruta).is_file(), ruta)
                with Image.open(self.actual / ruta) as imagen_final:
                    imagen_final.verify()

        resumen = self.leer_resumen(self.actual)
        self.assertEqual(resumen["entradas"]["trabajo_dir"], ".")
        self.assertEqual(resumen["entradas"]["markers_dir"], "markers")
        self.assertNotIn("nueva", json.dumps(resumen["entradas"]))

    def test_resumen_de_version(self):
        resultado = self.ejecutar()
        version = self.leer_resumen(self.actual)["version"]

        self.assertEqual(resultado["version"], version)
        self.assertEqual(version["inventario_sha256"], m.sha256_fichero(self.inv))
        self.assertEqual(version["gis_id_procesados"], 3)
        self.assertEqual(version["markers_encontrados"], 2)
        self.assertEqual(version["markers_vacios"], 1)
        self.assertEqual(version["errores_markers"], 0)
        self.assertEqual(version["relaciones_imagen"], 3)
        self.assertEqual(version["imagenes_fisicas_unicas"], 2)
        self.assertEqual(version["imagenes_descargadas_ok"], 2)
        self.assertEqual(version["imagenes_reutilizadas"], 0)
        self.assertEqual(version["miniaturas_generadas"], 2)
        self.assertEqual(version["errores_imagen"], 0)
        self.assertEqual(version["imagenes_pendientes"], 0)
        self.assertEqual(version["senales_bloqueo"], 0)
        self.assertEqual(version["resultado"], "COMPLETA")
        self.assertTrue(version["promocionable"])
        self.assertEqual(version["motivos_no_promocionable"], [])
        self.assertTrue(version["inicio"] <= version["fin"])
        run = json.loads((self.actual / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(run["resultado"], "COMPLETA")
        self.assertIn("fin", run)

    def test_corte_durante_la_promocion_se_recupera_en_la_siguiente_ejecucion(self):
        self.ejecutar()
        llamadas = []

        def renombrar_con_corte(origen, destino):
            llamadas.append(1)
            if len(llamadas) == 2:
                raise OSError("corte simulado")
            m._renombrar(origen, destino)

        with self.assertRaises(OSError):
            self.ejecutar(renombrar=renombrar_con_corte)

        self.assertFalse(self.actual.exists())
        self.assertTrue(m.version_completa(self.nueva))
        self.assertTrue(m.version_completa(self.anterior))

        mensajes = []
        m.ejecutar_version(
            self.inv, self.raiz, abrir=self.marcadores(), limitador=m.Limitador(0.0),
            parametros=dict(self.params, gis_ids=None, maxim=None, maxim_imatges=None),
            descarga={"abrir": self.imagenes_ok(), "limitador": m.Limitador(0.0),
                      "thumb_max": 300, "reintentos": 0},
            dormir=lambda s: None, imprimir=mensajes.append,
        )

        self.assertTrue(any("nueva -> actual" in str(x) for x in mensajes))
        self.assertTrue(m.version_completa(self.actual))
        self.assertTrue(m.version_completa(self.anterior))
        self.assertFalse(self.nueva.exists())


class TestVersionadoResume(VersionesBase):
    def fallar_una_imagen(self):
        return self.ejecutar(imagenes=Abridor(respuesta_imagen("JPEG"), http_error(404)))

    def test_resume_reutiliza_nueva_y_completa_solo_lo_pendiente(self):
        resultado = self.fallar_una_imagen()
        self.assertFalse(resultado["promocionable"])

        imagenes = Abridor(respuesta_imagen("PNG", (500, 500)))
        markers = Abridor()
        resultado = self.ejecutar(markers=markers, imagenes=imagenes, resume=True)

        self.assertEqual(markers.peticiones, [])
        self.assertEqual(len(imagenes.peticiones), 1)
        self.assertTrue(imagenes.peticiones[0].full_url.endswith("propia.png"))
        self.assertTrue(resultado["promovida"])
        self.assertEqual(resultado["version"]["imagenes_reutilizadas"], 1)
        self.assertEqual(resultado["version"]["imagenes_descargadas_ok"], 1)

    def test_resume_no_usa_actual_como_fuente(self):
        self.ejecutar()
        self.fallar_una_imagen()
        self.assertTrue(self.actual.is_dir())
        # El actual ya tiene propia.png, pero el resume debe descargarla de nuevo.
        imagenes = Abridor(respuesta_imagen("PNG", (500, 500)))
        self.ejecutar(markers=Abridor(), imagenes=imagenes, resume=True)

        self.assertEqual(len(imagenes.peticiones), 1)

    def test_resume_sin_nueva_no_reanuda_desde_actual(self):
        self.ejecutar()

        with self.assertRaises(ValueError):
            self.ejecutar(resume=True)

        self.assertTrue(self.actual.is_dir())

    def test_nueva_ejecucion_vuelve_a_empezar_desde_nueva_limpia(self):
        self.ejecutar()
        crear_version(self.nueva, "basura", completa=False)
        (self.nueva / "full").mkdir()
        (self.nueva / "full" / "huerfana.jpg").write_bytes(b"x")

        markers, imagenes = self.marcadores(), self.imagenes_ok()
        resultado = self.ejecutar(markers=markers, imagenes=imagenes)

        self.assertEqual(len(markers.peticiones), 3)
        self.assertEqual(len(imagenes.peticiones), 2)
        self.assertTrue(resultado["promovida"])
        self.assertFalse((self.actual / "full" / "huerfana.jpg").exists())
        self.assertFalse((self.anterior / "datos.txt").exists())

    def test_nueva_completa_pendiente_aborta_sin_borrar(self):
        crear_version(self.actual, "A")
        crear_version(self.nueva, "N")
        antes = arbol(self.raiz)

        with self.assertRaises(m.EstadoVersionesAmbiguo):
            self.ejecutar()

        self.assertEqual(arbol(self.raiz), antes)

    def test_resume_rechaza_inventario_distinto(self):
        self.fallar_una_imagen()
        escribir_inventario(self.inv, [("0000758", "OP", 41.0, 2.0)])

        with self.assertRaises(ValueError) as ctx:
            self.ejecutar(resume=True)

        self.assertIn("SHA-256", str(ctx.exception))
        self.assertTrue(self.nueva.is_dir())

    def test_resume_rechaza_parametros_de_contenido_distintos(self):
        self.fallar_una_imagen()

        with self.assertRaises(ValueError) as ctx:
            self.ejecutar(resume=True, maxim_imagenes=1)

        self.assertIn("maxim_imatges", str(ctx.exception))

    def test_resume_admite_cambiar_el_ritmo(self):
        self.fallar_una_imagen()
        self.params = dict(self.params, timeout=99)

        resultado = self.ejecutar(markers=Abridor(), imagenes=Abridor(respuesta_imagen("PNG", (500, 500))),
                                  resume=True)

        self.assertTrue(resultado["promovida"])

    def test_resume_retira_completa_de_una_nueva_ya_terminada(self):
        crear_version(self.nueva, "N")

        m.preparar_nueva(self.raiz, resume=True)

        self.assertFalse((self.nueva / m.MARCADOR_COMPLETA).exists())
        self.assertTrue((self.nueva / "datos.txt").exists())


class TestVerificacionEnDisco(VersionesBase):
    def setUp(self):
        super().setUp()
        self.ejecutar()
        self.resumen = self.leer_resumen(self.actual)

    def test_version_correcta_sin_motivos(self):
        self.assertEqual(m.verificar_version_en_disco(self.actual, self.resumen), [])

    def test_detecta_miniatura_ausente(self):
        (self.actual / "thumb" / "propia.png").unlink()
        self.assertEqual(m.verificar_version_en_disco(self.actual, self.resumen),
                         ["MINIATURAS_INVALIDAS:1"])

    def test_detecta_original_corrupto(self):
        (self.actual / "full" / "compartida.jpg").write_bytes(b"corrupto")
        self.assertEqual(m.verificar_version_en_disco(self.actual, self.resumen),
                         ["ORIGINALES_INVALIDOS:1"])

    def test_detecta_ficheros_part(self):
        (self.actual / "full" / "x.jpg.part").write_bytes(b"x")
        self.assertEqual(m.verificar_version_en_disco(self.actual, self.resumen),
                         ["FICHEROS_PART:1"])

    def test_detecta_csv_ausente_o_incompleto(self):
        (self.actual / "CONTROL_GIS_ID.csv").unlink()
        motivos = m.verificar_version_en_disco(self.actual, self.resumen)

        self.assertIn("FALTA_CONTROL_GIS_ID.csv", motivos)
        self.assertIn("CONTROL_GIS_ID_INVALIDO", motivos)


class TestCliVersionado(VersionesBase):
    def main(self, *argumentos):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return m.main(["--inventario", str(self.inv), "--versiones-dir", str(self.raiz), *argumentos])

    def test_incompatible_con_output_dir(self):
        for opcion in (["--output-dir", "x"], ["--trabajo-dir", "x"], ["--imagenes-dir", "x"]):
            with self.subTest(opcion), self.assertRaises(SystemExit):
                self.main(*opcion)

    def test_opciones_de_version_requieren_versiones_dir(self):
        for opcion in ("--permitir-seleccion-parcial", "--promover-nueva"):
            with self.subTest(opcion), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    m.main(["--inventario", str(self.inv), opcion])

    def test_promover_nueva_completa(self):
        crear_version(self.actual, "A")
        crear_version(self.nueva, "N")

        self.assertEqual(self.main("--promover-nueva"), 0)

        self.assertEqual((self.actual / "datos.txt").read_text(encoding="utf-8"), "N")
        self.assertEqual((self.anterior / "datos.txt").read_text(encoding="utf-8"), "A")

    def test_promover_nueva_recupera_un_corte_previo(self):
        crear_version(self.anterior, "A")
        crear_version(self.nueva, "N")

        self.assertEqual(self.main("--promover-nueva"), 0)

        self.assertEqual((self.actual / "datos.txt").read_text(encoding="utf-8"), "N")

    def test_promover_nueva_incompleta_falla_sin_tocar_nada(self):
        crear_version(self.actual, "A")
        crear_version(self.nueva, "parcial", completa=False)
        antes = arbol(self.raiz)

        with self.assertRaises(ValueError):
            self.main("--promover-nueva")

        self.assertEqual(arbol(self.raiz), antes)

    def test_estado_ambiguo_devuelve_codigo_6(self):
        crear_version(self.actual, "A", completa=False)
        antes = arbol(self.raiz)

        self.assertEqual(self.main("--promover-nueva"), 6)
        self.assertEqual(arbol(self.raiz), antes)

    def test_promover_nueva_no_admite_resume(self):
        with self.assertRaises(SystemExit):
            self.main("--promover-nueva", "--resume")


# ---------------------------------------------------------------------------
# Renombrado atómico con reintentos (bloqueos transitorios de Windows)
# ---------------------------------------------------------------------------

REPLACE_REAL = m.os.replace


class FalloTransitorio:
    """Sustituto de os.replace: falla con PermissionError las n primeras veces por destino."""

    def __init__(self, fallos_por_destino=1, error=None):
        self.fallos_por_destino = fallos_por_destino
        self.error = error or (lambda: PermissionError(13, "Acceso denegado"))
        self.intentos = {}

    def __call__(self, origen, destino):
        clave = str(destino)
        self.intentos[clave] = self.intentos.get(clave, 0) + 1

        if self.intentos[clave] <= self.fallos_por_destino:
            raise self.error()

        return REPLACE_REAL(origen, destino)


class TestRenombradoAtomico(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.origen = self.tmp / "dato.json.tmp"
        self.destino = self.tmp / "dato.json"
        self.origen.write_text("nuevo", encoding="utf-8")

    def renombrar(self, falla, **kwargs):
        with mock.patch.object(m.os, "replace", side_effect=falla) as replace, \
                mock.patch.object(m.time, "sleep") as dormir:
            try:
                m._renombrar(self.origen, self.destino, **kwargs)
            finally:
                self.replace, self.dormir = replace, dormir

    def test_sin_fallos_no_espera(self):
        self.renombrar(FalloTransitorio(0))

        self.assertEqual(self.destino.read_text(encoding="utf-8"), "nuevo")
        self.assertFalse(self.origen.exists())
        self.dormir.assert_not_called()

    def test_primer_intento_con_permissionerror_y_siguiente_correcto(self):
        self.renombrar(FalloTransitorio(1))

        self.assertEqual(self.replace.call_count, 2)
        self.assertEqual(self.dormir.call_args_list, [mock.call(m.RENOMBRADO_ESPERA_S)])
        self.assertEqual(self.destino.read_text(encoding="utf-8"), "nuevo")
        self.assertFalse(self.origen.exists())

    def test_varios_fallos_temporales_y_posterior_exito(self):
        self.renombrar(FalloTransitorio(3), intentos=6, espera=0.1)

        self.assertEqual(self.replace.call_count, 4)
        self.assertEqual(self.dormir.call_args_list,
                         [mock.call(0.1), mock.call(0.2), mock.call(0.4)])
        self.assertEqual(self.destino.read_text(encoding="utf-8"), "nuevo")
        self.assertFalse(self.origen.exists())

    def test_ultimo_intento_correcto_aun_se_acepta(self):
        self.renombrar(FalloTransitorio(2), intentos=3)

        self.assertEqual(self.replace.call_count, 3)
        self.assertEqual(self.destino.read_text(encoding="utf-8"), "nuevo")

    def test_se_supera_el_maximo_y_se_propaga_el_error(self):
        self.destino.write_text("anterior", encoding="utf-8")

        with self.assertRaises(PermissionError):
            self.renombrar(FalloTransitorio(100), intentos=4)

        self.assertEqual(self.replace.call_count, 4)
        self.assertEqual(self.dormir.call_count, 3)
        self.assertEqual(self.destino.read_text(encoding="utf-8"), "anterior")
        self.assertTrue(self.origen.exists())

    def test_intentos_por_defecto_acotados(self):
        with self.assertRaises(PermissionError):
            self.renombrar(FalloTransitorio(100))

        self.assertEqual(self.replace.call_count, m.RENOMBRADO_INTENTOS)
        self.assertEqual(self.dormir.call_count, m.RENOMBRADO_INTENTOS - 1)

    def test_otros_oserror_no_se_reintentan(self):
        for error in (OSError("disco"), FileNotFoundError("falta"), FileExistsError("existe")):
            with self.subTest(error=type(error).__name__):
                with self.assertRaises(type(error)):
                    self.renombrar(FalloTransitorio(100, error=lambda e=error: e))

                self.assertEqual(self.replace.call_count, 1)
                self.dormir.assert_not_called()

    def test_error_no_oserror_se_propaga_sin_reintentar(self):
        with self.assertRaises(ValueError):
            self.renombrar(ValueError("otro"))

        self.assertEqual(self.replace.call_count, 1)

    def test_intentos_invalidos(self):
        with self.assertRaises(ValueError):
            m._renombrar(self.origen, self.destino, intentos=0)

        self.assertTrue(self.origen.exists())


class TestEscrituraAtomicaConReintentos(unittest.TestCase):
    DATOS = {"clave": "ñandú", "lista": [1, 2, {"a": None}], "ok": True}
    FILAS = [{"A": "1", "B": "ñ"}, {"A": "x;y", "B": ""}]

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def con_fallos(self, n):
        return mock.patch.object(m.os, "replace", side_effect=FalloTransitorio(n)), \
            mock.patch.object(m.time, "sleep")

    def residuos(self):
        return [p.name for p in self.tmp.rglob("*") if p.suffix in (".tmp", ".part")]

    def test_json_identico_con_y_sin_fallos_transitorios(self):
        esperado = json.dumps(self.DATOS, ensure_ascii=False, indent=2).encode("utf-8")

        m.escribir_json_atomico(self.tmp / "limpio.json", self.DATOS)
        replace, dormir = self.con_fallos(2)
        with replace, dormir:
            m.escribir_json_atomico(self.tmp / "con_fallos.json", self.DATOS)

        self.assertEqual((self.tmp / "limpio.json").read_bytes(), esperado)
        self.assertEqual((self.tmp / "con_fallos.json").read_bytes(), esperado)
        self.assertEqual(self.residuos(), [])

    def test_json_con_sufijo_e_indent_se_respetan(self):
        m.escribir_json_atomico(self.tmp / "a.json", {"x": 1}, sufijo=".part", indent=None)

        self.assertEqual((self.tmp / "a.json").read_bytes(), b'{"x": 1}')
        self.assertEqual(self.residuos(), [])

    def test_csv_identico_con_y_sin_fallos_transitorios(self):
        esperado = ("\ufeffA;B\r\n1;ñ\r\n\"x;y\";\r\n").encode("utf-8")

        m.escribir_csv_atomico(self.tmp / "limpio.csv", ["A", "B"], self.FILAS)
        replace, dormir = self.con_fallos(2)
        with replace, dormir:
            m.escribir_csv_atomico(self.tmp / "con_fallos.csv", ["A", "B"], self.FILAS)

        self.assertEqual((self.tmp / "limpio.csv").read_bytes(), esperado)
        self.assertEqual((self.tmp / "con_fallos.csv").read_bytes(), esperado)
        self.assertEqual(self.residuos(), [])

    def test_fallo_definitivo_conserva_el_fichero_anterior_sin_parcial(self):
        for escribir, nombre, previo in (
            (lambda p: m.escribir_json_atomico(p, self.DATOS), "a.json", b'{"previo": 1}'),
            (lambda p: m.escribir_csv_atomico(p, ["A", "B"], self.FILAS), "a.csv", b"previo"),
        ):
            with self.subTest(nombre):
                destino = self.tmp / nombre
                destino.write_bytes(previo)
                replace, dormir = self.con_fallos(100)

                with replace, dormir:
                    with self.assertRaises(PermissionError):
                        escribir(destino)

                self.assertEqual(destino.read_bytes(), previo)

    def test_fallo_definitivo_sin_fichero_anterior_no_crea_el_final(self):
        replace, dormir = self.con_fallos(100)

        with replace, dormir:
            with self.assertRaises(PermissionError):
                m.escribir_json_atomico(self.tmp / "a.json", self.DATOS)

        self.assertFalse((self.tmp / "a.json").exists())

    def test_primero_se_escribe_el_temporal_y_despues_se_sustituye(self):
        vistos = {}

        def espia(origen, destino):
            vistos["temporal"] = Path(origen).read_text(encoding="utf-8")
            vistos["final_existe"] = Path(destino).exists()
            return REPLACE_REAL(origen, destino)

        with mock.patch.object(m.os, "replace", side_effect=espia):
            m.escribir_json_atomico(self.tmp / "a.json", {"x": 1})

        self.assertEqual(json.loads(vistos["temporal"]), {"x": 1})
        self.assertFalse(vistos["final_existe"])

    def test_otro_oserror_no_se_reintenta(self):
        with mock.patch.object(m.os, "replace", side_effect=OSError("disco")) as replace, \
                mock.patch.object(m.time, "sleep") as dormir:
            with self.assertRaises(OSError):
                m.escribir_csv_atomico(self.tmp / "a.csv", ["A"], [{"A": "1"}])

        self.assertEqual(replace.call_count, 1)
        dormir.assert_not_called()


class TestImagenesConBloqueosTransitorios(ImagenesBase):
    def test_descarga_con_bloqueo_transitorio_al_publicar(self):
        fallos = FalloTransitorio(1)

        with mock.patch.object(m.os, "replace", side_effect=fallos), \
                mock.patch.object(m.time, "sleep"):
            r = self.procesar(Abridor(respuesta_imagen("JPEG", (400, 200))), "foto.jpg")

        self.assertEqual(r["estado"], m.EST_IMG_OK)
        self.assertTrue(self.existe_full() and self.existe_thumb())
        self.assertEqual(sin_partes(self.tmp), [])

    def test_regeneracion_de_miniatura_con_bloqueo_transitorio(self):
        self.procesar(Abridor(respuesta_imagen("JPEG", (400, 200))), "foto.jpg")
        (self.imagenes / "thumb" / "foto.jpg").unlink()

        with mock.patch.object(m.os, "replace", side_effect=FalloTransitorio(1)), \
                mock.patch.object(m.time, "sleep"):
            r = self.procesar(Abridor(), "foto.jpg")

        self.assertEqual((r["estado"], r["accion"]), (m.EST_IMG_OK, m.ACCION_SOLO_MINIATURA))
        self.assertTrue(self.existe_thumb())


class TestVersionadoConBloqueosTransitorios(VersionesBase):
    def test_ejecucion_completa_con_un_bloqueo_por_cada_renombrado(self):
        self.ejecutar()
        limpia = {
            p.relative_to(self.actual).as_posix(): p.read_bytes()
            for p in self.actual.rglob("*")
            if p.is_file() and p.suffix in (".csv", ".jpg", ".png")
            and p.name not in ("log_markers.csv", "log_imatges.csv")
        }

        self.raiz = self.tmp / "con_bloqueos"
        self.nueva, self.actual, self.anterior = m.rutas_versiones(self.raiz)
        fallos = FalloTransitorio(1)

        with mock.patch.object(m.os, "replace", side_effect=fallos), \
                mock.patch.object(m.time, "sleep"):
            resultado = self.ejecutar()

        self.assertTrue(resultado["promovida"])
        self.assertTrue(all(n >= 2 for n in fallos.intentos.values()))
        self.assertEqual(self.residuos_en_version(), [])
        con_bloqueos = {
            p.relative_to(self.actual).as_posix(): p.read_bytes()
            for p in self.actual.rglob("*")
            if p.is_file() and p.suffix in (".csv", ".jpg", ".png")
            and p.name not in ("log_markers.csv", "log_imatges.csv")
        }
        self.assertEqual(con_bloqueos, limpia)

    def residuos_en_version(self):
        return [p.name for p in self.actual.rglob("*") if p.suffix in (".tmp", ".part")]


# ---------------------------------------------------------------------------
# FREQUENCIES_ANTENASMOVILES.csv
# ---------------------------------------------------------------------------

COLUMNAS_FRECUENCIAS = [
    "GIS_ID", "CODI_FREQUENCIA", "FREQUENCIA", "ESTAT", "PRIMERA_VEGADA_VISTA",
    "ULTIMA_VEGADA_VISTA", "ULTIMA_VEGADA_ACTUALITZADA", "CODI_OPERADOR",
]


def frecuencia(codigo, gis_id="1", estat=False, freq="949.90 - 959.90"):
    return [codigo, gis_id, freq, estat, "24/09/2024", "03/09/2026", "25/09/2024", "1"]


def elemento_con(frecuencias, codigo="1"):
    return [[codigo], ["OP", "1", "1"], ["DIR", 41.0, 2.0, "1m"], frecuencias, [], False, []]


def referencia_original(markers_dir):
    """Réplica de la lógica validada del script original (glob de *.json)."""
    filas = []

    for path in sorted(markers_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        respuesta = data.get("RESPUESTA", [])

        if not isinstance(respuesta, list):
            continue

        for marcador in respuesta:
            if not isinstance(marcador, list) or len(marcador) < 4:
                continue
            if not isinstance(marcador[3], list):
                continue

            for freq in marcador[3]:
                if not isinstance(freq, list) or len(freq) < 8:
                    continue

                filas.append({
                    "GIS_ID": str(freq[1]).strip(),
                    "CODI_FREQUENCIA": str(freq[0]).strip(),
                    "FREQUENCIA": str(freq[2]).strip(),
                    "ESTAT": str(freq[3]),
                    "PRIMERA_VEGADA_VISTA": str(freq[4]).strip(),
                    "ULTIMA_VEGADA_VISTA": str(freq[5]).strip(),
                    "ULTIMA_VEGADA_ACTUALITZADA": str(freq[6]).strip(),
                    "CODI_OPERADOR": str(freq[7]).strip(),
                })

    return filas


def guardar_marker(markers_dir, gis_id, data):
    markers_dir.mkdir(parents=True, exist_ok=True)
    m.guardar_respuesta(
        markers_dir / m.nombre_fichero_marker(gis_id),
        gis_id,
        {"http_status": 200, "url": "u", "content_type": "application/json", "data": data},
    )


class TestExtraerFrecuencias(unittest.TestCase):
    def test_columnas_exactas_y_orden(self):
        self.assertEqual(m.CAMPOS_FRECUENCIAS, COLUMNAS_FRECUENCIAS)
        self.assertEqual(m.NOMBRE_FRECUENCIAS, "FREQUENCIES_ANTENASMOVILES.csv")

        filas, _, _ = m.extraer_frecuencias(MARKER_REAL)

        self.assertEqual(list(filas[0]), COLUMNAS_FRECUENCIAS)

    def test_origen_de_cada_campo(self):
        filas, mal, sin_bloque = m.extraer_frecuencias(MARKER_REAL)

        self.assertEqual((mal, sin_bloque), (0, 0))
        self.assertEqual(filas, [{
            "GIS_ID": "0000758",
            "CODI_FREQUENCIA": "BB-2402052",
            "FREQUENCIA": "949.90 - 959.90",
            "ESTAT": "False",
            "PRIMERA_VEGADA_VISTA": "24/09/2024",
            "ULTIMA_VEGADA_VISTA": "03/09/2026",
            "ULTIMA_VEGADA_ACTUALITZADA": "24/09/2024",
            "CODI_OPERADOR": "1",
        }])

    def test_estat_se_conserva_como_texto_del_valor(self):
        filas, _, _ = m.extraer_frecuencias([elemento_con([
            frecuencia("A", estat=False), frecuencia("B", estat=True),
        ])])

        self.assertEqual([f["ESTAT"] for f in filas], ["False", "True"])

    def test_resto_de_campos_se_recortan_pero_estat_no(self):
        freq = [" A ", " 1 ", " f ", " x ", " p ", " u ", " a ", " o "]
        filas, _, _ = m.extraer_frecuencias([elemento_con([freq])])

        self.assertEqual(filas[0]["CODI_FREQUENCIA"], "A")
        self.assertEqual(filas[0]["ESTAT"], " x ")

    def test_varias_frecuencias_por_marker_en_orden_original(self):
        filas, _, _ = m.extraer_frecuencias([elemento_con([
            frecuencia("C"), frecuencia("A"), frecuencia("B"),
        ])])

        self.assertEqual([f["CODI_FREQUENCIA"] for f in filas], ["C", "A", "B"])

    def test_varias_respuestas_por_json(self):
        filas, _, _ = m.extraer_frecuencias([
            elemento_con([frecuencia("A", "1")], "1"),
            elemento_con([frecuencia("B", "2"), frecuencia("C", "2")], "2"),
        ])

        self.assertEqual([(f["GIS_ID"], f["CODI_FREQUENCIA"]) for f in filas],
                         [("1", "A"), ("2", "B"), ("2", "C")])

    def test_respuesta_vacia_da_cero_filas_sin_incidencias(self):
        self.assertEqual(m.extraer_frecuencias([]), ([], 0, 0))

    def test_elemento_con_lista_de_frecuencias_vacia_no_es_incidencia(self):
        self.assertEqual(m.extraer_frecuencias([elemento_con([])]), ([], 0, 0))

    def test_frecuencia_mal_formada_se_cuenta_y_el_resto_sigue(self):
        filas, mal, sin_bloque = m.extraer_frecuencias([elemento_con([
            frecuencia("A"), ["corta", "1", "f"], "texto", None, frecuencia("B"),
        ])])

        self.assertEqual([f["CODI_FREQUENCIA"] for f in filas], ["A", "B"])
        self.assertEqual((mal, sin_bloque), (3, 0))

    def test_frecuencia_con_mas_de_ocho_posiciones_se_acepta(self):
        filas, mal, _ = m.extraer_frecuencias([elemento_con([frecuencia("A") + ["extra"]])])

        self.assertEqual(len(filas), 1)
        self.assertEqual(mal, 0)
        self.assertEqual(list(filas[0]), COLUMNAS_FRECUENCIAS)

    def test_elemento_sin_bloque_valido_se_cuenta(self):
        filas, mal, sin_bloque = m.extraer_frecuencias([
            [["1"], ["OP"], ["D"], "no es lista"],
            [["2"], ["OP"], ["D"]],
            "no es lista",
            elemento_con([frecuencia("A")]),
        ])

        self.assertEqual([f["CODI_FREQUENCIA"] for f in filas], ["A"])
        self.assertEqual((mal, sin_bloque), (0, 3))

    def test_respuesta_que_no_es_lista_no_lanza(self):
        self.assertEqual(m.extraer_frecuencias({"error": "x"}), ([], 0, 0))


class TestGenerarFrecuencias(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.markers = self.tmp / "markers"
        self.destino = self.tmp / m.NOMBRE_FRECUENCIAS

    def leer(self):
        with self.destino.open(encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f, delimiter=";"))

    def test_formato_del_fichero(self):
        guardar_marker(self.markers, "0000758", MARKER_REAL)
        resumen = m.generar_frecuencias(self.markers, ["0000758"], self.destino)

        contenido = self.destino.read_bytes()
        self.assertTrue(contenido.startswith(b"\xef\xbb\xbf"))
        primera = contenido.decode("utf-8-sig").splitlines()[0]
        self.assertEqual(primera, ";".join(COLUMNAS_FRECUENCIAS))
        self.assertEqual(self.leer()[0]["ESTAT"], "False")
        self.assertEqual(resumen["filas"], 1)
        self.assertEqual(resumen["markers_procesados"], 1)

    def test_respuesta_vacia_genera_solo_cabecera(self):
        guardar_marker(self.markers, "999", [])
        resumen = m.generar_frecuencias(self.markers, ["999"], self.destino)

        self.assertEqual(self.leer(), [])
        self.assertEqual(self.destino.read_text(encoding="utf-8-sig").strip(),
                         ";".join(COLUMNAS_FRECUENCIAS))
        self.assertEqual(resumen["filas"], 0)
        self.assertEqual(resumen["markers_procesados"], 1)
        self.assertEqual(resumen["frecuencias_mal_formadas"], 0)

    def test_orden_determinista_sigue_la_seleccion(self):
        guardar_marker(self.markers, "A", [elemento_con([frecuencia("A2"), frecuencia("A1")])])
        guardar_marker(self.markers, "B", [elemento_con([frecuencia("B1")])])

        m.generar_frecuencias(self.markers, ["B", "A"], self.destino)
        self.assertEqual([f["CODI_FREQUENCIA"] for f in self.leer()], ["B1", "A2", "A1"])

        m.generar_frecuencias(self.markers, ["A", "B"], self.destino)
        self.assertEqual([f["CODI_FREQUENCIA"] for f in self.leer()], ["A2", "A1", "B1"])

    def test_solo_se_leen_los_gis_id_de_la_seleccion(self):
        guardar_marker(self.markers, "A", [elemento_con([frecuencia("A1")])])
        guardar_marker(self.markers, "B", [elemento_con([frecuencia("B1")])])

        m.generar_frecuencias(self.markers, ["A"], self.destino)

        self.assertEqual([f["CODI_FREQUENCIA"] for f in self.leer()], ["A1"])

    def test_duplicados_se_detectan_pero_no_se_eliminan(self):
        guardar_marker(self.markers, "A", [elemento_con([frecuencia("X"), frecuencia("Y")])])
        guardar_marker(self.markers, "B", [elemento_con([frecuencia("X"), frecuencia("Z", "2")])])

        resumen = m.generar_frecuencias(self.markers, ["A", "B"], self.destino)

        self.assertEqual(len(self.leer()), 4)
        self.assertEqual(resumen["filas"], 4)
        self.assertEqual(resumen["codi_frequencia_distintos"], 3)
        self.assertEqual(resumen["codi_frequencia_repetidos"], 1)
        self.assertEqual(resumen["filas_con_codi_frequencia_repetido"], 2)
        self.assertEqual(resumen["codi_frequencia_repetidos_detalle"], ["X"])
        self.assertEqual(resumen["filas_duplicadas_exactas"], 1)

    def test_mismo_codigo_con_datos_distintos_no_es_duplicado_exacto(self):
        guardar_marker(self.markers, "A", [elemento_con([
            frecuencia("X"), frecuencia("X", freq="1800"),
        ])])
        resumen = m.generar_frecuencias(self.markers, ["A"], self.destino)

        self.assertEqual(resumen["codi_frequencia_repetidos"], 1)
        self.assertEqual(resumen["filas_duplicadas_exactas"], 0)

    def test_sin_duplicados(self):
        guardar_marker(self.markers, "A", [elemento_con([frecuencia("X"), frecuencia("Y")])])
        resumen = m.generar_frecuencias(self.markers, ["A"], self.destino)

        self.assertEqual(resumen["codi_frequencia_repetidos"], 0)
        self.assertEqual(resumen["codi_frequencia_repetidos_detalle"], [])
        self.assertEqual(resumen["filas_duplicadas_exactas"], 0)

    def test_incidencias_se_acumulan_en_el_resumen(self):
        guardar_marker(self.markers, "A", [
            elemento_con([frecuencia("X"), ["corta"]]),
            [["2"], ["OP"], ["D"], "no lista"],
        ])
        resumen = m.generar_frecuencias(self.markers, ["A"], self.destino)

        self.assertEqual(resumen["filas"], 1)
        self.assertEqual(resumen["frecuencias_mal_formadas"], 1)
        self.assertEqual(resumen["elementos_sin_bloque_frecuencias"], 1)

    def test_marker_ausente_o_invalido_no_rompe(self):
        guardar_marker(self.markers, "A", [elemento_con([frecuencia("A1")])])
        (self.markers / m.nombre_fichero_marker("C")).write_text("{no json", encoding="utf-8")

        resumen = m.generar_frecuencias(self.markers, ["A", "B", "C"], self.destino)

        self.assertEqual(resumen["markers_procesados"], 1)
        self.assertEqual(resumen["markers_no_disponibles"], 2)
        self.assertEqual(len(self.leer()), 1)

    def test_escritura_atomica_sin_residuos(self):
        guardar_marker(self.markers, "A", [elemento_con([frecuencia("A1")])])
        m.generar_frecuencias(self.markers, ["A"], self.destino)

        self.assertEqual(list(self.tmp.rglob("*.part")), [])
        self.assertEqual(list(self.tmp.rglob("*.tmp")), [])

    def test_fallo_al_renombrar_conserva_el_fichero_anterior(self):
        guardar_marker(self.markers, "A", [elemento_con([frecuencia("A1")])])
        self.destino.write_text("previo", encoding="utf-8")

        with mock.patch.object(m.os, "replace", side_effect=OSError("bloqueado")):
            with self.assertRaises(OSError):
                m.generar_frecuencias(self.markers, ["A"], self.destino)

        self.assertEqual(self.destino.read_text(encoding="utf-8"), "previo")

    def test_no_usa_la_red(self):
        guardar_marker(self.markers, "A", [elemento_con([frecuencia("A1")])])

        with mock.patch.object(m.urllib.request, "urlopen", side_effect=AssertionError("red")):
            m.generar_frecuencias(self.markers, ["A"], self.destino)

    def test_listar_gis_ids_guardados(self):
        guardar_marker(self.markers, "B", [])
        guardar_marker(self.markers, "A", [])
        (self.markers / "roto.json").write_text("{no json", encoding="utf-8")

        gis_ids, ilegibles = m.listar_gis_ids_guardados(self.markers)

        self.assertEqual(sorted(gis_ids), ["A", "B"])
        self.assertEqual(ilegibles, 1)


class FrecuenciasVersionesBase(VersionesBase):
    """VersionesBase con frecuencias en las respuestas: 3 + 1 + 0 = 4 filas."""

    def setUp(self):
        super().setUp()
        self.respuestas["0000758"][0][3] = [
            frecuencia("F1", "0000758"), frecuencia("F2", "0000758", True),
        ]
        self.respuestas["0000758"].append(
            elemento_con([frecuencia("F3", "0000758")], "0000758")
        )
        self.respuestas["4300034"][0][3] = [frecuencia("F4", "4300034")]

    def filas_frecuencias(self, version):
        return self.leer_csv(version, m.NOMBRE_FRECUENCIAS)


class TestFrecuenciasEnVersion(FrecuenciasVersionesBase):
    def test_se_genera_en_nueva_y_se_promociona_a_actual(self):
        vistos = {}
        original = m.crear_completa

        def espia(version_dir, metadatos):
            vistos["nueva"] = (version_dir / m.NOMBRE_FRECUENCIAS).is_file()
            original(version_dir, metadatos)

        with mock.patch.object(m, "crear_completa", side_effect=espia):
            resultado = self.ejecutar()

        self.assertTrue(vistos["nueva"])
        self.assertTrue(resultado["promovida"])
        self.assertFalse(self.nueva.exists())
        self.assertIn(m.NOMBRE_FRECUENCIAS, m.ARCHIVOS_VERSION)
        filas = self.filas_frecuencias(self.actual)
        self.assertEqual([f["CODI_FREQUENCIA"] for f in filas], ["F1", "F2", "F3", "F4"])
        self.assertEqual([f["ESTAT"] for f in filas], ["False", "True", "False", "False"])
        self.assertEqual(list(filas[0]), COLUMNAS_FRECUENCIAS)

    def test_bloque_de_frecuencias_en_resum_json(self):
        self.ejecutar()
        bloque = self.leer_resumen(self.actual)["frecuencias"]

        self.assertEqual(bloque["fichero"], m.NOMBRE_FRECUENCIAS)
        self.assertEqual(bloque["filas"], 4)
        self.assertEqual(bloque["markers_procesados"], 3)
        self.assertEqual(bloque["frecuencias_mal_formadas"], 0)
        self.assertEqual(bloque["codi_frequencia_repetidos"], 0)
        self.assertEqual(bloque["codi_frequencia_distintos"], 4)

    def test_duplicados_se_reportan_sin_impedir_la_promocion(self):
        self.respuestas["4300034"][0][3] = [frecuencia("F1", "4300034")]
        resultado = self.ejecutar()

        self.assertTrue(resultado["promovida"])
        self.assertEqual(len(self.filas_frecuencias(self.actual)), 4)
        bloque = self.leer_resumen(self.actual)["frecuencias"]
        self.assertEqual(bloque["codi_frequencia_repetidos"], 1)
        self.assertEqual(bloque["codi_frequencia_repetidos_detalle"], ["F1"])

    def test_frecuencia_mal_formada_no_rompe_la_ejecucion(self):
        self.respuestas["4300034"][0][3] = [["corta"], frecuencia("F4", "4300034")]
        resultado = self.ejecutar()

        self.assertTrue(resultado["promovida"])
        self.assertEqual(self.leer_resumen(self.actual)["frecuencias"]["frecuencias_mal_formadas"], 1)

    def test_ninguna_llamada_http_adicional(self):
        markers, imagenes = self.marcadores(), self.imagenes_ok()
        self.ejecutar(markers=markers, imagenes=imagenes)

        self.assertEqual(len(markers.peticiones), 3)
        self.assertEqual(len(imagenes.peticiones), 2)
        self.assertTrue(all("/v1/markers" in p.full_url for p in markers.peticiones))

    def test_generar_frecuencias_no_hace_peticiones(self):
        with mock.patch.object(m, "generar_frecuencias", wraps=m.generar_frecuencias) as espia, \
                mock.patch.object(m.urllib.request, "urlopen", side_effect=AssertionError("red")):
            self.ejecutar()

        espia.assert_called_once()

    def test_resume_genera_el_csv_completo(self):
        resultado = self.ejecutar(imagenes=Abridor(respuesta_imagen("JPEG"), http_error(404)))
        self.assertFalse(resultado["promocionable"])

        markers = Abridor()
        resultado = self.ejecutar(
            markers=markers, imagenes=Abridor(respuesta_imagen("PNG", (500, 500))), resume=True
        )

        self.assertEqual(markers.peticiones, [])
        self.assertTrue(resultado["promovida"])
        filas = self.filas_frecuencias(self.actual)
        self.assertEqual([f["CODI_FREQUENCIA"] for f in filas], ["F1", "F2", "F3", "F4"])
        self.assertEqual(self.leer_resumen(self.actual)["frecuencias"]["filas"], 4)

    def test_resume_con_markers_pendientes_incluye_los_reutilizados(self):
        with self.assertRaises(KeyboardInterrupt):
            self.ejecutar(markers=Abridor(
                RespuestaFalsa(json_bytes(self.respuestas["0000758"])), KeyboardInterrupt(),
            ))

        markers = Abridor(
            RespuestaFalsa(json_bytes(self.respuestas["4300034"])), RespuestaFalsa(b"[]"),
        )
        resultado = self.ejecutar(markers=markers, resume=True)

        self.assertEqual(len(markers.peticiones), 2)
        self.assertTrue(resultado["promovida"])
        self.assertEqual([f["CODI_FREQUENCIA"] for f in self.filas_frecuencias(self.actual)],
                         ["F1", "F2", "F3", "F4"])

    def test_version_sin_el_csv_no_se_promociona(self):
        def sin_escribir(markers_dir, gis_ids, destino):
            return {"filas": 0}

        with mock.patch.object(m, "generar_frecuencias", side_effect=sin_escribir):
            resultado = self.ejecutar()

        self.assertFalse(resultado["promocionable"])
        self.assertFalse(resultado["promovida"])
        self.assertIn("FALTA_" + m.NOMBRE_FRECUENCIAS, resultado["motivos"])
        self.assertIn("FRECUENCIAS_INVALIDO", resultado["motivos"])
        self.assertFalse((self.nueva / m.MARCADOR_COMPLETA).exists())
        self.assertTrue(self.nueva.is_dir())
        self.assertFalse(self.actual.exists())

    def test_version_con_csv_inconsistente_no_se_promociona(self):
        original = m.generar_frecuencias

        def recortando(markers_dir, gis_ids, destino):
            resumen = original(markers_dir, gis_ids, destino)
            lineas = destino.read_text(encoding="utf-8-sig").splitlines()
            destino.write_text("\n".join(lineas[:-1]) + "\n", encoding="utf-8-sig")
            return resumen

        with mock.patch.object(m, "generar_frecuencias", side_effect=recortando):
            resultado = self.ejecutar()

        self.assertFalse(resultado["promovida"])
        self.assertIn("FRECUENCIAS_INCONSISTENTE", resultado["motivos"])

    def test_verificacion_detecta_csv_ausente_cabecera_o_filas(self):
        self.ejecutar()
        resumen = self.leer_resumen(self.actual)
        ruta = self.actual / m.NOMBRE_FRECUENCIAS
        original = ruta.read_bytes()
        self.assertEqual(m.verificar_version_en_disco(self.actual, resumen), [])

        ruta.write_text("OTRA;CABECERA\n1;2\n", encoding="utf-8-sig")
        self.assertEqual(m.verificar_version_en_disco(self.actual, resumen),
                         ["FRECUENCIAS_INVALIDO"])

        ruta.write_bytes(original)
        resumen["frecuencias"]["filas"] += 1
        self.assertEqual(m.verificar_version_en_disco(self.actual, resumen),
                         ["FRECUENCIAS_INCONSISTENTE"])

        ruta.unlink()
        motivos = m.verificar_version_en_disco(self.actual, resumen)
        self.assertIn("FALTA_" + m.NOMBRE_FRECUENCIAS, motivos)
        self.assertIn("FRECUENCIAS_INVALIDO", motivos)

    def test_no_cambian_imagenes_ni_control_ni_descargas(self):
        self.ejecutar()
        con_frecuencias = self.actual

        self.raiz = self.tmp / "sin_frecuencias"
        self.nueva, self.actual, self.anterior = m.rutas_versiones(self.raiz)

        def sin_escribir(markers_dir, gis_ids, destino):
            return {"filas": 0}

        with mock.patch.object(m, "generar_frecuencias", side_effect=sin_escribir):
            self.ejecutar()

        sin_frecuencias = self.nueva

        for nombre in ("IMATGES_INFOANTENAS.csv", "CONTROL_GIS_ID.csv"):
            self.assertEqual((con_frecuencias / nombre).read_bytes(),
                             (sin_frecuencias / nombre).read_bytes(), nombre)

        for subdirectorio in ("full", "thumb"):
            a = {p.name: p.read_bytes() for p in (con_frecuencias / subdirectorio).iterdir()}
            b = {p.name: p.read_bytes() for p in (sin_frecuencias / subdirectorio).iterdir()}
            self.assertEqual(a, b, subdirectorio)

    def test_equivalencia_con_el_script_standalone_y_la_referencia_original(self):
        import generar_frecuencies_desde_markers as standalone

        self.ejecutar()
        salida = self.tmp / "standalone.csv"

        with contextlib.redirect_stdout(io.StringIO()):
            codigo = standalone.main(
                ["--markers-dir", str(self.actual / "markers"), "--output", str(salida)]
            )

        self.assertEqual(codigo, 0)
        self.assertEqual(salida.read_bytes(), (self.actual / m.NOMBRE_FRECUENCIAS).read_bytes())
        self.assertEqual(self.filas_frecuencias(self.actual),
                         referencia_original(self.actual / "markers"))


class TestFrecuenciasOnlineSinVersionado(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.inv = self.tmp / "inv.csv"
        escribir_inventario(self.inv, [
            ("0000758", "VODAFONE ESPAÑA, S.A.U.", 41.321464, 2.014425),
            ("999", "OP", 41.0, 2.0),
        ])

    def test_csv_junto_a_los_demas_y_resumen_online(self):
        abridor = Abridor(RespuestaFalsa(json_bytes(MARKER_REAL)), RespuestaFalsa(b"[]"))
        resumen, _ = m.ejecutar_online(
            self.inv, self.tmp / "out", self.tmp / "out" / "_treball",
            abrir=abridor, limitador=m.Limitador(0.0), parametros={"timeout": 5, "reintentos": 0},
            dormir=lambda s: None, imprimir=lambda *_: None,
        )

        self.assertEqual(len(abridor.peticiones), 2)
        csv_path = self.tmp / "out" / m.NOMBRE_FRECUENCIAS
        with csv_path.open(encoding="utf-8-sig", newline="") as f:
            filas = list(csv.DictReader(f, delimiter=";"))
        self.assertEqual([f["CODI_FREQUENCIA"] for f in filas], ["BB-2402052"])
        self.assertEqual(resumen["frecuencias"]["filas"], 1)

        guardado = json.loads((self.tmp / "out" / "resum_online.json").read_text(encoding="utf-8"))
        self.assertEqual(guardado["frecuencias"]["filas"], 1)

    def test_parada_por_errores_no_genera_el_csv(self):
        resumen, _ = m.ejecutar_online(
            self.inv, self.tmp / "out", self.tmp / "out" / "_treball",
            abrir=Abridor(por_defecto=http_error(403)), limitador=m.Limitador(0.0),
            parametros={"timeout": 5, "reintentos": 0}, max_errores_consecutivos=1,
            dormir=lambda s: None, imprimir=lambda *_: None,
        )

        self.assertIsNone(resumen)
        self.assertFalse((self.tmp / "out" / m.NOMBRE_FRECUENCIAS).exists())


if __name__ == "__main__":
    unittest.main()
