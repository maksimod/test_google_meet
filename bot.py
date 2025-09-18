#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import sys
import json
import logging
import datetime
import time
import threading
import schedule
import pytz
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, BotCommand
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext, ConversationHandler
from google_meet.google_meet import google_meet

# Load environment variables from .env file
load_dotenv()

# Enable logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Removed lock file mechanism
def check_single_instance():
    """Function kept for compatibility but now always returns True."""
    logger.info("Lock file mechanism disabled")
    return True

# Conversation states
ADD_SCHEDULE, DELETE_SCHEDULE, ADD_REMINDER_TIME, ADD_REMINDER_FREQUENCY, ADD_REMINDER_TEXT, DELETE_REMINDER = range(6)

# Storage for scheduled tasks
SCHEDULE_FILE = 'scheduled_meets.json'
scheduled_meets = {}
schedule_lock = threading.Lock()

# Storage for reminders
REMINDERS_FILE = 'reminders.json'
reminders = {}
reminders_lock = threading.Lock()

# Moscow timezone
MOSCOW_TZ = pytz.timezone('Europe/Moscow')

# Days of the week in Russian
DAYS_RU = {
    'понедельник': 0,
    'вторник': 1,
    'среда': 2,
    'четверг': 3,
    'пятница': 4,
    'суббота': 5,
    'воскресенье': 6
}

# Days of the week in Russian for display
DAYS_DISPLAY = {
    0: 'понедельник',
    1: 'вторник',
    2: 'среда',
    3: 'четверг',
    4: 'пятница',
    5: 'суббота',
    6: 'воскресенье'
}

# Используем dict для отслеживания состояний в разных чатах
user_states = {}
# Временное хранилище для создаваемых напоминаний
temp_reminders = {}

def load_schedules():
    """Load schedules from file."""
    global scheduled_meets
    if os.path.exists(SCHEDULE_FILE):
        with open(SCHEDULE_FILE, 'r') as f:
            data = json.load(f)
            scheduled_meets = {int(k): v for k, v in data.items()}
    else:
        scheduled_meets = {}

def save_schedules():
    """Save schedules to file."""
    with open(SCHEDULE_FILE, 'w') as f:
        json.dump(scheduled_meets, f)

def load_reminders():
    """Load reminders from file."""
    global reminders
    if os.path.exists(REMINDERS_FILE):
        with open(REMINDERS_FILE, 'r') as f:
            data = json.load(f)
            reminders = {int(k): v for k, v in data.items()}
    else:
        reminders = {}

def save_reminders():
    """Save reminders to file."""
    with open(REMINDERS_FILE, 'w') as f:
        json.dump(reminders, f)

def setup_commands(updater):
    """Set up bot commands in menu"""
    commands = [
        BotCommand("start", "Начать работу с ботом"),
        BotCommand("help", "Показать справку"),
        BotCommand("add", "Добавить еженедельную отправку"),
        BotCommand("addtime", "Добавить отправку в формате: /addtime день ЧЧ:ММ"),
        BotCommand("list", "Посмотреть отправки"),
        BotCommand("delete", "Удалить отправку"),
        BotCommand("deletetime", "Удалить отправку в формате: /deletetime день ЧЧ:ММ"),
        BotCommand("meet", "Мгновенная встреча"),
        BotCommand("reminder", "Создать напоминание"),
        BotCommand("addreminder", "Быстрое создание напоминания"),
        BotCommand("reminders", "Посмотреть напоминания"),
        BotCommand("deletereminder", "Удалить напоминание")
    ]
    updater.bot.set_my_commands(commands)
    logger.info("Bot commands have been set up")

def start(update: Update, context: CallbackContext) -> None:
    """Send a message when the command /start is issued."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    
    keyboard = [
        ['/add', '/list'],
        ['/delete', '/meet'],
        ['/reminder', '/reminders'],
        ['/help']
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    update.message.reply_text(
        f'Привет, {user.first_name}! Я бот для создания и отправки ссылок на Microsoft Teams и напоминаний.\n\n'
        'Используйте команды ниже для управления.',
        reply_markup=reply_markup
    )
    
    # Register chat_id if it's a group
    if update.effective_chat.type in ['group', 'supergroup']:
        with schedule_lock:
            load_schedules()
            group_name = update.effective_chat.title
            logger.info(f"Bot added to group: {group_name} ({chat_id})")

def add_schedule_command(update: Update, context: CallbackContext) -> int:
    """Start the process of adding a new schedule."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    # Сохраняем состояние пользователя/чата
    user_states[f"{chat_id}_{user_id}"] = ADD_SCHEDULE
    
    # Проверяем, является ли отправитель администратором группы, если это групповой чат
    if update.effective_chat.type in ['group', 'supergroup']:
        try:
            member = context.bot.get_chat_member(chat_id, user_id)
            if member.status not in ['creator', 'administrator']:
                update.message.reply_text('Только администраторы группы могут управлять расписанием.')
                return ConversationHandler.END
        except Exception as e:
            logger.error(f"Error checking admin status: {e}")
            update.message.reply_text('Произошла ошибка при проверке прав администратора.')
            return ConversationHandler.END
    
    update.message.reply_text(
        'Укажите день недели и время по Москве в формате "день ЧЧ:ММ"\n'
        'Например: среда 12:46'
    )
    return ADD_SCHEDULE

