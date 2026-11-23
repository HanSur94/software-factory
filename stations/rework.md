# Station: Implementer (rework)

The change on this branch was sent back by: **{{source}}**. Fix what is described below. Do not start over, and do not widen the change beyond what the findings need.

## Inputs

- The ticket (the fixed mandate): `{{ticket_path}}`
- The plan: `{{plan_path}}`
- What has been built so far: the commits on this branch (`git log`, `git diff`)

## Findings

{{findings}}

{{source_hint}}

## Method

- For each finding, first see the problem yourself: run the failing check, or reproduce the behaviour. Then find the cause and fix the cause.
- Add or correct a test for each fix, so that the same problem cannot come back unnoticed.
- If a finding is wrong, do not change the code to please it. Say in your notes why it does not apply.
- Do not weaken or delete tests to get to green.
- Before you finish, run the project's checks and do not stop while one fails because of your change:
{{checks}}

## Output

The corrected change, uncommitted, in the working tree. In addition write a few lines to `{{notes_path}}`: what you fixed for each finding, and what you left and why.
