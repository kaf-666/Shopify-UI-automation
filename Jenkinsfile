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

    options {
        skipDefaultCheckout(true)
        timestamps()
        timeout(time: 60, unit: 'MINUTES')
        buildDiscarder(logRotator(numToKeepStr: '25', artifactNumToKeepStr: '25'))
    }

    parameters {
        choice(
            name: 'SMOKE_VIEWPORT',
            choices: ['both', 'desktop', 'mobile'],
            description: 'Run one Website Smoke V1 target. Use both for the formal gate.'
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
                checkout scm
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
                        if (isUnix()) {
                            sh ".venv/bin/python scripts/run_website_smoke_v1.py --viewport ${params.SMOKE_VIEWPORT}"
                        } else {
                            bat ".venv\\Scripts\\python.exe scripts\\run_website_smoke_v1.py --viewport ${params.SMOKE_VIEWPORT}"
                        }
                    }
                }
            }
        }

        stage('Result Validation') {
            steps {
                script {
                    if (isUnix()) {
                        sh '.venv/bin/python scripts/validate_result_schema.py --suite website_smoke_v1'
                    } else {
                        bat '.venv\\Scripts\\python.exe scripts\\validate_result_schema.py --suite website_smoke_v1'
                    }
                }
            }
        }
    }

    post {
        always {
            archiveArtifacts artifacts: 'artifacts/**', allowEmptyArchive: true, fingerprint: false
        }
    }
}
