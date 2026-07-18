# LLM-CodeReviewer

LLM-CodeReviewer 是一个本地代码评审 MVP。它用 FastAPI 提供后端 API，用静态 HTML 前端提供可操作界面，把单文件上传、本地项目目录、Git diff、静态分析、自动化测试和 LLM 评审合并成统一的结构化结果。

这个项目的重点不是训练模型，而是验证一条可落地的代码评审链路：先由后端收集明确证据，再交给 DeepSeek reviewer 生成 `ReviewResponse`，最后由前端展示问题、测试诊断、diff 摘要、Markdown 报告和历史记录。

完整项目说明、流程图、数据结构和阶段复盘见 [docs/project-overview.md](docs/project-overview.md)。

## 当前能力

- `POST /api/review`：评审本地 Git 或非 Git 项目目录。
- `POST /api/review/file`：把主源码和可选的关联测试文件放入隔离的临时 Git 工作区，再复用 diff、静态分析、测试和 DeepSeek 综合评审流程。
- `POST /api/diff`：读取本地 Git 仓库的 `base_branch...HEAD` diff，并返回文件级摘要。
- `POST /api/test`：识别并运行 Python、JavaScript/TypeScript、Java 项目的测试命令。
- Git 仓库路径优先走 diff review，只关注当前分支相对基础分支的变更。
- 非 Git 目录使用显式可关闭的受限源码快照：按语言筛选文件，排除测试、依赖、构建目录和常见敏感文件，并受单文件与总字符上限约束；静态分析和测试涉及的文件会优先进入上下文。
- 静态分析支持 Python 的 `ruff`、`mypy`、`bandit`，以及 JS/TS 项目的 `npm run lint`。
- 产品评审统一通过 OpenAI SDK 兼容接口调用 DeepSeek；自动化测试使用测试目录中的确定性替身，不访问网络。

## 中期答辩后改进

- 单文件评审使用唯一任务目录，支持附带最多 20 个关联测试文件，并自动建立空基线提交和 review 提交。
- 移除产品 MockReviewer 和语义黑名单过滤，保留结构化 JSON 校验与证据优先提示词。
- pytest 运行项目实际发现的全部用例；前端展示收集、通过、失败、跳过、错误数量，失败列表和可展开日志。

## 项目结构

```text
code-reviewer-mvp/
  app/
    main.py                  # FastAPI 入口和 API 编排
    schemas.py               # Pydantic 请求、响应、问题和测试模型
    settings.py              # .env 配置读取
    services/
      code_loader.py         # 目录代码读取、过滤和长度裁剪
      diff_loader.py         # Git diff 读取、解析、行号格式化
      llm.py                 # DeepSeek reviewer、结构化 JSON 校验和证据合并提示词
      static_analysis.py     # ruff、mypy、bandit、npm lint
      test_runner.py         # pytest、npm test、mvn/gradle test
  frontend/
    index.html               # 主界面：任务、结果、测试、报告、历史
    batch-test.html          # 批量测试辅助页面
  docs/
    phase1-guide.md
    phase2-diff-review.md
    phase3-automated-tests.md
    phase4-static-analysis.md
    project-overview.md
  tests/
    test_api.py              # 端到端 API 行为测试
    test_code_loader.py      # 非 Git 源码快照筛选和截断测试
  requirements.txt
  pytest.ini
```

## 核心流程

```mermaid
flowchart TD
    A["用户输入"] --> B{"输入类型"}
    B -->|上传单文件| D["/api/review/file: 读取 UploadFile"]
    B -->|本地目录| E{"是否 Git 仓库"}
    D --> W["创建隔离 Git 工作区和两次提交"]
    W --> F
    E -->|是| F["读取 git diff base_branch...HEAD"]
    F --> G["解析文件、hunk、ADDED 行号"]
    E -->|否| H["记录非 Git notice"]
    G --> I["可选静态分析"]
    H --> I
    I --> J["可选自动化测试"]
    J --> Q{"非 Git 且启用源码快照？"}
    Q -->|是| S["优先相关文件并构建受限源码快照"]
    Q -->|否| K["review_repository 汇总证据"]
    S --> K
    K --> R["DeepSeek reviewer"]
    R --> L["ReviewResponse"]
    L --> M["前端结果页 / 测试页 / 报告页 / 历史页"]
```

