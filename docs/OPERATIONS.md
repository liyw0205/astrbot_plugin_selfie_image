# 停机备份与恢复演练

`scripts/backup_restore.py` 仅使用 Python 标准库，面向 Linux/Termux 的停机维护。脚本不发现或扫描真实 AstrBot 数据，不停止服务，不改配置，不执行升级，也不清理原目录。所有数据访问均从操作人显式指定的 `--data-root` 或 `--backup-dir` 开始。

## 覆盖范围

插件通常把数据放在 AstrBot 数据目录下的 `plugin_data/astrbot_plugin_selfie_image/`，但自定义部署可能不同；执行前从部署配置确认实际路径。备份整个指定目录，包含未知的普通文件和空目录，避免只保留索引却丢失媒体：

| 数据 | 当前布局 | 校验与恢复行为 |
| --- | --- | --- |
| 插件配置 | `selfie_image_config.json` 及旧配置/`.bak` | 原样保留；所有 `.json` 验证能否解析，不输出内容 |
| 生成记录 | `generation_records.sqlite3`、旧 `generation_records.json`/`.bak` | SQLite 使用 `Connection.backup()`，保留已提交但仍在 WAL 中的数据；检查完整性、外键及 schema/user version |
| 任务与用量 | `generation_tasks.json`、`usage_stats.json` | 保留持久化任务状态、快照元数据和用量，不尝试恢复进程中的任务 |
| 来源与媒体 | `media_sources/`、`image_cache/` | 保留 sidecar、原图、引用图、派生媒体，不自动清理孤儿 |
| 形象与日程 | `image_persona.json`、`image-persona/`、`daily_schedule.json` | 保留主形象、辅助形象、日程和配置 |
| 其他业务状态 | `studio_sessions.json`、`creative_canvas/`、`asset_collections.json`、预设、COS 池、上下文和健康历史 | 按实际存在的文件完整保留 |

AstrBot 传入的原生配置可能存放在插件数据目录之外，且可能包含渠道密钥。此工具不会读取 `_native_config_path` 或自动扩展备份范围。升级前需要按 AstrBot 的运维流程单独保护原生配置、部署文件及当前插件版本；不能把本目录备份视为整套 AstrBot 备份。

SQLite 主库的 `-wal`、`-shm`、`-journal` 不作为独立恢复文件复制：标准 backup API 已生成完整快照，目标库转为 DELETE 日志模式，避免把旧日志与恢复后的库混用。仅对已识别 SQLite 主库的伴随文件应用该规则。

## 升级前

1. 暂停群聊/LLM 工具/Web 入口的新任务提交，并通知维护窗口。脚本没有全局任务锁，`--stopped` 只是确认运行条件。
2. 在任务页等待 queued/running 任务结束；无法等待时使用现有任务取消入口或 `/生图取消`。不要把强杀进程当作常规停止流程。
3. 正常停用插件或停止 AstrBot，并等待停止完成。插件 `terminate()` 会停止 Web、取消并等待 worker、等待记录/用量提交、释放额度预约并强制刷盘任务快照。确认没有第二个进程仍在写同一目录。
4. 确保备份目录的父目录存在，且可用空间足够同时容纳备份及临时 staging。选用数据根目录以外的全新路径。

以下路径均为示例，必须替换成已确认的路径；命令不会读取示例之外的真实数据。

```sh
python scripts/backup_restore.py backup \
  --data-root /srv/astrbot/data/plugin_data/astrbot_plugin_selfie_image \
  --backup-dir /srv/astrbot-backups/selfie-image-before-upgrade \
  --stopped

python scripts/backup_restore.py verify \
  --backup-dir /srv/astrbot-backups/selfie-image-before-upgrade
```

不写子命令时默认执行 `backup`。`restore` 必须显式输入。源目录和备份目录不允许相同或相互包含，任何已有备份目标都会被拒绝；原数据不会被覆盖。备份中包含 `manifest.json` 和 `data/`，清单记录 UTC 时间、原始数据根路径、文件大小、SHA-256、目录和 SQLite 版本信息。

备份目录、恢复目录及所有子目录使用 `0700`，文件使用 `0600`。验证会拒绝组/其他用户可读取的备份；复制到其他介质后应保留这些权限。备份未加密，也没有签名，SHA-256 用于发现损坏，不证明来源可信。不要把含密钥的备份放入公开仓库或公开网盘。

