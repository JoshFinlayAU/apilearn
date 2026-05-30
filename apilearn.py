#!/usr/bin/env python3
"""
apilearn.py — query a Swagger 2.0 / OpenAPI 3.x JSON spec one call at a time.

Designed to be driven by an LLM (Claude Code) so it can learn an API without
loading the whole spec into context. The workflow is:

    1. `apilearn.py spec.json info`            → overview: auth, conventions, groups
    2. `apilearn.py spec.json list`            → every call, one terse line each
    3. `apilearn.py spec.json show <selector>` → full detail for ONE call at a time

The agent lists the calls, then iterates — pulling the specifics of each call in
a small, self-contained chunk instead of being overwhelmed by a 900 KB document.

Companion tool: `api2md.py` renders the *entire* spec as one Markdown digest.
This tool shares api2md's $ref resolver and convention inference.

Selectors (for `show`) match, in order of preference:
    - an exact operationId            (e.g. ListTenants)
    - an exact "METHOD path"          (e.g. "GET /api/ListTenants")
    - an exact path                   (e.g. /api/ListTenants)
    - failing all of the above, a case-insensitive substring of path/operationId

Examples:
    python3 apilearn.py spec.json list
    python3 apilearn.py spec.json list --grep tenant --method GET
    python3 apilearn.py spec.json list --group "CIPP > Core"
    python3 apilearn.py spec.json show ListTenants
    python3 apilearn.py spec.json show "POST /api/ExecAddAlert" --format json
    python3 apilearn.py spec.json groups
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Reuse the resolver / inference engine from the companion tool.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import api2md  # noqa: E402


# --------------------------------------------------------------------------- #
# Selection helpers
# --------------------------------------------------------------------------- #
def operation_records(spec):
    """Normalised list of every operation with the fields we filter/show on."""
    records = []
    for method, path, op, item in api2md.iter_operations(spec):
        records.append(
            {
                "method": method,
                "path": path,
                "op": op,
                "item": item,
                "operationId": op.get("operationId") or "",
                "group": api2md.group_key(op, path),
                "summary": api2md.clean(op.get("summary") or op.get("description") or ""),
                "selector": op.get("operationId") or f"{method} {path}",
            }
        )
    return records


def match_records(records, selector):
    """Return records matching one selector, preferring exact over substring."""
    s = selector.lower().strip()
    exact, substr = [], []
    for r in records:
        oid = r["operationId"].lower()
        mp = f"{r['method']} {r['path']}".lower()
        if s in (oid, mp, r["path"].lower()):
            exact.append(r)
        elif s in r["path"].lower() or (oid and s in oid) or s in mp:
            substr.append(r)
    return exact if exact else substr


# --------------------------------------------------------------------------- #
# Auth + schema rendering
# --------------------------------------------------------------------------- #
def operation_auth(spec, op):
    sec = op.get("security")
    if sec is None:
        sec = spec.get("security", [])
    names = [k for req in sec for k in req.keys()] if sec else []
    return names or ["(none / inherits default)"]


def schema_block(res: "api2md.Resolver", schema) -> list[str]:
    """Render a request/response schema as Markdown lines (table or inline)."""
    if schema is None:
        return ["_(no body)_"]
    name = res.ref_name(schema)
    resolved = res.resolve(schema)
    if not isinstance(resolved, dict):
        return ["`any`"]

    # Arrays: describe the element shape.
    if resolved.get("type") == "array" or "items" in resolved:
        items = resolved.get("items", {})
        iname = res.ref_name(items)
        lines = [f"**Array** of `{iname or res.schema_summary(items)}`:"]
        ritems = res.resolve(items)
        if isinstance(ritems, dict) and (ritems.get("properties") or ritems.get("type") == "object"):
            lines += field_table_lines(res, items)
        return lines

    rows = res.field_table(schema)
    if rows:
        prefix = [f"_Schema: `{name}`_", ""] if name else []
        return prefix + field_table_lines(res, schema)

    # No object fields — inline summary (enum, scalar, map, etc.).
    return [f"`{res.schema_summary(schema)}`"]


def field_table_lines(res: "api2md.Resolver", schema) -> list[str]:
    rows = res.field_table(schema)
    if not rows:
        return [f"`{res.schema_summary(schema)}`"]
    lines = ["| field | type | required | description |", "| --- | --- | --- | --- |"]
    for fname, ftype, req, fdesc in rows:
        lines.append(
            f"| `{fname}` | `{api2md.truncate(ftype, 90)}` | {req} | {api2md.truncate(fdesc, 100)} |"
        )
    return lines


def request_body_schema(res, op):
    """Return (content_type, schema) for an operation's request body, or (None, None)."""
    rb = op.get("requestBody")
    if rb:
        rb = res.resolve(rb)
        for ct, media in (rb.get("content") or {}).items():
            if "schema" in media:
                return ct, media["schema"]
        return None, None
    # Swagger 2: a body parameter.
    for p in op.get("parameters", []):
        p = res.resolve(p)
        if isinstance(p, dict) and p.get("in") == "body":
            return "application/json", p.get("schema")
    return None, None


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_info(spec, args) -> str:
    res = api2md.Resolver(spec)
    info = spec.get("info", {})
    conv = api2md.infer_conventions(spec, res)
    groups = sorted({r["group"] for r in operation_records(spec)})

    if args.format == "json":
        return json.dumps(
            {
                "title": api2md.clean(info.get("title", "")),
                "version": api2md.clean(info.get("version", "")),
                "spec": api2md.spec_version(spec),
                "operation_count": conv["n_ops"],
                "auth": list(api2md.security_schemes(spec).keys()),
                "status_codes": dict(conv["status_codes"]),
                "success_envelope_keys": dict(conv["success_envelope_keys"].most_common(8)),
                "common_query_params": dict(conv["query_params"].most_common(15)),
                "groups": groups,
            },
            indent=2,
        )

    out = [f"# {api2md.clean(info.get('title','API'))} — overview", ""]
    out.append(f"- spec: {api2md.spec_version(spec)}")
    if info.get("version"):
        out.append(f"- version: {api2md.clean(info['version'])}")
    out.append(f"- operations: {conv['n_ops']} across {len(groups)} groups")
    schemes = api2md.security_schemes(spec)
    out.append(f"- auth schemes: {', '.join(schemes.keys()) or '(none declared)'}")
    env = conv["success_envelope_keys"]
    if env:
        threshold = max(2, conv["n_ops"] * 0.3)
        common = [k for k, c in env.most_common() if c >= threshold]
        if common:
            out.append(f"- success envelope: `{{ {', '.join(common)} }}`")
    if conv["status_codes"]:
        out.append(f"- status codes: {', '.join(sorted(conv['status_codes']))}")
    if conv["query_params"]:
        out.append(
            f"- common query params: {', '.join(k for k, _ in conv['query_params'].most_common(10))}"
        )
    out += ["", "## Groups (use with `list --group`)", ""]
    counts = {}
    for r in operation_records(spec):
        counts[r["group"]] = counts.get(r["group"], 0) + 1
    for g in groups:
        out.append(f"- {g} ({counts[g]})")
    out += [
        "",
        "Next: `list` to see calls, then `show <operationId>` for one call's detail.",
    ]
    return "\n".join(out)


