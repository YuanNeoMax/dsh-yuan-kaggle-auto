"""适配插件布局：把「用户本地工作区的检查」与「引擎本身的检查」分开。

## 为什么需要这一层

原来的 66 项自测写在**用户本地工作区**（`D:\\mumu\\kaggle-auto`）里，
其中若干项检查的是那个仓库自身的东西：

  - `README.md` 是否把关键能力写进去了（`test_readme_sync.py`）
  - 测试清单与模块清单是否同步（`test_suite_audit.py`）
  - 真实比赛产物（`work/titanic`、`work/digit-recognizer`）是否在
    （`test_entered_state.py` / `test_write_sub_id.py` 的收尾断言）
  - `gen_*` 清理语义（`test_prune_intermediate.py`，需要特定中间产物）
  - 真实 e2e 成本估算（`test_real_iterate.py`，读那个仓库的脚本）

装进 npm 包之后这些**前提不再成立**：插件包的 README 是另一份文档，
也不带真实比赛的运行产物。它们失败不是引擎退化，而是"问错了地方"。

## 处置

对每一项都做语义正确的降级，而不是删测试：

  - 目标文件不存在 → SKIP（不是 PASS、也不是 FAIL），并在输出里说明原因；
  - 目标存在 → 照原样执行（开发者在本地工作区里跑时要保持原有效力）。

这样同一套测试在两种布局下都给出**可信**结论。
"""
from __future__ import annotations

import sys
from pathlib import Path

SKIP_CODE = 77          # 与 pytest 的"跳过"约定一致，宿主可据此区分


def engine_root() -> Path:
    return Path(__file__).resolve().parent.parent


def needs_file(relative: str, why: str) -> bool:
    """目标缺失时打印原因并退出 SKIP_CODE。返回 True 表示可以继续。"""
    path = Path(relative)
    if path.exists():
        return True
    print(f"SKIP  {relative} 不存在 —— {why}")
    raise SystemExit(SKIP_CODE)


def needs_dir(relative: str, why: str) -> bool:
    path = Path(relative)
    if path.is_dir():
        return True
    print(f"SKIP  {relative} 不存在 —— {why}")
    raise SystemExit(SKIP_CODE)


def announce(name: str) -> None:
    print(f"[layout-guard] {name}: 插件包布局检查已启用", file=sys.stderr)
