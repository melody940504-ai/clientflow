# ClientFlow MVP

A lightweight client review and delivery dashboard for short-form video studios.

## Features

- Register / login / logout
- Client management
- Project management
- Video version links instead of storing large video files
- Feedback comments
- Status tracking: Awaiting Review, In Revision, Approved, Published
- Category filtering
- Responsive mobile layout
- Light / dark mode toggle
- SQLite database

## Run locally

```bash
cd clientflow_mvp
python -m venv .venv
# Windows PowerShell:
.venv\Scripts\Activate.ps1
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open:

```text
http://127.0.0.1:8000
```

## MVP note

This version intentionally does not store video files. Paste external links from Google Drive, Dropbox, YouTube unlisted, Frame.io, Catbox, etc. This keeps hosting costs low.