def cmd_groups(spec, args) -> str:
    counts = {}
    for r in operation_records(spec):
        counts[r["group"]] = counts.get(r["group"], 0) + 1
    groups = sorted(counts)
    if args.format == "json":
        return json.dumps([{"group": g, "count": counts[g]} for g in groups], indent=2)
    return "\n".join(f"{counts[g]:>4}  {g}" for g in groups)


def cmd_list(spec, args) -> str:
    records = operation_records(spec)

    if args.method:
        methods = {m.strip().upper() for m in args.method.split(",")}
        records = [r for r in records if r["method"] in methods]
    if args.group:
        records = [r for r in records if r["group"].lower() == args.group.lower()]
    if args.grep:
        g = args.grep.lower()
        records = [
            r
            for r in records
            if g in r["path"].lower()
            or g in r["operationId"].lower()
            or g in r["summary"].lower()
        ]

    records.sort(key=lambda r: (r["group"], r["path"], r["method"]))

    if args.format == "json":
        return json.dumps(
            [
                {
                    "selector": r["selector"],
                    "method": r["method"],
                    "path": r["path"],
                    "operationId": r["operationId"],
                    "group": r["group"],
                    "summary": r["summary"],
                }
                for r in records
            ],
            indent=2,
        )

    if not records:
        return "(no calls match)"

    lines = []
    current = None
    for r in records:
        if not args.flat and r["group"] != current:
            current = r["group"]
            lines.append(f"\n## {current}")
        sel = r["operationId"] or f'"{r["method"]} {r["path"]}"'
        summary = api2md.truncate(r["summary"], 80)
        lines.append(f"  {r['method']:6} {r['path']}  [{sel}]" + (f" — {summary}" if summary else ""))
    lines.append(f"\n{len(records)} call(s). Use `show <selector>` for full detail.")
    return "\n".join(lines).lstrip("\n")


