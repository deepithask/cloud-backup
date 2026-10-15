import logging
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from backup_engine import (
    BUCKET_NAME,
    REGION,
    create_backup,
    list_backups,
    restore_backup,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("cloudbackup")

app = FastAPI(title="Cloud Backup Dashboard")
s3 = boto3.client("s3", region_name=REGION)


def aws_error(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        return f"{error.get('Code', 'AWS error')}: {error.get('Message', str(exc))}"
    return f"{type(exc).__name__}: {exc}"


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cloud Backup Dashboard</title>
<style>
*{box-sizing:border-box}body{margin:0;font:15px Arial,sans-serif;background:#f3f6fb;color:#172033}
header{background:#111827;color:#fff;padding:24px 5%}header p{color:#cbd5e1}
main{max-width:1100px;margin:auto;padding:25px 18px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:15px}
.card,section{background:#fff;border-radius:12px;padding:20px;margin-bottom:18px;box-shadow:0 3px 14px #0f172a0c}
.label{color:#64748b;font-size:13px}.number{font-size:25px;font-weight:bold;margin-top:8px;overflow-wrap:anywhere}
button,.file-label,select{padding:11px 15px;border-radius:7px;border:1px solid #cbd5e1;font-size:14px}
button,.file-label{background:#2563eb;color:#fff;border:0;cursor:pointer;display:inline-block;margin:5px 5px 5px 0}
button:disabled{opacity:.6;cursor:wait}.secondary{background:#475569}
#status{white-space:pre-wrap;overflow-wrap:anywhere;background:#f8fafc;padding:14px;border-radius:8px;margin-top:12px}
.ok{color:#166534}.err{color:#b91c1c}.table-wrap{overflow-x:auto}table{border-collapse:collapse;width:100%;min-width:650px}
th,td{padding:12px;text-align:left;border-bottom:1px solid #e2e8f0}th{background:#f8fafc}
small{color:#64748b}ul{padding-left:22px}code{overflow-wrap:anywhere}
</style>
</head>
<body>
<header><h1>☁️ Cloud Backup Dashboard</h1><p>Select files from your computer and back them up to AWS S3.</p></header>
<main>
  <div class="grid">
    <div class="card"><div class="label">Backups found in S3</div><div class="number" id="total">—</div></div>
    <div class="card"><div class="label">Selected files</div><div class="number" id="selectedCount">0</div></div>
    <div class="card"><div class="label">Selected size</div><div class="number" id="selectedSize">0 B</div></div>
  </div>

  <section>
    <h2>1. Select files</h2>
    <p>Choose one or more files from any folder on your computer. The browser sends only the files you select.</p>
    <input id="filePicker" type="file" multiple hidden>
    <label class="file-label" for="filePicker">Select Files</label>
    <button id="clearBtn" class="secondary" type="button">Clear selection</button>
    <ul id="fileList"><li>No files selected.</li></ul>
    <button id="backupBtn" type="button">Start Backup</button>
    <button id="refreshBtn" class="secondary" type="button">Refresh / verify S3</button>
    <div id="status" role="status" aria-live="polite">Select files to begin. No backup has been started.</div>
  </section>

  <section>
    <h2>2. Restore a backup</h2>
    <p>Restored files are saved on the computer running FastAPI, inside <code>restore_data/&lt;backup-id&gt;/</code>.</p>
    <select id="backupSelect" style="width:100%;max-width:100%;margin-bottom:10px">
      <option value="">Loading backups from S3...</option>
    </select>
    <button id="restoreBtn" type="button">Restore selected backup</button>
  </section>

  <section>
    <h2>3. Backup history</h2>
    <small>History is loaded from manifests stored in S3, not from hardcoded sample records.</small>
    <div class="table-wrap" style="margin-top:12px">
      <table><thead><tr><th>Backup ID</th><th>Timestamp (UTC)</th><th>Files</th><th>Size</th><th>S3 location</th><th>Status</th></tr></thead>
      <tbody id="history"><tr><td colspan="6">Loading...</td></tr></tbody></table>
    </div>
  </section>
</main>
<script>
const $ = id => document.getElementById(id);
let selectedFiles = [];

function formatSize(bytes) {
  if (bytes < 1024) return bytes + " B";
  const units = ["KB","MB","GB","TB"]; let n = bytes / 1024, i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return n.toFixed(2) + " " + units[i];
}
function status(message, success=false) {
  $("status").textContent = message;
  $("status").className = success ? "ok" : "err";
}
function renderSelection() {
  $("selectedCount").textContent = selectedFiles.length;
  $("selectedSize").textContent = formatSize(selectedFiles.reduce((n,f)=>n+f.size,0));
  const list = $("fileList"); list.replaceChildren();
  if (!selectedFiles.length) {
    const li = document.createElement("li"); li.textContent = "No files selected."; list.appendChild(li); return;
  }
  selectedFiles.forEach((file,index) => {
    const li = document.createElement("li");
    li.textContent = file.name + " — " + formatSize(file.size);
    list.appendChild(li);
  });
}
$("filePicker").addEventListener("change", event => {
  selectedFiles = Array.from(event.target.files || []);
  renderSelection();
  if (selectedFiles.length) status("Ready to upload " + selectedFiles.length + " selected file(s).");
});
$("clearBtn").addEventListener("click", () => {
  selectedFiles = []; $("filePicker").value = ""; renderSelection();
  status("Selection cleared. No backup was started.", true);
});
$("backupBtn").addEventListener("click", async () => {
  if (!selectedFiles.length) { status("Please click Select Files and choose at least one file."); return; }
  $("backupBtn").disabled = true;
  status("Uploading selected files to AWS S3 and verifying their contents. Please wait...");
  try {
    const form = new FormData();
    selectedFiles.forEach(file => form.append("files", file, file.name));
    const response = await fetch("/backup", {method:"POST", body:form});
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail, null, 2));
    status("BACKUP SUCCESS — uploaded files were verified against S3\n" +
      "Backup ID: " + data.backup_id + "\nFiles uploaded: " + data.file_count +
      "\nTotal size: " + formatSize(data.size_bytes) + "\nS3 location: " + data.s3_uri, true);
    await refreshDashboard();
  } catch (error) { status("BACKUP FAILED\n" + error.message); }
  finally { $("backupBtn").disabled = false; }
});
$("refreshBtn").addEventListener("click", refreshDashboard);
$("restoreBtn").addEventListener("click", async () => {
  const backup_id = $("backupSelect").value;
  if (!backup_id) { status("Select an existing backup first."); return; }
  $("restoreBtn").disabled = true; status("Downloading selected backup from S3...");
  try {
    const response = await fetch("/restore", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({backup_id})
    });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail, null, 2));
    status("RESTORE SUCCESS\nBackup ID: " + data.backup_id + "\nFiles restored: " +
      data.restored_count + "\nTotal size: " + formatSize(data.size_bytes) +
      "\nRestore folder on server: " + data.restore_path, true);
  } catch (error) { status("RESTORE FAILED\n" + error.message); }
  finally { $("restoreBtn").disabled = false; }
});

async function refreshDashboard() {
  try {
    const response = await fetch("/api/backups");
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail));
    const backups = data.backups || [];
    $("total").textContent = backups.length;
    const select = $("backupSelect"); select.replaceChildren();
    const placeholder = document.createElement("option"); placeholder.value = "";
    placeholder.textContent = backups.length ? "Choose a backup" : "No backups found in S3";
    select.appendChild(placeholder);
    const body = $("history"); body.replaceChildren();
    if (!backups.length) {
      const tr = document.createElement("tr"), td = document.createElement("td");
      td.colSpan = 6; td.textContent = "No backup manifests were found in S3."; tr.appendChild(td); body.appendChild(tr);
    }
    backups.forEach(b => {
      const option = document.createElement("option"); option.value = b.backup_id;
      option.textContent = b.backup_id + " · " + b.file_count + " files"; select.appendChild(option);
      const tr = document.createElement("tr");
      [b.backup_id, b.timestamp_utc, String(b.file_count), formatSize(b.size_bytes),
       "s3://" + data.bucket + "/" + b.prefix, b.status].forEach(value => {
        const td = document.createElement("td"); td.textContent = value; tr.appendChild(td);
      });
      body.appendChild(tr);
    });
  } catch (error) { status("Could not load backup history from S3:\n" + error.message); }
}
renderSelection();
refreshDashboard();
</script>
</body>
</html>"""


@app.post("/backup")
async def backup(files: list[UploadFile] = File(...)):
    if not files:
        raise HTTPException(status_code=400, detail="Select at least one file.")

    try:
        result = create_backup(s3, files)
        return result
    except (ClientError, BotoCoreError) as exc:
        logger.exception("AWS backup error")
        raise HTTPException(status_code=502, detail=aws_error(exc)) from exc
    except Exception as exc:
        logger.exception("Backup failed")
        raise HTTPException(
            status_code=500,
            detail=f"Backup failed: {type(exc).__name__}: {exc}",
        ) from exc
    finally:
        for upload in files:
            await upload.close()


@app.get("/api/backups")
def api_backups():
    try:
        return {
            "success": True,
            "bucket": BUCKET_NAME,
            "region": REGION,
            "backups": list_backups(s3),
        }
    except (ClientError, BotoCoreError) as exc:
        logger.exception("Could not list S3 backups")
        raise HTTPException(status_code=502, detail=aws_error(exc)) from exc


class RestoreRequest(BaseModel):
    backup_id: str


@app.post("/restore")
def restore(request: RestoreRequest):
    try:
        return restore_backup(s3, request.backup_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ClientError, BotoCoreError) as exc:
        logger.exception("AWS restore error")
        raise HTTPException(status_code=502, detail=aws_error(exc)) from exc
    except Exception as exc:
        logger.exception("Restore failed")
        raise HTTPException(
            status_code=500,
            detail=f"Restore failed: {type(exc).__name__}: {exc}",
        ) from exc