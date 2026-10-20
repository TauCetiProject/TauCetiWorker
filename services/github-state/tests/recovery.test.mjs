import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {DatabaseSync} from 'node:sqlite';
import {build} from 'esbuild';

const shim={name:'fixture',setup(b){b.onResolve({filter:/^cloudflare:workers$/},()=>({path:'context',namespace:'fixture'}));b.onLoad({filter:/.*/,namespace:'fixture'},()=>({contents:'export class DurableObject {constructor(ctx,env){this.ctx=ctx;this.env=env}}',loader:'js'}));}};
const load=async entry=>import('data:text/javascript;base64,'+Buffer.from((await build({entryPoints:[entry],bundle:true,platform:'node',format:'esm',write:false,plugins:[shim]})).outputFiles[0].text).toString('base64'));
const worker=(await load('src/index.ts')).default;
const {RepositoryState}=await load('src/repository.ts');
const storage=await load('src/storage.ts');
const repo={id:'100',name:'org/repo',installation:'200'};
const sha=bytes=>createHash('sha256').update(bytes).digest('hex');

// R2 double with digest checks, prefix listing and injectable failures.
function bucket(){
 const objects=new Map(),failures=[];
 const meta=(key,o)=>({key,size:o.bytes.length,customMetadata:o.customMetadata,uploaded:o.uploaded,etag:o.etag});
 const check=(op,key)=>{const i=failures.findIndex(f=>f(op,key));if(i>=0){failures.splice(i,1);throw new Error('R2 '+op+' failed');}};
 return {objects,failures,
  async head(key){check('head',key);const o=objects.get(key);return o?meta(key,o):null;},
  async get(key){check('get',key);const o=objects.get(key);return o?{...meta(key,o),text:async()=>o.bytes.toString(),json:async()=>JSON.parse(o.bytes.toString()),arrayBuffer:async()=>new Uint8Array(o.bytes).buffer}:null;},
  async put(key,value,options={}){check('put',key);const bytes=Buffer.from(typeof value==='string'?value:new Uint8Array(value));
   if(options.sha256 && sha(bytes)!==options.sha256) throw new Error('digest mismatch');
   if(options.onlyIf?.etagMatches && objects.get(key)?.etag!==options.onlyIf.etagMatches) return null;
   objects.set(key,{bytes,customMetadata:options.customMetadata || {},uploaded:new Date(),etag:sha(bytes)});},
  async delete(keys){for(const key of [].concat(keys)){check('delete',key);objects.delete(key);}},
  async list({prefix='',limit=1000,cursor}={}){const keys=[...objects.keys()].filter(k=>k.startsWith(prefix)).sort(),start=Number(cursor || 0);
   return {objects:keys.slice(start,start+limit).map(k=>meta(k,objects.get(k))),truncated:start+limit<keys.length,cursor:String(start+limit)};},
 };
}
// Repository double: deduplicates by delivery ID like the real receipt table.
function harness(){
 const archive=bucket(),state={full:false,applied:new Map(),error:null,throws:false};
 const repository={async fetch(url,init){
  if(state.throws) throw new Error('network connection lost');
  const pointer=JSON.parse(init.body);
  if(state.applied.has(pointer.delivery)) return Response.json({ok:true});
  if(state.full) return Response.json({error:'storage_full',parkable:true},{status:503});
  if(state.error) return Response.json({error:state.error,parkable:false},{status:500});
  const body=await archive.get(pointer.key);
  if(!body) return Response.json({error:'internal',message:'event body unavailable'},{status:500});
  state.applied.set(pointer.delivery,(state.applied.get(pointer.delivery) || 0)+1);
  return Response.json({ok:true});
 }};
 const env={ALLOWED_REPOSITORIES:JSON.stringify([repo]),ARCHIVE:archive,REPOSITORIES:{idFromName:x=>x,get:()=>repository}};
 return {archive,state,env};
}
async function deliver(env,delivery,attempts=1){
 const key='webhooks/2026-10-20/'+delivery+'.json',text=JSON.stringify({delivery});
 if(!env.ARCHIVE.objects.has(key)) await env.ARCHIVE.put(key,text,{customMetadata:{sha256:sha(Buffer.from(text)),event:'issue_comment'}});
 const message={body:{key,delivery,event:'issue_comment',repository:repo.name,received:1792000000000},attempts,acked:false,retried:null,ack(){this.acked=true;},retry(options){this.retried=options;}};
 await worker.queue({messages:[message]},env);
 return message;
}
const parked=(archive,delivery)=>[...archive.objects.keys()].filter(k=>k.startsWith('parked/100/pointers/') && k.endsWith('-'+delivery+'.json'));
// Every acknowledged event has a committed receipt or a durable pointer and pinned body.
function durable({archive,state},delivery){
 if(state.applied.has(delivery)) return true;
 const [pointer]=parked(archive,delivery);
 return Boolean(pointer && archive.objects.has(JSON.parse(archive.objects.get(pointer).bytes).key));
}

