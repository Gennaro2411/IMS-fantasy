#!/usr/bin/env python3
"""
Point every IMS driver at their locally-hosted avatar image.

You already have the avatar images named by Discord user id (the `driver-avatars/` folder that
ships next to this script). This script does NOT contact Discord at all — no bot token needed.
It just:

  1. Reads (driver_name -> discord_id) from the bible .db.
  2. For each driver doc in Firestore, finds its discord id (via the bible, matched by name — or
     via a discordId already stored on the doc), checks that an avatar file exists for that id,
     and sets the doc's `discordAvatar` field to  <AVATAR_BASE><id>.png .

The fantasy app already reads `driver.discordAvatar`, so once this runs the photos appear.

AVATAR_BASE is the public URL/path where you host the images. For GitHub Pages, commit the
`driver-avatars/` folder into the same repo as index.html and use the default relative path
"driver-avatars/" — the app will load e.g.  driver-avatars/190472312074665985.png .

Usage (Windows PowerShell):
  $env:FIRESTORE_API_KEY="AIza...."      # same key the app uses
  $env:FIRESTORE_PROJECT_ID="fantasy-ims"
  python set_driver_avatars.py --bible ims_bible_snapshot.db

Options:
  --avatar-base "driver-avatars/"   URL prefix for the images (default: driver-avatars/)
  --avatars-dir driver-avatars      local folder to verify a file exists before linking
  --overwrite-admin                 also set drivers who have an admin photo (default: skip)
"""

import argparse, json, os, sqlite3, sys, unicodedata, urllib.request


def _req(url, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try: payload = json.loads(e.read().decode() or "{}")
        except Exception: payload = {}
        return e.code, payload


def norm(name):
    if not name: return ""
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return "".join(c for c in name.lower() if c.isalnum())


def fs_base(project):
    return f"https://firestore.googleapis.com/v1/projects/{project}/databases/(default)/documents"


def fs_list_drivers(project, api_key):
    out, page = [], None
    while True:
        url = f"{fs_base(project)}/drivers?key={api_key}&pageSize=300"
        if page: url += f"&pageToken={page}"
        status, data = _req(url)
        if status != 200:
            sys.exit(f"Firestore list failed ({status}): {data}")
        for doc in data.get("documents", []):
            f = doc.get("fields", {})
            out.append((
                doc["name"].split("/")[-1],
                f.get("name", {}).get("stringValue", ""),
                f.get("discordId", {}).get("stringValue", ""),
                bool(f.get("photo", {}).get("stringValue", "")),
            ))
        page = data.get("nextPageToken")
        if not page: break
    return out


def fs_set_avatar(project, api_key, doc_id, url):
    patch = f"{fs_base(project)}/drivers/{doc_id}?key={api_key}&updateMask.fieldPaths=discordAvatar"
    body = {"fields": {"discordAvatar": {"stringValue": url}}}
    status, _ = _req(patch, "PATCH", body, {"Content-Type": "application/json"})
    return status == 200


def ids_from_bible(db_path):
    c = sqlite3.connect(db_path)
    m = {}
    for tbl in ("roster_seats", "contracts"):
        try:
            for alias, did in c.execute(
                f"SELECT driver_alias, driver_discord_id FROM {tbl} "
                f"WHERE driver_discord_id IS NOT NULL AND driver_alias IS NOT NULL"):
                if alias and did: m.setdefault(norm(alias), str(did))
        except sqlite3.OperationalError:
            pass  # table not in this snapshot — that's fine
    c.close()
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bible", help="path to the IMS bible .db (source of discord ids by name)")
    ap.add_argument("--avatar-base", default="driver-avatars/",
                    help="public URL prefix for the images (default: driver-avatars/)")
    ap.add_argument("--avatars-dir", default="driver-avatars",
                    help="local folder to confirm an image exists before linking")
    ap.add_argument("--overwrite-admin", action="store_true")
    args = ap.parse_args()

    project = os.environ.get("FIRESTORE_PROJECT_ID", "fantasy-ims")
    api_key = os.environ.get("FIRESTORE_API_KEY")
    if not api_key:
        sys.exit("Set FIRESTORE_API_KEY (and optionally FIRESTORE_PROJECT_ID).")

    have_files = set()
    if os.path.isdir(args.avatars_dir):
        have_files = {os.path.splitext(f)[0] for f in os.listdir(args.avatars_dir)
                      if f.lower().endswith(".png")}
    print(f"{len(have_files)} avatar image files found in {args.avatars_dir}/")

    bible = ids_from_bible(args.bible) if args.bible else {}
    drivers = fs_list_drivers(project, api_key)
    print(f"{len(drivers)} driver docs in Firestore.")

    updated = skipped = no_id = no_file = failed = 0
    for doc_id, name, did_fs, has_photo in drivers:
        if has_photo and not args.overwrite_admin:
            skipped += 1; continue
        did = did_fs or bible.get(norm(name), "")
        if not did:
            no_id += 1; continue
        if have_files and did not in have_files:
            no_file += 1; continue
        url = f"{args.avatar_base}{did}.png"
        if fs_set_avatar(project, api_key, doc_id, url):
            updated += 1; print(f"  ✓ {name} -> {url}")
        else:
            failed += 1; print(f"  ✗ write failed: {name}", file=sys.stderr)

    print(f"\nDone. {updated} avatars linked, {no_id} no discord id, "
          f"{no_file} had an id but no image file, {skipped} skipped (admin photo), "
          f"{failed} failures.")


if __name__ == "__main__":
    main()
