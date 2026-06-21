---
name: veans
description: >-
  How to use the veans CLI to track work in Vikunja — the durable, shared task
  store for a project. Use this whenever you are working in a repo that has a
  .veans.yml (or one where the user wants veans set up), when asked to "set up
  veans", "init veans", "track this in Vikunja", "create a task/issue" for a
  project, or when you hit veans quirks (scrapped tasks not marked done, label
  permission errors, status transitions). Also use it to wire up a new
  project's Kanban board and buckets. If a project tracks tasks in Vikunja,
  prefer veans over TodoWrite for anything that should outlive the session.
---

# veans

`veans` is a CLI wrapper around a Vikunja instance. It gives each project its
own bot identity (`bot-<repo>`) and a curated set of task commands. In a
configured repo, the `veans prime` hook injects the full day-to-day workflow
(commands, HTML description format, status model) at session start — **this
skill does not repeat that**. This skill covers the things `prime` doesn't:
how to **bootstrap veans in a project**, the **durable-vs-in-flight tracking
model**, the **known quirks and their workarounds**, and **Kanban/bucket
setup**.

If `veans prime` runs and emits a prompt, follow it for normal task ops. Run
`veans prime` yourself any time you're unsure of the current project's IDs,
buckets, or commands.

## The tracking model: Vikunja is durable, the task list is in-flight

These two systems play different roles — use both, deliberately:

- **Vikunja (via `veans`) is the durable store for _work_.** A task holds the
  plan (what to do), the live status, and the work log — the decisions taken
  and *why* — carried in its description and comments as the work evolves. It
  is visible to the human and to future sessions. Prefer it over the local task
  list for anything about the work that should outlive this session.
- **The local task list is your in-flight scratchpad.** It shows the human, in
  this session, what you're actively working on. It evaporates when the session
  ends.

**Every local task-list entry must carry a pointer to the Vikunja task it
mirrors** — its veans identifier, so you (and the human) remember to push the
durable update back as work moves. Use the project's identifier prefix:
`PROJ-NN` if the project has an identifier set (e.g. a task-list subject like
`[VKMIG-12] Wire the auth flow`), or `#NN` if it doesn't (e.g. `[#12] …`). That
pointer is exactly what `veans show` / `veans update` resolve, so it's directly
actionable. The flow is: create/claim the Vikunja task → mirror it onto the
local task list with its identifier → as you work, update *both*, keeping the
Vikunja task authoritative.

Vikunja is a **work** tracker, not a general memory or knowledge base. It holds
work plans and work logs; a task's description and comments stand as the record
of what was decided and why, scoped to that unit of work. A note that isn't
about a piece of work doesn't belong in a task — keep that wherever your other
durable notes live.

## Setting up veans in a new project (`veans init`)

`veans init` onboards veans into the current repo. You (the agent) can drive
almost all of it — **only the browser sign-in needs the human.**

What it does:
1. Authenticates **as the human** (default: OAuth 2.0 + PKCE — prints a URL).
2. Picks a Vikunja project and Kanban view.
3. Bootstraps the canonical buckets (Todo / In Progress / In Review / Done /
   Scrapped).
4. Creates a `bot-<repo>` user, shares the project with it, mints its API token.
5. Stores the **bot's** token in the keychain (or `~/.config/veans/credentials.yml`).
6. Writes `.veans.yml` to the repo root, and optionally wires the `prime` hook.

### Who does what

- **Ownership follows the authenticating identity, not who types the command.**
  The bot is owned by whoever completes step 1's sign-in. So the human must do
  the browser auth if the bot is to be theirs. This is the *one* reason the
  human is involved — not because the command itself needs them.
- **You can run the command and hand off only the browser step.** With flags
  you skip every interactive prompt; when veans prints the OAuth URL, ask the
  human to open it, sign in, and paste the callback URL back.

### What you need from the human

`veans init` attaches to an **existing** project — it doesn't create one. So if
the project doesn't exist yet, ask the human to create it in Vikunja first (and
add a Kanban view).

Then ask the human to **open the project's Kanban view in Vikunja and paste the
URL**. It looks like `https://<host>/projects/<PROJECT_ID>/<VIEW_ID>` (e.g.
`https://vikunja.banjo-plant.xyz/projects/28/129`) — parse all three values
from it: server = scheme + host, project ID and view ID = the two path numbers.
One paste beats asking for three fields. Then:

