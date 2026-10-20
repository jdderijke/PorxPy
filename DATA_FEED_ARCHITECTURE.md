# PorxPy — Data-feed-architectuur met fallback-ketens

*Vullen, valideren, pinnen en automatisch verversen van fondsdata.*

**Versie 0.5 (concept)** · 20 oktober 2026 · Auteur: Jandirk, opgesteld met Claude

> **Status: ontwerp, nog niet gebouwd.** Dit document beschrijft hoe PorxPy
> fondsdata zou moeten ophalen en onderhouden, niet hoe de huidige release
> dat doet. Het heeft daarom een eigen versienummer, los van de
> PorxPy-release in `porxpy/__init__.py`, en volgt die niet bij elke
> release-bump. De prestatie-indicatoren en de classificatie die hun
> beoordeling stuurt, staan in
> [FUND_PERFORMANCE_INDICATORS.md](FUND_PERFORMANCE_INDICATORS.md); dit
> document behandelt het laden en onderhouden van fondsdata.

## Versiegeschiedenis

| Versie | Datum | Wijziging |
|---|---|---|
| 0.5 | 2026-10-20 | Omgezet van Word (`PorxPy_data_feed_architectuur.docx`) naar Markdown en onder versiebeheer gebracht. Hoofdstuk 14 (classificatie, indicatoren en kleuren) afgesplitst naar `FUND_PERFORMANCE_INDICATORS.md`; het nummer blijft bestaan als verwijzing, zodat de hoofdstuknummers en kruisverwijzingen gelijk blijven. De doelomschrijving (oud 14.5) verplaatst naar 13.6, omdat het een extractie-item is en geen indicator. Besluiten B1 en B2 toegevoegd en verwerkt in 2, 7.1, 10.1, 13.2 en 15.3. |
| 0.4 | 2026-10-20 | Concept als Word-document. |

## Besluiten

Genomen besluiten, met hun redenering. Een besluit wordt hier vastgelegd
en in de hoofdtekst verwerkt; de hoofdtekst verwijst terug met het
nummer (B1, B2, …). Onderdelen die als uitwerking zijn voorgesteld maar
nog niet expliciet bevestigd, staan als zodanig gemarkeerd.

### B1 — Extractie van factsheet en KID draait automatisch

*Besloten 2026-10-20.*

De extractie van een factsheet of KID via de Claude API start zonder
bevestiging, zodra er een document met een nieuwe hash binnenkomt
(hoofdstuk 13), ook als de scheduler dat document ophaalde.

**Redenering.** De extractie is de bovenste schakel van de ketens voor
`ter`, `kid_costs_risk`, `objective`, `classification` en een deel van
de indicatoren. Moet de gebruiker hem per document starten, dan kan
PorxPy zichzelf niet onderhouden (hoofdstuk 15): elke maandelijkse
factsheet zou op een klik wachten, en tot die klik blijven die items op
een lagere schakel hangen. De kosten zijn begrensd doordat er per
document-hash precies één extractie is: in de praktijk ongeveer één per
fonds per maand voor de factsheet en één per jaar voor de KID.

**Verschil met de huidige release.** Nu is de AI-helper opt-in en start
de gebruiker een extractie zelf. B1 maakt de extractie automatisch; de
Settings-toggle blijft bestaan, maar als beschikbaarheid van de bron
(B2), niet als knop per document.

### B2 — Een bron die niet beschikbaar is, wordt overgeslagen, niet als mislukt geteld

*De regel besloten 2026-10-20; de uitwerking is voorgesteld, zie hieronder.*

**De regel.** Staat de AI uit, dan telt de extractie als overgeslagen.
Is er geen API-key, of is het tegoed op, dan levert de extractie niets
op en gaat de keten door naar de volgende schakel om het data-item op te
lossen. Hetzelfde geldt voor elke andere bron die een API-key nodig heeft
(Eulerpool, EODHD) of die de gebruiker kan uitzetten (justETF).

**Redenering.** "Niet beschikbaar" zegt iets over de bron, niet over het
data-item en niet over de gepinde route. Telt het als een mislukte
poging, dan gaat dat op elk niveau van het statusmodel tegelijk mis:

- Na drie refresh-cycli krijgt elke extractie-pin status `pin_broken`
  (15.3), en loopt de resolver voor elk fonds de hele keten opnieuw af,
  terwijl er met de route niets mis is.
- Elk item krijgt backoff (15.3) en wacht na terugkeer van de bron
  langer dan nodig.
- Elk item belandt na verloop van tijd in de review-wachtrij (10.2):
  tientallen meldingen voor één oorzaak.

**Uitwerking (voorgesteld, nog niet expliciet bevestigd):**

1. **Beschikbaarheid hoort bij de bron, niet bij het item.** Een
   bronrequest (7.1) krijgt een beschikbaarheidstoestand: beschikbaar,
   uitgezet, geen key, geen tegoed, offline of rate-limited. De resolver
   vraagt die op voordat hij een ronde plant (10.1, stap 3). Een
   onbeschikbare bron wordt uit de ronde gelaten, en elke keten die hem
   nodig had, gaat direct door naar de volgende schakel.
2. **Drie uitkomsten per poging in plaats van twee:**

   | Uitkomst | Voorbeeld | Gevolg |
   |---|---|---|
   | **overgeslagen** (bron niet beschikbaar) | geen key, toggle uit, tegoed op, offline | volgende schakel; geen backoff, geen `pin_broken`, niet naar review |
   | **mislukt** (bron beschikbaar, antwoord onbruikbaar) | 404 op een URL-template, PDF van een ander fonds, parse-fout | telt mee voor `pin_broken` en backoff |
   | **geslaagd / kandidaat** | zoals in 7.3 en 10.2 | valideren; pinnen of als kandidaat bewaren |

3. **Tegoed op wordt pas achteraf bekend.** Het saldo is vooraf niet op
   te vragen; de API meldt het pas bij de eerste aanroep die mislukt. Die
   aanroep telt als overgeslagen, en vanaf dat moment staat de bron voor
   álle fondsen op "geen tegoed" (de circuit breaker uit 15.3), zodat
   niet elk volgend fonds het opnieuw probeert.
4. **Herstel.** Items die tijdens de onbeschikbaarheid uit een lagere
   schakel zijn gevuld, hebben status `filled` en worden al eerder dan
   hun TTL opnieuw geprobeerd (10.2), zodat ze vanzelf terugkeren naar
   de betere bron. Aanvulling: wordt een bron weer beschikbaar, dan
   vervalt de backoff van de items die hem hebben overgeslagen.
5. **Eén melding per bron.** "Tegoed op" is één melding over één bron,
   op één plek (bijvoorbeeld een bronstatus in Settings of het
   Tools-menu), en geen waarschuwingsbadge op elk fonds.

**Correctie op een aanname:** justETF heeft in de huidige release geen
API-key. Het is een opt-in webbron en valt voor B2 onder "uitgezet".

## Inhoud

1. Doel en uitgangspunten
2. Begrippen
3. Architectuuroverzicht
4. Data-items en afhankelijkheden
5. Feeds en fallback-ketens
6. Resource-bestanden
7. Feed-interface en validatie
8. Statusmodel en pinnen
9. Provenance en archivering
10. Resolver-algoritme en onvolledige resultaten
11. Enrichment van holdings
12. Gebruik tijdens enrichment en voortgang in de UI
13. Document-extractie: factsheet en KID
14. Classificatie, indicatoren en kleuren (verplaatst naar FUND_PERFORMANCE_INDICATORS.md)
15. Automatisch verversen van stale data
16. Bevindingen per issuer
17. Implementatieplan en open punten

## 1. Doel en uitgangspunten

Dit document beschrijft hoe PorxPy alle data-items van een fonds zo veel mogelijk automatisch vult, valideert, vastzet (pinnen) en ververst. De kern is dat elk data-item een eigen, geordende **keten van feeds** heeft. PorxPy probeert de feeds van boven naar beneden; zodra een feed een resultaat levert dat de validatie doorstaat, wordt dat resultaat opgeslagen met volledige herkomstinformatie.

