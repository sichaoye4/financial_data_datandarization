from __future__ import annotations

import base64
import binascii
import copy
import csv
import io
import json
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .ai_provider import DeepSeekMappingProvider, ProviderError
from .config import get_settings


APP_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIST = APP_ROOT / "frontend" / "dist"
RUNTIME_ROOT = APP_ROOT / "backend" / "runtime"
RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TABLES_PER_DOCUMENT = 20
MAX_COLUMNS_PER_TABLE = 500
FIELD_AI_BATCH_SIZE = 30
SUPPORTED_EXTENSIONS = {".csv", ".xlsx", ".xls", ".xlsm"}
ALLOWED_OPERATIONS = {"string", "date_parse", "coalesce", "direct", "aggregate"}
ALLOWED_AGGREGATES = {"sum", "count", "min", "max", "mean"}


class FileUploadPayload(BaseModel):
    filename: str = Field(min_length=1, max_length=200)
    content_base64: str = Field(min_length=1, max_length=12_000_000)


class PairedUploadRequest(BaseModel):
    source_file: FileUploadPayload
    target_file: FileUploadPayload


class TableProposalRequest(BaseModel):
    comment: str = Field(default="", max_length=1_000)


class ConfirmedTableLink(BaseModel):
    source_table_id: str
    target_table_id: str


class TableMappingConfirmation(BaseModel):
    mappings: list[ConfirmedTableLink] = Field(min_length=1, max_length=MAX_TABLES_PER_DOCUMENT)


class RevisionRequest(BaseModel):
    field: str
    comment: str = Field(min_length=3, max_length=1_000)
    base_revision: int = 0


class BulkFieldAcceptanceRequest(BaseModel):
    fields: list[str] = Field(default_factory=list, max_length=MAX_COLUMNS_PER_TABLE)
    accept_all: bool = False


@dataclass
class UploadedTable:
    id: str
    role: str
    filename: str
    name: str
    frame: pd.DataFrame
    detected_region: dict[str, Any]


@dataclass
class FieldMappingState:
    id: str
    source_table_id: str
    target_table_id: str
    target_schema: dict[str, Any]
    definition: dict[str, Any]
    revision: int = 0
    revisions: list[dict[str, Any]] = field(default_factory=list)
    pending: dict[str, dict[str, Any]] = field(default_factory=dict)
    ai_mode: str = "awaiting live DeepSeek field proposal"
    provider_metadata: dict[str, Any] | None = None


@dataclass
class MappingSession:
    id: str
    source_filename: str
    target_filename: str
    source_tables: dict[str, UploadedTable]
    target_tables: dict[str, UploadedTable]
    table_mapping_proposal: list[dict[str, Any]] = field(default_factory=list)
    table_mapping_confirmed: bool = False
    table_mappings: dict[str, FieldMappingState] = field(default_factory=dict)
    active_mapping_id: str | None = None
    table_provider_metadata: dict[str, Any] | None = None
    published_at: str | None = None


SESSIONS: dict[str, MappingSession] = {}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_filename(filename: str) -> str:
    name = Path(filename).name
    return re.sub(r"[^A-Za-z0-9._-]", "_", name) or "document"


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]", "_", value).strip("_")
    return slug[:60] or "table"


def clean_numeric(series: pd.Series) -> pd.Series:
    text = series.astype("string").fillna("").str.strip()
    text = text.str.replace(",", "", regex=False).str.replace("$", "", regex=False)
    return text.str.replace("(", "-", regex=False).str.replace(")", "", regex=False)


def infer_type(series: pd.Series) -> str:
    values = series.replace("", pd.NA).dropna()
    if values.empty:
        return "unknown"
    numeric_ratio = pd.to_numeric(clean_numeric(values), errors="coerce").notna().mean()
    date_ratio = max(
        pd.to_datetime(values, format=date_format, errors="coerce").notna().mean()
        for date_format in ("%d/%m/%Y", "%Y-%m-%d", "%m/%d/%Y")
    )
    if numeric_ratio >= 0.8:
        return "number"
    if date_ratio >= 0.8:
        return "date"
    return "string"


def semantic_type(column_name: str, inferred: str) -> str:
    if inferred != "unknown":
        return inferred
    normalized = column_name.lower()
    if any(token in normalized for token in ("date", "period", "as_of", "asof")):
        return "date"
    if any(
        token in normalized
        for token in ("amount", "revenue", "expense", "profit", "balance", "total", "value")
    ):
        return "number"
    return "string"


def profile_source(frame: pd.DataFrame) -> dict[str, Any]:
    columns = []
    for column in frame.columns:
        values = frame[column].astype("string")
        null_ratio = float((values.fillna("") == "").mean()) if len(values) else 0.0
        samples = [str(item) for item in values.fillna("").unique().tolist()[:3]]
        lower = str(column).lower()
        sensitive = any(
            token in lower
            for token in (
                "name",
                "account",
                "phone",
                "email",
                "id",
                "cif",
                "registration",
                "contact",
                "reference",
            )
        )
        columns.append(
            {
                "name": str(column),
                "inferred_type": infer_type(values),
                "null_ratio": round(null_ratio, 3),
                "unique_count": int(values.nunique(dropna=True)),
                "sample_values": samples,
                "sensitive_looking": sensitive,
            }
        )
    probable_grain = [
        column["name"]
        for column in columns
        if len(frame) and column["unique_count"] >= max(1, int(len(frame) * 0.8))
    ][:3]
    return {
        "row_count": int(len(frame)),
        "columns": columns,
        "probable_grain": probable_grain,
        "masked_for_model": True,
    }


def masked_model_profile(frame: pd.DataFrame) -> dict[str, Any]:
    profile = profile_source(frame)
    for column in profile["columns"]:
        if column["sensitive_looking"]:
            column["sample_values"] = ["[MASKED]" for _ in column["sample_values"]]
    return profile


def to_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    return json.loads(frame.to_json(orient="records", date_format="iso"))


def decode_upload(payload: FileUploadPayload) -> bytes:
    try:
        raw = base64.b64decode(payload.content_base64, validate=True)
    except (binascii.Error, ValueError) as error:
        raise HTTPException(status_code=422, detail=f"{payload.filename} is not valid upload data.") from error
    if not raw:
        raise HTTPException(status_code=422, detail=f"{payload.filename} is empty.")
    if len(raw) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail=f"{payload.filename} exceeds the 8 MB prototype limit.")
    return raw


def validate_table(frame: pd.DataFrame, *, label: str, allow_empty_rows: bool) -> pd.DataFrame:
    frame.columns = [str(column).strip() for column in frame.columns]
    if not len(frame.columns) or any(not column for column in frame.columns):
        raise HTTPException(status_code=422, detail=f"{label} has no usable column headers.")
    if len(set(frame.columns)) != len(frame.columns):
        raise HTTPException(status_code=422, detail=f"{label} has duplicate column headers.")
    if len(frame.columns) > MAX_COLUMNS_PER_TABLE:
        raise HTTPException(
            status_code=422,
            detail=f"{label} exceeds {MAX_COLUMNS_PER_TABLE} columns.",
        )
    if frame.empty and not allow_empty_rows:
        raise HTTPException(status_code=422, detail=f"{label} has no data rows.")
    return frame.astype("string").fillna("")


def cell_text(value: Any) -> str:
    if value is None or bool(pd.isna(value)):
        return ""
    return str(value).strip()


