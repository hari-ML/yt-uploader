from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

creds = Credentials.from_authorized_user_file("token.json", [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/drive.readonly",
])
drive = build("drive", "v3", credentials=creds)

results = drive.files().list(
    pageSize=10,
    fields="files(id, name, owners(emailAddress), parents)"
).execute()
for f in results.get("files", []):
    print(f)