# Scoreboard

Multi-tenant sales leaderboard platform. Sales data arrives from a customer's
reporting system; the platform owns the organization's structure, what it
measures, how it looks, and the screens on the office televisions.

`scoreboard` is a working codename. Run `python tools/rename.py <name>` once
the product name is decided.

## Where this came from

A single-office Raspberry Pi leaderboard, later a Windows desktop app, that
pulled one Tableau view and drove one TV. The valuable parts of it — the
metric rules and the Tableau parsing — are carried over deliberately. The
parts that tied it to one office and one machine are not.

## The two rules the design hangs on

**The source supplies numbers. The platform owns the organization.** Groups,
assignments, leads and themes survive every refresh. A source can never
overwrite them. This is why the original existed and it does not change.

**Rates and averages are always derived, never stored or summed.** A rep with
one lead and a perfect close rate must not move a group average as much as a
rep carrying two hundred. There is one place this arithmetic happens, so a
group total cannot disagree with the rows above it.

Everything else a customer might need — which axes they are divided along,
what they measure, how a metric is calculated — is data they own rather than
code we ship. The two rules above hold whatever they put in it.

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

The seed creates two deliberately trivial logins so the installation can be
looked at without ceremony — `admin / admin` for the platform panel and
`demo / demo` for the demo company's console. The admin panel says so on
every page while it is served from a private address. **Change both before
this is reachable from anywhere else.**

It also prints an ingest API key and a display URL once. Those are stored as
hashes and cannot be read again — the same rule a real customer will get.

- `http://localhost:8000/admin` — platform operator: add and manage companies
- `http://localhost:8000/console` — a company's own staff: structure, metrics,
  themes, screens
- `http://localhost:8000/architecture` — a diagrammed map of the whole system
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

Three kinds live here.

**Behaviour tests** cover metric, formula, leaderboard, theme, chart and
scheduling rules, and Tableau parsing against recorded exports — so no live
credential is ever needed to develop.

**Architecture guards** fail if a new table appears without `org_id`
(`tests/test_tenancy.py`) or if a mutating endpoint is added without a
credential dependency (`tests/test_auth.py`), which is how those properties
stay in the system rather than in anyone's memory.

**Database-backed tests** exist because the first two were once not enough,
and it is worth saying how. Threading the metric catalogue through the read
path introduced a parameter named `metrics` that collided with a local already
holding a row of stored figures; inside that loop the catalogue silently
became a database row. All 127 pure tests passed and the board would have
raised on the first request. So `tests/test_board_db.py` and
`tests/test_groups_db.py` build an organization, write to it and read back —
uniqueness, cascades, inheritance, roll-ups — and they skip when no database
is reachable, so the pure suite still runs anywhere. The second instance of
that same mistake was caught seconds after they existed.

The pages have no build step, so nothing catches a broken string literal
until a browser refuses to run the script and shows a blank screen. This does:

```bash
python tools/check_pages.py
```

And this fails if a model has changed without a migration — drift is invisible
in development and surfaces as a broken deploy:

```bash
DATABASE_URL=... python tools/check_migrations.py
```

`.github/workflows/ci.yml` runs all three on every push.

## Layout

