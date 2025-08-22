"""
Telegram Gateway plugin for PyGPT.

What it does
------------
- Starts a Telegram bot in the background when the plugin is enabled.
- Reads the bot token from PyGPT's plugin options (editable in UI).
- For each incoming text message, calls into PyGPT to get a response.
- Sends the response back to the Telegram chat.

Requirements
------------
pip install python-telegram-bot>=20

Notes
-----
- This file is a standard PyGPT plugin: it exposes a class with:
    - id, name, version, description metadata
    - handle(self, event, *args, **kwargs) -> react to PyGPT events
- UI options are declared via BasePlugin.add_option and appear under
  “Telegram Gateway” in Plugins → Settings.

- Supports agent modes: watches for `agent_output` contexts and
  finalizes when `agent_finish` is signaled.

Fill the “ADAPTER” spot below to call PyGPT's chat pipeline. The two
options shown match the docs pattern; pick whichever matches your build.
"""

import asyncio
import logging
import os
import threading
import httpx
from typing import Optional, AsyncIterator

from pygpt_net.core.events import Event  # event enum (docs list the names)
from pygpt_net.plugin.base.plugin import BasePlugin
from pygpt_net.core.types import (
    MODE_AGENT_LLAMA,
    MODE_AGENT_OPENAI,
    AGENT_TYPE_LLAMA,
    AGENT_TYPE_OPENAI,
)

# Telegram (python-telegram-bot v20+)
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown
from telegram.error import TimedOut
from PySide6.QtCore import Signal, Slot

from .config import Config
from .worker import Worker

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)


