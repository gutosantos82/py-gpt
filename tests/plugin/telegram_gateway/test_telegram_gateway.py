# This file is a part of PYGPT package               #
# Website: https://pygpt.net                         #
# GitHub:  https://github.com/szczyglis-dev/py-gpt   #
# MIT License                                        #
# Created By  : OpenAI Assistant                     #
# Updated Date: 2024.04.10                           #
# ================================================== #

import asyncio
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch, mock_open

from telegram.helpers import escape_markdown
from telegram.constants import ParseMode

from pygpt_net.item.ctx import CtxItem
from pygpt_net.config import Config
from plugins.telegram_gateway import Plugin
import pytest


class DummyInvoker:
    def __init__(self, parent=None):
        self.invoke = MagicMock()
        self.invoke.emit = MagicMock()


@pytest.fixture
def mock_window():
    window = MagicMock()
    window.STATE_IDLE = 'idle'
    window.STATE_BUSY = 'busy'
    window.STATE_ERROR = 'error'
    window.state = MagicMock()
    window.stateChanged = MagicMock()
    window.idx_logger_message = MagicMock()
    window.core = MagicMock()
    window.core.config = Config(window)
    window.core.config.initialized = True
    window.core.config.init = MagicMock()
    window.core.config.load = MagicMock()
    window.core.config.save = MagicMock()
    window.core.config.get_lang = MagicMock(return_value='en')
    window.core.debug = MagicMock()
    window.controller = MagicMock()
    window.controller.kernel = MagicMock()
    window.controller.kernel.STATE_BUSY = 'busy'
    window.controller.kernel.state = 'idle'
    window.tools = MagicMock()
    window.ui = MagicMock()
    window.threadpool = MagicMock()
    window.dispatch = MagicMock()
    window.update_status = MagicMock()
    window.update_state = MagicMock()
    window.core.ctx = MagicMock()
    window.core.filesystem = MagicMock()
    return window


def test_ask_pygpt_monitors_sub_reply(mock_window):
    with patch("plugins.telegram_gateway.MainThreadInvoker", DummyInvoker):
        plugin = Plugin(window=mock_window)

    initial = CtxItem()
    initial.output = None
    initial.results = []
    initial.images = []

    sub = CtxItem()
    sub.sub_reply = True
    sub.output = "here"
    sub.results = []
    sub.images = ["img.png"]

    plugin._text_send_on_main = MagicMock(return_value=initial)

    call = {"n": 0}

    def get_last_item():
        call["n"] += 1
        return sub if call["n"] > 1 else initial

    mock_window.core.ctx.get_last_item = get_last_item

    async def run():
        with patch("plugins.telegram_gateway.asyncio.sleep", new=AsyncMock()):
            agen = plugin._ask_pygpt("test")
            texts, images = await asyncio.wait_for(anext(agen), timeout=1)
            await agen.aclose()
            return texts, images

    texts, images = asyncio.run(run())
    assert texts == ["here"]
    assert images == ["img.png"]


def test_ask_pygpt_monitors_extra_sub_reply(mock_window):
    with patch("plugins.telegram_gateway.MainThreadInvoker", DummyInvoker):
        plugin = Plugin(window=mock_window)

    initial = CtxItem()
    initial.output = None
    initial.results = []
    initial.images = []

    sub = CtxItem()
    sub.output = "here"
    sub.results = []
    sub.images = ["img.png"]
    sub.extra["sub_reply"] = True

    plugin._text_send_on_main = MagicMock(return_value=initial)

    call = {"n": 0}

    def get_last_item():
        call["n"] += 1
        return sub if call["n"] > 1 else initial

    mock_window.core.ctx.get_last_item = get_last_item

    async def run():
        with patch("plugins.telegram_gateway.asyncio.sleep", new=AsyncMock()):
            agen = plugin._ask_pygpt("test")
            texts, images = await asyncio.wait_for(anext(agen), timeout=1)
            await agen.aclose()
            return texts, images

    texts, images = asyncio.run(run())
    assert texts == ["here"]
    assert images == ["img.png"]


@patch("plugins.telegram_gateway.os.path.exists", return_value=True)
@patch("plugins.telegram_gateway.open", new_callable=mock_open, read_data=b"data")
def test_on_text_forwards_tool_reply(mock_open_fn, mock_exists, mock_window):
    with patch("plugins.telegram_gateway.MainThreadInvoker", DummyInvoker):
        plugin = Plugin(window=mock_window)

    async def fake_ask(_):
        yield ["done"], ["img.png"]

    plugin._ask_pygpt = fake_ask
    mock_window.core.filesystem.to_workdir = lambda x: x

    bot = MagicMock()
    bot.send_chat_action = AsyncMock()
    bot.send_message = AsyncMock()
    bot.send_photo = AsyncMock()

    context = MagicMock()
    context.bot = bot

    update = MagicMock()
    update.effective_chat.id = 1
    update.effective_user.id = 2
    update.message.text = "hi"

    asyncio.run(plugin._on_text(update, context))

    bot.send_message.assert_any_call(
        chat_id=1,
        text=escape_markdown("done", version=2),
        parse_mode=ParseMode.MARKDOWN_V2,
        disable_web_page_preview=True,
    )
    bot.send_photo.assert_called_once()


