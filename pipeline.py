"""Validate Airtable fields, summarize USD sales and create inspectable report artifacts."""

import argparse
import csv
import io
import json
import os
import re
import zipfile
from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.sync_api import sync_playwright, Error as BrowserError
from airtable_client import Settings, ReportError, fetch_records

ROOT = Path(__file__).resolve().parent
FIELDS = {
    "order_id": "Order ID",
    "date": "Date",
    "customer": "Customer",
    "category": "Category",
    "amount": "Amount USD",
    "status": "Status",
}


def normalize(records):
    rows, order_ids = [], set()
    for number, record in enumerate(records, 1):
        fields = record.get("fields", {}) if isinstance(record, dict) else {}
        row = {}
        for key, label in FIELDS.items():
            value = fields.get(label)
            if key == "amount":
                if isinstance(value, bool) or not isinstance(
                    value, (str, int, float, Decimal)
                ):
                    raise ReportError(
                        f"Record {number}: Amount USD must be a non-negative decimal."
                    )
                try:
                    amount = Decimal(str(value))
                except InvalidOperation:
                    raise ReportError(f"Record {number}: invalid Amount USD.") from None
                if (
                    not amount.is_finite()
                    or amount < 0
                    or amount > Decimal("999999999.99")
                    or amount != amount.quantize(Decimal(".01"))
                ):
                    raise ReportError(
                        f"Record {number}: Amount USD must be 0-999999999.99 with at most two decimals."
                    )
                row[key] = amount
            else:
                if (
                    not isinstance(value, str)
                    or not value.strip()
                    or len(value) > 120
                    or any(ord(c) < 32 for c in value)
                ):
                    raise ReportError(
                        f"Record {number}: required text field is missing or invalid: {label}."
                    )
                row[key] = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", row["order_id"]):
            raise ReportError(f"Record {number}: invalid Order ID.")
        if row["order_id"] in order_ids:
            raise ReportError(
                f"Record {number}: duplicate Order ID; no report produced."
            )
        order_ids.add(row["order_id"])
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["date"]):
                raise ValueError()
            date.fromisoformat(row["date"])
        except ValueError:
            raise ReportError(
                f"Record {number}: Date must be a valid YYYY-MM-DD date."
            ) from None
        if row["status"] not in ("Paid", "Pending", "Refunded"):
            raise ReportError(
                f"Record {number}: Status must be Paid, Pending or Refunded."
            )
        rows.append(row)
    if not rows:
        raise ReportError(
            "No records found. Check the table or selected Airtable view."
        )
    return sorted(rows, key=lambda r: (r["date"], r["order_id"]))


def summarize(rows):
    total = lambda status: sum(
        (r["amount"] for r in rows if r["status"] == status), Decimal(0)
    )
    categories = Counter()
    for row in rows:
        if row["status"] == "Paid":
            categories[row["category"]] += row["amount"]
    return {
        "orders": len(rows),
        "paid_orders": sum(r["status"] == "Paid" for r in rows),
        "paid_usd": str(total("Paid").quantize(Decimal(".01"))),
        "pending_usd": str(total("Pending").quantize(Decimal(".01"))),
        "refunded_usd": str(total("Refunded").quantize(Decimal(".01"))),
        "from": min(r["date"] for r in rows),
        "to": max(r["date"] for r in rows),
        "categories": [
            {"name": name, "amount_usd": str(value.quantize(Decimal(".01")))}
            for name, value in sorted(
                categories.items(), key=lambda kv: (-kv[1], kv[0])
            )
        ],
    }


env = Environment(
    loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape(["html"])
)
env.filters["money"] = lambda value: "$" + format(Decimal(str(value)), ",.2f")


