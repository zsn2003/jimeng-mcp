"""Local terminal onboarding; never exposed as MCP tools.

Official command contracts checked 2026-10-30: canvas 1.2.0 public schema,
Dreamina creation CLI 1.4.22 help. Tokens and raw CLI output are never printed.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

CANVAS_VERSION = "1.2.0"
PLUGIN_VERSION = "1.4.22"
CANVAS_BASE = "https://jimeng.jianying.com/canvas-cli/install/1_0_0_53/"
CANVAS_CDN_BASE = "https://lf3-lv-buz.vlabstatic.com/obj/image-lvweb-buz/videocut/aigc/dreamina_canvas_cli/1_0_0_53/"
PLUGIN_BASE = "https://lf3-static.bytednsdoc.com/obj/eden-cn/psj_hupthlyk/ljhwZthlaukjlkulzlp/dreamina_cli_beta/"
# Canvas hashes are from the official versioned payload.sha256. Plugin hashes
# are SHA-256 pins of official 1.4.22 artifacts fetched 2026-10-30; that release
# does not publish an equivalent checksum manifest. Mutable URLs fail closed
# if the vendor replaces bytes until these reviewed pins are updated.
HASHES = {
    "canvas": {
        "darwin_amd64": "031b373f25a2d0ae06c9c85f4114df67a43dde8b98ffedf42348e975fe57f2c0",
        "darwin_arm64": "b140a8094630f70aa7bea978351d5f15cc27f59e67880f43fce30ed09771601c",
        "linux_amd64": "8c40be4b025c9033431961709f15575ba4a77d81794e5469d5dd6a12c079499b",
        "linux_arm64": "1a58caa270c5a8601beeb5d3c9bb67b47124602dac6c1d630ab05976d91f3392",
    },
    "plugin": {
        "darwin_amd64": "df195a76cfc23263b44663186f5136240aec935e2e019da071c4e5b2e1f067ff",
        "darwin_arm64": "85ba083d85e3a37a40edaac482ec832b9e6e8372e35c9e41159ce49b57a5fcb6",
        "linux_amd64": "d831fd5f6820ddbd8afcc39afaf0194cd63b17f780057dcc6c486bc7e152d7b6",
        "linux_arm64": "55d086f054464e79f4099e50ce6c51d5e920ccfb59a24a154f4cfc4f8565c378",
    },
}
BIN_NAMES = {"canvas": "dreamina-canvas", "plugin": "dreamina"}
BIN_ENV = {"canvas": "DREAMINA_CANVAS_BIN", "plugin": "DREAMINA_PLUGIN_BIN"}
MAX_DOWNLOAD = 160 * 1024 * 1024
MAX_PRIVATE_FILE = 65536
MAX_CLI_OUTPUT = 1024 * 1024


class OnboardingError(Exception):
    """Only fixed, credential-free errors may cross the terminal boundary."""
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


def configure_parser(subparsers) -> None:
    auth = subparsers.add_parser("auth", help="本地终端登录、状态和私有网页会话导入")
    commands = auth.add_subparsers(dest="auth_action", required=True)
    login = commands.add_parser("login", help="输出官方设备授权网址，不自动打开浏览器")
    login.add_argument("provider", choices=("canvas", "plugin"))
    complete = commands.add_parser("complete", aliases=["wait"], help="用户授权后完成之前的设备登录")
    complete.add_argument("provider", choices=("canvas", "plugin"))
    complete.add_argument("--timeout", type=int, default=120, help="等待秒数，1–600；默认120")
    status_parser = commands.add_parser("status", help="仅输出认证状态，不输出账号凭据")
    status_parser.add_argument("provider", choices=("canvas", "plugin", "web", "all"), nargs="?", default="all")
    logout = commands.add_parser("logout", help="清除所选认证入口的本地登录状态")
    logout.add_argument("provider", choices=("canvas", "plugin", "web"))
    web_import = commands.add_parser("import-web", help="导入用户自行提供的0600网页会话JSON")
    web_import.add_argument("file", type=Path)
    web_import.add_argument("--replace", action="store_true", help="明确替换已有导入文件")
    doctor = subparsers.add_parser("doctor", help="检查平台、私有目录和CLI，不创建生成任务")
    doctor.add_argument("--check-auth", action="store_true", help="另检查OAuth状态；plugin会只读查询账户")
    install = subparsers.add_parser("install-cli", help="下载并校验固定版本官方CLI到私有bin目录")
    install.add_argument("provider", choices=("canvas", "plugin", "all"), nargs="?", default="all")
    install.add_argument("--platform", choices=tuple(HASHES["canvas"]), help="显式平台，必须匹配当前系统")
    install.add_argument("--replace", action="store_true", help="明确替换私有bin中不同版本的现有文件")


def _emit(value) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _platform() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64", "aarch64": "arm64"}.get(machine)
    name = system + "_" + str(arch)
    if name not in HASHES["canvas"]:
        raise OnboardingError("UNSUPPORTED_PLATFORM", "支持macOS、Linux及Windows WSL2；原生Windows不支持本项目的POSIX权限保护。")
    return name


def _private_dir(path: Path) -> Path:
    if path.is_symlink():
        raise OnboardingError("PRIVATE_PATH_INVALID", "私有目录不能是符号链接。")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid():
        raise OnboardingError("PRIVATE_PATH_PERMISSIONS", "数据目录必须由当前用户拥有，权限为0700。")
    return path


def _root() -> Path:
    value = os.environ.get("JIMENG_MCP_DATA_DIR")
    if not value or not Path(value).is_absolute():
        raise OnboardingError("DATA_DIR_NOT_CONFIGURED", "请从jimeng-mcp主入口运行，并使用绝对路径的数据目录。")
    return _private_dir(Path(value))


def _read_private(path: Path, maximum: int = MAX_PRIVATE_FILE) -> bytes:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.geteuid() or info.st_size > maximum):
            raise OnboardingError("PRIVATE_FILE_PERMISSIONS", "认证文件必须是当前用户拥有的0600普通文件，且不超过大小限制。")
        raw = source.read(maximum + 1)
    if len(raw) > maximum:
        raise OnboardingError("PRIVATE_FILE_TOO_LARGE", "认证文件超过大小限制。")
    return raw


def _write_private(path: Path, raw: bytes, *, replace: bool) -> None:
    _private_dir(path.parent)
    if os.path.lexists(path):
        _read_private(path)
        if not replace:
            raise OnboardingError("PRIVATE_FILE_EXISTS", "已有认证文件；确认后使用--replace替换。")
    fd, filename = tempfile.mkstemp(prefix=".auth-", dir=path.parent)
    temporary = Path(filename)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _binary(provider: str) -> Path:
    path = Path(os.environ.get(BIN_ENV[provider]) or (_root() / "bin" / BIN_NAMES[provider]))
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise OnboardingError("CLI_NOT_INSTALLED", "官方CLI尚未配置或不可执行；先运行install-cli。")
    return path


def _invoke(provider: str, arguments: list[str], *, timeout: int = 45):
    argv = [str(_binary(provider)), *arguments]
    if provider == "canvas":
        profile = os.environ.get("DREAMINA_CANVAS_PROFILE", "jimeng-mcp")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", profile):
            raise OnboardingError("PROFILE_INVALID", "Canvas profile名称无效。")
        argv += ["--format", "json", "--non-interactive", "--profile", profile]
    try:
        process = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                 timeout=timeout, check=False, env=dict(os.environ))
    except subprocess.TimeoutExpired:
        raise OnboardingError("CLI_TIMEOUT", "官方CLI等待超时；可稍后检查状态或继续当前设备授权。") from None
    except OSError:
        raise OnboardingError("CLI_START_FAILED", "无法运行已配置的官方CLI。") from None
    if len(process.stdout) + len(process.stderr) > MAX_CLI_OUTPUT:
        raise OnboardingError("CLI_OUTPUT_LIMIT", "官方CLI输出超过安全处理限制。")
    return process


def _json_object(text: str) -> dict:
    try:
        result = json.loads(text)
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError, RecursionError):
        return {}


def _canvas_data(process) -> dict:
    envelope = _json_object(process.stdout)
    if process.returncode != 0 or envelope.get("ok") is not True or not isinstance(envelope.get("data"), dict):
        raise OnboardingError("CANVAS_COMMAND_FAILED", "Canvas命令未成功；原始输出未公开，请用官方CLI在本地终端检查。")
    return envelope["data"]


def _plugin_ok(process) -> bool:
    text = (process.stdout + "\n" + process.stderr).lower()
    if process.returncode != 0 or any(marker in text for marker in ("not logged in", "login required", "未检测到有效登录态", "请先登录")):
        return False
    obj = _json_object(process.stdout)
    if obj.get("ok") is False or obj.get("success") is False:
        return False
    if "ret" in obj and str(obj["ret"]) != "0":
        return False
    return True


def _challenge(provider: str, process) -> dict | None:
    if provider == "canvas":
        data = _canvas_data(process)
        challenge = data.get("challenge")
        state = data.get("state")
        if not challenge and isinstance(state, dict) and state.get("loggedIn") is True:
            return None
        if not isinstance(challenge, dict):
            raise OnboardingError("LOGIN_RESPONSE_INVALID", "官方登录返回未包含有效设备授权信息。")
        values = {"device_code": challenge.get("deviceCode"), "user_code": challenge.get("userCode"),
                  "verification_uri": challenge.get("verificationUri")}
    else:
        if not _plugin_ok(process):
            raise OnboardingError("PLUGIN_LOGIN_FAILED", "官方创作CLI未成功启动设备登录；原始输出未公开。")
        obj = _json_object(process.stdout)
        values = {}
        for key in ("device_code", "user_code", "verification_uri"):
            value = obj.get(key)
            if value is None:
                match = re.search(r"(?mi)^\s*" + re.escape(key) + r"\s*[:=]\s*(\S+)\s*$", process.stdout)
                value = match.group(1) if match else None
            values[key] = value
        if not any(values.values()):
            # login may reuse valid state. Confirm with a separate read; never
            # infer success from an unrecognized message or expose raw output.
            if _plugin_ok(_invoke("plugin", ["user_credit"])):
                return None
            raise OnboardingError("LOGIN_RESPONSE_INVALID", "无法识别官方设备授权信息，当前认证也未验证。")
    if any(not isinstance(values.get(k), str) or not 1 <= len(values[k]) <= 8192 for k in values):
        raise OnboardingError("LOGIN_RESPONSE_INVALID", "官方设备授权信息不完整。")
    if any(any(ord(c) < 32 for c in values[k]) for k in values):
        raise OnboardingError("LOGIN_RESPONSE_INVALID", "官方设备授权信息包含无效字符。")
    uri = urllib.parse.urlsplit(values["verification_uri"])
    if uri.scheme != "https" or not uri.hostname or uri.username or uri.password or uri.port not in (None, 443):
        raise OnboardingError("LOGIN_URL_INVALID", "官方设备授权网址必须是有效HTTPS地址。")
    return values


def _pending(provider: str) -> Path:
    return _private_dir(_root() / "auth") / (provider + "-pending.json")


def _remove_pending(provider: str) -> None:
    path = _pending(provider)
    if os.path.lexists(path):
        _read_private(path)
        path.unlink()


def _login(provider: str) -> None:
    process = _invoke(provider, ["auth", "login"] if provider == "canvas" else ["login", "--headless"])
    challenge = _challenge(provider, process)
    if challenge is None:
        _remove_pending(provider)
        _emit({"provider": provider, "loggedIn": True, "reusedExistingLogin": True})
        return
    record = {"version": 1, "provider": provider, "device_code": challenge["device_code"], "createdAt": int(time.time())}
    _write_private(_pending(provider), json.dumps(record).encode(), replace=True)
    _emit({"provider": provider, "status": "awaiting_user_authorization",
           "verification_url": challenge["verification_uri"], "user_code": challenge["user_code"],
           "next": "用户自行打开上方官方网址完成授权，然后在本地运行 jimeng-mcp auth complete " + provider,
           "browserOpened": False})


def _complete(provider: str, timeout: int) -> None:
    if type(timeout) is not int or not 1 <= timeout <= 600:
        raise OnboardingError("TIMEOUT_INVALID", "等待秒数必须在1到600之间。")
    try:
        pending = json.loads(_read_private(_pending(provider)))
    except FileNotFoundError:
        raise OnboardingError("LOGIN_NOT_STARTED", "尚无待完成设备授权；先运行auth login。") from None
    if (not isinstance(pending, dict) or pending.get("version") != 1 or pending.get("provider") != provider
            or not isinstance(pending.get("device_code"), str) or not 1 <= len(pending["device_code"]) <= 8192
            or any(ord(c) < 32 for c in pending["device_code"])):
        raise OnboardingError("PENDING_LOGIN_INVALID", "本地设备授权引用无效；请重新启动登录。")
    code = pending["device_code"]
    if provider == "canvas":
        process = _invoke(provider, ["auth", "wait", "--device-code", code, "--timeout", str(timeout) + "s"], timeout=timeout + 15)
        data = _canvas_data(process)
        state = data.get("state")
        logged_in = isinstance(state, dict) and state.get("loggedIn") is True
    else:
        process = _invoke(provider, ["login", "checklogin", "--device_code=" + code, "--poll=" + str(timeout)], timeout=timeout + 15)
        logged_in = _plugin_ok(process) and _plugin_ok(_invoke("plugin", ["user_credit"]))
    if not logged_in:
        raise OnboardingError("LOGIN_NOT_COMPLETE", "设备授权尚未验证成功；原待授权引用已保留，可再次complete或检查status。")
    _remove_pending(provider)
    _emit({"provider": provider, "loggedIn": True, "deviceReferenceRemoved": True})


def _web_path() -> Path:
    expected = _root() / "web-session.json"
    configured = Path(os.environ.get("JIMENG_WEB_SESSION_FILE", str(expected)))
    if configured != expected:
        raise OnboardingError("WEB_PATH_INVALID", "网页会话导入仅写入当前数据目录的web-session.json。")
    return expected


def _validate_web_file(path: Path) -> None:
    from .bridge_loader import load_bridge
    load_bridge()
    web = importlib.import_module("web_actions")
    old = os.environ.get("JIMENG_WEB_SESSION_FILE")
    try:
        os.environ["JIMENG_WEB_SESSION_FILE"] = str(path)
        web._load_session()
    except web.SessionError:
        raise OnboardingError("WEB_SESSION_INVALID", "网页会话未通过固定字段、有效期或私有权限校验；文件内容未公开。") from None
    finally:
        if old is None:
            os.environ.pop("JIMENG_WEB_SESSION_FILE", None)
        else:
            os.environ["JIMENG_WEB_SESSION_FILE"] = old


def _import_web(source: Path, replace: bool) -> None:
    # No browser cookie access or credentials over MCP. User chooses this file.
    raw = _read_private(source.expanduser().absolute())
    try:
        parsed = json.loads(raw)
    except (ValueError, UnicodeError):
        raise OnboardingError("WEB_SESSION_INVALID", "用户提供的网页会话文件不是有效JSON。") from None
    if not isinstance(parsed, dict):
        raise OnboardingError("WEB_SESSION_INVALID", "网页会话必须是JSON对象。")
    target = _web_path()
    if os.path.lexists(target):
        _read_private(target)
        if not replace:
            raise OnboardingError("PRIVATE_FILE_EXISTS", "已存在网页会话；确认后使用--replace替换。")
    fd, filename = tempfile.mkstemp(prefix=".web-import-", dir=_root())
    temporary = Path(filename)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        _validate_web_file(temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    _emit({"provider": "web", "configured": True, "serverLoginVerified": False,
           "note": "仅校验并导入本地网页会话；官方CLI OAuth与网页会话分别认证。"})


def _status(provider: str) -> dict:
    if provider == "web":
        path = _web_path()
        if not os.path.lexists(path):
            return {"provider": "web", "configured": False, "serverLoginVerified": False}
        _read_private(path)
        _validate_web_file(path)
        return {"provider": "web", "configured": True, "serverLoginVerified": False}
    if provider == "canvas":
        data = _canvas_data(_invoke(provider, ["auth", "status"]))
        if type(data.get("loggedIn")) is not bool:
            raise OnboardingError("LOGIN_STATUS_INVALID", "Canvas未返回预期认证状态字段。")
        return {"provider": provider, "loggedIn": data["loggedIn"], "verification": "official_local_auth_status"}
    return {"provider": provider, "loggedIn": _plugin_ok(_invoke(provider, ["user_credit"])),
            "verification": "official_read_only_account_query", "usesOfficialCliSharedLoginStore": True}


def _logout(provider: str) -> None:
    if provider == "web":
        path = _web_path()
        if os.path.lexists(path):
            _read_private(path)
            path.unlink()
        _emit({"provider": provider, "localSessionRemoved": True})
        return
    process = _invoke(provider, ["auth", "logout"] if provider == "canvas" else ["logout"])
    if provider == "canvas":
        if _canvas_data(process).get("loggedOut") is not True:
            raise OnboardingError("LOGOUT_NOT_VERIFIED", "Canvas未确认清除该profile的登录态。")
    elif not _plugin_ok(process):
        raise OnboardingError("LOGOUT_NOT_VERIFIED", "官方创作CLI未确认退出。")
    _remove_pending(provider)
    _emit({"provider": provider, "loggedOut": True,
           "scope": "configured_canvas_profile" if provider == "canvas" else "official_cli_shared_login_store"})


class _PinnedDownloadRedirect(urllib.request.HTTPRedirectHandler):
    """One observed Canvas 302, exact versioned filename and CDN; no others."""
    def __init__(self):
        super().__init__()
        self._used = False

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if self._used or req is None or code != 302 or req.get_method() != "GET" or req.data is not None:
            return None
        filename = req.full_url.removeprefix(CANVAS_BASE)
        known = {"dreamina-canvas_" + name for name in HASHES["canvas"]}
        if (filename not in known or req.full_url != CANVAS_BASE + filename
                or newurl != CANVAS_CDN_BASE + filename):
            return None
        self._used = True
        # Public artifact GET only; never forward caller credentials, cookies,
        # custom headers, query parameters or a POST body across the redirect.
        return urllib.request.Request(newurl, method="GET", headers={
            "Accept": "application/octet-stream", "User-Agent": "jimeng-mcp-installer/1"})


def _download(url: str, path: Path, expected: str) -> int:
    opener = urllib.request.build_opener(_PinnedDownloadRedirect())
    request = urllib.request.Request(url, headers={"Accept": "application/octet-stream", "User-Agent": "jimeng-mcp-installer/1"})
    digest, total = hashlib.sha256(), 0
    try:
        with opener.open(request, timeout=45) as response, path.open("wb") as target:
            if response.status != 200:
                raise OnboardingError("DOWNLOAD_REJECTED", "官方分发地址未返回成功响应。")
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_DOWNLOAD:
                    raise OnboardingError("DOWNLOAD_TOO_LARGE", "官方CLI下载超过固定大小限制。")
                digest.update(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
    except (urllib.error.URLError, OSError):
        raise OnboardingError("DOWNLOAD_FAILED", "官方CLI下载失败；未执行安装脚本或安装未校验文件。") from None
    if not total or digest.hexdigest() != expected:
        raise OnboardingError("CHECKSUM_MISMATCH", "官方CLI下载字节与固定SHA-256不一致，已拒绝安装；需要核实上游版本后更新校验值。")
    return total


def _install_one(provider: str, platform_name: str, replace: bool) -> dict:
    directory = _private_dir(_root() / "bin")
    target = directory / BIN_NAMES[provider]
    expected = HASHES[provider][platform_name]
    if os.path.lexists(target):
        if target.is_symlink() or not target.is_file():
            raise OnboardingError("BINARY_PATH_INVALID", "安装目标不能是符号链接或特殊文件。")
        with target.open("rb") as source:
            existing_hash = hashlib.file_digest(source, "sha256").hexdigest()
        if existing_hash == expected and os.access(target, os.X_OK):
            return {"provider": provider, "platform": platform_name, "path": str(target), "sha256": expected, "alreadyInstalled": True}
        if not replace:
            raise OnboardingError("CLI_ALREADY_EXISTS", "私有bin已有不同文件；确认替换后使用--replace。")
    filename = ("dreamina-canvas_" if provider == "canvas" else "dreamina_cli_") + platform_name
    url = (CANVAS_BASE if provider == "canvas" else PLUGIN_BASE) + filename
    fd, name = tempfile.mkstemp(prefix=".cli-download-", dir=directory)
    os.close(fd)
    temporary = Path(name)
    try:
        size = _download(url, temporary, expected)
        temporary.chmod(0o700)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    os.environ[BIN_ENV[provider]] = str(target)
    return {"provider": provider, "platform": platform_name, "path": str(target), "sha256": expected,
            "version": CANVAS_VERSION if provider == "canvas" else PLUGIN_VERSION,
            "bytes": size, "source": url, "installerScriptExecuted": False}


def _doctor(check_auth: bool) -> int:
    current = _platform()
    root = _root()
    checks, ready = {}, True
    for provider in ("canvas", "plugin"):
        try:
            binary = _binary(provider)
            process = _invoke(provider, ["version"])
            data = _json_object(process.stdout)
            if provider == "canvas":
                data = _canvas_data(process)
            version = data.get("version")
            if process.returncode != 0 or not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", version):
                raise OnboardingError("CLI_VERSION_UNVERIFIED", "官方CLI版本输出未通过固定格式校验。")
            expected_version = CANVAS_VERSION if provider == "canvas" else PLUGIN_VERSION
            checks[provider] = {"available": True, "path": str(binary), "version": version,
                                "testedVersion": expected_version, "matchesTestedVersion": version == expected_version}
            ready = ready and version == expected_version
            if check_auth:
                checks[provider]["auth"] = _status(provider)
                ready = ready and checks[provider]["auth"]["loggedIn"]
        except OnboardingError as exc:
            checks[provider] = {"available": False, "error": exc.code}
            ready = False
    try:
        web = _status("web")
    except OnboardingError as exc:
        web = {"provider": "web", "configured": False, "error": exc.code, "serverLoginVerified": False}
    _emit({"ready": ready, "platform": current, "dataDirectory": str(root), "privateDirectoryMode": "0700",
           "cli": checks, "optionalWebSession": web, "authChecked": check_auth,
           "generationSubmitted": False, "browserOpened": False})
    return 0 if ready else 1


def run(args) -> int:
    try:
        _platform()
        _root()
        if args.command == "install-cli":
            detected = _platform()
            chosen = args.platform or detected
            if chosen != detected:
                raise OnboardingError("PLATFORM_MISMATCH", "指定平台与当前系统不一致，拒绝安装不可运行的二进制。")
            providers = ("canvas", "plugin") if args.provider == "all" else (args.provider,)
            installed = []
            for provider in providers:
                installed.append(_install_one(provider, chosen, args.replace))
            _emit({"installed": installed, "loginRequired": True, "homeOrShellConfigurationModified": False})
        elif args.command == "doctor":
            return _doctor(args.check_auth)
        elif args.command == "auth":
            action = args.auth_action
            if action == "login":
                _login(args.provider)
            elif action in ("complete", "wait"):
                _complete(args.provider, args.timeout)
            elif action == "logout":
                _logout(args.provider)
            elif action == "import-web":
                _import_web(args.file, args.replace)
            elif action == "status":
                providers = ("canvas", "plugin", "web") if args.provider == "all" else (args.provider,)
                states = []
                for provider in providers:
                    try:
                        states.append(_status(provider))
                    except OnboardingError as exc:
                        states.append({"provider": provider, "status": "unavailable", "error": exc.code})
                _emit({"authentication": states, "credentialsPrinted": False})
            else:
                raise OnboardingError("COMMAND_INVALID", "不支持的认证操作。")
        else:
            raise OnboardingError("COMMAND_INVALID", "不支持的本地引导命令。")
        return 0
    except OnboardingError as exc:
        print(json.dumps({"error": exc.code, "message": exc.message}, ensure_ascii=False), file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, KeyError, ImportError, RuntimeError):
        print(json.dumps({"error": "LOCAL_SETUP_FAILED", "message": "本地引导未完成；请检查私有目录权限、官方CLI与依赖。认证内容未输出。"}, ensure_ascii=False), file=sys.stderr)
        return 1
