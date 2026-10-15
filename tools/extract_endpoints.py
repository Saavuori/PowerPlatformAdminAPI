#!/usr/bin/env python3
"""Extract REST operations from the Microsoft.PowerApps.Administration.PowerShell module.

Static parse only: the module is read as text and never imported or executed.

Usage (run from outside the extracted module folder, with -I):
    python -I tools/extract_endpoints.py <module_dir> -o out/endpoints.json

<module_dir> must contain Microsoft.PowerApps.Administration.PowerShell.psm1 and .psd1.
Re-run against a newer module version and diff the two endpoints.json files to see
which routes, verbs, api-versions or body fields changed.
"""
import argparse
import json
import re
import sys
from pathlib import Path

CMDLET_FILE = "Microsoft.PowerApps.Administration.PowerShell.psm1"
MANIFEST_FILE = "Microsoft.PowerApps.Administration.PowerShell.psd1"
SIG_MARKER = "# SIG # Begin signature block"

# Placeholders InvokeApi / InvokeApiNoParseContent substitute (RestClientModule.psm1).
HOST_MACROS = ["{flowEndpoint}", "{powerAppsEndpoint}", "{bapEndpoint}", "{graphEndpoint}",
               "{msGraphEndpoint}", "{pvaEndpoint}", "{cdsOneEndpoint}", "{powerPlatformEndpoint}"]
INVOKE_API_DEFAULT_API_VERSION = "2016-11-01"  # InvokeApi -ApiVersion default
CALL_RE = re.compile(r"\b(InvokeApiNoParseContent|InvokeApi|Invoke-Request|Invoke-RestMethod|Invoke-WebRequest)\b")
ASSIGN_RE = re.compile(r"^\s*\$(?:script:|global:)?([A-Za-z_]\w*)\s*(\+?=)\s*(.*)$", re.S)
MACRO_RE = re.compile(r"ReplaceMacro\s+-Macro\s+(\"[^\"]*\"|'[^']*')\s+-Value\s+(.+?)(?=\s*\||\s*;?\s*$|\s*\)\s*(?:-|$))", re.S)
DOTREPLACE_RE = re.compile(r"\.Replace\(\s*(\"[^\"]*\"|'[^']*')\s*,\s*([^)]+)\)")


# ---------------------------------------------------------------- text helpers

def read_source(path):
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    cut = text.find(SIG_MARKER)
    if cut >= 0:
        text = text[:cut]
    return text.split("\n")


def scan_balanced(s, i, open_ch, close_ch):
    """Return index just past the bracket matching s[i] (== open_ch), honouring quotes."""
    depth = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == "`":
            i += 2
            continue
        if c in "\"'":
            i = skip_string(s, i)
            continue
        if c == "#" and (i == 0 or s[i - 1] in " \t\n") and open_ch != "(":
            j = s.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def skip_string(s, i):
    q = s[i]
    i += 1
    n = len(s)
    while i < n:
        c = s[i]
        if q == '"' and c == "`":
            i += 2
            continue
        if c == q:
            if i + 1 < n and s[i + 1] == q:  # doubled quote escape
                i += 2
                continue
            return i + 1
        i += 1
    return n


def split_top_level(s, sep=","):
    parts, depth, cur, i = [], 0, [], 0
    while i < len(s):
        c = s[i]
        if c == "`":
            cur.append(s[i:i + 2]); i += 2; continue
        if c in "\"'":
            j = skip_string(s, i); cur.append(s[i:j]); i = j; continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        if c == sep and depth == 0:
            parts.append("".join(cur)); cur = []
        else:
            cur.append(c)
        i += 1
    if "".join(cur).strip():
        parts.append("".join(cur))
    return parts


