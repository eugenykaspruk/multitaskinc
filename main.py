import logging
import os
from datetime import datetime, timedelta
from typing import Union, List, Dict
from pytz import timezone

from telegram import Update, ForceReply
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)

# 1. Configuration and Global Storage

USER_JOBS: Dict[int, List[str]] = {}
USER_TIMEZONE = timezone('Europe/Kyiv')
PORT = int(os.environ.get('PORT', '8443')) # Render will provide this PORT
WEBHOOK_URL_PATH = os.environ.get('WEBHOOK_URL_PATH', '')
WEBHOOK_DOMAIN = os.environ.get('WEBHOOK_DOMAIN') # The base URL for your Render app

# State constants for ConversationHandler
GET_EVENT_NAME, GET_EVENT_DATETIME, GET_REMINDER_OFFSET = range(3)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


# 2. Utility Functions

def parse_timedelta(text: str) -> Union[timedelta, None]:
    """Parses text like '1h', '30m', '1d', or '0m' into a timedelta object."""
    text = text.strip().lower()

    if text == '0m':
        return timedelta(minutes=0)

    try:
        value = int(text[:-1])  # Number part
        unit = text[-1]  # Unit part (h, m, d)
    except (ValueError, IndexError):
        return None

    if unit == 'm':
        return timedelta(minutes=value)
    elif unit == 'h':
        return timedelta(hours=value)
    elif unit == 'd':
        return timedelta(days=value)
    else:
        return None


# 3. Command Handlers and Conversation Steps

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Sends a welcome message."""
    user = update.effective_user
    await update.message.reply_html(
        f"Hello, {user.mention_html()}! 👋\n"
        "I'm your task and plan tracking assistant.\n"
        "Use /new to create a task and /list to see your upcoming reminders.\n\n"
        "<b>Supported formats:</b>\n"
        "• Date/Time: <code>YYYY-MM-DD HH:MM</code> (e.g., 2025-10-25 18:00)\n"
        "• Reminder Offset: <code>1h</code>, <code>30m</code>, <code>5m</code>, or <code>0m</code> (at event time).",
        reply_markup=ForceReply(selective=True),
    )
    return ConversationHandler.END


async def new_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts a new conversation for task creation."""
    await update.message.reply_html("📝 <b>Step 1/3:</b> What is the name of the event?")
    return GET_EVENT_NAME


async def get_event_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Stores the event name and prompts for date/time."""
    context.user_data["event_name"] = update.message.text
    await update.message.reply_html(
        "📅 <b>Step 2/3:</b> Enter the event date and time (e.g., 2025-10-25 18:00):"
    )
    return GET_EVENT_DATETIME


async def get_event_datetime(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Parses date/time, validates it, stores it, and prompts for the reminder offset."""
    try:
        # Parse date in YYYY-MM-DD HH:MM format
        naive_event_time = datetime.strptime(update.message.text.strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid date/time format. Please try again (e.g., 2025-10-25 18:00):"
        )
        return GET_EVENT_DATETIME  # Stay on this step

    localized_event_time = USER_TIMEZONE.localize(naive_event_time)

    # Simple check to ensure the event is in the future
    if localized_event_time < datetime.now(USER_TIMEZONE) - timedelta(minutes=1):  # Allow a small margin of error
        await update.message.reply_text(
            "❌ The event must be in the future. Please try again:"
        )
        return GET_EVENT_DATETIME  # Stay on this step

    context.user_data["event_time"] = localized_event_time
    await update.message.reply_html(
        "🔔 <b>Step 3/3:</b> How long before the event do you want the reminder?\n"
        "Use formats: <code>1h</code> (1 hour), <code>30m</code> (30 minutes), or <code>0m</code> (at the event time)."
    )
    return GET_REMINDER_OFFSET


