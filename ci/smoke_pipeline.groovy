// Repository-local Jenkins adapter. Python owns the smoke/publication protocol.
// Loaded only after checkout; no plugin or external Shared Library is required.
def checkoutIdentity(Map scmVars) {
    def checkoutSha = "${scmVars?.GIT_COMMIT ?: ''}".trim()
    if (!checkoutSha) {
        checkoutSha = isUnix() ? sh(returnStdout: true, script: 'git rev-parse HEAD').trim() :
            bat(returnStdout: true, script: '@git rev-parse HEAD').trim()
    }
    if (!checkoutSha) { error('Unable to determine checked-out workspace HEAD SHA') }
    env.GIT_COMMIT_SHA = checkoutSha
    echo "Checkout SHA: ${checkoutSha}"
}

def initialize() {
    env.CI_VIEWPORT = "${params.SMOKE_VIEWPORT}"
    env.CI_CONTEXT_PATH = "artifacts/ci/${env.CI_RUN_ID}/run_context.json"
    env.CI_PYTHON_EXIT_CODE = 'UNKNOWN'
    env.CI_SECRET_GATE = 'NOT_RUN'
    env.CI_SCHEMA_GATE = 'NOT_RUN'
    def roots = [website_smoke_v1: 'artifacts/website-smoke-v1',
                 website_smoke_readonly_v1: 'artifacts/website-smoke-readonly-v1']
    if (!(env.CI_SITE in ['mondressy', 'lavetir', 'shirees']) ||
        !roots.containsKey(env.CI_SUITE) ||
        !(env.CI_VIEWPORT in ['both', 'desktop', 'mobile'])) {
        error('Invalid fixed CI target or viewport')
    }
    env.CI_RESULT_PATH = "${roots[env.CI_SUITE]}/${env.CI_SITE}/${env.CI_RUN_ID}/results.json"
}

def environmentSetup() {
    echo "Site: ${env.CI_SITE}; suite: ${env.CI_SUITE}; viewport: ${env.CI_VIEWPORT}"
    echo 'Signed Request credentials: configured (values redacted)'
    echo env.SHOPIFY_PROXY_SERVER?.trim() ? 'Proxy: configured' : 'Proxy: direct mode'
    if (isUnix()) {
        env.SYSTEM_PYTHON = sh(returnStdout: true, script: '''
            if command -v python3 >/dev/null 2>&1; then command -v python3
            elif command -v python >/dev/null 2>&1; then command -v python
            else echo "No Python interpreter found" >&2; exit 127; fi
        ''').trim()
        sh '"$SYSTEM_PYTHON" --version'
    } else {
        bat '@where python'
        bat '@python --version'
    }
}

def pythonCommand(String arguments, boolean status = false) {
    // Arguments are fixed repository step names, never user-supplied shell text.
    if (isUnix()) {
        return sh(returnStatus: status, script: ".venv/bin/python ${arguments}")
    }
    return bat(returnStatus: status, script: "@.venv\\Scripts\\python.exe ${arguments}")
}

def installDependencies() {
    if (isUnix()) { sh '"$SYSTEM_PYTHON" -m venv .venv' }
    else { bat '@python -m venv .venv' }
    pythonCommand('-m pip install --no-input -r requirements.lock.txt')
}

def installBrowsers() { pythonCommand('-m playwright install chromium webkit') }

def invoke(String action) {
    if (!(action in ['prepare', 'static', 'runtime', 'access', 'variant-diagnostic',
                     'smoke', 'secret', 'schema', 'publish', 'acknowledge'])) {
        error('Unsupported CI action')
    }
    return pythonCommand("scripts/run_smoke_ci.py --step ${action}", true)
}

def checkedStep(String action) {
    int code = invoke(action)
    if (action == 'smoke') {
        env.CI_PYTHON_EXIT_CODE = "${code}"
        if (env.CI_SUITE == 'website_smoke_readonly_v1') {
            env.READONLY_PYTHON_EXIT_CODE = "${code}"
        } else { env.STABILITY_PYTHON_EXIT_CODE = "${code}" }
    }
    if (action == 'secret') { env.CI_SECRET_GATE = code == 0 ? 'PASS' : 'FAIL' }
    if (action == 'schema') { env.CI_SCHEMA_GATE = code == 0 ? 'PASS' : 'FAIL' }
    if (code != 0) { error("CI ${action} exited with code ${code}") }
}

def runStep(String action) {
    if (action == 'smoke') {
        catchError(buildResult: 'FAILURE', stageResult: 'FAILURE') { checkedStep(action) }
    } else { checkedStep(action) }
}

def publish() {
    try {
        def causes = "${currentBuild.getBuildCauses()}"
        env.CI_TRIGGER = causes.contains('TimerTriggerCause') ? 'TIMER' :
            causes.contains('SCMTriggerCause') ? 'SCM' :
            causes.contains('UserIdCause') ? 'MANUAL' : 'OTHER'
    } catch (ignored) { env.CI_TRIGGER = 'UNKNOWN' }
    env.CI_JENKINS_RESULT = currentBuild.currentResult ?: 'FAILURE'
    try {
        int publishExit = invoke('publish')
        if (publishExit != 0) { error('Current invocation publication rejected') }
        // Only the frozen, completely scanned snapshot can cross this boundary.
        archiveArtifacts artifacts: "artifacts/ci/${env.CI_RUN_ID}/publish/**", allowEmptyArchive: false, fingerprint: false
        int acknowledgeExit = invoke('acknowledge')
        if (acknowledgeExit != 0) { error('Current invocation archive acknowledgement failed') }
    } catch (ignored) {
        currentBuild.result = 'FAILURE'
        def failureCode = !env.CI_RESULT_PATH || !fileExists(env.CI_RESULT_PATH) ?
            'CURRENT_RESULTS_MISSING' : env.CI_SECRET_GATE != 'PASS' ?
            'CURRENT_SECRET_SCAN_NOT_PASSED' : env.CI_SCHEMA_GATE != 'PASS' ?
            'CURRENT_SCHEMA_NOT_PASSED' : 'CURRENT_RUN_PUBLICATION_FAILED'
        dir("artifacts/ci/${env.CI_RUN_ID}") {
            writeFile file: 'publication-failure.json', text: '{"status":"FAIL","classification":"' + failureCode + '"}\n'
        }
        archiveArtifacts artifacts: "artifacts/ci/${env.CI_RUN_ID}/publication-failure.json", allowEmptyArchive: false, fingerprint: false
    }
}

return this
