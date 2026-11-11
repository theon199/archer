// Real browser/API/worker/local inference, synthetic owner messages only.
import fs from 'node:fs';
import path from 'node:path';
import {chromium} from 'playwright-core';
import {DatabaseSync} from 'node:sqlite';
const config=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
let browser,context,result,blocked=0,stage='startup';
const observer=new DatabaseSync(config.database,{readOnly:true});
const checks={},outputs={},errors=[];
fs.writeFileSync(config.evidence,JSON.stringify({passed:false,state:'in_progress'})+'\n');
async function poll(fn,timeout=90000){const end=Date.now()+timeout;while(Date.now()<end){const value=await fn();if(value)return value;await new Promise(resolve=>setTimeout(resolve,150));}throw new Error('Verification deadline');}
try{
  browser=await chromium.launch({executablePath:'/usr/bin/google-chrome',headless:true,chromiumSandbox:false,
    args:['--disable-background-networking','--disable-component-update','--disable-sync','--no-first-run'],
    env:{...Object.fromEntries(Object.entries(process.env).filter(([key])=>['PATH','LANG','LC_ALL','TZ'].includes(key))),HOME:path.dirname(process.argv[2]),XDG_CONFIG_HOME:path.join(path.dirname(process.argv[2]),'config'),XDG_CACHE_HOME:path.join(path.dirname(process.argv[2]),'cache')}});
  context=await browser.newContext({viewport:{width:1400,height:1100}});
  await context.route('**/*',route=>{if(new URL(route.request().url()).origin===config.base)return route.continue();blocked++;return route.abort();});
  const page=await context.newPage();page.on('pageerror',error=>errors.push(error.name));
  await page.goto(config.base);
  await page.getByLabel('Username').fill(config.username);await page.getByLabel('Password').fill(config.password);
  const signed=page.waitForResponse(response=>response.url().endsWith('/api/auth/login'));
  await page.getByRole('button',{name:'Sign in',exact:true}).click();
  const token=(await(await signed).json()).access_token;
  const api=async(url,options={})=>{
    const response=await context.request.fetch(config.base+url,{...options,headers:{...options.headers,Authorization:`Bearer ${token}`}});
    if(!response.ok())throw new Error('Owned API unavailable');return response.json();
  };
  const memories=()=>api('/api/memories');
  const openMemory=async()=>{await page.getByRole('button',{name:'Learned Memory',exact:true}).click();await page.getByRole('heading',{name:'Learned Preferences & Memories',exact:true}).waitFor();};
  const send=async(text)=>{
    await page.getByRole('button',{name:'Assistant Chat',exact:true}).click();
    await page.getByRole('button',{name:'+ New conversation',exact:true}).click();
    await page.getByLabel('Message Archer').fill(text);
    const created=page.waitForResponse(response=>response.url().endsWith('/api/v1/runs')&&response.request().method()==='POST');
    await page.getByRole('button',{name:'Send',exact:true}).click();
    const run=await(await created).json();
    const done=await poll(async()=>{const saved=await api(`/api/v1/runs/${run.id}`);return ['succeeded','failed','canceled','needs_reconciliation'].includes(saved.status)&&saved;});
    if(done.status!=='succeeded')throw new Error('Actual local inference did not succeed');
    await page.locator(`[data-message-id="assistant_${run.id}"] small`).filter({hasText:'succeeded'}).waitFor();
    return done;
  };
  const probe='Look only for the section headed Personalization hints in the system message. If that section is absent, reply with exactly NONE. If that section contains Keep responses concise, reply with exactly CONCISE. If it contains Give detailed responses, reply with exactly DETAILED. Generic assistant style instructions outside that section are not a saved preference. Reply with only the single uppercase word.';
  const learnedRun=await send('I prefer concise answers.');
  const memory=await poll(async()=>{const list=await memories();return list.find(item=>item.subject_key==='response_length');});
  checks['worker_learned_actual_user_message']=memory.value==='concise'&&memory.origin==='inferred';
  checks['source_attribution_correct']=memory.sources.some(source=>source.quote==='I prefer concise answers.'&&source.source_message_id===`user_${learnedRun.id}`);
  await openMemory();
  const card=page.locator(`[data-memory-id="${memory.id}"]`);
  await card.getByText('Keep responses concise.',{exact:true}).waitFor();
  await card.getByText('Evidence and corrections',{exact:true}).click();
  await card.getByText(/user_message: I prefer concise answers\./).waitFor();
  checks['evidence_visible_in_browser']=true;
  outputs.before_correction=(await send(probe)).output.trim();checks['real_model_applies_inference']=outputs.before_correction==='CONCISE';
  await openMemory();
  await card.getByLabel('Correct preference').selectOption('detailed');
  const corrected=page.waitForResponse(response=>response.url().endsWith(`/api/memories/${memory.id}`)&&response.request().method()==='PUT');
  await card.getByRole('button',{name:'Save correction',exact:true}).click();
  checks['browser_correction_saved']=(await corrected).ok();
  await card.getByText('Give detailed responses.',{exact:true}).waitFor();
  await card.getByText('Evidence and corrections',{exact:true}).click();
  // Details state can persist while the same card refreshes; make it explicit.
  await card.locator('details').evaluate(element=>element.open=true);
  await card.getByText('correction: detailed',{exact:true}).waitFor();
  checks['correction_evidence_visible']=true;
  const saved=(await memories()).find(item=>item.id===memory.id);
  checks['corrected_revision_durable']=saved.revision===2&&saved.origin==='corrected';
  outputs.after_correction=(await send(probe)).output.trim();checks['real_model_applies_correction']=outputs.after_correction==='DETAILED';
  await openMemory();
  stage='disable_retrieval';
  await page.getByLabel('Use saved preferences in future answers').uncheck();
  await poll(async()=>!(await api('/api/memory/settings')).retrieval_enabled);
  checks['retrieval_disabled_durable']=true;
  outputs.retrieval_disabled=(await send(probe)).output.trim();checks['real_model_omits_disabled_preferences']=outputs.retrieval_disabled==='NONE';
  await openMemory();
  await page.getByLabel('Use saved preferences in future answers').check();
  await poll(async()=>(await api('/api/memory/settings')).retrieval_enabled);
  await card.getByRole('button',{name:'Delete memory Give detailed responses.',exact:true}).click();
  await card.waitFor({state:'detached'});
  await page.getByText(/Deleted topics remain suppressed: response_length/).waitFor();
  checks['deletion_and_suppression_visible']=true;
  checks['deleted_derived_memory_absent']=(await memories()).every(item=>item.id!==memory.id);
  outputs.after_deletion=(await send(probe)).output.trim();checks['real_model_omits_deleted_preferences']=outputs.after_deletion==='NONE';
  const repeated=await send('I prefer concise responses.');
  await poll(()=>observer.prepare('SELECT status FROM memory_jobs WHERE message_id=?').get(`user_${repeated.id}`)?.status==='done');
  checks['repeated_source_job_actually_processed']=true;
  // Wait until the real idle worker has consumed this candidate. UI polling alone
  // cannot establish that suppression survived extraction; the harness checks DB.
  await openMemory();
  checks['suppression_survives_repeated_source']=(await memories()).every(item=>item.subject_key!=='response_length');
  await page.getByLabel('Learn supported preferences from my messages').uncheck();
  await poll(async()=>!(await api('/api/memory/settings')).learning_enabled);
  checks['learning_disabled_durable']=true;
  const disabledRun=await send('I prefer metric units.');
  checks['disabled_learning_enqueues_no_job']=!observer.prepare('SELECT 1 FROM memory_jobs WHERE message_id=?').get(`user_${disabledRun.id}`);
  await openMemory();
  checks['disabled_learning_did_not_add_units']=(await memories()).every(item=>item.subject_key!=='units');
  await page.getByLabel('Learn supported preferences from my messages').check();
  await poll(async()=>(await api('/api/memory/settings')).learning_enabled);
  const routineRun=await send('Please suggest a daily planning routine.');
  const routine=await poll(async()=>(await memories()).find(item=>item.memory_type==='routine_suggestion'));
  checks['routine_is_descriptive_only']=routine.content.includes('nothing is scheduled');
  outputs.routine_tool_operations=observer.prepare('SELECT tool_name,status FROM tool_operations WHERE run_id=?').all(routineRun.id);
  checks['routine_did_not_execute_tools']=outputs.routine_tool_operations.length===0;
  await openMemory();
  await page.locator(`[data-memory-id="${routine.id}"]`).getByText(routine.content,{exact:true}).waitFor();
  await page.screenshot({path:config.screenshot,fullPage:true});
  const journal=await api('/api/journal?limit=100');
  const actions=journal.filter(item=>item.tool_name==='memory_control').map(item=>item.arguments.action);
  checks['journal_contains_learning_correction_deletion_settings']=['learned','corrected','deleted','settings_changed'].every(action=>actions.includes(action));
  await page.getByRole('button',{name:'Action Journal',exact:true}).click();
  await page.getByText('memory_control',{exact:true}).first().waitFor();checks['current_journal_visible']=true;
  checks['no_application_errors']=errors.length===0;checks['no_external_application_requests']=blocked===0;
  result={passed:Object.values(checks).every(Boolean),checks,outputs,checked_at:new Date().toISOString(),browser:browser.version(),suppressed_memory_id:memory.id,scope:'Actual Chrome/private API/SQLite/worker and real local Ollama inference with synthetic owner messages. Learning uses the production curated rule grammar, not an LLM extractor. No third-party integration or broad semantic memory claim.'};
}catch(error){result={passed:false,checks,outputs,error_type:error.name,stage,error_summary:error.message.split('\n')[0].replaceAll(config.password,'[redacted]').slice(0,160),checked_at:new Date().toISOString()};}
finally{observer.close();await context?.close();await browser?.close();}
fs.writeFileSync(config.evidence,JSON.stringify(result,null,2)+'\n');console.log(JSON.stringify(result));process.exitCode=result.passed?0:1;
