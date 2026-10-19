"""Current-run presentation only. Never discovers latest results or reads history."""
from html import escape
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import quote

from utils.errors import sanitize_message
from utils.run_context import ordinary_files
from utils.safe_artifact_write import write_run_json, write_run_text

STEPS = ("prepare", "static", "runtime", "access", "smoke", "secret", "schema")
CSS = """body{font:16px/1.5 system-ui,sans-serif;max-width:1150px;margin:32px auto;padding:0 20px;color:#18212f;background:#f8fafc}h1,h2{line-height:1.2}h2{margin-top:28px}table{border-collapse:collapse;table-layout:fixed;width:100%;background:white}th,td{text-align:left;border:1px solid #ccd5df;padding:8px;vertical-align:top;overflow-wrap:anywhere}th{background:#e8eef5}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#eef2f6;padding:12px}a{color:#0759a5}dl{display:grid;grid-template-columns:230px 1fr;gap:5px 16px}dt{font-weight:650}dd{margin:0;overflow-wrap:anywhere}.notice{border-left:4px solid #58718d;padding:12px;background:#e8eef5}.FAIL,.BLOCKED{font-weight:bold;color:#9c2727}.PASS{font-weight:bold;color:#096442}summary{cursor:pointer;padding:8px 0}code{overflow-wrap:anywhere}@media(max-width:650px){body{margin:16px auto}table{font-size:13px}th,td{padding:5px}dl{grid-template-columns:1fr}dd{margin-bottom:8px}}"""


def text(value):
    return sanitize_message(str(value if value is not None else "UNKNOWN"))


def evidence_path(value):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        return None
    path = PurePosixPath(value)
    if (path.is_absolute() or PureWindowsPath(value).drive or ".." in path.parts
            or path.as_posix() != value or value in {".", ".."}):
        return None
    return value


def build_summary(result, run_root, *, suite, context=None, environ=None):
    """Only link ordinary files that exist below this exact run root."""
    env = environ or {}
    files = {p.relative_to(run_root).as_posix() for p in ordinary_files(Path(run_root))}
    files -= {"summary.html", "summary.json", "summary.css"}
    identity = dict(context["identity"]) if context else {
        "suite": suite, "site": result["site"], "run_id": result["run_id"],
        "commit_sha": "UNKNOWN", "job_name": "LOCAL", "build_number": None,
        "viewport": ",".join(v["viewport"] for v in result.get("viewports", [])) or "UNKNOWN"}
    fatal = result.get("fatal_error")
    viewports = []
    for vp in result.get("viewports", []):
        cases = vp.get("cases", [])
        by_id = {c["case_id"]: c for c in cases}

        def chain(case_id, seen=()):
            # Scope dependencies to this viewport; cycles/missing references stay explicit.
            if case_id in seen:
                return [[*seen, case_id, "CYCLE"]]
            case = by_id.get(case_id)
            if case is None:
                return [[*seen, case_id, "MISSING_CASE"]]
            parents = case.get("blocked_by", [])
            return ([path for p in parents for path in chain(p, (*seen, case_id))]
                    if parents else [[*seen, case_id]])

        rows = []
        for case in cases:
            evidence = []
            for item in case.get("evidence", []):
                path = evidence_path(item.get("path"))
                status = "AVAILABLE" if path in files else "MISSING" if path else "REJECTED_PATH"
                evidence.append({"type": text(item.get("type")), "path": path,
                                 "status": status, "href": path if status == "AVAILABLE" else None})
            rows.append({"case_id": text(case["case_id"]), "name": text(case.get("name")),
                         "status": case["status"], "classification": text(case.get("failure_classification"))
                         if case.get("failure_classification") else "NONE",
                         "dependency_chains": chain(case["case_id"]) if case.get("blocked_by") else [],
                         "diagnostic": text(case.get("detail")) if case.get("detail") else None,
                         "diagnostic_status": "AVAILABLE" if case.get("detail") else "NOT_RECORDED",
                         "capture_error": text(case["evidence_capture_error"]) if case.get("evidence_capture_error") else None,
                         "evidence_status": "RECORDED" if evidence else "NOT_RECORDED",
                         "evidence": evidence})
        viewports.append({"viewport": vp["viewport"], "status": vp["status"],
                          "partial": bool(vp.get("partial")), "counts": dict(vp["summary"]),
                          "pre_clean": vp.get("pre_clean"), "cleanup": vp.get("cleanup"),
                          "mutation_summary": vp.get("mutation_summary"), "cases": rows})
    return {"version": 1, "identity": identity, "scope": "CI" if context else "LOCAL",
            "result_validity": "VALIDATED_CURRENT_RUN" if context else "LOCAL_UNVALIDATED",
            "phase": "PRE_ARCHIVE" if context else "SMOKE_COMPLETE",
            "last_observed_step": env.get("CI_LAST_STEP", "UNKNOWN") if context else "smoke",
            "steps": {s: env.get("CI_STEP_" + s.upper(), "NOT_RECORDED") for s in STEPS},
            "gates": {"schema": env.get("CI_SCHEMA_GATE", "NOT_RUN"),
                      "secret": env.get("CI_SECRET_GATE", "NOT_RUN")},
            "publication": "FINAL_SCAN_AND_ARCHIVE_REQUIRED" if context else "NOT_PUBLISHED",
            "jenkins_result_before_archive": env.get("CI_JENKINS_RESULT", "NOT_APPLICABLE"),
            "python_exit_code": env.get("CI_PYTHON_EXIT_CODE", "NOT_RECORDED"),
            "business_status": result["overall_status"],
            "failure_kind": "EXECUTION_FAILURE" if fatal else "CASE_OR_LIFECYCLE_FAILURE"
            if result["overall_status"] != "PASS" else "NONE",
            "fatal_error": {"classification": text(fatal.get("classification")),
                            "message": text(fatal.get("message"))} if fatal else None,
            "counts": dict(result["summary"]), "duration_ms": result.get("duration_ms"),
            "viewports": viewports, "files": sorted(files),
            "history": "SEPARATE_NOT_READ"}


