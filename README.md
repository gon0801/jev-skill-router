# jev-skill-router

Skill suggestion for Hermes via the TypeSafe cookbook pattern (Jev) — as a
stdio MCP server. Suggests **at most one** skill for a request, before the
agent loads anything.

```
user request ──▶ skill_select ──▶ Jev (TypeSafe Decisions API)
                                  1. rank all skills + "need a skill?" gates
                                  2. rerank top-3 with full descriptions
                  ◀── one skill name + <skill_relevance> block
```

## Why

Hermes truncates skill index descriptions to ~60 characters. At 190+ skills
the agent guesses from near-zero information — the `.pptx` *editing* skill
reads like the *authoring* one, and on turns where nothing fits, the roster's
"err on the side of loading" invites a wrong load anyway. Measured on the
Hermes catalog (TypeSafe's cookbook, 488 requests, jev-1.12): wrong skill
loads **16.8% → 7.3%**, needless loads **9.8% → 4.0%**. Each avoided load
saves 5–15k characters of context ballast; each wrong one wastes a turn.

## How (two Jev calls per request)

1. **Rank + gate** — one `Choice` question over ALL skill names (criteria =
   the index description) plus 3 `Noul` gates asking whether the turn wants
   an action at all (`acts_on_user_system`, `would_follow_documented_procedure`,
   inverted `prose_suffices`). Gate mean < `gate_threshold` → suggest nothing.
2. **Rerank** — the top-3 candidates return with their FULL description +
   700 chars of `SKILL.md`, one `fits`-Noul each. The winner must be one of
   the candidates (hence in the roster) and its own `fits` must reach
   `fits_threshold`, otherwise → abstain. There is no fallback to "the first
   candidate": when in doubt the answer is "no skill".

Before call 1 the request is cleaned and may be skipped, see
[Precision tuning](#precision-tuning).

The winner's name goes into a `<skill_relevance>` block the caller can put
AFTER the skill roster — the roster itself stays byte-stable, so prefix
caching holds. An `abstain` is a valid answer; the agent keeps its own
judgement ("Ignore this if it does not fit what the user actually asked for").

The roster loads live from `$HERMES_HOME/skills` per session — never
hardcoded. Symlinked skills are followed, block-scalar (`>` / `|`)
descriptions are parsed, category/top-level duplicates are deduped.

## Tool

```
mcp__jev_skill_router__skill_select(request: str)
→ {decision: suggest|abstain, suggested: str|null, reason, thresholds,
   gate, gate_values, shortlist, fits, suggestion_block,
   usage_call1, usage_call2, seconds}
```

`reason` explains the decision: `ok`, `too_short`, `empty_roster`,
`no_ranking`, `gate_below_threshold`, `no_candidates`, `invalid_winner`,
`fits_below_threshold`, `skill_missing`. Tool name and argument are unchanged.

## Setup

### 1. Clone and sync

```bash
git clone https://github.com/ydmw74/jev-skill-router.git
cd jev-skill-router
uv sync          # creates .venv; alternatively: pip install -e .
```

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/).

### 2. Get a TypeSafe API key

Create one at `console.typesafe.ai/keys`.

### 3. Register in Hermes

Add to `~/.hermes/config.yaml` (or a profile config) under `mcp_servers`:

```yaml
  jev-skill-router:
    command: <uv path>          # e.g. /Users/you/.local/bin/uv
    args: [run, --project, <path/to/jev-skill-router>, jev-skill-router, mcp]
    env:
      JEV_TYPESAFE_API_KEY: <your key>
    timeout: 60
    enabled: true
```

Hermes discovers the tool on the next session start. Alternatively ask your
Hermes agent to add the block for you ("add this MCP server to my config")
— it edits the same YAML.

> **Note on the Agent Plugins v1 package (`hermes plugins install`):** the
> portable loader does not interpolate `${JEV_TYPESAFE_API_KEY}` in
> `mcp.json` — it only expands `${PLUGIN_ROOT}`/`${PLUGIN_DATA}`. For the
> packaged install, `JEV_TYPESAFE_API_KEY` must already be set in the
> environment the MCP server inherits (Hermes tracks this gap in
> NousResearch/hermes-agent#120526). The native `mcp_servers` block above
> does not have this limitation — declare the env there if in doubt.

### 4. Install the companion skill (recommended)

The skill tells the agent WHEN to call `skill_select` (and that an `abstain`
is a valid answer). Copy it into your Hermes skills directory:

```bash
mkdir -p ~/.hermes/skills/devops/jev-skill-router-mcp
cp skill/SKILL.md ~/.hermes/skills/devops/jev-skill-router-mcp/SKILL.md
```

### 5. Verify

```bash
hermes mcp test jev-skill-router
```

Cost: see current TypeSafe pricing; each selection is two small Decisions
calls. Latency: 0.3–0.9 s per selection.

### Adapter mode (central endpoint, shared key)

If you run a central Jev endpoint (recommended when several hosts share one
key), point the server at it instead — the payload format is identical:

```yaml
    env:
      JEV_SKILL_ADAPTER_URL: http://<your-adapter>:3004
      JEV_SKILL_ADAPTER_TOKEN: <adapter bearer token>
```

Adapter mode wins when `JEV_SKILL_ADAPTER_URL` is set. Works with any
endpoint that forwards `{state, questions}` to the TypeSafe Decisions API
(`api.typesafe.ai/v1/systemone`) and returns the raw answer.

## Precision tuning

The cookbook defaults (0.30 / 0.30, whole catalog, raw text) are generous. On
a live Hermes profile they suggested unrelated skills for chatter such as
"Revisa si ya quedo?" (→ a reminders skill), "Listo" (→ getting started) or
"Ok no limpies basura deja lo que copiaste limpio" (→ a text humanizer), and
could name a skill that was not in the roster. Behaviour now:

- **Thresholds** `gate_threshold` and `fits_threshold` default to **0.70**.
  That is a provisional starting point, not a calibrated optimum: clear
  matches score ≈0.9 (see examples below) while the observed false positives
  sit in the mid range that 0.30 let through. Every response carries the
  `gate`, `fits` and `thresholds` it used, so tune per profile from real
  traffic. Lower them if useful requests get `abstain`; raise them if noise
  still gets through.
- **Short messages** (< `min_chars`, default 15, measured after cleaning) are
  skipped without any backend call (`reason: too_short`).
- **Gateway wrappers are stripped** before the request is sent: lines starting
  with `JEV skill_select:` / `Jev sugiere:` / `Jev cierre:` and the bracketed
  `[Replying to: …]`, `[The user sent …]`, `[System note: …]`, `[Telegram …]`.
  Other text (including other `[brackets]`) is left alone; if nothing is left
  the original request is used.
- **Roster scoping** with `allow` / `deny`. Entries are skill names or
  `category:<name>` (glob patterns, case-insensitive). `allow` empty = every
  skill; `deny` always wins. Category is the first directory under
  `$HERMES_HOME/skills` (`skills/<category>/<skill>/SKILL.md`) or a
  `category:` frontmatter field; top-level skills have no category. Filtering
  happens before call 1, so excluded skills are never sent to the backend.
- **Validation**: the returned skill must be in the (filtered) roster and its
  `SKILL.md` must still exist; otherwise `abstain`.

### Configuration

Precedence: environment variable > JSON file > default. The JSON file is
`$JEV_SKILL_ROUTER_CONFIG` if set, else `$HERMES_HOME/jev-skill-router.json`
(a per-profile file, since `HERMES_HOME` is per profile):

```json
{
  "gate_threshold": 0.7,
  "fits_threshold": 0.7,
  "min_chars": 15,
  "allow": ["category:devops", "amazon-*", "goncloud-local-chains"],
  "deny": ["category:creative", "category:media"]
}
```

| Setting | Env var | Default |
|---|---|---|
| `gate_threshold` | `JEV_SKILL_GATE_THRESHOLD` | 0.7 |
| `fits_threshold` | `JEV_SKILL_FITS_THRESHOLD` | 0.7 |
| `min_chars` | `JEV_SKILL_MIN_CHARS` | 15 |
| `allow` | `JEV_SKILL_ALLOW` (comma-separated) | empty (all) |
| `deny` | `JEV_SKILL_DENY` (comma-separated) | empty |

Invalid values are ignored with a warning on stderr. Config and roster are
read once per session, like the roster.

### Tests

```bash
python -m unittest discover -s tests -v
```

No network: the backend call is faked. The suite covers the six real false
positives, clear positives, wrapper stripping, allow/deny, config precedence
and the unchanged MCP tool schema.

## Example results

Paraphrases with zero lexical overlap with the skill name:

- „Bearbeite die Folien meiner Präsentation von letzter Woche" → `powerpoint`
  (fits 0.90) — a semantic match the lexical index cannot see
- „Was ist eine Cloud-Funktion im Vergleich zu einem Server?" → abstain
  (gate 0.05) — a prose question needs no skill

## Notes

- Model: `jev-latest` via the Decisions API (`system_one`). Two calls are
  the cookbook shape; the first call's ranking carries most of the value.
- SHORTLIST=3 follows the cookbook; the thresholds are stricter than its
  0.30/0.30 defaults (see Precision tuning).
- Works for any MCP host, not just Hermes: the roster path and the tool
  schema are the only Hermes-specific parts.

## License

MIT