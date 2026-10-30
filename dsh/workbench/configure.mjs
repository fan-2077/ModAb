// Generate a local patch without changing the checked-in example or existing runtime.
import {readFile,mkdir,writeFile} from 'node:fs/promises';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {resolve,join} from 'node:path';
const root=fileURLToPath(new URL('../../',import.meta.url)).replace(/\\/g,'/').replace(/\/$/,'');
const python=process.env.MODAB_PYTHON || 'python';
const config=(process.env.MODAB_CONFIG || join(root,'runtime/autonomy-current/config.json')).replace(/\\/g,'/');
let patch=await readFile(new URL('workbench.patch.yml',import.meta.url),'utf8');
const yaml=x=>x.replaceAll("'","''");
patch=patch.replaceAll('${MODAB_ROOT_URI}',yaml(pathToFileURL(root+'/').href)).replaceAll('${MODAB_ROOT}',yaml(root)).replaceAll('${MODAB_PYTHON}',yaml(python.replace(/\\/g,'/'))).replaceAll('${MODAB_CONFIG}',yaml(config));
const target=resolve(root,'runtime/workbench-v1/local.patch.yml');
await mkdir(resolve(root,'runtime/workbench-v1'),{recursive:true});
await writeFile(target,patch);console.log(target);
