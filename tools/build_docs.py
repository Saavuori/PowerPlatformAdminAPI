#!/usr/bin/env python3
"""Build the HTML reference and openapi.yaml from endpoints.json + curation.yaml.

    python -I tools/build_docs.py --endpoints out/endpoints.json --curation tools/curation.yaml \
        --source <module_dir> --template tools/template.html --out out

<module_dir> is only read (never executed) to re-check every route against its source line.
"""
import argparse
import json
import re
import sys
from collections import Counter, OrderedDict
from pathlib import Path

import yaml

PROD_HOSTS = {
    "{bapEndpoint}": "api.bap.microsoft.com",
    "{powerAppsEndpoint}": "api.powerapps.com",
    "{flowEndpoint}": "api.flow.microsoft.com",
    "{powerPlatformEndpoint}": "api.powerplatform.com",
    "{cdsOneEndpoint}": "api.cds.microsoft.com",
    "api.cds.microsoft.com": "api.cds.microsoft.com",
}
# RestClientModule.psm1 Get-AudienceForHostName (prod hosts)
AUDIENCES = {
    "{bapEndpoint}": "https://service.powerapps.com/",
    "{powerAppsEndpoint}": "https://service.powerapps.com/",
    "{flowEndpoint}": "https://service.flow.microsoft.com/",
    "{powerPlatformEndpoint}": "https://api.powerplatform.com",
    "{cdsOneEndpoint}": "https://management.azure.com/",
    "api.cds.microsoft.com": "https://management.azure.com/",
}
SOURCE_FILE = "Microsoft.PowerApps.Administration.PowerShell.psm1"

ACTION_WORDS = ("get", "set", "list", "update", "modify", "add", "remove", "generate", "export", "import",
                "validate", "recover", "provision", "copy", "restore", "reset", "unlock", "lock", "approve",
                "reject", "prepare", "migrate", "delete", "publish", "acquire", "release", "enable", "disable",
                "associate", "dissociate", "start", "stop", "cancel", "create", "reevaluate", "activate")


def camel_words(s):
    s = s.replace("~", "")
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s).replace("_", " ").split()
    return " ".join(w[:1].upper() + w[1:] for w in words)


def singular(w):
    if w.endswith("ies"):
        return w[:-3] + "y"
    if w.endswith("ses") or w.endswith("xes"):
        return w[:-2]
    if w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


