import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createHmac, createHash } from 'node:crypto';
import { Miniflare, convertV4MiniflareOptions } from 'miniflare';
let mf, namespace, archive;
const token='test-client-token', admin='test-admin-token', secret='test-webhook-secret';
const sha=x=>createHash('sha256').update(x).digest('hex');
const repo={id:'10',name:'org/repo',installation:'20'};
before(async()=>{
  mf=new Miniflare(convertV4MiniflareOptions({modules:true,script:readFileSync('dist/index.js','utf8'),compatibilityDate:'2026-10-11',
    durableObjects:{REPOSITORIES:{className:'RepositoryState',useSQLite:true},INSTALLATIONS:{className:'InstallationScheduler',useSQLite:true}},
    r2Buckets:['ARCHIVE'],queueProducers:{EVENTS:'events'},
    bindings:{ALLOWED_REPOSITORIES:JSON.stringify([repo]),GITHUB_APP_ID:'',GITHUB_INSTALLATION_ID:'20',GITHUB_PRIVATE_KEY:'',WEBHOOK_SECRET:secret,CLIENT_TOKENS:JSON.stringify([{hash:sha(token),repositories:['org/repo']}]),ADMIN_TOKEN_HASH:sha(admin)}}));
  namespace=await mf.getDurableObjectNamespace('REPOSITORIES'); archive=await mf.getR2Bucket('ARCHIVE');
});
after(async()=>{await mf?.dispose();});
const request=(path,options={})=>mf.dispatchFetch('https://test'+path,options);
const headers={Authorization:'Bearer '+token};
function body(pr={}) { return {installation:{id:20},repository:{id:10,full_name:'org/repo'},action:'opened',pull_request:{id:123,number:7,title:'new',body:'text',state:'open',updated_at:'2026-10-11T00:00:00Z',head:{sha:'a'.repeat(40),ref:'feature',repo:{name:'repo',owner:{login:'org'}}},user:{login:'author'},...pr}}; }
async function ingest(id,event,payload,received=Date.now()) {
  const key='fixture/'+id; await archive.put(key,JSON.stringify(payload));
  return namespace.get(namespace.idFromName('10')).fetch('https://internal/event',{method:'POST',body:JSON.stringify({key,delivery:id,event,repository:'org/repo',received})});
}
test('public health does not expose internal quota or secrets',async()=>{
  const r=await request('/healthz'); assert.equal(r.status,200); assert.deepEqual(await r.json(),{service:'tauceti-github-state',version:1});
  assert.equal((await request('/v1/health?repository=org/repo')).status,401);
});
test('webhook rejects bad signatures and conflicting duplicate bodies',async()=>{
  const text=JSON.stringify(body()); const base={'x-github-delivery':'delivery-one','x-github-event':'pull_request'};
  assert.equal((await request('/webhook/github',{method:'POST',headers:base,body:text})).status,401);
  const signature='sha256='+createHmac('sha256',secret).update(text).digest('hex'); const good={...base,'x-hub-signature-256':signature};
  assert.equal((await request('/webhook/github',{method:'POST',headers:good,body:text})).status,202);
  assert.equal((await request('/webhook/github',{method:'POST',headers:good,body:text})).status,202);
  const other=JSON.stringify(body({title:'different'}));
  assert.equal((await request('/webhook/github',{method:'POST',headers:{...base,'x-hub-signature-256':'sha256='+createHmac('sha256',secret).update(other).digest('hex')},body:other})).status,409);
});
test('webhooks do not invent complete inventory; duplicate and old delivery are idempotent',async()=>{
  assert.equal((await ingest('new','pull_request',body())).status,200);
  const r=await request('/v1/query?repository=org/repo&operation=open_prs',{headers}); assert.equal(r.status,200);
  const p=await r.json(); assert.equal(p.status,'unavailable'); assert.equal(p.complete,false);
  const doStub=namespace.get(namespace.idFromName('10')); const first=await (await doStub.fetch('https://internal/health')).json();
  assert.equal((await ingest('new','pull_request',body())).status,200);
  await ingest('old','pull_request',body({title:'obsolete',state:'closed',updated_at:'2026-10-10T00:00:00Z'}),Date.now()-10000);
  // Background survey hydration may advance the global revision independently of these deliveries.
  const changes=await (await request('/v1/changes?repository=org/repo&after='+first.revision,{headers})).json();
  assert.equal(changes.reset,false);
  assert.deepEqual(changes.changes.filter(c=>c.kind==='pr' || c.kind==='collection'),[]);
});
test('resource tombstones advance journal without marking missing histories empty',async()=>{
  const c={id:999,body:'hello',created_at:'2026-10-11T00:00:00Z',updated_at:'2026-10-11T00:00:00Z',user:{login:'peer'}};
  await ingest('comment','issue_comment',{...body(),issue:{number:7},comment:c});
  await ingest('delete','issue_comment',{...body(),action:'deleted',issue:{number:7},comment:c});
  const changes=await (await request('/v1/changes?repository=org/repo',{headers})).json(); assert.equal(changes.changes.filter(c=>c.kind==='issue_comments' && c.id==='999').at(-1).tombstone,1);
  const q=await (await request('/v1/query?repository=org/repo&operation=issue_comments&args=%7B%22number%22%3A7%7D',{headers})).json(); assert.equal(q.status,'unavailable');
});
test('query validation, repo scoping, admin token independent',async()=>{
  assert.equal((await request('/v1/query?repository=org/repo&operation=unsafe',{headers})).status,400);
  const outside=await request('/v1/query?repository=org/private&operation=open_prs',{headers});
  assert.equal(outside.status,200);const unavailable=await outside.json();assert.equal(unavailable.status,'unavailable');assert.equal(unavailable.complete,false);assert.equal(Object.hasOwn(unavailable,'data'),false);
  assert.equal((await request('/v1/query?repository=org/private&operation=open_prs')).status,401);
  assert.equal((await request('/admin/bootstrap',{method:'POST',headers,body:JSON.stringify({repository:'org/repo'})})).status,401);
  assert.equal((await request('/admin/bootstrap',{method:'POST',headers:{Authorization:'Bearer '+admin},body:JSON.stringify({repository:'org/repo'})})).status,200);
});

