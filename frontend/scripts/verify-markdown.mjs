import fs from 'node:fs';
import path from 'node:path';
import {chromium} from 'playwright-core';

const config=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const checks={}, errors=[];
let browser,context,result,blocked=0;
try {
  browser=await chromium.launch({executablePath:'/usr/bin/google-chrome',headless:true,chromiumSandbox:false,
    args:['--disable-background-networking','--disable-component-update','--disable-sync','--no-first-run'],
    env:{...Object.fromEntries(Object.entries(process.env).filter(([key])=>['PATH','LANG','LC_ALL','TZ'].includes(key))),HOME:path.dirname(process.argv[2])}});
  context=await browser.newContext({viewport:{width:900,height:800}});
  await context.route('**/*',route=>{if(new URL(route.request().url()).origin===config.base)return route.continue();blocked++;return route.abort();});
  const page=await context.newPage(); page.on('pageerror',error=>errors.push(error.name));
  await page.goto(config.base);
  await page.getByLabel('Username').fill(config.username); await page.getByLabel('Password').fill(config.password);
  await page.getByRole('button',{name:'Sign in',exact:true}).click();
  await page.getByRole('button',{name:'Synthetic saved planning chat',exact:true}).click();
  const answer=page.locator('[data-message-id="synthetic-history"]');
  await answer.getByRole('heading',{name:'Research summary'}).waitFor();
  checks.bold=await answer.locator('strong').filter({hasText:'Useful finding'}).count()===1;
  checks.emphasis=await answer.locator('em').filter({hasText:'emphasis'}).count()===1;
  checks.list=await answer.locator('ul li').count()===2;
  checks.inline_code=await answer.locator('code').filter({hasText:'inline'}).count()===1;
  checks.table=await answer.locator('table').count()===1;
  const link=answer.getByRole('link',{name:'https://example.com/report'});
  checks.source_link=await link.getAttribute('href')==='https://example.com/report' && await link.getAttribute('target')==='_blank';
  checks.unsafe_link_inert=!(await answer.getByText('unsafe',{exact:true}).getAttribute('href'));
  checks.no_remote_image_or_script=await answer.locator('img, script').count()===0 && blocked===0;
  checks.no_browser_errors=errors.length===0;
  result={passed:Object.values(checks).every(Boolean),checks,checked_at:new Date().toISOString(),browser:browser.version(),scope:'Actual Chrome and isolated saved assistant message with synthetic Markdown and unsafe content.'};
} catch(error) {result={passed:false,checks,error_type:error.name,error:error.message,checked_at:new Date().toISOString()};}
finally {await context?.close();await browser?.close();}
fs.writeFileSync(config.evidence,JSON.stringify(result,null,2)+'\n'); console.log(JSON.stringify(result)); process.exitCode=result.passed?0:1;
