from __future__ import annotations

import base64
import io
import json

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app import main as workbench
from app.config import reset_settings_cache


SOURCE_CSV = """CIF,Report_Date,Currency,Line_Item,Amount
 C001 ,31/12/2025,SGD,Revenue,"1,000"
 C001 ,31/12/2025,SGD,Revenue,500
 C001 ,31/12/2025,SGD,Operating Expense,700
 C001 ,31/12/2025,SGD,Net Profit,800
 C002 ,31/12/2025,USD,Revenue,900
 C002 ,31/12/2025,USD,Operating Expense,600
 C002 ,31/12/2025,USD,Net Profit,300
"""

TARGET_CSV = """customer_id,reporting_date,currency,total_revenue,operating_expense,net_profit
EXAMPLE-001,2025-12-31,SGD,1250000.00,840000.00,410000.00
EXAMPLE-002,2025-12-31,USD,970000.00,620000.00,350000.00
"""

OFFSET_SOURCE_CSV = """Quarterly financial ledger,,,,,,,
Generated for review,,,,,,,
,,,,,,,
,,CIF,Report_Date,Currency,Line_Item,Amount,
,,C001,31/12/2025,SGD,Revenue,1000,
,,C001,31/12/2025,SGD,Net Profit,800,
,,,,,,,
,,End of report,,,,,
"""

OFFSET_TARGET_CSV = """Expected standardized layout,,,,,,
,,,,,,
,customer_id,reporting_date,currency,total_revenue,operating_expense,net_profit
,EXAMPLE-001,2025-12-31,SGD,1000,700,300
,End of sample,,,,,
"""


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("WORKBENCH_DB_PATH", str(tmp_path / "workbench.db"))


def encoded_file(filename: str, content: str | bytes) -> dict[str, str]:
    raw = content.encode("utf-8") if isinstance(content, str) else content
    return {
        "filename": filename,
        "content_base64": base64.b64encode(raw).decode("ascii"),
    }


def proposed_steps() -> list[dict[str, object]]:
    grain = ["customer_id", "reporting_date"]
    return [
        {
            "target": "customer_id",
            "op": "string",
            "function": "trim",
            "inputs": [{"column": "CIF"}],
            "confidence": 0.99,
            "rationale": "CIF is the customer identifier.",
        },
        {
            "target": "reporting_date",
            "op": "date_parse",
            "inputs": [{"column": "Report_Date"}],
            "params": {"format": "dd/MM/yyyy"},
            "confidence": 0.98,
            "rationale": "Report_Date is the period end.",
        },
        {
            "target": "currency",
            "op": "direct",
            "inputs": [{"column": "Currency"}],
            "confidence": 0.99,
            "rationale": "Currency is already standardized.",
        },
        {
            "target": "total_revenue",
            "op": "aggregate",
            "function": "sum",
            "inputs": [{"column": "Amount"}],
            "where": {"Line_Item": {"eq": "Revenue"}},
            "group_by": grain,
            "confidence": 0.96,
            "rationale": "Sum revenue rows at the target grain.",
        },
        {
            "target": "operating_expense",
            "op": "aggregate",
            "function": "sum",
            "inputs": [{"column": "Amount"}],
            "where": {"Line_Item": {"eq": "Operating Expense"}},
            "group_by": grain,
            "confidence": 0.96,
            "rationale": "Sum operating expense rows at the target grain.",
        },
        {
            "target": "net_profit",
            "op": "aggregate",
            "function": "sum",
            "inputs": [{"column": "Amount"}],
            "where": {"Line_Item": {"eq": "Net Profit"}},
            "group_by": grain,
            "confidence": 0.96,
            "rationale": "Sum net profit rows at the target grain.",
        },
    ]


