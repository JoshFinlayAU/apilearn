#!/usr/bin/env python3
"""
api2md.py - https://github.com/JoshFinlayAU/apilearn

Usage:
    python3 api2md.py spec.json                 # full digest to stdout
    python3 api2md.py spec.json -o API_GUIDE.md # write to a file
    python3 api2md.py spec.json --compact       # endpoint table only, terse
    python3 api2md.py spec.json --tag Users      # one tag / resource group
    python3 api2md.py spec.json --grep customer  # endpoints matching a term
    python3 api2md.py spec.json --format json    # machine-readable digest
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, OrderedDict
from typing import Any

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options", "trace")


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #
def load_spec(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        sys.exit(f"api2md: cannot read {path}: {exc}")

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        # Optional YAML support if PyYAML happens to be installed.
        try:
            import yaml  # type: ignore
        except ImportError:
            sys.exit(
                f"api2md: {path} is not valid JSON ({exc}). "
                "Install PyYAML to parse YAML specs, or convert to JSON."
            )
        return yaml.safe_load(text)


# --------------------------------------------------------------------------- #
# $ref resolution + schema flattening
# --------------------------------------------------------------------------- #
class Resolver:
    """Resolves local ($ref: '#/...') references and renders compact schemas."""

    def __init__(self, spec: dict[str, Any]):
        self.spec = spec

    def resolve(self, node: Any, _seen: tuple = ()) -> Any:
        """Follow a single $ref one hop (guards against ref cycles)."""
        if isinstance(node, dict) and "$ref" in node:
            ref = node["$ref"]
            if ref in _seen:
                return {"type": "object", "x-cycle": ref}
            target = self._lookup(ref)
            if target is None:
                return {"type": "object", "x-unresolved": ref}
            return self.resolve(target, _seen + (ref,))
        return node

    def ref_name(self, node: Any) -> str | None:
        if isinstance(node, dict) and "$ref" in node:
            return node["$ref"].rsplit("/", 1)[-1]
        return None

    def _lookup(self, ref: str) -> Any:
        if not ref.startswith("#/"):
            return None
        cur: Any = self.spec
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return None
        return cur

    def schema_summary(self, schema: Any, depth: int = 0, max_depth: int = 1000) -> str:
        """One-line-ish type description, recursing into objects/arrays."""
        name = self.ref_name(schema)
        schema = self.resolve(schema)
        if not isinstance(schema, dict):
            return "any"

        # Composition keywords.
        for key in ("allOf", "oneOf", "anyOf"):
            if key in schema:
                parts = [self.schema_summary(s, depth, max_depth) for s in schema[key]]
                joiner = " & " if key == "allOf" else " | "
                return joiner.join(dict.fromkeys(parts))  # dedupe, keep order

        if "enum" in schema:
            vals = ", ".join(json.dumps(v) for v in schema["enum"])
            return f"enum({vals})"

        t = schema.get("type")
        if isinstance(t, list):
            t = "|".join(t)

        if t == "array" or "items" in schema:
            inner_ref = self.ref_name(schema.get("items", {}))
            if inner_ref and depth >= 1:
                return f"{inner_ref}[]"
            return f"{self.schema_summary(schema.get('items', {}), depth + 1, max_depth)}[]"

        if t == "object" or "properties" in schema:
            if name and depth >= 1:
                return name  # don't re-expand a named component when nested
            props = schema.get("properties")
            if not props:
                if "additionalProperties" in schema and schema["additionalProperties"]:
                    return f"map<string,{self.schema_summary(schema['additionalProperties'], depth + 1, max_depth)}>"
                return "object"
            if depth >= max_depth:
                return name or "object{…}"
            required = set(schema.get("required", []))
            fields = []
            for pname, pschema in props.items():
                mark = "" if pname in required else "?"
                fields.append(f"{pname}{mark}: {self.schema_summary(pschema, depth + 1, max_depth)}")
            body = ", ".join(fields)
            return "{ " + body + " }"

        if t:
            fmt = schema.get("format")
            return f"{t}({fmt})" if fmt else t
        return name or "any"

    def field_table(self, schema: Any) -> list[tuple[str, str, str, str]]:
        """(name, type, required, description) rows for a top-level object."""
        schema = self.resolve(schema)
        if not isinstance(schema, dict):
            return []
        merged: dict[str, Any] = {}
        required: set[str] = set()
        for part in self._object_parts(schema):
            merged.update(part.get("properties", {}))
            required.update(part.get("required", []))
        rows = []
        for pname, pschema in merged.items():
            desc = ""
            rs = self.resolve(pschema)
            if isinstance(rs, dict):
                desc = clean(rs.get("description", ""))
            rows.append(
                (pname, self.schema_summary(pschema, depth=1), "yes" if pname in required else "no", desc)
            )
        return rows

    def _object_parts(self, schema: dict[str, Any]) -> list[dict[str, Any]]:
        """Flatten allOf chains into a list of objects carrying properties."""
        parts = []
        if "properties" in schema or schema.get("type") == "object":
            parts.append(schema)
        for sub in schema.get("allOf", []):
            parts.extend(self._object_parts(self.resolve(sub)))
        return parts


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def clean(text: Any) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", str(text)).strip()


def truncate(text: str, n: int) -> str:
    # Truncation disabled: api2md emits complete, uncompressed output.
    # The `n` argument is kept for call-site compatibility but ignored.
    return clean(text)


def spec_version(spec: dict[str, Any]) -> str:
    if "openapi" in spec:
        return f"OpenAPI {spec['openapi']}"
    if "swagger" in spec:
        return f"Swagger {spec['swagger']}"
    return "unknown"


def iter_operations(spec: dict[str, Any]):
    """Yield (method, path, operation, path_item) for every operation."""
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method in HTTP_METHODS:
            op = item.get(method)
            if isinstance(op, dict):
                yield method.upper(), path, op, item


def group_key(op: dict[str, Any], path: str) -> str:
    tags = op.get("tags")
    if tags:
        return tags[0]
    parts = [p for p in path.strip("/").split("/") if p and not p.startswith("{")]
    return parts[0] if parts else "(root)"


# --------------------------------------------------------------------------- #
# Convention inference — the part that actually teaches "how to build"
# --------------------------------------------------------------------------- #
def infer_conventions(spec: dict[str, Any], res: Resolver) -> dict[str, Any]:
    status_codes: Counter = Counter()
    success_envelope_keys: Counter = Counter()
    query_params: Counter = Counter()
    path_styles: Counter = Counter()
    body_content_types: Counter = Counter()
    resp_content_types: Counter = Counter()
    n_ops = 0

    for method, path, op, item in iter_operations(spec):
        n_ops += 1

        # Path naming style.
        segs = [s for s in path.strip("/").split("/") if s and not s.startswith("{")]
        for s in segs:
            if "_" in s:
                path_styles["snake_case"] += 1
            elif "-" in s:
                path_styles["kebab-case"] += 1
            elif s != s.lower():
                path_styles["camelOrPascal"] += 1
            else:
                path_styles["lowercase"] += 1
            if s.endswith("s"):
                path_styles["plural-noun"] += 1

        # Parameters (operation + path-level).
        for p in list(op.get("parameters", [])) + list(item.get("parameters", [])):
            p = res.resolve(p)
            if isinstance(p, dict) and p.get("in") == "query":
                query_params[p.get("name", "?")] += 1

        # Request body content types.
        for ct in (op.get("requestBody", {}) or {}).get("content", {}):
            body_content_types[ct] += 1

        # Responses: status codes, envelope keys, content types.
        for code, resp in (op.get("responses") or {}).items():
            status_codes[str(code)] += 1
            resp = res.resolve(resp)
            if not isinstance(resp, dict):
                continue
            content = resp.get("content", {})
            for ct, media in content.items():
                resp_content_types[ct] += 1
            if str(code).startswith("2"):
                schema = _first_schema(resp, res)
                rs = res.resolve(schema) if schema else None
                if isinstance(rs, dict) and rs.get("type") == "object" and rs.get("properties"):
                    for k in rs["properties"]:
                        success_envelope_keys[k] += 1

    return {
        "n_ops": n_ops,
        "status_codes": status_codes,
        "success_envelope_keys": success_envelope_keys,
        "query_params": query_params,
        "path_styles": path_styles,
        "body_content_types": body_content_types,
        "resp_content_types": resp_content_types,
    }


def _first_schema(resp: dict[str, Any], res: Resolver) -> Any:
    """Return the schema of a response, OpenAPI 3 or Swagger 2."""
    if "content" in resp:  # OpenAPI 3
        for media in resp["content"].values():
            if "schema" in media:
                return media["schema"]
    if "schema" in resp:  # Swagger 2
        return resp["schema"]
    return None


def security_schemes(spec: dict[str, Any]) -> dict[str, Any]:
    if "components" in spec:
        return spec["components"].get("securitySchemes", {}) or {}
    return spec.get("securityDefinitions", {}) or {}


def all_schemas(spec: dict[str, Any]) -> dict[str, Any]:
    if "components" in spec:
        return spec["components"].get("schemas", {}) or {}
    return spec.get("definitions", {}) or {}


# --------------------------------------------------------------------------- #
# Cross-linking — turn schema references into in-document Markdown links
# --------------------------------------------------------------------------- #
def schema_anchor(name: str) -> str:
    """Deterministic anchor id for a component schema's §5 entry."""
    return "schema-" + re.sub(r"[^0-9A-Za-z]+", "-", name).strip("-")


