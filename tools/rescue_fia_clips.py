#!/usr/bin/env python3
"""
Step 2 of grabbing FIA clips: the archive threads only contain Discord *links* to the clips
(cdn.discordapp.com/...), and those links expire. Discord lets a bot ask for fresh links
(POST /attachments/refresh-urls). This script does that for every clip link in fia_clips/index.json
and downloads whatever Discord still has into fia_clips/<thread>/.

Run in the same folder as before (where the fia_clips folder is):
  $env:DISCORD_BOT_TOKEN="your-bot-token"
  python rescue_fia_clips.py
"""
import json, os, re, sys, time
import requests

TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
if not TOKEN:
    sys.exit("Set DISCORD_BOT_TOKEN first.")
H = {"Authorization": "Bot " + TOKEN, "User-Agent": "IMS-fia-clips (1.1)", "Content-Type": "application/json"}
IDX = os.path.join("fia_clips", "index.json")
data = json.load(open(IDX, encoding="utf-8"))
CDN = re.compile(r"https://(?:cdn|media)\.discordapp\.(?:com|net)/attachments/[^\s<>()\]|*]+", re.I)
MEDIA = re.compile(r"\.(mp4|mov|webm|mkv|avi|m4v|png|jpe?g|gif|webp)$", re.I)


def base(u):
    return u.split("?")[0]


def refresh(urls):
    while True:
        r = requests.post("https://discord.com/api/v10/attachments/refresh-urls", headers=H, json={"attachment_urls": urls}, timeout=60)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 1)) + 0.2); continue
        if r.status_code != 200:
            print("  refresh failed:", r.status_code, r.text[:200]); return {}
        return {base(x["original"]): x["refreshed"] for x in r.json().get("refreshed_urls", [])}


ok = gone = have = 0
for t in data:
    text = " ".join(m.get("text", "") for m in t.get("messages", []))
    urls = list(dict.fromkeys(u.rstrip(".,") for u in CDN.findall(text) if MEDIA.search(base(u))))
    if not urls:
        continue
    folder = os.path.join("fia_clips", re.sub(r"[^\w\-]+", "_", t["thread"].split("|")[0].strip()))
    os.makedirs(folder, exist_ok=True)
    t.setdefault("files", [])
    fresh = {}
    for i in range(0, len(urls), 50):
        fresh.update(refresh(urls[i:i + 50]))
    for u in urls:
        parts = base(u).split("/")
        fn = f"{parts[-2]}_{re.sub(r'[^\w.\-]+', '_', parts[-1])}"
        path = os.path.join(folder, fn)
        rel = f"{os.path.basename(folder)}/{fn}"
        if os.path.exists(path):
            have += 1
        else:
            f = fresh.get(base(u))
            try:
                if not f:
                    raise RuntimeError("no fresh link")
                with requests.get(f, stream=True, timeout=300) as r:
                    r.raise_for_status()
                    with open(path, "wb") as out:
                        for chunk in r.iter_content(1 << 20):
                            out.write(chunk)
                ok += 1
            except Exception:
                gone += 1
                continue
        if not any(x.get("file") == rel for x in t["files"]):
            t["files"].append({"file": rel, "from": base(u)})
    print(f"  {t['thread'][:44]:44}  {len(urls)} clip link(s)")

json.dump(data, open(IDX, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"\nDone — downloaded {ok}, already had {have}, gone for good {gone}.")
print("Zip the fia_clips folder and send it.")
