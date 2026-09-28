**Lex.uz crawler — start from the homepage and resume anytime**

This project starts at `https://lex.uz/`, reads the site's search categories and language options, and collects public documents **one HTTP request at a time**. You do not need to supply document IDs.

It archives document text, HTML structure, tables, linked language versions, exposed historical editions, metadata cards, PDFs, Word exports, and same-host content images/attachments. It saves a SQLite queue so you can stop and restart without starting over.

**1. Install**

Install Python **3.10 or newer**, extract this project, and open a terminal inside the `lex-crawler` folder.

On macOS or Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

On Windows PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

On Windows, use `.\.venv\Scripts\python.exe` wherever the following examples say `python`. No browser, Playwright, API key, login, or database server is required.

**2. Start the full crawl**

```bash
python -m lex_crawler run
```

The crawler starts from the homepage, discovers searches, follows their pages, and downloads discovered resources sequentially. The default has no document/request limit. This is a potentially long run; keep the computer awake and provide sufficient disk space. It does not install a background service or a scheduler.

To start with a small test instead:

```bash
python -m lex_crawler --data test-data run --max-requests 30
```

That limits HTTP requests, including the homepage, robots checks, pagination, and downloads. A small test is intentionally incomplete; it leaves the remaining queue saved.

**3. Stop and resume**

Press **Ctrl+C** once. Run the identical command again:

```bash
python -m lex_crawler run
```

Completed resources remain saved. Pending work resumes. Interrupted writes are retried; duplicate URLs are not inserted again. A single data directory is locked so two crawler processes cannot accidentally run against it together.

Always use the same folder, or specify an absolute archive directory:

```bash
python -m lex_crawler --data /path/to/lex-archive run
python -m lex_crawler --data /path/to/lex-archive status
```

The `--data` option goes **before** `run`, `status`, or another command.

**4. Check progress**

In another terminal:

```bash
python -m lex_crawler status
```

The output lists saved, pending, failed, blocked, and missing tasks. At the end of each run, inspect:

- `data/report.json`: per-search totals, discovered IDs, queue state, and unresolved issues.
- `data/issues.json`: failures and their URLs/reasons.
- `data/state.sqlite3`: durable tasks, links, pagination checkpoints, HTTP records, and events.

“No pending tasks” does not automatically mean every document on the website exists in your archive. The report separately states whether discovered search totals reconcile and whether all discovered resources were saved. It never claims an independently verified mirror of the whole database.

**Where the downloaded information goes**

```text
data/
  state.sqlite3
  report.json
  issues.json
  documents/
    <signed-document-id>_<task-id>/
      document.json
      content.html
      text.txt
      snapshots/<response-hash>.json
  resources/
    <task-id>/
      metadata.json / page.html / viewer.html / file.pdf / file.doc / text.json / other files
      source.json
  blobs/
    <hash-prefix>/<response-hash>.gz
```

`document.json` contains ordered blocks, numeric element IDs, classes, citation links, annotations, tables, timestamps, and source hashes. Table data preserves merged-cell spans. `content.html` retains superscripts and formatting; `text.txt` is a convenience view. Original HTTP response bodies are compressed in `blobs/`. The SQLite `edges` table connects documents with metadata, exports, images, languages, editions, and references.

Metadata is saved independently, with its own URL and retrieval time. A current metadata card is not automatically labeled historical just because it was reached from a historical document. Russian or English notice-only pages remain notice-only pages; the crawler does not invent missing translations.

Some `/docs/` pages are PDF-only. Their document JSON says `representation: "pdf_only"` and lists `primary_pdf_urls`; the primary PDF is always queued, even with `--no-pdf` (that flag disables optional PDF exports). Its embedded text is extracted locally into the resource folder’s `text.json` and `text.txt`. `report.json` lists PDF text requiring review. Low-text pages can be scans, blank pages, or extraction limitations; the original PDF remains the authoritative saved representation.

**Useful commands**

```bash
# Slower requests: at least three seconds between request starts.
python -m lex_crawler run --delay 3

# Text and metadata, without toolbar PDF or Word downloads.
python -m lex_crawler run --no-pdf --no-word

# Limit successful document exports for this invocation.
python -m lex_crawler run --max-documents 10

# Requeue failed tasks after fixing the reported cause.
python -m lex_crawler retry
python -m lex_crawler run

# Recheck a resource that was previously missing.
python -m lex_crawler retry --status missing
python -m lex_crawler run

# Recheck completed work for updates. Existing snapshots remain saved.
python -m lex_crawler refresh
python -m lex_crawler run

# Verify stored response hashes without using the network.
python -m lex_crawler audit
```

