import { Query } from './protocol';
import { comment, issue, pr, status } from './normalize';
import catalog from '../../../tauceti_worker/github_catalog.json';
export interface Recipe { api: 'core' | 'graphql'; path: string; query?: string; variables?: Record<string, unknown>; cost: number; connection?: string[]; normalize: (x: any) => any; collection: boolean }
const PR = catalog.fields.pr;
const COMMENT = catalog.fields.comment;
const PAGE = 'pageInfo{hasNextPage endCursor}';
export function batchLimit(operation:string): number {
  return (catalog.batch_limits as Record<string,number>)[operation] || 0;
}
export function batchQuery(repository:string,jobs:any[],options:{threadPage?:number,commentPage?:number,allowLarge?:boolean}={}) {
  if (!jobs.length) throw new Error('empty batch');
  const operation=jobs[0].query.operation,limit=options.allowLarge?100:batchLimit(operation);
  if (jobs.length>limit || !['issue_comments','review_threads','statuses','reactions'].includes(operation)) throw new Error('invalid batch operation');
  const threadPage=options.threadPage || 10,commentPage=options.commentPage || 20;
  if (![threadPage,commentPage].every(n=>Number.isInteger(n) && n>=1 && n<=100)) throw new Error('invalid connection size');
  const [owner,name]=repository.split('/');
  const selection=jobs.map((j:any,i:number)=>{
    if (j.query.operation!==operation) throw new Error('mixed batch');
    const number=j.query.args?.number;
    if (operation==='reactions') {
      if (!j.node_id || !/^[A-Za-z0-9_+=/-]+$/.test(j.node_id)) throw new Error('missing reaction node identity');
      return `q${i}:node(id:${JSON.stringify(j.node_id)}){... on PullRequestReviewComment{reactions(first:100){${PAGE} nodes{${catalog.fields.reaction}}}}}`;
    }
    if (operation==='statuses') {
      if (!/^[a-f0-9]{40}$/.test(j.query.args?.sha)) throw new Error('invalid head');
      return `q${i}:object(expression:${JSON.stringify(j.query.args.sha)}){... on Commit{status{contexts{context state createdAt}}}}`;
    }
    if (!/^[1-9][0-9]*$/.test(String(number))) throw new Error('invalid history number');
    if (operation==='review_threads') return `q${i}:pullRequest(number:${number}){reviewThreads(first:${threadPage}){${PAGE} nodes{id isResolved isOutdated comments(first:${commentPage}){${PAGE} nodes{${catalog.fields.review_comment}}}}}}`;
    const fields=`comments(first:100){${PAGE} nodes{${COMMENT}}}`;
    return `q${i}:issueOrPullRequest(number:${number}){... on Issue{${fields}} ... on PullRequest{${fields}}}`;
  }).join(' ');
  return {query:operation==='reactions'?`query{rateLimit{cost remaining resetAt} ${selection}}`:`query($owner:String!,$name:String!){rateLimit{cost remaining resetAt} repository(owner:$owner,name:$name){${selection}}}`,variables:operation==='reactions'?{}:{owner,name}};
}
export function recipes(repository: string, q: Query, cursor?: string, page = 1): Recipe[] {
  const [owner, name] = repository.split('/');
  const args = q.args || {};
  const root = '/repos/' + repository;
  const rest = (path: string, normalize = (x: any) => x, collection = true): Recipe => ({api:'core', path: root + path, cost:1, normalize, collection});
  const gql = (fields: string, connection: string[], normalize = (x: any) => x, collection = true, extra: Record<string, unknown> = {}, cost = 1): Recipe => ({api:'graphql',path:'/graphql',query:`query($owner:String!,$name:String!${fields.includes('$cursor')?',$cursor:String':''}${Object.keys(extra).map(k => `,$${k}:${k === 'number' ? 'Int!' : 'String!'}`).join('')}){rateLimit{cost remaining resetAt} repository(owner:$owner,name:$name){${fields}}}`,variables:{owner,name,...(fields.includes('$cursor')?{cursor}:{}),...extra},connection:['repository',...connection],cost,normalize,collection});
  const suffix = `?per_page=100&page=${page}`;
  switch (q.operation) {
    case 'pr_delta': return [gql(`pullRequests(first:100,after:$cursor,orderBy:{field:UPDATED_AT,direction:DESC}){${PAGE} nodes{${PR}}}`,['pullRequests'],pr),rest('/pulls' + suffix + '&state=all&sort=updated&direction=desc',pr)];
    case 'issue_delta': return [gql(`issues(first:100,after:$cursor,orderBy:{field:UPDATED_AT,direction:DESC}){${PAGE} nodes{number title body state updatedAt createdAt url author{login __typename} labels(first:100){nodes{name}}}}`,['issues'],issue),rest('/issues' + suffix + '&state=all&sort=updated&direction=desc',issue)];
    case 'open_prs': return [gql(`pullRequests(states:OPEN,first:100,after:$cursor,orderBy:{field:CREATED_AT,direction:ASC}){${PAGE} nodes{${PR}}}`,['pullRequests'],pr), rest('/pulls' + suffix + '&state=open&sort=created&direction=asc',pr)];
    case 'open_issues': return [gql(`issues(states:OPEN,first:100,after:$cursor,orderBy:{field:CREATED_AT,direction:ASC}){${PAGE} nodes{number title body state updatedAt createdAt url author{login __typename} labels(first:100){nodes{name}}}}`,['issues'],issue),rest('/issues' + suffix + '&state=open&sort=created&direction=asc',issue)];
    case 'recent_prs': return [rest('/pulls' + suffix + '&state=closed&sort=updated&direction=desc',pr)];
    case 'recent_issues': return [rest('/issues' + suffix + '&state=closed&sort=updated&direction=desc',issue)];
    case 'pr': return [rest('/pulls/' + args.number,pr,false),gql(`pullRequest(number:$number){${PR} additions deletions changedFiles}`,['pullRequest'],pr,false,{number:args.number})];
    case 'issue_comments': {
      const c = `comments(first:100,after:$cursor){${PAGE} nodes{${COMMENT}}}`;
      return [gql(`issueOrPullRequest(number:$number){... on Issue{${c}} ... on PullRequest{${c}}}`,['issueOrPullRequest','comments'],comment,true,{number:args.number}), rest(`/issues/${args.number}/comments` + suffix,comment)];
    }
    // REST review histories are cheaper than a nested 20x20 connection for dense threads.
    case 'review_comments': return [rest(`/pulls/${args.number}/comments` + suffix,comment)];
    case 'review_threads': return [gql(`pullRequest(number:$number){reviewThreads(first:20,after:$cursor){${PAGE} nodes{id isResolved isOutdated comments(first:20){${PAGE} nodes{${COMMENT} replyTo{fullDatabaseId} path line originalLine diffHunk commit{oid} originalCommit{oid}}}}}}`,['pullRequest','reviewThreads'], x=>x,true,{number:args.number},1)];
    case 'reviews': return [rest(`/pulls/${args.number}/reviews` + suffix)];
    case 'files': return [rest(`/pulls/${args.number}/files` + suffix)];
    case 'reactions': return [rest(`/pulls/comments/${args.id}/reactions` + suffix, x=>({...x,id:String(x.id)}))];
    case 'statuses': return [rest(`/commits/${args.sha}/status`, x => (x.statuses || []).map(status),false),gql(`object(expression:$sha){... on Commit{status{contexts{context state createdAt}}}}`,['object'], x=>(x.status?.contexts || []).map(status),false,{sha:args.sha})];
    case 'checks': return [rest(`/commits/${args.sha}/check-runs` + suffix,x=>x)];
    case 'workflow_runs': return [rest('/actions/runs' + suffix + (args.after?'&created='+encodeURIComponent('>='+args.after):''))];
    case 'workflow_jobs': return [rest(`/actions/runs/${args.id}${args.attempt?'/attempts/'+args.attempt:''}/jobs` + suffix)];
    case 'refs': return [rest('/git/matching-refs/heads/' + suffix)];
    case 'repository': return [rest('',x=>x,false)];
    default: throw new Error('no upstream recipe');
  }
}
export function extract(recipe: Recipe, payload: any): { rows: any; cursor?: string } {
  let value = payload;
  if (recipe.api === 'graphql') {
    value = payload.data;
    for (const key of recipe.connection || []) value = value?.[key];
    if (value == null) throw new Error('missing GraphQL resource');
    if (recipe.collection) {
      if (!Array.isArray(value.nodes) || !value.pageInfo) throw new Error('incomplete connection');
      // Do not silently truncate labels or nested thread comments.
      for (const node of value.nodes) {
        if (node.labels && node.labels.totalCount > node.labels.nodes.length) throw new Error('labels overflow');
      }
      return {rows:value.nodes.map(recipe.normalize),cursor:value.pageInfo.hasNextPage ? value.pageInfo.endCursor : undefined};
    }
    return {rows:recipe.normalize(value)};
  }
  if (recipe.collection) {
    if (payload.check_runs) value = payload.check_runs;
    else if (payload.workflow_runs) value = payload.workflow_runs;
    else if (payload.jobs) value = payload.jobs;
    if (!Array.isArray(value)) throw new Error('invalid REST collection');
    value = value.filter((x:any) => !(recipe.path.includes('/issues?') && x.pull_request));
    return {rows:value.map(recipe.normalize)};
  }
  return {rows:recipe.normalize(value)};
}