## 恢复演练

先恢复到一个全新的临时目录，检查清单、数据完整性和业务引用；演练期间保持生产插件停止，或使用完全隔离的 AstrBot 实例。不要让测试实例读取生产绝对路径。

```sh
python scripts/backup_restore.py restore \
  --backup-dir /srv/astrbot-backups/selfie-image-before-upgrade \
  --data-root /srv/astrbot-rehearsal/selfie-image \
  --stopped
```

恢复前验证备份的完整文件/目录清单、SHA-256、JSON 和 SQLite；复制后在 staging 中再次验证，全部成功才原子发布完整目录。工具只接受不存在的恢复目标，连已有空目录也不覆盖。复制、校验或发布前发生异常时删除本次 staging 和空预留目录；此前的数据和备份保持不变。文件系统自身故障可能阻止清理，残留的隐藏 staging 仍限制为当前用户可读。发布后目录 fsync 失败意味着持久化状态不确定，保留已发布完整目录供检查，不进行破坏性回滚。

`image_persona.json` 及其他历史数据可能保存绝对路径。恢复到不同位置时不会自动重写它们，避免替换不属于本插件的数据。上述演练可核验文件和数据库，但不能直接证明迁址后运行正常。实际回退优先恢复到原始绝对路径；迁址需人工检查并重新设置形象等路径，再用隔离实例验证。

## 升级与回退

保留旧插件代码/版本和独立的原生配置备份后再升级。首次启动会执行现有 schema 迁移和任务重启协调，旧 queued/running 状态可能变为 expired；备份本身不会改这些状态。

升级后通过已认证的管理页面检查 `/api/health` 和只读的 `/api/records/consistency`。至少逐项核对：

- 生成记录数量、收藏/置顶、标签/备注、模型和关键词分页查询；
- 任务历史、终态、用量和额度，不存在重复发送或重复恢复运行中的任务；
- 记录详情可读取对应 sidecar，图片预览/原图可打开，收藏媒体仍存在；
- 主形象、辅助形象、日程、两套画布及集合引用可读取；
- schema/user version 和迁移结果符合目标版本；一致性报告中的缺失媒体、缺失 sidecar 和孤儿项已检查。

不要在未核对报告时调用修复接口删除孤儿。记录升级版本、迁移开始/结束时间、校验结果和失败原因，避免记录密钥、完整 Token 或媒体 URL。

回退时先再次正常停止插件。由操作人将失败版本的数据目录移动到独立保留位置，保留排障证据，并恢复旧插件代码和匹配的原生配置；然后将备份恢复到原路径：

```sh
python scripts/backup_restore.py restore \
  --backup-dir /srv/astrbot-backups/selfie-image-before-upgrade \
  --data-root /srv/astrbot/data/plugin_data/astrbot_plugin_selfie_image \
  --stopped
```

工具不会替操作人移动或删除已有目录，没有 `--force` 覆盖选项。恢复后先检查数据再开启任务入口，并重新执行上述业务检查。

## 限制与自动测试

工具验证磁盘快照及结构，不实施业务 schema 升级，不判断所有跨文件媒体引用，不读取远程媒体，也不提供跨多个活动数据库/JSON 的在线事务快照。源目录在备份过程中变化会被拒绝；SQLite 读操作自身可能修改 SHM read marks，或为已关闭的 WAL 数据库创建空 WAL/SHM，因此仅忽略 SHM 变化和初始不存在、结束时仍为空的 WAL。已有 WAL、含事务数据的新 WAL 和主库仍检查变化。停机条件仍需由部署流程保证。

脚本不修改插件健康页，也不会自动把恢复原因写入运行期状态。恢复原因和迁移时间目前保留在操作记录中；运行期迁移失败的只读降级与健康报告仍由后续开发处理。

可重复的演练只使用 pytest 临时目录，不操作真实数据：

```sh
python -m pytest -q tests/test_backup_restore.py
```

测试覆盖 WAL 中已提交记录、任务/形象/sidecar/缓存往返、私有权限、默认备份及显式恢复 CLI、损坏/未列出文件、SQLite 完整性/版本、路径穿越、符号链接/特殊文件、目标覆盖拒绝及复制失败后的原子性。
