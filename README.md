# CSpider NL2SQL 工作台

一个前后端分离的 CSpider 浏览与 NL2SQL 结果工作台：前端使用 Vite，后端使用 FastAPI；本地 SQLite 数据库以只读方式打开。

## 启动

需要 Python 3.10+ 和 Node.js 18+。在仓库根目录启动后端。

```powershell
python -m pip install -r backend/requirements.txt
python -m uvicorn backend.main:app --reload --port 8000
```

另开一个终端：

```powershell
cd frontend
npm install
npm run dev
```

浏览器打开 `http://127.0.0.1:5173`。后端接口文档位于 `http://127.0.0.1:8000/docs`。

## 页面功能

- 开发集、验证集、测试集样本列表与问题搜索。
- 每条样本显示所属数据库、金标 SQL、生成 SQL 编辑/保存框，以及生成历史。
- 数据库浏览器展示表、字段类型、主键/外键和分页数据预览。
- 批量运行审阅页可启动固定模型的基线实验，查看正确、错误、无法判定及生成失败样本。
- 逐条展开后展示自然语言问题、金标 SQL、本轮实际 Prompt、生成 SQL、初步原因、执行证据和结果预览。
- 已保存的生成记录可通过样本详情和 `GET /api/results` 查看。
- 原始 CSpider JSON 与各数据库 SQLite 文件不被修改。NL2SQL 生成记录单独写入 `backend/storage/workbench.sqlite3`；可用 `NL2SQL_STATE_DB` 指定保存位置。
- 前端 API 跨域来源默认允许 Vite 开发/预览地址；部署到其他域名时，可通过 `NL2SQL_CORS_ORIGINS` 设置逗号分隔的允许来源。

## 金标 SQL 来源

样本详情页使用对应 JSON 记录内的 `query` 字段作为金标 SQL，保证问题、数据库和参考 SQL 来自同一条标注。仓库中的 `development_gold.sql` 与 `validation_gold.sql` 行数虽与 JSON 相同，但部分行尾 `db_id` 和同序号 JSON 记录不一致；因此不把这两份文件直接按行号挂到样本上。原始文件保持不变。

进一步按数据库与 SQL 结构对账，也不能为所有样本唯一恢复原 gold 行与具体 SQL 字面值的对应关系，因此页面不会用推测出来的配对覆盖样本内的 `query`。

## 主要 API

- `GET /api/datasets` — 三份划分与数据统计。
- `GET /api/samples?split=development` — 搜索、筛选和分页样本。
- `GET /api/samples/{split}/{sample_index}` — 样本、金标与历史生成结果。
- `POST /api/samples/{split}/{sample_index}/results` — 保存 NL2SQL 输出。
- `GET /api/databases`、`GET /api/databases/{db_id}` — 数据库清单与 schema。
- `GET /api/databases/{db_id}/tables/{table_name}/rows` — 分页读取表数据。
- `GET /api/results` — 浏览已保存的生成记录。
- `GET /api/experiment-config` — 当前模型服务配置状态与默认 Prompt。
- `POST /api/experiments`、`GET /api/experiments` — 创建批次与查看批次列表。
- `GET /api/experiments/{run_id}`、`GET /api/experiments/{run_id}/items` — 查看进度、汇总和样本结果。
- `GET /api/experiments/{run_id}/items/{item_id}`、`POST /api/experiments/{run_id}/cancel` — 查看单条审阅详情和请求停止批次。

生成结果 POST 请求示例：

```json
{
  "generated_sql": "SELECT name FROM singer LIMIT 5",
  "model": "local-model",
  "run_id": "run-001",
  "sample_id": "<GET /api/samples 返回的 sample_id>",
  "latency_ms": 128
}
```

## 批量运行与评测配置

批量实验通过后端配置的 OpenAI-compatible 服务生成 SQL。后端启动时优先读取 `backend/.env`，再读取项目根目录 `.env` 作为回退配置，向 `{NL2SQL_API_BASE_URL}/chat/completions` 发送请求；系统环境变量优先级最高，API Key 只保留在后端，不会传到浏览器。首次配置可复制 `backend/.env.example` 到 `backend/.env`，再填写服务地址、密钥和模型：

```dotenv
NL2SQL_API_BASE_URL=https://api.example.com/v1
NL2SQL_API_KEY=your_api_key
NL2SQL_MODEL=your_model_id
```

也可以通过系统环境变量覆盖 `.env` 中的值。配置后重启后端。

`NL2SQL_API_BASE_URL` 是 API 前缀，后端会在末尾追加 `/chat/completions`。批量运行页面允许填写本轮使用的模型和 system prompt；模型默认值来自 `NL2SQL_MODEL`。每条样本的结果可查看本次实际使用的 Prompt、自然语言问题、金标 SQL、生成 SQL 和初步原因，供逐条审阅。

批量运行默认最多处理所选数据划分中的 20 条样本；将数量设为 `0` 表示运行该划分的全部样本。每批最多并发生成 6 条样本，进度分别统计排队中、生成中和已处理样本。创建批次后任务在后端后台运行，可通过以下接口查看运行状态和结果：

- `POST /api/experiments` — 创建批次并返回运行标识。
- `GET /api/experiments` — 查看批次列表。
- `GET /api/experiments/{run_id}` — 查看批次状态、进度和汇总。
- `GET /api/experiments/{run_id}/items` — 查看该批次的样本结果。
- `GET /api/experiments/{run_id}/items/{item_id}` — 查看单条结果详情。

评测会在对应的 SQLite 数据库上以只读方式分别执行生成 SQL 和金标 SQL，再对比查询结果。页面展示的是**该数据库上的执行结果是否一致**；结果一致不代表两条 SQL 在所有数据库状态下都语义等价，结果不一致也需要结合问题、Prompt、Schema 和 SQL 一起审阅。SQL 字符串不同本身不能判定错误；若金标无法执行、查询超时或结果被限制截断，应标为无法比较，不能据此判为正确或错误。
