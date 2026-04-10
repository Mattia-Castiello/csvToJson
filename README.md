# Quoro

Quoro e un tool Python che legge file tabellari eterogenei e li converte in JSON strutturato.

Il progetto e pensato per input reali, non perfetti: header non in prima riga, metadati fuori tabella, piu blocchi nello stesso file, fogli Excel multipli, righe di riepilogo, sub-item annidati e naming colonne incoerente.

## Cosa fa

La pipeline e composta da quattro layer:

1. `Reader`: legge CSV, TSV, TXT ed Excel e li converte in una struttura interna uniforme.
2. `Analyzer`: individua header, metadati, segmenti tabellari, summary rows e warning strutturali.
3. `Resolver`: assegna significato semantico ai campi usando OpenAI con fallback statico basato su schemi YAML.
4. `Normalizer`: normalizza numeri e date, collega i sub-item al parent corretto e serializza il JSON finale.

## Formati supportati

- `.csv`
- `.tsv`
- `.txt`
- `.xlsx`
- `.xlsm`

## Requisiti

- Python `>= 3.11`

## Installazione

### Con pip

```bash
pip install -e .
```

### Con uv

```bash
uv sync
```

### Dipendenze web opzionali

Per avviare la UI web:

```bash
uv sync --extra web
```

## Uso CLI

Dopo l'installazione:

```bash
quoro parse 01-simple-order.csv
```

Nota: il percorso supportato dal repository e l'entry point `quoro` installato dal package.
Se vuoi lavorare da checkout locale senza installazione globale, usa un ambiente virtuale e `pip install -e .` oppure `uv sync`.

### Opzioni principali

- `-o`, `--output`: salva il JSON su file invece di stamparlo su stdout
- `-v`, `--verbose`: mostra i passaggi della pipeline su stderr
- `--model`: forza il modello del resolver semantico
- `--schema`: usa una directory schemi diversa da `schemas/`

### Esempi

Parse base:

```bash
quoro parse 01-simple-order.csv
```

Con log dettagliati:

```bash
quoro parse 07-nested-packing.csv -v
```

Salvataggio su file:

```bash
quoro parse 02-european-invoice.csv -o output.json
```

Schemi custom:

```bash
quoro parse 01-simple-order.csv --schema ./schemas
```

Modello custom:

```bash
quoro parse 01-simple-order.csv --model gpt-5.4-nano
```

## Variabili ambiente

Quoro carica automaticamente le variabili da `.env` se presenti.

- `OPENAI_API_KEY`: chiave API OpenAI
- `QUORO_MODEL`: modello di default del resolver semantico
- `QUORO_SCHEMA_DIR`: directory schemi alternativa
- `QUORO_CONFIDENCE_THRESHOLD`: soglia minima per fidarsi della risposta LLM prima di arricchire o degradare verso il fallback statico

Se la chiamata LLM non e disponibile, il programma continua usando il resolver statico YAML.

## Output JSON

L'output contiene:

- `tipo`: tipo documento risolto
- eventuali metadati top-level
- `righe`: righe dati normalizzate
- `etichetta`: presente quando il documento deriva da uno specifico foglio o segmento
- `righe_senza_dati`: presente se il file contiene righe aggregate o fuori dal corpo dati principale
- `_meta`: metadati tecnici come `confidence_tipo`, `resolver`, `modello` e `warnings`

Nei casi con righe annidate tipo `Sub-item`, il normalizer collega automaticamente le righe figlie al parent tramite `parent_ref` e propaga il riferimento ordine quando manca.

## Schemi

Gli schemi YAML sono in `schemas/`:

- `ordine.yaml`
- `fattura.yaml`
- `packing_list.yaml`
- `tariffario.yaml`
- `export_spedizioni.yaml`

Ogni schema definisce tipo documento, campi canonici, sinonimi, tipi dati, obbligatorieta e metadati supportati.

## Web UI

Il repository include anche una UI web minimale in `web/`.

Avvio locale:

```bash
uvicorn web.app:app --reload
```

Poi apri `http://127.0.0.1:8000` e carica un file supportato.

La UI web accetta:

- `.csv`
- `.tsv`
- `.xlsx`

## Test

Esegui tutta la suite:

```bash
pytest
```

Esegui un test specifico:

```bash
pytest tests/test_integration.py::test_07_nested_packing_sub_items_have_parent_ref -q
```

## Struttura progetto

```text
src/quoro/
  reader/        # parsing CSV e Excel
  analyzer/      # analisi strutturale
  resolver/      # resolver semantico, multi-sheet e fallback statico
  normalizer/    # coercizione tipi e serializzazione
  cli.py         # comando `quoro parse`
schemas/         # schemi YAML
tests/           # test unitari e integrazione
web/             # interfaccia web opzionale
docs/            # documentazione tecnica
```

## Documentazione tecnica

Per una spiegazione piu dettagliata del funzionamento interno:

- `docs/FUNZIONAMENTO_PROGRAMMA.md`
- `docs/superpowers/specs/2026-04-03-quoro-tabular-extractor-design.md`
- `docs/superpowers/plans/2026-04-05-multi-sheet-resolver.md`

## Limitazioni note

- L'accuratezza dipende dalla qualita dell'input e dalla copertura degli schemi disponibili.
- File molto irregolari possono richiedere sinonimi schema aggiuntivi o tuning delle euristiche.
- Il resolver LLM non e deterministico; per questo il fallback statico resta sempre parte della pipeline.
