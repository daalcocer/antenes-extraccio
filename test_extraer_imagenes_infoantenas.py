# -*- coding: utf-8 -*-

import csv
import json
import tempfile
import unittest
from pathlib import Path

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


def marker(codigo, operador, lat, lon, imagenes):
    return [[
        [codigo],
        [operador, "1", "1"],
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
            self.assertEqual(
                imagenes[0]["URL_ORIGEN"],
                "https://antenasmoviles.es/archive/antenas/1729885461_6fbc2e6031932d57e56c.jpg",
            )
            self.assertEqual(imagenes[0]["OPERADOR_COINCIDE"], "SI")


if __name__ == "__main__":
    unittest.main()
