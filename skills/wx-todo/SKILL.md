---
name: wx-todo
description: 从近期微信群聊和私聊中提取待办事项、@我的请求、布置的任务或作业、截止时间（DDL）以及我答应他人尚未收尾的承诺，并关联对应附件。用于“整理最近有什么需要我处理的事”“提取这周群里的作业和截止时间”；不用于日常闲聊摘要（用 wx-brief）。
---

# wx-todo：待办事项与 DDL 提取

从服务端聚合信号中筛选行动项、提及与时间约束，并核对后续对话确认是“仍待办”还是“已完成/已取消”。

## 执行流程

1. **定向拉取行动信号**
   - 先确认账号（`list_accounts()`；多账号先确定 `account`）。
   - 调用 `digest(scope="all", chats=..., since=..., include=["mentions", "attachments", "highlights"], include_self=True, max_chats=20, token_budget=4000)`：
     - 日常待办常用 `since="48h"` 或 `"72h"`；周度任务/作业盘点常用 `since="7d"`。
     - 保留 `include_self=True`，以便同时看到用户自己说过“我一会儿发/我跟进”的承诺与群内后续回复。
2. **核对上下文闭环状态（关键步骤）**
   - 不要把已经解决或撤回的要求当成未完成待办：对 `mentions` 和高疑行动项所在会话，调用 `get_messages(chat=chat_id, after_seq=..., limit=20, detail="compact")` 查看提出时间之后用户是否已回复处理、提交或对方是否已改期/取消。
3. **关联核查任务附件与正文要求**
   - 若行动项涉及 `attachments`（如任务说明 PDF、表格、作业要求文档）：
     - `available == true` 时读取 `local_path`，补全具体要求、提交格式与隐藏 DDL；
     - `available == false` 时标注“附件未下载到本地，需先在微信中接收打开”。
4. **按可行动结构输出**
   - 将结果清晰分为两类：
     - **待处理 / 待跟进**：按截止紧迫度排序，每条写明【事项内容｜来源会话与提出人｜时间/DDL｜状态（未回复/等待中/缺附件）｜关联附件或链接】。
     - **已闭环 / 已失效**（如有发现）：简述证据（如用户后续已发送结果、对方已确认取消）。

## 纯终端 CLI 回退（无 MCP 时使用）

```powershell
wx-observer digest --scope all --since 48h --include mentions,attachments,highlights --json
wx-observer history "<chat_id>" --limit 20 --compact --skip-noise --json
```

## 只读边界

- 本 Skill 只做分析与清单整理，**绝不代发任何微信确认或回复消息**；是否回复、如何处理完全由用户自行决定。