def install_fake_provider(monkeypatch) -> list[str]:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-secret")
    reset_settings_cache()
    table_comments: list[str] = []

    async def fake_generate_json(self, *, system_prompt: str, user_prompt: str):  # type: ignore[no-untyped-def]
        assert "Return JSON only" in system_prompt
        assert "test-only-secret" not in user_prompt
        request = json.loads(user_prompt)
        command = request["command"]
        if command == "propose_table_mapping":
            table_comments.append(request["user_comment"])
            assert all(item["profile"]["masked_for_model"] for item in request["source_tables"])
            assert all(item["profile"]["masked_for_model"] for item in request["target_tables"])
            output = {
                "mappings": [
                    {
                        "source_table_id": source["id"],
                        "target_table_id": target["id"],
                        "confidence": 0.94,
                        "rationale": "Names and column semantics align.",
                    }
                    for source, target in zip(
                        request["source_tables"], request["target_tables"], strict=True
                    )
                ]
            }
        elif command == "propose_revision_patch":
            output = {
                "step": proposed_steps()[3],
                "explanation": "Revenue remains a filtered sum at customer-period grain.",
            }
        else:
            assert command == "propose_initial_definition"
            assert request["source_profile"]["masked_for_model"] is True
            output = {"steps": proposed_steps()}
        return (
            output,
            {
                "provider": "deepseek",
                "requested_model": "deepseek-v4-flash",
                "returned_model": "deepseek-v4-flash",
                "finish_reason": "stop",
                "latency_ms": 12,
                "usage": {},
            },
        )

    monkeypatch.setattr(workbench.DeepSeekMappingProvider, "generate_json", fake_generate_json)
    return table_comments


def create_paired_session(client: TestClient) -> dict[str, object]:
    response = client.post(
        "/api/sessions",
        json={
            "source_file": encoded_file("source.csv", SOURCE_CSV),
            "target_file": encoded_file("target.csv", TARGET_CSV),
        },
    )
    assert response.status_code == 200
    return response.json()


