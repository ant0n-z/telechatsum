"""Меню бота: сохранённые чаты в виде inline-кнопок.

  📬 название  — нажатие запускает выжимку непрочитанных сообщений этого чата;
  🗑           — сразу убирает чат из списка (без подтверждения).

Список открывается постоянной кнопкой «📋 Мои чаты» под полем ввода или командой /menu.
В данных кнопок лежит короткий номер записи (лимит Telegram — 64 байта), а не ссылка.
"""
from __future__ import annotations

from telethon import Button, errors

from store import ChatStore, SavedChat
from tg_fetch import Request

MENU_TEXT = "📋 Мои чаты"
PAGE_SIZE = 8
_MAX_TITLE = 40  # длиннее название на кнопке не помещается


def keyboard_button():
    """Постоянная кнопка под полем ввода; по нажатию бот получает текст MENU_TEXT."""
    return Button.text(MENU_TEXT, resize=True)


def _short(title: str) -> str:
    return title if len(title) <= _MAX_TITLE else title[: _MAX_TITLE - 1].rstrip() + "…"


def menu_view(chats: list[SavedChat], page: int = 0):
    """Возвращает (текст, кнопки, номер страницы после поправки на границы)."""
    if not chats:
        text = (
            "📋 Список пуст.\n\nОтправьте ссылку на чат, группу или канал: она автоматически "
            "добавится сюда, и в следующий раз её можно будет выбрать кнопкой."
        )
        return text, [[Button.inline("🔄 Обновить", data=b"page:0")]], 0

    pages = -(-len(chats) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    text = (
        f"📋 Ваши чаты: {len(chats)}.\n"
        "Нажмите на название — пришлю выжимку непрочитанных. 🗑 — убрать из списка."
    )
    if pages > 1:
        text += f"\nСтраница {page + 1} из {pages}."

    rows = [
        [
            Button.inline(f"📬 {_short(c.title)}", data=f"run:{c.id}".encode()),
            Button.inline("🗑", data=f"del:{c.id}:{page}".encode()),
        ]
        for c in chats[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]
    ]
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(Button.inline("◀", data=f"page:{page - 1}".encode()))
        nav.append(Button.inline(f"{page + 1}/{pages}", data=b"noop"))
        if page < pages - 1:
            nav.append(Button.inline("▶", data=f"page:{page + 1}".encode()))
        rows.append(nav)
    return text, rows, page


async def show_menu(event, store: ChatStore) -> None:
    """Присылает список новым сообщением (команда /menu или кнопка «Мои чаты»)."""
    text, rows, _ = menu_view(store.list(event.sender_id), 0)
    await event.respond(text, buttons=rows)


async def _refresh(event, store: ChatStore, page: int) -> None:
    """Перерисовывает список в том же сообщении."""
    text, rows, _ = menu_view(store.list(event.sender_id), page)
    try:
        await event.edit(text, buttons=rows)
    except errors.MessageNotModifiedError:
        pass  # перерисовывать нечего


def _parse(data: bytes) -> tuple[str, list[int]] | None:
    parts = (data or b"").decode("utf-8", "ignore").split(":")
    try:
        return parts[0], [int(x) for x in parts[1:]]
    except ValueError:
        return None


async def handle_callback(event, store: ChatStore, start_request) -> None:
    """Обработка нажатий на кнопки списка. Доступ уже проверен вызывающим кодом.

    start_request(event, Request) запускает выжимку через общую блокировку бота.
    """
    uid = event.sender_id
    parsed = _parse(event.data)
    if parsed is None:
        await event.answer()
        return
    action, args = parsed

    if action == "run" and args:
        chat = store.get(uid, args[0])
        if chat is None:
            await event.answer("Этого чата уже нет в списке", alert=True)
            await _refresh(event, store, 0)
            return
        await event.answer("Читаю…")
        await start_request(event, Request(chat.ref))  # только непрочитанные
    elif action == "del" and args:
        removed = store.remove(uid, args[0])
        await event.answer("Убрано из списка" if removed else "Уже убрано")
        await _refresh(event, store, args[1] if len(args) > 1 else 0)
    elif action == "page" and args:
        await event.answer()
        await _refresh(event, store, args[0])
    else:  # noop и неизвестные данные
        await event.answer()
