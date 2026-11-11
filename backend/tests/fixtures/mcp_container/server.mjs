// Synthetic DEVELOPMENT/TEST MCP wire fixture, never a third-party integration.
import fs from 'node:fs';
import net from 'node:net';
import readline from 'node:readline';
import {spawn} from 'node:child_process';
const inputSchema = {type:'object', properties:{mode:{type:'string',enum:['inspect','hold','descendant']}},required:['mode'],additionalProperties:false};
const outputSchema = {type:'object',properties:{uid:{type:'integer'},root_write_denied:{type:'boolean'},tmp_writable:{type:'boolean'},network_denied:{type:'boolean'},host_path_absent:{type:'boolean'},socket_absent:{type:'boolean'},ambient_secret:{type:'string'},memory_max:{type:'string'},pids_max:{type:'string'},cpu_max:{type:'string'},no_new_privs:{type:'boolean'},caps_zero:{type:'boolean'},pid:{type:'integer'}},required:['uid','root_write_denied','tmp_writable','network_denied','host_path_absent','socket_absent','ambient_secret','memory_max','pids_max','cpu_max','no_new_privs','caps_zero','pid'],additionalProperties:false};
const tool = {name:'sandbox_inspect',description:'Synthetic DEVELOPMENT ONLY sandbox probe',inputSchema,outputSchema};
process.on('SIGTERM',()=>{}); // Deliberately adversarial teardown fixture.
setInterval(()=>{},1000); // Remain alive even after Docker stdin closes; force explicit cleanup.
function reply(id,result){process.stdout.write(JSON.stringify({jsonrpc:'2.0',id,result})+'\n');}
function file(path){try{return fs.readFileSync(path,'utf8').trim();}catch{return 'unavailable';}}
async function inspect(){
 let denied=false;try{fs.writeFileSync('/var/tmp/archer-development-probe','synthetic');}catch(e){denied=e.code==='EROFS';}
 let tmp=false;try{fs.writeFileSync('/tmp/archer-development-probe','synthetic');tmp=true;}catch{}
 const networkDenied=await new Promise(resolve=>{const socket=net.connect({host:'127.0.0.1',port:11434});socket.setTimeout(300);socket.on('connect',()=>{socket.destroy();resolve(false)});socket.on('error',()=>resolve(true));socket.on('timeout',()=>{socket.destroy();resolve(true)});});
 const status=file('/proc/self/status');
 return {uid:process.getuid(),root_write_denied:denied,tmp_writable:tmp,network_denied:networkDenied,host_path_absent:!fs.existsSync('/home/sri/archer'),socket_absent:!fs.existsSync('/var/run/docker.sock'),ambient_secret:process.env.ARCHER_TEST_AMBIENT_SECRET??'absent',memory_max:file('/sys/fs/cgroup/memory.max'),pids_max:file('/sys/fs/cgroup/pids.max'),cpu_max:file('/sys/fs/cgroup/cpu.max'),no_new_privs:/NoNewPrivs:\s+1/.test(status),caps_zero:/CapEff:\s+0+\s/.test(status),pid:process.pid};
}
readline.createInterface({input:process.stdin,crlfDelay:Infinity}).on('line',async line=>{
 const request=JSON.parse(line);if(request.id===undefined)return;
 if(request.method==='initialize')reply(request.id,{protocolVersion:request.params.protocolVersion,capabilities:{tools:{}},serverInfo:{name:'archer-development-sandbox-fixture',version:'1'}});
 else if(request.method==='tools/list')reply(request.id,{tools:[tool]});
 else if(request.method==='tools/call'){
  if(request.params.arguments.mode==='descendant'){
   const child=spawn('/bin/sh',['-c','trap "" TERM; while :; do sleep 10; done'],{detached:true,stdio:'ignore'});child.unref();
  }
  if(request.params.arguments.mode!=='inspect')return; // Force SDK timeout/cancellation.
  reply(request.id,{content:[],structuredContent:await inspect(),isError:false});
 }else process.stdout.write(JSON.stringify({jsonrpc:'2.0',id:request.id,error:{code:-32601,message:'Unsupported development-fixture method'}})+'\n');
});
