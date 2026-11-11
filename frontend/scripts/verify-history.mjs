// Real local Chrome + HTTP + inference; synthetic owner data, no mocked providers.
import fs from 'node:fs';
import path from 'node:path';
import { chromium } from 'playwright-core';
const config = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const checks = {}, pageErrors = [];
let browser, context, token = '', blocked = 0;
async function poll(fn, timeout = 90000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) { const result = await fn(); if (result) return result; await new Promise(resolve => setTimeout(resolve, 150)); }
  throw new Error('Verification deadline exceeded');
}
let result;
try {
  browser = await chromium.launch({ executablePath: '/usr/bin/google-chrome', headless: true, chromiumSandbox: false,
    args: ['--disable-background-networking','--disable-component-update','--disable-sync','--no-first-run'],
    env: {...process.env, HOME:path.dirname(process.argv[2]), XDG_CONFIG_HOME:path.join(path.dirname(process.argv[2]),'config'),XDG_CACHE_HOME:path.join(path.dirname(process.argv[2]),'cache')} });
  context = await browser.newContext({viewport:{width:1400,height:1000}});
  await context.route('**/*', route => {
    if (new URL(route.request().url()).origin === config.base) return route.continue();
    blocked++; return route.abort();
  });
  const page = await context.newPage();
  page.on('pageerror', error => pageErrors.push(error.name));
  const login = async () => {
    await page.getByLabel('Username').fill(config.username);
    await page.getByLabel('Password').fill(config.password);
    const response = page.waitForResponse(response => response.url().endsWith('/api/auth/login') && response.request().method() === 'POST');
    await page.getByRole('button',{name:'Sign in',exact:true}).click();
    token = (await (await response).json()).access_token;
    await page.getByRole('heading',{name:'Saved conversations'}).waitFor();
  };
  const api = async url => {
    const response = await context.request.get(config.base+url,{headers:{Authorization:`Bearer ${token}`}});
    if (!response.ok()) throw new Error('Owned API read failed');
    return response.json();
  };
  await page.goto(config.base);
  checks['sign_in_required'] = await page.getByRole('heading',{name:'Sign in to Archer'}).isVisible();
  await login();
  await page.getByRole('button',{name:'Synthetic saved planning chat',exact:true}).click();
  await page.getByText('Synthetic saved history marker.',{exact:true}).waitFor();
  checks['saved_history_visible'] = true;
  await page.getByRole('button',{name:'New conversation',exact:true}).click();
  const prompt='Reply with exactly BROWSER_HISTORY_OK and nothing else.';
  await page.getByLabel('Message Archer').fill(prompt);
  await page.getByRole('button',{name:'Send',exact:true}).click();
  await page.waitForURL(url => url.searchParams.has('conversation'));
  const conversation = new URL(page.url()).searchParams.get('conversation');
  const runId = await poll(async () => await page.getByTestId('run-status').getAttribute('data-run-id'));
  const before = await api(`/api/v1/runs/${runId}`);
  checks['reload_during_saved_active_run'] = ['queued','generating'].includes(before.status);
  await page.reload();
  checks['reload_requires_fresh_sign_in'] = await page.getByRole('heading',{name:'Sign in to Archer'}).isVisible();
  await login();
  checks['selected_conversation_restored'] = new URL(page.url()).searchParams.get('conversation') === conversation;
  const completed = await poll(async () => {const value=await api(`/api/v1/runs/${runId}`);return ['succeeded','failed','canceled','needs_reconciliation'].includes(value.status) && value;});
  checks['real_local_run_succeeded'] = completed.status === 'succeeded';
  checks['real_model_expected_output'] = completed.output.trim() === 'BROWSER_HISTORY_OK';
  await page.waitForFunction(id => document.querySelector(`[data-message-id="assistant_${id}"] p`)?.textContent?.trim() === 'BROWSER_HISTORY_OK',runId);
  const messages = await api(`/api/v1/conversations/${conversation}/messages`);
  checks['reload_did_not_duplicate_submission'] = messages.length === 2 && messages.filter(message=>message.role==='user').length===1;
  checks['rendered_assistant_not_duplicated'] = await page.locator(`[data-message-id="assistant_${runId}"]`).count() === 1;
  checks['credentials_absent_from_browser_storage'] = await page.evaluate(() => localStorage.length === 0 && sessionStorage.length === 0);
  checks['token_absent_from_url'] = !page.url().includes(token);
  await page.locator(`[data-message-id="assistant_${runId}"] small`).filter({hasText:'succeeded'}).waitFor();
  checks['rendered_terminal_status'] = true;
  await page.screenshot({path:config.screenshot,fullPage:true});
  await page.getByRole('button',{name:'Synthetic saved planning chat',exact:true}).click();
  await page.getByText('Synthetic saved history marker.',{exact:true}).waitFor();
  await page.getByRole('button',{name:prompt,exact:true}).click();
  await page.getByText('BROWSER_HISTORY_OK',{exact:true}).waitFor();
  checks['switching_saved_conversations_preserves_output'] = true;
  await page.getByRole('button',{name:'New conversation',exact:true}).click();
  await page.getByLabel('Message Archer').fill('Write a very long general planning guide with many detailed sections.');
  await page.getByRole('button',{name:'Send',exact:true}).click();
  const cancelId=await poll(async()=>await page.getByTestId('run-status').getAttribute('data-run-id'));
  await page.getByRole('button',{name:'Cancel run',exact:true}).click();
  const canceled=await poll(async()=>{const value=await api(`/api/v1/runs/${cancelId}`);return value.status==='canceled'&&value;},15000);
  checks['browser_cancellation_durable'] = canceled.status==='canceled';
  await page.goto(config.base+'/?conversation=synthetic-other-private');
  await login();
  await page.getByText('Conversation unavailable for this account.',{exact:true}).waitFor();
  checks['known_other_conversation_denied_in_browser'] = !(await page.locator('body').innerText()).includes('OTHER_ACCOUNT_SECRET');
  await page.getByRole('button',{name:'Sign out',exact:true}).click();
  await page.getByRole('heading',{name:'Sign in to Archer'}).waitFor();
  checks['logout_clears_authenticated_view'] = true;
  checks['no_browser_application_errors'] = pageErrors.length===0;
  checks['no_external_application_requests'] = blocked===0;
  result={passed:Object.values(checks).every(Boolean),checks,checked_at:new Date().toISOString(),browser:browser.version(),run_id:runId,conversation_id:conversation,model_output:completed.output,scope:'Real installed Chrome, real HTTP/SQLite/worker and local Ollama inference with synthetic private owner data. Seeded legacy history is explicitly synthetic. Cancellation proves durable app state, not upstream GPU transport termination. No third-party integration or external network access demonstrated.'};
} catch(error) {
  result={passed:false,checks,error_type:error.name,error:error.message,checked_at:new Date().toISOString(),scope:'Browser drill failed; no mock integration substituted'};
} finally {await context?.close();await browser?.close();}
fs.writeFileSync(config.evidence,JSON.stringify(result,null,2)+'\n');
console.log(JSON.stringify(result));
process.exitCode=result.passed?0:1;
