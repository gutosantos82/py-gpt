#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ================================================== #
# This file is a part of PYGPT package               #
# Website: https://pygpt.net                         #
# GitHub:  https://github.com/szczyglis-dev/py-gpt   #
# MIT License                                        #
# Created By  : Marcin Szczygliński                  #
# Updated Date: 2025.02.16 00:00:00                  #
# ================================================== #

from pygpt_net.plugin.base.config import BaseConfig, BasePlugin


class Config(BaseConfig):
    def __init__(self, plugin: BasePlugin = None, *args, **kwargs):
        super(Config, self).__init__(plugin)
        self.plugin = plugin

    def from_defaults(self, plugin: BasePlugin = None):
        """Set default options for plugin"""
        plugin.add_option(
            "bot_token",
            type="text",
            value="",
            label="Telegram Bot Token",
            description="Create a bot with @BotFather and paste the token here.",
            secret=True,
        )
        plugin.add_option(
            "allowed_user_ids",
            type="text",
            value="",
            label="Allowed Telegram User IDs (comma-separated, optional)",
            description="Leave blank to allow anyone who knows the bot username.",
        )