```
backend/src/scoreboard/
  main.py          composition root; the only place things get wired together
  models.py        multi-tenant schema
  tenancy.py       TenantScope - the single chokepoint for org isolation
  security.py      password hashing, opaque tokens, secret encryption
  domain/          metric, formula, leaderboard, theme and chart rules — no
                   database, no framework, so every rule is directly testable
  domain/formula.py customer-written arithmetic, parsed and walked rather
                   than executed
  connectors/      one module per source system, behind a single interface
  services/        refresh, read, scheduling and screen assembly
  api/             routes, split by credential type
  api/platform.py  the one module that deliberately crosses organizations
  web/             tv.html, console.html, admin.html and the system map
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

Only stored components are accepted. A submitted `close_rate` is ignored,
because rates are derived.

Which keys count as stored is the organization's own catalogue, so a customer
can send a metric nobody shipped. A key that matches nothing is **named back
in the response** rather than silently dropped — someone sending `revenue` to
a catalogue that calls it `net_split` is told so instead of watching zeros
appear on a wall.

Adding a customer's own system means adding one module to `connectors/` and
one line to its registry. Nothing downstream changes.

## Structure

A company is divided along axes it chooses. There used to be a `teams` table
and a `branches` table, which between them answered the two questions we had
guessed and none of the ones customers actually ask — compare regions, compare
product lines, compare the people hired this year against last. Each of those
needed a third table and a release.

A **group type** is a row. `team` and `branch` ship with every organization
because the roles name them, but they are instances of the idea rather than
the idea itself, and adding one of your own is a form. Boards, charts, themes
and permission checks all read group types, so a new axis works everywhere the
moment it exists.

Two rules hold it together, both enforced rather than remembered:

- **One group per person per axis** is a unique constraint.
- **A group may sit inside a group of another type**, and membership and
  authority both flow down that chain. Somebody on Team Alpha, which sits in
  the Olympia branch, is in Olympia — inferred, never stored twice, so the two
  cannot disagree. A direct assignment still beats an inherited one, which is
  how one person seconded to another office stays possible.

A board is grouped along one axis and carries all of them, which is what lets
a screen ranked by team hold a chart broken down by branch, from one query.

## What a company measures

The metric catalogue is rows a customer owns. A stored value comes from their
source and is summed; a calculated one carries its formula and is worked out
fresh at whatever level is being totalled.

Most calculated metrics are one division, and the console offers that as three
labelled boxes because it is a better form for it. Anything else is written as
arithmetic:

```
(sold_leads - refunds) / issued_leads * 100
net_split * (0.12 if sold_leads >= 10 else 0.08)
```

Expressions are parsed with Python's own parser and then walked against an
allowlist of the nodes arithmetic needs. Nothing is compiled and `eval` is
never called, so there is no path from a form field to running code — which
matters here more than in most places, because the box is reachable from the
public internet and the person typing into it is a sales manager.
`tests/test_formula.py` fires eighteen attempts at it and each is refused by
the parser rather than by a filter.

The summing rule survives whatever the expression says, because it is
evaluated *after* the components are summed. A tiered commission where two
people each earn 8% can therefore total at 12%, since together they clear the
threshold. That is the correct answer, and the reason derived values are never
stored.

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

## Two kinds of authority

**Inside a company:** `owner > org_admin > branch_manager > team_lead > viewer`

**Outside every company:** the platform operator, who adds customers, renames,
suspends and deletes them, and can see how many groups and people each has.

These are deliberately not the same ladder. Operator status is a flag on the
account, not a very senior `Role`, because `Role` answers "what may you do
inside one organization" and this answers "may you stand outside all of them".
Conflating them would make every tenant guard in the system weaker than it
reads — `require_role` would quietly become "…or the operator".

The consequences are visible in the code and enforced by tests: an operator
holds a session that belongs to no organization, fails every rank check, and
is refused by every tenant-scoped route. Looking inside a customer's data
means granting yourself an ordinary account there, which appears in that
customer's own people list and audit log rather than happening invisibly.

## Roles inside a company

`owner > org_admin > branch_manager > team_lead > viewer`

Rank answers "how senior". It is never the whole answer for anything
group-shaped, because a team lead has full authority over one team and none
over the next, so those endpoints also run a scope check. That is what lets a
customer hand a team lead their own logo and colours without handing them
everyone else's.

Scope is one group, and authority reaches everything beneath it. A branch
manager holds a branch; teams sit inside branches; so the teams they may edit
are simply the ones underneath. Adding a level later widens their reach
automatically instead of needing another column.

`tests/test_auth.py` fails if a mutating endpoint is added without a
credential dependency — the mistake that actually happens.

A group's look is edited from the group itself, in the Structure list, rather
than from a separate menu where you would first have to remember which of five
dropdowns picks the one you meant.

## Themes

Colours, a logo and a font from a curated set. Never CSS.

A theme that would be unreadable is **refused on save**, not saved with a
warning. A television runs unattended for months in a room where nobody can
fix it, so this has to be a rule rather than advice. Text is held to 7:1
contrast rather than the web minimum of 4.5:1, because a board is read across
a room.

Themes inherit: organization brand, then a group's own overrides on top. A
team lead may restyle their own group and no other. Multi-group views carry
each group's palette on its own card, while the whole-office board wears the
organization's, so no single group's brand takes over a screen that belongs to
everybody.

Artwork has three slots rather than one "logo", because on a wall a group is
recognised by its crest long before its name is read: a **hero** banner across
the top, a **badge** shown beside each rep in the group column, and a modest
**header logo** for boards where a hero is too much. A background texture and
an optional frame in the group colour complete the look. The background dim is
part of the theme and is validated on save — contrast is measured against the
flat colour, so artwork is never allowed to show through brightly enough to
fight the text in front of it.

The demo organization ships with placeholder crests so a fresh installation
looks like a leaderboard rather than a spreadsheet. The files are committed
under `backend/src/scoreboard/demo_assets/`; `tools/make_demo_logos.py` is how
they were drawn, and it wants Pillow, which the product itself does not — the
application never processes an image, it only stores and serves what it is
given.

## Previewing

The console does not draw its own version of a board. `/preview/screen/<id>`
and `/preview/live?mode=…` serve the television's own page, authenticated by
the console session already in that browser, and the console embeds it in a
scaled frame. A preview built as a second implementation drifts, and then it
is showing something nobody will ever see on a wall.

## Displays

A television carries a long unguessable link instead of signing in, and can
either sit on one screen or cycle through several. The next screen in a cycle
is chosen by the server and carried in the payload, so a rotation edited in the
console takes effect on the next tick rather than when someone remembers to
reload the wall.

Uploaded logos are addressed by an unguessable key rather than an id, because
a television has no session to authenticate an image request — and an
enumerable URL would let one customer walk another's brand assets. What a file
claims to be is not evidence: its own first bytes decide, and SVG is refused
outright because it can carry script.

## Charts

Six types: big number, bar, donut, trend, gauge and leader list. The list is
short on purpose — "any chart you like" produces boards nobody can read from
the far side of a sales floor.

Every value is computed on the server through the same roll-up the tables use,
so a chart cannot disagree with the numbers beside it. A donut refuses a rate,
because a slice has to be a share of a whole. When a donut is trimmed to the
top few, the remainder appears as *Other* rather than being dropped.

## Where a number came from

A board is arithmetic performed on somebody else's data and shown to a room.
The first thing anybody says about a figure they dislike is that it must be
wrong, so every total on the console board opens the rows behind it.

Clicking a figure shows the formula, the totals it was evaluated against — 82
sales out of 506 leads — and everybody who contributed, with the components
they contributed. Nothing is stored to make this work: it reruns the board's
own roll-up. Deliberately the same one, because an explanation that computed
its own total could disagree with the figure it claims to explain, and then
there are two wrong numbers and no way to tell which.

Underneath that, each capture keeps the payload or export line it came from,
including the columns nobody mapped — mapping is guesswork until somebody can
see what was really there. It is trimmed, but never silently: what was dropped
is counted in its place. And every capture is kept rather than only the newest,
so "it was different yesterday" is a claim the product can settle rather than
deny.

## Status

Working: multi-tenant schema and isolation, Alembic migrations, accounts with
revocable sessions and role-scoped permissions, customer-defined groupings and
metrics, the full console API, the console, admin and television front ends,
four display modes along any axis, themes with enforced contrast, six chart
types, the Tableau, JSON-API and push connectors, daily metric history with
the source row behind each capture, and a background worker that refreshes
sources on their interval.

Not built yet: email delivery — so an administrator creates an account and
hands over a first password instead of sending an invitation, and resets one
the same way — object storage (uploads go to a mounted volume, which is one
adapter away from S3), per-organization timezones, and billing.

Rate limiting counts per process. With one API container that is exact; with
several it becomes per-container, which weakens the limit without breaking
anything. Moving the counters to Redis is a change to one file.
