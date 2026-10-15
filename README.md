# ☁️ Cloud Backup Dashboard

A simple web application to back up files to **AWS S3**, restore previous backups, and track backup history.

## Features
-  Select and upload multiple files
-  Store backups securely in AWS S3
-  Restore selected backups
-  View backup history, file sizes, and timestamps
-  Verify backup availability in S3

## Tech Stack
- **Frontend:** HTML, CSS, JavaScript
- **Backend:** Python, FastAPI
- **Cloud Storage:** AWS S3

## Usage
1. Select files from your computer.
2. Click **Start Backup** to upload files to S3.
3. View backup details in Backup History.
4. Select a backup and click **Restore** to recover files.

## Note
Requires AWS S3 configuration and a running FastAPI backend.

