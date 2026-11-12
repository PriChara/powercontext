# 订单报表的规格驱动上下文

样例任务是给订单报表增加 `minTotalCents`，同时保留原来的客户和状态筛选、顺序和不改输入的行为。`sample/` 是原创合成样例。基线源码和测试固定，MiniMax 返回的代码保存为独立候选。

## 启动

使用 Python 3.11+、seekdb 1.4 和 PowerContext 固定源码提交 `dab240063bfcfa7917898dc44da4baadb3747bd0`。进入本目录后运行 `python -m pip install -r requirements.txt`。已有 seekdb 时使用现有服务。需要新建本机实例时可用 Docker，Windows 上使用 Docker 数据卷。 [seekdb 官方容器说明](https://github.com/oceanbase/docker-images/blob/main/seekdb/README.md)

```powershell
docker run -d --name minimax-sdd-db -p 127.0.0.1:12881:2881 -e SEEKDB_DATABASE=test -e DATAFILE_SIZE=512M -e DATAFILE_MAXSIZE=2G -e LOG_DISK_SIZE=2G -v minimax-sdd-db:/var/lib/seekdb oceanbase/seekdb:latest
$env:SEEKDB_PORT = '12881'
```

可用 `SEEKDB_HOST`、`SEEKDB_PORT`、`SEEKDB_DATABASE`、`SEEKDB_USER`、`SEEKDB_PASSWORD` 配置连接。默认是 `127.0.0.1:2881`、`test`、`root` 和空密码。PowerContext 可在第二个 PowerShell 窗口安装、启动。已有服务时只需设置 `POWERCONTEXT_URL`。

```powershell
python -m venv .venv
$env:SETUPTOOLS_SCM_PRETEND_VERSION = '0.0.0+dab2400'
& .\.venv\Scripts\python.exe -m pip install 'powercontext[server,cli] @ https://codeload.github.com/oceanbase/powercontext/tar.gz/dab240063bfcfa7917898dc44da4baadb3747bd0'
$env:POWERCONTEXT_HOME = Join-Path (Get-Location) 'run/powercontext-data'
$env:POWERCONTEXT_SERVER_HTTP_PORT = '18379'
& .\.venv\Scripts\powercontext.exe server run
```

## 验证样例

```powershell
python verify_baseline.py
$corpus = Get-Content run/corpus.txt
python sdd.py --corpus $corpus context
```

脚本用新 corpus 从空状态运行。预期 `context` 返回 `sample/specs/orders.md`、`sample/orders.py`、`sample/test_orders.py` 和各自 SHA-256，恢复的 PowerContext 检查点显示目标测试在基线发生断言失败、回归退出码为 0。脚本将规格副本的位数上限从 9 改到 6，重新导入并检查新内容及摘要，然后恢复原始规格。原始输出在 `run/`，再次执行会使用新的隔离 corpus，无需清空现有记录。

复验本次提交中的 MiniMax 候选代码可直接运行下列命令，预期 4 项功能测试和 3 项回归测试全部通过。基线和候选使用同一份 `sample/test_orders.py`。复验已归档代码无需另开模型会话。

```powershell
python sdd.py --candidate docs/evidence/orders.py test
```

本次实测的输入、修改前后代码、测试结果和状态记录见 [验证记录](docs/evidence/README.md)。

最后阶段需要真实 MiniMax 账户授权。将 Key 通过本地环境变量 `MINIMAX_API_KEY` 提供给进程。默认使用国内接口及 `MiniMax-M3.1-Flash`，国际账号设置 `MINIMAX_REGION=global`。若套餐支持其他 Anthropic 兼容模型，可设置 `MINIMAX_MODEL`。 [MiniMax 国内接口说明](https://platform.minimax.cn/docs/api-reference/text-anthropic-api)

```powershell
python sdd.py --corpus $corpus generate
# 先审阅 run/orders.py，确认改动仅为订单筛选。
python sdd.py --corpus $corpus verify
python sdd.py --corpus $corpus context
```

生成阶段向 MiniMax 提供真实检索结果和上次运行保存的测试状态，保存返回的候选、请求 ID、模型、用量和候选摘要。验证阶段核对原始基线与测试摘要，在候选上运行相同测试，成功后产生 `orders.patch`、`after-tests.json` 并记录第二个任务检查点。凭据由进程环境传入。原始模型思维链不会保存。

### 使用 MiniMax Code 网页版

也可使用有模型额度的 [MiniMax Code 网页版](https://agent.minimax.cn/)。完成基线验证后执行 `python sdd.py --corpus $corpus prepare-web`。把 `run/minimax-prompt.txt` 的全文发到新的 MiniMax Code 任务中。保存界面显示的模型名称和会话证据，把最终答复中的完整代码另存为 UTF-8 的 `run/web-answer.txt`。

```powershell
python sdd.py --corpus $corpus --candidate run/web-answer.txt --observed-model '界面实际显示的模型名称' import-web
# 审阅 run/orders.py 后，再执行相同测试。
python sdd.py --corpus $corpus verify
python sdd.py --corpus $corpus context
```

网页接入记录使用界面显示的模型名称、请求内容和答复摘要。保留实际会话或截图供核对，避免在截图里包含账号资料。网页版不提供 API 请求 ID 或用量时，这些字段留空。