def ref_link(name: str, known: set) -> str:
    """`name` as a Markdown link to its §5 definition when it's a known schema."""
    if name in known:
        return f"[`{name}`](#{schema_anchor(name)})"
    return f"`{name}`"


def type_md(type_str: str, known: set) -> str:
    """
    Render a schema_summary() type string for a Markdown table cell, linking it
    to §5 when the whole type is a known component (optionally an array of one,
    e.g. `LabelValue` or `GroupRef[]`). Compound/scalar types stay code spans.
    """
    m = re.fullmatch(r"(\w+)(\[\])?", type_str)
    if m and m.group(1) in known:
        return f"[`{type_str}`](#{schema_anchor(m.group(1))})"
    return f"`{type_str}`"


def linkify_inline(summary: str, known: set) -> str:
    """
    Linkify whole-word component names inside a raw (non-backticked) inline type
    summary, e.g. `{ Days?: LabelValue }` → `{ Days?: [LabelValue](#…) }`.
    Only used where the summary is rendered as plain text, never inside a code span.
    """
    if not known:
        return summary
    pattern = r"\b(" + "|".join(re.escape(n) for n in sorted(known, key=len, reverse=True)) + r")\b"
    return re.sub(pattern, lambda m: f"[{m.group(1)}](#{schema_anchor(m.group(1))})", summary)


