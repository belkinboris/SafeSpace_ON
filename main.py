import os
import logging
import random
import datetime
import re

from flask import Flask
from threading import Thread

from telegram import (
    Update,
    BotCommand,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)


# ------------------------------------------------------------------------
# 1) ЧТЕНИЕ TOKEN ИЗ ОКРУЖЕНИЯ
# ------------------------------------------------------------------------
BOT_TOKEN = os.getenv("token_on")
if not BOT_TOKEN:
    raise ValueError("No token_on found in environment variables!")


# ------------------------------------------------------------------------
# 2) FLASK (мини-сервер) - KEEP ALIVE
# ------------------------------------------------------------------------
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "Я жив!"

def run_server():
    port = int(os.getenv("PORT", "8080"))  # Railway provides PORT
    flask_app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_server)
    t.start()


# ------------------------------------------------------------------------
# 3) ЛОГИРОВАНИЕ
# ------------------------------------------------------------------------
logging.basicConfig(
    filename='bot.log',
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)


# ------------------------------------------------------------------------
# 4) ГЛОБАЛЬНЫЕ СТРУКТУРЫ ДАННЫХ
# ------------------------------------------------------------------------
users_in_chat = {}       # { user_id: {...} }
users_history = {}       # { user_id: {...} }
parted_users = []        # [(nick, code, time), ...]
private_messages = {}    # { user_id: [ { from, text }, ... ] }
user_notify_settings = {}# { user_id: {...} }
polls = {}               # { creator_id: {...} }
admin_ids = set()
moderator_ids = set()


# ------------------------------------------------------------------------
# 5) ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ------------------------------------------------------------------------
def generate_nickname():
    """Случайный ник."""
    return f"👤{''.join(random.choices('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz', k=6))}"

def generate_personal_code():
    """Случайный код вида #XXXX."""
    return f"#{''.join(random.choices('ABCDEFGHIJKLMNOPQRSTUVWXYZ', k=4))}"

def ensure_user_in_dicts(user_id: int):
    """Добавляем запись для лички и уведомлений, если нет."""
    if user_id not in private_messages:
        private_messages[user_id] = []
    if user_id not in user_notify_settings:
        user_notify_settings[user_id] = {
            "privates": False,
            "replies": False,
            "hug": False,
            "interval": 5,
        }

def get_user_role(user_id: int) -> str:
    """Роль: admin | moderator | new | resident"""
    if user_id in admin_ids:
        return "admin"
    if user_id in moderator_ids:
        return "moderator"
    if user_id in users_history:
        c = users_history[user_id].get("join_count", 0)
        return "new" if c <= 1 else "resident"
    return "new"

def get_moon_symbol(seconds_diff: float) -> str:
    """
    Возвращаем «луну» по давности.
    < 60 -> 🌕
    < 300 -> 🌖
    < 900 -> 🌗
    < 1800 -> 🌘
    >= 1800 -> 🌑
    """
    if seconds_diff < 60:
        return "🌕"
    elif seconds_diff < 300:
        return "🌖"
    elif seconds_diff < 900:
        return "🌗"
    elif seconds_diff < 1800:
        return "🌘"
    else:
        return "🌑"

def get_user_by_code(code: str):
    """Найти user_id по коду."""
    for u_id, data in users_in_chat.items():
        if data["code"].lower() == code.lower():
            return u_id
    return None

def update_last_activity(user_id: int):
    """Обновить время последней активности."""
    if user_id in users_in_chat:
        users_in_chat[user_id]["last_activity"] = datetime.datetime.now()


# Широковещательная рассылка текста
async def broadcast_text(telegram_app, text: str, exclude_user: int = None):
    """Рассылка текста всем, кроме exclude_user."""
    for uid, info in users_in_chat.items():
        if uid == exclude_user:
            continue
        try:
            await telegram_app.bot.send_message(chat_id=info["chat_id"], text=text)
        except Exception as e:
            logging.warning(f"Ошибка отправки текста {info['nickname']}: {e}")


