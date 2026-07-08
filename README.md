# LLM-CodeReviewer

LLM-CodeReviewer 是一个本地代码评审 MVP。它用 FastAPI 提供后端 API，用一个静态 HTML 页面提供可操作界面，并把 Git diff、静态分析、自动化测试和 LLM 评审合并成一份结构化 review 结果。

这个项目的核心目标不是训练模型，而是验证一条可落地的代码评审链路：

1. 选择一段代码、一个文件，或一个本地项目目录。
2. 后端收集代码上下文，优先读取 `git diff <base_branch>...HEAD`。
3. 对项目运行本地静态分析工具，必要时运行自动化测试。
4. 把 diff、静态工具结果、测试结果交给 mock reviewer 或 DeepSeek reviewer 汇总。
5. 前端展示问题列表、diff 摘要、测试诊断、历史记录和 Markdown 报告。

更详细的项目复盘、模型说明、技术栈、工具列表和 PPT 素材见 [docs/project-overview.md](docs/project-overview.md)。

## 当前能力

- `POST /api/review`：评审代码文本或本地项目目录。
- `POST /api/review/file`：上传单个代码文件并评审。
- `POST /api/diff`：读取本地 Git 仓库的统一 diff，并返回文件级摘要。
- `POST /api/test`：自动识别并运行项目测试。
- 支持 configurable base branch，例如 `main`、`master`、`develop`。
- 支持 Python、JavaScript/TypeScript、Java 的测试命令识别。
- 支持 Python 静态分析工具 `ruff`、`mypy`、`bandit`，以及前端项目的 `npm run lint`。
- 默认使用 `LLM_PROVIDER=mock`，没有 API Key 也能跑通完整流程。
- 可切换 DeepSeek，使用 OpenAI SDK 兼容接口调用真实模型。

## 项目结构

```text
code-reviewer-mvp/
  app/
    main.py                  # FastAPI 入口和 API 编排
    schemas.py               # 请求、响应和问题模型
    settings.py              # .env 配置读取
    services/
      code_loader.py         # 代码读取与长度裁剪
      diff_loader.py         # Git diff 读取、解析和格式化
      llm.py                 # mock / DeepSeek reviewer
      static_analysis.py     # ruff、mypy、bandit、npm lint
      test_runner.py         # pytest、npm test、mvn/gradle test
  frontend/
    index.html               # 静态前端页面
  docs/
    phase1-guide.md
    phase2-diff-review.md
    phase3-automated-tests.md
    phase4-static-analysis.md
    project-overview.md
  tests/
    test_api.py              # 端到端 API 行为测试
  requirements.txt
  pytest.ini
```

## 本地运行

Windows PowerShell:

```powershell
cd D:\profile\code-reviewer-mvp
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload
```

如果还没有虚拟环境：

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
uvicorn app.main:app --reload
```

打开浏览器访问：

```text
http://127.0.0.1:8000
```

## 环境变量

默认 `.env.example` 使用 mock 模式：

```env
LLM_PROVIDER=mock
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
MAX_CODE_CHARS=24000
```

如果要调用 DeepSeek：

```env
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=你的 DeepSeek Key
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
```

## API 示例

评审一段代码：

```bash
curl -X POST http://127.0.0.1:8000/api/review \
  -H "Content-Type: application/json" \
  -d "{\"language\":\"python\",\"code\":\"def add(a,b): return a-b\"}"
```

评审本地仓库改动并运行测试：

```bash
curl -X POST http://127.0.0.1:8000/api/review \
  -H "Content-Type: application/json" \
  -d "{\"language\":\"python\",\"repository_path\":\"D:\\profile\\diff-test-project\",\"base_branch\":\"main\",\"run_static_analysis\":true,\"run_tests\":true}"
```

查看本地仓库 diff：

```bash
curl -X POST http://127.0.0.1:8000/api/diff \
  -H "Content-Type: application/json" \
  -d "{\"repository_path\":\"D:\\profile\\diff-test-project\",\"base_branch\":\"main\"}"
```

单独运行测试：

```bash
curl -X POST http://127.0.0.1:8000/api/test \
  -H "Content-Type: application/json" \
  -d "{\"repository_path\":\"D:\\profile\\diff-test-project\",\"language\":\"python\",\"timeout_seconds\":60}"
```

## 响应模型概览

`ReviewResponse` 的核心结构：

```json
{
  "summary": "Found 1 issue(s) from combined repository evidence.",
  "issues": [
    {
      "file_path": "calculator.py",
      "source": "ruff",
      "severity": "low",
      "category": "F841",
      "line": 2,
      "message": "Local variable `unused` is assigned to but never used.",
      "suggestion": "Remove the unused variable."
    }
  ],
  "notices": [],
  "test_result": {
    "test_status": "passed",
    "command": "pytest",
    "failed_cases": 0,
    "log_excerpt": "...",
    "llm_explanation": null
  }
}
```

## 测试

推荐用项目虚拟环境运行：

```powershell
D:\profile\code-reviewer-mvp\.venv\Scripts\python.exe -m pytest
```

测试会强制使用 `LLM_PROVIDER=mock`，覆盖代码文本评审、Git diff 评审、base branch 校验、静态分析合并、非 Git 目录降级、测试运行和响应模型等关键行为。

## 开发阶段记录

- Phase 1：最小可用版本，支持代码文本、文件上传、本地目录读取和 mock/DeepSeek reviewer。
- Phase 2：改为 Git diff review，只评审 `base_branch...HEAD` 的变更行。
- Phase 3：加入自动化测试执行，并把测试结果并入 review 响应。
- Phase 4：加入静态分析工具，形成 diff + static analysis + tests + LLM 的合并评审链路。
