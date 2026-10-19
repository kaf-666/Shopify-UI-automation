"""Summaries must preserve failures, provenance, links and publication boundaries."""
from html.parser import HTMLParser
import json
from pathlib import Path
import time
from urllib.parse import unquote, urlsplit

import pytest

from scripts import finalize_ci_run as publication
from tests.test_ci_run_context import prepare, result_for, save_current, publish, acknowledge, _refresh_summaries
from utils import run_context as runs
from utils.result import ResultWriteError
from utils.run_summary import build_summary, render_html, write_summary
from utils.safe_artifact_write import write_run_text


class Links(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.links = []
        self.tags = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.links.extend(v for k, v in attrs if k in {"href", "src"})


@pytest.mark.parametrize("suite", list(runs.SUITE_ROOTS))
def test_ci_summary_is_frozen_current_run_and_all_links_resolve(monkeypatch, tmp_path, suite):
    context = prepare(monkeypatch, tmp_path, suite=suite)
    root = save_current(context, tmp_path)
    (root / 'summary.json').write_text('{"stale":"FAKE PASS"}')
    (root / 'summary.html').write_text('FAKE PASS')
    (root.parent / 'stability-history.jsonl').write_text('HISTORY MUST NOT BE READ\n')
    assert publish(context) == 0
    snapshot = tmp_path / context['publish_dir']
    target = snapshot / context['identity']['run_id']
    model = json.loads((target / 'summary.json').read_text(encoding="utf-8"))
    assert model['identity'] == context['identity']
    assert model['result_validity'] == 'VALIDATED_CURRENT_RUN'
    assert model['counts']['total'] == (30 if suite == 'website_smoke_v1' else 22)
    assert model['gates'] == {'schema': 'PASS', 'secret': 'PASS'}
    assert model['phase'] == 'PRE_ARCHIVE'
    assert model['publication'] == 'FINAL_SCAN_AND_ARCHIVE_REQUIRED'
    assert model['history'] == 'SEPARATE_NOT_READ'
    html = (target / 'summary.html').read_text(encoding="utf-8")
    assert 'FAKE PASS' not in html and 'HISTORY MUST' not in html
    for href in Links(html).links:
        assert not urlsplit(href).scheme and not href.startswith('/')
        linked = (target / unquote(href)).resolve()
        assert linked.is_relative_to(target.resolve()) and linked.is_file()
    manifest = json.loads((snapshot / 'publication.json').read_text(encoding="utf-8"))
    assert {context['identity']['run_id'] + '/summary.' + suffix for suffix in ('html', 'json', 'css')} <= set(manifest['files'])


def test_failures_blocked_chains_capture_errors_and_partial_fatal_are_distinct(monkeypatch, tmp_path):
    context = prepare(monkeypatch, tmp_path)
    data = result_for(context)
    vp = data['viewports'][0]
    first, second, third = vp['cases'][:3]
    first.update(status='FAIL', failure_classification='PURCHASE_AREA_FAILURE', detail='size readiness budget exhausted', evidence_capture_error='SCREENSHOT_CAPTURE_FAILED')
    second.update(status='BLOCKED', blocked_by=[first['case_id']])
    third.update(status='BLOCKED', blocked_by=[second['case_id']])
    _refresh_summaries(data)
    root = save_current(context, tmp_path, data)
    assert publish(context, '1') == 0
    target = tmp_path / context['publish_dir'] / context['identity']['run_id']
    model = json.loads((target / 'summary.json').read_text(encoding="utf-8"))
    assert model['business_status'] == 'FAIL'
    assert model['failure_kind'] == 'CASE_OR_LIFECYCLE_FAILURE'
    cases = model['viewports'][0]['cases']
    assert cases[0]['capture_error'] == 'SCREENSHOT_CAPTURE_FAILED'
    assert cases[0]['evidence_status'] == 'NOT_RECORDED'
    assert cases[2]['dependency_chains'] == [[third['case_id'], second['case_id'], first['case_id']]]
    data['fatal_error'] = {'classification': 'RUNTIME_ERROR', 'message': 'browser stopped'}
    data['viewports'][0]['partial'] = True
    partial = build_summary(data, root, suite=context['identity']['suite'])
    assert partial['failure_kind'] == 'EXECUTION_FAILURE'
    assert partial['viewports'][0]['partial'] is True
    assert partial['fatal_error']['classification'] == 'RUNTIME_ERROR'


@pytest.mark.parametrize('path,status', [('../old/evidence.png','REJECTED_PATH'), ('https://outside/x','REJECTED_PATH'), ('/outside','REJECTED_PATH'), ('C:/outside','REJECTED_PATH'), ('desktop/missing.png','MISSING')])
def test_missing_or_escaping_evidence_is_explicit_and_never_linked(monkeypatch, tmp_path, path, status):
    context = prepare(monkeypatch, tmp_path)
    data = result_for(context)
    data['viewports'][0]['cases'][0].update(evidence=[{'type':'screenshot','path':path}], detail='')
    root = save_current(context, tmp_path)
    model = build_summary(data, root, suite=context['identity']['suite'])
    case = model['viewports'][0]['cases'][0]
    assert case['diagnostic_status'] == 'NOT_RECORDED'
    assert case['evidence'][0]['status'] == status
    assert case['evidence'][0]['href'] is None
    assert all('outside' not in href and 'missing.png' not in href for href in Links(render_html(model)).links)


def test_html_escapes_diagnostics_and_quotes_literal_filename(monkeypatch, tmp_path):
    context = prepare(monkeypatch, tmp_path)
    data = result_for(context)
    data['viewports'][0]['cases'][0]['detail'] = '<script>alert("unsafe")</script>'
    root = save_current(context, tmp_path)
    filename = 'evidence #1 &.png'
    (root / filename).write_bytes(b'fake image')
    model = write_summary(data, root, suite=context['identity']['suite'])
    html = (root / 'summary.html').read_text(encoding="utf-8")
    assert 'script' not in Links(html).tags
    assert '&lt;script&gt;' in html
    assert 'evidence%20%231%20%26.png' in Links(html).links
    assert model['scope'] == 'LOCAL' and model['result_validity'] == 'LOCAL_UNVALIDATED'


def test_summary_output_is_part_of_final_secret_scan(monkeypatch, tmp_path):
    context = prepare(monkeypatch, tmp_path)
    root = save_current(context, tmp_path)
    monkeypatch.setenv('SHOPIFY_PROXY_PASSWORD','PHASE5_SCAN_MARKER')
    real = publication.write_summary
    def injected(*args, **kwargs):
        model = real(*args, **kwargs)
        (Path(args[1]) / 'summary.html').write_text('PHASE5_SCAN_MARKER')
        return model
    monkeypatch.setattr(publication, 'write_summary', injected)
    assert publish(context) == 1
    assert acknowledge(context) == 1
    assert not (root.parent / 'stability-history.jsonl').exists()


def test_summary_write_failure_prevents_ready_snapshot(monkeypatch, tmp_path):
    context = prepare(monkeypatch, tmp_path)
    save_current(context, tmp_path)
    def fail(*_a, **_k):
        raise ResultWriteError('SIMULATED_SUMMARY_FAILURE')
    monkeypatch.setattr(publication, 'write_summary', fail)
    assert publish(context) == 1
    assert acknowledge(context) == 1
    assert not (tmp_path / context['publish_dir'] / 'publication.json').exists()


@pytest.mark.parametrize('target', ['results.json','../summary.html','other.html'])
def test_text_writer_keeps_confined_allowlist(tmp_path, target):
    with pytest.raises(ResultWriteError):
        write_run_text('text',tmp_path,target)


@pytest.mark.parametrize('suite', list(runs.SUITE_ROOTS))
def test_local_fatal_cli_writes_execution_failure_without_inventing_cases(tmp_path, suite):
    from scripts import run_website_smoke_v1 as full, run_website_smoke_readonly_v1 as readonly
    cli = full if suite == 'website_smoke_v1' else readonly
    assert cli._write_run_result(tmp_path, 'local-fatal', 'mondressy', 'https://mondressy.com',
                                 '2026-10-19T00:00:00+00:00', time.perf_counter(), [],
                                 fatal_error={'classification':'RUNTIME_ERROR','message':'browser stopped'})
    model = json.loads((tmp_path / 'summary.json').read_text(encoding='utf-8'))
    assert model['business_status'] == 'FAIL'
    assert model['failure_kind'] == 'EXECUTION_FAILURE'
    assert model['counts']['total'] == 0 and model['viewports'] == []
