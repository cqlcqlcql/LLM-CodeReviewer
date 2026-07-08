# LLM-CodeReviewer 项目说明

这份文档用于后期重新理解项目，也可以作为 PPT 汇报的材料来源。它按“项目是什么、为什么这样做、怎么运行、内部模型是什么、技术栈和工具有哪些、每个模块负责什么、后续怎么扩展”的顺序展开。

## 1. 项目定位

LLM-CodeReviewer 是一个面向本地项目的代码评审 MVP。它不是一个通用聊天机器人，也不是一个单纯的静态检查器，而是一个把多种证据合并起来生成代码评审结论的小型系统。

项目要验证的核心假设是：

- LLM 适合做“综合判断”和“面向人的解释”。
- Git diff 适合限定 review 范围，避免模型评论大量未修改代码。
- 静态分析工具适合提供确定性证据，例如 lint、类型错误、安全风险。
- 自动化测试适合提供运行时证据，例如某个改动是否破坏已有行为。
- 后端应该先组织好上下文，再让模型总结，而不是只靠 prompt 让模型自己猜。

因此，系统最终形成的评审链路是：

```text
代码文本 / 单文件
  -> 后端读取文本
  -> 交给 mock 或 DeepSeek reviewer
  -> 生成结构化 JSON

本地项目目录
  -> 读取 Git diff
  -> 运行静态分析
  -> 可选运行测试
  -> 汇总为统一证据包
  -> 交给 mock 或 DeepSeek reviewer
  -> 前端展示问题、证据、测试结果和报告
```

## 2. 项目目标

### 2.1 功能目标

- 支持用户直接粘贴代码进行评审。
- 支持上传单个代码文件进行评审，单文件路径直接把文件内容交给当前 reviewer。
- 支持输入本地项目路径进行仓库级评审。
- 仓库级评审以 Git diff 为主，只评审当前分支相对基础分支的改动。
- 支持基础分支配置，避免硬编码 `main`。
- 支持本地静态分析工具，把工具发现的问题合并到评审结果中。
- 支持自动运行项目测试，把失败日志和失败数量纳入最终判断。
- 支持在没有真实 LLM Key 的情况下用 mock reviewer 完整演示。
- 支持切换 DeepSeek，用真实模型生成中文评审结果。
- 前端提供任务配置、执行过程、结果展示、测试页、报告页和历史页。

### 2.2 工程目标

- 后端接口返回结构化 JSON，而不是让前端解析自然语言。
- 用 Pydantic 模型约束请求和响应，保证前后端契约稳定。
- 把 Git、测试、静态分析、LLM 调用拆成独立 service，便于逐步扩展。
- 所有本地命令输出都按 UTF-8 读取并允许替换错误字符，降低 Windows 编码问题影响。
- 对非 Git 目录、无 diff、缺少工具等情况做降级处理，不把“没有证据”误报成代码问题。

## 3. 用户视角的使用流程

### 3.1 代码片段评审

用户输入语言和代码文本，后端直接把裁剪后的代码交给 reviewer。

适合场景：

- 快速演示 LLM 评审能力。
- 检查一小段函数或算法。
- 不需要 Git 上下文的简单代码问题。

### 3.2 文件上传评审

用户上传一个代码文件，后端按 UTF-8 读取内容并交给 reviewer。

这条链路不运行 Git diff、静态分析或自动化测试，因为上传文件没有仓库上下文、基础分支和测试目录。它的实际流程是：

```text
浏览器选择文件
  -> multipart/form-data 上传到 POST /api/review/file
  -> FastAPI 读取 UploadFile bytes
  -> UTF-8 解码并按 MAX_CODE_CHARS 裁剪
  -> reviewer.review(language, code)
  -> 返回 ReviewResponse
```

如果 `.env` 中 `LLM_PROVIDER=mock`，文件内容只进入本地 mock reviewer；如果切换为 `LLM_PROVIDER=deepseek`，文件内容才会通过 DeepSeek 兼容接口发送给真实模型。

适合场景：

- 临时检查一个独立文件。
- 不想输入本地仓库路径。

### 3.3 本地项目评审

用户输入本地项目路径、语言、基础分支，并选择是否运行静态分析和测试。

系统执行：

