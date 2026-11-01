const fs = require('node:fs'), path = require('node:path'), crypto = require('node:crypto');
const front = path.resolve(__dirname,'..'), sha = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
const baseline = JSON.parse(fs.readFileSync(path.join(front,'SOURCE_COPY_MANIFEST.json'),'utf8'));
const previous = JSON.parse(fs.readFileSync(path.join(front,'FRONTEND_INTEGRATION_MANIFEST.json'),'utf8'));
const entries = [];
function add(full, relative) { const bytes = fs.readFileSync(full); entries.push({path:relative,bytes:bytes.length,sha256:sha(bytes)}); }
function walk(folder, prefix) { for (const item of fs.readdirSync(folder,{withFileTypes:true})) { const relative=prefix+item.name, full=path.join(folder,item.name); if (item.isSymbolicLink() || item.name.startsWith('.env') || item.name.endsWith('.tsbuildinfo')) throw Error('Unexpected source candidate: '+relative); if(item.isDirectory()) walk(full,relative+'/'); else if(item.isFile()) add(full,relative); } }
for (const name of ['app','components','lib','public','tests','reference']) walk(path.join(front,name),name+'/');
for (const name of baseline.rootFileWhitelist) add(path.join(front,name),name);
entries.sort((a,b)=>a.path<b.path?-1:a.path>b.path?1:0);
const current = new Map(entries.map(e=>[e.path,e])), originals = new Map(baseline.entries.map(e=>[e.path,e]));
const changed = baseline.entries.filter(e=>current.get(e.path)?.sha256!==e.sha256).map(e=>({path:e.path,before:e.sha256,after:current.get(e.path)?.sha256??null,...(e.path==='app/python-runtime/[file]/route.ts'?{note:'Dynamic route preserved in reference/python-runtime-route.ts.txt'}:e.path==='next-env.d.ts'?{note:'Next production build regenerated route/root-parameter type imports'}:{})}));
const pages=baseline.entries.filter(e=>/^app\/(?:[^/]+\/)?page\.tsx$/.test(e.path));
if(pages.length!==10 || pages.some(e=>current.get(e.path)?.sha256!==e.sha256)) throw Error('Original page hashes changed');
const styles=changed.filter(e=>e.path.endsWith('.css')).map(e=>e.path); if(JSON.stringify(styles)!==JSON.stringify(['components/task-bank.module.css']))throw Error('Unexpected stylesheet difference');
const fullLog=fs.readFileSync(path.join(front,'verification/postbuild-full-tests.tap'),'utf8');
if(!/# tests 182\s/.test(fullLog)||!/# pass 182\s/.test(fullLog)||!/# fail 0\s/.test(fullLog)||!/# skipped 0\s/.test(fullLog)) throw Error('Post-build test evidence is not green');
const output = JSON.parse(fs.readFileSync(path.join(front,'STATIC_EXPORT_MANIFEST.json'),'utf8'));
const manifest={...previous,
  state:'frozen static-export candidate; packaged native integration pending',
  originalRoutes:10,allOriginalPageHashesUnchanged:true,changedOriginalFiles:changed,
  newSourceFiles:entries.filter(e=>!originals.has(e.path)).map(e=>e.path),
  typecheckExit:0,focusedTestsPassed:74,focusedTestsFailed:0,
  fullNpmTests:'PASS equivalent complete package test glob via bundled Node --test --test-concurrency=1; 182/182, 0 skipped',
  staticBuild:'PASS Next 16.3.4 production export via --webpack; approved sandbox escalation',
  fullTests:{files:40,passed:182,failed:0,skipped:0,log:'verification/postbuild-full-tests.tap',node:'v24.19.0'},
  production:{command:'KONTUR_BUILD_CPUS=1 next build --webpack',cpus:1,pageConcurrency:1,exit:0,output:'out',resourceManifest:'STATIC_EXPORT_MANIFEST.json',resourceFiles:output.resourceFiles,resourceBytes:output.resourceBytes,resourceTreeSha256:output.resourceTreeSha256,buildId:output.buildId,log:'verification/production-webpack-build-approved.log'},
  buildLimitations:{defaultTurbopack:'approved dependency junction points outside filesystem root',sandboxWebpack:'compiled/typechecked; EPERM exporting nested RSC directories; same approved escalated build passed'},
  unresolved:['Packaged native/ASGI/CSP/close/import-export acceptance remains unverified by this frontend phase','Real local bank and attachment completeness require producer delivery/integration','Actual manual AI model/effort/login behavior not exercised; no real inference or GUI run'],
  sourceFiles:entries.length,sourceBytes:entries.reduce((sum,e)=>sum+e.bytes,0),sourceTreeSha256:sha(Buffer.from(JSON.stringify(entries),'utf8')),
  treeHashAlgorithm:'SHA-256 of UTF-8 JSON.stringify(entries), sorted by path, fields path/bytes/sha256',entries,
};
fs.writeFileSync(path.join(front,'FRONTEND_INTEGRATION_MANIFEST.json'),JSON.stringify(manifest,null,2)+'\n','utf8');
let report=fs.readFileSync(path.join(front,'FULL_PARITY_FRONTEND.md'),'utf8');
report=report.replace(/The current source receipt was independently checked with Node SHA-256:.*(?:\r?\n|$)/,'The current source receipt: '+manifest.sourceFiles+' files, '+manifest.sourceBytes+' bytes; SHA-256 '+manifest.sourceTreeSha256+'. Initial SOURCE_COPY_MANIFEST.json remains the original-copy receipt.\n');
report+='\nFrozen output receipt: '+output.resourceFiles+' files, '+output.resourceBytes+' bytes; resource tree SHA-256 '+output.resourceTreeSha256+'. All 10 original route sources remain identical; '+changed.length+' original files differ, including generated next-env.d.ts.\n';
fs.writeFileSync(path.join(front,'FULL_PARITY_FRONTEND.md'),report,'utf8');
console.log(JSON.stringify({sourceFiles:manifest.sourceFiles,sourceBytes:manifest.sourceBytes,sourceTreeSha256:manifest.sourceTreeSha256,changedOriginal:changed.map(e=>e.path),resourceTreeSha256:output.resourceTreeSha256,frozen:true}));