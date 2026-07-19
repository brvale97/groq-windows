from __future__ import annotations

import unicodedata
import re
from collections.abc import Iterable, Sequence


MAX_CUSTOM_WORDS = 25
MAX_CUSTOM_WORD_LENGTH = 50
# A byte-level tokenizer can never emit more tokens than input bytes. Keeping
# the generated vocabulary section below this size makes the feature bounded
# without pretending a heuristic is Groq's exact Whisper tokenizer.
MAX_VOCABULARY_PROMPT_BYTES = 192
MAX_WORD_REPLACEMENTS = 50
MAX_REPLACEMENT_PART_LENGTH = 80


class DictionaryValidationError(ValueError):
    """Raised when a custom dictionary entry cannot safely be used."""


def normalize_custom_word(value: str) -> str:
    word = unicodedata.normalize("NFC", str(value).strip())
    if not word:
        raise DictionaryValidationError("Vul eerst een woord of naam in.")
    if len(word) > MAX_CUSTOM_WORD_LENGTH:
        raise DictionaryValidationError(
            f"Een woordenboekitem mag maximaal {MAX_CUSTOM_WORD_LENGTH} tekens bevatten."
        )
    if any(unicodedata.category(character).startswith("C") for character in word):
        raise DictionaryValidationError("Een woordenboekitem mag geen regeleinden of stuurtekens bevatten.")
    return word


def normalize_custom_words(values: Iterable[object], *, strict: bool = True) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            if strict:
                raise DictionaryValidationError("Het woordenboek bevat een ongeldig item.")
            continue
        try:
            word = normalize_custom_word(value)
        except DictionaryValidationError:
            if strict:
                raise
            continue
        folded = word.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        result.append(word)
        if len(result) > MAX_CUSTOM_WORDS:
            if strict:
                raise DictionaryValidationError(
                    f"Het woordenboek mag maximaal {MAX_CUSTOM_WORDS} items bevatten."
                )
            result = result[:MAX_CUSTOM_WORDS]
            break
    def vocabulary_size(words: Sequence[str]) -> int:
        return len(f"Vocabulary: {', '.join(words)}.".encode("utf-8"))

    if result and vocabulary_size(result) > MAX_VOCABULARY_PROMPT_BYTES:
        if strict:
            raise DictionaryValidationError(
                "Het woordenboek is te groot voor betrouwbare Groq-spellingcontext. "
                "Verwijder enkele woorden of maak lange items korter."
            )
        while result and vocabulary_size(result) > MAX_VOCABULARY_PROMPT_BYTES:
            result.pop()
    return tuple(result)


def compose_transcription_prompt(base_prompt: str, custom_words: Sequence[str]) -> str:
    parts: list[str] = []
    cleaned_base = unicodedata.normalize("NFC", str(base_prompt).strip())
    if cleaned_base:
        parts.append(cleaned_base)

    words = normalize_custom_words(custom_words)
    if words:
        parts.append(f"Vocabulary: {', '.join(words)}.")

    return "\n".join(parts)


def normalize_replacement_part(value: object, label: str) -> str:
    part = unicodedata.normalize("NFC", str(value).strip())
    if not part:
        raise DictionaryValidationError(f"Vul eerst het {label} woord in.")
    if len(part) > MAX_REPLACEMENT_PART_LENGTH:
        raise DictionaryValidationError(
            f"Een vervangingsveld mag maximaal {MAX_REPLACEMENT_PART_LENGTH} tekens bevatten."
        )
    if any(unicodedata.category(character).startswith("C") for character in part):
        raise DictionaryValidationError("Een vervanging mag geen regeleinden of stuurtekens bevatten.")
    return part


def normalize_word_replacements(
    values: Iterable[object],
    *,
    strict: bool = True,
) -> tuple[tuple[str, str], ...]:
    result: list[tuple[str, str]] = []
    seen: dict[str, str] = {}
    for value in values:
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            if strict:
                raise DictionaryValidationError("De woordvervangingen bevatten een ongeldig item.")
            continue
        try:
            source = normalize_replacement_part(value[0], "verkeerd herkende")
            target = normalize_replacement_part(value[1], "correcte")
        except DictionaryValidationError:
            if strict:
                raise
            continue

        folded = source.casefold()
        previous = seen.get(folded)
        if previous is not None:
            if strict and previous != target:
                raise DictionaryValidationError(f"Voor '{source}' bestaat al een andere vervanging.")
            continue
        seen[folded] = target
        result.append((source, target))
        if len(result) > MAX_WORD_REPLACEMENTS:
            if strict:
                raise DictionaryValidationError(
                    f"Je kunt maximaal {MAX_WORD_REPLACEMENTS} woordvervangingen opslaan."
                )
            result = result[:MAX_WORD_REPLACEMENTS]
            break
    return tuple(result)


def apply_word_replacements(text: str, replacements: Sequence[tuple[str, str]]) -> str:
    result = text
    for source, target in normalize_word_replacements(replacements):
        pattern = re.compile(rf"(?<!\w){re.escape(source)}(?!\w)", re.IGNORECASE)
        result = pattern.sub(lambda _match, replacement=target: replacement, result)
    return result