1. 尝试读取 `git diff <base_branch>...HEAD`。
2. 如果启用静态分析，运行可识别的本地工具。
3. 如果启用测试，运行可识别的测试命令。
4. 把所有证据交给 reviewer 合并。
5. 返回 `summary`、`issues`、`notices`、`test_result`。

这是项目最重要的路径，也是后续做 PPT 时最值得重点讲的流程。

## 4. 系统模型

这里的“项目模型”可以理解为系统内部如何把现实中的代码评审拆成对象、证据和流程。

### 4.1 输入模型

系统支持三类输入：

| 输入类型 | 入口 | 核心字段 | 说明 |
| --- | --- | --- | --- |
| 代码文本 | `POST /api/review` | `language`, `code` | 直接评审一段代码 |
| 文件上传 | `POST /api/review/file` | `language`, `file` | 直接评审上传文件内容，不运行 diff、静态分析或测试 |
| 本地仓库 | `POST /api/review` | `language`, `repository_path`, `base_branch`, `run_tests`, `run_static_analysis` | 评审本地项目变更 |

`ReviewRequest` 是最核心的请求模型：

```text
language: string
code: string | null
repository_path: string | null
base_branch: string, default main
run_tests: boolean, default false
run_static_analysis: boolean, default true
```

模型校验规则是：`code` 和 `repository_path` 至少要提供一个。

### 4.2 证据模型

仓库级评审会收集三类证据：

| 证据 | 来源 | 作用 |
| --- | --- | --- |
| Git diff | `app/services/diff_loader.py` | 告诉 reviewer 哪些代码是本次真正修改的 |
| 静态分析结果 | `app/services/static_analysis.py` | 提供确定性的 lint、类型、安全问题 |
| 测试结果 | `app/services/test_runner.py` | 提供运行时行为证据 |

这三类证据不是简单拼接到前端，而是在后端先统一成数据结构，再交给 reviewer 生成去重后的 `issues`。

注意：这三类证据只属于“本地项目目录评审”。代码文本和单文件上传没有 Git 仓库上下文，所以不会生成 diff，也不会触发静态分析和测试。

### 4.3 问题模型

每个 review 问题都使用 `ReviewIssue`：

```text
file_path: string | null
source: string
severity: low | medium | high
category: string
line: integer | null
message: string
suggestion: string
```

字段含义：

- `file_path`：问题所在文件。代码片段评审时可以为空。
- `source`：问题来源，例如 `LLM`、`ruff`、`mypy`、`bandit`、`pytest + LLM`。
- `severity`：严重程度。
- `category`：问题类别或工具规则编号，例如 `logic_bug`、`F841`、`type_error`。
- `line`：问题行号。工具或模型无法定位时可以为空。
- `message`：问题说明。
- `suggestion`：修复建议。

### 4.4 响应模型

`ReviewResponse`：

```text
summary: string
issues: ReviewIssue[]
notices: string[]
test_result: TestRunResponse | null
```

`notices` 用于放“非问题”的状态提示，例如：

- 当前路径不是 Git 仓库，已跳过 diff review。
- 当前分支相对基础分支没有 diff，已跳过 Git diff review。

这很重要，因为“没有 diff”不是代码缺陷，不能塞进 `issues`。

### 4.5 测试结果模型

`TestRunResponse`：

```text
test_status: passed | failed | timeout | unsupported | error
command: string | null
failed_cases: integer | null
log_excerpt: string
llm_explanation: string | null
```

测试结果既可以由 `POST /api/test` 单独返回，也可以嵌入仓库级 `POST /api/review` 的响应里。

在仓库级 review 中，测试失败不会直接作为静态分析问题重复出现，而是作为 runtime evidence 交给 reviewer，尽量和 diff 问题合并。

## 5. 后端架构

后端是一个紧凑的 FastAPI 应用。

```text
app/main.py
  -> 接收 HTTP 请求
  -> 调用 diff_loader / static_analysis / test_runner
  -> 构造 reviewer
  -> 返回 Pydantic response

app/services/diff_loader.py
  -> 校验本地 Git 仓库
  -> 执行 git diff <base_branch>...HEAD
  -> 解析 unified diff
  -> 格式化为 LLM 易读的 diff context

app/services/static_analysis.py
  -> 根据项目类型识别静态工具
  -> 运行 ruff / mypy / bandit / npm run lint
  -> 解析工具输出为 ReviewIssue

app/services/test_runner.py
  -> 根据项目类型识别测试命令
  -> 运行 pytest / npm test / mvn test / gradle test
  -> 提取失败数量和日志摘要

app/services/llm.py
  -> 定义 CodeReviewer 抽象类
  -> 实现 MockReviewer
  -> 实现 DeepSeekReviewer
  -> 约束 LLM 输出 JSON schema
```

