# 黯灵师对局采集与走势图

这是一个面向已授权数据的本地/局域网监控台：`mitmproxy` 记录脱敏流量，FastAPI + SQLite 保存标准化胜负，浏览器实时展示走势和统计基线。

## 启动 Web 服务

```powershell
$env:WRITE_TOKEN = "change-me-local"
python -m uvicorn api.app:app --host 0.0.0.0 --port 8000
```

打开 `http://127.0.0.1:8000/`。局域网其他设备访问主机 IP 的 8000 端口。

## 先用模拟结果验证链路

```powershell
$headers = @{ "X-Write-Token" = "change-me-local" }
$body = @{ round_id = "demo-001"; winner = "red"; confidence = 1; source = "manual" } | ConvertTo-Json
Invoke-RestMethod http://127.0.0.1:8000/api/v1/results -Method Post -Headers $headers -Body $body -ContentType "application/json"
```

## 先获取真实网络请求（优先步骤）

### 安卓真机

1. 在 Windows 运行 `powershell -ExecutionPolicy Bypass -File collector/start_capture.ps1`，首次勘探先不要传 `-TargetHosts`。
2. 手机与电脑连接同一 Wi-Fi，把手机 Wi-Fi 代理设置为“手动”，主机填 Windows 局域网 IP，端口填 `8080`。
3. 在手机浏览器打开 `http://mitm.it`，只在授权测试设备上安装对应 CA 证书。
4. 重新打开小程序，进入一局并等待结算，再在另一个终端运行 `python collector/inspect_flows.py`。
5. 根据摘要中的域名和接口缩小范围，再重启并传入 `-TargetHosts "api.example.com,game.example.com"`。

### 微信开发者工具

如果你有小程序源码，优先使用开发者工具的 Network/调试面板查看请求和响应；把结算接口的脱敏 JSON 保存到 `data/fixtures/`，然后再调整解析字段。

### 证书绑定

如果代理无法看到 HTTPS 内容、页面提示网络异常或只有 CONNECT 记录，说明可能存在证书绑定。不要绕过校验；改用开发者工具、服务端日志或让运营方提供脱敏接口样例。

设置目标域名并启动 addon 的等价命令：

```powershell
$env:TARGET_HOSTS = "authorized.example.com"
$env:POST_URL = "http://127.0.0.1:8000/api/v1/results"
$env:WRITE_TOKEN = "change-me-local"
mitmdump --listen-host 0.0.0.0 --listen-port 8080 -s collector/mitm_addon.py
```

插件会将脱敏 HTTP、HTTPS 和 WebSocket 流量写入 `data/flows.jsonl`，并尝试从响应 JSON 的 `winner`、`result`、`side`、`round_id` 等字段识别结果。字段不匹配时先保留原始脱敏流量，再按实际接口样例调整 `extract_result`。

### 游戏二进制帧解析

该游戏的结算消息是二进制 WebSocket 帧。解析候选记录（保留原始 `result_code`，不猜红蓝映射）：

```powershell
python collector/parse_game.py data/game-flows.jsonl data/match-candidates.jsonl
```

候选记录包含开始时间、结束时间、`result_code`（当前观察到 1/2）和轮次提示。确认 UI 中 1/2 对应哪一方后，再通过 `RESULT_CODE_1` / `RESULT_CODE_2` 配置写入标准化结果。

## 测试

```powershell
pytest -q
```

## Admin model configuration

The model administration page is available at `/admij`. Configure these
environment variables before starting FastAPI; the password and encryption
key are intentionally not stored in the repository:

```powershell
$env:ADMIN_USERNAME = "admin"
$env:ADMIN_PASSWORD = "replace-with-a-long-random-password"
$env:ADMIN_SESSION_SECRET = "replace-with-a-long-random-session-secret"
$env:ADMIN_ENCRYPTION_KEY = (python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
$env:ADMIN_COOKIE_SECURE = "1" # use 1 when serving behind HTTPS
python -m uvicorn api.app:app --host 0.0.0.0 --port 8000
```

After login, add any OpenAI-compatible Chat Completions endpoint (DeepSeek,
Qwen, GPT, or a self-hosted service). API keys are encrypted in SQLite and
the UI only shows whether a key is configured plus its final four characters.
The public page reads `/api/v1/predictions` and updates through SSE; it never
returns model URLs or keys.

## Desktop authorization refresh agent

The server can collect settlements without an always-open mini-program while a
valid game credential exists. Run the server-side listener alongside the API:

```powershell
$env:ADMIN_ENCRYPTION_KEY = "the same key used by FastAPI"
$env:WRITE_TOKEN = "change-me-local"
python -m collector.server_ws_client
```

In `/admij`, create an **authorized refresh device** and copy its one-time
device ID and pairing token into the Windows desktop application:

```powershell
python collector/desktop_agent.py
```

The desktop agent uses the current Windows user's DPAPI to protect its pairing
token. It starts a local proxy only when requested. The operator must manually
open the authorized mini-program to refresh a login; the app captures the new
JWT and uploads it immediately without writing the JWT to disk. The server
then reconnects independently until the credential expires. Do not automate or
attempt to bypass the platform login flow.

预测模块是贝叶斯平滑频率基线，只用于描述最近样本，不代表确定性预测。
