import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {DatabaseSync} from 'node:sqlite';
import {build} from 'esbuild';

const shim={name:'fixture',setup(b){b.onResolve({filter:/^cloudflare:workers$/},()=>({path:'context',namespace:'fixture'}));b.onLoad({filter:/.*/,namespace:'fixture'},()=>({contents:'export class DurableObject {constructor(ctx,env){this.ctx=ctx;this.env=env}}',loader:'js'}));}};
const load=async entry=>import('data:text/javascript;base64,'+Buffer.from((await build({entryPoints:[entry],bundle:true,platform:'node',format:'esm',write:false,plugins:[shim]})).outputFiles[0].text).toString('base64'));
const {RepositoryState}=await load('src/repository.ts');
const migration=await load('src/migration.ts');
const {REQUIRED_TABLES}=await load('src/storage.ts');
const worker=(await load('src/index.ts')).default;
const repo={id:'100',name:'org/repo',installation:'200'};
const generation='c707ddba-076d-4a63-8696-96533031ce88';
const target='neon-test-branch-database';
const sha=text=>createHash('sha256').update(text).digest('hex');

function bucket(){
 const objects=new Map();let failure=null;
 const check=(op,key,when)=>{if(failure?.(op,key,when)){failure=null;throw new Error('sensitive payload/credential detail');}};
 const meta=(key,o)=>({key,size:Buffer.byteLength(o.text),etag:sha(o.text),uploaded:new Date(0),customMetadata:o.customMetadata});
 return {objects,fail(fn){failure=fn;},
  async put(key,text,options={}){check('put',key,'before');text=typeof text==='string'?text:Buffer.from(text).toString();if(options.sha256)assert.equal(sha(text),options.sha256);objects.set(key,{text,customMetadata:options.customMetadata || {}});check('put',key,'after');},
  async get(key){check('get',key,'before');const o=objects.get(key);return o?{...meta(key,o),text:async()=>o.text,json:async()=>JSON.parse(o.text),arrayBuffer:async()=>new Uint8Array(Buffer.from(o.text)).buffer}:null;},
  async head(key){const o=objects.get(key);return o?meta(key,o):null;},
  async delete(keys){for(const key of [].concat(keys))objects.delete(key);},
  async list({prefix='',cursor,limit=1000}={}){const keys=[...objects.keys()].filter(k=>k.startsWith(prefix)).sort();const start=Number(cursor || 0),end=start+Math.min(limit,2);return {objects:keys.slice(start,end).map(k=>meta(k,objects.get(k))),truncated:end<keys.length,cursor:String(end)};},
 };
}
async function fixture(db=new DatabaseSync(':memory:'),archive=bucket(),overrides={}){
 let alarm=null;const ready=[];
 const ctx={id:{toString:()=>'source-object'},blockConcurrencyWhile(fn){ready.push(fn());},storage:{sql:{databaseSize:1024,exec(sql,...bindings){let rows=[];if(!bindings.length && sql.includes(';'))db.exec(sql);else{const st=db.prepare(sql);if(st.columns().length)rows=st.all(...bindings);else st.run(...bindings);}return {toArray:()=>rows,one:()=>rows[0]};}},transactionSync(fn){db.exec('SAVEPOINT tx');try{const v=fn();db.exec('RELEASE tx');return v;}catch(e){db.exec('ROLLBACK TO tx');db.exec('RELEASE tx');throw e;}},sync:async()=>{},getCurrentBookmark:async()=>'authoritative-bookmark',setAlarm:async v=>{alarm=v;},getAlarm:async()=>alarm}};
 const env={ARCHIVE:archive,ALLOWED_REPOSITORIES:JSON.stringify([repo]),CF_VERSION:{id:'test-deployment'},MIGRATION_ENVIRONMENT:'test',...overrides};
 const state=new RepositoryState(ctx,env);await Promise.all(ready);
 if(!state.meta('repository'))state.setMeta('repository',repo);
 env.REPOSITORIES={idFromName:x=>x,get:()=>({fetch:(url,options)=>state.fetch(new Request(url,options))})};
 const request=(path,body)=>state.fetch(new Request('https://internal'+path,{method:'POST',body:JSON.stringify(body)}));
 const command=async(operation,extra={})=>{const response=await request('/migration',{repository:repo.name,environment:'test',operation,generation,target,apply:true,...extra});return {status:response.status,body:await response.json()};};
 return {db,archive,state,env,ctx,request,command};
}
function fill(f){
 const now=Date.now();
 f.state.setMeta('revision',123);
 for(const table of REQUIRED_TABLES){
  if(table==='meta')continue;
  const columns=f.db.prepare('PRAGMA table_info('+table+')').all();
  const values=columns.map(c=>c.type==='INTEGER'?1:'fixture');
  f.db.prepare('INSERT INTO '+table+' VALUES('+values.map(()=>'?').join(',')+')').run(...values);
 }
 f.db.prepare('INSERT INTO resources VALUES(?,?,?,?,?,?,?,?)').run('pr','9223372036854775807','',JSON.stringify({$r2:'resources/100/spill'}),'original-timestamp',now,123,1);
 f.db.prepare('INSERT INTO collections VALUES(?,?,?,?,?,?,?)').run('incomplete','open_prs','{}',0,now,123,'original-etag');
 f.db.prepare('INSERT INTO cursors VALUES(?,?,?)').run('old',now+3600000,JSON.stringify({legacy:'wire-format'}));
 f.db.exec("UPDATE jobs SET due=9999999999999,lease_until=9999999999998,attempt=4,started=12345,page=3,lease_id='old-worker',wait_reason='quota',generation=17,start_generation=16");
 f.db.exec('UPDATE activity_landings SET at=1792360000123456');
 // Exercise signed rowid extremes without JavaScript integer conversion.
 f.db.exec("INSERT INTO demand(rowid,key,at) VALUES(-9223372036854775808,'negative',9223372036854775807)");
 f.db.exec("INSERT INTO demand(rowid,key,at) VALUES(9223372036854775807,'positive',-9223372036854775808)");
 return Promise.all(['resources/100/spill','cursors/100/old/100.json','staging/100/job/12345/1','staging/100/job/12345/2'].map(key=>f.archive.put(key,'{"archived":true}',{customMetadata:{expires:'1'}})));
}
async function finish(f){
 let result;
 for(let n=0;n<500;n++){
  result=await f.command('step',{limit:2});
  assert.equal(result.status,200,JSON.stringify(result.body));
  if(result.body.phase==='source_verified')return result.body;
 }
 assert.fail('migration did not finish');
}