```bash
veans init \
  --server <SERVER_URL> \
  --project <PROJECT_ID> \
  --view <VIEW_ID> \
  --yes-buckets \
  --install-claude \
  --install-opencode
# veans prints an OAuth URL → human signs in → pastes the callback URL back.
```

Useful flags: `--server <url>`, `--bot-username <name>` (override `bot-<repo>`),
`--skip-buckets` (buckets already exist), `--token <PAT/JWT>` (bypass the
browser entirely — for SSO/OIDC instances or when the human hands you a token),
`--use-password --username <u>` (force `POST /login`).

After init, verify read access with a safe `veans list` before doing anything
that writes. To let this bot file bugs upstream (see "Found a bug" below), James
also adds it to the `veans-bots` team once — that grants write access to the
shared veans-intake project.

### Set a project identifier

`init` does not set a project identifier, so tasks render as bare `#NN`. Set a
short mnemonic (e.g. `VKMIG`) so tasks become `VKMIG-3` — readable in lists and
unambiguous as task-list pointers. There's no curated command; use `veans api`
against the project-update endpoint with an `identifier` field. Confirm the
exact path with `veans api GET /projects/<PROJECT_ID>` first, then send the
update. Once set, use that prefix in every `PROJ-NN` pointer.

## Known quirks and workarounds

These are real behaviours of the Vikunja-plus-`veans` setup, observed in
practice. Don't rediscover them the hard way. (The instance is a moving dev
build — run `veans api GET /info` for the exact version if a behaviour here
ever seems to have changed.)

### Scrapped does not set `done=true` yet — set it yourself

`veans update #N -s scrapped` moves the task to the Scrapped bucket but leaves
the task's `done` flag **false**. So a scrapped task still counts as open in
any `done`-based filter. Until veans fixes this, when you scrap, also mark it
done and tag it so the distinction is queryable:

```bash
veans update #N -s scrapped --reason "obsolete: <why>" \
  --label-add wontfix
# then set done as well (status alone won't):
veans api POST /tasks/N -d '{"done": true}'
```

Convention this enables:
- **Done** = `done = true` **without** `wontfix` — finished normally.
- **Scrapped** = `done = true` **with** `wontfix` — abandoned.

(The `veans:` prefix is auto-added, so `--label-add wontfix` becomes
`veans:wontfix`.)

### Applying a label can 403 even with write access — visibility is emergent

A non-owner can apply a label only if they own it **or** it is already attached
to at least one task in a project they can access. There is no label-sharing
grant. So the *first* time a `wontfix` (or any) label is used, the **label's
owner** must attach it once to a task in the shared project; after that the bot
can apply/remove it freely. If you get a permission error applying a label that
plainly exists, this is why — ask the human (label owner) to attach it once to
bootstrap visibility. Use single `apply-label` for the bootstrap; the bulk
endpoint rejects token auth.

### Read-only introspection: use `veans api`, never curl/JWT

To inspect Vikunja state the curated commands don't surface (a task's
`created_by`, raw view config, a comment thread, etc.), use `veans api GET ...`.
It reuses the bot's stored token, so you never hand-build a `curl` call with a
JWT — which is both fragile and a credential-handling risk. `veans api` is the
escape hatch for any endpoint the curated commands don't wrap.

### Updating a task can clobber assignees

On the unstable build, a task `update` that doesn't echo the existing assignees
can drop them. If you must update assignment-bearing tasks, read first and
preserve the `assignees` list. (Most agent flows don't touch assignees, so this
rarely bites.)

### Views have no persistent default sort

A Vikunja view cannot be configured to default to e.g. priority-descending —
sort (`sort_by`/`order_by`) is supplied per request by the client and lives in
the URL, never persisted on the view. The view's stored filter `sort_by` is not
honored by the read path. Don't promise the human a "default sort"; the only
persistent reordering is bucket grouping (below) or priority-encoded manual
order.

## Found a bug in veans or this setup? Report it

You run as your own project's bot. To flag a bug for the veans maintainer agent
`bot-veans`, file it in the shared **veans-intake** project (id 34) and
@mention `bot-veans`. It goes there — not your own board — because `bot-veans`
can only see projects shared with it, and veans-intake is the shared funnel:

