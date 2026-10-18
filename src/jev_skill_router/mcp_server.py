"""Skill suggestion via the TypeSafe cookbook pattern (Jev).

Zwei Calls pro Anfrage (Cookbook "Skill suggestion", docs.typesafe.ai):
  Call 1 rankt das komplette Roster (Choice ueber alle Skill-Namen, Kriterium
  = Index-Beschreibung) + 3 Noul-Gates ("will der Turn ueberhaupt ein Skill?").
  Gate-Mittel < 0.30 -> nichts vorschlagen, Call 2 entfaellt.
  Call 2 rerankt die Top-3 mit VOLLER Beschreibung + SKILL.md-Anfang (700 Z.)
  und einem fits-Noul je Kandidat; alle fits < 0.30 -> nichts.
Output: maximal EIN Skill-Name (oder ()) + der <skill_relevance>-Block.

Roster: dynamisch aus dem Hermes-Skill-Index (nie hardcoden). Der Server
liest alle SKILL.md-Frontmatters des aktiven Profils und haelt den Stand
fuer die Session vor (Roster-Aenderungen gelten ab naechster Session —
cache-safe, wie beim Router-Toolset ueblich).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
from urllib.error import HTTPError, URLError

ADAPTER_URL = (os.environ.get("JEV_SKILL_ADAPTER_URL") or "").rstrip("/")
ADAPTER_TOKEN = os.environ.get("JEV_SKILL_ADAPTER_TOKEN", "")
# Direct mode: your own TypeSafe key, no adapter hop (for standalone setups)
TYPESAFE_API_KEY = os.environ.get("JEV_TYPESAFE_API_KEY", "")
TYPESAFE_URL = (os.environ.get("JEV_TYPESAFE_URL")
                or "https://api.typesafe.ai/v1/systemone")
def _skills_root() -> str:
    """JEV_SKILLS_ROOT > HERMES_HOME/skills > owning profile dir > ~/.hermes/skills.

    The Hermes gateway does not always pass HERMES_HOME to the MCP subprocess;
    without this the roster is empty and TypeSafe rejects the choice with 422."""
    explicit = os.environ.get("JEV_SKILLS_ROOT")
    if explicit:
        return explicit
    home = os.environ.get("HERMES_HOME")
    if home:
        return home + "/skills"
    # <perfil>/plugins/jev-skill-router-mcp/src/jev_skill_router/mcp_server.py
    profile = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
    if os.path.isdir(os.path.join(profile, "skills")):
        return os.path.join(profile, "skills")
    return os.path.expanduser("~/.hermes") + "/skills"


SKILLS_ROOT = _skills_root()
GATE_THRESHOLD = 0.30
FITS_THRESHOLD = 0.30
SHORTLIST = 3
EXCERPT_CHARS = 700

CHOICE_INSTRUCTIONS = (
    "Which of these skills, if any, is the right one to load to help with the "
    "user's latest request?"
)
GATE_QUESTIONS = {
    "acts_on_user_system": (
        "Is the assistant being asked to act on the user's files, accounts, "
        "devices, or online services, rather than only to explain or advise?"
    ),
    "would_follow_documented_procedure": (
        "Would a careful expert answering this consult a specific documented "
        "procedure or set of commands, rather than answering from general "
        "understanding?"
    ),
    "prose_suffices": (
        "Could a knowledgeable generalist fully satisfy this request in "
        "prose, with no tools, no documentation, and no access to the user's "
        "files or accounts?"
    ),
}
INVERTED = {"prose_suffices"}
RERANK_INSTRUCTIONS = (
    "Exactly one of these skills is the right one to load for the user's "
    "latest request. Which one? Read what each actually does, not just its name."
)


# ---------------------------------------------------------------- roster

_FM_DESC = re.compile(r"^description:\s*(.+)$", re.M)


def _parse_frontmatter(text: str) -> dict:
    """Minimal-Frontmatter-Parser: name, description (inkl. > und | Block-Skalarare), category."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    fm_lines = text[3:end].split("\n")
    out: dict = {}
    i = 0
    while i < len(fm_lines):
        line = fm_lines[i]
        m = re.match(r"^(name|description):\s*(.*)$", line)
        if not m:
            i += 1
            continue
        key, value = m.group(1), m.group(2).strip()
        if value in (">", "|", ">-", "|-", ">", "|"):
            # Block-Skalar: alle folgenden eingerueckten Zeilen gehoeren dazu
            block = []
            j = i + 1
            while j < len(fm_lines) and (fm_lines[j].startswith("  ") or fm_lines[j].strip() == ""):
                block.append(fm_lines[j].strip())
                j += 1
            out[key] = " ".join(b for b in block if b)
            i = j
            continue
        out[key] = value.strip("'\"")
        i += 1
    return out