def process_schedule_add(update: Update, context: CallbackContext) -> int:
    """Process a schedule request."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    state_key = f"{chat_id}_{user_id}"
    
    # Получаем thread_id (ID темы), если сообщение из темы в супергруппе
    thread_id = update.message.message_thread_id if hasattr(update.message, 'message_thread_id') else None
    
    # Проверяем состояние пользователя
    if state_key not in user_states or user_states[state_key] != ADD_SCHEDULE:
        logger.info(f"Received message without valid state: {update.message.text} from {user_id} in chat {chat_id}")
        return ConversationHandler.END
    
    text = update.message.text.strip().lower()
    logger.info(f"Processing add schedule: {text} from user {user_id} in chat {chat_id}")
    
    try:
        day_text, time_text = text.split(' ', 1)
        
        if day_text not in DAYS_RU:
            update.message.reply_text(
                f'Некорректный день недели. Используйте: {", ".join(DAYS_RU.keys())}'
            )
            return ADD_SCHEDULE
        
        day_of_week = DAYS_RU[day_text]
        
        try:
            hours, minutes = map(int, time_text.split(':'))
            if not (0 <= hours < 24 and 0 <= minutes < 60):
                raise ValueError("Invalid time")
        except:
            update.message.reply_text('Некорректный формат времени. Используйте ЧЧ:ММ, например 12:46')
            return ADD_SCHEDULE
        
        with schedule_lock:
            load_schedules()
            
            if chat_id not in scheduled_meets:
                scheduled_meets[chat_id] = []
            
            # Check if this schedule already exists
            for schedule_item in scheduled_meets[chat_id]:
                if schedule_item['day'] == day_of_week and schedule_item['hours'] == hours and schedule_item['minutes'] == minutes:
                    update.message.reply_text('Такое расписание уже существует!')
                    # Очищаем состояние
                    if state_key in user_states:
                        del user_states[state_key]
                    return ConversationHandler.END
            
            # Создаем запись расписания
            schedule_entry = {
                'day': day_of_week,
                'hours': hours,
                'minutes': minutes
            }
            
            # Добавляем thread_id, если он есть
            if thread_id is not None:
                schedule_entry['thread_id'] = thread_id
            
            # Add new schedule
            scheduled_meets[chat_id].append(schedule_entry)
            
            save_schedules()
            
            # Schedule just this new task instead of rescheduling everything
            if hasattr(context, 'job_queue') and context.job_queue:
                create_job_for_schedule(
                    context.bot,
                    context.job_queue,
                    chat_id,
                    day_of_week,
                    hours,
                    minutes,
                    thread_id
                )
        
        update.message.reply_text(
            f'Еженедельная отправка добавлена: {day_text} {hours:02d}:{minutes:02d}'
        )
    except Exception as e:
        logger.error(f"Error adding schedule: {e}")
        update.message.reply_text(
            'Произошла ошибка. Убедитесь, что формат "день ЧЧ:ММ" правильный.\n'
            'Например: среда 12:46'
        )
    
    # Очищаем состояние
    if state_key in user_states:
        del user_states[state_key]
    
    return ConversationHandler.END

def list_schedules(update: Update, context: CallbackContext) -> None:
    """Show all scheduled tasks for the user."""
    chat_id = update.effective_chat.id
    
    # Получаем thread_id (ID темы), если сообщение из темы в супергруппе
    thread_id = update.message.message_thread_id if hasattr(update.message, 'message_thread_id') else None
    
    with schedule_lock:
        load_schedules()
        
        if chat_id not in scheduled_meets or not scheduled_meets[chat_id]:
            update.message.reply_text('У вас нет запланированных еженедельных отправок.')
            return
        
        schedules_list = []
        for schedule_item in scheduled_meets[chat_id]:
            # Если сообщение из темы, показываем только настройки для этой темы
            if thread_id is not None and schedule_item.get('thread_id') != thread_id:
                continue
                
            day = DAYS_DISPLAY[schedule_item['day']]
            hours = schedule_item['hours']
            minutes = schedule_item['minutes']
            schedules_list.append(f"{day} {hours:02d}:{minutes:02d}")
        
        if not schedules_list and thread_id is not None:
            update.message.reply_text('В этой теме нет запланированных еженедельных отправок.')
            return
            
        message = "Ваши еженедельные отправки:\n" + "\n".join(schedules_list)
        update.message.reply_text(message)

def delete_schedule_command(update: Update, context: CallbackContext) -> int:
    """Start the process of deleting a schedule."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    # Сохраняем состояние пользователя/чата
    user_states[f"{chat_id}_{user_id}"] = DELETE_SCHEDULE
    
    # Проверяем, является ли отправитель администратором группы, если это групповой чат
    if update.effective_chat.type in ['group', 'supergroup']:
        try:
            member = context.bot.get_chat_member(chat_id, user_id)
            if member.status not in ['creator', 'administrator']:
                update.message.reply_text('Только администраторы группы могут управлять расписанием.')
                return ConversationHandler.END
        except Exception as e:
            logger.error(f"Error checking admin status: {e}")
            update.message.reply_text('Произошла ошибка при проверке прав администратора.')
            return ConversationHandler.END
    
    with schedule_lock:
        load_schedules()
        
        if chat_id not in scheduled_meets or not scheduled_meets[chat_id]:
            update.message.reply_text('У вас нет запланированных еженедельных отправок.')
            return ConversationHandler.END
    
    update.message.reply_text(
        'Укажите день и время отправки, которую нужно удалить, в формате "день ЧЧ:ММ"\n'
        'Например: среда 12:46'
    )
    return DELETE_SCHEDULE