### 5.1 API 编排逻辑

`POST /api/review` 是最核心接口。

当请求里有 `repository_path`：

1. 初始化 `diff_context`、`notices`、`static_issues`、`test_result`。
2. 尝试读取 Git diff。
3. 如果 diff 不可用但属于可降级情况，就记录 notice。
4. 如果启用静态分析，就运行工具并收集 `ReviewIssue`。
5. 如果启用测试，就运行项目测试。
6. 如果有任何证据，就调用 `reviewer.review_repository(...)`。
7. 把 notice 和 test_result 合并进最终响应。

当请求里没有 `repository_path`：

1. 读取 `code`。
2. 按 `MAX_CODE_CHARS` 裁剪。
3. 调用 `reviewer.review(...)`。

当请求走 `POST /api/review/file`：

1. 读取上传文件 bytes。
2. 用 UTF-8 解码，无法识别的字符会被忽略。
3. 按 `MAX_CODE_CHARS` 裁剪。
4. 调用 `reviewer.review(language, code)`。

所以单文件上传和代码文本评审属于同一类“直接代码评审”，区别只是代码来源不同；它们不会进入 `review_repository(...)` 的三证据合并流程。

### 5.2 Git diff 模型

系统执行：

```text
git -c safe.directory=<root> -C <root> diff <base_branch>...HEAD
```

这样做有几个原因：

- `base_branch...HEAD` 能表达“当前分支相对基础分支的变更”。
- `safe.directory` 能减少 Windows / Git ownership 场景下的安全限制问题。
- `base_branch` 做了正则校验，避免用户输入危险参数。
- diff 输出统一按 UTF-8 解码，并使用 `errors="replace"`，避免中文 Windows 环境下默认编码导致异常。

解析后的 diff 会变成类似下面的 LLM 上下文：

```text
FILE: calculator.py
HUNK: @@ -1,2 +1,5 @@
CONTEXT old_line=1 new_line=1: def subtract(a, b):
CONTEXT old_line=2 new_line=2:     return a - b
ADDED new_line=4: def add(a, b):
ADDED new_line=5:     return a - b
```

这个格式的重点是让 reviewer 明确：

- 哪些行是新增或修改行。
- 每个 changed line 在新文件中的行号。
- context 只是辅助理解，不应该被单独评论。

### 5.3 静态分析模型

当前识别逻辑：

| 项目类型 | 触发条件 | 工具 |
| --- | --- | --- |
| Python | `language=python`，或存在 `pytest.ini`、`pyproject.toml`、`requirements.txt`、`tests/test_*.py` 等 | `ruff`、`mypy`、`bandit` |
| JavaScript/TypeScript | `language=javascript/typescript`，或存在 `package.json` | `npm run lint` |

静态分析结果会被转换成统一的 `ReviewIssue`。例如 `ruff` 的 `F841` 会变成：

```json
{
  "source": "ruff",
  "file_path": "calculator.py",
  "severity": "low",
  "category": "F841",
  "line": 2,
  "message": "Local variable `unused` is assigned to but never used.",
  "suggestion": "Remove the unused variable."
}
```

如果工具没有安装，或者 npm 项目没有 lint script，系统会跳过，不把缺工具当作代码问题。

### 5.4 自动化测试模型

测试命令识别：

| 项目类型 | 触发条件 | 命令 |
| --- | --- | --- |
| Python | Python 语言或 Python 项目标记 | `python -m pytest` |
| JavaScript/TypeScript | JS/TS 语言或 `package.json` | `npm test` |
| Java Maven | `pom.xml` | `mvn test` |
| Java Gradle | `build.gradle`、`build.gradle.kts`、`gradlew` | `gradle test` 或 wrapper |

测试状态：

- `passed`：命令退出码为 0。
- `failed`：命令能运行，但测试失败。
- `timeout`：超过超时时间。
- `unsupported`：没有识别到支持的测试命令。
- `error`：测试命令无法启动。