def excel_column_name(index: int) -> str:
    value = index + 1
    letters = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def populated_spans(row: pd.Series) -> list[tuple[int, int]]:
    populated = [index for index, value in enumerate(row.tolist()) if cell_text(value)]
    if not populated:
        return []
    spans: list[tuple[int, int]] = []
    start = previous = populated[0]
    for index in populated[1:]:
        if index != previous + 1:
            spans.append((start, previous))
            start = index
        previous = index
    spans.append((start, previous))
    return spans


def header_label_score(values: list[str]) -> float:
    label_pattern = re.compile(r"^[A-Za-z][A-Za-z0-9 _./()&%#-]{0,79}$")
    return sum(bool(label_pattern.fullmatch(value)) for value in values) / len(values)


def detect_table_region(
    raw_frame: pd.DataFrame,
    *,
    label: str,
    allow_empty_rows: bool,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Find a rectangular table without assuming A1 is its header.

    Candidates are contiguous, unique, text-like header cells. Width, nearby row
    density and downstream support dominate the score, which makes title blocks
    and workbook notes lose to the actual table header.
    """

    if raw_frame.empty or not len(raw_frame.columns):
        raise HTTPException(status_code=422, detail=f"{label} contains no usable table region.")
    raw_frame = raw_frame.copy().fillna("")
    candidates: list[tuple[float, int, int, int]] = []
    max_rows = min(len(raw_frame), 50)

    for row_index in range(max_rows):
        row = raw_frame.iloc[row_index]
        for start_column, end_column in populated_spans(row):
            width = end_column - start_column + 1
            if width > MAX_COLUMNS_PER_TABLE:
                continue
            headers = [cell_text(value) for value in row.iloc[start_column : end_column + 1]]
            if len(set(headers)) != width:
                continue
            supported_rows = 0
            coverage_total = 0.0
            for data_index in range(row_index + 1, min(len(raw_frame), row_index + 26)):
                values = raw_frame.iloc[data_index, start_column : end_column + 1]
                occupied = sum(bool(cell_text(value)) for value in values)
                coverage = occupied / width
                if occupied >= (1 if width == 1 else 2):
                    supported_rows += 1
                    coverage_total += coverage
                elif supported_rows:
                    break
            average_coverage = coverage_total / supported_rows if supported_rows else 0.0
            score = (
                width * 10
                + min(supported_rows, 10) * 8
                + average_coverage * 5
                + header_label_score(headers) * 4
                - row_index * 0.01
            )
            candidates.append((score, row_index, start_column, end_column))

    if not candidates:
        raise HTTPException(
            status_code=422,
            detail=f"{label} has no detectable header row.",
        )

    _, header_index, start_column, end_column = max(candidates, key=lambda item: item[0])
    headers = [
        cell_text(value)
        for value in raw_frame.iloc[header_index, start_column : end_column + 1]
    ]
    width = len(headers)
    last_data_index = header_index
    data_indices: list[int] = []
    for row_index in range(header_index + 1, len(raw_frame)):
        values = raw_frame.iloc[row_index, start_column : end_column + 1]
        occupied = sum(bool(cell_text(value)) for value in values)
        if occupied < (1 if width == 1 else 2):
            break
        data_indices.append(row_index)
        last_data_index = row_index

    if not data_indices and not allow_empty_rows:
        raise HTTPException(status_code=422, detail=f"{label} has a header but no data rows.")

    if data_indices:
        frame = raw_frame.iloc[data_indices, start_column : end_column + 1].copy()
    else:
        frame = pd.DataFrame(columns=range(width))
    frame.columns = headers
    frame.reset_index(drop=True, inplace=True)
    start_name = excel_column_name(start_column)
    end_name = excel_column_name(end_column)
    region = {
        "header_row": header_index + 1,
        "data_start_row": header_index + 2 if data_indices else None,
        "data_end_row": last_data_index + 1 if data_indices else None,
        "start_column": start_name,
        "end_column": end_name,
        "start_column_index": start_column,
        "end_column_index": end_column,
        "range": f"{start_name}{header_index + 1}:{end_name}{last_data_index + 1}",
        "method": "density-and-header-score",
    }
    return validate_table(frame, label=label, allow_empty_rows=allow_empty_rows), region


def parse_document(payload: FileUploadPayload, *, role: str) -> dict[str, UploadedTable]:
    filename = safe_filename(payload.filename)
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise HTTPException(status_code=422, detail="Upload CSV, XLSX, XLSM or XLS files only.")
    raw = decode_upload(payload)
    tables: dict[str, UploadedTable] = {}

    if suffix == ".csv":
        try:
            text = raw.decode("utf-8-sig")
            rows = list(csv.reader(io.StringIO(text)))
            width = max((len(row) for row in rows), default=0)
            frame = pd.DataFrame([row + [""] * (width - len(row)) for row in rows])
        except Exception as error:
            raise HTTPException(status_code=422, detail=f"{filename} could not be parsed as UTF-8 CSV.") from error
        table_id = f"{role}-1-{safe_slug(Path(filename).stem)}"
        frame, region = detect_table_region(
            frame,
            label=filename,
            allow_empty_rows=role == "target",
        )
        tables[table_id] = UploadedTable(
            table_id, role, filename, Path(filename).stem, frame, region
        )
        return tables

    try:
        workbook = pd.ExcelFile(io.BytesIO(raw))
    except Exception as error:
        raise HTTPException(status_code=422, detail=f"{filename} could not be opened as an Excel workbook.") from error
    if len(workbook.sheet_names) > MAX_TABLES_PER_DOCUMENT:
        raise HTTPException(status_code=422, detail="The prototype supports at most 20 sheets per workbook.")
    for index, sheet_name in enumerate(workbook.sheet_names, start=1):
        try:
            frame = pd.read_excel(
                workbook,
                sheet_name=sheet_name,
                header=None,
                dtype=object,
                keep_default_na=False,
            )
        except Exception as error:
            raise HTTPException(status_code=422, detail=f"Sheet {sheet_name} could not be read.") from error
        if not len(frame.columns) or not frame.map(lambda value: bool(cell_text(value))).to_numpy().any():
            continue
        table_id = f"{role}-{index}-{safe_slug(sheet_name)}"
        frame, region = detect_table_region(
            frame,
            label=f"{filename} / {sheet_name}",
            allow_empty_rows=role == "target",
        )
        tables[table_id] = UploadedTable(table_id, role, filename, sheet_name, frame, region)
    if not tables:
        raise HTTPException(status_code=422, detail=f"{filename} contains no usable {role} tables.")
    return tables


def infer_target_grain(frame: pd.DataFrame) -> list[str]:
    columns = [str(column) for column in frame.columns]
    identifier = next(
        (
            column
            for column in columns
            if any(
                token in column.lower()
                for token in ("customer", "account", "client", "cif", "_id", "reference", "_ref")
            )
        ),
        None,
    )
    period = next(
        (
            column
            for column in columns
            if any(token in column.lower() for token in ("date", "period", "as_of", "asof"))
        ),
        None,
    )
    semantic = [column for column in (identifier, period) if column]
    if semantic:
        return list(dict.fromkeys(semantic))
    probable = profile_source(frame)["probable_grain"]
    return probable[:2] or columns[:1]


def make_target_schema(table: UploadedTable) -> dict[str, Any]:
    profile = profile_source(table.frame)
    inferred = {item["name"]: item["inferred_type"] for item in profile["columns"]}
    return {
        "id": f"uploaded-{table.id}",
        "name": table.name,
        "table_id": table.id,
        "grain": infer_target_grain(table.frame),
        "fields": [
            {
                "name": str(column),
                "label": str(column).replace("_", " ").strip().title(),
                "type": semantic_type(str(column), inferred[str(column)]),
                "required": True,
            }
            for column in table.frame.columns
        ],
    }


def make_definition(
    source_table: UploadedTable,
    target_table: UploadedTable,
    target_schema: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "2.0",
        "definition_id": f"table-map-{uuid.uuid4().hex[:8]}",
        "name": f"{source_table.name} to {target_table.name}",
        "source": {
            "document": source_table.filename,
            "table_id": source_table.id,
            "table_name": source_table.name,
            "detected_region": source_table.detected_region,
            "grain": [],
        },
        "target": {
            "document": target_table.filename,
            "schema_id": target_schema["id"],
            "table_id": target_table.id,
            "table_name": target_table.name,
            "detected_region": target_table.detected_region,
            "grain": target_schema["grain"],
        },
        "steps": [
            {
                "id": f"s{index}",
                "target": target_field["name"],
                "op": "unmapped",
                "inputs": [],
                "review": {
                    "status": "empty",
                    "locked": False,
                    "confidence": 0.0,
                    "rationale": "Generate a live AI field proposal after confirming the table match.",
                },
            }
            for index, target_field in enumerate(target_schema["fields"], start=1)
        ],
        "publication": {"version": 0, "status": "working"},
    }


def table_summary(table: UploadedTable) -> dict[str, Any]:
    return {
        "id": table.id,
        "name": table.name,
        "filename": table.filename,
        "row_count": int(len(table.frame)),
        "column_count": int(len(table.frame.columns)),
        "columns": [str(column) for column in table.frame.columns],
        "detected_region": table.detected_region,
        "profile": profile_source(table.frame),
        "rows": to_records(table.frame.head(8)),
    }


def table_mapping_system_prompt() -> str:
    return (
        "You map uploaded source tables to uploaded target example tables for financial data standardization. "
        "Return JSON only. Map every target table exactly once to the single best source table. Use only "
        "the supplied table IDs, do not invent tables, and keep rationale concise."
    )


def table_mapping_prompt(session: MappingSession, comment: str = "") -> str:
    return json.dumps(
        {
            "command": "propose_table_mapping",
            "user_comment": comment.strip(),
            "source_tables": [
                {"id": table.id, "name": table.name, "profile": masked_model_profile(table.frame)}
                for table in session.source_tables.values()
            ],
            "target_tables": [
                {"id": table.id, "name": table.name, "profile": masked_model_profile(table.frame)}
                for table in session.target_tables.values()
            ],
            "output_contract": {
                "mappings": [
                    {
                        "source_table_id": "exact supplied source table id",
                        "target_table_id": "exact supplied target table id",
                        "confidence": 0.0,
                        "rationale": "short explanation based on names, columns and types",
                    }
                ]
            },
        },
        ensure_ascii=False,
        sort_keys=True,
    )


async def generate_json_with_retry(
    settings: Any,
    *,
    system_prompt: str,
    user_prompt: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Retry once only when a provider response cannot be decoded as JSON."""

    try:
        output, metadata = await DeepSeekMappingProvider(settings).generate_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        metadata["retry_count"] = 0
        return output, metadata
    except ProviderError as error:
        if error.category != "invalid_json":
            raise
    output, metadata = await DeepSeekMappingProvider(settings).generate_json(
        system_prompt=f"{system_prompt} Your previous response was not parseable. Return one JSON object only, without Markdown fences or commentary.",
        user_prompt=user_prompt,
    )
    metadata["retry_count"] = 1
    return output, metadata


async def generate_validated_json_with_retry(
    settings: Any,
    *,
    system_prompt: str,
    user_prompt: str,
    validator: Any,
) -> tuple[dict[str, Any], Any, dict[str, Any]]:
    """Retry once when syntactically valid provider JSON violates the mapping contract."""

    output, metadata = await generate_json_with_retry(
        settings,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )
    try:
        validated = validator(output)
    except ValueError as validation_error:
        retry_system_prompt = (
            f"{system_prompt} Your previous JSON was rejected by deterministic validation: "
            f"{str(validation_error)[:300]}. Correct that problem and return the complete JSON object again."
        )
        retry_output, retry_metadata = await generate_json_with_retry(
            settings,
            system_prompt=retry_system_prompt,
            user_prompt=user_prompt,
        )
        retry_metadata["retry_count"] = (
            int(metadata.get("retry_count", 0))
            + 1
            + int(retry_metadata.get("retry_count", 0))
        )
        retry_metadata["validation_retry_reason"] = str(validation_error)[:300]
        return retry_output, validator(retry_output), retry_metadata
    return output, validated, metadata


def sanitize_table_mapping(session: MappingSession, output: dict[str, Any]) -> list[dict[str, Any]]:
    raw_mappings = output.get("mappings")
    if not isinstance(raw_mappings, list):
        raise ValueError("provider output did not include table mappings")
    source_ids = set(session.source_tables)
    target_ids = set(session.target_tables)
    by_target: dict[str, dict[str, Any]] = {}
    for raw in raw_mappings:
        if not isinstance(raw, dict):
            raise ValueError("a table mapping is not an object")
        source_id = raw.get("source_table_id")
        target_id = raw.get("target_table_id")
        if source_id not in source_ids or target_id not in target_ids:
            raise ValueError("a table mapping references an unknown table")
        if target_id in by_target:
            raise ValueError("a target table was mapped more than once")
        confidence = raw.get("confidence", 0.7)
        confidence = float(confidence) if isinstance(confidence, (int, float)) else 0.7
        by_target[target_id] = {
            "id": f"tm-{len(by_target) + 1}",
            "source_table_id": source_id,
            "target_table_id": target_id,
            "confidence": max(0.0, min(1.0, confidence)),
            "rationale": str(raw.get("rationale") or "DeepSeek matched table structure and meaning.")[:400],
            "review": {"status": "proposed", "confirmed": False},
        }
    missing = target_ids - set(by_target)
    if missing:
        raise ValueError(f"provider omitted target tables: {', '.join(sorted(missing))}")
    return [by_target[target_id] for target_id in session.target_tables]


def mapping_system_prompt() -> str:
    return (
        "You are the Financial Data Standardization Workbench field-mapping assistant. The user already "
        "confirmed the source-table to target-table match. Return JSON only. Propose declarative rules and "
        "never write or execute code. Allowed operations are string, date_parse, coalesce, direct and "
        "aggregate. Aggregate functions are sum, count, min, max and mean. Filters may only use eq. Every "
        "filter must be shaped as {column, operator: 'eq', value}. Date formats must use Python strptime "
        "tokens such as %d/%m/%Y. Every aggregate must use the exact target grain. Use only supplied source "
        "columns and applicable keys."
    )


def initial_mapping_prompt(
    session: MappingSession,
    state: FieldMappingState,
    targets: list[str] | None = None,
) -> str:
    source = session.source_tables[state.source_table_id]
    unlocked_targets = targets or [
        step["target"] for step in state.definition["steps"] if not step.get("review", {}).get("locked")
    ]
    requested = set(unlocked_targets)
    target_schema = {
        **state.target_schema,
        "fields": [field for field in state.target_schema["fields"] if field["name"] in requested],
    }
    return json.dumps(
        {
            "command": "propose_initial_definition",
            "confirmed_table_mapping": {
                "source_table_id": state.source_table_id,
                "target_table_id": state.target_table_id,
            },
            "target_schema": target_schema,
            "source_profile": masked_model_profile(source.frame),
            "unlocked_targets": unlocked_targets,
            "current_steps": [
                step for step in state.definition["steps"] if step["target"] in requested
            ],
            "output_contract": {
                "steps": [
                    {
                        "target": "exact target field",
                        "op": "allowed operation",
                        "function": "only for string or aggregate",
                        "inputs": [{"column": "existing source column"}],
                        "params": {"format": "Python strptime format; use default instead for coalesce"},
                        "where": {
                            "column": "existing source column",
                            "operator": "eq",
                            "value": "literal value",
                        },
                        "group_by": "only for aggregate; exact target grain",
                        "confidence": 0.0,
                        "rationale": "short explanation",
                    }
                ]
            },
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def revision_mapping_prompt(
    session: MappingSession,
    state: FieldMappingState,
    request: RevisionRequest,
) -> str:
    source = session.source_tables[state.source_table_id]
    current = next(step for step in state.definition["steps"] if step["target"] == request.field)
    return json.dumps(
        {
            "command": "propose_revision_patch",
            "confirmed_table_mapping": {
                "source_table_id": state.source_table_id,
                "target_table_id": state.target_table_id,
            },
            "selected_target": request.field,
            "comment": request.comment,
            "target_schema": state.target_schema,
            "source_profile": masked_model_profile(source.frame),
            "current_rule": current,
            "output_contract": {
                "step": {
                    "target": request.field,
                    "op": "allowed operation",
                    "function": "only when applicable",
                    "inputs": [{"column": "existing source column"}],
                    "params": {"format": "Python strptime format; use default instead for coalesce"},
                    "where": {
                        "column": "existing source column",
                        "operator": "eq",
                        "value": "literal value",
                    },
                    "group_by": "only for aggregate; exact target grain",
                    "confidence": 0.0,
                    "rationale": "short explanation",
                },
                "explanation": "short user-facing explanation",
            },
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def target_field_type(target_schema: dict[str, Any], target: str) -> str:
    return next(field["type"] for field in target_schema["fields"] if field["name"] == target)


def normalize_date_format(value: str) -> str:
    aliases = {
        "dd/MM/yyyy": "%d/%m/%Y",
        "yyyy-MM-dd": "%Y-%m-%d",
        "MM/dd/yyyy": "%m/%d/%Y",
    }
    normalized = aliases.get(value, value)
    if normalized not in {"%d/%m/%Y", "%Y-%m-%d", "%m/%d/%Y"}:
        raise ValueError("date_parse format is not supported")
    return normalized


def normalize_where(where: object, source_columns: set[str]) -> dict[str, Any]:
    if not isinstance(where, dict):
        raise ValueError("suggested filter is not allowed")
    if set(where) == {"column", "operator", "value"}:
        column = where.get("column")
        operator = where.get("operator")
        value = where.get("value")
    elif len(where) == 1:
        column, predicate = next(iter(where.items()))
        if not isinstance(predicate, dict) or set(predicate) != {"eq"}:
            raise ValueError("suggested filter is not allowed")
        operator = "eq"
        value = predicate["eq"]
    else:
        raise ValueError("suggested filter is not allowed")
    if (
        not isinstance(column, str)
        or column not in source_columns
        or operator != "eq"
        or not isinstance(value, (str, int, float))
    ):
        raise ValueError("suggested filter is not allowed")
    return {"column": column, "operator": "eq", "value": value}


def sanitize_ai_step(
    raw_step: object,
    *,
    current_step: dict[str, Any],
    source_columns: set[str],
    target_schema: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(raw_step, dict):
        raise ValueError("suggested step is not an object")
    target = raw_step.get("target")
    operation = raw_step.get("op")
    if target != current_step["target"] or operation not in ALLOWED_OPERATIONS:
        raise ValueError("suggested target or operation is not allowed")
    field_type = target_field_type(target_schema, str(target))
    compatible = {
        "string": {"direct", "string", "coalesce"},
        "date": {"direct", "date_parse", "coalesce"},
        "number": {"direct", "aggregate", "coalesce"},
    }
    if operation not in compatible[field_type]:
        raise ValueError("suggested operation is incompatible with the target type")

    inputs = raw_step.get("inputs")
    if not isinstance(inputs, list) or not inputs or not all(isinstance(item, dict) for item in inputs):
        raise ValueError("suggested inputs are invalid")
    normalized_inputs = []
    for item in inputs:
        column = item.get("column")
        if not isinstance(column, str) or column not in source_columns:
            raise ValueError("suggested input references an unknown source column")
        normalized_inputs.append({"column": column})
    if operation != "coalesce" and len(normalized_inputs) != 1:
        raise ValueError("this operation requires exactly one source column")

    sanitized: dict[str, Any] = {
        "id": current_step["id"],
        "target": target,
        "op": operation,
        "inputs": normalized_inputs,
    }
    function = raw_step.get("function")
    if operation == "aggregate":
        if function == "avg":
            function = "mean"
        if function not in ALLOWED_AGGREGATES:
            raise ValueError("suggested aggregate is not allowed")
        if raw_step.get("group_by") != target_schema["grain"]:
            raise ValueError("suggested aggregate does not match the target grain")
        sanitized["function"] = function
        sanitized["group_by"] = target_schema["grain"]
    elif operation == "string":
        if function != "trim":
            raise ValueError("only trim is supported by the string operator")
        sanitized["function"] = function
    elif operation == "date_parse":
        params = raw_step.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("format"), str):
            raise ValueError("date_parse requires an explicit format")
        sanitized["params"] = {"format": normalize_date_format(params["format"])}
    elif operation == "coalesce":
        params = raw_step.get("params")
        if isinstance(params, dict) and isinstance(params.get("default"), (str, int, float)):
            sanitized["params"] = {"default": params["default"]}

    where = raw_step.get("where")
    if where:
        sanitized["where"] = normalize_where(where, source_columns)

    raw_confidence = raw_step.get("confidence", 0.7)
    confidence = float(raw_confidence) if isinstance(raw_confidence, (int, float)) else 0.7
    sanitized["review"] = {
        "status": "proposed",
        "locked": False,
        "confidence": max(0.0, min(1.0, confidence)),
        "rationale": str(raw_step.get("rationale") or "AI-proposed mapping.")[:300],
    }
    return sanitized


def apply_ai_initial_proposal(
    session: MappingSession,
    state: FieldMappingState,
    output: dict[str, Any],
    required_targets: set[str] | None = None,
    base_definition: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw_steps = output.get("steps")
    if not isinstance(raw_steps, list):
        raise ValueError("provider output did not include steps")
    suggestions = {
        step.get("target"): step
        for step in raw_steps
        if isinstance(step, dict) and isinstance(step.get("target"), str)
    }
    candidate = copy.deepcopy(base_definition or state.definition)
    source_columns = set(session.source_tables[state.source_table_id].frame.columns)
    required_targets = required_targets or {
        step["target"] for step in candidate["steps"] if not step.get("review", {}).get("locked")
    }
    if not required_targets.issubset(suggestions):
        missing = sorted(required_targets - set(suggestions))
        raise ValueError(f"provider omitted target mappings: {', '.join(missing)}")
    for index, current in enumerate(candidate["steps"]):
        if current.get("review", {}).get("locked") or current["target"] not in required_targets:
            continue
        candidate["steps"][index] = sanitize_ai_step(
            suggestions[current["target"]],
            current_step=current,
            source_columns=source_columns,
            target_schema=state.target_schema,
        )
    return candidate


def condition(frame: pd.DataFrame, where: dict[str, Any] | None) -> pd.Series:
    if not where:
        return pd.Series(True, index=frame.index)
    if where.get("operator") != "eq":
        raise HTTPException(status_code=422, detail="Only the eq filter is supported.")
    column = where.get("column")
    if column not in frame.columns:
        return pd.Series(False, index=frame.index)
    return frame[column].astype("string").eq(str(where.get("value", "")))


def evaluate_step(
    frame: pd.DataFrame,
    step: dict[str, Any],
    target_schema: dict[str, Any],
) -> pd.Series:
    operation = step["op"]
    values = frame[step["inputs"][0]["column"]]
    if operation == "string":
        result = values.astype("string").str.strip()
    elif operation == "date_parse":
        result = pd.to_datetime(
            values,
            format=step.get("params", {}).get("format"),
            errors="coerce",
        ).dt.strftime("%Y-%m-%d")
    elif operation == "coalesce":
        result = values.astype("string").replace("", pd.NA)
        for extra in step["inputs"][1:]:
            result = result.fillna(frame[extra["column"]].astype("string").replace("", pd.NA))
        result = result.fillna(step.get("params", {}).get("default"))
    elif operation in {"direct", "aggregate"}:
        result = values
    else:
        raise HTTPException(status_code=422, detail=f"Unsupported transformation: {operation}")

    field_type = target_field_type(target_schema, step["target"])
    if field_type == "number":
        result = pd.to_numeric(clean_numeric(result), errors="coerce")
    elif field_type == "string":
        result = result.astype("string")
    if step.get("where"):
        result = result.where(condition(frame, step["where"]))
    return result


def empty_target_frame(target_schema: dict[str, Any]) -> pd.DataFrame:
    return pd.DataFrame(columns=[field["name"] for field in target_schema["fields"]])


def run_definition(
    source: pd.DataFrame,
    definition: dict[str, Any],
    target_schema: dict[str, Any],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    mapped_steps = [step for step in definition["steps"] if step.get("op") != "unmapped"]
    mapped_targets = {step["target"] for step in mapped_steps}
    grain = target_schema["grain"]
    if not set(grain).issubset(mapped_targets):
        return empty_target_frame(target_schema), {
            "source_rows": int(len(source)),
            "target_rows": 0,
            "row_count_change": -int(len(source)),
            "duplicate_source_grain_rows": 0,
        }

    work = source.copy()
    for step in mapped_steps:
        if step["op"] != "aggregate":
            work[step["target"]] = evaluate_step(work, step, target_schema)
    output = work[grain].drop_duplicates().reset_index(drop=True)
    duplicate_source_grain_rows = int(work.duplicated(grain, keep=False).sum())

    for step in mapped_steps:
        target = step["target"]
        if target in grain:
            continue
        candidate = work[grain].copy()
        candidate[target] = evaluate_step(work, step, target_schema)
        candidate = candidate.replace("", pd.NA).dropna(subset=[target])
        if step["op"] == "aggregate":
            aggregate = step.get("function", "sum")
            if aggregate not in ALLOWED_AGGREGATES:
                raise HTTPException(status_code=422, detail=f"Unsupported aggregate: {aggregate}")
            derived = candidate.groupby(grain, dropna=False)[target].agg(aggregate).reset_index()
        else:
            derived = candidate.groupby(grain, dropna=False)[target].first().reset_index()
        output = output.merge(derived, on=grain, how="left")

    for target_field in target_schema["fields"]:
        if target_field["name"] not in output.columns:
            output[target_field["name"]] = pd.NA
    output = output[[field["name"] for field in target_schema["fields"]]]
    return output, {
        "source_rows": int(len(source)),
        "target_rows": int(len(output)),
        "row_count_change": int(len(output) - len(source)),
        "duplicate_source_grain_rows": duplicate_source_grain_rows,
    }


def validate_definition(
    definition: dict[str, Any],
    output: pd.DataFrame,
    metrics: dict[str, Any],
    target_schema: dict[str, Any],
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    for step in definition["steps"]:
        review = step.get("review", {})
        if step.get("op") == "unmapped":
            issues.append(
                {
                    "severity": "error",
                    "code": "MAPPING_REQUIRED",
                    "field": step["target"],
                    "message": "Generate and review a mapping for this target field.",
                }
            )
            continue
        if review.get("status") not in {"accepted", "published"}:
            issues.append(
                {
                    "severity": "error",
                    "code": "REVIEW_REQUIRED",
                    "field": step["target"],
                    "message": "This mapping is not yet accepted by a user.",
                }
            )
        if (
            target_field_type(target_schema, step["target"]) == "number"
            and step["op"] == "direct"
            and metrics.get("duplicate_source_grain_rows", 0) > 0
        ):
            issues.append(
                {
                    "severity": "error",
                    "code": "GRAIN_MISMATCH",
                    "field": step["target"],
                    "message": "Direct numeric mapping loses values because source rows repeat at the target grain.",
                }
            )

    mapped_targets = {
        step["target"] for step in definition["steps"] if step.get("op") != "unmapped"
    }
    for target_field in target_schema["fields"]:
        field_name = target_field["name"]
        if target_field["required"] and field_name in mapped_targets and (
            output.empty or output[field_name].replace("", pd.NA).isna().any()
        ):
            issues.append(
                {
                    "severity": "error",
                    "code": "REQUIRED_VALUE",
                    "field": field_name,
                    "message": "A required target value is missing.",
                }
            )
    duplicate_count = int(output.duplicated(target_schema["grain"]).sum()) if len(output) else 0
    if duplicate_count:
        issues.append(
            {
                "severity": "error",
                "code": "DUPLICATE_GRAIN",
                "field": target_schema["grain"][0],
                "message": f"{duplicate_count} rows duplicate the target grain.",
            }
        )
    blockers = sum(issue["severity"] == "error" for issue in issues)
    warnings = sum(issue["severity"] == "warning" for issue in issues)
    return {
        "issues": issues,
        "blockers": blockers,
        "warnings": warnings,
        "status": "blocked" if blockers else "ready",
        "metrics": metrics,
    }


def lineage(definition: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "target": step["target"],
            "operation": step["op"],
            "inputs": [item["column"] for item in step.get("inputs", [])],
            "group_by": step.get("group_by", []),
            "review": step.get("review", {}),
        }
        for step in definition["steps"]
    ]


def pair_view(
    session: MappingSession,
    state: FieldMappingState,
    definition: dict[str, Any] | None = None,
) -> dict[str, Any]:
    active_definition = definition or state.definition
    source_table = session.source_tables[state.source_table_id]
    target_table = session.target_tables[state.target_table_id]
    preview, metrics = run_definition(source_table.frame, active_definition, state.target_schema)
    validation = validate_definition(active_definition, preview, metrics, state.target_schema)
    return {
        "active_table_mapping_id": state.id,
        "filename": source_table.filename,
        "active_source_table": table_summary(source_table),
        "active_target_table": table_summary(target_table),
        "revision": state.revision,
        "target_schema": state.target_schema,
        "profile": profile_source(source_table.frame),
        "source_rows": to_records(source_table.frame.head(20)),
        "definition": active_definition,
        "preview": {
            "rows": to_records(preview),
            "metrics": metrics,
            "lineage": lineage(active_definition),
        },
        "validation": validation,
        "mode": state.ai_mode,
        "provider_last_call": state.provider_metadata,
        "revision_history": state.revisions,
    }


def session_view(session: MappingSession) -> dict[str, Any]:
    settings = get_settings()
    pair_summaries = []
    total_blockers = 0
    total_warnings = 0
    for state in session.table_mappings.values():
        current = pair_view(session, state)
        validation = current["validation"]
        total_blockers += validation["blockers"]
        total_warnings += validation["warnings"]
        pair_summaries.append(
            {
                "id": state.id,
                "source_table_id": state.source_table_id,
                "source_table_name": session.source_tables[state.source_table_id].name,
                "target_table_id": state.target_table_id,
                "target_table_name": session.target_tables[state.target_table_id].name,
                "revision": state.revision,
                "blockers": validation["blockers"],
                "status": validation["status"],
            }
        )
    payload: dict[str, Any] = {
        "session_id": session.id,
        "stage": (
            "implementation"
            if session.published_at
            else "field_mapping"
            if session.table_mapping_confirmed
            else "table_mapping"
        ),
        "source_document": session.source_filename,
        "target_document": session.target_filename,
        "source_tables": [table_summary(table) for table in session.source_tables.values()],
        "target_tables": [table_summary(table) for table in session.target_tables.values()],
        "table_mapping_proposal": session.table_mapping_proposal,
        "table_mapping_confirmed": session.table_mapping_confirmed,
        "table_pairs": pair_summaries,
        "overall_validation": {
            "blockers": total_blockers,
            "warnings": total_warnings,
            "status": "ready" if pair_summaries and total_blockers == 0 else "blocked",
        },
        "provider": {
            "name": "deepseek",
            "configured": settings.provider_configured,
            "model": settings.deepseek_model,
            "thinking": settings.deepseek_thinking,
            "last_table_call": session.table_provider_metadata,
        },
    }
    if session.active_mapping_id:
        payload.update(pair_view(session, session.table_mappings[session.active_mapping_id]))
    return payload


def require_session(session_id: str) -> MappingSession:
    session = SESSIONS.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Mapping session not found.")
    return session


def require_active_state(session: MappingSession) -> FieldMappingState:
    if not session.table_mapping_confirmed or not session.active_mapping_id:
        raise HTTPException(status_code=409, detail="Confirm the table mapping before reviewing fields.")
    return session.table_mappings[session.active_mapping_id]


def database_path() -> Path:
    configured = os.getenv("WORKBENCH_DB_PATH", "").strip()
    path = Path(configured) if configured else RUNTIME_ROOT / "workbench.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def database_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(database_path())
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS implementations (
            session_id TEXT PRIMARY KEY,
            source_document TEXT NOT NULL,
            target_document TEXT NOT NULL,
            saved_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )
        """
    )
    return connection


def save_implementation(implementation: dict[str, Any]) -> None:
    with database_connection() as connection:
        connection.execute(
            """
            INSERT INTO implementations (
                session_id, source_document, target_document, saved_at, payload_json
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                source_document = excluded.source_document,
                target_document = excluded.target_document,
                saved_at = excluded.saved_at,
                payload_json = excluded.payload_json
            """,
            (
                implementation["session_id"],
                implementation["source_document"],
                implementation["target_document"],
                implementation["saved_at"],
                json.dumps(implementation),
            ),
        )


def load_implementation(session_id: str) -> dict[str, Any]:
    with database_connection() as connection:
        row = connection.execute(
            "SELECT payload_json FROM implementations WHERE session_id = ?", (session_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Saved implementation not found.")
    return json.loads(row[0])


def compile_to_pandas(definition: dict[str, Any], target_schema: dict[str, Any]) -> str:
    definition_literal = json.dumps(definition, indent=2)
    target_types_literal = json.dumps(
        {field["name"]: field["type"] for field in target_schema["fields"]}, indent=2
    )
    target_columns_literal = json.dumps([field["name"] for field in target_schema["fields"]])
    return f'''"""Generated by Financial Data Standardization Workbench.
The embedded JSON definition is authoritative; only fixed operations are dispatched.
"""
import json
import csv
import pandas as pd
from pathlib import Path

DEFINITION = json.loads(r''' + "'''" + definition_literal + "'''" + r''')
TARGET_TYPES = json.loads(r''' + "'''" + target_types_literal + "'''" + r''')
TARGET_COLUMNS = json.loads(r''' + "'''" + target_columns_literal + "'''" + r''')
GRAIN = DEFINITION["target"]["grain"]


def clean_numeric(series):
    text = series.astype("string").fillna("").str.strip()
    text = text.str.replace(",", "", regex=False).str.replace("$", "", regex=False)
    return text.str.replace("(", "-", regex=False).str.replace(")", "", regex=False)


def load_source(source_file):
    path = Path(source_file)
    region = DEFINITION["source"]["detected_region"]
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle))
        width = max((len(row) for row in rows), default=0)
        raw = pd.DataFrame([row + [""] * (width - len(row)) for row in rows])
    else:
        raw = pd.read_excel(
            path,
            sheet_name=DEFINITION["source"]["table_name"],
            header=None,
            dtype=object,
            keep_default_na=False,
        )
    start = region["start_column_index"]
    end = region["end_column_index"] + 1
    header_row = region["header_row"] - 1
    data_end = region["data_end_row"]
    source = raw.iloc[header_row + 1:data_end, start:end].copy()
    source.columns = [str(value).strip() for value in raw.iloc[header_row, start:end]]
    return source.astype("string").fillna("").reset_index(drop=True)


def evaluate(frame, step):
    values = frame[step["inputs"][0]["column"]]
    if step["op"] == "string":
        result = values.astype("string").str.strip()
    elif step["op"] == "date_parse":
        result = pd.to_datetime(values, format=step["params"]["format"], errors="coerce").dt.strftime("%Y-%m-%d")
    elif step["op"] == "coalesce":
        result = values.astype("string").replace("", pd.NA)
        for extra in step["inputs"][1:]:
            result = result.fillna(frame[extra["column"]].astype("string").replace("", pd.NA))
        result = result.fillna(step.get("params", {}).get("default"))
    elif step["op"] in {"direct", "aggregate"}:
        result = values
    else:
        raise ValueError(f"Unsupported operation: {step['op']}")
    if TARGET_TYPES[step["target"]] == "number":
        result = pd.to_numeric(clean_numeric(result), errors="coerce")
    where = step.get("where")
    if where:
        if where["operator"] != "eq":
            raise ValueError("Unsupported filter")
        result = result.where(frame[where["column"]].astype("string").eq(str(where["value"])))
    return result


def run(source_file, target_csv):
    source = load_source(source_file)
    steps = [step for step in DEFINITION["steps"] if step["op"] != "unmapped"]
    for step in steps:
        if step["op"] != "aggregate":
            source[step["target"]] = evaluate(source, step)
    output = source[GRAIN].drop_duplicates().reset_index(drop=True)
    for step in steps:
        if step["target"] in GRAIN:
            continue
        candidate = source[GRAIN].copy()
        candidate[step["target"]] = evaluate(source, step)
        candidate = candidate.replace("", pd.NA).dropna(subset=[step["target"]])
        if step["op"] == "aggregate":
            derived = candidate.groupby(GRAIN, dropna=False)[step["target"]].agg(step["function"]).reset_index()
        else:
            derived = candidate.groupby(GRAIN, dropna=False)[step["target"]].first().reset_index()
        output = output.merge(derived, on=GRAIN, how="left")
    output[TARGET_COLUMNS].to_csv(target_csv, index=False)


if __name__ == "__main__":
    run("source.csv", "standardized.csv")
'''


def publish(session: MappingSession) -> dict[str, Any]:
    views = {state.id: pair_view(session, state) for state in session.table_mappings.values()}
    if any(item["validation"]["blockers"] for item in views.values()):
        raise HTTPException(
            status_code=409,
            detail="Saving is blocked until every confirmed table pair passes field review and validation.",
        )
    published_at = now()
    pairs: list[dict[str, Any]] = []

    for state in session.table_mappings.values():
        current = views[state.id]
        definition = copy.deepcopy(state.definition)
        definition["publication"] = {
            "version": state.revision,
            "approved_by": "workbench.user",
            "status": "published",
            "published_at": published_at,
        }
        state.definition = definition
        pairs.append(
            {
                "mapping_id": state.id,
                "source_table": table_summary(session.source_tables[state.source_table_id]),
                "target_table": table_summary(session.target_tables[state.target_table_id]),
                "definition": definition,
                "generated_pandas": compile_to_pandas(definition, state.target_schema),
                "preview": current["preview"],
                "validation": current["validation"],
            }
        )

    implementation = {
        "schema_version": "2.0",
        "session_id": session.id,
        "source_document": session.source_filename,
        "target_document": session.target_filename,
        "pairs": pairs,
        "saved_at": published_at,
        "storage": "sqlite",
    }
    save_implementation(implementation)
    session.published_at = published_at
    return {
        "message": "Mapping definitions and generated Pandas code were saved.",
        "implementation": implementation,
    }


app = FastAPI(
    title="Financial Data Standardization Workbench",
    version="0.4.0",
    description="Paired document, table-first, review-controlled financial mapping.",
)


@app.get("/api/health")
def health() -> dict[str, Any]:
    settings = get_settings()
    return {
        "status": "ok",
        "provider": "deepseek",
        "provider_configured": settings.provider_configured,
        "model": settings.deepseek_model,
        "thinking": settings.deepseek_thinking,
    }


@app.get("/api/provider/status")
def provider_status() -> dict[str, Any]:
    settings = get_settings()
    return {
        "provider": "deepseek",
        "configured": settings.provider_configured,
        "model": settings.deepseek_model,
        "thinking": settings.deepseek_thinking,
    }


@app.post("/api/sessions")
def create_session(payload: PairedUploadRequest) -> dict[str, Any]:
    source_tables = parse_document(payload.source_file, role="source")
    target_tables = parse_document(payload.target_file, role="target")
    session = MappingSession(
        id=uuid.uuid4().hex,
        source_filename=safe_filename(payload.source_file.filename),
        target_filename=safe_filename(payload.target_file.filename),
        source_tables=source_tables,
        target_tables=target_tables,
    )
    SESSIONS[session.id] = session
    return session_view(session)


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str) -> dict[str, Any]:
    return session_view(require_session(session_id))


@app.post("/api/sessions/{session_id}/table-proposal")
async def propose_table_mapping(
    session_id: str,
    request: TableProposalRequest | None = None,
) -> dict[str, Any]:
    session = require_session(session_id)
    settings = get_settings()
    if not settings.provider_configured:
        raise HTTPException(status_code=503, detail="DeepSeek is not configured.")
    try:
        _, table_mapping_proposal, metadata = await generate_validated_json_with_retry(
            settings,
            system_prompt=table_mapping_system_prompt(),
            user_prompt=table_mapping_prompt(session, request.comment if request else ""),
            validator=lambda output: sanitize_table_mapping(session, output),
        )
        session.table_mapping_proposal = table_mapping_proposal
    except ProviderError as error:
        raise HTTPException(status_code=502, detail=f"DeepSeek table request failed ({error.category}).") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=f"DeepSeek table mapping was rejected: {error}") from error
    session.table_provider_metadata = metadata
    message = (
        "Table matches regenerated from your comment."
        if request and request.comment.strip()
        else "Initial table matches were suggested automatically."
    )
    return {"message": message, "session": session_view(session)}


@app.post("/api/sessions/{session_id}/table-confirm")
def confirm_table_mapping(session_id: str, request: TableMappingConfirmation) -> dict[str, Any]:
    session = require_session(session_id)
    if not session.table_mapping_proposal:
        raise HTTPException(status_code=409, detail="Generate the AI table mapping before confirming it.")
    requested = {item.target_table_id: item.source_table_id for item in request.mappings}
    if len(requested) != len(request.mappings) or set(requested) != set(session.target_tables):
        raise HTTPException(status_code=422, detail="Confirm exactly one source table for every target table.")
    if any(source_id not in session.source_tables for source_id in requested.values()):
        raise HTTPException(status_code=422, detail="A confirmed source table does not exist.")

    proposal_by_target = {item["target_table_id"]: item for item in session.table_mapping_proposal}
    session.table_mappings = {}
    for index, target_id in enumerate(session.target_tables, start=1):
        source_id = requested[target_id]
        target_table = session.target_tables[target_id]
        target_schema = make_target_schema(target_table)
        mapping_id = f"tm-{index}"
        session.table_mappings[mapping_id] = FieldMappingState(
            id=mapping_id,
            source_table_id=source_id,
            target_table_id=target_id,
            target_schema=target_schema,
            definition=make_definition(session.source_tables[source_id], target_table, target_schema),
        )
        proposal = proposal_by_target[target_id]
        suggested_source = proposal["source_table_id"]
        proposal["source_table_id"] = source_id
        proposal["review"] = {
            "status": "accepted",
            "confirmed": True,
            "changed_by_user": source_id != suggested_source,
            "confirmed_at": now(),
        }
    session.table_mapping_confirmed = True
    session.active_mapping_id = next(iter(session.table_mappings))
    return session_view(session)


@app.post("/api/sessions/{session_id}/table-mappings/{mapping_id}/activate")
def activate_table_mapping(session_id: str, mapping_id: str) -> dict[str, Any]:
    session = require_session(session_id)
    if mapping_id not in session.table_mappings:
        raise HTTPException(status_code=404, detail="Confirmed table mapping not found.")
    session.active_mapping_id = mapping_id
    return session_view(session)


@app.post("/api/sessions/{session_id}/proposal")
async def proposal(session_id: str) -> dict[str, Any]:
    session = require_session(session_id)
    state = require_active_state(session)
    settings = get_settings()
    if not settings.provider_configured:
        raise HTTPException(status_code=503, detail="DeepSeek is not configured.")
    unlocked_targets = [
        step["target"]
        for step in state.definition["steps"]
        if not step.get("review", {}).get("locked")
    ]
    if not unlocked_targets:
        return {"message": "Every field mapping is already accepted.", "proposal": session_view(session)}
    batches = [
        unlocked_targets[index : index + FIELD_AI_BATCH_SIZE]
        for index in range(0, len(unlocked_targets), FIELD_AI_BATCH_SIZE)
    ]
    definition = copy.deepcopy(state.definition)
    batch_metadata: list[dict[str, Any]] = []
    try:
        for targets in batches:
            _, definition, metadata = await generate_validated_json_with_retry(
                settings,
                system_prompt=mapping_system_prompt(),
                user_prompt=initial_mapping_prompt(session, state, targets),
                validator=lambda output, requested=set(targets), current=definition: apply_ai_initial_proposal(
                    session,
                    state,
                    output,
                    required_targets=requested,
                    base_definition=current,
                ),
            )
            batch_metadata.append(metadata)
        state.definition = definition
    except ProviderError as error:
        raise HTTPException(status_code=502, detail=f"DeepSeek field request failed ({error.category}).") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=f"DeepSeek field mapping was rejected: {error}") from error
    metadata = batch_metadata[-1]
    metadata["batch_count"] = len(batch_metadata)
    metadata["batch_size"] = FIELD_AI_BATCH_SIZE
    metadata["total_latency_ms"] = sum(int(item.get("latency_ms", 0)) for item in batch_metadata)
    state.ai_mode = f"live DeepSeek field suggestion · {metadata['returned_model'] or metadata['requested_model']}"
    state.provider_metadata = metadata
    return {"message": "Live field mapping proposal validated and applied.", "proposal": session_view(session)}


@app.post("/api/sessions/{session_id}/revisions")
async def propose_revision(session_id: str, request: RevisionRequest) -> dict[str, Any]:
    session = require_session(session_id)
    state = require_active_state(session)
    if request.base_revision != state.revision:
        raise HTTPException(status_code=409, detail="This screen is stale; refresh before proposing.")
    current_rule = next((step for step in state.definition["steps"] if step["target"] == request.field), None)
    if current_rule is None:
        raise HTTPException(status_code=404, detail="Target field not found.")
    if current_rule.get("op") == "unmapped":
        raise HTTPException(status_code=409, detail="Generate the field mapping first.")
    if current_rule.get("review", {}).get("locked"):
        raise HTTPException(status_code=409, detail="Accepted rules are locked.")
    candidate = copy.deepcopy(state.definition)
    index = next(i for i, step in enumerate(candidate["steps"]) if step["target"] == request.field)
    settings = get_settings()
    if not settings.provider_configured:
        raise HTTPException(status_code=503, detail="DeepSeek is not configured.")
    try:
        output, patch_value, metadata = await generate_validated_json_with_retry(
            settings,
            system_prompt=mapping_system_prompt(),
            user_prompt=revision_mapping_prompt(session, state, request),
            validator=lambda output: sanitize_ai_step(
                output.get("step"),
                current_step=candidate["steps"][index],
                source_columns=set(session.source_tables[state.source_table_id].frame.columns),
                target_schema=state.target_schema,
            ),
        )
    except ProviderError as error:
        raise HTTPException(status_code=502, detail=f"DeepSeek revision failed ({error.category}).") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=f"DeepSeek revision was rejected: {error}") from error
    patch_value["review"]["status"] = "changed"
    patch_value["review"]["comment"] = request.comment
    candidate["steps"][index] = patch_value
    revision_id = uuid.uuid4().hex[:10]
    state.pending[revision_id] = {
        "definition": candidate,
        "field": request.field,
        "comment": request.comment,
        "patch": [{"op": "replace", "path": f"/steps/{index}", "value": patch_value}],
    }
    state.ai_mode = f"live DeepSeek revision · {metadata['returned_model'] or metadata['requested_model']}"
    state.provider_metadata = metadata
    return {
        "revision_id": revision_id,
        "explanation": str(output.get("explanation") or "DeepSeek proposed a validated change.")[:500],
        "patch": state.pending[revision_id]["patch"],
        "proposed": pair_view(session, state, candidate),
    }


@app.post("/api/sessions/{session_id}/fields/{target_field}/accept")
def accept_current_field(session_id: str, target_field: str) -> dict[str, Any]:
    session = require_session(session_id)
    state = require_active_state(session)
    step = next((item for item in state.definition["steps"] if item["target"] == target_field), None)
    if step is None:
        raise HTTPException(status_code=404, detail="Target field not found.")
    if step["op"] == "unmapped":
        raise HTTPException(status_code=409, detail="An unmapped field cannot be accepted.")
    if not step.get("review", {}).get("locked"):
        state.revision += 1
        step["review"].update(
            {
                "status": "accepted",
                "locked": True,
                "accepted_by": "workbench.user",
                "accepted_at": now(),
            }
        )
        state.revisions.append(
            {
                "revision": state.revision,
                "field": target_field,
                "actor": "workbench.user",
                "comment": "Accepted current AI proposal.",
                "timestamp": now(),
                "status": "accepted",
            }
        )
    return session_view(session)


@app.post("/api/sessions/{session_id}/fields/accept")
def accept_fields_in_bulk(
    session_id: str, request: BulkFieldAcceptanceRequest
) -> dict[str, Any]:
    session = require_session(session_id)
    state = require_active_state(session)
    steps_by_target = {step["target"]: step for step in state.definition["steps"]}
    requested = list(steps_by_target) if request.accept_all else list(dict.fromkeys(request.fields))
    if not requested:
        raise HTTPException(status_code=422, detail="Select at least one field to accept.")
    unknown = [target for target in requested if target not in steps_by_target]
    if unknown:
        raise HTTPException(status_code=404, detail=f"Target field not found: {unknown[0]}")
    unmapped = [target for target in requested if steps_by_target[target].get("op") == "unmapped"]
    if unmapped:
        raise HTTPException(
            status_code=409,
            detail=f"Generate a mapping before accepting {unmapped[0]}.",
        )
    accepted_at = now()
    accepted = [
        target
        for target in requested
        if not steps_by_target[target].get("review", {}).get("locked")
    ]
    if accepted:
        state.revision += 1
        for target in accepted:
            steps_by_target[target]["review"].update(
                {
                    "status": "accepted",
                    "locked": True,
                    "accepted_by": "workbench.user",
                    "accepted_at": accepted_at,
                }
            )
            state.revisions.append(
                {
                    "revision": state.revision,
                    "field": target,
                    "actor": "workbench.user",
                    "comment": "Accepted in bulk field review.",
                    "timestamp": accepted_at,
                    "status": "accepted",
                }
            )
    return {
        "message": f"Accepted and locked {len(accepted)} field mapping(s).",
        "accepted_fields": accepted,
        "session": session_view(session),
    }


@app.post("/api/sessions/{session_id}/revisions/{revision_id}/accept")
def accept_revision(session_id: str, revision_id: str) -> dict[str, Any]:
    session = require_session(session_id)
    state = require_active_state(session)
    pending = state.pending.pop(revision_id, None)
    if not pending:
        raise HTTPException(status_code=404, detail="Proposed revision not found.")
    accepted_definition = pending["definition"]
    step = next(item for item in accepted_definition["steps"] if item["target"] == pending["field"])
    step["review"].update(
        {
            "status": "accepted",
            "locked": True,
            "accepted_by": "workbench.user",
            "accepted_at": now(),
        }
    )
    state.revision += 1
    state.definition = accepted_definition
    state.revisions.append(
        {
            "revision": state.revision,
            "field": pending["field"],
            "actor": "workbench.user",
            "comment": pending["comment"],
            "timestamp": now(),
            "status": "accepted",
        }
    )
    return session_view(session)


@app.post("/api/sessions/{session_id}/preview")
def preview(session_id: str) -> dict[str, Any]:
    session = require_session(session_id)
    payload = pair_view(session, require_active_state(session))
    return {"preview": payload["preview"], "validation": payload["validation"]}


@app.post("/api/sessions/{session_id}/validate")
def validate(session_id: str) -> dict[str, Any]:
    session = require_session(session_id)
    return pair_view(session, require_active_state(session))["validation"]


@app.post("/api/sessions/{session_id}/publish")
def publish_session(session_id: str) -> dict[str, Any]:
    return publish(require_session(session_id))


@app.get("/api/implementations/{session_id}")
def get_implementation(session_id: str) -> dict[str, Any]:
    return load_implementation(session_id)


@app.post("/api/reset")
def reset_session_data() -> dict[str, str]:
    SESSIONS.clear()
    return {"status": "active sessions cleared; saved implementations retained"}


if FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
