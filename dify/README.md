# 月球邮局 · Dify × PowerContext

月球邮局的信柜只认印章。第一次运行时，把月兔章或星环章寄存在邮局；第二次运行时，凭回执选门。走对门会收到不同的彩蛋，走错还可以重来。

Dify Workflow 负责读取玩家的选择、调用存档服务、按条件选择寄存或取信、根据记忆判定结局。PowerContext 创建每局独立的 Scope，用 `memory/remember` 存储印章，再通过 `context/prepare` 召回。每个回执是随机生成的，只把它的哈希用作 Scope 的幂等键。工作流自身不保存跨次运行的状态。作品使用规则编排，不需要模型、外部 API Key、插件或预置数据。

## 启动和导入

1. 安装 Docker Desktop 或 Docker Engine（Linux 容器），以及支持 `!override` 的 Docker Compose（2.24.4+）。下载并解压 [Dify 1.18.0 官方发布包](https://github.com/langgenius/dify/releases/tag/1.18.0)，进入其 `docker` 目录，将 `.env.example` 复制为 `.env`。使用单独的本地评审环境。
2. 将环境变量 `MOON_POST_DIR` 设为参赛文件中本目录的绝对路径。在 Windows PowerShell 中，例如：`$env:MOON_POST_DIR = 'D:\oceanbase\ecosystem\dify'`。然后执行：

   ```powershell
   docker compose -p moonpost -f docker-compose.yaml -f "$env:MOON_POST_DIR/compose.moon-post.yml" up -d --build
   ```

   Linux/macOS 可将环境变量用 `export` 设置，再用 `$MOON_POST_DIR` 引用。首次启动需下载官方 Dify 镜像、PowerContext 固定源码提交和 Python 依赖。PowerContext 的 SQLite 数据写入 `moon_post_memory` 卷；服务只连接到 Dify 内部代理网络，代理仅额外放行 `moon-post-memory` 这个主机名。请勿把这套无独立鉴权的演示环境暴露到公网。
3. 打开 `http://localhost:18376/install`，按页面创建本地管理员。进入工作室，选择 **导入 DSL 文件**，上传 `moon-post.yml`，打开 **运行**。作品使用的环境变量 `MOON_POST_PC_URL` 默认是 `http://moon-post-memory:8000`；使用上面的 Compose 无需改动。若改用其他 PowerContext 部署，更新该变量并让 Dify 的 HTTP 节点能够访问新地址。

## 验证样例

| 顺序 | 操作 | 预期 |
| --- | --- | --- |
| 1 | 运行「开局：寄存月兔章」，回执留空 | 出现脚印线索和新的 32 位回执；写入节点返回真实记忆记录 |
| 2 | 发起一次新的运行，填上一步回执，选「回来：走光环门」 | `retry`，邮差提示换一扇门 |
| 3 | 再发起一次新的运行，用同一回执，选「回来：走脚印门」 | `open`，月兔签收；读取节点的返回是 `ready`，包含寄存的月兔章标记 |
| 4 | 「开局：寄存星环章」获得另一张回执；用它选「回来：走光环门」 | `open`，收到星图；两张回执对应不同 Scope |
| 5 | 使用不存在的回执，例如 `00000000000000000000000000000000` | `missing`，邮差找不到印章 |

重新开局会生成新回执，不影响旧存档。可在 Dify 的运行详情打开 **将印章写入 PowerContext** 和 **从 PowerContext 找回印章** 两个节点，检查 HTTP 响应。`verify_memory.py` 可以单独复测真实 PowerContext HTTP 接口，传入能访问该接口的 `--url`；它不代替 Dify 的导入及运行验证。

`game.py` 是嵌入工作流的 Python 逻辑。修改后，用 `python -m pip install PyYAML==6.0.3` 安装生成器依赖，再运行 `python build_workflow.py` 重新生成 DSL。

PowerContext 接口实测覆盖两种印章的写入、两局 Scope 隔离、两种正确结局、走错门以及无效回执。两次寄存均返回 HTTP 200 和记忆条目。三次有效回执查询均返回 `ready`，无效回执返回 `empty`。这些结果来自固定版本 PowerContext 的本地 HTTP 服务，可用 `verify_memory.py` 复现。