Uitgangspunten:
- **Volgorde per data-item, niet per fonds.** De beste bron voor koersen is een andere dan voor holdings of factsheets.
- **Een feed "slaagt" pas als de validator slaagt.** Een antwoord dat technisch lukt maar inhoudelijk onvolledig is (bijvoorbeeld alleen een top 10 als holdings) telt als mislukt, zodat de keten doorloopt.
- **Configuratie in resource-CSV's.** Ketens, URL-templates, TTL's en validatieregels staan in CSV-bestanden, consistent met het anchor-pattern: geen hardcoded vocabulaire, validatie bij het laden, fail-fast bij inconsistentie.
- **Pin de bron, niet de inhoud.** Wat eenmaal gevalideerd is, is de route ernaartoe (URL, product-ID, parameters). De inhoud ververst volgens een TTL.
- **Handmatige invoer wint altijd.** Een waarde die via Upload of de resolution-modal is vastgesteld, wordt nooit door een automatische feed overschreven.
- **Provenance voor elk veld.** Bron, tijdstip, as-of-datum en validatie-uitkomst worden altijd vastgelegd.
- **Agentische stappen via preview/commit.** Een zoekactie door Claude levert een voorstel op dat in de bestaande resolution-flow landt, niet direct in de cache.

## 2. Begrippen

| **Begrip** | **Betekenis** |
|---|---|
| Data-item | Eén logisch gegeven van een fonds, zoals `holdings`, `factsheet`, `ter`, `price_history`, `sector_breakdown`. |
| Feed | Een lezer die één data-item uit het antwoord van een bronrequest haalt, bijvoorbeeld `yahoo_ter` uit `yahoo_info`. Een feed belt zelf niet naar buiten. |
| Keten | De geordende lijst feeds voor een data-item. PorxPy probeert ze in volgorde. |
| Feed-parameters | Per fonds vastgelegde waarden die een feed nodig heeft: `isin`, `product_id`, `name_slug`, `lead_ticker`, enzovoort. |
| Pin | Een vastgelegde, gevalideerde keuze: welke feed met welke parameters dit item levert. Drie soorten: bron-pin, waarde-pin, handmatige pin (zie hoofdstuk 8). |
| TTL | Time-to-live: hoe lang een opgehaalde waarde vers is voordat hij ververst moet worden. |
| As-of-datum | De datum waarop de bron zegt dat de data betrekking heeft, los van wanneer PorxPy hem ophaalde. |
| Stale | Een waarde waarvan de TTL verlopen is, of waarvan de as-of-datum te oud is. |
| Provenance | Herkomstrecord per waarde: feed, URL, ophaaltijd, as-of, hash, validatiewaarschuwingen. |
| Kandidaat | Een resultaat dat bruikbaar is maar zachte waarschuwingen heeft. Wordt bewaard terwijl de keten verder zoekt naar iets beters. |
| Enrichment | Het aanvullen en gelijktrekken van holdings-regels: koppelen aan een effect (ISIN), labels vertalen naar het eigen vocabulaire en ontbrekende attributen opzoeken. |
| Facet | Eén dimensie waarop PorxPy uitsplitst: sector, land, valuta, asset class. |
| Dekking | Het percentage van het gewicht waarvoor een facet bekend is. |
| Bronrequest | Eén aanroep naar een externe bron, zoals `yahoo_info(ticker)` of `factsheet_pdf(isin)`. Meerdere feeds kunnen uit hetzelfde antwoord lezen. |
| Bestaande waarde | De waarde die al in de store staat. Is impliciet de laatste schakel van elke keten, zodat hij blijft staan als alle feeds falen. |
| Extractie | Het uitlezen van een gearchiveerd document (factsheet, KID) naar gestructureerde data-items, altijd via de Claude API. Draait automatisch (B1); is de AI niet beschikbaar, dan wordt de stap overgeslagen (B2). |
| Classificatie | Kenmerken van het fondstype (actief/passief, ETF/fonds, replicatiemethode, asset class, benchmark) die bepalen hoe indicatoren beoordeeld worden. Uitgewerkt in FUND_PERFORMANCE_INDICATORS.md. |
| Beschikbaarheid | Of een bron op dit moment gebruikt kan worden: uitgezet, geen key, geen tegoed, offline. Een onbeschikbare bron wordt overgeslagen, niet als mislukt geteld (B2). |

## 3. Architectuuroverzicht

De architectuur bestaat uit elf onderdelen. Ze passen in de bestaande opzet van PorxPy: pure-function compute-modules, cache-backed storage en de token-gebaseerde preview/commit-flow.

| **Onderdeel** | **Verantwoordelijkheid** | **Past in** |
|---|---|---|
| Resource-CSV's | Definities van data-items, ketens, URL-templates, TTL's en validatieregels. | `resources.py`, anchor-pattern |
| Bronrequests | Eén module per externe bron. Doen de eigenlijke aanroep, via requestcache en rate limiter. | Nieuw: `sources/` |
| Feeds | Lezen een data-item uit het antwoord van een bronrequest en normaliseren het. | Nieuw: `feeds/`; `extractors.py` voor parsing |
| Validators | Pure functies per data-item die een resultaat goed- of afkeuren, met waarschuwingen. | Nieuw: `validators.py` |
| Resolver | Loopt per fonds de afhankelijkheidsgraaf en per item de keten af; beslist over pinnen. | Uitbreiding van `resolver.py` |
| Store | Slaat waarden, pins en provenance op; kent TTL en status per item. | Bestaande cache-backed storage |
| Requestcache en rate limiter | Ontdubbelt aanroepen naar externe bronnen, cachet ook lege antwoorden, en beperkt de snelheid per host. | Nieuw, op de bestaande cache |
| Scheduler | Bepaalt welke items stale zijn en zet refresh-taken in de wachtrij. | Nieuw: `refresh.py` |
| Review-wachtrij | Verzamelt voorstellen die bevestiging nodig hebben (agentische stap, conflicten, onopgeloste regels). | Bestaande resolution-modal |
| Effectenregister | Gedeelde opslag van effecten met hun attributen (ISIN, sector, land, valuta), gevuld door enrichment. | Nieuw: `securities.py` |
| Enrichment-worker | Achtergrondtaak die holdings-regels koppelt en aanvult, in batches en op volgorde van gewicht. | Nieuw: `enrichment.py` |

De stroom voor één data-item:
```
Scheduler  --stale-->  Resolver
                         |
                         |  1. gepinde bron proberen
                         |  2. zo niet: keten aflopen
                         v
                     Feed-adapter  -->  Validator
                                           |
                         +---- geslaagd ---+---- mislukt ----+
                         v                                   v
                 Store (waarde + provenance)        volgende feed in de keten
                 pin bevestigen / aanmaken          ... laatste stap: Claude
                                                    --> Review-wachtrij
```

## 4. Data-items en afhankelijkheden

Sommige data-items zijn zelf weer een bron voor andere items. De factsheet en de KID zijn daar de duidelijkste voorbeelden van: ze zijn een data-item (het document zelf) én, via extractie, de bron voor kosten, risico, doelomschrijving, classificatie, top-10-holdings, verdelingen en indicatoren. Daardoor ontstaat een afhankelijkheidsgraaf (DAG). De resolver vult items in topologische volgorde.

