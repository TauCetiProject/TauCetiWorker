import {test} from 'node:test';
import assert from 'node:assert/strict';
import {runtime,repository,response,send} from './runtime.mjs';

test('large inventory snapshots paginate coherently and coalesce concurrent reads',async()=>{
 const count=Number(process.env.TAUCETI_STATE_TEST_PRS || 1200), head='a'.repeat(40);
 const prs=Array.from({length:count},(_,i)=>({number:i+1,title:'PR '+(i+1),body:'body'.repeat(300),state:'OPEN',isDraft:false,mergeable:'MERGEABLE',updatedAt:'2026-10-12T00:00:00Z',headRefOid:head,author:{login:'peer',__typename:'User'},labels:{nodes:[]}}));
 let inventoryRequests=0;
 const mf=await runtime(async(req,u)=>{
  if(u.pathname==='/graphql'){
   const {query,variables}=await req.json();
   const repositoryData={};
   if(query.includes('pullRequests(')){
    const offset=Number(variables.cursor || 0),next=offset+100;
    repositoryData.pullRequests={nodes:prs.slice(offset,next),pageInfo:{hasNextPage:next<count,endCursor:String(next)}};
    if(query.includes('states:OPEN')) inventoryRequests++;
   }else if(query.includes('issues(')) repositoryData.issues={nodes:[],pageInfo:{hasNextPage:false,endCursor:null}};
   else for(const [,alias] of query.matchAll(/(q\d+):/g)) repositoryData[alias]={status:{contexts:[]},comments:{nodes:[],pageInfo:{hasNextPage:false,endCursor:null}},reviewThreads:{nodes:[],pageInfo:{hasNextPage:false,endCursor:null}}};
   return response({data:{rateLimit:{cost:1,remaining:4999,resetAt:new Date(Date.now()+300000).toISOString()},repository:repositoryData}});
  }
  return response(u.pathname.endsWith('/check-runs')?{check_runs:[]}:u.pathname.endsWith('/status')?{statuses:[]}:[], 'core');
 });
 try{
  const namespace=await mf.getDurableObjectNamespace('REPOSITORIES'),stub=namespace.get(namespace.idFromName(repository.id));
  await send(stub,'/configure',{repository:repository.name});
  let health;
  const deadline=Date.now()+20000;
  do{
   await new Promise(resolve=>setTimeout(resolve,50));
   health=await (await stub.fetch('https://internal/health')).json();
  }while(!health.bootstrap_inventory && Date.now()<deadline);
  assert.ok(health.bootstrap_inventory,'inventory finished bootstrapping');
  assert.equal(inventoryRequests,count/100);
  const archive=await mf.getR2Bucket('ARCHIVE');
  do{
   await new Promise(resolve=>setTimeout(resolve,50));
   health=await (await stub.fetch('https://internal/health')).json();
  }while(!health.coverage.some(c=>c.operation==='statuses' && c.complete) && Date.now()<deadline);
  assert.ok(health.coverage.some(c=>c.operation==='statuses' && c.complete),'status baseline available');
  await archive.put('fixture/status',JSON.stringify({sha:head,context:'build',state:'success',created_at:'2026-10-12T01:00:00Z'}));
  await send(stub,'/event',{key:'fixture/status',delivery:'status',event:'status',repository:repository.name,received:Date.now()});
  const status=await (await send(stub,'/query',{repository:repository.name,operation:'statuses',args:{sha:head}})).json();
  assert.equal(status.data.find(c=>c.context==='build').state,'SUCCESS');
  const query={repository:repository.name,operation:'open_prs'};
  const began=performance.now();
  const responses=await Promise.all(Array.from({length:10},()=>send(stub,'/query',query).then(r=>r.json())));
  const first=responses[0];assert.equal(first.status,'ok');assert.equal(first.data.length,500);
  const groups=new Map();
  for (const result of responses) {
   const cursors=groups.get(result.revision) || new Set();cursors.add(result.cursor);groups.set(result.revision,cursors);
  }
  assert.ok([...groups.values()].every(cursors=>cursors.size===1),'identical revisions share one snapshot');
  const key='fixture/closed';
  await archive.put(key,JSON.stringify({repository:{id:Number(repository.id)},installation:{id:Number(repository.installation)},action:'closed',pull_request:{number:600,title:'changed later',state:'closed',updated_at:'2030-01-01T00:00:00Z',head:{sha:head},user:{login:'peer'}}}));
  await send(stub,'/event',{key,delivery:'closed',event:'pull_request',repository:repository.name,received:Date.now()});
  let cursor=first.cursor,rows=[...first.data],pages=1;
  while(cursor){
   const page=await (await send(stub,'/query',{...query,cursor})).json();
   assert.equal(page.revision,first.revision);rows.push(...page.data);cursor=page.cursor;pages++;
  }
  assert.equal(rows.length,count);assert.equal(new Set(rows.map(r=>r.number)).size,count);
  assert.equal(rows.find(r=>r.number===600).title,'PR 600');
  const changed=await (await send(stub,'/query',query)).json();assert.ok(changed.revision>first.revision);
  console.log(JSON.stringify({fixture:'large_inventory',prs:count,initial_inventory_requests:inventoryRequests,concurrent_clients:10,pages,query_and_continuation_ms:Math.round(performance.now()-began)}));
 }finally{await mf.dispose();}
});