async def set_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Parses reminder offset, calculates time, and schedules the Job."""

    # 1. Parse the offset (timedelta)
    reminder_offset = parse_timedelta(update.message.text)

    # Note: reminder_offset is correctly handled by the runtime logic,
    # even without the Union syntax in the function signature here.
    if reminder_offset is None:
        await update.message.reply_html(
            "❌ Invalid reminder format. Please use <code>1h</code>, <code>30m</code>, or <code>0m</code>:"
        )
        return GET_REMINDER_OFFSET  # Stay on this step

    # 2. Calculate reminder time
    event_time: datetime = context.user_data["event_time"]
    reminder_time = event_time - reminder_offset
    chat_id = update.effective_chat.id

    if reminder_time < datetime.now(USER_TIMEZONE):
        await update.message.reply_text(
            "❌ The calculated reminder time has already passed! Please try a smaller offset or reschedule the event."
        )
        return GET_REMINDER_OFFSET

    # 3. Plan the Reminder (JobQueue)
    event_name: str = context.user_data["event_name"]
    chat_id = update.effective_chat.id

    # We convert the localized time to UTC for the scheduler.
    utc_reminder_time = reminder_time.astimezone(timezone('UTC'))

    # Create a unique job name
    job_name = f"reminder_{chat_id}_{event_name}_{datetime.now().timestamp()}"

    # Dictionary with data for the reminder
    job_data = {
        'chat_id': chat_id,
        'event_name': event_name,
        'event_time_str': event_time.strftime("%Y-%m-%d %H:%M")
    }

    # Schedule the job
    context.application.job_queue.run_once(
        callback=send_reminder,
        when=utc_reminder_time,
        data=job_data,
        name=job_name
    )

    # Track the job name for the user
    if chat_id not in USER_JOBS:
        USER_JOBS[chat_id] = []
    USER_JOBS[chat_id].append(job_name)

    # 4. Send Confirmation
    await update.message.reply_html(
        f"✅ <b>Task scheduled!</b>\n"
        f"Event: <b>{event_name}</b>\n"
        f"Event Time: {event_time.strftime('%Y-%m-%d %H:%M')}\n"
        f"Reminder will arrive: {reminder_time.strftime('%Y-%m-%d %H:%M')}"
    )

    # End the conversation
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the current conversation."""
    await update.message.reply_text(
        "🚫 Task creation cancelled. You can start over with /new."
    )
    return ConversationHandler.END


# 4. Reminder and Listing Functionality

async def send_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    """The function called by JobQueue to send the reminder."""
    job_data = context.job.data
    chat_id = job_data['chat_id']
    event_name = job_data['event_name']
    event_time_str = job_data['event_time_str']

    # Remove the job from tracking list after it executes (optional, for cleanup)
    if chat_id in USER_JOBS and context.job.name in USER_JOBS[chat_id]:
        USER_JOBS[chat_id].remove(context.job.name)

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🔔 <b>REMINDER!</b>\n"
             f"The event <b>'{event_name}'</b> is scheduled for {event_time_str}.",
        parse_mode="HTML"
    )
    logger.info(f"Sent reminder for chat {chat_id} about event '{event_name}'.")


async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays all currently scheduled tasks for the user."""
    chat_id = update.effective_chat.id

    # Check if the user has any tracked jobs
    if chat_id not in USER_JOBS or not USER_JOBS[chat_id]:
        await update.message.reply_text("🚫 You have no scheduled tasks. Create a new one using /new.")
        return

    # Get all active jobs from the JobQueue based on the tracked names
    all_jobs = []
    for job_name in USER_JOBS[chat_id]:
        jobs = context.application.job_queue.get_jobs_by_name(name=job_name)
        if jobs:
            all_jobs.extend(jobs)

    if not all_jobs:
        await update.message.reply_text(
            "✅ Your tasks list is empty. All scheduled reminders might have already been sent.")
        USER_JOBS[chat_id] = []  # Clear the list if all jobs are finished/missing
        return

    response = "🗓️ <b>Your Scheduled Tasks:</b>\n\n"

    for job in all_jobs:
        # Check if the job belongs to this user (extra safety check)
        if job.data['chat_id'] == chat_id:
            event_name = job.data['event_name']
            event_time = job.data['event_time_str']
            # Get the scheduled reminder time (job.next_t is the next run time)
            local_reminder_time = job.next_t.astimezone(USER_TIMEZONE)
            reminder_time_str = local_reminder_time.strftime("%Y-%m-%d %H:%M:%S")

            response += (
                f"<b>{event_name}</b>\n"
                f"  📅 Event: {event_time}\n"
                f"  🔔 Reminder: {reminder_time_str}\n\n"
            )

    await update.message.reply_html(response)


# 5. Main Execution

def main() -> None:
    """Starts the bot."""

    # Retrieve token from environment variable (Best Practice)
    TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

    # Fallback to a hardcoded example if env var is missing (MUST BE REPLACED!)
    if not TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN environment variable not found. Using example token. REPLACE THIS!")
        TOKEN = "8233665524:AAFEocoUOOELeQBIwPifMTsv_ONLKhtxJ34"

    # Create the Application instance
    application = Application.builder().token(TOKEN).build()

    # ConversationHandler for the multi-step task creation
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("new", new_task)],
        states={
            GET_EVENT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_event_name)],
            GET_EVENT_DATETIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_event_datetime)],
            GET_REMINDER_OFFSET: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_reminder)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # Add all handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("list", list_tasks))
    application.add_handler(conv_handler)

    if not WEBHOOK_DOMAIN:
        logger.error("WEBHOOK_DOMAIN environment variable is not set. Cannot run webhooks.")
        # Fallback to polling for local testing if needed
        # application.run_polling(allowed_updates=Update.ALL_TYPES)
        return

    # Start the bot
    logger.info("Bot started. Waiting for commands...")
    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_URL_PATH,
        webhook_url=f"https://{WEBHOOK_DOMAIN}/{WEBHOOK_URL_PATH}",
        # We don't need SSL certificate files on Render/Heroku as they handle the SSL termination.
    )


if __name__ == "__main__":
    main()