Options such as `--delay`, `--no-pdf`, `--no-word`, and `--no-history` are remembered in that data directory. Restore them with `--pdf`, `--word`, or `--history`. If completed documents were previously processed with a feature disabled, run `refresh` before reprocessing them with that feature enabled. Request/document limits apply only to the current invocation.

The document limit can stop before its supplementary downloads finish. Those downloads stay pending for the next run. Use a request limit when the purpose is to bound all network activity.

**How common blockers are handled**

| Situation | Automatic response / practical next step |
|---|---|
| Internet disconnect, timeout, broken response stream, temporary 5xx | Bounded retries with backoff. Persistent failures stop the whole run with its queue saved; resume when connectivity recovers. |
| HTTP 429 | Saves a global cooldown, honoring numeric or HTTP-date `Retry-After`. Run again after the wait. It does not switch IPs or identities. |
| Expired ASP.NET state, repeated result page | Restarts that search with a fresh GET, with up to two state-recovery restarts. Already collected documents remain deduplicated. |
| Large search or truncated results | Splits using available year/month filters, then smaller date ranges where possible. Reconciles counts; falls back to the original broad query if partition coverage does not match. |
| Changing results during a crawl | Records changed result totals and keeps coverage unverified; use `refresh` for another pass. |
| Different interface languages or `/uz/`, `/ru/`, `/en/` prefixes | Recognizes localized routes and uses normalized document/search URLs. Signed document IDs are preserved. |
| PDF button or `/docs/` URL returns a PDF viewer | Recognizes the PDF-only document, follows its observed primary file URL, and validates `%PDF-` bytes. |
| PDF has embedded text | Extracts it page by page in a subprocess with a 90-second default timeout. |
| PDF is scanned, encrypted, or text extraction times out | Saves the PDF and flags text for review. Use a separate OCR workflow for scans; protected text is not unlocked. Increase `--pdf-timeout` or `--pdf-max-pages` if appropriate, then refresh/retry the resource. |
| Missing translation or empty metadata card | Preserves the actual returned notice/no-data text. A missing translation is not synthesized. |
| 404/410 | Records the URL as missing and continues with other resources. |
| Changed markup or an unexpected HTML response | Saves response evidence, marks failure, and avoids reporting empty content as a success. Fix the parser and run `retry`. |
| Disk full / local file error | Stops instead of silently discarding work. Free space and resume. |
| A file exceeds 64 MiB | Marks the task failed. If appropriate, increase `--max-response-mb`, then retry. |
| TLS/certificate failure | Stops. Correct the local clock/certificates or the server problem; TLS validation remains enabled. |
| 401/403, a login redirect, or a challenge page | Stops and records the blocker. Resolve access with the site/operator; then explicitly requeue with `retry --status blocked`. No CAPTCHA solving or access-control bypass is implemented. |
| robots.txt disallows a URL | Records it as excluded. A missing robots file is recorded, not treated as unrestricted permission. |

For implementation details and remaining limits, see `ARCHITECTURE.md`.

**Run the tests**

```bash
python -m unittest discover -s tests -v
```

The tests use saved Lex.uz HTML and simulated failures; they do not crawl the website. Live validation results are recorded in `VALIDATION.json`. Live checks were bounded tests, not a full-site download.

**Scope**

The objective is to exhaust the public searches exposed by the homepage and follow document links, variants, and editions. Site search coverage, unpublished/deleted records, new templates, external-host attachments, and inaccessible resources can limit completeness. Embedded PDF text is extracted; scanned PDFs are saved and flagged, but this project does not run OCR. PDF extraction is bounded by `--pdf-timeout` (90 seconds) and `--pdf-max-pages` (2,000); `--no-pdf-text` disables local text extraction. Search partitioning and state recovery are bounded; unresolved cases remain visible in the report.

Check [Lex.uz's information-use conditions](https://lex.uz/axborot) for your intended use, particularly bulk commercial reuse or redistribution. The crawler sends no feedback, creates no bookmarks, and does not authenticate.