@patch("plugins.telegram_gateway.os.path.exists", return_value=True)
@patch("plugins.telegram_gateway.open", new_callable=mock_open, read_data=b"data")
def test_on_text_deduplicates_images(mock_open_fn, mock_exists, mock_window):
    with patch("plugins.telegram_gateway.MainThreadInvoker", DummyInvoker):
        plugin = Plugin(window=mock_window)

    async def fake_ask(_):
        yield ["one"], ["img.png"]
        yield ["two"], ["img.png"]

    plugin._ask_pygpt = fake_ask
    mock_window.core.filesystem.to_workdir = lambda x: x

    bot = MagicMock()
    bot.send_chat_action = AsyncMock()
    bot.send_message = AsyncMock()
    bot.send_photo = AsyncMock()

    context = MagicMock()
    context.bot = bot

    update = MagicMock()
    update.effective_chat.id = 1
    update.effective_user.id = 2
    update.message.text = "hi"

    asyncio.run(plugin._on_text(update, context))

    bot.send_photo.assert_called_once()


def test_on_text_ignores_empty_chunks(mock_window):
    with patch("plugins.telegram_gateway.MainThreadInvoker", DummyInvoker):
        plugin = Plugin(window=mock_window)

    async def fake_ask(_):
        yield ["", "   ", "done"], []

    plugin._ask_pygpt = fake_ask

    bot = MagicMock()
    bot.send_chat_action = AsyncMock()
    bot.send_message = AsyncMock()

    context = MagicMock()
    context.bot = bot

    update = MagicMock()
    update.effective_chat.id = 1
    update.effective_user.id = 2
    update.message.text = "hi"

    asyncio.run(plugin._on_text(update, context))

    bot.send_message.assert_called_once_with(
        chat_id=1,
        text=escape_markdown("done", version=2),
        parse_mode=ParseMode.MARKDOWN_V2,
        disable_web_page_preview=True,
    )


def test_on_text_no_response_when_all_empty(mock_window):
    with patch("plugins.telegram_gateway.MainThreadInvoker", DummyInvoker):
        plugin = Plugin(window=mock_window)

    async def fake_ask(_):
        yield ["", "  "], []

    plugin._ask_pygpt = fake_ask

    bot = MagicMock()
    bot.send_chat_action = AsyncMock()
    bot.send_message = AsyncMock()

    context = MagicMock()
    context.bot = bot

    update = MagicMock()
    update.effective_chat.id = 1
    update.effective_user.id = 2
    update.message.text = "hi"

    asyncio.run(plugin._on_text(update, context))

    bot.send_message.assert_called_once_with(
        chat_id=1,
        text=escape_markdown("(no response)", version=2),
        parse_mode=ParseMode.MARKDOWN_V2,
        disable_web_page_preview=True,
    )


def test_stop_bot_joins_thread(mock_window):
    with patch("plugins.telegram_gateway.MainThreadInvoker", DummyInvoker):
        plugin = Plugin(window=mock_window)

    plugin.options["bot_token"]["value"] = "123"

    async def fake_tg_main(self, token):
        class DummyUpdater:
            async def stop(self):
                pass

        class DummyApp:
            def __init__(self):
                self.updater = DummyUpdater()

            async def stop(self):
                pass

            async def shutdown(self):
                pass

        self.state.tg_app = DummyApp()
        while not self.state.stop_event.is_set():
            await asyncio.sleep(0.01)

    with patch.object(Plugin, "_tg_main", fake_tg_main):
        plugin._start_bot()
        time.sleep(0.05)
        plugin._stop_bot()
        assert plugin.state.thread is None
        assert not any(t.name == "tg-gateway" for t in threading.enumerate())


def test_on_help_lists_commands(mock_window):
    with patch("plugins.telegram_gateway.MainThreadInvoker", DummyInvoker):
        plugin = Plugin(window=mock_window)

    bot = MagicMock()
    bot.send_message = AsyncMock()

    context = MagicMock()
    context.bot = bot

    update = MagicMock()
    update.effective_chat.id = 1
    update.effective_user.id = 2

    asyncio.run(plugin._on_help(update, context))

    expected = escape_markdown(
        "Supported commands:\n"
        "/new - start new context\n"
        "/mode <name> - switch mode\n"
        "/plugin <enable|disable> <plugin_id> - manage plugins\n"
        "/model <name> - switch model\n"
        "/agent <id> - switch agent\n"
        "/help - show this message",
        version=2,
    )
    bot.send_message.assert_called_once_with(
        chat_id=1,
        text=expected,
        parse_mode=ParseMode.MARKDOWN_V2,
        disable_web_page_preview=True,
    )
