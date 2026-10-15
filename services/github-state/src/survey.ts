import spec from '../../../tauceti_worker/survey_contract.json';

export const SURVEY_CONTRACT=spec.id;
const object=(v:any)=>v!==null && typeof v==='object' && !Array.isArray(v);
const parse=(text:string)=>{try{return JSON.parse(text);}catch{return null;}};
const matches=(pattern:string,body:string)=>[...body.matchAll(new RegExp(pattern,'g'))];
export function targetFocuses(body:string): string[] {
  return [...new Set(matches(spec.target_pattern,body || '').map(m=>parse(m[1])?.focus).filter(v=>typeof v==='string' && !['','any','auto'].includes(v)))].sort();
}
export function roadmapFocuses(labels:string[],targets:string[]=[],branch=''): string[] | null {
  const marked=labels.filter(l=>l.startsWith('roadmap/')),areas=marked.filter(l=>!['roadmap/','roadmap/none','roadmap/Unknown'].includes(l));
  if(areas.length) return [...new Set(areas.map(l=>l.slice(8)))];
  if(marked.includes('roadmap/Unknown')) return null;
  if(marked.includes('roadmap/none')) return [];
  if(targets.length) return targets;
  return !marked.length && branch.startsWith('roadmap/')?null:[];
}
export function inScope(row:any,roadmap='any',skip:string[]=[]): boolean {
  const areas=roadmapFocuses((row.labels || []).map((l:any)=>l.name),row.target_focuses || targetFocuses(row.body),row.headRefName);
  if(!areas?.length) return true;
  return !['','any','auto'].includes(roadmap)?areas.includes(roadmap):areas.some(a=>!skip.includes(a));
}
export function compactMeta(value:any): any {
  if(!object(value)) return null;
  const out:any=Object.fromEntries(spec.meta_fields.filter(k=>Object.hasOwn(value,k)).map(k=>[k,value[k]]));
  if(Array.isArray(out.runs)) out.runs=out.runs.filter(object).map((r:any)=>Object.fromEntries(spec.run_fields.filter(k=>Object.hasOwn(r,k)).map(k=>[k,r[k]])));
  return out;
}
// Project once on each comment mutation; never retain prose in this index.
export function issueFragment(c:any): any {
  const body=c.body || '',meta=matches(spec.meta_pattern,body),claim=matches(spec.claim_pattern,body)[0];
  const parsed=meta.length?parse(meta[meta.length-1][1]):null;
  const value=claim?parse(claim[1]):null;
  const marker=object(value) && Number.isSafeInteger(value.expires_at)?{head:value.head ?? null,expires_at:value.expires_at,providers:Array.isArray(value.providers)?value.providers.filter((p:any)=>typeof p==='string'):[]}:null;
  const rebase=spec.rebase_authors.includes(c.user?.login) && body.startsWith('Merge-queue recovery for head `')?body.split(/\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]/).flatMap((line:string)=>line.match(/^<!--tauceti-rebase:v1 ([a-f0-9]{40})-->$/)?.slice(1) || []):[];
  return {id:String(c.id),updated:c.updated_at || '',meta:body.includes(spec.scoreboard)?compactMeta(parsed):null,marker,rebase_heads:rebase};
}
export function reviewFragment(c:any): any {
  const body=c.body || '',root=c.in_reply_to_id==null?matches(spec.rubric_pattern,body)[0]?.[1]:null;
  return {id:String(c.id),parent:c.in_reply_to_id==null?null:String(c.in_reply_to_id),root:root || null,reply:!body.includes('tauceti-reply:') && !body.includes('tauceti-rubric:'),created_at:c.created_at ?? null};
}
export function summarizeFragments(issues:any[],reviews:any[]): any {
  const sorted=[...issues].sort((a,b)=>a.updated<b.updated?1:a.updated>b.updated?-1:0);
  const meta=sorted.find(c=>c.meta!==null)?.meta ?? null;
  const roots=new Map(reviews.filter(c=>c.root).map(c=>[c.id,c.root]));
  let contest:any=null;
  for(const c of reviews) {
    const rubric=roots.get(c.parent);
    if(rubric && c.reply && /^[0-9]+$/.test(c.id) && (!contest || BigInt(c.id)>BigInt(contest.id))) contest={id:c.id,rubric,created_at:c.created_at};
  }
  return {meta,markers:issues.filter(c=>c.marker).map(c=>c.marker),contest,rebase_heads:[...new Set(issues.flatMap(c=>c.rebase_heads))].sort()};
}
export function summarize(issues:any[],reviews:any[]): any {return summarizeFragments(issues.map(issueFragment),reviews.map(reviewFragment));}
export function compactPR(row:any): any {
  const {body,statusCheckRollup,...rest}=row;
  // Only fields consumed by PRInfo; exclude history, descriptions and diff data.
  const keys=['number','headRefOid','headRefName','headRepositoryOwner','headRepository','isDraft','mergeable','author','title','labels','updatedAt','state'];
  return {...Object.fromEntries(keys.filter(k=>Object.hasOwn(rest,k)).map(k=>[k,rest[k]])),target_focuses:row.target_focuses || targetFocuses(body),statusCheckRollup:statusCheckRollup || []};
}
