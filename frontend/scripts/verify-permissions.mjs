// Actual Chrome/private API/SQLite owner grants. Synthetic reviewed MCP metadata only.
import fs from 'node:fs';
import path from 'node:path';
import {chromium} from 'playwright-core';
const config=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const checks={},errors=[];
let browser,context,result,blocked=0;
fs.writeFileSync(config.evidence,JSON.stringify({passed:false,state:'in_progress'})+'\n');
try{
 browser=await chromium.launch({executablePath:'/usr/bin/google-chrome',headless:true,chromiumSandbox:false,args:['--disable-background-networking','--disable-component-update','--disable-sync','--no-first-run'],env:{...Object.fromEntries(Object.entries(process.env).filter(([key])=>['PATH','LANG','LC_ALL','TZ'].includes(key))),HOME:path.dirname(process.argv[2]),XDG_CONFIG_HOME:path.join(path.dirname(process.argv[2]),'config'),XDG_CACHE_HOME:path.join(path.dirname(process.argv[2]),'cache')}});
 context=await browser.newContext({viewport:{width:1400,height:1100}});
 await context.route('**/*',route=>{if(new URL(route.request().url()).origin===config.base)return route.continue();blocked++;return route.abort();});
 const signIn=async(page,username=config.username)=>{
  page.on('pageerror',error=>errors.push(error.name));await page.goto(config.base);
  await page.getByLabel('Username').fill(username);await page.getByLabel('Password').fill(config.password);
  const response=page.waitForResponse(r=>r.url().endsWith('/api/auth/login'));
  await page.getByRole('button',{name:'Sign in',exact:true}).click();
  const token=(await(await response).json()).access_token;
  await page.getByRole('button',{name:'Tool permissions',exact:true}).click();
  return token;
 };
 const page=await context.newPage(),token=await signIn(page);
 const api=(url,options={})=>context.request.fetch(config.base+url,{...options,headers:{...options.headers,Authorization:`Bearer ${token}`}});
 const card=(tool,p=page)=>p.locator(`[data-tool-name="${tool}"]`);
 const status=(tool,action,p=page)=>p.locator(`[data-permission-status="${tool}:${action}"]`);
 const change=async(tool,action,enabled,p=page)=>{
  const response=p.waitForResponse(r=>r.url().endsWith(`/api/permissions/${tool}/${action}`)&&r.request().method()==='PUT');
  await p.getByRole('button',{name:`${enabled?'Grant':'Revoke'} ${tool} ${action}`,exact:true}).click();return response;
 };
 for(const tool of ['personal_notes','read_approved_file','web_search','web_fetch','mcp_catalog_browser'])await card(tool).waitFor();
 checks['all_registered_owner_capabilities_visible']=true;
 checks['foreign_mapping_not_disclosed']=await card('mcp_other_browser').count()===0;
 await card('web_search').getByText('Unavailable: Tavily API key is not configured or is not private.',{exact:true}).waitFor();checks['missing_search_key_truthful']=true;
 await status('web_fetch','fetch').getByText('Review required: tool version changed',{exact:true}).waitFor();
 await status('mcp_catalog_browser','invoke').getByText('Review required: tool version changed',{exact:true}).waitFor();checks['stale_builtin_and_mcp_grants_need_review']=true;
 const granted=await change('web_fetch','fetch',true);
 checks['grant_uses_reviewed_version']=granted.ok()&&granted.request().postDataJSON().expected_tool_version==='1';
 await status('web_fetch','fetch').getByText('Allowed',{exact:true}).waitFor();
 let catalog=await(await api('/api/permissions')).json();
 checks['grant_persisted_current_version']=catalog.grants.some(g=>g.tool_name==='web_fetch'&&g.enabled&&g.tool_version==='1');
 checks['stale_version_api_rejected']=(await api('/api/permissions/web_fetch/fetch',{method:'PUT',data:{enabled:true,expected_tool_version:'stale-version'}})).status()===409;
 let releaseOld, markCaptured;
 const oldHeld=new Promise(resolve=>{releaseOld=resolve;});
 const oldCaptured=new Promise(resolve=>{markCaptured=resolve;});
 let holdOnce=true;
 await page.route('**/api/permissions',async route=>{
  if(!holdOnce||route.request().method()!=='GET')return route.fallback();
  holdOnce=false;const response=await route.fetch();markCaptured();await oldHeld;
  try{await route.fulfill({response});}catch{/* The real browser already aborted this superseded request. */}
 });
 await page.getByRole('button',{name:'Refresh permissions',exact:true}).click();await oldCaptured;
 checks['concurrent_real_revoke']=(await api('/api/permissions/web_fetch/fetch',{method:'PUT',data:{enabled:false,expected_tool_version:'1'}})).ok();
 await page.evaluate(()=>window.dispatchEvent(new Event('focus')));
 await status('web_fetch','fetch').getByText('Not allowed',{exact:true}).waitFor();
 releaseOld();await page.unroute('**/api/permissions');
 await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
 checks['superseded_snapshot_cannot_restore_old_grant']=await status('web_fetch','fetch').innerText()==='Not allowed';
 await change('web_fetch','fetch',true);await status('web_fetch','fetch').getByText('Allowed',{exact:true}).waitFor();
 for(const action of ['list','read']){checks[`files_${action}_grant`]=(await change('read_approved_file',action,true)).ok();await status('read_approved_file',action).getByText('Allowed',{exact:true}).waitFor();}
 const mcp=await change('mcp_catalog_browser','invoke',true);
 checks['mcp_fingerprint_sent']=mcp.ok()&&/^[a-f0-9]{64}$/.test(mcp.request().postDataJSON().expected_tool_version);
 await status('mcp_catalog_browser','invoke').getByText('Allowed',{exact:true}).waitFor();
 await page.route('**/api/permissions/web_search/search',async route=>{if(route.request().method()!=='PUT')return route.fallback();await route.fetch();await route.abort();await page.unroute('**/api/permissions/web_search/search');});
 await page.getByRole('button',{name:'Grant web_search search',exact:true}).click();
 await page.getByText('The change acknowledgement was lost. Review the refreshed server state before trying again.',{exact:true}).waitFor();
 await status('web_search','search').getByText('Allowed',{exact:true}).waitFor();checks['lost_ack_refreshed_real_committed_state']=true;
 const journal=await(await api('/api/journal?limit=100')).json();
 checks['lost_ack_no_duplicate_write']=journal.filter(e=>e.tool_name==='permission_change'&&e.arguments.tool_name==='web_search'&&e.arguments.enabled===true).length===1;
 const second=await context.newPage();await signIn(second);
 await status('web_fetch','fetch',second).getByText('Allowed',{exact:true}).waitFor();
 checks['second_tab_reads_saved_grant']=true;
 checks['second_tab_revoke_succeeded']=(await change('web_fetch','fetch',false,second)).ok();
 await status('web_fetch','fetch',second).getByText('Not allowed',{exact:true}).waitFor();
 await page.bringToFront();await page.evaluate(()=>window.dispatchEvent(new Event('focus')));
 await status('web_fetch','fetch').getByText('Not allowed',{exact:true}).waitFor();checks['cross_tab_revoke_refreshed']=true;
 await page.route('**/api/permissions',route=>route.abort());
 await page.getByRole('button',{name:'Refresh permissions',exact:true}).click();
 await page.getByText('Failed to fetch',{exact:true}).waitFor();
 checks['failed_refresh_clears_grant_controls']=await page.locator('[data-tool-name]').count()===0;
 await page.unroute('**/api/permissions');
 await page.getByRole('button',{name:'Refresh permissions',exact:true}).click();await card('web_fetch').waitFor();
 checks['explicit_refresh_recovers']=true;
 await page.getByRole('button',{name:'Grant personal_notes list',exact:true}).click();await status('personal_notes','list').getByText('Allowed',{exact:true}).waitFor();
 checks['notes_grant_applies_to_real_api']=(await api('/api/notes')).ok();
 await change('personal_notes','list',false);await status('personal_notes','list').getByText('Not allowed',{exact:true}).waitFor();
 checks['notes_revoke_applies_to_real_api']=(await api('/api/notes')).status()===403;
 await page.screenshot({path:config.screenshot,fullPage:true});
 const other=await context.newPage();await signIn(other,'synthetic-other');
 await other.getByText('Only the owner can manage tool permissions.',{exact:true}).waitFor();
 checks['nonowner_has_no_grant_controls']=await other.locator('[data-tool-name]').count()===0;
 checks['no_foreign_content_rendered']=!(await page.locator('body').innerText()).includes('OTHER_MAPPING_SECRET');
 checks['no_application_errors']=errors.length===0;checks['no_external_browser_requests']=blocked===0;
 result={passed:Object.values(checks).every(Boolean),checks,checked_at:new Date().toISOString(),browser:browser.version(),scope:'Actual Chrome/private API/SQLite and owner grants/revoke/version checks, two owner tabs and real committed lost acknowledgement. Synthetic reviewed MCP metadata only; no MCP launch, search API, model inference or file read claimed.'};
}catch(error){result={passed:false,checks,error_type:error.name,error_message:String(error.message).slice(0,250),checked_at:new Date().toISOString()};}
finally{await context?.close();await browser?.close();}
fs.writeFileSync(config.evidence,JSON.stringify(result,null,2)+'\n');console.log(JSON.stringify(result));process.exitCode=result.passed?0:1;