`POST /api/test` 会在测试失败时调用 reviewer 解释日志。仓库级 `POST /api/review` 则把测试结果作为最终综合评审的证据，不单独生成一段测试解释，避免重复。

## 6. LLM 评审模型

`app/services/llm.py` 定义了统一抽象：

```text
CodeReviewer
  review(language, code)
  review_diff(language, diff_context)
  review_repository(language, diff_context, static_issues, test_result)
  explain_test_failure(command, log_excerpt)
```

### 6.1 MockReviewer

mock reviewer 是本项目非常重要的工程设计，因为它让系统不依赖 API Key 也能完整演示和测试。

当前 mock 行为包括：

- 识别 Python 中 `def add(...): return a-b` 这种函数名与行为不一致的问题。
- 识别 `TODO` / `FIXME`。
- 在 diff review 中只关注 changed line。
- 合并静态分析问题和测试失败问题。
- 没有问题时返回空 `issues` 数组。

### 6.2 DeepSeekReviewer

DeepSeek reviewer 使用 `openai.AsyncOpenAI`，但把 `base_url` 配置为 DeepSeek 兼容接口。

关键约束：

- 使用 `response_format={"type": "json_object"}` 要求模型返回 JSON。
- 用 Pydantic 的 `ReviewResponse.model_validate(...)` 再验证一次。
- prompt 明确要求只评论新增或修改代码。
- 用户可见的 summary、message、suggestion 使用简体中文。
- 代码标识符、文件名、函数名、测试名、字面值保持原文。

### 6.3 为什么要用结构化 JSON

如果 LLM 只返回自然语言，前端很难稳定展示，也很难区分问题来源、严重程度、文件位置和修复建议。

结构化 JSON 的好处：

- 前端可以直接渲染卡片。
- 测试可以断言字段。
- 后续可以导出报告、做统计、做过滤。
- 工具结果和 LLM 结果可以共享同一个问题模型。

## 7. 前端架构

前端是 `frontend/index.html` 单文件静态应用，不需要 React/Vue 构建链。

它包含：

- 侧边栏导航。
- 创建任务页。
- 任务执行进度页。
- 结果页。
- 测试页。
- 报告页。
- 历史页。

前端主要职责：

- 收集用户输入的项目目录、语言、基础分支、是否运行静态分析和测试。
- 收集用户上传的单个代码文件。
- 调用 `/api/review`、`/api/review/file`、`/api/diff`、`/api/test`。
- 展示 diff 文件摘要和增删行。
- 展示 review issue 卡片。
- 展示测试日志和测试诊断。
- 生成 Markdown 报告。
- 使用 `localStorage` 保存最近的 review 历史。

前端当前仍是 MVP 形态：优点是部署简单，缺点是文件较大、状态管理集中在一个 HTML 文件中。后续如果继续扩展，可以拆成 React 或 Vue 项目。

## 8. 技术栈

### 8.1 后端

| 技术 | 用途 |
| --- | --- |
| Python | 后端主语言 |
| FastAPI | HTTP API 框架 |
| Pydantic | 请求/响应模型校验 |
| pydantic-settings | `.env` 配置读取 |
| python-dotenv | 加载环境变量 |
| OpenAI Python SDK | 通过兼容接口调用 DeepSeek |
| Uvicorn | 本地 ASGI 服务 |
| python-multipart | 支持文件上传接口 |

### 8.2 LLM

| 模式 | 用途 |
| --- | --- |
| `LLM_PROVIDER=mock` | 本地演示、自动化测试、无 API Key 场景 |
| `LLM_PROVIDER=deepseek` | 调用真实 DeepSeek 模型 |
| `DEEPSEEK_MODEL=deepseek-v4-flash` | 默认模型配置 |

### 8.3 本地分析与测试工具

| 工具 | 用途 |
| --- | --- |
| Git | 读取 `base_branch...HEAD` diff |
| pytest | Python 自动化测试 |
| ruff | Python lint |
| mypy | Python 类型检查 |
| bandit | Python 安全扫描 |
| npm test | JS/TS 项目测试 |
| npm run lint | JS/TS 项目 lint |
| Maven | Java 项目测试 |
| Gradle | Java 项目测试 |

### 8.4 前端

