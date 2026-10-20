# PorxPy — Prestatie-indicatoren van fondsen

*Classificatie, indicatoren met context, en de kleurregels per fondstype.*

**Versie 0.5 (concept)** · 20 oktober 2026 · Auteur: Jandirk, opgesteld met Claude

> **Status: ontwerp, nog niet gebouwd.** Dit document heeft een eigen
> versienummer, los van de PorxPy-release in `porxpy/__init__.py`.
> Indicatoren zijn gewoon een volgende set data-items: hoe ze worden
> opgehaald, gevalideerd, gepind en ververst, staat in
> [DATA_FEED_ARCHITECTURE.md](DATA_FEED_ARCHITECTURE.md) en wordt hier
> niet herhaald. Dit document beschrijft alleen wat er voor indicatoren
> bij komt.

## Versiegeschiedenis

| Versie | Datum | Wijziging |
|---|---|---|
| 0.5 | 2026-10-20 | Afgesplitst uit de data-feed-architectuur (versie 0.4, hoofdstuk 14) en onder versiebeheer gebracht als Markdown. 14.1–14.4 zijn hier §2, §3, §4 en §5. De indicator-regels uit de data-items-tabel (4), de ketentabel (5.2) en het implementatieplan (17) zijn meegekomen naar §3 en §6. De doelomschrijving (oud 14.5) is gebleven in DATA_FEED_ARCHITECTURE.md als 13.6, omdat het een extractie-item is en geen indicator. Gevolgen van besluiten B1 en B2 toegevoegd; bevinding over de bestaande classificatievelden toegevoegd in §2. |
| 0.4 | 2026-10-20 | Hoofdstuk 14 van het Word-concept `PorxPy_data_feed_architectuur.docx`. |

## Besluiten

