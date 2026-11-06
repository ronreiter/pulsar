from __future__ import annotations

from pathlib import Path

from lark import Lark, Token
from lark.exceptions import UnexpectedCharacters, UnexpectedEOF, UnexpectedInput, UnexpectedToken

from .errors import PulsarError

_CONTINUE_NEXT = {">>", "->", ".", "else", "elif"}  # a line starting with these continues the previous one
_CONTINUE_PREV = {">>", "->", ",", "=", "+", "-", "*", "/", "and", "or", "=>", ":"}


class _PostLex:
    """Drop newlines inside () and [] and around continuation operators."""

    always_accept = ("_NL",)

    def process(self, stream):
        depth = 0
        pending = None
        prev = None
        for tok in stream:
            if tok.type == "_NL":
                if depth > 0 or (prev is not None and prev.value in _CONTINUE_PREV):
                    continue
                pending = tok
                continue
            if pending is not None:
                if tok.value not in _CONTINUE_NEXT:
                    yield pending
                pending = None
            if tok.value in ("(", "["):
                depth += 1
            elif tok.value in (")", "]"):
                depth = max(0, depth - 1)
            prev = tok
            yield tok
        if pending is not None:
            yield pending


_parser = None


def parser() -> Lark:
    global _parser
    if _parser is None:
        _parser = Lark((Path(__file__).parent / "grammar.lark").read_text(), parser="lalr", lexer="basic",
                       postlex=_PostLex(), propagate_positions=True, maybe_placeholders=True)
    return _parser


_NICE = {"_NL": "end of line", "NAME": "a name", "NUMBER": "a number", "STRING": "a string",
         "LPAR": "'('", "RPAR": "')'", "LBRACE": "'{'", "RBRACE": "'}'", "LSQB": "'['", "RSQB": "']'",
         "COMMA": "','", "EQUAL": "'='", "COLON": "':'", "DOT": "'.'", "$END": "end of file"}


def parse(source: str):
    if not source.endswith("\n"):
        source += "\n"
    try:
        return parser().parse(source)
    except UnexpectedToken as e:
        tok = e.token
        got = "end of file" if tok.type == "$END" else ("end of line" if tok.type == "_NL" else repr(str(tok)))
        exp = sorted({_NICE.get(x, x.strip('"').lower()) for x in e.expected if not x.startswith("__")})[:8]
        hint = None
        if got == "end of line" and "'}'" in exp:
            hint = "a block opened with '{' is not closed"
        raise PulsarError(f"unexpected {got}; expected {', '.join(exp)}", (e.line, e.column), hint) from None
    except UnexpectedCharacters as e:
        raise PulsarError(f"unexpected character {source[e.pos_in_stream]!r}", (e.line, e.column)) from None
    except UnexpectedEOF as e:
        raise PulsarError("unexpected end of file (missing '}' or ')'?)") from None
    except UnexpectedInput as e:
        raise PulsarError(str(e), (getattr(e, "line", 0), getattr(e, "column", 0))) from None
