# 此刻

更新：2026-11-15 16:13 NZDT（Codex，总控轮换前核对）

## 一句话现状

Air 和 Mini 都已装 0.3.59，仓库都在已推送的 origin/main；两台 API 的 OpenAI 赠送额度和 Claude 都能实时读到。

## 当前目标

- 菜单栏和本地 API（`127.0.0.1:17655`）的各家额度读得准、刷新及时，并在 Air / Mini 两台保持一致

## 进行中

- 窗口拖到笔记本屏后不再跳回（0.3.56 已修） / 用户 / 下一步：用户实际拖一次确认

## 等你拍板

- Mini 上 AIX `feat/venice-provider`（`c39c3d4e4`、`742cf1df5`）未推送也未安装，是否安装
- 之前误打印过的 Venice key 何时更换（用户说过稍后再换）

## 卡住

- 无已确认的项目阻塞；Gemini / Google 已退订，不以恢复付费额度为目标

## 最近决定

- 用户批准会话轮换，当前项目只保留一个 Codex 总控；新总控先只读报告，通过旧总控核对后接管
- 按 Astra 总控流程：派工前精简 TaskGraph，按实时目录选择 Sol/Terra Worker 与推理强度，记录 internal/external 通道，独立复审后最终验收
- 不经用户同意不推送、不开 PR
- 0.3.57 是另一会话的 Claude 浏览器配置修复；OpenAI 额度读取修复归入 0.3.58
- RunningHub 只显示积分（附到期时间）和钱包，不再显示 RH 币

## 先读

- `/Users/mac/.codex/AGENTS.md`
- `README.md`
- `ai_limit/version.py`
- `menubar/ai-limit-app.py`
- `ai_limit/openai_platform.py`
- `ai_limit/api_credits.py`
- `ai_limit/providers.py`
