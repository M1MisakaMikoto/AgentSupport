> 简体中文 | [English](001-src-layout-and-runtime-boundaries.en.md)

# ADR-001：Src 布局与运行时边界

## 状态

已接受（Accepted）

## 决策

采用 `src/` 包布局，并将 AgentSupport 控制面、共享 Runner 契约与 Session Runner 视为独立的代码
边界。依赖构建集中在 `agentsupport.bootstrap.container`。

## 后果

可编辑安装与 wheel 安装解析到相同的包树。Runner 部署不依赖 AgentSupport 内部实现。根级模块提供
简洁的公共导入，实现保留在分层包结构中。
