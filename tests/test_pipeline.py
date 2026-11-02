import io
import json
import os
import subprocess
import sys
import threading
from decimal import Decimal
from pathlib import Path
import pytest
from pypdf import PdfReader
from airtable_client import ReportError
from pipeline import normalize, summarize, build_report, archive_bytes, ROOT
from jobs import JobStore
from app import create_app


def record(**extra):
    fields = {
        "Order ID": "ORD-1",
        "Date": "2026-10-01",
        "Customer": "Alex Example",
        "Category": "Care",
        "Amount USD": "0.10",
        "Status": "Paid",
    }
    fields.update(extra)
    return {"id": "recTEST", "fields": fields}


def test_decimal_and_status_semantics():
    rows = normalize(
        [
            record(),
            record(**{"Order ID": "ORD-2", "Status": "Pending", "Amount USD": 10}),
            record(**{"Order ID": "ORD-3", "Status": "Refunded", "Amount USD": 5}),
        ]
    )
    summary = summarize(rows)
    assert (
        summary["paid_usd"] == "0.10"
        and summary["pending_usd"] == "10.00"
        and summary["refunded_usd"] == "5.00"
    )
    assert summary["orders"] == 3 and summary["paid_orders"] == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("Order ID", ""),
        ("Order ID", "bad/id"),
        ("Date", "2026-02-30"),
        ("Customer", None),
        ("Customer", []),
        ("Customer", "x" * 121),
        ("Amount USD", True),
        ("Amount USD", float("nan")),
        ("Amount USD", float("inf")),
        ("Amount USD", -1),
        ("Amount USD", "0.001"),
        ("Status", "Unknown"),
    ],
    ids=[
        "missing-id",
        "id-format",
        "date",
        "missing-customer",
        "linked-field",
        "text-limit",
        "boolean",
        "nan",
        "infinity",
        "negative",
        "precision",
        "status",
    ],
)
def test_invalid_record_aborts_report(field, value):
    with pytest.raises(ReportError, match="Record 1"):
        normalize([record(**{field: value})])


def test_empty_missing_and_duplicate_order_data():
    for data in [[], [{"fields": {}}], [record(), record()]]:
        with pytest.raises(ReportError):
            normalize(data)
    assert normalize([record(**{"Amount USD": 0})])[0]["amount"] == Decimal(0)


def test_sample_bundle_and_escaped_template():
    seen = []
    artifacts, run = build_report(
        renderer=lambda html: seen.append(html) or b"%PDF-test"
    )
    assert (
        run["summary"]["orders"] == 24
        and run["stats"]["pages"] == 3
        and run["stats"]["requests"] == 0
    )
    assert set(artifacts) == {
        "report.pdf",
        "report.html",
        "snapshot.csv",
        "summary.json",
        "events.jsonl",
    }
    import zipfile

    with zipfile.ZipFile(io.BytesIO(archive_bytes(artifacts))) as z:
        assert len(z.namelist()) == 5

    def fetcher(*args, **kwargs):
        return [record(Customer="<script>alert(1)</script>")], {
            "pages": 1,
            "requests": 1,
            "retries": 0,
        }

    artifacts, _ = build_report(
        "airtable", fetcher=fetcher, renderer=lambda html: b"%PDF-test"
    )
    assert (
        b"<script>" not in artifacts["report.html"]
        and b"&lt;script&gt;" in artifacts["report.html"]
    )


def test_real_pdf_has_all_records_and_page_numbers():
    artifacts, run = build_report()
    reader = PdfReader(io.BytesIO(artifacts["report.pdf"]))
    text = "\n".join(p.extract_text() for p in reader.pages)
    assert len(reader.pages) >= 2
    for i in range(2001, 2025):
        assert f"ORD-{i}" in text
    assert format(Decimal(run["summary"]["paid_usd"]), ",.2f") in text
    for i, page in enumerate(reader.pages, 1):
        assert f"Page {i} of" in page.extract_text()


