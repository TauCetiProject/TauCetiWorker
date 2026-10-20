import { digest } from './protocol';

// Cloudflare SQLite keeps serving reads and deletes at its size limit while
// inserts and updates fail. Classify that precisely instead of hiding it.
export type Failure = 'storage_full' | 'storage_error' | 'schema_unavailable' | 'transport' | 'internal';
export function classify(error: unknown): Failure {
  const text = error instanceof Error ? error.message : String(error);
  if (/SQLITE_FULL|maximum database size|database or disk is full/i.test(text)) return 'storage_full';
  if (/no such (table|column)|schema/i.test(text)) return 'schema_unavailable';
  if (/SQLITE_|Durable Object storage|storage operation/i.test(text)) return 'storage_error';
  if (/network|fetch|connection|timed? ?out|Durable Object (is overloaded|reset)/i.test(text)) return 'transport';
  return 'internal';
}
export const PARKABLE = new Set<Failure>(['storage_full', 'schema_unavailable']);
export const FULL_MESSAGE = 'Exceeded the maximum database size. SQLITE_FULL (fault injection)';

// Request identifiers are echoed in errors and logs; never echo arbitrary input.
export function requestId(req: Request): string {
  const supplied = req.headers.get('x-request-id') || req.headers.get('cf-ray') || '';
  return /^[A-Za-z0-9-]{1,64}$/.test(supplied) ? supplied : crypto.randomUUID();
}
export function log(event: string, fields: Record<string, unknown>): void {
  console.log(JSON.stringify({ event, ...fields }));
}

// Test-only fault injection: the wrapper models a full database by rejecting
// statements that can allocate pages. Production uses the raw handle.
const ALLOCATES = /^\s*(INSERT|UPDATE|REPLACE|CREATE|ALTER)\b/i;
export function faultySql(sql: SqlStorage, full: () => boolean): SqlStorage {
  return new Proxy(sql, {
    get(target, property) {
      if (property === 'exec') return (query: string, ...bindings: any[]) => {
        if (full() && ALLOCATES.test(query)) throw new Error(FULL_MESSAGE);
        return target.exec(query, ...bindings);
      };
      const value = Reflect.get(target, property, target);
      return typeof value === 'function' ? value.bind(target) : value;
    },
  });
}

export const REQUIRED_TABLES = ['meta', 'resources', 'collections', 'members', 'deliveries', 'changes', 'jobs', 'demand', 'cursors', 'survey_fragments', 'survey_state', 'survey_inventory', 'survey_hydrated', 'survey_dirty', 'survey_hydration_failures', 'repair_metrics', 'query_metrics', 'head_prs', 'activity_loc_cache', 'activity_landings', 'retired_objects'];
export const REQUIRED_JOB_COLUMNS = ['dirty', 'generation', 'start_generation', 'lane', 'lease_id', 'lease_until', 'requested_at', 'created_at', 'wait_reason', 'nested'];
export function missingSchema(sql: SqlStorage): string[] {
  const tables = new Set(sql.exec<any>("SELECT name FROM sqlite_master WHERE type='table'").toArray().map(r => r.name));
  const missing = REQUIRED_TABLES.filter(t => !tables.has(t));
  if (tables.has('jobs')) {
    const columns = new Set(sql.exec<any>('PRAGMA table_info(jobs)').toArray().map(c => c.name));
    missing.push(...REQUIRED_JOB_COLUMNS.filter(c => !columns.has(c)).map(c => 'jobs.' + c));
  }
  return missing;
}

