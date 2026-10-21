#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
memes_scheduler.py

Берёт картинки из папки (альбома) memes_images на Android-устройстве и
ставит их в ОТЛОЖЕННЫЕ сообщения Telegram-канала с ровным шагом (в часах),
пока не упрётся в лимит отложенных (~100 на чат).

Запуск:  python memes_scheduler.py
Зависимости: kurigram (форк pyrogram), Pillow (для конвертации форматов)
"""

import asyncio
import os
import re
import shutil
import sys
import tempfile
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Pyrogram на новых версиях Python требует event loop уже на этапе импорта
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

try:
    from pyrogram import Client
    from pyrogram.errors import BadRequest, FloodWait, RPCError
    from pyrogram.raw import functions
except ImportError:
    sys.exit("Не установлен kurigram/pyrogram. Выполните: pip install kurigram")

try:
    from PIL import Image, ImageOps

    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# HEIC/HEIF (фото с некоторых камер) — необязательная поддержка
try:
    import pillow_heif  # type: ignore

    pillow_heif.register_heif_opener()
except Exception:
    pass

# ----------------------------- НАСТРОЙКИ ------------------------------------
ALBUM_NAME = "memes_images"      # имя папки-альбома
SENT_DIR_NAME = "_sent"          # сюда переносятся уже отправленные файлы
MOVE_SENT = True                 # False — не переносить файлы после отправки
PAUSE_BETWEEN = 1.5              # пауза между запросами, сек (защита от флуда)
SCHEDULE_LIMIT = 100             # лимит отложенных сообщений Telegram на чат
SESSION_NAME = "memes_scheduler"  # имя файла сессии (создаётся рядом со скриптом)
# ----------------------------------------------------------------------------

IMAGE_EXT = {
    ".jpg", ".jpeg", ".jfif", ".png", ".webp", ".gif", ".bmp",
    ".tif", ".tiff", ".heic", ".heif", ".avif",
}
DIRECT_EXT = {".jpg", ".jpeg", ".png"}          # отправляются как есть
PHOTO_ERRORS = (
    "PHOTO_INVALID_DIMENSIONS", "PHOTO_SAVE_FILE_INVALID",
    "IMAGE_PROCESS_FAILED", "PHOTO_EXT_INVALID", "PHOTO_CROP_SIZE_SMALL",
)
MAX_PHOTO_BYTES = 10 * 1024 * 1024


# ----------------------------- ВВОД ДАННЫХ ----------------------------------
def ask(prompt: str) -> str:
    return input(prompt).strip()


def ask_int(prompt: str) -> int:
    while True:
        raw = ask(prompt)
        if raw.isdigit():
            return int(raw)
        print("  Нужно целое число.")


def ask_hours(prompt: str) -> float:
    while True:
        raw = ask(prompt).replace(",", ".")
        try:
            val = float(raw)
            if val > 0:
                return val
        except ValueError:
            pass
        print("  Введите положительное число, например 6 или 1.5")


def ask_yes(prompt: str, default: bool = True) -> bool:
    raw = ask(prompt + (" [Y/n]: " if default else " [y/N]: ")).lower()
    if not raw:
        return default
    return raw in ("y", "yes", "д", "да")


def clean_channel(raw: str) -> str:
    raw = re.sub(r"^(https?://)?(t\.me|telegram\.me)/", "", raw.strip())
    return raw.lstrip("@").strip("/")


# ----------------------------- ПОИСК ПАПКИ ----------------------------------
def isdir(p: Path) -> bool:
    try:
        return p.is_dir()
    except OSError:
        return False


def find_album():
    home = Path.home()
    roots = [home / "storage" / "shared", Path("/storage/emulated/0"), Path("/sdcard")]
    roots = [r for r in roots if isdir(r)]

    for root in roots:
        for rel in ("Pictures", "DCIM", "Download", "Documents", ""):
            cand = root / rel / ALBUM_NAME
            if isdir(cand):
                return cand

    for root in roots:
        base = len(root.parts)
        for dirpath, dirnames, _ in os.walk(root):
            depth = len(Path(dirpath).parts) - base
            for d in dirnames:
                if d.lower() == ALBUM_NAME.lower():
                    return Path(dirpath) / d
            if depth >= 3:
                dirnames[:] = []
            else:
                dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "Android"]
    return None


def pick_folder() -> Path:
    found = find_album()
    if found:
        print(f"Найдена папка: {found}")
        raw = ask("Enter — использовать её, или введите другой путь: ")
        if not raw:
            return found
    else:
        print(f"Папка «{ALBUM_NAME}» не найдена автоматически.")
        if not (Path.home() / "storage").exists():
            print("  Подсказка: выполните в Termux `termux-setup-storage` и разрешите доступ.")
        raw = ask("Введите полный путь к папке с картинками: ")
    while True:
        p = Path(raw).expanduser()
        if isdir(p):
            return p
        raw = ask("  Такой папки нет. Введите путь ещё раз: ")


def list_images(folder: Path):
    files = [
        p for p in folder.iterdir()
        if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in IMAGE_EXT
    ]
    files.sort(key=lambda p: [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", p.name)])
    return files


# ----------------------------- ПОДГОТОВКА ФАЙЛОВ ----------------------------
def prepare_image(src: Path):
    """Возвращает (путь_для_отправки, это_временный_файл)."""
    ext = src.suffix.lower()
    size_ok = src.stat().st_size <= MAX_PHOTO_BYTES

    if ext in DIRECT_EXT and size_ok:
        if not HAS_PIL:
            return src, False
        try:
            with Image.open(src) as im:
                w, h = im.size
            if w + h <= 10000:
                return src, False
        except Exception:
            pass  # попробуем пересохранить ниже

    if not HAS_PIL:
        raise RuntimeError("для этого формата нужен Pillow (pkg install python-pillow)")

    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA") or "transparency" in im.info:
            rgba = im.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (255, 255, 255))
            bg.paste(rgba, mask=rgba.split()[-1])
            im = bg
        else:
            im = im.convert("RGB")
        im.thumbnail((2560, 2560))
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        tmp.close()
        im.save(tmp.name, "JPEG", quality=92, optimize=True)
    return Path(tmp.name), True


def err_code(e: Exception) -> str:
    return f"{getattr(e, 'ID', '')} {e}"


async def send_file(app: Client, chat_id, src: Path, when: datetime):
    if src.suffix.lower() == ".gif":
        await app.send_animation(chat_id=chat_id, animation=str(src), schedule_date=when)
        return

    prepared, is_tmp = await asyncio.to_thread(prepare_image, src)
    try:
        try:
            await app.send_photo(chat_id=chat_id, photo=str(prepared), schedule_date=when)
        except BadRequest as e:
            if any(x in err_code(e) for x in PHOTO_ERRORS):
                # нестандартные пропорции/размер — отправляем как файл
                await app.send_document(chat_id=chat_id, document=str(src), schedule_date=when)
            else:
                raise
    finally:
        if is_tmp:
            try:
                os.unlink(prepared)
            except OSError:
                pass


def move_sent(src: Path, sent_dir: Path):
    sent_dir.mkdir(exist_ok=True)
    dst = sent_dir / src.name
    n = 1
    while dst.exists():
        dst = sent_dir / f"{src.stem}_{n}{src.suffix}"
        n += 1
    shutil.move(str(src), str(dst))


# ----------------------------- ОТЛОЖЕННЫЕ -----------------------------------
async def existing_scheduled(app: Client, chat_id):
    """Даты (unix) уже стоящих в отложке сообщений канала."""
    try:
        peer = await app.resolve_peer(chat_id)
        res = await app.invoke(functions.messages.GetScheduledHistory(peer=peer, hash=0))
        return sorted(m.date for m in getattr(res, "messages", []) if getattr(m, "date", None))
    except Exception as e:
        print(f"  (не удалось получить уже отложенные сообщения: {e})")
        raise RuntimeError("Не удалось проверить отложенные сообщения") from e


def fmt(dt: datetime) -> str:
    return dt.astimezone().strftime("%d.%m.%Y %H:%M")


# ----------------------------- ОСНОВНОЙ КОД ---------------------------------
async def main():
    print("=== Планировщик мемов в отложенные сообщения Telegram ===\n")
    print("api_id и api_hash: https://my.telegram.org → API development tools\n")
    api_id = ask_int("api_id: ")
    api_hash = ask("api_hash: ")
    channel = clean_channel(ask("@имя канала: "))
    hours = ask_hours("Шаг между постами, часов (можно дробное, например 1.5): ")
    mode = ask("Источник: 1 — галерея, 2 — Telegram-канал [1]: ") or "1"
    while mode not in ("1", "2"):
        mode = ask("Введите 1 или 2: ")
    folder = pick_folder() if mode == "1" else None
    files = list_images(folder) if folder else []
    source_name = clean_channel(ask("@канал-источник или ID: ")) if mode == "2" else None
    if folder and not files:
        sys.exit("В папке нет изображений.")
    if mode == "2" and not source_name:
        sys.exit("Не указан канал-источник.")
    if not HAS_PIL:
        print("⚠ Pillow не установлен: файлы не в JPG/PNG конвертироваться не смогут.")

    step = timedelta(seconds=round(hours * 3600))
    workdir = str(Path(__file__).resolve().parent)

    print("\nПодключаюсь к Telegram (если сессии ещё нет — введите телефон, код и пароль 2FA)…")
    async with Client(SESSION_NAME, api_id=api_id, api_hash=api_hash, workdir=workdir) as app:
        try:
            chat = await app.get_chat(channel)
        except RPCError as e:
            sys.exit(f"Не удалось найти «{channel}»: {err_code(e)}")

        source = None
        if mode == "2":
            try:
                source = await app.get_chat(int(source_name) if source_name.lstrip("-").isdigit() else source_name)
                if source.id == chat.id:
                    sys.exit("Источник и цель должны быть разными каналами.")
                async for message in app.get_chat_history(source.id):
                    if message.photo:
                        files.append(message)
                files.sort(key=lambda message: message.id)
            except RPCError as e:
                sys.exit(f"Ошибка чтения источника: {err_code(e)}")
            if not files:
                sys.exit("В источнике нет фотографий.")
        existing = await existing_scheduled(app, chat.id)
        free = SCHEDULE_LIMIT - len(existing)
        if free <= 0:
            sys.exit(f"В канале уже {len(existing)} отложенных — лимит исчерпан.")

        now = datetime.now(timezone.utc)
        if existing:
            last = datetime.fromtimestamp(existing[-1], tz=timezone.utc)
            start = last + step
            if start < now + timedelta(minutes=1):
                start = now + step
        else:
            start = now + step
        if start.second or start.microsecond:
            start = start.replace(second=0, microsecond=0) + timedelta(minutes=1)

        planned = min(len(files), free)
        last_planned = start + step * (planned - 1)
        tz_label = datetime.now().astimezone().strftime("UTC%z")

        print("\n--- План ---")
        print(f"Канал:               {chat.title} (@{chat.username or channel})")
        print(f"Фотографий:         {len(files)}")
        print(f"Уже в отложке:       {len(existing)} (лимит ~{SCHEDULE_LIMIT}, свободно {free})")
        print(f"Шаг:                 {hours} ч")
        print(f"Первая публикация:   {fmt(start)}")
        print(f"Последняя (прогноз): {fmt(last_planned)}")
        print(f"Будет запланировано: до {planned} шт.")
        print(f"Время показано по поясу Termux ({tz_label}); в Telegram оно будет по поясу устройства.")
        if not ask_yes("\nНачинать?"):
            return

        sent_dir = folder / SENT_DIR_NAME if folder else None
        slot = 0
        sent = 0
        skipped = []
        stop_reason = None

        for src in files:
            if slot >= free:
                stop_reason = "Лимит отложенных сообщений исчерпан."
                break
            when = start + step * slot
            result = None
            while result is None:
                try:
                    if source:
                        await app.copy_message(chat.id, source.id, src.id, schedule_date=when)
                    else:
                        await send_file(app, chat.id, src, when)
                    result = "ok"
                except FloodWait as e:
                    wait = int(getattr(e, "value", 30)) + 1
                    print(f"   FloodWait: жду {wait} с…")
                    await asyncio.sleep(wait)
                except RPCError as e:
                    code = err_code(e)
                    if "SCHEDULE_TOO_MUCH" in code:
                        stop_reason = "Достигнут лимит отложенных сообщений Telegram."
                    elif "SCHEDULE_DATE_TOO_LATE" in code:
                        stop_reason = "Telegram не позволяет планировать так далеко вперёд (около года)."
                    else:
                        stop_reason = (
                            f"Ошибка Telegram: {code.strip()}\n"
                            "   Проверьте, что вы админ канала с правом публикации сообщений."
                        )
                    result = "stop"
                except Exception as e:
                    label = f"сообщение {src.id}" if source else src.name
                    print(f"   ✗ пропускаю {label}: {e}")
                    skipped.append(label)
                    result = "skip"

            if result == "stop":
                break
            if result == "skip":
                continue

            sent += 1
            slot += 1
            label = f"сообщение {src.id}" if source else src.name
            print(f"[{sent}/{planned}] {label} → {fmt(when)}")
            if source:
                try:
                    await app.delete_messages(source.id, src.id)
                except RPCError as e:
                    print(f"   Не удалось удалить исходное сообщение {src.id}: {err_code(e)}")
            elif MOVE_SENT:
                try:
                    move_sent(src, sent_dir)
                except OSError as e:
                    print(f"   (не удалось перенести файл: {e})")
            await asyncio.sleep(PAUSE_BETWEEN)

        print("\n--- Итог ---")
        print(f"Запланировано: {sent}")
        if sent:
            print(f"Последний пост: {fmt(start + step * (sent - 1))}")
        left = len(files) - sent - len(skipped)
        if left > 0:
            print(f"Осталось неотправленных файлов: {left} (запустите скрипт снова, когда отложка освободится)")
        if skipped:
            print("Пропущены из-за ошибок: " + ", ".join(skipped))
        if stop_reason:
            print(stop_reason)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nОстановлено. Уже запланированные сообщения остаются в отложке.")