# Широковещательная рассылка фото
async def broadcast_photo(telegram_app, photo_file_id: str, caption: str = "", exclude_user: int = None):
    """Рассылка фото всем, кроме exclude_user."""
    for uid, info in users_in_chat.items():
        if uid == exclude_user:
            continue
        try:
            await telegram_app.bot.send_photo(
                chat_id=info["chat_id"],
                photo=photo_file_id,
                caption=caption
            )
        except Exception as e:
            logging.warning(f"Ошибка отправки фото {info['nickname']}: {e}")


def parse_replied_nickname(bot_message_text: str) -> str:
    """
    Если в тексте бота есть «NickName: ...», вернём NickName,
    иначе вернём пустую строку.
    """
    m = re.match(r"^(.+?):\s", bot_message_text)
    if not m:
        return ""
    return m.group(1).strip()


# ------------------------------------------------------------------------
# 6) ХЕНДЛЕРЫ КОМАНД: /start, /stop
# ------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    ensure_user_in_dicts(user_id)

    if user_id in users_in_chat:
        nickname = users_in_chat[user_id]["nickname"]
        await update.message.reply_text(
            f"[BOT] Ты уже в чате под ником «{nickname}». Для выхода — /stop."
        )
        update_last_activity(user_id)
        return

    # Если пользователь уже заходил ранее
    if user_id in users_history:
        nickname = users_history[user_id]["nickname"]
        code = users_history[user_id]["code"]
        users_history[user_id]["join_count"] = users_history[user_id].get("join_count", 0) + 1
        join_count = users_history[user_id]["join_count"]
    else:
        # Первый раз
        nickname = generate_nickname()
        code = generate_personal_code()
        users_history[user_id] = {
            "nickname": nickname,
            "code": code,
            "join_count": 1
        }
        join_count = 1

    # Вставляем в активный список
    users_in_chat[user_id] = {
        "nickname": nickname,
        "code": code,
        "chat_id": chat_id,
        "last_activity": datetime.datetime.now()
    }

    # Приветственное сообщение
    await update.message.reply_text(
        f"[BOT] Добро пожаловать в анонимный чат для людей, столкнувшихся с онкологическим заболеванием. Это пространство создано для взаимопомощи и поддержки. Поделись с чатом, а чат поделится с тобой! 😊 \n"
        "Чтобы выйти — /stop.\n\n"
        f"Твой ник: {nickname}\n"
        f"Твой код: {code}\n"
        "Приятного общения!"
    )

    # Сообщение в общий чат о входе
    if join_count == 1:
        msg_broadcast = f"[Bot] {code} {nickname} входит в чат. Он новенький!"
    else:
        msg_broadcast = f"[Bot] {code} {nickname} входит в чат."

    await broadcast_text(context.application, msg_broadcast, exclude_user=user_id)
    logging.info(f"Пользователь {user_id} => {nickname} (join_count={join_count}).")


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in users_in_chat:
        await update.message.reply_text("[BOT] Тебя нет в чате. Используй /start, чтобы войти.")
        return

    nickname = users_in_chat[user_id]["nickname"]
    code = users_in_chat[user_id]["code"]
    users_in_chat.pop(user_id, None)

    parted_users.insert(0, (nickname, code, datetime.datetime.now()))
    if len(parted_users) > 20:
        parted_users.pop()

    await update.message.reply_text("[BOT] Ты вышел из чата. Возвращайся в любой момент через /start.")
    await broadcast_text(context.application, f"[Bot] {code} {nickname} вышел из чата.", exclude_user=user_id)
    logging.info(f"Пользователь {user_id} («{nickname}») вышел из чата.")


# ------------------------------------------------------------------------
# 7) СМЕНА НИКА /nick (ConversationHandler)
# ------------------------------------------------------------------------
NICK_WAITING = range(1)

async def nick_command_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in users_in_chat:
        await update.message.reply_text("[BOT] Тебя нет в чате. /start, чтобы войти.")
        return ConversationHandler.END

    await update.message.reply_text("[BOT] Введи новый ник сообщением.")
    return NICK_WAITING