class Builder:
    def __init__(self, endpoints, cur, src_lines):
        self.ep = endpoints
        self.cur = cur
        self.src = src_lines
        self.version = endpoints["version"]
        self.cmdlets = {c["name"]: c for c in endpoints["cmdlets"]}
        self.aliases = cur.get("param_aliases", {})

    # ------------------------------------------------------------- normalisation
    def alias(self, name):
        return self.aliases.get(name) or self.aliases.get(name.lower()) or name.lstrip("$")

    def norm_path(self, path):
        path = re.sub(r"\{\$?([\w.]+)\}", lambda m: "{%s}" % self.alias(m.group(0)[1:-1]), path)
        for o in self.cur.get("path_param_overrides", []):
            if re.search(o["match"], path):
                path = path.replace("{%s}" % o["from"], "{%s}" % o["to"])
        return path.rstrip("/") if path != "/" else path

    def merge(self):
        merged = OrderedDict()
        self.dynamic = []
        for o in self.ep["operations"]:
            if o.get("dynamic_route"):
                self.dynamic.append(o)
                continue
            path = self.norm_path(o["path"])
            key = "%s %s%s" % (o["method"], o["host"], path)
            m = merged.setdefault(key, {"key": key, "method": o["method"], "host": o["host"], "path": path, "records": []})
            m["records"].append(o)
        return list(merged.values())

    # ------------------------------------------------------------------ grouping
    def group_of(self, op):
        target = op["host"] + op["path"]
        for si, sec in enumerate(self.cur["sections"]):
            for g in sec["groups"]:
                if re.search(g["match"], target):
                    return sec["name"], g["name"]
        return "Platform administration", "Misc"

    def auto_name(self, op):
        segs = [s for s in op["path"].strip("/").split("/") if s]
        method = op["method"]
        funcs = [r["function"] for r in op["records"]]
        last = segs[-1] if segs else ""
        lits = [s for s in segs if not s.startswith("{")]
        if last.startswith("{"):
            noun = camel_words(singular(lits[-1])) if lits else "Resource"
            if method == "GET":
                return "Get " + noun
            if method == "PATCH":
                return "Update " + noun
            if method == "DELETE":
                return "Delete " + noun
            if method == "PUT":
                return ("Create Or Update " if any(f.startswith(("New-", "Add-")) for f in funcs) else "Update ") + noun
            return "Post " + noun
        low = last.lower()
        if low.startswith(ACTION_WORDS) and not (method == "GET" and low.endswith("s") and not low.startswith(("get", "list"))):
            name = camel_words(last)
            if low in ("get", "delete", "approve", "reject", "prepare", "migrate", "start", "stop", "restore",
                       "recover", "reset", "unlock", "activate", "publish"):
                parent = next((s for s in reversed(segs[:-1]) if not s.startswith("{")), "")
                name = camel_words(last) + " " + camel_words(singular(parent))
            return name
        noun = camel_words(last)
        if method == "GET":
            return "List " + noun
        if method == "POST":
            return "Create " + camel_words(singular(last))
        if method == "DELETE":
            return "Delete " + noun
        if method == "PATCH":
            return "Update " + noun
        return "Put " + noun

    # -------------------------------------------------------------- page model
    def build(self):
        ops = self.merge()
        cur_ops = self.cur.get("operations", {})
        official = self.cur.get("official", {})
        missing_cur = set(cur_ops) - {o["key"] for o in ops}
        if missing_cur:
            print("WARNING curated keys with no extracted operation:\n  " + "\n  ".join(sorted(missing_cur)), file=sys.stderr)
        pages = []
        for op in ops:
            c = cur_ops.get(op["key"], {})
            section, group = self.group_of(op)
            page = {
                "key": op["key"], "method": op["method"], "host": op["host"], "path": op["path"],
                "section": section, "group": group,
                "name": c.get("name") or self.cur.get("names", {}).get(op["key"]) or self.auto_name(op),
                "curated": bool(c),
            }
            page.update(self.describe(op, c))
            page["official"] = official.get(op["key"])
            pages.append(page)
        # unique names within a group
        by = {}
        for p in pages:
            by.setdefault((p["group"], p["name"]), []).append(p)
        for (g, n), ps in by.items():
            if len(ps) > 1:
                for p in ps:
                    tag = "admin scope" if "/scopes/admin/" in p["path"] else ("environment" if "{environmentName}" in p["path"] else "tenant")
                    p["name"] = "%s (%s)" % (n, tag)
                again = Counter(p["name"] for p in ps)
                for p in ps:
                    if again[p["name"]] > 1:
                        p["name"] = "%s [%s]" % (p["name"], p["path"].split("/")[-2])
        order = [g["name"] for s in self.cur["sections"] for g in s["groups"]] + ["Misc"]
        mrank = {"GET": 0, "POST": 1, "PUT": 2, "PATCH": 3, "DELETE": 4}
        pages.sort(key=lambda p: (order.index(p["group"]), p["path"].count("/"), p["path"], mrank.get(p["method"], 9)))
        for p in pages:
            p["id"] = re.sub(r"[^a-z0-9]+", "-", (p["group"] + "-" + p["name"]).lower()).strip("-")
        ids = Counter(p["id"] for p in pages)
        assert max(ids.values()) == 1, [i for i, n in ids.items() if n > 1]
        return pages

    def describe(self, op, c):
        recs = op["records"]
        funcs = []
        for r in recs:
            if r.get("exported", True):
                funcs.append(r["function"])
            else:
                funcs.extend(r.get("called_by") or [])
                funcs.append(r["function"] + " (internal)")
        funcs = list(OrderedDict.fromkeys(funcs))
        primary = next((self.cmdlets.get(f) for f in funcs if f in self.cmdlets), None)
        desc = c.get("description")
        if not desc and primary and primary.get("synopsis"):
            desc = primary["synopsis"].rstrip(".") + ". (Cmdlet synopsis from %s.)" % primary["name"]
        # api-version
        vers = Counter()
        vsrc = {}
        for r in recs:
            v = r["api_version"].get("value")
            src = r["api_version"].get("source")
            if v is None and src.startswith("cmdlet parameter") and not r.get("exported", True):
                # internal helper with a mandatory -ApiVersion: use the calling cmdlets' defaults
                for caller in r.get("called_by") or []:
                    d = next((p.get("default") for p in self.cmdlets.get(caller, {}).get("parameters", [])
                              if p["name"].lower() == "apiversion"), None)
                    if d:
                        vers[d] += 1
                        vsrc.setdefault(d, set()).add("%s via %s (caller -ApiVersion default)" % (caller, r["function"]))
                continue
            vers[v] += 1
            vsrc.setdefault(v, set()).add("%s (%s)" % (r["function"], src))
        api_primary = vers.most_common(1)[0][0]
        api_versions = [{"value": v, "used_by": sorted(vsrc[v])} for v, _ in vers.most_common()]
        # URI parameters
        params = []
        host_params = re.findall(r"\{(\w+)\}", op["host"]) if not op["host"].startswith("{") else []
        tmpl0 = recs[0]["template"]
        if "{f0}" in tmpl0:
            host_params = [self.alias("f0"), self.alias("f1")]
        for hp in host_params:
            params.append({"name": hp, "in": "host", "required": True, "type": "string",
                           "description": "Derived from the environment id: lower-case, dashes removed; prefix = all but the last 2 characters, suffix = the last 2 characters. [S]"})
        seen = set()
        for name in re.findall(r"\{(\w+)\}", op["path"]):
            if name in seen:
                continue
            seen.add(name)
            params.append(self.param_doc(name, "path", recs))
        qnames = OrderedDict()
        for r in recs:
            for q in r.get("query", []):
                if q["name"] == "api-version":
                    continue
                qnames.setdefault(q["name"], q)
            for a in r.get("query_appends", []):
                qnames.setdefault("(appended) " + a["value"].strip()[:60], {"name": "(appended)", "value": a["value"]})
        if api_primary:
            params.append({"name": "api-version", "in": "query", "required": True, "type": "string",
                           "description": "Module sends %s." % ", ".join("%s (%s)" % (a["value"], "; ".join(a["used_by"])) for a in api_versions)})
        for k, q in qnames.items():
            if q["name"] == "(appended)":
                params.append({"name": "$filter", "in": "query", "required": False, "type": "string",
                               "description": "Conditionally appended by the cmdlet: %s [S]" % q["value"]})
                continue
            src = q.get("cmdlet_parameter")
            d = "Module sends `%s`" % q["value"] if q.get("value") else "Flag parameter"
            if src:
                d += " from -%s" % src
            params.append({"name": q["name"], "in": "query", "required": False, "type": "string", "description": d + ". [S]"})
        # body
        body = self.body_doc(op, recs, c)
        # responses
        responses = self.responses_doc(op, recs, c)
        hints = OrderedDict()
        mapped = {}
        for r in recs:
            for p in r.get("response_hints", {}).get("properties_read", []):
                if not re.search(r"PSObject|PSobject|StatusCode|Headers|Content$", p):
                    hints[p] = 1
            mapped.update(r.get("response_hints", {}).get("mapped_by", {}))
        sources = [{"function": r["function"], "route_line": r["route_line"], "call_line": r["call_line"],
                    "call": r["call"], "exported": r.get("exported", True)} for r in recs]
        sources = list({(s["function"], s["route_line"], s["call_line"]): s for s in sources}.values())
        group_flow = op["host"] == "{flowEndpoint}"
        roles = self.cur.get("roles", {})
        security = {
            "audience": AUDIENCES.get(op["host"], "https://service.powerapps.com/"),
            "role": roles.get("flow") if group_flow else roles.get("default"),
        }
        caveats = list(c.get("caveats", []))
        if op["host"] in ("{cdsOneEndpoint}", "api.cds.microsoft.com"):
            caveats.append("[S] Get-AudienceForHostName has no entry for this host, so the module requests a token for https://management.azure.com/ (its fallback). This legacy CDS 1.0 route is likely retired.")
        if any(r["host"] == "api.cds.microsoft.com" for r in recs):
            caveats.append("[S] The host is hard-coded to the commercial cloud (api.cds.microsoft.com) instead of using {cdsOneEndpoint}.")
        if any(r["call"] == "Invoke-Request" for r in recs) and not c:
            caveats.append("[S] Called through Invoke-Request directly, bypassing InvokeApi (no nextLink paging; {apiVersion} is not substituted unless the cmdlet does it).")
        examples = self.examples(op, recs, c, api_primary, body)
        confidence = {
            "route": "Verified from source",
            "api_version": ("Verified from source: %s" % ", ".join(str(a["value"]) for a in api_versions)) if api_primary
            else "Verified from source: no api-version is sent",
            "body": body.get("basis_label") if body else "No body",
            "response": responses.get("basis_label"),
            "live": "Not verified live",
        }
        return {
            "description": desc or "", "cmdlets": funcs, "api_version": api_primary, "api_versions": api_versions,
            "params": params, "body": body, "responses": responses, "response_hints": list(hints),
            "mapped_by": mapped, "sources": sources, "security": security, "caveats": caveats,
            "examples": examples, "confidence": confidence,
            "definitions": sorted(self.collect_defs(c)),
        }

    def param_doc(self, name, where, recs):
        cmdlet_param, desc = None, None
        for r in recs:
            for u in r.get("uri_parameters", []):
                if self.alias(u["name"]) == name or u["name"] == name:
                    if u.get("cmdlet_parameter"):
                        cmdlet_param = u["cmdlet_parameter"]
                        cm = self.cmdlets.get(r["function"])
                        if cm:
                            for p in cm["parameters"]:
                                if p["name"] == cmdlet_param:
                                    desc = p.get("description")
                    elif u.get("source") and not cmdlet_param:
                        desc = desc or "Filled from `%s` in %s." % (u["source"], r["function"])
        d = (desc or "").strip()
        if cmdlet_param:
            d = ("%s (cmdlet parameter -%s)" % (d, cmdlet_param)).strip()
        return {"name": name, "in": where, "required": True, "type": "string", "description": d}

    def body_doc(self, op, recs, c):
        req = c.get("request")
        if req:
            d = self.cur["definitions"].get(req["type"], {})
            rows = [{"name": f["name"], "type": f["type"], "basis": f["basis"], "description": f.get("description", "")}
                    for f in d.get("fields", [])]
            bases = sorted({r["basis"] for r in rows})
            label = {"S": "Verified from source", "D": "Documented on Learn (cmdlet docs)", "I": "Inferred"}[req["basis"]]
            if len(bases) > 1:
                label += "; fields mixed (%s), see Basis column" % "/".join(bases)
            return {"kind": "definition", "type": req["type"], "rows": rows, "basis": req["basis"], "basis_label": label}
        b = next((r["body"] for r in recs if r.get("body") and r["body"].get("kind") not in ("undefined_variable",)), None)
        if not b:
            return None
        if b.get("schema"):
            rows = []
            self.flatten(b["schema"], "", rows)
            return {"kind": "literal", "rows": rows, "basis": "S", "basis_label": "Verified from source (body literal)",
                    "param": b.get("cmdlet_parameter"), "writes": b.get("property_writes", [])}
        if b["kind"] == "parameter":
            return {"kind": "parameter", "param": b["cmdlet_parameter"], "basis": "I",
                    "basis_label": "Caller-supplied; shape not defined by the module", "rows": []}
        if b["kind"] == "roundtrip":
            return {"kind": "roundtrip", "rows": [], "basis": "S", "writes": b.get("property_writes", []),
                    "basis_label": "Verified from source: full object from an earlier GET, modified"}
        return {"kind": b["kind"], "rows": [], "basis": "I", "basis_label": "Unknown", "expr": b.get("expr")}

    def flatten(self, node, prefix, rows):
        t = node.get("type")
        if t == "object":
            if prefix and not node.get("fields"):
                rows.append({"name": prefix, "type": "object", "basis": "S", "description": "Empty object in source."})
            for k, v in node.get("fields", {}).items():
                self.flatten(v, (prefix + "." if prefix else "") + k, rows)
        elif t == "array":
            items = node.get("items", [])
            if items and items[0].get("type") == "object":
                rows.append({"name": prefix, "type": "object[]", "basis": "S", "description": ""})
                self.flatten(items[0], prefix + "[]", rows)
            else:
                rows.append({"name": prefix, "type": "array", "basis": "S",
                             "description": ("Items: " + ", ".join(i.get("value", i.get("type")) if isinstance(i.get("value", ""), str) else str(i.get("value")) for i in items)) if items else "Empty array in source."})
        else:
            v = node.get("value")
            desc = ""
            if t == "string":
                desc = "Literal `%s`" % v if v and "{$" not in v else "From %s" % (v or "''")
            elif t == "expr":
                desc = "Set from `%s`" % (v[:90] + ("…" if len(v) > 90 else ""))
            elif t in ("boolean", "number"):
                desc = "Literal `%s`" % json.dumps(v)
            rows.append({"name": prefix, "type": {"expr": "any", "null": "null"}.get(t, t), "basis": "S", "description": desc})

    def responses_doc(self, op, recs, c):
        if c.get("responses_override"):
            rows = list(c["responses_override"])
            basis_label = "Verified from source (status handling)"
        else:
            resp = c.get("response")
            rtype = resp["type"] if resp else ""
            sh = {k: any(r.get("status_hints", {}).get(k) for r in recs) for k in ("checks_202", "polls_location", "uses_wait_until_finished")}
            m = op["method"]
            rows = []
            ok = {"GET": "200 OK", "DELETE": "200 OK", "POST": "200 OK", "PUT": "200 OK", "PATCH": "200 OK"}[m]
            rows.append({"code": ok, "type": rtype, "description": "Success. Exact 2xx code not captured; the module accepts any 2xx [I]."})
            if m == "DELETE":
                rows.append({"code": "204 No Content", "type": "", "description": "Possible alternative success code [I]."})
            if sh["checks_202"] or sh["polls_location"] or sh["uses_wait_until_finished"]:
                rows.append({"code": "202 Accepted", "type": "", "description": "Long-running: the cmdlet reads the Location or Operation-Location header and polls it with GET every 5 s until state.id is Succeeded or Failed [S]."})
            basis_label = {"S": "Verified from source", "D": "Documented on Learn (cmdlet docs)", "I": "Inferred"}[resp["basis"]] if resp else "Inferred (status codes only)"
        rows.append({"code": "Other Status Codes", "type": "ErrorResponse", "description": "Error body as read by the REST client [S]."})
        return {"rows": rows, "basis_label": basis_label}

    def collect_defs(self, c):
        defs = self.cur["definitions"]
        out = set()
        todo = [x["type"] for x in (c.get("request"), c.get("response")) if x] + ["ErrorResponse"]
        while todo:
            t = todo.pop().replace("[]", "")
            if t in defs and t not in out:
                out.add(t)
                for f in defs[t].get("fields", []):
                    todo.append(f["type"].replace("[]", ""))
        return out

    def examples(self, op, recs, c, api, body):
        tmpl = recs[0]["template"]
        host = PROD_HOSTS.get(op["host"], op["host"])
        url = tmpl
        for macro, h in PROD_HOSTS.items():
            url = url.replace(macro, h)
        url = url.replace("{f0}", "{environmentIdPrefix}").replace("{f1}", "{environmentIdSuffix}")
        url = re.sub(r"^https://[^/]+", "https://" + (host if not op["host"].startswith("{powerPlatform") else
                                                     "{environmentIdPrefix}.{environmentIdSuffix}.environment.api.powerplatform.com"), url)
        path, _, query = url.partition("?")
        path = "https://" + path[len("https://"):].split("/", 1)[0] + self.norm_path("/" + path[len("https://"):].split("/", 1)[1])
        qparts = []
        for part in [p for p in query.split("&") if p]:
            k, _, v = part.partition("=")
            if k == "api-version":
                v = api or v
            elif v.startswith("{"):
                v = "{" + self.alias(v.strip("{}").lstrip("$")) + "}"
            qparts.append(k + ("=" + v if _ else ""))
        url = path + ("?" + "&".join(qparts) if qparts else "")
        sample_req = c.get("sample_request")
        if sample_req is None and c.get("sample_request_ref"):
            sample_req = self.cur["definitions"][c["sample_request_ref"]].get("sample")
        if sample_req is None and body and body.get("kind") == "literal":
            sample_req = next((self.sample_from_node(r["body"]["schema"]) for r in recs if r.get("body") and r["body"].get("schema")), None)
        sample_resp = c.get("sample_response")
        if sample_resp is None and c.get("sample_response_ref"):
            sample_resp = self.cur["definitions"][c["sample_response_ref"]].get("sample")
        return {"url": url, "request_body": sample_req, "response_body": sample_resp,
                "audience": AUDIENCES.get(op["host"], "https://service.powerapps.com/")}

    def sample_from_node(self, node):
        t = node.get("type")
        if t == "object":
            return {k: self.sample_from_node(v) for k, v in node.get("fields", {}).items()}
        if t == "array":
            return [self.sample_from_node(i) for i in node.get("items", [])[:1]]
        if t == "string":
            v = node.get("value") or ""
            return re.sub(r"\{\$([\w.]+)\}", r"<\1>", v)
        if t in ("boolean", "number"):
            return node.get("value")
        if t == "null":
            return None
        return "<%s>" % node.get("value", "value")[:40]

    # ------------------------------------------------------------ verification
    def verify(self):
        """Re-read each route and call line from the source and confirm route literal and verb."""
        results = []
        for o in self.ep["operations"]:
            if o.get("dynamic_route"):
                continue
            rl = o["route_line"]
            window = " ".join(self.src[rl - 1:rl + 3]) if rl else ""
            path_lit = re.split(r"\{", o["path"], maxsplit=1)[0] if not o.get("route_builder") else "/copilotgovernance/settings/"
            route_ok = path_lit in window or (o.get("route_builder") is not None and path_lit in "\n".join(self.src))
            if not route_ok:  # composed from a base URI variable: base within the function, tail on the line
                tail = o["path"].rstrip("/").split("/")[-1]
                wide = " ".join(self.src[max(rl - 31, 0):rl + 3])
                route_ok = path_lit in wide and (tail.startswith("{") or tail in window)
            cl = o["call_line"]
            call_window = " ".join(self.src[cl - 1:cl + 3])
            verb_ok = bool(re.search(r"-Method\s+[\"']?%s\b" % o["method"], call_window, re.I)) or (
                o["call"] == "Invoke-WebRequest" and "-Method" not in call_window)
            ver = o["api_version"].get("value")
            if ver is None and o["api_version"].get("source", "").startswith("cmdlet parameter"):
                ver_ok = "{apiVersion}" in window  # caller-supplied; the placeholder is what the source proves
            elif ver and o["api_version"]["source"].startswith("cmdlet parameter"):
                pname = re.search(r"-(\w+) default", o["api_version"]["source"]).group(1)
                fstart = next(c["line"] for c in self.ep["cmdlets"] if c["name"] == o["function"])
                fend = cl
                ver_ok = bool(re.search(r"\$%s\s*=\s*[\"']%s[\"']" % (pname, re.escape(ver)), "\n".join(self.src[fstart - 1:fend]), re.I))
            elif ver:
                ver_ok = ver in window or ver in "\n".join(self.src[cl - 15:cl + 3]) or o["api_version"]["source"] == "InvokeApi default"
            else:
                ver_ok = "api-version" not in window
            results.append({"function": o["function"], "method": o["method"], "path": self.norm_path(o["path"]),
                            "route_line": rl, "call_line": cl, "route_ok": route_ok, "verb_ok": verb_ok,
                            "api_version": ver, "api_version_ok": ver_ok,
                            "dlp": bool(re.search(r"Governance|apiPolicies|reevaluateconnectionsdlp", o["path"]))})
        return results


