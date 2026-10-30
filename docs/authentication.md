# 本地安装与认证

认证在使用者自己的终端完成。MCP 工具不接收 Cookie、OAuth token 或设备授权代码，也不启动浏览器。`auth login` 只显示官方授权网址和用户验证码；用户自行打开网址确认后，再运行 `auth complete`。

## 安装官方 CLI

```sh
jimeng-mcp install-cli
jimeng-mcp doctor
```

默认安装两个官方程序到当前数据目录的 `bin/`。也可以只安装其中一个：

```sh
jimeng-mcp install-cli canvas
jimeng-mcp install-cli plugin
```

支持 macOS、Linux 的 arm64/amd64；Windows 使用 WSL2。原生 Windows 暂不支持本项目所需的 POSIX 文件权限和锁。WSL2 数据目录应放在 Linux 用户目录中。

安装器按实际系统选择平台，也接受显式、匹配当前系统的 `--platform`。下载完成且 SHA-256 匹配后才将文件设为可执行。它不运行下载的安装脚本、不修改 shell 启动文件，也不改变 `HOME`。已安装的相同校验值文件会直接复用；替换其他文件需要 `--replace`。

| 程序 | 本仓库核验版本 | 分发与校验 |
| --- | --- | --- |
| `dreamina-canvas` | `1.2.0` | 官方分发目录为 `1_0_0_53`；校验值来自该目录的 `payload.sha256`。目录号与程序版本不同。 |
| `dreamina` | `1.4.22` | 从官方 CDN 取得四个平台的文件，固定其 SHA-256。该分发未提供同等的官方校验清单；这些固定值不是厂商签名。 |

Canvas固定下载入口会返回一次官方302跳转。安装器仅允许从已登记的平台文件，跳转到 `lf3-lv-buz.vlabstatic.com/obj/image-lvweb-buz/videocut/aigc/dreamina_canvas_cli/1_0_0_53/` 下的**完全相同文件名**；不带查询参数、不转发凭据。第二次跳转、其他主机/版本/文件名以及创作CLI的跳转均拒绝。最终文件仍须通过原固定SHA-256校验。

本次元数据核验日期为 **2026-10-30**。官方创作 CLI 使用可变下载地址；如果上游替换文件，旧校验值会使安装失败。需要核实新版本及下载字节，再更新仓库中的固定值，不能跳过校验。

如已有自行安装的官方程序，可通过绝对路径设置 `DREAMINA_CANVAS_BIN`、`DREAMINA_PLUGIN_BIN`。`doctor` 会显示版本，并标明是否与本仓库核验版本一致；不会自动登录或创建任务。

## Canvas OAuth

```sh
jimeng-mcp auth login canvas
# 用户自行打开输出的网址，输入用户验证码并确认授权。
jimeng-mcp auth complete canvas
jimeng-mcp auth status canvas
```

`auth wait canvas` 是 `auth complete canvas` 的别名。可用 `--timeout 30` 调整本次等待，范围为 1–600 秒。等待未完成时，设备引用会保留，供再次 `complete` 使用。

对应官方命令为：

```text
dreamina-canvas auth login --format json --non-interactive --profile jimeng-mcp
dreamina-canvas auth wait --device-code <私有设备引用> --timeout 120s --format json --non-interactive --profile jimeng-mcp
dreamina-canvas auth status --format json --non-interactive --profile jimeng-mcp
```

引导程序不会传入 `--force`。已有有效登录可复用。Canvas 使用当前数据目录的 `canvas/`，并固定使用 `jimeng-mcp` profile；该状态与用户其他 Canvas profile 分开。

`auth status canvas` 读取官方本地登录状态，不能替代所有后续服务端权限检查。

## 创作 CLI OAuth

```sh
jimeng-mcp auth login plugin
# 用户自行完成网址上的授权。
jimeng-mcp auth complete plugin
jimeng-mcp auth status plugin
```

对应官方命令是 `dreamina login --headless`、`dreamina login checklogin --device_code=… --poll=120`。引导程序将短期 `device_code` 存为私有文件，完成时自行读取，不要求把它粘贴进聊天或命令历史。

未发现官方创作 CLI 的独立 `status` 子命令，因此 `auth status plugin` 使用官方只读 `user_credit` 查询验证认证，仅返回认证是否成功，不输出账户信息、积分明细或 token。完成设备登录后也会做一次该只读查询。

**创作 CLI 自行管理当前系统用户的官方登录目录。** 本项目不改 `HOME`，也不假定不存在的状态目录参数。该 OAuth 登录可能与该用户其他 `dreamina` 使用场景共享；更换 `--data-dir` 不会为创作 CLI 自动创建另一套账号状态。