def process_schedule_delete(update: Update, context: CallbackContext) -> int:
    """Process a schedule deletion request."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    state_key = f"{chat_id}_{user_id}"
    
    # Проверяем состояние пользователя
    if state_key not in user_states or user_states[state_key] != DELETE_SCHEDULE:
        logger.info(f"Received message without valid delete state: {update.message.text} from {user_id} in chat {chat_id}")
        return ConversationHandler.END
    
    text = update.message.text.strip().lower()
    logger.info(f"Processing delete schedule: {text} from user {user_id} in chat {chat_id}")
    
    try:
        # Раздельный анализ формата "день ЧЧ:ММ"
        parts = text.split()
        
        if len(parts) != 2:
            update.message.reply_text(
                'Некорректный формат. Используйте точно формат "день ЧЧ:ММ", например: среда 12:46'
            )
            return DELETE_SCHEDULE
            
        day_text = parts[0]
        time_text = parts[1]
        
        if day_text not in DAYS_RU:
            update.message.reply_text(
                f'Некорректный день недели. Используйте: {", ".join(DAYS_RU.keys())}'
            )
            return DELETE_SCHEDULE
        
        day_of_week = DAYS_RU[day_text]
        
        try:
            hours, minutes = map(int, time_text.split(':'))
            if not (0 <= hours < 24 and 0 <= minutes < 60):
                raise ValueError("Invalid time")
        except:
            update.message.reply_text('Некорректный формат времени. Используйте ЧЧ:ММ, например 12:46')
            return DELETE_SCHEDULE
        
        deleted = False
        with schedule_lock:
            load_schedules()
            
            # Логируем текущее состояние расписания перед удалением
            logger.info(f"Current schedules before deletion for chat {chat_id}: {scheduled_meets.get(chat_id, [])}")
            
            if chat_id in scheduled_meets:
                new_schedules = []
                for schedule_item in scheduled_meets[chat_id]:
                    if (schedule_item['day'] == day_of_week and 
                        schedule_item['hours'] == hours and 
                        schedule_item['minutes'] == minutes):
                        deleted = True
                        logger.info(f"Deleting schedule: {day_text} {hours:02d}:{minutes:02d} for chat {chat_id}")
                    else:
                        new_schedules.append(schedule_item)
                
                scheduled_meets[chat_id] = new_schedules
                save_schedules()
                
                # Логируем состояние расписания после удаления
                logger.info(f"Schedules after deletion for chat {chat_id}: {scheduled_meets.get(chat_id, [])}")
                
                # Remove the specific job instead of rescheduling everything
                if hasattr(context, 'job_queue') and context.job_queue:
                    for job in context.job_queue.jobs():
                        job_context = job.context
                        if (job_context.get('user_id') == chat_id and
                            job_context.get('day') == day_of_week and
                            job_context.get('hours') == hours and
                            job_context.get('minutes') == minutes):
                            job.schedule_removal()
                            logger.info(f"Removed job for chat {chat_id} on {DAYS_DISPLAY[day_of_week]} at {hours:02d}:{minutes:02d}")
        
        if deleted:
            update.message.reply_text(
                f'Еженедельная отправка удалена: {day_text} {hours:02d}:{minutes:02d}'
            )
        else:
            update.message.reply_text(
                f'Отправка {day_text} {hours:02d}:{minutes:02d} не найдена'
            )
    except Exception as e:
        logger.error(f"Error deleting schedule: {e}", exc_info=True)
        update.message.reply_text(
            'Произошла ошибка. Убедитесь, что формат "день ЧЧ:ММ" правильный.\n'
            'Например: среда 12:46'
        )
    
    # Очищаем состояние
    if state_key in user_states:
        del user_states[state_key]
    
    return ConversationHandler.END

def send_meet_link(context: CallbackContext) -> None:
    """Send a Google Meet link to the user or group."""
    job = context.job
    chat_id = job.context['user_id']  # это может быть ID пользователя или группы
    day = job.context['day']
    hours = job.context['hours']
    minutes = job.context['minutes']
    thread_id = job.context.get('thread_id')  # Получаем thread_id из контекста, если есть
    
    try:
        # Use static Google Meet link with increased timeout and retry
        for attempt in range(3):  # Try up to 3 times
            try:
                meet_link = "https://teams.microsoft.com/l/meetup-join/19%3Ameeting_ZjZhZjA5YzktZTg1NS00YjU5LTg0Y2MtNWE2ZTkyZDJkMWU4%40thread.v2/0?context=%7B%22Tid%22%3A%22c5df9671-e735-4318-9942-1bde198b3dd6%22%2C%22Oid%22%3A%22d2fbcd3c-fff2-48a6-8b55-99b4237b61c8%22%7D"
                
                day_text = DAYS_DISPLAY[day]
                
                # Параметры сообщения
                send_params = {
                    'chat_id': chat_id,
                    'text': f'Ваша еженедельная Microsoft Teams встреча ({day_text} {hours:02d}:{minutes:02d}):\n{meet_link}',
                    'disable_notification': False,  # Ensure notification is sent
                }
                
                # Добавляем параметр message_thread_id, если thread_id указан
                if thread_id is not None:
                    send_params['message_thread_id'] = thread_id
                
                # Отправляем сообщение с повышенным таймаутом
                message = context.bot.send_message(**send_params)
                
                # Параметры удаления сообщения
                delete_params = {
                    'chat_id': chat_id,
                    'message_id': message.message_id
                }
                
                # Schedule message deletion after 59 minutes
                context.job_queue.run_once(
                    lambda context: context.bot.delete_message(**delete_params),
                    3540,  # 59 minutes
                    context=None
                )
                
                logger.info(f"Successfully sent meet link to chat {chat_id} for {day_text} {hours:02d}:{minutes:02d}, thread_id={thread_id}")
                break  # Success, break out of retry loop
                
            except Exception as retry_error:
                logger.warning(f"Attempt {attempt+1}/3 failed: {retry_error}")
                if attempt < 2:  # If not the last attempt
                    time.sleep(2)  # Wait 2 seconds before retrying
                else:
                    raise  # On last attempt, re-raise the exception
                
    except Exception as e:
        logger.error(f"Error sending meet link after retries: {e}")
        try:
            # Параметры сообщения об ошибке
            send_params = {
                'chat_id': chat_id,
                'text': 'Произошла ошибка при отправке ссылки на Microsoft Teams.'
            }
            
            # Добавляем параметр message_thread_id, если thread_id указан
            if thread_id is not None:
                send_params['message_thread_id'] = thread_id
            
            context.bot.send_message(**send_params)
        except Exception as inner_e:
            logger.error(f"Failed to send error message: {inner_e}")

def create_job_for_schedule(bot, job_queue, user_id, day, hours, minutes, thread_id=None):
    """Create a job for the scheduler."""
    # We run the job at the specified time on the specified day of the week
    target_day = day
    
    # Calculate days until next occurrence
    now = datetime.datetime.now(MOSCOW_TZ)
    current_day = now.weekday()
    
    # Days until the next scheduled day (0-6)
    days_ahead = (target_day - current_day) % 7
    
    # If it's the same day but the time has passed, schedule for next week
    if days_ahead == 0:
        target_time = now.replace(hour=hours, minute=minutes, second=0, microsecond=0)
        if now > target_time:
            days_ahead = 7
    
    # Calculate the next run time
    target_date = now + datetime.timedelta(days=days_ahead)
    # Set the time one minute earlier to ensure message is sent on time
    target_time = target_date.replace(hour=hours, minute=minutes, second=0, microsecond=0) - datetime.timedelta(minutes=1)
    
    job_context = {
        'user_id': user_id,
        'day': day,
        'hours': hours,
        'minutes': minutes
    }
    
    # Добавляем thread_id в контекст, если он есть
    if thread_id is not None:
        job_context['thread_id'] = thread_id
    
    # Schedule the job with job_queue from telegram.ext with more precise timing
    job = job_queue.run_repeating(
        send_meet_link,
        interval=datetime.timedelta(days=7),  # Weekly
        first=target_time.astimezone(pytz.UTC),  # Convert to UTC for the job queue
        context=job_context
    )
    
    logger.info(f"Scheduled job for user {user_id} on {DAYS_DISPLAY[day]} at {hours:02d}:{minutes:02d}, thread_id={thread_id}, next run at {target_time}")
    return job

def setup_schedules(job_queue, bot) -> None:
    """Setup all schedules for all users."""
    # Clear all existing jobs in job_queue
    for job in job_queue.jobs():
        job.schedule_removal()
    
    with schedule_lock:
        load_schedules()
        
        logger.info(f"Setting up schedules from file: {scheduled_meets}")
        
        for user_id, user_schedules in scheduled_meets.items():
            for schedule_item in user_schedules:
                day = schedule_item['day']
                hours = schedule_item['hours']
                minutes = schedule_item['minutes']
                thread_id = schedule_item.get('thread_id')  # Получаем thread_id, если он есть
                
                # Create a job with the telegram job queue
                create_job_for_schedule(
                    bot,
                    job_queue,
                    int(user_id),  # Ensure user_id is an integer
                    day,
                    hours,
                    minutes,
                    thread_id
                )
        
        logger.info("All schedules have been set up successfully")

def help_command(update: Update, context: CallbackContext) -> None:
    """Send a message when the command /help is issued."""
    is_group = update.effective_chat.type in ['group', 'supergroup']
    
    if is_group:
        update.message.reply_text(
            'Команды бота в группах:\n\n'
            '📅 Microsoft Teams:\n'
            '/meet - получить мгновенную ссылку на встречу\n'
            '/addtime день ЧЧ:ММ - добавить еженедельную отправку\n'
            '/list - просмотреть все отправки\n'
            '/deletetime день ЧЧ:ММ - удалить отправку\n\n'
            '⏰ Напоминания:\n'
            '/addreminder ДД.ММ.ГГГГ ЧЧ:ММ периодичность текст - создать напоминание\n'
            '/reminders - просмотреть все напоминания\n'
            '/deletereminder - удалить напоминание\n\n'
            '📝 Примеры создания напоминаний:\n'
            '/addreminder 25.12.2024 15:30 однократно Поздравить с НГ\n'
            '/addreminder 06.08.2024 09:00 ежедневно Утренняя планерка\n'
            '/addreminder 12.08.2024 18:00 еженедельно Команда встреча\n\n'
            '🔄 Периодичность: однократно, ежедневно, еженедельно, ежемесячно, ежегодно\n\n'
            '/start - начать работу с ботом\n'
            '/help - показать эту справку\n\n'
            'В группах управлять расписанием могут только администраторы.'
        )
    else:
        update.message.reply_text(
            'Команды бота:\n\n'
            '📅 Microsoft Teams:\n'
            '/meet - получить мгновенную ссылку на встречу\n'
            '/add - добавить еженедельную отправку\n'
            '/list - просмотреть все отправки\n'
            '/delete - удалить отправку\n\n'
            '⏰ Напоминания:\n'
            '/reminder - создать напоминание (пошаговый диалог)\n'
            '/addreminder ДД.ММ.ГГГГ ЧЧ:ММ периодичность текст - быстрое создание\n'
            '/reminders - просмотреть все напоминания\n'
            '/deletereminder - удалить напоминание\n\n'
            '📝 Примеры:\n'
            '/addreminder 25.12.2024 15:30 однократно Поздравить с НГ\n'
            '/addreminder 06.08.2024 09:00 ежедневно Утренняя зарядка\n\n'
            '/start - начать работу с ботом\n'
            '/help - показать эту справку'
        )

def add_reminder_command(update: Update, context: CallbackContext) -> int:
    """Start the process of adding a new reminder."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    # Сохраняем состояние пользователя/чата
    state_key = f"{chat_id}_{user_id}"
    user_states[state_key] = ADD_REMINDER_TIME
    
    # Инициализируем временное хранилище для напоминания
    temp_reminders[state_key] = {
        'chat_id': chat_id,
        'thread_id': update.message.message_thread_id if hasattr(update.message, 'message_thread_id') else None
    }
    
    # Проверяем права администратора в группах
    if update.effective_chat.type in ['group', 'supergroup']:
        try:
            member = context.bot.get_chat_member(chat_id, user_id)
            if member.status not in ['creator', 'administrator']:
                update.message.reply_text('Только администраторы группы могут создавать напоминания.')
                if state_key in user_states:
                    del user_states[state_key]
                if state_key in temp_reminders:
                    del temp_reminders[state_key]
                return ConversationHandler.END
        except Exception as e:
            logger.error(f"Error checking admin status: {e}")
            update.message.reply_text('Произошла ошибка при проверке прав администратора.')
            return ConversationHandler.END
    
    update.message.reply_text(
        'Укажите время напоминания по Москве в формате "ДД.ММ.ГГГГ ЧЧ:ММ"\n'
        'Например: 25.12.2024 15:30'
    )
    return ADD_REMINDER_TIME

