"""Telegram-бот: получает ссылку на чат и присылает выжимку непрочитанных сообщений по дням.

Два клиента Telethon в одном процессе:
  * user — ваш аккаунт, читает чаты (при первом запуске попросит телефон и код);
  * bot  — бот из @BotFather, общается с вами.
"""
from __future__ import annotations

import asyncio
import logging

from telethon import TelegramClient, events

from auth import login_user
from config import load_settings
from llm import LLMError, build_provider
from media import send_media
from summarize import summarize_by_day
from tg_fetch import (
    ChatAccessError,
    RequestError,
    can_mark_read,
    fetch_for_request,
    mark_read,
    parse_request,
    resolve_entity,
    resolve_topic,
)

log = logging.getLogger("tg-summarizer")

HELP = (
    "Пришлите ссылку на чат, группу или канал (t.me/..., @username) — "
    "я пришлю выжимку по дням.\n\n"
    "После ссылки можно уточнить, что читать:\n"
    "• ничего — непрочитанные сообщения;\n"
    "• число — последние N сообщений: https://t.me/чат 200;\n"
    "• дата — начиная с неё и до сегодня: https://t.me/чат 10.10;\n"
    "• две даты — за период, обе включительно: https://t.me/чат 01.10 10.10.\n"
    "Даты: ДД.ММ, ДД.ММ.ГГГГ, ГГГГ-ММ-ДД, а также «сегодня» и «вчера».\n\n"
    "В группе с темами ссылка на тему (https://t.me/чат/номер_темы) читает только эту тему, "
    "ссылка без номера — всю группу.\n\n"
    "Аккаунт, под которым запущен скрипт, должен состоять в чате (публичные каналы с числом "
    "или датами можно читать и без вступления). Если включён mark_as_read, после выжимки "
    "непрочитанных (запрос без числа и дат) они отмечаются прочитанными в Telegram."
)


async def send_long(event, text: str, limit: int = 4000) -> None:
    """Telegram ограничивает сообщение 4096 символами — режем по строкам."""
    while text:
        if len(text) <= limit:
            await event.respond(text, link_preview=False)  # без превью, иначе ссылки раздувают сообщение
            return
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        await event.respond(text[:cut], link_preview=False)
        text = text[cut:].lstrip("\n")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s: %(message)s"
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)

    settings = load_settings()
    llm = build_provider(settings.llm)
    log.info("LLM-провайдер: %s", llm.name)

    user = TelegramClient(str(settings.sessions_dir / "user"), settings.api_id, settings.api_hash)
    bot = TelegramClient(str(settings.sessions_dir / "bot"), settings.api_id, settings.api_hash)
    await login_user(user, settings.login_method)  # при первом запуске: QR-код (или телефон)
    await bot.start(bot_token=settings.bot_token)

    lock = asyncio.Lock()  # один запрос за раз, чтобы не упереться в лимиты

    async def handle_request(event, req) -> None:
        status = await event.respond("⏳ Читаю чат…")
        try:
            entity = await resolve_entity(user, req.ref)
            topic, topic_note = await resolve_topic(user, entity, req.ref.topic_id)
            batch = await fetch_for_request(
                user,
                entity,
                req,
                settings.summary["max_messages"],
                settings.summary["timezone"],
                topic,
            )
            info = f"\nℹ️ {topic_note}" if topic_note else ""
            if not batch.messages:
                if batch.mode == "unread":
                    await status.edit(f"В «{batch.title}» нет непрочитанных сообщений.{info}")
                else:
                    await status.edit(
                        f"В «{batch.title}» нет сообщений для выжимки ({batch.description}).{info}"
                    )
                return

            header = f"📬 {batch.title}: {batch.description}{info}"
            if batch.note:
                header += f"\n⚠️ {batch.note}"
            await status.edit(header + "\n⏳ Делаю выжимку…")

            async for digest in summarize_by_day(
                llm, batch.title, batch.messages, settings.summary, batch.link_base
            ):
                await send_long(
                    event, f"📅 {digest.label} · {digest.count} сообщ.\n\n{digest.summary}"
                )
                if digest.media:
                    await send_media(
                        user,
                        bot,
                        event.chat_id,
                        digest.media,
                        settings.summary["max_media_mb"],
                        batch.link_base,
                    )
            done = "\n✅ Готово"
            if settings.mark_as_read and can_mark_read(entity, batch):
                # отмечаем только после того, как вся выжимка отправлена, чтобы при сбое
                # сообщения остались непрочитанными
                try:
                    await mark_read(user, entity, batch)
                    done += " Чат отмечен прочитанным" + (
                        " до последнего учтённого сообщения." if batch.truncated else "."
                    )
                except Exception as e:  # noqa: BLE001 - выжимка уже доставлена, это не критично
                    log.warning("Не удалось отметить прочитанным: %s", e)
                    done += f"\n⚠️ Не удалось отметить прочитанным: {type(e).__name__}"
            await status.edit(header + done)
        except ChatAccessError as e:
            await status.edit(f"⚠️ {e}")
        except LLMError as e:
            log.error("Ошибка LLM: %s", e)
            await status.edit(f"⚠️ Ошибка LLM-провайдера: {e}")
        except Exception as e:  # noqa: BLE001
            log.exception("Непредвиденная ошибка")
            await status.edit(f"⚠️ Непредвиденная ошибка: {type(e).__name__}: {e}")

    @bot.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
    async def on_message(event) -> None:
        if event.sender_id not in settings.allowed_user_ids:
            await event.respond(f"Нет доступа. Ваш ID: {event.sender_id}")
            return
        text = (event.raw_text or "").strip()
        if text.startswith(("/start", "/help")):
            await event.respond(HELP)
            return
        try:
            req = parse_request(text, settings.summary["timezone"])
        except RequestError as e:
            await event.respond(f"⚠️ {e}")
            return
        if req is None:
            await event.respond(
                "Не нашёл ссылку на чат. Примеры: https://t.me/durov, @durov 100, "
                "https://t.me/durov 01.10 10.10 (подробности: /help)"
            )
            return
        if lock.locked():
            await event.respond("Ещё обрабатываю предыдущий запрос, подождите.")
            return
        async with lock:
            await handle_request(event, req)

    log.info("Бот запущен. Отправьте ему ссылку на чат.")
    try:
        await bot.run_until_disconnected()
    finally:
        await user.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
