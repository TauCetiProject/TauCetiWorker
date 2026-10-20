import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync,readdirSync,rmSync,statSync} from 'node:fs';
import {join} from 'node:path';
import {tmpdir} from 'node:os';
import {DatabaseSync} from 'node:sqlite';
import {runtime,repository,response,send} from './runtime.mjs';

// A mirror with a two-page open inventory and complete histories for PR 1,
// served by a fake GitHub that counts every upstream request.
const count=150,head='a'.repeat(40);
const prs=Array.from({length:count},(_,i)=>({number:i+1,title:'PR '+(i+1),body:'x'.repeat(12000),state:'OPEN',isDraft:false,mergeable:'MERGEABLE',updatedAt:'2026-10-12T00:00:00Z',headRefOid:head,headRefName:'feature',author:{login:'peer',__typename:'User'},labels:{nodes:[]}}));
const network={count:0};
async function github(req,u){
 network.count++;
 if(u.pathname==='/graphql'){
  const {query,variables}=await req.json(),data={};
  if(query.includes('pullRequests(')){const offset=Number(variables.cursor || 0),next=offset+100;data.pullRequests={nodes:prs.slice(offset,next),pageInfo:{hasNextPage:next<count,endCursor:String(next)}};}
  else if(query.includes('issues(')) data.issues={nodes:[],pageInfo:{hasNextPage:false,endCursor:null}};
  else if(query.includes('pullRequest(number:')) data.pullRequest={...prs[0],additions:1,deletions:1,changedFiles:1,reviewThreads:{nodes:[],pageInfo:{hasNextPage:false,endCursor:null}}};
  else if(query.includes('issueOrPullRequest(number:')) data.issueOrPullRequest={comments:{nodes:[],pageInfo:{hasNextPage:false,endCursor:null}}};
  else if(query.includes('object(expression:$sha)')) data.object={status:{contexts:[]}};
  for(const [,alias] of query.matchAll(/(q\d+):/g)) data[alias]={status:{contexts:[]},comments:{nodes:[],pageInfo:{hasNextPage:false,endCursor:null}},reviewThreads:{nodes:[],pageInfo:{hasNextPage:false,endCursor:null}}};
  return response({data:{rateLimit:{cost:1,remaining:4999,resetAt:new Date(Date.now()+300000).toISOString()},repository:data}});
 }
 if(u.pathname.endsWith('/check-runs')) return response({check_runs:[]},'core');
 if(u.pathname.endsWith('/status')) return response({statuses:[]},'core');
 if(/\/pulls\/1$/.test(u.pathname)) return response({number:1,state:'open',title:'PR 1',head:{sha:head,ref:'feature'},base:{sha:'b'.repeat(40),ref:'main'},user:{login:'peer'},updated_at:'2026-10-12T00:00:00Z'},'core');
 return response([],'core');
}
const client={Authorization:'Bearer client'},admin={Authorization:'Bearer admin'};
const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const get=(mf,operation,args={},headers={})=>mf.dispatchFetch('https://test/v1/query?repository=org/repo&operation='+operation+'&args='+encodeURIComponent(JSON.stringify(args)),{headers:{...client,...headers}});
const adminPost=(mf,path,body)=>mf.dispatchFetch('https://test'+path,{method:'POST',headers:admin,body:JSON.stringify({repository:'org/repo',...body})});
async function until(read,check,label,ms=30000){
 const deadline=Date.now()+ms;let value;
 do {
  try {value=await read();if(check(value))return value;} catch(error) {value=String(error.cause || error);}
  await wait(200);
 } while(Date.now()<deadline);
 assert.fail(label+': '+JSON.stringify(value).slice(0,300));
}
async function paginate(mf,operation){
 const first=await (await get(mf,operation)).json();
 assert.equal(first.status,'ok');assert.equal(first.complete,true);
 let rows=[...first.data],cursor=first.cursor,pages=1;
 while(cursor){
  const page=await (await mf.dispatchFetch('https://test/v1/query?repository=org/repo&operation='+operation+'&cursor='+encodeURIComponent(cursor),{headers:client})).json();
  assert.equal(page.status,'ok',JSON.stringify(page).slice(0,200));assert.equal(page.revision,first.revision);
  rows.push(...page.data);cursor=page.cursor;pages++;
 }
 return {first,rows,pages};
}
async function event(mf,delivery,payload,attempts=1){
 const archive=await mf.getR2Bucket('ARCHIVE'),key='webhooks/2026-10-20/'+delivery+'.json';
 await archive.put(key,JSON.stringify(payload));
 const worker=await mf.getWorker();
 return worker.queue('events',[{id:'m-'+delivery,timestamp:new Date(),attempts,body:{key,delivery,event:'issue_comment',repository:repository.name,received:Date.now()}}]);
}
const comment=(id,number=1)=>({repository:{id:Number(repository.id)},installation:{id:Number(repository.installation)},action:'created',issue:{number},comment:{id,body:'note',created_at:'2026-10-20T00:00:00Z',updated_at:'2026-10-20T00:00:00Z',user:{login:'peer'}}});
const sqliteFile=(dir,id)=>{
 const walk=path=>readdirSync(path).flatMap(name=>{const full=join(path,name);return statSync(full).isDirectory()?walk(full):name===id+'.sqlite'?[full]:[];});
 return walk(dir)[0];
};

