# Yurist AI backend — legal data platform (parser slice)

Django 5.2 / PostgreSQL 17 / pgvector 0.8.6. This slice is the **shared database + ingestion layer** used by the
lex.uz Parser today and by Search / AI Yurist / NHH Expert later (TZ §41: one platform, no separate vector DB).

```
Internet ── lex-crawler (collector, SQLite + files) ──▶ archive folder ──┐
                                                                          │  (hand-off; the crawler never sees Postgres)
═══════════════════════ PRIVATE ZONE ═════════════════════════════════════╪══
        manage.py ingest_crawl <archive>  ◀───────────────────────────────┘
              │ parse → validate → apply (1 transaction / document)
              ▼
   PostgreSQL + pgvector          object storage (raw HTML/PDF snapshots)
```

## Run it

```powershell
docker compose up -d db                       # pgvector/pgvector:pg17 on localhost:55432 (roles are created by deploy/db/init)
.venv\Scripts\python.exe manage.py migrate
.venv\Scripts\python.exe manage.py ingest_crawl ..\data          # idempotent; re-run any time
.venv\Scripts\python.exe manage.py rechunk                       # only after the chunker changed
.venv\Scripts\python.exe manage.py test tests --settings=config.settings.test --noinput
```

### Acts published as a PDF inside a zip

Some lex.uz acts (e.g. `docs/5875370`) are a *wrapper page*: only the requisites plus the notice "the text is given as PDF", with a
link `/files/<n>.zip` that holds the real PDF. The crawler downloads that zip as a content asset; ingestion then

* opens it safely (`ingestion/zips.py`: no extraction to disk under archive-supplied names, size and member caps, only bytes that
  really start with `%PDF-`),
* extracts the PDF's embedded text in a **subprocess with a timeout** (`text/pdf.py`), and builds `pdf_page` sections after the
  wrapper's own requisites, so the act is chunked, searchable (Cyrillic and Latin) and readable in the admin,
* stores the PDF (`primary_pdf`) and the zip (`source_zip`, provenance) as attachments; the version's raw snapshot is the PDF.

Such a page is **not** a "stub". If the PDF has no usable text it is `needs_ocr`; if no PDF was downloaded yet it stays a stub until
the next crawl. A page that already has body text keeps its HTML text and just gets the PDF attached. To fetch specific acts, queue
them with `python -m lex_crawler seed https://lex.uz/docs/5875370 ...` and then `run`.

### Browse the data (development only)

A read-only Django admin renders each law as a readable document (headings, numbered clauses, tables, appendices), with filters,
search by number/title/id, all versions, and the other-language editions linked. It is loaded by `development`/`test` settings only
(the TZ keeps Django API-only in production).

```powershell
.venv\Scripts\python.exe manage.py migrate            # also creates the admin's own auth tables
.venv\Scripts\python.exe manage.py createsuperuser    # choose your own login
.venv\Scripts\python.exe manage.py runserver           # then open http://127.0.0.1:8000/admin/
```

The **document page** shows the current version's sections combined into one readable document, and the original **PDF embedded**
below it (streamed from object storage through a permission-checked admin URL). PDFs are stored as `LegalDocumentAttachment`s: the
PDF-only act's primary file, or the site's PDF export of an HTML act. The admin reads them from the configured object store
(`backend/.object-store` in development), which `ingest_crawl` fills; a missing file self-heals on the next ingest.

Legal data cannot be added, edited or deleted there (the database forbids it as well); `DocumentTypeRank`, organizations and
embedding profiles are the editable tables.

Environment variables (see `config/settings/base.py`): `DB_NAME/USER/PASSWORD/HOST/PORT`, `CHUNK_MAX_TOKENS`, `CHUNK_MIN_TOKENS`,
`PDF_MIN_CHARS_PER_PAGE`, and for raw snapshots `OBJECT_STORE_BACKEND` (`filesystem` for dev, `s3` for production) with
`OBJECT_STORE_BUCKET/ENDPOINT_URL/REGION/ACCESS_KEY/SECRET_KEY/PREFIX`. `config.settings.production` refuses to start with a
superuser/default DB login, a short secret key, or the filesystem store. Install `requirements-dev.txt` to run the S3 tests (moto).

### Compare with lex.uz's own statistics

lex.uz publishes how many documents it holds (by kind, form, language, legal status) at <https://lex.uz/uz/statistic>. The crawler
fetches that one page (a single request) and the backend measures the database against it.

```
python -m lex_crawler stats                        # crawler side, from the repo root: writes data/source_stats/<time>.json
python manage.py compare_stats ../data             # imports new captures, prints lex.uz vs. our database
python manage.py ingest_crawl ../data              # a normal ingest run also imports new captures
```

