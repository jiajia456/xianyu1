# 通用版（兼容 Win7）：怎么打、怎么验、出问题怎么查

> 最后更新：2026-11-25 16:03
> 一句话：**同一套源码，换 Python 3.8 工具链重打**，一个包跑遍 Win7 SP1 / 8 / 10 / 11。
> 成品名**不加后缀**，就叫「我在峡谷打僵尸_v1.92」；源码逻辑一行没改。

## 发放策略（2026-11-25 用户定）

- **只发这一版**：Win7 机器、Win10 机器、Win11 机器都用同一个文件夹，不再按系统分两份。
- 用 Python 3.13 打的回退包产物带 `-Win10` 后缀，**只作回退**，不再对外发新版。
- 版本号：发布版每打一次自动 +1（v1.91 → v1.92 → …，写回 `app_gui.py` 的 `APP_VER`）；
  临时重打不想推号时加 `KK_NO_BUMP=1`。

---

## 0. 为什么必须换工具链

| 组件 | 普通版 | Win7 版 | 为什么 |
|---|---|---|---|
| Python | 3.13 | **3.8.10** | 3.9 起官方不再支持 Win7；3.8.10 是最后一个支持 Win7 的版本 |
| PyInstaller | 6.x | **5.13.2** | 6.x 的 bootloader 要求 Win8+，打出来的 exe 在 Win7 直接不启动 |
| 产物布局 | `_internal/` 子目录 | **依赖平铺在 exe 同级** | PyInstaller 6 才引入 `_internal`，5.x 没有 |

其余依赖按「最后支持 py3.8 + Win7」锁版，见 `tools/requirements-win7.txt`。

---

## 1. 工具链（本机已装好）

```
py38 = C:\Users\Administrator\WorkBuddy\2026-11-25-13-13-10\win7build\py38\python.exe
```

- 没进 PATH，**不会干扰**日常用的 Python 3.13
- 依赖：`py38\python.exe -m pip list` 可查（numpy 1.24.4 / opencv 4.5.5.64 /
  PyQt5 5.15.9 / Pillow 10.4.0 / pywin32 306 / psutil 5.9.8 / PyInstaller 5.13.2）
- 重装/换机器时按 `tools/requirements-win7.txt` 装一遍即可

---

## 2. 怎么打

```bash
cd kk_auto
KK_TARGET=win7 \
KK_BUILD_DIR="C:/Users/Administrator/AppData/Local/Temp/kkbuild7" \
KK_DIST_ROOT="C:/Users/Administrator/AppData/Local/Temp/kkdist7" \
"<py38>/python.exe" build_exe.py --zip
```

