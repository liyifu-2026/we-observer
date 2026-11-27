# wx-observer

面向 Windows 10 / 11 微信 4.x（`Weixin.exe`）的**纯只读**本地聊天记录提取与实时消息观察工具，原生提供 CLI、MCP Server（`stdio` / Streamable HTTP）以及极简本地实时监控面板。

> [!CAUTION]
> **免责与学习声明（仅供学习研究使用）**
>
> - 本项目**仅供技术学习、学术研究与个人本机数据备份参考**，严禁用于任何商业用途、自动化营销、批量采集或违反相关软件服务协议及法律法规的场景。
> - 本项目严格恪守**纯只读边界**：不发送任何消息、不模拟键鼠或 UI 点击、不注入 DLL、不 Hook 目标进程、不写入目标进程内存，也不包含针对微信二进制特征码或版本固定 RVA 偏移的提取逻辑。
> - 使用本工具即表示您完全理解并自行承担因使用、修改或分发本代码所产生的一切合规与法律责任；作者不对任何直接或间接后果承担责任。

---

## 核心特性

- **后台账号实时识别与无状态查询**：动态枚举本机运行中的 `Weixin.exe` 进程及其打开的 `db_storage` 句柄，自动定位自定义/迁移后的存储目录，区分在线与离线账号；各读取接口传入显式 `account` 参数时独立执行、不改写进程默认绑定状态，并按账号/游标分段加锁。
- **紧凑型历史提取与关键词片段检索**：跨 `message_*.db` 分片合并查询，按分片独立 `Name2Id` 精确还原发送者，支持按 `chat_id` / 备注 / 昵称 / 群名模糊匹配，支持相对时间窗（如 `24h`、`7d`、`30m`）与 ISO/时间戳区间过滤。MCP 默认启用 `detail="compact"`、`max_text_chars=300`（`detail="full"` 不截断）与 `skip_noise=True`，检索自动返回围绕关键词的 `snippet`、`match_offset` 及 `est_tokens` 估算值；CLI 默认保持全字段输出（可用 `--compact` / `--skip-noise` 切换，支持导出 JSON / JSONL / CSV / Markdown）。
- **服务端跨会话摘要与本地附件定位**：新增 `digest` 工具在服务端一次性聚合私聊/群聊消息统计、按小时/按天分布、`top_senders`、URL 外链、`@提及` 与高信号摘要；新增 `list_attachments` 解析群/私聊文件及转发聊天记录（`chat_history`）内嵌文件对应的本地明文路径，支持 Windows `(1)/(2)` 重名后缀与 MD5/文件大小置信度校验。
- **至少一次（At-Least-Once）实时增量监控**：基于 SQLite `-wal` 增量帧合并与 `sort_seq` 组原子投递，各 `cursor_id` 独立持久化断点防重启漏消息；MCP 端 `poll_new_messages` / `wait_for_messages` 默认 `peek=True` 返回 `ack_token`，消费完成后调用 `ack_messages` 才推进持久化水位，防止 Agent 超时或压缩上下文时丢消息；CLI `watch` 与 Web UI SSE 保持连续自动提交。
- **Agent 原生 MCP 接入**：内置标准 MCP Server，提供 12 个标注 `readOnlyHint=true` 的只读工具，同时支持 `stdio`（客户端拉起）与本机 `127.0.0.1` Streamable HTTP（可选 Bearer Token 鉴权）。
- **极简本地 Web 控制台**：提供零外部 CDN、纯只读的单页控制台（`wx-observer ui`），支持切换在线账号、勾选要监控的私聊/群聊并实时滚动推送新消息。

---

## 工作原理

```mermaid
sequenceDiagram
    autonumber
    participant Client as Agent / CLI / Web UI
    participant Svc as ObserverService
    participant Disc as Discovery & KeyStore
    participant Pool as SQLCipher4 + WAL Pool
    participant Reduce as Shaping / Digest / Attachments
    participant State as CursorStateStore

    Client->>Svc: 请求账号列表或历史 / 摘要 / 附件 (可显式指定 account)
    Svc->>Disc: 枚举 Weixin.exe 进程与本地目录，按 DPAPI 缓存或只读内存取密钥
    Disc-->>Svc: 按账号加锁初始化并复用只读实例 (不覆盖默认账号)
    Svc->>Pool: 校验主库与 -wal 变更并增量合并至会话临时目录
    Pool-->>Reduce: 返回原始消息、转发记录与文件元数据
    Reduce-->>Client: 输出 compact/full 消息、snippet、est_tokens 或本地附件路径
    Client->>Svc: poll_new_messages / wait_for_messages (MCP 默认 peek=True)
    Svc-->>Client: 返回增量批次与 ack_token (此时尚未推进持久化水位)
    Client->>Svc: ack_messages(cursor_id, ack_token)
    Svc->>State: 原子提交该 cursor_id 的 sort_seq 水位断点
```