def ps_string_to_template(lit):
    """Convert a PowerShell string literal to a template; variables become {$name}."""
    if lit.startswith("'"):
        return lit[1:-1].replace("''", "'")
    body, out, i = lit[1:-1], [], 0
    while i < len(body):
        c = body[i]
        if c == "`" and i + 1 < len(body):
            out.append(body[i + 1]); i += 2; continue
        if c == "$" and i + 1 < len(body):
            if body[i + 1] == "(":
                j = scan_balanced(body, i + 1, "(", ")")
                out.append("{$" + body[i + 2:j - 1].strip().lstrip("$") + "}"); i = j; continue
            m = re.match(r"\$(?:global:|script:)?([A-Za-z_][\w]*(?:\.[A-Za-z_]\w*)*)", body[i:])
            if m:
                out.append("{$" + m.group(1) + "}"); i += m.end(); continue
        out.append(c); i += 1
    return "".join(out)


def expr_to_template(expr):
    """Concatenate string literals / variables joined by '+' into one template."""
    pieces = split_top_level(expr.split("|")[0], "+")
    out = []
    for p in pieces:
        p = p.strip().rstrip(";").strip()
        if not p:
            continue
        if p[0] in "\"'":
            end = skip_string(p, 0)
            out.append(ps_string_to_template(p[:end]))
        elif p.startswith("$"):
            out.append("{$" + p.lstrip("$") + "}")
        else:
            return None
    return "".join(out) if out else None


def clean_value(v):
    v = v.strip().rstrip("`;").strip()
    if v.endswith(")") and v.count("(") < v.count(")"):
        v = v[:-1].strip()
    return v


# --------------------------------------------------------- logical statements

def code_lines(lines):
    """Blank out <# ... #> blocks and strip trailing # comments, keeping line numbers."""
    out, in_block = [], False
    for line in lines:
        if in_block:
            if "#>" in line:
                in_block = False
                line = line[line.index("#>") + 2:]
            else:
                out.append("")
                continue
        if "<#" in line and "#>" not in line[line.index("<#"):]:
            in_block = True
            line = line[:line.index("<#")]
        out.append(strip_comment(line))
    return out


def strip_comment(line):
    i = 0
    while i < len(line):
        c = line[i]
        if c == "`":
            i += 2; continue
        if c in "\"'":
            i = skip_string(line, i); continue
        if c == "#" and (i == 0 or line[i - 1] in " \t;"):
            return line[:i].rstrip()
        i += 1
    return line


def logical_statements(lines, start, end):
    """Join backtick / pipe / open-paren continuations. Yields (first_line_no, text).

    `lines` must come from code_lines() so comments cannot swallow joined text."""
    i = start
    while i <= end:
        first = i
        buf = lines[i - 1]
        depth = paren_depth(buf)
        while i < end:
            stripped = buf.rstrip()
            nxt = lines[i].lstrip()
            if stripped.endswith("`"):
                buf = stripped[:-1] + " " + lines[i].strip()
            elif stripped.endswith("|") or nxt.startswith("|") or depth > 0:
                buf = stripped + " " + lines[i].strip()
            else:
                break
            depth = paren_depth(buf)
            i += 1
            if i - first > 40:
                break
        yield first, buf
        i += 1


def paren_depth(s):
    depth, i = 0, 0
    while i < len(s):
        c = s[i]
        if c == "`":
            i += 2; continue
        if c in "\"'":
            i = skip_string(s, i); continue
        if c == "#" and (i == 0 or s[i - 1] in " \t"):
            break
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        i += 1
    return depth


# ------------------------------------------------------------ PS literal parse

def parse_literal(s, i=0):
    """Parse a PowerShell hashtable/array/scalar starting at s[i]. Returns (node, end)."""
    i = skip_ws(s, i)
    m = re.match(r"\[(pscustomobject|ordered|hashtable|array|object\[\])\]\s*", s[i:], re.I)
    if m:
        i += m.end()
    if s.startswith("@{", i):
        return parse_hash(s, i + 2)
    if s.startswith("@(", i):
        return parse_array(s, i + 2)
    if i < len(s) and s[i] in "\"'":
        j = skip_string(s, i)
        return {"type": "string", "value": ps_string_to_template(s[i:j])}, j
    j = scan_expr(s, i)
    expr = s[i:j].strip()
    low = expr.lower()
    if low in ("$true", "$false"):
        node = {"type": "boolean", "value": low == "$true"}
    elif low == "$null":
        node = {"type": "null"}
    elif re.fullmatch(r"-?\d+(\.\d+)?", expr):
        node = {"type": "number", "value": expr}
    else:
        node = {"type": "expr", "value": expr}
    return node, j


