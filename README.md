# POS Template Mapping Agent

> 面向餐饮/奶茶行业的 POS 模板自动映射工具。上传主数据表和 POS 模板，一条 CLI 指令完成 SOP 字段自动填充。

---

## 项目背景

餐饮、奶茶行业存在大量不同的 POS 系统和第三方平台导入模板，字段名称、结构、语言各异，目前需要人工完成字段理解、商品查找、SOP 映射和结果校验，流程重复耗时且容易出错。

本项目目标：

```
上传主数据表 + 上传 POS 模板 + 输入一句自然语言指令
↓
Agent 自动完成字段理解 → 商品匹配 → SOP 填充 → 生成可导入模板
```

---

## 核心业务目标

将主数据表中的 SOP 字段自动映射到模板中的配料字段。

```bash
map sop to 配料字段
```

---

## 数据结构

### 主数据表字段

| 字段 | 说明 |
|------|------|
| 品名 | 商品名称 |
| 杯型 | 规格（大杯/中杯/小杯） |
| 奶底 | 牛奶、燕麦奶等（可为空，空=通配符） |
| 做法 | 温度（正常冰/少冰/去冰/热等） |
| 糖 | 糖度（全糖/七分糖/五分糖等） |
| 全信息 | 聚合字段，供参考 |
| SOP 代码 | **最终需要映射的目标数据** |

主数据表示例：

| 品名 | 杯型 | 奶底 | 做法 | 糖 | SOP |
|------|------|------|------|----|-----|
| 浅浅清茶 | 中杯 | 牛奶 | 少冰 | 七分糖 | T240、B30/80、S4、IC(S)、MS 3-5 |
| 浅浅清茶 | 中杯 | 牛奶 | 去冰 | 标准糖 | T265、B30/105、S5、IC(S)、MS 3-5 |

### 模板表字段（示例）

| 字段 | 说明 |
|------|------|
| 菜品名称 | 商品名称（字段名因模板而异） |
| 规格 | 杯型 |
| 口味做法组合 | 组合字段，逗号分隔，含茶底/奶底/糖度/温度中的若干项 |
| 配料 | **需要填充 SOP 的目标列（当前为空）** |

待匹配数据示例：

| 菜品名称 | 规格 | 口味做法组合 | 配料 |
|----------|------|--------------|------|
| 五黄高纤慢养瓶 | 五角瓶 | 红茶, 十二分糖, 温热 | ← 待填充 |
| 五黄高纤慢养瓶 | 五角瓶 | 红茶, 十二分糖, 正常冰 | ← 待填充 |

---

## 核心难点与决策

### 难点1：字段名称不统一

同一语义字段在不同模板中名称各异：

```
商品名称 / 菜品名称 / Product Name / MENU_NM
```

**决策**：LLM 在初始化阶段一次性分析模板 Schema，生成字段映射配置，后续匹配不再调用 LLM。

---

### 难点2：组合字段解析（最复杂）

`口味做法组合` 字段为逗号分隔的自由组合，理论上包含四个维度（茶底、奶底、糖度、温度），但饮品可能缺少其中一项或多项：

```
红茶, 十二分糖, 温热          # 缺奶底
燕麦奶, 正常冰, 七分糖        # 缺茶底
红茶, 燕麦奶, 五分糖, 少冰    # 完整四项
```

**决策**：Token 分类由 LLM 完成（规则词典兜不住所有缺项情况），LLM 输出结构化 JSON，再交给规则引擎验证。

---

### 难点3：商品名称高度相似，禁用 Embedding

```
黑糖波波
黑糖波波牛乳
黑糖波波牛乳茶   ← 三者是完全不同的商品
```

Embedding 会将上述三者的相似度打高，导致误匹配。

**决策**：商品名称只用 RapidFuzz 字符串精确匹配（高相似度阈值），拒绝 Embedding。

---

### 难点4：奶底为空时的通配逻辑

主数据表中奶底字段为空，表示该商品对奶底不敏感（通配符），可以匹配模板中任意奶底值。

**决策**：Rule Engine 处理此业务逻辑，奶底为空时跳过该维度约束。

---

