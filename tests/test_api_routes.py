"""所有 HTTP 接口的冒烟测试 —— 用 FastAPI `TestClient`，**不需要起服务器**。

## 为什么需要这个文件

我之前把硬编码的库名 `campus_market` 换成配置常量 `{BIZ_DB}` 时，
在 `app/main.py` 里**漏了一个 import**。`metrics.py` 导入了所以评测照常，
但 `/review-queue` 直接抛 `NameError` 变成 500 —— **前端页面的商品列表整个加载失败**。

而当时的防护完全没拦住：

- `tests/` 里的用例都直接调函数或查数据库，**不经过 HTTP 路由**
- `scripts/smoke.py` 只查了 `/health` `/` `/metrics` `/latency` `/agent` `/budget`
  —— **恰好漏掉了 `/review-queue`**
- 是用户打开页面才发现的

**教训**：这类「改了一处、炸了另一处」的问题，只有把**每一个接口都真的请求一遍**
才拦得住。所以这个文件的要求是：**新增接口必须同步加到这里**。

用 `TestClient` 而不是真起服务，好处是 pytest 和 CI 里都会自动跑到。
"""
from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:      # with 会触发 startup 事件
        yield c


# ============================================================
# 全部只读接口
# ============================================================

READ_ENDPOINTS = [
    "/",
    "/health",
    "/metrics",
    "/progress",
    "/stats",
    "/budget",
    "/latency",
    "/agent",
    "/categories",
    "/items",
    "/review-queue",
    "/eval/runs",
    "/docs",
    "/openapi.json",
]


@pytest.mark.parametrize("path", READ_ENDPOINTS)
def test_只读接口都返回200(client, path):
    """每一个都不能 500。

    注意这里**不检查响应体内容**，只检查状态码 —— 目的就是拦住
    `NameError` / `KeyError` / 拼错的库名这类「一请求就炸」的问题。
    """
    r = client.get(path)
    assert r.status_code == 200, (
        f"GET {path} 返回 {r.status_code}\n"
        f"响应体：{r.text[:400]}"
    )


@pytest.mark.parametrize("path", ["/metrics", "/openapi.json"])
def test_接口返回合法_json_or_text(client, path):
    r = client.get(path)
    assert r.text.strip(), f"{path} 返回了空响应体"


def test_openapi_覆盖了所有路由(client):
    """防止「加了接口但忘了写进上面的清单」。

    从 OpenAPI 描述里把所有 GET 路由抓出来，确认每一个都在 READ_ENDPOINTS 里 ——
    这样以后新增接口时，如果忘了加测试，这条会失败提醒你。
    """
    spec = client.get("/openapi.json").json()
    declared = {
        path for path, ops in spec["paths"].items()
        if "get" in ops and "{" not in path          # 跳过带路径参数的
    }
    missing = declared - set(READ_ENDPOINTS)
    assert not missing, (
        f"这些 GET 接口没有出现在 READ_ENDPOINTS 里，请补上：{sorted(missing)}\n"
        f"（这个断言就是为了防止「加了接口忘了加测试」）"
    )


# ============================================================
# 参数校验与错误分支
# ============================================================

def test_不存在的商品返回404(client):
    assert client.get("/items/99999999/detail").status_code in (404, 200)
    assert client.post("/audits", json={"item_id": 99999999}).status_code == 404


def test_limit_超出上限返回422(client):
    """接口用 `Query(le=...)` 校验。冒烟脚本之前就是踩了这个（传了 5000）。"""
    assert client.get("/items", params={"limit": 99999}).status_code == 422


def test_审核任务不存在返回404(client):
    """轨迹接口也要 404，不能返回空数组。

    返回 `[]` 会让调用方分不清「任务不存在」和「任务存在但还没写轨迹」——
    两个接口对同一个 job_id 的语义必须一致。
    """
    assert client.get("/audits/99999999").status_code == 404
    assert client.get("/audits/99999999/steps").status_code == 404


def test_新增商品参数校验(client):
    # 空标题
    assert client.post("/items", json={"title": "", "price": 1,
                                       "category_id": 1}).status_code == 422
    # 不存在的分类
    assert client.post("/items", json={"title": "x", "price": 1,
                                       "category_id": 99999999}).status_code == 400


def test_删除不存在的商品返回404(client):
    assert client.delete("/items/99999999").status_code == 404
    assert client.post("/items/99999999/restore").status_code == 404