async def nick_new_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in users_in_chat:
        await update.message.reply_text("[BOT] Ты уже вышел или не заходил.")
        return ConversationHandler.END

    new_nick = update.message.text.strip()
    if len(new_nick) > 15:
        await update.message.reply_text("[BOT] Ник слишком длинный (макс 15 символов).")
        return ConversationHandler.END

    old_nick = users_in_chat[user_id]["nickname"]
    code = users_in_chat[user_id]["code"]

    users_in_chat[user_id]["nickname"] = new_nick
    users_history[user_id]["nickname"] = new_nick

    await update.message.reply_text(f"[BOT] Новый ник: {new_nick}.")
    await broadcast_text(context.application, f"[Bot] {code} {old_nick} сменил(а) ник на {new_nick}.")
    update_last_activity(user_id)
    logging.info(f"{user_id} сменил ник с {old_nick} на {new_nick}.")
    return ConversationHandler.END

async def nick_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("[BOT] Отменено.")
    return ConversationHandler.END


# ------------------------------------------------------------------------
# 8) /list, /last
# ------------------------------------------------------------------------
async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not users_in_chat:
        await update.message.reply_text("[BOT] В чате никого нет.")
        return

    total_possible = 100  # Шутливое число из исходного кода :)
    lines = []
    now = datetime.datetime.now()

    for uid, data in users_in_chat.items():
        diff_sec = (now - data["last_activity"]).total_seconds()
        moon = get_moon_symbol(diff_sec)
        role = get_user_role(uid)
        code = data["code"]
        nick = data["nickname"]
        line = f"{moon} {role} {code} {nick}"
        lines.append(line)

    msg = f"[BOT] В чате {len(users_in_chat)} (из {total_possible}):\n" + "\n".join(lines)
    await update.message.reply_text(msg)
    update_last_activity(update.effective_user.id)


# ------------------------------------------------------------------------
# 9) /help, /rules, /about, /ping
# ------------------------------------------------------------------------
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "[BOT] Доступные команды:\n\n"
        "/start - Войти в чат\n"
        "/stop - Выйти из чата\n"
        "/nick - Сменить ник\n"
        "/list - Список пользователей\n"
        "/msg - Отправить личное сообщение\n"
        "/getmsg - Получить личные сообщения\n"
        "/hug [CODE] - Обнять пользователя\n"
        "/search [ТЕКСТ] - Поиск пользователя по нику\n"
        "/poll - Создать опрос\n"
        "/polldone - Завершить опрос\n"
        "/notify - Настройки уведомлений\n"
        "/ping - Проверить бота\n"
        "/rules - Правила чата\n"
        "/about - О боте\n\n"
        "Для сообщений «от третьего лица» начинай строку со знака %. Приятного общения!"
    )
    await update.message.reply_text(txt)
    update_last_activity(update.effective_user.id)

async def rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "[BOT] Правила чата:\n\n"
        "😊Мы за адекватное и уютное общение среди участников нашего комьюнити и призываем Вас соблюдать порядки, устои и наши традиции.\n\n"
        "🔸Запрещены призывы в личку, выпрашивание личных данных участников и отправка своих.\n"
        "🔸Флирт, попытка найти девушек и зазывы их в личку.\n"
        "🔸Оскорбление участников чата и переход на личности.\n"
        "🔸Запрещен мат и обесценная лексика (Резиденты могут скрывать мат под спойлер).\n"
        "🔸Запрещен флуд и поток бессвязного бреда.\n"
        "🔸Контент шокирующего формата, порно и другая запрещенка.\n"
        "🔸Общение только на русском языке в кириллической раскладке.\n"
        "🔸Реклама, спам и ссылки на сторонние ресурсы запрещены.\n"
        "🔸Разжигание конфликтов, провокации, споры на тему политики и религии запрещены.\n\n"
        "❗️Модераторы могут по своему усмотрению применять меры наказания. Незнание правил не освобождает Вас от ответственности.\n\n"
        "Рады каждому из Вас. Добро пожаловать в Чат!"
    )
    await update.message.reply_text(txt)
    update_last_activity(update.effective_user.id)

