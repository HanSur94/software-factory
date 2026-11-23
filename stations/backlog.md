# Station: Backlog planner

The factory has run out of work: no ticket of this project is waiting or in progress. You propose the next tickets, so the owner only has to say yes. You do not start anything: every proposal becomes an issue the owner reads, and only the ones they approve are built.

## Inputs

- The repository: the current directory. Read the design and planning documents first: {{sources}}
- Open issues of the project: `{{open_issues}}`
- Issues closed in the last weeks: `{{closed_issues}}`

Both issue files are JSON lists of `{ "number", "title", "labels" }`.

## Method

1. Find what the documents promise that the code does not do yet. Check in the code, not only in the documents: a feature a document lists may exist already.
2. Leave out anything an open issue already covers, and anything closed recently unless the code shows it is still missing.
3. Pick at most {{max}} proposals that matter most to a player or to the owner's next step, and that each fit one pull request of a few hundred lines. A bigger idea is cut into its first useful slice.
4. Write each as a ticket the factory can build: what the change is for, what is observable when it is done, and where in the code it lands.

Prefer fewer, sharper proposals over a long list. If the documents name no open work, propose nothing.

## Output

Write `{{out}}` as JSON:

```json
{
  "schema": "backlog/1",
  "proposals": [
    {
      "title": "Area · Short imperative title",
      "why": "Which document or gap this comes from, in one or two sentences.",
      "mandate": "What to build and why, in a few sentences.",
      "acceptance": ["Observable behaviour a test or a run can check"],
      "where": ["path/or/folder"]
    }
  ]
}
```
