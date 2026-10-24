from __future__ import annotations

import difflib


class PulsarError(Exception):
    """An error in a Pulsar program, with an optional (line, column) location."""

    def __init__(self, msg: str, loc: tuple[int, int] | None = None, hint: str | None = None):
        super().__init__(msg)
        self.msg = msg
        self.loc = loc
        self.hint = hint

    def format(self, source: str | None = None, filename: str = "<song>") -> str:
        where = f"{filename}:{self.loc[0]}:{self.loc[1]}" if self.loc else filename
        out = f"{where}: error: {self.msg}"
        if source and self.loc:
            lines = source.splitlines()
            if 0 < self.loc[0] <= len(lines):
                line = lines[self.loc[0] - 1]
                out += f"\n    {line}\n    {' ' * (self.loc[1] - 1)}^"
        if self.hint:
            out += f"\n  hint: {self.hint}"
        return out

    def __str__(self):
        return self.format()


def suggest(name: str, options) -> str | None:
    m = difflib.get_close_matches(name, list(options), n=3, cutoff=0.6)
    return f"did you mean {' or '.join(repr(x) for x in m)}?" if m else None
