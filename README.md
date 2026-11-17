# 视觉工坊 · Visual Media

> 发布候选：`0.5.0-rc.1`。升级说明与验证边界见 [RELEASE_NOTES.md](RELEASE_NOTES.md)。

将表情包收集、检索、发送、图片生成和角色参考管理整合为一个 AstrBot 插件。

**发布候选：`0.5.0-rc.1`。** 不依赖旧表情包小偷或旧 Gemini 生图插件，也不要求安装 Chat Dynamics。安装 Chat Dynamics 后可在其允许回复的回合里统一选择文字、表情或生图。

## 参考图与任务体验

新增可视化参考图选择、统一任务卡及失败恢复。详情见 [使用说明](docs/TASKS-AND-REFERENCES.md)。保留现有角色参考联动。

## 安装要求与验证范围

- AstrBot **4.29.4 或更新的 4.x**、Python **3.12+**。当前实际验证环境为 Linux Docker / AstrBot 4.29.4 / Python 3.12；其他宿主版本需自行完成兼容验证。
- 安装包已含前端产物，使用者不需要安装 Node。前端开发使用 `frontend/package.json` 声明的 Node/pnpm 版本。
- 包含 QQ/NapCat、QQ 官方、Telegram、Discord、微信等既有适配。适配代码存在不等于各平台均完成现场验收；本次升级不向 QQ 发送测试消息。
- 依赖见 `requirements.txt`。FAISS 为可选向量加速，未安装时回退到 NumPy；没有 Embedding 模型时仍可使用文字检索。

## 首次安装

1. 在 AstrBot 插件管理中上传安装归档（`astrbot_plugin_visual_media-*-install.zip`）。
2. 若仍启用了旧的 `astrbot_plugin_stealer` 或 `astrbot_plugin_gemini_image_generation`，先禁用旧插件再启用本插件，避免重复命令和工具注册。旧数据迁移见后文。
3. 打开插件配置，根据需要完成下表。没有配置生成供应商时控制台显示“待配置”，并不代表插件启动失败。
4. 打开“视觉工坊”页面，分别检查表情图库、生图创作和角色形象。默认没有个人角色档案、参考图或聊天数据。

| 配置位置 | 用途 | 未配置时 |
|---|---|---|
| 表情包 → 视觉模型 | 分类、图片描述；也可被角色核对复用 | 尝试宿主图片描述模型，否则无法自动看图 |
| 表情包 → Embedding 模型 | 语义检索 | 使用可用的文字检索路径 |
| 生图创作 → 供应商 | 文生图、改图及角色创作 | 生图不可用，表情库仍可使用 |
| 媒体行为与角色核对 → 角色核对视觉模型 | 单独指定核对模型 | 留空沿用表情视觉模型或宿主图片描述模型 |

角色核对通常额外调用一次视觉模型；它不保证原作形象绝对一致。可以关闭，不影响生图。不通过或超时会保留生成图并说明结果，不自动循环重画。

## 功能与控制台

- **总览**：图库数量、角色数量、生成状态及近期图片。
- **角色形象**：外观、画风、公开参考图；按角色创作会带入档案并进行可选核对。角色和标准图由管理员自行导入，通用包不内置本机角色资料。
- **表情图库**：收集、审核、分类、检索、收藏、范围管理、导入与存储维护。
- **生图创作**：文生图、参考图改图、批量生成、表情切分、模型配置、模板、任务和历史。
- **表达与记录**：成员偏好、过滤与选择原因、生成任务、角色核对以及真实发送回执。可筛选会话，并恢复单个成员的默认偏好。

图库中的生成图收藏默认为原会话范围，管理员可明确公开。共享角色参考图必须是公开图片。删除画廊记录不会删除已收藏的表情。

## 媒体偏好和配置

明确自然语言指令包括“以后少发表情”“不要再发表情了”“我喜欢水彩画风”“这张别再发了”“恢复默认媒体偏好”。偏好绑定完整会话和当前成员，不会修改全群默认设置。识别采用保守规则，引用、假设和不明确的句式可能不保存；不要把任意闲聊当作可持久化指令。

在插件配置的 **媒体行为与角色核对** 中调整以下选项。更改后按宿主提示保存、重载；控制台“当前策略”显示实际生效值。无密钥配置示例见 [config-example.json](docs/config-example.json)。

