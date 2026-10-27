// Shared catalog links on Vercel: the same API as server/shares.py, with photos and link details kept in
// Vercel Blob. Sharing switches on once a Blob store is connected to the project (Storage tab).
//   POST   /api/share/image       upload one photo {data: "data:image/jpeg;base64,..."} -> {name, url}
//   POST   /api/share             create a link from uploaded photos -> {id, key, url} (url on the public address)
//   GET    /api/share/<id>        the link's details
//   DELETE /api/share/<id>        stop sharing (header X-Share-Key)
//   GET    /api/share/img/<name>  a photo
//   GET    /api/share/status      whether a Blob store is connected (no secrets)
//   GET    /s/<id>                the client's page: share.html with the details and preview tags filled in
// vercel.json rewrites those paths to this function as ?id=, ?img= and ?page=.
import { put, get, head, del } from '@vercel/blob';
import { createHash, randomBytes, randomInt, timingSafeEqual } from 'node:crypto';
import { readFile } from 'node:fs/promises';

const MAX_IMAGE_BYTES = 3_000_000, MAX_ITEMS = 60;
const ID_CHARS = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789';   // no look-alike characters
const ID_RX = /^[A-Za-z0-9]{10}$/, IMAGE_RX = /^[a-f0-9]{64}\.(jpg|png|webp)$/;
const IMAGE_URL_RX = /^\/api\/share\/img\/[a-f0-9]{64}\.(jpg|png|webp)$/;
const TYPES = { jpg: 'image/jpeg', png: 'image/png', webp: 'image/webp' };

class ShareError extends Error {
  constructor(message, status = 400) { super(message); this.status = status; }
}

const sha256 = (data) => createHash('sha256').update(data).digest('hex');
const text = (v, n) => String(v ?? '').split(/\s+/).filter(Boolean).join(' ').slice(0, n);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#x27;' }[c]));
const json = (status, body) => Response.json(body, { status, headers: { 'Cache-Control': 'no-store' } });

// Connecting a Blob store adds BLOB_STORE_ID and/or BLOB_READ_WRITE_TOKEN. If a custom prefix was chosen when
// connecting, the names change, so a read-write token or store id is also recognised by its value.
const env = process.env;
const token = env.BLOB_READ_WRITE_TOKEN || Object.values(env).find((v) => /^vercel_blob_rw_/.test(v || ''));
const storeId = env.BLOB_STORE_ID || Object.entries(env).find(([k, v]) => k.endsWith('_STORE_ID') && /^store_/.test(v || ''))?.[1];
const auth = token ? { token } : storeId ? { storeId } : null;   // passed to every Blob call
// Links always point at the public production address: Vercel keeps preview and branch addresses
// (plugin-git-main-….vercel.app) behind a Vercel login, so clients can't open links made there
const site = env.VERCEL_PROJECT_PRODUCTION_URL ? `https://${env.VERCEL_PROJECT_PRODUCTION_URL}` : '';

// A Blob store is either public or private: use whichever one the connected store accepts
let access = process.env.JEWELREX_BLOB_ACCESS || null;
async function withAccess(fn) {
  let error = null, missing = false;
  for (const a of access ? [access] : ['public', 'private']) {
    try {
      const r = await fn(a);
      if (r != null) { access = a; return r; }
      missing = true;
    } catch (e) { error = e; }
  }
  if (missing || !error) return null;
  throw error;
}

function imageKind(d) {
  if (d[0] === 0xff && d[1] === 0xd8 && d[2] === 0xff) return 'jpg';
  if (d.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))) return 'png';
  if (d.toString('latin1', 0, 4) === 'RIFF' && d.toString('latin1', 8, 12) === 'WEBP') return 'webp';
  throw new ShareError('photos must be JPG, PNG or WEBP');
}

async function saveImage(body) {
  const dataUrl = body && body.data;
  if (typeof dataUrl !== 'string' || !dataUrl.startsWith('data:image/') || !dataUrl.includes(',')) throw new ShareError('missing photo');
  const b64 = dataUrl.slice(dataUrl.indexOf(',') + 1);
  if (!/^[A-Za-z0-9+/]*={0,2}$/.test(b64)) throw new ShareError('photo is not valid base64');
  const data = Buffer.from(b64, 'base64');
  if (!data.length || data.length > MAX_IMAGE_BYTES) throw new ShareError('photo is empty or larger than 3 MB');
  const name = `${sha256(data)}.${imageKind(data)}`, pathname = `shares/img/${name}`;
  // Stored under the hash of its bytes, so a photo that is already there is not uploaded again
  if (!(await head(pathname, auth).then(() => true, () => false)))
    await withAccess((a) => put(pathname, data, { access: a, contentType: TYPES[name.split('.')[1]], allowOverwrite: true, ...auth }));
  return { name, url: `/api/share/img/${name}` };
}

function build(body) {
  const items = body && body.items;
  if (!Array.isArray(items) || items.length < 1 || items.length > MAX_ITEMS) throw new ShareError(`choose between 1 and ${MAX_ITEMS} photos`);
  return {
    title: text(body.title, 80) || 'Selected designs', note: text(body.note, 300),
    business: text(body.business, 80), phone: text(body.phone, 20), created: new Date().toISOString().replace(/\.\d+Z$/, 'Z'),
    items: items.map((it) => {
      if (!it || typeof it !== 'object' || !IMAGE_URL_RX.test(String(it.img || ''))) throw new ShareError('every photo must be uploaded first');
      const details = (Array.isArray(it.details) ? it.details : []).slice(0, 8)
        .filter((d) => Array.isArray(d) && typeof d[0] === 'string' && text(d[1], 80)).map(([k, v]) => [text(k, 24), text(v, 80)]);
      const [w, h] = [it.w, it.h].map((n) => (typeof n === 'number' && n >= 1 && n <= 20000 ? Math.trunc(n) : 0));
      return { title: text(it.title, 120) || 'Untitled', kind: text(it.kind, 40), sku: text(it.sku, 40), details, img: it.img, w: w || 4, h: h || 5 };
    }),
  };
}

