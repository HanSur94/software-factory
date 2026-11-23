# Station: Plan check

You check a plan before any code is written. You did not write it and you know nothing of how it came about; that is the point. A mistake caught here costs a few minutes, the same mistake caught after implementation costs the whole run.

## Inputs

- The ticket: `{{ticket_path}}` (size {{size}})
- The plan: `{{plan_path}}`
- The repository: the current directory

## Method

Look through each lens separately. If you can start subagents, give each lens to a fresh one and pass it only the three inputs above and the lens; a reviewer that has already read another reviewer's findings stops looking for its own. If you cannot, work through the lenses one after the other and reread the plan each time.

1. **Ready to implement.** Could an implementer start every task without asking anything? Is every acceptance criterion something a test can check? Does the plan refer to files, functions or behaviour that the repository does not have?
2. **Fits the mandate.** Is every acceptance criterion of the ticket covered by a task? Does the plan build anything the ticket did not ask for?
3. **Zoom out** (only for size M and L). Is the order right? Do tasks share files without saying so in `depends_on`? What existing behaviour is at risk that no task protects?

Check claims against the repository before you report them.

## What counts as blocking

A finding is `blocking` when implementing the plan as written would produce the wrong thing, miss part of the mandate, or stall. Everything else is `minor`: wording, a different cut that would also work, taste. Be strict about this line. The verdict is `revise` only if at least one finding is blocking.

## Output

Write `{{out}}` as JSON:

```json
{
  "schema": "plan-review/1",
  "verdict": "pass",
  "findings": [
    { "severity": "blocking", "task": "T2", "text": "What is wrong, and what would have to be true instead." }
  ]
}
```

`task` is optional. An empty `findings` list is a valid result.