def render_pdf(html):
    try:
        with sync_playwright() as p:
            options = {"headless": True}
            if os.environ.get("REPORT_CHROME_CHANNEL"):
                options["channel"] = os.environ["REPORT_CHROME_CHANNEL"]
            browser = p.chromium.launch(**options)
            try:
                page = browser.new_page()
                page.route("**/*", lambda route: route.abort())
                page.set_content(html, wait_until="load")
                return page.pdf(
                    format="A4",
                    print_background=True,
                    display_header_footer=True,
                    header_template="<span></span>",
                    footer_template='<div style="font:9px Arial;color:#718265;width:100%;margin:0 17mm;display:flex;justify-content:space-between"><span>RELAY / SALES REPORT AUTOMATION</span><span>Page <span class="pageNumber"></span> of <span class="totalPages"></span></span></div>',
                    margin={
                        "top": "17mm",
                        "bottom": "20mm",
                        "left": "17mm",
                        "right": "17mm",
                    },
                )
            finally:
                browser.close()
    except BrowserError:
        raise ReportError(
            "PDF rendering failed. Install Playwright Chromium or configure REPORT_CHROME_CHANNEL."
        ) from None


def build_report(
    mode="sample",
    *,
    fetcher=fetch_records,
    renderer=render_pdf,
    emit=lambda event, **data: None,
):
    events = []

    def log(event, **data):
        item = {"at": datetime.now(timezone.utc).isoformat(), "event": event, **data}
        events.append(item)
        emit(event, **data)

    log("run_started", source=mode)
    if mode == "sample":
        pages = json.loads(
            (ROOT / "examples/sample-pages.json").read_text(encoding="utf-8")
        )
        records = []
        for i, page in enumerate(pages, 1):
            records.extend(page["records"])
            log("page_fetched", page=i, records=len(page["records"]))
        stats = {"pages": len(pages), "requests": 0, "retries": 0}
    elif mode == "airtable":
        records, stats = fetcher(Settings.from_env(), emit=log)
    else:
        raise ReportError("Unknown report source.")
    rows = normalize(records)
    summary = summarize(rows)
    log("records_validated", records=len(rows))
    html = env.get_template("report.html").render(
        rows=rows, summary=summary, source=mode
    )
    pdf = renderer(html)
    log("pdf_rendered", bytes=len(pdf))
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(FIELDS.keys())
    for row in rows:
        cells = [
            str(row[key]) if key != "amount" else format(row[key], ".2f")
            for key in FIELDS
        ]
        writer.writerow(["'" + v if re.match(r"^[\s]*[=+@-]", v) else v for v in cells])
    log("run_completed", records=len(rows))
    run = {
        "source": mode,
        "stats": stats,
        "summary": summary,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "notes": [
            "Paid revenue excludes Pending and Refunded amounts.",
            "This read-only report has no Airtable writeback.",
            "Fetching multiple pages is not a transactional Airtable snapshot.",
        ],
    }
    artifacts = {
        "report.pdf": pdf,
        "report.html": html.encode(),
        "snapshot.csv": ("\ufeff" + stream.getvalue()).encode("utf-8"),
        "summary.json": (json.dumps(run, indent=2, ensure_ascii=False) + "\n").encode(),
        "events.jsonl": ("\n".join(json.dumps(e) for e in events) + "\n").encode(),
    }
    return artifacts, run


def archive_bytes(artifacts):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in artifacts.items():
            archive.writestr(name, data)
    return out.getvalue()


def main():
    parser = argparse.ArgumentParser(
        description="Read Airtable or sample data and generate a report bundle."
    )
    parser.add_argument("--mode", choices=["sample", "airtable"], default="sample")
    parser.add_argument("--output-dir", type=Path, default=Path(".data/manual-run"))
    args = parser.parse_args()
    if args.output_dir.exists():
        parser.exit(
            2, "Error: choose a new output directory to preserve earlier reports.\n"
        )
    log = []
    try:
        artifacts, run = build_report(
            args.mode, emit=lambda event, **data: log.append({"event": event, **data})
        )
        args.output_dir.mkdir(parents=True, exist_ok=False)
        for name, data in artifacts.items():
            (args.output_dir / name).write_bytes(data)
        print(
            f"Created {args.output_dir}: {run['summary']['orders']} records, {run['stats']['pages']} pages."
        )
    except ReportError as error:
        args.output_dir.mkdir(parents=True, exist_ok=False)
        (args.output_dir / "failure.json").write_text(
            json.dumps(
                {"status": "failed", "error": str(error), "events": log}, indent=2
            ),
            encoding="utf-8",
        )
        parser.exit(2, f"Error: {error}\n")
    except OSError:
        parser.exit(
            2,
            "Error: could not write the output folder. Check permissions and free space.\n",
        )


if __name__ == "__main__":
    main()