| **Data-item** | **Omschrijving** | **Afhankelijk van** | **TTL (voorstel)** |
|---|---|---|---|
| `identity` | ISIN, naam, issuer, valuta, listings | – | 90 dagen |
| `feed_params` | Product-ID, slugs, lead ticker per issuer | `identity` | Tot validatie faalt |
| `holdings_raw` | Volledige lijst posities zoals de bron hem levert | `feed_params` | 7 dagen |
| `holdings_enriched` | Dezelfde lijst, gekoppeld aan het effectenregister met canonieke attributen | `holdings_raw` + effectenregister | Volgt `holdings_raw` |
| `sector_breakdown` | Sectorverdeling | `holdings_enriched` (berekend) of `factsheet` | Volgt bron |
| `country_breakdown` | Landverdeling | `holdings_enriched` (berekend) of `factsheet` | Volgt bron |
| `currency_breakdown` | Valutaverdeling | `holdings_enriched` (berekend) | Volgt bron |
| `factsheet` | Maandelijkse factsheet (PDF, gearchiveerd) | `feed_params` | 35 dagen |
| `kid` | PRIIPs-KID (PDF) | `identity` | 180 dagen |
| `ter` | Lopende kosten | `kid` of `factsheet` (extractie) | Volgt document |
| `kid_costs_risk` | Transactie-, instap- en uitstapkosten, SRI (1–7), aanbevolen termijn, scenario's | `kid` (extractie) | Volgt document |
| `objective` | Doelomschrijving (letterlijk) plus korte samenvatting (gegenereerd) | `kid` of `factsheet` (extractie) | Volgt document |
| `classification` | Actief/passief, ETF/fonds, replicatie, asset class, benchmark; de afzonderlijke items staan in FUND_PERFORMANCE_INDICATORS.md §2 | `factsheet`, `kid` (extractie) | Volgt document |
| Indicatoren | Alpha, beta, tracking error en difference, information ratio, active share, Sharpe, volatiliteit; uitgewerkt in FUND_PERFORMANCE_INDICATORS.md | `price_history` + benchmark (berekend), of `factsheet` | 7 dagen (berekend) |
| `aum` | Fondsvermogen | `factsheet` of feed | 35 dagen |
| `price_history` | Beurskoers- of NAV-reeks | `identity` | 1 dag |

**Afgeleide items.** Verdelingen die PorxPy zelf uit volledige holdings berekent, zijn nauwkeuriger dan die uit een factsheet. Ze krijgen daarom voorrang in de keten, en hun TTL volgt die van `holdings_raw`. Wordt `holdings_raw` ververst of vordert de enrichment, dan worden de afgeleide items automatisch opnieuw berekend.

## 5. Feeds en fallback-ketens

### 5.1 Beschikbare bronnen

| **Bron** | **Sterk in** | **Beperking** |
|---|---|---|
| Issuer-bestanden (iShares, Amundi, ...) | Volledige holdings, actuele factsheets, gratis, primaire bron | Eigen URL-patroon per issuer; templates kunnen breken bij een website-herbouw |
| Yahoo Finance (yfinance) | Koershistorie, gratis | Onofficieel; kan zonder aankondiging veranderen of geblokkeerd worden. Holdings alleen top 10. |
| Eulerpool API | Volledige holdings op ISIN, UCITS-dekking (volgens eigen opgave) | Nog niet getest; gratis tier, betaald vanaf $99/maand |
| EODHD | Koersen op Europese beurzen, ETF-fundamentals | Volledigheid holdings verschilt per fonds; betaald vanaf $19,99/maand |
| Documentportal (fundinfo) | Factsheets en KID's van veel aanbieders op één plek | Disclaimer per land, gebruiksvoorwaarden beperken automatisering, niet volledig |
| Claude API (extractie) | Uitlezen van factsheets en KID's, voor elke layout, ook grafieken | Kost geld per document; daarom één keer per document-hash |
| Claude (agentisch zoeken) | Zoeken wanneer alle andere bronnen falen | Traag, kost geld, niet deterministisch: alleen via review |
| Upload (handmatig) | Altijd mogelijk, ook voor klassieke beleggingsfondsen | Handwerk |

### 5.2 Voorgestelde ketens

| **Data-item** | **Keten (in volgorde)** |
|---|---|
| `holdings` | 1. Issuer-download (iShares CSV)<br>2. Eulerpool<br>3. EODHD<br>4. Upload-voorstel |
| `factsheet` | 1. Issuer-template (gepind)<br>2. Issuer-template andere taal<br>3. Documentportal<br>4. Claude als zoeker (review) |
| `kid` | 1. Issuer-template op ISIN<br>2. Documentportal<br>3. Claude als zoeker (review) |
| `sector_breakdown` / `country_breakdown` | 1. Berekend uit `holdings_enriched`<br>2. Extractie uit factsheet (let op: kan index i.p.v. fonds zijn)<br>3. Yahoo |
| `ter` | 1. Extractie uit KID<br>2. Extractie uit factsheet<br>3. Yahoo |
| `objective`, `classification` | 1. Extractie uit KID<br>2. Extractie uit factsheet<br>3. Handmatig |
| Indicatoren | Zie FUND_PERFORMANCE_INDICATORS.md §3: berekend gaat voor extractie uit de factsheet |
| `price_history` | 1. Yahoo<br>2. EODHD<br>3. Issuer-NAV-historie (gemarkeerd als NAV) |

**Laatste schakel: de bestaande waarde.** Elke keten eindigt impliciet met de waarde die al in de store staat, ook als die stale is. Falen alle feeds, dan blijft die waarde dus staan in plaats van te verdwijnen (paragraaf 10.2). Deze schakel hoeft niet in de CSV te worden opgenomen.

**Waarom Yahoo niet bovenaan staat voor holdings.** Yahoo geeft altijd een top 10 terug. Die call "slaagt" technisch, waardoor de keten zou stoppen met een onvolledig resultaat. Daarom staat Yahoo alleen bovenaan voor koershistorie, waar het wel volledig is.

## 6. Resource-bestanden

Alle configuratie staat in CSV-bestanden die bij het laden gevalideerd worden (anchor-pattern). Onbekende data-items, feeds of placeholders leiden tot een harde fout bij het opstarten, niet tot stil falen tijdens een refresh.

### 6.1 Data_item_definitions.csv

Eén regel per data-item, met TTL, maximale ouderdom van de as-of-datum en afhankelijkheden.
```
data_item,ttl_days,max_asof_age_days,depends_on,derivable_from,validator
holdings,7,10,feed_params,,validate_holdings
factsheet,35,62,feed_params,,validate_factsheet
sector_breakdown,,,holdings,holdings;factsheet,validate_breakdown
price_history,1,4,identity,,validate_price_history
```

### 6.2 Data_feed_priorities.csv

De keten per data-item. Optioneel per issuer, zodat een issuer-specifieke feed alleen voor die issuer geldt. De kolom `source_request` geeft aan uit welk bronrequest de feed leest; feeds met hetzelfde request delen één aanroep (paragraaf 7.2).
```
data_item,issuer,priority,feed,source_request,requires_review
holdings_raw,ishares,1,ishares_holdings,ishares_holdings_csv,false
holdings_raw,*,2,eulerpool_holdings,eulerpool_holdings,false
factsheet,ishares,1,ishares_factsheet,ishares_factsheet_pdf,false
factsheet,amundi,1,amundi_factsheet,amundi_factsheet_pdf,false
factsheet,*,9,claude_search,claude_search,true
ter,*,3,yahoo_ter,yahoo_info,false
aum,*,3,yahoo_aum,yahoo_info,false
sector_breakdown,*,3,yahoo_sectors,yahoo_info,false
price_history,*,1,yahoo_history,yahoo_history,false
```

### 6.3 Feed_url_templates.csv

Per issuer en data-item het URL-patroon met placeholders, het verwachte responsetype, het getalformaat, de benodigde parameters en de teststatus. Dit bestand bestaat al als eerste versie. De kolom `status` (`verified`, `seen`, `untested`, `not_found`) bepaalt hoe de resolver een template behandelt: `untested` templates worden pas geprobeerd na de `verified` en `seen` varianten, en een `not_found`-regel laat de resolver direct doorgaan naar de volgende feed.
```
issuer,data_item,priority,url_template,response_type,number_format,required_params,...,status
ishares,holdings,1,https://www.ishares.com/ch/individual/en/products/{product_id}/
    {product_slug}/1495092304805.ajax?fileType=csv&fileName={lead_ticker}_holdings
    &dataType=fund,csv,ch,product_id;product_slug;lead_ticker,...,verified
amundi,factsheet,1,https://www.amundietf.de/pdfDocuments/monthly-factsheet/{isin}/
    DEU/DEU/INSTITUTIONNEL/ETF,pdf,de,isin,...,verified
```

