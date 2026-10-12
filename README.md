# ModelTrace

ModelTrace 是一个本地运行的主动模型归因工具。它通过三条独立的长整数生成挑战提取输出指纹，在统一候选库中自动判断模型家族和具体版本。

## 运行

```powershell
python -m pip install -r requirements.txt
python start.py
```

页面地址为 `http://127.0.0.1:10001/`，自动进入公开首页；管理后台为 `/admin`。监测需要 Redis、worker 和 scheduler；完整服务请使用下方 Docker 启动方式。

## Docker 异步服务

服务包含 Flask/Gunicorn API、默认 3 个独立 worker、服务器定时调度器和带 AOF 持久化的 Redis。每个 worker 同时执行一个任务，默认最多并行检测 3 项；任务使用现有统一指纹库和归因算法。浏览器自动测试入口也会提交异步任务。

### 启动

在安装了 Docker Compose v2 的服务器上：

```sh
docker compose up -d --build
```

启动后直接打开 **`http://服务器公网IP:10001/status`**，无需创建或修改 `.env`，也无需添加 `--scale` 参数就会启动 3 个 worker。默认监听 `0.0.0.0:10001`，服务器防火墙或云平台安全组需允许 TCP 10001 端口。

首次启动会自动生成并保存两个密钥，不需要手动运行 Python 命令，也不需要预先创建 `.env`。它们的用途分别是：

- **管理令牌**：仅供旧版管理接口和手动测试使用；新的 `/api` 四参数调用不需要。
- **凭据加密密钥**：由程序内部使用，用来加密暂存在任务队列中的上游 API Key，调用方不需要填写。

如需使用旧版管理接口，可以执行下面的命令查看管理令牌；普通 `/api` 调用可跳过：

```sh
docker compose exec api python runtime_config.py token
```

自动生成的密钥保存在独立的 `runtime-data` 持久卷中，API 和 worker 共用，重启不会改变。请与 `redis-data` 一起保留和备份；删除密钥卷会导致现有令牌失效、旧任务凭据无法解密。启动日志不会打印密钥。如需自定义端口、超时或密钥，再复制 `.env.example` 为 `.env` 修改即可；显式填写的密钥优先于自动生成值。

Docker 默认对外监听 `0.0.0.0:10001`，也可通过反向代理提供 HTTPS。如需限制为服务器本机访问，可在 `.env` 中设置 `BIND_ADDRESS=127.0.0.1`；已有 `.env` 或环境变量中的显式设置仍优先于默认值。网页的 API 自动测试只需要填写 URL、Key、模型和思考强度。旧版 `/api/*` 管理接口仍使用 `MODELTRACE_API_TOKEN`；新的 `/api` 入口不要求该令牌。

Redis 不发布宿主机端口。任务和结果保存在 `redis-data` 卷；普通容器重建不会删除数据，`docker compose down -v` 会删除。AOF 每秒同步，主机突然掉电仍可能损失最近约一秒的状态；应用不会自动重试未知提交结果。

```sh
# 可选：将检测并发从默认 3 提高到 5；每个任务内仍串行采样。
docker compose up -d --scale worker=5
# 查看健康状态、运行日志（不输出请求体或上游 Key）。
docker compose ps
docker compose logs --tail=100 api worker
```

服务器模式下指纹库只读，旧的同步上游测试、自动采集和建库写接口返回 409。在本地维护 `data/` 后重新构建并重启 API 和 worker，更新参考库；每个任务记录实际使用的 `bank_sha256`。手动测试和指纹库查看继续可用。GitHub Pages 静态站点仍为手动版本。

### 构建时依赖下载中断

