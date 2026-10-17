"""
YouTube Bulk Uploader — Google Drive -> YouTube

Pulls videos from a Drive folder, appends a fixed hashtag block to each
description, and uploads them as PRIVATE with a scheduled publishAt date so
YouTube auto-releases PUBLISH_PER_DAY of them per day on its own.

If a video has a matching image in the same Drive folder (same filename,
different extension — e.g. ep12.mp4 + ep12.webp), that image is uploaded as
the video's custom thumbnail right after the video itself is inserted.

Upload calls are still paced to stay under the YouTube Data API default
quota: 10,000 units/day, 1600 units per upload -> max ~6 uploads/day.
Setting a thumbnail costs another 50 units per video, which is negligible
at this volume. That cap applies no matter when the video is scheduled to
go public, so this still needs to run once per day (cron / Task Scheduler)
— it cannot upload everything in one sitting. Each run uploads the next
UPLOADS_PER_RUN videos and remembers what's already done, so reruns never
duplicate an upload.

SETUP
1. pip install -r requirements.txt
2. Google Cloud Console -> enable "YouTube Data API v3" and "Google Drive API"
3. OAuth consent screen -> External -> Testing -> add your own Google account
   as a test user
4. Credentials -> Create OAuth client ID -> Desktop app -> download JSON,
   save it next to this script as client_secret.json
5. Edit the CONFIG block below
6. IMPORTANT for thumbnails: custom thumbnails require the channel to be
   phone-verified. Check youtube.com/features (or Studio -> Settings ->
   Channel -> Feature eligibility). If it's not verified, thumbnails().set()
   will fail with a 403 for every video, though the video upload itself
   still succeeds.
7. First run: `python main.py` — a browser window opens for one-time
   login/consent. WHEN THE ACCOUNT PICKER APPEARS, pick the Google account
   that actually owns DRIVE_FOLDER_ID below — not just whichever account
   is default in your browser. This is the #1 cause of the 404 error on
   startup (token authorized for the wrong account can't see the folder).
   Successful login caches a token.json for future runs.
8. Schedule it daily (see setup steps for the exact cron / Task Scheduler line)
"""

import io
import json
import os
import smtplib
import time
from datetime import datetime, timedelta
from email.mime.text import MIMEText

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from googleapiclient.errors import HttpError

# ---------------- CONFIG — edit these ----------------
DRIVE_FOLDER_ID = "1TJbVrQwEhCQiZ6M1G4OPf4q3s8FSpNQ-"
HASHTAGS = ["#top5", "#ranking", "#countdown", "#shorts", "#top5moments",
            "#bestmoments", "#epicmoments", "#movieclips", "#tvmoments", "#viral",
            "#trending", "#fyp", "#movies", "#tvshows", "#compilation"]
TAG_KEYWORDS = ["top 5", "ranking", "countdown"]      # backend search tags, not shown on video
UPLOADS_PER_RUN = 5                                   # inserts per run — quota caps this at ~6/day, don't go higher
CATEGORY_ID = "24"                                    # 24 = Entertainment
MADE_FOR_KIDS = False

# Custom thumbnails: matched by filename, e.g. ep12.mp4 + ep12.webp/.jpg/.png
# in the SAME Drive folder as the video. Set to False to skip entirely.
ENABLE_CUSTOM_THUMBNAILS = True
THUMBNAIL_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
THUMBNAIL_MAX_BYTES = 2 * 1024 * 1024   # YouTube's hard limit is 2MB

# Scheduled release: every upload is set PRIVATE with a publishAt date, so
# YouTube itself makes PUBLISH_PER_DAY of them public per day automatically.
PRIVACY_STATUS = "private"
PUBLISH_PER_DAY = 5
PUBLISH_START_DATE = "2026-08-31"   # YYYY-MM-DD — first day anything goes public
PUBLISH_TIME_IST = "18:00"          # HH:MM 24-hour, India time

# Email a summary after each run (needs a Gmail App Password — 16 chars,
# generated at myaccount.google.com/apppasswords, NOT your normal password).
EMAIL_ENABLED = False
EMAIL_ADDRESS = "your.email@gmail.com"
EMAIL_APP_PASSWORD = "PASTE_16_CHAR_APP_PASSWORD"
EMAIL_TO = "your.email@gmail.com"
# ------------------------------------------------------

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/drive.readonly",
]
CLIENT_SECRET_FILE = "client_secret.json"
TOKEN_FILE = "token.json"
TRACKING_FILE = "uploaded_log.json"
TEMP_DIR = "temp_downloads"


class FolderAccessError(Exception):
    """Raised when the authenticated account can't see DRIVE_FOLDER_ID."""