De besluiten zelf staan in
[DATA_FEED_ARCHITECTURE.md](DATA_FEED_ARCHITECTURE.md#besluiten), want
ze gaan over de bronnen en niet over de indicatoren. Hier staat alleen
wat ze voor indicatoren betekenen.

**B1 — Extractie draait automatisch.** Indicatoren uit de factsheet en
de classificatie uit factsheet en KID komen automatisch binnen bij elk
nieuw document. Er hoeft niets te worden aangeklikt om de kleuren
actueel te houden.

**B2 — Een onbeschikbare bron wordt overgeslagen.** Is de AI uitgezet,
ontbreekt de key of is het tegoed op, dan wordt de extractie
overgeslagen. Voor indicatoren heeft dat twee gevolgen:

- **Indicatoren** hebben dan alleen de berekende schakel (§3). Dat is al
  de bovenste schakel, dus voor de meeste indicatoren verandert er
  niets. Alleen een indicator die PorxPy niet zelf kan berekenen (zoals
  active share, waarvoor de holdings van de index nodig zijn) blijft dan
  leeg.
- **De classificatie** valt terug op de volgende schakel: de bestaande
  waarde of handmatige invoer. Is er ook daar niets, dan is de
  classificatie onbekend en blijven de kleuren grijs (§2), in plaats van
  te gokken. Dat is bewust: een kleur op basis van een geraden fondstype
  is erger dan geen kleur.

## Inhoud

1. Doel en samenhang
2. Classificatie
3. Indicatoren met context
4. Kleurregels per fondstype
5. Indicator_rules.csv
6. Implementatieplan en open punten

## 1. Doel en samenhang

Om te beoordelen of een indicator goed of slecht is, moet PorxPy weten
wat voor fonds het is: een beta van 0,8 is voor een passieve tracker een
slechte uitvoering en voor een actief defensief fonds een keuze. Dit
document legt daarom drie dingen vast:

- de **classificatie** van het fonds, die bepaalt welke regels gelden (§2);
- de **indicatoren** zelf, opgeslagen met hun context, zodat alleen
  vergelijkbare waarden met elkaar vergeleken worden (§3);
- de **kleurregels**, per indicator en per fondstype (§4 en §5).

Classificatie en indicatoren zijn data-items in de zin van
DATA_FEED_ARCHITECTURE.md hoofdstuk 4: met een keten, validatie,
pinnen, provenance en TTL. De bronnen staan in
DATA_FEED_ARCHITECTURE.md 13.3 (wat uit factsheet en KID wordt
uitgelezen).

## 2. Classificatie

Om te beoordelen of een indicator goed of slecht is, moet PorxPy weten wat voor fonds het is. Deze kenmerken zijn zelf data-items, grotendeels gevuld door de extractie. Is de classificatie onbekend, dan blijven de kleuren grijs in plaats van te gokken.

| **Data-item** | **Waarden (uit vocabulaire-CSV)** | **Bron** |
|---|---|---|
| `management_style` | active / passive | KID-doelstelling, factsheet |
| `vehicle` | ETF / fund | `identity`, factsheet |
| `replication` | full physical / optimised / synthetic (swap) | Factsheet (iShares: "Physical / Optimised", Amundi: "Physisch") |
| `asset_class` | equity / bond / mixed / ... | Bestaande fondsklasse |
| `benchmark` | Naam en ticker van de index | Factsheet |

**Bevinding: vier van de vijf bestaan al in PorxPy (v0.133.0), onder een
andere naam.** Ze staan in `config.OVERRIDABLE_FIELDS`, worden nu al uit
de factsheet geëxtraheerd en kunnen in de Edit-dialoog worden
overschreven:

| Data-item hier | Bestaand veld | Bestaand vocabulaire | Verschil |
|---|---|---|---|
| `management_style` | `style` | `active`, `passive`, `unknown` | alleen de naam |
| `vehicle` | `structure` | `etf`, `fund`, `unknown` | alleen de naam |
| `replication` | `replication` | `full`, `sampled`, `synthetic`, `n/a`, `unknown` | `sampled` heet hier `optimised`; `n/a` (geen ETF) ontbreekt hier |
| `asset_class` | `primary_asset_class` | `Primary_asset_class_definitions.csv` | geen; dit is "de bestaande fondsklasse" |
| `benchmark` | — | — | bestaat nog niet |

Het ligt voor de hand de bestaande velden te gebruiken in plaats van er
een tweede set naast te zetten; dat is nog niet besloten (§6.2).

## 3. Indicatoren met context

**Als data-item** (uit DATA_FEED_ARCHITECTURE.md hoofdstuk 4):

| **Data-item** | **Omschrijving** | **Afhankelijk van** | **TTL (voorstel)** |
|---|---|---|---|
| Indicatoren | Alpha, beta, tracking error en difference, information ratio, active share, Sharpe, volatiliteit | `price_history` + benchmark (berekend), of `factsheet` | 7 dagen (berekend) |

**Keten** (uit DATA_FEED_ARCHITECTURE.md 5.2):

| **Data-item** | **Keten (in volgorde)** |
|---|---|
| Indicatoren | 1. Berekend door PorxPy (NAV-historie + benchmarkreeks)<br>2. Extractie uit factsheet |

Indicatoren uit verschillende factsheets zijn niet vergelijkbaar: iShares geeft een 3-jaars beta en standaarddeviatie, Amundi volatiliteit over 1 jaar en sinds oprichting. Daarom:
- **Opslaan met context:** periode (1j / 3j / sinds oprichting), valuta, benchmark, as-of-datum en bron (berekend of factsheet). Kleuren en vergelijken alleen bij gelijke periode.
- **Keten per indicator:** 1. berekend door PorxPy uit de NAV-historie en een benchmarkreeks (overal dezelfde definitie en periode); 2. extractie uit de factsheet. Indexreeksen zoals de MSCI World zijn niet vrij beschikbaar; een referentie-ETF dient als proxy voor de benchmark.
- **Tracking difference toevoegen:** voor passieve fondsen de belangrijkste maatstaf, het jaarlijkse rendementsverschil met de index. Tracking error meet alleen hoe gelijkmatig dat verschil is.
- **Information ratio toevoegen** voor actieve fondsen: alpha gedeeld door tracking error.
- **SRI uit de KID** als gestandaardiseerde risicomaat naast volatiliteit.

## 4. Kleurregels per fondstype

De kernvraag per indicator: kan de beheerder dit beïnvloeden, gegeven het type fonds? Bij passieve fondsen wordt de uitvoering beoordeeld, niet de markt. Een MSCI World-tracker met een lage Sharpe ratio is geen slecht fonds: de wereldmarkt had dan een matig jaar. Absolute volatiliteit wordt nooit gekleurd, omdat 15% normaal is voor aandelen en extreem voor obligaties.

| **Indicator** | **Passief** | **Actief** |
|---|---|---|
| Tracking difference | Groen: dicht bij 0 of positief<br>Rood: duidelijk negatiever dan de TER | Grijs |
| Tracking error | Groen: laag; drempel afhankelijk van `replication` (synthetisch/full lager, optimised hoger) | Informatief: hoge TE is een keuze |
| Beta | Groen: ≈ 1,0; afwijking = slecht volgen | Grijs of informatief (defensief/offensief) |
| Alpha | Groen: ≈ 0 (licht negatief = kosten)<br>Rood: duidelijk negatief | Groen: > 0<br>Rood: < 0 |
| Information ratio | Grijs | Groen: > 0,5<br>Oranje: 0–0,5<br>Rood: < 0 |
| Active share | Grijs | Groen: > 60%<br>Rood: < 40% ("closet indexer") |
| Sharpe ratio | Grijs | T.o.v. benchmark: groen hoger, rood lager |
| Volatiliteit | Grijs | T.o.v. benchmark: oranje bij duidelijk hoger |

## 5. Indicator_rules.csv

De regels staan in een resource-CSV, zodat ze zonder codewijziging aan te passen zijn en door het anchor-pattern gevalideerd worden. Een `*` betekent: geldt voor alle waarden. Voor elke indicator wordt de meest specifieke passende regel gebruikt; zonder passende regel is de kleur grijs.
```
indicator,management_style,asset_class,replication,period,green,amber,red,direction
tracking_error,passive,equity,full,3y,<0.10,0.10-0.30,>0.30,lower_better
tracking_error,passive,equity,optimised,3y,<0.20,0.20-0.50,>0.50,lower_better
information_ratio,active,*,*,3y,>0.5,0-0.5,<0,higher_better
active_share,active,equity,*,,>60,40-60,<40,higher_better
sharpe_ratio,passive,*,*,*,,,,neutral
```

In de UI krijgt elke kleur een tooltip met de regel en de periode, bijvoorbeeld "TE 3j 0,05%: groen, want passief optimised < 0,20%". De drempelwaarden zijn voorstellen om in de praktijk bij te stellen.

## 6. Implementatieplan en open punten

### 6.1 Volgorde van implementatie

Deze stappen komen na de basis uit DATA_FEED_ARCHITECTURE.md 17.1 (ketens, validators, resolver, scheduler en document-extractie), want ze gebruiken die.

1. Classificatie-items en `Indicator_rules.csv`; kleuren met tooltip in de UI.
1. Indicatoren zelf berekenen uit NAV-historie en benchmark-proxy.

### 6.2 Open punten

- Per benchmark een referentie-ETF als proxy kiezen.
- Drempelwaarden in `Indicator_rules.csv` bijstellen.
- De bestaande velden `style`, `structure`, `replication` en `primary_asset_class` gebruiken voor de classificatie (§2), of een eigen set naast ze zetten. Bij hergebruik: `sampled` en `optimised` gelijktrekken, en `n/a` voor een fonds dat geen ETF is meenemen in de kleurregels.
- `benchmark` als nieuw data-item: naam en ticker, en waar de referentie-ETF als proxy wordt vastgelegd.
