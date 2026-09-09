from __future__ import annotations

import asyncio
import html
import logging
import os
import random
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.client.default import DefaultBotProperties

from compiler import QuizSyntaxError, Question, compile_quiz
from storage import QuizStorage

TOKEN = os.getenv("BOT_TOKEN", "")
storage = QuizStorage()
router = Router()
dashboard_messages: dict[int, int] = {}
waiting_for_source: set[int] = set()
selected_quizzes: dict[int, int] = {}


@dataclass
class Session:
    quiz_id: int
    questions: list[Question]
    position: int = 0
    picked: set[int] = field(default_factory=set)
    message_id: int | None = None
    finished: bool = False


sessions: dict[tuple[int, str], Session] = {}


def esc(value: str) -> str:
    return html.escape(value)


def dashboard_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Опросы", callback_data="quizzes:0")],
        [InlineKeyboardButton(text="Добавить опрос", callback_data="add")],
    ])


async def edit_dashboard(query: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    await query.message.edit_text(text, reply_markup=markup)
    dashboard_messages[query.from_user.id] = query.message.message_id


def quizzes_keyboard(selected: int | None, page: int) -> InlineKeyboardMarkup:
    quizzes = storage.list_quizzes()
    per_page = 8
    page = max(0, min(page, max(0, (len(quizzes) - 1) // per_page)))
    rows = []
    for quiz in quizzes[page * per_page:(page + 1) * per_page]:
        prefix = "✅ " if selected == quiz["id"] else ""
        rows.append([InlineKeyboardButton(text=prefix + quiz["name"], callback_data=f"pick:{quiz['id']}:{page}")])
    if len(quizzes) > per_page:
        navigation = []
        if page:
            navigation.append(InlineKeyboardButton(text="‹", callback_data=f"quizzes:{page - 1}"))
        navigation.append(InlineKeyboardButton(text=f"{page + 1}/{(len(quizzes) - 1) // per_page + 1}", callback_data="noop"))
        if (page + 1) * per_page < len(quizzes):
            navigation.append(InlineKeyboardButton(text="›", callback_data=f"quizzes:{page + 1}"))
        rows.append(navigation)
    if selected is not None:
        rows.append([InlineKeyboardButton(text="Начать опрос", callback_data=f"start:{selected}")])
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def answer_options(session: Session, question: Question) -> list[str]:
    needed = 3 - len(question.answers)
    wrong = storage.distractor_answers(session.quiz_id, question.topic, question.answers, needed)
    if len(wrong) != needed:
        raise ValueError("недостаточно других уникальных ответов для трёх кнопок")
    options = list(question.answers) + wrong
    random.shuffle(options)
    return options


def question_keyboard(sid: str, session: Session, question: Question) -> InlineKeyboardMarkup:
    rows = []
    if question.kind != "text":
        # Keep a stable order while the user ticks and unticks multiple answers.
        if getattr(session, "_options_position", None) != session.position:
            session._options = answer_options(session, question)  # type: ignore[attr-defined]
            session._options_position = session.position  # type: ignore[attr-defined]
        options = session._options  # type: ignore[attr-defined]
        for index, option in enumerate(options):
            checked = question.kind == "multiple" and index in session.picked
            rows.append([InlineKeyboardButton(text=("✅ " if checked else "") + option, callback_data=f"answer:{sid}:{index}")])
        if question.kind == "multiple":
            rows.append([InlineKeyboardButton(text="Подтвердить", callback_data=f"confirm:{sid}")])
    if question.hint:
        rows.append([InlineKeyboardButton(text="Подсказка", callback_data=f"hint:{sid}")])
    rows.append([InlineKeyboardButton(text="Остановить опрос", callback_data=f"stop:{sid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_question(user_id: int, sid: str, message: Message) -> None:
    session = sessions[(user_id, sid)]
    question = session.questions[session.position]
    instruction = {"single": "Выберите один вариант.", "multiple": "Отметьте все верные варианты и нажмите «Подтвердить».", "text": "Напишите ответ следующим сообщением."}[question.kind]
    text = f"<b>Задание {session.position + 1}/{len(session.questions)}</b>\n\n{esc(question.text)}\n\n<i>{instruction}</i>"
    await message.edit_text(text, reply_markup=question_keyboard(sid, session, question))


def result_keyboard(sid: str, has_next: bool) -> InlineKeyboardMarkup:
    label = "Следующее задание" if has_next else "Завершить"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"next:{sid}")],
        [InlineKeyboardButton(text="Остановить опрос", callback_data=f"stop:{sid}")],
    ])


async def show_result(query: CallbackQuery, sid: str, correct: bool, detail: str) -> None:
    session = sessions[(query.from_user.id, sid)]
    prefix = "✅ <b>Верно!</b>" if correct else "❌ <b>Пока не получилось.</b>"
    await query.message.edit_text(f"{prefix}\n\n{detail}", reply_markup=result_keyboard(sid, session.position + 1 < len(session.questions)))


@router.message(CommandStart())
async def start(message: Message) -> None:
    sent = await message.answer("Выберите действие.", reply_markup=dashboard_keyboard())
    dashboard_messages[message.from_user.id] = sent.message_id


@router.callback_query(F.data == "home")
async def home(query: CallbackQuery) -> None:
    selected_quizzes.pop(query.from_user.id, None)
    await edit_dashboard(query, "Выберите действие.", dashboard_keyboard())
    await query.answer()


@router.callback_query(F.data.startswith("quizzes:"))
async def show_quizzes(query: CallbackQuery) -> None:
    page = int(query.data.split(":")[1])
    text = "<b>Опросы</b>\n\nВыберите один опрос, затем нажмите «Начать опрос»."
    if not storage.list_quizzes():
        text += "\n\nПока здесь пусто — добавьте первый опрос."
    selected_quizzes.pop(query.from_user.id, None)
    await edit_dashboard(query, text, quizzes_keyboard(None, page))
    await query.answer()


@router.callback_query(F.data.startswith("pick:"))
async def pick_quiz(query: CallbackQuery) -> None:
    _, quiz_id, page = query.data.split(":")
    quiz_id = int(quiz_id)
    # Pressing a marked quiz clears the selection.
    selected = None if selected_quizzes.get(query.from_user.id) == quiz_id else quiz_id
    if selected is None:
        selected_quizzes.pop(query.from_user.id, None)
    else:
        selected_quizzes[query.from_user.id] = selected
    await edit_dashboard(query, "<b>Опросы</b>\n\nВыберите один опрос, затем нажмите «Начать опрос».", quizzes_keyboard(selected, int(page)))
    await query.answer()


@router.callback_query(F.data == "add")
async def ask_source(query: CallbackQuery) -> None:
    waiting_for_source.add(query.from_user.id)
    text = ("Пришлите текст опроса одним сообщением. Название укажите первой строкой: <code>@ Мой опрос</code>.\n\n"
            "<code># Животные\n|Кто мяукает?|[кот]\n/Переведи: собака/[dog]\n\\Выбери животных\\[кот;собака]{Домашние питомцы}</code>")
    await edit_dashboard(query, text, InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="← Отмена", callback_data="home")]]))
    await query.answer()