def skip_ws(s, i):
    while i < len(s):
        if s[i] in " \t\r\n;`":
            i += 1
        elif s[i] == "#":
            j = s.find("\n", i)
            i = len(s) if j < 0 else j
        else:
            break
    return i


def scan_expr(s, i):
    depth = 0
    while i < len(s):
        c = s[i]
        if c == "`":
            i += 2; continue
        if c in "\"'":
            i = skip_string(s, i); continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                return i
            depth -= 1
        elif depth == 0 and c in "\n;,":
            return i
        i += 1
    return i


def parse_hash(s, i):
    fields = {}
    while i < len(s):
        i = skip_ws(s, i)
        if i >= len(s) or s[i] == "}":
            return {"type": "object", "fields": fields}, i + 1
        if s[i] in "\"'":
            j = skip_string(s, i)
            key = ps_string_to_template(s[i:j])
        else:
            m = re.match(r"[\w.$-]+", s[i:])
            if not m:  # not a key: skip to the next entry
                i = scan_expr(s, i) + 1
                continue
            key, j = m.group(0), i + m.end()
        j = skip_ws(s, j)
        if j < len(s) and s[j] == "=":
            node, i = parse_literal(s, j + 1)
            fields[key] = node
        else:
            i = j + 1
    return {"type": "object", "fields": fields}, i


def parse_array(s, i):
    items = []
    while i < len(s):
        i = skip_ws(s, i)
        if i < len(s) and s[i] == ",":
            i += 1; continue
        if i >= len(s) or s[i] == ")":
            return {"type": "array", "items": items}, i + 1
        node, j = parse_literal(s, i)
        if j <= i:
            j = i + 1
        items.append(node)
        i = j
    return {"type": "array", "items": items}, i


# ------------------------------------------------------------------ functions

def split_functions(lines):
    starts = [(i + 1, m.group(1)) for i, l in enumerate(lines)
              if (m := re.match(r"^function\s+([A-Za-z0-9_-]+)", l))]
    funcs = []
    for k, (ln, name) in enumerate(starts):
        end = starts[k + 1][0] - 1 if k + 1 < len(starts) else len(lines)
        funcs.append({"name": name, "start": ln, "end": end})
    return funcs


def parse_help(text):
    m = re.search(r"<#(.*?)#>", text, re.S)
    if not m:
        return {}
    out, params, key, buf = {}, {}, None, []

    def flush():
        if key is None:
            return
        val = " ".join(x.strip() for x in buf if x.strip())
        if key[0] == "PARAMETER":
            params[key[1]] = val
        elif key[0] in ("SYNOPSIS", "DESCRIPTION"):
            out[key[0].lower()] = val
        elif key[0] == "EXAMPLE":
            out.setdefault("examples", []).append("\n".join(x.strip() for x in buf if x.strip()))

    for line in m.group(1).split("\n"):
        h = re.match(r"\s*\.(\w+)\s*(\S*)", line)
        if h and h.group(1).upper() in ("SYNOPSIS", "DESCRIPTION", "PARAMETER", "EXAMPLE", "NOTES"):
            flush()
            key, buf = (h.group(1).upper(), h.group(2)), []
        else:
            buf.append(line)
    flush()
    out["parameters"] = params
    return out


