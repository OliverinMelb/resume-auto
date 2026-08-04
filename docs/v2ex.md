# V2EX 酷工作本机归档

这个数据源读取 V2EX 官方公开的 `jobs` JSON Feed。Feed 一次只提供最近约
50 条且没有翻页接口，因此本项目每天保存一次看到的安全结果，并按主题链接去重，
让本地查看器逐日积累历史。

它不需要 V2EX 账号、Token 或 Cookie，也不会读取主题详情或回复。旧帖子离开当前
Feed 后仍会保留，但这不代表岗位仍开放。若两次成功抓取之间新增超过 50 条，仍可能
漏掉从未出现在快照中的帖子。

## 手动归档一次

在仓库根目录使用项目的明确 Python 解释器运行：

```powershell
& 'C:\path\to\.venv\Scripts\python.exe' -m job_sources.v2ex.archive
```

默认数据库是 `.runtime/v2ex/jobs.db`，该目录已被 Git 忽略。可临时指定其他位置：

```powershell
$env:V2EX_ARCHIVE_DB_PATH = 'D:\local-data\v2ex-jobs.db'
& 'C:\path\to\.venv\Scripts\python.exe' -m job_sources.v2ex.archive
```

命令只输出状态和数量。`complete`、`partial` 返回退出码 0；网络、Feed 或本地存储
失败返回非 0。

计划任务使用的 runner 也可以离线检查路径，不发起 Feed 请求：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File `
  .\scripts\v2ex\run_archive.ps1 `
  -PythonPath 'C:\path\to\.venv\Scripts\python.exe' `
  -DryRun
```

## 查看累计历史

```powershell
& 'C:\path\to\.venv\Scripts\python.exe' -m job_sources.v2ex.viewer
```

然后打开 `http://127.0.0.1:8765/`。查看器只监听本机地址，默认读取同一个归档库；
冷启动会抓取并归档一次，页面内手动刷新至少间隔 60 秒，普通访问使用 5 分钟内存
缓存。Feed 暂时失败时，有历史则继续显示历史并给出陈旧数据提示；没有历史才返回
503。

## 安装每天 09:00 的 Windows 任务

先用 `-DryRun` 检查定义，不会创建任务：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File `
  .\scripts\v2ex\install_daily_task.ps1 `
  -PythonPath 'C:\path\to\.venv\Scripts\python.exe' `
  -At '09:00' `
  -DryRun
```

确认后移除 `-DryRun` 即可创建或更新当前用户任务
`CzcGoodJob-V2EX-Daily-Archive`。任务错过 09:00 后会在系统和用户会话可用时补跑；
若上一次仍在运行，不启动重叠实例。任务保存的是解释器和仓库绝对路径，不保存密码、
Token 或 Cookie。

检查任务：

```powershell
Get-ScheduledTask -TaskName 'CzcGoodJob-V2EX-Daily-Archive'
```

仅删除这个任务：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File `
  .\scripts\v2ex\uninstall_daily_task.ps1
```

## API 2.0

API 2.0 暂未接入。以后取得 Token 后，可以新增 API 归档适配器；当前 Feed 归档、
SQLite 数据和查看器不依赖它。
