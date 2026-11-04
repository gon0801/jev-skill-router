---
name: jev-skill-router-mcp
description: Suggest ONE relevant skill via Jev before loading any.
version: 0.1.0
license: MIT
related_skills: [jev-mcp-router, typesafe-ai]
tags: [jev, typesafe, skill-routing, token-saving]
---

# JEV Skill Router

Suggests at most ONE skill for the current request via the TypeSafe cookbook
pattern (Jev): one Choice call ranks all skills + 3 Noul gates ("does this
turn need a skill at all?"), a second reranks the top-3 with full
descriptions. Measured on the Hermes catalog: wrong skill loads 16.8% -> 7.3%,
needless loads 9.8% -> 4.0%.

## Usage

The MCP server `jev-skill-router` registers:

```
mcp__jev_skill_router__skill_select
```

Arguments: `request` (string) — the user's request / task description.

Call it at the START of a task, BEFORE loading any skill with skill_view. The
result carries:
- `decision`: `suggest` | `abstain`
- `suggested`: the skill name (max ONE) or null
- `gate`, `gate_values`, `shortlist`, `fits`: why
- `suggestion_block`: the `<skill_relevance>` block

## Rules

- An `abstain` is a valid answer: when the gate says no skill fits, do NOT load
  one anyway. But the roster preamble still applies — if the request clearly
  matches a skill the router missed, trust your own judgment and load it.
- The suggestion is a HINT, not an order: "Ignore this if it does not fit what
  the user actually asked for." If you know better, you know better.
- Only ONE skill comes back. For multi-part tasks, call skill_select per part
  or use your own judgment for secondary skills.
- The router reads the roster from HERMES_HOME/skills at first call per
  session (symlinks followed, duplicates deduped); skill changes apply next
  session.
- Backend: either the TypeSafe Decisions API directly (JEV_TYPESAFE_API_KEY)
  or a LAN adapter endpoint (JEV_SKILL_ADAPTER_URL + token) — see the
  jev-skill-router README for both modes.

## When to use

- Large rosters (100+ skills) where index descriptions are truncated to ~60
  chars and lookalike skills (pptx edit vs author) are confusable.
- Paraphrases where lexical matching fails ("Bearbeite die Folien meiner
  Präsentation" -> powerpoint).
- Before EVERY skill load in fresh sessions when unsure which skill fits.
- NOT for trivial requests (greetings, simple questions) — the Jev gate
  abstains on prose-only turns.