In the admin: **Statistics > Source statistics snapshots > Compare latest with our database** (`/admin/statistics/sourcestatisticssnapshot/compare/`).

* lex.uz counts each language variant as its own document, as we do, so the numbers are directly comparable (overall, by language, by status).
* The crawler checks the page's own arithmetic (forms add up to each kind, languages and statuses add up to the total, kinds add up to
  the sentence "Bugun bazada N ta hujjat"). A page that does not add up, or a layout change, is flagged and never stored silently.
* **Kind/category** is known to us only from the legal-analysis card, so documents without a card are listed as *not classified* rather
  than guessed; `Document category maps` (editable in the admin) maps a card's document type to lex.uz's category name.
* Status likewise comes from the card: documents without one are *unknown*, never counted as in force.
* Snapshots are kept, so growth of lex.uz between captures is visible. Take a fresh one whenever you want a current comparison.

## Data model (apps)

| App | Tables | Notes |
|---|---|---|
| `legal_sources` | LegalSource, SourceSnapshot | Snapshot = pointer to raw bytes in object storage (sha256, key). Immutable. |
| `legal_documents` | LegalDocument, LegalDocumentVersion, LegalDocumentSection, LegalDocumentRelation, LegalDocumentClassification, LegalDocumentCard, DocumentTypeRank | One `LegalDocument` = one lex.uz id = one language/script variant. Variants are tied by `group_id`. |
| `legal_monitoring` | LegalUpdate | new / amended / expired / effective. |
| `parsers` | ParserJob, ParserItem, ParserError | Only *events* get item rows; unchanged documents are counted on the job. |
| `search` | DocumentChunk | Official law, org knowledge and drafts share one table (`source_type`). |
| `embeddings` | EmbeddingProfile, ChunkEmbedding | Untyped `vector` + one partial HNSW index per profile. |

### Rules the database itself enforces (not just the app)

* **One current version per document**, **no overlapping validity windows** (deferrable `EXCLUDE USING gist`), valid range ≥ start.
* **Version content is immutable** (trigger): only `valid_from/valid_to/is_current` may move. Sections and snapshots are fully immutable.
* **Official law can't be hard-deleted** (trigger; `apps.common.db.allow_hard_delete()` is the explicit maintenance escape hatch).
* **Row Level Security** on `search_documentchunk` and `embeddings_chunkembedding`, keyed on the transaction-local `app.organization_id`
  (`apps.common.db.organization_context`). `NULL` organization = official/shared. A tenant reads official + own rows, writes only own.
  A RESTRICTIVE policy forces an embedding's organization to equal its chunk's (otherwise a private vector could be stored as "official").
* **Least-privilege group roles** (`deploy/db/init/00-roles.sql`, grants re-applied atomically after every `migrate`):
  `yurist_api` (RLS enforced, official law read-only), `yurist_parser` (writes the official corpus, can never see or write org rows) and
  `yurist_search` (no login; owns the official-search function). Django must connect as a member of the first two — a superuser or
  `BYPASSRLS` role skips RLS entirely (TZ §140). Note `FORCE ROW LEVEL SECURITY`: a migrator role that is *not* in these groups sees 0 rows
  of chunk/embedding tables, so data migrations must run as a group member or a superuser.
* **The organization directory is tenant-scoped too**: a tenant sees only its own `organizations_organization` row.
* **Official search runs through `search_official_fts()`** (SECURITY DEFINER, owner `yurist_search`, hard-coded `organization_id IS NULL`).
  Under RLS the `@@` operator is not leakproof, so a plain query cannot use the GIN index. Measured on 200k chunks as `yurist_api`:
  selective query 89 ms → 4.6 ms, match-most-rows query 131 ms → 38 ms (plain RLS cost grows linearly with table size).
* **Chunks and embeddings are replace-only** (trigger): content can't be edited in place, so a vector can never go stale; a chunk's
  document must own its version; an embedding profile's dimension/metric are frozen once vectors exist.
* **Every chunk names its legal document** in `metadata` (`document_title`, `document_number`, `document_external_id`, `source_url`,
  `adopted_at`, `version_number`), and the title also heads `search_text`, so a query naming a law finds all its chunks. Validity dates
  and status are deliberately not copied (a newer edition or a start-date correction would make them stale): filter on the version/document rows.
  A corrected title/number, or chunks lacking these keys, are reported by `versions_needing_rechunk()`, i.e. plain `manage.py rechunk`.
* **Embedding dimension** must equal its profile's (trigger); one active profile at a time.

### Versioning (TZ §45–47, 54–55, 77)