| 技术 | 用途 |
| --- | --- |
| HTML | 页面结构 |
| CSS | 布局、卡片、状态颜色、响应式样式 |
| 原生 JavaScript | API 调用、状态管理、渲染和报告生成 |
| localStorage | 保存最近 review 历史 |

### 8.5 测试与开发

| 工具 | 用途 |
| --- | --- |
| pytest | 后端测试运行器 |
| FastAPI TestClient | API 行为测试 |
| httpx | TestClient 依赖 |
| `.venv` | 项目本地 Python 虚拟环境 |

## 9. 工具列表

### 9.1 运行项目需要的命令

```powershell
cd D:\profile\code-reviewer-mvp
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload
```

### 9.2 安装依赖

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 9.3 运行测试

```powershell
D:\profile\code-reviewer-mvp\.venv\Scripts\python.exe -m pytest
```

### 9.4 典型 API 调试

健康检查：

```bash
curl http://127.0.0.1:8000/api/health
```

仓库评审：

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

## 10. API 详解

### 10.1 `GET /`

返回前端页面 `frontend/index.html`。

### 10.2 `GET /api/health`

返回服务状态和当前 reviewer provider。

示例：

```json
{
  "status": "ok",
  "provider": "mock"
}
```

### 10.3 `POST /api/review`

最重要的 review 接口。

代码文本评审请求：

```json
{
  "language": "python",
  "code": "def add(a,b): return a-b"
}
```

仓库评审请求：

```json
{
  "language": "python",
  "repository_path": "D:\\profile\\diff-test-project",
  "base_branch": "main",
  "run_static_analysis": true,
  "run_tests": true
}
```

仓库评审响应：

```json
{
  "summary": "Found 1 issue(s) from combined repository evidence.",
  "issues": [
    {
      "file_path": "calculator.py",
      "source": "LLM",
      "severity": "high",
      "category": "logic_bug",
      "line": 4,
      "message": "add function returns subtraction on a changed line.",
      "suggestion": "Change the added return expression from a-b to a+b."
    }
  ],
  "notices": [],
  "test_result": null
}
```

### 10.4 `POST /api/review/file`

上传单个文件进行评审，使用 `multipart/form-data`。

字段：

- `language`
- `file`

示例：

```bash
curl -X POST http://127.0.0.1:8000/api/review/file \
  -F "language=python" \
  -F "file=@calculator.py"
```

处理逻辑：

```text
UploadFile -> UTF-8 文本 -> trim_code -> reviewer.review
```

这条接口不会运行 Git diff、静态分析或自动化测试。是否真的发给 DeepSeek 取决于 `LLM_PROVIDER`：默认 `mock` 不会调用外部模型，`deepseek` 才会调用真实模型。

### 10.5 `POST /api/diff`

读取本地仓库 diff。

请求：

```json
{
  "repository_path": "D:\\profile\\diff-test-project",
  "base_branch": "main"
}
```

响应：

```json
{
  "base_branch": "main",
  "diff": "...",
  "files": [
    {
      "path": "calculator.py",
      "additions": 1,
      "deletions": 1,
      "hunks": 1
    }
  ]
}
```

### 10.6 `POST /api/test`

单独运行项目测试。

请求：

```json
{
  "repository_path": "D:\\profile\\diff-test-project",
  "language": "python",
  "timeout_seconds": 60
}
```

响应：

```json
{
  "test_status": "failed",
  "command": "pytest",
  "failed_cases": 1,
  "log_excerpt": "...",
  "llm_explanation": "..."
}
```

## 11. 阶段演进

### 11.1 Phase 1：最小可用版本

目标是先跑通从用户输入到 LLM JSON 输出再到前端展示的闭环。

实现内容：

- FastAPI 服务。
- `/api/review`。
- `/api/review/file`。
- mock reviewer。
- DeepSeek reviewer。
- Pydantic 响应模型。
- 单文件前端。
- 基础 API 测试。

### 11.2 Phase 2：Git diff review

问题背景：直接读取整个仓库会让模型评论很多没改过的代码，review 范围不清晰。

改进：

- 引入 `base_branch`。
- 使用 `git diff <base_branch>...HEAD`。
- 解析 unified diff。
- prompt 明确要求只评审新增或修改代码。
- 没有真实问题时返回空 `issues`。

### 11.3 Phase 3：自动化测试

问题背景：代码表面看起来没问题，但测试失败能提供更强的运行时证据。