async function createShare(body) {
  const share = build(body), key = randomBytes(24).toString('base64url');
  const id = Array.from({ length: 10 }, () => ID_CHARS[randomInt(ID_CHARS.length)]).join('');
  await withAccess((a) => put(`shares/${id}.json`, JSON.stringify({ ...share, keyHash: sha256(key) }),
    { access: a, contentType: 'application/json', cacheControlMaxAge: 60, ...auth }));
  return { id, key, url: `${site}/s/${id}` };
}

async function readShare(id) {
  if (!ID_RX.test(id || '')) return null;
  // Read past the CDN cache so a link stops working as soon as sharing is stopped
  const r = await withAccess((a) => get(`shares/${id}.json`, { access: a, useCache: false, ...auth }));
  return r && r.statusCode === 200 ? JSON.parse(await new Response(r.stream).text()) : null;
}

const publicShare = ({ keyHash, ...share }, id) => ({ ...share, id });   // everything except the key's hash

async function deleteShare(id, key) {
  const share = await readShare(id);
  if (!share) throw new ShareError('this link is no longer shared', 404);
  const a = Buffer.from(sha256(key || '')), b = Buffer.from(String(share.keyHash));
  if (a.length !== b.length || !timingSafeEqual(a, b)) throw new ShareError('only the person who created this link can stop sharing it', 403);
  await del(`shares/${id}.json`, auth);
}

async function shareImage(name) {
  const r = IMAGE_RX.test(name) ? await withAccess((a) => get(`shares/img/${name}`, { access: a, ...auth })) : null;
  if (!r || r.statusCode !== 200) return new Response('Not found', { status: 404 });
  // Photos are stored under the hash of their bytes, so they never change
  return new Response(r.stream, { headers: { 'Content-Type': TYPES[name.split('.')[1]], 'X-Content-Type-Options': 'nosniff',
    'Cache-Control': 'public, max-age=31536000, s-maxage=31536000, immutable' } });
}

// <head> additions for /s/<id>: the link preview (WhatsApp etc.) and the catalog data for share.html
function pageHead(share, origin) {
  if (!share) return '<title>Catalog not available</title><script>window.SHARE=null</script>';
  const title = share.title + (share.business ? ` · ${share.business}` : ''), n = share.items.length;
  const desc = share.note || `${n} piece${n === 1 ? '' : 's'} – tap to view`;
  return `<title>${esc(title)}</title><meta property="og:title" content="${esc(title)}">` +
    `<meta property="og:description" content="${esc(desc)}"><meta property="og:type" content="website">` +
    `<meta property="og:image" content="${esc(origin + share.items[0].img)}"><meta name="twitter:card" content="summary_large_image">` +
    `<script>window.SHARE=${JSON.stringify(share).replace(/</g, '\\u003c')}</script>`;
}

let template = null;
async function shareTemplate(origin) {
  // share.html is bundled with this function (vercel.json includeFiles); fetching it is the fallback
  template ??= await readFile(new URL('../share.html', import.meta.url), 'utf8')
    .catch(() => readFile(`${process.cwd()}/share.html`, 'utf8'))
    .catch(async () => {
      const r = await fetch(`${origin}/share.html`);
      if (!r.ok) throw new Error('share.html not found');
      return r.text();
    });
  return template;
}

async function sharePage(origin, id, on) {
  const share = on ? await readShare(id) : null;
  const head = pageHead(share && publicShare(share, id), origin);
  const html = (await shareTemplate(origin)).replace('<!--SHARE-->', () => head);
  return new Response(html, { status: share ? 200 : 404, headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' } });
}

export default {
  async fetch(request) {
    const url = new URL(request.url), q = url.searchParams, method = request.method;
    const page = q.get('page') ?? url.pathname.match(/^\/s\/([^/]+)$/)?.[1];
    const img = q.get('img') ?? url.pathname.match(/^\/api\/share\/img\/([^/]+)$/)?.[1];
    const id = q.get('id') ?? url.pathname.match(/^\/api\/share\/([^/]+)$/)?.[1];
    const on = Boolean(auth);
    try {
      if (method === 'GET' && page != null) return await sharePage(url.origin, page, on);
      if (method === 'GET' && id === 'status') return json(200, { on, via: token ? 'token' : storeId ? 'oidc' : null, site: site || null });
      if (!on) return json(503, { error: 'sharing_off', message: 'Sharing is not switched on yet: no Vercel Blob store is connected to this deployment. Connect one in the project\'s Storage tab, then redeploy.' });
      if (method === 'GET' && img != null) return await shareImage(img);
      if (method === 'POST' && id === 'image') return json(201, await saveImage(await request.json()));
      if (method === 'POST' && id == null) return json(201, await createShare(await request.json()));
      if (method === 'GET' && id != null) {
        const share = await readShare(id);
        return share ? json(200, publicShare(share, id)) : json(404, { error: 'not_found', message: 'this link is no longer shared' });
      }
      if (method === 'DELETE' && id != null) { await deleteShare(id, request.headers.get('x-share-key')); return json(200, { ok: true }); }
      return json(404, { error: 'not_found' });
    } catch (e) {
      if (e instanceof ShareError) return json(e.status, { error: 'ShareError', message: e.message });
      if (e instanceof SyntaxError) return json(400, { error: 'bad_request', message: 'invalid JSON' });
      console.error(e);
      return json(502, { error: 'storage_error', message: String(e && e.message || e).slice(0, 300) });
    }
  },
};
