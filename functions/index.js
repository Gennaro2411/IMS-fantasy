// IMS Fantasy snapshot server.
// Every minute: takes the documents the app changed (listed in _snapQ by the app itself), patches them
// into one gzipped file per collection in Storage (snap/<col>.json.gz), and writes snap/meta.json with
// a version per collection. The app downloads those files instead of reading every document from
// Firestore. Every 6 hours each collection is rebuilt in full as a safety net.
const { onSchedule } = require('firebase-functions/v2/scheduler');
const admin = require('firebase-admin');
const zlib = require('zlib');

admin.initializeApp();
const db = admin.firestore();
const bucket = admin.storage().bucket('fantasy-ims.firebasestorage.app');
const COLS = ['drivers', 'races', 'teams', 'constructors', 'schedule', 'seasonArchives', 'archives', 'bets', 'transfers', 'rosterMoves', 'predictions', 'settings'];
// Only these settings docs go in the snapshot: they change rarely and every visit reads them. Fast-changing
// ones (lock state, live games, daily flags) are always read live by the app.
const SETTINGS = ['seasonNumber','totalRounds','seasonDefinitions','trackRecords','qualiPace','heroImage','fiaVerdicts','betSpecials','betBoost','betH2H','betConfig','betOddsOverrides','bdayOdds','statOverrides','raceStats','musicCustom','departedFanZonePoints','departedDriverPrices','betStatsSince','wordleCfg','adminUsers','stewardIncidents'];
const readCol = async c => c === 'settings'
  ? (await db.getAll(...SETTINGS.map(id => db.collection('settings').doc(id)))).filter(s => s.exists)
  : (await db.collection(c).get()).docs;
const TOKEN = 'ims-snap-v1';            // fixed download token the app uses for snap/ files
const FULL_EVERY = 6 * 3600 * 1000;

// Same shape the app's own Firestore reader produces: plain JSON, timestamps/refs/bytes become null.
function clean(v) {
  if (v === null || v === undefined) return null;
  if (typeof v === 'number') return isFinite(v) ? v : null;
  if (typeof v !== 'object') return v;
  if (Array.isArray(v)) return v.map(clean);
  if (v instanceof admin.firestore.Timestamp || v instanceof admin.firestore.DocumentReference || v instanceof admin.firestore.GeoPoint || Buffer.isBuffer(v)) return null;
  const o = {}; for (const k of Object.keys(v)) o[k] = clean(v[k]); return o;
}
const toObj = d => Object.assign({ id: d.id }, clean(d.data()));
const byId = (a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);

async function readFile(path, gz) {
  try { const [buf] = await bucket.file(path).download(); return JSON.parse((gz ? zlib.gunzipSync(buf) : buf).toString('utf8')); }
  catch (e) { if (e.code === 404) return null; throw e; }
}
async function writeFile(path, obj, gz, cache) {
  const body = gz ? zlib.gzipSync(JSON.stringify(obj)) : Buffer.from(JSON.stringify(obj));
  await bucket.file(path).save(body, { resumable: false, contentType: gz ? 'application/octet-stream' : 'application/json',
    metadata: { cacheControl: cache, metadata: { firebaseStorageDownloadTokens: TOKEN } } });
}
async function putCol(col, docs) {
  docs.sort(byId);
  await writeFile(`snap/${col}.json.gz`, { col, at: Date.now(), docs }, true, 'public, max-age=31536000');
  return Date.now().toString(36);       // new version → new URL in the app, so old copies are never reused
}

