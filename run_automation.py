"""One report per invocation; an external scheduler decides when to invoke it."""

import argparse
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Run a report using a fresh timestamped output directory."
    )
    parser.add_argument("--mode", choices=["sample", "airtable"], default="sample")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    name = (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + uuid.uuid4().hex[:8]
    )
    target = root / ".data/scheduled" / name
    result = subprocess.run(
        [
            sys.executable,
            str(root / "pipeline.py"),
            "--mode",
            args.mode,
            "--output-dir",
            str(target),
        ]
    )
    raise SystemExit(result.returncode)