def to_openapi(pages, cur, version):
    defs = cur["definitions"]
    servers = {
        "{bapEndpoint}": "https://api.bap.microsoft.com",
        "{powerAppsEndpoint}": "https://api.powerapps.com",
        "{flowEndpoint}": "https://api.flow.microsoft.com",
        "{cdsOneEndpoint}": "https://api.cds.microsoft.com",
        "api.cds.microsoft.com": "https://api.cds.microsoft.com",
    }
    schemes = {"https://service.powerapps.com/": "bapToken", "https://service.flow.microsoft.com/": "flowToken",
               "https://api.powerplatform.com": "powerPlatformToken", "https://management.azure.com/": "armToken"}
    paths = OrderedDict()
    for p in pages:
        url_path = p["path"]
        item = paths.setdefault(url_path, OrderedDict())
        if p["host"] != "{bapEndpoint}":
            if p["host"].startswith("{powerPlatform"):
                item["servers"] = [{"url": "https://{environmentIdPrefix}.{environmentIdSuffix}.environment.api.powerplatform.com",
                                    "variables": {"environmentIdPrefix": {"default": "00000000000000000000000000000000"[:30]},
                                                  "environmentIdSuffix": {"default": "00"}}}]
            else:
                item["servers"] = [{"url": servers[p["host"]]}]
        params = []
        for prm in p["params"]:
            if prm["in"] == "host" or prm["name"].startswith("("):
                continue
            entry = {"name": prm["name"], "in": prm["in"], "required": prm["required"], "schema": {"type": "string"},
                     "description": prm["description"]}
            if prm["name"] == "api-version" and p["api_version"]:
                entry["schema"]["default"] = p["api_version"]
            params.append(entry)
        opid = re.sub(r"[^A-Za-z0-9]", "", p["group"].title()) + "_" + re.sub(r"[^A-Za-z0-9]", "", p["name"].title())
        o = OrderedDict()
        o["tags"] = [p["group"]]
        o["summary"] = "%s - %s" % (p["group"], p["name"])
        o["description"] = p["description"] + ("\n\nOfficially documented: " + p["official"]["url"] if p.get("official") else
                                                "\n\nUndocumented. Derived from Microsoft.PowerApps.Administration.PowerShell %s." % version)
        o["operationId"] = opid
        if params:
            o["parameters"] = params
        if p["body"]:
            if p["body"].get("type"):
                schema = {"$ref": "#/components/schemas/%s" % p["body"]["type"]}
            else:
                schema = {"type": "object", "description": p["body"]["basis_label"]}
                if p["body"]["rows"]:
                    schema = rows_to_schema(p["body"]["rows"])
            o["requestBody"] = {"required": True, "content": {"application/json": {"schema": schema}}}
        resps = OrderedDict()
        for r in p["responses"]["rows"]:
            code = r["code"].split()[0]
            code = "default" if code == "Other" else code
            entry = {"description": r["description"] or r["code"]}
            if r.get("type") and r["type"] in defs:
                entry["content"] = {"application/json": {"schema": {"$ref": "#/components/schemas/%s" % r["type"]}}}
            resps[code] = entry
        o["responses"] = resps
        o["security"] = [{schemes.get(p["security"]["audience"], "bapToken"): []}]
        o["x-confidence"] = p["confidence"]
        o["x-cmdlets"] = p["cmdlets"]
        o["x-source"] = ["%s:%s (%s)" % (SOURCE_FILE, s["route_line"], s["function"]) for s in p["sources"]]
        if p["caveats"]:
            o["x-caveats"] = p["caveats"]
        item[p["method"].lower()] = o
    comps = OrderedDict()
    for name, d in defs.items():
        if "enum" in d:
            comps[name] = {"type": "string", "enum": [e["value"] for e in d["enum"]], "description": d.get("description", ""),
                           "x-basis": {e["value"]: e["basis"] for e in d["enum"]}}
        else:
            s = rows_to_schema(d.get("fields", []), defs)
            if d.get("description"):
                s["description"] = d["description"]
            comps[name] = s
    doc = OrderedDict()
    doc["openapi"] = "3.0.3"
    doc["info"] = {"title": "Power Platform admin (BAP) API — unofficial", "version": version,
                   "description": "UNOFFICIAL. Reverse-documented from Microsoft.PowerApps.Administration.PowerShell %s. "
                                  "Routes may change without notice. x-confidence and x-basis mark what is verified "
                                  "from source (S), documented on Learn (D) or inferred (I)." % version}
    doc["servers"] = [{"url": "https://api.bap.microsoft.com", "description": "Commercial (prod). See Common concepts for other clouds."}]
    doc["tags"] = [{"name": g["name"], "description": g.get("intro", "")} for s in cur["sections"] for g in s["groups"]]
    doc["paths"] = paths
    doc["components"] = {"schemas": comps, "securitySchemes": {
        "bapToken": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT", "description": "Entra ID token, audience https://service.powerapps.com/ (scope https://service.powerapps.com//.default)."},
        "flowToken": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT", "description": "Audience https://service.flow.microsoft.com/."},
        "powerPlatformToken": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT", "description": "Audience https://api.powerplatform.com."},
        "armToken": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT", "description": "Module fallback audience https://management.azure.com/ (legacy CDS host)."},
    }}
    return doc