1. **进程与目录发现**：通过 Win32 窗口/进程枚举及文件句柄映射，只读确认当前后台运行中的 `Weixin.exe` 所绑定的账号与真实数据路径。
2. **只读密钥获取与缓存**：优先读取经 Windows DPAPI 按当前登录用户加密的本地缓存（或用户提供的 `--keys-file`）；缺失时通过只读 `ReadProcessMemory` 查找通用 WCDB 配置对象并经 SQLCipher 4 页 1 HMAC-SHA512 强校验，验证通过才使用。
3. **临时解密、归约与附件定位**：将主库按页解密至独立会话临时目录，检测到 `-wal` 新增帧且 `wal_salt` 一致时仅做增量合并，并经 `PRAGMA quick_check` 校验完整性；服务端直接完成紧凑裁剪、跨群聚合与本地附件路径匹配，程序退出时自动清理临时解密文件。
4. **安全游标推进与多端隔离**：MCP 客户端默认使用 `peek=True` 拉取新消息并凭 `ack_token` 调用 `ack_messages` 原子提交水位；`cursor_id` 之间互不覆盖，支持多账号并发访问。

---

## 环境要求

- **操作系统**：Windows 10 / Windows 11（64 位）
- **Python**：64 位 Python 3.11 及以上（需与 64 位微信进程匹配）
- **目标客户端**：桌面端微信 4.x（`Weixin.exe`）
- **权限要求**：`wx-observer` 需与微信运行在同一 Windows 用户权限级别（请勿单独以“管理员身份”运行微信）

---

## 复制给 AI Agent 一键安装并接入 MCP

如果你想让 AI 编程助手（如 Claude Code、Cursor、Antigravity 等）替你完成下载、虚拟环境配置、可用性验证和 MCP 接入，直接复制下面这段提示词发给它即可：

```text
请帮我在本地安装并配置开源项目 https://github.com/liyifu-2026/we-observer.git （纯只读微信 4.x 观察与 MCP 工具）：

1. 确认环境为 Windows 64 位且已安装 Python 3.11+ 与 git；将仓库克隆到合适目录（例如 C:\Tools\wx-observer 或当前工作区旁边的 wx-observer）。
2. 在仓库内创建独立虚拟环境 .venv，升级 pip/setuptools/wheel 后执行可编辑安装（`pip install -e ".[dev]"`）；若系统 Python 的 `ensurepip` 报错请用 `python -m venv --without-pip .venv` 并使用宿主 `python -m pip --python .\.venv\Scripts\python.exe` 引导安装，若直连 PyPI 较慢可加镜像参数 `-i https://pypi.tuna.tsinghua.edu.cn/simple`。
3. 运行单元测试（pytest -v tests）确认所有离线测试通过。
4. 运行 `<venv>\Scripts\wx-observer.exe accounts --json` 验证命令可正常执行（如当前本机未打开微信或未登录，只需如实告知我）。
5. 为当前客户端配置好该 MCP Server（stdio 模式）：
   - command 指向 `<repo>\.venv\Scripts\wx-observer.exe`
   - args 为 `["mcp", "--transport", "stdio"]`
6. 如需独立多客户端接入，顺便告诉我如何用 `--transport http --host 127.0.0.1 --port 8765 --token <自定义密钥>` 启动 Streamable HTTP 模式，并验证 MCP 工具列表可用。
```

---

## 手动安装与快速开始

### 1. 克隆并安装

```powershell
git clone https://github.com/liyifu-2026/we-observer.git
cd we-observer
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip setuptools wheel
.\.venv\Scripts\pip.exe install -e ".[dev]"
```

> [!NOTE]
> - 若当前系统 Python 的 `ensurepip` 精简缺失导致 `python -m venv .venv` 报错，可改用 `--without-pip` 并通过宿主 Python 引导安装虚拟环境内的 `pip`：
>   ```powershell
>   python -m venv --without-pip .venv
>   python -m pip --python .\.venv\Scripts\python.exe install --upgrade pip setuptools wheel
>   ```
> - 若网络直连官方 PyPI 较慢或超时，可在 `pip install` 后追加镜像源 `-i https://pypi.tuna.tsinghua.edu.cn/simple`。

运行离线单元测试：

```powershell
.\.venv\Scripts\pytest.exe -v tests
```

### 2. CLI 常用命令

```powershell
# 查看后台正在运行的微信账号与本地历史账号
.\.venv\Scripts\wx-observer.exe accounts --json

# 列出私聊与群聊（支持 --type private|group 和 --query 模糊搜索）
.\.venv\Scripts\wx-observer.exe chats --limit 20

# 提取指定会话历史（支持传入 wxid、备注、昵称或群名）
.\.venv\Scripts\wx-observer.exe history "会话名或wxid" --limit 50

# 导出历史记录为 CSV / Markdown / JSONL
.\.venv\Scripts\wx-observer.exe history "会话名或wxid" --limit 200 --export md --output chat.md

# 跨会话全文搜索
.\.venv\Scripts\wx-observer.exe search "关键词" --type all --limit 50

# 实时监控新消息（JSONL 流式输出，默认不含自己发出的消息）
.\.venv\Scripts\wx-observer.exe watch --mode all --interval 0.5 --jsonl

# 紧凑模式读取历史（过滤表情/系统消息，查看 est_tokens）
.\.venv\Scripts\wx-observer.exe history "产品研发群" --since 24h --compact --skip-noise --json

# 生成最近 24 小时跨会话聚合摘要
.\.venv\Scripts\wx-observer.exe digest --scope all --since 24h

# 列出最近 7 天聊天与转发记录中的附件及其本地明文路径
.\.venv\Scripts\wx-observer.exe attachments --since 7d --only-available
```

