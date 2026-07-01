# Phase 2: Git Diff Review

Repository-path reviews now inspect changes from:

```bash
git diff main...HEAD
```

The backend parses the unified diff by file and hunk, then sends the reviewer:

- file path
- changed line numbers in the new file
- added or modified lines
- surrounding context lines
- removed lines only as context

The LLM prompt requires the reviewer to:

- act as a code review assistant
- review only changed code from the diff
- avoid generic advice
- return each issue with file name, line number, severity, reason, and suggestion
- return an empty `issues` array when there are no real problems

Pasted-code and uploaded-file reviews still use the existing whole-code review path.