TYPE_MAP = {"string": "string", "integer": "integer", "boolean": "boolean", "number": "number", "object": "object",
            "string (date-time)": "string", "any": "string", "null": "string", "array": "array"}


def rows_to_schema(rows, defs=None):
    root = {"type": "object", "properties": OrderedDict()}
    for r in rows:
        parts = r["name"].replace("[]", ".[]").split(".")
        node = root
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            if part == "[]":
                continue
            nxt_is_arr = i + 1 < len(parts) and parts[i + 1] == "[]"
            props = node.setdefault("properties", OrderedDict())
            if last:
                props[part] = type_schema(r["type"], defs)
                props[part]["description"] = r.get("description", "")
                props[part]["x-basis"] = r.get("basis", "I")
            else:
                child = props.setdefault(part, {"type": "array", "items": {"type": "object"}} if nxt_is_arr else {"type": "object"})
                node = child["items"] if child.get("type") == "array" else child
    return root


def type_schema(t, defs):
    arr = t.endswith("[]")
    base = t[:-2] if arr else t
    if defs and base in defs:
        s = {"$ref": "#/components/schemas/%s" % base}
    else:
        s = {"type": TYPE_MAP.get(base, "object")}
        if base == "string (date-time)":
            s["format"] = "date-time"
        if s["type"] == "array":
            s["items"] = {}
    if arr:
        return {"type": "array", "items": s}
    if "$ref" in s:
        return {"allOf": [s]}
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoints", type=Path, required=True)
    ap.add_argument("--curation", type=Path, required=True)
    ap.add_argument("--source", type=Path, required=True)
    ap.add_argument("--template", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--pages", type=Path, help="also write a standalone GitHub Pages site (index.html + data files) here")
    a = ap.parse_args()
    endpoints = json.loads(a.endpoints.read_text(encoding="utf-8"))
    cur = yaml.safe_load(a.curation.read_text(encoding="utf-8"))
    src = (a.source / SOURCE_FILE).read_text(encoding="utf-8-sig", errors="replace").split("\n")
    b = Builder(endpoints, cur, src)
    pages = b.build()
    verification = b.verify()
    dyn = [{"function": o["function"], "method": o["method"], "call_line": o["call_line"],
            "source": (o.get("route_source") or "")[:160]} for o in b.dynamic]
    model = {"version": b.version, "module": endpoints["module"], "source_file": SOURCE_FILE,
             "sections": [{"name": s["name"], "groups": [{"name": g["name"], "intro": g.get("intro", "")} for g in s["groups"]]}
                          for s in cur["sections"]] + [{"name": "Other", "groups": [{"name": "Misc", "intro": "Operations no group rule matched."}]}],
             "pages": pages, "definitions": cur["definitions"], "verification": verification, "dynamic": dyn,
             "official_not_in_module": cur.get("official_not_in_module", []), "roles": cur.get("roles", {})}
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "model.json").write_text(json.dumps(model, indent=1, ensure_ascii=False), encoding="utf-8")
    data = json.dumps(model, ensure_ascii=False).replace("</", "<\\/")
    html = a.template.read_text(encoding="utf-8").replace("__DATA_JSON__", data).replace("__MODULE_VERSION__", b.version)
    (a.out / "site").mkdir(exist_ok=True)
    (a.out / "site" / "bap-admin-api.html").write_text(html, encoding="utf-8")
    oa = to_openapi(pages, cur, b.version)
    yaml.add_representer(OrderedDict, lambda d, v: d.represent_mapping("tag:yaml.org,2002:map", v.items()))
    oa_text = yaml.dump(oa, sort_keys=False, allow_unicode=True, width=120)
    (a.out / "openapi.yaml").write_text(oa_text, encoding="utf-8")
    if a.pages:
        # The template is an artifact body (the artifact host adds doctype and head);
        # GitHub Pages needs a complete document, so wrap it here.
        cut = html.index("</style>") + len("</style>")
        page = ("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
                "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">\n"
                "<style>body{margin:0}img{max-width:100%}[hidden]{display:none!important}</style>\n"
                + html[:cut] + "\n</head>\n<body>\n" + html[cut:] + "\n</body>\n</html>\n")
        a.pages.mkdir(parents=True, exist_ok=True)
        (a.pages / "index.html").write_text(page, encoding="utf-8")
        (a.pages / "openapi.yaml").write_text(oa_text, encoding="utf-8")
        (a.pages / "endpoints.json").write_text(a.endpoints.read_text(encoding="utf-8"), encoding="utf-8")
        (a.pages / ".nojekyll").write_text("", encoding="utf-8")
    bad = [v for v in verification if not (v["route_ok"] and v["verb_ok"] and v["api_version_ok"])]
    groups = Counter(p["group"] for p in pages)
    print("pages: %d (curated %d); groups: %s" % (len(pages), sum(p["curated"] for p in pages), dict(groups)), file=sys.stderr)
    print("verification: %d records, %d failing" % (len(verification), len(bad)), file=sys.stderr)
    for v in bad:
        print("  FAIL", v, file=sys.stderr)


if __name__ == "__main__":
    main()
