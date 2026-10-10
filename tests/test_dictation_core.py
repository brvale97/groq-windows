import unittest
import unicodedata

from dictation_core import (
    PARAGRAPH_SEPARATOR,
    DictionaryValidationError,
    append_trailing_space,
    apply_final_period_preference,
    apply_word_replacements,
    clipboard_text,
    compose_transcription_prompt,
    format_paragraphs,
    normalize_custom_word,
    normalize_custom_words,
    normalize_word_replacements,
)


class FinalPeriodPreferenceTests(unittest.TestCase):
    def test_keeps_final_period_when_setting_is_disabled(self) -> None:
        self.assertEqual(
            apply_final_period_preference("Dit is de laatste zin.", remove_final_period=False),
            "Dit is de laatste zin.",
        )

    def test_removes_final_period_when_setting_is_enabled(self) -> None:
        self.assertEqual(
            apply_final_period_preference("Dit is de laatste zin.", remove_final_period=True),
            "Dit is de laatste zin",
        )

    def test_never_shortens_an_ellipsis(self) -> None:
        self.assertEqual(
            apply_final_period_preference("Even denken...", remove_final_period=True),
            "Even denken...",
        )


class ParagraphTests(unittest.TestCase):
    MESSAGE = (
        "Hoi Jan, ik wilde even laten weten dat de offerte bijna klaar is. Ik heb bijv. de prijzen al aangepast. "
        "Het enige wat nog mist is de planning voor volgende week. Die stuur ik morgen. "
        "Verder wilde ik vragen of je donderdag tijd hebt om even te bellen. Dan kunnen we de details doornemen. "
        "Groetjes, Bram."
    )

    def test_splits_a_long_dictation_into_chat_paragraphs(self) -> None:
        self.assertEqual(
            format_paragraphs(self.MESSAGE),
            PARAGRAPH_SEPARATOR.join((
                "Hoi Jan, ik wilde even laten weten dat de offerte bijna klaar is. Ik heb bijv. de prijzen al aangepast.",
                "Het enige wat nog mist is de planning voor volgende week. Die stuur ik morgen.",
                "Verder wilde ik vragen of je donderdag tijd hebt om even te bellen. Dan kunnen we de details doornemen.",
                "Groetjes, Bram.",
            )),
        )

    def test_only_whitespace_between_sentences_changes(self) -> None:
        self.assertEqual(format_paragraphs(self.MESSAGE).split(), self.MESSAGE.split())

    def test_short_messages_stay_one_block(self) -> None:
        for text in ("Kort bericht. Echt kort. Klaar.", "Een lange zin " * 30, "Zin een.\nZin twee."):
            self.assertEqual(format_paragraphs(text), text)

    def test_paragraphs_stay_short(self) -> None:
        text = " ".join(f"Dit is zin nummer {index} met wat extra woorden erbij." for index in range(1, 21))
        paragraphs = format_paragraphs(text).split(PARAGRAPH_SEPARATOR)
        self.assertGreater(len(paragraphs), 5)
        for paragraph in paragraphs:
            self.assertLessEqual(paragraph.count("."), 3)
            self.assertLessEqual(len(paragraph), 260)

    def test_never_breaks_inside_numbers_or_abbreviations(self) -> None:
        text = (
            "Het kost 3.5 euro per stuk en dat is o.a. door de transportkosten. Dhr. Jansen vond dat te veel. "
            "Hij wil bijv. korting. Wat vind jij daarvan? Laat het me weten! Dan kan ik het doorgeven aan hem."
        )
        for paragraph in format_paragraphs(text).split(PARAGRAPH_SEPARATOR):
            self.assertFalse(paragraph.startswith(("5", "a.", "Jansen", "korting")), paragraph)

    def test_clipboard_uses_windows_line_breaks(self) -> None:
        self.assertEqual(clipboard_text("Een.\n\nTwee. "), "Een.\r\n\r\nTwee. ")
        self.assertEqual(clipboard_text("Al\r\ngoed"), "Al\r\ngoed")


class TrailingSpaceTests(unittest.TestCase):
    def test_adds_one_space_after_the_transcription(self) -> None:
        self.assertEqual(append_trailing_space("Dit is een zin."), "Dit is een zin. ")

    def test_does_not_double_an_existing_space(self) -> None:
        self.assertEqual(append_trailing_space("Dit is een zin. "), "Dit is een zin. ")
        self.assertEqual(append_trailing_space("Dit is een zin.\n"), "Dit is een zin.\n")

    def test_leaves_empty_text_alone(self) -> None:
        self.assertEqual(append_trailing_space(""), "")


