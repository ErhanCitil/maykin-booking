# Demodata

`hotels.json` bevat de demodata die je laadt met:

```bash
python src/manage.py load_demo_data            # vraagt om bevestiging
python src/manage.py load_demo_data --noinput  # zonder bevestiging
```

Het command verwijdert eerst alle steden, hotels, kamers, boekingen en reviews en
maakt daarna de steden, hotels en kamers uit dit bestand aan. Gebruikers en
highlights blijven staan. In productie (`ENVIRONMENT=production`) weigert het
command te draaien. Het command leest alleen dit bestand en gebruikt geen
netwerk; twee keer draaien geeft hetzelfde resultaat.

Inhoud: 6 steden (AMS, ANT, ATH, BAK, BAR, BER), 201 hotels en 510 kamers.

## Bron en licentie

De hotels (naam, sterren, straat en huisnummer, en waar bekend het aantal kamers,
gratis wifi en rolstoeltoegankelijkheid) komen uit
[OpenStreetMap](https://www.openstreetmap.org/). Ze zijn op 25 november 2026 eenmalig
opgehaald via de Overpass API. Voor Amsterdam ging dat via `tourism=hotel` met `name`
en `stars` binnen de gemeentegrens. Voor de andere steden via `tourism=hotel` met
`name` en `addr:street` binnen een rechthoek rond het centrum.

Daarmee is `hotels.json` een afgeleide database van OpenStreetMap en valt het
onder de [Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/):
© OpenStreetMap-bijdragers, zie <https://www.openstreetmap.org/copyright>. De
bronvermelding staat ook in de footer van de site. Elk hotel heeft een `osm`-veld
(bijvoorbeeld `node/123`) dat naar het oorspronkelijke OSM-object verwijst.

De foto's zijn niet van OpenStreetMap. Hun bronnen en licenties staan in
`src/maykinbooking/static/demo/CREDITS.txt`. Het zijn illustratiefoto's: ze tonen
niet het betreffende hotel.

## Hoe de dataset is gemaakt

Een eenmalig script (niet in de repository) heeft de OSM-data omgezet. Dat ging
deterministisch, dus dezelfde OSM-data geeft altijd hetzelfde bestand.

- **Selectie**: per stad maximaal 34 hotels (Antwerpen: 31, meer bruikbare waren er niet). Hotels met een sterrenclassificatie
  gaan voor, daarna hotels met een website en een huisnummer. Overgeslagen zijn
  hostels, appartementen, B&B's en vergelijkbare logiesvormen, namen zonder Latijns
  schrift, kale merknamen (zoals alleen "Ibis"), 1-sterrenhotels en adressen met een
  andere `addr:city`. Ook hotels met een rommelig straatveld (een volledig adres,
  een komma of een nummer vooraan) zijn overgeslagen. Per keten zijn maximaal 2
  hotels per stad opgenomen.
- **Adressen**: Griekse straatnamen zonder Latijnse variant in OSM zijn
  getranslitereerd volgens ELOT 743, zoals op Atheense straatnaambordjes
  (bijvoorbeeld Ερμού wordt Ermou).
- **Sterren**: `stars=4s` (superior) telt als 4. Hotels zonder `stars` hebben
  `"stars": null`. Prijzen en kamertypes zijn voor hen berekend als bij een
  3-sterrenhotel, maar de beschrijving claimt geen sterren.
- **Beschrijving**: Nederlands, 2 à 3 zinnen. Alleen feiten uit OSM (sterren,
  adres, stad, aantal kamers, gratis wifi als `internet_access:fee=no`,
  rolstoeltoegankelijk als `wheelchair=yes`), plus een neutrale zin die past bij
  het sterrenniveau. Bij hotels zonder sterren is die zin neutraal en zonder
  klasse-aanduiding. Er zijn geen voorzieningen verzonnen.
- **Kamers**: 2 à 3 per hotel. Bij 2 en 3 sterren: eenpersoons- en
  tweepersoonskamer, soms ook een familiekamer. Bij 4 sterren alle drie, bij 5
  sterren een tweepersoons- en een familiekamer. De prijs per nacht van een
  tweepersoonskamer is `basis[sterren] × stadsfactor`, met basis
  {2: 90, 3: 120, 4: 165, 5: 320} en factor AMS 1,30, BAR 1,15, ANT 0,90,
  BER 0,85, ATH 0,85 en BAK 0,45. Daarbovenop komt een vaste spreiding van ±10%
  per hotel (afgeleid van de hotelnaam). Een eenpersoonskamer kost 0,8× en een
  familiekamer 1,45× de tweepersoonskamer, afgerond op hele euro's.
- **Voorwaarden**: algemene voorwaarden van Maykin Booking ("Voor boekingen via
  Maykin Booking gelden de volgende voorwaarden."), dus geen regels van het hotel
  zelf. Ze gaan over in- en uitchecken, gratis annuleren tot 24 uur vooraf, betalen
  in het hotel en toeristenbelasting die ter plekke wordt afgerekend.
- **Foto's**: de hotelfoto's rouleren per stad vanaf een ander startpunt. De
  kamerfoto's horen bij het kamertype. De eenvoudigste familiekamerfoto
  (`family-3.jpg`) wordt niet bij 4- en 5-sterrenhotels gebruikt.
- **Stabiele hotel_id's**: bij een nieuwe versie van de dataset houden hotels die
  blijven hun plek (en dus hun `hotel_id`). Een vervangend hotel krijgt de plek van
  het hotel dat het vervangt. Zo is in Athene het hostel Yellowsquare vervangen door
  NLH Kerameikos (ATH34).

Wil je de data aanpassen, bewerk dan `hotels.json` direct. Het command controleert
het bestand (verplichte velden, kamertypes, prijzen en of de foto's bestaan) en
verwijdert pas iets als het bestand in orde is.
