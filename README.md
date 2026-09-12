# Financial Data Standardization Workbench

A runnable React + FastAPI prototype that converts customer financial data into a user-supplied standardized format.

The workbench always starts with **two upload sections**:

1. **Source data** — a CSV or Excel document with arbitrary customer headers, layouts and naming.
2. **Target example** — a CSV or Excel document whose headers and example values define the expected standardized output format.

For CSV, each document is one table. For Excel (`.xlsx`, `.xlsm` or `.xls`), every non-empty sheet is treated as a separate table. The parser scores candidate header rows and contiguous column spans, so title rows, leading blank rows/columns and trailing notes do not have to be removed first. DeepSeek then suggests which source table belongs with each target table. The user confirms or changes those table pairs before any field-level mapping begins.

The model only proposes constrained JSON mapping rules. The Python backend validates those rules and runs them through an allow-listed Pandas runtime; model output is never executed as Python. Wide schemas are supported up to 500 columns, with initial AI proposals automatically batched in groups of 30 fields.

## Repository layout

- `frontend/` — React 19 application built with Vite.
- `backend/` — FastAPI API, layout-aware document parsers, DeepSeek adapter, mapping validator, deterministic Pandas runtime and SQLite persistence.
- `examples/` — synthetic CSV and multi-sheet Excel source/target pairs for browser verification.
- `docs/DESIGN.md` — implemented workflow, architecture and trust boundaries.
- `Financial_Data_Standardization_Workbench_PRD_HLD.docx` — original product and high-level design document.

## Provider configuration

The API configuration follows the same backend-only pattern as Football Legend:

- `DEEPSEEK_BASE_URL=https://api.deepseek.com`
- `DEEPSEEK_MODEL=deepseek-v4-flash`
- `DEEPSEEK_THINKING=enabled`
- `DEEPSEEK_API_KEY` has priority when injected by a secret manager.
- Otherwise the backend reads the trimmed contents of `DEEPSEEK_API_KEY_FILE`.
- For local development, an ignored `llm_key.txt` in this repository is detected automatically.
- `WORKBENCH_DB_PATH` optionally overrides the default ignored `backend/runtime/workbench.db` SQLite database.

Never put the key in `.env`, a `VITE_*` variable, browser code, logs or an image layer. See `.env.example` for non-secret settings.

## Fastest run: Docker

From the repository root:

```powershell
docker compose up --build
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). Compose mounts `llm_key.txt` read-only into the API container.

## Local development

Terminal 1:

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Terminal 2:

```powershell
cd frontend
pnpm install
pnpm run dev
```

Open [http://127.0.0.1:5173](http://127.0.0.1:5173). Vite proxies `/api` to the Python service.

## Use the workbench

1. In **Source data**, choose the customer CSV or Excel document.
2. In **Target example**, choose the example of the standardized CSV or Excel output.
3. Select **Continue to table matching**. Both files are required.
4. DeepSeek automatically suggests one source table for every target table when the pairing board opens.
5. Drag source cards onto target cards to add or change assignments, use **Remove** to clear one, or use the dropdown as a keyboard-friendly alternative.
6. If the initial table proposal needs rethinking, write a table-level comment and select **Regenerate from comment**. Then confirm the complete set of pairs.
7. DeepSeek automatically generates the first field proposal when each confirmed pair is opened. Accept one rule, select several with checkboxes, use **Accept selected**, or use **Accept all**. **Select all** and **Clear all** keep large schemas manageable.
8. Resolve validation blockers for every table pair, then select **Save & review implementation**.
9. The full implementation page previews each persisted mapping definition and its deterministic Pandas code. These are stored in SQLite and remain available after a page refresh; the UI does not offer mapping/code downloads by default.

Only masked representative profile values—not complete uploaded files—are sent to DeepSeek. Thinking is enabled for both table-level and field-level suggestions.

## Mock CSV pair

Use these together on the first screen:

- Source data: `examples/source_financial_ledger.csv`
- Target example: `examples/target_standardized_example.csv`

Both files are synthetic and contain no customer data.

For multi-sheet verification, use these together:

- Source data: `examples/source_multisheet_financial_data.xlsx` (`Financial Ledger`, `Account Register`).
- Target example: `examples/target_multisheet_standardized_example.xlsx` (`Standard P&L`, `Account Master`).

For layout-detection verification, use these together:

- Source data: `examples/source_offset_financial_ledger.csv` (header at `C4`, with preamble and footer).
- Target example: `examples/target_offset_standardized_example.csv` (header at `B3`, with preamble and footer).

## Persisted implementation

Saving writes a single implementation record per session to SQLite. It contains every authoritative reviewed definition, detected source/target range, validation result, preview/lineage data and generated Pandas script. The fourth workflow page lets the user switch between table pairs and inspect the mapping JSON beside its code. Docker Compose persists `backend/runtime` in the `workbench-runtime` volume.

## Checks

```powershell
cd frontend
pnpm run build

cd ..\backend
python -m pip install -r requirements-dev.txt
pytest
```

The test suite covers paired uploads, offset table-range detection, multi-sheet Excel parsing, AI table confirmation, individual and bulk field review, validation, generated code and SQLite reload persistence. It uses an in-memory fake provider, so tests do not spend provider credits or read the local credential.

With the backend running on port 8000 and DeepSeek configured, run the complete browser workflow in an installed Chrome or Edge browser:

```powershell
cd frontend
pnpm run test:e2e
```

This live test first completes the offset CSV workflow, including detected ranges, a table comment, field revision, checkbox multi-selection, clear/select/accept-all controls, validation, implementation preview and database-backed reload. It then uploads the two-sheet Excel pair, removes and drag-drops both assignments, verifies automatic field proposals and pair switching, bulk-accepts and validates every rule, and previews both persisted implementations. Browser console errors fail the run. It makes live provider calls.
