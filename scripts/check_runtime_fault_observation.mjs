import fs from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
const {chromium}=await import(new URL('../node_modules/playwright/index.mjs',import.meta.url));
const root=process.argv[2]||fileURLToPath(new URL('../workspace/validation-20261119-faults',import.meta.url));
const token=JSON.parse(await fs.readFile(root+'/tokens.json','utf8')).operator;
const browser=await chromium.launch();
try{
 const page=await browser.newPage();await page.addInitScript(t=>sessionStorage.setItem('dios_access_token',t),token);
 let unavailable=false;
 const status={observed_at:Date.now()/1000,actual_concurrency:null,unknown_attempts:1,workers:[],
  attempts:[{attempt_id:'ui-probe',worker_id:'ui-worker',run_id:'ui-run',phase:'result_unknown',freshness:'stale'}],
  circuits:[{resource:'ui-observation-test',state:'half_open',retry_at:0,reason:'PROVIDER_RATE_LIMITED',probe_attempt:'ui-probe'}]};
 await page.route('**/api/os/runtime/workers',route=>route.fulfill({status:unavailable?503:200,contentType:'application/json',body:JSON.stringify(unavailable?{detail:'RUNTIME_UNAVAILABLE'}:status)}));
 await page.goto('http://127.0.0.1:13082/dios/#console/tasks');
 await page.getByText('探测结果待对账，暂停新领取',{exact:false}).waitFor();
 await page.getByText(/实际并发 待确认/).waitFor();
 unavailable=true;
 await page.getByText('节点观测暂不可用，当前保留最近一次信息。').waitFor({timeout:10000});
 await page.getByText(/熔断观测待更新/).waitFor();
 await fs.writeFile(root+'/fault-observation-ui.json',JSON.stringify({scope:'UI-only injected stale observation and unavailable endpoint',unknownProbeVisible:true,unknownConcurrencyVisible:true,staleCircuitRetained:true},null,2));
 console.log('Unknown probe and stale circuit observations rendered correctly.');
}finally{await browser.close();}
