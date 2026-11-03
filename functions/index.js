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
