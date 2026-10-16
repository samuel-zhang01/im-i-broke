import assert from 'node:assert/strict';
import test from 'node:test';
import { sourceModule } from './source-module.mjs';
const { accountSelectionRequest } = await sourceModule('accountSelectionRequest');
const { fetchInvestmentAnalysis, fetchInvestmentInsights } = await sourceModule('investmentAnalysisApi');
const { fetchInvestmentCalendar } = await sourceModule('calendarApi');
const { fetchInvestmentExposure } = await sourceModule('investmentExposureApi');
const { fetchInvestmentCoverage } = await sourceModule('investmentCoverageApi');
const { fetchMarketValue } = await sourceModule('marketValueApi');
const { fetchWealth } = await sourceModule('wealthApi');
const ids = Array.from({length: 90}, (_, i) => `connection:${String(Math.floor(i/30)).padStart(32,'0')}:${String(i).padStart(8,'0')}-1111-2222-3333-444444444444`);

test('small GET selections retain empty-selection semantics, filters and cancellation', async () => {
  const original=globalThis.fetch, calls=[], signal=new AbortController().signal;
  globalThis.fetch=async (url,options)=>{ calls.push({url,options}); return new Response('{}'); };
  try {
    const filters=new URLSearchParams({currency:'EUR',range:'1w'});
    await accountSelectionRequest('/api/investments/analysis',filters,[],signal);
    await accountSelectionRequest('/api/investments/analysis',filters,['a&b'],signal);
    assert.equal(calls[0].url,'/api/investments/analysis?currency=EUR&range=1w&account=__none__');
    assert.equal(new URL(calls[1].url,'http://localhost').searchParams.get('account'),'a&b');
    assert.equal(calls[1].options.method,undefined);
    assert.equal(calls[1].options.signal,signal);
    assert.equal(filters.has('account'),false);
  } finally {globalThis.fetch=original;}
});

test('every aggregate investment reader transports large selections without changing its filters or auth protections', async () => {
  const original=globalThis.fetch, calls=[], signal=new AbortController().signal;
  globalThis.fetch=async(url,options)=>{
    calls.push({url,options});
    return Response.json({summary:{},coverage:{},items:[],holdings:[],accounts:[],metric:'account_value_change',days:[],months:[],points:[],scenarios:{},instruments:[],account_overlap:[],performance:{},methodology:[],basis:'current_holdings',warnings:[]});
  };
  try {
    await fetchInvestmentAnalysis(ids,'EUR','1w',signal);
    await fetchInvestmentInsights(ids,'EUR',signal);
    await fetchInvestmentCalendar(ids,'EUR',2026,8,'recorded',signal);
    await fetchInvestmentExposure(ids,'EUR',signal);
    await fetchInvestmentCoverage(ids,'EUR',signal);
    await fetchMarketValue(ids,'EUR','1w','1d',signal);
    await fetchWealth({currency:'EUR',range:'1m',accountIds:ids},signal);
    assert.equal(calls.length,7);
    for(const {url,options} of calls){
      assert.ok(url.length<4000);
      assert.equal(options.method,'POST');
      assert.deepEqual(JSON.parse(options.body),{account:ids});
      assert.equal(options.headers.get('Content-Type'),'application/json');
      assert.equal(options.headers.get('X-Ledger-Request'),'1');
      assert.equal(options.credentials,'same-origin');
      assert.equal(options.cache,'no-store');
      assert.equal(options.signal,signal);
      assert.equal(new URL(url,'http://localhost').searchParams.get('currency'),'EUR');
    }
    assert.equal(calls[2].url,'/api/investments/analysis/calendar?currency=EUR&year=2026&month=8&source=recorded');
    assert.equal(calls[5].url,'/api/investments/market/value?currency=EUR&range=1w&interval=1d');
  }finally{globalThis.fetch=original;}
});