test('full fenced export contains every table, exact integers, timestamps, leases and R2 manifest; restart every step',async()=>{
 let f=await fixture();await fill(f);
 const before=f.db.prepare('SELECT * FROM jobs').get();
 const frozen=await f.command('freeze');assert.equal(frozen.status,200);assert.equal(frozen.body.bookmark,'authoritative-bookmark');
 for(let n=0;n<500;n++){
  const result=await f.command('step',{limit:2});assert.equal(result.status,200,JSON.stringify(result.body));
  if(result.body.phase==='source_verified')break;
  f=await fixture(f.db,f.archive);
  if(n===499)assert.fail('did not finish');
 }
 const receipt=(await f.command('status')).body;
 assert.equal(receipt.phase,'source_verified');
 assert.deepEqual(Object.keys(receipt.tables).sort(),[...REQUIRED_TABLES].sort());
 assert.ok(Object.values(receipt.tables).every(t=>t.verified && t.count===t.rows));
 assert.equal(receipt.blobs.objects,4,'short R2 list pages are all inventoried');
 const pages=[...f.archive.objects.entries()].filter(([key])=>key.includes('/demand/')).map(([,o])=>JSON.parse(o.text));
 const rows=pages.flatMap(p=>p.rows);
 assert.deepEqual(rows.find(r=>r[0]==='-9223372036854775808'),['-9223372036854775808',['text','negative'],['integer','9223372036854775807']]);
 assert.deepEqual(rows.find(r=>r[0]==='9223372036854775807'),['9223372036854775807',['text','positive'],['integer','-9223372036854775808']]);
 assert.deepEqual(f.db.prepare('SELECT * FROM jobs').get(),before,'export never resets cooldowns or leases');
 assert.equal(receipt.write_owner,'sqlite');assert.equal(receipt.production_cutover_supported,false);
 assert.ok(f.archive.objects.has('migrations/100/'+generation+'/receipt.json'));
});

