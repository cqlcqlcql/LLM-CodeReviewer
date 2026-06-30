# 第一阶段：最小可用版本学习指南

这一阶段不要训练模型，也不要急着做复杂前端。目标只有一个：用户提交代码，后端读到代码，LLM 或 mock 生成评审 JSON，前端展示结果。

## 1. 项目结构

```text
code-reviewer-mvp/
  app/
    main.py                 # FastAPI 入口，定义接口
    schemas.py              # 请求和响应的数据结构
    settings.py             # 环境变量配置
    services/
      code_loader.py        # 从仓库路径读取代码
      llm.py                # mock/DeepSeek 评审器
  frontend/
    index.html              # 最小前端页面
  tests/
    test_api.py             # 接口测试
  requirements.txt          # Python 依赖
```

## 2. FastAPI 后端做了什么

核心接口是：

```text
POST /api/review
```

它支持两种 JSON 输入：

```json
{
  "language": "python",
  "code": "def add(a,b): return a-b"
}
```

或：

```json
{
  "language": "python",
  "repository_path": "/path/to/local/repo"
}
```

文件上传走另一个接口：

```text
POST /api/review/file
```

这是因为上传文件需要 `multipart/form-data`，和普通 JSON 请求不是同一种请求体。

## 3. DeepSeek 调用方式

DeepSeek 提供 OpenAI SDK 兼容接口，所以后端使用 `openai` 这个 Python SDK，但把 `base_url` 改成 DeepSeek：

```python
client = AsyncOpenAI(
    api_key=settings.deepseek_api_key,
    base_url="https://api.deepseek.com",
)
```

第一版默认使用：

```env
LLM_PROVIDER=mock
```

这样没有 API Key 也能演示完整流程。要切换到 DeepSeek：

```env
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=你的 DeepSeek Key
DEEPSEEK_MODEL=deepseek-v4-flash
```

## 4. JSON Schema 约束输出

LLM 最大的问题是输出不稳定。第一版用了两层约束：

1. 请求 LLM 时使用 `response_format={"type": "json_object"}`，要求模型输出 JSON。
2. 后端用 Pydantic 的 `ReviewResponse` 再验证一次结构。

如果 LLM 返回的 JSON 字段不对，后端会返回 `502`，而不是把脏数据交给前端。

## 5. 前端调用后端 API

前端只有一个文件：`frontend/index.html`。

它用浏览器原生 `fetch` 调用接口：

```js
const response = await fetch("/api/review", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ language, code }),
});
```

拿到 JSON 后，把 `summary` 和 `issues` 渲染到页面上。

## 6. 本地运行

```bash
cd code-reviewer-mvp
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

浏览器打开：

```text
http://127.0.0.1:8000
```

## 7. 测试

```bash
pytest
```

当前测试验证两件事：

- mock 模式能识别 `add` 函数执行减法的问题。
- `/api/review` 至少需要 `code` 或 `repository_path`。

## 8. Git 提交流程

第一次提交：

```bash
git status
git add .
git commit -m "Build phase 1 code review MVP"
```

如果已经关联 GitHub 远程仓库：

```bash
git remote -v
git push -u origin main
```

如果本地分支叫 `master`，可以先改成 `main`：

```bash
git branch -M main
```

## 9. 下一阶段可以做什么

第一阶段验收通过后，再加这些功能：

- 对 Python 接入 `ruff` 或 `pylint` 做静态分析。
- 把静态分析结果和代码一起喂给 LLM。
- 支持 Git diff，只评审本次改动。
- 增加历史记录和报告导出。
- 前端拆成 React 或 Vue 项目。
