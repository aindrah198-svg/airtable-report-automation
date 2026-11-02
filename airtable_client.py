"""Read-only Airtable list-records client with bounded retries and pagination."""

import json
import os
import re
import time
from urllib.parse import quote
import requests


class ReportError(Exception):
    """Public, credential-safe error messages only."""


class Settings:
    def __init__(self, token="", base="", table="", view=""):
        self.token, self.base, self.table, self.view = token, base, table, view

    @classmethod
    def from_env(cls):
        return cls(
            *(
                os.environ.get(key, "").strip()
                for key in (
                    "AIRTABLE_TOKEN",
                    "AIRTABLE_BASE_ID",
                    "AIRTABLE_TABLE",
                    "AIRTABLE_VIEW",
                )
            )
        )

    def validate(self):
        if (
            not self.token
            or not re.fullmatch(r"app[A-Za-z0-9]{14}", self.base)
            or not self.table
            or len(self.table) > 200
            or len(self.view) > 200
        ):
            raise ReportError(
                "Configure AIRTABLE_TOKEN, a valid AIRTABLE_BASE_ID and AIRTABLE_TABLE on the server."
            )
        if any(ord(c) < 32 or ord(c) > 126 for c in self.token):
            raise ReportError("The server token contains unsupported characters.")

    def configured(self):
        try:
            self.validate()
            return True
        except ReportError:
            return False


def http_get(url, headers, params):
    try:
        with requests.get(
            url,
            headers=headers,
            params=params,
            timeout=(5, 20),
            allow_redirects=False,
            stream=True,
        ) as response:
            content = response.raw.read(2 * 1024 * 1024 + 1, decode_content=True)
            if len(content) > 2 * 1024 * 1024:
                raise ReportError("Airtable response exceeds the supported page size.")
            try:
                payload = json.loads(content)
            except (ValueError, UnicodeError):
                payload = None
            return response.status_code, dict(response.headers), payload
    except requests.RequestException:
        # Never surface request URLs, Authorization headers or provider exception bodies.
        raise ReportError("Airtable network request failed.") from None


def fetch_records(
    settings, *, transport=http_get, sleep=time.sleep, emit=lambda event, **data: None
):
    settings.validate()
    url = (
        "https://api.airtable.com/v0/"
        + settings.base
        + "/"
        + quote(settings.table, safe="")
    )
    headers = {
        "Authorization": "Bearer " + settings.token,
        "Accept": "application/json",
    }
    records, seen_ids, seen_offsets = [], set(), set()
    stats = {"pages": 0, "requests": 0, "retries": 0}
    offset = None
    for page in range(1, 51):
        params = {"pageSize": 100}
        if settings.view:
            params["view"] = settings.view
        if offset:
            params["offset"] = offset
        payload = None
        for attempt in range(4):
            stats["requests"] += 1
            try:
                status, response_headers, payload = transport(url, headers, params)
            except ReportError:
                if attempt == 3:
                    raise ReportError(
                        "Airtable network request failed after bounded retries."
                    ) from None
                delay = 2**attempt
            else:
                if status == 200:
                    break
                provider_type = (
                    payload.get("error", {}).get("type")
                    if isinstance(payload, dict)
                    and isinstance(payload.get("error"), dict)
                    else None
                )
                if (
                    status == 429
                    and provider_type == "PUBLIC_API_BILLING_LIMIT_EXCEEDED"
                ):
                    raise ReportError(
                        "Airtable monthly API allowance is exhausted. Review the account plan before retrying."
                    )
                if status in (401, 403):
                    raise ReportError(
                        "Airtable denied access. Check token scope and base access on the server."
                    )
                if status == 404:
                    raise ReportError("Airtable base, table or view was not found.")
                if status not in (429, 500, 502, 503, 504):
                    raise ReportError(
                        "Airtable request failed with HTTP " + str(status) + "."
                    )
                if attempt == 3:
                    raise ReportError(
                        "Airtable retry limit reached (HTTP " + str(status) + ")."
                    )
                if status == 429:
                    retry = next(
                        (
                            v
                            for k, v in response_headers.items()
                            if k.lower() == "retry-after"
                        ),
                        "30",
                    )
                    delay = max(
                        30, min(120, int(retry) if str(retry).isdigit() else 30)
                    )
                else:
                    delay = 2**attempt
            stats["retries"] += 1
            emit("retry", page=page, attempt=attempt + 1, wait_seconds=delay)
            sleep(delay)
        if not isinstance(payload, dict) or not isinstance(
            payload.get("records"), list
        ):
            raise ReportError("Airtable returned an unexpected records payload.")
        if len(payload["records"]) > 100:
            raise ReportError("Airtable page exceeded 100 records.")
        for record in payload["records"]:
            if (
                not isinstance(record, dict)
                or not isinstance(record.get("id"), str)
                or not record.get("id")
                or not isinstance(record.get("fields"), dict)
            ):
                raise ReportError("Airtable returned an invalid record structure.")
            if record["id"] in seen_ids:
                raise ReportError(
                    "Repeated Airtable record ID encountered; report stopped to avoid double counting."
                )
            seen_ids.add(record["id"])
            records.append(record)
        stats["pages"] += 1
        emit("page_fetched", page=page, records=len(payload["records"]))
        if len(records) > 5000:
            raise ReportError("This portfolio tool supports at most 5000 records.")
        offset = payload.get("offset")
        if "offset" not in payload:
            return records, stats
        if (
            not isinstance(offset, str)
            or not offset
            or len(offset) > 2000
            or offset in seen_offsets
        ):
            raise ReportError("Repeated or invalid pagination offset; report stopped.")
        seen_offsets.add(offset)
        sleep(0.25)
    raise ReportError(
        "Pagination safety limit reached before all records were fetched."
    )
