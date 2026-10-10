from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable, Sequence


MAX_CUSTOM_WORDS = 25
MAX_CUSTOM_WORD_LENGTH = 50
# A byte-level tokenizer can never emit more tokens than input bytes. Keeping
# the generated vocabulary section below this size makes the feature bounded
# without pretending a heuristic is Groq's exact Whisper tokenizer.
MAX_VOCABULARY_PROMPT_BYTES = 192
MAX_WORD_REPLACEMENTS = 50
MAX_REPLACEMENT_PART_LENGTH = 80
# Selector label prefix for "follow the Windows default input".
DEFAULT_DEVICE_LABEL = "Windows-standaard"


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


def apply_final_period_preference(text: str, *, remove_final_period: bool) -> str:
    if remove_final_period and text.endswith(".") and not text.endswith("..."):
        return text[:-1]
    return text


def append_trailing_space(text: str) -> str:
    """Add one trailing space so the next words start cleanly after a paste."""
    if not text or text[-1].isspace():
        return text
    return f"{text} "


# Chat-style paragraphs: short blocks of a few sentences, like a typed message.
PARAGRAPH_MIN_CHARACTERS = 200
PARAGRAPH_MAX_SENTENCES = 3
PARAGRAPH_TARGET_CHARACTERS = 220
PARAGRAPH_TOPIC_MIN_CHARACTERS = 60
# One empty line between blocks, like a normal paragraph break.
PARAGRAPH_SEPARATOR = "\n\n"
# Sentence openers that usually start a new thought.
TOPIC_SHIFT_OPENERS = (
    "verder", "daarnaast", "daarna", "oh ja", "o ja", "dan nog", "wat betreft", "trouwens", "overigens", "anyway", "oké", "oke", "ok", "nou",
    "tot slot", "ten slotte", "andere vraag", "nog iets", "btw", "by the way",
    "also", "besides", "furthermore", "additionally", "finally",
)
CLOSING_OPENERS = (
    "groetjes", "groeten", "groet", "met vriendelijke groet", "mvg", "fijne dag", "fijn weekend",
    "alvast bedankt", "bedankt alvast", "dank je wel", "dankjewel", "thanks", "cheers",
    "best regards", "kind regards",
)
# Abbreviations whose period does not end a sentence.
NON_TERMINAL_ABBREVIATIONS = frozenset({
    "bijv", "bv", "bijvoorbeeld", "o.a", "d.w.z", "i.p.v", "m.b.t", "t.o.v", "z.s.m", "e.d",
    "enz", "etc", "dhr", "mevr", "mr", "mrs", "ms", "dr", "prof", "ir", "ing", "drs", "mw",
    "nr", "ca", "e.g", "i.e", "vs", "st",
})
_SENTENCE_END = re.compile(r"(?<=[.!?…])[\"'”’)]*\s+(?=[\"'“‘(]?[^\W\d_]|\d)")


def split_sentences(text: str) -> list[str]:
    """Split on sentence punctuation, keeping abbreviations like 'bijv.' intact."""
    sentences: list[str] = []
    start = 0
    for match in _SENTENCE_END.finditer(text):
        candidate = text[start:match.start()].rstrip()
        last_word = candidate.rsplit(None, 1)[-1] if candidate else ""
        if last_word.endswith(".") and last_word[:-1].casefold().lstrip("(\"'“‘") in NON_TERMINAL_ABBREVIATIONS:
            continue
        sentences.append(text[start:match.end()].strip())
        start = match.end()
    tail = text[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences


def _starts_with(sentence: str, openers: Sequence[str]) -> bool:
    lowered = sentence.casefold().lstrip("\"'“‘(")
    return any(
        lowered.startswith(opener) and (len(lowered) == len(opener) or not lowered[len(opener)].isalnum())
        for opener in openers
    )


def format_paragraphs(text: str) -> str:
    """Break one dictated block into short, readable chat paragraphs.

    Only whitespace between sentences changes; the words stay exactly as
    transcribed. Short messages and text that already has line breaks are
    returned unchanged.
    """
    if "\n" in text or len(text) < PARAGRAPH_MIN_CHARACTERS:
        return text
    sentences = split_sentences(text)
    if len(sentences) < 3:
        return text

    # First cut where a new thought or the closing starts, then split the
    # remaining runs into evenly sized blocks.
    runs: list[list[str]] = [[]]
    for sentence in sentences:
        run = runs[-1]
        length = sum(len(part) + 1 for part in run)
        if run and (
            _starts_with(sentence, CLOSING_OPENERS)
            or (length >= PARAGRAPH_TOPIC_MIN_CHARACTERS and _starts_with(sentence, TOPIC_SHIFT_OPENERS))
        ):
            runs.append([])
        runs[-1].append(sentence)
    paragraphs = [paragraph for run in runs for paragraph in _balanced_chunks(run)]
    return PARAGRAPH_SEPARATOR.join(" ".join(paragraph) for paragraph in paragraphs)


def _balanced_chunks(sentences: list[str]) -> list[list[str]]:
    total = sum(len(sentence) + 1 for sentence in sentences)
    count = max(
        math.ceil(len(sentences) / PARAGRAPH_MAX_SENTENCES),
        math.ceil(total / PARAGRAPH_TARGET_CHARACTERS),
    )
    count = max(1, min(count, len(sentences)))
    chunks: list[list[str]] = []
    current: list[str] = []
    done = 0
    for index, sentence in enumerate(sentences):
        size = len(sentence) + 1
        remaining_sentences = len(sentences) - index
        remaining_chunks = count - len(chunks)
        target = (total - done) / remaining_chunks
        filled = sum(len(part) + 1 for part in current)
        # Close the block when adding this sentence overshoots the even share
        # more than stopping short does, or when the rest needs one each.
        if current and remaining_chunks > 1 and (
            remaining_sentences < remaining_chunks
            or abs(filled + size - target) > abs(filled - target)
            or len(current) >= PARAGRAPH_MAX_SENTENCES
        ):
            chunks.append(current)
            done += filled
            current = []
        current.append(sentence)
    chunks.append(current)
    return chunks


def clipboard_text(text: str) -> str:
    """Windows apps expect CRLF line breaks on the clipboard."""
    return text.replace("\r\n", "\n").replace("\n", "\r\n")
