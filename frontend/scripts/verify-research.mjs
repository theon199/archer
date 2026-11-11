// Real browser, local model and Tavily; disposable owner and a public query.
import fs from 'node:fs';
import path from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { chromium } from 'playwright-core';

const config = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const checks = {}, errors = [];
let browser, context, result, blocked = 0;
async function poll(fn, timeout = 120000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const value = await fn();
    if (value) return value;
    await new Promise(resolve => setTimeout(resolve, 200));
  }
  throw new Error('Research verification timed out');
}
try {
  browser = await chromium.launch({executablePath:'/usr/bin/google-chrome', headless:true, chromiumSandbox:false,
    args:['--disable-background-networking','--disable-component-update','--disable-sync','--no-first-run'],
    env:{...Object.fromEntries(Object.entries(process.env).filter(([key])=>['PATH','LANG','LC_ALL','TZ'].includes(key))),
      HOME:path.dirname(process.argv[2]), XDG_CONFIG_HOME:path.join(path.dirname(process.argv[2]),'config'), XDG_CACHE_HOME:path.join(path.dirname(process.argv[2]),'cache')}});
  context = await browser.newContext();
  await context.route('**/*', route => {
    if (new URL(route.request().url()).origin === config.base) return route.continue();
    blocked++; return route.abort();
  });
  const page = await context.newPage();
  page.on('pageerror', error => errors.push(error.name));
  await page.goto(config.base);
  await page.getByLabel('Username').fill(config.username);
  await page.getByLabel('Password').fill(config.password);
  const login = page.waitForResponse(response => response.url().endsWith('/api/auth/login') && response.request().method() === 'POST');
  await page.getByRole('button',{name:'Sign in',exact:true}).click();
  const token = (await (await login).json()).access_token;
  await page.getByLabel('Message Archer').waitFor();
  await page.getByLabel('Message Archer').fill('Search the web for SQLite write-ahead logging documentation. Explain one advantage and one limitation, and cite the exact source URLs returned by your search.');
  await page.getByRole('button',{name:'Send',exact:true}).click();
  const runId = await poll(() => page.getByTestId('run-status').getAttribute('data-run-id'));
  const completed = await poll(async () => {
    const response = await context.request.get(`${config.base}/api/v1/runs/${runId}`,{headers:{Authorization:`Bearer ${token}`}});
    if (!response.ok()) throw new Error('Run snapshot unavailable');
    const run = await response.json();
    return ['succeeded','failed','canceled','needs_reconciliation'].includes(run.status) && run;
  });
  const db = new DatabaseSync(config.database,{readOnly:true});
  const events = db.prepare('SELECT event,data FROM run_events WHERE run_id=? ORDER BY seq').all(runId);
  db.close();
  const results = events.filter(event => event.event === 'tool_result').map(event => JSON.parse(event.data));
  const search = results.find(item => item.tool === 'web_search')?.result;
  const urls = (search?.results ?? []).map(item => item.url);
  checks.real_search_selected_and_succeeded = search?.success === true && urls.length > 0;
  checks.run_succeeded = completed.status === 'succeeded';
  checks.answer_cites_returned_source = urls.some(url => completed.output.includes(url));
  checks.answer_addresses_wal = /write.ahead|\bwal\b/i.test(completed.output);
  await page.locator(`[data-message-id="assistant_${runId}"]`).getByText(completed.output,{exact:true}).waitFor();
  checks.answer_rendered = true;
  checks.no_browser_errors_or_external_requests = errors.length === 0 && blocked === 0;
  result = {passed:Object.values(checks).every(Boolean),checks,checked_at:new Date().toISOString(),browser:browser.version(),
    answer:completed.output,search_results:search?.results ?? [],run_status:completed.status,
    scope:'Actual browser, disposable owner, local model and live Tavily search. Citation matching checks returned URL identity; source relevance and answer accuracy require review.'};
} catch (error) {
  result = {passed:false,checks,error_type:error.name,error:error.message,checked_at:new Date().toISOString(),scope:'Research drill failed; no mocked search or model substituted.'};
} finally { await context?.close(); await browser?.close(); }
fs.writeFileSync(config.evidence,JSON.stringify(result,null,2)+'\n',{flag:'wx'});
console.log(JSON.stringify(result));
process.exitCode=result.passed?0:1;