如果构建日志在 `pip install` 阶段出现 `Broken pipe`、`Connection broken` 或下载超时，说明下载连接中断，此时应用尚未启动。旧版 pip 还存在遇到 `ProtocolError` 时无法进入续传逻辑的问题，仅重试整轮安装可能反复失败；该问题已在 [pip 26.2 中修复](https://pip.pypa.io/en/stable/news/#v26-2)。

Docker 构建会先升级到 pip 26.2.1，再安装依赖：使用 120 秒网络超时、5 次连接重试，并允许每个中断的下载最多续传或重启 10 次。pip 升级和依赖安装各有最多 3 次整轮尝试，同一次构建中已完成的下载会缓存供下一次尝试使用。持续无法连接的软件源仍需排查网络或更换源。

先更新代码并重新构建：

```sh
git pull --ff-only && \
docker compose build api && \
docker compose up -d --no-build
```

`&&` 确保构建成功后才启动服务。若构建失败后仍执行启动命令，可能出现 `No such image: modeltrace:local` 或 `pull access denied`；这是本地镜像尚未构建成功造成的，不需要登录镜像仓库。

API、worker 和 scheduler 共用同一个镜像，构建 `api` 一次即可。默认使用官方 PyPI，英国等海外服务器可保留默认源。如果服务器到官方 PyPI 的连接不稳定，可选择服务器能稳定访问的镜像源，例如在项目 `.env` 中添加或修改：

```dotenv
PYPI_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
```

也可以仅为本次构建指定镜像源：

```sh
PYPI_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple docker compose build api && \
docker compose up -d --no-build
```

这里使用的是 [清华 PyPI 镜像](https://mirrors.tuna.tsinghua.edu.cn/help/pypi/)，也可设置为服务器能稳定访问的其他 HTTPS 包索引地址。该配置仅影响构建时的 Python 包下载，不改变运行时上游模型地址。不需要升级服务器宿主机的 pip，也不要删除持久化卷来解决下载失败。

### 公开首页与管理后台

`/`（跳转到 `/status`）和 `/status` 是公开首页，任何人都能查看管理员精选监测与下方公共检测列表，无需注册。

- **精选监测**：仅展示管理员明确公开的项目，按展示顺序排列，自动更新最新完成结果，提供候选排名与最近 20 轮历史。
- **公开检测**：用户填写自己的 URL、API Key、模型和思考强度，提交单次检测。网站提交的进度和结果进入公共列表，所有人可查看；不开放用户定时监测、配置编辑、启停或删除。
- **数据展示**：公开响应不包含 Key、完整接口地址、内部任务 ID、原始上游错误或内部配置。Key 在任务结束后清理。网站公共结果使用独立公开 ID，默认保留终态结果 24 小时，每页 20 条。
- **权限**：监测管理接口 `/status/api/*` 全部要求管理员身份。监测 ID 或旧面板链接不会授予管理权限。现有四参数 `/api` 调用不会自动进入网站公共列表；管理员监测任务不能通过公共 `/api` 查询或取消。

#### 管理员首次登录

打开 **`http://服务器地址:10001/admin`**，未登录时进入管理令牌登录页。普通用户不需要此令牌。在服务器项目目录获取管理令牌：

```sh
docker compose exec api python runtime_config.py token
```

使用输出的令牌登录。浏览器只保存 HttpOnly、SameSite=Lax 的会话 Cookie，不把管理令牌保存到浏览器存储；会话有效期 8 小时，退出立即失效，更换管理令牌后旧会话也会失效。HTTPS 下 Cookie 自动启用 Secure。后台写请求校验 CSRF，现有服务令牌的 Bearer 调用仍可管理受保护接口。

#### 管理监测与首页

1. 在后台点击「新增监测」，填写 URL、Key、模型、思考强度和检测间隔（1 分钟～7 天）。监测名称供后台区分供应商，Key 加密保存在服务器。
2. 保存配置不会自动产生模型调用。在左侧列表或当前详情点击「立即检测」或「开启监测」。编辑时 Key 留空保留原值，更换接口地址必须重新填写 Key。
3. 同一供应商的其他模型可通过「复用连接」新增；Key 由服务器复制，不会回传浏览器。副本的开关、配置与历史独立，默认暂停且不公开。
4. 在新增或编辑对话框勾选「在公开首页展示此监测」，填写公开名称与展示顺序。数字越小越靠前，同序时按创建时间排序；公开名称留空显示模型名称。公开与启停独立，取消公开不会暂停检测。
5. 「首页与开放设置」可修改标题、说明、纯文本公告及匿名检测开放开关和限制。关闭匿名检测会拒绝新的网站和四参数 API 提交，已提交任务继续处理，已有结果仍可查看。
6. 「公共检测管理」可隐藏或恢复记录。隐藏后公开列表和公开详情接口都不再展示，后台仍可查看，不改变该检测任务的执行状态。
7. 不再需要的监测可点击左侧卡片「删除」并确认；删除配置、保存的 Key 和监测历史，停止后续检测并取消当前任务。已发出的上游请求仍可能计费。删除当前项后自动切换到其他项。

**关闭浏览器后，管理员定时检测仍会继续。** 每项同一时间最多运行一轮，下一轮从上一轮完成被调度器确认后开始计算间隔。暂停只停止后续定时检测，当前检测仍会完成；重启保留配置、开关和历史，不补跑停机期间错过的多轮。

匿名检测默认每 IP 每小时 5 次、最多 1 个未完成任务、公共排队最多 10 项、公共执行并发最多 2。管理员可在后台调整。限流保存在 Redis，多个 API 进程共享；网站与四参数 `/api` 共享匿名额度，超限返回 `429`。公共任务与管理员任务分别排队，worker 优先处理管理员任务；默认 3 个 worker 下，公共并发 2 为管理员保留一个执行名额。修改 worker 数量时同步调整公共并发，避免将所有执行名额分配给公共请求。

匿名调用始终禁止连接本机、内网和保留 IP，即使服务器为管理员配置了 `ALLOW_PRIVATE_UPSTREAM=true`；现有域名允许列表仍对全部任务生效。

#### 升级已有部署

```sh
git pull --ff-only && \
docker compose build api && \
docker compose up -d --no-build
```

首次请求或调度器启动会执行幂等迁移。旧监测配置、Key、启停状态、任务和历史保留，**默认不公开**；管理员登录后按需勾选公开展示。旧版未标注任务来源、且已不在监测最近历史中的记录无法可靠区分来源，升级后仅管理员可查询；任务状态和结果仍保留，四参数 API 的新提交协议不变。旧共享面板继续用于后台；需要继续使用原面板书签时，将 `/status#dashboard=...&monitor=...` 或 `/status#monitor=...` 的页面路径改为 `/admin`，并先登录。

保留 `redis-data` 与 `runtime-data` 持久卷。首页设置、公开标记及会话使用现有 Redis；API、worker、scheduler 需一起更新到同一版本。API 与 worker 共用镜像，构建 `api` 一次即可。

#### 对外 HTTPS 与反向代理

正式公开访问通过 HTTPS 提交管理令牌和用户 Key。若服务器前面只有一层可信 HTTPS 代理，项目 `.env` 设置：

```dotenv
BIND_ADDRESS=127.0.0.1
TRUSTED_PROXY_HOPS=1
```

代理将请求转发到 `http://127.0.0.1:10001`，传递真实客户端 IP 和协议。例如已有 HTTPS Nginx 站点中的代理配置：

```nginx
location / {
    proxy_pass http://127.0.0.1:10001;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

更新容器后通过 HTTPS 域名访问。IP 限流使用真实客户端地址，管理员 Cookie 使用 Secure。默认 `TRUSTED_PROXY_HOPS=0` 不信任客户端提交的转发头；只有后端无法被绕过代理直接访问时，才配置实际的可信代理层数。[Flask 代理配置说明](https://flask.palletsprojects.com/en/stable/deploying/proxy_fix/)

若后台显示「调度服务离线」，检查 `docker compose ps` 与 `docker compose logs --tail=100 scheduler worker`。本地开发需让 API、worker、scheduler 使用相同的 Redis 地址和加密密钥。

### API 调用说明：`/api`

**只需提供 `url`、`key`、`model`、`reasoning_effort` 四个参数，不需要管理令牌或 Authorization 请求头。** 匿名开放开关、每 IP 限额及公共队列限制同网站检测，关闭开放时提交返回 `403`，超限返回 `429`；这些 API 任务不会自动加入网站公共列表。检测是异步的：提交一次任务，获得任务 ID，再查询进度和结果。

本文示例假设在部署服务器本机调用，地址为 `http://127.0.0.1:10001`。Docker 默认对外监听 10001 端口，从其他机器调用时直接替换为 `http://服务器IP:10001` 或反向代理域名即可，无需额外配置监听地址。

这里的检测服务地址，与请求体中的上游模型服务 `url` 是两个不同的地址。

| 操作 | 方法与路由 | 参数位置 |
|---|---|---|
| 提交检测 | `POST /api` | JSON 请求体中的四个参数 |
| 查询进度或结果 | `GET /api?id=任务ID` | URL 查询参数 `id` |
| 取消检测 | `DELETE /api?id=任务ID` | URL 查询参数 `id` |

#### 1. 提交检测

请求头必须包含 `Content-Type: application/json`，四个参数放在 JSON 请求体中，不要把 Key 放进 URL 查询参数。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `url` | 字符串 | 是 | **待测模型服务**的地址，支持根地址、`/v1` 或完整 `/v1/responses` 地址 |
| `key` | 字符串 | 是 | 该模型服务的 API Key，不是 ModelTrace 管理令牌 |
| `model` | 字符串 | 是 | 上游接受的模型名或别名，仅用于指定调用目标，不作为指纹识别依据 |
| `reasoning_effort` | 字符串 | 是 | 思考强度，例如 `high`；可选值见下方 |

`url`、`key` 和 `model` 不能为空。只接受上述四个字段；不要附加提示词、温度或其他参数，检测提示词由服务自动生成。

```sh
curl -i -X POST http://127.0.0.1:10001/api \
  -H 'Content-Type: application/json' \
  -d '{
    "url": "https://example.com/v1",
    "key": "替换为你的上游API Key",
    "model": "替换为上游接受的模型名",
    "reasoning_effort": "high"
  }'
```

`reasoning_effort` 支持 `none`、`minimal`、`low`、`medium`、`high`、`xhigh`、`max`。每轮原样传给 Responses 的 `reasoning.effort`。具体可用档位取决于待测模型，不支持时任务会记录上游错误，不会自动降低强度。[官方思考强度说明](https://developers.openai.com/api/docs/guides/reasoning)

成功提交返回 **HTTP 202**，表示已接收任务，**不代表检测完成**。以下为省略部分字段的示例：

```json
{
  "id": "0123456789abcdef0123456789abcdef",
  "status": "queued",
  "status_url": "/api?id=0123456789abcdef0123456789abcdef",
  "progress": {"target": 3, "valid": 0, "attempted": 0, "max_attempts": 6},
  "result": null
}
```

保存响应中的真实 `id`，或直接用检测服务地址拼接 `status_url`。响应头也会提供 `Location` 查询路径和 `Retry-After: 2`。

**提交成功后不要重复 POST 来等待结果。** 每次 POST 都会创建新任务，可能产生新的上游调用费用。如果提交时连接中断且未拿到任务 ID，提交结果未知；目前不支持幂等提交。

#### 2. 查询进度和结果

将下面的 ID 替换为提交响应中的真实 ID，建议每两秒查询一次：

```sh
curl 'http://127.0.0.1:10001/api?id=0123456789abcdef0123456789abcdef'
```

查询成功返回 **HTTP 200**。先读取 `status` 决定是否继续查询，再读取 `result` 获取归因结果：

| `status` | 是否结束 | 含义 |
|---|---|---|
| `queued` | 否 | 等待 worker 执行，继续查询 |
| `running` | 否 | 正在采样，继续查询；`progress` 显示进度 |
| `completed` | 是 | 已获得三份有效样本，`result` 是完整结果 |
| `partial` | 是 | 已结束但只有一至两份有效样本，`result` 是标注“样本不足”的初步结果 |
| `failed` | 是 | 没有有效样本或排队超时，`result` 为 null |
| `cancelled` | 是 | 任务已取消；可能保留取消前取得的结果 |
| `interrupted` | 是 | worker 中断；可能保留中断前取得的结果，不自动重跑 |

运行中的任务也可能带有初步 `result`，因此不要仅以 `result` 是否为空判断完成。**出现任一终态就停止轮询**，包括 `partial`、`failed`、`cancelled` 和 `interrupted`。

完整结果示例（以下模型名称及概率仅为格式示意，不是实际测试结论；省略了其他字段和候选）：

```json
{
  "status": "completed",
  "reason": "target_reached",
  "progress": {"target": 3, "valid": 3, "attempted": 3, "max_attempts": 6},
  "preliminary": false,
  "insufficient_samples": false,
  "elapsed_seconds": 125.4,
  "result": {
    "prediction": "candidate-model-a",
    "prediction_name": "候选模型 A",
    "probability": 0.92,
    "used_outputs": 3,
    "sample_warning": null,
    "results": [
      {"model": "candidate-model-a", "display_name": "候选模型 A", "probability": 0.92, "profile_similarity": 0.81}
    ]
  }
}
```

`result.prediction_name` 是最可能的模型，`result.results` 是按归因概率从高到低排列的候选列表。概率和相似度使用 0～1 的数值，例如 `0.92` 表示 92%。这是现有指纹库内的判断，不能当成模型真实身份的绝对证明。

样本不足示例（同样省略部分字段）：

```json
{
  "status": "partial",
  "reason": "attempts_exhausted",
  "progress": {"target": 3, "valid": 2, "attempted": 6, "max_attempts": 6},
  "preliminary": true,
  "insufficient_samples": true,
  "result": {
    "prediction": "candidate-model-a",
    "prediction_name": "候选模型 A",
    "used_outputs": 2,
    "preliminary": true,
    "insufficient_samples": true,
    "sample_warning": "样本不足，当前为初步结果"
  }
}
```

`partial` 也是结束状态，不会在后台继续补齐三份。可通过 `errors` 查看失败轮次，通过 `reason` 查看结束原因。

#### 3. 取消检测

```sh
curl -X DELETE 'http://127.0.0.1:10001/api?id=0123456789abcdef0123456789abcdef'
```

返回 HTTP 200 和当前任务状态。未结束的任务转为 `cancelled`；已结束的任务保持原有状态，重复取消不会重新执行。取消会停止后续采样，已提交给上游的请求仍可能计费。

#### 4. 请求错误与任务失败

| HTTP 状态码 | 含义 | 处理方式 |
|---|---|---|
| `202` | 提交成功 | 保存任务 ID，开始查询 |
| `200` | 查询或取消成功 | 检查响应体的 `status`，不代表模型检测一定成功 |
| `400` | 参数缺失、格式错误或思考强度无效 | 根据响应的 `error` 修正请求 |
| `404` | 任务 ID 无效、任务不存在或结果已过期 | 检查 ID；默认终态结果保留 24 小时 |
| `429` | 等待队列已满 | 按 `Retry-After` 建议稍后提交 |
| `503` | 服务配置或 Redis 暂不可用 | 检查服务状态；查询失败时不需要重新提交任务 |

接口级错误示例：

```json
{"error": "请提供四个参数：url、key、model、reasoning_effort"}
```

上游 Key 错误、模型不支持所选思考强度、上游超时等发生在**后台任务**中：提交仍可能返回 202，随后查询返回 HTTP 200，但任务为 `failed` 或 `partial`。请查看 `errors` 和 `reason`，不要只检查 HTTP 状态码。

任务 ID 用于查询和取消，请仅分享给需要访问结果的人。`/api` 无需管理令牌；提交任务的人提供自己的上游 Key。旧版受保护接口创建的任务不能通过公共 `/api` 查询或取消。

### 返回字段说明

下方字段适用于 `/api` 和兼容接口的任务响应。

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

参数错误返回 400，任务不存在或结果过期 404，队列满 429，Redis 不可用 503；兼容管理接口鉴权失败时额外返回 401。`GET /healthz` 无需鉴权，仅检查 API 与任务存储是否可用，不代表 worker 已上线。

### 兼容接口：`/api/detections`

新接入请优先使用上面的 `/api`。以下为保留的兼容接口，参数名称和鉴权方式不同，不要与四参数入口混用。

`SERVICE_TOKEN` 是自动生成或自行设置的管理令牌，不是上游 Key；可通过前文的 `runtime_config.py token` 命令查看。

```sh
curl -i http://127.0.0.1:10001/api/detections \
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
| `reasoning_effort` | 否 | 思考强度；取值同上，省略则使用上游默认 |
| `temperature` | 否 | 0～2；默认不传，由上游使用默认值。上游不支持此参数时应省略 |

```sh
# 查询：运行中与终态均返回 200。
curl http://127.0.0.1:10001/api/detections/TASK_ID \
  -H "Authorization: Bearer $SERVICE_TOKEN"
# 取消：重复取消不会重新执行，也不会覆盖已有终态。
curl -X DELETE http://127.0.0.1:10001/api/detections/TASK_ID \
  -H "Authorization: Bearer $SERVICE_TOKEN"
```

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

Docker 默认自动生成并共享 `MODELTRACE_API_TOKEN` 与 `CREDENTIAL_ENCRYPTION_KEY`，无需手动设置；自定义时也须使用两个独立密钥，且 API/worker 保持一致。不要把真实密钥提交到仓库。更换加密密钥前先清空等待任务，否则旧任务凭据无法解密。上游默认不能访问本机、内网和保留地址；连接使用校验后的 IP 并保留 TLS 主机校验，禁用 HTTP 重定向及环境代理，防止 Key 被重定向到其他主机。

### 开发与测试

```sh
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

可选的页面集成测试会启动本机测试服务，使用模拟模型响应验证设置、定时监测、关闭页面后执行，以及桌面/手机布局：

```sh
python -m pip install -r requirements-browser.txt
python -m playwright install chromium
python tests/status_browser.py
python tests/public_browser.py
```

后台截图默认输出到 `/tmp/modeltrace-status-preview`，公开页面截图输出到 `/tmp/modeltrace-public-preview`，可用 `STATUS_SCREENSHOT_DIR` 修改输出目录。

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