def get_credentials():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return creds


def verify_account_and_folder(drive):
    """
    Fails fast with a clear message instead of the cryptic
    'File not found: .' 404 that files().list() throws on a bad folder ID.
    Prints which Google account is authenticated and confirms it can see
    DRIVE_FOLDER_ID before any download/upload work starts.
    """
    about = drive.about().get(fields="user").execute()
    email = about["user"]["emailAddress"]
    print(f"Authenticated as: {email}")

    try:
        folder = drive.files().get(
            fileId=DRIVE_FOLDER_ID, fields="id,name,trashed,mimeType"
        ).execute()
    except HttpError as e:
        raise FolderAccessError(
            f"Account {email} cannot see folder {DRIVE_FOLDER_ID}.\n"
            f"Either this account doesn't have access, or the folder was "
            f"deleted/moved. Delete token.json and rerun, and when the "
            f"browser account picker appears, choose the Google account "
            f"that actually owns this folder.\nOriginal error: {e}"
        ) from e

    if folder.get("mimeType") != "application/vnd.google-apps.folder":
        raise FolderAccessError(f"{DRIVE_FOLDER_ID} is not a folder (found: {folder.get('name')}).")
    if folder.get("trashed"):
        raise FolderAccessError(f"Folder '{folder['name']}' ({DRIVE_FOLDER_ID}) is in the trash. Restore it first.")

    print(f"Folder OK: '{folder['name']}'")


def load_uploaded():
    if os.path.exists(TRACKING_FILE):
        with open(TRACKING_FILE, "r") as f:
            return set(json.load(f))
    return set()


def save_uploaded(uploaded_set):
    with open(TRACKING_FILE, "w") as f:
        json.dump(sorted(uploaded_set), f, indent=2)


def _list_drive_files(drive, folder_id, mime_prefix):
    query = f"'{folder_id}' in parents and mimeType contains '{mime_prefix}/' and trashed = false"
    files, page_token = [], None
    while True:
        resp = drive.files().list(
            q=query,
            fields="nextPageToken, files(id, name)",
            orderBy="name",
            pageToken=page_token,
        ).execute()
        files.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return files


def list_drive_videos(drive, folder_id):
    return _list_drive_files(drive, folder_id, "video")


def list_drive_images(drive, folder_id):
    """Same folder, image files only — used to find thumbnail matches."""
    return _list_drive_files(drive, folder_id, "image")


def find_matching_thumbnail(video_filename, image_files):
    """Match by base filename, e.g. ep12.mp4 -> ep12.webp / .jpg / .png."""
    base_name = os.path.splitext(video_filename)[0]
    for f in image_files:
        name_no_ext, ext = os.path.splitext(f["name"])
        if name_no_ext == base_name and ext.lower() in THUMBNAIL_EXTENSIONS:
            return f
    return None


def download_file(drive, file_id, dest_path, max_attempts=3):
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    for attempt in range(1, max_attempts + 1):
        try:
            request = drive.files().get_media(fileId=file_id)
            with io.FileIO(dest_path, "wb") as fh:
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
            return
        except Exception as e:
            print(f"Download attempt {attempt}/{max_attempts} failed: {e}")
            if attempt == max_attempts:
                raise
            time.sleep(5 * attempt)


def build_description(title):
    return f"{title}\n\n{' '.join(HASHTAGS)}"


def compute_publish_at(schedule_index):
    """publishAt for the video at this position in the overall upload order."""
    day_offset = schedule_index // PUBLISH_PER_DAY
    start = datetime.strptime(PUBLISH_START_DATE, "%Y-%m-%d")
    hour, minute = (int(x) for x in PUBLISH_TIME_IST.split(":"))
    publish_local = start + timedelta(days=day_offset, hours=hour, minutes=minute)
    publish_utc = publish_local - timedelta(hours=5, minutes=30)  # IST -> UTC
    return publish_utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def upload_video(youtube, filepath, title, schedule_index):
    body = {
        "snippet": {
            "title": title[:100],
            "description": build_description(title),
            "tags": TAG_KEYWORDS,
            "categoryId": CATEGORY_ID,
        },
        "status": {
            "privacyStatus": PRIVACY_STATUS,
            "publishAt": compute_publish_at(schedule_index),
            "selfDeclaredMadeForKids": MADE_FOR_KIDS,
        },
    }
    media = MediaFileUpload(filepath, chunksize=-1, resumable=True, mimetype="video/*")
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        _, response = request.next_chunk()
    return response["id"]


