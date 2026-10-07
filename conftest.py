"""pytest 根配置。

这个文件放在项目根目录，pytest 会把该目录加入 sys.path，
这样测试里可以直接 `import app.xxx` / `import worker`，
不需要每个测试文件自己折腾 sys.path。
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