test('a full repository keeps serving stored reads, parks ingestion, recovers headroom, and spends no GitHub quota',async()=>{
 const dir=mkdtempSync(join(tmpdir(),'github-state-full-'));
 let mf=await runtime(github,{FAULT_INJECTION:'true'},{resourcePersistencePath:dir}),cron;
 try {
  const ns=await mf.getDurableObjectNamespace('REPOSITORIES'),id=ns.idFromName(repository.id).toString();
  let stub=ns.get(ns.idFromName(repository.id));
  await send(stub,'/configure',{repository:repository.name});
  // Production cron re-arms the repair alarm every minute; emulate it while seeding.
  cron=setInterval(()=>send(stub,'/tick',{repository:repository.name}).catch(()=>{}),1000);
  await until(async()=>(await (await stub.fetch('https://internal/health')).json()).bootstrap_inventory,Boolean,'bootstrap');
  await until(async()=>(await (await get(mf,'progress',{number:1})).json()),p=>p.status==='ok','progress baseline');
  await until(async()=>(await (await get(mf,'statuses',{sha:head})).json()),p=>p.status==='ok','status baseline');
  await until(async()=>(await (await get(mf,'issue_comments',{number:1})).json()),p=>p.status==='ok','history baseline');
  clearInterval(cron);
  const before=await paginate(mf,'open_prs');
  assert.equal(before.rows.length,count);assert.ok(before.pages>1,'inventory spans pages');
  const archive=await mf.getR2Bucket('ARCHIVE');
  assert.ok((await archive.list({prefix:'cursors/'+repository.id+'/'})).objects.length>0,'continuations are stored in R2');
  const etag=(await get(mf,'open_prs')).headers.get('etag');

  // Inject a full database: inserts and updates fail, reads and deletes work.
  assert.equal((await adminPost(mf,'/admin/storage/fault',{mode:'full'})).status,200);
  await wait(300);
  const quota=network.count;
  assert.equal((await get(mf,'open_prs',{},{'If-None-Match':etag})).status,304,'conditional read');
  const hit=await (await get(mf,'open_prs')).json();
  assert.equal(hit.status,'ok');assert.equal(hit.data.length,before.first.data.length,'in-memory hit');
  const statuses=await (await get(mf,'statuses',{sha:head})).json();
  assert.equal(statuses.status,'ok');assert.equal(statuses.complete,true);
  const survey=await (await get(mf,'survey',{roadmap:'any'})).json();
  assert.equal(survey.status,'ok');assert.equal(survey.data.length,count);
  const progress=await (await get(mf,'progress',{number:1})).json();
  assert.equal(progress.status,'ok');assert.equal(progress.data.head,head);
  for(const result of [progress,survey]) {assert.ok(result.observed_at>0,'stored observation time');assert.ok(result.verified_at>0,'stored verification time');}
  const unknown=await (await get(mf,'issue_comments',{number:9999})).json();
  assert.equal(unknown.status,'unavailable');assert.equal(unknown.reason,'coverage_unavailable');assert.equal(Object.hasOwn(unknown,'data'),false);
  const batch=await (await mf.dispatchFetch('https://test/v1/query_batch',{method:'POST',headers:client,body:JSON.stringify({queries:[{repository:'org/repo',operation:'open_prs',cursor:before.first.cursor},{repository:'org/repo',operation:'issue_comments',args:{number:1}}]})})).json();
  assert.equal(batch.results[0].result.status,'ok');assert.equal(batch.results[1].result.status,'ok');
  const health=await (await stub.fetch('https://internal/health')).json();
  assert.equal(health.storage.mode,'degraded');assert.equal(health.storage.degraded.reason,'storage_full');

  // Ingestion parks durably instead of spending queue retries.
  const parked=await event(mf,'full-1',comment(501));
  assert.deepEqual(parked.explicitAcks,['m-full-1']);assert.deepEqual(parked.retryMessages,[]);
  const backlog=await (await mf.dispatchFetch('https://test/admin/ingress?repository=org/repo',{headers:admin})).json();
  assert.equal(backlog.parked.count,1);assert.equal(backlog.mode,'apply');
  assert.ok(await archive.head('parked/'+repository.id+'/bodies/full-1.json'),'body pinned');

  // Diagnostics and approved cleanup are reachable at full storage.
  assert.equal((await mf.dispatchFetch('https://test/admin/storage?repository=org/repo',{headers:client})).status,401);
  const summary=await (await mf.dispatchFetch('https://test/admin/storage?repository=org/repo',{headers:admin})).json();
  assert.ok(summary.tables.some(t=>t.name==='resources'));assert.equal(summary.storage.mode,'degraded');
  const scanned=await (await adminPost(mf,'/admin/storage/scan',{table:'resources',limit:50})).json();
  assert.equal(scanned.rows,50);assert.ok(scanned.column_bytes.data>0);assert.ok('resource_parent' in scanned.index_bytes);
  assert.equal((await adminPost(mf,'/admin/storage/scan',{table:'sqlite_master'})).status,400);
  const cursors=await (await adminPost(mf,'/admin/storage/cleanup',{class:'expired_cursors'})).json();
  assert.equal(cursors.rows,0);assert.equal(cursors.recovery,null);
  assert.equal((await adminPost(mf,'/admin/storage/cleanup',{class:'resources'})).status,400,'semantic data is not a cleanup class');
  const refused=await (await adminPost(mf,'/admin/storage/cleanup',{class:'stale_demand'})).json();
  assert.equal(refused.error,'invalid_recovery');
  const recovery=await (await adminPost(mf,'/admin/storage/recovery',{})).json();
  assert.ok(recovery.bookmark);
  assert.equal((await (await adminPost(mf,'/admin/storage/cleanup',{class:'stale_demand',recovery:recovery.id})).json()).error,'recovery_not_verified');
  let exported;do exported=await (await adminPost(mf,'/admin/storage/export',{recovery:recovery.id,table:'demand'})).json(); while(!exported.complete);
  const verified=await (await adminPost(mf,'/admin/storage/verify',{recovery:recovery.id,table:'demand'})).json();
  assert.equal(verified.verified,true);
  const demand=await (await adminPost(mf,'/admin/storage/cleanup',{class:'stale_demand',recovery:recovery.id})).json();
  assert.equal(demand.rows,0,'current demand is younger than a day');
  const probe=await (await adminPost(mf,'/admin/storage/probe',{bytes:4096})).json();
  assert.equal(probe.writable,false);assert.equal(probe.reason,'storage_full');
  await send(stub,'/tick',{repository:repository.name});
  await (await mf.getWorker()).scheduled({cron:'* * * * *'});
  await wait(300);
  assert.equal(network.count,quota,'no GitHub requests while full, including the cron delivery audit');

  // Cold start with the store still full: no startup write blocks the data,
  // and fresh multi-page snapshots still paginate through R2.
  await mf.dispose();
  mf=await runtime(github,{FAULT_INJECTION:'true'},{resourcePersistencePath:dir});
  const cold=await paginate(mf,'open_prs');
  assert.equal(cold.rows.length,count);assert.equal(new Set(cold.rows.map(r=>r.number)).size,count);
  assert.equal((await get(mf,'open_prs',{},{'If-None-Match':cold.first.revision?'"'+cold.first.revision+'"':''})).status,304);
  assert.equal((await (await get(mf,'survey',{roadmap:'any'})).json()).status,'ok');
  const wrong=await (await mf.dispatchFetch('https://test/v1/query?repository=org/repo&operation=open_issues&cursor='+encodeURIComponent(cold.first.cursor),{headers:client})).json();
  assert.equal(wrong.status,'unavailable');assert.equal(wrong.reason,'cursor_query_mismatch');
  const outside=await (await mf.dispatchFetch('https://test/v1/query?repository=org/private&operation=open_prs&cursor='+encodeURIComponent(cold.first.cursor),{headers:client})).json();
  assert.equal(outside.reason,'repository_unavailable');assert.equal(Object.hasOwn(outside,'data'),false);
  assert.equal((await mf.dispatchFetch('https://test/v1/query?repository=org/repo&operation=open_prs&cursor='+encodeURIComponent(cold.first.cursor))).status,401);
  const forged=await (await mf.dispatchFetch('https://test/v1/query?repository=org/repo&operation=open_prs&cursor='+encodeURIComponent(crypto.randomUUID()+':100'),{headers:client})).json();
  assert.equal(forged.reason,'cursor_expired');

  // A store whose migration never committed still serves existing data.
  await mf.dispose();
  const db=new DatabaseSync(sqliteFile(dir,id));db.exec("DELETE FROM meta WHERE key='schema_version'");db.close();
  mf=await runtime(github,{FAULT_INJECTION:'true'},{resourcePersistencePath:dir});
  stub=(await mf.getDurableObjectNamespace('REPOSITORIES')).get((await mf.getDurableObjectNamespace('REPOSITORIES')).idFromName(repository.id));
  assert.equal((await (await stub.fetch('https://internal/health')).json()).storage.schema,'legacy');
  assert.equal((await paginate(mf,'open_prs')).rows.length,count);
  assert.equal(network.count,quota,'cold starts and recovery reads make no GitHub requests');

  // An object with no schema reports that precisely instead of empty data.
  const fresh=(await mf.getDurableObjectNamespace('REPOSITORIES')).idFromName('fresh');
  await (await mf.getR2Bucket('ARCHIVE')).put('faults/'+fresh.toString()+'.json',JSON.stringify({mode:'full'}));
  const missing=await (await send((await mf.getDurableObjectNamespace('REPOSITORIES')).get(fresh),'/query',{repository:'org/repo',operation:'open_prs'})).json();
  assert.equal(missing.error,'schema_unavailable');assert.ok(missing.missing.includes('resources'));assert.equal(missing.parkable,true);

  // Headroom returns: the operator probe ends degraded mode, the migration
  // commits, and parked events replay exactly once.
  assert.equal((await adminPost(mf,'/admin/storage/fault',{mode:'none'})).status,200);
  const writable=await (await adminPost(mf,'/admin/storage/probe',{bytes:4096})).json();
  assert.equal(writable.writable,true);assert.equal(writable.storage.schema,'current');assert.equal(writable.storage.mode,'writable');
  const replay=await (await adminPost(mf,'/admin/ingress/replay',{})).json();
  assert.equal(replay.replayed,1);
  const receipts=await (await send(stub,'/storage/deliveries',{ids:['full-1','unknown']})).json();
  assert.deepEqual(receipts.known,['full-1']);
  const archive2=await mf.getR2Bucket('ARCHIVE');
  assert.equal((await archive2.list({prefix:'parked/'+repository.id+'/'})).objects.length,0,'pointer and pinned body removed after the receipt');
  const comments=await (await send(stub,'/query',{repository:'org/repo',operation:'issue_comments',args:{number:1}})).json();
  assert.ok(comments.data.some(c=>c.id==='501'));
 } finally {clearInterval(cron);await mf.dispose();rmSync(dir,{recursive:true,force:true});}
});

