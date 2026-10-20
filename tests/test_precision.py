"""Regression tests for skill_select precision. No network: call_adapter is faked.

Run: python -m unittest discover -s tests -v
"""
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from jev_skill_router import mcp_server as ms  # noqa: E402

SKILLS = {
    # (category dir, name, description)
    ("", "amazon-seller-orders"): "Check and report Amazon seller orders.",
    ("devops", "hermes-cron-ops"): "Create and manage Hermes cron jobs.",
    ("devops", "goncloud-local-chains"): "Operate local goncloud agent chains.",
    ("productivity", "apple-reminders"): "Manage Apple Reminders lists.",
    ("productivity", "inbox-gmail-cleanup"): "Limpieza de inbox Gmail.",
    ("misc", "getting-started"): "Getting started with Hermes.",
    ("misc", "interrogate"): "Interrogate a codebase with questions.",
    ("creative", "humanizer"): "Rewrite text to sound human.",
}

REAL_BAD_MESSAGES = {
    "Revisa si ya quedo?": "apple-reminders",
    "Ok avisame cuando este al 100 y limpio": "inbox-gmail-cleanup",
    "Listo": "getting-started",
    "Dame lista de los problemas para que Claude lo revise": "interrogate",
    "Ok no limpies basura deja lo que copiaste limpio": "humanizer",
    "si clona todos los repos que falten en PC": "multi-machine-repo-sync",
}
POSITIVE = {
    "revisa las ordenes de Amazon de hoy": "amazon-seller-orders",
    "crea un cron diario": "hermes-cron-ops",
    "opera una cadena goncloud": "goncloud-local-chains",
}


def make_root(tmp):
    for (cat, name), desc in SKILLS.items():
        d = os.path.join(tmp, cat, name) if cat else os.path.join(tmp, name)
        os.makedirs(d)
        with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write(f"---\nname: {name}\ndescription: {desc}\n---\nBody of {name}.\n")
    return tmp


class FakeJev:
    """Stands in for the backend. `plan` maps request -> (gate, winner, fits).

    Scores model a mediocre-but-above-0.30 match (what slipped through live)
    unless the plan says otherwise. Records every call."""

    def __init__(self, plan):
        self.plan = plan
        self.calls = []

    def __call__(self, document):
        self.calls.append(document)
        request = document["state"]["request"]
        gate, winner, fit = self.plan[request]
        qs = document["questions"]
        if any(k.startswith("gate::") for k in qs):
            answers = {"which": {"probabilities": {winner: 0.6, "interrogate": 0.2,
                                                   "humanizer": 0.1}}}
            for key in qs:
                if key.startswith("gate::"):
                    inverted = key == "gate::prose_suffices"
                    answers[key] = {"noul": (1 - gate) if inverted else gate}
            return {"answers": answers, "usage": {"calls": 1}}
        fits = {k.removeprefix("fits::"): 0.05 for k in qs if k.startswith("fits::")}
        if winner in fits:
            fits[winner] = fit
        return {"answers": {"which": {"choice": winner},
                            **{f"fits::{n}": {"noul": v} for n, v in fits.items()}},
                "usage": {"calls": 2}}


class PrecisionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = make_root(tmp.name)
        self.roster = ms.load_roster(self.root)

    def run_suggest(self, plan, request, cfg=None):
        fake = FakeJev(plan)
        with mock.patch.object(ms, "call_adapter", fake):
            return ms.suggest(request, self.roster, cfg), fake

    # ---- the six live failures
    def test_real_bad_messages_never_suggest_the_listed_skill(self):
        # Backend is "fairly sure" (0.55): above the old 0.30, below the new 0.7.
        plan = {m: (0.55, s, 0.55) for m, s in REAL_BAD_MESSAGES.items()}
        for message, bad in REAL_BAD_MESSAGES.items():
            with self.subTest(message=message):
                res, _ = self.run_suggest(plan, message)
                self.assertNotEqual(res["suggested"], bad)
                self.assertEqual(res["decision"], "abstain")

    def test_old_thresholds_would_have_suggested_them(self):
        # Discriminating check: the same fixtures DO produce the bad suggestion
        # at the old 0.30 thresholds, so the test above really exercises the fix.
        old = ms.Config(gate_threshold=0.30, fits_threshold=0.30, min_chars=0)
        plan = {m: (0.55, s, 0.55) for m, s in REAL_BAD_MESSAGES.items()
                if s in {x["name"] for x in self.roster}}
        for message in plan:
            with self.subTest(message=message):
                res, _ = self.run_suggest(plan, message, old)
                self.assertEqual(res["suggested"], plan[message][1])

    def test_skill_outside_roster_is_not_suggested(self):
        msg = "si clona todos los repos que falten en PC"
        # high scores and a winner that is not in the roster: must abstain,
        # not fall back to shortlist[0].
        res, _ = self.run_suggest({msg: (0.95, "multi-machine-repo-sync", 0.95)}, msg)
        self.assertEqual(res["decision"], "abstain")
        self.assertIsNone(res["suggested"])

    def test_winner_outside_shortlist_abstains(self):
        msg = "revisa las ordenes de Amazon de hoy"
        fake = FakeJev({msg: (0.95, "amazon-seller-orders", 0.95)})
        base = fake.__call__

        def ghost_winner(doc):
            resp = base(doc)
            if "fits::amazon-seller-orders" in doc["questions"]:
                resp["answers"]["which"]["choice"] = "ghost-skill"
            return resp

        with mock.patch.object(ms, "call_adapter", ghost_winner):
            res = ms.suggest(msg, self.roster)
        self.assertEqual(res["reason"], "invalid_winner")
        self.assertIsNone(res["suggested"])

    def test_winner_own_fit_must_pass_not_just_any_candidate(self):
        msg = "revisa las ordenes de Amazon de hoy"
        fake = FakeJev({msg: (0.95, "amazon-seller-orders", 0.2)})
        base = fake.__call__

        def other_fits_high(doc):
            resp = base(doc)
            for k in list(resp["answers"]):
                if k.startswith("fits::") and k != "fits::amazon-seller-orders":
                    resp["answers"][k] = {"noul": 0.99}
            return resp

        with mock.patch.object(ms, "call_adapter", other_fits_high):
            res = ms.suggest(msg, self.roster)
        self.assertEqual(res["decision"], "abstain")
        self.assertEqual(res["reason"], "fits_below_threshold")

    def test_skill_deleted_since_session_start_abstains(self):
        msg = "revisa las ordenes de Amazon de hoy"
        entry = next(s for s in self.roster if s["name"] == "amazon-seller-orders")
        os.remove(entry["path"])
        res, _ = self.run_suggest({msg: (0.95, entry["name"], 0.95)}, msg)
        self.assertEqual(res["reason"], "skill_missing")
        self.assertIsNone(res["suggested"])

    # ---- clear positives
    def test_clear_positives_are_suggested(self):
        plan = {m: (0.92, s, 0.9) for m, s in POSITIVE.items()}
        for message, skill in POSITIVE.items():
            with self.subTest(message=message):
                res, fake = self.run_suggest(plan, message)
                self.assertEqual(res["decision"], "suggest")
                self.assertEqual(res["suggested"], skill)
                self.assertEqual(len(fake.calls), 2)

    # ---- short messages / wrappers
    def test_short_message_skips_backend(self):
        res, fake = self.run_suggest({}, "Listo")
        self.assertEqual(res["reason"], "too_short")
        self.assertEqual(fake.calls, [])

    def test_fourteen_chars_skipped_fifteen_goes_through(self):
        plan = {"a" * 15: (0.95, "interrogate", 0.95)}
        res, fake = self.run_suggest(plan, "a" * 14)
        self.assertEqual(fake.calls, [])
        res, fake = self.run_suggest(plan, "a" * 15)
        self.assertEqual(len(fake.calls), 2)

    def test_wrapped_short_message_is_still_short(self):
        wrapped = ("[Telegram Gon (id 1) 2026-10-19 10:00]\n"
                   "Jev sugiere: tipo=codigo (1.00), modo=directo (0.31)\n"
                   "Listo")
        res, fake = self.run_suggest({}, wrapped)
        self.assertEqual(res["reason"], "too_short")
        self.assertEqual(fake.calls, [])

    def test_wrapper_stripped_before_backend_but_user_text_kept(self):
        real = "revisa las ordenes de Amazon de hoy [ ] y tambien [x] las de ayer"
        wrapped = (
            "[Telegram Gon (id 1) 2026-10-19 10:00]\n"
            "[Replying to: Gon: algo anterior]\n"
            "[The user sent an image]\n"
            "[System note: the gateway restarted]\n"
            "JEV skill_select: suggested=none\n"
            "Jev cierre: ok\n"
            + real
        )
        fake = FakeJev({real: (0.95, "amazon-seller-orders", 0.95)})
        with mock.patch.object(ms, "call_adapter", fake):
            res = ms.suggest(wrapped, self.roster)
        self.assertEqual(fake.calls[0]["state"]["request"], real)
        self.assertEqual(res["suggested"], "amazon-seller-orders")

    def test_clean_request_falls_back_to_original_when_empty(self):
        only_wrapper = "Jev sugiere: tipo=codigo (1.00)"
        self.assertEqual(ms.clean_request(only_wrapper), only_wrapper)
        self.assertEqual(ms.clean_request("[Telegram x]"), "[Telegram x]")

    def test_inline_bracket_header_removed_keeps_following_text(self):
        self.assertEqual(
            ms.clean_request("[Telegram Gon 10:00] crea un cron diario"),
            "crea un cron diario")

    # ---- roster scoping
    def test_allow_and_deny_by_name_and_category(self):
        names = lambda cfg: {s["name"] for s in ms.filter_roster(self.roster, cfg)}  # noqa: E731
        self.assertEqual(names(ms.Config()), {s["name"] for s in self.roster})
        self.assertEqual(names(ms.Config(allow=("category:devops",))),
                         {"hermes-cron-ops", "goncloud-local-chains"})
        self.assertEqual(names(ms.Config(deny=("category:creative", "category:misc"))),
                         {n for (_c, n) in SKILLS} - {"humanizer", "getting-started",
                                                     "interrogate"})
        self.assertEqual(
            names(ms.Config(allow=("category:devops", "amazon-*"),
                            deny=("goncloud-*",))),
            {"hermes-cron-ops", "amazon-seller-orders"})

    def test_denied_skill_is_never_sent_to_backend_nor_suggested(self):
        cfg = ms.Config(deny=("category:productivity",))
        self.roster = ms.filter_roster(self.roster, cfg)
        msg = "Revisa si ya quedo? con recordatorios"
        res, fake = self.run_suggest({msg: (0.95, "apple-reminders", 0.95)}, msg, cfg)
        sent = fake.calls[0]["questions"]["which"]["criteria"]
        self.assertNotIn("apple-reminders", sent)
        self.assertNotIn("inbox-gmail-cleanup", sent)
        self.assertIsNone(res["suggested"])

    def test_empty_roster_after_filter_abstains_without_calls(self):
        self.roster = ms.filter_roster(self.roster, ms.Config(allow=("no-such-skill",)))
        self.assertEqual(self.roster, [])
        res, fake = self.run_suggest({}, "crea un cron diario de reportes")
        self.assertEqual(res["reason"], "empty_roster")
        self.assertEqual(fake.calls, [])

    def test_category_from_directory_and_frontmatter(self):
        by = {s["name"]: s["category"] for s in self.roster}
        self.assertEqual(by["amazon-seller-orders"], "")
        self.assertEqual(by["hermes-cron-ops"], "devops")
        d = os.path.join(self.root, "x", "fm-skill")
        os.makedirs(d)
        with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nname: fm-skill\ncategory: custom\ndescription: d\n---\nb\n")
        by = {s["name"]: s["category"] for s in ms.load_roster(self.root)}
        self.assertEqual(by["fm-skill"], "custom")


class ConfigTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = tmp.name

    def write_json(self, data):
        with open(os.path.join(self.home, ms.CONFIG_FILENAME), "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def test_defaults(self):
        cfg = ms.load_config(env={}, home=self.home)
        self.assertEqual((cfg.gate_threshold, cfg.fits_threshold, cfg.min_chars),
                         (0.7, 0.7, 15))
        self.assertEqual((cfg.allow, cfg.deny), ((), ()))

    def test_json_file_then_env_overrides(self):
        self.write_json({"gate_threshold": 0.5, "fits_threshold": 0.6, "min_chars": 20,
                         "allow": ["category:devops"], "deny": ["x"]})
        cfg = ms.load_config(env={}, home=self.home)
        self.assertEqual((cfg.gate_threshold, cfg.fits_threshold, cfg.min_chars),
                         (0.5, 0.6, 20))
        self.assertEqual((cfg.allow, cfg.deny), (("category:devops",), ("x",)))
        cfg = ms.load_config(env={"JEV_SKILL_GATE_THRESHOLD": "0.8",
                                  "JEV_SKILL_ALLOW": "a, b",
                                  "JEV_SKILL_DENY": "category:media"}, home=self.home)
        self.assertEqual(cfg.gate_threshold, 0.8)
        self.assertEqual(cfg.fits_threshold, 0.6)  # still from the file
        self.assertEqual(cfg.allow, ("a", "b"))
        self.assertEqual(cfg.deny, ("category:media",))

    def test_explicit_config_path_env(self):
        path = os.path.join(self.home, "custom.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"fits_threshold": 0.9}, fh)
        cfg = ms.load_config(env={"JEV_SKILL_ROUTER_CONFIG": path}, home=self.home)
        self.assertEqual(cfg.fits_threshold, 0.9)

    def test_invalid_values_fall_back_to_defaults(self):
        with mock.patch.object(ms, "_warn"):
            cfg = ms.load_config(env={"JEV_SKILL_GATE_THRESHOLD": "abc",
                                      "JEV_SKILL_FITS_THRESHOLD": "7",
                                      "JEV_SKILL_MIN_CHARS": "x"}, home=self.home)
        self.assertEqual((cfg.gate_threshold, cfg.fits_threshold, cfg.min_chars),
                         (0.7, 0.7, 15))

    def test_broken_json_is_not_fatal(self):
        with open(os.path.join(self.home, ms.CONFIG_FILENAME), "w") as fh:
            fh.write("{nope")
        with mock.patch.object(ms, "_warn"):
            cfg = ms.load_config(env={}, home=self.home)
        self.assertEqual(cfg, ms.Config())

    def test_threshold_is_honoured_by_suggest(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        roster = ms.load_roster(make_root(tmp.name))
        msg = "revisa las ordenes de Amazon de hoy"
        fake = FakeJev({msg: (0.8, "amazon-seller-orders", 0.8)})
        with mock.patch.object(ms, "call_adapter", fake):
            self.assertEqual(ms.suggest(msg, roster)["decision"], "suggest")
            strict = ms.Config(fits_threshold=0.85)
            self.assertEqual(ms.suggest(msg, roster, strict)["reason"],
                             "fits_below_threshold")
            strict = ms.Config(gate_threshold=0.85)
            self.assertEqual(ms.suggest(msg, roster, strict)["reason"],
                             "gate_below_threshold")


class McpInterfaceTests(unittest.TestCase):
    """The MCP surface (tool name + arguments) must not change."""

    def run_server(self, messages, env):
        stdin = io.StringIO("".join(json.dumps(m) + "\n" for m in messages))
        out = io.BytesIO()
        stdout = mock.Mock(buffer=out)
        with mock.patch.object(sys, "stdin", stdin), \
                mock.patch.object(sys, "stdout", stdout), \
                mock.patch.dict(os.environ, env):
            ms.serve_stdio()
        return [json.loads(line) for line in out.getvalue().decode().splitlines()]

    def test_tool_schema_unchanged(self):
        out = self.run_server([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}], {})
        tool = out[0]["result"]["tools"][0]
        self.assertEqual(tool["name"], "skill_select")
        self.assertEqual(tool["inputSchema"]["required"], ["request"])
        self.assertEqual(list(tool["inputSchema"]["properties"]), ["request"])

    def test_end_to_end_with_scoped_roster(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = make_root(os.path.join(tmp.name, "skills"))
        msg = "crea un cron diario de reportes"
        fake = FakeJev({msg: (0.95, "hermes-cron-ops", 0.95)})
        call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "skill_select", "arguments": {"request": msg}}}
        env = {"JEV_SKILL_ALLOW": "category:devops", "HERMES_HOME": tmp.name}
        with mock.patch.object(ms, "SKILLS_ROOT", root), \
                mock.patch.object(ms, "call_adapter", fake):
            out = self.run_server([call], env)
        res = json.loads(out[0]["result"]["content"][0]["text"])
        self.assertEqual(res["suggested"], "hermes-cron-ops")
        self.assertEqual(res["roster_size"], 2)
        self.assertIn("hermes-cron-ops", res["suggestion_block"])


class SkillsRootTests(unittest.TestCase):
    """_skills_root: JEV_SKILLS_ROOT > HERMES_HOME > perfil del plugin > ~/.hermes."""

    def make_profile(self, with_skills=True):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        profile = os.path.join(tmp.name, "claw")
        module = os.path.join(profile, "plugins", "jev-skill-router-mcp",
                              "src", "jev_skill_router", "mcp_server.py")
        os.makedirs(os.path.dirname(module))
        if with_skills:
            os.makedirs(os.path.join(profile, "skills"))
        return profile, module

    def test_explicit_root_wins_over_everything(self):
        profile, module = self.make_profile()
        env = {"JEV_SKILLS_ROOT": "/x/skills", "HERMES_HOME": "/h"}
        self.assertEqual(ms._skills_root(env, module), "/x/skills")

    def test_hermes_home_when_no_explicit_root(self):
        profile, module = self.make_profile()
        self.assertEqual(ms._skills_root({"HERMES_HOME": "/h"}, module),
                         "/h/skills")

    def test_owner_profile_when_hermes_home_missing(self):
        profile, module = self.make_profile()
        got = ms._skills_root({}, module)
        self.assertEqual(os.path.normcase(os.path.abspath(got)),
                         os.path.normcase(os.path.join(profile, "skills")))

    def test_falls_back_to_home_outside_a_profile(self):
        # Checkout normal (kein .../plugins/<plugin>/): nie das Repo-eigene skills/
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        module = os.path.join(tmp.name, "repo", "src", "jev_skill_router",
                              "mcp_server.py")
        os.makedirs(os.path.dirname(module))
        os.makedirs(os.path.join(tmp.name, "skills"))
        self.assertEqual(ms._skills_root({}, module),
                         os.path.expanduser("~/.hermes") + "/skills")

    def test_falls_back_when_profile_has_no_skills_dir(self):
        profile, module = self.make_profile(with_skills=False)
        self.assertEqual(ms._skills_root({}, module),
                         os.path.expanduser("~/.hermes") + "/skills")


class ModelPayloadTests(unittest.TestCase):
    """Direct mode must send "model" (TypeSafe answers HTTP 422 without it)."""

    def send(self, document, env=None):
        seen = {}

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"{}"

        def fake_urlopen(req, timeout=None):
            seen["body"] = json.loads(req.data.decode("utf-8"))
            return Resp()

        with mock.patch.object(ms, "ADAPTER_URL", ""),                 mock.patch.object(ms, "TYPESAFE_API_KEY", "k"),                 mock.patch.dict(os.environ, env or {}, clear=False),                 mock.patch.object(ms.urllib.request, "urlopen", fake_urlopen):
            if not env or "JEV_SKILL_MODEL" not in env:
                os.environ.pop("JEV_SKILL_MODEL", None)
            ms.call_adapter(document)
        return seen["body"]

    def test_default_model_is_sent(self):
        body = self.send({"state": {}, "questions": []})
        self.assertEqual(body["model"], "jev-latest")
        self.assertEqual(body["questions"], [])

    def test_env_model_overrides_default(self):
        body = self.send({"state": {}, "questions": []},
                         {"JEV_SKILL_MODEL": "jev-x"})
        self.assertEqual(body["model"], "jev-x")

    def test_model_in_document_is_not_overwritten(self):
        body = self.send({"model": "mine", "state": {}, "questions": []})
        self.assertEqual(body["model"], "mine")


if __name__ == "__main__":
    unittest.main()