# --------------------------------------------------------------------------- #
# Markdown rendering
# --------------------------------------------------------------------------- #
def render_markdown(spec: dict[str, Any], args) -> str:
    res = Resolver(spec)
    info = spec.get("info", {})
    out: list[str] = []
    w = out.append

    title = clean(info.get("title", "API"))
    w(f"# API Specification — {title}")
    w("")
    w(f"> Generated by `api2md.py` from a {spec_version(spec)} spec. https://github.com/JoshFinlayAU/apilearn")
    w("")

    # ---- Overview -------------------------------------------------------- #
    w("## 1. Overview")
    w("")
    if info.get("version"):
        w(f"- **Version:** {clean(info['version'])}")
    if info.get("description"):
        w(f"- **Description:** {truncate(info['description'], 400)}")
    servers = spec.get("servers")
    if servers:
        for s in servers:
            w(f"- **Base URL:** `{s.get('url','')}` {clean(s.get('description',''))}".rstrip())
    elif spec.get("basePath"):
        host = spec.get("host", "")
        schemes = ",".join(spec.get("schemes", [])) or "https"
        w(f"- **Base URL:** `{schemes}://{host}{spec['basePath']}`")
    conv = infer_conventions(spec, res)
    known = set(all_schemas(spec).keys())
    w(f"- **Operations:** {conv['n_ops']} across {len(set(group_key(o, p) for _, p, o, _ in iter_operations(spec)))} resource groups")
    if known:
        w("- **Reusable schemas:** see [§5](#schemas) — referenced throughout §4 via links.")
    w("")

    # ---- Auth ------------------------------------------------------------ #
    w("## 2. Authentication")
    w("")
    schemes = security_schemes(spec)
    if not schemes:
        w("_No security schemes declared in the spec._")
    else:
        for name, scheme in schemes.items():
            scheme = res.resolve(scheme)
            stype = scheme.get("type", "?")
            detail = ""
            if stype in ("apiKey",):
                detail = f"`{scheme.get('name')}` in {scheme.get('in')}"
            elif stype in ("http",):
                detail = f"scheme=`{scheme.get('scheme')}`" + (
                    f", bearerFormat=`{scheme.get('bearerFormat')}`" if scheme.get("bearerFormat") else ""
                )
            elif stype == "oauth2":
                flows = ", ".join((scheme.get("flows") or {}).keys())
                detail = f"flows: {flows}"
            w(f"- **{name}** — `{stype}` {detail}".rstrip())
    glob = spec.get("security")
    if glob:
        applied = ", ".join(k for req in glob for k in req.keys())
        w(f"- **Applied globally:** {applied}")
    w("")

    # ---- Conventions ----------------------------------------------------- #
    w('<a id="conventions"></a>')
    w("## 3. Conventions (inferred)")
    w("")
    env = conv["success_envelope_keys"]
    if env:
        threshold = max(2, conv["n_ops"] * 0.3)
        common = [k for k, c in env.most_common() if c >= threshold]
        if common:
            w(f"- **Success response envelope:** most 2xx bodies wrap data in "
              f"`{{ {', '.join(common)} }}`.")
        else:
            w(f"- **Top success-body keys:** {', '.join(k for k, _ in env.most_common(6))}")
    sc = conv["status_codes"]
    if sc:
        w(f"- **Status codes in use:** {', '.join(f'{c} (×{n})' for c, n in sorted(sc.items()))}")
    ps = conv["path_styles"]
    if ps:
        dominant = ", ".join(f"{k}" for k, _ in ps.most_common(3))
        w(f"- **Path naming:** {dominant}")
    qp = conv["query_params"]
    if qp:
        w(f"- **Query params (all, by frequency):** {', '.join(f'`{k}`' for k, _ in qp.most_common())}")
    bct = conv["body_content_types"]
    if bct:
        w(f"- **Request content types:** {', '.join(f'`{k}`' for k, _ in bct.most_common())}")
    rct = conv["resp_content_types"]
    if rct:
        w(f"- **Response content types:** {', '.join(f'`{k}`' for k, _ in rct.most_common())}")
    w("")

    # ---- Endpoints ------------------------------------------------------- #
    w("## 4. Endpoints")
    w("")

    groups: "OrderedDict[str, list]" = OrderedDict()
    for method, path, op, item in iter_operations(spec):
        key = group_key(op, path)
        groups.setdefault(key, []).append((method, path, op, item))

    if args.tag:
        wanted = args.tag.lower()
        groups = OrderedDict((k, v) for k, v in groups.items() if k.lower() == wanted)
        if not groups:
            w(f"_No endpoints found for tag/group `{args.tag}`._")

    grep = args.grep.lower() if args.grep else None
    rendered = 0
    for key in sorted(groups):
        ops = groups[key]
        if grep:
            ops = [o for o in ops if grep in o[1].lower() or grep in clean(o[2].get("summary", "")).lower()]
        if not ops:
            continue
        w(f"### {key}")
        w("")
        for method, path, op, item in sorted(ops, key=lambda x: (x[1], x[0])):
            render_operation(w, res, method, path, op, item, compact=args.compact, known=known)
            rendered += 1
        w("")

    if args.compact:
        return "\n".join(out)

    # ---- Component schemas ---------------------------------------------- #
    schemas = all_schemas(spec)
    if schemas:
        w('<a id="schemas"></a>')
        w("## 5. Reusable schemas (components)")
        w("")
        w("Reference these by name; build new request/response bodies to match. "
          "Field types that are themselves components link to their definition below.")
        w("")
        w("**Index:** " + " · ".join(ref_link(name, known) for name in schemas))
        w("")
        for name, schema in schemas.items():
            desc = clean(res.resolve(schema).get("description", "")) if isinstance(res.resolve(schema), dict) else ""
            w(f'<a id="{schema_anchor(name)}"></a>')
            w(f"#### `{name}`" + (f" — {desc}" if desc else ""))
            rows = res.field_table(schema)
            if not rows:
                w(type_md(res.schema_summary(schema), known))
                w("")
                continue
            w("| field | type | required | description |")
            w("| --- | --- | --- | --- |")
            for fname, ftype, req, fdesc in rows:
                w(f"| `{fname}` | {type_md(ftype, known)} | {req} | {fdesc} |")
            w("")

    # ---- Build checklist ------------------------------------------------- #
    w("## 6. How to build a matching endpoint")
    w("")
    w(_build_checklist(conv, schemes))

    return "\n".join(out)


