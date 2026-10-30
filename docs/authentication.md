# 登录指南

第一次使用，先把你的即梦账号连接好。按要用的功能选择入口，之后就可以让 Agent 调用对应能力。

| 入口 | 主要用途 | 登录命令 |
| --- | --- | --- |
| Canvas | 画布与基础创作 | `jimeng-mcp auth login canvas` |
| 创作 CLI | 多关键帧、创作会话、任务历史 | `jimeng-mcp auth login plugin` |
| 网页工作室 | 个人主体及部分网页工作室操作 | 按下方说明导入网页会话 |

## 先装好依赖

```sh
jimeng-mcp install-cli all
jimeng-mcp doctor
```

工具会根据当前系统下载两套官方 CLI，并核对 SHA-256。支持 macOS、Linux 的 arm64/amd64；Windows 请在 WSL2 中运行，数据目录放在 Linux 用户目录里。

也可以只装其中一套：

```sh
jimeng-mcp install-cli canvas
jimeng-mcp install-cli plugin
```

## Canvas 登录

```sh
jimeng-mcp auth login canvas
```

终端会显示官方授权网址和验证码。打开网址，用自己的即梦账号完成授权，然后运行：

```sh
jimeng-mcp auth complete canvas
jimeng-mcp auth status canvas
```

已有有效登录时会直接复用。等待时间不够，可以再次运行 `auth complete canvas`；也可以加 `--timeout 30` 设置本次等待秒数，支持 1–600 秒。`auth wait canvas` 是同一个命令的别名。

## 创作 CLI 登录

```sh
jimeng-mcp auth login plugin
```

同样打开终端给出的授权网址，完成后运行：

```sh
jimeng-mcp auth complete plugin
jimeng-mcp auth status plugin
```

完成登录后，工具会用官方账户查询检查状态。短期设备引用由程序自行保存和读取，你只需完成网页上的确认。

创作 CLI 使用当前系统用户的官方登录状态，可能与其他 `dreamina` 使用场景共用。Canvas 则使用本工具单独的 `jimeng-mcp` profile。

## 网页工作室

个人主体和部分工作室动作使用网页登录。需要这些能力时，把自己的网页会话整理成一个本地 JSON 文件，再导入工具。

示例结构如下，将占位内容替换为你自己的当前会话：

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

设置文件权限并导入：

```sh
chmod 600 ./web-session.json
jimeng-mcp auth import-web ./web-session.json
jimeng-mcp auth status web
```

更新已有会话时，在导入命令后加 `--replace`。工具会先检查文件，成功后再替换旧会话。

`configured: true` 表示本地文件已配置好；会话是否仍有效，会在实际调用网页功能时确认。会话过期后，更新文件并重新导入即可。Cookie 属于账号凭据，文件留在本机使用。

<details>
<summary>网页会话字段说明</summary>

必填字段为 `enabled`、`cookie`、`userAgent`；可选字段为 `headers`、`actionHeaders`、`validUntil`。`validUntil` 使用 Unix 秒时间戳。

导入时会检查固定字段、请求来源、签名头及有效期。源文件需要由当前用户拥有，权限为 `0600`。导入失败时，已有配置保持可用。

</details>

## 检查和退出

```sh
# 检查依赖版本
jimeng-mcp doctor

# 同时检查登录
jimeng-mcp doctor --check-auth

# 查看三种入口的状态
jimeng-mcp auth status

# 按入口退出
jimeng-mcp auth logout canvas
jimeng-mcp auth logout plugin
jimeng-mcp auth logout web
```

Canvas 退出当前 profile；创作 CLI 退出该系统用户共用的官方登录；网页退出会移除本工具保存的会话文件，浏览器保持原状态。

登录完成后，按 [README](../README.md#准备正式开工设个积分额度) 设置生成额度，就可以开始创作。

## 数据放在哪里

| 系统 | 默认目录 |
| --- | --- |
| macOS | `~/Library/Application Support/jimeng-mcp` |
| Linux / WSL2 | `$XDG_DATA_HOME/jimeng-mcp`；未设置时为 `~/.local/share/jimeng-mcp` |

任务、导出素材、设置和待完成的授权记录都在这个私有目录里。目录权限为 `0700`，认证文件权限为 `0600`。官方 OAuth token 由各自的 CLI 管理；本工具保存的短期设备引用在登录完成后删除。

自定义目录时，全局参数放在子命令前：

```sh
jimeng-mcp --data-dir /absolute/private/jimeng-mcp install-cli all
jimeng-mcp --data-dir /absolute/private/jimeng-mcp auth login canvas
```

## 安装版本与排错

当前安装器核对过的版本如下，核验日期为 **2026-10-30**：

| 程序 | 版本 | 下载与校验 |
| --- | --- | --- |
| `dreamina-canvas` | `1.2.0` | 固定分发目录 `1_0_0_53`，SHA 来自官方 `payload.sha256` |
| `dreamina` | `1.4.22` | 从官方 CDN 下载，使用本项目核对并固定的 SHA-256 |

Canvas 的官方地址会跳转到指定 CDN；安装器只接受相同版本、相同平台文件的一次指定跳转，然后核对 SHA。创作 CLI 的地址可能随官方更新替换文件，出现校验不一致时需要更新仓库中的版本与校验值。

已有自行安装的 CLI，可以用绝对路径设置 `DREAMINA_CANVAS_BIN` 和 `DREAMINA_PLUGIN_BIN`。`doctor` 会显示当前版本，方便对照。替换本工具安装目录中的其他版本时，使用 `install-cli --replace`。

安装或登录出错时，记录版本和错误码即可，Issue 中请去掉认证内容。

官方参考：[创作 CLI 分发入口](https://jimeng.jianying.com/cli)、[版本元数据](https://lf3-static.bytednsdoc.com/obj/eden-cn/psj_hupthlyk/ljhwZthlaukjlkulzlp/version.json)、[创作 CLI 说明](https://lf3-static.bytednsdoc.com/obj/eden-cn/psj_hupthlyk/ljhwZthlaukjlkulzlp/dreamina_cli_beta/SKILL.md)、[Canvas 校验清单](https://jimeng.jianying.com/canvas-cli/install/1_0_0_53/payload.sha256)。
