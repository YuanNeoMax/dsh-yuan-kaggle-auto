# 第三方声明

本包的 `engine/` 目录携带 Python 引擎源码，插件层为 TypeScript。
以下为其中涉及的第三方组件与各自的许可。

## 运行期依赖（由引擎 venv 安装，不随本包分发）

| 组件 | 许可 | 用途 |
|---|---|---|
| pandas | BSD-3-Clause | 表格处理 |
| numpy | BSD-3-Clause | 数值计算 |
| scikit-learn | BSD-3-Clause | 交叉验证与基线模型 |
| LightGBM | MIT | 梯度提升树 |
| kaggle (CLI) | Apache-2.0 | Kaggle 官方命令行 |
| kagglehub | Apache-2.0 | 数据集下载 |

这些依赖在插件首次初始化时由 pip 安装到独立 venv，**不进入本仓库的版本库，也不进入 npm 包体**。

## 宿主依赖（由 DSH 提供，不随本包分发）

`@deepseek-ai/cordis`、`@deepseek-ai/schemastery`、`@deepseek-ai/dsh-tools`、
`@deepseek-ai/dsh-settings` 等由运行中的 DSH 宿主提供；本包以 peer 依赖方式声明，
不内联第二份副本（内联会导致 Loader 的配置写入路径失效）。

## 可选组件（不随本包分发，由用户选择安装）

### dsh-ego-browser

- 仓库：https://github.com/Fisfzy/dsh-ego-browser
- 维护者：Fisfzy
- 许可：MIT

本插件的「安装 ego」动作**只代为安装**：执行包管理器安装、写 profile 的
`package.json` 与装配层、热装载。它不修改该插件的代码，也不修改其配置。

该插件内部嵌入 ego-lite 的运行时代码：

- ego-lite：https://github.com/CitroLabs/ego-lite，MIT
- Windows 平台为社区移植版本，属实验性支持；macOS 为官方支持平台

### kaggle-skill

- 仓库：https://github.com/shepsci/kaggle-skill
- 维护者：shepsci

本包**不携带**其代码。仅在用户把「skill 桥接目录」指向一个既有 checkout 时，
通过子进程桥接调用它的脚本。缺失时本包全部核心功能照常可用。

## 商标

Kaggle 是 Kaggle Inc. 的商标。本插件由社区维护，与 Kaggle Inc. 无隶属、
无背书、无赞助关系。使用时请遵守各比赛规则与 Kaggle 服务条款。
DeepSeek、DeepSeek Harness 相关名称归其各自权利人所有。
