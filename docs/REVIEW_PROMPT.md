# Fresh-eyes review prompt

Run this in a **new** Claude Code session after a task is implemented and before committing. The session that wrote the code doesn't review it.

```
You are reviewing uncommitted changes for task <ID>. You did not write them.

1. Read CLAUDE.md, the design decisions in PLAN.md, and this task card:
<paste the task card>

2. Run `git diff` and `git status` to see every change.

3. Check each item and answer yes/no with file:line evidence:
- Only files allowed by the card were touched?
- Any dead code, TODOs, commented-out blocks, or unexplained constants?
- Any broad try/except, ignored errors, or swallowed failures?
- Tests assert behavior? Was any test edited, skipped or weakened?
- Any new dependency? Is it pinned and justified?
- Does it follow D1–D13 (config profiles, flags, schemas, thesis_ metrics, JSON logs with run-id)?
- Are thresholds in config with a comment saying why?
- Logs and metrics present where the thesis needs measurements?
- Could the author explain every line at a thesis defense?

4. Report only problems, risks and questions, most serious first. Don't fix anything.
```

If the review finds problems, fix them in the original session, then review again in a new one.
