"""Documented scientific method candidates for research planning.

This directory describes methods, not execution adapters. The runtime registry
and the commissioned tool allow-list remain the authority for running a job.
"""
from copy import deepcopy


TOOL_FAMILIES = [
    {
        'id': 'rfd4_proteina', 'name': 'RFD4-Proteina',
        'methods': ['RFD4-Proteina'], 'modab_status': 'integration_candidate',
        'summary_zh': '蛋白结合物生成、固定骨架逆折叠、功能基序支架、小分子结合蛋白设计；官方还提供酶与 DNA 结合物设计入口。',
        'report_function_zh': '蛋白结合物、逆折叠、基序支架、小分子结合蛋白；可扩展至酶与 DNA 结合物',
        'inputs_zh': '目标结构、带条件标注的 CIF、序列或骨架与固定区域',
        'outputs_zh': '候选序列与结构；按所选任务提供相应指标',
        'source_urls': ['https://github.com/RosettaCommons/RFD4-Proteina/blob/aaccdd2ad0720c180468df5f9cf037a713d403e5/skills/rfd4-proteina-generate/SKILL.md'],
    },
    {
        'id': 'boltzgen', 'name': 'BoltzGen', 'methods': ['BoltzGen'],
        'modab_status': 'used_in_retained_case', 'case_version': '0.3.2',
        'summary_zh': '结合物生成与已有候选约束改造；包含蛋白、抗体、肽和小分子等设计方向。',
        'report_function_zh': '结合物与抗体生成、约束改造、肽及环肽相关设计',
        'inputs_zh': '靶标结构、设计区域及固定条件', 'outputs_zh': '候选序列、结构与生成记录',
        'source_urls': ['https://github.com/HannesStark/boltzgen'],
    },
    {
        'id': 'rfantibody', 'name': 'RFantibody', 'methods': ['RFantibody'],
        'modab_status': 'integration_candidate',
        'summary_zh': '抗体与纳米抗体从头设计，串联骨架生成、序列设计和结构筛选。',
        'report_function_zh': '抗体与纳米抗体从头设计，骨架生成、序列设计和结构筛选',
        'inputs_zh': '抗原结构、热点、抗体框架及 CDR 条件',
        'outputs_zh': '候选抗体或纳米抗体序列与复合物结构',
        'source_urls': ['https://github.com/RosettaCommons/RFantibody'],
    },
    {
        'id': 'mpnn', 'name': 'ProteinMPNN / LigandMPNN',
        'methods': ['ProteinMPNN', 'LigandMPNN'], 'modab_status': 'integration_candidate',
        'summary_zh': '固定骨架序列设计、指定残基保留和局部重设计；LigandMPNN 可考虑周围配体的原子环境。',
        'report_function_zh': '固定骨架序列设计、局部重设计与配体环境感知的序列优化',
        'inputs_zh': '蛋白骨架、固定残基和可选配体环境',
        'outputs_zh': '设计序列、条件概率及可选侧链结构',
        'source_urls': ['https://github.com/dauparas/ProteinMPNN', 'https://github.com/dauparas/LigandMPNN'],
    },
    {
        'id': 'boltz2', 'name': 'Boltz-2', 'methods': ['Boltz-2'],
        'modab_status': 'used_in_retained_case', 'case_version': '2.2.1',
        'summary_zh': '复合物结构预测、修饰残基输入和匹配状态结构比较；本项目不将小分子亲和力头用于抗体亲和力。',
        'report_function_zh': '复合物结构预测、修饰残基输入与匹配状态结构比较',
        'inputs_zh': '分子序列、修饰、MSA 及可选模板条件',
        'outputs_zh': '预测复合物坐标与结构置信信息',
        'source_urls': ['https://github.com/jwohlwend/boltz/blob/v2.2.1/docs/prediction.md'],
    },
    {
        'id': 'siphab', 'name': 'SIpHAB', 'methods': ['SIpHAB'],
        'modab_status': 'integration_candidate',
        'summary_zh': '基于抗体序列提出组氨酸突变候选，面向偏好酸性或生理 pH 的结合设计。',
        'report_function_zh': '基于抗体序列提出 pH 敏感的组氨酸突变候选',
        'inputs_zh': '抗体可变区序列、CDR 位置和期望 pH 方向',
        'outputs_zh': '组氨酸替换候选及方法评分',
        'source_urls': ['https://pmc.ncbi.nlm.nih.gov/articles/PMC11409498/'],
    },
    {
        'id': 'proton_pottsmpnn', 'name': 'Proton-PottsMPNN',
        'methods': ['Proton-PottsMPNN'], 'modab_status': 'integration_candidate',
        'summary_zh': '显式表示质子化状态，围绕指定中心对结合蛋白进行 pH 响应序列重设计。',
        'report_function_zh': '考虑质子化状态的 pH 响应结合蛋白序列重设计',
        'inputs_zh': '固定骨架、质子化中心和邻域重设计条件',
        'outputs_zh': '候选序列、质子化状态与模型评分',
        'source_urls': ['https://github.com/christian-creator/ProtonPottsMPNN'],
    },
]

WORKFLOWS = [
    {'id': 'W01', 'name_zh': '通用蛋白结合物设计', 'candidate_families': ['boltzgen', 'rfd4_proteina']},
    {'id': 'W02', 'name_zh': '抗体与纳米抗体设计', 'candidate_families': ['boltzgen', 'rfantibody']},
    {'id': 'W03', 'name_zh': '已有候选约束改造', 'candidate_families': ['boltzgen', 'mpnn', 'rfd4_proteina']},
    {'id': 'W04', 'name_zh': 'pH 依赖性设计', 'candidate_families': ['siphab', 'proton_pottsmpnn']},
    {'id': 'W05', 'name_zh': '磷酸化选择性研究', 'candidate_families': ['boltzgen', 'boltz2'], 'scope_zh': '修饰条件与多状态结构评价组合；结构指标不直接给出选择性结论'},
    {'id': 'W06', 'name_zh': '肽与环肽相关设计', 'candidate_families': ['boltzgen']},
    {'id': 'W07', 'name_zh': '固定骨架序列设计', 'candidate_families': ['mpnn', 'rfd4_proteina']},
    {'id': 'W08', 'name_zh': '功能片段与基序支架设计', 'candidate_families': ['rfd4_proteina']},
    {'id': 'W09', 'name_zh': '小分子结合蛋白设计', 'candidate_families': ['boltzgen', 'rfd4_proteina', 'mpnn']},
    {'id': 'W10', 'name_zh': '已有候选评价与比较', 'candidate_families': ['boltz2']},
]


def scientific_tool_catalog():
    """Return planning metadata without modifying runtime tool availability."""
    return {
        'schema': 'modab_scientific_tool_candidates_v1',
        'family_count': len(TOOL_FAMILIES),
        'method_count': sum(len(item['methods']) for item in TOOL_FAMILIES),
        'tools': deepcopy(TOOL_FAMILIES), 'workflows': deepcopy(WORKFLOWS),
        'status_labels_zh': {'used_in_retained_case': '已接入案例', 'integration_candidate': '扩展候选'},
        'execution_policy': 'Planning directory only. Inclusion does not install a method or create an execution adapter. Use the runtime catalog, configured profiles and commission allow-list for execution. Do not describe integration_candidate entries as runnable ModAb tools.',
    }
