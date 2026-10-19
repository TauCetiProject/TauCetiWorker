import {test} from 'node:test';
import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {Miniflare,convertV4MiniflareOptions} from 'miniflare';
const bundle=await build({entryPoints:['src/activity_landings.ts'],bundle:true,platform:'node',format:'esm',write:false});
const {promotionReceipts,readPromotionReceipts,BORS_RECEIPTS}=await import('data:text/javascript;base64,'+Buffer.from(bundle.outputFiles[0].text).toString('base64'));
const base='a'.repeat(40),commit='b'.repeat(40),head='c'.repeat(40);
const fixture=()=>({schema:'tauceti-bors.candidates/v1',repo:'TauCetiProject/TauCeti',base:'main',timestamp_unit:'unix_microseconds',candidates:[{
 snapshot:{contract:'tauceti-bors.candidate/v1',base:'main',base_sha:base,members:[{pr:7,head_sha:head}]},
 events:[{kind:'constructed',details:{commit,base_sha:base}},{kind:'promoted',source:'github.base_push_response',observed_at:1792380591874607,details:{base:'main',base_sha:base,commit}}]
}]});
test('promotion receipts retain exact approved PR heads and the actual recorded promotion time',()=>{
 assert.deepEqual(promotionReceipts(fixture()),[{number:7,head,at:1792380591874607,source:'bors-main-push-receipt'}]);
});
test('staging, CI success, other bases and mismatched construction never prove a landing',()=>{
 for(const change of [
  c=>c.events.pop(),c=>c.events[1].kind='status_observed',c=>c.events[1].source='github.staging_push_response',
  c=>c.events[1].details.base='other',c=>c.events[1].details.commit='d'.repeat(40),
  c=>c.events[0].details.base_sha='d'.repeat(40),c=>c.snapshot.members[0].head_sha='',
 ]){const v=fixture();change(v.candidates[0]);assert.deepEqual(promotionReceipts(v),[]);}
 const v=fixture();v.repo='other/repo';assert.throws(()=>promotionReceipts(v));
});
test('receipt fetch is restricted to the existing stored Bors journal and refuses redirects',async()=>{
 const original=globalThis.fetch;
 try{
  globalThis.fetch=async(url,options)=>{assert.equal(url,BORS_RECEIPTS);assert.equal(options.redirect,'manual');return Response.json(fixture());};
  assert.equal((await readPromotionReceipts()).length,1);
  globalThis.fetch=async()=>new Response(new Uint8Array(8*1024*1024+1));
  await assert.rejects(readPromotionReceipts(),/size limit/);
 }finally{globalThis.fetch=original;}
});
test('Workers runtime accepts the binding fetch options and never follows redirects',async()=>{
 const reader=await build({stdin:{contents:`import {readPromotionReceipts} from './src/activity_landings';export default {async fetch(req,env){try{return Response.json(await readPromotionReceipts((url,options)=>env.BORS_ACTIVITY.fetch(url,options)));}catch(error){return Response.json({error:error.message},{status:502});}}}`,resolveDir:process.cwd()},bundle:true,platform:'browser',format:'esm',write:false});
 for(const redirect of [false,true]) {
  const mf=new Miniflare(convertV4MiniflareOptions({workers:[
   {name:'reader',modules:true,script:reader.outputFiles[0].text,compatibilityDate:'2026-10-11',serviceBindings:{BORS_ACTIVITY:'bors-fixture'}},
   {name:'bors-fixture',modules:true,script:`export default {fetch(req){if(req.method!=='GET')throw Error('Only GET allowed');return ${redirect?"new Response(null,{status:302,headers:{Location:'https://forbidden.invalid/'}})":'Response.json('+JSON.stringify(fixture())+')'};}}`,compatibilityDate:'2026-10-11'}
  ]}));
  try {
   const response=await mf.dispatchFetch('http://localhost/');
   const body=await response.json();
   if(redirect){assert.equal(response.status,502);assert.match(body.error,/HTTP 302/);}
   else {assert.equal(response.status,200);assert.equal(body[0].number,7);}
  }finally{await mf.dispose();}
 }
});
test('production receipt reads use the explicit service fetcher without public-network fallback',async()=>{
 const original=globalThis.fetch;
 try{
  globalThis.fetch=()=>{throw Error('public-network fetch forbidden');};
  const service=async(url,options)=>{assert.equal(url,BORS_RECEIPTS);assert.equal(options.method,'GET');return Response.json(fixture());};
  assert.equal((await readPromotionReceipts(service)).length,1);
  await assert.rejects(readPromotionReceipts(async()=>new Response('',{status:503})),/HTTP 503/);
 }finally{globalThis.fetch=original;}
});
