# 本地文档检索 · StepFun × seekdb

导入 UTF-8 的 `.txt`、`.md` 文件后，可按关键词或自然语言检索原文。应用内置 12 篇短文和 8 个带相关性标注的查询，开箱可复现实验。所用模型是英文 MiniLM，建议用英文文档和查询评估语义检索。

## 启动

需要 Node.js 22.12+、npm、Edge 或 Chrome，以及支持 `DBMS_HYBRID_SEARCH` 的 seekdb 1.4。已有 seekdb 时，可以直接用环境变量指定地址。下面给出 Windows PowerShell 示例。

```powershell
docker run -d --name stepfun-db -p 127.0.0.1:12881:2881 -e SEEKDB_DATABASE=test -e DATAFILE_SIZE=512M -e DATAFILE_MAXSIZE=2G -e LOG_DISK_SIZE=2G -v stepfun-db:/var/lib/seekdb oceanbase/seekdb:latest
$env:SEEKDB_HOST = '127.0.0.1'
$env:SEEKDB_PORT = '12881'
npm ci
npm run dev
```

在本目录运行 npm 命令，等待数据库完成初始化，再打开终端显示的本机地址。`SEEKDB_DATABASE`、`SEEKDB_USER`、`SEEKDB_PASSWORD` 默认为 `test`、`root`、空字符串。数据库设了密码时，通过环境变量传入。Linux 上若已安装 SDK 对应的原生绑定，可不设置 `SEEKDB_HOST`，应用会使用 `.seekdb` 目录下的嵌入式数据库。生产构建可用 `npm run build` 和 `npm run preview` 在本机验证。

模型文件默认从 Hugging Face 下载到浏览器缓存，文档和查询在浏览器中生成向量。离线部署时，将固定提交的 `config.json`、`tokenizer.json`、`tokenizer_config.json` 和 `onnx/{model,model_fp16,model_quantized}.onnx` 放到 `public/models/Xenova/all-MiniLM-L6-v2/`，启动或构建前设置 `$env:VITE_LOCAL_MODELS = '1'`。模型和运行时文件无需提交到 Git。 [模型固定提交](https://huggingface.co/Xenova/all-MiniLM-L6-v2/tree/751bff37182d3f1213fa05d7196b954e230abad9)

## 验收

1. 点击「导入示例」，查看后端和导入进度。搜索 `recover the database to a point in time`，结果应包含 `Backup recovery`。
2. 导入包含唯一词 `SeleneQuartz` 的新文本，等待显示写入完成，再搜索该词。新文档应立即出现在结果里。重新导入相同文件，段落计数应保持不变。
3. 换一个知识库名称后搜索相同内容，验证各库隔离。切换精度后需在对应集合重新导入，避免混用不同精度生成的向量。
4. 刷新页面后点击「运行性能与质量测试」，下载 JSON。三种配置分别在新建的测试库内对相同语料检索，每种配置执行 3 轮。没有支持 `shader-f16` 的 WebGPU 设备时，默认模式会显示 WASM，强制 WebGPU 的测试项记录原因。

服务端只接收浏览器算好的向量和原文。应用调用 seekdb-js `hybridSearch`，由数据库完成全文和向量的 RRF 融合。数据库启动参数参见 [seekdb 官方镜像说明](https://github.com/oceanbase/docker-images/blob/main/seekdb/README.md)。

实验环境、索引刷新方法、量化对比和分阶段延迟见 [技术说明](TECHNICAL.md)。
