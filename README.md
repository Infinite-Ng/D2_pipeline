# D2 每日简报自动化（D2_agent）

每个工作日 09:00，自动抓取 D2 系统里**昨天**收到的全部条目，整理成一封邮件
（分组统计 + 每条 `I-xxxx-xxxxx` 的说明 + Excel 明细附件），发给指定收件组。

> 注：文中 `HOST` / `REPO` / `ACCOUNT` 均为占位符（内网主机名 / 仓库名 / 账号）。
> 真实值只放在本地 `config.ini`（已 gitignore），模板见 [config.example.ini](config.example.ini)。

---

## 一、系统识别：这是 OpenText Documentum D2

从界面特征可以确认（DOCUMENT WORKSPACE / TASK WORKSPACE、Virtual document、
Renditions、Workflows、Task Attachment、Task notes、Workflow performers、V1.0 版本图标、
生产服务器标识）：`https://HOST/D2/#d2` 是 **Documentum D2 经典客户端**（ExtJS 界面），
后端是 Documentum Content Server + 一个关系数据库（Oracle 或 SQL Server）。

这个结论很重要，因为它意味着**有正规接口可用**，不必去解析 ExtJS 的 DOM。

### 关于「直连数据库」

底层确实有数据库，但直连是三条路里最差的一条：

| | 直连 DB | **DQL over REST（推荐）** | 浏览器抓取 |
|---|---|---|---|
| 可行性 | 需要 DBA 开只读账号 + 防火墙开口，机构里基本批不下来 | 用你现有 D2 账号 Basic Auth 即可 | 能跑，但脆弱 |
| 数据难度 | Documentum 把一个对象拆成 `_s`/`_r` 两套表（repeating attribute 按 `i_position` 排），一个「任务列表」要跨 `dmi_queue_item_s`、`dmi_workitem_s`、`dm_workflow_s`、`dm_sysobject_s/_r` 五六张表 join | 一条 DQL 搞定，Content Server 自己做 join | 只能拿到界面上显示的列 |
| 权限 | 绕过 Documentum 的 ACL 模型，看到的数据可能超出你本人权限范围 —— 合规上说不清 | 完全走你自己的权限 | 走你的权限 |
| 支持度 | OpenText 官方**不支持**直读内部表，升级时结构可能变 | 官方 API | — |
| 速度 | 快 | 快（几秒） | 慢（要起浏览器、跑 JS、翻页） |

所以路线定为 **Documentum REST + DQL**：

```
GET https://HOST/D2-REST/repositories/{仓库名}?dql=<DQL>&page=1&items-per-page=200
Authorization: Basic base64(用户名:密码)
Accept: application/json
```

任务列表就是 Documentum 的 workflow inbox，对应对象类型 `dmi_queue_item`：

```sql
select * from dmi_queue_item
where name = '<你的 user_name>' and delete_flag = false
order by date_sent desc
```

界面上的列基本能对上：`date_sent`→Sent、`sent_by`→Sender、`message`/`task_subject`→Subject、
`priority`→Priority、`due_date`→Due Date；而 Identifier(`I-2099-000001`，示例) 与
Sending/Receiving(ISR→CHN) 大概率是挂在任务对象上的 D2 自定义属性 ——
探测脚本第 5 步会把那个对象的全部属性 dump 出来，一看就知道字段名。

备选协议：CMIS（`/cmis`）、DFS SOAP。若 REST 组件没部署，再回退 `probe_d2.py` 浏览器方案。

---

## 二、整体架构

`HOST` 是内网地址，云端/远程环境**没有内网访问权限**，所以取数必须在本机跑：

| 环节 | 方案 |
|---|---|
| 取数 | 本机 Python + `requests` 调 Documentum REST（DQL） |
| 定时 | Windows 任务计划，周一至周五 09:00（不依赖任何客户端开着） |
| 发信 | 首选本机 Outlook COM（以你身份发，零凭据）；备选 ITU 内部 SMTP；长期可选 Graph |
| 凭据 | Windows 凭据管理器 / 本地 `config.ini`，不进代码、不进对话 |

---

## 三、进度

- [x] 系统识别 + 路线选定
- [x] **M0a 无凭据侦察**（`probe_recon.py`）：确认主路在线 —— `https://HOST/D2-REST`、
      D2 REST Services **20.4**、repository **`REPO`**、`auth_mode=basic`（无 SSO 墙）
- [ ] **M0b 带凭据探测 `probe_api.py`（← 现在跑这个，只读 GET）**
- [ ] 第 2 步：取数模块（DQL 分页 + 昨日筛选 + 字段映射）
- [ ] 第 3 步：报告生成（HTML 正文 + xlsx 附件）
- [ ] 第 4 步：Outlook 发信（首轮只存草稿）+ 任务计划注册
- [ ] 第 5 步：端到端验证（跟界面上的条数对账）

> 工程细节（架构、模块契约、风险对策、里程碑）见 [DEVELOPMENT.md](DEVELOPMENT.md)。

---

## 四、现在请做这一件事

无凭据侦察（M0a）已经跑完、主路已确认，所以**不用再装依赖、不用 venv**——
带凭据探测脚本已改成纯标准库，直接跑：

```powershell
cd D:\codingSpace\D2_agent
python probe_api.py
```

脚本已内置确认好的 base(`https://HOST/D2-REST`) 和 repo(`REPO`)，直接提示你输入
D2 登录名和密码。密码用 `getpass` 当场读入，**不落盘、不写日志、不会发给我**。

跑完 `probe_out\` 里会有 `api_01_auth_check.json` … `api_06_workitems.json`
（含业务数据、无密码，已被 `.gitignore` 挡住，不会上传）。告我一声我就把取数模块写完。

> 备路（浏览器方案 `probe_d2.py`、`setup.bat` 装 Playwright）预计用不到，留作保险。

---

## 五、还需要你确认（第 3 步前给我就行）

1. **收件组**：目标邮箱列表或通讯组地址。
2. **「昨天」的定义**：周一早上是否要覆盖整个周末。
3. **「内容概述」深度**：只用任务对象上的属性（快），还是要读附件正文（PDF/Word，慢）。
4. **正文语言**：中文 / 英文 / 双语。