### 6.4 Fund_feed_params (per fonds, in de store)

De waarden voor de placeholders zijn fondsdata, geen configuratie. Ze worden per fonds opgeslagen als data-item `feed_params`, met provenance, en zelf gepind zodra een template ermee gevalideerd is.

| **Parameter** | **Voorbeeld (SWDA)** | **Hoe verkregen** |
|---|---|---|
| `isin` | IE00B4L5Y983 | Uit `identity` |
| `product_id` | 251882 | Productlijst iShares (eenmalig) |
| `product_slug` | ishares-msci-world-ucits-etf-acc-fund | Productlijst iShares (eenmalig) |
| `lead_ticker` | SWDA | Productpagina; ook gebruikt op niet-UK-sites |
| `name_slug` | ishares-core-msci-world-ucits-etf | Afgeleid van fondsnaam; niet gelijk aan `product_slug` |

## 7. Feed-interface en validatie

### 7.1 Feed-interface

Er zijn twee contracten: een bronrequest doet de aanroep naar buiten, een feed leest daaruit één data-item en normaliseert het. Geen van beide beslist of het resultaat goed genoeg is; dat doet de validator.

*Aanvulling (B2, voorgesteld):* een bronrequest meldt daarnaast zijn **beschikbaarheid** (uitgezet, geen key, geen tegoed, offline, rate-limited) voordat er iets wordt aangeroepen. `SourceUnavailable` hieronder dekt dan twee gevallen die verschillend behandeld worden: een bron die niet beschikbaar is (overgeslagen) en een bron die wel beschikbaar is maar onbruikbaar antwoordt (mislukt).
```
class FeedResult(TypedDict):
    value: Any                 # genormaliseerde data (DataFrame, bytes, float, ...)
    as_of: date | None         # datum volgens de bron
    source_url: str | None
    raw_hash: str              # sha256 van de ruwe respons
    meta: dict                 # o.a. index_vs_fund, nav_vs_price, currency
```
```
class SourceRequest(Protocol):
    name: str                  # bijv. "yahoo_info"
    host: str                  # voor rate limiter en circuit breaker
    required_params: tuple[str, ...]
    def key(self, fund: FundParams) -> str: ...      # cachesleutel
    def fetch(self, fund: FundParams) -> RawResponse: ...
        # raises SourceUnavailable (netwerk, 4xx/5xx, rate limit)
```
```
class Feed(Protocol):
    name: str
    data_item: str
    source_request: str        # naam van het SourceRequest
    requires_review: bool
    def extract(self, raw: RawResponse, fund: FundParams) -> FeedResult: ...
        # raises FeedParseError; belt zelf nooit naar buiten
```

### 7.2 Bronrequests, requestcache en rate limiting

Als elke keten zelf naar buiten zou bellen, zou PorxPy Yahoo per fonds apart vragen om TER, AUM en sectoren, terwijl één antwoord dat allemaal bevat. Bij veel fondsen leidt dat tot onnodig veel calls en rate-limit-problemen. Daarom zijn ophalen en uitlezen gescheiden.
- **Feeds delen bronrequests.** `yahoo_ter`, `yahoo_aum` en `yahoo_sectors` lezen alle drie uit hetzelfde `yahoo_info`-antwoord. De factsheet-extractie werkt al zo: één PDF, veel data-items.
- **Requestcache.** Alle aanroepen lopen via één cache met als sleutel bron, endpoint en parameters, en een geldigheid van één refresh-cyclus. De tweede keten die hetzelfde request nodig heeft, krijgt het antwoord uit de cache.
- **Negatieve cache.** Een leeg of mislukt antwoord wordt ook gecachet, zodat niet elke volgende keten het opnieuw probeert.
- **Opportunistisch vullen.** Een binnengekomen antwoord vult álle items die eruit gelezen kunnen worden, niet alleen het item dat erom vroeg. Voorwaarde: het item is leeg of stale, of deze feed staat minstens zo hoog in de keten als de huidige bron. Een top 10 van Yahoo overschrijft dus nooit de holdings uit de iShares-CSV.
- **Batchen over fondsen heen.** Waar een API dat toelaat, één aanroep voor veel fondsen: koershistorie via `yf.download` voor meerdere tickers, OpenFIGI met batches, bulk-endpoints van Eulerpool of EODHD.
- **Rate limiter per host.** Alle aanroepen naar dezelfde host gaan door één wachtrij met een vaste snelheid (token bucket). Samen met de circuit breaker (paragraaf 15.3) voorkomt dat blokkades, ook bij een volledige refresh van alle preloaded fondsen.

Effect: voor een fonds met tien Yahoo-items gaat het aantal calls van tien naar één of twee (`info` en `history`). Voor koershistorie over alle fondsen kan het naar één call in totaal.

### 7.3 Validators per data-item

Een validator is een pure functie. Hij geeft per resultaat een lijst bevindingen terug, elk met een ernst. Er zijn twee soorten:
- **Harde fout**: het resultaat is onbruikbaar of mogelijk fout. Bijvoorbeeld een ander ISIN in de PDF, een disclaimerpagina in plaats van een PDF, nulkoersen, of een parse-fout. Zo'n resultaat wordt nooit opgeslagen.
- **Zachte waarschuwing**: het resultaat klopt, maar is onvolledig of minder goed. Bijvoorbeeld alleen een top 10 als holdings, gewichten die optellen tot 94%, een as-of-datum die iets te oud is, of een verdeling over de index in plaats van het fonds. Zo'n resultaat is een **kandidaat** (zie hoofdstuk 10).

Elke zachte waarschuwing heeft een gewicht. De som daarvan is de **kwaliteitsscore** van een kandidaat: hoe lager, hoe beter. Alleen resultaten zonder bevindingen mogen gepind worden. In de tabel staat per regel tussen haakjes of een overtreding hard (H) of zacht (Z) is.

| **Data-item** | **Validatieregels** |
|---|---|
| `holdings_raw` | Getallen correct geparsed volgens `number_format`, bijv. ’ als duizendtal-scheiding (H)<br>Gewichten tellen op tot 99–101% (Z)<br>Aantal posities ≈ "Number of Holdings" uit factsheet, ±5% (Z; alleen top 10 = zware Z)<br>As-of-datum ≤ 10 dagen oud (Z) |
| `holdings_enriched` | Dekking per facet ≥ 99% van het gewicht (Z, zie hoofdstuk 11) |
| `factsheet` | Content-type is PDF (H)<br>ISIN van het fonds komt in de tekst voor (H)<br>Rapportagemaand ≤ 2 maanden oud (Z) |
| `kid` | PDF en bevat ISIN (H)<br>Datum ≤ 18 maanden oud (Z) |
| Verdelingen | Som 99–101% (Z)<br>Alle labels mappen op het CSV-vocabulaire (Z; onbekend telt als "Onbekend")<br>Verdeling over index i.p.v. fonds (Z) |
| `ter` | Tussen 0% en 3% (H)<br>Afwijking > 50% t.o.v. vorige waarde (Z) |
| `price_history` | Geen nulkoersen (H)<br>Valuta klopt, let op pence bij LSE (H)<br>Laatste datum ≤ 4 handelsdagen oud (Z)<br>Markering NAV of beurskoers en dividendcorrectie aanwezig (H) |

## 8. Statusmodel en pinnen

### 8.1 Statussen per data-item

