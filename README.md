# astrbot_plugin_dafeiyu_emotion

给 AstrBot 角色加一层**持久情感状态**：从已有长期记忆里整理关系档案，在主人私聊中注入当前情感，并按小步反思更新好感、信任、安全感、心情和占有欲。

这不是模型微调。它不改变模型权重，也不改人格主体，只通过状态、记忆和提示词让表达慢慢变化。

> 当前是 0.1.0 原型版：先跑一周看效果，再决定是否加每日深度巩固。

## 功能

- 从 `livingmemory` 已有记忆生成关系档案和初始状态；
- 只对配置的 `owner_qq` 私聊生效，群聊和定时任务不训练；
- 每 N 轮主人私聊回复，后台分析一次互动，做**有上限的小步调整**；
- 心情每天向基线回落，好感和信任变化更慢；
- `/情感` 查看当前状态，`/情感原因` 查看最近触发，`/情感重置` 回到初始基线；
- 情感事件最多保留 30 条，状态只保存在本机插件数据目录，不进 Git。

## 运行原理

1. **关系档案 seed**：首次私聊时读取 `livingmemory` 的文档/记忆原子，让模型整理出关系档案和初始好感、信任、安全感、心情、精力。
2. **聊天注入**：每次主人私聊，把当前阶段、好感、信任、安全感、心情、精力，以及最近 2 条触发，作为内部参考注入请求。
3. **小步反思**：每 `reflect_every` 轮回复，后台用当前聊天模型分析这段互动，返回有依据的 JSON 变化量。
4. **边界**：变化量有硬上限，心情会衰减回基线；群聊、cron 任务、其他人都不参与。
5. **展示**：`/情感` 用自然语言和数值给主人看；聊天里不会主动报数值。

## 环境要求

- AstrBot `>= 4.29.0`；
- Python 3.10+（AstrBot 自带环境即可）；
- 可选：已安装并启用的 [`livingmemory`](https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory)，用于 seed 关系档案；
- 没有 `livingmemory` 也能运行，会使用默认档案，只从之后的聊天里学习。

不需要额外 pip 依赖。

## 安装

### 方式一：手动放进插件目录

```bash
cd AstrBot/data/plugins
git clone https://github.com/YukineYuuna/astrbot_plugin_dafeiyu_emotion
```

重启 AstrBot，或在插件管理里重载 `astrbot_plugin_dafeiyu_emotion`。

### 方式二：从 GitHub 安装

在 AstrBot 插件管理里选择“从仓库安装”，填：

```text
https://github.com/YukineYuuna/astrbot_plugin_dafeiyu_emotion
```

仓库根目录已经是插件结构，包含 `main.py`、`metadata.yaml`、`_conf_schema.json`。

## 配置

安装后在 AstrBot 插件配置里填写：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `owner_qq` | 是 | 唯一主人 QQ。只有这个号私聊会注入/训练；留空插件不生效 |
| `enabled` | 否 | 是否启用，默认 `true` |
| `reflect_every` | 否 | 每多少轮回复反思一次，默认 `6`，建议 `6-10` |
| `livingmemory_db` | 否 | 自定义 livingmemory 数据库路径；留空用 AstrBot 数据目录下的默认路径 |

配置示例：

```json
{
  "owner_qq": "123456789",
  "enabled": true,
  "reflect_every": 6,
  "livingmemory_db": ""
}
```

## 命令

以下命令只在**主人私聊**里响应：

| 命令 | 作用 |
| --- | --- |
| `/情感` | 查看当前阶段、好感、信任、安全感、心情、精力，以及最近一次触发 |
| `/情感原因` | 查看最近 5 条情感事件 |
| `/情感重置` | 回到 seed 时的基线，保留关系档案，清空事件 |

## 数据与隐私

状态文件：

```text
data/plugin_data/astrbot_plugin_dafeiyu_emotion/state.json
```

里面可能包含关系档案和主人私聊事件。`.gitignore` 已排除：

```text
state.json
config.json
config.local.json
*.log
.env
```

**不要**把 `state.json`、聊天记录、key 或 `owner_qq` 提交到公开仓库。

## 变化上限

| 维度 | 单次变化上限 | 范围 |
| --- | --- | --- |
| 好感 / 信任 / 安全感 / 占有欲 | ±2 | 0–100 |
| 心情 | ±8 | -50–50 |
| 精力 | ±5 | 0–100 |

心情每次反思后会向基线回落 15%，每天再向基线回落一次。

## 已知限制

- 第一版没有独立的每日深度总结；目前是每 N 轮反思 + 每日心情回落；
- 初次 seed 依赖当前聊天 provider；provider 未就绪时会用默认档案，等首次私聊再补；
- 如果角色本身有很强的人格提示词，情感状态只应作为语气细节，不应压过人格；
- 运行时会产生额外后台模型调用，频率由 `reflect_every` 控制；
- 只支持主人私聊，不支持群聊共用同一份状态。

## 排错

**`/情感` 没反应**

1. 确认是在主人私聊里发；
2. 确认 `owner_qq` 填的是你的 QQ，且不是群聊；
3. 确认插件已启用、已重载。

**一直没有关系档案**

- 没装 `livingmemory` 或数据库路径不对时，会使用默认档案；
- 首次 seed 后台模型调用失败时，下一次主人私聊会再尝试。

**变化太快 / 太慢**

- 调大 `reflect_every` 会减少反思频率；
- 想重置回 seed 基线，在主人私聊里发 `/情感重置`；
- 想完全停掉，把 `enabled` 设为 `false`。

**想回到没有情感插件的状态**

删除插件目录和 `state.json`，或在插件管理里禁用/卸载插件。角色人格不会因此被修改。

## Roadmap

- 每日深度巩固：汇总当天事件，更新长期基线；
- 多角色/多用户独立状态；
- 可选 WebUI 查看曲线和触发原因；
- 把 seed 做成可手动触发的命令。

## License

MIT