exports.imsSnapshot = onSchedule({ schedule: 'every 1 minutes', region: 'europe-west2', timeoutSeconds: 240, memory: '512MiB', maxInstances: 1, retryCount: 0 }, async () => {
  const meta = (await readFile('snap/meta.json')) || { v: {}, full: 0 };
  meta.v = meta.v || {};
  if (!meta.cors) {   // let the app (any origin) download the snapshot files
    await bucket.setCorsConfiguration([{ origin: ['*'], method: ['GET', 'HEAD'], responseHeader: ['Content-Type'], maxAgeSeconds: 3600 }]);
    meta.cors = 1;
  }
  const now = Date.now();
  if (now - (meta.full || 0) > FULL_EVERY || COLS.some(c => !meta.v[c])) {
    for (const c of COLS) meta.v[c] = await putCol(c, (await readCol(c)).map(toObj));
    meta.full = now;   // the queue is left alone: next run re-applies anything changed meanwhile
  } else {
    const q = await db.collection('_snapQ').limit(1000).get();
    if (!q.empty) {
      const byCol = {};
      q.docs.forEach(d => { const { col, id } = d.data() || {}; if (COLS.includes(col) && id && (col !== 'settings' || SETTINGS.includes(id))) (byCol[col] = byCol[col] || new Set()).add(id); });
      for (const [col, ids] of Object.entries(byCol)) {
        const cur = await readFile(`snap/${col}.json.gz`, true);
        if (!cur || !Array.isArray(cur.docs)) { meta.v[col] = await putCol(col, (await readCol(col)).map(toObj)); continue; }
        const map = new Map(cur.docs.map(d => [d.id, d])), list = [...ids];
        for (let i = 0; i < list.length; i += 300) {
          const snaps = await db.getAll(...list.slice(i, i + 300).map(id => db.collection(col).doc(id)));
          snaps.forEach(s => { if (s.exists) map.set(s.id, toObj(s)); else map.delete(s.id); });
        }
        meta.v[col] = await putCol(col, [...map.values()]);
      }
      // Remove processed queue entries — unless the app re-queued one while we worked (then keep it).
      await Promise.all(q.docs.map(d => d.ref.delete({ lastUpdateTime: d.updateTime }).catch(() => {})));
      meta.qok = 1;   // the app can write the queue → snapshots can be trusted to stay current
    }
  }
  meta.beat = Date.now();
  await writeFile('snap/meta.json', meta, false, 'public, max-age=10');
});

