import copy
import unittest
import numpy as np
from dualctx.contracts import ContractError
from modab.autonomy.pose_diagnostics import pose_components, prediction_settings
from modab.autonomy.adapters import validate_boltz_input


class PoseDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.shape=np.array([[0.,0.,0.],[1.,2.,0.],[2.,0.,3.],[4.,1.,2.]])

    def test_translation_is_retained_in_placement_but_not_shape(self):
        r=pose_components(self.shape+[4.,0.,0.],self.shape)
        self.assertAlmostEqual(r['Tau_CA_RMSD_after_Fv_fit_A'],4)
        self.assertAlmostEqual(r['Tau_centroid_shift_after_Fv_fit_A'],4)
        self.assertAlmostEqual(r['Tau_shape_RMSD_after_independent_fit_A'],0)
        self.assertAlmostEqual(r['orientation_excess_mean_square_A2'],0)

    def test_rotation_is_not_confused_with_shape_change(self):
        center=self.shape.mean(0)
        rotation=np.array([[0.,-1.,0.],[1.,0.,0.],[0.,0.,1.]])
        moved=(self.shape-center)@rotation+center
        r=pose_components(moved,self.shape)
        self.assertAlmostEqual(r['Tau_shape_RMSD_after_independent_fit_A'],0)
        self.assertAlmostEqual(r['Tau_centroid_shift_after_Fv_fit_A'],0)
        self.assertAlmostEqual(r['Tau_best_fit_rotation_angle_deg'],90)
        self.assertGreater(r['orientation_excess_mean_square_A2'],0)

    def test_actual_shape_change_survives_independent_fit(self):
        moved=self.shape.copy();moved[0,2]+=3
        self.assertGreater(pose_components(moved,self.shape)['Tau_shape_RMSD_after_independent_fit_A'],0.1)

    def test_template_cannot_force_coordinates_or_escape_job_directory(self):
        valid={'version':1,'sequences':[{'protein':{'id':'A','sequence':'AST','msa':'empty'}}],
               'templates':[{'cif':'pose_template.cif','chain_id':['A','B','C'],'template_id':['A','B','C'],'force':False}]}
        validate_boltz_input(valid,'single_sequence')
        for key,value in [('cif','../foreign.cif'),('force',True),('template_id',['X','Y','Z'])]:
            bad=copy.deepcopy(valid);bad['templates'][0][key]=value
            with self.assertRaises(ContractError):validate_boltz_input(bad,'single_sequence')

    def test_timeout_difference_does_not_break_seed_matching(self):
        result,_=prediction_settings({'parameters':{'seed':42,'timeout_seconds':100}},None)
        self.assertEqual(result,{'seed':42})


if __name__=='__main__':unittest.main()
