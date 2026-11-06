/* Deprecated entry. Use the fixed site Full/Readonly entries and shared CI protocol. */
pipeline {
    agent none
    options {
        skipDefaultCheckout(true)
    }
    stages {
        stage('Deprecated Entry') {
            steps {
                error('LEGACY_PIPELINE_DISABLED: use Jenkinsfile.full[.lavetir|.shirees] or Jenkinsfile.readonly[.lavetir|.shirees]')
            }
        }
    }
}
