"""A minimal HTTP server, started only when a $PORT env var is present.

This exists purely so the bot can run as a **free** Render (or similar) Web
Service, which requires binding to a port — a Background Worker or a plain
VM doesn't need this at all, so on those it never starts (no `PORT` set).

It does not replace Telegram long-polling; the bot still talks to Telegram
the same way it always has. This just gives an external uptime pinger
(UptimeRobot, cron-job.org, ...) something to hit every few minutes so the
free instance never goes to sleep — see README "Deploying to Render (free)".
"""

import logging
import time

from aiohttp import web

logger = logging.getLogger("qpoll.webserver")

_started_at = time.monotonic()


async def _root(request):
    return web.Response(text="quality_poll bot is running\n")


async def _healthz(request):
    return web.json_response({
        "status": "ok",
        "uptime_seconds": int(time.monotonic() - _started_at),
    })


def build_app():
    app = web.Application()
    app.router.add_get("/", _root)
    app.router.add_get("/healthz", _healthz)
    return app


async def start(port):
    """Start the server and return its AppRunner (call .cleanup() on it to stop)."""
    runner = web.AppRunner(build_app())
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(
        "Health-check web server listening on 0.0.0.0:%d (/, /healthz) — "
        "point an uptime pinger at /healthz to keep a free Render instance awake",
        port,
    )
    return runner
