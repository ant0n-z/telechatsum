"""Список сохранённых чатов (меню бота): JSON-файл, у каждого пользователя свой список.

Чат попадает в список автоматически после первого успешного запроса. Хранится не то, что вы
прислали (инвайт-ссылки протухают), а устойчивая ссылка: публичное имя или числовой id.
Формат файла:
  {"version": 1, "users": {"<telegram id>": {"next_id": 3, "chats": [
      {"id": 1, "title": "Qiyuan / Флуд", "kind": "username", "value": "QiyuanChat",
       "topic_id": 2035, "last_used": 1760000000.0}]}}}
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from tg_fetch import ChatRef

log = logging.getLogger(__name__)


@dataclass
class SavedChat:
    id: int  # короткий номер внутри списка пользователя; попадает в данные кнопок (лимит 64 байта)
    title: str
    kind: str
    value: str
    topic_id: int | None
    last_used: float

    @property
    def ref(self) -> ChatRef:
        return ChatRef(self.kind, self.value, self.topic_id)


def _key(ref: ChatRef) -> tuple:
    """По чему узнаём тот же чат: имя без учёта регистра, id и номер темы."""
    value = ref.value.lower() if ref.kind == "username" else ref.value
    return ref.kind, value, ref.topic_id


class ChatStore:
    def __init__(self, path: str | Path, max_chats: int = 30):
        self.path = Path(path)
        self.max_chats = max(1, int(max_chats))
        self._data = self._load()

    # ------------------------------------------------------------ файл

    def _load(self) -> dict:
        empty = {"version": 1, "users": {}}
        if not self.path.exists():
            return empty
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("users"), dict):
                raise ValueError("неожиданная структура файла")
            return data
        except (OSError, ValueError) as e:
            backup = self.path.with_suffix(self.path.suffix + ".bak")
            log.warning("Список чатов %s повреждён (%s), он сохранён как %s", self.path, e, backup)
            try:
                os.replace(self.path, backup)
            except OSError:
                pass
            return empty

    def _save(self) -> None:
        """Атомарная запись: сначала во временный файл рядом, затем подмена."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _user(self, user_id: int) -> dict:
        u = self._data["users"].setdefault(str(user_id), {"next_id": 1, "chats": []})
        u.setdefault("next_id", 1)
        u.setdefault("chats", [])
        return u

    @staticmethod
    def _to_chat(d: dict) -> SavedChat | None:
        try:
            return SavedChat(
                id=int(d["id"]),
                title=str(d.get("title") or d["value"]),
                kind=str(d["kind"]),
                value=str(d["value"]),
                topic_id=int(d["topic_id"]) if d.get("topic_id") is not None else None,
                last_used=float(d.get("last_used", 0)),
            )
        except (KeyError, TypeError, ValueError):
            return None  # битую запись молча пропускаем

    # ------------------------------------------------------------ операции

    def list(self, user_id: int) -> list[SavedChat]:
        """Сначала те, чем пользовались недавно."""
        chats = [c for c in map(self._to_chat, self._user(user_id)["chats"]) if c]
        return sorted(chats, key=lambda c: (c.last_used, c.id), reverse=True)

    def get(self, user_id: int, chat_id: int) -> SavedChat | None:
        return next((c for c in self.list(user_id) if c.id == chat_id), None)

    def add_or_touch(
        self, user_id: int, ref: ChatRef, title: str, now: float | None = None
    ) -> tuple[SavedChat, bool]:
        """Добавляет чат (или обновляет название и время использования). Возвращает (запись, новый ли).

        Если список переполнен, удаляется тот чат, которым не пользовались дольше всего.
        """
        now = time.time() if now is None else now
        u = self._user(user_id)
        for d in u["chats"]:
            chat = self._to_chat(d)
            if chat and _key(chat.ref) == _key(ref):
                d["title"], d["last_used"] = title, now
                self._save()
                return self._to_chat(d), False

        entry = {
            "id": u["next_id"],
            "title": title,
            "kind": ref.kind,
            "value": ref.value,
            "topic_id": ref.topic_id,
            "last_used": now,
        }
        u["next_id"] += 1
        u["chats"].append(entry)
        while len(u["chats"]) > self.max_chats:
            oldest = min(u["chats"], key=lambda d: (d.get("last_used", 0), d.get("id", 0)))
            u["chats"].remove(oldest)
        self._save()
        return self._to_chat(entry), True

    def remove(self, user_id: int, chat_id: int) -> bool:
        u = self._user(user_id)
        before = len(u["chats"])
        u["chats"] = [d for d in u["chats"] if d.get("id") != chat_id]
        removed = len(u["chats"]) != before
        if removed:
            self._save()
        return removed
