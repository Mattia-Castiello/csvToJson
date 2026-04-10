# Quoro — Tabular Extractor: Design Specification

**Date:** 2026-04-03  
**Status:** Approved  
**Project:** quoro

---

## 1. Problema

Ogni cliente produce file tabulari (CSV, Excel) con strutture completamente diverse: header in lingue diverse, formati numerici europei, metadati mescolati ai dati, tabelle multiple nello stesso file, fogli Excel eterogenei, dati sporchi con righe di riepilogo, export enormi da gestionali. Il sistema deve estrarre i dati in un JSON strutturato, standardizzato e tipizzato — adattandosi a qualsiasi struttura senza configurazione manuale per ogni cliente.

---

## 2. Interfaccia

**CLI:**
```bash
quoro parse <file>                        # output JSON su stdout
quoro parse <file> --output result.json   # salva su file
quoro parse <file> --verbose              # mostra confidence scores e decisioni
quoro parse <file> --model llama3.3:70b   # specifica modello Ollama
quoro parse <file> --schema ./my-schemas  # cartella schemi custom
```

**Variabili d'ambiente:**
- `QUORO_MODEL` — modello Ollama di default (raccomandato: `llama3.3:70b`, minimo: `llama3.1:8b`)
- `QUORO_SCHEMA_DIR` — cartella schemi YAML custom
- `QUORO_CONFIDENCE_THRESHOLD` — soglia confidenza per escalation a LLM (default: `0.65`)

---

## 3. Architettura

Il sistema è composto da 4 layer sequenziali con interfacce ben definite.

```
CLI (quoro parse <file>)
        │
        ▼
┌─────────────────────────────────┐
│  1. READER                      │
│  CSV/TSV/Excel                  │  → lista di RawSheet
└────────────────┬────────────────┘
                 │
                 ▼
┌─────────────────────────────────┐
│  2. STRUCTURAL ANALYZER         │
│  Euristiche + confidence        │  → lista di Segment
└────────────────┬────────────────┘
                 │
                 ▼
┌─────────────────────────────────┐
│  3. SEMANTIC RESOLVER           │
│  Ollama (capace) + fallback     │  → lista di TypedDocument
└────────────────┬────────────────┘
                 │
                 ▼
┌─────────────────────────────────┐
│  4. NORMALIZER + SERIALIZER     │
│  Numeri, date, JSON flat        │  → lista di dict (JSON)
└─────────────────────────────────┘
```

**Regola multi-documento:**
- Segmenti con tipo diverso → array di JSON distinti
- Segmenti con stesso tipo **e stesse colonne canoniche** → un singolo JSON unificato (le righe vengono concatenate)
- Segmenti con stesso tipo **ma colonne diverse** → array di JSON distinti (es. Zona A/B e Zona C del tariffario hanno strutture incompatibili)

---

## 4. Layer 1 — Reader

**Input:** path file  
**Output:** `list[RawSheet]`

```python
@dataclass
class RawCell:
    value: str
    bold: bool
    bg_color: str | None
    font_size: float | None
    merged: bool

@dataclass
class RawSheet:
    name: str                        # nome foglio Excel o "sheet1" per CSV
    rows: list[list[RawCell]]        # tutte le righe, celle con metadati
    separator: str | None            # solo CSV: "," ";" "\t"
```

**csv_reader.py:**
- Rileva encoding con `chardet`
- Rileva separatore provando `,` `;` `\t` sulla prima riga
- Restituisce RawCell con solo `value` (nessun metadato formattazione)

**excel_reader.py:**
- Legge tutti i fogli con `openpyxl`
- Per ogni cella: valore + `bold`, `bg_color`, `font_size`, `merged`
- Gestisce celle unite (merged cells): propaga il valore alla prima cella del range

---

## 5. Layer 2 — Structural Analyzer

**Input:** `list[RawSheet]`  
**Output:** `list[Segment]`

```python
@dataclass
class Segment:
    sheet_name: str
    metadata: dict[str, str]         # coppie key-value estratte dalle righe header
    header: list[str]                # nomi colonne della tabella dati
    rows: list[list[str]]            # righe dati pulite (senza riepilogo/vuote)
    confidence: float                # 0.0 – 1.0
    warnings: list[str]              # es. "righe riepilogo rimosse: 3"
```

### 5.1 Header Detection

Per ogni riga si calcola un `header_score`:

| Segnale | Peso |
|---|---|
| Densità celle (% non vuote) | 0.30 |
| Ratio testuale (% celle stringa non numerica) | 0.25 |
| Consistenza tipi nelle righe sottostanti | 0.20 |
| Celle in grassetto (solo Excel) | 0.15 |
| Sfondo diverso dal corpo / font più grande (Excel) | 0.10 |

