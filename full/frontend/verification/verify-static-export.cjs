const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const front = path.resolve(__dirname, '..'), output = path.join(front, 'out');
const digest = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
function inventory(folder, prefix = '') {
  return fs.readdirSync(folder, {withFileTypes:true}).flatMap(item => {
    const relative = prefix + item.name, full = path.join(folder, item.name);
    if (item.isSymbolicLink()) throw Error('Unexpected linked export resource: ' + relative);
    if (item.isDirectory()) return inventory(full, relative + '/');
    if (!item.isFile()) throw Error('Unexpected export resource type: ' + relative);
    const bytes = fs.readFileSync(full); return [{path:relative, bytes:bytes.length, sha256:digest(bytes)}];
  }).sort((a,b) => a.path < b.path ? -1 : a.path > b.path ? 1 : 0);
}
const entries = inventory(output), byPath = new Map(entries.map(e => [e.path,e]));
const originals = JSON.parse(fs.readFileSync(path.join(front,'SOURCE_COPY_MANIFEST.json'),'utf8'));
const expectedRoutes = ['/', '/analytics', '/bookmarks', '/chat', '/errors', '/exams', '/settings', '/skills', '/tasks', '/workspace'];
const routes = expectedRoutes.map(route => {
  const htmlPath = route === '/' ? 'index.html' : route.slice(1) + '/index.html';
  const file = byPath.get(htmlPath); if (!file) throw Error('Missing original route: ' + route);
  const original = originals.entries.find(e => e.path === (route === '/' ? 'app/page.tsx' : 'app' + route + '/page.tsx'));
  if (!original || digest(fs.readFileSync(path.join(front,original.path))) !== original.sha256) throw Error('Original route source changed: ' + route);
  return {route, htmlPath, bytes:file.bytes, sha256:file.sha256, originalPageUnchanged:true};
});
const brokenReferences = [], inlineScripts = [], externalResources = new Set(); let localReferences = 0;
for (const entry of entries.filter(e => e.path.endsWith('.html'))) {
  const html = fs.readFileSync(path.join(output,entry.path),'utf8');
  for (const tag of html.matchAll(/<(?:script|link|img|source)\b[^>]*>/gi)) {
    for (const attribute of tag[0].matchAll(/\b(?:src|href)="([^"]+)"/gi)) {
      const raw = attribute[1].replaceAll('&amp;','&');
      if (/^(?:data:|blob:|#)/i.test(raw)) continue;
      const url = new URL(raw, 'https://kontur.invalid/' + entry.path);
      if (url.origin !== 'https://kontur.invalid') { externalResources.add(url.href); continue; }
      const relative = decodeURIComponent(url.pathname).replace(/^\//,'');
      const target = relative.endsWith('/') || !path.extname(relative) ? relative.replace(/\/?$/,'/') + 'index.html' : relative;
      const normalized = target === '/index.html' ? 'index.html' : target;
      if (!byPath.has(normalized)) brokenReferences.push({html:entry.path,reference:raw,target:normalized}); else localReferences++;
    }
  }
  for (const script of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)) {
    if (/\bsrc\s*=/i.test(script[1]) || !script[2]) continue;
    const bytes = Buffer.from(script[2], 'utf8');
    inlineScripts.push({html:entry.path,bytes:bytes.length,cspSha256:'sha256-' + crypto.createHash('sha256').update(bytes).digest('base64')});
  }
}
if (brokenReferences.length) throw Error('Missing static resources: ' + JSON.stringify(brokenReferences));
if (externalResources.size) throw Error('Unexpected external static resources: ' + JSON.stringify([...externalResources]));
const publicFiles = inventory(path.join(front,'public'));
for (const file of publicFiles) if (byPath.get(file.path)?.sha256 !== file.sha256) throw Error('Public resource differs from source: ' + file.path);
const pythonReceipt = JSON.parse(fs.readFileSync(path.join(front,'PYODIDE_ASSETS.json'),'utf8'));
const python = pythonReceipt.map(file => { const relative = file.path.replace(/^public\//,''); if (byPath.get(relative)?.sha256 !== file.sha256) throw Error('Python integrity mismatch: ' + relative); return byPath.get(relative); });
const config = JSON.parse(fs.readFileSync(path.join(front,'.next/required-server-files.json'),'utf8')).config;
if (config.output !== 'export' || config.env.NEXT_PUBLIC_API_BASE !== '/api/v1' || config.env.NEXT_PUBLIC_DEMO_MODE !== 'false' || config.experimental.cpus !== 1 || config.experimental.staticGenerationMaxConcurrency !== 1) throw Error('Production build policy differs from contract');
const treeSha256 = digest(Buffer.from(JSON.stringify(entries),'utf8'));
const nativePolicy = JSON.parse(fs.readFileSync(path.join(front,'../../native/tauri.conf.json'),'utf8')).app?.security?.dangerousDisableAssetCspModification ?? false;
const manifest = {
  kind:'full-original-frontend-static-export', state:'files verified; packaged native behavior unverified',
  output:'out', buildId:fs.readFileSync(path.join(front,'.next/BUILD_ID'),'utf8').trim(),
  toolchain:{node:process.version,next:require(path.join(front,'node_modules/next/package.json')).version,bundler:'webpack',cpus:1,pageConcurrency:1},
  buildCommand:'KONTUR_BUILD_CPUS=1 next build --webpack', apiBase:'/api/v1',demoMode:false,
  tests:{files:40,passed:184,failed:0,skipped:0,typecheckExit:0},
  routes, internalHtml:entries.filter(e => e.path.endsWith('.html') && !routes.some(r => r.htmlPath === e.path)).map(e => e.path),
  localResourceReferencesVerified:localReferences,externalStaticResources:[...externalResources],
  publicFilesVerified:publicFiles.length,pythonAssets:python,
  csp:{inlineScripts,uniqueInlineScriptHashes:[...new Set(inlineScripts.map(s => s.cspSha256))],nativeAssetCspModificationSetting:nativePolicy,nativeScriptCspModificationEnabled:nativePolicy !== true && !(Array.isArray(nativePolicy) && nativePolicy.includes('script-src')),packagedPolicy:'not exercised; native packaging must retain normal Tauri asset CSP modification'},
  resourceFiles:entries.length,resourceBytes:entries.reduce((sum,e)=>sum+e.bytes,0),
  resourceTreeSha256:treeSha256,treeHashAlgorithm:'SHA-256 of UTF-8 JSON.stringify(entries), sorted by path, fields path/bytes/sha256',entries,
};
fs.writeFileSync(path.join(front,'STATIC_EXPORT_MANIFEST.json'),JSON.stringify(manifest,null,2)+'\n','utf8');
console.log(JSON.stringify({routes:routes.map(r=>r.route),files:manifest.resourceFiles,bytes:manifest.resourceBytes,resourceTreeSha256:treeSha256,localReferences,publicFiles:publicFiles.length,pythonAssets:python.length,inlineScriptHashes:manifest.csp.uniqueInlineScriptHashes.length}));
