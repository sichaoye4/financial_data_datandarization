# Financial Data Standardization Workbench — implementation design

Status: runnable prototype  
Source product document: `../Financial_Data_Standardization_Workbench_PRD_HLD.docx`

## Product boundary

The workbench standardizes customer financial tables against an example output supplied by the user. It does not use a built-in target schema, score credit, approve lending, join unrelated source tables, execute arbitrary Python or let a model transform customer data directly.

The two required inputs have deliberately different roles:

| Upload section | Meaning | Accepted files |
| --- | --- | --- |
| Source data | Customer data with arbitrary headers, layout and terminology | CSV, XLSX, XLSM, XLS |
| Target example | The expected standardized structure; its table names, headers and representative values define the output contract | CSV, XLSX, XLSM, XLS |

A CSV document contains one table. In Excel, every non-empty worksheet is considered a separate table. A target worksheet may contain headers only; a source worksheet must contain at least one data row. Tables may start after preamble rows or blank leading columns: the parser discovers a header and rectangular range for every CSV or worksheet instead of assuming cell A1.

## User-controlled workflow

```text
upload source document + target example document
                       │
                       ▼
        discover and profile every table
                       │
                       ▼
    DeepSeek automatically suggests source → target pairs
                       │
                       ▼
         user edits and confirms every pair
                       │
                       ▼
 DeepSeek automatically suggests fields when a pair opens
                       │
                       ▼
 user bulk-accepts/revises rules → validates all pairs
                       │
                       ▼
 save mapping definitions + Pandas code to SQLite
                       │
                       ▼
       preview the persistent implementation page
```

Table mapping and field mapping are separate review gates. Field-level work cannot start until the user has explicitly confirmed exactly one source table for every target table. The user can then switch among the confirmed pairs; implementation saving remains blocked until all of them are ready. The fourth progress item is **Implementation**, not a publish action, and it is a full page rather than a modal.

## Runtime architecture

```text
React/Vite workbench
    │  base64 file payloads + JSON over /api
    ▼
FastAPI session API
    ├── layout-aware CSV / Excel document parser
    ├── table profiler and sensitive-value masker
    ├── DeepSeek table- and field-mapping provider
    ├── transformation contract validator
    ├── allow-listed Pandas runtime
    └── SQLite implementation repository
```

The Docker image builds the React bundle and serves it from FastAPI, giving the deployed prototype a single origin. Local development runs Vite on port 5173 with an API proxy to FastAPI on port 8000.

## React experience

The first screen makes the input contract explicit with two independent file cards. The submit action stays disabled until both documents are selected.

After parsing, the table-mapping stage shows:

- separate inventories for discovered source and target tables;
- worksheet/table name, row count, columns and representative rows;
- one automatic DeepSeek suggestion per target table, with confidence and rationale;
- a drag-and-drop board where source cards can be added to, removed from or moved between target assignments;
- an editable source-table selector for keyboard and assistive-technology access;
- table-level comments that enable deliberate AI regeneration after the initial proposal;
- a single confirmation gate before field mapping.

The field-mapping stage automatically requests its initial field proposal and keeps the existing three-pane workbench:

- source pane with profile summary, source rows and inferred column types;
- mapping rail with one card per uploaded target header;
- multi-select checkboxes with select-all, clear-all, accept-selected and accept-all controls;
- target pane with both the uploaded example and the generated standardized preview;
- field-scoped natural-language revision requests;
- persistent progress and failure states for live DeepSeek calls, including an explicit retry after failure;
- validation tray, per-pair status selector and overall save gate;
- a final implementation page with per-pair mapping JSON and generated Pandas code previews.

There is no canned mapping or provider fallback. Provider status is visible without exposing credential material.

## DeepSeek provider boundary

Configuration mirrors Football Legend:

| Setting | Default |
| --- | --- |
| Base URL | `https://api.deepseek.com` |
| Endpoint | `POST /chat/completions` |
| Model | `deepseek-v4-flash` |
| Response format | JSON object |
| Thinking | Enabled |
| Maximum output tokens | 4096 |

Credential resolution order:

1. Non-empty `DEEPSEEK_API_KEY`.
2. Trimmed contents of `DEEPSEEK_API_KEY_FILE`.
3. The repository-local ignored `llm_key.txt` path supplied as the default local file location.

The token is read only inside the Python provider adapter. It is not returned by health/status endpoints, included in prompts, printed in errors, passed to React or copied into Docker images.

Three provider commands are supported:

- `propose_table_mapping` returns one source-table suggestion for every target table. The initial call is automatic; later calls may include the user's table-level comment.
- `propose_initial_definition` returns a complete rule for every unlocked field in the active confirmed table pair.
- `propose_revision_patch` returns one field-scoped replacement rule and a short explanation.

For wide tables, initial field proposals are divided into deterministic batches of at most 30 target fields. Each batch is validated before it is merged, and no partial batch result is committed if the overall request fails. This keeps hundreds-column schemas within model output limits while retaining one automatic proposal action in the UI.

All provider output is treated as untrusted input and must pass server-side contract validation.
Harmless JSON wrappers such as Markdown fences are normalized, and an undecodable response receives one bounded retry with a stricter JSON-only instruction. Syntactically valid JSON that violates the table, field or revision contract also receives one bounded correction attempt containing the deterministic validation reason. If the corrected response still fails, the page retains a visible error and explicit retry action instead of silently returning to an awaiting state.

