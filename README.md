# cc-watermark-lab

CC_Codex 二期（26.11）**数据工程**产品线：**血缘水印台**（BlackSheep + Alpine CDN + SQLite）。

## 一句话骨架（自有，非外仓抄题）

**产出表水印模板 → 批次盖印 → 下游追索 → 缺口对账**

与现有七线正交（不是采集目录、ETL 管道、同步源、解析入库、编排流、种子摄取、留存裁撤）。
不碰外仓常见指纹（Feed 归并 / CDC 游标 / 质检签发 / Slicegate 类仓名）。

## 领域

- **所属领域**：数据工程
- **子方向**：数据血缘与水印（产出表盖印 / 追索闭环）

## 技术栈

| 层 | 选型 |
|----|------|
| 后端 | Python 3.12、BlackSheep、Uvicorn、SQLite |
| 前端 | 原生 JS SPA（全中文，底栏），雾灰底 + 青柠强调；不依赖外网 CDN |
| 测试 | pytest + httpx（本地秒级闭环，优先于 compose） |

## 基线已通主链

1. 产出表水印模板（表名 / 列名 / 现行版本）
2. 向运行批次盖印水印值
3. 按水印值追索下游批次
4. 缺口对账（已盖印 vs 应盖印）

## 残段提交边界（26.11 补链）

盖印逐行落库。中途中断时不再返回「全成功」，而是把**已提交行 / 待盖行 /
所选边界**登记成一处「残段」，四处同源露出：

| 处 | 页面 | 接口 |
|----|------|------|
| 残段目录 | 底栏「残段」 | `GET /api/residues` · `POST /api/residues/{id}/resume` |
| 批次列表 | 底栏「批次」芯片 | `GET /api/batches`（`chip` / `stamp_state`） |
| 盖印附页 | 批次行「附页」 | `GET /api/batches/{id}/sheet` |
| 对账席 | 底栏「缺口」 | `POST /api/gap-reports`（`residue_count` / `retained_rows`） |

**选边决定残段是否占库存**（点续写时后端按边界一次落库）：

- **回滚** → 已提交水印行整笔恢复待盖，残段 `已收口·回滚`，批次退回 `未盖`，
  缺口随之回升。残段**不占库存**。
- **留存** → 已提交行保持 `已盖` 并占库存，另开补盖任务写剩余码，残段
  `已收口·留存`，批次转 `待补盖`；补盖写满后收成 `已盖印`。

同源校验：批次态只有 `未盖 / 残段 / 待补盖 / 已盖` 四档，必须与残段行数互为镜像；
任何写入口（盖印 / 续写 / 补盖 / 对账）发现漂移即拦截，不落库。
并发续写由 `BEGIN IMMEDIATE` 写锁排他，双侧同时点只落一笔。

复现步骤纸：`docs/residue-boundary-repro.md`。

预留切口（未做，供后续功能题）：模板换版生效窗、盖印作废回灌、跨表依赖冻结、
追索深度限制等。

## 本地（推荐反馈环）

```powershell
cd claude-workspace\cc-watermark-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

http://127.0.0.1:7000/login · **admin / 123456**

## Docker

```powershell
docker compose up -d --build
```

端口 **7000**。冒烟：`python ..\scripts\_smoke_lab_access.py --dir .`