@router.message(Command("delete"))
async def delete_quiz(message: Message) -> None:
    name = (message.text or "").partition(" ")[2].strip()
    if not name:
        await message.answer("Использование: <code>/delete Название опроса</code>")
        return
    await message.answer("Опрос удалён." if storage.delete_by_name(name) else "Опрос с таким названием не найден.")


@router.message(F.text)
async def receive_source_or_answer(message: Message) -> None:
    user_id = message.from_user.id
    if user_id in waiting_for_source:
        try:
            draft = compile_quiz(message.text)
            fallback = f"Опрос {datetime.now():%Y-%m-%d %H:%M}"
            name = storage.save_quiz(draft, message.text, fallback)
        except QuizSyntaxError as exc:
            await message.answer(f"<b>Не удалось добавить опрос.</b>\n{esc(str(exc))}")
            return
        except sqlite3.IntegrityError:
            await message.answer("Опрос с таким названием уже есть. Измените строку <code>@ ...</code>.")
            return
        waiting_for_source.discard(user_id)
        await message.answer(f"✅ Опрос «{esc(name)}» добавлен: {len(draft.questions)} заданий.")
        return

    # A text reply belongs to the only active session of this user.
    candidates = [(sid, s) for (uid, sid), s in sessions.items() if uid == user_id and not s.finished and s.questions[s.position].kind == "text"]
    if not candidates:
        return
    sid, session = candidates[-1]
    question = session.questions[session.position]
    submitted = message.text.strip()
    correct_answer = question.answers[0]
    correct = submitted.casefold() == correct_answer.casefold()
    detail = (f"Ваш ответ: «{esc(submitted)}».\nПравильный ответ: «{esc(correct_answer)}»." if not correct
              else f"Ответ «{esc(submitted)}» принят. Отличная работа!")
    try:
        await message.delete()
    except Exception:
        pass
    quiz_message = await message.bot.edit_message_text(chat_id=message.chat.id, message_id=session.message_id,
        text=("✅ <b>Верно!</b>\n\n" if correct else "❌ <b>Пока не получилось.</b>\n\n") + detail,
        reply_markup=result_keyboard(sid, session.position + 1 < len(session.questions)))


