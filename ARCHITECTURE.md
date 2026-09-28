**Implementation notes**

`parsing.py` contains pure HTML parsers and URL normalization. It reads public inline JavaScript as text to discover static paths; it does not evaluate website code. `http.py` handles serial network requests, bounded retries, allowed-origin redirects, size limits, robots rules, and persistent cooldowns. `storage.py` owns SQLite, content-addressed gzip response blobs, atomic exports, and coverage reports. `engine.py` advances the durable queue. `pdf_text.py` extracts embedded PDF text in a bounded subprocess, retaining page boundaries and review flags. `cli.py` handles the process lock, commands, configuration, and shutdown.

**Discovery and collection**

The only fixed entry point is `https://lex.uz/`. Bootstrap checks which of the six recognized category routes actually occur in the homepage links or scripts, and reads available language values from its controls. It creates one query per exposed category/language combination. If the broader `/search/all` route appears, it includes that as well. This is a deliberately conservative parser of the current site, not a general web spider or an assumption that every possible category will exist forever.

Homepage/search links lead to documents. Their content discovers associated resources and related documents. Every task has a unique normalized URL; fragments do not cause duplicate downloads. Negative IDs stay negative. Same-day `ONDATE=dd.mm.yyyy 00` and `ONDATE=dd.mm.yyyy` links normalize to one edition request. Comparison and two-column display URLs are excluded; ordinary editions retain their date. Bibliographic links retain their original full URLs and anchor fragments in exported records.

The queue is serial. Text documents are prioritized over their initial listing-level exports; a parsed document promotes its own metadata/files for early collection. Citation traversal has lower priority than catalog pagination. No threads, async download pool, browser automation, IP rotation, or generated numeric-ID sweep is used.

**Durability**

SQLite uses WAL and FULL synchronous commits. Tasks are marked running before execution; an interrupted running task is returned to pending on the next invocation. URL inserts are idempotent. Source bodies use SHA-256 filenames and atomic replacement; partial temporary files do not count as completed exports. The response log stores selected response headers, not request cookies or credentials.

Listing checkpoints store the next form action, current fields, and a public browsing session cookie snapshot. These are local state; do not publish `state.sqlite3`. Each response contributes a page fingerprint and discovered IDs. Expired form validation or repeated-page content triggers a bounded restart from the search URL. A restart clears that query's current coverage count but does not discard documents or source blobs already saved.

`refresh` requeues completed pages and resources. Original response blobs and per-document JSON snapshots are retained. `content.html`, `text.txt`, and `document.json` are current convenience views. Conditional GETs use ETag/Last-Modified when the server supplies them; 304 responses reuse the checked stored payload. Updating an archive does not create a timeless, immutable legal version identifier: recorded URLs and observation times remain the provenance.

**Partitioning and completeness**

Default split threshold: 1,000 results. Broad searches can split by observed years; year searches by observed months; a known year/month/date interval can split into disjoint date ranges. Partitions respect existing filters. Each query has a bounded page safety limit, default 500, and at most two automatic fresh-state restarts.

Parent queries are marked covered only when their finished descendants' distinct result IDs match the parent's reported count. If partitioning loses undated records or otherwise fails reconciliation, the parent is restarted with partitioning disabled and paged directly. This fallback is still subject to the page safety limit. Persistent mismatches are failures requiring review, not an assertion of completion.

Counts are not a proof of semantic completeness: a mutable index can substitute one result for another without changing its count. Distinct search result IDs are counted across text/PDF links without double counting download controls. The report separates unfinished work, missing/blocked resources, per-query reconciliation, and the broader unproven whole-site-completeness claim.

**Data model**

- `tasks`: canonical URL, task kind, priority, status, error, raw source hash, and JSON checkpoint.
- `responses`: method, URL, status, retrieval time, size, selected headers, and response hash for every received response, including errors and redirects.
- `pages` / `query_ids`: fingerprints and distinct IDs discovered for each search.
- `query_children`: parent/partition relationships.
- `edges`: source/target URL and relationship, for joining documents and separately saved resources.
- `events` / `meta`: diagnostics, saved options, global cooldown, and homepage scope.

PDF-only document pages are recognized by their observed `#pdfBody`/`pdffile` relationship. They retain a document record and a separately queued primary PDF. The primary PDF is collected even if optional exports are disabled. Text extraction failures, low-text pages, encryption, page limits, and timeouts are explicit in `text.json` and the report; raw PDF capture can succeed while text extraction remains incomplete.

Document JSON retains direct-child order, semantic CSS classes, element IDs, link edges, annotations, and table HTML plus row/column spans. Repeated `.lx_elem2` interface controls are removed only from the parsed working copy. The unmodified source response remains archived. Simple text extraction inserts whitespace between inline elements, so use retained HTML for exact typography and superscripts.

**Recovery workflow**

`status` reads the database while a crawl is running. `retry` requeues failures after a cause is corrected; `retry --status blocked` is explicit so an access denial does not silently trigger another series of requests. `refresh` rechecks already completed work, while missing/blocked/failed records require their corresponding retry command. `audit` validates response-blob hashes without network traffic. It verifies stored response integrity, not that the site's entire corpus was discovered.

Exit codes: 0 for a clean run or an intentional checkpoint pause; 2 for invalid configuration, local errors, or a finished queue with unresolved issues; 3 for access blockers; 4 when another process holds the archive lock. The JSON reports determine completeness; exit 0 alone does not.

**Known boundaries**

Selectors and routes are observed interfaces, not a contractual API. No software can promise to overcome every future site change. This implementation supports current public templates; changed/unsupported templates are saved for review. It does not perform OCR, reverse-engineer authenticated areas, save external-host assets automatically, resolve CAPTCHAs, or implement legal permission for bulk reuse.

Full corpus collection may take days and significant disk space, especially with separate editions and exports. The program does not auto-purchase storage, send files elsewhere, or schedule unattended future runs. A deployment can run the same command under its own approved job manager; the saved queue and process lock support restarting it.