def test_reviewed_paired_mapping_is_persisted_with_generated_code(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    workbench.SESSIONS.clear()
    table_comments = install_fake_provider(monkeypatch)
    client = TestClient(workbench.app)

    initial = create_paired_session(client)
    assert initial["stage"] == "table_mapping"
    assert initial["source_document"] == "source.csv"
    assert initial["target_document"] == "target.csv"
    assert len(initial["source_tables"]) == 1
    assert len(initial["target_tables"]) == 1
    assert "definition" not in initial

    session_id = initial["session_id"]
    suggested = client.post(f"/api/sessions/{session_id}/table-proposal")
    assert suggested.status_code == 200
    suggestion_session = suggested.json()["session"]
    suggestion = suggestion_session["table_mapping_proposal"][0]
    assert suggestion["source_table_id"] == initial["source_tables"][0]["id"]
    assert suggestion["target_table_id"] == initial["target_tables"][0]["id"]
    assert suggestion_session["provider"]["thinking"] == "enabled"
    regenerated = client.post(
        f"/api/sessions/{session_id}/table-proposal",
        json={"comment": "Keep the ledger paired with the standardized P&L target."},
    )
    assert regenerated.status_code == 200
    assert table_comments == ["", "Keep the ledger paired with the standardized P&L target."]
    suggestion = regenerated.json()["session"]["table_mapping_proposal"][0]

    confirmed = client.post(
        f"/api/sessions/{session_id}/table-confirm",
        json={
            "mappings": [
                {
                    "source_table_id": suggestion["source_table_id"],
                    "target_table_id": suggestion["target_table_id"],
                }
            ]
        },
    )
    assert confirmed.status_code == 200
    confirmed_body = confirmed.json()
    assert confirmed_body["stage"] == "field_mapping"
    assert confirmed_body["validation"]["blockers"] == 6
    assert all(step["op"] == "unmapped" for step in confirmed_body["definition"]["steps"])

    response = client.post(f"/api/sessions/{session_id}/proposal")
    assert response.status_code == 200
    proposal = response.json()["proposal"]
    assert proposal["provider_last_call"]["returned_model"] == "deepseek-v4-flash"
    assert proposal["preview"]["metrics"]["target_rows"] == 2
    assert proposal["preview"]["rows"][0]["total_revenue"] == 1500.0

    revision = client.post(
        f"/api/sessions/{session_id}/revisions",
        json={
            "field": "total_revenue",
            "comment": "Confirm revenue is summed at customer-period grain.",
            "base_revision": 0,
        },
    )
    assert revision.status_code == 200
    revision_body = revision.json()
    assert revision_body["patch"][0]["path"] == "/steps/3"
    accepted_revision = client.post(
        f"/api/sessions/{session_id}/revisions/{revision_body['revision_id']}/accept"
    )
    assert accepted_revision.status_code == 200

    remaining = [
        step["target"]
        for step in accepted_revision.json()["definition"]["steps"]
        if not step["review"]["locked"]
    ]
    selected = client.post(
        f"/api/sessions/{session_id}/fields/accept",
        json={"fields": remaining[:2], "accept_all": False},
    )
    assert selected.status_code == 200
    assert selected.json()["accepted_fields"] == remaining[:2]
    accepted_all = client.post(
        f"/api/sessions/{session_id}/fields/accept",
        json={"fields": [], "accept_all": True},
    )
    assert accepted_all.status_code == 200
    assert len(accepted_all.json()["accepted_fields"]) == len(remaining) - 2

    validation = client.post(f"/api/sessions/{session_id}/validate").json()
    assert validation["status"] == "ready"
    assert validation["blockers"] == 0

    published = client.post(f"/api/sessions/{session_id}/publish")
    assert published.status_code == 200
    implementation = published.json()["implementation"]
    assert implementation["storage"] == "sqlite"
    assert len(implementation["pairs"]) == 1
    assert implementation["pairs"][0]["definition"]["publication"]["status"] == "published"
    generated_code = implementation["pairs"][0]["generated_pandas"]
    compiled = compile(generated_code, "generated.py", "exec")
    generated_namespace: dict[str, object] = {}
    exec(compiled, generated_namespace)
    source_path = tmp_path / "source.csv"
    output_path = tmp_path / "standardized.csv"
    source_path.write_text(SOURCE_CSV, encoding="utf-8")
    generated_namespace["run"](source_path, output_path)  # type: ignore[operator]
    assert output_path.read_text(encoding="utf-8").splitlines()[0] == (
        "customer_id,reporting_date,currency,total_revenue,operating_expense,net_profit"
    )

    workbench.SESSIONS.clear()
    persisted = client.get(f"/api/implementations/{session_id}")
    assert persisted.status_code == 200
    assert persisted.json() == implementation


def test_offset_csv_header_and_table_range_are_detected() -> None:
    workbench.SESSIONS.clear()
    client = TestClient(workbench.app)
    response = client.post(
        "/api/sessions",
        json={
            "source_file": encoded_file("offset-source.csv", OFFSET_SOURCE_CSV),
            "target_file": encoded_file("offset-target.csv", OFFSET_TARGET_CSV),
        },
    )
    assert response.status_code == 200
    body = response.json()
    source = body["source_tables"][0]
    target = body["target_tables"][0]
    assert source["columns"] == ["CIF", "Report_Date", "Currency", "Line_Item", "Amount"]
    assert source["row_count"] == 2
    assert source["detected_region"]["range"] == "C4:G6"
    assert target["columns"] == [
        "customer_id",
        "reporting_date",
        "currency",
        "total_revenue",
        "operating_expense",
        "net_profit",
    ]
    assert target["row_count"] == 1
    assert target["detected_region"]["range"] == "B3:G4"


def test_hundreds_of_columns_are_accepted_for_bulk_review() -> None:
    workbench.SESSIONS.clear()
    client = TestClient(workbench.app)
    source_headers = [f"source_{index:03d}" for index in range(201)]
    target_headers = [f"target_{index:03d}" for index in range(201)]
    source_csv = ",".join(source_headers) + "\n" + ",".join(str(index) for index in range(201))
    target_csv = ",".join(target_headers) + "\n" + ",".join(str(index) for index in range(201))
    response = client.post(
        "/api/sessions",
        json={
            "source_file": encoded_file("wide-source.csv", source_csv),
            "target_file": encoded_file("wide-target.csv", target_csv),
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["source_tables"][0]["column_count"] == 201
    assert body["target_tables"][0]["column_count"] == 201


def test_semantically_invalid_field_proposal_is_retried(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    workbench.SESSIONS.clear()
    install_fake_provider(monkeypatch)
    client = TestClient(workbench.app)
    initial = create_paired_session(client)
    session_id = initial["session_id"]
    suggestion = client.post(f"/api/sessions/{session_id}/table-proposal").json()["session"][
        "table_mapping_proposal"
    ][0]
    client.post(
        f"/api/sessions/{session_id}/table-confirm",
        json={
            "mappings": [
                {
                    "source_table_id": suggestion["source_table_id"],
                    "target_table_id": suggestion["target_table_id"],
                }
            ]
        },
    )

    calls = 0

    async def flaky_generate_json(self, *, system_prompt: str, user_prompt: str):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        steps = proposed_steps()
        if calls == 1:
            steps[0] = {**steps[0], "inputs": [{"column": "Unknown_Customer_Column"}]}
        return (
            {"steps": steps},
            {
                "provider": "deepseek",
                "requested_model": "deepseek-v4-flash",
                "returned_model": "deepseek-v4-flash",
                "finish_reason": "stop",
                "latency_ms": 12,
                "usage": {},
            },
        )

    monkeypatch.setattr(workbench.DeepSeekMappingProvider, "generate_json", flaky_generate_json)
    response = client.post(f"/api/sessions/{session_id}/proposal")

    assert response.status_code == 200
    assert calls == 2
    metadata = response.json()["proposal"]["provider_last_call"]
    assert metadata["retry_count"] == 1
    assert "unknown source column" in metadata["validation_retry_reason"]


def workbook_bytes(sheets: list[tuple[str, list[list[object]]]]) -> bytes:
    workbook = Workbook()
    workbook.remove(workbook.active)
    for name, rows in sheets:
        sheet = workbook.create_sheet(name)
        for row in rows:
            sheet.append(row)
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def test_excel_sheets_become_tables_and_are_confirmed_as_pairs(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    workbench.SESSIONS.clear()
    install_fake_provider(monkeypatch)
    client = TestClient(workbench.app)
    source_workbook = workbook_bytes(
        [
            (
                "Ledger",
                [
                    ["Financial ledger export"],
                    [None],
                    [None, "CIF", "Report_Date", "Currency", "Line_Item", "Amount"],
                    [None, "C001", "31/12/2025", "SGD", "Revenue", 1000],
                ],
            ),
            (
                "Accounts",
                [["Account register"], [None], [None, "Account", "Category"], [None, "4000", "Revenue"]],
            ),
            ("Empty sheet", []),
        ]
    )
    target_workbook = workbook_bytes(
        [
            (
                "Standard P&L",
                [
                    ["Expected P&L output"],
                    [None],
                    [
                        None,
                        "customer_id",
                        "reporting_date",
                        "currency",
                        "total_revenue",
                        "operating_expense",
                        "net_profit",
                    ],
                    [None, "EXAMPLE", "2025-12-31", "SGD", 1, 1, 1],
                ],
            ),
            ("Account Master", [["Expected account output"], [None], [None, "account_id", "category"]]),
        ]
    )
    created = client.post(
        "/api/sessions",
        json={
            "source_file": encoded_file("source.xlsx", source_workbook),
            "target_file": encoded_file("target.xlsx", target_workbook),
        },
    )
    assert created.status_code == 200
    initial = created.json()
    assert [table["name"] for table in initial["source_tables"]] == ["Ledger", "Accounts"]
    assert [table["name"] for table in initial["target_tables"]] == [
        "Standard P&L",
        "Account Master",
    ]
    assert [table["detected_region"]["range"] for table in initial["source_tables"]] == [
        "B3:F4",
        "B3:C4",
    ]
    assert [table["detected_region"]["range"] for table in initial["target_tables"]] == [
        "B3:G4",
        "B3:C3",
    ]

    session_id = initial["session_id"]
    suggested = client.post(f"/api/sessions/{session_id}/table-proposal")
    assert suggested.status_code == 200
    mappings = suggested.json()["session"]["table_mapping_proposal"]
    assert len(mappings) == 2
    confirmed = client.post(
        f"/api/sessions/{session_id}/table-confirm",
        json={
            "mappings": [
                {
                    "source_table_id": mapping["source_table_id"],
                    "target_table_id": mapping["target_table_id"],
                }
                for mapping in mappings
            ]
        },
    )
    assert confirmed.status_code == 200
    result = confirmed.json()
    assert result["stage"] == "field_mapping"
    assert len(result["table_pairs"]) == 2
    assert all(pair["status"] == "blocked" for pair in result["table_pairs"])


def test_session_requires_a_source_and_target_document() -> None:
    response = TestClient(workbench.app).post(
        "/api/sessions",
        json={"source_file": encoded_file("source.csv", SOURCE_CSV)},
    )
    assert response.status_code == 422


def test_built_react_application_is_served() -> None:
    response = TestClient(workbench.app).get("/")
    assert response.status_code == 200
    assert '<div id="root"></div>' in response.text