def process_reminder_time(update: Update, context: CallbackContext) -> int:
    """Process reminder time input."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    state_key = f"{chat_id}_{user_id}"
    
    logger.info(f"Processing reminder time from user {user_id} in chat {chat_id}")
    logger.info(f"Current user states: {user_states}")
    logger.info(f"Looking for state_key: {state_key}")
    
    if state_key not in user_states or user_states[state_key] != ADD_REMINDER_TIME:
        logger.warning(f"State key {state_key} not found or wrong state. Current state: {user_states.get(state_key, 'None')}")
        return ConversationHandler.END
    
    text = update.message.text.strip()
    logger.info(f"Processing time text: '{text}'")
    
    try:
        # Парсим дату и время
        reminder_datetime = datetime.datetime.strptime(text, "%d.%m.%Y %H:%M")
        reminder_datetime = MOSCOW_TZ.localize(reminder_datetime)
        
        # Проверяем, что время в будущем
        now = datetime.datetime.now(MOSCOW_TZ)
        if reminder_datetime <= now:
            update.message.reply_text('Время напоминания должно быть в будущем. Попробуйте снова.')
            return ADD_REMINDER_TIME
        
        # Сохраняем время
        temp_reminders[state_key]['datetime'] = reminder_datetime
        
        # Переходим к выбору периодичности
        user_states[state_key] = ADD_REMINDER_FREQUENCY
        
        keyboard = [
            ['Однократно', 'Каждый день'],
            ['Каждую неделю', 'Каждый месяц'],
            ['Каждый год']
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
        
        update.message.reply_text(
            'Выберите периодичность напоминания:',
            reply_markup=reply_markup
        )
        return ADD_REMINDER_FREQUENCY
        
    except ValueError:
        update.message.reply_text(
            'Некорректный формат даты и времени.\n'
            'Используйте формат "ДД.ММ.ГГГГ ЧЧ:ММ"\n'
            'Например: 25.12.2024 15:30'
        )
        return ADD_REMINDER_TIME

def process_reminder_frequency(update: Update, context: CallbackContext) -> int:
    """Process reminder frequency selection."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    state_key = f"{chat_id}_{user_id}"
    
    if state_key not in user_states or user_states[state_key] != ADD_REMINDER_FREQUENCY:
        return ConversationHandler.END
    
    text = update.message.text.strip()
    
    frequency_map = {
        'Однократно': 'once',
        'Каждый день': 'daily',
        'Каждую неделю': 'weekly',
        'Каждый месяц': 'monthly',
        'Каждый год': 'yearly'
    }
    
    if text not in frequency_map:
        update.message.reply_text('Пожалуйста, выберите один из предложенных вариантов.')
        return ADD_REMINDER_FREQUENCY
    
    # Сохраняем периодичность
    temp_reminders[state_key]['frequency'] = frequency_map[text]
    
    # Переходим к вводу текста напоминания
    user_states[state_key] = ADD_REMINDER_TEXT
    
    update.message.reply_text(
        'Введите текст напоминания:',
        reply_markup=ReplyKeyboardRemove()
    )
    return ADD_REMINDER_TEXT

def process_reminder_text(update: Update, context: CallbackContext) -> int:
    """Process reminder text and save the reminder."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    state_key = f"{chat_id}_{user_id}"
    
    if state_key not in user_states or user_states[state_key] != ADD_REMINDER_TEXT:
        return ConversationHandler.END
    
    text = update.message.text.strip()
    
    if not text:
        update.message.reply_text('Текст напоминания не может быть пустым. Попробуйте снова.')
        return ADD_REMINDER_TEXT
    
    # Сохраняем текст
    temp_reminders[state_key]['text'] = text
    
    # Создаем напоминание
    with reminders_lock:
        load_reminders()
        
        if chat_id not in reminders:
            reminders[chat_id] = []
        
        # Генерируем уникальный ID
        import uuid
        reminder_id = str(uuid.uuid4())
        
        reminder = {
            'id': reminder_id,
            'datetime': temp_reminders[state_key]['datetime'].isoformat(),
            'frequency': temp_reminders[state_key]['frequency'],
            'text': temp_reminders[state_key]['text'],
            'thread_id': temp_reminders[state_key].get('thread_id'),
            'created_at': datetime.datetime.now(MOSCOW_TZ).isoformat()
        }
        
        reminders[chat_id].append(reminder)
        save_reminders()
        
        # Планируем отправку напоминания
        if hasattr(context, 'job_queue') and context.job_queue:
            schedule_reminder(
                context.bot,
                context.job_queue,
                chat_id,
                reminder
            )
    
    # Показываем клавиатуру по умолчанию
    keyboard = [
        ['/add', '/list'],
        ['/delete', '/meet'],
        ['/reminder', '/reminders'],
        ['/help']
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    
    # Формируем сообщение об успехе до удаления временных данных
    datetime_str = temp_reminders[state_key]["datetime"].strftime("%d.%m.%Y %H:%M")
    frequency_str = {
        'once': 'Однократно',
        'daily': 'Каждый день',
        'weekly': 'Каждую неделю',
        'monthly': 'Каждый месяц',
        'yearly': 'Каждый год'
    }.get(temp_reminders[state_key]['frequency'], 'Неизвестно')
    
    # Очищаем временные данные и состояние
    if state_key in user_states:
        del user_states[state_key]
    if state_key in temp_reminders:
        del temp_reminders[state_key]
    
    update.message.reply_text(
        f'Напоминание создано!\n'
        f'Время: {datetime_str} МСК\n'
        f'Периодичность: {frequency_str}',
        reply_markup=reply_markup
    )
    
    return ConversationHandler.END

def send_instant_meet_link(update: Update, context: CallbackContext) -> None:
    """Send an instant Google Meet link to the user."""
    chat_id = update.effective_chat.id
    
    # Получаем thread_id (ID темы), если сообщение из темы в супергруппе
    thread_id = update.message.message_thread_id if hasattr(update.message, 'message_thread_id') else None
    
    try:
        # Use static Google Meet link with retry mechanism
        for attempt in range(3):  # Try up to 3 times
            try:
                meet_link = "https://teams.microsoft.com/l/meetup-join/19%3Ameeting_ZjZhZjA5YzktZTg1NS00YjU5LTg0Y2MtNWE2ZTkyZDJkMWU4%40thread.v2/0?context=%7B%22Tid%22%3A%22c5df9671-e735-4318-9942-1bde198b3dd6%22%2C%22Oid%22%3A%22d2fbcd3c-fff2-48a6-8b55-99b4237b61c8%22%7D"
                
                # Параметры сообщения
                send_params = {
                    'text': f'Ваша мгновенная Microsoft Teams ссылка:\n{meet_link}',
                    'disable_notification': False,  # Ensure notification is sent
                }
                
                # Отправляем сообщение
                message = update.message.reply_text(**send_params)
                
                # Параметры удаления сообщения
                delete_params = {
                    'chat_id': chat_id,
                    'message_id': message.message_id
                }
                
                # Schedule message deletion after 59 minutes
                context.job_queue.run_once(
                    lambda context: context.bot.delete_message(**delete_params),
                    3540,  # 59 minutes
                    context=None
                )
                
                logger.info(f"Successfully sent instant meet link to chat {chat_id}, thread_id={thread_id}")
                break  # Success, break out of retry loop
                
            except Exception as retry_error:
                logger.warning(f"Instant link attempt {attempt+1}/3 failed: {retry_error}")
                if attempt < 2:  # If not the last attempt
                    time.sleep(2)  # Wait 2 seconds before retrying
                else:
                    raise  # On last attempt, re-raise the exception
                
    except Exception as e:
        logger.error(f"Error sending instant meet link after retries: {e}")
        update.message.reply_text('Произошла ошибка при отправке ссылки на Microsoft Teams.')

def add_schedule_direct(update: Update, context: CallbackContext) -> None:
    """Directly add a schedule without conversation (for groups)."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    # Получаем thread_id (ID темы), если сообщение из темы в супергруппе
    thread_id = update.message.message_thread_id if hasattr(update.message, 'message_thread_id') else None
    
    # Проверяем, является ли отправитель администратором группы, если это групповой чат
    if update.effective_chat.type in ['group', 'supergroup']:
        try:
            member = context.bot.get_chat_member(chat_id, user_id)
            if member.status not in ['creator', 'administrator']:
                update.message.reply_text('Только администраторы группы могут управлять расписанием.')
                return
        except Exception as e:
            logger.error(f"Error checking admin status: {e}")
            update.message.reply_text('Произошла ошибка при проверке прав администратора.')
            return
    
    # Проверяем, есть ли аргументы команды
    if not context.args or len(context.args) < 2:
        update.message.reply_text(
            'Пожалуйста, укажите день недели и время в формате:\n'
            '/addtime день ЧЧ:ММ\n'
            'Например: /addtime среда 12:46'
        )
        return
    
    # Получаем день и время из аргументов
    day_text = context.args[0].lower()
    time_text = context.args[1]
    
    try:
        # Проверяем день недели
        if day_text not in DAYS_RU:
            update.message.reply_text(
                f'Некорректный день недели. Используйте: {", ".join(DAYS_RU.keys())}'
            )
            return
        
        day_of_week = DAYS_RU[day_text]
        
        # Проверяем формат времени
        try:
            hours, minutes = map(int, time_text.split(':'))
            if not (0 <= hours < 24 and 0 <= minutes < 60):
                raise ValueError("Invalid time")
        except:
            update.message.reply_text('Некорректный формат времени. Используйте ЧЧ:ММ, например 12:46')
            return
        
        with schedule_lock:
            load_schedules()
            
            if chat_id not in scheduled_meets:
                scheduled_meets[chat_id] = []
            
            # Проверяем, существует ли уже такое расписание
            for schedule_item in scheduled_meets[chat_id]:
                if schedule_item['day'] == day_of_week and schedule_item['hours'] == hours and schedule_item['minutes'] == minutes:
                    update.message.reply_text('Такое расписание уже существует!')
                    return
            
            # Создаем запись расписания
            schedule_entry = {
                'day': day_of_week,
                'hours': hours,
                'minutes': minutes
            }
            
            # Добавляем thread_id, если он есть
            if thread_id is not None:
                schedule_entry['thread_id'] = thread_id
                
            # Добавляем новое расписание
            scheduled_meets[chat_id].append(schedule_entry)
            
            save_schedules()
            
            # Создаем задание для нового расписания
            if hasattr(context, 'job_queue') and context.job_queue:
                create_job_for_schedule(
                    context.bot,
                    context.job_queue,
                    chat_id,
                    day_of_week,
                    hours,
                    minutes,
                    thread_id
                )
        
        update.message.reply_text(
            f'Еженедельная отправка добавлена: {day_text} {hours:02d}:{minutes:02d}'
        )
    except Exception as e:
        logger.error(f"Error adding schedule directly: {e}")
        update.message.reply_text('Произошла ошибка при добавлении расписания.')

