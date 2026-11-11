// Actual Chrome + private HTTP/SQLite notes; synthetic data, no mocked providers.
import fs from 'node:fs';
import path from 'node:path';
import {chromium} from 'playwright-core';
const config=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const checks={}, errors=[];
let browser,context,blocked=0,result;
fs.writeFileSync(config.evidence,JSON.stringify({passed:false,state:'in_progress'})+'\n');
try {
  browser=await chromium.launch({executablePath:'/usr/bin/google-chrome',headless:true,chromiumSandbox:false,
    args:['--disable-background-networking','--disable-component-update','--disable-sync','--no-first-run'],
    env:{...Object.fromEntries(Object.entries(process.env).filter(([key])=>['PATH','LANG','LC_ALL','TZ'].includes(key))),HOME:path.dirname(process.argv[2]),XDG_CONFIG_HOME:path.join(path.dirname(process.argv[2]),'config'),XDG_CACHE_HOME:path.join(path.dirname(process.argv[2]),'cache')}});
  context=await browser.newContext({viewport:{width:1400,height:1100}});
  await context.route('**/*',route=>{
    if(new URL(route.request().url()).origin===config.base)return route.continue();
    blocked++;return route.abort();
  });
  const page=await context.newPage();
  page.on('pageerror',error=>errors.push(error.name));
  await page.goto(config.base);
  await page.getByLabel('Username').fill(config.username);
  await page.getByLabel('Password').fill(config.password);
  const signed=page.waitForResponse(response=>response.url().endsWith('/api/auth/login'));
  await page.getByRole('button',{name:'Sign in',exact:true}).click();
  const token=(await(await signed).json()).access_token;
  const api=(url,options={})=>context.request.fetch(config.base+url,{...options,headers:{...options.headers,Authorization:`Bearer ${token}`}});
  const clickResponse=async(name,url)=>{
    const response=page.waitForResponse(value=>value.url().endsWith(url));
    await page.getByRole('button',{name,exact:true}).click();return response;
  };
  await page.getByRole('button',{name:'Personal Notes',exact:true}).click();
  await page.getByText('Notes access is unavailable. Review the permissions below.',{exact:true}).waitFor();
  checks['default_deny_visible']=true;
  await page.getByLabel('New note title').fill('Synthetic weekly plan');
  await page.getByLabel('New note content').fill('Original synthetic errands');
  checks['ungranted_create_denied']=(await clickResponse('Save Note','/api/notes')).status()===403;
  await page.getByText('Note permissions',{exact:true}).click();
  for(const action of ['list','get','create','update','history']){
    checks[`owner_grants_${action}`]=(await clickResponse(`Allow ${action}`,`/api/permissions/personal_notes/${action}`)).ok();
  }
  let lostKey;
  await page.route('**/api/notes',async route=>{
    if(route.request().method()!=='POST')return route.fallback();
    lostKey=route.request().headers()['idempotency-key'];
    await route.fetch(); // Actual server commits; lose only the acknowledgment.
    await route.abort();await page.unroute('**/api/notes');
  });
  await page.getByRole('button',{name:'Save Note',exact:true}).click();
  await page.getByText('Save status is uncertain. Retry the same note or refresh notes to inspect it.',{exact:true}).waitFor();
  const committed=await(await api('/api/notes')).json();
  checks['lost_ack_committed_once']=committed.length===1;
  const replay=await clickResponse('Save Note','/api/notes');
  checks['retry_reuses_operation_key']=replay.request().headers()['idempotency-key']===lostKey;
  await page.getByRole('heading',{name:'Synthetic weekly plan',exact:true}).waitFor();
  const id=committed[0].id, card=page.locator(`[data-note-id="${id}"]`);
  checks['replay_did_not_duplicate_note']=(await(await api('/api/notes')).json()).length===1;
  await card.getByRole('button',{name:'Edit note',exact:true}).click();
  await page.getByLabel('Edit note content').fill('Updated synthetic errands');
  checks['update_succeeded']=(await clickResponse('Save changes',`/api/notes/${id}`)).ok();
  await card.getByText('Updated synthetic errands',{exact:true}).waitFor();
  await card.getByText('Revision 2',{exact:true}).waitFor();
  await card.getByRole('button',{name:'View revisions',exact:true}).click();
  const history=card.getByRole('region',{name:'Note revision history'});
  await history.getByText('Original synthetic errands',{exact:true}).waitFor();
  await history.getByText('Updated synthetic errands',{exact:true}).waitFor();
  checks['revision_history_inspectable']=true;
  await card.getByRole('button',{name:'Edit note',exact:true}).click();
  const concurrent=await api(`/api/notes/${id}`,{method:'PUT',headers:{'Idempotency-Key':'synthetic-concurrent-edit'},data:{content:'Concurrent synthetic update',expected_revision:2}});
  checks['concurrent_revision_written']=concurrent.ok();
  await page.getByLabel('Edit note content').fill('Stale browser edit');
  checks['stale_edit_conflict_visible']=(await clickResponse('Save changes',`/api/notes/${id}`)).status()===409;
  await page.getByText('Note was not updated (409). Refresh before retrying.',{exact:true}).waitFor();
  checks['stale_edit_did_not_overwrite']=(await(await api(`/api/notes/${id}`)).json()).note.content==='Concurrent synthetic update';
  await page.getByRole('button',{name:'Cancel edit',exact:true}).click();
  await page.getByRole('button',{name:'Refresh notes',exact:true}).click();
  await card.getByText('Concurrent synthetic update',{exact:true}).waitFor();
  await clickResponse('Revoke notes history','/api/permissions/personal_notes/history');
  checks['revoked_history_denied']=(await clickResponse('View revisions',`/api/notes/${id}/history`)).status()===403;
  checks['known_other_note_denied']=(await api('/api/notes/synthetic-other-note')).status()===404;
  checks['delete_without_grant_denied']=(await clickResponse('Delete note Synthetic weekly plan',`/api/notes/${id}?expected_revision=3`)).status()===403;
  await clickResponse('Revoke notes list','/api/permissions/personal_notes/list');
  await card.waitFor({state:'detached'});
  checks['revoke_list_clears_cached_cards']=true;
  checks['revoked_list_denied']=(await api('/api/notes')).status()===403;
  await clickResponse('Allow list','/api/permissions/personal_notes/list');
  await card.waitFor();
  await clickResponse('Allow history','/api/permissions/personal_notes/history');
  await card.getByRole('button',{name:'View revisions',exact:true}).click();
  await history.getByText('Concurrent synthetic update',{exact:true}).waitFor();
  checks['three_revisions_preserved']=(await(await api(`/api/notes/${id}/history`)).json()).revisions.length===3;
  await page.screenshot({path:config.screenshot,fullPage:true});
  await clickResponse('Allow delete','/api/permissions/personal_notes/delete');
  checks['delete_succeeded']=(await clickResponse('Delete note Synthetic weekly plan',`/api/notes/${id}?expected_revision=3`)).ok();
  await card.waitFor({state:'detached'});
  checks['deleted_note_unavailable']=(await api(`/api/notes/${id}`)).status()===404;
  const revisions=(await(await api(`/api/notes/${id}/history`)).json()).revisions;
  checks['deletion_revision_preserved']=revisions.length===4&&Boolean(revisions[3].deleted_at);
  const journal=await(await api('/api/journal?limit=100')).json();
  const actions=journal.filter(entry=>entry.tool_name==='personal_notes');
  checks['journal_records_denied_create']=actions.some(entry=>entry.arguments.action==='create'&&entry.permission_decision==='denied');
  checks['journal_records_conflict']=actions.some(entry=>entry.arguments.action==='update'&&entry.outcome?.status==='conflict');
  checks['journal_records_successful_create_once']=actions.filter(entry=>entry.arguments.action==='create'&&entry.outcome?.success===true).length===1;
  checks['journal_records_delete']=actions.some(entry=>entry.arguments.action==='delete'&&entry.outcome?.success===true);
  await page.getByRole('button',{name:'Action Journal',exact:true}).click();
  await page.getByText('personal_notes',{exact:true}).first().waitFor();
  checks['journal_visible_in_browser']=true;
  checks['no_other_content_rendered']=!(await page.locator('body').innerText()).includes('OTHER_NOTE_SECRET');
  checks['no_application_errors']=errors.length===0;checks['no_external_application_requests']=blocked===0;
  result={passed:Object.values(checks).every(Boolean),checks,checked_at:new Date().toISOString(),browser:browser.version(),scope:'Actual Chrome/private API/worker/SQLite and note registry with synthetic owners. A real committed note acknowledgment was aborted; replay verified. Concurrent edit injected through actual authenticated HTTP API. No model inference, third-party MCP or credentialed search claimed.'};
}catch(error){result={passed:false,checks,error_type:error.name,checked_at:new Date().toISOString()};}
finally{await context?.close();await browser?.close();}
fs.writeFileSync(config.evidence,JSON.stringify(result,null,2)+'\n');console.log(JSON.stringify(result));process.exitCode=result.passed?0:1;