def cmd_show(spec, args) -> str:
    res = api2md.Resolver(spec)
    records = operation_records(spec)

    matched = []
    seen = set()
    for selector in args.selector:
        for r in match_records(records, selector):
            key = (r["method"], r["path"])
            if key not in seen:
                seen.add(key)
                matched.append(r)

    if not matched:
        return f"No call matches {args.selector}. Try `list` to see valid selectors."

    if args.format == "json":
        return json.dumps([show_json(spec, res, r) for r in matched], indent=2)

    blocks = [show_markdown(spec, res, r) for r in matched]
    note = ""
    if len(matched) > 1:
        note = f"_(matched {len(matched)} calls — narrow the selector for just one)_\n\n"
    return note + "\n\n---\n\n".join(blocks)


def show_markdown(spec, res, r) -> str:
    op = r["op"]
    out = [f"## `{r['method']} {r['path']}`", ""]
    if r["operationId"]:
        out.append(f"- **operationId:** `{r['operationId']}`")
    out.append(f"- **group:** {r['group']}")
    if op.get("tags"):
        out.append(f"- **tags:** {', '.join(op['tags'])}")
    out.append(f"- **auth:** {', '.join(operation_auth(spec, op))}")
    if op.get("summary"):
        out.append(f"- **summary:** {api2md.clean(op['summary'])}")
    desc = api2md.clean(op.get("description", ""))
    if desc and desc != api2md.clean(op.get("summary", "")):
        out.append(f"- **description:** {api2md.truncate(desc, 600)}")
    if op.get("deprecated"):
        out.append("- **deprecated:** yes")

    # Parameters.
    params = list(op.get("parameters", [])) + list(r["item"].get("parameters", []))
    rows = []
    for p in params:
        p = res.resolve(p)
        if not isinstance(p, dict) or p.get("in") == "body":
            continue
        pschema = p.get("schema", p)
        rp = res.resolve(pschema)
        typ = res.schema_summary(pschema)
        extra = []
        if isinstance(rp, dict):
            if rp.get("default") is not None:
                extra.append(f"default={json.dumps(rp['default'])}")
            if rp.get("enum"):
                vals = ", ".join(json.dumps(v) for v in rp["enum"][:8])
                extra.append(f"enum: {vals}")
        pdesc = api2md.clean(p.get("description", ""))
        if extra:
            pdesc = (pdesc + " " if pdesc else "") + "(" + "; ".join(extra) + ")"
        rows.append((p.get("name", "?"), p.get("in", "?"), typ, "yes" if p.get("required") else "no", pdesc))
    if rows:
        out += ["", "### Parameters", "", "| name | in | type | required | description |", "| --- | --- | --- | --- | --- |"]
        for name, loc, typ, req, pdesc in rows:
            out.append(f"| `{name}` | {loc} | `{api2md.truncate(typ, 70)}` | {req} | {api2md.truncate(pdesc, 90)} |")

    # Request body.
    ct, body_schema = request_body_schema(res, op)
    if body_schema is not None:
        out += ["", f"### Request body ({ct or 'application/json'})", ""]
        out += schema_block(res, body_schema)

    # Responses.
    responses = op.get("responses") or {}
    if responses:
        out += ["", "### Responses", ""]
        for code, resp in sorted(responses.items(), key=lambda x: str(x[0])):
            rresp = res.resolve(resp)
            rdesc = api2md.clean(rresp.get("description", "")) if isinstance(rresp, dict) else ""
            out.append(f"#### {code}" + (f" — {api2md.truncate(rdesc, 120)}" if rdesc else ""))
            schema = api2md._first_schema(rresp, res) if isinstance(rresp, dict) else None
            out += schema_block(res, schema) if schema is not None else ["_(no body)_"]
            out.append("")

    return "\n".join(out).rstrip()