def delete_schedule_direct(update: Update, context: CallbackContext) -> None:
    """Directly delete a schedule without conversation (for groups)."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    # Получаем thread_id (ID темы), если сообщение из темы в супергруппе
    thread_id = None
    if update.message and hasattr(update.message, 'message_thread_id'):
        thread_id = update.message.message_thread_id
    
    # Проверяем, является ли отправитель администратором группы, если это групповой чат
    if update.effective_chat.type in ['group', 'supergroup']:
        try:
            member = context.bot.get_chat_member(chat_id, user_id)
            if member.status not in ['creator', 'administrator']:
                update.message.reply_text('Только администраторы группы могут управлять расписанием.')
                return
        except Exception as e:
            logger.error(f"Error checking admin status: {e}")
            update.message.reply_text('Произошла ошибка при проверке прав администратора.')
            return
    
    # Получаем аргументы
    text = update.message.text.strip()
    
    # Удаляем имя бота, если команда вызвана с @botname
    if '@' in text:
        # Находим первый пробел после @botname и сохраняем аргументы
        at_pos = text.find('@')
        space_after_at = text.find(' ', at_pos)
        if space_after_at != -1:
            # Есть пробел после @botname - берем команду до @ и аргументы после пробела
            command_part = text[:at_pos]
            args_part = text[space_after_at + 1:]
            text = command_part + ' ' + args_part
        else:
            # Нет пробела после @botname - только команда без аргументов
            text = text.split('@', 1)[0].strip()
    
    # Удаляем команду из текста
    parts = text.split(' ', 1)
    if len(parts) < 2:
        update.message.reply_text(
            'Пожалуйста, укажите день и время в формате: /deletetime день ЧЧ:ММ\n'
            'Например: /deletetime среда 12:46'
        )
        return
    
    # Разбираем аргументы (день и время)
    arguments = parts[1].strip().lower()
    
    # Проверяем, есть ли в тексте день недели
    day_found = False
    for day in DAYS_RU.keys():
        if day in arguments:
            day_text = day
            day_found = True
            # Удаляем день из строки, чтобы проще было найти время
            remaining_text = arguments.replace(day, "").strip()
            break
    
    if not day_found:
        update.message.reply_text(
            f'Некорректный день недели. Используйте: {", ".join(DAYS_RU.keys())}\n'
            'Например: /deletetime среда 12:46'
        )
        return
    
    # Ищем время в формате ЧЧ:ММ в оставшемся тексте
    import re
    time_match = re.search(r'(\d{1,2}):(\d{2})', remaining_text)
    if not time_match:
        update.message.reply_text(
            'Некорректный формат времени. Используйте ЧЧ:ММ, например 12:46\n'
            'Например: /deletetime среда 12:46'
        )
        return
    
    hours = int(time_match.group(1))
    minutes = int(time_match.group(2))
    
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        update.message.reply_text('Некорректное время. Часы должны быть от 0 до 23, минуты от 0 до 59.')
        return
    
    logger.info(f"Processing direct delete: day={day_text}, time={hours}:{minutes:02d} from user {user_id} in chat {chat_id}, thread_id={thread_id}")
    
    try:
        day_of_week = DAYS_RU[day_text]
        
        deleted = False
        with schedule_lock:
            load_schedules()
            
            if chat_id in scheduled_meets:
                new_schedules = []
                for schedule_item in scheduled_meets[chat_id]:
                    # Проверяем, что schedule_item не None
                    if schedule_item is None:
                        logger.warning(f"Found None schedule_item in chat {chat_id}, skipping")
                        continue
                        
                    if (schedule_item['day'] == day_of_week and 
                        schedule_item['hours'] == hours and 
                        schedule_item['minutes'] == minutes and
                        schedule_item.get('thread_id') == thread_id):
                        deleted = True
                        logger.info(f"Deleting schedule: {day_text} {hours:02d}:{minutes:02d} for chat {chat_id}")
                    else:
                        new_schedules.append(schedule_item)
                
                scheduled_meets[chat_id] = new_schedules
                save_schedules()
                
                # Remove the specific job instead of rescheduling everything
                if hasattr(context, 'job_queue') and context.job_queue:
                    for job in context.job_queue.jobs():
                        job_context = job.context
                        if (job_context.get('user_id') == chat_id and
                            job_context.get('day') == day_of_week and
                            job_context.get('hours') == hours and
                            job_context.get('minutes') == minutes and
                            job_context.get('thread_id') == thread_id):
                            job.schedule_removal()
                            logger.info(f"Removed job for chat {chat_id} on {DAYS_DISPLAY[day_of_week]} at {hours:02d}:{minutes:02d}, thread_id={thread_id}")
        
        if deleted:
            update.message.reply_text(
                f'Еженедельная отправка удалена: {day_text} {hours:02d}:{minutes:02d}'
            )
        else:
            update.message.reply_text(
                f'Отправка {day_text} {hours:02d}:{minutes:02d} не найдена'
            )
    except Exception as e:
        logger.error(f"Error in direct delete schedule: {e}")
        update.message.reply_text(
            'Произошла ошибка. Убедитесь, что формат "/deletetime день ЧЧ:ММ" правильный.\n'
            'Например: /deletetime среда 12:46'
        )

def handle_text(update: Update, context: CallbackContext) -> None:
    """Handle text messages."""
    if not update.message:
        return
        
    text = update.message.text.strip()
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    state_key = f"{chat_id}_{user_id}"
    
    logger.info(f"Received text: '{text}' from user {user_id} in chat {chat_id}, state_key: {state_key}")
    logger.info(f"All active states: {user_states}")
    
    # Проверяем, если пользователь находится в состоянии диалога
    if state_key in user_states:
        state = user_states[state_key]
        logger.info(f"User {user_id} in chat {chat_id} has state: {state}")
        if state == ADD_SCHEDULE:
            return process_schedule_add(update, context)
        elif state == DELETE_SCHEDULE:
            return process_schedule_delete(update, context)
        elif state == ADD_REMINDER_TIME:
            return process_reminder_time(update, context)
        elif state == ADD_REMINDER_FREQUENCY:
            return process_reminder_frequency(update, context)
        elif state == ADD_REMINDER_TEXT:
            return process_reminder_text(update, context)
        elif state == DELETE_REMINDER:
            return process_reminder_delete(update, context)
    
    # Обработка команд с @username в группах
    if '@' in text:
        cmd_parts = text.split('@', 1)
        command = cmd_parts[0]
        if command == '/add':
            return add_schedule_command(update, context)
        elif command == '/list':
            return list_schedules(update, context)
        elif command == '/delete':
            return delete_schedule_command(update, context)
        elif command == '/meet':
            return send_instant_meet_link(update, context)
        elif command == '/reminder':
            return add_reminder_command(update, context)
        elif command == '/reminders':
            return list_reminders(update, context)
        elif command == '/deletereminder':
            return delete_reminder_command(update, context)
        elif command == '/addreminder':
            return add_reminder_direct(update, context)
    
    # Если это не состояние диалога, обрабатываем команды
    if text.startswith('/add') or text == 'Добавить еженедельную отправку':
        return add_schedule_command(update, context)
    elif text.startswith('/list') or text == 'Посмотреть отправки':
        return list_schedules(update, context)
    elif text.startswith('/delete') or text == 'Удалить отправку':
        return delete_schedule_command(update, context)
    elif text.startswith('/meet') or text == 'Мгновенная встреча':
        return send_instant_meet_link(update, context)
    elif text.startswith('/reminder') or text == 'Создать напоминание':
        return add_reminder_command(update, context)
    elif text.startswith('/addreminder'):
        return add_reminder_direct(update, context)
    elif text.startswith('/reminders') or text == 'Мои напоминания':
        return list_reminders(update, context)
    elif text.startswith('/deletereminder') or text == 'Удалить напоминание':
        return delete_reminder_command(update, context)
    else:
        # Не отвечаем на случайные сообщения в группах (кроме случаев, когда пользователь в диалоге)
        if update.effective_chat.type in ['private']:
            update.message.reply_text(
                'Используйте команды меню или /help для справки'
            )
        # В группах игнорируем сообщения, которые не являются состояниями диалога
        # (состояния диалога уже обработаны выше)

def send_reminder(context: CallbackContext) -> None:
    """Send a reminder message."""
    job = context.job
    chat_id = job.context['chat_id']
    reminder = job.context['reminder']
    
    try:
        # Параметры сообщения
        send_params = {
            'chat_id': chat_id,
            'text': f'⏰ Напоминание:\n\n{reminder["text"]}',
            'disable_notification': False,
        }
        
        # Добавляем параметр message_thread_id, если thread_id указан
        if reminder.get('thread_id') is not None:
            send_params['message_thread_id'] = reminder['thread_id']
        
        # Отправляем сообщение
        context.bot.send_message(**send_params)
        
        logger.info(f"Successfully sent reminder to chat {chat_id}")
        
        # Если напоминание однократное, удаляем его из списка
        if reminder['frequency'] == 'once':
            with reminders_lock:
                load_reminders()
                if chat_id in reminders:
                    reminders[chat_id] = [r for r in reminders[chat_id] if r['id'] != reminder['id']]
                    save_reminders()
                    
    except Exception as e:
        logger.error(f"Error sending reminder: {e}")

def schedule_reminder(bot, job_queue, chat_id, reminder):
    """Schedule a reminder based on its frequency."""
    try:
        # Парсим время напоминания
        reminder_datetime = datetime.datetime.fromisoformat(reminder['datetime'])
        
        # Контекст для задания
        job_context = {
            'chat_id': chat_id,
            'reminder': reminder
        }
        
        # Планируем напоминание в зависимости от периодичности
        if reminder['frequency'] == 'once':
            # Однократное напоминание
            job_queue.run_once(
                send_reminder,
                when=reminder_datetime.astimezone(pytz.UTC),
                context=job_context
            )
            logger.info(f"Scheduled one-time reminder for chat {chat_id} at {reminder_datetime}")
            
        elif reminder['frequency'] == 'daily':
            # Ежедневное напоминание
            job_queue.run_daily(
                send_reminder,
                time=reminder_datetime.time(),
                context=job_context
            )
            logger.info(f"Scheduled daily reminder for chat {chat_id} at {reminder_datetime.time()}")
            
        elif reminder['frequency'] == 'weekly':
            # Еженедельное напоминание
            job_queue.run_repeating(
                send_reminder,
                interval=datetime.timedelta(days=7),
                first=reminder_datetime.astimezone(pytz.UTC),
                context=job_context
            )
            logger.info(f"Scheduled weekly reminder for chat {chat_id} starting {reminder_datetime}")
            
        elif reminder['frequency'] == 'monthly':
            # Ежемесячное напоминание - используем run_repeating с интервалом 30 дней
            job_queue.run_repeating(
                send_reminder,
                interval=datetime.timedelta(days=30),
                first=reminder_datetime.astimezone(pytz.UTC),
                context=job_context
            )
            logger.info(f"Scheduled monthly reminder for chat {chat_id} starting {reminder_datetime}")
            
        elif reminder['frequency'] == 'yearly':
            # Ежегодное напоминание
            job_queue.run_repeating(
                send_reminder,
                interval=datetime.timedelta(days=365),
                first=reminder_datetime.astimezone(pytz.UTC),
                context=job_context
            )
            logger.info(f"Scheduled yearly reminder for chat {chat_id} starting {reminder_datetime}")
            
    except Exception as e:
        logger.error(f"Error scheduling reminder: {e}")

def list_reminders(update: Update, context: CallbackContext) -> None:
    """Show all reminders for the chat."""
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id if hasattr(update.message, 'message_thread_id') else None
    
    with reminders_lock:
        load_reminders()
        
        if chat_id not in reminders or not reminders[chat_id]:
            update.message.reply_text('У вас нет активных напоминаний.')
            return
        
        reminders_list = []
        for idx, reminder in enumerate(reminders[chat_id], 1):
            # Если сообщение из темы, показываем только напоминания для этой темы
            if thread_id is not None and reminder.get('thread_id') != thread_id:
                continue
                
            dt = datetime.datetime.fromisoformat(reminder['datetime'])
            frequency_str = {
                'once': 'однократно',
                'daily': 'ежедневно',
                'weekly': 'еженедельно',
                'monthly': 'ежемесячно',
                'yearly': 'ежегодно'
            }.get(reminder['frequency'], 'неизвестно')
            
            reminders_list.append(
                f"{idx}. {dt.strftime('%d.%m.%Y %H:%M')} ({frequency_str})\n"
                f"   Текст: {reminder['text'][:50]}{'...' if len(reminder['text']) > 50 else ''}"
            )
        
        if not reminders_list and thread_id is not None:
            update.message.reply_text('В этой теме нет активных напоминаний.')
            return
            
        message = "Ваши напоминания:\n\n" + "\n\n".join(reminders_list)
        update.message.reply_text(message)

def add_reminder_direct(update: Update, context: CallbackContext) -> None:
    """Directly add a reminder with all parameters in one command (for groups)."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    # Получаем thread_id (ID темы), если сообщение из темы в супергруппе
    thread_id = update.message.message_thread_id if hasattr(update.message, 'message_thread_id') else None
    
    # Проверяем, является ли отправитель администратором группы, если это групповой чат
    if update.effective_chat.type in ['group', 'supergroup']:
        try:
            member = context.bot.get_chat_member(chat_id, user_id)
            if member.status not in ['creator', 'administrator']:
                update.message.reply_text('Только администраторы группы могут создавать напоминания.')
                return
        except Exception as e:
            logger.error(f"Error checking admin status: {e}")
            update.message.reply_text('Произошла ошибка при проверке прав администратора.')
            return
    
    # Получаем полный текст команды
    full_text = update.message.text.strip()
    
    # Удаляем имя бота, если команда вызвана с @botname
    if '@' in full_text:
        # Находим первый пробел после @botname и удаляем только @botname
        at_pos = full_text.find('@')
        space_after_at = full_text.find(' ', at_pos)
        if space_after_at != -1:
            # Есть пробел после @botname - удаляем только @botname
            command_part = full_text[:at_pos]
            args_part = full_text[space_after_at:]
            full_text = command_part + args_part
        else:
            # Нет пробела после @botname - только команда без аргументов
            full_text = full_text.split('@', 1)[0].strip()
    
    # Разбираем команду
    parts = full_text.split(' ', 1)
    if len(parts) < 2:
        update.message.reply_text(
            'Используйте формат:\n'
            '/addreminder ДД.ММ.ГГГГ ЧЧ:ММ периодичность текст напоминания\n\n'
            'Например:\n'
            '/addreminder 25.12.2024 15:30 однократно Поздравить с Новым годом\n'
            '/addreminder 06.08.2024 09:00 ежедневно Утренняя планерка\n\n'
            'Периодичность: однократно, ежедневно, еженедельно, ежемесячно, ежегодно'
        )
        return
    
    # Парсим аргументы
    args_text = parts[1].strip()
    args_parts = args_text.split(' ')
    
    if len(args_parts) < 4:
        update.message.reply_text(
            'Недостаточно параметров. Используйте формат:\n'
            '/addreminder ДД.ММ.ГГГГ ЧЧ:ММ периодичность текст напоминания\n\n'
            'Например:\n'
            '/addreminder 25.12.2024 15:30 однократно Поздравить с Новым годом'
        )
        return
    
    date_str = args_parts[0]
    time_str = args_parts[1]
    frequency_str = args_parts[2].lower()
    reminder_text = ' '.join(args_parts[3:])
    
    # Валидация периодичности
    frequency_map = {
        'однократно': 'once',
        'ежедневно': 'daily',
        'еженедельно': 'weekly',
        'ежемесячно': 'monthly',
        'ежегодно': 'yearly'
    }
    
    if frequency_str not in frequency_map:
        update.message.reply_text(
            f'Неверная периодичность "{frequency_str}". Используйте:\n'
            'однократно, ежедневно, еженедельно, ежемесячно, ежегодно\n\n'
            'Например: /addreminder 25.12.2024 15:30 однократно Текст напоминания'
        )
        return
    
    frequency = frequency_map[frequency_str]
    
    try:
        # Парсим дату и время
        datetime_str = f"{date_str} {time_str}"
        reminder_datetime = datetime.datetime.strptime(datetime_str, "%d.%m.%Y %H:%M")
        reminder_datetime = MOSCOW_TZ.localize(reminder_datetime)
        
        # Проверяем, что время в будущем
        now = datetime.datetime.now(MOSCOW_TZ)
        if reminder_datetime <= now:
            update.message.reply_text('Время напоминания должно быть в будущем.')
            return
        
        # Создаем напоминание
        with reminders_lock:
            load_reminders()
            
            if chat_id not in reminders:
                reminders[chat_id] = []
            
            # Генерируем уникальный ID
            import uuid
            reminder_id = str(uuid.uuid4())
            
            reminder = {
                'id': reminder_id,
                'datetime': reminder_datetime.isoformat(),
                'frequency': frequency,
                'text': reminder_text,
                'thread_id': thread_id,
                'created_at': datetime.datetime.now(MOSCOW_TZ).isoformat()
            }
            
            reminders[chat_id].append(reminder)
            save_reminders()
            
            # Планируем отправку напоминания
            if hasattr(context, 'job_queue') and context.job_queue:
                schedule_reminder(
                    context.bot,
                    context.job_queue,
                    chat_id,
                    reminder
                )
        
        frequency_display = {
            'once': 'однократно',
            'daily': 'ежедневно',
            'weekly': 'еженедельно',
            'monthly': 'ежемесячно',
            'yearly': 'ежегодно'
        }.get(frequency, frequency)
        
        update.message.reply_text(
            f'✅ Напоминание создано!\n'
            f'📅 Время: {reminder_datetime.strftime("%d.%m.%Y %H:%M")} МСК\n'
            f'🔄 Периодичность: {frequency_display}\n'
            f'📝 Текст: {reminder_text}'
        )
        
    except ValueError as e:
        update.message.reply_text(
            'Некорректный формат даты или времени.\n'
            'Используйте формат: ДД.ММ.ГГГГ ЧЧ:ММ\n'
            'Например: 25.12.2024 15:30'
        )
        logger.error(f"Date parsing error: {e}")
    except Exception as e:
        logger.error(f"Error creating reminder: {e}")
        update.message.reply_text('Произошла ошибка при создании напоминания.')

