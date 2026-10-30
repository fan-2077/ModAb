"""Real local subprocess tests; fixtures never assert scientific/model success."""
import copy
from pathlib import Path
import sys
import subprocess
import tempfile
import time
import unittest

from modab.autonomy.contracts import atomic_json, digest, read
from modab.autonomy.controller import Controller
from modab.autonomy.jobs import LocalJobs, inherited_environment
from modab.autonomy.planners import RecordedPlanner
from modab.autonomy.store import ResearchStore
from modab.autonomy.adapters import Registry
from modab.tests.test_autonomy import commission


def plan():
    return [
        {'action':'run_tool','reason':'Validate all commissioned evidence','evidence_ids':['measurements'],
         'tool_id':'reuse_evidence','input_ids':['measurements','reference','geometry'], 'parameters':{'timeout_seconds':15},'conclusion':None},
        {'action':'run_tool','reason':'Render verified factual report','evidence_ids':['measurements'],
         'tool_id':'grounded_report','input_ids':['kind:fact_bundle'], 'parameters':{'timeout_seconds':15},'conclusion':None},
        {'action':'finish','reason':'Existing evidence delivered; no new prediction was executed','evidence_ids':['measurements'],
         'tool_id':None,'input_ids':[], 'parameters':{},'conclusion':'preparation_only'},
    ]


class SimulatedControllerExit(BaseException):
    pass


