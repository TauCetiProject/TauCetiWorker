import { DurableObject } from 'cloudflare:workers';
import { Env, json, Repository, stub, parseGitHub, digest } from './protocol';
import { Recipe, extract, recipes, batchQuery, batchLimit } from './catalog';
import { settings } from './reconciliation';

class QuotaDeferred extends Error {
  constructor(readonly result:any) { super('GitHub quota deferred'); }
}

export class InstallationScheduler extends DurableObject<Env> {
  private tokenFlight?:Promise<string>;
  private auditFlight?:Promise<void>;
  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    ctx.storage.sql.exec(`CREATE TABLE IF NOT EXISTS budgets(resource TEXT PRIMARY KEY,remaining INTEGER,lim INTEGER,reset INTEGER,blocked INTEGER DEFAULT 0);
      CREATE TABLE IF NOT EXISTS accounting(id INTEGER PRIMARY KEY AUTOINCREMENT,at INTEGER,operation TEXT,api TEXT,method TEXT,endpoint TEXT,status INTEGER,points INTEGER,latency INTEGER,bytes INTEGER);
      CREATE TABLE IF NOT EXISTS quota_types(category TEXT PRIMARY KEY,first_seen INTEGER,last_seen INTEGER,count INTEGER,evidence TEXT);`);
    this.ctx.storage.sql.exec('CREATE TABLE IF NOT EXISTS reservations(id TEXT PRIMARY KEY,resource TEXT,cost INTEGER,expires INTEGER);');
    this.ctx.storage.sql.exec('CREATE TABLE IF NOT EXISTS redelivery_retries(id TEXT PRIMARY KEY,due INTEGER,attempt INTEGER);');
    const columns=new Set(this.ctx.storage.sql.exec<any>('PRAGMA table_info(accounting)').toArray().map(c=>c.name));
    for(const [name,type] of Object.entries({shape:'TEXT',batch_size:'INTEGER DEFAULT 1',predicted:'INTEGER'})) if(!columns.has(name)) this.ctx.storage.sql.exec('ALTER TABLE accounting ADD COLUMN '+name+' '+type);
  }
  async jwt(): Promise<string> {
    const b64 = (bytes: Uint8Array) => btoa(String.fromCharCode(...bytes)).replace(/=/g,'').replace(/\+/g,'-').replace(/\//g,'_');
    const encode = (value: unknown) => b64(new TextEncoder().encode(JSON.stringify(value)));
    const now = Math.floor(Date.now()/1000);
    const data = encode({alg:'RS256',typ:'JWT'}) + '.' + encode({iat:now-60,exp:now+540,iss:this.env.GITHUB_APP_ID});
    const pem = this.env.GITHUB_PRIVATE_KEY;
    if (!pem?.includes('BEGIN PRIVATE KEY')) throw new Error('GitHub App key must be PKCS8');
    const der = Uint8Array.from(atob(pem.replace(/-----[^-]+-----/g,'').replace(/\s/g,'')),c=>c.charCodeAt(0));
    const key = await crypto.subtle.importKey('pkcs8',der,{name:'RSASSA-PKCS1-v1_5',hash:'SHA-256'},false,['sign']);
    const signature = new Uint8Array(await crypto.subtle.sign('RSASSA-PKCS1-v1_5',key,new TextEncoder().encode(data)));
    return data + '.' + b64(signature);
  }
  async token(installation: string, force = false): Promise<string> {
    if (this.tokenFlight) return this.tokenFlight;
    const flight=this.loadToken(installation,force);this.tokenFlight=flight;
    try {return await flight;} finally {if(this.tokenFlight===flight) this.tokenFlight=undefined;}
  }
  async loadToken(installation: string, force = false): Promise<string> {
    let t = await this.ctx.storage.get<{token:string;expires:number}>('token');
    if (!force && t && t.expires > Date.now()+300000) return t.token;
    const until=await this.ctx.storage.get<number>('secondary_until') || 0,budget=this.budget('app-core');
    if (until>Date.now() || budget?.blocked>Date.now()) throw new QuotaDeferred({deferred:true,category:until>Date.now()?'secondary':'primary:app-core',retry_at:Math.max(until,budget?.blocked || 0)});
    const response = await this.http('/app/installations/' + installation + '/access_tokens', await this.jwt(), 'POST', {}, 'installation_token', 'authentication');
    if (response.deferred) throw new QuotaDeferred(response);
    if (response.status !== 201 || !response.body.token) throw new Error('installation token unavailable');
    t = {token:response.body.token,expires:Date.parse(response.body.expires_at)};
    await this.ctx.storage.put('token',t);
    return t.token;
  }
  observe(headers: Headers): void {
    const resource = headers.get('x-ratelimit-resource');
    const remaining = Number(headers.get('x-ratelimit-remaining')), reset = Number(headers.get('x-ratelimit-reset')) * 1000;
    if (!resource || !headers.has('x-ratelimit-remaining') || !Number.isFinite(reset)) return;
    const prior = this.ctx.storage.sql.exec<any>('SELECT * FROM budgets WHERE resource=?',resource).toArray()[0];
    if (prior?.reset>reset) {
      // Contradictory windows cannot erase a concrete zero before its own
      // reset, but an expired rejection must not exhaust a newer window.
      if(remaining===0 && reset>Date.now()) this.ctx.storage.sql.exec('UPDATE budgets SET blocked=MAX(blocked,?) WHERE resource=?',reset,resource);
      return;
    }
    const blocked = remaining === 0 ? Math.max(prior?.blocked || 0, reset) : prior?.blocked || 0;
    const effective = prior?.reset === reset ? Math.min(prior.remaining,remaining) : remaining;
    this.ctx.storage.sql.exec('INSERT INTO budgets VALUES(?,?,?,?,?) ON CONFLICT(resource) DO UPDATE SET remaining=excluded.remaining,lim=excluded.lim,reset=excluded.reset,blocked=excluded.blocked',resource,effective,Number(headers.get('x-ratelimit-limit')),reset,blocked);
  }
  budget(api: string): any {
    return this.ctx.storage.sql.exec<any>('SELECT * FROM budgets WHERE resource=?',api).toArray()[0];
  }
  usable(api:string): number {
    const b=this.budget(api),now=Date.now();
    if(b?.blocked>now) return 0;
    const pending=this.ctx.storage.sql.exec<any>('SELECT COALESCE(SUM(cost),0) cost FROM reservations WHERE resource=? AND expires>?',api,now).one().cost;
    return Math.max(0,(b && b.reset>now?b.remaining:b?.lim || 5000)-pending);
  }
  deferral(api:string,cost:number): any {
    const b=this.budget(api),now=Date.now();
    if(this.usable(api)>=cost) return null;
    const actual=b && b.reset>now?b.remaining:b?.lim || 5000;
    if((!b || b.blocked<=now) && actual>=cost) return {deferred:true,category:'capacity',retry_at:now+250};
    return {deferred:true,category:'primary:'+api,retry_at:Math.max(now+1000,b?.blocked || b?.reset || now+60000)};
  }
  estimate(operation:string,api:string,shape?:string,fallback=1): number {
    if(api!=='graphql') return 1;
    const row=this.ctx.storage.sql.exec<any>('SELECT MAX(points) cost FROM accounting WHERE api=? AND operation IN (?,?) AND (? IS NULL OR shape=?) AND at>?',api,operation,'measurement_'+operation,shape || null,shape || null,Date.now()-3600000).one();
    return Math.max(fallback,row.cost || 0);
  }
  async http(path: string, token: string, method: string, body: unknown, operation: string, api: string, etag?: string): Promise<any> {
    const started = Date.now();
    const resource=api==='app' || api==='authentication'?'app-core':api;
    const query=(body as any)?.query || '';
    const shape=query?await digest(query.replace(/\b\d+\b/g,'#').replace(/"[A-Za-z0-9_+=/-]+"/g,'"?"')):path.split('?')[0].replace(/\/\d+(?=\/|$)/g,'/{id}');
    const aliases=[...query.matchAll(/q\d+:/g)].length,batchSize=aliases || 1;
    const predicted=this.estimate(operation,api,shape,operation==='review_threads_batch'?Math.max(1,Math.round(batchSize*11/100)):1);
    const secondary=await this.ctx.storage.get<number>('secondary_until') || 0;
    if(secondary>Date.now()) throw new QuotaDeferred({deferred:true,category:'secondary',retry_at:secondary});
    this.ctx.storage.sql.exec('DELETE FROM reservations WHERE expires<=?',Date.now());
    if(this.ctx.storage.sql.exec<any>('SELECT COUNT(*) n FROM reservations').one().n>=settings(this.env).concurrency) throw new QuotaDeferred({deferred:true,category:'capacity',retry_at:Date.now()+250});
    const deferred=this.deferral(resource,predicted);if(deferred) throw new QuotaDeferred(deferred);
    const reservation=crypto.randomUUID();
    this.ctx.storage.sql.exec('INSERT INTO reservations VALUES(?,?,?,?)',reservation,resource,predicted,Date.now()+settings(this.env).lease_ms);
    try {
    const headers: Record<string,string> = {'Authorization':'Bearer ' + token,'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28','User-Agent':'tauceti-github-state','Content-Type':'application/json'};
    if (etag) headers['If-None-Match'] = etag;
    const response = await fetch('https://api.github.com' + path,{method,headers,body:method === 'POST' ? JSON.stringify(body) : undefined,signal:AbortSignal.timeout(12000)});
    const text = await response.text();
    let parsed: any;
    try { parsed = text ? parseGitHub(text) : {}; } catch { parsed = {}; }
    const quotaHeaders=new Headers(response.headers);
    if (api==='app' || api==='authentication') quotaHeaders.set('x-ratelimit-resource','app-core');
    if (parsed.data?.rateLimit && !quotaHeaders.has('x-ratelimit-remaining')) {
      quotaHeaders.set('x-ratelimit-resource','graphql');
      quotaHeaders.set('x-ratelimit-remaining',String(parsed.data.rateLimit.remaining));
      quotaHeaders.set('x-ratelimit-reset',String(Date.parse(parsed.data.rateLimit.resetAt)/1000));
    }
    this.observe(quotaHeaders);
    const cost = parsed.data?.rateLimit?.cost;
    const points = typeof cost === 'number' ? cost : api === 'core' ? response.status===304?0:1 : null;
    this.ctx.storage.sql.exec('INSERT INTO accounting(at,operation,api,method,endpoint,status,points,latency,bytes,shape,batch_size,predicted) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',Date.now(),operation,api,method,path.split('?')[0].replace(/\/\d+(?=\/|$)/g,'/{id}'),response.status,points,Date.now()-started,text.length,shape,batchSize,predicted);
    if (!quotaHeaders.has('x-ratelimit-remaining')) this.ctx.storage.sql.exec('UPDATE budgets SET remaining=MAX(0,remaining-?) WHERE resource=?',points ?? predicted,resource);
    const errors = parsed.errors || [];
    const logicalQuota=errors.some((e:any)=>['RATE_LIMIT','RATE_LIMITED'].includes(e.type) || ['RATE_LIMIT','RATE_LIMITED'].includes(e.extensions?.code));
    const failed = response.status === 403 || response.status === 429 || logicalQuota;
    if (failed) {
      const msg = String(parsed.message || errors.map((e:any)=>e.message).join(' ')).toLowerCase();
      const secondaryEvidence=/secondary|abuse|content creation|concurr|cpu|compute|points per minute/.test(msg);
      const primary = !secondaryEvidence && (response.headers.get('x-ratelimit-remaining') === '0' || logicalQuota);
      const bucket=api==='app' || api==='authentication'?'app-core':api;
      const category = primary ? 'primary:' + bucket : /content|creation|spam/.test(msg) ? 'secondary:content' : /concurr/.test(msg) ? 'secondary:concurrency' : /cpu|compute|processing time/.test(msg)?'secondary:compute':/points per minute|point limit/.test(msg)?'secondary:points': /secondary|abuse|too many|rate/.test(msg) || response.status===429 ? 'secondary:unspecified' : 'permission';
      if (category !== 'permission') {
        const retry = response.headers.get('retry-after');
        const delta = retry && /^\d+$/.test(retry) ? Number(retry)*1000 : retry ? Math.max(0,Date.parse(retry)-Date.now()) : 60000;
        const reportedReset=Number(response.headers.get('x-ratelimit-reset'))*1000,current=this.budget(bucket);
        const olderWindow=primary && reportedReset>0 && current?.reset>reportedReset;
        const until = primary ? Math.max(Date.now()+1000,olderWindow?reportedReset:current?.reset || Date.now()+60000) : Date.now()+delta;
        if (primary && !olderWindow) this.ctx.storage.sql.exec('INSERT INTO budgets VALUES(?,?,?,?,?) ON CONFLICT(resource) DO UPDATE SET remaining=0,reset=MAX(reset,excluded.reset),blocked=MAX(blocked,excluded.blocked)',bucket,0,current?.lim || 5000,until,until);
        const evidence = JSON.stringify({operation,api,status:response.status,remaining:response.headers.get('x-ratelimit-remaining'),reset:response.headers.get('x-ratelimit-reset'),retry_after:retry});
        const newType = !this.ctx.storage.sql.exec('SELECT category FROM quota_types WHERE category=?',category).toArray().length;
        this.ctx.storage.sql.exec('INSERT INTO quota_types VALUES(?,?,?,?,?) ON CONFLICT(category) DO UPDATE SET last_seen=excluded.last_seen,count=count+1,evidence=excluded.evidence',category,Date.now(),Date.now(),1,evidence);
        if (newType) console.error(JSON.stringify({event:'new_quota_type',category,evidence:JSON.parse(evidence)}));
        if (!primary) await this.ctx.storage.put('secondary_until',until);
        return {status:response.status,body:parsed,deferred:true,category,retry_at:until};
      }
    }
    return {status:response.status,body:parsed,etag:response.headers.get('etag'),link:response.headers.get('link'),points,bytes:text.length,elapsed_ms:Date.now()-started,remaining:response.headers.get('x-ratelimit-remaining'),limit:response.headers.get('x-ratelimit-limit')};
    } catch(error) {
      // A lost response can still have consumed quota; retain the predicted debit.
      this.ctx.storage.sql.exec('UPDATE budgets SET remaining=MAX(0,remaining-?) WHERE resource=?',predicted,resource);
      throw error;
    } finally {this.ctx.storage.sql.exec('DELETE FROM reservations WHERE id=?',reservation);}
  }
  // All GitHub requests from all repository objects pass through this installation owner.
  // Budget selection affects background reads only; authoring writes never enter this service.
  async read(input: any): Promise<Response> {
    if (input.nested) return this.threadPage(input);
    const candidates = recipes(input.repository.name,input.query,input.cursor,input.page).map(r=>({...r,cost:this.estimate(input.query.operation,r.api,undefined,r.cost)}));
    const until = await this.ctx.storage.get<number>('secondary_until') || 0;
    if (until > Date.now()) return json({deferred:true,category:'secondary',retry_at:until});
    const available = candidates.filter(r => {
      if (input.api && r.api!==input.api) return false;
      const b = this.budget(r.api);
      return (!b || b.blocked<=Date.now()) && this.usable(r.api)>=this.estimate(input.query.operation,r.api,undefined,r.cost);
    });
    if (!available.length) {
      const stalled=candidates.map(r=>this.deferral(r.api,r.cost)).filter(Boolean);
      const temporary=stalled.find(d=>d.category==='capacity');
      return json(temporary || this.alternate(input) || stalled.sort((a,b)=>a.retry_at-b.retry_at)[0] || {deferred:true,category:'primary',retry_at:Date.now()+1000});
    }
    const minimum=Math.min(...available.map(r=>this.pressure(r)));
    // Prefer latency when quota pressure is similar. Large pressure differences
    // still preserve the scarcer pool for operations with no alternative API.
    available.sort((a,b)=>{
      const pa=this.pressure(a),pb=this.pressure(b);
      if (pa<=minimum*1.2 && pb<=minimum*1.2) {
        const stats=(r:Recipe)=>this.ctx.storage.sql.exec<any>('SELECT COUNT(*) n,AVG(latency) latency FROM accounting WHERE api=? AND operation=? AND status IN (200,304) AND at>?',r.api,input.query.operation,Date.now()-3600000).one();
        const sa=stats(a),sb=stats(b);
        if (sa.n>=3 && sb.n>=3) return sa.latency-sb.latency;
      }
      return pa-pb;
    });
    let result: any;
    for (const recipe of available) {
      let token = await this.token(input.repository.installation);
      const request = () => this.http(recipe.path,token,recipe.api==='graphql'?'POST':'GET',recipe.query?{query:recipe.query,variables:recipe.variables}:undefined,(input.reference?'reference_':'')+input.query.operation,recipe.api,input.etag);
      try {
        result = await request();
        if (result.status === 401) { token = await this.token(input.repository.installation,true); result = await request(); }
      } catch(error) {if(error instanceof QuotaDeferred) result=error.result;else throw error;}
      if (result.deferred) {
        if (String(result.category).startsWith('secondary') || result.category==='capacity') return json(result);
        continue;
      }
      if (result.status === 304) return json({unchanged:true,api:recipe.api,etag:result.etag});
      if (result.status >= 400 || result.body.errors?.length) return json({unavailable:true,status:result.status,retry_at:Date.now()+60000});
      let data;
      try { data=extract(recipe,result.body); } catch { continue; }
      if (input.query.operation==='review_threads') {
        if (data.rows.some((t:any)=>t.comments?.pageInfo?.hasNextPage)) return json({continuation:{rows:data.rows,cursor:data.cursor,etag:result.etag},api:'graphql',points:result.points});
      }
      const next = recipe.api==='graphql' ? data.cursor : result.link?.match(/<([^>]+)>; rel="next"/)?.[1];
      return json({data:data.rows,cursor:next,api:recipe.api,etag:result.etag,points:result.points,...(input.reference?{bytes:result.bytes,elapsed_ms:result.elapsed_ms,status:result.status,remaining:result.remaining,limit:result.limit}:{})});
    }
    return json(result?.deferred?this.alternate(input) || result:{unavailable:true,status:result?.status,retry_at:Date.now()+60000});
  }
  async threadPage(input:any): Promise<Response> {
    const pending=input.nested,thread=pending.rows.find((t:any)=>t.comments?.pageInfo?.hasNextPage);
    if(!thread) return json({data:pending.rows,cursor:pending.cursor,etag:pending.etag,api:'graphql'});
    const cursor=thread.comments.pageInfo.endCursor;
    if(!cursor) throw new Error('bad thread continuation');
    let token=await this.token(input.repository.installation);
    const body={query:'query($id:ID!,$cursor:String){rateLimit{cost remaining resetAt} node(id:$id){... on PullRequestReviewThread{comments(first:100,after:$cursor){pageInfo{hasNextPage endCursor} nodes{id fullDatabaseId body createdAt updatedAt authorAssociation author{login __typename} replyTo{fullDatabaseId} path line originalLine diffHunk commit{oid} originalCommit{oid}}}}}}',variables:{id:thread.id,cursor}};
    const request=()=>this.http('/graphql',token,'POST',body,(input.reference?'reference_':'')+'review_thread_overflow','graphql');
    let result=await request();if(result.status===401) {token=await this.token(input.repository.installation,true);result=await request();}
    if(result.deferred || result.status!==200 || result.body.errors?.length) return json({...result,unavailable:true});
    const conn=result.body.data?.node?.comments;
    if(!Array.isArray(conn?.nodes) || !conn.pageInfo || conn.pageInfo.hasNextPage && conn.pageInfo.endCursor===cursor) throw new Error('incomplete thread overflow');
    const ids=new Set(thread.comments.nodes.map((n:any)=>n.id));
    thread.comments.nodes.push(...conn.nodes.filter((n:any)=>!ids.has(n.id)));thread.comments.pageInfo=conn.pageInfo;
    return pending.rows.some((t:any)=>t.comments?.pageInfo?.hasNextPage)?json({continuation:pending,api:'graphql',points:result.points}):json({data:pending.rows,cursor:pending.cursor,etag:pending.etag,api:'graphql',points:result.points});
  }
  alternate(input:any): any {
    if (!input.api) return null;
    // Pagination formats differ. Restart the staged read at page one in the
    // other pool instead of reusing a REST URL as a GraphQL cursor.
    const alternative=recipes(input.repository.name,input.query,undefined,1).find(r=>{
      const b=this.budget(r.api);
      return r.api!==input.api && (!b || b.blocked<=Date.now() && (b.reset<=Date.now() || b.remaining>=r.cost));
    });
    return alternative?{restart:true,api:alternative.api}:null;
  }
  pressure(r: Recipe): number {
    const b = this.budget(r.api);
    if (!b || b.reset < Date.now()) return r.cost / 5000;
    // Spend the pool with greater usable headroom; observed charges never come from header deltas.
    return r.cost / Math.max(1,this.usable(r.api));
  }
  async batch(input:any): Promise<Response> {
    const b=this.budget('graphql'), until=await this.ctx.storage.get<number>('secondary_until') || 0;
    if (until>Date.now()) return json({deferred:true,category:'secondary',retry_at:until});
    if (!Array.isArray(input.jobs) || !input.jobs.length) throw new Error('invalid batch');
    const operation=input.jobs[0].query.operation;
    const limit=batchLimit(operation);
    const predicted=this.estimate(operation+'_batch','graphql',undefined,operation==='review_threads'?Math.max(1,Math.round(input.jobs.length*11/100)):1);
    const core=this.usable('core'),graphql=this.usable('graphql');
    if(operation!=='review_threads' && core>=input.jobs.length && predicted/Math.max(1,graphql)>1.2*input.jobs.length/Math.max(1,core)) return json({deferred:true,category:'routing:core',retry_at:Date.now()});
    const deferred=this.deferral('graphql',predicted);if(deferred) return json(deferred);
    if (input.jobs.length>limit || !['issue_comments','review_threads','statuses','reactions'].includes(operation)) throw new Error('invalid batch operation');
    const request=batchQuery(input.repository.name,input.jobs);
    let token=await this.token(input.repository.installation);
    let result=await this.http('/graphql',token,'POST',request,operation+'_batch','graphql');
    if (result.status===401) {token=await this.token(input.repository.installation,true);result=await this.http('/graphql',token,'POST',request,operation+'_batch','graphql');}
    if (result.deferred || result.status!==200 || !result.body.data) return json({...result,unavailable:true});
    const errors=result.body.errors || [];
    if (errors.some((e:any)=>!e.path?.some((p:any)=>/^q\d+$/.test(String(p))))) return json({...result,unavailable:true});
    const emojis:Record<string,string>={THUMBS_UP:'+1',THUMBS_DOWN:'-1',LAUGH:'laugh',HOORAY:'hooray',CONFUSED:'confused',HEART:'heart',ROCKET:'rocket',EYES:'eyes'};
    return json({results:input.jobs.map((j:any,i:number)=>{
      if (errors.some((e:any)=>e.path?.includes('q'+i))) return {key:j.key,unavailable:true,retry_at:Date.now()+60000};
      const item=operation==='reactions'?result.body.data?.['q'+i]:result.body.data?.repository?.['q'+i];
      if (!item) return {key:j.key,unavailable:true,retry_at:Date.now()+60000};
      if (operation==='statuses') return {key:j.key,data:(item.status?.contexts || []).map((c:any)=>({context:c.context,state:c.state,startedAt:c.createdAt})),api:'graphql'};
      const conn=item[operation==='review_threads'?'reviewThreads':operation==='reactions'?'reactions':'comments'];
      if (!conn?.nodes || !conn.pageInfo) return {key:j.key,unavailable:true,retry_at:Date.now()+60000};
      if (operation==='review_threads' && conn.nodes.some((t:any)=>t.comments?.pageInfo?.hasNextPage)) return {key:j.key,continuation:{rows:conn.nodes,cursor:conn.pageInfo.hasNextPage?conn.pageInfo.endCursor:undefined},api:'graphql'};
      const recipe=operation==='issue_comments'?recipes(input.repository.name,j.query).find(r=>r.api==='graphql'):null;
      const rows=operation==='reactions'?conn.nodes.map((n:any)=>({id:String(n.databaseId),content:emojis[n.content],created_at:n.createdAt,user:n.user || {}})):recipe?conn.nodes.map(recipe.normalize):conn.nodes;
      return {key:j.key,data:rows,api:'graphql',cursor:conn.pageInfo.hasNextPage?conn.pageInfo.endCursor:undefined};
    })});
  }
  async measure(input:any): Promise<Response> {
    if(!Array.isArray(input.jobs) || !input.jobs.length || input.jobs.length>100) throw new Error('invalid measurement batch');
    const operation=input.jobs[0].query.operation;
    const options={threadPage:input.profile?.threadPage || 10,commentPage:input.profile?.commentPage || 20,allowLarge:true};
    if(options.threadPage>20) throw new Error('invalid measurement profile');
    const request=batchQuery(input.repository.name,input.jobs,options);
    let token=await this.token(input.repository.installation);
    const execute=()=>this.http('/graphql',token,'POST',request,'measurement_'+operation+'_batch','graphql');
    let result=await execute();if(result.status===401) {token=await this.token(input.repository.installation,true);result=await execute();}
    const container=operation==='reactions'?result.body.data:result.body.data?.repository;
    const connection=operation==='review_threads'?'reviewThreads':operation==='reactions'?'reactions':'comments';
    const nodes=Object.values(container || {}).filter((n:any)=>n?.[connection]) as any[];
    return json({status:result.status,api:'graphql',points:result.points,bytes:result.bytes,seconds:(result.elapsed_ms || 0)/1000,batch_items:input.jobs.length,
      connection_overflows:nodes.filter(n=>n[connection].pageInfo?.hasNextPage).length,
      nested_overflows:nodes.filter(n=>n.reviewThreads?.nodes?.some((t:any)=>t.comments?.pageInfo?.hasNextPage)).length,
      query_sha256:await digest(request.query),remaining:result.remaining,limit:result.limit,
      error_types:(result.body.errors || []).map((e:any)=>e.type || e.extensions?.code),deferred:result.deferred,category:result.category,retry_at:result.retry_at});
  }
  async audit(repo: Repository): Promise<void> {
    if ((await this.ctx.storage.get<number>('secondary_until') || 0)>Date.now()) return;
    if (this.budget('app-core')?.blocked>Date.now()) return;
    const last = await this.ctx.storage.get<number>('delivery_audit') || 0;
    let scan=await this.ctx.storage.get<any>('delivery_scan');
    const retry=this.ctx.storage.sql.exec<any>('SELECT id FROM redelivery_retries WHERE due<=? ORDER BY due LIMIT 1',Date.now()).toArray()[0];
    if (!scan && !retry && Date.now()-last<300000) return;
    scan ||= {started:Date.now(),since:Math.max(Date.now()-3*86400000,(last || Date.now()-300000)-60000),path:'/app/hook/deliveries?per_page=100',pending:[]};
    if(retry && !scan.pending.includes(retry.id)) scan.pending.unshift(retry.id);
    const began=Date.now(),token=await this.jwt();
    // Persist pagination and redelivery progress. Busy repositories can exceed
    // one hundred deliveries between audits; a page-one-only scan loses gaps.
    // Bound each DO turn so ordinary repository reads keep making progress.
    for (let calls=0;calls<5 && Date.now()-began<12000;calls++) {
      if (scan.pending.length) {
        const id=scan.pending[0],key='redelivery:'+id;
        if (!await this.ctx.storage.get(key)) {
          const pendingRetry=this.ctx.storage.sql.exec<any>('SELECT due FROM redelivery_retries WHERE id=?',id).toArray()[0];
          if(pendingRetry?.due>Date.now()) {scan.pending.shift();await this.ctx.storage.put('delivery_scan',scan);continue;}
          const result=await this.http('/app/hook/deliveries/'+id+'/attempts',token,'POST',{},'delivery_redelivery','app');
          if (result.status!==202) {
            if(result.deferred) {await this.ctx.storage.put('delivery_scan',scan);return;}
            const failure={id,status:result.status,at:Date.now()};
            const first=!await this.ctx.storage.get('redelivery_error:'+id);
            if(first) console.error(JSON.stringify({event:'delivery_redelivery_rejected',...failure}));
            await this.ctx.storage.put('redelivery_error:'+id,failure);
            await this.ctx.storage.put('last_redelivery_failure',failure);
            // Deleted deliveries are terminal. Auth/transport failures remain
            // retryable without hiding subsequent gaps or causing repair storms.
            if(result.status===404 || result.status===410) {
              await this.ctx.storage.put(key,Date.now());
              this.ctx.storage.sql.exec('DELETE FROM redelivery_retries WHERE id=?',id);
            } else {
              const previous=this.ctx.storage.sql.exec<any>('SELECT attempt FROM redelivery_retries WHERE id=?',id).toArray()[0]?.attempt || 0;
              this.ctx.storage.sql.exec('INSERT INTO redelivery_retries VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET due=excluded.due,attempt=excluded.attempt',id,Date.now()+Math.min(3600000,300000*2**Math.min(previous,4)),previous+1);
            }
            if(first) await stub(this.env.REPOSITORIES,repo.id).fetch('https://internal/repair',{method:'POST',body:JSON.stringify({broad:true})});
            scan.pending.shift();await this.ctx.storage.put('delivery_scan',scan);continue;
          }
          await this.ctx.storage.put(key,Date.now());
          this.ctx.storage.sql.exec('DELETE FROM redelivery_retries WHERE id=?',id);
        }
        scan.pending.shift();
      } else if (scan.path) {
        const response=await this.http(scan.path,token,'GET',undefined,'delivery_audit','app');
        if (response.status!==200 || !Array.isArray(response.body)) { await this.ctx.storage.put('delivery_scan',scan); return; }
        const rows=response.body;
        scan.pending=rows.filter((d:any)=>!(d.status_code>=200 && d.status_code<300) && Date.parse(d.delivered_at)>=scan.since).map((d:any)=>String(d.id));
        const next=response.link?.match(/<([^>]+)>; rel="next"/)?.[1];
        const covered=rows.some((d:any)=>Date.parse(d.delivered_at)<=scan.since);
        const url=next?new URL(next,'https://api.github.com'):null;
        scan.path=!covered && url?.origin==='https://api.github.com'?url.pathname+url.search:null;
      }
      await this.ctx.storage.put('delivery_scan',scan);
      if (!scan.path && !scan.pending.length) {
        if (last && scan.started-last>3*86400000) await stub(this.env.REPOSITORIES,repo.id).fetch('https://internal/repair',{method:'POST',body:JSON.stringify({broad:true})});
        await this.ctx.storage.put('delivery_audit',scan.started);
        await this.ctx.storage.delete('delivery_scan');
        return;
      }
    }
  }
  async fetch(req: Request): Promise<Response> {
    try {
      const path = new URL(req.url).pathname;
      if (path==='/read') return await this.read(await req.json());
      if (path==='/batch') return await this.batch(await req.json());
      if (path==='/measure') return await this.measure(await req.json());
      if (path==='/audit') {
        this.ctx.storage.sql.exec('DELETE FROM accounting WHERE at<?',Date.now()-7*86400000);
        const repo=await req.json() as Repository;
        this.auditFlight ||= this.audit(repo).finally(()=>{this.auditFlight=undefined;});
        await this.auditFlight;return json({ok:true});
      }
      if (path==='/health') return json({redelivery_failure:await this.ctx.storage.get('last_redelivery_failure') || null,reconciliation_version:2,deployment_id:this.env.CF_VERSION?.id,settings:settings(this.env),inflight:this.ctx.storage.sql.exec('SELECT resource,COUNT(*) requests,SUM(cost) reserved_points FROM reservations WHERE expires>? GROUP BY resource',Date.now()).toArray(),budgets:this.ctx.storage.sql.exec('SELECT * FROM budgets').toArray(),quota_types:this.ctx.storage.sql.exec('SELECT * FROM quota_types').toArray(),hour:this.ctx.storage.sql.exec('SELECT api,operation,COUNT(*) requests,SUM(points) points,SUM(points IS NULL) unmeasured,SUM(bytes) bytes,AVG(latency) latency FROM accounting WHERE at>? GROUP BY api,operation',Date.now()-3600000).toArray(),recipes:this.ctx.storage.sql.exec('SELECT api,operation,shape,batch_size,COUNT(*) requests,SUM(points) points,AVG(points) mean_points,MAX(points) max_points,AVG(predicted) predicted,AVG(latency) latency,MAX(latency) max_latency FROM accounting WHERE at>? GROUP BY api,operation,shape,batch_size',Date.now()-3600000).toArray()});
      return json({error:'not_found'},404);
    } catch (error) {
      if (error instanceof QuotaDeferred) return json(error.result);
      console.error('installation operation failed',error instanceof Error?error.message:'unknown'); return json({unavailable:true,retry_at:Date.now()+60000},503);
    }
  }
}
