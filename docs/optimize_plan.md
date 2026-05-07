- 高风险 默认开放所有用户访问  
  clawchat_gateway/install.py:356-378 会默认写入 CLAWCHAT_ALLOW_ALL_USERS=true，__init__.py:221-222 又把它注册成平台鉴权入口，docs/architecture.md:66 也明确写了这是默认值。这个不是“代码味道”，而是真正的 open-by-default 访问控制风险：新安装后如果运维没额外收紧，任何 ClawChat 用户都可能直接和 Hermes 对话。
- 高风险 明文 http/ws 默认值会暴露 Bearer Token  
  clawchat_gateway/api_client.py:14-15 默认是 http://... / ws://...，activate.py:78-84 会从 http 推导出 ws，而 connection.py:196-200 不但在 Authorization 里带 token，还把 token 放进了 WebSocket subprotocol：bearer.<token>。这意味着只要链路不是完全可信，凭证可能被中间人、代理或日志系统看到，风险比普通 header-only 认证还高。
- 中高风险 媒体 URL 可触发任意远程抓取，带 SSRF / DoS 面  
  clawchat_gateway/media_runtime.py 里 _load_remote_media()、download_inbound_media()、upload_outbound_media() 会对任意 http(s) URL 执行 urlopen，而且整块读入内存，没有大小上限；media_runtime.py:135、158、204 的三处 urlopen 调用**完全没有传入 timeout 参数**，会退化成系统默认（通常意味着可被无限挂起）。若攻击者能影响 media URL，就可能让宿主机去访问内网地址、慢速地址或超大文件，形成 SSRF / 资源耗尽 / 外连滥用 风险。
- 中风险 大量静默吞异常，问题会被隐藏  
  media_runtime.py:258-261, 294-295 在媒体上传/下载失败时直接 continue。这本身不是漏洞来源，但会让 SSRF、鉴权失败、超时、格式异常等行为不易被发现和审计，同时增加排障难度。
- 中风险 日志泄露面偏大  
  adapter.py 和 connection.py 会记录 sender id、bot user id、chat id、message id、trace id，以及前 80 字符文本摘要。这更偏 隐私/日志敏感信息暴露，不是直接入侵入口，但在生产环境里风险不低。
- 低到中风险 restart 用 sh -lc 很脆弱，但暂时不像显著注入点  
  restart.py 的确用了 shell，但 delay_seconds 被强制转成 int，路径值也做了 repr 引号包裹，所以从当前证据看，不像明显命令注入漏洞。不过它仍然是脆弱实现：可观测性差、失败静默、行为依赖 shell 语义。
可以优先优化的地方：
- 把 WebSocket 读循环和消息处理解耦  
  connection.py 当前读到帧后会直接推进后续处理；若某条消息触发较慢的媒体下载或 Hermes 处理，可能拖慢心跳、收包和断线检测。建议改成“读循环 + 有界队列 + worker”模型。
- 统一出站写路径，避免直接发送和队列刷新并存  
  connection.py 现在既有直接 ws.send()，又有排队再 flush，容易产生顺序和并发写问题。建议统一成单 writer coroutine 或显式锁。
- 媒体 I/O 改成更受控的异步/限流模型  
  media_runtime.py 和 api_client.py 现在大量靠 asyncio.to_thread + urlopen + 全量缓冲，在多媒体场景下容易造成线程池压力和内存尖峰。这里是最有收益的性能改造点之一。
- 减少热路径里的重复解析和过量日志  
  adapter.py / connection.py 在热路径里有重复 decode、重复遍历 payload、为日志再解析一次队列帧的情况。流量高时这些都是纯额外开销。
- 清理 active run 的状态结构  
  adapter.py 里 _resolve_active_run（adapter.py:686-704）当前已经是基于 _active_runs_by_id / _active_chat_runs 两个 dict 的 O(1) 查找，并不存在“线性扫描”问题；真正值得关注的是这两个 dict 的清理路径以及它们之间的一致性维护——失败/中断场景下容易残留 stale 条目，并发会话多时会放大状态管理成本。
补一个验证结论：本次结论主要基于源码阅读、AST 搜索和并行子代理审查。Python LSP（basedpyright，当前为 1.38.4）目前已安装于 `/Users/joe/.local/bin/basedpyright`，后续可在此基础上补一轮静态类型诊断。
