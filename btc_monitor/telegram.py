from __future__ import annotations

import requests

from .config import HTTP_TIMEOUT, TELEGRAM_SAFE_LIMIT


class TelegramError(RuntimeError):
    pass


def split_message(text: str, limit: int = TELEGRAM_SAFE_LIMIT) -> list[str]:
    text = text.strip()
    if not text:
        return []
    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    current = ""

    def append_piece(piece: str) -> None:
        nonlocal current
        candidate = f"{current}\n\n{piece}" if current else piece
        if len(candidate) <= limit:
            current = candidate
            return
        if current:
            chunks.append(current)
            current = ""
        if len(piece) <= limit:
            current = piece
            return
        lines = piece.splitlines() or [piece]
        for line in lines:
            while len(line) > limit:
                split_at = line.rfind(" ", 0, limit)
                if split_at < limit // 2:
                    split_at = limit
                chunks.append(line[:split_at].strip())
                line = line[split_at:].strip()
            if line:
                if current and len(current) + len(line) + 1 <= limit:
                    current += "\n" + line
                elif current:
                    chunks.append(current)
                    current = line
                else:
                    current = line

    for paragraph in paragraphs:
        append_piece(paragraph)
    if current:
        chunks.append(current)
    return chunks


def send_telegram_message(session: requests.Session, token: str, chat_id: str, text: str) -> int:
    chunks = split_message(text)
    if not chunks:
        raise TelegramError("Нельзя отправить пустое сообщение")
    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    # Do not reuse the retry-enabled data session: urllib3 retry logs can include
    # Telegram's token-bearing URL. A fresh session keeps credentials out of logs.
    telegram_session = requests.Session()
    telegram_session.headers.update(session.headers)
    try:
        for index, chunk in enumerate(chunks, start=1):
            if len(chunks) > 1:
                chunk = f"{chunk}\n\n[{index}/{len(chunks)}]"
            try:
                response = telegram_session.post(
                    endpoint,
                    data={
                        "chat_id": chat_id,
                        "text": chunk,
                        "disable_web_page_preview": "true",
                    },
                    timeout=HTTP_TIMEOUT,
                )
            except requests.RequestException:
                raise TelegramError("Не удалось соединиться с Telegram API") from None
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            if response.status_code >= 400 or not payload.get("ok"):
                description = str(payload.get("description", "неизвестная ошибка"))[:240]
                raise TelegramError(
                    f"Telegram API отклонил сообщение (HTTP {response.status_code}): {description}"
                )
    finally:
        telegram_session.close()
    return len(chunks)
