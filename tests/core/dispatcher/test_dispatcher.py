#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ================================================== #
# This file is a part of PYGPT package               #
# Website: https://pygpt.net                         #
# GitHub:  https://github.com/szczyglis-dev/py-gpt   #
# MIT License                                        #
# Created By  : Marcin Szczygliński                  #
# Updated Date: 2024.11.20 03:00:00                  #
# ================================================== #

import json
import os
from unittest.mock import MagicMock, patch

from tests.mocks import mock_window
from pygpt_net.core.dispatcher import Dispatcher
from pygpt_net.core.events import Event

def test_dispatch(mock_window):
    """Test dispatch"""
    dispatcher = Dispatcher(mock_window)
    event = Event('test')
    dispatcher.apply = MagicMock()
    mock_window.core.plugins.plugins = {'test1': {}, 'test2': {}, 'test3': {}}
    mock_window.controller.plugins.is_enabled = MagicMock(return_value=True)
    affected, event = dispatcher.dispatch(event)
    assert affected == ['test1', 'test2', 'test3']
    assert event.name == 'test'


def test_apply(mock_window):
    """Test apply"""
    dispatcher = Dispatcher(mock_window)
    event = Event('test')
    mock_window.core.plugins.plugins = {'test1': MagicMock()}
    mock_window.controller.plugins.is_enabled = MagicMock(return_value=True)
    dispatcher.apply('test1', event)
    mock_window.core.plugins.plugins['test1'].handle.assert_called_once_with(event)

'''

def test_reply(mock_window):
    """Test reply"""
    dispatcher = Dispatcher(mock_window)
    ctx = MagicMock()
    ctx.reply = True
    ctx.results = {'test': 'test'}
    ctx.internal = False
    ctx.output = None
    ctx.extra_ctx = None
    ctx.prev_ctx = None
    ctx.sub_call = False
    ctx.agent_call = False
    ctx.pid = 0

    prev_ctx = MagicMock()
    dispatcher.reply_idx = -1
    dispatcher.window.core.ctx.as_previous = MagicMock(return_value=prev_ctx)
    dispatcher.window.core.ctx.update_item = MagicMock()
    dispatcher.window.controller.chat.input.send = MagicMock()
    dispatcher.reply(ctx, flush=True)
    dispatcher.window.core.ctx.update_item.assert_called_once()
    dispatcher.window.controller.chat.input.send.assert_called_once_with(
        text=json.dumps(['test']),
        force=True,
        reply=True,
        internal=True,
        prev_ctx=prev_ctx,
        parent_id=None,
    )
'''