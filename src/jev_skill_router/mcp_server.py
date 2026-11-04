"""Skill suggestion via the TypeSafe cookbook pattern (Jev).

Zwei Calls pro Anfrage (Cookbook "Skill suggestion", docs.typesafe.ai):
  Call 1 rankt das komplette Roster (Choice ueber alle Skill-Namen, Kriterium
  = Index-Beschreibung) + 3 Noul-Gates ("will der Turn ueberhaupt ein Skill?").
  Gate-Mittel < gate_threshold -> nichts vorschlagen, Call 2 entfaellt.
  Call 2 rerankt die Top-3 mit VOLLER Beschreibung + SKILL.md-Anfang (700 Z.)
  und einem fits-Noul je Kandidat; der Gewinner muss im Roster stehen und
  selbst >= fits_threshold haben, sonst nichts.
Output: maximal EIN Skill-Name (oder ()) + der <skill_relevance>-Block.

Praezision (Defaults gate 0.70 / fits 0.40, konfigurierbar, siehe
load_config): Gateway-Umschlaege werden vor der Anfrage entfernt, Nachrichten < 15 Zeichen werden
uebersprungen, das Roster laesst sich per allow/deny eingrenzen, und im
Zweifel gibt es KEINEN Vorschlag.

Roster: dynamisch aus dem Hermes-Skill-Index (nie hardcoden). Der Server
liest alle SKILL.md-Frontmatters des aktiven Profils und haelt den Stand
fuer die Session vor (Roster-Aenderungen gelten ab naechster Session —
cache-safe, wie beim Router-Toolset ueblich).
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import sys
import time
import urllib.request
from dataclasses import dataclass
from urllib.error import HTTPError, URLError

ADAPTER_URL = (os.environ.get("JEV_SKILL_ADAPTER_URL") or "").rstrip("/")
ADAPTER_TOKEN = os.environ.get("JEV_SKILL_ADAPTER_TOKEN", "")
# Direct mode: your own TypeSafe key, no adapter hop (for standalone setups)
TYPESAFE_API_KEY = os.environ.get("JEV_TYPESAFE_API_KEY", "")
TYPESAFE_URL = (os.environ.get("JEV_TYPESAFE_URL")
                or "https://api.typesafe.ai/v1/systemone")


def _skills_root(env=None, module_file: str | None = None) -> str:
    """JEV_SKILLS_ROOT > HERMES_HOME/skills > Profil des Plugins > ~/.hermes/skills.

    Der Gateway reicht HERMES_HOME nicht immer an den MCP-Subprozess durch;
    ohne den Profil-Fallback ist das Roster leer und TypeSafe antwortet 422."""
    env = os.environ if env is None else env
    explicit = env.get("JEV_SKILLS_ROOT")
    if explicit:
        return explicit
    home = env.get("HERMES_HOME")
    if home:
        return home + "/skills"
    # <profil>/plugins/<plugin>/src/jev_skill_router/mcp_server.py
    here = os.path.dirname(os.path.abspath(module_file or __file__))
    plugin_dir = os.path.abspath(os.path.join(here, "..", ".."))
    plugins_dir = os.path.dirname(plugin_dir)
    if os.path.basename(plugins_dir) == "plugins":
        skills = os.path.join(os.path.dirname(plugins_dir), "skills")
        if os.path.isdir(skills):
            return skills
    return os.path.expanduser("~/.hermes") + "/skills"


SKILLS_ROOT = _skills_root()
# Cookbook-Default war 0.30; gegen echte Hermes-Turns zu lax (siehe README,
# "Precision tuning"). gate 0.70 / fits 0.40 = kalibriert am 2026-10-19
# (README, "Calibration"); ueber env/JSON pro Profil anpassbar.
DEFAULT_GATE_THRESHOLD = 0.70
DEFAULT_FITS_THRESHOLD = 0.40
DEFAULT_MIN_CHARS = 15
CONFIG_FILENAME = "jev-skill-router.json"
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
    """Minimal-Frontmatter-Parser: name, description (inkl. > und | Block-Skalare), category."""
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
        m = re.match(r"^(name|description|category):\s*(.*)$", line)
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


IGNORED_DIR_PATTERNS = ("_archivado", "*.bak", "backup*")


def _is_ignored_dir(name: str) -> bool:
    lowered = name.lower()
    return lowered.startswith(".") or any(
        fnmatch.fnmatchcase(lowered, p) for p in IGNORED_DIR_PATTERNS)


def load_roster(root: str | None = None) -> list[dict]:
    """Liest alle SKILL.md unter root (Default SKILLS_ROOT) ->
    [{name, description, category, path, body}].

    category = Frontmatter-Wert, sonst das erste Verzeichnis unter root
    (skills/<category>/<skill>/SKILL.md); Top-Level-Skills haben "".
    followlinks=True: Top-Level-Skills sind oft Symlinks (z.B. -> ~/.claude/skills)."""
    root = root or SKILLS_ROOT
    roster = []
    for dirpath, dirs, files in os.walk(root, followlinks=True):
        # Versteckte und Backup-Ordner (.archive, .git, _archivado, *.bak,
        # backup*) sind keine lebenden Skills; nur Namen unterhalb von root
        # werden geprueft, root selbst darf unter einem versteckten Pfad liegen.
        dirs[:] = [d for d in dirs if not _is_ignored_dir(d)]
        if "SKILL.md" not in files:
            continue
        path = os.path.join(dirpath, "SKILL.md")
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        fm = _parse_frontmatter(text)
        name = fm.get("name") or os.path.basename(dirpath)
        body = text.split("---", 2)[-1] if text.startswith("---") else text
        rel = os.path.relpath(dirpath, root).replace(os.sep, "/").split("/")
        dir_category = rel[0] if len(rel) > 1 else ""
        entry = {
            "name": name,
            "description": fm.get("description", ""),
            "category": fm.get("category") or dir_category,
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


# ---------------------------------------------------------------- config

@dataclass(frozen=True)
class Config:
    gate_threshold: float = DEFAULT_GATE_THRESHOLD
    fits_threshold: float = DEFAULT_FITS_THRESHOLD
    min_chars: int = DEFAULT_MIN_CHARS
    allow: tuple = ()   # leer = alle Skills; sonst nur Treffer
    deny: tuple = ()    # gewinnt immer gegen allow


def _warn(msg: str) -> None:
    print(f"jev-skill-router: {msg}", file=sys.stderr)


def _as_threshold(raw, name: str, default: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        _warn(f"ignoring invalid {name}={raw!r}")
        return default
    if not 0.0 <= value <= 1.0:
        _warn(f"ignoring out-of-range {name}={raw!r} (need 0..1)")
        return default
    return value


def _as_patterns(raw) -> tuple:
    if raw is None:
        return ()
    items = raw.split(",") if isinstance(raw, str) else raw
    if not isinstance(items, (list, tuple)):
        return ()
    return tuple(str(i).strip() for i in items if str(i).strip())


def load_config(env=None, home: str | None = None) -> Config:
    """Prioritaet: env > JSON-Datei > Default.

    JSON: $JEV_SKILL_ROUTER_CONFIG, sonst $HERMES_HOME/jev-skill-router.json
    (Keys: gate_threshold, fits_threshold, min_chars, allow, deny).
    env: JEV_SKILL_GATE_THRESHOLD, JEV_SKILL_FITS_THRESHOLD,
    JEV_SKILL_MIN_CHARS, JEV_SKILL_ALLOW, JEV_SKILL_DENY (kommagetrennt).
    Ungueltige Werte werden ignoriert (Warnung auf stderr), nie fatal."""
    env = os.environ if env is None else env
    home = home or env.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    path = env.get("JEV_SKILL_ROUTER_CONFIG") or os.path.join(home, CONFIG_FILENAME)
    data: dict = {}
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                data = loaded
            else:
                _warn(f"{path}: expected a JSON object, ignoring")
        except (OSError, ValueError) as exc:
            _warn(f"cannot read {path}: {exc}")

    def pick(env_key: str, json_key: str):
        return env[env_key] if env.get(env_key) not in (None, "") else data.get(json_key)

    gate = pick("JEV_SKILL_GATE_THRESHOLD", "gate_threshold")
    fits = pick("JEV_SKILL_FITS_THRESHOLD", "fits_threshold")
    min_chars = pick("JEV_SKILL_MIN_CHARS", "min_chars")
    try:
        min_chars = DEFAULT_MIN_CHARS if min_chars is None else max(0, int(min_chars))
    except (TypeError, ValueError):
        _warn(f"ignoring invalid min_chars={min_chars!r}")
        min_chars = DEFAULT_MIN_CHARS
    return Config(
        gate_threshold=DEFAULT_GATE_THRESHOLD if gate is None
        else _as_threshold(gate, "gate_threshold", DEFAULT_GATE_THRESHOLD),
        fits_threshold=DEFAULT_FITS_THRESHOLD if fits is None
        else _as_threshold(fits, "fits_threshold", DEFAULT_FITS_THRESHOLD),
        min_chars=min_chars,
        allow=_as_patterns(pick("JEV_SKILL_ALLOW", "allow")),
        deny=_as_patterns(pick("JEV_SKILL_DENY", "deny")),
    )


def _matches(pattern: str, skill: dict) -> bool:
    """`category:<glob>` prueft die Kategorie, alles andere den Skill-Namen
    (fnmatch, case-insensitive)."""
    pattern = pattern.lower()
    if pattern.startswith("category:"):
        return fnmatch.fnmatchcase((skill.get("category") or "").lower(),
                                   pattern[len("category:"):])
    return fnmatch.fnmatchcase(skill["name"].lower(), pattern)


def filter_roster(roster: list[dict], cfg: Config) -> list[dict]:
    """allow (leer = alle) einschraenken, dann deny abziehen."""
    return [
        s for s in roster
        if (not cfg.allow or any(_matches(p, s) for p in cfg.allow))
        and not any(_matches(p, s) for p in cfg.deny)
    ]


# ---------------------------------------------------------------- request cleaning

# Gateway-Zeilen aus frueheren Vorschlaegen/Abschluessen.
_WRAPPER_LINE = re.compile(
    r"^[ \t]*(?:JEV skill_select|Jev sugiere|Jev cierre):.*$", re.I | re.M)
# Eckige Umschlaege des Gateways; bewusst nur diese vier Tags, damit echter
# Nutzertext in [..] (z.B. "[ ] tarea") nie verschwindet.
_WRAPPER_BRACKET = re.compile(
    r"\[(?:Replying to|The user sent|System note|Telegram)\b[^\]]*\]", re.I)


def clean_request(text: str) -> str:
    """Entfernt Gateway-Umschlaege; bleibt nichts uebrig, gilt das Original."""
    original = text.strip()
    cleaned = _WRAPPER_LINE.sub(" ", original)
    cleaned = _WRAPPER_BRACKET.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or original


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
        # Die TypeSafe-API antwortet ohne "model" mit HTTP 422.
        document = {"model": os.environ.get("JEV_SKILL_MODEL", "jev-latest"),
                    **document}
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

def suggest(request: str, roster: list[dict], cfg: Config | None = None) -> dict:
    """Cookbook suggest(): zwei Calls, zwei Schwellen, max. ein Skill-Name.

    Im Zweifel abstain; result["reason"] sagt warum."""
    cfg = cfg or Config()
    by_name = {s["name"]: s for s in roster}
    request = clean_request(request)
    state = {"request": request, "recent_context": ""}
    result = {
        "roster_size": len(roster),
        "thresholds": {"gate": cfg.gate_threshold, "fits": cfg.fits_threshold},
        "suggested": None,
        "decision": "abstain",
    }
    if len(request) < cfg.min_chars:
        result["reason"] = "too_short"
        return result
    if not roster:
        result["reason"] = "empty_roster"
        return result

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

    result["gate"] = round(gate, 3)
    result["gate_values"] = {k: round(v, 3) for k, v in values.items()}
    result["usage_call1"] = resp.get("usage") or {}
    if not ranked:
        result["reason"] = "no_ranking"
        return result
    if gate < cfg.gate_threshold:
        result["reason"] = "gate_below_threshold"
        return result

    # ---- Call 2: Rerank Top-3 mit voller Beschreibung + Body-Exzerpt
    shortlist = tuple(name for name, _p in ranked[:SHORTLIST] if name in by_name)
    result["shortlist"] = shortlist
    if not shortlist:
        result["reason"] = "no_candidates"
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
    # Kein Fallback auf shortlist[0]: ein Gewinner ausserhalb der Shortlist
    # (= ausserhalb des Rosters) ist Zweifel, also nichts.
    if winner not in shortlist:
        result["reason"] = "invalid_winner"
        return result
    if fits.get(winner, 0.0) < cfg.fits_threshold:
        result["reason"] = "fits_below_threshold"
        return result
    if not os.path.isfile(by_name[winner]["path"]):
        result["reason"] = "skill_missing"  # seit Session-Start verschwunden
        return result
    result["decision"] = "suggest"
    result["reason"] = "ok"
    result["suggested"] = winner
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
    cfg = None
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
                    cfg = load_config()
                    roster = filter_roster(load_roster(), cfg)
                started = time.perf_counter()
                res = suggest(request_text, roster, cfg)
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