def parse_params(text):
    m = re.search(r"(?im)^\s*param\s*\(", text)
    if m:
        open_idx = text.index("(", m.start())
    else:
        m = re.match(r"\s*function\s+[\w-]+\s*\(", text)
        if not m:
            return []
        open_idx = m.end() - 1
    close_idx = scan_balanced(text, open_idx, "(", ")")
    block = text[open_idx + 1:close_idx - 1]
    params = []
    for piece in split_top_level(block, ","):
        attrs, i, p = [], 0, piece
        while True:
            i = skip_ws(p, i)
            if i < len(p) and p[i] == "[":
                j = scan_balanced(p, i, "[", "]")
                attrs.append(p[i + 1:j - 1].strip()); i = j
            else:
                break
        nm = re.match(r"\$([A-Za-z_]\w*)\s*(?:=\s*(.*))?", p[i:], re.S)
        if not nm:
            continue
        prm = {"name": nm.group(1), "type": None, "sets": [], "mandatory_in": [], "validate_set": None}
        if nm.group(2):
            prm["default"] = nm.group(2).strip().strip('"').strip("'")
        for a in attrs:
            low = a.lower()
            if low.startswith("parameter"):
                inner = a[a.find("(") + 1:a.rfind(")")] if "(" in a else ""
                ps = re.search(r"ParameterSetName\s*=\s*[\"']([^\"']+)", inner, re.I)
                setname = ps.group(1) if ps else "__AllParameterSets"
                prm["sets"].append(setname)
                if re.search(r"Mandatory\s*=\s*\$true|(^|,)\s*Mandatory\s*(,|$)", inner, re.I):
                    prm["mandatory_in"].append(setname)
                if re.search(r"ValueFromPipeline", inner, re.I):
                    prm["pipeline"] = True
            elif low.startswith("validateset"):
                prm["validate_set"] = re.findall(r"[\"']([^\"']*)[\"']", a)
            elif re.match(r"^[\w.\[\]]+$", a) and not low.startswith(("alias", "allow", "validate", "cmdletbinding")):
                prm["type"] = a
        # inline "[string][ValidateSet(...)]$x" leaves the type before ValidateSet; handled above
        params.append(prm)
    return params


# ------------------------------------------------------------ main extraction

def extract(module_dir):
    psm1 = module_dir / CMDLET_FILE
    psd1 = module_dir / MANIFEST_FILE
    lines = read_source(psm1)
    manifest = psd1.read_text(encoding="utf-8-sig", errors="replace")
    version = re.search(r"ModuleVersion\s*=\s*'([^']+)'", manifest).group(1)
    exported = set(re.findall(r"'([A-Za-z]+-[A-Za-z0-9-]+)'", manifest.split("FunctionsToExport", 1)[1].split(")", 1)[0])) \
        if "FunctionsToExport" in manifest else set()

    funcs = split_functions(lines)
    code = code_lines(lines)
    route_builders = find_route_builders(funcs, code)
    helper_props = {}
    for f in funcs:
        text = "\n".join(lines[f["start"] - 1:f["end"]])
        f["text"] = text
        f["params"] = parse_params(text)
        f["help"] = parse_help(text)
        if f["name"].startswith("Create") and f["name"].endswith("Object"):
            props = set()
            for p in f["params"]:
                for m in re.finditer(r"\$" + re.escape(p["name"]) + r"\.([A-Za-z_][\w]*(?:\.[A-Za-z_]\w*)*)", text, re.I):
                    props.add(m.group(1))
            helper_props[f["name"]] = sorted(props)

    ops = []
    for f in funcs:
        stmts = list(logical_statements(code, f["start"], f["end"]))
        pnames = {p["name"].lower(): p for p in f["params"]}
        assigns = []  # (line, var, op, rhs)
        for ln, st in stmts:
            m = ASSIGN_RE.match(st)
            if m:
                assigns.append((ln, m.group(1).lower(), m.group(2), m.group(3)))
        call_lines_by_var = {}
        for ln, st in stmts:
            for cm in CALL_RE.finditer(st):
                seg = st[cm.start():]
                nxt = CALL_RE.search(seg, len(cm.group(0)))
                if nxt:
                    seg = seg[:nxt.start()]
                op = analyse_call(f, ln, st, cm.group(1), seg, assigns, call_lines_by_var, pnames, route_builders, helper_props)
                if op:
                    ops.extend(op)
    for f in funcs:
        f["exported"] = f["name"] in exported if exported else ("-" in f["name"])
    func_index = {f["name"]: f for f in funcs}
    for o in ops:
        f = func_index[o["function"]]
        o["exported"] = f["exported"]
        # status handling in the cmdlet body: hints for the Responses section
        t = f["text"]
        o["status_hints"] = {
            "checks_202": bool(re.search(r"StatusCode\s*-eq\s*[\"']?202", t)),
            "polls_location": bool(re.search(r"Headers\[['\"](Location|Operation-Location)['\"]\]", t)),
            "uses_wait_until_finished": "WaitUntilFinished" in t,
        }
    # attribute internal helper operations to the exported cmdlets that call them
    callers = {}
    for f in funcs:
        for g in funcs:
            if g is not f and not g["exported"] and re.search(r"(?<![\w-])" + re.escape(g["name"]) + r"(?![\w-])", f["text"]):
                callers.setdefault(g["name"], []).append(f["name"])
    for o in ops:
        if not o["exported"]:
            o["called_by"] = sorted(set(callers.get(o["function"], [])))
    cmdlets = [{
        "name": f["name"], "line": f["start"], "exported": f["exported"],
        "synopsis": f["help"].get("synopsis"), "description": f["help"].get("description"),
        "parameters": [dict(p, description=f["help"].get("parameters", {}).get(p["name"])) for p in f["params"]],
    } for f in funcs]
    return {"module": "Microsoft.PowerApps.Administration.PowerShell", "version": version,
            "source_file": CMDLET_FILE, "operations": ops, "cmdlets": cmdlets}


