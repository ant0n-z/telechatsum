"""Вход пользовательского аккаунта: по QR-коду (по умолчанию) или по номеру телефона.

QR: скрипт рисует код в терминале, вы сканируете его в приложении Telegram на телефоне
(Настройки → Устройства → Подключить устройство). Код живёт около 30 секунд, поэтому
скрипт сам обновляет его, пока вы не отсканируете. Если включена двухфакторная защита,
спросит облачный пароль (или возьмёт его из переменной TELEGRAM_2FA_PASSWORD).
"""
from __future__ import annotations

import asyncio
import getpass
import logging
import os

from telethon.errors import PasswordHashInvalidError, SessionPasswordNeededError

log = logging.getLogger(__name__)

LOGIN_METHODS = ("qr", "phone")


def print_qr(url: str) -> None:
    """Рисует QR в терминале; если пакет qrcode недоступен, печатает ссылку."""
    try:
        import qrcode

        code = qrcode.QRCode(border=1)
        code.add_data(url)
        code.make()
        print("\nОтсканируйте QR-код: Telegram → Настройки → Устройства → Подключить устройство")
        code.print_ascii(invert=True)  # invert=True — для тёмного фона терминала
    except ImportError:
        print(
            "\nУстановите пакет qrcode (pip install qrcode), чтобы увидеть QR в терминале.\n"
            f"Ссылка для входа (откройте её на телефоне с Telegram): {url}"
        )


async def _ask_password() -> str:
    return await asyncio.to_thread(getpass.getpass, "Облачный пароль Telegram (2FA): ")


async def _two_factor(client, password: str | None, ask_password) -> None:
    from_env = bool(password)
    for _ in range(3):
        pw = password or await ask_password()
        try:
            await client.sign_in(password=pw)
            return
        except PasswordHashInvalidError:
            if from_env:
                raise SystemExit("Пароль из TELEGRAM_2FA_PASSWORD не подошёл.") from None
            print("Неверный пароль, попробуйте ещё раз.")
    raise SystemExit("Не удалось войти: пароль 2FA введён неверно три раза.")


async def login_user(
    client,
    method: str = "qr",
    *,
    password: str | None = None,
    show_qr=print_qr,
    ask_password=_ask_password,
    attempts: int = 10,
    timeout: float = 30,
) -> None:
    """Авторизует клиент. Если сессия уже сохранена, ничего не спрашивает."""
    if method not in LOGIN_METHODS:
        raise SystemExit(f"[telegram].login должен быть одним из: {', '.join(LOGIN_METHODS)}")
    if password is None:
        password = os.environ.get("TELEGRAM_2FA_PASSWORD", "").strip() or None

    await client.connect()
    if await client.is_user_authorized():
        return

    if method == "phone":
        await client.start()  # интерактивно: телефон, код, пароль 2FA
        return

    qr = await client.qr_login()
    for _ in range(attempts):
        show_qr(qr.url)
        try:
            await qr.wait(timeout)
            return
        except asyncio.TimeoutError:
            await qr.recreate()  # код устарел — рисуем новый
        except SessionPasswordNeededError:
            await _two_factor(client, password, ask_password)
            return
    raise SystemExit("QR-код не был отсканирован. Запустите скрипт ещё раз.")
