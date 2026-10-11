# ModelTrace

ModelTrace 是一个本地运行的主动模型归因工具。它通过三条独立的长整数生成挑战提取输出指纹，在统一候选库中自动判断模型家族和具体版本。

## 运行

```powershell
python -m pip install -r requirements.txt
python start.py
```

页面地址为 `http://127.0.0.1:7860/`。

## Docker 异步服务

服务包含 Flask/Gunicorn API、独立 worker 和带 AOF 持久化的 Redis。每个 worker 同时执行一个任务，任务使用现有统一指纹库和归因算法。浏览器自动测试入口也会提交异步任务。

### 启动

在安装了 Docker Compose v2 的服务器上：

```sh
cp .env.example .env
# 生成服务访问令牌和独立的 Fernet 加密密钥，分别填入 .env 对应项。
python3 -c 'import secrets, base64; print("MODELTRACE_API_TOKEN=" + secrets.token_urlsafe(32)); print("CREDENTIAL_ENCRYPTION_KEY=" + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'
docker compose up -d --build
```

默认监听 `127.0.0.1:7860`，可通过反向代理提供 HTTPS。需要直接监听外部网卡时在 `.env` 中设置 `BIND_ADDRESS=0.0.0.0`。网页中的“服务访问令牌”填写 `MODELTRACE_API_TOKEN`，与上游 API Key 分开。所有 `/api/*` 请求都需要此令牌；此版本是共享令牌的可信调用方服务，不提供多租户隔离。

Redis 不发布宿主机端口。任务和结果保存在 `redis-data` 卷；普通容器重建不会删除数据，`docker compose down -v` 会删除。AOF 每秒同步，主机突然掉电仍可能损失最近约一秒的状态；应用不会自动重试未知提交结果。

```sh
# 将检测并发提高到 2；每个任务内仍串行采样。
docker compose up -d --scale worker=2
# 查看健康状态、运行日志（不输出请求体或上游 Key）。
docker compose ps
docker compose logs --tail=100 api worker
```

服务器模式下指纹库只读，旧的同步上游测试、自动采集和建库写接口返回 409。在本地维护 `data/` 后重新构建并重启 API 和 worker，更新参考库；每个任务记录实际使用的 `bank_sha256`。手动测试和指纹库查看继续可用。GitHub Pages 静态站点仍为手动版本。

### 异步 API

以下示例中的 `SERVICE_TOKEN` 是你自行设置的服务访问令牌，不是上游 Key。

```sh
curl -i http://127.0.0.1:7860/api/detections \
  -H "Authorization: Bearer $SERVICE_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"base_url":"https://example.com/v1","api_key":"UPSTREAM_KEY","model":"provider-model-alias"}'
```

返回 `202 Accepted`，包含 `id`、`status: "queued"`、`status_url` 和进度。`Location` 指向查询地址，`Retry-After: 2` 建议两秒后查询。**提交后不要为了等待结果重复 POST**，每次提交都会创建新任务并可能产生费用。提交连接中断且未获得 ID 时结果未知，此版本不提供幂等提交。

请求字段：

| 字段 | 必填 | 含义 |
|---|---|---|
| `base_url` | 是 | 服务根地址、以 `/v1` 结尾的地址，或完整 `/responses` 地址；自动规范化到 Responses |
| `api_key` | 是 | 上游 API Key，仅用于此任务 |
| `model` | 是 | 上游接受的模型别名，仅用于路由，不作为识别依据 |
| `temperature` | 否 | 0～2；默认不传，由上游使用默认值。上游不支持此参数时应省略 |

```sh
# 查询：运行中与终态均返回 200。
curl http://127.0.0.1:7860/api/detections/TASK_ID \
  -H "Authorization: Bearer $SERVICE_TOKEN"
# 取消：重复取消不会重新执行，也不会覆盖已有终态。
curl -X DELETE http://127.0.0.1:7860/api/detections/TASK_ID \
  -H "Authorization: Bearer $SERVICE_TOKEN"
```

主要返回字段：

