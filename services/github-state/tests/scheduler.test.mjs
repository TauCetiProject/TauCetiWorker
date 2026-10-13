import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {build} from 'esbuild';

const bundle=await build({entryPoints:['src/repository.ts'],bundle:true,platform:'node',format:'esm',write:false,plugins:[{
 name:'durable-object-context',setup(builder){
  builder.onResolve({filter:/^cloudflare:workers$/},()=>({path:'context',namespace:'fixture'}));
  builder.onLoad({filter:/.*/,namespace:'fixture'},()=>({contents:'export class DurableObject {constructor(ctx,env){this.ctx=ctx;this.env=env}}',loader:'js'}));
 }
}]});
const {RepositoryState}=await import('data:text/javascript;base64,'+Buffer.from(bundle.outputFiles[0].text).toString('base64'));
function fixture(){
 const db=new DatabaseSync(':memory:');let alarm=null;
 const objects=new Map();
 const env={ALLOWED_REPOSITORIES:JSON.stringify([{id:'100',name:'org/repo',installation:'200'}]),ARCHIVE:{
  async put(key,text){objects.set(key,text);},async get(key){const text=objects.get(key);return text===undefined?null:{text:async()=>text,json:async()=>JSON.parse(text)};}
 }};
 const ctx={storage:{sql:{exec(sql,...bindings){
  let rows=[];
  if(!bindings.length && sql.includes(';')) db.exec(sql);
  else {const statement=db.prepare(sql);if(statement.columns().length)rows=statement.all(...bindings);else statement.run(...bindings);}
  return {toArray:()=>rows,one:()=>{assert.equal(rows.length,1);return rows[0];}};
 }},getAlarm:async()=>alarm,setAlarm:async value=>{alarm=value;}}};
 const state=new RepositoryState(ctx,env);
 const insert=(operation,number,priority,due,attempt=0)=>{
  const q={operation,args:{number}},key=operation+':'+JSON.stringify(q.args);
  db.prepare('INSERT INTO jobs(key,query,priority,due,attempt,lane) VALUES(?,?,?,?,?,?)').run(key,JSON.stringify(q),priority,due,attempt,state.lane(q,false,priority));
  return q;
 };
 return {state,db,insert,env,objects,ctx};
}

test('partial PR payloads retain mergeability only for the same head and base',async()=>{
 const {state,db}=fixture();try {
  await state.put('pr','7',{number:7,headRefOid:'a',baseRefOid:'b',mergeable:'CONFLICTING',updatedAt:'2026-10-12T00:00:00Z'});
  await state.put('pr','7',{number:7,headRefOid:'a',baseRefOid:'b',title:'list row',updatedAt:'2026-10-12T00:00:00Z'});
  assert.equal(JSON.parse(db.prepare("SELECT data FROM resources WHERE kind='pr' AND id='7'").get().data).mergeable,'CONFLICTING');
  await state.put('pr','7',{number:7,headRefOid:'new',baseRefOid:'b',updatedAt:'2026-10-12T00:01:00Z'});
  assert.equal(JSON.parse(db.prepare("SELECT data FROM resources WHERE kind='pr' AND id='7'").get().data).mergeable,'UNKNOWN');
 }finally{db.close();}
});

test('REST offset inventory gaps retain previously observed open PRs',async()=>{
 const {state,db}=fixture();try {
  state.setMeta('repository',{id:'100',name:'org/repo',installation:'200'});
  const q={repository:'org/repo',operation:'open_prs'},key='open_prs:{}';
  for(const number of [7,8]) {await state.put('pr',String(number),{number,state:'OPEN'});state.membership(key,String(number),true);}
  await state.enqueue(q);const job=state.claim(state.nextJob());
  await state.refresh(job,{data:[{number:8,state:'OPEN'}],api:'core'});
  assert.deepEqual((await state.rows(q)).map(r=>r.number),[7,8]);
 }finally{db.close();}
});