La riga con `header_score` massimo è il candidato header.

**Confidence sulla detection:**
- Un solo candidato con score > 0.7 → alta confidenza (+0.3)
- Candidati multipli con score simile → bassa confidenza (-0.3)
- Le righe sotto il candidato hanno tipi consistenti → +0.2
- Celle in grassetto nel candidato → +0.15

### 5.2 Metadata Extraction

Le righe *prima* dell'header con pattern `chiave: valore` o `chiave; valore` vengono parsate come coppie key-value:

```
"Fornitore: Nordic Design AB"  →  {"Fornitore": "Nordic Design AB"}
"Data ordine: 09/02/2026"      →  {"Data ordine": "09/02/2026"}
"PERIODO;01/01/2025 - 28/03/2026" → {"PERIODO": "01/01/2025 - 28/03/2026"}
```

### 5.3 Multi-Section Detection

Una nuova sezione viene rilevata quando, dopo un blocco di righe dati, si incontra:
- Una riga vuota seguita da una riga con alto `header_score`
- Una riga con testo centrato/in grassetto senza valori numerici (titolo sezione)
- Un cambio netto nella struttura colonne

Ogni sezione produce un `Segment` indipendente.

### 5.4 Data Cleaning

Vengono rimosse (con warning nel Segment) le righe che:
- Sono completamente vuote
- Hanno densità < 30% rispetto all'header
- Hanno pattern da riepilogo: prima cella vuota + ultima cella numerica prominente (es. "TOTALE", "SUBTOTALE")

### 5.5 Confidenza Finale e Escalation

```
confidence < 0.65  →  LLM coinvolto per struttura + tipo + mapping
confidence ≥ 0.65  →  LLM coinvolto solo per tipo + mapping colonne
LLM non disponibile →  fallback al resolver statico con fuzzy matching
```

---

## 6. Layer 3 — Semantic Resolver

**Input:** `list[Segment]`  
**Output:** `list[TypedDocument]`

```python
@dataclass
class TypedDocument:
    tipo: str                          # "ordine", "fattura", ecc. o "sconosciuto"
    canonical_fields: dict             # campi canonici mappati
    extra_fields: dict                 # colonne non in schema
    rows: list[dict]                   # righe con chiavi canoniche
    confidence_tipo: float             # confidenza classificazione
    warnings: list[str]
```

### 6.1 Prompt Ollama

Il prompt invia la **rappresentazione strutturale** — mai i dati grezzi completi. Per ogni Segment:

```
File analizzato — struttura rilevata:

SEGMENTO 1: metadati
  Fornitore: Nordic Design AB | Data ordine: 09/02/2026 | Riferimento: ORD-2026-W07

SEGMENTO 2: tabella (18 righe)
  Header: [Codice, Finitura, Descrizione, Quantità, Prezzo, Sconto]
  Esempio: FRN-045 | Bianco lucido | Tavolo multifunzione | 50 | 97.09 | 0.00

Tipi candidati disponibili: ordine, fattura, packing_list, tariffario, export_spedizioni

Rispondi in JSON:
{
  "tipo": "...",
  "mapping_colonne": {"Codice": "codice_articolo", "Quantità": "quantita", ...},
  "mapping_metadati": {"Fornitore": "fornitore", "Data ordine": "data_ordine", ...}
}
```

Il modello raccomandato è `llama3.3:70b` o superiore per garantire mapping accurati su nomi ambigui e multilingua.

### 6.2 Fallback Statico

Se Ollama non è disponibile o restituisce output non valido:
- Carica tutti gli schemi YAML da `schemas/`
- Per ogni header trovato, calcola similarity con i sinonimi di ogni campo usando `difflib.SequenceMatcher`
- Il tipo con il maggior numero di corrispondenze ad alta similarity (> 0.75) vince
- Se nessun tipo raggiunge soglia → tipo `"sconosciuto"`, colonne originali non rinominate

---

## 7. Schema System

Gli schemi sono file YAML nella cartella `schemas/`. Il sistema scopre automaticamente tutti i file nella cartella (drop-in: aggiungere un file = nuovo tipo disponibile).

