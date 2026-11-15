# 此刻

更新：2026-11-15 14:40 NZDT（Claude，ai-limit 会话）

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

- Google 卡片报 403“Google CLI auth expired”：需要用户重新登录 Antigravity / Gemini CLI，代码侧无事可做

## 最近决定

- 不经用户同意不推送、不开 PR
- 0.3.57 是另一会话的 Claude 浏览器配置修复；OpenAI 额度读取修复归入 0.3.58
- RunningHub 只显示积分（附到期时间）和钱包，不再显示 RH 币
- 刷新失败后保留的旧数值统一用品红色 `#f0abfc`，与红 / 琥珀 / 橙警戒色区分
- 各服务并行抓取，回来一个更新一张卡；启动时可先显示 6 小时内的缓存

## 先读

- `README.md`
- `ai_limit/version.py`
- `menubar/ai-limit-app.py`
- `ai_limit/openai_platform.py`
- `ai_limit/api_credits.py`
- `ai_limit/providers.py`
