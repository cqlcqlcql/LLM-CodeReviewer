# Code Review MVP

基于 FastAPI + DeepSeek API 的最小可用代码评审系统。

第一阶段目标：

- 提供 `POST /api/review`
- 接收代码文本、代码文件或本地仓库路径
- 后端读取代码
- 调用 LLM 生成结构化评审意见
- 返回 JSON
- 前端展示结果

## 运行

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

打开浏览器访问：

```text
http://127.0.0.1:8000
```

默认 `LLM_PROVIDER=mock`，不需要 API Key 也能跑通闭环。

如果要调用 DeepSeek：

```env
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=你的 DeepSeek Key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
```

## API 示例

```bash
curl -X POST http://127.0.0.1:8000/api/review \
  -H "Content-Type: application/json" \
  -d '{"language":"python","code":"def add(a,b): return a-b"}'
```

返回格式：

```json
{
  "summary": "函数名与实际行为不一致",
  "issues": [
    {
      "severity": "high",
      "category": "logic_bug",
      "line": 1,
      "message": "add 函数实际执行了减法",
      "suggestion": "将 return a-b 改为 return a+b"
    }
  ]
}
```
