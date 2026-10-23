"""AI cleanup request, prompt handling and fallbacks; no network."""
import unittest
from unittest import mock

import text_cleanup as tc


def client_answering(content, finish_reason="stop"):
    client = mock.Mock()
    client.with_options.return_value = client
    choice = mock.Mock(finish_reason=finish_reason)
    choice.message.content = content
    client.chat.completions.create.return_value = mock.Mock(choices=[choice])
    return client


class PromptTests(unittest.TestCase):
    def test_default_prompt_is_stored_as_empty(self) -> None:
        self.assertEqual(tc.normalize_cleanup_prompt(tc.DEFAULT_CLEANUP_PROMPT), "")
        self.assertEqual(tc.normalize_cleanup_prompt(tc.DEFAULT_CLEANUP_PROMPT.replace("\n", "\r\n") + "\n"), "")
        self.assertEqual(tc.normalize_cleanup_prompt(None), "")
        self.assertEqual(tc.normalize_cleanup_prompt("  Eigen prompt.  "), "Eigen prompt.")

    def test_overlong_prompt_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            tc.normalize_cleanup_prompt("x" * (tc.MAX_CLEANUP_PROMPT_LENGTH + 1))

    def test_composed_prompt_uses_own_prompt_and_dictionary(self) -> None:
        self.assertEqual(tc.compose_cleanup_prompt(""), tc.DEFAULT_CLEANUP_PROMPT)
        self.assertEqual(tc.compose_cleanup_prompt("Eigen."), "Eigen.")
        composed = tc.compose_cleanup_prompt("", ("Groq", "Clinon"))
        self.assertTrue(composed.startswith(tc.DEFAULT_CLEANUP_PROMPT))
        self.assertTrue(composed.endswith("exactly like this: Groq, Clinon."))

    def test_default_prompt_guards_language_and_instructions(self) -> None:
        prompt = tc.DEFAULT_CLEANUP_PROMPT
        for phrase in ("Translate", "ignore previous instructions", "<transcript>", "nee wacht"):
            self.assertIn(phrase, prompt)


class RequestTests(unittest.TestCase):
    def test_gpt_oss_request_shape(self) -> None:
        client = client_answering("Ik wil morgen afspreken.")
        result = tc.clean_transcript(client, "Eh, ik wil morgen afspreken.", model="openai/gpt-oss-120b", system_prompt="P")
        self.assertEqual(result, "Ik wil morgen afspreken.")
        client.with_options.assert_called_once_with(max_retries=0)
        kwargs = client.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs["messages"][0], {"role": "system", "content": "P"})
        self.assertEqual(kwargs["messages"][1]["content"], "<transcript>\nEh, ik wil morgen afspreken.\n</transcript>")
        self.assertEqual((kwargs["reasoning_effort"], kwargs["include_reasoning"], kwargs["temperature"]), ("low", False, 0.0))
        self.assertLessEqual(kwargs["timeout"], 30)

    def test_other_models_get_no_reasoning_options_and_thinking_is_removed(self) -> None:
        client = client_answering("<think>eerst nadenken</think>\nSchone tekst.")
        result = tc.clean_transcript(client, "Eh schone tekst.", model="qwen/qwen3.8-27b", system_prompt="P")
        self.assertEqual(result, "Schone tekst.")
        kwargs = client.chat.completions.create.call_args.kwargs
        self.assertNotIn("reasoning_effort", kwargs)
        self.assertNotIn("include_reasoning", kwargs)

    def test_echoed_tags_are_removed(self) -> None:
        client = client_answering("<transcript>\nHallo.\n</transcript>")
        self.assertEqual(tc.clean_transcript(client, "Eh hallo.", model="openai/gpt-oss-120b", system_prompt="P"), "Hallo.")

    def test_typographic_characters_and_stray_line_breaks_are_normalized(self) -> None:
        client = client_answering("Een sub\u2011mapje van\u00a050 MB.\nKlaar.")
        prompt = tc.compose_cleanup_prompt("", ("Groq",))
        result = tc.clean_transcript(client, "Een eh sub-mapje van 50 MB. Klaar.", model="openai/gpt-oss-120b", system_prompt=prompt)
        self.assertEqual(result, "Een sub-mapje van 50 MB. Klaar.")
        # An own prompt may ask for line breaks; those stay.
        client = client_answering("- melk\n- kaas")
        self.assertEqual(
            tc.clean_transcript(client, "melk en kaas", model="openai/gpt-oss-120b", system_prompt="Maak een lijst."),
            "- melk\n- kaas",
        )

    def test_empty_text_needs_no_request(self) -> None:
        client = client_answering("x")
        self.assertEqual(tc.clean_transcript(client, "  ", model="m", system_prompt="P"), "  ")
        client.chat.completions.create.assert_not_called()

    def test_unusable_answers_raise_cleanup_error(self) -> None:
        for content, finish_reason in (("", "stop"), (None, "stop"), ("Afgekapt", "length"), ("lang " * 200, "stop")):
            with self.subTest(content=content, finish_reason=finish_reason):
                client = client_answering(content, finish_reason)
                with self.assertRaises(tc.CleanupError):
                    tc.clean_transcript(client, "Korte tekst.", model="openai/gpt-oss-120b", system_prompt="P")

    def test_request_errors_become_readable_cleanup_errors(self) -> None:
        rate_limited = RuntimeError("Error code: 429")
        rate_limited.status_code = 429
        missing = RuntimeError("model_not_found")
        for exc, expected in ((rate_limited, "limiet"), (missing, "niet beschikbaar"), (TimeoutError("timed out"), "mislukt")):
            with self.subTest(expected=expected):
                client = client_answering("x")
                client.chat.completions.create.side_effect = exc
                with self.assertRaisesRegex(tc.CleanupError, expected):
                    tc.clean_transcript(client, "Tekst.", model="openai/gpt-oss-120b", system_prompt="P")


if __name__ == "__main__":
    unittest.main()
