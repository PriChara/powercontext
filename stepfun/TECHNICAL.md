# StepFun 技术说明

浏览器通过 Transformers.js 3.8.1 运行 Xenova/all-MiniLM-L6-v2（固定提交 `751bff37182d3f1213fa05d7196b954e230abad9`），对文档和查询分别做 mean pooling 与 L2 归一化，输出 384 维向量。默认探测 WebGPU 的 `shader-f16`，用 FP16 ONNX 权重并试跑；探测或初始化失败时用 WASM 和 INT8 权重。另保留 WASM FP32 基线。量化文件来自模型仓库同一提交的预转换 ONNX 文件。 [模型文件](https://huggingface.co/Xenova/all-MiniLM-L6-v2/tree/751bff37182d3f1213fa05d7196b954e230abad9/onnx)

每个知识库和精度各用一个 seekdb 集合。seekdb-js 1.4.0 保存段落原文、标题、段号、稳定段落 ID 和向量，使用 `beng` 全文索引与余弦距离 HNSW。写入分批 upsert，最后一批等待 `refresh_index()` 后再反馈成功。查询通过 seekdb-js `hybridSearch` 由数据库融合两路候选并返回 RRF 排名。 [SDK 示例](https://github.com/oceanbase/seekdb-js/blob/main/examples/complete-example.ts)

同一台 Windows 电脑，i9-14900HX、RTX 4060 Laptop、无头 Edge 157，seekdb 1.4.0.0（`ae55a18546f0f392b2c8e56d713ee1af9326820d`，容器 4 vCPU / 6 GiB 上限）。使用内置 12 段、8 个单相关文档标注查询，每种配置 3 轮。体积含 ONNX 权重及同一套 tokenizer/config，按实际文件字节计，MB 为十进制。

| 配置 | 文件体积 / 压缩比 | Recall@5 / nDCG@5 | 权重读取 / 首次可用（ms） | 12 段逐条 / 批量 8（ms） |
| --- | ---: | ---: | ---: | ---: |
| WASM FP32 | 91.10 MB / 1.00× | 100% / 0.938 | 2336.3 / 3937.6 | 436.0 / 168.4 |
| WASM INT8 | 23.69 MB / 3.85× | 100% / 0.938 | 538.3 / 680.3 | 120.5 / 116.2 |
| WebGPU FP16 | 46.01 MB / 1.98× | 100% / 0.938 | 1054.5 / 1604.9 | 82.4 / 13.6 |

| 配置 | 写入 / 刷新（ms） | 首次查询（ms） | 后续向量化 / 数据库 / 至结果呈现（ms） |
| --- | ---: | ---: | ---: |
| WASM FP32 | 48.2 / 509.9 | 50.7 | 18.3 / 4.2 / 24.5 |
| WASM INT8 | 43.4 / 604.5 | 32.5 | 4.9 / 3.9 / 10.4 |
| WebGPU FP16 | 42.5 / 603.7 | 23.9 | 7.2 / 4.7 / 14.3 |

批处理按三轮中位数统计，后续查询取除首条外 23 次的中位数。每种配置的首次可用时间包含模型读取、初始化与一次预热，权重从本机 HTTP 服务读取。FP32 排在首位，首次计时包含公共运行时加载。端到端计时到结果插入 DOM 为止。这个小样本三种配置排名一致，不能外推到更大或多语种语料。

生产构建已通过示例导入、检索、增量即查、同文件重导和切库隔离。实际 Edge 默认选中 WebGPU FP16。在独立浏览器会话屏蔽 WebGPU API 后，默认模式自动选择 WASM INT8，导入和检索通过。页面按钮可生成同格式的 [原始实测记录](benchmark.json)。
