# 即梦 MCP · 本地版

让本地 agent 使用你自己的即梦账号，操作画布、图片、视频、音频、主体、剧本和故事板。基于官方 CLI 与固定网页接口适配，**这是社区项目，不是即梦官方 MCP**。

- 标准 **stdio MCP**：客户端启动本地进程，不开 HTTP 端口、不需要公共服务器。
- **10 个 MCP 工具、122 项固定业务动作**，通过 `action_schema` 查询参数。
- 每位用户自行授权；任务数据库、恢复记录和导出文件留在本机。
- 写操作默认 `dryRun:true`，生成积分额度默认 **0**；启用付费生成需要用户修改本地设置。
- Python **3.12+**；支持 macOS、Linux，Windows 请在 **WSL2** 中运行。暂不支持原生 Windows。

## 安装

安装需要 Python 3.12+ 和 Git。可使用独立工具环境：

```sh
uv tool install 'git+https://github.com/zsn2003/jimeng-mcp.git'
jimeng-mcp --version
```

也可从源码安装，不需要 uv：

```sh
git clone https://github.com/zsn2003/jimeng-mcp.git
cd jimeng-mcp
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/jimeng-mcp --version
```

下面命令中的 `jimeng-mcp`，源码安装时可替换成 `.venv/bin/jimeng-mcp`。这个包目前通过 GitHub 安装，未声明已发布到 PyPI。

## 首次授权

先安装官方依赖，再由用户自己打开授权网址：

```sh
jimeng-mcp install-cli all
jimeng-mcp auth login canvas
# 打开输出的官方授权网址，使用自己的即梦账号确认后：
jimeng-mcp auth complete canvas

# 多关键帧、官方会话与任务历史使用另一套官方 CLI 授权：
jimeng-mcp auth login plugin
jimeng-mcp auth complete plugin
jimeng-mcp doctor --check-auth
```

安装器只从官方 HTTPS 地址下载**固定版本并核对 SHA-256**，不执行下载的安装脚本，也不把官方二进制打包进本仓库。授权流程不会自动启动浏览器、代填密码或自动确认权限。设备凭据保存在本机私有目录，不要把凭据或 Cookie 发进聊天。

**两套 CLI OAuth 不等于网页工作室登录。** 画布和官方生成 CLI 授权后可使用对应动作；个人主体库、网页短剧工作室等动作还需要当前账号的独立网页会话。这部分是可选高级能力，使用本地 `auth import-web` 导入，详见 [授权与数据位置](docs/authentication.md)。未配置时会明确返回未授权，不会借用任何共享账号。

## 连接本地 agent

运行以下命令，得到与你的安装位置匹配的配置：

```sh
jimeng-mcp client-config
```

将输出的 `mcpServers` 合并到支持 stdio MCP 的客户端配置。典型配置如下，`command` 应使用本机实际可执行文件路径：

```json
{
  "mcpServers": {
    "jimeng": {
      "command": "/absolute/path/to/jimeng-mcp",
      "args": ["serve"]
    }
  }
}
```

使用 Codex CLI 时，可直接添加本地服务：

```sh
codex mcp add jimeng -- /absolute/path/to/jimeng-mcp serve
```

将示例路径换成 `command -v jimeng-mcp` 的输出。源码安装则使用该仓库 `.venv/bin/jimeng-mcp` 的绝对路径。

客户端必须在安装了工具的同一电脑、同一 OS 用户下运行。仅支持远程 HTTP URL 的客户端不能直接连接此 stdio 版本。无需 API Key、MCP Bearer Token 或对外开放端口。

可让 agent 执行：

> 先查即梦 capabilities，再查 account.status 的 action_schema，提交只读状态查询并回读同一 job。不要生成媒体。

使用顺序：`capabilities` → `action_schema` → `submit_job` → `get_job`。同一次请求重试保留原 `idempotencyKey` 和完整参数。

## 生成额度与写入