**Struttura schema:**
```yaml
tipo: ordine
campi:
  codice_articolo:
    sinonimi: ["Codice", "Cod. Art.", "ITEM", "Rif.", "Art.", "Item Code"]
    tipo: string
    obbligatorio: true
  quantita:
    sinonimi: ["Quantità", "Qtà", "Qty", "QUANTITY", "N_COLLI", "Qty per Box"]
    tipo: integer
    obbligatorio: true
  prezzo_unitario:
    sinonimi: ["Prezzo", "Prezzo Unit.", "Prezzo Unitario", "Price"]
    tipo: float
    obbligatorio: false
  sconto_percentuale:
    sinonimi: ["Sconto", "Sconto %", "Discount"]
    tipo: float
    obbligatorio: false
  importo_netto:
    sinonimi: ["Importo Netto", "Importo", "Net Amount"]
    tipo: float
    obbligatorio: false
metadati_canonici:
  fornitore: ["Fornitore", "Supplier", "Mittente", "MITTENTE"]
  data_ordine: ["Data", "Data ordine", "DATA_SPED", "Date"]
  riferimento: ["Riferimento", "Rif.", "RIF_CLIENTE", "Order Ref"]
  destinatario: ["Destinatario", "Cliente", "DESTINATARIO"]
```

**Schemi inclusi:**
- `ordine.yaml` — ordini di acquisto
- `fattura.yaml` — fatture/listini prezzi
- `packing_list.yaml` — documenti di spedizione con colli e pesi
- `tariffario.yaml` — tariffe con fasce peso/zona
- `export_spedizioni.yaml` — export da gestionale spedizioni

---

## 8. Layer 4 — Normalizer + Serializer

**Input:** `list[TypedDocument]`  
**Output:** `list[dict]` (JSON)

### 8.1 Normalizzazione Numeri

Rileva il formato dalla prima occorrenza valida nella colonna:
- Formato EU: `1.234,56` → `1234.56` (punto = migliaia, virgola = decimale)
- Formato US: `1,234.56` → `1234.56`
- Valuta: rimuove simbolo `€` `$` prima di convertire

### 8.2 Normalizzazione Date

Pattern supportati → ISO 8601 (`YYYY-MM-DD`):
- `DD/MM/YYYY`, `DD-MM-YYYY`, `MM/DD/YYYY`
- `DD/MM/YY` (anno a 2 cifre → 2000+)

### 8.3 Output JSON Flat

```json
{
  "tipo": "ordine",
  "fornitore": "Nordic Design AB",
  "data_ordine": "2026-02-09",
  "riferimento": "ORD-2026-W07",
  "destinatario": "Casa Bella Trading SRL",
  "righe": [
    {
      "codice_articolo": "FRN-045",
      "finitura": "Bianco lucido",
      "quantita": 50,
      "prezzo_unitario": 97.09,
      "sconto_percentuale": 0.0
    }
  ],
  "_meta": {
    "confidence_struttura": 0.91,
    "confidence_tipo": 0.87,
    "resolver": "ollama",
    "modello": "llama3.3:70b",
    "warnings": []
  }
}
```

I campi extra (non in schema) appaiono nelle righe con il loro nome originale normalizzato in snake_case. Il blocco `_meta` è sempre presente e include confidence, resolver usato e warning.

---

## 9. Gestione Casi Speciali

| Caso | File campione | Gestione |
|---|---|---|
| Metadati offset prima della tabella | `03-header-offset.csv` | Metadata extractor rileva key-value, header detection salta le righe iniziali |
| Tabelle multiple tipi diversi | `04-multi-table.csv` | Multi-section detection → array di JSON distinti |
| Dati sporchi con subtotali | `06-dirty-data.csv` | Data cleaning rimuove righe riepilogo con warning |
| Struttura annidata (sub-item) | `07-nested-packing.csv` | Sotto-righe con prima cella vuota → campo `parent_ref` ereditato |
| Export enorme | `08-large-courier-export.csv` | LLM riceve solo struttura (header + 1 riga esempio), non tutte le 1000+ righe |
| Tariffario multi-zona | `09-shipping-tariff.csv` | Zone diverse → segmenti dello stesso tipo `tariffario` → JSON unificato con campo `zona` |
| Excel multi-foglio | `05-multi-sheet.xlsx` | Ogni foglio → Segment indipendente, poi merge se stesso tipo |

---

## 10. Dipendenze

| Libreria | Versione | Uso |
|---|---|---|
| `click` | ≥ 8.0 | CLI |
| `openpyxl` | ≥ 3.1 | Lettura Excel + metadati formattazione |
| `pyyaml` | ≥ 6.0 | Schemi documento |
| `chardet` | ≥ 5.0 | Rilevamento encoding CSV |
| `ollama` | ≥ 0.3 | Client Ollama locale |
| `difflib` | stdlib | Fuzzy matching nomi colonne (fallback) |

---

## 11. Testing Strategy (AI-First)

### 11.1 Fixture JSON Attesi

Prima di scrivere qualsiasi codice, vengono committati in `tests/fixtures/` i JSON di output attesi per tutti e 9 i file campione. Ogni test di regressione confronta l'output prodotto con la fixture corrispondente.