def render_operation(w, res: Resolver, method, path, op, item, compact: bool, known: set | None = None):
    known = known or set()
    summary = clean(op.get("summary") or op.get("description") or "")
    if compact:
        w(f"- `{method} {path}` — {truncate(summary, 90)}")
        return

    w(f"- **`{method} {path}`** — {truncate(summary, 200)}".rstrip())
    if op.get("operationId"):
        w(f"  - operationId: `{op['operationId']}`")

    # Parameters.
    params = list(op.get("parameters", [])) + list(item.get("parameters", []))
    by_loc: dict[str, list[str]] = {}
    for p in params:
        p = res.resolve(p)
        if not isinstance(p, dict):
            continue
        loc = p.get("in", "?")
        schema = p.get("schema", p)  # OpenAPI 3 nests under schema; Swagger 2 inlines
        pref = res.ref_name(schema)
        typ = ref_link(pref, known) if pref else type_md(res.schema_summary(schema), known)
        req = "*" if p.get("required") else ""
        by_loc.setdefault(loc, []).append(f"`{p.get('name')}{req}`:{typ}")
    for loc in ("path", "query", "header"):
        if by_loc.get(loc):
            w(f"  - {loc} params: {', '.join(by_loc[loc])}")

    # Request body.
    rb = op.get("requestBody")
    if rb:
        rb = res.resolve(rb)
        body_schema = None
        for media in (rb.get("content") or {}).values():
            if "schema" in media:
                body_schema = media["schema"]
                break
        if body_schema is not None:
            name = res.ref_name(body_schema)
            w(f"  - body: {ref_link(name, known) if name else linkify_inline(res.schema_summary(body_schema), known)}")
    else:
        # Swagger 2: body param.
        for p in params:
            p = res.resolve(p)
            if isinstance(p, dict) and p.get("in") == "body":
                name = res.ref_name(p.get("schema", {}))
                w(f"  - body: {ref_link(name, known) if name else linkify_inline(res.schema_summary(p.get('schema', {})), known)}")

    # Responses (success + first error).
    resp_lines = []
    for code, resp in sorted((op.get("responses") or {}).items(), key=lambda x: str(x[0])):
        rresp = res.resolve(resp)
        schema = _first_schema(rresp, res) if isinstance(rresp, dict) else None
        name = res.ref_name(schema) if schema is not None else None
        if name:
            shape = ref_link(name, known)
        elif schema is not None:
            shape = res.schema_summary(schema)
        else:
            shape = "—"
        resp_lines.append(f"{code}→{shape}")
    if resp_lines:
        w(f"  - responses: {', '.join(resp_lines)}")


