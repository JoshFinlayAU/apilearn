# API learning tools for Claude Code

Two dependency-free Python tools (3.8+) that turn a Swagger 2.0 / OpenAPI 3.x
JSON spec into something an LLM can actually learn from. They handle both
OpenAPI 3 (`requestBody`/`content`/`components`) and Swagger 2 (`body` params,
`definitions`, `securityDefinitions`), resolve local `$ref`s, and guard against
reference cycles. YAML works too if PyYAML happens to be installed.

| Tool | Use when |
|------|----------|
| **`apilearn.py`** | The spec is large. Browse and pull calls **one at a time** so context stays lean. |
| **`api2md.py`** | You want the **whole API** distilled into a single Markdown document. |

Raw specs are huge (the bundled CIPP spec is ~945 KB / 498 operations) and mostly
boilerplate. Both tools strip that and surface only what's needed to author a new
endpoint consistently: auth, the success response envelope, status codes, path
naming, query/pagination params, request/response shapes, and component schemas.

---

## apilearn.py — query one call at a time (recommended for big specs)

Three subcommands. The intended agent workflow is **info → list → show**:

```bash
# 1. Overview: auth, conventions, and the resource groups
python3 apilearn.py spec.json info

# 2. Browse the calls — one terse line each, grouped by resource
python3 apilearn.py spec.json list
python3 apilearn.py spec.json list --grep tenant --method GET
python3 apilearn.py spec.json list --group "CIPP > Core"
python3 apilearn.py spec.json groups          # just the groups + counts

# 3. Pull full detail for ONE call (params, request body, responses — all expanded)
python3 apilearn.py spec.json show ListTenants
python3 apilearn.py spec.json show "POST /api/ExecAddAlert"
```

Add `--format json` to any subcommand for machine-readable output.

**Selectors** (for `show`) match in this order: exact `operationId` → exact
`"METHOD path"` → exact path → case-insensitive substring of path/operationId.
You can pass several selectors at once.

### Why this shape

An agent runs `list` once to learn what exists, then loops, calling `show
<selector>` per call. Each `show` is a small, self-contained chunk — so the model
never has to hold the entire 900 KB spec in context to understand and replicate
a single endpoint.

---

## api2md.py — the whole spec as one Markdown digest

```bash
python3 api2md.py spec.json                  # full digest to stdout
python3 api2md.py spec.json -o API_GUIDE.md  # write to a file
python3 api2md.py spec.json --compact        # endpoint list only, one line each
python3 api2md.py spec.json --tag Users      # one resource group
python3 api2md.py spec.json --grep customer  # endpoints matching a term
python3 api2md.py spec.json --format json    # machine-readable digest
```

The Markdown is **complete and uncompressed** — every endpoint and every field
of every component schema, with component references cross-linked to their
definitions in §5. See `TOOL.md` for the full agent-facing guide.

The digest ends with a **"how to build a matching endpoint"** checklist derived
from the inferred conventions. Feed it to Claude Code like:

```bash
python3 api2md.py vendor-spec.json -o /tmp/api.md
# then: "Read /tmp/api.md and build a matching POST /widgets endpoint."
```

`apilearn.py` imports `api2md.py` as a library (shared `$ref` resolver and
convention inference), so keep the two files side by side.

---

## Notes

- Conventions (envelope, status codes, naming, common params) are **inferred by
  frequency**, not declared — treat them as strong hints and confirm against a
  sibling endpoint when in doubt.
- Both tools exit cleanly when piped into `head`/`less` (no `BrokenPipeError`).
