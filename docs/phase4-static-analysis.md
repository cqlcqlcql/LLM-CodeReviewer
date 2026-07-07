# Phase 4: Static Analysis

Phase 4 runs deterministic local tools before the final LLM review. Repository
reviews collect Git diff context, static-analysis findings, and optional test
results first, then send that combined evidence to the reviewer once. The
response is one deduplicated `issues` list where every item includes a `source`
field, such as `LLM`, `ruff`, `mypy`, `bandit`, `npm`, or `LLM + pytest`.

Supported tool detection:

| Project | Command | Reported source |
| --- | --- | --- |
| Python | `ruff check .` | `ruff` |
| Python | `mypy .` | `mypy` |
| Python | `bandit -r .` | `bandit` |
| JavaScript/TypeScript | `npm run lint` | `npm` |

Repository review enables static analysis by default:

```json
{
  "language": "python",
  "repository_path": "D:\\profile\\code-reviewer-mvp",
  "base_branch": "main",
  "run_static_analysis": true
}
```

The backend skips tools that are not installed or npm scripts that do not exist,
so missing optional tooling does not create fake review issues. Static tools
exclude common virtual environment, cache, and temporary directories by default.
Bandit also skips its `assert` rule, which avoids noisy test-style warnings in
the merged review. Tools that run and fail are converted into normal review
issues with their original source, file path and line number when the tool
provides them.

Repository review can still run static analysis and tests when the target path
is not a Git repository. In that case Git diff review is skipped with a neutral
`notices` message, while tool results are still returned normally.

Example merged issue:

```json
{
  "source": "ruff",
  "file_path": "calculator.py",
  "severity": "low",
  "category": "F841",
  "line": 2,
  "message": "Local variable `unused` is assigned to but never used.",
  "suggestion": "Remove the unused variable."
}
```

`run_tests: true` returns the detailed `test_result` object from Phase 3.
Test commands such as `pytest` and `npm test` are not merged as static-analysis
issues. They are passed into the final repository review prompt as runtime
evidence, so a test failure and a matching diff issue can be reported as one
combined issue instead of duplicate cards.