def process_recovery_probe(root):
    """Child controller deliberately remains alive after launching an owned job."""
    root=Path(root)
    class SlowFixtureRegistry(Registry):
        def manifest(self, step, jobs):
            value=super().manifest(step,jobs)
            original=value['argv']
            script=jobs.directory(step['job_id'])/'slow_fixture.py'
            script.write_text('import time,subprocess,sys\ntime.sleep(2)\nsys.exit(subprocess.run('+repr(original)+').returncode)\n',encoding='utf-8')
            value['argv']=[sys.executable,'-B',str(script)]
            value['inputs'][str(script)]=digest(script)
            return value
    def stay(phase,value):
        if phase=='after_submit':
            while True:time.sleep(.1)
    with ResearchStore(root/'store') as store:
        planner=RecordedPlanner({'path':str(root/'plan.json'),'sha256':digest(root/'plan.json')})
        controller=Controller(store,{'workspace':str(root/'store')},planner,
            registry=SlowFixtureRegistry({}),checkpoint=stay)
        mid=store.missions()[0]['mission_id']
        for _ in range(3):controller.tick(mid)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = ResearchStore(self.root / 'store', migrate=True)
        self.addCleanup(self.store.close)
        self.config = {'workspace': str(self.root / 'store'), 'tools': {}}
        self.planpath = self.root / 'plan.json'
        atomic_json(self.planpath, plan())
        self.planner = RecordedPlanner({'path':str(self.planpath),'sha256':digest(self.planpath)})
        m = self.store.create(commission(), {'kind':'fixture'})
        self.mid = m['mission_id']
        self.store.approve(self.mid, m['commission_sha256'], 'fixture_user','fixture://local-tests')

    def controller(self, **kwargs):
        return Controller(self.store, self.config, self.planner, **kwargs)

    def finish(self, controller=None):
        end = time.monotonic() + 15
        while time.monotonic() < end:
            snap = (controller or self.controller()).tick(self.mid)
            if snap['status'] in {'COMPLETED_WITH_LIMITATIONS','BUDGET_EXHAUSTED','CANCELLED','UNKNOWN','NEEDS_ATTENTION'}:
                return snap
            time.sleep(.1)
        self.fail('Task did not finish: ' + str(snap))

    def test_actual_actions_report_and_service_reopen(self):
        snap = self.finish()
        self.assertEqual(snap['status'],'COMPLETED_WITH_LIMITATIONS',snap['reason'])
        self.assertEqual(len(snap['steps']),2)
        self.assertEqual(len(snap['decisions']),3)
        self.assertEqual(snap['usage']['job_attempts'],2)
        self.assertEqual(snap['reports'][-1]['document']['facts']['candidate_ids'],['candidate-01','candidate-02','candidate-03','candidate-04'])
        self.assertTrue(all(s['receipt']['process_exit_code']==0 for s in snap['steps']))
        with ResearchStore(self.root/'store') as reopened:
            self.assertEqual(reopened.snapshot(self.mid),snap)

    def test_crash_before_after_submission_and_during_collection(self):
        phases = set()
        def crash(phase, step):
            if phase in {'before_submit','after_submit','before_collection_commit'} and phase not in phases:
                phases.add(phase)
                raise SimulatedControllerExit()
        controller = self.controller(checkpoint=crash)
        end = time.monotonic()+15
        while time.monotonic()<end:
            try:
                snap=controller.tick(self.mid)
            except SimulatedControllerExit:
                continue
            if snap['status']=='COMPLETED_WITH_LIMITATIONS':break
            time.sleep(.1)
        self.assertEqual(snap['status'],'COMPLETED_WITH_LIMITATIONS',snap['reason'])
        self.assertEqual(phases,{'before_submit','after_submit','before_collection_commit'})
        self.assertEqual(len(list((self.root/'store/jobs').glob('*/execution-once.json'))),2)
        self.assertEqual(snap['usage']['job_attempts'],2)

    def test_unknown_planner_request_is_not_repeated(self):
        def crash(phase, value):
            if phase=='before_planner':raise SimulatedControllerExit()
        with self.assertRaises(SimulatedControllerExit):self.controller(checkpoint=crash).tick(self.mid)
        for _ in range(3):snap=self.controller().tick(self.mid)
        self.assertEqual(snap['status'],'UNKNOWN')
        self.assertEqual(len(snap['decisions']),1)

    def test_cancel_after_claim_before_submission_starts_no_process(self):
        self.controller().tick(self.mid)
        self.controller().tick(self.mid)
        self.store.control(self.mid,'cancel',{'kind':'fixture'})
        snap=self.controller().tick(self.mid)
        self.assertEqual(snap['status'],'CANCELLED')
        self.assertFalse(list((self.root/'store/jobs').glob('*/execution-once.json')))

    def test_actual_controller_process_exit_does_not_cancel_owned_job(self):
        code='from modab.tests.test_autonomy_controller import process_recovery_probe;process_recovery_probe('+repr(str(self.root))+')'
        process=subprocess.Popen([sys.executable,'-B','-c',code],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            end=time.monotonic()+8
            running=False
            while time.monotonic()<end:
                steps=self.store.steps(self.mid)
                if steps and LocalJobs(self.root/'store/jobs').status(steps[0]['job_id'])['status']=='RUNNING':
                    running=True;break
                if process.poll() is not None:break
                time.sleep(.05)
            self.assertTrue(running,'Child controller did not start its owned worker')
            process.terminate();process.wait(timeout=5)
            snap=self.finish()
            self.assertEqual(snap['status'],'COMPLETED_WITH_LIMITATIONS',snap['reason'])
            self.assertEqual(len(snap['steps']),2)
            self.assertEqual(len(list((self.root/'store/jobs').glob('*/execution-once.json'))),2)
        finally:
            if process.poll() is None:process.terminate();process.wait(timeout=5)

    def test_input_mutation_blocks_remaining_steps(self):
        self.controller().tick(self.mid)
        # Modify the recorded planner, not scientific source files.
        self.planpath.write_text('[]',encoding='utf-8')
        # Existing accepted decision may execute, next model call must be blocked.
        snap=self.finish()
        self.assertEqual(snap['status'],'NEEDS_ATTENTION')
        self.assertIn('Recorded planner changed',snap['reason'])


class WorkerTests(unittest.TestCase):
    def test_timeout_cancel_and_repeated_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);jobs=LocalJobs(root/'jobs')
            script=root/'child.py';script.write_text('import pathlib,time\npathlib.Path("calls.txt").open("a").write("called\\n")\ntime.sleep(10)\n',encoding='utf-8')
            for index, cancel in enumerate((False, True)):
                jid='job_fixture'+str(index);directory=jobs.directory(jid);directory.mkdir()
                manifest={'schema':'modab_owned_job_v1','job_id':jid,'mission_id':'fixture','step_id':'step_fixture',
                    'argv':[sys.executable,'-B',str(script)],'cwd':str(directory),'env':inherited_environment(),
                    'inputs':{str(script):digest(script)},'timeout_seconds':1 if not cancel else 10}
                jobs.submit(manifest);jobs.submit(manifest)
                end=time.monotonic()+10
                while time.monotonic()<end:
                    state=jobs.status(jid)
                    if cancel and state['status']=='RUNNING':jobs.cancel(jid)
                    if state['status'] in {'CANCELLED','TIMED_OUT','FAILED'}:break
                    time.sleep(.1)
                self.assertEqual(state['status'],'CANCELLED' if cancel else 'TIMED_OUT')
                self.assertLessEqual((directory/'calls.txt').read_text().count('called'),1)
                jobs.submit(manifest)
                time.sleep(.2)
                self.assertEqual(jobs.status(jid),state)


if __name__=='__main__':unittest.main()
