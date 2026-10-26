"""observe_attention and attention_key — a human-facing prompt, apart from answering.

The Auto-Yes answer detector returns None for AskUserQuestion menus and codex's
downgrade menus by design (it must never answer them), so it cannot be the
WAITING signal for a delegate pane (spec 7185 §4.4). `observe_attention` sees
those structurally — two or more numbered options above a live footer — and
adds one guard the detector does not have: an idle composer row below the
prompt means the prompt is quoted or historical, not live.

Fixtures in tests/fixtures/delegates/ are real captures (META.json records
width, title and whether the live dialog hid the composer), except
codex-faster-model, which is synthetic: its composer visibility is recorded as
"unverified", never as an observed boolean. Both directions:
every must-fire has a must-not-fire twin.

Run: .venv/bin/python3 -m unittest tests.test_delegate_attention
"""

import json
import unittest
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "delegates"
AUTOYES_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "autoyes"


def fixture(name):
    return (FIXTURES / f"{name}.txt").read_text().rstrip("\n")


def meta(name):
    return json.loads((FIXTURES / "META.json").read_text())[name]


CLAUDE_IDLE_COMPOSER = (
    "────────────────────────────────────────────────────────────────────────────\n"
    "❯ \n"
    "────────────────────────────────────────────────────────────────────────────\n"
    "  ░░░░░░░░░░ 0.0% (0k/800k) │ 0m                                 ● high · /effort"
)
CODEX_IDLE_COMPOSER = (
    "› Ask Codex to do anything\n"
    "  GPT-6-Astra medium · /tmp/x · GPT-6-Astra · medium · Context 97% left"
)


class ObserveAttentionTests(unittest.TestCase):
    def setUp(self):
        from routes.autoyes import observe_attention
        self.observe = observe_attention

    # --- must fire ---
    def test_a_claude_permission_dialog_is_answerable(self):
        got = self.observe(fixture("claude-permission"), "claude")
        self.assertIsNotNone(got)
        self.assertEqual(got["kind"], "answerable")
        self.assertEqual(got["answer"][0], "numbered-yes")
        self.assertTrue(got["key"])

    def test_an_ask_user_question_is_a_question(self):
        got = self.observe(fixture("claude-ask"), "claude")
        self.assertIsNotNone(got)
        self.assertEqual(got["kind"], "question")
        self.assertIsNone(got["answer"])

    def test_a_codex_approval_is_answerable(self):
        got = self.observe(fixture("codex-approval"), "codex")
        self.assertEqual(got["kind"], "answerable")
        self.assertEqual(got["answer"][0], "numbered-yes")

    def test_the_faster_model_menu_is_a_menu(self):
        got = self.observe(fixture("codex-faster-model"), "codex")
        self.assertIsNotNone(got)
        self.assertEqual(got["kind"], "menu")
        self.assertIsNone(got["answer"], "the downgrade menu must never be answerable")

    # --- must not fire ---
    def test_a_quoted_claude_menu_above_an_idle_composer_is_nothing(self):
        quoted = fixture("claude-ask") + "\n" + CLAUDE_IDLE_COMPOSER
        self.assertIsNone(self.observe(quoted, "claude"))

    def test_a_quoted_codex_menu_above_an_idle_composer_is_nothing(self):
        quoted = fixture("codex-faster-model") + "\n" + CODEX_IDLE_COMPOSER
        self.assertIsNone(self.observe(quoted, "codex"))
        quoted = fixture("codex-approval") + "\n" + CODEX_IDLE_COMPOSER
        self.assertIsNone(self.observe(quoted, "codex"))

    def test_a_selected_option_row_is_not_a_composer(self):
        # "❯ 1. Yes" and "› 1. Yes, proceed" are option rows, not composers:
        # the real dialogs end with them and must still fire.
        self.assertIsNotNone(self.observe(fixture("claude-permission"), "claude"))
        self.assertIsNotNone(self.observe(fixture("codex-approval"), "codex"))

    def test_working_under_a_prose_list_is_nothing(self):
        tail = (
            "• Here is the plan:\n"
            "  1. Read the file\n"
            "  2. Edit the function\n"
            "  3. Run the tests\n"
            "• Working (12m 03s • esc to interrupt)\n"
        )
        self.assertIsNone(self.observe(tail, "codex"))

    def test_a_mid_screen_yn_is_nothing(self):
        tail = (
            "Here is the script:\n"
            "  read -p 'Continue? (y/n) ' answer\n"
            "  echo done\n"
            "● Wrote the script.\n"
        )
        self.assertIsNone(self.observe(tail, "claude"))

    def test_a_shell_pane_is_never_a_menu(self):
        self.assertIsNone(self.observe(fixture("claude-ask"), "shell"))

    def test_the_answer_detector_is_untouched(self):
        from routes.autoyes import _detect_autoyes_prompt
        self.assertIsNone(_detect_autoyes_prompt(fixture("claude-ask"), "claude"))
        self.assertIsNone(_detect_autoyes_prompt(fixture("codex-faster-model"), "codex"))


