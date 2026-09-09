"""Compiler for the compact Telegram quiz format.

Format:
    @ Quiz name                 # optional, but recommended
    # Topic                     # optional and repeatable
    |Question|[one answer]{hint}
    /Question/[one answer]{hint}
    \\Question\\[answer one;answer two]{hint}
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re


class QuizSyntaxError(ValueError):
    """A syntax error with a line number suitable for showing to an author."""


@dataclass(frozen=True)
class Question:
    text: str
    answers: tuple[str, ...]
    kind: str  # single, text, multiple
    hint: str | None = None
    topic: str | None = None


@dataclass(frozen=True)
class QuizDraft:
    name: str | None
    questions: tuple[Question, ...]
    topics: tuple[str, ...] = field(default_factory=tuple)


QUESTION_RE = re.compile(
    r"^(?P<open>[|/\\])(?P<text>.+?)(?P=open)\[(?P<answers>[^\]]+)\]"
    r"(?:\{(?P<hint>[^{}]+)\})?$"
)


def _error(line_no: int, message: str) -> QuizSyntaxError:
    return QuizSyntaxError(f"Строка {line_no}: {message}")


def compile_quiz(source: str) -> QuizDraft:
    """Parse source and return normalized questions; never writes to the database."""
    name: str | None = None
    topic: str | None = None
    topics: list[str] = []
    questions: list[Question] = []

    for line_no, raw_line in enumerate(source.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("@"):
            if name is not None:
                raise _error(line_no, "название опроса @... можно указать только один раз")
            name = line[1:].strip()
            if not name:
                raise _error(line_no, "после @ нужно указать название опроса")
            continue
        # Accept \# too: it is convenient when the text was copied from Markdown.
        if line.startswith("#") or line.startswith("\\#"):
            topic = line.lstrip("\\")[1:].strip()
            if not topic:
                raise _error(line_no, "после # нужно указать тему")
            if topic not in topics:
                topics.append(topic)
            continue

        match = QUESTION_RE.fullmatch(line)
        if not match:
            raise _error(line_no, "не понимаю запись. Используйте |...|[...], /.../[...] или \\...\\[...]")
        marker = match.group("open")
        kind = {"|": "single", "/": "text", "\\": "multiple"}[marker]
        text = match.group("text").strip()
        answers = tuple(part.strip() for part in match.group("answers").split(";"))
        hint = (match.group("hint") or "").strip() or None
        if not text:
            raise _error(line_no, "задание не может быть пустым")
        if not all(answers):
            raise _error(line_no, "между ; должны быть непустые ответы")
        if len(set(answer.casefold() for answer in answers)) != len(answers):
            raise _error(line_no, "ответы в одном задании не должны повторяться")
        if kind != "multiple" and len(answers) != 1:
            raise _error(line_no, "для |...| и /.../ нужен ровно один ответ")
        if kind == "multiple" and len(answers) < 2:
            raise _error(line_no, "для \\...\\ нужны хотя бы два ответа через ;")
        questions.append(Question(text, answers, kind, hint, topic))

    if not questions:
        raise QuizSyntaxError("В опросе нет заданий.")
    return QuizDraft(name, tuple(questions), tuple(topics))