### 难点5：匹配失败处理

**决策**：填入置信度最高的候选结果，并在输出文件中标注 `LOW_CONFIDENCE`，生成独立的校验报告。

---

## 系统架构

### 三层引擎设计

```
┌─────────────────────────────────────┐
│           LLM 层（DeepSeek API）      │
│  Schema Analyzer + Token Classifier  │
│  · 仅在初始化阶段调用，不参与逐行匹配  │
└──────────────────┬──────────────────┘
                   │ 输出：字段映射配置 + Token 分类结果
┌──────────────────▼──────────────────┐
│              Rule Engine             │
│  · 字段标准化                        │
│  · Token 验证与纠错                  │
│  · 奶底通配逻辑                      │
│  · Canonical Schema 转换             │
└──────────────────┬──────────────────┘
                   │ 输出：标准化行数据
┌──────────────────▼──────────────────┐
│            Matching Engine           │
│  · 商品名：RapidFuzz 高阈值匹配      │
│  · 属性组合：规则精确匹配            │
│  · 兜底：Embedding 候选召回          │
│  · 失败：最佳猜测 + LOW_CONFIDENCE   │
└──────────────────┬──────────────────┘
                   │
             写入 Excel 输出
```

### Canonical Schema（内部标准结构）

所有模板字段最终统一转换为：

```json
{
  "product_name": "",
  "size": "",
  "milk_base": "",
  "temperature": "",
  "sugar": "",
  "tea_base": ""
}
```

---

## 工作流详解

```
CLI 命令输入
↓
读取 Excel（主数据表 + 模板表）
↓
[LLM] Schema Analyzer
  · 分析模板字段语义
  · 识别组合字段、冗余字段
  · 输出字段映射配置（一次性，缓存复用）
↓
[LLM] Token Classifier（逐行）
  · 解析口味做法组合
  · 识别缺失维度
  · 输出结构化 Token JSON
↓
[Rule Engine] 标准化
  · 验证 Token 合法性
  · 应用奶底通配逻辑
  · 转换为 Canonical Schema
↓
[Matching Engine] 匹配
  · Step 1：RapidFuzz 商品名精确匹配
  · Step 2：属性组合规则匹配（温度/糖度/规格/奶底/茶底）
  · Step 3：Embedding 候选召回（仅当前两步置信度不足）
  · 无结果：填最佳猜测，标注 LOW_CONFIDENCE
↓
写入 Excel（保留原格式，填充配料列）
↓
输出校验报告（低置信度行汇总）
```

---

## Token 词典

### 温度

```
热 / 温热 / 正常冰 / 少冰 / 去冰 / 冰沙
```

### 糖度

```
全糖 / 十二分糖 / 标准糖 / 七分糖（推荐）/ 五分糖 / 三分糖 / 不另加糖 / 无糖
```

### 奶底

```
牛奶 / 燕麦奶 / 厚乳 / 椰乳
```

### 规格

```
大杯 / 中杯 / 小杯 / 五角瓶（模板特有，需 LLM 识别并映射）
```

### 茶底

```
红茶 / 绿茶 / 乌龙茶 / 五角排红茶（品牌特有名称）
```

> 词典为软约束，LLM Token Classifier 可识别词典外的值并标注为 `UNKNOWN_TOKEN`。

---

## 匹配策略详解

### 商品名匹配（严格）

- 使用 RapidFuzz `token_sort_ratio`，阈值 ≥ 90
- **不使用 Embedding**，防止高相似商品误匹配
- 匹配失败直接进入低置信度流程，不降阈值重试

### 属性组合匹配

在商品名匹配成功的候选集内，对以下维度逐一精确匹配：

| 维度 | 匹配方式 | 缺失时 |
|------|----------|--------|
| 规格 | 精确 | 必须有，否则失败 |
| 温度 | 精确 | 必须有，否则失败 |
| 糖度 | 精确 | 必须有，否则失败 |
| 奶底 | 精确（主数据为空则通配）| 通配 |
| 茶底 | 精确（主数据为空则通配）| 通配 |

### Embedding 兜底（可选）

