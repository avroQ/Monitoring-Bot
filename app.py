import os
import re
import logging
import tempfile
import html
import telebot
from telebot import types
import paramiko
import psycopg2
from dotenv import load_dotenv

# Настраиваем логирование: пишем в bot.log и дублируем в консоль
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Загружаем переменные окружения
load_dotenv()
TOKEN = os.getenv("TOKEN")
RM_HOST = os.getenv("RM_HOST")
RM_PORT = os.getenv("RM_PORT", "22")
RM_USER = os.getenv("RM_USER")
RM_PASSWORD = os.getenv("RM_PASSWORD")

# Приводим порт к int
try:
    RM_PORT = int(RM_PORT)
except ValueError:
    RM_PORT = 22

# Проверяем токен перед стартом
if not TOKEN or TOKEN == "token":
    logger.critical("Бот не может запуститься: TOKEN в файле .env не задан!")
    raise ValueError("Пожалуйста, заполните TOKEN в файле .env")

# Инициализируем бота
bot = telebot.TeleBot(TOKEN)


# Работа с базой данных PostgreSQL

def get_db_connection():
    """Создает и возвращает подключение к базе данных PostgreSQL"""
    db_user = os.getenv("DB_USER")
    db_password = os.getenv("DB_PASSWORD")
    db_host = os.getenv("DB_HOST")
    db_port = os.getenv("DB_PORT", "5432")
    db_name = os.getenv("DB_DATABASE")
    
    # Проверяем, настроены ли переменные
    if not all([db_user, db_password, db_host, db_name]) or db_host == "db_host":
        return None
        
    try:
        conn = psycopg2.connect(
            host=db_host,
            port=db_port,
            database=db_name,
            user=db_user,
            password=db_password
        )
        return conn
    except Exception as e:
        logger.error(f"Не удалось подключиться к базе данных PostgreSQL: {e}")
        return None