| **Status** | **Betekenis** | **Volgende stap** |
|---|---|---|
| `empty` | Nog nooit gevuld | Resolver loopt de keten af |
| `filled` | Beste beschikbare kandidaat opgeslagen, met zachte waarschuwingen | Gebruiken met waarschuwingsbadge; niet pinnen; eerder opnieuw proberen dan de TTL (met backoff); na langere tijd naar review |
| `validated` | Waarde aanwezig en validatie geslaagd | Bron-pin aanmaken of bevestigen |
| `pinned` | Gevalideerd en de bron is vastgezet | Verversen via de gepinde bron bij TTL-verloop |
| `stale` | TTL verlopen of as-of te oud; ook de status als alle feeds falen en de bestaande waarde blijft staan | Scheduler zet refresh in de wachtrij (met backoff); oude waarde blijft bruikbaar |
| `pin_broken` | Gepinde bron faalt herhaaldelijk | Keten opnieuw aflopen; nieuwe bron-pin bij succes |
| `needs_review` | Alleen een agentisch voorstel of een conflict beschikbaar | Naar de resolution-modal |
| `failed` | Hele keten gefaald en er is ook geen bestaande waarde | Melding; backoff |

### 8.2 Drie soorten pins

- **Bron-pin** (automatisch): legt vast welke feed met welke parameters het item leverde, bijvoorbeeld de factsheet-URL voor SWDA. Bij een refresh wordt eerst deze bron geprobeerd. Dit is de normale situatie.
- **Waarde-pin** (automatisch, voor tijdloze items): voor gegevens die niet veranderen, zoals ISIN of oprichtingsdatum, wordt de waarde zelf vastgezet. Een refresh is dan niet nodig.
- **Handmatige pin**: elke correctie via Upload of de resolution-modal. Bron is `user`. Een automatische feed mag deze waarde nooit overschrijven; een afwijkende nieuwe waarde wordt hooguit als voorstel ter review aangeboden.

### 8.3 Wanneer wordt er gepind?

1. Een feed levert een resultaat en de validator geeft geen enkele bevinding, hard of zacht.
1. Bestaat er nog geen pin, dan wordt een bron-pin aangemaakt met feed, parameters en tijdstip.
1. Bestaat er al een pin naar dezelfde bron, dan wordt alleen `last_verified` bijgewerkt.
1. Kwam het resultaat van een lagere feed in de keten, dan vervangt de nieuwe bron-pin de oude alleen als de oude status `pin_broken` heeft. Zo springt PorxPy niet heen en weer door één toevallige storing.
1. Resultaten met `requires_review = true` (Claude) worden nooit automatisch gepind; dat gebeurt pas bij commit in de resolution-modal.

## 9. Provenance en archivering

Elke opgeslagen waarde krijgt een provenance-record. Dat maakt debuggen eenvoudig en kan in de UI getoond worden, bijvoorbeeld als tooltip bij een sectorverdeling.
```
{
  "fund": "IE00B4L5Y983",
  "data_item": "holdings",
  "feed": "ishares_holdings_csv",
  "source_url": "https://www.ishares.com/ch/.../1495092304805.ajax?...",
  "fetched_at": "2026-10-19T09:12:04Z",
  "as_of": "2026-10-15",
  "raw_hash": "sha256:9f2c...",
  "status": "pinned",
  "warnings": [],
  "meta": {"scope": "fund", "rows": 1250, "weight_sum": 100.01}
}
```

Documenten (factsheet, KID) worden als bestand gearchiveerd onder hun hash. Afgeleide waarden verwijzen naar de hash van het document waaruit ze komen, zodat altijd te achterhalen is op welke versie een getal gebaseerd is. Een nieuwe factsheet met dezelfde hash als de vorige telt niet als vernieuwing.

## 10. Resolver-algoritme en onvolledige resultaten

### 10.1 Stappen per data-item

De resolver werkt per fonds en plant alle items die ververst moeten worden samen, in rondes. Zo wordt elke externe bron zo weinig mogelijk aangeroepen.
1. **Handmatige pin aanwezig?** Dan wordt het item overgeslagen. Eventueel wel een controle-ophaling als signaal: wijkt de bron sterk af, dan een voorstel naar review.
1. **Afhankelijkheden:** items worden in lagen van de afhankelijkheidsgraaf verwerkt. Items in dezelfde laag worden samen gepland.
1. **Ronde plannen:** bepaal per item de eerstvolgende feed (de gepinde bron eerst, daarna de keten). Verzamel de benodigde bronrequests en ontdubbel die lijst. Bronnen met een open circuit breaker of een negatieve cache-hit worden overgeslagen, net als bronnen die niet beschikbaar zijn (geen key, uitgezet, geen tegoed); dat telt als overgeslagen, niet als mislukt (B2).
1. **Ophalen:** elk uniek bronrequest één keer, via requestcache en rate limiter.
1. **Uitlezen:** elk antwoord wordt gelezen door alle feeds die ervan lezen, ook opportunistisch voor andere items (paragraaf 7.2).
1. **Valideren:** perfect resultaat → opslaan en pinnen (paragraaf 8.3); alleen zachte waarschuwingen → bewaren als kandidaat; harde fout → weggooien.
1. **Volgende ronde** voor items zonder perfect resultaat: hun volgende feed, weer ontdubbeld. Dit herhaalt zich tot elk item een perfect resultaat heeft of het einde van zijn keten bereikt.
1. **Review-feed** (Claude als zoeker): alleen als er nog geen kandidaat is. Resultaat als voorstel in de review-wachtrij; status `needs_review`.
1. **Einde keten:** de laatste schakel is altijd de bestaande waarde. De resolver kiest de beste kandidaat (paragraaf 10.2).

### 10.2 Als niets in de keten perfect is

Een onvolledig resultaat laat de keten doorlopen, maar wordt niet weggegooid. Bruikbare data is beter dan geen data: een top 10 als holdings is beter dan niets.
1. **De bestaande waarde doet mee als kandidaat.** Elke keten eindigt impliciet met de feed `current_value`: de waarde die al in de store staat. Is die stale, dan krijgt hij een zachte waarschuwing voor zijn ouderdom, en verder de waarschuwingen die hij al had.
1. **Beste kandidaat kiezen:** de laagste kwaliteitsscore; bij gelijke stand de hoogste plek in de keten. Een gevalideerde holdingslijst van vorige week (alleen de waarschuwing "oud") wint zo van een top 10 van vandaag (zware waarschuwing "onvolledig").
1. **Alle feeds gefaald:** dan is de bestaande waarde de enige kandidaat en blijft hij dus staan, met status `stale` en backoff voor de volgende poging. Er wordt nooit iets gewist. Alleen als er ook geen bestaande waarde is, wordt de status `failed`.
1. **Wint een nieuwe, onvolledige kandidaat: opslaan als filled, niet pinnen.** De waarschuwingen gaan mee in de provenance. Zonder pin loopt de volgende refresh de keten weer helemaal af, voor het geval een betere bron weer werkt. Die refresh komt eerder dan de normale TTL, met backoff.
1. **Zichtbaar maken:** het item krijgt in de UI een waarschuwingsbadge, bijvoorbeeld "holdings: alleen top 10 (Yahoo)". Afgeleide items erven die markering, zodat een sectorverdeling op basis van een top 10 niet als volledig wordt gepresenteerd.
1. **Escaleren:** blijft een item langer dan een instelbare termijn (bijv. 14 dagen) `filled`, dan komt het in de review-wachtrij als signaal om een feed toe te voegen of iets te uploaden.

Voor `holdings_raw` is een variant mogelijk: in plaats van stoppen bij het eerste perfecte resultaat twee bronnen ophalen en de meest volledige kiezen. Holdings uit verschillende bronnen worden nooit samengevoegd tot één lijst.

## 11. Enrichment van holdings

### 11.1 Wat en waarom

Een holdings-regel zoals de iShares-CSV hem levert, ziet er zo uit: `ASML HOLDING · Information Technology · Netherlands · EUR · 0,70%`. Met de gewichten kun je rekenen, maar PorxPy weet nog niet zeker welk effect het is (er staat geen ISIN bij), en de labels zijn de woorden van iShares. Amundi noemt dezelfde sector misschien "IT" en zet landnamen in het Duits.

Enrichment doet drie dingen per regel:
- **Koppelen**: welk effect (ISIN of FIGI) hoort bij deze regel?
- **Vertalen**: labels uit de bron worden via alias-CSV's gemapt op het canonieke vocabulaire (anchor-pattern). Een onbekend label is geen fout maar een onopgeloste regel.
- **Aanvullen**: ontbrekende attributen (land, valuta, asset class) worden opgezocht.