def test_background_failure_is_recorded_without_partial_download(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise ReportError("Airtable denied access. Check server settings.")

    monkeypatch.setattr("jobs.build_report", fail)
    store = JobStore(tmp_path)
    identifier = store.submit("sample")
    store.pool.shutdown(wait=True)
    job = store.get(identifier)
    assert job["status"] == "failed" and "denied" in job["error"]
    assert not (tmp_path / (identifier + ".zip")).exists()
    assert (tmp_path / (identifier + ".failure.json")).exists()
    assert (
        create_app(store)
        .test_client()
        .get("/api/runs/" + identifier + "/download")
        .status_code
        == 409
    )


def test_single_worker_lock_and_success_archive(tmp_path, monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def slow(*args, **kwargs):
        entered.set()
        release.wait(5)
        return {"report.pdf": b"%PDF-test"}, {
            "summary": {"orders": 1},
            "stats": {"pages": 1},
        }

    monkeypatch.setattr("jobs.build_report", slow)
    store = JobStore(tmp_path)
    try:
        identifier = store.submit("sample")
        assert entered.wait(2)
        with pytest.raises(ReportError, match="already running"):
            store.submit("sample")
    finally:
        release.set()
        store.pool.shutdown(wait=True)
    assert store.get(identifier)["status"] == "completed"
    client = create_app(store).test_client()
    assert client.get("/api/runs/" + identifier + "/download").status_code == 200
    assert client.get("/api/runs/invalid/download").status_code == 404


def test_unexpected_failures_do_not_expose_secrets(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("Bearer secret-should-never-appear")

    monkeypatch.setattr("jobs.build_report", broken)
    store = JobStore(tmp_path)
    identifier = store.submit("sample")
    store.pool.shutdown(wait=True)
    assert "secret-should" not in str(store.get(identifier))


def test_restart_recovers_interrupted_history(tmp_path):
    store = JobStore(tmp_path)
    with store.db() as db:
        db.execute(
            "INSERT INTO jobs(id,source,status,created) VALUES (?,?,?,?)",
            ("a" * 32, "sample", "running", "2026-10-01"),
        )
    store.pool.shutdown(wait=True)
    fresh = JobStore(tmp_path)
    assert fresh.get("a" * 32)["status"] == "failed"
    fresh.pool.shutdown(wait=True)


def test_web_config_errors_no_credentials_exposed(tmp_path, monkeypatch):
    for key in ["AIRTABLE_TOKEN", "AIRTABLE_BASE_ID", "AIRTABLE_TABLE"]:
        monkeypatch.delenv(key, raising=False)
    store = JobStore(tmp_path)
    client = create_app(store).test_client()
    assert client.get("/").status_code == 200
    assert client.get("/api/state").json["configured"] is False
    assert client.post("/api/runs", json={"source": "airtable"}).status_code == 422
    assert client.post("/api/runs", json=[]).status_code == 422
    assert (
        client.post(
            "/api/runs",
            json={"source": "sample"},
            headers={"Origin": "https://other.example"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/runs", data=b"x" * 5000, content_type="application/json"
        ).status_code
        == 413
    )
    store.pool.shutdown(wait=True)


def test_cli_failure_log_without_partial_report_or_overwrite(tmp_path):
    target = tmp_path / "failure"
    env = dict(os.environ)
    env["AIRTABLE_TOKEN"] = ""
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "pipeline.py"),
            "--mode",
            "airtable",
            "--output-dir",
            str(target),
        ],
        capture_output=True,
        env=env,
    )
    assert (
        result.returncode == 2
        and (target / "failure.json").exists()
        and not (target / "report.pdf").exists()
    )
    original = (target / "failure.json").read_bytes()
    again = subprocess.run(
        [sys.executable, str(ROOT / "pipeline.py"), "--output-dir", str(target)],
        capture_output=True,
    )
    assert again.returncode == 2 and original == (target / "failure.json").read_bytes()


def test_failure_log_keeps_safe_progress_events(tmp_path, monkeypatch):
    def broken(mode, emit):
        emit("retry", page=1, wait_seconds=30)
        raise ReportError("Airtable retry limit reached.")

    monkeypatch.setattr("jobs.build_report", broken)
    store = JobStore(tmp_path)
    identifier = store.submit("airtable")
    store.pool.shutdown(wait=True)
    failure = json.loads((tmp_path / (identifier + ".failure.json")).read_text())
    assert (
        failure["events"][0]["event"] == "retry"
        and failure["events"][0]["wait_seconds"] == 30
    )