def route_candidates(var, call_line, assigns, call_lines_by_var):
    """Base route assignments feeding a call, plus later modifiers (ReplaceMacro / +=)."""
    before = [a for a in assigns if a[1] == var and a[0] <= call_line]
    bases = [a for a in before if template_from_rhs(a[3]) is not None]
    mods = [a for a in before if template_from_rhs(a[3]) is None]
    # Bases between the previous call on this variable and this call are if/else alternatives.
    # A window with none (GET then PUT on the same $route) reuses the previous call's window.
    bounds = sorted(l for l in call_lines_by_var.get(var, []) if l < call_line) + [call_line]
    chosen = []
    for k in range(len(bounds) - 1, -1, -1):
        lo = bounds[k - 1] if k > 0 else 0
        chosen = [b for b in bases if lo < b[0] <= bounds[k]]
        if chosen:
            break
    first = min(b[0] for b in chosen) if chosen else 0
    mods = [m for m in mods if m[0] > first]
    return chosen, mods


def template_from_rhs(rhs):
    rhs = rhs.strip()
    if not rhs or rhs[0] not in "\"'":
        return None
    t = expr_to_template(rhs)
    if t and ("://" in t or any(h in t for h in HOST_MACROS) or re.match(r"\{\w+\}/", t)):
        return t
    return None


def find_route_builders(funcs, code):
    """Functions that `return "https://..." -f args` (e.g. GetCopilotGovernanceSettingRoute)."""
    out = {}
    for f in funcs:
        text = " ".join(l.rstrip("`") for l in code[f["start"] - 1:f["end"]])
        m = re.search(r"return\s+(\"https://[^\"]*\")\s*-f\s+(.*)", text, re.S)
        if not m:
            continue
        tmpl = ps_string_to_template(m.group(1)).replace("{{", "\x00").replace("}}", "\x01")
        tmpl = re.sub(r"\{(\d+)\}", r"{f\1}", tmpl).replace("\x00", "{").replace("\x01", "}")
        rest = m.group(2)
        rest = rest[:rest.rfind("}")] if "}" in rest else rest
        out[f["name"]] = (tmpl, [a.strip() for a in split_top_level(rest, ",")])
    return out


def substitutions(text):
    subs = {}
    for m in MACRO_RE.finditer(text):
        macro = m.group(1)[1:-1]
        if macro in HOST_MACROS or macro == "{apiVersion}":
            if macro == "{apiVersion}":
                subs[macro] = clean_value(m.group(2))
            continue
        subs[macro] = clean_value(m.group(2))
    for m in DOTREPLACE_RE.finditer(text):
        subs[m.group(1)[1:-1]] = clean_value(m.group(2))
    return subs