async def about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("[BOT] Это тестовый анонимный чат-бот. Приятного использования!")
    update_last_activity(update.effective_user.id)

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Pong!")
    update_last_activity(update.effective_user.id)


# ------------------------------------------------------------------------
# 10) ЛИЧНЫЕ СООБЩЕНИЯ /msg
# ------------------------------------------------------------------------
MSG_SELECT_RECIPIENT, MSG_ENTER_TEXT = range(2)

async def msg_command_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in users_in_chat:
        await update.message.reply_text("[BOT] Тебя нет в чате. /start, чтобы войти.")
        return ConversationHandler.END

    # если введено /msg CODE текст
    if len(context.args) >= 2:
        code = context.args[0]
        text_msg = " ".join(context.args[1:])
        to_user = get_user_by_code(code)
        if to_user is None:
            await update.message.reply_text("[BOT] Не нашли пользователя с таким кодом.")
            return ConversationHandler.END

        from_nick = users_in_chat[user_id]["nickname"]
        ensure_user_in_dicts(to_user)
        # Сохраняем копию
        private_messages[to_user].append({"from": from_nick, "text": text_msg})

        # Отправляем получателю сразу
        chat_to = users_in_chat[to_user]["chat_id"]
        await context.application.bot.send_message(
            chat_id=chat_to,
            text=f"[ЛС от {from_nick}]: {text_msg}"
        )

        await update.message.reply_text(f"[BOT] Личное сообщение отправлено для {code}.")
        update_last_activity(user_id)
        return ConversationHandler.END

    # иначе — показать inline-список (кнопочки) всех
    keyboard = []
    row = []
    i = 0
    for uid, data in users_in_chat.items():
        if uid == user_id:
            continue
        i += 1
        btn_text = f"{data['code']} {data['nickname']}"
        row.append(InlineKeyboardButton(btn_text, callback_data=f"msg_select|{uid}"))
        if i % 3 == 0:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="msg_cancel")])
    await update.message.reply_text(
        "[BOT] Выбери пользователя, чтобы отправить ЛС:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    update_last_activity(user_id)
    return MSG_SELECT_RECIPIENT

async def msg_callback_select_recipient(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    parts = query.data.split("|")
    if len(parts) != 2:
        await query.answer("Ошибка.")
        return ConversationHandler.END

    recipient_id = int(parts[1])
    context.user_data["msg_recipient"] = recipient_id

    code_to = users_in_chat[recipient_id]["code"]
    nick_to = users_in_chat[recipient_id]["nickname"]

    await query.message.edit_text(
        f"[BOT] Отправь сообщение, и оно будет доставлено пользователю {code_to} {nick_to}."
    )
    await query.answer()
    return MSG_ENTER_TEXT

async def msg_enter_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if "msg_recipient" not in context.user_data:
        await update.message.reply_text("[BOT] Ошибка: нет получателя.")
        return ConversationHandler.END

    recipient_id = context.user_data["msg_recipient"]
    if recipient_id not in users_in_chat:
        await update.message.reply_text("[BOT] Похоже, пользователь вышел.")
        return ConversationHandler.END

    from_nick = users_in_chat[user_id]["nickname"]
    text_msg = update.message.text

    to_code = users_in_chat[recipient_id]["code"]
    to_nick = users_in_chat[recipient_id]["nickname"]

    ensure_user_in_dicts(recipient_id)
    # Сохраняем копию
    private_messages[recipient_id].append({"from": from_nick, "text": text_msg})

    # Отправляем получателю
    chat_to = users_in_chat[recipient_id]["chat_id"]
    await context.application.bot.send_message(
        chat_id=chat_to,
        text=f"[ЛС от {from_nick}]: {text_msg}"
    )

    await update.message.reply_text(
        f"[BOT] Сообщение для {to_code} {to_nick} отправлено."
    )
    logging.info(f"ЛС: {from_nick} -> {to_nick}: {text_msg}")

    context.user_data.pop("msg_recipient", None)
    update_last_activity(user_id)
    return ConversationHandler.END

async def msg_callback_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.message.edit_text("Отправка ЛС отменена.")
    await query.answer()
    return ConversationHandler.END

async def getmsg_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in users_in_chat:
        await update.message.reply_text("[BOT] Тебя нет в чате.")
        return

    ensure_user_in_dicts(user_id)
    msgs = private_messages[user_id]
    if not msgs:
        await update.message.reply_text("[BOT] У тебя нет личных сообщений.")
        return

    lines = []
    for m in msgs:
        lines.append(f"От {m['from']}: {m['text']}")
    text = "[BOT] Твои личные сообщения (копия):\n\n" + "\n".join(lines)
    await update.message.reply_text(text)
    update_last_activity(user_id)


# ------------------------------------------------------------------------
# 11) /hug
# ------------------------------------------------------------------------
HUG_SELECT = range(1)

async def hug_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in users_in_chat:
        await update.message.reply_text("[BOT] Тебя нет в чате.")
        return ConversationHandler.END

    # Если /hug CODE
    if context.args:
        code = context.args[0]
        to_user = get_user_by_code(code)
        if not to_user:
            await update.message.reply_text("[BOT] Не нашли пользователя с таким кодом.")
            return ConversationHandler.END

        from_nick = users_in_chat[user_id]["nickname"]
        from_code = users_in_chat[user_id]["code"]
        to_nick = users_in_chat[to_user]["nickname"]
        text = f"[Bot] {from_code} {from_nick} обнял(а) {to_nick}!"
        await broadcast_text(context.application, text)
        update_last_activity(user_id)
        return ConversationHandler.END

    # Иначе inline-список
    keyboard = []
    row = []
    i = 0
    for uid, data in users_in_chat.items():
        if uid == user_id:
            continue
        i += 1
        btn_text = f"{data['code']} {data['nickname']}"
        row.append(InlineKeyboardButton(btn_text, callback_data=f"hug_select|{uid}"))
        if i % 3 == 0:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="hug_cancel")])
    await update.message.reply_text(
        "[BOT] Выбери, кого обнять:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    update_last_activity(user_id)
    return HUG_SELECT

async def hug_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    parts = query.data.split("|")
    if len(parts) != 2:
        await query.answer("Ошибка.")
        return ConversationHandler.END

    to_user_id = int(parts[1])
    from_nick = users_in_chat[user_id]["nickname"]
    from_code = users_in_chat[user_id]["code"]
    to_nick = users_in_chat[to_user_id]["nickname"]

    text = f"[Bot] {from_code} {from_nick} обнял(а) {to_nick}!"
    await broadcast_text(context.application, text)
    await query.message.edit_text("Обнимашка отправлена!")
    await query.answer()
    update_last_activity(user_id)
    return ConversationHandler.END

async def hug_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.message.edit_text("Обнимашки отменены.")
    await query.answer()
    return ConversationHandler.END


# ------------------------------------------------------------------------
# 12) /search
# ------------------------------------------------------------------------
async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in users_in_chat:
        await update.message.reply_text("[BOT] Тебя нет в чате.")
        return

    if not context.args:
        await update.message.reply_text("[BOT] /search <текст> — поиск в нике.")
        return

    pattern = " ".join(context.args).lower()
    results = []
    for uid, info in users_in_chat.items():
        if pattern in info["nickname"].lower():
            results.append(f"{info['code']} {info['nickname']}")

    if results:
        await update.message.reply_text("[BOT] Найдены:\n" + "\n".join(results))
    else:
        await update.message.reply_text("[BOT] Никого не нашли.")
    update_last_activity(user_id)


# ------------------------------------------------------------------------
# 13) /poll
# ------------------------------------------------------------------------
POLL_AWAITING_QUESTION = range(1)

async def poll_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in users_in_chat:
        await update.message.reply_text("[BOT] Тебя нет в чате.")
        return ConversationHandler.END

    await update.message.reply_text(
        "[BOT] Начинаем опрос.\n\n"
        "Введи вопрос и варианты ответа, каждый на новой строке, например:\n\n"
        "Что делать?\n"
        "Вариант1\n"
        "Вариант2\n"
        "/cancel чтобы отменить."
    )
    update_last_activity(user_id)
    return POLL_AWAITING_QUESTION

async def poll_received_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in users_in_chat:
        return ConversationHandler.END

    text = update.message.text.strip()
    lines = text.split("\n")
    if len(lines) < 2:
        await update.message.reply_text("[BOT] Нужно минимум 1 вопрос и 1 вариант ответа.")
        return ConversationHandler.END

    question = lines[0]
    options = lines[1:]

    polls[user_id] = {
        "question": question,
        "options": options,
        "votes": {opt: set() for opt in options},
        "active": True,
        "message_ids": {},
        "chat_ids": {}
    }

    from_nick = users_in_chat[user_id]["nickname"]
    from_code = users_in_chat[user_id]["code"]
    header_text = f"[Bot] {from_code} {from_nick} поставил(а) вопрос:\n{question}"

    def build_poll_keyboard(creator_id):
        kb = []
        for i, opt in enumerate(options, start=1):
            callback_data = f"pollvote|{creator_id}|{i}"
            btn_text = f"{i} - {opt}"
            kb.append([InlineKeyboardButton(btn_text, callback_data=callback_data)])
        return InlineKeyboardMarkup(kb)

    markup = build_poll_keyboard(user_id)
    for uid, info in users_in_chat.items():
        try:
            msg = await context.application.bot.send_message(
                chat_id=info["chat_id"],
                text=header_text,
                reply_markup=markup
            )
            polls[user_id]["message_ids"][uid] = msg.message_id
            polls[user_id]["chat_ids"][uid] = info["chat_id"]
        except Exception as e:
            logging.warning(f"Не смог отправить опрос {info['nickname']}: {e}")

    update_last_activity(user_id)
    return ConversationHandler.END

async def poll_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("[BOT] Опрос отменён.")
    return ConversationHandler.END

async def poll_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in polls or not polls[user_id]["active"]:
        await update.message.reply_text("[BOT] У тебя нет активных опросов.")
        return

    polls[user_id]["active"] = False
    await update.message.reply_text("[BOT] Твой опрос завершён.")

    # Уберём кнопки у всех
    for uid, msg_id in polls[user_id]["message_ids"].items():
        chat_id = polls[user_id]["chat_ids"][uid]
        try:
            await context.application.bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=msg_id,
                reply_markup=None
            )
        except:
            pass

    update_last_activity(user_id)

