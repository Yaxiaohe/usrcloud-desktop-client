# 有人云巡站客户端（Windows / macOS）

[有人云](https://www.usr.cn) 平台的第三方桌面客户端：批量巡检设备、自动截图归档、
离线/数据陈旧告警、历史数据留档。Electron 壳 + Python 后端，界面是本地网页。

> 本仓库是**干净版本**：不含任何账号、密码、登录缓存、巡检截图与历史数据，
> 也不含任何公司标识。克隆下来后需要自己添加账号。

## 一、目录说明

| 路径 | 作用 |
| --- | --- |
| `main.js` | Electron 主进程：拉起 Python 后端、加载界面、托盘/通知 |
| `webui_server.py` | Python 后端（HTTP 服务 + 有人云 API 调度） |
| `run_usrcloud.py` | 巡站截图核心逻辑（画设备卡片、打包 zip） |
| `watch_engine.py` | 实时监控引擎（离线/陈旧判定、告警去重） |
| `usr_control.py` | 设备控制（走有人云 MQTT） |
| `ui/index.html` | 界面（巡站 / 监控 / 历史 / 账号 四页） |
| `accounts.csv` | 账号表，**仓库里只有表头，账号自己填** |
| `监控规则.json` | 告警规则（仓库里 rules 为空） |
| `登录缓存/` | 登录 token 缓存，运行时生成 |
| `build/icon.ico` `build/icon.png` | 应用图标（Windows 用 ico，macOS 用 png） |

## 二、从源码运行

需要 **Python 3.11+** 和 **Node.js**（仅用于装 Electron）。

```bash
# 1) Python 依赖
pip install pillow requests paho-mqtt

# 2) Electron（国内建议走镜像）
set ELECTRON_MIRROR=https://registry.npmmirror.com/-/binary/electron/   # Windows
export ELECTRON_MIRROR=https://registry.npmmirror.com/-/binary/electron/  # macOS / Linux
npm install

# 3) 启动
npm start
```

启动后先在「账号」页添加账号：名称 / 账号（手机号）/ 密码 / 平台 ID（默认 `W3rfV`）/ 站点过滤（留空＝该账号全部设备）。

## 三、使用免安装包（推荐）

Release 里给的是**免安装版**，解压即用，不需要装 Node.js 和 Python：

| 文件 | 平台 | 怎么用 |
| --- | --- | --- |
| `usrcloud-patrol-client-Windows-x64.zip` | Windows 10/11 x64 | 解压后双击 `有人云巡站客户端.exe` |
| `usrcloud-patrol-client-macOS-arm64.zip` | Apple Silicon Mac | 解压后把 `.app` 拖进「应用程序」，首次打开**必须右键**（见下一节） |

两个包都**自带 Python 3.11 运行时**（含 pillow / requests / paho-mqtt），
不依赖系统里装的 Python。Windows 版放在 `resources/app/python/python.exe`，
macOS 版放在 `Contents/Resources/app/python/bin/python3`（到 `python3.11` 的符号链接）；
只有找不到内置解释器时才回落到系统 `python3`。

### macOS 未签名，首次打开怎么过门禁

本项目的 Mac 包**没有苹果开发者签名**（没有交 99 美元/年的开发者账号费用），
所以 macOS 的 Gatekeeper 会拦截，表现是双击后弹：

> 「有人云巡站客户端」已损坏，无法打开。您应该将它移到废纸篓。

**这不是文件损坏，也不是病毒**，只是系统不认识开发者。三种办法任选：

**办法一（推荐，最省事）**
1. 在访达里找到 `有人云巡站客户端.app`
2. **右键**（或按住 Control 点）→ 选「打开」
3. 弹窗里点「打开」

只需要做这一次，之后双击就能直接启动。

**办法二（用包内脚本）**
双击 zip 解压出来的 `解除门禁.command`，它会自动对 app 做一次去隔离属性处理。

**办法三（命令行）**
```bash
xattr -dr com.apple.quarantine "/Applications/有人云巡站客户端.app"
```

如果还想手动确认，可以看下签名状态：
```bash
spctl -a -vv "/Applications/有人云巡站客户端.app"
# 输出 "rejected" 是正常的，说明只是没签名，不是被判定为恶意软件
```

> 说明：只有从网上下载的 app 会被打上隔离属性（quarantine）。
> 自己编译的、或用 U 盘拷的，通常不会触发这个提示。

## 四、运行时数据写到哪

| 数据 | 位置 |
| --- | --- |
| 巡站截图 | 程序目录下 `截图/<批次时间戳>/<账号名>/*.png` |
| 导出压缩包 | `有人云巡站截图_<时间戳>.zip` |
| 历史数据 | `历史数据/历史数据_YYYY-MM.csv` |
| 监控记录 | `监控记录/监控记录_YYYY-MM.csv` |
| 登录缓存 | `登录缓存/` |

这些数据都不进仓库（见 `.gitignore`）。

## 五、自行打包

1. 下载对应平台的 Electron 运行时（`electron-v44.5.1-win32-x64.zip`、`electron-v44.5.1-darwin-arm64.zip`）
2. 把源码放进 Electron 的 `resources/app/`
3. Windows 版额外把 Python 运行时放到 `resources/app/python/`（主进程按 `python/python.exe` 找）
4. 改 exe / app 名与图标即可分发

## 六、许可

MIT。第三方客户端，与有人云平台官方无关。
