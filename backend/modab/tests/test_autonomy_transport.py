import base64
import json
from pathlib import Path
import tempfile
import unittest

from dualctx.contracts import ContractError
from modab.autonomy.adapters import Registry
from modab.autonomy.contracts import atomic_json,digest
from modab.autonomy.transports import JobRouter
from modab.autonomy.worker import identity


class RemoteContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.jobs=JobRouter(self.root/'jobs')
        self.runtime={'enabled':True,'runtime_qualified':True,'transport':'ssh','resource_id':'fixture_remote',
            'ssh':'C:/Windows/System32/OpenSSH/ssh.exe','ssh_target':'fixture.invalid','remote_root':'/fixture/modab',
            'control_python':'/usr/bin/python3','endpoint':'/fixture/modab/remote_endpoint.py','max_collect_bytes':1048576,
            'python':'/fixture/env/bin/python','cache':'/fixture/cache','checkpoint':'/fixture/cache/boltz2_conf.ckpt',
            'expected_versions':{'boltz':'2.2.1'},'assets':{},'bin_path':'/usr/bin:/bin','home':'/fixture',
            'device_index':0,'cpu_threads':2,'memory_bytes':32*1024**3,'msa_endpoint':'https://api.colabfold.com'}

    def test_scientific_manifest_pins_input_and_has_no_affinity_request(self):
        p=self.root/'input.json'
        atomic_json(p,{'version':1,'sequences':[{'protein':{'id':'A','sequence':'ASTY','msa':'empty'}}]})
        registry=Registry({'tools':{'boltz2_predict':self.runtime}})
        mission={'mission_id':'research_fixture','commission':{'data_policy':{'allowed_endpoints':[]}}}
        params={'timeout_seconds':60,'seed':1,'sampling_steps':200,'recycling_steps':3,'diffusion_samples':1,
            'use_potentials':False,'msa_mode':'single_sequence'}
        spec=registry.prepare(mission,{'tool_id':'boltz2_predict','input_ids':['system'],'parameters':params},
            {'system':{'path':str(p),'sha256':digest(p),'kind':'boltz2_input'}},[])
        step={'spec':spec,'job_id':'job_fixture','mission_id':'research_fixture','step_id':'step_fixture'}
        manifest=registry.manifest(step,self.jobs)
        request=json.loads((self.jobs.directory('job_fixture')/'request.json').read_bytes())
        self.assertEqual(manifest['env']['CUDA_VISIBLE_DEVICES'],'0')
        self.assertIn('--no_kernels',request['command'])
        self.assertNotIn('--use_msa_server',request['command'])
        self.assertFalse(any('affinity' in x for x in request['command']))
        self.assertIn('/fixture/modab/jobs/job_fixture/input.yaml',manifest['inputs'])

    def test_unavailable_remote_submit_stays_unknown_and_collects_original_receipt(self):
        self.jobs.bind_remote('job_fixture',self.runtime)
        manifest={'schema':'modab_owned_job_v1','job_id':'job_fixture','mission_id':'research_fixture','step_id':'step_fixture',
            'argv':['/fixture/python'],'cwd':'/fixture/modab/jobs/job_fixture','env':{},'inputs':{},'timeout_seconds':60}
        calls=[]
        def offline(route,operation,jid,**payload):
            calls.append(operation)
            if operation=='submit':raise ContractError('fixture response lost')
            if operation=='status':return receipt
            if operation=='collect':return {'status':receipt,'files':{'receipt.json':base64.b64encode(json.dumps(receipt).encode()).decode()}}
        self.jobs.request=offline
        self.assertEqual(self.jobs.submit(manifest)['status'],'UNKNOWN')
        receipt={'job_id':'job_fixture','mission_id':'research_fixture','step_id':'step_fixture','manifest_sha256':identity(manifest),
            'status':'SUCCEEDED','process_exit_code':0,'elapsed_seconds':1,'files':{}}
        receipt['receipt_sha256']=identity(receipt)
        self.assertEqual(self.jobs.status('job_fixture'),receipt)
        self.assertEqual(self.jobs.status('job_fixture'),receipt)
        self.assertEqual(calls,['submit','status','collect'])

    def test_not_qualified_runtime_is_not_executable(self):
        self.runtime['runtime_qualified']=False
        with self.assertRaisesRegex(ContractError,'CAPABILITY_NOT_READY'):
            Registry({'tools':{'boltz2_predict':self.runtime}})._boltz({}, {}, {})


if __name__=='__main__':unittest.main()
