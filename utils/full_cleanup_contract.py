"""Internal consistency of saved Full cleanup evidence (not authentication)."""
import re

STAGES = ("pre_clean", "direct", "search", "browse")
_ID = re.compile(r"[0-9a-f]{32}")


def uses_v3(value, mutation):
    reports = value.get("journeys", [])
    api = mutation.get("api_cleanup") if isinstance(mutation, dict) else None
    records = api.get("records", []) if isinstance(api, dict) else []
    return ("contract_version" in value or "progress" in value
            or (isinstance(reports, list) and any(isinstance(r, dict) and "operation_id" in r for r in reports))
            or (isinstance(records, list) and any(isinstance(r, dict) and "operation_id" in r for r in records)))


def validate_progress(value, viewport, cases, partial, fatal, overall, case_ids):
    """Constrain recorded progress to the actual ordered Full execution plan."""
    progress = value.get("progress")
    if value.get("contract_version") != 3 or not isinstance(progress, dict):
        return False
    if set(progress) != {"phase", "active_journey", "completed_journeys",
                         "pre_clean_completed", "recorded_case_ids", "final_cleanup_attempted"}:
        return False
    if (progress["recorded_case_ids"] != [c.get("case_id") for c in cases]
            or progress["final_cleanup_attempted"] is not True
            or type(progress["pre_clean_completed"]) is not bool):
        return False
    completed = progress["completed_journeys"]
    if not isinstance(completed, list) or completed not in [list(STAGES[1:1+n]) for n in range(4)]:
        return False
    phase, active = progress["phase"], progress["active_journey"]
    ids = progress["recorded_case_ids"]
    if not isinstance(ids, list) or ids != [cid for cid in case_ids if cid in ids]:
        return False
    if partial and (not isinstance(fatal, dict) or not fatal.get("classification")
                    or not fatal.get("message") or viewport.get("status") != "FAIL" or overall != "FAIL"):
        return False
    reports = value.get("journeys", [])
    if not isinstance(reports, list) or not all(isinstance(r, dict) for r in reports):
        return False
    stages = [r.get("journey") for r in reports]
    if not stages or stages[-1] != "browse" or len(stages) != len(set(stages)):
        return False
    groups = {"direct": case_ids[:3], "search": case_ids[3:7], "browse": case_ids[7:]}
    pre_done = progress["pre_clean_completed"]
    if phase == "CREATED":
        return partial and not ids and not pre_done and not completed and active is None and stages == ["browse"]
    if phase == "PRE_CLEAN":
        return (partial and not ids and not completed and active is None and not pre_done
                and stages in [["browse"], ["pre_clean", "browse"]])
    if phase == "PRE_CLEAN_FAILED":
        return (not pre_done and not completed and active is None
                and stages == ["pre_clean", "browse"]
                and (viewport.get("pre_clean") or {}).get("status") in {"FAIL", "BLOCKED"}
                and all(c.get("status") == "BLOCKED" and c.get("blocked_by") == ["pre-clean"] for c in cases))
    if not pre_done or not reports or reports[0].get("status") != "PASS":
        return False
    if phase == "COMPLETE":
        return completed == list(STAGES[1:]) and active is None and ids == list(case_ids) and stages == list(STAGES)
    if not partial or phase not in {"JOURNEY", "JOURNEY_CLEANUP"}:
        return False
    if phase == "JOURNEY":
        if len(completed) == 3 or active != STAGES[len(completed)+1]:
            return False
        prior_ids = [cid for j in completed for cid in groups[j]]
        remaining = ids[len(prior_ids):]
        if ids[:len(prior_ids)] != prior_ids or remaining != list(groups[active][:len(remaining)]):
            return False
        return stages == ["pre_clean", *completed, "browse"]
    # A fatal can occur during a reached Direct/Search cleanup before its
    # report is available. Other completed cleanups are still mandatory.
    if not completed or active != completed[-1]:
        return False
    if ids != [cid for j in completed for cid in groups[j]]:
        return False
    prior = ["pre_clean", *completed[:-1]]
    candidates = [prior + ["browse"]]
    if active != "browse":
        candidates.append(prior + [active, "browse"])
    return stages in candidates