def render_html(model):
    def e(value):
        return escape(text(value), quote=True)

    def link(path, label=None):
        return f'<a href="{quote(path, safe="/")}">{e(label or path)}</a>'

    def mapping(values):
        return "<dl>" + "".join(f"<dt>{e(k)}</dt><dd>{e(v)}</dd>" for k, v in values.items()) + "</dl>"

    def table(headers, rows):
        return "<table><thead><tr>" + "".join(f"<th>{e(h)}</th>" for h in headers) + "</tr></thead><tbody>" + "".join("<tr>" + "".join(f"<td>{v}</td>" for v in row) + "</tr>" for row in rows) + "</tbody></table>"

    parts = ['<!doctype html><html lang="en"><head><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width,initial-scale=1">',
             '<title>Current run summary</title><link rel="stylesheet" href="summary.css"></head><body><main>',
             f'<h1>Run summary: <span class="{e(model["business_status"])}">{e(model["business_status"])}</span></h1>',
             '<p class="notice">This report contains this run only. CI snapshots are created before final scan, archive and acknowledgement. Jenkins console/build status is authoritative for those later actions. Local reports are unvalidated. Historical trends are separate and are not read here.</p>',
             f'<p>{e(model["identity"]["site"])} / {e(model["identity"]["suite"])} / {e(model["identity"]["viewport"])} · {e(model["result_validity"])}</p>',
             '<h2>Case counts</h2>', table(list(model["counts"]), [[e(v) for v in model["counts"].values()]])]
    issues = [(vp, case) for vp in model["viewports"] for case in vp["cases"] if case["status"] != "PASS"]
    parts += ['<h2>Failures and blocked cases</h2>', table(
        ["Viewport", "Case", "Status", "Classification", "Dependencies"],
        [[e(vp["viewport"]), e(case["case_id"]), e(case["status"]), e(case["classification"]),
          '<br>'.join(e(' → '.join(c)) for c in case["dependency_chains"]) or 'NONE']
         for vp, case in issues]) if issues else '<p>No failing or blocked cases recorded. Check execution/lifecycle status above before treating this as a successful run.</p>']
    parts += ['<h2>Identity</h2>', mapping(model["identity"]),
              '<details><summary>Execution phases and gates</summary>',
              mapping({k: model[k] for k in ("scope", "result_validity", "phase", "last_observed_step", "failure_kind", "python_exit_code", "jenkins_result_before_archive", "publication")}),
              mapping(model["gates"]), mapping(model["steps"]), '</details>']
    if model["fatal_error"]:
        parts += ['<h2>Execution failure</h2>', mapping(model["fatal_error"])]
    for vp in model["viewports"]:
        parts += [f'<h2>{e(vp["viewport"])} — {e(vp["status"])}</h2>',
                  table(list(vp["counts"]), [[e(v) for v in vp["counts"].values()]]),
                  mapping({"partial": vp["partial"],
                           "pre_clean": (vp["pre_clean"] or {}).get("status", "NOT_RECORDED"),
                           "cleanup": (vp["cleanup"] or {}).get("status", "NOT_RECORDED"),
                           "cleanup_detail": (vp["cleanup"] or {}).get("detail", "NOT_RECORDED"),
                           "mutation_status": (vp["mutation_summary"] or {}).get("status", "NOT_APPLICABLE")}),
                  '<details><summary>Lifecycle / audit detail</summary><pre>' + e(json.dumps(
                      {k: vp[k] for k in ("pre_clean", "cleanup", "mutation_summary")}, ensure_ascii=False, indent=2)) + '</pre></details>']
        rows = []
        for case in vp["cases"]:
            diagnostics = '<p>Diagnostic: NOT_RECORDED</p>'
            if case["diagnostic"]:
                diagnostics = f'<details><summary>Diagnostic</summary><pre>{e(case["diagnostic"])}</pre></details>'
            evidence = "<br>".join(link(x["href"]) if x["href"] else f'{e(x["type"])}: {e(x["status"])}' for x in case["evidence"])
            evidence = evidence or 'Evidence: NOT_RECORDED'
            if case["capture_error"]:
                evidence += '<p>Capture error: ' + e(case["capture_error"]) + '</p>'
            chains = '<br>'.join(e(' → '.join(c)) for c in case["dependency_chains"]) or 'NONE'
            rows.append([e(case["case_id"]) + '<br>' + e(case["name"]),
                         f'<span class="{e(case["status"])}">{e(case["status"])}</span>',
                         e(case["classification"]), chains, diagnostics + evidence])
        parts.append(table(["Case", "Status", "Classification", "Blocked dependency chain", "Diagnostics / evidence"], rows))
    parts += ['<h2>Current run files</h2><ul>', *(f'<li>{link(p)}</li>' for p in model["files"]),
              f'<li>{link("summary.json")}</li></ul></main></body></html>']
    return "\n".join(parts)


def write_summary(result, run_root, *, suite, context=None, environ=None):
    model = build_summary(result, Path(run_root), suite=suite, context=context, environ=environ)
    write_run_json(model, run_root, "summary.json")
    write_run_text(CSS, run_root, "summary.css")
    write_run_text(render_html(model), run_root, "summary.html")
    return model