改进：

- 新增 `/api/test`。
- 识别 Python、JS/TS、Java 测试命令。
- 返回测试状态、命令、失败数量、日志摘要。
- 仓库级 review 可以通过 `run_tests: true` 把测试结果纳入综合评审。

### 11.4 Phase 4：静态分析

问题背景：LLM 的判断不稳定，很多 lint、类型、安全问题应该让确定性工具先发现。

改进：

- Python 项目运行 `ruff`、`mypy`、`bandit`。
- JS/TS 项目运行 `npm run lint`。
- 工具输出统一转换成 `ReviewIssue`。
- 最终 review 合并 diff、静态分析、测试三类证据。
- 静态分析默认开启。

## 12. 关键设计取舍

### 12.1 为什么默认 mock

mock 模式保证项目在没有 API Key、没有网络、模型不可用时仍然能演示、测试和开发。

这对 MVP 很关键，因为前后端、数据模型、工具执行、错误处理都可以独立于真实模型验证。

### 12.2 为什么只 review diff

真实代码评审通常关注“本次改动引入了什么风险”。如果把整个仓库交给模型，模型可能会：

- 评论历史遗留问题。
- 忽略本次修改重点。
- 消耗更多 token。
- 生成大量泛泛建议。

所以仓库级 review 以 diff 为主。

### 12.3 为什么静态分析默认开启

静态工具的输出更确定，适合发现格式、类型、安全等基础问题。LLM 更适合做综合判断和解释。

默认开启静态分析可以让 review 结果更可靠，同时工具缺失时会自动跳过，避免影响基本流程。

### 12.4 为什么测试在仓库 review 中不直接生成解释

单独 `/api/test` 的目标是“解释测试失败”，所以可以返回 `llm_explanation`。

仓库级 `/api/review` 的目标是“生成最终问题列表”。如果测试失败和 diff 指向同一个 bug，应该合并成一个问题，而不是同时展示一个代码问题和一个重复的测试问题。

## 13. 异常与降级策略

| 场景 | 处理方式 |
| --- | --- |
| `repository_path` 不存在 | 返回 400 |
| `repository_path` 不是目录 | 返回 400 |
| 不是 Git 仓库 | 跳过 diff，记录 notice，继续静态分析和测试 |
| 没有 diff | 跳过 diff，记录 notice，继续静态分析和测试 |
| `base_branch` 非法 | 返回 400 |
| Git 命令超时 | 返回 408 |
| 静态工具未安装 | 跳过该工具 |
| npm 没有 lint script | 跳过 `npm run lint` |
| 没有识别到测试命令 | 返回 `unsupported` |
| 测试超时 | 返回 `timeout` |
| DeepSeek 返回非 JSON | 返回 502 |

## 14. 测试覆盖

当前 `tests/test_api.py` 覆盖了这些核心行为：

- mock reviewer 能识别 `add` 函数返回减法的问题。
- 仓库 review 使用用户配置的 `base_branch`。
- diff context 能正确定位新增函数后的问题行。
- `/api/diff` 返回文件摘要和 unified diff。
- 非法 `base_branch` 会被拒绝。
- `/api/test` 能识别 Python 测试失败。
- 仓库 review 可以包含测试结果。
- 静态分析问题可以合并进 review response。
- 没有 diff 时仍然可以运行静态分析。
- 非 Git 目录也可以运行静态分析和测试。
- 静态分析不会把 pytest 失败当作 lint 问题。
- 仓库 review 中测试失败不会暴露 diff 不可用文本为问题。
- `/api/review` 必须有 `code` 或 `repository_path`。

推荐验证命令：

```powershell
D:\profile\code-reviewer-mvp\.venv\Scripts\python.exe -m pytest
```

## 15. PPT 可用提纲

### 第 1 页：项目标题

标题：LLM-CodeReviewer：基于 LLM 的本地代码评审 MVP

一句话介绍：把 Git diff、静态分析、自动化测试和大模型合并成结构化代码评审结果。

### 第 2 页：为什么做这个项目

可讲要点：

- 普通 LLM 直接 review 整个仓库范围太大。
- 人工 code review 需要关注本次变更、测试结果和工具信号。
- 项目尝试让 LLM 不再“凭空看代码”，而是基于明确证据做总结。