test('freeze fences all mutation routes and alarm dispatch, while stored reads/304/legacy cursors retain timestamps',async()=>{
 const f=await fixture();
 f.db.prepare('INSERT INTO resources VALUES(?,?,?,?,?,?,?,?)').run('pr','1','',JSON.stringify({number:1,state:'OPEN'}),'',123456,7,0);
 f.db.prepare('INSERT INTO collections VALUES(?,?,?,?,?,?,?)').run('open_prs:{}','open_prs','{}',1,123456,7,'etag');
 f.db.prepare('INSERT INTO members VALUES(?,?)').run('open_prs:{}','1');
 f.state.setMeta('revision',7);
 const page={version:1,repository:repo.name,operation:'open_prs',status:'ok',revision:7,complete:true,data:[{number:1}]};
 f.db.prepare('INSERT INTO cursors VALUES(?,?,?)').run('legacy:100',Date.now()+60000,JSON.stringify({key:'open_prs:{}',result:page}));
 const cursorId='0b6c2f0e-1f7a-4b4c-9a1e-5b8d3c2a1f00';
 await f.archive.put('cursors/100/'+cursorId+'/100.json',JSON.stringify({repository:'100',key:'open_prs:{}',allow_observed:false,expires:Date.now()+60000,result:page}));
 await f.command('freeze');
 const sqlBefore=f.db.prepare('SELECT total_changes() n').get().n;
 for(const path of ['/repair','/tick','/activity-refresh','/configure','/storage/cleanup','/storage/probe','/storage/ingress','/event'])assert.equal((await f.request(path,{})).status,503,path);
 f.state.nextJob=()=>assert.fail('GitHub repair dispatch during freeze');
 await f.state.alarm();
 const query={repository:repo.name,operation:'open_prs'};
 const data=await (await f.request('/query',query)).json();assert.equal(data.status,'ok');assert.equal(data.observed_at,123456);assert.equal(data.verified_at,123456);
 assert.deepEqual(await (await f.request('/query',{...query,cursor:'legacy:100'})).json(),page);
 assert.deepEqual(await (await f.request('/query',{...query,cursor:cursorId+':100'})).json(),page);
 const conditional=await f.state.fetch(new Request('https://internal/query',{method:'POST',headers:{'if-none-match':'"7"'},body:JSON.stringify(query)}));assert.equal(conditional.status,304);
 assert.equal(f.db.prepare('SELECT total_changes() n').get().n,sqlBefore,'no SQL mutation from reads or alarms');
 assert.throws(()=>f.state.setMeta('forbidden',true),/migration_frozen/);
});

test('in-flight event finishes before bookmark; new input parks even beyond retry horizon and survives retention',async()=>{
 const f=await fixture();
 let finishEvent,entered;
 const waiting=new Promise(r=>entered=r),gate=new Promise(r=>finishEvent=r);
 f.state.ingest=async()=>{entered();await gate;f.state.setMeta('revision',987);};
 const inflight=f.request('/event',{repository:repo.name});await waiting;
 let bookmarkRevision;
 f.ctx.storage.getCurrentBookmark=async()=>{bookmarkRevision=f.state.revision();return 'after-event';};
 const freezing=f.command('freeze');
 // Wait until durable barrier exists, without depending on a fixed timer.
 while(!f.archive.objects.has('control/100/migration.json'))await new Promise(r=>setImmediate(r));
 const body='{"zen":"during-freeze"}',key='webhooks/2000-01-01/during-freeze.json';
 await f.archive.put(key,body,{customMetadata:{sha256:sha(body),expires:'1'}});
 for(let attempts=1;attempts<=30;attempts++){
  let acked=false;
  await worker.queue({messages:[{body:{key,delivery:'during-freeze',event:'ping',repository:repo.name,received:1},attempts,ack(){acked=true;},retry(){assert.fail('spent queue retries during freeze');}}]},f.env);
  assert.equal(acked,true);
 }
 finishEvent();await inflight;
 assert.equal((await freezing).status,200);assert.equal(bookmarkRevision,987);
 const tasks=[];
 await worker.scheduled({},f.env,{waitUntil(p){tasks.push(p);}});await Promise.all(tasks);
 assert.ok(f.archive.objects.has(key),'raw input retention is pinned');
 assert.ok(f.archive.objects.has('parked/100/bodies/during-freeze.json'));
 assert.equal([...f.archive.objects.keys()].filter(k=>k.includes('/pointers/')).length,1);
});

test('production and wrong identity are rejected; no caller assertion enables cutover',async()=>{
 const f=await fixture();
 assert.equal((await f.command('freeze',{environment:'production',s0_passed:true,s1_passed:true})).status,400);
 assert.equal(f.archive.objects.size,0);
 assert.equal((await f.command('freeze',{apply:false})).body.error,'migration_apply_required');
 assert.equal((await f.command('freeze',{target:'postgresql://user:secret@host/db'})).body.error,'invalid_target_identity');
 assert.equal((await f.command('freeze')).status,200);
 assert.equal((await f.command('step',{target:'wrong-target'})).body.error,'migration_identity_conflict');
 assert.equal((await f.command('switch')).body.error,'migration_target_backend_unavailable');
 const restarted=await fixture(f.db,f.archive,{CF_VERSION:{id:'old-deployment'}});
 assert.equal((await restarted.command('step')).body.error,'migration_source_identity_changed');
 assert.equal((await restarted.request('/repair',{})).status,503);
});

test('unknown tables and unavailable authoritative bookmark fail closed',async()=>{
 const f=await fixture();f.db.exec('CREATE TABLE forgotten_state(id TEXT)');
 assert.equal((await f.command('freeze')).body.error,'migration_table_manifest_mismatch');
 assert.equal(f.archive.objects.size,0);
 f.db.exec('DROP TABLE forgotten_state');f.ctx.storage.getCurrentBookmark=async()=>null;
 assert.equal((await f.command('freeze')).body.error,'migration_bookmark_unavailable');
 assert.equal((await f.request('/repair',{})).status,503);
 f.ctx.storage.getCurrentBookmark=async()=>'recovered-bookmark';
 assert.equal((await f.command('freeze')).body.bookmark,'recovered-bookmark');
});

