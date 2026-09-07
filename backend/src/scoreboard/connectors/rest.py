"""Pull from any JSON HTTP endpoint.

The escape hatch for a customer whose system nobody has heard of but which can
at least answer a GET. Everything about the request and the shape of the reply
lives in configuration:

    {
      "url": "https://crm.example.com/api/sales",
      "method": "GET",
      "headers": {"X-Api-Key": "..."},          # the secret goes in `secret`
      "auth_header": "Authorization",            # where `secret` is placed
      "auth_prefix": "Bearer ",
      "records_path": "data.reps",               # where the array lives
      "date_params": {"from": "start", "to": "end"},
      "mapping": {"rep_key": "id", "rep_name": "name", "sold_leads": "sold"}
    }

`discover()` fetches once and reports the fields it found, so the mapping can
be filled in from what the endpoint actually returns rather than from what
somebody remembers it returning.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from scoreboard.connectors.base import (
    ConnectionResult,
    Connector,
    Period,
    SourceError,
    SourceRecord,
)
from scoreboard.domain.metrics import ADDITIVE

REQUEST_TIMEOUT = 45
MAX_BYTES = 12 * 1024 * 1024
# What a person filling in the mapping screen calls a field, and what the
# normalized record calls it. "team" is the natural word; `source_team` is the
# record field, named to make clear the platform may override it.
IDENTITY_FIELDS = {
    "team": "source_team",
    "home_branch": "home_branch",
    "title": "title",
    "hire_date": "hire_date",
}


def dig(payload, path: str):
    """Follow a dotted path into nested JSON.

    "data.reps" finds the array however deeply the endpoint chose to bury it,
    which is more useful than insisting every customer return a bare list.
    """
    if not path:
        return payload
    current = payload
    for step in path.split("."):
        step = step.strip()
        if not step:
            continue
        if isinstance(current, dict):
            if step not in current:
                raise SourceError(f"The reply has no '{step}' inside '{path}'.")
            current = current[step]
        elif isinstance(current, list) and step.isdigit():
            index = int(step)
            if index >= len(current):
                raise SourceError(f"The reply has no item {index} inside '{path}'.")
            current = current[index]
        else:
            raise SourceError(f"Cannot follow '{step}' in '{path}': the reply is not an object.")
    return current


def find_records(payload, path: str = "") -> list[dict]:
    """Locate the list of people in the reply."""
    node = dig(payload, path)
    if isinstance(node, list):
        return [row for row in node if isinstance(row, dict)]
    if isinstance(node, dict):
        # A single object is a common shape for a one-row reply.
        return [node]
    raise SourceError(
        f"'{path or 'the reply'}' is not a list of records. "
        "Set the records path to wherever the array lives, for example 'data.reps'."
    )


def number(value) -> float:
    """Accept the several ways an API can express a number."""
    if value is None or isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("$", "").replace("%", "")
    if not text:
        return 0.0
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    try:
        parsed = float(text)
    except ValueError:
        return 0.0
    return -parsed if negative else parsed


def key_for(value: str) -> str:
    key = "".join(c if c.isalnum() else "-" for c in str(value or "").lower()).strip("-")
    while "--" in key:
        key = key.replace("--", "-")
    return key or "unknown"


def describe_fields(rows: list[dict]) -> list[dict]:
    """What each field looks like, so the mapping screen can suggest sensibly."""
    fields: dict[str, dict] = {}
    for row in rows[:50]:
        for name, value in row.items():
            entry = fields.setdefault(name, {"field": name, "numeric": 0, "samples": []})
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                entry["numeric"] += 1
            elif isinstance(value, str) and number(value) and any(c.isdigit() for c in value):
                entry["numeric"] += 1
            if len(entry["samples"]) < 3 and value not in (None, ""):
                entry["samples"].append(str(value)[:40])
    return [
        {"field": e["field"], "looks_numeric": e["numeric"] > len(rows[:50]) / 2,
         "samples": e["samples"]}
        for e in fields.values()
    ]


class RestConnector(Connector):
    kind = "rest"
    label = "HTTP / JSON API"

    # ------------------------------------------------------------ request
    def _url(self, period: Period | None) -> str:
        url = str(self.config.get("url") or "").strip()
        if not url:
            raise SourceError("No URL configured.")
        if not url.lower().startswith(("http://", "https://")):
            # Anything else would let a saved source read a local file.
            raise SourceError("The URL must start with http:// or https://.")

        params = dict(self.config.get("query") or {})
        date_params = self.config.get("date_params") or {}
        if period and date_params:
            for name, which in date_params.items():
                params[name] = (period.start if which == "start" else period.end).isoformat()
        if not params:
            return url

        joiner = "&" if "?" in url else "?"
        return url + joiner + urllib.parse.urlencode(params)

    def _fetch(self, period: Period | None):
        headers = {"Accept": "application/json", "User-Agent": "scoreboard-connector"}
        headers.update({str(k): str(v) for k, v in (self.config.get("headers") or {}).items()})

        if self.secret:
            name = str(self.config.get("auth_header") or "Authorization")
            prefix = str(self.config.get("auth_prefix") or "Bearer ")
            headers[name] = prefix + self.secret

        request = urllib.request.Request(
            self._url(period), headers=headers,
            method=str(self.config.get("method") or "GET").upper(),
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                raw = response.read(MAX_BYTES + 1)
        except urllib.error.HTTPError as exc:
            body = exc.read(500).decode("utf-8", "replace").strip()
            raise SourceError(f"The API answered {exc.code}. {body[:200]}") from exc
        except urllib.error.URLError as exc:
            raise SourceError(f"Could not reach the API: {exc.reason}") from exc

        if len(raw) > MAX_BYTES:
            raise SourceError(
                "The reply is larger than 12MB. Narrow it with a filter or a date range."
            )
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError as exc:
            raise SourceError("The reply is not JSON.") from exc

    # ------------------------------------------------------------ interface
    def test_connection(self) -> ConnectionResult:
        try:
            payload = self._fetch(Period.current_month())
            rows = find_records(payload, str(self.config.get("records_path") or ""))
        except SourceError as exc:
            return ConnectionResult(ok=False, message=str(exc))
        return ConnectionResult(
            ok=True,
            message=f"Reached the API and found {len(rows)} records.",
            detail={"fields": [f["field"] for f in describe_fields(rows)][:40]},
        )

    def discover(self) -> dict:
        """Report the fields the endpoint actually returns."""
        payload = self._fetch(Period.current_month())
        rows = find_records(payload, str(self.config.get("records_path") or ""))
        return {
            "records": len(rows),
            "fields": describe_fields(rows),
            "sample": rows[0] if rows else {},
            "mappable": ["rep_key", "rep_name", *IDENTITY_FIELDS, *ADDITIVE],
        }

    def fetch(self, period: Period) -> list[SourceRecord]:
        payload = self._fetch(period)
        rows = find_records(payload, str(self.config.get("records_path") or ""))
        mapping = {k: str(v) for k, v in (self.config.get("mapping") or {}).items() if v}

        name_field = mapping.get("rep_name")
        if not name_field:
            raise SourceError(
                "Map a field to 'rep_name' first — without a name there is nobody to rank."
            )

        records = []
        for row in rows:
            name = str(row.get(name_field) or "").strip()
            if not name:
                continue
            # Without an explicit id, the name is the identity. It is stable
            # enough for a leaderboard and avoids demanding a key from a system
            # that may not expose one.
            raw_key = row.get(mapping["rep_key"]) if mapping.get("rep_key") else name
            records.append(
                SourceRecord(
                    rep_key=key_for(raw_key or name),
                    rep_name=name,
                    **{
                        record_field: str(row.get(mapping[mapping_key]) or "").strip()
                        for mapping_key, record_field in IDENTITY_FIELDS.items()
                        if mapping.get(mapping_key)
                    },
                    components={
                        metric: number(row.get(mapping[metric]))
                        for metric in ADDITIVE
                        if mapping.get(metric)
                    },
                    # The row as the API returned it, including the columns
                    # nobody mapped. Mapping is guesswork until somebody can
                    # see what was actually there.
                    source_row=dict(row) if isinstance(row, dict) else {},
                    source_name=str(self.config.get("name") or "JSON API"),
                )
            )
        return records
