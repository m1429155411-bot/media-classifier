# 媒体分类工具

一个完全在本机运行的图片 / 视频分类工具。界面是本地 HTML，后端是**零第三方依赖**的 Python 标准库服务（只监听 `127.0.0.1`），可打包成**免启动服务、无命令行黑框的独立 exe**（WebView2 独立窗口）。

GitHub 仓库：<https://github.com/m1429155411-bot/media-classifier>

---

## 一、功能概览

| 板块 | 分类方式 |
| --- | --- |
| 图片 | 按分辨率、文件格式、横竖方向、像素档位、修改月份、文件大小、文件名称、自定义规则 |
| 视频 | 按清晰度、分辨率、时长、编码、文件格式、方向、像素档位、修改月份、文件大小、文件名称、自定义规则 |

- **按文件名称 → 按自定义内容**：输入文件名里重复出现的内容（支持 `内容 => 目录名`），自上而下首个命中生效。`都没包含时归入` 留空时，未匹配的文件**留在原文件夹不动**；填写名称才统一归入该目录。
- **自定义规则**：按尺寸 / 时长 / 编码 / 体积等条件组合，图片与视频规则各自独立保存。
- **执行选项**：复制（默认，保留原文件）或移动；重名时自动重命名 / 跳过 / 覆盖；可跳过已存在的相同文件。
- **拖放**：把文件夹直接拖到界面即可设为源文件夹。
- **兼容引擎**：可改为调用原程序 `图片分类.exe`（按分辨率复制到 `分类/<宽>x<高>/`）。
- **主题**：右上角切换浅色 / 深色，选择会持久化。

图片支持 `.png .jpg .jpeg .bmp .gif .webp .tif .tiff`；视频支持 `.mp4 .mov .m4v .avi .mkv .webm .flv .wmv .mpg .mpeg .ts .mts .m2ts .3gp .vob .rmvb`。视频元数据由标准库解析容器头，不依赖 ffprobe。

---

## 二、发布方式：三种交付形态

### 形态 A · GitHub Release 的独立 exe（面向使用者，推荐）

发布地址：<https://github.com/m1429155411-bot/media-classifier/releases/tag/v1.0>

1. 下载附件 `media-classifier.exe`（约 13.1 MB），可重命名为 `图片分类工具.exe`。
2. 双击运行，无需安装 Python、无需启动任何服务、不会弹出命令行窗口。
3. 程序在进程内部启动临时服务（优先 `127.0.0.1:8766`），并打开独立应用窗口；关闭窗口或点击界面「退出程序」后，服务与进程一并结束。

窗口优先级：**WebView2 独立窗口** → QQBrowser / Edge 的 `--app` 独立窗口 → 系统默认浏览器。Windows 10/11 通常已自带 WebView2 Runtime。

> 附件名使用 ASCII 是为了让下载链接干净且避免 GitHub 对中文资产名的冲突判定；下载后改名即可。
> 校验：`SHA256 c400fd29c6230660494c16fa7775de6682ac699a37d3500b0327ff7411455028`。

### 形态 B · 源码运行（无需打包）

要求 Python 3.9+，仅标准库，不需要 pip 安装任何依赖。

```bash
python server.py                 # 自动打开 http://127.0.0.1:8765/
IMGCLASS_NO_BROWSER=1 python server.py   # 不自动打开浏览器，手动访问该地址
```

Windows 也可直接双击 `启动.bat`，脚本会依次查找 PATH 中的 `python` 与 `py` 启动器，不含任何硬编码的本机路径。

### 形态 C · 自行打包独立 exe

需要额外安装（仅打包 / 窗口依赖，核心分类逻辑仍不依赖它们）：

```bash
pip install pyinstaller pywebview pythonnet clr-loader bottle proxy_tools
```

在本目录执行（Windows Git Bash / MSYS 中 `--add-data` 用 `;` 分隔；Linux/macOS 改为 `:`）：

```bash
python -m PyInstaller --noconfirm --clean --onefile --noconsole --icon app.ico --name "图片分类工具" --add-data "index.html;." --collect-all webview --collect-all clr_loader --collect-all pythonnet --collect-all bottle --collect-all proxy_tools --hidden-import=clr --hidden-import=webview.platforms.edgechromium --hidden-import=webview.platforms.winforms app.py
```

