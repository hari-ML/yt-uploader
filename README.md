# YouTube Bulk Uploader (Google Drive ➔ YouTube)

Automated script to fetch video files and matching thumbnails from Google Drive, apply custom metadata/hashtags, and schedule upload them to YouTube while respecting daily YouTube Data API v3 quotas.

---

## Features
- **Drive to YouTube**: Automatically downloads pending videos from a designated Google Drive folder and uploads them to YouTube.
- **Custom Thumbnails**: Automatically matches thumbnail images (`.jpg`, `.png`, `.webp`) with the same base name and applies them to the video.
- **Scheduled Staggering**: Configurable `publishAt` release scheduling (e.g. 5 uploads/day).
- **Quota & Duplicate Protection**: Tracks already-uploaded drive IDs in `uploaded_log.json` so reruns never duplicate an upload.
- **Optional Email Notifications**: Summary email report after each batch run.

---

## Setup & Installation

### 1. Prerequisites
- Python 3.8+
- A Google Cloud Project with the following APIs enabled:
  - **YouTube Data API v3**
  - **Google Drive API**

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

### 3. OAuth Credentials Setup
1. In Google Cloud Console, set up your OAuth Consent Screen (**External** -> add your Google Account as a **Test User**).
2. Go to **Credentials** -> **Create Credentials** -> **OAuth client ID** -> **Desktop app**.
3. Download the JSON credential file and save it in the project root directory as:
   ```text
   client_secret.json
   ```
   *(See `client_secret.example.json` for structure reference)*

### 4. Configuration
Open `main.py` and customize the `CONFIG` section:
- `DRIVE_FOLDER_ID`: The folder ID from your Google Drive URL.
- `HASHTAGS` & `TAG_KEYWORDS`: Default tags and hashtags added to each upload.
- `PUBLISH_PER_DAY` & `PUBLISH_START_DATE`: Daily release schedule settings.
- `EMAIL_*`: (Optional) Gmail App Password for email status notifications.

---

## Running the Uploader

Run the script locally:
```bash
python main.py
```
- On the first run, a browser window will open for one-time Google OAuth authorization.
- Sign in with the Google Account that has access to the target Google Drive folder.
- A `token.json` file will be created locally to handle automatic token refresh on subsequent runs.