async def poll_vote_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    parts = query.data.split("|")
    if len(parts) != 3 or parts[0] != "pollvote":
        await query.answer("Ошибка.")
        return

    creator_id = int(parts[1])
    opt_index = int(parts[2]) - 1
    user_id = update.effective_user.id

    if creator_id not in polls:
        await query.answer("Опрос не найден или не активен.")
        return
    poll_data = polls[creator_id]
    if not poll_data["active"]:
        await query.answer("Опрос завершён.")
        return

    options = poll_data["options"]
    if opt_index < 0 or opt_index >= len(options):
        await query.answer("Неправильный вариант.")
        return

    chosen_opt = options[opt_index]
    # Снимаем предыдущие голоса
    for opt in options:
        if user_id in poll_data["votes"][opt]:
            poll_data["votes"][opt].remove(user_id)
    poll_data["votes"][chosen_opt].add(user_id)
    await query.answer("Голос учтён!")

    # Пересобираем текст (результаты)
    question = poll_data["question"]
    out_lines = [question]
    for i, opt in enumerate(options, start=1):
        c = len(poll_data["votes"][opt])
        mark = "✔️" if c > 0 else f"{i}"
        out_lines.append(f"{mark} - {opt} ({c})")

    new_text = "\n".join(out_lines)
    # Обновим сообщение у всех
    for uid, msg_id in poll_data["message_ids"].items():
        chat_id = poll_data["chat_ids"][uid]
        try:
            await context.application.bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=new_text,
                reply_markup=query.message.reply_markup
            )
        except Exception as e:
            logging.warning(f"Не смог обновить опрос для {uid}: {e}")

    update_last_activity(user_id)