test('observed workflow coverage is explicit, isolated by query preference, and unknown runs stay unavailable',async()=>{
 const args=encodeURIComponent(JSON.stringify({id:'77'}));
 assert.equal((await request('/v1/query?repository=org/repo&operation=issue_comments&args=%7B%22number%22%3A7%7D&allow_observed=true',{headers})).status,400);
 const unknown=await (await request('/v1/query?repository=org/repo&operation=workflow_jobs&args='+args+'&allow_observed=true',{headers})).json();assert.equal(unknown.status,'unavailable');
 await ingest('workflow-run','workflow_run',{workflow_run:{id:77,run_attempt:1,status:'completed',head_branch:'feature'}});
 await ingest('workflow-job','workflow_job',{workflow_job:{id:88,run_id:77,run_attempt:1,status:'completed'}});
 const observed=await (await request('/v1/query?repository=org/repo&operation=workflow_jobs&args='+args+'&allow_observed=true',{headers})).json();
 assert.equal(observed.coverage,'webhook_observed');assert.equal(observed.complete,false);assert.equal(observed.data.length,1);
 const strict=await (await request('/v1/query?repository=org/repo&operation=workflow_jobs&args='+args,{headers})).json();assert.equal(strict.status,'unavailable');
});

test('reference reads require an explicitly enabled environment and admin authentication',async()=>{
 const body=JSON.stringify({query:{repository:'org/repo',operation:'files',args:{number:7}}});
 assert.equal((await request('/admin/reference',{method:'POST',headers:{Authorization:'Bearer '+admin},body})).status,401,'production default disables reference reads');
});


test('activity observations are authenticated, repository-scoped, bounded, and tolerate missing coverage',async()=>{
 const body=JSON.stringify({repository:'org/repo',numbers:[999999]});
 assert.equal((await request('/v1/activity',{method:'POST',body})).status,401);
 assert.equal((await request('/v1/activity',{method:'POST',headers,body:JSON.stringify({repository:'org/private',numbers:[7]})})).status,403);
 for(const input of [null,{repository:'org/repo',numbers:[-1]},{repository:'org/repo',numbers:Array(501).fill(1)}])
  assert.equal((await request('/v1/activity',{method:'POST',headers,body:JSON.stringify(input)})).status,400);
 const r=await request('/v1/activity',{method:'POST',headers,body});
 assert.equal(r.status,200);const data=await r.json();assert.equal(data.coverage,'observed');assert.deepEqual(data.missing,[999999]);
});
