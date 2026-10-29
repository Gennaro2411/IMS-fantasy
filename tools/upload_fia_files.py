#!/usr/bin/env python3
"""
Uploads the rescued clip files in fia_clips/ to Firebase Storage (fia/<thread>/<file>),
where the IMS Fantasy app plays them. Safe to run again: files already uploaded are skipped.

Run in the folder that has fia_clips/ and serviceAccount.json:
  pip install firebase-admin
  python upload_fia_files.py
"""
import mimetypes, os, re, sys
import firebase_admin
from firebase_admin import credentials, storage

KEY = "serviceAccount.json"
if not os.path.exists(KEY):
    sys.exit("serviceAccount.json not found — put the Firebase key in this folder with exactly that name.")
firebase_admin.initialize_app(credentials.Certificate(KEY), {"storageBucket": "fantasy-ims.firebasestorage.app"})
bucket = storage.bucket()
MEDIA = re.compile(r"\.(mp4|mov|webm|mkv|m4v|png|jpe?g|gif|webp)$", re.I)

up = skip = failed = 0
for folder in sorted(os.listdir("fia_clips")):
    full = os.path.join("fia_clips", folder)
    if not os.path.isdir(full):
        continue
    for fn in sorted(os.listdir(full)):
        if not MEDIA.search(fn):
            continue
        blob = bucket.blob(f"fia/{folder}/{fn}")
        if blob.exists():
            skip += 1; continue
        ct = mimetypes.guess_type(fn)[0] or ("video/mp4" if fn.lower().endswith(".mp4") else "application/octet-stream")
        if fn.lower().endswith(".mov"):
            ct = "video/quicktime"
        blob.chunk_size = 2 * 1024 * 1024  # upload in 2MB pieces so slow connections don't time out
        mb = os.path.getsize(os.path.join(full, fn)) / 1e6
        if mb > 60:  # huge files would burn Firebase's free daily download allowance in a few views
            print(f"  skipped {folder}/{fn} ({mb:.0f} MB) — too big for the app"); continue
        print(f"  uploading {folder}/{fn} ({mb:.0f} MB)…", flush=True)
        for attempt in range(3):
            try:
                blob.upload_from_filename(os.path.join(full, fn), content_type=ct, timeout=600)
                up += 1
                print("    done")
                break
            except Exception as e:
                print(f"    try {attempt + 1} failed: {str(e)[:120]}")
        else:
            failed += 1
print(f"\nDone — uploaded {up}, already there {skip}, failed {failed}." + (" Run it again to retry the failed ones." if failed else ""))