for(const when of ['before','after'])test(`restart around ${when}-commit export and validation checkpoint failures`,async()=>{
 let f=await fixture();await fill(f);await f.command('freeze');
 f.archive.fail((op,key,at)=>op==='put' && key.includes('/activity_landings/') && at===when);
 const failed=await f.command('step');assert.equal(failed.body.error,'migration_storage_failure');
 assert.ok(!JSON.stringify(failed).includes('sensitive'));
 f=await fixture(f.db,f.archive);
 // Interrupt after a page has been saved but while its control checkpoint is written.
 f.archive.fail((op,key,at)=>op==='put' && key==='control/100/migration.json' && at===when);
 assert.equal((await f.command('step')).status,400);
 f=await fixture(f.db,f.archive);
 while((await f.command('status')).body.phase==='exporting')assert.equal((await f.command('step',{limit:2})).status,200);
 f.archive.fail((op,key,at)=>op==='put' && key==='control/100/migration.json' && at===when);
 assert.equal((await f.command('step')).status,400);
 f=await fixture(f.db,f.archive);
 assert.equal((await finish(f)).phase,'source_verified');
});

test('verification rejects missing R2 references and changed/corrupt source pages; abandon retains archives',async()=>{
 const f=await fixture();await fill(f);await f.command('freeze');
 while((await f.command('status')).body.phase==='exporting')assert.equal((await f.command('step')).status,200);
 const key=[...f.archive.objects.keys()].find(k=>k.includes('/activity_landings/'));
 const saved=f.archive.objects.get(key);f.archive.objects.set(key,{...saved,text:'[]'});
 assert.equal((await f.command('step')).body.error,'migration_page_corrupt');
 f.archive.objects.set(key,saved);
 f.archive.objects.delete('resources/100/spill');
 let failed;
 for(let n=0;n<100;n++){failed=await f.command('step');if(failed.status!==200)break;}
 assert.equal(failed.body.error,'migration_blob_missing');
 await f.archive.put('resources/100/spill','{}');
 await finish(f);
 assert.equal((await f.command('abandon')).body.phase,'abandoned');
 assert.ok(f.archive.objects.has(key),'abandon never removes archives');
 assert.equal((await f.command('freeze')).body.error,'migration_generation_abandoned');
 f.state.setMeta('unfrozen',true);
});


test('public migration endpoint requires admin auth and never exposes stored rows',async()=>{
 const f=await fixture(undefined,undefined,{ADMIN_TOKEN_HASH:sha('admin'),CLIENT_TOKENS:JSON.stringify([{hash:sha('client'),repositories:[repo.name]}])});
 const call=token=>worker.fetch(new Request('https://service/admin/migration',{method:'POST',headers:token?{authorization:'Bearer '+token}:{},body:JSON.stringify({repository:repo.name,operation:'preflight'})}),f.env,{});
 assert.equal((await call()).status,401);
 assert.equal((await call('client')).status,401);
 const response=await call('admin');assert.equal(response.status,200);
 const data=await response.json();assert.equal(data.production_cutover_supported,false);assert.equal(data.source_export_environment,'test');
 assert.ok(data.blockers.includes('s1_backend_unavailable'));
 assert.equal(Object.hasOwn(data,'rows'),false);
});


test('final receipt interruption resumes without repeating export; a new generation requires explicit abandonment',async()=>{
 let f=await fixture();await f.command('freeze');
 while((await f.command('status')).body.phase!=='source_verified'){
  const status=(await f.command('status')).body;
  if(status.phase==='verifying' && Object.values(status.tables).every(t=>t.verified)){
   f.archive.fail((op,key,at)=>op==='put' && key.endsWith('/receipt.json') && at==='before');
   let failed;
   for(let n=0;n<10;n++){failed=await f.command('step');if(failed.status!==200)break;}
   assert.equal(failed.body.error,'migration_storage_failure');break;
  }
  assert.equal((await f.command('step')).status,200);
 }
 f=await fixture(f.db,f.archive);
 const complete=await f.command('step');assert.equal(complete.body.phase,'source_verified');
 assert.ok(f.archive.objects.has('migrations/100/'+generation+'/receipt.json'));
 const next='c707ddba-076d-4a63-8696-96533031ce89';
 assert.equal((await f.command('freeze',{generation:next})).body.error,'migration_identity_conflict');
 await f.command('abandon');
 assert.equal((await f.command('freeze',{generation:next})).body.phase,'exporting');
 assert.ok(f.archive.objects.has('migrations/100/'+generation+'/abandoned.json'));
});