def set_thumbnail(youtube, video_id, thumbnail_path):
    youtube.thumbnails().set(
        videoId=video_id,
        media_body=MediaFileUpload(thumbnail_path)
    ).execute()


def send_summary_email(subject, body):
    if not EMAIL_ENABLED:
        return
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = EMAIL_ADDRESS
        msg["To"] = EMAIL_TO
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(EMAIL_ADDRESS, EMAIL_APP_PASSWORD)
            server.send_message(msg)
    except Exception as e:
        print(f"Email notification failed: {e}")


def main():
    creds = get_credentials()
    drive = build("drive", "v3", credentials=creds)
    youtube = build("youtube", "v3", credentials=creds)

    try:
        verify_account_and_folder(drive)
    except FolderAccessError as e:
        print(f"\nSTOPPED: {e}\n")
        return

    all_videos = list_drive_videos(drive, DRIVE_FOLDER_ID)
    all_images = list_drive_images(drive, DRIVE_FOLDER_ID) if ENABLE_CUSTOM_THUMBNAILS else []
    uploaded = load_uploaded()
    pending = [v for v in all_videos if v["id"] not in uploaded]

    if not pending:
        print("Nothing left to upload.")
        send_summary_email("YouTube uploader: all done", "All videos have been uploaded. Nothing left to process.")
        return

    batch = pending[:UPLOADS_PER_RUN]
    os.makedirs(TEMP_DIR, exist_ok=True)
    summary_lines = []

    for video in batch:
        title = os.path.splitext(video["name"])[0]
        local_path = os.path.join(TEMP_DIR, video["name"])
        schedule_index = len(uploaded)
        publish_at = compute_publish_at(schedule_index)
        video_id = None
        try:
            print(f"Downloading {video['name']}...")
            download_file(drive, video["id"], local_path)

            print(f"Uploading {title} (publishes {publish_at})...")
            video_id = upload_video(youtube, local_path, title, schedule_index)
            print(f"Scheduled: https://youtube.com/watch?v={video_id}")
            uploaded.add(video["id"])
            summary_lines.append(f"OK   {title} -> publishes {publish_at} -> https://youtube.com/watch?v={video_id}")
        except Exception as e:
            print(f"Failed on {video['name']}: {e}")
            summary_lines.append(f"FAIL {video['name']}: {e}")
        finally:
            if os.path.exists(local_path):
                os.remove(local_path)

        # Thumbnail is set only after a successful video upload, and never
        # blocks the run — a failed thumbnail just gets logged.
        if video_id and ENABLE_CUSTOM_THUMBNAILS:
            thumb_file = find_matching_thumbnail(video["name"], all_images)
            if not thumb_file:
                print(f"No matching thumbnail for {video['name']}, using YouTube's auto-generated one.")
                summary_lines.append(f"     no thumbnail found for {video['name']}")
            else:
                thumb_local_path = os.path.join(TEMP_DIR, thumb_file["name"])
                try:
                    download_file(drive, thumb_file["id"], thumb_local_path)
                    if os.path.getsize(thumb_local_path) > THUMBNAIL_MAX_BYTES:
                        print(f"Thumbnail {thumb_file['name']} exceeds 2MB, skipping.")
                        summary_lines.append(f"     thumbnail too large, skipped: {thumb_file['name']}")
                    else:
                        set_thumbnail(youtube, video_id, thumb_local_path)
                        print(f"Thumbnail set from {thumb_file['name']}.")
                        summary_lines.append(f"     thumbnail set: {thumb_file['name']}")
                except HttpError as e:
                    print(f"Thumbnail set failed for {video['name']}: {e}")
                    summary_lines.append(f"     thumbnail FAILED for {video['name']}: {e}")
                except Exception as e:
                    print(f"Thumbnail download/set error for {video['name']}: {e}")
                    summary_lines.append(f"     thumbnail FAILED for {video['name']}: {e}")
                finally:
                    if os.path.exists(thumb_local_path):
                        os.remove(thumb_local_path)

        save_uploaded(uploaded)

    remaining = len(pending) - len(batch)
    days_left = -(-remaining // UPLOADS_PER_RUN)  # ceil division
    footer = f"{remaining} videos left, ~{days_left} more days at {UPLOADS_PER_RUN}/day."
    print(footer)

    ok_count = sum(1 for line in summary_lines if line.startswith("OK"))
    fail_count = sum(1 for line in summary_lines if "FAIL" in line)
    subject = f"YouTube uploader: {ok_count} uploaded" + (f", {fail_count} failed" if fail_count else "")
    body = "\n".join(summary_lines) + "\n\n" + footer
    send_summary_email(subject, body)


if __name__ == "__main__":
    main()