test('a newer close cannot be re-added by an older inventory response',async()=>{
 const {state,db}=fixture();try {
  state.setMeta('repository',{id:'100',name:'org/repo',installation:'200'});
  const q={repository:'org/repo',operation:'open_issues'},key='open_issues:{}';
  await state.enqueue(q);const job=state.claim(state.nextJob());job.started=Date.now()-10000;
  await state.put('issue','7',{number:7,state:'CLOSED'},Date.now(),false,'',true);
  await state.refresh(job,{data:[{number:7,state:'OPEN'}],api:'graphql'});
  assert.deepEqual(await state.rows(q),[]);
  assert.equal(db.prepare('SELECT COUNT(*) n FROM members WHERE collection=? AND id=?').get(key,'7').n,0);
 }finally{db.close();}
});

test('expired staging restarts from page one instead of retrying a missing page forever',async()=>{
 const {state,db,env}=fixture();try {
  state.setMeta('repository',{id:'100',name:'org/repo',installation:'200'});
  env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:async()=>Response.json({data:[],api:'core'})})};
  const q={repository:'org/repo',operation:'files',args:{number:7}};await state.enqueue(q);
  const key='files:{"number":7}';db.prepare('UPDATE jobs SET started=?,page=2,cursor=? WHERE key=?').run(Date.now()-86400001,'old',key);
  await state.dispatch(state.claim(state.nextJob()));
  const row=db.prepare('SELECT * FROM jobs WHERE key=?').get(key);
  assert.equal(row.started,null);assert.equal(row.page,1);assert.equal(row.cursor,null);assert.equal(row.wait_reason,'staging_expired');
 }finally{db.close();}
});

test('one bad batched history does not turn a completed sibling into a retry',async()=>{
 const {state,db,env}=fixture();try {
  state.setMeta('repository',{id:'100',name:'org/repo',installation:'200'});
  env.INSTALLATIONS={idFromName:id=>id,get:()=>({fetch:async()=>Response.json({results:[{data:[],api:'graphql'},{data:[],api:'graphql'}]})})};
  for(const number of [7,8]) await state.enqueue({repository:'org/repo',operation:'issue_comments',args:{number}});
  const original=state.refresh.bind(state);state.refresh=async(job,result)=>{if(JSON.parse(job.query).args.number===8)throw new Error('staging page missing');return original(job,result);};
  await state.dispatch(state.claim(state.nextJob()));
  const done=db.prepare('SELECT * FROM jobs WHERE key=?').get('issue_comments:{"number":7}');
  assert.equal(done.attempt,0);assert.equal(done.wait_reason,null);assert.ok(done.due>Date.now()+10000);
  assert.equal(db.prepare('SELECT wait_reason FROM jobs WHERE key=?').get('issue_comments:{"number":8}').wait_reason,'staging_expired');
 }finally{db.close();}
});

test('persistent parser errors keep their pagination and back off without rapid full scans',async()=>{
 const {state,db}=fixture();try {
  const q={repository:'org/repo',operation:'files',args:{number:7}};await state.enqueue(q);
  const key='files:{"number":7}';db.prepare('UPDATE jobs SET started=?,page=2,cursor=? WHERE key=?').run(Date.now()-1000,'cursor',key);
  let job=state.claim(state.nextJob());state.retryJob(job,new Error('repeated upstream cursor'));
  let row=db.prepare('SELECT * FROM jobs WHERE key=?').get(key);
  assert.equal(row.page,2);assert.equal(row.cursor,'cursor');assert.ok(row.started);assert.ok(row.due>=Date.now()+59000);
  db.prepare('UPDATE jobs SET due=0,lease_until=0 WHERE key=?').run(key);
  job=state.claim(state.nextJob());state.retryJob(job,new Error('repeated upstream cursor'));
  row=db.prepare('SELECT * FROM jobs WHERE key=?').get(key);
  assert.ok(row.due>=Date.now()+119000);assert.equal(row.attempt,2);
 }finally{db.close();}
});