| 字段 | 含义 |
|---|---|
| `status` | `queued`、`running`、`completed`、`partial`、`failed`、`cancelled` 或 `interrupted` |
| `progress` | `target: 3`、`valid`、`attempted`、`max_attempts: 6` |
| `result` | 无有效样本时为 null；否则含 `prediction`、`prediction_name`、`results` 候选排名、家族概率、相似度及校准信息 |
| `preliminary` | 有结果但样本少于三份时为 true |
| `insufficient_samples` | 有效样本少于三份时为 true；结果内还有中文 `sample_warning` |
| `samples` | 各轮数字数量、是否计入及上游返回的 token 用量（上游未提供时为空） |
| `errors` | 未计入的轮次及原因；上游 HTTP 错误只记录状态码，避免错误体回显 Key |
| `reason` | 如 `target_reached`、`attempts_exhausted`、`upstream_error`、`task_timeout`、`queue_timeout`、`user_cancelled`、`worker_lost` |
| `elapsed_seconds` | 从开始执行到当前或结束的耗时，不包含排队 |
| `created_at` / `started_at` / `finished_at` | Unix 时间戳（秒） |
| `bank_sha256` | 执行时加载的统一指纹库文件摘要 |

返回状态语义：

- `completed`：三份有效回答，完整结果。
- `partial`：因六次尝试用完、总超时或不可恢复的上游错误而结束，但有一至两份有效回答，返回初步结果。
- `failed`：没有可用回答，或排队超时；不生成模型结论。
- `cancelled` / `interrupted`：取消或执行中断；保留已保存的结果，样本不足时仍标注为初步结果，不自动重跑。

运行中 `result` 也可能包含已完成样本的初步结果；请根据 `status` 判断任务是否结束。归因仍为**现有候选库内的闭集判断**，不保证未收录模型能被识别为未知。

参数错误返回 400，鉴权失败 401，任务不存在或结果过期 404，队列满 429，Redis 不可用 503。`GET /healthz` 无需鉴权，仅检查 API 与任务存储是否可用，不代表 worker 已上线。

### 长任务与凭据配置

| 环境变量 | 默认值 | 含义 |
|---|---|---|
| `REQUEST_TIMEOUT_SECONDS` | 240 | 单次上游请求硬时限，覆盖 DNS、连接和完整响应读取 |
| `TASK_TIMEOUT_SECONDS` | 1800 | 每个任务开始执行后的总时限 |
| `QUEUE_TIMEOUT_SECONDS` | 3600 | 最大排队时长 |
| `RESULT_TTL_SECONDS` | 86400 | 终态结果保留时长（秒） |
| `MAX_QUEUED_TASKS` | 100 | 最大等待任务数，超过返回 429 |
| `UPSTREAM_ALLOWED_HOSTS` | 空 | 可选的上游域名白名单，以逗号分隔，精确匹配主机名 |
| `ALLOW_PRIVATE_UPSTREAM` | false | 仅在需要访问可信内网 LLM 时启用 |