test('a full repository parks deliveries instead of spending queue retries',async()=>{
 const h=harness();h.state.full=true;
 const message=await deliver(h.env,'d1');
 assert.equal(message.acked,true);assert.equal(message.retried,null);
 const pointer=JSON.parse(h.archive.objects.get(parked(h.archive,'d1')[0]).bytes);
 assert.equal(pointer.original_key,'webhooks/2026-10-20/d1.json');assert.equal(pointer.received,1792000000000);assert.equal(pointer.event,'issue_comment');
 assert.equal(sha(h.archive.objects.get(pointer.key).bytes),pointer.sha256,'pinned body verified');
 assert.equal(h.archive.objects.get(pointer.key).customMetadata.expires,undefined,'normal retention never expires parked bodies');
 // Duplicate delivery and redelivery after a crash before ack are idempotent.
 assert.equal((await deliver(h.env,'d1')).acked,true);
 assert.equal(parked(h.archive,'d1').length,1);
});

test('R2 failure while parking retries with bounded backoff and never acknowledges',async()=>{
 const h=harness();h.state.full=true;
 h.archive.failures.push((op,key)=>op==='put' && key.includes('/pointers/'));
 const first=await deliver(h.env,'d2');
 assert.equal(first.acked,false);assert.deepEqual(first.retried,{delaySeconds:5});
 assert.equal(parked(h.archive,'d2').length,0,'crash before pointer: nothing claims durability');
 const second=await deliver(h.env,'d2',2);
 assert.equal(second.acked,true);assert.ok(durable(h,'d2'));
 h.archive.failures.push((op,key)=>op==='put' && key.includes('/bodies/'));
 const third=await deliver(h.env,'d3',30);
 assert.equal(third.acked,false);assert.deepEqual(third.retried,{delaySeconds:600});
});

test('ordinary failures back off, then park before the queue retry budget is spent',async()=>{
 const h=harness();h.state.error='internal';
 const early=await deliver(h.env,'d4',1);
 assert.equal(early.acked,false);assert.deepEqual(early.retried,{delaySeconds:5});
 const later=await deliver(h.env,'d4',10);
 assert.equal(later.acked,true);assert.ok(durable(h,'d4'));
 h.state.error=null;h.state.throws=true;
 assert.deepEqual((await deliver(h.env,'d5',3)).retried,{delaySeconds:20});
 assert.equal((await deliver(h.env,'d5',12)).acked,true);assert.ok(durable(h,'d5'));
});