Pas daarna zijn verdelingen over meerdere fondsen heen consistent.

### 11.2 Effectenregister

De attributen van een effect worden opgeslagen in een gedeeld effectenregister, met precies hetzelfde mechanisme als bij fondsen: keten, validatie, pinnen, provenance en TTL, maar dan per effect.

| **Attribuut** | **Keten (in volgorde)** | **TTL** |
|---|---|---|
| Identificatie (ISIN/FIGI) | 1. Gepinde waarde<br>2. OpenFIGI op ticker + beurs (batch)<br>3. Eulerpool<br>4. Claude (review)<br>5. Handmatig | Waarde-pin |
| Sector | 1. Gepinde waarde<br>2. Label van de issuer, gemapt via aliassen<br>3. Yahoo<br>4. Handmatig | 90+ dagen |
| Land / valuta | 1. Gepinde waarde<br>2. Issuer-regel, gemapt<br>3. OpenFIGI / Yahoo<br>4. Handmatig | 90+ dagen |
| Asset class | 1. Issuer-regel, gemapt<br>2. Afgeleid (cash, derivaten, geldmarktfonds) | 90+ dagen |

- **Gedeeld tussen fondsen:** de posities van MSCI World overlappen grotendeels met die van andere wereld-, VS- en Europa-ETF's. Na het eerste fonds is enrichment vooral een lookup.
- **Handmatige oplossing = pin op het effect:** een correctie in de resolution-modal geldt daarna automatisch voor elk fonds dat dat effect bevat.
- **Conflict tussen bronnen:** zegt de issuer sector X en het register Y, dan wint de gepinde registerwaarde, voor consistentie. Het verschil wordt gelogd.
- **Cash, derivaten en geldmarktfondsen** (zoals "USD CASH" en "BLK ICS USD LIQ") krijgen direct een eigen asset class en geen ISIN-lookup.
- **Look-through:** is een positie zelf een fonds, dan kan PorxPy daarvan weer de holdings ophalen. Voorlopig beperkt tot één niveau diep.

### 11.3 Snelheid

Enrichment per ticker via Yahoo is traag (één call per ticker) en loopt tegen rate limits aan. Voor 1.200 holdings kan dat lang duren. De aanpak, op volgorde van effect:
1. **Gebruik wat de issuer al geeft.** De iShares-CSV bevat sector, land, valuta en asset class. Die hoeven alleen vertaald te worden via de alias-CSV's: een tabel-lookup zonder netwerk, voor 1.200 regels een fractie van een seconde. Dit is verreweg de grootste winst.
1. **ISIN later.** Voor sector-, land- en valutaverdeling is de ISIN niet nodig. Die is pas nodig voor overlap tussen fondsen en look-through, en mag dus op de achtergrond.
1. **Eén keer per effect.** Dankzij het gedeelde register en de lange TTL is alleen het eerste fonds met nieuwe effecten traag.
1. **Batch-API's.** OpenFIGI accepteert veel tickers per request (met een gratis API-key volgens de documentatie 100), dus 1.200 holdings zijn ongeveer 12 requests. Actuele limieten controleren.
1. **Grootste gewichten eerst.** Onopgeloste regels worden op gewicht gesorteerd. De grootste paar honderd posities dekken het overgrote deel van een breed fonds; de staart volgt op de achtergrond.
1. **Parallel en op de achtergrond**, met een limiet per host. Yahoo wordt zo een zeldzame fallback in plaats van de hoofdroute.

### 11.4 Dekking en onopgeloste regels

Na elke enrichment-batch berekent PorxPy per fonds en per facet de **dekking**: het percentage van het gewicht waarvoor dat facet bekend is. Dat is de kwaliteitsmaat voor `holdings_enriched`: bij 99% of meer is het item `validated`, anders `filled` met een zachte waarschuwing, bijvoorbeeld "3,2% van het gewicht zonder sector".

Regels die geen enkele keten kan oplossen, gaan naar de review-wachtrij, gesorteerd op gewicht: de grootste gaten eerst.

## 12. Gebruik tijdens enrichment en voortgang in de UI

### 12.1 X-ray wacht nooit op enrichment

Enrichment loopt op de achtergrond. De portfolio-X-ray en de fondsdetails moeten de al bekende facet-data meteen kunnen gebruiken. Enrichment maakt ze alleen nauwkeuriger, nooit trager.
- **Per facet, niet per regel.** Een regel hoeft niet volledig verrijkt te zijn om mee te tellen. Van een regel kan de sector bekend zijn (uit de issuer-CSV) terwijl de ISIN nog ontbreekt. Per facet wordt gebruikt wat er is.
- **"Onbekend" als eigen categorie.** Wat nog niet bekend is, telt mee als "Onbekend" met zijn gewicht, zodat verdelingen altijd op 100% uitkomen en niet stil worden scheefgetrokken.
- **Dekking bij elke verdeling.** De X-ray toont per facet hoeveel van het gewicht bekend is, bijvoorbeeld "Sector: 97,8% bekend".
- **Incrementeel herberekenen.** Zodra enrichment een batch heeft opgelost, worden alleen de verdelingen van de betreffende fondsen en portefeuilles opnieuw berekend. Dat zijn pure-function compute-modules, dus goedkoop. De UI ververst vanzelf.

### 12.2 Vangnet bij lage dekking

Voor een fonds waarvan de dekking nog laag is (bijvoorbeeld net toegevoegd met veel onbekende effecten) gebruikt PorxPy tijdelijk de verdeling uit de factsheet of van de issuer. Die is per definitie compleet. Zodra de dekking uit de holdings boven een drempel komt (bijv. 95%), schakelt PorxPy over naar de berekende verdeling.
- Per fonds en per facet één bron, nooit een mix binnen hetzelfde fonds.
- De X-ray toont welke bron gebruikt is ("uit holdings" of "uit factsheet"), en of die over het fonds of over de index gaat.

### 12.3 Voortgangsindicator

Op drie plekken in de UI is te zien hoe ver enrichment is. De cijfers komen rechtstreeks uit de dekkingsberekening en kosten dus vrijwel niets extra.

| **Pagina** | **Wat je ziet** | **Berekend over** |
|---|---|---|
| Fund detail | Voortgang per facet (identificatie, sector, land, valuta)<br>Status: in wachtrij / bezig / klaar / X regels te reviewen | De holdings van dat fonds |
| Portfolio | Eén totaalbalk, uitklapbaar per facet en per fonds | Alle holdings in die portefeuille, gewogen naar de eigen posities |
| Preloaded funds | Per fonds een compacte balk of percentage in de lijst<br>Een totaal bovenaan | Alle preloaded fondsen |

- **Gewogen naar gewicht als hoofdgetal.** "97,8% opgelost" zegt meer dan "1.180 van 1.250 regels", omdat kleine posities nauwelijks meewegen. Het aantal regels staat in een tooltip of tweede regel.
- **Aanklikbaar.** Een klik toont de nog onopgeloste regels, gesorteerd op gewicht, met een directe link naar de resolution-modal voor regels die op review wachten.
- **Live bijwerken.** Een klein Flask-endpoint geeft per fonds de dekking en status terug. De pagina vraagt dat elke paar seconden op zolang er enrichment loopt en stopt daarna met pollen.
- **Kleuren uit het dark theme:** `--green` bij ≥ 99%, `--amber` bij gedeeltelijk, `--red` bij een mislukte keten of veel open reviews.
```
GET /api/enrichment/status?scope=fund&id=IE00B4L5Y983
{
  "state": "running",            // queued | running | done | needs_review
  "coverage": {"identity": 0.912, "sector": 0.998, "country": 1.0, "currency": 1.0},
  "rows": {"total": 1250, "resolved": 1141, "needs_review": 3},
  "updated_at": "2026-10-20T06:41:12Z"
}
```

## 13. Document-extractie: factsheet en KID