```
tests/
├── fixtures/
│   ├── 01-simple-order.expected.json
│   ├── 02-european-invoice.expected.json
│   ├── 03-header-offset.expected.json
│   ├── 04-multi-table.expected.json
│   ├── 05-multi-sheet.expected.json
│   ├── 06-dirty-data.expected.json
│   ├── 07-nested-packing.expected.json
│   ├── 08-large-courier-export.expected.json
│   └── 09-shipping-tariff.expected.json
├── test_reader.py
├── test_analyzer.py
├── test_resolver.py
├── test_normalizer.py
├── test_integration.py
└── eval/
    └── test_llm_resolver.py          # eval separato, richiede Ollama attivo
```

### 11.2 Test per Layer (Boundary Checks)

Ogni coppia di layer ha test di integrazione dedicati che verificano i contratti tra interfacce:

| Boundary | Test | Criterio |
|---|---|---|
| Reader → Analyzer | Ogni file campione → Segment con campi corretti | Struttura, numero segmenti, header trovato |
| Analyzer → Resolver | Confidence scoring → escalation corretta | File con struttura chiara → confidence ≥ 0.65 |
| Resolver → Serializer | TypedDocument → JSON flat valido | Tutti i campi canonici presenti, `_meta` presente |

### 11.3 Fallback Testato Esplicitamente

Il resolver statico (fuzzy matching YAML) ha una suite di test dedicata con Ollama simulato assente (mock che lancia `ConnectionError`). I test verificano che il fallback:
- Non sollevi eccezioni
- Produca JSON con tipo riconosciuto quando i sinonimi matchano
- Produca tipo `"sconosciuto"` con colonne originali quando nessun schema matcha
- Includa `"resolver": "fallback"` nel blocco `_meta`

### 11.4 Eval Harness LLM (Separato)

La parte Ollama non è deterministica e non viene testata nei test unitari normali. Viene valutata con un eval harness separato in `tests/eval/`:

**Input:** rappresentazione strutturale pre-costruita dei 9 file (output dell'Analyzer, non i file grezzi)  
**Output atteso:** `tipo` e `mapping_colonne` corretti per ciascun file  
**Metriche target:**
- Accuracy classificazione tipo: **100%** sui 9 campioni
- Accuracy mapping colonne: **≥ 90%** (colonne canoniche correttamente identificate)

L'eval si esegue con `pytest tests/eval/ -m llm` e richiede Ollama attivo con il modello configurato. Non fa parte della CI standard.

### 11.5 Acceptance Criteria per File Campione

| File | Tipo atteso | Assertion chiave |
|---|---|---|
| `01-simple-order.csv` | `ordine` | 6 righe, campi `codice_articolo` + `quantita` |
| `02-european-invoice.csv` | `fattura` | numeri EU normalizzati (97,09 → 97.09), separatore `;` |
| `03-header-offset.csv` | `ordine` | metadati estratti: `fornitore`, `data_ordine`, `riferimento` |
| `04-multi-table.csv` | `[ordine, ordine, riepilogo_spedizione]` | 3 JSON distinti (2 ordini + 1 riepilogo) |
| `05-multi-sheet.xlsx` | dipende dai fogli | ogni foglio → Segment, merge se stesso tipo |
| `06-dirty-data.csv` | `fattura` | righe riepilogo rimosse, warning in `_meta` |
| `07-nested-packing.csv` | `packing_list` | sub-item con `parent_ref` ereditato, 2 delivery note separate |
| `08-large-courier-export.csv` | `export_spedizioni` | metadati header estratti, tutte le righe dati presenti |
| `09-shipping-tariff.csv` | `[tariffario, tariffario, supplementi]` | Zona A+B separate da Zona C (colonne diverse) |

---

## 12. Rischi e Mitigazioni

| Rischio | Probabilità | Mitigazione |
|---|---|---|
| Ollama non disponibile | Media | Fallback automatico al resolver statico con fuzzy matching, testato esplicitamente |
| Mapping errato su nomi molto ambigui | Bassa (modello capace) | Confidence score nella risposta LLM + warning nel `_meta` + eval harness per rilevarlo |
| Encoding esotici su CSV vecchi | Bassa | `chardet` + fallback a `latin-1` |
| File enormi lenti da parsare | Media | Reader processa in streaming, LLM riceve solo struttura |
| Schema mancante per tipo nuovo | Alta (casi reali) | Fallback a tipo `"sconosciuto"` con colonne originali, mai un errore bloccante |
| Regressioni silenti su codice AI-generated | Alta | Fixture JSON committate per tutti i campioni, CI blocca su diff |
