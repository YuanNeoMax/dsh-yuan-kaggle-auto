"""kauto —— 本地优先的 Kaggle 自动参赛流水线。

链路：list → prepare → baseline → iterate(LLM 迭代) → submit(需确认)

与现成仓库的差别：模型的迭代判定完全落在本地 CV 上，不消耗线上提交次数；
LLM 只负责写"特征工程 + 拟合"这一小段，其余（下载/切分/编码/评分/提交）都是确定性代码。
"""
__version__ = "0.1.0"

from . import runtime  # noqa: F401  （必须在任何 numpy/lgbm 导入前设置线程上限）
from . import kapi, prep, model, iter  # noqa: F401