// ===== FIA clips: every 6 hours, read new incident threads in #fia-transcripts and attach their clips =====
// Needs the IMS bot token once:  firebase functions:secrets:set DISCORD_BOT_TOKEN
// Links (Twitch/Medal/YouTube/Streamable/Xbox) are saved to settings/stClipsExtra; videos/images posted in
// Discord are copied into Storage (fia/<thread>/<file>) before Discord's links expire. Defences go to
// settings/stDefExtra. The app's Steward Simulator picks all of it up (Daily + Infinite).
const { defineSecret } = require('firebase-functions/params');
const DISCORD_BOT_TOKEN = defineSecret('DISCORD_BOT_TOKEN');
const FIA_CHANNEL = '1476258411783127258', BOT_ID = '1471899283405148433', API = 'https://discord.com/api/v10';
const H = /Season (\d+) Round (\d+) (\w+) incident (\d+) \| \*\*Session :\*\* ([^\n*]+?)\s*\n\*\*Lap :\*\* ([^\n]*)\n\*\*Drivers involved :\*\* ([^\n]*)\n/;
const BLK = /\*\*(.+?)\*\* \(`(\d+)`\)\n([\s\S]*?)(?=\n---\n|$)/g;
function clipOf(u) { let m;
  if ((m = u.match(/twitch\.tv\/[^/]+\/clip\/([\w-]+)/i)) || (m = u.match(/clips\.twitch\.tv\/(?:embed\?clip=)?([\w-]+)/i))) return ['twitch', m[1]];
  if (/medal\.tv/i.test(u)) { m = u.match(/medal\.tv\/(?:[^?#]*\/)?clips?\/([\w-]+)/i); return m ? ['medal', m[1]] : null; }
  if (/youtu/i.test(u)) { m = u.match(/(?:v=|youtu\.be\/|shorts\/|embed\/|live\/)([\w-]{11})/); return m ? ['yt', m[1]] : null; }
  if (/streamable\.com/i.test(u)) { m = u.match(/streamable\.com\/(?:e\/)?(\w+)/i); return m ? ['st', m[1]] : null; }
  if (/xbox\.com/i.test(u) && (/\/media\//.test(u) || /clip/i.test(u))) return ['link', u];
  return null; }
exports.imsFiaClips = onSchedule({ schedule: 'every 6 hours', region: 'europe-west2', timeoutSeconds: 540, memory: '1GiB', maxInstances: 1, retryCount: 0, secrets: [DISCORD_BOT_TOKEN] }, async () => {
  const H0 = { Authorization: 'Bot ' + DISCORD_BOT_TOKEN.value(), 'User-Agent': 'IMS-fantasy-clips (1.0)' };
  const get = async (url) => { for (let i = 0; i < 5; i++) { const r = await fetch(url, { headers: H0 }); if (r.status === 429) { const w = ((await r.json().catch(() => ({}))).retry_after || 1) * 1000 + 200; await new Promise(res => setTimeout(res, w)); continue; } if (!r.ok) throw new Error(url + ' ' + r.status); return r.json(); } throw new Error('rate limited'); };
  const since = Date.now() - 21 * 864e5, threads = {};
  const ch = await get(`${API}/channels/${FIA_CHANNEL}`);
  (await get(`${API}/guilds/${ch.guild_id}/threads/active`)).threads.filter(t => t.parent_id === FIA_CHANNEL).forEach(t => { threads[t.id] = t; });
  for (const kind of ['public', 'private']) { let before = null;
    for (let page = 0; page < 10; page++) { let d; try { d = await get(`${API}/channels/${FIA_CHANNEL}/threads/archived/${kind}?limit=100${before ? '&before=' + encodeURIComponent(before) : ''}`); } catch (e) { break; }
      let old = false; (d.threads || []).forEach(t => { const at = Date.parse((t.thread_metadata || {}).archive_timestamp || 0); if (at < since) old = true; else threads[t.id] = t; });
      if (!d.has_more || !d.threads.length || old) break; before = d.threads[d.threads.length - 1].thread_metadata.archive_timestamp; } }
  const extraDoc = db.collection('settings').doc('stClipsExtra'), defDoc = db.collection('settings').doc('stDefExtra');
  const V = ((await extraDoc.get()).data() || {}).value || {}, D = ((await defDoc.get()).data() || {}).value || {};
  const [have] = await bucket.getFiles({ prefix: 'fia/' }); const stored = new Set(have.map(f => f.name));
  let added = 0;
  for (const t of Object.values(threads)) {
    let msgs = [], before = null;
    for (let i = 0; i < 20; i++) { const page = await get(`${API}/channels/${t.id}/messages?limit=100${before ? '&before=' + before : ''}`); msgs = msgs.concat(page); if (page.length < 100) break; before = page[page.length - 1].id; }
    msgs.reverse(); const txt = msgs.map(m => m.content || '').join('\n'), h = txt.match(H); if (!h) continue;
    const [, se, rn, dv, num, , , drvs] = h, key = `${se}-${rn}-${dv.toUpperCase()}-I${String(Number(num)).padStart(4, '0')}`;
    const list = new Set(V[key] || []), blocks = [...txt.matchAll(BLK)];
    blocks.forEach(([, n, id, b]) => { if (id === BOT_ID) return; (b.match(/https?:\/\/[^\s<>()\[\]|*"]+/g) || []).forEach(u => { const c = clipOf(u.replace(/[.,)]+$/, '')); if (c) list.add(c[0] + '|' + c[1] + '|' + n.replace(/\|/g, '')); }); });
    // videos/images attached in the thread → copy into Storage now (Discord's own links expire)
    const folder = t.name.split('|')[0].trim().replace(/[^\w\-]+/g, '_');
    for (const m of msgs) for (const a of (m.attachments || [])) {
      if (!/^(video|image)\//.test(a.content_type || '') && !/\.(mp4|mov|webm|m4v|png|jpe?g|gif|webp)$/i.test(a.filename)) continue;
      if ((a.size || 0) > 60e6) continue;
      const path = `fia/${folder}/${m.id}_${a.filename.replace(/[^\w.\-]+/g, '_')}`;
      if (!stored.has(path)) { try { const r = await fetch(a.url); if (!r.ok) continue; const buf = Buffer.from(await r.arrayBuffer());
          await bucket.file(path).save(buf, { resumable: false, contentType: a.content_type || 'application/octet-stream' }); stored.add(path); } catch (e) { continue; } }
      const who = blocks.find(([, , , b]) => b.includes(a.filename) || b.includes(a.id)); list.add('file|' + path.slice(4) + '|' + (who ? who[1].replace(/\|/g, '') : ''));
    }
    // defences: what the accused wrote
    const nk = (n) => n.toLowerCase().replace(/\[[^\]]*\]/g, '').replace(/[^a-z0-9]/g, ''), acc = new Set(drvs.split(/,\s*/).map(nk));
    const defs = blocks.filter(([, n, id]) => id !== BOT_ID && acc.has(nk(n))).map(([, n, id, b]) => { const body = b.split('\nMessage ID')[0].split('\nEmbeds:')[0].split(/\nAttachments:/)[0].replace(/https?:\/\/\S+/g, '').replace(/<@!?\d+>/g, '').trim();
      return body.length >= 3 ? [n, id, '0', body.slice(0, 600)].map(x => String(x).replace(/\|/g, '/')).join('|') : null; }).filter(Boolean).slice(0, 8);
    if (list.size !== (V[key] || []).length) { V[key] = [...list]; added++; }
    if (defs.length && !D[key]) D[key] = defs;
  }
  if (added) await extraDoc.set({ value: V }, { merge: true });
  await defDoc.set({ value: D }, { merge: true });
  console.log('[fia clips]', Object.keys(threads).length, 'threads checked,', added, 'incidents updated');
});
