# Offline framework CI

Pipeline: `Jenkinsfile.framework-ci`.

Job: [Framework - Offline Regression](https://testing.lightingrid.com/job/Monitoring/job/Shopify/job/UI%20automation/job/Framework%20-%20Offline%20Regression/).

The GitHub Branch Source discovers repository branches without open PRs, and
origin PRs merged with their target branch. Fork discovery is disabled. Jenkins
deduplicates SCM revisions; each child job aborts its previous running build
when a new build starts. Test execution has no storefront credentials.

The repository webhook sends push and pull_request events to
`https://testing.lightingrid.com/github-webhook/` with TLS verification enabled.
Its initial ping was confirmed with HTTP 200 on 2026-10-15. Events drive GitHub
Branch Source discovery; there is no duplicate per-branch polling schedule.

Every build checks out a clean workspace, creates `.venv-ci`, installs the
version-pinned runtime/test lock and Chromium/WebKit, then invokes
`scripts/run_framework_ci.py` with a unique build report directory. Browser
system libraries must already be available on the agent, as for the existing
smoke jobs. The validated targets are Windows Python 3.10 and Jenkins Linux
Python 3.13. Network is needed to install dependencies and browser binaries;
checks use synthetic HTML, mocks and loopback HTTP servers.

The runner executes compilation, all pytest tests, Runtime Contract and
stability validation even when an earlier check fails. It preserves their exit
codes in `summary.json`. JUnit and logs are sanitized and scanned before Jenkins
publishes them. Only the current `artifacts/framework-ci/build-*/` directory is
archived. Report/scan infrastructure errors publish a fixed error JSON instead.
Missing or empty JUnit cannot produce a successful build. Jenkins exposes test
failures through its native test report.

The child environment excludes Shopify signatures, GitHub tokens, proxies,
user pytest options and third-party pytest plugin auto-loading. OS/browser
settings and Jenkins process-tree cookies are retained so cancellation also
stops browser descendants. A timed-out check terminates its process tree.

Local equivalent:

```text
python -m pip install -r requirements-test.lock.txt
python -m playwright install chromium webkit
python scripts/run_framework_ci.py --report-dir artifacts/framework-ci/local-unique-id
```

Never reuse a report directory. Jenkins compilation/test logs remain tied to the
checked-out commit and build through the summary and build page. Storefront
smoke schedules and Full stability histories are independent of this job.

## Integration validation (2026-10-15)

Push and pull_request webhook deliveries were confirmed with HTTP 200 and
automatically created branch and PR jobs. A superseded PR build logged
`Superseded by #2` and stopped its subprocess. PR-2 build #2 ran a temporary
intentional failing test, remained FAILURE, and exposed that test in Jenkins
JUnit while archiving all six scanned report files. The temporary test is
removed from the implementation before merge.

The initial Linux run also caught a pre-existing mock test using a real 1 ms
deadline. That contract test now uses the existing deterministic clock fixture;
the production deadline behavior is unchanged. Local regression has 806 passed
and 3 existing Windows symlink-permission skips. Linux skips two Windows-only
filesystem tests instead.

**GitHub status/required-check activation is pending credential permission.**
The existing `github-kaf-666` SCM credential can read/checkout the repository but
GitHub rejects commit-status updates with HTTP 403 (`Resource not accessible by
personal access token`). The token owner must grant this repository's Commit
statuses read/write permission, or select an existing appropriately scoped
credential in the branch source. Never place the token in source or test env.
After that change, verify pending → failure/success status on the PR, then add
the observed Jenkins PR status context to main's required checks. Do not claim
the GitHub merge gate is enabled until this round trip and protection settings
are verified.
