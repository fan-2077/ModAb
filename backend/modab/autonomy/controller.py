"""Persistent decision/action loop. Each tick reconciles durable work first."""
from __future__ import annotations

import math
from pathlib import Path

from dualctx.contracts import ContractError, canonical_hash
from .adapters import Registry
from .contracts import TERMINAL, atomic_json, digest, read, validate_decision, verify_inputs
from .jobs import LocalJobs
from .transports import JobRouter
from .reports import assemble, deterministic_claims,preferred_report,verify_facts
from .worker import exclusive_lock, LockHeld
from .reliability import compact_decisions,compact_receipt
from .research_review import observation_summary,validate_review

JOB_END = {'SUCCEEDED', 'FAILED', 'CANCELLED', 'TIMED_OUT'}


class Controller:
    def __init__(self, store, config, planner, *, jobs=None, registry=None, checkpoint=None):
        self.store, self.config, self.planner = store, config, planner
        self.jobs = jobs or JobRouter(Path(config['workspace']) / 'jobs')
        self.registry = registry or Registry(config)
        self.checkpoint = checkpoint or (lambda phase, value: None)

    def context(self, mid):
        snap = self.store.snapshot(mid)
        inputs = dict(snap['commission']['inputs'])
        evidence = {}
        for step in snap['steps']:
            if step['receipt']:
                evidence[step['step_id']] = compact_receipt(step['receipt'])
                for artifact in step['receipt']['artifacts']:
                    if digest(artifact['path']) != artifact['sha256']:
                        raise ContractError('Recorded output changed: ' + artifact['id'])
                    inputs[artifact['id']] = {k: artifact[k] for k in ('path', 'sha256', 'kind')}
        for key, item in inputs.items():
            if item['kind'] == 'fact_bundle':
                evidence[key] = read(item['path'])
            elif item['kind'] in {'method_requirements','structure_prediction','prediction_confidence','at8_sequence_dataset','prepared_states'}:
                evidence[key] = read(item['path'])
            elif item['kind'] == 'research_context_config':
                evidence[key] = {'methods': read(item['path'])['methods']}
        step_summary = [{**{k: step[k] for k in ('step_id', 'decision_id', 'tool_id', 'status', 'error')},
            'receipt':compact_receipt(step['receipt'])} for step in snap['steps']]
        delivery=[]
        for decision in snap['decisions']:
            response_path=self.store.workspace/'decisions'/decision['decision_id']/'response.json'
            if response_path.exists():
                response=read(response_path)
                delivery.append({'decision_id':decision['decision_id'],'stop_reason':response.get('raw_response',{}).get('stopReason'),
                    'usable_decision':response.get('decision') is not None})
        catalog={k:v for k,v in self.registry.catalog().items() if k in snap['commission']['allowed_tools']}
        report=preferred_report(snap['reports'])
        return {'mission_id': mid, 'commission': snap['commission'], 'usage': snap['usage'],
            'available_inputs': inputs, 'catalog': catalog, 'evidence': evidence,
            'input_ids_by_kind':{kind:[key for key,item in inputs.items() if item['kind']==kind] for kind in sorted({item['kind'] for item in inputs.values()})},
            'steps': step_summary, 'decisions': compact_decisions(snap['decisions']),'model_delivery':delivery,
            'revision_requests':self.store.revisions(mid),
            'research_policy':self.config.get('research_policy',{}),
            'research_progress':observation_summary(inputs,snap['steps'],snap['commission']['budget'],snap['usage']),
            'latest_report':{k:report['document'].get(k) for k in ('validation_status','proposed_conclusion','research_review')} if report else None}

    def tick(self, mid):
        try:
            with exclusive_lock(self.store.workspace / 'locks' / (mid + '.lock')):
                return self._tick(mid)
        except LockHeld:
            return self.store.snapshot(mid)
        except (ContractError, ValueError, KeyError, OSError) as exc:
            status = 'BUDGET_EXHAUSTED' if str(exc).startswith('BUDGET_EXHAUSTED:') else 'NEEDS_ATTENTION'
            self.store.status(mid, status, str(exc))
            self._fallback_report(mid)
            return self.store.snapshot(mid)

    def _fallback_report(self, mid):
        """A budget limit still delivers already verified facts, without a new call."""
        steps = self.store.steps(mid)
        for step in reversed(steps):
            for artifact in (step['receipt'] or {}).get('artifacts', []):
                if artifact['kind'] == 'fact_bundle':
                    try:
                        if digest(artifact['path']) != artifact['sha256']:
                            return
                        bundle = read(artifact['path'])
                        existing=preferred_report(self.store.snapshot(mid)['reports'])
                        if existing and canonical_hash(existing['document']['facts'])==canonical_hash(bundle):
                            verify_facts(existing['document']['facts'])
                            return
                        report = assemble(bundle, deterministic_claims(bundle), mid, steps, 'inconclusive')
                        report['delivery_reason'] = self.store.mission(mid)['reason']
                        self.store.report(mid, report)
                    except (OSError, ValueError, KeyError, ContractError):
                        pass
                    return

    def _collect(self, step, state):
        receipt = self.registry.collect(step, self.jobs.directory(step['job_id']), state)
        self.checkpoint('before_collection_commit', step)
        self.store.update_step(step['step_id'], state['status'], receipt)
        self.store.settle(step['mission_id'], step['step_id'],
            {'compute_seconds': math.ceil(state['elapsed_seconds']), 'job_attempts': 1})
        for artifact in receipt['artifacts']:
            if artifact['kind'] == 'grounded_report':
                self.store.report(step['mission_id'], read(artifact['path']))

    def _tick(self, mid):
        mission = self.store.mission(mid)
        if not mission['approval'] or mission['status'] in TERMINAL:
            return self.store.snapshot(mid)
        # A recorded receipt may precede accounting when the controller stops.
        for step in self.store.steps(mid):
            if step['receipt']:
                self.store.settle(mid, step['step_id'], {
                    'compute_seconds': math.ceil(step['receipt']['elapsed_seconds']), 'job_attempts': 1})
                for artifact in step['receipt']['artifacts']:
                    if artifact['kind'] == 'grounded_report':
                        if digest(artifact['path']) != artifact['sha256']:
                            raise ContractError('Report artifact changed')
                        self.store.report(mid, read(artifact['path']))
        active = [s for s in self.store.steps(mid) if not s['receipt']]
        waiting = False
        for step in active:
            state = self.jobs.status(step['job_id'])
            if mission['desired'] == 'CANCEL':
                if state['status'] in {'NOT_FOUND', 'PREPARED'}:
                    # Never create a tool process just to cancel it.
                    receipt = {'status': 'CANCELLED', 'job_id': step['job_id'], 'step_id': step['step_id'],
                        'tool_id': step['tool_id'], 'tool_version': step['spec']['tool_version'],
                        'process_exit_code': None, 'elapsed_seconds': 0, 'artifacts': [],
                        'scientific_prediction': False, 'reason': 'Cancelled before submission'}
                    receipt['receipt_sha256'] = canonical_hash(receipt)
                    self.store.update_step(step['step_id'], 'CANCELLED', receipt)
                    self.store.settle(mid, step['step_id'], {'compute_seconds': 0, 'job_attempts': 1})
                    continue
                self.jobs.cancel(step['job_id'])
            if state['status'] in JOB_END:
                self._collect(step, state)
                continue
            if state['status'] == 'UNKNOWN':
                self.store.update_step(step['step_id'], 'UNKNOWN', error=state.get('reason'))
                self.store.status(mid, 'UNKNOWN', state.get('reason'))
                return self.store.snapshot(mid)
            if state['status'] in {'NOT_FOUND', 'PREPARED'}:
                latest = self.store.mission(mid)
                if latest['desired'] != 'RUN':
                    waiting = True
                    continue
                manifest = self.registry.manifest(step, self.jobs)
                self.checkpoint('before_submit', step)
                # Read authority again immediately before handing off.
                if self.store.mission(mid)['desired'] != 'RUN':
                    return self.store.snapshot(mid)
                state = self.jobs.submit(manifest)
                self.checkpoint('after_submit', step)
            self.store.update_step(step['step_id'], state['status'])
            waiting = True
        mission = self.store.mission(mid)
        if mission['desired'] == 'CANCEL':
            self.store.status(mid, 'CANCELLING' if waiting else 'CANCELLED')
            return self.store.snapshot(mid)
        if mission['desired'] == 'PAUSE':
            if mission['status']=='NEEDS_ATTENTION':return self.store.snapshot(mid)
            self.store.status(mid, 'PAUSED', 'Existing work can finish; no new work submitted')
            return self.store.snapshot(mid)
        if waiting:
            self.store.status(mid, 'RUNNING')
            return self.store.snapshot(mid)
        verify_inputs(mission['commission'])
        self.recover_response(mid)
        context = self.context(mid)
        decisions = self.store.decisions(mid)
        if decisions and decisions[-1]['status'] == 'REQUEST_RESERVED':
            # No response was persisted. Retain its full budget reservation.
            self.store.status(mid, 'UNKNOWN', 'Planner request outcome unknown; reconcile the original request before retry')
            return self.store.snapshot(mid)
        # A report action already declares stop/continue in its reviewed fields.
        # Completing that accepted intent requires no extra paid model request.
        if self._finalize_reviewed_report(mid,context):return self.store.snapshot(mid)
        decision = decisions[-1] if decisions else None
        revisions=context['revision_requests']
        if decision and revisions and decision['sequence']<=revisions[-1]['after_decision_sequence']:
            decision=None
        consumed = {s['decision_id'] for s in context['steps']}
        if decision and decision['status'] == 'ACCEPTED' and decision['decision_id'] not in consumed:
            return self._apply(mission, decision, context)
        request = self.planner.prepare(context)
        did = self.store.begin_decision(mid, context, request['origin'], request['costs'])
        directory = self.store.workspace / 'decisions' / did
        directory.mkdir(parents=True, exist_ok=True)
        atomic_json(directory / 'context.json', context, immutable=True)
        self.checkpoint('before_planner', {'decision_id': did})
        # A response file written before a crash is recoverable with no new call.
        response_path = directory / 'response.json'
        try:
            response = self.planner.decide(context, request)
            atomic_json(response_path, response, immutable=True)
        except Exception as exc:
            self.store.status(mid, 'UNKNOWN', 'Planner response not available: ' + type(exc).__name__)
            return self.store.snapshot(mid)
        self._record_response(mid, did, response, context)
        return self.store.snapshot(mid)

    def _finalize_reviewed_report(self,mid,context):
        if not self.config.get('research_policy',{}).get('require_stop_review'):return False
        if context['commission']['evidence_kind']!='COMPUTATIONAL_RESEARCH':return False
        snap=self.store.snapshot(mid);entry=preferred_report(snap['reports'])
        if not entry or any(not s['receipt'] for s in snap['steps']):return False
        doc=entry['document'];review=doc.get('research_review')
        if not review or review.get('status')!='STRUCTURED_REVIEW_PASSED':return False
        if review['review']['stop_basis']=='continue_research' or review['review']['next_test']['disposition']=='execute':return False
        revisions=context.get('revision_requests',[])
        # A new user-requested review must not be closed by its historical report.
        new_report_steps=[s for s in snap['steps'] if s['tool_id']=='grounded_report' and s['receipt'] and s['receipt']['status']=='SUCCEEDED']
        if revisions:
            sequence={d['decision_id']:d['sequence'] for d in snap['decisions']}
            new_report_steps=[s for s in new_report_steps if sequence[s['decision_id']]>revisions[-1]['after_decision_sequence']]
        if not new_report_steps:return False
        artifacts=new_report_steps[-1]['receipt']['artifacts']
        if not any(a['kind']=='grounded_report' and digest(a['path'])==a['sha256'] and read(a['path'])==doc for a in artifacts):return False
        progress=context['research_progress']
        if review['observations']['successful_structure_sha256']!=progress['successful_structure_sha256']:return False
        validate_review(review['review'],doc['facts'],progress,context['catalog'],self.config['research_policy'])
        self.store.status(mid,'COMPLETED_WITH_LIMITATIONS','Reviewed report delivered: '+review['review']['reason'])
        return True

    def _record_response(self, mid, did, response, context):
        if response.get('usage'):
            self.store.settle(mid, did, response['usage'])
        try:
            if response.get('parse_error'):raise ContractError('MODEL_JSON_DELIVERY: '+response['parse_error'])
            doc = validate_decision(response['decision'], context['commission'], context['catalog'], context['available_inputs'])
            known = set(context['available_inputs']) | set(context['evidence']) | {s['step_id'] for s in context['steps']}
            if not set(doc['evidence_ids']) <= known:
                raise ContractError('Unknown decision evidence ID')
            self.store.finish_decision(did, 'ACCEPTED', doc)
        except (ContractError, ValueError, TypeError, KeyError) as exc:
            self.store.finish_decision(did, 'REJECTED', response.get('decision'), str(exc))
            last=self.store.decisions(mid)[-2:]
            can_adapt_output_limit=response.get('raw_response',{}).get('stopReason')=='length' and last[-1]['origin'].get('reasoning')!='off'
            if len(last)==2 and all(x['status']=='REJECTED' and x['error']==str(exc) for x in last) and not can_adapt_output_limit:
                self.store.control(mid,'pause','automatic_repeated_planner_rejection_guard')
                self.store.status(mid,'NEEDS_ATTENTION','Repeated identical planner rejection; requests held to conserve the commissioned budget: '+str(exc))
            else:
                self.store.status(mid, 'QUEUED', 'Planner output rejected; retained for correction: ' + str(exc))

    def recover_response(self, mid):
        """Only a persisted response to the original request can clear UNKNOWN."""
        decisions = self.store.decisions(mid)
        if decisions and decisions[-1]['status'] == 'REQUEST_RESERVED':
            d = decisions[-1]
            directory = self.store.workspace / 'decisions' / d['decision_id']
            if (directory / 'response.json').exists():
                context = read(directory / 'context.json')
                if canonical_hash(context) != d['context_sha256']:
                    raise ContractError('Original planner context changed')
                self._record_response(mid, d['decision_id'], read(directory / 'response.json'), context)

    def _apply(self, mission, decision, context):
        mid, doc = mission['mission_id'], decision['document']
        if doc['action'] == 'needs_input':
            self.store.status(mid, 'NEEDS_INPUT', doc['reason'])
        elif doc['action'] == 'finish':
            reports = self.store.snapshot(mid)['reports']
            if not reports:
                self._fallback_report(mid)
                reports = self.store.snapshot(mid)['reports']
            if not reports:
                self.store.status(mid, 'NEEDS_ATTENTION', 'No validated report available for task completion')
            else:
                if self.config.get('research_policy',{}).get('require_stop_review') and mission['commission']['evidence_kind']=='COMPUTATIONAL_RESEARCH':
                    report=reports[-1]['document'];review=report.get('research_review')
                    error=None
                    if not review or review.get('status')!='STRUCTURED_REVIEW_PASSED':error='RESEARCH_REVIEW: Generate a new report with research_review before finishing'
                    elif review['review']['stop_basis']=='continue_research':error='RESEARCH_REVIEW: Report identifies a pending informative action; execute it before finishing'
                    elif review['observations']['successful_structure_sha256']!=context['research_progress']['successful_structure_sha256']:error='RESEARCH_REVIEW: Report predates current predictions; update measurement and report'
                    elif doc['conclusion']!=report['proposed_conclusion']:error='RESEARCH_REVIEW: Finish conclusion differs from the reviewed report'
                    if error:
                        self._reject_action(mid,decision['decision_id'],error)
                        return self.store.snapshot(mid)
                self.store.status(mid, 'COMPLETED_WITH_LIMITATIONS', doc['reason'])
        else:
            try:
                spec = self.registry.prepare(mission, doc, context['available_inputs'], self.store.steps(mid),
                    remaining_compute_seconds=mission['commission']['budget']['compute_seconds']-self.store.usage(mid).get('compute_seconds',0))
                if doc['tool_id']=='grounded_report' and self.config.get('research_policy',{}).get('require_stop_review') and mission['commission']['evidence_kind']=='COMPUTATIONAL_RESEARCH':
                    bundle=read(next(x['path'] for x in spec['inputs'].values() if x['kind']=='fact_bundle'))
                    reviewed=validate_review(doc['parameters'].get('research_review'),bundle,context['research_progress'],context['catalog'],self.config['research_policy'])
                    spec['request']['research_review_result']=reviewed
            except ContractError as exc:
                if str(exc).startswith('BUDGET_EXHAUSTED:'):raise
                if str(exc).startswith('CAPABILITY_NOT_READY:'):
                    self.store.status(mid, 'NEEDS_INPUT', str(exc))
                else:
                    self._reject_action(mid,decision['decision_id'],str(exc))
                return self.store.snapshot(mid)
            self.store.create_step(mid, decision['decision_id'], spec)
            self.store.status(mid, 'QUEUED', doc['reason'])
        return self.store.snapshot(mid)

    def _reject_action(self,mid,did,error):
        self.store.reject_action(did,error)
        last=self.store.decisions(mid)[-2:]
        if len(last)==2 and all(x['status']=='REJECTED' and x['error']==error for x in last):
            self.store.control(mid,'pause','automatic_repeated_action_rejection_guard')
            self.store.status(mid,'NEEDS_ATTENTION','Repeated unchanged action rejection; no new model/tool work: '+error)
        else:self.store.status(mid,'QUEUED','Tool request rejected; planner must correct: '+error)
