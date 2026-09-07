"""Tableau connector.

Ported from the original single-office implementation, with every fixed value
moved into `config`. Nothing about a customer's server, site, workbook, view,
filters or column names is compiled in, so pointing a new customer at their
own report is configuration rather than a release.

Tableau is not queried as a database. We sign in with a Personal Access
Token, resolve a workbook and view, and download that view's data as CSV
through the REST API. The awkward parts below are all consequences of that:
Tableau hands back a spreadsheet, not a schema.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
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

log = logging.getLogger("scoreboard.tableau")

DEFAULT_API_VERSION = "3.22"
REQUEST_TIMEOUT = 60

# Column names vary between workbooks. Explicit mapping in config wins; these
# aliases are the fallback that lets a fresh connection work before anyone has
# opened the mapping screen. Longest alias first, so "pitched rate" is not
# swallowed by "pitched".
FIELD_ALIASES: dict[str, list[str]] = {
    "issued_leads": ["issuedleads", "leadsissued", "issued"],
    "pitched_leads": ["pitchedleads", "leadspitched", "pitched"],
    "sold_leads": ["soldleads", "leadssold", "sold"],
    "gross_split": ["grosssplit", "grosssales", "grossvolume", "gross"],
    "pending_split": ["pendingsplit", "pending"],
    "net_split": ["netsplit", "netsales", "netvolume"],
}
ALIAS_INDEX = sorted(
    ((alias, field) for field, aliases in FIELD_ALIASES.items() for alias in aliases),
    key=lambda pair: -len(pair[0]),
)

REP_ALIASES = ["srname", "repname", "salesrep", "salesperson", "employee", "assignedrep", "rep"]
BRANCH_ALIASES = ["userhomebranch", "homebranch", "branchnew", "branch", "office", "location"]
TITLE_ALIASES = ["usertitlepicklist", "title", "position"]
HIRE_ALIASES = ["hiredate", "hire"]
TEAM_ALIASES = ["teamleadname", "teamlead", "team"]
LEAD_ID_ALIASES = ["leadid"]
MEASURE_NAME_COLS = ["measurenames", "measure"]
MEASURE_VALUE_COLS = ["measurevalues", "value"]

COUNT_FIELDS = {"issued_leads", "pitched_leads", "sold_leads"}


# ---------------------------------------------------------------- parsing helpers
def norm(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def clean_number(raw) -> float | None:
    """'$48,750' -> 48750.0 ; '82%' -> 82.0 ; '(120)' -> -120.0 ; '' -> None."""
    if raw is None:
        return None
    text = str(raw).strip().replace("$", "").replace(",", "").replace("%", "")
    if text in ("", "-", "—", "n/a", "null", "None"):
        return None
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    try:
        value = float(text)
    except ValueError:
        return None
    return -value if negative else value


def match_field(header_norm: str) -> str | None:
    if not header_norm:
        return None
    for alias, field in ALIAS_INDEX:
        if alias in header_norm:
            return field
    return None


def find_column(headers: list[str], aliases: list[str]) -> str | None:
    """Exact normalized match first, then a conservative substring match.

    Tableau often exposes a field under a longer caption than the filter card
    shows, so "USER-Home Branch Picklist__c" still has to resolve to branch.
    """
    normalized = [(header, norm(header)) for header in (headers or [])]
    for alias in aliases:
        for header, value in normalized:
            if value == alias:
                return header
    for alias in aliases:
        for header, value in normalized:
            if value.startswith(alias) or alias in value:
                return header
    return None


def rep_key_for(name: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")
    return key or "unknown"


def _feed(bucket: dict, field: str, raw, long_format: bool, lead_id: str) -> None:
    """Add one measured value to a rep's running totals.

    Takes the row's context as arguments rather than closing over the loop
    variables. It happened to be correct as a closure because it was only ever
    called within the same iteration, but that is a property nobody can see
    from the function itself.
    """
    value = clean_number(raw)
    if value is None:
        return
    if long_format and field in COUNT_FIELDS and lead_id:
        # A lead covering several products appears on several rows. Count it
        # once; dollars still sum across them.
        bucket.setdefault(field, {}).setdefault(lead_id, value)
    else:
        bucket[field] = bucket.get(field, 0.0) + value


def parse_csv(csv_text: str, mapping: dict | None = None) -> list[SourceRecord]:
    """Turn a Tableau view export into normalized records.

    Two shapes have to be handled. A wide export has one column per measure.
    A long export has Measure Names / Measure Values, which means a sold lead
    covering several products appears on several rows. Counting metrics are
    therefore taken once per lead id while dollar metrics sum across rows;
    doing otherwise inflates close rates for reps who sell bundles.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    headers = reader.fieldnames or []
    mapping = mapping or {}

    rep_col = find_column(headers, REP_ALIASES)
    if not rep_col:
        raise SourceError(
            "No sales-rep column found in the Tableau export. "
            f"Columns seen: {', '.join(headers) or '(none)'}"
        )

    branch_col = find_column(headers, BRANCH_ALIASES)
    title_col = find_column(headers, TITLE_ALIASES)
    hire_col = find_column(headers, HIRE_ALIASES)
    team_col = find_column(headers, TEAM_ALIASES)
    lead_col = find_column(headers, LEAD_ID_ALIASES)
    measure_name_col = find_column(headers, MEASURE_NAME_COLS)
    measure_value_col = find_column(headers, MEASURE_VALUE_COLS)
    long_format = bool(measure_name_col and measure_value_col)

    # Explicit mapping from the settings screen takes priority over aliases.
    explicit = {header: field for field, header in mapping.items() if header in headers}
    header_norms = {header: norm(header) for header in headers}

    totals: dict[str, dict] = {}
    meta: dict[str, dict] = {}
    # How many export lines fed each person, so a stored record can say it was
    # assembled rather than pretending to be a row that existed.
    line_counts: dict[str, int] = {}
    order: list[str] = []

    for row in reader:
        name = (row.get(rep_col) or "").strip()
        if not name:
            continue
        if name not in totals:
            totals[name] = {}
            meta[name] = {}
            line_counts[name] = 0
            order.append(name)
        line_counts[name] += 1

        lead_id = (row.get(lead_col) or "").strip() if lead_col else ""
        # Tableau emits per-rep "All" roll-up rows; counting them double-counts.
        if long_format and lead_id.lower() == "all":
            continue

        info = meta[name]
        for key, column in (
            ("source_team", team_col),
            ("home_branch", branch_col),
            ("title", title_col),
            ("hire_date", hire_col),
        ):
            if not column:
                continue
            value = (row.get(column) or "").strip()
            if value and value.lower() != "all" and not info.get(key):
                info[key] = value

        if long_format:
            field = match_field(norm(row.get(measure_name_col) or ""))
            if field:
                _feed(totals[name], field, row.get(measure_value_col), long_format, lead_id)
        else:
            for header, header_norm in header_norms.items():
                field = explicit.get(header) or match_field(header_norm)
                if field:
                    _feed(totals[name], field, row.get(header), long_format, lead_id)

    records = []
    for name in order:
        components = {}
        for field, value in totals[name].items():
            components[field] = sum(value.values()) if isinstance(value, dict) else value
        records.append(
            SourceRecord(
                rep_key=rep_key_for(name),
                rep_name=name,
                components=components,
                # One person's figures come from several export lines, so the
                # honest record is what was read for them rather than a row
                # that never existed. `_lines` says how many fed it.
                source_row={
                    **components,
                    **{k: v for k, v in meta[name].items() if v},
                    "_lines": line_counts.get(name, 0),
                },
                source_name="Tableau export",
                **{k: meta[name].get(k, "") for k in
                   ("source_team", "home_branch", "title", "hire_date")},
            )
        )
    return records