// Bounded operator diagnostics. Names come from sqlite_master, never from input.
export function userTables(sql: SqlStorage): string[] {
  return sql.exec<any>("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '_cf_%' ORDER BY name").toArray().map(r => r.name);
}
const quote = (name: string) => '"' + name.replace(/"/g, '""') + '"';
function columns(sql: SqlStorage, table: string): string[] {
  return sql.exec<any>('PRAGMA table_info(' + quote(table) + ')').toArray().map(c => c.name);
}
function indexes(sql: SqlStorage, table: string): { name: string; columns: string[] }[] {
  return sql.exec<any>('PRAGMA index_list(' + quote(table) + ')').toArray().map(i => ({ name: i.name, columns: sql.exec<any>('PRAGMA index_info(' + quote(i.name) + ')').toArray().map(c => c.name).filter(Boolean) }));
}
function pragma(sql: SqlStorage, name: string): number | null {
  try { return Number(Object.values(sql.exec<any>('PRAGMA ' + name).one())[0]); } catch { return null; }
}
export function summary(sql: SqlStorage): any {
  // The cursor table has no expiry index; its rows are few but large, and the
  // expiry column precedes the payload, so this scan does not read overflow pages.
  const cursors = sql.exec<any>('SELECT COUNT(*) total,SUM(expires<?) expired,MIN(expires) oldest_expiry,MAX(expires) newest_expiry FROM cursors', Date.now()).one();
  return {
    database_size: sql.databaseSize,
    page_size: pragma(sql, 'page_size'),
    // Cloudflare rejects these pragmas; null records that free-page data is unavailable.
    page_count: pragma(sql, 'page_count'),
    freelist_count: pragma(sql, 'freelist_count'),
    tables: userTables(sql).map(name => ({ name, columns: columns(sql, name), indexes: indexes(sql, name), max_rowid: sql.exec<any>('SELECT COALESCE(MAX(rowid),0) n FROM ' + quote(name)).one().n })),
    legacy_cursors: cursors,
  };
}
export function table(sql: SqlStorage, name: unknown): string {
  if (typeof name !== 'string' || !userTables(sql).includes(name)) throw new AdminError('unknown_table');
  return name;
}
export class AdminError extends Error {}
const bounded = (value: unknown, fallback: number, max: number) => Number.isSafeInteger(value) && (value as number) > 0 ? Math.min(value as number, max) : fallback;
const after = (value: unknown) => Number.isSafeInteger(value) && (value as number) >= 0 ? value as number : 0;

// Keyset scan: logical bytes per column and index for one bounded rowid range.
export function scan(sql: SqlStorage, input: any): any {
  const name = table(sql, input.table), cols = columns(sql, name), idx = indexes(sql, name), limit = bounded(input.limit, 2000, 5000);
  const size = (c: string) => 'COALESCE(length(CAST(' + quote(c) + ' AS BLOB)),0)';
  const select = cols.map((c, i) => 'SUM(' + size(c) + ') c' + i).concat(idx.map((x, i) => 'SUM(' + (x.columns.map(size).join('+') || '0') + '+8) i' + i));
  const row = sql.exec<any>('SELECT COUNT(*) n,MIN(rowid) first,MAX(rowid) last,' + select.join(',') + ' FROM (SELECT rowid,* FROM ' + quote(name) + ' WHERE rowid>? ORDER BY rowid LIMIT ?)', after(input.after), limit).one();
  const column_bytes = Object.fromEntries(cols.map((c, i) => [c, row['c' + i] || 0]));
  return {
    table: name, rows: row.n, first_rowid: row.first, last_rowid: row.last, done: row.n < limit,
    bytes: Object.values(column_bytes).reduce((a: number, b: any) => a + Number(b), 0), column_bytes,
    index_bytes: Object.fromEntries(idx.map((x, i) => [x.name, row['i' + i] || 0])),
  };
}

// Recovery manifests and exports live in private R2, outside the full database.
// They have no expiry metadata, so normal retention never removes them.
export interface Manifest { id: string; repository: string; created: number; bookmark: string | null; database_size: number; revision: number; tables: Record<string, TableExport> }
export interface TableExport { started: number; upper: number; after: number; rows: number; pages: { key: string; sha256: string; rows: number; first: number; last: number }[]; complete: boolean; verified: boolean; verified_pages: number; verified_at?: number }
export const manifestKey = (repo: string, id: string) => `recovery/${repo}/${id}.json`;
export async function readManifest(bucket: R2Bucket, repo: string, id: unknown): Promise<Manifest> {
  if (typeof id !== 'string' || !/^[0-9]{13}-[0-9a-f-]{36}$/.test(id)) throw new AdminError('invalid_recovery');
  const object = await bucket.get(manifestKey(repo, id));
  if (!object) throw new AdminError('unknown_recovery');
  return object.json();
}
export async function createRecovery(bucket: R2Bucket, storage: DurableObjectStorage, sql: SqlStorage, repo: string, revision: number): Promise<Manifest> {
  let bookmark: string | null = null;
  try { bookmark = await storage.getCurrentBookmark(); } catch { bookmark = null; }
  const manifest: Manifest = { id: Date.now() + '-' + crypto.randomUUID(), repository: repo, created: Date.now(), bookmark, database_size: sql.databaseSize, revision, tables: {} };
  await bucket.put(manifestKey(repo, manifest.id), JSON.stringify(manifest));
  return manifest;
}
export async function exportPage(bucket: R2Bucket, sql: SqlStorage, repo: string, input: any): Promise<any> {
  const manifest = await readManifest(bucket, repo, input.recovery), name = table(sql, input.table);
  const state = manifest.tables[name] ||= { started: Date.now(), upper: sql.exec<any>('SELECT COALESCE(MAX(rowid),0) n FROM ' + quote(name)).one().n, after: 0, rows: 0, pages: [], complete: false, verified: false, verified_pages: 0 };
  if (state.complete) return { table: name, ...state, pages: state.pages.length };
  const cols = columns(sql, name), limit = bounded(input.limit, 1000, 5000);
  const size = cols.map(c => 'COALESCE(length(CAST(' + quote(c) + ' AS BLOB)),0)').join('+');
  const candidates = sql.exec<any>('SELECT rowid id,' + size + ' bytes FROM ' + quote(name) + ' WHERE rowid>? AND rowid<=? ORDER BY rowid LIMIT ?', state.after, state.upper, limit).toArray();
  if (!candidates.length) state.complete = true;
  else {
    let bytes = 0, last = state.after;
    for (const row of candidates) { if (last !== state.after && bytes + row.bytes > 1000000) break; bytes += row.bytes; last = row.id; }
    const rows = sql.exec('SELECT rowid AS __rowid,* FROM ' + quote(name) + ' WHERE rowid>? AND rowid<=? ORDER BY rowid', state.after, last).toArray();
    const text = JSON.stringify(rows), sha256 = await digest(text), key = `recovery/${repo}/${manifest.id}/${name}/${state.after + 1}-${last}.json`;
    await bucket.put(key, text, { sha256, customMetadata: { kind: 'recovery', sha256 } });
    state.pages.push({ key, sha256, rows: rows.length, first: state.after + 1, last });
    state.rows += rows.length; state.after = last;
    if (last >= state.upper) state.complete = true;
  }
  state.verified = false;
  await bucket.put(manifestKey(repo, manifest.id), JSON.stringify(manifest));
  return { table: name, upper: state.upper, after: state.after, rows: state.rows, pages: state.pages.length, complete: state.complete };
}
// Re-read every exported page and check its digest before any deletion.
export async function verifyExport(bucket: R2Bucket, repo: string, input: any): Promise<any> {
  const manifest = await readManifest(bucket, repo, input.recovery), state = manifest.tables[input.table];
  if (!state) throw new AdminError('table_not_exported');
  if (!state.complete) throw new AdminError('export_incomplete');
  const stop = Math.min(state.pages.length, state.verified_pages + 50);
  for (; state.verified_pages < stop; state.verified_pages++) {
    const page = state.pages[state.verified_pages], object = await bucket.get(page.key);
    if (!object) throw new AdminError('export_page_missing');
    const text = await object.text();
    if (await digest(text) !== page.sha256 || JSON.parse(text).length !== page.rows) throw new AdminError('export_page_corrupt');
  }
  state.verified = state.verified_pages === state.pages.length;
  if (state.verified) state.verified_at = Date.now();
  await bucket.put(manifestKey(repo, manifest.id), JSON.stringify(manifest));
  return { table: input.table, verified: state.verified, verified_pages: state.verified_pages, pages: state.pages.length, rows: state.rows, upper: state.upper, bookmark: manifest.bookmark };
}

// Only these classes may be deleted. Ephemeral classes follow the existing
// retention policy; the others require a verified export covering the rows.
// PR histories, resources, membership/coverage, tombstones, jobs, and
// delivery receipts are never cleanup classes.
export const CLEANUP: Record<string, { table: string; where: string; bound: (now: number, input: any) => number; recovery: boolean }> = {
  expired_cursors: { table: 'cursors', where: 'expires<?', bound: now => now, recovery: false },
  expired_repair_metrics: { table: 'repair_metrics', where: 'at<?', bound: now => now - 7 * 86400000, recovery: false },
  expired_changes: { table: 'changes', where: 'at<?', bound: now => now - 7 * 86400000, recovery: false },
  expired_retired_objects: { table: 'retired_objects', where: 'at<?', bound: now => now - 30 * 86400000, recovery: false },
  // Expendable telemetry: archive first, then delete rows older than an hour.
  repair_metrics: { table: 'repair_metrics', where: 'at<?', bound: (now, input) => Math.min(now - 3600000, Number.isSafeInteger(input.before) ? input.before : now - 86400000), recovery: true },
  // Demand older than its five-minute scheduling window is equivalent to absence.
  stale_demand: { table: 'demand', where: 'at<?', bound: now => now - 86400000, recovery: true },
};
// A verified export covers rows with rowid <= upper that were last written
// before it started. Rowids can be reused, so both bounds are required.
export function deleteClass(sql: SqlStorage, name: string, exported: { upper: number; started: number } | null, input: any, now = Date.now(), measure = true): { rows: number; logical_bytes: number | null } {
  const rule = CLEANUP[name], limit = bounded(input.limit, 100, 500);
  // Measuring reads every deleted payload; routine expiry skips that cost.
  const size = measure ? columns(sql, rule.table).map(c => 'COALESCE(length(CAST(' + quote(c) + ' AS BLOB)),0)').join('+') : '0';
  const bound = exported === null ? rule.bound(now, input) : Math.min(rule.bound(now, input), exported.started);
  const scope = rule.where + (exported === null ? '' : ' AND rowid<=?'), bindings = exported === null ? [bound] : [bound, exported.upper];
  // Rowid order finds the oldest expired rows first and bounds every statement.
  const ids = sql.exec<any>('SELECT rowid id,' + size + ' bytes FROM ' + quote(rule.table) + ' WHERE ' + scope + ' ORDER BY rowid LIMIT ?', ...bindings, limit).toArray();
  for (let i = 0; i < ids.length; i += 50) {
    const chunk = ids.slice(i, i + 50).map(r => r.id);
    sql.exec('DELETE FROM ' + quote(rule.table) + ' WHERE rowid IN (' + chunk.map(() => '?').join(',') + ')', ...chunk);
  }
  return { rows: ids.length, logical_bytes: measure ? ids.reduce((a, r) => a + r.bytes, 0) : null };
}