def load_roster() -> list[dict]:
    """Liest alle SKILL.md unter SKILLS_ROOT -> [{name, description, path, body}].

    followlinks=True: Top-Level-Skills sind oft Symlinks (z.B. -> ~/.claude/skills)."""
    roster = []
    for dirpath, _dirs, files in os.walk(SKILLS_ROOT, followlinks=True):
        if "SKILL.md" not in files:
            continue
        path = os.path.join(dirpath, "SKILL.md")
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        fm = _parse_frontmatter(text)
        name = fm.get("name") or os.path.basename(dirpath)
        body = text.split("---", 2)[-1] if text.startswith("---") else text
        entry = {
            "name": name,
            "description": fm.get("description", ""),
            "path": path,
            "body": body.strip(),
        }
        # Duplikate (gleicher Skill ueber Kategorie-Dir UND Top-Level): der
        # Eintrag mit der laengeren Beschreibung gewinnt (der andere ist oft
        # die trunkierte Katalog-Kopie).
        if name in {s["name"] for s in roster}:
            existing = next(s for s in roster if s["name"] == name)
            if len(entry["description"]) > len(existing["description"]):
                roster.remove(existing)
                roster.append(entry)
            continue
        roster.append(entry)
    roster.sort(key=lambda s: s["name"])
    return roster


# ---------------------------------------------------------------- adapter

