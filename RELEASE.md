# 析间 0.4.0-dev.4 预发行

本版把内侧模型的字段授权放在提案前，并让新候选程序在本机专属 Windows AppContainer 中执行。适用于受信任 Windows 单机上的合成数据试用。

- **控制内侧输入。** 提案默认只传任务原文、获准字段名和类型。需要样例时，同时选择字段与行号。结果行另行授权，默认留在本机。
- **核对外侧披露。** 批准前可逐字段查看用途、外侧接收方和实际公共 JSON。程序返回后保留原始字节。
- **隔离执行与换表复用。** 新候选使用标准 AppContainer、零额外能力和单进程 Job。隔离建立失败时停止。最终包的回环合成验收中，同一程序对两张表分别得到 13 和 6。公开样例、首次执行、换表复用三次均有隔离回执，重开后两次结果均可读取。

## 下载与启动

下载 `Xijian-0.4.0-dev.4-windows-x64-trial.zip` 并完整解压，双击 `Xijian/Xijian.exe`。需要 Windows 10/11 x64 和 Microsoft Edge WebView2 Runtime。包内含运行时、两份合成 CSV、试用步骤及第三方许可材料。程序尚未签名。

Windows 附件为 297239926 字节，SHA-256 为 `e599c9ef9992cad05f0eb270fe4b168e92eede73bf682495501eb3b07a80a203`。另附 `Xijian-0.4.0-dev.4-source.zip`，其中公开文件及摘要见 `source-allowlist.json`。本次 Windows 构建的产品来源提交为 `9076246209a72a35c7ae2d3edbdf960cf586b40f`。

## 验收范围

本轮使用合成数据及本机替身，真实模型调用为零。容器拒绝读取任务外的测试标记和连接本机监听端口。标准 AppContainer 仍可读取 Windows 或其他软件明确开放给所有应用包的公共资源。LPAC 在本机加载当前冻结版 Python 运行库时受阻。本版采用标准 AppContainer。

独立试用者在干净 Windows 机器上的验收仍待安排。按用户和任务授权、数据保留与删除需要进一步验证。当前仅限合成数据试用。

[入门与示例](README.md) · [GitHub Release](https://github.com/kevin1000x/xijian/releases/tag/v0.4.0-dev.4) · [MIT 许可证](LICENSE)
