// S2 source protocol. This is deliberately separate from observational recovery
// exports: only a drained, fenced repository may produce a migration archive.
import { AdminError, REQUIRED_TABLES, userTables } from './storage';
import { digest } from './protocol';

export const migrationKey = (repo: string) => `control/${repo}/migration.json`;
export const archiveKey = (repo: string, generation: string) => `migrations/${repo}/${generation}`;
const quote = (name: string) => '"' + name.replace(/"/g, '""') + '"';
const ZERO = '0'.repeat(64);
export type Cell = [string, string | null];
export type Row = [string, ...Cell[]];
interface Checkpoint { after: string | null; rows: number; hash: string; pages: number }
interface TableState extends Checkpoint {
  columns: { name: string; type: string; pk: number; notnull: number; dflt_value: string | null }[];
  count: number; complete: boolean; verified: boolean; check: Checkpoint;
}
export interface Migration {
  version: 1; generation: string; repository: string; target: string;
  source: { object: string; deployment: string; schema: number };
  phase: 'freezing' | 'exporting' | 'verifying' | 'source_verified' | 'abandoned';
  created: number; updated: number; bookmark?: string; revision?: string;
  tables: Record<string, TableState>; failure: string | null;
  blobs: { prefix: number; cursor?: string; pages: number; objects: number; hash: string; complete: boolean };
}
const fresh = (): Checkpoint => ({ after: null, rows: 0, hash: ZERO, pages: 0 });
export function generation(value: unknown): string {
  if (typeof value !== 'string' || !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(value)) throw new AdminError('invalid_generation');
  return value;
}
export function targetIdentity(value: unknown): string {
  // An opaque project/branch/database identity, never a connection string.
  if (typeof value !== 'string' || !/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,119}$/.test(value)) throw new AdminError('invalid_target_identity');
  return value;
}
export async function readMigration(bucket: R2Bucket, repo: string): Promise<Migration | null> {
  const object = await bucket.get(migrationKey(repo));
  if(!object) return null;
  const state=await object.json<Migration>();
  if(state.version!==1 || state.repository!==repo || !['freezing','exporting','verifying','source_verified','abandoned'].includes(state.phase) || !state.source?.object || !state.source.deployment || !state.tables || !state.blobs) throw new AdminError('migration_control_corrupt');
  generation(state.generation); targetIdentity(state.target);
  return state;
}
export function expectedTables(sql: SqlStorage): string[] {
  const actual = userTables(sql), expected = [...REQUIRED_TABLES].sort();
  if (JSON.stringify(actual) !== JSON.stringify(expected)) throw new AdminError('migration_table_manifest_mismatch');
  return actual;
}
export function schema(sql: SqlStorage, name: string): TableState['columns'] {
  return sql.exec<any>('PRAGMA table_info(' + quote(name) + ')').toArray().map(c => ({ name: c.name, type: c.type, pk: c.pk, notnull: c.notnull, dflt_value: c.dflt_value }));
}
export async function saveMigration(bucket: R2Bucket, state: Migration): Promise<void> {
  state.updated = Date.now();
  // The control record is the checkpoint. A failed write leaves the previous
  // checkpoint valid; deterministic page keys can safely be written again.
  await bucket.put(migrationKey(state.repository), JSON.stringify(state));
}
export function sourceRevision(sql: SqlStorage): string {
  const row = sql.exec<any>("SELECT value FROM meta WHERE key='revision'").toArray()[0];
  return row?.value ?? '0';
}
export async function beginExport(bucket: R2Bucket, storage: DurableObjectStorage, sql: SqlStorage, state: Migration): Promise<void> {
  const tables = expectedTables(sql);
  await storage.sync();
  const bookmark = await storage.getCurrentBookmark();
  if (!bookmark) throw new AdminError('migration_bookmark_unavailable');
  state.bookmark = bookmark;
  state.revision = sourceRevision(sql);
  for (const name of tables) state.tables[name] = {
    ...fresh(), columns: schema(sql, name), count: sql.exec<any>('SELECT COUNT(*) n FROM ' + quote(name)).one().n,
    complete: false, verified: false, check: fresh(),
  };
  state.phase = 'exporting';
  await saveMigration(bucket, state);
}
// Every value is typed. SQLite INTEGERs and rowids cross JavaScript as decimal
// strings, preserving all signed 64-bit values. JSON text is never reserialized.
export function sourceRows(sql: SqlStorage, name: string, columns: TableState['columns'], after: string | null, limit: number): Row[] {
  const selects = columns.flatMap((c, i) => {
    const col = quote(c.name);
    return [`typeof(${col}) AS t${i}`, `CASE typeof(${col}) WHEN 'blob' THEN hex(${col}) WHEN 'real' THEN printf('%!.17g',${col}) ELSE CAST(${col} AS TEXT) END AS v${i}`];
  });
  // Read lengths first so neither many large rows nor maliciously large input
  // limits allocate an unbounded result. Include one large row for progress.
  const sizes = columns.map(c => `COALESCE(length(CAST(${quote(c.name)} AS BLOB)),0)`).join('+');
  const candidates = sql.exec<any>(`SELECT CAST(rowid AS TEXT) id,${sizes} bytes FROM ${quote(name)} WHERE (? IS NULL OR rowid>CAST(? AS INTEGER)) ORDER BY rowid LIMIT ?`, after, after, limit).toArray();
  if (!candidates.length) return [];
  let bytes = 0, last = candidates[0].id;
  for (const row of candidates) {
    if (bytes && bytes + row.bytes > 512000) break;
    bytes += row.bytes; last = row.id;
  }
  return sql.exec<any>(`SELECT CAST(rowid AS TEXT) id,${selects.join(',')} FROM ${quote(name)} WHERE (? IS NULL OR rowid>CAST(? AS INTEGER)) AND rowid<=CAST(? AS INTEGER) ORDER BY rowid`, after, after, last).toArray().map(row => [row.id, ...columns.map((_, i): Cell => [row['t' + i], row['v' + i]])]);
}
async function chain(hash: string, rows: Row[]): Promise<string> {
  for (const row of rows) hash = await digest(hash + '\n' + JSON.stringify(row));
  return hash;
}
function references(rows: Row[]): string[] {
  const keys = new Set<string>();
  for (const row of rows) for (const cell of row.slice(1) as Cell[]) {
    if (cell[0] !== 'text' || !cell[1]?.startsWith('{')) continue;
    try { const value = JSON.parse(cell[1]); if (typeof value?.$r2 === 'string') keys.add(value.$r2); } catch { /* Not JSON. */ }
  }
  return [...keys].sort();
}
export async function exportStep(bucket: R2Bucket, sql: SqlStorage, state: Migration, requestedLimit: unknown): Promise<void> {
  if (state.phase !== 'exporting') return;
  const name = Object.keys(state.tables).find(n => !state.tables[n].complete);
  if (!name) { state.phase = 'verifying'; await saveMigration(bucket, state); return; }
  const table = state.tables[name];
  const limit = Number.isSafeInteger(requestedLimit) && Number(requestedLimit) > 0 ? Math.min(Number(requestedLimit), 250) : 100;
  const rows = sourceRows(sql, name, table.columns, table.after, limit);
  if (!rows.length) {
    if (table.rows !== table.count) throw new AdminError('migration_source_count_changed');
    table.complete = true;
  } else {
    const hash = await chain(table.hash, rows), refs = references(rows);
    const page = JSON.stringify({ version: 1, table: name, index: table.pages, before: table.after, rows, refs, hash });
    const sha256 = await digest(page);
    await bucket.put(`${archiveKey(state.repository, state.generation)}/${name}/${table.pages}.json`, page, { sha256, customMetadata: { kind: 'migration-page', sha256 } });
    table.after = rows.at(-1)![0]; table.rows += rows.length; table.hash = hash; table.pages++;
  }
  await saveMigration(bucket, state);
}
export async function verifyStep(bucket: R2Bucket, sql: SqlStorage, state: Migration): Promise<void> {
  if (state.phase !== 'verifying') return;
  if (sourceRevision(sql) !== state.revision) throw new AdminError('migration_revision_changed');
  expectedTables(sql);
  const name = Object.keys(state.tables).find(n => !state.tables[n].verified);
  if (!name) { await blobStep(bucket, state); return; }
  const table = state.tables[name], check = table.check;
  if (JSON.stringify(schema(sql, name)) !== JSON.stringify(table.columns)) throw new AdminError('migration_schema_changed');
  if (check.pages === table.pages) {
    if (check.rows !== table.count || check.hash !== table.hash || sql.exec<any>('SELECT COUNT(*) n FROM ' + quote(name)).one().n !== table.count) throw new AdminError('migration_checksum_mismatch');
    table.verified = true;
  } else {
    const object = await bucket.get(`${archiveKey(state.repository, state.generation)}/${name}/${check.pages}.json`);
    if (!object) throw new AdminError('migration_page_missing');
    const text = await object.text();
    if (await digest(text) !== object.customMetadata?.sha256) throw new AdminError('migration_page_corrupt');
    const page: { table: string; index: number; before: string | null; rows: Row[]; refs: string[]; hash: string } = JSON.parse(text);
    if (page.table !== name || page.index !== check.pages || page.before !== check.after || !page.rows.length || page.rows.length > 250) throw new AdminError('migration_page_corrupt');
    const source = sourceRows(sql, name, table.columns, check.after, page.rows.length);
    if (JSON.stringify(source) !== JSON.stringify(page.rows)) throw new AdminError('migration_source_mismatch');
    if (JSON.stringify(references(page.rows)) !== JSON.stringify(page.refs)) throw new AdminError('migration_reference_mismatch');
    for (const key of page.refs) if (!await bucket.head(key)) throw new AdminError('migration_blob_missing');
    check.hash = await chain(check.hash, page.rows);
    if (check.hash !== page.hash) throw new AdminError('migration_checksum_mismatch');
    check.after = page.rows.at(-1)![0]; check.rows += page.rows.length; check.pages++;
  }
  await saveMigration(bucket, state);
}
// Inventory cursor and staging blobs as well as resources. Staging is addressed
// implicitly by (job key, started, page), not only by explicit $r2 references.
async function blobStep(bucket: R2Bucket, state: Migration): Promise<void> {
  const prefixes = ['resources/', 'cursors/', 'staging/'].map(p => p + state.repository + '/'), check = state.blobs;
  if (check.prefix === prefixes.length) {
    check.complete = true; state.phase = 'source_verified';
    await saveMigration(bucket, state);
    // Retained receipt, separate from the mutable current-generation control.
    await bucket.put(archiveKey(state.repository, state.generation) + '/receipt.json', JSON.stringify(state));
    return;
  }
  const page = await bucket.list({ prefix: prefixes[check.prefix], cursor: check.cursor, limit: 100, include: ['customMetadata'] });
  const objects = page.objects.map(o => ({ key: o.key, etag: o.etag, size: o.size, expires: o.customMetadata?.expires ?? null }));
  const text = JSON.stringify(objects), sha256 = await digest(text);
  await bucket.put(`${archiveKey(state.repository, state.generation)}/blobs/${check.pages}.json`, text, { sha256, customMetadata: { sha256 } });
  check.hash = await digest(check.hash + '\n' + sha256); check.pages++; check.objects += objects.length;
  if (page.truncated) check.cursor = page.cursor;
  else { check.prefix++; delete check.cursor; }
  await saveMigration(bucket, state);
}
export function publicReceipt(state: Migration | null): unknown {
  if (!state) return { migration: null };
  // Do not return SQL cells, R2 reference keys, query arguments, or secrets.
  return { generation: state.generation, repository: state.repository, source: state.source, target: state.target,
    phase: state.phase, bookmark: state.bookmark, revision: state.revision, created: state.created, updated: state.updated, failure: state.failure,
    tables: Object.fromEntries(Object.entries(state.tables).map(([name, t]) => [name, { count: t.count, rows: t.rows, after: t.after, pages: t.pages, hash: t.hash, complete: t.complete, verified: t.verified, verified_pages: t.check.pages }])),
    blobs: state.blobs, write_owner: 'sqlite', production_cutover_supported: false };
}
