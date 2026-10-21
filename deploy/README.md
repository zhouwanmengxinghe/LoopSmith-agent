# Docker 一键启动

镜像运行 LoopSmith Core，启动脚本等待 Core 就绪后进入 Textual TUI。API 密钥只通过运行时配置传入，项目源码与用户仓库分离；会话、日志和权限记录保存在命名卷中。首次构建需要联网下载基础镜像和依赖，正常聊天会消耗所配置模型的额度。

## 1. 准备

- 安装并启动 Docker Desktop（Windows/macOS 使用 Linux containers），或 Linux Docker Engine。
- Docker Compose v2.20+，终端支持交互式 TTY；Windows 推荐 Windows Terminal。
- 待操作的代码仓库保存在本机。

在项目根目录复制配置，现有根目录 `.env` 不会被覆盖：

```powershell
Copy-Item deploy/.env.example deploy/.env
notepad deploy/.env
```

填写 `DEEPSEEK_API_KEY` 和 `LOOPSMITH_WORKSPACE`。Windows 路径使用正斜杠，例如 `D:/project/my-python-project`；有空格时可用双引号包住整个配置值。Linux/macOS 用绝对路径。目标目录必须已经存在，配置错误不会自动创建目录。

默认使用 DeepSeek 官方 Anthropic 兼容接口。HKU 使用 `LOOPSMITH_LLM_PROVIDER=hku`、`HKU_API_KEY`、学校的模型部署名及接口地址。Anthropic 使用 `ANTHROPIC_API_KEY`、相应模型名与 `https://api.anthropic.com`。只配置需要的供应商；不要把密钥放进 Dockerfile 或提交到 Git。

## 2. 一键启动并聊天

Windows PowerShell：

```powershell
.\start.ps1
```

也可以在资源管理器中双击项目根目录的 `start.cmd`，它会打开 PowerShell 并运行同一个启动流程。

若本机执行策略禁止运行脚本，可仅对此次进程使用 `powershell -NoProfile -ExecutionPolicy Bypass -File .\start.ps1`，无需修改系统执行策略。

Linux/macOS：

```bash
bash start.sh
```

若没有 `deploy/.env`，脚本会生成模板并提示填写，再退出；填好后重新执行。首次会构建镜像并等待健康检查，通过后进入 TUI。在 TUI 中输入：

```text
请读取 /workspace 的项目结构，说明主要模块。先不要修改文件。
```

或：

```text
请运行项目已有测试并分析失败原因，本次不要修改代码，不启动子 Agent。
```

所有实际任务由所配模型执行。健康检查只发送 `core.ping`，不会调用模型。目标仓库以可写方式挂载，Agent 修改 `/workspace` 会同步改变宿主机文件；执行审批仍由 TUI 中的原有权限机制处理。

## 3. 启动选项

```powershell
# 仅后台运行，不进入聊天；可用于非交互终端
.\start.ps1 -NoChat
# 使用已构建镜像，跳过构建
.\start.ps1 -NoBuild
# 临时操作其他仓库（路径优先于配置文件）
.\start.ps1 -Workspace "D:/project/another-repo"
# 显式复用原有根目录配置；不会改写该文件
.\start.ps1 -EnvFile .env
```

```bash
bash start.sh --no-chat --no-build
bash start.sh --workspace /absolute/repository --env-file deploy/.env
```

同一个 Compose 项目只有一个 Core/工作区。切换 Workspace 会重建该容器，应等现有任务结束再切换；持久化卷会保留原来的运行记录。不同用户或仓库需要完全独立的数据时，可以直接使用 Compose 的 `-p` 为不同部署命名。

## 4. 日常管理

以下命令在项目根目录执行。使用自定义配置文件时，同时设置 `LOOPSMITH_ENV_FILE` 为其绝对路径并替换 `--env-file`；启动脚本会自动完成这一步。启动脚本无需调用后台进程启动子命令，容器主进程本身就是 Core。

