# The privacy gate

Client mail, client meetings and client repos never reach a model. That
covers Claude, Haiku, and any model Atlas may use later. Atlas still stores
and counts what arrives, and you still see your own mail in Gmail. No model
reads its sender, subject or body.

The code is `core/privacy.py`. `model_visible(signal)` is the only
decision, and every path that hands a signal to a model goes through it.

## Safe-start (the default)

While safe-start is on, **no model reads any Gmail, Calendar or GitHub
signal**, private or not. Atlas stores the sender and subject only (no body),
scores each one by rules (urgency 1, importance from the known-people tier),
and models see only a count: "3 new mail, calendar or GitHub signals, not yet
read by a model (safe-start)." Canvas is not affected.

Two switches must both be on before any model reads mail:

1. `ATLAS_MAIL_MODEL_READS=true` in `.env` (default false; needs a restart)
2. `mail_model_reads: true` in `data/model_exclude.yaml` (default false)

A missing or broken rules file keeps model reads off, and so does a missing
switch.

### Turning model reads on

1. Connect Google read-only (`scripts\connect.bat`) and let Atlas run for a
   day or two.
2. In **your own terminal**, run `python -m scripts.exclusion_preview`. It
   reads the headers and label names of your last 50 inbox messages (never a
   body) and prints each sender's domain and the rule it matched, or
   "visible". Do not paste its output into any chat.
3. Add rules to `data/model_exclude.yaml` until everything that should be
   private shows a rule label. Run the preview again.
4. Set `mail_model_reads: true` in the file and `ATLAS_MAIL_MODEL_READS=true`
   in `.env`, then restart Atlas (`scripts\deploy.bat`, or restart the
   AtlasWorker and AtlasWeb services).

From then on, models read non-private mail, calendar and GitHub signals.
Private signals stay hidden however the switches are set.

## What makes a signal private

Rules live in `data/model_exclude.yaml` (gitignored; the format, with
comments, is in `config/model_exclude.example.yaml`). Each rule has a
**label**, and the label is the only thing about a private item that a model
or a brief ever shows.

| Field | Matches |
|---|---|
| `senders` | exact addresses, any case |
| `domains` | the sender's domain, including subdomains (`client.com` also covers `mail.client.com`, but not `notclient.com`) |
| `labels` | Gmail label names, any case. Atlas resolves label ids through `labels.list` and caches them for an hour. If the names cannot be read while a label rule exists, the message is private ("unchecked Gmail label"). |
| `recipients_domains` | mail you sent or were copied on, by To or Cc domain |
| `calendar.attendee_domains` | an event whose organizer or any attendee is at these domains (falls back to `domains` when not given) |
| `calendar.title_keywords` | an event whose title contains one of these words |
| `github.orgs`, `github.repos` | every repo of an owner, or single `owner/name` repos |

An exclusion beats every other rule, including the known-people list in
`config/context.yaml`: a known person at an excluded domain is still private.

### Adding a rule

Edit `data/model_exclude.yaml`, add a value under an existing label or a new
`- label: ...` block, and save. Atlas re-reads the file within a minute; no
restart. Signals stored before the edit keep their old status.

### The content screen

With `content_screen: true` (the default), local regular expressions mark a
message private when its own subject or text looks like patient data. No
model is involved. The label is "possible patient data". The patterns:

- an SSN-shaped number (`123-45-6789`)
- `DOB`, `D.O.B.` or "date of birth"
- `MRN` or "medical record"
- "patient" next to a capitalized first and last name ("Patient: Jane Roe",
  "Jane Roe, a patient")
- a member, subscriber, policy, insurance or insured ID, number or `#`

Ordinary mail ("thanks for your patience", order numbers, phone numbers)
does not match. The tests list both kinds.

### A Gmail label and filter for anything else

For mail no domain rule catches:

1. In Gmail, open Settings, then Labels, then "Create new label", and name it
   `atlas-private`.
2. Open Settings, then "Filters and Blocked Addresses", then "Create a new
   filter". Enter the senders or words, click "Create filter", tick "Apply
   the label: atlas-private", and click "Create filter".
3. The `private label` rule in your file already lists `atlas-private`.

## What a private signal looks like

It is stored with **no body**, the subject `Private message (<label>)`,
`private = true` and `private_rule = <label>` (migration 035). Its triage is
rules-only. Briefs end with one line added by code after the model's text,
in both full and quiet briefs:

> Private mail Atlas did not read: dental clients 3, Oakline 5.

Labels and counts only, never an address or a subject. A private arrival
also counts as a change, so it never lets a quiet brief go out in its place.

## Model-visible or not

| Path | What a model sees |
|---|---|
| Triage (`core/loop/triage.py`) | visible signals only; the rest are scored by rules and never sent |
| Prepare and senses dispatch (`core/loop/senses.py`) | visible signals only |
| Briefs and the executive loop (`core/signals.py` `brief_block`, `core/loop/briefs.py`) | visible signals; the private line is appended by code after the model |
| Consolidation context packs (`core/memory/consolidation.py`) | visible signals only |
| Knowledge-graph extraction | reads CEO replies, whose inputs are already gated |
| CEO open loops (`core/context_pack.py` `open_loops_block`) | excluded addresses dropped; in safe-start, mail-derived loops become a count |
| CEO `plan_week` agenda (`core/planner.py` `for_model`) | private and gated items become placeholders |
| Job-mail scan (`core/jobs/mail.py`) | private and screened messages are skipped; in safe-start no model reads job mail (rules only) |
| Career agent `get_application` | mail-derived events read "(from mail, not read by a model)" in safe-start |
| Mail learning (`core/mail_learning.py`) | threads with an excluded address are dropped; the project-labelling model call is off in safe-start |
| MCP `recent_signals`, `get_project_context` | placeholders only for private and gated signals |
| Canvas | not gated |

The owed-promises block in each brief goes to you, not to a model, and keeps
its text.

The Today room (`core/core_view.py`) is for you, not a model, but it holds
the same line: a hand-off whose task text trips the content screen or names
an excluded domain, sender, label or keyword shows as "Private item" with the
agent names only. An approval card never shows an email body, and shows a
recipient or subject only when mail is model-visible.

## Read-only Google

Atlas asks Google for `gmail.readonly` and `calendar.readonly` only. Gmail
drafts (`draft_email`) and calendar events (`calendar_create_event`) still
exist as tools, both behind an approval card, but with read-only scopes
Google refuses them and the tool says so: "Google is connected read-only, so
Atlas cannot create Gmail drafts. Copy the draft into Gmail yourself."

A test sweeps `agents/*/tools.py` and fails if any tool whose name has an
outward verb (send, draft, email, post, reply, event, and others) has no
approve or deny row in `approval_policy`. An unmatched tool would run
without asking.

## GitHub

The connector reads two endpoints per repo: `/repos/{owner}/{repo}/events`
(pushes, pull requests, reviews, issues, comments, releases) and failed
Actions runs. Repos from an excluded org or an excluded repo become private
signals. In safe-start no GitHub signal reaches a model.
