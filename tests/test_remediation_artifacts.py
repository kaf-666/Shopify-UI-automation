"""Evidence writes and fatal fallback keep the run boundary and identity."""
import json
import os
import subprocess

import pytest

from utils.result import ResultWriteError
from utils.safe_artifact_write import write_run_bytes, write_run_text
from utils.screenshots import capture_case_failure
from utils.stability import contains_forbidden_marker
from utils.suite_runner import guarded_main, register_fatal_context


def test_screenshot_bytes_and_viewport_fallback_are_confined(tmp_path):
    calls = []
    class Page:
        def screenshot(self, *, full_page):
            calls.append(full_page)
            if full_page:
                raise RuntimeError("synthetic page too large")
            return b"synthetic png"
    rel = capture_case_failure(Page(), tmp_path, "desktop", "WSMOKE-TEST-01")
    assert rel == "desktop/WSMOKE-TEST-01-failure.png"
    assert (tmp_path / rel).read_bytes() == b"synthetic png"
    assert calls == [True, False]
    assert capture_case_failure(Page(), tmp_path, "../outside", "WSMOKE-TEST-01") is None
    assert not (tmp_path.parent / "outside").exists()


@pytest.mark.parametrize("relative", ["../outside.png", "desktop/x.png", "desktop/../../../outside.png",
                                      "C:/outside.png", "desktop\\WSMOKE-TEST-01-failure.png"])
def test_arbitrary_binary_paths_are_rejected(tmp_path, relative):
    with pytest.raises(ResultWriteError, match="ARTIFACT_WRITE_REJECTED"):
        write_run_bytes(b"x", tmp_path, relative)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("directory,write", [("desktop", "screenshot"), ("traffic", "text")])
def test_evidence_and_traffic_reject_linked_parent(tmp_path, directory, write):
    run, outside = tmp_path / "run", tmp_path / "outside"
    run.mkdir(); outside.mkdir()
    link = run / directory
    if os.name == "nt":
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
        assert result.returncode == 0
    else:
        link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ResultWriteError, match="ARTIFACT_WRITE_REJECTED"):
        if write == "screenshot":
            write_run_bytes(b"x", run, "desktop/WSMOKE-TEST-01-failure.png")
        else:
            write_run_text("x", run, "traffic/requests.jsonl")
    assert not list(outside.iterdir())


@pytest.mark.parametrize("site", ["MONDRESSY_US", "LAVETIR_US", "SHIREES_US", "FUTURE_SITE"])
@pytest.mark.parametrize("suffix", ["", "_INPUT", "_AGENT"])
def test_all_site_credential_bindings_are_rejected_without_environment_values(site, suffix):
    assert contains_forbidden_marker({"detail": f"{site}_SHOPIFY_SIGNATURE{suffix}=synthetic"}) == "SHOPIFY_SIGNATURE_BINDING"
    assert contains_forbidden_marker({f"{site}_SHOPIFY_SIGNATURE{suffix}": "synthetic"}) == "SHOPIFY_SIGNATURE_BINDING"


def test_generic_binding_and_missing_variable_diagnostics():
    assert contains_forbidden_marker({"SHOPIFY_SIGNATURE": "synthetic"}) == "SHOPIFY_SIGNATURE_BINDING"
    assert contains_forbidden_marker({"detail": "missing SHIREES_US_SHOPIFY_SIGNATURE"}) is None


@pytest.mark.parametrize("known", [True, False])
def test_fatal_fallback_uses_known_identity_or_separate_infrastructure(tmp_path, known):
    root = tmp_path / "artifacts"
    root.mkdir()
    actual = root / "lavetir" / "actual-run"
    def main(_argv):
        if known:
            actual.mkdir(parents=True)
            register_fatal_context(artifact_dir=actual, run_id="actual-run", site="lavetir",
                                   suite="website_smoke_v1", viewports=[])
        raise RuntimeError("synthetic fatal")
    # Supply only the CLI's configured root, without mutating this test module.
    from types import FunctionType
    wrapped = FunctionType(main.__code__, {**main.__globals__, "ARTIFACT_ROOT": root}, closure=main.__closure__)
    assert guarded_main(wrapped, []) == 1
    paths = list(root.rglob("results.json"))
    assert len(paths) == 1
    result = json.loads(paths[0].read_text(encoding="utf-8"))
    assert result["overall_status"] == "FAIL" and result["viewports"] == []
    assert result["site"] == ("lavetir" if known else "")
    assert paths[0].parent == actual if known else paths[0].parent.parent == root / "infrastructure"
    assert (paths[0].parent / "summary.html").is_file()
