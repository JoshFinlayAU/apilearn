# apilearn / api2md — learn any API from its Swagger/OpenAPI spec

> **Audience: AI coding agents (Claude Code, etc.).** This file tells you what
> these tools are, when to reach for each, and the exact commands to run. If you
> have been asked to build, call, or understand an API and a Swagger 2.0 /
> OpenAPI 3.x JSON (or YAML) spec exists, **use these tools instead of reading
> the raw spec file** — raw specs are huge (hundreds of KB, hundreds of
> operations) and will blow your context.

Two dependency-free Python scripts (3.8+, stdlib only). They resolve local
`$ref`s, guard against reference cycles, and understand both OpenAPI 3
(`requestBody`/`content`/`components`) and Swagger 2 (`body` params,
`definitions`, `securityDefinitions`). YAML specs work if PyYAML is installed.

| Tool | Use when | Output |
|------|----------|--------|
| **`apilearn.py`** | The spec is large, or you only need a few calls. Browse and pull calls **one at a time** so context stays lean. | text or `--format json` |
| **`api2md.py`** | You want the **whole API** as one complete, cross-linked Markdown document to read or hand to a teammate. | Markdown or `--format json` |

`apilearn.py` imports `api2md.py` as a library, so **keep the two files in the
same directory.**

---

## Decision guide (for the agent)

```
Need to understand or build against an API?
│
├─ Is there a Swagger/OpenAPI JSON/YAML spec?
│   ├─ No  → ask the user for one, or read the API's own docs.
│   └─ Yes ↓
│
├─ Do you need the WHOLE API at once (overview doc, hand-off, < ~150 ops)?
│   └─ api2md.py spec.json -o API_GUIDE.md     then Read API_GUIDE.md
│
└─ Large spec, or you only need specific calls?
    └─ apilearn.py spec.json info              # overview + groups
       apilearn.py spec.json list --grep X     # find the calls you need
       apilearn.py spec.json show <selector>   # pull ONE call, fully expanded
```

**Rule of thumb:** if the spec is over ~100 operations, prefer the
`info → list → show` loop. Pull each call only when you're about to use it.

---

## apilearn.py — query one call at a time (default for big specs)

Three subcommands. Intended workflow is **info → list → show**:

```bash
# 1. Overview: spec type, auth, inferred conventions, resource groups + counts
python3 apilearn.py spec.json info

# 2. Browse calls — one terse line each, grouped by resource/tag
python3 apilearn.py spec.json list
python3 apilearn.py spec.json list --grep tenant --method GET
python3 apilearn.py spec.json list --group "Identity > Administration > Users"
python3 apilearn.py spec.json groups            # just groups + counts

# 3. Pull FULL detail for one call (params w/ enums+defaults, request body as a
#    field table, every response code + schema — nothing truncated)
python3 apilearn.py spec.json show ListTenants
python3 apilearn.py spec.json show "POST /api/ExecAddAlert"
python3 apilearn.py spec.json show ListTenants ExecAddAlert   # several at once
```

Add `--format json` to any subcommand for machine-readable output you can parse.

**Selectors** (for `show`) resolve in order: exact `operationId` → exact
`"METHOD path"` → exact path → case-insensitive substring of path/operationId.

**Why this shape:** run `list` once to learn what exists, then loop `show
<selector>` per call. Each `show` is a small, self-contained chunk, so you never
hold the entire spec in context to understand one endpoint.

---

## api2md.py — the whole spec as one complete Markdown document

```bash
python3 api2md.py spec.json                   # full digest to stdout
python3 api2md.py spec.json -o API_GUIDE.md   # write to a file (then Read it)
python3 api2md.py spec.json --compact         # endpoint list only, one line each
python3 api2md.py spec.json --tag Users       # one resource group/tag
python3 api2md.py spec.json --grep customer   # endpoints matching a term
python3 api2md.py spec.json --format json     # machine-readable digest
```

