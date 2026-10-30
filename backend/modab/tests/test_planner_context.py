import tempfile,unittest
from pathlib import Path
from modab.autonomy.contracts import atomic_json,digest
from modab.autonomy.planner_context import compact_evidence


class PlannerProjectionTests(unittest.TestCase):
    def test_projection_preserves_all_values_and_points_to_strict_superset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);evidence={}
            for name,shas in [('old',['a']),('new',['a','b'])]:
                p=root/(name+'.json');atomic_json(p,{'records':[{'structure_sha256':s}for s in shas]})
                evidence[name]={'schema':'modab_fact_bundle_v1','bundle_sha256':name,'candidate_ids':['c1'],
                                'sources':{'measurements':{'path':str(p),'sha256':digest(p)}},
                                'facts':[{'id':'f','subject':'sample','metric':'distance','value':1.2345,'unit':'angstrom','scope':'geometry','source_id':'measurements','json_pointer':'/x','derivation':None}]}
            out=compact_evidence(evidence)
            self.assertEqual(out['old']['superseded_by'],'new')
            self.assertEqual(out['new']['facts'][0]['value'],1.2345)
            self.assertEqual(evidence['new']['schema'],'modab_fact_bundle_v1')

    def test_no_coverage_means_no_suppression(self):
        bundle={'schema':'modab_fact_bundle_v1','facts':[],'sources':{},'candidate_ids':[]}
        out=compact_evidence({'a':bundle,'b':bundle})
        self.assertTrue(all('superseded_by'not in x for x in out.values()))


if __name__=='__main__':unittest.main()
