# 析间 0.4.0-dev.2 预发行

此版本面向 Windows x64 本地试用。解压 Windows 附件后运行 Xijian/Xijian.exe。
可以先用附带的合成 CSV 试用本地分析，再按节点指南配置合作 Agent。

- 支持已批准公共包的远程生成、进度查询、取消和原始候选取回。
- 公开样例失败后可准备修复版本，每份修复材料单独批准。
- 本机合成节点用打包客户端核验了断线后取回、修复、内部执行和原程序复用。
  两组示例观察值分别为 130 和 20。跨机器的 SSH 隧道和真实供应商账户待验收。
- 当前生成入口包括经批准的 Codex、OpenCode、Claude Code 及既有外侧路线。
  Cursor 和 Antigravity 的生成接入仍在验证，未包含在此次已验收包中。

项目采用 MIT 许可证。Windows 附件随附第三方许可和 SHA256SUMS；反馈请使用仓库 Issues，仅提交可公开的合成示例。

源码和 Windows 附件对应当前预发行版本。源码包文件见 source-allowlist.json。
Windows 原始构建 SHA-256 为 315f28879223da7db35ff74508ca15598ac37ddbd05b187d5f14f0af7c444b53；发布附件在相同运行文件之外附带许可、说明和合成示例。
