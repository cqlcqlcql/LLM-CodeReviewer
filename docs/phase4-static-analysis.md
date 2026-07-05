# Phase 4: Static Analysis

Phase 4 runs deterministic local tools before relying only on the LLM. Repository
reviews now return one merged `issues` list where every item includes a `source`
field, such as `LLM`, `ruff`, `mypy`, `bandit`, `pytest`, or `npm`.

Supported tool detection:

| Project | Command | Reported source |
| --- | --- | --- |
| Python | `ruff check .` | `ruff` |
| Python | `mypy .` | `mypy` |
| Python | `bandit -r .` | `bandit` |
| Python with tests | `pytest` | `pytest` |
| JavaScript/TypeScript | `npm run lint` | `npm` |
| JavaScript/TypeScript | `npm test` | `npm` |

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

`run_tests: true` still returns the detailed `test_result` object from Phase 3.
The Phase 4 pytest run only contributes a merged issue when tests exist and fail.
