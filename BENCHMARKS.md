# LoopSmith 本地评测与 HKU API

业务目标：面向陌生代码仓库的开发辅助 Agent，支持源码问答、问题定位和小规模代码修复，并提供可验证的执行结果。

本次范围是评测前置准备和 HKU 模型兼容层。没有实现 RAG，没有迁移 AgentLoop，也没有进行真实模型评测。数据下载和本地测试不消耗模型 token。

## 已准备的数据

- QuixBugs Python：10 道开发题，原始仓库和提交版本保存在 `.eval-data/quixbugs` 与冻结清单中。
- SWE-bench Verified：完整 500 条公开元数据快照，固定种子 `20261018` 按仓库轮转选择 30 个候选任务；仅下载元数据，不代表仓库镜像和测试环境已经就绪。
- 当前清单：`.eval-data/frozen-v1/manifest.json`，内容校验：`manifest.sha256`。
- Agent 可见任务：`.eval-data/frozen-v1/agent/`；SWE 任务仅包含题目、仓库和基准提交，不包含参考补丁、测试补丁或答案。
- 可版本管理的任务索引：`benchmarks/selection.json`。

QuixBugs 只用作开发集；30 道 SWE 题保持为评估集，不根据 Agent 成败换题。环境排除项必须提前记录，不得在正式模型运行后删除失败题。当前划分不是跨仓库训练/测试划分，也不能作为公开榜单成绩。

初始 Quix 候选包含 `levenshtein`，但上游测试自带固定 skip；在模型评测开始前将其替换为 `next_permutation`。初始预检保留在 `.eval-data/reports/quix-preflight-02`，不能把沙箱超时当作算法失败结论。最终预检单独执行。

## 本地命令（项目根目录，PowerShell）

```powershell
# 检查数据与评测测试没有被修改
.\.venv\Scripts\python.exe scripts/evaluate.py verify

# 检查 SWE-bench 与 Docker 就绪情况；不拉取镜像
.\.venv\Scripts\python.exe scripts/evaluate.py doctor

# 原始缺陷应失败，官方参考修复应通过；目录不可重复使用
.\.venv\Scripts\python.exe scripts/evaluate.py preflight-quix --reports .eval-data/reports/quix-check-new

# 验收某个候选源码；验收使用外部官方测试，不接受候选自行修改测试
.\.venv\Scripts\python.exe scripts/evaluate.py grade-quix --program gcd --candidate <candidate.py> --reports .eval-data/reports/gcd-new
```

Quix 评分以 pytest 退出状态和 JUnit 用例结果共同判断：零用例、跳过、收集失败、超时均不算通过。保留测试日志、JUnit、源码 SHA256、耗时和状态。该分数是“通过固定测试”，不是形式化正确性证明。Quix 本地评分是进程隔离，不是恶意代码安全沙箱；不能据此宣称宿主文件不可访问。未来模型运行应在仅挂载任务目录的受限容器中执行，不要挂载整个项目、数据目录或宿主密钥。

## SWE-bench 的准备和验收

正式验收使用官方 `swebench.harness.run_evaluation`，不自行用模型评价补丁是否正确。

```powershell
# 默认仅生成独立验收输入与调用计划，不执行容器、不调用模型
.\.venv\Scripts\python.exe scripts/evaluate.py grade-swe --mode reference --reports .eval-data/reports/swe-reference-plan
.\.venv\Scripts\python.exe scripts/evaluate.py grade-swe --mode negative --reports .eval-data/reports/swe-negative-plan

# 将每题补丁命名为 <instance_id>.patch，再转换为官方预测格式
.\.venv\Scripts\python.exe scripts/evaluate.py export-swe --patches <patch-directory> --output .eval-data/predictions.jsonl

# 先对单题验证环境；需要安装 swebench、启动 Linux Docker 引擎并准备镜像
.\.venv\Scripts\python.exe scripts/evaluate.py grade-swe --mode reference --instance-id <instance_id> --reports .eval-data/reports/swe-ref-one --execute
.\.venv\Scripts\python.exe scripts/evaluate.py grade-swe --mode negative --instance-id <instance_id> --reports .eval-data/reports/swe-neg-one --execute

# 正式补丁验收；预测必须恰好覆盖当前所选任务集合
.\.venv\Scripts\python.exe scripts/evaluate.py grade-swe --mode candidate --predictions .eval-data/predictions.jsonl --reports .eval-data/reports/swe-candidate-new --execute
```

