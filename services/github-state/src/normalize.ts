// Canonical DTOs are gh PR JSON and REST histories. IDs stay decimal strings on the wire.
export function comment(n: any): any {
  if ('body' in n && 'created_at' in n) return { ...n, id: String(n.id), in_reply_to_id: n.in_reply_to_id == null ? null : String(n.in_reply_to_id) };
  const author = n.author || {};
  const login = author.login || '';
  return { id: String(n.fullDatabaseId), node_id: n.id, body: n.body, created_at: n.createdAt, updated_at: n.updatedAt,
    user: {login: author.__typename === 'Bot' && login && !login.endsWith('[bot]') ? login + '[bot]' : login},
    author_association: n.authorAssociation,
    ...(n.replyTo !== undefined ? { in_reply_to_id: n.replyTo ? String(n.replyTo.fullDatabaseId) : null,
      path: n.path, line: n.line, original_line: n.originalLine, diff_hunk: n.diffHunk,
      commit_id: n.commit?.oid, original_commit_id: n.originalCommit?.oid } : {}) };
}
export function pr(n: any): any {
  if ('headRefOid' in n) {
    const bot = n.author?.__typename === 'Bot';
    return { ...n, author: {login: bot && n.author?.login ? 'app/' + n.author.login : n.author?.login || '', is_bot: bot},
      labels: n.labels?.nodes || n.labels || [],
      statusCheckRollup: (n.commits?.nodes?.[0]?.commit?.status?.contexts || n.statusCheckRollup || []).map((c: any) => ({context: c.context, state: c.state, startedAt: c.createdAt || c.startedAt})) };
  }
  return { number: n.number, title: n.title, body: n.body || '', state: n.merged || n.merged_at ? 'MERGED' : String(n.state).toUpperCase(),
    isDraft: Boolean(n.draft), ...(n.mergeable===undefined?{}:{mergeable:n.mergeable === false ? 'CONFLICTING' : n.mergeable === true ? 'MERGEABLE' : 'UNKNOWN'}),
    mergedAt: n.merged_at, closedAt: n.closed_at,
    updatedAt: n.updated_at, createdAt: n.created_at, headRefOid: n.head?.sha, headRefName: n.head?.ref,
    headRepositoryOwner: {login: n.head?.repo?.owner?.login || n.head?.user?.login || ''}, headRepository: {name: n.head?.repo?.name || ''},
    author: {login: n.user?.type === 'Bot' ? 'app/' + (n.user.login || '').replace(/\[bot\]$/, '') : n.user?.login || '', is_bot: n.user?.type === 'Bot'},
    labels: n.labels || [], statusCheckRollup: [], additions: n.additions, deletions: n.deletions, changedFiles: n.changed_files,
    baseRefName: n.base?.ref, baseRefOid: n.base?.sha, url: n.html_url };
}
export function issue(n: any): any {
  return { ...n, number: n.number, title: n.title, body: n.body || '', state: String(n.state).toUpperCase(),
    labels: n.labels?.nodes || n.labels || [], updatedAt: n.updatedAt || n.updated_at, createdAt: n.createdAt || n.created_at,
    author: n.author || n.user || {}, url: n.html_url || n.url };
}
export function status(n: any): any { return { context: n.context, state: String(n.state).toUpperCase(), startedAt: n.created_at || n.createdAt || n.startedAt }; }