def log_action_to_db(user_id, command, success, detail=""):
    """Записывает действие пользователя в базу данных PostgreSQL (создает таблицу при первом обращении)"""
    conn = get_db_connection()
    if not conn:
        logger.warning("Запись лога в БД пропущена (БД не настроена или недоступна)")
        return
        
    try:
        with conn.cursor() as cur:
            # Создаем таблицу, если ее нет
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_logs (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    command VARCHAR(100) NOT NULL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    success BOOLEAN NOT NULL,
                    detail TEXT
                );
            """)
            # Записываем лог
            cur.execute("""
                INSERT INTO bot_logs (user_id, command, success, detail)
                VALUES (%s, %s, %s, %s);
            """, (user_id, command[:100], success, detail))
            conn.commit()
            logger.info(f"Лог успешно записан в PostgreSQL для пользователя {user_id}")
    except Exception as e:
        logger.error(f"Ошибка при записи лога в БД: {e}")
    finally:
        conn.close()


def get_replica_db_connection():
    """Создает и возвращает подключение к базе данных PostgreSQL Slave (Replica)"""
    db_user = os.getenv("DB_REPL_USER")
    db_password = os.getenv("DB_REPL_PASSWORD")
    db_host = os.getenv("DB_REPL_HOST")
    db_port = os.getenv("DB_REPL_PORT", "5432")
    db_name = os.getenv("DB_DATABASE")
    
    # Проверяем, настроены ли переменные
    if not all([db_user, db_password, db_host, db_name]) or db_host == "db_repl_host":
        # Если реплика не настроена, используем master
        return get_db_connection()
        
    try:
        conn = psycopg2.connect(
            host=db_host,
            port=db_port,
            database=db_name,
            user=db_user,
            password=db_password
        )
        return conn
    except Exception as e:
        logger.error(f"Не удалось подключиться к Slave (Replica) базе данных PostgreSQL: {e}")
        # Резервное переключение на master
        return get_db_connection()


# Временное хранилище найденных контактов для подтверждения записи в БД
user_temp_data = {}  # {chat_id: {'type': 'email'/'phone', 'items': [...]}}





# Вспомогательные функции для регулярок

def extract_emails(text):
    """Ищет все email-адреса в тексте"""
    email_regex = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
    return re.findall(email_regex, text)


def extract_phone_numbers(text):
    """Ищет телефонные номера в различных форматах (8/7, скобки, пробелы, дефисы)"""
    phone_regex = r'(?:\+7|8)[\s-]?\(?\d{3}\)?[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}'
    return re.findall(phone_regex, text)


def is_password_strong(password):
    """Проверяет сложность пароля: длина от 8, строчная, заглавная, цифра, спецсимвол"""
    password_regex = r'^(?=.*[A-Z])(?=.*[a-z])(?=.*[0-9])(?=.*[!@#$%^&*()]).{8,}$'
    return bool(re.match(password_regex, password))


# Вспомогательная функция для SSH подключения

def run_ssh_command(command):
    """Подключается по SSH к серверу и выполняет команду"""
    logger.info(f"Выполняю SSH команду: {command}")
    
    if not all([RM_HOST, RM_USER, RM_PASSWORD]) or RM_HOST == "rm_host":
        error_msg = "Ошибка: параметры SSH (RM_HOST, RM_USER, RM_PASSWORD) не настроены в .env!"
        logger.error(error_msg)
        return error_msg
        
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        client.connect(
            hostname=RM_HOST,
            port=RM_PORT,
            username=RM_USER,
            password=RM_PASSWORD,
            timeout=10
        )
        stdin, stdout, stderr = client.exec_command(command)
        
        # Считываем вывод и ошибки
        out = stdout.read().decode('utf-8', errors='replace')
        err = stderr.read().decode('utf-8', errors='replace')
        
        # Если есть ошибки и нет обычного вывода
        if err and not out:
            logger.warning(f"Команда завершилась с ошибкой: {err}")
            return f"Ошибка выполнения:\n{err}"
            
        return out if out else "Команда выполнена, вывод пуст."
        
    except Exception as e:
        logger.exception("Исключение при выполнении SSH команды")
        return f"Не удалось подключиться к серверу по SSH:\n{str(e)}"
    finally:
        client.close()


def send_ssh_result(message, title, cmd):
    """Шаблон отправки результатов выполнения SSH-команды в чат"""
    bot.send_chat_action(message.chat.id, 'typing')
    result = run_ssh_command(cmd)
    
    # Ограничение Telegram в 4096 символов
    if len(result) > 4000:
        result = result[:4000] + "\n...[Вывод обрезан из-за ограничений Telegram]..."
        
    # Пишем лог выполнения в БД
    success = not result.startswith("Не удалось подключиться") and not result.startswith("Ошибка выполнения")
    log_action_to_db(message.chat.id, cmd, success, result[:200])
    
    # Форматируем в HTML с экранированием, чтобы избежать ошибок парсинга
    escaped_result = html.escape(result)
    response = f"<b>{title}</b>:\n<pre>{escaped_result}</pre>"
    bot.reply_to(message, response, parse_mode='HTML')


# Обработчики команд общего назначения

def get_main_menu():
    """Создает клавиатуру с кнопкой вывода списка команд"""
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("Вывести список команд"))
    return markup


@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    logger.info(f"Пользователь {message.chat.id} запросил помощь")
    log_action_to_db(message.chat.id, '/start', True)
    
    text = (
        "Привет! Я бот для мониторинга серверов и поиска информации.\n\n"
        "<b>Команды поиска:</b>\n"
        "<code>/find_email</code> - Найти email в тексте\n"
        "<code>/find_phone_number</code> - Найти номера телефонов в тексте\n"
        "<code>/verify_password</code> - Проверить сложность пароля\n\n"
        "<b>Команды базы данных и репликации:</b>\n"
        "<code>/get_emails</code> - Список email-адресов (Slave БД)\n"
        "<code>/get_phone_numbers</code> - Список телефонов (Slave БД)\n"
        "<code>/get_repl_logs</code> - Логи репликации PostgreSQL (Master)\n\n"
        "<b>Команды мониторинга Linux:</b>\n"
        "<code>/get_release</code> - Информация о релизе ОС\n"
        "<code>/get_uname</code> - Архитектура, хост и версия ядра\n"
        "<code>/get_uptime</code> - Время работы системы\n"
        "<code>/get_df</code> - Состояние файловой системы\n"
        "<code>/get_free</code> - Оперативная память\n"
        "<code>/get_mpstat</code> - Производительность процессора\n"
        "<code>/get_w</code> - Активные пользователи\n"
        "<code>/get_auths</code> - Последние 10 входов в систему\n"
        "<code>/get_critical</code> - Последние 5 критических событий\n"
        "<code>/get_ps</code> - Запущенные процессы\n"
        "<code>/get_ss</code> - Используемые порты\n"
        "<code>/get_apt_list</code> - Установленные пакеты (поиск или список)\n"
        "<code>/get_services</code> - Запущенные сервисы"
    )
    bot.reply_to(message, text, parse_mode='HTML', reply_markup=get_main_menu())


@bot.message_handler(func=lambda message: message.text == "Вывести список команд")
def handle_show_commands(message):
    send_welcome(message)


# Сценарии регулярных выражений

# 1. Поиск Email
@bot.message_handler(commands=['find_email'])
def cmd_find_email(message):
    logger.info(f"Пользователь {message.chat.id} запустил поиск email")
    msg = bot.reply_to(message, "Отправь мне текст, в котором нужно найти email-адреса:")
    bot.register_next_step_handler(msg, process_email_search)

def process_email_search(message):
    text = message.text
    if not text:
        bot.reply_to(message, "Пожалуйста, отправь текстовое сообщение.")
        return
        
    # Ищем email в присланном тексте с помощью регулярного выражения
    emails = extract_emails(text)
    
    if emails:
        unique_emails = sorted(list(set(emails)))
        user_temp_data[message.chat.id] = {
            'type': 'email',
            'items': unique_emails
        }
        
        response = "<b>Найденные email-адреса:</b>\n" + "\n".join(unique_emails)
        bot.reply_to(message, response, parse_mode='HTML')
        
        markup = types.InlineKeyboardMarkup()
        btn_yes = types.InlineKeyboardButton("Да, записать в БД", callback_data="save_contacts")
        btn_no = types.InlineKeyboardButton("Нет, отмена", callback_data="cancel_save")
        markup.row(btn_yes, btn_no)
        
        bot.send_message(
            message.chat.id,
            "Хотите записать найденную информацию в базу данных?",
            reply_markup=markup
        )
        log_action_to_db(message.chat.id, '/find_email', True, f"Найдено: {len(unique_emails)}, предложено сохранить")
    else:
        response = "Ни одного email-адреса не найдено."
        log_action_to_db(message.chat.id, '/find_email', False, "Ничего не найдено")
        bot.reply_to(message, response)


# 2. Поиск телефонов
@bot.message_handler(commands=['find_phone_number'])
def cmd_find_phone_number(message):
    logger.info(f"Пользователь {message.chat.id} запустил поиск номеров телефонов")
    msg = bot.reply_to(message, "Отправь мне текст, в котором нужно найти номера телефонов:")
    bot.register_next_step_handler(msg, process_phone_search)

def process_phone_search(message):
    text = message.text
    if not text:
        bot.reply_to(message, "Пожалуйста, отправь текстовое сообщение.")
        return
        
    # Ищем номера в присланном тексте с помощью регулярного выражения
    phones = extract_phone_numbers(text)
    
    if phones:
        unique_phones = sorted(list(set(phones)))
        user_temp_data[message.chat.id] = {
            'type': 'phone',
            'items': unique_phones
        }
        
        response = "<b>Найденные номера телефонов:</b>\n" + "\n".join(unique_phones)
        bot.reply_to(message, response, parse_mode='HTML')
        
        markup = types.InlineKeyboardMarkup()
        btn_yes = types.InlineKeyboardButton("Да, записать в БД", callback_data="save_contacts")
        btn_no = types.InlineKeyboardButton("Нет, отмена", callback_data="cancel_save")
        markup.row(btn_yes, btn_no)
        
        bot.send_message(
            message.chat.id,
            "Хотите записать найденную информацию в базу данных?",
            reply_markup=markup
        )
        log_action_to_db(message.chat.id, '/find_phone_number', True, f"Найдено: {len(unique_phones)}, предложено сохранить")
    else:
        response = "Ни одного номера телефона не найдено."
        log_action_to_db(message.chat.id, '/find_phone_number', False, "Ничего не найдено")
        bot.reply_to(message, response)


# Обработчики подтверждения сохранения контактов в БД
@bot.callback_query_handler(func=lambda call: call.data in ["save_contacts", "cancel_save"])
def handle_save_callback(call):
    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id)
    
    temp_data = user_temp_data.get(chat_id)
    if not temp_data:
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        bot.send_message(chat_id, "Данные устарели или не найдены. Пожалуйста, выполните поиск заново.")
        return
        
    # Убираем кнопки
    bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
    
    if call.data == "cancel_save":
        bot.send_message(chat_id, "Запись в базу данных отменена.")
        user_temp_data.pop(chat_id, None)
        log_action_to_db(chat_id, 'cancel_save_contacts', True, "User cancelled saving")
        return
        
    data_type = temp_data['type']
    items = temp_data['items']
    
    conn = get_db_connection()  # Подключение к Master для записи
    if not conn:
        bot.send_message(chat_id, "Ошибка при подключении к базе данных. Пожалуйста, проверьте настройки подключения.")
        log_action_to_db(chat_id, 'save_contacts_db_error', False, "DB connection failed during save")
        user_temp_data.pop(chat_id, None)
        return
        
    try:
        inserted_count = 0
        with conn.cursor() as cur:
            if data_type == 'email':
                for email in items:
                    cur.execute("""
                        INSERT INTO emails (email) VALUES (%s)
                        ON CONFLICT (email) DO NOTHING;
                    """, (email,))
                    inserted_count += cur.rowcount
            elif data_type == 'phone':
                for phone in items:
                    cur.execute("""
                        INSERT INTO phones (phone_number) VALUES (%s)
                        ON CONFLICT (phone_number) DO NOTHING;
                    """, (phone,))
                    inserted_count += cur.rowcount
            conn.commit()
            
        bot.send_message(chat_id, f"Успешно записано в базу данных! Добавлено новых записей: {inserted_count} из {len(items)}.")
        log_action_to_db(chat_id, f'save_{data_type}s_success', True, f"Saved: {inserted_count}/{len(items)}")
    except Exception as e:
        logger.error(f"Ошибка при записи контактов в БД: {e}")
        bot.send_message(chat_id, f"Произошла ошибка при записи в базу данных: {e}")
        log_action_to_db(chat_id, f'save_{data_type}s_error', False, str(e))
    finally:
        conn.close()
        user_temp_data.pop(chat_id, None)


# Вывод списков сохраненных контактов
@bot.message_handler(commands=['get_emails'])
def cmd_get_emails(message):
    logger.info(f"Пользователь {message.chat.id} запросил список email")
    bot.send_chat_action(message.chat.id, 'typing')
    
    conn = get_replica_db_connection()  # Чтение из Slave
    if not conn:
        bot.reply_to(message, "Ошибка: база данных реплики недоступна.")
        log_action_to_db(message.chat.id, '/get_emails', False, "DB connection error")
        return
        
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, email FROM emails ORDER BY id;")
            rows = cur.fetchall()
            
        if rows:
            lines = [f"<b>{row[0]}.</b> <code>{html.escape(row[1])}</code>" for row in rows]
            response = "<b>Список email-адресов из базы (Slave):</b>\n\n" + "\n".join(lines)
            log_action_to_db(message.chat.id, '/get_emails', True, f"Получено записей: {len(rows)}")
        else:
            response = "База данных email-адресов пуста."
            log_action_to_db(message.chat.id, '/get_emails', True, "База пуста")
            
        bot.reply_to(message, response, parse_mode='HTML')
    except Exception as e:
        logger.error(f"Ошибка при чтении email из БД: {e}")
        bot.reply_to(message, f"Ошибка при чтении данных: {e}")
        log_action_to_db(message.chat.id, '/get_emails', False, str(e))
    finally:
        conn.close()


@bot.message_handler(commands=['get_phone_numbers'])
def cmd_get_phone_numbers(message):
    logger.info(f"Пользователь {message.chat.id} запросил список телефонов")
    bot.send_chat_action(message.chat.id, 'typing')
    
    conn = get_replica_db_connection()  # Чтение из Slave
    if not conn:
        bot.reply_to(message, "Ошибка: база данных реплики недоступна.")
        log_action_to_db(message.chat.id, '/get_phone_numbers', False, "DB connection error")
        return
        
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, phone_number FROM phones ORDER BY id;")
            rows = cur.fetchall()
            
        if rows:
            lines = [f"<b>{row[0]}.</b> <code>{html.escape(row[1])}</code>" for row in rows]
            response = "<b>Список номеров телефонов из базы (Slave):</b>\n\n" + "\n".join(lines)
            log_action_to_db(message.chat.id, '/get_phone_numbers', True, f"Получено записей: {len(rows)}")
        else:
            response = "База данных номеров телефонов пуста."
            log_action_to_db(message.chat.id, '/get_phone_numbers', True, "База пуста")
            
        bot.reply_to(message, response, parse_mode='HTML')
    except Exception as e:
        logger.error(f"Ошибка при чтении телефонов из БД: {e}")
        bot.reply_to(message, f"Ошибка при чтении данных: {e}")
        log_action_to_db(message.chat.id, '/get_phone_numbers', False, str(e))
    finally:
        conn.close()


# Получение логов репликации
@bot.message_handler(commands=['get_repl_logs'])
def cmd_get_repl_logs(message):
    logger.info(f"Пользователь {message.chat.id} запросил логи репликации")
    # Команда выполняет grep с Tail на Master сервере по SSH
    cmd = (
        'docker logs db_container 2>&1 | '
        'grep -iE "replication|walsender|walreceiver|ready to accept connections|received replication command" '
        '| tail -n 30'
    )
    send_ssh_result(message, "Логи репликации PostgreSQL (Master)", cmd)


# 3. Проверка пароля
@bot.message_handler(commands=['verify_password'])
def cmd_verify_password(message):
    logger.info(f"Пользователь {message.chat.id} запустил проверку пароля")
    msg = bot.reply_to(message, "Отправь мне пароль для проверки:")
    bot.register_next_step_handler(msg, process_password_verification)

def process_password_verification(message):
    password = message.text
    if not password:
        bot.reply_to(message, "Пожалуйста, отправь текстовое сообщение с паролем.")
        return
        
    strong = is_password_strong(password)
    log_action_to_db(message.chat.id, '/verify_password', True, f"Сложный: {strong}")
    if strong:
        bot.reply_to(message, "Пароль сложный")
    else:
        bot.reply_to(message, "Пароль простой")


# Сценарии мониторинга Linux

@bot.message_handler(commands=['get_release'])
def cmd_get_release(message):
    send_ssh_result(message, "Релиз ОС", "cat /etc/os-release")

@bot.message_handler(commands=['get_uname'])
def cmd_get_uname(message):
    send_ssh_result(message, "Сведения о ядре и хосте", "uname -a")

@bot.message_handler(commands=['get_uptime'])
def cmd_get_uptime(message):
    send_ssh_result(message, "Время работы (uptime)", "uptime")

@bot.message_handler(commands=['get_df'])
def cmd_get_df(message):
    send_ssh_result(message, "Файловая система (df -h)", "df -h")

@bot.message_handler(commands=['get_free'])
def cmd_get_free(message):
    send_ssh_result(message, "Оперативная память (free -h)", "free -h")

@bot.message_handler(commands=['get_mpstat'])
def cmd_get_mpstat(message):
    send_ssh_result(message, "Производительность процессора (mpstat)", "mpstat 1 1")

@bot.message_handler(commands=['get_w'])
def cmd_get_w(message):
    send_ssh_result(message, "Активные пользователи (w)", "w")

@bot.message_handler(commands=['get_auths'])
def cmd_get_auths(message):
    send_ssh_result(message, "Последние 10 входов (last)", "last -n 10")

@bot.message_handler(commands=['get_critical'])
def cmd_get_critical(message):
    # Берем критические события через journalctl
    send_ssh_result(message, "Последние 5 критических событий", "journalctl -p err -n 5 --no-pager")

@bot.message_handler(commands=['get_ps'])
def cmd_get_ps(message):
    # Ограничиваем список первыми 30 строками, чтобы не превысить лимит сообщения
    send_ssh_result(message, "Запущенные процессы (top-30)", "ps aux | head -n 30")

@bot.message_handler(commands=['get_ss'])
def cmd_get_ss(message):
    send_ssh_result(message, "Используемые порты (ss)", "ss -tuln")

@bot.message_handler(commands=['get_services'])
def cmd_get_services(message):
    send_ssh_result(message, "Запущенные службы systemd", "systemctl list-units --type=service --state=running --no-pager")


# Пакеты (APT List) с интерактивным выбором

@bot.message_handler(commands=['get_apt_list'])
def cmd_get_apt_list(message):
    logger.info(f"Пользователь {message.chat.id} вызвал get_apt_list")
    markup = types.InlineKeyboardMarkup()
    btn_all = types.InlineKeyboardButton("Все пакеты", callback_data="apt_all")
    btn_search = types.InlineKeyboardButton("Поиск пакета", callback_data="apt_search")
    markup.row(btn_all, btn_search)
    
    bot.send_message(
        message.chat.id, 
        "Выбери режим получения пакетов:", 
        reply_markup=markup
    )

@bot.callback_query_handler(func=lambda call: call.data == "apt_all")
def callback_apt_all(call):
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "Запрашиваю список пакетов... Это может занять несколько секунд.")
    
    result = run_ssh_command("dpkg -l")
    
    # Т.к. список пакетов огромный, отправляем его как текстовый файл
    try:
        with tempfile.NamedTemporaryFile(mode='w+', delete=False, suffix='.txt', encoding='utf-8') as temp_file:
            temp_file.write(result)
            temp_path = temp_file.name
            
        with open(temp_path, 'rb') as doc:
            bot.send_document(
                call.message.chat.id,
                doc,
                visible_file_name="installed_packages.txt",
                caption="Все установленные в системе пакеты"
            )
        os.remove(temp_path)
    except Exception as e:
        logger.exception("Ошибка отправки файла пакетов")
        bot.send_message(call.message.chat.id, f"Ошибка формирования файла: {e}")

@bot.callback_query_handler(func=lambda call: call.data == "apt_search")
def callback_apt_search(call):
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, "Введи название пакета для поиска:")
    bot.register_next_step_handler(msg, process_apt_search)

def process_apt_search(message):
    package_name = message.text
    if not package_name:
        bot.reply_to(message, "Пожалуйста, введи название пакета.")
        return
        
    # Разрешаем только буквы, цифры, дефисы и точки во избежание инъекций в bash
    clean_name = "".join(c for c in package_name if c.isalnum() or c in "-._")
    if not clean_name:
        bot.reply_to(message, "Недопустимое имя пакета.")
        return
        
    bot.send_chat_action(message.chat.id, 'typing')
    
    # Ищем пакет
    cmd = f"dpkg -l | grep -i '{clean_name}'"
    result = run_ssh_command(cmd)
    
    if "вывод пуст" in result.lower() or not result.strip() or "Не удалось подключиться" in result:
        response = f"Пакет '{clean_name}' не найден среди установленных (или сервер недоступен)."
        log_action_to_db(message.chat.id, 'apt_search', False, f"Package '{clean_name}' not found")
        bot.reply_to(message, response)
    else:
        if len(result) > 4000:
            result = result[:4000] + "\n...[Вывод обрезан]..."
            
        log_action_to_db(message.chat.id, 'apt_search', True, f"Found package '{clean_name}'")
        escaped_result = html.escape(result)
        response = f"<b>Результаты поиска по запросу '{clean_name}':</b>\n<pre>{escaped_result}</pre>"
        bot.reply_to(message, response, parse_mode='HTML')


# Запуск

if __name__ == '__main__':
    logger.info("Запуск бота...")
    try:
        bot.infinity_polling()
    except Exception as e:
        logger.exception("Критическое исключение при работе")
