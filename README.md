# Power Platform admin (BAP) API reference — unofficial

**Browse it: https://saavuori.github.io/PowerPlatformAdminAPI/**

Operation-by-operation reference for the REST endpoints that the `Microsoft.PowerApps.Administration.PowerShell` module calls, mainly on `api.bap.microsoft.com`. It is laid out like the Power Platform REST reference on Microsoft Learn, and data policy (DLP) operations come first.

> **Unofficial. May change without notice.** Not affiliated with or endorsed by Microsoft. Microsoft documents only a few of these routes. Everything here was derived from module source **2.0.219** by static parsing; the module was never imported or run. Every page labels what is verified from source (S), documented on Learn (D) or inferred (I). Nothing has been verified against live traffic yet.

## Contents

| Path | What it is |
| --- | --- |
| [`docs/index.html`](docs/index.html) | The reference site (GitHub Pages): 172 operations, Common concepts, Verification, Definitions |
| [`docs/openapi.yaml`](docs/openapi.yaml) | OpenAPI 3.0.3 with `x-confidence`, `x-basis`, `x-cmdlets`, `x-source`. Import into Postman by URL: `https://saavuori.github.io/PowerPlatformAdminAPI/openapi.yaml` |
| [`docs/endpoints.json`](docs/endpoints.json) | Raw extraction, one record per call site (233 static, 12 dynamic) |
| [`tools/extract_endpoints.py`](tools/extract_endpoints.py) | Parser: module folder → `endpoints.json` |
| [`tools/curation.yaml`](tools/curation.yaml) | Hand-curated names, groups, schemas (per-field basis S/D/I), caveats, links to the officially documented endpoints |
| [`tools/build_docs.py`](tools/build_docs.py) | `endpoints.json` + curation → site, `openapi.yaml`, and a re-check of every route against its source line |
| [`tools/template.html`](tools/template.html) | Page template |

## Regenerate for a new module version

Requires Python 3 with PyYAML. A `.nupkg` is a zip. Unpack it into its own folder and run Python with `-I` from the repo root, not from inside the extracted folder:

```powershell
Invoke-WebRequest https://www.powershellgallery.com/api/v2/package/Microsoft.PowerApps.Administration.PowerShell -OutFile admin.zip
Expand-Archive admin.zip -DestinationPath .\module-src
python -I tools/extract_endpoints.py .\module-src -o out/endpoints.json
python -I tools/build_docs.py --endpoints out/endpoints.json --curation tools/curation.yaml --source .\module-src --template tools/template.html --out out --pages docs
```

`build_docs.py` warns about curated entries that no longer match a route, and lists every call site whose route, verb or api-version fails the source re-check. `out/` and `module-src/` are git-ignored.

To see what changed between module versions, keep each version's `endpoints.json` and compare `(method, path, api_version.value, body)` per record.

## Contributing live evidence

Pages say "Not verified live" until a captured request and response confirms them. If you capture traffic from the admin center (browser dev tools) or from the cmdlets (`-Verbose` plus Fiddler), open an issue with the redacted request line, status code and response shape.
