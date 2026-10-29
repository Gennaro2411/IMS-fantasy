#!/usr/bin/env python3
"""
Grab every clip from #fia-transcripts in one go.

Uses the IMS bot's token (the bot can already read the channel). For every archive thread
(e.g. "d0-i0002-s8r2 | D0 S8 R3 archive") it downloads the video/image attachments into
  fia_clips/<thread-name>/...
and writes fia_clips/index.json listing each thread, its messages and the files / clip links
(Medal, YouTube, Streamable, etc.) — that's what the app needs.

Run (Windows PowerShell):
  pip install requests
  $env:DISCORD_BOT_TOKEN="your-bot-token"
  python grab_fia_clips.py

Then zip the fia_clips folder and send it.
"""
import json, os, re, sys, time
import requests

TOKEN = os.environ.get("DISCORD_BOT_TOKEN") or (sys.argv[1] if len(sys.argv) > 1 else "")
CHANNEL = "1476258411783127258"   # 🗄️-fia-transcripts
SEASONS = {"7", "8"}               # only S7 + S8 threads
OUT = "fia_clips"
API = "https://discord.com/api/v10"
if not TOKEN:
    sys.exit("Set DISCORD_BOT_TOKEN first.")
H = {"Authorization": "Bot " + TOKEN, "User-Agent": "IMS-fia-clips (1.0)"}


def get(url, **params):
    while True:
        r = requests.get(url, headers=H, params=params, timeout=60)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 1)) + 0.2); continue
        r.raise_for_status(); return r.json()


def threads():
    seen = {}
    # active threads in the guild that belong to this channel
    ch = get(f"{API}/channels/{CHANNEL}")
    act = get(f"{API}/guilds/{ch['guild_id']}/threads/active")
    for t in act.get("threads", []):
        if t.get("parent_id") == CHANNEL: seen[t["id"]] = t
    # archived threads (public + private), paged
    for kind in ("public", "private"):
        before = None
        while True:
            try:
                d = get(f"{API}/channels/{CHANNEL}/threads/archived/{kind}", **({"before": before} if before else {}), limit=100)
            except requests.HTTPError as e:
                print(f"  (skipping {kind} archived threads: {e})"); break
            for t in d.get("threads", []): seen[t["id"]] = t
            if not d.get("has_more") or not d.get("threads"): break
            before = d["threads"][-1]["thread_metadata"]["archive_timestamp"]
    return list(seen.values())


def messages(cid):
    out, before = [], None
    while True:
        d = get(f"{API}/channels/{cid}/messages", limit=100, **({"before": before} if before else {}))
        if not d: break
        out += d; before = d[-1]["id"]
        if len(d) < 100: break
    return list(reversed(out))


LINK = re.compile(r"https?://\S*(medal\.tv|youtu\.be|youtube\.com|streamable\.com|twitch\.tv|clips\.|gyazo|imgur|outplayed|cdn\.discordapp|media\.discordapp)\S*", re.I)
os.makedirs(OUT, exist_ok=True)
index = []
ts = threads()
print(f"Found {len(ts)} threads")
for t in sorted(ts, key=lambda x: x["name"]):
    m = re.match(r"(\w+)-(i\d+)-s(\d+)r(\d+)", t["name"], re.I)
    if not m or m.group(3) not in SEASONS: continue
    name = re.sub(r"[^\w\-]+", "_", t["name"].split("|")[0].strip())
    folder = os.path.join(OUT, name); os.makedirs(folder, exist_ok=True)
    msgs, files, links = messages(t["id"]), [], []
    for msg in msgs:
        for a in msg.get("attachments", []):
            ct = a.get("content_type", "") or ""
            if not (ct.startswith("video") or ct.startswith("image") or re.search(r"\.(mp4|mov|webm|mkv|png|jpe?g|gif)$", a["filename"], re.I)): continue
            fn = f"{msg['id']}_{re.sub(r'[^\w.\-]+', '_', a['filename'])}"
            path = os.path.join(folder, fn)
            if not os.path.exists(path):
                with requests.get(a["url"], stream=True, timeout=300) as r:
                    r.raise_for_status()
                    with open(path, "wb") as f:
                        for chunk in r.iter_content(1 << 20): f.write(chunk)
            files.append({"file": f"{name}/{fn}", "type": ct, "by": msg["author"].get("global_name") or msg["author"]["username"]})
        for l in LINK.finditer(msg.get("content", "")): links.append(l.group(0))
        for e in msg.get("embeds", []):
            for k in ("url",):
                if e.get(k) and LINK.search(e[k]): links.append(e[k])
    index.append({"thread": t["name"], "thread_id": t["id"], "div": m.group(1).upper(), "incident": m.group(2).upper(), "season": int(m.group(3)), "round": int(m.group(4)),
                  "files": files, "links": sorted(set(links)),
                  "messages": [{"by": x["author"].get("global_name") or x["author"]["username"], "at": x["timestamp"], "text": x.get("content", "")} for x in msgs]})
    print(f"  {t['name'][:40]:40}  {len(files)} file(s), {len(set(links))} link(s)")
json.dump(index, open(os.path.join(OUT, "index.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"\nDone — {len(index)} incident threads saved to {OUT}/. Zip that folder and send it.")