test('replay removes pointers only after the receipt commits; crashes repeat harmlessly',async()=>{
 const h=harness();h.state.full=true;
 for(const d of ['r1','r2','r3']) await deliver(h.env,d);
 h.state.full=false;
 // Crash after application, before cleanup: the next replay deduplicates.
 h.archive.failures.push((op,key)=>op==='delete' && key.includes('/bodies/r1'));
 const {drain}=await load('src/ingress.ts');
 await assert.rejects(drain(h.env,repo));
 assert.equal(h.state.applied.get('r1'),1);assert.equal(parked(h.archive,'r1').length,1);
 // A crash between body and pointer removal leaves a receipt-backed pointer.
 h.archive.failures.push((op,key)=>op==='delete' && key.includes('/pointers/') && key.endsWith('-r2.json'));
 await assert.rejects(drain(h.env,repo));
 const result=await drain(h.env,repo);
 assert.equal(result.replayed,2);
 for(const d of ['r1','r2','r3']) {assert.equal(h.state.applied.get(d),1,d+' applied once');assert.equal(parked(h.archive,d).length,0);}
 assert.equal([...h.archive.objects.keys()].filter(k=>k.startsWith('parked/')).length,0);
 // A redelivered message after parking applies directly; its pointer then deduplicates.
 h.state.full=true;await deliver(h.env,'r4');h.state.full=false;
 assert.equal((await deliver(h.env,'r4')).acked,true);
 assert.equal((await drain(h.env,repo)).replayed,1);assert.equal(h.state.applied.get('r4'),1);
});

test('a persistently failing parked event is held aside without blocking newer events',async()=>{
 const h=harness();h.state.full=true;
 await deliver(h.env,'bad');
 h.state.full=false;
 const {drain,backlog}=await load('src/ingress.ts');
 h.archive.objects.delete(JSON.parse(h.archive.objects.get(parked(h.archive,'bad')[0]).bytes).key);
 for(let i=0;i<5;i++) await drain(h.env,repo);
 const report=await backlog(h.env,repo);
 assert.equal(report.parked.count,0);assert.equal(report.held.count,1);
 h.state.full=true;await deliver(h.env,'good');h.state.full=false;
 assert.equal((await drain(h.env,repo)).replayed,1);assert.equal(h.state.applied.get('good'),1);
});

// Repository harness over node:sqlite with an R2 double.
function repositoryFixture(env={},size=0,db=new DatabaseSync(':memory:'),archive=bucket()){
 let alarm=null;
 const ctx={blockConcurrencyWhile:fn=>fn(),id:{toString:()=>'fixture'},storage:{sql:{exec(sql,...bindings){let rows=[];if(!bindings.length && sql.includes(';'))db.exec(sql);else{const st=db.prepare(sql);if(st.columns().length)rows=st.all(...bindings);else st.run(...bindings);}return {toArray:()=>rows,one:()=>rows[0]};},get databaseSize(){return size;}},
  transactionSync(fn){db.exec('SAVEPOINT tx');try{const v=fn();db.exec('RELEASE tx');return v;}catch(e){db.exec('ROLLBACK TO tx');db.exec('RELEASE tx');throw e;}},getAlarm:async()=>alarm,setAlarm:async v=>{alarm=v;}}};
 const state=new RepositoryState(ctx,{ALLOWED_REPOSITORIES:JSON.stringify([repo]),ARCHIVE:archive,...env});
 if(!size) state.setMeta('repository',repo);
 return {db,state,archive};
}
const queryFixture=(state,q)=>state.fetch(new Request('https://internal/query',{method:'POST',body:JSON.stringify({repository:'org/repo',...q})})).then(r=>r.json());