## 可选网页会话

官方 CLI OAuth 与网页 Cookie 会话是两个认证入口。只需官方 CLI 功能时，不必导入网页会话。个人主体及部分工作室功能可能还需要网页会话，具体以能力状态为准。

导入仅接受用户自行准备的本地 JSON。程序不会读取浏览器 Cookie 数据库，也不会通过 MCP 接收认证内容。示例中的值均为占位符：

```json
{
  "enabled": true,
  "cookie": "YOUR_OWN_COOKIE",
  "userAgent": "YOUR_BROWSER_USER_AGENT",
  "headers": {
    "origin": "https://jimeng.jianying.com",
    "referer": "https://jimeng.jianying.com/"
  },
  "actionHeaders": {}
}
```

文件使用固定结构：`enabled`、`cookie`、`userAgent`，以及可选的 `headers`、`actionHeaders`、`validUntil`。`validUntil` 为 Unix 秒时间戳。导入会复用网页适配器的完整字段、来源、签名头白名单及到期校验；不允许任意额外字段或转发头。

```sh
chmod 600 ./web-session.json
jimeng-mcp auth import-web ./web-session.json
jimeng-mcp auth status web
```

已有导入文件时需要明确加 `--replace`。程序先在数据目录内建立 0600 临时文件，校验成功后原子替换 `web-session.json`；失败保留旧文件并清理临时副本。源文件保留，认证内容不会打印。

`configured: true` 仅说明本地格式、有效期和权限检查通过，**不代表已验证当前网页登录有效**。实际调用可能因会话过期、权限或官方接口变化而失败。不要把真实会话 JSON 放入 Git、聊天、Issue 或 MCP 参数。

## 检查、退出与文件范围

```sh
jimeng-mcp doctor
jimeng-mcp doctor --check-auth
jimeng-mcp auth status
jimeng-mcp auth logout canvas
jimeng-mcp auth logout plugin
jimeng-mcp auth logout web
```

- `doctor` 默认只读取平台、文件权限和两个 CLI 的版本；`--check-auth` 另检查 OAuth，创作 CLI 会发起只读账户查询。
- `auth status` 默认汇总三种入口，网页状态仍只做本地检查。
- `logout canvas` 调用官方退出命令，只影响配置的 Canvas profile。
- `logout plugin` 调用官方 `dreamina logout`，会清除该系统用户共享的官方创作 CLI 登录态。
- `logout web` 只删除本项目导入的网页会话文件，不退出浏览器，也不删除用户提供的源文件。

认证状态和网页会话文件需由当前用户拥有，文件权限为 `0600`，私有目录为 `0700`。OAuth access/refresh token 由官方 CLI 保存；本项目只保存待完成授权的设备引用，成功后删除该引用。

默认数据目录由主入口决定：macOS 为 `~/Library/Application Support/jimeng-mcp`，Linux/WSL2 为 `$XDG_DATA_HOME/jimeng-mcp`，未设置 XDG 时为 `~/.local/share/jimeng-mcp`。使用自定义目录时，全局参数放在子命令前：

```sh
jimeng-mcp --data-dir /absolute/private/jimeng-mcp install-cli
jimeng-mcp --data-dir /absolute/private/jimeng-mcp auth login canvas
```

`run(args)` 成功返回 `0`，本地引导错误返回 `1`；`doctor` 依赖缺失或版本不匹配也返回 `1`。`auth status` 用结构化字段报告未登录/不可用状态，状态汇总本身成功时返回 `0`。

这些命令不创建图片、音频或视频任务，不修改费用上限。登录成功与允许付费生成是不同配置。

## 官方来源

- [创作 CLI 官方分发入口与安装脚本](https://jimeng.jianying.com/cli)：确认平台文件名与官方 CDN；本项目读取分发契约，不执行该脚本。
- [创作 CLI 官方版本元数据](https://lf3-static.bytednsdoc.com/obj/eden-cn/psj_hupthlyk/ljhwZthlaukjlkulzlp/version.json)。
- [创作 CLI 官方使用说明](https://lf3-static.bytednsdoc.com/obj/eden-cn/psj_hupthlyk/ljhwZthlaukjlkulzlp/dreamina_cli_beta/SKILL.md)。
- [Canvas 固定分发校验清单](https://jimeng.jianying.com/canvas-cli/install/1_0_0_53/payload.sha256)。
- 本地官方 `dreamina-canvas schema` 中的 `auth` 契约及 `auth login/wait/status/logout --help`；官方 `dreamina login --help`、`login checklogin --help`、`logout --help`、`user_credit --help`。