```bash
veans api PUT /projects/34/tasks --data '{
  "title": "bug: <one-line summary>",
  "description": "<p><mention-user data-id=\"bot-veans\" data-label=\"veans bot\"></mention-user> Repro, expected vs actual, and the version from <code>veans api GET /info</code>.</p>"
}'
```

The mention's `data-id` is the username `bot-veans` (the form the web UI emits).
Your bot must be a member of the `veans-bots` team for the write to land — James
adds each project's bot to that team as part of onboarding.

## Reading comments and picking up instructions

Humans steer work by **leaving comments on tasks** — often @-mentioning the bot
(e.g. *"@bot-veans switch origin to https"*). veans has **no first-class
"show me new comments" command** (the comment-detection gap), so you have to
look for them deliberately. Do this at the start of a work session and after
long-running steps, so you don't miss a course-correction.

**Find tasks with recent activity.** A new comment bumps the task's `updated`
timestamp, so sort by it and look at what changed since you last checked:

```bash
veans api GET '/projects/<PROJECT_ID>/tasks?sort_by=updated&order_by=desc&per_page=10'
```

Track the most recent `updated` you've seen between runs; anything newer may
carry a new comment.

**Read the comments themselves:**

```bash
veans api GET '/tasks/<TASK_ID>/comments'
# → [{ author:{username}, created, comment (HTML) }, …], oldest first.
```

Comments are HTML; an @-mention renders as a `<mention-user data-id="…">` tag.

**Respond in-thread** so the human sees you got it:

```bash
veans update #N --comment '<p>On it — switching origin to HTTPS.</p>'
```

## Kanban / bucket setup

### Default: the canonical manual buckets (what `init` creates)

`init` creates five **manual** buckets — Todo, In Progress, In Review, Done,
Scrapped — and this is what the veans status model drives. `veans claim` and
`veans update -s <status>` move a task into the matching bucket by writing its
bucket assignment. **Keep manual mode unless you have a specific reason not
to** — it's the only mode in which veans' status transitions and drag-to-move
both work.

Designate the **Done** bucket as the view's `done_bucket_id`: in manual mode,
dragging a card into the done bucket auto-sets `done=true` (and out of it clears
it). This is the one place a bucket writes a task attribute.

### View-level filters (hiding completed work)

To make a List/Table/Kanban view show only open work, set the view's filter to
`done = false`. This is a display filter on the whole view (set via the
project-view update endpoint, or the Vikunja UI's view settings) — distinct from
per-bucket filters below.

### Filter-mode buckets (derived boards) — advanced, with a real tradeoff

A view can switch `bucket_configuration_mode` from `manual` to `filter`. In
filter mode, each bucket has its own filter and a task **appears** in a bucket
because its attributes match — membership is *derived*, recomputed on every
read. This is how you'd express the two-tier done model as a board:

- **To-Do**: `done = false`
- **Done**: `done = true && labels not in <wontfix_label_id>`
- **Scrapped**: `done = true && labels in <wontfix_label_id>`

(Filters use bare label **IDs**, not names, and the bucket filters must be
mutually exclusive.)

**Critical caveats — understand before switching:**
- **Filter mode buckets are synthetic** (no DB rows). You **cannot drag** tasks
  between them, and `veans update -s <status>` / `veans claim` — which move
  tasks by *writing a bucket assignment* — will **not** place tasks correctly.
  In filter mode you move a task between columns only by **changing its
  attributes** (`done`, labels, `percent_done`).
- **A bucket cannot set a label, and in filter mode cannot set `done`** (the
  auto-mark-done behaviour is manual-mode only). So the agent must set `done`
  and the `wontfix` label itself — exactly the scrapped workaround above. The
  board then sorts itself.

So: filter mode gives a clean attribute-derived layout but **breaks veans'
status-move workflow**. Only adopt it for a project whose agents drive status by
setting attributes directly. For the standard veans workflow, stay on manual
buckets.

Setting bucket config isn't wrapped by a curated command — use `veans api`
against the project-view update endpoint (confirm the exact path/verb with
`veans api GET` against the view first), sending `bucket_configuration_mode`
and a `bucket_configuration` array of `{title, filter}` entries.