- `KK_TARGET` 可省略：用 3.8 解释器跑会自动判成 `win7`，用 3.13 跑就是回退版（`-Win10`）
- `KK_BUILD_DIR` / `KK_DIST_ROOT` 指到临时盘：**宿主机对项目目录里已存在的文件会拒绝覆盖**
- 打完把 `kkdist\我在峡谷打僵尸_v1.9X\` 和同名 zip 拷回 `dist/` 或桌面

版本号规则：每打一次发布包，`app_gui.py` 的 `APP_VER` 自动 +1（v1.91 → v1.92 → …），
成品名和界面标题都取这个号。临时重打不想推号就加 `KK_NO_BUMP=1`
（比如这次 v1.92 第一遍因脚本 bug 失败、第二遍重打用的就是 `KK_NO_BUMP=1`）。

---

## 3. 四道闸（`build_exe.py` 自动跑，任一不过就非 0 退出）

1. **preflight**：解释器必须 3.8、PyInstaller 必须 5.x、numpy 不能是 2.x
2. **core 齐全**：✅ 26 个必需脚本 + 5 张模板 + 11 个配置
3. **依赖自检**：拿成品 exe 真跑 `core/_envcheck.py`，`"bad": []` 才算过
4. **界面自检**：成品 exe 渲染一张截图（验证瘦身没删坏 Qt）
5. **PE 静态兼容闸**：65 个二进制逐个查 PE 头 —— 操作系统/子系统版本必须 ≤ 6.1、
   导入表里不能出现 Win8+ 才有的 DLL/函数（`WaitOnAddress`、`GetSystemTimePreciseAsFileTime`、
   `api-ms-win-core-path-l1-1-0.dll` 等）

瘦身清单统一按 PyInstaller 6 的 `_internal/xxx` 写，用 `_layout_fix()` 换算到 5.x 的平铺布局
（这个坑踩过：清单写死 `_internal` 前缀时 Win7 版一个文件都没删掉，成品 229MB）。

---

## 4. Win7 交付物里多了什么

| 文件 | 作用 |
|---|---|
| `启动_自动安装.exe` | 原生 Windows 启动器，调用同目录的安装脚本检查 VC++ 运行库；缺少时从微软下载兼容 Win7 的 2015 x64 版，验证签名后安装并启动主程序；安装可能要求授权或重启 |
| `先运行我_自检.bat` | 一键跑 `core/_envcheck.py`，把 `envcheck.json` 用记事本打开；`"bad": []` = 依赖全齐 |
| `我在峡谷打僵尸_全静默版.exe` | 随包附带的无界面版（沿用 v1.91 那个，用户 2026-11-25 要求带上）。⚠ **Python 3.13 单文件包，Win7 起不来**（`python313.dll` 静态依赖 `api-ms-win-core-path-l1-1-0.dll`）；Win8/10/11 正常。源件放 `tools/`，`build_exe.py` 的 `EXTRA_TOOLS` 自动拷进成品 |

---

## 5. 真机排查清单（Win7 上起不来时按顺序看）

1. **系统**：必须 Win7 **SP1 64 位**。32 位 Win7 不行（cv2 / Qt 只有 64 位）。
2. **运行 `启动_自动安装.exe` 自动检查并安装运行库**。若安装要求重启，重启后再运行。典型症状：
   - 「无法启动此程序，因为计算机中丢失 api-ms-win-crt-runtime-l1-1-0.dll」
   - 「缺少 MSVCP140.dll / VCRUNTIME140.dll」
3. **还报缺 DLL** → 装补丁 `KB2999226`（UCRT）和 `KB2533623`（`SetDefaultDllDirectories`，
   Python 3.8 需要）。这两条通常已被 VC redist / Windows Update 带上。
4. **跑「先运行我_自检.bat」**：
   - `"bad": []` → 依赖齐，问题在别处（看 `core/logs/gui_boot.log`）
   - `"bad": ["numpy"]` / `"cv2"` → 99% 是运行库没装或没重启
5. **界面能开、点击没反应** → 右键「以管理员身份运行」（模拟按键 / 全局热键需要提权）。
6. **截图全黑 / 识别不到** → Win7 关了 Aero 时 `PrintWindow` 可能返回黑图；
   程序优先用屏幕抓取，一般不受影响；若开了「后台抓图」请改回默认。
7. **中文路径**：项目目录可以带中文（已用 imencode+tofile 绕开 `cv2.imwrite` 的限制）。

---

## 6. 系统识别：Win7 / Win10 自动分档（2026-11-25 新增）

点「开始运行」时先判定本机系统，界面日志打两行，并写 `core\logs\sysenv.json`：

```
🖥 本机系统：Windows 7 Service Pack 1（6.1.7601，AMD64）｜屏幕 1920x1080｜缩放 100%｜管理员
   按「win7」档运行：悬停时长 ×1.25、等窗口前台 ×1.2、抓图方式 auto
