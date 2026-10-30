import copy,json,tempfile,unittest
from pathlib import Path
from modab.autonomy.science_actions import prepare,validate_preparation,geometry,verify_prediction_identity,atoms
from modab.autonomy.contracts import atomic_json,digest
from dualctx.contracts import ContractError

class ScienceActionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.dataset={'antibodies':{'ref':{'A':'AST','B':'TTY'}},'tau':{'sequence':'SGYSSPGSPGTPGSRSRTPS','start':195,'end':214},'source_files':{},'candidate_ids':['c1','c2','c3','c4']}
        self.params={'timeout_seconds':30,'subjects':['ref'],'tau_start':195,'tau_end':214,'states':[{'name':'three_p','phosphorylated_positions':[202,205,208]},{'name':'unmodified','phosphorylated_positions':[]}],'msa_mode':'single_sequence','antibody_form':'Fv'}
    def test_state_preparation_changes_only_declared_modifications(self):
        p=self.root/'dataset.json';atomic_json(p,self.dataset)
        result=prepare({'dataset':{'path':str(p),'sha256':digest(p),'kind':'at8_sequence_dataset'}},self.params,self.root/'result.json')
        a,b=[json.loads(Path(x['path']).read_bytes()) for x in result['records']]
        self.assertEqual(a['sequences'][2]['protein']['modifications'],[{'position':8,'ccd':'SEP'},{'position':11,'ccd':'TPO'},{'position':14,'ccd':'SEP'}])
        a['sequences'][2]['protein']['modifications']=[];self.assertEqual(a,b)
        self.assertEqual(result['candidate_denominator'],['c1','c2','c3','c4'])
    def test_wrong_parent_or_out_of_range_is_rejected(self):
        for bad in (194,203,215):
            p=copy.deepcopy(self.params);p['states'][0]['phosphorylated_positions']=[bad]
            with self.assertRaises(ContractError):validate_preparation(p,self.dataset)
    def test_source_change_is_not_silently_used(self):
        src=self.root/'source';src.write_text('first');self.dataset['source_files']['s']={'path':str(src),'sha256':digest(src)}
        p=self.root/'dataset';atomic_json(p,self.dataset);src.write_text('second')
        with self.assertRaisesRegex(ContractError,'source changed'):prepare({'x':{'path':str(p),'kind':'at8_sequence_dataset'}},self.params,self.root/'result')
    def test_peptide_placement_error_is_not_fitted_away(self):
        import gemmi,math
        def write(name,move_tau):
            d=gemmi.cif.Document();b=d.add_new_block('fixture');l=b.init_loop('_atom_site.',['label_asym_id','label_seq_id','label_comp_id','label_atom_id','type_symbol','Cartn_x','Cartn_y','Cartn_z','pdbx_PDB_model_num','label_alt_id'])
            for chain,n in [('A',112),('B',113)]:
                for i in range(1,n+1):l.add_row([chain,str(i),'ALA','CA','C',str(i),str(math.sin(i)+(10 if chain=='B' else 0)),str(math.cos(i)),'1','.'])
            for i in range(8):l.add_row(['C',str(i+9 if name=='ref' else i+8),'SER','CA','C',str(i+move_tau),'0','4','1','.'])
            p=self.root/(name+'.cif');d.write_file(str(p));return p
        reference=write('ref',0);prediction=write('pred',10)
        values=geometry(prediction,reference,{'tau_start':195,'tau_end':214},4.0,2.0)
        self.assertAlmostEqual(values['Fv_CA_fit_RMSD_A'],0,places=6)
        self.assertAlmostEqual(values['Tau_CA_RMSD_after_Fv_fit_A'],10,places=6)
        self.assertEqual(values['Fv_fit_CA_count'],225);self.assertEqual(values['shared_Tau_CA_count'],8)
    def test_prediction_must_preserve_modified_residue_and_phosphorus(self):
        import gemmi
        d=gemmi.cif.Document();b=d.add_new_block('identity');l=b.init_loop('_atom_site.',['label_asym_id','label_seq_id','label_comp_id','label_atom_id','type_symbol','Cartn_x','Cartn_y','Cartn_z','pdbx_PDB_model_num','label_alt_id'])
        for number,name in [(1,'ALA'),(2,'SEP'),(3,'THR')]:l.add_row(['A',str(number),name,'CA','C',str(number),'0','0','1','.'])
        p=self.root/'identity.cif';d.write_file(str(p));model={'sequences':[{'protein':{'id':'A','sequence':'AST','modifications':[{'position':2,'ccd':'SEP'}]}}]}
        with self.assertRaisesRegex(ContractError,'phosphorylation identity'):verify_prediction_identity(p,model)
        l.add_row(['A','2','SEP','P','P','2','2','0','1','.']);d.write_file(str(p));verify_prediction_identity(p,model)

    def test_reference_selection_retains_unique_labels_but_rejects_ambiguous_selected_atoms(self):
        import gemmi
        d=gemmi.cif.Document();b=d.add_new_block('reference');l=b.init_loop('_atom_site.',['label_asym_id','label_seq_id','label_comp_id','label_atom_id','type_symbol','Cartn_x','Cartn_y','Cartn_z','pdbx_PDB_model_num','label_alt_id'])
        for number,alt in [(119,'A'),(119,'B'),(42,'A')]:l.add_row(['B',str(number),'PRO','CA','C','1','2','3','1',alt])
        p=self.root/'altloc.cif';d.write_file(str(p));provenance=[]
        selected=atoms(p,reference_ca=True,provenance=provenance)
        self.assertEqual(set(selected),{('B',42,'CA')});self.assertEqual(provenance[0]['altloc'],'A')
        with self.assertRaises(ContractError):atoms(p)
        l.add_row(['B','42','PRO','CA','C','4','5','6','1','B']);d.write_file(str(p))
        with self.assertRaisesRegex(ContractError,'Ambiguous atom'):atoms(p,reference_ca=True)

if __name__=='__main__':unittest.main()
