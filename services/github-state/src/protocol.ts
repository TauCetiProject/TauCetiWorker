export interface Repository { id: string; name: string; installation: string }
export interface Env {
  REPOSITORIES: DurableObjectNamespace; INSTALLATIONS: DurableObjectNamespace;
  ARCHIVE: R2Bucket; EVENTS: Queue<EventPointer>;
  ALLOWED_REPOSITORIES: string; GITHUB_APP_ID: string; GITHUB_INSTALLATION_ID: string;
  GITHUB_PRIVATE_KEY: string; WEBHOOK_SECRET: string;
  CLIENT_TOKENS: string; ADMIN_TOKEN_HASH: string;
  RECONCILIATION_SETTINGS?: string; REFERENCE_READS?: string;
  CF_VERSION?: {id:string};
  BORS_ACTIVITY: Fetcher;
}
export interface EventPointer { key: string; delivery: string; event: string; repository: string; received: number }
export interface Query { repository: string; operation: string; args?: Record<string, string | number>; cursor?: string; allow_observed?: boolean }
export interface Result {
  version: 1; repository: string; operation: string; status: 'ok' | 'unavailable';
  data?: unknown; revision: number; observed_at?: number; verified_at?: number; complete: boolean; cursor?: string;
  reason?: string;
  coverage?: 'complete' | 'webhook_observed';
  contract?: string;
  author_counts?: Record<string,number>;
}
export function json(value: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return Response.json(value, { status, headers: { 'Cache-Control': 'private, no-cache', ...headers } });
}
// GitHub REST database identifiers can exceed JavaScript's exact integer range.
// The reviver source retains the original digits before Number rounds them.
export function parseGitHub(text: string): any {
  return (JSON.parse as any)(text, (_key:string, value:any, context?:{source?:string}) =>
    typeof value==='number' && Number.isInteger(value) && !Number.isSafeInteger(value) && /^-?[0-9]+$/.test(context?.source || '') ? context!.source : value);
}
export function repositories(env: Env): Repository[] {
  const rows = JSON.parse(env.ALLOWED_REPOSITORIES || '[]');
  if (!Array.isArray(rows)) throw new Error('invalid repository configuration');
  return rows;
}
export function repository(env: Env, name: string): Repository | undefined {
  return repositories(env).find(r => r.name === name || r.id === name);
}
export async function digest(value: string | ArrayBuffer): Promise<string> {
  const bytes = typeof value === 'string' ? new TextEncoder().encode(value) : value;
  return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), b => b.toString(16).padStart(2, '0')).join('');
}
export function constantEqual(a: string, b: string): boolean {
  let difference = a.length ^ b.length;
  for (let i = 0; i < Math.max(a.length, b.length); i++) difference |= (a.charCodeAt(i) || 0) ^ (b.charCodeAt(i) || 0);
  return difference === 0;
}
export async function authorize(req: Request, env: Env, repo?: string, admin = false): Promise<boolean> {
  const token = req.headers.get('Authorization')?.match(/^Bearer (.+)$/)?.[1];
  if (!token) return false;
  const hash = await digest(token);
  if (admin) return Boolean(env.ADMIN_TOKEN_HASH) && constantEqual(hash, env.ADMIN_TOKEN_HASH);
  const clients = JSON.parse(env.CLIENT_TOKENS || '[]') as { hash: string; repositories: string[] }[];
  return clients.some(c => constantEqual(hash, c.hash) && (!repo || c.repositories.includes(repo)));
}
export async function validSignature(body: ArrayBuffer, signature: string | null, secret: string): Promise<boolean> {
  if (!secret || !/^sha256=[a-f0-9]{64}$/.test(signature || '')) return false;
  const key = await crypto.subtle.importKey('raw', new TextEncoder().encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  const mac = await crypto.subtle.sign('HMAC', key, body);
  const expected = 'sha256=' + Array.from(new Uint8Array(mac), b => b.toString(16).padStart(2, '0')).join('');
  return constantEqual(expected, signature!);
}
export const OPERATIONS = new Set(['survey', 'open_prs', 'open_issues', 'recent_prs', 'recent_issues', 'pr', 'files', 'issue_comments', 'review_comments', 'review_threads', 'reviews', 'reactions', 'statuses', 'checks', 'workflow_runs', 'workflow_jobs', 'refs', 'repository', 'progress']);
export function validateQuery(q: Query): void {
  if (!q || typeof q.repository !== 'string' || !OPERATIONS.has(q.operation) || (q.args && (typeof q.args !== 'object' || Array.isArray(q.args)))) throw new Error('invalid query');
  if (q.allow_observed!==undefined && (typeof q.allow_observed!=='boolean' || q.operation!=='workflow_jobs')) throw new Error('invalid observed coverage preference');
  for (const [key, value] of Object.entries(q.args || {})) {
    if(q.operation==='survey') {
      if(!['roadmap','skip','numbers','stages'].includes(key) || typeof value!=='string' || value.length>4096) throw new Error('invalid survey scope');
      if(key==='numbers' && !/^(?:[1-9][0-9]*(?:,[1-9][0-9]*)*)?$/.test(value)) throw new Error('invalid survey targets');
      continue;
    }
    if (!['number', 'id', 'sha'].includes(key)) throw new Error('unsupported argument');
    if (key === 'sha' ? !/^[a-f0-9]{40}$/.test(String(value)) : !/^[1-9][0-9]*$/.test(String(value))) throw new Error('invalid argument');
  }
  const required = ['pr','files','issue_comments','review_comments','review_threads','reviews','progress'].includes(q.operation) ? 'number' : ['reactions','workflow_jobs'].includes(q.operation) ? 'id' : ['statuses','checks'].includes(q.operation) ? 'sha' : null;
  if (required && !q.args?.[required]) throw new Error('missing argument');
}
export function queryKey(q: Pick<Query, 'operation' | 'args'>): string {
  return q.operation + ':' + JSON.stringify(Object.fromEntries(Object.entries(q.args || {}).sort(([a], [b]) => a.localeCompare(b))));
}
export function stub(ns: DurableObjectNamespace, id: string): DurableObjectStub { return ns.get(ns.idFromName(id)); }