默认允许固定业务写入，但每个写动作仍默认 `dryRun:true`。真实修改或生成必须显式提供 `dryRun:false`；删除动作还要求对具体对象的永久删除确认。登录本身不会解除积分限制。

```sh
# 对支持报价/额度的动作设置单次最多 20 积分；不是月度累计预算
jimeng-mcp config --max-credits 20

# 可选：全局只读，连非计费的真实修改也阻止
jimeng-mcp config --read-only
jimeng-mcp config --allow-writes

# 仅在用户愿意自行承担消费时：允许无法执行数字额度的网页/多帧动作
jimeng-mcp config --max-credits unlimited

# 可选：要求视频先做官方样片，普通模式需要本次明确授权声明
jimeng-mcp config --video-policy sample_first
```

修改设置后重启 MCP 客户端中的服务进程。本地 `settings.json` 是积分与工作流策略的权威来源，启动器不会继承宿主 shell 的旧服务额度。`unlimited` 不代表无限免费，也不代替每次任务授权；积分和可用模型由用户的即梦账号决定。

## 工具

| 工具 | 作用 |
|---|---|
| `capabilities` | 当前动作、配置与可用条件；不是重新验证所有登录 |
| `studio_catalog` | 功能映射、限制和未接通项 |
| `action_schema` | 查询单项动作的完整参数 |
| `submit_job` / `submit_job_json` | 持久化提交；严格白名单与参数校验 |
| `get_job` | 查询原任务与真实业务结果 |
| `list_jobs` / `queue_summary` | 找回任务、查看队列 |
| `cancel_job` | 仅取消尚未执行的排队任务 |
| `artifact_manifest` | 校验已有受管素材并导出本地文件，返回 `localPath`、大小与 SHA-256 |

`COMPLETED` 只表示调用结束。还需检查 `operationOutcome`、生成资源终态和实际文件；dry-run 成功不代表已生成素材。超时、`partial`、`unknown` 沿原任务和上游对象核对，不能换一个 key 盲目重提。

一个数据目录只允许一个服务进程。多个客户端同时使用会明确提示占用；先关闭原客户端，或为**独立用途**创建不同的 `--data-dir`。不要用新目录绕过未决任务的防重保护。

## 功能边界

122 项动作不等于网页全部功能已实现。目录有 124 项功能，其中 108 项有完整或部分动作映射，16 项尚未实现：分镜重排、智能预演四项、工作室重拍、导演台预览/图像与视频渲染/定格/恢复导出五项，以及后期合成/字幕/配乐/转场/成片导出五项。

- 主体创建目前复制已有图片主体，文字更新不包含参考图重组。
- 个人声音动作只处理已有声音的绑定、替换和解绑，不生成新音色。
- 主体和分集永久删除未包含可恢复归档；分享图片读取仍缺成功运行验收。
- 视频延长需要网页已保存的原生目标草稿，自动建目标与本地服务实际生成验收仍未完成。
- 网页接口可能随官方更新变化；字段契约、代码存在和真实运行成功必须分开判断。

公开包不携带任何账号的历史“已验收”凭证，`acceptance_records` 初始为空。用户自己的登录、权限、模型和实际生成需在本地验证。详见 [验证范围](docs/verification.md)。

## 开发与隐私

```sh
python3 scripts/test_all.py
python3 -m pip wheel --no-deps . -w dist
```

发布 wheel 和源码包时使用 `python3 scripts/build_release.py`（需要 uv）。该脚本清除源码归档中的本机用户名、uid/gid 与文件时间元数据，并生成 `dist/SHA256SUMS`。

运行时只依赖 Python 标准库；构建使用 setuptools。源码、合成测试数据和固定接口契约可公开，账号、Cookie、令牌、数据库、日志、真实素材和测试账号回执均不进入仓库。请勿把本机数据目录作为 issue 附件。

协议依据：[MCP stdio transport](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports)。第三方依赖与商标说明见 [NOTICE](NOTICE)，本项目代码许可见 [LICENSE](LICENSE)。