def branch_profile(csv_text: str, aliases: list[str] | None = None) -> tuple[str, set[str]]:
    """Return the export's branch column caption and its distinct values."""
    reader = csv.DictReader(io.StringIO(csv_text))
    headers = reader.fieldnames or []
    column = find_column(headers, aliases or BRANCH_ALIASES)
    values = set()
    if column:
        for row in reader:
            value = str(row.get(column) or "").strip()
            if value and value.lower() != "all":
                values.add(value)
    return column or "", values


# ---------------------------------------------------------------- connector
class TableauConnector(Connector):
    kind = "tableau"
    label = "Tableau"

    # ------------------------------------------------------------ HTTP
    def _request(self, url, method="GET", token=None, body=None, timeout=REQUEST_TIMEOUT):
        headers = {"Accept": "application/json", "User-Agent": "scoreboard-connector"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if token:
            headers["X-Tableau-Auth"] = token
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except urllib.error.URLError as exc:
            raise SourceError(f"Could not reach Tableau: {exc.reason}") from exc

    @property
    def server(self) -> str:
        server = str(self.config.get("server") or "").strip().rstrip("/")
        if not server:
            raise SourceError("Tableau server URL is not configured.")
        return server

    def _api_base(self) -> str:
        """Ask the server which REST version it speaks, falling back to a known one."""
        version = DEFAULT_API_VERSION
        status, raw = self._request(f"{self.server}/api/{version}/serverinfo", timeout=20)
        if status == 200:
            try:
                version = json.loads(raw)["serverInfo"]["restApiVersion"]
            except (ValueError, KeyError, TypeError) as exc:
                log.debug("serverinfo did not report a version (%s); using %s", exc, version)
        return f"{self.server}/api/{version}"

    def signin(self) -> tuple[str, str, str]:
        if not self.secret:
            raise SourceError("Tableau personal access token secret is not set.")
        site = str(self.config.get("site") or "").strip()
        pat_name = str(self.config.get("pat_name") or "").strip()
        if not pat_name:
            raise SourceError("Tableau personal access token name is not configured.")

        base = self._api_base()
        status, raw = self._request(
            f"{base}/auth/signin",
            method="POST",
            body={
                "credentials": {
                    "personalAccessTokenName": pat_name,
                    "personalAccessTokenSecret": self.secret,
                    "site": {"contentUrl": site},
                }
            },
        )
        if status != 200:
            raise SourceError(
                f"Tableau sign-in failed. Check the secret for token '{pat_name}' on site '{site}'."
            )
        try:
            credentials = json.loads(raw)["credentials"]
            return base, credentials["token"], credentials["site"]["id"]
        except Exception as exc:
            raise SourceError("Tableau sign-in returned an unexpected response.") from exc

    def signout(self, base: str, token: str) -> None:
        try:
            self._request(f"{base}/auth/signout", method="POST", token=token)
        except SourceError as exc:
            # A failed sign-out is not worth failing a refresh over; the token
            # expires on its own. Recorded so it is not entirely invisible.
            log.debug("Tableau sign-out failed: %s", exc)

    # ------------------------------------------------------------ content
    @staticmethod
    def _as_list(payload: dict, outer: str, inner: str) -> list[dict]:
        """Tableau returns a bare object instead of a list when there is one item."""
        if not isinstance(payload, dict):
            return []
        node = payload.get(outer, {})
        node = node.get(inner, []) if isinstance(node, dict) else node
        if isinstance(node, dict):
            return [node]
        return node if isinstance(node, list) else []

    def _workbook_id(self, base, token, site_id) -> str:
        workbook = str(self.config.get("workbook") or "").strip()
        if not workbook:
            raise SourceError("No Tableau workbook selected.")
        key = urllib.parse.quote(workbook, safe="")
        status, raw = self._request(
            f"{base}/sites/{site_id}/workbooks/{key}?key=contentUrl", token=token
        )
        if status != 200:
            raise SourceError(f"Tableau workbook '{workbook}' was not found (HTTP {status}).")
        try:
            workbook_id = str(json.loads(raw).get("workbook", {}).get("id") or "").strip()
        except Exception:
            workbook_id = ""
        if not workbook_id:
            raise SourceError(f"Tableau workbook '{workbook}' returned no id.")
        return workbook_id

    def _views(self, base, token, site_id) -> list[dict]:
        workbook_id = self._workbook_id(base, token, site_id)
        status, raw = self._request(
            f"{base}/sites/{site_id}/workbooks/{workbook_id}/views", token=token
        )
        if status != 200:
            raise SourceError(f"Could not list views (HTTP {status}).")
        try:
            return self._as_list(json.loads(raw), "views", "view")
        except Exception:
            return []

    def _view_id(self, base, token, site_id) -> str:
        target = str(self.config.get("view") or "").strip()
        if not target:
            raise SourceError("No Tableau view selected.")
        wanted = target.lower()
        for view in self._views(base, token, site_id):
            candidates = {
                str(view.get("viewUrlName") or "").lower(),
                str(view.get("name") or "").lower(),
            }
            content_url = str(view.get("contentUrl") or "").lower()
            if wanted in candidates or content_url.endswith(f"/{wanted}"):
                view_id = str(view.get("id") or "").strip()
                if view_id:
                    return view_id
        visible = ", ".join(
            str(v.get("viewUrlName") or v.get("name") or "?")
            for v in self._views(base, token, site_id)[:20]
        )
        raise SourceError(
            f"View '{target}' not found. Views in this workbook: {visible or '(none)'}"
        )

    def _query_view_csv(self, base, token, site_id, view_id, period, filters) -> str:
        params = {"maxAge": "1"}
        start_field = str(self.config.get("date_start_field") or "").strip()
        end_field = str(self.config.get("date_end_field") or "").strip()
        if start_field:
            params[f"vf_{start_field}"] = period.start.isoformat()
        if end_field:
            params[f"vf_{end_field}"] = period.end.isoformat()
        for entry in filters:
            field = str(entry.get("field") or "").strip()
            if field:
                params[f"vf_{field}"] = str(entry.get("value") or "")

        # Field names must use %20, not '+', or Tableau silently ignores the filter.
        query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        status, raw = self._request(
            f"{base}/sites/{site_id}/views/{view_id}/data?{query}", token=token
        )
        if status != 200:
            raise SourceError(
                f"Tableau data request failed (HTTP {status}) for "
                f"{period.start} to {period.end}."
            )
        return raw.decode("utf-8-sig", errors="replace")

    def fetch_csv(self, base, token, site_id, period) -> str:
        """Download the view, verifying that filters were actually applied.

        Tableau ignores a view filter whose key does not match the underlying
        field rather than reporting an error, so a workbook rename would
        silently put other branches on the board. When the returned data
        contains values the filter should have excluded, the response itself
        reveals the real column caption, and we retry once with that.
        """
        view_id = self._view_id(base, token, site_id)
        filters = list(self.config.get("filters") or [])
        csv_text = self._query_view_csv(base, token, site_id, view_id, period, filters)

        guard = next(
            (f for f in filters if norm(f.get("field")) in {norm(a) for a in BRANCH_ALIASES}),
            None,
        )
        if not guard:
            return csv_text

        expected = str(guard.get("value") or "").strip().lower()
        column, values = branch_profile(csv_text)
        if not values or all(value.lower() == expected for value in values):
            return csv_text

        if column and norm(column) != norm(guard.get("field")):
            retried = [
                {**f, "field": column} if f is guard else f for f in filters
            ]
            return self._query_view_csv(base, token, site_id, view_id, period, retried)

        raise SourceError(
            f"Tableau ignored the '{guard.get('field')}' filter and returned "
            f"{', '.join(sorted(values))}. Check the field name on the mapping screen."
        )

    # ------------------------------------------------------------ interface
    def test_connection(self) -> ConnectionResult:
        try:
            base, token, site_id = self.signin()
        except SourceError as exc:
            return ConnectionResult(ok=False, message=str(exc))
        try:
            views = self._views(base, token, site_id) if self.config.get("workbook") else []
            return ConnectionResult(
                ok=True,
                message="Connected to Tableau.",
                detail={"views": [v.get("name") for v in views]},
            )
        except SourceError as exc:
            return ConnectionResult(ok=False, message=str(exc))
        finally:
            self.signout(base, token)

    def discover(self) -> dict:
        base, token, site_id = self.signin()
        try:
            status, raw = self._request(
                f"{base}/sites/{site_id}/workbooks?pageSize=200", token=token
            )
            workbooks = []
            if status == 200:
                try:
                    workbooks = self._as_list(json.loads(raw), "workbooks", "workbook")
                except Exception:
                    workbooks = []
            reports = [
                {"id": w.get("contentUrl"), "name": w.get("name")}
                for w in workbooks
                if w.get("contentUrl")
            ]
            views = []
            if self.config.get("workbook"):
                views = [
                    {"id": v.get("viewUrlName"), "name": v.get("name")}
                    for v in self._views(base, token, site_id)
                ]
            return {"reports": reports, "views": views, "columns": [], "filters": []}
        finally:
            self.signout(base, token)

    def fetch(self, period: Period) -> list[SourceRecord]:
        base, token, site_id = self.signin()
        try:
            csv_text = self.fetch_csv(base, token, site_id, period)
        finally:
            self.signout(base, token)
        return parse_csv(csv_text, mapping=self.config.get("mapping"))
