"""Explicit additive migration on ProjectStore; durable mission and budget state."""
from __future__ import annotations

from contextlib import closing, contextmanager
import json
from pathlib import Path
import sqlite3

from dualctx.contracts import ContractError, canonical_hash
from modab.project_store import ProjectStore, UNVERIFIED_EXTERNAL
from .contracts import BUDGET_FIELDS, TERMINAL, atomic_json, identifier, now, validate_mission, verify_inputs


def packed(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


class ResearchStore:
    def __init__(self, workspace, *, migrate=False):
        self.workspace = Path(workspace).resolve()
        if migrate:
            with ProjectStore(self.workspace):
                pass
        self.db = sqlite3.connect((self.workspace / 'modab.sqlite3').as_uri() + '?mode=rw',
            uri=True, timeout=15, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        version = self.db.execute("SELECT value FROM metadata WHERE key='autonomy_schema_version'").fetchone()
        if not version and migrate:
            backup = self.workspace / 'before-autonomy-v1.sqlite3'
            if backup.exists():
                raise ContractError('Migration backup already exists; inspect previous migration')
            with closing(sqlite3.connect(backup)) as dest:
                self.db.backup(dest)
            self._migrate()
        elif not version or version[0] != '1':
            self.db.close()
            raise ContractError('Autonomy schema requires explicit migration to version 1')

    def _migrate(self):
        self.db.executescript('''
        BEGIN IMMEDIATE;
        CREATE TABLE ar_missions (
            mission_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(project_id),
            commission TEXT NOT NULL, commission_sha256 TEXT NOT NULL, approval TEXT,
            desired TEXT NOT NULL, status TEXT NOT NULL, reason TEXT, created_utc TEXT NOT NULL,
            updated_utc TEXT NOT NULL);
        CREATE TABLE ar_decisions (
            decision_id TEXT PRIMARY KEY, mission_id TEXT NOT NULL REFERENCES ar_missions(mission_id),
            sequence INTEGER NOT NULL, status TEXT NOT NULL, document TEXT, origin TEXT NOT NULL,
            context_sha256 TEXT NOT NULL, error TEXT, UNIQUE(mission_id,sequence));
        CREATE TABLE ar_steps (
            step_id TEXT PRIMARY KEY, mission_id TEXT NOT NULL REFERENCES ar_missions(mission_id),
            decision_id TEXT NOT NULL UNIQUE REFERENCES ar_decisions(decision_id),
            tool_id TEXT NOT NULL, resource_id TEXT NOT NULL, spec TEXT NOT NULL,
            status TEXT NOT NULL, job_id TEXT NOT NULL UNIQUE, receipt TEXT, error TEXT);
        CREATE TABLE ar_budget (
            charge_id TEXT PRIMARY KEY, mission_id TEXT NOT NULL REFERENCES ar_missions(mission_id),
            kind TEXT NOT NULL, reserved INTEGER NOT NULL, actual INTEGER, state TEXT NOT NULL,
            source TEXT NOT NULL, UNIQUE(mission_id,source,kind));
        CREATE TABLE ar_reports (
            report_id TEXT PRIMARY KEY, mission_id TEXT NOT NULL REFERENCES ar_missions(mission_id),
            document TEXT NOT NULL, created_utc TEXT NOT NULL);
        INSERT INTO metadata VALUES ('autonomy_schema_version','1');
        COMMIT;
        ''')

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def _event(self, mission, event, document):
        self.db.execute('INSERT INTO events(project_id,event_type,document,recorded_utc) VALUES (?,?,?,?)',
            (mission['project_id'], event, packed({'mission_id': mission['mission_id'], **document}), now()))

    def create(self, commission, origin):
        validate_mission(commission)
        verify_inputs(commission)
        identity = canonical_hash(commission)
        mid = 'research_' + identity
        with self.transaction():
            row = self.db.execute('SELECT mission_id FROM ar_missions WHERE mission_id=?', (mid,)).fetchone()
            if row:
                return self.mission(mid)
            self.db.execute('INSERT OR IGNORE INTO projects(project_id,evidence_kind) VALUES (?,?)',
                (commission['project_id'], UNVERIFIED_EXTERNAL))
            self.db.execute('INSERT INTO ar_missions VALUES (?,?,?,?,?,?,?,?,?,?)',
                (mid, commission['project_id'], packed(commission), identity, None, 'PAUSE', 'DRAFT',
                 'Awaiting task commission', now(), now()))
            self._event(self.mission(mid), 'RESEARCH_MISSION_DRAFTED', {'origin': origin, 'commission_sha256': identity})
        return self.mission(mid)

    def mission(self, mid):
        identifier(mid)
        row = self.db.execute('SELECT * FROM ar_missions WHERE mission_id=?', (mid,)).fetchone()
        if not row:
            raise ContractError('Unknown research mission')
        value = dict(row)
        value['commission'] = json.loads(value['commission'])
        value['approval'] = json.loads(value['approval']) if value['approval'] else None
        if canonical_hash(value['commission']) != value['commission_sha256'] or mid != 'research_' + value['commission_sha256']:
            raise ContractError('Commission identity mismatch')
        return value

    def approve(self, mid, expected_sha256, actor, source_ref):
        if not actor or not source_ref:
            raise ContractError('Trusted approval attribution required')
        with self.transaction():
            mission = self.mission(mid)
            if mission['commission_sha256'] != expected_sha256:
                raise ContractError('Commission changed before approval')
            verify_inputs(mission['commission'])
            if mission['approval']:
                return mission
            if mission['status'] != 'DRAFT':
                raise ContractError('Only a draft can receive initial authority')
            approval = {'actor': actor, 'source_ref': source_ref, 'commission_sha256': expected_sha256, 'approved_utc': now()}
            self.db.execute("UPDATE ar_missions SET approval=?,desired='RUN',status='QUEUED',reason=NULL,updated_utc=? WHERE mission_id=?",
                (packed(approval), now(), mid))
            self._event(mission, 'RESEARCH_MISSION_COMMISSIONED', approval)
        return self.mission(mid)

    def control(self, mid, action, origin):
        if action not in {'pause', 'resume', 'cancel'}:
            raise ContractError('Unknown control action')
        with self.transaction():
            mission = self.mission(mid)
            if not mission['approval']:
                raise ContractError('Task is not commissioned')
            if mission['status'] in TERMINAL:
                return mission
            if action == 'resume' and mission['desired'] == 'CANCEL':
                raise ContractError('Cancellation cannot be undone')
            desired = {'pause': 'PAUSE', 'resume': 'RUN', 'cancel': 'CANCEL'}[action]
            status = {'pause': 'PAUSED', 'resume': 'QUEUED', 'cancel': 'CANCELLING'}[action]
            self.db.execute('UPDATE ar_missions SET desired=?,status=?,reason=?,updated_utc=? WHERE mission_id=?',
                (desired, status, 'User requested ' + action, now(), mid))
            self._event(mission, 'RESEARCH_CONTROL_CHANGED', {'action': action, 'origin': origin})
        return self.mission(mid)

    def status(self, mid, status, reason=None):
        with self.transaction():
            m = self.mission(mid)
            if (m['status'], m['reason']) != (status, reason):
                self.db.execute('UPDATE ar_missions SET status=?,reason=?,updated_utc=? WHERE mission_id=?', (status, reason, now(), mid))
                self._event(m, 'RESEARCH_STATUS', {'status': status, 'reason': reason})

    def revisions(self,mid):
        result=[]
        for row in self.db.execute("SELECT document FROM events WHERE event_type='RESEARCH_REVISION_REQUESTED' ORDER BY rowid"):
            value=json.loads(row[0])
            if value['mission_id']==mid:result.append(value)
        return result

    def request_revision(self,mid,origin,reason,expected_usage):
        """Explicitly requested review, preserving the commission and every charge."""
        if not origin or not reason:raise ContractError('Attributed revision request required')
        with self.transaction():
            m=self.mission(mid)
            if m['status'] not in {'COMPLETED','COMPLETED_WITH_LIMITATIONS'} or m['desired']=='CANCEL':
                raise ContractError('Only a completed non-cancelled task can enter requested review')
            if self.usage(mid)!=expected_usage:raise ContractError('Usage changed before revision')
            verify_inputs(m['commission'])
            sequence=len(self.decisions(mid))
            self._event(m,'RESEARCH_REVISION_REQUESTED',{'origin':origin,'reason':reason,
                'previous_status':m['status'],'previous_reason':m['reason'],'after_decision_sequence':sequence,
                'usage_at_revision':expected_usage,'commission_sha256':m['commission_sha256']})
            self.db.execute("UPDATE ar_missions SET desired='RUN',status='QUEUED',reason=?,updated_utc=? WHERE mission_id=?",(reason,now(),mid))
        return self.mission(mid)

    def missions(self):
        return [self.mission(row[0]) for row in self.db.execute('SELECT mission_id FROM ar_missions ORDER BY created_utc')]

    def usage(self, mid):
        rows = self.db.execute('SELECT kind,SUM(COALESCE(actual,reserved)) total FROM ar_budget WHERE mission_id=? GROUP BY kind', (mid,))
        return {row['kind']: row['total'] for row in rows}

    def _reserve(self, mission, source, amounts):
        if not mission['approval'] or mission['desired'] != 'RUN' or mission['status'] in TERMINAL:
            raise ContractError('Mission is not permitted to start new work')
        used = self.usage(mission['mission_id'])
        for kind, amount in amounts.items():
            if kind not in BUDGET_FIELDS - {'max_concurrent_jobs'} or type(amount) is not int or amount < 0:
                raise ContractError('Invalid budget reservation')
            old = self.db.execute('SELECT reserved FROM ar_budget WHERE mission_id=? AND source=? AND kind=?',
                (mission['mission_id'], source, kind)).fetchone()
            if old:
                if old[0] != amount:
                    raise ContractError('Reservation identity changed')
                continue
            limit = mission['commission']['budget'][kind]
            if limit is not None and used.get(kind, 0) + amount > limit:
                raise ContractError('BUDGET_EXHAUSTED:' + kind)
            self.db.execute('INSERT INTO ar_budget VALUES (?,?,?,?,?,?,?)',
                ('charge_' + canonical_hash([mission['mission_id'], source, kind]), mission['mission_id'], kind, amount,
                 None, 'RESERVED', source))

    def settle(self, mid, source, actual):
        with self.transaction():
            mission = self.mission(mid)
            for kind, amount in actual.items():
                if type(amount) is not int or amount < 0:
                    raise ContractError('Invalid accounted amount')
                old = self.db.execute('SELECT * FROM ar_budget WHERE mission_id=? AND source=? AND kind=?', (mid, source, kind)).fetchone()
                if not old:
                    raise ContractError('No preceding budget reservation')
                if old['actual'] is not None and old['actual'] != amount:
                    raise ContractError('Accounting receipt changed')
                if old['actual'] == amount:
                    continue
                self.db.execute("UPDATE ar_budget SET actual=?,state='SETTLED' WHERE charge_id=?", (amount, old['charge_id']))
                self._event(mission, 'RESEARCH_RESOURCE_ACCOUNTED', {'source': source, 'kind': kind, 'actual': amount})
                if amount > old['reserved']:
                    self._event(mission, 'RESOURCE_RESERVATION_OVERRUN', {'kind': kind, 'source': source, 'reserved': old['reserved'], 'actual': amount})

    def begin_decision(self, mid, context, origin, costs):
        with self.transaction():
            mission = self.mission(mid)
            seq = self.db.execute('SELECT COUNT(*) FROM ar_decisions WHERE mission_id=?', (mid,)).fetchone()[0] + 1
            did = 'decision_' + canonical_hash([mid, seq, context])
            self._reserve(mission, did, costs)
            self.db.execute('INSERT INTO ar_decisions VALUES (?,?,?,?,?,?,?,?)',
                (did, mid, seq, 'REQUEST_RESERVED', None, packed(origin), canonical_hash(context), None))
            self._event(mission, 'RESEARCH_DECISION_RESERVED', {'decision_id': did, 'origin': origin})
        return did

    def finish_decision(self, did, status, document=None, error=None):
        with self.transaction():
            row = self.db.execute('SELECT * FROM ar_decisions WHERE decision_id=?', (did,)).fetchone()
            if not row or row['status'] != 'REQUEST_RESERVED':
                raise ContractError('Decision is not awaiting response')
            self.db.execute('UPDATE ar_decisions SET status=?,document=?,error=? WHERE decision_id=?',
                (status, packed(document) if document is not None else None, error, did))
            self._event(self.mission(row['mission_id']), 'RESEARCH_DECISION_RECORDED', {'decision_id': did, 'status': status, 'error': error})

    def decisions(self, mid):
        result = []
        for row in self.db.execute('SELECT * FROM ar_decisions WHERE mission_id=? ORDER BY sequence', (mid,)):
            value = dict(row)
            for key in ('document', 'origin'):
                value[key] = json.loads(value[key]) if value[key] else None
            result.append(value)
        return result

    def reject_action(self, did, error):
        with self.transaction():
            row = self.db.execute('SELECT * FROM ar_decisions WHERE decision_id=?', (did,)).fetchone()
            step = self.db.execute('SELECT 1 FROM ar_steps WHERE decision_id=?', (did,)).fetchone()
            if not row or row['status'] != 'ACCEPTED' or step:
                raise ContractError('Cannot revise an executed decision')
            self.db.execute("UPDATE ar_decisions SET status='REJECTED',error=? WHERE decision_id=?", (error, did))
            self._event(self.mission(row['mission_id']), 'RESEARCH_ACTION_REJECTED', {'decision_id':did,'error':error})

    def create_step(self, mid, did, spec):
        with self.transaction():
            mission = self.mission(mid)
            row = self.db.execute('SELECT * FROM ar_decisions WHERE decision_id=? AND mission_id=?', (did, mid)).fetchone()
            if not row or row['status'] != 'ACCEPTED':
                raise ContractError('Accepted planner decision required')
            existing = self.db.execute('SELECT step_id FROM ar_steps WHERE decision_id=?', (did,)).fetchone()
            if existing:
                return self.step(existing[0])
            if spec['tool_id'] not in mission['commission']['allowed_tools'] or spec['resource_id'] not in mission['commission']['allowed_resources']:
                raise ContractError('Tool or resource outside commission')
            active = self.db.execute('SELECT COUNT(*) FROM ar_steps WHERE mission_id=? AND receipt IS NULL', (mid,)).fetchone()[0]
            if active >= mission['commission']['budget']['max_concurrent_jobs']:
                raise ContractError('Mission concurrent job limit reached')
            verify_inputs(mission['commission'])
            sid = 'step_' + canonical_hash([did, spec])
            jid = 'job_' + canonical_hash([sid, mission['commission_sha256']])
            self._reserve(mission, sid, spec['cost_reservation'])
            self.db.execute('INSERT INTO ar_steps VALUES (?,?,?,?,?,?,?,?,?,?)',
                (sid, mid, did, spec['tool_id'], spec['resource_id'], packed(spec), 'SUBMISSION_PENDING', jid, None, None))
            self._event(mission, 'RESEARCH_STEP_CLAIMED', {'step_id': sid, 'job_id': jid, 'spec_sha256': canonical_hash(spec)})
        return self.step(sid)

    def step(self, sid):
        row = self.db.execute('SELECT * FROM ar_steps WHERE step_id=?', (sid,)).fetchone()
        if not row:
            raise ContractError('Unknown step')
        value = dict(row)
        value['spec'] = json.loads(value['spec'])
        value['receipt'] = json.loads(value['receipt']) if value['receipt'] else None
        return value

    def steps(self, mid):
        return [self.step(row[0]) for row in self.db.execute('SELECT step_id FROM ar_steps WHERE mission_id=? ORDER BY rowid', (mid,))]

    def update_step(self, sid, status, receipt=None, error=None):
        with self.transaction():
            step = self.step(sid)
            if step['receipt'] is not None:
                if receipt is not None and receipt != step['receipt']:
                    raise ContractError('Cannot replace a recorded result')
                return
            if step['status'] == status and step['error'] == error and receipt is None:
                return
            self.db.execute('UPDATE ar_steps SET status=?,receipt=?,error=? WHERE step_id=?',
                (status, packed(receipt) if receipt is not None else None, error, sid))
            self._event(self.mission(step['mission_id']), 'RESEARCH_STEP_STATUS', {'step_id': sid, 'job_id': step['job_id'], 'status': status, 'error': error})

    def report(self, mid, document):
        rid = 'report_' + canonical_hash([mid, document])
        with self.transaction():
            added = self.db.execute('INSERT OR IGNORE INTO ar_reports VALUES (?,?,?,?)', (rid, mid, packed(document), now())).rowcount
            if added:
                self._event(self.mission(mid), 'RESEARCH_REPORT_STORED', {'report_id': rid, 'validation_status': document['validation_status']})
        return rid

    def snapshot(self, mid):
        mission = self.mission(mid)
        mission['usage'] = self.usage(mid)
        mission['steps'] = self.steps(mid)
        mission['decisions'] = self.decisions(mid)
        mission['reports'] = [dict(row) | {'document': json.loads(row['document'])} for row in
            self.db.execute('SELECT * FROM ar_reports WHERE mission_id=? ORDER BY created_utc', (mid,))]
        return mission