参考补丁必须 resolved，负对照必须 unresolved，才能标记该题环境有效。负对照只新增一个无关文本文件，保留原始缺陷；不使用会被官方 harness 跳过的空补丁。`--execute` 会调用 Docker，可拉取较大的公开镜像；模型推理不在此命令中。Docker 镜像拉取由 Docker 引擎负责，需要确认其代理配置，不能假设 Python 的代理变量会控制 Docker 引擎。

当前主机尚未完成 SWE 容器任务预检。脚本优先使用 `.eval-data/swe-env` 的独立验收环境，也可用 `LOOPSMITH_SWEBENCH_PYTHON` 指定解释器。执行计划记录实际 `swebench` 版本，不要在正式对照实验之间更新依赖。官方 harness 的安装、Docker 和资源要求见下方链接。

输入使用本地冻结元数据快照，任务版本通过 `base_commit` 固定。每次使用新的报告目录和 run ID，防止官方 harness 的结果缓存误用于新补丁。进程退出码 0 不等于修复成功，必须读取每题官方报告；缺少报告按环境或 harness 错误处理。

## 重新准备数据

```powershell
$env:HTTP_PROXY='http://127.0.0.1:7890'
$env:HTTPS_PROXY='http://127.0.0.1:7890'
$env:NO_PROXY='localhost,127.0.0.1,::1'
git clone --depth 1 https://github.com/jkoppel/QuixBugs.git .eval-data/quixbugs
.\.venv\Scripts\python.exe scripts/evaluate.py fetch-swe
.\.venv\Scripts\python.exe scripts/evaluate.py prepare
```

上述命令用于新副本；已存在的快照与冻结目录不会被覆盖。Quix 重新下载时应 checkout `benchmarks/selection.json` 记录的提交。首次 SWE 快照来自公开 rows API，并以内容 SHA256 固定；不是已核实的上游 Git 提交快照。正式复现实验应保存这一份快照，不能重新下载后仍当作同一版本。

## HKU 配置

已提供显式 `hku` Provider：`https://api.hku.hk/foundry/student/models/chat/completions`，认证头为 `api-key`。现有 Anthropic 格式历史和工具 schema 会在请求边界转换，AgentLoop、权限机制、TUI 和事件格式保持原有接口。

```dotenv
LOOPSMITH_LLM_PROVIDER=hku
LOOPSMITH_LLM_DEFAULT_MODEL=DeepSeek-V4-Flash
LOOPSMITH_LLM_BASE_URL=https://api.hku.hk/foundry/student/models
LOOPSMITH_LLM_MAX_OUTPUT_TOKENS=4096
LOOPSMITH_LLM_TIMEOUT_S=120
LOOPSMITH_LLM_CONTEXT_WINDOW=0
HKU_API_KEY=
```

在项目 `.env` 填入学校密钥即可。不要将密钥提交到 Git。模型部署名称大小写需与 HKU 门户一致；当前按既有 HKU 使用记录及用户选择设置为 `DeepSeek-V4-Flash`，尚未在线验证该部署。系统环境变量优先于 `.env`。Core 已启动时要重启，才能加载新配置。

适配器处理 SSE 文本、按 index 拼接的多工具调用、nullable function/name/arguments、DeepSeek reasoning_content 续传、工具结果回填和 token usage；截断或无合法 finish_reason 不记为完成。密钥只发送给 HKU HTTPS 主机，不跟随重定向；HTTP/流错误不自动重试，避免隐藏额度消耗。未知模型窗口不猜测占用比例，保持 0；没有 usage 时保留未知，不能声称 token 为零。

本地 HTTP 模拟测试验证兼容格式，不等于学校服务在线验收。在线验证应按“短回答 → 流式 → 工具往返”逐级进行；正式评测前另设总预算、调用次数、修复轮数及任务超时。本轮没有发出任何模型请求。

## 后续指标与边界

每次模型任务应关联任务 ID、Agent 提交版本、模型部署、预算、最终源码/补丁、事件日志和外部验收报告。记录通过率、误报成功率、工具调用数、耗时和 token；失败与超时保留在分母内，环境错误单列。

Quix 参考修复预检不是 Agent 成绩；30 道 Verified 子集不是全量榜单。先比较原有搜索与 RAG，再比较检索策略，每次固定其他变量；RAG 只能索引修复前源码和允许文档，不能索引参考补丁、未来提交、答案或保留测试。公开基准存在模型见过历史代码的可能，不能宣称完全无数据污染。

## 官方来源

- https://github.com/jkoppel/QuixBugs
- https://huggingface.co/datasets/SWE-bench/SWE-bench_Verified
- https://www.swebench.com/SWE-bench/guides/evaluation/
- https://github.com/SWE-bench/SWE-bench
