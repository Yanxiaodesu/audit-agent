"""配置。

配置来源优先级：**环境变量 > .env 文件 > 代码默认值**。
也就是说 `.env` 里写了、但命令行又 set 了同名变量，以命令行为准（方便临时覆盖）。

LLM 部分是可选的：没有 API Key 时整个服务仍能运行，
审核走「规则引擎 + 人工复核」，只是不启用模型层。
"""
import os
import pathlib


def _load_dotenv(path: pathlib.Path) -> None:
    """极简 .env 加载器，不引入 python-dotenv。

    只支持 `KEY=VALUE`、`#` 注释、值可选用引号包裹。
    已存在的环境变量**不会被覆盖**，这样命令行仍可临时改。
    """
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.split("#")[0].strip() if not val.strip().startswith(('"', "'")) else val.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"
_load_dotenv(ENV_FILE)

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "127.0.0.1"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", "root"),
    "charset": "utf8mb4",
    "autocommit": False,
}

META_DB = os.getenv("META_DB", "audit_agent")     # Agent 自己的库
BIZ_DB = os.getenv("BIZ_DB", "campus_market")     # 平台业务库

# ------------------------------------------------------------
# LLM（可选）。支持任何 OpenAI 兼容接口。
#   DeepSeek : LLM_BASE_URL=https://api.deepseek.com/v1   LLM_MODEL=deepseek-chat
#   硅基流动  : LLM_BASE_URL=https://api.siliconflow.cn/v1 LLM_MODEL=deepseek-ai/DeepSeek-V3
# ------------------------------------------------------------
LLM_API_KEY = (os.getenv("LLM_API_KEY")
               or os.getenv("DEEPSEEK_API_KEY")
               or os.getenv("SILICONFLOW_API_KEY")
               or os.getenv("OPENAI_API_KEY")
               or "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "60"))

# LLM_MODE:
#   live  —— 真调模型（需要 LLM_API_KEY）
#   mock  —— 用脚本化的假响应驱动循环，不需要 key
#            用途是验证「工具调用 / 消息拼装 / 终止条件」这条代码路径 ——
#            没有 key 时它永远不会被执行，是最容易藏 bug 的地方
LLM_MODE = os.getenv("LLM_MODE", "live").lower()
if LLM_MODE == "mock":
    LLM_ENABLED = True
else:
    LLM_ENABLED = bool(LLM_API_KEY)

# mock 模式下每轮"模型响应"的模拟延迟（秒）。
# 设成和真实模型接近的值，就能做一个**可复现、零成本**的并发性能基准。
MOCK_LLM_DELAY = float(os.getenv("MOCK_LLM_DELAY", "0"))

# ------------------------------------------------------------
# 任务与重试
# ------------------------------------------------------------
# worker 被 kill 后，任务会永远卡在 running。超过这个时长就回收。
JOB_LOCK_TIMEOUT_MIN = int(os.getenv("JOB_LOCK_TIMEOUT_MIN", "10"))
# 单个任务最多被领取几次，超过就标记 failed，不再无限重试
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "3"))
# 每处理多少个任务检查一次僵尸任务（也可以只靠 worker 启动时检查）
RECLAIM_EVERY = int(os.getenv("RECLAIM_EVERY", "50"))

# 每个 worker 进程内并发处理的任务数。
# 任务是 IO 密集型（等模型 HTTP 响应 + 等数据库），线程池就够了。
# 单个 worker 开了并发之后，通常不再需要手动开多个窗口。
WORKER_CONCURRENCY = int(os.getenv("WORKER_CONCURRENCY", "8"))

# LLM 调用失败时的重试次数（只对 429/5xx/网络错误重试，401/402/404 重试没意义）
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "3"))

