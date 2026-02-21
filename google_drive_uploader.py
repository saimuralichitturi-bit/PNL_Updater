"""
google_drive_uploader.py
────────────────────────
Uploads PNL CSV files to a Google Drive folder using a service account.

INPUT FILES:
  snapshot_path.txt          ←  written by tradetron_scraper.py
  pnl_YYYY-MM-DD_HH-MM.csv  ←  timestamped snapshot
  pnl_latest.csv             ←  always-latest copy

OUTPUT (Google Drive folder):
  pnl_YYYY-MM-DD_HH-MM.csv  ← new file each run (keeps history)
  pnl_latest.csv             ← updated/overwritten each run
"""

import os
import json
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ── FILE NAMES ─────────────────────────────────────────────────────────────────
SNAPSHOT_PTR_FILE = "snapshot_path.txt"   # ← input: pointer written by tradetron_scraper.py
LATEST_CSV        = "pnl_latest.csv"      # ← input: latest CSV to overwrite on Drive

# ── SECRETS (from GitHub Secrets) ─────────────────────────────────────────────
FOLDER_ID  = os.environ["GOOGLE_DRIVE_FOLDER_ID"]
CREDS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]

SCOPES = ["https://www.googleapis.com/auth/drive"]

# ── Auth ───────────────────────────────────────────────────────────────────────
creds_dict = json.loads(CREDS_JSON)
creds      = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
drive      = build("drive", "v3", credentials=creds)

# ── Read snapshot filename written by tradetron_scraper.py ─────────────────────
with open(SNAPSHOT_PTR_FILE) as f:
    SNAPSHOT_CSV = f.read().strip()   # e.g. pnl_2024-05-01_15-16.csv

print(f"[Uploader] Files to upload:")
print(f"  {SNAPSHOT_CSV:<35} ← new timestamped snapshot")
print(f"  {LATEST_CSV:<35} ← overwrite latest on Drive")
print(f"  Drive Folder ID: {FOLDER_ID}")

# ── Upload helper ──────────────────────────────────────────────────────────────
def upload_to_drive(local_path: str, drive_filename: str, overwrite: bool = False):
    """
    Upload a local file to Google Drive.
    If overwrite=True, replaces an existing file with the same name in the folder.
    """
    media = MediaFileUpload(local_path, mimetype="text/csv", resumable=True)

    if overwrite:
        existing = drive.files().list(
            q=f"name='{drive_filename}' and '{FOLDER_ID}' in parents and trashed=false",
            fields="files(id, name)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True
        ).execute().get("files", [])

        if existing:
            file_id = existing[0]["id"]
            result  = drive.files().update(fileId=file_id, media_body=media,supportsAllDrives=True).execute()
            print(f"[Uploader] ✓ Updated  '{drive_filename}'  (Drive ID: {result['id']})")
            return result["id"]

    # Create brand new file
    metadata = {"name": drive_filename, "parents": [FOLDER_ID]}
    result   = drive.files().create(body=metadata, media_body=media, fields="id",supportsAllDrives=True).execute()
    print(f"[Uploader] ✓ Uploaded '{drive_filename}'  (Drive ID: {result['id']})")
    return result["id"]


# ── Upload ─────────────────────────────────────────────────────────────────────
upload_to_drive(SNAPSHOT_CSV, SNAPSHOT_CSV, overwrite=False)   # always a new file
upload_to_drive(LATEST_CSV,   LATEST_CSV,   overwrite=True)    # replaces previous latest

print("\n[Uploader] Google Drive upload complete.")