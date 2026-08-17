# -*- coding: utf-8 -*-
"""
test_error_middleware — 未預期例外的對外回應（資通系統防護基準第 55 點）
====================================================================
「發生錯誤時，使用者頁面僅顯示簡短錯誤訊息及代碼，不包含詳細之錯誤訊息」。
這裡驗證三件事：例外細節不會出現在回應裡、回應帶得到可對日誌的代碼、
以及各 handler 刻意 raise 的 HTTPException（401/404/409…）不會被誤攔。

另外驗證 500 也有安全標頭——error_middleware 刻意排在
security_headers_middleware 內層就是為了這個（見 routes.py create_app）。

用法（專案根目錄）：
    python -m unittest discover -s tests -v
"""

import json
import logging
import os
import sys
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

from monitor.web.routes import error_middleware
from monitor.web.security_headers import security_headers_middleware

# 例外訊息裡塞進「絕對不該外流」的字串，用來反證回應沒有洩漏
SECRET = "資料庫密碼 hunter2 於 C:/secret/store.sqlite3"


async def _boom(request):
    raise RuntimeError(SECRET)


async def _deliberate_404(request):
    raise web.HTTPNotFound(text='{"error": "未知裝置"}',
                           content_type="application/json")


class ErrorMiddlewareTest(AioHTTPTestCase):

    async def get_application(self):
        app = web.Application(middlewares=[security_headers_middleware,
                                           error_middleware])
        app["tls_enabled"] = False
        app.router.add_get("/boom", _boom)
        app.router.add_get("/missing", _deliberate_404)
        return app

    def setUp(self):
        super().setUp()
        # 這些測試會刻意觸發例外，traceback 本來就會被記錄；
        # 關掉輸出避免污染測試報告（不影響 middleware 行為）
        self._web_log = logging.getLogger("web")
        self._saved = self._web_log.handlers[:], self._web_log.propagate
        self._web_log.handlers = [logging.NullHandler()]
        self._web_log.propagate = False

    def tearDown(self):
        self._web_log.handlers, self._web_log.propagate = self._saved
        super().tearDown()

    async def test_未預期例外回500且不洩漏細節(self):
        resp = await self.client.get("/boom")
        self.assertEqual(resp.status, 500)
        body = await resp.text()
        for leak in ("hunter2", "secret", "sqlite3", "RuntimeError", "Traceback",
                     "File \"", "monitor/web"):
            self.assertNotIn(leak, body, f"回應洩漏內部細節: {leak}")

    async def test_回應帶可對日誌的代碼(self):
        resp = await self.client.get("/boom")
        data = json.loads(await resp.text())
        self.assertTrue(data.get("error"))
        self.assertTrue(data.get("code"), "缺少讓使用者回報、供日誌定位的代碼")

    async def test_每次例外的代碼不重複(self):
        first = json.loads(await (await self.client.get("/boom")).text())["code"]
        second = json.loads(await (await self.client.get("/boom")).text())["code"]
        self.assertNotEqual(first, second)

    async def test_traceback只進伺服器日誌(self):
        with self.assertLogs("web", level="ERROR") as captured:
            await self.client.get("/boom")
        logged = "\n".join(captured.output)
        self.assertIn(SECRET, logged, "伺服器端日誌應保有完整例外供排查")
        self.assertIn("Traceback", logged)

    async def test_500也有安全標頭(self):
        resp = await self.client.get("/boom")
        self.assertTrue(resp.headers.get("Content-Security-Policy"))
        self.assertEqual(resp.headers.get("X-Content-Type-Options"), "nosniff")

    async def test_刻意的HTTP例外照原樣放行(self):
        resp = await self.client.get("/missing")
        self.assertEqual(resp.status, 404)
        self.assertEqual(json.loads(await resp.text())["error"], "未知裝置")


if __name__ == "__main__":
    unittest.main()