# ------------------------------------------------------------------------
# 14) /notify (демо)
# ------------------------------------------------------------------------
def build_notify_keyboard(user_id: int):
    s = user_notify_settings[user_id]
    def on_off(flag: bool):
        return "✅" if flag else "❌"

    kb = [
      [
        InlineKeyboardButton(f"{on_off(s['privates'])} ЛС", callback_data="notify|privates"),
        InlineKeyboardButton(f"{on_off(s['replies'])} Ответы", callback_data="notify|replies"),
        InlineKeyboardButton(f"{on_off(s['hug'])} Обнимашки", callback_data="notify|hug"),
      ],
    ]
    row = []
    for val in [0, 1, 5, 10, 20, 30]:
        mark = "✅" if s['interval'] == val else "❌"
        row.append(InlineKeyboardButton(f"{mark} {val}", callback_data=f"notify|interval|{val}"))
    kb.append(row)
    kb.append([InlineKeyboardButton("❌ Отмена", callback_data="notify|cancel")])
    return InlineKeyboardMarkup(kb)

async def notify_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in users_in_chat:
        await update.message.reply_text("[BOT] Тебя нет в чате.")
        return

    ensure_user_in_dicts(user_id)
    kb = build_notify_keyboard(user_id)
    await update.message.reply_text("[BOT] Настройки уведомлений:", reply_markup=kb)
    update_last_activity(user_id)