class Plugin(BasePlugin):
    """Telegram gateway plugin"""

    dispatch_event = Signal(object)
    text_send = Signal(str, bool)
    ctx_new = Signal()
    mode_select = Signal(str)
    model_select = Signal(str)
    config_set = Signal(str, object)
    plugin_enable = Signal(str)
    plugin_disable = Signal(str)
    plugin_is_enabled = Signal(str, object, object)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.id = "telegram_gateway"
        self.name = "Telegram Gateway"
        self.version = "1.0.0"
        self.description = "Receive text from Telegram and reply using PyGPT."
        self.worker = None
        self.allowed_users = set()  # optional allowlist by Telegram user id
        self.config = Config(self)
        self.init_options()

        # connect helper signals to main-thread slots
        self.dispatch_event.connect(self._on_dispatch_event)
        self.text_send.connect(self._on_text_send)
        self.ctx_new.connect(self._on_ctx_new)
        self.mode_select.connect(self._on_mode_select)
        self.model_select.connect(self._on_model_select)
        self.config_set.connect(self._on_config_set)
        self.plugin_enable.connect(self._on_plugin_enable)
        self.plugin_disable.connect(self._on_plugin_disable)
        self.plugin_is_enabled.connect(self._on_plugin_is_enabled)

    def init_options(self):
        """Initialize options"""
        self.config.from_defaults(self)
        
        # Add timeout configuration options
        self.add_option(
            "response_timeout",
            type="int",
            value=60,
            label="Response timeout (seconds)",
            description="Maximum time to wait for PyGPT response before timing out",
        )
        
        self.add_option(
            "idle_window",
            type="float", 
            value=2.0,
            label="Idle window (seconds)",
            description="Time to wait after last response before considering completion",
        )

    # ---------- PyGPT lifecycle ----------

    def handle(self, event: Event, *args, **kwargs):
        """
        React to PyGPT-dispatched events. See the official event list.
        """
        name = event.name
        data = event.data or {}

        if name == Event.ENABLE:
            # Plugin toggled ON in the UI
            if data.get("value") == self.id:
                log.info("[TelegramGateway] ENABLE received; starting bot")
                self._start_bot()

        elif name == Event.DISABLE:
            if data.get("value") == self.id:
                log.info("[TelegramGateway] DISABLE received; stopping bot")
                self._stop_bot()

        elif name == Event.PLUGIN_SETTINGS_CHANGED:
            # User clicked "Save" in Plugins → Settings
            log.info("[TelegramGateway] Settings changed; restarting bot")
            self._restart_bot()

        elif name == Event.FORCE_STOP:
            # Application is exiting - ensure bot is properly stopped
            log.info("[TelegramGateway] FORCE_STOP received; stopping bot")
            self._stop_bot()

        # (Optional) If you want to expose a /syntax or inline commands,
        # you can also handle CMD_SYNTAX / CMD_SYNTAX_INLINE here and append help.

    # ---------- Telegram lifecycle ----------

    def _start_bot(self):
        self._stop_bot()  # idempotent
        token = (self.get_option_value("bot_token") or "").strip()
        allow = (self.get_option_value("allowed_user_ids") or "").strip()

        self.allowed_users = {
            int(x) for x in allow.split(",") if x.strip().isdigit()
        } if allow else set()

        if not token:
            log.warning("[TelegramGateway] Bot token is empty; not starting.")
            return

        try:
            worker = Worker()
            worker.from_defaults(self)
            worker.token = token

            worker.signals.started.connect(self.handle_started)
            worker.signals.stopped.connect(self.handle_stop)

            self.worker = worker
            worker.run_async()
        except Exception as e:
            self.error(e)

    def _stop_bot(self):
        if self.worker is not None:
            self.worker.stop()
            self.worker = None

    def _restart_bot(self):
        self._stop_bot()
        self._start_bot()

    def handle_started(self):
        log.info("[TelegramGateway] Telegram bot started")

    def handle_stop(self):
        log.info("[TelegramGateway] Telegram bot stopped")
        self.worker = None

    # ---------- Main-thread slots ----------

    @Slot(object)
    def _on_dispatch_event(self, event):
        self.window.dispatch(event)

    @Slot(str, bool)
    def _on_text_send(self, text: str, internal: bool = True):
        self.window.controller.chat.text.send(text, internal=internal)

    @Slot()
    def _on_ctx_new(self):
        self.window.controller.ctx.new_ungrouped()

    @Slot(str)
    def _on_mode_select(self, mode: str):
        self.window.controller.mode.select(mode)

    @Slot(str)
    def _on_model_select(self, model: str):
        self.window.controller.model.select(model)

    @Slot(str, object)
    def _on_config_set(self, key: str, value):
        self.window.core.config.set(key, value)

    @Slot(str)
    def _on_plugin_enable(self, plugin_id: str):
        self.window.controller.plugins.enable(plugin_id)

    @Slot(str)
    def _on_plugin_disable(self, plugin_id: str):
        self.window.controller.plugins.disable(plugin_id)

    @Slot(str, object, object)
    def _on_plugin_is_enabled(self, plugin_id: str, result: dict, done: threading.Event):
        try:
            result['value'] = self.window.controller.plugins.is_enabled(plugin_id)
        finally:
            done.set()

    # ---------- Main-thread helpers ----------

    def _dispatch_on_main(self, event):
        """Dispatch PyGPT event on the main thread safely."""
        try:
            self.dispatch_event.emit(event)
        except Exception as e:
            log.info("[TelegramGateway] Failed to dispatch event: {}".format(e))

    def _text_send_on_main(self, text: str, internal: bool = True):
        """Emit text send signal to main thread."""
        self.text_send.emit(text, internal)

    # ---------- Telegram handlers ----------

    async def _on_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_chat or not update.effective_user:
            return

        chat_id = update.effective_chat.id
        user_id = update.effective_user.id

        if self.allowed_users and user_id not in self.allowed_users:
            await context.bot.send_message(
                chat_id=chat_id,
                text="This bot is locked. Your Telegram user ID is not allowed.",
            )
            return

        try:
            self.ctx_new.emit()
            reply_text = escape_markdown("New context created.", version=2)
            await context.bot.send_message(
                chat_id=chat_id,
                text=reply_text,
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )

        except Exception as e:
            log.exception("Failed to create new context")
            reply_text = escape_markdown(f"⚠️ Error: {e}", version=2)
            await context.bot.send_message(
                chat_id=chat_id,
                text=reply_text,
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )

    async def _on_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_chat or not update.effective_user:
            return

        chat_id = update.effective_chat.id
        user_id = update.effective_user.id

        if self.allowed_users and user_id not in self.allowed_users:
            await context.bot.send_message(
                chat_id=chat_id,
                text="This bot is locked. Your Telegram user ID is not allowed.",
            )
            return

        args = context.args or []

        if not args:
            modes = ", ".join(self.window.core.modes.get_all().keys())
            reply_text = escape_markdown(
                f"Usage: /mode <name>\nAvailable modes: {modes}",
                version=2,
            )
            await context.bot.send_message(
                chat_id=chat_id,
                text=reply_text,
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
            return

        target_mode = args[0]
        available_modes = self.window.core.modes.get_all().keys()
        if target_mode not in available_modes:
            modes = ", ".join(available_modes)
            reply_text = escape_markdown(
                f"⚠️ Unknown mode: {target_mode}\nAvailable modes: {modes}",
                version=2,
            )
            await context.bot.send_message(
                chat_id=chat_id,
                text=reply_text,
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
            return

        try:
            self.mode_select.emit(target_mode)
            reply_text = escape_markdown(
                f"Mode switched to {target_mode}",
                version=2,
            )
        except Exception as e:
            reply_text = escape_markdown(f"⚠️ Error: {e}", version=2)

        await context.bot.send_message(
            chat_id=chat_id,
            text=reply_text,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )

    async def _on_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_chat or not update.effective_user:
            return

        chat_id = update.effective_chat.id
        user_id = update.effective_user.id

        if self.allowed_users and user_id not in self.allowed_users:
            await context.bot.send_message(
                chat_id=chat_id,
                text="This bot is locked. Your Telegram user ID is not allowed.",
            )
            return

        args = context.args or []
        current_mode = self.window.core.config.get('mode')

        if not args:
            models = ", ".join(self.window.core.models.get_by_mode(current_mode).keys())
            reply_text = escape_markdown(
                f"Usage: /model <name>\nAvailable models: {models}",
                version=2,
            )
            await context.bot.send_message(
                chat_id=chat_id,
                text=reply_text,
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
            return

        target_model = args[0]
        available_models = self.window.core.models.get_by_mode(current_mode).keys()
        if target_model not in available_models:
            models = ", ".join(available_models)
            reply_text = escape_markdown(
                f"⚠️ Unknown model: {target_model}\nAvailable models: {models}",
                version=2,
            )
            await context.bot.send_message(
                chat_id=chat_id,
                text=reply_text,
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
            return

        try:
            self.model_select.emit(target_model)
            reply_text = escape_markdown(
                f"Model switched to {target_model}",
                version=2,
            )
        except Exception as e:
            reply_text = escape_markdown(f"⚠️ Error: {e}", version=2)

        await context.bot.send_message(
            chat_id=chat_id,
            text=reply_text,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )

    async def _on_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_chat or not update.effective_user:
            return

        chat_id = update.effective_chat.id
        user_id = update.effective_user.id

        if self.allowed_users and user_id not in self.allowed_users:
            await context.bot.send_message(
                chat_id=chat_id,
                text="This bot is locked. Your Telegram user ID is not allowed.",
            )
            return

        commands = (
            "Supported commands:\n"
            "/new - start new context\n"
            "/mode <name> - switch mode\n"
            "/plugin <enable|disable> <plugin_id> - manage plugins\n"
            "/model <name> - switch model\n"
            "/agent <id> - switch agent\n"
            "/help - show this message"
        )
        reply_text = escape_markdown(commands, version=2)

        await context.bot.send_message(
            chat_id=chat_id,
            text=reply_text,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )

    async def _on_agent(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_chat or not update.effective_user:
            return

        chat_id = update.effective_chat.id
        user_id = update.effective_user.id

        if self.allowed_users and user_id not in self.allowed_users:
            await context.bot.send_message(
                chat_id=chat_id,
                text="This bot is locked. Your Telegram user ID is not allowed.",
            )
            return

        current_mode = self.window.core.config.get("mode")
        if current_mode == MODE_AGENT_LLAMA:
            config_key = "agent.llama.provider"
            agent_type = AGENT_TYPE_LLAMA
        elif current_mode == MODE_AGENT_OPENAI:
            config_key = "agent.openai.provider"
            agent_type = AGENT_TYPE_OPENAI
        else:
            reply_text = escape_markdown(
                "⚠️ Agent provider can be changed only in agent modes.",
                version=2,
            )
            await context.bot.send_message(
                chat_id=chat_id,
                text=reply_text,
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
            return

        args = context.args or []
        if not args:
            choices = self.window.core.agents.provider.get_choices(agent_type)
            agent_ids = ", ".join([list(item.keys())[0] for item in choices])
            reply_text = escape_markdown(
                f"Usage: /agent <id>\nAvailable agents: {agent_ids}",
                version=2,
            )
            await context.bot.send_message(
                chat_id=chat_id,
                text=reply_text,
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
            return

        agent_id = args[0]
        if not self.window.core.agents.provider.has(agent_id):
            choices = self.window.core.agents.provider.get_choices(agent_type)
            agent_ids = ", ".join([list(item.keys())[0] for item in choices])
            reply_text = escape_markdown(
                f"⚠️ Unknown agent: {agent_id}\nAvailable agents: {agent_ids}",
                version=2,
            )
            await context.bot.send_message(
                chat_id=chat_id,
                text=reply_text,
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
            return

        try:
            self.config_set.emit(config_key, agent_id)
            reply_text = escape_markdown(
                f"Agent provider switched to {agent_id}",
                version=2,
            )
        except Exception as e:
            reply_text = escape_markdown(f"⚠️ Error: {e}", version=2)

        await context.bot.send_message(
            chat_id=chat_id,
            text=reply_text,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )

    async def _on_plugin(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_chat or not update.effective_user:
            return

        chat_id = update.effective_chat.id
        user_id = update.effective_user.id

        if self.allowed_users and user_id not in self.allowed_users:
            await context.bot.send_message(
                chat_id=chat_id,
                text="This bot is locked. Your Telegram user ID is not allowed.",
            )
            return

        args = context.args or []

        if len(args) < 2:
            available_plugins = self.window.core.plugins.plugins.keys()
            plugins_list = ", ".join(available_plugins)
            reply_text = escape_markdown(
                f"Usage: /plugin <enable|disable> <plugin_id>\nAvailable plugins: {plugins_list}",
                version=2,
            )
            await context.bot.send_message(
                chat_id=chat_id,
                text=reply_text,
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
            return

        action = args[0].lower()
        plugin_id = args[1]

        if not self.window.core.plugins.is_registered(plugin_id):
            available_plugins = self.window.core.plugins.plugins.keys()
            plugins_list = ", ".join(available_plugins)
            reply_text = escape_markdown(
                f"⚠️ Unknown plugin: {plugin_id}\nAvailable plugins: {plugins_list}",
                version=2,
            )
            await context.bot.send_message(
                chat_id=chat_id,
                text=reply_text,
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
            return

        try:
            if action == "enable":
                self.plugin_enable.emit(plugin_id)
            elif action == "disable":
                self.plugin_disable.emit(plugin_id)
            else:
                reply_text = escape_markdown(
                    "Usage: /plugin <enable|disable> <plugin_id>",
                    version=2,
                )
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=reply_text,
                    parse_mode=ParseMode.MARKDOWN_V2,
                    disable_web_page_preview=True,
                )
                return

            result = {}
            done = threading.Event()
            self.plugin_is_enabled.emit(plugin_id, result, done)
            done.wait(timeout=5.0)
            state = "enabled" if result.get('value') else "disabled"
            reply_text = escape_markdown(
                f"Plugin {plugin_id} {state}.",
                version=2,
            )
        except Exception as e:
            reply_text = escape_markdown(f"⚠️ Error: {e}", version=2)

        await context.bot.send_message(
            chat_id=chat_id,
            text=reply_text,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )

    async def _on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_chat or not update.effective_user:
            return

        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        text = (update.message.text or "").strip()
        log.info("[TelegramGateway] Received text: '%s' from user %s", text, user_id)

        if self.allowed_users and user_id not in self.allowed_users:
            await context.bot.send_message(
                chat_id=chat_id,
                text="This bot is locked. Your Telegram user ID is not allowed.",
            )
            return

        if not text:
            return

        # Inform user we’re working
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")

        sent_any = False
        sent_images: set[str] = set()
        try:
            async for texts, images in self._ask_pygpt(text):
                log.info("[TelegramGateway] Got reply: %s texts, %s images", len(texts), len(images))
                if not texts and images and not sent_any:
                    placeholder = escape_markdown("image generated", version=2)
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=placeholder,
                        parse_mode=ParseMode.MARKDOWN_V2,
                        disable_web_page_preview=True,
                    )
                for reply_text in texts:
                    cleaned = reply_text.strip()
                    if not cleaned:
                        continue
                    sent_any = True
                    reply_text = escape_markdown(cleaned, version=2)
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=reply_text,
                        parse_mode=ParseMode.MARKDOWN_V2,
                        disable_web_page_preview=True,
                    )
                for img_path in images:
                    img_path = self.window.core.filesystem.to_workdir(img_path)
                    if img_path in sent_images:
                        continue
                    sent_images.add(img_path)
                    sent_any = True
                    try:
                        if not os.path.exists(img_path):
                            raise FileNotFoundError(f"Missing image: {img_path}")
                        with open(img_path, "rb") as fh:
                            try:
                                await context.bot.send_photo(
                                    chat_id=chat_id,
                                    photo=fh,
                                    read_timeout=30,
                                )
                            except (TimedOut, httpx.ReadTimeout):
                                log.warning(
                                    "Timed out while sending image %s; treating as sent",
                                    img_path,
                                )
                    except Exception:
                        log.exception("Failed to send image %s", img_path)
        except Exception as e:
            log.exception("PyGPT error")
            reply_text = escape_markdown(f"⚠️ Error while asking PyGPT: {e}", version=2)
            await context.bot.send_message(
                chat_id=chat_id,
                text=reply_text,
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
            return

        if not sent_any:
            reply_text = escape_markdown("(no response)", version=2)
            await context.bot.send_message(
                chat_id=chat_id,
                text=reply_text,
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )

    # ---------- Bridge into PyGPT ----------

    async def _ask_pygpt(self, user_text: str) -> AsyncIterator[tuple[list[str], list[str]]]:
        """
        Bridge: send `user_text` to PyGPT and stream the assistant's replies.
        Yields lists of new text chunks and generated images as they appear.
        """
        # Best-effort: notify plugins that user sent text
        try:
            self._dispatch_on_main(Event(Event.USER_SEND, {'value': user_text}))
        except Exception as e:
            log.info(f"[TelegramGateway] Failed to dispatch USER_SEND event {e}")

        # Initiate the chat turn via the Text controller
        self._text_send_on_main(user_text, internal=True)

        loop = asyncio.get_running_loop()
        
        # Use configurable timeout values  
        timeout = max(60, int(self.get_option_value("response_timeout") or 60))  # Default 60 seconds
        idle_window = max(2.0, float(self.get_option_value("idle_window") or 2.0))  # Default 2 seconds
        
        deadline = loop.time() + timeout
        kernel = self.window.controller.kernel
        start_time = loop.time()

        last_seen = loop.time()
        got_any = False
        
        # Get initial context state
        initial_ctx = self.window.core.ctx.get_last_item()
        initial_ctx_id = getattr(initial_ctx, "id", None) if initial_ctx else None
        log.info(f"[TelegramGateway] Starting with context ID: {initial_ctx_id}")
        
        # Track what we've already seen
        seen_outputs = set()
        seen_results_count = 0
        seen_images_count = 0
        
        # Initialize tracking variables
        if initial_ctx:
            if hasattr(initial_ctx, "output") and initial_ctx.output:
                seen_outputs.add(str(initial_ctx.output))
            if hasattr(initial_ctx, "results"):
                seen_results_count = len(initial_ctx.results or [])
            if hasattr(initial_ctx, "images"):
                seen_images_count = len(initial_ctx.images or [])

        consecutive_no_change = 0
        max_no_change_cycles = 8  # About 2 seconds at 0.25s intervals

        while loop.time() < deadline:
            try:
                # Get current context
                curr_ctx = self.window.core.ctx.get_last_item()
                curr_ctx_id = getattr(curr_ctx, "id", None) if curr_ctx else None
                
                if not curr_ctx:
                    log.debug("[TelegramGateway] No current context available")
                    await asyncio.sleep(0.25)
                    continue

                # Log context changes for debugging
                if curr_ctx_id != initial_ctx_id:
                    log.info(f"[TelegramGateway] Context changed from {initial_ctx_id} to {curr_ctx_id}")
                    # Reset tracking for new context
                    seen_outputs.clear()
                    seen_results_count = 0
                    seen_images_count = 0
                    initial_ctx_id = curr_ctx_id

                new_texts: list[str] = []
                new_images: list[str] = []
                found_new_content = False

                # Check for new output
                curr_output = getattr(curr_ctx, "output", None)
                if curr_output and str(curr_output).strip():
                    output_str = str(curr_output).strip()
                    if output_str not in seen_outputs:
                        log.info(f"[TelegramGateway] Found new output: '{output_str[:100]}{'...' if len(output_str) > 100 else ''}'")
                        seen_outputs.add(output_str)
                        new_texts.append(output_str)
                        found_new_content = True

                # Check for new results
                curr_results = getattr(curr_ctx, "results", []) or []
                if len(curr_results) > seen_results_count:
                    new_result_items = curr_results[seen_results_count:]
                    log.info(f"[TelegramGateway] Found {len(new_result_items)} new results")
                    for item in new_result_items:
                        if isinstance(item, dict):
                            value = item.get("result")
                            if value is not None:
                                new_texts.append(str(value))
                            else:
                                new_texts.append(str(item))
                        else:
                            new_texts.append(str(item))
                    seen_results_count = len(curr_results)
                    found_new_content = True

                # Check for new images
                curr_images = getattr(curr_ctx, "images", []) or []
                if len(curr_images) > seen_images_count:
                    new_image_items = curr_images[seen_images_count:]
                    log.info(f"[TelegramGateway] Found {len(new_image_items)} new images")
                    new_images.extend(new_image_items)
                    seen_images_count = len(curr_images)
                    found_new_content = True

                # Yield new content if found
                if new_texts or new_images:
                    log.info(
                        "[TelegramGateway] Yielding %d texts and %d images",
                        len(new_texts),
                        len(new_images),
                    )
                    last_seen = loop.time()
                    got_any = True
                    consecutive_no_change = 0
                    yield new_texts, new_images
                elif found_new_content:
                    # Reset no-change counter even if we didn't yield (empty content)
                    consecutive_no_change = 0
                    last_seen = loop.time()
                else:
                    consecutive_no_change += 1

                # Check various exit conditions
                curr_extra = getattr(curr_ctx, "extra", {}) or {}
                agent_finish = isinstance(curr_extra, dict) and curr_extra.get("agent_finish")
                
                # Check if context indicates completion
                is_stopped = getattr(curr_ctx, "stopped", False)
                is_current = getattr(curr_ctx, "current", True)
                
                # Log kernel state for debugging
                kernel_state = getattr(kernel, "state", None)
                log.debug(f"[TelegramGateway] Kernel state: {kernel_state}, stopped: {is_stopped}, current: {is_current}")

                # Exit conditions (only after we got some content)
                if got_any:
                    # Agent explicitly finished
                    if agent_finish:
                        log.info("[TelegramGateway] Agent signaled finish; exiting")
                        break
                    
                    # Context marked as stopped
                    if is_stopped:
                        log.info("[TelegramGateway] Context marked as stopped; exiting")
                        break
                    
                    # Kernel is not busy and we've been idle
                    if (hasattr(kernel, "STATE_BUSY") and 
                        kernel_state != kernel.STATE_BUSY and 
                        (loop.time() - last_seen) > idle_window):
                        log.info("[TelegramGateway] Kernel not busy and idle window expired; finishing")
                        break
                    
                    # Too many cycles without any content changes
                    if consecutive_no_change >= max_no_change_cycles:
                        log.info(f"[TelegramGateway] No changes for {consecutive_no_change} cycles; assuming complete")
                        break

                await asyncio.sleep(0.25)

            except Exception as e:
                log.exception(f"[TelegramGateway] Error in polling loop: {e}")
                await asyncio.sleep(0.25)
                continue

        elapsed_time = loop.time() - start_time
        log.info("[TelegramGateway] _ask_pygpt done (got_any: %s, time_elapsed: %.1fs)", 
                got_any, elapsed_time)

        # Only raise timeout if we genuinely got no response at all
        if not got_any:
            log.error("[TelegramGateway] Timeout: No response received from PyGPT")
            raise RuntimeError(f"Timed out waiting for PyGPT reply after {elapsed_time:.1f}s")
