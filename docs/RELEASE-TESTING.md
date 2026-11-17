# 0.4.0-beta.2 发布验证

此版本为私有测试版，不自动上传仓库、创建 Release 或提交插件市场。`repo` 暂留空，`author: visual_media` 为项目标识；公开前再确定维护者和仓库地址。

## 测试环境

- 后端：Python 3.12、AstrBot 4.29.4，Linux Docker。最低宿主声明为 4.29.4；其他 4.x 版本尚需各自验证。
- 前端：Node 24、pnpm（由 frontend/package.json 声明版本），锁文件安装与构建。
- 已实现的平台适配列在 metadata.yaml；这不代表每个平台都做过现场发送验收。本轮不发送 QQ 测试消息，也不要求用户手动配合。
- 自动化测试使用隔离数据和假供应商；真实模型检查应另记为网络验收，不混入离线测试数量。

## 可复现流程

在已有 AstrBot 开发环境中，把本项目放在父目录下，确保该父目录和 AstrBot 源码根目录在 `PYTHONPATH`。不使用正在运行的插件数据目录。

```sh
python -m pip install -r requirements-tested.txt
python -m pytest integration_tests -q
python -m pytest tests -q
cd frontend
pnpm install --frozen-lockfile
pnpm test:unit --run
pnpm run build
cd ..
python tools/build_release.py --output /path/to/private-artifacts
```

两组 Python 测试分别运行，防止上游测试桩覆盖真实 AstrBot 类。`requirements-tested.txt` 记录本轮已验证的插件依赖，不是宿主全部依赖的锁文件。普通安装使用 `requirements.txt`；FAISS 可选，未安装时使用 NumPy 检索。OpenCV、SciPy、scikit-image 当前仍是切图模块的必要依赖，不宣称可任意删除。

## 发布门槛

1. 空数据目录启动，无旧插件、无 Chat Dynamics、无供应商时仍能打开控制台，生成状态显示待配置。
2. 老库迁移保留源文件，WAL 数据可读，目标已存在时拒绝覆盖；配置发布失败后可重试。
3. 0.3 的行为库原地升级保留偏好、回执与日志；更高数据库版本拒绝打开。
4. 当前会话、跨会话、裸会话 ID、目标不匹配和错误回执均覆盖；跨会话发送不消耗来源会话的发送状态。
5. 角色核对覆盖矛盾结果、无依据、部分检查、缺图、关闭、超时和无视觉模型，不把未知当作通过。
6. 磁盘与指纹计算不阻塞事件循环；同步发送回调不查询数据库、不重新计算指纹；卸载前排空已接收的审计任务。
7. ZIP 使用白名单构建，保留许可证与上游声明；不包含运行数据库、密钥、缓存、个人角色参考图或本地验收输出。

安装包和完整源码包一同保留。源码包包含前端源码、锁文件与测试；安装包包含已构建前端。`release-report.json` 给出 SHA-256，包内 manifest 用于核对文件。构建工具只写本地文件。

## 上线与回退

上线前确认没有正在生成的任务，保存插件源文件、配置与 SQLite 一致性备份；核对源文件没有被其他工作修改后，仅替换本插件并通过 Dashboard 热重载。新增 `media` 配置采用与 0.3 一致的默认值，原 `memes`、`generation` 设置不得丢失。

回退源码前先核对部署 manifest，禁止覆盖上线后他人修改。行为数据库升级仅增加版本标记，结构保持兼容；回退不主动清除新偏好。备份数据仅用于明确需要恢复数据时，避免覆盖上线后的正常聊天记录。
