import assert from "node:assert/strict";
import test from "node:test";
import { sourceModule } from "./source-module.mjs";

const { api } = await sourceModule("api");
const fallback = {query:"Example Coffee",provider:"Wikidata",status:"browser_lookup_required",cached:false,stale:false,fetched_at:null,message:"",matches:[]};
const json = (body, status=200, headers={}) => new Response(JSON.stringify(body), {status,headers});

function sandbox(t) {
  const originalFetch = globalThis.fetch;
  const originalStorage = globalThis.sessionStorage;
  const saved = new Map();
  globalThis.sessionStorage = {getItem:k=>saved.get(k)??null,setItem:(k,v)=>saved.set(k,v)};
  t.after(()=>{ globalThis.fetch=originalFetch; globalThis.sessionStorage=originalStorage; });
}

test("browser business lookup sends canonical name, verifies claims, and caches candidates", async t => {
  sandbox(t);
  const calls=[];
  globalThis.fetch=async (url, options) => {
    calls.push({url,options});
    if(calls.length===1) return json(fallback);
    if(calls.length===2) return json({search:[{id:"Q123",label:"Example Coffee",description:"coffee shop chain"}]});
    if(calls.length===3) return json({entities:{Q123:{claims:{P31:[{mainsnak:{datavalue:{value:{id:"Q11707"}}}}]}}}});
    return json({...fallback,status:"ok"});
  };
  const result=await api.lookupMerchant("example coffee");
  assert.equal(result.status,"ok");
  assert.equal(new URL(calls[1].url).searchParams.get("search"),"Example Coffee");
  assert.equal(new URL(calls[1].url).searchParams.get("origin"),"*");
  assert.equal(new URL(calls[1].url).searchParams.has("maxlag"),false);
  assert.equal(calls[1].options.credentials,"omit");
  assert.deepEqual(JSON.parse(calls[3].options.body).matches[0].instance_of,["Q11707"]);
});

test("HTTP 200 maxlag is an error, retains stale suggestions, and is not cached as empty", async t => {
  sandbox(t);
  const stale={...fallback,cached:true,stale:true,matches:[{id:"Q123",label:"Example Coffee"}]};
  let remoteCalls=0;
  globalThis.fetch=async url => url.startsWith("/api/") ? json(stale) : (remoteCalls++,json({error:{code:"maxlag"}}));
  const first=await api.lookupMerchant("Example Coffee");
  const second=await api.lookupMerchant("Example Coffee");
  assert.equal(first.status,"rate_limited");
  assert.deepEqual(first.matches,stale.matches);
  assert.equal(second.status,"rate_limited");
  assert.equal(remoteCalls,1);
});

test("malformed search or missing claims never create a cached empty result", async t => {
  sandbox(t);
  for(const malformed of ["search","claims"]) {
    const calls=[];
    globalThis.fetch=async (url,options) => {
      calls.push({url,options});
      if(calls.length===1) return json(fallback);
      if(calls.length===2) return json(malformed==="search" ? {} : {search:[{id:"Q123",label:"Example Coffee",description:"coffee shop chain"}]});
      return json({});
    };
    const result=await api.lookupMerchant("Example Coffee");
    assert.equal(result.status,"unavailable");
    assert.equal(calls.some(c=>c.options?.method==="POST"),false);
  }
});

test("Wikidata Retry-After prevents repeated browser requests", async t => {
  sandbox(t);
  let remoteCalls=0;
  globalThis.fetch=async url => url.startsWith("/api/") ? json(fallback) : (remoteCalls++,json({},429,{"Retry-After":"90"}));
  const first=await api.lookupMerchant("Example Coffee");
  const second=await api.lookupMerchant("Example Coffee");
  assert.equal(first.status,"rate_limited");
  assert.equal(first.retry_after,90);
  assert.equal(second.status,"rate_limited");
  assert.equal(remoteCalls,1);
});
