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
import firebase_admin
from firebase_admin import credentials, db

# 1. Configuration and Global Storage
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
    """Parses reminder offset, calculates time, schedules the job, and stores it in Firebase."""

    # 1. Parse the offset
    reminder_offset = parse_timedelta(update.message.text)
    if reminder_offset is None:
        await update.message.reply_html(
            "❌ Invalid reminder format. Please use <code>1h</code>, <code>30m</code>, or <code>0m</code>:"
        )
        return GET_REMINDER_OFFSET

    # 2. Calculate reminder time
    event_time: datetime = context.user_data["event_time"]
    reminder_time = event_time - reminder_offset
    chat_id = update.effective_chat.id

    if reminder_time < datetime.now(USER_TIMEZONE):
        await update.message.reply_text("❌ The calculated reminder time has already passed! Please try again.")
        return GET_REMINDER_OFFSET

    # 3. Schedule the reminder and store it
    event_name: str = context.user_data["event_name"]
    utc_reminder_time = reminder_time.astimezone(timezone('UTC'))

    # Sanitize event name for a safe job name
    safe_event_name = "".join(c if c.isalnum() else '_' for c in event_name)
    job_name = f"reminder_{chat_id}_{safe_event_name}_{int(datetime.now().timestamp())}"

    job_data = {
        'chat_id': chat_id,
        'event_name': event_name,
        'event_time_str': event_time.strftime("%Y-%m-%d %H:%M"),
        'reminder_time_utc_iso': utc_reminder_time.isoformat(),
        'job_name': job_name
    }

    # Schedule the job
    context.application.job_queue.run_once(
        callback=send_reminder,
        when=utc_reminder_time,
        data=job_data,
        name=job_name
    )

    # Store in Firebase
    try:
        ref = db.reference(f'jobs/{chat_id}/{job_name}')
        ref.set(job_data)
        logger.info(f"Stored job '{job_name}' in Firebase.")
    except Exception as e:
        logger.error(f"Firebase Error storing job '{job_name}': {e}")
        await update.message.reply_text("⚠️ Reminder scheduled, but failed to save permanently.")

    # 4. Send confirmation
    await update.message.reply_html(
        f"✅ <b>Task scheduled!</b>\n"
        f"Event: <b>{event_name}</b>\n"
        f"Event Time: {event_time.strftime('%Y-%m-%d %H:%M')}\n"
        f"Reminder will arrive: {reminder_time.strftime('%Y-%m-%d %H:%M')}"
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the current conversation."""
    await update.message.reply_text(
        "🚫 Task creation cancelled. You can start over with /new."
    )
    return ConversationHandler.END


# 4. Reminder and Listing Functionality

async def send_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends the reminder and removes the job from Firebase."""
    job_data = context.job.data
    chat_id = job_data['chat_id']
    job_name = job_data.get('job_name')

    # Remove from Firebase
    if job_name:
        try:
            db.reference(f'jobs/{chat_id}/{job_name}').delete()
            logger.info(f"Deleted job '{job_name}' from Firebase.")
        except Exception as e:
            logger.error(f"Firebase Error deleting job '{job_name}': {e}")

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🔔 <b>REMINDER!</b>\n"
             f"The event <b>'{job_data['event_name']}'</b> is scheduled for {job_data['event_time_str']}.",
        parse_mode="HTML"
    )
    logger.info(f"Sent reminder for chat {chat_id} about event '{job_data['event_name']}'.")


async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays all scheduled tasks for the user from Firebase."""
    chat_id = update.effective_chat.id

    try:
        jobs_ref = db.reference(f'jobs/{chat_id}')
        jobs = jobs_ref.get()
    except Exception as e:
        logger.error(f"Firebase Error fetching jobs for chat {chat_id}: {e}")
        await update.message.reply_text("❌ Could not retrieve your tasks.")
        return

    if not jobs:
        await update.message.reply_text("🚫 You have no scheduled tasks. Create one using /new.")
        return

    response = "🗓️ <b>Your Scheduled Tasks:</b>\n\n"
    sorted_jobs = sorted(jobs.values(), key=lambda j: j.get('reminder_time_utc_iso', ''))

    for job in sorted_jobs:
        event_name = job.get('event_name', 'N/A')
        event_time = job.get('event_time_str', 'N/A')

        try:
            utc_reminder = datetime.fromisoformat(job['reminder_time_utc_iso'])
            local_reminder = utc_reminder.astimezone(USER_TIMEZONE)
            reminder_time_str = local_reminder.strftime("%Y-%m-%d %H:%M:%S")
        except (KeyError, ValueError):
            reminder_time_str = "Invalid date"

        response += (
            f"<b>{event_name}</b>\n"
            f"  📅 Event: {event_time}\n"
            f"  🔔 Reminder: {reminder_time_str}\n\n"
        )

    await update.message.reply_html(response)


# 5. Main Execution
def reschedule_jobs(application: Application) -> None:
    """Reschedules all jobs from Firebase that are still in the future."""
    try:
        all_jobs_ref = db.reference('jobs')
        all_jobs = all_jobs_ref.get()

        if not all_jobs:
            logger.info("No jobs in Firebase to reschedule.")
            return

        for chat_id, jobs in all_jobs.items():
            for job_name, job_data in jobs.items():
                utc_reminder_time = datetime.fromisoformat(job_data['reminder_time_utc_iso'])

                # Reschedule if the job is in the future
                if utc_reminder_time > datetime.now(timezone('UTC')):
                    application.job_queue.run_once(
                        callback=send_reminder,
                        when=utc_reminder_time,
                        data=job_data,
                        name=job_name
                    )
                    logger.info(f"Rescheduled job '{job_name}' for chat {chat_id}.")
                else:
                    # If the job is in the past, remove it
                    db.reference(f'jobs/{chat_id}/{job_name}').delete()
                    logger.info(f"Removed past job '{job_name}' for chat {chat_id}.")

    except Exception as e:
        logger.error(f"Firebase Error during job rescheduling: {e}")

def main() -> None:
    """Starts the bot."""
    # Initialize Firebase
    cred = credentials.Certificate(os.environ.get("FIREBASE_SERVICE_ACCOUNT_KEY_PATH"))
    firebase_admin.initialize_app(cred, {
        'databaseURL': os.environ.get("FIREBASE_DATABASE_URL")
    })
    # Retrieve token from environment variable (Best Practice)
    TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

    # Fallback to a hardcoded example if env var is missing (MUST BE REPLACED!)
    if not TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN environment variable not found. Using example token. REPLACE THIS!")
        TOKEN = "8233665524:AAFEocoUOOELeQBIwPifMTsv_ONLKhtxJ34"

    # Create the Application instance
    application = Application.builder().token(TOKEN).build()
    reschedule_jobs(application)

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