async def notify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    parts = query.data.split("|")
    if parts[0] != "notify":
        return

    if len(parts) == 2:
        if parts[1] == "cancel":
            await query.message.delete()
            return
        k = parts[1]
        user_notify_settings[user_id][k] = not user_notify_settings[user_id][k]
    elif len(parts) == 3 and parts[1] == "interval":
        val = int(parts[2])
        user_notify_settings[user_id]["interval"] = val
    else:
        await query.answer("Неизвестный параметр.")
        return

    new_kb = build_notify_keyboard(user_id)
    try:
        await query.message.edit_reply_markup(new_kb)
    except:
        pass
    await query.answer("Настройка сохранена.")
    update_last_activity(user_id)


# ------------------------------------------------------------------------
# 15) ОБРАБОТКА СООБЩЕНИЙ (текст + фото)
# ------------------------------------------------------------------------
async def anonymous_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in users_in_chat:
        await update.message.reply_text("[BOT] Тебя нет в чате. /start, чтобы войти.")
        return

    nickname = users_in_chat[user_id]["nickname"]
    code = users_in_chat[user_id]["code"]

    # Если фото
    if update.message.photo:
        photo = update.message.photo[-1]
        file_id = photo.file_id
        caption = update.message.caption if update.message.caption else ""
        full_caption = f"{code} {nickname} прислал(а) фото"
        if caption:
            full_caption += f"\n{caption}"

        await broadcast_photo(context.application, file_id, caption=full_caption, exclude_user=user_id)
        update_last_activity(user_id)
        return

    # Иначе текст
    text = update.message.text.strip()
    replied_nick = ""
    if update.message.reply_to_message and update.message.reply_to_message.from_user.id == context.application.bot.id:
        replied_nick = parse_replied_nickname(update.message.reply_to_message.text)

    if text.startswith("%"):
        # Третье лицо
        out_text = text[1:].lstrip()
        if replied_nick:
            final_text = f"{nickname} (reply to {replied_nick}) {out_text}"
        else:
            final_text = f"{nickname} {out_text}"
        await broadcast_text(context.application, final_text, exclude_user=user_id)
    else:
        # Обычное сообщение
        if replied_nick:
            final_text = f"{nickname} (reply to {replied_nick}): {text}"
        else:
            final_text = f"{nickname}: {text}"
        await broadcast_text(context.application, final_text, exclude_user=user_id)

    update_last_activity(user_id)