@router.callback_query(F.data.startswith("start:"))
async def start_quiz(query: CallbackQuery) -> None:
    quiz_id = int(query.data.split(":")[1])
    selected_quizzes.pop(query.from_user.id, None)
    questions = storage.questions_for(quiz_id)
    sid = f"{random.randrange(16**7):07x}"
    session = Session(quiz_id, questions)
    sessions[(query.from_user.id, sid)] = session
    # Validate all button questions before opening a new quiz message.
    try:
        for question in questions:
            if question.kind != "text":
                needed = 3 - len(question.answers)
                if len(storage.distractor_answers(quiz_id, question.topic, question.answers, needed)) != needed:
                    raise ValueError
    except ValueError:
        sessions.pop((query.from_user.id, sid), None)
        await query.answer("Для одного из заданий не хватает ответов для трёх кнопок.", show_alert=True)
        return
    sent = await query.message.answer("Открываю опрос…")
    session.message_id = sent.message_id
    await show_question(query.from_user.id, sid, sent)
    await query.answer()


@router.callback_query(F.data.startswith("answer:"))
async def answer_button(query: CallbackQuery) -> None:
    _, sid, raw_index = query.data.split(":")
    session = sessions.get((query.from_user.id, sid))
    if not session or session.finished:
        await query.answer("Этот опрос уже закрыт.", show_alert=True); return
    question = session.questions[session.position]
    index = int(raw_index)
    options = session._options  # type: ignore[attr-defined]
    if question.kind == "multiple":
        (session.picked.remove(index) if index in session.picked else session.picked.add(index))
        await show_question(query.from_user.id, sid, query.message)
        await query.answer(); return
    chosen = options[index]
    correct = chosen.casefold() == question.answers[0].casefold()
    detail = (f"«{esc(chosen)}» — правильный ответ к заданию «{esc(question.text)}»." if correct
              else f"Вы выбрали «{esc(chosen)}». К заданию «{esc(question.text)}» правильный ответ: «{esc(question.answers[0])}».")
    await show_result(query, sid, correct, detail)
    await query.answer()


@router.callback_query(F.data.startswith("confirm:"))
async def confirm_multiple(query: CallbackQuery) -> None:
    sid = query.data.split(":")[1]
    session = sessions.get((query.from_user.id, sid))
    if not session or session.finished:
        await query.answer("Этот опрос уже закрыт.", show_alert=True); return
    question = session.questions[session.position]
    options = session._options  # type: ignore[attr-defined]
    picked = {options[i].casefold() for i in session.picked}
    expected = {x.casefold() for x in question.answers}
    correct = picked == expected
    chosen = ", ".join(f"«{esc(options[i])}»" for i in sorted(session.picked)) or "ничего"
    expected_text = ", ".join(f"«{esc(x)}»" for x in question.answers)
    detail = (f"Вы выбрали {chosen}. Все верные ответы: {expected_text}." if correct
              else f"Вы выбрали {chosen}. Для задания «{esc(question.text)}» верные ответы: {expected_text}.")
    await show_result(query, sid, correct, detail)
    await query.answer()


@router.callback_query(F.data.startswith("hint:"))
async def hint(query: CallbackQuery) -> None:
    sid = query.data.split(":")[1]
    session = sessions.get((query.from_user.id, sid))
    if session:
        await query.answer(session.questions[session.position].hint or "Подсказки нет.", show_alert=True)


@router.callback_query(F.data.startswith("next:"))
async def next_question(query: CallbackQuery) -> None:
    sid = query.data.split(":")[1]
    session = sessions.get((query.from_user.id, sid))
    if not session: return
    session.position += 1
    session.picked.clear()
    if session.position == len(session.questions):
        session.finished = True
        await query.message.edit_text("🎉 <b>Опрос завершён!</b>\n\nСпасибо за прохождение.")
    else:
        await show_question(query.from_user.id, sid, query.message)
    await query.answer()


@router.callback_query(F.data.startswith("stop:"))
async def stop_quiz(query: CallbackQuery) -> None:
    sid = query.data.split(":")[1]
    session = sessions.get((query.from_user.id, sid))
    if session: session.finished = True
    await query.message.edit_text("Опрос остановлен. Результат не сохранён.")
    await query.answer()


@router.callback_query(F.data == "noop")
async def noop(query: CallbackQuery) -> None:
    await query.answer()


async def main() -> None:
    if not TOKEN:
        raise RuntimeError("Укажите BOT_TOKEN в переменной окружения.")
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