## 数据结构概览

后端统一返回 `ReviewResponse`：

```json
{
  "summary": "Found 1 issue(s) from combined repository evidence.",
  "issues": [
    {
      "file_path": "calculator.py",
      "source": "ruff",
      "evidence_sources": ["ruff"],
      "severity": "low",
      "category": "F841",
      "line": 2,
      "message": "Local variable `unused` is assigned to but never used.",
      "suggestion": "Remove the unused variable."
    }
  ],
  "notices": [],
  "generated_by": "deepseek",
  "llm_status": "succeeded",
  "code_context": "git_diff",
  "context_files": 0,
  "context_chars": 1840,
  "test_result": {
    "test_status": "passed",
    "command": "pytest",
    "collected_cases": 8,
    "passed_cases": 8,
    "failed_cases": 0,
    "skipped_cases": 0,
    "error_cases": 0,
    "log_excerpt": "...",
    "llm_explanation": null
  }
}
```

Git diff 路径会把 unified diff 转成 reviewer 更容易使用的上下文，例如：

```text
FILE: calculator.py
HUNK: @@ -1,2 +1,2 @@
ADDED new_line=2:     return a - b
```

因此 Git 项目里的评审意见可以追溯到具体文件和新文件行号。单文件和非 Git 目录在缺少 diff 时仍能评审，但行号证据取决于文件内容、静态分析或测试日志。

## 本地运行

Windows PowerShell:

```powershell
cd D:\profile\code-reviewer-mvp
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload
```

打开：

```text
http://127.0.0.1:8000
```

如果还没有虚拟环境：

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
uvicorn app.main:app --reload
```

## 环境变量

复制 `.env.example` 后填写 DeepSeek API Key：

```env
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
MAX_CODE_CHARS=24000
```

产品运行不再提供 mock provider；缺少 `DEEPSEEK_API_KEY` 时，健康检查仍可用，但评审请求会明确返回配置错误。

## API 示例

评审本地 Git 仓库，并合并静态分析和测试结果：

```bash
curl -X POST http://127.0.0.1:8000/api/review \
  -H "Content-Type: application/json" \
  -d "{\"language\":\"python\",\"repository_path\":\"D:\\profile\\diff-test-project\",\"base_branch\":\"main\",\"run_static_analysis\":true,\"run_tests\":true}"
```

单独查看 diff：

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

## 前端速览

主界面包含五个视图：

- 任务页：上传主源码及关联测试文件，或填写本地项目目录，并配置 base branch、静态分析和测试。
- 结果页：展示总体评分、问题卡片、证据片段、diff 摘要和测试状态。
- 测试页：展示测试命令、状态、失败用例数量、日志摘要和 LLM 测试分析。
- 报告页：生成 Markdown 报告，可下载或通过浏览器打印。
- 历史页：用浏览器 `localStorage` 保存最近评审摘要。

前端评分由 `scoreReview(issues, test)` 计算：高危问题或测试失败为 `C`，中危问题为 `B`，只有低危问题为 `A-`，无问题且测试未失败为 `A`。当前实现没有使用 `D` 档。

## 测试

推荐使用项目虚拟环境运行：

```powershell
D:\profile\code-reviewer-mvp\.venv\Scripts\python.exe -m pytest
```

测试会自动注入仅存在于 `tests/` 的确定性 reviewer，不访问 DeepSeek。测试覆盖仓库路径校验、单文件临时 Git 工作区、Git diff、base branch、静态分析合并、非 Git 源码快照、pytest 数量统计和响应模型等关键行为。
