"""Разбор запроса пользователя и получение сообщений через пользовательский аккаунт.

Запрос — это ссылка на чат и, необязательно, уточнение:
  <ссылка>                      непрочитанные сообщения
  <ссылка> 200                  последние 200 сообщений
  <ссылка> 10.10                с 10 октября до сегодня
  <ссылка> 01.10 10.10          с 1 по 10 октября включительно

В группах с темами (форумах) ссылка вида t.me/имя/НОМЕР_ТЕМЫ (или t.me/c/ID/НОМЕР_ТЕМЫ)
читает только эту тему; ссылка без номера читает всю группу.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from telethon import TelegramClient, errors, utils
from telethon.tl import functions as tl_functions
from telethon.tl.functions.messages import (
    CheckChatInviteRequest,
    GetPeerDialogsRequest,
    ReadDiscussionRequest,
)
from telethon.tl.types import (
    Channel,
    Chat,
    ChatInviteAlready,
    InputDialogPeer,
    Message,
    PeerChannel,
    PeerChat,
    User,
)


class ChatAccessError(Exception):
    """Чат не найден или у аккаунта нет к нему доступа. Текст безопасно показывать пользователю."""


class RequestError(Exception):
    """Не удалось разобрать уточнение к ссылке. Текст безопасно показывать пользователю."""


@dataclass(frozen=True)
class ChatRef:
    kind: str  # "username" | "channel_id" | "chat_id" (обычная малая группа) | "invite"
    value: str
    # Первое число после имени чата. Если чат — форум и такая тема есть, читается только она;
    # в обычных группах это номер сообщения и игнорируется.
    topic_id: int | None = None


@dataclass(frozen=True)
class Request:
    ref: ChatRef
    mode: str = "unread"  # "unread" | "last" | "period"
    limit: int | None = None  # для "last": сколько последних сообщений
    since: date | None = None  # для "period": с какой даты (включительно)
    until: date | None = None  # для "period": по какую дату включительно; None — до сегодня


_LINK_RE = re.compile(r"(?:https?://)?(?:www\.)?(?:t|telegram)\.me/([^\s?#]+)", re.I)
_AT_RE = re.compile(r"(?<![\w/])@([A-Za-z][A-Za-z0-9_]{3,31})")
_USERNAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}")
_DATE_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?\b|сегодня|вчера", re.I
)
_NUMBER_RE = re.compile(r"\b\d+\b")


def _topic_from(parts: list[str]) -> int | None:
    """Номер темы: первое число после имени чата (t.me/имя/ТЕМА[/сообщение])."""
    return int(parts[0]) if parts and parts[0].isdigit() else None


def _ref_from_link(path: str) -> ChatRef | None:
    parts = [p for p in path.split("/") if p]
    if not parts:
        return None
    head = parts[0]
    if head == "c" and len(parts) >= 2 and parts[1].isdigit():
        return ChatRef("channel_id", parts[1], _topic_from(parts[2:]))
    if head.startswith("+") and len(head) > 1:
        return ChatRef("invite", head[1:])
    if head.lower() == "joinchat" and len(parts) >= 2:
        return ChatRef("invite", parts[1])
    if _USERNAME_RE.fullmatch(head):
        return ChatRef("username", head, _topic_from(parts[1:]))
    return None


def _find_ref(text: str) -> tuple[ChatRef, int, int] | None:
    """Ищет ссылку на чат; возвращает (ссылка, начало, конец) её слова в тексте."""
    m = _LINK_RE.search(text)
    if m:
        # знаки препинания после ссылки («(t.me/chat)», «t.me/chat.») к ней не относятся
        ref = _ref_from_link(m.group(1).rstrip(".,;:!)]}»\"'"))
        if ref is None:
            return None
        start, end = m.start(), m.end()
        while start > 0 and not text[start - 1].isspace():
            start -= 1
        while end < len(text) and not text[end].isspace():
            end += 1
        return ref, start, end
    m = _AT_RE.search(text)
    if m:
        return ChatRef("username", m.group(1)), m.start(), m.end()
    return None


def parse_chat_ref(text: str) -> ChatRef | None:
    """Понимает t.me/name, t.me/name/123, t.me/c/<id>/123, t.me/+hash, t.me/joinchat/hash, @name."""
    found = _find_ref(text)
    return found[0] if found else None


def _to_date(token: str, today: date) -> date:
    t = token.lower()
    if t == "сегодня":
        return today
    if t == "вчера":
        return today - timedelta(days=1)
    try:
        if "-" in t:
            y, mo, d = (int(x) for x in t.split("-"))
            return date(y, mo, d)
        parts = re.split(r"[./]", t)
        d, mo = int(parts[0]), int(parts[1])
        if len(parts) == 2:  # без года: текущий, а если дата ещё не наступила — прошлый
            result = date(today.year, mo, d)
            return result if result <= today else date(today.year - 1, mo, d)
        if len(parts[2]) == 2:
            return date(2000 + int(parts[2]), mo, d)
        if len(parts[2]) == 4:
            return date(int(parts[2]), mo, d)
    except ValueError:
        pass
    raise RequestError(
        f"Не понял дату «{token}». Форматы: ДД.ММ, ДД.ММ.ГГГГ, ГГГГ-ММ-ДД, «сегодня», «вчера»."
    )


def parse_request(text: str, tz_name: str = "Europe/Moscow", today: date | None = None) -> Request | None:
    """Разбирает сообщение пользователя. None — ссылки на чат нет; RequestError — плохое уточнение."""
    found = _find_ref(text)
    if found is None:
        return None
    ref, start, end = found
    rest = text[:start] + " " + text[end:]
    today = today or datetime.now(ZoneInfo(tz_name)).date()

    dates = [_to_date(m.group(), today) for m in _DATE_RE.finditer(rest)]
    numbers = _NUMBER_RE.findall(_DATE_RE.sub(" ", rest))

    if dates and numbers:
        raise RequestError("Укажите либо число сообщений, либо даты, но не всё сразу.")
    if len(numbers) > 1:
        raise RequestError("Укажите одно число: сколько последних сообщений прочитать.")
    if len(dates) > 2:
        raise RequestError("Можно указать не больше двух дат: начало и конец периода.")

    if numbers:
        n = int(numbers[0])
        if n < 1:
            raise RequestError("Число сообщений должно быть больше нуля.")
        return Request(ref, "last", limit=n)
    if dates:
        since, until = min(dates), (max(dates) if len(dates) == 2 else None)
        if since > today:
            raise RequestError(f"Дата {since:%d.%m.%Y} ещё не наступила.")
        return Request(ref, "period", since=since, until=until)
    return Request(ref)


async def resolve_entity(client: TelegramClient, ref: ChatRef):
    try:
        if ref.kind == "invite":
            res = await client(CheckChatInviteRequest(ref.value))
            if isinstance(res, ChatInviteAlready):
                return res.chat
            raise ChatAccessError(
                "Аккаунт скрипта не состоит в этом чате. Вступите в него вручную и пришлите ссылку ещё раз."
            )

        if ref.kind == "channel_id":
            peer = PeerChannel(int(ref.value))
        elif ref.kind == "chat_id":
            peer = PeerChat(int(ref.value))
        else:
            peer = ref.value
        try:
            return await client.get_entity(peer)
        except ValueError:
            # сущности нет в кэше сессии — подгружаем список диалогов и пробуем ещё раз
            await client.get_dialogs()
            return await client.get_entity(peer)
    except (
        errors.UsernameNotOccupiedError,
        errors.UsernameInvalidError,
        errors.ChannelPrivateError,
        errors.InviteHashExpiredError,
        errors.InviteHashInvalidError,
        ValueError,
    ):
        raise ChatAccessError(
            "Не удалось найти чат или у аккаунта скрипта нет к нему доступа."
        ) from None


def build_link_base(entity) -> str | None:
    """Основа для ссылок на сообщения: https://t.me/name или https://t.me/c/<id>.

    У обычных (не супер-) групп и личных диалогов ссылок на сообщения нет — вернётся None.
    """
    if not isinstance(entity, Channel):
        return None
    username = getattr(entity, "username", None)
    if username:
        return f"https://t.me/{username}"
    return f"https://t.me/c/{entity.id}"


def canonical_ref(entity, topic: "Topic | None" = None) -> ChatRef | None:
    """Устойчивая ссылка на чат для сохранения в меню: публичное имя, иначе числовой id.

    Инвайт-ссылки протухают, поэтому сохраняется то, что получилось после открытия чата.
    Для личных диалогов без публичного имени вернётся None (открыть их позже нельзя).
    """
    topic_id = topic.id if topic else None
    if isinstance(entity, Channel):
        username = getattr(entity, "username", None)
        if username:
            return ChatRef("username", username, topic_id)
        return ChatRef("channel_id", str(entity.id), topic_id)
    if isinstance(entity, Chat):
        return ChatRef("chat_id", str(entity.id))
    if isinstance(entity, User) and getattr(entity, "username", None):
        return ChatRef("username", entity.username)
    return None


# ---------------------------------------------------------------- темы форума

@dataclass(frozen=True)
class Topic:
    id: int
    title: str
    read_max_id: int = 0  # последнее прочитанное сообщение темы
    unread: int = 0


def is_forum(entity) -> bool:
    return bool(isinstance(entity, Channel) and getattr(entity, "forum", False))


def _topics_request(entity, topic_id: int):
    # В свежих версиях Telethon метод лежит в messages, в старых — в channels
    cls = getattr(tl_functions.messages, "GetForumTopicsByIDRequest", None)
    if cls is not None:
        return cls(peer=entity, topics=[topic_id])
    return tl_functions.channels.GetForumTopicsByIDRequest(channel=entity, topics=[topic_id])


async def resolve_topic(client: TelegramClient, entity, topic_id: int | None) -> tuple[Topic | None, str]:
    """Возвращает (тема или None, пояснение для пользователя).

    Тема возвращается только для форума и только если такая тема существует. Во всех остальных
    случаях читается весь чат, а число в ссылке — это просто номер сообщения.
    """
    if not is_forum(entity):
        return None, ""
    if topic_id is None:
        return None, (
            "Это группа с темами: ссылка без номера темы читает сообщения всей группы. "
            "Чтобы прочитать одну тему, пришлите ссылку вида t.me/имя/номер_темы."
        )
    try:
        res = await client(_topics_request(entity, topic_id))
    except errors.RPCError:
        res = None
    for t in getattr(res, "topics", None) or []:
        if getattr(t, "id", None) == topic_id and hasattr(t, "read_inbox_max_id"):
            return (
                Topic(
                    id=topic_id,
                    title=(getattr(t, "title", "") or f"тема {topic_id}").strip(),
                    read_max_id=t.read_inbox_max_id or 0,
                    unread=t.unread_count or 0,
                ),
                "",
            )
    return None, f"Тема №{topic_id} не найдена, читаю всю группу."


def _thread(topic: Topic | None) -> int | None:
    return topic.id if topic else None


def _title(entity, topic: Topic | None) -> str:
    base = utils.get_display_name(entity) or "чат"
    return f"{base} / {topic.title}" if topic else base


# ---------------------------------------------------------------- чтение сообщений

@dataclass
class Batch:
    title: str
    messages: list[Message]  # от старых к новым
    mode: str = "unread"  # "unread" | "last" | "period"
    description: str = ""  # что именно прочитано — для заголовка в боте
    note: str = ""  # предупреждение, если сработал лимит max_messages
    truncated: bool = False
    link_base: str | None = None  # см. build_link_base
    unread_count: int | None = None
    topic: Topic | None = None  # если читалась одна тема форума


UnreadBatch = Batch  # прежнее имя


async def fetch_unread(
    client: TelegramClient, entity, max_messages: int, topic: Topic | None = None
) -> Batch:
    if topic is not None:
        unread, read_max_id = topic.unread, topic.read_max_id
    else:
        input_peer = await client.get_input_entity(entity)
        res = await client(GetPeerDialogsRequest(peers=[InputDialogPeer(input_peer)]))
        if not res.dialogs:
            raise ChatAccessError(
                "Этого чата нет в диалогах аккаунта скрипта: он должен в нём состоять."
            )
        dialog = res.dialogs[0]
        unread, read_max_id = dialog.unread_count, dialog.read_inbox_max_id

    messages: list[Message] = []
    if unread:
        # reverse=True + min_id: идём от первого непрочитанного к самому новому
        async for m in client.iter_messages(
            entity, min_id=read_max_id, reverse=True, limit=max_messages, reply_to=_thread(topic)
        ):
            if m.id > read_max_id:
                messages.append(m)

    truncated = len(messages) >= max_messages and unread > len(messages)
    return Batch(
        title=_title(entity, topic),
        messages=messages,
        mode="unread",
        description=f"непрочитанных {unread}",
        note=f"Взято только первые {len(messages)} (лимит max_messages в config.toml)" if truncated else "",
        truncated=truncated,
        link_base=build_link_base(entity),
        unread_count=unread,
        topic=topic,
    )


async def fetch_last(
    client: TelegramClient, entity, n: int, max_messages: int, topic: Topic | None = None
) -> Batch:
    """Последние n сообщений независимо от того, прочитаны они или нет."""
    take = min(n, max_messages)
    messages = [m async for m in client.iter_messages(entity, limit=take, reply_to=_thread(topic))]
    messages.reverse()
    truncated = n > max_messages
    return Batch(
        title=_title(entity, topic),
        messages=messages,
        mode="last",
        description=f"последние {len(messages)} сообщений",
        note=f"Запрошено {n}, но лимит max_messages в config.toml — {max_messages}" if truncated else "",
        truncated=truncated,
        link_base=build_link_base(entity),
        topic=topic,
    )


async def fetch_period(
    client: TelegramClient,
    entity,
    since: date,
    until: date | None,
    max_messages: int,
    tz_name: str,
    topic: Topic | None = None,
) -> Batch:
    """Сообщения с начала дня `since` до конца дня `until` (или до сегодняшнего момента)."""
    tz = ZoneInfo(tz_name)
    start = datetime.combine(since, time.min, tzinfo=tz)
    end = datetime.combine(until + timedelta(days=1), time.min, tzinfo=tz) if until else None

    messages: list[Message] = []
    truncated = False
    # reverse=True и offset_date: идём вперёд от начала периода; границы перепроверяем сами
    async for m in client.iter_messages(
        entity, offset_date=start - timedelta(seconds=1), reverse=True, reply_to=_thread(topic)
    ):
        if end is not None and m.date >= end:
            break
        if m.date < start:
            continue
        if len(messages) >= max_messages:
            truncated = True
            break
        messages.append(m)

    period = f"с {since:%d.%m.%Y}" + (f" по {until:%d.%m.%Y}" if until else "")
    return Batch(
        title=_title(entity, topic),
        messages=messages,
        mode="period",
        description=f"{period}, сообщений {len(messages)}" + ("+" if truncated else ""),
        note=(
            f"Лимит max_messages ({max_messages}) в config.toml: взяты самые ранние сообщения периода"
            if truncated
            else ""
        ),
        truncated=truncated,
        link_base=build_link_base(entity),
        topic=topic,
    )


async def fetch_for_request(
    client: TelegramClient,
    entity,
    req: Request,
    max_messages: int,
    tz_name: str,
    topic: Topic | None = None,
) -> Batch:
    if req.mode == "last":
        return await fetch_last(client, entity, req.limit, max_messages, topic)
    if req.mode == "period":
        return await fetch_period(client, entity, req.since, req.until, max_messages, tz_name, topic)
    return await fetch_unread(client, entity, max_messages, topic)


# ---------------------------------------------------------------- отметка прочитанным

def can_mark_read(entity, batch: Batch) -> bool:
    """Отмечаем только запрос «непрочитанные»; у форума без указанной темы — нельзя."""
    if batch.mode != "unread" or not batch.messages:
        return False
    return not (is_forum(entity) and batch.topic is None)


async def mark_read(client: TelegramClient, entity, batch: Batch) -> int:
    """Отмечает прочитанным всё до последнего сообщения, попавшего в выжимку. Возвращает его id.

    Если сработал лимит max_messages, более новые сообщения остаются непрочитанными.
    """
    max_id = max(m.id for m in batch.messages)
    if batch.topic is not None:
        await client(
            ReadDiscussionRequest(peer=entity, msg_id=batch.topic.id, read_max_id=max_id)
        )
    else:
        await client.send_read_acknowledge(entity, max_id=max_id, clear_mentions=not batch.truncated)
    return max_id