def call_adapter(document: dict) -> dict:
    """Ein Call ans Backend: {state, questions} -> rohe Antwort.

    Zwei Modi: Adapter (LAN-Endpoint mit Bearer) oder Direct (eigener
    TypeSafe-Key). Beide sprechen dasselbe {state, questions}-Format."""
    url = ADAPTER_URL + "/" if ADAPTER_URL else TYPESAFE_URL
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "jev-skill-router/1.0",
    }
    if ADAPTER_URL:
        if not ADAPTER_TOKEN:
            raise RuntimeError("JEV_SKILL_ADAPTER_TOKEN not set")
        headers["Authorization"] = f"Bearer {ADAPTER_TOKEN}"
    else:
        if not TYPESAFE_API_KEY:
            raise RuntimeError(
                "set JEV_SKILL_ADAPTER_URL + JEV_SKILL_ADAPTER_TOKEN (adapter mode) "
                "or JEV_TYPESAFE_API_KEY (direct mode)")
        headers["Authorization"] = f"Bearer {TYPESAFE_API_KEY}"
        # Parche local Goncloud (2026-10-18): la API de TypeSafe ahora exige "model" (HTTP 422 sin él).
        document = {"model": os.environ.get("JEV_SKILL_MODEL", "jev-latest"), **document}
    req = urllib.request.Request(
        url,
        data=json.dumps(document).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        err = exc.read().decode("utf-8", "replace")[:200]
        raise RuntimeError(f"backend HTTP {exc.code}: {err}") from None
    except URLError as exc:
        raise RuntimeError(f"backend unreachable: {exc.reason}") from exc


# ---------------------------------------------------------------- suggest

def suggest(request: str, roster: list[dict]) -> dict:
    """Cookbook suggest(): zwei Calls, zwei Schwellen, max. ein Skill-Name."""
    by_name = {s["name"]: s for s in roster}
    state = {"request": request, "recent_context": ""}

    # ---- Call 1: Choice-Ranking + Gates (ein Round-Trip)
    questions = {
        "which": {
            "type": "choice",
            "instructions": CHOICE_INSTRUCTIONS,
            "criteria": {s["name"]: s["description"] for s in roster},
        }
    }
    for key, text in GATE_QUESTIONS.items():
        questions[f"gate::{key}"] = {"type": "noul", "instructions": text}
    resp = call_adapter({"state": state, "questions": questions})
    answers = resp.get("answers") or {}
    which = answers.get("which") or {}
    probabilities = which.get("probabilities") or {}
    ranked = sorted(probabilities.items(), key=lambda kv: -kv[1])
    values = {
        k.removeprefix("gate::"): (a.get("noul") or 0.0)
        for k, a in answers.items() if k.startswith("gate::")
    }
    oriented = [(1.0 - v) if k in INVERTED else v for k, v in values.items()]
    gate = sum(oriented) / len(oriented) if oriented else 0.0

    result = {
        "roster_size": len(roster),
        "gate": round(gate, 3),
        "gate_values": {k: round(v, 3) for k, v in values.items()},
        "suggested": None,
        "usage_call1": resp.get("usage") or {},
    }
    if gate < GATE_THRESHOLD or not ranked:
        result["decision"] = "abstain"
        return result

    # ---- Call 2: Rerank Top-3 mit voller Beschreibung + Body-Exzerpt
    shortlist = tuple(name for name, _p in ranked[:SHORTLIST] if name in by_name)
    result["shortlist"] = shortlist
    if not shortlist:
        result["decision"] = "abstain"
        return result
    questions2 = {
        "which": {
            "type": "choice",
            "instructions": RERANK_INSTRUCTIONS,
            "criteria": {
                n: f"{by_name[n]['description']} — {by_name[n]['body'][:EXCERPT_CHARS]}"
                for n in shortlist
            },
        }
    }
    for name in shortlist:
        questions2[f"fits::{name}"] = {
            "type": "noul",
            "instructions": (
                f"Does the skill '{name}' do the specific thing the user's request "
                f"asks for? It is described as: {by_name[name]['description']}"
            ),
        }
    resp2 = call_adapter({"state": state, "questions": questions2})
    answers2 = resp2.get("answers") or {}
    fits = {
        k.removeprefix("fits::"): (a.get("noul") or 0.0)
        for k, a in answers2.items() if k.startswith("fits::")
    }
    winner = (answers2.get("which") or {}).get("choice")
    result["fits"] = {k: round(v, 3) for k, v in fits.items()}
    result["usage_call2"] = resp2.get("usage") or {}
    if not fits or max(fits.values()) < FITS_THRESHOLD:
        result["decision"] = "abstain"
        return result
    result["decision"] = "suggest"
    result["suggested"] = winner if winner in by_name else shortlist[0]
    return result


def suggestion_block(name: str | None) -> str:
    """Der Block, der ans System-Prompt-ENDE gehen wuerde (cache-sicher)."""
    body = (
        f"Relevant to the current request: {name}. Ignore this if it does not "
        "fit what the user actually asked for."
        if name else
        "No skill in the roster appears relevant to this request."
    )
    return f"\n\n<skill_relevance>\n{body}\n</skill_relevance>"


# ---------------------------------------------------------------- stdio MCP

def _write(msg: dict) -> None:
    """MCP-Stdio (2024-11-05): newline-delimited JSON."""
    raw = json.dumps(msg).encode("utf-8")
    sys.stdout.buffer.write(raw + b"\n")
    sys.stdout.buffer.flush()


def _read() -> dict:
    line = sys.stdin.readline()
    if not line:
        raise EOFError
    return json.loads(line)


def serve_stdio() -> None:
    roster = None  # lazy: erst beim ersten skill_select
    while True:
        try:
            msg = _read()
        except EOFError:
            break
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        msg_id = msg.get("id")
        if method == "initialize":
            _write({"jsonrpc": "2.0", "id": msg_id, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "jev-skill-router", "version": "0.1.0"},
            }})
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            _write({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": [{
                "name": "skill_select",
                "description": (
                    "Suggest at most ONE skill for a user request via Jev "
                    "(TypeSafe cookbook pattern). Returns the skill name, the "
                    "gate/fit scores, and a <skill_relevance> block to append "
                    "after the skill roster. Call BEFORE loading any skill."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "request": {
                            "type": "string",
                            "description": "The user's request / task description.",
                        },
                    },
                    "required": ["request"],
                },
            }]}})
        elif method == "tools/call":
            params = (msg.get("params") or {})
            if (params.get("name") or "") != "skill_select":
                _write({"jsonrpc": "2.0", "id": msg_id, "error": {
                    "code": -32601, "message": "unknown tool"}})
                continue
            args = params.get("arguments") or {}
            request_text = str(args.get("request") or "").strip()
            if not request_text:
                _write({"jsonrpc": "2.0", "id": msg_id, "result": {
                    "content": [{"type": "text", "text": json.dumps({
                        "error": "request required"})}]}})
                continue
            try:
                if roster is None:
                    roster = load_roster()
                started = time.perf_counter()
                res = suggest(request_text, roster)
                res["seconds"] = round(time.perf_counter() - started, 2)
                res["suggestion_block"] = suggestion_block(res.get("suggested"))
                _write({"jsonrpc": "2.0", "id": msg_id, "result": {
                    "content": [{"type": "text",
                                 "text": json.dumps(res, ensure_ascii=False)}]}})
            except Exception as exc:  # nie den stdio-Loop killen
                _write({"jsonrpc": "2.0", "id": msg_id, "result": {
                    "content": [{"type": "text", "text": json.dumps({
                        "error": str(exc)})}]}})


def main() -> None:
    serve_stdio()


if __name__ == "__main__":
    main()