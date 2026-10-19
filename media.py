"""Отправка вложений, которые модель выбрала для выжимки.

Бот не состоит в читаемом чате, поэтому файл скачивает пользовательский аккаунт,
а отправляет его бот — в диалог с вами.
"""
from __future__ import annotations

import logging
import tempfile

log = logging.getLogger(__name__)


def _fits(m, max_bytes: int) -> bool:
    size = getattr(getattr(m, "file", None), "size", None)
    return not (size and size > max_bytes)


async def send_media(user, bot, chat_id, items, max_mb: float, link_base: str | None) -> None:
    """Скачивает вложения аккаунтом `user` и отправляет их ботом `bot` в чат `chat_id`."""
    max_bytes = int(max_mb * 1024 * 1024)
    for item in items:
        link = f"{link_base}/{item.msg_id}" if link_base else None
        msgs = [m for m in item.messages if _fits(m, max_bytes)]
        if not msgs:
            continue
        try:
            with tempfile.TemporaryDirectory() as tmp:
                paths = []
                for m in msgs:
                    path = await user.download_media(m, file=tmp)
                    if path:
                        paths.append(path)
                if not paths:
                    continue
                kwargs = {"caption": f"↗ {link}"} if link else {}
                if len(paths) == 1:
                    # у одиночного файла сохраняем исходные атрибуты (гифка останется гифкой,
                    # видео — видео, а не файлом)
                    attrs = getattr(getattr(msgs[0], "document", None), "attributes", None)
                    if attrs:
                        kwargs["attributes"] = attrs
                    await bot.send_file(
                        chat_id, paths[0], supports_streaming=item.kind != "фото", **kwargs
                    )
                else:
                    await bot.send_file(chat_id, paths, **kwargs)
        except Exception as e:  # noqa: BLE001 - одно упавшее вложение не должно ломать выжимку
            log.warning("Не удалось приложить медиа #%s: %s", item.msg_id, e)
            note = f"⚠️ Не удалось приложить медиа #{item.msg_id}"
            await bot.send_message(chat_id, note + (f": {link}" if link else ""), link_preview=False)
