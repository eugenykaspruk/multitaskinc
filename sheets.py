import gspread
from google.oauth2.service_account import Credentials
import os
import json
from typing import List, Dict, Union

# 1. Configuration and Authentication
SERVICE_ACCOUNT_FILE = 'gcreds.json'
SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive.file'
]

# Load credentials from environment variable
gcreds_json_str = os.environ.get('GCREDS_JSON')
if not gcreds_json_str:
    raise ValueError("GCREDS_JSON environment variable not set.")

# Parse the JSON string into a dictionary
gcreds_dict = json.loads(gcreds_json_str)

# Create credentials from the dictionary
creds = Credentials.from_service_account_info(gcreds_dict, scopes=SCOPES)
gc = gspread.authorize(creds)

# Open the spreadsheet
spreadsheet_url = os.environ.get('SPREADSHEET_URL')
if not spreadsheet_url:
    raise ValueError("SPREADSHEET_URL environment variable not set.")
spreadsheet = gc.open_by_url(spreadsheet_url)
worksheet = spreadsheet.sheet1

# 2. Column Definitions
COLUMNS = [
    "chat_id", "job_name", "event_name", "event_time_str",
    "reminder_time_utc_iso", "status"
]


def setup_columns():
    """Ensures the Google Sheet has the correct column headers."""
    existing_headers = worksheet.row_values(1)
    if existing_headers != COLUMNS:
        worksheet.clear()
        worksheet.append_row(COLUMNS)


# 3. Core Functions
def add_job(job_data: Dict[str, Union[str, int]]):
    """Adds a new job to the spreadsheet with 'scheduled' status."""
    job_data['status'] = 'scheduled'  # Set initial status
    # Ensure all columns are present, filling with None if missing
    row_to_add = [job_data.get(col) for col in COLUMNS]
    worksheet.append_row(row_to_add)


def get_all_jobs() -> List[Dict]:
    """Retrieves all jobs from the spreadsheet."""
    records = worksheet.get_all_records()
    return records


def update_job_status(job_name: str, new_status: str):
    """Updates the status of a job (e.g., to 'sent')."""
    try:
        cell = worksheet.find(job_name, in_column=COLUMNS.index("job_name") + 1)
        # Update the 'status' column for that row
        worksheet.update_cell(cell.row, COLUMNS.index("status") + 1, new_status)
    except gspread.exceptions.CellNotFound:
        print(f"Warning: Job '{job_name}' not found in spreadsheet for status update.")


def delete_job(job_name: str):
    """Deletes a job from the spreadsheet."""
    try:
        cell = worksheet.find(job_name, in_column=COLUMNS.index("job_name") + 1)
        worksheet.delete_rows(cell.row)
    except gspread.exceptions.CellNotFound:
        print(f"Warning: Job '{job_name}' not found for deletion.")

# Ensure the columns are set up on module import
setup_columns()