# ------------------------------------------------------------------------
# 16) УСТАНОВКА КОМАНД ДЛЯ МЕНЮ, post_init
# ------------------------------------------------------------------------
async def set_bot_commands(telegram_app):
    commands = [
        BotCommand("start", "Войти в чат"),
        BotCommand("stop", "Выйти из чата"),
        BotCommand("nick", "Сменить ник"),
        BotCommand("list", "Список пользователей"),
        BotCommand("msg", "Отправить ЛС"),
        BotCommand("getmsg", "Получить ЛС"),
        BotCommand("hug", "Обнять"),
        BotCommand("search", "Поиск по нику"),
        BotCommand("poll", "Создать опрос"),
        BotCommand("polldone", "Завершить опрос"),
        BotCommand("notify", "Уведомления"),
        BotCommand("ping", "Проверка бота"),
        BotCommand("rules", "Правила чата"),
        BotCommand("about", "О боте"),
        BotCommand("help", "Помощь"),
    ]
    await telegram_app.bot.set_my_commands(commands)

async def post_init(telegram_app):
    await set_bot_commands(telegram_app)


# ------------------------------------------------------------------------
# 17) ГЛАВНАЯ ФУНКЦИЯ
# ------------------------------------------------------------------------
def main():
    # Запускаем Flask (keep-alive) в фоновом потоке
    keep_alive()

    # Создаём Telegram-приложение
    bot_app = ApplicationBuilder().token(BOT_TOKEN).build()
    logging.info("Бот запускается...")

    # 1) Conversation /nick
    nick_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("nick", nick_command_start)],
        states={
            NICK_WAITING: [MessageHandler(filters.TEXT & ~filters.COMMAND, nick_new_name)],
        },
        fallbacks=[CommandHandler("cancel", nick_cancel)]
    )

    # 2) Conversation /poll
    poll_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("poll", poll_command)],
        states={
            POLL_AWAITING_QUESTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, poll_received_text)
            ],
        },
        fallbacks=[CommandHandler("cancel", poll_cancel)]
    )

    # 3) Conversation /msg
    msg_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("msg", msg_command_start)],
        states={
            MSG_SELECT_RECIPIENT: [
                CallbackQueryHandler(msg_callback_select_recipient, pattern="^msg_select\\|"),
                CallbackQueryHandler(msg_callback_cancel, pattern="^msg_cancel$")
            ],
            MSG_ENTER_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, msg_enter_text),
            ],
        },
        fallbacks=[CallbackQueryHandler(msg_callback_cancel, pattern="^msg_cancel$")]
    )

    # 4) Conversation /hug
    hug_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("hug", hug_command)],
        states={
            HUG_SELECT: [
                CallbackQueryHandler(hug_select_callback, pattern="^hug_select\\|"),
                CallbackQueryHandler(hug_cancel_callback, pattern="^hug_cancel$")
            ],
        },
        fallbacks=[CallbackQueryHandler(hug_cancel_callback, pattern="^hug_cancel$")]
    )

    # Регистрируем хендлеры
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("stop", stop))

    bot_app.add_handler(nick_conv_handler)
    bot_app.add_handler(CommandHandler("list", list_users))
    bot_app.add_handler(CommandHandler("help", help_command))
    bot_app.add_handler(CommandHandler("rules", rules))
    bot_app.add_handler(CommandHandler("about", about))
    bot_app.add_handler(CommandHandler("ping", ping))

    bot_app.add_handler(msg_conv_handler)
    bot_app.add_handler(CommandHandler("getmsg", getmsg_command))

    bot_app.add_handler(hug_conv_handler)
    bot_app.add_handler(CommandHandler("search", search_command))

    bot_app.add_handler(poll_conv_handler)
    bot_app.add_handler(CommandHandler("polldone", poll_done))

    bot_app.add_handler(CommandHandler("notify", notify_command))
    bot_app.add_handler(CallbackQueryHandler(notify_callback, pattern="^notify\\|"))

    bot_app.add_handler(CallbackQueryHandler(poll_vote_callback, pattern="^pollvote\\|"))

    # Обработка сообщений (текст/фото)
    bot_app.add_handler(MessageHandler(~filters.COMMAND & (filters.TEXT | filters.PHOTO), anonymous_message))

    # post_init для установки /команд
    bot_app.post_init = post_init

    # Запуск
    bot_app.run_polling()


# ------------------------------------------------------------------------
# Запуск
# ------------------------------------------------------------------------
if __name__ == "__main__":
    main()