class FixtureProvenanceTests(unittest.TestCase):
    def test_only_a_live_capture_records_an_observed_composer(self):
        records = json.loads((FIXTURES / "META.json").read_text())
        for name, record in records.items():
            with self.subTest(fixture=name):
                if record["captured"] == "synthetic":
                    self.assertEqual(record["composer_hidden"], "unverified")
                else:
                    self.assertIsInstance(record["composer_hidden"], bool)


class AttentionKeyTests(unittest.TestCase):
    def setUp(self):
        from routes.autoyes import attention_key
        self.key = attention_key

    def test_space_runs_differ(self):
        a = fixture("claude-permission").replace("curl -sI https://example.com | head -3",
                                                 "$ sed 's/  / /g' input.txt")
        b = fixture("claude-permission").replace("curl -sI https://example.com | head -3",
                                                 "$ sed 's/ / /g' input.txt")
        self.assertNotEqual(self.key(a, "claude", 120), self.key(b, "claude", 120))

    def test_a_single_space_differs(self):
        a = fixture("claude-permission").replace("curl -sI", "printf '%s' 'a b'")
        b = fixture("claude-permission").replace("curl -sI", "printf '%s' 'ab'")
        self.assertNotEqual(self.key(a, "claude", 120), self.key(b, "claude", 120))

    def test_a_difference_before_the_last_2000_characters_counts(self):
        base = fixture("codex-approval")
        pad = "\n".join(f"  2. Yes, and don't ask again for commands that start with `x{i:04d}`" for i in range(40))
        a = base.replace("  2. Yes, and don't ask again", pad + "\n  2. Yes, and don't ask again", 1)
        b = a.replace("x0000", "y0000")
        self.assertGreater(len(b) - b.index("y0000"), 2000, "the difference must sit before the last 2000 chars")
        self.assertNotEqual(self.key(a, "codex", 120), self.key(b, "codex", 120))

    def test_a_hard_wrap_gives_the_same_key_as_the_wide_rendering(self):
        wide = (AUTOYES_FIXTURES / "judge3-veto-before.txt").read_text().rstrip("\n")
        narrow = (AUTOYES_FIXTURES / "judge3-narrow.txt").read_text().rstrip("\n")
        self.assertEqual(self.key(wide, "codex", 120), self.key(narrow, "codex", 40))

    def test_a_soft_reflow_gives_a_different_key(self):
        # Word-wrapped to fewer cells than the pane: no width evidence, so the
        # rows stay separate and the key differs. Accepted (spec §4.8).
        base = fixture("codex-approval")
        soft = base.replace("  Reason: May I run this exact command outside the sandbox because the sandbox failed to initialize?",
                            "  Reason: May I run this exact command outside the sandbox\n  because the sandbox failed to initialize?")
        self.assertNotEqual(self.key(base, "codex", 120), self.key(soft, "codex", 120))

    def test_no_prompt_gives_no_key(self):
        self.assertIsNone(self.key("just output\nmore output", "claude", 120))


def _menu(question, transcript="", options=("Staging", "Production")):
    rows = [transcript, ""] if transcript else []
    rows += [question, ""]
    rows += [f"  {n}. {label}" for n, label in enumerate(options, 1)]
    rows.append("Press enter to confirm")
    return "\n".join(rows)


def _anchored_menu(question, options=("Staging", "Production")):
    rule = "─" * 76
    rows = ["earlier output", rule, question, ""]
    rows += [f"  {n}. {label}" for n, label in enumerate(options, 1)]
    rows += [rule, "Enter to select · ↑/↓ to navigate · Esc to cancel"]
    return "\n".join(rows)