| 选项 | 默认值 | 作用 |
|---|---|---|
| 偏好记忆 | 开启 | 可独立关闭，已有记录保留 |
| 相同图片去重 | 开启；1800 秒、最近 12 次 | 只计算确认发送，明确重发可放行 |
| 少发表情间隔 | 180 秒 | 仅对偏好“少用表情”的成员生效 |
| 相似图降序 | 开启；距离 8 | 配字相同且画面相近的候选降序 |
| 决策日志 | 开启；30 天、5000 条 | 关闭不影响偏好和去重；不是调用额度限制 |
| 角色核对 | 开启；60 秒、最多 2 张 | 超时或未覆盖全部图片时不会宣称全部通过 |

图片只在确认发出后进入近期记录。通用发送工具指定其他会话时，记录归属真实目标会话；该目标不会继承来源成员的偏好。`selected`、`submitted`、`generated` 均不等于 `delivered`。决策页使用匿名会话标识，不保存完整聊天原文。

## 聊天与其他插件

Chat Dynamics 控制参与和停止边界，本插件控制当前已获准回复回合的媒体选择。固定角色优先使用 `generate_character_image`，普通生图工具不会冒充经过角色核对。只在 JSON 中写 `media=generate` 不会生成图片，必须实际调用工具。

保留工具 `search_meme`、`send_meme`、`steal_meme`、`gemini_image_generation`、`gemini_image_batch`、`gemini_image_lookup`、`gemini_image_share`，以及 `list_visual_characters`、`generate_character_image`、`save_generated_meme`。旧 `gemini_` 前缀用于兼容，供应商不限于 Gemini。

常用命令：`/视觉帮助`、`/meme menu`、`/meme status`、`/生图 描述`、`/改图 描述`、`/换风格`、`/快速 表情包`、`/切图`。

其他插件可通过 `context.get_registered_star("astrbot_plugin_visual_media").star_cls.get_service(api_version=1)` 获取生成服务。协议见 [服务说明](docs/upstream-generation/plugin-api.md)，其中旧插件名称应替换为本插件名称。

## 数据、旧插件迁移与升级

所有运行数据位于 `data/plugin_data/astrbot_plugin_visual_media/`，配置位于 `data/config/astrbot_plugin_visual_media_config.json`。主要内容包括 `memes/`、`gallery/`、`visual_library.sqlite3`（角色和入库关联）、`media_behavior.sqlite3`（偏好、回执和决策）。配置的 `memes`、`generation`、`media` 三部分分别管理。

从旧的两个插件迁入时，**先停止旧、新插件并备份**。目标数据目录和配置文件必须不存在；首次误启动已创建空目录时，应先单独保存并处理该空目录，不要覆盖合并。

```sh
python tools/migrate_legacy.py \
  --data-root /AstrBot/data/plugin_data \
  --config-root /AstrBot/data/config \
  --destination /AstrBot/data/plugin_data/astrbot_plugin_visual_media \
  --config-destination /AstrBot/data/config/astrbot_plugin_visual_media_config.json
```

迁移只复制，保留原数据；SQLite 使用一致性备份并重写数据路径。目标存在、来源缺失或符号链接会拒绝执行。配置原子发布失败会撤回目标数据；同时被其他进程创建的配置不会被覆盖。原宿主全局 KV 的历史额度与在途任务不会迁移。

从 0.3 升级时不需要运行旧插件迁移工具。行为库自动标记 schema 版本 1，保留原有偏好、记录与表结构；遇到更高版本会拒绝打开。新增配置项采用兼容默认值。

回退应使用部署前的源码和配置备份，并先核对是否有后续修改。保留上线后新增的运行数据，只有明确需要恢复历史状态时才替换数据库。

## 开发、验证与私有打包

详见 [发布验证说明](docs/RELEASE-TESTING.md) 和 [更新记录](CHANGELOG.md)。基础命令：

```sh
python -m pytest integration_tests -q
python -m pytest tests -q
cd frontend
pnpm install --frozen-lockfile
pnpm test:unit --run
pnpm run build
cd ..
python tools/build_release.py --output /path/to/private-artifacts
```

后端测试需要已安装 AstrBot 的开发环境，且两个测试目录分别运行。构建工具生成本地安装包、完整源码包和 SHA-256 报告，不执行上传或公开发布。对应源码包包含前端源码、锁文件、测试与迁移工具。

## 来源和许可证

本项目整合两个已有插件的修改版快照，保留 AGPL-3.0 许可证、原作者署名与上游文档。详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)、[LICENSE](LICENSE) 和 `licenses/`。不声称这些快照等同于未经修改的上游标签。
