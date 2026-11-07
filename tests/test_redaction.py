import random

import pytest

from quilr_assessment.redaction import StreamingRedactor


@pytest.mark.parametrize(
    "sensitive",
    [
        "alice@example.com",
        "first.last+tag@sub.example.co.uk",
        "123-45-6789",
        "4111111111111111",
        "4111 1111 1111 1111",
        "4111-1111-1111-1111",
    ],
)
def test_every_possible_split(sensitive):
    source = f"Contact {sensitive}, thanks."
    for split in range(len(source) + 1):
        redactor = StreamingRedactor()
        actual = redactor.feed(source[:split]) + redactor.feed(source[split:]) + redactor.finish()
        assert actual == "Contact [REDACTED], thanks."


def test_random_chunks_multiple_patterns_unicode_and_eof():
    text = "你好! alice@example.com 123-45-6789 4111 1111 1111 1111"
    for seed in range(100):
        rng = random.Random(seed)
        redactor, parts = StreamingRedactor(), []
        offset = 0
        while offset < len(text):
            length = rng.randint(1, 9)
            parts.append(redactor.feed(text[offset : offset + length]))
            offset += length
        parts.append(redactor.finish())
        assert "".join(parts) == "你好! [REDACTED] [REDACTED] [REDACTED]"


def test_normal_text_is_emitted_without_waiting_for_end():
    redactor = StreamingRedactor()
    assert redactor.feed("Hello ") == "Hello "
    assert redactor.feed("alice@") == ""
    assert redactor.feed("example.com ") == "[REDACTED] "


def test_overlong_candidates_fail_closed_and_memory_is_bounded():
    redactor = StreamingRedactor()
    output = "".join(redactor.feed("a" * 100) for _ in range(1000))
    output += redactor.feed("@example.com done ") + redactor.finish()
    assert output == "[REDACTED] done "
    assert redactor.peak_buffer <= 512
