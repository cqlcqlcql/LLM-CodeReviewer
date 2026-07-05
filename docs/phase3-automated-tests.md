# Phase 3: Automated Tests

Phase 3 adds a project test runner that can be used directly through
`POST /api/test` or alongside repository diff review by sending
`run_tests: true` to `POST /api/review`.

Supported project detection:

| Language | Detection | Command |
| --- | --- | --- |
| Python | pytest config, requirements, or `tests/test_*.py` | `pytest` |
| JavaScript/TypeScript | `package.json` | `npm test` |
| Java | `pom.xml` or Gradle build files | `mvn test` / `gradle test` |

Test runs are limited by `timeout_seconds`, with repository reviews using a
60-second default. The backend captures stdout and stderr, records pass/fail
status, extracts a failed-case count when the test tool reports one, and asks
the configured reviewer to explain failed test logs.

Example response:

```json
{
  "test_status": "failed",
  "command": "pytest",
  "failed_cases": 2,
  "log_excerpt": "...",
  "llm_explanation": "pytest failed because one or more assertions did not match the expected behavior."
}
```
