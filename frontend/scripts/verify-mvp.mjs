// Practical browser check with an isolated owner and synthetic data.
import fs from 'node:fs';
import path from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { chromium } from 'playwright-core';
const config = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const checks = {}, errors = [];
let browser, context, result, blocked = 0;
async function poll(fn, timeout = 110000) {
  const until = Date.now() + timeout;
  while (Date.now() < until) { const value = await fn(); if (value) return value; await new Promise(r => setTimeout(r, 200)); }
  throw new Error('Timed out waiting for the check');
}
try {
  browser = await chromium.launch({executablePath:'/usr/bin/google-chrome',headless:true,chromiumSandbox:false,
    args:['--disable-background-networking','--disable-component-update','--disable-sync','--no-first-run'],
    env:{...Object.fromEntries(Object.entries(process.env).filter(([k])=>['PATH','LANG','LC_ALL','TZ'].includes(k))),HOME:path.dirname(process.argv[2])}});
  context = await browser.newContext({viewport:{width:1400,height:1000}});
  await context.route('**/*', route => { if (new URL(route.request().url()).origin === config.base) return route.continue(); blocked++; return route.abort(); });
  const page = await context.newPage(); page.on('pageerror', e => errors.push(e.name));
  await page.goto(config.base);
  await page.getByLabel('Username').fill(config.username); await page.getByLabel('Password').fill(config.password);
  const login = page.waitForResponse(r=>r.url().endsWith('/api/auth/login') && r.request().method()==='POST');
  await page.getByRole('button',{name:'Sign in',exact:true}).click();
  const token=(await (await login).json()).access_token;
  const api=async (url, options={})=>context.request.fetch(config.base+url,{...options,headers:{...options.headers,Authorization:`Bearer ${token}`}});
  await page.getByLabel('Message Archer').fill('Read my approved file shopping.txt and tell me the items on the shopping list.');
  await page.getByRole('button',{name:'Send',exact:true}).click();
  const runId=await poll(()=>page.getByTestId('run-status').getAttribute('data-run-id'));
  const run=await poll(async()=>{const r=await api(`/api/v1/runs/${runId}`); const v=await r.json(); return ['succeeded','failed','canceled','needs_reconciliation'].includes(v.status)&&v;});
  const db=new DatabaseSync(config.database,{readOnly:true});
  const results=db.prepare("SELECT data FROM run_events WHERE run_id=? AND event='tool_result'").all(runId).map(r=>JSON.parse(r.data)); db.close();
  checks.file_tool_succeeded=results.some(x=>x.tool==='read_approved_file'&&x.result.success===true&&x.result.content?.includes('apples'));
  checks.model_answered_from_file=run.status==='succeeded'&&['apples','bread','tea'].every(x=>run.output.toLowerCase().includes(x));
  await page.locator(`[data-message-id="assistant_${runId}"]`).getByText(run.output,{exact:true}).waitFor();
  checks.answer_visible=true;
  await page.getByRole('button',{name:'Personal Notes',exact:true}).click();
  await page.getByLabel('New note title').fill('MVP test note'); await page.getByLabel('New note content').fill('A useful synthetic note.');
  const saved=page.waitForResponse(r=>r.url().endsWith('/api/notes')&&r.request().method()==='POST');
  await page.getByRole('button',{name:'Save Note',exact:true}).click(); checks.note_saved=(await saved).ok();
  await page.getByRole('heading',{name:'MVP test note',exact:true}).waitFor(); checks.note_visible=true;
  await page.getByRole('button',{name:'Action Journal',exact:true}).click();
  await page.getByText('personal_notes',{exact:true}).first().waitFor(); checks.journal_visible=true;
  await page.getByRole('button',{name:'Learned Memory',exact:true}).click();
  const toggle=page.getByLabel('Use saved preferences in future answers'); await toggle.waitFor();
  const original=await toggle.isChecked(); const changed=page.waitForResponse(r=>r.url().endsWith('/api/memory/settings')&&r.request().method()==='PUT');
  await toggle.setChecked(!original); checks.memory_setting_saved=(await changed).ok();
  await poll(async()=>!(await toggle.isDisabled()));
  checks.memory_setting_persisted=Boolean((await (await api('/api/memory/settings')).json()).retrieval_enabled)===!original;
  await page.getByRole('button',{name:'Tools',exact:true}).click();
  const state=page.locator('[data-permission-status="read_approved_file:read"]'); await state.getByText('Allowed',{exact:true}).waitFor();
  const revoked=page.waitForResponse(r=>r.url().endsWith('/api/permissions/read_approved_file/read')&&r.request().method()==='PUT');
  await page.getByRole('button',{name:'Revoke read_approved_file read',exact:true}).click(); checks.permission_revoked=(await revoked).ok();
  await state.getByText('Not allowed',{exact:true}).waitFor(); checks.permission_visible=true;
  checks.no_browser_errors_or_external_requests=errors.length===0&&blocked===0;
  result={passed:Object.values(checks).every(Boolean),checks,checked_at:new Date().toISOString(),browser:browser.version(),file_answer:run.output,scope:'Isolated browser and owner, actual local inference, synthetic approved file and note; no production data modified.'};
} catch(error) { result={passed:false,checks,error_type:error.name,error:error.message,checked_at:new Date().toISOString()}; }
finally { await context?.close(); await browser?.close(); }
fs.writeFileSync(config.evidence,JSON.stringify(result,null,2)+'\n'); console.log(JSON.stringify(result)); process.exitCode=result.passed?0:1;
