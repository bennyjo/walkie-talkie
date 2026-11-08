import os
import subprocess
import sys
import unittest

from walkie_gateway.text import (
    MAX_CHARS,
    NOTHING_SPEAKABLE,
    ON_SCREEN,
    TRUNCATED,
    SpeechFilter,
    clean_line,
    kind,
    sanitize_prompt,
    spell_numbers,
    trails_off,
)


def spoken(text, piece=7):
    """What a reply reads as, fed in small pieces the way the mod sends it."""
    speech = SpeechFilter()
    out = []
    for start in range(0, len(text), piece):
        out.extend(speech.feed(text[start:start + piece]))
    return out + speech.finish()


class SpeechFilterTest(unittest.TestCase):
    def test_sentences_come_out_before_the_line_ends(self):
        speech = SpeechFilter()
        self.assertEqual(speech.feed("Done. The tests"), ["Done."])
        self.assertEqual(speech.finish(), ["The tests"])

    def test_pieces_make_no_difference(self):
        text = "The build passed. It took two minutes, as usual.\nNo action needed."
        whole = spoken(text, piece=len(text))
        for size in (1, 3, 11):
            self.assertEqual(spoken(text, piece=size), whole)

    def test_a_long_first_sentence_is_cut_at_its_first_clause(self):
        units = spoken("I checked the session router carefully, and it now reports what happened clearly.")
        self.assertEqual(units[0], "I checked the session router carefully,")

    def test_only_the_first_sentence_is_cut(self):
        units = spoken("Done. I checked the session router carefully, and it now reports what happened clearly.")
        self.assertEqual(units[1], "I checked the session router carefully, and it now reports what happened clearly.")

    def test_code_blocks_are_not_read_only_mentioned(self):
        self.assertEqual(spoken("Here it is.\n```python\nprint('secret')\n```\nThat's all."),
                         ["Here it is.", ON_SCREEN, "That's all."])

    def test_a_sentence_inside_a_code_block_is_not_peeled(self):
        speech = SpeechFilter()
        self.assertEqual(speech.feed("```\nDone. This is code. And more"), [])
        self.assertEqual(speech.feed(" of it.\n```\nReal words."), [])
        self.assertEqual(speech.finish(), [ON_SCREEN, "Real words."])

    def test_tables_diffs_and_keys_are_dropped(self):
        self.assertEqual(spoken("| a | b |\n| 1 | 2 |\nAfter."), [ON_SCREEN, "After."])
        self.assertEqual(spoken("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n\nAfter."), [ON_SCREEN, "After."])
        self.assertEqual(spoken("-----BEGIN KEY-----\nabc\n-----END KEY-----\nAfter."), [ON_SCREEN, "After."])

    def test_a_plain_dash_list_is_read(self):
        self.assertEqual(spoken("- first item\n- second item"), ["first item", "second item"])

    def test_inline_code_paths_and_urls_leave_a_placeholder(self):
        self.assertEqual(clean_line("See `foo()` in /etc/hosts at https://example.com now."),
                         "See something on screen in something on screen at something on screen now.")

    def test_a_confirmation_never_reads_as_if_it_was_whole(self):
        # What the adversarial review heard: each of these asked for a yes with the target removed.
        cases = {
            "Should I run `git push --force origin main`? Say yes to confirm.":
                ["Should I run something on screen?", "Say yes to confirm."],
            "I'm about to delete `~/work/prod-db`. Say yes to confirm.":
                ["I'm about to delete something on screen.", "Say yes to confirm."],
            "Do not run `rm -rf build`, run `make clean` instead. Say yes to go ahead.":
                ["Do not run something on screen, run something on screen instead.", "Say yes to go ahead."],
            "I will delete /Users/me/prod and keep /Users/me/test. Is that a yes?":
                ["I will delete something on screen and keep something on screen.", "Is that a yes?"],
        }
        for reply, heard in cases.items():
            self.assertEqual(" ".join(spoken(reply)), " ".join(heard))

    def test_formatting_tags_leave_the_words_reading_cleanly(self):
        self.assertEqual(spoken("Use <b>this</b> one."), ["Use this one."])
        self.assertEqual(spoken("Use <STRONG>this</STRONG>."), ["Use this."])
        self.assertEqual(spoken("It's fine.<br>Next step is clear."), ["It's fine.", "Next step is clear."])
        self.assertEqual(spoken("It's fine.<br />Next step is clear."), ["It's fine.", "Next step is clear."])
        self.assertEqual(spoken("""Use <a href="x" title='a b'>this</a> one."""), ["Use this one."])

    def test_any_other_tag_leaves_a_placeholder(self):
        self.assertEqual(spoken("Should I delete <prod-db>? Say yes to confirm."),
                         ["Should I delete something on screen?", "Say yes to confirm."])
        self.assertEqual(spoken("Should I delete <a staging branch>? Say yes to confirm."),
                         ["Should I delete something on screen?", "Say yes to confirm."])
        self.assertEqual(clean_line("Is i<a and b>c true?"), "Is i something on screen c true?")

    def test_adjacent_placeholders_collapse_into_one(self):
        self.assertEqual(clean_line("Delete <prod-db><test-db> now."), "Delete something on screen now.")

    def test_a_placeholder_never_fuses_with_the_words_beside_it(self):
        for line in ("Delete<prod-db>now.", "Delete`rm`now.", "Delete![x](y)now.", "Delete$HOME now."):
            self.assertEqual(clean_line(line), "Delete something on screen now.")

    def test_a_code_block_a_confirmation_points_at_is_mentioned(self):
        self.assertEqual(spoken("```\nrm -rf /srv/prod\n```\nThat is what I'll run. Say yes to confirm."),
                         [ON_SCREEN, "That is what I'll run.", "Say yes to confirm."])

    def test_a_code_block_at_the_end_of_a_reply_is_mentioned(self):
        self.assertEqual(spoken("I'll run this.\n\n```\nrm -rf /srv/prod\n```"), ["I'll run this.", ON_SCREEN])

    def test_a_line_that_is_only_code_is_mentioned_once(self):
        self.assertEqual(spoken("First.\n`one`\n\n`two`, `three`\nLast."), ["First.", ON_SCREEN, "Last."])

    def test_a_tag_that_never_closes_returns_quickly(self):
        # The gateway can't hear a tap while this runs, and the pattern once doubled with each attribute.
        # In a process of its own, because a pattern that backtracks can't be interrupted from inside.
        lines = [
            "<div " + 'a="v" ' * 5000 + "and more words.",
            "<div " + 'a="v" ' * 5000 + "disabled>",
            "<div " + "disabled " * 5000 + ">",
            "<div " + "a='v' " * 5000 + "and more words.",
            "<div " + "a=v " * 5000 + "and more words.",
        ]
        code = "import sys\nfrom walkie_gateway.text import clean_line\nfor line in sys.argv[1:]: clean_line(line)"
        subprocess.run([sys.executable, "-c", code, *lines], check=True, timeout=5,
                       cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    def test_formatting_tags_with_attributes_still_read_cleanly(self):
        self.assertEqual(clean_line("""Use <a href="x y" class='z' id=w>this</a> one."""), "Use this one.")

    def test_a_rule_is_not_something_on_screen(self):
        self.assertEqual(spoken("First.\n---\nLast."), ["First.", "Last."])

    def test_links_keep_their_words_and_plain_slashes_stay(self):
        self.assertEqual(clean_line("Use [the guide](https://example.com) and/or ask."), "Use the guide and/or ask.")

    def test_secrets_are_dropped(self):
        self.assertNotIn("ghp_", clean_line("The token is ghp_abcdef123456 here."))

    def test_never_cuts_inside_inline_code(self):
        speech = SpeechFilter()
        self.assertEqual(speech.feed("Run `a. B` now"), [])

    def test_a_reply_with_nothing_to_read_says_so(self):
        self.assertEqual(spoken("```\ncode\n```"), [NOTHING_SPEAKABLE])
        self.assertEqual(spoken("`only code`"), [NOTHING_SPEAKABLE])
        self.assertEqual(spoken("https://example.com"), [NOTHING_SPEAKABLE])
        self.assertEqual(spoken(""), [])

    def test_a_long_reply_stops_at_the_cap(self):
        units = spoken("This sentence goes on for a little while. " * 100)
        self.assertEqual(units[-1], TRUNCATED)
        self.assertLessEqual(sum(len(u) for u in units[:-1]), MAX_CHARS)

    def test_numbers_are_spelled(self):
        self.assertEqual(spell_numbers("2 plus 2 is 4"), "two plus two is four")
        self.assertEqual(spell_numbers("$3.50"), "three dollars and fifty cents")


class UtteranceTest(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(kind("um"), "noise")
        self.assertEqual(kind("a"), "noise")
        self.assertEqual(kind("Oh never mind, cancel."), "cancel")
        self.assertEqual(kind("Cancel."), "cancel")
        self.assertEqual(kind("Which of these jobs should I cancel"), "prompt")
        self.assertEqual(kind("Type it."), "confirm-typing")
        self.assertEqual(kind("Please type it into the notes file."), "prompt")
        self.assertEqual(kind("What's the status?"), "prompt")
        self.assertEqual(kind("Yes."), "yes")
        self.assertEqual(kind("Um, yes!"), "yes")
        self.assertEqual(kind("Yes?"), "prompt")
        self.assertEqual(kind("Oh, yes?"), "prompt")
        self.assertEqual(kind("Yes -"), "prompt")
        self.assertEqual(kind("Yes..."), "prompt")
        self.assertEqual(kind("Yes please."), "prompt")
        self.assertEqual(kind("Yes, but only after the build passes."), "prompt")
        self.assertEqual(kind("No."), "no")
        self.assertEqual(kind("Um, no!"), "no")
        self.assertEqual(kind("No?"), "prompt")
        self.assertEqual(kind("No -"), "prompt")
        self.assertEqual(kind("No, wait for the build."), "prompt")
        self.assertEqual(kind("Yeah."), "prompt")

    def test_trailing_words(self):
        self.assertTrue(trails_off("I want to fix that and"))
        self.assertFalse(trails_off("I want to fix that."))

    def test_a_full_stop_does_not_make_a_fragment_finished(self):
        self.assertTrue(trails_off("And maybe we should."))
        self.assertTrue(trails_off("Move it into the."))
        self.assertTrue(trails_off("So."))
        self.assertFalse(trails_off("Run the tests again."))

    def test_a_prompt_can_never_be_a_command(self):
        self.assertEqual(sanitize_prompt("/clear the log"), "clear the log")
        self.assertEqual(sanitize_prompt("!rm -rf x"), "rm -rf x")
        self.assertEqual(sanitize_prompt("# note"), "note")
        self.assertEqual(sanitize_prompt("@file please"), "file please")
        self.assertEqual(sanitize_prompt("one\ntwo\tthree\x07"), "one two three")


if __name__ == "__main__":
    unittest.main()
