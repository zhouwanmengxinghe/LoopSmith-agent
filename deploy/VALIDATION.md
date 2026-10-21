# Docker 部署验证记录

日期：2026-10-21。环境：Windows Docker Desktop Linux Engine，Docker CLI 29.8.2、Compose v5.6.0。该记录验证打包与启动机制，不是模型在线效果或生产负载报告。

## 实际执行结果

| 检查 | 结果 |
|---|---|
| 多阶段 Docker 镜像构建 | 成功；构建下载经本机 7890 代理，基础 Python 镜像来自已有本地缓存 |
| PowerShell 一键启动 | 使用独立 Compose 项目、虚拟密钥、专用样例工作目录，`-NoChat` 启动至 healthy |
| Python 健康检查 | 6 个用例通过，包含有效 Ping 与错误/无关响应拒绝 |
| 静态检查 | 新增 Python 文件 Ruff / Mypy 通过；PowerShell 解析与 Bash 语法检查通过 |
| 真实 Core 通信 | `loopsmith ping` 返回 pong；没有提交 Agent 任务 |
| 镜像内 TUI | Textual headless pilot 连接真实 Core、创建 Session，聊天输入框启用；未发送用户任务 |
| 普通用户运行 | 容器 UID 1000，镜像用户 loopsmith |
| 内置资源打包 | review Skill 与 reviewer 角色配置存在于安装后的包中 |
| 文件工具与挂载 | 直接调用真实 WriteFileTool、ReadFileTool，宿主机可读取容器写入的样例文件 |
| Bash / pytest | 通过真实 BashTool 执行镜像内 pytest，人工样例 1 passed；不是基准修复成绩 |
| 容器重建 | `--force-recreate` 后工作区文件、命名卷证明文件、原 Session meta 仍存在 |
| 模型调用 | 0；使用虚拟密钥，TUI/工具验收时断开测试容器外部网络，未生成 Agent run 记录 |
| 依赖一致性 | 镜像内 `pip check` 通过 |

测试镜像 ID：

```text
sha256:2fc9307685f235a462efd99af505dd6824cb02ed6b8415d138bd689e6f8a2f35
```

当次镜像依赖记录：LoopSmith 0.0.1、Anthropic SDK 1.17.0、Textual 8.2.8、pytest 9.1.1、Pydantic 2.14.1。Dockerfile 按项目声明的版本范围安装，不承诺后续构建解析出完全相同的版本；发布前应重新验收并保留镜像 digest。

原始验收辅助脚本与虚拟配置保存在本机 `.eval-data/docker-deploy-smoke/`，不提交 Git。测试创建的独立容器与卷在验收后清理，镜像保留供本地使用。未覆盖 Linux/macOS 真机部署、桌面终端人工视觉体验、真实供应商请求、任意目标仓库的依赖兼容或长时间运行。TUI 的历史 Session 选择/续聊尚未实现；此次只确认文件持久化和既有回放入口可用于后续观察。