def arg(seg, name):
    m = re.search(r"(?i)(?<![\w-])-" + name + r"\s+", seg)
    if not m:
        return None
    i = m.end()
    if i < len(seg) and seg[i] == "(":
        j = scan_balanced(seg, i, "(", ")")
        return seg[i:j]
    if i < len(seg) and seg[i] in "\"'":
        j = skip_string(seg, i)
        return seg[i:j]
    if seg.startswith("@{", i):
        return "@{}"
    mm = re.match(r"[^\s)]+", seg[i:])
    return mm.group(0) if mm else None


def analyse_call(f, ln, st, kind, seg, assigns, call_lines_by_var, pnames, route_builders, helper_props):
    method_raw = arg(seg, "Method")
    route_raw = arg(seg, "Route") or arg(seg, "Uri")
    if route_raw is None and kind in ("Invoke-WebRequest", "Invoke-RestMethod"):
        mm = re.match(r"\S+\s+(\$\w+)", seg)
        route_raw = mm.group(1) if mm else None
    if route_raw is None:
        return None
    body_raw = arg(seg, "Body")
    apiver_raw = arg(seg, "ApiVersion")
    method = (method_raw or "").strip("\"'").upper() or None
    if kind == "Invoke-WebRequest" and not method:
        method = "GET"  # Invoke-WebRequest default
    inline_subs = {}
    route_var = None
    inline_template = None
    if route_raw.startswith("("):
        inner = route_raw[1:-1]
        vm = re.match(r"\s*\$(\w+)", inner)
        route_var = vm.group(1).lower() if vm else None
        inline_subs = substitutions(inner)
    elif route_raw.startswith("$"):
        route_var = route_raw[1:].lower()
    elif route_raw[0] in "\"'":
        inline_template = ps_string_to_template(route_raw)

    results = []
    if route_var:
        call_lines_by_var.setdefault(route_var, [])
        bases, mods = route_candidates(route_var, ln, assigns, call_lines_by_var)
        call_lines_by_var[route_var].append(ln)
        src = [a for a in assigns if a[1] == route_var and a[0] <= ln]
        builder = re.match(r"\s*([A-Za-z][\w-]*)\b", src[-1][3]).group(1) if src and re.match(r"\s*[A-Za-z]", src[-1][3]) else None
        if not bases and builder in route_builders:
            tmpl, fargs = route_builders[builder]
            results.append({"template": tmpl, "route_line": src[-1][0], "route_builder": builder,
                            "subs": {"{f%d}" % k: v for k, v in enumerate(fargs)}, "query_appends": [], "route_var": route_var})
        elif not bases:
            results.append({"dynamic": True, "route_var": route_var,
                            "route_source": src[-1][3].strip()[:200] if src else None,
                            "route_line": src[-1][0] if src else None})
        for b in bases:
            subs = substitutions(b[3])
            tmpl = template_from_rhs(b[3])
            lead = re.match(r"\{(\w+)\}", tmpl)
            if lead and "{%s}" % lead.group(1) not in HOST_MACROS:
                ref = subs.get("{%s}" % lead.group(1), "")
                rv = re.match(r"\$(\w+)$", ref)
                outer = [a for a in assigns if rv and a[1] == rv.group(1).lower() and a[0] < b[0]
                         and template_from_rhs(a[3]) and "://" in template_from_rhs(a[3])]
                if outer:
                    osubs = substitutions(outer[-1][3])
                    tmpl = template_from_rhs(outer[-1][3]) + tmpl[lead.end():]
                    osubs.update({k: v for k, v in subs.items() if k != lead.group(0)})
                    subs = osubs
            appends = []
            for m in mods:
                subs.update(substitutions(m[3]))
                if m[2] == "+=":
                    t = expr_to_template(m[3])
                    if t:
                        appends.append({"line": m[0], "value": t})
            # pipeline substitutions in the call statement itself
            subs.update(inline_subs)
            results.append({"template": tmpl, "route_line": b[0],
                            "subs": subs, "query_appends": appends, "route_var": route_var})
    elif inline_template:
        results.append({"template": inline_template, "route_line": ln, "subs": substitutions(st), "query_appends": []})
    else:
        results.append({"dynamic": True, "route_source": route_raw, "route_line": ln})

    ops = []
    for r in results:
        op = {"function": f["name"], "call": kind, "call_line": ln, "method": method,
              "method_raw": method_raw, "route_line": r.get("route_line")}
        if r.get("route_builder"):
            op["route_builder"] = r["route_builder"]
        if r.get("dynamic"):
            op["dynamic_route"] = True
            op["route_source"] = r.get("route_source") or r.get("route_var")
            ops.append(op)
            continue
        tmpl = r["template"]
        host_macro = next((h for h in HOST_MACROS if h in tmpl), None)
        hm = re.match(r"https://([^/{]*\{?[^/]*)", tmpl)
        op["host"] = host_macro or (hm.group(1) if hm else None)
        path_q = re.sub(r"^https://[^/]+", "", tmpl)
        path, _, query = path_q.partition("?")
        op["template"] = tmpl
        op["path"] = path
        # path / query parameters
        uri_params = []
        for macro in re.findall(r"\{[A-Za-z_]\w*\}", path):
            if macro in HOST_MACROS:
                continue
            src = r["subs"].get(macro)
            uri_params.append(param_entry(macro.strip("{}"), "path", src, pnames))
        for macro in re.findall(r"\{\$([\w.]+)\}", path):
            uri_params.append(param_entry(macro, "path", "$" + macro, pnames))
        op["uri_parameters"] = uri_params
        q = []
        for part in [p for p in re.split(r"&", query) if p]:
            k, _, v = part.partition("=")
            entry = {"name": k, "value": v}
            if v.startswith("{") and v.endswith("}") and v != "{apiVersion}":
                macro = v
                src = r["subs"].get(macro) or (v[1:-1] if v.startswith("{$") else None)
                entry["source"] = src
                entry["cmdlet_parameter"] = match_param(src, pnames)
            q.append(entry)
        op["query"] = q
        op["query_appends"] = r["query_appends"]
        # api-version
        op["api_version"] = api_version_for(tmpl, query, kind, apiver_raw, r["subs"], pnames)
        # body
        op["body"] = body_for(body_raw, f, ln, assigns, pnames)
        # response hints
        op["response_hints"] = response_hints(f, ln, st, helper_props)
        ops.append(op)
    return ops