test('planned migration parks every delivery without calling the repository object',async()=>{
 const mf=await runtime(github,{FAULT_INJECTION:'true'});
 try {
  const ns=await mf.getDurableObjectNamespace('REPOSITORIES'),stub=ns.get(ns.idFromName(repository.id));
  await send(stub,'/configure',{repository:repository.name});
  const paused=await (await adminPost(mf,'/admin/ingress',{mode:'parked',reason:'migration rehearsal'})).json();
  assert.equal(paused.repository.drained,true);
  // The repository object itself is the barrier, even for an in-flight consumer.
  const direct=await (await send(stub,'/event',{key:'webhooks/x.json',delivery:'direct',event:'push',repository:repository.name,received:Date.now()})).json();
  assert.equal(direct.error,'ingress_parked');assert.equal(direct.parkable,true);
  const result=await event(mf,'planned-1',comment(777));
  assert.deepEqual(result.explicitAcks,['m-planned-1']);
  assert.deepEqual((await (await send(stub,'/storage/deliveries',{ids:['planned-1']})).json()).known,[]);
  assert.equal((await (await adminPost(mf,'/admin/ingress/replay',{})).json()).stopped,'parked','replay waits for the operator');
  await adminPost(mf,'/admin/ingress',{mode:'apply'});
  assert.equal((await (await adminPost(mf,'/admin/ingress/replay',{})).json()).replayed,1);
  assert.deepEqual((await (await send(stub,'/storage/deliveries',{ids:['planned-1']})).json()).known,['planned-1']);
  // Reconciliation finds retained deliveries without receipts (queue or DLQ loss).
  const archive=await mf.getR2Bucket('ARCHIVE');
  await archive.put('webhooks/2026-10-20/lost-1.json',JSON.stringify(comment(778)),{customMetadata:{event:'issue_comment'}});
  const dry=await (await adminPost(mf,'/admin/ingress/reconcile',{date:'2026-10-20'})).json();
  assert.deepEqual(dry.missing,['lost-1']);assert.deepEqual(dry.parked,[]);
  const applied=await (await adminPost(mf,'/admin/ingress/reconcile',{date:'2026-10-20',apply:true})).json();
  assert.deepEqual(applied.parked,['lost-1']);
  assert.equal((await (await adminPost(mf,'/admin/ingress/replay',{})).json()).replayed,1);
  assert.deepEqual((await (await send(stub,'/storage/deliveries',{ids:['lost-1']})).json()).known,['lost-1']);
 } finally {await mf.dispose();}
});

test('fault injection is unavailable unless the environment enables it',async()=>{
 const mf=await runtime(github);
 try {
  const ns=await mf.getDurableObjectNamespace('REPOSITORIES');
  await send(ns.get(ns.idFromName(repository.id)),'/configure',{repository:repository.name});
  const r=await adminPost(mf,'/admin/storage/fault',{mode:'full'});
  assert.equal(r.status,400);assert.equal((await r.json()).error,'fault_injection_disabled');
 } finally {await mf.dispose();}
});
