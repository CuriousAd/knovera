import os
import subprocess
import threading
import time
from pathlib import Path

from core.config import ENABLE_BUCKET_SYNC, HF_BUCKET_URI

_ROOT = Path(__file__).parent.parent
# IMPORTANT: Only sync the static vector database, NEVER user session directories
LOCAL_VECTOR_DB_DIR = _ROOT / "data" / "vector_db"


def restore_from_bucket():
    """Download the vector database from the remote bucket if sync is enabled."""
    if not ENABLE_BUCKET_SYNC or not HF_BUCKET_URI:
        print("[Sync] Remote bucket sync is disabled. Using local vector database.")
        return

    print(f"[Sync] Downloading vector DB from bucket {HF_BUCKET_URI} to {LOCAL_VECTOR_DB_DIR}...")
    LOCAL_VECTOR_DB_DIR.mkdir(parents=True, exist_ok=True)
    
    try:
        subprocess.run(["hf", "sync", HF_BUCKET_URI, str(LOCAL_VECTOR_DB_DIR)], check=True, shell=os.name == 'nt')
        print("[Sync] Restore complete.")
    except subprocess.CalledProcessError as e:
        print(f"[Sync] Restore failed (CLI error): {e}")
    except FileNotFoundError:
        print("[Sync] Note: The 'hf' CLI is not installed or not in PATH. Skipping bucket download.")


def backup_to_bucket():
    """Upload only the static vector DB to the bucket if sync is enabled."""
    if not ENABLE_BUCKET_SYNC or not HF_BUCKET_URI:
        return

    if not LOCAL_VECTOR_DB_DIR.exists():
        return
        
    print(f"[Sync] Backing up {LOCAL_VECTOR_DB_DIR} to {HF_BUCKET_URI}...")
    try:
        subprocess.run(["hf", "sync", str(LOCAL_VECTOR_DB_DIR), HF_BUCKET_URI], check=True, shell=os.name == 'nt')
        print("[Sync] Backup complete.")
    except subprocess.CalledProcessError as e:
        print(f"[Sync] Backup failed (CLI error): {e}")
    except FileNotFoundError:
        print("[Sync] Note: The 'hf' CLI is not installed or not in PATH.")


def start_background_sync(interval_seconds: int = 300):
    """Start periodic background sync only if explicitly enabled."""
    if not ENABLE_BUCKET_SYNC or not HF_BUCKET_URI:
        return

    def _sync_loop():
        print(f"[Sync] Background sync started. Backing up every {interval_seconds}s.")
        while True:
            time.sleep(interval_seconds)
            backup_to_bucket()

    thread = threading.Thread(target=_sync_loop, daemon=True)
    thread.start()