test('legacy SQL cursors and R2 cursors validate query, coverage and expiry',async()=>{
 const {db,state,archive}=repositoryFixture();
 const page={version:1,repository:'org/repo',operation:'open_prs',status:'ok',revision:3,complete:true,data:[{number:1}]};
 const key='open_prs:{}',now=Date.now();
 db.prepare('INSERT INTO cursors VALUES(?,?,?)').run('legacy:100',now+60000,JSON.stringify({key,allow_observed:undefined,result:page}));
 db.prepare('INSERT INTO cursors VALUES(?,?,?)').run('old:100',now-1,JSON.stringify({key,result:page}));
 assert.deepEqual(await queryFixture(state,{operation:'open_prs',cursor:'legacy:100'}),page);
 assert.equal((await queryFixture(state,{operation:'open_prs',cursor:'old:100'})).reason,'cursor_expired');
 assert.equal((await queryFixture(state,{operation:'open_issues',cursor:'legacy:100'})).reason,'cursor_query_mismatch');
 const id='0b6c2f0e-1f7a-4b4c-9a1e-5b8d3c2a1f00';
 await archive.put('cursors/100/'+id+'/100.json',JSON.stringify({repository:'100',key:'workflow_jobs:{"id":"9"}',allow_observed:true,expires:now+60000,result:{...page,operation:'workflow_jobs'}}));
 await archive.put('cursors/100/'+id+'/200.json',JSON.stringify({repository:'100',key,allow_observed:false,expires:now-1,result:page}));
 await archive.put('cursors/100/'+id+'/300.json',JSON.stringify({repository:'999',key,allow_observed:false,expires:now+60000,result:page}));
 assert.equal((await queryFixture(state,{operation:'workflow_jobs',args:{id:'9'},cursor:id+':100'})).reason,'cursor_coverage_mismatch');
 assert.equal((await queryFixture(state,{operation:'workflow_jobs',args:{id:'9'},allow_observed:true,cursor:id+':100'})).status,'ok');
 assert.equal((await queryFixture(state,{operation:'open_prs',cursor:id+':200'})).reason,'cursor_expired');
 assert.equal((await queryFixture(state,{operation:'open_prs',cursor:id+':300'})).reason,'cursor_expired','another repository cannot use the cursor');
 assert.equal((await queryFixture(state,{operation:'open_prs',cursor:'../../etc:1'})).reason,'cursor_expired');
 // Very old whole-snapshot cursors continue only inside their snapshot.
 db.prepare('INSERT INTO cursors VALUES(?,?,?)').run('whole',now+60000,JSON.stringify({key,result:{...page,data:undefined},rows:[{number:1},{number:2}]}));
 assert.deepEqual((await queryFixture(state,{operation:'open_prs',cursor:'whole:1'})).data,[{number:2}]);
 for(const cursor of ['whole:2','whole:900000','whole:1:extra']) assert.equal((await queryFixture(state,{operation:'open_prs',cursor})).reason,'cursor_invalid',cursor);
});

test('expiry runs before repair and hydration, survives their failure, and keeps semantic data',async()=>{
 const {db,state}=repositoryFixture();
 const now=Date.now();
 db.prepare('INSERT INTO resources VALUES(?,?,?,?,?,?,?,?)').run('pr','1','1',JSON.stringify({number:1,state:'OPEN'}),'',now-90*86400000,1,0);
 db.prepare('INSERT INTO resources VALUES(?,?,?,?,?,?,?,?)').run('issue_comments','5','1',JSON.stringify({id:5}),'',now-90*86400000,2,1);
 db.prepare('INSERT INTO collections VALUES(?,?,?,?,?,?,?)').run('open_prs:{}','open_prs','{}',1,now-90*86400000,1,null);
 db.prepare('INSERT INTO members VALUES(?,?)').run('open_prs:{}','1');
 db.prepare('INSERT INTO deliveries VALUES(?,?,?)').run('recent','push',now-86400000);
 for(let i=0;i<60;i++) db.prepare('INSERT INTO cursors VALUES(?,?,?)').run('c'+i,now-1000,'x'.repeat(1000));
 db.prepare('INSERT INTO cursors VALUES(?,?,?)').run('live',now+60000,'{}');
 db.prepare('INSERT INTO repair_metrics VALUES(?,?,?,?,?)').run(now-8*86400000,'page','active','pr',1);
 db.prepare('INSERT INTO jobs(key,query,priority,due,lane) VALUES(?,?,?,?,?)').run('pr:{"number":1}',JSON.stringify({repository:'org/repo',operation:'pr',args:{number:1}}),5,0,'active');
 state.nextJob=()=>{throw new Error('repair dispatch failed');};
 state.hydrateSurvey=async()=>{throw new Error('hydration failed');};
 state.invalidateSurvey=()=>{throw new Error('invalidation failed');};
 await state.alarm();
 assert.equal(db.prepare('SELECT COUNT(*) n FROM cursors').get().n,11,'first bounded expiry batch ran');
 assert.equal(db.prepare('SELECT COUNT(*) n FROM repair_metrics').get().n,0);
 await state.alarm();
 assert.deepEqual(db.prepare('SELECT id FROM cursors').all().map(r=>r.id),['live']);
 assert.equal(db.prepare('SELECT COUNT(*) n FROM resources').get().n,2,'resources and tombstones remain');
 assert.equal(db.prepare('SELECT complete FROM collections').get().complete,1,'coverage is not reset');
 assert.equal(db.prepare('SELECT COUNT(*) n FROM members').get().n,1);
 assert.equal(db.prepare('SELECT COUNT(*) n FROM deliveries').get().n,1,'delivery receipts within retention remain');
});

