#!groovy

// Use "node {...}" to use any Jenkins server, or "node('master') {...}" to
// only run on the master node.
node {
    // You can hardcode the settings here, or have it dynamically figured out
    // in the build step.
    def djangoSettings = "maykintest.settings_test"
    def curDir = pwd()
    def envDir = "${curDir}/env"

    stage ("Build") {
        // Submit to Chucks will
	chuckNorris()
        cleanWs()
        // Use the clean option that fits best in the project.
        // Clean build when changing target
        if (env.CHANGE_TARGET) {
            // Clean workspace
            // cleanWs()

            // Clean virtual environment
            dir("env") {
                deleteDir()
            }
        }
        // Clean build when the previous build failed.
        // cleanWs cleanWhenNotBuilt: false, cleanWhenSuccess: false, notFailBuild: true

        def installed = fileExists "${envDir}/bin/activate"

        checkout scm

        // Hard way of determining the Django settings path.
	// Might break if you have multiple directories in src

        if (!installed) {
            sh "virtualenv ${envDir} -p python3"
        }
    }

    stage ("Install backend requirements") {
        sh """
            . ${envDir}/bin/activate
            pip install -r requirements.txt
            deactivate
          """
    }

    stage ("Test backend") {
        def testsError = null
        def keepDbOption = ""

        if (!env.CHANGE_TARGET) {
            keepDbOption = "--keepdb"
        }

        withEnv(["SECRET_KEY=test_key","ELASTIC_APM_DISABLE_SEND=true"]) {
            try {
                sh """
                    . ${envDir}/bin/activate
                    python manage.py jenkins \
                        --project-apps-tests \
                        --verbosity 2 \
                        --noinput \
                        --pep8-rcfile=.pep8 \
                        --coverage-rcfile=.coveragerc \
                        ${keepDbOption} \
                        --enable-coverage \
                        --settings=${djangoSettings}
                    deactivate
                """
            }
            catch(err) {
                testsError = err
                currentBuild.result = "FAILURE"
            }
            finally {
                dir("media") {
                    deleteDir()
                }
                junit "reports/junit.xml"

                if (testsError) {
                    throw testsError
                }
            }
        }

        withEnv(["SECRET_KEY=test_key"]) {
            try {
                sh "${envDir}/bin/isort --recursive --check-only --diff --quiet src > reports/isort.report"
            }
            catch(err) {
                // Nothing...
            }
        }
    }

    stage ("Quality") {
        step(
            [
                $class: "CoberturaPublisher",
                coberturaReportFile: "reports/coverage.xml"
            ]
        )
        step(
            [
                $class: "WarningsPublisher",
                parserConfigurations: [
                    [
                        parserName: "PyLint",
                        pattern: "reports/pylint.report",
                        unstableTotalAll: "10",
                        usePreviousBuildAsReference: true,
                    ],
                    [
                        parserName: "Pep8",
                        pattern: "reports/pep8.report",
                        unstableTotalAll: "50",
                        usePreviousBuildAsReference: true,
                    ],
                    [
                        parserName: "Dynamic",
                        pattern: "reports/isort.report",
                        unstableTotalAll: "10",
                        usePreviousBuildAsReference: true,
                    ],
                ]
            ]
        )
    }

// Enable for SonarQube
//  stage("Analysis") {
//    def scannerHome = tool "SonarQube Scanner 2.8";
//    withSonarQubeEnv("Jenkins Scanner") {
//      sh "${scannerHome}/bin/sonar-scanner"
//    }
//  }
}
