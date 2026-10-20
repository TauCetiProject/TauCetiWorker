import { Env, EventPointer, Repository, digest, parseGitHub, stub } from './protocol';
import { log } from './storage';

// Parked ingress keeps acknowledged webhooks durable while the repository
// object cannot apply them (storage full or a planned migration). Pointers and
// pinned bodies live in private R2 without expiry metadata, so normal
// retention never removes them. Keys are deterministic: parking is idempotent.
export interface Parked extends EventPointer { original_key: string; sha256: string; parked_at: number; reason: string; replay_failures?: number; last_error?: string }
export const PARK_AFTER_ATTEMPTS = 10;
const HOLD_AFTER_FAILURES = 5;
const pad = (n: number) => String(Math.max(0, Math.floor(n))).padStart(15, '0');
export const pointerKey = (repo: string, p: EventPointer, held = false) => `parked/${repo}/${held ? 'held' : 'pointers'}/${pad(p.received)}-${p.delivery}.json`;
export const bodyKey = (repo: string, delivery: string) => `parked/${repo}/bodies/${delivery}.json`;
export const controlKey = (repo: string) => `control/${repo}/ingress.json`;
export function backoff(attempts: number): number { return Math.min(600, 5 * 2 ** Math.max(0, Math.min(attempts - 1, 8))); }

export async function ingressMode(env: Env, repo: Repository): Promise<{ mode: 'apply' | 'parked'; reason?: string; at?: number }> {
  const object = await env.ARCHIVE.get(controlKey(repo.id));
  return object ? object.json() : { mode: 'apply' };
}
export async function setIngressMode(env: Env, repo: Repository, mode: unknown, reason: unknown): Promise<any> {
  if (mode !== 'apply' && mode !== 'parked') throw new Error('invalid ingress mode');
  const value = { mode, reason: typeof reason === 'string' ? reason.slice(0, 200) : undefined, at: Date.now() };
  if (mode === 'apply') await env.ARCHIVE.delete(controlKey(repo.id));
  else await env.ARCHIVE.put(controlKey(repo.id), JSON.stringify(value));
  return value;
}

// Pin and verify the body, then store its pointer. Only after both writes
// succeed may the caller acknowledge the queue message.
export async function park(env: Env, repo: Repository, pointer: EventPointer, reason: string): Promise<Parked> {
  const pinned = bodyKey(repo.id, pointer.delivery);
  let sha256 = (await env.ARCHIVE.head(pinned))?.customMetadata?.sha256;
  if (!sha256) {
    const source = await env.ARCHIVE.get(pointer.key);
    if (!source) throw new Error('event body unavailable');
    const bytes = await source.arrayBuffer();
    sha256 = await digest(bytes);
    if (source.customMetadata?.sha256 && source.customMetadata.sha256 !== sha256) throw new Error('event body digest mismatch');
    // R2 rejects the write unless the stored bytes match this digest.
    await env.ARCHIVE.put(pinned, bytes, { sha256, customMetadata: { kind: 'parked-body', sha256, delivery: pointer.delivery } });
    const check = await env.ARCHIVE.head(pinned);
    if (!check || check.size !== bytes.byteLength || check.customMetadata?.sha256 !== sha256) throw new Error('parked body verification failed');
  }
  const parked: Parked = { key: pinned, delivery: pointer.delivery, event: pointer.event, repository: repo.name, received: pointer.received, original_key: pointer.key, sha256, parked_at: Date.now(), reason };
  const text = JSON.stringify(parked), key = pointerKey(repo.id, pointer);
  await env.ARCHIVE.put(key, text, { sha256: await digest(text), customMetadata: { kind: 'parked-pointer', delivery: pointer.delivery } });
  if (!await env.ARCHIVE.head(key)) throw new Error('parked pointer verification failed');
  return parked;
}

