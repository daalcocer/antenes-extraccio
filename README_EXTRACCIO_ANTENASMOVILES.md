# Extracció d'Antenes Mòbils (antenasmoviles.es) — flux vigent

Documentació del flux **vigent** que genera `IMATGES_INFOANTENAS.csv` i
`FREQUENCIES_ANTENASMOVILES.csv`. Està escrita només a partir del codi actual
del repositori. Quan un detall no es pot confirmar en el codi, s'indica com
**no confirmat**.

## Índex

1. [Propòsit](#1-propòsit)
2. [Script vigent](#2-script-vigent)
3. [Entrada](#3-entrada)
4. [Consulta a Antenas Móviles](#4-consulta-a-antenas-móviles)
5. [Estructura de versionat](#5-estructura-de-versionat)
6. [Generació de IMATGES_INFOANTENAS.csv](#6-generació-de-imatges_infoantenascsv)
7. [Generació de FREQUENCIES_ANTENASMOVILES.csv](#7-generació-de-frequencies_antenasmovilescsv)
8. [Regeneració offline de freqüències](#8-regeneració-offline-de-freqüències)
9. [Reintents i escriptures atòmiques](#9-reintents-i-escriptures-atòmiques)
10. [Sortides i controls](#10-sortides-i-controls)
11. [Validació actual](#11-validació-actual)
12. [Flux resumit](#12-flux-resumit)
13. [Scripts antics / fora del flux vigent](#13-scripts-antics--fora-del-flux-vigent)
14. [Execució](#14-execució)
15. [Punts no confirmats](#15-punts-no-confirmats)

---

## 1. Propòsit

El flux parteix de l'**inventari oficial d'Infoantenas** (`inventari_infoantenas.csv`)
i utilitza cada `GIS_ID` de l'inventari per consultar `https://antenasmoviles.es/v1/markers`.
Les respostes es guarden com a JSON, un per `GIS_ID`, i d'aquests mateixos JSON
se'n deriven:

- `IMATGES_INFOANTENAS.csv`: relació `GIS_ID` ↔ imatge, amb la imatge completa
  descarregada i la miniatura generada, i amb el `CODI_OPERADOR` d'Antenas Móviles
  de cada fila.
- `FREQUENCIES_ANTENASMOVILES.csv`: dades base de les freqüències de cada marker.
- `CONTROL_GIS_ID.csv`: control del creuament entre l'inventari i Antenas Móviles.

Tot es genera dins d'una **versió** (`nueva/`) que només es publica com a
`actual/` si supera la validació.

## 2. Script vigent

**[extraer_imagenes_infoantenas.py](extraer_imagenes_infoantenas.py)** és l'script
principal. Fa, en un sol procés:

1. Llegeix l'inventari i selecciona els `GIS_ID`.
2. Consulta `/v1/markers` per `GIS_ID` i en desa la resposta (reprenable).
3. Creua l'inventari amb els markers (`CONTROL_GIS_ID.csv`, `IMATGES_INFOANTENAS.csv`).
4. Descarrega les imatges completes i en genera les miniatures.
5. Genera `FREQUENCIES_ANTENASMOVILES.csv` a partir dels JSON ja desats.
6. Valida la versió i, si és vàlida, la promou (`nueva` → `actual`).

El mode de treball vigent és el **versionat**, activat amb `--versiones-dir`
(vegeu [§5](#5-estructura-de-versionat)).

Altres fitxers relacionats:

| Fitxer | Responsabilitat |
|---|---|
| [extraer_imagenes_infoantenas.py](extraer_imagenes_infoantenas.py) | Flux complet descrit aquí. |
| [generar_frecuencies_desde_markers.py](generar_frecuencies_desde_markers.py) | Embolcall CLI per regenerar només les freqüències a partir d'un directori `markers/` (vegeu [§8](#8-regeneració-offline-de-freqüències)). |
| [test_extraer_imagenes_infoantenas.py](test_extraer_imagenes_infoantenas.py) | Tests `unittest` del flux (HTTP simulat). |
| [requirements.txt](requirements.txt) | Conté `Pillow>=10`. |

Dependències: Pillow és obligatòria per descarregar imatges (sense Pillow l'script
llança un error que indica instal·lar-lo amb `python -m pip install -r requirements.txt`).
`truststore` és opcional: si està instal·lat s'usa el magatzem de certificats del
sistema; si no, el contexte SSL estàndard.

## 3. Entrada

### Fitxer d'inventari

- Argument `--inventario`. Valor per defecte al codi (`DEFAULT_INVENTARIO`):
  `infoantenas/01_DADES_FINALS/inventari_infoantenas.csv`.
- El `.gitignore` exclou `infoantenas/01_DADES_FINALS/`, per tant aquest fitxer
  **no està versionat a git**.
- Es llegeix com a CSV amb separador `;` i codificació `utf-8-sig`.
- El nom `inventari_infoantenas.csv` és el que escriu
  `infoantenas/03_CODI_I_REEXECUCIO/scripts/preparar_inventari_infoantenas.py`.
  El detall d'aquell flux queda fora d'aquest README.

### Columnes obligatòries de l'inventari

Si en falta alguna, l'script falla amb `ValueError`.

| Columna | Ús |
|---|---|
| `properties_Gis_ID` | **És el `GIS_ID`**: es llegeix com a text literal (només `strip()`), sense treure zeros ni normalitzar. |
| `properties_Gis_Codigo` | S'hi dedueix l'operador amb el format `"OPERADOR - GIS_ID"`. |
| `geometry_latitud` | Coordenades de l'inventari (per a `DISTANCIA_M`). |
| `geometry_longitud` | Idem. |

Regles de lectura:

- Les files sense `GIS_ID` s'ignoren i es compten.
- Un `GIS_ID` amb caràcters de control provoca `ValueError`.
- Les files es **agrupen per `GIS_ID`**, en ordre d'aparició: un `GIS_ID` repetit a
  l'inventari es consulta una sola vegada.

### Selecció dels `GIS_ID` a consultar

Per defecte, **tots** els `GIS_ID` únics de l'inventari, en ordre d'aparició.
Es pot restringir amb:

| Argument | Efecte |
|---|---|
| `--gis-id ID` (repetible) | Només aquests `GIS_ID`, en l'ordre indicat i sense duplicats. Si algun no és a l'inventari → `ValueError`. |
| `--maxim N` | Només els primers N de la selecció anterior. |

Una selecció parcial no es promou amb `--versiones-dir` llevat que s'afegeixi
`--permitir-seleccion-parcial` (motiu `SELECCION_PARCIAL`; pensat «només per a proves»).

## 4. Consulta a Antenas Móviles

- Petició `GET https://antenasmoviles.es/v1/markers?id=<GIS_ID>`, amb el `GIS_ID`
  literal codificat a la URL (`urllib.parse.urlencode`).
- Capçaleres enviades: `User-Agent: Mozilla/5.0 (compatible; SITEP-Antenas/1.0)`,
  `Accept: application/json`, `Accept-Encoding: identity`.
- **Una petició per `GIS_ID`**, seqüencial, amb un interval mínim entre inicis de
  petició de `60 / --peticions-per-minut` s (per defecte 10 peticions/minut).
- `--timeout` (segons, per defecte 60) i `--reintents` (per defecte 3, reintents
  addicionals per `GIS_ID`).

### Classificació de la resposta

Només es consideren vàlides les respostes HTTP 200 amb cos JSON que sigui una **llista**.

| Estat (`log_markers.csv`) | Significat | Es desa a `markers/` |
|---|---|---|
| `RESPUESTA_CON_MARKER` | Llista amb elements interpretables. | Sí |
| `JSON_VACIO` | Llista buida (`[]`). | Sí |
| `SIN_MARKER_COMPATIBLE` | Llista sense elements interpretables. | Sí |
| `YA_EXISTENTE` | Ja hi havia un JSON vàlid desat; no es torna a consultar. | (ja existia) |
| `HTTP_NO_ENCONTRADO` | HTTP 404 o 410. | No; **no** compta com a error tècnic |
| `ESTRUCTURA_INESPERADA`, `JSON_INVALIDO`, `HTTP_NO_RECUPERABLE`, `ERROR_HTTP_RECUPERABLE`, `ERROR_RED` | Errors tècnics. | No |

### Un JSON per `GIS_ID`

Ruta: `<versió>/markers/<GIS_ID codificat>_<sha12>.json`, on el nom és
`urllib.parse.quote(gis_id, safe="")` (màx. 160 caràcters) + `_` + els 12 primers
caràcters del SHA-256 del `GIS_ID` (funció `nombre_fichero_marker`).

Estructura del fitxer (claus confirmades a `guardar_respuesta`):

| Clau | Contingut |
|---|---|
| `GIS_ID` | `GIS_ID` consultat. |
| `HTTP_STATUS` | Estat HTTP (ha de ser 200 per considerar-lo vàlid). |
| `CONSULTADO_EN` | Moment de la consulta (ISO, segons). |
| `URL` | URL consultada. |
| `CONTENT_TYPE` | `Content-Type` de la resposta. |
| `RESPUESTA` | La llista JSON crua de `/v1/markers`. |

Un fitxer només es considera vàlid (`leer_respuesta_guardada`) si és un objecte
JSON, el seu `GIS_ID` coincideix, `HTTP_STATUS` és 200 i `RESPUESTA` és una llista.

### Contingut de `RESPUESTA`

`RESPUESTA` és una llista d'**elements**; cada element és una llista. El codi
n'utilitza aquestes posicions (documentades a `parsear_marker`):

| Posició | Contingut |
|---|---|
| `[0]` | `[codi]` |
| `[1]` | `[empresa, codi_operador, <tercer valor sense ús>]` (`codi_operador` és el `CODI_OPERADOR` de `IMATGES_INFOANTENAS.csv`) |
| `[2]` | `[direcció, latitud, longitud, altura]` |
| `[3]` | **Freqüències** (llista de `freq[]`, vegeu [§7](#7-generació-de-frequencies_antenasmovilescsv)) |
| `[4]` | **Imatges**: diccionaris amb `id`, `fichero`, `autor`, `fecha_foto`, `fecha_subida`, `old`, `desmantelada`, `lat_subida`, `lon_subida`, `asociadas` |

> **El mateix JSON conté tres coses:** les dades del marker (`[0]`–`[2]`), les
> freqüències (`[3]`) i les imatges (`[4]`). Per això les freqüències s'obtenen
> sense cap segona crida HTTP.

Els elements més enllà de la posició `[4]` (a les mostres hi ha dos valors més)
no els utilitza el codi; el seu significat **no està confirmat**.

## 5. Estructura de versionat

S'activa amb `--versiones-dir RAIZ` (no té valor per defecte). Implica mode
`online` i descàrrega d'imatges, i és **incompatible** amb `--output-dir`,
`--trabajo-dir` i `--imagenes-dir`.

```
RAIZ/
├── nueva/      versió en construcció (tot es genera aquí)
├── actual/     última versió vàlida publicada
└── anterior/   còpia de la versió prèvia
```

| Element | Funció |
|---|---|
| `nueva/` | Hi va **tot**: `markers/`, `full/`, `thumb/`, els CSV, els logs, `run.json` i `resum.json`. `actual/` i `anterior/` no es toquen durant l'execució. |
| `actual/` | Última versió que va superar la validació. |
| `anterior/` | Versió que hi havia abans d'`actual/`. |
| `_COMPLETA` | Marcador JSON (`finalizado_en`, `inventario_sha256`, `resultado`). És **l'últim** fitxer que s'escriu i només si no hi ha cap motiu de rebuig. |
| `run.json` | Estat de l'execució: `formato` (1), `inicio`, `inventario`, `inventario_sha256`, `parametros`, `reanudaciones`; en acabar s'hi afegeixen `fin`, `resultado`, `promocionable` i `motivos_no_promocionable`. |

Les rutes dels CSV (`full/<f>`, `thumb/<f>`) són **relatives a la versió**, per
tant continuen sent vàlides després de renombrar `nueva` → `actual`.

### Seqüència d'una execució (`ejecutar_version`)

1. `recuperar_versiones`: repara una interrupció durant una promoció anterior
   (només actua si falta `actual/`).
2. `preparar_nueva`: prepara `nueva/`.
3. `ejecutar_online`: consulta, creua, descarrega imatges i genera tots els CSV.
4. `evaluar_version`: llista de motius pels quals la versió **no** és promocionable.
5. Escriu `resum.json` i actualitza `run.json`.
6. Si no hi ha motius: crea `_COMPLETA` i `promover_nueva`
   (elimina `anterior/`, `actual/` → `anterior/`, `nueva/` → `actual/`).

### Comportament sense `--resume`

- Si `nueva/` existeix i és **incompleta**, s'elimina i es comença de zero.
- Si `nueva/` és **completa** (té `_COMPLETA`) i pendent de promoure, s'avorta
  amb error d'estat ambigu sense esborrar res; cal usar `--promover-nueva` o
  esborrar-la manualment.
- Es torna a consultar i descarregar **tot**; `actual/` no s'usa mai com a font
  incremental.

### Reanudació (`--resume`)

Només serveix per continuar **una execució interrompida**:

- Requereix que existeixi `nueva/` (si no, `ValueError`) i en treu `_COMPLETA`.
- Requereix el `run.json` previ amb `formato` 1 i el **mateix SHA-256 de
  l'inventari**; si no, no es reprèn.
- Els paràmetres que canvien el contingut han de coincidir amb els de l'execució
  original (`CLAVES_REANUDACION`): `gis_ids`, `maxim`, `maxim_imatges`,
  `miniatura_max`, `descargar_imagenes`. Els ritmes, timeouts i reintents es poden canviar.
- Es reutilitzen les respostes JSON vàlides ja desades i només es consulten les pendents.
- Les imatges: vegeu [§6](#6-generació-de-imatges_infoantenascsv).
- Cada reanudació s'anota a `reanudaciones` de `run.json`.

### Quan es promou i què passa si no

La versió es promou **només si la llista de motius és buida**. Si no ho és:

- no es crea `_COMPLETA`;
- `nueva/` es conserva per a diagnòstic o `--resume`;
- `actual/` i `anterior/` queden intactes;
- el procés acaba amb un codi de sortida.

Motius de rebuig (`evaluar_version` i `verificar_version_en_disco`):

| Motiu | Origen |
|---|---|
| `CONSULTA_DETENIDA_POR_ERRORES` | Parada per `--max-errores-consecutivos`. |
| `ERRORES_MARKERS:n` | n consultes amb error tècnic. |
| `SIN_DESCARGA_DE_IMAGENES` | No hi ha bloc de descàrrega. |
| `ERRORES_IMAGEN:n`, `IMAGENES_PENDIENTES:n` | Imatges amb error / no intentades. |
| `BLOQUEO_DETECTADO`, `DESCARGA_DETENIDA_POR_ERRORES` | Descàrrega aturada. |
| `SELECCION_PARCIAL` | Selecció parcial sense `--permitir-seleccion-parcial`. |
| `FALTA_<fitxer>` | Falta algun fitxer de `ARCHIVOS_VERSION`. |
| `FICHEROS_PART:n` | Queden fitxers `*.part`. |
| `CONTROL_GIS_ID_INVALIDO`, `CONTROL_GIS_ID_INCONSISTENTE` | `CONTROL_GIS_ID.csv` amb capçalera errònia o nombre de files incoherent. |
| `IMATGES_INFOANTENAS_INVALIDO`, `IMATGES_INFOANTENAS_INCONSISTENTE` | Idem per a `IMATGES_INFOANTENAS.csv`. |
| `FRECUENCIAS_INVALIDO`, `FRECUENCIAS_INCONSISTENTE` | Idem per a `FREQUENCIES_ANTENASMOVILES.csv`. |
| `RUTAS_NO_RELATIVAS_O_VACIAS:n`, `ORIGINALES_INVALIDOS:n`, `MINIATURAS_INVALIDAS:n` | Rutes o imatges `full/`/`thumb/` invàlides. |

El codi indica explícitament que **no** són motius de rebuig: HTTP 200 amb `[]`,
un `GIS_ID` sense imatges, `old=1`, `desmantelada=1`, distància elevada o
operador diferent (són dades de control).

`ARCHIVOS_VERSION` (fitxers exigits): `IMATGES_INFOANTENAS.csv`,
`CONTROL_GIS_ID.csv`, `FREQUENCIES_ANTENASMOVILES.csv`, `log_markers.csv`,
`log_imatges.csv`, `run.json`, `resum.json`.

### Codis de sortida (mode versionat)

| Codi | Significat |
|---|---|
| 0 | Versió promoguda |
| 2 | Errors tècnics (`ERRORES_*`) |
| 3 | Consulta o descàrrega aturada (`CONSULTA_DETENIDA…`, `DESCARGA_DETENIDA…`) |
| 4 | Bloqueig (`BLOQUEO…`) |
| 5 | Un altre motiu |
| 6 | Estat de versions ambigu (`EstadoVersionesAmbiguo`) |
| 130 | Interrompuda amb Ctrl+C (el missatge indica relançar amb `--resume`) |
| 1 | Qualsevol altra excepció no controlada |

### `--promover-nueva`

Amb `--versiones-dir`, sense consultar res: recupera una interrupció prèvia i
promou `nueva/` si conté `_COMPLETA`. No admet `--resume`, `--gis-id` ni `--maxim`.

## 6. Generació de IMATGES_INFOANTENAS.csv

### De quin origen surt

Dels **JSON de markers desats** (no directament de la resposta HTTP):

1. `consultar_pendientes` desa cada resposta a `markers/`.
2. `leer_markers_dir` torna a llegir aquests fitxers (validats amb `leer_respuesta_guardada`).
3. `parsear_marker` interpreta cada resposta; `cruzar` genera les files.

### Relació amb el `GIS_ID`

El CSV té **una fila per relació** (`GIS_ID`, imatge): per a cada `GIS_ID`
consultat, cada element de la resposta i cada imatge de `elemento[4]` amb
`fichero`. Les imatges sense `fichero` es descarten i es reporten
(`IMAGENES_SIN_FICHERO_DESCARTADAS`). `GIS_ID` és el de l'inventari consultat;
`CODIGO_ANTENASMOVILES` és el codi de l'element de la resposta i `CODI_OPERADOR`
el codi d'operador d'Antenas Móviles d'aquest mateix element (`elemento[1][1]`).

Cada element processat del marker conté el seu propi `codi_operador` i les
files d'imatge hereten aquest valor. Per això un mateix `GIS_ID` pot produir
files amb `CODI_OPERADOR` diferents; la parella `GIS_ID` + `CODI_OPERADOR`
permet resoldre l'antena corresponent. `NOMBRE_FICHERO_ORIGEN` només serveix per deduplicar la
descàrrega física i no identifica l'antena.

### Columnes (`CAMPOS_IMAGENES` + `CAMPOS_DESCARGA`)

| Columna | Origen |
|---|---|
| `GIS_ID` | `GIS_ID` de l'inventari consultat |
| `CODIGO_ANTENASMOVILES` | `elemento[0][0]` de la resposta |
| `CODI_OPERADOR` | `elemento[1][1]` de la resposta (text; buit només si el marker no el porta) |
| `ID_IMAGEN_ORIGEN` | imatge `id` |
| `NOMBRE_FICHERO_ORIGEN` | imatge `fichero` |
| `AUTOR` | imatge `autor` |
| `FECHA_FOTO` | imatge `fecha_foto` |
| `FECHA_SUBIDA` | imatge `fecha_subida` |
| `URL_ORIGEN` | `https://antenasmoviles.es/archive/antenas/` + nom de fitxer codificat |
| `OLD` | imatge `old` |
| `DESMANTELADA` | imatge `desmantelada` |
| `LAT_SUBIDA` | imatge `lat_subida` |
| `LON_SUBIDA` | imatge `lon_subida` |
| `OPERADOR_COINCIDE` | `SI` / `NO` / buit (no comparable), de l'element |
| `DISTANCIA_M` | distància en metres amb 3 decimals, o buit |
| `RUTA_IMAGEN` | `full/<nom local>` si la descàrrega és `OK`; si no, buit |
| `RUTA_MINIATURA` | `thumb/<nom local>` si la descàrrega és `OK`; si no, buit |
| `ESTADO_DESCARGA` | estat de la descàrrega (vegeu sota) |

El CSV es codifica amb `utf-8-sig` i separador `;`. L'operador de l'inventari es
dedueix de `properties_Gis_Codigo` i es compara per igualtat exacta amb l'empresa
del marker; no es defineix cap llindar de distància.

`CODI_OPERADOR` s'obté durant l'extracció (a `cruzar`, a partir del resultat de
`parsear_marker`); no cal cap altra consulta a `/v1/markers`. Com que
`verificar_version_en_disco` exigeix la capçalera exacta, una versió generada
abans d'aquest canvi (sense la columna) es marca com `IMATGES_INFOANTENAS_INVALIDO`
fins que es regenera.

### Ús des de FME

FME rep ja `GIS_ID` + `CODI_OPERADOR` a `IMATGES_INFOANTENAS.csv`. **No necessita
llegir ni interpretar els JSON de `markers/`** per carregar les imatges: aquests
JSON són només part del procés intern d'extracció.

Per resoldre l'antena concreta, FME ha de mapar `CODI_OPERADOR` a l'`OPERADOR_ID`
intern i després resoldre:

```
GIS_ID + OPERADOR_ID -> TELCO_ANTENES_MOBILS.ID -> ANTENA_ID
```

Mapatge de referència (queda a FME, no al codi d'aquest repositori):

| `CODI_OPERADOR` | Operador | `OPERADOR_ID` |
|---|---|---|
| `1` | Vodafone | 1 |
| `3` | Orange | 2 |
| `7` | Telefónica | 3 |
| `4` | Xfera | 4 |
| `22` | Digi | 5 |

### Quan es descarreguen les imatges full

Dins de `generar_salidas`, **després** del creuament i **abans** d'escriure els
CSV: el callback `procesar_imagenes` rep les files i descarrega cada **fitxer
físic una sola vegada** (deduplicat per `NOMBRE_FICHERO_ORIGEN`), encara que
l'usin diversos `GIS_ID`.

- Seqüencial, a `--imatges-per-minut` (per defecte 5/minut) i amb
  `--imatges-reintents` reintents addicionals (per defecte 2).
- Es descarrega a `full/<nom>.part` i només passa al nom definitiu si és vàlida:
  HTTP 200, `Content-Type` d'imatge, cos complet i no buit, màxim 50 MiB
  (`MAX_BYTES_IMAGEN`) i descodificable amb Pillow.
- Les imatges `old`/`desmantelada` i la distància/operador **no** exclouen cap descàrrega.
- `--maxim-imatges N` limita a N fitxers únics; la resta queda `PENDIENTE`.
- `--parar-ante-bloqueo` atura la descàrrega al primer HTTP 429/403 o desafiament
  de Cloudflare.
- Nom local (`nombre_local_seguro`): el nom original si coincideix amb
  `[A-Za-z0-9_-][A-Za-z0-9._-]*`; si no, es codifica i s'hi afegeix un hash.

Valors de `ESTADO_DESCARGA`: `OK`, `HTTP_ERROR`, `CONTENT_TYPE_INVALIDO`, `VACIA`,
`IMAGEN_CORRUPTA`, `ERROR_RED`, `PENDIENTE`.

### Com es generen les miniatures

**Mai es descarreguen**: es generen localment amb Pillow a partir de la imatge
completa (`generar_miniatura`).

- El costat màxim és `--miniatura-max` (per defecte 300 px).
- Conserva proporció i format; no retalla ni amplia.
- Aplica l'orientació EXIF només a la miniatura.
- JPEG: qualitat 85 amb `optimize`; PNG: `optimize`.
- Es desa a `thumb/<nom local>`.

### Reanudació de descàrregues ja existents

Amb `--resume`, `full/` i `thumb/` es conserven (sense `--resume` s'esborren) i
per a cada fitxer (`procesar_imagen`):

| Situació | Acció (`log_imatges.csv`) |
|---|---|
| Original vàlid i miniatura vàlida | `REUTILIZADA` (no es descarrega res) |
| Original vàlid, miniatura absent o invàlida | `SOLO_MINIATURA` (es regenera) |
| Original absent o corrupte | `DESCARGA` (es torna a descarregar) |

## 7. Generació de FREQUENCIES_ANTENASMOVILES.csv

- Es genera dels **mateixos JSON de `<versió>/markers/`**.
- **No fa cap segona crida HTTP** a `/v1/markers` i **no torna a descarregar
  imatges** (llegeix només fitxers locals).
- **No modifica** el comportament de les imatges ni el de `IMATGES_INFOANTENAS.csv`.
- Forma part del mateix flux versionat: es genera dins de `nueva/`.
- Moment exacte: a `ejecutar_online`, **just després de `generar_salidas`** i
  **abans** d'`evaluar_version`/promoció. La funció és `generar_frecuencias`.
- Llegeix la resposta crua (no usa `parsear_marker`): itera la selecció amb
  `nombre_fichero_marker`, valida amb `leer_respuesta_guardada` i extreu
  `elemento[3]` amb `extraer_frecuencias`.
- Escriu amb `escribir_csv_atomico` (`;` i `utf-8-sig`).
- Si la consulta s'atura per errors (`ejecutar_online` retorna sense resum), no es genera.
- El mode `--modo offline` **no** genera aquest CSV (només `ejecutar_online` el crida).

### Columnes (`CAMPOS_FRECUENCIAS`), en aquest ordre

| # | Columna | Origen a `freq[]` |
|---|---|---|
| 1 | `GIS_ID` | `freq[1]` |
| 2 | `CODI_FREQUENCIA` | `freq[0]` |
| 3 | `FREQUENCIA` | `freq[2]` |
| 4 | `ESTAT` | `freq[3]` |
| 5 | `PRIMERA_VEGADA_VISTA` | `freq[4]` |
| 6 | `ULTIMA_VEGADA_VISTA` | `freq[5]` |
| 7 | `ULTIMA_VEGADA_ACTUALITZADA` | `freq[6]` |
| 8 | `CODI_OPERADOR` | `freq[7]` |

> El `GIS_ID` d'aquest CSV és `freq[1]` (el que ve a la resposta), **no** el
> `GIS_ID` consultat; el codi no els compara.

### Regles de conversió

- Tots els camps es desen com a text amb `strip()`, **excepte `ESTAT`**, que és
  `str(freq[3])` sense `strip()`: **es conserva com a `"False"` / `"True"`**.
- **No es transformen les dates** (es mantenen tal com arriben, p. ex. `24/09/2024`).
- **No es calculen bandes ni tecnologies.**
- **No es genera directament l'històric visual.** Aquest CSV conté les dades base
  necessàries per reconstruir-lo posteriorment.
- No s'afegeixen columnes ni camps deduïts.

### Entrades invàlides (no trenquen l'execució)

| Cas | Tractament |
|---|---|
| `RESPUESTA` buida, o element amb `elemento[3]` buit | 0 files, sense error |
| Element que no és llista, té menys de 4 posicions o `elemento[3]` no és llista | Compta a `elementos_sin_bloque_frecuencias` |
| Freqüència que no és llista o té menys de 8 posicions | Compta a `frecuencias_mal_formadas` |
| Freqüència amb més de 8 posicions | S'accepta; només s'usen les 8 primeres |
| `GIS_ID` de la selecció sense JSON vàlid (p. ex. `HTTP_NO_ENCONTRADO`) | Compta a `markers_no_disponibles` |

### Ordre i duplicats

- Ordre determinista: l'**ordre de la selecció** i, dins de cada resposta,
  l'ordre original.
- **No s'eliminen duplicats**: es detecten i es reporten a `resum.json`.
- Un `CODI_FREQUENCIA` repetit **no impedeix** la promoció.

## 8. Regeneració offline de freqüències

[generar_frecuencies_desde_markers.py](generar_frecuencies_desde_markers.py) és
ara un **embolcall CLI fi** sobre la mateixa lògica integrada
(`extraer_imagenes_infoantenas.generar_frecuencias`); ja no té una implementació
pròpia. No fa peticions HTTP.

Arguments (`argparse`):

| Argument | Valor per defecte (al codi) |
|---|---|
| `--markers-dir` | `C:\datos\antenas\actual\markers` |
| `--output` | `C:\datos\antenas\actual\FREQUENCIES_ANTENASMOVILES.csv` |

Funcionament:

1. Si `--markers-dir` no és un directori: missatge d'error i codi 1.
2. `listar_gis_ids_guardados` obté els `GIS_ID` dels `*.json` del directori
   (llegint-ne la clau `GIS_ID`), en **ordre de nom de fitxer**.
3. `generar_frecuencias` genera el CSV.
4. Mostra `MARKERS PROCESADOS`, `FILAS DE FRECUENCIAS`, `ERRORES`, `CSV GENERADO`
   i el bloc de resum en JSON.

Notes:

- No actualitza el `resum.json` de cap versió (només escriu el CSV).
- **Ordre de les files i igualtat byte a byte.** El flux integrat segueix
  l'**ordre de la selecció de `GIS_ID` de l'inventari**; l'script independent pot
  seguir l'**ordre dels fitxers de `markers/`** (per nom de fitxer). Tots dos han de
  produir el **mateix conjunt de registres**, però **no es garanteix** que l'ordre
  de les files ni el fitxer byte a byte siguin sempre idèntics, tret que l'ordre
  d'entrada coincideixi.
- Sobre les dades reals el CSV generat va ser idèntic byte a byte al CSV ja
  validat (vegeu [§11](#11-validació-actual)); és un resultat observat en aquella
  extracció, no una garantia general.

Exemple (rutes de `<RAIZ>` a adaptar):

```
python generar_frecuencies_desde_markers.py --markers-dir <RAIZ>\actual\markers --output <RAIZ>\actual\FREQUENCIES_ANTENASMOVILES.csv
```

Sense arguments s'usen els valors per defecte de la taula.

## 9. Reintents i escriptures atòmiques

Tot el renombrat/substitució atòmica passa per **una única funció**: `_renombrar`.

### `_renombrar(origen, destino, intentos, espera)`

| Aspecte | Valor (al codi) |
|---|---|
| Operació | `os.replace(origen, destino)` |
| Reintenta | **Només `PermissionError`** (a Windows, bloqueig transitori d'un fitxer acabat d'escriure) |
| Intents | `RENOMBRADO_INTENTOS = 6` |
| Espera | `RENOMBRADO_ESPERA_S = 0.1` s, exponencial: `espera × 2^(intent−1)` → 0,1 / 0,2 / 0,4 / 0,8 / 1,6 s (3,1 s en total) |
| Si s'esgoten els intents | Es propaga el `PermissionError` |
| Altres errors | Es propaguen al primer intent, sense reintentar ni esperar |
| `intentos < 1` | `ValueError` |

### Qui la utilitza

| Funció | Paper |
|---|---|
| `escribir_csv_atomico` | Escriu `<fitxer>.tmp` i després `_renombrar` cap al destí. |
| `escribir_json_atomico` | Escriu el temporal (`.tmp` per defecte; el paràmetre `sufijo` permet `.part`, que usen `run.json`, `_COMPLETA` i els JSON de `markers/`) i després `_renombrar`. |
| `procesar_imagen` | Publica `full/` i `thumb/` des de `*.part` amb `_renombrar` (descàrrega nova i regeneració de miniatura). Els `*.part` s'esborren en un `finally`. |
| `promover_nueva`, `recuperar_versiones`, `ejecutar_version` | Reben `_renombrar` com a funció de renombrat de directoris per defecte. |

### Semàntica conservada

1. Primer s'escriu el temporal completament.
2. Només després se substitueix el destí.
3. Si la substitució falla definitivament, **el destí anterior queda intacte** i
   no es crea cap fitxer final parcial; el temporal queda al disc (un `*.part` dins
   d'una versió la fa no promocionable amb `FICHEROS_PART:n`).

## 10. Sortides i controls

Dins de `<RAIZ>/nueva/` (després `actual/`):

| Fitxer / directori | Contingut |
|---|---|
| `IMATGES_INFOANTENAS.csv` | Relació `GIS_ID` ↔ imatge (vegeu [§6](#6-generació-de-imatges_infoantenascsv)). |
| `FREQUENCIES_ANTENASMOVILES.csv` | Freqüències base (vegeu [§7](#7-generació-de-frequencies_antenasmovilescsv)). |
| `CONTROL_GIS_ID.csv` | Una fila per `GIS_ID` de la selecció. |
| `resum.json` | Resum de l'execució i bloc `version`. |
| `run.json` | Estat i paràmetres de l'execució. |
| `log_markers.csv` | Una fila per `GIS_ID` tractat en aquesta invocació. |
| `log_imatges.csv` | Una fila per fitxer d'imatge únic. |
| `markers/` | Un JSON per `GIS_ID` (vegeu [§4](#4-consulta-a-antenas-móviles)). |
| `full/` | Imatges completes descarregades. |
| `thumb/` | Miniatures generades localment. |
| `_COMPLETA` | Marcador de versió completa. |

`log_markers.csv` i `log_imatges.csv` es **reescriuen a cada invocació**; després
d'un `--resume` només contenen les files d'aquesta invocació.

### `CONTROL_GIS_ID.csv`

Columnes (`CAMPOS_CONTROL`): `GIS_ID`, `ENCONTRADO_ANTENASMOVILES` (`SI`/`NO`),
`CODIGO_ANTENASMOVILES`, `OPERADOR_ANTENASMOVILES`, `OPERADOR_COINCIDE`,
`DISTANCIA_M`, `NUM_IMAGENES`, `OBSERVACIONES`.

`OBSERVACIONES` és una llista separada per `|` amb aquests codis (segons `cruzar`):
`VARIOS_OPERADORES_INFOANTENAS`, `COORDENADAS_DISTINTAS_EN_INVENTARIO`,
`COORDENADAS_INVENTARIO_INVALIDAS`, `OPERADOR_INVENTARIO_NO_DEDUCIBLE`,
`CONSULTA_<estat>`, `SIN_RESPUESTA_EN_MARKERS_RAW`, `MARKER_SIN_ELEMENTOS`,
`VARIOS_ELEMENTOS_EN_RESPUESTA`, `CODIGO_RESPUESTA_DISTINTO`,
`IMAGENES_SIN_FICHERO_DESCARTADAS`, `OPERADOR_NO_COINCIDE`,
`OPERADOR_NO_COMPARABLE`, `DISTANCIA_NO_CALCULABLE`.

### Columnes dels logs

- `log_markers.csv` (`CAMPOS_LOG`): `MOMENTO`, `GIS_ID`, `ESTADO`, `HTTP_STATUS`,
  `INTENTOS`, `BYTES`, `CONTENT_TYPE`, `CABECERAS`, `FICHERO`, `ERROR`, `MUESTRA_CUERPO`.
- `log_imatges.csv` (`CAMPOS_LOG_IMAGENES`): `MOMENTO`, `ID_IMAGEN_ORIGEN`,
  `NOMBRE_FICHERO`, `NOMBRE_LOCAL`, `URL_ORIGEN`, `ESTADO`, `ACCION`, `HTTP_STATUS`,
  `INTENTOS`, `CONTENT_TYPE`, `BYTES`, `FORMATO`, `ANCHO`, `ALTO`,
  `MINIATURA_ANCHO`, `MINIATURA_ALTO`, `CABECERAS`, `ERROR`.

### `resum.json`

Claus de primer nivell (flux online/versionat): `modo`, `generado_en`, `entradas`,
`inventario`, `markers_raw`, `cruce`, `imagenes`, `seleccion`, `consulta`,
`descarga`, `frecuencias` i `version`.

> La clau `markers_raw` es manté per compatibilitat de nom amb el mode offline,
> però en el flux online conté les estadístiques de lectura de `markers/`.

#### Bloc `frecuencias`

| Camp | Significat |
|---|---|
| `fichero` | Nom del CSV (`FREQUENCIES_ANTENASMOVILES.csv`). |
| `markers_procesados` | `GIS_ID` de la selecció amb JSON vàlid llegit. |
| `markers_no_disponibles` | `GIS_ID` sense JSON vàlid (absent o invàlid). |
| `filas` | Files escrites al CSV. |
| `frecuencias_mal_formadas` | Freqüències descartades per no ser llista o tenir < 8 posicions. |
| `elementos_sin_bloque_frecuencias` | Elements sense `elemento[3]` vàlid. |
| `filas_duplicadas_exactas` | Files idèntiques en els 8 camps a una anterior. |
| `codi_frequencia_distintos` | Nombre de `CODI_FREQUENCIA` diferents. |
| `codi_frequencia_repetidos` | `CODI_FREQUENCIA` que apareixen més d'una vegada. |
| `filas_con_codi_frequencia_repetido` | Files que tenen un `CODI_FREQUENCIA` repetit. |
| `codi_frequencia_repetidos_detalle` | Llista ordenada dels repetits (màxim 100). |

`verificar_version_en_disco` exigeix que la capçalera del CSV sigui `CAMPOS_FRECUENCIAS`
i que el nombre de files coincideixi amb `frecuencias.filas`.

#### Bloc `version`

`inventario_sha256`, `inicio`, `fin`, `gis_id_procesados`, `markers_encontrados`,
`markers_vacios`, `errores_markers`, `relaciones_imagen`, `imagenes_fisicas_unicas`,
`imagenes_descargadas_ok`, `imagenes_reutilizadas`, `miniaturas_generadas`,
`errores_imagen`, `imagenes_pendientes`, `senales_bloqueo`, `seleccion_parcial`,
`resultado` (`COMPLETA` o `NO_PROMOCIONABLE`), `promocionable` i
`motivos_no_promocionable`.

## 11. Validació actual

Resultats de la **extracció ja validada**, obtinguts en executar la lògica de
freqüències sobre els `markers/` d'aquella extracció:

| Mesura | Valor |
|---|---|
| Markers processats | 9338 |
| Files de freqüència | 45176 |
| Freqüències mal formades | 0 |
| Elements sense bloc de freqüències | 0 |
| Duplicats exactes | 0 |
| `CODI_FREQUENCIA` diferents | 45176 |
| `CODI_FREQUENCIA` repetits | 0 |

Notes:

- El CSV regenerat amb `generar_frecuencies_desde_markers.py` va resultar idèntic
  byte a byte al `FREQUENCIES_ANTENASMOVILES.csv` ja validat.
- El `resum.json` d'aquella extracció és anterior a la integració de les
  freqüències al flux i, per tant, **no conté el bloc `frecuencias`**; aquell CSV
  es va generar amb el script independent.
- Segons aquell mateix `resum.json`: 9338 `GIS_ID` únics a l'inventari i
  processats, 9337 amb elements i 1 amb resposta buida; 6779 relacions
  `GIS_ID`–imatge i 2888 imatges úniques descarregades, sense errors.
- Validació addicional amb dades reals de `CODI_OPERADOR`: s'ha generat un
  `IMATGES_INFOANTENAS.csv` temporal només a partir dels JSON existents de
  `markers/` (sense xarxa), amb 6779 relacions d'imatge, `CODI_OPERADOR` només
  amb els valors `1`, `3`, `4`, `7` i `22`, i 0 files amb `CODI_OPERADOR` buit.

Tests ([test_extraer_imagenes_infoantenas.py](test_extraer_imagenes_infoantenas.py)):

- Suite actual: **229 tests, OK.**
- Els tests no fan peticions de xarxa reals (HTTP simulat) i inclouen la
  cobertura de les freqüències, de les escriptures atòmiques amb reintents i del
  versionat.

## 12. Flux resumit

```
inventari_infoantenas.csv   (properties_Gis_ID = GIS_ID)
        |
        v
extraer_imagenes_infoantenas.py --versiones-dir <RAIZ>
        |
        v
GET /v1/markers?id=<GIS_ID>      (una petició per GIS_ID)
        |
        v
<RAIZ>/nueva/markers/            (un JSON per GIS_ID: marker + frequencies + imatges;
        |                         intern a l'extracció, no és entrada de FME)
        +--> CONTROL_GIS_ID.csv
        |
        +--> IMATGES_INFOANTENAS.csv   (inclou CODI_OPERADOR: FME rep GIS_ID + CODI_OPERADOR)
        |       +--> full/    (descàrrega, una vegada per fitxer físic)
        |       +--> thumb/   (generades localment)
        |
        +--> FREQUENCIES_ANTENASMOVILES.csv   (sense HTTP; sense descarregar imatges)
        |
        v
validació (evaluar_version + verificar_version_en_disco)
        |
        +-- amb motius --> nueva/ es conserva; actual/ i anterior/ intactes
        |
        v
_COMPLETA
        |
        v
promoció: anterior eliminada, actual -> anterior, nueva -> actual
```

## 13. Scripts antics / fora del flux vigent

> **Aquests scripts NO formen part del flux vigent. No s'han d'usar per generar
> `IMATGES_INFOANTENAS.csv` ni `FREQUENCIES_ANTENASMOVILES.csv`.**

| Script | Situació (comprovable al repositori) |
|---|---|
| [extraer_markers_antenas_v2.py](extraer_markers_antenas_v2.py) | Flux antic. Per defecte llegeix `salida_antenas/Antenas.csv` i consulta `/v1/markers` per `codigo_antena` (no per `GIS_ID` d'Infoantenas). Escriu a `salida_markers/` (`Detalle_antenas.csv`, `Frecuencias_antenas.csv`, `Imagenes_antenas.csv`, `markers_raw.jsonl`, `checkpoint_markers.json`, `errores_markers.csv`) i té un checkpoint propi. Només construeix URLs d'imatges; no les descarrega. Per defecte processa 10 antenes (`--limit`). |
| [extraer_markers_antenas_v2_old.py](extraer_markers_antenas_v2_old.py) | Variant del d'abans: la diferència amb `v2` és que no inclou `codigo_operador` a `Detalle_antenas.csv`. |

Evidència que no formen part del flux vigent:

- Cap script Python del repositori els importa ni els crida.
- El flux vigent no depèn de cap sortida seva.
- Les carpetes `salida_antenas/`, `salida_markers/` i `salida_radiaciones/` estan
  al `.gitignore` com a «Salidas antiguas de antenasmoviles».
- Tots dos van entrar al repositori al primer commit (`Inicialitzar repositori
  d'extracció de dades d'antenes`).

Vincle residual: el **mode offline** de `extraer_imagenes_infoantenas.py`
(`--modo offline`, per defecte) llegeix `--raw`, amb valor per defecte
`salida_markers/markers_raw.jsonl`, que és el format que escriu `v2`. És un mode
heretat: no descarrega imatges, no genera `FREQUENCIES_ANTENASMOVILES.csv` i no
usa versionat. **No és el flux vigent.**

No s'han revisat per a aquest README, i no formen part del flux descrit:
`filtrar_antenas_catalunya_auto.py` i `regenerar_detalle_desde_raw.py`.

## 14. Execució

Els noms d'arguments són els de l'`argparse` d'`extraer_imagenes_infoantenas.py`
i `generar_frecuencies_desde_markers.py`. `<RAIZ>` és el directori de versions
que triï l'usuari (no té valor per defecte).

### Instal·lació de dependències

```
python -m pip install -r requirements.txt
```

### Execució normal (versionada)

```
python extraer_imagenes_infoantenas.py --versiones-dir <RAIZ>
```

Usa l'inventari per defecte (`infoantenas/01_DADES_FINALS/inventari_infoantenas.csv`),
10 peticions/minut per a markers i 5 descàrregues/minut per a imatges. Opcions
que es poden afegir (totes existents a `argparse`): `--inventario`,
`--peticions-per-minut`, `--imatges-per-minut`, `--timeout`, `--reintents`,
`--imatges-reintents`, `--miniatura-max`, `--max-errores-consecutivos`,
`--parar-ante-bloqueo`.

### Reanudació d'una execució interrompida

```
python extraer_imagenes_infoantenas.py --versiones-dir <RAIZ> --resume
```

Requereix `<RAIZ>/nueva/` amb el seu `run.json`, el **mateix inventari** (SHA-256)
i els mateixos `--gis-id`, `--maxim`, `--maxim-imatges` i `--miniatura-max`.

### Promoure una `nueva/` completa pendent

```
python extraer_imagenes_infoantenas.py --versiones-dir <RAIZ> --promover-nueva
```

### Regenerar només les freqüències (offline, sense HTTP)

```
python generar_frecuencies_desde_markers.py --markers-dir <RAIZ>\actual\markers --output <RAIZ>\actual\FREQUENCIES_ANTENASMOVILES.csv
```

### Executar els tests

```
python -m unittest test_extraer_imagenes_infoantenas
```

## 15. Punts no confirmats

- El significat de les posicions `[5]` i `[6]` dels elements de `RESPUESTA` (a les
  mostres són un booleà i una llista buida): el codi no les utilitza.
- El significat funcional de `freq[1]` més enllà de ser la font de `GIS_ID` al CSV
  de freqüències; el codi no el compara amb el `GIS_ID` consultat.
- La causa exacta del `PermissionError` transitori a Windows (antivirus, indexador…):
  el codi i els tests només tracten el símptoma amb reintents.
- Quin dels dos fitxers `extraer_markers_antenas_v2.py` o
  `extraer_markers_antenas_v2_old.py` és cronològicament anterior: només es pot
  afirmar la diferència de contingut (`codigo_operador`); tots dos són al primer commit.
- Ruta i comanda de l'extracció validada: se sap que l'extracció validada que
  s'utilitza actualment és a `C:\datos\antenas\actual` i que els seus markers són a
  `C:\datos\antenas\actual\markers`. No està documentada la comanda original ni el
  valor exacte que es va subministrar a `--versiones-dir` en aquella execució
  (`--versiones-dir` no té valor per defecte al codi).
