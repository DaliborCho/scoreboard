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

- `http://localhost:8000/console` — sign in to manage teams, themes and screens
- `http://localhost:8000/tv/<token>` — what a television opens
- `http://localhost:8000/docs` — interactive API reference
- `http://localhost:8000/health`

A worker process refreshes pull sources on their own interval:
`docker compose logs -f worker`.

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
  domain/          metric, leaderboard, theme and chart rules — no database,
                   no framework, so every rule is directly testable
  connectors/      one module per source system, behind a single interface
  services/        refresh, read, scheduling and screen assembly
  api/             routes, split by credential type
  web/             the two pages a person opens: tv.html and console.html
  worker.py        background refresh loop, its own process
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

## Themes

Colours, a logo and a font from a curated set. Never CSS.

A theme that would be unreadable is **refused on save**, not saved with a
warning. A television runs unattended for months in a room where nobody can
fix it, so this has to be a rule rather than advice. Text is held to 7:1
contrast rather than the web minimum of 4.5:1, because a board is read across
a room.

Themes inherit: organization brand, then a team's own overrides on top. A team
lead may restyle their own team and no other. Multi-team views carry each
team's palette on its own card, while the whole-office board wears the
organization's, so no single team's brand takes over a screen that belongs to
everybody.

## Charts

Six types: big number, bar, donut, trend, gauge and leader list. The list is
short on purpose — "any chart you like" produces boards nobody can read from
the far side of a sales floor.

Every value is computed on the server through the same roll-up the tables use,
so a chart cannot disagree with the numbers beside it. A donut refuses a rate,
because a slice has to be a share of a whole. When a donut is trimmed to the
top few, the remainder appears as *Other* rather than being dropped.

## Status

Working: multi-tenant schema and isolation, Alembic migrations, accounts with
revocable sessions and role-scoped permissions, the full console API, the
console and television front ends, four display modes, themes with enforced
contrast, six chart types, the Tableau and push connectors, daily metric
history, and a background worker that refreshes sources on their interval.

Not built yet: object storage for uploaded logos (a logo is a URL for now),
invitations and password reset, per-screen rotation, and billing.

Known rough edges: widgets on a head-to-head board still show office-wide
figures rather than the two selected teams; the console edits teams through
browser prompts rather than a proper dialog.
