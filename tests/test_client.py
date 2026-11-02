import pytest
import requests
from airtable_client import Settings, ReportError, fetch_records, http_get


def settings():
    return Settings(
        "unit-test-token-not-real", "appABCDEF12345678", "Orders / sample", "Ready"
    )


def record(identifier="rec1"):
    return {"id": identifier, "fields": {}}


def test_paginated_reads_keep_params_and_token_out_of_events():
    calls = []
    events = []
    waits = []

    def transport(url, headers, params):
        calls.append((url, headers.copy(), params.copy()))
        return (
            (200, {}, {"records": [record("rec1")], "offset": "next/page"})
            if len(calls) == 1
            else (200, {}, {"records": [record("rec2")]})
        )

    records, stats = fetch_records(
        settings(),
        transport=transport,
        sleep=waits.append,
        emit=lambda event, **data: events.append((event, data)),
    )
    assert len(records) == 2 and stats == {"pages": 2, "requests": 2, "retries": 0}
    assert calls[1][2]["offset"] == "next/page" and calls[0][2]["pageSize"] == 100
    assert calls[0][2]["view"] == "Ready" and "Orders%20%2F%20sample" in calls[0][0]
    assert calls[0][1]["Authorization"] == "Bearer unit-test-token-not-real"
    assert waits == [0.25] and "unit-test-token" not in str(events)


@pytest.mark.parametrize(
    "retry_after,expected", [("1", 30), ("45", 45), ("999", 120), ("bad", 30)]
)
def test_429_waits_at_least_thirty_seconds(retry_after, expected):
    calls = []
    waits = []

    def transport(*args):
        calls.append(1)
        return (
            (429, {"Retry-After": retry_after}, {})
            if len(calls) == 1
            else (200, {}, {"records": []})
        )

    _, stats = fetch_records(settings(), transport=transport, sleep=waits.append)
    assert waits == [expected] and stats["retries"] == 1


def test_transient_network_and_server_retries_are_bounded():
    waits = []
    calls = []

    def transport(*args):
        calls.append(1)
        if len(calls) == 1:
            raise ReportError("network unavailable")
        return 503, {}, {}

    with pytest.raises(ReportError, match="retry limit"):
        fetch_records(settings(), transport=transport, sleep=waits.append)
    assert len(calls) == 4 and waits == [1, 2, 4]


@pytest.mark.parametrize("status", [401, 403, 404, 302, 422])
def test_permanent_errors_are_not_retried(status):
    waits = []
    with pytest.raises(ReportError):
        fetch_records(
            settings(), transport=lambda *args: (status, {}, {}), sleep=waits.append
        )
    assert waits == []


def test_monthly_allowance_is_not_retried():
    waits = []
    with pytest.raises(ReportError, match="monthly"):
        fetch_records(
            settings(),
            transport=lambda *args: (
                429,
                {},
                {"error": {"type": "PUBLIC_API_BILLING_LIMIT_EXCEEDED"}},
            ),
            sleep=waits.append,
        )
    assert waits == []


def test_repeated_offset_and_record_ids_fail():
    with pytest.raises(ReportError, match="offset"):
        fetch_records(
            settings(),
            transport=lambda *args: (200, {}, {"records": [], "offset": "same"}),
            sleep=lambda _: None,
        )
    with pytest.raises(ReportError, match="record ID"):
        fetch_records(
            settings(),
            transport=lambda *args: (
                200,
                {},
                {"records": [record()], "offset": "same"},
            ),
            sleep=lambda _: None,
        )


def test_page_cap_fails_instead_of_truncating():
    calls = []

    def transport(*args):
        calls.append(1)
        return 200, {}, {"records": [], "offset": str(len(calls))}

    with pytest.raises(ReportError, match="safety limit"):
        fetch_records(settings(), transport=transport, sleep=lambda _: None)
    assert len(calls) == 50


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"records": "bad"},
        {"records": [{}]},
        {"records": [record(str(i)) for i in range(101)]},
    ],
)
def test_unexpected_response_structure(payload):
    with pytest.raises(ReportError):
        fetch_records(
            settings(), transport=lambda *args: (200, {}, payload), sleep=lambda _: None
        )


def test_network_error_never_exposes_provider_exception(monkeypatch):
    def broken(*args, **kwargs):
        raise requests.ConnectionError("Authorization: Bearer do-not-log-this")

    monkeypatch.setattr(requests, "get", broken)
    with pytest.raises(ReportError) as error:
        http_get("https://api.airtable.com/v0/base/table", {}, {})
    assert "do-not-log" not in str(error.value)


def test_settings_validation():
    for item in [
        Settings(),
        Settings("test", "wrong", "Orders"),
        Settings("bad\nheader", "appABCDEF12345678", "Orders"),
    ]:
        assert not item.configured()
        with pytest.raises(ReportError):
            item.validate()


@pytest.mark.parametrize("offset", [0, None, False, ""])
def test_present_but_invalid_offset_is_not_silently_truncated(offset):
    with pytest.raises(ReportError, match="offset"):
        fetch_records(
            settings(),
            transport=lambda *args: (200, {}, {"records": [], "offset": offset}),
            sleep=lambda _: None,
        )
