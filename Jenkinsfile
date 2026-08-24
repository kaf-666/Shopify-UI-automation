/*
 * Jenkins orchestration only.
 *
 * The Python runners own business logic, exit codes, results.json and evidence.
 * This pipeline only prepares the agent, binds credentials, runs gates, and
 * archives artifacts. It intentionally runs one Website Smoke V1 command per
 * build; the default/final gate is --viewport both.
 */
pipeline {
    agent any

    triggers {
        // Hashed minute avoids synchronized starts when more sites are added.
        cron('H */3 * * *')
    }

    options {
        skipDefaultCheckout(true)
        disableConcurrentBuilds()
        timestamps()
        timeout(time: 60, unit: 'MINUTES')
        buildDiscarder(logRotator(numToKeepStr: '25', artifactNumToKeepStr: '25'))
    }

    parameters {
        choice(
            name: 'SMOKE_VIEWPORT',
            choices: ['both', 'desktop', 'mobile'],
            description: 'Run one Website Smoke V1 target. Default/scheduled target is both.'
        )
    }

    environment {
        // These IDs are Jenkins credential IDs, never credential values.
        // The credentials are Secret Text entries in the global store.
        MONDRESSY_US_SHOPIFY_SIGNATURE = credentials('MONDRESSY_US_SHOPIFY_SIGNATURE')
        MONDRESSY_US_SHOPIFY_SIGNATURE_INPUT = credentials('MONDRESSY_US_SHOPIFY_SIGNATURE_INPUT')
        MONDRESSY_US_SHOPIFY_SIGNATURE_AGENT = credentials('MONDRESSY_US_SHOPIFY_SIGNATURE_AGENT')
    }

    stages {
        stage('Checkout') {
            steps {
                script {
                    // These values must remain mutable. Declarative environment
                    // entries cannot be reliably overridden with env.* later.
                    env.STABILITY_SECRET_GATE = 'NOT_RUN'
                    env.STABILITY_SCHEMA_GATE = 'NOT_RUN'
                    env.STABILITY_PYTHON_EXIT_CODE = 'UNKNOWN'

                    def scmVars = checkout scm
                    def checkoutSha = "${scmVars?.GIT_COMMIT ?: ''}".trim()
                    if (!checkoutSha) {
                        if (isUnix()) {
                            checkoutSha = sh(
                                returnStdout: true,
                                script: 'git rev-parse HEAD'
                            ).trim()
                        } else {
                            checkoutSha = bat(
                                returnStdout: true,
                                script: '@git rev-parse HEAD'
                            ).trim()
                        }
                    }
                    if (!checkoutSha) {
                        error('Unable to determine the checked-out workspace HEAD SHA')
                    }
                    env.GIT_COMMIT_SHA = checkoutSha
                    echo "Checkout SHA: ${checkoutSha}"
                }
            }
        }

        stage('Environment') {
            steps {
                script {
                    echo 'Signed Request: configured by Jenkins Credentials Binding (values redacted)'
                    echo env.SHOPIFY_PROXY_SERVER?.trim() ?
                        'Proxy: configured through the Jenkins environment (server value redacted)' :
                        'Proxy: direct mode; SHOPIFY_PROXY_SERVER is not configured'
                    if (isUnix()) {
                        env.SYSTEM_PYTHON = sh(
                            returnStdout: true,
                            script: '''
                                if command -v python3 >/dev/null 2>&1; then
                                    command -v python3
                                elif command -v python >/dev/null 2>&1; then
                                    command -v python
                                else
                                    echo "No Python interpreter found on this Jenkins Agent" >&2
                                    exit 127
                                fi
                            '''
                        ).trim()
                        env.PYTHON_BIN = '.venv/bin/python'
                        sh "${env.SYSTEM_PYTHON} --version"
                    } else {
                        env.SYSTEM_PYTHON = 'python'
                        env.PYTHON_BIN = '.venv\\Scripts\\python.exe'
                        bat 'where python'
                        bat 'python --version'
                    }
                }
            }
        }

        stage('Install Dependencies') {
            steps {
                script {
                    if (isUnix()) {
                        sh "${env.SYSTEM_PYTHON} -m venv .venv"
                        sh '.venv/bin/python -m pip install --no-input -r requirements.lock.txt'
                    } else {
                        bat 'python -m venv .venv'
                        bat '.venv\\Scripts\\python.exe -m pip install --no-input -r requirements.lock.txt'
                    }
                }
            }
        }

        stage('Install Playwright Browsers') {
            steps {
                script {
                    if (isUnix()) {
                        sh '.venv/bin/python -m playwright install chromium webkit'
                    } else {
                        bat '.venv\\Scripts\\python.exe -m playwright install chromium webkit'
                    }
                }
            }
        }

        stage('Static Validation') {
            steps {
                script {
                    if (isUnix()) {
                        sh '.venv/bin/python -m compileall .'
                    } else {
                        bat '.venv\\Scripts\\python.exe -m compileall .'
                    }
                }
            }
        }

        stage('Runtime Contract') {
            steps {
                script {
                    if (isUnix()) {
                        sh '.venv/bin/python scripts/validate_runtime_contract.py'
                    } else {
                        bat '.venv\\Scripts\\python.exe scripts\\validate_runtime_contract.py'
                    }
                }
            }
        }

        stage('Signed Request / Site Access') {
            steps {
                script {
                    if (isUnix()) {
                        sh '.venv/bin/python scripts/validate_site_access.py --viewport both'
                    } else {
                        bat '.venv\\Scripts\\python.exe scripts\\validate_site_access.py --viewport both'
                    }
                }
            }
        }

        stage('Website Smoke V1') {
            steps {
                script {
                    // Keep the build failed on Exit 1/2 while allowing result
                    // validation and post(always) artifact archiving to run.
                    catchError(buildResult: 'FAILURE', stageResult: 'FAILURE') {
                        int smokeExitCode
                        if (isUnix()) {
                            smokeExitCode = sh(
                                returnStatus: true,
                                script: ".venv/bin/python scripts/run_website_smoke_v1.py --viewport ${params.SMOKE_VIEWPORT}"
                            )
                        } else {
                            smokeExitCode = bat(
                                returnStatus: true,
                                script: ".venv\\Scripts\\python.exe scripts\\run_website_smoke_v1.py --viewport ${params.SMOKE_VIEWPORT}"
                            )
                        }
                        env.STABILITY_PYTHON_EXIT_CODE = "${smokeExitCode}"
                        if (smokeExitCode != 0) {
                            error("Website Smoke V1 exited with code ${smokeExitCode}")
                        }
                    }
                }
            }
        }

        stage('Secret Leakage Check') {
            steps {
                script {
                    int gateExitCode
                    if (isUnix()) {
                        gateExitCode = sh(
                            returnStatus: true,
                            script: '.venv/bin/python scripts/validate_ci_safe_outputs.py'
                        )
                    } else {
                        gateExitCode = bat(
                            returnStatus: true,
                            script: '.venv\\Scripts\\python.exe scripts\\validate_ci_safe_outputs.py'
                        )
                    }
                    env.STABILITY_SECRET_GATE = gateExitCode == 0 ? 'PASS' : 'FAIL'
                    if (gateExitCode != 0) {
                        error("Secret Leakage Check exited with code ${gateExitCode}")
                    }
                }
            }
        }

        stage('Result Validation') {
            steps {
                script {
                    int gateExitCode
                    if (isUnix()) {
                        gateExitCode = sh(
                            returnStatus: true,
                            script: '.venv/bin/python scripts/validate_result_schema.py --suite website_smoke_v1'
                        )
                    } else {
                        gateExitCode = bat(
                            returnStatus: true,
                            script: '.venv\\Scripts\\python.exe scripts\\validate_result_schema.py --suite website_smoke_v1'
                        )
                    }
                    env.STABILITY_SCHEMA_GATE = gateExitCode == 0 ? 'PASS' : 'FAIL'
                    if (gateExitCode != 0) {
                        error("Result Validation exited with code ${gateExitCode}")
                    }
                }
            }
        }
    }

    post {
        always {
            script {
                // Use only the safe, supported causes API. If a Jenkins
                // installation does not expose it, the record uses UNKNOWN.
                try {
                    def causeText = "${currentBuild.getBuildCauses()}"
                    if (causeText.contains('TimerTriggerCause')) {
                        env.STABILITY_TRIGGER = 'TIMER'
                    } else if (causeText.contains('SCMTriggerCause')) {
                        env.STABILITY_TRIGGER = 'SCM'
                    } else if (causeText.contains('UserIdCause')) {
                        env.STABILITY_TRIGGER = 'MANUAL'
                    } else {
                        env.STABILITY_TRIGGER = 'OTHER'
                    }
                } catch (ignored) {
                    env.STABILITY_TRIGGER = 'UNKNOWN'
                }
                env.STABILITY_JENKINS_RESULT = currentBuild.currentResult ?: 'UNKNOWN'
                // Stability collection is observational. A collection error
                // must not change the functional build result or exit contract.
                catchError(buildResult: 'SUCCESS', stageResult: 'UNSTABLE') {
                    def commitSha = env.GIT_COMMIT_SHA ?: ''
                    def schemaGate = env.STABILITY_SCHEMA_GATE ?: 'NOT_RUN'
                    def secretGate = env.STABILITY_SECRET_GATE ?: 'NOT_RUN'
                    if (isUnix()) {
                        sh ".venv/bin/python scripts/record_stability.py --commit-sha \"${commitSha}\" --schema-gate \"${schemaGate}\" --secret-gate \"${secretGate}\""
                    } else {
                        bat ".venv\\Scripts\\python.exe scripts\\record_stability.py --commit-sha \"${commitSha}\" --schema-gate \"${schemaGate}\" --secret-gate \"${secretGate}\""
                    }
                }
            }
            archiveArtifacts artifacts: 'artifacts/**', allowEmptyArchive: true, fingerprint: false
        }
    }
}
