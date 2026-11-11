# 课题交接簿（handover-book）

把一次交接的发现、证据、下一步写入 PowerContext，按当前问题召回并生成可复制的接手清单。
**仅本机运行**：只监听 `127.0.0.1`，不读取凭据，不调用任何公网 API；示例数据全部为原创虚构素材。

## 环境要求

- Node.js **22+**（`node -v` 确认）
- PowerContext 本机服务，固定源码版本 `dab240063bfcfa7917898dc44da4baadb3747bd0`，监听 `http://127.0.0.1:18379`

## 启动

首次部署可在 PowerShell 用独立环境安装固定版本的 PowerContext。已运行同一版本服务时，直接启动应用即可。

```powershell
python -m venv .pc-venv
$env:SETUPTOOLS_SCM_PRETEND_VERSION = '0.0.0+dab2400'
& .\.pc-venv\Scripts\python.exe -m pip install 'powercontext[server,cli] @ https://codeload.github.com/oceanbase/powercontext/tar.gz/dab240063bfcfa7917898dc44da4baadb3747bd0'
$env:POWERCONTEXT_HOME = Join-Path (Get-Location) '.pc-data'
$env:POWERCONTEXT_SERVER_HTTP_PORT = '18379'
& .\.pc-venv\Scripts\powercontext.exe server run
```

在第二个终端进入本目录并启动应用，无需安装 npm 依赖。

```bash
npm start
```

打开 `http://127.0.0.1:8787`。可用环境变量（也可写在 `.env`，已 gitignore）：
`HB_HOST`（只允许回环地址）、`HB_PORT`、`POWERCONTEXT_URL`、`POWERCONTEXT_TIMEOUT_MS`、`HB_DATA_DIR`。

## 示例复现

1. 首页第 1 步选「示例课题A：机械臂相机标定复现（虚构）」，点「载入示例」写入 PowerContext。
2. 第 3 步当前问题填 `重启后抓取偏差变大，下一步该做什么`，点「召回并生成清单」。
3. 期望：返回与问题相关的虚构交接，清单逐条带来源记录号 `hb-xxxxxxxxxx`。把问题换成 `串口超时 缺帧` 可定位另一条记录。换成全新课题名后，结果为空且只给出提示。
   素材来自 `examples/seed.json`，重启应用后仍可召回（回执存本机 `.data`）。

## 验收

```bash
npm run verify            # 真实调用 PowerContext + 应用，端到端 35 项
npm run verify:offline    # PowerContext 不可用时只验应用侧
```

输出原样保存到 `docs/test-output/verify-<运行编号>.txt`，当前结果 **35 PASS / 0 FAIL**。
