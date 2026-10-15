import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from botocore.exceptions import ClientError

BUCKET_NAME = "cfps12-cloud-backup-deepitha-2026"
REGION = "ap-south-1"
BACKUP_ROOT = "backups/"
MANIFEST_NAME = "manifest.json"
RESTORE_DIR = Path(__file__).resolve().parent / "restore_data"


def _sha256_stream(stream):
    digest = hashlib.sha256()
    size = 0
    stream.seek(0)
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
        size += len(chunk)
    stream.seek(0)
    return digest.hexdigest(), size


def _valid_backup_id(backup_id):
    if not isinstance(backup_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,80}", backup_id
    ):
        raise ValueError("Invalid backup ID.")


def _read_manifest(s3, key):
    response = s3.get_object(Bucket=BUCKET_NAME, Key=key)
    try:
        raw = response["Body"].read()
    finally:
        response["Body"].close()
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict) or not data.get("backup_id"):
        raise ValueError(f"Invalid backup manifest at {key}")
    return data


def create_backup(s3, uploads):
    """Upload selected FastAPI UploadFile objects and verify each S3 object."""
    if not uploads:
        raise ValueError("No files were selected.")

    backup_id = (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + uuid.uuid4().hex[:8]
    )
    prefix = f"{BACKUP_ROOT}{backup_id}/"
    uploaded = []
    total_size = 0

    try:
        for index, upload in enumerate(uploads, start=1):
            original_name = (upload.filename or "").replace("\\", "/")
            name = PurePosixPath(original_name).name
            if not name or name in {".", ".."}:
                raise ValueError("A selected file has an invalid filename.")

            stream = upload.file
            original_hash, size = _sha256_stream(stream)
            key = f"{prefix}files/{index:04d}_{name}"

            extra_args = {"ContentType": upload.content_type or "application/octet-stream"}
            s3.upload_fileobj(stream, BUCKET_NAME, key, ExtraArgs=extra_args)

            head = s3.head_object(Bucket=BUCKET_NAME, Key=key)
            if head.get("ContentLength") != size:
                raise RuntimeError(f"Uploaded size verification failed: {name}")

            remote = s3.get_object(Bucket=BUCKET_NAME, Key=key)
            remote_digest = hashlib.sha256()
            remote_size = 0
            try:
                for chunk in iter(
                    lambda: remote["Body"].read(1024 * 1024), b""
                ):
                    remote_digest.update(chunk)
                    remote_size += len(chunk)
            finally:
                remote["Body"].close()

            if remote_digest.hexdigest() != original_hash or remote_size != size:
                raise RuntimeError(f"S3 content verification failed: {name}")

            uploaded.append({
                "original_name": name,
                "s3_key": key,
                "size_bytes": size,
                "sha256": original_hash,
                "verified": True,
            })
            total_size += size

        manifest = {
            "backup_id": backup_id,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "bucket": BUCKET_NAME,
            "region": REGION,
            "prefix": prefix,
            "file_count": len(uploaded),
            "size_bytes": total_size,
            "verified": True,
            "files": uploaded,
        }

        # The manifest is written only after every selected file passes verification.
        s3.put_object(
            Bucket=BUCKET_NAME,
            Key=prefix + MANIFEST_NAME,
            Body=json.dumps(manifest, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
        # Verify that the manifest itself is present and readable.
        saved_manifest = _read_manifest(s3, prefix + MANIFEST_NAME)
        if saved_manifest.get("backup_id") != backup_id:
            raise RuntimeError("S3 manifest verification failed.")

        return {
            "success": True,
            "message": "All selected files uploaded and verified against S3.",
            **manifest,
            "s3_uri": f"s3://{BUCKET_NAME}/{prefix}",
        }

    except Exception:
        # Do not report success if an upload or verification failed.
        # Any objects uploaded before the failure may remain in S3.
        raise


def list_backups(s3):
    """Discover backups using real S3 prefixes and their stored manifests."""
    paginator = s3.get_paginator("list_objects_v2")
    prefixes = set()

    for page in paginator.paginate(Bucket=BUCKET_NAME, Prefix=BACKUP_ROOT, Delimiter="/"):
        for item in page.get("CommonPrefixes", []):
            prefix = item.get("Prefix")
            if prefix:
                prefixes.add(prefix)

    backups = []
    for prefix in sorted(prefixes, reverse=True):
        manifest_key = prefix + MANIFEST_NAME
        try:
            manifest = _read_manifest(s3, manifest_key)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in {"NoSuchKey", "404", "NotFound"}:
                # Ignore incomplete prefixes without a manifest.
                continue
            raise

        if manifest.get("bucket") != BUCKET_NAME:
            continue

        files = manifest.get("files", [])
        # A backup is listed only if its manifest's objects still exist in S3.
        actual_files = []
        for entry in files:
            key = entry.get("s3_key")
            if not key or not key.startswith(prefix + "files/"):
                continue
            try:
                head = s3.head_object(Bucket=BUCKET_NAME, Key=key)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code in {"NoSuchKey", "404", "NotFound"}:
                    continue
                raise
            actual_files.append(entry)

        if not actual_files:
            continue

        backups.append({
            "backup_id": manifest["backup_id"],
            "timestamp_utc": manifest.get("timestamp_utc", ""),
            "prefix": prefix,
            "file_count": len(actual_files),
            "size_bytes": sum(item.get("size_bytes", 0) for item in actual_files),
            "status": "Verified in S3" if all(x.get("verified") for x in actual_files) else "Present in S3",
        })

    return backups


def restore_backup(s3, backup_id):
    """Download the selected backup's manifest-listed files to restore_data/id/."""
    _valid_backup_id(backup_id)
    prefix = f"{BACKUP_ROOT}{backup_id}/"
    manifest_key = prefix + MANIFEST_NAME

    try:
        manifest = _read_manifest(s3, manifest_key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"NoSuchKey", "404", "NotFound"}:
            raise FileNotFoundError(
                f"No backup manifest found in S3 for backup ID {backup_id}."
            ) from exc
        raise

    if manifest.get("backup_id") != backup_id or manifest.get("bucket") != BUCKET_NAME:
        raise ValueError("Backup manifest does not match the requested backup.")
    files = manifest.get("files", [])
    if not files:
        raise FileNotFoundError("This backup contains no files.")

    destination_root = (RESTORE_DIR / backup_id).resolve()
    destination_root.mkdir(parents=True, exist_ok=True)
    restored = []
    total_size = 0

    for entry in files:
        key = entry.get("s3_key", "")
        if not key.startswith(prefix + "files/"):
            raise ValueError("Manifest contains an invalid S3 key.")

        name = entry.get("original_name", "")
        safe_name = PurePosixPath(name.replace("\\", "/")).name
        if not safe_name or safe_name in {".", ".."}:
            raise ValueError("Manifest contains an invalid filename.")

        # Prefix each restored filename with its upload index to avoid collisions.
        stored_name = key.rsplit("/", 1)[-1]
        safe_stored_name = PurePosixPath(stored_name).name
        destination = (destination_root / safe_stored_name).resolve()
        if not destination.is_relative_to(destination_root):
            raise ValueError("Unsafe restore destination.")

        response = s3.get_object(Bucket=BUCKET_NAME, Key=key)
        digest = hashlib.sha256()
        size = 0
        try:
            with destination.open("wb") as output:
                for chunk in iter(
                    lambda: response["Body"].read(1024 * 1024), b""
                ):
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
        finally:
            response["Body"].close()

        if size != entry.get("size_bytes") or digest.hexdigest() != entry.get("sha256"):
            destination.unlink(missing_ok=True)
            raise RuntimeError(f"Restore verification failed for {safe_name}.")

        restored.append({
            "original_name": safe_name,
            "restored_file": str(destination),
            "size_bytes": size,
            "verified": True,
        })
        total_size += size

    return {
        "success": True,
        "message": "Files downloaded from S3 and verified.",
        "backup_id": backup_id,
        "restored_count": len(restored),
        "size_bytes": total_size,
        "restore_path": str(destination_root),
        "files": restored,
    }