def show_json(spec, res, r) -> dict:
    op = r["op"]
    params = []
    for p in list(op.get("parameters", [])) + list(r["item"].get("parameters", [])):
        p = res.resolve(p)
        if not isinstance(p, dict) or p.get("in") == "body":
            continue
        pschema = p.get("schema", p)
        rp = res.resolve(pschema)
        params.append(
            {
                "name": p.get("name"),
                "in": p.get("in"),
                "type": res.schema_summary(pschema),
                "required": bool(p.get("required")),
                "description": api2md.clean(p.get("description", "")),
                "enum": rp.get("enum") if isinstance(rp, dict) else None,
                "default": rp.get("default") if isinstance(rp, dict) else None,
            }
        )
    ct, body_schema = request_body_schema(res, op)
    responses = {}
    for code, resp in (op.get("responses") or {}).items():
        rresp = res.resolve(resp)
        schema = api2md._first_schema(rresp, res) if isinstance(rresp, dict) else None
        responses[str(code)] = {
            "description": api2md.clean(rresp.get("description", "")) if isinstance(rresp, dict) else "",
            "schema": res.schema_summary(schema) if schema is not None else None,
            "schemaRef": res.ref_name(schema),
        }
    return {
        "method": r["method"],
        "path": r["path"],
        "operationId": r["operationId"],
        "group": r["group"],
        "tags": op.get("tags", []),
        "auth": operation_auth(spec, op),
        "summary": api2md.clean(op.get("summary", "")),
        "description": api2md.clean(op.get("description", "")),
        "deprecated": bool(op.get("deprecated")),
        "parameters": params,
        "requestBody": {
            "contentType": ct,
            "schema": res.schema_summary(body_schema) if body_schema is not None else None,
            "schemaRef": res.ref_name(body_schema),
        }
        if body_schema is not None
        else None,
        "responses": responses,
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="apilearn.py",
        description="Query a Swagger/OpenAPI spec one call at a time (list / show / info).",
    )
    parser.add_argument("spec", help="path to the Swagger 2.0 / OpenAPI 3.x JSON (or YAML) file")
    sub = parser.add_subparsers(dest="command")

    p_info = sub.add_parser("info", help="spec overview: auth, conventions, groups")
    p_info.add_argument("--format", choices=("text", "json"), default="text")

    p_groups = sub.add_parser("groups", help="list resource groups/tags with call counts")
    p_groups.add_argument("--format", choices=("text", "json"), default="text")

    p_list = sub.add_parser("list", help="list calls, one terse line each")
    p_list.add_argument("--grep", help="filter by substring of path/operationId/summary")
    p_list.add_argument("--group", help="only this resource group/tag")
    p_list.add_argument("--method", help="filter by HTTP method(s), comma-separated")
    p_list.add_argument("--flat", action="store_true", help="no group headers, just a flat list")
    p_list.add_argument("--format", choices=("text", "json"), default="text")

    p_show = sub.add_parser("show", help="full detail for one or more calls by selector")
    p_show.add_argument("selector", nargs="+", help="operationId, 'METHOD path', path, or substring")
    p_show.add_argument("--format", choices=("text", "json"), default="text")

    args = parser.parse_args(argv)
    if args.command is None:
        args.command = "list"
        args.grep = args.group = args.method = None
        args.flat = False
        args.format = "text"

    spec = api2md.load_spec(args.spec)
    if not isinstance(spec, dict) or not (spec.get("paths") or spec.get("swagger") or spec.get("openapi")):
        sys.exit("apilearn: this does not look like a Swagger/OpenAPI document.")

    handlers = {"info": cmd_info, "groups": cmd_groups, "list": cmd_list, "show": cmd_show}
    rendered = handlers[args.command](spec, args)

    try:
        print(rendered)
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except BrokenPipeError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