* Identity of a version is the **source bytes** (`source_sha256` + primary file sha), checked *before* the extracted-text hash. Improving our
  own parser therefore never creates a fake legal version or an "amended" alert.
* Current-text observation: same source/text → unchanged; different → new version, old window closed (never edited).
* **Stale observations are ignored** (`SKIPPED`): replaying an older archive can never re-install old text as current.
* **Ingest order does not matter.** An `?ONDATE=` fetch identical to a stored text whose start was only a guess *applies the real edition
  date* (window columns are mutable, content is not). The current text sorts after every started edition but before a future-dated one,
  so a scheduled amendment never removes the act from today's search.
* **Daily runs skip untouched documents**: a fingerprint of (page bytes, every metadata card, primary PDF, language links, ingest-logic
  version) is compared before any parsing. Real archive: 9.3 s first run, 0.08 s unchanged run (0.9 ms/document).
* `?ONDATE=` editions back-fill history. If a stored historical edition later differs, that is a **`VersionConflict` for human review**, never an overwrite.
* An act seen as `expired` with no loss-of-force date closes its window at the day we first saw it (never open-ended, never sliding
  forward); the real date from a card replaces it. A one-day minimum window guards a loss-of-force date earlier than the version's start.
* `valid_from` always records its provenance (`valid_from_source`): `edition` > `card_effective` > `published` > `adopted` > `observed`.
  Guessed starts are nudged past real editions (`observed_adjusted`); two real editions on one day are an error.
* `as_of_date` search = `valid_period @> date`, default today in **Tashkent** (UTC+5). An expired act's last window is closed at its loss-of-force date, so it drops
  out by itself; an act with unknown status is returned **flagged `unknown`**, never silently "in force".

### Text pipeline

* `text/normalize.py`: browser-like HTML→text (inline tags join without spaces; tables become ` | ` rows), NUL/control stripping.
* Search key (`normalize_search`): lowercase, apostrophes removed, **Uzbek Cyrillic folded to Latin** → one FTS index and one document-number
  key (`ПҚ-330` ≡ `PQ-330`) serve both scripts. Russian stays Cyrillic. `query_variants()` covers ambiguous Cyrillic queries.
* `parsing/structure.py`: every content block → **exactly one** section (tested on all real blocks), with `ltree` path, `source_anchor`
  (lex.uz element id → deep links for citations), tables as cell structure.
* Documents are classified `ok` / `stub` (untranslated RU/EN page: header only) / `needs_ocr` (scanned PDF). Only `ok` is chunked and embedded.
* `search/chunking.py`: structure-aware packing, heading path as metadata, table rows split with repeated header, no mid-word cuts
  unless a single sentence exceeds the budget (flagged). Token counts are **estimates** until the embedding model is chosen (TZ §65).

## Known limits / decisions still open

Open items from the independent review that were **not** changed (deliberately, or because the layer does not exist yet):

* No API layer yet, so no middleware sets `app.organization_id`, and no per-role connection wiring. The GUC is settable by any SQL the
  `yurist_api` role runs — RLS is only as strong as the application's control over its own SQL (documented in `apps/common/db.py`).
* `embeddings.services.nearest()` is a low-level primitive: it has no as-of/version/status filter, and new versions re-chunk and re-embed
  everything (no reuse by `content_hash` yet). `rechunk --all` needs `--yes` because it cascade-deletes embeddings.
* Audit-type tables (`LegalUpdate`, `ParserItem`, `ParserError`) are writable by `yurist_parser`; only the legal corpus is append-only.
* `STRUCTURE_VERSION` bumps only flag "reprocess pending"; there is no command that re-derives sections (they are immutable by design).
* FTS uses `plainto_tsquery` (AND of words per script variant); relevance tuning belongs to the search milestone.
* Pathologically deep section nesting (thousands of levels) raises `RecursionError`; it is recorded as a failed item, not a crash.

* **OCR** is not implemented: scanned PDF-only acts (e.g. presidential decrees) are stored as `needs_ocr` and are not searchable yet.
* **Legal rank** needs the lawyers to fill `DocumentTypeRank`; ingestion jobs list every unmatched (type, form) / (authority, form) pair.
* **Two Uzbek scripts double the chunk/embedding volume.** Decide (benchmark, TZ §65) whether to embed one canonical script per `group_id`.
* Only ~7% of the archive has a `card1` today (crawl incomplete), so `status`/`effective_*` are mostly `unknown` for now; `published` dates fill validity starts.
* `ltree` and `btree_gist` are extensions beyond the TZ's minimal list (`vector`, `pg_trgm`, `unaccent`) and need approval (TZ §43).
* Chunks/embeddings (and the organization directory) are org-scoped so far; add future org tables with `apps.common.db.org_rls_sql(table)`.