test('cold start on a current schema performs no writes',async()=>{
 const {db}=repositoryFixture();
 const writes=[];
 const ctx={blockConcurrencyWhile:fn=>fn(),id:{toString:()=>'fixture'},storage:{sql:{exec(sql,...bindings){if(/^\s*(INSERT|UPDATE|REPLACE|CREATE|ALTER|DELETE)/i.test(sql))writes.push(sql.slice(0,40));let rows=[];if(!bindings.length && sql.includes(';'))db.exec(sql);else{const st=db.prepare(sql);if(st.columns().length)rows=st.all(...bindings);else st.run(...bindings);}return {toArray:()=>rows,one:()=>rows[0]};}},transactionSync:fn=>fn()}};
 const state=new RepositoryState(ctx,{ALLOWED_REPOSITORIES:JSON.stringify([repo])});
 assert.deepEqual(writes,[]);assert.equal(state.schema,'current');
});

test('verified cleanup never deletes a reused rowid written after the export began',()=>{
 const db=new DatabaseSync(':memory:'),now=Date.now(),day=86400000;
 const sql={exec(q,...b){let rows=[];const st=db.prepare(q);if(st.columns().length)rows=st.all(...b);else st.run(...b);return {toArray:()=>rows,one:()=>rows[0]};}};
 db.exec('CREATE TABLE demand(key TEXT PRIMARY KEY,at INTEGER)');
 db.prepare('INSERT INTO demand VALUES(?,?)').run('a',now-5*day);
 db.prepare('INSERT INTO demand VALUES(?,?)').run('b',now-5*day);
 const exported={upper:2,started:now-3*day};
 db.prepare("DELETE FROM demand WHERE key='b'").run();
 db.prepare('INSERT INTO demand VALUES(?,?)').run('reused',now-2*day);
 assert.equal(db.prepare("SELECT rowid FROM demand WHERE key='reused'").get().rowid,2);
 db.prepare("UPDATE demand SET at=? WHERE key='a'").run(now-2*day);
 assert.equal(storage.deleteClass(sql,'stale_demand',exported,{}).rows,0,'rows written after export start survive');
 db.prepare('INSERT INTO demand VALUES(?,?)').run('later',now-4*day);
 assert.equal(storage.deleteClass(sql,'stale_demand',exported,{}).rows,0,'rows beyond the exported rowid survive');
 db.prepare("UPDATE demand SET at=? WHERE key='a'").run(now-4*day);
 assert.equal(storage.deleteClass(sql,'stale_demand',exported,{}).rows,1);
});

test('a replayed long-parked event keeps its receipt through maintenance',async()=>{
 const {db,state,archive}=repositoryFixture();
 await archive.put('parked/100/bodies/old.json',JSON.stringify({zen:'parked'}));
 await state.ingest({key:'parked/100/bodies/old.json',delivery:'old',event:'ping',repository:'org/repo',received:Date.now()-20*86400000});
 await state.maintenance();
 assert.equal(db.prepare("SELECT COUNT(*) n FROM deliveries WHERE id='old'").get().n,1);
});