### 13.1 Principe

Ophalen en uitlezen zijn twee aparte stappen. De feed haalt het document op en archiveert het onder zijn hash (hoofdstuk 9). Daarna leest de extractie het gearchiveerde document uit naar gestructureerde data-items. Een extractor is dus gewoon een feed in de keten van die items, met als bron het document in plaats van een URL.
```
factsheet (PDF, hash) --extractie--> ter, aum, number_of_holdings, top10,
                                     sector_breakdown, country_breakdown,
                                     classification, objective, indicatoren
kid (PDF, hash)       --extractie--> ter, kid_costs_risk, objective, classification
```

Extractie draait alleen als er een document met een **nieuwe hash** binnenkomt: in de praktijk ongeveer één keer per fonds per maand voor de factsheet, en meestal één keer per jaar voor de KID. Is het document niet veranderd, dan wordt er niets opnieuw gelezen.

### 13.2 Altijd via Claude

Extractie loopt altijd via de Claude API, aangeroepen vanuit de Flask-backend met een eigen API-key. Dat kost geld per document, maar het werkt voor elke layout en elke issuer zonder layout-parsers, en er is geen aparte label-CSV per taal nodig. Om het robuust en betaalbaar te houden:
- **Automatisch (B1).** De extractie start zonder bevestiging zodra een document met een nieuwe hash binnenkomt.
- **Zonder AI: overslaan (B2).** Staat de AI uit, is er geen key of is het tegoed op, dan wordt de extractie overgeslagen en lost de keten het item op met de volgende schakel (Yahoo, handmatig). Dat telt niet als mislukte poging: geen backoff, geen `pin_broken`, geen review.
- **Eigen vocabulaire in de prompt.** De canonieke waarden uit de bestaande resource-CSV's (sectoren, landen, valuta, fondsklassen) gaan mee in de prompt. Claude geeft direct die termen terug, dus er is ook geen aparte mapping-stap. De anchor-pattern-validatie controleert daarna of alles in het vocabulaire staat.
- **Vast JSON-schema als uitvoer.** Elk veld met waarde, eenheid, periode waar relevant, `scope` (fonds of index) en paginanummer. Een antwoord dat niet aan het schema voldoet, is een harde fout.
- **Eén extractie per document.** Het resultaat wordt opgeslagen onder de hash van de PDF. Daarmee is het niet-deterministische karakter opgelost: per document is er precies één uitkomst.
- **Klein model eerst.** Begin met een klein, goedkoop model. Faalt de schema-validatie of een kruiscontrole, dan één poging met een groter model, en daarna pas review.
- **Provenance:** documenthash, paginanummer, model en promptversie per waarde. Een betere prompt kan zo gericht tot herextractie leiden.

### 13.3 Wat wordt uitgelezen

| **Veld** | **Factsheet** | **KID** |
|---|---|---|
| TER / lopende kosten | Ja | Ja (leidend) |
| Transactie-, instap-, uitstapkosten | – | Ja |
| Risico-indicator (SRI 1–7) | Soms | Ja (leidend) |
| Aanbevolen beleggingstermijn, scenario's | – | Ja |
| Doelomschrijving | Ja | Ja (leidend) |
| Classificatie (actief/passief, replicatie, benchmark) | Ja | Deels |
| Fondsvermogen, aantal holdings, top 10 | Ja (leidend) | – |
| Sector- en landverdeling | Ja (soms als grafiek, soms index) | – |
| Indicatoren (beta, volatiliteit, TE, Sharpe, ...) | Ja, met periode | – |

De rolverdeling: de **KID is leidend voor kosten, risico en doelomschrijving**. Hij is wettelijk gestandaardiseerd (PRIIPs), heeft voor elk fonds dezelfde opbouw en is verplicht voor elk fonds dat in de EU aan particulieren wordt verkocht, ook klassieke beleggingsfondsen. De **factsheet is leidend voor portefeuillegegevens**.

### 13.4 Kruiscontroles en automatisch accepteren

Elke uitgelezen waarde wordt vergeleken met andere bronnen. Slagen alle relevante controles, dan wordt het resultaat automatisch geaccepteerd; bij een verschil gaat het naar review.
- Aantal holdings in de factsheet tegenover het aantal regels in `holdings_raw` (voor SWDA allebei 1.250).
- Top 10 uit de factsheet tegenover de grootste 10 posities uit de holdings.
- Beheerkosten uit de KID tegenover de TER uit de factsheet.
- Verdelingen tellen op tot ongeveer 100%; labels staan in het vocabulaire.
- ISIN in het document is gelijk aan het ISIN van het fonds (harde fout bij verschil).

Dit werkt in twee richtingen: de factsheet is zo ook een validator voor de holdings.

### 13.5 Aandachtspunten

- **iShares:** de verdelingen staan als grafiek in de PDF. Claude kan die lezen, maar de berekende verdeling uit de holdings blijft leidend.
- **Amundi:** verdelingen en top 10 gaan over de **index**. De extractie zet `scope = index`, zodat de X-ray en het vangnet uit paragraaf 12.2 dat kunnen tonen.

### 13.6 Doelomschrijving en samenvatting

- **Doelomschrijving letterlijk uit de KID**, met de factsheet als fallback. De KID-tekst is wettelijk verplicht en dus altijd aanwezig.
- **Korte samenvatting door Claude** in het Nederlands, één tot twee zinnen, gemaakt in dezelfde extractie-aanroep. Duidelijk gemarkeerd als gegenereerd, met de letterlijke tekst uitklapbaar eronder.
- **Classificatievoorstel** in dezelfde aanroep: Claude stelt actief/passief en replicatiemethode voor; kruiscontroles of review bevestigen. Hoe die classificatie de indicatoren stuurt: FUND_PERFORMANCE_INDICATORS.md §2.

## 14. Classificatie, indicatoren en kleuren

Verplaatst naar [FUND_PERFORMANCE_INDICATORS.md](FUND_PERFORMANCE_INDICATORS.md) (versie 0.5). Indicatoren zijn gewoon een volgende set data-items: ze gebruiken dezelfde ketens, validatie, pinnen en verversing als de rest van dit document. Dat document beschrijft alleen wat er per indicator bij komt: de classificatie die bepaalt hoe een indicator beoordeeld wordt, de context waarmee hij wordt opgeslagen, en de kleurregels. De doelomschrijving (oud 14.5) staat nu in 13.6.

## 15. Automatisch verversen van stale data

Met deze architectuur kan PorxPy zichzelf onderhouden. Omdat elke waarde een TTL, een as-of-datum en een gepinde bron heeft, weet de scheduler precies wat ververst moet worden en hoe.

### 15.1 Wanneer is iets stale?

- **TTL verlopen:** `now - fetched_at > ttl_days`.
- **As-of te oud:** `today - as_of > max_asof_age_days`. Dit vangt bronnen op die wel antwoorden maar oude data blijven leveren.
- **Bron gewijzigd:** een afhankelijkheid is ververst (nieuwe holdings → verdelingen opnieuw berekenen; nieuwe factsheet → TER opnieuw uitlezen).
- **Effectattributen:** het effectenregister heeft eigen, lange TTL's. Een verlopen effect wordt opnieuw verrijkt zonder dat de holdings opnieuw opgehaald hoeven te worden.
- **Nieuw document:** een factsheet of KID met een nieuwe hash maakt alle geëxtraheerde items stale; de extractie draait opnieuw, daarna de kruiscontroles. Een nieuwe promptversie kan optioneel een herextractie van alle documenten starten.
- **Verwacht nieuw document:** voor maandelijkse factsheets kan de scheduler na de 10e van de maand alvast proberen, ook als de TTL nog niet verlopen is.

### 15.2 De refresh-cyclus

