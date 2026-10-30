# 开源及第三方资源使用清单

核对日期：2026-10-30。自研部分为 ModAb 的任务控制、适配、事实组织、工作台扩展和复现工具；没有训练或声称自研底层大模型。

| 资源 / 类型 | 实际版本与来源 | 用途与自研边界 | 许可 / 义务与分发方式 |
|---|---|---|---|
| DSH / Agent 宿主 | 0.2.1-rc.3，@deepseek-ai/dsh 本地安装包 | 原生对话、会话与扩展接口；ModAb 自研研究插件和控制器 | 本地 LICENSE 标明 MIT、Copyright 2026 DeepSeek；保留原版权与许可。此包不分发其运行环境，附许可供复核 |
| DSH 的 npm 依赖 | 见 dsh-dependency-manifest.json | 宿主传递依赖，不计入 ModAb 原创代码 | 逐项按清单与上游包许可；未重新打包依赖实现 |
| DeepSeek API / 模型服务 | 运行时模型标识见任务配置；当前对话 deepseek-flash；https://api-docs.deepseek.com/ | 规划下一步与研究解释；本地控制器执行权限、预算和事实约束 | 在线服务，适用 DeepSeek API 条款；不分发模型权重或账户凭据；模型输出仍需审核 |
| Boltz / 结构预测 | 2.2.1；https://github.com/jwohlwend/boltz | 原始 AT8 配对状态预测；ModAb 自研输入组织、监督执行和测量 | 上游代码与权重声明 MIT；保留版权和许可；本包不分发模型权重或软件环境 |
| BoltzGen / 候选生成 | 0.3.2；https://github.com/HannesStark/boltzgen | 4 个候选生成；ModAb 保存约束、尝试和原始坐标 | 上游代码 MIT；依赖与资产按对应条款获取。此包仅含本次计算产物，不打包软件或权重 |
| Gemmi / 结构解析 | 0.7.5；https://github.com/project-gemmi/gemmi | mmCIF 解析，ModAb 编写原子对应与测量逻辑 | MPL-2.0；未修改或再分发库；安装者从官方包获取。若未来改动或分发受覆盖文件须履行对应义务 |
| NumPy / 数值计算 | 2.4.0，离线重测；https://numpy.org/ | 矩阵与距离运算；测量判据由项目代码定义 | BSD-3-Clause 为主，二进制包还含其他许可组件；完整本机 LICENSE 随清单保留，不重分发二进制 |
| PyTorch / 科学模型运行 | 2.6.0+cu126；https://pytorch.org/ | Boltz 运行依赖 | BSD-style 及捆绑许可；不分发运行环境 |
| PyTorch Lightning | 2.5.0；https://github.com/Lightning-AI/pytorch-lightning | Boltz 运行依赖 | Apache-2.0；未修改、未分发 |
| Biopython | 1.84；https://biopython.org/ | 科学环境中的生物信息依赖 | Biopython License Agreement / BSD-style；未分发 |
| RDKit | 2026.9.1；https://www.rdkit.org/ | 化学结构处理依赖 | BSD-3-Clause；未修改、未分发 |
| Python / 运行时 | 本机 3.14；科学环境另见 science-environment-versions.json；https://www.python.org/ | 服务、监督执行、测量与复现脚本 | PSF 及捆绑组件许可；未随包分发解释器 |
| Node.js / 运行时 | 版本见交付验证记录；https://nodejs.org/ | DSH 和前端扩展加载 | MIT 及捆绑组件许可；未随包分发运行时 |
| SQLite / 本地数据 | Python 标准库 sqlite3；https://www.sqlite.org/ | 持久任务与审计记录 | SQLite 核心 public domain；原始账户和任务数据库未分发 |
| PDB 5E2W、5E2V / 实验结构 | https://www.rcsb.org/structure/5E2W 与 /5E2V | 参考坐标、Fv/Tau 选择、3 个 Asp1 侧链原子修补 | PDB 档案按 wwPDB 数据政策使用；引述条目和原始文献，说明加工。图表与预测产物由本次工作生成 |
| MSA 服务 / 外部服务 | Boltz server MSA 模式 | 为结构预测获取序列比对，提交输入序列 | 依服务可用性及条款；没有把服务声明为 ModAb 自有组件，不分发服务端数据库 |
| Codex / AI 开发辅助 | 本次任务所用 Codex 环境 | 协助代码、资料核对、报告、演示制作和检查 | 适用平台条款；不将 AI 生成内容当然视为全部自主权利。作者仍需最终审阅提交材料 |
| ReportLab、Pillow、FFmpeg / 材料制作 | 实际版本见材料生成记录 | 生成 PDF、整理截图和编码 MP4；不参与科研指标测量 | 各工具按其发行许可使用；交付只含输出材料，未分发工具二进制 |

## 当前开放方式

作者已于 2026-10-30 选择公开仓库及 MIT 许可证。自研代码和配套文档的许可见根目录 LICENSE，第三方代码、模型资产和在线服务继续适用原有条款。公开仓库：https://github.com/fan-2077/ModAb 。

拟公开的内容包括自研运行代码、适配契约、测试、坐标重测脚本、匿名案例与技术文档。凭据、用户账户数据库、未经许可的数据及第三方权重不在拟公开包中。