The Markdown is **complete and uncompressed** — every operation, every field of
every component schema, full descriptions and enums, no row/length caps.
Sections:

1. Overview · 2. Authentication · 3. Conventions (inferred) ·
4. Endpoints · 5. Reusable schemas (components) · 6. How to build a matching endpoint

**Cross-linking:** every reference to a component schema (in request bodies,
responses, parameters, and nested schema fields) is a Markdown link to its
definition in §5, which carries stable `<a id="schema-NAME">` anchors plus an
index. §3 and §5 are anchored (`#conventions`, `#schemas`) and the build
checklist links back to them. This makes the document navigable for both humans
and agents.

Typical agent use:

```bash
python3 api2md.py vendor-spec.json -o /tmp/api.md
# then: Read /tmp/api.md and build a matching POST /widgets endpoint that
#       follows the same auth, response envelope, and status-code conventions.
```

---

## What "conventions (inferred)" means

§3 of api2md and the `info` command report patterns **inferred by frequency**,
not declared in the spec: the dominant success-response envelope, the status
codes in use, path-naming style, and the most common query/pagination params.
Treat them as strong hints for writing a *consistent* new endpoint — and confirm
against a sibling endpoint in the same group when in doubt.

---

## Setting this up in Claude Code

You don't strictly need to install anything — the scripts are plain Python you
can invoke directly. The setup below just makes them ergonomic and removes
permission prompts.

### 1. Put the tools somewhere stable

Keep `api2md.py` and `apilearn.py` together. Either leave them in the repo (e.g.
`misc/apilearn/`) or copy both to `~/bin/`:

```bash
mkdir -p ~/bin && cp api2md.py apilearn.py ~/bin/ && chmod +x ~/bin/*.py
```

### 2. (Optional) YAML support

Only needed if your specs are YAML rather than JSON:

```bash
pip install pyyaml
```

### 3. Pre-approve the commands (skip permission prompts)

Add to the project's `.claude/settings.json` (or `~/.claude/settings.json` for
all projects):

```json
{
  "permissions": {
    "allow": [
      "Bash(python3 *apilearn.py*)",
      "Bash(python3 *api2md.py*)"
    ]
  }
}
```

You can also just run `/fewer-permission-prompts` after the first use, or use the
`/permissions` command interactively.

### 4. Tell Claude when to use them

Add a short pointer to your project's `CLAUDE.md` so the agent reaches for these
instead of reading raw specs:

```markdown
## Learning an external API

When working against a third-party API that ships a Swagger/OpenAPI spec, use the
tools in `misc/apilearn/` rather than reading the raw spec:

- Large spec / specific calls: `python3 misc/apilearn/apilearn.py <spec> info`,
  then `list`, then `show <selector>`.
- Whole API as one doc: `python3 misc/apilearn/api2md.py <spec> -o /tmp/api.md`.

See `misc/apilearn/TOOL.md` for full usage.
```

### 5. (Optional) expose as a slash command

Create `.claude/commands/api-learn.md` so you can type `/api-learn <spec.json>`:

```markdown
---
description: Summarise a Swagger/OpenAPI spec and list its calls
---
Run `python3 misc/apilearn/apilearn.py $ARGUMENTS info` and then
`python3 misc/apilearn/apilearn.py $ARGUMENTS list`, and summarise the API's
auth, conventions, and the calls relevant to my task.
```

### Quick smoke test

A sample spec ships in `example/`:

```bash
python3 apilearn.py example/veeamone-swagger.json info
python3 api2md.py  example/veeamone-swagger.json -o /tmp/veeam.md
```

---

## Notes & limits

- **Stdlib only**, Python 3.8+. No network access; everything is local.
- Resolves **local** `$ref`s only (`#/components/...`, `#/definitions/...`).
  External-file `$ref`s render as `x-unresolved`.
- Both tools exit cleanly when piped into `head`/`less` (no `BrokenPipeError`).
- Conventions are inferred, not authoritative — verify against a sibling endpoint.
