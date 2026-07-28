#!/usr/bin/env python3
"""
Fetch every IMS driver's Discord avatar and write it into their Firestore driver doc.

Runs SERVER-SIDE (alongside the IMS bot) — it needs the bot token, which must never be
placed in the public fantasy web app. The fantasy app then reads the `discordAvatar` field
you write here; no app-side fetching required.

What it does:
  1. Collect (driver_name, discord_id) pairs — from the bible DB by default, or from the
     existing Firestore driver docs if they already carry discordId.
  2. For each discord_id, call the Discord API (GET /users/{id}) to get the avatar hash.
  3. Build the CDN URL and PATCH it into that driver's Firestore doc as `discordAvatar`.

Usage:
  export DISCORD_BOT_TOKEN="xxxxx"          # the IMS bot token (keep secret!)
  export FIRESTORE_PROJECT_ID="fantasy-ims"
  export FIRESTORE_API_KEY="AIza...."       # same web API key the app uses
  python3 fetch_driver_avatars.py --bible ims_bible_snapshot.db
  # or, if driver docs already have discordId stored:
  python3 fetch_driver_avatars.py --from-firestore

Notes:
  * Users with NO custom avatar return a null hash — we SKIP them so the app falls back to its
    initials disc instead of storing Discord's generic default logo.
  * Discord rate-limits ~ a few requests/sec; we sleep and honour 429 Retry-After.
  * Idempotent: re-running just refreshes URLs (avatar hash changes when a user changes pfp).
"""

import argparse
import os
import sqlite3
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
import json

DISCORD_API = "https://discord.com/api/v10"
CDN = "https://cdn.discordapp.com"


def _req(url, method="GET", headers=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode() or "{}")
        except Exception:
            payload = {}
        return e.code, payload


# ---- Discord ----------------------------------------------------------------

def fetch_avatar_url(discord_id, token, size=256):
    """Return the CDN avatar URL for a user id, or None if they have no custom avatar."""
    headers = {"Authorization": f"Bot {token}"}
    url = f"{DISCORD_API}/users/{discord_id}"
    for attempt in range(5):
        status, data = _req(url, headers=headers)
        if status == 200:
            avatar_hash = data.get("avatar")
            if not avatar_hash:
                return None  # no custom avatar -> let the app show initials
            ext = "gif" if str(avatar_hash).startswith("a_") else "png"
            return f"{CDN}/avatars/{discord_id}/{avatar_hash}.{ext}?size={size}"
        if status == 429:
            retry = float(data.get("retry_after", 1.0)) + 0.25
            time.sleep(retry)
            continue
        if status == 404:
            return None  # user no longer exists / left
        # transient — back off and retry
        time.sleep(1.0 + attempt)
    print(f"  ! gave up on {discord_id} (last status {status})", file=sys.stderr)
    return None


# ---- Firestore (REST, same project the app uses) ----------------------------

def fs_base(project):
    return f"https://firestore.googleapis.com/v1/projects/{project}/databases/(default)/documents"


def fs_list_drivers(project, api_key):
    """Return list of (doc_id, name, discord_id, has_admin_photo)."""
    out = []
    page_token = None
    while True:
        url = f"{fs_base(project)}/drivers?key={api_key}&pageSize=300"
        if page_token:
            url += f"&pageToken={page_token}"
        status, data = _req(url)
        if status != 200:
            print(f"Firestore list failed ({status}): {data}", file=sys.stderr)
            break
        for doc in data.get("documents", []):
            doc_id = doc["name"].split("/")[-1]
            f = doc.get("fields", {})
            name = f.get("name", {}).get("stringValue", "")
            did = f.get("discordId", {}).get("stringValue", "")
            photo = f.get("photo", {}).get("stringValue", "")
            out.append((doc_id, name, did, bool(photo)))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return out


def fs_write_avatar(project, api_key, doc_id, url):
    patch = (f"{fs_base(project)}/drivers/{doc_id}"
             f"?key={api_key}&updateMask.fieldPaths=discordAvatar")
    body = {"fields": {"discordAvatar": {"stringValue": url}}}
    status, data = _req(patch, method="PATCH", body=body,
                        headers={"Content-Type": "application/json"})
    return status == 200


# ---- name matching (bible DB -> firestore) ----------------------------------

def norm(name):
    if not name:
        return ""
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return "".join(c for c in name.lower() if c.isalnum())


def ids_from_bible(db_path):
    """Return {normalised_name: discord_id} from the bible DB's roster_seats + contracts."""
    c = sqlite3.connect(db_path)
    mapping = {}
    # occupied seats (current drivers) are the primary source
    for alias, did in c.execute(
        "SELECT driver_alias, driver_discord_id FROM roster_seats "
        "WHERE driver_discord_id IS NOT NULL AND driver_alias IS NOT NULL"
    ):
        if alias and did:
            mapping.setdefault(norm(alias), str(did))
    # contracts fill in anyone not currently seated
    for alias, did in c.execute(
        "SELECT driver_alias, driver_discord_id FROM contracts "
        "WHERE driver_discord_id IS NOT NULL AND driver_alias IS NOT NULL"
    ):
        if alias and did:
            mapping.setdefault(norm(alias), str(did))
    c.close()
    return mapping


# ---- main -------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bible", help="path to the IMS bible .db (source of discord ids)")
    ap.add_argument("--from-firestore", action="store_true",
                    help="use discordId already stored on driver docs instead of the bible DB")
    ap.add_argument("--overwrite-admin", action="store_true",
                    help="also set avatars for drivers who have an admin-set photo (default: skip)")
    ap.add_argument("--size", type=int, default=256)
    args = ap.parse_args()

    token = os.environ.get("DISCORD_BOT_TOKEN")
    project = os.environ.get("FIRESTORE_PROJECT_ID", "fantasy-ims")
    api_key = os.environ.get("FIRESTORE_API_KEY")
    if not token or not api_key:
        sys.exit("Set DISCORD_BOT_TOKEN and FIRESTORE_API_KEY environment variables.")

    drivers = fs_list_drivers(project, api_key)
    print(f"{len(drivers)} driver docs in Firestore.")

    bible_map = {}
    if args.bible:
        bible_map = ids_from_bible(args.bible)
        print(f"{len(bible_map)} discord ids from bible DB.")
    elif not args.from_firestore:
        sys.exit("Pass --bible <path> or --from-firestore to say where discord ids come from.")

    updated = skipped_photo = no_id = no_avatar = failed = 0
    for doc_id, name, did_fs, has_photo in drivers:
        if has_photo and not args.overwrite_admin:
            skipped_photo += 1
            continue
        did = did_fs if (args.from_firestore and did_fs) else bible_map.get(norm(name), "")
        if not did:
            no_id += 1
            continue
        url = fetch_avatar_url(did, token, size=args.size)
        time.sleep(0.35)  # gentle on the rate limit
        if not url:
            no_avatar += 1
            continue
        if fs_write_avatar(project, api_key, doc_id, url):
            updated += 1
            print(f"  ✓ {name} -> {url.split('?')[0]}")
        else:
            failed += 1
            print(f"  ✗ write failed for {name}", file=sys.stderr)

    print(f"\nDone. {updated} avatars written, {no_avatar} had no custom avatar, "
          f"{no_id} had no matchable discord id, {skipped_photo} skipped (admin photo), "
          f"{failed} write failures.")


if __name__ == "__main__":
    main()