// Replay oldest first. A pointer is removed only after the repository commits
// the delivery receipt; a crash before removal repeats a deduplicated replay.
export async function drain(env: Env, repo: Repository, options: { limit?: number; held?: boolean } = {}): Promise<any> {
  if ((await ingressMode(env, repo)).mode === 'parked') return { replayed: 0, stopped: 'parked' };
  const prefix = `parked/${repo.id}/${options.held ? 'held' : 'pointers'}/`;
  const page = await env.ARCHIVE.list({ prefix, limit: Math.min(Math.max(1, options.limit || 25), 100) });
  let replayed = 0, failed = 0;
  for (const object of page.objects) {
    const parked = await env.ARCHIVE.get(object.key).then(o => o?.json<Parked>());
    if (!parked) continue;
    const response = await stub(env.REPOSITORIES, repo.id).fetch('https://internal/event', { method: 'POST', body: JSON.stringify({ key: parked.key, delivery: parked.delivery, event: parked.event, repository: parked.repository, received: parked.received }) });
    if (response.ok) {
      // Body first: a missing body with a committed receipt still deduplicates.
      await env.ARCHIVE.delete(parked.key);
      await env.ARCHIVE.delete(object.key);
      replayed++;
      continue;
    }
    const error: any = await response.json().catch(() => ({}));
    log('parked_replay_failed', { repository: repo.name, delivery: parked.delivery, error: error.error, request_id: error.request_id });
    if (error.parkable) return { replayed, failed, remaining: true, stopped: error.error };
    // A repeatedly failing item moves aside so it cannot block newer events;
    // it remains durable and replayable from the held prefix.
    failed++;
    const updated = { ...parked, replay_failures: (parked.replay_failures || 0) + 1, last_error: String(error.error || response.status).slice(0, 200) };
    const hold = !options.held && updated.replay_failures >= HOLD_AFTER_FAILURES, target = hold ? pointerKey(repo.id, parked, true) : object.key;
    await env.ARCHIVE.put(target, JSON.stringify(updated), { customMetadata: { kind: 'parked-pointer', delivery: parked.delivery } });
    if (hold) await env.ARCHIVE.delete(object.key);
  }
  return { replayed, failed, remaining: page.truncated || replayed < page.objects.length };
}

export async function backlog(env: Env, repo: Repository): Promise<any> {
  const count = async (prefix: string) => {
    let cursor: string | undefined, total = 0, oldest: string | undefined, pages = 0;
    do {
      const page = await env.ARCHIVE.list({ prefix, cursor, limit: 1000 });
      total += page.objects.length; oldest ||= page.objects[0]?.key;
      cursor = page.truncated ? page.cursor : undefined;
    } while (cursor && ++pages < 10);
    const received = oldest ? Number(oldest.slice(prefix.length, prefix.length + 15)) : null;
    return { count: total, truncated: Boolean(cursor), oldest_received: received };
  };
  return { ...await ingressMode(env, repo), parked: await count(`parked/${repo.id}/pointers/`), held: await count(`parked/${repo.id}/held/`) };
}

// Queue/DLQ reconciliation: retained raw deliveries without a committed
// receipt are parked for normal replay. Dry run unless apply is set.
export async function reconcile(env: Env, repo: Repository, input: any, targets: (payload: any, event: string) => Repository[]): Promise<any> {
  if (typeof input.date !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(input.date)) throw new Error('invalid reconciliation date');
  const page = await env.ARCHIVE.list({ prefix: `webhooks/${input.date}/`, cursor: typeof input.cursor === 'string' ? input.cursor : undefined, limit: 100, include: ['customMetadata'] });
  const objects = page.objects.map(o => ({ object: o, delivery: o.key.slice(o.key.lastIndexOf('/') + 1).replace(/\.json$/, '') })).filter(o => /^[a-zA-Z0-9-]{1,100}$/.test(o.delivery));
  const response = await stub(env.REPOSITORIES, repo.id).fetch('https://internal/storage/deliveries', { method: 'POST', body: JSON.stringify({ ids: objects.map(o => o.delivery), repository_id: repo.id }) });
  if (!response.ok) throw new Error('receipt check failed');
  const known = new Set((await response.json() as any).known as string[]);
  const missing: string[] = [], ignored: string[] = [], parked: string[] = [];
  for (const { object, delivery } of objects) {
    if (known.has(delivery)) continue;
    const event = object.customMetadata?.event || '';
    const body = await env.ARCHIVE.get(object.key);
    if (!body || !targets(parseGitHub(await body.text()), event).some(r => r.id === repo.id)) { ignored.push(delivery); continue; }
    missing.push(delivery);
    if (input.apply === true) { await park(env, repo, { key: object.key, delivery, event, repository: repo.name, received: object.uploaded.getTime() }, 'reconcile'); parked.push(delivery); }
  }
  return { date: input.date, checked: objects.length, known: known.size, missing, parked, ignored: ignored.length, cursor: page.truncated ? page.cursor : null };
}
