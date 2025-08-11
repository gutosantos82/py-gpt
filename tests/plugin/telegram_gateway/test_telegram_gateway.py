# This file is a part of PYGPT package               #
# Website: https://pygpt.net                         #
# GitHub:  https://github.com/szczyglis-dev/py-gpt   #
# MIT License                                        #
# Created By  : OpenAI Assistant                     #
# Updated Date: 2024.04.10                           #
# ================================================== #

import asyncio
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