产物为 `dist/图片分类工具.exe`。`app.py` 把 `index.html` 从解包目录读入并启动内部服务；`build/`、`dist/`、`*.spec`、`__pycache__` 均由 `.gitignore` 排除，**不进仓库**。

---

## 三、维护者：如何发布到 GitHub

### 常规流程（网络正常时）

```bash
git init -b main
git add . && git commit -m "feat: ..."
gh repo create media-classifier --public --source=. --remote=origin --push \
  --description "图片/视频分类工具"
gh release create v1.0 dist/图片分类工具.exe --title "v1.0" --notes "..."
```

### 代理受限时的替代通道（本项目实际采用）

本机 `git push` 经过代理时返回 `CONNECT tunnel failed, 502`（GitHub git 传输协议不通），但 `gh api` 的 REST 通道正常。此时改用 GitHub 数据接口写入提交：

1. **空仓库需先激活**：完全空的仓库直接调用 `/git/blobs` 会返回 `409 Git Repository is empty`。先用内容接口创建一个已存在的文件（本项目用 `.gitignore`）：
   `PUT /repos/{owner}/{repo}/contents/.gitignore`，body 为 `{"message":"...","content":"<base64>","branch":"main"}`。
2. `POST /git/blobs`：逐个上传文件的 base64 内容，GitHub 按内容计算 sha，与本地 `git cat-file blob` 的 sha 一致。
3. `POST /git/trees`：提交完整文件列表（path / mode / type / sha），得到 tree sha。
4. `POST /git/commits`：带 `tree`、`message`、`author`、`committer`；有历史时还需 `parents`。复用本地提交里的作者、时间戳和说明，可让**远端 commit sha 与本地完全相同**。
5. `PATCH /git/refs/heads/main`：把分支指向新提交（`force: true`）。
6. `git update-ref refs/remotes/origin/main <sha>`：让本地仓库与远端对齐，无需 fetch。

`gh api` 的请求体建议写入临时 JSON 文件后用 `--input <file>` 传入，避免长内容和中文文件名触发命令行长度 / 编码问题。

> 说明：以后在这台机器上直接 `git push` / `git clone` 仍可能受同一代理限制，可优先使用 `gh api`，或切换到可正常访问 GitHub git 端点的网络环境。

### Release 附件

```bash
gh release create v1.0 dist/media-classifier.exe --title "媒体分类工具 v1.0" --notes "..."
gh release upload v1.0 dist/media-classifier.exe --clobber
```

注意：`gh release create` 处理中文路径时可能把资产名回退成 `default.exe`，且中文资产名在服务端可能报 `already_exists`。可先将 exe 复制为 ASCII 文件名再上传；如需中文名，可直接 `POST https://uploads.github.com/repos/{owner}/{repo}/releases/{id}/assets?name=<urlencoded>`，body 为文件二进制，头带 `Authorization: Bearer <gh auth token>` 与 `Content-Type: application/octet-stream`。

---

## 四、目录结构

```
├── index.html      # 界面（暗/浅主题、双板块、拖放、进度与日志）
├── server.py       # 本地服务与分类引擎（标准库）
├── app.py          # 独立 exe 入口：内部服务 + WebView2 独立窗口
├── 启动.bat        # 源码模式的一键启动（Windows）
├── app.ico         # exe 图标
├── .gitignore      # 排除 dist/、build/、*.spec、__pycache__ 等
└── README.md
```

`dist/` 只在本机打包后存在，不纳入版本库。

---

## 五、数据与配置

配置保存在 `%APPDATA%\ImageClassifier\`：

| 文件 | 内容 |
| --- | --- |
| `rules.json` | 自定义规则，图片 / 视频各一套 |
| `name_keywords.json` | 文件名关键词与「都没包含时归入」，图片 / 视频各一套 |
| `theme.json` | 界面主题（`dark` / `light`），同时用于 exe 启动时的窗口背景色 |

配置只写本机，不随程序上传；分类结果输出到源文件夹下可配置的输出目录（默认 `分类`）。

---

## 六、注意事项

- 默认**复制**而非移动；切换到「移动」会移除原文件，执行前建议先「扫描预览」确认分组。
- 服务只监听 `127.0.0.1`，不对外暴露；界面中的文件夹选择通过系统原生对话框完成。
- 视频元数据解析失败时会自动回退 Windows Shell 媒体属性，仍无法识别的文件归入可配置的兜底目录。
- 原程序 `图片分类.exe` 作为「兼容引擎」可被调用，但本仓库不包含该二进制文件。
