#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ================================================== #
# This file is a part of PYGPT package               #
# Website: https://pygpt.net                         #
# GitHub:  https://github.com/szczyglis-dev/py-gpt   #
# MIT License                                        #
# Created By  : Marcin Szczygliński                  #
# Updated Date: 2025.08.11 14:00:00                  #
# ================================================== #

import asyncio
import threading
from contextlib import suppress

from PySide6.QtCore import Slot

from pygpt_net.plugin.base.worker import BaseWorker
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters


class Worker(BaseWorker):
    def __init__(self, *args, **kwargs):
        super(Worker, self).__init__()
        self.args = args
        self.kwargs = kwargs
        self.token = ""
        self.loop = None
        self.tg_app = None
        self.stop_event = threading.Event()
        self.is_shutting_down = False

    def stop(self):
        if self.is_shutting_down:
            return
        self.is_shutting_down = True
        self.stop_event.set()
        
        if self.loop and self.tg_app:
            try:
                async def _shutdown():
                    with suppress(RuntimeError):
                        await self.tg_app.updater.stop()
                    with suppress(RuntimeError):
                        await self.tg_app.stop()
                    with suppress(RuntimeError):
                        await self.tg_app.shutdown()

                future = asyncio.run_coroutine_threadsafe(_shutdown(), self.loop)
                future.result(timeout=5)  # Reduced timeout for faster shutdown
            except Exception:
                pass

    def safe_signal_emit(self, signal_method):
        """Safely emit a signal, handling cases where signal objects have been deleted"""
        try:
            signal_method()
        except (RuntimeError, AttributeError):
            # Signal object has been deleted or is no longer available
            pass

    @Slot()
    def run(self):
        try:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.safe_signal_emit(self.started)
            self.loop.run_until_complete(self._tg_main())
        except Exception as e:
            self.safe_signal_emit(lambda: self.error(e))
        finally:
            if not self.is_shutting_down:
                self.stop()
            self.safe_signal_emit(self.stopped)
            self.safe_signal_emit(self.cleanup)

    async def _tg_main(self):
        app = (
            ApplicationBuilder()
            .token(self.token)
            .read_timeout(30)
            .build()
        )

        self.tg_app = app

        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.plugin._on_text))
        app.add_handler(CommandHandler("new", self.plugin._on_new))
        app.add_handler(CommandHandler("mode", self.plugin._on_mode))
        app.add_handler(CommandHandler("plugin", self.plugin._on_plugin))
        app.add_handler(CommandHandler("model", self.plugin._on_model))
        app.add_handler(CommandHandler("help", self.plugin._on_help))
        app.add_handler(CommandHandler("agent", self.plugin._on_agent))

        await app.initialize()
        await app.start()
        try:
            await app.updater.start_polling()
            while not self.stop_event.is_set():
                await asyncio.sleep(0.25)
        finally:
            with suppress(RuntimeError):
                await app.updater.stop()
            with suppress(RuntimeError):
                await app.stop()
            with suppress(RuntimeError):
                await app.shutdown()