def match_param(src, pnames):
    if not src:
        return None
    m = re.match(r"\$([A-Za-z_]\w*)$", src.strip())
    if m and m.group(1).lower() in pnames:
        return pnames[m.group(1).lower()]["name"]
    return None


def param_entry(name, where, src, pnames):
    return {"name": name, "in": where, "source": src, "cmdlet_parameter": match_param(src, pnames)}


def api_version_for(tmpl, query, kind, apiver_raw, subs, pnames):
    m = re.search(r"api-version=([^&]+)", query)
    if not m:
        return {"value": None, "source": "none", "note": "route has no api-version query parameter"}
    raw = m.group(1)
    if raw == "{apiVersion}":
        if "{apiVersion}" in subs:
            return resolve_ver(subs["{apiVersion}"], pnames, "ReplaceMacro in cmdlet")
        if kind in ("InvokeApi", "InvokeApiNoParseContent"):
            if apiver_raw:
                return resolve_ver(apiver_raw, pnames, "-ApiVersion argument")
            return {"value": INVOKE_API_DEFAULT_API_VERSION, "source": "InvokeApi default"}
        return {"value": "{apiVersion}", "source": "unsubstituted",
                "note": "called through %s, which does not replace {apiVersion}" % kind}
    if raw.startswith("{$"):
        return resolve_ver("$" + raw[2:-1], pnames, "interpolated in route string")
    return {"value": raw, "source": "hard-coded in route"}


def resolve_ver(expr, pnames, how):
    expr = expr.strip().strip('"').strip("'")
    m = re.match(r"\$([A-Za-z_]\w*)$", expr)
    if m and m.group(1).lower() in pnames:
        p = pnames[m.group(1).lower()]
        return {"value": p.get("default"), "source": "cmdlet parameter -%s default (%s)" % (p["name"], how),
                "overridable": True}
    return {"value": expr, "source": how}


