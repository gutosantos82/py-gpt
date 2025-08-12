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
import threading
import os
import httpx
from dataclasses import dataclass, field
from typing import Optional, AsyncIterator
from contextlib import suppress

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
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
)
from telegram.helpers import escape_markdown
from telegram.error import TimedOut
from PySide6.QtCore import QTimer, QObject, Signal

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)


@dataclass
class _BotState:
    token: Optional[str] = None
    app_task: Optional[asyncio.Task] = None
    loop: Optional[asyncio.AbstractEventLoop] = None
    thread: Optional[threading.Thread] = None
    tg_app: Optional["telegram.ext.Application"] = None  # type: ignore
    stop_event: threading.Event = field(default_factory=threading.Event)


class MainThreadInvoker(QObject):
    """
    Helper to marshal callables onto the Qt main thread via a queued signal.
    """
    invoke = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.invoke.connect(self._on_invoke)

    def _on_invoke(self, fn):
        try:
            fn()
        except Exception:
            # The wrapper handles error propagation back to the waiting thread.
            pass


class Plugin(BasePlugin):
    """Telegram gateway plugin"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.id = "telegram_gateway"
        self.name = "Telegram Gateway"
        self.version = "1.0.0"
        self.description = "Receive text from Telegram and reply using PyGPT."
        self.state = _BotState()  # runtime TG state
        self.allowed_users = set()  # optional allowlist by Telegram user id
        self._invoker = MainThreadInvoker(parent=self.window)
        # plugin options
        self.add_option(
            "bot_token",
            type="text",
            label="Telegram Bot Token",
            description="Create a bot with @BotFather and paste the token here.",
            secret=True,
        )
        self.add_option(
            "allowed_user_ids",
            type="text",
            label="Allowed Telegram User IDs (comma-separated, optional)",
            description="Leave blank to allow anyone who knows the bot username.",
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

        self.state.stop_event.clear()

        def _runner():
            self.state.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.state.loop)
            self.state.loop.run_until_complete(self._tg_main(token))

        self.state.thread = threading.Thread(target=_runner, name="tg-gateway", daemon=True)
        self.state.thread.start()
        log.info("[TelegramGateway] Telegram bot started")

    def _stop_bot(self):
        self.state.stop_event.set()
        if self.state.loop and self.state.tg_app:
            log.info("[TelegramGateway] Stopping Telegram bot...")
            loop = self.state.loop
            app = self.state.tg_app
            try:
                async def _shutdown():
                    with suppress(RuntimeError):
                        await app.updater.stop()
                    with suppress(RuntimeError):
                        await app.stop()
                    with suppress(RuntimeError):
                        await app.shutdown()

                asyncio.run_coroutine_threadsafe(_shutdown(), loop).result(timeout=10)
            except Exception as e:
                log.debug("shutdown exception: %s", e)

        thread = self.state.thread
        if thread and thread.is_alive():
            thread.join(timeout=10)

        self.state.tg_app = None
        self.state.loop = None
        self.state.thread = None
        log.info("[TelegramGateway] Telegram bot stopped")

    def _restart_bot(self):
        self._stop_bot()
        self._start_bot()

    # ---------- Main-thread helpers ----------

    def _call_on_main(self, fn):
        """
        Execute callable on the Qt main thread and return its result.
        Blocks current thread until completion or timeout.
        """
        # If already on the main thread, execute directly
        if threading.current_thread() is threading.main_thread():
            return fn()

        # If invoker is not available for any reason, fall back (may fail without event loop)
        if not hasattr(self, "_invoker") or self._invoker is None:
            done = threading.Event()
            out = {}

            def wrapper():
                try:
                    out["result"] = fn()
                except Exception as e:
                    out["error"] = e
                finally:
                    done.set()

            QTimer.singleShot(0, wrapper)
            if not done.wait(timeout=15.0):
                raise RuntimeError("Main thread call timeout")
            if "error" in out:
                raise out["error"]
            return out.get("result")

        # Normal path: marshal via Qt signal to the main thread
        done = threading.Event()
        out = {}

        def wrapper():
            try:
                out["result"] = fn()
            except Exception as e:
                out["error"] = e
            finally:
                done.set()

        # Emit to main thread via queued connection
        self._invoker.invoke.emit(wrapper)

        # wait up to 15s for the call to complete
        if not done.wait(timeout=15.0):
            raise RuntimeError("Main thread call timeout")
        if "error" in out:
            raise out["error"]
        return out.get("result")

    def _dispatch_on_main(self, event):
        """Dispatch PyGPT event on the main thread safely."""
        try:
            self._call_on_main(lambda: self.window.dispatch(event))
        except Exception:
            pass

    def _text_send_on_main(self, text: str, internal: bool = True):
        """
        Call Text.send on the main thread and return ctx.
        This avoids triggering UI render from a background thread.
        """
        return self._call_on_main(lambda: self.window.controller.chat.text.send(text, internal=internal))

    async def _tg_main(self, token: str):
        app = (
            ApplicationBuilder()
                .token(token)
                .read_timeout(30)
                .build()
        )

        # Message handler: plain text only
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_text))
        app.add_handler(CommandHandler("new", self._on_new))
        app.add_handler(CommandHandler("mode", self._on_mode))
        app.add_handler(CommandHandler("plugin", self._on_plugin))
        app.add_handler(CommandHandler("model", self._on_model))
        app.add_handler(CommandHandler("help", self._on_help))
        app.add_handler(CommandHandler("agent", self._on_agent))

        # Optionally, you can add a /start or /help command handler
        # from telegram.ext import CommandHandler
        # app.add_handler(CommandHandler("start", self._on_start))

        await app.initialize()
        self.state.tg_app = app
        await app.start()
        # Idle loop (non-blocking because we run in a thread)
        try:
            await app.updater.start_polling()
            while not self.state.stop_event.is_set():
                await asyncio.sleep(0.25)
        finally:
            with suppress(RuntimeError):
                await app.updater.stop()
            with suppress(RuntimeError):
                await app.stop()
            with suppress(RuntimeError):
                await app.shutdown()

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
            self._call_on_main(lambda: self.window.controller.ctx.new_ungrouped())
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
            self._call_on_main(lambda: self.window.controller.mode.select(target_mode))
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
            self._call_on_main(lambda: self.window.controller.model.select(target_model))
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
            self._call_on_main(lambda: self.window.core.config.set(config_key, agent_id))
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
                self._call_on_main(lambda: self.window.controller.plugins.enable(plugin_id))
            elif action == "disable":
                self._call_on_main(lambda: self.window.controller.plugins.disable(plugin_id))
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

            is_enabled = self._call_on_main(
                lambda: self.window.controller.plugins.is_enabled(plugin_id)
            )
            state = "enabled" if is_enabled else "disabled"
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
        except Exception:
            pass

        # Initiate the chat turn via the Text controller
        try:
            loop = asyncio.get_running_loop()
            ctx = await loop.run_in_executor(
                None, lambda: self._text_send_on_main(user_text, internal=True)
            )
        except Exception as e:
            raise RuntimeError(f"Bridge call failed: {e}")

        loop = asyncio.get_running_loop()
        deadline = loop.time() + 120.0  # seconds
        idle_window = 60.0

        last_seen = loop.time()
        got_any = False
        curr_ctx = ctx
        prev_output = getattr(curr_ctx, "output", None)
        prev_results_len = len(getattr(curr_ctx, "results", []) or [])
        prev_images_len = len(getattr(curr_ctx, "images", []) or [])

        while loop.time() < deadline:
            last_ctx = self.window.core.ctx.get_last_item()
            if (
                last_ctx is not None
                and last_ctx is not curr_ctx
                and (
                    getattr(last_ctx, "sub_reply", False)
                    or getattr(last_ctx, "agent_output", False)
                    or (
                        isinstance(getattr(last_ctx, "extra", None), dict)
                        and (
                            last_ctx.extra.get("sub_reply")
                            or last_ctx.extra.get("agent_output")
                        )
                    )
                )
            ):
                curr_ctx = last_ctx
                prev_output = None
                prev_results_len = 0
                prev_images_len = 0

            curr_output = getattr(curr_ctx, "output", None)
            curr_results = getattr(curr_ctx, "results", []) or []
            curr_images = getattr(curr_ctx, "images", []) or []
            curr_extra = getattr(curr_ctx, "extra", {}) or {}
            agent_finish = (
                isinstance(curr_extra, dict) and curr_extra.get("agent_finish")
            )

            new_texts: list[str] = []
            new_images: list[str] = []

            if curr_output != prev_output and curr_output is not None:
                new_texts.append(str(curr_output))
                prev_output = curr_output

            if len(curr_results) > prev_results_len:
                for item in curr_results[prev_results_len:]:
                    if isinstance(item, dict):
                        value = item.get("result")
                        new_texts.append(str(value) if value is not None else str(item))
                    else:
                        new_texts.append(str(item))
                prev_results_len = len(curr_results)

            if len(curr_images) > prev_images_len:
                new_images = list(curr_images[prev_images_len:])
                prev_images_len = len(curr_images)

            if new_texts or new_images:
                log.info(
                    "[TelegramGateway] Yielding %s new texts and %s new images",
                    len(new_texts),
                    len(new_images),
                )
                last_seen = loop.time()
                got_any = True
                yield new_texts, new_images

            if agent_finish:
                log.info("[TelegramGateway] Agent signaled finish; exiting")
                break

            if got_any and (loop.time() - last_seen) > idle_window:
                log.info("[TelegramGateway] Idle window expired; finishing")
                break

            await asyncio.sleep(0.25)

        log.info("[TelegramGateway] _ask_pygpt done")

        if not got_any:
            raise RuntimeError("Timed out waiting for PyGPT reply")