```

| 差异点 | Win7 | Win10/11 | 落点 |
|---|---|---|---|
| DPI 感知 | 只能 system-aware（无 shcore） | per-monitor | 已有 try/except，识别后写日志 |
| 后台抓图 | 关 Aero 时 PrintWindow 常黑图 | 一般可用 | `Capture` 读 `capture_mode`，可强制 screen |
| 鼠标悬停 | 老机器慢半拍，0.8s 常不认 | 0.8s 够 | `hover × 1.25` |
| 切前台等待 | 要更久 | 正常 | `wait × 1.2` |
| 权限 | 非管理员时模拟按键常无效 | 同样，但提示更早 | 日志告警 |

调参：`core\configs\os_profile.json`（改完重新点「开始运行」生效，**不用重启**）；
现场救急可在根上写 `"_force_capture_mode": "screen"` 强制抓图方式。
`sysenv.py` 的 `classify()` 是纯函数（Win11 也报 10.0，靠 build≥22000 区分），
离线测试 `test_sysenv.py` 覆盖映射/覆盖优先级/非法值/落盘。

---

## 7. 调试开关：真机出问题怎么取证（2026-11-25 新增）


成品界面最下面一排多了两个按钮，专门给「换台机器就点不中 / 认不出」用。

| 按钮 | 干什么 | 产物 |
|---|---|---|
| **调试：关 → 开** | 运行全程记录每次找图与点击 | `core\logs\debug\<时间>\events.jsonl` + 点击前带红十字的截图（滚动保留最近 40 张 / 28MB） |
| **抓现场** | 立刻存一份静态证据（约 5~10 秒） | `环境.json` / `窗口.json` / `屏幕.png` / 窗口客户区截图 / `模板打分.json` |

**调试记录里有什么**：每次找图一条 `find_hit` / `find_miss`（模板名、阈值、实际最高分、
窗口客户区、缩放系数 k）；每次点击一条 `click`（客户区坐标、屏幕坐标、k）。

**判读方法**（这是本次加它的目的）：

| 现象 | 看哪里 | 结论 |
|---|---|---|
| 明明在却认不出 | `find_miss` 的 score | 0.75~0.85 → 阈值偏紧（渲染/取色差异，可降阈值或重截模板）；<0.6 → 画面不是预期界面 |
| 点了没反应 | `click` 的 client / k / screen_xy | 尺寸或 k 不对 → 坐标换算/窗口绑错；全对 → 弹窗遮挡或没提权 |
| 不知道绑了哪个窗 | 抓现场的 `窗口.json` + `环境.json` | 对比 rect 与预期（1690x921 / 1332x945） |
| 图色是否异常 | 抓现场的 `模板打分.json` | 分数整体掉档 = 渲染差异；全 <0.6 = 界面不对 |

⚠ 要记录子流程（通关/钓鱼/签到），必须在**点「开始运行」之前**打开调试——子进程靠
环境变量 `KK_DEBUG` 继承，跑起来之后再开会漏掉当前阶段。

实现：`dbgprobe.py`（独立模块）+ `game_auto.Bot.find/click/click_hover` 三个钩子，
`logpack` 会把 `logs\debug` 整个打进 zip（单独「调试」文件夹，120 个 / 40MB 上限）。
离线测试 `test_dbgprobe.py`（6 组）。

---

## 8. 已知风险（没在真机验证过的部分）


本机是 Windows 10（1607），**没有 Win7 真机**。已做的验证是：

- 29 个离线测试在 Python 3.8 下全绿（这部分和系统无关，验证的是 3.8 与 3.13 的差异）
- 成品在 Win10 上跑通依赖自检 + 界面自检
- PE 静态扫描 65 个二进制全部 ≤6.1、无 Win8+ 导入（这是「能不能加载」最硬的那一半）

**没覆盖的**：Win7 图形栈（Aero 开关、GDI 抓屏细节）、Win7 的 UIPI 提权差异、
老显卡驱动下的 Qt 渲染。这些必须真机跑一局才知道。

如果 Win7 上 `numpy` 报 `DLL load failed while importing _multiarray_umath`：
先确认 VC redist 装好并重启；仍然失败说明 1.24.4 在这台机器上确实起不来，
把 `tools/requirements-win7.txt` 里 numpy 降到 `1.20.3` 重装重打即可
（1.20.3 是社区验证过在 Win7 上最稳的一版，代价是不支持部分新 API —— 本项目没用到）。