每个任务最多发送六个 Responses 请求，没有额外的隐藏 HTTP 重试。408、429、部分 5xx、超时和无效样本会消耗下一次尝试；401/403、请求参数错误等不可恢复错误提前结束。请求不携带工具、历史对话或前一轮响应，`store=false`、`stream=false`；只从完整 assistant 消息的 `output_text` 中提取数字，拒答、截断和 reasoning 内容不计入。[Responses 官方说明](https://developers.openai.com/api/docs/guides/migrate-to-responses)

取消会阻止后续采样，并结束本地正在等待的请求进程；已到达上游的请求仍可能继续计费。worker 心跳租约为 30 秒；API 查询或存活 worker 检查到租约过期后，将任务标为 `interrupted`。正常关闭也会标记中断。排队任务在重启后继续排队，已开始但中断的任务不会自动重跑。

`MODELTRACE_API_TOKEN` 与 `CREDENTIAL_ENCRYPTION_KEY` 必须独立设置且 API/worker 一致；不要把真实密钥提交到仓库。更换加密密钥前先清空等待任务，否则旧任务凭据无法解密。上游默认不能访问本机、内网和保留地址；连接使用校验后的 IP 并保留 TLS 主机校验，禁用 HTTP 重定向及环境代理，防止 Key 被重定向到其他主机。

### 开发与测试

```sh
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

测试使用模拟 Redis 和本机模拟 Responses 服务，不消耗真实模型额度。完整异步运行需启动 Redis，设置与 Compose 相同的令牌、加密密钥及 `REDIS_URL`，然后分别运行 `python start.py` 和 `python detection_worker.py`。没有 `REDIS_URL` 时，本地网页沿用同步测试流程（同样只用 Responses）。

## GitHub Pages

`static/index.html` 是不依赖后端的手动测试版本，归因计算和指纹库读取都在浏览器本地完成。仓库附带的 GitHub Actions 会将 `static/` 部署到 GitHub Pages。

## 使用

- **手动测试**：复制三条挑战，分别发送给同一个待测模型，再粘贴每次完整输出。
- **API 自动测试**：填写 Base URL、API Key 和模型名。仅使用 Responses 接口，以三份有效回答为目标完成归因；不足三份时返回标注“样本不足”的初步结果。
- **指纹库管理**：可以新建指纹库，或通过 API 为现有指纹库添加模型指纹。

本地同步模式不保存 API Key。Docker 异步模式中，上游 API Key 使用独立密钥加密后临时存入 Redis；任务终止或凭据过期后删除。Key 不进入任务结果、浏览器存储或日志。Redis AOF/备份中可能暂留加密历史记录。
自动采集会在对应的 `*_reference.jsonl` 中保存实际 user prompt、base prompt、system prompt 和 user prefix；拟合后的 `*_bank.json` 与 `unified_bank.json` 只保存统计指纹和校准参数。

## 指纹方法

### Codex 任务内监测插件

[ModelTrace Guard](codex-plugin/modeltrace-guard/README.md) 按工具调用次数，在后台从原 Codex 任务的冻结快照分别 fork 进行普通检测和异常复测，测后清理临时分支。正常结果静默保存，主任务无需等待检测。用户可调整工具间隔、复测次数（默认 3）、有效期和十种语言，不设每轮或任务累计探针上限。网页仪表盘提供完整历史分页和实时提醒；首次不一致要求智能体告知并暂停工作，额外复测全部不一致时停止原任务并拦截后续受支持的工作工具。探针由 Codex 自身生成并消耗对应推理额度，评分使用插件附带的 ModelTrace 指纹库，页面刷新不消耗模型额度。

安装后在 Codex CLI 的 `/hooks` 中审阅并信任插件 hooks，再在目标任务中开启监测。安装、配置和结果解释详见[插件使用说明](codex-plugin/modeltrace-guard/README.md)。

### 核心算法

所有模型使用同一个全局特征空间、同一套特征与权重：

```text
0.75 × 去除环境方向后的 Hellinger 模型中心相似度
+ 0.25 × 有序块数字序列特征
```

建库时使用共享环境的平均偏移估计主要干扰方向。归因时先从数字分布和有序特征中投影掉这些方向，再同时与全部模型中心比较。三份回答分别评分后取平均，并使用与查询数量对应的校准温度转换为概率。

具体模型概率由一次全局 softmax 得到，家族概率是该家族下各模型概率之和：

```text
P(具体模型) = softmax(β × 全局模型得分)
P(模型家族) = Σ P(该家族中的具体模型)
```

结果是当前候选库内、均匀先验下的闭集概率。未收录模型仍会被归到最相似的现有候选。

## 项目结构

```text
app.py              Web 接口
fingerprint.py      指纹提取与归因
bank_builder.py     指纹库构建与概率校准
challenge_suite.py  自动建库挑战
enrollment.py       API 调用与指纹采集
rebuild_unified_bank.py  重建统一全局库
data/               参考数据与指纹库
static/             页面资源
templates/          页面模板
```

## 指纹库说明

项目中现有指纹库共包含两个模型家族、18 个模型：

```
gpt-5.4
gpt-5.5
gpt-5.6-luna
gpt-5.6-terra
gpt-5.6-sol
gpt-6-astra
gpt-6-sol
gpt-6-luna
claude-haiku-4-5-20251001
claude-haiku-5-5
claude-sonnet-4-6
claude-sonnet-5
claude-sonnet-5-5
claude-opus-4-6
claude-opus-4-7
claude-opus-4-8
claude-opus-5
claude-opus-5-5
```

GPT 采集自官方订阅 Codex，Claude 采集自 [OAIPro](https://api.oaipro.com/)。

## 声明

测试结果仅供参考，并非判断模型的决定性证据

注意该项目为归因工具，只对指纹库内的模型尽可能去判断属于哪种模型，若待测模型不在指纹库中，得到任何结果都是有可能的

目前已知因系统提示词严重影响模型偏好，在Claude Code中得到的测试结果存在较大偏差，建议不要在Claude Code中测试

## 致谢

感谢 [hanlinwenyuan/hlwy-ai-checker](https://github.com/hanlinwenyuan/hlwy-ai-checker)。该项目较早将语言模型的随机数字生成偏差用于第三方 API 渠道一致性检查，为 ModelTrace 提供了重要参考。



[LINUX DO](https://linux.do/)