def validate_operations(value, mutation, viewport):
    """Match each cleanup's final attempt to its saved authorization/response."""
    reports = value.get("journeys", [])
    api = mutation.get("api_cleanup") if isinstance(mutation, dict) else None
    records = api.get("records") if isinstance(api, dict) else None
    if not isinstance(records, list) or not isinstance(reports, list):
        return False
    required = {"operation_id", "attempt_id", "journey", "clear_requested", "status",
                "precheck_status", "api_policy_decision", "postcheck_status",
                "cart_item_count", "safe_error_code", "clear_response_category"}
    if not all(isinstance(r, dict) and required <= set(r) for r in reports):
        return False
    record_keys = {"operation_id", "attempt_id", "cleanup_stage", "viewport",
                   "authorization", "dispatch", "response", "http_status"}
    if not all(isinstance(r, dict) and record_keys <= set(r) for r in records):
        return False
    ids = [r.get("operation_id") for r in reports if isinstance(r, dict)]
    if (len(ids) != len(reports) or any(not isinstance(i, str) or not _ID.fullmatch(i) for i in ids)
            or len(ids) != len(set(ids))):
        return False
    for record in records:
        if not isinstance(record, dict):
            return False
        if record.get("operation_id") is None:
            # Standalone denied API checks are auditable but cannot authorize
            # or supply a response for any cleanup operation.
            if record.get("authorization") != "DENIED" or record.get("dispatch") != "NOT_ATTEMPTED":
                return False
            continue
        if record.get("operation_id") not in ids or record.get("viewport") != viewport:
            return False
    for report in reports:
        linked = [r for r in records if r.get("operation_id") == report["operation_id"]]
        if any(r.get("cleanup_stage") != report["journey"] for r in linked):
            return False
        if [r.get("attempt_id") for r in linked] != list(range(1, len(linked)+1)):
            return False
        if not linked:
            if report.get("attempt_id") is not None or report["clear_requested"]:
                return False
            if report["precheck_status"] == "BLOCKED":
                if (report["status"] != "BLOCKED" or report["api_policy_decision"] != "NOT_REQUESTED"
                        or report["postcheck_status"] != "NOT_RUN"):
                    return False
            elif report["cart_item_count"] == 0:
                if (report["status"] != "PASS" or report["api_policy_decision"] != "NOT_REQUESTED"
                        or report["postcheck_status"] != "PASS"):
                    return False
            elif not (report["status"] == "BLOCKED" and report["api_policy_decision"] == "DENIED"
                      and report["safe_error_code"] == "API_CLEANUP_POLICY_DENIED"):
                return False
            continue
        if report["precheck_status"] != "PASS" or type(report["cart_item_count"]) is not int or report["cart_item_count"] <= 0:
            return False
        last = linked[-1]
        if report.get("attempt_id") != last["attempt_id"]:
            return False
        expected_policy = "API_CLEANUP_EXPECTED_MUTATION" if last["authorization"] == "ALLOWED" else "DENIED"
        if report["api_policy_decision"] != expected_policy:
            return False
        attempted = last["dispatch"] == "ATTEMPTED"
        if report["clear_requested"] is not attempted:
            return False
        if not attempted:
            if (report["status"] != "BLOCKED" or report["postcheck_status"] != "NOT_RUN"
                    or report["clear_response_category"] != "NOT_REQUESTED"):
                return False
            continue
        if last["response"] == "OUTCOME_UNKNOWN":
            if (report["status"] != "FAIL" or report["clear_response_category"] != "REQUEST_FAILED"
                    or report["postcheck_status"] != "NOT_RUN"):
                return False
            continue
        if last["response"] != "RESPONSE_RECEIVED":
            return False
        response = report["clear_response_category"]
        if response == "HTTP_OR_REDIRECT_FAILURE":
            if report["status"] != "FAIL" or report["postcheck_status"] != "NOT_RUN":
                return False
        elif response == "HTTP_200" and last["http_status"] == 200:
            postcheck = report["postcheck_status"]
            if report["status"] != {"PASS": "PASS", "FAIL": "FAIL", "BLOCKED": "BLOCKED"}.get(postcheck):
                return False
        else:
            return False
    return True