def delete_reminder_command(update: Update, context: CallbackContext) -> int:
    """Start the process of deleting a reminder."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    # Проверяем права администратора в группах
    if update.effective_chat.type in ['group', 'supergroup']:
        try:
            member = context.bot.get_chat_member(chat_id, user_id)
            if member.status not in ['creator', 'administrator']:
                update.message.reply_text('Только администраторы группы могут удалять напоминания.')
                return ConversationHandler.END
        except Exception as e:
            logger.error(f"Error checking admin status: {e}")
            update.message.reply_text('Произошла ошибка при проверке прав администратора.')
            return ConversationHandler.END
    
    # Проверяем, есть ли аргументы команды (для прямого удаления)
    text = update.message.text.strip()
    parts = text.split(' ', 1)
    
    if len(parts) > 1:
        # Пытаемся удалить напоминание по дате и времени
        args_text = parts[1].strip()
        
        # Удаляем имя бота, если команда вызвана с @botname
        if '@' in args_text:
            args_text = args_text.split('@')[0].strip()
        
        # Парсим дату и время
        import re
        datetime_match = re.match(r'(\d{1,2}\.\d{1,2}\.\d{4})\s+(\d{1,2}:\d{2})', args_text)
        
        if datetime_match:
            date_str = datetime_match.group(1)
            time_str = datetime_match.group(2)
            
            try:
                # Парсим дату и время
                datetime_str = f"{date_str} {time_str}"
                target_datetime = datetime.datetime.strptime(datetime_str, "%d.%m.%Y %H:%M")
                target_datetime = MOSCOW_TZ.localize(target_datetime)
                
                # Ищем и удаляем напоминание
                with reminders_lock:
                    load_reminders()
                    
                    if chat_id in reminders:
                        deleted = False
                        new_reminders = []
                        
                        for reminder in reminders[chat_id]:
                            reminder_dt = datetime.datetime.fromisoformat(reminder['datetime'])
                            # Сравниваем только дату и время (без секунд)
                            if (reminder_dt.year == target_datetime.year and
                                reminder_dt.month == target_datetime.month and
                                reminder_dt.day == target_datetime.day and
                                reminder_dt.hour == target_datetime.hour and
                                reminder_dt.minute == target_datetime.minute):
                                deleted = True
                                logger.info(f"Deleting reminder: {datetime_str} for chat {chat_id}")
                            else:
                                new_reminders.append(reminder)
                        
                        if deleted:
                            reminders[chat_id] = new_reminders
                            save_reminders()
                            update.message.reply_text(f'Напоминание на {datetime_str} успешно удалено.')
                        else:
                            update.message.reply_text(f'Напоминание на {datetime_str} не найдено.')
                        
                        return ConversationHandler.END
                        
            except ValueError:
                # Если не удалось распарсить дату/время, продолжаем с интерактивным режимом
                pass
    
    with reminders_lock:
        load_reminders()
        
        if chat_id not in reminders or not reminders[chat_id]:
            update.message.reply_text('У вас нет активных напоминаний.')
            return ConversationHandler.END
    
    # Сохраняем состояние
    state_key = f"{chat_id}_{user_id}"
    user_states[state_key] = DELETE_REMINDER
    
    # Показываем список напоминаний с номерами
    thread_id = update.message.message_thread_id if hasattr(update.message, 'message_thread_id') else None
    
    reminders_list = []
    valid_indices = []
    
    for idx, reminder in enumerate(reminders[chat_id], 1):
        # Если сообщение из темы, показываем только напоминания для этой темы
        if thread_id is not None and reminder.get('thread_id') != thread_id:
            continue
            
        dt = datetime.datetime.fromisoformat(reminder['datetime'])
        frequency_str = {
            'once': 'однократно',
            'daily': 'ежедневно',
            'weekly': 'еженедельно',
            'monthly': 'ежемесячно',
            'yearly': 'ежегодно'
        }.get(reminder['frequency'], 'неизвестно')
        
        reminders_list.append(
            f"{idx}. {dt.strftime('%d.%m.%Y %H:%M')} ({frequency_str})\n"
            f"   Текст: {reminder['text'][:50]}{'...' if len(reminder['text']) > 50 else ''}"
        )
        valid_indices.append(idx)
    
    if not reminders_list:
        update.message.reply_text('В этой теме нет активных напоминаний.')
        if state_key in user_states:
            del user_states[state_key]
        return ConversationHandler.END
    
    # Сохраняем валидные индексы для этого пользователя
    temp_reminders[state_key] = {'valid_indices': valid_indices, 'thread_id': thread_id}
    
    message = "Выберите номер напоминания для удаления:\n\n" + "\n\n".join(reminders_list)
    update.message.reply_text(message)
    
    return DELETE_REMINDER

def process_reminder_delete(update: Update, context: CallbackContext) -> int:
    """Process reminder deletion."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    state_key = f"{chat_id}_{user_id}"
    
    if state_key not in user_states or user_states[state_key] != DELETE_REMINDER:
        return ConversationHandler.END
    
    text = update.message.text.strip()
    
    try:
        reminder_num = int(text)
        
        if state_key not in temp_reminders or reminder_num not in temp_reminders[state_key]['valid_indices']:
            update.message.reply_text('Некорректный номер напоминания. Попробуйте снова или /cancel для отмены.')
            return DELETE_REMINDER
        
        with reminders_lock:
            load_reminders()
            
            if chat_id in reminders:
                thread_id = temp_reminders[state_key].get('thread_id')
                
                # Фильтруем напоминания по thread_id, если нужно
                filtered_reminders = []
                indices_map = []  # Маппинг отображаемого индекса на реальный
                
                for real_idx, reminder in enumerate(reminders[chat_id]):
                    if thread_id is None or reminder.get('thread_id') == thread_id:
                        filtered_reminders.append(reminder)
                        indices_map.append(real_idx)
                
                # Проверяем, что номер в допустимых пределах
                if 0 < reminder_num <= len(filtered_reminders):
                    # Получаем реальный индекс напоминания для удаления
                    real_index = indices_map[reminder_num - 1]
                    
                    # Удаляем напоминание по реальному индексу
                    deleted_reminder = reminders[chat_id].pop(real_index)
                    save_reminders()
                    
                    # Отменяем задание в планировщике
                    # TODO: Реализовать отмену задания в job_queue
                    
                    dt = datetime.datetime.fromisoformat(deleted_reminder['datetime'])
                    update.message.reply_text(
                        f'Напоминание на {dt.strftime("%d.%m.%Y %H:%M")} успешно удалено.'
                    )
                else:
                    update.message.reply_text('Ошибка при удалении напоминания.')
            else:
                update.message.reply_text('Ошибка при удалении напоминания.')
        
    except ValueError:
        update.message.reply_text('Пожалуйста, введите номер напоминания.')
        return DELETE_REMINDER
    
    # Очищаем состояние
    if state_key in user_states:
        del user_states[state_key]
    if state_key in temp_reminders:
        del temp_reminders[state_key]
    
    return ConversationHandler.END