```powershell
# 再次进入聊天（会创建新聊天会话，历史记录仍保存在卷中）
docker compose --env-file deploy/.env exec agent loopsmith-tui
# 查看 Core 日志与状态
docker compose --env-file deploy/.env logs --tail 60 agent
docker compose --env-file deploy/.env ps
# 停止服务并保留数据
docker compose --env-file deploy/.env down
# 修改密钥/模型后，重新运行启动脚本以应用新环境；restart 不重新读取环境文件
.\start.ps1 -NoBuild
```

数据在 Compose 的 `agent-data` 命名卷，挂载为 `/home/loopsmith/.loopsmith`；默认项目下实际卷名为 `loopsmith_agent-data`。会话、运行记录和日志会保留，TUI 启动时尚无选择旧会话的界面。需要观察历史运行时，可执行 `loopsmith-tui --replay <RUN_ID>`；回放不等于继续执行旧任务。不要使用 `down -v`，除非明确要删除历史数据。

服务只监听容器内部 `127.0.0.1:7437`，不发布宿主机端口；TUI 通过 `compose exec` 在同容器访问。镜像以默认 UID/GID 1000 的普通用户运行。Linux 宿主用户非 1000 时，在配置中设置 `LOOPSMITH_UID` / `LOOPSMITH_GID` 并重新构建；已有卷也需保持相应访问权限。脚本不会对用户仓库自动递归修改权限。

容器默认使用 `deploy/container.toml`，避免载入目标仓库中包含宿主绝对路径的配置；项目 `.loopsmith/context.md` 和 Skills 仍由原有加载器读取。需要配置 MCP 时，准备容器可访问的 TOML 并设置 `LOOPSMITH_CONTAINER_CONFIG` 为容器内路径。stdio MCP 所需 Node.js 或其他运行时需要自行扩展镜像。

## 5. 网络代理

宿主机的 `127.0.0.1` 与容器的 `127.0.0.1` 不同。Windows Docker Desktop 可尝试在 `deploy/.env` 中配置：

```dotenv
LOOPSMITH_BUILD_PROXY=http://host.docker.internal:7890
LOOPSMITH_CONTAINER_PROXY=http://host.docker.internal:7890
```

前者仅供构建阶段安装依赖，后者用于容器运行时 HTTP 请求；均不把公共目标加入 NO_PROXY。代理必须能从容器访问。Linux `host-gateway` 不是宿主机 loopback，代理仅监听 127.0.0.1 时通常不可达，需要用户提供适合的受限监听/网络配置；脚本不会自行修改代理设置或失败后绕过代理。

基础镜像的拉取由 Docker Desktop/Engine 管理，需要在其代理设置中配置。构建参数不能替代 daemon 的拉取代理。构建网络失败时先检查代理，脚本不会禁用 TLS 或自动换直连。

## 6. 镜像交付给其他人

目前默认本地构建 `loopsmith:local`，没有自动发布镜像。发布者可以先构建后推送到自己的镜像仓库，接收者在配置中设置 `LOOPSMITH_IMAGE` 并运行：

```powershell
docker compose --env-file deploy/.env pull agent
.\start.ps1 -NoBuild
```

也可以离线 `docker save` / `docker load` 分发同名镜像。API 密钥、用户代码和命名卷均不在交付镜像中。

## 7. 能力范围与验证

镜像包含 Python 3.12、Git、Bash、pytest、pytest-asyncio 和 Agent 依赖。它没有预装每个目标项目的依赖，也不包含 JDK、Node.js 或 SWE-bench 题目镜像。复杂项目应派生专用镜像，在构建时安装其系统/语言依赖；运行时普通用户也可使用 `python -m pip install --user ...`，但容器重建后这类安装不会保留。

Docker 一键启动不自动启用评测专用的 `check_issue` / `run_tests` 守卫，也不沿用此前模型实验的 token 总账。聊天中的步骤硬上限由 `LOOPSMITH_MAX_STEPS` 控制，真实模型支出按供应商计费。

本地机制检查：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_docker_healthcheck.py -q
```

容器验收应使用虚拟凭据与隔离网络，检查真实 Ping、TUI 就绪、挂载写入、会话落盘及容器重建后数据保留；不通过聊天提交真实任务。模型在线可用性需要用户另行执行一项小任务确认。

2026-10-21 的本机验收结果见 [部署验证记录](VALIDATION.md)。