def body_for(body_raw, f, call_line, assigns, pnames):
    if body_raw is None or body_raw.lower() == "$null":
        return None
    if body_raw == "@{}":
        return {"kind": "literal", "schema": {"type": "object", "fields": {}}}
    m = re.match(r"\$([A-Za-z_]\w*)$", body_raw)
    if not m:
        return {"kind": "expr", "expr": body_raw}
    var = m.group(1).lower()
    out = {"var": m.group(1)}
    if var in pnames:
        p = pnames[var]
        out.update(kind="parameter", cmdlet_parameter=p["name"], type=p.get("type"))
    src = [a for a in assigns if a[1] == var and a[0] < call_line and a[2] == "="]
    if src:
        ln, _, _, rhs = src[-1]
        out["assigned_line"] = ln
        text = f["text"]
        # re-locate the assignment in the raw function text to parse multi-line literals
        rel = ln - f["start"]
        raw_lines = text.split("\n")
        raw = "\n".join(raw_lines[rel:])
        eq = raw.find("=")
        rhs_raw = raw[eq + 1:]
        if re.match(r"\s*(\[\w+\]\s*)?@[{(]", rhs_raw):
            node, _ = parse_literal(rhs_raw, 0)
            out.setdefault("kind", "literal")
            out["schema"] = node
        elif CALL_RE.search(rhs.split("|")[0]):
            out.setdefault("kind", "roundtrip")
            out["note"] = "body is the object returned by an earlier request in the same cmdlet, then modified"
        else:
            out.setdefault("kind", "expr")
            out["expr"] = rhs.strip()[:200]
    # property writes on the body variable, e.g. $policy.properties.definition.constraints = ...
    writes = sorted(set(mm.group(1) for mm in re.finditer(
        r"\$" + re.escape(m.group(1)) + r"\.([A-Za-z_\"$`][\w.\"$`]*)\s*(?:\+?=)(?!=)", f["text"], re.I)))
    if writes:
        out["property_writes"] = [w.replace("`", "") for w in writes]
    if "kind" not in out and not src and not writes:
        out["kind"] = "undefined_variable"
        out["note"] = "variable is never assigned in the cmdlet, so it is $null and no body is sent"
    out.setdefault("kind", "unknown")
    return out


def response_hints(f, call_line, st, helper_props):
    hints = {}
    m = re.match(r"\s*\$([A-Za-z_]\w*)\s*=\s*(?:\(?\s*)?(InvokeApi|InvokeApiNoParseContent|Invoke-Request)", st)
    rel = call_line - f["start"]
    after = "\n".join(f["text"].split("\n")[rel:])
    props = set()
    if m:
        var = m.group(1)
        hints["result_var"] = var
        for mm in re.finditer(r"\$" + re.escape(var) + r"\.([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)", after, re.I):
            props.add(mm.group(1))
    for mm in re.finditer(r"\$_\.([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)", st):
        props.add(mm.group(1))
    if props:
        hints["properties_read"] = sorted(props)
    helpers = sorted(set(h for h in re.findall(r"\b(Create\w+Object)\b", f["text"]) if h in helper_props))
    if helpers:
        hints["mapped_by"] = {h: helper_props[h] for h in helpers}
    if re.match(r"\s*return\s+(InvokeApi|Invoke-Request)", st):
        hints["returned_raw"] = True
    return hints


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("module_dir", type=Path)
    ap.add_argument("-o", "--output", type=Path, default=Path("endpoints.json"))
    a = ap.parse_args()
    data = extract(a.module_dir)
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    ops = data["operations"]
    print("module %s: %d functions, %d call sites, %d with static routes, %d dynamic"
          % (data["version"], len(data["cmdlets"]), len(ops),
             sum(1 for o in ops if not o.get("dynamic_route")), sum(1 for o in ops if o.get("dynamic_route"))),
          file=sys.stderr)


if __name__ == "__main__":
    main()
