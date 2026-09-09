from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from compiler import QuizDraft, Question


class QuizStorage:
    def __init__(self, path: str = "quizzes.sqlite3") -> None:
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS quizzes (
              id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE,
              source TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS questions (
              id INTEGER PRIMARY KEY, quiz_id INTEGER NOT NULL REFERENCES quizzes(id) ON DELETE CASCADE,
              position INTEGER NOT NULL, topic TEXT, question TEXT NOT NULL,
              answers_json TEXT NOT NULL, kind TEXT NOT NULL, hint TEXT
            );
            CREATE INDEX IF NOT EXISTS questions_quiz_position ON questions(quiz_id, position);
            """
        )

    def save_quiz(self, draft: QuizDraft, source: str, fallback_name: str) -> str:
        name = (draft.name or fallback_name).strip()
        with self.conn:
            cursor = self.conn.execute("INSERT INTO quizzes(name, source) VALUES (?, ?)", (name, source))
            quiz_id = cursor.lastrowid
            self.conn.executemany(
                "INSERT INTO questions(quiz_id, position, topic, question, answers_json, kind, hint) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(quiz_id, index, q.topic, q.text, json.dumps(q.answers, ensure_ascii=False), q.kind, q.hint)
                 for index, q in enumerate(draft.questions)],
            )
        return name

    def list_quizzes(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT id, name FROM quizzes ORDER BY name COLLATE NOCASE").fetchall()

    def delete_by_name(self, name: str) -> bool:
        with self.conn:
            return self.conn.execute("DELETE FROM quizzes WHERE name = ? COLLATE NOCASE", (name.strip(),)).rowcount > 0

    def questions_for(self, quiz_id: int) -> list[Question]:
        rows = self.conn.execute("SELECT * FROM questions WHERE quiz_id = ? ORDER BY position", (quiz_id,)).fetchall()
        return [Question(r["question"], tuple(json.loads(r["answers_json"])), r["kind"], r["hint"], r["topic"]) for r in rows]

    def distractor_answers(self, quiz_id: int, topic: str | None, excluded: tuple[str, ...], count: int) -> list[str]:
        """Prefer the same topic, then use every other saved question as fallback.

        quiz_id stays in the API because callers naturally have it and makes a future
        per-quiz policy easy to add; distractors deliberately span the whole database.
        """
        rows = self.conn.execute("SELECT topic, answers_json FROM questions").fetchall()
        excluded_keys = {x.casefold() for x in excluded}
        same, other = [], []
        for row in rows:
            target = same if topic and row["topic"] == topic else other
            for answer in json.loads(row["answers_json"]):
                if answer.casefold() not in excluded_keys and answer.casefold() not in {x.casefold() for x in target}:
                    target.append(answer)
        return (same + other)[:count]
