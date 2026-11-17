# AutoSearch

BoringResearch's front mod

单用户论文跟踪工具。每轮由你指定研究问题与代表论文。SQLite 保存本轮采集、解读、预算和报告，Markdown 按研究周期归档。启动下一轮会切换跟踪主题，旧报告可继续查阅。

## 启动第一轮

在本目录打开 PowerShell，使用 Python 3.11 或更新版本。当前工作站已具备所需 Python 和 Beautiful Soup。在新环境安装依赖：

```powershell
python -m pip install -r requirements.txt
```

下面用一个示例演示命令。创建你的周期时替换研究问题与代表论文。支持论文题名、题名加笔记、arXiv ID/链接及 DOI/链接。标识会在创建周期时联网解析。可多次传入 `--seed` 和 `--term`。

```powershell
python autosearch.py start --question "如何减少视觉 Transformer 的推理计算量？" --seed "DynamicViT: Efficient Vision Transformers with Dynamic Token Sparsification" --term "token pruning" --term "token sparsification"
python autosearch.py status
python autosearch.py run
```

北京时间每日 13:00 起生成当天日报。首日截点前执行时，任务等待当天截点。已有周期在停机后重新运行会回补未处理的日报窗口。每次执行先检查已成功处理的来源，失败来源在下次执行重试。首次会议检查会读取最新可识别的一届作为起始资料。报告位于 `data/reports/<周期编号>/`。用同样的 `start` 命令指定下一轮输入；新周期开始后调度器只推进新周期。

## 中文解读与模型

设置支持 Chat Completions 和 JSON 对象输出的模型。PowerShell 示例中的值均需替换，密钥通过环境提供：

```powershell
$env:AUTOSEARCH_BASE_URL = "https://your-provider.example/v1"
$env:AUTOSEARCH_MODEL = "your-model-id"
$env:AUTOSEARCH_API_KEY = Read-Host "Model API key" -MaskInput
```

`Read-Host -MaskInput` 适用于较新的 PowerShell。Windows PowerShell 5.1 可通过 Windows 用户环境变量设置密钥。配置模型后，`start` 可省略英文检索词，由模型结合研究问题和代表论文生成。未配置模型时显式提供检索词，系统先归档命中的题录和可用原文摘要，后续启用模型会补充当前周期报告。精选论文优先读取 arXiv HTML 的方法、实验、讨论与局限等章节；无法取得正文时以摘要为依据。会议从官方页面读取精选论文摘要，期刊使用已登记的摘要。题录缺少证据时会标注材料不足。

报告中可找到论文标识。以下操作会在下次运行时更新本轮排序。

```powershell
python autosearch.py feedback "arxiv:实际编号" "值得追踪"
```

## 后台运行

- 前台常驻运行 `python autosearch.py watch --minutes 30`，关闭窗口会停止该进程。
- 使用 Windows 任务计划程序时，在已激活目标 Python 环境的 PowerShell 中执行 `.\Install-Schedule.ps1`。脚本记住当前解释器与项目位置，每 30 分钟以当前用户身份执行一次，错过触发后可补跑。此模式适用于用户已登录、主机开机联网的场景。任务计划程序使用用户环境，需为该账户持久设置模型和投递参数。可在任务计划程序中查看或停用 AutoSearch。
- Linux 可由 cron/systemd 定时执行 Python 和 `autosearch.py run` 的绝对路径。所有入口默认使用脚本所在目录，可通过子命令前的 `--data` 与 `--settings` 指定其他目录或配置文件。同一数据目录有进程互斥锁。

## 来源与报告周期

| 来源 | 默认配置与调度 |
|---|---|
| arXiv | 英文短语并集，按更新时间排序，重叠窗口回看。首发及修订按标识和版本去重。日报记录首次发现日，列出主题走势、精选解读和完整命中索引。 |
| PMLR | ICML 最新官方主会卷，按卷目录题名筛选，入选后取摘要。可在 `pmlr` 中增加目录格式相同的会议，如 AISTATS。 |
| CVF | CVPR、ICCV 最新官方主会目录。分批增加的命中论文更新对应届次专报。 |
| Crossref 期刊 | 默认 TPAMI、IJCV，按 ISSN 查询索引更新，卷期归档。匹配的在线优先论文暂存，登记期号后纳入专报。连续出版期刊可在设置中添加 `{"name":"刊名","issn":"ISSN","continuous":true}`，按出版月份汇总。期刊覆盖取决于出版社向 Crossref 登记的元数据；初始回看 7 天，过滤较早年度的索引修订。 |

会议初筛依据官方目录题名。匹配报告显示当前周期已发现的主题命中。可设置 `AUTOSEARCH_CONTACT` 为联系人邮箱，用于学术来源请求。来源接口见[设计草案](DESIGN.md)。

## 跨领域讨论与预算

每个自然周从本轮最近七天的精选论文中选取一项有原文依据的局限。模型给出机制层面的英文检索式，程序检索本地资料及不限学科和年份的 Crossref 文献，补充近似已有工作和至多一跳引用。每批最多两次文献搜索、一次引用查询，冻结至多三篇有摘要的材料，再开始讨论。没有符合条件的局限时跳过该周检查，之后有新证据可再次触发。

方法迁移和实验设计两个角色并行，各执行一次。批评角色执行一次，编辑执行一次，给出 0–2 项待验证设想及可追溯的来源。每周批次会持久记录模型调用，即使任务重启也受四次上限约束。预算或时限不足时保存停止原因。讨论输出属于研究假设，仍需实验验证。

`settings.json` 可调整日报截点、回看天数、精选篇数、各来源、讨论时限和日/月/批次 token 上限。默认每批讨论上限 12,000 token，每日 90,000、每月 1,800,000，其中每日给报告预留 18,000。每次请求先按输入 UTF-8 字节数、协议余量和最大输出保守预留，返回后按供应商用量结算。网络中断或供应商未报告用量时保留预留值。该估计适用于常见字节级分词模型，实际费用由供应商计价。达到额度后先保留题录和已完成解读，下次检查可补充。

## 投递与故障检查

如需同时控制费用，可在 `settings.json` 的 `cost` 中填写模型输入、输出两者中较高的每百万 token 单价，并设置同币种的日/月金额上限。默认金额上限为零，表示仅启用 token 限额。估算采用调用时配置的单价。

默认本地归档。要同时发邮件，在运行工作进程的账户中设置 `AUTOSEARCH_MAIL_TO`、`AUTOSEARCH_MAIL_FROM`、`AUTOSEARCH_SMTP_HOST`。SMTP 使用隐式 TLS，默认 465，可用 `AUTOSEARCH_SMTP_PORT` 修改；认证使用 `AUTOSEARCH_SMTP_USER`、`AUTOSEARCH_SMTP_PASSWORD`。仅投递配置收件人后生成或更新的报告。重复运行按内容去重，连接失败下次重试，发送结果不确定时保留记录。

`python autosearch.py status` 可查看活动周期、来源错误、当天 token 用量及送达待确认记录。人工核对邮箱后，执行 `python autosearch.py mail-resolve 邮件记录ID sent` 确认已送达，或以 `retry` 允许重试。来源失败时查看命令错误与状态记录；单次执行遇到未完成的任务返回非零。测试命令为 `python -m unittest -v`，使用临时数据库与模拟模型，不产生 API 费用。

