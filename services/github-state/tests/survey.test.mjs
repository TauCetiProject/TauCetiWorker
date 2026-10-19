import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {build} from 'esbuild';
import {runtime,repository,response,send} from './runtime.mjs';

const bundle=await build({entryPoints:['src/survey.ts'],bundle:true,platform:'node',format:'esm',write:false});
const facts=await import('data:text/javascript;base64,'+Buffer.from(bundle.outputFiles[0].text).toString('base64'));
test('Python fallback and TypeScript projection agree on 240 changing histories and scopes',()=>{
 const process=spawnSync('python3',['../../tests/fixtures/survey_facts_parity.py'],{encoding:'utf8',maxBuffer:5000000});
 assert.equal(process.status,0,process.stderr);
 for(const c of JSON.parse(process.stdout)) {
  assert.deepEqual(facts.summarize(c.issues,c.reviews),c.expected);
  assert.deepEqual(facts.targetFocuses(c.row.body),c.focuses);
  assert.equal(facts.inScope(c.row,c.scope,c.skip),c.included);
 }
});

test('compact survey is authenticated, roadmap scoped and does not issue GitHub requests',async()=>{
 let network=0;
 const mf=await runtime(async()=>{network++;return response([],'core');});
 try {
  const ns=await mf.getDurableObjectNamespace('REPOSITORIES'),stub=ns.get(ns.idFromName('100'));
  await send(stub,'/configure',{repository:repository.name});
  const before=network;
  const r=await mf.dispatchFetch('https://test/v1/query?repository=org/repo&operation=survey&args='+encodeURIComponent(JSON.stringify({roadmap:'PDE',skip:'',numbers:''})),{headers:{Authorization:'Bearer client'}});
  assert.equal(r.status,200);const payload=await r.json();
  assert.equal(payload.contract,facts.SURVEY_CONTRACT);assert.equal(payload.status,'unavailable');assert.equal(network,before);
  assert.equal((await mf.dispatchFetch('https://test/v1/query?repository=org/repo&operation=survey',{headers:{Authorization:'Bearer invalid'}})).status,401);
 }finally{await mf.dispose();}
});

test('head-bound blockers retain their authenticated actor and do not trust quoted markers',()=>{
 const head='a'.repeat(40), marker='<!--tauceti-blocked:v1 '+head+'-->';
 const rows=[{body:marker+'\nEvidence',user:{login:'Author'}},{body:'> '+marker,user:{login:'Author'}},{body:'Quoted\n'+marker,user:{login:'Author'}}];
 assert.deepEqual(facts.summarize(rows,[]).blocked,[{head,author:'author'}]);
 assert.deepEqual(facts.summarize([],[]).blocked,[]);
 assert.deepEqual(facts.summarizeFragments([{meta:null,rebase_heads:[],marker:null}],[]).blocked,[],'old stored fragments require no refetch');
});