# ------------------------------------------------------------
# LLM 限流与成本护栏
#
# 三道护栏：
#   1. 速率限制（令牌桶）—— 全局共享，多进程也生效
#   2. 日预算上限      —— 超过就不再调模型
#   3. 日调用次数上限  —— 兜底，防止单价估算错误导致失控
#
# 触发任何一道都是**降级**（商品转人工复核），不是把任务判失败。
# ------------------------------------------------------------
# 每分钟允许的 LLM 调用数；设为 0 表示不限速
LLM_RATE_PER_MIN = float(os.getenv("LLM_RATE_PER_MIN", "60"))
# 令牌桶容量（允许的瞬时突发）；默认等于一分钟的量
LLM_RATE_BURST = float(os.getenv("LLM_RATE_BURST", str(LLM_RATE_PER_MIN)))
# 取不到令牌时最多等多久（秒），等不到就降级
LLM_RATE_WAIT_MAX = float(os.getenv("LLM_RATE_WAIT_MAX", "20"))

# 单价（每 100 万 token）。**按你实际用的服务商官网价格填写**，这里只是占位默认值。
LLM_PRICE_INPUT_PER_M = float(os.getenv("LLM_PRICE_INPUT_PER_M", "2.0"))
LLM_PRICE_OUTPUT_PER_M = float(os.getenv("LLM_PRICE_OUTPUT_PER_M", "8.0"))
LLM_PRICE_CURRENCY = os.getenv("LLM_PRICE_CURRENCY", "CNY")

# 日预算上限；设为 0 表示不限
LLM_DAILY_BUDGET = float(os.getenv("LLM_DAILY_BUDGET", "10.0"))
# 日调用次数上限；设为 0 表示不限
LLM_DAILY_CALL_LIMIT = int(os.getenv("LLM_DAILY_CALL_LIMIT", "2000"))

# 护栏总开关。mock 模式会自动跳过（不做真实调用，且会扭曲压测结果）
LLM_GUARD_ENABLED = os.getenv("LLM_GUARD_ENABLED", "1").lower() not in ("0", "false", "no")

# ------------------------------------------------------------
# 管理接口鉴权
#
# 留空 = **不启用**（本地演示方便），但启动时会打一条警告。
# 一旦配置，下面这些接口必须带 `X-Admin-Token` 请求头：
#   POST   /admin/reset              清空全部任务与轨迹
#   DELETE /items/{id}?hard=true     物理删除商品及审核记录
#   POST   /items/{id}/human-decide  把商品改成在售或驳回
#
# 为什么必须有：这些接口在无鉴权时，同网段任何人一次请求就能
# 清空整个审核库、或把任意商品改成在售。演示环境尤其危险。
# ------------------------------------------------------------
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()

# ------------------------------------------------------------
# 调查预算（Agent 层的成本控制）
#
# 不加限制时，模型会对**每个**商品把 6 个工具全调一遍
# （实测 31/37 个任务的工具组合完全相同）—— 这不是决策，是固定流程。
#
# 给一个预算，模型就必须判断「这个商品值不值得调查、先查哪个」。
# 设为 0 表示不限（回到固定流程）。
# ------------------------------------------------------------
LLM_TOOL_BUDGET = int(os.getenv("LLM_TOOL_BUDGET", "3"))

# ------------------------------------------------------------
# 审核阈值
# ------------------------------------------------------------
# 自动通过的门槛：规则无命中 + 信用分 >= 此值 + 无未处理举报
AUTO_APPROVE_MIN_CREDIT = int(os.getenv("AUTO_APPROVE_MIN_CREDIT", "70"))
# 信用分低于此值的卖家，商品一律不进自动通过（只调阈值，不作判罚依据）
STRICT_CREDIT = int(os.getenv("STRICT_CREDIT", "60"))
# LLM 置信度低于此值时转人工复核
REVIEW_THRESHOLD = float(os.getenv("REVIEW_THRESHOLD", "0.75"))

API_HOST = os.getenv("API_HOST", "127.0.0.1")
API_PORT = int(os.getenv("API_PORT", "8100"))