test('daily archive checkpoints bounded pages and retains active R2 references',async()=>{
 const {state,db,objects}=fixture();try {
  state.setMeta('repository',{id:'100',name:'org/repo',installation:'200'});
  const insert=db.prepare('INSERT INTO resources VALUES(?,?,?,?,?,?,?,?)');
  for(let i=0;i<1500;i++)insert.run('issue_comments',String(i),'7',JSON.stringify({body:'x'.repeat(2000)}),'',Date.now(),1,0);
  for(let i=0;i<20 && !state.meta('last_snapshot');i++)await state.snapshotPages(Date.now());
  assert.ok(state.meta('last_snapshot'));
  const manifests=[...objects.entries()].filter(([k])=>k.startsWith('snapshots/') && k.endsWith('.json') && !/\/(meta|resources|collections|members)\//.test(k));
  assert.equal(manifests.length,1);assert.equal(JSON.parse(manifests[0][1]).version,2);
  const pages=[...objects.entries()].filter(([k])=>k.includes('/resources/'));
  assert.ok(pages.length>1);assert.equal(pages.reduce((n,[,text])=>n+JSON.parse(text).length,0),1500);
  assert.ok(pages.every(([,text])=>text.length<1300000));
  const key='resources/100/retained';insert.run('pr','7','',JSON.stringify({$r2:key}),'',Date.now(),1,0);
  assert.equal((await (await state.fetch(new Request('https://internal/retained-object?key='+encodeURIComponent(key)))).json()).retained,true);
  assert.equal((await (await state.fetch(new Request('https://internal/retained-object?key=unused'))).json()).retained,true,'unreferenced objects retain an archive grace period');
  db.prepare('UPDATE retired_objects SET at=? WHERE key=?').run(Date.now()-9*86400000,'unused');
  assert.equal((await (await state.fetch(new Request('https://internal/retained-object?key=unused'))).json()).retained,false);
 }finally{db.close();}
});

test('large old repair backlog cannot starve fresh deltas and still makes progress',()=>{
 const {state,db,insert}=fixture();
 try{
  const now=Date.now();
  for(let i=0;i<5000;i++)insert('files',i,20,now-2*3600000+i);
  insert('pr_delta',1,0,now-1000);
  const choices=Array.from({length:15},()=>state.nextJob());
  assert.equal(choices.filter(j=>j.key.startsWith('pr_delta:')).length,13);
  assert.equal(choices.filter(j=>j.key.startsWith('files:')).length,2);
 }finally{db.close();}
});

test('demand advances quiet reconciliation without revoking actual quota deferrals',async()=>{
 const {state,db,insert}=fixture();
 try{
  const now=Date.now(),hot=insert('reactions',1,20,now+86400000),blocked=insert('reactions',2,20,now+3600000,1);
  await state.noteDemand(hot);await state.noteDemand(blocked);
  const rows=db.prepare('SELECT priority,due,attempt FROM jobs ORDER BY key').all();
  assert.equal(rows[0].priority,5);
  assert.ok(rows[0].due<=Date.now(),'missing coverage is promoted immediately');
  assert.equal(rows[1].due,now+3600000);
  assert.equal(state.priority({operation:'pr_delta'}),0);
  assert.equal(state.priority(hot),5);
  assert.equal(state.priority({operation:'review_threads',args:{number:99}}),20);
 }finally{db.close();}
});
test('closed PR history and CI polling retire, then client demand reactivates them',async()=>{
 const {state,db}=fixture();
 try{
  const sha='a'.repeat(40);
  await state.put('pr','7',{number:7,state:'MERGED',headRefOid:sha,updatedAt:new Date().toISOString()});
  await state.put('review_comments','12',{id:'12',node_id:'node'},Date.now(),false,'7');
  for(const q of [{operation:'review_threads',args:{number:7}},{operation:'statuses',args:{sha}},{operation:'checks',args:{sha}},{operation:'reactions',args:{id:'12'}}])assert.equal(state.dormant(q),true);
  const q={repository:'org/repo',operation:'review_threads',args:{number:7}},key='review_threads:{"number":7}';
  db.prepare('INSERT INTO collections VALUES(?,?,?,?,?,?,?)').run(key,q.operation,JSON.stringify(q.args),1,Date.now(),1,null);
  await state.noteDemand(q);
  assert.equal(state.dormant(q),false);
  assert.equal(db.prepare('SELECT COUNT(*) n FROM jobs WHERE key=?').get(key).n,1);
  assert.equal(state.dormant({operation:'checks',args:{sha:'b'.repeat(40)}}),false,'unknown heads are conservative');
 }finally{db.close();}
});
test('a webhook arriving during a staged read keeps its repair pending',async()=>{
 const {state,db}=fixture();
 try{
  const q={operation:'review_threads',args:{number:7}},key='review_threads:{"number":7}';
  await state.put('pr','7',{number:7,state:'MERGED'});
  await state.enqueue(q,0,Date.now(),true);
  const job=db.prepare('SELECT * FROM jobs WHERE key=?').get(key);
  job.start_generation=job.generation;
  await state.enqueue(q,0,Date.now(),true);
  state.finishJob(job,q);
  const next=db.prepare('SELECT * FROM jobs WHERE key=?').get(key);
  assert.equal(next.dirty,1);assert.ok(next.due<=Date.now());
  state.finishJob({...next,start_generation:next.generation},q);
  assert.equal(db.prepare('SELECT COUNT(*) n FROM jobs WHERE key=?').get(key).n,0,'quiet closed history retires after the latest repair');
 }finally{db.close();}
});

test('all four lanes get their configured share and leased work is excluded',()=>{
 const {state,db,insert}=fixture();
 try {
  for(const [i,lane] of ['requested','active','verification','archive'].entries()) {
   const q=insert('files',i+1,20,Date.now()-10000);
   db.prepare('UPDATE jobs SET lane=? WHERE key=?').run(lane,'files:'+JSON.stringify(q.args));
  }
  const counts={};for(let i=0;i<15;i++){const j=state.nextJob();counts[j.lane]=(counts[j.lane] || 0)+1;}
  assert.deepEqual(counts,{requested:8,active:4,verification:2,archive:1});
  const next=state.nextJob(),claimed=state.claim(next);
  assert.ok(claimed.lease_id);assert.equal(state.claim(next),undefined);
  assert.notEqual(state.nextJob().key,claimed.key);
  db.prepare('UPDATE jobs SET lease_until=0 WHERE key=?').run(claimed.key);
  const replacement=state.claim(claimed);assert.notEqual(replacement.lease_id,claimed.lease_id);
  assert.equal(replacement.started,claimed.started,'expired leases preserve staged pagination');
 } finally {db.close();}
});

test('completed undemanded workflows remain observed; strict demand repairs them and reruns isolate attempts',async()=>{
 const {state,db,objects}=fixture();
 const event=async(id,type,body)=>{const key='fixture/'+id;objects.set(key,JSON.stringify(body));await state.ingest({key,delivery:id,event:type,repository:'org/repo',received:Date.now()});};
 try {
  await event('run','workflow_run',{workflow_run:{id:77,run_attempt:1,status:'completed',head_branch:'feature',updated_at:'2026-10-12T00:00:00Z'}});
  await event('job','workflow_job',{workflow_job:{id:88,run_id:77,run_attempt:1,status:'completed',completed_at:'2026-10-12T00:00:00Z'}});
  assert.equal(db.prepare("SELECT COUNT(*) n FROM jobs WHERE key LIKE 'workflow_jobs:%'").get().n,0);
  const q={repository:'org/repo',operation:'workflow_jobs',args:{id:'77'}};
  const observed=await state.query({...q,allow_observed:true});
  assert.equal(observed.complete,false);assert.equal(observed.coverage,'webhook_observed');assert.equal(observed.data.length,1);
  assert.equal(db.prepare("SELECT COUNT(*) n FROM demand WHERE key LIKE 'workflow_jobs:%'").get().n,0);
  const strict=await state.query(q);assert.equal(strict.status,'unavailable');
  assert.equal(db.prepare("SELECT lane FROM jobs WHERE key LIKE 'workflow_jobs:%'").get().lane,'requested');
  await event('rerun','workflow_run',{workflow_run:{id:77,run_attempt:2,status:'in_progress',head_branch:'feature',updated_at:'2026-10-12T01:00:00Z'}});
  await event('job2','workflow_job',{workflow_job:{id:99,run_id:77,run_attempt:2,status:'in_progress',started_at:'2026-10-12T01:00:00Z'}});
  await event('old','workflow_job',{workflow_job:{id:88,run_id:77,run_attempt:1,status:'completed'}});
  assert.deepEqual((await state.query({...q,allow_observed:true})).data.map(j=>j.id),[99]);
  assert.equal(db.prepare("SELECT COUNT(*) n FROM resources WHERE kind='workflow_jobs'").get().n,2,'old rows remain retained');
 } finally {db.close();}
});

test('retirement keeps stored rows and important Bors builds still request a baseline',async()=>{
 const {state,db}=fixture();
 try {
  const q={repository:'org/repo',operation:'workflow_jobs',args:{id:'77'}};
  await state.put('workflow_runs','77',{id:77,status:'completed',head_branch:'feature'});
  await state.put('workflow_jobs','88',{id:88,run_attempt:1},Date.now(),false,'77');
  state.setMeta('workflow_finished:77',true);await state.enqueue(q,1,Date.now(),true);
  assert.equal(state.retireUnneeded(state.nextJob()),true);
  assert.equal(db.prepare("SELECT COUNT(*) n FROM resources WHERE kind='workflow_jobs'").get().n,1);
  await state.put('workflow_runs','78',{id:78,status:'in_progress',head_branch:'staging'});
  await state.enqueue({...q,args:{id:'78'}},1,Date.now(),true);
  assert.equal(state.retireUnneeded(state.nextJob()),false);
 } finally {db.close();}
});

test('late R2 encoding cannot overwrite a webhook and replaced leases cannot publish coverage',async()=>{
 const {state,db,env}=fixture();
 try {
  state.setMeta('repository',{id:'100',name:'org/repo',installation:'200'});
  let release,entered;const ready=new Promise(r=>entered=r),gate=new Promise(r=>release=r);
  const original=env.ARCHIVE.put;env.ARCHIVE.put=async(...args)=>{entered();await gate;return original(...args);};
  const pending=state.put('issue_comments','1',{id:1,body:'x'.repeat(1500000),updated_at:'2026-10-12T00:00:00Z'},Date.now()-10000);
  await ready;await state.put('issue_comments','1',{id:1,body:'new webhook',updated_at:'2026-10-12T00:01:00Z'},Date.now(),false,'7',true);
  release();assert.equal(await pending,false);
  assert.equal(JSON.parse(db.prepare("SELECT data FROM resources WHERE kind='issue_comments' AND id='1'").get().data).body,'new webhook');
  const q={repository:'org/repo',operation:'files',args:{number:7}};await state.enqueue(q);
  const claimed=state.claim(state.nextJob());db.prepare('UPDATE jobs SET lease_id=NULL WHERE key=?').run(claimed.key);
  await state.refresh(claimed,{data:[],api:'core'});assert.equal(state.complete(q),false);
 } finally {db.close();}
});

test('future-due historical workflow retries retire without touching actual requested quota waits',async()=>{
 const {state,db}=fixture();
 try {
  state.setMeta('repository',{id:'100',name:'org/repo',installation:'200'});state.setMeta('last_snapshot',Date.now());
  const now=Date.now();
  for(const id of ['77','78','79']) {
   await state.put('workflow_runs',id,{id,status:'completed',head_branch:id==='79'?'staging':'feature'});
   await state.enqueue({repository:'org/repo',operation:'workflow_jobs',args:{id}},20,now+3600000);
  }
  state.setMeta('important_final_pending:79',true);
  db.prepare('INSERT INTO demand VALUES(?,?)').run('workflow_jobs:{"id":"78"}',now);
  db.prepare("UPDATE jobs SET wait_reason='primary:core',attempt=1 WHERE key=?").run('workflow_jobs:{"id":"78"}');
  await state.maintenance();
  assert.deepEqual(db.prepare('SELECT key FROM jobs ORDER BY key').all().map(j=>j.key),['workflow_jobs:{"id":"78"}','workflow_jobs:{"id":"79"}']);
  assert.equal(db.prepare('SELECT due FROM jobs WHERE key=?').get('workflow_jobs:{"id":"78"}').due,now+3600000);
 } finally {db.close();}
});

test('head-change webhooks refresh immutable file lists without resetting a quota cooldown',async()=>{
 const {state,db,objects}=fixture();
 try {
  await state.put('pr','7',{number:7,state:'OPEN',headRefOid:'a'.repeat(40),updatedAt:'2026-10-12T00:00:00Z'});
  const q={repository:'org/repo',operation:'files',args:{number:7}},key='files:{"number":7}';
  await state.enqueue(q,20,Date.now()+86400000);
  db.prepare("UPDATE jobs SET due=?,wait_reason='primary:core',attempt=1 WHERE key=?").run(Date.now()+3600000,key);
  const before=db.prepare('SELECT * FROM jobs WHERE key=?').get(key);
  objects.set('head',JSON.stringify({pull_request:{number:7,state:'open',updated_at:'2026-10-12T01:00:00Z',head:{sha:'b'.repeat(40)},user:{login:'peer'}}}));
  await state.ingest({key:'head',delivery:'head',event:'pull_request',repository:'org/repo',received:Date.now()});
  const after=db.prepare('SELECT * FROM jobs WHERE key=?').get(key);
  assert.equal(after.dirty,1);assert.equal(after.lane,'active');assert.equal(after.due,before.due);assert.ok(after.generation>before.generation);
 } finally {db.close();}
});

test('legacy rerun baselines adopt the cached current attempt and cannot serve an earlier complete list',async()=>{
 const {state,db}=fixture();
 try {
  state.setMeta('repository',{id:'100',name:'org/repo',installation:'200'});
  await state.put('workflow_runs','77',{id:77,run_attempt:2,status:'completed',head_branch:'feature'});
  const q={repository:'org/repo',operation:'workflow_jobs',args:{id:'77'}},key='workflow_jobs:{"id":"77"}';
  await state.put('workflow_jobs','88',{id:88,run_attempt:1});state.membership(key,'88',true);
  await state.put('workflow_jobs','99',{id:99,run_attempt:2});state.membership(key,'99',true);
  db.prepare('INSERT INTO collections VALUES(?,?,?,?,?,?,?)').run(key,q.operation,JSON.stringify(q.args),1,Date.now(),1,null);
  assert.equal((await state.query(q)).status,'unavailable');assert.equal(state.runAttempt('77'),2);
  const observed=await state.query({...q,allow_observed:true});assert.deepEqual(observed.data.map(j=>j.id),[99]);
  assert.equal(state.workflowAttempt('77',1),false);
  assert.equal(db.prepare("SELECT COUNT(*) n FROM resources WHERE kind='workflow_jobs'").get().n,2);
 } finally {db.close();}
});

test('REST exhaustion repairs a flat review history through its existing GraphQL thread dependency',async()=>{
 const {state,db}=fixture();
 try {
  state.setMeta('repository',{id:'100',name:'org/repo',installation:'200'});
  const q={repository:'org/repo',operation:'review_comments',args:{number:7}},key='review_comments:{"number":7}';
  await state.noteDemand(q);await state.enqueue(q,5);
  const parent=state.claim(state.nextJob()),reset=Date.now()+3600000;
  await state.refresh(parent,{deferred:true,category:'primary:core',retry_at:reset});
  assert.equal(db.prepare('SELECT due FROM jobs WHERE key=?').get(key).due,reset);
  const sourceKey='review_threads:{"number":7}',source=db.prepare('SELECT * FROM jobs WHERE key=?').get(sourceKey);
  assert.equal(source.lane,'requested');
  await state.refresh(source,{api:'graphql',data:[{id:'thread',isResolved:false,comments:{nodes:[{id:'node',fullDatabaseId:'99',body:'retained review',createdAt:'2026-10-12T00:00:00Z',updatedAt:'2026-10-12T00:00:00Z',author:{login:'peer'}}],pageInfo:{hasNextPage:false}}}]});
  const result=await state.query(q);assert.equal(result.complete,true);assert.equal(result.data[0].body,'retained review');
  assert.equal(db.prepare('SELECT wait_reason FROM jobs WHERE key=?').get(key).wait_reason,null);
 } finally {db.close();}
});