1. De scheduler draait periodiek (bijvoorbeeld elk uur, of bij het opstarten van PorxPy) en selecteert alle items met status `stale`, `pin_broken` of `failed` waarvan de backoff verstreken is.
1. Items worden gegroepeerd per fonds en in DAG-volgorde aan de resolver aangeboden.
1. De resolver probeert eerst de gepinde bron (hoofdstuk 10). In de normale situatie is dit één request per item, zonder de keten te hoeven aflopen.
1. Tijdens de refresh blijft de oude waarde bruikbaar. Pas na een geslaagde validatie wordt hij atomisch vervangen, zodat de UI nooit lege of halve data toont.
1. Afgeleide items worden direct opnieuw berekend. Nieuwe of gewijzigde holdings-regels gaan naar de enrichment-worker; regels van bekende effecten worden meteen uit het register gekoppeld.
1. Het resultaat wordt gelogd: ververst, ongewijzigd (zelfde hash), teruggevallen op lagere feed, gefaald, of naar review.

### 15.3 Robuustheid

- **Pin-reparatie:** faalt een gepinde bron drie keer achter elkaar, dan krijgt de pin status `pin_broken` en loopt de resolver de keten opnieuw af. Vindt hij een nieuwe werkende bron, dan wordt die gepind. Zo herstelt PorxPy zichzelf als bijvoorbeeld iShares een URL wijzigt.
- **Circuit breaker per feed:** faalt een feed voor meerdere fondsen tegelijk (bijvoorbeeld Yahoo ligt plat), dan wordt die feed tijdelijk overgeslagen voor álle fondsen. Dat voorkomt duizenden zinloze requests en rate-limit-blokkades. Dezelfde breaker vangt een bron op die niet beschikbaar blijkt, zoals een API-tegoed dat bij de eerste aanroep op blijkt te zijn: vanaf dan wordt de bron voor alle fondsen overgeslagen, als overgeslagen en niet als mislukt (B2).
- **Backoff per item:** na een mislukte poging groeit de wachttijd (1 uur, 4 uur, 1 dag), zodat een permanent onbereikbare bron niet blijft hameren.
- **Rate limiting per host:** via de rate limiter uit paragraaf 7.2, met een herkenbare User-Agent. Een volledige refresh plant alle fondsen samen, zodat requests over fondsen heen ontdubbeld en gebatcht worden.
- **Geen stil verlies:** omdat de bestaande waarde de laatste schakel van elke keten is, laat een gefaalde refresh de oude waarde staan, gemarkeerd als stale, in plaats van hem te wissen.

### 15.4 Wat is volledig automatisch en wat niet?

Alle stappen tot en met de laatste niet-agentische feed verlopen zonder tussenkomst. Twee situaties vragen bewust om bevestiging:
- Een resultaat van de agentische stap (Claude), omdat dat niet deterministisch is.
- Een automatisch resultaat dat afwijkt van een handmatige pin.

Die gaan naar de review-wachtrij in de bestaande resolution-modal. Als later blijkt dat de agentische stap betrouwbaar genoeg is, kan per data-item een drempel worden ingesteld waarboven een voorstel automatisch wordt geaccepteerd, bijvoorbeeld voor een factsheet-URL die alle validaties doorstaat.

## 16. Bevindingen per issuer

### 16.1 iShares

- **Holdings:** volledige CSV via een directe link op de productpagina, zonder disclaimer. Getest voor SWDA: circa 1.250 posities, as-of 15 oktober 2026. Geen ISIN per positie (alleen ticker, beurs, land), dus een resolver-stap via OpenFIGI is nodig.
- **Getalformaat:** de Zwitserse site gebruikt ’ als duizendtal-scheiding. Kies één vaste landversie en leg die vast in `number_format`.
- **Factsheet:** directe PDF via `gls-download`, gevalideerd (editie september 2026). De sector- en landverdeling staan als grafiek in de PDF, niet als tekst. Claude-extractie kan die grafieken wel lezen, als extra controle; de holdings-CSV blijft de primaire bron.
- **Parameters:** `product_id` en `product_slug` uit de productlijst; `name_slug` volgt de fondsnaam en wijkt af van de product-slug.

### 16.2 Amundi

- **Factsheet:** directe PDF op basis van alleen de ISIN; zonder datum in de URL krijg je de nieuwste editie. Gevalideerd voor IE000BI8OT95 (editie 30 september 2026).
- **Let op:** de land- en sectorverdeling en de top 10 in de factsheet beschrijven de **index**, niet het fonds. Voor fysiek repliceerde trackers is het verschil klein, voor swap-ETF's niet. Leg dit vast in `meta.scope`.
- **Holdings:** nog geen directe download gevonden. Eulerpool of een browser-verkenning is de volgende stap.

### 16.3 Overige aandachtspunten

- **Yahoo Finance:** de betaalmuur voor de downloadknop raakt yfinance niet direct, maar yfinance blijft onofficieel en kan wegvallen. Daarom altijd een tweede bron voor `price_history`.
- **NAV versus beurskoers:** ook bij accumulerende fondsen verschillen ze, door valuta, tijdstip en premie/discount. Elke reeks wordt gemarkeerd als NAV of koers, met valuta en dividendcorrectie, zodat de keten ze nooit door elkaar haalt.
- **Gebruiksvoorwaarden:** issuer-downloads via de door de issuer aangeboden links zijn prima voor eigen gebruik. Scraping van sites zonder officiële route (zoals justETF) hoort hooguit onderaan de keten.

## 17. Implementatieplan en open punten

### 17.1 Volgorde van implementatie

1. `Data_item_definitions.csv` en `Data_feed_priorities.csv` aanmaken; laden en valideren in `resources.py`.
1. Feed-interface, `FeedResult` en provenance-model; statusmodel in de store.
1. Eerste feeds: `ishares_holdings_csv`, `ishares_factsheet_template`, `amundi_factsheet_template`, `yahoo_price_history`.
1. Validators voor `holdings`, `factsheet` en `price_history`.
1. Bronrequests met requestcache (inclusief negatieve cache) en rate limiter per host.
1. Resolver met DAG, rondeplanning, ontdubbeling van requests, opportunistisch vullen en de bestaande waarde als laatste schakel; pinnen.
1. Scheduler met TTL, as-of-controle, backoff, circuit breaker en pin-reparatie.
1. Tweede bronnen: Eulerpool (na test), EODHD voor koersen.
1. Kandidatenmodel: harde fout versus zachte waarschuwing in de validators, beste-kandidaat-keuze in de resolver.
1. Effectenregister en enrichment-worker: eerst alias-mapping van issuer-labels, daarna OpenFIGI in batches.
1. Dekkingsberekening, "Onbekend"-categorie in de X-ray, vangnet bij lage dekking.
1. Voortgangsindicator op fund detail, portfolio en preloaded funds, met status-endpoint.
1. Document-extractie via de Claude API: schema, vocabulaire in de prompt, opslag per hash, kruiscontroles.
1. Indicatoren: zie het implementatieplan in FUND_PERFORMANCE_INDICATORS.md §6.
1. Agentische stap en koppeling met de review-wachtrij.

### 17.2 Open punten

- Eulerpool testen op volledigheid holdings (aantal posities, ISIN per positie, actualiteit) met `test_eulerpool_holdings.py`.
- Amundi holdings-download zoeken via browser-verkenning.
- Templates voor Vanguard en Xtrackers uitzoeken.
- `seen`- en `untested`-templates in `Feed_url_templates.csv` verifiëren (iShares fund-data-XLS, KID, NL-varianten).
- Besluiten over de documentportal: alleen inzetten als er fondsen buiten de grote ETF-huizen in de portefeuille zitten.
- TTL-waarden, validatiedrempels, gewichten van zachte waarschuwingen en de dekkingsdrempels (99% voor gevalideerd, 95% voor overschakelen van factsheet naar holdings) in de praktijk bijstellen.
- OpenFIGI-limieten controleren en een gratis API-key aanvragen.
- Claude API-key voor de backend; modelkeuze (klein/groot) en kosten per extractie in de praktijk meten.
- Bronbeschikbaarheid (B2): de uitwerking bevestigen, en vastleggen waar de bronstatus in de UI staat.
- Open punten over indicatoren: zie FUND_PERFORMANCE_INDICATORS.md §6.