## Dynamic target contract

### Smart table-region discovery

Every CSV and worksheet is first read as raw cells with no assumed header. The parser evaluates the first 50 rows and every contiguous populated column span up to 500 columns. Candidate scoring favors wider unique text-like headers, downstream rows and sustained row support. The chosen table ends before a blank or one-cell footer row. The saved metadata includes the one-based header/data rows, zero-based column indexes, Excel-style start/end columns and display range such as `C4:G11`.

This is a deterministic heuristic, not an AI operation. The detected range is visible during table pairing and is embedded into the authoritative definition, so generated Pandas code reloads the same CSV or worksheet region.

Each uploaded target table becomes a target schema:

- every header becomes a required output field;
- representative values and header semantics infer `string`, `date` or `number`;
- identifier- and period-like columns define the probable output grain;
- target example rows remain visible beside the generated preview for comparison.

Each confirmed table pair owns an ordered transformation definition with:

- stable pair and step IDs;
- exact source and target table IDs;
- one or more existing source-column inputs;
- an allow-listed operation;
- typed parameters and optional equality filter;
- exact uploaded target grain for aggregations;
- confidence, rationale and user-review metadata.

Supported executable operations are:

- `direct`;
- `string` with `trim`;
- `date_parse` with an explicit format;
- `coalesce` with an optional typed default;
- `aggregate` using `sum`, `count`, `min`, `max` or `mean`;
- equality filters on existing source columns.

The backend rejects unknown table IDs, omitted target tables, unknown target fields, unknown source columns, incompatible target types, unsupported functions, unsupported predicates and aggregation grain that differs from the inferred target grain.

## Security and privacy

- Each uploaded file is limited to 8 MB; a document may expose at most 20 usable tables and each table at most 500 columns.
- Likely identifiers and contact fields have representative values replaced with `[MASKED]` before prompt construction.
- DeepSeek receives masked profiles, column names, types and statistics—not the complete uploaded documents.
- Provider error responses expose only a redacted category.
- Active session data is memory-only; saved implementations live in the ignored SQLite database at `backend/runtime/workbench.db` (or `WORKBENCH_DB_PATH`).
- No `eval`, `exec`, shell call, SQL expression or model-generated Python is executed.
- Accepted rules are locked; later proposals cannot silently overwrite them.

## API surface

| Method | Endpoint | Purpose |
| --- | --- | --- |
| GET | `/api/health` | Process and non-secret provider status |
| GET | `/api/provider/status` | Provider, model, thinking and configuration availability |
| POST | `/api/sessions` | Upload the required source/target document pair and discover tables |
| GET | `/api/sessions/{id}` | Read the complete current session state |
| POST | `/api/sessions/{id}/table-proposal` | Request live table-pair suggestions, optionally regenerated from a user comment |
| POST | `/api/sessions/{id}/table-confirm` | Confirm one source table for every target table |
| POST | `/api/sessions/{id}/table-mappings/{mapping}/activate` | Select a confirmed pair for field review |
| POST | `/api/sessions/{id}/proposal` | Request and validate live field mappings for the active pair |
| POST | `/api/sessions/{id}/revisions` | Request a live field-scoped revision |
| POST | `/api/sessions/{id}/fields/{field}/accept` | Accept and lock the current field proposal |
| POST | `/api/sessions/{id}/fields/accept` | Accept selected fields or every reviewable field in one revision |
| POST | `/api/sessions/{id}/revisions/{revision}/accept` | Accept and lock a proposed patch |
| POST | `/api/sessions/{id}/preview` | Execute the active pair's current validated definition |
| POST | `/api/sessions/{id}/validate` | Run schema, review, required-value and grain checks on the active pair |
| POST | `/api/sessions/{id}/publish` | Freeze and persist all mappings plus generated code |
| GET | `/api/implementations/{id}` | Reload a saved implementation directly from SQLite |

## Persistent implementation

Saving is blocked while any pair has a required field that is unmapped, unaccepted, missing or invalid. A successful save upserts one SQLite record keyed by session ID containing:

- authoritative reviewed definitions for every table pair;
- the generated Pandas script for every pair;
- source and target table summaries and detected regions;
- standardized previews, lineage and validation metrics;
- save timestamp and source/target document identity.

The implementation page reads this record and previews mappings and code without presenting downloads by default. Its query-string URL can be refreshed after the volatile upload session is gone. The generated Pandas scripts use the same allow-listed operation vocabulary, read the stored offset table region and do not call DeepSeek.

## Browser acceptance test

`e2e/workbench.e2e.cjs` drives an installed Chrome or Edge browser against the running service. The CSV path uses preamble/offset/footer fixtures and verifies the detected ranges, two-file intake, automatic table proposal, removal and native drag-and-drop reassignment, comment-based table regeneration, automatic six-field proposal, field correction, select-all/clear-all/multi-select/bulk acceptance, validation and the full implementation page. It reloads that page through `GET /api/implementations/{id}` to prove SQLite persistence and checks that no default download link is exposed. A second path uploads two-sheet source and target workbooks, rebuilds both assignments with drag-and-drop, opens both confirmed pairs, waits for separate automatic field proposals, bulk-accepts and validates every rule, and previews both persisted implementations. Console and uncaught page errors fail the run.