### 3. 启动极简实时监控 Web UI

```powershell
.\.venv\Scripts\wx-observer.exe ui
```

启动后仅监听 `http://127.0.0.1:8766/` 并自动打开浏览器，可用于在页面中切换后台账号、勾选需要盯防的私聊或群聊，并实时滚动查看新消息（支持 `--no-open` 与 `--token`）。

### 4. 配置 MCP Server

#### 方式一：`stdio` 模式（推荐，由 Agent 客户端自动拉起）

```json
{
  "mcpServers": {
    "wx-observer": {
      "command": "C:\\path\\to\\we-observer\\.venv\\Scripts\\wx-observer.exe",
      "args": ["mcp", "--transport", "stdio"]
    }
  }
}
```

#### 方式二：`Streamable HTTP` 模式（独立常驻，支持多客户端共享）

```powershell
.\.venv\Scripts\wx-observer.exe mcp --transport http --host 127.0.0.1 --port 8765 --token "replace-with-your-secret"
```

客户端连接 `http://127.0.0.1:8765/mcp`，请求头携带 `Authorization: Bearer replace-with-your-secret`。

#### MCP 只读工具列表

| 工具名 | 作用说明 |
| --- | --- |
| `list_accounts` | 扫描后台 `Weixin.exe` 进程与本地账号目录，区分在线/离线与 PID |
| `select_account` | 按 `account` 或 `pid` 绑定默认目标账号（支持传入外部 `keys_file`；其他读取工具显式传 `account` 时互不覆盖） |
| `list_chats` | 列出私聊/群聊，支持 `chat_type` 与昵称/备注/群名模糊过滤，附带 `est_tokens` |
| `get_messages` | 提取指定会话历史，支持 `before_seq`/`after_seq` 翻页、相对/ISO 时间窗（`24h`/`7d`）、类型与发送者筛选；默认 `detail="compact"`（300 字符上限）与 `skip_noise=True`，可切 `detail="full"` |
| `search_messages` | 按关键词检索指定会话或全库消息（含压缩长文），默认 `compact` 仅返回围绕匹配词的 `snippet` 与 `match_offset` |
| `digest` | 服务端跨会话聚合摘要，按范围与时间窗输出计数、小时/天活跃桶、`top_senders`、URL 外链、附件、提及与精选高亮 |
| `list_attachments` | 列出聊天文件与转发记录内嵌文件，并按路径安全边界、同名 `(1)` 后缀、文件大小与 MD5 解析本地明文路径 |
| `configure_watch` | 为独立 `cursor_id` 配置监控范围（`all`/`private`/`group`/`allowlist`、排除列表、是否含自己） |
| `poll_new_messages` | 非阻塞拉取 `cursor_id` 增量新消息；默认 `peek=True` 返回 `ack_token` 而不推进水位，`detail="compact"`、`skip_noise=True` |
| `ack_messages` | 使用 `poll_new_messages` / `wait_for_messages` 返回的 `ack_token`（及可选 `upto`）原子提交该 `cursor_id` 水位 |
| `wait_for_messages` | 长轮询等待新消息（收到即返回，超时返回空列表）；默认 `peek=True` 与 `detail="compact"` |
| `get_status` | 查看目标账号状态、解锁数据库数、监控过滤规则与游标进度 |

---

## 安全与隐私边界

> [!IMPORTANT]
> 本工具默认仅在本地运行，不发起任何外部网络请求，也不会保存或上传任何个人聊天数据。

- **密钥保护**：提取的 SQLCipher 密钥仅通过 Windows DPAPI（`CryptProtectData`）按当前 Windows 用户加密保存在 `%LOCALAPPDATA%\wx-observer\keys\`；若缓存损坏或跨用户复制，直接失败拒绝，绝不回落为明文解析。也支持通过 `--keys-file` 注入外部密钥文件。
- **临时文件自动销毁**：解密后的 SQLite 快照仅存放在 `%TEMP%\wx_observer_tmp\sess-<pid>-<uuid>\` 独立会话目录中，正常退出或收到中断信号时自动清理，启动时亦会回收已退出进程的残留目录。
- **本地监听限制**：MCP Streamable HTTP 与 Web UI 默认均绑定 `127.0.0.1`，并支持通过 `--token` 开启恒定时间比对（`hmac.compare_digest`）访问校验。
