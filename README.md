# Scoreboard

Multi-tenant sales leaderboard platform. Sales data arrives from a customer's
reporting system; the platform owns team structure, presentation and the
screens shown on office televisions.

`scoreboard` is a working codename. Run `python tools/rename.py <name>` once
the product name is decided.

## Where this came from

A single-office Raspberry Pi leaderboard, later a Windows desktop app, that
pulled one Tableau view and drove one TV. The valuable parts of it — the
metric rules and the Tableau parsing — are carried over deliberately. The
parts that tied it to one office and one machine are not.

## The two rules the design hangs on

**The source supplies numbers. The platform owns the organization.** Teams,
assignments, leads and themes survive every refresh. A source can never
overwrite them. This is why the original existed and it does not change.

**Rates and averages are always derived, never stored or summed.** A rep with
one lead and a perfect close rate must not move a team average as much as a
rep carrying two hundred. `domain/metrics.py` is the only place this
arithmetic happens, so a team total cannot disagree with the rows above it.

## Running it

```bash
cp .env.example .env
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# paste into SCOREBOARD_ENCRYPTION_KEY

docker compose up -d --build
docker compose exec api python -m scoreboard.seed
```

The API container runs `alembic upgrade head` before serving, so the schema is
current on every start.

The seed prints an ingest API key and a display URL once. They are stored as
hashes and cannot be read again — the same rule a real customer will get.

- `http://localhost:8000/health`
- `http://localhost:8000/docs` — interactive API reference

Behind TLS-inspecting antivirus or a corporate proxy, the Docker build cannot
verify certificates. See `backend/certs/README.md`.

## Tests

```bash
docker compose exec api pytest -q
```

Two kinds live here. Behaviour tests cover metric and leaderboard rules, and
Tableau parsing against recorded exports — so no live credential is ever
needed to develop. Architecture guards in `tests/test_tenancy.py` fail if a
new table appears without `org_id`, which is how tenant isolation stays a
property of the system rather than of anyone's memory.

## Layout

```
backend/src/scoreboard/
  main.py          composition root; the only place things get wired together
  models.py        multi-tenant schema
  tenancy.py       TenantScope - the single chokepoint for org isolation
  security.py      password hashing, opaque tokens, secret encryption
  domain/          metric and leaderboard rules; no database, no framework
  connectors/      one module per source system, behind a single interface
  services/        refresh and read paths
  api/             routes, split by credential type
```

## Getting data in

Two ways, and the second is the one that makes the product sellable.

**Pull.** We hold the customer's credential and fetch on a schedule. Works for
Tableau Cloud. Does not work when their reporting server sits behind a
firewall, which is common.

**Push.** The customer posts rows to the ingest endpoint with an API key.
Nothing of theirs needs to be reachable from our network and no credential of
theirs lives in our database.

```bash
curl -X POST http://localhost:8000/api/v1/ingest/reps \
  -H "Authorization: Bearer sb_..." \
  -H "Content-Type: application/json" \
  -d '{"reps":[{"rep_key":"j-doe","rep_name":"J Doe","team":"Team Alpha",
       "issued_leads":40,"pitched_leads":28,"sold_leads":12,
       "gross_split":120000,"pending_split":8000,"net_split":105000}]}'
```

Only additive components are accepted. A submitted `close_rate` is ignored,
because rates are derived.

Adding a customer's own system means adding one module to `connectors/` and
one line to its registry. Nothing downstream changes.

## Credentials

Three types, deliberately not interchangeable:

| Type | Carried by | Scope |
|---|---|---|
| API key | a machine pushing data | write, ingest only |
| Display token | a television | read, one screen |
| User session | a person in the console | role-based |

A TV cannot log in, so it holds a long opaque URL instead. Revoking the token
is how a relocated or lost screen is cut off.

Sessions are rows, not self-contained tokens. When someone leaves a company
the customer expects access to stop immediately, and a JWT cannot be withdrawn
before it expires.

Customer credentials we hold — a Tableau token, for instance — are encrypted
at rest and never returned by the API. Tokens we issue are stored as hashes.

## Roles

`owner > org_admin > branch_manager > team_lead > viewer`

Rank answers "how senior". It is never the whole answer for anything
team-shaped, because a team lead has full authority over one team and none
over the next, so those endpoints also run a scope check. That is what lets a
customer hand a team lead their own logo and colours without handing them
everyone else's.

`tests/test_auth.py` fails if a mutating endpoint is added without a
credential dependency — the mistake that actually happens.

## Status

Working: multi-tenant schema and isolation, Alembic migrations, user accounts
with revocable sessions and role-scoped permissions, the console API (teams,
assignments, credentials, sources, audit log), the four display modes, metric
rules, the Tableau and push connectors, and daily metric history.

Not built yet: the console front end, the theme editor, charts and the
dashboard builder, scheduled refresh, object storage for logos, and password
reset / invitations.
