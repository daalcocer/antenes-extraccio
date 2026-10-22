LLIURAMENT FINAL DE L'EXTRACCIÓ D'INFOANTENAS

Aquest lliurament correspon a una instantània de la font oficial obtinguda
entre el 4 i el 9 de setembre de 2026. No és una sèrie històrica.

RESULTATS PRINCIPALS
- 3515 respostes GeoJSON de cel·la.
- 9520 registres a l'inventari final.
- 9338 Gis_ID únics.
- 9331 pàgines HTML de detall vàlides.
- 7 Gis_ID oficials sense pàgina de detall vàlida,
  documentats a 02_CONTROL_QUALITAT/gis_ids_sense_html_infoantenas.csv.
- 39797 files de característiques tècniques.
- 40863 files de nivells mesurats en l'entorn.

ESTRUCTURA
00_DOCUMENTACIO
    Document formal que descriu l'abast, el procés, els resultats i les
    limitacions de l'extracció.

01_DADES_FINALS
    Inventari en CSV i GeoJSON, característiques tècniques i nivells mesurats.

02_CONTROL_QUALITAT
    Resums, controls, incidències, exclusions territorials, duplicats exactes
    de solapament i tots els registres d'execució disponibles.

03_CODI_I_REEXECUCIO
    Codi de producció, configuració, límit administratiu, dependències i
    instruccions per reproduir o actualitzar l'extracció.

04_DADES_ORIGINALS_COMPRIMIDES
    Respostes GeoJSON i pàgines HTML originals comprimides separadament.

MANIFEST_SHA256.csv
    Empremta SHA-256 i mida de cada fitxer del lliurament, excepte el mateix
    manifest.

RESUM_LLIURAMENT.json
    Resum llegible per màquina de la validació i dels recomptes.

CRITERI DE CONSERVACIÓ
Només s'han eliminat les repeticions JSON exactes generades pel solapament de
les cel·les de consulta. No s'han normalitzat ni fusionat Gis_ID, codis,
operadors, coordenades, característiques o mesures. Els 7 detalls que la font
no retorna correctament s'han documentat; no se n'ha inferit ni fabricat el
contingut.
