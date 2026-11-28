---
name: wx-find
description: 在微信历史记录中统一查找特定消息、某人说过的话、分享过的链接或 PDF/Word/压缩包等本地文件，并还原前后语境与出处。用于“找一下之前谁发过的报价单/作业”“翻出上个月关于某话题的原话和附件”；不用于全局聊天汇总（用 wx-brief）。
---

# wx-find：跨会话找消息、找链接与找文件

将关键词检索、附件定位与上下游语境溯源合为一条线索闭环，先用低开销索引定位，再按需补全上下文与读取本地文件。

## 执行流程

1. **按线索类型选择首跳工具（不确定时两条并行）**
   - **偏文件/文档/作业/压缩包线索**：先调 `list_attachments(chat_type=..., chats=..., exclude_chats=..., since=..., extensions=..., only_available=False, limit=30)`（含转发记录 `chat_history` 内嵌文件）。若发现关键资料在某条转发聊天记录（`type="chat_history"`）内，调 `expand_chat_history(chat=chat_id, seq=...)` 展开子消息与全部内嵌文件的本地路径。
   - **偏文字/链接/某人发言线索**：先调 `search_messages(keyword=..., chat=..., chats=..., exclude_chats=..., chat_type=..., since=..., min_activity=..., limit=20)`，利用返回的 `snippet`、`match_offset`、`chat_id` 与 `seq` 精准锚定位置（默认 `detail="compact"` 省 token）。
   - 若仅记得大致对象不记得关键词，先 `list_chats(name_query=...)` 锁定会话，再用 `get_messages(chat=..., sender=..., msg_types=..., since=..., limit=30)` 筛选。
2. **还原锚点前后的完整语境**
   - 拿到命中记录的 `chat_id` 与 `seq` 后，用 `get_messages(chat=chat_id, before_seq=seq + 5, limit=12, detail="compact")` 或联合 `after_seq` 拉取命中前后的真实对话，避免断章取义。
   - 遇到被截断的长文本需要全文核对时，仅针对该小窗口传 `detail="full"`。
3. **本地明文附件的核验与读取边界**
   - 若附件记录 `available == true` 且返回了 `local_path`，可直接用文件读取工具分析本地 PDF/Word/文本内容并总结重点。
   - 若 `available == false`（微信尚未把文件下载到本机，或已清理/移动），**明确提示用户在电脑端微信中双击下载该文件后重查**，绝不能仅凭文件名臆测文件内容。
4. **带出处交付结果**
   - 每条结论必须附带明确出处：会话名称（私聊/群聊）、时间（`t`）、发送者，以及（若有）已验证存在的本地路径或外链 URL。

## 纯终端 CLI 回退（无 MCP 时使用）

```powershell
wx-observer search "<关键词>" --since 30d --compact --json
wx-observer attachments --since 30d --ext pdf,docx --limit 30 --json
wx-observer history "<chat_id>" --before-seq <seq> --limit 12 --compact --json
```

## 只读与安全边界

- 仅读取工具返回的位于个人微信 `msg/file` 目录下的真实 `local_path`，不随意猜测路径或写入微信目录。
