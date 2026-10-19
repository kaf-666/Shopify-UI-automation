"""Lavetir-specific scheduler and optional diagnostic remain explicit."""
from tests.test_jenkins_pipeline_split import entry


def test_lavetir_readonly_keeps_q4h_schedule_and_optional_diagnostic():
    text = entry("Jenkinsfile.readonly.lavetir")
    assert "cron('H H/4 * * *')" in text
    assert text.count("cron(") == 1
    assert "name: 'VARIANT_DIAGNOSTIC'" in text and "defaultValue: false" in text
    assert "expression { params.VARIANT_DIAGNOSTIC == true }" in text
    assert "catchError(buildResult: 'UNSTABLE', stageResult: 'UNSTABLE')" in text
    assert "smokeCi.runStep('variant-diagnostic')" in text
    assert text.index("stage('Variant Stability Diagnostic')") < text.index("stage('Website Smoke Readonly V1')")
    assert "stage('Site Access')" in text