def setup_reminders(job_queue, bot):
    """Setup all reminders for all chats."""
    with reminders_lock:
        load_reminders()
        
        logger.info(f"Setting up reminders from file: {reminders}")
        
        for chat_id, chat_reminders in reminders.items():
            for reminder in chat_reminders:
                schedule_reminder(bot, job_queue, int(chat_id), reminder)
        
        logger.info("All reminders have been set up successfully")

def cleanup():
    """Clean up resources before exiting."""
    logger.info("Cleanup: Lock file mechanism disabled")
    pass

def main() -> None:
    """Start the bot."""
    # Always true now
    check_single_instance()
    
    try:
        # Get bot token from environment variable
        token = os.getenv("TELEGRAM_TOKEN")
        if not token:
            logger.error("No TELEGRAM_TOKEN found in .env file")
            return
        
        # Create the Updater and pass it your bot's token with increased timeouts
        updater = Updater(token, request_kwargs={'read_timeout': 60, 'connect_timeout': 60})
        
        # Get the dispatcher to register handlers
        dispatcher = updater.dispatcher
        
        # Add handlers
        dispatcher.add_handler(CommandHandler("start", start))
        dispatcher.add_handler(CommandHandler("help", help_command))
        dispatcher.add_handler(CommandHandler("meet", send_instant_meet_link))
        dispatcher.add_handler(CommandHandler("list", list_schedules))
        dispatcher.add_handler(CommandHandler("add", add_schedule_command))
        dispatcher.add_handler(CommandHandler("delete", delete_schedule_command))
        
        # Прямые команды для групп
        dispatcher.add_handler(CommandHandler("addtime", add_schedule_direct))
        dispatcher.add_handler(CommandHandler("deletetime", delete_schedule_direct))
        
        # Команды для напоминаний
        dispatcher.add_handler(CommandHandler("reminder", add_reminder_command))
        dispatcher.add_handler(CommandHandler("addreminder", add_reminder_direct))
        dispatcher.add_handler(CommandHandler("reminders", list_reminders))
        dispatcher.add_handler(CommandHandler("deletereminder", delete_reminder_command))
        
        # Обработка текстовых сообщений
        dispatcher.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_text))
        
        # Setup bot commands in menu
        setup_commands(updater)
        
        # Setup schedules using the job_queue from the updater
        load_schedules()
        setup_schedules(updater.job_queue, updater.bot)
        
        # Setup reminders using the job_queue from the updater
        load_reminders()
        setup_reminders(updater.job_queue, updater.bot)
        
        # Добавляем функцию для регулярной перезагрузки расписаний и напоминаний (раз в час)
        def reload_schedules_job(context):
            """Функция для периодической перезагрузки расписаний и напоминаний"""
            logger.info("Performing periodic schedule and reminders reload")
            try:
                setup_schedules(context.job_queue, context.bot)
                setup_reminders(context.job_queue, context.bot)
                logger.info("Periodic schedule and reminders reload completed successfully")
            except Exception as e:
                logger.error(f"Error during periodic schedule and reminders reload: {e}")
        
        # Запускаем регулярное задание для перезагрузки расписаний и напоминаний каждый час
        updater.job_queue.run_repeating(reload_schedules_job, interval=3600, first=300)
        logger.info("Set up periodic schedule and reminders reload (every hour)")
        
        # Start the Bot with increased allowed update types for better reliability
        updater.start_polling(allowed_updates=["message", "callback_query", "chat_member"], timeout=60, drop_pending_updates=False)
        logger.info("Bot started with improved reliability settings")
        
        # Run the bot until you press Ctrl-C
        updater.idle()
    finally:
        # Clean up when the bot stops
        cleanup()

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
        cleanup()
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        cleanup() 