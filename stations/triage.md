# Station: Check triage

The project's checks failed on this branch. You read the logs and write down what actually went wrong, so that the implementer does not have to dig through them.

## Failing checks

{{failed}}

## Method

Read each log. Find the first real error, not the noise after it. Look at the change on this branch (`git diff`, `git log`) to say which part of it most likely caused the failure. Do not fix anything.

## Output

Write `{{out}}`, at most 30 lines of plain Markdown. For each failing check:

- what failed: the test names or the first real error message, quoted exactly
- the most likely cause in the change, with file and line if you can tell
- whether it looks like a problem of the change or of the environment (missing tool, timeout)
