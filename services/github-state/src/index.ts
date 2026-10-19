import { Env, EventPointer, Query, authorize, digest, json, repositories, repository, stub, validSignature, validateQuery, parseGitHub } from './protocol';
export { RepositoryState } from './repository';
export { InstallationScheduler } from './installation';

async function query(req:Request,env:Env,q:Query): Promise<Response> {
  try { validateQuery(q); } catch { return json({error:'invalid_query'},400); }
  const repo=repository(env,q.repository);
  if (!await authorize(req,env)) return json({error:'unauthorized'},401);
  if (!repo || !await authorize(req,env,repo.name)) return json({version:1,repository:q.repository,operation:q.operation,status:'unavailable',revision:0,complete:false,reason:'repository_unavailable'});
  const response=await stub(env.REPOSITORIES,repo.id).fetch('https://internal/query',{method:'POST',headers:{'If-None-Match':req.headers.get('If-None-Match') || ''},body:JSON.stringify({...q,repository:repo.name})});
  if (response.status===304) return response;
  if (response.status!==200) return json({error:'storage_unavailable'},503);
  const etag=response.headers.get('etag');
  if (etag && req.headers.get('if-none-match')===etag && !q.cursor) {
    const copy:any=await response.clone().json();
    if (copy.status==='ok') return new Response(null,{status:304,headers:{ETag:etag,'Cache-Control':'private, no-cache'}});
  }
  return response;
}
export default {
  async fetch(req:Request,env:Env,ctx:ExecutionContext):Promise<Response> {
    const url=new URL(req.url);
    try {
      if (url.pathname==='/healthz' && req.method==='GET') return json({service:'tauceti-github-state',version:1});
      if (url.pathname==='/setup/register' && req.method==='GET') {
        const suffix=url.hostname.includes('github-state-test.')?'-test':'';
        const manifest={name:'tauceti-github-state'+suffix,url:url.origin,hook_attributes:{url:url.origin+'/webhook/github',active:true},redirect_url:url.origin+'/registration-complete',public:false,description:'Exclusive read-only webhook mirror for TauCeti fleet queries.',default_permissions:{metadata:'read',contents:'read',pull_requests:'read',issues:'read',checks:'read',statuses:'read',actions:'read',merge_queues:'read'},default_events:['pull_request','pull_request_review','pull_request_review_comment','pull_request_review_thread','issues','issue_comment','label','milestone','push','create','delete','repository','status','check_run','check_suite','workflow_run','workflow_job','merge_group']};
        const escaped=JSON.stringify(manifest).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;');
        return new Response(`<!doctype html><meta name="referrer" content="no-referrer"><title>Register TauCeti GitHub state App</title><h1>Register the dedicated read-only GitHub App</h1><p>This creates ${manifest.name}, owned by TauCetiProject. Install it only on TauCeti. It cannot write to GitHub.</p><form method="post" action="https://github.com/organizations/TauCetiProject/settings/apps/new"><input type="hidden" name="manifest" value="${escaped}"><button>Register ${manifest.name}</button></form>`,{headers:{'Content-Type':'text/html; charset=utf-8','Referrer-Policy':'no-referrer','Cache-Control':'no-store'}});
      }
      if (url.pathname==='/registration-complete' && req.method==='GET') return new Response('GitHub App registration completed. Give the one-time code from this URL to the setup helper, then install the App only on TauCeti.',{headers:{'Content-Type':'text/plain','Referrer-Policy':'no-referrer','Cache-Control':'no-store'}});
      if (url.pathname==='/webhook/github' && req.method==='POST') {
        if (Number(req.headers.get('content-length') || 0)>26*1024*1024) return json({error:'payload_too_large'},413);
        const body=await req.arrayBuffer();
        if (body.byteLength>26*1024*1024) return json({error:'payload_too_large'},413);
        if (!await validSignature(body,req.headers.get('x-hub-signature-256'),env.WEBHOOK_SECRET)) return json({error:'invalid_signature'},401);
        const delivery=req.headers.get('x-github-delivery'),event=req.headers.get('x-github-event');
        if (!delivery || !/^[a-zA-Z0-9-]{1,100}$/.test(delivery) || !event || !/^[a-z_]{1,100}$/.test(event)) return json({error:'invalid_delivery'},400);
        let payload:any; try { payload=parseGitHub(new TextDecoder().decode(body)); } catch { return json({error:'invalid_json'},400); }
        const configured=repositories(env);
        const target=payload.repository ? configured.filter(r=>r.id===String(payload.repository.id)) : ['installation','installation_repositories','ping'].includes(event)?configured:[];
        if (!target.length) return json({ignored:true},202);
        const received=Date.now(),key=`webhooks/${new Date(received).toISOString().slice(0,10)}/${delivery}.json`;
        // R2 overwrite on duplicate is harmless only for the same signed bytes.
        const hash=await digest(body),prior=await env.ARCHIVE.head(key);
        if (prior?.customMetadata?.sha256 && prior.customMetadata.sha256!==hash) return json({error:'delivery_conflict'},409);
        await env.ARCHIVE.put(key,body,{customMetadata:{sha256:hash,event,expires:String(received+14*86400000)}});
        for (const repo of target) {
          if (payload.installation?.id && String(payload.installation.id)!==repo.installation) continue;
          await env.EVENTS.send({key,delivery,event,repository:repo.name,received});
        }
        return json({accepted:true},202);
      }
      // Reporting never enters query(), noteDemand(), configure(), or the scheduler.
      if (url.pathname==='/v1/activity' && req.method==='POST') {
        if (!await authorize(req,env)) return json({error:'unauthorized'},401);
        if (Number(req.headers.get('content-length') || 0)>16384) return json({error:'payload_too_large'},413);
        const text=await req.text();if(text.length>16384)return json({error:'payload_too_large'},413);
        let input:any;try {input=JSON.parse(text);}catch{return json({error:'invalid_json'},400);}
        if(!input || typeof input!=='object' || Array.isArray(input))return json({error:'invalid_request'},400);
        const repo=repository(env,input.repository);
        if (!repo || !await authorize(req,env,repo.name)) return json({error:'repository_unavailable'},403);
        if (!Array.isArray(input.numbers) || input.numbers.length>500 || input.numbers.some((n:any)=>!Number.isSafeInteger(n) || n<=0)) return json({error:'invalid_numbers'},400);
        return stub(env.REPOSITORIES,repo.id).fetch('https://internal/activity',{method:'POST',body:JSON.stringify({repository:repo.name,numbers:[...new Set(input.numbers)]})});
      }
      if (url.pathname==='/v1/query' && req.method==='GET') {
        let args; try { args=JSON.parse(url.searchParams.get('args') || '{}'); } catch { return json({error:'invalid_args'},400); }
        if (url.searchParams.has('allow_observed') && !['true','false'].includes(url.searchParams.get('allow_observed')!)) return json({error:'invalid_query'},400);
        return await query(req,env,{repository:url.searchParams.get('repository') || '',operation:url.searchParams.get('operation') || '',args,cursor:url.searchParams.get('cursor') || undefined,allow_observed:url.searchParams.has('allow_observed')?url.searchParams.get('allow_observed')==='true':undefined});
      }
      if (url.pathname==='/v1/query_batch' && req.method==='POST') {
        const input:any=await req.json();
        if (!Array.isArray(input.queries) || input.queries.length>50) return json({error:'invalid_batch'},400);
        const results=[];
        for (const q of input.queries) { const response=await query(req,env,q); results.push({status:response.status,result:response.status===304?null:await response.json()}); }
        return json({version:1,results});
      }
      if (['/v1/health','/v1/changes'].includes(url.pathname) && req.method==='GET') {
        const repo=repository(env,url.searchParams.get('repository') || '');
        if (!repo || !await authorize(req,env,repo.name)) return json({error:'unauthorized'},401);
        const data=await stub(env.REPOSITORIES,repo.id).fetch('https://internal/' + (url.pathname.endsWith('health')?'health':'changes?after=' + encodeURIComponent(url.searchParams.get('after') || '0')));
        if (!url.pathname.endsWith('health')) return data;
        const quota=await stub(env.INSTALLATIONS,repo.installation).fetch('https://internal/health');
        return json({state:await data.json(),quota:await quota.json()});
      }
      if (url.pathname==='/admin/bootstrap' && req.method==='POST') {
        if (!await authorize(req,env,undefined,true)) return json({error:'unauthorized'},401);
        const input:any=await req.json(),repo=repository(env,input.repository);
        if (!repo) return json({error:'repository_not_allowed'},400);
        return stub(env.REPOSITORIES,repo.id).fetch('https://internal/configure',{method:'POST',body:JSON.stringify({repository:repo.name})});
      }
      if (url.pathname==='/admin/reference' && req.method==='POST') {
        if (env.REFERENCE_READS!=='true' || !await authorize(req,env,undefined,true)) return json({error:'unauthorized'},401);
        const input:any=await req.json();
        try {validateQuery(['pr_delta','issue_delta'].includes(input.query?.operation)?{...input.query,operation:'open_prs'}:input.query);} catch {return json({error:'invalid_query'},400);}
        const repo=repository(env,input.query.repository);
        if (!repo || input.api && !['core','graphql'].includes(input.api) || input.page && (!Number.isInteger(input.page) || input.page<1)) return json({error:'invalid_query'},400);
        if (input.since && (input.query.operation!=='workflow_runs' || !Number.isFinite(Date.parse(input.since)))) return json({error:'invalid_query'},400);
        const referenceQuery=input.since?{...input.query,args:{...input.query.args,after:input.since}}:input.query;
        if(input.jobs) {
          if(!Array.isArray(input.jobs) || !input.jobs.length || input.jobs.length>100) return json({error:'invalid_batch'},400);
          try {for(const job of input.jobs) {validateQuery({...job.query,repository:repo.name});if(job.query.operation!==input.query.operation) throw new Error('mixed batch');}} catch {return json({error:'invalid_batch'},400);}
          return stub(env.INSTALLATIONS,repo.installation).fetch('https://internal/measure',{method:'POST',body:JSON.stringify({repository:repo,jobs:input.jobs,profile:input.profile})});
        }
        // The quota owner performs an uncached, catalog-only reference read. No arbitrary proxy.
        return stub(env.INSTALLATIONS,repo.installation).fetch('https://internal/read',{method:'POST',body:JSON.stringify({repository:repo,query:referenceQuery,page:input.page || 1,cursor:input.cursor,api:input.api,nested:input.nested,reference:true})});
      }
      return json({error:'not_found'},404);
    } catch { return json({error:'service_unavailable'},503); }
  },
  async queue(batch:MessageBatch<EventPointer>,env:Env):Promise<void> {
    for (const message of batch.messages) {
      const pointer=message.body,repo=repository(env,pointer.repository);
      if (!repo) { message.ack(); continue; }
      try {
        const result=await stub(env.REPOSITORIES,repo.id).fetch('https://internal/event',{method:'POST',body:JSON.stringify(pointer)});
        if (result.ok) message.ack(); else message.retry();
      } catch { message.retry(); }
    }
  },
  async scheduled(_event:ScheduledController,env:Env,ctx:ExecutionContext):Promise<void> {
    for (const repo of repositories(env)) {
      ctx.waitUntil(stub(env.REPOSITORIES,repo.id).fetch('https://internal/tick',{method:'POST',body:JSON.stringify({repository:repo.name})}));
      if(repo.name==='TauCetiProject/TauCeti') ctx.waitUntil(stub(env.REPOSITORIES,repo.id).fetch('https://internal/activity-refresh',{method:'POST'}));
      ctx.waitUntil(stub(env.INSTALLATIONS,repo.installation).fetch('https://internal/audit',{method:'POST',body:JSON.stringify(repo)}));
    }
    // Retention is explicit so raw webhook bodies and abandoned staging pages do not accumulate.
    ctx.waitUntil((async()=>{
      const page=await env.ARCHIVE.list({limit:100,include:['customMetadata'],cursor:await env.ARCHIVE.get('retention-cursor').then(o=>o?.text()) || undefined});
      for (const object of page.objects) {
        if(Number(object.customMetadata?.expires)<Date.now()) await env.ARCHIVE.delete(object.key);
        else if(object.customMetadata?.kind==='resource' && object.uploaded.getTime()<Date.now()-8*86400000) {
          const repo=repositories(env).find(r=>object.key.startsWith('resources/'+r.id+'/'));
          if(repo) {
            const response=await stub(env.REPOSITORIES,repo.id).fetch('https://internal/retained-object?key='+encodeURIComponent(object.key));
            if(response.ok && (await response.json() as any).retained===false) await env.ARCHIVE.delete(object.key);
          }
        }
      }
      if (page.truncated) await env.ARCHIVE.put('retention-cursor',page.cursor); else await env.ARCHIVE.delete('retention-cursor');
    })());
  }
} satisfies ExportedHandler<Env,EventPointer>;
