import unittest
from modab.autonomy.evidence_summary import evidence_summary

class EvidenceSummaryTests(unittest.TestCase):
    def mission(self, end=214, rows=1):
        return {'steps':[{'tool':'prepare_at8_states','parameters':{'antibody_form':'Fv','tau_start':195,'tau_end':end,'subjects':['original_AT8'],'states':[]}}],
                'comparisons':[{'subject':f'original_AT8/state/{n}'} for n in range(rows)]}
    def test_inclusive_construct_length(self):
        self.assertEqual(evidence_summary(self.mission())['constructs'][0]['tau_residue_count'],20)
        self.assertEqual(evidence_summary(self.mission(209))['constructs'][0]['tau_residue_count'],15)
    def test_single_sample_does_not_estimate_variation(self):
        self.assertIs(evidence_summary(self.mission())['within_state_variation_estimated'],False)
    def test_multiple_samples_do_not_automatically_validate_variation(self):
        self.assertIsNone(evidence_summary(self.mission(rows=2))['within_state_variation_estimated'])
    def test_no_measurement_is_not_zero_variation(self):
        self.assertIsNone(evidence_summary(self.mission(rows=0))['within_state_variation_estimated'])
    def test_distinct_metrics_keep_distinct_rankings(self):
        m=self.mission(rows=0)
        m['comparisons']=[{'subject':state,'metrics':{'iptm':a,'complex_plddt':b}} for state,a,b in
            [('AT8/0P/0',.990,.975),('AT8/2P/0',.985,.903),('AT8/3P/0',.963,.924)]]
        ranks=evidence_summary(m)['metric_values_descending']
        self.assertEqual([x['subject'] for x in ranks['iptm']],['AT8/0P/0','AT8/2P/0','AT8/3P/0'])
        self.assertEqual([x['subject'] for x in ranks['complex_plddt']],['AT8/0P/0','AT8/3P/0','AT8/2P/0'])