class DictionaryTests(unittest.TestCase):
    def test_normalizes_unicode_and_preserves_casing(self) -> None:
        decomposed = "Cli\u006e\u006f\u0301n"
        self.assertEqual(normalize_custom_word(f"  {decomposed}  "), unicodedata.normalize("NFC", decomposed))

    def test_deduplicates_case_insensitively_in_original_order(self) -> None:
        self.assertEqual(normalize_custom_words(["Groq", "Clinon", "groq"]), ("Groq", "Clinon"))

    def test_rejects_empty_and_control_characters(self) -> None:
        with self.assertRaises(DictionaryValidationError):
            normalize_custom_word("   ")
        with self.assertRaises(DictionaryValidationError):
            normalize_custom_word("Groq\nClinon")

    def test_lenient_loading_ignores_invalid_values(self) -> None:
        self.assertEqual(normalize_custom_words(["Groq", 42, "", "groq"], strict=False), ("Groq",))

    def test_lenient_loading_caps_count_and_total_size(self) -> None:
        loaded = normalize_custom_words([f"{index}-{'a' * 48}" for index in range(60)], strict=False)
        self.assertLessEqual(len(loaded), 25)
        self.assertLessEqual(len(f"Vocabulary: {', '.join(loaded)}.".encode("utf-8")), 192)

    def test_rejects_dictionary_that_is_too_large_for_useful_prompt_context(self) -> None:
        with self.assertRaises(DictionaryValidationError):
            normalize_custom_words([f"{index}-{'a' * 48}" for index in range(5)])


class PromptTests(unittest.TestCase):
    def test_dictionary_uses_android_vocabulary_format(self) -> None:
        self.assertEqual(compose_transcription_prompt("", ("Groq", "Clinon")), "Vocabulary: Groq, Clinon.")

    def test_existing_prompt_is_preserved_before_dictionary(self) -> None:
        self.assertEqual(
            compose_transcription_prompt("Nederlandse vergadering", ("Groq",)),
            "Nederlandse vergadering\nVocabulary: Groq.",
        )

    def test_empty_values_do_not_send_a_prompt(self) -> None:
        self.assertEqual(compose_transcription_prompt("", ()), "")

    def test_existing_long_prompt_is_not_rejected_by_a_false_byte_limit(self) -> None:
        prompt = "Dit is geldige context. " * 20
        self.assertEqual(compose_transcription_prompt(prompt, ()), prompt.strip())

    def test_existing_prompt_is_not_false_blocked_by_a_token_estimate(self) -> None:
        prompt = "context " * 170
        self.assertEqual(
            compose_transcription_prompt(prompt, ("Groq",)),
            f"{prompt.strip()}\nVocabulary: Groq.",
        )


class WordReplacementTests(unittest.TestCase):
    def test_replaces_known_whisper_variants_case_insensitively(self) -> None:
        replacements = (("Grok", "Groq"), ("Grog", "Groq"))
        self.assertEqual(
            apply_word_replacements("Grok Android en grog Android.", replacements),
            "Groq Android en Groq Android.",
        )

    def test_only_replaces_complete_words(self) -> None:
        self.assertEqual(
            apply_word_replacements("Grok Grokking", (("Grok", "Groq"),)),
            "Groq Grokking",
        )

    def test_supports_phrases_and_regex_characters_literally(self) -> None:
        replacements = (("C++ tool", "C++ Tooling"),)
        self.assertEqual(apply_word_replacements("De C++ tool werkt.", replacements), "De C++ Tooling werkt.")

    def test_punctuation_terms_are_not_replaced_inside_larger_tokens(self) -> None:
        replacements = (("C++", "C Plus Plus"), (".NET", "dotnet"))
        self.assertEqual(
            apply_word_replacements("C++ C++17 .NET x.NET", replacements),
            "C Plus Plus C++17 dotnet x.NET",
        )

    def test_rejects_conflicting_aliases(self) -> None:
        with self.assertRaises(DictionaryValidationError):
            normalize_word_replacements((("Grok", "Groq"), ("grok", "Grok AI")))

    def test_lenient_loading_ignores_invalid_replacements(self) -> None:
        self.assertEqual(
            normalize_word_replacements((("Grok", "Groq"), ("invalid",), 42), strict=False),
            (("Grok", "Groq"),),
        )


if __name__ == "__main__":
    unittest.main()