- 使用 `sentence-transformers` + FAISS
- 仅用于商品名候选召回，不参与最终定位
- 结果置信度强制标注为低

---

## 置信度输出格式

输出 Excel 中新增两列：

| 配料（SOP） | 匹配置信度 |
|-------------|-----------|
| T240、B30/80、S4 | HIGH |
| T265、B30/105、S3 | HIGH |
| T240、B30/80、S4（猜测） | LOW_CONFIDENCE |

同时输出 `mapping_report.txt`，汇总所有低置信度行及失败原因。

---

## 技术选型

| 模块 | 技术 | 说明 |
|------|------|------|
| Agent 框架 | LangGraph | 支持复杂工作流，节点可复用 |
| LLM | DeepSeek API | Schema 理解 + Token 分类，不参与匹配 |
| 字符串匹配 | RapidFuzz | 商品名精确匹配 |
| 向量检索 | sentence-transformers + FAISS | 兜底候选召回 |
| Excel 处理 | openpyxl | 保留原格式、公式、数据验证 |
| 数据处理 | pandas | 读取、转换、匹配计算 |

---

## 项目结构（建议）

```
pos-mapping-agent/
├── main.py                  # CLI 入口
├── agent/
│   ├── workflow.py          # LangGraph 工作流定义
│   ├── schema_analyzer.py   # LLM Schema 理解
│   ├── token_classifier.py  # LLM Token 分类
│   ├── rule_engine.py       # 标准化 + 业务规则
│   └── matching_engine.py   # 匹配逻辑
├── data/
│   ├── token_dict.py        # Token 词典
│   └── canonical_schema.py  # 标准 Schema 定义
├── excel_io/
│   ├── excel_reader.py      # 读取 Excel
│   └── excel_writer.py      # 写入结果（保留格式）
├── config.py                # API Key、阈值配置
└── README.md
```

---

## CLI 使用方式（MVP）

```bash
# 基本用法
python main.py \
  --master 主数据表.xlsx \
  --template POS模板.xlsx \
  --output 填充结果.xlsx

# 指定目标列
python main.py \
  --master 主数据表.xlsx \
  --template POS模板.xlsx \
  --target-col 配料 \
  --output 填充结果.xlsx

# 查看校验报告
cat mapping_report.txt
```

---

## MVP 范围（第一阶段）✅ 全部完成

- [x] 单模板支持
- [x] Schema 自动理解（LLM 一次性分析）
- [x] 组合字段解析（LLM Token 分类）
- [x] 商品名精确匹配（RapidFuzz）
- [x] 属性组合规则匹配
- [x] 低置信度标注 + 校验报告
- [x] 端到端 CLI 管线
- [ ] 多模板批量处理（第二阶段）
- [ ] 历史映射规则缓存（第二阶段）
- [ ] Agent 自学习（第三阶段）

---

## 关键设计约束（AI 助手必读）

1. **LLM 调用时机**：仅在初始化阶段调用两次（Schema 分析 + Token 分类），不在逐行匹配中调用。
2. **商品名不用 Embedding**：高相似商品（黑糖波波牛乳 vs 黑糖波波牛乳茶）是不同商品，Embedding 会误匹配。
3. **奶底为空 = 通配符**：主数据表奶底为空时，该行可匹配任意奶底值的模板行。
4. **口味做法组合字段缺项是正常情况**：LLM 需输出哪些维度存在、哪些缺失，Rule Engine 按缺失维度调整匹配约束。
5. **匹配失败不报错**：填入最佳猜测，标注 `LOW_CONFIDENCE`，汇总进报告。
6. **保留 Excel 原始格式**：使用 openpyxl 写入，不破坏原有样式、公式和数据验证。

## 开发进度

