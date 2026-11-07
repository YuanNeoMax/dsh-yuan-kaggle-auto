"""端到端验证 AppContainer 的文件隔离。

流程：
1. 建 profile，拿到 SID
2. 只授权 python 解释器所在目录（否则起不来）
3. 容器内跑探针：尝试读凭据 / .dsh 目录 / hosts
4. 判据：凭据必须读不到，而工作目录（已授权）必须读得到

如果凭据能读到，就说明隔离失败 —— 我不想看到那种"看起来成功了"的假通过。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kauto import appcontainer as ac

NAME = "kauto.sandbox"
PY = sys.executable
pyroot = str(Path(PY).parent.parent)      # .venv 根，含 Lib/site-packages
home = Path.home()
creds = str(home / ".dsh" / ".credentials.yaml")

print("=== 1) 建 profile ===")
sid = ac.ensure_profile(NAME)
print("   SID:", ac._sid_to_string(sid))

print("=== 2) 授权 python 目录（最小必要面）===")
# venv 的 python.exe 只是 launcher，真正的解释器在 base_prefix。
# 少了它进程会以 0xC0000142（DLL_INIT_FAILED）秒退 —— 实测踩到。
import sysconfig
import contextlib

base_root = Path(sys.base_prefix)
venv_root = Path(sys.prefix)
dll_dir = base_root / "DLLs"
std_lib = base_root / "Lib"
site = Path(sysconfig.get_paths()["purelib"])

grant_list = [str(x) for x in (base_root, dll_dir, std_lib, venv_root, site,
                               Path("work").resolve())]
for p in grant_list:
    r = ac.grant_read(p, NAME)
    ok = r.get("ok")
    print(f"   {'OK ' if ok else 'FAIL'} {p}")

probe = "\n".join([
    "import sys",
    "out = []",
    "for p in sys.argv[1:]:",
    "    try:",
    "        with open(p, 'rb') as f:",
    "            f.read(32)",
    "        out.append('READABLE ' + p)",
    "    except PermissionError:",
    "        out.append('DENIED ' + p)",
    "    except FileNotFoundError:",
    "        out.append('NOTFOUND ' + p)",
    "    except Exception as e:",
    "        out.append('BLOCKED ' + p + ' ' + type(e).__name__)",
    "print(chr(10).join(out))",
    "",
])

# 写成文件再跑：`python -c` 传多行脚本会被命令行转义/截断，
# 实测报过 SyntaxError: '(' was never closed。
workdir = Path("work/demo-comp").resolve()
probe_path = workdir / "_ac_probe.py"
probe_path.write_text(probe, encoding="utf-8")

print("\n=== 3) 容器内读取尝试 ===")
res = ac.run_in_container([PY, str(probe_path), creds, str(home / ".dsh"),
                           str(workdir / "train.csv")],
                          cwd=str(workdir), name=NAME, timeout=60)
print("   rc:", res.get("rc"), "err:", res.get("error"))
if res.get("stderr"):
    tail = [l for l in res["stderr"].splitlines() if l.strip()][-2:]
    print("   stderr:", " | ".join(tail)[:220])
print("   --- stdout ---")
for line in (res.get("stdout") or "").splitlines():
    print("     ", line.strip())

print("\n=== 判读 ===")
out = res.get("stdout") or ""
cred_line = next((l for l in out.splitlines() if ".credentials.yaml" in l), "")
work_line = next((l for l in out.splitlines() if "train.csv" in l), "")
denied = ("DENIED" in cred_line) or ("BLOCKED" in cred_line)
work_ok = "READABLE" in work_line
print("   凭据:", (cred_line.strip() or "(无输出)"))
print("   工作目录:", (work_line.strip() or "(无输出)"))
print(f"   → 凭据被隔离      : {denied}")
print(f"   → 授权目录可访问  : {work_ok}")
print(f"   → 结论            : {'PASS' if (denied and work_ok) else 'FAIL 需复查'}")

probe_path.unlink(missing_ok=True)