test('a database at the size limit starts read-only: no migration, degraded until a verified probe',async()=>{
 const {db,archive}=repositoryFixture();
 db.exec("DELETE FROM meta WHERE key='schema_version'");
 const full=10*1024**3;
 const {state}=repositoryFixture({},full,db,archive);
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(state.schema,'legacy');assert.equal(state.degraded.operation,'database_size');
 assert.equal(db.prepare("SELECT COUNT(*) n FROM meta WHERE key='schema_version'").get().n,0,'no migration write was attempted');
 // After cleanup the file stays large; a probe-verified state lifts the size rule.
 await archive.put('control/100/storage.json',JSON.stringify({state:'writable',verified_at:Date.now()}));
 const {state:recovered}=repositoryFixture({},full,db,archive);
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(recovered.degraded,undefined);assert.equal(recovered.schema,'current');
 // A recorded failure degrades even a small database.
 await archive.put('control/100/storage.json',JSON.stringify({state:'full',since:1,source:'queue'}));
 const {state:marked}=repositoryFixture({},1,db,archive);
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(marked.degraded.operation,'recorded:queue');
});

test('a storage-full exception from the reset object parks immediately and records the state',async()=>{
 const h=harness();
 h.env.REPOSITORIES.get=()=>({fetch:async()=>{throw new Error('Exceeded the maximum database size.');}});
 const message=await deliver(h.env,'thrown',1);
 assert.equal(message.acked,true);assert.equal(message.retried,null);
 assert.ok(durable(h,'thrown'));
 assert.equal(JSON.parse(h.archive.objects.get('control/100/storage.json').bytes).state,'full');
 h.env.REPOSITORIES.get=()=>({fetch:async()=>{throw new Error('out of memory: SQLITE_NOMEM');}});
 assert.deepEqual((await deliver(h.env,'nomem',1)).retried,{delaySeconds:5},'other storage errors retry');
});

test('a recorded full state stops legacy migration even below the size threshold',async()=>{
 const {db,archive}=repositoryFixture();
 db.exec("DELETE FROM meta WHERE key='schema_version'");
 await archive.put('control/100/storage.json',JSON.stringify({state:'full',since:1,source:'queue'}));
 const {state}=repositoryFixture({},1,db,archive);
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(state.schema,'legacy');assert.equal(state.degraded.operation,'recorded:queue');
 assert.equal(db.prepare("SELECT COUNT(*) n FROM meta WHERE key='schema_version'").get().n,0);
});

test('degraded workflow reads never run the legacy attempt rewrite',async()=>{
 const {db,state}=repositoryFixture();
 db.prepare('INSERT INTO resources VALUES(?,?,?,?,?,?,?,?)').run('workflow_runs','77','',JSON.stringify({id:77,run_attempt:2}),'',1,1,0);
 state.degraded={since:1,reason:'storage_full',operation:'test'};
 const before=db.prepare('SELECT COUNT(*) n FROM changes').get().n;
 const result=await queryFixture(state,{operation:'workflow_jobs',args:{id:'77'}});
 assert.equal(result.status,'unavailable');assert.equal(result.reason,'storage_full');
 assert.equal(db.prepare("SELECT COUNT(*) n FROM meta WHERE key='workflow_attempt:77'").get().n,0);
 assert.equal(db.prepare('SELECT COUNT(*) n FROM changes').get().n,before);
});

test('a failure report older than a verified probe cannot undo recovery',async()=>{
 const {noteRepositoryFull}=await load('src/ingress.ts');
 const archive=bucket(),env={ARCHIVE:archive};
 const started=Date.now()-1000;
 await archive.put('control/100/storage.json',JSON.stringify({state:'writable',verified_at:Date.now()}));
 await noteRepositoryFull(env,'100','queue',started);
 assert.equal(JSON.parse(archive.objects.get('control/100/storage.json').bytes).state,'writable');
 await noteRepositoryFull(env,'100','queue',Date.now()+1000);
 assert.equal(JSON.parse(archive.objects.get('control/100/storage.json').bytes).state,'full');
});