| 模块 | 文件 | 状态 | 自测结果 | 备注 | Git commit |
|------|------|------|----------|------|------|
| Excel 读写 | excel_io/excel_reader.py, excel_writer.py | ✅ 已完成 | 12/12 + 16/16 passed | 读：主数据校验/多sheet/列名strip；写：保留样式/列宽/置信度列/报告 | `—` |
| Token 词典 | data/token_dict.py | ✅ 已完成 | 47/47 passed | 5 种类型，28 个 Token；normalize_token() 四级优先级清洗；testdata 真数据补全茉莉绿茶 | `d2de102` |
| Canonical Schema | data/canonical_schema.py | ✅ 已完成 | 22/22 passed | 6 字段定义、主数据映射、Token 类型映射、通配维度 | `93ca42a` |
| Rule Engine | agent/rule_engine.py | ✅ 已完成 | 51/51 passed | 主数据/模板标准化 + Token 验证 + 奶底通配；集成 normalize_token 三处调用 | `205ce26` |
| Schema Analyzer | agent/schema_analyzer.py | ✅ 已完成 | 23/23 passed | LLM 字段语义分析、字段映射配置、结果缓存、Mock 模式 | `afa16df` |
| Token Classifier | agent/token_classifier.py | ✅ 已完成 | 40/40 passed | **纯规则词典分类**（逗号切割 → normalize → lookup）+ **未知词三级兜底**（词典→记忆→交互）；无 LLM 调用；进程内去重缓存 | `9189a04` |
| 长期记忆 | data/memory.py | ✅ 已完成 | 23/23 passed | JSON 持久化（~/.pos_agent/memory.json）、token别名/模板规则/匹配修正三类存储、/memory 指令共用 | _待提交_ |
| Matching Engine | agent/matching_engine.py | ✅ 已完成 | 35/35 passed | RapidFuzz 商品名匹配、属性组合规则匹配、奶底通配、LOW_CONFIDENCE 兜底、校验报告 | `d391bee` |
| LangGraph 工作流 | agent/workflow.py | ✅ 已完成 | 31/31 passed | 7 步管线编排、PipelineState 状态传递、逐节点错误处理、LangGraph/纯顺序双模式 | `852a4e2` |
| CLI 入口 | main.py | ✅ 已完成 | 12/12 passed | argparse 参数解析、--master/--template/--output/--target-col/--report、结果摘要、错误处理 | `a712c40` |

## MVP 验证结果（testdata/ 真实数据）

| 指标 | 数值 |
|------|------|
| 总行数 | 96 |
| 完全匹配 | 40 行 |
| **准确率** | **50.0%** |
| 低置信度 | 48 行 |
| 匹配失败 | 0 行 |

**48 条低置信度原因分析：**
- 48 条：模板奶底=`燕麦奶`，但主数据 53 行奶底全部为 `牛奶`（数据覆盖不全）

> 结论：糖度后缀格式问题已通过 `normalize_token()` 清洗解决（+8 行 → HIGH）。剩余低置信度均为数据覆盖不全，非匹配引擎 bug。

### 安全性修复（上一轮）
- LLM 输入隔离：Schema Analyzer 不再接收完整行数据，仅接收列名+去重样例值
- Unicode 标准化：excel_reader 读取阶段统一 NFC 标准化
- 断言保护：匹配前验证商品名称从原始读取到匹配引擎未被改写，不一致直接报错

### Token Classifier 纯规则改造（`9189a04`）
- **改动**：Token Classifier 由 LLM 改为纯规则词典分类（逗号切割 → normalize_token() → lookup()）
- **效果**：API 调用 5 次 → 1 次（仅剩 Schema Analyzer），总耗时 45.5s → 3.4s（**13 倍提速**）
- **准确率不变**：50.0%（48 HIGH / 48 LOW），低置信度仍为数据覆盖不全
- **词典补充**：扫描 testdata/ 真数据，新增茶底词条「茉莉绿茶」

### 长期记忆 + 未知词兜底（本轮）
- **新增** `data/memory.py`：JSON 持久化存储（`~/.pos_agent/memory.json`），不入 git
- **存储结构**：`token_aliases`（未知词→类型映射）、`template_rules`、`match_corrections` 三类
- **三级兜底机制**：标准词典 → 长期记忆 → 交互式确认（同词每进程仅问一次）
- **交互确认**：三个选项（加入词典/标 UNKNOWN 继续/跳过此行）、支持 mock hook 自动化测试
- **Token Classifier 自测更新**：新增记忆命中、会话缓存、跳过行等场景（40/40 passed）