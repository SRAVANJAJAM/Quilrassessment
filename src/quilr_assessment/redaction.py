import re

EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
SSN = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
CANDIDATE = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._%+@-")
NUMERIC = frozenset("0123456789 -")


def redact(text: str) -> str:
    for pattern in (EMAIL, SSN, CARD):
        text = pattern.sub("[REDACTED]", text)
    return text


class StreamingRedactor:
    """Hold an unfinished word or numeric run, never the complete response.

    A word could become an email in the next delta. Releasing it early cannot be
    undone. Overlong candidates are conservatively redacted rather than truncated.
    """

    def __init__(self, max_candidate: int = 512):
        self.pending = ""
        self.max_candidate = max_candidate
        self.discard: str | None = None
        self.peak_buffer = 0

    def _flush(self) -> str:
        output = redact(self.pending)
        self.pending = ""
        return output

    def feed(self, delta: str) -> str:
        output: list[str] = []
        for char in delta:
            if self.discard:
                allowed = NUMERIC if self.discard == "numeric" else CANDIDATE
                if char in allowed:
                    continue
                self.discard = None

            numeric = bool(self.pending) and all(c in NUMERIC for c in self.pending)
            if numeric and " " in self.pending and char not in NUMERIC:
                output.append(self._flush())
                numeric = False

            if char in CANDIDATE or (char == " " and numeric):
                self.pending += char
                self.peak_buffer = max(self.peak_buffer, len(self.pending))
                if len(self.pending) >= self.max_candidate:
                    self.discard = "numeric" if all(c in NUMERIC for c in self.pending) else "word"
                    self.pending = ""
                    output.append("[REDACTED]")
            else:
                output.append(self._flush())
                output.append(char)
        return "".join(output)

    def finish(self) -> str:
        self.discard = None
        return self._flush()