### 第 3 页：整体架构

可画流程：

```text
Frontend
  -> FastAPI
  -> Git diff / Static tools / Test runner
  -> Reviewer abstraction
  -> Mock or DeepSeek
  -> Structured JSON
  -> UI cards and report
```

### 第 4 页：核心数据模型

重点展示：

- `ReviewRequest`
- `ReviewIssue`
- `ReviewResponse`
- `TestRunResponse`

强调：所有输出都结构化，前端不解析自然语言。

### 第 5 页：Diff Review

重点展示：

- `git diff <base_branch>...HEAD`
- 只 review changed lines。
- context lines 只做理解辅助。
- 支持 `main`、`master`、`develop`。

### 第 6 页：静态分析

重点展示：

- `ruff`
- `mypy`
- `bandit`
- `npm run lint`
- 工具结果统一转换成 `ReviewIssue`。

### 第 7 页：自动化测试

重点展示：

- `pytest`
- `npm test`
- `mvn test`
- `gradle test`
- 测试失败作为 runtime evidence，并入最终 review。

### 第 8 页：LLM 设计

重点展示：

- `CodeReviewer` 抽象。
- `MockReviewer` 保证无 Key 可运行。
- `DeepSeekReviewer` 使用 OpenAI SDK 兼容接口。
- JSON schema + Pydantic 双重约束。

### 第 9 页：前端展示

重点展示：

- 任务配置。
- 执行进度。
- diff 摘要。
- issue 卡片。
- 测试日志。
- Markdown 报告。
- 历史记录。

### 第 10 页：项目亮点

可讲要点：

- 不是只写 prompt，而是先构建可靠上下文。
- 支持无模型环境下完整测试。
- 使用确定性工具增强 LLM 判断。
- 对非 Git 目录、无 diff、缺少工具等情况有降级处理。
- 数据模型清晰，便于后续扩展。

### 第 11 页：不足与后续优化

可讲要点：

- 前端还是单文件，后续可拆 React/Vue。
- 当前只支持少量语言和工具。
- 还没有持久化数据库。
- 没有接 GitHub PR 评论。
- 没有用户体系和权限控制。
- LLM 输出质量依赖模型能力和 prompt。
- 可加入报告导出、历史对比、CI 集成、PR bot。

## 16. 后续扩展方向

### 16.1 产品功能

- 接入 GitHub Pull Request，自动读取 PR diff。
- 支持把 review 结果评论回 PR。
- 增加项目历史记录数据库。
- 增加报告导出为 PDF。
- 增加 review 严重程度过滤和按文件筛选。
- 增加规则配置，例如忽略某些路径或工具规则。

### 16.2 工程能力

- 前端拆成组件化工程。
- 后端增加任务队列，避免长时间测试阻塞请求。
- 增加缓存，避免重复运行相同 diff 的工具。
- 增加更细粒度的日志和审计。
- 增加 Docker 部署。
- 增加 OpenAPI 文档示例。

### 16.3 模型能力

- 支持更多模型 provider。
- 针对不同语言使用不同 review prompt。
- 对 LLM 输出做更强的 schema 约束。
- 增加“证据引用”，让每个问题都能回到 diff 行、工具输出或测试日志。
- 增加自动修复建议 patch。

## 17. 重新理解项目时的阅读顺序

建议以后回顾时按这个顺序看：

1. 先读外层 `README.md`，快速恢复项目用途和运行方式。
2. 再读本文件，理解系统模型和整体架构。
3. 看 `app/schemas.py`，掌握请求响应契约。
4. 看 `app/main.py`，理解 API 编排。
5. 看 `app/services/diff_loader.py`，理解 diff review。
6. 看 `app/services/static_analysis.py`，理解工具结果如何转成 issue。
7. 看 `app/services/test_runner.py`，理解测试执行。
8. 看 `app/services/llm.py`，理解 mock 和 DeepSeek reviewer。
9. 看 `tests/test_api.py`，确认哪些行为已经被测试固定。
10. 最后看 `frontend/index.html`，理解用户界面如何消费 API。

## 18. 一句话总结

LLM-CodeReviewer 的价值不在于“让模型随便看一段代码”，而在于用工程化方式把 diff、静态分析和测试结果组织成清晰证据，再让 LLM 生成结构化、可展示、可测试、可扩展的代码评审结果。