def _build_checklist(conv: dict[str, Any], schemes: dict[str, Any]) -> str:
    env = conv["success_envelope_keys"]
    threshold = max(2, conv["n_ops"] * 0.3)
    common = [k for k, c in env.most_common() if c >= threshold]
    auth = next(iter(schemes), None)
    lines = [
        "Replicate these patterns so a new endpoint is consistent with the rest of the API:",
        "",
        f"1. **Path & method** — match the observed naming style "
        f"({', '.join(k for k, _ in conv['path_styles'].most_common(2)) or 'see §3'}); "
        "use the standard verb for the action (GET read, POST create, PUT/PATCH update, DELETE remove).",
    ]
    if auth:
        lines.append(f"2. **Auth** — require the `{auth}` security scheme like every other operation.")
    else:
        lines.append("2. **Auth** — apply the same auth as neighbouring endpoints.")
    if common:
        lines.append(
            f"3. **Response envelope** — wrap the payload as `{{ {', '.join(common)} }}` to match existing 2xx bodies."
        )
    else:
        lines.append("3. **Response envelope** — mirror the response shape of a sibling endpoint in the same group.")
    common_codes = [c for c, _ in conv["status_codes"].most_common(6)]
    lines.append(f"4. **Status codes** — reuse the codes already in play: {', '.join(sorted(common_codes))}.")
    lines.append("5. **Schemas** — reference existing component schemas ([§5](#schemas)) for inputs/outputs instead of inventing new shapes.")
    lines.append("6. **List endpoints** — if returning a collection, include the same pagination params the API already uses ([§3](#conventions)).")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# JSON rendering (machine-readable digest)