class MenuQuestionKeyTests(unittest.TestCase):
    """The question above a menu's options is part of its key, across the blank
    row(s) that separate them (finding 4334): two question pages with the same
    options are different alerts. Unrelated transcript above the question stays
    out, so the same dialog under different scrollback is the same key."""

    def setUp(self):
        from routes.autoyes import attention_key, observe_attention
        self.key = attention_key
        self.observe = observe_attention

    def test_a_changed_question_with_the_same_options_differs_unanchored(self):
        a = _menu("Which deployment target?")
        b = _menu("Which database should be deleted?")
        for kind in ("codex", "gemini"):
            with self.subTest(kind=kind):
                self.assertEqual(self.observe(a, kind, 120)["kind"], "menu")
                self.assertEqual(self.observe(b, kind, 120)["kind"], "menu")
                self.assertNotEqual(self.key(a, kind, 120), self.key(b, kind, 120))

    def test_a_changed_question_with_the_same_options_differs_anchored(self):
        a = _anchored_menu("Which deployment target?")
        b = _anchored_menu("Which database should be deleted?")
        self.assertIsNotNone(self.observe(a, "claude", 120))
        self.assertNotEqual(self.key(a, "claude", 120), self.key(b, "claude", 120))

    def test_the_same_dialog_gives_the_same_key(self):
        a = _menu("Which deployment target?")
        for kind in ("codex", "gemini"):
            with self.subTest(kind=kind):
                self.assertEqual(self.key(a, kind, 120), self.key(_menu("Which deployment target?"), kind, 120))
        self.assertEqual(self.key(_anchored_menu("Q?"), "claude", 120),
                         self.key(_anchored_menu("Q?"), "claude", 120))

    def test_unrelated_transcript_above_the_question_stays_out(self):
        a = _menu("Which deployment target?", transcript="I finished the build step.")
        b = _menu("Which deployment target?", transcript="Something else entirely happened.")
        self.assertEqual(self.key(a, "codex", 120), self.key(b, "codex", 120))
        self.assertEqual(self.key(a, "codex", 120), self.key(_menu("Which deployment target?"), "codex", 120))

    def test_a_hard_wrapped_question_matches_the_wide_rendering(self):
        q = "Which database should be deleted from the production cluster today?"
        wide = _menu(q)
        narrow = _menu(q[:40] + "\n" + q[40:])
        self.assertEqual(len(q[:40]), 40)
        self.assertEqual(self.key(wide, "codex", 120), self.key(narrow, "codex", 40))
        # Without width evidence the cut stays a line break: a different key.
        self.assertNotEqual(self.key(wide, "codex", 120), self.key(narrow, "codex", 120))

    def test_a_ticking_working_line_above_the_blank_row_stays_out(self):
        # Stepping over the blank row must not pull in codex's timer: a key that
        # changes every second could never hold for the WAITING hold.
        m = lambda t: f"• Working ({t} • esc to interrupt)\n\n  1. Staging\n  2. Production\nPress enter to confirm"
        self.assertEqual(self.key(m("2m 14s"), "codex", 120), self.key(m("2m 15s"), "codex", 120))
        q = lambda t: f"• Working ({t} • esc to interrupt)\n\nWhich target?\n\n  1. Staging\n  2. Production\nPress enter to confirm"
        self.assertEqual(self.key(q("2m 14s"), "codex", 120), self.key(q("2m 15s"), "codex", 120))
        self.assertNotEqual(self.key(q("2m 14s"), "codex", 120), self.key(m("2m 14s"), "codex", 120))
        two = lambda t: f"• Working ({t} • esc to interrupt)\n  thinking about it\n\n  1. Staging\n  2. Production\nPress enter to confirm"
        self.assertEqual(self.key(two("2m 14s"), "codex", 120), self.key(two("2m 15s"), "codex", 120))

    def test_a_question_that_mentions_esc_to_interrupt_is_still_the_question(self):
        # Only a working-chrome ROW is excluded, never a question that names the phrase.
        a = _menu("Which handler should emit esc to interrupt?")
        b = _menu("Which test should assert esc to interrupt?")
        for kind in ("codex", "gemini"):
            with self.subTest(kind=kind):
                self.assertNotEqual(self.key(a, kind, 120), self.key(b, kind, 120))
                self.assertEqual(self.key(a, kind, 120),
                                 self.key(_menu("Which handler should emit esc to interrupt?"), kind, 120))
                self.assertNotEqual(self.key(a, kind, 120), self.key(_menu("Q?"), kind, 120))

    def test_a_one_word_question_shaped_like_chrome_is_still_the_question(self):
        a, b = _menu("Confirm (esc to interrupt)"), _menu("Proceed (esc to interrupt)")
        self.assertNotEqual(self.key(a, "codex", 120), self.key(b, "codex", 120))

    def test_claude_thinking_chrome_above_the_blank_row_stays_out(self):
        m = lambda t: f"✻ Pondering… ({t} · ↑ 1.2k tokens · esc to interrupt)\n\n  1. Staging\n  2. Production\nPress enter to confirm"
        self.assertEqual(self.key(m("12s"), "codex", 120), self.key(m("13s"), "codex", 120))

    def test_a_question_directly_above_the_options_is_unchanged(self):
        # The faster-model menu: its question paragraph touches option 1.
        base = fixture("codex-faster-model")
        changed = base.replace("Our systems are thinking", "Our servers are thinking")
        self.assertNotEqual(self.key(base, "codex", 120), self.key(changed, "codex", 120))
        moved = base.replace("  /review - review any changes", "  /review - look at changes")
        self.assertEqual(self.key(base, "codex", 120), self.key(moved, "codex", 120))


if __name__ == "__main__":
    unittest.main()
