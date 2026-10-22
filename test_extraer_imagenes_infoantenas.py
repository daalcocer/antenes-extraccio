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


# ---------------------------------------------------------------------------
# Fase 2: modo online (sin red real; el cliente HTTP está simulado)
# ---------------------------------------------------------------------------

def cabeceras(**valores):
    mensaje = email.message.Message()
    for nombre, valor in valores.items():
        mensaje[nombre.replace("_", "-")] = valor
    return mensaje


class RespuestaFalsa:
    def __init__(self, cuerpo, status=200, content_type="application/json; charset=utf-8"):
        self._cuerpo = cuerpo
        self.status = status
        self.headers = cabeceras(Content_Type=content_type)

    def read(self):
        return self._cuerpo

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
        for opcion in (["--resume"], ["--maxim", "3"], ["--gis-id", "1"]):
            with self.subTest(opcion), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    m.main(opcion)


if __name__ == "__main__":
    unittest.main()