# --------------------------------------------------------------------------- #
def render_json(spec: dict[str, Any], args) -> str:
    res = Resolver(spec)
    conv = infer_conventions(spec, res)
    ops = []
    for method, path, op, item in iter_operations(spec):
        ops.append(
            {
                "method": method,
                "path": path,
                "group": group_key(op, path),
                "summary": clean(op.get("summary", "")),
                "operationId": op.get("operationId"),
            }
        )
    digest = {
        "title": clean(spec.get("info", {}).get("title", "")),
        "version": clean(spec.get("info", {}).get("version", "")),
        "spec": spec_version(spec),
        "auth": list(security_schemes(spec).keys()),
        "conventions": {
            "status_codes": dict(conv["status_codes"]),
            "success_envelope_keys": dict(conv["success_envelope_keys"].most_common(10)),
            "common_query_params": dict(conv["query_params"].most_common(15)),
            "path_styles": dict(conv["path_styles"]),
        },
        "operation_count": conv["n_ops"],
        "operations": ops,
        "schemas": list(all_schemas(spec).keys()),
    }
    return json.dumps(digest, indent=2)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="api2md.py",
        description="Distil a Swagger/OpenAPI JSON spec into an LLM-friendly API build guide.",
    )
    parser.add_argument("spec", help="path to the Swagger 2.0 / OpenAPI 3.x JSON (or YAML) file")
    parser.add_argument("-o", "--output", help="write to this file instead of stdout")
    parser.add_argument("--format", choices=("md", "json"), default="md", help="output format (default: md)")
    parser.add_argument("--compact", action="store_true", help="endpoint list only, one line each")
    parser.add_argument("--tag", help="only the given tag / resource group")
    parser.add_argument("--grep", help="only endpoints whose path or summary contains this term")
    args = parser.parse_args(argv)

    spec = load_spec(args.spec)
    if not isinstance(spec, dict) or not (spec.get("paths") or spec.get("swagger") or spec.get("openapi")):
        sys.exit("api2md: this does not look like a Swagger/OpenAPI document.")

    rendered = render_json(spec, args) if args.format == "json" else render_markdown(spec, args)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(rendered + "\n")
        size = len(rendered)
        print(f"api2md: wrote {args.output} ({size:,} chars)", file=sys.stderr)
    else:
        try:
            print(rendered)
        except BrokenPipeError:
            # Downstream pager/`head` closed the pipe — exit quietly.
            try:
                sys.stdout.close()
            except BrokenPipeError:
                pass
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
