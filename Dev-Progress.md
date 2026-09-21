# AgentSupport × Document-Assistant 对接进度

> 本文件在 AgentSupport 仓库根目录维护，并同步一份到 Document-Assistant 仓库根目录（两边内容一致）。
> 更新时间：2026-09-21

## 本轮：用户手改 → 修订 / 高亮 / 「接受修订」按钮

现象（用户 2026-09-21 实测）：AI 没动过文档、只有用户手改时，「接受修订」的按钮一直不出现。

根因（lab 实测，不是推断）：

1. 横幅与按钮的显示条件读的是**落盘文件**里的修订数，而 ONLYOFFICE 自己落盘很慢 ——
   实测用户改完 1–2 分钟文件还是旧的，甚至要关掉文档（回调 `status 2`）才写回。
2. 「记录修订」以前靠 ui-trim 插件调 `asc_SetTrackRevisions(true)`，而插件要等文档 ready 之后才加载
   （实测 24–28s）—— 这段时间用户改的字**根本没被记成修订**（没高亮、不进计数、AI 也看不到）。

改动：

1. **编辑器配置层**：`permissions.review = can_edit` + `customization.review.trackChanges = true`
   → 打开即记录修订（实测 ~9s，远早于插件）。依据：官方 schema（`review.trackChanges`）与
   `ReviewChanges.js:958` 的消费点，而 `canReview` 被 `Main.js:1718` 门控 ——
   这正是以前 `review=false` 时开不了跟踪修订的原因。
   AI 持锁（`edit=false`、`mode=view`）时仍 `review=false`：否则 ONLYOFFICE 的「审阅模式」会给用户
   留一条"提修订意见"的写入口，破坏串行锁（实测锁后 `canEdit=false`、打字不进文件）。
2. **落盘通道**：ui-trim 插件新增宿主 `postMessage({type:'save'})` → `Asc.editor.asc_Save()`
   （编辑器工具栏「保存」按钮的同一条路）；前端用户一改就显示修订横幅并自动触发落盘，
   点「接受修订 / 应用我的修改 / 清除变更标记」之前先落盘，再动服务端文件。
3. **界面收敛**：插件新增隐藏 `#btn-doc-review`、`#btn-review-on`、`#btn-review-view`、
   `#btn-change-prev/next/accept/reject`（`review=true` 之后回来的审阅入口）。
4. **单测**：`tests/test_doc_host_accept_mine.py`、`tests/test_doc_host_revision_pairs.py` 共 16 条通过。

待验证（本轮被打断）：协同模式下 `asc_Save()` 有时被文档服务器判为"无改动"
（CommandService `forcesave` 返回 `error 4`），只有关闭文档才回调 `status 2` 写回；
正在逐条验证 Ctrl+S / 插件通道 / 关闭会话 三条落盘路径（`lab/editor-probe/probe_save_paths.py`）。
落盘触发方式定下来之后再复验「应用我的修改」（只并入人的修订、AI 的修订保持不动）。

## 未完成（沿用原交接 + 最新状态）

1. 部分业务（如在有未处理的保存时发送对话消息）缺乏事务保证，会出现中间状态 —— 未修。
2. Agent 技能与工具换了新版但未优化，需要按旧版 Document-Assistant 迁移 —— 进行中
   （MCP 工具面已重做 `read/write/append/insert/delete/replace` + 统一 scope 语法，旧入口保留并标注）。
3. ONLYOFFICE 内置功能仍有不需要在网页展示的入口 —— 进行中（本轮补了审阅类入口的隐藏，仍需系统盘点一遍）。
4. 相邻「删除 + 插入」合并成一次编辑：sdkjs 补丁（`patch_oo_rev_pair.sh`）在排查"只读"问题期间**停用**，
   目前靠保存回调里的 `revision_pairs` 归一；"修改类型"仍未进审计模型 —— 未完成。
5. 用户改动的黄底应该第一时间出现（现在是保存回调时补的外挂）—— 未完成（本轮把落盘时机提前了）。
6. AI 对话记录还没入库，不能重建对话 —— 未完成。
7. 工期紧，代码来不及优化，找 Bug 修 Bug。
8. 按业务需求与用户反馈继续优化。

## 环境注意事项（2026-09-21 踩到）

- da-server 重建之后，Windows→WSL 的 18080 端口转发会失效（前端代理报 502）：
  `wsl --shutdown` 之后跑 `lab/editor-probe/recover_lab_stack.sh` 可恢复。
- 改文档服务器静态资源（插件 / CSS）必须三件套：`install_ui_trim_plugin.sh`、
  `bump_oo_asset_token.sh`、并把 da-server 的 `DOCS_ASSET_VERSION` 一起改掉（浏览器是 immutable 缓存）。
- `GET /api/doc-host/{pid}/revisions` 读的是**落盘文件**，不是编辑器内存 —— 排查"按钮不出现"时先看这条。
