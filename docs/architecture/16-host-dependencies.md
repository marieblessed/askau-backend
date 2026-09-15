# 16 — Host and service dependencies (for the platform team)

Everything the application needs that lives outside this repository. It does not
build images (ADR-0014); this page is the contract with whoever does.

**Design position: nothing is installed on the API or worker host.** Where a
capability needs a system binary or a model, it runs as its own container and
the application talks to it over HTTP. That is a deliberate constraint, not an
accident of packaging — see §3 for why.

---

## 1. What runs where

| Dependency | Form | Needed by | Required? |
|---|---|---|---|
| PostgreSQL ≥ 16 + **pgvector ≥ 0.8.0** | service | API, worker | always |
| Redis | service | API, worker | always |
| **OCR (Apache Tika)** | service | worker only | only if the corpus has scans |
| LLM endpoint (OpenAI-compatible) | service | API | production |
| Embedding endpoint | service | API, worker | production |

Nothing on this list is an `apt install` on an application host.

---

## 2. pgvector ≥ 0.8.0 — enforced at startup

Retrieval sets `hnsw.iterative_scan = relaxed_order` per session. Below 0.8.0
that setting does not exist, and an ACL-filtered ANN search **silently
under-returns**: the query succeeds, returns fewer rows than it should, and the
reader sees a thinner answer with no indication anything went wrong.

Because the failure is invisible, the application refuses to start rather than
degrade. `ASKAU_MIN_PGVECTOR_VERSION` is asserted at boot.

```bash
psql -tAc "SELECT extversion FROM pg_extension WHERE extname='vector';"
```

---

## 3. OCR — a container, never a host install

### Why not install it

OCR needs an engine plus language data per script. Both options are bad on the
host:

- **Tesseract** is a system binary. `pip install pytesseract` installs a wrapper
  around something that must already be there, and language packs are separate
  packages again.
- **Deep-learning OCR** (RapidOCR, docTR, PaddleOCR) is pip-installable but
  fetches model weights at runtime, trading a system package for a
  model-artifact supply problem — worse in a controlled environment, not better.
  None of them support Amharic.

Either way the application's behaviour would depend on how the machine was
provisioned, and the failure is silent: a scanned document produces no text,
indexes as nothing, and staff are told there is not enough evidence for a policy
that is demonstrably in the corpus. Nothing errors.

So OCR runs where every other stateful dependency already runs — in its own
container, versioned and operated by the platform team. Adding Amharic becomes
an image change rather than a code change.

### What to run

```yaml
# alongside postgres and redis
tika:
  image: apache/tika:3.0.0.0-full     # -full carries the language data
  ports: ["9998:9998"]
```

The `-full` variant is the one that matters: the slim image has no OCR data at
all. Reachable from the **worker** only; the API never calls it.

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `ASKAU_OCR_PROVIDER` | `none` | `none` (off) or `tika` |
| `ASKAU_OCR_URL` | — | e.g. `http://tika:9998`. Required unless provider is `none` |
| `ASKAU_OCR_LANGUAGES` | `eng` | Comma-separated, e.g. `eng,fra,ara,amh` |

Startup refuses `ASKAU_OCR_PROVIDER != none` with no URL, rather than accepting
a configuration that can only fail later.

Declaring a language is what makes its absence detectable. A language the corpus
needs but the configuration does not name is invisible to every check here.

### Behaviour, and how each state is visible

| State | What happens to a scan | Where you see it |
|---|---|---|
| `provider=none` | Rejected as `no_text_layer`, with a remedy for the source owner | Admin → Ingestion |
| `provider=tika`, reachable | OCR'd, flagged approximate, indexed | Admin → Ingestion |
| `provider=tika`, unreachable | Fails as `ocr_unavailable` — **an infrastructure fault, not a document problem** | `/health/deep` → `degraded` |

That last row is the distinction worth preserving. `no_text_layer` sends someone
to the document's owner; `ocr_unavailable` sends someone to restart a container.
Reporting one as the other wastes everybody's time.

`GET /health/deep` probes the service and reports `degraded` when it is
configured but unreachable, so monitoring catches it rather than waiting for
someone to notice missing answers.

### Verification

```bash
curl -s localhost:9998/version                       # Tika answers
curl -s localhost:8080/health/deep | jq '.status, .checks.ocr'
```

### The trade OCR makes

OCR text is **not verbatim**. A citation quoting it may not match the
authoritative document character for character, which matters when the point of
a citation is that the reader can check it. So the position is: OCR makes a
scanned document **findable**, and the reader is sent to the original to **read**
it. Every OCR-derived result carries that warning, and no heading structure is
inferred — OCR yields characters, not hierarchy, and a guessed hierarchy would
be fabricated precision.

### Is it worth turning on?

Measure before deciding. `GET /v1/admin/ingestion/failures` reports
`ocr_would_recover` — the number of documents rejected specifically as
`no_text_layer`. That is exactly the set OCR would add to the corpus, and it is
far more useful measured against the real AUC corpus than estimated in advance.
