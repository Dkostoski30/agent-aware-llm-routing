# Task card template

Copy this into a new Claude Code session. One card = one task = one commit.

```
# <ID>: <short title>

Goal:
<one or two sentences: what exists after this task that didn't before>

Depends on:
<task IDs that must be green first>

Files allowed:
<exact paths or folders Claude may create or change; everything else is off limits>

Interfaces / outputs:
<config keys, headers, metric names, file formats, commands this task creates or must respect>

Notes:
<decisions that apply (D1–D13), known pitfalls, links>

Done when (I run this myself):
<command>
<expected output>

Record in:
<versions.md / experiments/log.md / nothing>

Instructions for Claude:
Read CLAUDE.md and PLAN.md first. Plan first and wait for my approval. Touch only the files allowed. Tests first for logic. Don't claim it works; tell me which command proves it.
```
