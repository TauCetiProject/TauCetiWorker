import {test} from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {build} from 'esbuild';
const bundle=await build({entryPoints:['src/repository.ts'],bundle:true,platform:'node',format:'esm',write:false,plugins:[{
 name:'fixture',setup(b){b.onResolve({filter:/^cloudflare:workers$/},()=>({path:'context',namespace:'fixture'}));b.onLoad({filter:/.*/,namespace:'fixture'},()=>({contents:'export class DurableObject {constructor(ctx,env){this.ctx=ctx;this.env=env}}',loader:'js'}));}
}]});
const {RepositoryState}=await import('data:text/javascript;base64,'+Buffer.from(bundle.outputFiles[0].text).toString('base64'));
function fixture(){
 const db=new DatabaseSync(':memory:');
 const forbidden=()=>{throw new Error('Reporting attempted upstream/scheduler access');};
 const ctx={blockConcurrencyWhile:fn=>fn(),storage:{sql:{exec(sql,...bindings){let rows=[];if(!bindings.length && sql.includes(';'))db.exec(sql);else{const st=db.prepare(sql);if(st.columns().length)rows=st.all(...bindings);else st.run(...bindings);}return {toArray:()=>rows,one:()=>rows[0]};}},transactionSync(fn){db.exec('SAVEPOINT tx');try{const value=fn();db.exec('RELEASE tx');return value;}catch(error){db.exec('ROLLBACK TO tx');db.exec('RELEASE tx');throw error;}},getAlarm:forbidden,setAlarm:forbidden}};
 const state=new RepositoryState(ctx,{INSTALLATIONS:new Proxy({},{get:forbidden}),ARCHIVE:{get:forbidden},BORS_ACTIVITY:{fetch:(...args)=>globalThis.fetch(...args)}});
 for(const name of ['configure','noteDemand','enqueue','query','runAttempt'])state[name]=forbidden;
 return {db,state};
}
const query=(state,numbers)=>state.fetch(new Request('https://internal/activity',{method:'POST',body:JSON.stringify({repository:'org/repo',numbers})}));
test('reporting missing/never-configured resources creates no demand, jobs, alarms or upstream calls',async()=>{
 const {db,state}=fixture();try{
  for(let i=0;i<3;i++){
   const result=await (await query(state,[7,8])).json();
   assert.deepEqual(result.missing,[7,8]);assert.deepEqual(result.prs,[]);assert.equal(result.coverage,'observed');
  }
  assert.equal(db.prepare('SELECT COUNT(*) n FROM demand').get().n,0);
  assert.equal(db.prepare('SELECT COUNT(*) n FROM jobs').get().n,0);
  assert.equal(state.meta('repository'),undefined);
 }finally{db.close();}
});
test('stale cached closed/merged PRs remain readable without reactivating polling',async()=>{
 const {db,state}=fixture();try{
  const at=Date.now()-7*86400000;
  for(const p of [{number:7,state:'CLOSED',headRefOid:'head'},{number:8,state:'MERGED',mergedAt:'2026-10-18T00:00:00Z'}])
   db.prepare('INSERT INTO resources VALUES (?,?,?,?,?,?,?,0)').run('pr',String(p.number),'',JSON.stringify(p),'',at,1);
  const result=await (await query(state,[7,8,9])).json();
  assert.deepEqual(result.missing,[9]);assert.equal(result.prs[0].observed_at,at);
  assert.equal(result.prs[0].landed_at,null,'closed does not mean landed');
  assert.equal(result.prs[1].landed_at,'2026-10-18T00:00:00Z');
  assert.equal(db.prepare('SELECT COUNT(*) n FROM demand').get().n,0);
  assert.equal(db.prepare('SELECT COUNT(*) n FROM jobs').get().n,0);
 }finally{db.close();}
});
test('unavailable stored resource fails without falling back to upstream repair',async()=>{
 const {db,state}=fixture();try{
  db.prepare('INSERT INTO resources VALUES (?,?,?,?,?,?,?,0)').run('pr','7','',JSON.stringify({$r2:'missing'}),'',Date.now(),1);
  assert.equal((await query(state,[7])).status,500);
  assert.equal(db.prepare('SELECT COUNT(*) n FROM jobs').get().n,0);
 }finally{db.close();}
});
test('LOC uses complete cached files at the exact PR head without scheduling work',async()=>{
 const {db,state}=fixture();try{
  const head='a'.repeat(40),base='b'.repeat(40),key='files:{"number":7}';
  const p={number:7,state:'OPEN',headRefOid:head,baseRefOid:base,changedFiles:2,additions:32,deletions:5};
  const put=()=>db.prepare('INSERT OR REPLACE INTO resources VALUES (?,?,?,?,?,?,?,0)').run('pr','7','',JSON.stringify(p),'',Date.now(),1);
  put();
  const file=(name,added,deleted,sha=head)=>({filename:name,status:'modified',additions:added,deletions:deleted,blob_url:`https://github.com/org/repo/blob/${sha}/${name}`});
  for(const f of [file('TauCeti/A.lean',30,4),file('README.md',2,1)]){
   const id='7:'+f.filename;
   db.prepare('INSERT INTO resources VALUES (?,?,?,?,?,?,?,0)').run('files',id,'7',JSON.stringify(f),'',Date.now(),1);
   db.prepare('INSERT INTO members VALUES (?,?)').run(key,id);
  }
  db.prepare('INSERT INTO collections VALUES (?,?,?,?,?,?,?)').run(key,'files','{"number":7}',0,Date.now(),1,null);
  assert.equal((await (await query(state,[7])).json()).prs[0].lean_loc,null);
  db.prepare('UPDATE collections SET complete=1 WHERE key=?').run(key);
  assert.deepEqual((await (await query(state,[7])).json()).prs[0].lean_loc,{added:30,deleted:4,head_sha:head});
  p.headRefOid='c'.repeat(40);put();
  assert.equal((await (await query(state,[7])).json()).prs[0].lean_loc,null,'an old-head diff must not count');
  const newer={filename:'TauCeti/A.lean',status:'modified',additions:30,deletions:4,blob_url:`https://github.com/org/repo/blob/${p.headRefOid}/TauCeti/A.lean`};
  db.prepare("UPDATE resources SET data=? WHERE kind='files' AND id='7:TauCeti/A.lean'").run(JSON.stringify(newer));
  newer.filename='README.md';newer.additions=2;newer.deletions=1;newer.blob_url=`https://github.com/org/repo/blob/${p.headRefOid}/README.md`;
  db.prepare("UPDATE resources SET data=? WHERE kind='files' AND id='7:README.md'").run(JSON.stringify(newer));
  db.prepare('UPDATE collections SET revision=revision+1 WHERE key=?').run(key);
  assert.equal((await (await query(state,[7])).json()).prs[0].lean_loc.added,30,'a completed new collection invalidates a negative cache');
  assert.equal(db.prepare('SELECT COUNT(*) n FROM demand').get().n,0);
  assert.equal(db.prepare('SELECT COUNT(*) n FROM jobs').get().n,0);
 }finally{db.close();}
});
test('background Bors receipt import is shared and rate limited; activity reads never fetch',async()=>{
 const {db,state}=fixture(),original=globalThis.fetch;
 try{
  const head='c'.repeat(40),base='a'.repeat(40),commit='b'.repeat(40);
  state.setMeta('repository',{name:'TauCetiProject/TauCeti'});
  state.setMeta('activity_landings_checked',Date.now()); // Earlier public-fetch failure must not delay the transport upgrade.
  const v={schema:'tauceti-bors.candidates/v1',repo:'TauCetiProject/TauCeti',base:'main',timestamp_unit:'unix_microseconds',candidates:[{
   snapshot:{contract:'tauceti-bors.candidate/v1',base:'main',base_sha:base,members:[{pr:7,head_sha:head}]},
   events:[{kind:'constructed',details:{commit,base_sha:base}},{kind:'promoted',source:'github.base_push_response',observed_at:1792380591874607,details:{base:'main',base_sha:base,commit}}]
  }]};
  let calls=0;
  globalThis.fetch=async(url)=>{assert.equal(new URL(url).hostname,'bors.taucetiproject.org');calls++;return Response.json(v);};
  await state.refreshActivityLandings();await state.refreshActivityLandings();assert.equal(calls,1);
  db.prepare('INSERT INTO resources VALUES (?,?,?,?,?,?,?,0)').run('pr','7','',JSON.stringify({state:'CLOSED',headRefOid:head}),'',Date.now(),1);
  globalThis.fetch=()=>{throw Error('No network during activity reads');};
  let result=await (await query(state,[7])).json();
  assert.equal(result.prs[0].landed_at,'2026-10-19T03:29:51.874Z');
  assert.equal(result.prs[0].landing_source,'bors-main-push-receipt');
  db.prepare("UPDATE resources SET data=? WHERE kind='pr' AND id='7'").run(JSON.stringify({state:'CLOSED',headRefOid:'d'.repeat(40)}));
  result=await (await query(state,[7])).json();assert.equal(result.prs[0].landed_at,null,'different PR heads cannot inherit a landing');
  state.setMeta('activity_landings_checked',Date.now()-300001);
  await state.refreshActivityLandings();assert.ok(state.meta('activity_landings_error'));
  assert.equal(db.prepare('SELECT COUNT(*) n FROM activity_landings').get().n,1,'failures retain receipts');
  assert.equal(db.prepare('SELECT COUNT(*) n FROM demand').get().n,0);
  assert.equal(db.prepare('SELECT COUNT(*) n FROM jobs').get().n,0);
 }finally{globalThis.fetch=original;db.close();}
});
