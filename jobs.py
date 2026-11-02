"""Single-worker report jobs. SQLite stores status; archives are published atomically."""

import json
import os
import re
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from airtable_client import ReportError
from pipeline import build_report, archive_bytes


class JobStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=1)
        with self.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, source TEXT, status TEXT, created TEXT, completed TEXT, summary TEXT, error TEXT)"
            )
        # A local restart cannot safely resume an interrupted external read/render.
        with self.db() as db:
            db.execute(
                "UPDATE jobs SET status='failed', error='Local server stopped before completion. Run again.' WHERE status IN ('queued','running')"
            )

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path / "history.sqlite", timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def history(self):
        with self.db() as db:
            return [
                self.decode(row)
                for row in db.execute(
                    "SELECT * FROM jobs ORDER BY created DESC LIMIT 30"
                )
            ]

    def decode(self, row):
        if row is None:
            return None
        result = dict(row)
        result["summary"] = json.loads(result["summary"]) if result["summary"] else None
        return result

    def get(self, job_id):
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            return None
        with self.db() as db:
            return self.decode(
                db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            )

    def submit(self, source):
        if not self.lock.acquire(blocking=False):
            raise ReportError(
                "Another report is already running. Wait for it to finish."
            )
        job_id = uuid.uuid4().hex
        try:
            with self.db() as db:
                db.execute(
                    "INSERT INTO jobs(id,source,status,created) VALUES (?,?,?,?)",
                    (job_id, source, "queued", datetime.now(timezone.utc).isoformat()),
                )
            self.pool.submit(self.execute, job_id, source)
        except Exception:
            self.lock.release()
            raise
        return job_id

    def execute(self, job_id, source):
        temporary = self.path / (job_id + ".tmp")
        events = []

        def log(event, **data):
            events.append(
                {"at": datetime.now(timezone.utc).isoformat(), "event": event, **data}
            )

        try:
            with self.db() as db:
                db.execute("UPDATE jobs SET status='running' WHERE id=?", (job_id,))
            artifacts, run = build_report(source, emit=log)
            temporary.write_bytes(archive_bytes(artifacts))
            os.replace(temporary, self.path / (job_id + ".zip"))
            with self.db() as db:
                db.execute(
                    "UPDATE jobs SET status='completed', summary=?, completed=? WHERE id=?",
                    (json.dumps(run), datetime.now(timezone.utc).isoformat(), job_id),
                )
        except Exception as error:
            message = (
                str(error)
                if isinstance(error, ReportError)
                else "Unexpected local report failure. Check the installation and try again."
            )
            with self.db() as db:
                db.execute(
                    "UPDATE jobs SET status='failed', error=?, completed=? WHERE id=?",
                    (message, datetime.now(timezone.utc).isoformat(), job_id),
                )
            (self.path / (job_id + ".failure.json")).write_text(
                json.dumps(
                    {
                        "id": job_id,
                        "status": "failed",
                        "error": message,
                        "events": events,
                    }
                ),
                encoding="utf-8",
            )
        finally:
            temporary.unlink(missing_ok=True)
            self.lock.release()
